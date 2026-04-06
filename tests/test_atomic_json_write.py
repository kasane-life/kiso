"""Tests for atomic JSON file writes.

Verifies that _atomic_json_write produces valid JSON files and cleans up
temp files, preventing the corruption seen in Paul's 'Extra data: line 400' bug.
"""

import json
from pathlib import Path

import pytest

from mcp_server.tools import _atomic_json_write


class TestAtomicJsonWrite:
    """_atomic_json_write writes via tmp + os.replace for crash safety."""

    def test_writes_valid_json(self, tmp_path):
        path = tmp_path / "test.json"
        data = {"key": "value", "number": 42}
        _atomic_json_write(path, data)

        assert path.exists()
        with open(path) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_tmp_file_cleaned_up(self, tmp_path):
        path = tmp_path / "test.json"
        _atomic_json_write(path, {"a": 1})

        tmp_file = path.with_suffix(".json.tmp")
        assert not tmp_file.exists(), "Temp file should be removed after atomic write"

    def test_overwrites_existing_file(self, tmp_path):
        path = tmp_path / "test.json"
        _atomic_json_write(path, {"version": 1})
        _atomic_json_write(path, {"version": 2})

        with open(path) as f:
            loaded = json.load(f)
        assert loaded["version"] == 2

    def test_preserves_indent(self, tmp_path):
        path = tmp_path / "test.json"
        _atomic_json_write(path, {"a": 1}, indent=2)

        content = path.read_text()
        assert "  " in content  # indented

    def test_handles_default_str(self, tmp_path):
        """Datetime and other non-serializable types use default=str."""
        from datetime import datetime
        path = tmp_path / "test.json"
        data = {"ts": datetime(2026, 4, 5, 12, 0)}
        _atomic_json_write(path, data, default=str)

        with open(path) as f:
            loaded = json.load(f)
        assert "2026" in loaded["ts"]

    def test_list_data(self, tmp_path):
        """Daily series files are JSON arrays, not objects."""
        path = tmp_path / "daily.json"
        data = [{"date": "2026-04-05", "rhr": 52}, {"date": "2026-04-04", "rhr": 54}]
        _atomic_json_write(path, data)

        with open(path) as f:
            loaded = json.load(f)
        assert len(loaded) == 2
        assert loaded[0]["rhr"] == 52

    def test_creates_parent_dirs(self, tmp_path):
        """Write should work even if parent dir exists (it always should, but verify)."""
        path = tmp_path / "data" / "users" / "paul" / "test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(path, {"ok": True})
        assert path.exists()
