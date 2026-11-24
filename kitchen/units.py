"""Parse amounts like "500 g Hähnchen", "2 Zwiebeln", "1 EL Öl" and convert to base units."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .db import KitchenError

# unit word -> (kind, factor). kind: g | ml | pc | pack
UNIT_ALIASES: dict[str, tuple[str, float]] = {}


def _alias(kind: str, factor: float, *words: str) -> None:
    for word in words:
        UNIT_ALIASES[word] = (kind, factor)


_alias("g", 1, "g", "gr", "gramm", "gram", "grams")
_alias("g", 1000, "kg", "kilo", "kilogramm")
_alias("g", 0.5, "prise", "prisen", "msp", "messerspitze")
_alias("g", 30, "handvoll")
_alias("ml", 1, "ml", "milliliter")
_alias("ml", 10, "cl")
_alias("ml", 100, "dl")
_alias("ml", 1000, "l", "liter", "ltr")
_alias("ml", 15, "el", "esslöffel", "essloeffel", "tbsp")
_alias("ml", 5, "tl", "teelöffel", "teeloeffel", "tsp")
_alias("ml", 240, "tasse", "tassen", "cup", "cups")
_alias("ml", 20, "schuss", "spritzer")
_alias("pc", 1, "stück", "stueck", "stk", "st", "pc", "pcs", "piece", "pieces", "x", "zehe", "zehen",
       "scheibe", "scheiben", "knolle", "knollen", "kopf", "köpfe", "bund", "bündel", "stange", "stangen", "blatt", "blätter")
# Words that describe what is inside one pack of a household supply ("8 Rollen", "40 Tabs", "20 WL"): one pack.
_alias("content", 1, "rolle", "rollen", "tab", "tabs", "tabletten", "waschladung", "waschladungen", "wl", "beutelchen", "pads",
       "kapseln", "pods")
_alias("pack", 1, "packung", "packungen", "pck", "pckg", "pkg", "pack", "packs", "dose", "dosen", "becher",
       "glas", "gläser", "flasche", "flaschen", "beutel", "tüte", "tüten", "netz", "schale", "schalen", "tetrapak")

FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}
NUMBER = r"(?:\d+\s+\d+\s*/\s*\d+|\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[½¼¾⅓⅔])"
MULTIPLIER_RE = re.compile(r"(?P<count>\d+)\s*[x×]\s*(?=\d)")
THOUSANDS_RE = re.compile(r"(?<![\d.,])(\d{1,3})\.(\d{3})(?![\d.,])")
AMOUNT_RE = re.compile(rf"^\s*(?:ca\.?\s*|etwa\s*)?(?P<num>{NUMBER})(?:\s*(?:-|bis)\s*(?P<num2>{NUMBER}))?\s*(?P<unit>[A-Za-zÄÖÜäöüß.]+)?\s*(?P<rest>.*)$")
TRAILING_RE = re.compile(rf"^(?P<rest>.*?[A-Za-zÄÖÜäöüß)])[\s,:]+(?P<num>{NUMBER})\s*(?P<unit>[A-Za-zÄÖÜäöüß.]+)?\s*$")
SIZE_RE = re.compile(r"(?:(?P<count>\d+)\s*[x×]\s*)?(?P<size>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g|ml|cl|l)\b", re.IGNORECASE)
PIECES_RE = re.compile(r"\b(?P<count>\d+)\s*(?:er|stück|stk)\b", re.IGNORECASE)


def to_number(text: str) -> float:
    text = text.strip()
    if text in FRACTIONS:
        return FRACTIONS[text]
    whole = 0.0
    if " " in text and "/" in text:  # "1 1/2"
        first, text = text.split(None, 1)
        whole = float(first)
    if "/" in text:
        a, b = text.split("/", 1)
        if float(b.replace(",", ".")) == 0:
            raise KitchenError("validation_error", "Menge mit Division durch null.")
        return whole + float(a.replace(",", ".")) / float(b.replace(",", "."))
    return whole + float(text.replace(",", "."))


@dataclass
class Amount:
    name: str
    value: float | None  # None = no amount given ("Salz", "Feta")
    kind: str | None  # g | ml | pc | pack | None
    raw: str


def parse_item(text: str, qty: float | None = None, unit: str | None = None) -> Amount:
    """Split free text into amount and ingredient name. Explicit qty/unit win over the text."""
    raw = text.strip()
    multiplier = 1.0
    text = THOUSANDS_RE.sub(r"\1\2", raw)  # "1.000 g" -> "1000 g"
    match = MULTIPLIER_RE.search(text)
    if match and qty is None:  # "2 x 200 g Hähnchen" / "Hähnchen 2 x 200 g"
        multiplier = float(match.group("count"))
        text = (text[: match.start()] + text[match.end():]).strip()
    raw_text, raw = raw, text
    name = raw
    value: float | None = None
    kind: str | None = None
    if qty is None:
        match = AMOUNT_RE.match(raw)
        trailing = None if match else TRAILING_RE.match(raw)
        if trailing and (trailing.group("unit") or "").rstrip(".").casefold() in UNIT_ALIASES | {"": None}:
            kind, factor = UNIT_ALIASES.get((trailing.group("unit") or "").rstrip(".").casefold(), ("pc", 1))
            value = 1.0 if kind == "content" else to_number(trailing.group("num")) * factor
            name = trailing.group("rest")
        if match:
            value = to_number(match.group("num"))
            if match.group("num2"):
                value = (value + to_number(match.group("num2"))) / 2
            unit_word = (match.group("unit") or "").rstrip(".").casefold()
            rest = match.group("rest").strip()
            if unit_word in UNIT_ALIASES:
                kind, factor = UNIT_ALIASES[unit_word]
                value = 1.0 if kind == "content" else value * factor
                name = rest
            else:
                # "2 Zwiebeln": the "unit" is the first word of the name.
                kind = "pc"
                name = f"{match.group('unit') or ''} {rest}".strip()
    else:
        value = float(qty)
        if unit:
            unit_word = unit.strip().rstrip(".").casefold()
            if unit_word not in UNIT_ALIASES:
                raise KitchenError("validation_error", f"Unbekannte Einheit „{unit}“.",
                                   {"known_units": sorted({"g", "kg", "ml", "l", "EL", "TL", "Stück", "Packung", "Dose", "Becher", "Prise"})})
            kind, factor = UNIT_ALIASES[unit_word]
            value *= factor
        else:
            kind = "pc"
    if value is not None:
        value *= multiplier
    if value is not None and (not math.isfinite(value) or value < 0):
        raise KitchenError("validation_error", f"Ungültige Menge in „{raw_text}“.")
    name = re.sub(r"^(von|vom|der|die|das)\s+", "", name, flags=re.IGNORECASE).strip(" ,.;:")
    if not name:
        raise KitchenError("validation_error", f"Kein Zutatenname in „{raw}“.")
    return Amount(name=name, value=value, kind=kind, raw=raw_text)


def to_base(value: float, kind: str, ing: dict[str, Any]) -> tuple[float, bool]:
    """Convert a parsed amount into the ingredient's base unit. Returns (qty, is_approximate)."""
    base = ing["base_unit"]
    gpp, density, pack = ing.get("g_per_piece"), ing.get("density_g_per_ml"), ing.get("pack_qty")
    approx = False
    if ing.get("is_supply"):
        # Supplies are counted in packs: "2 Packungen", "3 Klopapier"; a size ("750 ml Spülmittel", "8 Rollen") is one pack.
        return (value, False) if kind in ("pc", "pack") else (max(1.0, value) if kind == "content" else 1.0, True)
    if kind == "content":
        return value * (pack or 1), True
    if kind == "pack":
        if not pack:
            raise KitchenError("unit_mismatch", f"Wie groß ist eine Packung {ing['name']}?",
                               ask_user=[{"question": f"Wie viel ist in einer Packung {ing['name']} (z. B. 500 g)?"}])
        return value * pack, True
    if kind == base:
        return value, False
    # Bring everything to grams first, then to the base unit.
    if kind == "g":
        grams = value
    elif kind == "ml":
        if density is None:
            approx = True
        grams = value * (density or 1.0)
    elif kind == "pc":
        if not gpp:
            raise KitchenError("unit_mismatch", f"{ing['name']} wird in {base} gemessen; wie viel wiegt ein Stück?",
                               {"ingredient": ing["name"], "base_unit": base},
                               ask_user=[{"question": f"Wie viel {base} sind ein Stück {ing['name']}?"}])
        grams, approx = value * gpp, True
    else:
        raise KitchenError("unit_mismatch", f"Unbekannte Mengenart für {ing['name']}.")
    if base == "g":
        return grams, approx
    if base == "ml":
        return grams / (density or 1.0), approx or density is None
    if not gpp:
        raise KitchenError("unit_mismatch", f"{ing['name']} wird in Stück gezählt; wie viel wiegt ein Stück?",
                           ask_user=[{"question": f"Wie viele Stück {ing['name']} sind das?"}])
    return grams / gpp, True


