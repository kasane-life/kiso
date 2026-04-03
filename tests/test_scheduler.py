"""Tests for the deterministic scheduler (Option B).

Covers: time window checks, quiet hours, dedup logic, eligible person
filtering, full schedule run with mocked composition/delivery, and
audit logging.
"""

import json
import os
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from zoneinfo import ZoneInfo

from engine.gateway.config import GatewayConfig
from engine.gateway.server import create_app
from engine.gateway.scheduler import (
    _already_sent,
    _audit_scheduler,
    _get_eligible_persons,
    _in_quiet_hours,
    _in_window,
    _record_send,
    _run_schedule,
    _user_local_now,
)


# --- Fixtures ---

_NOW = "2026-04-02T00:00:00Z"


def _insert_person(db, id, name, user_id, channel=None, target=None, tz="America/Los_Angeles", deleted_at=None):
    """Insert a person with required NOT NULL fields."""
    db.execute(
        "INSERT INTO person (id, name, health_engine_user_id, channel, channel_target, timezone, created_at, updated_at, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, name, user_id, channel, target, tz, _NOW, _NOW, deleted_at),
    )
    db.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh SQLite database with schema applied."""
    monkeypatch.setattr("mcp_server.tools.PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    actual_db_path = tmp_path / "data" / "kasane.db"
    from engine.gateway.db import init_db, get_db
    init_db(str(actual_db_path))
    conn = get_db(str(actual_db_path))
    return conn


@pytest.fixture
def db_with_andrew(db):
    """Database with Andrew's person record populated."""
    _insert_person(db, "andrew-001", "Andrew", "andrew", "whatsapp", "+14152009584")
    return db


@pytest.fixture
def db_with_multi_tz(db):
    """Database with users in different timezones."""
    _insert_person(db, "andrew-001", "Andrew", "andrew", "whatsapp", "+14152009584")
    _insert_person(db, "grigoriy-001", "Grigoriy", "grigoriy", "telegram", "80135247", "Europe/Minsk")
    return db


@pytest.fixture
def audit_path(tmp_path, monkeypatch):
    """Redirect scheduler audit log to temp file."""
    path = str(tmp_path / "api_audit.jsonl")
    monkeypatch.setattr("engine.gateway.scheduler._AUDIT_LOG_PATH", path)
    return path


# --- Time window tests ---


