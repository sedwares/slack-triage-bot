-- =============================================================================
-- 0002_rls.sql — Row-Level Security for tenant isolation
--
-- This is a SEATBELT, not the steering wheel. The application still writes
-- explicit `WHERE workspace_id = $1` on every query. RLS exists so that the day
-- someone forgets, the database refuses rather than leaking.
--
-- Mechanism: the app sets `SET LOCAL app.workspace_id = '<id>'` at the start of
-- each transaction (see src/triage/db/pool.py). Policies read that setting.
-- SET LOCAL is transaction-scoped, so a pooled connection cannot carry one
-- tenant's context into another tenant's transaction.
--
-- Note the `true` second argument to current_setting(): it returns NULL instead
-- of raising when the setting is absent. Combined with the policy shape below,
-- an unset context yields ZERO rows rather than an error that might get caught
-- and swallowed somewhere.
--
-- model_pricing is deliberately excluded — it is global reference data.
-- =============================================================================

BEGIN;

SET LOCAL search_path = triage, public;

-- Role the application connects as. It must NOT be the table owner and must NOT
-- be superuser: both bypass RLS entirely.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'triage_app') THEN
        CREATE ROLE triage_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA triage TO triage_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA triage TO triage_app;
GRANT SELECT ON triage.model_pricing TO triage_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA triage TO triage_app;

-- Deliberately no DELETE grant on the append-only fact tables; purge jobs run
-- as a separate role. Bodies and raw events are the exception (retention).
GRANT DELETE ON triage.message_bodies, triage.raw_events TO triage_app;


-- Helper: current tenant from session config, NULL when unset.
CREATE OR REPLACE FUNCTION triage.current_workspace_id()
RETURNS BIGINT
LANGUAGE sql
STABLE
AS $$
    SELECT nullif(current_setting('app.workspace_id', true), '')::bigint;
$$;


-- Apply an identical policy to every tenant-scoped table.
DO $$
DECLARE
    t TEXT;
    tenant_tables TEXT[] := ARRAY[
        'workspaces', 'channels', 'actors', 'message_events',
        'message_bodies', 'raw_events', 'classifications',
        'llm_invocations', 'classification_feedback', 'trigger_dispatches'
    ];
    key_col TEXT;
BEGIN
    FOREACH t IN ARRAY tenant_tables LOOP
        -- The tenant root keys on its own PK; everything else on workspace_id.
        key_col := CASE WHEN t = 'workspaces' THEN 'workspace_id'
                        ELSE 'workspace_id' END;

        EXECUTE format('ALTER TABLE triage.%I ENABLE ROW LEVEL SECURITY', t);

        -- FORCE makes the policy apply to the table owner too, so a migration
        -- run or a psql session as owner cannot silently read across tenants.
        EXECUTE format('ALTER TABLE triage.%I FORCE ROW LEVEL SECURITY', t);

        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON triage.%I
            USING (%I = triage.current_workspace_id())
            WITH CHECK (%I = triage.current_workspace_id())
        $f$, t, key_col, key_col);
    END LOOP;
END
$$;


-- Escape hatch for migrations, purge jobs, and cross-tenant analytics. Grant
-- this role only to operational tooling, never to the request-serving app.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'triage_admin') THEN
        CREATE ROLE triage_admin NOLOGIN BYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA triage TO triage_admin;
GRANT ALL ON ALL TABLES IN SCHEMA triage TO triage_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA triage TO triage_admin;

COMMIT;
