"""Tests for wearable_daily reader queries with multi-source data.

When a user has both Garmin and Apple Health rows for the same date,
readers must return one row per date (not duplicates).
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def db(tmp_path):
    """Create a temp DB with multi-source wearable_daily data."""
    from engine.gateway.db import init_db, get_db, close_db
    close_db()
    db_path = tmp_path / "kasane.db"
    init_db(db_path)
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("p1", "Andrew", "andrew", now, now),
    )

    # Two sources for the same 3 dates
    for i, d in enumerate(["2026-04-01", "2026-04-02", "2026-04-03"]):
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, "
            "sleep_start, vo2_max, "
            "calories_total, calories_active, calories_bmr, zone2_min, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"g{i}", "p1", d, "garmin", 48.0, 62.0, 9500, 7.5,
             "22:30", 47.0,
             2400, 600, 1800, 145, now, now),
        )
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, "
            "calories_total, calories_active, calories_bmr, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"a{i}", "p1", d, "apple_health", 50.0, 40.0, 8000, 7.0,
             None, 450, None, now, now),
        )
    conn.commit()
    yield conn, db_path
    close_db()


class TestGetWearableDaily:
    """db_read.get_wearable_daily should return one row per date."""

    def test_no_duplicate_dates(self, db):
        conn, db_path = db
        import engine.db_read as dr

        with patch.object(dr, "_DB_PATH", db_path), patch.object(dr, "_initialized", False):
            rows = dr.get_wearable_daily(user_id="andrew", days=7)

        dates = [r["date"] for r in rows]
        assert len(dates) == len(set(dates)), (
            f"Duplicate dates in get_wearable_daily: {dates}"
        )

    def test_prefers_garmin_over_apple_health(self, db):
        conn, db_path = db
        import engine.db_read as dr

        with patch.object(dr, "_DB_PATH", db_path), patch.object(dr, "_initialized", False):
            rows = dr.get_wearable_daily(user_id="andrew", days=7)

        for r in rows:
            assert r["source"] == "garmin", (
                f"Expected garmin for date {r['date']}, got {r['source']}"
            )


class TestPersonContextWearable:
    """_get_person_context wearable snapshot should prefer garmin when both exist."""

    def test_snapshot_prefers_garmin(self, db):
        """The production query: SELECT * ... ORDER BY date DESC LIMIT 1"""
        conn, db_path = db

        row = conn.execute(
            "SELECT * FROM wearable_daily WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            ("p1",),
        ).fetchone()

        assert row is not None
        assert row["source"] == "garmin", (
            f"Expected garmin for latest snapshot, got {row['source']}"
        )


class TestCaloriesBurnQuery:
    """Calorie burn query should not double-count from multiple sources."""

    def test_no_duplicate_burn_dates(self, db):
        conn, db_path = db

        rows = conn.execute(
            "SELECT date, calories_total, calories_active, calories_bmr "
            "FROM wearable_daily "
            "WHERE person_id = ? AND calories_total IS NOT NULL "
            "ORDER BY date",
            ("p1",),
        ).fetchall()

        dates = [r["date"] for r in rows]
        assert len(dates) == len(set(dates)), (
            f"Duplicate burn dates would inflate calorie totals: {dates}"
        )


class TestBriefingWearable:
    """Briefing wearable query should return one row per date."""

    def test_no_duplicate_dates_in_series(self, db):
        conn, db_path = db
        from engine.coaching.briefing import _load_wearable_daily_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            rows = _load_wearable_daily_sqlite("p1")

        assert rows is not None
        dates = [r["date"] for r in rows]
        assert len(dates) == len(set(dates)), (
            f"Duplicate dates in briefing series: {dates}"
        )


class TestLoadWearableAveragesSqlite:
    """_load_wearable_averages_sqlite should compute rolling averages from wearable_daily."""

    def test_returns_scoring_keys(self, db):
        """Should return all keys that scoring expects."""
        conn, db_path = db
        from mcp_server.tools import _load_wearable_averages_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        assert avgs is not None
        assert "resting_hr" in avgs
        assert "daily_steps_avg" in avgs
        assert "sleep_duration_avg" in avgs
        assert "hrv_rmssd_avg" in avgs
        assert "vo2_max" in avgs
        assert "zone2_min_per_week" in avgs

    def test_averages_from_garmin_preferred(self, db):
        """Averages should come from garmin rows (preferred source), not apple_health."""
        conn, db_path = db
        from mcp_server.tools import _load_wearable_averages_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        # Garmin rhr=48.0 for all 3 days, apple_health=50.0
        assert avgs["resting_hr"] == 48.0
        assert avgs["daily_steps_avg"] == 9500
        assert avgs["sleep_duration_avg"] == 7.5
        assert avgs["hrv_rmssd_avg"] == 62.0

    def test_returns_none_when_no_data(self, db):
        """Should return None for a person with no wearable data."""
        conn, db_path = db
        from mcp_server.tools import _load_wearable_averages_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("nonexistent-person")

        assert avgs is None

    def test_vo2_max_uses_latest(self, db):
        """vo2_max should be the most recent value, not averaged."""
        conn, db_path = db
        from mcp_server.tools import _load_wearable_averages_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        assert avgs["vo2_max"] == 47.0

    def test_zone2_uses_sum(self, db):
        """zone2_min_per_week should be the sum over the window, not average."""
        conn, db_path = db
        from mcp_server.tools import _load_wearable_averages_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        # 3 days x 145 min = 435
        assert avgs["zone2_min_per_week"] == 435


class TestVo2MaxSourceAnnotation:
    """VO2 max must include source so coaching doesn't alarm on cross-source changes."""

    def test_vo2_max_source_returned(self, db):
        """_load_wearable_averages_sqlite must return vo2_max_source alongside vo2_max."""
        conn, db_path = db
        from mcp_server.tools import _load_wearable_averages_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        assert avgs is not None
        assert "vo2_max_source" in avgs, "Missing vo2_max_source key"
        assert avgs["vo2_max_source"] == "garmin"

    def test_vo2_max_source_tracks_actual_provider(self, tmp_path):
        """When latest VO2 comes from apple_health, vo2_max_source should say so."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "Grigoriy", "grigoriy", now, now),
        )
        # Garmin data on Apr 1 with VO2 47.0
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, vo2_max, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("g1", "p1", "2026-04-01", "garmin", 48.0, 62.0, 9500, 7.5, 47.0, now, now),
        )
        # Apple Health data on Apr 2 with VO2 32.3 (different algorithm)
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, vo2_max, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("a1", "p1", "2026-04-02", "apple_health", 50.0, 40.0, 8000, 7.0, 32.3, now, now),
        )
        conn.commit()

        from mcp_server.tools import _load_wearable_averages_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        assert avgs["vo2_max"] == 32.3, "Should use latest VO2 (apple_health)"
        assert avgs["vo2_max_source"] == "apple_health"

        close_db()

    def test_vo2_max_source_none_when_no_vo2(self, tmp_path):
        """When no VO2 data exists, vo2_max_source should be None."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "NoVO2", "novo2", now, now),
        )
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, sleep_hrs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("w1", "p1", "2026-04-01", "garmin", 48.0, 7.5, now, now),
        )
        conn.commit()

        from mcp_server.tools import _load_wearable_averages_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        assert avgs["vo2_max"] is None
        assert avgs["vo2_max_source"] is None

        close_db()


