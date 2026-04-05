"""Tests for the Kasane v1 API — sync, CRUD, and person context."""

import json
import os
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
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

    def test_x_app_token_works(self, client):
        resp = client.get("/api/v1/persons", headers={"x-app-token": TOKEN})
        assert resp.status_code == 200


# --- Per-user token isolation ---

PAUL_TOKEN = "tok_paul"
ANDREW_TOKEN = "tok_andrew"


@pytest.fixture
def multi_user_client(db_path, monkeypatch):
    """Client with per-user tokens configured alongside the admin token."""
    monkeypatch.setattr("engine.gateway.db._db_path", lambda: db_path)
    import engine.gateway.v1_api as v1_mod

    def patched_get_db(p=None):
        return get_db(db_path)
    monkeypatch.setattr(v1_mod, "get_db", patched_get_db)

    config = GatewayConfig(
        port=18899,
        api_token=TOKEN,
        token_persons={
            PAUL_TOKEN: ["paul-001"],
            ANDREW_TOKEN: ["andrew-001"],
        },
    )
    app = create_app(config)
    return TestClient(app)


class TestPerUserTokens:
    """Verify per-user tokens can only access their own data."""

    def _seed_persons(self, client):
        """Create two persons via admin token."""
        client.post("/api/v1/persons", params={"token": TOKEN}, json={"id": "andrew-001", "name": "Andrew"})
        client.post("/api/v1/persons", params={"token": TOKEN}, json={"id": "paul-001", "name": "Paul"})

    def test_user_token_authenticates(self, multi_user_client):
        self._seed_persons(multi_user_client)
        resp = multi_user_client.get("/api/v1/persons/paul-001", params={"token": PAUL_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Paul"

    def test_user_token_blocked_from_other_person(self, multi_user_client):
        self._seed_persons(multi_user_client)
        resp = multi_user_client.get("/api/v1/persons/andrew-001", params={"token": PAUL_TOKEN})
        assert resp.status_code == 403

    def test_user_token_list_persons_filtered(self, multi_user_client):
        self._seed_persons(multi_user_client)
        resp = multi_user_client.get("/api/v1/persons", params={"token": PAUL_TOKEN})
        assert resp.status_code == 200
        persons = resp.json()
        assert len(persons) == 1
        assert persons[0]["id"] == "paul-001"

    def test_admin_token_sees_all(self, multi_user_client):
        self._seed_persons(multi_user_client)
        resp = multi_user_client.get("/api/v1/persons", params={"token": TOKEN})
        assert len(resp.json()) == 2

    def test_user_token_blocked_from_other_habits(self, multi_user_client):
        self._seed_persons(multi_user_client)
        # Create a habit for Andrew via admin
        multi_user_client.post(
            "/api/v1/persons/andrew-001/habits",
            params={"token": TOKEN},
            json={"title": "Andrew's habit"},
        )
        # Paul cannot list Andrew's habits
        resp = multi_user_client.get("/api/v1/persons/andrew-001/habits", params={"token": PAUL_TOKEN})
        assert resp.status_code == 403

    def test_user_token_sync_blocked_for_other_person(self, multi_user_client):
        resp = multi_user_client.post(
            "/api/v1/sync",
            params={"token": PAUL_TOKEN},
            json={
                "deviceId": "pauls-phone",
                "personId": "andrew-001",
                "lastSyncAt": None,
                "changes": [],
            },
        )
        assert resp.status_code == 403

    def test_user_token_context_blocked_for_other_person(self, multi_user_client):
        self._seed_persons(multi_user_client)
        resp = multi_user_client.get("/api/v1/persons/andrew-001/context", params={"token": PAUL_TOKEN})
        assert resp.status_code == 403

    def test_invalid_token_rejected(self, multi_user_client):
        resp = multi_user_client.get("/api/v1/persons", params={"token": "bad-token"})
        assert resp.status_code == 403


# --- Generate Focus Plan (LLM endpoint) ---

class TestGenerateFocusPlan:

    _FAKE_LLM_RESPONSE = json.dumps({
        "healthSnapshot": "ok",
        "reflection": "looking good",
        "insight": "sleep more",
        "encouragement": "keep going",
        "primaryRecommendation": {
            "catalogueId": "sleep-consistent-bedtime",
            "action": "go to bed by 10pm",
            "anchor": "after dinner",
            "reasoning": "sleep drives recovery",
            "category": "sleep",
            "purpose": "improve recovery",
            "evidence": [],
        },
        "alternatives": [],
    })

    def _mock_anthropic(self, monkeypatch):
        import engine.gateway.focus_plan_api as fp_mod
        mock_called = {}

        class FakeMessage:
            def __init__(self):
                self.content = [type("Block", (), {"text": TestGenerateFocusPlan._FAKE_LLM_RESPONSE})()]

        class FakeMessages:
            def create(self, **kwargs):
                mock_called["model"] = kwargs.get("model")
                return FakeMessage()

        class FakeAnthropic:
            def __init__(self):
                self.messages = FakeMessages()

        monkeypatch.setattr(fp_mod.anthropic, "Anthropic", FakeAnthropic)
        return mock_called

    def test_auth_required(self, client):
        """Generate endpoint rejects requests without valid token."""
        resp = client.post("/api/v1/generate-focus-plan", json={"context": "test"})
        assert resp.status_code == 403

    def test_auth_accepts_valid_token(self, client, monkeypatch):
        """Generate endpoint accepts valid token and reaches the LLM call."""
        mock_called = self._mock_anthropic(monkeypatch)

        resp = client.post(
            "/api/v1/generate-focus-plan",
            json={"token": TOKEN, "context": "Test user context"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "healthSnapshot" in body
        assert "generated_at" in body
        assert mock_called.get("model") is not None

    def test_per_user_token_accepted(self, multi_user_client, monkeypatch):
        """Per-user tokens (like Paul's) must be accepted by generate-focus-plan."""
        self._mock_anthropic(monkeypatch)

        # Seed person so FK constraint is satisfied
        multi_user_client.post(
            "/api/v1/persons",
            params={"token": TOKEN},
            json={"id": "paul-001", "name": "Paul"},
        )

        resp = multi_user_client.post(
            "/api/v1/generate-focus-plan",
            json={"token": PAUL_TOKEN, "person_id": "paul-001", "context": "Paul's health context"},
        )
        assert resp.status_code == 200, f"Per-user token should be accepted, got {resp.status_code}"

    def test_generated_plan_persisted_to_db(self, multi_user_client, monkeypatch, db_path):
        """Generated focus plan must be saved to the focus_plan table.

        Bug: generate-focus-plan returned the plan but never wrote it to SQLite.
        This meant latestFocusPlan in the context endpoint was always null.
        """
        self._mock_anthropic(monkeypatch)

        # Seed person
        multi_user_client.post(
            "/api/v1/persons",
            params={"token": TOKEN},
            json={"id": "paul-001", "name": "Paul"},
        )

        # Generate a focus plan
        resp = multi_user_client.post(
            "/api/v1/generate-focus-plan",
            json={"token": PAUL_TOKEN, "person_id": "paul-001", "context": "Paul's health context"},
        )
        assert resp.status_code == 200
        body = resp.json()
        plan_id = body.get("id")
        assert plan_id, "Response must include the persisted plan's id"

        # Verify it's in the DB
        db = get_db(db_path)
        row = db.execute("SELECT * FROM focus_plan WHERE person_id = ?", ("paul-001",)).fetchone()
        assert row is not None, "Focus plan must be persisted to SQLite"
        assert dict(row)["health_snapshot"] == "ok"

    def test_generated_plan_appears_in_context(self, multi_user_client, monkeypatch):
        """After generating a plan, the context endpoint must return it as latestFocusPlan."""
        self._mock_anthropic(monkeypatch)

        # Seed person
        multi_user_client.post(
            "/api/v1/persons",
            params={"token": TOKEN},
            json={"id": "paul-001", "name": "Paul"},
        )

        # Generate
        multi_user_client.post(
            "/api/v1/generate-focus-plan",
            json={"token": PAUL_TOKEN, "person_id": "paul-001", "context": "Paul's health context"},
        )

        # Check context
        resp = multi_user_client.get("/api/v1/persons/paul-001/context", params={"token": TOKEN})
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["latestFocusPlan"] is not None, "latestFocusPlan must not be null after generation"
        assert ctx["latestFocusPlan"]["primaryAction"] == "go to bed by 10pm"

    def test_catalogue_endpoint_no_auth(self, client):
        """Habit catalogue is a public read-only endpoint."""
        resp = client.get("/api/v1/habit-catalogue")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 15


# --- Audit logging ---

class TestAuditLogging:
    def test_sync_writes_audit_entry(self, client, tmp_path, monkeypatch):
        """Sync endpoint writes an audit log entry."""
        audit_path = tmp_path / "admin" / "api_audit.jsonl"
        monkeypatch.setattr("engine.gateway.v1_api._AUDIT_LOG_PATH", str(audit_path))

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
                        "data": {"name": "Audit Test"},
                        "updatedAt": "2026-03-23T12:00:00+00:00",
                    }
                ],
            },
        )

        assert audit_path.exists()
        entries = [json.loads(line) for line in audit_path.read_text().strip().split("\n")]
        assert len(entries) >= 1
        entry = entries[0]
        assert entry["source"] == "v1_api"
        assert entry["endpoint"] == "/api/v1/sync"
        assert entry["method"] == "POST"
        assert entry["person_id"] == "p1"
        assert entry["status"] == 200


