#!/usr/bin/env python3
"""Minimal forward-only migration runner.

Why raw SQL files instead of Alembic: the Postgres layer is the point of this
project, and hand-written DDL shows constraints, partial indexes, EXCLUDE
constraints, and RLS policies that an ORM autogenerate step would not produce.
Alembic earns its keep when the schema is derived from models; here the schema
is the artifact.

Contract:
  * Files in db/migrations/ named NNNN_description.sql, applied in order.
  * Each file runs inside a single transaction. A failure rolls that file back
    entirely — no half-applied migrations to untangle by hand.
  * Applied filenames and their checksums are recorded. Editing a migration that
    has already run is detected and refused, because that is how two
    environments silently diverge.

Usage:
    python scripts/migrate.py up      # apply pending migrations
    python scripts/migrate.py seed    # load reference data
    python scripts/migrate.py status  # show applied vs pending
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
SEED_DIR = REPO_ROOT / "db" / "seed"

# Lives in the default schema, not `triage`, so it survives a schema drop.
_TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER     NOT NULL
);
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _database_url() -> str:
    load_dotenv(REPO_ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env first.")
    # asyncpg does not accept the SQLAlchemy-style driver suffix.
    if "+" in url.split("://", 1)[0]:
        scheme, rest = url.split("://", 1)
        url = f"{scheme.split('+', 1)[0]}://{rest}"
    return url


async def _apply_file(conn: asyncpg.Connection, path: Path, *, track: bool) -> None:
    """Execute one SQL file inside a transaction and optionally record it."""
    sql = path.read_text(encoding="utf-8")
    digest = _checksum(sql)

    loop = asyncio.get_event_loop()
    started = loop.time()

    # Migration files carry their own BEGIN/COMMIT; asyncpg's execute() runs a
    # multi-statement script in an implicit transaction either way, so a failure
    # anywhere rolls the whole file back.
    await conn.execute(sql)

    duration_ms = int((loop.time() - started) * 1000)

    if track:
        await conn.execute(
            """
            INSERT INTO public.schema_migrations (filename, checksum, duration_ms)
            VALUES ($1, $2, $3)
            ON CONFLICT (filename) DO NOTHING
            """,
            path.name,
            digest,
            duration_ms,
        )

    print(f"  applied {path.name} ({duration_ms} ms)")


async def cmd_up() -> None:
    conn = await asyncpg.connect(_database_url())
    try:
        await conn.execute(_TRACKING_TABLE_DDL)

        applied: dict[str, str] = {
            r["filename"]: r["checksum"]
            for r in await conn.fetch(
                "SELECT filename, checksum FROM public.schema_migrations"
            )
        }

        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            print("no migration files found")
            return

        pending = []
        for path in files:
            digest = _checksum(path.read_text(encoding="utf-8"))
            if path.name not in applied:
                pending.append(path)
            elif applied[path.name] != digest:
                # Refuse rather than reapply. A changed migration means this
                # database and a colleague's have diverged; reapplying would
                # hide that, not fix it.
                sys.exit(
                    f"ERROR: {path.name} was modified after being applied "
                    f"(recorded {applied[path.name]}, now {digest}).\n"
                    "Write a new migration instead of editing an applied one."
                )

        if not pending:
            print("database is up to date")
            return

        print(f"applying {len(pending)} migration(s):")
        for path in pending:
            await _apply_file(conn, path, track=True)
        print("done")
    finally:
        await conn.close()


async def cmd_seed() -> None:
    """Load reference data. Seed files must be idempotent (ON CONFLICT ...)."""
    conn = await asyncpg.connect(_database_url())
    try:
        files = sorted(SEED_DIR.glob("*.sql"))
        if not files:
            print("no seed files found")
            return
        print(f"seeding {len(files)} file(s):")
        for path in files:
            await _apply_file(conn, path, track=False)

        count = await conn.fetchval("SELECT count(*) FROM triage.model_pricing")
        print(f"done — {count} pricing rows loaded")
    finally:
        await conn.close()


async def cmd_status() -> None:
    conn = await asyncpg.connect(_database_url())
    try:
        await conn.execute(_TRACKING_TABLE_DDL)
        applied = {
            r["filename"] for r in await conn.fetch(
                "SELECT filename FROM public.schema_migrations"
            )
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            mark = "applied" if path.name in applied else "PENDING"
            print(f"  [{mark:>7}] {path.name}")
    finally:
        await conn.close()


def main() -> None:
    commands = {"up": cmd_up, "seed": cmd_seed, "status": cmd_status}
    command = sys.argv[1] if len(sys.argv) > 1 else "up"
    if command not in commands:
        sys.exit(f"unknown command '{command}'. Use: {', '.join(commands)}")
    asyncio.run(commands[command]())


if __name__ == "__main__":
    main()
