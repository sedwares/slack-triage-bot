"""Connection pooling and tenant-scoped transactions.

The important thing in this module is :func:`tenant_transaction`. It is the only
sanctioned way to touch tenant data, and it sets ``app.workspace_id`` with
``SET LOCAL`` so the RLS policies from migration 0002 can enforce isolation.

``SET LOCAL`` rather than ``SET`` is load-bearing: ``SET LOCAL`` is scoped to the
transaction and is reset on commit or rollback. A plain ``SET`` would persist on
the pooled connection and leak one tenant's context into the next tenant's
transaction — which is precisely the bug RLS is supposed to catch.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from triage.config import Settings
from triage.logging_setup import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup, run once when a connection joins the pool."""
    # asyncpg hands back JSONB as raw strings by default. Decoding here means
    # call sites get dicts and never sprinkle json.loads() around.
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    # Every query is unqualified-safe without repeating the schema name.
    await conn.execute("SET search_path = triage, public")


async def init_pool(settings: Settings) -> asyncpg.Pool:
    """Create the process-wide connection pool. Call once at startup."""
    global _pool
    if _pool is not None:
        return _pool

    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=settings.db_command_timeout_seconds,
        init=_init_connection,
        # Statement cache off is the safe default when a connection pooler
        # (PgBouncer in transaction mode, common on Fly.io) sits in front.
        # Cached plans break when the underlying server connection changes.
        statement_cache_size=0,
    )

    version = await _pool.fetchval("SELECT version()")
    log.info(
        "db_pool_ready",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        server=version.split(",")[0] if version else "unknown",
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    """Return the initialized pool, or raise if startup was skipped."""
    if _pool is None:
        raise RuntimeError("Connection pool not initialized; call init_pool() first.")
    return _pool


async def close_pool() -> None:
    """Close the pool during graceful shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db_pool_closed")


@asynccontextmanager
async def tenant_transaction(workspace_id: int) -> AsyncIterator[asyncpg.Connection]:
    """Open a transaction bound to one workspace.

    All tenant data access goes through here. The RLS policies read
    ``app.workspace_id``; without it set, every policy evaluates to NULL and the
    tables return zero rows. That is intentional — a missing tenant context
    should produce visibly empty results, not silent cross-tenant reads.

    Args:
        workspace_id: Internal workspace primary key (not the Slack team ID).

    Yields:
        A connection inside an open transaction.

    Example:
        >>> async with tenant_transaction(ws_id) as conn:
        ...     await conn.fetch("SELECT * FROM classifications LIMIT 10")
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Parameterized via set_config rather than string interpolation:
            # SET LOCAL does not accept bind parameters, and building the
            # statement by hand would be an injection point.
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                str(workspace_id),
            )
            yield conn


@asynccontextmanager
async def admin_transaction() -> AsyncIterator[asyncpg.Connection]:
    """Open a transaction with no tenant scope.

    Only for operations that are genuinely cross-tenant or pre-tenant:
    resolving a Slack team ID to a workspace row, reading global reference data
    like ``model_pricing``, and retention purges. Never for request handling.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn
