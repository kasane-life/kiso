"""Tool definitions for the Health Engine MCP server.

Each tool has a standalone _function() that can be called directly (e.g. from
the HTTP API gateway) and a thin MCP-decorated wrapper registered via
register_tools(mcp).
"""

import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml
from engine.utils.csv_io import read_csv, write_csv, append_csv
from engine.db_read import get_weights, get_bp, get_meals, get_habits, get_labs, get_strength, get_wearable_daily

# User home directory for pip-installed (uvx) usage
_USER_HOME = Path(os.path.expanduser("~/.config/health-engine"))


def _user_dir(user_id: str) -> Path:
    """Per-user data directory under data/users/<user_id>/."""
    base = PROJECT_ROOT / "data" / "users" / user_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _config_path(user_id: str | None = None) -> Path:
    """Find config.yaml — per-user if user_id given, else legacy paths."""
    if user_id and user_id != "default":
        return _user_dir(user_id) / "config.yaml"
    local = PROJECT_ROOT / "config.yaml"
    if local.exists():
        return local
    home = _USER_HOME / "config.yaml"
    return home


def _resolve_person_id(user_id: str | None = None) -> str | None:
    """Resolve a health_engine_user_id to a SQLite person.id.

    Returns the person_id or None if no matching record exists.
    Used by tools that dual-write to CSV + SQLite during migration.
    """
    if not user_id:
        return None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        row = db.execute(
            "SELECT id FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


def _latest_weight_sqlite(person_id: str | None) -> float | None:
    """Get the most recent weight from SQLite."""
    if not person_id:
        return None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        row = db.execute(
            "SELECT weight_lbs FROM weight_entry WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        return row["weight_lbs"] if row else None
    except Exception:
        return None


def _latest_bp_sqlite(person_id: str | None) -> tuple[float, float] | None:
    """Get the most recent BP from SQLite. Returns (systolic, diastolic) or None."""
    if not person_id:
        return None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        row = db.execute(
            "SELECT systolic, diastolic FROM bp_entry WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        return (row["systolic"], row["diastolic"]) if row else None
    except Exception:
        return None


def _latest_labs_sqlite(person_id: str | None) -> dict | None:
    """Get latest lab values from SQLite. Returns dict of {marker: value} or None."""
    if not person_id:
        return None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        rows = db.execute(
            "SELECT lr.marker, lr.value, lr.value_text, ld.date "
            "FROM lab_result lr JOIN lab_draw ld ON lr.draw_id = ld.id "
            "WHERE lr.person_id = ? ORDER BY ld.date DESC",
            (person_id,),
        ).fetchall()
        if not rows:
            return None
        latest = {}
        for r in rows:
            if r["marker"] not in latest:
                latest[r["marker"]] = r["value"] if r["value"] is not None else r["value_text"]
        return latest
    except Exception:
        return None


def _get_token_store():
    """Get the shared TokenStore instance."""
    from engine.gateway.token_store import TokenStore
    return TokenStore()


def _garmin_token_dir(user_id: str | None = None) -> str | None:
    """Resolve per-user Garmin token directory. Returns None if no tokens exist.

    Uses SQLite-backed TokenStore (with automatic migration from legacy files).
    NEVER falls back to another user's tokens.
    """
    ts = _get_token_store()
    uid = user_id if user_id and user_id != "default" else "default"

    if uid != "default":
        if ts.has_token("garmin", uid):
            return str(ts.garmin_token_dir(uid))
        return None

    # Legacy CLI path (no user_id context)
    if ts.has_token("garmin", "default"):
        return str(ts.garmin_token_dir("default"))
    # Check old-style legacy path as absolute fallback for CLI
    legacy = Path(os.path.expanduser("~/.config/health-engine/garmin-tokens"))
    if legacy.exists() and any(legacy.iterdir()):
        return str(legacy)
    return None


def _load_config(user_id: str | None = None) -> dict:
    path = _config_path(user_id)
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _data_dir(user_id: str | None = None) -> Path:
    if user_id and user_id != "default":
        return _user_dir(user_id)
    config = _load_config()
    rel = config.get("data_dir", None)
    if rel:
        # If config exists at PROJECT_ROOT, resolve relative to it
        config_dir = _config_path().parent
        return (config_dir / rel).resolve()
    # Default: ~/.config/health-engine/data for uvx, ./data for local dev
    if (PROJECT_ROOT / "config.yaml").exists():
        return (PROJECT_ROOT / "data").resolve()
    data = _USER_HOME / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data


def _load_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


# Wearable attributes shared across scoring, checkin, onboard
_WEARABLE_ATTRS = (
    "resting_hr", "daily_steps_avg", "sleep_regularity_stddev",
    "sleep_duration_avg", "vo2_max", "hrv_rmssd_avg", "zone2_min_per_week",
)


def _load_wearable_data(data_dir: Path) -> dict | None:
    """Load wearable data with priority fallback: garmin > oura > whoop > apple_health."""
    sources = [
        "garmin_latest.json",
        "oura_latest.json",
        "whoop_latest.json",
        "apple_health_latest.json",
    ]
    for source in sources:
        data = _load_json_file(data_dir / source)
        if data is not None:
            return data
    return None


# -- Standard supplement stacks (edit here when stack changes) --
SUPPLEMENT_STACKS = {
    "morning": [
        {"name": "baby_aspirin", "dose": "81mg"},
        {"name": "vitamin_d", "dose": "5000 IU"},
        {"name": "fish_oil", "dose": "2 capsules (Nordic Naturals)"},
        {"name": "hims", "dose": "finasteride 1.2mg / minoxidil 3mg / biotin 2.5mg"},
        {"name": "heart_and_soil", "dose": "organ meat probiotics x3"},
        {"name": "celsius", "dose": "1 can"},
        {"name": "bone_broth", "dose": "8oz (Kettle & Fire)"},
    ],
    "evening": [
        {"name": "magnesium_glycinate", "dose": "200-400mg"},
        {"name": "melatonin", "dose": "300mcg"},
    ],
}


# Alias map: common lab report names → canonical keys
_LAB_ALIASES: dict[str, str] = {
    # Lipids
    "apo b": "apob", "apolipoprotein b": "apob", "apolipoprotein-b": "apob",
    "ldl": "ldl_c", "ldl cholesterol": "ldl_c", "ldl-c": "ldl_c",
    "hdl": "hdl_c", "hdl cholesterol": "hdl_c", "hdl-c": "hdl_c",
    "total cholesterol": "total_cholesterol", "cholesterol": "total_cholesterol",
    "triglycerides": "triglycerides", "trigs": "triglycerides", "trig": "triglycerides",
    # Metabolic
    "glucose": "fasting_glucose", "fasting glucose": "fasting_glucose",
    "a1c": "hba1c", "hemoglobin a1c": "hba1c", "hba1c": "hba1c", "hgba1c": "hba1c",
    "insulin": "fasting_insulin", "fasting insulin": "fasting_insulin",
    # Inflammation
    "hs-crp": "hscrp", "hscrp": "hscrp", "c-reactive protein": "hscrp",
    "high sensitivity crp": "hscrp", "high-sensitivity crp": "hscrp",
    "crp": "hscrp",
    # Liver
    "alt": "alt", "sgpt": "alt", "alanine aminotransferase": "alt",
    "ast": "ast", "sgot": "ast", "aspartate aminotransferase": "ast",
    "ggt": "ggt", "gamma-glutamyl transferase": "ggt",
    # Thyroid
    "tsh": "tsh", "thyroid stimulating hormone": "tsh",
    "free t4": "t4_free", "t4 free": "t4_free", "ft4": "t4_free",
    "free t3": "t3_free", "t3 free": "t3_free", "ft3": "t3_free",
    # Vitamins & minerals
    "vitamin d": "vitamin_d", "25-oh vitamin d": "vitamin_d", "25-hydroxy vitamin d": "vitamin_d",
    "ferritin": "ferritin",
    "iron saturation": "iron_saturation_pct", "iron sat": "iron_saturation_pct",
    # CBC
    "hemoglobin": "hemoglobin", "hgb": "hemoglobin", "hb": "hemoglobin",
    "wbc": "wbc", "white blood cells": "wbc", "white blood cell count": "wbc",
    "platelets": "platelets", "plt": "platelets", "platelet count": "platelets",
    # Hormones
    "testosterone": "testosterone_total", "total testosterone": "testosterone_total",
    "testosterone total": "testosterone_total",
    "free testosterone": "testosterone_free", "testosterone free": "testosterone_free",
    "shbg": "shbg", "sex hormone binding globulin": "shbg",
    "fsh": "fsh", "follicle stimulating hormone": "fsh",
    "lh": "lh", "luteinizing hormone": "lh",
    "estradiol": "estradiol", "e2": "estradiol",
    "cortisol": "cortisol",
    "dhea-s": "dhea_s", "dhea sulfate": "dhea_s", "dheas": "dhea_s",
    "prolactin": "prolactin",
    "leptin": "leptin",
    # Other
    "lp(a)": "lpa", "lipoprotein a": "lpa", "lipoprotein(a)": "lpa", "lpa": "lpa",
    "homocysteine": "homocysteine",
    "omega-3 index": "omega3_index", "omega3 index": "omega3_index",
    "psa": "psa",
    "uric acid": "uric_acid",
    "bun": "bun", "blood urea nitrogen": "bun",
    "mma": "mma", "methylmalonic acid": "mma",
    # Kidney
    "egfr": "egfr", "estimated gfr": "egfr", "glomerular filtration rate": "egfr",
    "estimated glomerular filtration rate": "egfr", "gfr": "egfr",
}

# Sanity ranges: (min, max) — values outside trigger a warning but still store
_LAB_RANGES: dict[str, tuple[float, float]] = {
    "ldl_c": (10, 500), "hdl_c": (5, 200), "total_cholesterol": (50, 600),
    "triglycerides": (10, 2000), "apob": (10, 400),
    "fasting_glucose": (20, 600), "hba1c": (2.0, 20.0), "fasting_insulin": (0.1, 300),
    "hscrp": (0.01, 200), "alt": (1, 2000), "ast": (1, 2000), "ggt": (1, 2000),
    "tsh": (0.01, 100), "vitamin_d": (1, 200), "ferritin": (1, 5000),
    "hemoglobin": (3, 25), "wbc": (0.5, 50), "platelets": (10, 1000),
    "lpa": (0, 500), "testosterone_total": (10, 2000), "testosterone_free": (0.1, 500),
    "shbg": (1, 300), "fsh": (0.1, 200), "lh": (0.1, 100),
    "estradiol": (1, 500), "cortisol": (0.1, 80), "dhea_s": (10, 1500),
    "homocysteine": (1, 100), "omega3_index": (0.5, 20), "psa": (0, 100),
    "uric_acid": (0.5, 20), "bun": (1, 150), "t4_free": (0.1, 10),
    "t3_free": (0.5, 15), "prolactin": (0.1, 500), "leptin": (0.1, 200),
    "mma": (10, 5000), "iron_saturation_pct": (1, 100),
    "egfr": (5, 200),
}

# Fields that feed into UserProfile scoring
_SCORED_FIELDS = {
    "ldl_c", "hdl_c", "total_cholesterol", "triglycerides", "apob",
    "fasting_glucose", "hba1c", "fasting_insulin", "hscrp",
    "alt", "ast", "ggt", "tsh", "vitamin_d", "ferritin",
    "hemoglobin", "wbc", "platelets", "lpa",
}


def _normalize_lab_key(name: str) -> str:
    """Normalize a biomarker name to its canonical key."""
    lower = name.strip().lower()
    if lower in _LAB_ALIASES:
        return _LAB_ALIASES[lower]
    underscored = lower.replace(" ", "_").replace("-", "_")
    return underscored


# =====================================================================
# Standalone tool implementations — callable without MCP
# =====================================================================

def _checkin(greeting: str = "morning check-in", user_id: str | None = None) -> dict:
    from engine.coaching.briefing import build_briefing

    # Pull fresh Garmin data before building the briefing
    garmin_pull_error = None
    try:
        from engine.integrations.garmin import GarminClient
        config_pre = _load_config(user_id)
        garmin_cfg = config_pre.get("garmin", {})
        if GarminClient.has_tokens(token_dir=garmin_cfg.get("token_dir")):
            result = _pull_garmin(user_id=user_id)
            if not result.get("pulled"):
                garmin_pull_error = result.get("error", "Unknown error")
    except Exception as e:
        garmin_pull_error = str(e)

    config = _load_config(user_id)
    if user_id and user_id != "default":
        config["data_dir"] = str(_data_dir(user_id))
    briefing = build_briefing(config)

    if garmin_pull_error:
        briefing["garmin_pull_failed"] = True
        briefing["garmin_pull_error"] = garmin_pull_error
        # Flag staleness
        garmin_data = briefing.get("garmin", {})
        if garmin_data.get("last_updated"):
            last_up = garmin_data['last_updated']
            briefing["garmin_stale_warning"] = f"WARNING: Garmin data is stale (last updated {last_up}). Pull failed: {garmin_pull_error}. Sleep, HR, HRV, steps may not reflect last night."

    return briefing


def _score(user_id: str | None = None) -> dict:
    from engine.models import Demographics, UserProfile
    from engine.scoring.engine import score_profile

    config = _load_config(user_id)
    profile_cfg = config.get("profile", {})
    demo = Demographics(
        age=profile_cfg.get("age", 35),
        sex=profile_cfg.get("sex", "M"),
    )
    profile = UserProfile(demographics=demo)

    if profile_cfg.get("family_history") is not None:
        profile.has_family_history = profile_cfg["family_history"]
    if profile_cfg.get("medications") is not None:
        profile.has_medication_list = True
    if profile_cfg.get("waist_inches") is not None:
        profile.waist_circumference = profile_cfg["waist_inches"]
    if profile_cfg.get("phq9_score") is not None:
        profile.phq9_score = profile_cfg["phq9_score"]

    data_dir = _data_dir(user_id)
    wearable_data = _load_wearable_data(data_dir)
    if wearable_data:
        for attr in _WEARABLE_ATTRS:
            val = wearable_data.get(attr)
            if val is not None:
                setattr(profile, attr, val)

    _pid = _resolve_person_id(user_id)
    bp = _latest_bp_sqlite(_pid)
    if bp:
        profile.systolic, profile.diastolic = bp
    else:
        bp_rows = get_bp(user_id, data_dir)
        if bp_rows and bp_rows[-1].get("systolic", "").strip():
            profile.systolic = float(bp_rows[-1]["systolic"])
            profile.diastolic = float(bp_rows[-1]["diastolic"])

    wt = _latest_weight_sqlite(_pid)
    if wt is not None:
        profile.weight_lbs = wt
    else:
        weight_rows = get_weights(user_id, data_dir)
        if weight_rows and weight_rows[-1].get("weight_lbs", "").strip():
            profile.weight_lbs = float(weight_rows[-1]["weight_lbs"])

    # Load lab results for scoring + clinical zones
    metric_dates = {}
    metric_counts = {}

    # Try SQLite labs first
    _sqlite_labs = _latest_labs_sqlite(_pid)
    if _sqlite_labs:
        for key in ("ldl_c", "hdl_c", "triglycerides", "apob", "fasting_glucose",
                    "hba1c", "fasting_insulin", "hscrp", "alt", "ggt", "tsh",
                    "ferritin", "hemoglobin", "lpa"):
            val = _sqlite_labs.get(key)
            if val is not None:
                setattr(profile, key, val)
        # Get draw dates and counts from SQLite
        if _pid:
            from engine.gateway.db import get_db as _gdb2
            _db2 = _gdb2()
            _draw_rows = _db2.execute(
                "SELECT lr.marker, ld.date FROM lab_result lr JOIN lab_draw ld ON lr.draw_id = ld.id "
                "WHERE lr.person_id = ? ORDER BY ld.date DESC", (_pid,)
            ).fetchall()
            for r in _draw_rows:
                if r["marker"] not in metric_dates:
                    metric_dates[r["marker"]] = r["date"]
                metric_counts[r["marker"]] = metric_counts.get(r["marker"], 0) + 1
    else:
        # Fallback to JSON
        lab_path = data_dir / "lab_results.json"
        if lab_path.exists():
            import json as json_mod
            with open(lab_path) as f:
                labs = json_mod.load(f)
            latest = labs.get("latest", {})
            for key in ("ldl_c", "hdl_c", "triglycerides", "apob", "fasting_glucose",
                        "hba1c", "fasting_insulin", "hscrp", "alt", "ggt", "tsh",
                        "ferritin", "hemoglobin", "lpa"):
                val = latest.get(key)
                if val is not None:
                    setattr(profile, key, val)
            for draw in labs.get("draws", []):
                draw_date = draw.get("date", "")
                for key in draw.get("results", {}):
                    if key not in metric_dates:
                        metric_dates[key] = draw_date
            for draw in labs.get("draws", []):
                for key in draw.get("results", {}):
                    metric_counts[key] = metric_counts.get(key, 0) + 1

    # Wearable dates
    if wearable_data:
        wearable_date = wearable_data.get("last_updated", "")[:10]
        if wearable_date:
            for attr in _WEARABLE_ATTRS:
                metric_dates[attr] = wearable_date

    output = score_profile(profile, metric_dates=metric_dates,
                           metric_counts=metric_counts)
    return {
        "coverage_score": output["coverage_score"],
        "coverage_fraction": output["coverage_fraction"],
        "tier1_pct": output["tier1_pct"],
        "tier1_fraction": output["tier1_fraction"],
        "tier2_pct": output["tier2_pct"],
        "tier2_fraction": output["tier2_fraction"],
        "avg_percentile": output["avg_percentile"],
        "results": [r.to_dict() for r in output["results"] if r.has_data],
        "gaps": [
            {"name": g.name, "weight": g.coverage_weight, "cost": g.cost_to_close}
            for g in output["gaps"]
        ],
    }


def _get_protocols(user_id: str | None = None) -> list[dict]:
    from engine.coaching.protocols import load_protocol, protocol_progress

    config = _load_config(user_id)
    focus_list = config.get("focus", [])
    if not focus_list:
        return [{"message": "No active protocols in config.yaml focus list."}]

    data_dir = _data_dir(user_id)
    habit_data = get_habits(user_id, data_dir=data_dir) or None

    garmin = None
    garmin_path = data_dir / "garmin_latest.json"
    if garmin_path.exists():
        with open(garmin_path) as f:
            garmin = json.load(f)

    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    for entry in focus_list:
        proto_name = entry.get("protocol")
        started = entry.get("started")
        if not proto_name or not started:
            continue
        proto = load_protocol(proto_name, protocols_dir=PROJECT_ROOT / "protocols")
        if not proto:
            results.append({"protocol": proto_name, "error": "Protocol file not found"})
            continue
        progress = protocol_progress(
            protocol=proto, started=started,
            habit_data=habit_data, garmin=garmin, as_of=today,
        )
        progress["priority"] = entry.get("priority", 99)
        results.append(progress)

    results.sort(key=lambda p: p.get("priority", 99))
    return results


def _log_weight(weight_lbs: float, date: str | None = None, user_id: str | None = None) -> dict:
    import uuid as _uuid
    date = date or datetime.now().strftime("%Y-%m-%d")

    # CSV write (legacy, kept during migration)
    data_dir = _data_dir(user_id)
    path = data_dir / "weight_log.csv"
    rows = read_csv(path)
    fieldnames = ["date", "weight_lbs", "source", "waist_in"]
    rows.append({"date": date, "weight_lbs": str(weight_lbs), "source": "mcp", "waist_in": ""})
    write_csv(path, rows, fieldnames=fieldnames)

    # SQLite write (new)
    person_id = _resolve_person_id(user_id)
    if person_id:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        now = datetime.now().isoformat()
        rid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:weight_entry:{date}"))
        db.execute(
            "INSERT OR REPLACE INTO weight_entry (id, person_id, date, weight_lbs, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, person_id, date, weight_lbs, "mcp", now, now),
        )
        db.commit()

    return {"logged": True, "date": date, "weight_lbs": weight_lbs}


def _log_bp(systolic: int, diastolic: int, date: str | None = None, user_id: str | None = None) -> dict:
    import uuid as _uuid
    date = date or datetime.now().strftime("%Y-%m-%d")

    # CSV write (legacy)
    data_dir = _data_dir(user_id)
    path = data_dir / "bp_log.csv"
    rows = read_csv(path)
    fieldnames = ["date", "systolic", "diastolic", "source"]
    rows.append({"date": date, "systolic": str(systolic), "diastolic": str(diastolic), "source": "mcp"})
    write_csv(path, rows, fieldnames=fieldnames)

    # SQLite write (new)
    person_id = _resolve_person_id(user_id)
    if person_id:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        now = datetime.now().isoformat()
        rid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:bp_entry:{date}"))
        db.execute(
            "INSERT OR REPLACE INTO bp_entry (id, person_id, date, systolic, diastolic, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, person_id, date, systolic, diastolic, "mcp", now, now),
        )
        db.commit()

    return {"logged": True, "date": date, "systolic": systolic, "diastolic": diastolic}


def _log_habits(habits: dict, date: str | None = None, user_id: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "daily_habits.csv"
    rows = read_csv(path)

    if rows:
        fieldnames = list(rows[0].keys())
    else:
        fieldnames = ["date"] + list(habits.keys()) + ["notes"]

    target_row = None
    for row in rows:
        if row.get("date") == date:
            target_row = row
            break
    if target_row is None:
        target_row = {"date": date}
        rows.append(target_row)

    for k in habits:
        if k not in fieldnames:
            fieldnames.insert(-1, k)

    for k, v in habits.items():
        target_row[k] = v

    write_csv(path, rows, fieldnames=fieldnames)

    # SQLite write (normalized: one row per habit)
    import uuid as _uuid
    person_id = _resolve_person_id(user_id)
    if person_id:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        now = datetime.now().isoformat()
        for habit_name, val in habits.items():
            completed = str(val).strip().lower() in ("1", "true", "yes", "x", "done")
            rid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:habit_log:{date}:{habit_name}"))
            db.execute(
                "INSERT OR REPLACE INTO habit_log (id, person_id, date, habit_name, completed, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, person_id, date, habit_name, int(completed), now, now),
            )
        db.commit()

    return {"logged": True, "date": date, "habits": habits}


def _log_supplements(stack: str | None = None, supplements: list[str] | None = None, date: str | None = None, user_id: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "supplement_log.csv"
    rows = read_csv(path)

    items: list[dict] = []
    if stack and stack in SUPPLEMENT_STACKS:
        items = SUPPLEMENT_STACKS[stack]
    elif supplements:
        all_supps = {s["name"]: s for st in SUPPLEMENT_STACKS.values() for s in st}
        for name in supplements:
            if name in all_supps:
                items.append(all_supps[name])
            else:
                items.append({"name": name, "dose": ""})
    else:
        return {"logged": False, "error": "Provide stack='morning'|'evening' or supplements=['name1', 'name2']. Available stacks: " + ", ".join(SUPPLEMENT_STACKS.keys())}

    fieldnames = ["date", "name", "dose", "stack", "source"]
    for item in items:
        rows.append({
            "date": date,
            "name": item["name"],
            "dose": item.get("dose", ""),
            "stack": stack or "individual",
            "source": "mcp",
        })
    write_csv(path, rows, fieldnames=fieldnames)

    logged_names = [i["name"] for i in items]
    return {"logged": True, "date": date, "count": len(items), "supplements": logged_names}


def _log_sleep(bed_time: str, wake_time: str, date: str | None = None, user_id: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")

    for time_str, name in [(bed_time, "bed_time"), (wake_time, "wake_time")]:
        try:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except (ValueError, IndexError, AttributeError):
            return {"logged": False, "error": f"Invalid {name} format '{time_str}'. Use HH:MM (e.g. '22:15')."}

    return _log_habits({"bed_time": bed_time, "wake_time": wake_time}, date, user_id=user_id)


def _log_session(
    rpe: float,
    duration_min: float | None = None,
    session_type: str = "training",
    name: str = "",
    date: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Log a training session RPE. Merges with Garmin workout data if available."""
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "session_log.csv"
    fieldnames = ["date", "rpe", "duration_min", "type", "name", "notes"]

    if rpe < 1 or rpe > 10:
        return {"error": "RPE must be between 1 and 10"}

    new_row = {
        "date": date,
        "rpe": str(rpe),
        "duration_min": str(duration_min) if duration_min else "",
        "type": session_type,
        "name": name,
        "notes": "",
    }
    append_csv(path, new_row, fieldnames=fieldnames)

    # SQLite write (new)
    import uuid as _uuid
    person_id = _resolve_person_id(user_id)
    if person_id:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        now = datetime.now().isoformat()
        rid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:training_session:{date}:{name}"))
        db.execute(
            "INSERT OR REPLACE INTO training_session (id, person_id, date, rpe, duration_min, type, name, notes, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, person_id, date, rpe, duration_min, session_type, name, None, "mcp", now, now),
        )
        db.commit()

    return {"logged": True, "date": date, "rpe": rpe, "duration_min": duration_min, "type": session_type}


def _log_meal(
    description: str,
    protein_g: float,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    calories: float | None = None,
    date: str | None = None,
    user_id: str | None = None,
) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "meal_log.csv"
    rows = read_csv(path)
    fieldnames = ["date", "meal_num", "time_of_day", "description", "protein_g", "carbs_g", "fat_g", "calories", "notes"]

    meals_today = [r for r in rows if r.get("date") == date]
    meal_num = len(meals_today) + 1

    hour = datetime.now().hour
    time_of_day = "AM" if hour < 12 else ("PM" if hour < 17 else "EVE")

    new_row = {
        "date": date,
        "meal_num": str(meal_num),
        "time_of_day": time_of_day,
        "description": description,
        "protein_g": str(protein_g),
        "carbs_g": str(carbs_g) if carbs_g is not None else "",
        "fat_g": str(fat_g) if fat_g is not None else "",
        "calories": str(calories) if calories is not None else "",
        "notes": "",
    }
    append_csv(path, new_row, fieldnames=fieldnames)

    # SQLite write (new)
    import uuid as _uuid
    person_id = _resolve_person_id(user_id)
    if person_id:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        now = datetime.now().isoformat()
        rid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:meal_entry:{date}:{meal_num}:{description[:50]}"))
        db.execute(
            "INSERT OR IGNORE INTO meal_entry (id, person_id, date, meal_num, time_of_day, description, "
            "protein_g, carbs_g, fat_g, calories, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, person_id, date, meal_num, time_of_day, description,
             protein_g, carbs_g, fat_g, calories, None, now, now),
        )
        db.commit()

    return {"logged": True, "date": date, "meal_num": meal_num, "description": description, "protein_g": protein_g}


def _get_meals(
    date: str | None = None,
    days: int = 1,
    user_id: str | None = None,
) -> dict:
    from engine.tracking.nutrition import daily_totals, remaining_to_hit
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)

    # Load meals from SQLite first, CSV fallback
    _pid = _resolve_person_id(user_id)
    rows = None
    _meal_source = "csv_fallback"
    if _pid:
        from engine.gateway.db import get_db, init_db
        init_db()
        _mdb = get_db()
        _mrows = _mdb.execute(
            "SELECT date, meal_num, time_of_day, description, protein_g, carbs_g, fat_g, calories, notes "
            "FROM meal_entry WHERE person_id = ? ORDER BY date, meal_num", (_pid,)
        ).fetchall()
        if _mrows:
            rows = [dict(r) for r in _mrows]
            _meal_source = "sqlite"
    if rows is None:
        rows = get_meals(user_id, data_dir=data_dir)

    # Load burns from SQLite wearable_daily, JSON fallback
    burn_by_date = {}
    _burn_source = "json_fallback"
    if _pid:
        _brows = _mdb.execute(
            "SELECT date, calories_total, calories_active, calories_bmr FROM wearable_daily "
            "WHERE person_id = ? AND calories_total IS NOT NULL", (_pid,)
        ).fetchall()
        for b in _brows:
            burn_by_date[b["date"]] = {"total": b["calories_total"], "active": b["calories_active"], "bmr": b["calories_bmr"]}
        if burn_by_date:
            _burn_source = "sqlite"
    if not burn_by_date:
        burn_path = data_dir / "garmin_daily_burn.json"
        if burn_path.exists():
            with open(burn_path) as f:
                burns = json.load(f)
            for b in burns:
                burn_by_date[b["date"]] = b

    from datetime import timedelta
    end = datetime.strptime(date, "%Y-%m-%d")
    dates = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    result = {}
    for d in sorted(dates):
        day_meals = [r for r in rows if r.get("date") == d]
        if day_meals:
            totals = daily_totals(day_meals)
            day_result = {"data_source": {"meals": _meal_source, "burns": _burn_source}, "meals": day_meals, "totals": totals}

            config = _load_config(user_id)
            targets = config.get("targets", {})
            if targets:
                cal_target = targets.get("calories_training", targets.get("calories_rest"))
                protein_target = targets.get("protein_g")
                if cal_target and protein_target:
                    macro_targets = {"calories": cal_target, "protein_g": protein_target}
                    day_result["remaining"] = remaining_to_hit(day_meals, macro_targets)
        else:
            totals = {"protein_g": 0, "calories": 0}
            day_result = {"meals": [], "totals": totals}

        burn = burn_by_date.get(d)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if d == today_str:
            try:
                from engine.integrations.garmin import GarminClient
                gc_token_dir = _garmin_token_dir(user_id)
                if gc_token_dir:
                    gc_config = _load_config(user_id)
                    gc_config.setdefault("garmin", {})["token_dir"] = gc_token_dir
                    gc = GarminClient.from_config(gc_config)
                    gc.data_dir = data_dir
                    live = gc.pull_today()
                else:
                    live = {}
                if live.get("calories_total") and live["calories_total"] > 0:
                    burn = {
                        "total": live["calories_total"],
                        "active": live.get("calories_active"),
                        "bmr": live.get("calories_bmr"),
                    }
            except Exception:
                pass

        if burn and burn.get("total"):
            day_result["garmin_burn"] = {
                "total": burn["total"],
                "active": burn.get("active"),
                "bmr": burn.get("bmr"),
            }
            intake = float(totals.get("calories", 0) or 0)
            day_result["calorie_balance"] = {
                "intake": intake,
                "burn": burn["total"],
                "surplus_deficit": round(intake - burn["total"]),
                "status": "surplus" if intake > burn["total"] else "deficit",
            }

        result[d] = day_result

    return result


def _log_medication(
    name: str,
    dose: str,
    route: str | None = None,
    notes: str | None = None,
    date: str | None = None,
    user_id: str | None = None,
) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "medication_log.csv"
    rows = read_csv(path)
    fieldnames = ["date", "name", "dose", "route", "notes", "source"]

    rows.append({
        "date": date,
        "name": name,
        "dose": dose,
        "route": route or "",
        "notes": notes or "",
        "source": "mcp",
    })
    write_csv(path, rows, fieldnames=fieldnames)
    return {"logged": True, "date": date, "name": name, "dose": dose, "route": route or ""}


def _get_status(user_id: str | None = None) -> dict:
    data_dir = _data_dir(user_id)
    files = {}
    for name in ["weight_log.csv", "bp_log.csv", "meal_log.csv", "daily_habits.csv",
                  "strength_log.csv", "medication_log.csv", "supplement_log.csv",
                  "garmin_latest.json", "garmin_daily.json",
                  "apple_health_latest.json", "lab_results.json", "briefing.json"]:
        path = data_dir / name
        if path.exists():
            stat = path.stat()
            info = {
                "exists": True,
                "last_modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size_bytes": stat.st_size,
            }
            if name.endswith(".csv"):
                rows = read_csv(path)
                info["rows"] = len(rows)
            files[name] = info
        else:
            files[name] = {"exists": False}

    config = _load_config(user_id)
    has_config = bool(config.get("profile", {}).get("age"))
    return {"data_dir": str(data_dir), "config_loaded": has_config, "files": files}


def _onboard(user_id: str | None = None) -> dict:
    from engine.models import Demographics, UserProfile
    from engine.scoring.engine import score_profile

    config = _load_config(user_id)
    profile_cfg = config.get("profile", {})
    data_dir = _data_dir(user_id)

    has_config = bool(profile_cfg.get("age"))
    if has_config:
        demo = Demographics(
            age=profile_cfg.get("age", 35),
            sex=profile_cfg.get("sex", "M"),
        )
    else:
        demo = Demographics(age=35, sex="M")

    profile = UserProfile(demographics=demo)

    if profile_cfg.get("family_history") is not None:
        profile.has_family_history = profile_cfg["family_history"]
    if profile_cfg.get("medications") is not None:
        profile.has_medication_list = True
    if profile_cfg.get("waist_inches") is not None:
        profile.waist_circumference = profile_cfg["waist_inches"]
    if profile_cfg.get("phq9_score") is not None:
        profile.phq9_score = profile_cfg["phq9_score"]

    garmin_path = data_dir / "garmin_latest.json"
    wearable_data = _load_wearable_data(data_dir)
    if wearable_data:
        for attr in _WEARABLE_ATTRS:
            val = wearable_data.get(attr)
            if val is not None:
                setattr(profile, attr, val)

    _pid = _resolve_person_id(user_id)
    bp = _latest_bp_sqlite(_pid)
    if bp:
        profile.systolic, profile.diastolic = bp
    else:
        bp_rows = get_bp(user_id, data_dir)
        if bp_rows and bp_rows[-1].get("systolic", "").strip():
            profile.systolic = float(bp_rows[-1]["systolic"])
            profile.diastolic = float(bp_rows[-1]["diastolic"])

    wt = _latest_weight_sqlite(_pid)
    if wt is not None:
        profile.weight_lbs = wt
    else:
        weight_rows = get_weights(user_id, data_dir)
        if weight_rows and weight_rows[-1].get("weight_lbs", "").strip():
            profile.weight_lbs = float(weight_rows[-1]["weight_lbs"])

    _sqlite_labs2 = _latest_labs_sqlite(_pid)
    if _sqlite_labs2:
        for key in ("ldl_c", "hdl_c", "triglycerides", "apob", "fasting_glucose",
                    "hba1c", "fasting_insulin", "hscrp", "alt", "ggt", "tsh",
                    "ferritin", "hemoglobin", "lpa"):
            val = _sqlite_labs2.get(key)
            if val is not None:
                setattr(profile, key, val)
    else:
        lab_path = data_dir / "lab_results.json"
        if lab_path.exists():
            with open(lab_path) as f:
                labs = json.load(f)
            latest = labs.get("latest", {})
            for key in ("ldl_c", "hdl_c", "triglycerides", "apob", "fasting_glucose",
                        "hba1c", "fasting_insulin", "hscrp", "alt", "ggt", "tsh",
                        "ferritin", "hemoglobin", "lpa"):
                val = latest.get(key)
                if val is not None:
                    setattr(profile, key, val)

    output = score_profile(profile)
    total_weight = sum(r.coverage_weight for r in output["results"])

    coverage_map = []
    for r in output["results"]:
        entry = {
            "name": r.name,
            "tier": r.tier,
            "weight": r.coverage_weight,
            "weight_pct": round(r.coverage_weight / total_weight * 100, 1),
        }
        if r.has_data and r.value is not None:
            entry["status"] = "scored"
            entry["value"] = r.value
            entry["unit"] = r.unit
            entry["standing"] = r.standing.value
            entry["percentile"] = r.percentile_approx
        elif r.has_data:
            entry["status"] = "collected"
            entry["standing"] = r.standing.value
        else:
            entry["status"] = "missing"
            entry["cost"] = r.cost_to_close
            entry["why"] = r.note
        coverage_map.append(entry)

    next_steps = []
    for g in output["gaps"][:5]:
        next_steps.append({
            "name": g.name,
            "tier": g.tier,
            "weight": g.coverage_weight,
            "coverage_boost": round(g.coverage_weight / total_weight * 100, 1),
            "cost": g.cost_to_close,
            "why": g.note,
        })

    data_sources = {}
    for name in ["garmin_latest.json", "apple_health_latest.json",
                  "bp_log.csv", "weight_log.csv",
                  "meal_log.csv", "daily_habits.csv", "lab_results.json",
                  "strength_log.csv"]:
        data_sources[name] = (data_dir / name).exists()

    from engine.integrations.garmin import GarminClient
    garmin_token_dir = _garmin_token_dir(user_id)
    garmin_tokens = garmin_token_dir is not None
    garmin_has_data = garmin_path.exists()
    garmin_freshness = None
    if garmin_has_data:
        garmin_json = _load_json_file(garmin_path)
        if garmin_json:
            garmin_freshness = garmin_json.get("last_updated")

    if not garmin_tokens:
        garmin_hint = "Run `python3 cli.py auth garmin` to authenticate."
    elif not garmin_has_data:
        garmin_hint = "Tokens cached. Run `python3 cli.py pull garmin` to fetch data."
    else:
        garmin_hint = "Connected. Pull to refresh."

    ah_has_data = (data_dir / "apple_health_latest.json").exists()
    ah_freshness = None
    if ah_has_data:
        ah_data = _load_json_file(data_dir / "apple_health_latest.json")
        if ah_data:
            ah_freshness = ah_data.get("last_updated")

    wearables = {
        "garmin": {
            "tokens_cached": garmin_tokens,
            "has_data": garmin_has_data,
            "freshness": garmin_freshness,
            "connect_hint": garmin_hint,
        },
        "apple_health": {
            "has_data": ah_has_data,
            "freshness": ah_freshness,
            "connect_hint": (
                "Connected." if ah_has_data else
                "Export from iPhone: Settings → Health → Export Health Data. "
                "Then run `python3 cli.py import apple-health /path/to/export.zip`."
            ),
        },
    }

    return {
        "profile": {
            "age": profile_cfg.get("age"),
            "sex": profile_cfg.get("sex"),
            "configured": has_config,
            "family_history": profile_cfg.get("family_history"),
            "medications": profile_cfg.get("medications"),
            "waist_inches": profile_cfg.get("waist_inches"),
            "phq9_score": profile_cfg.get("phq9_score"),
        },
        "coverage_score": output["coverage_score"],
        "tier1_pct": output["tier1_pct"],
        "tier2_pct": output["tier2_pct"],
        "coverage_map": coverage_map,
        "next_steps": next_steps,
        "data_sources_detected": data_sources,
        "wearables": wearables,
        "interests_hint": (
            "Ask the user what they care about most — heart health, fitness, "
            "metabolic health, longevity, mental health — to personalize which "
            "gaps to prioritize. Not everyone needs all 20 metrics."
        ),
    }


def _auth_garmin(user_id: str | None = None) -> dict:
    from mcp_server.garmin_auth import run_auth_flow

    # Per-user token directory: tokens/garmin/{user_id}
    if user_id and user_id != "default":
        token_dir = str(Path(os.path.expanduser("~/.config/health-engine/tokens/garmin")) / user_id)
    else:
        token_dir = os.path.expanduser("~/.config/health-engine/garmin-tokens")
    Path(token_dir).mkdir(parents=True, exist_ok=True)
    return run_auth_flow(token_dir=token_dir)


def _pull_garmin(history: bool = False, workouts: bool = False, user_id: str | None = None) -> dict:
    from engine.integrations.garmin import GarminClient

    config = _load_config(user_id)
    if user_id and user_id != "default":
        config["data_dir"] = str(_data_dir(user_id))
    # Per-user Garmin tokens: never fall back to another user's tokens
    token_dir = _garmin_token_dir(user_id)
    if not token_dir:
        return {
            "pulled": False,
            "error": f"No Garmin tokens for user '{user_id or 'default'}'. They need to authenticate first.",
            "hint": f"Send them the auth link: connect_wearable(service='garmin', user_id='{user_id}')",
        }
    config.setdefault("garmin", {})["token_dir"] = token_dir
    try:
        client = GarminClient.from_config(config)
        person_id = _resolve_person_id(user_id)
        result = client.pull_all(
            history=history,
            history_days=90,
            workouts=workouts,
            workout_days=7,
            person_id=person_id,
        )
        # Sync any token refreshes back to SQLite
        uid = user_id if user_id and user_id != "default" else "default"
        _get_token_store().sync_garmin_tokens(uid)

        from engine.coaching.briefing import build_briefing
        briefing = build_briefing(config)
        data_dir = Path(config.get("data_dir", "./data"))
        with open(data_dir / "briefing.json", "w") as f:
            json.dump(briefing, f, indent=2, default=str)

        return {
            "pulled": True,
            "metrics": result,
            "briefing_refreshed": True,
        }
    except Exception as e:
        return {
            "pulled": False,
            "error": str(e),
            "hint": "If tokens expired, use auth_garmin to re-authenticate via browser.",
        }


def _connect_garmin(user_id: str | None = None) -> dict:
    from engine.integrations.garmin import GarminClient

    token_dir = _garmin_token_dir(user_id)
    has_tokens = token_dir is not None

    data_dir = _data_dir(user_id)
    garmin_path = data_dir / "garmin_latest.json"
    has_data = garmin_path.exists()
    freshness = None
    if has_data:
        with open(garmin_path) as f:
            garmin = json.load(f)
        freshness = garmin.get("last_updated")

    if not has_tokens:
        hint = "No Garmin tokens found. Use auth_garmin tool with your email and password."
    elif not has_data:
        hint = "Tokens cached but no data yet. Use pull_garmin tool to fetch metrics."
    else:
        hint = "Connected. Use pull_garmin tool to refresh data."

    return {
        "tokens_cached": has_tokens,
        "has_data": has_data,
        "last_updated": freshness,
        "hint": hint,
    }


def _auth_oura(user_id: str | None = None) -> dict:
    """Kick off Oura OAuth flow. Requires oura.client_id and oura.client_secret in gateway.yaml."""
    from engine.gateway.config import load_gateway_config
    from engine.integrations.oura_auth import run_auth_flow

    gw_config = load_gateway_config()
    oura_cfg = gw_config.oura
    client_id = oura_cfg.get("client_id")
    client_secret = oura_cfg.get("client_secret")

    if not client_id or not client_secret:
        return {
            "authenticated": False,
            "error": "Oura OAuth not configured. Add oura.client_id and oura.client_secret to gateway.yaml.",
        }

    uid = user_id
    if not uid:
        return {"error": "user_id is required."}
    return run_auth_flow(
        client_id=client_id,
        client_secret=client_secret,
        user_id=uid,
    )


def _pull_oura(history: bool = False, user_id: str | None = None) -> dict:
    """Pull health metrics from Oura Ring API."""
    from engine.integrations.oura import OuraClient

    uid = user_id
    if not uid:
        return {"error": "user_id is required."}
    config = _load_config(user_id)
    data_dir_path = str(_data_dir(uid)) if uid and uid != "default" else config.get("data_dir", "./data")

    try:
        client = OuraClient(
            user_id=uid,
            data_dir=data_dir_path,
        )
        result = client.pull_all(history=history, history_days=90)

        # Rebuild briefing
        if uid and uid != "default":
            config["data_dir"] = data_dir_path
        from engine.coaching.briefing import build_briefing
        briefing = build_briefing(config)
        briefing_dir = Path(data_dir_path)
        briefing_dir.mkdir(parents=True, exist_ok=True)
        with open(briefing_dir / "briefing.json", "w") as f:
            json.dump(briefing, f, indent=2, default=str)

        return {
            "pulled": True,
            "metrics": result,
            "briefing_refreshed": True,
        }
    except Exception as e:
        return {
            "pulled": False,
            "error": str(e),
            "hint": "If tokens expired, use auth_oura to re-authenticate.",
        }


def _connect_oura(user_id: str | None = None) -> dict:
    """Check Oura connection status."""
    from engine.integrations.oura import OuraClient

    uid = user_id
    if not uid:
        return {"error": "user_id is required."}
    has_tokens = OuraClient.has_tokens(user_id=uid)

    data_dir = _data_dir(uid)
    oura_path = data_dir / "oura_latest.json"
    has_data = oura_path.exists()
    freshness = None
    if has_data:
        with open(oura_path) as f:
            oura = json.load(f)
        freshness = oura.get("last_updated")

    if not has_tokens:
        hint = "No Oura tokens found. Use auth_oura tool to authenticate via browser."
    elif not has_data:
        hint = "Tokens cached but no data yet. Use pull_oura tool to fetch metrics."
    else:
        hint = "Connected. Use pull_oura tool to refresh data."

    return {
        "tokens_cached": has_tokens,
        "has_data": has_data,
        "last_updated": freshness,
        "hint": hint,
    }


def _auth_whoop(user_id: str | None = None) -> dict:
    """Kick off WHOOP OAuth flow. Requires whoop.client_id and whoop.client_secret in gateway.yaml."""
    from engine.gateway.config import load_gateway_config
    from engine.integrations.whoop_auth import run_auth_flow

    gw_config = load_gateway_config()
    whoop_cfg = gw_config.whoop
    client_id = whoop_cfg.get("client_id")
    client_secret = whoop_cfg.get("client_secret")

    if not client_id or not client_secret:
        return {
            "authenticated": False,
            "error": "WHOOP OAuth not configured. Add whoop.client_id and whoop.client_secret to gateway.yaml.",
        }

    uid = user_id
    if not uid:
        return {"error": "user_id is required."}
    return run_auth_flow(
        client_id=client_id,
        client_secret=client_secret,
        user_id=uid,
    )


def _pull_whoop(history: bool = False, user_id: str | None = None) -> dict:
    """Pull health metrics from WHOOP API."""
    from engine.integrations.whoop import WhoopClient

    uid = user_id
    if not uid:
        return {"error": "user_id is required."}
    config = _load_config(user_id)
    data_dir_path = str(_data_dir(uid)) if uid and uid != "default" else config.get("data_dir", "./data")

    try:
        client = WhoopClient(
            user_id=uid,
            data_dir=data_dir_path,
        )
        result = client.pull_all(history=history, history_days=90)

        # Rebuild briefing
        if uid and uid != "default":
            config["data_dir"] = data_dir_path
        from engine.coaching.briefing import build_briefing
        briefing = build_briefing(config)
        briefing_dir = Path(data_dir_path)
        briefing_dir.mkdir(parents=True, exist_ok=True)
        with open(briefing_dir / "briefing.json", "w") as f:
            json.dump(briefing, f, indent=2, default=str)

        return {
            "pulled": True,
            "metrics": result,
            "briefing_refreshed": True,
        }
    except Exception as e:
        return {
            "pulled": False,
            "error": str(e),
            "hint": "If tokens expired, use auth_whoop to re-authenticate.",
        }


def _connect_whoop(user_id: str | None = None) -> dict:
    """Check WHOOP connection status."""
    from engine.integrations.whoop import WhoopClient

    uid = user_id
    if not uid:
        return {"error": "user_id is required."}
    has_tokens = WhoopClient.has_tokens(user_id=uid)

    data_dir = _data_dir(uid)
    whoop_path = data_dir / "whoop_latest.json"
    has_data = whoop_path.exists()
    freshness = None
    if has_data:
        with open(whoop_path) as f:
            whoop = json.load(f)
        freshness = whoop.get("last_updated")

    if not has_tokens:
        hint = "No WHOOP tokens found. Use auth_whoop tool to authenticate via browser."
    elif not has_data:
        hint = "Tokens cached but no data yet. Use pull_whoop tool to fetch metrics."
    else:
        hint = "Connected. Use pull_whoop tool to refresh data."

    return {
        "tokens_cached": has_tokens,
        "has_data": has_data,
        "last_updated": freshness,
        "hint": hint,
    }


def _connect_wearable(service: str, user_id: str | None = None) -> dict:
    if not user_id:
        return {"error": "user_id is required. Pass the user_id for the person you are connecting a wearable for."}
    if service in ("apple_health", "apple_watch", "apple"):
        install_url = "https://www.icloud.com/shortcuts/b0c11b2912c1434fad4a2d87f4d2a762"
        return {
            "service": "apple_health",
            "supported": True,
            "setup_method": "icloud_link",
            "install_url": install_url,
            "coach_instructions": (
                "Apple Watch is fully supported. Send TWO messages:\n"
                "Message 1: Send the install_url link. Say: "
                "'Tap this link to add a shortcut that syncs your Apple Watch "
                "data to me every morning. When it opens, tap Add Shortcut.'\n"
                "Message 2: After they confirm it installed, say: "
                "'Now open Shortcuts, tap Automation at the bottom, tap +, "
                "pick Time of Day, set 7 AM, choose Baseline Health Sync, "
                "and turn off Ask Before Running. That is it.'\n"
                "The first time it runs, their phone will ask permission to "
                "read health data. Just tap Allow for everything.\n"
                "Do NOT use technical language. Do NOT mention APIs, JSON, tokens, "
                "signing, or endpoints."
            ),
        }

    supported = ["garmin", "oura", "whoop"]
    if service not in supported:
        return {
            "error": f"Unsupported service: {service}. Supported: {', '.join(supported)}. For Apple Health, use service='apple_health'.",
        }

    from engine.gateway.config import load_gateway_config
    gw_config = load_gateway_config()

    from engine.gateway.token_store import TokenStore
    ts = TokenStore()

    if ts.has_token(service, user_id):
        data_dir = _data_dir(user_id)
        has_data = _load_wearable_data(data_dir) is not None
        if has_data:
            return {
                "already_connected": True,
                "service": service,
                "user_id": user_id,
                "hint": f"{service.title()} is already connected. Use pull_{service} to refresh data.",
            }

    import hashlib, hmac, time as _time, secrets
    secret = gw_config.hmac_secret or secrets.token_hex(32)
    bucket = str(int(_time.time()) // 3600)
    payload = f"{user_id}:{service}:{bucket}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    state = f"{payload}:{sig}"
    auth_url = f"{gw_config.base_url}/auth/{service}?user={user_id}&state={state}"

    return {
        "auth_url": auth_url,
        "service": service,
        "user_id": user_id,
        "instructions": f"Send this link to the user. They tap it, sign in to {service.title()}, and tokens are cached automatically.",
    }


def _connect_google_calendar(user_id: str | None = None) -> dict:
    if not user_id:
        return {"error": "user_id is required."}
    """Generate a tappable OAuth link for connecting Google Calendar."""
    from engine.gateway.config import load_gateway_config
    from engine.gateway.token_store import TokenStore

    gw_config = load_gateway_config()
    ts = TokenStore()

    if ts.has_token("google-calendar", user_id):
        return {
            "already_connected": True,
            "service": "google-calendar",
            "user_id": user_id,
            "hint": "Google Calendar is already connected. Use calendar_list_events to verify.",
        }

    import hashlib, hmac, time as _time, secrets
    secret = gw_config.hmac_secret or secrets.token_hex(32)
    bucket = str(int(_time.time()) // 3600)
    payload = f"{user_id}:google-calendar:{bucket}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    state = f"{payload}:{sig}"
    auth_url = f"{gw_config.base_url}/auth/google?user={user_id}&state={state}"

    return {
        "auth_url": auth_url,
        "service": "google-calendar",
        "user_id": user_id,
        "instructions": "Send this link to the user. They tap it, authorize Google Calendar access, and tokens are saved automatically.",
    }


def _get_daily_snapshot(user_id: str | None = None) -> dict:
    from engine.tracking.nutrition import daily_totals, remaining_to_hit

    data_dir = _data_dir(user_id)
    today = datetime.now().strftime("%Y-%m-%d")

    garmin_snapshot = {}
    try:
        config = _load_config(user_id)
        from engine.integrations.garmin import GarminClient
        gc = GarminClient.from_config(config)
        gc.data_dir = data_dir
        garmin_snapshot = gc.pull_today()
    except Exception as e:
        garmin_snapshot = {"error": str(e)}

    rows = get_meals(user_id, data_dir=data_dir)
    day_meals = [r for r in rows if r.get("date") == today]
    totals = daily_totals(day_meals) if day_meals else {"protein_g": 0, "calories": 0}

    remaining = None
    config = _load_config(user_id)
    targets = config.get("targets", {})
    if targets and day_meals:
        cal_target = targets.get("calories_training", targets.get("calories_rest"))
        protein_target = targets.get("protein_g")
        if cal_target and protein_target:
            remaining = remaining_to_hit(day_meals, {"calories": cal_target, "protein_g": protein_target})

    calorie_balance = None
    burn = garmin_snapshot.get("calories_total")
    if burn and burn > 0:
        intake = float(totals.get("calories", 0) or 0)
        calorie_balance = {
            "intake": intake,
            "burn": burn,
            "surplus_deficit": round(intake - burn),
            "status": "surplus" if intake > burn else "deficit",
        }

    return {
        "date": today,
        "garmin": garmin_snapshot,
        "meals": {"items": day_meals, "totals": totals, "remaining": remaining},
        "calorie_balance": calorie_balance,
    }


def _open_dashboard(user_id: str | None = None) -> dict:
    from engine.coaching.briefing import build_briefing

    config = _load_config(user_id)
    if user_id and user_id != "default":
        config["data_dir"] = str(_data_dir(user_id))
    briefing = build_briefing(config)

    data_dir = _data_dir(user_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    briefing_path = data_dir / "briefing.json"
    with open(briefing_path, "w") as f:
        json.dump(briefing, f, indent=2)

    dashboard_path = PROJECT_ROOT / "dashboard" / "index.html"
    if not dashboard_path.exists():
        return {"opened": False, "error": "dashboard/index.html not found"}

    webbrowser.open(f"file://{dashboard_path.resolve()}")
    return {"opened": True, "briefing_refreshed": True}


def _import_apple_health(file_path: str, lookback_days: int = 90, user_id: str | None = None) -> dict:
    from engine.integrations.apple_health import AppleHealthParser

    data_dir = _data_dir(user_id)
    parser = AppleHealthParser(data_dir=str(data_dir))

    expanded = os.path.expanduser(file_path)
    if not os.path.exists(expanded):
        return {
            "imported": False,
            "error": f"File not found: {file_path}",
            "hint": (
                "Make sure the file path is correct. On Mac, you can drag "
                "the file into Terminal to get the full path."
            ),
        }

    try:
        result = parser.parse_export(expanded, lookback_days=lookback_days)
        out_path = parser.save(result)

        metrics_found = [k for k in (
            "resting_hr", "hrv_rmssd_avg", "daily_steps_avg",
            "vo2_max", "sleep_duration_avg", "sleep_regularity_stddev",
        ) if result.get(k) is not None]

        return {
            "imported": True,
            "saved_to": str(out_path),
            "lookback_days": lookback_days,
            "metrics_found": metrics_found,
            "metrics_count": len(metrics_found),
            "data": {k: result[k] for k in metrics_found},
            "note": (
                "Apple Health HRV uses SDNN (not RMSSD). Values may differ "
                "slightly from Garmin. Both are valid for tracking trends."
            ) if "hrv_rmssd_avg" in metrics_found else None,
            "next": "Run checkin or score to see how these metrics affect your health picture.",
        }
    except FileNotFoundError as e:
        return {"imported": False, "error": str(e)}
    except ValueError as e:
        return {
            "imported": False,
            "error": str(e),
            "hint": "Make sure this is an Apple Health export file (ZIP containing export.xml).",
        }
    except Exception as e:
        return {"imported": False, "error": f"Parse error: {e}"}


def _setup_profile(
    age: int,
    sex: str,
    weight_target: float | None = None,
    protein_target: float | None = None,
    family_history: bool | None = None,
    medications: str | None = None,
    waist_inches: float | None = None,
    phq9_score: int | None = None,
    name: str | None = None,
    goals: list[str] | None = None,
    obstacles: str | None = None,
    existing_habits: str | None = None,
    exercise_freq: str | None = None,
    sleep_hours: float | None = None,
    sleep_quality: str | None = None,
    stress_level: str | None = None,
    conditions: list[str] | None = None,
    alcohol_use: str | None = None,
    tobacco_use: str | None = None,
    equipment: list[str] | None = None,
    user_id: str | None = None,
) -> dict:
    cp = _config_path(user_id)
    cp.parent.mkdir(parents=True, exist_ok=True)

    if cp.exists():
        with open(cp) as f:
            config = yaml.safe_load(f) or {}
    else:
        if not user_id or user_id == "default":
            example = PROJECT_ROOT / "config.example.yaml"
            if example.exists():
                with open(example) as f:
                    config = yaml.safe_load(f) or {}
            else:
                config = {}
        else:
            config = {}

    config.setdefault("profile", {})
    config["profile"]["age"] = age
    config["profile"]["sex"] = sex.upper()
    if name is not None:
        config["profile"]["name"] = name
    if family_history is not None:
        config["profile"]["family_history"] = family_history
    if medications is not None:
        config["profile"]["medications"] = medications
    if waist_inches is not None:
        config["profile"]["waist_inches"] = waist_inches
    if phq9_score is not None:
        config["profile"]["phq9_score"] = phq9_score

    config.setdefault("targets", {})
    if weight_target is not None:
        config["targets"]["weight_lbs"] = weight_target
    if protein_target is not None:
        config["targets"]["protein_g"] = protein_target

    intake_fields = {
        "goals": goals,
        "obstacles": obstacles,
        "existing_habits": existing_habits,
        "exercise_freq": exercise_freq,
        "sleep_hours": sleep_hours,
        "sleep_quality": sleep_quality,
        "stress_level": stress_level,
        "conditions": conditions,
        "alcohol_use": alcohol_use,
        "tobacco_use": tobacco_use,
    }

    # Equipment registry
    if equipment is not None:
        config["profile"]["equipment"] = equipment
    has_intake = any(v is not None for v in intake_fields.values())
    if has_intake:
        config.setdefault("intake", {})
        for key, val in intake_fields.items():
            if val is not None:
                config["intake"][key] = val

    with open(cp, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    data_dir = _data_dir(user_id)
    data_dir.mkdir(parents=True, exist_ok=True)

    return {
        "saved": True,
        "config_path": str(cp),
        "profile": config["profile"],
        "targets": config.get("targets", {}),
        "intake": config.get("intake", {}),
    }


def _check_engagement(user_id: str | None = None) -> dict:
    if not user_id:
        return {"error": "user_id is required."}
    data_dir = _data_dir(user_id)
    nudge_path = data_dir / "nudge_state.json"

    has_data = any(
        (data_dir / f).exists()
        for f in ["config.yaml", "weight_log.csv", "meal_log.csv",
                   "daily_habits.csv", "garmin_latest.json", "lab_results.json"]
    )

    if nudge_path.exists():
        with open(nudge_path) as f:
            state = json.load(f)
    else:
        state = {
            "onboarded_at": datetime.now().strftime("%Y-%m-%d"),
            "nudges_sent": [],
            "responded": False,
            "dormant": False,
        }

    if has_data:
        state["responded"] = True
        with open(nudge_path, "w") as f:
            json.dump(state, f, indent=2)
        return {"status": "engaged", "user_id": user_id, "state": state}

    if state.get("dormant"):
        return {"status": "dormant", "user_id": user_id, "state": state,
                "action": "none", "reason": "User marked dormant after Day 7 nudge."}

    onboarded = datetime.strptime(state["onboarded_at"], "%Y-%m-%d")
    days_since = (datetime.now() - onboarded).days
    nudges = state.get("nudges_sent", [])

    if days_since >= 1 and "day1" not in nudges:
        action = "send_day1_nudge"
    elif days_since >= 3 and "day3" not in nudges:
        action = "remind_andrew_day3"
    elif days_since >= 7 and "day7" not in nudges:
        action = "send_day7_nudge"
    elif days_since > 7:
        state["dormant"] = True
        with open(nudge_path, "w") as f:
            json.dump(state, f, indent=2)
        action = "none"
    else:
        action = "wait"

    return {
        "status": "unresponsive",
        "user_id": user_id,
        "days_since_onboarding": days_since,
        "state": state,
        "action": action,
    }


def _log_nudge(user_id: str, nudge_type: str) -> dict:
    data_dir = _data_dir(user_id)
    nudge_path = data_dir / "nudge_state.json"

    if nudge_path.exists():
        with open(nudge_path) as f:
            state = json.load(f)
    else:
        state = {
            "onboarded_at": datetime.now().strftime("%Y-%m-%d"),
            "nudges_sent": [],
            "responded": False,
            "dormant": False,
        }

    if nudge_type not in state.get("nudges_sent", []):
        state.setdefault("nudges_sent", []).append(nudge_type)
        state[f"{nudge_type}_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    with open(nudge_path, "w") as f:
        json.dump(state, f, indent=2)

    return {"ok": True, "user_id": user_id, "state": state}


def _get_user_profile(user_id: str | None = None) -> dict:
    config = _load_config(user_id)
    data_dir = _data_dir(user_id)
    has_data = {}
    for name in ["weight_log.csv", "bp_log.csv", "meal_log.csv", "daily_habits.csv",
                  "garmin_latest.json", "lab_results.json"]:
        has_data[name] = (data_dir / name).exists()
    return {
        "profile": config.get("profile", {}),
        "targets": config.get("targets", {}),
        "intake": config.get("intake", {}),
        "focus": config.get("focus", []),
        "data_dir": str(data_dir),
        "data_available": has_data,
    }


def _log_labs(
    results: dict,
    date: str | None = None,
    source: str | None = None,
    user_id: str | None = None,
) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    source = source or "unknown"
    data_dir = _data_dir(user_id)
    lab_path = data_dir / "lab_results.json"

    normalized = {}
    warnings = []
    for raw_name, value in results.items():
        key = _normalize_lab_key(raw_name)
        try:
            val = float(value)
        except (ValueError, TypeError):
            warnings.append(f"Skipped '{raw_name}': could not parse '{value}' as a number")
            continue
        if key in _LAB_RANGES:
            lo, hi = _LAB_RANGES[key]
            if val < lo or val > hi:
                warnings.append(
                    f"'{raw_name}' ({key}) = {val} is outside expected range [{lo}, {hi}]. "
                    "Stored anyway — confirm with user if this is correct."
                )
        normalized[key] = val

    if not normalized:
        return {"logged": False, "error": "No valid biomarker values to log", "warnings": warnings}

    if lab_path.exists():
        with open(lab_path) as f:
            data = json.load(f)
    else:
        data = {"draws": [], "latest": {}}

    data["draws"].append({
        "date": date,
        "source": source,
        "results": normalized,
    })

    data["draws"].sort(key=lambda d: d.get("date", ""), reverse=True)

    latest: dict[str, float] = {}
    for draw in data["draws"]:
        for key, val in draw.get("results", {}).items():
            if key not in latest:
                latest[key] = val
    data["latest"] = latest
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")

    with open(lab_path, "w") as f:
        json.dump(data, f, indent=2)

    # SQLite write (lab_draw + lab_result)
    import uuid as _uuid
    person_id = _resolve_person_id(user_id)
    if person_id:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        now = datetime.now().isoformat()
        draw_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:lab_draw:{date}:{source}"))
        db.execute(
            "INSERT OR IGNORE INTO lab_draw (id, person_id, date, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (draw_id, person_id, date, source, now, now),
        )
        for marker, val in normalized.items():
            result_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:lab_result:{date}:{marker}"))
            db.execute(
                "INSERT OR REPLACE INTO lab_result (id, draw_id, person_id, marker, value, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result_id, draw_id, person_id, marker, val, now, now),
            )
        db.commit()

    scored = [k for k in normalized if k in _SCORED_FIELDS]
    extra = [k for k in normalized if k not in _SCORED_FIELDS]

    return {
        "logged": True,
        "date": date,
        "source": source,
        "count": len(normalized),
        "biomarkers": list(normalized.keys()),
        "scored_fields": scored,
        "extra_fields": extra,
        "warnings": warnings,
        "total_draws": len(data["draws"]),
        "total_latest": len(data["latest"]),
    }


def _get_labs(user_id: str | None = None) -> dict:
    # Try SQLite first
    _pid = _resolve_person_id(user_id)
    if _pid:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        draws = db.execute(
            "SELECT id, date, source, notes FROM lab_draw WHERE person_id = ? ORDER BY date DESC",
            (_pid,),
        ).fetchall()
        if draws:
            latest = _latest_labs_sqlite(_pid) or {}
            draw_list = []
            for d in draws:
                results = db.execute(
                    "SELECT marker, value, value_text, unit, flag FROM lab_result WHERE draw_id = ?",
                    (d["id"],),
                ).fetchall()
                draw_list.append({
                    "date": d["date"],
                    "source": d["source"],
                    "results": {r["marker"]: r["value"] if r["value"] is not None else r["value_text"] for r in results},
                })
            return {
                "has_labs": True,
                "draws": draw_list,
                "latest": latest,
                "total_draws": len(draw_list),
                "total_biomarkers": len(latest),
            }

    # Fallback to JSON
    data_dir = _data_dir(user_id)
    lab_path = data_dir / "lab_results.json"
    if not lab_path.exists():
        return {"has_labs": False, "draws": [], "latest": {}}
    with open(lab_path) as f:
        data = json.load(f)
    return {
        "has_labs": True,
        "draws": data.get("draws", []),
        "latest": data.get("latest", {}),
        "total_draws": len(data.get("draws", [])),
        "total_biomarkers": len(data.get("latest", {})),
        "last_updated": data.get("last_updated", ""),
    }


# =====================================================================
# Google Calendar tools
# =====================================================================

def _calendar_list_events(
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 10,
    query: str | None = None,
    calendar_id: str = "primary",
    user_id: str | None = None,
) -> dict:
    """List upcoming calendar events."""
    from engine.integrations.gcal import GoogleCalendarClient

    if not user_id:
        return {"error": "user_id is required."}
    client = GoogleCalendarClient(user_id=user_id)
    events = client.list_events(
        time_min=time_min,
        time_max=time_max,
        max_results=max_results,
        query=query,
        calendar_id=calendar_id,
    )
    return {"events": events, "count": len(events)}


def _calendar_create_event(
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    calendar_id: str = "primary",
    user_id: str | None = None,
) -> dict:
    """Create a new calendar event."""
    from engine.integrations.gcal import GoogleCalendarClient

    if not user_id:
        return {"error": "user_id is required."}
    client = GoogleCalendarClient(user_id=user_id)
    event = client.create_event(
        summary=summary,
        start=start,
        end=end,
        description=description,
        location=location,
        calendar_id=calendar_id,
    )
    return {"created": True, "event": event}


def _calendar_search_events(
    query: str,
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 10,
    calendar_id: str = "primary",
    user_id: str | None = None,
) -> dict:
    """Search calendar events by text query."""
    from engine.integrations.gcal import GoogleCalendarClient

    if not user_id:
        return {"error": "user_id is required."}
    client = GoogleCalendarClient(user_id=user_id)
    events = client.search_events(
        query=query,
        time_min=time_min,
        time_max=time_max,
        max_results=max_results,
        calendar_id=calendar_id,
    )
    return {"events": events, "count": len(events)}


def _get_api_stats(days: int = 7, user_id: str | None = None) -> dict:
    """Compute API latency stats and error rates from the audit log."""
    audit_path = PROJECT_ROOT / "data" / "admin" / "api_audit.jsonl"
    if not audit_path.exists():
        return {"error": "No audit log found. API has not been called yet."}

    from collections import defaultdict
    cutoff = datetime.now().timestamp() - (days * 86400)

    tool_stats: dict[str, list] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    timeouts: dict[str, int] = defaultdict(int)
    total = 0

    with open(audit_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            ts_str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str).timestamp()
            except (ValueError, TypeError):
                continue

            if ts < cutoff:
                continue

            # Filter by user_id if specified
            if user_id and entry.get("user_id") != user_id:
                continue

            tool = entry.get("tool", "unknown")
            ms = entry.get("ms")
            status = entry.get("status", "")
            total += 1

            if ms is not None:
                tool_stats[tool].append(ms)

            if status == "error":
                errors[tool] += 1
                error_msg = entry.get("error", "")
                if "timeout" in error_msg.lower() or "abort" in error_msg.lower():
                    timeouts[tool] += 1

    if total == 0:
        return {"message": f"No API calls in the last {days} days.", "days": days}

    # Compute p50, p95, max per tool
    import statistics
    per_tool = {}
    for tool, latencies in sorted(tool_stats.items()):
        latencies.sort()
        n = len(latencies)
        p50 = latencies[n // 2] if n else 0
        p95 = latencies[int(n * 0.95)] if n else 0
        per_tool[tool] = {
            "calls": n,
            "p50_ms": p50,
            "p95_ms": p95,
            "max_ms": max(latencies) if latencies else 0,
            "errors": errors.get(tool, 0),
            "timeouts": timeouts.get(tool, 0),
        }

    # Flag slow tools (p95 > 5000ms)
    slow = [t for t, s in per_tool.items() if s["p95_ms"] > 5000]

    return {
        "days": days,
        "total_calls": total,
        "total_errors": sum(errors.values()),
        "total_timeouts": sum(timeouts.values()),
        "per_tool": per_tool,
        "slow_tools": slow,
    }


def _get_coaching_resource(topic: str) -> dict:
    """Load a coaching resource file on demand.

    Available topics: onboarding, program-engine, self-review
    """
    valid_topics = ["onboarding", "program-engine", "self-review"]
    if topic not in valid_topics:
        return {"error": f"Unknown topic '{topic}'. Available: {', '.join(valid_topics)}"}

    # Look in repo data/ first, then fall back to package-bundled data
    path = PROJECT_ROOT / "data" / "coaching" / f"{topic}.md"
    if not path.exists():
        # Fallback: packaged data inside mcp_server/
        path = Path(__file__).parent / "data" / "coaching" / f"{topic}.md"
    if not path.exists():
        return {"error": f"Resource file not found for topic: {topic}"}

    content = path.read_text()
    return {"topic": topic, "content": content}


def _get_skill_ladder(goal_id: str) -> dict:
    """Return the ranked skill ladder for a goal.

    Each level has: habit, why (evidence), diagnostic question.
    The coach uses this to diagnose where the user is and start at the
    first unmastered level.
    """
    ladders_path = PROJECT_ROOT / "engine" / "coaching" / "skill_ladders.yaml"
    if not ladders_path.exists():
        return {"error": "skill_ladders.yaml not found"}

    with open(ladders_path) as f:
        ladders = yaml.safe_load(f)

    if goal_id not in ladders:
        return {
            "error": f"Unknown goal: {goal_id}",
            "available_goals": list(ladders.keys()),
        }

    ladder = ladders[goal_id]
    return {
        "goal_id": goal_id,
        "name": ladder["name"],
        "outcome": ladder["outcome"],
        "levels": [
            {
                "level": i + 1,
                "habit": level["habit"],
                "why": level["why"],
                "diagnostic": level["diagnostic"],
            }
            for i, level in enumerate(ladder["levels"])
        ],
        "total_levels": len(ladder["levels"]),
        "instructions": (
            "Start at Level 1. Ask the diagnostic question. If the user already "
            "has this habit locked in, move to the next level. The first unmastered "
            "level becomes their 14-day program focus. Use the Arrival Principle: "
            "lead them to name the habit themselves through questions, don't prescribe it."
        ),
    }


def _check_health_priorities_tool(user_id: str | None = None) -> dict:
    """Check all available health data for red-flag conditions.

    Loads labs, BP, and profile info, runs flag checks, and returns
    structured results with severity, coaching language, and goal connections.
    """
    from engine.coaching.health_flags import check_health_priorities

    config = _load_config(user_id)
    data_dir = _data_dir(user_id)
    profile_cfg = config.get("profile", {})
    intake_cfg = config.get("intake", {})

    # Load labs (SQLite first)
    _pid_hp = _resolve_person_id(user_id)
    labs = _latest_labs_sqlite(_pid_hp) or {}
    if not labs:
        lab_path = data_dir / "lab_results.json"
        if lab_path.exists():
            with open(lab_path) as f:
                lab_data = json.load(f)
            labs = lab_data.get("latest", {})

    # Load latest BP
    bp_systolic = None
    bp_diastolic = None
    _pid = _resolve_person_id(user_id)
    bp = _latest_bp_sqlite(_pid)
    if bp:
        bp_systolic, bp_diastolic = bp
    else:
        bp_rows = get_bp(user_id, data_dir)
        if bp_rows:
            try:
                bp_systolic = float(bp_rows[-1]["systolic"])
                bp_diastolic = float(bp_rows[-1]["diastolic"])
            except (KeyError, ValueError):
                pass

    # Determine sex
    sex = profile_cfg.get("sex")

    # Determine current goal
    current_goal = None
    goals = intake_cfg.get("goals")
    if goals:
        if isinstance(goals, list) and goals:
            current_goal = goals[0]
        elif isinstance(goals, str):
            current_goal = goals

    result = check_health_priorities(
        labs=labs,
        bp_systolic=bp_systolic,
        bp_diastolic=bp_diastolic,
        sex=sex,
        current_goal=current_goal,
    )
    return result.to_dict()


# =====================================================================
# Apple Health Shortcut ingest
# =====================================================================

# Valid metric keys that the Shortcut can send
_APPLE_HEALTH_METRICS = {
    "resting_hr", "hrv_sdnn", "steps", "sleep_hours",
    "sleep_start", "sleep_end", "weight_lbs", "vo2_max",
    "blood_oxygen", "active_calories", "respiratory_rate",
}


def _ingest_health_snapshot(
    user_id: str,
    metrics: dict,
    timestamp: str | None = None,
) -> dict:
    """Ingest a daily health snapshot from an iOS Shortcut (Apple Health bridge).

    Accepts a flat dict of metrics from HealthKit, appends to a daily time
    series file, and updates a rolling-average latest file for scoring.

    Args:
        user_id: User identifier (e.g. 'paul')
        metrics: Dict of metric_name -> value. All optional individually.
        timestamp: ISO 8601 timestamp of when the snapshot was taken.

    Returns:
        Summary of what was stored.
    """
    if not user_id:
        return {"ingested": False, "error": "user_id is required"}
    if not metrics or not isinstance(metrics, dict):
        return {"ingested": False, "error": "metrics dict is required and must be non-empty"}

    # Filter to known metrics, skip None values
    clean = {}
    unknown = []
    for k, v in metrics.items():
        if k in _APPLE_HEALTH_METRICS:
            if v is not None:
                clean[k] = v
        else:
            unknown.append(k)

    if not clean:
        return {
            "ingested": False,
            "error": "No valid metrics provided",
            "valid_keys": sorted(_APPLE_HEALTH_METRICS),
        }

    # --- Validation: catch shortcut bugs before they corrupt data ---
    import logging
    _ingest_log = logging.getLogger("kiso.ingest")
    warnings = []
    rejections = []

    # Steps sanity check (yesterday's total should be > 100 for any active person)
    if "steps" in clean:
        try:
            steps_val = float(clean["steps"])
            if steps_val < 50:
                rejections.append(f"steps={steps_val} is impossibly low (shortcut may be querying today's partial, not yesterday's total)")
                del clean["steps"]
            elif steps_val < 500:
                warnings.append(f"steps={steps_val} seems low but accepted")
        except (ValueError, TypeError):
            rejections.append(f"steps={clean['steps']} is not a number")
            del clean["steps"]

    # Sleep hours sanity check
    if "sleep_hours" in clean:
        try:
            sh = float(clean["sleep_hours"])
            if sh < 0 or sh > 16:
                rejections.append(f"sleep_hours={sh} out of range 0-16")
                del clean["sleep_hours"]
        except (ValueError, TypeError):
            rejections.append(f"sleep_hours={clean['sleep_hours']} is not a number")
            del clean["sleep_hours"]

    # Sleep start/end format check (should be HH:MM, not human-readable)
    for field in ("sleep_start", "sleep_end"):
        if field in clean:
            val = str(clean[field])
            # Accept HH:MM format. Reject anything else.
            import re
            if not re.match(r"^\d{1,2}:\d{2}$", val):
                rejections.append(f"{field}='{val}' is not HH:MM format")
                del clean[field]

    # Timestamp freshness check (reject stale replays > 48 hours old)
    if timestamp:
        try:
            ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            age_hours = (datetime.now().astimezone() - ts_dt).total_seconds() / 3600
            if age_hours > 48:
                return {
                    "ingested": False,
                    "error": f"Timestamp is {age_hours:.0f} hours old. Rejecting stale data.",
                    "timestamp": timestamp,
                }
        except (ValueError, TypeError):
            pass

    # Dedup check: skip if same-day entry already exists
    data_dir = _data_dir(user_id)
    daily_path_check = data_dir / "apple_health_daily.json"
    today_date = datetime.now().strftime("%Y-%m-%d")
    if daily_path_check.exists():
        try:
            existing = json.load(open(daily_path_check))
            if isinstance(existing, list):
                today_entries = [e for e in existing if e.get("timestamp", "")[:10] == today_date]
                if today_entries:
                    # Already have data for today. Check if it's been less than 6 hours.
                    last_ts = today_entries[-1].get("timestamp", "")
                    try:
                        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                        hours_since = (datetime.now().astimezone() - last_dt).total_seconds() / 3600
                        if hours_since < 6:
                            return {
                                "ingested": False,
                                "error": f"Already have data from {hours_since:.1f} hours ago today. Skipping duplicate.",
                                "last_entry": last_ts,
                            }
                    except (ValueError, TypeError):
                        pass
        except (json.JSONDecodeError, IOError):
            pass

    if rejections:
        _ingest_log.warning("Ingest validation rejections for %s: %s", user_id, rejections)
    if warnings:
        _ingest_log.info("Ingest validation warnings for %s: %s", user_id, warnings)

    if not clean:
        return {
            "ingested": False,
            "error": "All metrics failed validation",
            "rejections": rejections,
        }

    ts = timestamp or datetime.now().astimezone().isoformat()

    # Build the daily entry
    entry = {"timestamp": ts, **clean}

    # Add HRV method metadata if SDNN was provided
    if "hrv_sdnn" in clean:
        entry["hrv_method"] = "SDNN"

    data_dir = _data_dir(user_id)
    daily_path = data_dir / "apple_health_daily.json"
    latest_path = data_dir / "apple_health_latest.json"

    # --- Append to daily time series ---
    series = []
    if daily_path.exists():
        try:
            with open(daily_path) as f:
                series = json.load(f)
            if not isinstance(series, list):
                series = []
        except (json.JSONDecodeError, IOError):
            series = []

    series.append(entry)

    with open(daily_path, "w") as f:
        json.dump(series, f, indent=2)

    # --- Update latest.json with rolling averages ---
    # Use up to last 7 entries for averages (like garmin_latest.json)
    recent = series[-7:]

    def _rolling_avg(key: str) -> float | None:
        vals = [e[key] for e in recent if key in e and e[key] is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)

    def _latest_val(key: str):
        for e in reversed(recent):
            if key in e and e[key] is not None:
                return e[key]
        return None

    latest = {
        "last_updated": ts,
        "source": "apple_health_shortcut",
        "resting_hr": _rolling_avg("resting_hr"),
        "hrv_rmssd_avg": _rolling_avg("hrv_sdnn"),  # Map SDNN into the scoring field
        "daily_steps_avg": _rolling_avg("steps"),
        "sleep_duration_avg": _rolling_avg("sleep_hours"),
        "vo2_max": _latest_val("vo2_max"),
        "sleep_regularity_stddev": None,  # Can't compute from single daily snapshots
        "zone2_min_per_week": None,  # Not available from Shortcuts
        "metadata": {
            "hrv_method": "SDNN",
            "source_detail": "ios_shortcut",
            "entries_in_series": len(series),
            "rolling_window": len(recent),
        },
    }

    # Add optional metrics that don't map to scoring but are useful
    if _latest_val("blood_oxygen") is not None:
        latest["blood_oxygen"] = _latest_val("blood_oxygen")
    if _latest_val("respiratory_rate") is not None:
        latest["respiratory_rate"] = _latest_val("respiratory_rate")
    if _latest_val("active_calories") is not None:
        latest["active_calories"] = _rolling_avg("active_calories")

    with open(latest_path, "w") as f:
        json.dump(latest, f, indent=2)

    # --- Also log weight if provided ---
    weight_logged = False
    if "weight_lbs" in clean:
        try:
            _log_weight(clean["weight_lbs"], user_id=user_id)
            weight_logged = True
        except Exception:
            pass

    result = {
        "ingested": True,
        "user_id": user_id,
        "metrics_stored": sorted(clean.keys()),
        "metrics_count": len(clean),
        "series_length": len(series),
        "latest_updated": True,
        "weight_logged": weight_logged,
        "timestamp": ts,
    }
    if unknown:
        result["unknown_keys_ignored"] = unknown
    return result


# =====================================================================
# Kasane person context (SQLite + CSV merge)
# =====================================================================

def _get_person_context(person_id: str | None = None, user_id: str | None = None) -> dict:
    """Get unified coaching context for a person: profile, habits, check-ins from SQLite + health metrics from CSVs.

    Look up by person_id (SQLite row id) or user_id (health_engine_user_id).
    Returns merged dict with person profile, active habits, focus plan, and health data.
    """
    from engine.gateway.db import get_db, init_db
    init_db()
    db = get_db()

    # Resolve person
    if person_id:
        row = db.execute(
            "SELECT * FROM person WHERE id = ? AND deleted_at IS NULL", (person_id,)
        ).fetchone()
    elif user_id:
        row = db.execute(
            "SELECT * FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL", (user_id,)
        ).fetchone()
    else:
        return {"error": "Provide person_id or user_id"}

    if not row:
        return {"error": "Person not found", "person_id": person_id, "user_id": user_id}

    person = dict(row)
    pid = person["id"]

    # Active habits with recent check-ins
    habits = []
    habit_rows = db.execute(
        "SELECT * FROM habit WHERE person_id = ? AND deleted_at IS NULL AND state = 'active' ORDER BY sort_order",
        (pid,),
    ).fetchall()
    for h in habit_rows:
        hd = dict(h)
        checkins = db.execute(
            "SELECT * FROM check_in WHERE habit_id = ? AND deleted_at IS NULL "
            "AND date >= date('now', '-30 days') ORDER BY date DESC",
            (hd["id"],),
        ).fetchall()
        hd["recent_checkins"] = [dict(c) for c in checkins]
        habits.append(hd)

    # Latest focus plan
    fp = db.execute(
        "SELECT * FROM focus_plan WHERE person_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (pid,),
    ).fetchone()

    context = {
        "person": person,
        "active_habits": habits,
        "latest_focus_plan": dict(fp) if fp else None,
    }

    # Merge health data (SQLite first, CSV fallback)
    he_uid = person.get("health_engine_user_id")
    health = {}

    # Weight trend (SQLite)
    weight_rows = db.execute(
        "SELECT date, weight_lbs, waist_in, source FROM weight_entry "
        "WHERE person_id = ? ORDER BY date DESC LIMIT 14", (pid,)
    ).fetchall()
    if weight_rows:
        health["weight_recent"] = [dict(r) for r in reversed(weight_rows)]
    elif he_uid:
        csv_rows = get_weights(he_uid, _data_dir(he_uid))
        if csv_rows:
            health["weight_recent"] = csv_rows[-14:]

    # Wearable snapshot (SQLite for latest day, JSON fallback)
    wearable_row = db.execute(
        "SELECT * FROM wearable_daily WHERE person_id = ? ORDER BY date DESC LIMIT 1", (pid,)
    ).fetchone()
    if wearable_row:
        health["wearable_snapshot"] = dict(wearable_row)
        health["wearable_source"] = wearable_row["source"] or "garmin"
    elif he_uid:
        data_dir = _data_dir(he_uid)
        for fname in ("garmin_latest.json", "oura_latest.json", "whoop_latest.json", "apple_health_latest.json"):
            snapshot = _load_json_file(data_dir / fname)
            if snapshot:
                health["wearable_snapshot"] = snapshot
                health["wearable_source"] = fname.replace("_latest.json", "")
                break

    # Labs (SQLite)
    lab_rows = db.execute(
        "SELECT lr.marker, lr.value, lr.value_text, lr.unit, lr.flag, ld.date "
        "FROM lab_result lr JOIN lab_draw ld ON lr.draw_id = ld.id "
        "WHERE lr.person_id = ? ORDER BY ld.date DESC", (pid,)
    ).fetchall()
    if lab_rows:
        # Build latest dict (first occurrence of each marker = most recent)
        latest_labs = {}
        for r in lab_rows:
            if r["marker"] not in latest_labs:
                latest_labs[r["marker"]] = r["value"] if r["value"] is not None else r["value_text"]
        health["latest_labs"] = latest_labs
    elif he_uid:
        labs = _load_json_file(_data_dir(he_uid) / "lab_results.json")
        if labs and "latest" in labs:
            health["latest_labs"] = labs["latest"]

    # Today's meals (SQLite)
    today = datetime.now().strftime("%Y-%m-%d")
    meal_rows = db.execute(
        "SELECT date, meal_num, time_of_day, description, protein_g, carbs_g, fat_g, calories, notes "
        "FROM meal_entry WHERE person_id = ? AND date = ? ORDER BY meal_num", (pid, today)
    ).fetchall()
    if meal_rows:
        health["meals_today"] = [dict(r) for r in meal_rows]
    elif he_uid:
        csv_rows = get_meals(he_uid, data_dir=_data_dir(he_uid))
        if csv_rows:
            health["meals_today"] = [r for r in csv_rows if r.get("date") == today]

    if health:
        context["health"] = health

    # Load coach notes from context.md if present
    if he_uid:
        context_md_path = _data_dir(he_uid) / "context.md"
        if context_md_path.exists():
            try:
                context["coach_notes"] = context_md_path.read_text().strip()
            except Exception:
                pass

    return context




# =====================================================================
# Family summary (Kasane person habits/check-ins digest)
# =====================================================================

# =====================================================================
# Coach task assignment
# =====================================================================

def _log_coach_task(
    user_id: str,
    task_type: str,
    description: str,
    priority: str = "normal",
    context: str | None = None,
) -> dict:
    """Create a task for the human coach to review.

    Used when Milo detects something requiring human judgment:
    compound lab patterns, re-engagement decisions, onboarding review, etc.

    Tasks are stored in data/admin/coach_tasks.json and included in the weekly ops digest.
    """
    tasks_path = PROJECT_ROOT / "data" / "admin" / "coach_tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if tasks_path.exists():
        try:
            with open(tasks_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = []

    import uuid as _uuid
    task = {
        "id": str(_uuid.uuid4())[:8],
        "user_id": user_id,
        "type": task_type,
        "description": description,
        "priority": priority,
        "context": context,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    existing.append(task)

    with open(tasks_path, "w") as f:
        json.dump(existing, f, indent=2)

    return {"created": True, "task_id": task["id"], "type": task_type, "priority": priority}


def _get_coach_tasks(status: str = "pending") -> dict:
    """Get pending coach tasks for the weekly digest."""
    tasks_path = PROJECT_ROOT / "data" / "admin" / "coach_tasks.json"
    if not tasks_path.exists():
        return {"tasks": [], "count": 0}

    with open(tasks_path) as f:
        all_tasks = json.load(f)

    filtered = [t for t in all_tasks if t.get("status") == status]
    return {"tasks": filtered, "count": len(filtered)}


def _complete_coach_task(task_id: str) -> dict:
    """Mark a coach task as completed."""
    tasks_path = PROJECT_ROOT / "data" / "admin" / "coach_tasks.json"
    if not tasks_path.exists():
        return {"error": "No tasks file"}

    with open(tasks_path) as f:
        tasks = json.load(f)

    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = "completed"
            t["completed_at"] = datetime.now().isoformat()
            with open(tasks_path, "w") as f:
                json.dump(tasks, f, indent=2)
            return {"completed": True, "task_id": task_id}

    return {"error": f"Task {task_id} not found"}


# =====================================================================
# Family summary (Kasane person habits/check-ins digest)
# =====================================================================

def _get_family_summary(person_id: str) -> dict:
    """Generate a family-friendly summary of a person's habits, check-ins, and health data.

    Queries the Kasane SQLite database for the given person and returns a structured
    summary including habit statuses, streak info, recent notes, and health metrics.
    Suitable for email digests or proactive coaching conversations.
    """
    from engine.coaching.family_summary import generate_family_summary
    return generate_family_summary(person_id)


# =====================================================================
# Tool registry for HTTP API access
# =====================================================================

TOOL_REGISTRY = {
    "checkin": _checkin,
    "score": _score,
    "get_protocols": _get_protocols,
    "log_weight": _log_weight,
    "log_bp": _log_bp,
    "log_habits": _log_habits,
    "log_supplements": _log_supplements,
    "log_sleep": _log_sleep,
    "log_meal": _log_meal,
    "get_meals": _get_meals,
    "log_medication": _log_medication,
    "get_status": _get_status,
    "onboard": _onboard,
    "log_session": _log_session,
    "pull_garmin": _pull_garmin,
    "connect_garmin": _connect_garmin,
    "connect_wearable": _connect_wearable,
    "connect_google_calendar": _connect_google_calendar,
    "get_daily_snapshot": _get_daily_snapshot,
    "setup_profile": _setup_profile,
    "check_engagement": _check_engagement,
    "log_nudge": _log_nudge,
    "get_user_profile": _get_user_profile,
    "log_labs": _log_labs,
    "get_labs": _get_labs,
    "calendar_list_events": _calendar_list_events,
    "calendar_create_event": _calendar_create_event,
    "calendar_search_events": _calendar_search_events,
    "get_api_stats": _get_api_stats,
    "get_skill_ladder": _get_skill_ladder,
    "import_apple_health": _import_apple_health,
    "check_health_priorities": _check_health_priorities_tool,
    "pull_oura": _pull_oura,
    "connect_oura": _connect_oura,
    "pull_whoop": _pull_whoop,
    "connect_whoop": _connect_whoop,
    "ingest_health_snapshot": _ingest_health_snapshot,
    "get_person_context": _get_person_context,
    "get_family_summary": _get_family_summary,
    "log_coach_task": _log_coach_task,
    "get_coach_tasks": _get_coach_tasks,
    "complete_coach_task": _complete_coach_task,
    # Excluded from HTTP: auth_garmin (interactive), auth_oura (interactive),
    # auth_whoop (interactive), open_dashboard (browser)
}


# =====================================================================
# MCP registration — thin wrappers
# =====================================================================

def register_tools(mcp: FastMCP):
    """Register all Health Engine tools on the given MCP server."""

    @mcp.tool()
    def checkin(greeting: str = "morning check-in", user_id: str | None = None) -> dict:
        """Full health coaching briefing — scores, insights, weight, nutrition, habits, protocols, Garmin data. Call this first when the user asks about their health. Pass a short greeting like 'morning check-in' or 'how am I doing'."""
        return _checkin(greeting, user_id)

    @mcp.tool()
    def score(user_id: str | None = None) -> dict:
        """Get the user's health coverage score. Returns coverage %, NHANES percentiles for 20 metrics, tier breakdown, and ranked gap analysis showing what to measure next. Just call it, no parameters needed. user_id is optional, omit for the local user."""
        return _score(user_id)

    @mcp.tool()
    def get_protocols(user_id: str | None = None) -> list[dict]:
        """Active protocol progress — day, week, phase, last night's habits, nudges, outcomes. Covers sleep stack, nicotine taper, and any other active protocols."""
        return _get_protocols(user_id)

    @mcp.tool()
    def log_weight(weight_lbs: float, date: str | None = None, user_id: str | None = None) -> dict:
        """Log a weight measurement. Date defaults to today."""
        return _log_weight(weight_lbs, date, user_id)

    @mcp.tool()
    def log_bp(systolic: int, diastolic: int, date: str | None = None, user_id: str | None = None) -> dict:
        """Log a blood pressure reading. Date defaults to today."""
        return _log_bp(systolic, diastolic, date, user_id)

    @mcp.tool()
    def log_habits(habits: dict, date: str | None = None, user_id: str | None = None) -> dict:
        """Log daily habits. Pass a dict of habit_name: 'y' or 'n'. Date defaults to today. Habit names must match CSV columns (e.g. am_sunlight, creatine, evening_routine)."""
        return _log_habits(habits, date, user_id)

    @mcp.tool()
    def log_supplements(stack: str | None = None, supplements: list[str] | None = None, date: str | None = None, user_id: str | None = None) -> dict:
        """Log supplement intake. Use stack='morning' or stack='evening' to log a full predefined stack, or pass supplements as a list of individual names (e.g. ['vitamin_d', 'fish_oil']). Date defaults to today."""
        return _log_supplements(stack, supplements, date, user_id)

    @mcp.tool()
    def log_sleep(bed_time: str, wake_time: str, date: str | None = None, user_id: str | None = None) -> dict:
        """Log bed and wake times. Times should be in HH:MM format (e.g. '22:15', '06:10'). Date defaults to today. For bed_time, use the date you went TO bed (not the next morning)."""
        return _log_sleep(bed_time, wake_time, date, user_id)

    @mcp.tool()
    def log_meal(
        description: str,
        protein_g: float,
        carbs_g: float | None = None,
        fat_g: float | None = None,
        calories: float | None = None,
        date: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Log a meal. Estimate protein from the description if the user doesn't give exact numbers. Carbs, fat, calories are optional. Date defaults to today."""
        return _log_meal(description, protein_g, carbs_g, fat_g, calories, date, user_id)

    @mcp.tool()
    def get_meals(
        date: str | None = None,
        days: int = 1,
        user_id: str | None = None,
    ) -> dict:
        """Get meals and Garmin calorie burn for a given date (or last N days).
        Returns meals, totals, remaining macros, and Garmin daily burn (total/active/BMR).
        Compares intake vs burn to show actual surplus/deficit.
        Use this when the user asks about past meals, yesterday's nutrition, weekly intake,
        or calorie balance. Date defaults to today. Set days > 1 for a range."""
        return _get_meals(date, days, user_id)

    @mcp.tool()
    def log_medication(
        name: str,
        dose: str,
        route: str | None = None,
        notes: str | None = None,
        date: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Log a medication or injection (e.g. tirzepatide 2.5mg subcutaneous). Tracks dose changes over time. Route examples: oral, subcutaneous, intramuscular, topical. Date defaults to today."""
        return _log_medication(name, dose, route, notes, date, user_id)


    @mcp.tool()
    def log_session(rpe: float, duration_min: float | None = None, session_type: str = "training", name: str = "", date: str | None = None, user_id: str | None = None) -> dict:
        """Log a training session RPE (1-10 scale). Call after any workout. Merges with Garmin data for ACWR computation. If Garmin captured the workout, you only need the RPE. Duration is optional (Garmin provides it)."""
        return _log_session(rpe, duration_min, session_type, name, date, user_id)

    @mcp.tool()
    def get_status(user_id: str | None = None) -> dict:
        """Data files inventory — what exists, last modified, row counts. Useful for understanding what data the user has."""
        return _get_status(user_id)

    @mcp.tool()
    def onboard(user_id: str | None = None) -> dict:
        """IMPORTANT: Call this FIRST when a new user interacts with you, or when someone says 'what should I measure?', 'set me up', or 'what can you do?'. Returns all 20 health metrics, what's tracked vs missing, and ranked next steps by leverage. After calling this, call get_coaching_resource('onboarding') to load the full coaching conversation flow."""
        return _onboard(user_id)

    @mcp.tool()
    def auth_garmin(user_id: str | None = None) -> dict:
        """Authenticate with Garmin Connect via a secure browser form. Opens your browser — you type credentials there, never in chat. Credentials are used once to obtain tokens and are NOT stored."""
        return _auth_garmin(user_id)

    @mcp.tool()
    def pull_garmin(history: bool = False, workouts: bool = False, user_id: str | None = None) -> dict:
        """Pull fresh data from Garmin Connect. Returns latest metrics and optionally 90-day history + workout details. Requires auth_garmin first if tokens are expired."""
        return _pull_garmin(history, workouts, user_id)

    @mcp.tool()
    def connect_garmin(user_id: str | None = None) -> dict:
        """Check Garmin connection status — whether tokens are cached, data freshness, and hints for next steps."""
        return _connect_garmin(user_id)

    @mcp.tool()
    def auth_oura(user_id: str | None = None) -> dict:
        """Authenticate with Oura Ring via OAuth. Opens your browser for authorization. Requires oura.client_id and oura.client_secret in gateway.yaml."""
        return _auth_oura(user_id)

    @mcp.tool()
    def pull_oura(history: bool = False, user_id: str | None = None) -> dict:
        """Pull fresh data from Oura Ring. Returns latest metrics (RHR, HRV, sleep, steps) and optionally 90-day history. Requires auth_oura first if tokens are expired."""
        return _pull_oura(history, user_id)

    @mcp.tool()
    def connect_oura(user_id: str | None = None) -> dict:
        """Check Oura Ring connection status — whether tokens are cached, data freshness, and hints for next steps."""
        return _connect_oura(user_id)

    @mcp.tool()
    def auth_whoop(user_id: str | None = None) -> dict:
        """Authenticate with WHOOP via OAuth. Opens your browser for authorization. Requires whoop.client_id and whoop.client_secret in gateway.yaml."""
        return _auth_whoop(user_id)

    @mcp.tool()
    def pull_whoop(history: bool = False, user_id: str | None = None) -> dict:
        """Pull fresh data from WHOOP. Returns latest metrics (RHR, HRV, sleep, recovery) and optionally 90-day history. Requires auth_whoop first if tokens are expired."""
        return _pull_whoop(history, user_id)

    @mcp.tool()
    def connect_whoop(user_id: str | None = None) -> dict:
        """Check WHOOP connection status — whether tokens are cached, data freshness, and hints for next steps."""
        return _connect_whoop(user_id)

    @mcp.tool()
    def connect_wearable(service: str, user_id: str | None = None) -> dict:
        """Get connection instructions for a wearable device.
        For OAuth services (garmin, oura, whoop): returns a tappable auth link.
        For Apple Health/Apple Watch: returns iOS Shortcuts setup instructions.

        Args:
            service: Wearable service name (garmin, oura, whoop, apple_health, apple_watch, apple)
            user_id: User identifier for multi-user support
        """
        return _connect_wearable(service, user_id)

    @mcp.tool()
    def connect_google_calendar(user_id: str | None = None) -> dict:
        """Get a tappable OAuth link for connecting Google Calendar.
        The user opens this link on their phone, authorizes with Google,
        and tokens are saved automatically. No credentials touch our server.

        Args:
            user_id: User identifier for multi-user support
        """
        return _connect_google_calendar(user_id)

    @mcp.tool()
    def get_daily_snapshot(user_id: str | None = None) -> dict:
        """Get a live snapshot of today's wearable data (steps, calories burned, body battery,
        stress, heart rate) alongside today's meals and calorie balance.
        Pulls fresh data from Garmin on each call (~15s). Use when the user asks
        'how's my day going', 'what's my burn so far', or 'how much can I still eat'."""
        return _get_daily_snapshot(user_id)

    @mcp.tool()
    def open_dashboard(user_id: str | None = None) -> dict:
        """Open the health dashboard in a browser. Refreshes briefing data first."""
        return _open_dashboard(user_id)

    @mcp.tool()
    def import_apple_health(file_path: str, lookback_days: int = 90, user_id: str | None = None) -> dict:
        """Import Apple Health data from an export ZIP or XML file.

        Parses RHR, HRV (SDNN), steps, VO2 max, and sleep data using streaming
        SAX parser (handles large exports without memory issues).

        HOW TO EXPORT from iPhone:
        1. Open the Health app (or Settings → Health)
        2. Tap your profile picture (top right)
        3. Scroll down → "Export All Health Data"
        4. Tap "Export" — this creates a ZIP file
        5. AirDrop, email, or transfer the ZIP to your computer
        6. Call this tool with the file path

        Args:
            file_path: Path to the export.zip (or export.xml) on your machine
            lookback_days: How many days of history to include (default 90)
        """
        return _import_apple_health(file_path, lookback_days, user_id)

    @mcp.tool()
    def setup_profile(
        age: int,
        sex: str,
        weight_target: float | None = None,
        protein_target: float | None = None,
        family_history: bool | None = None,
        medications: str | None = None,
        waist_inches: float | None = None,
        phq9_score: int | None = None,
        name: str | None = None,
        goals: list[str] | None = None,
        obstacles: str | None = None,
        existing_habits: str | None = None,
        exercise_freq: str | None = None,
        sleep_hours: float | None = None,
        sleep_quality: str | None = None,
        stress_level: str | None = None,
        conditions: list[str] | None = None,
        alcohol_use: str | None = None,
        tobacco_use: str | None = None,
        equipment: list[str] | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Save user profile info: age, sex, goals, weight target, conditions. Call this when the user shares personal health details. Sex = 'M' or 'F'. You can call this incrementally as info is shared. user_id is optional, omit for the local user."""
        return _setup_profile(
            age, sex, weight_target, protein_target, family_history, medications,
            waist_inches, phq9_score, name, goals, obstacles, existing_habits,
            exercise_freq, sleep_hours, sleep_quality, stress_level, conditions,
            alcohol_use, tobacco_use, equipment, user_id,
        )

    @mcp.tool()
    def check_engagement(user_id: str | None = None) -> dict:
        """Check if a user has engaged after onboarding. Returns engagement status,
        days since onboarding, nudge history, and recommended next action.
        Used by the follow-up nudge system to decide what to send."""
        return _check_engagement(user_id)

    @mcp.tool()
    def log_nudge(user_id: str, nudge_type: str) -> dict:
        """Record that a nudge was sent to a user. nudge_type should be 'day1', 'day3', or 'day7'."""
        return _log_nudge(user_id, nudge_type)

    @mcp.tool()
    def get_user_profile(user_id: str | None = None) -> dict:
        """Read the user's saved profile: age, sex, goals, targets, conditions. Call this to check what you already know about someone before asking them questions you might already have answers to. user_id is optional, omit for the local user."""
        return _get_user_profile(user_id)

    @mcp.tool()
    def log_labs(
        results: dict,
        date: str | None = None,
        source: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Log lab results (biomarker key-value pairs) from any provider. Names are normalized automatically — 'Apo B', 'apolipoprotein b', and 'apob' all work. Date defaults to today. Source is optional (e.g. 'Quest', 'Function Health', 'LabCorp')."""
        return _log_labs(results, date, source, user_id)

    @mcp.tool()
    def get_labs(user_id: str | None = None) -> dict:
        """Retrieve full lab history — all draws with dates, sources, results, and the computed latest values. Use this to check what labs are on file, compare across draws, and identify gaps."""
        return _get_labs(user_id)

    @mcp.tool()
    def calendar_list_events(
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 10,
        query: str | None = None,
        calendar_id: str = "primary",
        user_id: str | None = None,
    ) -> dict:
        """List upcoming Google Calendar events. Returns events with title, start/end times, location, and description. Defaults to upcoming events from now. Use time_min/time_max (ISO 8601) to filter a date range. Use calendar_id to target a specific calendar (default 'primary')."""
        return _calendar_list_events(time_min, time_max, max_results, query, calendar_id, user_id)

    @mcp.tool()
    def calendar_create_event(
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str = "primary",
        user_id: str | None = None,
    ) -> dict:
        """Create a Google Calendar event. Start/end can be ISO 8601 datetime (e.g. '2026-06-26T09:00:00') for timed events or YYYY-MM-DD for all-day events. Use calendar_id to target a specific calendar (default 'primary'). For training events, use the Health calendar."""
        return _calendar_create_event(summary, start, end, description, location, calendar_id, user_id)

    @mcp.tool()
    def calendar_search_events(
        query: str,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 10,
        calendar_id: str = "primary",
        user_id: str | None = None,
    ) -> dict:
        """Search Google Calendar events by text. Searches event titles, descriptions, locations, and attendees. Use to find specific events like 'lab retest' or 'training'. Use calendar_id to target a specific calendar."""
        return _calendar_search_events(query, time_min, time_max, max_results, calendar_id, user_id)

    @mcp.tool()
    def get_api_stats(days: int = 7, user_id: str | None = None) -> dict:
        """API latency and error report. Shows p50/p95/max latency per tool, error counts, timeout counts, and flags slow tools (>5s p95). Use for debugging performance issues and monitoring system health."""
        return _get_api_stats(days, user_id)

    @mcp.tool()
    def get_skill_ladder(goal_id: str) -> dict:
        """Get the habit progression for a specific goal. Returns levels ranked by impact: each level has a habit, evidence, and a diagnostic question to ask the user. Use this after the user picks a goal to find their starting level. Valid: sleep-better, less-stress, lose-weight, build-strength, more-energy, sharper-focus, better-mood, eat-healthier."""
        return _get_skill_ladder(goal_id)

    @mcp.tool()
    def get_coaching_resource(topic: str) -> dict:
        """Load coaching methodology and conversation flows. MUST call this to know how to coach properly. Topics: 'onboarding' (the 5-message new user flow with goal clusters and habit programs), 'program-engine' (14-day focused blocks, skill ladders), 'self-review' (weekly reflection). Call get_coaching_resource('onboarding') after onboard() to learn the full coaching conversation flow."""
        return _get_coaching_resource(topic)

    @mcp.tool()
    def check_health_priorities(user_id: str | None = None) -> dict:
        """Run a health priority checkpoint. Scans all available health data (labs, BP, wearable metrics) for red-flag conditions like pre-diabetic glucose, thyroid abnormalities, high blood pressure, low testosterone, elevated LDL, and more. Returns flags with severity (urgent/notable), coaching-appropriate language, and connections to the user's current goal. Call this after new lab results arrive or during periodic reviews to catch findings that may override the user's chosen coaching focus."""
        return _check_health_priorities_tool(user_id)

    @mcp.tool()
    def ingest_health_snapshot(user_id: str, metrics: dict, timestamp: str | None = None) -> dict:
        """Ingest a daily health snapshot from an iOS Shortcut (Apple Health bridge). Accepts a flat dict of metric values from HealthKit. Valid keys: resting_hr, hrv_sdnn, steps, sleep_hours, sleep_start, sleep_end, weight_lbs, vo2_max, blood_oxygen, active_calories, respiratory_rate. All metrics optional individually. Appends to daily series and updates rolling averages for scoring."""
        return _ingest_health_snapshot(user_id, metrics, timestamp)

    @mcp.tool()
    def get_person_context(person_id: str | None = None, user_id: str | None = None) -> dict:
        """Get unified coaching context for a person: profile, habits, check-ins from Kasane + health metrics from CSVs. Look up by person_id (Kasane UUID) or user_id (health-engine user like 'default'). Returns merged dict for full coaching context."""
        return _get_person_context(person_id, user_id)



    @mcp.tool()
    def get_family_summary(person_id: str) -> dict:
        """Get a daily digest summary of a person's habits, check-ins, streaks, and health data. Use for family members who want updates on their loved one's progress. Pass the person_id (Kasane UUID) to generate the summary."""
        return _get_family_summary(person_id)

    @mcp.tool()
    def log_coach_task(user_id: str, task_type: str, description: str, priority: str = "normal", context: str | None = None) -> dict:
        """Create a task for the human coach. Use when you detect a compound lab pattern you're uncertain about, a user needs re-engagement review, or any situation where human judgment adds value. Types: lab_review, re_engagement, onboarding_review, compound_pattern, custom. Priorities: low, normal, high."""
        return _log_coach_task(user_id, task_type, description, priority, context)

    @mcp.tool()
    def get_coach_tasks(status: str = "pending") -> dict:
        """Get pending coach tasks. Used by the ops agent for weekly digests and by Andrew to review what needs attention."""
        return _get_coach_tasks(status)

    @mcp.tool()
    def complete_coach_task(task_id: str) -> dict:
        """Mark a coach task as completed. Andrew calls this after reviewing and acting on a task."""
        return _complete_coach_task(task_id)

    @mcp.tool()
    def save_coaching_message(person_id: str, message_text: str, habit_id: str | None = None, message_type: str = "coaching", user_id: str | None = None) -> dict:
        """Save a coaching message to kasane.db so it syncs to the Kasane iOS app. Call this after sending a coaching response to ensure the user sees it in-app too. person_id is the Kasane UUID."""
        return _save_coaching_message(person_id, message_text, habit_id, message_type, user_id)

def register_resources(mcp: FastMCP):
    """Register MCP resources (readable documents)."""

    @mcp.resource("health-engine://methodology")
    def methodology() -> str:
        """Full scoring methodology — why each metric is measured, evidence sources, clinical thresholds, freshness decay, reliability multipliers."""
        methodology_path = PROJECT_ROOT / "docs" / "METHODOLOGY.md"
        if not methodology_path.exists():
            methodology_path = Path(__file__).parent.parent / "engine" / "data" / "METHODOLOGY.md"
        if not methodology_path.exists():
            return "METHODOLOGY.md not found."
        return methodology_path.read_text()


def _save_coaching_message(
    person_id: str,
    message_text: str,
    habit_id: str | None = None,
    message_type: str = "coaching",
    user_id: str | None = None,
) -> dict:
    """Save a coaching message to kasane.db so it syncs to the iOS app."""
    import uuid
    from datetime import datetime, timezone

    db_path = Path(__file__).parent.parent / 'data' / 'kasane.db'
    if not db_path.exists():
        return {'error': 'kasane.db not found'}

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        'INSERT INTO check_in_message (id, person_id, habit_id, message_text, message_type, action_type, created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (msg_id, person_id, habit_id, message_text, message_type, 'coaching', now, now)
    )
    conn.commit()
    conn.close()

    return {
        'status': 'saved',
        'message_id': msg_id,
        'person_id': person_id,
        'syncs_to_ios': True,
    }
