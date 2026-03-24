#!/usr/bin/env python3
"""Seed the Kasane SQLite database with Andrew's person record.

Links to health_engine_user_id='default' so Milo can read both
Kasane data (SQLite) and health data (CSVs) for the same person.

Usage:
    cd /Users/adeal/src/health-engine
    python3 scripts/seed_kasane_db.py
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone
from engine.gateway.db import get_db, init_db


def seed():
    init_db()
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Check if Andrew already exists
    existing = db.execute(
        "SELECT id FROM person WHERE health_engine_user_id = 'default'"
    ).fetchone()
    if existing:
        print(f"Andrew's person record already exists: {existing['id']}")
        return existing["id"]

    person_id = "andrew-deal-001"
    db.execute(
        """INSERT INTO person (
            id, name, relationship, date_of_birth, biological_sex,
            health_engine_user_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            person_id,
            "Andrew Deal",
            "self",
            "1991-01-01",
            "M",
            "default",
            now,
            now,
        ),
    )
    db.commit()
    print(f"Created person record: {person_id}")
    print(f"  name: Andrew Deal")
    print(f"  health_engine_user_id: default")
    print(f"  biological_sex: M")
    return person_id


if __name__ == "__main__":
    pid = seed()
    print(f"\nDone. Person ID: {pid}")
    print("Paul can use this ID to test SyncService connectivity.")
