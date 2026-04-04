#!/usr/bin/env python3
"""health-engine CLI — score profiles, generate insights, pull Garmin data."""

import argparse
import json
import sys
from pathlib import Path

import yaml

from engine.models import Demographics, UserProfile
from engine.scoring.engine import score_profile, print_report
from engine.insights.engine import generate_insights, load_rules


def _resolve_person_id(data_dir: Path) -> str | None:
    """Resolve person_id from data_dir (e.g. .../data/users/andrew -> person_id)."""
    if "users" not in data_dir.parts:
        return None
    user_id = data_dir.name
    try:
        from engine.gateway.db import get_db, init_db
        init_db()
        row = get_db().execute(
            "SELECT id FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


def _load_wearable_for_profile(data_dir: Path, person_id: str | None) -> dict | None:
    """Load wearable averages: SQLite first, garmin_latest.json fallback."""
    if person_id:
        from mcp_server.tools import _load_wearable_averages_sqlite
        avgs = _load_wearable_averages_sqlite(person_id)
        if avgs:
            return avgs
    # JSON fallback
    garmin_path = data_dir / "garmin_latest.json"
    if garmin_path.exists():
        with open(garmin_path) as f:
            return json.load(f)
    return None


def load_config(path: str) -> dict:
    """Load config.yaml."""
    p = Path(path)
    if not p.exists():
        print(f"Config not found: {p}")
        print("Copy config.example.yaml → config.yaml and fill in your values.")
        sys.exit(1)
    with open(p) as f:
        return yaml.safe_load(f)


def cmd_score(args):
    """Score a user profile."""
    config = load_config(args.config)
    profile_cfg = config.get("profile", {})

    # Build profile from config + any Garmin data on disk
    demo = Demographics(
        age=profile_cfg.get("age", 35),
        sex=profile_cfg.get("sex", "M"),
    )
    profile = UserProfile(demographics=demo)

    # Load wearable data (SQLite first, JSON fallback)
    data_dir = Path(config.get("data_dir", "./data"))
    person_id = _resolve_person_id(data_dir)
    garmin = _load_wearable_for_profile(data_dir, person_id)
    if garmin:
        profile.resting_hr = garmin.get("resting_hr")
        profile.daily_steps_avg = garmin.get("daily_steps_avg")
        profile.sleep_regularity_stddev = garmin.get("sleep_regularity_stddev")
        profile.sleep_duration_avg = garmin.get("sleep_duration_avg")
        profile.vo2_max = garmin.get("vo2_max")
        profile.hrv_rmssd_avg = garmin.get("hrv_rmssd_avg")
        profile.zone2_min_per_week = garmin.get("zone2_min_per_week")

    # Load from profile JSON if provided
    if args.profile:
        with open(args.profile) as f:
            data = json.load(f)
        demo_data = data.pop("demographics", {})
        profile = UserProfile(
            demographics=Demographics(**demo_data),
            **{k: v for k, v in data.items() if hasattr(UserProfile, k)},
        )

    output = score_profile(profile)
    print_report(output)

    # Also output JSON if requested
    if args.json:
        json_output = {
            k: v for k, v in output.items()
            if k not in ("results", "gaps")
        }
        json_output["results"] = [r.to_dict() for r in output["results"]]
        json_output["gaps"] = [r.to_dict() for r in output["gaps"]]
        print(json.dumps(json_output, indent=2))


def cmd_insights(args):
    """Generate health insights."""
    config = load_config(args.config)
    data_dir = Path(config.get("data_dir", "./data"))

    # Load wearable data (SQLite first, JSON fallback)
    person_id = _resolve_person_id(data_dir)
    garmin = _load_wearable_for_profile(data_dir, person_id)

    # Load daily series for trends (SQLite first, JSON fallback)
    trends = None
    series = None
    if person_id:
        from engine.coaching.briefing import _load_wearable_daily_sqlite
        series = _load_wearable_daily_sqlite(person_id)
    if not series:
        series_path = data_dir / "garmin_daily.json"
        if series_path.exists():
            with open(series_path) as f:
                series = json.load(f)
    if series:
        rhr_pts = [{"rhr": e["rhr"]} for e in series if e.get("rhr") is not None]
        hrv_pts = [{"hrv": e["hrv"]} for e in series if e.get("hrv") is not None]
        if rhr_pts or hrv_pts:
            trends = {"rhr_pts": rhr_pts, "hrv_pts": hrv_pts}

    # Load weight data
    weights = None
    weight_path = data_dir / "weight_log.csv"
    if weight_path.exists():
        from engine.utils.csv_io import read_csv
        rows = read_csv(weight_path)
        weights = [{"weight": float(r["weight_lbs"]), "date": r["date"]}
                    for r in rows if r.get("weight_lbs")]

    # Load BP data
    bp_readings = None
    bp_path = data_dir / "bp_log.csv"
    if bp_path.exists():
        from engine.utils.csv_io import read_csv
        rows = read_csv(bp_path)
        bp_readings = [{"sys": float(r["systolic"]), "dia": float(r["diastolic"])}
                       for r in rows if r.get("systolic")]

    # Load rules
    rules_file = config.get("insights", {}).get("thresholds_file")
    rules = load_rules(rules_file) if rules_file else load_rules()

    insights = generate_insights(
        garmin=garmin,
        weights=weights,
        bp_readings=bp_readings,
        trends=trends,
        rules=rules,
    )

    if not insights:
        print("No insights generated — add more data.")
        return

    for ins in insights:
        severity_icons = {
            "critical": "!!",
            "warning": " !",
            "positive": " +",
            "neutral": " ~",
        }
        icon = severity_icons.get(ins.severity, "  ")
        print(f"  [{icon}] {ins.title}")
        print(f"      {ins.body}")
        print()


def cmd_auth(args):
    """Authenticate with a wearable service."""
    if args.service == "garmin":
        from engine.integrations.garmin import GarminClient
        config = load_config(args.config)
        garmin_cfg = config.get("garmin", {})
        if garmin_cfg.get("email") or garmin_cfg.get("password"):
            print(
                "NOTE: garmin.email/password found in config.yaml — "
                "consider removing them (credentials should not be stored in config)."
            )
        token_dir = garmin_cfg.get("token_dir")
        # Sync tokens to SQLite after interactive auth
        from engine.gateway.db import init_db
        from engine.gateway.token_store import TokenStore
        init_db()
        ts = TokenStore()
        user_id = getattr(args, "user", None) or "default"
        GarminClient.auth_interactive(token_dir=token_dir, token_store=ts, user_id=user_id)
    elif args.service == "google-calendar":
        from engine.integrations.gcal_auth import run_auth_flow
        if not args.secrets:
            print("ERROR: --secrets is required for google-calendar auth.")
            print("Usage: python3 cli.py auth google-calendar --secrets /path/to/client_secret.json")
            sys.exit(1)
        user_id = getattr(args, "user", None) or "default"
        run_auth_flow(args.secrets, user_id=user_id)


def cmd_import(args):
    """Import data from wearable exports."""
    config = load_config(args.config)
    data_dir = Path(config.get("data_dir", "./data"))

    if args.source == "apple-health":
        from engine.integrations.apple_health import AppleHealthParser
        parser = AppleHealthParser(data_dir=str(data_dir))
        result = parser.parse_export(args.path, lookback_days=args.lookback_days)
        parser.save(result)

        print(f"\nApple Health import summary:")
        for key, val in result.items():
            if key not in ("source", "metadata", "last_updated"):
                if val is not None:
                    print(f"  {key}: {val}")
                else:
                    print(f"  {key}: no data")
        print(f"\nSaved to {data_dir / 'apple_health_latest.json'}")


def cmd_pull(args):
    """Pull data from Garmin Connect."""
    config = load_config(args.config)

    from engine.integrations.garmin import GarminClient
    client = GarminClient.from_config(config)
    client.pull_all(
        history=args.history,
        history_days=args.history_days,
        workouts=args.workouts,
        workout_days=args.workout_days,
    )


def cmd_briefing(args):
    """Generate a full coaching briefing (JSON snapshot of all health data)."""
    config = load_config(args.config)

    from engine.coaching.briefing import build_briefing
    briefing = build_briefing(config)

    print(json.dumps(briefing, indent=2))


def cmd_checkin(args):
    """Morning check-in — coached narrative from your health data."""
    config = load_config(args.config)

    from engine.coaching.briefing import build_briefing
    briefing = build_briefing(config)

    _render_checkin(briefing, config)


def cmd_gateway(args):
    """Start the auth gateway server."""
    from engine.gateway.config import load_gateway_config
    from engine.gateway.server import run_gateway

    config = load_gateway_config()
    if args.port:
        config.port = args.port
    run_gateway(config)


def cmd_status(args):
    """Show current data status."""
    config = load_config(args.config)
    data_dir = Path(config.get("data_dir", "./data"))

    print(f"\n  Data directory: {data_dir.resolve()}")
    print()

    files = [
        ("garmin_latest.json", "Garmin metrics"),
        ("garmin_daily.json", "Daily series (trends)"),
        ("garmin_daily_burn.json", "Daily calorie burn"),
        ("garmin_workouts.json", "Workout history"),
        ("apple_health_latest.json", "Apple Health metrics"),
        ("weight_log.csv", "Weight log"),
        ("meal_log.csv", "Meal log"),
        ("strength_log.csv", "Strength log"),
        ("bp_log.csv", "Blood pressure log"),
    ]

    for filename, label in files:
        p = data_dir / filename
        if p.exists():
            size = p.stat().st_size
            mod = p.stat().st_mtime
            from datetime import datetime
            mod_str = datetime.fromtimestamp(mod).strftime("%Y-%m-%d %H:%M")
            print(f"  ✓ {label:<25} {filename:<30} {size:>8,} bytes  ({mod_str})")
        else:
            print(f"  ✗ {label:<25} {filename:<30} missing")
    print()


def main():
    parser = argparse.ArgumentParser(
        prog="health-engine",
        description="Health intelligence engine — scoring, insights, wearable integrations",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    sub = parser.add_subparsers(dest="command")

    # score
    p_score = sub.add_parser("score", help="Score a health profile")
    p_score.add_argument("--profile", help="Path to profile JSON file")
    p_score.add_argument("--json", action="store_true", help="Also output JSON")
    p_score.set_defaults(func=cmd_score)

    # insights
    p_insights = sub.add_parser("insights", help="Generate health insights")
    p_insights.set_defaults(func=cmd_insights)

    # briefing
    p_briefing = sub.add_parser("briefing", help="Full coaching briefing (JSON)")
    p_briefing.set_defaults(func=cmd_briefing)

    # checkin
    p_checkin = sub.add_parser("checkin", help="Morning check-in (coached narrative)")
    p_checkin.set_defaults(func=cmd_checkin)

    # auth
    p_auth = sub.add_parser("auth", help="Authenticate with a service")
    p_auth.add_argument("service", choices=["garmin", "google-calendar"], help="Service to authenticate")
    p_auth.add_argument("--secrets", help="Path to OAuth client_secret.json (required for google-calendar)")
    p_auth.add_argument("--user", help="User ID for multi-user support (default: 'default')")
    p_auth.set_defaults(func=cmd_auth)

    # import
    p_import = sub.add_parser("import", help="Import wearable data exports")
    p_import.add_argument("source", choices=["apple-health"], help="Data source")
    p_import.add_argument("path", help="Path to export file (ZIP or XML)")
    p_import.add_argument("--lookback-days", type=int, default=90, help="Days of data to import")
    p_import.set_defaults(func=cmd_import)

    # pull
    p_pull = sub.add_parser("pull", help="Pull data from integrations")
    p_pull.add_argument("source", choices=["garmin"], help="Data source")
    p_pull.add_argument("--history", action="store_true", help="Pull daily series")
    p_pull.add_argument("--history-days", type=int, default=90)
    p_pull.add_argument("--workouts", action="store_true", help="Pull workouts")
    p_pull.add_argument("--workout-days", type=int, default=7)
    p_pull.set_defaults(func=cmd_pull)

    # gateway
    p_gateway = sub.add_parser("gateway", help="Start the auth gateway server")
    p_gateway.add_argument("action", choices=["start"], help="Gateway action")
    p_gateway.add_argument("--port", type=int, help="Override port (default: 18800)")
    p_gateway.set_defaults(func=cmd_gateway)

    # status
    p_status = sub.add_parser("status", help="Show data status")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


def _render_checkin(b: dict, config: dict):
    """Render briefing as a coached morning check-in."""
    from datetime import datetime

    today = datetime.now()
    day_name = today.strftime("%A")

    # --- Header with coached greeting ---
    print()
    print(f"  {day_name} check-in")
    print(f"  {'─' * 40}")

    # Build a contextual greeting
    weight = b.get("weight", {})
    current_w = weight.get("current")
    rate = weight.get("weekly_rate")
    remaining = weight.get("remaining")
    garmin_data = b.get("garmin", {})
    hrv_val = garmin_data.get("hrv_rmssd_avg")
    sleep_val = garmin_data.get("sleep_duration_avg")

    greeting_parts = []
    if current_w and remaining:
        greeting_parts.append(f"{current_w} this morning — {remaining:.0f} lbs from target.")
    if rate is not None:
        if rate > 0.3:
            greeting_parts.append("Weight's moving the wrong direction this week.")
        elif rate > -0.2:
            greeting_parts.append("Rate's stalled — let's look at why.")
        else:
            greeting_parts.append("Trending in the right direction.")
    if sleep_val and sleep_val < 7.0:
        greeting_parts.append(f"Sleep's at {sleep_val:.1f} hours — that's going to show up.")
    if hrv_val and hrv_val > 60:
        greeting_parts.append("HRV holding strong though.")

    # --- Protocol progress (before greeting) ---
    protocols = b.get("protocols", [])
    if protocols:
        for proto in protocols:
            hit = proto["last_night"]["hit"]
            total = proto["last_night"]["total"]
            day = proto["day"]
            phase = proto.get("phase", "")
            name = proto["name"]

            print(f"  Night {day} of {name} — {hit}/{total} habits")
            if phase:
                week = proto["week"]
                print(f"  Week {week}: {phase}")
            nudge = proto.get("top_nudge")
            if nudge:
                print(f"  >> Lock in: {nudge['label'].lower()}. {nudge['nudge']}")
            print()

    if greeting_parts:
        print()
        greeting = " ".join(greeting_parts)
        # Word wrap the greeting
        words = greeting.split()
        line = "  "
        for word in words:
            if len(line) + len(word) + 1 > 72:
                print(line)
                line = "  " + word
            else:
                line += (" " if line.strip() else "") + word
        if line.strip():
            print(line)
    print()

    # --- Vitals snapshot ---
    garmin = b.get("garmin", {})
    score = b.get("score", {})

    rhr = garmin.get("resting_hr")
    hrv = garmin.get("hrv_rmssd_avg")
    sleep = garmin.get("sleep_duration_avg")
    steps = garmin.get("daily_steps_avg")
    zone2 = garmin.get("zone2_min_per_week")
    coverage = score.get("coverage")

    vitals_parts = []
    if rhr:
        vitals_parts.append(f"RHR {rhr:.0f}")
    if hrv:
        vitals_parts.append(f"HRV {hrv:.0f}")
    if sleep:
        vitals_parts.append(f"Sleep {sleep:.1f}h")
    if steps:
        vitals_parts.append(f"{steps:,.0f} steps")

    if vitals_parts:
        print(f"  Vitals    {' · '.join(vitals_parts)}")

    # BP
    bp_data = b.get("score", {}).get("results", [])
    bp_result = next((r for r in bp_data if r["name"] == "Blood Pressure"), None)
    if bp_result and bp_result.get("value"):
        unit = bp_result.get("unit", "")
        # unit is like "mmHg/66" — extract diastolic
        print(f"  BP        {int(bp_result['value'])}/{unit.split('/')[-1]}")

    # Zone 2
    if zone2:
        print(f"  Zone 2    {zone2} min/week")

    # Coverage
    if coverage:
        scored = len(score.get("results", []))
        gaps = score.get("gap_count", 0)
        print(f"  Coverage  {coverage}% ({scored} scored, {gaps} gaps)")

    print()

    # --- Weight & cut progress ---
    weight = b.get("weight", {})
    if weight:
        current = weight.get("current")
        target = config.get("targets", {}).get("weight_lbs")
        rate = weight.get("weekly_rate")
        remaining = weight.get("remaining")

        if current:
            line = f"  Weight    {current} lbs"
            if remaining:
                line += f"  →  {target} lbs ({remaining} to go)"
            print(line)
        if rate is not None:
            assessment = weight.get("rate_assessment", "")
            marker = ""
            if assessment == "too_slow":
                marker = " ← stalled"
            elif assessment == "too_fast":
                marker = " ← aggressive"
            print(f"  Rate      {rate:+.1f} lbs/week{marker}")
        print()

    # --- Labs highlight (top scores) ---
    results = score.get("results", [])
    if results:
        optimal = [r for r in results if r.get("standing") == "Optimal"]
        concerning = [r for r in results if r.get("standing") in ("Concerning", "Poor")]

        if optimal:
            names = ", ".join(r["name"] for r in optimal[:4])
            print(f"  Strong    {names}")
        if concerning:
            names = ", ".join(r["name"] for r in concerning)
            print(f"  Watch     {names}")

        labs_info = b.get("labs", {})
        if labs_info.get("last_draw"):
            draw_date = datetime.strptime(labs_info["last_draw"], "%Y-%m-%d")
            days_ago = (today - draw_date).days
            print(f"  Last labs {labs_info['last_draw']} ({days_ago}d ago)")
        print()

    # --- Insights (coached) ---
    insights = b.get("insights", [])
    coaching = b.get("coaching_signals", [])
    all_signals = coaching + insights  # coaching signals first

    if all_signals:
        print(f"  Insights")
        print(f"  {'─' * 40}")
        severity_icons = {"critical": "!!", "warning": " !", "positive": " +", "neutral": " ~"}
        for sig in all_signals:
            icon = severity_icons.get(sig["severity"], "  ")
            print(f"  [{icon}] {sig['title']}")
            # Wrap body text
            body = sig["body"]
            words = body.split()
            line = "      "
            for word in words:
                if len(line) + len(word) + 1 > 72:
                    print(line)
                    line = "      " + word
                else:
                    line += (" " if line.strip() else "") + word
            if line.strip():
                print(line)
            print()

    # --- Nutrition (today so far) ---
    nutrition = b.get("nutrition", {})
    if nutrition:
        totals = nutrition.get("today_totals", {})
        remaining = nutrition.get("remaining", {})
        protein = totals.get("protein_g", 0)
        cals = totals.get("calories", 0)
        prot_remaining = remaining.get("protein_g")
        cal_remaining = remaining.get("calories")

        print(f"  Nutrition (today)")
        print(f"  {'─' * 40}")
        line = f"  Logged    {protein:.0f}g protein · {cals:.0f} cal"
        print(line)
        if prot_remaining is not None and cal_remaining is not None:
            print(f"  Left      {prot_remaining:.0f}g protein · {cal_remaining:.0f} cal")
        warn = nutrition.get("protein_warning")
        if warn:
            print(f"  [ !] {warn}")
        print()

    # --- Habits ---
    habits = b.get("habits", {})
    if habits:
        active = {k: v for k, v in habits.items() if v.get("current_streak", 0) > 0}
        if active:
            print(f"  Habits")
            print(f"  {'─' * 40}")
            for name, data in sorted(active.items(), key=lambda x: -x[1]["current_streak"]):
                streak_val = data["current_streak"]
                label = name.replace("_", " ")
                print(f"  {label:<28} {streak_val}d streak")
            print()

    # --- Top gaps (what to measure next) ---
    gaps = score.get("top_gaps", [])
    if gaps:
        print(f"  Gaps (highest leverage)")
        print(f"  {'─' * 40}")
        for g in gaps[:3]:
            print(f"  {g['name']:<28} {g['cost']}")
        print()


if __name__ == "__main__":
    main()
