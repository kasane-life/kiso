"""Tests for structured JSON logging configuration."""

import ast
import json
import logging
import sys
from io import StringIO
from pathlib import Path

import pytest

from engine.gateway.log_config import JsonFormatter, configure_logging

GATEWAY_DIR = Path(__file__).parent.parent / "engine" / "gateway"


class TestJsonFormatter:
    """JsonFormatter turns log records into structured JSON."""

    def setup_method(self):
        self.formatter = JsonFormatter()

    def _format(self, logger_name="test", level=logging.INFO, msg="hello",
                exc_info=None, extra=None):
        record = logging.LogRecord(
            name=logger_name, level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=exc_info,
        )
        if extra:
            for k, v in extra.items():
                setattr(record, k, v)
        return self.formatter.format(record)

    def test_output_is_valid_json(self):
        line = self._format()
        parsed = json.loads(line)
        assert isinstance(parsed, dict)

    def test_schema_has_required_fields(self):
        parsed = json.loads(self._format(
            logger_name="health-engine.api", level=logging.WARNING, msg="test msg",
        ))
        assert "ts" in parsed
        assert parsed["level"] == "warning"
        assert parsed["logger"] == "health-engine.api"
        assert parsed["message"] == "test msg"

    def test_ts_is_iso8601(self):
        parsed = json.loads(self._format())
        ts = parsed["ts"]
        assert "T" in ts
        # Should have timezone info
        assert "+" in ts or ts.endswith("Z") or "-" in ts.split("T")[1]

    def test_exception_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        line = self._format(exc_info=exc_info)
        parsed = json.loads(line)
        assert "exception" in parsed
        assert "ValueError: boom" in parsed["exception"]

    def test_no_exception_field_when_none(self):
        parsed = json.loads(self._format())
        assert "exception" not in parsed

    def test_extra_fields_included(self):
        parsed = json.loads(self._format(extra={"user_id": "paul", "tool": "checkin"}))
        assert parsed["user_id"] == "paul"
        assert parsed["tool"] == "checkin"

    def test_extra_fields_dont_overwrite_core(self):
        """Extra fields named 'level' or 'logger' don't clobber the core schema."""
        parsed = json.loads(self._format(
            logger_name="real.logger",
            extra={"logger": "evil", "level": "evil"},
        ))
        assert parsed["logger"] == "real.logger"
        assert parsed["level"] == "info"


class TestConfigureLogging:
    """configure_logging() sets up root logger with JSON on stdout."""

    def test_root_logger_gets_json_handler(self):
        # Save and restore root logger state
        root = logging.getLogger()
        old_handlers = root.handlers[:]
        old_level = root.level
        try:
            configure_logging()
            # Root should have at least one handler with JsonFormatter
            json_handlers = [
                h for h in root.handlers
                if isinstance(h.formatter, JsonFormatter)
            ]
            assert len(json_handlers) >= 1
        finally:
            root.handlers = old_handlers
            root.level = old_level

    def test_child_logger_emits_json_to_stdout(self):
        root = logging.getLogger()
        old_handlers = root.handlers[:]
        old_level = root.level
        try:
            buf = StringIO()
            configure_logging(stream=buf)
            child = logging.getLogger("health-engine.test-child")
            child.info("structured output")
            output = buf.getvalue()
            parsed = json.loads(output.strip())
            assert parsed["message"] == "structured output"
            assert parsed["logger"] == "health-engine.test-child"
        finally:
            root.handlers = old_handlers
            root.level = old_level

    def test_idempotent(self):
        """Calling configure_logging twice doesn't double handlers."""
        root = logging.getLogger()
        old_handlers = root.handlers[:]
        old_level = root.level
        try:
            buf = StringIO()
            configure_logging(stream=buf)
            configure_logging(stream=buf)
            json_handlers = [
                h for h in root.handlers
                if isinstance(h.formatter, JsonFormatter)
            ]
            assert len(json_handlers) == 1
        finally:
            root.handlers = old_handlers
            root.level = old_level


class TestNoPrintInGateway:
    """Lint: gateway code must use logger, not print()."""

    def test_no_print_in_gateway(self):
        """No print() calls in engine/gateway/ — use logging.getLogger() instead."""
        violations = []
        for py_file in sorted(GATEWAY_DIR.glob("*.py")):
            tree = ast.parse(py_file.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "print"):
                    violations.append(f"{py_file.name}:{node.lineno}")
        assert violations == [], (
            f"print() found in gateway code (use logger instead): {violations}"
        )
