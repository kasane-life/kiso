"""Garmin Connect integration — pull health metrics for scoring.

Requires: pip install garminconnect
Auth: Run `python3 cli.py auth garmin` for interactive login (tokens cached).
"""

import csv
import getpass
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


DEFAULT_EXERCISE_MAP = {
    "barbell deadlift": "deadlift",
    "sumo deadlift": "deadlift",
    "deadlift": "deadlift",
    "barbell bench press": "bench_press",
    "dumbbell bench press": "bench_press",
    "bench press": "bench_press",
    "barbell back squat": "squat",
    "back squat": "squat",
    "belt squat": "squat",
    "squat": "squat",
    "barbell squat": "squat",
}


class GarminClient:
    """Wrapper around garminconnect with token caching and config-driven setup."""

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token_dir: Optional[str] = None,
        exercise_map: Optional[dict] = None,
        data_dir: Optional[str] = None,
    ):
        self.email = email or os.environ.get("GARMIN_EMAIL")
        self.password = password or os.environ.get("GARMIN_PASSWORD")
        self.token_dir = Path(os.path.expanduser(token_dir or "~/.config/health-engine/garmin-tokens"))
        self.exercise_map = exercise_map or DEFAULT_EXERCISE_MAP
        self.data_dir = Path(data_dir or "./data")
        self._client = None

    @classmethod
    def from_config(cls, config: dict) -> "GarminClient":
        """Create a GarminClient from a parsed config.yaml dict."""
        garmin_cfg = config.get("garmin", {})
        # Deprecation warning for plaintext credentials in config
        if garmin_cfg.get("email") or garmin_cfg.get("password"):
            print(
                "WARNING: garmin.email/password in config.yaml is deprecated. "
                "Remove them and run `python3 cli.py auth garmin` instead.",
                file=sys.stderr,
            )
        return cls(
            email=garmin_cfg.get("email") or None,
            password=garmin_cfg.get("password") or None,
            token_dir=garmin_cfg.get("token_dir"),
            exercise_map=config.get("exercise_name_map"),
            data_dir=config.get("data_dir"),
        )

    @classmethod
    def has_tokens(cls, token_dir: str | None = None) -> bool:
        """Check if cached garth token files exist."""
        td = Path(token_dir or os.path.expanduser("~/.config/health-engine/garmin-tokens"))
        # garth stores oauth1_token.json and oauth2_token.json
        return td.exists() and any(td.iterdir())

    @classmethod
    def auth_interactive(cls, token_dir: str | None = None) -> bool:
        """Interactive CLI auth — prompts for email/password, caches tokens."""
        from garminconnect import Garmin

        td = Path(token_dir or os.path.expanduser("~/.config/health-engine/garmin-tokens"))
        email = input("Garmin Connect email: ").strip()
        password = getpass.getpass("Garmin Connect password: ")

        print("Logging in to Garmin Connect...")
        client = Garmin(email, password, prompt_mfa=input)
        client.login()
        td.mkdir(parents=True, exist_ok=True)
        client.garth.dump(str(td))
        print("Authenticated and tokens cached. Credentials are NOT stored.")
        return True

    def connect(self):
        """Authenticate with Garmin Connect."""
        from garminconnect import Garmin

        # Try cached tokens first
        if self.token_dir.exists() and any(self.token_dir.iterdir()):
            try:
                client = Garmin()
                client.garth.load(str(self.token_dir))
                dn = (client.garth.profile.get("displayName")
                      or client.garth.profile.get("userName")
                      or client.garth.profile.get("profileId"))
                if dn:
                    client.display_name = dn
                else:
                    raise RuntimeError("No display name in cached profile")
                print("Authenticated with cached token.")
                self._client = client
                return client
            except Exception as e:
                print(f"Cached token load failed: {e}", file=sys.stderr)

        if not self.email or not self.password:
            raise RuntimeError(
                "No tokens found. Run `python3 cli.py auth garmin` to authenticate."
            )

        print("Logging in to Garmin Connect...")
        client = Garmin(self.email, self.password)
        client.login()
        self.token_dir.mkdir(parents=True, exist_ok=True)
        client.garth.dump(str(self.token_dir))
        print("Authenticated and token cached.")
        self._client = client
        return client

    @property
    def client(self):
        if self._client is None:
            self.connect()
        return self._client

    def pull_resting_hr(self, days=30) -> Optional[float]:
        """Get average resting heart rate over N days."""
        values = []
        today = date.today()
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            try:
                data = self.client.get_rhr_day(d)
                if data and isinstance(data, dict):
                    rhr = (data.get("restingHeartRate")
                           or data.get("currentDayRestingHeartRate"))
                    if not rhr:
                        metrics_map = (data.get("allMetrics", {}) or {}).get("metricsMap", {}) or {}
                        wellness_rhr = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [])
                        if wellness_rhr and isinstance(wellness_rhr, list):
                            rhr = wellness_rhr[0].get("value")
                    if rhr and isinstance(rhr, (int, float)) and rhr > 0:
                        values.append(rhr)
            except Exception:
                pass
            time.sleep(0.3)

        if values:
            avg = round(statistics.mean(values), 1)
            print(f"  Resting HR: {avg} bpm (from {len(values)}/{days} days)")
            return avg
        print("  Resting HR: no data found")
        return None

    def pull_steps(self, days=30) -> Optional[int]:
        """Get average daily steps over N days."""
        values = []
        today = date.today()
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            try:
                stats = self.client.get_stats(d)
                if stats and stats.get("totalSteps"):
                    steps = stats["totalSteps"]
                    if isinstance(steps, (int, float)) and steps > 0:
                        values.append(steps)
            except Exception:
                pass
            time.sleep(0.3)

        if values:
            avg = round(statistics.mean(values))
            print(f"  Daily steps: {avg} avg (from {len(values)}/{days} days)")
            return avg
        print("  Daily steps: no data found")
        return None

    def pull_sleep_regularity(self, days=30) -> Optional[float]:
        """Get bedtime standard deviation (minutes) over N days."""
        bedtimes = []
        today = date.today()
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            try:
                sleep = self.client.get_sleep_data(d)
                if sleep:
                    dto = sleep.get("dailySleepDTO", {})
                    ts = dto.get("sleepStartTimestampLocal")
                    if ts:
                        dt = datetime.fromtimestamp(ts / 1000)
                        minutes = dt.hour * 60 + dt.minute
                        if minutes < 720:
                            minutes += 1440
                        bedtimes.append(minutes)
            except Exception:
                pass
            time.sleep(0.3)

        if len(bedtimes) > 1:
            stdev = round(statistics.stdev(bedtimes), 1)
            avg_time = statistics.mean(bedtimes) % 1440
            avg_h = int(avg_time // 60)
            avg_m = int(avg_time % 60)
            print(f"  Sleep regularity: ±{stdev} min stdev, avg bedtime ~{avg_h}:{avg_m:02d} (from {len(bedtimes)}/{days} days)")
            return stdev
        print("  Sleep regularity: insufficient data")
        return None

    def pull_sleep_duration(self, days=30) -> Optional[float]:
        """Get average sleep duration (hours) over N days."""
        durations = []
        today = date.today()
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            try:
                sleep = self.client.get_sleep_data(d)
                if sleep:
                    dto = sleep.get("dailySleepDTO", {})
                    secs = dto.get("sleepTimeSeconds")
                    if secs and isinstance(secs, (int, float)) and secs > 0:
                        durations.append(secs / 3600)
            except Exception:
                pass
            time.sleep(0.3)

        if durations:
            avg = round(statistics.mean(durations), 1)
            print(f"  Sleep duration: {avg} hrs avg (from {len(durations)}/{days} days)")
            return avg
        print("  Sleep duration: no data found")
        return None

    def pull_vo2_max(self) -> Optional[float]:
        """Get latest VO2 max estimate."""
        today = date.today()
        try:
            data = self.client.get_max_metrics(today.isoformat())
            if data:
                if isinstance(data, list) and len(data) > 0:
                    entry = data[0]
                else:
                    entry = data

                vo2 = entry.get("generic", {}).get("vo2MaxValue") if isinstance(entry.get("generic"), dict) else None
                if vo2 is None:
                    vo2 = entry.get("vo2MaxValue")

                if vo2 and isinstance(vo2, (int, float)) and vo2 > 0:
                    print(f"  VO2 max: {vo2} mL/kg/min")
                    return round(vo2, 1)
        except Exception as e:
            print(f"  VO2 max: error ({e})")
        print("  VO2 max: no data found")
        return None

    def pull_hrv(self, days=7) -> Optional[float]:
        """Get average HRV RMSSD over N days."""
        values = []
        today = date.today()
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            try:
                data = self.client.get_hrv_data(d)
                if data:
                    summary = data.get("hrvSummary", {}) or {}
                    nightly = data.get("lastNightAvg") or summary.get("lastNightAvg")
                    weekly = data.get("weeklyAvg") or summary.get("weeklyAvg")
                    val = nightly or weekly
                    if val and isinstance(val, (int, float)) and val > 0:
                        values.append(val)
            except Exception:
                pass
            time.sleep(0.3)

        if values:
            avg = round(statistics.mean(values), 1)
            print(f"  HRV RMSSD: {avg} ms (from {len(values)}/{days} days)")
            return avg
        print("  HRV RMSSD: no data found")
        return None

    def pull_zone2_minutes(self, days=7) -> Optional[int]:
        """Get total Zone 2 cardio minutes over the past week."""
        today = date.today()
        week_ago = today - timedelta(days=days)
        total_z2 = 0

        try:
            activities = self.client.get_activities_by_date(
                week_ago.isoformat(), today.isoformat()
            )
            if not activities:
                print("  Zone 2: no activities found")
                return None

            for act in activities:
                z2_secs = act.get("hrTimeInZone_2")
                if z2_secs and isinstance(z2_secs, (int, float)):
                    total_z2 += z2_secs / 60

            total_z2 = round(total_z2)
            print(f"  Zone 2: {total_z2} min/week (from {len(activities)} activities)")
            return total_z2 if total_z2 > 0 else None
        except Exception as e:
            print(f"  Zone 2: error ({e})")
            return None

    def pull_daily_series(self, days=90) -> list[dict]:
        """Pull daily RHR + HRV + sleep time series for trend analysis."""
        series = []
        today = date.today()
        print(f"\n  Pulling {days}-day daily series (RHR + HRV + sleep)...")

        for i in range(days):
            d = today - timedelta(days=i)
            d_str = d.isoformat()
            entry = {
                "date": d_str, "rhr": None, "hrv": None, "steps": None,
                "sleep_hrs": None, "sleep_start": None, "sleep_end": None,
            }

            try:
                stats = self.client.get_stats(d_str)
                if stats and stats.get("totalSteps"):
                    steps_val = stats["totalSteps"]
                    if isinstance(steps_val, (int, float)) and steps_val > 0:
                        entry["steps"] = int(steps_val)
            except Exception:
                pass

            try:
                data = self.client.get_rhr_day(d_str)
                if data and isinstance(data, dict):
                    rhr = (data.get("restingHeartRate")
                           or data.get("currentDayRestingHeartRate"))
                    if not rhr:
                        metrics_map = (data.get("allMetrics", {}) or {}).get("metricsMap", {}) or {}
                        wellness_rhr = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [])
                        if wellness_rhr and isinstance(wellness_rhr, list):
                            rhr = wellness_rhr[0].get("value")
                    if rhr and isinstance(rhr, (int, float)) and rhr > 0:
                        entry["rhr"] = round(rhr, 1)
            except Exception:
                pass

            try:
                data = self.client.get_hrv_data(d_str)
                if data:
                    summary = data.get("hrvSummary", {}) or {}
                    nightly = data.get("lastNightAvg") or summary.get("lastNightAvg")
                    weekly = data.get("weeklyAvg") or summary.get("weeklyAvg")
                    val = nightly or weekly
                    if val and isinstance(val, (int, float)) and val > 0:
                        entry["hrv"] = round(val, 1)
            except Exception:
                pass

            try:
                sleep = self.client.get_sleep_data(d_str)
                if sleep:
                    dto = sleep.get("dailySleepDTO", {})
                    secs = dto.get("sleepTimeSeconds")
                    if secs and isinstance(secs, (int, float)) and secs > 0:
                        entry["sleep_hrs"] = round(secs / 3600, 1)
                    ts = dto.get("sleepStartTimestampLocal")
                    if ts:
                        start_dt = datetime.fromtimestamp(ts / 1000)
                        entry["sleep_start"] = start_dt.strftime("%H:%M")
                        if secs and secs > 0:
                            end_dt = start_dt + timedelta(seconds=secs)
                            entry["sleep_end"] = end_dt.strftime("%H:%M")
            except Exception:
                pass

            series.append(entry)
            time.sleep(0.3)

        series.reverse()
        filled_rhr = sum(1 for e in series if e["rhr"] is not None)
        filled_hrv = sum(1 for e in series if e["hrv"] is not None)
        filled_sleep = sum(1 for e in series if e["sleep_hrs"] is not None)
        print(f"  Daily series: {filled_rhr} RHR, {filled_hrv} HRV, {filled_sleep} sleep days (of {days})")
        return series

    def normalize_exercise(self, name: str) -> str:
        """Map Garmin exercise name to normalized key using config exercise map."""
        lower = name.strip().lower()
        if lower in self.exercise_map:
            return self.exercise_map[lower]
        return lower.replace(" ", "_")

    def pull_workouts(self, days=7) -> list[dict]:
        """Pull recent activities and extract workout details."""
        today = date.today()
        start = today - timedelta(days=days)

        print(f"\n  Pulling activities from {start} to {today}...")
        try:
            activities = self.client.get_activities_by_date(
                start.isoformat(), today.isoformat()
            )
        except Exception as e:
            print(f"  Error fetching activities: {e}")
            return []

        if not activities:
            print("  No activities found.")
            return []

        workouts = []
        for act in activities:
            activity_id = act.get("activityId")
            activity_type = act.get("activityType", {})
            type_key = activity_type.get("typeKey", "unknown") if isinstance(activity_type, dict) else str(activity_type)
            act_name = act.get("activityName", type_key)
            start_local = act.get("startTimeLocal", "")
            act_date = start_local[:10] if start_local else today.isoformat()
            duration_secs = act.get("duration", 0)
            calories = act.get("calories", 0)
            avg_hr = act.get("averageHR")

            workout = {
                "activity_id": activity_id,
                "date": act_date,
                "name": act_name,
                "type": type_key,
                "duration_min": round(duration_secs / 60, 1) if duration_secs else 0,
                "calories": calories,
                "avg_hr": avg_hr,
                "strength_sets": [],
            }

            if activity_id:
                try:
                    sets_data = self.client.get_activity_exercise_sets(activity_id)
                    if not sets_data:
                        raise ValueError("no data")
                    exercises = sets_data.get("exerciseSets", []) if isinstance(sets_data, dict) else sets_data if isinstance(sets_data, list) else []
                    for s in exercises:
                        if not isinstance(s, dict):
                            continue
                        set_type = s.get("setType")
                        if set_type == "REST":
                            continue
                        ex_name = s.get("exerciseName") or s.get("exercises", [{}])[0].get("exerciseName", "") if s.get("exercises") else ""
                        if not ex_name:
                            ex_category = s.get("exerciseCategory", "")
                            ex_name = ex_category if ex_category else "unknown"
                        weight = s.get("weight")
                        reps = s.get("repetitionCount") or s.get("reps")
                        rpe = s.get("rpe")

                        weight_lbs = None
                        if weight and isinstance(weight, (int, float)) and weight > 0:
                            weight_lbs = round(weight / 453.592, 1)

                        workout["strength_sets"].append({
                            "exercise": ex_name,
                            "exercise_normalized": self.normalize_exercise(ex_name),
                            "weight_lbs": weight_lbs,
                            "reps": reps,
                            "rpe": rpe,
                        })
                    time.sleep(0.3)
                except Exception:
                    pass

            workouts.append(workout)
            set_count = len(workout["strength_sets"])
            if set_count:
                print(f"    {act_date} {act_name}: {set_count} sets")
            else:
                print(f"    {act_date} {act_name} ({type_key})")

        print(f"  Found {len(workouts)} activities.")
        return workouts

    def pull_daily_burn(self, days=7) -> list[dict]:
        """Pull daily calorie burn (BMR + active) for recent days."""
        today = date.today()
        burns = []

        print(f"\n  Pulling daily calorie burn ({days} days)...")
        for i in range(days):
            d = today - timedelta(days=i)
            d_str = d.isoformat()
            try:
                stats = self.client.get_stats(d_str)
                if stats:
                    entry = {
                        "date": d_str,
                        "bmr": stats.get("bmrKilocalories"),
                        "active": stats.get("activeKilocalories") or stats.get("wellnessActiveKilocalories"),
                        "total": stats.get("totalKilocalories") or stats.get("wellnessKilocalories"),
                    }
                    burns.append(entry)
                    total = entry["total"] or 0
                    active = entry["active"] or 0
                    print(f"    {d_str}: {total:.0f} cal total ({active:.0f} active)")
            except Exception:
                pass
            time.sleep(0.3)

        burns.sort(key=lambda x: x["date"])
        return burns

    def pull_all(self, history=False, history_days=90, workouts=False, workout_days=7) -> dict:
        """Pull all standard Garmin metrics. Returns a dict compatible with the scoring engine."""
        print("\nPulling Garmin data...")

        rhr = self.pull_resting_hr()
        steps = self.pull_steps()
        sleep_stdev = self.pull_sleep_regularity()
        sleep_duration = self.pull_sleep_duration()
        vo2 = self.pull_vo2_max()
        hrv = self.pull_hrv()
        zone2 = self.pull_zone2_minutes()

        garmin_data = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "resting_hr": rhr,
            "daily_steps_avg": steps,
            "sleep_regularity_stddev": sleep_stdev,
            "sleep_duration_avg": sleep_duration,
            "vo2_max": vo2,
            "hrv_rmssd_avg": hrv,
            "zone2_min_per_week": zone2,
        }

        # Save to data dir — only if we actually got data
        self.data_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.data_dir / "garmin_latest.json"
        metric_keys = [k for k in garmin_data if k != "last_updated"]
        filled = sum(1 for k in metric_keys if garmin_data[k] is not None)

        if filled > 0:
            with open(out_path, "w") as f:
                json.dump(garmin_data, f, indent=2)
            print(f"\nSaved to {out_path}")
        else:
            print(f"\nNo metrics retrieved — keeping existing {out_path.name} unchanged.")

        print(f"\n{filled}/{len(metric_keys)} metrics pulled successfully.")

        missing = [k for k in metric_keys if garmin_data[k] is None]
        if missing:
            print(f"Missing: {', '.join(missing)}")

        # Daily calorie burn
        burns = self.pull_daily_burn()
        if burns:
            burn_path = self.data_dir / "garmin_daily_burn.json"
            with open(burn_path, "w") as f:
                json.dump(burns, f, indent=2)

        # Workouts
        if workouts:
            workout_list = self.pull_workouts(days=workout_days)
            if workout_list:
                workouts_path = self.data_dir / "garmin_workouts.json"
                # Merge with existing
                existing = []
                if workouts_path.exists():
                    try:
                        with open(workouts_path) as f:
                            existing = json.load(f)
                    except (json.JSONDecodeError, IOError):
                        pass
                existing_ids = {w["activity_id"] for w in existing if "activity_id" in w}
                for w in workout_list:
                    if w["activity_id"] not in existing_ids:
                        existing.append(w)
                existing.sort(key=lambda w: w.get("date", ""), reverse=True)
                with open(workouts_path, "w") as f:
                    json.dump(existing, f, indent=2)
                print(f"  Saved {len(existing)} workouts to {workouts_path.name}")

        # Historical daily series
        if history:
            series = self.pull_daily_series(days=history_days)
            series_path = self.data_dir / "garmin_daily.json"
            has_any_data = any(
                e.get("rhr") is not None or e.get("hrv") is not None or e.get("sleep_hrs") is not None
                for e in series
            )
            if has_any_data:
                with open(series_path, "w") as f:
                    json.dump(series, f, indent=2)
                print(f"Saved daily series to {series_path}")
            else:
                print(f"No daily data retrieved — keeping existing {series_path.name} unchanged.")

        return garmin_data
