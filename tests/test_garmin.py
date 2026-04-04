"""Tests for Garmin integration (unit tests, no API calls)."""

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.integrations.garmin import GarminClient, DEFAULT_EXERCISE_MAP


def test_default_exercise_map():
    """Default exercise map should contain common lifts."""
    assert "barbell deadlift" in DEFAULT_EXERCISE_MAP
    assert "barbell bench press" in DEFAULT_EXERCISE_MAP
    assert "barbell back squat" in DEFAULT_EXERCISE_MAP


def test_normalize_exercise_mapped():
    """Known exercises should map to normalized names."""
    client = GarminClient()
    assert client.normalize_exercise("Barbell Deadlift") == "deadlift"
    assert client.normalize_exercise("dumbbell bench press") == "bench_press"
    assert client.normalize_exercise("Back Squat") == "squat"


def test_normalize_exercise_unknown():
    """Unknown exercises should be lowercased and underscored."""
    client = GarminClient()
    assert client.normalize_exercise("Lat Pulldown") == "lat_pulldown"
    assert client.normalize_exercise("Seated Row") == "seated_row"


def test_custom_exercise_map():
    """Custom exercise map should override defaults."""
    custom_map = {"cable fly": "chest_fly", "hammer curl": "bicep_curl"}
    client = GarminClient(exercise_map=custom_map)
    assert client.normalize_exercise("Cable Fly") == "chest_fly"
    assert client.normalize_exercise("Hammer Curl") == "bicep_curl"
    # Unknown exercises still get normalized
    assert client.normalize_exercise("Deadlift") == "deadlift"  # not in custom map


def test_from_config():
    """GarminClient.from_config should parse config dict."""
    config = {
        "garmin": {
            "email": "test@example.com",
            "token_dir": "/tmp/tokens",
        },
        "exercise_name_map": {"front squat": "squat"},
        "data_dir": "/tmp/data",
    }
    client = GarminClient.from_config(config)
    assert client.email == "test@example.com"
    assert str(client.token_dir) == "/tmp/tokens"
    assert client.exercise_map == {"front squat": "squat"}
    assert str(client.data_dir) == "/tmp/data"


def test_has_tokens_no_dir(tmp_path):
    """has_tokens returns False when token dir doesn't exist."""
    assert GarminClient.has_tokens(token_dir=str(tmp_path / "nonexistent")) is False


def test_has_tokens_with_dir(tmp_path):
    """has_tokens returns True when token dir has files."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "oauth1_token.json").write_text("{}")
    assert GarminClient.has_tokens(token_dir=str(token_dir)) is True


def test_has_tokens_empty_dir(tmp_path):
    """has_tokens returns False when token dir exists but is empty."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    assert GarminClient.has_tokens(token_dir=str(token_dir)) is False


def test_deprecation_warning(capsys):
    """from_config prints deprecation warning when credentials are in config."""
    config = {
        "garmin": {
            "email": "test@example.com",
            "password": "secret",
            "token_dir": "/tmp/tokens",
        },
    }
    GarminClient.from_config(config)
    captured = capsys.readouterr()
    assert "deprecated" in captured.err.lower()


# --- Schema tests ---

class TestWearableDailySchema:
    """Verify wearable_daily schema supports zone2_min and multi-source."""

    def test_zone2_min_column_exists(self, tmp_path):
        """wearable_daily should have a zone2_min column."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        db = get_db(db_path)
        cols = {row[1] for row in db.execute("PRAGMA table_info(wearable_daily)").fetchall()}
        assert "zone2_min" in cols, f"zone2_min not in wearable_daily columns: {cols}"
        close_db()

    def test_multi_source_unique_index(self, tmp_path):
        """Two rows with same (person_id, date) but different source should coexist."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        db = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO person (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("p1", "Test", now, now),
        )
        db.execute(
            "INSERT INTO wearable_daily (id, person_id, date, source, rhr, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w1", "p1", "2026-04-02", "garmin", 48.0, now, now),
        )
        db.execute(
            "INSERT INTO wearable_daily (id, person_id, date, source, rhr, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w2", "p1", "2026-04-02", "apple_health", 50.0, now, now),
        )
        db.commit()
        rows = db.execute(
            "SELECT * FROM wearable_daily WHERE person_id = 'p1' AND date = '2026-04-02'"
        ).fetchall()
        assert len(rows) == 2, f"Expected 2 rows (garmin + apple_health), got {len(rows)}"
        sources = {r["source"] for r in rows}
        assert sources == {"garmin", "apple_health"}
        close_db()


