"""Tests for voice bridge: Twilio MediaStreams <-> OpenAI Realtime API.

Covers all 8 layers:
1. TranscriptCollector
2. OPENAI_TOOLS definitions
3. dispatch_tool_call
4. build_session_context
5. save_transcript
6. TwiML incoming call handler
7. WebSocket bridge handler
8. Route registration
"""

import json
import os
import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocket

class AsyncIterator:
    """Helper to make an async iterator from a list."""
    def __init__(self, items):
        self._items = iter(items)
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


from engine.gateway.voice_bridge import (
    OPENAI_TOOLS,
    TranscriptCollector,
    build_session_context,
    create_incoming_call_handler,
    dispatch_tool_call,
    register_voice_routes,
    save_transcript,
)


# --- Layer 1: TranscriptCollector ---


class TestTranscriptCollector:

    def test_accumulates_deltas(self):
        tc = TranscriptCollector()
        tc.add_delta("Hello, ")
        tc.add_delta("how are you?")
        tc.finalize_assistant_turn()
        assert "Hello, how are you?" in tc.full_transcript()

    def test_finalize_creates_turn(self):
        tc = TranscriptCollector()
        tc.add_delta("First response.")
        tc.finalize_assistant_turn()
        assert len(tc._turns) == 1
        assert tc._turns[0] == ("assistant", "First response.")

    def test_user_transcript(self):
        tc = TranscriptCollector()
        tc.add_user_transcript("How did I sleep?")
        assert len(tc._turns) == 1
        assert tc._turns[0] == ("user", "How did I sleep?")

    def test_full_transcript_alternates_roles(self):
        tc = TranscriptCollector()
        tc.add_user_transcript("How did I sleep?")
        tc.add_delta("You slept 7.2 hours last night.")
        tc.finalize_assistant_turn()
        text = tc.full_transcript()
        assert text == "User: How did I sleep?\n\nMilo: You slept 7.2 hours last night."

    def test_empty_transcript(self):
        tc = TranscriptCollector()
        assert tc.full_transcript() == ""

    def test_whitespace_only_delta_ignored(self):
        tc = TranscriptCollector()
        tc.add_delta("   ")
        tc.finalize_assistant_turn()
        assert tc._turns == []

    def test_whitespace_only_user_ignored(self):
        tc = TranscriptCollector()
        tc.add_user_transcript("  ")
        assert tc._turns == []

    def test_multiple_turns(self):
        tc = TranscriptCollector()
        tc.add_user_transcript("What's my score?")
        tc.add_delta("Your coverage score is 72%.")
        tc.finalize_assistant_turn()
        tc.add_user_transcript("What are my gaps?")
        tc.add_delta("Sleep and VO2 max are your biggest gaps.")
        tc.finalize_assistant_turn()
        assert len(tc._turns) == 4
        assert "User: What are my gaps?" in tc.full_transcript()


# --- Layer 2: OPENAI_TOOLS ---


class TestOpenAITools:

    def test_is_list(self):
        assert isinstance(OPENAI_TOOLS, list)

    def test_all_have_required_fields(self):
        for tool in OPENAI_TOOLS:
            assert tool["type"] == "function"
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool

    def test_expected_tool_names(self):
        names = {t["name"] for t in OPENAI_TOOLS}
        expected = {"checkin", "score", "get_protocols", "get_daily_snapshot",
                     "log_workout", "log_meal", "get_workout_program"}
        assert names == expected

    def test_log_workout_requires_exercises(self):
        tool = next(t for t in OPENAI_TOOLS if t["name"] == "log_workout")
        assert "exercises" in tool["parameters"]["required"]

    def test_log_meal_requires_description_and_protein(self):
        tool = next(t for t in OPENAI_TOOLS if t["name"] == "log_meal")
        assert "description" in tool["parameters"]["required"]
        assert "protein_g" in tool["parameters"]["required"]


# --- Layer 3: dispatch_tool_call ---


