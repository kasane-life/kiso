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
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

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
)

router = APIRouter(prefix="/api/v1")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# --- Auth dependency ---

def _get_config(request: Request):
    return request.app.state.config


def _verify_token(request: Request, token: str = Query(None)):
    """Verify API token from query param or Authorization header."""
    config = _get_config(request)
    if not config.api_token:
        raise HTTPException(500, "API token not configured")

    effective = token
    if not effective:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            effective = auth[7:]

    if not effective or effective != config.api_token:
        raise HTTPException(403, "Invalid token")
    return effective


# --- Helpers ---

def _row_to_dict(row) -> dict:
    """Convert sqlite3.Row to dict, dropping None values for cleaner JSON."""
    if row is None:
        return {}
    return dict(row)


def _get_or_404(table: str, row_id: str, db=None) -> dict:
    """Fetch a row by id or raise 404."""
    if db is None:
        db = get_db()
    row = db.execute(f"SELECT * FROM {table} WHERE id = ? AND deleted_at IS NULL", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"{table} {row_id} not found")
    return _row_to_dict(row)


# --- Sync endpoint ---

@router.post("/sync")
def sync(body: SyncRequest, _token: str = Depends(_verify_token)):
    """Bidirectional sync: client pushes changes, pulls server changes.

    Conflict resolution: last-write-wins by updated_at timestamp.
    """
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
            d = _row_to_dict(row)
            server_changes.append({
                "entity": entity,
                "id": d["id"],
                "action": "delete" if d.get("deleted_at") else "upsert",
                "data": d,
                "updated_at": d["updated_at"],
            })

    # Update sync cursor
    db.execute(
        "INSERT INTO sync_cursor (device_id, person_id, last_sync_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(device_id, person_id) DO UPDATE SET last_sync_at = ?",
        (body.device_id, body.person_id, now, now),
    )
    db.commit()

    return SyncResponse(
        server_changes=server_changes,
        sync_at=now,
        stats={"pushed": pushed, "pulled": len(server_changes)},
    )


# --- Persons CRUD ---

@router.get("/persons")
def list_persons(_token: str = Depends(_verify_token)):
    db = get_db()
    init_db()
    rows = db.execute("SELECT * FROM person WHERE deleted_at IS NULL").fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/persons/{person_id}")
def get_person(person_id: str, _token: str = Depends(_verify_token)):
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
def update_person(person_id: str, body: PersonUpdate, _token: str = Depends(_verify_token)):
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
def list_habits(person_id: str, _token: str = Depends(_verify_token)):
    db = get_db()
    init_db()
    rows = db.execute(
        "SELECT * FROM habit WHERE person_id = ? AND deleted_at IS NULL ORDER BY sort_order",
        (person_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/persons/{person_id}/habits", status_code=201)
def create_habit(person_id: str, body: HabitCreate, _token: str = Depends(_verify_token)):
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
def update_habit(habit_id: str, body: HabitUpdate, _token: str = Depends(_verify_token)):
    db = get_db()
    init_db()
    _get_or_404("habit", habit_id)
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
    since: str | None = Query(None),
    _token: str = Depends(_verify_token),
):
    db = get_db()
    init_db()
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
    return [_row_to_dict(r) for r in rows]


@router.post("/habits/{habit_id}/checkins", status_code=201)
def create_checkin(habit_id: str, body: CheckInCreate, _token: str = Depends(_verify_token)):
    db = get_db()
    init_db()
    _get_or_404("habit", habit_id)
    now = _now_iso()
    cid = body.id or _new_id()
    db.execute(
        "INSERT INTO check_in (id, habit_id, date, completed, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, habit_id, body.date, int(body.completed), body.note, now, now),
    )
    db.commit()
    return _get_or_404("check_in", cid)


# --- Focus Plans ---

@router.get("/persons/{person_id}/focus-plans")
def list_focus_plans(
    person_id: str,
    limit: int = Query(10),
    _token: str = Depends(_verify_token),
):
    db = get_db()
    init_db()
    rows = db.execute(
        "SELECT * FROM focus_plan WHERE person_id = ? AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (person_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/persons/{person_id}/focus-plans", status_code=201)
def create_focus_plan(person_id: str, body: FocusPlanCreate, _token: str = Depends(_verify_token)):
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
def get_person_context_api(person_id: str, _token: str = Depends(_verify_token)):
    """Return merged context: SQLite person data + CSV health metrics.

    This is the primary endpoint Milo uses to get full coaching context.
    """
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
    person = _row_to_dict(person_row)

    # Active habits with recent check-ins (last 30 days)
    habits = []
    habit_rows = db.execute(
        "SELECT * FROM habit WHERE person_id = ? AND deleted_at IS NULL AND state = 'active' "
        "ORDER BY sort_order",
        (person_id,),
    ).fetchall()
    for h in habit_rows:
        hd = _row_to_dict(h)
        checkins = db.execute(
            "SELECT * FROM check_in WHERE habit_id = ? AND deleted_at IS NULL "
            "AND date >= date('now', '-30 days') ORDER BY date DESC",
            (hd["id"],),
        ).fetchall()
        hd["recent_checkins"] = [_row_to_dict(c) for c in checkins]
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
        "active_habits": habits,
        "latest_focus_plan": _row_to_dict(fp_row) if fp_row else None,
        "recent_messages": [_row_to_dict(m) for m in msg_rows],
    }

    # Merge CSV health data if health_engine_user_id is set
    he_user_id = person.get("health_engine_user_id")
    if he_user_id:
        context["health"] = _load_health_context(he_user_id)

    return context


def _load_health_context(user_id: str) -> dict:
    """Load health metrics from CSVs for the given health-engine user_id."""
    from mcp_server.tools import _data_dir, _load_json_file

    data_dir = _data_dir(user_id)
    health = {}

    # Weight trend (last 14 entries)
    weight_path = data_dir / "weight_log.csv"
    if weight_path.exists():
        from engine.utils.csv_io import read_csv
        rows = read_csv(weight_path)
        health["weight_recent"] = rows[-14:] if rows else []

    # Latest wearable snapshot
    for fname in ("garmin_latest.json", "oura_latest.json", "whoop_latest.json", "apple_health_latest.json"):
        snapshot = _load_json_file(data_dir / fname)
        if snapshot:
            health["wearable_snapshot"] = snapshot
            health["wearable_source"] = fname.replace("_latest.json", "")
            break

    # Latest labs
    labs = _load_json_file(data_dir / "lab_results.json")
    if labs and "latest" in labs:
        health["latest_labs"] = labs["latest"]

    # Today's meals
    meal_path = data_dir / "meal_log.csv"
    if meal_path.exists():
        from engine.utils.csv_io import read_csv
        today = datetime.now().strftime("%Y-%m-%d")
        rows = read_csv(meal_path)
        health["meals_today"] = [r for r in rows if r.get("date") == today]

    return health


# --- Route registration ---

def register_v1_routes(app):
    """Register the v1 API router on the FastAPI app. Call before wildcard routes."""
    init_db()
    app.include_router(router)
