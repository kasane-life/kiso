"""Focus plan generation endpoint with cited habits.

Standalone endpoint for Kasane iOS to generate a focus plan server-side
with verified citations from the habit catalogue. This satisfies
Apple Guideline 1.4.1 (Safety - Physical Harm).

POST /api/v1/generate-focus-plan
  - Accepts user health context (same payload the app sends to Claude today)
  - Generates a focus plan using the curated habit catalogue
  - Returns the same FocusPlanResponse schema the app expects, plus citations
"""

import json
import os
from datetime import datetime

import anthropic
from fastapi import APIRouter, HTTPException, Request

from engine.coaching.habit_catalogue import HABITS

router = APIRouter(prefix="/api/v1", tags=["focus-plan"])

# Model for focus plan generation (Sonnet, not Opus, per cost rules)
FOCUS_PLAN_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a health coach generating a personalized focus plan. You have access
to a curated habit catalogue with verified medical citations. Your job is to
select the best habit for this person from the catalogue, personalize the
anchor and reasoning to their context, and return the result.

## Habit Catalogue

{catalogue_json}

## Rules

1. Select ONE primary recommendation from the catalogue that best matches
   the person's outcomes, obstacles, and health context.
2. Select 1-2 alternatives from different categories.
3. Personalize the action, anchor, and reasoning to their specific situation.
   The catalogue gives you the template. Make it feel like it was written for them.
4. Include the citation(s) from the catalogue habit in your response.
5. The response MUST include an "evidence" array for the primary recommendation
   and each alternative, copied from the catalogue.
6. Write a brief health snapshot, reflection, insight, and encouragement.
7. If the person has wearable data, reference specific numbers.
8. Do an internal risk assessment (top 5 risks) based on their profile.
9. Suggest one care team recommendation if appropriate.

## Tone
- Direct. No throat-clearing.
- "What stands out..." not "I see..."
- Inline markdown only (bold, italic). No headers or bullets in the prose sections.

## Response Format (strict JSON)

{{
  "healthSnapshot": "2-3 sentences about their current state",
  "reflection": "2 sentences max, no first-person",
  "insight": "2 sentences, direct",
  "primaryRecommendation": {{
    "action": "under 8 words, lowercase",
    "anchor": "when/where to do it",
    "reasoning": "1 sentence connecting to their outcomes",
    "connection": "links habit to their stated outcome",
    "category": "nutrition|movement|sleep|stress|social|mental|medical|other",
    "purpose": "max 6 words, completes 'to...'",
    "catalogueId": "the habit ID from the catalogue",
    "evidence": [
      {{
        "title": "paper title",
        "authors": "author list",
        "journal": "journal name",
        "year": 2024,
        "pmid": "12345678",
        "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/"
      }}
    ]
  }},
  "alternatives": [
    {{
      "action": "under 8 words",
      "anchor": "when/where",
      "reasoning": "1 sentence",
      "category": "category",
      "purpose": "max 6 words",
      "catalogueId": "habit ID",
      "evidence": [...]
    }}
  ],
  "encouragement": "1 sentence, under 20 words",
  "methodology": "2-3 sentences explaining your reasoning",
  "riskAssessment": "top 5 risks, numbered",
  "careTeamRecommendation": {{
    "topic": "what to discuss",
    "specialist": "type of specialist",
    "reasoning": "why"
  }}
}}
"""


def _build_catalogue_json() -> str:
    """Build a compact JSON representation of the habit catalogue for the prompt."""
    compact = []
    for h in HABITS:
        compact.append({
            "id": h["id"],
            "action": h["action"],
            "category": h["category"],
            "purpose": h["purpose"],
            "citations": h["citations"],
        })
    return json.dumps(compact, indent=2)


@router.post("/generate-focus-plan")
async def generate_focus_plan(request: Request):
    """Generate a focus plan with cited habits from the curated catalogue.

    Expects JSON body with:
    - context: str (the health context payload, same as what the app sends to Claude)
    - token: str (API token for auth)

    Optional:
    - outcomes: list[str] (selected health outcomes)
    - obstacles: list[str] (what hasn't worked)
    - forming_habits: list[str] (current forming habits)
    - practicing_habits: list[str] (current practicing habits)
    - wearable_summary: str (Apple Health data summary)
    """
    body = await request.json()

    # Auth — config is injected by create_app() via app.state
    config = request.app.state.config
    token = body.get("token") or request.headers.get("authorization", "").replace("Bearer ", "")
    if not config.api_token:
        raise HTTPException(500, "API token not configured")
    if not token or token != config.api_token:
        # Also check token_persons for per-user tokens
        if token not in (config.token_persons or {}):
            raise HTTPException(403, "Invalid token")

    context = body.get("context", "")
    outcomes = body.get("outcomes", [])
    obstacles = body.get("obstacles", [])
    forming = body.get("forming_habits", [])
    practicing = body.get("practicing_habits", [])
    wearable = body.get("wearable_summary", "")

    # Build the user message
    user_parts = []
    if context:
        user_parts.append(f"Health Context:\n{context}")
    if outcomes:
        user_parts.append(f"Selected Outcomes: {', '.join(outcomes)}")
    if obstacles:
        user_parts.append(f"Obstacles: {', '.join(obstacles)}")
    if forming:
        user_parts.append(f"Currently Forming: {', '.join(forming)}")
    if practicing:
        user_parts.append(f"Currently Practicing: {', '.join(practicing)}")
    if wearable:
        user_parts.append(f"Wearable Data:\n{wearable}")

    user_message = "\n\n".join(user_parts) if user_parts else "Generate a focus plan for a new user with no context yet."

    # Build prompt with catalogue
    catalogue_json = _build_catalogue_json()
    system = SYSTEM_PROMPT.format(catalogue_json=catalogue_json)

    # Call Claude
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=FOCUS_PLAN_MODEL,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        raise HTTPException(502, f"LLM call failed: {e}")

    text = response.content[0].text.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "LLM returned invalid JSON")

    # Validate citations exist in catalogue
    _validate_citations(result)

    # Add metadata
    result["generated_at"] = datetime.utcnow().isoformat()
    result["model"] = FOCUS_PLAN_MODEL
    result["catalogue_version"] = len(HABITS)

    return result


def _validate_citations(result: dict):
    """Ensure all citations in the response match the catalogue.

    If the LLM hallucinated a citation, replace it with the catalogue version.
    If the catalogueId doesn't exist, drop the evidence array.
    """
    catalogue_by_id = {h["id"]: h for h in HABITS}

    def _fix_rec(rec: dict):
        cid = rec.get("catalogueId")
        if cid and cid in catalogue_by_id:
            # Replace evidence with verified catalogue citations
            rec["evidence"] = catalogue_by_id[cid]["citations"]
        elif cid:
            # Unknown catalogue ID, drop evidence to be safe
            rec["evidence"] = []

    primary = result.get("primaryRecommendation")
    if primary:
        _fix_rec(primary)

    for alt in result.get("alternatives", []):
        _fix_rec(alt)


@router.get("/habit-catalogue")
async def get_catalogue():
    """Return the full habit catalogue with citations. For debugging/review."""
    return {
        "count": len(HABITS),
        "habits": HABITS,
    }
