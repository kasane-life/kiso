"""SQLite database for Kasane entities (persons, habits, check-ins, focus plans).

Health tracking data (weight, meals, labs, Garmin) stays in CSVs.
The health_engine_user_id column on person bridges SQLite <-> CSV user directories.

DB file: data/kasane.db (inside existing Docker volume mount).
"""

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

CREATE INDEX IF NOT EXISTS idx_habit_person ON habit(person_id);
CREATE INDEX IF NOT EXISTS idx_checkin_habit ON check_in(habit_id);
CREATE INDEX IF NOT EXISTS idx_checkin_date ON check_in(date);
CREATE INDEX IF NOT EXISTS idx_message_person ON check_in_message(person_id);
CREATE INDEX IF NOT EXISTS idx_focus_plan_person ON focus_plan(person_id);
CREATE INDEX IF NOT EXISTS idx_health_measurement_person ON health_measurement(person_id);
CREATE INDEX IF NOT EXISTS idx_workout_record_person ON workout_record(person_id);
"""


def init_db(db_path: Path | str | None = None):
    """Create all tables if they don't exist."""
    conn = get_db(db_path)
    conn.executescript(_SCHEMA)
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
}

# Columns per table (excluding id, created_at, updated_at, deleted_at which are handled generically)
TABLE_COLUMNS = {
    "person": [
        "name", "relationship", "date_of_birth", "biological_sex",
        "conditions_json", "medications", "family_history_json",
        "health_notes", "health_engine_user_id",
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
}
