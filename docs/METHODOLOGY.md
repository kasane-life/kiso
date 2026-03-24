# Methodology: Why We Score Health This Way

This document explains the reasoning behind every scoring decision in Kiso. It's written for the curious user who asks "why this number?" and for the coach (Claude) who needs to explain it.

---

## The Core Problem

Most health data is interpreted one of two ways:

1. **Reference ranges** — "Your LDL is within normal limits." This tells you that you're not clinically sick. It doesn't tell you whether you're optimized or trending in the wrong direction.

2. **Population averages** — "You're at the 60th percentile." This compares you to the average American, who is 42% likely to be obese and 38% likely to be prediabetic. Being "better than average" in a sick population is a low bar.

Neither answer the question that actually matters: **Am I healthy, and what should I do about it?**

Our approach uses both signals, in the right order:

- **Clinical zones** (primary): Where does this value sit relative to evidence-based health thresholds? This answers "am I healthy?"
- **Population percentiles** (secondary): Where do I rank among my age/sex peers? This provides context — "healthier than X% of your cohort."

---

## Why Not Just Use Reference Ranges?

Lab reference ranges are designed to flag disease, not optimize health. They're set at the 2.5th and 97.5th percentiles of a "healthy" reference population — meaning 95% of people fall within "normal."

The problem: a fasting glucose of 99 mg/dL is "normal" but sits one point below the ADA prediabetes threshold of 100. A fasting glucose of 78 mg/dL is also "normal." These are not the same.

Clinical zones solve this by distinguishing **Optimal** (where you want to be) from **Healthy** (fine but not exceptional) from **Borderline** (early warning) from **Elevated** (needs attention). Each zone is sourced from clinical guidelines, not arbitrary cutoffs.

## Why Not Just Use NHANES Percentiles?

NHANES (National Health and Nutrition Examination Survey) is the best population-level health data in the United States — survey-weighted, nationally representative, continuous. We use it. But it has a fundamental limitation:

The 50th percentile = the median American. The median American is overweight (BMI 30.0), has borderline high LDL (119 mg/dL), has prediabetic fasting glucose (100 mg/dL), and sleeps 6.8 hours. Being at the 60th percentile for fasting glucose means you're "better than average" while being metabolically suboptimal.

Percentiles tell you **where you rank**. Clinical zones tell you **whether you're healthy**. You need both.

---

## The Two Scores

### Coverage Score: "Do you have the data?"

Not all health metrics are equally important. We weight them by **evidence strength x actionability**:

| Weight | Meaning | Examples |
|--------|---------|---------|
| 8 | Essential, strong mortality data, directly actionable | Blood pressure, ApoB, fasting insulin, Lp(a) |
| 5-6 | Important, good evidence, moderately actionable | Waist, sleep regularity, VO2 max |
| 3-4 | Useful context or screening | Medications, hs-CRP, vitamin D |
| 2 | Helpful signal, lower individual impact | HRV, liver enzymes, PHQ-9 |

Your coverage score is the weighted percentage of metrics you've actually measured. This creates a natural prioritization: the gap analysis shows you what to measure next, ranked by leverage.

**Why coverage matters:** You can't improve what you don't measure. A user with 40% coverage has blind spots — potentially missing elevated Lp(a) (20% of people have it, invisible on standard panels) or early insulin resistance (fasting insulin catches it 10-15 years before glucose moves).

### Assessment Score: "Where do you stand?"

For metrics with data, we compute a weighted average percentile using **standing weights** — which differ from coverage weights for one key metric:

**Lp(a) bifurcation:** Lp(a) has a coverage weight of 8 (it's critically important to check — 20% of people are elevated and don't know it) but a standing weight of 4 (it's genetically fixed, so a high Lp(a) shouldn't permanently crush your health score when you can't change it). The score reflects what you can act on.

---

## Clinical Zones: The Primary Signal

Each metric is assessed against evidence-based thresholds from major clinical guidelines:

### Blood Pressure (AHA/ACC 2017 Hypertension Guidelines)

| Zone | Systolic | What it means |
|------|----------|---------------|
| Optimal | <120 mmHg | Low cardiovascular risk |
| Healthy | 120-129 | Elevated — not hypertension, but worth monitoring |
| Borderline | 130-139 | Stage 1 hypertension — lifestyle intervention |
| Elevated | >=140 | Stage 2 hypertension — clinical follow-up |

**Why this matters:** Each 20 mmHg increase above 115 systolic doubles cardiovascular mortality. The relationship is log-linear and begins well below "normal" ranges. A reading of 135 feels fine. It's not.

### Lipids: ApoB over LDL-C (ESC/EAS 2019)

| Zone | ApoB | What it means |
|------|------|---------------|
| Optimal | <80 mg/dL | Below ESC primary prevention target |
| Healthy | 80-100 | Within reference range |
| Borderline | 100-120 | Exceeds prevention target |
| Elevated | >120 | Significant atherosclerotic CVD risk |

**Why ApoB, not LDL-C?** LDL-C measures cholesterol mass. ApoB counts atherogenic particles — each LDL, VLDL, and Lp(a) particle carries exactly one ApoB molecule. When triglycerides are elevated, LDL particles become small and dense: more particles carrying less cholesterol each. LDL-C looks normal while particle count (and risk) is high. ApoB catches this. Mendelian randomization studies confirm ApoB is a stronger causal predictor than LDL-C.

We score ApoB when available, LDL-C as fallback. If you have both, ApoB wins.

### Metabolic Panel: Fasting Insulin First (ADA 2023 + Literature Consensus)

| Metric | Optimal | Healthy | Borderline | Elevated | Source |
|--------|---------|---------|------------|----------|--------|
| Fasting Glucose | <90 | 90-99 | 100-125 | >=126 | ADA 2023 |
| HbA1c | <5.2% | 5.2-5.6% | 5.7-6.4% | >=6.5% | ADA 2023 |
| Fasting Insulin | <5 | 5-12 | 12-18 | >18 | Literature consensus |

**Why fasting insulin first?** Glucose is the last thing to move in insulin resistance. The progression:

1. Insulin rises to compensate for cellular resistance (years before glucose moves)
2. HbA1c drifts upward (the 3-month average catches what fasting glucose misses)
3. Fasting glucose finally breaks 100 (prediabetes diagnosed, but the process started years ago)

Fasting insulin catches phase 1. Most standard panels only test glucose (phase 3). A person with glucose 92 and insulin 15 has a "normal" glucose result and active insulin resistance. We score insulin when available, HbA1c second, glucose last.

### Inflammation: hs-CRP (AHA/CDC Scientific Statement)

| Zone | hs-CRP | What it means |
|------|--------|---------------|
| Optimal | <1.0 mg/L | Low cardiovascular inflammation risk |
| Healthy | 1.0-2.0 | Average risk |
| Borderline | 2.0-3.0 | High risk category |
| Elevated | >3.0 | Very high — rule out acute infection first |

**Important caveat:** hs-CRP has 42% within-subject biological variation (CVI). A single reading is directional, not definitive. This is why we apply a reliability multiplier of 0.6 to single hs-CRP readings. Two readings 2+ weeks apart get full credit. See [Reliability](#reliability-why-some-readings-count-less) below.

### Thyroid: TSH (Endocrine Society)

TSH is bidirectional — both high and low are concerning:

| Zone | TSH | What it means |
|------|-----|---------------|
| Optimal | 0.5-2.5 mIU/L | Optimal thyroid function |
| Healthy | 2.5-4.0 | Within reference range |
| Borderline | 4.0-10.0 | Subclinical hypothyroidism — recheck in 6-12 weeks |
| Elevated | >10.0 | Overt hypothyroidism |
| Low flag | <0.4 | Suggests hyperthyroidism |

12% lifetime prevalence. Highly treatable. Often missed because symptoms (fatigue, weight gain, brain fog) overlap with everything else.

### VO2 Max (ACSM Guidelines)

VO2 max is the strongest modifiable predictor of all-cause mortality (Mandsager et al., JAMA 2018). A person in the bottom 25% of fitness has 4x the mortality risk of someone in the top 25%. Moving from "low" to "below average" fitness provides a larger mortality reduction than quitting smoking.

Thresholds are age and sex stratified (a 35-year-old male needs >50 mL/kg/min for "Optimal"; a 55-year-old female needs >38).

### Additional Metrics

All clinical thresholds are documented in `engine/scoring/clinical.py` with source citations. Key metrics not detailed above:

- **Vitamin D** (Endocrine Society 2011): >40 optimal, 20-30 insufficient, <20 deficient. 42% of US adults deficient.
- **Waist** (NHLBI/AHA): Men >40", Women >35" = metabolic syndrome criterion
- **Lp(a)** (EAS consensus): <30 nmol/L optimal, >125 elevated. Genetically fixed — measure once.
- **Ferritin, Hemoglobin, ALT, GGT**: Sex-stratified thresholds from clinical consensus

---

## Freshness: Why Data Ages

A lipid panel from 18 months ago doesn't mean the same thing as one from last week. Biology changes. The scoring engine applies a **freshness decay** to each metric based on when it was measured.

### The Decay Model

```
Freshness = 1.0            (within fresh window)
Freshness = linear decay   (between fresh and stale window)
Freshness = 0.0            (beyond stale window)
```

### Per-Metric Windows

| Metric | Fresh (full credit) | Stale (decays to 0) | Why |
|--------|--------------------|--------------------|-----|
| ApoB, LDL-C, HDL-C, HbA1c | 6 months | 18 months | Low CVI (2-8%), slow biological drift |
| Fasting Glucose | 3 months | 12 months | Moderate CVI (5.6%), can shift with diet changes |
| Fasting Insulin | 3 months | 9 months | High CVI (21-25%), pulsatile secretion |
| Triglycerides | 3 months | 9 months | High CVI (19.9%), strongly affected by recent meals |
| Blood Pressure (single) | 1 month | 6 months | Moderate CVI (6-8%), circadian + situational variation |
| Blood Pressure (7-day avg) | 3 months | 12 months | Averaging reduces effective CVI to ~2-3% |
| hs-CRP | 6 months | 12 months | Very high CVI (42.2%) — already reliability-gated |
| Wearable metrics | 7 days | 30 days | Continuous data; freshness = recency of sync |
| Lp(a) | Lifetime | Never | 70-90% genetically determined, ~10% CVI |
| Family History | Lifetime | Never | Doesn't change |

### What CVI Means

CVI (Coefficient of Variation, within-subject) is how much a metric naturally fluctuates in the same healthy person over time. A CVI of 20% means if your "true" fasting insulin is 8, individual readings will range from roughly 6.4 to 9.6 — not because anything changed, but because biology is noisy.

High-CVI metrics need:
- **More frequent measurement** (shorter fresh windows)
- **Multiple readings averaged** (reliability multipliers)
- **Cautious interpretation** of single values

Low-CVI metrics (HbA1c at 1.9%, Lp(a) at ~10%) are stable — a single reading carries real signal and stays valid longer.

### Why This Matters for Users

Freshness creates a natural reason to come back. A user with 80% coverage from year-old labs has a decaying score — "Your ApoB from 11 months ago is at 82% credit. A retest would refresh it to 100%." This is honest: old data really is less informative. It's also a retention mechanism that respects the user — the nudge is grounded in biology, not artificial gamification.

---

## Reliability: Why Some Readings Count Less

Not all measurements are equally trustworthy. A single blood pressure reading varies by 20+ mmHg through the day. A single hs-CRP reading has 42% biological variation. Scoring these at full confidence would be dishonest.

### Reliability Multipliers

| Metric | Single reading | Multiple readings | Protocol (7-day) | Why |
|--------|---------------|-------------------|-------------------|-----|
| Blood Pressure | 0.5 | 0.75 | 1.0 | AHA recommends 7-day average for clinical decisions |
| hs-CRP | 0.6 | 1.0 | — | 42% CVI; single reading is directional only |
| Fasting Insulin | 0.7 | 1.0 | — | 21-25% CVI; pulsatile secretion pattern |
| Triglycerides | 0.7 | 1.0 | — | 19.9% CVI; fasting compliance varies |
| Vitamin D | 0.7 (opposite season) | 1.0 | — | 30% seasonal variation |
| All others | 1.0 | — | — | CVI low enough for single reading |

### Effective Weight

The actual contribution of a metric to your coverage score is:

```
effective_weight = base_weight x freshness x reliability
```

A single BP reading from 2 months ago: `8 x 1.0 x 0.5 = 4.0` (half credit).
A 7-day BP protocol from last week: `8 x 1.0 x 1.0 = 8.0` (full credit).
An hs-CRP from 10 months ago (single reading): `3 x 0.67 x 0.6 = 1.2` (low credit).

This means a user who does a 7-day BP protocol and gets two hs-CRP readings will have meaningfully higher coverage than one who checks BP once and has one hs-CRP draw — reflecting the real difference in data quality.

---

## Cross-Metric Patterns: What Individual Scores Miss

Some of the most important health signals are invisible when metrics are scored independently. The engine detects four compound patterns:

### Metabolic Syndrome

**Criteria (NCEP ATP III harmonized):** >= 3 of 5:
1. Triglycerides >= 150 mg/dL
2. HDL < 40 (M) or < 50 (F) mg/dL
3. Fasting glucose >= 100 mg/dL
4. Waist > 40" (M) or > 35" (F)
5. Blood pressure >= 130/85 mmHg

**Why it matters:** Each criterion individually might rate as "Borderline" — not alarming on its own. But three or more together indicate metabolic syndrome, which carries 2x cardiovascular risk and 5x diabetes risk. The whole is worse than the sum of the parts.

### Atherogenic Dyslipidemia

**Signal:** TG/HDL ratio > 3.5 with triglycerides >= 130

**Why it matters:** This ratio is a proxy for small dense LDL particle predominance. A person with "normal" LDL-C of 110 but TG/HDL ratio of 4.0 likely has elevated ApoB and increased particle-driven risk. The pattern suggests ApoB testing if not already done.

### Insulin Resistance Pattern

**Signal:** Fasting insulin > 12 with glucose still < 100

**Why it matters:** This is the pattern that standard panels miss entirely. Glucose is the last domino to fall in insulin resistance — the pancreas compensates by producing more insulin for years before glucose finally rises. A fasting insulin of 15 with glucose of 92 looks "normal" on a standard metabolic panel. It's not — the pancreas is working overtime.

### Recovery Stress

**Signal:** 2+ of: HRV < 55ms, RHR > 58bpm, sleep < 6.5 hours

**Why it matters:** These three wearable signals compound. Low HRV + elevated RHR + short sleep = the body is accumulating physiological stress. Any one alone is a mild flag; two or more together indicate a recovery deficit that shows up as plateaus, illness susceptibility, or injury risk. Especially relevant during a caloric deficit or high training load.

---

## Weight Adjustments: Why Some Metrics Matter More

### VO2 Max: Upgraded to Weight 6

VO2 max is the strongest modifiable predictor of all-cause mortality. Mandsager et al. (JAMA 2018) showed that low cardiorespiratory fitness carries greater mortality risk than smoking, diabetes, or coronary artery disease. Moving from "low" to "above average" fitness reduces all-cause mortality more than any pharmaceutical intervention. It was underweighted at 5; now 6.

### Sleep Regularity: Upgraded to Weight 6

Phillips et al. (Sleep, 2017) and Windred et al. (UK Biobank) demonstrated that sleep regularity predicts mortality independent of sleep duration. Irregular sleepers who average 7+ hours still have elevated risk. Regular sleepers who average 6.5 hours fare better than irregular 8-hour sleepers. Was 5; now 6.

### Lp(a): Bifurcated Weights

Lp(a) is 70-90% genetically determined. You can't change it. But you need to know if you have it — 20% of people are elevated, it's invisible on standard panels, and it carries 2-3x cardiovascular risk. So:

- **Coverage weight: 8** — high priority to check
- **Standing weight: 4** — reduced impact on health score (can't act on it)

A person with elevated Lp(a) shouldn't have a permanently depressed health score. They should know about it (coverage), manage around it (more aggressive lipid targets), and not be penalized for genetics (standing).

### Medications: Reduced to Weight 3

Medication list is context, not measurement. It matters for interpretation (statins affect lipid scoring, metformin affects glucose) but doesn't carry independent health signal. Reduced from 4 to 3.

---

## Population Data: NHANES

We use NHANES 2017-March 2020 Pre-Pandemic as our primary percentile source. This is the most recent nationally representative health survey not affected by COVID-era disruptions.

**Why pre-pandemic?** The March 2020 cutoff excludes pandemic-era data where sampling was disrupted, healthcare access changed, and population health metrics shifted temporarily. Pre-pandemic data gives a more stable baseline.

**How percentiles work:** For each metric, NHANES provides survey-weighted percentile tables stratified by age group, sex, and ethnicity. We use linear interpolation between table values to produce continuous percentiles (not just 25th/50th/75th).

**What "70th percentile" means:** You scored higher than 70% of Americans in your age/sex cohort on this metric. Remember: this is a population that is 42% obese and 38% prediabetic. Being at the 50th percentile doesn't mean you're healthy — it means you're average in a population with significant metabolic disease burden.

**That's why clinical zones come first.** A fasting glucose at the NHANES 60th percentile might be 95 mg/dL — "better than most Americans" but approaching the prediabetic threshold. The clinical zone says "Healthy (90-99)" while the percentile says "60th." Both are true. The clinical zone is more useful for decision-making.

---

## What We Don't Score (And Why)

**BMI:** Waist circumference is a better proxy for visceral fat and metabolic risk. BMI conflates muscle mass with fat mass. A lean, muscular person at BMI 28 has different risk than a sedentary person at BMI 28. Waist catches what BMI misses.

**Total Cholesterol:** Replaced by ApoB as the primary lipid marker. Total cholesterol includes HDL (which is protective), making it a noisy signal. LDL-C is better; ApoB is best.

**Heart Rate Variability alone:** HRV is a recovery/stress signal, not a standalone health metric in the way BP or ApoB are. We score it (Tier 2, weight 2) but don't overweight it. It's most valuable in the recovery stress pattern where it combines with RHR and sleep.

**Testosterone/Cortisol/DHEA:** Tracked in lab results but not scored. Reference ranges are wide, clinical significance of "optimization" is debated, and the evidence for population-level health impact doesn't match Tier 1/2 metrics. May be added in future tiers.

**Genetics/Genomics:** Outside current scope. Lp(a) is the one genetic marker we score because it's actionable (changes lipid management targets) and measurable with a standard blood test.

---

## Sources

### Clinical Guidelines
- AHA/ACC 2017 Hypertension Guidelines
- ESC/EAS 2019 Dyslipidaemia Guidelines
- ADA Standards of Medical Care 2023
- Endocrine Society Clinical Practice Guidelines (Thyroid, Vitamin D)
- AHA/CDC Scientific Statement on hs-CRP
- ACSM Guidelines for Exercise Testing and Prescription
- NCEP ATP III Metabolic Syndrome Criteria
- ACG 2017 Clinical Guidelines (Liver Enzymes)

### Key Studies
- Mandsager et al., JAMA 2018 — VO2 max and mortality
- Paluch et al., Lancet 2022 — Steps and mortality dose-response
- Windred et al., UK Biobank — Sleep regularity and mortality
- Phillips et al., Sleep 2017 — Irregular sleep and cardiometabolic risk
- JUPITER Trial — hs-CRP and statin benefit
- Copenhagen City Heart Study — RHR and mortality
- Kraft, Reaven — Insulin resistance progression model
- INTERHEART Study — Family history and CVD risk

### Biological Variation Data
- Westgard Desirable Biological Variation Database
- Ricos et al. — Within-subject biological variation coefficients
- Fraser & Harris — Biological variation in clinical chemistry
- PLOS ONE 2024 meta-analysis — hs-CRP biological variation (60 studies)

### Population Data
- NHANES 2017-March 2020 Pre-Pandemic (CDC/NCHS)