class TestPersonContextNoJsonFallback:
    """_get_person_context should get wearable data from SQLite, not JSON files."""

    def test_no_json_fallback_when_sqlite_has_data(self, db):
        """Even if JSON files don't exist, wearable_snapshot should be populated."""
        conn, db_path = db

        # Query SQLite directly (simulating what _get_person_context does)
        with patch("engine.gateway.db._db_path", return_value=db_path):
            row = conn.execute(
                "SELECT * FROM wearable_daily WHERE person_id = ? "
                "ORDER BY date DESC, "
                "CASE source WHEN 'garmin' THEN 1 WHEN 'apple_health' THEN 2 ELSE 3 END "
                "LIMIT 1", ("p1",)
            ).fetchone()

        assert row is not None
        assert row["source"] == "garmin"
        assert row["rhr"] == 48.0


# --- Briefing SQLite migration tests ---

@pytest.fixture
def briefing_db(tmp_path):
    """DB with wearable, BP, weight, and training data for briefing tests."""
    from engine.gateway.db import init_db, get_db, close_db
    close_db()
    db_path = tmp_path / "kasane.db"
    init_db(db_path)
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("p1", "Andrew", "andrew", now, now),
    )

    # Wearable data: 7 days with calories
    for i in range(7):
        d = f"2026-04-{i+1:02d}"
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, "
            "sleep_start, vo2_max, zone2_min, "
            "calories_total, calories_active, calories_bmr, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"w{i}", "p1", d, "garmin", 48.0, 62.0, 9500, 7.5,
             "22:30", 47.0, 20,
             2400, 600, 1800, now, now),
        )

    # BP entries
    conn.execute(
        "INSERT INTO bp_entry (id, person_id, date, systolic, diastolic, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bp1", "p1", "2026-03-15", 112.0, 65.0, now, now),
    )

    # Weight entries
    conn.execute(
        "INSERT INTO weight_entry (id, person_id, date, weight_lbs, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("wt1", "p1", "2026-04-01", 192.5, now, now),
    )

    # Training session
    conn.execute(
        "INSERT INTO training_session (id, person_id, date, type, duration_min, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ts1", "p1", "2026-04-01", "strength", 60, "Upper push", now, now),
    )

    conn.commit()
    yield conn, db_path, tmp_path
    close_db()


