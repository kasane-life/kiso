"""Tests for Google Calendar integration."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.integrations.gcal import (
    GoogleCalendarClient,
    _format_event,
    _parse_time,
    _ensure_tz,
    SERVICE_NAME,
)


# --- _parse_time tests ---

def test_parse_time_datetime():
    result = _parse_time("2026-06-26T09:00:00")
    assert result == {"dateTime": "2026-06-26T09:00:00", "timeZone": "America/Los_Angeles"}


def test_parse_time_date_only():
    result = _parse_time("2026-06-26")
    assert result == {"date": "2026-06-26"}


# --- _ensure_tz tests ---

def test_ensure_tz_with_z():
    assert _ensure_tz("2026-06-26T09:00:00Z") == "2026-06-26T09:00:00Z"


def test_ensure_tz_with_offset():
    assert _ensure_tz("2026-06-26T09:00:00-07:00") == "2026-06-26T09:00:00-07:00"


def test_ensure_tz_date_only():
    result = _ensure_tz("2026-06-26")
    assert "T00:00:00" in result


def test_ensure_tz_naive_datetime():
    result = _ensure_tz("2026-06-26T09:00:00")
    assert result == "2026-06-26T09:00:00"


# --- _format_event tests ---

def test_format_event_timed():
    raw = {
        "id": "abc123",
        "summary": "Lab retest",
        "start": {"dateTime": "2026-06-26T09:00:00-07:00"},
        "end": {"dateTime": "2026-06-26T09:30:00-07:00"},
        "location": "Quest Diagnostics",
        "description": "Full panel",
        "status": "confirmed",
        "htmlLink": "https://calendar.google.com/event?id=abc123",
    }
    result = _format_event(raw)
    assert result["id"] == "abc123"
    assert result["summary"] == "Lab retest"
    assert result["start"] == "2026-06-26T09:00:00-07:00"
    assert result["end"] == "2026-06-26T09:30:00-07:00"
    assert result["location"] == "Quest Diagnostics"
    assert result["description"] == "Full panel"
    assert result["status"] == "confirmed"
    assert result["html_link"] == "https://calendar.google.com/event?id=abc123"


def test_format_event_all_day():
    raw = {
        "id": "def456",
        "summary": "Rest day",
        "start": {"date": "2026-06-27"},
        "end": {"date": "2026-06-28"},
    }
    result = _format_event(raw)
    assert result["start"] == "2026-06-27"
    assert result["end"] == "2026-06-28"
    assert result["location"] == ""
    assert result["description"] == ""


def test_format_event_missing_fields():
    raw = {"start": {}, "end": {}}
    result = _format_event(raw)
    assert result["id"] == ""
    assert result["summary"] == "(no title)"
    assert result["start"] == ""
    assert result["end"] == ""


# --- GoogleCalendarClient tests ---

@pytest.fixture
def mock_token_store():
    store = MagicMock()
    store.load_token.return_value = {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
    }
    return store


@pytest.fixture
def mock_service():
    service = MagicMock()
    return service


def test_missing_tokens():
    store = MagicMock()
    store.load_token.return_value = None
    client = GoogleCalendarClient(user_id="nobody", token_store=store)

    # Mock the google import inside _get_credentials
    mock_creds_module = MagicMock()
    with patch.dict("sys.modules", {"google.oauth2.credentials": mock_creds_module, "google.oauth2": MagicMock(), "google": MagicMock()}):
        with pytest.raises(RuntimeError, match="No Google Calendar tokens"):
            client._get_credentials()


@patch("engine.integrations.gcal.GoogleCalendarClient._get_service")
def test_list_events(mock_get_service, mock_token_store):
    mock_svc = MagicMock()
    mock_svc.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "e1",
                "summary": "Training",
                "start": {"dateTime": "2026-06-26T15:00:00-07:00"},
                "end": {"dateTime": "2026-06-26T16:30:00-07:00"},
                "status": "confirmed",
            },
            {
                "id": "e2",
                "summary": "Wind-down",
                "start": {"dateTime": "2026-06-26T20:00:00-07:00"},
                "end": {"dateTime": "2026-06-26T21:15:00-07:00"},
                "status": "confirmed",
            },
        ]
    }
    mock_get_service.return_value = mock_svc

    client = GoogleCalendarClient(user_id="default", token_store=mock_token_store)
    events = client.list_events(max_results=5)

    assert len(events) == 2
    assert events[0]["summary"] == "Training"
    assert events[1]["summary"] == "Wind-down"


@patch("engine.integrations.gcal.GoogleCalendarClient._get_service")
def test_create_event(mock_get_service, mock_token_store):
    mock_svc = MagicMock()
    mock_svc.events.return_value.insert.return_value.execute.return_value = {
        "id": "new123",
        "summary": "Lab retest",
        "start": {"dateTime": "2026-06-26T09:00:00-07:00"},
        "end": {"dateTime": "2026-06-26T09:30:00-07:00"},
        "status": "confirmed",
        "htmlLink": "https://calendar.google.com/event?id=new123",
    }
    mock_get_service.return_value = mock_svc

    client = GoogleCalendarClient(user_id="default", token_store=mock_token_store)
    result = client.create_event(
        summary="Lab retest",
        start="2026-06-26T09:00:00",
        end="2026-06-26T09:30:00",
        description="T total + free, SHBG, FSH, LH",
    )

    assert result["id"] == "new123"
    assert result["summary"] == "Lab retest"
    # Verify the API was called with correct body
    call_kwargs = mock_svc.events.return_value.insert.call_args
    body = call_kwargs[1]["body"] if "body" in call_kwargs[1] else call_kwargs[0][0]
    assert body["summary"] == "Lab retest"
    assert body["description"] == "T total + free, SHBG, FSH, LH"


@patch("engine.integrations.gcal.GoogleCalendarClient._get_service")
def test_search_events(mock_get_service, mock_token_store):
    mock_svc = MagicMock()
    mock_svc.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "e1",
                "summary": "Bio Leadership Summit",
                "start": {"dateTime": "2026-04-14T09:00:00-07:00"},
                "end": {"dateTime": "2026-04-14T17:00:00-07:00"},
            }
        ]
    }
    mock_get_service.return_value = mock_svc

    client = GoogleCalendarClient(user_id="default", token_store=mock_token_store)
    events = client.search_events(query="Bio Leadership")

    assert len(events) == 1
    assert events[0]["summary"] == "Bio Leadership Summit"


@patch("engine.integrations.gcal.GoogleCalendarClient._get_credentials")
def test_token_refresh(mock_get_creds, mock_token_store):
    """Verify that credentials are obtained and service can be built."""
    mock_creds = MagicMock()
    mock_creds.expired = False
    mock_get_creds.return_value = mock_creds

    client = GoogleCalendarClient(user_id="default", token_store=mock_token_store)

    mock_build = MagicMock(return_value=MagicMock())
    mock_discovery = MagicMock()
    mock_discovery.build = mock_build
    with patch.dict("sys.modules", {"googleapiclient.discovery": mock_discovery, "googleapiclient": MagicMock()}):
        # Need to reimport to pick up the mock
        import importlib
        import engine.integrations.gcal as gcal_module
        # Directly call _get_service which does lazy import
        service = client._get_service()
        mock_build.assert_called_once_with("calendar", "v3", credentials=mock_creds)


# --- Standalone tool function tests ---

@patch("engine.integrations.gcal.GoogleCalendarClient.list_events")
def test_calendar_list_events_tool(mock_list):
    from mcp_server.tools import _calendar_list_events

    mock_list.return_value = [{"id": "e1", "summary": "Test"}]
    result = _calendar_list_events(max_results=5, user_id="test_user")
    assert result["count"] == 1
    assert result["events"][0]["summary"] == "Test"


@patch("engine.integrations.gcal.GoogleCalendarClient.create_event")
def test_calendar_create_event_tool(mock_create):
    from mcp_server.tools import _calendar_create_event

    mock_create.return_value = {"id": "new1", "summary": "Lab retest"}
    result = _calendar_create_event(
        summary="Lab retest",
        start="2026-06-26T09:00:00",
        end="2026-06-26T09:30:00",
        user_id="test_user",
    )
    assert result["created"] is True
    assert result["event"]["summary"] == "Lab retest"


@patch("engine.integrations.gcal.GoogleCalendarClient.search_events")
def test_calendar_search_events_tool(mock_search):
    from mcp_server.tools import _calendar_search_events

    mock_search.return_value = [{"id": "e1", "summary": "Training"}]
    result = _calendar_search_events(query="Training", user_id="test_user")
    assert result["count"] == 1
    assert result["events"][0]["summary"] == "Training"


# --- Scope consistency ---

def test_scope_is_calendar_events():
    """Scope should be calendar.events (sensitive), not calendar (broad)."""
    from engine.integrations.gcal import SCOPES
    assert SCOPES == ["https://www.googleapis.com/auth/calendar.events"]


# --- Update event tests ---

@patch("engine.integrations.gcal.GoogleCalendarClient._get_service")
def test_update_event(mock_get_service, mock_token_store):
    mock_svc = MagicMock()
    mock_svc.events.return_value.get.return_value.execute.return_value = {
        "id": "e1",
        "summary": "Old title",
        "start": {"dateTime": "2026-06-26T09:00:00-07:00"},
        "end": {"dateTime": "2026-06-26T10:00:00-07:00"},
        "status": "confirmed",
    }
    mock_svc.events.return_value.update.return_value.execute.return_value = {
        "id": "e1",
        "summary": "New title",
        "start": {"dateTime": "2026-06-26T09:00:00-07:00"},
        "end": {"dateTime": "2026-06-26T10:00:00-07:00"},
        "status": "confirmed",
    }
    mock_get_service.return_value = mock_svc

    client = GoogleCalendarClient(user_id="default", token_store=mock_token_store)
    result = client.update_event(event_id="e1", summary="New title")

    assert result["summary"] == "New title"
    mock_svc.events.return_value.update.assert_called_once()


@patch("engine.integrations.gcal.GoogleCalendarClient._get_service")
def test_update_event_partial(mock_get_service, mock_token_store):
    """Only provided fields should change."""
    mock_svc = MagicMock()
    existing = {
        "id": "e1",
        "summary": "Training",
        "start": {"dateTime": "2026-06-26T15:00:00-07:00"},
        "end": {"dateTime": "2026-06-26T16:30:00-07:00"},
        "location": "Pacific Strength",
        "status": "confirmed",
    }
    mock_svc.events.return_value.get.return_value.execute.return_value = existing.copy()
    mock_svc.events.return_value.update.return_value.execute.return_value = {
        **existing, "location": "Home gym"
    }
    mock_get_service.return_value = mock_svc

    client = GoogleCalendarClient(user_id="default", token_store=mock_token_store)
    result = client.update_event(event_id="e1", location="Home gym")

    assert result["location"] == "Home gym"
    assert result["summary"] == "Training"  # unchanged


# --- Delete event tests ---

@patch("engine.integrations.gcal.GoogleCalendarClient._get_service")
def test_delete_event(mock_get_service, mock_token_store):
    mock_svc = MagicMock()
    mock_svc.events.return_value.delete.return_value.execute.return_value = None
    mock_get_service.return_value = mock_svc

    client = GoogleCalendarClient(user_id="default", token_store=mock_token_store)
    result = client.delete_event(event_id="e1")

    assert result is True
    mock_svc.events.return_value.delete.assert_called_once()


# --- Tool wrapper tests for update/delete ---

@patch("engine.integrations.gcal.GoogleCalendarClient.update_event")
def test_calendar_update_event_tool(mock_update):
    from mcp_server.tools import _calendar_update_event

    mock_update.return_value = {"id": "e1", "summary": "Updated"}
    result = _calendar_update_event(event_id="e1", summary="Updated", user_id="test_user")
    assert result["updated"] is True
    assert result["event"]["summary"] == "Updated"


@patch("engine.integrations.gcal.GoogleCalendarClient.delete_event")
def test_calendar_delete_event_tool(mock_delete):
    from mcp_server.tools import _calendar_delete_event

    mock_delete.return_value = True
    result = _calendar_delete_event(event_id="e1", user_id="test_user")
    assert result["deleted"] is True
    assert result["event_id"] == "e1"


def test_calendar_update_requires_user_id():
    from mcp_server.tools import _calendar_update_event
    result = _calendar_update_event(event_id="e1", summary="Test")
    assert "error" in result


def test_calendar_delete_requires_user_id():
    from mcp_server.tools import _calendar_delete_event
    result = _calendar_delete_event(event_id="e1")
    assert "error" in result


def test_update_delete_in_tool_registry():
    from mcp_server.tools import TOOL_REGISTRY, _calendar_update_event, _calendar_delete_event
    assert "calendar_update_event" in TOOL_REGISTRY
    assert TOOL_REGISTRY["calendar_update_event"] is _calendar_update_event
    assert "calendar_delete_event" in TOOL_REGISTRY
    assert TOOL_REGISTRY["calendar_delete_event"] is _calendar_delete_event
