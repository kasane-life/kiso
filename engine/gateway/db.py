"""SQLite database for Kasane entities + health tracking data.

All data lives in kasane.db: person profiles, habits, check-ins, focus plans,
AND health tracking (weight, meals, BP, labs, wearables, training).

DB file: data/kasane.db
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_DB_NAME = "kasane.db"
_local = threading.local()


def _db_path() -> Path:
    """Resolve the database file path."""
    # Same data/ directory used by the rest of health-engine
    from mcp_server.tools import PROJECT_ROOT
    return PROJECT_ROOT / "data" / _DB_NAME


def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local SQLite connection with WAL mode and foreign keys.

    Reuses the connection within the same thread. Pass db_path to override
    (useful for tests with tmp directories).
    """
    path = str(db_path) if db_path else str(_db_path())
    conn = getattr(_local, "conn", None)
    cached_path = getattr(_local, "conn_path", None)

    if conn is not None and cached_path == path:
        return conn

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    _local.conn_path = path
    return conn


def close_db():
    """Close the thread-local connection if open."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.conn_path = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    relationship TEXT,
    date_of_birth TEXT,
    biological_sex TEXT,
    conditions_json TEXT DEFAULT '[]',
    medications TEXT,
    family_history_json TEXT DEFAULT '[]',
    health_notes TEXT,
    health_engine_user_id TEXT,
    channel TEXT,
    channel_target TEXT,
    wearables_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS habit (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    title TEXT NOT NULL,
    purpose TEXT,
    category TEXT,
    emoji TEXT,
    anchor TEXT,
    state TEXT DEFAULT 'active',
    sort_order INTEGER DEFAULT 0,
    identity_threshold REAL,
    graduated_at TEXT,
    show_in_today INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS check_in (
    id TEXT PRIMARY KEY,
    habit_id TEXT NOT NULL REFERENCES habit(id),
    date TEXT NOT NULL,
    completed INTEGER DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS check_in_message (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    habit_id TEXT,
    message_text TEXT,
    message_type TEXT,
    action_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS focus_plan (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    generated_at TEXT,
    health_snapshot TEXT,
    reflection TEXT,
    insight TEXT,
    encouragement TEXT,
    primary_action TEXT,
    primary_anchor TEXT,
    primary_reasoning TEXT,
    primary_category TEXT,
    primary_purpose TEXT,
    alternatives_json TEXT DEFAULT '[]',
    risk_assessment TEXT,
    care_team_note TEXT,
    care_team_summary TEXT,
    care_team_suggestions TEXT,
    legacy_habit_title TEXT,
    legacy_habit_purpose TEXT,
    legacy_habit_anchor TEXT,
    legacy_habit_category TEXT,
    legacy_habit_emoji TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS health_measurement (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    type_identifier TEXT NOT NULL,
    value REAL,
    unit TEXT,
    date TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS workout_record (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    workout_type TEXT,
    duration REAL,
    calories REAL,
    date TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_cursor (
    device_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    last_sync_at TEXT NOT NULL,
    PRIMARY KEY (device_id, person_id)
);

-- Health tracking tables (migrated from CSVs)

CREATE TABLE IF NOT EXISTS weight_entry (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    weight_lbs REAL NOT NULL,
    waist_in REAL,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meal_entry (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    meal_num INTEGER,
    time_of_day TEXT,
    description TEXT,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    calories REAL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bp_entry (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    systolic REAL NOT NULL,
    diastolic REAL NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_session (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    rpe REAL,
    duration_min REAL,
    type TEXT,
    name TEXT,
    notes TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strength_set (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES training_session(id),
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    exercise TEXT NOT NULL,
    weight_lbs REAL,
    reps INTEGER,
    rpe REAL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wearable_daily (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    source TEXT,
    rhr REAL,
    hrv REAL,
    hrv_weekly_avg REAL,
    hrv_status TEXT,
    steps INTEGER,
    sleep_hrs REAL,
    deep_sleep_hrs REAL,
    light_sleep_hrs REAL,
    rem_sleep_hrs REAL,
    awake_hrs REAL,
    sleep_start TEXT,
    sleep_end TEXT,
    calories_total REAL,
    calories_active REAL,
    calories_bmr REAL,
    stress_avg INTEGER,
    floors REAL,
    distance_m REAL,
    max_hr INTEGER,
    min_hr INTEGER,
    vo2_max REAL,
    body_battery INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_draw (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    source TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_result (
    id TEXT PRIMARY KEY,
    draw_id TEXT NOT NULL REFERENCES lab_draw(id),
    person_id TEXT NOT NULL REFERENCES person(id),
    marker TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    unit TEXT,
    reference_low REAL,
    reference_high REAL,
    flag TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS habit_log (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    date TEXT NOT NULL,
    habit_name TEXT NOT NULL,
    completed INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wearable_token (
    id TEXT PRIMARY KEY,
    person_id TEXT REFERENCES person(id),
    user_id TEXT NOT NULL,
    service TEXT NOT NULL,
    token_name TEXT NOT NULL,
    token_data BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_wearable_token_user_service_name
    ON wearable_token(user_id, service, token_name);

-- Indexes: existing
CREATE INDEX IF NOT EXISTS idx_habit_person ON habit(person_id);
CREATE INDEX IF NOT EXISTS idx_checkin_habit ON check_in(habit_id);
CREATE INDEX IF NOT EXISTS idx_checkin_date ON check_in(date);
CREATE INDEX IF NOT EXISTS idx_message_person ON check_in_message(person_id);
CREATE INDEX IF NOT EXISTS idx_focus_plan_person ON focus_plan(person_id);
CREATE INDEX IF NOT EXISTS idx_health_measurement_person ON health_measurement(person_id);
CREATE INDEX IF NOT EXISTS idx_workout_record_person ON workout_record(person_id);

-- Indexes: health tracking
CREATE UNIQUE INDEX IF NOT EXISTS idx_weight_person_date ON weight_entry(person_id, date);
CREATE INDEX IF NOT EXISTS idx_meal_person_date ON meal_entry(person_id, date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bp_person_date ON bp_entry(person_id, date);
CREATE INDEX IF NOT EXISTS idx_session_person_date ON training_session(person_id, date);
CREATE INDEX IF NOT EXISTS idx_strength_person_date ON strength_set(person_id, date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wearable_person_date ON wearable_daily(person_id, date);
CREATE INDEX IF NOT EXISTS idx_lab_draw_person ON lab_draw(person_id);
CREATE INDEX IF NOT EXISTS idx_lab_result_person_marker ON lab_result(person_id, marker);
CREATE INDEX IF NOT EXISTS idx_habit_log_person_date ON habit_log(person_id, date);
"""


