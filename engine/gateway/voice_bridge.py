"""Voice bridge: Twilio MediaStreams <-> OpenAI Realtime API.

Enables voice-based health coaching via phone calls. Clients call the
Twilio number, talk to Milo (via OpenAI Realtime API), and Milo can
look up health data and log information mid-conversation using
health-engine tools.

Architecture:
  Phone call -> Twilio -> POST /api/voice/incoming (TwiML)
                       -> WSS /api/voice/ws (MediaStream <-> OpenAI Realtime)
                       -> On disconnect: save transcript to conversation_message
"""

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .db import get_db, init_db
from .twilio_sms import _lookup_user_by_phone

logger = logging.getLogger("health-engine.voice")


# --- Layer 1: Transcript Collector ---


class TranscriptCollector:
    """Accumulates transcript from OpenAI Realtime events."""

    def __init__(self):
        self._turns: list[tuple[str, str]] = []
        self._current_delta = ""

    def add_delta(self, text: str):
        """Accumulate assistant speech transcript fragment."""
        self._current_delta += text

    def finalize_assistant_turn(self):
        """Mark end of an assistant speech turn."""
        if self._current_delta.strip():
            self._turns.append(("assistant", self._current_delta.strip()))
        self._current_delta = ""

    def add_user_transcript(self, text: str):
        """Add a completed user speech transcript."""
        if text.strip():
            self._turns.append(("user", text.strip()))

    def full_transcript(self) -> str:
        """Format all turns as readable transcript."""
        if not self._turns:
            return ""
        lines = []
        for role, text in self._turns:
            label = "Milo" if role == "assistant" else "User"
            lines.append(f"{label}: {text}")
        return "\n\n".join(lines)


# --- Layer 2: OpenAI Tool Definitions ---


OPENAI_TOOLS = [
    {
        "type": "function",
        "name": "checkin",
        "description": "Get a full health briefing for the user: scores, insights, weight, nutrition, habits, protocols, wearable data.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "type": "function",
        "name": "score",
        "description": "Get the user's health coverage score with tier breakdowns, percentiles, and gaps to close.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "type": "function",
        "name": "get_protocols",
        "description": "Get the user's active coaching protocols with progress, habits, and nudges.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "type": "function",
        "name": "get_daily_snapshot",
        "description": "Get today's health snapshot: Garmin data, meals, calorie balance.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "type": "function",
        "name": "log_workout",
        "description": "Log a workout. Exercises should be semicolon-separated, e.g. 'Back Squat 4x5 @155 RPE 7; RDL 3x8 @135'.",
        "parameters": {
            "type": "object",
            "properties": {
                "exercises": {
                    "type": "string",
                    "description": "Semicolon-separated exercise entries, e.g. 'Bench Press 5x5 @185 RPE 8; Pull-ups 3x10'",
                },
                "program_day": {
                    "type": "integer",
                    "description": "Program day number (1-4) if following a program",
                },
                "duration_min": {
                    "type": "number",
                    "description": "Workout duration in minutes",
                },
                "sentiment": {
                    "type": "string",
                    "enum": ["great", "good", "ok", "rough", "bad"],
                    "description": "How the workout felt",
                },
            },
            "required": ["exercises"],
        },
    },
    {
        "type": "function",
        "name": "log_meal",
        "description": "Log a meal with macros.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What was eaten, e.g. 'salmon, rice, and broccoli'",
                },
                "protein_g": {
                    "type": "number",
                    "description": "Protein in grams",
                },
                "calories": {
                    "type": "number",
                    "description": "Total calories (optional)",
                },
            },
            "required": ["description", "protein_g"],
        },
    },
    {
        "type": "function",
        "name": "get_workout_program",
        "description": "Get the user's active workout program with all prescribed exercises per day.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
]


# --- Layer 3: Tool Call Dispatcher ---


def dispatch_tool_call(name: str, args: dict, user_id: str) -> str:
    """Call a health-engine tool by name, return result as JSON string."""
    from mcp_server.tools import (
        _checkin,
        _get_daily_snapshot,
        _get_protocols,
        _get_workout_program,
        _log_meal,
        _log_workout,
        _score,
    )

    dispatch = {
        "checkin": _checkin,
        "score": _score,
        "get_protocols": _get_protocols,
        "get_daily_snapshot": _get_daily_snapshot,
        "log_workout": _log_workout,
        "log_meal": _log_meal,
        "get_workout_program": _get_workout_program,
    }

    fn = dispatch.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        args["user_id"] = user_id
        result = fn(**args)
        return json.dumps(result, default=str)
    except Exception as e:
        logger.error("Tool call %s failed: %s", name, e)
        return json.dumps({"error": str(e)})


# --- Layer 4: Session Context Builder ---