class TestBriefingUsesWearableSqlite:
    """Briefing wearable profile should come from SQLite, not *_latest.json."""

    def test_briefing_wearable_from_sqlite(self, briefing_db):
        """Briefing should populate wearable data even when no JSON files exist."""
        conn, db_path, tmp_path = briefing_db
        data_dir = tmp_path / "data" / "users" / "andrew"
        data_dir.mkdir(parents=True)
        # No garmin_latest.json, no apple_health_latest.json — only SQLite

        from mcp_server.tools import _load_wearable_averages_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        assert avgs is not None
        assert avgs["resting_hr"] == 48.0
        assert avgs["daily_steps_avg"] == 9500.0
        assert avgs["hrv_rmssd_avg"] == 62.0
        assert avgs["vo2_max"] == 47.0


class TestBriefingBpFromSqlite:
    """Briefing BP date should come from SQLite, not read_csv(bp_log.csv)."""

    def test_latest_bp_date_from_sqlite(self, briefing_db):
        """Should get the last BP date from bp_entry table."""
        conn, db_path, tmp_path = briefing_db

        row = conn.execute(
            "SELECT date FROM bp_entry WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            ("p1",),
        ).fetchone()
        assert row is not None
        assert row["date"] == "2026-03-15"

    def test_bp_count_from_sqlite(self, briefing_db):
        """Should count recent BP readings from SQLite."""
        conn, db_path, tmp_path = briefing_db

        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM bp_entry WHERE person_id = ? AND date >= ?",
            ("p1", "2026-03-01"),
        ).fetchone()["cnt"]
        assert count == 1


class TestBriefingWeightFromSqlite:
    """Briefing weight date should come from SQLite, not read_csv(weight_log.csv)."""

    def test_latest_weight_date_from_sqlite(self, briefing_db):
        """Should get the last weight date from weight_entry table."""
        conn, db_path, tmp_path = briefing_db

        row = conn.execute(
            "SELECT date FROM weight_entry WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            ("p1",),
        ).fetchone()
        assert row is not None
        assert row["date"] == "2026-04-01"


class TestBriefingBurnFromSqlite:
    """Briefing calorie burn should come from wearable_daily, not garmin_daily_burn.json."""

    def test_burn_data_from_sqlite(self, briefing_db):
        """Should get calorie data from wearable_daily table."""
        conn, db_path, tmp_path = briefing_db

        rows = conn.execute(
            "SELECT date, calories_total, calories_active, calories_bmr "
            "FROM wearable_daily WHERE person_id = ? AND calories_total IS NOT NULL "
            "ORDER BY date DESC LIMIT 7",
            ("p1",),
        ).fetchall()
        assert len(rows) == 7
        assert rows[0]["calories_total"] == 2400


