"""Kasane v1 REST API — shared data layer for iOS app + Milo agent.

Endpoints:
    POST /api/v1/sync                      — bidirectional sync
    GET  /api/v1/persons                    — list persons
    GET  /api/v1/persons/:id               — get person
    POST /api/v1/persons                    — create person
    PUT  /api/v1/persons/:id               — update person
    GET  /api/v1/persons/:id/habits        — list habits
    POST /api/v1/persons/:id/habits        — create habit
    PUT  /api/v1/habits/:id               — update habit
    GET  /api/v1/habits/:id/checkins      — list check-ins
    POST /api/v1/habits/:id/checkins      — create check-in
    GET  /api/v1/persons/:id/focus-plans  — list focus plans
    POST /api/v1/persons/:id/focus-plans  — create focus plan
    GET  /api/v1/persons/:id/context      — Milo's unified read

Auth: same api_token as existing /api/ endpoints.
"""

import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("kiso.v1_api")

# --- Apple Sign In constants ---

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"
APPLE_AUDIENCE = ["co.enchant.Hematica", "com.adeal.Habica.Kasane"]
_apple_jwks_cache: dict | None = None
_apple_jwks_cache_time: float = 0
_JWKS_CACHE_TTL = 3600  # 1 hour

ACCESS_TOKEN_TTL = 3600 * 24  # 24 hours
REFRESH_TOKEN_TTL = 3600 * 24 * 90  # 90 days


def _fetch_apple_jwks() -> dict:
    """Fetch Apple's JWKS public keys. Cached for 1 hour."""
    global _apple_jwks_cache, _apple_jwks_cache_time
    now = time.time()
    if _apple_jwks_cache and (now - _apple_jwks_cache_time) < _JWKS_CACHE_TTL:
        return _apple_jwks_cache
    import urllib.request
    with urllib.request.urlopen(APPLE_JWKS_URL, timeout=10) as resp:
        _apple_jwks_cache = json.loads(resp.read())
        _apple_jwks_cache_time = now
        return _apple_jwks_cache


def _verify_apple_identity_token(identity_token: str) -> dict:
    """Verify an Apple identity token JWT. Returns decoded claims.

    Raises HTTPException(401) on any verification failure.
    """
    try:
        jwks = _fetch_apple_jwks()
        # Get the signing key from JWKS
        header = jwt.get_unverified_header(identity_token)
        kid = header.get("kid")
        key_data = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                key_data = key
                break
        if not key_data:
            raise HTTPException(401, "Apple signing key not found")

        # Build the public key from JWK
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        # Verify and decode
        claims = jwt.decode(
            identity_token,
            public_key,
            algorithms=["RS256"],
            audience=APPLE_AUDIENCE,
            issuer=APPLE_ISSUER,
        )
        return claims
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Apple identity token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid Apple identity token: {e}")
    except Exception as e:
        raise HTTPException(401, f"Apple token verification failed: {e}")

from .db import (
    ENTITY_TABLES,
    TABLE_COLUMNS,
    get_db,
    init_db,
)
from .v1_models import (
    CheckInCreate,
    CheckInOut,
    FocusPlanCreate,
    FocusPlanOut,
    HabitCreate,
    HabitOut,
    HabitUpdate,
    PersonCreate,
    PersonOut,
    PersonUpdate,
    SyncChange,
    SyncRequest,
    SyncResponse,
    IosSyncRequest,
    IosSyncResponse,
    IosSyncChanges,
    IosSyncPersonDTO,
    IosSyncHabitDTO,
    IosSyncCheckInDTO,
    IosSyncFocusPlanDTO,
    IosSyncMessageDTO,
)

router = APIRouter(prefix="/api/v1")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# --- Audit logging ---

_AUDIT_LOG_PATH = os.path.join("data", "admin", "api_audit.jsonl")


