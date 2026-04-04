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
        token_store=None,
        user_id: str = "default",
    ):
        self.email = email or os.environ.get("GARMIN_EMAIL")
        self.password = password or os.environ.get("GARMIN_PASSWORD")
        if token_dir:
            self.token_dir = Path(os.path.expanduser(token_dir))
        else:
            # No default fallback. Each user must have their own tokens.
            # Legacy path only used when no user_id context (CLI usage).
            legacy_path = Path(os.path.expanduser("~/.config/health-engine/garmin-tokens"))
            self.token_dir = legacy_path
        self.exercise_map = exercise_map or DEFAULT_EXERCISE_MAP
        self.data_dir = Path(data_dir or "./data")
        self._client = None
        self.token_store = token_store
        self.user_id = user_id

    @classmethod
    def from_config(cls, config: dict, token_store=None, user_id: str = "default") -> "GarminClient":
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
            token_store=token_store,
            user_id=user_id,
        )

    @classmethod
    def has_tokens(cls, token_dir: str | None = None) -> bool:
        """Check if cached garth token files exist at the specified path."""
        if token_dir:
            td = Path(token_dir)
            return td.exists() and any(td.iterdir())
        # Legacy CLI path only (no default fallback to another user)
        legacy = Path(os.path.expanduser("~/.config/health-engine/garmin-tokens"))
        return legacy.exists() and any(legacy.iterdir())

    @classmethod
    def auth_interactive(cls, token_dir: str | None = None, token_store=None, user_id: str = "default") -> bool:
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
        if token_store:
            token_store.sync_garmin_tokens(user_id)
        print("Authenticated and tokens cached. Credentials are NOT stored.")
        return True

    def _sync_to_store(self):
        """Sync garth-cache tokens back to SQLite if token_store is set."""
        if self.token_store:
            self.token_store.sync_garmin_tokens(self.user_id)

    def connect(self):
        """Authenticate with Garmin Connect.

        Token refresh strategy (never hits SSO from cron):
        1. Load cached tokens
        2. If access token expired but refresh token valid, refresh locally
        3. If refresh token also expired, raise -- require interactive re-auth
        """
        from garminconnect import Garmin

        # Try cached tokens first
        if self.token_dir.exists() and any(self.token_dir.iterdir()):
            try:
                client = Garmin()
                client.garth.load(str(self.token_dir))

                # Check token expiry before making any network calls
                if hasattr(client.garth, "oauth2_token") and client.garth.oauth2_token:
                    if client.garth.oauth2_token.refresh_expired:
                        raise RuntimeError(
                            "Refresh token expired. Run `python3 cli.py auth garmin` to re-authenticate."
                        )
                    if client.garth.oauth2_token.expired:
                        print("Access token expired, refreshing via refresh token...", file=sys.stderr)
                        client.garth.refresh_oauth2()
                        client.garth.dump(str(self.token_dir))
                        self._sync_to_store()
                        print("Token refreshed successfully.")

                dn = (client.garth.profile.get("displayName")
                      or client.garth.profile.get("userName")
                      or client.garth.profile.get("profileId"))
                if dn:
                    client.display_name = dn
                else:
                    raise RuntimeError("No display name in cached profile")
                # Persist any token changes from auto-refresh
                client.garth.dump(str(self.token_dir))
                self._sync_to_store()
                print("Authenticated with cached token.")
                self._client = client
                return client
            except Exception as e:
                print(f"Cached token auth failed: {e}", file=sys.stderr)
                # Never fall through to SSO login from automated context.
                # That causes rate limits. Require interactive re-auth.
                if not self.email or not self.password:
                    raise RuntimeError(
                        f"Token auth failed: {e}. Run `python3 cli.py auth garmin` to re-authenticate."
                    ) from e

        if not self.email or not self.password:
            raise RuntimeError(
                "No tokens found. Run `python3 cli.py auth garmin` to authenticate."
            )

        print("Logging in to Garmin Connect...")
        client = Garmin(self.email, self.password)
        client.login()
        self.token_dir.mkdir(parents=True, exist_ok=True)
        client.garth.dump(str(self.token_dir))
        self._sync_to_store()
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
                        dt = datetime.utcfromtimestamp(ts / 1000)
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

    def pull_today(self) -> dict:
        """Pull current-day intraday data from Garmin Connect.

        Returns a snapshot of today's data so far: steps, calories,
        body battery, stress, and heart rate. Updates every Garmin sync (~15 min).
        """
        today = date.today().isoformat()
        result = {"date": today}

        try:
            stats = self.client.get_stats(today)
            if stats:
                result["steps"] = stats.get("totalSteps") or 0
                result["calories_total"] = stats.get("totalKilocalories") or 0
                result["calories_active"] = (
                    stats.get("activeKilocalories")
                    or stats.get("wellnessActiveKilocalories")
                    or 0
                )
                result["calories_bmr"] = stats.get("bmrKilocalories") or 0
        except Exception as e:
            print(f"  Today stats error: {e}", file=sys.stderr)

        try:
            bb = self.client.get_body_battery(today)
            if bb and isinstance(bb, list) and len(bb) > 0:
                # Body battery is a list of readings; take the latest
                latest = bb[-1]
                result["body_battery"] = latest.get("charged") if isinstance(latest, dict) else None
            elif bb and isinstance(bb, dict):
                charged = bb.get("charged")
                if charged is not None:
                    result["body_battery"] = charged
        except Exception as e:
            print(f"  Body battery error: {e}", file=sys.stderr)

        try:
            stress = self.client.get_stress_data(today)
            if stress and isinstance(stress, dict):
                result["stress_avg"] = stress.get("overallStressLevel") or stress.get("avgStressLevel")
            elif stress and isinstance(stress, list) and len(stress) > 0:
                vals = [s.get("stressLevel", 0) for s in stress if isinstance(s, dict) and s.get("stressLevel", 0) > 0]
                if vals:
                    result["stress_avg"] = round(statistics.mean(vals))
        except Exception as e:
            print(f"  Stress error: {e}", file=sys.stderr)

        try:
            hr = self.client.get_heart_rates(today)
            if hr and isinstance(hr, dict):
                entries = hr.get("heartRateValues") or []
                # heartRateValues is list of [timestamp_ms, hr_value]
                valid = [v[1] for v in entries if isinstance(v, (list, tuple)) and len(v) >= 2 and v[1] and v[1] > 0]
                if valid:
                    result["last_hr"] = valid[-1]
                result["resting_hr"] = hr.get("restingHeartRate")
        except Exception as e:
            print(f"  Heart rate error: {e}", file=sys.stderr)

        result["last_updated"] = datetime.now().isoformat(timespec="seconds")
        return result

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
                        start_dt = datetime.utcfromtimestamp(ts / 1000)
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

    def _pull_day_snapshot(self, d: str) -> dict:
        """Pull a single day's complete data in 3 API calls.

        Args:
            d: Date string in ISO format (YYYY-MM-DD).

        Returns:
            Dict with all daily metrics from get_stats + get_sleep_data + get_hrv_data.
        """
        entry = {
            "date": d, "steps": None, "rhr": None, "hrv": None,
            "hrv_weekly_avg": None, "hrv_status": None,
            "sleep_hrs": None, "deep_sleep_hrs": None, "light_sleep_hrs": None,
            "rem_sleep_hrs": None, "awake_hrs": None, "sleep_start": None,
            "calories_total": None, "calories_active": None, "calories_bmr": None,
            "stress_avg": None, "floors": None, "distance_m": None,
            "max_hr": None, "min_hr": None,
        }

        # Call 1: get_stats (steps, RHR, calories, distance, floors, stress, HR)
        try:
            stats = self.client.get_stats(d)
            if stats:
                steps = stats.get("totalSteps")
                if isinstance(steps, (int, float)) and steps > 0:
                    entry["steps"] = int(steps)
                rhr = stats.get("restingHeartRate")
                if isinstance(rhr, (int, float)) and rhr > 0:
                    entry["rhr"] = round(rhr, 1)
                entry["calories_total"] = stats.get("totalKilocalories")
                entry["calories_active"] = stats.get("activeKilocalories") or stats.get("wellnessActiveKilocalories")
                entry["calories_bmr"] = stats.get("bmrKilocalories")
                entry["stress_avg"] = stats.get("averageStressLevel")
                entry["floors"] = stats.get("floorsAscended")
                entry["distance_m"] = stats.get("totalDistanceMeters")
                entry["max_hr"] = stats.get("maxHeartRate")
                entry["min_hr"] = stats.get("minHeartRate")
        except Exception as e:
            print(f"  get_stats({d}) error: {e}", file=sys.stderr)

        # Call 2: get_sleep_data (duration, stages, bedtime)
        try:
            sleep = self.client.get_sleep_data(d)
            if sleep:
                dto = sleep.get("dailySleepDTO", {})
                secs = dto.get("sleepTimeSeconds")
                if secs and isinstance(secs, (int, float)) and secs > 0:
                    entry["sleep_hrs"] = round(secs / 3600, 1)
                deep = dto.get("deepSleepSeconds")
                if deep and isinstance(deep, (int, float)):
                    entry["deep_sleep_hrs"] = round(deep / 3600, 1)
                light = dto.get("lightSleepSeconds")
                if light and isinstance(light, (int, float)):
                    entry["light_sleep_hrs"] = round(light / 3600, 1)
                rem = dto.get("remSleepSeconds")
                if rem and isinstance(rem, (int, float)):
                    entry["rem_sleep_hrs"] = round(rem / 3600, 1)
                awake = dto.get("awakeSleepSeconds")
                if awake and isinstance(awake, (int, float)):
                    entry["awake_hrs"] = round(awake / 3600, 1)
                ts = dto.get("sleepStartTimestampLocal")
                if ts:
                    start_dt = datetime.utcfromtimestamp(ts / 1000)
                    entry["sleep_start"] = start_dt.strftime("%H:%M")
                    if secs and secs > 0:
                        end_dt = start_dt + timedelta(seconds=secs)
                        entry["sleep_end"] = end_dt.strftime("%H:%M")
        except Exception as e:
            print(f"  get_sleep_data({d}) error: {e}", file=sys.stderr)

        # Call 3: get_hrv_data (last night avg, weekly avg, status)
        try:
            hrv_data = self.client.get_hrv_data(d)
            if hrv_data:
                summary = hrv_data.get("hrvSummary", {}) or {}
                nightly = hrv_data.get("lastNightAvg") or summary.get("lastNightAvg")
                weekly = hrv_data.get("weeklyAvg") or summary.get("weeklyAvg")
                val = nightly or weekly
                if val and isinstance(val, (int, float)) and val > 0:
                    entry["hrv"] = round(val, 1)
                if weekly and isinstance(weekly, (int, float)):
                    entry["hrv_weekly_avg"] = round(weekly, 1)
                entry["hrv_status"] = hrv_data.get("status") or summary.get("status")
        except Exception as e:
            print(f"  get_hrv_data({d}) error: {e}", file=sys.stderr)

        return entry

    def _append_to_daily_series(self, snapshot: dict, person_id: str | None = None) -> list:
        """Append a day snapshot to wearable_daily SQLite table.

        Returns the snapshot in a list for backward compatibility.
        """
        snap_date = snapshot["date"]
        series = [snapshot]

        # SQLite write
        if person_id:
            try:
                import uuid as _uuid
                from engine.gateway.db import get_db, init_db
                init_db()
                db = get_db()
                now = datetime.now().isoformat(timespec="seconds")
                rid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{person_id}:wearable_daily:{snap_date}:garmin"))

                def _sf(v):
                    """Safe float for SQLite."""
                    if v is None or v == "":
                        return None
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return None

                def _si(v):
                    if v is None or v == "":
                        return None
                    try:
                        return int(float(v))
                    except (ValueError, TypeError):
                        return None

                db.execute(
                    "INSERT OR REPLACE INTO wearable_daily (id, person_id, date, source, "
                    "rhr, hrv, hrv_weekly_avg, hrv_status, steps, sleep_hrs, deep_sleep_hrs, "
                    "light_sleep_hrs, rem_sleep_hrs, awake_hrs, sleep_start, sleep_end, "
                    "calories_total, calories_active, calories_bmr, stress_avg, floors, "
                    "distance_m, max_hr, min_hr, vo2_max, body_battery, zone2_min, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, person_id, snap_date, "garmin",
                     _sf(snapshot.get("rhr")), _sf(snapshot.get("hrv")),
                     _sf(snapshot.get("hrv_weekly_avg")), snapshot.get("hrv_status"),
                     _si(snapshot.get("steps")), _sf(snapshot.get("sleep_hrs")),
                     _sf(snapshot.get("deep_sleep_hrs")), _sf(snapshot.get("light_sleep_hrs")),
                     _sf(snapshot.get("rem_sleep_hrs")), _sf(snapshot.get("awake_hrs")),
                     snapshot.get("sleep_start"), snapshot.get("sleep_end"),
                     _sf(snapshot.get("calories_total")), _sf(snapshot.get("calories_active")),
                     _sf(snapshot.get("calories_bmr")), _si(snapshot.get("stress_avg")),
                     _sf(snapshot.get("floors")), _sf(snapshot.get("distance_m")),
                     _si(snapshot.get("max_hr")), _si(snapshot.get("min_hr")),
                     _sf(snapshot.get("vo2_max")), _si(snapshot.get("body_battery")),
                     _si(snapshot.get("zone2_min")),
                     now, now),
                )
                db.commit()
            except Exception as e:
                print(f"  SQLite wearable_daily write error: {e}", file=sys.stderr)

        return series

    def _compute_averages(self, series: list) -> dict:
        """Compute 7-day and 30-day averages from the daily series.

        Returns a dict matching the garmin_latest.json schema.
        """
        def _avg(entries, key, days):
            recent = [e for e in entries[-days:] if e.get(key) is not None]
            if not recent:
                return None
            return round(statistics.mean(e[key] for e in recent), 1)

        def _sleep_regularity(entries, days=30):
            bedtimes = []
            for e in entries[-days:]:
                st = e.get("sleep_start")
                if not st:
                    continue
                parts = st.split(":")
                if len(parts) == 2:
                    minutes = int(parts[0]) * 60 + int(parts[1])
                    if minutes < 720:  # before noon = after midnight
                        minutes += 1440
                    bedtimes.append(minutes)
            if len(bedtimes) > 1:
                return round(statistics.stdev(bedtimes), 1)
            return None

        return {
            "resting_hr": _avg(series, "rhr", 30),
            "daily_steps_avg": round(_avg(series, "steps", 30)) if _avg(series, "steps", 30) else None,
            "sleep_duration_avg": _avg(series, "sleep_hrs", 30),
            "sleep_regularity_stddev": _sleep_regularity(series, 30),
            "hrv_rmssd_avg": _avg(series, "hrv", 7),
            # VO2 and zone2 don't come from daily series
            "vo2_max": None,
            "zone2_min_per_week": None,
        }

    def pull_all(self, history=False, history_days=90, workouts=False, workout_days=7, person_id: str | None = None) -> dict:
        """Pull Garmin metrics. 3 API calls for today + local aggregation.

        With history=True, backfills daily series (for initial setup or recovery).
        person_id: if provided, dual-writes wearable data to SQLite.
        """
        print("\nPulling Garmin data...")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Pull today's snapshot (3 API calls)
        today_str = date.today().isoformat()
        snapshot = self._pull_day_snapshot(today_str)

        filled = sum(1 for k, v in snapshot.items() if k != "date" and v is not None)
        print(f"  Today ({today_str}): {filled} metrics from 3 API calls")

        # Also pull yesterday if today is sparse (watch may not have synced yet)
        if not snapshot.get("sleep_hrs") and not snapshot.get("steps"):
            yesterday_str = (date.today() - timedelta(days=1)).isoformat()
            yesterday = self._pull_day_snapshot(yesterday_str)
            self._append_to_daily_series(yesterday, person_id=person_id)
            y_filled = sum(1 for k, v in yesterday.items() if k != "date" and v is not None)
            print(f"  Yesterday ({yesterday_str}): {y_filled} metrics (today was sparse)")

        # Append today to daily series
        series = self._append_to_daily_series(snapshot, person_id=person_id)

        # VO2 max (doesn't change daily, grab from get_stats or latest in series)
        vo2 = None
        try:
            data = self.client.get_max_metrics(today_str)
            if data:
                entry = data[0] if isinstance(data, list) and data else data
                vo2 = entry.get("generic", {}).get("vo2MaxValue") if isinstance(entry.get("generic"), dict) else None
                if vo2 is None:
                    vo2 = entry.get("vo2MaxValue")
                if vo2 and isinstance(vo2, (int, float)) and vo2 > 0:
                    vo2 = round(vo2, 1)
                else:
                    vo2 = None
        except Exception:
            pass
        if vo2 is None:
            # Fall back to cached garmin_latest.json
            latest_path = self.data_dir / "garmin_latest.json"
            if latest_path.exists():
                try:
                    with open(latest_path) as f:
                        vo2 = json.load(f).get("vo2_max")
                except Exception:
                    pass

        # Zone 2 (from activities, 1 API call)
        zone2 = self.pull_zone2_minutes()

        # Update today's wearable_daily row with vo2_max and zone2_min
        # (these are computed after _append_to_daily_series, so patch them in)
        if person_id and (vo2 is not None or zone2 is not None):
            try:
                from engine.gateway.db import get_db, init_db
                init_db()
                db = get_db()
                db.execute(
                    "UPDATE wearable_daily SET vo2_max = COALESCE(?, vo2_max), "
                    "zone2_min = COALESCE(?, zone2_min), "
                    "updated_at = ? "
                    "WHERE person_id = ? AND date = ? AND source = 'garmin'",
                    (vo2, zone2, datetime.now().isoformat(timespec="seconds"),
                     person_id, today_str),
                )
                db.commit()
            except Exception as e:
                print(f"  SQLite vo2/zone2 update error: {e}", file=sys.stderr)

        # Compute averages from local series
        avgs = self._compute_averages(series)
        avgs["vo2_max"] = vo2
        avgs["zone2_min_per_week"] = zone2

        # Build garmin_latest.json (same schema as before)
        garmin_data = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            **avgs,
            "today": {
                "date": today_str,
                "steps": snapshot.get("steps"),
                "rhr": snapshot.get("rhr"),
                "hrv_last_night": snapshot.get("hrv"),
                "hrv_weekly_avg": snapshot.get("hrv_weekly_avg"),
                "sleep_hrs": snapshot.get("sleep_hrs"),
                "deep_sleep_hrs": snapshot.get("deep_sleep_hrs"),
                "rem_sleep_hrs": snapshot.get("rem_sleep_hrs"),
                "calories_total": snapshot.get("calories_total"),
                "calories_active": snapshot.get("calories_active"),
                "stress_avg": snapshot.get("stress_avg"),
            },
        }

        metric_keys = [k for k in avgs if avgs[k] is not None]
        print(f"{len(metric_keys)}/{len(avgs)} averages computed from {len(series)} days of history.")

        # Historical backfill (only when explicitly requested)
        if history:
            print(f"\n  Backfilling {history_days}-day daily series...")
            for i in range(1, history_days):
                d = (date.today() - timedelta(days=i)).isoformat()
                # Skip dates we already have
                existing_dates = {e["date"] for e in series}
                if d in existing_dates:
                    continue
                day_data = self._pull_day_snapshot(d)
                series = self._append_to_daily_series(day_data, person_id=person_id)
                time.sleep(0.3)
            print(f"  Backfill complete: {len(series)} days in series.")

            # Forward-fill vo2_max on historical rows that got NULL from _pull_day_snapshot
            if person_id:
                self.backfill_vo2_zone2(person_id=person_id)

        return garmin_data

    def backfill_vo2_zone2(self, person_id: str) -> int:
        """Forward-fill NULL vo2_max on garmin rows from the latest known value.

        VO2 max changes infrequently (weekly at most) on Garmin, so forward-filling
        from the most recent known value is a reasonable approximation for historical rows.
        Zone2 varies weekly so we leave those NULL (requires API calls to backfill properly).

        Returns the number of rows updated.
        """
        from engine.gateway.db import get_db, init_db
        init_db()
        db = get_db()

        # Find the latest known vo2_max for this person from garmin rows
        row = db.execute(
            "SELECT vo2_max FROM wearable_daily "
            "WHERE person_id = ? AND source = 'garmin' AND vo2_max IS NOT NULL "
            "ORDER BY date DESC LIMIT 1",
            (person_id,),
        ).fetchone()

        if not row:
            print("  No known vo2_max to forward-fill from.")
            return 0

        vo2 = row["vo2_max"]

        # Update all garmin rows that have NULL vo2_max
        cursor = db.execute(
            "UPDATE wearable_daily SET vo2_max = ?, updated_at = ? "
            "WHERE person_id = ? AND source = 'garmin' AND vo2_max IS NULL",
            (vo2, datetime.now().isoformat(timespec="seconds"), person_id),
        )
        db.commit()

        updated = cursor.rowcount
        if updated:
            print(f"  Forward-filled vo2_max={vo2} on {updated} garmin rows.")
        return updated