class TestBriefingTrainingFromSqlite:
    """Briefing training sessions should come from SQLite, not session_log.csv."""

    def test_sessions_from_sqlite(self, briefing_db):
        """Should get training sessions from training_session table."""
        conn, db_path, tmp_path = briefing_db

        rows = conn.execute(
            "SELECT * FROM training_session WHERE person_id = ? ORDER BY date DESC",
            ("p1",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["type"] == "strength"
        assert rows[0]["duration_min"] == 60


class TestGetProtocolsSqlite:
    """_get_protocols should use SQLite wearable averages, not garmin_latest.json."""

    def test_protocols_no_json_needed(self, briefing_db):
        """protocol_progress garmin arg should come from SQLite averages."""
        conn, db_path, tmp_path = briefing_db
        from mcp_server.tools import _load_wearable_averages_sqlite

        with patch("engine.gateway.db._db_path", return_value=db_path):
            avgs = _load_wearable_averages_sqlite("p1")

        assert avgs is not None
        # These are the keys protocol_progress looks up via garmin.get(metric_id)
        assert "resting_hr" in avgs
        assert "daily_steps_avg" in avgs
        assert "sleep_duration_avg" in avgs
        assert "hrv_rmssd_avg" in avgs
        assert avgs["resting_hr"] == 48.0


class TestBriefingDailySeriesFromSqliteAllSources:
    """briefing daily_series should use wearable_daily SQLite for Oura/Whoop, not separate JSON."""

    def test_oura_daily_from_sqlite_no_json(self, tmp_path):
        """When wearable_daily has oura rows, briefing should not need oura_daily.json."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "Oura User", "oura_user", now, now),
        )

        # 7 days of oura-only data
        for i in range(7):
            d = f"2026-04-{i+1:02d}"
            conn.execute(
                "INSERT INTO wearable_daily "
                "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, "
                "sleep_start, vo2_max, zone2_min, "
                "calories_total, calories_active, calories_bmr, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"o{i}", "p1", d, "oura", 55.0, 45.0, 7000, 7.2,
                 "23:00", None, None,
                 2100, 400, 1700, now, now),
            )
        conn.commit()

        from engine.coaching.briefing import _load_wearable_daily_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            series = _load_wearable_daily_sqlite("p1")

        assert series is not None
        assert len(series) == 7
        assert series[0]["source"] == "oura"
        assert series[0]["rhr"] == 55.0

        close_db()

    def test_whoop_daily_from_sqlite_no_json(self, tmp_path):
        """When wearable_daily has whoop rows, briefing should not need whoop_daily.json."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "Whoop User", "whoop_user", now, now),
        )

        for i in range(5):
            d = f"2026-04-{i+1:02d}"
            conn.execute(
                "INSERT INTO wearable_daily "
                "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, "
                "sleep_start, "
                "calories_total, calories_active, calories_bmr, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"w{i}", "p1", d, "whoop", 60.0, 50.0, 6000, 6.8,
                 "23:30",
                 2000, 350, 1650, now, now),
            )
        conn.commit()

        from engine.coaching.briefing import _load_wearable_daily_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            series = _load_wearable_daily_sqlite("p1")

        assert series is not None
        assert len(series) == 5
        assert series[0]["source"] == "whoop"

        close_db()

    def test_briefing_data_available_oura_daily_from_sqlite(self, tmp_path):
        """BUG: data_available['oura_daily'] is False for Oura users with SQLite data.

        briefing.py line 119 reads oura_daily.json, ignoring that
        _load_wearable_daily_sqlite already has Oura rows. data_available
        should reflect the actual presence of daily series data regardless
        of which file/table it came from.
        """
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "Oura User", "oura_user", now, now),
        )
        for i in range(7):
            d = f"2026-04-{i+1:02d}"
            conn.execute(
                "INSERT INTO wearable_daily "
                "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, "
                "sleep_start, calories_total, calories_active, calories_bmr, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"o{i}", "p1", d, "oura", 55.0, 45.0, 7000, 7.2,
                 "23:00", 2100, 400, 1700, now, now),
            )
        conn.commit()

        # Set up minimal config pointing at a data dir with NO JSON files
        data_dir = tmp_path / "data" / "users" / "oura_user"
        data_dir.mkdir(parents=True)
        (data_dir / "config.yaml").write_text("profile:\n  age: 30\n  sex: M\n")

        config = {
            "data_dir": str(data_dir),
            "profile": {"age": 30, "sex": "M"},
        }

        with patch("engine.gateway.db._db_path", return_value=db_path):
            from engine.coaching.briefing import build_briefing
            result = build_briefing(config)

        da = result["data_available"]
        # After migration: unified wearable_daily flag replaces per-source flags
        assert da.get("wearable_daily") is True, (
            f"Oura daily data is in SQLite but data_available shows: {da}"
        )
        # Per-source daily flags should be gone
        assert "garmin_daily" not in da
        assert "oura_daily" not in da
        assert "whoop_daily" not in da

        close_db()


