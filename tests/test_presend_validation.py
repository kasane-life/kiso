"""Tests for pre-send coaching validation.

Covers: source change detection in wearable_daily, coaching message
validation that flags metric claims when the data source changed.
"""

import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from zoneinfo import ZoneInfo

from engine.gateway.scheduler import (
    _run_schedule,
    detect_source_changes,
    validate_coaching_claims,
)


# --- Fixtures ---

_NOW = "2026-04-02T00:00:00Z"


def _insert_person(db, id, name, user_id, channel=None, target=None, tz="America/Los_Angeles"):
    db.execute(
        "INSERT INTO person (id, name, health_engine_user_id, channel, channel_target, timezone, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (id, name, user_id, channel, target, tz, _NOW, _NOW),
    )
    db.commit()


def _insert_wearable(db, person_id, date, source, vo2_max=None, rhr=None, hrv=None, sleep_hrs=None):
    """Insert a wearable_daily row for testing."""
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{person_id}:wearable_daily:{date}:{source}"))
    now = datetime.utcnow().isoformat() + "Z"
    db.execute(
        "INSERT OR REPLACE INTO wearable_daily "
        "(id, person_id, date, source, vo2_max, rhr, hrv, sleep_hrs, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, person_id, date, source, vo2_max, rhr, hrv, sleep_hrs, now, now),
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
    _insert_person(db, "andrew-001", "Andrew", "andrew", "whatsapp", "+14152009584")
    return db


# --- detect_source_changes tests ---


class TestDetectSourceChanges:
    def test_no_data_returns_empty(self, db_with_andrew):
        result = detect_source_changes(db_with_andrew, "andrew-001")
        assert result == {}

    def test_single_source_no_change(self, db_with_andrew):
        """All garmin data, no source change."""
        for day in range(1, 8):
            _insert_wearable(db_with_andrew, "andrew-001", f"2026-04-{day:02d}", "garmin", vo2_max=47.0)
        result = detect_source_changes(db_with_andrew, "andrew-001")
        assert result == {}

    def test_vo2_source_change_detected(self, db_with_andrew):
        """Garmin VO2 for 5 days, then Apple Health VO2. Should flag vo2_max."""
        for day in range(1, 6):
            _insert_wearable(db_with_andrew, "andrew-001", f"2026-03-{25 + day:02d}", "garmin", vo2_max=47.0)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=32.3)

        result = detect_source_changes(db_with_andrew, "andrew-001", days=7)
        assert "vo2_max" in result
        assert result["vo2_max"]["old_source"] == "garmin"
        assert result["vo2_max"]["new_source"] == "apple_health"

    def test_rhr_source_change_detected(self, db_with_andrew):
        """RHR switches from oura to garmin."""
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "oura", rhr=52)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "garmin", rhr=48)

        result = detect_source_changes(db_with_andrew, "andrew-001", days=7)
        assert "rhr" in result
        assert result["rhr"]["old_source"] == "oura"
        assert result["rhr"]["new_source"] == "garmin"

    def test_ignores_changes_outside_window(self, db_with_andrew):
        """Source change 30 days ago should not be flagged."""
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-01", "garmin", vo2_max=47.0)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-05", "apple_health", vo2_max=32.3)
        # Recent data all from one source
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=33.0)

        result = detect_source_changes(db_with_andrew, "andrew-001", days=7)
        assert "vo2_max" not in result

    def test_multiple_metrics_can_change(self, db_with_andrew):
        """Both VO2 and HRV change source."""
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "garmin", vo2_max=47.0, hrv=65)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=32.3, hrv=45)

        result = detect_source_changes(db_with_andrew, "andrew-001", days=7)
        assert "vo2_max" in result
        assert "hrv" in result


# --- validate_coaching_claims tests ---


