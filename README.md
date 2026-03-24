# Kiso (基礎)

The backend platform for [Kasane](https://kasanelife.com) and Milo.

Kiso is a health intelligence server that powers two clients: the Kasane iOS app (habits, coaching, focus plans) and Milo (an AI coaching agent on WhatsApp). Both read and write the same person profiles, habits, check-ins, and health data through a shared API.

The name means "foundation" in Japanese. Kasane means "layers." Kiso is what the layers sit on.

## What it does

**For the iOS app**: bidirectional sync of persons, habits, check-ins, focus plans, and health measurements via REST API.

**For Milo**: 40+ MCP tools for health coaching. Log weight, meals, labs, blood pressure. Pull data from Garmin, Apple Health, Oura, Whoop. Score 20 health metrics against NHANES population percentiles and clinical guidelines.

**For both**: a unified person context that merges Kasane data (SQLite) with health tracking data (CSVs) in one call.

## Quick start

```bash
git clone https://github.com/a-deal/kiso.git
cd kiso
pip install -e ".[gateway,dev]"

# Run the gateway
python3 -m uvicorn engine.gateway.server:create_app --factory --port 18800

# Run tests
python3 -m pytest tests/ -v
```

### MCP server (for Claude Desktop / Claude Code)

```json
{
  "mcpServers": {
    "kiso": {
      "command": "uvx",
      "args": ["kiso"]
    }
  }
}
```

## API surfaces

| Surface | Path | Client |
|---------|------|--------|
| Kasane sync | `POST /api/v1/sync` | iOS app |
| Kasane CRUD | `/api/v1/persons`, `/api/v1/habits`, etc. | iOS app |
| Person context | `GET /api/v1/persons/:id/context` | Milo |
| Health tools | `/api/{tool_name}` | Milo, iOS Shortcuts |
| Wearable auth | `/auth/garmin`, `/auth/google` | Browser (OAuth) |

Full API reference: [docs/API.md](docs/API.md)

## Architecture

One Python process. Two storage systems. Three clients.

- **SQLite** (`data/kasane.db`): persons, habits, check-ins, focus plans. Synced with iOS.
- **CSVs** (`data/`): weight, meals, labs, Garmin, supplements. Written by Milo's tools.
- **Bridge**: `person.health_engine_user_id` links a SQLite person to a CSV data directory.

Full architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Health scoring

20 clinically validated metrics scored against NHANES population data (300K+ Americans) and clinical guidelines from AHA, ADA, and ESC. Covers cardiovascular, metabolic, body composition, recovery, and lifestyle dimensions.

Going from 0% to full coverage costs under $300.

Full methodology: [docs/METHODOLOGY.md](docs/METHODOLOGY.md)

## Docs

| Doc | What's in it |
|-----|-------------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, storage model, deploy model, project layout |
| [API.md](docs/API.md) | v1 endpoints, sync protocol, data types |
| [ROADMAP.md](docs/ROADMAP.md) | Cloud adoption phases (JWT, Litestream, Supabase) |
| [METHODOLOGY.md](docs/METHODOLOGY.md) | Why each metric, evidence sources, clinical thresholds |
| [SCORING.md](docs/SCORING.md) | How the scoring engine works |
| [METRICS.md](docs/METRICS.md) | 20-metric catalog |
| [DATA_FORMATS.md](docs/DATA_FORMATS.md) | CSV/JSON schemas |
| [ONBOARDING.md](docs/ONBOARDING.md) | Setup walkthrough |

## License

MIT