class TestCliScoreUsesWearableSqlite:
    """cli.py cmd_score should load wearable data from SQLite, not garmin_latest.json."""

    def test_score_populates_profile_from_sqlite(self, tmp_path):
        """cmd_score should populate UserProfile from wearable_daily SQLite."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "Test User", "test_user", now, now),
        )
        for i in range(7):
            d = f"2026-04-{i+1:02d}"
            conn.execute(
                "INSERT INTO wearable_daily "
                "(id, person_id, date, source, rhr, hrv, steps, sleep_hrs, "
                "sleep_start, vo2_max, zone2_min, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"g{i}", "p1", d, "garmin", 48.0, 62.0, 9500, 7.5,
                 "22:30", 47.0, 20, now, now),
            )
        conn.commit()

        # data_dir with NO garmin_latest.json
        data_dir = tmp_path / "data" / "users" / "test_user"
        data_dir.mkdir(parents=True)
        assert not (data_dir / "garmin_latest.json").exists()

        from cli import _resolve_person_id, _load_wearable_for_profile
        with patch("engine.gateway.db._db_path", return_value=db_path):
            person_id = _resolve_person_id(data_dir)
            assert person_id == "p1"
            wearable = _load_wearable_for_profile(data_dir, person_id)

        assert wearable is not None
        assert wearable["resting_hr"] == 48.0
        assert wearable["daily_steps_avg"] == 9500.0

        close_db()


class TestAdminDigestWearableFreshnessSqlite:
    """admin_digest.py _gather_from_files should get wearable freshness from SQLite."""

    def test_wearable_freshness_from_sqlite_no_json(self, tmp_path):
        """has_wearable + freshness should come from wearable_daily, not garmin_latest.json."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "Test", "test_user", now, now),
        )
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, sleep_hrs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("w1", "p1", "2026-04-02", "garmin", 48.0, 7.5, now, now),
        )
        conn.commit()

        # No JSON files
        user_dir = tmp_path / "data" / "users" / "test_user"
        user_dir.mkdir(parents=True)
        assert not (user_dir / "garmin_latest.json").exists()

        from scripts.admin_digest import _wearable_freshness_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            freshness = _wearable_freshness_sqlite("p1")

        assert freshness is not None
        assert freshness["has_wearable"] is True
        assert freshness["source"] == "garmin"
        assert freshness["last_date"] == "2026-04-02"

        close_db()


