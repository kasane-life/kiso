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
    phone TEXT,
    email TEXT,
    timezone TEXT DEFAULT 'America/Los_Angeles',
    role TEXT DEFAULT 'user',
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
    zone2_min INTEGER,
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

-- Workout programming tables

CREATE TABLE IF NOT EXISTS workout_program (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES person(id),
    name TEXT NOT NULL,
    description TEXT,
    days_per_week INTEGER,
    start_date TEXT,
    end_date TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS program_day (
    id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL REFERENCES workout_program(id),
    day_number INTEGER NOT NULL,
    name TEXT NOT NULL,
    day_type TEXT,
    notes TEXT,
    sort_order INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prescribed_exercise (
    id TEXT PRIMARY KEY,
    program_day_id TEXT NOT NULL REFERENCES program_day(id),
    exercise_name TEXT NOT NULL,
    sets INTEGER,
    reps TEXT,
    rpe_target REAL,
    rest_seconds INTEGER,
    notes TEXT,
    sort_order INTEGER,
    category TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    channel TEXT,
    session_key TEXT,
    message_id TEXT,
    timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_wearable_person_date_source ON wearable_daily(person_id, date, source);
CREATE INDEX IF NOT EXISTS idx_lab_draw_person ON lab_draw(person_id);
CREATE INDEX IF NOT EXISTS idx_lab_result_person_marker ON lab_result(person_id, marker);
CREATE INDEX IF NOT EXISTS idx_habit_log_person_date ON habit_log(person_id, date);

CREATE INDEX IF NOT EXISTS idx_workout_program_person ON workout_program(person_id);
CREATE INDEX IF NOT EXISTS idx_program_day_program ON program_day(program_id);
CREATE INDEX IF NOT EXISTS idx_prescribed_exercise_day ON prescribed_exercise(program_day_id);

CREATE INDEX IF NOT EXISTS idx_convo_user ON conversation_message(user_id);
CREATE INDEX IF NOT EXISTS idx_convo_timestamp ON conversation_message(timestamp);
CREATE INDEX IF NOT EXISTS idx_convo_session ON conversation_message(session_key);

-- Scheduled send dedup: prevents double-sends per user per schedule type per day
CREATE TABLE IF NOT EXISTS scheduled_send (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   TEXT NOT NULL,
    schedule_type TEXT NOT NULL,  -- 'morning_brief', 'evening_checkin', 'weekly_review'
    sent_date   TEXT NOT NULL,    -- YYYY-MM-DD in the user's local timezone
    sent_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    status      TEXT NOT NULL DEFAULT 'sent',  -- 'sent', 'failed', 'dry_run'
    message_preview TEXT,
    UNIQUE(person_id, schedule_type, sent_date)
);

-- Per-user issue tracker: auto-created from signals and audit error spikes
CREATE TABLE IF NOT EXISTS user_issue (
    id          TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL REFERENCES person(id),
    category    TEXT NOT NULL,   -- auth_failure, stale_data, engagement, onboarding, error_spike, bad_coaching
    title       TEXT NOT NULL,
    detail      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open, resolved
    source      TEXT,            -- signal, audit, manual
    dedup_key   TEXT,            -- prevents duplicate open issues
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_issue_person ON user_issue(person_id);
CREATE INDEX IF NOT EXISTS idx_user_issue_status ON user_issue(status);

-- OAuth: dynamic client registration (Claude iOS registers itself)
CREATE TABLE IF NOT EXISTS oauth_client (
    client_id   TEXT PRIMARY KEY,
    client_json TEXT NOT NULL,  -- full OAuthClientInformationFull as JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- OAuth: authorization codes (short-lived, exchanged for tokens)
CREATE TABLE IF NOT EXISTS oauth_code (
    code         TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    person_id    TEXT NOT NULL,  -- links to person.id for user resolution
    scopes       TEXT NOT NULL DEFAULT '',  -- space-separated
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL DEFAULT 1,
    resource     TEXT,  -- RFC 8707
    expires_at   REAL NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- OAuth: access and refresh tokens
CREATE TABLE IF NOT EXISTS oauth_token (
    token       TEXT PRIMARY KEY,
    token_type  TEXT NOT NULL,  -- 'access' or 'refresh'
    client_id   TEXT NOT NULL,
    person_id   TEXT NOT NULL,  -- links to person.id for user resolution
    scopes      TEXT NOT NULL DEFAULT '',
    resource    TEXT,
    expires_at  REAL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- OAuth: magic invite links (pre-generated per user, used at consent page)
CREATE TABLE IF NOT EXISTS oauth_invite (
    code        TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL,  -- links to person.id
    used_at     TEXT,  -- NULL until used
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def init_db(db_path: Path | str | None = None):
    """Create all tables if they don't exist, then run migrations."""
    conn = get_db(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Safe migrations for existing databases. Each is idempotent."""
    # Person table migrations
    cols = {row[1] for row in conn.execute("PRAGMA table_info(person)").fetchall()}
    migrations = {
        "wearables_json": "ALTER TABLE person ADD COLUMN wearables_json TEXT DEFAULT '[]'",
        "channel": "ALTER TABLE person ADD COLUMN channel TEXT",
        "channel_target": "ALTER TABLE person ADD COLUMN channel_target TEXT",
        "phone": "ALTER TABLE person ADD COLUMN phone TEXT",
        "email": "ALTER TABLE person ADD COLUMN email TEXT",
        "timezone": "ALTER TABLE person ADD COLUMN timezone TEXT DEFAULT 'America/Los_Angeles'",
        "role": "ALTER TABLE person ADD COLUMN role TEXT DEFAULT 'user'",
    }
    dirty = False
    for col, sql in migrations.items():
        if col not in cols:
            conn.execute(sql)
            dirty = True

    # training_session: add program linkage columns
    ts_cols = {row[1] for row in conn.execute("PRAGMA table_info(training_session)").fetchall()}
    ts_migrations = {
        "program_id": "ALTER TABLE training_session ADD COLUMN program_id TEXT REFERENCES workout_program(id)",
        "program_day_id": "ALTER TABLE training_session ADD COLUMN program_day_id TEXT REFERENCES program_day(id)",
        "sentiment": "ALTER TABLE training_session ADD COLUMN sentiment TEXT",
        "energy_level": "ALTER TABLE training_session ADD COLUMN energy_level INTEGER",
        "sleep_quality": "ALTER TABLE training_session ADD COLUMN sleep_quality TEXT",
    }
    for col, sql in ts_migrations.items():
        if col not in ts_cols:
            conn.execute(sql)
            dirty = True

    # strength_set: add prescribed_exercise linkage
    ss_cols = {row[1] for row in conn.execute("PRAGMA table_info(strength_set)").fetchall()}
    if "prescribed_exercise_id" not in ss_cols:
        conn.execute("ALTER TABLE strength_set ADD COLUMN prescribed_exercise_id TEXT REFERENCES prescribed_exercise(id)")
        dirty = True
    if "completed" not in ss_cols:
        conn.execute("ALTER TABLE strength_set ADD COLUMN completed INTEGER DEFAULT 1")
        dirty = True

    # wearable_daily: add zone2_min column and update unique index to include source
    wd_cols = {row[1] for row in conn.execute("PRAGMA table_info(wearable_daily)").fetchall()}
    if "zone2_min" not in wd_cols:
        conn.execute("ALTER TABLE wearable_daily ADD COLUMN zone2_min INTEGER")
        dirty = True

    # Migrate unique index from (person_id, date) to (person_id, date, source)
    idx_info = conn.execute("PRAGMA index_list(wearable_daily)").fetchall()
    old_idx_exists = any(r[1] == "idx_wearable_person_date" for r in idx_info)
    if old_idx_exists:
        conn.execute("DROP INDEX idx_wearable_person_date")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_wearable_person_date_source "
            "ON wearable_daily(person_id, date, source)"
        )
        dirty = True

    # program_day / prescribed_exercise: add timestamp columns for sync
    pd_cols = {row[1] for row in conn.execute("PRAGMA table_info(program_day)").fetchall()}
    if "created_at" not in pd_cols:
        conn.execute("ALTER TABLE program_day ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE program_day ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        dirty = True

    pe_cols = {row[1] for row in conn.execute("PRAGMA table_info(prescribed_exercise)").fetchall()}
    if "created_at" not in pe_cols:
        conn.execute("ALTER TABLE prescribed_exercise ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE prescribed_exercise ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        dirty = True

    if dirty:
        conn.commit()


# =====================================================================
# User registry helpers (canonical source of truth for users)
# =====================================================================


def get_active_users(db_path: Path | str | None = None) -> list[dict]:
    """Get all active users with messaging info.

    Returns list of dicts with: user_id, name, phone, email, channel,
    channel_target, timezone, role, person_id.
    """
    conn = get_db(db_path)
    rows = conn.execute(
        """SELECT id, name, health_engine_user_id, phone, email,
                  channel, channel_target, timezone, role
           FROM person
           WHERE health_engine_user_id IS NOT NULL
             AND deleted_at IS NULL
           ORDER BY name"""
    ).fetchall()
    return [
        {
            "person_id": r["id"],
            "user_id": r["health_engine_user_id"],
            "name": r["name"],
            "phone": r["phone"],
            "email": r["email"],
            "channel": r["channel"],
            "channel_target": r["channel_target"],
            "timezone": r["timezone"] or "America/Los_Angeles",
            "role": r["role"] or "user",
        }
        for r in rows
    ]


def get_user(user_id: str, db_path: Path | str | None = None) -> dict | None:
    """Get a single user by health_engine_user_id. Returns None if not found."""
    conn = get_db(db_path)
    r = conn.execute(
        """SELECT id, name, health_engine_user_id, phone, email,
                  channel, channel_target, timezone, role
           FROM person
           WHERE health_engine_user_id = ?
             AND deleted_at IS NULL""",
        (user_id,),
    ).fetchone()
    if r is None:
        return None
    return {
        "person_id": r["id"],
        "user_id": r["health_engine_user_id"],
        "name": r["name"],
        "phone": r["phone"],
        "email": r["email"],
        "channel": r["channel"],
        "channel_target": r["channel_target"],
        "timezone": r["timezone"] or "America/Los_Angeles",
        "role": r["role"] or "user",
    }


def get_phone_to_user_map(db_path: Path | str | None = None) -> dict[str, dict]:
    """Get phone -> user dict for inbound message routing."""
    users = get_active_users(db_path)
    result = {}
    for u in users:
        if u["phone"]:
            result[u["phone"]] = u
            clean = u["phone"].replace("+", "").replace(" ", "").replace("-", "")
            result[clean] = u
    return result


def write_wearable_daily_row(person_id: str, day: dict, source: str):
    """Write a single day's wearable data to wearable_daily.

    day dict should have: date (required), plus any of: rhr, hrv, steps,
    sleep_hrs, deep_sleep_hrs, light_sleep_hrs, rem_sleep_hrs, awake_hrs,
    sleep_start, sleep_end, calories_total, calories_active, calories_bmr,
    stress_avg, floors, distance_m, max_hr, min_hr, vo2_max, body_battery, zone2_min.
    """
    import uuid as _uuid
    from datetime import datetime

    snap_date = day["date"]
    now = datetime.now().isoformat(timespec="seconds")
    rid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:wearable_daily:{snap_date}:{source}"))

    def _sf(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    def _si(v):
        if v is None or v == "":
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO wearable_daily (id, person_id, date, source, "
        "rhr, hrv, hrv_weekly_avg, hrv_status, steps, sleep_hrs, deep_sleep_hrs, "
        "light_sleep_hrs, rem_sleep_hrs, awake_hrs, sleep_start, sleep_end, "
        "calories_total, calories_active, calories_bmr, stress_avg, floors, "
        "distance_m, max_hr, min_hr, vo2_max, body_battery, zone2_min, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, person_id, snap_date, source,
         _sf(day.get("rhr")), _sf(day.get("hrv")),
         _sf(day.get("hrv_weekly_avg")), day.get("hrv_status"),
         _si(day.get("steps")), _sf(day.get("sleep_hrs")),
         _sf(day.get("deep_sleep_hrs")), _sf(day.get("light_sleep_hrs")),
         _sf(day.get("rem_sleep_hrs")), _sf(day.get("awake_hrs")),
         day.get("sleep_start"), day.get("sleep_end"),
         _sf(day.get("calories_total")), _sf(day.get("calories_active")),
         _sf(day.get("calories_bmr")), _si(day.get("stress_avg")),
         _sf(day.get("floors")), _sf(day.get("distance_m")),
         _si(day.get("max_hr")), _si(day.get("min_hr")),
         _sf(day.get("vo2_max")), _si(day.get("body_battery")),
         _si(day.get("zone2_min")),
         now, now),
    )
    db.commit()


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
    "workout_program": "workout_program",
    "program_day": "program_day",
    "prescribed_exercise": "prescribed_exercise",
}

# Columns per table (excluding id, created_at, updated_at, deleted_at which are handled generically)
TABLE_COLUMNS = {
    "person": [
        "name", "relationship", "date_of_birth", "biological_sex",
        "conditions_json", "medications", "family_history_json",
        "health_notes", "health_engine_user_id", "phone", "email",
        "timezone", "role", "channel", "channel_target", "wearables_json",
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
        "program_id", "program_day_id", "sentiment", "energy_level", "sleep_quality",
    ],
    "strength_set": [
        "session_id", "person_id", "date", "exercise", "weight_lbs", "reps", "rpe", "notes",
        "prescribed_exercise_id", "completed",
    ],
    "wearable_daily": [
        "person_id", "date", "source", "rhr", "hrv", "hrv_weekly_avg", "hrv_status",
        "steps", "sleep_hrs", "deep_sleep_hrs", "light_sleep_hrs", "rem_sleep_hrs",
        "awake_hrs", "sleep_start", "sleep_end", "calories_total", "calories_active",
        "calories_bmr", "stress_avg", "floors", "distance_m", "max_hr", "min_hr",
        "vo2_max", "body_battery", "zone2_min",
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
    "workout_program": [
        "person_id", "name", "description", "days_per_week", "start_date",
        "end_date", "status",
    ],
    "program_day": [
        "program_id", "day_number", "name", "day_type", "notes", "sort_order",
    ],
    "prescribed_exercise": [
        "program_day_id", "exercise_name", "sets", "reps", "rpe_target",
        "rest_seconds", "notes", "sort_order", "category",
    ],
}
