"""Ingredient catalog: seed data, fuzzy name resolution, dietary rule checks, stores."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import KitchenError, jdumps, jloads, new_id, utc_now

ALLERGEN_WORDS = {
    "gluten": "gluten", "weizen": "gluten", "zöliakie": "gluten",
    "laktose": "lactose", "lactose": "lactose", "milchzucker": "lactose",
    "milch": "milk", "milcheiweiß": "milk", "milk": "milk", "kuhmilch": "milk",
    "ei": "eggs", "eier": "eggs", "eggs": "eggs", "hühnerei": "eggs",
    "fisch": "fish", "fish": "fish",
    "erdnuss": "peanuts", "erdnüsse": "peanuts", "peanuts": "peanuts",
    "nuss": "tree_nuts", "nüsse": "tree_nuts", "schalenfrüchte": "tree_nuts", "baumnüsse": "tree_nuts", "tree_nuts": "tree_nuts",
    "soja": "soy", "soy": "soy",
    "sellerie": "celery", "celery": "celery",
    "senf": "mustard", "mustard": "mustard",
    "sesam": "sesame", "sesame": "sesame",
    "sulfit": "sulphites", "sulfite": "sulphites", "schwefeldioxid": "sulphites", "sulphites": "sulphites",
    "lupine": "lupin", "lupinen": "lupin", "lupin": "lupin",
    "krebstiere": "crustaceans", "garnelen": "crustaceans", "shrimps": "crustaceans", "krustentiere": "crustaceans", "crustaceans": "crustaceans",
    "weichtiere": "molluscs", "muscheln": "molluscs", "tintenfisch": "molluscs", "molluscs": "molluscs",
}
DIETS = {
    "vegetarisch": "vegetarian", "vegetarian": "vegetarian", "vegetarier": "vegetarian",
    "vegan": "vegan", "veganer": "vegan",
    "pescetarisch": "pescetarian", "pescetarian": "pescetarian", "pescetarier": "pescetarian",
    "kein schwein": "no_pork", "no_pork": "no_pork", "halal": "no_pork",
}
DIET_FORBIDS = {
    "vegetarian": {"meat", "fish"},
    "pescetarian": {"meat"},
    "vegan": {"meat", "fish", "dairy", "egg", "honey"},
}
INGREDIENT_COLUMNS = (
    "name", "synonyms", "category", "section", "base_unit", "g_per_piece", "density_g_per_ml",
    "shelf_life_days", "opened_shelf_life_days", "freezer_days", "storage", "allergens", "animal",
    "kcal", "protein_g", "carbs_g", "fat_g", "nutrient_source", "price_cents", "pack_qty", "is_staple",
)


CUTS = ("filetseite", "filets", "filet", "portionen", "portion", "teilstücke", "teilstück", "kerne", "stücke", "scheiben",
        "würfel", "streifen", "seite", "s", "n", "e")
STORAGE_WORDS = {"kühlschrank": "fridge", "fridge": "fridge", "kühl": "fridge", "gekühlt": "fridge", "tiefkühler": "freezer",
                 "gefrierschrank": "freezer", "tk": "freezer", "freezer": "freezer", "eisfach": "freezer", "tiefkühltruhe": "freezer",
                 "tiefgekühlt": "freezer", "vorrat": "pantry", "schrank": "pantry", "vorratsschrank": "pantry", "pantry": "pantry",
                 "keller": "pantry", "cabinet": "pantry", "speisekammer": "pantry", "other": "pantry", "regal": "pantry",
                 "trocken": "pantry", "raumtemperatur": "pantry"}


def only_cuts(rest: str) -> bool:
    """True if `rest` is made only of cut/form words: "filetportionen", "kerne", "filets"."""
    while rest:
        for cut in CUTS:
            if rest.startswith(cut):
                rest = rest[len(cut):]
                break
        else:
            return False
    return True


def fold(text: str) -> str:
    return text.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def storage_code(value: str | None) -> str | None:
    return STORAGE_WORDS.get(norm(value)) if value else None


def norm(text: str) -> str:
    text = text.casefold().replace("-", "").replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    return " ".join(text.split())


def variants(q: str) -> list[str]:
    """q plus singular guesses for its last word (German plurals)."""
    words = q.split()
    if not words:
        return [q]
    last = words[-1]
    forms = [last]
    for suffix in ("nen", "en", "er", "n", "e", "s"):
        if last.endswith(suffix) and len(last) - len(suffix) >= 3:
            forms.append(last[: -len(suffix)])
    if last.endswith("äpfel"):
        forms.append(last[:-5] + "apfel")
    return list(dict.fromkeys(" ".join(words[:-1] + [f]) for f in forms))


def load_seed(conn: sqlite3.Connection) -> int:
    return _load_food(conn) + _load_supplies(conn)


def _load_supplies(conn: sqlite3.Connection) -> int:
    """Household supplies; also added to existing databases, and turns same-named user items into supplies."""
    from .supplies_seed import SUPPLIES, SUPPLY_SECTION
    stamp = f"{len(SUPPLIES)}:{sum(len(s['synonyms']) for s in SUPPLIES)}"
    if (conn.execute("SELECT value FROM meta WHERE key = 'supply_seed'").fetchone() or [None])[0] == stamp:
        return 0
    now = utc_now()
    for item in SUPPLIES:
        values = {"synonyms": jdumps(item["synonyms"]), "category": item["category"], "section": SUPPLY_SECTION,
                  "price_cents": item["price_cents"], "restock_days": item["restock_days"],
                  "restock_per_person": int(item["per_person"])}
        conn.execute(
            "INSERT OR IGNORE INTO ingredient (id, name, base_unit, pack_qty, storage, allergens, animal, is_supply, is_builtin, "
            f"details_known, created_at, updated_at, {', '.join(values)}) VALUES (?, ?, 'pc', 1, 'pantry', '[]', NULL, 1, 1, 1, ?, ?, "
            f"{', '.join('?' for _ in values)})", [new_id("ing"), item["name"], now, now, *values.values()])
        conn.execute(
            f"UPDATE ingredient SET is_supply = 1, details_known = 1, base_unit = 'pc', pack_qty = 1, allergens = '[]', animal = NULL, "
            f"{', '.join(f'{k} = ?' for k in values)}, updated_at = ? WHERE name = ? COLLATE NOCASE",
            [*values.values(), now, item["name"]])
        # Items an agent created earlier under a synonym ("Klopapier" as a 'food') become supplies too.
        labels = [label.casefold() for label in item["synonyms"]]
        conn.execute(
            "UPDATE ingredient SET is_supply = 1, details_known = 1, base_unit = 'pc', pack_qty = 1, allergens = '[]', animal = NULL, "
            f"section = ?, category = coalesce(category, ?), restock_days = ?, restock_per_person = ?, updated_at = ? "
            f"WHERE is_builtin = 0 AND is_supply = 0 AND lower(name) IN ({', '.join('?' for _ in labels)})",
            [SUPPLY_SECTION, item["category"], item["restock_days"], int(item["per_person"]), now, *labels])
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('supply_seed', ?)", (stamp,))
    return len(SUPPLIES)


def _load_food(conn: sqlite3.Connection) -> int:
    if conn.execute("SELECT 1 FROM ingredient WHERE is_builtin = 1 AND is_supply = 0 LIMIT 1").fetchone():
        return 0
    try:
        from .catalog_seed import SEED
    except ImportError:
        return 0
    now = utc_now()
    for item in SEED:
        values = {
            "name": item["name"], "synonyms": jdumps(item.get("synonyms") or []), "category": item.get("category"),
            "section": item.get("section"), "base_unit": item["base_unit"], "g_per_piece": item.get("g_per_piece"),
            "density_g_per_ml": item.get("density_g_per_ml"), "shelf_life_days": item.get("shelf_life_days"),
            "opened_shelf_life_days": item.get("opened_shelf_life_days"), "freezer_days": item.get("freezer_days"),
            "storage": item.get("storage") or "pantry", "allergens": jdumps(item.get("allergens") or []),
            "animal": item.get("animal"), "kcal": item.get("kcal"), "protein_g": item.get("protein_g"),
            "carbs_g": item.get("carbs_g"), "fat_g": item.get("fat_g"), "nutrient_source": "builtin_approx",
            "price_cents": item.get("price_cents"), "pack_qty": item.get("pack_qty"), "is_staple": int(bool(item.get("is_staple"))),
        }
        conn.execute(
            f"INSERT OR IGNORE INTO ingredient (id, {', '.join(values)}, is_builtin, created_at, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in values)}, 1, ?, ?)",
            [new_id("ing")] + list(values.values()) + [now, now],
        )
    return len(SEED)


def ingredient_row(row: sqlite3.Row | dict) -> dict[str, Any]:
    item = dict(row)
    item["synonyms"] = jloads(item.get("synonyms"), [])
    item["allergens"] = jloads(item.get("allergens"), [])
    return item


class Catalog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._terms: list[tuple[str, str]] | None = None
        self._stamp: tuple | None = None

    def terms(self) -> list[tuple[str, str]]:
        stamp = self.conn.execute("SELECT count(*), max(updated_at) FROM ingredient").fetchone()
        stamp = tuple(stamp)
        if self._terms is None or stamp != self._stamp:
            terms: list[tuple[str, str]] = []
            for row in self.conn.execute("SELECT id, name, synonyms FROM ingredient"):
                for term in [row["name"], *jloads(row["synonyms"], [])]:
                    terms.append((norm(term), row["id"]))
            self._terms, self._stamp = terms, stamp
        return self._terms

    def get(self, ingredient_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM ingredient WHERE id = ?", (ingredient_id,)).fetchone()
        if row is None:
            raise KitchenError("not_found", "Zutat nicht gefunden.", {"ingredient_id": ingredient_id})
        return ingredient_row(row)

    def candidates(self, name: str) -> tuple[str | None, list[str]]:
        """(exact or head-noun match, other plausible matches)."""
        q = norm(name)
        terms = self.terms()
        exact = {t: i for t, i in terms}
        for v in variants(q):
            if v in exact:
                return exact[v], []
        # Head noun: "bio hähnchenbrust" -> "hähnchenbrust"; longest suffix term wins.
        # Head noun: "bio hähnchenbrust" -> "hähnchenbrust"; longest suffix term wins. A food word in front of the head
        # makes it a different product ("haselnussmilch" is not Milch, "sojajoghurt" is not Joghurt).
        profile: dict[str, tuple] = {}
        for row in self.conn.execute("SELECT id, animal, allergens FROM ingredient"):
            profile[row["id"]] = (row["animal"], tuple(sorted(jloads(row["allergens"], []))))
        known = [(fold(t), i) for t, i in terms if len(t) >= 4 and " " not in t]
        best: tuple[int, str] | None = None
        for v in variants(q):
            for term, ing_id in terms:
                if len(term) >= 4 and v.endswith(term) and (best is None or len(term) > best[0]):
                    prefix = v[: -len(term)].strip()
                    last = fold(prefix.split()[-1]) if prefix.split() else ""
                    if len(last) >= 4 and not v.endswith(" " + term) and any(
                            (fk.startswith(last) or last.startswith(fk) or last.endswith(fk)) and profile.get(ki) != profile.get(ing_id)
                            for fk, ki in known):
                        continue  # the modifier changes diet class or allergens: a different product
                    best = (len(term), ing_id)
        if best:
            return best[1], []
        # The query is part of known names: "sahne" -> Schlagsahne, Kochsahne ...
        containing = sorted({i for t, i in terms if len(q) >= 3 and any(v in t for v in variants(q))})
        if len(containing) == 1:
            return containing[0], []
        if containing:
            return None, containing
        # A known name followed only by a cut word: "putenbrustfilet" -> Putenbrust, "walnusskerne" -> Walnüsse.
        inner = max(((len(t), i) for t, i in terms if len(t) >= 5 and any(v.startswith(t) and only_cuts(v[len(t):]) for v in variants(q))),
                    default=None)
        if inner:
            return inner[1], []
        return None, []

    def resolve(self, name: str, *, create: bool = True, details: dict[str, Any] | None = None,
                kind_hint: str | None = None) -> dict[str, Any]:
        found, options = self.candidates(name)
        if found:
            ing = self.get(found)
            if details:
                self._fill_unknown(ing, details)
                ing = self.get(found)
            return ing
        if options and not (details and details.get("create_new")):
            names = sorted(self.get(i)["name"] for i in options)[:8]
            raise KitchenError(
                "needs_clarification", f"„{name}“ ist mehrdeutig.", {"input": name, "candidates": names},
                ask_user=[{"question": f"Welche Zutat ist mit „{name}“ gemeint?", "options": names}],
            )
        if not create:
            raise KitchenError("not_found", f"Zutat „{name}“ ist unbekannt.", {"input": name})
        return self.create(name, details or {}, kind_hint)

    def create(self, name: str, details: dict[str, Any], kind_hint: str | None) -> dict[str, Any]:
        clean = " ".join(name.strip().split())
        clean = clean[0].upper() + clean[1:]
        if details.get("non_food"):
            return self._create_supply(clean, details)
        base = details.get("base_unit") or {"ml": "ml", "pc": "pc"}.get(kind_hint or "", "g")
        if base == "pc" and details.get("g_per_piece") is None and kind_hint == "pc":
            base = "pc"
        values = {
            "name": clean, "synonyms": jdumps(details.get("synonyms") or []),
            "category": details.get("category"), "section": details.get("section") or "Drogerie & Sonstiges",
            "base_unit": base, "g_per_piece": details.get("g_per_piece"), "density_g_per_ml": details.get("density_g_per_ml"),
            "shelf_life_days": details.get("shelf_life_days"), "opened_shelf_life_days": details.get("shelf_life_days"),
            "freezer_days": details.get("freezer_days"), "storage": storage_code(details.get("storage")) or "pantry",
            "allergens": jdumps(_allergen_codes(details.get("allergens") or [])), "animal": details.get("animal"),
            "kcal": details.get("kcal"), "protein_g": details.get("protein_g"), "carbs_g": details.get("carbs_g"),
            "fat_g": details.get("fat_g"), "nutrient_source": "agent_estimate" if details.get("kcal") is not None else None,
            "price_cents": details.get("price_cents"), "pack_qty": details.get("pack_qty"),
            "is_staple": int(bool(details.get("is_staple"))),
        }
        known = int(details.get("allergens") is not None and details.get("animal", "unset") != "unset")
        now = utc_now()
        ing_id = new_id("ing")
        self.conn.execute(
            f"INSERT INTO ingredient (id, {', '.join(values)}, details_known, created_at, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in values)}, ?, ?, ?)",
            [ing_id] + list(values.values()) + [known, now, now],
        )
        return self.get(ing_id)

    def _create_supply(self, name: str, details: dict[str, Any]) -> dict[str, Any]:
        now, ing_id = utc_now(), new_id("ing")
        self.conn.execute(
            "INSERT INTO ingredient (id, name, synonyms, category, section, base_unit, pack_qty, storage, allergens, price_cents, "
            "is_supply, restock_days, restock_per_person, details_known, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'Drogerie & Sonstiges', 'pc', 1, 'pantry', '[]', ?, 1, ?, ?, 1, ?, ?)",
            (ing_id, name, jdumps(details.get("synonyms") or []), details.get("category") or "Haushalt",
             details.get("price_cents"), details.get("lasts_days") or 30, int(details.get("per_person", True)), now, now))
        return self.get(ing_id)

    def _fill_unknown(self, ing: dict[str, Any], details: dict[str, Any]) -> None:
        """Agent-provided facts fill gaps of non-builtin ingredients; builtin values stay."""
        updates: dict[str, Any] = {}
        for key in ("category", "g_per_piece", "density_g_per_ml", "shelf_life_days", "animal",
                    "kcal", "protein_g", "carbs_g", "fat_g", "pack_qty"):
            if details.get(key) is not None and ing.get(key) is None:
                updates[key] = details[key]
        if storage_code(details.get("storage")) and not ing["is_builtin"]:
            updates["storage"] = storage_code(details.get("storage"))
        if details.get("allergens") is not None and not ing["is_builtin"] and not ing["details_known"]:
            updates["allergens"] = jdumps(_allergen_codes(details["allergens"]))
            updates["details_known"] = 1
        if "kcal" in updates:
            updates["nutrient_source"] = "agent_estimate"
        if updates:
            updates["updated_at"] = utc_now()
            self.conn.execute(
                f"UPDATE ingredient SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                list(updates.values()) + [ing["id"]],
            )


def _allergen_codes(values: list[str]) -> list[str]:
    codes = []
    for value in values:
        code = ALLERGEN_WORDS.get(norm(value), norm(value))
        if code not in codes:
            codes.append(code)
    return codes


def parse_restriction(kind: str, value: str) -> tuple[str, str]:
    """Normalize what the user said into (kind, value) the rule check understands."""
    v = norm(value)
    if v in DIETS:
        return "diet", DIETS[v]
    if v in ALLERGEN_WORDS and kind in ("allergy", "intolerance"):
        return kind, ALLERGEN_WORDS[v]
    if kind in ("allergy", "intolerance") and v in ALLERGEN_WORDS.values():
        return kind, v
    if kind == "diet":
        raise KitchenError("validation_error", f"Unbekannte Ernährungsform „{value}“.",
                           {"known": ["vegetarisch", "vegan", "pescetarisch", "kein Schwein"]})
    # Anything else is an exclusion of an ingredient or category by name.
    if kind in ("allergy", "intolerance"):
        return kind, v
    return "exclude", v


def violations(ing: dict[str, Any], restrictions: list[dict[str, Any]]) -> list[str]:
    """Human-readable reasons why an ingredient breaks a hard rule."""
    if ing.get("is_supply"):
        return []
    reasons = []
    names = {norm(ing["name"]), *(norm(s) for s in ing.get("synonyms") or [])}
    category = norm(ing.get("category") or "")
    for r in restrictions:
        kind, value = r["kind"], r["value"]
        if kind in ("allergy", "intolerance"):
            if value in ing["allergens"]:
                reasons.append(f"{ing['name']} enthält {value} ({'Allergie' if kind == 'allergy' else 'Unverträglichkeit'})")
            elif value == "milk" and "lactose" in ing["allergens"]:
                reasons.append(f"{ing['name']} enthält Milch")
            elif any(value == n or value in n.split() for n in names):
                reasons.append(f"{ing['name']} ist ausgeschlossen ({value})")
        elif kind == "diet":
            if value == "no_pork":
                if category == "schwein" or any("schwein" in n or "speck" in n for n in names):
                    reasons.append(f"{ing['name']} ist Schwein")
            elif ing.get("animal") in DIET_FORBIDS.get(value, set()):
                reasons.append(f"{ing['name']} ist nicht {value}")
        elif kind == "exclude":
            if value == category or any(value == n or n.endswith(value) for n in names):
                reasons.append(f"{ing['name']} ist ausgeschlossen ({value})")
    return reasons


def unknown_details(ing: dict[str, Any], restrictions: list[dict[str, Any]]) -> bool:
    return bool(restrictions) and not ing["is_builtin"] and not ing["details_known"] and not ing.get("is_supply")
