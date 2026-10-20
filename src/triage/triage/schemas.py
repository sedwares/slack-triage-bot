"""Classification taxonomy and the structured-output contract.

Structured JSON is obtained via **forced tool use**, not by asking the model to
emit JSON in prose. With ``tool_choice={"type": "tool", "name": ...}`` the API
constrains the response to the tool's input schema, so you get a validated
object rather than something that needs a regex to strip markdown fences off.

The taxonomy is defined once here and rendered into the prompt from this single
definition. Duplicating the category list in a hand-written prompt string is how
a category ends up in the prompt but not in the CHECK constraint, and then a
perfectly good classification fails on insert at 2am.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Categories and the guidance the model sees. Keys MUST match the CHECK
# constraint on triage.classifications.message_type in migration 0001.
MESSAGE_TYPES: dict[str, str] = {
    "incident": "Something is broken or degraded right now and users are affected.",
    "bug_report": "A defect is described, but it is not an active outage.",
    "feature_request": "A request for new capability or an enhancement.",
    "question": "Someone is asking for information or help understanding something.",
    "access_request": "A request for permissions, credentials, or an account.",
    "status_update": "An informational update on work in progress. No action needed.",
    "discussion": "Open-ended conversation, planning, or decision-making.",
    "chatter": "Social or administrative talk with no work content.",
    "other": "Genuinely does not fit any category above.",
}

# Urgency guidance is spelled out because "urgency 1-5" without anchors produces
# a model that answers 3 to everything.
URGENCY_SCALE: dict[int, str] = {
    1: "No time pressure. Informational, or purely social.",
    2: "Low. Should be handled eventually; no one is blocked.",
    3: "Normal. Should be handled in the next day or two. Someone is mildly blocked.",
    4: "High. Someone is blocked now, or a customer is affected. Same-day response.",
    5: "Critical. Active outage, data loss, security issue, or revenue impact.",
}

TAXONOMY_VERSION = "v1"


class ClassificationResult(BaseModel):
    """Validated classifier output.

    Validation is deliberately strict. The model is constrained by the tool
    schema, but "constrained" is not "guaranteed", and a bad value here would
    hit a CHECK constraint on insert and cost you a confusing traceback instead
    of a clean, logged fallback.
    """

    message_type: str = Field(description="One of the taxonomy keys.")
    urgency: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_owner: str = Field(
        description="Role or team best suited to handle this, e.g. 'on-call engineer'."
    )
    rationale: str = Field(default="", max_length=500)

    @field_validator("message_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in MESSAGE_TYPES:
            # Degrade to 'other' rather than raising. A slightly wrong category
            # is recoverable; a dropped classification is a hole in the dataset.
            return "other"
        return normalized


@dataclass(frozen=True, slots=True)
class TieredDecision:
    """A classification plus the routing metadata that produced it.

    ``invocations`` carries every API call made, including failures and the
    superseded cheap-tier call when an escalation happened. The pipeline writes
    all of them so cost reporting reflects what was actually spent.
    """

    result: ClassificationResult
    tier: str  # 'heuristic' | 'cheap' | 'deep'
    invocations: list[dict[str, Any]]
    escalated: bool = False
    prefilter_reason: str | None = None


def build_classification_tool() -> dict[str, Any]:
    """Return the Anthropic tool definition used to force structured output.

    Generated from ``MESSAGE_TYPES`` so the enum in the schema and the CHECK
    constraint in Postgres cannot drift apart.
    """
    return {
        "name": "record_triage",
        "description": (
            "Record the triage classification for a Slack message. "
            "You must call this tool exactly once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_type": {
                    "type": "string",
                    "enum": list(MESSAGE_TYPES.keys()),
                    "description": "The category that best fits the message.",
                },
                "urgency": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "How quickly this needs a response (1-5).",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": (
                        "Your confidence in this classification. Be honest: low "
                        "confidence routes the message to a stronger model, which "
                        "is the correct outcome for a genuinely ambiguous message."
                    ),
                },
                "suggested_owner": {
                    "type": "string",
                    "description": (
                        "The role or team that should handle this, such as "
                        "'on-call engineer', 'support', or 'product manager'. "
                        "Use a role, never a specific person's name."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": "One sentence explaining the classification.",
                },
            },
            "required": [
                "message_type",
                "urgency",
                "confidence",
                "suggested_owner",
            ],
        },
    }
