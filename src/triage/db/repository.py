"""Data access. All SQL lives here; no SQL anywhere else in the codebase.

Two patterns from ETL work do the heavy lifting:

**Idempotent upserts.** Slack redelivers any event it did not get a 200 for
within three seconds, including events your own slow handler caused. Every
ingest path is ``ON CONFLICT`` guarded, and
:meth:`MessageRepository.record_event` reports whether the row was new so the
caller can skip re-spending tokens on a duplicate.

**Write the whole unit of work in one transaction.** A classification and its
invocations commit together or not at all. Otherwise a crash between the two
leaves you with cost rows attributed to nothing, and your spend reporting
quietly drifts from reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

import asyncpg

from triage.db.pool import admin_transaction, tenant_transaction
from triage.logging_setup import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Row containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Workspace:
    """A tenant, with its privacy posture and routing configuration."""

    workspace_id: int
    slack_team_id: str
    name: str
    store_message_bodies: bool
    store_raw_events: bool
    body_retention_days: int
    content_hmac_key_id: str
    urgency_trigger_threshold: int
    deep_tier_enabled: bool
    monthly_budget_usd: Decimal | None
    status: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of recording a Slack message event.

    Attributes:
        message_event_id: Internal id of the (new or pre-existing) event row.
        occurred_at: Event time, needed for the composite FKs on child tables.
        is_new: ``False`` when this delivery was a duplicate. The pipeline stops
            here when ``False``, which is what keeps Slack's retry behaviour from
            multiplying your Anthropic bill.
    """

    message_event_id: int
    occurred_at: datetime
    is_new: bool


@dataclass(frozen=True, slots=True)
class PriceRow:
    """One effective-dated pricing window, per million tokens."""

    model: str
    valid_from: date
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal
    cache_write_5m_usd_per_mtok: Decimal
    cache_write_1h_usd_per_mtok: Decimal


# ---------------------------------------------------------------------------
# Workspace / channel / actor resolution
# ---------------------------------------------------------------------------


