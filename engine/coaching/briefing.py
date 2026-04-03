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
from engine.db_read import get_weights, get_bp, get_meals, get_habits, get_sleep, get_strength, get_labs, get_wearable_daily


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

    # Resolve person_id for SQLite reads (derive from data_dir user_id)
    _person_id = None
    try:
        # data_dir is like .../data/users/andrew → user_id = "andrew"
        if "users" in data_dir.parts:
            user_id = data_dir.name
            from engine.gateway.db import get_db, init_db
            init_db()
            _db = get_db()
            _row = _db.execute(
                "SELECT id FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
            if _row:
                _person_id = _row["id"]
    except Exception:
        pass

    profile_cfg = config.get("profile", {})
    targets = config.get("targets", {})
    today = datetime.now().strftime("%Y-%m-%d")

    briefing = {
        "as_of": today,
        "data_available": {},
    }

    # --- Wearable data (SQLite first, JSON fallback) ---
    from mcp_server.tools import _load_wearable_averages_sqlite
    wearable = _load_wearable_averages_sqlite(_person_id)
    wearable_source = None
    if wearable:
        # Determine source from latest wearable_daily row
        try:
            from engine.gateway.db import get_db, init_db
            init_db()
            _src_row = get_db().execute(
                "SELECT source FROM wearable_daily WHERE person_id = ? "
                "ORDER BY date DESC LIMIT 1", (_person_id,)
            ).fetchone()
            wearable_source = _src_row["source"] if _src_row else "garmin"
        except Exception:
            wearable_source = "garmin"
    else:
        # JSON fallback for users not yet in wearable_daily
        garmin = _load_json(data_dir / "garmin_latest.json")
        oura = _load_json(data_dir / "oura_latest.json")
        whoop = _load_json(data_dir / "whoop_latest.json")
        apple_health = _load_json(data_dir / "apple_health_latest.json")
        wearable = garmin or oura or whoop or apple_health
        if garmin: wearable_source = "garmin"
        elif oura: wearable_source = "oura"
        elif whoop: wearable_source = "whoop"
        elif apple_health: wearable_source = "apple_health"

    # Unified daily series: SQLite first (all sources), then JSON fallbacks
    daily_series_data = _load_wearable_daily_sqlite(_person_id)
    if not daily_series_data:
        daily_series_data = (
            _load_json(data_dir / "garmin_daily.json")
            or _load_json(data_dir / "oura_daily.json")
            or _load_json(data_dir / "whoop_daily.json")
        )
    briefing["data_available"]["garmin"] = wearable is not None and wearable_source == "garmin"
    briefing["data_available"]["oura"] = wearable_source == "oura"
    briefing["data_available"]["whoop"] = wearable_source == "whoop"
    briefing["data_available"]["apple_health"] = wearable_source == "apple_health"
    briefing["data_available"]["wearable_daily"] = daily_series_data is not None

    # --- Daily burn (SQLite first, JSON fallback) ---
    daily_burn = None
    if _person_id:
        try:
            from engine.gateway.db import get_db, init_db
            init_db()
            _burn_rows = get_db().execute(
                "SELECT date, calories_total as total, calories_active as active, calories_bmr as bmr "
                "FROM wearable_daily WHERE person_id = ? AND calories_total IS NOT NULL "
                "AND id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY date "
                "      ORDER BY CASE source WHEN 'garmin' THEN 1 WHEN 'apple_health' THEN 2 ELSE 3 END"
                "    ) AS rn FROM wearable_daily WHERE person_id = ?"
                "  ) WHERE rn = 1"
                ") ORDER BY date DESC LIMIT 7",
                (_person_id, _person_id),
            ).fetchall()
            if _burn_rows:
                daily_burn = [dict(r) for r in _burn_rows]
        except Exception:
            pass
    if daily_burn is None:
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
            "vo2_max_source": wearable.get("vo2_max_source"),
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

    # Add today's daily snapshot from daily series (unified wearable_daily)
    daily_series = daily_series_data
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

    # Profile fields from config (family history, medications, PHQ-9, body measurements)
    if profile_cfg.get("family_history") is not None:
        profile.has_family_history = profile_cfg["family_history"]
    if profile_cfg.get("medications") is not None:
        profile.has_medication_list = True
    if profile_cfg.get("phq9_score") is not None:
        profile.phq9_score = profile_cfg["phq9_score"]
    if profile_cfg.get("waist_inches") is not None:
        profile.waist_circumference = profile_cfg["waist_inches"]
    if profile_cfg.get("height_inches") is not None:
        profile.height_inches = profile_cfg["height_inches"]

    if wearable:
        profile.resting_hr = wearable.get("resting_hr")
        profile.daily_steps_avg = wearable.get("daily_steps_avg")
        profile.sleep_regularity_stddev = wearable.get("sleep_regularity_stddev")
        profile.sleep_duration_avg = wearable.get("sleep_duration_avg")
        profile.vo2_max = wearable.get("vo2_max")
        profile.hrv_rmssd_avg = wearable.get("hrv_rmssd_avg")
        profile.zone2_min_per_week = wearable.get("zone2_min_per_week")

    # Incorporate latest BP reading into profile for scoring
    bp_data_for_score = _load_bp_log(data_dir, person_id=_person_id)
    if bp_data_for_score and len(bp_data_for_score) > 0:
        latest_bp = bp_data_for_score[-1]
        profile.systolic = latest_bp["sys"]
        profile.diastolic = latest_bp["dia"]

    # Incorporate latest weight into profile
    weights_for_score = _load_weight_log(data_dir, person_id=_person_id)
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
    if bp_data_for_score and _person_id:
        try:
            from engine.gateway.db import get_db, init_db
            init_db()
            _bp_latest = get_db().execute(
                "SELECT date FROM bp_entry WHERE person_id = ? AND deleted_at IS NULL ORDER BY date DESC LIMIT 1",
                (_person_id,),
            ).fetchone()
            if _bp_latest:
                metric_dates["bp_single"] = _bp_latest["date"]
                metric_dates["bp_protocol"] = _bp_latest["date"]
            _bp_7d = get_db().execute(
                "SELECT COUNT(*) as cnt FROM bp_entry WHERE person_id = ? AND deleted_at IS NULL AND date >= date('now', '-7 days')",
                (_person_id,),
            ).fetchone()
            metric_counts["bp"] = _bp_7d["cnt"] if _bp_7d else 0
        except Exception:
            bp_rows_raw = read_csv(data_dir / "bp_log.csv")
            if bp_rows_raw:
                metric_dates["bp_single"] = bp_rows_raw[-1].get("date", "")
                metric_dates["bp_protocol"] = bp_rows_raw[-1].get("date", "")
                bp_count = _count_recent_readings(bp_rows_raw, 7)
                metric_counts["bp"] = bp_count
    if weights_for_score and _person_id:
        try:
            from engine.gateway.db import get_db, init_db
            init_db()
            _wt_latest = get_db().execute(
                "SELECT date FROM weight_entry WHERE person_id = ? AND deleted_at IS NULL ORDER BY date DESC LIMIT 1",
                (_person_id,),
            ).fetchone()
            if _wt_latest:
                metric_dates["weight_lbs"] = _wt_latest["date"]
        except Exception:
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
    weights_data = _load_weight_log(data_dir, person_id=_person_id)
    bp_data = _load_bp_log(data_dir, person_id=_person_id)
    trends = _build_trends(daily_series_data)
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

        last_date = weights_data[-1].get("date", "")
        weighed_today = last_date == today

        weight_section = {
            "current": current,
            "last_date": last_date,
            "weighed_in_today": weighed_today,
            "rolling_avg_7d": rolled[-1]["rolling_avg"] if rolled else None,
            "weekly_rate": rate,
            "entries": len(weights_data),
        }

        if not weighed_today:
            weight_section["note"] = f"Last weigh-in was {current} lbs on {last_date}. No entry for today yet."

        if rate and current:
            weight_section["rate_assessment"] = rate_assessment(rate, current)

        if target_w and rate and rate > 0:
            weight_section["target"] = target_w
            weight_section["remaining"] = round(current - target_w, 1)
            weight_section["projected_date"] = projected_date(current, target_w, rate)

        briefing["weight"] = weight_section

    # --- Nutrition (today) ---
    meals_today = _load_meals_for_date(data_dir, today, person_id=_person_id)
    briefing["data_available"]["meal_log"] = (data_dir / "meal_log.csv").exists() or bool(_load_meals_sqlite(_person_id))

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
    strength_data = _load_strength_log(data_dir, config, user_id=_user_id)
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
    habit_data = _load_habits(data_dir, user_id=_user_id)
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

    # Protein horizons (from SQLite or meal_log.csv)
    all_meals = get_meals(_user_id, data_dir=data_dir) or None
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
    strength_log_data = _load_strength_log(data_dir, config, user_id=_user_id)
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

    # --- Today's check-in status ---
    # Shows what's been logged today vs what's still missing, so the coach
    # can prompt for it instead of assuming.
    today_status = {}

    # Sleep: did we log last night's sleep?
    sleep_rows = get_sleep(_user_id, data_dir=data_dir)
    if sleep_rows:
        last_sleep = sleep_rows[-1]
        last_sleep_date = last_sleep.get("date", "")
        today_status["sleep"] = {
            "logged_today": last_sleep_date == today,
            "last_date": last_sleep_date,
            "last_bed_time": last_sleep.get("bed_time"),
            "last_wake_time": last_sleep.get("wake_time"),
        }
        if last_sleep_date != today:
            today_status["sleep"]["note"] = f"No sleep entry for today. Last logged: {last_sleep_date}. Did you track last night's bed/wake time?"
    else:
        # Check Garmin for sleep data
        garmin_sleep = briefing.get("today_snapshot", {}).get("sleep_hrs")
        if garmin_sleep:
            today_status["sleep"] = {
                "logged_today": True,
                "source": "garmin",
                "hours": garmin_sleep,
            }
        else:
            today_status["sleep"] = {
                "logged_today": False,
                "note": "No sleep data for today. What time did you go to bed and wake up?",
            }

    # Habits: what's logged today vs what's expected
    if habit_data:
        today_habits_raw = [h for h in habit_data if h.get("date") == today]
        if today_habits_raw:
            sample = today_habits_raw[-1]
            skip_cols = {"date", "notes"}
            logged = {}
            missing = []
            for k, v in sample.items():
                if k.lower() in skip_cols:
                    continue
                val = (v or "").lower()
                if val in ("yes", "true", "1", "y"):
                    logged[k] = True
                elif val in ("no", "false", "0", "n"):
                    logged[k] = False
                else:
                    missing.append(k)
            today_status["habits"] = {
                "logged_today": True,
                "date": today,
                "completed": {k: v for k, v in logged.items() if v},
                "missed": {k: v for k, v in logged.items() if not v},
                "not_yet_logged": missing,
                "hit_count": sum(1 for v in logged.values() if v),
                "total_count": len(logged),
            }
        else:
            last_habit_date = habit_data[-1].get("date", "") if habit_data else ""
            today_status["habits"] = {
                "logged_today": False,
                "last_date": last_habit_date,
                "note": f"No habits logged for today. Last entry: {last_habit_date}. Did you knock out your sleep stack last night? What about this morning's routine?",
            }

    # Weight: already handled above, reference it
    if "weight" in briefing:
        w = briefing["weight"]
        today_status["weight"] = {
            "logged_today": w.get("weighed_in_today", False),
            "last_value": w.get("current"),
            "last_date": w.get("last_date"),
        }
        if not w.get("weighed_in_today"):
            today_status["weight"]["note"] = f"No weigh-in today. Last: {w.get('current')} lbs on {w.get('last_date')}. Step on the scale?"

    briefing["today_status"] = today_status

    # --- Measurement prompts based on equipment + schedules ---
    measurement_prompts = []
    today_dt = datetime.strptime(today, '%Y-%m-%d')
    config_profile = config.get('profile', {})
    equipment = config_profile.get('equipment', [])

    # Equipment detection: infer from data if not explicitly set
    has_bp_monitor = 'bp_monitor' in equipment or (data_dir / 'bp_log.csv').exists()
    has_scale = 'scale' in equipment or (data_dir / 'weight_log.csv').exists()
    has_tape = 'tape_measure' in equipment or config_profile.get('waist_inches') is not None
    has_wearable = any(w in equipment for w in ['garmin', 'oura', 'whoop', 'apple_watch']) or briefing.get('wearable_source')

    # Blood pressure: 7-day series monthly (AHA standard)
    last_bp_date = None
    if _person_id:
        try:
            from engine.gateway.db import get_db, init_db
            init_db()
            _bp_row = get_db().execute(
                "SELECT date FROM bp_entry WHERE person_id = ? AND deleted_at IS NULL ORDER BY date DESC LIMIT 1",
                (_person_id,),
            ).fetchone()
            if _bp_row:
                last_bp_date = _bp_row["date"]
        except Exception:
            pass
    if not last_bp_date:
        bp_rows_raw = read_csv(data_dir / 'bp_log.csv')
        if bp_rows_raw:
            last_bp_date = bp_rows_raw[-1].get('date', '')
    if last_bp_date:
        try:
            days_since_bp = (today_dt - datetime.strptime(last_bp_date, '%Y-%m-%d')).days
            if days_since_bp >= 28:
                measurement_prompts.append({
                    'metric': 'blood_pressure',
                    'action': 'Start your monthly 7-day BP series. Take a morning reading before coffee, seated 5 min. Log daily for 7 days. The average of 7 readings is your real number.',
                    'last_measured': last_bp_date,
                    'days_since': days_since_bp,
                    'schedule': 'monthly (7-day series)',
                })
        except ValueError:
            pass
    elif has_bp_monitor:
        measurement_prompts.append({
            'metric': 'blood_pressure',
            'action': 'You have a BP monitor but no readings logged. Start a 7-day morning series: seated 5 min, before coffee. A single reading is noise. 7-day average is signal.',
            'last_measured': None,
            'schedule': 'monthly (7-day series)',
        })
    else:
        measurement_prompts.append({
            'metric': 'blood_pressure',
            'action': 'No BP monitor detected. An Omron cuff (~$40) is the single highest-leverage piece of equipment you can buy. BP is weighted 8/86 in your coverage score.',
            'equipment_needed': 'bp_monitor',
            'cost': '$40 one-time',
            'schedule': 'monthly (7-day series)',
        })

    # Weight: daily weigh-in
    last_weight_date = None
    if _person_id:
        try:
            from engine.gateway.db import get_db, init_db
            init_db()
            _wt_row = get_db().execute(
                "SELECT date FROM weight_entry WHERE person_id = ? AND deleted_at IS NULL ORDER BY date DESC LIMIT 1",
                (_person_id,),
            ).fetchone()
            if _wt_row:
                last_weight_date = _wt_row["date"]
        except Exception:
            pass
    if not last_weight_date:
        weight_rows = read_csv(data_dir / 'weight_log.csv')
        if weight_rows:
            last_weight_date = weight_rows[-1].get('date', '')
    if last_weight_date:
        try:
            days_since_weight = (today_dt - datetime.strptime(last_weight_date, '%Y-%m-%d')).days
            if days_since_weight >= 3:
                measurement_prompts.append({
                    'metric': 'weight',
                    'action': f'No weigh-in for {days_since_weight} days. Daily weighing gives the 7-day rolling average that shows real trends. Step on the scale tomorrow morning, before eating.',
                    'last_measured': last_weight_date,
                    'days_since': days_since_weight,
                    'schedule': 'daily',
                })
        except ValueError:
            pass
    elif has_scale:
        measurement_prompts.append({
            'metric': 'weight',
            'action': 'You have a scale but no weight logged. Weigh in tomorrow morning, before eating. Daily readings build the rolling average that shows real trends.',
            'last_measured': None,
            'schedule': 'daily',
        })

    # Waist circumference: monthly
    waist = config_profile.get('waist_inches')
    waist_date = config_profile.get('waist_date')
    if waist and waist_date:
        try:
            days_since_waist = (today_dt - datetime.strptime(waist_date, '%Y-%m-%d')).days
            if days_since_waist >= 28:
                measurement_prompts.append({
                    'metric': 'waist_circumference',
                    'action': 'Monthly waist measurement due. Measure at navel, standing, exhale normally.',
                    'last_measured': waist_date,
                    'last_value': waist,
                    'days_since': days_since_waist,
                    'schedule': 'monthly',
                })
        except ValueError:
            pass
    elif has_tape:
        measurement_prompts.append({
            'metric': 'waist_circumference',
            'action': 'You have a tape measure but no waist logged. Measure at navel, standing, exhale normally. 30 seconds. Tracks body comp changes the scale misses.',
            'last_measured': None,
            'schedule': 'monthly',
        })
    else:
        measurement_prompts.append({
            'metric': 'waist_circumference',
            'action': 'No waist measurement. A $3 tape measure tracks body comp changes the scale misses. Measure at navel, standing, exhale normally.',
            'equipment_needed': 'tape_measure',
            'cost': '$3',
            'schedule': 'monthly',
        })

    # Wearable: prompt if none detected
    if not has_wearable:
        measurement_prompts.append({
            'metric': 'wearable',
            'action': 'No wearable connected. A Garmin, Oura, Apple Watch, or WHOOP unlocks 5 metrics automatically (sleep, HR, HRV, steps, VO2 max). Combined coverage weight: 22/86.',
            'equipment_needed': 'wearable',
            'schedule': 'continuous (daily wear)',
        })

    # Equipment summary for the user
    detected_equipment = []
    if has_bp_monitor: detected_equipment.append('bp_monitor')
    if has_scale: detected_equipment.append('scale')
    if has_tape: detected_equipment.append('tape_measure')
    if has_wearable: detected_equipment.append(briefing.get('wearable_source', 'wearable'))
    briefing['equipment_detected'] = detected_equipment

    if measurement_prompts:
        briefing['measurement_prompts'] = measurement_prompts

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


def _load_wearable_daily_sqlite(person_id: str | None) -> Optional[list]:
    """Load wearable daily series from SQLite. Returns list of dicts matching garmin_daily.json format."""
    if not person_id:
        return None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        rows = db.execute(
            "SELECT * FROM wearable_daily WHERE person_id = ? "
            "AND id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY date "
            "      ORDER BY CASE source WHEN 'garmin' THEN 1 WHEN 'apple_health' THEN 2 ELSE 3 END"
            "    ) AS rn FROM wearable_daily WHERE person_id = ?"
            "  ) WHERE rn = 1"
            ") ORDER BY date",
            (person_id, person_id),
        ).fetchall()
        if not rows:
            return None
        return [dict(r) for r in rows]
    except Exception:
        return None


def _load_meals_sqlite(person_id: str | None, date: str | None = None) -> Optional[list]:
    """Load meals from SQLite. Returns list of dicts matching meal_log.csv format."""
    if not person_id:
        return None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        if date:
            rows = db.execute(
                "SELECT * FROM meal_entry WHERE person_id = ? AND date = ? ORDER BY meal_num",
                (person_id, date),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM meal_entry WHERE person_id = ? ORDER BY date, meal_num",
                (person_id,),
            ).fetchall()
        if not rows:
            return None
        return [dict(r) for r in rows]
    except Exception:
        return None



def _user_id_from_person(person_id):
    """Reverse lookup: person.id -> health_engine_user_id."""
    if not person_id:
        return None
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()
        row = db.execute(
            "SELECT health_engine_user_id FROM person WHERE id = ? AND deleted_at IS NULL",
            (person_id,),
        ).fetchone()
        if row and row["health_engine_user_id"]:
            return row["health_engine_user_id"]
    except Exception:
        pass
    return person_id


def _load_weight_log(data_dir: Path, person_id: str | None = None) -> Optional[list]:
    """Load weights from SQLite (via db_read), CSV fallback built in."""
    # Map person_id to user_id for db_read
    user_id = _user_id_from_person(person_id)
    rows = get_weights(user_id, data_dir)
    if rows:
        return [{"weight": float(r["weight_lbs"]), "date": r["date"]} for r in rows]
    return None


def _load_bp_log(data_dir: Path, person_id: str | None = None) -> Optional[list]:
    """Load BP from SQLite (via db_read), CSV fallback built in."""
    user_id = _user_id_from_person(person_id)
    rows = get_bp(user_id, data_dir)
    if rows:
        return [{"sys": float(r["systolic"]), "dia": float(r["diastolic"]), "date": r.get("date", "")} for r in rows]
    return None


def _load_meals_for_date(data_dir: Path, date: str, person_id: str | None = None) -> Optional[list]:
    """Load meals for a date from SQLite (via db_read), CSV fallback built in."""
    user_id = _user_id_from_person(person_id)
    meals = get_meals(user_id, date=date, data_dir=data_dir)
    return meals if meals else None


def _load_strength_log(data_dir: Path, config: dict, user_id: str | None = None) -> Optional[list]:
    """Load strength from SQLite (via db_read), CSV fallback built in."""
    rows = get_strength(user_id, data_dir=data_dir)
    if rows:
        exercise_map = config.get("exercise_name_map", {})
        for r in rows:
            raw_name = (r.get("exercise") or "").lower().strip()
            r["exercise"] = exercise_map.get(raw_name, raw_name)
    return rows if rows else None


def _load_habits(data_dir: Path, user_id: str | None = None) -> Optional[list]:
    """Load habits from SQLite (via db_read), CSV fallback built in."""
    rows = get_habits(user_id, data_dir=data_dir)
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
