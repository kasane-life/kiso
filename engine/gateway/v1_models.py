"""Pydantic models for the Kasane v1 API.

camelCase aliases match iOS JSON conventions. The API accepts and returns
camelCase keys while Python code uses snake_case internally.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


def _to_camel(s: str) -> str:
    parts = s.split("_")
    return parts[0] + "".join(w.capitalize() for w in parts[1:])


class CamelModel(BaseModel):
    model_config = {
        "alias_generator": _to_camel,
        "populate_by_name": True,
        "serialize_by_alias": True,
    }


# --- Sync protocol ---

class SyncChange(CamelModel):
    entity: str
    id: str
    action: str  # "upsert" or "delete"
    data: dict[str, Any] = {}
    updated_at: str


class SyncRequest(CamelModel):
    device_id: str
    person_id: str
    last_sync_at: str | None = None
    changes: list[SyncChange] = []


class SyncResponse(CamelModel):
    server_changes: list[dict[str, Any]]
    sync_at: str
    stats: dict[str, int]


# --- Person ---

class PersonCreate(CamelModel):
    id: str | None = None
    name: str
    relationship: str | None = None
    date_of_birth: str | None = None
    biological_sex: str | None = None
    conditions_json: str | None = None
    medications: str | None = None
    family_history_json: str | None = None
    health_notes: str | None = None
    health_engine_user_id: str | None = None


class PersonUpdate(CamelModel):
    name: str | None = None
    relationship: str | None = None
    date_of_birth: str | None = None
    biological_sex: str | None = None
    conditions_json: str | None = None
    medications: str | None = None
    family_history_json: str | None = None
    health_notes: str | None = None
    health_engine_user_id: str | None = None


class PersonOut(CamelModel):
    id: str
    name: str
    relationship: str | None = None
    date_of_birth: str | None = None
    biological_sex: str | None = None
    conditions_json: str | None = None
    medications: str | None = None
    family_history_json: str | None = None
    health_notes: str | None = None
    health_engine_user_id: str | None = None
    created_at: str
    updated_at: str
    deleted_at: str | None = None


# --- Habit ---

class HabitCreate(CamelModel):
    id: str | None = None
    title: str
    purpose: str | None = None
    category: str | None = None
    emoji: str | None = None
    anchor: str | None = None
    state: str = "active"
    sort_order: int = 0
    identity_threshold: float | None = None
    show_in_today: bool = True


class HabitUpdate(CamelModel):
    title: str | None = None
    purpose: str | None = None
    category: str | None = None
    emoji: str | None = None
    anchor: str | None = None
    state: str | None = None
    sort_order: int | None = None
    identity_threshold: float | None = None
    graduated_at: str | None = None
    show_in_today: bool | None = None


class HabitOut(CamelModel):
    id: str
    person_id: str
    title: str
    purpose: str | None = None
    category: str | None = None
    emoji: str | None = None
    anchor: str | None = None
    state: str | None = None
    sort_order: int = 0
    identity_threshold: float | None = None
    graduated_at: str | None = None
    show_in_today: bool = True
    created_at: str
    updated_at: str
    deleted_at: str | None = None


# --- CheckIn ---

class CheckInCreate(CamelModel):
    id: str | None = None
    date: str
    completed: bool = False
    note: str | None = None


class CheckInOut(CamelModel):
    id: str
    habit_id: str
    date: str
    completed: bool = False
    note: str | None = None
    created_at: str
    updated_at: str
    deleted_at: str | None = None


# --- FocusPlan ---

class FocusPlanCreate(CamelModel):
    id: str | None = None
    generated_at: str | None = None
    health_snapshot: str | None = None
    reflection: str | None = None
    insight: str | None = None
    encouragement: str | None = None
    primary_action: str | None = None
    primary_anchor: str | None = None
    primary_reasoning: str | None = None
    primary_category: str | None = None
    primary_purpose: str | None = None
    alternatives_json: str | None = None
    risk_assessment: str | None = None
    care_team_note: str | None = None
    care_team_summary: str | None = None
    care_team_suggestions: str | None = None


class FocusPlanOut(CamelModel):
    id: str
    person_id: str
    generated_at: str | None = None
    health_snapshot: str | None = None
    reflection: str | None = None
    insight: str | None = None
    encouragement: str | None = None
    primary_action: str | None = None
    primary_anchor: str | None = None
    primary_reasoning: str | None = None
    primary_category: str | None = None
    primary_purpose: str | None = None
    alternatives_json: str | None = None
    risk_assessment: str | None = None
    care_team_note: str | None = None
    care_team_summary: str | None = None
    care_team_suggestions: str | None = None
    created_at: str
    updated_at: str
    deleted_at: str | None = None
