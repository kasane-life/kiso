"""Health coverage & assessment scoring engine.

Takes a user profile (demographics + health data) and computes:
1. Coverage score: what percentage of high-ROI health data do you have?
2. Assessment: for metrics with actual values, where do you stand vs peers?
3. Gap analysis: what's missing and what would it cost to close?

Tier system mirrors Hoffman strength standards:
  Optimal | Good | Average | Below Average | Concerning

Percentile sources:
  - Primary: NHANES 2017-March 2020 Pre-Pandemic (continuous, survey-weighted)
  - Fallback: Manual cutoff tables for metrics without NHANES data
"""

import json
from typing import Optional

from engine.models import Standing, Demographics, UserProfile, MetricResult
from engine.scoring.tables import (
    BP_SYSTOLIC, BP_DIASTOLIC, LDL_C, HDL_C, APOB, TRIGLYCERIDES,
    FASTING_GLUCOSE, HBA1C, FASTING_INSULIN, RHR, DAILY_STEPS, WAIST,
    LPA, SLEEP_REGULARITY, HSCRP, ALT, GGT, TSH, VITAMIN_D, FERRITIN,
    HEMOGLOBIN, VO2_MAX, HRV_RMSSD, ZONE2_MIN, WHTR,
    TIER1_WEIGHTS, TIER2_WEIGHTS,
    TIER1_STANDING_WEIGHTS, TIER2_STANDING_WEIGHTS,
)

# Try to load NHANES continuous percentile lookup
try:
    from engine.scoring.nhanes import get_percentile as nhanes_percentile, get_standing as nhanes_standing
    NHANES_AVAILABLE = True
except ImportError:
    NHANES_AVAILABLE = False

from engine.scoring.clinical import clinical_assess
from engine.scoring.freshness import compute_freshness, reliability_factor


def age_bucket(age: int) -> str:
    if age < 30:
        return "20-29"
    elif age < 40:
        return "30-39"
    elif age < 50:
        return "40-49"
    elif age < 60:
        return "50-59"
    elif age < 70:
        return "60-69"
    return "70+"


def percentile_to_standing(pct: float) -> Standing:
    """Map a continuous percentile to a Standing tier."""
    if pct >= 85:
        return Standing.OPTIMAL
    elif pct >= 65:
        return Standing.GOOD
    elif pct >= 35:
        return Standing.AVERAGE
    elif pct >= 15:
        return Standing.BELOW_AVG
    else:
        return Standing.CONCERNING


# NHANES metric key mapping
NHANES_KEY_MAP = {
    "bp_systolic": "bp_systolic",
    "bp_diastolic": "bp_diastolic",
    "rhr": "rhr",
    "ldl_c": "ldl_c",
    "hdl_c": "hdl_c",
    "triglycerides": "triglycerides",
    "fasting_glucose": "fasting_glucose",
    "hba1c": "hba1c",
    "fasting_insulin": "fasting_insulin",
    "waist": "waist",
    "hscrp": "hscrp",
    "alt": "alt",
    "ggt": "ggt",
    "ferritin": "ferritin",
    "hemoglobin": "hemoglobin",
    "apob": "apob",
    "vitamin_d": "vitamin_d",
    "tsh": "tsh",
    "lpa": "lpa",
}


def assess(value: Optional[float], table: dict, demo: Demographics,
           nhanes_key: str = None) -> tuple[Standing, Optional[float]]:
    """
    Assess a value against population data. Returns (Standing, percentile).

    Uses NHANES continuous percentiles when available, falls back to
    manual cutoff tables otherwise.
    """
    if value is None:
        return Standing.UNKNOWN, None

    # Normalize sex: config may use "MALE"/"FEMALE", tables use "M"/"F"
    sex = demo.sex
    if sex and len(sex) > 1:
        sex = sex[0].upper()

    # Try NHANES continuous scoring first
    if NHANES_AVAILABLE and nhanes_key and nhanes_key in NHANES_KEY_MAP:
        bucket = age_bucket(demo.age)
        pct = nhanes_percentile(NHANES_KEY_MAP[nhanes_key], value, bucket, sex)
        if pct is not None:
            return percentile_to_standing(pct), round(pct)

    # Fallback: manual cutoff tables (5-bucket approximation)
    bucket = age_bucket(demo.age)
    key = (bucket, sex)
    cutoffs = table["cutoffs"].get(key) or table["cutoffs"].get("universal")
    if not cutoffs:
        return Standing.UNKNOWN, None

    lower_is_better = table["lower_is_better"]

    if lower_is_better:
        if value <= cutoffs[0]:
            return Standing.OPTIMAL, 90
        elif value <= cutoffs[1]:
            return Standing.GOOD, 70
        elif value <= cutoffs[2]:
            return Standing.AVERAGE, 50
        elif value <= cutoffs[3]:
            return Standing.BELOW_AVG, 25
        else:
            return Standing.CONCERNING, 10
    else:
        if value <= cutoffs[0]:
            return Standing.CONCERNING, 10
        elif value <= cutoffs[1]:
            return Standing.BELOW_AVG, 25
        elif value <= cutoffs[2]:
            return Standing.AVERAGE, 50
        elif value <= cutoffs[3]:
            return Standing.GOOD, 70
        else:
            return Standing.OPTIMAL, 90


