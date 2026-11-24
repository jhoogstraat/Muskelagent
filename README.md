# Muskelagent – Kitchen Agent

An MCP server that turns Claude (or ChatGPT/Gemini) into a kitchen assistant: it plans meals, builds the
shopping list from current supermarket offers, tracks the pantry with best-before dates, books purchases and
cooking, plans leftovers, and enforces allergies. You only talk to your agent; the server does every
calculation and tells the agent what to do next.

Design: the specification “Kitchen Agent – Systemdesign”; decisions and deviations in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Everyday use

Talk normally. Examples:

| You say | What happens |
| --- | --- |
| „Plan mir die nächsten 5 Abende, viel Protein, max. 50 €“ | Draft plan using pantry and leftovers first, offers, budget; you approve with „passt“ |
| „Dienstag lieber vegetarisch“, „nur ein Laden“ | Plan revised; shopping list recalculated |
| „Was muss ich bei Lidl holen?“ | List by store and aisle, packs, prices, offers |
| „War einkaufen, alles außer Feta“ | Everything booked into the pantry; asks about a replacement for Feta |
| „Curry gekocht, zwei Portionen übrig“ | Ingredients deducted, leftovers stored and planned next |
| „Samstag essen wir auswärts“ | Meal skipped, reserved food freed, unneeded items taken off the list |
| „Was muss bald weg?“ / „Hähnchen eingefroren“ | Pantry by expiry / moved to the freezer with a new date |
| „Ich vertrage keine Laktose“, „kein Aldi mehr“ | Saved as hard rules / store preferences |
| „Mach das rückgängig“ | Last change undone |

## Tools (MCP)

17 intent tools: `get_kitchen_status` (entry point with guide), `plan_meals`, `revise_plan`, `confirm_plan`,
`get_shopping_list`, `update_shopping_list`, `record_purchase`, `record_cooking`, `skip_meal`, `get_pantry`,
`update_pantry`, `find_recipes`, `save_recipe`, `manage_household`, `find_offers`, `get_insights`,
`undo_last_action`. Every answer is an envelope with `summary`, `data`, `next_actions`, `ask_user`, `warnings`.

## Run

```bash
uv sync                                   # once
.venv/bin/python -m kitchen.server        # stdio (default)
KITCHEN_TOKEN=… .venv/bin/python -m kitchen.server --transport http --host 0.0.0.0   # Streamable HTTP on :8000/mcp
```

- Database: `~/.local/share/muskelagent/kitchen.sqlite3` (`--db` / `KITCHEN_DB`).
- Offers: fetched from Muskelmarkt in the background once a day; `--offers file.json` seeds the cache, `--no-refresh` disables fetching.
- The old command line `python3 offer_mcp_server.py --offers …` still works (it re-launches with `.venv`), so the existing tunnel profile keeps running. `mise run tunnel:configure` updates it to the new command.
- Claude Desktop / Claude Code (stdio):

```json
{"mcpServers": {"kitchen": {"command": "/absolute/path/.venv/bin/python", "args": ["/absolute/path/offer_mcp_server.py"]}}}
```

- Container (Raspberry Pi or any host): `docker compose up -d` or `podman compose up -d` (`.env` with `KITCHEN_TOKEN`;
  `KITCHEN_HOST_PORT` if 8000 is taken; `--profile tunnel` plus `TUNNEL_TOKEN` for Cloudflare).

## Tests

End-to-end only.

```bash
mise run test         # 21 scenarios over MCP against the real server process (stdio + HTTP), pinned to a fixture week
mise run test:agent   # Claude itself plays a week from plain German sentences; transcript in tests/agent/last-run.md
```

## Offer extractor

`muskelmarkt_extractor.py` converts the offer cards on [MuskelMarkt](https://www.muskelmarkt.de/app1.php)
(Aldi, Edeka, Kaufland, Lidl, Netto, Penny, Rewe, …) into versioned JSON. The server runs it daily; you can
also run it by hand (`mise run offers:refresh`). Pack sizes come from the title or price ÷ unit price.