class TestValidateCoachingClaims:
    def test_no_source_changes_returns_empty(self, db_with_andrew):
        message = "Your VO2 max dropped to 32. This is concerning."
        warnings = validate_coaching_claims(message, "andrew-001", db_with_andrew)
        assert warnings == []

    def test_flags_vo2_drop_with_source_change(self, db_with_andrew):
        """The Grigoriy scenario: VO2 mentioned + source changed = warning."""
        # Set up the source change
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "garmin", vo2_max=47.0)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=32.3)

        message = "Your VO2 max dropped to 32. This is a significant decline."
        warnings = validate_coaching_claims(message, "andrew-001", db_with_andrew)
        assert len(warnings) >= 1
        assert any("vo2" in w.lower() for w in warnings)
        assert any("source" in w.lower() for w in warnings)

    def test_flags_hrv_mention_with_source_change(self, db_with_andrew):
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "oura", hrv=65)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "garmin", hrv=50)

        message = "HRV is down to 50, which is below your baseline."
        warnings = validate_coaching_claims(message, "andrew-001", db_with_andrew)
        assert len(warnings) >= 1
        assert any("hrv" in w.lower() for w in warnings)

    def test_no_flag_when_metric_not_mentioned(self, db_with_andrew):
        """Source changed for VO2 but message doesn't mention VO2."""
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "garmin", vo2_max=47.0)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=32.3)

        message = "Great sleep last night. 7.5 hours with good deep sleep."
        warnings = validate_coaching_claims(message, "andrew-001", db_with_andrew)
        assert warnings == []

    def test_flags_rhr_mention(self, db_with_andrew):
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "oura", rhr=52)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "garmin", rhr=48)

        message = "Resting heart rate improved to 48, down from 52."
        warnings = validate_coaching_claims(message, "andrew-001", db_with_andrew)
        assert len(warnings) >= 1

    def test_multiple_flagged_metrics(self, db_with_andrew):
        """Message mentions both VO2 and HRV, both have source changes."""
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "garmin", vo2_max=47.0, hrv=65)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=32.3, hrv=45)

        message = "VO2 max dropped to 32 and HRV is at 45. Both concerning."
        warnings = validate_coaching_claims(message, "andrew-001", db_with_andrew)
        assert len(warnings) >= 2


# --- Integration: validation wired into _run_schedule ---


class TestValidationInScheduler:
    """Verify that _run_schedule annotates messages when source changes are detected."""

    @pytest.fixture(autouse=True)
    def mock_wearable_connected(self):
        from unittest.mock import MagicMock
        with patch("engine.gateway.scheduler._get_token_store") as mock_ts:
            ts = MagicMock()
            ts.has_token.return_value = True
            mock_ts.return_value = ts
            yield mock_ts

    @patch("engine.gateway.scheduler._compose_message", return_value="Your VO2 max dropped to 32. Concerning decline.")
    @patch("engine.gateway.scheduler._gather_context", return_value={"checkin": {"test": True}})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_dry_run_message_annotated_on_source_change(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        """When source changed and message mentions the metric, dry_run output includes disclaimer."""
        # Set up source change in wearable_daily
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "garmin", vo2_max=47.0)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=32.3)

        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        msg = result["results"][0]["message"]
        assert "source change" in msg.lower()
        assert "vo2_max" in msg.lower()

    @patch("engine.gateway.scheduler._compose_message", return_value="Great sleep last night, 7.5 hours.")
    @patch("engine.gateway.scheduler._gather_context", return_value={"checkin": {"test": True}})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_no_annotation_when_metric_not_mentioned(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        """Source changed for VO2, but message doesn't mention it. No disclaimer."""
        _insert_wearable(db_with_andrew, "andrew-001", "2026-03-28", "garmin", vo2_max=47.0)
        _insert_wearable(db_with_andrew, "andrew-001", "2026-04-01", "apple_health", vo2_max=32.3)

        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        msg = result["results"][0]["message"]
        assert "source change" not in msg.lower()

    @patch("engine.gateway.scheduler._compose_message", return_value="Your VO2 max is 47. Looking strong.")
    @patch("engine.gateway.scheduler._gather_context", return_value={"checkin": {"test": True}})
    @patch("engine.gateway.scheduler._user_local_now")
    @patch("engine.gateway.scheduler._get_eligible_persons")
    @patch("engine.gateway.scheduler._audit_scheduler")
    def test_no_annotation_when_no_source_change(self, mock_audit, mock_persons, mock_now, mock_context, mock_compose, db_with_andrew):
        """Single source throughout. No disclaimer even though VO2 is mentioned."""
        for day in range(1, 8):
            _insert_wearable(db_with_andrew, "andrew-001", f"2026-04-{day:02d}", "garmin", vo2_max=47.0)

        mock_persons.return_value = [
            {"id": "andrew-001", "name": "Andrew", "health_engine_user_id": "andrew",
             "channel": "whatsapp", "channel_target": "+14152009584", "timezone": "America/Los_Angeles"},
        ]
        mock_now.return_value = datetime(2026, 4, 2, 7, 10, tzinfo=ZoneInfo("America/Los_Angeles"))

        result = _run_schedule("morning_brief", target_hour=7, dry_run=True)

        msg = result["results"][0]["message"]
        assert "source change" not in msg.lower()
