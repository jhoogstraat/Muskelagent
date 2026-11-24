"""Entry point status, insights and offer search."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from .catalog import norm
from .cooking import CookingMixin
from .core import MEAL_LABEL, action, envelope
from .db import jloads
from .offers import offer_pack_in_base
from .units import eur, fmt_qty

GUIDE = """Kurzleitfaden für den Agenten:
- Beginne jedes Gespräch mit get_kitchen_status; folge next_actions und stelle Fragen aus ask_user in eigenen Worten.
- Der Nutzer nennt nie IDs. Übergib Namen, Mengen als Text ("500 g Hähnchenbrust") und Tage wörtlich ("gestern", "Dienstag").
- Der Server kennt das heutige Datum (Feld today in jeder Antwort) und den Plan. Frag nicht nach, ob ein Datum oder Gericht
  stimmt – ruf das passende Tool direkt auf; Unstimmigkeiten meldet der Server selbst in ask_user.
- Planen: plan_meals → Vorschlag zeigen → bei Zustimmung confirm_plan. Fehlen Rezepte, schlage selbst Gerichte vor und übergib sie mit Zutaten.
- Einkauf, Kochen, Vorrat bucht der Server direkt: record_purchase ("alles außer Feta"), record_cooking, update_pantry. Bei Fehlern: undo_last_action.
- Rechne nie selbst Mengen, Preise oder Nährwerte aus; nutze die Zahlen aus den Antworten. Schätzpreise als "ca." nennen.
- Allergien und Ausschlüsse prüft der Server; nenne Warnungen aus warnings immer.
- Antworte kurz, im Ton eines hilfsbereiten Mitbewohners, auf Deutsch, außer der Nutzer schreibt anders."""


class StatusMixin(CookingMixin):
    def get_kitchen_status(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            today = self.today()
            hh = self.household()
            ask_user, next_actions, warnings = [], [], []
            onboarding = not self.members() and not self.restrictions() and not self.store_rows() and not hh["stores_asked"]
            if onboarding:
                ask_user.append({"question": "Kurz zum Start: Für wie viele Personen kochst du, gibt es Allergien oder Dinge, die nicht "
                                             "auf den Tisch sollen, und in welchen Supermärkten kaufst du ein?",
                                 "save_with": "manage_household"})
            meals = [dict(r) for r in self.conn.execute(
                "SELECT s.*, coalesce(rc.name, l.label) AS label FROM meal_slot s LEFT JOIN recipe rc ON rc.id = s.recipe_id "
                "LEFT JOIN lot l ON l.id = s.lot_id WHERE s.status = 'planned' AND s.date_on >= ? ORDER BY s.date_on, s.meal_type LIMIT 6",
                ((today - dt.timedelta(days=1)).isoformat(),))]
            overdue = [m for m in meals if m["date_on"] < today.isoformat()]
            upcoming = [{"day": self.day_label(m["date_on"]), "meal": MEAL_LABEL[m["meal_type"]], "recipe": m["label"]}
                        for m in meals if m["date_on"] >= today.isoformat()][:4]
            if overdue:
                m = overdue[0]
                ask_user.append({"question": f"Hast du gestern {m['label']} gekocht?", "options": ["ja", "nein, ausgelassen", "verschieben"]})
                next_actions.append(action("record_cooking", "Wenn ja", {"recipe": m["label"], "date": m["date_on"]}))
                next_actions.append(action("skip_meal", "Wenn ausgelassen oder verschoben", {"recipe": m["label"], "date": m["date_on"]}))
            trips = [dict(r) for r in self.conn.execute("SELECT * FROM shopping_trip WHERE status = 'planned' ORDER BY planned_on LIMIT 4")]
            shopping = []
            for t in trips:
                count = self.conn.execute("SELECT count(*) FROM shopping_item WHERE trip_id = ? AND status = 'open'", (t["id"],)).fetchone()[0]
                if count:
                    shopping.append({"day": self.day_label(t["planned_on"]), "store": t["store_name"] or "beliebig", "items": count,
                                     "estimated_total": eur(t["estimated_total_cents"]), "overdue": t["planned_on"] < today.isoformat()})
            if any(s["overdue"] for s in shopping):
                ask_user.append({"question": "Warst du schon einkaufen? Dann buche ich den Einkauf.", "options": ["ja, alles", "ja, aber …", "noch nicht"]})
                next_actions.append(action("record_purchase", "Wenn eingekauft wurde (rest_of_list_bought=true, missing=[…])"))
            expiring, leftovers = [], []
            for lot in self.lots(include_staples=False):
                ing = self.catalog.get(lot["ingredient_id"]) if lot["kind"] == "raw" else None
                bb = self.effective_bb(lot, ing)
                if lot["kind"] == "prepared":
                    leftovers.append({"name": lot["label"], "portions": lot["qty"], "until": self.day_label(bb) if bb else None,
                                      "planned": lot["reserved"] > 0})
                elif bb and (bb - today).days <= 2 and lot["qty"] - lot["reserved"] > 0:
                    expiring.append({"name": ing["name"], "qty": fmt_qty(lot["qty"] - lot["reserved"], ing["base_unit"]),
                                     "until": self.day_label(bb), "expired": bb < today})
            low = [self.catalog.get(l["ingredient_id"])["name"] for l in self.lots() if l["staple_level"] in ("low", "empty")]
            if expiring:
                next_actions.append(action("find_recipes", "Ideen für bald Ablaufendes anbieten",
                                           {"ingredients": [e["name"] for e in expiring if not e["expired"]][:5], "use_expiring": True}))
            draft = self.conn.execute("SELECT id, revision, start_on FROM meal_plan WHERE status = 'draft' ORDER BY updated_at DESC LIMIT 1").fetchone()
            pending = None
            if draft:
                pending = f"{draft['id']}.r{draft['revision']}"
                next_actions.append(action("confirm_plan", "Offener Planvorschlag wartet auf Zustimmung (erst nach Ja)", {"proposal_id": pending}, consent=True))
            if not meals and not draft:
                next_actions.append(action("plan_meals", "Wenn der Nutzer planen möchte (z. B. 'Plan mir die Woche')"))
            fetched = self.offers.fetched_at()
            active = len(self.offers.active_offers(on=today))
            if not active:
                warnings.append("Keine aktuellen Angebote im Cache – Preise sind Schätzungen." +
                                (f" Letzter Abruf-Fehler: {self.offers.last_error}" if self.offers.last_error else ""))
            parts = []
            if upcoming:
                parts.append(f"Als Nächstes: {upcoming[0]['day']} {upcoming[0]['recipe']}")
            if shopping:
                parts.append(f"Einkauf {shopping[0]['day']} bei {shopping[0]['store']} ({shopping[0]['items']} Posten)")
            if expiring:
                parts.append(f"bald fällig: {', '.join(e['name'] for e in expiring[:4])}")
            if leftovers:
                parts.append(f"Reste: {', '.join(l['name'] for l in leftovers[:3])}")
            summary = "; ".join(parts) if parts else ("Neu hier – noch kein Plan und kein Vorrat." if onboarding else "Nichts geplant.")
            return envelope(summary, {
                "today": today.isoformat(), "weekday": self.day_label(today), "upcoming_meals": upcoming, "shopping": shopping,
                "expiring_soon": expiring, "leftovers": leftovers, "staples_low": low, "pending_proposal": pending,
                "household": self.household_overview() if not onboarding else None,
                "offers": {"active": active, "fetched_at": fetched.isoformat() if fetched else None},
                "guide": GUIDE,
            }, next_actions=next_actions, ask_user=ask_user, warnings=warnings)
        return self.read(run)

    def get_insights(self, topic: str = "overview", days: int = 30, ingredient: str | None = None) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            today = self.today()
            since = (today - dt.timedelta(days=days)).isoformat()
            data: dict[str, Any] = {"period": {"from": since, "to": today.isoformat()}}
            parts = []
            if topic in ("overview", "spending"):
                rows = self.conn.execute("SELECT store_name, purchased_on, total_cents FROM purchase WHERE purchased_on >= ?", (since,)).fetchall()
                by_store: dict[str, int] = defaultdict(int)
                by_week: dict[str, int] = defaultdict(int)
                for r in rows:
                    by_store[r["store_name"] or "unbekannt"] += r["total_cents"] or 0
                    week = dt.date.fromisoformat(r["purchased_on"]).isocalendar()
                    by_week[f"KW {week.week}"] += r["total_cents"] or 0
                total = sum(by_store.values())
                estimated = self.conn.execute("SELECT count(*) FROM purchase_line pl JOIN purchase p ON p.id = pl.purchase_id "
                                              "WHERE p.purchased_on >= ? AND pl.price_is_estimate = 1", (since,)).fetchone()[0]
                data["spending"] = {"total": eur(total), "by_store": {k: eur(v) for k, v in by_store.items()},
                                    "by_week": {k: eur(v) for k, v in sorted(by_week.items())}, "purchases": len(rows),
                                    "lines_with_estimated_price": estimated}
                parts.append(f"Ausgaben {eur(total)} in {len(rows)} Einkäufen")
            if topic in ("overview", "cooking"):
                rows = self.conn.execute("SELECT label, cooked_on, recipe_id FROM cook_log WHERE cooked_on >= ? AND label != 'waste' ORDER BY cooked_on DESC",
                                         (since,)).fetchall()
                counts: dict[str, int] = defaultdict(int)
                for r in rows:
                    counts[r["label"]] += 1
                ratings = {r["name"]: round(r["avg"], 1) for r in self.conn.execute(
                    "SELECT rc.name, avg(rr.rating) AS avg FROM recipe_rating rr JOIN recipe rc ON rc.id = rr.recipe_id GROUP BY rc.name")}
                data["cooking"] = {"meals": len(rows), "most_cooked": sorted(counts.items(), key=lambda kv: -kv[1])[:5],
                                   "ratings": ratings, "recent": [f"{r['cooked_on']} {r['label']}" for r in rows[:7]]}
                parts.append(f"{len(rows)} Mahlzeiten gekocht")
            if topic in ("overview", "waste"):
                rows = self.conn.execute("SELECT consumed FROM cook_log WHERE label = 'waste' AND cooked_on >= ?", (since,)).fetchall()
                waste = defaultdict(float)
                value = 0.0
                for r in rows:
                    for c in jloads(r["consumed"], []):
                        ing = self.catalog.get(c["ingredient_id"])
                        waste[ing["name"]] += c["qty"]
                        value += c["qty"] * (ing.get("price_cents") or 0) / (1 if ing["base_unit"] == "pc" else 1000)
                data["waste"] = {"items": {k: v for k, v in waste.items()}, "estimated_value": eur(value)}
                if waste:
                    parts.append(f"weggeworfen ca. {eur(value)}")
            if topic in ("overview", "nutrition"):
                rows = self.conn.execute("SELECT recipe_id, cooked_on, servings_cooked FROM cook_log WHERE recipe_id IS NOT NULL AND cooked_on >= ?",
                                         (since,)).fetchall()
                per_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
                for r in rows:
                    summary = self.recipe_summary(self.recipe(r["recipe_id"]))
                    for k, v in summary["per_serving"].items():
                        per_day[r["cooked_on"]][k] += v
                if per_day:
                    avg = {k: round(sum(d[k] for d in per_day.values()) / len(per_day), 1) for k in ("kcal", "protein_g", "carbs_g", "fat_g")}
                    data["nutrition"] = {"cooked_meals_per_person_per_day_avg": avg, "days": len(per_day),
                                         "note": "Nur gekochte, erfasste Mahlzeiten; Richtwerte"}
            if topic == "prices" or ingredient:
                found = self.catalog.candidates(ingredient)[0] if ingredient else None
                if found:
                    ing = self.catalog.get(found)
                    obs = [{"date": r["observed_on"], "store": r["store_name"], "price_per_unit": eur(r["price_cents"] / r["qty"] * (1 if ing["base_unit"] == "pc" else 1000)) + ("/Stück" if ing["base_unit"] == "pc" else "/kg" if ing["base_unit"] == "g" else "/l"),
                            "offer": bool(r["is_offer"])}
                           for r in self.conn.execute("SELECT * FROM price_observation WHERE ingredient_id = ? ORDER BY observed_on DESC LIMIT 20", (found,))]
                    offers = [{"store": o["store"], "name": o["name"], "price": eur(o["price_cents"])}
                              for o in self.offers.for_ingredients({found}, today).get(found, [])]
                    data["prices"] = {"ingredient": ing["name"], "paid": obs, "current_offers": offers,
                                      "catalog_estimate": eur(ing["price_cents"]) + ("/Stück" if ing["base_unit"] == "pc" else "/kg") if ing.get("price_cents") else None}
                    parts.append(f"{len(obs)} Preise für {ing['name']}")
            return envelope("; ".join(parts) or "Noch keine Daten.", data)
        return self.read(run)

    def find_offers(self, query: str | None = None, stores: list[str] | None = None, min_protein_g_per_100g: float | None = None,
                    max_price_eur: float | None = None, for_shopping_list: bool = False, sort_by: str = "protein_per_euro",
                    limit: int = 12) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            today = self.today()
            wanted_stores = {norm(self.match_chain(s)) for s in stores or []}
            list_ings = set()
            if for_shopping_list:
                list_ings = {r["ingredient_id"] for r in self.conn.execute(
                    "SELECT i.ingredient_id FROM shopping_item i JOIN shopping_trip t ON t.id = i.trip_id WHERE i.status = 'open' AND t.status = 'planned'")}
            q = norm(query or "")
            result = []
            for o in self.offers.active_offers(on=today):
                if wanted_stores and norm(o["store"]) not in wanted_stores:
                    continue
                ing = self.catalog.get(o["ingredient_id"]) if o["ingredient_id"] else None
                if q and q not in norm(o["name"]) and not any(q in norm(c) for c in o["categories"]) and not (ing and q in norm(ing["name"])):
                    continue
                if for_shopping_list and o["ingredient_id"] not in list_ings:
                    continue
                protein = (o["nutrition"] or {}).get("protein_g")
                if min_protein_g_per_100g is not None and (protein is None or protein < min_protein_g_per_100g):
                    continue
                if max_price_eur is not None and (o["price_cents"] is None or o["price_cents"] > max_price_eur * 100):
                    continue
                pack = offer_pack_in_base(o, ing) if ing else None
                per_euro = None
                if protein and o["pack_amount"] and o["price_cents"] and o["pack_unit"] in ("g", "ml"):
                    per_euro = round(protein * o["pack_amount"] / 100 / (o["price_cents"] / 100), 1)
                result.append({"store": o["store"], "name": o["name"], "price": eur(o["price_cents"]),
                               "regular_price": eur(o["regular_price_cents"]) if o["regular_price_cents"] else None,
                               "until": self.day_label(o["valid_until"][:10]) if o["valid_until"] else None,
                               "per_100g": o["nutrition"], "protein_per_euro_g": per_euro, "matches_ingredient": ing["name"] if ing else None,
                               "pack": fmt_qty(pack, ing["base_unit"]) if pack and ing else None, "_price": o["price_cents"] or 10**9,
                               "_protein": protein or -1, "_until": o["valid_until"] or "9999"})
            sorters = {"protein_per_euro": lambda r: -(r["protein_per_euro_g"] or -1), "price": lambda r: r["_price"],
                       "protein": lambda r: -r["_protein"], "expiry": lambda r: r["_until"]}
            result.sort(key=lambda r: (sorters.get(sort_by, sorters["protein_per_euro"])(r), r["name"]))
            for r in result:
                for k in ("_price", "_protein", "_until"):
                    r.pop(k)
            limit_ = max(1, min(50, int(limit)))
            fetched = self.offers.fetched_at()
            return envelope(f"{len(result)} aktuelle Angebote" + (f", zeige {min(limit_, len(result))}" if result else ""),
                            {"offers": result[:limit_], "total": len(result), "fetched_at": fetched.isoformat() if fetched else None},
                            next_actions=[action("update_shopping_list", "Wenn der Nutzer ein Angebot kaufen will"),
                                          action("plan_meals", "Angebote fließen beim Planen automatisch ein")])
        return self.read(run)


class Kitchen(StatusMixin):
    """The complete kitchen service used by the MCP server."""
