"""Basket optimizer: pick stores, products and pack counts with minimal total cost.

Pure and deterministic: no database, no network, same input -> same output.
cost(S) = sum_i [ price_i + waste_factor * waste_i + malus_i ] + sum_{s in S} visit_cost_s,  |S| <= k
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Any

ANY = ""  # pseudo store: "available everywhere" (catalog estimate)
MAX_STORES_CONSIDERED = 8


@dataclass(frozen=True)
class Option:
    store: str  # chain name, ANY for estimates
    price_cents: float  # per pack, or per base unit when pack_qty is None (loose / by weight)
    pack_qty: float | None
    source: str  # offer | history | estimate
    confidence: str  # high | medium | low
    label: str
    offer_id: str | None = None
    malus_cents: float = 0.0


@dataclass
class Need:
    key: str
    name: str
    need_qty: float
    base_unit: str
    waste_risk: float  # share of surplus expected to spoil before its next use (0..1)
    unit_value_cents: float  # value of one base unit, for waste
    options: list[Option] = field(default_factory=list)


@dataclass
class Choice:
    key: str
    store: str
    packs: list[tuple[Option, int]]  # option, count (count = qty for loose)
    purchase_qty: float
    cost_cents: float
    price_cents: float
    waste_cents: float
    unpriced: bool


def _combos(opts: list[Option], need: float) -> list[tuple[list[tuple[Option, int]], float, float]]:
    """Pack combinations covering `need` from one store: (packs, qty, price)."""
    result = []
    for o in opts:
        if o.pack_qty is None:
            result.append(([(o, need)], need, o.price_cents * need))
    packs = sorted((o for o in opts if o.pack_qty), key=lambda o: (o.price_cents / o.pack_qty, o.label))[:3]
    if packs:
        ranges = [range(0, math.ceil(need / o.pack_qty) + 1) for o in packs]
        for counts in itertools.product(*ranges):
            qty = sum(c * o.pack_qty for c, o in zip(counts, packs))
            if qty + 1e-9 < need or not any(counts):
                continue
            chosen = [(o, c) for o, c in zip(packs, counts) if c]
            result.append((chosen, qty, sum(c * o.price_cents for o, c in chosen)))
    return result


def _best_at_store(need: Need, store: str, waste_factor: float) -> Choice | None:
    opts = [o for o in need.options if o.store == store]
    if not opts:
        return None
    best: Choice | None = None
    for packs, qty, price in _combos(opts, need.need_qty):
        surplus = max(0.0, qty - need.need_qty)
        waste = surplus * need.unit_value_cents * need.waste_risk
        malus = sum(o.malus_cents for o, _ in packs)
        cost = price + waste_factor * waste + malus
        key = (round(cost, 4), qty, tuple(o.label for o, _ in packs))
        if best is None or key < (round(best.cost_cents, 4), best.purchase_qty, tuple(o.label for o, _ in best.packs)):
            best = Choice(need.key, store, packs, qty, cost, price, waste, False)
    return best


def optimize(needs: list[Need], stores: dict[str, int], *, max_stores: int, waste_factor: float = 1.0,
             preferred: list[str] | None = None) -> dict[str, Any]:
    """stores: allowed chain -> visit cost in cents. Returns best plan plus alternatives by store count."""
    preferred = preferred or []
    per_store: dict[str, dict[str, Choice]] = {}
    candidates = sorted({o.store for n in needs for o in n.options if o.store != ANY and o.store in stores})
    for n in needs:
        for s in candidates + [ANY]:
            choice = _best_at_store(n, s, waste_factor)
            if choice:
                per_store.setdefault(n.key, {})[s] = choice

    # Keep the most useful stores when there are many: preferred ones, then by total savings vs. estimate.
    def savings(store: str) -> float:
        total = 0.0
        for n in needs:
            options = per_store.get(n.key, {})
            if store in options:
                base = options.get(ANY).cost_cents if ANY in options else options[store].cost_cents * 1.3
                total += max(0.0, base - options[store].cost_cents)
        return total

    ranked = sorted(candidates, key=lambda s: (s not in preferred, -savings(s), s))[:MAX_STORES_CONSIDERED]
    subsets: list[tuple[str, ...]] = []
    for size in range(1, max(1, max_stores) + 1):
        subsets.extend(itertools.combinations(sorted(ranked), size))
    if not subsets:
        subsets = [tuple(sorted(s for s in preferred if s in stores))[:max(1, max_stores)] or ()]

    evaluated = []
    for subset in subsets:
        evaluated.append(_evaluate(needs, per_store, subset, stores, preferred))
    evaluated.sort(key=lambda e: (round(e["objective"], 2), len(e["stores"]), e["stores"]))
    best = evaluated[0]
    alternatives = []
    seen_sizes = {len(best["stores"])}
    for e in evaluated[1:]:
        if len(e["stores"]) not in seen_sizes:
            seen_sizes.add(len(e["stores"]))
            alternatives.append({
                "stores": e["stores"], "total_cents": e["total_cents"], "visit_cents": e["visit_cents"],
                "delta_cents": round(e["total_cents"] - best["total_cents"]),
            })
    payload = {"needs": [n.__dict__ | {"options": [o.__dict__ for o in n.options]} for n in needs],
               "stores": stores, "max_stores": max_stores, "waste_factor": waste_factor}
    best["input_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
    best["alternatives"] = alternatives
    return best


def _evaluate(needs: list[Need], per_store: dict[str, dict[str, Choice]], subset: tuple[str, ...],
              stores: dict[str, int], preferred: list[str]) -> dict[str, Any]:
    choices: list[Choice] = []
    for n in needs:
        options = per_store.get(n.key, {})
        pool = [options[s] for s in subset if s in options]
        if ANY in options:
            pool.append(options[ANY])
        if not pool:
            choices.append(Choice(n.key, ANY, [], n.need_qty, 0.0, 0.0, 0.0, True))
            continue
        pool.sort(key=lambda c: (round(c.cost_cents, 4), c.store == ANY, c.store))
        choices.append(dataclasses.replace(pool[0]))
    used = sorted({c.store for c in choices if c.store != ANY})
    # Items "available anywhere" go to the main store of the trip.
    if any(c.store == ANY for c in choices):
        counts = {s: sum(1 for c in choices if c.store == s) for s in subset}
        main = sorted(subset, key=lambda s: (s not in preferred, -counts.get(s, 0), s))[0] if subset else ANY
        for c in choices:
            if c.store == ANY:
                c.store = main
        if main != ANY and main not in used:
            used = sorted(set(used) | {main})
    visit = sum(stores.get(s, 0) for s in used)
    return {
        "stores": used,
        "choices": choices,
        "total_cents": round(sum(c.price_cents for c in choices)),
        "waste_cents": round(sum(c.waste_cents for c in choices)),
        "visit_cents": visit,
        "objective": sum(c.cost_cents for c in choices) + visit,
        "unpriced": [c.key for c in choices if c.unpriced],
    }
