#!/usr/bin/env python3
"""Migrate a Kasane markdown export into health-engine's shared data layer.

Parses the markdown export format, generates deterministic UUIDs, and can
either produce a JSON file (for the /api/v1/sync endpoint) or import
directly into the local SQLite database.

Usage:
    python3 scripts/migrate_kasane_export.py /path/to/export.txt                    # Parse + summary
    python3 scripts/migrate_kasane_export.py /path/to/export.txt --json output.json # Export as JSON
    python3 scripts/migrate_kasane_export.py /path/to/export.txt --import           # Import to SQLite

Safety:
    - Dry run by default (summary only)
    - --import flag required to write to SQLite
    - Idempotent: deterministic UUIDs mean re-runs upsert, not duplicate
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Project root for SQLite path resolution
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "kasane.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(message)s",
)
log = logging.getLogger("migrate_kasane")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CheckIn:
    date: str            # YYYY-MM-DD
    completed: bool
    note: Optional[str] = None
    id: str = ""

@dataclass
class Habit:
    title: str
    state: str           # forming, practicing, resting (raw from export)
    category: str
    emoji: str = ""
    anchor: Optional[str] = None
    purpose: Optional[str] = None
    show_in_today: bool = True
    identity_threshold: int = 21
    created_at: str = ""
    graduated_at: Optional[str] = None
    reminder_time: Optional[str] = None
    sort_order: int = 0
    check_ins: list[CheckIn] = field(default_factory=list)
    id: str = ""

@dataclass
class Person:
    name: str
    relationship: str     # self, spouse, child, parent, other
    dob: Optional[str] = None
    habits: list[Habit] = field(default_factory=list)
    id: str = ""


# ---------------------------------------------------------------------------
# Deterministic UUID generation
# ---------------------------------------------------------------------------
NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

def make_uuid(seed: str) -> str:
    """Generate a deterministic UUID v5 from a seed string."""
    return str(uuid.uuid5(NAMESPACE, seed))


# ---------------------------------------------------------------------------
# State mapping: Kasane export -> health-engine DB
# ---------------------------------------------------------------------------
STATE_MAP = {
    "forming":    "active",
    "practicing": "graduated",
    "resting":    "seasonal",
}

def map_state(raw: str) -> str:
    return STATE_MAP.get(raw.lower(), raw.lower())


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_export(text: str) -> list[Person]:
    """Parse a Kasane markdown export into structured Person objects."""
    persons: list[Person] = []
    current_person: Optional[Person] = None
    current_habit: Optional[Habit] = None
    in_checkins = False

    for line in text.splitlines():
        stripped = line.strip()

        # --- Person header ---
        m = re.match(r"^## Person:\s*(.+)$", stripped)
        if m:
            if current_person is not None:
                persons.append(current_person)
            current_person = Person(name=m.group(1).strip(), relationship="other")
            current_habit = None
            in_checkins = False
            continue

        # --- Habit header (must be checked before person fields block) ---
        m = re.match(r"^### Habit:\s*(.+)$", stripped)
        if m and current_person is not None:
            current_habit = Habit(title=m.group(1).strip(), state="", category="")
            current_person.habits.append(current_habit)
            in_checkins = False
            continue

        # --- Person fields (only when no habit is active) ---
        if current_person and current_habit is None:
            if stripped.startswith("- Relationship:"):
                current_person.relationship = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("- DOB:"):
                current_person.dob = stripped.split(":", 1)[1].strip()
            continue

        # --- Check-Ins section header ---
        if stripped == "#### Check-Ins":
            in_checkins = True
            continue

        # --- Check-in entry ---
        if in_checkins and current_habit and stripped.startswith("- "):
            # Format: - YYYY-MM-DD | completed
            # Or:     - YYYY-MM-DD | completed | note text
            parts = [p.strip() for p in stripped[2:].split("|")]
            if len(parts) >= 2:
                ci_date = parts[0]
                ci_completed = parts[1].lower() == "completed"
                ci_note = parts[2] if len(parts) > 2 else None
                current_habit.check_ins.append(
                    CheckIn(date=ci_date, completed=ci_completed, note=ci_note)
                )
            continue

        # --- Habit fields ---
        if current_habit and not in_checkins and stripped.startswith("- "):
            key_val = stripped[2:]
            if ":" not in key_val:
                continue
            key, val = key_val.split(":", 1)
            key = key.strip()
            val = val.strip()

            if key == "State":
                current_habit.state = val
            elif key == "Category":
                current_habit.category = val
            elif key == "Emoji":
                current_habit.emoji = val
            elif key == "Anchor":
                current_habit.anchor = val
            elif key == "Purpose":
                current_habit.purpose = val
            elif key == "ShowInToday":
                current_habit.show_in_today = val.lower() == "true"
            elif key == "IdentityThreshold":
                current_habit.identity_threshold = int(val)
            elif key == "CreatedAt":
                current_habit.created_at = val
            elif key == "GraduatedAt":
                current_habit.graduated_at = val
            elif key == "ReminderTime":
                current_habit.reminder_time = val
            elif key == "SortOrder":
                current_habit.sort_order = int(val)

    # Don't forget the last person
    if current_person is not None:
        persons.append(current_person)

    # Assign deterministic IDs
    for person in persons:
        person.id = make_uuid(f"person:{person.name}")
        for habit in person.habits:
            habit.id = make_uuid(f"habit:{person.name}:{habit.title}")
            seen_dates: dict[str, int] = {}
            for ci in habit.check_ins:
                # Handle duplicate dates by appending an index
                count = seen_dates.get(ci.date, 0)
                base_seed = f"checkin:{person.name}:{habit.title}:{ci.date}"
                if count > 0:
                    ci.id = make_uuid(f"{base_seed}:{count}")
                else:
                    ci.id = make_uuid(base_seed)
                seen_dates[ci.date] = count + 1

    return persons


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(persons: list[Person]) -> None:
    total_habits = 0
    total_checkins = 0
    print("\n=== Kasane Export Migration Summary ===\n")
    for p in persons:
        habit_count = len(p.habits)
        checkin_count = sum(len(h.check_ins) for h in p.habits)
        total_habits += habit_count
        total_checkins += checkin_count
        dob_str = f", DOB {p.dob}" if p.dob else ""
        print(f"  Person: {p.name} ({p.relationship}{dob_str})")
        print(f"    ID: {p.id}")
        print(f"    Habits: {habit_count}")
        print(f"    Check-ins: {checkin_count}")
        for h in p.habits:
            state_arrow = f"{h.state} -> {map_state(h.state)}"
            ci_str = f"  ({len(h.check_ins)} check-ins)" if h.check_ins else ""
            grad_str = f"  [graduated {h.graduated_at}]" if h.graduated_at else ""
            print(f"      {h.emoji}  {h.title}  [{state_arrow}]{ci_str}{grad_str}")
        print()

    print(f"  Totals: {len(persons)} people, {total_habits} habits, {total_checkins} check-ins")
    print()


# ---------------------------------------------------------------------------
# JSON export (for /api/v1/sync)
# ---------------------------------------------------------------------------

def to_sync_json(persons: list[Person]) -> dict:
    """Build the JSON payload matching the v1 sync endpoint format."""
    now = datetime.now(timezone.utc).isoformat()

    persons_out = []
    habits_out = []
    checkins_out = []

    for p in persons:
        persons_out.append({
            "id": p.id,
            "name": p.name,
            "relationship": p.relationship,
            "dateOfBirth": p.dob,
            "createdAt": now,
            "updatedAt": now,
        })
        for h in p.habits:
            habits_out.append({
                "id": h.id,
                "personId": p.id,
                "title": h.title,
                "purpose": h.purpose,
                "category": h.category,
                "emoji": h.emoji,
                "anchor": h.anchor,
                "state": map_state(h.state),
                "sortOrder": h.sort_order,
                "identityThreshold": h.identity_threshold,
                "graduatedAt": h.graduated_at,
                "showInToday": h.show_in_today,
                "reminderTime": h.reminder_time,
                "createdAt": h.created_at + "T00:00:00Z" if h.created_at else now,
                "updatedAt": now,
            })
            for ci in h.check_ins:
                checkins_out.append({
                    "id": ci.id,
                    "habitId": h.id,
                    "date": ci.date,
                    "completed": ci.completed,
                    "note": ci.note,
                    "createdAt": ci.date + "T12:00:00Z",
                    "updatedAt": now,
                })

    return {
        "deviceId": "kasane-export-migration",
        "persons": persons_out,
        "habits": habits_out,
        "checkIns": checkins_out,
    }


def write_json(data: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Wrote JSON to %s", path)


# ---------------------------------------------------------------------------
# SQLite import
# ---------------------------------------------------------------------------

def import_to_sqlite(persons: list[Person], db_path: Path = DB_PATH) -> None:
    """Upsert persons, habits, and check-ins into the local SQLite database."""
    if not db_path.exists():
        log.error("Database not found at %s. Generate JSON output instead.", db_path)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    now = datetime.now(timezone.utc).isoformat()

    stats = {"persons_upserted": 0, "habits_upserted": 0, "checkins_upserted": 0}

    for p in persons:
        conn.execute(
            """INSERT INTO person (id, name, relationship, date_of_birth, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name = excluded.name,
                 relationship = excluded.relationship,
                 date_of_birth = excluded.date_of_birth,
                 updated_at = excluded.updated_at""",
            (p.id, p.name, p.relationship, p.dob, now, now),
        )
        stats["persons_upserted"] += 1

        for h in p.habits:
            created = h.created_at + "T00:00:00Z" if h.created_at else now
            conn.execute(
                """INSERT INTO habit (
                    id, person_id, title, purpose, category, emoji, anchor,
                    state, sort_order, identity_threshold, graduated_at,
                    show_in_today, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    purpose = excluded.purpose,
                    category = excluded.category,
                    emoji = excluded.emoji,
                    anchor = excluded.anchor,
                    state = excluded.state,
                    sort_order = excluded.sort_order,
                    identity_threshold = excluded.identity_threshold,
                    graduated_at = excluded.graduated_at,
                    show_in_today = excluded.show_in_today,
                    updated_at = excluded.updated_at""",
            (
                h.id, p.id, h.title, h.purpose, h.category, h.emoji,
                h.anchor, map_state(h.state), h.sort_order,
                h.identity_threshold, h.graduated_at,
                1 if h.show_in_today else 0, created, now,
            ),
            )
            stats["habits_upserted"] += 1

            for ci in h.check_ins:
                ci_created = ci.date + "T12:00:00Z"
                conn.execute(
                    """INSERT INTO check_in (
                        id, habit_id, date, completed, note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        completed = excluded.completed,
                        note = excluded.note,
                        updated_at = excluded.updated_at""",
                    (ci.id, h.id, ci.date, 1 if ci.completed else 0, ci.note, ci_created, now),
                )
                stats["checkins_upserted"] += 1

    conn.commit()
    conn.close()

    log.info(
        "SQLite import complete: %d persons, %d habits, %d check-ins upserted to %s",
        stats["persons_upserted"],
        stats["habits_upserted"],
        stats["checkins_upserted"],
        db_path,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Migrate a Kasane markdown export to health-engine format."
    )
    parser.add_argument("export_file", help="Path to the Kasane export .txt file")
    parser.add_argument("--json", metavar="OUTPUT", help="Write sync JSON to this path")
    parser.add_argument(
        "--import", dest="do_import", action="store_true",
        help="Import directly into the local SQLite database"
    )
    parser.add_argument(
        "--db", metavar="PATH", default=str(DB_PATH),
        help=f"SQLite database path (default: {DB_PATH})"
    )
    args = parser.parse_args()

    export_path = Path(args.export_file)
    if not export_path.exists():
        log.error("Export file not found: %s", export_path)
        sys.exit(1)

    text = export_path.read_text(encoding="utf-8")
    persons = parse_export(text)

    if not persons:
        log.error("No persons found in export file.")
        sys.exit(1)

    # Always show summary
    print_summary(persons)

    # JSON export
    if args.json:
        data = to_sync_json(persons)
        write_json(data, args.json)

    # SQLite import
    if args.do_import:
        import_to_sqlite(persons, db_path=Path(args.db))
    elif not args.json:
        print("  (dry run: pass --json or --import to write data)\n")


if __name__ == "__main__":
    main()
