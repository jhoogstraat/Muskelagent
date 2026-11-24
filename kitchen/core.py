"""Kitchen service base: commands, response envelope, household, dates, nutrition helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .catalog import Catalog, load_seed, norm, parse_restriction
from .db import Clock, KitchenError, connect, jdumps, jloads, migrate, new_id, revert_event, utc_now
from .offers import OfferService
from .units import eur

DEDUP_SECONDS = 120
WEEKDAYS = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]
WEEKDAYS_EN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MEAL_TYPES = {
    "breakfast": "breakfast", "frühstück": "breakfast", "fruehstueck": "breakfast", "morgens": "breakfast",
    "lunch": "lunch", "mittag": "lunch", "mittagessen": "lunch", "mittags": "lunch",
    "dinner": "dinner", "abendessen": "dinner", "abend": "dinner", "abends": "dinner",
    "snack": "snack", "zwischenmahlzeit": "snack",
}
MEAL_LABEL = {"breakfast": "Frühstück", "lunch": "Mittagessen", "dinner": "Abendessen", "snack": "Snack"}
MEAL_SHARE = {"breakfast": 0.25, "lunch": 0.35, "dinner": 0.35, "snack": 0.05}
NUTRIENTS = {"kcal": "kcal", "kalorien": "kcal", "energie": "kcal", "protein": "protein_g", "eiweiß": "protein_g",
             "eiweiss": "protein_g", "protein_g": "protein_g", "kohlenhydrate": "carbs_g", "carbs": "carbs_g",
             "carbs_g": "carbs_g", "fett": "fat_g", "fat": "fat_g", "fat_g": "fat_g"}


def envelope(summary: str, data: Any = None, *, next_actions: list | None = None, ask_user: list | None = None,
             warnings: list | None = None, display_hint: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"summary": summary}
    if data is not None:
        result["data"] = data
    result["next_actions"] = next_actions or []
    result["ask_user"] = ask_user or []
    result["warnings"] = [w for w in (warnings or []) if w]
    if display_hint:
        result["display_hint"] = display_hint
    return result


def action(tool: str, when: str, args: dict | None = None, consent: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {"tool": tool, "when": when}
    if args:
        item["args"] = args
    if consent:
        item["requires_user_consent"] = True
    return item


class KitchenBase:
    def __init__(self, db_path: Path, *, offers_file: Path | None = None, auto_refresh: bool = True) -> None:
        self.db_path = db_path
        self.conn = connect(db_path)
        migrate(self.conn)
        self.lock = threading.RLock()
        self.catalog = Catalog(self.conn)
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            load_seed(self.conn)
            self.conn.execute("COMMIT")
        self.clock = Clock(self.household()["timezone"])
        self.offers = OfferService(self.conn, self.catalog, db_path=db_path, offers_file=offers_file, auto_refresh=auto_refresh,
                                   tz=self.clock.tz)
        self.offers.startup()

    # --- running tools ---------------------------------------------------------------------
    def read(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            self.offers.ensure_fresh()
            return fn()

    def command(self, tool: str, args: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Atomic, logged, undoable. Identical repeats within two minutes return the first result."""
        with self.lock:
            digest = hashlib.sha256(f"{tool}|{jdumps(args)}".encode()).hexdigest()
            previous = self.conn.execute(
                "SELECT result FROM event WHERE args_hash = ? AND undone = 0 AND occurred_ts > ? ORDER BY occurred_ts DESC LIMIT 1",
                (digest, time.time() - DEDUP_SECONDS),
            ).fetchone()
            if previous and previous["result"]:
                result = jloads(previous["result"])
                result.setdefault("warnings", []).insert(0, "Gleicher Aufruf wurde gerade schon ausgeführt – nichts doppelt gebucht.")
                return result
            event_id = new_id("evt")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("UPDATE current_event SET event_id = ? WHERE k = 1", (event_id,))
                result = fn()
                self.conn.execute("UPDATE current_event SET event_id = NULL WHERE k = 1")
                self.conn.execute(
                    "INSERT INTO event (id, tool, args, args_hash, result, summary, occurred_at, occurred_ts) VALUES (?,?,?,?,?,?,?,?)",
                    (event_id, tool, jdumps(args), digest, jdumps(result), result.get("summary"), utc_now(), time.time()),
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return result

    def undo_last_action(self) -> dict[str, Any]:
        with self.lock:
            event = self.conn.execute(
                "SELECT e.* FROM event e WHERE e.undone = 0 AND EXISTS (SELECT 1 FROM change_log c WHERE c.event_id = e.id) "
                "ORDER BY e.occurred_ts DESC LIMIT 1"
            ).fetchone()
            if event is None:
                return envelope("Es gibt nichts, was ich rückgängig machen könnte.")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                revert_event(self.conn, event["id"])
                self.conn.execute("UPDATE event SET undone = 1 WHERE id = ?", (event["id"],))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return envelope(
                f"Rückgängig gemacht: {event['summary'] or event['tool']}",
                {"undone_tool": event["tool"], "undone_at": event["occurred_at"]},
                next_actions=[action("get_kitchen_status", "Um den aktuellen Stand zu zeigen")],
            )

    # --- dates --------------------------------------------------------------------------------
    def today(self) -> dt.date:
        return self.clock.today()

    def parse_day(self, text: str | None, ref: dt.date | None = None) -> dt.date | None:
        """ISO date, heute/morgen/übermorgen, or a weekday name (next occurrence from ref)."""
        if text is None or str(text).strip() == "":
            return None
        value = norm(str(text))
        if not value:
            raise KitchenError("validation_error", f"Datum „{text}“ verstehe ich nicht.")
        ref = ref or self.today()
        try:
            return dt.date.fromisoformat(str(text).strip()[:10])
        except ValueError:
            pass
        relative = {"heute": 0, "today": 0, "morgen": 1, "tomorrow": 1, "übermorgen": 2, "gestern": -1, "yesterday": -1}
        if value in relative:
            return self.today() + dt.timedelta(days=relative[value])
        word = value.split()[-1] if value.split()[0] in ("am", "on", "nächsten", "next") else value.split()[0]
        for names in (WEEKDAYS, WEEKDAYS_EN):
            for index, name in enumerate(names):
                if len(word) >= 2 and name.startswith(word):
                    return ref + dt.timedelta(days=(index - ref.weekday()) % 7)
        match = re.match(r"(\d{1,2})\.(\d{1,2})\.?(\d{2,4})?", str(text).strip())
        if match:
            year = int(match.group(3) or ref.year)
            year = year + 2000 if year < 100 else year
            try:
                day = dt.date(year, int(match.group(2)), int(match.group(1)))
            except ValueError as err:
                raise KitchenError("validation_error", f"Das Datum „{text}“ gibt es nicht.") from err
            if not match.group(3) and day < ref - dt.timedelta(days=120):
                day = day.replace(year=day.year + 1)  # "3.1." said in late December means next January
            return day
        raise KitchenError("validation_error", f"Datum „{text}“ verstehe ich nicht.",
                           {"examples": ["2026-11-25", "morgen", "Dienstag", "25.11."]})

    def day_label(self, day: dt.date | str) -> str:
        day = dt.date.fromisoformat(day) if isinstance(day, str) else day
        delta = (day - self.today()).days
        prefix = {0: "heute", 1: "morgen", -1: "gestern"}.get(delta)
        base = f"{WEEKDAY_SHORT[day.weekday()]} {day.day}.{day.month}."
        return f"{prefix} ({base})" if prefix else base

    @staticmethod
    def meal_type(value: str | None, default: str = "dinner") -> str:
        if not value:
            return default
        key = norm(value)
        if key not in MEAL_TYPES:
            raise KitchenError("validation_error", f"Mahlzeit „{value}“ unbekannt.", {"known": list(MEAL_LABEL.values())})
        return MEAL_TYPES[key]

    # --- household -----------------------------------------------------------------------------
    def household(self) -> dict[str, Any]:
        row = dict(self.conn.execute("SELECT * FROM household WHERE id = 1").fetchone())
        row["default_meal_types"] = jloads(row["default_meal_types"], ["dinner"])
        return row

    def members(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM member WHERE is_active = 1 ORDER BY created_at")]

    def restrictions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.*, m.name AS member_name FROM restriction r LEFT JOIN member m ON m.id = r.member_id ORDER BY r.created_at")
        return [dict(r) for r in rows]

    def servings_default(self) -> float:
        members = self.members()
        return round(sum(m["portion_factor"] for m in members), 2) if members else 1.0

    def preferences(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM preference ORDER BY kind, value")]

    def store_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM store ORDER BY name")]

    def allowed_stores(self, only: list[str] | None = None) -> tuple[dict[str, int], list[str]]:
        """Stores the optimizer may use -> visit cost, plus the preferred ones."""
        hh = self.household()
        rows = self.store_rows()
        chains = self.offers.chains()
        if only:
            names = [self.match_chain(n) for n in only]
            return {n: hh["visit_cost_cents"] for n in names}, names
        if rows:
            allowed = {r["name"]: (r["visit_cost_cents"] if r["visit_cost_cents"] is not None else hh["visit_cost_cents"])
                       + (100 if r["stance"] == "neutral" else 0)
                       for r in rows if r["stance"] != "avoid"}
            return allowed, [r["name"] for r in rows if r["stance"] == "preferred"]
        return {c: hh["visit_cost_cents"] for c in chains}, []

    def match_chain(self, name: str) -> str:
        key = norm(name)
        chains = self.offers.chains() + [r["name"] for r in self.store_rows()]
        for chain in chains:
            if norm(chain) == key:
                return chain
        prefixed = [c for c in dict.fromkeys(chains) if norm(c).startswith(key) or key.startswith(norm(c))]
        if len(prefixed) == 1:
            return prefixed[0]
        if len(prefixed) > 1:
            raise KitchenError("needs_clarification", f"Welcher Laden ist mit „{name}“ gemeint?", {"candidates": prefixed},
                               ask_user=[{"question": f"Welcher Laden ist mit „{name}“ gemeint?", "options": prefixed}])
        return " ".join(w[:1].upper() + w[1:] for w in name.strip().split())

    # --- nutrition -------------------------------------------------------------------------------
    @staticmethod
    def nutrients_for(ing: dict[str, Any], qty: float) -> dict[str, float] | None:
        if ing.get("kcal") is None:
            return None
        if ing["base_unit"] == "g":
            grams = qty
        elif ing["base_unit"] == "ml":
            grams = qty * (ing.get("density_g_per_ml") or 1.0)
        else:
            if not ing.get("g_per_piece"):
                return None
            grams = qty * ing["g_per_piece"]
        factor = grams / 100
        return {k: round((ing.get(k) or 0) * factor, 1) for k in ("kcal", "protein_g", "carbs_g", "fat_g")}

    # --- manage_household ---------------------------------------------------------------------
    def manage_household(self, args: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            changes: list[str] = []
            warnings: list[str] = []
            now = utc_now()
            hh = self.household()
            for m in args.get("members") or []:
                name = (m.get("name") or "").strip()
                if not name:
                    raise KitchenError("validation_error", "Mitglied braucht einen Namen.")
                existing = self.conn.execute("SELECT * FROM member WHERE name = ?", (name,)).fetchone()
                if m.get("remove"):
                    if existing:
                        self.conn.execute("UPDATE member SET is_active = 0, updated_at = ? WHERE id = ?", (now, existing["id"]))
                        changes.append(f"{name} entfernt")
                    continue
                factor = float(m.get("portion_factor") or (existing["portion_factor"] if existing else 1.0))
                if existing:
                    self.conn.execute("UPDATE member SET portion_factor = ?, is_active = 1, updated_at = ? WHERE id = ?",
                                      (factor, now, existing["id"]))
                    changes.append(f"{name}: Portionsfaktor {factor:g}")
                else:
                    self.conn.execute("INSERT INTO member (id, name, portion_factor, created_at, updated_at) VALUES (?,?,?,?,?)",
                                      (new_id("mem"), name, factor, now, now))
                    changes.append(f"{name} hinzugefügt")
            members = {m["name"].casefold(): m for m in self.members()}

            def member_id(name: str | None) -> str | None:
                if not name:
                    return None
                found = members.get(name.casefold())
                if not found:
                    raise KitchenError("not_found", f"Mitglied „{name}“ gibt es nicht.", {"members": [m["name"] for m in members.values()]})
                return found["id"]

            for r in args.get("restrictions") or []:
                kind, value = parse_restriction(r.get("kind") or "exclude", r.get("value") or "")
                mid = member_id(r.get("member"))
                if r.get("remove"):
                    removed = self.conn.execute("DELETE FROM restriction WHERE kind = ? AND value = ? AND member_id IS ?",
                                                (kind, value, mid)).rowcount
                    if removed:
                        changes.append(f"Regel „{r.get('value')}“ entfernt")
                    continue
                if not self.conn.execute("SELECT 1 FROM restriction WHERE kind = ? AND value = ? AND member_id IS ?",
                                         (kind, value, mid)).fetchone():
                    self.conn.execute("INSERT INTO restriction (id, member_id, kind, value, created_at) VALUES (?,?,?,?,?)",
                                      (new_id("rst"), mid, kind, value, now))
                    changes.append(f"Regel: {kind} {r.get('value')}")
            for t in args.get("nutrition_targets") or []:
                nutrient = NUTRIENTS.get(norm(t.get("nutrient") or ""))
                if not nutrient:
                    raise KitchenError("validation_error", f"Nährstoff „{t.get('nutrient')}“ unbekannt.", {"known": ["kcal", "protein", "kohlenhydrate", "fett"]})
                mid = member_id(t.get("member")) or (self.members()[0]["id"] if self.members() else None)
                if mid is None:
                    mid = new_id("mem")
                    self.conn.execute("INSERT INTO member (id, name, portion_factor, created_at, updated_at) VALUES (?,?,?,?,?)",
                                      (mid, "Ich", 1.0, now, now))
                    changes.append("Mitglied „Ich“ angelegt")
                self.conn.execute(
                    "INSERT INTO nutrition_target (id, member_id, nutrient, daily_min, daily_max) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(member_id, nutrient) DO UPDATE SET daily_min = excluded.daily_min, daily_max = excluded.daily_max",
                    (new_id("tgt"), mid, nutrient, t.get("daily_min"), t.get("daily_max")))
                changes.append(f"Ziel {nutrient}: {t.get('daily_min') or '–'}–{t.get('daily_max') or '–'} pro Tag")
            for s in args.get("stores") or []:
                name = self.match_chain(s.get("name") or "")
                stance = s.get("stance") or "preferred"
                if stance not in ("preferred", "neutral", "avoid"):
                    raise KitchenError("validation_error", "stance muss preferred, neutral oder avoid sein.")
                if s.get("remove"):
                    self.conn.execute("UPDATE shopping_trip SET store_id = NULL WHERE store_id IN (SELECT id FROM store WHERE name = ?)", (name,))
                    self.conn.execute("DELETE FROM store WHERE name = ?", (name,))
                    changes.append(f"Laden {name} entfernt")
                    continue
                cost = None if s.get("visit_cost_eur") is None else int(round(float(s["visit_cost_eur"]) * 100))
                self.conn.execute(
                    "INSERT INTO store (id, name, stance, visit_cost_cents, created_at, updated_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET stance = excluded.stance, visit_cost_cents = coalesce(excluded.visit_cost_cents, store.visit_cost_cents), updated_at = excluded.updated_at",
                    (new_id("sto"), name, stance, cost, now, now))
                if name not in self.offers.chains():
                    warnings.append(f"Für {name} habe ich keine Angebote; Preise dort sind Schätzungen.")
                changes.append(f"Laden {name}: {stance}")
            for p in args.get("preferences") or []:
                kind = p.get("kind") or "ingredient"
                value = norm(p.get("value") or "")
                if not value:
                    continue
                if p.get("remove"):
                    self.conn.execute("DELETE FROM preference WHERE kind = ? AND value = ?", (kind, value))
                    changes.append(f"Vorliebe „{value}“ entfernt")
                    continue
                weight = 1 if (p.get("like", True)) else -1
                self.conn.execute(
                    "INSERT INTO preference (id, kind, value, weight, created_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(kind, value) DO UPDATE SET weight = excluded.weight", (new_id("prf"), kind, value, weight, now))
                changes.append(f"{'mag' if weight > 0 else 'mag nicht'}: {value}")
            updates: dict[str, Any] = {}
            if args.get("weekly_budget_eur") is not None:
                updates["weekly_budget_cents"] = int(round(float(args["weekly_budget_eur"]) * 100)) or None
                changes.append(f"Wochenbudget {eur(updates['weekly_budget_cents'])}")
            if args.get("max_stores_per_trip") is not None:
                updates["max_stores_per_trip"] = max(1, int(args["max_stores_per_trip"]))
                changes.append(f"höchstens {updates['max_stores_per_trip']} Läden pro Einkauf")
            if args.get("default_meal_types"):
                updates["default_meal_types"] = jdumps([self.meal_type(m) for m in args["default_meal_types"]])
                changes.append("Standard-Mahlzeiten geändert")
            if args.get("store_visit_cost_eur") is not None:
                updates["visit_cost_cents"] = int(round(float(args["store_visit_cost_eur"]) * 100))
                changes.append("Besuchskosten geändert")
            if args.get("stores") is not None or args.get("stores_answered"):
                updates["stores_asked"] = 1
            if updates:
                updates["updated_at"] = now
                self.conn.execute(f"UPDATE household SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = 1",
                                  list(updates.values()))
            overview = self.household_overview()
            summary = ("Gespeichert: " + "; ".join(changes)) if changes else "Keine Änderung."
            return envelope(summary, overview, warnings=warnings,
                            next_actions=[action("plan_meals", "Wenn der Nutzer jetzt planen möchte")])
        if not any(args.get(k) is not None for k in args):
            return self.read(lambda: envelope("Haushaltsprofil", self.household_overview()))
        return self.command("manage_household", args, run)

    def household_overview(self) -> dict[str, Any]:
        hh = self.household()
        targets = [dict(r) for r in self.conn.execute(
            "SELECT m.name AS member, t.nutrient, t.daily_min, t.daily_max FROM nutrition_target t JOIN member m ON m.id = t.member_id")]
        return {
            "members": [{"name": m["name"], "portion_factor": m["portion_factor"]} for m in self.members()],
            "restrictions": [{"member": r["member_name"] or "alle", "kind": r["kind"], "value": r["value"]} for r in self.restrictions()],
            "nutrition_targets": targets,
            "stores": [{"name": s["name"], "stance": s["stance"]} for s in self.store_rows()],
            "preferences": [{"kind": p["kind"], "value": p["value"], "like": p["weight"] > 0} for p in self.preferences()],
            "weekly_budget": eur(hh["weekly_budget_cents"]) if hh["weekly_budget_cents"] else None,
            "max_stores_per_trip": hh["max_stores_per_trip"],
            "default_meal_types": [MEAL_LABEL[m] for m in hh["default_meal_types"]],
            "stores_with_offers": self.offers.chains(),
        }
