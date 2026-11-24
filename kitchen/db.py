"""SQLite storage: schema, migrations, change log for undo, clock and ids."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

# Tables whose changes are recorded for undo. Every one has a TEXT/INTEGER primary key `id`.
TRACKED_TABLES = (
    "household", "member", "restriction", "nutrition_target", "preference", "store",
    "ingredient", "lot", "reservation", "recipe", "recipe_ingredient", "recipe_rating",
    "meal_plan", "meal_slot", "shopping_trip", "shopping_item", "purchase", "purchase_line",
    "price_observation", "cook_log",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS household (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT 'Mein Haushalt',
    timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
    currency TEXT NOT NULL DEFAULT 'EUR',
    max_stores_per_trip INTEGER NOT NULL DEFAULT 2,
    weekly_budget_cents INTEGER,
    visit_cost_cents INTEGER NOT NULL DEFAULT 150,
    default_meal_types TEXT NOT NULL DEFAULT '["dinner"]',
    stores_asked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    portion_factor REAL NOT NULL DEFAULT 1.0, is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restriction (
    id TEXT PRIMARY KEY, member_id TEXT REFERENCES member(id),
    kind TEXT NOT NULL CHECK (kind IN ('allergy','intolerance','diet','exclude')),
    value TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nutrition_target (
    id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES member(id),
    nutrient TEXT NOT NULL, daily_min REAL, daily_max REAL,
    UNIQUE (member_id, nutrient)
);

CREATE TABLE IF NOT EXISTS preference (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL COLLATE NOCASE,
    weight INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS store (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    stance TEXT NOT NULL DEFAULT 'preferred' CHECK (stance IN ('preferred','neutral','avoid')),
    visit_cost_cents INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingredient (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    synonyms TEXT NOT NULL DEFAULT '[]', category TEXT, section TEXT,
    base_unit TEXT NOT NULL CHECK (base_unit IN ('g','ml','pc')),
    g_per_piece REAL, density_g_per_ml REAL,
    shelf_life_days REAL, opened_shelf_life_days REAL, freezer_days REAL,
    storage TEXT NOT NULL DEFAULT 'pantry', allergens TEXT NOT NULL DEFAULT '[]', animal TEXT,
    kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, nutrient_source TEXT,
    price_cents REAL, pack_qty REAL, is_staple INTEGER NOT NULL DEFAULT 0,
    is_builtin INTEGER NOT NULL DEFAULT 0, details_known INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lot (
    id TEXT PRIMARY KEY, ingredient_id TEXT REFERENCES ingredient(id),
    kind TEXT NOT NULL DEFAULT 'raw' CHECK (kind IN ('raw','prepared')),
    recipe_id TEXT REFERENCES recipe(id), label TEXT,
    qty REAL NOT NULL DEFAULT 0 CHECK (qty >= 0), staple_level TEXT,
    location TEXT NOT NULL DEFAULT 'pantry',
    status TEXT NOT NULL DEFAULT 'sealed' CHECK (status IN ('sealed','opened','depleted','discarded')),
    best_before_on TEXT, is_estimated_bb INTEGER NOT NULL DEFAULT 1,
    opened_on TEXT, added_on TEXT NOT NULL, last_verified_on TEXT,
    purchase_line_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservation (
    id TEXT PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES meal_slot(id),
    ingredient_id TEXT NOT NULL REFERENCES ingredient(id),
    lot_id TEXT REFERENCES lot(id), shopping_item_id TEXT REFERENCES shopping_item(id),
    qty REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','consumed','released'))
);

CREATE TABLE IF NOT EXISTS recipe (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    prep_min INTEGER, cook_min INTEGER, tags TEXT NOT NULL DEFAULT '[]', instructions TEXT,
    is_meal_prep INTEGER NOT NULL DEFAULT 0, cooked_shelf_life_days REAL NOT NULL DEFAULT 3,
    source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_ingredient (
    id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL REFERENCES recipe(id),
    ingredient_id TEXT NOT NULL REFERENCES ingredient(id),
    qty_per_serving REAL NOT NULL, is_optional INTEGER NOT NULL DEFAULT 0, note TEXT,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recipe_rating (
    id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL REFERENCES recipe(id), member_id TEXT,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5), comment TEXT, rated_on TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meal_plan (
    id TEXT PRIMARY KEY, start_on TEXT NOT NULL, end_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','confirmed','completed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1, request TEXT NOT NULL, proposal TEXT NOT NULL,
    confirmed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meal_slot (
    id TEXT PRIMARY KEY, plan_id TEXT REFERENCES meal_plan(id), date_on TEXT NOT NULL,
    meal_type TEXT NOT NULL, recipe_id TEXT REFERENCES recipe(id), lot_id TEXT REFERENCES lot(id),
    servings REAL NOT NULL, status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','cooked','skipped','cancelled')),
    note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shopping_trip (
    id TEXT PRIMARY KEY, plan_id TEXT REFERENCES meal_plan(id), store_id TEXT REFERENCES store(id),
    store_name TEXT, planned_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','purchased','cancelled')),
    estimated_total_cents INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shopping_item (
    id TEXT PRIMARY KEY, trip_id TEXT NOT NULL REFERENCES shopping_trip(id),
    ingredient_id TEXT NOT NULL REFERENCES ingredient(id),
    need_qty REAL NOT NULL, purchase_qty REAL NOT NULL, surplus_qty REAL NOT NULL DEFAULT 0,
    pack_count INTEGER, pack_qty REAL, est_price_cents INTEGER,
    price_source TEXT, price_confidence TEXT, offer_snapshot TEXT, rationale TEXT,
    check_at_home INTEGER NOT NULL DEFAULT 0, is_manual INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','bought','missing','removed'))
);

CREATE TABLE IF NOT EXISTS purchase (
    id TEXT PRIMARY KEY, trip_id TEXT REFERENCES shopping_trip(id), store_name TEXT,
    purchased_on TEXT NOT NULL, total_cents INTEGER, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_line (
    id TEXT PRIMARY KEY, purchase_id TEXT NOT NULL REFERENCES purchase(id),
    shopping_item_id TEXT, ingredient_id TEXT NOT NULL REFERENCES ingredient(id),
    qty REAL NOT NULL, price_cents INTEGER, price_is_estimate INTEGER NOT NULL DEFAULT 0, raw_label TEXT
);

CREATE TABLE IF NOT EXISTS price_observation (
    id TEXT PRIMARY KEY, store_name TEXT, ingredient_id TEXT NOT NULL REFERENCES ingredient(id),
    qty REAL NOT NULL, price_cents INTEGER NOT NULL, is_offer INTEGER NOT NULL DEFAULT 0, observed_on TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cook_log (
    id TEXT PRIMARY KEY, slot_id TEXT, recipe_id TEXT, label TEXT, cooked_on TEXT NOT NULL,
    meal_type TEXT, servings_cooked REAL, servings_eaten REAL, leftover_lot_id TEXT,
    consumed TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    id TEXT PRIMARY KEY, tool TEXT NOT NULL, args TEXT NOT NULL, args_hash TEXT NOT NULL,
    result TEXT, summary TEXT, occurred_at TEXT NOT NULL, occurred_ts REAL NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS event_hash ON event (args_hash, occurred_ts);

CREATE TABLE IF NOT EXISTS change_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, tbl TEXT NOT NULL,
    op TEXT NOT NULL, row_id TEXT NOT NULL, old TEXT
);
CREATE TABLE IF NOT EXISTS current_event (k INTEGER PRIMARY KEY CHECK (k = 1), event_id TEXT);

CREATE TABLE IF NOT EXISTS offer_cache (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, store TEXT NOT NULL, name TEXT NOT NULL,
    price_cents INTEGER, regular_price_cents INTEGER, unit_price_cents INTEGER, unit_price_unit TEXT,
    pack_amount REAL, pack_unit TEXT, valid_from TEXT, valid_until TEXT,
    nutrition TEXT, categories TEXT, can_stock_up INTEGER, shelf_life_days REAL,
    storage_instructions TEXT, usage_hint TEXT, fetched_at TEXT NOT NULL
);
"""


