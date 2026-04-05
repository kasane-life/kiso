"""Tests for pre-compose gate: skip Sonnet for zero-data users.

When a user has no health data (0/20 coverage, no wearable, no logs),
the scheduler should send a hardcoded onboarding message instead of
calling Sonnet to compose from nothing.
"""

from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
from zoneinfo import ZoneInfo

from engine.gateway.scheduler import (
    _run_schedule,
    has_composable_data,
)


_NOW = "2026-04-04T00:00:00Z"


def _insert_person(db, id, name, user_id, channel=None, target=None, tz="America/Los_Angeles"):
    db.execute(
        "INSERT INTO person (id, name, health_engine_user_id, channel, channel_target, timezone, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (id, name, user_id, channel, target, tz, _NOW, _NOW),
    )
    db.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_server.tools.PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    db_path = tmp_path / "data" / "kasane.db"
    from engine.gateway.db import init_db, get_db
    init_db(str(db_path))
    conn = get_db(str(db_path))
    return conn


@pytest.fixture
def db_with_mike(db):
    _insert_person(db, "mike-001", "Mike", "mike", "whatsapp", "+14155551234")
    return db


class TestHasComposableData:
    """has_composable_data checks if context has real metrics to compose from."""

    def test_zero_data_returns_false(self):
        context = {
            "checkin": {
                "data_available": {
                    "garmin": False, "oura": False, "whoop": False,
                    "apple_health": False, "wearable_daily": False,
                    "weight_log": False, "bp_log": False, "meal_log": False,
                },
                "score": {"coverage": 0},
            }
        }
        assert has_composable_data(context) is False

    def test_has_wearable_data_returns_true(self):
        context = {
            "checkin": {
                "data_available": {
                    "garmin": True, "oura": False, "whoop": False,
                    "apple_health": False, "wearable_daily": True,
                    "weight_log": False, "bp_log": False, "meal_log": False,
                },
                "score": {"coverage": 6},
            }
        }
        assert has_composable_data(context) is True

    def test_has_manual_logs_returns_true(self):
        context = {
            "checkin": {
                "data_available": {
                    "garmin": False, "oura": False, "whoop": False,
                    "apple_health": False, "wearable_daily": False,
                    "weight_log": True, "bp_log": False, "meal_log": False,
                },
                "score": {"coverage": 1},
            }
        }
        assert has_composable_data(context) is True

    def test_error_context_returns_false(self):
        context = {"error": "Failed to gather context"}
        assert has_composable_data(context) is False

    def test_empty_context_returns_false(self):
        assert has_composable_data({}) is False

    def test_checkin_with_no_data_available_returns_false(self):
        context = {"checkin": {"score": {"coverage": 0}}}
        assert has_composable_data(context) is False


class TestZeroDataSchedulerIntegration:
    """_run_schedule should skip Sonnet for zero-data users."""

    @pytest.fixture(autouse=True)
    def mock_wearable_disconnected(self):
        from unittest.mock import MagicMock
        with patch("engine.gateway.scheduler._get_token_store") as mock_ts:
            ts = MagicMock()
            ts.has_token.return_value = False
            mock_ts.return_value = ts
            yield mock_ts

    @patch("engine.gateway.scheduler._gather_context")
    @patch("engine.gateway.scheduler._compose_message")
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_skips_zero_data_users_entirely(self, mock_audit, mock_persons, mock_now, mock_compose, mock_context, db_with_mike):
        """Zero-data user should be skipped entirely, no message sent."""
        mock_persons.return_value = [
            {"id": "mike-001", "name": "Mike", "health_engine_user_id": "mike",
             "channel": "whatsapp", "channel_target": "+14155551234", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 4, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        mock_context.return_value = {
            "checkin": {
                "data_available": {k: False for k in ["garmin", "oura", "whoop", "apple_health", "wearable_daily", "weight_log", "bp_log", "meal_log"]},
                "score": {"coverage": 0},
            }
        }

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        # Sonnet should NOT have been called
        mock_compose.assert_not_called()

        # User should be skipped, not sent anything
        assert result["results"][0]["status"] == "skip"
        assert "no data" in result["results"][0]["reason"]

    @patch("engine.gateway.scheduler._gather_context")
    @patch("engine.gateway.scheduler._compose_message", return_value="HRV is 52, sleep was 7.1 hours.")
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_calls_sonnet_when_data_exists(self, mock_audit, mock_persons, mock_now, mock_compose, mock_context, db_with_mike):
        """User with data should still get Sonnet-composed message."""
        mock_persons.return_value = [
            {"id": "mike-001", "name": "Mike", "health_engine_user_id": "mike",
             "channel": "whatsapp", "channel_target": "+14155551234", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 4, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        mock_context.return_value = {
            "checkin": {
                "data_available": {"garmin": True, "wearable_daily": True, "weight_log": False},
                "score": {"coverage": 6},
            }
        }

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        mock_compose.assert_called_once()