def _audit_v1(endpoint: str, method: str, person_id: str | None, status: int,
              elapsed_ms: int, detail: str | None = None):
    """Append one v1 audit entry to the shared audit log."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "v1_api",
        "endpoint": endpoint,
        "method": method,
        "person_id": person_id,
        "status": status,
        "ms": elapsed_ms,
    }
    if detail:
        entry["detail"] = detail
    try:
        os.makedirs(os.path.dirname(_AUDIT_LOG_PATH), exist_ok=True)
        with open(_AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        logger.warning("Failed to write v1 audit log", exc_info=True)


# --- Auth dependency ---

def _get_config(request: Request):
    return request.app.state.config


def _verify_token(request: Request, token: str = Query(None)):
    """Verify API token from query param, Authorization header, or OAuth token table.

    Accepts: admin api_token, per-user token_persons keys, or OAuth access tokens.
    """
    config = _get_config(request)

    effective = token
    if not effective:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            effective = auth[7:]
    if not effective:
        effective = request.headers.get("x-app-token")

    if not effective:
        raise HTTPException(403, "Invalid token")

    # Check main admin token
    if config.api_token and effective == config.api_token:
        return effective

    # Check per-user tokens
    if effective in config.token_persons:
        return effective

    # Check OAuth access tokens
    db = get_db()
    init_db()
    row = db.execute(
        "SELECT person_id, expires_at, revoked FROM oauth_token "
        "WHERE token = ? AND token_type = 'access'",
        (effective,),
    ).fetchone()
    if row and not row["revoked"] and (not row["expires_at"] or row["expires_at"] > time.time()):
        # Store resolved person_id on request state for _check_person_access
        request.state.oauth_person_id = row["person_id"]
        return effective

    raise HTTPException(403, "Invalid token")


def _check_person_access(request: Request, token: str, person_id: str):
    """Verify the token is allowed to access this person's data.

    Admin token (api_token) can access everything.
    Per-user tokens can only access their mapped person IDs.
    OAuth tokens (from SIWA) are trusted — the token itself is the authorization.
    The iOS app's local CoreData person UUID may differ from the server's
    person UUID, so we can't do strict matching here.
    """
    config = _get_config(request)
    # Admin token: unrestricted
    if config.api_token and token == config.api_token:
        return
    # Per-user token: check mapping
    allowed = config.token_persons.get(token, [])
    if person_id in allowed:
        return
    # OAuth token: trusted (single-user token, already authenticated via SIWA)
    oauth_pid = getattr(request.state, "oauth_person_id", None)
    if oauth_pid:
        return
    raise HTTPException(403, "Access denied for this person")


# --- Helpers ---

def _row_to_dict(row) -> dict:
    """Convert sqlite3.Row to plain dict (snake_case, internal use only)."""
    if row is None:
        return {}
    return dict(row)


# Map entity/table names to their Pydantic output models for camelCase serialization.
_MODEL_MAP = {
    "person": PersonOut,
    "habit": HabitOut,
    "check_in": CheckInOut,
    "focus_plan": FocusPlanOut,
}


def _serialize(row, model_cls) -> dict:
    """Convert sqlite3.Row to camelCase dict via Pydantic model."""
    if row is None:
        return {}
    return model_cls(**dict(row)).model_dump(by_alias=True)


def _serialize_list(rows, model_cls) -> list[dict]:
    """Convert a list of sqlite3.Rows to camelCase dicts."""
    return [_serialize(r, model_cls) for r in rows]


def _get_or_404(table: str, row_id: str, db=None) -> dict:
    """Fetch a row by id or raise 404. Returns camelCase dict."""
    if db is None:
        db = get_db()
    row = db.execute(f"SELECT * FROM {table} WHERE id = ? AND deleted_at IS NULL", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"{table} {row_id} not found")
    model_cls = _MODEL_MAP.get(table)
    if model_cls:
        return _serialize(row, model_cls)
    return _row_to_dict(row)


# --- Health snapshot endpoint ---

from pydantic import BaseModel


class HealthSnapshotRequest(BaseModel):
    user_id: str
    metrics: dict
    timestamp: str | None = None


@router.post("/health-snapshot")
def ingest_health_snapshot(body: HealthSnapshotRequest, request: Request, _token: str = Depends(_verify_token)):
    """Ingest Apple Health metrics via per-user token auth.

    Accepts the same payload as the legacy /api/ingest_health_snapshot
    but uses v1 token_persons auth.
    """
    t0 = time.monotonic()
    db = get_db()
    init_db()

    # Resolve user_id to person_id for access check
    row = db.execute(
        "SELECT id FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
        (body.user_id,),
    ).fetchone()
    if row:
        _check_person_access(request, _token, row["id"])

    from mcp_server.tools import _ingest_health_snapshot
    result = _ingest_health_snapshot(body.user_id, body.metrics, body.timestamp)

    elapsed = int((time.monotonic() - t0) * 1000)
    _audit_v1("/api/v1/health-snapshot", "POST", row["id"] if row else None, 200, elapsed,
              f"user_id={body.user_id} metrics={len(body.metrics)}")

    return result


# --- Sync endpoint ---

@router.post("/sync")
def sync(body: SyncRequest, request: Request, _token: str = Depends(_verify_token)):
    """Bidirectional sync: client pushes changes, pulls server changes.

    Conflict resolution: last-write-wins by updated_at timestamp.
    """
    _check_person_access(request, _token, body.person_id)
    t0 = time.monotonic()
    db = get_db()
    init_db()
    now = _now_iso()
    pushed = 0

    for change in body.changes:
        table = ENTITY_TABLES.get(change.entity)
        if not table:
            continue

        if change.action == "delete":
            # Soft delete: only if server's updated_at is older
            existing = db.execute(
                f"SELECT updated_at FROM {table} WHERE id = ?", (change.id,)
            ).fetchone()
            if existing and existing["updated_at"] > change.updated_at:
                continue  # Server wins
            if existing:
                db.execute(
                    f"UPDATE {table} SET deleted_at = ?, updated_at = ? WHERE id = ?",
                    (now, change.updated_at, change.id),
                )
                pushed += 1
        elif change.action == "upsert":
            existing = db.execute(
                f"SELECT updated_at FROM {table} WHERE id = ?", (change.id,)
            ).fetchone()

            if existing and existing["updated_at"] > change.updated_at:
                continue  # Server wins

            columns = TABLE_COLUMNS.get(table, [])
            data = change.data

            if existing:
                # Update
                sets = []
                vals = []
                for col in columns:
                    if col in data:
                        sets.append(f"{col} = ?")
                        vals.append(data[col])
                sets.append("updated_at = ?")
                vals.append(change.updated_at)
                vals.append(change.id)
                if sets:
                    db.execute(
                        f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?",
                        vals,
                    )
                    pushed += 1
            else:
                # Insert
                col_names = ["id", "created_at", "updated_at"]
                col_vals = [change.id, data.get("created_at", now), change.updated_at]
                for col in columns:
                    if col in data:
                        col_names.append(col)
                        col_vals.append(data[col])
                placeholders = ", ".join(["?"] * len(col_names))
                db.execute(
                    f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})",
                    col_vals,
                )
                pushed += 1

    db.commit()

    # Pull server changes since last_sync_at
    server_changes = []
    for entity, table in ENTITY_TABLES.items():
        if body.last_sync_at:
            rows = db.execute(
                f"SELECT * FROM {table} WHERE updated_at > ? "
                f"AND (id IN (SELECT id FROM {table} WHERE "
                f"  EXISTS (SELECT 1 FROM person WHERE person.id = ? AND "
                f"    ('{table}' = 'person' OR "
                f"     ('{table}' != 'person' AND 1=1))))"
                f")",
                (body.last_sync_at, body.person_id),
            ).fetchall()
        else:
            # First sync: pull everything for this person
            if table == "person":
                rows = db.execute(
                    f"SELECT * FROM {table} WHERE id = ?", (body.person_id,)
                ).fetchall()
            elif "person_id" in TABLE_COLUMNS.get(table, []):
                rows = db.execute(
                    f"SELECT * FROM {table} WHERE person_id = ?", (body.person_id,)
                ).fetchall()
            elif table == "check_in":
                # Check-ins link through habits
                rows = db.execute(
                    f"SELECT c.* FROM check_in c "
                    f"JOIN habit h ON c.habit_id = h.id "
                    f"WHERE h.person_id = ?",
                    (body.person_id,),
                ).fetchall()
            else:
                rows = []

        for row in rows:
            raw = _row_to_dict(row)
            model_cls = _MODEL_MAP.get(entity)
            d = _serialize(row, model_cls) if model_cls else raw
            server_changes.append({
                "entity": entity,
                "id": raw["id"],
                "action": "delete" if raw.get("deleted_at") else "upsert",
                "data": d,
                "updated_at": raw["updated_at"],
            })

    # Update sync cursor
    db.execute(
        "INSERT INTO sync_cursor (device_id, person_id, last_sync_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(device_id, person_id) DO UPDATE SET last_sync_at = ?",
        (body.device_id, body.person_id, now, now),
    )
    db.commit()

    elapsed = int((time.monotonic() - t0) * 1000)
    _audit_v1("/api/v1/sync", "POST", body.person_id, 200, elapsed,
              f"pushed={pushed} pulled={len(server_changes)}")
    return SyncResponse(
        server_changes=server_changes,
        sync_at=now,
        stats={"pushed": pushed, "pulled": len(server_changes)},
    )


# --- iOS Sync (nested format matching SyncService.swift) ---

_IOS_ENTITY_MAP = {
    "persons": ("person", lambda dto: dto.model_dump(exclude_none=True, exclude={"id"})),
    "habits": ("habit", lambda dto: dto.model_dump(exclude_none=True, exclude={"id"})),
    "check_ins": ("check_in", lambda dto: dto.model_dump(exclude_none=True, exclude={"id"})),
    "focus_plans": ("focus_plan", lambda dto: dto.model_dump(exclude_none=True, exclude={"id"})),
    "messages": ("check_in_message", lambda dto: dto.model_dump(exclude_none=True, exclude={"id"})),
}

_IOS_DTO_CLASSES = {
    "person": IosSyncPersonDTO,
    "habit": IosSyncHabitDTO,
    "check_in": IosSyncCheckInDTO,
    "focus_plan": IosSyncFocusPlanDTO,
    "check_in_message": IosSyncMessageDTO,
}

_IOS_RESPONSE_KEYS = {
    "person": "persons",
    "habit": "habits",
    "check_in": "check_ins",
    "focus_plan": "focus_plans",
    "check_in_message": "messages",
}


@router.post("/sync/ios")
def sync_ios(body: IosSyncRequest, request: Request, _token: str = Depends(_verify_token)):
    """iOS-compatible sync endpoint. Accepts nested SyncChanges format
    matching the iOS SyncService.swift DTOs (snake_case JSON)."""
    _check_person_access(request, _token, body.person_id)
    t0 = time.monotonic()
    db = get_db()
    init_db()
    now = _now_iso()
    pushed = 0

    # Push: upsert each DTO into the appropriate table
    for field_name, (table, to_data) in _IOS_ENTITY_MAP.items():
        dtos = getattr(body.changes, field_name, [])
        columns = TABLE_COLUMNS.get(table, [])

        for dto in dtos:
            data = to_data(dto)
            row_id = dto.id

            # Filter to only columns that exist in the DB
            db_data = {k: v for k, v in data.items() if k in columns}

            existing = db.execute(
                f"SELECT id FROM {table} WHERE id = ?", (row_id,)
            ).fetchone()

            if existing:
                if db_data:
                    sets = [f"{k} = ?" for k in db_data]
                    sets.append("updated_at = ?")
                    vals = list(db_data.values()) + [now, row_id]
                    db.execute(
                        f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?",
                        vals,
                    )
                    pushed += 1
            else:
                col_names = ["id", "created_at", "updated_at"]
                col_vals = [row_id, db_data.pop("created_at", now), now]
                for k, v in db_data.items():
                    col_names.append(k)
                    col_vals.append(v)
                placeholders = ", ".join(["?"] * len(col_names))
                db.execute(
                    f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})",
                    col_vals,
                )
                pushed += 1

    db.commit()

    # Pull: get server changes since last_sync for this person
    result_changes = IosSyncChanges()

    for entity, table in ENTITY_TABLES.items():
        response_key = _IOS_RESPONSE_KEYS.get(table)
        dto_class = _IOS_DTO_CLASSES.get(table)
        if not response_key or not dto_class:
            continue

        # person table uses id, check_in uses habit_id (join through habit)
        if table == "person":
            where = "id = ?"
            params_base = (body.person_id,)
        elif table == "check_in":
            # check_ins don't have person_id, join through habit
            where = "habit_id IN (SELECT id FROM habit WHERE person_id = ?)"
            params_base = (body.person_id,)
        else:
            where = "person_id = ?"
            params_base = (body.person_id,)

        if body.last_sync:
            rows = db.execute(
                f"SELECT * FROM {table} WHERE {where} AND updated_at > ? AND deleted_at IS NULL",
                params_base + (body.last_sync,),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT * FROM {table} WHERE {where} AND deleted_at IS NULL",
                params_base,
            ).fetchall()

        dtos = []
        for row in rows:
            row_dict = dict(row)
            # Filter to fields the DTO accepts
            dto_fields = dto_class.model_fields.keys()
            filtered = {k: v for k, v in row_dict.items() if k in dto_fields and v is not None}
            try:
                dtos.append(dto_class(**filtered))
            except Exception:
                pass
        setattr(result_changes, response_key, dtos)

    # Update sync cursor
    db.execute(
        "INSERT INTO sync_cursor (device_id, person_id, last_sync_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(device_id, person_id) DO UPDATE SET last_sync_at = ?",
        ("ios-app", body.person_id, now, now),
    )
    db.commit()

    pulled = sum(
        len(getattr(result_changes, k, []))
        for k in ["persons", "habits", "check_ins", "focus_plans", "messages"]
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    _audit_v1("/api/v1/sync/ios", "POST", body.person_id, 200, elapsed,
              f"pushed={pushed} pulled={pulled}")

    return IosSyncResponse(
        server_changes=result_changes,
        sync_token=now,
    )


# --- Persons CRUD ---

@router.get("/persons")
def list_persons(request: Request, _token: str = Depends(_verify_token)):
    config = _get_config(request)
    db = get_db()
    init_db()
    rows = db.execute("SELECT * FROM person WHERE deleted_at IS NULL").fetchall()
    results = _serialize_list(rows, PersonOut)
    # Per-user token: filter to allowed persons only
    allowed = config.token_persons.get(_token)
    if allowed is not None:
        results = [p for p in results if p["id"] in allowed]
    return results


@router.get("/persons/{person_id}")
def get_person(person_id: str, request: Request, _token: str = Depends(_verify_token)):
    _check_person_access(request, _token, person_id)
    init_db()
    return _get_or_404("person", person_id)


@router.post("/persons", status_code=201)
def create_person(body: PersonCreate, _token: str = Depends(_verify_token)):
    db = get_db()
    init_db()
    now = _now_iso()
    pid = body.id or _new_id()
    data = body.model_dump(exclude_none=True, by_alias=False)
    data.pop("id", None)

    col_names = ["id", "name", "created_at", "updated_at"]
    col_vals = [pid, data["name"], now, now]

    for col in TABLE_COLUMNS["person"]:
        if col in data and col != "name":
            col_names.append(col)
            col_vals.append(data[col])

    placeholders = ", ".join(["?"] * len(col_names))
    db.execute(
        f"INSERT INTO person ({', '.join(col_names)}) VALUES ({placeholders})",
        col_vals,
    )
    db.commit()
    return _get_or_404("person", pid)


@router.put("/persons/{person_id}")
def update_person(person_id: str, body: PersonUpdate, request: Request, _token: str = Depends(_verify_token)):
    _check_person_access(request, _token, person_id)
    db = get_db()
    init_db()
    _get_or_404("person", person_id)  # 404 check
    now = _now_iso()
    data = body.model_dump(exclude_none=True, by_alias=False)
    if not data:
        return _get_or_404("person", person_id)

    sets = []
    vals = []
    for col, val in data.items():
        sets.append(f"{col} = ?")
        vals.append(val)
    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(person_id)

    db.execute(f"UPDATE person SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    return _get_or_404("person", person_id)


# --- Habits CRUD ---

@router.get("/persons/{person_id}/habits")
def list_habits(person_id: str, request: Request, _token: str = Depends(_verify_token)):
    _check_person_access(request, _token, person_id)
    db = get_db()
    init_db()
    rows = db.execute(
        "SELECT * FROM habit WHERE person_id = ? AND deleted_at IS NULL ORDER BY sort_order",
        (person_id,),
    ).fetchall()
    return _serialize_list(rows, HabitOut)


@router.post("/persons/{person_id}/habits", status_code=201)
def create_habit(person_id: str, body: HabitCreate, request: Request, _token: str = Depends(_verify_token)):
    _check_person_access(request, _token, person_id)
    db = get_db()
    init_db()
    _get_or_404("person", person_id)  # verify person exists
    now = _now_iso()
    hid = body.id or _new_id()
    data = body.model_dump(exclude_none=True, by_alias=False)
    data.pop("id", None)

    col_names = ["id", "person_id", "title", "created_at", "updated_at"]
    col_vals = [hid, person_id, data["title"], now, now]

    for col in TABLE_COLUMNS["habit"]:
        if col in data and col not in ("person_id", "title"):
            col_vals.append(data[col])
            col_names.append(col)

    placeholders = ", ".join(["?"] * len(col_names))
    db.execute(
        f"INSERT INTO habit ({', '.join(col_names)}) VALUES ({placeholders})",
        col_vals,
    )
    db.commit()
    return _get_or_404("habit", hid)


@router.put("/habits/{habit_id}")
def update_habit(habit_id: str, body: HabitUpdate, request: Request, _token: str = Depends(_verify_token)):
    db = get_db()
    init_db()
    habit = _get_or_404("habit", habit_id)
    _check_person_access(request, _token, habit["personId"])
    now = _now_iso()
    data = body.model_dump(exclude_none=True, by_alias=False)
    if not data:
        return _get_or_404("habit", habit_id)

    sets = []
    vals = []
    for col, val in data.items():
        sets.append(f"{col} = ?")
        vals.append(val)
    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(habit_id)

    db.execute(f"UPDATE habit SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    return _get_or_404("habit", habit_id)


# --- Check-ins CRUD ---

@router.get("/habits/{habit_id}/checkins")
def list_checkins(
    habit_id: str,
    request: Request,
    since: str | None = Query(None),
    _token: str = Depends(_verify_token),
):
    db = get_db()
    init_db()
    habit = _get_or_404("habit", habit_id)
    _check_person_access(request, _token, habit["personId"])
    if since:
        rows = db.execute(
            "SELECT * FROM check_in WHERE habit_id = ? AND deleted_at IS NULL AND date >= ? ORDER BY date",
            (habit_id, since),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM check_in WHERE habit_id = ? AND deleted_at IS NULL ORDER BY date",
            (habit_id,),
        ).fetchall()
    return _serialize_list(rows, CheckInOut)


@router.post("/habits/{habit_id}/checkins", status_code=201)
def create_checkin(habit_id: str, body: CheckInCreate, request: Request, _token: str = Depends(_verify_token)):
    db = get_db()
    init_db()
    habit = _get_or_404("habit", habit_id)
    _check_person_access(request, _token, habit["personId"])
    now = _now_iso()
    cid = body.id or _new_id()
    db.execute(
        "INSERT INTO check_in (id, habit_id, date, completed, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, habit_id, body.date, int(body.completed), body.note, now, now),
    )
    db.commit()
    return _get_or_404("check_in", cid)


# --- Apple Sign In Auth ---

@router.post("/auth/apple")
async def auth_apple(request: Request):
    """Exchange an Apple identity token for an access/refresh token pair.

    Identity reconciliation waterfall:
    1. apple_user_identifier match (returning user / reinstall)
    2. Name match against existing persons (existing Kiso user installs Kasane)
    3. Create new person with client-provided UUID (new Kasane user)

    Body: {
        "identity_token": "<JWT>",
        "person_id": "<optional CoreData UUID>",
        "full_name": "<optional, from SIWA credential.fullName>",
        "email": "<optional, from SIWA credential.email>"
    }
    """
    body = await request.json()

    identity_token = body.get("identity_token")
    if not identity_token:
        raise HTTPException(400, "identity_token is required")

    # Verify the Apple JWT
    claims = _verify_apple_identity_token(identity_token)
    apple_sub = claims["sub"]

    # Extract profile data from request body and JWT claims
    full_name = body.get("full_name")
    email = body.get("email") or claims.get("email")

    db = get_db()
    init_db()
    now = _now_iso()
    client_person_id = body.get("person_id")

    # --- Waterfall 1: match by apple_user_identifier (Case 3: reinstall) ---
    row = db.execute(
        "SELECT id FROM person WHERE apple_user_identifier = ? AND deleted_at IS NULL",
        (apple_sub,),
    ).fetchone()

    if row:
        person_id = row["id"]
        # Update email if we got one and don't have it yet
        if email:
            db.execute(
                "UPDATE person SET email = COALESCE(email, ?), updated_at = ? WHERE id = ?",
                (email, now, person_id),
            )
            db.commit()
    else:
        # --- Waterfall 2: match by name (Case 2: existing Kiso user) ---
        matched_row = None
        if full_name:
            matches = db.execute(
                "SELECT id FROM person WHERE name = ? COLLATE NOCASE "
                "AND apple_user_identifier IS NULL AND deleted_at IS NULL",
                (full_name,),
            ).fetchall()
            if len(matches) == 1:
                matched_row = matches[0]
            elif len(matches) > 1:
                logger.warning(
                    "Apple auth: %d persons match name=%s, skipping name match",
                    len(matches), full_name,
                )

        if matched_row:
            person_id = matched_row["id"]
            db.execute(
                "UPDATE person SET apple_user_identifier = ?, "
                "email = COALESCE(email, ?), updated_at = ? WHERE id = ?",
                (apple_sub, email, now, person_id),
            )
            db.commit()
            logger.info("Apple auth: matched existing person by name=%s", full_name)
        else:
            # --- Waterfall 3: create new person (Case 1: new Kasane user) ---
            person_id = client_person_id or _new_id()
            name = full_name or claims.get("email", "").split("@")[0] or "User"
            db.execute(
                "INSERT INTO person (id, name, email, apple_user_identifier, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (person_id, name, email, apple_sub, now, now),
            )
            db.commit()

    # Issue access + refresh tokens
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    now_ts = time.time()
    now_iso = _now_iso()

    db.execute(
        "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, expires_at, created_at) "
        "VALUES (?, 'access', 'kasane-ios', ?, 'health', ?, ?)",
        (access_token, person_id, now_ts + ACCESS_TOKEN_TTL, now_iso),
    )
    db.execute(
        "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, expires_at, created_at) "
        "VALUES (?, 'refresh', 'kasane-ios', ?, 'health', ?, ?)",
        (refresh_token, person_id, now_ts + REFRESH_TOKEN_TTL, now_iso),
    )
    db.commit()

    logger.info("Apple auth: person=%s apple_sub=%s", person_id, apple_sub[:8])

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "person_id": person_id,
    }


@router.post("/auth/refresh")
async def auth_refresh(request: Request):
    """Exchange a refresh token for a new access/refresh token pair.

    Body: {"refresh_token": "..."}
    """
    body = await request.json()

    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise HTTPException(400, "refresh_token is required")

    db = get_db()
    init_db()

    row = db.execute(
        "SELECT person_id, expires_at, revoked FROM oauth_token "
        "WHERE token = ? AND token_type = 'refresh'",
        (refresh_token,),
    ).fetchone()

    if not row:
        raise HTTPException(401, "Invalid refresh token")
    if row["revoked"]:
        raise HTTPException(401, "Refresh token revoked")
    if row["expires_at"] and row["expires_at"] < time.time():
        raise HTTPException(401, "Refresh token expired")

    person_id = row["person_id"]

    # Revoke only the used refresh token (not all tokens, to avoid race conditions
    # when multiple sync requests refresh simultaneously)
    db.execute(
        "UPDATE oauth_token SET revoked = 1 WHERE token = ?",
        (refresh_token,),
    )

    # Issue new pair
    new_access = secrets.token_urlsafe(32)
    new_refresh = secrets.token_urlsafe(32)
    now_ts = time.time()
    now_iso = _now_iso()

    db.execute(
        "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, expires_at, created_at) "
        "VALUES (?, 'access', 'kasane-ios', ?, 'health', ?, ?)",
        (new_access, person_id, now_ts + ACCESS_TOKEN_TTL, now_iso),
    )
    db.execute(
        "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, expires_at, created_at) "
        "VALUES (?, 'refresh', 'kasane-ios', ?, 'health', ?, ?)",
        (new_refresh, person_id, now_ts + REFRESH_TOKEN_TTL, now_iso),
    )
    db.commit()

    logger.info("Token refresh: person=%s", person_id)

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "person_id": person_id,
    }


# --- Focus Plans ---

@router.get("/persons/{person_id}/focus-plans")
def list_focus_plans(
    person_id: str,
    request: Request,
    limit: int = Query(10),
    _token: str = Depends(_verify_token),
):
    _check_person_access(request, _token, person_id)
    db = get_db()
    init_db()
    rows = db.execute(
        "SELECT * FROM focus_plan WHERE person_id = ? AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (person_id, limit),
    ).fetchall()
    return _serialize_list(rows, FocusPlanOut)


@router.post("/persons/{person_id}/focus-plans", status_code=201)
def create_focus_plan(person_id: str, body: FocusPlanCreate, request: Request, _token: str = Depends(_verify_token)):
    _check_person_access(request, _token, person_id)
    db = get_db()
    init_db()
    _get_or_404("person", person_id)
    now = _now_iso()
    fid = body.id or _new_id()
    data = body.model_dump(exclude_none=True, by_alias=False)
    data.pop("id", None)

    col_names = ["id", "person_id", "created_at", "updated_at"]
    col_vals = [fid, person_id, now, now]

    for col in TABLE_COLUMNS["focus_plan"]:
        if col in data and col != "person_id":
            col_names.append(col)
            col_vals.append(data[col])

    placeholders = ", ".join(["?"] * len(col_names))
    db.execute(
        f"INSERT INTO focus_plan ({', '.join(col_names)}) VALUES ({placeholders})",
        col_vals,
    )
    db.commit()
    return _get_or_404("focus_plan", fid)


# --- Context endpoint (Milo's unified read) ---

@router.get("/persons/{person_id}/context")
def get_person_context_api(person_id: str, request: Request, _token: str = Depends(_verify_token)):
    """Return merged context: SQLite person data + CSV health metrics.

    This is the primary endpoint Milo uses to get full coaching context.
    """
    _check_person_access(request, _token, person_id)
    init_db()
    return _build_person_context(person_id)


def _build_person_context(person_id: str) -> dict:
    """Build unified person context from SQLite + CSVs.

    Used by both the REST endpoint and the MCP tool.
    """
    db = get_db()

    # Person profile
    person_row = db.execute(
        "SELECT * FROM person WHERE id = ? AND deleted_at IS NULL", (person_id,)
    ).fetchone()
    if not person_row:
        raise HTTPException(404, f"Person {person_id} not found")
    person = _serialize(person_row, PersonOut)

    # Active habits with recent check-ins (last 30 days)
    habits = []
    habit_rows = db.execute(
        "SELECT * FROM habit WHERE person_id = ? AND deleted_at IS NULL AND state = 'active' "
        "ORDER BY sort_order",
        (person_id,),
    ).fetchall()
    for h in habit_rows:
        hd = _serialize(h, HabitOut)
        checkins = db.execute(
            "SELECT * FROM check_in WHERE habit_id = ? AND deleted_at IS NULL "
            "AND date >= date('now', '-30 days') ORDER BY date DESC",
            (hd["id"],),
        ).fetchall()
        hd["recentCheckins"] = _serialize_list(checkins, CheckInOut)
        habits.append(hd)

    # Latest focus plan
    fp_row = db.execute(
        "SELECT * FROM focus_plan WHERE person_id = ? AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (person_id,),
    ).fetchone()

    # Recent messages
    msg_rows = db.execute(
        "SELECT * FROM check_in_message WHERE person_id = ? AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT 20",
        (person_id,),
    ).fetchall()

    context = {
        "person": person,
        "activeHabits": habits,
        "latestFocusPlan": _serialize(fp_row, FocusPlanOut) if fp_row else None,
        "recentMessages": [_row_to_dict(m) for m in msg_rows],
    }

    # Merge CSV health data if health_engine_user_id is set
    he_user_id = person.get("healthEngineUserId")
    if he_user_id:
        context["health"] = _load_health_context(he_user_id)

    return context


def _load_health_context(user_id: str) -> dict:
    """Load health metrics for a user. SQLite first, CSV/JSON fallback."""
    from mcp_server.tools import _data_dir, _load_json_file

    data_dir = _data_dir(user_id)
    health = {}

    # Resolve person_id for SQLite queries
    _pid = None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        _db = get_db()
        _prow = _db.execute(
            "SELECT id FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        if _prow:
            _pid = _prow["id"]
    except Exception:
        pass

    # Weight trend (last 14 entries) — SQLite first, CSV fallback
    if _pid:
        try:
            _db = get_db()
            _wt_rows = _db.execute(
                "SELECT date, weight_lbs, waist_in, source FROM weight_entry "
                "WHERE person_id = ? ORDER BY date DESC LIMIT 14",
                (_pid,),
            ).fetchall()
            if _wt_rows:
                health["weight_recent"] = [dict(r) for r in reversed(_wt_rows)]
        except Exception:
            pass
    if "weight_recent" not in health:
        weight_path = data_dir / "weight_log.csv"
        if weight_path.exists():
            from engine.utils.csv_io import read_csv
            rows = read_csv(weight_path)
            health["weight_recent"] = rows[-14:] if rows else []

    # Latest wearable snapshot — SQLite first, JSON fallback
    if _pid:
        try:
            _db = get_db()
            _wrow = _db.execute(
                "SELECT * FROM wearable_daily WHERE person_id = ? "
                "ORDER BY date DESC, "
                "CASE source WHEN 'garmin' THEN 1 WHEN 'apple_health' THEN 2 ELSE 3 END "
                "LIMIT 1", (_pid,)
            ).fetchone()
            if _wrow:
                health["wearable_snapshot"] = dict(_wrow)
                health["wearable_source"] = _wrow["source"] or "garmin"
        except Exception:
            pass
    if "wearable_snapshot" not in health:
        for fname in ("garmin_latest.json", "oura_latest.json", "whoop_latest.json", "apple_health_latest.json"):
            snapshot = _load_json_file(data_dir / fname)
            if snapshot:
                health["wearable_snapshot"] = snapshot
                health["wearable_source"] = fname.replace("_latest.json", "")
                break

    # Latest labs — SQLite first, JSON fallback
    if _pid:
        try:
            _db = get_db()
            _lab_rows = _db.execute(
                "SELECT lr.marker, lr.value, lr.unit, lr.flag, ld.date "
                "FROM lab_result lr JOIN lab_draw ld ON lr.draw_id = ld.id "
                "WHERE lr.person_id = ? ORDER BY ld.date DESC LIMIT 20",
                (_pid,),
            ).fetchall()
            if _lab_rows:
                health["latest_labs"] = [dict(r) for r in _lab_rows]
        except Exception:
            pass
    if "latest_labs" not in health:
        labs = _load_json_file(data_dir / "lab_results.json")
        if labs and "latest" in labs:
            health["latest_labs"] = labs["latest"]

    # Today's meals — SQLite first, CSV fallback
    today = datetime.now().strftime("%Y-%m-%d")
    if _pid:
        try:
            _db = get_db()
            _meal_rows = _db.execute(
                "SELECT * FROM meal_entry WHERE person_id = ? AND date = ?",
                (_pid, today),
            ).fetchall()
            if _meal_rows:
                health["meals_today"] = [dict(r) for r in _meal_rows]
        except Exception:
            pass
    if "meals_today" not in health:
        meal_path = data_dir / "meal_log.csv"
        if meal_path.exists():
            from engine.utils.csv_io import read_csv
            rows = read_csv(meal_path)
            health["meals_today"] = [r for r in rows if r.get("date") == today]

    return health


# --- Issues endpoints (admin-only) ---


def _require_admin(request: Request, token: str = Query(None)):
    """Require the admin api_token (not per-user tokens)."""
    config = _get_config(request)
    effective = token
    if not effective:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            effective = auth[7:]
    if not effective or effective != config.api_token:
        raise HTTPException(403, "Admin token required")
    return effective


@router.get("/issues")
def list_issues_endpoint(
    request: Request,
    token: str = Depends(_require_admin),
    person_id: str = Query(None),
    status: str = Query(None),
):
    from .issues import list_issues
    db = get_db()
    issues = list_issues(db, person_id=person_id, status=status)
    return {"issues": issues, "count": len(issues)}


@router.post("/issues/{issue_id}/resolve")
def resolve_issue_endpoint(
    issue_id: str,
    request: Request,
    token: str = Depends(_require_admin),
):
    from .issues import resolve_issue
    db = get_db()
    resolve_issue(db, issue_id)
    return {"status": "resolved", "id": issue_id}


# --- Route registration ---

def register_v1_routes(app):
    """Register the v1 API router on the FastAPI app. Call before wildcard routes."""
    init_db()
    app.include_router(router)
