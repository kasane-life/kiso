"""Tests for the scoring engine."""

import json
from pathlib import Path

from engine.models import Demographics, UserProfile, Standing
from engine.scoring.engine import score_profile, assess, age_bucket, percentile_to_standing
from engine.scoring.tables import BP_SYSTOLIC, LDL_C, HDL_C, FASTING_INSULIN


FIXTURES = Path(__file__).parent / "fixtures"


def test_age_bucket():
    assert age_bucket(25) == "20-29"
    assert age_bucket(35) == "30-39"
    assert age_bucket(45) == "40-49"
    assert age_bucket(55) == "50-59"
    assert age_bucket(65) == "60-69"
    assert age_bucket(75) == "70+"


def test_percentile_to_standing():
    assert percentile_to_standing(90) == Standing.OPTIMAL
    assert percentile_to_standing(85) == Standing.OPTIMAL
    assert percentile_to_standing(70) == Standing.GOOD
    assert percentile_to_standing(50) == Standing.AVERAGE
    assert percentile_to_standing(20) == Standing.BELOW_AVG
    assert percentile_to_standing(10) == Standing.CONCERNING


def test_assess_lower_is_better():
    demo = Demographics(age=35, sex="M")
    # BP 110 should be optimal (<=110 first cutoff)
    standing, pct = assess(110, BP_SYSTOLIC, demo, nhanes_key="bp_systolic")
    assert standing in (Standing.OPTIMAL, Standing.GOOD)
    assert pct is not None


def test_assess_higher_is_better():
    demo = Demographics(age=35, sex="M")
    # HDL 65 should be good or optimal
    standing, pct = assess(65, HDL_C, demo, nhanes_key="hdl_c")
    assert standing in (Standing.OPTIMAL, Standing.GOOD)


def test_assess_none_value():
    demo = Demographics(age=35, sex="M")
    standing, pct = assess(None, BP_SYSTOLIC, demo)
    assert standing == Standing.UNKNOWN
    assert pct is None


def test_score_empty_profile():
    """Scoring an empty profile should return 0% coverage."""
    profile = UserProfile(demographics=Demographics(age=35, sex="M"))
    output = score_profile(profile)
    assert output["coverage_score"] == 0
    assert output["avg_percentile"] is None
    assert len(output["gaps"]) == 20  # all metrics are gaps


def test_score_full_profile():
    """Scoring a fully populated profile should return high coverage.

    Note: coverage may be <100% due to reliability multipliers on single
    readings (BP 0.5, fasting insulin 0.7, hs-CRP 0.6). With protocol-level
    data (7-day BP avg, multiple draws), coverage would be 100%.
    """
    with open(FIXTURES / "sample_profile.json") as f:
        data = json.load(f)
    demo_data = data.pop("demographics")
    profile = UserProfile(
        demographics=Demographics(**demo_data),
        **{k: v for k, v in data.items() if hasattr(UserProfile, k)},
    )
    output = score_profile(profile)
    assert output["coverage_score"] >= 85  # All metrics present, some with reliability < 1.0
    assert output["avg_percentile"] is not None
    assert len(output["gaps"]) == 0

    # With multi-reading counts, coverage reaches 100%
    counts = {"bp": 7, "hscrp": 2, "fasting_insulin": 2}
    output_full = score_profile(profile, metric_counts=counts)
    assert output_full["coverage_score"] == 100


def test_score_partial_profile():
    """Scoring with just BP + lipids should give partial coverage."""
    profile = UserProfile(
        demographics=Demographics(age=35, sex="M"),
        systolic=120,
        diastolic=75,
        ldl_c=100,
    )
    output = score_profile(profile)
    assert 0 < output["coverage_score"] < 100
    assert output["avg_percentile"] is not None
    assert len(output["gaps"]) > 0