def pack_size(name: str) -> tuple[float, str] | None:
    """Pack size from an offer title: "4x 150g" -> (600, "g"), "0,25l" -> (250, "ml"), "6er" -> (6, "pc")."""
    match = SIZE_RE.search(name)
    if match:
        size = to_number(match.group("size"))
        count = int(match.group("count") or 1)
        unit = match.group("unit").lower()
        factor, base = {"kg": (1000, "g"), "g": (1, "g"), "l": (1000, "ml"), "cl": (10, "ml"), "ml": (1, "ml")}[unit]
        return count * size * factor, base
    match = PIECES_RE.search(name)
    if match:
        return float(match.group("count")), "pc"
    return None


def fmt_qty(qty: float, base_unit: str) -> str:
    """Human German quantity: 1500 g -> "1,5 kg", 3 pc -> "3 Stück"."""
    if qty is None:
        return ""
    if base_unit == "g" and qty >= 1000:
        return f"{_num(qty / 1000)} kg"
    if base_unit == "ml" and qty >= 1000:
        return f"{_num(qty / 1000)} l"
    unit = {"g": "g", "ml": "ml", "pc": "Stück"}[base_unit]
    return f"{_num(qty)} {unit}"


def fmt_packs(qty: float) -> str:
    n = _num(qty)
    return f"{n} Packung" if n == "1" else f"{n} Packungen"


def _num(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.1f}".replace(".", ",") if value < 100 else str(int(round(value)))


def eur(cents: float | None) -> str:
    if cents is None:
        return "?"
    return f"{cents / 100:.2f} €".replace(".", ",")