class TestDispatchToolCall:

    def test_dispatches_checkin_with_user_id(self):
        mock_fn = MagicMock(return_value={"score": 72})
        with patch("mcp_server.tools._checkin", mock_fn, create=True):
            result = dispatch_tool_call("checkin", {}, "andrew")
        parsed = json.loads(result)
        assert parsed == {"score": 72}
        mock_fn.assert_called_once_with(user_id="andrew")

    def test_unknown_tool_returns_error(self):
        result = dispatch_tool_call("nonexistent_tool", {}, "andrew")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown tool" in parsed["error"]

    def test_exception_returns_error(self):
        with patch("mcp_server.tools._score", side_effect=RuntimeError("DB down"), create=True):
            result = dispatch_tool_call("score", {}, "andrew")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "DB down" in parsed["error"]

    def test_log_workout_passes_args(self):
        mock_fn = MagicMock(return_value={"logged": True})
        with patch("mcp_server.tools._log_workout", mock_fn, create=True):
            result = dispatch_tool_call(
                "log_workout",
                {"exercises": "Bench 5x5 @185", "sentiment": "good"},
                "andrew",
            )
        mock_fn.assert_called_once_with(
            exercises="Bench 5x5 @185", sentiment="good", user_id="andrew"
        )
        assert json.loads(result)["logged"] is True


# --- Layer 4: build_session_context ---


