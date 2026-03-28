"""CSV parse/write utilities.

Safety guarantees:
- write_csv uses atomic rename (write to .tmp, then rename)
- append_csv opens in append mode (no full rewrite risk)
- validate_row checks required fields before any write
- parse_csv is deprecated (naive comma split, breaks on quoted fields)
"""

import csv
import logging
import os
import warnings
from pathlib import Path

logger = logging.getLogger("kiso.csv_io")

# Required non-empty fields per CSV file type
REQUIRED_FIELDS = {
    "meal_log.csv": ["date", "description", "protein_g"],
    "weight_log.csv": ["date", "weight_lbs"],
    "bp_log.csv": ["date", "systolic", "diastolic"],
    "daily_habits.csv": ["date"],
    "strength_log.csv": ["date", "exercise"],
}


def parse_csv(text: str) -> list[dict]:
    """Parse a CSV string into a list of dicts.

    DEPRECATED: Uses naive comma splitting that breaks on quoted fields.
    Use csv.DictReader or read_csv instead.
    """
    warnings.warn(
        "parse_csv uses naive comma splitting. Use read_csv for file-based reads.",
        DeprecationWarning,
        stacklevel=2,
    )
    lines = text.strip().split("\n")
    if not lines:
        return []
    headers = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        vals = line.split(",")
        row = {}
        for i, h in enumerate(headers):
            row[h] = vals[i].strip() if i < len(vals) else ""
        rows.append(row)
    return rows


def read_csv(path: str | Path) -> list[dict]:
    """Read a CSV file into a list of dicts."""
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def validate_row(row: dict, filename: str) -> bool:
    """Check that required fields are present and non-empty.

    Returns True if valid. Logs a warning and returns False if invalid.
    """
    basename = Path(filename).name
    required = REQUIRED_FIELDS.get(basename)
    if not required:
        return True
    for field in required:
        val = row.get(field)
        if val is None or str(val).strip() == "":
            logger.error(
                "CSV validation failed: %s missing required field '%s'. Row: %s",
                basename, field, {k: v for k, v in row.items() if k in required},
            )
            return False
    return True


def write_csv(path: str | Path, rows: list[dict], fieldnames: list[str] | None = None):
    """Write a list of dicts to a CSV file using atomic rename.

    Writes to a .tmp file first, then renames. If the process crashes
    during write, the original file is untouched.
    """
    if not rows:
        return
    p = Path(path)
    if fieldnames is None:
        fieldnames = list(rows[0].keys())

    # Validate all rows before writing
    basename = p.name
    valid_rows = []
    for row in rows:
        if validate_row(row, basename):
            valid_rows.append(row)
        else:
            logger.warning("Skipping invalid row in %s: %s", basename, row)

    if not valid_rows:
        logger.error("No valid rows to write to %s. Aborting write.", basename)
        return

    # Write to temp file, then atomic rename
    tmp = p.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(valid_rows)

    # Atomic rename (POSIX guarantees atomicity on same filesystem)
    tmp.rename(p)

    # Post-write integrity check
    rows_after = read_csv(p)
    if len(rows_after) != len(valid_rows):
        logger.error(
            "CSV integrity check failed: %s has %d rows, expected %d",
            basename, len(rows_after), len(valid_rows),
        )


def append_csv(path: str | Path, row: dict, fieldnames: list[str]):
    """Append a single row to a CSV file.

    Opens in append mode. No full rewrite. Safe for concurrent access
    on POSIX (single-line writes under PIPE_BUF are atomic).
    """
    p = Path(path)
    if not validate_row(row, p.name):
        raise ValueError(f"Row validation failed for {p.name}: missing required fields")

    write_header = not p.exists() or p.stat().st_size == 0
    with open(p, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