def _apply_clinical(result: MetricResult, clinical_key: str, value, demo: Demographics):
    """Apply clinical zone assessment to a MetricResult."""
    if value is not None:
        zone, note = clinical_assess(clinical_key, float(value), demo.age, demo.sex)
        result.clinical_zone = zone
        result.clinical_note = note


def _apply_freshness(result: MetricResult, freshness_key: str,
                     dates: dict, as_of: str = None):
    """Apply freshness decay to a MetricResult based on observed date."""
    obs_date = dates.get(freshness_key)
    if obs_date:
        result.observed_date = obs_date
        result.freshness_fraction = compute_freshness(freshness_key, obs_date, as_of)


def _apply_reliability(result: MetricResult, reliability_key: str,
                       counts: dict, is_protocol: bool = False):
    """Apply reliability multiplier to a MetricResult."""
    count = counts.get(reliability_key, 1)
    rel, note = reliability_factor(reliability_key, reading_count=count,
                                   is_protocol=is_protocol)
    result.reliability = rel
    result.reliability_note = note


def score_profile(profile: UserProfile, metric_dates: dict = None,
                  metric_counts: dict = None, as_of: str = None) -> dict:
    """Score a user profile and return coverage + assessment results.

    Args:
        profile: UserProfile with health data
        metric_dates: Optional dict mapping metric keys to ISO date strings
                      e.g., {"apob": "2026-02-13", "resting_hr": "2026-03-10"}
        metric_counts: Optional dict mapping metric keys to reading counts
                       e.g., {"bp": 7, "hscrp": 1}
        as_of: Reference date for freshness (defaults to today)
    """
    demo = profile.demographics
    dates = metric_dates or {}
    counts = metric_counts or {}
    results = []

    # --- Blood Pressure ---
    bp_has_data = profile.systolic is not None
    bp_standing, bp_pct = assess(profile.systolic, BP_SYSTOLIC, demo, nhanes_key="bp_systolic")
    results.append(MetricResult(
        name="Blood Pressure",
        tier=1, rank=1,
        has_data=bp_has_data,
        value=profile.systolic,
        unit="mmHg" + (f"/{int(profile.diastolic)}" if profile.diastolic else ""),
        standing=bp_standing,
        percentile_approx=bp_pct,
        coverage_weight=TIER1_WEIGHTS["blood_pressure"],
        cost_to_close="$40 one-time (Omron cuff)",
        note="Each 20 mmHg >115 SBP doubles CVD mortality" if not bp_has_data else "",
    ))
    _apply_clinical(results[-1], "bp_systolic", profile.systolic, demo)
    bp_key = "bp_protocol" if counts.get("bp", 1) >= 7 else "bp_single"
    _apply_freshness(results[-1], bp_key, dates, as_of)
    _apply_reliability(results[-1], "bp", counts, is_protocol=counts.get("bp", 1) >= 7)

    # --- Lipid Panel + ApoB ---
    lipid_values = [profile.ldl_c, profile.hdl_c, profile.triglycerides]
    lipid_has_data = any(v is not None for v in lipid_values)
    apob_has_data = profile.apob is not None
    if apob_has_data:
        lip_standing, lip_pct = assess(profile.apob, APOB, demo, nhanes_key="apob")
        lip_val, lip_unit = profile.apob, "mg/dL (ApoB)"
    elif profile.ldl_c is not None:
        lip_standing, lip_pct = assess(profile.ldl_c, LDL_C, demo, nhanes_key="ldl_c")
        lip_val, lip_unit = profile.ldl_c, "mg/dL (LDL-C)"
    else:
        lip_standing, lip_pct = Standing.UNKNOWN, None
        lip_val, lip_unit = None, ""

    results.append(MetricResult(
        name="Lipid Panel + ApoB",
        tier=1, rank=2,
        has_data=lipid_has_data or apob_has_data,
        value=lip_val,
        unit=lip_unit,
        standing=lip_standing,
        percentile_approx=lip_pct,
        coverage_weight=TIER1_WEIGHTS["lipid_apob"],
        cost_to_close="$30-50/yr (Quest lipid + ApoB add-on)",
        note="ApoB > LDL-C for risk prediction" if not apob_has_data and lipid_has_data else "",
    ))
    if apob_has_data:
        _apply_clinical(results[-1], "apob", profile.apob, demo)
        _apply_freshness(results[-1], "apob", dates, as_of)
    elif profile.ldl_c is not None:
        _apply_clinical(results[-1], "ldl_c", profile.ldl_c, demo)
        _apply_freshness(results[-1], "ldl_c", dates, as_of)

    # --- Metabolic Panel ---
    met_values = [profile.fasting_glucose, profile.hba1c, profile.fasting_insulin]
    met_has_data = any(v is not None for v in met_values)
    if profile.fasting_insulin is not None:
        met_standing, met_pct = assess(profile.fasting_insulin, FASTING_INSULIN, demo, nhanes_key="fasting_insulin")
        met_val, met_unit = profile.fasting_insulin, "µIU/mL (fasting insulin)"
    elif profile.hba1c is not None:
        met_standing, met_pct = assess(profile.hba1c, HBA1C, demo, nhanes_key="hba1c")
        met_val, met_unit = profile.hba1c, "% (HbA1c)"
    elif profile.fasting_glucose is not None:
        met_standing, met_pct = assess(profile.fasting_glucose, FASTING_GLUCOSE, demo, nhanes_key="fasting_glucose")
        met_val, met_unit = profile.fasting_glucose, "mg/dL (glucose)"
    else:
        met_standing, met_pct = Standing.UNKNOWN, None
        met_val, met_unit = None, ""

    results.append(MetricResult(
        name="Metabolic Panel",
        tier=1, rank=3,
        has_data=met_has_data,
        value=met_val,
        unit=met_unit,
        standing=met_standing,
        percentile_approx=met_pct,
        coverage_weight=TIER1_WEIGHTS["metabolic"],
        cost_to_close="$40-60/yr (glucose + HbA1c + insulin)",
        note="Fasting insulin catches IR 10-15 yrs before diagnosis" if profile.fasting_insulin is None and met_has_data else "",
    ))
    if profile.fasting_insulin is not None:
        _apply_clinical(results[-1], "fasting_insulin", profile.fasting_insulin, demo)
        _apply_freshness(results[-1], "fasting_insulin", dates, as_of)
        _apply_reliability(results[-1], "fasting_insulin", counts)
    elif profile.hba1c is not None:
        _apply_clinical(results[-1], "hba1c", profile.hba1c, demo)
        _apply_freshness(results[-1], "hba1c", dates, as_of)
    elif profile.fasting_glucose is not None:
        _apply_clinical(results[-1], "fasting_glucose", profile.fasting_glucose, demo)
        _apply_freshness(results[-1], "fasting_glucose", dates, as_of)

    # --- Family History ---
    fh_has_data = profile.has_family_history is not None
    results.append(MetricResult(
        name="Family History",
        tier=1, rank=4,
        has_data=fh_has_data,
        standing=Standing.GOOD if fh_has_data else Standing.UNKNOWN,
        coverage_weight=TIER1_WEIGHTS["family_history"],
        cost_to_close="Free — 10 min conversation",
        note="One-time. Parental CVD <60 doubles risk." if not fh_has_data else "",
    ))

    # --- Sleep ---
    sleep_has_data = profile.sleep_regularity_stddev is not None or profile.sleep_duration_avg is not None
    sleep_standing, sleep_pct = assess(profile.sleep_regularity_stddev, SLEEP_REGULARITY, demo)
    results.append(MetricResult(
        name="Sleep Regularity",
        tier=1, rank=5,
        has_data=sleep_has_data,
        value=profile.sleep_regularity_stddev,
        unit="min std dev (bedtime variability)",
        standing=sleep_standing,
        percentile_approx=sleep_pct,
        coverage_weight=TIER1_WEIGHTS["sleep"],
        cost_to_close="Free with any wearable",
        note="Regularity predicts mortality > duration" if not sleep_has_data else "",
    ))
    _apply_freshness(results[-1], "sleep_regularity_stddev", dates, as_of)

    # --- Daily Steps ---
    steps_has_data = profile.daily_steps_avg is not None
    steps_standing, steps_pct = assess(profile.daily_steps_avg, DAILY_STEPS, demo)
    results.append(MetricResult(
        name="Daily Steps",
        tier=1, rank=6,
        has_data=steps_has_data,
        value=profile.daily_steps_avg,
        unit="steps/day",
        standing=steps_standing,
        percentile_approx=steps_pct,
        coverage_weight=TIER1_WEIGHTS["steps"],
        cost_to_close="Free with phone",
        note="Each +1K steps = ~15% lower mortality" if not steps_has_data else "",
    ))
    _apply_freshness(results[-1], "daily_steps_avg", dates, as_of)

    # --- Resting Heart Rate ---
    rhr_has_data = profile.resting_hr is not None
    rhr_standing, rhr_pct = assess(profile.resting_hr, RHR, demo, nhanes_key="rhr")
    results.append(MetricResult(
        name="Resting Heart Rate",
        tier=1, rank=7,
        has_data=rhr_has_data,
        value=profile.resting_hr,
        unit="bpm",
        standing=rhr_standing,
        percentile_approx=rhr_pct,
        coverage_weight=TIER1_WEIGHTS["resting_hr"],
        cost_to_close="Free with wearable",
    ))
    _apply_clinical(results[-1], "rhr", profile.resting_hr, demo)
    _apply_freshness(results[-1], "resting_hr", dates, as_of)

    # --- Waist Circumference ---
    waist_has_data = profile.waist_circumference is not None
    waist_standing, waist_pct = assess(profile.waist_circumference, WAIST, demo, nhanes_key="waist")
    results.append(MetricResult(
        name="Waist Circumference",
        tier=1, rank=8,
        has_data=waist_has_data,
        value=profile.waist_circumference,
        unit="inches",
        standing=waist_standing,
        percentile_approx=waist_pct,
        coverage_weight=TIER1_WEIGHTS["waist"],
        cost_to_close="$3 tape measure",
    ))
    _apply_clinical(results[-1], "waist", profile.waist_circumference, demo)
    _apply_freshness(results[-1], "waist", dates, as_of)

    # --- Medication List ---
    meds_has_data = profile.has_medication_list is not None
    results.append(MetricResult(
        name="Medication List",
        tier=1, rank=9,
        has_data=meds_has_data,
        standing=Standing.GOOD if meds_has_data else Standing.UNKNOWN,
        coverage_weight=TIER1_WEIGHTS["medications"],
        cost_to_close="Free — 5 min entry",
        note="Context for interpreting all other data" if not meds_has_data else "",
    ))

    # --- Lp(a) ---
    lpa_has_data = profile.lpa is not None
    lpa_standing, lpa_pct = assess(profile.lpa, LPA, demo, nhanes_key="lpa")
    results.append(MetricResult(
        name="Lp(a)",
        tier=1, rank=10,
        has_data=lpa_has_data,
        value=profile.lpa,
        unit="nmol/L",
        standing=lpa_standing,
        percentile_approx=lpa_pct,
        coverage_weight=TIER1_WEIGHTS["lpa"],
        cost_to_close="$30 — once in your lifetime",
        note="20% of people have elevated Lp(a), invisible on standard panels" if not lpa_has_data else "",
    ))
    _apply_clinical(results[-1], "lpa", profile.lpa, demo)
    _apply_freshness(results[-1], "lpa", dates, as_of)

    # --- Tier 2: VO2 Max ---
    vo2_has = profile.vo2_max is not None
    vo2_standing, vo2_pct = assess(profile.vo2_max, VO2_MAX, demo)
    results.append(MetricResult(
        name="VO2 Max",
        tier=2, rank=11,
        has_data=vo2_has,
        value=profile.vo2_max,
        unit="mL/kg/min",
        standing=vo2_standing,
        percentile_approx=vo2_pct,
        coverage_weight=TIER2_WEIGHTS["vo2_max"],
        cost_to_close="Free with Garmin/Apple Watch (estimate)",
        note="Strongest modifiable predictor of all-cause mortality" if not vo2_has else "",
    ))
    _apply_clinical(results[-1], "vo2_max", profile.vo2_max, demo)
    _apply_freshness(results[-1], "vo2_max", dates, as_of)

    # --- Tier 2: HRV ---
    hrv_has = profile.hrv_rmssd_avg is not None
    hrv_standing, hrv_pct = assess(profile.hrv_rmssd_avg, HRV_RMSSD, demo)
    results.append(MetricResult(
        name="HRV (7-day avg)",
        tier=2, rank=12,
        has_data=hrv_has,
        value=profile.hrv_rmssd_avg,
        unit="ms RMSSD",
        standing=hrv_standing,
        percentile_approx=hrv_pct,
        coverage_weight=TIER2_WEIGHTS["hrv"],
        cost_to_close="Free with wearable",
        note="Use 7-day rolling avg, not single readings" if not hrv_has else "",
    ))
    _apply_freshness(results[-1], "hrv_rmssd_avg", dates, as_of)

    # --- Tier 2: hs-CRP ---
    crp_has = profile.hscrp is not None
    crp_standing, crp_pct = assess(profile.hscrp, HSCRP, demo, nhanes_key="hscrp")
    results.append(MetricResult(
        name="hs-CRP",
        tier=2, rank=13,
        has_data=crp_has,
        value=profile.hscrp,
        unit="mg/L",
        standing=crp_standing,
        percentile_approx=crp_pct,
        coverage_weight=TIER2_WEIGHTS["hscrp"],
        cost_to_close="$20/year (add to lab order)",
        note="Adds CVD risk stratification beyond lipids" if not crp_has else "",
    ))
    _apply_clinical(results[-1], "hscrp", profile.hscrp, demo)
    _apply_freshness(results[-1], "hscrp", dates, as_of)
    _apply_reliability(results[-1], "hscrp", counts)

    # --- Tier 2: Liver Enzymes ---
    liver_values = [profile.alt, profile.ggt]
    liver_has = any(v is not None for v in liver_values)
    if profile.ggt is not None:
        liver_standing, liver_pct = assess(profile.ggt, GGT, demo, nhanes_key="ggt")
        liver_val, liver_unit = profile.ggt, "U/L (GGT)"
    elif profile.alt is not None:
        liver_standing, liver_pct = assess(profile.alt, ALT, demo, nhanes_key="alt")
        liver_val, liver_unit = profile.alt, "U/L (ALT)"
    else:
        liver_standing, liver_pct = Standing.UNKNOWN, None
        liver_val, liver_unit = None, ""
    results.append(MetricResult(
        name="Liver Enzymes",
        tier=2, rank=14,
        has_data=liver_has,
        value=liver_val,
        unit=liver_unit,
        standing=liver_standing,
        percentile_approx=liver_pct,
        coverage_weight=TIER2_WEIGHTS["liver"],
        cost_to_close="Usually included in standard panels",
        note="GGT independently predicts CV mortality + diabetes" if not liver_has else "",
    ))
    if profile.ggt is not None:
        _apply_clinical(results[-1], "ggt", profile.ggt, demo)
        _apply_freshness(results[-1], "ggt", dates, as_of)
    elif profile.alt is not None:
        _apply_clinical(results[-1], "alt", profile.alt, demo)
        _apply_freshness(results[-1], "alt", dates, as_of)

    # --- Tier 2: CBC ---
    cbc_values = [profile.hemoglobin, profile.wbc, profile.platelets]
    cbc_has = any(v is not None for v in cbc_values)
    if profile.hemoglobin is not None:
        cbc_standing, cbc_pct = assess(profile.hemoglobin, HEMOGLOBIN, demo, nhanes_key="hemoglobin")
        cbc_val, cbc_unit = profile.hemoglobin, "g/dL (Hgb)"
    else:
        cbc_standing, cbc_pct = Standing.UNKNOWN, None
        cbc_val, cbc_unit = None, ""
    results.append(MetricResult(
        name="CBC",
        tier=2, rank=15,
        has_data=cbc_has,
        value=cbc_val,
        unit=cbc_unit,
        standing=cbc_standing,
        percentile_approx=cbc_pct,
        coverage_weight=TIER2_WEIGHTS["cbc"],
        cost_to_close="Usually included in standard panels",
        note="Safety net screening — RDW predicts all-cause mortality" if not cbc_has else "",
    ))
    _apply_clinical(results[-1], "hemoglobin", profile.hemoglobin, demo)
    _apply_freshness(results[-1], "hemoglobin", dates, as_of)

    # --- Tier 2: Thyroid ---
    thyroid_has = profile.tsh is not None
    if profile.tsh is not None and profile.tsh < 0.4:
        thyroid_standing = Standing.CONCERNING
        thyroid_pct = 10
    elif profile.tsh is not None and profile.tsh <= 2.5:
        thyroid_standing = Standing.OPTIMAL
        thyroid_pct = 90
    else:
        thyroid_standing, thyroid_pct = assess(profile.tsh, TSH, demo, nhanes_key="tsh")
    results.append(MetricResult(
        name="Thyroid (TSH)",
        tier=2, rank=16,
        has_data=thyroid_has,
        value=profile.tsh,
        unit="mIU/L",
        standing=thyroid_standing,
        percentile_approx=thyroid_pct,
        coverage_weight=TIER2_WEIGHTS["thyroid"],
        cost_to_close="$20/year",
        note="12% lifetime prevalence. Highly treatable." if not thyroid_has else "",
    ))
    _apply_clinical(results[-1], "tsh", profile.tsh, demo)
    _apply_freshness(results[-1], "tsh", dates, as_of)

    # --- Tier 2: Vitamin D + Ferritin ---
    vd_fer_values = [profile.vitamin_d, profile.ferritin]
    vd_fer_has = any(v is not None for v in vd_fer_values)
    if profile.vitamin_d is not None:
        vd_standing, vd_pct = assess(profile.vitamin_d, VITAMIN_D, demo, nhanes_key="vitamin_d")
        vd_val, vd_unit = profile.vitamin_d, "ng/mL (Vit D)"
    elif profile.ferritin is not None:
        vd_standing, vd_pct = assess(profile.ferritin, FERRITIN, demo, nhanes_key="ferritin")
        vd_val, vd_unit = profile.ferritin, "ng/mL (Ferritin)"
    else:
        vd_standing, vd_pct = Standing.UNKNOWN, None
        vd_val, vd_unit = None, ""
    results.append(MetricResult(
        name="Vitamin D + Ferritin",
        tier=2, rank=17,
        has_data=vd_fer_has,
        value=vd_val,
        unit=vd_unit,
        standing=vd_standing,
        percentile_approx=vd_pct,
        coverage_weight=TIER2_WEIGHTS["vitamin_d_ferritin"],
        cost_to_close="$40-60 baseline lab add-on",
        note="42% of US adults Vit D deficient. Cheap to fix." if not vd_fer_has else "",
    ))
    if profile.vitamin_d is not None:
        _apply_clinical(results[-1], "vitamin_d", profile.vitamin_d, demo)
        _apply_freshness(results[-1], "vitamin_d", dates, as_of)
    elif profile.ferritin is not None:
        _apply_clinical(results[-1], "ferritin", profile.ferritin, demo)
        _apply_freshness(results[-1], "ferritin", dates, as_of)

    # --- Tier 2: Weight Trends (WHtR if available, else binary) ---
    weight_has = profile.weight_lbs is not None
    whtr_value = None
    whtr_standing = Standing.UNKNOWN
    whtr_pct = None
    if profile.waist_circumference and profile.height_inches:
        whtr_value = round(profile.waist_circumference / profile.height_inches, 3)
        whtr_standing, whtr_pct = assess(whtr_value, WHTR, demo)
    results.append(MetricResult(
        name="Weight Trends",
        tier=2, rank=18,
        has_data=weight_has,
        value=whtr_value,
        unit=f"WHtR ({profile.weight_lbs:.0f} lbs, {profile.waist_circumference:.1f}\" waist)" if whtr_value else "",
        standing=whtr_standing if whtr_value else (Standing.GOOD if weight_has else Standing.UNKNOWN),
        percentile_approx=whtr_pct,
        coverage_weight=TIER2_WEIGHTS["weight_trends"],
        cost_to_close="$20-50 (smart scale)",
        note="" if weight_has else "Progressive drift is the signal, not absolute weight",
    ))
    _apply_freshness(results[-1], "weight_lbs", dates, as_of)

    # --- Tier 2: PHQ-9 ---
    phq9_has = profile.phq9_score is not None
    results.append(MetricResult(
        name="PHQ-9 (Depression)",
        tier=2, rank=19,
        has_data=phq9_has,
        standing=Standing.GOOD if phq9_has else Standing.UNKNOWN,
        coverage_weight=TIER2_WEIGHTS["phq9"],
        cost_to_close="Free — 3 min questionnaire",
        note="Depression independently raises CVD risk 80%" if not phq9_has else "",
    ))

    # --- Tier 2: Zone 2 Cardio ---
    z2_has = profile.zone2_min_per_week is not None
    z2_standing, z2_pct = assess(profile.zone2_min_per_week, ZONE2_MIN, demo)
    results.append(MetricResult(
        name="Zone 2 Cardio",
        tier=2, rank=20,
        has_data=z2_has,
        value=profile.zone2_min_per_week,
        unit="min/week",
        standing=z2_standing if z2_has else Standing.UNKNOWN,
        percentile_approx=z2_pct,
        coverage_weight=TIER2_WEIGHTS["zone2"],
        cost_to_close="Free with HR wearable",
        note="150-300 min/week = largest mortality reduction (AHA)" if not z2_has else "",
    ))
    _apply_freshness(results[-1], "zone2_min_per_week", dates, as_of)

    # --- Compute scores ---
    # Apply freshness × reliability to effective coverage weight
    total_weight = sum(TIER1_WEIGHTS.values()) + sum(TIER2_WEIGHTS.values())
    covered_weight = sum(
        r.coverage_weight * r.freshness_fraction * r.reliability
        for r in results if r.has_data
    )
    coverage_pct = round(covered_weight / total_weight * 100)

    tier1_results = [r for r in results if r.tier == 1]
    tier2_results = [r for r in results if r.tier == 2]
    t1_total = sum(TIER1_WEIGHTS.values())
    t2_total = sum(TIER2_WEIGHTS.values())
    t1_covered = sum(
        r.coverage_weight * r.freshness_fraction * r.reliability
        for r in tier1_results if r.has_data
    )
    t2_covered = sum(
        r.coverage_weight * r.freshness_fraction * r.reliability
        for r in tier2_results if r.has_data
    )
    t1_pct = round(t1_covered / t1_total * 100)
    t2_pct = round(t2_covered / t2_total * 100)

    # Weighted percentile composite using standing weights
    # (Lp(a) has reduced standing weight since it's genetically fixed)
    standing_weight_map = {**TIER1_STANDING_WEIGHTS, **TIER2_STANDING_WEIGHTS}
    # Map metric names to standing weight keys for lookup
    _name_to_key = {
        "Blood Pressure": "blood_pressure", "Lipid Panel + ApoB": "lipid_apob",
        "Metabolic Panel": "metabolic", "Family History": "family_history",
        "Sleep Regularity": "sleep", "Daily Steps": "steps",
        "Resting Heart Rate": "resting_hr", "Waist Circumference": "waist",
        "Medication List": "medications", "Lp(a)": "lpa",
        "VO2 Max": "vo2_max", "HRV (7-day avg)": "hrv",
        "hs-CRP": "hscrp", "Liver Enzymes": "liver", "CBC": "cbc",
        "Thyroid (TSH)": "thyroid", "Vitamin D + Ferritin": "vitamin_d_ferritin",
        "Weight Trends": "weight_trends", "PHQ-9 (Depression)": "phq9",
        "Zone 2 Cardio": "zone2",
    }
    assessed = [r for r in results if r.percentile_approx is not None]
    if assessed:
        total_sw = sum(standing_weight_map.get(_name_to_key.get(r.name, ""), 1) for r in assessed)
        weighted_pct = sum(
            r.percentile_approx * standing_weight_map.get(_name_to_key.get(r.name, ""), 1)
            for r in assessed
        )
        avg_percentile = round(weighted_pct / total_sw) if total_sw > 0 else None
    else:
        avg_percentile = None

    gaps = [r for r in results if not r.has_data]
    gaps_sorted = sorted(gaps, key=lambda r: r.coverage_weight, reverse=True)

    return {
        "demographics": f"{demo.age}{demo.sex}, {demo.ethnicity}",
        "coverage_score": coverage_pct,
        "coverage_fraction": f"{sum(1 for r in results if r.has_data)}/{len(results)}",
        "tier1_pct": t1_pct,
        "tier1_fraction": f"{sum(1 for r in tier1_results if r.has_data)}/{len(tier1_results)}",
        "tier1_weight": f"{t1_covered}/{t1_total}",
        "tier2_pct": t2_pct,
        "tier2_fraction": f"{sum(1 for r in tier2_results if r.has_data)}/{len(tier2_results)}",
        "tier2_weight": f"{t2_covered}/{t2_total}",
        "avg_percentile": avg_percentile,
        "results": results,
        "gaps": gaps_sorted,
    }