def build_session_context(user_id: str) -> str:
    """Build OpenAI system prompt with client context."""
    from mcp_server.tools import _get_daily_snapshot, _get_protocols

    name = user_id  # fallback
    try:
        init_db()
        db = get_db()
        row = db.execute(
            "SELECT name FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        if row and row["name"]:
            name = row["name"]
    except Exception:
        pass

    today = datetime.now().strftime("%Y-%m-%d")

    snapshot_summary = "No data available."
    try:
        snapshot = _get_daily_snapshot(user_id=user_id)
        parts = []
        garmin = snapshot.get("garmin", {})
        if garmin:
            if garmin.get("steps"):
                parts.append(f"Steps: {garmin['steps']}")
            if garmin.get("heart_rate"):
                parts.append(f"HR: {garmin['heart_rate']} bpm")
            if garmin.get("body_battery"):
                parts.append(f"Body battery: {garmin['body_battery']}")
        meals = snapshot.get("meals", {})
        totals = meals.get("totals", {})
        if totals.get("protein_g"):
            parts.append(f"Protein so far: {totals['protein_g']}g")
        balance = snapshot.get("calorie_balance", {})
        if balance.get("status"):
            parts.append(f"Calorie status: {balance['status']} ({balance.get('surplus_deficit', 0)} cal)")
        if parts:
            snapshot_summary = "; ".join(parts)
    except Exception:
        pass

    protocol_summary = "No active protocols."
    try:
        protocols = _get_protocols(user_id=user_id)
        if protocols and isinstance(protocols, list):
            names = [p.get("protocol", "unknown") for p in protocols if isinstance(p, dict)]
            if names:
                protocol_summary = ", ".join(names)
    except Exception:
        pass

    return (
        f"You are Milo, {name}'s health coach. Today is {today}.\n\n"
        f"Current health snapshot: {snapshot_summary}\n"
        f"Active protocols: {protocol_summary}\n\n"
        "OPENING: When the conversation starts, greet them by name with genuine energy. "
        "Give a quick, excited status check: where they are in their journey right now "
        "based on the snapshot data above. One or two standout things. Then naturally "
        "suggest a couple trailheads they could explore: maybe their sleep, their program, "
        "logging a workout, checking their score. Don't list options like a phone menu. "
        "Just weave them in conversationally, like a coach who's been following along.\n\n"
        "STYLE: direct, warm, energized. Like a trainer who's genuinely stoked about "
        "the work you're putting in. Never use em dashes. Use periods, commas, or colons instead. "
        "Keep responses concise for voice. 2-3 sentences max unless asked for detail. "
        "Reference actual data when available. If data is missing, acknowledge briefly "
        "and pivot to what you do know."
    )


# --- Layer 5: Transcript Persistence ---


def save_transcript(user_id: str, stream_sid: str, transcript: TranscriptCollector):
    """Save voice call transcript to conversation_message table."""
    text = transcript.full_transcript()
    if not text:
        return

    init_db()
    db = get_db()

    now = datetime.now(timezone.utc).isoformat()
    for role, content in transcript._turns:
        sender_name = "milo-voice" if role == "assistant" else user_id
        sender_id = "voice_bridge" if role == "assistant" else user_id
        db.execute(
            """INSERT INTO conversation_message
               (user_id, role, content, sender_id, sender_name, channel,
                session_key, message_id, timestamp, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, role, content, sender_id, sender_name, "voice",
             f"voice:{stream_sid}", "", now, now),
        )
    db.commit()


# --- Layer 6: TwiML Incoming Call Handler ---


def create_incoming_call_handler(config):
    """Create the handler for POST /api/voice/incoming."""
    tunnel_domain = getattr(config, "tunnel_domain", "") or ""

    async def incoming_call(request: Request):
        form = await request.form()
        from_phone = form.get("From", "")

        user_id = _lookup_user_by_phone(from_phone)

        if not user_id:
            twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                "<Say>Sorry, I don't recognize this number. Goodbye.</Say>"
                "<Hangup/>"
                "</Response>"
            )
            return Response(content=twiml, media_type="application/xml")

        ws_url = f"wss://{tunnel_domain}/api/voice/ws"

        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Connect>"
            f'<Stream url="{ws_url}">'
            f'<Parameter name="user_id" value="{user_id}" />'
            "</Stream>"
            "</Connect>"
            "</Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    return incoming_call


# --- Layer 7: WebSocket Bridge ---


async def voice_ws_handler(websocket: WebSocket):
    """Bridge Twilio MediaStream to OpenAI Realtime API."""
    import websockets

    await websocket.accept()

    transcript = TranscriptCollector()
    stream_sid = None
    openai_ws = None
    user_id = None

    try:
        # Main event loop: process Twilio events
        async for raw_msg in websocket.iter_text():
            msg = json.loads(raw_msg)
            event = msg.get("event")

            if event == "connected":
                logger.info("Twilio stream connected")

            elif event == "start":
                # Extract user_id from custom parameters (set via TwiML <Parameter>)
                custom_params = msg.get("start", {}).get("customParameters", {})
                user_id = custom_params.get("user_id")
                if not user_id:
                    logger.warning("Voice WebSocket missing user_id in start params, closing")
                    await websocket.close(code=4001)
                    return

                # Build context now that we know who's calling
                system_prompt = build_session_context(user_id)
                stream_sid = msg.get("start", {}).get("streamSid", "")
                logger.info("Stream started: %s (user=%s)", stream_sid, user_id)

                # Open OpenAI Realtime connection
                openai_api_key = os.environ.get("OPENAI_API_KEY", "")
                if not openai_api_key:
                    logger.error("OPENAI_API_KEY not set")
                    await websocket.close(code=1011)
                    return

                openai_ws = await websockets.connect(
                    "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17",
                    additional_headers={
                        "Authorization": f"Bearer {openai_api_key}",
                        "OpenAI-Beta": "realtime=v1",
                    },
                )

                # Configure session
                await openai_ws.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "instructions": system_prompt,
                        "voice": "echo",
                        "input_audio_format": "g711_ulaw",
                        "output_audio_format": "g711_ulaw",
                        "input_audio_transcription": {"model": "whisper-1"},
                        "tools": OPENAI_TOOLS,
                        "tool_choice": "auto",
                    },
                }))

                # Start OpenAI listener task
                asyncio.create_task(
                    _relay_openai_to_twilio(openai_ws, websocket, stream_sid, transcript, user_id)
                )

                # Trigger Milo to greet first (don't wait for caller to speak)
                await openai_ws.send(json.dumps({"type": "response.create"}))

            elif event == "media":
                if openai_ws:
                    payload = msg.get("media", {}).get("payload", "")
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload,
                    }))

            elif event == "stop":
                logger.info("Stream stopped: %s", stream_sid)
                break

    except WebSocketDisconnect:
        logger.info("Twilio disconnected (user=%s)", user_id)
    except Exception as e:
        logger.error("Voice bridge error: %s", e, exc_info=True)
    finally:
        # Save transcript
        if stream_sid and transcript.full_transcript():
            try:
                save_transcript(user_id, stream_sid, transcript)
                logger.info("Saved voice transcript for %s (%d turns)", user_id, len(transcript._turns))
            except Exception as e:
                logger.error("Failed to save transcript: %s", e)

        # Close OpenAI connection
        if openai_ws:
            try:
                await openai_ws.close()
            except Exception:
                pass


async def _relay_openai_to_twilio(
    openai_ws,
    twilio_ws: WebSocket,
    stream_sid: str,
    transcript: TranscriptCollector,
    user_id: str,
):
    """Listen to OpenAI Realtime and relay audio/events back to Twilio."""
    pending_args: dict[str, str] = {}  # call_id -> accumulated args
    pending_names: dict[str, str] = {}  # call_id -> function name

    try:
        async for raw_msg in openai_ws:
            msg = json.loads(raw_msg)
            msg_type = msg.get("type", "")

            if msg_type == "response.audio.delta":
                # Relay audio back to Twilio
                delta = msg.get("delta", "")
                if delta:
                    await twilio_ws.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": delta},
                    })

            elif msg_type == "response.audio_transcript.delta":
                transcript.add_delta(msg.get("delta", ""))

            elif msg_type == "response.audio_transcript.done":
                transcript.finalize_assistant_turn()

            elif msg_type == "conversation.item.input_audio_transcription.completed":
                user_text = msg.get("transcript", "")
                transcript.add_user_transcript(user_text)

            elif msg_type == "response.function_call_arguments.delta":
                call_id = msg.get("call_id", "")
                pending_args[call_id] = pending_args.get(call_id, "") + msg.get("delta", "")
                if "name" in msg:
                    pending_names[call_id] = msg["name"]

            elif msg_type == "response.function_call_arguments.done":
                call_id = msg.get("call_id", "")
                fn_name = msg.get("name", "") or pending_names.get(call_id, "")
                args_str = pending_args.pop(call_id, "{}")
                pending_names.pop(call_id, None)

                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}

                logger.info("Voice tool call: %s(%s) for %s", fn_name, args_str[:100], user_id)
                result = dispatch_tool_call(fn_name, args, user_id)

                # Send result back to OpenAI
                await openai_ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result,
                    },
                }))

                # Ask OpenAI to continue
                await openai_ws.send(json.dumps({"type": "response.create"}))

            elif msg_type == "error":
                logger.error("OpenAI Realtime error: %s", msg.get("error", {}))

    except Exception as e:
        logger.error("OpenAI relay error: %s", e)


# --- Layer 8: Route Registration ---


def register_voice_routes(app, config):
    """Register voice bridge endpoints on the FastAPI app."""
    handler = create_incoming_call_handler(config)
    app.post("/api/voice/incoming")(handler)
    app.websocket("/api/voice/ws")(voice_ws_handler)
