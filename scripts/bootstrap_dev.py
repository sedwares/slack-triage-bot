#!/usr/bin/env python3
"""Bootstrap a workspace for local development.

Phase 1 deliberately refuses to process messages from an unknown workspace or an
unwatched channel. Those are the right defaults for a multi-tenant bot — a fresh
install should observe nothing until someone opts a channel in — but it means a
freshly migrated database classifies exactly zero messages until this script has
run.

Connects with raw asyncpg rather than going through ``triage.config``, so it
works before any Slack or Anthropic credentials exist. The only environment
variables it needs are ``DATABASE_URL`` and (optionally)
``CONTENT_HMAC_KEY_GENERATION``.

Usage:
    # Discover your IDs first — run `make phase0` and read the printed team ID,
    # then right-click the channel in Slack -> View channel details -> ID.
    python scripts/bootstrap_dev.py \\
        --team-id T01ABCDEF \\
        --name "My Workspace" \\
        --channel C01GENERAL \\
        --channel-name general

    # Optional extras
    python scripts/bootstrap_dev.py --team-id T01ABCDEF --channel C01GENERAL \\
        --owner U01ALICE:incident,bug_report \\
        --budget 5.00 \\
        --store-bodies

    # Inspect what is currently configured
    python scripts/bootstrap_dev.py --show
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from decimal import Decimal
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirrors triage.slack.privacy.key_id. Duplicated deliberately: this script must
# run without importing the application package, which requires full config.
# The duplication is three lines and is covered by the assertion in the docstring
# of that function — if you change one, change both.
def _key_id(generation: str, slack_team_id: str) -> str:
    suffix = hashlib.sha256(slack_team_id.encode("utf-8")).hexdigest()[:8]
    return f"{generation}:{suffix}"


def _database_url() -> str:
    load_dotenv(REPO_ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env first.")
    if "+" in url.split("://", 1)[0]:
        scheme, rest = url.split("://", 1)
        url = f"{scheme.split('+', 1)[0]}://{rest}"
    return url


async def _warn_if_rls_bypassed(conn: asyncpg.Connection) -> None:
    """Report whether the connecting role actually has RLS enforced against it.

    This matters more than it looks. Migration 0002 enables and FORCEs row-level
    security, but Postgres exempts superusers and BYPASSRLS roles unconditionally
    — FORCE only extends enforcement to the table *owner*, not past a superuser
    bit. The default docker-compose user is a superuser, so in local development
    the tenant-isolation policies are present but never evaluated.

    That is acceptable for local work and NOT acceptable in production, where a
    missing WHERE clause would then leak across tenants with the seatbelt sitting
    unused. Printing it here keeps it from being a surprise at deploy time.
    """
    row = await conn.fetchrow(
        """
        SELECT current_user AS role,
               rolsuper     AS is_superuser,
               rolbypassrls AS bypasses_rls
        FROM pg_roles
        WHERE rolname = current_user
        """
    )
    if row and (row["is_superuser"] or row["bypasses_rls"]):
        print(
            f"\n  ! RLS NOT ENFORCED for role '{row['role']}'"
            f" (superuser={row['is_superuser']}, bypassrls={row['bypasses_rls']}).\n"
            "    Fine locally. Before deploying, create a dedicated login role and\n"
            "    point DATABASE_URL at it:\n\n"
            "      CREATE ROLE triage_svc LOGIN PASSWORD '<secret>' "
            "IN ROLE triage_app;\n"
            "      ALTER ROLE triage_svc NOSUPERUSER NOBYPASSRLS;\n"
        )


async def show(conn: asyncpg.Connection) -> None:
    """Print current configuration and a small activity summary."""
    workspaces = await conn.fetch(
        """
        SELECT workspace_id, slack_team_id, name, status::text AS status,
               store_message_bodies, deep_tier_enabled,
               urgency_trigger_threshold, monthly_budget_usd
        FROM triage.workspaces
        ORDER BY workspace_id
        """
    )
    if not workspaces:
        print("No workspaces configured. Run without --show to create one.")
        return

    for ws in workspaces:
        print(f"\nworkspace {ws['workspace_id']}: {ws['name']} ({ws['slack_team_id']})")
        print(f"  status            : {ws['status']}")
        print(f"  store bodies      : {ws['store_message_bodies']}")
        print(f"  deep tier enabled : {ws['deep_tier_enabled']}")
        print(f"  trigger at urgency: {ws['urgency_trigger_threshold']}")
        print(f"  monthly budget    : {ws['monthly_budget_usd'] or 'unlimited'}")

        channels = await conn.fetch(
            """
            SELECT slack_channel_id, name, is_watched
            FROM triage.channels WHERE workspace_id = $1 ORDER BY channel_id
            """,
            ws["workspace_id"],
        )
        print("  channels:")
        for ch in channels or []:
            mark = "WATCHED" if ch["is_watched"] else "ignored"
            print(f"    [{mark:>7}] {ch['slack_channel_id']} ({ch['name'] or '?'})")
        if not channels:
            print("    (none)")

        owners = await conn.fetch(
            """
            SELECT slack_user_id, owns_message_types
            FROM triage.actors
            WHERE workspace_id = $1 AND cardinality(owns_message_types) > 0
            """,
            ws["workspace_id"],
        )
        if owners:
            print("  owners:")
            for o in owners:
                print(f"    {o['slack_user_id']} -> {', '.join(o['owns_message_types'])}")

        stats = await conn.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM triage.message_events WHERE workspace_id = $1)
                AS events,
              (SELECT count(*) FROM triage.classifications
                WHERE workspace_id = $1 AND is_current) AS classified,
              (SELECT coalesce(sum(cost_usd), 0) FROM triage.llm_invocations
                WHERE workspace_id = $1) AS spend
            """,
            ws["workspace_id"],
        )
        print(
            f"  activity          : {stats['events']} events, "
            f"{stats['classified']} classified, ${stats['spend']:.6f} spent"
        )