# --- Garmin SQLite write tests ---

class TestGarminSqliteWrite:
    """Verify _append_to_daily_series writes vo2_max and zone2_min to wearable_daily."""

    def _make_client(self, tmp_path):
        data_dir = tmp_path / "data" / "users" / "andrew"
        data_dir.mkdir(parents=True)
        return GarminClient(data_dir=str(data_dir))

    def _setup_db(self, tmp_path):
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        db = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p-andrew", "Andrew", "andrew", now, now),
        )
        db.commit()
        return db_path

    def test_append_writes_vo2_max_to_sqlite(self, tmp_path):
        """When snapshot includes vo2_max, it should land in wearable_daily."""
        db_path = self._setup_db(tmp_path)
        from engine.gateway.db import get_db

        client = self._make_client(tmp_path)
        snapshot = {
            "date": "2026-04-02",
            "rhr": 48.0,
            "hrv": 62.0,
            "steps": 9500,
            "sleep_hrs": 7.5,
            "vo2_max": 51.3,
            "deep_sleep_hrs": None, "light_sleep_hrs": None,
            "rem_sleep_hrs": None, "awake_hrs": None,
            "sleep_start": None, "sleep_end": None,
            "hrv_weekly_avg": None, "hrv_status": None,
            "calories_total": None, "calories_active": None,
            "calories_bmr": None, "stress_avg": None,
            "floors": None, "distance_m": None,
            "max_hr": None, "min_hr": None,
        }
        with patch("engine.gateway.db._db_path", return_value=db_path):
            client._append_to_daily_series(snapshot, person_id="p-andrew")

        db = get_db(db_path)
        row = db.execute(
            "SELECT vo2_max FROM wearable_daily WHERE person_id = 'p-andrew' AND date = '2026-04-02'"
        ).fetchone()
        assert row is not None
        assert row["vo2_max"] == 51.3

    def test_append_writes_zone2_min_to_sqlite(self, tmp_path):
        """When snapshot includes zone2_min, it should land in wearable_daily."""
        db_path = self._setup_db(tmp_path)
        from engine.gateway.db import get_db

        client = self._make_client(tmp_path)
        snapshot = {
            "date": "2026-04-02",
            "rhr": 48.0, "hrv": 62.0, "steps": 9500,
            "sleep_hrs": 7.5, "vo2_max": 51.3, "zone2_min": 145,
            "deep_sleep_hrs": None, "light_sleep_hrs": None,
            "rem_sleep_hrs": None, "awake_hrs": None,
            "sleep_start": None, "sleep_end": None,
            "hrv_weekly_avg": None, "hrv_status": None,
            "calories_total": None, "calories_active": None,
            "calories_bmr": None, "stress_avg": None,
            "floors": None, "distance_m": None,
            "max_hr": None, "min_hr": None,
        }
        with patch("engine.gateway.db._db_path", return_value=db_path):
            client._append_to_daily_series(snapshot, person_id="p-andrew")

        db = get_db(db_path)
        row = db.execute(
            "SELECT zone2_min FROM wearable_daily WHERE person_id = 'p-andrew' AND date = '2026-04-02'"
        ).fetchone()
        assert row is not None
        assert row["zone2_min"] == 145


# --- Backfill tests ---

