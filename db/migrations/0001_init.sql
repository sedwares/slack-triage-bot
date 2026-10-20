-- =============================================================================
-- 0001_init.sql — core schema for the Slack AI Triage Bot
--
-- Design notes live in db/README.md. Short version of the load-bearing choices:
--   * Shared-schema multi-tenancy. Every tenant table carries workspace_id.
--     Reference data (model_pricing) is deliberately global — see below.
--   * Surrogate bigint PKs, natural keys enforced as UNIQUE constraints.
--   * PKs on the two growth tables include their time column so that converting
--     to declarative range partitioning later is a mechanical migration.
--   * Message bodies live in a sidecar table so retention purges never touch
--     the hot analytical tables.
--   * Classifications are append-only and versioned; nothing is UPDATEd.
--   * Costs are their own fact table at the grain of one API call.
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS triage;

-- btree_gist lets us mix an equality column (model) with a range column
-- (validity period) inside a single EXCLUDE constraint. Used by model_pricing.
CREATE EXTENSION IF NOT EXISTS btree_gist;

SET LOCAL search_path = triage, public;


-- -----------------------------------------------------------------------------
-- Enumerated types
--
-- Only STABLE vocabularies become native enums. Postgres enums are compact and
-- fast, but ALTER TYPE ... ADD VALUE cannot be used in the same transaction that
-- adds it, and values can never be removed. The message-type taxonomy will churn
-- weekly for the first month, so it stays TEXT + CHECK (see classifications).
-- -----------------------------------------------------------------------------

CREATE TYPE triage.routing_tier AS ENUM (
    'heuristic',  -- resolved by the prefilter, zero LLM calls, zero cost
    'cheap',      -- Haiku
    'deep'        -- Sonnet, either by direct routing or by escalation
);

CREATE TYPE triage.invocation_status AS ENUM (
    'ok',
    'error',      -- non-retryable API error
    'timeout',
    'rate_limited',
    'refusal'     -- model declined / stop_reason indicated a refusal
);

CREATE TYPE triage.workspace_status AS ENUM (
    'active',
    'suspended',   -- over budget or admin-disabled; events acked but not processed
    'uninstalled'
);


