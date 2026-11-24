"""Household supplies (toilet paper, dish soap, toothpaste …): counted in packs, never expire, restocked on a learned rhythm.

Per supply the server keeps when it was last bought, how many packs, and how many days one pack lasts this household.
That interval starts from the catalog default (scaled by household size), learns from purchases and from what the user
says ("Klopapier ist alle" = it lasted until today; "hab noch" when it was on the list = it lasts longer), and a user
statement ("eine Packung hält bei uns 3 Wochen") overrides it. Due supplies go onto the next shopping trip by themselves.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .catalog import norm
from .core import KitchenBase
from .db import utc_now
from .units import fmt_packs, fmt_qty

LEVEL_LABEL = {"ok": "da", "low": "knapp", "empty": "leer"}


class SupplyMixin(KitchenBase):
    def qty_label(self, qty: float, ing: dict[str, Any]) -> str:
        return fmt_packs(qty) if ing.get("is_supply") else fmt_qty(qty, ing["base_unit"])

    def _persons(self) -> int:
        return max(1, len(self.members()))

    def _default_days_per_pack(self, ing: dict[str, Any]) -> float:
        days = float(ing.get("restock_days") or 30)
        if ing.get("restock_per_person", 1):
            days /= self._persons() ** 0.8  # two people use a bit less than twice as much
        return max(3.0, round(days, 1))

    def supply_state(self, ing_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM supply_state WHERE id = ?", (ing_id,)).fetchone()
        return dict(row) if row else None

    def _supply_upsert(self, ing: dict[str, Any], **values: Any) -> dict[str, Any]:
        state = self.supply_state(ing["id"])
        values["updated_at"] = utc_now()
        if state is None:
            values.setdefault("days_per_pack", self._default_days_per_pack(ing))
            cols = ["id", *values]
            self.conn.execute(f"INSERT INTO supply_state ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                              [ing["id"], *values.values()])
        else:
            self.conn.execute(f"UPDATE supply_state SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                              [*values.values(), ing["id"]])
        return self.supply_state(ing["id"])  # type: ignore[return-value]

    def supply_due_on(self, state: dict[str, Any] | None) -> dt.date | None:
        if not state or not state["tracking"]:
            return None
        if state["level"] in ("low", "empty"):
            return dt.date.fromisoformat(state["level_on"]) if state["level_on"] else self.today()
        if not state["last_bought_on"]:
            return None
        due = dt.date.fromisoformat(state["last_bought_on"]) + dt.timedelta(days=round(state["packs"] * state["days_per_pack"]))
        if state["snoozed_until"]:
            due = max(due, dt.date.fromisoformat(state["snoozed_until"]))
        return due

    # --- what happened -------------------------------------------------------------------------
    def supply_refill(self, ing: dict[str, Any], packs: float, on: dt.date) -> None:
        """Bought (or otherwise got) packs. Learns how long the previous packs lasted."""
        packs = max(packs, 0.5)
        state = self.supply_state(ing["id"])
        if state is None or not state["last_bought_on"]:
            self._supply_upsert(ing, last_bought_on=on.isoformat(), packs=packs, level="ok", level_on=None, snoozed_until=None, tracking=1)
            return
        dpp, source = state["days_per_pack"], state["days_source"]
        last = dt.date.fromisoformat(state["last_bought_on"])
        end = dt.date.fromisoformat(state["level_on"]) if state["level"] in ("low", "empty") and state["level_on"] else on
        gap = max(0, (end - last).days)
        left = 0.0 if state["level"] == "empty" else max(0.0, state["packs"] - gap / dpp)
        if state["level"] == "low":
            left = min(left, 0.25)
        if left > 0.3:
            packs += left  # bought ahead (offer, stockpiling): the old packs are still there, nothing to learn
        elif source != "user" and gap >= 3:
            observed = gap / max(state["packs"] - left, 0.5)
            observed = min(max(observed, dpp * 0.5), dpp * 2)
            dpp, source = round(0.5 * dpp + 0.5 * observed, 1), "learned"
        self._supply_upsert(ing, last_bought_on=on.isoformat(), packs=round(packs, 2), days_per_pack=dpp, days_source=source,
                            level="ok", level_on=None, snoozed_until=None, tracking=1)

    def supply_count(self, ing: dict[str, Any], packs: float, on: dt.date) -> None:
        """The user counted what is at home ("noch 3 Packungen Klopapier"); not a consumption signal."""
        if packs <= 0:
            self.supply_level(ing, "empty", on)
            return
        self._supply_upsert(ing, last_bought_on=on.isoformat(), packs=packs, level="ok", level_on=None, snoozed_until=None, tracking=1)

    def supply_level(self, ing: dict[str, Any], level: str, on: dt.date) -> None:
        if level == "ok":
            state = self.supply_state(ing["id"])
            if state is None or not state["last_bought_on"]:
                self._supply_upsert(ing, last_bought_on=on.isoformat(), packs=1, level="ok", level_on=None, tracking=1)
            else:
                self.supply_had_enough(ing, on)
            return
        self._supply_upsert(ing, level=level, level_on=on.isoformat(), tracking=1)

    def supply_had_enough(self, ing: dict[str, Any], on: dt.date) -> dt.date:
        """It was due but there was still enough at home: wait a while and stretch the interval a bit."""
        state = self.supply_state(ing["id"]) or self._supply_upsert(ing, last_bought_on=on.isoformat(), packs=1)
        dpp = state["days_per_pack"] if state["days_source"] == "user" else round(state["days_per_pack"] * 1.15, 1)
        until = on + dt.timedelta(days=max(7, round(0.25 * state["packs"] * dpp)))
        self._supply_upsert(ing, days_per_pack=dpp, days_source=state["days_source"] if state["days_source"] == "user" else "learned",
                            level="ok", level_on=None, snoozed_until=until.isoformat())
        return until

    def supply_settings(self, ing: dict[str, Any], *, lasts_days: float | None = None, track: bool | None = None) -> list[str]:
        lines = []
        if lasts_days is not None:
            self._supply_upsert(ing, days_per_pack=float(lasts_days), days_source="user", tracking=1)
            lines.append(f"{ing['name']}: eine Packung hält ca. {round(lasts_days)} Tage")
        if track is not None:
            self._supply_upsert(ing, tracking=int(track))
            lines.append(f"{ing['name']}: {'wird wieder' if track else 'wird nicht mehr'} automatisch nachgekauft")
        return lines

    # --- what is due ---------------------------------------------------------------------------
    def supplies_due(self, until: dt.date) -> list[dict[str, Any]]:
        result = []
        for row in self.conn.execute("SELECT * FROM supply_state WHERE tracking = 1"):
            state = dict(row)
            due = self.supply_due_on(state)
            if due is None or due > until:
                continue
            ing = self.catalog.get(state["id"])
            result.append({"ing": ing, "due_on": due, "level": state["level"], "state": state, "reason": self._supply_reason(state, due)})
        result.sort(key=lambda r: (r["due_on"], r["ing"]["name"]))
        return result

    def _supply_reason(self, state: dict[str, Any], due: dt.date) -> str:
        if state["level"] == "empty":
            return "ist leer"
        if state["level"] == "low":
            return "ist knapp"
        last = dt.date.fromisoformat(state["last_bought_on"])
        weeks = (self.today() - last).days / 7
        ago = f"vor {round(weeks)} Wochen" if weeks >= 1.5 else f"vor {(self.today() - last).days} Tagen"
        when = "müsste bald leer sein" if due >= self.today() else "müsste schon leer sein"
        return f"zuletzt {ago} gekauft, {when}"

    def supply_overview(self, query: str | None = None) -> list[dict[str, Any]]:
        today, rows = self.today(), []
        for row in self.conn.execute("SELECT s.*, i.name FROM supply_state s JOIN ingredient i ON i.id = s.id ORDER BY i.name"):
            state = dict(row)
            if query:
                ing = self.catalog.get(state["id"])
                if not any(norm(query) in norm(label) for label in [ing["name"], *ing["synonyms"]]):
                    continue
            due = self.supply_due_on(state)
            if not state["tracking"]:
                status = "wird nicht nachgekauft"
            elif state["level"] in ("low", "empty"):
                status = LEVEL_LABEL[state["level"]]
            elif due is None:
                status = "unbekannt"
            elif due <= today + dt.timedelta(days=3):
                status = "bald nachkaufen"
            else:
                status = f"reicht noch ca. {(due - today).days} Tage"
            rows.append({"name": state["name"], "status": status, "due": due.isoformat() if due else None,
                         "last_bought": state["last_bought_on"],
                         "pack_lasts_days": round(state["days_per_pack"]),
                         "interval_source": {"default": "Schätzung", "learned": "gelernt", "user": "von dir"}[state["days_source"]]})
        rows.sort(key=lambda r: (r["due"] or "9999", r["name"]))
        return rows

    def low_staples(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM lot WHERE staple_level IN ('low','empty') AND status IN ('sealed','opened')")
        return [{"ing": self.catalog.get(r["ingredient_id"]), "level": r["staple_level"]} for r in rows]
