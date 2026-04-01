"""Tests for Oura Ring integration (unit tests, no API calls)."""

import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.integrations.oura import OuraClient, SERVICE_NAME
from engine.integrations.oura_auth import (
    run_auth_flow,
    run_gateway_auth_flow,
    _exchange_code,
    SERVICE_NAME as AUTH_SERVICE_NAME,
    DEFAULT_SCOPES,
)


# =====================================================================
# OuraClient unit tests
# =====================================================================


class TestOuraClientInit:
    def test_default_init(self):
        client = OuraClient()
        assert client.user_id == "default"
        assert client.data_dir == Path("./data")

    def test_custom_init(self):
        client = OuraClient(user_id="paul", data_dir="/tmp/health")
        assert client.user_id == "paul"
        assert client.data_dir == Path("/tmp/health")

    def test_from_config(self):
        config = {"data_dir": "/tmp/data"}
        client = OuraClient.from_config(config, user_id="mike")
        assert client.user_id == "mike"
        assert str(client.data_dir) == "/tmp/data"

    def test_from_config_defaults(self):
        client = OuraClient.from_config({})
        assert client.user_id == "default"


class TestOuraHasTokens:
    def test_no_tokens(self, tmp_path):
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        assert OuraClient.has_tokens(user_id="nobody", token_store=store) is False

    def test_with_tokens(self, tmp_path):
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        store._fernet = None  # Disable encryption for test simplicity
        store.save_token("oura", "testuser", {"access_token": "test123"})
        assert OuraClient.has_tokens(user_id="testuser", token_store=store) is True


class TestExtractRHR:
    def test_from_sleep_periods_via_extract_resting_hr(self):
        """_extract_resting_hr now delegates to sleep periods (not readiness
        contributor scores, which are 1-100 scores, not bpm)."""
        client = OuraClient()
        sleep_periods = [
            {"lowest_heart_rate": 52},
            {"lowest_heart_rate": 54},
            {"lowest_heart_rate": 56},
        ]
        result = client._extract_resting_hr([], sleep_periods)
        assert result == round(statistics.mean([52, 54, 56]), 1)

    def test_empty_data(self):
        client = OuraClient()
        result = client._extract_resting_hr([], [])
        assert result is None

    def test_from_sleep_periods(self):
        client = OuraClient()
        periods = [
            {"lowest_heart_rate": 52},
            {"lowest_heart_rate": 54},
        ]
        result = client._extract_resting_hr_from_sleep_periods(periods)
        assert result == 53.0


class TestExtractHRV:
    def test_from_sleep_periods(self):
        client = OuraClient()
        periods = [
            {"average_hrv": 45.0},
            {"average_hrv": 50.0},
            {"average_hrv": 55.0},
        ]
        result = client._extract_hrv(periods)
        assert result == 50.0

    def test_empty_periods(self):
        client = OuraClient()
        result = client._extract_hrv([])
        assert result is None

    def test_skips_zero(self):
        client = OuraClient()
        periods = [
            {"average_hrv": 0},
            {"average_hrv": 50.0},
        ]
        result = client._extract_hrv(periods)
        assert result == 50.0


class TestExtractSleepDuration:
    def test_basic(self):
        client = OuraClient()
        daily_sleep = [
            {"total_sleep_duration": 7 * 3600},  # 7 hours
            {"total_sleep_duration": 8 * 3600},  # 8 hours
        ]
        result = client._extract_sleep_duration(daily_sleep)
        assert result == 7.5

    def test_empty(self):
        client = OuraClient()
        result = client._extract_sleep_duration([])
        assert result is None


class TestExtractSleepRegularity:
    def test_consistent_bedtime(self):
        client = OuraClient()
        periods = [
            {"type": "long_sleep", "bedtime_start": "2026-03-20T22:30:00-07:00"},
            {"type": "long_sleep", "bedtime_start": "2026-03-21T22:35:00-07:00"},
            {"type": "long_sleep", "bedtime_start": "2026-03-22T22:25:00-07:00"},
        ]
        result = client._extract_sleep_regularity(periods)
        assert result is not None
        assert result < 10  # Very consistent, should be small

    def test_irregular_bedtime(self):
        client = OuraClient()
        periods = [
            {"type": "long_sleep", "bedtime_start": "2026-03-20T21:00:00-07:00"},
            {"type": "long_sleep", "bedtime_start": "2026-03-21T01:00:00-07:00"},
            {"type": "long_sleep", "bedtime_start": "2026-03-22T23:00:00-07:00"},
        ]
        result = client._extract_sleep_regularity(periods)
        assert result is not None
        assert result > 30  # Irregular, should be larger

    def test_skips_naps(self):
        client = OuraClient()
        periods = [
            {"type": "long_sleep", "bedtime_start": "2026-03-20T22:30:00-07:00"},
            {"type": "rest", "bedtime_start": "2026-03-20T14:00:00-07:00"},
            {"type": "long_sleep", "bedtime_start": "2026-03-21T22:30:00-07:00"},
        ]
        result = client._extract_sleep_regularity(periods)
        assert result is not None
        assert result < 5  # Should be very consistent (nap excluded)

    def test_insufficient_data(self):
        client = OuraClient()
        periods = [
            {"type": "long_sleep", "bedtime_start": "2026-03-20T22:30:00-07:00"},
        ]
        result = client._extract_sleep_regularity(periods)
        assert result is None


