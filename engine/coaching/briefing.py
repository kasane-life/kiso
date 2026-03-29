"""Briefing assembly — gathers all available data into a single coaching snapshot.

This is the data layer for AI coaching. One call produces everything Claude
(or any LLM) needs to assess where the user stands and coach them forward.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from engine.models import Demographics, UserProfile
from engine.scoring.engine import score_profile
from engine.insights.engine import generate_insights, load_rules
from engine.insights.coaching import (
    assess_sleep_debt, assess_deficit_impact, assess_taper_readiness,
    assess_sleep_deficit_interaction, assess_nutrition_deviation,
)
from engine.insights.patterns import detect_patterns, summarize_patterns
from engine.tracking.weight import rolling_average, weekly_rate, projected_date, rate_assessment
from engine.scoring.rolling import compute_rolling, compute_rolling_from_csv, compute_protein_rolling
from engine.scoring.alerts import check_alerts
from engine.scoring.acwr import compute_acwr, build_session_list, acwr_alert
from engine.scoring.conditions import (
    get_user_conditions, enrich_alerts_with_conditions,
    get_condition_primary_metrics, get_condition_retest_overrides,
    get_condition_doctor_triggers,
)
from engine.scoring.lab_trends import compute_lab_trends
from engine.scoring.disclosure import (
    get_tenure_days, get_tenure_tier, resolve_outcome,
    filter_horizons, filter_alerts,
)
from engine.tracking.nutrition import remaining_to_hit, daily_totals, protein_check
from engine.tracking.strength import est_1rm, progression_summary
from engine.tracking.habits import streak, gap_analysis
from engine.utils.csv_io import read_csv


def build_briefing(config: dict) -> dict:
    """
    Assemble a complete health briefing from all available data.

    Reads config, data files, and runs scoring + insights to produce
    a single structured snapshot suitable for LLM coaching.

    Args:
        config: Parsed config.yaml dict

    Returns:
        Dict with sections: meta, score, insights, weight, nutrition,
        strength, habits, garmin, coaching, gaps
    """
    raw_data_dir = Path(config.get("data_dir", "./data"))
    if raw_data_dir.is_absolute():
        data_dir = raw_data_dir
    else:
        data_dir = (Path(__file__).parent.parent.parent / raw_data_dir).resolve()
    profile_cfg = config.get("profile", {})
    targets = config.get("targets", {})
    today = datetime.now().strftime("%Y-%m-%d")

    briefing = {
        "as_of": today,
        "data_available": {},
    }

    # --- Wearable data (priority: garmin > oura > whoop > apple_health) ---
    garmin = _load_json(data_dir / "garmin_latest.json")
    oura = _load_json(data_dir / "oura_latest.json")
    whoop = _load_json(data_dir / "whoop_latest.json")
    apple_health = _load_json(data_dir / "apple_health_latest.json")
    garmin_daily = _load_json(data_dir / "garmin_daily.json")
    oura_daily = _load_json(data_dir / "oura_daily.json")
    whoop_daily = _load_json(data_dir / "whoop_daily.json")
    briefing["data_available"]["garmin"] = garmin is not None
    briefing["data_available"]["oura"] = oura is not None
    briefing["data_available"]["whoop"] = whoop is not None
    briefing["data_available"]["apple_health"] = apple_health is not None
    briefing["data_available"]["garmin_daily"] = garmin_daily is not None
    briefing["data_available"]["oura_daily"] = oura_daily is not None
    briefing["data_available"]["whoop_daily"] = whoop_daily is not None

    # Use Garmin if available, then Oura, then WHOOP, then Apple Health
    wearable = garmin or oura or whoop or apple_health
    if garmin:
        wearable_source = "garmin"
    elif oura:
        wearable_source = "oura"
    elif whoop:
        wearable_source = "whoop"
    elif apple_health:
        wearable_source = "apple_health"
    else:
        wearable_source = None

    # --- Daily burn (Garmin TDEE data) ---
    daily_burn = _load_json(data_dir / "garmin_daily_burn.json")
    briefing["data_available"]["garmin_daily_burn"] = daily_burn is not None

    if wearable:
        briefing["wearable_source"] = wearable_source
        briefing["garmin"] = {
            "last_updated": wearable.get("last_updated"),
            "hrv_rmssd_avg": wearable.get("hrv_rmssd_avg"),
            "resting_hr": wearable.get("resting_hr"),
            "sleep_duration_avg": wearable.get("sleep_duration_avg"),
            "sleep_regularity_stddev": wearable.get("sleep_regularity_stddev"),
            "vo2_max": wearable.get("vo2_max"),
            "daily_steps_avg": wearable.get("daily_steps_avg"),
            "zone2_min_per_week": wearable.get("zone2_min_per_week"),
        }

    # Add daily burn / TDEE to briefing
    if daily_burn and isinstance(daily_burn, list):
        today_burn = next((d for d in daily_burn if d.get("date") == today), None)
        burn_section = {
            "today": today_burn,
            "recent": daily_burn,
        }
        # Compute 7-day average TDEE (excluding today if partial)
        completed_days = [d for d in daily_burn if d.get("date") != today and d.get("total")]
        if completed_days:
            burn_section["avg_tdee_7d"] = round(
                sum(d["total"] for d in completed_days) / len(completed_days)
            )
        briefing["daily_burn"] = burn_section

    # Add today's daily snapshot from daily series (garmin or oura or whoop)
    daily_series = garmin_daily or oura_daily or whoop_daily
    if daily_series and isinstance(daily_series, list):
        today_daily = next((d for d in daily_series if d.get("date") == today), None)
        if not today_daily:
            # Fall back to most recent day
            today_daily = daily_series[-1] if daily_series else None
        if today_daily:
            briefing["today_snapshot"] = today_daily

    # --- Score ---
    demo = Demographics(
        age=profile_cfg.get("age", 35),
        sex=profile_cfg.get("sex", "M"),
    )
    profile = UserProfile(demographics=demo)

    if wearable:
        profile.resting_hr = wearable.get("resting_hr")
        profile.daily_steps_avg = wearable.get("daily_steps_avg")
        profile.sleep_regularity_stddev = wearable.get("sleep_regularity_stddev")
        profile.sleep_duration_avg = wearable.get("sleep_duration_avg")
        profile.vo2_max = wearable.get("vo2_max")
        profile.hrv_rmssd_avg = wearable.get("hrv_rmssd_avg")
        profile.zone2_min_per_week = wearable.get("zone2_min_per_week")

    # Incorporate latest BP reading into profile for scoring
    bp_data_for_score = _load_bp_log(data_dir)
    if bp_data_for_score and len(bp_data_for_score) > 0:
        latest_bp = bp_data_for_score[-1]
        profile.systolic = latest_bp["sys"]
        profile.diastolic = latest_bp["dia"]

    # Incorporate latest weight into profile
    weights_for_score = _load_weight_log(data_dir)
    if weights_for_score:
        profile.weight_lbs = weights_for_score[-1]["weight"]

    # Incorporate lab results into profile for scoring
    labs = _load_lab_results(data_dir)
    briefing["data_available"]["lab_results"] = labs is not None
    if labs:
        latest = labs.get("latest", {})
        lab_field_map = {
            "ldl_c": "ldl_c",
            "hdl_c": "hdl_c",
            "total_cholesterol": "total_cholesterol",
            "triglycerides": "triglycerides",
            "apob": "apob",
            "fasting_glucose": "fasting_glucose",
            "hba1c": "hba1c",
            "fasting_insulin": "fasting_insulin",
            "hscrp": "hscrp",
            "ast": "ast",
            "alt": "alt",
            "ggt": "ggt",
            "tsh": "tsh",
            "ferritin": "ferritin",
            "hemoglobin": "hemoglobin",
            "wbc": "wbc",
            "platelets": "platelets",
            "lpa": "lpa",
        }
        for lab_key, profile_attr in lab_field_map.items():
            val = latest.get(lab_key)
            if val is not None:
                setattr(profile, profile_attr, val)
        briefing["labs"] = {
            "last_draw": labs.get("draws", [{}])[0].get("date") if labs.get("draws") else None,
            "markers_available": len(latest),
        }

        # Lab trends: compare across draws (Phase 5)
        lab_trend_data = compute_lab_trends(labs)
        if lab_trend_data:
            briefing["lab_trends"] = {
                "significant_changes": lab_trend_data.get("significant_changes", []),
                "retest_due": lab_trend_data.get("retest_due", []),
                "total_markers": lab_trend_data.get("total_markers", 0),
                "total_draws": lab_trend_data.get("total_draws", 0),
            }

    # Build metric dates and counts for freshness/reliability
    metric_dates = {}
    metric_counts = {}
    if labs:
        metric_dates.update(_extract_lab_dates(labs))
        metric_counts.update(_count_lab_readings(labs))
    if wearable:
        wearable_date = wearable.get("last_updated", "")[:10]  # ISO date portion
        if wearable_date:
            for key in ("resting_hr", "daily_steps_avg", "sleep_regularity_stddev",
                        "sleep_duration_avg", "vo2_max", "hrv_rmssd_avg", "zone2_min_per_week"):
                metric_dates[key] = wearable_date
    if bp_data_for_score:
        bp_rows_raw = read_csv(data_dir / "bp_log.csv")
        if bp_rows_raw:
            metric_dates["bp_single"] = bp_rows_raw[-1].get("date", "")
            metric_dates["bp_protocol"] = bp_rows_raw[-1].get("date", "")
            # Count BP readings in last 7 days
            bp_count = _count_recent_readings(bp_rows_raw, 7)
            metric_counts["bp"] = bp_count
    if weights_for_score:
        weight_rows_raw = read_csv(data_dir / "weight_log.csv")
        if weight_rows_raw:
            metric_dates["weight_lbs"] = weight_rows_raw[-1].get("date", "")

    score_output = score_profile(profile, metric_dates=metric_dates,
                                 metric_counts=metric_counts)
    briefing["score"] = {
        "coverage": score_output["coverage_score"],
        "avg_percentile": score_output["avg_percentile"],
        "tier1_pct": score_output["tier1_pct"],
        "tier2_pct": score_output["tier2_pct"],
        "results": [r.to_dict() for r in score_output["results"] if r.has_data],
        "gap_count": len(score_output["gaps"]),
        "top_gaps": [
            {"name": g.name, "weight": g.coverage_weight, "cost": g.cost_to_close}
            for g in score_output["gaps"][:5]
        ],
    }

    # --- Insights ---
    weights_data = _load_weight_log(data_dir)
    bp_data = _load_bp_log(data_dir)
    trends = _build_trends(garmin_daily or oura_daily or whoop_daily)
    briefing["data_available"]["weight_log"] = weights_data is not None
    briefing["data_available"]["bp_log"] = bp_data is not None

    # Extract user_id from data_dir for per-user threshold overrides
    # data_dir is typically data/users/<user_id>
    _user_id = None
    _data_parts = raw_data_dir.parts
    if "users" in _data_parts:
        _idx = _data_parts.index("users")
        if _idx + 1 < len(_data_parts):
            _user_id = _data_parts[_idx + 1]

    rules_file = config.get("insights", {}).get("thresholds_file")
    if rules_file:
        p = Path(rules_file)
        if not p.is_absolute():
            p = Path(__file__).parent.parent.parent / rules_file
        rules = load_rules(str(p), user_id=_user_id)
    else:
        rules = load_rules(user_id=_user_id)

    insights = generate_insights(
        garmin=wearable,
        weights=weights_data,
        bp_readings=bp_data,
        trends=trends,
        rules=rules,
    )
    # Compute weight rate early (needed by pattern detection)
    # weekly_rate() returns negative for weight loss; loss_rate is positive when losing
    rate = None
    loss_rate = None
    if weights_data and len(weights_data) >= 2:
        rate = weekly_rate(weights_data)
        loss_rate = abs(rate) if rate and rate < 0 else 0

    # Pattern detection — cross-metric interaction signals
    patterns = detect_patterns(profile, garmin=wearable, weekly_loss_rate=loss_rate)
    all_insights = insights + patterns

    briefing["insights"] = [
        {"severity": i.severity, "category": i.category, "title": i.title, "body": i.body}
        for i in all_insights
    ]

    # Structured pattern summaries for dashboard
    briefing["patterns"] = summarize_patterns(profile, garmin=wearable, weekly_loss_rate=loss_rate)

    # --- Weight ---
    if weights_data and len(weights_data) >= 2:
        rolled = rolling_average(weights_data)
        current = weights_data[-1]["weight"]
        target_w = targets.get("weight_lbs")

        weight_section = {
            "current": current,
            "rolling_avg_7d": rolled[-1]["rolling_avg"] if rolled else None,
            "weekly_rate": rate,
            "entries": len(weights_data),
        }

        if rate and current:
            weight_section["rate_assessment"] = rate_assessment(rate, current)

        if target_w and rate and rate > 0:
            weight_section["target"] = target_w
            weight_section["remaining"] = round(current - target_w, 1)
            weight_section["projected_date"] = projected_date(current, target_w, rate)

        briefing["weight"] = weight_section

    # --- Nutrition (today) ---
    meals_today = _load_meals_for_date(data_dir, today)
    briefing["data_available"]["meal_log"] = (data_dir / "meal_log.csv").exists()

    if meals_today:
        totals = daily_totals(meals_today)
        briefing["nutrition"] = {"today_totals": totals}

        if targets.get("protein_g") or targets.get("calories_training"):
            # Use rest-day calories if available and no workout today
            cal_target = targets.get("calories_training", 0)
            if targets.get("calories_rest"):
                # Check if today had a workout (from Garmin workouts)
                workouts_data = _load_json(data_dir / "garmin_workouts.json")
                has_workout_today = False
                if workouts_data and isinstance(workouts_data, list):
                    has_workout_today = any(w.get("date") == today for w in workouts_data)
                if not has_workout_today:
                    cal_target = targets["calories_rest"]
            macro_targets = {
                "protein": targets.get("protein_g", 0),
                "calories": cal_target,
            }
            briefing.setdefault("nutrition", {})["day_type"] = "training" if cal_target == targets.get("calories_training", 0) else "rest"
            remaining = remaining_to_hit(meals_today, macro_targets)
            briefing["nutrition"]["remaining"] = remaining

            if targets.get("protein_g"):
                warn = protein_check(totals["protein_g"], targets["protein_g"])
                if warn:
                    briefing["nutrition"]["protein_warning"] = warn

    # --- Strength ---
    strength_data = _load_strength_log(data_dir, config)
    briefing["data_available"]["strength_log"] = strength_data is not None

    if strength_data:
        exercises = set(s.get("exercise") for s in strength_data if s.get("exercise"))
        strength_section = {}
        for ex in sorted(exercises):
            prog = progression_summary(strength_data, ex)
            if prog:
                strength_section[ex] = {
                    "current_1rm": prog["current_1rm"],
                    "peak_1rm": prog["peak_1rm"],
                    "peak_pct": prog["peak_pct"],
                    "total_sets": prog["total_sets"],
                }
        if strength_section:
            briefing["strength"] = strength_section

    # --- Habits ---
    habit_data = _load_habits(data_dir)
    briefing["data_available"]["daily_habits"] = habit_data is not None

    if habit_data:
        habits_section = {}
        # Detect format: wide (one col per habit) vs long (habit + completed cols)
        sample = habit_data[0]
        if "habit" in sample and "completed" in sample:
            # Long format: date, habit, completed
            habit_names = set(h["habit"] for h in habit_data)
            for habit_name in sorted(habit_names):
                completed_dates = [
                    h["date"] for h in habit_data
                    if h["habit"] == habit_name and h.get("completed", "").lower() in ("yes", "true", "1", "y")
                ]
                ga = gap_analysis(completed_dates, window_days=30, as_of=today)
                habits_section[habit_name] = {
                    "current_streak": ga["current_streak"],
                    "completion_rate": ga["completion_rate"],
                    "longest_streak": ga["longest_streak"],
                }
        else:
            # Wide format: date, habit1, habit2, ... (values: y/n/yes/no)
            skip_cols = {"date", "notes"}
            habit_names = [k for k in sample.keys() if k.lower() not in skip_cols]
            for habit_name in habit_names:
                completed_dates = [
                    h["date"] for h in habit_data
                    if (h.get(habit_name) or "").lower() in ("yes", "true", "1", "y")
                ]
                ga = gap_analysis(completed_dates, window_days=30, as_of=today)
                habits_section[habit_name] = {
                    "current_streak": ga["current_streak"],
                    "completion_rate": ga["completion_rate"],
                    "longest_streak": ga["longest_streak"],
                }
        if habits_section:
            briefing["habits"] = habits_section

    # --- Protocols (active focus) ---
    focus_list = config.get("focus", [])
    if focus_list and habit_data:
        from engine.coaching.protocols import load_protocol, protocol_progress
        protocols_section = []
        for entry in focus_list:
            proto_name = entry.get("protocol")
            started = entry.get("started")
            if not proto_name or not started:
                continue
            proto = load_protocol(proto_name)
            if not proto:
                continue
            progress = protocol_progress(
                protocol=proto,
                started=started,
                habit_data=habit_data,
                garmin=wearable,
                as_of=today,
            )
            progress["priority"] = entry.get("priority", 99)
            protocols_section.append(progress)
        if protocols_section:
            protocols_section.sort(key=lambda p: p.get("priority", 99))
            briefing["protocols"] = protocols_section

    # --- Coaching signals (compound) ---
    coaching_signals = []

    if wearable:
        sleep_debt = assess_sleep_debt(wearable.get("sleep_duration_avg"))
        if sleep_debt:
            coaching_signals.append({
                "severity": sleep_debt.severity,
                "title": sleep_debt.title,
                "body": sleep_debt.body,
            })

    rate = briefing.get("weight", {}).get("weekly_rate")
    if rate is not None:
        deficit = assess_deficit_impact(
            rate,
            wearable.get("hrv_rmssd_avg") if wearable else None,
            wearable.get("resting_hr") if wearable else None,
        )
        if deficit:
            coaching_signals.append({
                "severity": deficit.severity,
                "title": deficit.title,
                "body": deficit.body,
            })

        target_w = targets.get("weight_lbs")
        current_w = briefing.get("weight", {}).get("current")
        if target_w and current_w:
            taper = assess_taper_readiness(
                weeks_in_deficit=None,  # TODO: track deficit start date in config
                weight_current=current_w,
                weight_target=target_w,
                weekly_loss_rate=loss_rate,
            )
            if taper:
                coaching_signals.append({
                    "severity": taper.severity,
                    "title": taper.title,
                    "body": taper.body,
                })

    # Sleep-deficit interaction signal
    if wearable:
        sleep_deficit = assess_sleep_deficit_interaction(
            sleep_hrs_avg=wearable.get("sleep_duration_avg"),
            sleep_regularity=wearable.get("sleep_regularity_stddev"),
            weekly_loss_rate=loss_rate,
            hrv=wearable.get("hrv_rmssd_avg"),
        )
        if sleep_deficit:
            coaching_signals.append({
                "severity": sleep_deficit.severity,
                "category": sleep_deficit.category,
                "title": sleep_deficit.title,
                "body": sleep_deficit.body,
            })

    # Nutrition deviation flags (surplus + late eating)
    if meals_today:
        cal_target = targets.get("calories_training")
        bed_time_val = None
        if habit_data:
            today_habits = [h for h in habit_data if h.get("date") == today]
            if today_habits:
                bed_time_val = today_habits[-1].get("bed_time")
        deviations = assess_nutrition_deviation(
            meals_today=meals_today,
            cal_target=cal_target,
            bed_time=bed_time_val,
            as_of_hour=datetime.now().hour,
        )
        for dev in deviations:
            coaching_signals.append({
                "severity": dev.severity,
                "category": dev.category,
                "title": dev.title,
                "body": dev.body,
            })

    if coaching_signals:
        briefing["coaching_signals"] = coaching_signals

    # --- Multi-timescale horizons (Phase 1 of timescale framework) ---
    # Gives Milo context beyond single-day values. Each metric shows
    # today's value, 7-day avg, 30-day avg, and week-over-week trend.
    horizons = {}

    # Weight horizons (from weight_log.csv)
    if weights_data and len(weights_data) >= 2:
        horizons["weight"] = compute_rolling(
            weights_data, value_key="weight", windows=(7, 30)
        )

    # Wearable horizons (from garmin_daily.json 90-day series)
    if daily_series and isinstance(daily_series, list) and len(daily_series) >= 3:
        for metric_key, label in [
            ("rhr", "resting_hr"),
            ("hrv", "hrv_rmssd"),
            ("sleep_hrs", "sleep_duration"),
            ("steps", "steps"),
        ]:
            result = compute_rolling(daily_series, value_key=metric_key, windows=(7, 30))
            if result:
                horizons[label] = result

    # Protein horizons (from meal_log.csv, all dates)
    all_meals = read_csv(data_dir / "meal_log.csv") if (data_dir / "meal_log.csv").exists() else None
    if all_meals:
        protein_horizons = compute_protein_rolling(all_meals, windows=(7, 30))
        if protein_horizons:
            horizons["protein_g"] = protein_horizons

    if horizons:
        briefing["horizons"] = horizons

    # --- Alerts (Phase 2 of timescale framework) ---
    # Check rolling averages and daily series for threshold breaches.
    # Milo addresses these before discussing anything else.
    garmin_today_data = None
    garmin_today_path = data_dir / "garmin_today.json"
    if garmin_today_path.exists():
        garmin_today_data = _load_json(garmin_today_path)

    alerts = check_alerts(
        daily_series=daily_series,
        weight_data=weights_data,
        habit_data=habit_data,
        garmin_today=garmin_today_data,
        horizons=horizons,
        targets=targets,
    )
    # --- ACWR Training Load (Phase 4 of timescale framework) ---
    garmin_workouts_data = _load_json(data_dir / "garmin_workouts.json")
    strength_log_data = _load_strength_log(data_dir, config)
    session_log_path = data_dir / "session_log.csv"
    session_log_data = read_csv(session_log_path) if session_log_path.exists() else None

    sessions = build_session_list(
        garmin_workouts=garmin_workouts_data if isinstance(garmin_workouts_data, list) else None,
        strength_log=strength_log_data,
        session_log=session_log_data,
    )

    if sessions:
        acwr_result = compute_acwr(sessions)
        if acwr_result:
            briefing["training"] = acwr_result
            # Add ACWR alerts to the alerts list
            acwr_alerts = acwr_alert(acwr_result)
            if acwr_alerts:
                alerts.extend(acwr_alerts)

        # --- Condition-Aware Coaching ---
    user_conditions = get_user_conditions(config)
    if user_conditions:
        # Enrich alerts with condition-specific coaching context
        alerts = enrich_alerts_with_conditions(alerts, user_conditions)

        # Add condition metadata to briefing
        briefing["conditions"] = {
            "active": [c.get("type") for c in user_conditions],
            "additional_primary_metrics": list(get_condition_primary_metrics(user_conditions)),
            "retest_overrides": get_condition_retest_overrides(user_conditions),
            "doctor_triggers": get_condition_doctor_triggers(user_conditions),
        }

        # --- Progressive Disclosure (Phase 3 of timescale framework) ---
    # Filter horizons and alerts by user tenure and selected outcome.
    tenure_days = get_tenure_days(data_dir)
    tenure_tier = get_tenure_tier(tenure_days)
    outcome = resolve_outcome(config)

    briefing["disclosure"] = {
        "tenure_days": tenure_days,
        "tenure_tier": tenure_tier,
        "outcome": outcome,
    }

    if horizons:
        briefing["horizons"] = filter_horizons(horizons, outcome, tenure_tier)

    if alerts:
        briefing["alerts"] = filter_alerts(alerts, outcome, tenure_tier)

    return briefing


# --- Data loading helpers ---

def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _load_lab_results(data_dir: Path) -> Optional[dict]:
    path = data_dir / "lab_results.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _load_weight_log(data_dir: Path) -> Optional[list]:
    path = data_dir / "weight_log.csv"
    if not path.exists():
        return None
    rows = read_csv(path)
    weights = [
        {"weight": float(r["weight_lbs"]), "date": r["date"]}
        for r in rows if r.get("weight_lbs") and r["weight_lbs"].strip()
    ]
    return weights if weights else None


def _load_bp_log(data_dir: Path) -> Optional[list]:
    path = data_dir / "bp_log.csv"
    if not path.exists():
        return None
    rows = read_csv(path)
    readings = [
        {"sys": float(r["systolic"]), "dia": float(r["diastolic"])}
        for r in rows if r.get("systolic") and r["systolic"].strip()
    ]
    return readings if readings else None


def _load_meals_for_date(data_dir: Path, date: str) -> Optional[list]:
    path = data_dir / "meal_log.csv"
    if not path.exists():
        return None
    rows = read_csv(path)
    meals = [r for r in rows if r.get("date") == date]
    return meals if meals else None


def _load_strength_log(data_dir: Path, config: dict) -> Optional[list]:
    path = data_dir / "strength_log.csv"
    if not path.exists():
        return None
    rows = read_csv(path)
    exercise_map = config.get("exercise_name_map", {})
    for r in rows:
        raw_name = (r.get("exercise") or "").lower().strip()
        r["exercise"] = exercise_map.get(raw_name, raw_name)
    return rows if rows else None


def _load_habits(data_dir: Path) -> Optional[list]:
    path = data_dir / "daily_habits.csv"
    if not path.exists():
        return None
    rows = read_csv(path)
    return rows if rows else None


def _build_trends(garmin_daily) -> Optional[dict]:
    if not garmin_daily or not isinstance(garmin_daily, list):
        return None
    rhr_pts = [{"rhr": e["rhr"]} for e in garmin_daily if e.get("rhr") is not None]
    hrv_pts = [{"hrv": e["hrv"]} for e in garmin_daily if e.get("hrv") is not None]
    if rhr_pts or hrv_pts:
        return {"rhr_pts": rhr_pts, "hrv_pts": hrv_pts}
    return None


def _extract_lab_dates(labs: dict) -> dict:
    """Extract the most recent draw date for each lab metric from draws array."""
    dates = {}
    draws = labs.get("draws", [])
    for draw in draws:
        draw_date = draw.get("date", "")
        if not draw_date:
            continue
        results = draw.get("results", {})
        for key in results:
            if key not in dates:  # First (most recent) draw wins
                dates[key] = draw_date
    return dates


def _count_lab_readings(labs: dict) -> dict:
    """Count how many draws contain each metric."""
    counts = {}
    draws = labs.get("draws", [])
    for draw in draws:
        results = draw.get("results", {})
        for key in results:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _count_recent_readings(rows: list, days: int) -> int:
    """Count rows within the last N days."""
    from datetime import timedelta
    today = datetime.now().date()
    cutoff = today - timedelta(days=days)
    count = 0
    for row in rows:
        try:
            row_date = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date()
            if row_date >= cutoff:
                count += 1
        except (ValueError, TypeError):
            pass
    return count
