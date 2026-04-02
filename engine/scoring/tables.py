"""Cutoff tables for health metric scoring.

Sourced from NHANES, AHA/ACC 2017, ADA, ACSM, Copenhagen City Heart Study,
Paluch et al. (Lancet 2022), INTERHEART, etc.

For "lower is better": cutoffs = [Optimal ceiling, Good ceiling, Avg ceiling, Below Avg ceiling]
For "higher is better": cutoffs = [Concerning ceiling, Below Avg ceiling, Avg ceiling, Good ceiling]
"""

# Blood Pressure (Systolic) — lower is better
BP_SYSTOLIC = {
    "lower_is_better": True,
    "unit": "mmHg",
    "cutoffs": {
        ("30-39", "M"): [110, 120, 130, 140],
        ("30-39", "F"): [110, 120, 130, 140],
        ("40-49", "M"): [115, 125, 135, 145],
        ("50-59", "M"): [120, 130, 140, 150],
    },
}

# Blood Pressure (Diastolic) — lower is better
BP_DIASTOLIC = {
    "lower_is_better": True,
    "unit": "mmHg",
    "cutoffs": {
        ("30-39", "M"): [70, 80, 85, 90],
        ("30-39", "F"): [70, 80, 85, 90],
    },
}

# LDL Cholesterol — lower is better
LDL_C = {
    "lower_is_better": True,
    "unit": "mg/dL",
    "cutoffs": {
        ("30-39", "M"): [80, 100, 130, 160],
        ("30-39", "F"): [80, 100, 130, 160],
    },
}

# HDL Cholesterol — higher is better
HDL_C = {
    "lower_is_better": False,
    "unit": "mg/dL",
    "cutoffs": {
        ("30-39", "M"): [35, 40, 50, 60],
        ("30-39", "F"): [40, 50, 60, 70],
    },
}

# ApoB — lower is better
APOB = {
    "lower_is_better": True,
    "unit": "mg/dL",
    "cutoffs": {
        "universal": [70, 90, 110, 130],
    },
}

# Triglycerides — lower is better
TRIGLYCERIDES = {
    "lower_is_better": True,
    "unit": "mg/dL",
    "cutoffs": {
        ("30-39", "M"): [75, 100, 150, 200],
        ("30-39", "F"): [75, 100, 150, 200],
    },
}

# Fasting Glucose — lower is better
FASTING_GLUCOSE = {
    "lower_is_better": True,
    "unit": "mg/dL",
    "cutoffs": {
        ("30-39", "M"): [88, 95, 100, 113],
        ("30-39", "F"): [88, 95, 100, 113],
    },
}

# HbA1c — lower is better
HBA1C = {
    "lower_is_better": True,
    "unit": "%",
    "cutoffs": {
        ("30-39", "M"): [5.0, 5.2, 5.6, 6.0],
        ("30-39", "F"): [5.0, 5.2, 5.6, 6.0],
    },
}

# Fasting Insulin — lower is better
FASTING_INSULIN = {
    "lower_is_better": True,
    "unit": "µIU/mL",
    "cutoffs": {
        ("30-39", "M"): [5.0, 8.0, 12.0, 19.0],
        ("30-39", "F"): [5.0, 8.0, 12.0, 19.0],
    },
}

# Resting Heart Rate — lower is better
RHR = {
    "lower_is_better": True,
    "unit": "bpm",
    "cutoffs": {
        ("30-39", "M"): [58, 65, 74, 85],
        ("30-39", "F"): [60, 68, 76, 88],
    },
}

# Daily Steps — higher is better
DAILY_STEPS = {
    "lower_is_better": False,
    "unit": "steps/day",
    "cutoffs": {
        "universal": [4000, 6000, 8000, 10000],
    },
}

# Waist Circumference — lower is better
WAIST = {
    "lower_is_better": True,
    "unit": "inches",
    "cutoffs": {
        ("30-39", "M"): [33, 35, 38, 41],
        ("30-39", "F"): [28, 31, 35, 38],
    },
}

# Lp(a) — lower is better, genetically fixed
LPA = {
    "lower_is_better": True,
    "unit": "nmol/L",
    "cutoffs": {
        "universal": [30, 75, 125, 200],
    },
}

# Sleep Regularity — lower is better
SLEEP_REGULARITY = {
    "lower_is_better": True,
    "unit": "min std dev",
    "cutoffs": {
        "universal": [15, 30, 45, 60],
    },
}

# hs-CRP — lower is better
HSCRP = {
    "lower_is_better": True,
    "unit": "mg/L",
    "cutoffs": {
        ("30-39", "M"): [0.5, 1.0, 2.0, 5.0],
        ("30-39", "F"): [0.5, 1.0, 2.0, 5.0],
    },
}

# ALT — lower is better
ALT = {
    "lower_is_better": True,
    "unit": "U/L",
    "cutoffs": {
        ("30-39", "M"): [20, 30, 44, 60],
        ("30-39", "F"): [15, 25, 35, 50],
    },
}

# GGT — lower is better
GGT = {
    "lower_is_better": True,
    "unit": "U/L",
    "cutoffs": {
        ("30-39", "M"): [20, 30, 50, 80],
        ("30-39", "F"): [15, 25, 40, 65],
    },
}

