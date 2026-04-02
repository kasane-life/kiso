"""Core data models for health-engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Standing(Enum):
    OPTIMAL = "Optimal"
    GOOD = "Good"
    AVERAGE = "Average"
    BELOW_AVG = "Below Average"
    CONCERNING = "Concerning"
    UNKNOWN = "No Data"


class ClinicalZone(Enum):
    OPTIMAL = "Optimal"
    HEALTHY = "Healthy"
    BORDERLINE = "Borderline"
    ELEVATED = "Elevated"
    UNKNOWN = ""


@dataclass
class Demographics:
    age: int
    sex: str  # "M" or "F"
    ethnicity: str = "white"  # for NHANES percentile lookup


@dataclass
class UserProfile:
    demographics: Demographics

    # Blood pressure
    systolic: Optional[float] = None
    diastolic: Optional[float] = None

    # Lipids
    ldl_c: Optional[float] = None
    hdl_c: Optional[float] = None
    total_cholesterol: Optional[float] = None
    triglycerides: Optional[float] = None
    apob: Optional[float] = None

    # Metabolic
    fasting_glucose: Optional[float] = None
    hba1c: Optional[float] = None
    fasting_insulin: Optional[float] = None

    # Family history
    has_family_history: Optional[bool] = None

    # Sleep
    sleep_regularity_stddev: Optional[float] = None  # minutes
    sleep_duration_avg: Optional[float] = None  # hours

    # Activity
    daily_steps_avg: Optional[float] = None
    resting_hr: Optional[float] = None

    # Body
    waist_circumference: Optional[float] = None  # inches
    weight_lbs: Optional[float] = None
    height_inches: Optional[float] = None  # for BMI calculation

    # Medications
    has_medication_list: Optional[bool] = None

    # Lp(a)
    lpa: Optional[float] = None  # nmol/L

    # Inflammation
    hscrp: Optional[float] = None  # mg/L

    # Liver enzymes
    alt: Optional[float] = None  # U/L
    ast: Optional[float] = None  # U/L
    ggt: Optional[float] = None  # U/L

    # Thyroid
    tsh: Optional[float] = None  # mIU/L

    # Vitamin D + Iron
    vitamin_d: Optional[float] = None  # ng/mL (25-OH)
    ferritin: Optional[float] = None  # ng/mL

    # CBC
    hemoglobin: Optional[float] = None  # g/dL
    wbc: Optional[float] = None  # K/uL
    platelets: Optional[float] = None  # K/uL

    # Cardiorespiratory
    vo2_max: Optional[float] = None  # mL/kg/min
    hrv_rmssd_avg: Optional[float] = None  # ms

    # Mental health
    phq9_score: Optional[float] = None  # 0-27

    # Zone 2
    zone2_min_per_week: Optional[float] = None

    # Supplements
    has_supplement_list: Optional[bool] = None


@dataclass
class MetricResult:
    name: str
    tier: int
    rank: int
    has_data: bool
    value: Optional[float] = None
    unit: str = ""
    standing: Standing = Standing.UNKNOWN
    percentile_approx: Optional[int] = None
    coverage_weight: float = 1.0
    cost_to_close: str = ""
    note: str = ""
    clinical_zone: str = ""        # "Optimal" / "Healthy" / "Borderline" / "Elevated" / ""
    clinical_note: str = ""        # e.g., "ApoB 72 is below the ESC target of <80"
    observed_date: str = ""        # ISO date of when this metric was measured
    freshness_fraction: float = 1.0  # 0.0-1.0, decays over time
    reliability: float = 1.0      # 0.0-1.0, based on CVI and reading count
    reliability_note: str = ""     # e.g., "Single hs-CRP reading (42% CVI)"

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "tier": self.tier,
            "rank": self.rank,
            "has_data": self.has_data,
            "value": self.value,
            "unit": self.unit,
            "standing": self.standing.value,
            "percentile_approx": self.percentile_approx,
            "coverage_weight": self.coverage_weight,
            "cost_to_close": self.cost_to_close,
            "note": self.note,
        }
        if self.clinical_zone:
            d["clinical_zone"] = self.clinical_zone
            d["clinical_note"] = self.clinical_note
        if self.observed_date:
            d["observed_date"] = self.observed_date
        if self.freshness_fraction < 1.0:
            d["freshness_fraction"] = round(self.freshness_fraction, 2)
        if self.reliability < 1.0:
            d["reliability"] = round(self.reliability, 2)
            d["reliability_note"] = self.reliability_note
        return d


@dataclass
class Insight:
    """A single health insight generated from data analysis."""
    severity: str  # "critical", "warning", "positive", "neutral"
    title: str
    body: str
    category: str = ""  # e.g., "hrv", "sleep", "rhr", "weight", "bp"


# --- Health tracking models (SQLite-backed) ---

@dataclass
class WeightEntry:
    """A single weight measurement."""
    id: str
    person_id: str
    date: str
    weight_lbs: float
    waist_in: Optional[float] = None
    source: Optional[str] = None

@dataclass
class MealEntry:
    """A single meal log entry."""
    id: str
    person_id: str
    date: str
    meal_num: Optional[int] = None
    time_of_day: Optional[str] = None
    description: Optional[str] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    calories: Optional[float] = None
    notes: Optional[str] = None

@dataclass
class BpEntry:
    """A single blood pressure reading."""
    id: str
    person_id: str
    date: str
    systolic: float
    diastolic: float
    source: Optional[str] = None

@dataclass
class TrainingSession:
    """A training session (cardio, strength, etc.)."""
    id: str
    person_id: str
    date: str
    rpe: Optional[float] = None
    duration_min: Optional[float] = None
    type: Optional[str] = None
    name: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = None

@dataclass
class StrengthSet:
    """A single set within a training session."""
    id: str
    person_id: str
    date: str
    exercise: str
    session_id: Optional[str] = None
    weight_lbs: Optional[float] = None
    reps: Optional[int] = None
    rpe: Optional[float] = None
    notes: Optional[str] = None

@dataclass
class WearableDaily:
    """One day of wearable data (Garmin, Oura, WHOOP, Apple Watch)."""
    id: str
    person_id: str
    date: str
    source: Optional[str] = None
    rhr: Optional[float] = None
    hrv: Optional[float] = None
    hrv_weekly_avg: Optional[float] = None
    hrv_status: Optional[str] = None
    steps: Optional[int] = None
    sleep_hrs: Optional[float] = None
    deep_sleep_hrs: Optional[float] = None
    light_sleep_hrs: Optional[float] = None
    rem_sleep_hrs: Optional[float] = None
    awake_hrs: Optional[float] = None
    sleep_start: Optional[str] = None
    sleep_end: Optional[str] = None
    calories_total: Optional[float] = None
    calories_active: Optional[float] = None
    calories_bmr: Optional[float] = None
    stress_avg: Optional[int] = None
    floors: Optional[float] = None
    distance_m: Optional[float] = None
    max_hr: Optional[int] = None
    min_hr: Optional[int] = None
    vo2_max: Optional[float] = None
    body_battery: Optional[int] = None

@dataclass
class LabDraw:
    """A lab visit / blood draw event."""
    id: str
    person_id: str
    date: str
    source: Optional[str] = None
    notes: Optional[str] = None
    results: list = field(default_factory=list)  # list[LabResult]

@dataclass
class LabResult:
    """A single biomarker from a lab draw."""
    id: str
    draw_id: str
    person_id: str
    marker: str
    value: Optional[float] = None
    value_text: Optional[str] = None
    unit: Optional[str] = None
    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    flag: Optional[str] = None

@dataclass
class HabitLog:
    """A single habit completion record (normalized from wide CSV format)."""
    id: str
    person_id: str
    date: str
    habit_name: str
    completed: bool = False
    notes: Optional[str] = None
