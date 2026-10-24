"""Seed page navigation aliases and local embeddings."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from services.api.app.domain.page.nav_alias import build_default_page_nav_alias_seed_rows
from services.api.app.infrastructure.db.page_nav_alias_repository import (
    PostgresPageNavAliasRepository,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
_REQUIRED_TABLES = ("page_nav_alias_term", "page_nav_alias_embedding")


def _database_url() -> str:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    raw_url = os.getenv("ALIOTH_DATABASE_URL")
    if not raw_url:
        raise RuntimeError("ALIOTH_DATABASE_URL is required")
    return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        existing_rows = await connection.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            """,
            list(_REQUIRED_TABLES),
        )
    existing = {str(row["table_name"]) for row in existing_rows}
    missing = sorted(set(_REQUIRED_TABLES) - existing)
    if missing:
        raise RuntimeError(
            "Missing page nav alias tables, run Alembic first: " + ", ".join(missing)
        )


async def main() -> None:
    rows = build_default_page_nav_alias_seed_rows()
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=1)
    try:
        await _ensure_schema(pool)
        repository = PostgresPageNavAliasRepository(pool=pool)
        upserted_count = await repository.upsert_seed_rows(rows)
    finally:
        await pool.close()

    print(f"seeded page_nav_alias rows={upserted_count}")


if __name__ == "__main__":
    asyncio.run(main())