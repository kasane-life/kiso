"""Tests for WHOOP integration (unit tests, no API calls)."""

import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.integrations.whoop import WhoopClient, SERVICE_NAME
from engine.integrations.whoop_auth import (
    run_auth_flow,
    run_gateway_auth_flow,
    _exchange_code,
    SERVICE_NAME as AUTH_SERVICE_NAME,
    DEFAULT_SCOPES,
)


# =====================================================================
# WhoopClient unit tests
# =====================================================================


class TestWhoopClientInit:
    def test_default_init(self):
        client = WhoopClient()
        assert client.user_id == "default"
        assert client.data_dir == Path("./data")

    def test_custom_init(self):
        client = WhoopClient(user_id="paul", data_dir="/tmp/health")
        assert client.user_id == "paul"
        assert client.data_dir == Path("/tmp/health")

    def test_from_config(self):
        config = {"data_dir": "/tmp/data"}
        client = WhoopClient.from_config(config, user_id="mike")
        assert client.user_id == "mike"
        assert str(client.data_dir) == "/tmp/data"

    def test_from_config_defaults(self):
        client = WhoopClient.from_config({})
        assert client.user_id == "default"


class TestWhoopHasTokens:
    def test_no_tokens(self, tmp_path):
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        assert WhoopClient.has_tokens(user_id="nobody", token_store=store) is False

    def test_with_tokens(self, tmp_path):
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        store._fernet = None  # Disable encryption for test simplicity
        store.save_token("whoop", "testuser", {"access_token": "test123"})
        assert WhoopClient.has_tokens(user_id="testuser", token_store=store) is True


# =====================================================================
# Metric extraction tests
# =====================================================================


class TestExtractRHR:
    def test_from_recovery(self):
        client = WhoopClient()
        recovery = [
            {"score": {"resting_heart_rate": 58}},
            {"score": {"resting_heart_rate": 60}},
            {"score": {"resting_heart_rate": 56}},
        ]
        result = client._extract_resting_hr(recovery)
        assert result == round(statistics.mean([58, 60, 56]), 1)

    def test_empty_recovery(self):
        client = WhoopClient()
        result = client._extract_resting_hr([])
        assert result is None

    def test_skips_zero(self):
        client = WhoopClient()
        recovery = [
            {"score": {"resting_heart_rate": 0}},
            {"score": {"resting_heart_rate": 55}},
        ]
        result = client._extract_resting_hr(recovery)
        assert result == 55.0


class TestExtractHRV:
    def test_from_recovery(self):
        client = WhoopClient()
        recovery = [
            {"score": {"hrv_rmssd_milli": 45.0}},
            {"score": {"hrv_rmssd_milli": 50.0}},
            {"score": {"hrv_rmssd_milli": 55.0}},
        ]
        result = client._extract_hrv(recovery)
        assert result == 50.0

    def test_empty_recovery(self):
        client = WhoopClient()
        result = client._extract_hrv([])
        assert result is None

    def test_skips_zero(self):
        client = WhoopClient()
        recovery = [
            {"score": {"hrv_rmssd_milli": 0}},
            {"score": {"hrv_rmssd_milli": 50.0}},
        ]
        result = client._extract_hrv(recovery)
        assert result == 50.0


class TestExtractSleepDuration:
    def test_basic(self):
        client = WhoopClient()
        sleep_data = [
            {"score": {"stage_summary": {"total_in_bed_time_milli": 7 * 3600 * 1000}}},  # 7 hours
            {"score": {"stage_summary": {"total_in_bed_time_milli": 8 * 3600 * 1000}}},  # 8 hours
        ]
        result = client._extract_sleep_duration(sleep_data)
        assert result == 7.5

    def test_empty(self):
        client = WhoopClient()
        result = client._extract_sleep_duration([])
        assert result is None

    def test_rejects_unreasonable(self):
        client = WhoopClient()
        sleep_data = [
            {"score": {"stage_summary": {"total_in_bed_time_milli": 100}}},  # ~0 hours
        ]
        result = client._extract_sleep_duration(sleep_data)
        assert result is None


