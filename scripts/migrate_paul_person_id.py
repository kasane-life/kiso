#!/usr/bin/env python3
"""One-time migration: re-parent Paul's data from UUID to paul-001, then soft-delete the UUID record.

Context: Paul has two person records:
  - paul-001 (seed, canonical, no child data)
  - 230b25d3-4352-551d-b3e1-c8484d454db8 (iOS-created, has 23 habits + 5 wearable_daily + 1 weight)

This script migrates all foreign key references to paul-001 and soft-deletes the UUID record.

Safe to run multiple times (idempotent).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.gateway.db import init_db, get_db

UUID = "230b25d3-4352-551d-b3e1-c8484d454db8"
CANONICAL = "paul-001"

# All tables with person_id foreign keys
TABLES_WITH_PERSON_ID = [
    "habit", "checkin", "focus_plan", "wearable_daily", "lab_result",
    "meal", "weight_entry", "training_session", "workout_program",
    "program_day", "supplement_log", "medication_log", "blood_pressure",
]


def main():
    init_db()
    db = get_db()

    # Verify both records exist
    uuid_row = db.execute("SELECT id FROM person WHERE id = ? AND deleted_at IS NULL", (UUID,)).fetchone()
    canon_row = db.execute("SELECT id FROM person WHERE id = ? AND deleted_at IS NULL", (CANONICAL,)).fetchone()

    if not uuid_row:
        print(f"UUID record {UUID} already deleted or missing. Nothing to migrate.")
        return
    if not canon_row:
        print(f"Canonical record {CANONICAL} missing. Aborting.")
        sys.exit(1)

    # Migrate all foreign keys
    total_migrated = 0
    for table in TABLES_WITH_PERSON_ID:
        try:
            cursor = db.execute(
                f"UPDATE {table} SET person_id = ? WHERE person_id = ?",
                (CANONICAL, UUID),
            )
            if cursor.rowcount > 0:
                print(f"  {table}: migrated {cursor.rowcount} rows")
                total_migrated += cursor.rowcount
        except Exception as e:
            print(f"  {table}: skipped ({e})")

    # Soft-delete the UUID record
    now = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE person SET deleted_at = ? WHERE id = ?", (now, UUID))
    db.commit()

    print(f"\nDone. Migrated {total_migrated} rows. Soft-deleted {UUID}.")

    # Verify
    dupes = db.execute(
        "SELECT health_engine_user_id, COUNT(*) as cnt "
        "FROM person WHERE deleted_at IS NULL AND health_engine_user_id IS NOT NULL "
        "GROUP BY health_engine_user_id HAVING cnt > 1"
    ).fetchall()
    if dupes:
        print(f"WARNING: duplicates still exist: {[dict(d) for d in dupes]}")
    else:
        print("Verified: no duplicate person records remain.")


if __name__ == "__main__":
    main()