class KitchenError(Exception):
    """Domain error returned to the agent with a stable code."""

    def __init__(self, code: str, message: str, details: Any = None, ask_user: list | None = None,
                 next_actions: list | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details
        self.ask_user, self.next_actions = ask_user or [], next_actions or []


def new_id(prefix: str) -> str:
    # Time-ordered, readable, unique enough for one household.
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Clock:
    """Local calendar for the household. KITCHEN_TODAY pins the date for end-to-end tests."""

    def __init__(self, tz: str = "Europe/Berlin") -> None:
        self.tz = ZoneInfo(tz)

    def now(self) -> dt.datetime:
        pinned = os.environ.get("KITCHEN_TODAY")
        if pinned:
            hour = int(os.environ.get("KITCHEN_HOUR", "10"))
            return dt.datetime.combine(dt.date.fromisoformat(pinned), dt.time(hour), self.tz)
        return dt.datetime.now(self.tz)

    def today(self) -> dt.date:
        return self.now().date()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA)
    for table in TRACKED_TABLES:
        _install_change_triggers(conn, table)
    now = utc_now()
    conn.execute("INSERT OR IGNORE INTO household (id, created_at, updated_at) VALUES (1, ?, ?)", (now, now))
    conn.execute("INSERT OR IGNORE INTO current_event (k, event_id) VALUES (1, NULL)")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _install_change_triggers(conn: sqlite3.Connection, table: str) -> None:
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    old_json = "json_object(" + ", ".join(f"'{c}', OLD.{c}" for c in cols) + ")"
    guard = "(SELECT event_id FROM current_event WHERE k = 1) IS NOT NULL"
    ev = "(SELECT event_id FROM current_event WHERE k = 1)"
    conn.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS {table}_log_i AFTER INSERT ON {table} WHEN {guard}
        BEGIN INSERT INTO change_log (event_id, tbl, op, row_id, old) VALUES ({ev}, '{table}', 'I', NEW.id, NULL); END;
        CREATE TRIGGER IF NOT EXISTS {table}_log_u AFTER UPDATE ON {table} WHEN {guard}
        BEGIN INSERT INTO change_log (event_id, tbl, op, row_id, old) VALUES ({ev}, '{table}', 'U', OLD.id, {old_json}); END;
        CREATE TRIGGER IF NOT EXISTS {table}_log_d AFTER DELETE ON {table} WHEN {guard}
        BEGIN INSERT INTO change_log (event_id, tbl, op, row_id, old) VALUES ({ev}, '{table}', 'D', OLD.id, {old_json}); END;
    """)


def revert_event(conn: sqlite3.Connection, event_id: str) -> int:
    """Apply the inverse of every change an event made, newest first. Caller holds the transaction."""
    rows = conn.execute("SELECT * FROM change_log WHERE event_id = ? ORDER BY seq DESC", (event_id,)).fetchall()
    conn.execute("PRAGMA defer_foreign_keys = ON")
    for row in rows:
        table = row["tbl"]
        if table not in TRACKED_TABLES:
            continue
        if row["op"] == "I":
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["row_id"],))
            continue
        old = json.loads(row["old"])
        cols = list(old)
        if row["op"] == "U":
            assignments = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [old[c] for c in cols] + [row["row_id"]])
        else:
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", [old[c] for c in cols])
    return len(rows)


def jloads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def jdumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
