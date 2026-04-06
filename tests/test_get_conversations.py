"""Tests for _get_conversations filtering.

Verifies that cron status codes (HEARTBEAT_OK, NO_REPLY) are excluded
from conversation context returned to the coaching agent. These messages
pollute Milo's context window — 220 of 1,539 messages (14%) on April 5.

Filter on read, not write: the raw data stays in the DB for debugging,
but the coaching agent never sees it.
"""

from datetime import datetime, timezone

import pytest

from engine.gateway.db import init_db, get_db, close_db
from mcp_server.tools import _get_conversations


@pytest.fixture
def conv_db(tmp_path, monkeypatch):
    """Fresh DB with conversation_message table and sample data."""
    close_db()

    # Patch PROJECT_ROOT so get_db() resolves to our test DB
    monkeypatch.setattr("mcp_server.tools.PROJECT_ROOT", tmp_path)

    db_path = tmp_path / "data" / "kasane.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(str(db_path))
    db = get_db(str(db_path))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Insert a mix of real coaching + cron noise
    rows = [
        # Real coaching messages (should appear)
        ("andrew", "user", "how am I doing?", "Andrew", "whatsapp", "sess1", now, now),
        ("andrew", "assistant", "Sleep was 7.2 hours. HRV at 58, solid recovery.", "Milo", "whatsapp", "sess1", now, now),
        # Cron status codes (should be FILTERED OUT)
        ("andrew", "assistant", "HEARTBEAT_OK", "Milo", "whatsapp", "sess1", now, now),
        ("andrew", "assistant", "NO_REPLY", "Milo", "whatsapp", "sess1", now, now),
        # Another user's real message + noise
        ("paul", "user", "logged 3 eggs", "Paul", "whatsapp", "sess2", now, now),
        ("paul", "assistant", "HEARTBEAT_OK", "Milo", "whatsapp", "sess2", now, now),
    ]
    for r in rows:
        db.execute(
            "INSERT INTO conversation_message "
            "(user_id, role, content, sender_name, channel, session_key, timestamp, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            r,
        )
    db.commit()

    yield db

    close_db()


class TestHeartbeatFiltering:
    """HEARTBEAT_OK and NO_REPLY cron status codes must not appear in results."""

    def test_filters_heartbeat_ok_for_specific_user(self, conv_db):
        result = _get_conversations(user_id="andrew", hours=24)
        contents = [m["content"] for m in result["conversations"]["andrew"]]

        assert "how am I doing?" in contents
        assert "Sleep was 7.2 hours. HRV at 58, solid recovery." in contents
        assert "HEARTBEAT_OK" not in contents
        assert "NO_REPLY" not in contents

    def test_filters_heartbeat_ok_all_users(self, conv_db):
        result = _get_conversations(hours=24)
        all_contents = []
        for user_msgs in result["conversations"].values():
            all_contents.extend(m["content"] for m in user_msgs)

        assert "HEARTBEAT_OK" not in all_contents
        assert "NO_REPLY" not in all_contents
        # Real messages still present
        assert "how am I doing?" in all_contents
        assert "logged 3 eggs" in all_contents

    def test_message_count_excludes_cron_noise(self, conv_db):
        result = _get_conversations(hours=24)
        # 6 total rows, 3 are cron noise → 3 real messages expected
        # andrew: 2 real (user + assistant), paul: 1 real (user)
        assert result["total_messages"] == 3


class TestAllUsersQuery:
    """None user_id must return conversations for ALL users, not just the authenticated one."""

    def test_none_user_id_returns_all_users(self, conv_db):
        result = _get_conversations(user_id=None, hours=24)
        assert "andrew" in result["users"]
        assert "paul" in result["users"]
        assert len(result["users"]) == 2

    def test_specific_user_id_filters(self, conv_db):
        result = _get_conversations(user_id="paul", hours=24)
        assert result["users"] == ["paul"]
        assert result["total_messages"] == 1

    def test_hours_window_returns_recent(self, conv_db):
        # All test data is "now", so hours=1 should return everything
        result = _get_conversations(hours=1)
        assert result["total_messages"] == 3
