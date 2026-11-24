#!/usr/bin/env python3
"""Agent-in-the-loop end-to-end test: Claude talks to the Kitchen server from plain user sentences.

Claude gets only this MCP server and a neutral chat-app system prompt (no tool rules), so the server's
descriptions, envelopes and guide must carry the flow. After each turn the database is checked directly.

    .venv/bin/python tests/agent/run_agent_e2e.py [--model sonnet]

Costs a few API calls; writes a transcript to tests/agent/last-run.md.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "offers-2026-07-30.json"
SYSTEM = ("Du bist Claude in einer Chat-App. Der Nutzer hat den Connector „Kitchen Agent“ verbunden, "
          "der seine Küche, Einkäufe und Mahlzeiten verwaltet. Antworte auf Deutsch.")


class Run:
    def __init__(self, model: str, workdir: Path) -> None:
        self.model, self.workdir = model, workdir
        self.db = workdir / "kitchen.sqlite3"
        self.session: str | None = None
        self.transcript: list[str] = []
        self.results: list[tuple[str, bool, str]] = []

    def config(self, day: str, hour: int) -> Path:
        path = self.workdir / f"mcp-{day}-{hour}.json"
        path.write_text(json.dumps({"mcpServers": {"kitchen": {
            "command": sys.executable, "args": ["-m", "kitchen.server", "--db", str(self.db), "--offers", str(FIXTURE)],
            "cwd": str(ROOT),
            "env": {"KITCHEN_TODAY": day, "KITCHEN_HOUR": str(hour), "KITCHEN_NO_REFRESH": "1", "PYTHONPATH": str(ROOT),
                    "KITCHEN_LOG": "WARNING"}}}}))
        return path

    def say(self, text: str, day: str, hour: int = 10) -> tuple[str, list[dict[str, Any]]]:
        cmd = ["claude", "-p", text, "--model", self.model, "--output-format", "stream-json", "--verbose",
               "--system-prompt", SYSTEM + f" Aktuelle Uhrzeit beim Nutzer: {day} {hour}:00.",
               "--mcp-config", str(self.config(day, hour)), "--strict-mcp-config", "--tools", "",
               "--allowedTools", "mcp__kitchen", "--max-budget-usd", "2"]
        if self.session:
            cmd += ["--resume", self.session]
        started = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=self.workdir)
        calls, answer = [], ""
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.session = event.get("session_id") or self.session
            if event.get("type") == "assistant":
                for block in event["message"].get("content", []):
                    if block.get("type") == "tool_use":
                        calls.append({"tool": block["name"].split("__")[-1], "input": block.get("input", {})})
            if event.get("type") == "result":
                answer = event.get("result") or ""
        if not answer and proc.returncode:
            answer = f"(claude exited {proc.returncode}: {proc.stderr[-500:]})"
        self.transcript.append(f"### Nutzer ({day} {hour}:00)\n\n{text}\n\n**Tool-Aufrufe** ({time.time() - started:.0f}s): "
                               + (", ".join(c["tool"] for c in calls) or "keine")
                               + "\n\n" + "\n".join(f"- `{c['tool']}` {json.dumps(c['input'], ensure_ascii=False)[:400]}" for c in calls)
                               + f"\n\n### Claude\n\n{answer}\n")
        return answer, calls

    def check(self, name: str, fn: Callable[[], Any]) -> None:
        try:
            ok = bool(fn())
            detail = ""
        except Exception as exc:  # A failing check is a result, not a crash.
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append((name, ok, detail))
        self.transcript.append(f"- {'✅' if ok else '❌'} {name} {detail}\n")
        print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}", flush=True)

    def q(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sonnet")
    args = parser.parse_args()
    run = Run(args.model, Path(tempfile.mkdtemp(prefix="kitchen-agent-e2e-")))
    tools = lambda calls: [c["tool"] for c in calls]  # noqa: E731

    _, calls = run.say("Hi! Kurz zu mir: Ich wohne allein, vertrage keine Laktose und kaufe meistens bei Lidl oder Aldi Nord. "
                       "Kannst du mir die Abendessen für die nächsten 3 Tage planen? Gern proteinreich.", "2026-07-27")
    run.check("Laktose als Regel gespeichert", lambda: run.q("SELECT 1 FROM restriction WHERE value = 'lactose'"))
    run.check("Läden Lidl und Aldi Nord gespeichert",
              lambda: {r["name"] for r in run.q("SELECT name FROM store")} >= {"Lidl", "Aldi Nord"})
    run.check("Planentwurf existiert, nicht ohne Zustimmung bestätigt",
              lambda: run.q("SELECT 1 FROM meal_plan WHERE status = 'draft'") and not run.q("SELECT 1 FROM meal_plan WHERE status = 'confirmed'"))
    run.check("Alle 3 Abende haben ein Gericht",
              lambda: (p := json.loads(run.q("SELECT proposal FROM meal_plan ORDER BY updated_at DESC LIMIT 1")[0]["proposal"]))
              and not p["open_slots"] and sum(1 for m in p["meals"] if m.get("recipe")) >= 3)
    run.check("Kein Gericht verstößt gegen Laktose",
              lambda: not any(m.get("violations") for m in json.loads(run.q("SELECT proposal FROM meal_plan ORDER BY updated_at DESC LIMIT 1")[0]["proposal"])["meals"]))

    _, calls = run.say("Klingt gut, mach so.", "2026-07-27")
    run.check("confirm_plan aufgerufen", lambda: "confirm_plan" in tools(calls))
    run.check("Plan bestätigt mit Einkaufsliste",
              lambda: run.q("SELECT 1 FROM meal_plan WHERE status = 'confirmed'") and run.q("SELECT 1 FROM shopping_item WHERE status = 'open'"))

    answer, calls = run.say("Was muss ich einkaufen?", "2026-07-27")
    open_items = [r["name"] for r in run.q("SELECT i.name FROM shopping_item s JOIN ingredient i ON i.id = s.ingredient_id "
                                            "WHERE s.status = 'open' AND s.check_at_home = 0 ORDER BY s.est_price_cents")]
    run.check("Einkaufsliste geholt", lambda: "get_shopping_list" in tools(calls))
    run.check("Antwort nennt Posten der Liste", lambda: sum(1 for n in open_items if n.split()[0] in answer) >= max(1, len(open_items) // 2))

    missing = open_items[0] if open_items else "Paprika"
    _, calls = run.say(f"Bin zurück vom Einkaufen. Hat alles geklappt, nur {missing} gab es nicht.", "2026-07-27", 18)
    run.check("record_purchase aufgerufen", lambda: "record_purchase" in tools(calls))
    run.check(f"{missing} als fehlend gebucht", lambda: run.q(
        "SELECT 1 FROM shopping_item s JOIN ingredient i ON i.id = s.ingredient_id WHERE s.status = 'missing' AND i.name = ?", missing))
    run.check("Rest liegt im Vorrat", lambda: len(run.q("SELECT 1 FROM lot WHERE purchase_line_id IS NOT NULL")) >= len(open_items) - 1)

    _, calls = run.say("Hab gestern das geplante Essen gekocht, eine Portion ist übrig geblieben.", "2026-07-28", 19)
    run.check("record_cooking aufgerufen", lambda: "record_cooking" in tools(calls))
    run.check("Mahlzeit vom 27. als gekocht gebucht", lambda: run.q("SELECT 1 FROM meal_slot WHERE date_on = '2026-07-27' AND status = 'cooked'"))
    run.check("1 Portion Reste angelegt", lambda: run.q("SELECT 1 FROM lot WHERE kind = 'prepared' AND qty = 1"))

    _, calls = run.say("Ups, ich hab mich vertan – mach den letzten Eintrag bitte rückgängig.", "2026-07-28", 19)
    run.check("undo_last_action aufgerufen", lambda: "undo_last_action" in tools(calls))
    run.check("Kochen zurückgenommen", lambda: run.q("SELECT 1 FROM meal_slot WHERE date_on = '2026-07-27' AND status = 'planned'")
              and not run.q("SELECT 1 FROM lot WHERE kind = 'prepared'"))

    _, calls = run.say("Was sollte ich heute kochen, und muss irgendwas bald weg?", "2026-07-28", 17)
    run.check("Status oder Vorrat abgefragt", lambda: {"get_kitchen_status", "get_pantry"} & set(tools(calls)))

    supply = "SELECT s.* FROM supply_state s JOIN ingredient i ON i.id = s.id WHERE i.name = ?"
    on_list = ("SELECT 1 FROM shopping_item s JOIN ingredient i ON i.id = s.ingredient_id "
               "WHERE s.status = 'open' AND i.name = ?")
    _, calls = run.say("Übrigens: Klopapier und Spülmittel sind fast alle. Und Zahnpasta hab ich heute noch mitgenommen.",
                       "2026-07-28", 20)
    run.check("Klopapier und Spülmittel als knapp/leer gebucht",
              lambda: all(r and r[0]["level"] in ("low", "empty") for r in (run.q(supply, "Toilettenpapier"), run.q(supply, "Spülmittel"))))
    run.check("Klopapier und Spülmittel stehen auf der Einkaufsliste",
              lambda: run.q(on_list, "Toilettenpapier") and run.q(on_list, "Spülmittel"))
    run.check("Zahnpasta als gekauft gebucht", lambda: (r := run.q(supply, "Zahnpasta")) and r[0]["last_bought_on"] == "2026-07-28")
    run.check("Keine Haushaltsartikel als Lebensmittel angelegt",
              lambda: not run.q("SELECT 1 FROM ingredient WHERE is_builtin = 0 AND is_supply = 0 AND name NOT IN ('Reste')"))

    answer, calls = run.say("Wie lange reicht eigentlich noch meine Zahnpasta?", "2026-08-10", 9)
    run.check("Vorrat oder Status für Reichweite abgefragt", lambda: {"get_kitchen_status", "get_pantry"} & set(tools(calls)))
    run.check("Antwort nennt eine Reichweite", lambda: any(ch.isdigit() for ch in answer) or "bald" in answer.lower())

    passed = sum(1 for _, ok, _ in run.results if ok)
    report = ROOT / "tests" / "agent" / "last-run.md"
    report.write_text(f"# Agent-E2E ({args.model}) – {passed}/{len(run.results)} Checks bestanden\n\n" + "\n".join(run.transcript),
                      encoding="utf-8")
    print(f"\n{passed}/{len(run.results)} checks passed. Transcript: {report}")
    return 0 if passed == len(run.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
