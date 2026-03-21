"""Tests for log_labs and get_labs MCP tools."""

import json
import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.tools import register_tools


class FakeMCP:
    """Minimal stand-in for FastMCP that captures registered tools."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator

    def resource(self, uri):
        def decorator(fn):
            return fn
        return decorator


@pytest.fixture
def tools():
    mcp = FakeMCP()
    register_tools(mcp)
    return mcp.tools


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Patch _data_dir to use a temp directory."""
    import mcp_server.tools as mod
    original = mod._data_dir

    def patched_data_dir(user_id=None):
        return tmp_path

    monkeypatch.setattr(mod, "_data_dir", patched_data_dir)
    return tmp_path


# --- Alias normalization ---

def test_alias_apo_b(tools, tmp_data):
    result = tools["log_labs"](results={"Apo B": 72}, date="2026-01-01", source="Quest")
    assert result["logged"] is True
    assert "apob" in result["biomarkers"]


def test_alias_ldl(tools, tmp_data):
    result = tools["log_labs"](results={"LDL": 90}, date="2026-01-01")
    assert "ldl_c" in result["biomarkers"]


def test_alias_ldl_cholesterol(tools, tmp_data):
    result = tools["log_labs"](results={"LDL cholesterol": 90}, date="2026-01-01")
    assert "ldl_c" in result["biomarkers"]


def test_alias_hemoglobin_a1c(tools, tmp_data):
    result = tools["log_labs"](results={"hemoglobin a1c": 5.3}, date="2026-01-01")
    assert "hba1c" in result["biomarkers"]


def test_alias_hscrp(tools, tmp_data):
    result = tools["log_labs"](results={"hs-CRP": 0.5}, date="2026-01-01")
    assert "hscrp" in result["biomarkers"]


def test_alias_lpa(tools, tmp_data):
    result = tools["log_labs"](results={"Lp(a)": 10}, date="2026-01-01")
    assert "lpa" in result["biomarkers"]


def test_alias_case_insensitive(tools, tmp_data):
    result = tools["log_labs"](results={"APOLIPOPROTEIN B": 72}, date="2026-01-01")
    assert "apob" in result["biomarkers"]


def test_canonical_key_passthrough(tools, tmp_data):
    result = tools["log_labs"](results={"apob": 72, "ldl_c": 90}, date="2026-01-01")
    assert "apob" in result["biomarkers"]
    assert "ldl_c" in result["biomarkers"]


# --- Range validation ---

def test_range_warning_out_of_range(tools, tmp_data):
    result = tools["log_labs"](results={"apob": 5000}, date="2026-01-01")
    assert result["logged"] is True
    assert len(result["warnings"]) == 1
    assert "outside expected range" in result["warnings"][0]
    # Value should still be stored
    lab_data = json.loads((tmp_data / "lab_results.json").read_text())
    assert lab_data["latest"]["apob"] == 5000


def test_range_valid_no_warning(tools, tmp_data):
    result = tools["log_labs"](results={"apob": 72}, date="2026-01-01")
    assert result["warnings"] == []


def test_invalid_value_skipped(tools, tmp_data):
    result = tools["log_labs"](results={"apob": "not_a_number"}, date="2026-01-01")
    assert result["logged"] is False
    assert "could not parse" in result["warnings"][0]


# --- Multi-draw merge ---

def test_two_draws_latest_updated(tools, tmp_data):
    tools["log_labs"](results={"apob": 65}, date="2024-09-01", source="Quest")
    result = tools["log_labs"](results={"apob": 72}, date="2025-06-01", source="Quest")
    assert result["total_draws"] == 2
    lab_data = json.loads((tmp_data / "lab_results.json").read_text())
    # Latest should be the newer value (2025-06-01)
    assert lab_data["latest"]["apob"] == 72


def test_latest_uses_newest_per_biomarker(tools, tmp_data):
    tools["log_labs"](results={"apob": 65, "ldl_c": 100}, date="2024-01-01")
    tools["log_labs"](results={"apob": 72}, date="2025-01-01")
    # ldl_c only in first draw, apob updated in second
    lab_data = json.loads((tmp_data / "lab_results.json").read_text())
    assert lab_data["latest"]["apob"] == 72
    assert lab_data["latest"]["ldl_c"] == 100


def test_draws_sorted_newest_first(tools, tmp_data):
    tools["log_labs"](results={"apob": 65}, date="2024-01-01")
    tools["log_labs"](results={"apob": 72}, date="2025-06-01")
    tools["log_labs"](results={"apob": 70}, date="2024-09-01")
    lab_data = json.loads((tmp_data / "lab_results.json").read_text())
    dates = [d["date"] for d in lab_data["draws"]]
    assert dates == ["2025-06-01", "2024-09-01", "2024-01-01"]


# --- Scored vs extra field classification ---

def test_scored_fields_classified(tools, tmp_data):
    result = tools["log_labs"](
        results={"apob": 72, "ldl_c": 87, "testosterone_total": 664},
        date="2026-01-01",
    )
    assert "apob" in result["scored_fields"]
    assert "ldl_c" in result["scored_fields"]
    assert "testosterone_total" in result["extra_fields"]


# --- get_labs ---

def test_get_labs_empty(tools, tmp_data):
    result = tools["get_labs"]()
    assert result["has_labs"] is False
    assert result["draws"] == []


def test_get_labs_after_logging(tools, tmp_data):
    tools["log_labs"](results={"apob": 72, "ldl_c": 87}, date="2026-02-13", source="Quest")
    result = tools["get_labs"]()
    assert result["has_labs"] is True
    assert result["total_draws"] == 1
    assert result["total_biomarkers"] == 2
    assert result["latest"]["apob"] == 72


# --- Date defaults ---

def test_date_defaults_to_today(tools, tmp_data):
    from datetime import datetime
    result = tools["log_labs"](results={"apob": 72})
    assert result["date"] == datetime.now().strftime("%Y-%m-%d")


# --- Source defaults ---

def test_source_defaults_to_unknown(tools, tmp_data):
    result = tools["log_labs"](results={"apob": 72})
    assert result["source"] == "unknown"


# --- Empty results ---

def test_empty_results(tools, tmp_data):
    result = tools["log_labs"](results={})
    assert result["logged"] is False