-- -----------------------------------------------------------------------------
-- workspaces — the tenant root
--
-- Privacy posture is per-workspace and enforced in the application layer, but it
-- is DECLARED here so that a schema reader can see the data-handling contract
-- without reading Python.
--
-- content_hmac_key_id is an IDENTIFIER pointing into your secret manager. The
-- actual HMAC key is never stored in Postgres. If the database is dumped, the
-- fingerprints in message_events remain opaque.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.workspaces (
    workspace_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Slack's natural keys.
    slack_team_id           TEXT        NOT NULL UNIQUE,
    slack_enterprise_id     TEXT,
    name                    TEXT        NOT NULL,

    -- Privacy: bodies are NOT stored unless a workspace explicitly opts in.
    store_message_bodies    BOOLEAN     NOT NULL DEFAULT FALSE,
    store_raw_events        BOOLEAN     NOT NULL DEFAULT FALSE,
    body_retention_days     INTEGER     NOT NULL DEFAULT 30
                                        CHECK (body_retention_days BETWEEN 1 AND 3650),
    content_hmac_key_id     TEXT        NOT NULL,

    -- Routing configuration.
    urgency_trigger_threshold SMALLINT  NOT NULL DEFAULT 4
                                        CHECK (urgency_trigger_threshold BETWEEN 1 AND 5),
    deep_tier_enabled       BOOLEAN     NOT NULL DEFAULT TRUE,

    -- Cost guardrail. NULL means unlimited. Checked before each LLM call.
    monthly_budget_usd      NUMERIC(10, 2)
                                        CHECK (monthly_budget_usd IS NULL
                                               OR monthly_budget_usd >= 0),

    status                  triage.workspace_status NOT NULL DEFAULT 'active',

    installed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN triage.workspaces.content_hmac_key_id IS
    'Identifier for the per-workspace HMAC key held in the secret manager. '
    'The key material itself is never persisted in Postgres.';


-- -----------------------------------------------------------------------------
-- channels
-- -----------------------------------------------------------------------------

CREATE TABLE triage.channels (
    channel_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    slack_channel_id    TEXT        NOT NULL,
    name                TEXT,
    is_watched          BOOLEAN     NOT NULL DEFAULT FALSE,
    is_private          BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Slack channel IDs are unique within a workspace, not globally.
    UNIQUE (workspace_id, slack_channel_id)
);

-- The hot path asks "is this channel watched?" on every single event.
CREATE INDEX channels_watched_idx
    ON triage.channels (workspace_id, slack_channel_id)
    WHERE is_watched;


-- -----------------------------------------------------------------------------
-- actors — humans and bots
--
-- display_name is nullable and only populated when a workspace opts into body
-- storage. Slack user IDs alone are pseudonymous; names are not.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.actors (
    actor_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    slack_user_id       TEXT        NOT NULL,
    is_bot              BOOLEAN     NOT NULL DEFAULT FALSE,
    display_name        TEXT,

    -- Message types this actor is the default owner for. Drives the
    -- suggested_owner resolution without another LLM call.
    owns_message_types  TEXT[]      NOT NULL DEFAULT '{}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (workspace_id, slack_user_id)
);

CREATE INDEX actors_owns_types_idx
    ON triage.actors USING GIN (owns_message_types);


-- -----------------------------------------------------------------------------
-- message_events — immutable ingest facts
--
-- This table NEVER holds message text. It holds the derived features the
-- prefilter and the analytics need, plus an HMAC fingerprint.
--
-- Two unique constraints, doing different jobs:
--   (workspace_id, slack_event_id)          -> delivery idempotency (Slack retries)
--   (workspace_id, channel_id, slack_ts)    -> message identity (dedup across
--                                              event types referencing one message)
--
-- PK is (message_event_id, occurred_at) so a future conversion to monthly range
-- partitioning does not require changing the primary key.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.message_events (
    message_event_id    BIGINT GENERATED ALWAYS AS IDENTITY,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    channel_id          BIGINT      NOT NULL REFERENCES triage.channels(channel_id),
    actor_id            BIGINT      REFERENCES triage.actors(actor_id),

    -- Slack identifiers, kept as exact strings. slack_ts is a string, not a
    -- number: "1712345678.000200" loses its identity if cast to float.
    slack_event_id      TEXT        NOT NULL,
    slack_ts            TEXT        NOT NULL,
    slack_thread_ts     TEXT,
    subtype             TEXT,

    -- occurred_at is derived from slack_ts; received_at is our clock. The gap
    -- between them is queue lag, which is worth being able to measure.
    occurred_at         TIMESTAMPTZ NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Derived features. Computed once at ingest so the prefilter never needs
    -- the body, and so analytics work even for opted-out workspaces.
    char_len            INTEGER     NOT NULL CHECK (char_len >= 0),
    word_count          INTEGER     NOT NULL CHECK (word_count >= 0),
    mention_count       SMALLINT    NOT NULL DEFAULT 0,
    link_count          SMALLINT    NOT NULL DEFAULT 0,
    has_code_block      BOOLEAN     NOT NULL DEFAULT FALSE,
    has_question_mark   BOOLEAN     NOT NULL DEFAULT FALSE,
    is_broadcast        BOOLEAN     NOT NULL DEFAULT FALSE,  -- @here / @channel
    is_thread_reply     BOOLEAN     NOT NULL DEFAULT FALSE,

    -- HMAC-SHA256(workspace key, normalized_text). NOT a plain hash: the space
    -- of real Slack messages is small enough that SHA-256("lgtm") is a lookup
    -- away. HMAC with a key held outside the DB keeps this useful for
    -- within-workspace dedup and useless to anyone reading a table dump.
    content_fingerprint BYTEA       NOT NULL CHECK (length(content_fingerprint) = 32),

    PRIMARY KEY (message_event_id, occurred_at),
    UNIQUE (workspace_id, slack_event_id),
    UNIQUE (workspace_id, channel_id, slack_ts)
);

-- Primary read pattern: recent activity in a watched channel.
CREATE INDEX message_events_channel_recent_idx
    ON triage.message_events (workspace_id, channel_id, occurred_at DESC);

-- Phase 2 (thread summarization) reads whole threads.
CREATE INDEX message_events_thread_idx
    ON triage.message_events (workspace_id, slack_thread_ts, occurred_at)
    WHERE slack_thread_ts IS NOT NULL;

-- Near-duplicate detection ("this incident was already reported").
CREATE INDEX message_events_fingerprint_idx
    ON triage.message_events (workspace_id, content_fingerprint);


-- -----------------------------------------------------------------------------
-- message_bodies — opt-in sidecar, 1:1 with message_events
--
-- Separate table rather than a nullable column on message_events because:
--   1. Retention purge is a DELETE here and never rewrites the hot table.
--   2. A workspace revoking consent is one statement, not a column rewrite.
--   3. "Where does raw text live?" has exactly one answer, auditable in one grep.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.message_bodies (
    message_event_id    BIGINT      NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    body                TEXT        NOT NULL,
    redacted            BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Set at write time from workspaces.body_retention_days. A single scheduled
    -- job purges on this column; no per-workspace logic needed at purge time.
    purge_after         TIMESTAMPTZ NOT NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (message_event_id, occurred_at),
    FOREIGN KEY (message_event_id, occurred_at)
        REFERENCES triage.message_events (message_event_id, occurred_at)
        ON DELETE CASCADE
);

CREATE INDEX message_bodies_purge_idx ON triage.message_bodies (purge_after);


-- -----------------------------------------------------------------------------
-- raw_events — opt-in JSONB sidecar for the full Slack payload
--
-- JSONB is correct here precisely because the schema is not ours: Slack adds
-- fields without notice. Anything we filter or aggregate on gets promoted to a
-- typed column on message_events. Nothing queries this in the hot path, so it
-- gets no GIN index — add one only when a real query demands it.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.raw_events (
    message_event_id    BIGINT      NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    payload             JSONB       NOT NULL,
    purge_after         TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (message_event_id, occurred_at),
    FOREIGN KEY (message_event_id, occurred_at)
        REFERENCES triage.message_events (message_event_id, occurred_at)
        ON DELETE CASCADE
);

CREATE INDEX raw_events_purge_idx ON triage.raw_events (purge_after);


-- -----------------------------------------------------------------------------
-- classifications — append-only, versioned
--
-- We never UPDATE a classification. Re-running with a new prompt inserts a new
-- row and flips is_current on the old one. That preserves the evidence needed to
-- answer "did prompt v3 route better than v2?", which is the entire evals story.
--
-- message_type is TEXT + CHECK rather than a native enum because this taxonomy
-- will change repeatedly in the first weeks. Dropping and re-adding a CHECK is a
-- plain transactional migration; ALTER TYPE is not.
--
-- FUTURE: when clients want custom categories, promote message_type to a
-- per-workspace lookup table (workspace_id, type_key, label, description) and
-- feed the descriptions into the prompt. Deliberately out of scope for v1.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.classifications (
    classification_id   BIGINT GENERATED ALWAYS AS IDENTITY,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    message_event_id    BIGINT      NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL,

    tier                triage.routing_tier NOT NULL,

    message_type        TEXT        NOT NULL
                                    CHECK (message_type IN (
                                        'incident',       -- something is broken now
                                        'bug_report',
                                        'feature_request',
                                        'question',
                                        'access_request',
                                        'status_update',
                                        'discussion',
                                        'chatter',        -- social, no action needed
                                        'other'
                                    )),

    urgency             SMALLINT    NOT NULL CHECK (urgency BETWEEN 1 AND 5),

    -- Model self-reported confidence. Drives escalation from cheap -> deep.
    confidence          NUMERIC(4, 3) CHECK (confidence BETWEEN 0 AND 1),

    -- Resolved owner. actor_id when we could map the suggestion to a real user,
    -- label always populated for auditability even when the mapping failed.
    suggested_owner_actor_id BIGINT REFERENCES triage.actors(actor_id),
    suggested_owner_label    TEXT   NOT NULL,

    rationale           TEXT,

    -- When a cheap-tier result was escalated, this points at the superseded row.
    -- Self-referencing FK omitted intentionally: partitioned-table-compatible
    -- self FKs are awkward, and the value is analytic rather than relational.
    escalated_from_id   BIGINT,

    -- Reproducibility stamps. Without these, historical rows are uninterpretable.
    prompt_version      TEXT        NOT NULL,
    taxonomy_version    TEXT        NOT NULL,
    pipeline_version    TEXT        NOT NULL,

    -- Wall-clock from Slack event receipt to classification committed.
    e2e_latency_ms      INTEGER     CHECK (e2e_latency_ms >= 0),

    is_current          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (classification_id, created_at),
    FOREIGN KEY (message_event_id, occurred_at)
        REFERENCES triage.message_events (message_event_id, occurred_at)
        ON DELETE CASCADE
);

-- Exactly one authoritative classification per message. The partial index is
-- what makes append-only safe: history accumulates, but reads stay unambiguous.
CREATE UNIQUE INDEX classifications_current_uniq
    ON triage.classifications (workspace_id, message_event_id)
    WHERE is_current;

CREATE INDEX classifications_recent_idx
    ON triage.classifications (workspace_id, created_at DESC);

-- Powers the workflow-trigger query in Phase 3. Partial so it stays small.
CREATE INDEX classifications_urgent_idx
    ON triage.classifications (workspace_id, urgency, created_at DESC)
    WHERE is_current AND urgency >= 4;

-- Tier-effectiveness analysis: "how often does cheap tier get escalated?"
CREATE INDEX classifications_tier_idx
    ON triage.classifications (workspace_id, tier, created_at DESC);


-- -----------------------------------------------------------------------------
-- model_pricing — effective-dated reference data. INTENTIONALLY NOT TENANT-SCOPED.
--
-- Anthropic charges every tenant the same rate, so a workspace_id here would
-- force duplicate rows per tenant and break the composite FK from
-- llm_invocations that pins each call to the exact rate it was billed at.
--
-- The EXCLUDE constraint makes overlapping validity windows for the same model
-- physically impossible — you cannot accidentally seed two live prices.
--
-- Rates are per MILLION tokens, matching Anthropic's published units, so seed
-- data can be transcribed from the docs without unit conversion errors.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.model_pricing (
    model                       TEXT        NOT NULL,
    valid_from                  DATE        NOT NULL,
    valid_to                    DATE,       -- exclusive upper bound; NULL = open

    input_usd_per_mtok          NUMERIC(10, 4) NOT NULL CHECK (input_usd_per_mtok >= 0),
    output_usd_per_mtok         NUMERIC(10, 4) NOT NULL CHECK (output_usd_per_mtok >= 0),
    cache_read_usd_per_mtok     NUMERIC(10, 4) NOT NULL DEFAULT 0,
    cache_write_5m_usd_per_mtok NUMERIC(10, 4) NOT NULL DEFAULT 0,
    cache_write_1h_usd_per_mtok NUMERIC(10, 4) NOT NULL DEFAULT 0,

    source_url                  TEXT,
    verified_on                 DATE,
    notes                       TEXT,

    PRIMARY KEY (model, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from),

    -- No two price windows for the same model may overlap.
    EXCLUDE USING gist (
        model WITH =,
        daterange(valid_from, valid_to, '[)') WITH &&
    )
);


-- -----------------------------------------------------------------------------
-- llm_invocations — one row per API call. The cost fact table.
--
-- Why this is not a set of columns on classifications:
--   * An escalation is ONE classification but TWO calls.
--   * A heuristic-tier decision is one classification and ZERO calls.
--   * Phase 2 summarization makes calls with no classification at all.
--   * A retry after a 429 costs money even when the final call fails.
--
-- classification_id is nullable (a failed call has no result), message_event_id
-- is not (we always know what we were working on).
--
-- cost_usd is materialized at write time. You cannot recompute historical cost
-- from current prices — Sonnet 5's introductory rate expires 2026-08-31. The
-- composite FK pins every row to the exact rate applied.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.llm_invocations (
    invocation_id       BIGINT GENERATED ALWAYS AS IDENTITY,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,

    message_event_id    BIGINT      NOT NULL,
    classification_id   BIGINT,

    purpose             TEXT        NOT NULL
                                    CHECK (purpose IN ('classify',
                                                       'escalate',
                                                       'summarize')),

    model               TEXT        NOT NULL,
    tier                triage.routing_tier NOT NULL,

    -- Anthropic's request id, echoed in response headers. Keep it: it is what
    -- support will ask for, and it makes your logs joinable to their side.
    provider_request_id TEXT,

    input_tokens        INTEGER     NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens       INTEGER     NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens   INTEGER     NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens  INTEGER     NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),

    -- NUMERIC, never float. Per-call costs are fractions of a cent.
    cost_usd            NUMERIC(14, 8) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    pricing_valid_from  DATE        NOT NULL,

    latency_ms          INTEGER     NOT NULL CHECK (latency_ms >= 0),
    attempt             SMALLINT    NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    status              triage.invocation_status NOT NULL,
    stop_reason         TEXT,
    error_class         TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (invocation_id, created_at),

    -- Pins the exact dated rate used. Any cost figure is traceable to a row
    -- in model_pricing with a source URL and a verification date.
    FOREIGN KEY (model, pricing_valid_from)
        REFERENCES triage.model_pricing (model, valid_from)
);