def print_report(output: dict):
    """Print a formatted scoring report to the terminal."""
    STANDING_COLORS = {
        Standing.OPTIMAL: "\033[92m",
        Standing.GOOD: "\033[96m",
        Standing.AVERAGE: "\033[93m",
        Standing.BELOW_AVG: "\033[33m",
        Standing.CONCERNING: "\033[91m",
        Standing.UNKNOWN: "\033[90m",
    }
    RESET = "\033[0m"

    demo = output["demographics"]
    print(f"\n{'='*70}")
    print(f"  HEALTH ENGINE — Coverage Assessment")
    print(f"  Profile: {demo}")
    if NHANES_AVAILABLE:
        print(f"  Percentiles: NHANES 2017-2020 (continuous, survey-weighted)")
    else:
        print(f"  Percentiles: Approximate (5-tier model)")
    print(f"{'='*70}\n")

    cov = output["coverage_score"]
    frac = output["coverage_fraction"]
    bar_len = 30
    filled = round(cov / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"  Coverage:  [{bar}] {cov}% ({frac} metrics)")

    t1_pct = output["tier1_pct"]
    t2_pct = output["tier2_pct"]
    t1_bar_len = 20
    t1_filled = round(t1_pct / 100 * t1_bar_len)
    t2_filled = round(t2_pct / 100 * t1_bar_len)
    t1_bar = "█" * t1_filled + "░" * (t1_bar_len - t1_filled)
    t2_bar = "█" * t2_filled + "░" * (t1_bar_len - t2_filled)
    print(f"    Foundation (T1): [{t1_bar}] {t1_pct}%  ({output['tier1_weight']} pts, {output['tier1_fraction']} metrics)")
    print(f"    Enhanced   (T2): [{t2_bar}] {t2_pct}%  ({output['tier2_weight']} pts, {output['tier2_fraction']} metrics)")

    if output["avg_percentile"]:
        print(f"  Standing:  ~{output['avg_percentile']}th percentile vs peers")
    print()

    tier1 = [r for r in output["results"] if r.tier == 1]
    tier2 = [r for r in output["results"] if r.tier == 2]
    total_weight = sum(r.coverage_weight for r in output["results"])

    for tier_label, tier_results in [("TIER 1: Foundation", tier1), ("TIER 2: Enhanced Picture", tier2)]:
        covered = sum(1 for r in tier_results if r.has_data)
        print(f"  ── {tier_label} ({covered}/{len(tier_results)}) ──")
        print(f"  {'#':<3} {'Metric':<22} {'Value':<28} {'Standing':<16} {'~%ile':<6} {'Wt':<6}")
        print(f"  {'─'*3} {'─'*22} {'─'*28} {'─'*16} {'─'*6} {'─'*6}")

        for r in tier_results:
            color = STANDING_COLORS.get(r.standing, "")
            if r.has_data and r.value is not None:
                val_str = f"{r.value:g} {r.unit}"
            elif r.has_data:
                val_str = "✓ Collected"
            else:
                val_str = "— missing"

            pct_str = f"~{r.percentile_approx}" if r.percentile_approx else "—"
            standing_str = f"{color}{r.standing.value}{RESET}"
            wt_str = f"+{r.coverage_weight / total_weight * 100:.1f}%"

            print(f"  {r.rank:<3} {r.name:<22} {val_str:<28} {standing_str:<25} {pct_str:<6} {wt_str:<6}")
        print()

    if output["gaps"]:
        print(f"  {'─'*70}")
        print(f"  NEXT MOVES (ranked by leverage):\n")
        for i, g in enumerate(output["gaps"], 1):
            tier_label = f"T{g.tier}"
            weight_bar = "■" * g.coverage_weight + "□" * (8 - min(g.coverage_weight, 8))
            print(f"    {i}. [{tier_label}] {g.name:<22} {weight_bar}  wt:{g.coverage_weight}")
            print(f"       Cost: {g.cost_to_close}")
            if g.note:
                print(f"       Why:  {g.note}")
            print()

    print(f"{'='*70}\n")
