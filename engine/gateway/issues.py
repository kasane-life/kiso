"""Per-user issue tracking: auto-created from signals and audit error spikes.

Issues are deduped by (dedup_key, status='open') so running the same
detection twice doesn't create duplicates. Signal-sourced issues auto-resolve
when the signal stops firing.
"""

import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def create_issue(
    db: sqlite3.Connection,
    person_id: str,
    category: str,
    title: str,
    detail: str | None = None,
    source: str | None = None,
    dedup_key: str | None = None,
) -> dict | None:
    """Create an issue. Returns None if dedup_key matches an open issue."""
    if dedup_key:
        existing = db.execute(
            "SELECT id FROM user_issue WHERE dedup_key = ? AND status = 'open'",
            (dedup_key,),
        ).fetchone()
        if existing:
            return None

    issue_id = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO user_issue (id, person_id, category, title, detail, source, dedup_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (issue_id, person_id, category, title, detail, source, dedup_key),
    )
    db.commit()
    return {
        "id": issue_id,
        "person_id": person_id,
        "category": category,
        "title": title,
        "detail": detail,
        "status": "open",
        "source": source,
        "dedup_key": dedup_key,
    }


def resolve_issue(db: sqlite3.Connection, issue_id: str):
    """Mark an issue as resolved."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE user_issue SET status = 'resolved', resolved_at = ? WHERE id = ?",
        (now, issue_id),
    )
    db.commit()


def list_issues(
    db: sqlite3.Connection,
    person_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List issues, optionally filtered by person and/or status."""
    query = "SELECT * FROM user_issue WHERE 1=1"
    params = []
    if person_id:
        query += " AND person_id = ?"
        params.append(person_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# --- Signal-to-issue mapping ---

_SIGNAL_MAP = {
    "quiet": ("engagement", "User quiet for {detail}"),
    "garmin stale": ("stale_data", "Garmin data stale (>24h)"),
    "no wearable": ("onboarding", "No wearable connected"),
    "streak broken": ("engagement", "Habit streak broken"),
}


def _signal_to_issue(signal: str) -> tuple[str, str] | None:
    """Map a signal string to (category, title). Returns None if unknown."""
    for prefix, (cat, title_tpl) in _SIGNAL_MAP.items():
        if signal.startswith(prefix):
            # Extract detail from signal (e.g. "quiet 3d" -> "3d")
            detail = signal[len(prefix):].strip()
            title = title_tpl.format(detail=detail) if detail else title_tpl
            return cat, title
    return None


def process_signals(
    db: sqlite3.Connection,
    signals: list[dict],
    all_person_ids: list[str] | None = None,
):
    """Convert admin digest signals into issues. Auto-resolve cleared signals.

    signals: list of {"person_id": "...", "signal": "..."}
    all_person_ids: if provided, auto-resolve signal-sourced issues for persons
                    whose signal is no longer firing.
    """
    # Track which (person_id, dedup_key) are still active
    active_dedup_keys = set()

    for s in signals:
        pid = s["person_id"]
        sig = s["signal"]
        mapped = _signal_to_issue(sig)
        if not mapped:
            continue
        cat, title = mapped
        dedup_key = f"signal:{sig}:{pid}"
        active_dedup_keys.add((pid, dedup_key))
        create_issue(db, pid, cat, title, source="signal", dedup_key=dedup_key)

    # Auto-resolve: find open signal-sourced issues whose signal cleared
    if all_person_ids:
        open_signal_issues = db.execute(
            "SELECT id, person_id, dedup_key FROM user_issue "
            "WHERE source = 'signal' AND status = 'open'"
        ).fetchall()
        for row in open_signal_issues:
            pid = row["person_id"]
            dk = row["dedup_key"]
            if pid in all_person_ids and (pid, dk) not in active_dedup_keys:
                resolve_issue(db, row["id"])


# --- Audit error spike detection ---


def check_audit_errors(
    db: sqlite3.Connection,
    audit_path: str,
    threshold: int = 3,
    window_hours: int = 24,
):
    """Scan audit log for per-user error spikes. Create issues for users
    with >= threshold errors in the last window_hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    # user_id -> error count
    error_counts: dict[str, int] = defaultdict(int)

    try:
        with open(audit_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("status") != "error":
                    continue
                ts_str = entry.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if ts < cutoff:
                    continue
                uid = entry.get("user_id", "")
                if uid:
                    error_counts[uid] += 1
    except FileNotFoundError:
        return

    # Resolve user_id -> person_id
    for uid, count in error_counts.items():
        if count < threshold:
            continue
        row = db.execute(
            "SELECT id FROM person WHERE health_engine_user_id = ?", (uid,)
        ).fetchone()
        if not row:
            continue
        pid = row["id"]
        dedup_key = f"audit:error_spike:{pid}"
        create_issue(
            db, pid, "error_spike",
            f"{count} errors in last {window_hours}h",
            source="audit",
            dedup_key=dedup_key,
        )
