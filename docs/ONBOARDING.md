# Onboarding — Get Running in 5 Minutes

## How It Works

Kiso is an always-on health layer, not a separate app. Once set up, it's available in every Claude conversation — Claude Desktop, Claude Code, any MCP client. Your data lives locally and persists across chats.

There are two moments:

1. **First time (onboard)** — "Set me up." Claude walks you through all 20 health dimensions, collects your basics, and shows you exactly where to start. Takes 5 minutes.
2. **Every time after (check-in)** — "How am I doing?" Claude reads your latest data and coaches you forward. Each new chat picks up where the last left off.

You don't need to re-onboard. The `onboard` tool is for first-time setup and periodic reassessment ("what am I still missing?"). Daily use is just `checkin`.

## Prerequisites

- Python 3.11+ (with [uv](https://docs.astral.sh/uv/) recommended)
- A Garmin Connect account (optional, for wearable data)

## Step 1: Install

```bash
git clone https://github.com/a-deal/kiso.git
cd kiso
uv sync                          # or: python3 -m pip install -e .
uv sync --extra garmin           # optional: Garmin integration
```

## Step 2: Connect to Claude

Add to your MCP config (`~/.mcp.json` for Claude Code, or Claude Desktop settings):

```json
{
  "mcpServers": {
    "kiso": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/kiso", "python3", "-m", "mcp_server.server"],
      "cwd": "/path/to/kiso"
    }
  }
}
```

Restart Claude. That's it — the tools are now available in every conversation.

## Step 3: Say "Set me up"

Claude calls the `onboard` tool and walks you through:

- **Your coverage map** — all 20 health dimensions, scored or missing
- **What you have** — any data it detects (Garmin, weight logs, lab results)
- **What you're missing** — ranked by leverage, with exact cost and effort
- **Your basics** — age, sex, family history, medications (collected conversationally via `setup_profile`)

After onboarding, your `config.yaml` has your profile and your `data/` directory is ready for incoming data.

## Step 4: Start Tracking

The easiest wins after onboarding:

| What | How | Coverage boost |
|------|-----|---------------|
| Family history | "No family history of early heart disease" | +7% |
| Waist measurement | "My waist is 34 inches" | +6% |
| Blood pressure | Buy an Omron cuff ($40), log a reading | +9% |
| Medications | "I take vitamin D and creatine" | +5% |
| Weight | "I weighed 192 this morning" | +2% |

Each of these is free or nearly free and can be logged conversationally — just tell Claude the number.

## Step 5: Pull Garmin Data (optional)

```bash
# Set credentials (first time only)
export GARMIN_EMAIL="you@example.com"
export GARMIN_PASSWORD="your-password"

# Pull metrics
python3 cli.py pull garmin

# Pull with 90-day trends + workout details
python3 cli.py pull garmin --history --workouts
```

Or tell Claude: "Pull my Garmin data" (if credentials are in config).

## After Onboarding

Every new conversation, just talk:

- **"How am I doing?"** → Full coaching snapshot
- **"192 this morning"** → Weight logged, trend updated
- **"128/82"** → BP logged
- **"What should I measure next?"** → Gap analysis with ranked priorities
- **"Show me my scores"** → Deep dive into all 20 dimensions

No commands to remember. No dashboard to check. The data is always there, the conversation is the interface.

## Data Files

All personal data lives in `data/` (gitignored). Supported formats:

| File | Format | Description |
|------|--------|-------------|
| `garmin_latest.json` | JSON | Latest Garmin metrics (auto-created by `pull`) |
| `garmin_daily.json` | JSON | Daily RHR/HRV/steps series (auto-created by `pull --history`) |
| `weight_log.csv` | CSV | Daily weigh-ins: `date,weight_lbs,source` |
| `meal_log.csv` | CSV | Meal entries: `date,time_of_day,description,protein_g,carbs_g,fat_g,calories` |
| `strength_log.csv` | CSV | Lift entries: `date,exercise,weight_lbs,reps,rpe,notes` |
| `bp_log.csv` | CSV | BP readings: `date,systolic,diastolic` |
| `daily_habits.csv` | CSV | Habit tracking: `date,habit1,habit2,...` (y/n values) |
| `lab_results.json` | JSON | Lab draws with biomarker values |

## Customizing Thresholds

Edit `engine/insights/rules.yaml` to adjust insight thresholds for your needs:

```yaml
hrv:
  critical_low: 45   # your personal threshold
  warning_low: 50
  healthy_high: 60
```

## CLI (for power users)

```bash
python3 cli.py score                 # Score profile (shows gaps)
python3 cli.py insights              # Generate health insights
python3 cli.py briefing              # Full coaching snapshot as JSON
python3 cli.py status                # Check what data files exist
```
