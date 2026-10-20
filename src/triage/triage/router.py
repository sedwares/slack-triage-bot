"""Tiered routing: heuristic -> Haiku -> Sonnet.

The whole cost argument for this project lives in this file.

    tier 0  heuristic   $0.00        bots, acks, emoji, obvious incidents
    tier 1  cheap       ~$0.0002     ordinary messages (Haiku)
    tier 2  deep        ~$0.0008     ambiguous or high-stakes (Sonnet)

Two ways to reach the deep tier:

**Direct** — the prefilter saw a broadcast, a critical term, or a long message.
No cheap call is made, because it would have been escalated anyway.

**Escalation** — Haiku answered but reported low confidence, or assigned high
urgency. Both calls are recorded against the same message, so the true cost of
the escalation path is measurable rather than assumed.

That second condition is the non-obvious one. A cheap model calling something a
P1 is *precisely* when you want a second opinion: the downside of a false
positive (a spurious page) and a false negative (a missed outage) are both high,
and $0.0008 is a rounding error against either.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from triage.config import Settings
from triage.llm.client import AnthropicClient
from triage.llm.prompts import CLASSIFIER_SYSTEM_PROMPT, build_user_content
from triage.logging_setup import get_logger
from triage.triage.prefilter import Decision, PrefilterResult
from triage.triage.schemas import (
    ClassificationResult,
    TieredDecision,
    build_classification_tool,
)

log = get_logger(__name__)

_TOOL = build_classification_tool()

# Result recorded when the prefilter resolves a message without a model.
# Confidence 1.0 is honest here: these are deterministic rule matches, not
# probabilistic judgements.
_HEURISTIC_CHATTER = ClassificationResult(
    message_type="chatter",
    urgency=1,
    confidence=1.0,
    suggested_owner="none",
    rationale="Resolved by heuristic prefilter; no model call required.",
)

# Used when every model call fails. Urgency 3 rather than 1 is deliberate: an
# unclassified message should land in the middle of the queue, not be silently
# buried. Confidence 0.0 marks it as unreliable for downstream evals.
_FALLBACK = ClassificationResult(
    message_type="other",
    urgency=3,
    confidence=0.0,
    suggested_owner="triage queue",
    rationale="Classification unavailable; needs manual review.",
)


class TieredRouter:
    """Routes a message through the cheapest tier that can handle it."""

    def __init__(self, client: AnthropicClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def classify(
        self,
        *,
        text: str,
        prefilter_result: PrefilterResult,
        channel_name: str | None = None,
        deep_tier_enabled: bool = True,
    ) -> TieredDecision:
        """Classify a message, choosing and escalating tiers as needed.

        Args:
            text: Raw message text.
            prefilter_result: Output of :func:`triage.triage.prefilter.prefilter`.
            channel_name: Channel name, passed to the model as context.
            deep_tier_enabled: Per-workspace switch. When off, the router stays
                on the cheap tier even where it would normally escalate — a
                budget lever, and the reason ``tier`` is stored per row rather
                than inferred from the model name.

        Returns:
            A :class:`TieredDecision` carrying the result, the tier that
            produced it, and every invocation made along the way.
        """
        # --- Tier 0 ---------------------------------------------------------
        if prefilter_result.decision in (Decision.AUTO_CHATTER, Decision.SKIP):
            log.info(
                "routed_heuristic",
                reason=prefilter_result.reason,
                char_len=prefilter_result.features["char_len"],
            )
            return TieredDecision(
                result=_HEURISTIC_CHATTER,
                tier="heuristic",
                invocations=[],
                prefilter_reason=prefilter_result.reason,
            )

        user_content = build_user_content(
            text=text,
            channel_name=channel_name,
            is_thread_reply=prefilter_result.features["is_thread_reply"],
        )

        # --- Direct to deep tier -------------------------------------------
        if prefilter_result.decision == Decision.ROUTE_DEEP and deep_tier_enabled:
            log.info("routed_deep_direct", reason=prefilter_result.reason)
            outcome = await self._client.call_with_tool(
                model=self._settings.triage_deep_model,
                tier="deep",
                purpose="classify",
                system=CLASSIFIER_SYSTEM_PROMPT,
                user_content=user_content,
                tool=_TOOL,
            )
            result = self._parse(outcome.tool_input)
            return TieredDecision(
                result=result or _FALLBACK,
                tier="deep",
                invocations=outcome.invocations,
                prefilter_reason=prefilter_result.reason,
            )

        # --- Tier 1: cheap ---------------------------------------------------
        cheap_outcome = await self._client.call_with_tool(
            model=self._settings.triage_cheap_model,
            tier="cheap",
            purpose="classify",
            system=CLASSIFIER_SYSTEM_PROMPT,
            user_content=user_content,
            tool=_TOOL,
        )
        cheap_result = self._parse(cheap_outcome.tool_input)
        invocations = list(cheap_outcome.invocations)

        # Cheap tier failed outright. Escalate rather than give up: a working
        # answer from the expensive model beats no answer from the cheap one.
        if cheap_result is None:
            if not deep_tier_enabled:
                log.warning("cheap_failed_deep_disabled")
                return TieredDecision(
                    result=_FALLBACK,
                    tier="cheap",
                    invocations=invocations,
                    prefilter_reason=prefilter_result.reason,
                )
            log.warning("cheap_failed_escalating")
            return await self._escalate(
                user_content=user_content,
                invocations=invocations,
                prefilter_reason=prefilter_result.reason,
                fallback=_FALLBACK,
            )

        # --- Escalation decision ---------------------------------------------
        should_escalate, why = self._should_escalate(cheap_result)

        if should_escalate and deep_tier_enabled:
            log.info(
                "escalating_to_deep",
                reason=why,
                cheap_confidence=cheap_result.confidence,
                cheap_urgency=cheap_result.urgency,
                cheap_type=cheap_result.message_type,
            )
            return await self._escalate(
                user_content=user_content,
                invocations=invocations,
                prefilter_reason=prefilter_result.reason,
                fallback=cheap_result,  # keep the cheap answer if deep fails
            )

        log.info(
            "routed_cheap",
            message_type=cheap_result.message_type,
            urgency=cheap_result.urgency,
            confidence=cheap_result.confidence,
        )
        return TieredDecision(
            result=cheap_result,
            tier="cheap",
            invocations=invocations,
            prefilter_reason=prefilter_result.reason,
        )

    def _should_escalate(self, result: ClassificationResult) -> tuple[bool, str]:
        """Decide whether a cheap-tier result warrants a second opinion."""
        if result.confidence < self._settings.triage_escalate_below_confidence:
            return True, "low_confidence"

        # A cheap model claiming high urgency gets verified. False positives
        # here erode trust in the alerting; false negatives miss real incidents.
        # Either way the deep call is cheaper than the mistake.
        if result.urgency >= self._settings.triage_escalate_at_urgency:
            return True, "high_urgency"

        return False, ""

    async def _escalate(
        self,
        *,
        user_content: str,
        invocations: list[dict[str, Any]],
        prefilter_reason: str | None,
        fallback: ClassificationResult,
    ) -> TieredDecision:
        """Run the deep tier and merge its invocation records with the cheap ones.

        ``purpose='escalate'`` rather than ``'classify'`` distinguishes these
        calls in the cost table, so "what fraction of spend is escalation?" is a
        one-line query.
        """
        outcome = await self._client.call_with_tool(
            model=self._settings.triage_deep_model,
            tier="deep",
            purpose="escalate",
            system=CLASSIFIER_SYSTEM_PROMPT,
            user_content=user_content,
            tool=_TOOL,
        )
        invocations.extend(outcome.invocations)
        deep_result = self._parse(outcome.tool_input)

        if deep_result is None:
            # Deep tier failed too. Keep the cheap answer — a low-confidence
            # classification is strictly more useful than none.
            log.warning("escalation_failed_using_fallback")
            return TieredDecision(
                result=fallback,
                tier="cheap",
                invocations=invocations,
                escalated=True,
                prefilter_reason=prefilter_reason,
            )

        return TieredDecision(
            result=deep_result,
            tier="deep",
            invocations=invocations,
            escalated=True,
            prefilter_reason=prefilter_reason,
        )

    @staticmethod
    def _parse(tool_input: dict[str, Any] | None) -> ClassificationResult | None:
        """Validate raw tool arguments into a result, or ``None`` on failure."""
        if tool_input is None:
            return None
        try:
            return ClassificationResult.model_validate(tool_input)
        except ValidationError as exc:
            # The tool schema should prevent this. Log loudly if it happens —
            # it means the schema and the validator have diverged.
            log.error("classification_validation_failed", errors=exc.error_count())
            return None