class TestExtractSteps:
    def test_basic(self):
        client = OuraClient()
        activity = [
            {"steps": 8000},
            {"steps": 10000},
            {"steps": 12000},
        ]
        result = client._extract_steps(activity)
        assert result == 10000

    def test_empty(self):
        client = OuraClient()
        result = client._extract_steps([])
        assert result is None


class TestExtractZone2:
    def test_basic(self):
        client = OuraClient()
        today = date.today()
        activity = [
            {"day": (today - timedelta(days=1)).isoformat(), "medium_activity_met_minutes": 80},
            {"day": (today - timedelta(days=2)).isoformat(), "medium_activity_met_minutes": 60},
        ]
        result = client._extract_zone2_minutes(activity, days=7)
        assert result is not None
        assert result > 0

    def test_excludes_old_data(self):
        client = OuraClient()
        today = date.today()
        activity = [
            {"day": (today - timedelta(days=30)).isoformat(), "medium_activity_met_minutes": 200},
        ]
        result = client._extract_zone2_minutes(activity, days=7)
        assert result is None


class TestPullAll:
    def test_saves_oura_latest(self, tmp_path):
        """pull_all should save oura_latest.json with correct schema."""
        client = OuraClient(data_dir=str(tmp_path))

        # Mock all API calls
        with patch.object(client, 'pull_sleep', return_value=[
            {"day": "2026-03-20", "total_sleep_duration": 7 * 3600},
        ]), patch.object(client, 'pull_sleep_periods', return_value=[
            {"day": "2026-03-20", "type": "long_sleep", "average_hrv": 50.0, "lowest_heart_rate": 55,
             "bedtime_start": "2026-03-20T22:30:00-07:00", "total_sleep_duration": 7 * 3600},
        ]), patch.object(client, 'pull_activity', return_value=[
            {"day": "2026-03-20", "steps": 9500},
        ]), patch.object(client, 'pull_readiness', return_value=[]):
            result = client.pull_all()

        # Check return value schema matches garmin_latest.json
        assert "last_updated" in result
        assert "resting_hr" in result
        assert "hrv_rmssd_avg" in result
        assert "sleep_duration_avg" in result
        assert "sleep_regularity_stddev" in result
        assert "daily_steps_avg" in result
        assert "vo2_max" in result  # Should be None for Oura
        assert "zone2_min_per_week" in result

        assert result["vo2_max"] is None
        # RHR now comes from sleep periods (lowest_heart_rate), not readiness contributors
        assert result["resting_hr"] == 55.0
        assert result["hrv_rmssd_avg"] == 50.0
        assert result["sleep_duration_avg"] == 7.0
        assert result["daily_steps_avg"] == 9500

        # Check file was saved
        out_path = tmp_path / "oura_latest.json"
        assert out_path.exists()
        saved = json.loads(out_path.read_text())
        assert saved["source"] == "oura"
        assert saved["resting_hr"] == 55.0

    def test_saves_daily_series_with_history(self, tmp_path):
        """pull_all with history=True should save oura_daily.json."""
        client = OuraClient(data_dir=str(tmp_path))

        today = date.today()
        sleep_data = [
            {"day": (today - timedelta(days=i)).isoformat(), "total_sleep_duration": 7 * 3600}
            for i in range(5)
        ]
        period_data = [
            {"day": (today - timedelta(days=i)).isoformat(), "type": "long_sleep",
             "average_hrv": 50.0, "lowest_heart_rate": 55,
             "bedtime_start": f"{(today - timedelta(days=i)).isoformat()}T22:30:00-07:00",
             "total_sleep_duration": 7 * 3600}
            for i in range(5)
        ]
        activity_data = [
            {"day": (today - timedelta(days=i)).isoformat(), "steps": 9000}
            for i in range(5)
        ]

        with patch.object(client, 'pull_sleep', return_value=sleep_data), \
             patch.object(client, 'pull_sleep_periods', return_value=period_data), \
             patch.object(client, 'pull_activity', return_value=activity_data), \
             patch.object(client, 'pull_readiness', return_value=[]):
            client.pull_all(history=True, history_days=5)

        series_path = tmp_path / "oura_daily.json"
        assert series_path.exists()
        series = json.loads(series_path.read_text())
        assert isinstance(series, list)
        assert len(series) == 5
        # Verify schema matches garmin_daily.json
        for entry in series:
            assert "date" in entry
            assert "rhr" in entry
            assert "hrv" in entry
            assert "steps" in entry
            assert "sleep_hrs" in entry

    def test_no_data_doesnt_overwrite(self, tmp_path):
        """If API returns no data, existing file should be kept."""
        out_path = tmp_path / "oura_latest.json"
        out_path.write_text('{"existing": true}')

        client = OuraClient(data_dir=str(tmp_path))

        with patch.object(client, 'pull_sleep', return_value=[]), \
             patch.object(client, 'pull_sleep_periods', return_value=[]), \
             patch.object(client, 'pull_activity', return_value=[]), \
             patch.object(client, 'pull_readiness', return_value=[]):
            result = client.pull_all()

        # All metrics should be None
        assert result["resting_hr"] is None
        assert result["hrv_rmssd_avg"] is None
        # File should still have old content
        assert json.loads(out_path.read_text()) == {"existing": True}


