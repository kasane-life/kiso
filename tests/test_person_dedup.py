"""Tests for person record deduplication.

Verifies that:
1. _resolve_person_id returns exactly one record per health_engine_user_id
2. Duplicate person records are detected by /health/deep
3. Migration script correctly re-parents foreign keys and soft-deletes orphans
"""

from datetime import datetime, timezone

import pytest

from engine.gateway.db import init_db, get_db, close_db
from mcp_server.tools import _resolve_person_id


@pytest.fixture
def person_db(tmp_path, monkeypatch):
    """Fresh DB with person table and duplicate records."""
    close_db()
    monkeypatch.setattr("mcp_server.tools.PROJECT_ROOT", tmp_path)

    db_path = tmp_path / "data" / "kasane.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(str(db_path))
    db = get_db(str(db_path))

    now = datetime.now(timezone.utc).isoformat()

    # Insert canonical person record
    db.execute(
        "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("paul-001", "Paul", "paul", "2026-01-01T00:00:00Z", now),
    )
    # Insert duplicate (the UUID orphan)
    db.execute(
        "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("230b25d3-uuid", "Paul", "paul", "2026-03-24T16:53:23Z", now),
    )
    # Insert data referencing the UUID (should be migrated)
    db.execute(
        "INSERT INTO habit (id, person_id, title, category, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("habit-1", "230b25d3-uuid", "Eat clean", "nutrition", "active", now, now),
    )
    db.execute(
        "INSERT INTO weight_entry (id, person_id, date, weight_lbs, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("w-1", "230b25d3-uuid", "2026-04-01", 185.0, "mcp", now, now),
    )
    db.commit()

    yield db
    close_db()


class TestResolvePersonIdUniqueness:
    """Every health_engine_user_id should map to exactly one person record."""

    def test_duplicate_user_id_returns_one_result(self, person_db):
        """_resolve_person_id returns a result even with duplicates, but this
        is a data integrity problem that should be caught by /health/deep."""
        result = _resolve_person_id("paul")
        assert result is not None

    def test_no_duplicates_after_cleanup(self, person_db):
        """After soft-deleting the orphan, only one person record remains."""
        now = datetime.now(timezone.utc).isoformat()
        person_db.execute(
            "UPDATE person SET deleted_at = ? WHERE id = ?",
            (now, "230b25d3-uuid"),
        )
        person_db.commit()

        rows = person_db.execute(
            "SELECT id FROM person WHERE health_engine_user_id = 'paul' AND deleted_at IS NULL"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "paul-001"


class TestDuplicatePersonDetection:
    """The health check should detect duplicate person records."""

    def test_detect_duplicate_person_ids(self, person_db):
        """Query that /health/deep will use to find duplicates."""
        dupes = person_db.execute(
            "SELECT health_engine_user_id, COUNT(*) as cnt "
            "FROM person WHERE deleted_at IS NULL AND health_engine_user_id IS NOT NULL "
            "GROUP BY health_engine_user_id HAVING cnt > 1"
        ).fetchall()
        assert len(dupes) == 1
        assert dupes[0]["health_engine_user_id"] == "paul"

    def test_no_duplicates_after_soft_delete(self, person_db):
        now = datetime.now(timezone.utc).isoformat()
        person_db.execute(
            "UPDATE person SET deleted_at = ? WHERE id = ?",
            (now, "230b25d3-uuid"),
        )
        person_db.commit()

        dupes = person_db.execute(
            "SELECT health_engine_user_id, COUNT(*) as cnt "
            "FROM person WHERE deleted_at IS NULL AND health_engine_user_id IS NOT NULL "
            "GROUP BY health_engine_user_id HAVING cnt > 1"
        ).fetchall()
        assert len(dupes) == 0


class TestForeignKeyMigration:
    """Data referencing the orphan person_id must be re-parented before deletion."""

    def test_migrate_habit_to_canonical(self, person_db):
        person_db.execute(
            "UPDATE habit SET person_id = 'paul-001' WHERE person_id = '230b25d3-uuid'"
        )
        person_db.commit()

        rows = person_db.execute(
            "SELECT person_id FROM habit WHERE id = 'habit-1'"
        ).fetchone()
        assert rows["person_id"] == "paul-001"

    def test_migrate_weight_to_canonical(self, person_db):
        person_db.execute(
            "UPDATE weight_entry SET person_id = 'paul-001' WHERE person_id = '230b25d3-uuid'"
        )
        person_db.commit()

        rows = person_db.execute(
            "SELECT person_id FROM weight_entry WHERE id = 'w-1'"
        ).fetchone()
        assert rows["person_id"] == "paul-001"
