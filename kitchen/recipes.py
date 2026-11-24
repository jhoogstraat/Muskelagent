"""Recipes: save with normalized ingredients, look up, score against pantry, offers and rules."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .catalog import norm, unknown_details, violations
from .core import action, envelope
from .db import KitchenError, jdumps, jloads, new_id, utc_now
from .pantry import PantryMixin
from .units import fmt_qty, parse_item, to_base


class RecipeMixin(PantryMixin):
    def recipe(self, recipe_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM recipe WHERE id = ?", (recipe_id,)).fetchone()
        if row is None:
            raise KitchenError("not_found", "Rezept nicht gefunden.")
        recipe = dict(row)
        recipe["tags"] = jloads(recipe["tags"], [])
        recipe["ingredients"] = []
        for ri in self.conn.execute("SELECT * FROM recipe_ingredient WHERE recipe_id = ? ORDER BY position", (recipe_id,)):
            ing = self.catalog.get(ri["ingredient_id"])
            recipe["ingredients"].append({"ingredient": ing, "qty_per_serving": ri["qty_per_serving"],
                                          "is_optional": bool(ri["is_optional"]), "note": ri["note"]})
        rating = self.conn.execute("SELECT avg(rating), count(*) FROM recipe_rating WHERE recipe_id = ?", (recipe_id,)).fetchone()
        recipe["rating"] = round(rating[0], 1) if rating[0] else None
        return recipe

    def recipe_by_name(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT id FROM recipe WHERE name = ?", (name.strip(),)).fetchone()
        if row:
            return self.recipe(row["id"])
        key = norm(name)
        matches = [r["id"] for r in self.conn.execute("SELECT id, name FROM recipe") if key in norm(r["name"]) or norm(r["name"]) in key]
        if len(matches) == 1:
            return self.recipe(matches[0])
        if len(matches) > 1:
            names = [self.recipe(m)["name"] for m in matches[:6]]
            raise KitchenError("needs_clarification", f"Mehrere Rezepte passen zu „{name}“.", {"candidates": names},
                               ask_user=[{"question": "Welches Rezept ist gemeint?", "options": names}])
        return None

    def recipe_summary(self, recipe: dict[str, Any], servings: float = 1.0) -> dict[str, Any]:
        nutrition = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
        complete = True
        for item in recipe["ingredients"]:
            if item["is_optional"]:
                continue
            values = self.nutrients_for(item["ingredient"], item["qty_per_serving"])
            if values is None:
                complete = False
                continue
            for k in nutrition:
                nutrition[k] += values[k]
        return {
            "name": recipe["name"], "time_min": (recipe["prep_min"] or 0) + (recipe["cook_min"] or 0) or None,
            "tags": recipe["tags"], "rating": recipe["rating"],
            "per_serving": {k: round(v, 1) for k, v in nutrition.items()}, "nutrition_complete": complete,
            "ingredients": [f"{fmt_qty(i['qty_per_serving'] * servings, i['ingredient']['base_unit'])} {i['ingredient']['name']}"
                            + (" (optional)" if i["is_optional"] else "") for i in recipe["ingredients"]],
        }

    def recipe_violations(self, recipe: dict[str, Any], restrictions: list[dict[str, Any]] | None = None) -> list[str]:
        restrictions = self.restrictions() if restrictions is None else restrictions
        reasons: list[str] = []
        for item in recipe["ingredients"]:
            if not item["is_optional"]:
                reasons += violations(item["ingredient"], restrictions)
        return reasons

    def recipe_unknowns(self, recipe: dict[str, Any], restrictions: list[dict[str, Any]]) -> list[str]:
        return [i["ingredient"]["name"] for i in recipe["ingredients"] if unknown_details(i["ingredient"], restrictions)]

    def store_recipe(self, data: dict[str, Any], *, source: str = "agent") -> tuple[dict[str, Any], list[str]]:
        """Create or replace a recipe. `ingredients` are amounts for `servings` portions."""
        name = (data.get("name") or "").strip()
        if not name:
            raise KitchenError("validation_error", "Rezept braucht einen Namen.")
        servings = float(data.get("servings") or 1)
        if servings <= 0:
            raise KitchenError("validation_error", "servings muss größer als 0 sein.")
        ingredients = data.get("ingredients") or []
        if not ingredients:
            raise KitchenError("validation_error", f"Rezept „{name}“ braucht Zutaten mit Mengen.",
                               ask_user=[{"question": f"Welche Zutaten (mit Mengen) braucht „{name}“?"}])
        warnings: list[str] = []
        rows = []
        clarify: list[dict[str, Any]] = []
        for position, raw in enumerate(ingredients):
            raw = {"name": raw} if isinstance(raw, str) else dict(raw)
            amount = parse_item(raw["name"], raw.get("qty"), raw.get("unit"))
            try:
                ing = self.catalog.resolve(amount.name, kind_hint=amount.kind, details=raw.get("details"))
            except KitchenError as err:
                if err.code == "needs_clarification":
                    clarify.append({"input": raw["name"], "candidates": err.details["candidates"]})
                    continue
                raise
            if amount.value is None:
                if ing["is_staple"]:
                    qty = 0.0
                else:
                    raise KitchenError("validation_error", f"Menge für „{raw['name']}“ fehlt.",
                                       ask_user=[{"question": f"Wie viel {ing['name']} braucht das Rezept?"}])
            else:
                qty, approx = to_base(amount.value, amount.kind or ing["base_unit"], ing)
            rows.append((ing, qty / servings, bool(raw.get("optional")), raw.get("note")))
        if clarify:
            raise KitchenError("needs_clarification", "Einige Zutaten sind mehrdeutig.", {"ingredients": clarify},
                               ask_user=[{"question": f"Mit „{c['input']}“ gemeint?", "options": c["candidates"]} for c in clarify],
                               next_actions=[action("save_recipe", "Erneut mit eindeutigen Zutatennamen aufrufen")])
        now = utc_now()
        existing = self.conn.execute("SELECT id FROM recipe WHERE name = ?", (name,)).fetchone()
        recipe_id = existing["id"] if existing else new_id("rec")
        values = (name, data.get("prep_min"), data.get("cook_min"), jdumps([norm(t) for t in data.get("tags") or []]),
                  data.get("instructions"), int(bool(data.get("is_meal_prep"))), float(data.get("cooked_shelf_life_days") or 3), source)
        if existing:
            self.conn.execute("UPDATE recipe SET name=?, prep_min=?, cook_min=?, tags=?, instructions=?, is_meal_prep=?, "
                              "cooked_shelf_life_days=?, source=?, updated_at=? WHERE id=?", values + (now, recipe_id))
            self.conn.execute("DELETE FROM recipe_ingredient WHERE recipe_id = ?", (recipe_id,))
        else:
            self.conn.execute("INSERT INTO recipe (name, prep_min, cook_min, tags, instructions, is_meal_prep, cooked_shelf_life_days, "
                              "source, id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", values + (recipe_id, now, now))
        merged: dict[str, list] = {}
        for ing, qty, optional, note in rows:
            if ing["id"] in merged:
                merged[ing["id"]][1] += qty
            else:
                merged[ing["id"]] = [ing, qty, optional, note]
        for position, (ing, qty, optional, note) in enumerate(merged.values()):
            self.conn.execute("INSERT INTO recipe_ingredient (id, recipe_id, ingredient_id, qty_per_serving, is_optional, note, position) "
                              "VALUES (?,?,?,?,?,?,?)", (new_id("ri"), recipe_id, ing["id"], qty, int(optional), note, position))
        recipe = self.recipe(recipe_id)
        restrictions = self.restrictions()
        unknown = self.recipe_unknowns(recipe, restrictions)
        if unknown:
            warnings.append(f"Allergene/Tierprodukt unbekannt für: {', '.join(unknown)}. Bitte mit details.allergens angeben.")
        reasons = self.recipe_violations(recipe, restrictions)
        if reasons:
            warnings.append("Verstößt gegen Haushaltsregeln: " + "; ".join(reasons))
        return recipe, warnings

    def save_recipe(self, data: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            recipe, warnings = self.store_recipe(data, source=data.get("source") or "agent")
            info = self.recipe_summary(recipe, float(data.get("servings") or 1))
            return envelope(f"Rezept „{recipe['name']}“ gespeichert ({len(recipe['ingredients'])} Zutaten).", info,
                            warnings=warnings,
                            next_actions=[action("plan_meals", "Wenn der Nutzer das Gericht einplanen will",
                                                 {"meals": [{"recipe": recipe["name"]}]})])
        return self.command("save_recipe", data, run)

    # --- scoring ---------------------------------------------------------------------------------
    def stock_map(self, on: dt.date) -> dict[str, dict[str, Any]]:
        """ingredient_id -> free qty and earliest best-before of usable stock."""
        stock: dict[str, dict[str, Any]] = {}
        for lot in self.lots(include_staples=True):
            if lot["kind"] != "raw":
                continue
            ing_id = lot["ingredient_id"]
            entry = stock.setdefault(ing_id, {"qty": 0.0, "bb": None, "staple": False})
            if lot["staple_level"] is not None:
                entry["staple"] = lot["staple_level"] != "empty"
                continue
            free = max(0.0, lot["qty"] - lot["reserved"])
            if free <= 0:
                continue
            ing = self.catalog.get(ing_id)
            bb = self.effective_bb(lot, ing)
            if bb and bb < on:
                continue
            entry["qty"] += free
            if bb and (entry["bb"] is None or bb < entry["bb"]):
                entry["bb"] = bb
        return stock

    def find_recipes(self, query: str | None = None, ingredients: list[str] | None = None, use_expiring: bool = False,
                     max_minutes: int | None = None, limit: int = 8) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            today = self.today()
            stock = self.stock_map(today)
            restrictions = self.restrictions()
            wanted = []
            for name in ingredients or []:
                found, options = self.catalog.candidates(name)
                if found:
                    wanted.append(found)
            expiring = {i for i, s in stock.items() if s["bb"] and (s["bb"] - today).days <= 3 and s["qty"] > 0}
            offers = {o["ingredient_id"] for o in self.offers.active_offers(on=today) if o["ingredient_id"]}
            results = []
            for row in self.conn.execute("SELECT id FROM recipe"):
                recipe = self.recipe(row["id"])
                if query and norm(query) not in norm(recipe["name"]) and not any(norm(query) in t for t in recipe["tags"]) \
                        and not any(norm(query) in norm(i["ingredient"]["name"]) for i in recipe["ingredients"]):
                    continue
                total = (recipe["prep_min"] or 0) + (recipe["cook_min"] or 0)
                if max_minutes and total and total > max_minutes:
                    continue
                ids = {i["ingredient"]["id"] for i in recipe["ingredients"] if not i["is_optional"]}
                if wanted and not set(wanted) & ids:
                    continue
                if self.recipe_violations(recipe, restrictions):
                    continue
                have = [i["ingredient"]["name"] for i in recipe["ingredients"]
                        if not i["is_optional"] and (stock.get(i["ingredient"]["id"], {}).get("qty", 0) > 0 or stock.get(i["ingredient"]["id"], {}).get("staple"))]
                uses_expiring = [i["ingredient"]["name"] for i in recipe["ingredients"] if i["ingredient"]["id"] in expiring]
                if use_expiring and not uses_expiring:
                    continue
                score = 3 * len(uses_expiring) + 2 * len(set(wanted) & ids) + len(have) / max(1, len(ids)) + (recipe["rating"] or 3) / 5 \
                    + 0.5 * len(ids & offers)
                info = self.recipe_summary(recipe, self.servings_default())
                info.update({"have_at_home": have, "missing": [i["ingredient"]["name"] for i in recipe["ingredients"]
                                                              if not i["is_optional"] and i["ingredient"]["name"] not in have],
                             "uses_expiring": uses_expiring, "on_offer": [i["ingredient"]["name"] for i in recipe["ingredients"] if i["ingredient"]["id"] in offers]})
                results.append((score, recipe["name"], info))
            results.sort(key=lambda r: (-r[0], r[1]))
            found = [r[2] for r in results[:limit]]
            expiring_names = sorted(self.catalog.get(i)["name"] for i in expiring)
            hints = {
                "expiring_soon": expiring_names,
                "restrictions": [f"{r['kind']}: {r['value']}" for r in restrictions],
                "dislikes": [p["value"] for p in self.preferences() if p["weight"] < 0],
            }
            if found:
                summary = f"{len(found)} passende gespeicherte Rezepte"
                next_actions = [action("plan_meals", "Gewähltes Rezept einplanen", {"meals": [{"recipe": found[0]["name"]}]}),
                                action("save_recipe", "Wenn du ein neues Rezept vorschlägst und der Nutzer es behalten will")]
            else:
                summary = "Keine gespeicherten Rezepte passen. Schlag selbst 2–3 Gerichte vor (Regeln in data.hints beachten)."
                next_actions = [action("save_recipe", "Gefällt dem Nutzer ein Vorschlag: mit Zutaten und Mengen speichern"),
                                action("plan_meals", "Oder direkt einplanen: meals=[{recipe:{name, servings, ingredients}}]")]
            return envelope(summary, {"recipes": found, "hints": hints}, next_actions=next_actions,
                            display_hint="Kurz: Name, Zeit, was fehlt; bald Ablaufendes betonen")
        return self.read(run)
