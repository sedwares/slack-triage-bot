# Slack AI Triage Bot

Classifies every message in a watched Slack channel by type, urgency, and
suggested owner — using a tiered routing strategy that sends most traffic to a
cheap model and reserves the expensive one for genuinely ambiguous or
high-stakes messages. Every classification, every API call, and every cent is
recorded in Postgres.

Built as a portfolio project. The interesting part is not that an LLM classifies
text; it is the data layer underneath — multi-tenancy, idempotent ingest,
effective-dated cost accounting, and privacy-first storage.

**Status:** Phase 0 and Phase 1 complete. Thread summarization, workflow
triggers, an admin dashboard, and deployment are not yet built.

---

## Why tiered routing

Three tiers, each handling what it is cheapest at:

| Tier | Handler | Cost/message | Handles |
|---|---|---|---|
| 0 — heuristic | Regex prefilter | $0 | Bots, joins, acks, emoji, obvious incidents |
| 1 — cheap | Claude Haiku 4.5 | ~$0.001 | Ordinary messages |
| 2 — deep | Claude Sonnet 5 | ~$0.002 | Ambiguous or high-stakes messages |

A large share of real Slack traffic is `lgtm`, `:tada:`, and `thanks!`. Sending
those to any model is waste, so tier 0 resolves them with string comparisons.

The deep tier is reached two ways. **Directly**, when the prefilter sees a
broadcast (`@here`), a critical term (`outage`, `P1`, `data loss`), or a very
long message — no cheap call is made, because it would have been escalated
anyway. Or by **escalation**, when the cheap model reports low confidence or
assigns high urgency. Both calls are recorded against the same message, so the
real cost of the escalation path is measurable rather than assumed.

That second escalation trigger — high urgency from a cheap model — is the
non-obvious one. It is exactly when a second opinion is worth having: the
downside of a spurious page and the downside of a missed outage are both far
larger than $0.002.

---

## Setup

Requires Python 3.12+, Docker, a Slack app in Socket Mode, and an Anthropic API
key.

```bash
git clone <your-repo-url> && cd slack-triage-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> CONTENT_HMAC_MASTER_KEY
# then fill in the Slack and Anthropic values in .env

make up        # local Postgres on :5433
make migrate   # apply schema
make seed      # load model pricing
```

### Slack app configuration