class TestBackfillVo2Zone2:
    """Verify backfill_vo2_zone2 forward-fills NULL vo2_max from latest known value."""

    def _setup_db_with_rows(self, tmp_path, rows):
        """Create wearable_daily rows. Each row is (date, vo2_max, zone2_min)."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        db = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p-andrew", "Andrew", "andrew", now, now),
        )
        for i, (d, vo2, z2) in enumerate(rows):
            db.execute(
                "INSERT INTO wearable_daily (id, person_id, date, source, vo2_max, zone2_min, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"w{i}", "p-andrew", d, "garmin", vo2, z2, now, now),
            )
        db.commit()
        return db_path

    def test_forward_fills_null_vo2_max(self, tmp_path):
        """Rows with NULL vo2_max should get the latest known value."""
        rows = [
            ("2026-03-30", None, None),
            ("2026-03-31", None, None),
            ("2026-04-01", None, None),
            ("2026-04-02", 47.0, 152),  # today has values
        ]
        db_path = self._setup_db_with_rows(tmp_path, rows)
        from engine.gateway.db import get_db

        client = GarminClient(data_dir=str(tmp_path / "data"))
        with patch("engine.gateway.db._db_path", return_value=db_path):
            updated = client.backfill_vo2_zone2(person_id="p-andrew")

        db = get_db(db_path)
        results = db.execute(
            "SELECT date, vo2_max FROM wearable_daily WHERE person_id = 'p-andrew' ORDER BY date"
        ).fetchall()
        for row in results:
            assert row["vo2_max"] == 47.0, f"date {row['date']} has vo2_max={row['vo2_max']}, expected 47.0"
        assert updated == 3  # 3 rows were updated

    def test_does_not_overwrite_existing_vo2(self, tmp_path):
        """Rows that already have vo2_max should keep their value."""
        rows = [
            ("2026-03-30", 45.0, None),  # different historical value
            ("2026-04-02", 47.0, 152),
        ]
        db_path = self._setup_db_with_rows(tmp_path, rows)
        from engine.gateway.db import get_db

        client = GarminClient(data_dir=str(tmp_path / "data"))
        with patch("engine.gateway.db._db_path", return_value=db_path):
            updated = client.backfill_vo2_zone2(person_id="p-andrew")

        db = get_db(db_path)
        row = db.execute(
            "SELECT vo2_max FROM wearable_daily WHERE date = '2026-03-30'"
        ).fetchone()
        assert row["vo2_max"] == 45.0  # preserved, not overwritten
        assert updated == 0  # nothing to update

    def test_no_known_vo2_returns_zero(self, tmp_path):
        """If all rows have NULL vo2_max, nothing to forward-fill."""
        rows = [
            ("2026-03-30", None, None),
            ("2026-03-31", None, None),
        ]
        db_path = self._setup_db_with_rows(tmp_path, rows)

        client = GarminClient(data_dir=str(tmp_path / "data"))
        with patch("engine.gateway.db._db_path", return_value=db_path):
            updated = client.backfill_vo2_zone2(person_id="p-andrew")

        assert updated == 0

    def test_only_updates_garmin_source(self, tmp_path):
        """Should not touch apple_health rows."""
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        db = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO person (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("p1", "Test", now, now),
        )
        db.execute(
            "INSERT INTO wearable_daily (id, person_id, date, source, vo2_max, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w1", "p1", "2026-04-02", "garmin", 47.0, now, now),
        )
        db.execute(
            "INSERT INTO wearable_daily (id, person_id, date, source, vo2_max, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w2", "p1", "2026-04-01", "apple_health", None, now, now),
        )
        db.commit()

        client = GarminClient(data_dir=str(tmp_path / "data"))
        with patch("engine.gateway.db._db_path", return_value=db_path):
            client.backfill_vo2_zone2(person_id="p1")

        row = db.execute(
            "SELECT vo2_max FROM wearable_daily WHERE id = 'w2'"
        ).fetchone()
        assert row["vo2_max"] is None  # apple_health row untouched


class TestTier4NoJsonWrites:
    """Tier 4: _append_to_daily_series writes to SQLite only, not JSON."""

    def _make_client(self, tmp_path):
        data_dir = tmp_path / "data" / "users" / "andrew"
        data_dir.mkdir(parents=True)
        return GarminClient(data_dir=str(data_dir)), data_dir

    def _setup_db(self, tmp_path):
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        db = get_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("p-andrew", "Andrew", "andrew", now, now),
        )
        db.commit()
        return db_path

    def test_append_writes_sqlite_not_json(self, tmp_path):
        """_append_to_daily_series should write to SQLite and NOT create garmin_daily.json."""
        db_path = self._setup_db(tmp_path)
        from engine.gateway.db import get_db
        client, data_dir = self._make_client(tmp_path)

        snapshot = {
            "date": "2026-04-02",
            "rhr": 48.0, "hrv": 62.0, "steps": 9500,
            "sleep_hrs": 7.5, "vo2_max": 51.3,
            "deep_sleep_hrs": None, "light_sleep_hrs": None,
            "rem_sleep_hrs": None, "awake_hrs": None,
            "sleep_start": None, "sleep_end": None,
            "hrv_weekly_avg": None, "hrv_status": None,
            "calories_total": None, "calories_active": None,
            "calories_bmr": None, "stress_avg": None,
            "floors": None, "distance_m": None,
            "max_hr": None, "min_hr": None,
        }

        with patch("engine.gateway.db._db_path", return_value=db_path):
            client._append_to_daily_series(snapshot, person_id="p-andrew")

        # SQLite should have the row
        db = get_db(db_path)
        row = db.execute(
            "SELECT rhr FROM wearable_daily WHERE person_id = 'p-andrew' AND date = '2026-04-02'"
        ).fetchone()
        assert row is not None
        assert row["rhr"] == 48.0

        # JSON file should NOT exist
        json_path = data_dir / "garmin_daily.json"
        assert not json_path.exists(), f"garmin_daily.json should not be written (Tier 4), but found at {json_path}"


class TestPullAllHistoryBackfill:
    """Verify pull_all(history=True) calls backfill_vo2_zone2 after backfill."""

    def test_history_backfill_calls_vo2_forward_fill(self, tmp_path):
        """pull_all(history=True) should call backfill_vo2_zone2 after the loop."""
        data_dir = tmp_path / "data" / "users" / "andrew"
        data_dir.mkdir(parents=True)
        client = GarminClient(data_dir=str(data_dir))

        fake_snapshot = {
            "date": None, "rhr": 48.0, "hrv": 62.0, "steps": 9500,
            "sleep_hrs": 7.5, "vo2_max": None, "zone2_min": None,
            "deep_sleep_hrs": None, "light_sleep_hrs": None,
            "rem_sleep_hrs": None, "awake_hrs": None,
            "sleep_start": None, "sleep_end": None,
            "hrv_weekly_avg": None, "hrv_status": None,
            "calories_total": None, "calories_active": None,
            "calories_bmr": None, "stress_avg": None,
            "floors": None, "distance_m": None,
            "max_hr": None, "min_hr": None,
        }

        def mock_pull_day(d):
            s = dict(fake_snapshot)
            s["date"] = d
            return s

        mock_garmin = type("MockGarmin", (), {
            "get_max_metrics": lambda self, d: [{"generic": {"vo2MaxValue": 47.0}}],
            "get_activities_by_date": lambda self, s, e: [],
        })()

        with patch.object(GarminClient, "client", new_callable=lambda: property(lambda self: mock_garmin)), \
             patch.object(client, "_pull_day_snapshot", side_effect=mock_pull_day), \
             patch.object(client, "pull_zone2_minutes", return_value=152), \
             patch.object(client, "backfill_vo2_zone2", return_value=0) as mock_backfill:

            client.pull_all(history=True, history_days=3, person_id="p-andrew")

        mock_backfill.assert_called_once_with(person_id="p-andrew")


# --- Sleep time timezone tests ---

class TestGarminClientTokenStore:
    """Step 2: GarminClient uses TokenStore for token lifecycle."""

    @pytest.fixture
    def test_db(self, tmp_path):
        from engine.gateway.db import init_db, get_db, close_db
        close_db()
        db_path = tmp_path / "kasane.db"
        init_db(db_path)
        yield db_path
        close_db()

    @pytest.fixture
    def store(self, tmp_path, test_db, monkeypatch):
        garth_cache = tmp_path / "garth-cache"
        monkeypatch.setattr("engine.gateway.token_store._GARTH_CACHE_DIR", garth_cache)
        monkeypatch.setattr("engine.gateway.token_store._LEGACY_BASE_DIR", tmp_path / "legacy")
        monkeypatch.setattr(
            "engine.gateway.token_store._get_db",
            lambda: __import__("engine.gateway.db", fromlist=["get_db"]).get_db(test_db),
        )
        from engine.gateway.token_store import TokenStore
        return TokenStore(base_dir=tmp_path / "legacy")

    def test_init_accepts_token_store(self, store):
        """GarminClient.__init__ accepts token_store and user_id params."""
        client = GarminClient(token_store=store, user_id="andrew")
        assert client.token_store is store
        assert client.user_id == "andrew"

    def test_init_without_token_store(self):
        """GarminClient still works without token_store (backward compat)."""
        client = GarminClient()
        assert client.token_store is None
        assert client.user_id == "default"

    def test_from_config_passes_token_store(self, store):
        """from_config forwards token_store and user_id."""
        config = {"garmin": {"token_dir": "/tmp/test"}, "data_dir": "/tmp/data"}
        client = GarminClient.from_config(config, token_store=store, user_id="andrew")
        assert client.token_store is store
        assert client.user_id == "andrew"

    def test_connect_syncs_to_store_after_refresh(self, store, tmp_path):
        """After garth.dump() in connect(), tokens are synced to SQLite."""
        # Seed garth-cache with token files so connect() has something to load
        cache_dir = tmp_path / "garth-cache" / "andrew"
        cache_dir.mkdir(parents=True)
        (cache_dir / "oauth1_token.json").write_text('{"token": "o1"}')
        (cache_dir / "oauth2_token.json").write_text('{"access": "a", "refresh": "r"}')

        # Pre-populate the store so garmin_token_dir returns the cache
        store._import_garth_cache("andrew")

        token_dir = str(store.garmin_token_dir("andrew"))
        client = GarminClient(token_dir=token_dir, token_store=store, user_id="andrew")

        # Mock garth interactions in connect()
        mock_garth = type("MockGarth", (), {
            "load": lambda self, d: None,
            "dump": lambda self, d: None,
            "oauth2_token": type("T", (), {"expired": False, "refresh_expired": False})(),
            "profile": {"displayName": "TestUser"},
        })()
        mock_garmin = type("MockGarmin", (), {
            "garth": mock_garth,
            "display_name": None,
        })()

        with patch("garminconnect.Garmin", return_value=mock_garmin):
            client.connect()

        # Tokens should be in SQLite now
        assert store.has_token("garmin", "andrew")

    def test_auth_interactive_syncs_to_store(self, store, tmp_path):
        """auth_interactive with token_store syncs tokens to SQLite."""
        cache_dir = tmp_path / "garth-cache" / "andrew"
        cache_dir.mkdir(parents=True)

        mock_garth = type("MockGarth", (), {
            "dump": lambda self, d: (
                Path(d).mkdir(parents=True, exist_ok=True),
                (Path(d) / "oauth1_token.json").write_text('{"t": "1"}'),
                (Path(d) / "oauth2_token.json").write_text('{"t": "2"}'),
            ),
        })()
        mock_garmin = type("MockGarmin", (), {
            "garth": mock_garth,
            "login": lambda self: None,
        })()

        with patch("garminconnect.Garmin", return_value=mock_garmin), \
             patch("builtins.input", return_value="test@example.com"), \
             patch("getpass.getpass", return_value="password"):
            GarminClient.auth_interactive(
                token_dir=str(cache_dir),
                token_store=store,
                user_id="andrew",
            )

        assert store.has_token("garmin", "andrew")

    def test_auth_interactive_without_store(self, tmp_path):
        """auth_interactive without token_store still works (backward compat)."""
        token_dir = tmp_path / "tokens"

        mock_garth = type("MockGarth", (), {
            "dump": lambda self, d: (
                Path(d).mkdir(parents=True, exist_ok=True),
                (Path(d) / "oauth1_token.json").write_text('{"t": "1"}'),
            ),
        })()
        mock_garmin = type("MockGarmin", (), {
            "garth": mock_garth,
            "login": lambda self: None,
        })()

        with patch("garminconnect.Garmin", return_value=mock_garmin), \
             patch("builtins.input", return_value="test@example.com"), \
             patch("getpass.getpass", return_value="password"):
            result = GarminClient.auth_interactive(token_dir=str(token_dir))

        assert result is True
        assert (token_dir / "oauth1_token.json").exists()

    def test_connect_without_store_no_sync(self, tmp_path):
        """connect() without token_store doesn't crash (backward compat)."""
        token_dir = tmp_path / "tokens"
        token_dir.mkdir()
        (token_dir / "oauth1_token.json").write_text('{"token": "o1"}')
        (token_dir / "oauth2_token.json").write_text('{"access": "a"}')

        client = GarminClient(token_dir=str(token_dir))

        mock_garth = type("MockGarth", (), {
            "load": lambda self, d: None,
            "dump": lambda self, d: None,
            "oauth2_token": type("T", (), {"expired": False, "refresh_expired": False})(),
            "profile": {"displayName": "TestUser"},
        })()
        mock_garmin = type("MockGarmin", (), {
            "garth": mock_garth,
            "display_name": None,
        })()

        with patch("garminconnect.Garmin", return_value=mock_garmin):
            client.connect()
        # No crash, no store to sync to
        assert client.token_store is None


