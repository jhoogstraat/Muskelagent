#!/usr/bin/env python3
"""Export current MuskelMarkt supermarket offers as stable, agent-friendly JSON.

The page already combines offers from Aldi, Edeka, Kaufland, Lidl, Rewe, etc.
This extractor keeps its published product data intact and normalizes the parts
an nutrition/recommendation agent needs for deterministic filtering.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urljoin


DEFAULT_URL = "https://www.muskelmarkt.de/app1.php"
SCHEMA_VERSION = "1.0"


def decimal(value: str | None) -> float | None:
    if not value:
        return None
    try:
        # HTML data attributes use JSON-style decimal points ("20.00"), while
        # displayed German values use commas ("4,49"). A dot is only a
        # thousands separator when a comma is present.
        cleaned = value.strip()
        if "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        return float(cleaned)
    except ValueError:
        return None


def iso_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return value  # Preserve an unexpected source value instead of discarding it.


def unit_price(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*€\s*/\s*([A-Za-z]+)", value)
    if not match:
        return {"display": value}
    return {"amount_eur": decimal(match.group(1)), "unit": match.group(2).lower(), "display": value}


def protein_per_euro(text: str) -> float | None:
    match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*g\s+Prot\./€", text)
    return decimal(match.group(1)) if match else None


class OfferCards(HTMLParser):
    """Minimal dependency-free parser for the offer-card data contract."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.offers: list[dict[str, Any]] = []
        self._card: dict[str, Any] | None = None
        self._article_depth = 0
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "article" and "offer-card" in classes and self._card is None:
            self._card = attributes
            self._article_depth = 1
            self._text = []
            return
        if self._card is not None and tag == "article":
            self._article_depth += 1
        if self._card is not None and tag == "img" and attributes.get("data-large"):
            self._card["_image"] = attributes["data-large"]
        if self._card is not None and tag == "button" and attributes.get("data-suggestion"):
            self._card["_suggestion"] = attributes["data-suggestion"]

    def handle_endtag(self, tag: str) -> None:
        if self._card is not None and tag == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                self._card["_text"] = " ".join("".join(self._text).split())
                self.offers.append(self._card)
                self._card = None

    def handle_data(self, data: str) -> None:
        if self._card is not None:
            self._text.append(data)


def as_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value == "1"


def normalize(card: dict[str, Any], source_url: str) -> dict[str, Any]:
    text = card.get("_text", "")
    categories = [x.strip() for x in card.get("data-categories", "").split(",") if x.strip()]
    return {
        "id": card.get("data-id"),
        "supermarket": card.get("data-store"),
        "name": card.get("data-name"),
        "price": {
            "current_eur": decimal(card.get("data-price")),
            "regular_eur": decimal(card.get("data-normal-price")),
            "unit_price": unit_price(next(iter(re.findall(r"[0-9]+(?:[.,][0-9]+)?\s*€\s*/\s*[A-Za-z]+", text)), None)),
        },
        "offer_period": {
            "starts_at": iso_datetime(card.get("data-offer-start")),
            "ends_at": iso_datetime(card.get("data-offer-end")),
        },
        "nutrition_per_100g": {
            "protein_g": decimal(card.get("data-protein")),
            "carbohydrates_g": decimal(card.get("data-carbs")),
            "fat_g": decimal(card.get("data-fat")),
            "energy_kcal": decimal(card.get("data-kcal")),
        },
        "agent_features": {
            "protein_per_euro_g": protein_per_euro(text),
            "categories": categories,
            "can_stock_up": as_bool(card.get("data-can-stock-up")),
            "shelf_life_days": decimal(card.get("data-shelf-life")),
            "shelf_life_after_expiry_days": decimal(card.get("data-shelf-life-after-expiry")),
            "storage_instructions": card.get("data-storage-instructions") or None,
            "usage_hint": card.get("_suggestion") or None,
        },
        "source": {
            "url": source_url,
            "catalog_page": decimal(card.get("data-catalog-page")),
            "image_url": urljoin(source_url, card["_image"]) if card.get("_image") else None,
        },
    }


def download(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; meal-offer-export/1.0)"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode(response.headers.get_content_charset() or "utf-8")
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(f"Could not download {url}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Offer page to extract")
    parser.add_argument("--store", action="append", help="Only export this supermarket (repeatable, exact name)")
    parser.add_argument("--input-html", type=Path, help="Parse a saved page instead of downloading it")
    parser.add_argument("--output", type=Path, help="Write JSON to this file (stdout by default)")
    args = parser.parse_args()

    html = args.input_html.read_text(encoding="utf-8") if args.input_html else download(args.url)
    cards = OfferCards()
    cards.feed(html)
    offers = [normalize(card, args.url) for card in cards.offers]
    if args.store:
        wanted = set(args.store)
        offers = [offer for offer in offers if offer["supermarket"] in wanted]

    export = {
        "schema_version": SCHEMA_VERSION,
        "source": {"name": "MuskelMarkt", "url": args.url},
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "offer_count": len(offers),
        "offers": offers,
    }
    rendered = json.dumps(export, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