-- Cost rollups and budget checks.
CREATE INDEX llm_invocations_cost_idx
    ON triage.llm_invocations (workspace_id, created_at DESC);

-- "What did the escalation path cost us this month?"
CREATE INDEX llm_invocations_tier_idx
    ON triage.llm_invocations (workspace_id, tier, created_at DESC);

CREATE INDEX llm_invocations_message_idx
    ON triage.llm_invocations (workspace_id, message_event_id);

-- Error-rate monitoring; partial so the index tracks only the interesting rows.
CREATE INDEX llm_invocations_failures_idx
    ON triage.llm_invocations (workspace_id, status, created_at DESC)
    WHERE status <> 'ok';


-- -----------------------------------------------------------------------------
-- classification_feedback — the outcome loop
--
-- Without this the project is a classifier with no ground truth. With it you can
-- report real precision per tier, which is what turns "I built a Slack bot" into
-- "cheap tier handles 78% of volume at 94% agreement."
--
-- source distinguishes a passive signal (someone reacted with ✅) from an
-- explicit correction, because they carry very different evidential weight.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.classification_feedback (
    feedback_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    classification_id   BIGINT      NOT NULL,

    source              TEXT        NOT NULL
                                    CHECK (source IN ('reaction',
                                                      'shortcut',
                                                      'admin_ui',
                                                      'backfill_eval')),
    agreed              BOOLEAN,
    corrected_type      TEXT,
    corrected_urgency   SMALLINT    CHECK (corrected_urgency BETWEEN 1 AND 5),
    corrected_owner_actor_id BIGINT REFERENCES triage.actors(actor_id),
    submitted_by_actor_id    BIGINT REFERENCES triage.actors(actor_id),
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX classification_feedback_lookup_idx
    ON triage.classification_feedback (workspace_id, classification_id);


-- -----------------------------------------------------------------------------
-- trigger_dispatches — PHASE 3 TABLE. Created now, unused in Phase 1.
--
-- Included because schema should be designed as a whole; adding it later would
-- mean revisiting the classifications indexes. No code writes here yet.
--
-- dedup_key exists because the failure mode of an alerting system is duplicate
-- pages, not missed ones.
-- -----------------------------------------------------------------------------

CREATE TABLE triage.trigger_dispatches (
    dispatch_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id        BIGINT      NOT NULL
                                    REFERENCES triage.workspaces(workspace_id)
                                    ON DELETE CASCADE,
    classification_id   BIGINT      NOT NULL,
    dedup_key           TEXT        NOT NULL,
    destination         TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'sent',
                                                      'failed', 'suppressed')),
    attempts            SMALLINT    NOT NULL DEFAULT 0,
    last_error          TEXT,
    response_payload    JSONB,
    dispatched_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (workspace_id, dedup_key)
);