class TestExtractSleepRegularity:
    def test_consistent_bedtime(self):
        client = WhoopClient()
        sleep_data = [
            {"start": "2026-03-20T22:30:00-07:00", "nap": False},
            {"start": "2026-03-21T22:35:00-07:00", "nap": False},
            {"start": "2026-03-22T22:25:00-07:00", "nap": False},
        ]
        result = client._extract_sleep_regularity(sleep_data)
        assert result is not None
        assert result < 10  # Very consistent

    def test_irregular_bedtime(self):
        client = WhoopClient()
        sleep_data = [
            {"start": "2026-03-20T21:00:00-07:00", "nap": False},
            {"start": "2026-03-21T01:00:00-07:00", "nap": False},
            {"start": "2026-03-22T23:00:00-07:00", "nap": False},
        ]
        result = client._extract_sleep_regularity(sleep_data)
        assert result is not None
        assert result > 30  # Irregular

    def test_skips_naps(self):
        client = WhoopClient()
        sleep_data = [
            {"start": "2026-03-20T22:30:00-07:00", "nap": False},
            {"start": "2026-03-20T14:00:00-07:00", "nap": True},
            {"start": "2026-03-21T22:30:00-07:00", "nap": False},
        ]
        result = client._extract_sleep_regularity(sleep_data)
        assert result is not None
        assert result < 5  # Very consistent (nap excluded)

    def test_insufficient_data(self):
        client = WhoopClient()
        sleep_data = [
            {"start": "2026-03-20T22:30:00-07:00", "nap": False},
        ]
        result = client._extract_sleep_regularity(sleep_data)
        assert result is None


class TestExtractZone2:
    def test_basic(self):
        client = WhoopClient()
        today = date.today()
        workouts = [
            {
                "start": (datetime.combine(today - timedelta(days=1), datetime.min.time())).isoformat() + "Z",
                "score": {"zone_durations": {"zone_two_milli": 30 * 60 * 1000}},  # 30 min
            },
            {
                "start": (datetime.combine(today - timedelta(days=2), datetime.min.time())).isoformat() + "Z",
                "score": {"zone_durations": {"zone_two_milli": 20 * 60 * 1000}},  # 20 min
            },
        ]
        result = client._extract_zone2_from_workouts(workouts, days=7)
        assert result == 50

    def test_excludes_old_data(self):
        client = WhoopClient()
        today = date.today()
        workouts = [
            {
                "start": (datetime.combine(today - timedelta(days=30), datetime.min.time())).isoformat() + "Z",
                "score": {"zone_durations": {"zone_two_milli": 60 * 60 * 1000}},
            },
        ]
        result = client._extract_zone2_from_workouts(workouts, days=7)
        assert result is None

    def test_no_zone_data(self):
        client = WhoopClient()
        result = client._extract_zone2_from_workouts([], days=7)
        assert result is None


# =====================================================================
# pull_all tests
# =====================================================================


class TestPullAll:
    def test_saves_whoop_latest(self, tmp_path):
        """pull_all should save whoop_latest.json with correct schema."""
        client = WhoopClient(data_dir=str(tmp_path))

        recovery = [
            {"score": {"resting_heart_rate": 58, "hrv_rmssd_milli": 50.0}},
        ]
        sleep = [
            {
                "start": "2026-03-20T22:30:00-07:00",
                "end": "2026-03-21T06:30:00-07:00",
                "nap": False,
                "score": {"stage_summary": {"total_in_bed_time_milli": 8 * 3600 * 1000}},
            },
            {
                "start": "2026-03-21T22:35:00-07:00",
                "end": "2026-03-22T06:35:00-07:00",
                "nap": False,
                "score": {"stage_summary": {"total_in_bed_time_milli": 8 * 3600 * 1000}},
            },
        ]

        with patch.object(client, 'pull_recovery', return_value=recovery), \
             patch.object(client, 'pull_sleep', return_value=sleep), \
             patch.object(client, 'pull_workouts', return_value=[]):
            result = client.pull_all()

        # Check return value schema matches garmin_latest.json
        assert "last_updated" in result
        assert "resting_hr" in result
        assert "hrv_rmssd_avg" in result
        assert "sleep_duration_avg" in result
        assert "sleep_regularity_stddev" in result
        assert "daily_steps_avg" in result
        assert "vo2_max" in result
        assert "zone2_min_per_week" in result

        # WHOOP-specific: no steps, no VO2 max
        assert result["daily_steps_avg"] is None
        assert result["vo2_max"] is None
        assert result["resting_hr"] == 58.0
        assert result["hrv_rmssd_avg"] == 50.0
        assert result["sleep_duration_avg"] == 8.0
        assert result["source"] == "whoop"

        # Check file was saved
        out_path = tmp_path / "whoop_latest.json"
        assert out_path.exists()
        saved = json.loads(out_path.read_text())
        assert saved["source"] == "whoop"
        assert saved["resting_hr"] == 58.0

    def test_saves_daily_series_with_history(self, tmp_path):
        """pull_all with history=True should save whoop_daily.json."""
        client = WhoopClient(data_dir=str(tmp_path))

        today = date.today()
        recovery = [
            {
                "created_at": f"{(today - timedelta(days=i)).isoformat()}T08:00:00Z",
                "score": {"resting_heart_rate": 55, "hrv_rmssd_milli": 50.0},
            }
            for i in range(5)
        ]
        sleep = [
            {
                "start": f"{(today - timedelta(days=i+1)).isoformat()}T22:30:00-07:00",
                "end": f"{(today - timedelta(days=i)).isoformat()}T06:30:00-07:00",
                "nap": False,
                "score": {"stage_summary": {"total_in_bed_time_milli": 8 * 3600 * 1000}},
            }
            for i in range(5)
        ]

        with patch.object(client, 'pull_recovery', return_value=recovery), \
             patch.object(client, 'pull_sleep', return_value=sleep), \
             patch.object(client, 'pull_workouts', return_value=[]):
            client.pull_all(history=True, history_days=5)

        series_path = tmp_path / "whoop_daily.json"
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
        out_path = tmp_path / "whoop_latest.json"
        out_path.write_text('{"existing": true}')

        client = WhoopClient(data_dir=str(tmp_path))

        with patch.object(client, 'pull_recovery', return_value=[]), \
             patch.object(client, 'pull_sleep', return_value=[]), \
             patch.object(client, 'pull_workouts', return_value=[]):
            result = client.pull_all()

        # All metrics should be None (except steps and vo2 which are always None)
        assert result["resting_hr"] is None
        assert result["hrv_rmssd_avg"] is None
        # File should still have old content
        assert json.loads(out_path.read_text()) == {"existing": True}


