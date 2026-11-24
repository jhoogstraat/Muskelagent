"""Cooking and skipping meals: consume reservations, create leftovers, free ingredients."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .catalog import norm
from .core import MEAL_LABEL, action, envelope
from .db import KitchenError, jdumps, new_id, utc_now
from .shopping import ShoppingMixin
from .units import fmt_qty, parse_item, to_base


class CookingMixin(ShoppingMixin):
    def _guess_meal_type(self) -> str:
        hour = self.clock.now().hour
        return "breakfast" if hour < 11 else "lunch" if hour < 16 else "dinner"

    def _find_slot(self, recipe: str | None, date: str | None, meal_type: str | None, statuses=("planned",)) -> dict[str, Any] | None:
        day = self.parse_day(date) if date else self.today()
        marks = ",".join("?" for _ in statuses)
        rows = [dict(r) for r in self.conn.execute(
            f"SELECT s.*, coalesce(rc.name, l.label) AS label FROM meal_slot s LEFT JOIN recipe rc ON rc.id = s.recipe_id "
            f"LEFT JOIN lot l ON l.id = s.lot_id WHERE s.status IN ({marks}) ORDER BY s.date_on", statuses)]
        if recipe:
            key = norm(recipe)
            matches = [r for r in rows if r["label"] and (key in norm(r["label"]) or norm(r["label"]) in key)]
            if not date:
                # Planned meals of the same name: the one closest to today.
                matches.sort(key=lambda r: (abs((dt.date.fromisoformat(r["date_on"]) - day).days), r["date_on"]))
                return matches[0] if matches and abs((dt.date.fromisoformat(matches[0]["date_on"]) - day).days) <= 3 else None
            rows = matches
        same_day = [r for r in rows if r["date_on"] == day.isoformat()]
        if meal_type:
            mt = self.meal_type(meal_type)
            same_day = [r for r in same_day if r["meal_type"] == mt]
        elif len(same_day) > 1:
            guess = self._guess_meal_type()
            same_day = [r for r in same_day if r["meal_type"] == guess] or same_day
        return same_day[0] if same_day else None

    def _finish_plan_if_done(self, plan_id: str | None) -> None:
        if not plan_id:
            return
        left = self.conn.execute("SELECT count(*) FROM meal_slot WHERE plan_id = ? AND status = 'planned'", (plan_id,)).fetchone()[0]
        if not left:
            self.conn.execute("UPDATE meal_plan SET status = 'completed', updated_at = ? WHERE id = ? AND status = 'confirmed'", (utc_now(), plan_id))

    def record_cooking(self, args: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            today = self.today()
            day = self.parse_day(args.get("date")) or today
            slot = self._find_slot(args.get("recipe"), args.get("date"), args.get("meal_type"))
            warnings, consumed_lines, consumed = [], [], []
            recipe = None
            if slot and slot["recipe_id"]:
                recipe = self.recipe(slot["recipe_id"])
            elif args.get("recipe") and not (slot and slot["lot_id"]):
                recipe = self.recipe_by_name(args["recipe"])
            if slot is None and recipe is None and not args.get("recipe") and not args.get("ingredients"):
                raise KitchenError("not_found", "Für heute ist nichts geplant – was wurde gekocht?",
                                   ask_user=[{"question": "Was hast du gekocht?"}])
            if slot and slot["lot_id"]:
                # Eating leftovers.
                lot = dict(self.conn.execute("SELECT * FROM lot WHERE id = ?", (slot["lot_id"],)).fetchone())
                eaten = float(args.get("servings_eaten") or slot["servings"])
                self.take(lot, min(eaten, lot["qty"]))
                self.conn.execute("UPDATE reservation SET status = 'consumed' WHERE slot_id = ? AND status = 'active'", (slot["id"],))
                self.conn.execute("UPDATE meal_slot SET status = 'cooked', updated_at = ? WHERE id = ?", (utc_now(), slot["id"]))
                self.conn.execute("INSERT INTO cook_log (id, slot_id, label, cooked_on, meal_type, servings_eaten, consumed, created_at) "
                                  "VALUES (?,?,?,?,?,?,?,?)", (new_id("ckl"), slot["id"], lot["label"], day.isoformat(), slot["meal_type"],
                                                               eaten, "[]", utc_now()))
                self._finish_plan_if_done(slot["plan_id"])
                return envelope(f"Gebucht: {lot['label']} gegessen ({eaten:g} Portionen).", {"meal": lot["label"]},
                                next_actions=[action("undo_last_action", "Falls falsch")])
            servings = float(args.get("servings_cooked") or (slot["servings"] if slot else self.servings_default()))
            if recipe:
                reserved = {}
                if slot:
                    for r in self.conn.execute("SELECT * FROM reservation WHERE slot_id = ? AND status = 'active'", (slot["id"],)):
                        reserved.setdefault(r["ingredient_id"], []).append(r["lot_id"])
                for item in recipe["ingredients"]:
                    ing = item["ingredient"]
                    if ing["is_staple"] or item["is_optional"] and ing["id"] not in reserved:
                        continue
                    need = item["qty_per_serving"] * servings
                    if need <= 1e-9:
                        continue
                    taken, short = self.consume(ing["id"], need, prefer=[l for l in reserved.get(ing["id"], []) if l])
                    consumed.append({"ingredient_id": ing["id"], "qty": need - short})
                    consumed_lines.append(f"{fmt_qty(need - short, ing['base_unit'])} {ing['name']}")
                    if short > 1e-6 and not item["is_optional"]:
                        warnings.append(f"{ing['name']}: laut Vorrat fehlten {fmt_qty(short, ing['base_unit'])} – Bestand war wohl ungenau.")
            for raw in args.get("ingredients") or []:
                raw = {"name": raw} if isinstance(raw, str) else dict(raw)
                amount = parse_item(raw["name"], raw.get("qty"), raw.get("unit"))
                ing = self.catalog.resolve(amount.name, kind_hint=amount.kind)
                if amount.value is None or ing["is_staple"]:
                    continue
                qty, _ = to_base(amount.value, amount.kind or ing["base_unit"], ing)
                _, short = self.consume(ing["id"], qty)
                consumed.append({"ingredient_id": ing["id"], "qty": qty - short})
                consumed_lines.append(f"{fmt_qty(qty - short, ing['base_unit'])} {ing['name']}")
            if not recipe and not args.get("ingredients"):
                warnings.append(f"„{args.get('recipe')}“ ist kein gespeichertes Rezept – Zutaten wurden nicht abgebucht. "
                                "Mit ingredients angeben oder das Rezept speichern.")
            if slot:
                self.conn.execute("UPDATE reservation SET status = 'consumed' WHERE slot_id = ? AND status = 'active'", (slot["id"],))
                self.conn.execute("UPDATE meal_slot SET status = 'cooked', updated_at = ? WHERE id = ?", (utc_now(), slot["id"]))
            label = recipe["name"] if recipe else str(args.get("recipe") or "Gekocht")
            leftover = args.get("leftover_servings")
            if leftover is None and args.get("servings_eaten") is not None:
                leftover = max(0.0, servings - float(args["servings_eaten"]))
            leftover_id = None
            if leftover and float(leftover) > 0:
                leftover_id = new_id("lot")
                shelf = recipe["cooked_shelf_life_days"] if recipe else 3
                self.conn.execute(
                    "INSERT INTO lot (id, ingredient_id, kind, recipe_id, label, qty, location, best_before_on, is_estimated_bb, added_on, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                    (leftover_id, self._leftover_ingredient(), "prepared", recipe["id"] if recipe else None, f"{label} (Reste)",
                     float(leftover), "fridge", (day + dt.timedelta(days=int(shelf))).isoformat(), day.isoformat(), utc_now(), utc_now()))
            self.conn.execute(
                "INSERT INTO cook_log (id, slot_id, recipe_id, label, cooked_on, meal_type, servings_cooked, servings_eaten, leftover_lot_id, consumed, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("ckl"), slot["id"] if slot else None, recipe["id"] if recipe else None, label, day.isoformat(),
                 slot["meal_type"] if slot else self.meal_type(args.get("meal_type"), self._guess_meal_type()), servings,
                 args.get("servings_eaten"), leftover_id, jdumps(consumed), utc_now()))
            if args.get("rating") and recipe:
                self.conn.execute("INSERT INTO recipe_rating (id, recipe_id, rating, comment, rated_on) VALUES (?,?,?,?,?)",
                                  (new_id("rat"), recipe["id"], int(args["rating"]), args.get("comment"), day.isoformat()))
            if slot:
                self._finish_plan_if_done(slot["plan_id"])
            summary = f"Gekocht: {label}"
            if consumed_lines:
                summary += f" – abgebucht: {', '.join(consumed_lines[:6])}{' …' if len(consumed_lines) > 6 else ''}"
            next_actions = []
            if leftover_id:
                summary += f"; {float(leftover):g} Portionen Reste im Kühlschrank"
                next_actions.append(action("revise_plan", "Reste in einen kommenden Termin einplanen (recipe: 'Reste')"))
            next_actions.append(action("undo_last_action", "Falls falsch gebucht"))
            if not slot and recipe:
                warnings.append("Das war nicht geplant – Zutaten wurden trotzdem abgebucht.")
            return envelope(summary + ".", {"recipe": label, "servings": servings, "consumed": consumed_lines,
                                            "leftover_servings": float(leftover) if leftover else 0}, warnings=warnings,
                            next_actions=next_actions)
        return self.command("record_cooking", args, run)

    def skip_meal(self, args: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            slot = self._find_slot(args.get("recipe"), args.get("date"), args.get("meal_type"))
            if slot is None:
                raise KitchenError("not_found", "Dafür ist keine Mahlzeit geplant.",
                                   next_actions=[action("get_kitchen_status", "Geplante Mahlzeiten ansehen")])
            when = f"{self.day_label(slot['date_on'])} {MEAL_LABEL[slot['meal_type']]}"
            if args.get("move_to"):
                target = self.parse_day(args["move_to"])  # weekdays count from today, like the user means them
                mt = self.meal_type(args.get("move_to_meal_type"), slot["meal_type"])
                taken = self.conn.execute("SELECT s.id, s.recipe_id, s.lot_id FROM meal_slot s WHERE s.date_on = ? AND s.meal_type = ? AND s.status = 'planned' AND s.id != ?",
                                          (target.isoformat(), mt, slot["id"])).fetchone()
                if taken:
                    # Swap the two meals.
                    self.conn.execute("UPDATE meal_slot SET date_on = ?, meal_type = ?, updated_at = ? WHERE id = ?",
                                      (slot["date_on"], slot["meal_type"], utc_now(), taken["id"]))
                self.conn.execute("UPDATE meal_slot SET date_on = ?, meal_type = ?, updated_at = ? WHERE id = ?",
                                  (target.isoformat(), mt, utc_now(), slot["id"]))
                self._swap_in_request(slot["plan_id"], f"{slot['date_on']}:{slot['meal_type']}", f"{target.isoformat()}:{mt}")
                warnings = self._freshness_after_move(slot["id"], target)
                return envelope(f"{slot['label']} verschoben auf {self.day_label(target)}" + (" (getauscht)" if taken else "") + ".",
                                {"moved": slot["label"], "to": target.isoformat()}, warnings=warnings,
                                next_actions=[action("undo_last_action", "Falls falsch")])
            freed = []
            for r in self.conn.execute("SELECT * FROM reservation WHERE slot_id = ? AND status = 'active'", (slot["id"],)).fetchall():
                ing = self.catalog.get(r["ingredient_id"])
                freed.append({"name": ing["name"], "qty": fmt_qty(r["qty"], ing["base_unit"]), "lot": r["lot_id"], "item": r["shopping_item_id"],
                              "ingredient_id": ing["id"]})
            self.conn.execute("UPDATE reservation SET status = 'released' WHERE slot_id = ? AND status = 'active'", (slot["id"],))
            self.conn.execute("UPDATE meal_slot SET status = 'skipped', note = ?, updated_at = ? WHERE id = ?", (args.get("reason"), utc_now(), slot["id"]))
            removed = []
            for f in freed:
                if f["item"] and not f["lot"]:
                    still = self.conn.execute("SELECT count(*) FROM reservation WHERE shopping_item_id = ? AND status = 'active'", (f["item"],)).fetchone()[0]
                    item = self.conn.execute("SELECT * FROM shopping_item WHERE id = ?", (f["item"],)).fetchone()
                    if not still and item and item["status"] == "open" and not item["is_manual"]:
                        self.conn.execute("UPDATE shopping_item SET status = 'removed' WHERE id = ?", (f["item"],))
                        removed.append(f["name"])
            self._finish_plan_if_done(slot["plan_id"])
            in_stock = [f for f in freed if f["lot"]]
            summary = f"{when} ({slot['label']}) gestrichen."
            if removed:
                summary += f" Von der Einkaufsliste genommen: {', '.join(removed)}."
            next_actions = [action("undo_last_action", "Falls falsch")]
            soon = []
            for f in in_stock:
                lot = self.conn.execute("SELECT * FROM lot WHERE id = ?", (f["lot"],)).fetchone()
                if lot and lot["best_before_on"] and (dt.date.fromisoformat(lot["best_before_on"]) - self.today()).days <= 3:
                    soon.append(f["name"])
            if soon:
                summary += f" Frei geworden und bald fällig: {', '.join(soon)}."
                next_actions.insert(0, action("find_recipes", "Verwertung für frei gewordene Zutaten vorschlagen", {"ingredients": soon}))
            return envelope(summary, {"skipped": slot["label"], "freed": [{"name": f["name"], "qty": f["qty"]} for f in in_stock],
                                      "removed_from_list": removed}, next_actions=next_actions)
        return self.command("skip_meal", args, run)

    def _swap_in_request(self, plan_id: str | None, old_key: str, new_key: str) -> None:
        """Keep the plan's stored wishes in step with a moved meal, so a later revise/confirm does not move it back."""
        if not plan_id:
            return
        row = self.conn.execute("SELECT request FROM meal_plan WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return
        req = json.loads(row["request"])
        a = req.get("assignments", {})
        a[old_key], a[new_key] = a.get(new_key), a.get(old_key)
        req["assignments"] = {k: v for k, v in a.items() if v is not None}
        start = dt.date.fromisoformat(req["start_on"])
        day, mt = new_key.split(":")
        end = start + dt.timedelta(days=req["days"] - 1)
        if dt.date.fromisoformat(day) > end:
            req["days"] = (dt.date.fromisoformat(day) - start).days + 1
        if mt not in req["meal_types"]:
            req.setdefault("extra_slots", []).append(new_key)
        self.conn.execute("UPDATE meal_plan SET request = ?, updated_at = ? WHERE id = ?", (jdumps(req), utc_now(), plan_id))

    def _freshness_after_move(self, slot_id: str, target: dt.date) -> list[str]:
        warnings = []
        for r in self.conn.execute("SELECT r.*, l.best_before_on FROM reservation r LEFT JOIN lot l ON l.id = r.lot_id WHERE r.slot_id = ? AND r.status = 'active'", (slot_id,)):
            if r["best_before_on"] and r["best_before_on"] < target.isoformat():
                warnings.append(f"{self.catalog.get(r['ingredient_id'])['name']} ist bis {self.day_label(target)} vermutlich nicht mehr gut.")
        return warnings
