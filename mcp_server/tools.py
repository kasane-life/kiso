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
from engine.utils.csv_io import read_csv, write_csv

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

    config = _load_config(user_id)
    if user_id and user_id != "default":
        config["data_dir"] = str(_data_dir(user_id))
    return build_briefing(config)


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

    bp_rows = read_csv(data_dir / "bp_log.csv")
    if bp_rows:
        profile.systolic = float(bp_rows[-1]["systolic"])
        profile.diastolic = float(bp_rows[-1]["diastolic"])

    weight_rows = read_csv(data_dir / "weight_log.csv")
    if weight_rows and weight_rows[-1].get("weight_lbs"):
        profile.weight_lbs = float(weight_rows[-1]["weight_lbs"])

    # Load lab results for scoring + clinical zones
    metric_dates = {}
    metric_counts = {}
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
    habit_data = read_csv(data_dir / "daily_habits.csv") or None

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
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "weight_log.csv"
    rows = read_csv(path)
    fieldnames = ["date", "weight_lbs", "source", "waist_in"]
    rows.append({"date": date, "weight_lbs": str(weight_lbs), "source": "mcp", "waist_in": ""})
    write_csv(path, rows, fieldnames=fieldnames)
    return {"logged": True, "date": date, "weight_lbs": weight_lbs}


def _log_bp(systolic: int, diastolic: int, date: str | None = None, user_id: str | None = None) -> dict:
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "bp_log.csv"
    rows = read_csv(path)
    fieldnames = ["date", "systolic", "diastolic", "source"]
    rows.append({"date": date, "systolic": str(systolic), "diastolic": str(diastolic), "source": "mcp"})
    write_csv(path, rows, fieldnames=fieldnames)
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

    rows.append({
        "date": date,
        "meal_num": str(meal_num),
        "time_of_day": time_of_day,
        "description": description,
        "protein_g": str(protein_g),
        "carbs_g": str(carbs_g) if carbs_g is not None else "",
        "fat_g": str(fat_g) if fat_g is not None else "",
        "calories": str(calories) if calories is not None else "",
        "notes": "",
    })
    write_csv(path, rows, fieldnames=fieldnames)
    return {"logged": True, "date": date, "meal_num": meal_num, "description": description, "protein_g": protein_g}


def _get_meals(
    date: str | None = None,
    days: int = 1,
    user_id: str | None = None,
) -> dict:
    from engine.tracking.nutrition import daily_totals, remaining_to_hit
    date = date or datetime.now().strftime("%Y-%m-%d")
    data_dir = _data_dir(user_id)
    path = data_dir / "meal_log.csv"
    rows = read_csv(path)

    burn_by_date = {}
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
            day_result = {"meals": day_meals, "totals": totals}

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
                gc_config = _load_config(user_id)
                gc = GarminClient.from_config(gc_config)
                gc.data_dir = data_dir
                live = gc.pull_today()
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

    bp_rows = read_csv(data_dir / "bp_log.csv")
    if bp_rows:
        profile.systolic = float(bp_rows[-1]["systolic"])
        profile.diastolic = float(bp_rows[-1]["diastolic"])

    weight_rows = read_csv(data_dir / "weight_log.csv")
    if weight_rows and weight_rows[-1].get("weight_lbs"):
        profile.weight_lbs = float(weight_rows[-1]["weight_lbs"])

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
    garmin_cfg = config.get("garmin", {})
    garmin_token_dir = garmin_cfg.get("token_dir")
    garmin_tokens = GarminClient.has_tokens(token_dir=garmin_token_dir)
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

    config = _load_config(user_id)
    garmin_cfg = config.get("garmin", {})
    token_dir = garmin_cfg.get("token_dir", os.path.expanduser("~/.config/health-engine/garmin-tokens"))
    return run_auth_flow(token_dir=token_dir)


def _pull_garmin(history: bool = False, workouts: bool = False, user_id: str | None = None) -> dict:
    from engine.integrations.garmin import GarminClient

    config = _load_config(user_id)
    if user_id and user_id != "default":
        config["data_dir"] = str(_data_dir(user_id))
    try:
        client = GarminClient.from_config(config)
        result = client.pull_all(
            history=history,
            history_days=90,
            workouts=workouts,
            workout_days=7,
        )
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

    config = _load_config(user_id)
    garmin_cfg = config.get("garmin", {})
    token_dir = garmin_cfg.get("token_dir")
    has_tokens = GarminClient.has_tokens(token_dir=token_dir)

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


def _connect_wearable(service: str, user_id: str = "default") -> dict:
    supported = ["garmin"]
    if service not in supported:
        return {
            "error": f"Unsupported service: {service}. Supported: {', '.join(supported)}",
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


def _connect_google_calendar(user_id: str = "default") -> dict:
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

    path = data_dir / "meal_log.csv"
    rows = read_csv(path)
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


def _check_engagement(user_id: str = "default") -> dict:
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

    client = GoogleCalendarClient(user_id=user_id or "default")
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

    client = GoogleCalendarClient(user_id=user_id or "default")
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

    client = GoogleCalendarClient(user_id=user_id or "default")
    events = client.search_events(
        query=query,
        time_min=time_min,
        time_max=time_max,
        max_results=max_results,
        calendar_id=calendar_id,
    )
    return {"events": events, "count": len(events)}


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
    # Excluded from HTTP: auth_garmin (interactive), open_dashboard (browser),
    # import_apple_health (file path)
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
        """Scoring engine deep-dive: coverage %, NHANES percentiles for 20 metrics, tier breakdown, and ranked gap analysis showing what to measure next."""
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
        """Log a meal. Protein is required; carbs, fat, calories are optional. Date defaults to today."""
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
    def get_status(user_id: str | None = None) -> dict:
        """Data files inventory — what exists, last modified, row counts. Useful for understanding what data the user has."""
        return _get_status(user_id)

    @mcp.tool()
    def onboard(user_id: str | None = None) -> dict:
        """Coverage map and guided setup. Shows all 20 health metrics,
        what's tracked vs missing, and ranked next steps by leverage.
        Call for new users or when someone asks 'what should I measure?'"""
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
    def connect_wearable(service: str, user_id: str = "default") -> dict:
        """Get a tappable auth link for connecting a wearable device.
        The user opens this link on their phone to sign in.
        Currently supports: garmin. Future: oura, whoop.

        Args:
            service: Wearable service name (garmin, oura, whoop)
            user_id: User identifier for multi-user support
        """
        return _connect_wearable(service, user_id)

    @mcp.tool()
    def connect_google_calendar(user_id: str = "default") -> dict:
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
        user_id: str | None = None,
    ) -> dict:
        """Create or update config.yaml with user profile. Sex should be 'M' or 'F'. Weight target in lbs, protein in grams."""
        return _setup_profile(
            age, sex, weight_target, protein_target, family_history, medications,
            waist_inches, phq9_score, name, goals, obstacles, existing_habits,
            exercise_freq, sleep_hours, sleep_quality, stress_level, conditions,
            alcohol_use, tobacco_use, user_id,
        )

    @mcp.tool()
    def check_engagement(user_id: str = "default") -> dict:
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
        """Retrieve full user profile including intake data, targets, and active protocols. Useful for understanding a user's context before coaching."""
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