# =====================================================================
# Pagination tests
# =====================================================================


class TestPagination:
    def test_single_page(self):
        """Single page of results (no next_token)."""
        client = WhoopClient()
        client._token_data = {"access_token": "test"}
        client._access_token = "test"

        page1 = {"records": [{"id": 1}, {"id": 2}]}

        with patch.object(client, '_api_get', return_value=page1):
            result = client._api_get_all("recovery")

        assert len(result) == 2
        assert result[0]["id"] == 1

    def test_multi_page(self):
        """Multiple pages with next_token."""
        client = WhoopClient()
        client._token_data = {"access_token": "test"}
        client._access_token = "test"

        page1 = {"records": [{"id": 1}], "next_token": "abc123"}
        page2 = {"records": [{"id": 2}]}

        call_count = 0

        def mock_api_get(endpoint, params=None, retry_on_401=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1
            return page2

        with patch.object(client, '_api_get', side_effect=mock_api_get):
            result = client._api_get_all("recovery")

        assert len(result) == 2
        assert call_count == 2

    def test_empty_response(self):
        """Empty response returns empty list."""
        client = WhoopClient()
        client._token_data = {"access_token": "test"}
        client._access_token = "test"

        with patch.object(client, '_api_get', return_value=None):
            result = client._api_get_all("recovery")

        assert result == []


# =====================================================================
# Token storage tests
# =====================================================================


class TestWhoopTokenStorage:
    def test_save_and_load(self, tmp_path, monkeypatch):
        from engine.gateway.token_store import TokenStore
        from engine.gateway.db import init_db, close_db, get_db
        close_db()
        db_path = tmp_path / "test.db"
        init_db(db_path)
        monkeypatch.setattr("engine.gateway.token_store._get_db", lambda: get_db(db_path))

        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        token_data = {
            "access_token": "whoop_test_token",
            "refresh_token": "whoop_refresh",
            "client_id": "test_client",
            "client_secret": "test_secret",
            "scopes": ["read:recovery", "read:sleep"],
        }

        store.save_token("whoop", "testuser", token_data)
        loaded = store.load_token("whoop", "testuser")
        assert loaded == token_data
        close_db()

    def test_has_token(self, tmp_path, monkeypatch):
        from engine.gateway.token_store import TokenStore
        from engine.gateway.db import init_db, close_db, get_db
        close_db()
        db_path = tmp_path / "test.db"
        init_db(db_path)
        monkeypatch.setattr("engine.gateway.token_store._get_db", lambda: get_db(db_path))

        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        assert not store.has_token("whoop", "paul")
        store.save_token("whoop", "paul", {"access_token": "t"})
        assert store.has_token("whoop", "paul")
        close_db()


# =====================================================================
# Schema compatibility tests
# =====================================================================


class TestSchemaCompat:
    """Verify whoop_latest.json schema matches garmin_latest.json."""

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
        client = WhoopClient(data_dir=str(tmp_path))

        with patch.object(client, 'pull_recovery', return_value=[]), \
             patch.object(client, 'pull_sleep', return_value=[]), \
             patch.object(client, 'pull_workouts', return_value=[]):
            result = client.pull_all()

        for key in self.REQUIRED_KEYS:
            assert key in result, f"Missing key: {key}"

    def test_daily_series_schema(self, tmp_path):
        client = WhoopClient(data_dir=str(tmp_path))
        series = client._build_daily_series([], [], days=3)
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
# Wearable fallback tests
# =====================================================================


class TestWearableFallback:
    """Test that _load_wearable_data falls back: garmin > oura > whoop > apple_health."""

    def test_whoop_fallback(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        whoop = {"resting_hr": 57, "source": "whoop"}
        (tmp_path / "whoop_latest.json").write_text(json.dumps(whoop))

        result = _load_wearable_data(tmp_path)
        assert result["resting_hr"] == 57
        assert result["source"] == "whoop"

    def test_oura_before_whoop(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        oura = {"resting_hr": 60, "source": "oura"}
        whoop = {"resting_hr": 57, "source": "whoop"}
        (tmp_path / "oura_latest.json").write_text(json.dumps(oura))
        (tmp_path / "whoop_latest.json").write_text(json.dumps(whoop))

        result = _load_wearable_data(tmp_path)
        assert result["resting_hr"] == 60
        assert result["source"] == "oura"

    def test_whoop_before_apple(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        whoop = {"resting_hr": 57, "source": "whoop"}
        apple = {"resting_hr": 62, "source": "apple"}
        (tmp_path / "whoop_latest.json").write_text(json.dumps(whoop))
        (tmp_path / "apple_health_latest.json").write_text(json.dumps(apple))

        result = _load_wearable_data(tmp_path)
        assert result["resting_hr"] == 57
        assert result["source"] == "whoop"

    def test_garmin_over_whoop(self, tmp_path):
        from mcp_server.tools import _load_wearable_data

        garmin = {"resting_hr": 55, "source": "garmin"}
        whoop = {"resting_hr": 57, "source": "whoop"}
        (tmp_path / "garmin_latest.json").write_text(json.dumps(garmin))
        (tmp_path / "whoop_latest.json").write_text(json.dumps(whoop))

        result = _load_wearable_data(tmp_path)
        assert result["resting_hr"] == 55
        assert result["source"] == "garmin"


# =====================================================================
# Auth tests
# =====================================================================


class TestWhoopAuth:
    def test_service_name(self):
        assert AUTH_SERVICE_NAME == "whoop"

    def test_default_scopes(self):
        assert "read:recovery" in DEFAULT_SCOPES
        assert "read:sleep" in DEFAULT_SCOPES
        assert "read:workout" in DEFAULT_SCOPES
        assert "read:profile" in DEFAULT_SCOPES
        assert "read:body_measurement" in DEFAULT_SCOPES
        assert "read:cycles" in DEFAULT_SCOPES

    def test_exchange_code_error_handling(self):
        """_exchange_code should return error dict on network failure."""
        with patch("engine.integrations.whoop_auth.urllib.request.urlopen") as mock_open:
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

        with patch("engine.integrations.whoop_auth.urllib.request.urlopen") as mock_open:
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
        saved = store.load_token("whoop", "paul")
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

        store.save_token("whoop", "default", {
            "access_token": "old_token",
            "refresh_token": "refresh_123",
            "client_id": "cid",
            "client_secret": "csecret",
        })

        client = WhoopClient(user_id="default", token_store=store)

        mock_response = json.dumps({
            "access_token": "new_token",
            "refresh_token": "new_refresh",
            "expires_in": 86400,
        }).encode()

        with patch("engine.integrations.whoop.urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            result = client._refresh_token()

        assert result is True
        assert client._access_token == "new_token"

        # Verify saved
        saved = store.load_token("whoop", "default")
        assert saved["access_token"] == "new_token"
        assert saved["refresh_token"] == "new_refresh"

    def test_refresh_fails_no_refresh_token(self, tmp_path):
        from engine.gateway.token_store import TokenStore
        store = TokenStore(base_dir=tmp_path)
        store._fernet = None

        store.save_token("whoop", "default", {
            "access_token": "old_token",
            "client_id": "cid",
            "client_secret": "csecret",
        })

        client = WhoopClient(user_id="default", token_store=store)
        assert client._refresh_token() is False


# =====================================================================
# MCP tool registration tests
# =====================================================================


class TestToolRegistry:
    def test_whoop_tools_in_registry(self):
        from mcp_server.tools import TOOL_REGISTRY
        assert "pull_whoop" in TOOL_REGISTRY
        assert "connect_whoop" in TOOL_REGISTRY

    def test_connect_wearable_supports_whoop(self):
        from mcp_server.tools import _connect_wearable
        # Should not return "unsupported" error for whoop
        result = _connect_wearable("whoop", user_id="nonexistent_test_user")
        assert "error" not in result or "Unsupported" not in result.get("error", "")
