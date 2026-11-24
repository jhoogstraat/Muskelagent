"""Offer cache: import the Muskelmarkt export, refresh it daily, map offers to ingredients."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from .catalog import Catalog, norm, variants
from .db import connect, jdumps, jloads, utc_now
from .units import pack_size

log = logging.getLogger("kitchen.offers")
EXTRACTOR = Path(__file__).resolve().parent.parent / "muskelmarkt_extractor.py"
REFRESH_AFTER = dt.timedelta(hours=20)
# Words in offer titles that are not part of the product ("XXL", brands are left in; longest-term matching copes).
NOISE = {"xxl", "bio", "frische", "frisch", "frisches", "frischer", "fairtrade", "gewürzt", "mild", "aromatisch", "natur",
         "mariniert", "geröstet", "gesalzen", "gesalzene", "ungesalzen", "original", "deutsche", "dtsch", "regional", "premium",
         "extra", "light", "classic", "bbq", "metzgerfrisch", "go", "regio"}
GENERIC = {"steak", "hack", "hackfleisch", "hähnchen", "hühnchen", "schnitzel", "salat", "fleisch", "käse", "wurst", "fisch",
           "gemüse", "obst", "brot", "lachsschinken"}
from .catalog import only_cuts as _only_cuts  # noqa: E402


def _cents(eur: float | None) -> int | None:
    return None if eur is None else int(round(eur * 100))


def import_export(conn: sqlite3.Connection, export: dict[str, Any]) -> int:
    """Replace the Muskelmarkt part of the cache with a fresh export (offers are never kept longer)."""
    fetched = export.get("retrieved_at") or utc_now()
    rows = []
    for offer in export.get("offers", []):
        if not offer.get("name") or not offer.get("supermarket"):
            continue
        price, unit_price = offer.get("price") or {}, (offer.get("price") or {}).get("unit_price") or {}
        size = pack_size(offer["name"])
        unit_cents = _cents(unit_price.get("amount_eur"))
        current = _cents(price.get("current_eur"))
        if size is None and current and unit_cents and unit_price.get("unit") in ("kg", "l"):
            size = (round(current / unit_cents * 1000), "g" if unit_price["unit"] == "kg" else "ml")
        features = offer.get("agent_features") or {}
        period = offer.get("offer_period") or {}
        rows.append((
            f"muskelmarkt:{offer['id']}", "muskelmarkt", offer["supermarket"], offer["name"], current,
            _cents(price.get("regular_eur")), unit_cents, unit_price.get("unit"),
            size[0] if size else None, size[1] if size else None,
            period.get("starts_at"), period.get("ends_at"), jdumps(offer.get("nutrition_per_100g") or {}),
            jdumps(features.get("categories") or []), None if features.get("can_stock_up") is None else int(features["can_stock_up"]),
            features.get("shelf_life_days"), features.get("storage_instructions"), features.get("usage_hint"), fetched,
        ))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM offer_cache WHERE source = 'muskelmarkt'")
        conn.executemany(
            "INSERT OR REPLACE INTO offer_cache (id, source, store, name, price_cents, regular_price_cents, unit_price_cents, "
            "unit_price_unit, pack_amount, pack_unit, valid_from, valid_until, nutrition, categories, can_stock_up, "
            "shelf_life_days, storage_instructions, usage_hint, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('offers_fetched_at', ?)", (fetched,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


class OfferService:
    def __init__(self, conn: sqlite3.Connection, catalog: Catalog, *, db_path: Path, offers_file: Path | None,
                 auto_refresh: bool, tz: Any = None) -> None:
        self.conn, self.catalog, self.db_path = conn, catalog, db_path
        self.tz = tz or dt.timezone(dt.timedelta(hours=1))
        self.offers_file, self.auto_refresh = offers_file, auto_refresh
        self._lock = threading.Lock()
        self._refreshing = False
        self._map: dict[str, str | None] | None = None
        self._map_stamp: tuple | None = None
        self.last_error: str | None = None

    # --- freshness -----------------------------------------------------------------------
    def fetched_at(self) -> dt.datetime | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'offers_fetched_at'").fetchone()
        return dt.datetime.fromisoformat(row[0].replace("Z", "+00:00")) if row else None

    def startup(self) -> None:
        empty = self.conn.execute("SELECT 1 FROM offer_cache LIMIT 1").fetchone() is None
        if self.offers_file and self.offers_file.exists():
            mtime = dt.datetime.fromtimestamp(self.offers_file.stat().st_mtime, dt.timezone.utc)
            fetched = self.fetched_at()
            if empty or fetched is None or mtime > fetched:
                import_export(self.conn, json.loads(self.offers_file.read_text(encoding="utf-8")))
        self.ensure_fresh()

    def ensure_fresh(self) -> None:
        """Kick off a background refresh when the cache is older than a day. Never blocks a tool call."""
        if not self.auto_refresh:
            return
        fetched = self.fetched_at()
        if fetched and dt.datetime.now(dt.timezone.utc) - fetched < REFRESH_AFTER:
            return
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh, name="offer-refresh", daemon=True).start()

    def _refresh(self) -> None:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "offers.json"
                subprocess.run([sys.executable, str(EXTRACTOR), "--output", str(out)], check=True,
                               capture_output=True, timeout=120)
                export = json.loads(out.read_text(encoding="utf-8"))
            if export.get("offers"):
                own = connect(self.db_path)  # own connection: tool calls keep using theirs meanwhile
                try:
                    count = import_export(own, export)
                finally:
                    own.close()
                log.info("offers refreshed: %s", count)
                self.last_error = None
        except Exception as exc:  # The cache stays usable; status reports the problem.
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("offer refresh failed: %s", self.last_error)
        finally:
            self._refreshing = False

    # --- matching ------------------------------------------------------------------------
    def ingredient_for(self, offer_name: str) -> str | None:
        """The one ingredient an offer sells. German noun compounds: the head (last part) names the product,
        a food word in front of it is a modifier ("Hähnchen-Steaks" are not Rindersteak)."""
        title = offer_name.casefold().split(", ")[0]
        title = re.split(r"\s(?:mit|in|im|ohne|zero|oder|aus|für|nach|vom|/)\s", f" {title} ")[0]
        text = " ".join(w for w in norm(title).split() if w not in NOISE and not any(ch.isdigit() for ch in w))
        words = text.split()
        if not words:
            return None
        vegan = any(w.startswith(("vegan", "pflanzlich", "veggie", "plant")) for w in words)
        terms = [(t, i) for t, i in self.catalog.terms() if len(t) >= 2 and t not in GENERIC]
        known = {t for t, _ in self.catalog.terms() if len(t) >= 4}
        best: tuple[float, str] | None = None
        for position, word in enumerate(words):
            for form in variants(word):
                for term, ing_id in terms:
                    score = 0.0
                    if " " in term:
                        score = 200 + len(term) if term in text else 0
                    elif form == term:
                        score = 200 + len(term)
                    elif len(term) >= 4 and form.endswith(term):
                        prefix = form[: -len(term)]
                        if len(prefix) >= 2 and not any(prefix.startswith(k) or prefix.endswith(k) for k in known):
                            score = 100 + len(term)
                    elif len(term) >= 4 and form.startswith(term) and _only_cuts(form[len(term):]):
                        score = 150 + len(term)
                    if score:
                        score += position * 0.5  # later words are the head of the noun phrase
                        if (best is None or score > best[0]) and not (vegan and self.catalog.get(ing_id).get("animal")):
                            best = (score, ing_id)
        return best[1] if best else None

    def mapping(self) -> dict[str, str | None]:
        stamp = (self.conn.execute("SELECT value FROM meta WHERE key = 'offers_fetched_at'").fetchone() or [None])[0], \
            tuple(self.conn.execute("SELECT count(*), max(updated_at) FROM ingredient").fetchone())
        if self._map is None or stamp != self._map_stamp:
            self._map = {r["id"]: self.ingredient_for(r["name"]) for r in self.conn.execute("SELECT id, name FROM offer_cache")}
            self._map_stamp = stamp
        return self._map

    def active_offers(self, on: dt.date | None = None, at: dt.datetime | None = None) -> list[dict[str, Any]]:
        mapping = self.mapping()
        result = []
        for row in self.conn.execute("SELECT * FROM offer_cache"):
            offer = dict(row)
            if not _valid(offer, on=on, at=at, tz=self.tz):
                continue
            offer["ingredient_id"] = mapping.get(offer["id"])
            offer["nutrition"] = jloads(offer["nutrition"], {})
            offer["categories"] = jloads(offer["categories"], [])
            result.append(offer)
        return result

    def for_ingredients(self, ingredient_ids: set[str], on: dt.date) -> dict[str, list[dict[str, Any]]]:
        by_ing: dict[str, list[dict[str, Any]]] = {}
        for offer in self.active_offers(on=on):
            if offer["ingredient_id"] in ingredient_ids:
                by_ing.setdefault(offer["ingredient_id"], []).append(offer)
        return by_ing

    def chains(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT store FROM offer_cache ORDER BY store")]


def _valid(offer: dict[str, Any], *, on: dt.date | None, at: dt.datetime | None, tz: Any) -> bool:
    start = dt.datetime.fromisoformat(offer["valid_from"].replace("Z", "+00:00")) if offer.get("valid_from") else None
    end = dt.datetime.fromisoformat(offer["valid_until"].replace("Z", "+00:00")) if offer.get("valid_until") else None
    if on is not None:
        # Valid on a local calendar day if the offer window overlaps that day in the household's time zone.
        day_start = dt.datetime.combine(on, dt.time(0), tz)
        day_end = day_start + dt.timedelta(days=1)
        return (start is None or start < day_end) and (end is None or end >= day_start)
    at = at or dt.datetime.now(dt.timezone.utc)
    return (start is None or start <= at) and (end is None or at <= end)


def offer_pack_in_base(offer: dict[str, Any], ing: dict[str, Any]) -> float | None:
    """Offer pack size expressed in the ingredient's base unit."""
    amount, unit = offer.get("pack_amount"), offer.get("pack_unit")
    if not amount:
        return None
    base = ing["base_unit"]
    if unit == base:
        return amount
    grams = amount if unit == "g" else amount * (ing.get("density_g_per_ml") or 1.0) if unit == "ml" else \
        amount * ing["g_per_piece"] if unit == "pc" and ing.get("g_per_piece") else None
    if grams is None:
        return None
    if base == "g":
        return grams
    if base == "ml":
        return grams / (ing.get("density_g_per_ml") or 1.0)
    return grams / ing["g_per_piece"] if ing.get("g_per_piece") else None