At [api.slack.com/apps](https://api.slack.com/apps), create an app and enable
**Socket Mode**.

- **Bot token scopes:** `channels:history`, `channels:read`, `chat:write`,
  `groups:history`, `groups:read`, `reactions:write`, `users:read`
- **Event subscriptions:** `message.channels`, `message.groups`
- **App-level token** with `connections:write`

Then invite the bot to a channel: `/invite @your-bot-name`

### Verify credentials before anything else

```bash
make phase0
```

This runs an echo bot that touches no database and calls no LLM. When it fails,
there is exactly one category of cause. Post in the channel; you should get a
threaded reply.

### Opt a channel in

Phase 1 refuses to process messages from an unknown workspace or an unwatched
channel — correct defaults for a multi-tenant bot, and the reason a freshly
migrated database classifies nothing until you run:

```bash
python scripts/bootstrap_dev.py \
    --team-id T01ABCDEF \
    --name "My Workspace" \
    --channel C01GENERAL --channel-name general \
    --owner U01ALICE:incident,bug_report \
    --budget 5.00

make run
```

`python scripts/bootstrap_dev.py --show` prints current configuration and spend.

---

## Repository layout

```
db/migrations/0001_init.sql     Core schema. The centerpiece — read this first.
db/migrations/0002_rls.sql      Row-level security policies
db/seed/model_pricing.sql       Effective-dated rates, transcribed from Anthropic docs

scripts/migrate.py              Forward-only migration runner with checksum guards
scripts/bootstrap_dev.py        Workspace/channel setup for local development

phase0/echo_bot.py              Credential verification. No DB, no LLM.

src/triage/
  config.py                     Typed settings; fails fast on missing secrets
  logging_setup.py              structlog JSON + PII-scrubbing processor
  main.py                       Ordered startup, graceful shutdown
  db/pool.py                    asyncpg pool; tenant_transaction() sets RLS context
  db/repository.py              All SQL lives here. Nowhere else.
  llm/client.py                 Anthropic wrapper; forced tool use, retries, costing
  llm/pricing.py                Effective-dated cost calculation
  llm/prompts.py                Prompts built from the single taxonomy definition
  triage/prefilter.py           Tier 0. Pure, synchronous, heavily tested.
  triage/router.py              Tier selection and escalation
  triage/pipeline.py            Ingest -> route -> persist
  triage/schemas.py             Taxonomy + structured-output tool schema
  slack/handlers.py             Bolt handlers. Ack fast, work in background.
  slack/privacy.py              HMAC fingerprinting, per-workspace key derivation
```

---

## Schema design decisions

Full reasoning is in the comments of `0001_init.sql`. Summary of the choices
that shaped everything else:

**Shared-schema multi-tenancy with RLS underneath.** `workspace_id` on every
tenant table, plus row-level security policies reading a per-transaction
`app.workspace_id` setting. The app still filters explicitly — RLS is the
seatbelt, not the steering wheel. `SET LOCAL` rather than `SET`, so a pooled
connection cannot carry one tenant's context into another's transaction.

`model_pricing` is deliberately **not** tenant-scoped. Anthropic charges everyone
the same rate; a `workspace_id` there would force duplicate rows per tenant and
break the FK that pins each invocation to the exact rate it was billed at.

**Idempotency is a column, not a hope.** Slack retries any event it does not get
a 200 for within three seconds — including events your own slow handler caused.
`UNIQUE (workspace_id, slack_event_id)` plus `ON CONFLICT DO NOTHING RETURNING`
means a duplicate delivery is detected before a single token is spent.

**Bodies in a sidecar, fingerprints via HMAC.** `message_events` never holds
text. Bodies live in `message_bodies` with a `purge_after` timestamp, so
retention is one DELETE that never touches the hot analytical table.
Fingerprints are HMAC-SHA256 with a per-workspace key held outside the
database — plain SHA-256 of Slack messages is not privacy, since `sha256("lgtm")`
is one rainbow table away.

**Classifications are append-only and versioned.** Never UPDATEd. Each row
carries `prompt_version`, `taxonomy_version`, and `pipeline_version`, with a
partial unique index (`WHERE is_current`) keeping exactly one row authoritative.
Re-running a classifier with a new prompt is a legitimate operation, not a
data-loss event — which is what makes "did v3 route better than v2?" answerable.

**Cost is its own fact table.** `llm_invocations` is at the grain of one API
call, because an escalation is one classification and two calls, a heuristic
decision is one classification and zero calls, and a retried 429 costs money even
when it fails. Rates are effective-dated with a `daterange` EXCLUDE constraint
making overlapping windows physically impossible, and each invocation carries a
composite FK to the exact price row used.

That last point is live right now, not theoretical: **Claude Sonnet 5 runs at
introductory pricing of $2/$10 per MTok through 2026-08-31, then $3/$15 from
2026-09-01.** Both windows are already seeded. On September 1st new calls price
at the new rate, historical rows keep what they were billed, and no deploy is
needed.

**Partitioning is shaped for, not enabled.** `message_events` and
`llm_invocations` have PKs of `(id, time_column)` so a later conversion to
monthly range partitioning is a mechanical migration rather than a redesign.
Enabling it on day one would complicate every local debugging query for no
benefit at current volume.

---

## Verification

Schema and constraints were validated against a real PostgreSQL 16 instance.
Both migrations and the seed apply cleanly to a fresh database: 11 tables,
2 views, 34 indexes, 10 RLS policies.

Constraints proven to reject bad data:

| Attempt | Result |
|---|---|
| Overlapping price windows for one model | Rejected by `daterange` EXCLUDE |
| Duplicate `slack_event_id` | Rejected — idempotency holds |
| Malformed content fingerprint | Rejected by length CHECK |
| Invented `message_type` | Rejected by taxonomy CHECK |
| Two `is_current` rows for one message | Rejected by partial unique index |
| Invocation citing an unseeded model | Rejected by composite pricing FK |

Tenant isolation proven with a non-superuser role: no context yields zero rows
(not an error), each context sees only its own tenant, context vanishes at
COMMIT, and a cross-tenant INSERT is rejected by the policy's `WITH CHECK`.

```bash
make test    # 42 unit tests
make lint
```

---

## Operational notes

### RLS is bypassed by superusers

Migration 0002 enables and FORCEs row-level security, but Postgres exempts
superusers and `BYPASSRLS` roles unconditionally — `FORCE` only extends
enforcement to the table *owner*. The default docker-compose user is a
superuser, so **locally the policies exist but are never evaluated.**

Fine for development. Before deploying, connect as a dedicated non-superuser:

```sql
CREATE ROLE triage_svc LOGIN PASSWORD '<secret>' IN ROLE triage_app;
ALTER ROLE triage_svc NOSUPERUSER NOBYPASSRLS;
```

`scripts/bootstrap_dev.py` prints a warning when the connecting role bypasses
RLS, so this cannot become a deploy-day surprise.

### Prompt caching may not help at low volume

The system prompt is marked with `cache_control`, which bills cache hits at 0.1x
base input. But the cache TTL is five minutes — on a quiet channel every call is
a cache *write* at 1.25x, which is worse than no caching at all. Measure
`cache_read_tokens` against `cache_write_tokens` in your first week before
assuming it is a win:

```sql
SELECT model,
       sum(cache_read_tokens)  AS reads,
       sum(cache_write_tokens) AS writes,
       sum(cost_usd)           AS spend
FROM triage.llm_invocations
GROUP BY model;
```

### Re-classification and cost attribution

When a classification is superseded by a re-run, its invocations stay attached to
the superseded row. `v_tier_effectiveness` filters on `is_current`, so
re-classified messages show zero cost. That is arguably correct — the new
classification genuinely cost nothing if it came from a backfill — but decide
deliberately before building evals on top of it.

### Useful queries

```sql
-- Tier effectiveness: the query that justifies the whole design
SELECT * FROM triage.v_tier_effectiveness;

-- Daily spend by tier and model
SELECT * FROM triage.v_daily_workspace_cost ORDER BY usage_date DESC;

-- Which prefilter rules actually fire (a rule that never fires is dead weight)
SELECT tier, count(*) FROM triage.classifications
WHERE is_current GROUP BY tier;

-- Escalation rate
SELECT count(*) FILTER (WHERE purpose = 'escalate')::float
       / nullif(count(*) FILTER (WHERE purpose = 'classify'), 0) AS escalation_rate
FROM triage.llm_invocations;
```

---

## Not yet built

- **Phase 2** — thread summarization via message shortcut
- **Phase 3** — workflow trigger dispatch (`trigger_dispatches` table exists,
  nothing writes to it; `PipelineOutcome.should_trigger` is computed and logged)
- **Phase 4** — admin dashboard, Fly.io deployment, retention purge job

---

## Security

No secrets are committed. `.gitignore` blocks `.env`, key material, and log
files (structured logs can contain message content). `make check-secrets` fails
if anything token-shaped is staged — wire it into a pre-commit hook.

If you find a credential in this repository's history, it is a mistake and should
be reported so it can be rotated.