# =====================================================================
# Token storage tests
# =====================================================================


class TestOuraTokenStorage:
    def test_save_and_load(self, tmp_path, monkeypatch):
        from engine.gateway.token_store import TokenStore
        from engine.gateway.db import init_db, close_db, get_db
        close_db()
        db_path = tmp_path / "test.db"
        init_db(db_path)
        # Ensure token_store uses our test DB
        monkeypatch.setattr(
            "engine.gateway.token_store._get_db",
            lambda: get_db(db_path),
        )

        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        token_data = {
            "access_token": "oura_test_token",
            "refresh_token": "oura_refresh",
            "client_id": "test_client",
            "client_secret": "test_secret",
            "scopes": ["daily", "sleep"],
        }

        store.save_token("oura", "testuser", token_data)
        loaded = store.load_token("oura", "testuser")
        assert loaded == token_data
        close_db()

    def test_has_token(self, tmp_path, monkeypatch):
        from engine.gateway.token_store import TokenStore
        from engine.gateway.db import init_db, close_db, get_db
        close_db()
        db_path = tmp_path / "test.db"
        init_db(db_path)
        monkeypatch.setattr(
            "engine.gateway.token_store._get_db",
            lambda: get_db(db_path),
        )

        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        assert not store.has_token("oura", "paul")
        store.save_token("oura", "paul", {"access_token": "t"})
        assert store.has_token("oura", "paul")
        close_db()


# =====================================================================
# Metric mapping / schema tests
# =====================================================================


class TestSchemaCompat:
    """Verify oura_latest.json schema matches garmin_latest.json."""

    REQUIRED_KEYS = [
        "last_updated",
        "resting_hr",
        "daily_steps_avg",
        "sleep_regularity_stddev",
        "sleep_duration_avg",
        "vo2_max",
        "hrv_rmssd_avg",
        "zone2_min_per_week",
    ]

    def test_pull_all_returns_all_keys(self, tmp_path):
        client = OuraClient(data_dir=str(tmp_path))

        with patch.object(client, 'pull_sleep', return_value=[]), \
             patch.object(client, 'pull_sleep_periods', return_value=[]), \
             patch.object(client, 'pull_activity', return_value=[]), \
             patch.object(client, 'pull_readiness', return_value=[]):
            result = client.pull_all()

        for key in self.REQUIRED_KEYS:
            assert key in result, f"Missing key: {key}"

    def test_daily_series_schema(self, tmp_path):
        client = OuraClient(data_dir=str(tmp_path))
        series = client._build_daily_series([], [], [], days=3)
        assert len(series) == 3
        for entry in series:
            assert "date" in entry
            assert "rhr" in entry
            assert "hrv" in entry
            assert "steps" in entry
            assert "sleep_hrs" in entry
            assert "sleep_start" in entry
            assert "sleep_end" in entry


# =====================================================================
# Wearable fallback tests (briefing.py _load_wearable_data)
# =====================================================================


