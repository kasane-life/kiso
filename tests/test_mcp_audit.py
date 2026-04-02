"""Tests for MCP transport audit logging.

The HTTP API logs every tool call to api_audit.jsonl. MCP tool calls
go through a separate ASGI transport and historically produced NO audit
entries. These tests verify that MCP calls are now audited.
"""

import json
import os
from unittest.mock import patch

import pytest

from engine.gateway.api import _audit_log
from engine.gateway.config import GatewayConfig
from engine.gateway.server import create_app


@pytest.fixture
def audit_log_path(tmp_path, monkeypatch):
    """Redirect audit log to a temp file."""
    path = str(tmp_path / "api_audit.jsonl")
    monkeypatch.setattr("engine.gateway.api._AUDIT_LOG_PATH", path)
    return path


def _read_audit(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


class TestMCPAuditLog:
    """MCP tool calls should produce audit log entries."""

    def test_mcp_tool_call_is_audited(self, audit_log_path):
        """When a tools/call JSON-RPC arrives over MCP, it should write an audit entry."""
        _audit_log(
            "checkin", "paul",
            {"greeting": "morning check-in"},
            {"status": "ok"},
            None, 150,
            source="mcp",
        )
        entries = _read_audit(audit_log_path)
        assert len(entries) == 1
        e = entries[0]
        assert e["tool"] == "checkin"
        assert e["user_id"] == "paul"
        assert e["source"] == "mcp"
        assert e["status"] == "ok"

    def test_http_calls_default_to_http_source(self, audit_log_path):
        """Existing HTTP audit entries should have source='http' by default."""
        _audit_log(
            "checkin", "paul",
            {"greeting": "hi"},
            {"status": "ok"},
            None, 50,
        )
        entries = _read_audit(audit_log_path)
        assert entries[0]["source"] == "http"

    def test_mcp_audit_captures_tool_name(self, audit_log_path):
        """The audit entry should capture which MCP tool was called."""
        _audit_log(
            "score", "andrew",
            {},
            {"coverage": 0.65},
            None, 200,
            source="mcp",
        )
        entries = _read_audit(audit_log_path)
        assert entries[0]["tool"] == "score"

    def test_mcp_audit_captures_error(self, audit_log_path):
        """MCP errors should be logged like HTTP errors."""
        _audit_log(
            "pull_garmin", "paul",
            {},
            None,
            "Token expired",
            120,
            source="mcp",
        )
        entries = _read_audit(audit_log_path)
        e = entries[0]
        assert e["status"] == "error"
        assert e["error"] == "Token expired"
        assert e["source"] == "mcp"


class TestMCPMiddlewareAudit:
    """The MCPAuthMiddleware should call _audit_log for tools/call requests."""

    def test_middleware_logs_tool_call(self, audit_log_path):
        """When MCPAuthMiddleware intercepts a tools/call, it writes an audit entry."""
        from engine.gateway.server import MCPAuthMiddleware

        calls_received = []

        async def fake_app(scope, receive, send):
            # Consume the (possibly modified) request body
            msg = await receive()
            calls_received.append(msg)

        middleware = MCPAuthMiddleware(fake_app, resolve_user_id=lambda t: "paul")

        import asyncio

        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer test-token")],
            "query_string": b"",
        }

        body = json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "checkin",
                "arguments": {"greeting": "morning"},
            },
        }).encode()

        async def receive():
            return {"type": "http.request", "body": body}

        async def send(msg):
            pass

        asyncio.get_event_loop().run_until_complete(
            middleware(scope, receive, send)
        )

        entries = _read_audit(audit_log_path)
        assert len(entries) == 1
        e = entries[0]
        assert e["tool"] == "checkin"
        assert e["user_id"] == "paul"
        assert e["source"] == "mcp"

    def test_middleware_skips_non_tool_calls(self, audit_log_path):
        """Non tools/call JSON-RPC messages should not produce audit entries."""
        from engine.gateway.server import MCPAuthMiddleware

        async def fake_app(scope, receive, send):
            await receive()

        middleware = MCPAuthMiddleware(fake_app, resolve_user_id=lambda t: "paul")

        import asyncio

        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer test-token")],
            "query_string": b"",
        }

        body = json.dumps({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {},
        }).encode()

        async def receive():
            return {"type": "http.request", "body": body}

        async def send(msg):
            pass

        asyncio.get_event_loop().run_until_complete(
            middleware(scope, receive, send)
        )

        entries = _read_audit(audit_log_path)
        assert len(entries) == 0
