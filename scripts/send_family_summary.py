#!/usr/bin/env python3
"""Send daily family summary emails via Resend.

Usage:
    python3 scripts/send_family_summary.py                          # send all configured summaries
    python3 scripts/send_family_summary.py --dry-run                # print instead of sending
    python3 scripts/send_family_summary.py --person pops --to deal.e.andrew@gmail.com  # specific
    python3 scripts/send_family_summary.py --person pops --dry-run  # preview one person
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.coaching.family_summary import generate_family_summary, format_email, format_email_html

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(message)s",
)
log = logging.getLogger("family_summary")

CONFIG_PATH = PROJECT_ROOT / "config" / "family_summaries.yaml"
DB_PATH = PROJECT_ROOT / "data" / "kasane.db"

# Resend API key: check env first, then fall back to Baseline key
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = "milo@mybaseline.health"
FROM_NAME = "Milo"


def load_config() -> dict:
    """Load family summary configuration."""
    if not CONFIG_PATH.exists():
        log.error("Config not found: %s", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def send_email(to_email: str, to_name: str, subject: str, body: str, html_body: str = None) -> dict:
    """Send an email via Resend API."""
    email_data = {
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    if html_body:
        email_data["html"] = html_body
    payload = json.dumps(email_data).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Baseline-Health-Engine/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "id": result.get("id")}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return {"ok": False, "error": f"HTTP {e.code}: {error_body}"}


def main():
    parser = argparse.ArgumentParser(description="Send daily family summary emails.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print summaries instead of sending emails",
    )
    parser.add_argument(
        "--person", type=str, default=None,
        help="Filter to a specific person name (case-insensitive)",
    )
    parser.add_argument(
        "--to", type=str, default=None,
        help="Override recipient email (for testing)",
    )
    args = parser.parse_args()

    config = load_config()
    summaries = config.get("summaries", [])

    if not summaries:
        log.warning("No summaries configured in %s", CONFIG_PATH)
        return

    sent = 0
    errors = 0

    for entry in summaries:
        person_name = entry["person_name"]
        person_id = entry["person_id"]

        # Filter by person if specified
        if args.person and args.person.lower() != person_name.lower():
            continue

        log.info("Generating summary for %s (%s)", person_name, person_id)

        summary = generate_family_summary(person_id, db_path=DB_PATH)
        if "error" in summary:
            log.error("Failed to generate summary for %s: %s", person_name, summary["error"])
            errors += 1
            continue

        recipients = entry.get("recipients", [])

        for recipient in recipients:
            to_email = args.to if args.to else recipient["email"]
            to_name = recipient["name"]

            subject, body, html_body = format_email_html(summary, to_name, person_name)

            if args.dry_run:
                print(f"\n{'='*60}")
                print(f"TO: {to_name} <{to_email}>")
                print(f"FROM: {FROM_NAME} <{FROM_EMAIL}>")
                print(f"SUBJECT: {subject}")
                print(f"{'='*60}")
                print(body)
                print(f"{'='*60}\n")
                sent += 1
            else:
                log.info("Sending to %s <%s>", to_name, to_email)
                result = send_email(to_email, to_name, subject, body, html_body)
                if result["ok"]:
                    log.info("Sent successfully (id: %s)", result.get("id"))
                    sent += 1
                else:
                    log.error("Failed to send to %s: %s", to_email, result["error"])
                    errors += 1

    action = "previewed" if args.dry_run else "sent"
    log.info("Done: %d %s, %d errors", sent, action, errors)


if __name__ == "__main__":
    main()