-- -----------------------------------------------------------------------------
-- Reporting views
-- -----------------------------------------------------------------------------

-- Daily spend per workspace and tier. Backs the budget guardrail and the
-- eventual admin dashboard. Promote to a materialized view once row counts
-- make the scan uncomfortable.
CREATE VIEW triage.v_daily_workspace_cost AS
SELECT
    workspace_id,
    (created_at AT TIME ZONE 'UTC')::date AS usage_date,
    tier,
    model,
    count(*)                                       AS invocations,
    count(*) FILTER (WHERE status <> 'ok')         AS failures,
    sum(input_tokens)                              AS input_tokens,
    sum(output_tokens)                             AS output_tokens,
    sum(cost_usd)                                  AS cost_usd,
    round(avg(latency_ms))                         AS avg_latency_ms,
    percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms
FROM triage.llm_invocations
GROUP BY 1, 2, 3, 4;

-- Tier effectiveness: volume share, unit cost, and agreement rate per tier.
-- This is the query that justifies the whole tiered-routing design to a client.
--
-- The CTEs pre-aggregate to one row per classification BEFORE joining. Joining
-- classifications directly to llm_invocations would fan out — an escalated
-- message has two invocation rows, so count(*) would report it as two
-- classifications and halve the apparent cost per classification. Pre-aggregating
-- is the only shape that gives correct counts when a one-to-many join is present.
CREATE VIEW triage.v_tier_effectiveness AS
WITH cost_rollup AS (
    SELECT workspace_id,
           classification_id,
           sum(cost_usd)  AS cost_usd,
           count(*)       AS llm_calls
    FROM triage.llm_invocations
    WHERE classification_id IS NOT NULL
    GROUP BY 1, 2
),
feedback_rollup AS (
    SELECT workspace_id,
           classification_id,
           count(*)                        AS feedback_count,
           count(*) FILTER (WHERE agreed)  AS agreed_count
    FROM triage.classification_feedback
    GROUP BY 1, 2
)
SELECT
    c.workspace_id,
    c.tier,
    count(*)                                                AS classifications,
    count(*) FILTER (WHERE c.escalated_from_id IS NOT NULL)  AS from_escalation,
    round(avg(c.confidence), 3)                             AS avg_confidence,
    round(avg(c.e2e_latency_ms))                            AS avg_e2e_latency_ms,
    coalesce(sum(cr.llm_calls), 0)                          AS llm_calls,
    coalesce(sum(cr.cost_usd), 0)                           AS total_cost_usd,
    round(coalesce(sum(cr.cost_usd), 0) / nullif(count(*), 0), 8)
                                                            AS cost_per_classification_usd,
    coalesce(sum(fr.agreed_count), 0)                       AS agreed_count,
    coalesce(sum(fr.feedback_count), 0)                     AS feedback_count
FROM triage.classifications c
LEFT JOIN cost_rollup cr
       ON cr.workspace_id = c.workspace_id
      AND cr.classification_id = c.classification_id
LEFT JOIN feedback_rollup fr
       ON fr.workspace_id = c.workspace_id
      AND fr.classification_id = c.classification_id
WHERE c.is_current
GROUP BY 1, 2;


COMMIT;
