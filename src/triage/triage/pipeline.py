"""End-to-end triage pipeline for one Slack message.

Ordering here is the design:

1. Resolve the workspace (pre-tenant, admin transaction).
2. Prefilter — decide before spending anything.
3. Ingest transaction — record the event idempotently. **Stop here on duplicate.**
4. Route through the tiers (LLM calls happen outside any open transaction).
5. Persist transaction — classification and invocations commit atomically.

Step 3 stopping early is the load-bearing part. Slack retries any event it does
not get a 200 for within three seconds, and a slow classification is exactly what
produces that. Without the duplicate check, every slow message would be
classified twice and billed twice.

Steps 4 and 5 are separate transactions on purpose. Holding a Postgres
transaction open across a multi-second network call to Anthropic would pin a
pooled connection for the duration and, under load, exhaust the pool. The cost is
a small window where an event is recorded but unclassified — which is
recoverable (a backfill job finds events with no current classification) in a way
that connection exhaustion is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from triage.config import Settings
from triage.db.pool import tenant_transaction
from triage.db.repository import (
    ActorRepository,
    ChannelRepository,
    ClassificationRepository,
    InvocationRepository,
    MessageRepository,
    Workspace,
    WorkspaceRepository,
)
from triage.logging_setup import get_logger
from triage.slack.privacy import derive_workspace_key, fingerprint
from triage.triage.prefilter import Decision, prefilter
from triage.triage.router import TieredRouter
from triage.triage.schemas import TieredDecision

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    """What the pipeline did, for the caller to act on and for logging."""

    handled: bool
    reason: str
    classification_id: int | None = None
    tier: str | None = None
    message_type: str | None = None
    urgency: int | None = None
    should_trigger: bool = False  # Phase 3 will act on this


class TriagePipeline:
    """Orchestrates ingest, routing, and persistence."""

    def __init__(self, router: TieredRouter, settings: Settings) -> None:
        self._router = router
        self._settings = settings
        # Derived HMAC keys, cached per workspace. PBKDF2 is intentionally not
        # free; recomputing it on every message would show up in latency.
        self._key_cache: dict[str, bytes] = {}

    def _workspace_key(self, workspace: Workspace) -> bytes:
        cached = self._key_cache.get(workspace.slack_team_id)
        if cached is None:
            cached = derive_workspace_key(
                self._settings.content_hmac_master_key.get_secret_value(),
                workspace.slack_team_id,
                self._settings.content_hmac_key_generation,
            )
            self._key_cache[workspace.slack_team_id] = cached
        return cached

    async def process(
        self,
        *,
        event: dict[str, Any],
        slack_team_id: str,
        slack_event_id: str,
        bot_user_id: str | None,
        channel_name: str | None = None,
    ) -> PipelineOutcome:
        """Process one Slack message event.

        Args:
            event: The Slack ``message`` event payload.
            slack_team_id: Team ID from the envelope.
            slack_event_id: Slack's event ID (``Ev...``), the idempotency key.
            bot_user_id: Our bot's user ID, so we never classify our own posts.
            channel_name: Channel name when known.

        Returns:
            A :class:`PipelineOutcome`.
        """
        started = time.perf_counter()
        text: str = event.get("text", "") or ""

        # --- 1. Resolve tenant ------------------------------------------------
        workspace = await WorkspaceRepository.get_by_slack_team_id(slack_team_id)
        if workspace is None:
            log.warning("unknown_workspace", slack_team_id=slack_team_id)
            return PipelineOutcome(handled=False, reason="unknown_workspace")

        if workspace.status != "active":
            log.info(
                "workspace_inactive",
                workspace_id=workspace.workspace_id,
                status=workspace.status,
            )
            return PipelineOutcome(handled=False, reason=f"status_{workspace.status}")

        # --- 2. Prefilter -----------------------------------------------------
        pf = prefilter(text, event, bot_user_id=bot_user_id)

        if pf.decision == Decision.SKIP:
            # Not user-authored content. Store nothing: a channel-join row would
            # be noise in every downstream aggregate.
            log.debug("prefilter_skip", reason=pf.reason)
            return PipelineOutcome(handled=False, reason=pf.reason)

        # --- 3. Ingest (idempotent) ------------------------------------------
        occurred_at = datetime.fromtimestamp(
            float(event["ts"]), tz=timezone.utc
        )

        async with tenant_transaction(workspace.workspace_id) as conn:
            channel_id, is_watched = await ChannelRepository.upsert(
                conn,
                workspace_id=workspace.workspace_id,
                slack_channel_id=event["channel"],
                name=channel_name,
            )

            if not is_watched:
                # Channel is known but not opted in. Recorded above so an admin
                # can flip the flag later; not processed.
                log.debug("channel_not_watched", slack_channel_id=event["channel"])
                return PipelineOutcome(handled=False, reason="channel_not_watched")

            actor_id: int | None = None
            if event.get("user"):
                actor_id = await ActorRepository.upsert(
                    conn,
                    workspace_id=workspace.workspace_id,
                    slack_user_id=event["user"],
                    is_bot=bool(event.get("bot_id")),
                )

            ingest = await MessageRepository.record_event(
                conn,
                workspace_id=workspace.workspace_id,
                channel_id=channel_id,
                actor_id=actor_id,
                slack_event_id=slack_event_id,
                slack_ts=event["ts"],
                slack_thread_ts=event.get("thread_ts"),
                subtype=event.get("subtype"),
                occurred_at=occurred_at,
                features=pf.features,
                content_fingerprint=fingerprint(text, self._workspace_key(workspace)),
            )

            # THE guard against paying twice for Slack's retries.
            if not ingest.is_new:
                return PipelineOutcome(handled=False, reason="duplicate_event")

            # Bodies only when the workspace has explicitly opted in.
            if workspace.store_message_bodies:
                await MessageRepository.store_body(
                    conn,
                    workspace_id=workspace.workspace_id,
                    message_event_id=ingest.message_event_id,
                    occurred_at=ingest.occurred_at,
                    body=text,
                    retention_days=workspace.body_retention_days,
                )

        # --- 3b. Budget guardrail --------------------------------------------
        # Checked after ingest so the event is recorded regardless, and before
        # routing so an over-budget workspace spends nothing.
        if workspace.monthly_budget_usd is not None:
            spend = await WorkspaceRepository.month_to_date_spend(
                workspace.workspace_id
            )
            if spend >= workspace.monthly_budget_usd:
                log.warning(
                    "budget_exceeded",
                    workspace_id=workspace.workspace_id,
                    spend_usd=str(spend),
                    budget_usd=str(workspace.monthly_budget_usd),
                )
                return PipelineOutcome(handled=False, reason="budget_exceeded")

        # --- 4. Route (no transaction held across network I/O) ---------------
        decision: TieredDecision = await self._router.classify(
            text=text,
            prefilter_result=pf,
            channel_name=channel_name,
            deep_tier_enabled=workspace.deep_tier_enabled,
        )

        e2e_latency_ms = int((time.perf_counter() - started) * 1000)

        # --- 5. Persist atomically -------------------------------------------
        async with tenant_transaction(workspace.workspace_id) as conn:
            # Resolve the model's role suggestion to a real actor where the
            # workspace has configured ownership. Deterministic mapping beats
            # asking the model to guess a username it cannot know.
            owner_actor_id = await ActorRepository.find_owner_for_type(
                conn,
                workspace_id=workspace.workspace_id,
                message_type=decision.result.message_type,
            )

            classification_id = await ClassificationRepository.insert(
                conn,
                workspace_id=workspace.workspace_id,
                message_event_id=ingest.message_event_id,
                occurred_at=ingest.occurred_at,
                tier=decision.tier,
                message_type=decision.result.message_type,
                urgency=decision.result.urgency,
                confidence=decision.result.confidence,
                suggested_owner_actor_id=owner_actor_id,
                suggested_owner_label=decision.result.suggested_owner,
                rationale=decision.result.rationale or None,
                escalated_from_id=None,
                prompt_version=self._settings.prompt_version,
                taxonomy_version=self._settings.taxonomy_version,
                pipeline_version=self._settings.pipeline_version,
                e2e_latency_ms=e2e_latency_ms,
            )

            # Same transaction as the classification: cost rows and the result
            # they belong to must never disagree about whether they exist.
            await InvocationRepository.insert_many(
                conn,
                workspace_id=workspace.workspace_id,
                message_event_id=ingest.message_event_id,
                classification_id=classification_id,
                invocations=decision.invocations,
            )

        total_cost = sum(
            (inv.get("cost_usd", 0) for inv in decision.invocations), start=0
        )

        log.info(
            "message_classified",
            workspace_id=workspace.workspace_id,
            classification_id=classification_id,
            tier=decision.tier,
            escalated=decision.escalated,
            prefilter_reason=decision.prefilter_reason,
            message_type=decision.result.message_type,
            urgency=decision.result.urgency,
            confidence=decision.result.confidence,
            llm_calls=len(decision.invocations),
            cost_usd=str(total_cost),
            e2e_latency_ms=e2e_latency_ms,
        )

        return PipelineOutcome(
            handled=True,
            reason="classified",
            classification_id=classification_id,
            tier=decision.tier,
            message_type=decision.result.message_type,
            urgency=decision.result.urgency,
            # Computed but not acted on — Phase 3 wires up the dispatch.
            should_trigger=(
                decision.result.urgency >= workspace.urgency_trigger_threshold
            ),
        )
