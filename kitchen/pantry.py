"""Pantry: lots with best-before dates, staples, FIFO consumption, inventory checks."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .catalog import STORAGE_WORDS, norm
from .core import KitchenBase, action, envelope
from .db import KitchenError, new_id, utc_now
from .units import fmt_qty, parse_item, to_base

LOCATIONS = STORAGE_WORDS
LOCATION_LABEL = {"fridge": "Kühlschrank", "freezer": "Tiefkühler", "pantry": "Vorrat"}
STAPLE_LEVELS = {"voll": "full", "full": "full", "da": "full", "genug": "full", "wenig": "low", "low": "low", "fast leer": "low",
                 "knapp": "low", "leer": "empty", "empty": "empty", "alle": "empty", "aus": "empty"}


class PantryMixin(KitchenBase):
    def location(self, value: str | None, ing: dict[str, Any] | None = None) -> str:
        if not value:
            return (ing or {}).get("storage") or "pantry"
        key = norm(value)
        if key not in LOCATIONS:
            raise KitchenError("validation_error", f"Lagerort „{value}“ unbekannt.", {"known": list(LOCATION_LABEL.values())})
        return LOCATIONS[key]

    def estimated_best_before(self, ing: dict[str, Any], location: str, start: dt.date) -> dt.date:
        if location == "freezer":
            days = ing.get("freezer_days") or 90
        else:
            days = ing.get("shelf_life_days") or 7
        return start + dt.timedelta(days=int(days))

    def effective_bb(self, lot: dict[str, Any], ing: dict[str, Any] | None = None) -> dt.date | None:
        bb = dt.date.fromisoformat(lot["best_before_on"]) if lot.get("best_before_on") else None
        if lot.get("status") == "opened" and lot.get("opened_on") and ing and ing.get("opened_shelf_life_days") and lot["location"] != "freezer":
            opened_limit = dt.date.fromisoformat(lot["opened_on"]) + dt.timedelta(days=int(ing["opened_shelf_life_days"]))
            bb = min(bb, opened_limit) if bb else opened_limit
        return bb

    def lots(self, ingredient_id: str | None = None, *, include_staples: bool = True) -> list[dict[str, Any]]:
        # Reservations of meals more than a day overdue no longer block stock (the meal evidently did not happen as planned).
        sql = ("SELECT l.*, coalesce((SELECT sum(r.qty) FROM reservation r JOIN meal_slot s ON s.id = r.slot_id "
               "WHERE r.lot_id = l.id AND r.status = 'active' AND s.date_on >= ?), 0) AS reserved "
               "FROM lot l WHERE l.status IN ('sealed','opened') AND (l.qty > 0 OR l.staple_level IS NOT NULL)")
        params: list[Any] = [(self.today() - dt.timedelta(days=1)).isoformat()]
        if ingredient_id:
            sql += " AND l.ingredient_id = ?"
            params.append(ingredient_id)
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        if not include_staples:
            rows = [r for r in rows if r["staple_level"] is None]
        return rows

    def add_lot(self, ing: dict[str, Any], qty: float, *, location: str | None = None, best_before: dt.date | None = None,
                purchase_line_id: str | None = None, on: dt.date | None = None) -> dict[str, Any]:
        now, today = utc_now(), on or self.today()
        location = location or ing.get("storage") or "pantry"
        estimated = best_before is None
        bb = best_before or self.estimated_best_before(ing, location, today)
        if ing["is_staple"]:
            existing = self.conn.execute("SELECT * FROM lot WHERE ingredient_id = ? AND staple_level IS NOT NULL AND status IN ('sealed','opened')",
                                         (ing["id"],)).fetchone()
            if existing:
                self.conn.execute("UPDATE lot SET staple_level = 'full', last_verified_on = ?, updated_at = ? WHERE id = ?",
                                  (today.isoformat(), now, existing["id"]))
                return dict(existing)
            lot_id = new_id("lot")
            self.conn.execute(
                "INSERT INTO lot (id, ingredient_id, qty, staple_level, location, best_before_on, is_estimated_bb, added_on, "
                "last_verified_on, purchase_line_id, created_at, updated_at) VALUES (?,?,0,'full',?,NULL,1,?,?,?,?,?)",
                (lot_id, ing["id"], location, today.isoformat(), today.isoformat(), purchase_line_id, now, now))
            return {"id": lot_id}
        same = self.conn.execute(
            "SELECT * FROM lot WHERE ingredient_id = ? AND location = ? AND status = 'sealed' AND kind = 'raw' AND best_before_on = ? AND purchase_line_id IS NULL",
            (ing["id"], location, bb.isoformat())).fetchone()
        if same and purchase_line_id is None:
            self.conn.execute("UPDATE lot SET qty = qty + ?, updated_at = ? WHERE id = ?", (qty, now, same["id"]))
            return dict(same)
        lot_id = new_id("lot")
        self.conn.execute(
            "INSERT INTO lot (id, ingredient_id, qty, location, best_before_on, is_estimated_bb, added_on, last_verified_on, "
            "purchase_line_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (lot_id, ing["id"], qty, location, bb.isoformat(), int(estimated), today.isoformat(), today.isoformat(),
             purchase_line_id, now, now))
        return {"id": lot_id}

    def take(self, lot: dict[str, Any], qty: float) -> None:
        now = utc_now()
        remaining = max(0.0, lot["qty"] - qty)
        status = "depleted" if remaining < 1e-6 else ("opened" if lot["status"] == "sealed" else lot["status"])
        opened_on = lot.get("opened_on") or (self.today().isoformat() if status == "opened" else None)
        self.conn.execute("UPDATE lot SET qty = ?, status = ?, opened_on = ?, updated_at = ? WHERE id = ?",
                          (0.0 if status == "depleted" else remaining, status, opened_on, now, lot["id"]))
        lot["qty"], lot["status"] = remaining, status

    def consume(self, ingredient_id: str, qty: float, *, prefer: list[str] | None = None,
                ignore_reservations_of: set[str] | None = None) -> tuple[list[tuple[str, float]], float]:
        """Use stock FIFO (opened first, then earliest best-before). Returns [(lot_id, qty)], shortfall."""
        ing = self.catalog.get(ingredient_id)
        lots = self.lots(ingredient_id, include_staples=False)
        prefer = prefer or []
        lots.sort(key=lambda l: (l["id"] not in prefer, l["status"] != "opened",
                                 self.effective_bb(l, ing) or dt.date.max, l["added_on"]))
        taken: list[tuple[str, float]] = []
        left = qty
        for lot in lots:
            if left <= 1e-9:
                break
            amount = min(left, lot["qty"])
            if amount <= 0:
                continue
            self.take(lot, amount)
            taken.append((lot["id"], amount))
            left -= amount
        return taken, max(0.0, left)

    # --- tools --------------------------------------------------------------------------------
    def get_pantry(self, query: str | None = None, location: str | None = None, expiring_within_days: int | None = None) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            today = self.today()
            loc = self.location(location) if location else None
            items, staples = [], []
            for lot in self.lots():
                if loc and lot["location"] != loc:
                    continue
                if lot["kind"] == "prepared":
                    name = lot["label"] or "Reste"
                    ing = None
                else:
                    ing = self.catalog.get(lot["ingredient_id"])
                    name = ing["name"]
                if query and norm(query) not in norm(name) and not (ing and any(norm(query) in norm(s) for s in ing["synonyms"])):
                    continue
                if lot["staple_level"] is not None:
                    staples.append({"name": name, "level": lot["staple_level"], "location": LOCATION_LABEL[lot["location"]]})
                    continue
                bb = self.effective_bb(lot, ing)
                days_left = (bb - today).days if bb else None
                if expiring_within_days is not None and (days_left is None or days_left > expiring_within_days):
                    continue
                unit = "pc" if lot["kind"] == "prepared" else ing["base_unit"]
                items.append({
                    "name": name, "qty": fmt_qty(lot["qty"], unit).replace("Stück", "Portionen" if lot["kind"] == "prepared" else "Stück"),
                    "free_qty": fmt_qty(max(0.0, lot["qty"] - lot["reserved"]), unit) if lot["reserved"] else None,
                    "reserved_for_meals": bool(lot["reserved"]),
                    "location": LOCATION_LABEL[lot["location"]], "best_before": bb.isoformat() if bb else None,
                    "days_left": days_left, "estimated_date": bool(lot["is_estimated_bb"]), "opened": lot["status"] == "opened",
                    "leftover": lot["kind"] == "prepared",
                })
            items.sort(key=lambda i: (i["days_left"] if i["days_left"] is not None else 9999, i["name"]))
            expiring = [i for i in items if i["days_left"] is not None and i["days_left"] <= 2]
            expired = [i for i in items if i["days_left"] is not None and i["days_left"] < 0]
            summary = f"{len(items)} Posten im Vorrat"
            if expiring:
                summary += f", {len(expiring)} laufen in ≤2 Tagen ab ({', '.join(i['name'] for i in expiring[:5])})"
            low = [s for s in staples if s["level"] != "full"]
            next_actions = []
            if expiring:
                next_actions.append(action("find_recipes", "Rezeptideen für bald Ablaufendes",
                                           {"ingredients": [i["name"] for i in expiring[:5] if not i["leftover"]]}))
            if low:
                next_actions.append(action("update_shopping_list", "Knappe Grundzutaten auf die Liste setzen",
                                           {"add": [s["name"] for s in low]}))
            return envelope(summary, {"items": items, "staples": staples},
                            warnings=[f"Abgelaufen: {', '.join(i['name'] for i in expired)}" if expired else None],
                            next_actions=next_actions,
                            display_hint="Nach Ablaufdatum, bald Ablaufendes hervorheben; bei geschätzten Daten 'ca.' davor")
        return self.read(run)

    def update_pantry(self, items: list[dict[str, Any]], stocktake_location: str | None = None) -> dict[str, Any]:
        """items: {name, qty?, unit?, action: add|set|use|discard|move|level, location?, best_before?, level?}"""
        args = {"items": items, "stocktake_location": stocktake_location}

        def run() -> dict[str, Any]:
            lines, warnings = [], []
            today = self.today()
            seen: set[str] = set()
            for raw in items:
                act = (raw.get("action") or "add").casefold()
                amount = parse_item(raw["name"], raw.get("qty"), raw.get("unit"))
                ing = self.catalog.resolve(amount.name, kind_hint=amount.kind, details=raw.get("details"))
                seen.add(ing["id"])
                loc = self.location(raw.get("location"), ing) if raw.get("location") or act in ("add", "set", "move") else None
                bb = self.parse_day(raw.get("best_before")) if raw.get("best_before") else None
                if ing["is_staple"] or act == "level":
                    level = STAPLE_LEVELS.get(norm(raw.get("level") or ""), None)
                    if level is None:
                        level = {"add": "full", "set": "full", "use": "full", "discard": "empty", "move": "full"}.get(act, "full")
                        if act == "set" and amount.value == 0:
                            level = "empty"
                    existing = self.conn.execute("SELECT id FROM lot WHERE ingredient_id = ? AND staple_level IS NOT NULL AND status IN ('sealed','opened')",
                                                 (ing["id"],)).fetchone()
                    if existing:
                        self.conn.execute("UPDATE lot SET staple_level = ?, last_verified_on = ?, updated_at = ? WHERE id = ?",
                                          (level, today.isoformat(), utc_now(), existing["id"]))
                    else:
                        if not ing["is_staple"]:
                            self.conn.execute("UPDATE ingredient SET is_staple = 1, updated_at = ? WHERE id = ?", (utc_now(), ing["id"]))
                            ing["is_staple"] = 1
                        self.add_lot(ing, 0, location=loc)
                        if level != "full":
                            self.conn.execute("UPDATE lot SET staple_level = ? WHERE ingredient_id = ? AND staple_level IS NOT NULL", (level, ing["id"]))
                    lines.append(f"{ing['name']}: {dict(full='voll', low='knapp', empty='leer')[level]}")
                    continue
                qty = None
                if amount.value is not None:
                    qty, approx = to_base(amount.value, amount.kind or ing["base_unit"], ing)
                if act == "add":
                    if qty is None:
                        qty = ing.get("pack_qty") or (1 if ing["base_unit"] == "pc" else None)
                        if qty is None:
                            raise KitchenError("validation_error", f"Wie viel {ing['name']}?",
                                               ask_user=[{"question": f"Wie viel {ing['name']} ist es?"}])
                        warnings.append(f"Menge für {ing['name']} angenommen: {fmt_qty(qty, ing['base_unit'])}")
                    self.add_lot(ing, qty, location=loc, best_before=bb)
                    lines.append(f"+ {fmt_qty(qty, ing['base_unit'])} {ing['name']} ({LOCATION_LABEL[loc]})")
                elif act == "use":
                    if qty is None:
                        raise KitchenError("validation_error", f"Wie viel {ing['name']} wurde verbraucht?")
                    reserved_before = sum(l["reserved"] for l in self.lots(ing["id"]))
                    _, short = self.consume(ing["id"], qty)
                    lines.append(f"− {fmt_qty(qty - short, ing['base_unit'])} {ing['name']}")
                    if short > 0:
                        warnings.append(f"Es war nur {fmt_qty(qty - short, ing['base_unit'])} {ing['name']} gebucht.")
                    self._warn_reserved(ing, reserved_before, warnings)
                elif act == "discard":
                    lots = [l for l in self.lots(ing["id"], include_staples=False) if not loc or l["location"] == loc]
                    lots.sort(key=lambda l: self.effective_bb(l, ing) or dt.date.max)
                    left = qty if qty is not None else sum(l["qty"] for l in lots)
                    total = 0.0
                    for lot in lots:
                        if left <= 1e-9:
                            break
                        amount_d = min(left, lot["qty"])
                        remaining = lot["qty"] - amount_d
                        self.conn.execute("UPDATE lot SET qty = ?, status = ?, updated_at = ? WHERE id = ?",
                                          (remaining, "discarded" if remaining <= 1e-9 else lot["status"], utc_now(), lot["id"]))
                        if remaining <= 1e-9:
                            if lot["reserved"]:
                                warnings.append(f"{ing['name']} war für geplante Mahlzeiten vorgesehen – dafür fehlt es jetzt.")
                            self.conn.execute("UPDATE reservation SET status = 'released' WHERE lot_id = ? AND status = 'active'", (lot["id"],))
                        self._log_waste(ing, amount_d)
                        left -= amount_d
                        total += amount_d
                    lines.append(f"entsorgt: {fmt_qty(total, ing['base_unit'])} {ing['name']}")
                elif act == "set":
                    if qty is None:
                        raise KitchenError("validation_error", f"Wie viel {ing['name']} ist da?")
                    self._set_total(ing, qty, loc if raw.get("location") else None, bb)
                    lines.append(f"{ing['name']} = {fmt_qty(qty, ing['base_unit'])}")
                elif act == "move":
                    lots = [l for l in self.lots(ing["id"], include_staples=False) if l["location"] != loc]
                    left = qty if qty is not None else sum(l["qty"] for l in lots)
                    moved = 0.0
                    for lot in lots:
                        if left <= 1e-9:
                            break
                        part = min(left, lot["qty"])
                        new_bb = bb or self.estimated_best_before(ing, loc, today)
                        if loc != "freezer" and lot["best_before_on"]:
                            new_bb = min(new_bb, dt.date.fromisoformat(lot["best_before_on"]))
                        if part >= lot["qty"] - 1e-9:
                            self.conn.execute("UPDATE lot SET location = ?, best_before_on = ?, is_estimated_bb = 1, updated_at = ? WHERE id = ?",
                                              (loc, new_bb.isoformat(), utc_now(), lot["id"]))
                        else:
                            self.take(lot, part)
                            self.add_lot(ing, part, location=loc, best_before=new_bb)
                        moved += part
                        left -= part
                    lines.append(f"{fmt_qty(moved, ing['base_unit'])} {ing['name']} → {LOCATION_LABEL[loc]}")
                else:
                    raise KitchenError("validation_error", f"Unbekannte Aktion „{act}“.", {"known": ["add", "set", "use", "discard", "move", "level"]})
            if stocktake_location:
                loc = self.location(stocktake_location)
                gone = [l for l in self.lots() if l["location"] == loc and l["ingredient_id"] not in seen and l["kind"] == "raw"]
                for lot in gone:
                    if lot["staple_level"] is not None:
                        continue
                    self.conn.execute("UPDATE lot SET qty = 0, status = 'depleted', updated_at = ? WHERE id = ?", (utc_now(), lot["id"]))
                    if lot["reserved"]:
                        warnings.append(f"{self.catalog.get(lot['ingredient_id'])['name']} war für geplante Mahlzeiten vorgesehen – dafür fehlt es jetzt.")
                    self.conn.execute("UPDATE reservation SET status = 'released' WHERE lot_id = ? AND status = 'active'", (lot["id"],))
                self.conn.execute("UPDATE lot SET last_verified_on = ? WHERE location = ? AND status IN ('sealed','opened')",
                                  (today.isoformat(), loc))
                if gone:
                    lines.append(f"Inventur {LOCATION_LABEL[loc]}: {len(gone)} nicht genannte Posten auf 0 gesetzt")
            return envelope("Vorrat aktualisiert: " + "; ".join(lines), {"changes": lines}, warnings=warnings,
                            next_actions=[action("undo_last_action", "Falls etwas falsch verstanden wurde")])
        return self.command("update_pantry", args, run)

    def _set_total(self, ing: dict[str, Any], qty: float, loc: str | None, bb: dt.date | None) -> None:
        lots = [l for l in self.lots(ing["id"], include_staples=False) if not loc or l["location"] == loc]
        current = sum(l["qty"] for l in lots)
        today = self.today().isoformat()
        if qty > current + 1e-9:
            self.add_lot(ing, qty - current, location=loc, best_before=bb)
        elif qty < current - 1e-9:
            excess = current - qty
            # Counting less than recorded: the newest lots are the ones most likely miscounted? No - the oldest got used.
            lots.sort(key=lambda l: self.effective_bb(l, ing) or dt.date.max)
            for lot in lots:
                if excess <= 1e-9:
                    break
                part = min(excess, lot["qty"])
                self.take(lot, part)
                excess -= part
        for lot in self.lots(ing["id"], include_staples=False):
            self.conn.execute("UPDATE lot SET last_verified_on = ? WHERE id = ?", (today, lot["id"]))
        if bb:
            self.conn.execute("UPDATE lot SET best_before_on = ?, is_estimated_bb = 0 WHERE ingredient_id = ? AND status IN ('sealed','opened') AND staple_level IS NULL",
                              (bb.isoformat(), ing["id"]))

    def _warn_reserved(self, ing: dict[str, Any], reserved_before: float, warnings: list[str]) -> None:
        free = sum(l["qty"] for l in self.lots(ing["id"], include_staples=False))
        if free + 1e-9 < reserved_before:
            warnings.append(f"{ing['name']} war für geplante Mahlzeiten reserviert; es fehlen jetzt {fmt_qty(reserved_before - free, ing['base_unit'])}.")

    def _log_waste(self, ing: dict[str, Any], qty: float) -> None:
        self.conn.execute(
            "INSERT INTO cook_log (id, label, cooked_on, consumed, created_at) VALUES (?, 'waste', ?, ?, ?)",
            (new_id("ckl"), self.today().isoformat(), __import__("json").dumps([{"ingredient_id": ing["id"], "qty": qty}]), utc_now()))
