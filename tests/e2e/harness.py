"""End-to-end harness: runs the real Kitchen server as a subprocess and talks MCP to it."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "offers-2026-07-30.json"
MONDAY = "2026-07-27"  # offers in the fixture are valid 2026-07-26 .. 2026-08-01


class ToolFailed(Exception):
    def __init__(self, tool: str, payload: dict[str, Any]) -> None:
        super().__init__(f"{tool}: {json.dumps(payload, ensure_ascii=False)[:600]}")
        self.tool, self.payload = tool, payload
        self.code = (payload.get("error") or {}).get("code")


class Kitchen:
    """Thin MCP client wrapper: `await k.call("plan_meals", days=3)` returns the response envelope."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.log: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        args = {k: v for k, v in args.items() if v is not None}
        self.log.append((tool, args))
        result = await self.client.call_tool(tool, args)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raw = text.split(": ", 1)[1] if text.startswith("Error executing tool") else text
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"error": {"code": "unparsed", "message": text}}
            raise ToolFailed(tool, payload)
        data = result.structured_content if result.structured_content is not None else json.loads(text)
        if set(data) == {"result"}:
            data = data["result"]
        assert {"summary", "next_actions", "ask_user", "warnings"} <= set(data), f"{tool} broke the envelope: {list(data)}"
        return data

    async def fails(self, tool: str, **args: Any) -> ToolFailed:
        try:
            await self.call(tool, **args)
        except ToolFailed as failure:
            return failure
        raise AssertionError(f"{tool} should have failed")


def server_env(day: str, hour: int = 10, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KITCHEN_")}
    env.update({"KITCHEN_TODAY": day, "KITCHEN_HOUR": str(hour), "KITCHEN_NO_REFRESH": "1", "KITCHEN_LOG": "WARNING",
                "PYTHONPATH": str(ROOT)})
    env.update(extra or {})
    return env


@contextlib.asynccontextmanager
async def kitchen(db: Path, day: str = MONDAY, hour: int = 10) -> AsyncIterator[Kitchen]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "kitchen.server", "--db", str(db), "--offers", str(FIXTURE)],
        env=server_env(day, hour), cwd=str(ROOT))
    async with Client(params) as client:
        yield Kitchen(client)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def http_server(db: Path, token: str, day: str = MONDAY):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "kitchen.server", "--transport", "http", "--port", str(port), "--db", str(db), "--offers", str(FIXTURE)],
        env=server_env(day, extra={"KITCHEN_TOKEN": token}), cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(proc.stderr.read().decode() if proc.stderr else "server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@contextlib.asynccontextmanager
async def http_kitchen(base: str, token: str) -> AsyncIterator[Kitchen]:
    transport = streamable_http_client(f"{base}/mcp", http_client=create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}))
    async with Client(transport) as client:
        yield Kitchen(client)


def items(proposal_or_list: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Shopping items by name from a plan proposal or a get_shopping_list response."""
    data = proposal_or_list["data"]
    found: dict[str, dict[str, Any]] = {}
    for trip in data.get("shopping", []):
        for item in trip["items"]:
            found[item["name"]] = item | {"store": trip["store"], "date": trip["date"]}
    for trip in data.get("trips", []):
        for section in trip["sections"].values():
            for item in section:
                found[item["name"]] = item | {"store": trip["store"], "date": trip["date"]}
    return found


CURRY = {"name": "Hähnchen-Curry", "servings": 2, "tags": ["schnell"], "prep_min": 10, "cook_min": 20,
         "ingredients": ["400 g Hähnchenbrust", "1 Zwiebel", "400 ml Kokosmilch", "150 g Basmatireis", "1 EL Currypulver", "1 Paprika"]}
OMELETT = {"name": "Spinat-Omelett", "servings": 1, "ingredients": ["3 Eier", "100 g Spinat", "Salz"]}
LACHS = {"name": "Ofenlachs mit Kartoffeln", "servings": 2,
         "ingredients": ["250 g Lachsfilet", "500 g Kartoffeln", "200 g Brokkoli", "1 EL Olivenöl"]}
PASTA = {"name": "Pasta mit Tomatensoße", "servings": 2, "tags": ["vegetarisch"],
         "ingredients": ["250 g Spaghetti", "400 g passierte Tomaten", "1 Zwiebel", "1 Knoblauchzehe", "40 g Parmesan"]}
CARBONARA = {"name": "Pasta Carbonara", "servings": 2,
             "ingredients": ["250 g Spaghetti", "100 g Speck", "2 Eier", "50 g Parmesan", "100 ml Sahne"]}
