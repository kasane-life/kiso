"""Tests for per-user issue tracking pipeline.

Covers: schema creation, issue CRUD, dedup (no duplicate open issues),
signal-to-issue creation, audit error spike detection, auto-resolve.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.gateway.db import init_db, get_db, close_db
from engine.gateway.config import GatewayConfig


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite database with schema."""
    close_db()
    db_path = tmp_path / "kasane.db"
    init_db(db_path)
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("g1", "Grigoriy", "grigoriy", now, now),
    )
    conn.execute(
        "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("a1", "Andrew", "andrew", now, now),
    )
    conn.commit()
    yield conn
    close_db()


# --- Schema ---


class TestUserIssueSchema:
    def test_table_exists(self, db):
        """user_issue table must exist after init_db."""
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "user_issue" in tables

    def test_required_columns(self, db):
        """Table must have all required columns."""
        cols = {r[1] for r in db.execute("PRAGMA table_info(user_issue)").fetchall()}
        expected = {"id", "person_id", "category", "title", "detail",
                    "status", "source", "created_at", "resolved_at", "dedup_key"}
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"


# --- CRUD ---


class TestIssueCRUD:
    def test_create_issue(self, db):
        from engine.gateway.issues import create_issue
        issue = create_issue(db, "g1", "auth_failure",
                             "Garmin 429 rate limit", detail="Blocked until Apr 4")
        assert issue["id"]
        assert issue["status"] == "open"
        assert issue["person_id"] == "g1"
        assert issue["category"] == "auth_failure"

    def test_list_open_issues(self, db):
        from engine.gateway.issues import create_issue, list_issues
        create_issue(db, "g1", "auth_failure", "Garmin 429")
        create_issue(db, "g1", "stale_data", "No wearable sync in 48h")

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 2

    def test_list_all_users(self, db):
        from engine.gateway.issues import create_issue, list_issues
        create_issue(db, "g1", "auth_failure", "Garmin 429")
        create_issue(db, "a1", "stale_data", "Apple Health stale")

        issues = list_issues(db)
        assert len(issues) == 2

    def test_resolve_issue(self, db):
        from engine.gateway.issues import create_issue, resolve_issue, list_issues
        issue = create_issue(db, "g1", "auth_failure", "Garmin 429")
        resolve_issue(db, issue["id"])

        open_issues = list_issues(db, person_id="g1", status="open")
        resolved = list_issues(db, person_id="g1", status="resolved")
        assert len(open_issues) == 0
        assert len(resolved) == 1
        assert resolved[0]["resolved_at"] is not None

    def test_dedup_prevents_duplicate_open_issue(self, db):
        """Same category + person should not create duplicate open issues."""
        from engine.gateway.issues import create_issue, list_issues
        create_issue(db, "g1", "auth_failure", "Garmin 429", dedup_key="garmin_auth:g1")
        create_issue(db, "g1", "auth_failure", "Garmin 429 again", dedup_key="garmin_auth:g1")

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 1, "Duplicate open issue was created"

    def test_dedup_allows_after_resolve(self, db):
        """After resolving, same dedup_key should create a new issue."""
        from engine.gateway.issues import create_issue, resolve_issue, list_issues
        issue = create_issue(db, "g1", "auth_failure", "Garmin 429", dedup_key="garmin_auth:g1")
        resolve_issue(db, issue["id"])
        create_issue(db, "g1", "auth_failure", "Garmin 429 recurred", dedup_key="garmin_auth:g1")

        open_issues = list_issues(db, person_id="g1", status="open")
        assert len(open_issues) == 1


# --- Signal-to-issue pipeline ---


class TestSignalToIssue:
    def test_quiet_signal_creates_issue(self, db):
        from engine.gateway.issues import process_signals, list_issues
        signals = [{"person_id": "g1", "signal": "quiet 3d"}]
        process_signals(db, signals)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 1
        assert issues[0]["category"] == "engagement"
        assert "quiet" in issues[0]["title"].lower()

    def test_stale_garmin_creates_issue(self, db):
        from engine.gateway.issues import process_signals, list_issues
        signals = [{"person_id": "g1", "signal": "garmin stale"}]
        process_signals(db, signals)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 1
        assert issues[0]["category"] == "stale_data"

    def test_no_wearable_creates_issue(self, db):
        from engine.gateway.issues import process_signals, list_issues
        signals = [{"person_id": "g1", "signal": "no wearable"}]
        process_signals(db, signals)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 1
        assert issues[0]["category"] == "onboarding"

    def test_streak_broken_creates_issue(self, db):
        from engine.gateway.issues import process_signals, list_issues
        signals = [{"person_id": "g1", "signal": "streak broken"}]
        process_signals(db, signals)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 1
        assert issues[0]["category"] == "engagement"

    def test_signal_dedup(self, db):
        """Running signals twice should not create duplicates."""
        from engine.gateway.issues import process_signals, list_issues
        signals = [{"person_id": "g1", "signal": "garmin stale"}]
        process_signals(db, signals)
        process_signals(db, signals)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 1

    def test_signal_auto_resolves_when_cleared(self, db):
        """If a signal was open but is no longer firing, auto-resolve it."""
        from engine.gateway.issues import process_signals, create_issue, list_issues
        # Previously created stale_data issue
        create_issue(db, "g1", "stale_data", "Garmin stale",
                     source="signal", dedup_key="signal:garmin stale:g1")

        # Now signals run but garmin stale is gone for g1
        current_signals = [{"person_id": "a1", "signal": "garmin stale"}]
        process_signals(db, current_signals, all_person_ids=["g1", "a1"])

        g_issues = list_issues(db, person_id="g1", status="open")
        assert len(g_issues) == 0, "Stale issue should auto-resolve when signal clears"


