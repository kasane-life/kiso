# Getting to 100% Coverage

## The Problem

Your health data is everywhere and nowhere. Lab results sit in email attachments. Wearable data lives in siloed apps. Nobody — not you, not your doctor, not any AI — has a complete, structured picture of your health.

The average American has strong opinions about their LDL but has never tested fasting insulin, Lp(a), or ApoB — the markers with the strongest causal evidence for the things that actually kill people. "Normal" on a standard panel means "typical for a population where 96 million people are prediabetic and half of hypertensives don't know it."

Average is not healthy. The engine doesn't grade on a curve — it shows you where you actually stand.

## The Coverage Score

The engine tracks 40+ biomarkers across 20 scored dimensions spanning blood panels, wearable data, vital signs, and self-report. Your **coverage score** tells you what percentage of the picture you've filled in. The **gap analysis** tells you exactly what's missing, ranked by how much each gap costs to close.

You don't need everything on day one. Start with what you have.

## The Path to 100%

| Coverage | What You Add | Cost | Time |
|---|---|---|---|
| **~20%** | Connect a wearable (Garmin, Oura, Apple Watch, WHOOP) | Already own one? You're here. | 5 min |
| **~35%** | Bathroom scale + blood pressure cuff | ~$60 total | 10 min setup |
| **~75%** | One blood draw (lipid panel, metabolic, CBC, thyroid, hs-CRP) | $100-200 | 30 min at Quest |
| **~90%** | Waist circumference + family history + medication list | Free | 15 min |
| **100%** | PHQ-9 mental health screen | Free | 3 min questionnaire |

Going from 0% to 90% costs under $300 and about an hour of your time. Most of it is stuff you can do from your couch.

## Gear Guide

### Wearable (~20% coverage)

Any watch with an optical heart rate sensor gives you 5 metrics automatically: resting heart rate, HRV, sleep duration + regularity, daily steps, and zone 2 cardio minutes.

| Device | What You Get | Notes |
|---|---|---|
| **Garmin** (Venu, Forerunner, Fenix) | RHR, HRV, sleep, steps, VO2 max, zone 2, workouts, daily burn | Built-in API integration. Best data depth. |
| **Oura Ring** | RHR, HRV, sleep, steps, SpO2 | Best for sleep tracking. Personal access token makes API integration easy. |
| **Apple Watch** | RHR, HRV, sleep, steps, VO2 max | Largest market share. Export via Apple Health XML. |
| **WHOOP** | RHR, HRV, sleep, strain | Recovery-focused. CSV export. |
| **Fitbit** | RHR, sleep, steps | Good entry point. JSON export. |

You don't need the most expensive model. A Garmin Forerunner 55 or Venu Sq does everything the scoring engine needs.

### Blood Pressure Cuff (~8% coverage)

Blood pressure is the #1 modifiable cardiovascular risk factor. Each 20 mmHg increase above 115 systolic doubles CVD mortality. A single office reading is noisy — home monitoring is far more useful.

**Recommended:** Omron home cuff (~$40). Take 3 readings in the morning, log the average. The engine scores against NHANES population percentiles, not the simplified "120/80 is normal" threshold.

47% of Americans have hypertension. Half don't know it. A $40 cuff closes that blind spot.

### Digital Scale (~4% coverage)

Any accurate scale works. The engine doesn't care about a single weigh-in — it computes 7-day rolling averages, weekly rate of change, and projects trend lines. Daily weight fluctuates 2-4 lbs from water alone. The rolling average cuts through the noise.

Weigh daily, same time (morning, before eating), and log it. The engine does the math.

### Tape Measure (~5% coverage)

Waist circumference is a better predictor of metabolic risk than BMI. Measure at the navel, first thing in the morning, standing relaxed. M >40in / F >35in = elevated visceral fat risk.

Cost: $3. Time: 30 seconds.

### Blood Work (~40% coverage)

This is the single biggest jump in coverage. One draw covers 8 scored metrics: lipid panel + ApoB, metabolic panel (glucose, HbA1c, fasting insulin), CBC, thyroid (TSH), hs-CRP, liver enzymes, ferritin, and Lp(a).

**Where to get it:**

| Provider | What You Get | Cost | Notes |
|---|---|---|---|
| **Function Health** | 100+ biomarkers, 2x/year | $499/yr | Most comprehensive. Includes everything below plus hormones, heavy metals, vitamins. Best for people who want the full picture without thinking about it. |
| **Quest Diagnostics** (walk-in) | Order individual tests | $30-200 depending on panel | Order online, walk into any Quest location. No doctor required. A la carte — you pick exactly what you need. |
| **Your doctor** | Standard panels via insurance | Copay | Ask specifically for fasting insulin, ApoB, and Lp(a) — they're rarely included in standard orders but are the most informative markers. |
| **Ulta Lab Tests / Walk-In Lab** | Online ordering, Quest/Labcorp draw | $30-150 | Similar to Quest direct but sometimes better pricing on bundled panels. |

**Priority order for lab markers** (if you're ordering a la carte):

1. **Lipid panel + ApoB** ($30-50) — ApoB outperforms LDL-C for cardiovascular risk. Mendelian randomization confirms causality.
2. **Fasting insulin** ($15-30) — Catches insulin resistance 10-15 years before glucose goes abnormal. Your glucose can look "normal" while your insulin has been screaming for a decade.
3. **Lp(a)** ($30, once in your lifetime) — Genetically determined. 20% of people have elevated Lp(a). Invisible on standard panels. 2-3x cardiovascular risk. One test, done forever.
4. **hs-CRP** ($15-25) — Systemic inflammation marker. Low levels = good sign your body isn't fighting something chronic.
5. **CBC + metabolic panel** ($20-40) — Baseline blood health. Hemoglobin, glucose, liver enzymes, kidney function.
6. **TSH** ($15-25) — Thyroid function. Subclinical dysfunction is common and treatable.
7. **HbA1c** ($20-30) — 3-month blood sugar average. More stable than a single fasting glucose.
8. **Ferritin** ($15-25) — Iron stores. Too low = fatigue, poor recovery. Too high = inflammation signal.

### Family History (6% coverage, free)

The poor man's genetic test. Parental cardiovascular disease before age 60 approximately doubles your risk. The engine uses it as a context signal — it changes how every other metric gets interpreted.

Have a 10-minute conversation with your parents. Ask about heart disease, stroke, diabetes, and cancer in first-degree relatives. Log it once, it never changes.

### Medication List (4% coverage, free)

The most underrated health metric isn't a biomarker — it's your medication list. Without it, every other number gets misread. A low RHR on a beta-blocker means something completely different than a low RHR from cardiovascular fitness. Statins change your lipid picture. Thyroid meds change your TSH.

5 minutes to log. Essential context for everything else.

### PHQ-9 Mental Health Screen (3% coverage, free)

A validated 9-question depression screening tool. Scores 0-27. Takes 3 minutes. Mental health is health — the engine includes it because leaving it out creates a blind spot.

## The Full Picture

With all 20 metrics filled in, you get:
- A **percentile** for every metric against NHANES population data (real CDC survey data, not arbitrary "good/bad" ranges)
- A **standing** (Optimal, Good, Average, Below Average, Concerning) based on where you fall
- **Insight rules** that flag compound effects — not just "your HRV is low" but "your HRV is dropping while you're in a caloric deficit and your sleep regularity is poor, which means recovery is compromised"
- A **gap analysis** showing what you're still missing, ranked by information value

Run `python3 cli.py score` to see exactly where you stand and what to do next.