class TestWearableFallback:
    """Test that _load_wearable_data falls back: garmin > oura > whoop > apple_health."""

    def test_garmin_takes_priority(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        garmin = {"resting_hr": 55, "source": "garmin"}
        oura = {"resting_hr": 60, "source": "oura"}
        (tmp_path / "garmin_latest.json").write_text(json.dumps(garmin))
        (tmp_path / "oura_latest.json").write_text(json.dumps(oura))

        result = _load_wearable_data(tmp_path)
        assert result["resting_hr"] == 55

    def test_oura_fallback(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        oura = {"resting_hr": 60, "source": "oura"}
        (tmp_path / "oura_latest.json").write_text(json.dumps(oura))

        result = _load_wearable_data(tmp_path)
        assert result["resting_hr"] == 60

    def test_oura_before_apple(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        oura = {"resting_hr": 60, "source": "oura"}
        apple = {"resting_hr": 62, "source": "apple"}
        (tmp_path / "oura_latest.json").write_text(json.dumps(oura))
        (tmp_path / "apple_health_latest.json").write_text(json.dumps(apple))

        result = _load_wearable_data(tmp_path)
        assert result["resting_hr"] == 60

    def test_no_wearable(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        result = _load_wearable_data(tmp_path)
        assert result is None


# =====================================================================
# Auth tests
# =====================================================================


class TestOuraAuth:
    def test_service_name(self):
        assert AUTH_SERVICE_NAME == "oura"

    def test_default_scopes(self):
        assert "daily" in DEFAULT_SCOPES
        assert "sleep" in DEFAULT_SCOPES
        assert "heartrate" in DEFAULT_SCOPES
        assert "workout" in DEFAULT_SCOPES
        assert "personal" in DEFAULT_SCOPES

    def test_exchange_code_error_handling(self):
        """_exchange_code should return error dict on network failure."""
        with patch("engine.integrations.oura_auth.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = Exception("Connection refused")
            result = _exchange_code("code", "client_id", "secret", "http://localhost/callback")
            assert "error" in result
            assert result["error"] == "network_error"

    def test_gateway_auth_flow_saves_tokens(self, tmp_path):
        """run_gateway_auth_flow should save tokens on success."""
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        mock_response = json.dumps({
            "access_token": "test_access",
            "refresh_token": "test_refresh",
            "token_type": "Bearer",
            "expires_in": 86400,
        }).encode()

        with patch("engine.integrations.oura_auth.urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            result = run_gateway_auth_flow(
                code="test_code",
                client_id="cid",
                client_secret="csecret",
                redirect_uri="http://localhost/callback",
                user_id="paul",
                token_store=store,
            )

        assert result["authenticated"] is True
        assert result["user_id"] == "paul"

        # Verify tokens were saved
        saved = store.load_token("oura", "paul")
        assert saved is not None
        assert saved["access_token"] == "test_access"
        assert saved["refresh_token"] == "test_refresh"
        assert saved["client_id"] == "cid"
        assert saved["client_secret"] == "csecret"


# =====================================================================
# Token refresh tests
# =====================================================================


class TestTokenRefresh:
    def test_refresh_updates_token(self, tmp_path):
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        store.save_token("oura", "default", {
            "access_token": "old_token",
            "refresh_token": "refresh_123",
            "client_id": "cid",
            "client_secret": "csecret",
        })

        client = OuraClient(user_id="default", token_store=store)

        mock_response = json.dumps({
            "access_token": "new_token",
            "refresh_token": "new_refresh",
            "expires_in": 86400,
        }).encode()

        with patch("engine.integrations.oura.urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            result = client._refresh_token()

        assert result is True
        assert client._access_token == "new_token"

        # Verify saved
        saved = store.load_token("oura", "default")
        assert saved["access_token"] == "new_token"
        assert saved["refresh_token"] == "new_refresh"

    def test_refresh_fails_no_refresh_token(self, tmp_path):
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        store.save_token("oura", "default", {
            "access_token": "old_token",
            "client_id": "cid",
            "client_secret": "csecret",
        })

        client = OuraClient(user_id="default", token_store=store)
        assert client._refresh_token() is False


# =====================================================================
# MCP tool registration tests
# =====================================================================


class TestToolRegistry:
    def test_oura_tools_in_registry(self):
        from mcp_server.tools import TOOL_REGISTRY
        assert "pull_oura" in TOOL_REGISTRY
        assert "connect_oura" in TOOL_REGISTRY

    def test_connect_wearable_supports_oura(self):
        from mcp_server.tools import _connect_wearable
        # Should not return "unsupported" error for oura
        result = _connect_wearable("oura", user_id="nonexistent_test_user")
        assert "error" not in result or "Unsupported" not in result.get("error", "")