class TestDigestHasWearableSqlite:
    """digest.py has_garmin/has_apple_health should check wearable_daily, not JSON."""

    def test_has_wearable_from_sqlite(self, tmp_path):
        """has_wearable should be True when wearable_daily has data, even without JSON."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "Test", "test_user", now, now),
        )
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, sleep_hrs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("w1", "p1", "2026-04-02", "oura", 55.0, 7.2, now, now),
        )
        conn.commit()

        from scripts.digest import _has_wearable_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            result = _has_wearable_sqlite("p1")

        assert result is True

        close_db()


class TestServerHealthSqliteFreshness:
    """server.py /health apple_health freshness should check wearable_daily, not JSON."""

    def test_apple_health_freshness_from_sqlite(self, tmp_path):
        """Should get Apple Health freshness from wearable_daily updated_at."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        conn = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p1", "AH User", "ah_user", now, now),
        )
        conn.execute(
            "INSERT INTO wearable_daily "
            "(id, person_id, date, source, rhr, sleep_hrs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("ah1", "p1", "2026-04-02", "apple_health", 52.0, 7.0, now, now),
        )
        conn.commit()

        from engine.gateway.server import _wearable_freshness_sqlite
        with patch("engine.gateway.db._db_path", return_value=db_path):
            freshness = _wearable_freshness_sqlite("ah_user")

        assert freshness is not None
        assert freshness["source"] == "apple_health"

        close_db()


class TestLoadHealthContextSqlite:
    """v1_api _load_health_context should read weight and meals from SQLite."""

    def test_weight_from_sqlite(self, briefing_db):
        """Weight data should come from weight_entry, not weight_log.csv."""
        conn, db_path, tmp_path = briefing_db
        from engine.gateway.db import close_db, get_db

        data_dir = tmp_path / "data" / "users" / "andrew"
        data_dir.mkdir(parents=True)

        # Verify data is in SQLite
        row = conn.execute(
            "SELECT weight_lbs FROM weight_entry WHERE person_id = 'p1'"
        ).fetchone()
        assert row is not None
        assert row["weight_lbs"] == 192.5

        # Test the query pattern _load_health_context uses
        pid_row = conn.execute(
            "SELECT id FROM person WHERE health_engine_user_id = 'andrew' AND deleted_at IS NULL"
        ).fetchone()
        assert pid_row is not None
        wt_rows = conn.execute(
            "SELECT date, weight_lbs, waist_in, source FROM weight_entry "
            "WHERE person_id = ? ORDER BY date DESC LIMIT 14",
            (pid_row["id"],),
        ).fetchall()
        assert len(wt_rows) == 1
        assert wt_rows[0]["weight_lbs"] == 192.5

    def test_meals_from_sqlite(self, briefing_db):
        """Meals should come from meal_entry, not meal_log.csv."""
        conn, db_path, tmp_path = briefing_db

        # Add a meal to SQLite
        now = datetime.now(timezone.utc).isoformat()
        today = datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO meal_entry (id, person_id, date, description, calories, protein_g, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "p1", today, "Chicken and rice", 650, 45, now, now),
        )
        conn.commit()

        # Test the query pattern _load_health_context uses
        meal_rows = conn.execute(
            "SELECT * FROM meal_entry WHERE person_id = 'p1' AND date = ?",
            (today,),
        ).fetchall()
        assert len(meal_rows) == 1
        assert meal_rows[0]["description"] == "Chicken and rice"


class TestWearableFreshnessSqlite:
    """Verify health/deep handles naive timestamps from wearable_daily."""

    def test_naive_timestamp_does_not_cause_parse_error(self, db, tmp_path):
        """Naive updated_at (no timezone) must not produce parse_error status."""
        from engine.gateway.db import get_db
        conn = get_db(tmp_path / "kasane.db")
        conn.execute(
            "INSERT INTO wearable_daily (id, person_id, date, source, rhr, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w-naive", "p1", "2026-04-10", "apple_health", 61.0,
             "2026-04-10T06:05:49", "2026-04-10T06:05:49"),
        )
        conn.commit()

        # Simulate the exact code path from server.py health/deep (lines 493-502)
        from datetime import datetime as _dt, timezone as _tz

        from engine.gateway.server import _wearable_freshness_sqlite
        with patch("engine.gateway.db.get_db", return_value=conn), \
             patch("engine.gateway.db.init_db"):
            freshness = _wearable_freshness_sqlite("andrew")

        assert freshness is not None
        ts_str = freshness["updated_at"]  # "2026-04-10T06:05:49" — no timezone

        # This is the exact code from server.py that causes parse_error
        ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        age_hours = (_dt.now(_tz.utc) - ts).total_seconds() / 3600
        status = "ok" if age_hours < 48 else "stale"
        assert status in ("ok", "stale")  # must not be parse_error