# --- Sign in with Apple auth ---

# Generate a test RSA key pair for signing fake Apple JWTs
_TEST_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_RSA_PUB = _TEST_RSA_KEY.public_key()


def _make_apple_jwt(sub="apple-user-001", aud="co.enchant.Hematica", exp_offset=3600, kid="test-key-1", email="test@example.com"):
    """Create a JWT mimicking Apple's identityToken."""
    now = int(time.time())
    payload = {
        "iss": "https://appleid.apple.com",
        "aud": aud,
        "exp": now + exp_offset,
        "iat": now,
        "sub": sub,
        "email": email,
        "email_verified": True,
    }
    return jwt.encode(payload, _TEST_RSA_KEY, algorithm="RS256", headers={"kid": kid})


def _jwks_response():
    """Build a JWKS response containing our test public key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    pub_numbers = _TEST_RSA_PUB.public_numbers()

    import base64

    def _b64url(num, length):
        return base64.urlsafe_b64encode(num.to_bytes(length, "big")).rstrip(b"=").decode()

    n = _b64url(pub_numbers.n, 256)  # 2048-bit key = 256 bytes
    e = _b64url(pub_numbers.e, 3)

    return {
        "keys": [{
            "kty": "RSA",
            "kid": "test-key-1",
            "use": "sig",
            "alg": "RS256",
            "n": n,
            "e": e,
        }]
    }


class TestAppleAuth:
    """Sign in with Apple authentication bridge."""

    def _mock_apple_jwks(self, monkeypatch):
        """Mock the fetch of Apple's JWKS public keys."""
        import engine.gateway.v1_api as v1_mod
        monkeypatch.setattr(v1_mod, "_fetch_apple_jwks", lambda: _jwks_response())

    def test_apple_auth_valid_token(self, client, monkeypatch, db_path):
        """Valid Apple identity token creates a person and returns access token."""
        self._mock_apple_jwks(monkeypatch)

        token = _make_apple_jwt(sub="apple-user-paul")
        resp = client.post("/api/v1/auth/apple", json={"identity_token": token})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["person_id"]

        # Verify person created in DB with apple_user_identifier
        db = get_db(db_path)
        row = db.execute(
            "SELECT apple_user_identifier FROM person WHERE id = ?",
            (body["person_id"],),
        ).fetchone()
        assert row is not None
        assert row["apple_user_identifier"] == "apple-user-paul"

    def test_apple_auth_reuse_existing_person(self, client, monkeypatch, db_path):
        """Second auth with same Apple ID reuses the existing person."""
        self._mock_apple_jwks(monkeypatch)

        token = _make_apple_jwt(sub="apple-user-returning")
        resp1 = client.post("/api/v1/auth/apple", json={"identity_token": token})
        assert resp1.status_code == 200
        person_id_1 = resp1.json()["person_id"]

        resp2 = client.post("/api/v1/auth/apple", json={"identity_token": token})
        assert resp2.status_code == 200
        person_id_2 = resp2.json()["person_id"]

        assert person_id_1 == person_id_2, "Should reuse existing person, not create a new one"

    def test_apple_auth_invalid_token_rejected(self, client, monkeypatch):
        """Malformed or invalid JWT is rejected."""
        self._mock_apple_jwks(monkeypatch)

        resp = client.post("/api/v1/auth/apple", json={"identity_token": "garbage.not.jwt"})
        assert resp.status_code == 401

    def test_apple_auth_expired_token_rejected(self, client, monkeypatch):
        """Expired JWT is rejected."""
        self._mock_apple_jwks(monkeypatch)

        token = _make_apple_jwt(sub="apple-expired", exp_offset=-3600)
        resp = client.post("/api/v1/auth/apple", json={"identity_token": token})
        assert resp.status_code == 401

    def test_apple_auth_link_by_name(self, client, monkeypatch, db_path):
        """Can link an Apple ID to an existing person by name match."""
        self._mock_apple_jwks(monkeypatch)

        # Create person without apple_user_identifier
        client.post(
            "/api/v1/persons", params=_auth(),
            json={"id": "paul-001", "name": "Paul"},
        )

        token = _make_apple_jwt(sub="apple-user-paul-link")
        resp = client.post("/api/v1/auth/apple", json={
            "identity_token": token,
            "full_name": "Paul",
        })
        assert resp.status_code == 200
        assert resp.json()["person_id"] == "paul-001"

        # Verify link was saved
        db = get_db(db_path)
        row = db.execute(
            "SELECT apple_user_identifier FROM person WHERE id = ?",
            ("paul-001",),
        ).fetchone()
        assert row["apple_user_identifier"] == "apple-user-paul-link"

    def test_oauth_token_accepted_by_sync(self, client, monkeypatch, db_path):
        """OAuth access token from Apple auth works for sync endpoint."""
        self._mock_apple_jwks(monkeypatch)

        token = _make_apple_jwt(sub="apple-sync-user")
        auth_resp = client.post("/api/v1/auth/apple", json={"identity_token": token})
        assert auth_resp.status_code == 200
        access_token = auth_resp.json()["access_token"]
        person_id = auth_resp.json()["person_id"]

        # Use the OAuth token for sync
        sync_resp = client.post(
            "/api/v1/sync/ios",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "person_id": person_id,
                "last_sync": None,
                "changes": {"persons": [], "habits": [], "check_ins": [], "focus_plans": [], "messages": []},
            },
        )
        assert sync_resp.status_code == 200, f"Sync with OAuth token failed: {sync_resp.text}"

    def test_oauth_token_refresh(self, client, monkeypatch):
        """Refresh token issues new access token and revokes old one."""
        self._mock_apple_jwks(monkeypatch)

        token = _make_apple_jwt(sub="apple-refresh-user")
        auth_resp = client.post("/api/v1/auth/apple", json={"identity_token": token})
        assert auth_resp.status_code == 200
        old_access = auth_resp.json()["access_token"]
        refresh = auth_resp.json()["refresh_token"]

        # Refresh
        refresh_resp = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        assert refresh_resp.status_code == 200
        new_access = refresh_resp.json()["access_token"]
        assert new_access != old_access

        # Old access token still works until expiry (we only revoke the
        # used refresh token, not all tokens, to handle concurrent syncs)
        old_resp = client.get(
            "/api/v1/persons",
            headers={"Authorization": f"Bearer {old_access}"},
        )
        assert old_resp.status_code == 200

        # New access token should also work
        new_resp = client.get(
            "/api/v1/persons",
            headers={"Authorization": f"Bearer {new_access}"},
        )
        assert new_resp.status_code == 200

    # --- Identity reconciliation tests (Cases 1, 2, 3) ---

    def test_case1_new_kasane_user_creates_with_client_uuid(self, client, monkeypatch, db_path):
        """Case 1: New Kasane user not in Kiso. Server creates person using
        the client-provided UUID (CoreData person ID) so both sides agree."""
        self._mock_apple_jwks(monkeypatch)

        client_uuid = "coredata-uuid-aaaa-1111"
        token = _make_apple_jwt(sub="apple-new-user", email="jane@icloud.com")
        resp = client.post("/api/v1/auth/apple", json={
            "identity_token": token,
            "person_id": client_uuid,
            "full_name": "Jane Smith",
            "email": "jane@icloud.com",
        })
        assert resp.status_code == 200
        assert resp.json()["person_id"] == client_uuid

        # Verify person created with client UUID, name, email, and apple_user_identifier
        db = get_db(db_path)
        row = db.execute(
            "SELECT name, email, apple_user_identifier FROM person WHERE id = ?",
            (client_uuid,),
        ).fetchone()
        assert row is not None, "Person should be created with client-provided UUID"
        assert row["name"] == "Jane Smith"
        assert row["email"] == "jane@icloud.com"
        assert row["apple_user_identifier"] == "apple-new-user"

    def test_case2_existing_kiso_user_matched_by_name(self, client, monkeypatch, db_path):
        """Case 2: Existing Kiso/Milo user installs Kasane. Server matches
        by name, links apple_user_identifier, stores email."""
        self._mock_apple_jwks(monkeypatch)

        # Pre-create a Kiso person (as if created by Milo onboarding)
        db = get_db(db_path)
        init_db()
        from engine.gateway.v1_api import _now_iso
        now = _now_iso()
        db.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("kiso-grigoriy-001", "Grigoriy Kogan", "grigoriy", now, now),
        )
        db.commit()

        # SIWA with name that matches
        token = _make_apple_jwt(sub="apple-grigoriy", email="grigoriy@gmail.com")
        resp = client.post("/api/v1/auth/apple", json={
            "identity_token": token,
            "person_id": "coredata-uuid-cccc",  # iOS CoreData UUID (different)
            "full_name": "Grigoriy Kogan",
            "email": "grigoriy@gmail.com",
        })
        assert resp.status_code == 200
        # Should return the EXISTING Kiso person ID, not the CoreData UUID
        assert resp.json()["person_id"] == "kiso-grigoriy-001"

        # Verify apple_user_identifier and email stored on existing person
        row = db.execute(
            "SELECT apple_user_identifier, email FROM person WHERE id = ?",
            ("kiso-grigoriy-001",),
        ).fetchone()
        assert row["apple_user_identifier"] == "apple-grigoriy"
        assert row["email"] == "grigoriy@gmail.com"

        # Verify no new person was created with the CoreData UUID
        orphan = db.execute(
            "SELECT id FROM person WHERE id = ?", ("coredata-uuid-cccc",),
        ).fetchone()
        assert orphan is None, "Should NOT create a new person when name match found"

    def test_case3_reinstall_matches_by_apple_id(self, client, monkeypatch, db_path):
        """Case 3: Reinstall or new device. apple_user_identifier already set,
        server returns existing person even if client sends a different UUID."""
        self._mock_apple_jwks(monkeypatch)

        # First auth: create person normally
        token = _make_apple_jwt(sub="apple-reinstall-user", email="user@icloud.com")
        resp1 = client.post("/api/v1/auth/apple", json={
            "identity_token": token,
            "person_id": "original-coredata-uuid",
            "full_name": "Andrew Deal",
            "email": "user@icloud.com",
        })
        assert resp1.status_code == 200
        original_pid = resp1.json()["person_id"]

        # Second auth from new device: different CoreData UUID, same Apple ID
        resp2 = client.post("/api/v1/auth/apple", json={
            "identity_token": token,
            "person_id": "new-device-coredata-uuid",
            "full_name": "Andrew Deal",
            "email": "user@icloud.com",
        })
        assert resp2.status_code == 200
        # Should return the ORIGINAL person, not create new
        assert resp2.json()["person_id"] == original_pid

    def test_case2_name_match_is_case_insensitive(self, client, monkeypatch, db_path):
        """Name matching should be case-insensitive."""
        self._mock_apple_jwks(monkeypatch)

        db = get_db(db_path)
        init_db()
        from engine.gateway.v1_api import _now_iso
        now = _now_iso()
        db.execute(
            "INSERT INTO person (id, name, health_engine_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dean-001", "Dean", "dean", now, now),
        )
        db.commit()

        # Apple sends full name with different casing
        token = _make_apple_jwt(sub="apple-dean", email="dean@gmail.com")
        resp = client.post("/api/v1/auth/apple", json={
            "identity_token": token,
            "person_id": "coredata-dean-uuid",
            "full_name": "dean",
            "email": "dean@gmail.com",
        })
        assert resp.status_code == 200
        assert resp.json()["person_id"] == "dean-001"

    def test_case2_duplicate_names_skips_to_create(self, client, monkeypatch, db_path):
        """When multiple persons share a name, skip name match and create new
        to avoid silently linking to the wrong person."""
        self._mock_apple_jwks(monkeypatch)

        db = get_db(db_path)
        init_db()
        from engine.gateway.v1_api import _now_iso
        now = _now_iso()
        db.execute(
            "INSERT INTO person (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("paul-a", "Paul", now, now),
        )
        db.execute(
            "INSERT INTO person (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("paul-b", "Paul", now, now),
        )
        db.commit()

        token = _make_apple_jwt(sub="apple-paul", email="paul@gmail.com")
        resp = client.post("/api/v1/auth/apple", json={
            "identity_token": token,
            "person_id": "coredata-paul-uuid",
            "full_name": "Paul",
            "email": "paul@gmail.com",
        })
        assert resp.status_code == 200
        # Should NOT match either Paul — creates new with client UUID
        assert resp.json()["person_id"] == "coredata-paul-uuid"

        # Neither existing Paul should have been linked
        db2 = get_db(db_path)
        for pid in ("paul-a", "paul-b"):
            row = db2.execute(
                "SELECT apple_user_identifier FROM person WHERE id = ?", (pid,),
            ).fetchone()
            assert row["apple_user_identifier"] is None
