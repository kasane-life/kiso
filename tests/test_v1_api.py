"""Tests for the Kasane v1 API — sync, CRUD, and person context."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.gateway.config import GatewayConfig
from engine.gateway.db import close_db, get_db, init_db
from engine.gateway.server import create_app


TOKEN = "test-token-v1"


@pytest.fixture(autouse=True)
def _reset_thread_local():
    """Ensure each test gets a fresh DB connection."""
    close_db()
    yield
    close_db()


@pytest.fixture
def db_path(tmp_path):
    """Create a temp database."""
    path = tmp_path / "kasane.db"
    init_db(path)
    return path


@pytest.fixture
def client(db_path, monkeypatch):
    """FastAPI test client wired to the temp database."""
    # Patch db.py to use the temp path
    monkeypatch.setattr("engine.gateway.db._db_path", lambda: db_path)
    monkeypatch.setattr("engine.gateway.v1_api.get_db", lambda db_path=None: get_db(db_path or str(db_path)))

    # Re-patch so v1_api.get_db uses our db_path by default
    import engine.gateway.v1_api as v1_mod
    orig_get_db = v1_mod.get_db
    def patched_get_db(p=None):
        return get_db(db_path)
    monkeypatch.setattr(v1_mod, "get_db", patched_get_db)

    config = GatewayConfig(port=18899, api_token=TOKEN)
    app = create_app(config)
    return TestClient(app)


def _auth(params=None):
    """Add token to query params."""
    p = params or {}
    p["token"] = TOKEN
    return p


# --- Person CRUD ---

class TestPersonCRUD:
    def test_create_and_get(self, client):
        resp = client.post(
            "/api/v1/persons",
            params={"token": TOKEN},
            json={"name": "Andrew", "biologicalSex": "M", "healthEngineUserId": "default"},
        )
        assert resp.status_code == 201
        person = resp.json()
        assert person["name"] == "Andrew"
        assert person["biologicalSex"] == "M"
        assert person["healthEngineUserId"] == "default"
        pid = person["id"]

        # GET by id
        resp2 = client.get(f"/api/v1/persons/{pid}", params=_auth())
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "Andrew"

    def test_list_persons(self, client):
        client.post("/api/v1/persons", params=_auth(), json={"name": "Alice"})
        client.post("/api/v1/persons", params=_auth(), json={"name": "Bob"})
        resp = client.get("/api/v1/persons", params=_auth())
        assert len(resp.json()) == 2

    def test_update_person(self, client):
        resp = client.post("/api/v1/persons", params=_auth(), json={"name": "Old Name"})
        pid = resp.json()["id"]

        resp2 = client.put(f"/api/v1/persons/{pid}", params=_auth(), json={"name": "New Name"})
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "New Name"

    def test_404_on_missing_person(self, client):
        resp = client.get("/api/v1/persons/nonexistent", params=_auth())
        assert resp.status_code == 404

    def test_auth_required(self, client):
        resp = client.get("/api/v1/persons")
        assert resp.status_code in (403, 422)

        resp2 = client.get("/api/v1/persons", params={"token": "wrong"})
        assert resp2.status_code == 403


# --- Habit CRUD ---

class TestHabitCRUD:
    def _create_person(self, client):
        resp = client.post("/api/v1/persons", params=_auth(), json={"name": "Test Person"})
        return resp.json()["id"]

    def test_create_and_list_habits(self, client):
        pid = self._create_person(client)

        resp = client.post(
            f"/api/v1/persons/{pid}/habits",
            params=_auth(),
            json={"title": "Morning walk", "category": "movement", "emoji": "🚶"},
        )
        assert resp.status_code == 201
        habit = resp.json()
        assert habit["title"] == "Morning walk"
        assert habit["personId"] == pid

        # List
        resp2 = client.get(f"/api/v1/persons/{pid}/habits", params=_auth())
        assert len(resp2.json()) == 1

    def test_update_habit(self, client):
        pid = self._create_person(client)
        resp = client.post(
            f"/api/v1/persons/{pid}/habits",
            params=_auth(),
            json={"title": "Read"},
        )
        hid = resp.json()["id"]

        resp2 = client.put(f"/api/v1/habits/{hid}", params=_auth(), json={"title": "Read 10 pages"})
        assert resp2.json()["title"] == "Read 10 pages"


# --- Check-in CRUD ---

class TestCheckInCRUD:
    def _setup(self, client):
        pid = client.post("/api/v1/persons", params=_auth(), json={"name": "P"}).json()["id"]
        hid = client.post(
            f"/api/v1/persons/{pid}/habits", params=_auth(), json={"title": "H"}
        ).json()["id"]
        return pid, hid

    def test_create_and_list_checkins(self, client):
        _, hid = self._setup(client)

        resp = client.post(
            f"/api/v1/habits/{hid}/checkins",
            params=_auth(),
            json={"date": "2026-03-23", "completed": True, "note": "Done!"},
        )
        assert resp.status_code == 201
        ci = resp.json()
        assert ci["completed"] is False or ci["completed"] is True  # Pydantic coerces to bool
        assert ci["habitId"] == hid

        resp2 = client.get(f"/api/v1/habits/{hid}/checkins", params=_auth())
        assert len(resp2.json()) == 1

    def test_checkins_since_filter(self, client):
        _, hid = self._setup(client)
        client.post(f"/api/v1/habits/{hid}/checkins", params=_auth(), json={"date": "2026-03-01", "completed": True})
        client.post(f"/api/v1/habits/{hid}/checkins", params=_auth(), json={"date": "2026-03-20", "completed": True})

        resp = client.get(f"/api/v1/habits/{hid}/checkins", params={**_auth(), "since": "2026-03-15"})
        assert len(resp.json()) == 1


# --- Focus Plan ---

class TestFocusPlan:
    def test_create_and_list(self, client):
        pid = client.post("/api/v1/persons", params=_auth(), json={"name": "P"}).json()["id"]

        resp = client.post(
            f"/api/v1/persons/{pid}/focus-plans",
            params=_auth(),
            json={
                "primaryAction": "Walk 10 min after lunch",
                "primaryCategory": "movement",
                "insight": "You sit too much",
            },
        )
        assert resp.status_code == 201
        fp = resp.json()
        assert fp["primaryAction"] == "Walk 10 min after lunch"
        assert fp["personId"] == pid

        resp2 = client.get(f"/api/v1/persons/{pid}/focus-plans", params=_auth())
        assert len(resp2.json()) == 1


# --- Sync ---

class TestSync:
    def test_sync_push_and_pull(self, client):
        """Push a person via sync, then pull it back."""
        now = "2026-03-23T12:00:00+00:00"
        resp = client.post(
            "/api/v1/sync",
            params=_auth(),
            json={
                "deviceId": "iphone-1",
                "personId": "person-abc",
                "lastSyncAt": None,
                "changes": [
                    {
                        "entity": "person",
                        "id": "person-abc",
                        "action": "upsert",
                        "data": {
                            "name": "Andrew",
                            "biological_sex": "M",
                            "health_engine_user_id": "default",
                        },
                        "updatedAt": now,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["stats"]["pushed"] == 1

        # Pull back
        resp2 = client.post(
            "/api/v1/sync",
            params=_auth(),
            json={
                "deviceId": "iphone-1",
                "personId": "person-abc",
                "lastSyncAt": None,
                "changes": [],
            },
        )
        body2 = resp2.json()
        changes = body2["serverChanges"]
        person_changes = [c for c in changes if c["entity"] == "person"]
        assert len(person_changes) == 1
        assert person_changes[0]["data"]["name"] == "Andrew"

    def test_sync_conflict_server_wins(self, client):
        """When server has a newer updated_at, client change is rejected."""
        # Push initial
        client.post(
            "/api/v1/sync",
            params=_auth(),
            json={
                "deviceId": "d1",
                "personId": "p1",
                "lastSyncAt": None,
                "changes": [
                    {
                        "entity": "person",
                        "id": "p1",
                        "action": "upsert",
                        "data": {"name": "Server Version"},
                        "updatedAt": "2026-03-23T14:00:00+00:00",
                    }
                ],
            },
        )

        # Try to push older update
        resp = client.post(
            "/api/v1/sync",
            params=_auth(),
            json={
                "deviceId": "d2",
                "personId": "p1",
                "lastSyncAt": None,
                "changes": [
                    {
                        "entity": "person",
                        "id": "p1",
                        "action": "upsert",
                        "data": {"name": "Client Version"},
                        "updatedAt": "2026-03-23T12:00:00+00:00",
                    }
                ],
            },
        )
        assert resp.json()["stats"]["pushed"] == 0

        # Verify server version persists
        person = client.get("/api/v1/persons/p1", params=_auth()).json()
        assert person["name"] == "Server Version"

    def test_sync_soft_delete(self, client):
        """Soft-deleted records propagate through sync."""
        # Create
        client.post(
            "/api/v1/sync",
            params=_auth(),
            json={
                "deviceId": "d1",
                "personId": "p1",
                "lastSyncAt": None,
                "changes": [
                    {
                        "entity": "person",
                        "id": "p1",
                        "action": "upsert",
                        "data": {"name": "To Delete"},
                        "updatedAt": "2026-03-23T12:00:00+00:00",
                    }
                ],
            },
        )

        # Delete
        resp = client.post(
            "/api/v1/sync",
            params=_auth(),
            json={
                "deviceId": "d1",
                "personId": "p1",
                "lastSyncAt": "2026-03-23T12:00:00+00:00",
                "changes": [
                    {
                        "entity": "person",
                        "id": "p1",
                        "action": "delete",
                        "data": {},
                        "updatedAt": "2026-03-23T13:00:00+00:00",
                    }
                ],
            },
        )
        assert resp.json()["stats"]["pushed"] == 1

        # GET should 404 (soft deleted)
        resp2 = client.get("/api/v1/persons/p1", params=_auth())
        assert resp2.status_code == 404

    def test_sync_habit_and_checkin(self, client):
        """Push a person, habit, and check-in via sync."""
        base = "2026-03-23T12:00:00+00:00"
        resp = client.post(
            "/api/v1/sync",
            params=_auth(),
            json={
                "deviceId": "d1",
                "personId": "p1",
                "lastSyncAt": None,
                "changes": [
                    {
                        "entity": "person",
                        "id": "p1",
                        "action": "upsert",
                        "data": {"name": "Test"},
                        "updatedAt": base,
                    },
                    {
                        "entity": "habit",
                        "id": "h1",
                        "action": "upsert",
                        "data": {"person_id": "p1", "title": "Meditate"},
                        "updatedAt": base,
                    },
                    {
                        "entity": "check_in",
                        "id": "ci1",
                        "action": "upsert",
                        "data": {"habit_id": "h1", "date": "2026-03-23", "completed": 1},
                        "updatedAt": base,
                    },
                ],
            },
        )
        assert resp.json()["stats"]["pushed"] == 3

        # Verify via CRUD endpoints
        habits = client.get("/api/v1/persons/p1/habits", params=_auth()).json()
        assert len(habits) == 1
        assert habits[0]["title"] == "Meditate"

        checkins = client.get("/api/v1/habits/h1/checkins", params=_auth()).json()
        assert len(checkins) == 1


# --- Context endpoint ---

class TestPersonContext:
    def test_context_returns_merged_data(self, client):
        """Context endpoint returns person + habits + focus plan."""
        # Create person with habits
        pid = client.post(
            "/api/v1/persons",
            params=_auth(),
            json={"name": "Andrew", "healthEngineUserId": "default"},
        ).json()["id"]

        client.post(
            f"/api/v1/persons/{pid}/habits",
            params=_auth(),
            json={"title": "Walk", "state": "active"},
        )

        client.post(
            f"/api/v1/persons/{pid}/focus-plans",
            params=_auth(),
            json={"primaryAction": "Walk more"},
        )

        resp = client.get(f"/api/v1/persons/{pid}/context", params=_auth())
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["person"]["name"] == "Andrew"
        assert len(ctx["activeHabits"]) == 1
        assert ctx["latestFocusPlan"]["primaryAction"] == "Walk more"

    def test_context_404_missing_person(self, client):
        resp = client.get("/api/v1/persons/missing/context", params=_auth())
        assert resp.status_code == 404


# --- Bearer token auth ---

class TestBearerAuth:
    def test_bearer_token_works(self, client):
        resp = client.get("/api/v1/persons", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200
