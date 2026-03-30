"""SQLite read/write layer for health data.

Two modes:
- Repo mode (multi-user): DB at PROJECT_ROOT/data/kasane.db, person_id mapping
- Package mode (single-user): DB at ~/.baseline/data.db, default person_id

On first access, creates the database and all tables if they don't exist.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now().isoformat()

# Determine DB path: repo > env var > ~/.baseline/
_PROJECT_ROOT = Path(__file__).parent.parent
_REPO_DB = _PROJECT_ROOT / "data" / "kasane.db"
_HOME_DB = Path(os.path.expanduser("~/.baseline/data.db"))
_DEFAULT_PERSON = "local-user"

# Use repo DB if it exists (developer/server mode), otherwise package mode
if _REPO_DB.exists():
    _DB_PATH = _REPO_DB
else:
    _DB_PATH = Path(os.environ.get("BASELINE_DB", str(_HOME_DB)))

_initialized = False


def _ensure_db():
    """Create DB directory and tables on first access."""
    global _initialized
    if _initialized:
        return
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        from engine.gateway.db import init_db
        init_db(str(_DB_PATH))
    except ImportError:
        # Minimal schema for package mode (no gateway module)
        conn = sqlite3.connect(str(_DB_PATH))
        conn.executescript(_MINIMAL_SCHEMA)
        conn.commit()
        conn.close()
    # Ensure sleep_entry exists (added after initial schema)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("CREATE TABLE IF NOT EXISTS sleep_entry (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, date TEXT NOT NULL, bed_time TEXT, wake_time TEXT, source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    _initialized = True


_MINIMAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
    id TEXT PRIMARY KEY,
    name TEXT,
    biological_sex TEXT,
    health_engine_user_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS weight_entry (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    weight_lbs REAL NOT NULL,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meal_entry (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    meal_num INTEGER DEFAULT 1,
    time_of_day TEXT,
    description TEXT,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    calories REAL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bp_entry (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    systolic REAL NOT NULL,
    diastolic REAL NOT NULL,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS habit_log (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    habit_name TEXT NOT NULL,
    completed INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lab_draw (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    source TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lab_result (
    id TEXT PRIMARY KEY,
    draw_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    marker TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    unit TEXT,
    reference_low REAL,
    reference_high REAL,
    flag TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wearable_daily (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    source TEXT,
    rhr REAL, hrv REAL, hrv_weekly_avg REAL, hrv_status TEXT,
    steps INTEGER, sleep_hrs REAL, deep_sleep_hrs REAL,
    light_sleep_hrs REAL, rem_sleep_hrs REAL, awake_hrs REAL,
    sleep_start TEXT, sleep_end TEXT,
    calories_total REAL, calories_active REAL, calories_bmr REAL,
    stress_avg INTEGER, floors INTEGER, distance_m REAL,
    max_hr INTEGER, min_hr INTEGER, vo2_max REAL, body_battery INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS training_session (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    rpe REAL, duration_min REAL, type TEXT, name TEXT,
    notes TEXT, source TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strength_set (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    exercise TEXT, weight_lbs REAL, reps INTEGER, rpe REAL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sleep_entry (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    date TEXT NOT NULL,
    bed_time TEXT,
    wake_time TEXT,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def _db() -> sqlite3.Connection:
    _ensure_db()
    db = sqlite3.connect(str(_DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def _person_id_for_user(user_id: str | None) -> str:
    """Map health-engine user_id to person_id.

    Repo mode: maps known users to kasane person IDs.
    Package mode: returns default local-user.
    """
    if not user_id or user_id == "default":
        # Repo mode with kasane.db
        if _DB_PATH == _REPO_DB:
            return "andrew-deal-001"
        # Package mode
        return _DEFAULT_PERSON
    mapping = {
        "andrew": "andrew-deal-001",
        "grigoriy": "grigoriy-001",
    }
    return mapping.get(user_id, user_id)


def get_db_path() -> Path:
    """Return the current database path."""
    return _DB_PATH


def ensure_local_person(user_id: str | None = None) -> str:
    """Ensure a person record exists for local/package users. Returns person_id."""
    pid = _person_id_for_user(user_id)
    db = _db()
    existing = db.execute("SELECT id FROM person WHERE id = ?", (pid,)).fetchone()
    if not existing:
        now = datetime.now().isoformat()
        db.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (pid, "Local User", user_id or "default", now, now),
        )
        db.commit()
    db.close()
    return pid


# ── Read functions ──────────────────────────────────────────────────


def get_weights(user_id: str | None = None, data_dir: Path | None = None) -> list[dict]:
    """Return weight entries sorted by date."""
    pid = _person_id_for_user(user_id)
    try:
        db = _db()
        rows = db.execute(
            "SELECT date, weight_lbs, source FROM weight_entry WHERE person_id = ? ORDER BY date",
            (pid,)
        ).fetchall()
        db.close()
        if rows:
            return [{"date": r["date"], "weight_lbs": str(r["weight_lbs"]), "source": r["source"] or ""} for r in rows]
    except Exception:
        pass
    # CSV fallback (repo mode only)
    if data_dir:
        from engine.utils.csv_io import read_csv
        csv_path = data_dir / "weight_log.csv"
        if csv_path.exists():
            return read_csv(csv_path)
    return []


def get_bp(user_id: str | None = None, data_dir: Path | None = None) -> list[dict]:
    """Return BP entries sorted by date."""
    pid = _person_id_for_user(user_id)
    try:
        db = _db()
        rows = db.execute(
            "SELECT date, systolic, diastolic, source FROM bp_entry WHERE person_id = ? ORDER BY date",
            (pid,)
        ).fetchall()
        db.close()
        if rows:
            return [{"date": r["date"], "systolic": str(r["systolic"]), "diastolic": str(r["diastolic"]), "source": r["source"] or ""} for r in rows]
    except Exception:
        pass
    if data_dir:
        from engine.utils.csv_io import read_csv
        csv_path = data_dir / "bp_log.csv"
        if csv_path.exists():
            return read_csv(csv_path)
    return []


def get_meals(user_id: str | None = None, date: str | None = None, days: int = 1, data_dir: Path | None = None) -> list[dict]:
    """Return meal entries. If date given, filter to that range."""
    pid = _person_id_for_user(user_id)
    try:
        db = _db()
        if date:
            from datetime import timedelta
            start = date
            end = date
            if days > 1:
                end_dt = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days - 1)
                end = end_dt.strftime("%Y-%m-%d")
            rows = db.execute(
                "SELECT date, description, protein_g, carbs_g, fat_g, calories FROM meal_entry WHERE person_id = ? AND date BETWEEN ? AND ? ORDER BY date, meal_num",
                (pid, start, end)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT date, description, protein_g, carbs_g, fat_g, calories FROM meal_entry WHERE person_id = ? ORDER BY date, meal_num",
                (pid,)
            ).fetchall()
        db.close()
        if rows:
            return [{"date": r["date"], "description": r["description"], "protein_g": str(r["protein_g"] or ""), "carbs_g": str(r["carbs_g"] or ""), "fat_g": str(r["fat_g"] or ""), "calories": str(r["calories"] or "")} for r in rows]
    except Exception:
        pass
    if data_dir:
        from engine.utils.csv_io import read_csv
        csv_path = data_dir / "meal_log.csv"
        if csv_path.exists():
            all_rows = read_csv(csv_path)
            if date:
                return [r for r in all_rows if r.get("date") == date]
            return all_rows
    return []


def get_habits(user_id: str | None = None, date: str | None = None, data_dir: Path | None = None) -> list[dict]:
    """Return habit log entries in long format: [{date, habit, completed}, ...]"""
    pid = _person_id_for_user(user_id)
    try:
        db = _db()
        if date:
            rows = db.execute(
                "SELECT date, habit_name, completed FROM habit_log WHERE person_id = ? AND date = ? ORDER BY date",
                (pid, date)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT date, habit_name, completed FROM habit_log WHERE person_id = ? ORDER BY date",
                (pid,)
            ).fetchall()
        db.close()
        if rows:
            return [{"date": r["date"], "habit": r["habit_name"], "completed": "y" if r["completed"] else "n"} for r in rows]
    except Exception:
        pass
    if data_dir:
        from engine.utils.csv_io import read_csv
        csv_path = data_dir / "daily_habits.csv"
        if csv_path.exists():
            return read_csv(csv_path)
    return []


def get_sleep(user_id: str | None = None, data_dir: Path | None = None) -> list[dict]:
    """Return sleep entries."""
    pid = _person_id_for_user(user_id)
    try:
        db = _db()
        rows = db.execute(
            "SELECT date, bed_time, wake_time, source FROM sleep_entry WHERE person_id = ? ORDER BY date",
            (pid,)
        ).fetchall()
        db.close()
        if rows:
            return [{"date": r["date"], "bed_time": r["bed_time"], "wake_time": r["wake_time"]} for r in rows]
    except Exception:
        pass
    if data_dir:
        from engine.utils.csv_io import read_csv
        csv_path = data_dir / "sleep_log.csv"
        if csv_path.exists():
            return read_csv(csv_path)
    return []


def get_wearable_daily(user_id: str | None = None, days: int = 7) -> list[dict]:
    """Return recent wearable daily summaries."""
    pid = _person_id_for_user(user_id)
    try:
        db = _db()
        rows = db.execute(
            "SELECT * FROM wearable_daily WHERE person_id = ? ORDER BY date DESC LIMIT ?",
            (pid, days)
        ).fetchall()
        db.close()
        if rows:
            return [dict(r) for r in rows]
    except Exception:
        pass
    return []


def get_labs(user_id: str | None = None, data_dir: Path | None = None) -> dict:
    """Return lab results with draws and latest values."""
    pid = _person_id_for_user(user_id)
    db = None
    try:
        db = _db()
        draws = db.execute(
            "SELECT id, date, source FROM lab_draw WHERE person_id = ? ORDER BY date",
            (pid,)
        ).fetchall()
        if draws:
            result = {"draws": [], "latest": {}}
            for draw in draws:
                results = db.execute(
                    "SELECT marker, value, unit, reference_low, reference_high, flag FROM lab_result WHERE draw_id = ?",
                    (draw["id"],)
                ).fetchall()
                draw_data = {
                    "date": draw["date"],
                    "source": draw["source"],
                    "results": {r["marker"]: r["value"] for r in results},
                }
                result["draws"].append(draw_data)
                for r in results:
                    result["latest"][r["marker"]] = r["value"]
            db.close()
            return result
    except Exception:
        pass
    if db:
        db.close()
    if data_dir:
        lab_path = data_dir / "lab_results.json"
        if lab_path.exists():
            with open(lab_path) as f:
                return json.load(f)
    return {}


def get_strength(user_id: str | None = None, data_dir: Path | None = None) -> list[dict]:
    """Return strength log entries."""
    pid = _person_id_for_user(user_id)
    try:
        db = _db()
        rows = db.execute(
            "SELECT s.date, s.exercise, s.weight_lbs, s.reps, s.rpe "
            "FROM strength_set s WHERE s.person_id = ? ORDER BY s.date",
            (pid,)
        ).fetchall()
        db.close()
        if rows:
            return [{"date": r["date"], "exercise": r["exercise"], "weight_lbs": str(r["weight_lbs"]), "reps": str(r["reps"]), "rpe": str(r["rpe"] or "")} for r in rows]
    except Exception:
        pass
    if data_dir:
        from engine.utils.csv_io import read_csv
        csv_path = data_dir / "strength_log.csv"
        if csv_path.exists():
            return read_csv(csv_path)
    return []


# ── Write functions (for package mode) ──────────────────────────────


def write_weight(weight_lbs: float, date: str, user_id: str | None = None, source: str = "mcp") -> dict:
    """Write a weight entry to SQLite."""
    import uuid
    pid = _person_id_for_user(user_id)
    ensure_local_person(user_id)
    db = _db()
    entry_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO weight_entry (id, person_id, date, weight_lbs, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entry_id, pid, date, weight_lbs, source, _now(), _now()),
    )
    db.commit()
    db.close()
    return {"logged": True, "date": date, "weight_lbs": weight_lbs}


def write_bp(systolic: int, diastolic: int, date: str, user_id: str | None = None, source: str = "mcp") -> dict:
    """Write a BP entry to SQLite."""
    import uuid
    pid = _person_id_for_user(user_id)
    ensure_local_person(user_id)
    db = _db()
    entry_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO bp_entry (id, person_id, date, systolic, diastolic, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, pid, date, systolic, diastolic, source, _now(), _now()),
    )
    db.commit()
    db.close()
    return {"logged": True, "date": date, "systolic": systolic, "diastolic": diastolic}


def write_meal(description: str, protein_g: float, date: str, carbs_g: float | None = None,
               fat_g: float | None = None, calories: float | None = None,
               user_id: str | None = None) -> dict:
    """Write a meal entry to SQLite."""
    import uuid
    pid = _person_id_for_user(user_id)
    ensure_local_person(user_id)
    db = _db()
    entry_id = str(uuid.uuid4())
    # Get next meal_num for today
    existing = db.execute(
        "SELECT COUNT(*) FROM meal_entry WHERE person_id = ? AND date = ?",
        (pid, date),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO meal_entry (id, person_id, date, meal_num, description, protein_g, carbs_g, fat_g, calories, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, pid, date, existing + 1, description, protein_g, carbs_g, fat_g, calories, _now(), _now()),
    )
    db.commit()
    db.close()
    return {"logged": True, "date": date, "meal_num": existing + 1, "description": description, "protein_g": protein_g}


def write_sleep(bed_time: str, wake_time: str, date: str, user_id: str | None = None) -> dict:
    """Write a sleep entry to SQLite."""
    import uuid
    pid = _person_id_for_user(user_id)
    ensure_local_person(user_id)
    db = _db()
    entry_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sleep_entry (id, person_id, date, bed_time, wake_time, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entry_id, pid, date, bed_time, wake_time, _now(), _now()),
    )
    db.commit()
    db.close()
    return {"logged": True, "date": date, "bed_time": bed_time, "wake_time": wake_time}


def write_habit(habit_name: str, completed: bool, date: str, user_id: str | None = None) -> dict:
    """Write a habit log entry to SQLite."""
    import uuid
    pid = _person_id_for_user(user_id)
    ensure_local_person(user_id)
    db = _db()
    entry_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO habit_log (id, person_id, date, habit_name, completed, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entry_id, pid, date, habit_name, 1 if completed else 0, _now(), _now()),
    )
    db.commit()
    db.close()
    return {"logged": True, "date": date, "habit": habit_name, "completed": completed}
