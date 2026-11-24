"""Meal planning: draft proposals, net requirements per shopping date, basket optimization, confirmation."""

from __future__ import annotations

import copy
import datetime as dt
from collections import defaultdict
from typing import Any

from .catalog import norm
from .core import MEAL_LABEL, MEAL_SHARE, action, envelope
from .db import KitchenError, jdumps, jloads, new_id, utc_now
from .offers import offer_pack_in_base
from .optimizer import ANY, Need, Option, optimize
from .recipes import RecipeMixin
from .units import eur, fmt_qty

MEAL_ORDER = {"breakfast": 0, "lunch": 1, "snack": 2, "dinner": 3}
SKIP_WORDS = {"none", "nichts", "auswärts", "auswaerts", "frei", "skip", "kein", "keins", "-"}
LEFTOVER_WORDS = {"reste", "rest", "leftovers", "leftover", "resteessen"}
AUTO_WORDS = {"auto", "automatisch", "egal", "vorschlag", "irgendwas"}


def _d(value: str | dt.date) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(value)


class PlanningMixin(RecipeMixin):
    # --- request handling -------------------------------------------------------------------
    def _slot_keys(self, req: dict[str, Any]) -> list[tuple[str, str, str]]:
        start = _d(req["start_on"])
        keys = []
        for offset in range(req["days"]):
            day = (start + dt.timedelta(days=offset)).isoformat()
            types = set(req["meal_types"]) | {k.split(":")[1] for k in req.get("extra_slots", []) if k.startswith(day)}
            for mt in sorted(types, key=lambda m: MEAL_ORDER[m]):
                keys.append((f"{day}:{mt}", day, mt))
        return keys

    def _apply_meals(self, req: dict[str, Any], meals: list[dict[str, Any]], warnings: list[str]) -> None:
        """Turn the agent's meal wishes into fixed slot assignments."""
        keys = self._slot_keys(req)
        start = _d(req["start_on"])
        undated = []
        for meal in meals or []:
            spec = self._meal_spec(meal, warnings)
            if meal.get("date"):
                day = self.parse_day(meal["date"], ref=start)
                mt = self.meal_type(meal.get("meal_type"), req["meal_types"][0])
                key = f"{day.isoformat()}:{mt}"
                if key not in {k for k, _, _ in keys}:
                    end = start + dt.timedelta(days=req["days"] - 1)
                    if day > end:
                        req["days"] = (day - start).days + 1
                    if mt not in req["meal_types"]:
                        req.setdefault("extra_slots", []).append(key)
                    if day < start:
                        raise KitchenError("validation_error", f"{self.day_label(day)} liegt vor dem Planstart.")
                req["assignments"][key] = spec
            else:
                undated.append((spec, meal.get("meal_type")))
        for spec, mt in undated:
            wanted = self.meal_type(mt, req["meal_types"][0]) if mt else None
            free = [k for k, _, m in self._slot_keys(req)
                    if (wanted is None or m == wanted) and (k not in req["assignments"] or req["assignments"][k].get("source") == "auto")]
            if not free:
                warnings.append(f"Kein freier Termin mehr für {spec.get('label')}.")
                continue
            req["assignments"][free[0]] = spec

    def _meal_spec(self, meal: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        recipe = meal.get("recipe")
        servings = meal.get("servings")
        if isinstance(recipe, dict):
            stored, more = self.store_recipe(recipe, source="agent")
            warnings += more
            return {"recipe_id": stored["id"], "label": stored["name"], "source": "requested", "servings": servings}
        text = norm(str(recipe or ""))
        if text in SKIP_WORDS:
            return {"skip": True, "label": "—", "source": "requested"}
        if text in AUTO_WORDS or not text:
            return {"source": "auto_request", "label": "auto", "servings": servings}
        if text in LEFTOVER_WORDS or text.startswith("reste"):
            label = str(recipe)
            lot = self._find_leftover(text.replace("reste", "").strip())
            if lot is None:
                raise KitchenError("not_found", "Keine passenden Reste im Vorrat.", next_actions=[action("get_pantry", "Reste prüfen")])
            return {"lot_id": lot["id"], "label": lot["label"], "source": "requested", "servings": servings}
        found = self.recipe_by_name(str(recipe))
        if found is None:
            raise KitchenError(
                "not_found", f"Rezept „{recipe}“ kenne ich noch nicht.", {"recipe": recipe},
                next_actions=[action("plan_meals", "Rezept als Objekt mit Zutaten übergeben: recipe={name, servings, ingredients:[\"200 g …\"]}")])
        return {"recipe_id": found["id"], "label": found["name"], "source": "requested", "servings": servings}

    def _find_leftover(self, hint: str) -> dict[str, Any] | None:
        lots = [l for l in self.lots() if l["kind"] == "prepared" and l["qty"] - l["reserved"] > 0]
        if hint:
            lots = [l for l in lots if hint in norm(l["label"] or "")] or lots
        lots.sort(key=lambda l: l["best_before_on"] or "9999")
        return lots[0] if lots else None

    def _new_request(self, args: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        hh = self.household()
        now = self.clock.now()
        start = self.parse_day(args.get("start")) or (now.date() if now.hour < 15 else now.date() + dt.timedelta(days=1))
        days = int(args.get("days") or 7)
        if not 1 <= days <= 14:
            raise KitchenError("validation_error", "Ein Plan umfasst 1 bis 14 Tage.")
        meal_types = [self.meal_type(m) for m in args.get("meal_types") or []] or list(hh["default_meal_types"])
        budget = args.get("budget_eur")
        if budget is None and hh["weekly_budget_cents"]:
            budget_cents = int(round(hh["weekly_budget_cents"] * days / 7))
        else:
            budget_cents = int(round(float(budget) * 100)) if budget is not None else None
        req = {
            "start_on": start.isoformat(), "days": days, "meal_types": list(dict.fromkeys(meal_types)),
            "servings": float(args["servings"]) if args.get("servings") else None, "budget_cents": budget_cents,
            "max_stores": int(args.get("max_stores") or hh["max_stores_per_trip"]),
            "shopping_days": [self.parse_day(d, ref=start).isoformat() for d in args.get("shopping_days") or []] or None,
            "stores_only": args.get("stores") or None, "assignments": {}, "avoid": {}, "pending": True,
        }
        self._apply_meals(req, args.get("meals") or [], warnings)
        return req

    # --- tools -------------------------------------------------------------------------------
    def plan_meals(self, args: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            warnings: list[str] = []
            req = self._new_request(args, warnings)
            plan_id = new_id("plan")
            proposal = self._compute(req, plan_id, 1, warnings)
            now = utc_now()
            self.conn.execute(
                "INSERT INTO meal_plan (id, start_on, end_on, status, revision, request, proposal, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (plan_id, proposal["period"]["start"], proposal["period"]["end"], "draft", 1, jdumps(req), jdumps(proposal), now, now))
            return self._proposal_envelope(proposal)
        return self.command("plan_meals", args, run)

    def revise_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            plan = self._plan_for_proposal(args.get("proposal_id"), allow_stale=True)
            warnings: list[str] = []
            req = jloads(plan["request"])
            if args.get("start") or args.get("days"):
                if args.get("start"):
                    req["start_on"] = self.parse_day(args["start"]).isoformat()
                if args.get("days"):
                    if not 1 <= int(args["days"]) <= 14:
                        raise KitchenError("validation_error", "Ein Plan umfasst 1 bis 14 Tage.")
                    req["days"] = int(args["days"])
                valid = {k for k, _, _ in self._slot_keys(req)}
                req["assignments"] = {k: v for k, v in req["assignments"].items() if k in valid}
            if args.get("meal_types"):
                req["meal_types"] = [self.meal_type(m) for m in args["meal_types"]]
            for key in ("max_stores",):
                if args.get(key) is not None:
                    req[key] = int(args[key])
            if args.get("budget_eur") is not None:
                req["budget_cents"] = int(round(float(args["budget_eur"]) * 100)) or None
            if args.get("servings") is not None:
                req["servings"] = float(args["servings"])
            if args.get("stores") is not None:
                req["stores_only"] = args["stores"] or None
            if args.get("shopping_days") is not None:
                req["shopping_days"] = [self.parse_day(d, ref=_d(req["start_on"])).isoformat() for d in args["shopping_days"]] or None
            for meal in args.get("replace_meals") or []:
                if meal.get("date") is None:
                    raise KitchenError("validation_error", "replace_meals braucht date (z. B. „Dienstag“).")
                day = self.parse_day(meal["date"], ref=_d(req["start_on"]))
                mt = self.meal_type(meal.get("meal_type"), req["meal_types"][0])
                key = f"{day.isoformat()}:{mt}"
                old = req["assignments"].get(key)
                if old and old.get("recipe_id") and norm(str(meal.get("recipe") or "")) in AUTO_WORDS | {""}:
                    req["avoid"].setdefault(key, []).append(old["recipe_id"])
                req["assignments"].pop(key, None)
                self._apply_meals(req, [meal | {"date": day.isoformat(), "meal_type": mt}], warnings)
            if args.get("meals"):
                self._apply_meals(req, args["meals"], warnings)
            req["pending"] = True
            revision = plan["revision"] + 1
            proposal = self._compute(req, plan["id"], revision, warnings)
            self.conn.execute("UPDATE meal_plan SET revision = ?, request = ?, proposal = ?, start_on = ?, end_on = ?, updated_at = ? WHERE id = ?",
                              (revision, jdumps(req), jdumps(proposal), proposal["period"]["start"], proposal["period"]["end"], utc_now(), plan["id"]))
            return self._proposal_envelope(proposal, revised=True)
        return self.command("revise_plan", args, run)

    def confirm_plan(self, proposal_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            plan = self._plan_for_proposal(proposal_id)
            req = jloads(plan["request"])
            if plan["status"] == "confirmed" and not req.get("pending"):
                return envelope("Der Plan ist bereits bestätigt.", {"plan_id": plan["id"]},
                                next_actions=[action("get_shopping_list", "Einkaufsliste zeigen")])
            today = self.today()
            replaced = self._release_future(plan["id"], today, own=True)
            others = [r["id"] for r in self.conn.execute(
                "SELECT id FROM meal_plan WHERE status = 'confirmed' AND id != ? AND end_on >= ? AND start_on <= ?",
                (plan["id"], plan["start_on"], plan["end_on"]))]
            for other in others:
                self._release_future(other, _d(plan["start_on"]))
            warnings: list[str] = []
            if others:
                warnings.append("Ein früher bestätigter Plan überschnitt sich; seine offenen Mahlzeiten ab Planstart wurden ersetzt.")
            proposal = self._compute(req, plan["id"], plan["revision"], warnings, keep_internal=True)
            internal = proposal.pop("_internal")
            blocked = [m for m in proposal["meals"] if m.get("violations")]
            if blocked:
                raise KitchenError("rule_violation", "Der Plan verstößt gegen Haushaltsregeln und wurde nicht bestätigt.",
                                   {"meals": [{"date": m["date"], "recipe": m["recipe"], "violations": m["violations"]} for m in blocked]},
                                   next_actions=[action("revise_plan", "Betroffene Mahlzeiten ersetzen", {"proposal_id": proposal_id})])
            self._materialize(plan["id"], proposal, internal, today)
            req["pending"] = False
            self.conn.execute("UPDATE meal_plan SET status = 'confirmed', request = ?, proposal = ?, confirmed_at = ?, updated_at = ? WHERE id = ?",
                              (jdumps(req), jdumps(proposal), utc_now(), utc_now(), plan["id"]))
            trips = proposal["shopping"]
            first = trips[0] if trips else None
            summary = f"Plan bestätigt: {len([m for m in proposal['meals'] if m.get('recipe')])} Mahlzeiten"
            if first:
                summary += f", Einkauf {self.day_label(first['date'])} bei {', '.join(t['store'] for t in trips if t['date'] == first['date'])}"
            if replaced:
                summary += " (Änderungen übernommen)"
            return envelope(summary + ".", {"plan_id": plan["id"], "period": proposal["period"], "shopping_trips": [
                {"date": t["date"], "store": t["store"], "items": len(t["items"]), "total": t["total"]} for t in trips]},
                warnings=warnings + proposal.get("warnings", []),
                next_actions=[action("get_shopping_list", "Wenn der Nutzer die Einkaufsliste sehen will"),
                              action("record_purchase", "Nach dem Einkauf: was gekauft wurde und was fehlte")])
        return self.command("confirm_plan", {"proposal_id": proposal_id}, run)

    def _plan_for_proposal(self, proposal_id: str | None, allow_stale: bool = False) -> dict[str, Any]:
        if not proposal_id:
            row = self.conn.execute("SELECT * FROM meal_plan WHERE status IN ('draft','confirmed') ORDER BY updated_at DESC LIMIT 1").fetchone()
            if row is None:
                raise KitchenError("not_found", "Es gibt keinen Plan.", next_actions=[action("plan_meals", "Neuen Plan erstellen")])
            return dict(row)
        plan_id, _, rev = proposal_id.partition(".r")
        row = self.conn.execute("SELECT * FROM meal_plan WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise KitchenError("not_found", "Diesen Vorschlag gibt es nicht.", {"proposal_id": proposal_id})
        plan = dict(row)
        if plan["status"] in ("cancelled", "completed"):
            raise KitchenError("invalid_transition", f"Der Plan ist bereits {plan['status']}.")
        if not allow_stale and rev and int(rev) != plan["revision"]:
            raise KitchenError("stale_proposal", "Dieser Vorschlag ist veraltet; es gibt eine neuere Fassung.",
                               {"current_proposal_id": f"{plan['id']}.r{plan['revision']}"},
                               next_actions=[action("confirm_plan", "Aktuelle Fassung bestätigen, wenn der Nutzer sie gesehen hat",
                                                    {"proposal_id": f"{plan['id']}.r{plan['revision']}"}, consent=True)])
        return plan

    def _release_future(self, plan_id: str, from_day: dt.date, own: bool = False) -> bool:
        """Cancel a confirmed plan's planned meals from `from_day` on, free their reservations and drop shopping items
        that no remaining meal needs. Cooked, skipped and earlier meals and their shopping stay untouched."""
        slots = [r["id"] for r in self.conn.execute(
            "SELECT id FROM meal_slot WHERE plan_id = ? AND status = 'planned' AND date_on >= ?", (plan_id, from_day.isoformat()))]
        for slot in slots:
            self.conn.execute("UPDATE reservation SET status = 'released' WHERE slot_id = ? AND status = 'active'", (slot,))
            self.conn.execute("UPDATE meal_slot SET status = 'cancelled', updated_at = ? WHERE id = ?", (utc_now(), slot))
        remaining = self.conn.execute("SELECT count(*) FROM meal_slot WHERE plan_id = ? AND status = 'planned'", (plan_id,)).fetchone()[0]
        changed = bool(slots)
        for trip in [r["id"] for r in self.conn.execute("SELECT id FROM shopping_trip WHERE plan_id = ? AND status = 'planned'", (plan_id,))]:
            for item in self.conn.execute("SELECT * FROM shopping_item WHERE trip_id = ? AND status = 'open' AND is_manual = 0", (trip,)).fetchall():
                needed = self.conn.execute("SELECT count(*) FROM reservation WHERE shopping_item_id = ? AND status = 'active'",
                                           (item["id"],)).fetchone()[0]
                if needed or (item["check_at_home"] and remaining and not own):
                    continue
                self.conn.execute("UPDATE shopping_item SET status = 'removed' WHERE id = ?", (item["id"],))
                changed = True
            if not self.conn.execute("SELECT count(*) FROM shopping_item WHERE trip_id = ? AND status = 'open'", (trip,)).fetchone()[0]:
                self.conn.execute("UPDATE shopping_trip SET status = 'cancelled', updated_at = ? WHERE id = ?", (utc_now(), trip))
        status = self.conn.execute("SELECT status FROM meal_plan WHERE id = ?", (plan_id,)).fetchone()[0]
        if status == "confirmed" and not remaining and slots and not own:
            cooked = self.conn.execute("SELECT count(*) FROM meal_slot WHERE plan_id = ? AND status = 'cooked'", (plan_id,)).fetchone()[0]
            self.conn.execute("UPDATE meal_plan SET status = ?, updated_at = ? WHERE id = ?",
                              ("completed" if cooked else "cancelled", utc_now(), plan_id))
        return changed

    # --- computation -------------------------------------------------------------------------
    def _compute(self, req: dict[str, Any], plan_id: str, revision: int, warnings: list[str],
                 keep_internal: bool = False) -> dict[str, Any]:
        """Compute a proposal. Over budget, swap the priciest auto-chosen meal (twice at most) and keep the cheapest
        attempt. The request is updated in place to exactly the kept attempt, so confirming reproduces what was shown."""
        best: dict[str, Any] | None = None
        best_req: dict[str, Any] | None = None
        working = copy.deepcopy(req)
        attempts = 1 if keep_internal else 3  # confirm must reproduce the approved proposal, never swap meals
        for _ in range(attempts):
            attempt_req = copy.deepcopy(working)
            result = self._compute_once(attempt_req, plan_id, revision, list(warnings))
            if best is None or result["totals"]["food_cents"] < best["totals"]["food_cents"]:
                best, best_req = result, attempt_req
            budget = attempt_req.get("budget_cents")
            if not budget or result["totals"]["food_cents"] <= budget:
                break
            auto = [m for m in result["meals"] if m.get("source") == "auto" and m.get("recipe_id")]
            if not auto:
                break
            worst = max(auto, key=lambda m: (m.get("est_cost_cents") or 0, m["date"]))
            key = f"{worst['date']}:{worst['meal_type']}"
            working = copy.deepcopy(attempt_req)  # never mutate the attempt kept as best
            working.setdefault("avoid", {}).setdefault(key, []).append(worst["recipe_id"])
            working["assignments"].pop(key, None)
        assert best is not None and best_req is not None
        req.clear()
        req.update(best_req)
        if best["totals"].get("over_budget_cents"):
            best["ask_user"].append({"question": f"Der Plan liegt {eur(best['totals']['over_budget_cents'])} über dem Budget. "
                                                 "Günstigere Gerichte wählen, Budget anpassen oder so lassen?",
                                     "options": ["günstiger", "Budget anpassen", "so lassen"]})
        if not keep_internal:
            best.pop("_internal", None)
        return best

    def _compute_once(self, req: dict[str, Any], plan_id: str, revision: int, warnings: list[str]) -> dict[str, Any]:
        today = self.today()
        restrictions = self.restrictions()
        servings_default = req.get("servings") or self.servings_default()
        done = {f"{r['date_on']}:{r['meal_type']}": r for r in self.conn.execute(
            "SELECT s.date_on, s.meal_type, s.status, coalesce(rc.name, l.label, 'Mahlzeit') AS label FROM meal_slot s "
            "LEFT JOIN recipe rc ON rc.id = s.recipe_id LEFT JOIN lot l ON l.id = s.lot_id "
            "WHERE s.plan_id = ? AND s.status IN ('cooked','skipped')", (plan_id,))}
        slots = []
        for key, day, mt in self._slot_keys(req):
            spec = req["assignments"].get(key)
            slot = {"key": key, "date": day, "meal_type": mt, "servings": servings_default, "recipe_id": None, "lot_id": None,
                    "source": None, "reason": None}
            if key in done:
                slot.update(source="done", reason=done[key]["status"], label=done[key]["label"])
            elif spec:
                if spec.get("skip"):
                    slot["source"] = "skip"
                elif spec.get("lot_id"):
                    slot.update(lot_id=spec["lot_id"], source="leftover", reason="Reste verwerten")
                elif spec.get("recipe_id"):
                    slot.update(recipe_id=spec["recipe_id"], source=spec.get("source") or "requested", reason=spec.get("reason"))
                if spec.get("servings"):
                    slot["servings"] = float(spec["servings"])
            slots.append(slot)
        self._auto_fill(req, slots, restrictions, today)

        # Shopping dates: requested ones, else the first meal day plus extra trips where fresh food would not last.
        first_day = slots[0]["date"] if slots else req["start_on"]
        trip_dates = sorted(set(req.get("shopping_days") or [first_day]))
        recipes = {s["recipe_id"]: self.recipe(s["recipe_id"]) for s in slots if s["recipe_id"]}
        exclude_plan = self.conn.execute("SELECT id FROM meal_plan WHERE id = ?", (plan_id,)).fetchone()
        pool = self._pool(plan_id if exclude_plan else None)
        if not req.get("shopping_days"):
            for _ in range(2):
                sim = self._simulate(slots, recipes, pool, trip_dates)
                late = sorted({p["date"] for p in sim["freshness"]})
                if not late or len(trip_dates) >= 3:
                    break
                trip_dates = sorted(set(trip_dates) | {late[0]})
        stores, preferred = self.allowed_stores(req.get("stores_only"))
        virtual: list[dict[str, Any]] = []
        trip_results = []
        for index, trip_day in enumerate(trip_dates):
            sim = self._simulate(slots, recipes, pool + virtual, trip_dates)
            needs = sim["trip_needs"].get(index, {})
            staples = sim["staples_missing"] if index == 0 else set()
            result = self._optimize_trip(_d(trip_day), needs, staples, stores, preferred, req["max_stores"])
            trip_results.append(result)
            for choice in result["choices"]:
                if choice["check_at_home"]:
                    continue
                ing = self.catalog.get(choice["ingredient_id"])
                virtual.append({"id": f"trip{index}:{ing['id']}", "ing": ing["id"], "qty": choice["purchase_qty"],
                                "from": _d(trip_day), "bb": self.estimated_best_before(ing, ing.get("storage") or "pantry", _d(trip_day)),
                                "kind": "raw"})
        final = self._simulate(slots, recipes, pool + virtual, trip_dates)
        return self._proposal(req, plan_id, revision, slots, recipes, trip_dates, trip_results, final, warnings, restrictions)

    def _auto_fill(self, req: dict[str, Any], slots: list[dict[str, Any]], restrictions: list[dict[str, Any]], today: dt.date) -> None:
        open_slots = [s for s in slots if s["source"] is None or s["source"] == "auto_request"]
        if not open_slots:
            return
        # Leftovers first: they are already cooked.
        leftovers = [l for l in self.lots() if l["kind"] == "prepared" and l["qty"] - l["reserved"] > 0]
        used_lots = {s["lot_id"] for s in slots if s["lot_id"]}
        for lot in sorted(leftovers, key=lambda l: l["best_before_on"] or "9999"):
            if lot["id"] in used_lots:
                continue
            free = lot["qty"] - lot["reserved"]
            for slot in open_slots:
                if slot["source"] in (None, "auto_request") and slot["meal_type"] in ("lunch", "dinner") \
                        and free + 1e-6 >= slot["servings"] and (not lot["best_before_on"] or slot["date"] <= lot["best_before_on"]):
                    slot.update(lot_id=lot["id"], source="auto", reason=f"Reste: {lot['label']}")
                    req["assignments"][slot["key"]] = {"lot_id": lot["id"], "label": lot["label"], "source": "auto",
                                                       "reason": slot["reason"]}
                    free -= slot["servings"]
        candidates = []
        for row in self.conn.execute("SELECT id FROM recipe ORDER BY name"):
            recipe = self.recipe(row["id"])
            if not self.recipe_violations(recipe, restrictions):
                candidates.append(recipe)
        if not candidates:
            return
        stock = self.stock_map(today)
        on_offer = {o["ingredient_id"] for o in self.offers.active_offers(on=_d(req["start_on"])) if o["ingredient_id"]}
        recent = {r["recipe_id"]: r["cooked_on"] for r in self.conn.execute(
            "SELECT recipe_id, max(cooked_on) AS cooked_on FROM cook_log WHERE recipe_id IS NOT NULL GROUP BY recipe_id")}
        prefs = self.preferences()
        likes = {p["value"] for p in prefs if p["weight"] > 0}
        dislikes = {p["value"] for p in prefs if p["weight"] < 0}
        protein_goal = self.conn.execute("SELECT 1 FROM nutrition_target WHERE nutrient = 'protein_g' AND daily_min IS NOT NULL").fetchone()
        in_plan = [s["recipe_id"] for s in slots if s["recipe_id"]]
        for s in slots:
            if s["lot_id"]:
                row = self.conn.execute("SELECT recipe_id FROM lot WHERE id = ?", (s["lot_id"],)).fetchone()
                if row and row["recipe_id"]:
                    in_plan.append(row["recipe_id"])
        for slot in open_slots:
            if slot["source"] not in (None, "auto_request"):
                continue
            day = _d(slot["date"])
            avoid = set(req.get("avoid", {}).get(slot["key"], []))
            best = None
            for recipe in candidates:
                if recipe["id"] in avoid or (recipe["id"] in in_plan and not recipe["is_meal_prep"]):
                    continue
                score, reasons = 0.0, []
                names = [norm(i["ingredient"]["name"]) for i in recipe["ingredients"]]
                tags = set(recipe["tags"])
                breakfast_like = bool(tags & {"frühstück", "breakfast", "fruehstueck"})
                if (slot["meal_type"] == "breakfast") != breakfast_like:
                    score -= 1500
                for item in recipe["ingredients"]:
                    s = stock.get(item["ingredient"]["id"])
                    if s and s["qty"] > 0:
                        score += 150
                        if s["bb"] and (s["bb"] - day).days <= 3 and s["bb"] >= day:
                            score += 300
                            reasons.append(f"verwertet {item['ingredient']['name']}")
                    if item["ingredient"]["id"] in on_offer:
                        score += 100
                        reasons.append(f"{item['ingredient']['name']} im Angebot")
                if recipe["rating"]:
                    score += 60 * (recipe["rating"] - 3)
                last = recent.get(recipe["id"])
                if last and (day - _d(last)).days <= 2:
                    continue
                if last and (day - _d(last)).days < 7:
                    score -= 800
                score -= 2000 * in_plan.count(recipe["id"])
                if any(any(d in n or n.endswith(d) for n in names) for d in dislikes) or tags & dislikes:
                    score -= 600
                if any(any(l in n for n in names) for l in likes) or tags & likes:
                    score += 100
                if protein_goal:
                    score += 3 * self.recipe_summary(recipe)["per_serving"]["protein_g"]
                key = (score, recipe["name"])
                if best is None or (key[0] > best[0][0]) or (key[0] == best[0][0] and key[1] < best[0][1]):
                    best = (key, recipe, reasons)
            if best is None:
                continue
            _, recipe, reasons = best
            slot.update(recipe_id=recipe["id"], source="auto", reason=", ".join(dict.fromkeys(reasons)) or None)
            in_plan.append(recipe["id"])
            req["assignments"][slot["key"]] = {"recipe_id": recipe["id"], "label": recipe["name"], "source": "auto",
                                               "reason": slot["reason"], "servings": None}

    def _pool(self, exclude_plan: str | None) -> list[dict[str, Any]]:
        """Usable stock: lots minus reservations held by other plans."""
        pool = []
        for lot in self.conn.execute("SELECT * FROM lot WHERE status IN ('sealed','opened') AND qty > 0"):
            lot = dict(lot)
            reserved = self.conn.execute(
                "SELECT coalesce(sum(r.qty), 0) FROM reservation r JOIN meal_slot s ON s.id = r.slot_id "
                "WHERE r.lot_id = ? AND r.status = 'active' AND (s.plan_id IS NOT ? OR ? IS NULL) AND s.date_on >= ?",
                (lot["id"], exclude_plan, exclude_plan, (self.today() - dt.timedelta(days=1)).isoformat())).fetchone()[0]
            free = lot["qty"] - reserved
            if free <= 1e-9:
                continue
            ing = self.catalog.get(lot["ingredient_id"]) if lot["ingredient_id"] else None
            pool.append({"id": lot["id"], "ing": lot["ingredient_id"], "qty": free, "from": dt.date.min,
                         "bb": self.effective_bb(lot, ing), "kind": lot["kind"]})
        return pool

    def _shelf_days(self, ing: dict[str, Any]) -> int:
        return int(ing.get("shelf_life_days") or 7)

    def _simulate(self, slots: list[dict[str, Any]], recipes: dict[str, dict[str, Any]], pool: list[dict[str, Any]],
                  trip_dates: list[str]) -> dict[str, Any]:
        pool = [dict(p) for p in pool]
        stock_staples = {l["ingredient_id"] for l in self.lots() if l["staple_level"] in ("full", "low")}
        allocations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        trip_needs: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        staples_missing: set[str] = set()
        freshness, early = [], []
        trips = [_d(t) for t in trip_dates]
        for slot in sorted(slots, key=lambda s: (s["date"], MEAL_ORDER[s["meal_type"]])):
            day = _d(slot["date"])
            if slot["lot_id"]:
                for p in pool:
                    if p["id"] == slot["lot_id"]:
                        take = min(p["qty"], slot["servings"])
                        p["qty"] -= take
                        allocations[slot["key"]].append({"ing": None, "source": p["id"], "qty": take})
                continue
            recipe = recipes.get(slot["recipe_id"]) if slot["recipe_id"] else None
            if recipe is None:
                continue
            for item in recipe["ingredients"]:
                ing = item["ingredient"]
                if item["is_optional"]:
                    continue
                if ing["is_staple"]:
                    if ing["id"] not in stock_staples:
                        staples_missing.add(ing["id"])
                    continue
                need = item["qty_per_serving"] * slot["servings"]
                if need <= 1e-9:
                    continue
                usable = sorted((p for p in pool if p["ing"] == ing["id"] and p["qty"] > 1e-9 and p["from"] <= day
                                 and (p["bb"] is None or p["bb"] >= day)),
                                key=lambda p: (p["bb"] or dt.date.max, p["id"]))
                for p in usable:
                    if need <= 1e-9:
                        break
                    take = min(p["qty"], need)
                    p["qty"] -= take
                    need -= take
                    alloc = {"ing": ing["id"], "source": p["id"], "qty": take}
                    if p["id"].startswith("trip"):
                        alloc["trip"] = int(p["id"][4:].split(":")[0])
                    allocations[slot["key"]].append(alloc)
                if need <= 1e-6:
                    continue
                before = [i for i, t in enumerate(trips) if t <= day]
                if not before:
                    early.append({"date": slot["date"], "ingredient": ing["name"]})
                    before = [0]
                shelf = self._shelf_days(ing)
                fresh = [i for i in before if (day - trips[i]).days <= shelf]
                index = max(fresh) if fresh else max(before)
                if not fresh:
                    freshness.append({"date": slot["date"], "ingredient": ing["name"], "ingredient_id": ing["id"]})
                trip_needs[index][ing["id"]] += need
                allocations[slot["key"]].append({"ing": ing["id"], "source": f"trip{index}:{ing['id']}", "qty": need, "trip": index})
        return {"allocations": allocations, "trip_needs": trip_needs, "staples_missing": staples_missing,
                "freshness": freshness, "early": early}

    def _options(self, ing: dict[str, Any], day: dt.date, stores: dict[str, int]) -> list[Option]:
        options: list[Option] = []
        for offer in self.offers.for_ingredients({ing["id"]}, day).get(ing["id"], []):
            if offer["store"] not in stores or not offer["price_cents"]:
                continue
            pack = offer_pack_in_base(offer, ing)
            if pack:
                options.append(Option(offer["store"], offer["price_cents"], pack, "offer", "high", offer["name"], offer["id"]))
        for row in self.conn.execute(
                "SELECT store_name, qty, price_cents FROM price_observation WHERE ingredient_id = ? AND store_name IS NOT NULL "
                "ORDER BY observed_on DESC", (ing["id"],)):
            if row["store_name"] in stores and row["qty"] > 0 and not any(o.store == row["store_name"] and o.source == "history" for o in options):
                per_unit = row["price_cents"] / row["qty"]
                pack = ing.get("pack_qty")
                options.append(Option(row["store_name"], per_unit * pack if pack else per_unit, pack, "history", "medium",
                                      f"{ing['name']} (zuletzt dort bezahlt)"))
        if ing.get("price_cents"):
            per_unit = ing["price_cents"] / (1 if ing["base_unit"] == "pc" else 1000)
            pack = ing.get("pack_qty")
            options.append(Option(ANY, per_unit * pack if pack else per_unit, pack, "estimate", "low", ing["name"]))
            if ing.get("section") == "Obst & Gemüse" and (pack or ing.get("g_per_piece")):
                # Produce can also be bought loose, a little dearer than the bag; by the piece where it is counted.
                piece = ing.get("g_per_piece") if ing["base_unit"] == "g" else None
                options.append(Option(ANY, per_unit * 1.15 * piece if piece else per_unit * 1.15, piece, "estimate", "low",
                                      f"{ing['name']} (lose)"))
        return options

    def _optimize_trip(self, day: dt.date, needs: dict[str, float], staples: set[str], stores: dict[str, int],
                       preferred: list[str], max_stores: int) -> dict[str, Any]:
        items: list[Need] = []
        for ing_id, qty in sorted(needs.items()):
            ing = self.catalog.get(ing_id)
            shelf = self._shelf_days(ing)
            risk = 0.02 if shelf >= 60 else 0.7 if shelf <= 5 else 0.35 if shelf <= 14 else 0.1
            if ing.get("freezer_days"):
                risk *= 0.5
            unit_value = (ing.get("price_cents") or 0) / (1 if ing["base_unit"] == "pc" else 1000)
            items.append(Need(ing_id, ing["name"], qty, ing["base_unit"], risk, unit_value, self._options(ing, day, stores)))
        result = optimize(items, stores, max_stores=max_stores, preferred=preferred) if items else \
            {"stores": [], "choices": [], "total_cents": 0, "visit_cents": 0, "unpriced": [], "alternatives": [], "input_hash": None}
        choices = []
        for choice in result["choices"]:
            ing = self.catalog.get(choice.key)
            need = next(n for n in items if n.key == choice.key)
            packs = [{"label": o.label, "count": c if o.pack_qty else None, "pack_qty": o.pack_qty, "source": o.source,
                      "offer_id": o.offer_id, "price_cents": o.price_cents} for o, c in choice.packs]
            source = choice.packs[0][0].source if choice.packs else "none"
            confidence = choice.packs[0][0].confidence if choice.packs else "none"
            choices.append({
                "ingredient_id": ing["id"], "name": ing["name"], "section": ing.get("section") or "Drogerie & Sonstiges",
                "store": choice.store, "need_qty": need.need_qty, "purchase_qty": choice.purchase_qty,
                "surplus_qty": max(0.0, choice.purchase_qty - need.need_qty), "base_unit": ing["base_unit"],
                "pack_count": sum(p["count"] or 0 for p in packs) or None,
                "pack_qty": packs[0]["pack_qty"] if len(packs) == 1 else None, "price_cents": round(choice.price_cents) if not choice.unpriced else None,
                "price_source": source, "price_confidence": confidence, "packs": packs, "check_at_home": False,
                "offer": next(({"name": p["label"], "offer_id": p["offer_id"], "price": eur(p["price_cents"])} for p in packs if p["source"] == "offer"), None),
            })
        for ing_id in sorted(staples):
            ing = self.catalog.get(ing_id)
            price = None
            if ing.get("price_cents") and ing.get("pack_qty"):
                price = round(ing["price_cents"] / (1 if ing["base_unit"] == "pc" else 1000) * ing["pack_qty"])
            main = result["stores"][0] if result["stores"] else (preferred[0] if preferred else ANY)
            choices.append({"ingredient_id": ing_id, "name": ing["name"], "section": ing.get("section") or "Gewürze & Öle",
                            "store": main, "need_qty": 0, "purchase_qty": ing.get("pack_qty") or 1, "surplus_qty": 0,
                            "base_unit": ing["base_unit"], "pack_count": 1, "pack_qty": ing.get("pack_qty"), "price_cents": price,
                            "price_source": "estimate", "price_confidence": "low", "packs": [], "check_at_home": True, "offer": None})
        return {"date": day.isoformat(), "stores": result["stores"], "choices": choices,
                "total_cents": sum(c["price_cents"] or 0 for c in choices if not c["check_at_home"]),
                "visit_cents": result["visit_cents"], "unpriced": [self.catalog.get(k)["name"] for k in result["unpriced"]],
                "alternatives": result.get("alternatives", []), "input_hash": result.get("input_hash")}

    def _proposal(self, req, plan_id, revision, slots, recipes, trip_dates, trip_results, final, warnings, restrictions) -> dict[str, Any]:
        unit_cost: dict[tuple[int, str], float] = {}
        for index, trip in enumerate(trip_results):
            for c in trip["choices"]:
                if c["price_cents"] and c["purchase_qty"]:
                    unit_cost[(index, c["ingredient_id"])] = c["price_cents"] / c["purchase_qty"]
        meals = []
        open_slots = []
        nutrition_by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for slot in slots:
            entry = {"date": slot["date"], "day": self.day_label(slot["date"]), "meal_type": slot["meal_type"],
                     "meal": MEAL_LABEL[slot["meal_type"]], "servings": slot["servings"], "source": slot["source"]}
            if slot["source"] == "skip":
                entry["recipe"] = None
                entry["note"] = "nichts geplant"
                meals.append(entry)
                continue
            if slot["source"] == "done":
                entry.update(recipe=slot["label"], note="gekocht" if slot["reason"] == "cooked" else "ausgelassen")
                meals.append(entry)
                continue
            if slot["lot_id"]:
                lot = self.conn.execute("SELECT label FROM lot WHERE id = ?", (slot["lot_id"],)).fetchone()
                entry.update(recipe=lot["label"] if lot else "Reste", leftover=True, reason=slot["reason"])
                meals.append(entry)
                continue
            if not slot["recipe_id"]:
                entry["recipe"] = None
                open_slots.append({"date": slot["date"], "meal_type": slot["meal_type"]})
                meals.append(entry)
                continue
            recipe = recipes[slot["recipe_id"]]
            summary = self.recipe_summary(recipe, slot["servings"])
            cost = sum(a["qty"] * unit_cost.get((a.get("trip", -1), a["ing"]), 0) for a in final["allocations"][slot["key"]] if a.get("trip") is not None)
            from_pantry = [a for a in final["allocations"][slot["key"]] if a.get("trip") is None and a["ing"]]
            entry.update(recipe=recipe["name"], recipe_id=recipe["id"], reason=slot["reason"], time_min=summary["time_min"],
                         per_serving=summary["per_serving"], est_cost_cents=round(cost),
                         uses_pantry=sorted({self.catalog.get(a["ing"])["name"] for a in from_pantry}))
            reasons = self.recipe_violations(recipe, restrictions)
            if reasons:
                entry["violations"] = reasons
            unknown = self.recipe_unknowns(recipe, restrictions)
            if unknown:
                entry["unchecked_ingredients"] = unknown
            for k, v in summary["per_serving"].items():
                nutrition_by_day[slot["date"]][k] += v
            meals.append(entry)

        shopping = []
        for index, trip in enumerate(trip_results):
            by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for c in trip["choices"]:
                by_store[c["store"]].append(c)
            for store, items in sorted(by_store.items()):
                label = store or "Supermarkt deiner Wahl"
                shopping.append({
                    "trip_index": index, "date": trip["date"], "day": self.day_label(trip["date"]), "store": label,
                    "total": eur(sum(i["price_cents"] or 0 for i in items if not i["check_at_home"])),
                    "total_note": "ohne Posten 'zu Hause prüfen'" if any(i["check_at_home"] for i in items) else None,
                    "total_cents": sum(i["price_cents"] or 0 for i in items if not i["check_at_home"]),
                    "items": [self._item_view(i) for i in sorted(items, key=lambda i: (i["check_at_home"], i["section"], i["name"]))],
                })
        food = sum(t["total_cents"] for t in trip_results)
        visits = sum(t["visit_cents"] for t in trip_results)
        budget = req.get("budget_cents")
        stores_used = sorted({s["store"] for s in shopping})
        totals = {"food_cents": food, "food": eur(food), "stores": stores_used, "shopping_trips": len(shopping),
                  "unpriced": sorted({u for t in trip_results for u in t["unpriced"]}),
                  "budget": eur(budget) if budget else None,
                  "over_budget_cents": max(0, food - budget) if budget else 0,
                  "estimated_share": self._estimate_share(trip_results)}
        alternatives = []
        for index, trip in enumerate(trip_results):
            for alt in trip["alternatives"]:
                alternatives.append({"date": trip["date"], "stores": alt["stores"],
                                     "food_difference": ("+" if alt["delta_cents"] >= 0 else "−") + eur(abs(alt["delta_cents"]))})
        ask_user: list[dict[str, Any]] = []
        hh = self.household()
        if not self.store_rows() and not hh["stores_asked"]:
            chains = self.offers.chains()
            ask_user.append({"question": "In welchen Supermärkten kaufst du normalerweise ein? Dann plane ich nur mit diesen.",
                             "options": chains[:8], "save_with": "manage_household(stores=[...])"})
        for item in final["early"]:
            warnings.append(f"{item['ingredient']} für {self.day_label(item['date'])} wird vor dem ersten Einkauf gebraucht und fehlt.")
        for item in final["freshness"]:
            warnings.append(f"{item['ingredient']} hält vermutlich nicht bis {self.day_label(item['date'])} – einfrieren oder später kaufen.")
        nutrition = self._nutrition_check(nutrition_by_day, req["meal_types"])
        warnings += nutrition.pop("warnings")
        proposal = {
            "proposal_id": f"{plan_id}.r{revision}", "plan_id": plan_id, "revision": revision,
            "period": {"start": req["start_on"], "end": (_d(req["start_on"]) + dt.timedelta(days=req["days"] - 1)).isoformat()},
            "meals": meals, "open_slots": open_slots, "shopping": shopping, "totals": totals,
            "alternatives": alternatives, "nutrition": nutrition, "warnings": list(dict.fromkeys(warnings)), "ask_user": ask_user,
            "_internal": {"slots": slots, "allocations": {k: v for k, v in final["allocations"].items()}, "trip_results": trip_results,
                          "trip_dates": trip_dates},
        }
        return proposal

    def _estimate_share(self, trip_results: list[dict[str, Any]]) -> str:
        total = sum(c["price_cents"] or 0 for t in trip_results for c in t["choices"] if not c["check_at_home"])
        estimated = sum(c["price_cents"] or 0 for t in trip_results for c in t["choices"]
                        if not c["check_at_home"] and c["price_source"] == "estimate")
        if not total:
            return "keine Preise"
        return f"{round(100 * estimated / total)} % Schätzpreise"

    def _item_view(self, c: dict[str, Any]) -> dict[str, Any]:
        unit = c["base_unit"]
        if c["check_at_home"]:
            return {"name": c["name"], "buy": "1 Packung, nur falls nicht mehr zu Hause", "check_at_home": True,
                    "price_if_needed": eur(c["price_cents"]) if c["price_cents"] else None, "in_total": False, "section": c["section"]}
        if c["pack_count"] and c["packs"] and c["packs"][0]["label"].endswith("(lose)"):
            buy = f"{c['pack_count']} Stück (lose)"
        elif c["pack_count"] and c["pack_qty"]:
            buy = f"{c['pack_count']} × {fmt_qty(c['pack_qty'], unit)}"
        elif c["pack_count"] and c["packs"]:
            buy = " + ".join(f"{p['count']} × {fmt_qty(p['pack_qty'], unit)}" for p in c["packs"] if p["count"])
        else:
            buy = fmt_qty(c["purchase_qty"], unit)
        view = {"name": c["name"], "buy": buy, "need": fmt_qty(c["need_qty"], unit), "section": c["section"],
                "price": eur(c["price_cents"]) if c["price_cents"] is not None else "unbekannt",
                "price_source": {"offer": "Angebot", "history": "zuletzt bezahlt", "estimate": "Schätzung", "none": "ohne Preis"}[c["price_source"]]}
        if c["offer"]:
            view["offer"] = c["offer"]["name"]
        if c["surplus_qty"] > 1e-6:
            view["surplus"] = f"{fmt_qty(c['surplus_qty'], unit)} bleiben übrig"
        return view

    def _nutrition_check(self, by_day: dict[str, dict[str, float]], meal_types: list[str]) -> dict[str, Any]:
        share = sum(MEAL_SHARE[m] for m in meal_types)
        targets = [dict(r) for r in self.conn.execute(
            "SELECT m.name, m.portion_factor, t.nutrient, t.daily_min, t.daily_max FROM nutrition_target t JOIN member m ON m.id = t.member_id")]
        days = [d for d, v in by_day.items() if v]
        avg = {k: round(sum(by_day[d][k] for d in days) / len(days), 1) for k in ("kcal", "protein_g", "carbs_g", "fat_g")} if days else {}
        checks, warnings = [], []
        for t in targets:
            if not avg:
                break
            planned = avg[t["nutrient"]] * t["portion_factor"]
            expected_min = t["daily_min"] * share if t["daily_min"] else None
            expected_max = t["daily_max"] * share if t["daily_max"] else None
            status = "ok"
            if expected_min and planned < 0.85 * expected_min:
                status = "zu niedrig"
                warnings.append(f"{t['name']}: geplante Mahlzeiten liefern Ø {planned:.0f} {t['nutrient'].replace('_g', ' g')} – "
                                f"anteilig wären ~{expected_min:.0f} nötig ({round(share * 100)} % des Tagesziels).")
            if expected_max and planned > 1.15 * expected_max:
                status = "zu hoch"
                warnings.append(f"{t['name']}: Ø {planned:.0f} {t['nutrient']} liegt über dem anteiligen Maximum ~{expected_max:.0f}.")
            checks.append({"member": t["name"], "nutrient": t["nutrient"], "planned_per_day": round(planned, 1),
                           "target_share": round(expected_min or expected_max or 0, 1), "status": status})
        return {"per_person_per_day_avg": avg, "meal_share_of_day": round(share, 2), "targets": checks, "warnings": warnings,
                "source": "Richtwerte aus Katalog/Angeboten, keine Produktangaben"}

    def _proposal_envelope(self, proposal: dict[str, Any], revised: bool = False) -> dict[str, Any]:
        meals = [m for m in proposal["meals"] if m.get("recipe")]
        totals = proposal["totals"]
        stores = ", ".join(totals["stores"]) or "–"
        start, end = proposal["period"]["start"], proposal["period"]["end"]
        summary = (f"{'Überarbeiteter ' if revised else ''}Vorschlag {self.day_label(start)}–{self.day_label(end)}: {len(meals)} Mahlzeiten, "
                   f"Einkauf bei {stores} für ca. {totals['food']}.")
        using = [m for m in meals if m.get("uses_pantry")]
        if using:
            summary += f" {len(using)} Gerichte nutzen Vorrat."
        next_actions = []
        if proposal["open_slots"]:
            summary += f" {len(proposal['open_slots'])} Termine noch ohne Gericht."
            next_actions.append(action(
                "revise_plan",
                "ZUERST: Für die offenen Termine selbst passende Gerichte vorschlagen (Regeln, Vorrat, Angebote beachten) und mit "
                "replace_meals=[{date, meal_type, recipe:{name, servings, ingredients:[\"200 g Hähnchenbrust\", …]}}] eintragen, "
                "dann dem Nutzer den vollständigen Plan zeigen.",
                {"proposal_id": proposal["proposal_id"]}))
        next_actions.append(action("confirm_plan", "Erst wenn der Nutzer dem gezeigten Plan zugestimmt hat",
                                   {"proposal_id": proposal["proposal_id"]}, consent=True))
        next_actions.append(action("revise_plan", "Wenn der Nutzer Gerichte, Tage, Läden oder Budget ändern möchte",
                                   {"proposal_id": proposal["proposal_id"]}))
        return envelope(summary, proposal, next_actions=next_actions, ask_user=proposal["ask_user"],
                        warnings=proposal["warnings"],
                        display_hint="Erst Gerichte nach Tagen (mit Grund, z. B. 'verwertet Spinat'), dann Einkauf je Laden mit Summe; "
                                     "Schätzpreise als 'ca.' kennzeichnen; Alternativen nur kurz erwähnen")

    # --- materialize -----------------------------------------------------------------------------
    def _materialize(self, plan_id: str, proposal: dict[str, Any], internal: dict[str, Any], today: dt.date) -> None:
        now = utc_now()
        slot_ids: dict[str, str] = {}
        for slot in internal["slots"]:
            if slot["source"] in ("skip", "done") or (not slot["recipe_id"] and not slot["lot_id"]):
                continue
            if slot["date"] < today.isoformat():
                continue
            slot_id = new_id("slot")
            slot_ids[slot["key"]] = slot_id
            self.conn.execute("INSERT INTO meal_slot (id, plan_id, date_on, meal_type, recipe_id, lot_id, servings, note, created_at, updated_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (slot_id, plan_id, slot["date"], slot["meal_type"], slot["recipe_id"], slot["lot_id"], slot["servings"],
                               slot["reason"], now, now))
        item_ids: dict[str, str] = {}
        for index, trip in enumerate(internal["trip_results"]):
            by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for c in trip["choices"]:
                by_store[c["store"]].append(c)
            for store, items in by_store.items():
                store_row = self.conn.execute("SELECT id FROM store WHERE name = ?", (store,)).fetchone() if store else None
                trip_id = new_id("trip")
                self.conn.execute(
                    "INSERT INTO shopping_trip (id, plan_id, store_id, store_name, planned_on, estimated_total_cents, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (trip_id, plan_id, store_row["id"] if store_row else None, store or None, trip["date"],
                     sum(i["price_cents"] or 0 for i in items if not i["check_at_home"]), now, now))
                for c in items:
                    item_id = new_id("item")
                    snapshot = jdumps(c["offer"] | {"packs": c["packs"]}) if c["offer"] else None
                    rationale = self._rationale(c)
                    self.conn.execute(
                        "INSERT INTO shopping_item (id, trip_id, ingredient_id, need_qty, purchase_qty, surplus_qty, pack_count, pack_qty, "
                        "est_price_cents, price_source, price_confidence, offer_snapshot, rationale, check_at_home) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (item_id, trip_id, c["ingredient_id"], c["need_qty"], c["purchase_qty"], c["surplus_qty"], c["pack_count"],
                         c["pack_qty"], c["price_cents"], c["price_source"], c["price_confidence"], snapshot, rationale, int(c["check_at_home"])))
                    if not c["check_at_home"]:
                        item_ids[f"trip{index}:{c['ingredient_id']}"] = item_id
        for key, allocations in internal["allocations"].items():
            slot_id = slot_ids.get(key)
            if not slot_id:
                continue
            for a in allocations:
                if a["qty"] <= 1e-9:
                    continue
                ing = a["ing"]
                if a["source"].startswith("trip"):
                    item_id = item_ids.get(a["source"])
                    if item_id:
                        self.conn.execute("INSERT INTO reservation (id, slot_id, ingredient_id, shopping_item_id, qty) VALUES (?,?,?,?,?)",
                                          (new_id("res"), slot_id, ing, item_id, a["qty"]))
                else:
                    lot = self.conn.execute("SELECT ingredient_id FROM lot WHERE id = ?", (a["source"],)).fetchone()
                    if lot is None:
                        continue
                    self.conn.execute("INSERT INTO reservation (id, slot_id, ingredient_id, lot_id, qty) VALUES (?,?,?,?,?)",
                                      (new_id("res"), slot_id, lot["ingredient_id"] or self._leftover_ingredient(), a["source"], a["qty"]))

    def _leftover_ingredient(self) -> str:
        row = self.conn.execute("SELECT id FROM ingredient WHERE name = 'Reste'").fetchone()
        if row:
            return row["id"]
        return self.catalog.create("Reste", {"base_unit": "pc", "allergens": [], "animal": None, "shelf_life_days": 3,
                                             "storage": "fridge"}, "pc")["id"]

    @staticmethod
    def _rationale(c: dict[str, Any]) -> str:
        parts = []
        if c["offer"]:
            parts.append(f"Angebot: {c['offer']['name']} für {c['offer']['price']}")
        elif c["price_source"] == "history":
            parts.append("Preis vom letzten Einkauf")
        elif c["price_source"] == "estimate":
            parts.append("Schätzpreis")
        if c["surplus_qty"] > 1e-6:
            parts.append(f"Überschuss {fmt_qty(c['surplus_qty'], c['base_unit'])} geht in den Vorrat")
        return "; ".join(parts)