class TestBuildSessionContext:

    def _mock_db(self, name=None):
        """Create mock DB returning given name."""
        mock_db = MagicMock()
        row = {"name": name} if name else None
        mock_db.execute.return_value.fetchone.return_value = row
        return mock_db

    def test_includes_coaching_style(self):
        with patch("mcp_server.tools._checkin", side_effect=Exception, create=True):
            with patch("mcp_server.tools._score", side_effect=Exception, create=True):
                with patch("engine.gateway.voice_bridge.init_db", return_value=None):
                    with patch("engine.gateway.voice_bridge.get_db", return_value=self._mock_db()):
                        ctx = build_session_context("andrew")
        assert "COACH" in ctx
        assert "em dashes" in ctx

    def test_includes_date(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with patch("mcp_server.tools._checkin", side_effect=Exception, create=True):
            with patch("mcp_server.tools._score", side_effect=Exception, create=True):
                with patch("engine.gateway.voice_bridge.init_db", return_value=None):
                    with patch("engine.gateway.voice_bridge.get_db", return_value=self._mock_db()):
                        ctx = build_session_context("andrew")
        assert today in ctx

    def test_includes_client_name(self):
        with patch("mcp_server.tools._checkin", side_effect=Exception, create=True):
            with patch("mcp_server.tools._score", side_effect=Exception, create=True):
                with patch("engine.gateway.voice_bridge.init_db", return_value=None):
                    with patch("engine.gateway.voice_bridge.get_db", return_value=self._mock_db("Andrew")):
                        ctx = build_session_context("andrew")
        assert "Andrew" in ctx

    def test_includes_checkin_data(self):
        checkin = {
            "garmin": {"hrv_rmssd_avg": 64.7, "resting_hr": 48.4, "sleep_duration_avg": 6.0},
            "coverage_score": 83,
        }
        with patch("mcp_server.tools._checkin", return_value=checkin, create=True):
            with patch("mcp_server.tools._score", return_value={}, create=True):
                with patch("engine.gateway.voice_bridge.init_db", return_value=None):
                    with patch("engine.gateway.voice_bridge.get_db", return_value=self._mock_db("Andrew")):
                        ctx = build_session_context("andrew")
        assert "64.7" in ctx
        assert "48.4" in ctx

    def test_includes_score(self):
        with patch("mcp_server.tools._checkin", return_value={}, create=True):
            with patch("mcp_server.tools._score", return_value={"coverage_score": 83}, create=True):
                with patch("engine.gateway.voice_bridge.init_db", return_value=None):
                    with patch("engine.gateway.voice_bridge.get_db", return_value=self._mock_db("Andrew")):
                        ctx = build_session_context("andrew")
        assert "coverage_score" in ctx

    def test_handles_all_failures_gracefully(self):
        """Even if everything fails, we get a valid prompt."""
        with patch("mcp_server.tools._checkin", side_effect=Exception("boom"), create=True):
            with patch("mcp_server.tools._score", side_effect=Exception("boom"), create=True):
                with patch("engine.gateway.voice_bridge.init_db", side_effect=Exception("no db")):
                    ctx = build_session_context("andrew")
        assert "Milo" in ctx
        assert "andrew" in ctx  # falls back to user_id as name


# --- Layer 5: save_transcript ---


class TestSaveTranscript:

    def _make_db(self):
        """Create in-memory SQLite with conversation_message schema."""
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("""
            CREATE TABLE conversation_message (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sender_id TEXT,
                sender_name TEXT,
                channel TEXT,
                session_key TEXT,
                message_id TEXT,
                timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        return db

    def test_saves_raw_turns_plus_summary(self):
        db = self._make_db()
        tc = TranscriptCollector()
        tc.add_user_transcript("How did I sleep?")
        tc.add_delta("You slept 7.2 hours.")
        tc.finalize_assistant_turn()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Discussed sleep. Andrew slept 7.2 hours.")]

        with patch("engine.gateway.voice_bridge.init_db", return_value=None):
            with patch("engine.gateway.voice_bridge.get_db", return_value=db):
                with patch("anthropic.Anthropic") as mock_anthropic:
                    mock_anthropic.return_value.messages.create.return_value = mock_response
                    save_transcript("andrew", "MS123", tc)

        rows = db.execute("SELECT * FROM conversation_message ORDER BY id").fetchall()
        # 2 raw turns + 1 summary = 3 rows
        assert len(rows) == 3
        assert rows[0]["role"] == "user"
        assert rows[0]["content"] == "How did I sleep?"
        assert rows[1]["role"] == "assistant"
        assert rows[1]["content"] == "You slept 7.2 hours."
        # Summary row
        assert rows[2]["sender_name"] == "milo-voice-summary"
        assert "7.2 hours" in rows[2]["content"]

    def test_uses_voice_channel(self):
        db = self._make_db()
        tc = TranscriptCollector()
        tc.add_user_transcript("hello")
        tc.add_delta("hi")
        tc.finalize_assistant_turn()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Brief greeting exchange.")]

        with patch("engine.gateway.voice_bridge.init_db", return_value=None):
            with patch("engine.gateway.voice_bridge.get_db", return_value=db):
                with patch("anthropic.Anthropic") as mock_anthropic:
                    mock_anthropic.return_value.messages.create.return_value = mock_response
                    save_transcript("andrew", "MS456", tc)

        rows = db.execute("SELECT channel FROM conversation_message").fetchall()
        assert all(r["channel"] == "voice" for r in rows)

    def test_session_key_format(self):
        db = self._make_db()
        tc = TranscriptCollector()
        tc.add_user_transcript("test")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Test call.")]

        with patch("engine.gateway.voice_bridge.init_db", return_value=None):
            with patch("engine.gateway.voice_bridge.get_db", return_value=db):
                with patch("anthropic.Anthropic") as mock_anthropic:
                    mock_anthropic.return_value.messages.create.return_value = mock_response
                    save_transcript("andrew", "MS789", tc)

        row = db.execute("SELECT session_key FROM conversation_message LIMIT 1").fetchone()
        assert row["session_key"] == "voice:MS789"

    def test_empty_transcript_noops(self):
        db = self._make_db()
        tc = TranscriptCollector()  # empty

        with patch("engine.gateway.voice_bridge.init_db", return_value=None):
            with patch("engine.gateway.voice_bridge.get_db", return_value=db):
                save_transcript("andrew", "MS000", tc)

        rows = db.execute("SELECT * FROM conversation_message").fetchall()
        assert len(rows) == 0

    def test_summary_fallback_on_llm_failure(self):
        """If Haiku fails, raw turns still saved, no summary row."""
        db = self._make_db()
        tc = TranscriptCollector()
        tc.add_user_transcript("How's my score?")
        tc.add_delta("83 percent coverage.")
        tc.finalize_assistant_turn()

        with patch("engine.gateway.voice_bridge.init_db", return_value=None):
            with patch("engine.gateway.voice_bridge.get_db", return_value=db):
                with patch("anthropic.Anthropic", side_effect=Exception("API down")):
                    save_transcript("andrew", "MS999", tc)

        rows = db.execute("SELECT * FROM conversation_message ORDER BY id").fetchall()
        assert len(rows) == 2  # raw turns only, no summary


# --- Layer 5b: Conversation history filters voice raw turns ---


class TestGetConversationsVoiceFiltering:

    def _make_db_with_messages(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("""
            CREATE TABLE conversation_message (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sender_id TEXT,
                sender_name TEXT,
                channel TEXT,
                session_key TEXT,
                message_id TEXT,
                timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        now = "2026-04-03T12:00:00Z"
        # WhatsApp message
        db.execute(
            "INSERT INTO conversation_message (user_id, role, content, sender_name, channel, session_key, timestamp, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("andrew", "user", "Logged 2 protein shakes", "Andrew", "whatsapp", "wa:123", now, now),
        )
        # Voice raw turns
        db.execute(
            "INSERT INTO conversation_message (user_id, role, content, sender_name, channel, session_key, timestamp, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("andrew", "user", "How did I sleep?", "andrew", "voice", "voice:MS1", now, now),
        )
        db.execute(
            "INSERT INTO conversation_message (user_id, role, content, sender_name, channel, session_key, timestamp, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("andrew", "assistant", "You slept 6 hours.", "milo-voice", "voice", "voice:MS1", now, now),
        )
        # Voice summary
        db.execute(
            "INSERT INTO conversation_message (user_id, role, content, sender_name, channel, session_key, timestamp, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("andrew", "assistant", "Discussed sleep duration. Andrew slept 6 hours, below 7hr target.", "milo-voice-summary", "voice", "voice:MS1", now, now),
        )
        db.commit()
        return db

    def test_filters_raw_voice_keeps_summary(self):
        db = self._make_db_with_messages()

        with patch("engine.gateway.db.get_db", return_value=db):
            from mcp_server.tools import _get_conversations
            result = _get_conversations("andrew", hours=168)

        messages = result["conversations"]["andrew"]
        contents = [m["content"] for m in messages]

        # WhatsApp message: kept
        assert "Logged 2 protein shakes" in contents
        # Voice summary: kept
        assert any("Discussed sleep" in c for c in contents)
        # Voice raw turns: filtered out
        assert "How did I sleep?" not in contents
        assert "You slept 6 hours." not in contents


# --- Layer 6: TwiML Incoming Call Handler ---


class TestIncomingCallHandler:

    def _make_app(self):
        from fastapi import FastAPI
        app = FastAPI()
        config = MagicMock()
        config.tunnel_domain = "auth.mybaseline.health"
        handler = create_incoming_call_handler(config)
        app.post("/api/voice/incoming")(handler)
        return app

    def test_known_caller_gets_stream_twiml(self):
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)

        with patch("engine.gateway.voice_bridge._lookup_user_by_phone", return_value="andrew"):
            resp = client.post("/api/voice/incoming", data={"From": "+14152009584"})

        assert resp.status_code == 200
        assert "<Connect>" in resp.text
        assert "<Stream" in resp.text
        assert 'name="user_id"' in resp.text
        assert 'value="andrew"' in resp.text
        assert "auth.mybaseline.health" in resp.text

    def test_unknown_caller_gets_rejection(self):
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)

        with patch("engine.gateway.voice_bridge._lookup_user_by_phone", return_value=None):
            resp = client.post("/api/voice/incoming", data={"From": "+19999999999"})

        assert resp.status_code == 200
        assert "<Say>" in resp.text
        assert "recognize" in resp.text
        assert "<Hangup/>" in resp.text
        assert "<Stream" not in resp.text


# --- Layer 7: WebSocket Bridge (key behaviors) ---


class TestWebSocketBridge:

    @pytest.mark.asyncio
    async def test_missing_user_id_closes_with_4001(self):
        from engine.gateway.voice_bridge import voice_ws_handler

        mock_ws = AsyncMock(spec=WebSocket)
        # Simulate: connected event, then start event with no customParameters
        start_msg = json.dumps({"event": "start", "start": {"streamSid": "MS123", "customParameters": {}}})
        connected_msg = json.dumps({"event": "connected"})
        mock_ws.iter_text = MagicMock(return_value=AsyncIterator([connected_msg, start_msg]))

        await voice_ws_handler(mock_ws)

        mock_ws.accept.assert_called_once()
        mock_ws.close.assert_called_once_with(code=4001)


# --- Layer 8: Route Registration ---


class TestRouteRegistration:

    def test_voice_routes_registered(self):
        from fastapi import FastAPI

        app = FastAPI()
        config = MagicMock()
        config.tunnel_domain = "auth.mybaseline.health"

        register_voice_routes(app, config)

        routes = [r.path for r in app.routes]
        assert "/api/voice/incoming" in routes
        assert "/api/voice/ws" in routes

    def test_voice_routes_in_gateway_app(self):
        """Voice routes must be registered in the actual gateway server."""
        from engine.gateway.server import create_app

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            app = create_app()

        routes = [r.path for r in app.routes]
        assert "/api/voice/incoming" in routes, f"Voice incoming not in routes: {routes}"
        assert "/api/voice/ws" in routes, f"Voice WS not in routes: {routes}"