# --- Audit error spike detection ---


class TestAuditErrorSpike:
    def test_detects_error_spike(self, tmp_path, db):
        """3+ errors in 24h for one user should create an issue."""
        from engine.gateway.issues import check_audit_errors, list_issues

        audit_path = tmp_path / "api_audit.jsonl"
        now = datetime.now(timezone.utc)
        lines = []
        for i in range(4):
            ts = (now - timedelta(hours=i)).isoformat()
            lines.append(json.dumps({
                "ts": ts, "tool": "pull_garmin", "user_id": "grigoriy",
                "status": "error", "error": f"429 rate limit attempt {i}",
            }))
        # One ok entry (should not count)
        lines.append(json.dumps({
            "ts": now.isoformat(), "tool": "checkin", "user_id": "grigoriy",
            "status": "ok",
        }))
        audit_path.write_text("\n".join(lines) + "\n")

        check_audit_errors(db, str(audit_path), threshold=3, window_hours=24)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 1
        assert issues[0]["category"] == "error_spike"
        assert "4 errors" in issues[0]["title"]

    def test_no_spike_below_threshold(self, tmp_path, db):
        """2 errors (below threshold of 3) should not create an issue."""
        from engine.gateway.issues import check_audit_errors, list_issues

        audit_path = tmp_path / "api_audit.jsonl"
        now = datetime.now(timezone.utc)
        lines = []
        for i in range(2):
            ts = (now - timedelta(hours=i)).isoformat()
            lines.append(json.dumps({
                "ts": ts, "tool": "pull_garmin", "user_id": "grigoriy",
                "status": "error", "error": "429",
            }))
        audit_path.write_text("\n".join(lines) + "\n")

        check_audit_errors(db, str(audit_path), threshold=3, window_hours=24)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 0

    def test_old_errors_outside_window(self, tmp_path, db):
        """Errors older than window_hours should not count."""
        from engine.gateway.issues import check_audit_errors, list_issues

        audit_path = tmp_path / "api_audit.jsonl"
        now = datetime.now(timezone.utc)
        lines = []
        for i in range(5):
            ts = (now - timedelta(hours=48 + i)).isoformat()
            lines.append(json.dumps({
                "ts": ts, "tool": "pull_garmin", "user_id": "grigoriy",
                "status": "error", "error": "stale error",
            }))
        audit_path.write_text("\n".join(lines) + "\n")

        check_audit_errors(db, str(audit_path), threshold=3, window_hours=24)

        issues = list_issues(db, person_id="g1", status="open")
        assert len(issues) == 0


# --- API endpoint tests ---


class TestIssuesAPI:
    @pytest.fixture
    def client(self, db):
        """TestClient that shares the same DB connection as the db fixture."""
        from engine.gateway.server import create_app
        from fastapi.testclient import TestClient
        config = GatewayConfig(port=18899, api_token="admin-token-xyz")
        # Patch get_db to return the fixture's connection (already init'd at tmp_path)
        with patch("engine.gateway.v1_api.get_db", return_value=db):
            app = create_app(config)
            yield TestClient(app)

    def test_list_issues_empty(self, client):
        resp = client.get("/api/v1/issues?token=admin-token-xyz")
        assert resp.status_code == 200
        assert resp.json()["issues"] == []

    def test_list_issues_with_data(self, client, db):
        from engine.gateway.issues import create_issue
        create_issue(db, "g1", "auth_failure", "Garmin 429")
        create_issue(db, "a1", "stale_data", "Apple Health stale")

        resp = client.get("/api/v1/issues?token=admin-token-xyz")
        assert resp.status_code == 200
        assert len(resp.json()["issues"]) == 2

    def test_filter_by_person(self, client, db):
        from engine.gateway.issues import create_issue
        create_issue(db, "g1", "auth_failure", "Garmin 429")
        create_issue(db, "a1", "stale_data", "Apple Health stale")

        resp = client.get("/api/v1/issues?token=admin-token-xyz&person_id=g1")
        assert resp.status_code == 200
        assert len(resp.json()["issues"]) == 1
        assert resp.json()["issues"][0]["category"] == "auth_failure"

    def test_filter_by_status(self, client, db):
        from engine.gateway.issues import create_issue, resolve_issue
        issue = create_issue(db, "g1", "auth_failure", "Garmin 429")
        resolve_issue(db, issue["id"])
        create_issue(db, "g1", "stale_data", "Garmin stale")

        resp = client.get("/api/v1/issues?token=admin-token-xyz&status=open")
        assert resp.status_code == 200
        assert len(resp.json()["issues"]) == 1

    def test_resolve_via_api(self, client, db):
        from engine.gateway.issues import create_issue
        issue = create_issue(db, "g1", "auth_failure", "Garmin 429")

        resp = client.post(
            f"/api/v1/issues/{issue['id']}/resolve?token=admin-token-xyz"
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    def test_rejects_bad_token(self, client):
        resp = client.get("/api/v1/issues?token=wrong")
        assert resp.status_code == 403