class TestSleepTimeTimezone:
    """Verify sleep_start/sleep_end are extracted correctly from Garmin timestamps.

    Garmin's sleepStartTimestampLocal is a UTC epoch representing local wall-clock
    time. Using datetime.fromtimestamp() double-converts by applying the system
    timezone offset. The fix: use datetime.utcfromtimestamp().
    """

    def _make_client(self, tmp_path):
        data_dir = tmp_path / "data" / "users" / "andrew"
        data_dir.mkdir(parents=True)
        client = GarminClient(data_dir=str(data_dir))
        from unittest.mock import MagicMock
        client._client = MagicMock()
        return client

    def _garmin_sleep_dto(self, hour=22, minute=30, sleep_secs=25200):
        """Build a Garmin sleep DTO with sleepStartTimestampLocal.

        The timestamp represents local wall-clock time encoded as UTC epoch.
        hour=22, minute=30 means the user went to bed at 10:30 PM local.
        sleep_secs=25200 means 7 hours of sleep.
        """
        # Garmin encodes local time as a UTC timestamp
        ts = int(datetime(2026, 4, 3, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)
        return {
            "dailySleepDTO": {
                "sleepStartTimestampLocal": ts,
                "sleepTimeSeconds": sleep_secs,
                "deepSleepSeconds": 3600,
                "lightSleepSeconds": 10800,
                "remSleepSeconds": 7200,
                "awakeSleepSeconds": 3600,
            }
        }

    def test_pull_day_snapshot_sleep_times(self, tmp_path):
        """_pull_day_snapshot should extract correct local sleep times."""
        client = self._make_client(tmp_path)
        sleep_dto = self._garmin_sleep_dto(hour=22, minute=30, sleep_secs=22320)

        client._client.get_stats.return_value = {}
        client._client.get_sleep_data.return_value = sleep_dto
        client._client.get_hrv_data.return_value = {}

        result = client._pull_day_snapshot("2026-04-03")

        assert result["sleep_start"] == "22:30", f"Expected 22:30, got {result['sleep_start']}"
        assert result["sleep_end"] == "04:42", f"Expected 04:42, got {result['sleep_end']}"

    def test_pull_daily_series_sleep_times(self, tmp_path):
        """pull_daily_series should extract correct local sleep times."""
        client = self._make_client(tmp_path)
        sleep_dto = self._garmin_sleep_dto(hour=23, minute=0, sleep_secs=25200)

        client._client.get_stats.return_value = {}
        client._client.get_rhr_day.return_value = {}
        client._client.get_hrv_data.return_value = {}
        client._client.get_sleep_data.return_value = sleep_dto

        with patch("engine.integrations.garmin.time.sleep"):
            with patch("engine.integrations.garmin.date") as mock_date:
                mock_date.today.return_value = date(2026, 4, 3)
                mock_date.side_effect = lambda *a, **k: date(*a, **k)
                series = client.pull_daily_series(days=1)

        assert len(series) == 1
        assert series[0]["sleep_start"] == "23:00", f"Expected 23:00, got {series[0]['sleep_start']}"
        assert series[0]["sleep_end"] == "06:00", f"Expected 06:00, got {series[0]['sleep_end']}"

    def test_sleep_regularity_uses_correct_times(self, tmp_path):
        """pull_sleep_regularity should compute bedtime stdev from correct times."""
        client = self._make_client(tmp_path)

        # Two nights: 10:30 PM and 11:00 PM (30 min apart -> stdev ~21.2 min)
        sleep_dtos = [
            self._garmin_sleep_dto(hour=22, minute=30),
            self._garmin_sleep_dto(hour=23, minute=0),
        ]
        call_count = [0]

        def mock_get_sleep(d):
            idx = min(call_count[0], len(sleep_dtos) - 1)
            call_count[0] += 1
            return sleep_dtos[idx]

        client._client.get_sleep_data.side_effect = mock_get_sleep

        with patch("engine.integrations.garmin.time.sleep"):
            result = client.pull_sleep_regularity(days=2)

        assert result is not None
        assert 20 < result < 23, f"Expected stdev ~21.2, got {result}"
