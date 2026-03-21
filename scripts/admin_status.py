#!/usr/bin/env python3
"""
Aggregate health-engine user data into a single admin status JSON.

Reads briefing.json, config.yaml, and garmin_latest.json for each known user.
Outputs data/admin/admin_status.json.

Usage:
    python3 scripts/admin_status.py
    python3 scripts/admin_status.py --data-dir /path/to/data
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


KNOWN_USERS = [
    {"user_id": "andrew", "name": "Andrew", "is_default": True},
    {"user_id": "paul", "name": "Paul", "is_default": False},
    {"user_id": "mike", "name": "Mike", "is_default": False},
    {"user_id": "dad", "name": "Dad", "is_default": False},
]


def read_json(path: Path) -> Optional[dict]:
    """Read a JSON file, return None if missing or invalid."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def file_age_hours(path: Path) -> Optional[float]:
    """Return hours since file was last modified, or None if missing."""
    try:
        mtime = os.path.getmtime(path)
        delta = datetime.now(timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)
        return round(delta.total_seconds() / 3600, 1)
    except FileNotFoundError:
        return None


def user_data_dir(data_dir: Path, user: dict) -> Path:
    """Return the data directory for a user."""
    if user["is_default"]:
        return data_dir
    return data_dir / "users" / user["user_id"]


def extract_user_status(data_dir: Path, user: dict) -> dict:
    """Extract status info for a single user."""
    udir = user_data_dir(data_dir, user)

    result = {
        "user_id": user["user_id"],
        "name": user["name"],
        "data_dir_exists": udir.is_dir(),
        "onboarded": False,
        "has_briefing": False,
        "garmin": {"status": "missing", "last_updated": None, "age_hours": None},
        "metrics": {
            "hrv": None,
            "rhr": None,
            "sleep_hours": None,
            "weight": None,
            "score_coverage": None,
        },
    }

    if not udir.is_dir():
        return result

    # Check config.yaml (onboarded)
    config_path = udir / "config.yaml"
    if not config_path.exists() and user["is_default"]:
        # Default user config is at repo root
        config_path = data_dir.parent / "config.yaml"
    result["onboarded"] = config_path.exists()

    # Read briefing.json
    briefing = read_json(udir / "briefing.json")
    if briefing:
        result["has_briefing"] = True
        result["briefing_as_of"] = briefing.get("as_of")

        # Garmin metrics
        garmin = briefing.get("garmin", {})
        if garmin:
            result["metrics"]["hrv"] = garmin.get("hrv_rmssd_avg")
            result["metrics"]["rhr"] = garmin.get("resting_hr")
            result["metrics"]["sleep_hours"] = garmin.get("sleep_duration_avg")

        # Weight
        weight_section = briefing.get("weight", {})
        if isinstance(weight_section, dict):
            result["metrics"]["weight"] = weight_section.get("current") or weight_section.get("latest")

        # Score coverage
        score_section = briefing.get("score", {})
        if isinstance(score_section, dict):
            result["metrics"]["score_coverage"] = score_section.get("coverage")

    # Check garmin_latest.json
    garmin_path = udir / "garmin_latest.json"
    garmin_data = read_json(garmin_path)
    if garmin_data:
        last_updated = garmin_data.get("last_updated")
        age = file_age_hours(garmin_path)
        if age is not None and age < 24:
            status = "connected"
        elif age is not None:
            status = "stale"
        else:
            status = "missing"
        result["garmin"] = {
            "status": status,
            "last_updated": last_updated,
            "age_hours": age,
        }

    return result


def compute_activity_status(user_status: dict) -> str:
    """Determine if user is active/quiet/inactive based on data freshness."""
    if not user_status["has_briefing"]:
        if user_status["onboarded"]:
            return "quiet"
        return "inactive"

    garmin_age = user_status["garmin"].get("age_hours")
    if garmin_age is not None and garmin_age < 48:
        return "active"
    if garmin_age is not None and garmin_age < 168:  # 7 days
        return "quiet"

    # Has briefing but garmin is old or missing. Check briefing date.
    as_of = user_status.get("briefing_as_of")
    if as_of:
        try:
            briefing_date = datetime.strptime(as_of, "%Y-%m-%d")
            days_old = (datetime.now() - briefing_date).days
            if days_old < 3:
                return "active"
            if days_old < 7:
                return "quiet"
        except ValueError:
            pass

    return "inactive"


def main():
    parser = argparse.ArgumentParser(description="Generate admin status JSON")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Path to health-engine data directory (default: data/)",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    if not data_dir.is_dir():
        print(f"Warning: data directory not found: {data_dir}", file=sys.stderr)
        print("Creating minimal status file anyway.", file=sys.stderr)

    users = []
    for user in KNOWN_USERS:
        status = extract_user_status(data_dir, user)
        status["activity"] = compute_activity_status(status)
        users.append(status)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "user_count": len(users),
        "active_count": sum(1 for u in users if u["activity"] == "active"),
        "users": users,
    }

    # Write output
    out_dir = data_dir / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "admin_status.json"

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {out_path}")
    print(f"  {output['user_count']} users, {output['active_count']} active")


if __name__ == "__main__":
    main()
