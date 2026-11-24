"""Shopping list, manual list changes and recording purchases."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from typing import Any

from .catalog import norm
from .core import action, envelope
from .db import KitchenError, jloads, new_id, utc_now
from .optimizer import ANY
from .planning import PlanningMixin
from .units import eur, fmt_packs, fmt_qty, parse_item, to_base


class ShoppingMixin(PlanningMixin):
    def _open_trips(self, store: str | None = None, day: dt.date | None = None) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM shopping_trip WHERE status = 'planned' ORDER BY planned_on, store_name")]
        if store:
            chain = self.match_chain(store)
            rows = [r for r in rows if (r["store_name"] or "") == chain or r["store_name"] is None] or []
        if day:
            rows = [r for r in rows if r["planned_on"] == day.isoformat()]
        return rows

    def _items(self, trip_id: str, statuses: tuple[str, ...] = ("open",)) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        rows = self.conn.execute(f"SELECT * FROM shopping_item WHERE trip_id = ? AND status IN ({marks})", (trip_id, *statuses))
        return [dict(r) for r in rows]

    def _item_line(self, item: dict[str, Any]) -> dict[str, Any]:
        ing = self.catalog.get(item["ingredient_id"])
        unit = ing["base_unit"]
        if item["check_at_home"]:
            buy = "1 Packung, falls nicht zu Hause"
        elif ing.get("is_supply"):
            buy = fmt_packs(item["purchase_qty"])
        elif item["pack_count"] and item["pack_qty"] and ing.get("g_per_piece") == item["pack_qty"] and ing.get("section") == "Obst & Gemüse":
            buy = f"{item['pack_count']} Stück"
        elif item["pack_count"] and item["pack_qty"]:
            buy = f"{item['pack_count']} × {fmt_qty(item['pack_qty'], unit)}"
        else:
            buy = fmt_qty(item["purchase_qty"], unit)
        offer = jloads(item["offer_snapshot"], None)
        used_for = [f"{self.day_label(r['date_on'])} {r['label']}" for r in self.conn.execute(
            "SELECT s.date_on, coalesce(rc.name, 'Reste') AS label FROM reservation r JOIN meal_slot s ON s.id = r.slot_id "
            "LEFT JOIN recipe rc ON rc.id = s.recipe_id WHERE r.shopping_item_id = ? AND r.status = 'active' ORDER BY s.date_on", (item["id"],))]
        line = {"name": ing["name"], "buy": buy,
                "price" if not item["check_at_home"] else "price_if_needed": eur(item["est_price_cents"]) if item["est_price_cents"] is not None else None,
                "section": ing.get("section") or "Drogerie & Sonstiges", "check_at_home": bool(item["check_at_home"])}
        if item["check_at_home"]:
            line["in_total"] = False
        elif item["price_confidence"] == "low" and item["est_price_cents"] is not None:
            line["price"] = "ca. " + line["price"]
        if offer:
            line["offer"] = offer.get("name")
        if used_for:
            line["for"] = list(dict.fromkeys(used_for))
        if item["is_manual"]:
            line["added_by_user"] = True
        if item["restock"]:
            line["restock"] = True
            line["why"] = item["rationale"]
        return line

    def get_shopping_list(self, store: str | None = None, date: str | None = None) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            day = self.parse_day(date) if date else None
            trips = self._open_trips(store, day)
            result = []
            for trip in trips:
                items = [self._item_line(i) for i in self._items(trip["id"])]
                if not items:
                    continue
                sections: dict[str, list] = defaultdict(list)
                for line in sorted(items, key=lambda l: (l["check_at_home"], l["name"])):
                    sections["Zu Hause prüfen" if line["check_at_home"] else line["section"]].append(line)
                total = sum(i["est_price_cents"] or 0 for i in self._items(trip["id"]) if not i["check_at_home"])
                result.append({"date": trip["planned_on"], "day": self.day_label(trip["planned_on"]),
                               "store": trip["store_name"] or "Supermarkt deiner Wahl", "estimated_total": eur(total),
                               "sections": dict(sorted(sections.items(), key=lambda kv: (kv[0] == "Zu Hause prüfen", kv[0])))})
            suggestions = self.restock_preview()
            hint = []
            if suggestions:
                hint = [action("update_shopping_list", "Bald nötige Haushaltsartikel/Grundzutaten mit auf die Liste (nach kurzem Ja)",
                               {"add": [x["name"] for x in suggestions]})]
            if not result:
                summary = "Die Einkaufsliste ist leer."
                if suggestions:
                    summary += f" Bald nötig: {', '.join(x['name'] for x in suggestions)}."
                return envelope(summary, {"trips": [], "restock_suggestions": suggestions}, next_actions=hint + [
                    action("plan_meals", "Wenn der Nutzer planen will"),
                    action("update_shopping_list", "Wenn der Nutzer etwas auf die Liste setzen will")])
            count = sum(len(v) for t in result for v in t["sections"].values())
            summary = f"{count} Posten: " + "; ".join(f"{t['day']} {t['store']} ca. {t['estimated_total']}" for t in result)
            if suggestions:
                summary += f". Außerdem bald nötig: {', '.join(x['name'] for x in suggestions)}"
            return envelope(summary, {"trips": result, "restock_suggestions": suggestions},
                            next_actions=hint + [action("record_purchase", "Nach dem Einkauf: 'alles gekauft außer …'"),
                                          action("update_shopping_list", "Etwas ergänzen oder streichen")],
                            display_hint="Je Laden nach Abschnitten (Obst & Gemüse, Kühlregal …) als Checkliste; Angebote markieren")
        return self.read(run)

    def update_shopping_list(self, add: list[Any] | None = None, remove: list[str] | None = None, store: str | None = None,
                             date: str | None = None) -> dict[str, Any]:
        args = {"add": add, "remove": remove, "store": store, "date": date}

        def run() -> dict[str, Any]:
            lines, warnings = [], []
            day = self.parse_day(date) if date else None
            trips = self._open_trips(store, day)
            trip = trips[0] if trips else None
            if add and trip is None:
                trip = self._new_trip(self.match_chain(store) if store else None, day or self.today())
            for raw in add or []:
                raw = {"name": raw} if isinstance(raw, str) else dict(raw)
                amount = parse_item(raw["name"], raw.get("qty"), raw.get("unit"))
                ing = self.catalog.resolve(amount.name, kind_hint=amount.kind, details=raw.get("details"))
                if amount.value is not None:
                    qty, _ = to_base(amount.value, amount.kind or ing["base_unit"], ing)
                else:
                    qty = ing.get("pack_qty") or 1
                qty = self._list_item(trip, ing, qty, manual=True, warnings=warnings)
                lines.append(f"+ {self.qty_label(qty, ing)} {ing['name']}")
            for name in remove or []:
                found, _ = self.catalog.candidates(name)
                hits = [i for t in (trips or self._open_trips()) for i in self._items(t["id"]) if i["ingredient_id"] == found]
                if not hits:
                    warnings.append(f"„{name}“ steht nicht auf der Liste.")
                    continue
                for item in hits:
                    reserved = self.conn.execute("SELECT count(*) FROM reservation WHERE shopping_item_id = ? AND status = 'active'",
                                                 (item["id"],)).fetchone()[0]
                    if reserved:
                        warnings.append(f"{self.catalog.get(found)['name']} war für geplante Gerichte vorgesehen – die fehlen dann.")
                        self.conn.execute("UPDATE reservation SET status = 'released' WHERE shopping_item_id = ? AND status = 'active'", (item["id"],))
                    self.conn.execute("UPDATE shopping_item SET status = 'removed' WHERE id = ?", (item["id"],))
                if any(i["restock"] for i in hits):
                    self._not_needed_now(self.catalog.get(found))
                lines.append(f"− {self.catalog.get(found)['name']}")
            if not lines:
                raise KitchenError("validation_error", "Nichts zu ändern: add oder remove angeben.")
            restocked = self._sync_restock()
            if restocked:
                lines.append("dazu, weil bald nötig: " + ", ".join(restocked))
            self._update_trip_totals()
            return envelope("Einkaufsliste: " + ", ".join(lines), {"changes": lines}, warnings=warnings,
                            next_actions=[action("get_shopping_list", "Liste zeigen, wenn gewünscht")])
        return self.command("update_shopping_list", args, run)

    # --- purchases ---------------------------------------------------------------------------
    def record_purchase(self, args: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            today = self.today()
            day = self.parse_day(args.get("date")) or today
            store = self.match_chain(args["store"]) if args.get("store") else None
            trips = [t for t in self._open_trips() if t["planned_on"] <= (day + dt.timedelta(days=1)).isoformat()]
            if store:
                trips = [t for t in trips if t["store_name"] in (store, None)]
            elif trips:
                first = trips[0]["planned_on"]
                trips = [t for t in trips if t["planned_on"] == first]
            rest_bought = args.get("rest_of_list_bought")
            if rest_bought is None:
                rest_bought = bool(trips) and not args.get("bought")
            list_ids = {i["ingredient_id"] for t in trips for i in self._items(t["id"])}
            missing_ids, not_listed = set(), []
            for name in args.get("missing") or []:
                try:
                    missing_ids.add(self._match_on_list(name, list_ids))
                except KitchenError as err:
                    if err.code != "not_found":
                        raise
                    not_listed.append(name)
            explicit: dict[str, dict[str, Any]] = {}
            extras: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for raw in args.get("bought") or []:
                raw = {"name": raw} if isinstance(raw, str) else dict(raw)
                amount = parse_item(raw["name"], raw.get("qty"), raw.get("unit"))
                on_list = self._match_on_list(amount.name, list_ids, required=False)
                ing = self.catalog.get(on_list) if on_list else self.catalog.resolve(amount.name, kind_hint=amount.kind, details=raw.get("details"))
                qty = to_base(amount.value, amount.kind or ing["base_unit"], ing)[0] if amount.value is not None else None
                entry = {"ing": ing, "qty": qty, "price_cents": None if raw.get("price_eur") is None else int(round(float(raw["price_eur"]) * 100)),
                         "best_before": self.parse_day(raw["best_before"]) if raw.get("best_before") else None,
                         "location": self.location(raw.get("location"), ing) if raw.get("location") else None, "label": raw["name"]}
                explicit[ing["id"]] = entry
            now = utc_now()
            purchase_id = new_id("pur")
            store_label = store or (trips[0]["store_name"] if trips else None)
            self.conn.execute("INSERT INTO purchase (id, trip_id, store_name, purchased_on, total_cents, created_at) VALUES (?,?,?,?,?,?)",
                              (purchase_id, trips[0]["id"] if len(trips) == 1 else None, store_label, day.isoformat(),
                               None if args.get("total_eur") is None else int(round(float(args["total_eur"]) * 100)), now))
            bought_lines, missing_lines, still_home = [], [], []
            warnings = [f"„{n}“ stand nicht auf der Liste – nichts zu tun." for n in not_listed]
            affected: list[dict[str, Any]] = []
            handled: set[str] = set()
            spent = 0
            for trip in trips:
                for item in self._items(trip["id"]):
                    ing = self.catalog.get(item["ingredient_id"])
                    entry = explicit.pop(ing["id"], None) if ing["id"] not in handled else None
                    if ing["id"] in missing_ids:
                        self.conn.execute("UPDATE shopping_item SET status = 'missing' WHERE id = ?", (item["id"],))
                        affected += self._release_item(item["id"])
                        missing_lines.append(ing["name"])
                        handled.add(ing["id"])
                        continue
                    if entry is None and not rest_bought:
                        continue
                    if entry is None and item["check_at_home"]:
                        self.conn.execute("UPDATE shopping_item SET status = 'removed' WHERE id = ?", (item["id"],))
                        if ing.get("is_supply"):
                            self.supply_had_enough(ing, day)  # was still at home: ask again later
                            still_home.append(ing["name"])
                        else:
                            self.add_lot(ing, 0)  # was at home: mark staple as present
                        continue
                    qty = (entry or {}).get("qty") or item["purchase_qty"]
                    price = (entry or {}).get("price_cents")
                    estimate = price is None
                    price = price if price is not None else item["est_price_cents"]
                    spent += price or 0
                    self._book_line(purchase_id, ing, qty, price, estimate, store_label or trip["store_name"], day,
                                    (entry or {}).get("best_before"), (entry or {}).get("location"), item, (entry or {}).get("label"))
                    if qty + 1e-6 < item["need_qty"]:
                        warnings.append(f"{ing['name']}: nur {self.qty_label(qty, ing)} statt {self.qty_label(item['need_qty'], ing)} gekauft.")
                    self.conn.execute("UPDATE shopping_item SET status = 'bought' WHERE id = ?", (item["id"],))
                    bought_lines.append(f"{self.qty_label(qty, ing)} {ing['name']}")
                    handled.add(ing["id"])
                open_left = self._items(trip["id"])
                if not open_left:
                    self.conn.execute("UPDATE shopping_trip SET status = 'purchased', updated_at = ? WHERE id = ?", (now, trip["id"]))
            for ing_id, entry in explicit.items():
                ing = entry["ing"]
                qty = entry["qty"] or ing.get("pack_qty") or 1
                if entry["qty"] is None and not ing.get("is_supply"):
                    warnings.append(f"Menge für {ing['name']} angenommen: {self.qty_label(qty, ing)}.")
                spent += entry["price_cents"] or 0
                self._book_line(purchase_id, ing, qty, entry["price_cents"], entry["price_cents"] is None, store_label, day,
                                entry["best_before"], entry["location"], None, entry["label"])
                bought_lines.append(f"{self.qty_label(qty, ing)} {ing['name']} (nicht auf der Liste)")
            if not bought_lines and not missing_lines:
                raise KitchenError("validation_error", "Ich weiß nicht, was gekauft wurde.",
                                   ask_user=[{"question": "Was hast du eingekauft? (oder: alles von der Liste?)"}])
            total = args.get("total_eur")
            if total is None:
                self.conn.execute("UPDATE purchase SET total_cents = ? WHERE id = ?", (spent, purchase_id))
            summary = f"Einkauf gebucht: {len(bought_lines)} Posten im Vorrat"
            if missing_lines:
                summary += f"; fehlte: {', '.join(missing_lines)}"
            if still_home:
                summary += f"; nicht gekauft, weil wohl noch zu Hause: {', '.join(still_home)} (frage ich später wieder)"
            ask_user, next_actions = [], []
            if affected:
                meals = sorted({f"{a['day']} {a['recipe']}" for a in affected})
                subs = self._substitutes([self.catalog.candidates(n)[0] for n in missing_lines if self.catalog.candidates(n)[0]])
                ask_user.append({"question": f"Ohne {', '.join(missing_lines)} fehlt etwas für: {', '.join(meals)}. "
                                             "Ersatz kaufen, Gericht tauschen oder ohne kochen?",
                                 "options": ["Ersatz kaufen", "Gericht tauschen", "ohne kochen"], "substitutes": subs})
                next_actions += [action("update_shopping_list", "Ersatz auf die Liste (anderer Laden möglich)"),
                                 action("revise_plan", "Gericht tauschen (bestätigten Plan anpassen, danach confirm_plan)")]
            next_actions.append(action("undo_last_action", "Falls die Buchung falsch ist"))
            return envelope(summary + ".", {"bought": bought_lines, "missing": missing_lines, "still_at_home": still_home,
                                            "affected_meals": affected},
                            warnings=warnings, ask_user=ask_user, next_actions=next_actions)
        return self.command("record_purchase", args, run)

    def _match_on_list(self, name: str, list_ids: set[str], required: bool = True) -> str | None:
        """Resolve a name the user said against what is on the shopping list first ("Käse" -> the Gouda on the list)."""
        found, options = self.catalog.candidates(name)
        if found and (found in list_ids or not list_ids):
            return found
        key = norm(name)
        hits = sorted({i for i in list_ids if key in norm(self.catalog.get(i)["name"])} | (set(options) & list_ids))
        if len(hits) == 1:
            return hits[0]
        if not required:
            return None
        names = sorted(self.catalog.get(i)["name"] for i in hits)[:8] or \
            (sorted(self.catalog.get(i)["name"] for i in options)[:8] if not found else [])
        if names:
            raise KitchenError("needs_clarification", f"Was genau fehlte bei „{name}“?", {"candidates": names},
                               ask_user=[{"question": f"Was genau gab es nicht („{name}“)?", "options": names}])
        raise KitchenError("not_found", f"„{name}“ steht nicht auf der Einkaufsliste.",
                           next_actions=[action("get_shopping_list", "Liste prüfen")])

    def _book_line(self, purchase_id: str, ing: dict[str, Any], qty: float, price: int | None, estimate: bool, store: str | None,
                   day: dt.date, best_before: dt.date | None, location: str | None, item: dict[str, Any] | None, label: str | None) -> None:
        line_id = new_id("pl")
        self.conn.execute("INSERT INTO purchase_line (id, purchase_id, shopping_item_id, ingredient_id, qty, price_cents, price_is_estimate, raw_label) "
                          "VALUES (?,?,?,?,?,?,?,?)", (line_id, purchase_id, item["id"] if item else None, ing["id"], qty, price, int(estimate), label))
        lot = self.add_lot(ing, qty, location=location, best_before=best_before, purchase_line_id=line_id, on=day)
        if price is not None and not estimate:
            self.conn.execute("INSERT INTO price_observation (id, store_name, ingredient_id, qty, price_cents, is_offer, observed_on) VALUES (?,?,?,?,?,?,?)",
                              (new_id("po"), store, ing["id"], qty, price, int(bool(item and item["offer_snapshot"])), day.isoformat()))
        if item:
            self.conn.execute("UPDATE reservation SET lot_id = ? WHERE shopping_item_id = ? AND status = 'active'", (lot["id"], item["id"]))

    def _release_item(self, item_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.id, s.date_on, coalesce(rc.name, 'Reste') AS recipe FROM reservation r JOIN meal_slot s ON s.id = r.slot_id "
            "LEFT JOIN recipe rc ON rc.id = s.recipe_id WHERE r.shopping_item_id = ? AND r.status = 'active'", (item_id,)).fetchall()
        self.conn.execute("UPDATE reservation SET status = 'released' WHERE shopping_item_id = ? AND status = 'active'", (item_id,))
        return [{"date": r["date_on"], "day": self.day_label(r["date_on"]), "recipe": r["recipe"]} for r in rows]

    def _substitutes(self, ingredient_ids: list[str]) -> list[str]:
        result = []
        stock = self.stock_map(self.today())
        offers = {o["ingredient_id"] for o in self.offers.active_offers(on=self.today()) if o["ingredient_id"]}
        for ing_id in ingredient_ids:
            ing = self.catalog.get(ing_id)
            for row in self.conn.execute("SELECT id, name FROM ingredient WHERE category = ? AND id != ? ORDER BY name", (ing["category"], ing_id)):
                if row["id"] in stock or row["id"] in offers:
                    result.append(row["name"] + (" (im Vorrat)" if row["id"] in stock else " (im Angebot)"))
        return result[:5]

    # --- list helpers shared by manual items and restocking -----------------------------------
    def _new_trip(self, store_name: str | None, day: dt.date) -> dict[str, Any]:
        trip = {"id": new_id("trip"), "store_name": store_name, "planned_on": day.isoformat()}
        now = utc_now()
        self.conn.execute("INSERT INTO shopping_trip (id, store_name, planned_on, estimated_total_cents, created_at, updated_at) "
                          "VALUES (?,?,?,0,?,?)", (trip["id"], store_name, trip["planned_on"], now, now))
        return trip

    def _update_trip_totals(self) -> None:
        for t in self._open_trips():
            total = sum(i["est_price_cents"] or 0 for i in self._items(t["id"]) if not i["check_at_home"])
            self.conn.execute("UPDATE shopping_trip SET estimated_total_cents = ? WHERE id = ?", (total, t["id"]))

    def _list_item(self, trip: dict[str, Any], ing: dict[str, Any], qty: float, *, manual: bool, restock: bool = False,
                   check_at_home: bool = False, rationale: str | None = None, warnings: list[str] | None = None) -> float:
        """Price an item for a trip (offer, own price history, estimate) and put it on the list. Returns the bought qty."""
        stores = {trip["store_name"]: 0} if trip["store_name"] else self.allowed_stores()[0]
        options = sorted(self._options(ing, dt.date.fromisoformat(trip["planned_on"]), stores),
                         key=lambda o: (o.source != "offer", (o.price_cents / o.pack_qty) if o.pack_qty else o.price_cents))
        price, source, confidence, snapshot, pack_qty, count = None, "none", "none", None, None, None
        if options:
            o = options[0]
            if o.pack_qty:
                count = max(1, int(-(-qty // o.pack_qty)))
                pack_qty, price = o.pack_qty, round(count * o.price_cents)
                qty = count * o.pack_qty
            else:
                price = round(o.price_cents * qty)
            source, confidence = o.source, o.confidence
            if o.source == "offer":
                snapshot = json.dumps({"name": o.label, "offer_id": o.offer_id, "store": o.store}, ensure_ascii=False)
                if not trip["store_name"] and o.store != ANY and warnings is not None:
                    warnings.append(f"{ing['name']} gibt es im Angebot bei {o.store}: {o.label}.")
        existing = self.conn.execute("SELECT id, est_price_cents FROM shopping_item WHERE trip_id = ? AND ingredient_id = ? "
                                     "AND status = 'open' AND (is_manual = 1 OR restock = 1)", (trip["id"], ing["id"])).fetchone() if manual else None
        if existing:
            total = None if price is None and existing["est_price_cents"] is None else (price or 0) + (existing["est_price_cents"] or 0)
            self.conn.execute("UPDATE shopping_item SET purchase_qty = purchase_qty + ?, need_qty = need_qty + ?, est_price_cents = ?, "
                              "pack_count = NULL, is_manual = 1, check_at_home = 0 WHERE id = ?", (qty, qty, total, existing["id"]))
        else:
            self.conn.execute(
                "INSERT INTO shopping_item (id, trip_id, ingredient_id, need_qty, purchase_qty, pack_count, pack_qty, est_price_cents, "
                "price_source, price_confidence, offer_snapshot, rationale, check_at_home, is_manual, restock) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("item"), trip["id"], ing["id"], qty, qty, count, pack_qty, price, source, confidence, snapshot, rationale,
                 int(check_at_home), int(manual), int(restock)))
        return qty

    def _not_needed_now(self, ing: dict[str, Any]) -> None:
        """The user took a restock item off the list: they still have enough."""
        if ing.get("is_supply"):
            self.supply_had_enough(ing, self.today())
        elif ing["is_staple"]:
            self.conn.execute("UPDATE lot SET staple_level = 'full', last_verified_on = ? WHERE ingredient_id = ? AND staple_level IS NOT NULL "
                              "AND status IN ('sealed','opened')", (self.today().isoformat(), ing["id"]))

    def _restock_wanted(self) -> list[dict[str, Any]]:
        """Due supplies and low staples that are not on any open list yet."""
        today = self.today()
        on_list = {i["ingredient_id"] for t in self._open_trips() for i in self._items(t["id"])}
        wanted = [d for d in self.supplies_due(today + dt.timedelta(days=60)) if d["ing"]["id"] not in on_list]
        wanted += [{"ing": s["ing"], "due_on": today, "level": s["level"], "reason": "ist " + ("leer" if s["level"] == "empty" else "knapp")}
                   for s in self.low_staples() if s["ing"]["id"] not in on_list]
        return wanted

    def restock_preview(self) -> list[dict[str, Any]]:
        """For read-only views: what the next list change will add (due before the last planned trip, or within 3 days)."""
        today = self.today()
        trips = [t for t in self._open_trips() if t["planned_on"] >= today.isoformat()]
        until = dt.date.fromisoformat(trips[-1]["planned_on"]) + dt.timedelta(days=7) if trips else today + dt.timedelta(days=3)
        return [{"name": w["ing"]["name"], "why": w["reason"], "firm": w["level"] in ("low", "empty")}
                for w in self._restock_wanted() if w["due_on"] < until]

    def _sync_restock(self, create_trip: bool = False) -> list[str]:
        """Put due household supplies and low staples on the shopping list (each on the trip before it runs out).
        Empty/low ones are firm items; supplies that are only predicted to run out are 'check at home' items."""
        today = self.today()
        trips = [t for t in self._open_trips() if t["planned_on"] >= today.isoformat()]
        wanted = self._restock_wanted()
        if not trips and create_trip and any(w["level"] in ("low", "empty") for w in wanted):
            trips = [self._new_trip(None, today)]
        if not trips:
            return []
        added = []
        for want in wanted:
            # The first trip whose next trip comes too late (or the last trip, covering a week).
            trip = None
            for index, t in enumerate(trips):
                horizon = dt.date.fromisoformat(trips[index + 1]["planned_on"]) if index + 1 < len(trips) else \
                    dt.date.fromisoformat(t["planned_on"]) + dt.timedelta(days=7)
                if want["due_on"] < horizon:
                    trip = t
                    break
            if trip is None:
                continue
            ing, firm = want["ing"], want["level"] in ("low", "empty")
            qty = 1 if ing.get("is_supply") else (ing.get("pack_qty") or 1)
            self._list_item(trip, ing, qty, manual=False, restock=True, check_at_home=not firm, rationale=want["reason"])
            added.append(ing["name"] + ("" if firm else " (prüfen)"))
        if added:
            self._update_trip_totals()
        return added