async def bootstrap(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(_database_url())
    try:
        await _warn_if_rls_bypassed(conn)

        if args.show:
            await show(conn)
            return

        if not args.team_id:
            sys.exit("--team-id is required. Run `make phase0` to find yours.")

        # --- workspace ------------------------------------------------------
        workspace_id = await conn.fetchval(
            """
            INSERT INTO triage.workspaces
                (slack_team_id, name, content_hmac_key_id,
                 store_message_bodies, monthly_budget_usd)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (slack_team_id) DO UPDATE
                SET name = EXCLUDED.name,
                    store_message_bodies = EXCLUDED.store_message_bodies,
                    monthly_budget_usd = EXCLUDED.monthly_budget_usd,
                    status = 'active',
                    updated_at = now()
            RETURNING workspace_id
            """,
            args.team_id,
            args.name or args.team_id,
            _key_id(
                os.environ.get("CONTENT_HMAC_KEY_GENERATION", "v1"), args.team_id
            ),
            args.store_bodies,
            Decimal(args.budget) if args.budget else None,
        )
        print(f"✓ workspace {workspace_id} ({args.team_id})")

        if args.store_bodies:
            # Loud because it is a privacy decision, not a convenience toggle.
            print(
                "  ! message body storage is ON for this workspace. "
                "Bodies purge after the configured retention window."
            )

        # --- channels -------------------------------------------------------
        # Explicit SQL rather than ChannelRepository.upsert, which deliberately
        # never flips is_watched — that is an admin action, and this script is
        # the admin path.
        for slack_channel_id in args.channel or []:
            await conn.execute(
                """
                INSERT INTO triage.channels
                    (workspace_id, slack_channel_id, name, is_watched)
                VALUES ($1, $2, $3, TRUE)
                ON CONFLICT (workspace_id, slack_channel_id) DO UPDATE
                    SET is_watched = TRUE,
                        name = COALESCE(EXCLUDED.name, triage.channels.name),
                        updated_at = now()
                """,
                workspace_id,
                slack_channel_id,
                args.channel_name,
            )
            print(f"✓ watching channel {slack_channel_id}")

        # --- owners ---------------------------------------------------------
        for spec in args.owner or []:
            if ":" not in spec:
                sys.exit(
                    f"Bad --owner value {spec!r}. "
                    "Expected USERID:type1,type2 (e.g. U01ALICE:incident,bug_report)"
                )
            slack_user_id, types_csv = spec.split(":", 1)
            types = [t.strip() for t in types_csv.split(",") if t.strip()]
            await conn.execute(
                """
                INSERT INTO triage.actors
                    (workspace_id, slack_user_id, owns_message_types)
                VALUES ($1, $2, $3)
                ON CONFLICT (workspace_id, slack_user_id) DO UPDATE
                    SET owns_message_types = EXCLUDED.owns_message_types,
                        updated_at = now()
                """,
                workspace_id,
                slack_user_id,
                types,
            )
            print(f"✓ {slack_user_id} owns {', '.join(types)}")

        # --- sanity check ---------------------------------------------------
        # Pricing must be seeded or the first classification fails at startup.
        priced = await conn.fetchval("SELECT count(*) FROM triage.model_pricing")
        if priced == 0:
            print(
                "\n  ! model_pricing is empty. Run `make seed` — the app refuses "
                "to start without it, by design."
            )

        watched = await conn.fetchval(
            "SELECT count(*) FROM triage.channels "
            "WHERE workspace_id = $1 AND is_watched",
            workspace_id,
        )
        if watched == 0:
            print(
                "\n  ! no watched channels. The bot will ingest nothing. "
                "Re-run with --channel C0XXXXXXX."
            )
        else:
            print(f"\nReady. Run `make run`, then post in one of the {watched} "
                  "watched channel(s).")

    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure a workspace for local development."
    )
    parser.add_argument("--team-id", help="Slack team ID, e.g. T01ABCDEF")
    parser.add_argument("--name", help="Human-readable workspace name")
    parser.add_argument(
        "--channel",
        action="append",
        help="Slack channel ID to watch. Repeatable.",
    )
    parser.add_argument("--channel-name", help="Channel name, for readable logs")
    parser.add_argument(
        "--owner",
        action="append",
        help="USERID:type1,type2 — default owner for those message types. Repeatable.",
    )
    parser.add_argument(
        "--budget",
        help="Monthly spend cap in USD. Omit for unlimited.",
    )
    parser.add_argument(
        "--store-bodies",
        action="store_true",
        help="Opt this workspace into storing message text. Off by default.",
    )
    parser.add_argument(
        "--show", action="store_true", help="Print current configuration and exit."
    )
    asyncio.run(bootstrap(parser.parse_args()))


if __name__ == "__main__":
    main()