class TestInWindow:
    def test_start_of_window(self):
        t = datetime(2026, 4, 2, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert _in_window(t, 7) is True

    def test_end_of_window(self):
        t = datetime(2026, 4, 2, 7, 29, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert _in_window(t, 7) is True

    def test_just_outside_window(self):
        t = datetime(2026, 4, 2, 7, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert _in_window(t, 7) is False

    def test_wrong_hour(self):
        t = datetime(2026, 4, 2, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert _in_window(t, 7) is False

    def test_custom_window_60_min(self):
        t = datetime(2026, 4, 2, 7, 45, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert _in_window(t, 7, window_minutes=60) is True

    def test_evening_window(self):
        t = datetime(2026, 4, 2, 20, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert _in_window(t, 20) is True

    def test_before_target_hour(self):
        t = datetime(2026, 4, 2, 6, 59, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert _in_window(t, 7) is False


class TestQuietHours:
    def test_late_night_is_quiet(self):
        t = datetime(2026, 4, 2, 22, 0)
        assert _in_quiet_hours(t) is True

    def test_early_morning_is_quiet(self):
        t = datetime(2026, 4, 2, 5, 0)
        assert _in_quiet_hours(t) is True

    def test_boundary_start(self):
        """9:15 PM is the start of quiet hours."""
        t = datetime(2026, 4, 2, 21, 15)
        assert _in_quiet_hours(t) is True

    def test_just_before_quiet(self):
        t = datetime(2026, 4, 2, 21, 14)
        assert _in_quiet_hours(t) is False

    def test_boundary_end(self):
        """6:00 AM is the end of quiet hours."""
        t = datetime(2026, 4, 2, 6, 0)
        assert _in_quiet_hours(t) is False

    def test_midday_not_quiet(self):
        t = datetime(2026, 4, 2, 12, 0)
        assert _in_quiet_hours(t) is False

    def test_7am_not_quiet(self):
        t = datetime(2026, 4, 2, 7, 0)
        assert _in_quiet_hours(t) is False


# --- Dedup tests ---


class TestDedup:
    def test_not_sent_yet(self, db_with_andrew):
        assert _already_sent(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02") is False

    def test_after_real_send(self, db_with_andrew):
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", status="sent")
        assert _already_sent(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02") is True

    def test_dry_run_does_not_block(self, db_with_andrew):
        """Critical: dry_run records must NOT prevent real sends."""
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", status="dry_run")
        assert _already_sent(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02") is False

    def test_failed_does_not_block(self, db_with_andrew):
        """Failed sends should allow retry."""
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", status="failed")
        assert _already_sent(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02") is False

    def test_different_day_not_blocked(self, db_with_andrew):
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-01", status="sent")
        assert _already_sent(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02") is False

    def test_different_schedule_type_not_blocked(self, db_with_andrew):
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", status="sent")
        assert _already_sent(db_with_andrew, "andrew-001", "evening_checkin", "2026-04-02") is False

    def test_real_send_overwrites_dry_run(self, db_with_andrew):
        """A real send must replace a prior dry_run record so dedup works."""
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", status="dry_run")
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", status="sent")
        assert _already_sent(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02") is True
        # Only one row should exist
        count = db_with_andrew.execute(
            "SELECT COUNT(*) FROM scheduled_send WHERE person_id = 'andrew-001' AND sent_date = '2026-04-02'"
        ).fetchone()[0]
        assert count == 1

    def test_preview_truncated(self, db_with_andrew):
        long_message = "x" * 500
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", preview=long_message)
        row = db_with_andrew.execute(
            "SELECT message_preview FROM scheduled_send WHERE person_id = 'andrew-001'"
        ).fetchone()
        assert len(row[0]) == 200


# --- Eligible persons tests ---


class TestEligiblePersons:
    def test_returns_configured_persons(self, db_with_andrew):
        persons = _get_eligible_persons(db_with_andrew)
        assert len(persons) == 1
        assert persons[0]["health_engine_user_id"] == "andrew"

    def test_skips_null_channel(self, db):
        _insert_person(db, "test-001", "Test", "test", channel=None, target="+1234")
        assert len(_get_eligible_persons(db)) == 0

    def test_skips_null_channel_target(self, db):
        _insert_person(db, "test-001", "Test", "test", channel="telegram", target=None)
        assert len(_get_eligible_persons(db)) == 0

    def test_skips_null_user_id(self, db):
        _insert_person(db, "test-001", "Test", None, channel="telegram", target="12345")
        assert len(_get_eligible_persons(db)) == 0

    def test_skips_deleted_persons(self, db):
        _insert_person(db, "test-001", "Test", "test", "whatsapp", "+1234", deleted_at="2026-04-01T00:00:00Z")
        assert len(_get_eligible_persons(db)) == 0

    def test_multi_timezone_users(self, db_with_multi_tz):
        persons = _get_eligible_persons(db_with_multi_tz)
        assert len(persons) == 2
        tzs = {p["health_engine_user_id"]: p["timezone"] for p in persons}
        assert tzs["andrew"] == "America/Los_Angeles"
        assert tzs["grigoriy"] == "Europe/Minsk"


# --- Full schedule run tests ---


class TestRunSchedule:
    """Test _run_schedule with mocked composition and delivery."""

    @patch("engine.gateway.scheduler._compose_message", return_value="Test morning brief message")
    @patch("engine.gateway.scheduler._gather_context", return_value={"checkin": {"test": True}})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_sends_when_in_window(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        assert result["eligible_count"] == 1
        assert result["results"][0]["status"] == "dry_run"
        assert result["results"][0]["channel"] == "whatsapp"
        assert result["results"][0]["message"] == "Test morning brief message"
        mock_compose.assert_called_once()

    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_skips_outside_window(self, mock_audit, mock_persons, mock_now, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        assert result["results"][0]["status"] == "skip"
        assert "not in window" in result["results"][0]["reason"]

    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_skips_not_friday_for_weekly(self, mock_audit, mock_persons, mock_now, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        # Wednesday
        mock_now.return_value = datetime(2026, 4, 1, 18, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("weekly_review", target_hour=18, require_friday=True, dry_run=True)

        assert result["results"][0]["status"] == "skip"
        assert "not Friday" in result["results"][0]["reason"]

    @patch("engine.gateway.scheduler._compose_message", return_value="Weekly review")
    @patch("engine.gateway.scheduler._gather_context", return_value={"score": {}})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_sends_on_friday_for_weekly(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        # Friday April 3, 2026
        mock_now.return_value = datetime(2026, 4, 3, 18, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("weekly_review", target_hour=18, require_friday=True, dry_run=True)

        assert result["results"][0]["status"] == "dry_run"

    @patch("engine.gateway.scheduler._compose_message", return_value="Brief")
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._send_via_openclaw", return_value={"status": "sent", "message_id": 123})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_real_send_calls_openclaw(self, mock_audit, mock_persons, mock_now, mock_send, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=False)

        assert result["results"][0]["status"] == "sent"
        mock_send.assert_called_once_with("whatsapp", "+14152009584", "Brief")

    @patch("engine.gateway.scheduler._compose_message", return_value="Brief")
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._send_via_openclaw", return_value={"status": "error", "error": "timeout"})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_failed_send_recorded(self, mock_audit, mock_persons, mock_now, mock_send, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=False)

        assert result["results"][0]["status"] == "failed"

    @patch("engine.gateway.scheduler._compose_message", return_value="Brief")
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_dedup_blocks_second_send(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        # First run
        _record_send(db_with_andrew, "andrew-001", "morning_brief", "2026-04-02", status="sent")

        # Second run should be blocked
        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        assert result["results"][0]["status"] == "skip"
        assert "already sent" in result["results"][0]["reason"]
        # compose should not have been called
        mock_compose.assert_not_called()

    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_bad_timezone_skips_gracefully(self, mock_audit, mock_persons, mock_now, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "Invalid/Timezone"},
        ]
        mock_now.side_effect = Exception("No time zone found with key 'Invalid/Timezone'")

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        assert result["results"][0]["status"] == "skip"
        assert "bad timezone" in result["results"][0]["reason"]

    @patch("engine.gateway.scheduler._compose_message", side_effect=Exception("API rate limit"))
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_compose_failure_skips_gracefully(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        assert result["results"][0]["status"] == "error"
        assert "compose failed" in result["results"][0]["reason"]


# --- Multi-timezone scenario ---


class TestMultiTimezone:
    @patch("engine.gateway.scheduler._compose_message", return_value="Brief")
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_only_sends_to_users_in_window(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_multi_tz):
        """When cron fires, only users whose local time is 7:00-7:29 get a message."""
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
            {"id": "grigoriy-001", "name": "Grigoriy", "health_engine_user_id": "grigoriy",
             "channel": "telegram", "channel_target": "80135247", "timezone": "Europe/Minsk"},
        ]

        def mock_local_now(tz_name):
            if tz_name == "America/Los_Angeles":
                return datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo(tz_name))  # 7:10 AM - in window
            else:
                return datetime(2026, 4, 2, 17, 10, tzinfo=ZoneInfo(tz_name))  # 5:10 PM - not in window

        mock_now.side_effect = mock_local_now

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        statuses = {r["user_id"]: r["status"] for r in result["results"]}
        assert statuses["andrew"] == "dry_run"
        assert statuses["grigoriy"] == "skip"


# --- Audit logging ---


class TestAuditLogging:
    def test_audit_writes_to_file(self, audit_path):
        summary = {
            "schedule_type": "morning_brief",
            "dry_run": False,
            "eligible_count": 2,
            "results": [
                {"user_id": "andrew", "status": "sent"},
                {"user_id": "grigoriy", "status": "skip", "reason": "not in window"},
            ],
        }
        _audit_scheduler("morning_brief", False, summary)

        with open(audit_path) as f:
            entry = json.loads(f.readline())

        assert entry["source"] == "scheduler"
        assert entry["schedule_type"] == "morning_brief"
        assert entry["sent"] == 1
        assert entry["skipped"] == 1
        assert entry["eligible"] == 2
        assert entry["dry_run"] is False


# --- Conversation ingestion tests ---


class TestConversationIngestion:
    """Scheduled messages must be written to conversation_message so Milo has context."""

    @patch("engine.gateway.scheduler._compose_message", return_value="Your HRV is 71ms. Focus on sleep tonight.")
    @patch("engine.gateway.scheduler._gather_context", return_value={"checkin": {"test": True}})
    @patch("engine.gateway.scheduler._send_via_openclaw", return_value={"status": "sent", "message_id": "abc123"})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_sent_message_ingested_to_conversation(self, mock_audit, mock_persons, mock_now, mock_send, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        _run_schedule("morning_brief", target_hour=7, dry_run=False)

        # Verify message was written to conversation_message table
        row = db_with_andrew.execute(
            "SELECT user_id, role, content, channel, sender_name FROM conversation_message ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "No conversation_message row found after send"
        assert row[0] == "andrew"       # user_id
        assert row[1] == "assistant"    # role
        assert "HRV is 71ms" in row[2]  # content matches composed message
        assert row[3] == "whatsapp"     # channel
        assert row[4] == "milo-scheduler"  # sender_name identifies source

    @patch("engine.gateway.scheduler._compose_message", return_value="Test message")
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_dry_run_does_not_ingest(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        _run_schedule("morning_brief", target_hour=7, dry_run=True)

        row = db_with_andrew.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
        assert row[0] == 0, "dry_run should not write to conversation_message"

    @patch("engine.gateway.scheduler._compose_message", return_value="Test message")
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._send_via_openclaw", return_value={"status": "error", "error": "timeout"})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_failed_send_does_not_ingest(self, mock_audit, mock_persons, mock_now, mock_send, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        _run_schedule("morning_brief", target_hour=7, dry_run=False)

        row = db_with_andrew.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
        assert row[0] == 0, "Failed send should not write to conversation_message"


# --- Conversation message dedup tests ---


class TestConversationDedup:
    """Verify that duplicate messages within 60s are skipped."""

    @patch("engine.gateway.scheduler._compose_message", return_value="Good morning! Here's your brief.")
    @patch("engine.gateway.scheduler._gather_context", return_value={})
    @patch("engine.gateway.scheduler._send_via_openclaw", return_value={"status": "sent", "message_id": "abc"})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_scheduler_skips_duplicate_within_60s(self, mock_audit, mock_persons, mock_now, mock_send, mock_context, mock_compose, db_with_andrew):
        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        # Simulate OpenClaw webhook arriving first
        db_with_andrew.execute(
            """INSERT INTO conversation_message
               (user_id, role, content, sender_id, sender_name, channel,
                session_key, message_id, timestamp, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("andrew", "assistant", "Good morning! Here's your brief.", "milo", "Milo", "whatsapp",
             "agent:main:whatsapp:direct:+14152009584", "webhook-123"),
        )
        db_with_andrew.commit()

        # Now scheduler runs and tries to ingest the same message
        _run_schedule("morning_brief", target_hour=7, dry_run=False)

        # Should have exactly 1 row (the webhook one), not 2
        count = db_with_andrew.execute("SELECT COUNT(*) FROM conversation_message").fetchone()[0]
        assert count == 1, f"Expected 1 message (deduped), got {count}"


# --- Manual send endpoint tests ---


class TestSendMessage:
    """POST /api/v1/send-message: manual send with conversation ingestion."""

    @patch("engine.gateway.scheduler._send_via_openclaw", return_value={"status": "sent", "message_id": "xyz"})
    def test_sends_and_ingests(self, mock_send, db_with_andrew):
        from engine.gateway.scheduler import _send_and_ingest
        result = _send_and_ingest(db_with_andrew, "andrew", "whatsapp", "+14152009584", "Test message")

        assert result["status"] == "sent"
        mock_send.assert_called_once_with("whatsapp", "+14152009584", "Test message")

        # Verify ingested to conversation_message
        row = db_with_andrew.execute(
            "SELECT user_id, role, content, channel, sender_name FROM conversation_message ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "andrew"
        assert row[1] == "assistant"
        assert row[2] == "Test message"
        assert row[4] == "milo-manual"

    @patch("engine.gateway.scheduler._send_via_openclaw", return_value={"status": "error", "error": "timeout"})
    def test_failed_send_not_ingested(self, mock_send, db_with_andrew):
        from engine.gateway.scheduler import _send_and_ingest
        result = _send_and_ingest(db_with_andrew, "andrew", "whatsapp", "+14152009584", "Test message")

        assert result["status"] == "error"
        row = db_with_andrew.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
        assert row[0] == 0

    def test_route_rejects_bad_token(self):
        config = GatewayConfig(port=18899, api_token="admin-token-xyz")
        app = create_app(config)
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/api/v1/send-message?token=wrong", json={"user_id": "andrew", "message": "hi"})
        assert resp.status_code == 403

    def test_route_requires_user_id_and_message(self):
        config = GatewayConfig(port=18899, api_token="admin-token-xyz")
        app = create_app(config)
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/api/v1/send-message?token=admin-token-xyz", json={})
        assert resp.status_code == 422


# --- Route auth tests ---


class TestSchedulerRoutes:
    @pytest.fixture
    def client(self):
        config = GatewayConfig(port=18899, api_token="admin-token-xyz")
        app = create_app(config)
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_rejects_bad_token(self, client):
        resp = client.post("/api/v1/scheduled/morning-brief?token=wrong")
        assert resp.status_code == 403

    def test_rejects_missing_token(self, client):
        resp = client.post("/api/v1/scheduled/morning-brief")
        assert resp.status_code == 403

    def test_rejects_per_user_token(self, client):
        """Per-user tokens should NOT be able to trigger scheduled sends."""
        resp = client.post("/api/v1/scheduled/morning-brief?token=some-user-token")
        assert resp.status_code == 403

    def test_accepts_admin_token(self, client):
        resp = client.post("/api/v1/scheduled/morning-brief?token=admin-token-xyz&dry_run=true")
        assert resp.status_code == 200

    def test_accepts_bearer_token(self, client):
        resp = client.post(
            "/api/v1/scheduled/morning-brief?dry_run=true",
            headers={"Authorization": "Bearer admin-token-xyz"},
        )
        assert resp.status_code == 200
