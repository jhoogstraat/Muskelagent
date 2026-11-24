"""End-to-end scenarios over MCP: each test is a user story, played against the real server process."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from harness import CARBONARA, CURRY, FIXTURE, LACHS, MONDAY, OMELETT, PASTA, ROOT, http_kitchen, http_server, items, kitchen

INTENT_TOOLS = {
    "get_kitchen_status", "plan_meals", "revise_plan", "confirm_plan", "get_shopping_list", "update_shopping_list",
    "record_purchase", "record_cooking", "skip_meal", "get_pantry", "update_pantry", "find_recipes", "save_recipe",
    "manage_household", "find_offers", "get_insights", "undo_last_action",
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "kitchen.sqlite3"


def test_catalogue_of_tools_guides_the_agent(db):
    async def scenario():
        async with kitchen(db) as k:
            tools = (await k.client.list_tools()).tools
            names = {t.name for t in tools}
            assert names == INTENT_TOOLS
            for tool in tools:
                words = len((tool.description or "").split())
                assert 15 <= words <= 150, f"{tool.name}: {words} words"
                assert tool.annotations is not None
            read_only = {t.name for t in tools if t.annotations.read_only_hint}
            assert read_only == {"get_kitchen_status", "get_shopping_list", "get_pantry", "find_recipes", "find_offers", "get_insights"}
            assert "get_kitchen_status" in (k.client.instructions or "")
            prompts = {p.name for p in (await k.client.list_prompts()).prompts}
            assert {"woche_planen", "einkauf_erfassen"} <= prompts
    run(scenario())


def test_first_contact_asks_onboarding_once(db):
    async def scenario():
        async with kitchen(db) as k:
            status = await k.call("get_kitchen_status")
            assert status["ask_user"] and "Personen" in status["ask_user"][0]["question"]
            assert "guide" in status["data"]
            saved = await k.call("manage_household", members=[{"name": "Josh"}],
                                 restrictions=[{"kind": "intolerance", "value": "Laktose"}],
                                 stores=[{"name": "lidl"}, {"name": "Aldi Nord"}],
                                 nutrition_targets=[{"nutrient": "Protein", "daily_min": 140}])
            assert {s["name"] for s in saved["data"]["stores"]} == {"Lidl", "Aldi Nord"}
            assert saved["data"]["restrictions"][0]["value"] == "lactose"
            again = await k.call("get_kitchen_status")
            assert not any("Personen" in q["question"] for q in again["ask_user"])
    run(scenario())


def test_week_from_plan_to_leftovers(db):
    """Plan → revise → confirm → shop with a missing item → cook with leftovers → skip a day → insights."""
    async def monday():
        async with kitchen(db, MONDAY) as k:
            await k.call("manage_household", members=[{"name": "Josh"}], stores=[{"name": "Lidl"}, {"name": "Aldi Nord"}])
            await k.call("update_pantry", items=[{"name": "6 Eier"}, {"name": "500 g Spinat", "best_before": "2026-07-29"},
                                                 {"name": "Salz"}, {"name": "Olivenöl", "action": "level", "level": "voll"}])
            draft = await k.call("plan_meals", days=3, meals=[{"recipe": CURRY}, {"recipe": OMELETT}])
            proposal = draft["data"]
            assert [m["recipe"] for m in proposal["meals"]][:2] == ["Hähnchen-Curry", "Spinat-Omelett"]
            assert proposal["open_slots"] == [{"date": "2026-07-29", "meal_type": "dinner"}]
            assert draft["next_actions"][0]["tool"] == "revise_plan"  # agent must fill the gap before showing the plan
            shop = items(draft)
            assert "Spinat" not in shop and "Ei" not in shop, "pantry stock must not be bought again"
            assert shop["Paprika"]["buy"].endswith("Stück (lose)")
            revised = await k.call("revise_plan", proposal_id=proposal["proposal_id"],
                                   replace_meals=[{"date": "Mittwoch", "recipe": LACHS}])
            assert revised["data"]["open_slots"] == []
            assert revised["data"]["meals"][0]["recipe"] == "Hähnchen-Curry", "other days stay as they were"
            stale = await k.fails("confirm_plan", proposal_id=proposal["proposal_id"])
            assert stale.code == "stale_proposal"
            confirmed = await k.call("confirm_plan", proposal_id=revised["data"]["proposal_id"])
            assert confirmed["summary"].startswith("Plan bestätigt")
            again = await k.call("confirm_plan", proposal_id=revised["data"]["proposal_id"])
            assert "Gleicher Aufruf" in again["warnings"][0] or "bereits" in again["summary"]

            shopping = await k.call("get_shopping_list")
            on_list = items(shopping)
            assert {"Hähnchenbrust", "Lachs", "Paprika", "Kokosmilch"} <= set(on_list)
            assert on_list["Hähnchenbrust"]["for"], "items say which meal they are for"
            pantry = await k.call("get_pantry", query="Ei")
            assert pantry["data"]["items"][0]["reserved_for_meals"]

            bought = await k.call("record_purchase", missing=["Paprika"], total_eur=31.40)
            assert "Paprika" in bought["data"]["missing"]
            assert bought["ask_user"] and "Hähnchen-Curry" in bought["ask_user"][0]["question"]
            assert (await k.call("get_shopping_list"))["data"]["trips"] == []
            stock = {i["name"] for i in (await k.call("get_pantry"))["data"]["items"]}
            assert {"Hähnchenbrust", "Lachs", "Kokosmilch"} <= stock

    async def tuesday():
        async with kitchen(db, "2026-07-28", hour=19) as k:
            status = await k.call("get_kitchen_status")
            assert any("gestern" in q["question"] for q in status["ask_user"]), "asks about yesterday's planned meal"
            cooked = await k.call("record_cooking", recipe="Hähnchen-Curry", date="gestern", servings_cooked=2, servings_eaten=1)
            assert cooked["data"]["leftover_servings"] == 1
            assert any("Hähnchenbrust" in line for line in cooked["data"]["consumed"])
            pantry = await k.call("get_pantry")
            leftovers = [i for i in pantry["data"]["items"] if i["leftover"]]
            assert leftovers and leftovers[0]["name"] == "Hähnchen-Curry (Reste)"
            chicken = [i for i in pantry["data"]["items"] if i["name"] == "Hähnchenbrust"]
            assert not chicken or chicken[0]["qty"] != "400 g"
            skipped = await k.call("skip_meal", date="Mittwoch", reason="auswärts")
            assert "gestrichen" in skipped["summary"]
            omelett = await k.call("record_cooking")  # tonight's planned meal, no arguments needed
            assert omelett["data"]["recipe"] == "Spinat-Omelett"
            insights = await k.call("get_insights", topic="overview")
            assert insights["data"]["spending"]["total"] == "31,40 €"
            assert insights["data"]["cooking"]["meals"] == 2

    run(monday())
    run(tuesday())


def test_allergies_are_enforced_by_the_server(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", restrictions=[{"kind": "intolerance", "value": "Laktose"}], stores_answered=True)
            await k.call("save_recipe", recipe=PASTA)
            saved = await k.call("save_recipe", recipe=CARBONARA)
            assert any("Haushaltsregeln" in w for w in saved["warnings"])
            auto = await k.call("plan_meals", days=2)
            chosen = {m["recipe"] for m in auto["data"]["meals"]}
            assert "Pasta Carbonara" not in chosen, "auto planning never picks a recipe that breaks a rule"
            forced = await k.call("plan_meals", days=1, meals=[{"recipe": "Pasta Carbonara"}])
            assert forced["data"]["meals"][0]["violations"]
            blocked = await k.fails("confirm_plan", proposal_id=forced["data"]["proposal_id"])
            assert blocked.code == "rule_violation"
            assert blocked.payload["next_actions"][0]["tool"] == "revise_plan"
    run(scenario())


def test_vegetarian_rule_and_dislikes(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", restrictions=[{"kind": "diet", "value": "vegetarisch"}],
                         preferences=[{"value": "Pilze", "like": False}], stores_answered=True)
            for recipe in (CURRY, PASTA, LACHS):
                await k.call("save_recipe", recipe=recipe)
            plan = await k.call("plan_meals", days=3)
            planned = [m["recipe"] for m in plan["data"]["meals"] if m.get("recipe")]
            assert planned == ["Pasta mit Tomatensoße"], planned
            assert len(plan["data"]["open_slots"]) == 2
    run(scenario())


def test_ambiguous_words_trigger_a_question_not_a_guess(db):
    async def scenario():
        async with kitchen(db) as k:
            failure = await k.fails("save_recipe", recipe={"name": "Käsebrot", "servings": 1, "ingredients": ["2 Scheiben Brot", "50 g Käse"]})
            assert failure.code == "needs_clarification"
            options = failure.payload["ask_user"][0]["options"]
            assert len(options) >= 3 and all("Brot" not in o for o in options), options
            ok = await k.call("save_recipe", recipe={"name": "Käsebrot", "servings": 1, "ingredients": ["100 g Vollkornbrot", "50 g Gouda"]})
            assert "gespeichert" in ok["summary"]
    run(scenario())


def test_duplicate_calls_and_undo(db):
    async def scenario():
        async with kitchen(db) as k:
            first = await k.call("update_pantry", items=[{"name": "6 Eier"}])
            repeat = await k.call("update_pantry", items=[{"name": "6 Eier"}])
            assert "Gleicher Aufruf" in repeat["warnings"][0]
            eggs = (await k.call("get_pantry", query="Ei"))["data"]["items"]
            assert eggs[0]["qty"] == "6 Stück"
            await k.call("update_pantry", items=[{"name": "Ei", "action": "use", "qty": 2}])
            assert (await k.call("get_pantry", query="Ei"))["data"]["items"][0]["qty"] == "4 Stück"
            undone = await k.call("undo_last_action")
            assert "Rückgängig" in undone["summary"]
            assert (await k.call("get_pantry", query="Ei"))["data"]["items"][0]["qty"] == "6 Stück"
            await k.call("undo_last_action")
            assert (await k.call("get_pantry", query="Ei"))["data"]["items"] == []
            assert first["summary"].startswith("Vorrat aktualisiert")
    run(scenario())


def test_undo_a_confirmed_plan_restores_everything(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            draft = await k.call("plan_meals", days=1, meals=[{"recipe": CURRY}])
            await k.call("confirm_plan", proposal_id=draft["data"]["proposal_id"])
            assert (await k.call("get_shopping_list"))["data"]["trips"]
            await k.call("undo_last_action")
            assert (await k.call("get_shopping_list"))["data"]["trips"] == []
            status = await k.call("get_kitchen_status")
            assert status["data"]["pending_proposal"] == draft["data"]["proposal_id"]
    run(scenario())


def test_budget_and_store_limits(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            plan = await k.call("plan_meals", days=3, budget_eur=5, meals=[{"recipe": CURRY}, {"recipe": LACHS}, {"recipe": PASTA}])
            assert plan["data"]["totals"]["over_budget_cents"] > 0
            assert any("Budget" in q["question"] for q in plan["ask_user"])
            one = await k.call("revise_plan", proposal_id=plan["data"]["proposal_id"], max_stores=1, budget_eur=60)
            by_date: dict[str, set[str]] = {}
            for trip in one["data"]["shopping"]:
                by_date.setdefault(trip["date"], set()).add(trip["store"])
            assert all(len(stores) == 1 for stores in by_date.values()), by_date
            assert one["data"]["totals"]["over_budget_cents"] == 0
    run(scenario())


def test_offers_flow_into_the_basket(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            offers = await k.call("find_offers", query="Hähnchenbrust", sort_by="price")
            assert offers["data"]["offers"], "fixture has chicken offers"
            big = {"name": "Meal-Prep Hähnchen", "servings": 6, "ingredients": ["1500 g Hähnchenbrust", "600 g Reis", "900 g Brokkoli"]}
            plan = await k.call("plan_meals", days=1, servings=6, meals=[{"recipe": big}])
            chicken = items(plan)["Hähnchenbrust"]
            assert chicken["price_source"] == "Angebot", chicken
            assert chicken["offer"]
    run(scenario())


def test_leftovers_are_planned_first(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            await k.call("save_recipe", recipe=PASTA)
            cooked = await k.call("record_cooking", recipe="Pasta mit Tomatensoße", servings_cooked=3, servings_eaten=1)
            assert cooked["data"]["leftover_servings"] == 2
            plan = await k.call("plan_meals", start="morgen", days=3)
            meals = plan["data"]["meals"]
            assert meals[0].get("leftover") and meals[1].get("leftover"), meals
            assert meals[2]["recipe"] is None or meals[2]["recipe"] != "Pasta mit Tomatensoße"
    run(scenario())


def test_manual_shopping_list_and_freezing(db):
    async def scenario():
        async with kitchen(db) as k:
            added = await k.call("update_shopping_list", add=["Milch", "2 Packungen Haferflocken"], store="Lidl")
            assert "Milch" in added["summary"]
            lst = items(await k.call("get_shopping_list"))
            assert lst["Haferflocken"]["buy"].startswith("2") and lst["Milch"]["store"] == "Lidl"
            await k.call("update_shopping_list", remove=["Milch"])
            assert "Milch" not in items(await k.call("get_shopping_list"))
            await k.call("record_purchase", bought=["1 kg Hähnchenbrust"], store="Lidl")
            fresh = (await k.call("get_pantry", query="Hähnchenbrust"))["data"]["items"][0]
            await k.call("update_pantry", items=[{"name": "Hähnchenbrust", "action": "move", "location": "Tiefkühler"}])
            frozen = (await k.call("get_pantry", query="Hähnchenbrust"))["data"]["items"][0]
            assert frozen["location"] == "Tiefkühler" and frozen["days_left"] > fresh["days_left"] + 30
            await k.call("update_pantry", items=[{"name": "Joghurt", "qty": 500, "unit": "g", "location": "Kühlschrank"},
                                                 {"name": "Butter", "qty": 250, "unit": "g"}],
                         stocktake_location="Kühlschrank")
            fridge = {i["name"] for i in (await k.call("get_pantry", location="Kühlschrank"))["data"]["items"]}
            assert fridge == {"Joghurt", "Butter"}, fridge
    run(scenario())


def test_move_meal_swaps_days(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            draft = await k.call("plan_meals", days=2, meals=[{"recipe": PASTA}, {"recipe": CURRY}])
            await k.call("confirm_plan", proposal_id=draft["data"]["proposal_id"])
            moved = await k.call("skip_meal", recipe="Pasta", move_to="morgen")
            assert "getauscht" in moved["summary"]
            status = await k.call("get_kitchen_status")
            assert status["data"]["upcoming_meals"][0]["recipe"] == "Hähnchen-Curry"
    run(scenario())


def test_http_transport_requires_token(db):
    with http_server(db, token="s3cret") as base:
        with urllib.request.urlopen(f"{base}/healthz") as response:
            assert json.loads(response.read())["ok"] is True
        request = urllib.request.Request(f"{base}/mcp", data=b"{}", method="POST", headers={"content-type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        assert denied.value.code == 401

        async def scenario():
            async with http_kitchen(base, "s3cret") as k:
                status = await k.call("get_kitchen_status")
                assert status["data"]["today"] == MONDAY
        run(scenario())


def test_legacy_command_line_still_starts_the_server(db):
    """The tunnel profile runs `python3 offer_mcp_server.py --offers …` with the system Python."""
    request = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                       "clientInfo": {"name": "e2e", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]) + "\n"
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KITCHEN_", "VIRTUAL_ENV", "PYTHON"))}
    env.update({"KITCHEN_DB": str(db), "KITCHEN_NO_REFRESH": "1", "PATH": "/usr/bin:/bin:" + env.get("PATH", "")})
    proc = subprocess.Popen(["python3", str(ROOT / "offer_mcp_server.py"), "--offers", str(FIXTURE)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, cwd="/")
    try:
        proc.stdin.write(request)
        proc.stdin.flush()  # keep stdin open like a real client until the answer arrives
        reply = None
        for line in proc.stdout:
            message = json.loads(line)
            if message.get("id") == 2:
                reply = message
                break
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert reply is not None
    assert {t["name"] for t in reply["result"]["tools"]} == INTENT_TOOLS


def test_confirmed_list_matches_the_approved_proposal_after_budget_swaps(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            for recipe in (CURRY, LACHS, PASTA, OMELETT):
                await k.call("save_recipe", recipe=recipe)
            draft = await k.call("plan_meals", days=2, budget_eur=6)
            shown = {m["date"]: m["recipe"] for m in draft["data"]["meals"]}
            shown_total = draft["data"]["totals"]["food_cents"]
            await k.call("confirm_plan", proposal_id=draft["data"]["proposal_id"])
            status = await k.call("get_kitchen_status")
            planned = [m["recipe"] for m in status["data"]["upcoming_meals"]]
            assert planned == [r for r in shown.values() if r], (planned, shown)
            trips = (await k.call("get_shopping_list"))["data"]["trips"]
            listed = sum(round(float(t["estimated_total"].replace(" €", "").replace(",", ".")) * 100) for t in trips)
            assert abs(listed - shown_total) <= 2, (listed, shown_total)
    run(scenario())


# --- regressions from the code review -------------------------------------------------------------------------------

def test_reconfirming_a_revised_plan_keeps_cooked_skipped_and_moved_meals(db):
    async def monday():
        async with kitchen(db, MONDAY, hour=19) as k:
            await k.call("manage_household", stores_answered=True)
            draft = await k.call("plan_meals", start="heute", days=4,
                                 meals=[{"recipe": PASTA}, {"recipe": CURRY}, {"recipe": LACHS}, {"recipe": OMELETT}])
            await k.call("confirm_plan", proposal_id=draft["data"]["proposal_id"])
            await k.call("record_purchase", rest_of_list_bought=True)
            await k.call("record_cooking")  # Monday: Pasta
            await k.call("skip_meal", date="Mittwoch")  # Lachs skipped
            await k.call("skip_meal", recipe="Spinat-Omelett", move_to="Dienstag")  # swap Tue <-> Thu
            revised = await k.call("revise_plan", servings=2)
            notes = {m["date"]: (m["recipe"], m.get("note")) for m in revised["data"]["meals"]}
            assert notes["2026-07-27"] == ("Pasta mit Tomatensoße", "gekocht")
            assert notes["2026-07-29"][1] == "ausgelassen"
            assert notes["2026-07-28"][0] == "Spinat-Omelett" and notes["2026-07-30"][0] == "Hähnchen-Curry"
            await k.call("confirm_plan", proposal_id=revised["data"]["proposal_id"])
            upcoming = [(m["day"], m["recipe"]) for m in (await k.call("get_kitchen_status"))["data"]["upcoming_meals"]]
            assert [r for _, r in upcoming] == ["Spinat-Omelett", "Hähnchen-Curry"], upcoming
            on_list = items(await k.call("get_shopping_list"))
            assert "Spaghetti" not in on_list and "Lachs" not in on_list, on_list.keys()
    run(monday())


def test_overlapping_plan_keeps_the_earlier_plans_open_shopping(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            a = await k.call("plan_meals", days=4, shopping_days=["heute", "Donnerstag"],
                             meals=[{"recipe": CURRY}, {"recipe": PASTA}, {"recipe": LACHS}, {"recipe": OMELETT}])
            await k.call("confirm_plan", proposal_id=a["data"]["proposal_id"])
            b = await k.call("plan_meals", start="Donnerstag", days=2, meals=[{"recipe": CARBONARA}])
            await k.call("confirm_plan", proposal_id=b["data"]["proposal_id"])
            on_list = items(await k.call("get_shopping_list", date="heute"))
            assert {"Hähnchenbrust", "Spaghetti"} <= set(on_list), on_list.keys()
            upcoming = [m["recipe"] for m in (await k.call("get_kitchen_status"))["data"]["upcoming_meals"]]
            assert upcoming[:3] == ["Hähnchen-Curry", "Pasta mit Tomatensoße", "Ofenlachs mit Kartoffeln"], upcoming
    run(scenario())


def test_purchase_matches_spoken_names_against_the_list_and_books_lines_once(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores_answered=True)
            sandwich = {"name": "Käsebrot", "servings": 1, "ingredients": ["100 g Vollkornbrot", "50 g Gouda"]}
            draft = await k.call("plan_meals", days=1, meals=[{"recipe": sandwich}])
            await k.call("confirm_plan", proposal_id=draft["data"]["proposal_id"])
            await k.call("update_shopping_list", add=["1 kg Hähnchenbrust"], date="morgen")
            bought = await k.call("record_purchase", missing=["Käse"], bought=[{"name": "1 kg Hähnchenbrust", "price_eur": 10}])
            assert bought["data"]["missing"] == ["Gouda"]
            chicken = (await k.call("get_pantry", query="Hähnchenbrust"))["data"]["items"]
            assert [c["qty"] for c in chicken] == ["1 kg"], chicken
            spent = (await k.call("get_insights", topic="spending"))["data"]["spending"]["total"]
            assert spent.startswith("1") and float(spent.split()[0].replace(",", ".")) < 15, spent
            note = await k.call("record_purchase", missing=["Feta"], bought=["Milch"])
            assert any("Feta" in w for w in note["warnings"])
    run(scenario())


def test_household_edge_cases_do_not_crash(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", stores=[{"name": "Lidl"}, {"name": "Rewe"}])
            draft = await k.call("plan_meals", days=1, meals=[{"recipe": PASTA}])
            await k.call("confirm_plan", proposal_id=draft["data"]["proposal_id"])
            removed = await k.call("manage_household", stores=[{"name": "Lidl", "remove": True}])
            assert "Lidl" in removed["summary"]
            ambiguous = await k.fails("manage_household", stores=[{"name": "Aldi"}])
            assert ambiguous.code == "needs_clarification"
            added = await k.call("update_pantry", items=[{"name": "200 g Zzyzxpaste", "details": {"storage": "Kühlschrank"}}])
            assert "Kühlschrank" in added["summary"]
            assert (await k.call("get_pantry"))["data"]["items"]
            bad = await k.fails("update_pantry", items=[{"name": "Milch", "best_before": "31.2."}])
            assert bad.code == "validation_error"
            lunch = await k.call("plan_meals", days=3, meals=[{"recipe": PASTA, "date": "Dienstag", "meal_type": "Mittagessen"}])
            types = [(m["date"], m["meal_type"]) for m in lunch["data"]["meals"]]
            assert types.count(("2026-07-28", "lunch")) == 1 and sum(1 for _, t in types if t == "lunch") == 1, types
    run(scenario())


def test_plant_based_products_are_not_mistaken_for_dairy(db):
    async def scenario():
        async with kitchen(db) as k:
            await k.call("manage_household", restrictions=[{"kind": "allergy", "value": "Nüsse"}], stores_answered=True)
            saved = await k.call("save_recipe", recipe={"name": "Porridge", "servings": 1,
                                                        "ingredients": ["60 g Haferflocken", "250 ml Haselnussmilch"]})
            assert any("Haselnussmilch" in w for w in saved["warnings"]), saved["warnings"]
            fixed = await k.call("save_recipe", recipe={"name": "Porridge", "servings": 1, "ingredients": [
                "60 g Haferflocken", {"name": "250 ml Haselnussmilch", "details": {"allergens": ["Nüsse"], "animal": None}}]})
            assert any("Haushaltsregeln" in w for w in fixed["warnings"]), fixed["warnings"]
    run(scenario())
