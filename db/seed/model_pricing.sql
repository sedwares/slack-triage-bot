-- =============================================================================
-- Seed: model_pricing
--
-- Rates transcribed from https://platform.claude.com/docs/en/about-claude/pricing
-- Verified 2026-08-03. All values are USD per MILLION tokens, matching the units
-- Anthropic publishes, so transcription errors are easy to spot.
--
-- NOTE THE SONNET 5 SPLIT. Introductory pricing ($2/$10) runs through
-- 2026-08-31; standard pricing ($3/$15) starts 2026-09-01. This is exactly why
-- pricing is effective-dated: on September 1st, every invocation automatically
-- prices at the new rate and every historical row keeps the rate it was billed
-- at. No code change, no backfill, no silently wrong dashboard.
--
-- The EXCLUDE constraint on model_pricing will reject this seed if the date
-- windows overlap, so a typo here fails loudly at seed time.
--
-- Idempotent: safe to re-run.
-- =============================================================================

SET search_path = triage, public;

INSERT INTO triage.model_pricing (
    model,
    valid_from, valid_to,
    input_usd_per_mtok, output_usd_per_mtok,
    cache_read_usd_per_mtok,
    cache_write_5m_usd_per_mtok, cache_write_1h_usd_per_mtok,
    source_url, verified_on, notes
) VALUES
    -- Cheap tier. Handles the bulk of routine classification.
    ('claude-haiku-4-5-20251001',
     DATE '2025-10-01', NULL,
     1.0000, 5.0000, 0.1000, 1.2500, 2.0000,
     'https://platform.claude.com/docs/en/about-claude/pricing',
     DATE '2026-08-03',
     'Cheap tier for routine classification.'),

    -- Deep tier, introductory window.
    ('claude-sonnet-5',
     DATE '2026-06-30', DATE '2026-09-01',
     2.0000, 10.0000, 0.2000, 2.5000, 4.0000,
     'https://platform.claude.com/docs/en/about-claude/pricing',
     DATE '2026-08-03',
     'Introductory pricing through 2026-08-31 inclusive.'),

    -- Deep tier, standard rate. Pre-seeded so the transition needs no deploy.
    ('claude-sonnet-5',
     DATE '2026-09-01', NULL,
     3.0000, 15.0000, 0.3000, 3.7500, 6.0000,
     'https://platform.claude.com/docs/en/about-claude/pricing',
     DATE '2026-08-03',
     'Standard pricing from 2026-09-01.'),

    -- Not used by the v1 router. Seeded so that ad-hoc escalation experiments
    -- do not fail the composite FK from llm_invocations.
    ('claude-opus-5',
     DATE '2026-07-24', NULL,
     5.0000, 25.0000, 0.5000, 6.2500, 10.0000,
     'https://platform.claude.com/docs/en/about-claude/pricing',
     DATE '2026-08-03',
     'Not routed to in v1; available for experiments.')

ON CONFLICT (model, valid_from) DO UPDATE SET
    valid_to                    = EXCLUDED.valid_to,
    input_usd_per_mtok          = EXCLUDED.input_usd_per_mtok,
    output_usd_per_mtok         = EXCLUDED.output_usd_per_mtok,
    cache_read_usd_per_mtok     = EXCLUDED.cache_read_usd_per_mtok,
    cache_write_5m_usd_per_mtok = EXCLUDED.cache_write_5m_usd_per_mtok,
    cache_write_1h_usd_per_mtok = EXCLUDED.cache_write_1h_usd_per_mtok,
    source_url                  = EXCLUDED.source_url,
    verified_on                 = EXCLUDED.verified_on,
    notes                       = EXCLUDED.notes;
