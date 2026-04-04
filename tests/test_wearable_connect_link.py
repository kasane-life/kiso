"""Tests for deterministic wearable connect link in scheduled messages.

When a user has no wearable tokens, the scheduler should append a
connect link to the composed message. This is post-composition,
deterministic (not dependent on Sonnet including it).
"""

import uuid
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
from zoneinfo import ZoneInfo

from engine.gateway.scheduler import (
    _run_schedule,
    append_wearable_connect_link,
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
    actual_db_path = tmp_path / "data" / "kasane.db"
    from engine.gateway.db import init_db, get_db
    init_db(str(actual_db_path))
    conn = get_db(str(actual_db_path))
    return conn


@pytest.fixture
def db_with_mike(db):
    _insert_person(db, "mike-001", "Mike", "mike", "whatsapp", "+14155551234")
    return db


class TestAppendWearableConnectLink:
    """Unit tests for append_wearable_connect_link."""

    def test_appends_link_when_no_tokens(self, db_with_mike):
        """If user has no wearable tokens, append the connect link."""
        message = "Good morning Mike. Sleep data unavailable."
        mock_ts = MagicMock()
        mock_ts.has_token.return_value = False

        result = append_wearable_connect_link(
            message, "mike", mock_ts,
            base_url="https://auth.mybaseline.health",
            hmac_secret="test-secret",
        )
        assert "auth.mybaseline.health" in result
        assert "/auth/garmin" in result
        assert message in result  # original message preserved

    def test_no_append_when_garmin_connected(self, db_with_mike):
        """If user has Garmin tokens, don't append anything."""
        message = "Good morning Mike. HRV is 52."
        mock_ts = MagicMock()
        mock_ts.has_token.side_effect = lambda uid, svc: svc == "garmin"

        result = append_wearable_connect_link(
            message, "mike", mock_ts,
            base_url="https://auth.mybaseline.health",
            hmac_secret="test-secret",
        )
        assert result == message  # unchanged

    def test_no_append_when_oura_connected(self, db_with_mike):
        """Any wearable connected means no link appended."""
        message = "Good morning Mike."
        mock_ts = MagicMock()
        mock_ts.has_token.side_effect = lambda uid, svc: svc == "oura"

        result = append_wearable_connect_link(
            message, "mike", mock_ts,
            base_url="https://auth.mybaseline.health",
            hmac_secret="test-secret",
        )
        assert result == message

    def test_link_is_hmac_signed(self, db_with_mike):
        """The appended link should contain an HMAC state parameter."""
        message = "Morning check-in."
        mock_ts = MagicMock()
        mock_ts.has_token.return_value = False

        result = append_wearable_connect_link(
            message, "mike", mock_ts,
            base_url="https://auth.mybaseline.health",
            hmac_secret="test-secret",
        )
        assert "state=" in result
        assert "user=mike" in result

    def test_failure_returns_original_message(self, db_with_mike):
        """If token check raises, return the original message untouched."""
        message = "Good morning Mike."
        mock_ts = MagicMock()
        mock_ts.has_token.side_effect = Exception("DB error")

        result = append_wearable_connect_link(
            message, "mike", mock_ts,
            base_url="https://auth.mybaseline.health",
            hmac_secret="test-secret",
        )
        assert result == message


class TestSchedulerWearableLinkIntegration:
    """Integration: _run_schedule appends connect link for users without wearables."""

    @patch("engine.gateway.scheduler._compose_message", return_value="Good morning Mike. No data available yet.")
    @patch("engine.gateway.scheduler._gather_context", return_value={"checkin": {"test": True}})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_dry_run_includes_connect_link(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_mike):
        """Scheduled message for user without wearable should include connect link."""
        mock_persons.return_value = [
            {"id": "mike-001", "name": "Mike", "health_engine_user_id": "mike",
             "channel": "whatsapp", "channel_target": "+14155551234", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 4, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        with patch("engine.gateway.scheduler._get_token_store") as mock_get_ts:
            mock_ts = MagicMock()
            mock_ts.has_token.return_value = False
            mock_get_ts.return_value = mock_ts

            result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        msg = result["results"][0]["message"]
        assert "/auth/garmin" in msg
        assert "user=mike" in msg

    @patch("engine.gateway.scheduler._compose_message", return_value="Good morning Mike. HRV is 52, sleep was 7.1 hours.")
    @patch("engine.gateway.scheduler._gather_context", return_value={"checkin": {"test": True}})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_dry_run_no_link_when_connected(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_mike):
        """Scheduled message for connected user should NOT include connect link."""
        mock_persons.return_value = [
            {"id": "mike-001", "name": "Mike", "health_engine_user_id": "mike",
             "channel": "whatsapp", "channel_target": "+14155551234", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 4, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        with patch("engine.gateway.scheduler._get_token_store") as mock_get_ts:
            mock_ts = MagicMock()
            mock_ts.has_token.side_effect = lambda uid, svc: svc == "garmin"
            mock_get_ts.return_value = mock_ts

            result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        msg = result["results"][0]["message"]
        assert "/auth/garmin" not in msg