class WorkspaceRepository:
    """Resolves Slack identifiers to internal tenant rows.

    Runs in an admin transaction because resolving a team ID is inherently
    pre-tenant: you cannot set the tenant context until you know the tenant.
    """

    @staticmethod
    async def get_by_slack_team_id(slack_team_id: str) -> Workspace | None:
        async with admin_transaction() as conn:
            row = await conn.fetchrow(
                """
                SELECT workspace_id, slack_team_id, name,
                       store_message_bodies, store_raw_events,
                       body_retention_days, content_hmac_key_id,
                       urgency_trigger_threshold, deep_tier_enabled,
                       monthly_budget_usd, status::text AS status
                FROM triage.workspaces
                WHERE slack_team_id = $1
                """,
                slack_team_id,
            )
        return Workspace(**dict(row)) if row else None

    @staticmethod
    async def upsert(
        *,
        slack_team_id: str,
        name: str,
        content_hmac_key_id: str,
        slack_enterprise_id: str | None = None,
    ) -> Workspace:
        """Create or refresh a workspace. Called on install and at dev bootstrap.

        Privacy defaults are set by the schema, not here: a new workspace starts
        with body storage OFF. Turning it on must be a deliberate act.
        """
        async with admin_transaction() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO triage.workspaces
                    (slack_team_id, slack_enterprise_id, name, content_hmac_key_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (slack_team_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        content_hmac_key_id = EXCLUDED.content_hmac_key_id,
                        status = 'active',
                        updated_at = now()
                RETURNING workspace_id, slack_team_id, name,
                          store_message_bodies, store_raw_events,
                          body_retention_days, content_hmac_key_id,
                          urgency_trigger_threshold, deep_tier_enabled,
                          monthly_budget_usd, status::text AS status
                """,
                slack_team_id,
                slack_enterprise_id,
                name,
                content_hmac_key_id,
            )
        return Workspace(**dict(row))

    @staticmethod
    async def month_to_date_spend(workspace_id: int) -> Decimal:
        """Total spend since the start of the current UTC month.

        Read before each classification to enforce ``monthly_budget_usd``.
        Cheap today; swap the view for a materialized rollup when it is not.
        """
        async with tenant_transaction(workspace_id) as conn:
            value = await conn.fetchval(
                """
                SELECT coalesce(sum(cost_usd), 0)
                FROM triage.llm_invocations
                WHERE workspace_id = $1
                  AND created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
                """,
                workspace_id,
            )
        return Decimal(value or 0)


class ChannelRepository:
    @staticmethod
    async def upsert(
        conn: asyncpg.Connection,
        *,
        workspace_id: int,
        slack_channel_id: str,
        name: str | None = None,
        is_watched: bool = False,
    ) -> tuple[int, bool]:
        """Return ``(channel_id, is_watched)``, creating the row if needed.

        ``is_watched`` is never overwritten by this path — a channel's watched
        status is an admin decision, and an incoming message should not silently
        flip it. The COALESCE on name lets a later event fill in a name that was
        unknown at first sight without clobbering a known one.
        """
        row = await conn.fetchrow(
            """
            INSERT INTO triage.channels
                (workspace_id, slack_channel_id, name, is_watched)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (workspace_id, slack_channel_id) DO UPDATE
                SET name = COALESCE(EXCLUDED.name, triage.channels.name),
                    updated_at = now()
            RETURNING channel_id, is_watched
            """,
            workspace_id,
            slack_channel_id,
            name,
            is_watched,
        )
        return row["channel_id"], row["is_watched"]


class ActorRepository:
    @staticmethod
    async def upsert(
        conn: asyncpg.Connection,
        *,
        workspace_id: int,
        slack_user_id: str,
        is_bot: bool = False,
        display_name: str | None = None,
    ) -> int:
        """Return the internal ``actor_id``.

        ``display_name`` is only ever passed by callers when the workspace has
        opted into body storage; the default path stores the pseudonymous Slack
        ID alone.
        """
        return await conn.fetchval(
            """
            INSERT INTO triage.actors
                (workspace_id, slack_user_id, is_bot, display_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (workspace_id, slack_user_id) DO UPDATE
                SET display_name = COALESCE(EXCLUDED.display_name,
                                            triage.actors.display_name),
                    updated_at = now()
            RETURNING actor_id
            """,
            workspace_id,
            slack_user_id,
            is_bot,
            display_name,
        )

    @staticmethod
    async def find_owner_for_type(
        conn: asyncpg.Connection, *, workspace_id: int, message_type: str
    ) -> int | None:
        """Map a message type to its default owner, if one is configured.

        Deterministic ownership beats asking the model to guess a username: the
        model does not know your on-call rota, and a hallucinated @mention is
        worse than no suggestion.
        """
        return await conn.fetchval(
            """
            SELECT actor_id
            FROM triage.actors
            WHERE workspace_id = $1
              AND $2 = ANY(owns_message_types)
            ORDER BY actor_id
            LIMIT 1
            """,
            workspace_id,
            message_type,
        )


# ---------------------------------------------------------------------------
# Message ingest
# ---------------------------------------------------------------------------


class MessageRepository:
    @staticmethod
    async def record_event(
        conn: asyncpg.Connection,
        *,
        workspace_id: int,
        channel_id: int,
        actor_id: int | None,
        slack_event_id: str,
        slack_ts: str,
        slack_thread_ts: str | None,
        subtype: str | None,
        occurred_at: datetime,
        features: dict[str, Any],
        content_fingerprint: bytes,
    ) -> IngestResult:
        """Idempotently record one message event.

        The ``ON CONFLICT DO NOTHING ... RETURNING`` shape is the whole trick: on
        a duplicate delivery, RETURNING yields no row, so we know without a
        second round trip that this event was already handled and can stop
        before spending anything.
        """
        row = await conn.fetchrow(
            """
            INSERT INTO triage.message_events (
                workspace_id, channel_id, actor_id,
                slack_event_id, slack_ts, slack_thread_ts, subtype,
                occurred_at,
                char_len, word_count, mention_count, link_count,
                has_code_block, has_question_mark, is_broadcast, is_thread_reply,
                content_fingerprint
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14, $15, $16, $17)
            ON CONFLICT (workspace_id, slack_event_id) DO NOTHING
            RETURNING message_event_id, occurred_at
            """,
            workspace_id,
            channel_id,
            actor_id,
            slack_event_id,
            slack_ts,
            slack_thread_ts,
            subtype,
            occurred_at,
            features["char_len"],
            features["word_count"],
            features["mention_count"],
            features["link_count"],
            features["has_code_block"],
            features["has_question_mark"],
            features["is_broadcast"],
            features["is_thread_reply"],
            content_fingerprint,
        )

        if row is not None:
            return IngestResult(row["message_event_id"], row["occurred_at"], True)

        # Duplicate delivery: fetch the existing row so callers still have ids.
        existing = await conn.fetchrow(
            """
            SELECT message_event_id, occurred_at
            FROM triage.message_events
            WHERE workspace_id = $1 AND slack_event_id = $2
            """,
            workspace_id,
            slack_event_id,
        )
        log.info(
            "duplicate_event_skipped",
            workspace_id=workspace_id,
            slack_event_id=slack_event_id,
        )
        return IngestResult(existing["message_event_id"], existing["occurred_at"], False)

    @staticmethod
    async def store_body(
        conn: asyncpg.Connection,
        *,
        workspace_id: int,
        message_event_id: int,
        occurred_at: datetime,
        body: str,
        retention_days: int,
    ) -> None:
        """Persist message text. Callers MUST check ``store_message_bodies`` first.

        ``purge_after`` is computed at write time so the retention job is a
        single unconditional DELETE with no per-workspace logic.
        """
        await conn.execute(
            """
            INSERT INTO triage.message_bodies
                (message_event_id, occurred_at, workspace_id, body, purge_after)
            VALUES ($1, $2, $3, $4, now() + make_interval(days => $5))
            ON CONFLICT (message_event_id, occurred_at) DO NOTHING
            """,
            message_event_id,
            occurred_at,
            workspace_id,
            body,
            retention_days,
        )


# ---------------------------------------------------------------------------
# Classifications and cost
# ---------------------------------------------------------------------------


class ClassificationRepository:
    @staticmethod
    async def insert(
        conn: asyncpg.Connection,
        *,
        workspace_id: int,
        message_event_id: int,
        occurred_at: datetime,
        tier: str,
        message_type: str,
        urgency: int,
        confidence: float | None,
        suggested_owner_actor_id: int | None,
        suggested_owner_label: str,
        rationale: str | None,
        escalated_from_id: int | None,
        prompt_version: str,
        taxonomy_version: str,
        pipeline_version: str,
        e2e_latency_ms: int | None,
    ) -> int:
        """Append a classification and make it the current one.

        Two statements, one transaction. The demotion runs first because the
        partial unique index permits only one ``is_current`` row per message —
        inserting before demoting would violate it.
        """
        await conn.execute(
            """
            UPDATE triage.classifications
            SET is_current = FALSE
            WHERE workspace_id = $1
              AND message_event_id = $2
              AND is_current
            """,
            workspace_id,
            message_event_id,
        )

        return await conn.fetchval(
            """
            INSERT INTO triage.classifications (
                workspace_id, message_event_id, occurred_at,
                tier, message_type, urgency, confidence,
                suggested_owner_actor_id, suggested_owner_label, rationale,
                escalated_from_id,
                prompt_version, taxonomy_version, pipeline_version,
                e2e_latency_ms, is_current
            )
            VALUES ($1, $2, $3, $4::triage.routing_tier, $5, $6, $7,
                    $8, $9, $10, $11, $12, $13, $14, $15, TRUE)
            RETURNING classification_id
            """,
            workspace_id,
            message_event_id,
            occurred_at,
            tier,
            message_type,
            urgency,
            confidence,
            suggested_owner_actor_id,
            suggested_owner_label,
            rationale,
            escalated_from_id,
            prompt_version,
            taxonomy_version,
            pipeline_version,
            e2e_latency_ms,
        )


class InvocationRepository:
    @staticmethod
    async def insert_many(
        conn: asyncpg.Connection,
        *,
        workspace_id: int,
        message_event_id: int,
        classification_id: int | None,
        invocations: Sequence[dict[str, Any]],
    ) -> None:
        """Record every API call made while handling one message.

        Includes failures and retries. A 429 that was retried still consumed
        budget and still tells you something about your latency profile; writing
        only successes makes your cost reporting optimistic and your error rate
        invisible.
        """
        if not invocations:
            return

        await conn.executemany(
            """
            INSERT INTO triage.llm_invocations (
                workspace_id, message_event_id, classification_id,
                purpose, model, tier, provider_request_id,
                input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens,
                cost_usd, pricing_valid_from,
                latency_ms, attempt,
                status, stop_reason, error_class
            )
            VALUES ($1, $2, $3, $4, $5, $6::triage.routing_tier, $7,
                    $8, $9, $10, $11, $12, $13, $14, $15,
                    $16::triage.invocation_status, $17, $18)
            """,
            [
                (
                    workspace_id,
                    message_event_id,
                    classification_id,
                    inv["purpose"],
                    inv["model"],
                    inv["tier"],
                    inv.get("provider_request_id"),
                    inv.get("input_tokens", 0),
                    inv.get("output_tokens", 0),
                    inv.get("cache_read_tokens", 0),
                    inv.get("cache_write_tokens", 0),
                    inv.get("cost_usd", Decimal(0)),
                    inv["pricing_valid_from"],
                    inv["latency_ms"],
                    inv.get("attempt", 1),
                    inv["status"],
                    inv.get("stop_reason"),
                    inv.get("error_class"),
                )
                for inv in invocations
            ],
        )


class PricingRepository:
    """Reads the global, effective-dated pricing table.

    Not tenant-scoped: Anthropic charges every workspace the same rate, so this
    is reference data. Loaded once at startup and cached in memory — see
    ``triage.llm.pricing``.
    """

    @staticmethod
    async def load_all() -> list[PriceRow]:
        async with admin_transaction() as conn:
            rows = await conn.fetch(
                """
                SELECT model, valid_from,
                       input_usd_per_mtok, output_usd_per_mtok,
                       cache_read_usd_per_mtok,
                       cache_write_5m_usd_per_mtok, cache_write_1h_usd_per_mtok
                FROM triage.model_pricing
                ORDER BY model, valid_from
                """
            )
        return [PriceRow(**dict(r)) for r in rows]