def init_db(db_path: Path | str | None = None):
    """Create all tables if they don't exist, then run migrations."""
    conn = get_db(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Safe migrations for existing databases. Each is idempotent."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(person)").fetchall()}
    dirty = False
    if "wearables_json" not in cols:
        conn.execute("ALTER TABLE person ADD COLUMN wearables_json TEXT DEFAULT '[]'")
        dirty = True
    if "channel" not in cols:
        conn.execute("ALTER TABLE person ADD COLUMN channel TEXT")
        dirty = True
    if "channel_target" not in cols:
        conn.execute("ALTER TABLE person ADD COLUMN channel_target TEXT")
        dirty = True
    if dirty:
        conn.commit()


# Entity name -> table name mapping (used by sync)
ENTITY_TABLES = {
    "person": "person",
    "habit": "habit",
    "check_in": "check_in",
    "check_in_message": "check_in_message",
    "focus_plan": "focus_plan",
    "health_measurement": "health_measurement",
    "workout_record": "workout_record",
    "weight_entry": "weight_entry",
    "meal_entry": "meal_entry",
    "bp_entry": "bp_entry",
    "training_session": "training_session",
    "strength_set": "strength_set",
    "wearable_daily": "wearable_daily",
    "wearable_token": "wearable_token",
    "lab_draw": "lab_draw",
    "lab_result": "lab_result",
    "habit_log": "habit_log",
}

# Columns per table (excluding id, created_at, updated_at, deleted_at which are handled generically)
TABLE_COLUMNS = {
    "person": [
        "name", "relationship", "date_of_birth", "biological_sex",
        "conditions_json", "medications", "family_history_json",
        "health_notes", "health_engine_user_id", "channel", "channel_target",
        "wearables_json",
    ],
    "wearable_token": [
        "person_id", "user_id", "service", "token_name", "token_data",
    ],
    "habit": [
        "person_id", "title", "purpose", "category", "emoji", "anchor",
        "state", "sort_order", "identity_threshold", "graduated_at", "show_in_today",
    ],
    "check_in": [
        "habit_id", "date", "completed", "note",
    ],
    "check_in_message": [
        "person_id", "habit_id", "message_text", "message_type", "action_type",
    ],
    "focus_plan": [
        "person_id", "generated_at", "health_snapshot", "reflection",
        "insight", "encouragement", "primary_action", "primary_anchor",
        "primary_reasoning", "primary_category", "primary_purpose",
        "alternatives_json", "risk_assessment", "care_team_note",
        "care_team_summary", "care_team_suggestions",
        "legacy_habit_title", "legacy_habit_purpose", "legacy_habit_anchor",
        "legacy_habit_category", "legacy_habit_emoji",
    ],
    "health_measurement": [
        "person_id", "type_identifier", "value", "unit", "date", "source",
    ],
    "workout_record": [
        "person_id", "workout_type", "duration", "calories", "date", "source",
    ],
    "weight_entry": [
        "person_id", "date", "weight_lbs", "waist_in", "source",
    ],
    "meal_entry": [
        "person_id", "date", "meal_num", "time_of_day", "description",
        "protein_g", "carbs_g", "fat_g", "calories", "notes",
    ],
    "bp_entry": [
        "person_id", "date", "systolic", "diastolic", "source",
    ],
    "training_session": [
        "person_id", "date", "rpe", "duration_min", "type", "name", "notes", "source",
    ],
    "strength_set": [
        "session_id", "person_id", "date", "exercise", "weight_lbs", "reps", "rpe", "notes",
    ],
    "wearable_daily": [
        "person_id", "date", "source", "rhr", "hrv", "hrv_weekly_avg", "hrv_status",
        "steps", "sleep_hrs", "deep_sleep_hrs", "light_sleep_hrs", "rem_sleep_hrs",
        "awake_hrs", "sleep_start", "sleep_end", "calories_total", "calories_active",
        "calories_bmr", "stress_avg", "floors", "distance_m", "max_hr", "min_hr",
        "vo2_max", "body_battery",
    ],
    "lab_draw": [
        "person_id", "date", "source", "notes",
    ],
    "lab_result": [
        "draw_id", "person_id", "marker", "value", "value_text", "unit",
        "reference_low", "reference_high", "flag",
    ],
    "habit_log": [
        "person_id", "date", "habit_name", "completed", "notes",
    ],
}