def test_assess_normalizes_sex_to_single_letter():
    """Cutoff tables use 'M'/'F' but config may pass 'MALE'/'FEMALE'.
    assess() must handle both. This was the root cause of null percentiles
    for VO2 Max, HRV, RHR when sex='MALE' from config.yaml.
    """
    from engine.scoring.tables import VO2_MAX, HRV_RMSSD, RHR
    demo_male = Demographics(age=35, sex="MALE")
    demo_m = Demographics(age=35, sex="M")

    # VO2 Max should return the same result for both
    s1, p1 = assess(47.0, VO2_MAX, demo_male)
    s2, p2 = assess(47.0, VO2_MAX, demo_m)
    assert s1 == s2, f"VO2 sex='MALE' got {s1}, sex='M' got {s2}"
    assert p1 == p2
    assert p1 is not None, "VO2 percentile should not be None for age=35"

    # HRV should work with both
    s1, p1 = assess(55.0, HRV_RMSSD, demo_male)
    assert s1 != Standing.UNKNOWN, f"HRV with sex='MALE' returned UNKNOWN"
    assert p1 is not None

    # RHR should work with both
    s1, p1 = assess(52.0, RHR, demo_male)
    assert s1 != Standing.UNKNOWN, f"RHR with sex='MALE' returned UNKNOWN"
    assert p1 is not None


def test_bp_reliability_uses_actual_count():
    """BP reliability should reflect actual reading count, not default to 1.
    With 16 readings, should use protocol reliability (1.0), not single (0.5).
    """
    profile = UserProfile(
        demographics=Demographics(age=35, sex="M"),
        systolic=120,
        diastolic=75,
    )
    # Single reading: reliability should be 0.5
    output_single = score_profile(profile)
    bp_result_single = next(r for r in output_single["results"] if r.name == "Blood Pressure")
    assert bp_result_single.reliability == 0.5, f"Single BP reading should have 0.5 reliability, got {bp_result_single.reliability}"

    # Protocol (7+ readings): reliability should be 1.0
    output_protocol = score_profile(profile, metric_counts={"bp": 16})
    bp_result_protocol = next(r for r in output_protocol["results"] if r.name == "Blood Pressure")
    assert bp_result_protocol.reliability == 1.0, f"16 BP readings should have 1.0 reliability, got {bp_result_protocol.reliability}"


def test_zone2_cardio_returns_percentile():
    """Zone 2 cardio should be scored against AHA guidelines, not binary.
    152 min/week meets the 150 min target and should return a real percentile.
    """
    profile = UserProfile(
        demographics=Demographics(age=35, sex="M"),
        zone2_min_per_week=152,
    )
    output = score_profile(profile)
    z2 = next(r for r in output["results"] if r.name == "Zone 2 Cardio")
    assert z2.has_data is True
    assert z2.percentile_approx is not None, "Zone 2 with 152 min/week should have a percentile"
    assert z2.percentile_approx >= 50, "152 min/week meets AHA target, should be at least 50th pct"

    # Under target
    profile_low = UserProfile(
        demographics=Demographics(age=35, sex="M"),
        zone2_min_per_week=60,
    )
    output_low = score_profile(profile_low)
    z2_low = next(r for r in output_low["results"] if r.name == "Zone 2 Cardio")
    assert z2_low.percentile_approx is not None
    assert z2_low.percentile_approx < z2.percentile_approx, "60 min should score lower than 152 min"


def test_weight_trends_shows_whtr_and_percentile():
    """Weight Trends should calculate WHtR and score against Ashwell cutoffs.
    5'10" (70 inches), 35.5" waist = WHtR 0.507.
    """
    profile = UserProfile(
        demographics=Demographics(age=35, sex="M"),
        weight_lbs=190.5,
        waist_circumference=35.5,
    )
    profile.height_inches = 70  # 5'10"

    output = score_profile(profile)
    wt = next(r for r in output["results"] if r.name == "Weight Trends")
    assert wt.has_data is True
    assert wt.value is not None, "Weight Trends should show WHtR value"
    assert 0.4 < wt.value < 0.6, f"WHtR should be ~0.507, got {wt.value}"
    assert wt.percentile_approx is not None, "Weight Trends should have a percentile"
    # 0.507 is just above the 0.50 healthy threshold (Below Average in Ashwell framework)
    # Lower is better for WHtR. 0.507 > 0.50 cutoff = Below Average (25th)
    assert wt.percentile_approx == 25, f"WHtR 0.507 should be Below Average (25th), got {wt.percentile_approx}"


def test_results_have_required_fields():
    """Each MetricResult should have the expected fields."""
    profile = UserProfile(
        demographics=Demographics(age=35, sex="M"),
        resting_hr=52,
    )
    output = score_profile(profile)
    for r in output["results"]:
        assert hasattr(r, "name")
        assert hasattr(r, "tier")
        assert hasattr(r, "rank")
        assert hasattr(r, "has_data")
        assert hasattr(r, "standing")
        d = r.to_dict()
        assert "name" in d
        assert "standing" in d