# TSH — bidirectional (simplified as lower is better for cutoff table)
TSH = {
    "lower_is_better": True,
    "unit": "mIU/L",
    "cutoffs": {
        "universal": [2.5, 4.0, 6.0, 10.0],
    },
}

# Vitamin D — higher is better
VITAMIN_D = {
    "lower_is_better": False,
    "unit": "ng/mL",
    "cutoffs": {
        "universal": [15, 20, 30, 40],
    },
}

# Ferritin — higher is better (primary concern is deficiency)
FERRITIN = {
    "lower_is_better": False,
    "unit": "ng/mL",
    "cutoffs": {
        ("30-39", "M"): [20, 40, 80, 150],
        ("30-39", "F"): [10, 20, 40, 80],
    },
}

# Hemoglobin — higher is better (primary concern is anemia)
HEMOGLOBIN = {
    "lower_is_better": False,
    "unit": "g/dL",
    "cutoffs": {
        ("30-39", "M"): [12.0, 13.5, 14.5, 15.5],
        ("30-39", "F"): [10.5, 12.0, 13.0, 14.0],
    },
}

# VO2 Max — higher is better (ACSM classifications)
VO2_MAX = {
    "lower_is_better": False,
    "unit": "mL/kg/min",
    "cutoffs": {
        ("20-29", "M"): [35, 40, 46, 52],
        ("30-39", "M"): [33, 38, 44, 50],
        ("40-49", "M"): [31, 36, 42, 48],
        ("50-59", "M"): [28, 33, 39, 45],
        ("60-69", "M"): [24, 29, 35, 41],
        ("70+", "M"):   [20, 25, 31, 37],
        ("20-29", "F"): [30, 35, 40, 46],
        ("30-39", "F"): [28, 33, 38, 44],
        ("40-49", "F"): [25, 30, 35, 41],
        ("50-59", "F"): [22, 27, 32, 38],
        ("60-69", "F"): [19, 24, 29, 35],
        ("70+", "F"):   [16, 21, 26, 32],
    },
}

# HRV (RMSSD) — higher is better
HRV_RMSSD = {
    "lower_is_better": False,
    "unit": "ms (RMSSD)",
    "cutoffs": {
        ("20-29", "M"): [18, 25, 40, 60],
        ("30-39", "M"): [15, 22, 35, 55],
        ("40-49", "M"): [12, 18, 28, 45],
        ("50-59", "M"): [10, 15, 22, 38],
        ("60-69", "M"): [8, 12, 18, 30],
        ("70+", "M"):   [6, 10, 15, 25],
        ("20-29", "F"): [18, 25, 40, 60],
        ("30-39", "F"): [15, 22, 35, 55],
        ("40-49", "F"): [12, 18, 28, 45],
        ("50-59", "F"): [10, 15, 22, 38],
        ("60-69", "F"): [8, 12, 18, 30],
        ("70+", "F"):   [6, 10, 15, 25],
    },
}


# Coverage weights — reflects relative ROI
# Zone 2 Cardio (min/week) — higher is better
# AHA: 150 min/week moderate = meets guideline, 300+ = exceeds
# Universal (not age/sex stratified for this metric)
ZONE2_MIN = {
    "lower_is_better": False,
    "unit": "min/week",
    "cutoffs": {
        "universal": [60, 100, 150, 250],
    },
}


# These determine how much each metric contributes to the coverage score.
TIER1_WEIGHTS = {
    "blood_pressure": 8,
    "lipid_apob": 8,
    "metabolic": 8,
    "family_history": 6,
    "sleep": 6,         # Was 5. Phillips et al. — regularity > duration for mortality
    "steps": 4,
    "resting_hr": 4,
    "waist": 5,
    "medications": 3,   # Was 4. Information, not measurement. Slight reduction.
    "lpa": 8,
}

# Standing weights — used for the health standing composite score.
# Differs from coverage weight for metrics where "having the data" matters
# more than the ongoing score contribution (e.g., Lp(a) is genetically fixed).
TIER1_STANDING_WEIGHTS = {
    "blood_pressure": 8,
    "lipid_apob": 8,
    "metabolic": 8,
    "family_history": 6,
    "sleep": 6,
    "steps": 4,
    "resting_hr": 4,
    "waist": 5,
    "medications": 3,
    "lpa": 4,           # Coverage wt=8 (important to check), standing wt=4 (genetically fixed, can't act on it)
}

TIER2_WEIGHTS = {
    "vo2_max": 6,       # Was 5. Strongest modifiable all-cause mortality predictor (Mandsager, JAMA 2018)
    "hrv": 2,
    "hscrp": 3,
    "liver": 2,
    "cbc": 2,
    "thyroid": 2,
    "vitamin_d_ferritin": 3,
    "weight_trends": 2,
    "phq9": 2,
    "zone2": 2,
}

TIER2_STANDING_WEIGHTS = {
    "vo2_max": 6,
    "hrv": 2,
    "hscrp": 3,
    "liver": 2,
    "cbc": 2,
    "thyroid": 2,
    "vitamin_d_ferritin": 3,
    "weight_trends": 2,
    "phq9": 2,
    "zone2": 2,
}
