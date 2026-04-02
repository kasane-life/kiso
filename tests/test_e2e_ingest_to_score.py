"""End-to-end test: ingest health data -> score reflects ingested values.

Verifies the full data flow that Paul/Teague rely on:
  BaselineSync -> ingest_health_snapshot -> wearable_daily SQLite + JSON
  -> _score / _checkin reads the data back.

This is the "sync -> MCP check-in -> see fresh data" loop.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from functools import partial

import pytest

from mcp_server.tools import _ingest_health_snapshot, _score, _checkin


def _recent_ts(hours_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat()


@pytest.fixture
def e2e_env(tmp_path):
    """Set up a complete test environment: user dir, SQLite DB, config."""
    from engine.gateway.db import init_db, get_db, close_db
    close_db()

    db_path = tmp_path / "kasane.db"
    init_db(db_path)
    db = get_db(db_path)

    # Create person row
    now = datetime.now(timezone.utc).isoformat()
    person_id = "p-e2e-001"
    user_id = "e2e_user"
    db.execute(
        "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (person_id, "E2E Test User", user_id, now, now),
    )
    db.commit()

    # Create data dir with minimal config
    data_dir = tmp_path / "data" / "users" / user_id
    data_dir.mkdir(parents=True)

    config = {
        "profile": {"age": 35, "sex": "M"},
        "data_dir": str(data_dir),
    }
    (data_dir / "config.yaml").write_text(
        "profile:\n  age: 35\n  sex: M\n"
    )

    yield {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "person_id": person_id,
        "user_id": user_id,
        "data_dir": data_dir,
    }

    close_db()


def _find_result(score, metric_name):
    """Find a metric result by name in score['results']."""
    for r in score.get("results", []):
        if r["name"] == metric_name:
            return r
    return None


class TestIngestToScore:
    """Ingest health data, then verify _score sees it."""

    def test_ingested_rhr_appears_in_score(self, e2e_env):
        """After ingesting resting_hr=54, _score should use that value."""
        uid = e2e_env["user_id"]
        data_dir = e2e_env["data_dir"]
        db_path = e2e_env["db_path"]

        with (
            patch("mcp_server.tools._data_dir", return_value=data_dir),
            patch("engine.gateway.db._db_path", return_value=db_path),
        ):
            # Ingest
            result = _ingest_health_snapshot(
                user_id=uid,
                metrics={
                    "resting_hr": 54.2,
                    "hrv_sdnn": 42.5,
                    "steps": 8450,
                    "sleep_hours": 7.1,
                    "vo2_max": 51.3,
                },
                timestamp=_recent_ts(1),
            )
            assert result["ingested"] is True

            # Score
            score = _score(user_id=uid)

        # results is a list of metric dicts with display names
        rhr = _find_result(score, "Resting Heart Rate")
        assert rhr is not None, (
            f"Resting Heart Rate not in scored results. Got: {[r['name'] for r in score.get('results', [])]}"
        )
        assert rhr["has_data"] is True
        assert rhr["value"] == pytest.approx(54.2, abs=1.0)

    def test_ingested_steps_appear_in_score(self, e2e_env):
        """After ingesting steps=8450, _score should reflect daily_steps_avg."""
        uid = e2e_env["user_id"]
        data_dir = e2e_env["data_dir"]
        db_path = e2e_env["db_path"]

        with (
            patch("mcp_server.tools._data_dir", return_value=data_dir),
            patch("engine.gateway.db._db_path", return_value=db_path),
        ):
            _ingest_health_snapshot(
                user_id=uid,
                metrics={"steps": 8450},
                timestamp=_recent_ts(1),
            )
            score = _score(user_id=uid)

        steps = _find_result(score, "Daily Steps")
        assert steps is not None, (
            f"Daily Steps not in scored results. Got: {[r['name'] for r in score.get('results', [])]}"
        )
        assert steps["has_data"] is True
        assert steps["value"] == pytest.approx(8450, abs=100)

    def test_no_data_before_ingest(self, e2e_env):
        """Before any ingest, wearable metrics should be absent from results."""
        uid = e2e_env["user_id"]
        data_dir = e2e_env["data_dir"]
        db_path = e2e_env["db_path"]

        with (
            patch("mcp_server.tools._data_dir", return_value=data_dir),
            patch("engine.gateway.db._db_path", return_value=db_path),
        ):
            score = _score(user_id=uid)

        # results only includes metrics with has_data=True
        rhr = _find_result(score, "Resting Heart Rate")
        assert rhr is None, "Expected no Resting Heart Rate in results before ingest"
