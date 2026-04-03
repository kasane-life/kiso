"""Deterministic scheduler for proactive Milo messages.

Replaces LLM-based HEARTBEAT routing with pure Python:
- Cron fires every 30 min, hits an endpoint
- Python checks each user's local time
- If it's the right window, compose message via Sonnet, send via openclaw
- Dedup via scheduled_send table prevents double-sends

Endpoints:
    POST /api/v1/scheduled/morning-brief    — 7:00 AM local
    POST /api/v1/scheduled/evening-checkin   — 8:00 PM local
    POST /api/v1/scheduled/weekly-review     — Friday 6:00 PM local
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime

from zoneinfo import ZoneInfo

logger = logging.getLogger("kiso.scheduler")

# OpenClaw binary path on Mac Mini
_OPENCLAW_BIN = os.environ.get(
    "OPENCLAW_BIN",
    os.path.expanduser("~/Library/pnpm/openclaw"),
)

# Node.js must be on PATH for openclaw to work
_OPENCLAW_ENV = {
    **os.environ,
    "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", ""),
}

SONNET_MODEL = "claude-sonnet-4-6"


# --- Audit logging ---

_AUDIT_LOG_PATH = os.path.join("data", "admin", "api_audit.jsonl")


def _audit_scheduler(schedule_type: str, dry_run: bool, summary: dict):
    """Append scheduler run to the shared audit log."""
    sent_count = sum(1 for r in summary["results"] if r["status"] in ("sent", "dry_run"))
    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "source": "scheduler",
        "schedule_type": schedule_type,
        "dry_run": dry_run,
        "eligible": summary["eligible_count"],
        "sent": sent_count,
        "skipped": len(summary["results"]) - sent_count,
    }
    try:
        os.makedirs(os.path.dirname(_AUDIT_LOG_PATH), exist_ok=True)
        with open(_AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        logger.warning("Failed to write scheduler audit log", exc_info=True)


# --- Time window check ---


def _user_local_now(tz_name: str) -> datetime:
    """Get current time in a user's timezone."""
    return datetime.now(ZoneInfo(tz_name))


def _in_window(local_now: datetime, target_hour: int, window_minutes: int = 30) -> bool:
    """Check if local_now is within [target_hour:00, target_hour:00 + window)."""
    start_minute = target_hour * 60
    current_minute = local_now.hour * 60 + local_now.minute
    return start_minute <= current_minute < start_minute + window_minutes


def _in_quiet_hours(local_now: datetime) -> bool:
    """9:15 PM - 6:00 AM local time."""
    minutes = local_now.hour * 60 + local_now.minute
    return minutes >= 21 * 60 + 15 or minutes < 6 * 60


# --- Dedup ---


def _already_sent(db, person_id: str, schedule_type: str, sent_date: str) -> bool:
    """Check if we already sent this schedule type to this person today.

    Only counts actual sends (status='sent'), not dry_runs or failures.
    """
    row = db.execute(
        "SELECT 1 FROM scheduled_send WHERE person_id = ? AND schedule_type = ? AND sent_date = ? AND status = 'sent'",
        (person_id, schedule_type, sent_date),
    ).fetchone()
    return row is not None


def _record_send(
    db, person_id: str, schedule_type: str, sent_date: str,
    status: str = "sent", preview: str = "",
):
    """Record a send to prevent duplicates.

    Uses INSERT OR REPLACE so a real send overwrites a prior dry_run record.
    """
    db.execute(
        "INSERT OR REPLACE INTO scheduled_send (person_id, schedule_type, sent_date, status, message_preview) "
        "VALUES (?, ?, ?, ?, ?)",
        (person_id, schedule_type, sent_date, status, preview[:200]),
    )
    db.commit()


# --- Message composition via Sonnet ---


def _compose_message(schedule_type: str, user_name: str, context_data: dict) -> str:
    """Call Anthropic Sonnet to compose a coaching message."""
    from anthropic import Anthropic

    prompts = {
        "morning_brief": (
            f"You are Milo, a direct and warm health coach. Compose a morning brief for {user_name}. "
            "Include: 1) Last night's sleep (duration, quality, bed/wake if available), "
            "2) Top health signal (one thing to watch), 3) Today's one focus. "
            "Keep it to 3-4 sentences. No greetings, no sign-offs. "
            "End with: \\nYour dashboard: https://dashboard.mybaseline.health/dashboard/member.html"
        ),
        "evening_checkin": (
            f"You are Milo, a direct and warm health coach. Compose an evening check-in for {user_name}. "
            "Include: 1) Active program status if any, 2) Ask about their anchor habit, "
            "3) Any meals left to log, 4) Tonight's protocol reminder if applicable. "
            "Keep it to 3-4 sentences. No greetings, no sign-offs."
        ),
        "weekly_review": (
            f"You are Milo, a direct and warm health coach. Compose a weekly review for {user_name}. "
            "Include: 1) Weight trend, 2) Key metric movements (HRV, RHR, sleep), "
            "3) Protocol compliance, 4) Coverage gaps, 5) One focus for next week. "
            "Keep it to 5-6 sentences. No greetings, no sign-offs."
        ),
    }

    system_prompt = (
        "You are Milo, a health coaching agent. You speak like a trainer who knows the user's numbers. "
        "Be direct, warm, data-grounded. Reference actual data when available. "
        "Never use em dashes. Use periods, commas, or colons instead. "
        "If data is missing or empty, acknowledge it briefly and focus on what you do know."
    )

    client = Anthropic()
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=300,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"{prompts[schedule_type]}\n\nHealth data:\n{json.dumps(context_data, indent=2, default=str)}"
        }],
    )
    return response.content[0].text


