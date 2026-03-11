# Health Engine

Open-source health scoring engine that runs locally with Claude. 20 clinically validated metrics, NHANES population percentiles, wearable integration, visual dashboard. Your data never leaves your machine.

**Score → Body → Actions.** Check in, see where you stand, know what to do next. Repeat.

## Get Started (2 minutes)

### Option A: Install from PyPI (recommended)

```bash
uvx health-engine
```

Add to your Claude Desktop or Claude Code config:

```json
{
  "mcpServers": {
    "health-engine": {
      "command": "uvx",
      "args": ["health-engine"]
    }
  }
}
```

Then say **"set me up"** in any Claude conversation. Done.

### Option B: Clone and run

```bash
git clone https://github.com/a-deal/health-engine.git
cd health-engine
pip install -e .
```

Add to Claude config:

```json
{
  "mcpServers": {
    "health-engine": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/health-engine", "python3", "-m", "mcp_server"]
    }
  }
}
```

## What It Does

You talk to Claude. It knows your health data.

- **"How am I doing?"** — Full coaching read: scores, trends, compound patterns, one actionable nudge
- **"Show me the dashboard"** — Visual snapshot: coverage ring, clinical zones, wearable trends, gap analysis
- **"Set me up"** — Guided walkthrough of all 20 health dimensions, what's tracked vs missing, ranked next steps
- **"I weighed 192 this morning"** — Logged. Trend updated. No forms.
- **"What should I measure next?"** — Gap analysis ranked by leverage and cost

Every conversation picks up where the last one left off. Same scores, same trends, same progress.

## Dashboard

Open the dashboard from any Claude conversation: **"show me the dashboard"**

Three sections, progressive reveal:

1. **Your Score** — Coverage ring, assessment percentile, compound coaching read
2. **Your Body** — Recovery trends (RHR, HRV, sleep), body composition, movement, nutrition, habits
3. **Your Actions** — Next 3 moves ranked by impact, coaching insights, compound pattern alerts

The dashboard shows the complete 20-metric picture — measured metrics with clinical zones and trends, unmeasured metrics with cost-to-close. It reads `briefing.json` locally. No server required.

## Scoring

Five pillars:

**Clinical zones** (Optimal / Healthy / Borderline / Elevated) from AHA, ADA, and ESC guidelines — the same thresholds your cardiologist uses.

**Population percentiles** from NHANES (300K+ Americans). The 50th percentile = median American — 42% obese, 38% prediabetic. Better than average is a low bar.

**Freshness decay** — a lipid panel from 18 months ago gets ~33% credit. Old data shouldn't anchor your current picture.

**Reliability weighting** — single readings of noisy metrics (BP, hs-CRP, fasting insulin) count less than averaged readings.

**Cross-metric patterns** — metabolic syndrome, insulin resistance, atherogenic dyslipidemia, recovery stress. Compound signals are often more important than any single number.

Full methodology: [docs/METHODOLOGY.md](docs/METHODOLOGY.md)

## 20 Scored Metrics

| Tier | Metrics |
|------|---------|
| **Foundation** | BMI, Blood Pressure, Fasting Glucose, Total Cholesterol |
| **Standard** | LDL-C, HDL-C, Triglycerides, HbA1c, Waist Circumference |
| **Advanced** | ApoB, Fasting Insulin, hs-CRP, Lp(a) |
| **Wearable** | Resting HR, HRV, VO2 Max, Sleep Duration, Sleep Regularity, Daily Steps, Zone 2 Minutes |

## Wearable Integration

**Garmin Connect** — pulls RHR, HRV, sleep, steps, VO2 max, zone 2 minutes automatically:

```bash
python3 cli.py auth garmin        # One-time interactive auth (no plaintext creds)
python3 cli.py pull garmin        # Pull latest data
```

**Apple Health** — import from iPhone/Apple Watch export:

```bash
python3 cli.py import apple-health /path/to/export.zip
```

Parses RHR, HRV (SDNN), steps, VO2 max, and sleep via SAX streaming. Handles large exports.

## MCP Tools

When connected to Claude, 14 tools are available:

| Tool | What it does |
|------|-------------|
| `checkin` | Full coaching briefing — scores, insights, weight, nutrition, habits, wearable data |
| `score` | Deep-dive: coverage %, NHANES percentiles for 20 metrics, tier breakdown, gap analysis |
| `onboard` | 20-metric coverage map, wearable connection status, ranked next steps |
| `get_protocols` | Active protocol progress — day, week, phase, habits, nudges |
| `log_weight` | Log a weight measurement |
| `log_bp` | Log blood pressure |
| `log_habits` | Log daily habits |
| `log_meal` | Log a meal with macros |
| `connect_garmin` | Check Garmin connection status |
| `open_dashboard` | Open the visual health dashboard in a browser |
| `setup_profile` | Create or update user profile |
| `get_status` | Data files inventory — what exists, last modified, row counts |

Plus a methodology resource (`health-engine://methodology`) that explains every scoring decision in plain language.

## CLI

```bash
python3 cli.py score              # Score profile, show gaps
python3 cli.py briefing           # Full coaching snapshot (JSON)
python3 cli.py insights           # Health insights with explanations
python3 cli.py status             # What data exists, when last updated
python3 cli.py pull garmin --history --workouts  # 90-day trends + workouts
```

## Use as a Library

```python
from engine.models import Demographics, UserProfile
from engine.scoring.engine import score_profile

profile = UserProfile(
    demographics=Demographics(age=35, sex="M"),
    resting_hr=52, hrv_rmssd_avg=62, vo2_max=47,
)
output = score_profile(profile)
print(f"Coverage: {output['coverage_score']}%")
```

## Tests

```bash
python3 -m pytest tests/ -v   # 121 tests
```

## Project Structure

```
engine/
├── scoring/           # 20 metrics × NHANES percentiles × clinical zones
├── insights/          # Rule-based coaching + compound pattern detection
├── coaching/          # Briefing builder, protocol engine
├── integrations/      # Garmin Connect API, Apple Health XML parser
├── tracking/          # Weight, nutrition, strength, habits
└── data/              # NHANES percentile tables, methodology (ships with package)

mcp_server/            # MCP server (FastMCP) — 14 tools + methodology resource
dashboard/             # Visual health dashboard (single-file HTML, reads briefing.json)
```

## Docs

- [METHODOLOGY.md](docs/METHODOLOGY.md) — Why we score each metric, evidence sources, clinical thresholds
- [SCORING.md](docs/SCORING.md) — How the scoring engine works
- [METRICS.md](docs/METRICS.md) — 20 metrics with evidence
- [COVERAGE.md](docs/COVERAGE.md) — Path to 100% coverage, cost breakdown
- [ONBOARDING.md](docs/ONBOARDING.md) — Setup walkthrough
- [DATA_FORMATS.md](docs/DATA_FORMATS.md) — CSV/JSON schemas

## License

MIT