# --- Delivery via openclaw ---


def _send_via_openclaw(channel: str, target: str, message: str) -> dict:
    """Send a message using the openclaw CLI."""
    cmd = [
        _OPENCLAW_BIN, "message", "send",
        "--channel", channel,
        "--target", target,
        "--message", message,
        "--json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=_OPENCLAW_ENV,
        )
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"status": "sent", "raw": result.stdout[:200]}
        else:
            return {"status": "error", "error": result.stderr[:300], "returncode": result.returncode}
    except FileNotFoundError:
        return {"status": "error", "error": f"openclaw binary not found at {_OPENCLAW_BIN}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "openclaw send timed out after 30s"}


# --- Conversation ingestion ---


def _ingest_scheduled_message(db, user_id: str, channel: str, message: str, source: str):
    """Write an outbound message to conversation_message so Milo has context.

    When a user replies, Milo reads conversation history via get_conversations().
    Without this, Milo has no idea what was sent.

    source: 'morning_brief', 'evening_checkin', 'weekly_review', or 'manual'
    """
    now = datetime.utcnow().isoformat() + "Z"
    sender_name = "milo-manual" if source == "manual" else "milo-scheduler"
    try:
        # Dedup: skip if identical message for same user was written in last 60s
        existing = db.execute(
            """SELECT 1 FROM conversation_message
               WHERE user_id = ? AND role = 'assistant' AND content = ?
               AND created_at > datetime('now', '-60 seconds')
               LIMIT 1""",
            (user_id, message),
        ).fetchone()
        if existing:
            logger.debug("Skipping duplicate scheduled message for %s", user_id)
            return

        db.execute(
            """INSERT INTO conversation_message
               (user_id, role, content, sender_id, sender_name, channel,
                session_key, message_id, timestamp, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, "assistant", message, "scheduler", sender_name, channel,
             f"scheduler:{source}", "", now, now),
        )
        db.commit()
    except Exception:
        logger.warning("Failed to ingest message for %s", user_id, exc_info=True)


# --- Manual send with ingestion ---


def _send_and_ingest(db, user_id: str, channel: str, target: str, message: str) -> dict:
    """Send a message via openclaw and write to conversation history.

    Use this instead of raw openclaw message send so all outbound
    messages are visible in conversation_message.
    """
    result = _send_via_openclaw(channel, target, message)
    if result.get("status") != "error":
        _ingest_scheduled_message(db, user_id, channel, message, "manual")
        result["status"] = "sent"
    return result


# --- Core scheduler logic ---


def _get_eligible_persons(db):
    """Get all persons with channel data configured."""
    rows = db.execute(
        "SELECT id, name, health_engine_user_id, channel, channel_target, timezone "
        "FROM person "
        "WHERE deleted_at IS NULL AND channel IS NOT NULL AND channel_target IS NOT NULL "
        "AND health_engine_user_id IS NOT NULL",
    ).fetchall()
    return rows


def _run_schedule(schedule_type: str, target_hour: int, require_friday: bool = False, dry_run: bool = False, force_user: str | None = None) -> dict:
    """Run a scheduled message type for all eligible users."""
    from .db import get_db, init_db

    init_db()
    db = get_db()
    persons = _get_eligible_persons(db)

    results = []
    for person in persons:
        person_id = person["id"]
        user_id = person["health_engine_user_id"]
        name = person["name"]
        tz_name = person["timezone"] or "America/Los_Angeles"
        channel = person["channel"]
        target = person["channel_target"]

        try:
            local_now = _user_local_now(tz_name)
        except Exception as e:
            results.append({"user_id": user_id, "status": "skip", "reason": f"bad timezone: {e}"})
            continue

        # If force_user is set, skip all checks for that user only
        is_forced = force_user and user_id == force_user

        if not is_forced:
            # Check day of week for weekly review
            if require_friday and local_now.weekday() != 4:
                results.append({"user_id": user_id, "status": "skip", "reason": "not Friday"})
                continue

            # Check time window
            if not _in_window(local_now, target_hour):
                results.append({"user_id": user_id, "status": "skip", "reason": f"not in window (local {local_now.strftime('%H:%M')})"})
                continue

            # Check quiet hours
            if _in_quiet_hours(local_now):
                results.append({"user_id": user_id, "status": "skip", "reason": "quiet hours"})
                continue

        # Dedup check
        sent_date = local_now.strftime("%Y-%m-%d")
        if _already_sent(db, person_id, schedule_type, sent_date):
            results.append({"user_id": user_id, "status": "skip", "reason": "already sent today"})
            continue

        # Gather health context
        context_data = _gather_context(schedule_type, user_id)

        # Compose message
        try:
            message = _compose_message(schedule_type, name, context_data)
        except Exception as e:
            logger.error("Failed to compose message for %s: %s", user_id, e)
            results.append({"user_id": user_id, "status": "error", "reason": f"compose failed: {e}"})
            continue

        if dry_run:
            _record_send(db, person_id, schedule_type, sent_date, status="dry_run", preview=message)
            results.append({
                "user_id": user_id, "status": "dry_run", "channel": channel,
                "target": target, "message": message,
            })
            continue

        # Send
        send_result = _send_via_openclaw(channel, target, message)
        status = "sent" if send_result.get("status") != "error" else "failed"
        _record_send(db, person_id, schedule_type, sent_date, status=status, preview=message)

        # Write to conversation history so Milo has context when user replies
        if status == "sent":
            _ingest_scheduled_message(db, user_id, channel, message, schedule_type)

        results.append({
            "user_id": user_id, "status": status, "channel": channel,
            "send_result": send_result,
        })

    summary = {
        "schedule_type": schedule_type,
        "dry_run": dry_run,
        "eligible_count": len(persons),
        "results": results,
    }
    _audit_scheduler(schedule_type, dry_run, summary)
    return summary


def _gather_context(schedule_type: str, user_id: str) -> dict:
    """Gather health data for message composition."""
    from mcp_server.tools import _checkin, _score, _get_protocols

    context = {}
    try:
        if schedule_type in ("morning_brief", "evening_checkin"):
            context["checkin"] = _checkin(user_id=user_id)
        if schedule_type == "evening_checkin":
            context["protocols"] = _get_protocols(user_id=user_id)
        if schedule_type == "weekly_review":
            context["checkin"] = _checkin(user_id=user_id)
            context["score"] = _score(user_id=user_id)
    except Exception as e:
        logger.warning("Failed to gather context for %s/%s: %s", user_id, schedule_type, e)
        context["error"] = str(e)

    return context


# --- FastAPI route registration ---


def register_scheduler_routes(app):
    """Register scheduler endpoints on the FastAPI app."""
    from fastapi import Query, Request
    from fastapi.responses import JSONResponse

    def _verify_admin(request: Request, token: str = Query(None)):
        """Only admin token can trigger scheduled sends."""
        config = request.app.state.config
        effective = token
        if not effective:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                effective = auth[7:]
        if not effective or effective != config.api_token:
            from fastapi import HTTPException
            raise HTTPException(403, "Scheduler endpoints require admin token")
        return effective

    @app.post("/api/v1/send-message")
    async def send_message(request: Request, token: str = Query(None)):
        """Send a manual message to a user with conversation ingestion."""
        from .db import get_db, init_db
        _verify_admin(request, token)

        body = await request.json()
        user_id = body.get("user_id")
        message = body.get("message")
        if not user_id or not message:
            from fastapi import HTTPException
            raise HTTPException(422, "user_id and message are required")

        init_db()
        db = get_db()
        row = db.execute(
            "SELECT channel, channel_target FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        if not row or not row["channel"] or not row["channel_target"]:
            from fastapi import HTTPException
            raise HTTPException(404, f"No channel configured for user '{user_id}'")

        result = _send_and_ingest(db, user_id, row["channel"], row["channel_target"], message)
        return JSONResponse({"user_id": user_id, "channel": row["channel"], **result})

    @app.post("/api/v1/scheduled/morning-brief")
    async def scheduled_morning_brief(request: Request, token: str = Query(None), dry_run: bool = Query(False), force_user: str = Query(None)):
        _verify_admin(request, token)
        result = _run_schedule("morning_brief", target_hour=7, dry_run=dry_run, force_user=force_user)
        return JSONResponse(result)

    @app.post("/api/v1/scheduled/evening-checkin")
    async def scheduled_evening_checkin(request: Request, token: str = Query(None), dry_run: bool = Query(False), force_user: str = Query(None)):
        _verify_admin(request, token)
        result = _run_schedule("evening_checkin", target_hour=20, dry_run=dry_run, force_user=force_user)
        return JSONResponse(result)

    @app.post("/api/v1/scheduled/weekly-review")
    async def scheduled_weekly_review(request: Request, token: str = Query(None), dry_run: bool = Query(False), force_user: str = Query(None)):
        _verify_admin(request, token)
        result = _run_schedule("weekly_review", target_hour=18, require_friday=True, dry_run=dry_run, force_user=force_user)
        return JSONResponse(result)
