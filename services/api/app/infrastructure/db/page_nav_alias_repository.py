"""页面导航别名 PostgreSQL 仓储。"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from services.api.app.domain.page.nav_alias import (
    PAGE_NAV_ALIAS_EMBEDDING_MODEL,
    PageNavAliasMatch,
    PageNavAliasSeedRow,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresPageNavAliasRepository:
    """读取和写入已确认的页面导航别名及本地向量。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def find_confirmed_exact(
        self,
        normalized_text: str,
    ) -> PageNavAliasMatch | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    canonical_code,
                    page_subtype_code,
                    label,
                    source_category,
                    confidence
                FROM page_nav_alias_term
                WHERE normalized_text = $1
                  AND review_status = 'confirmed'
                  AND enabled IS TRUE
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 1
                """,
                normalized_text,
            )
        return _row_to_match(row, match_type="exact")

    async def find_confirmed_vector_match(
        self,
        *,
        embedding: tuple[float, ...],
        embedding_model: str,
        min_similarity: float,
    ) -> PageNavAliasMatch | None:
        pool = await self._get_pool()
        vector_literal = _vector_literal(embedding)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH query_embedding AS (
                    SELECT $1::vector AS embedding
                )
                SELECT
                    t.canonical_code,
                    t.page_subtype_code,
                    t.label,
                    t.source_category,
                    GREATEST(
                        t.confidence::float,
                        (1 - (e.embedding <=> query_embedding.embedding))::float
                    ) AS confidence
                FROM page_nav_alias_embedding e
                JOIN page_nav_alias_term t ON t.id = e.alias_term_id
                CROSS JOIN query_embedding
                WHERE t.review_status = 'confirmed'
                  AND t.enabled IS TRUE
                  AND e.embedding_model = $2
                  AND (1 - (e.embedding <=> query_embedding.embedding)) >= $3
                ORDER BY
                    e.embedding <=> query_embedding.embedding ASC,
                    t.confidence DESC,
                    t.updated_at DESC
                LIMIT 1
                """,
                vector_literal,
                embedding_model,
                min_similarity,
            )
        return _row_to_match(row, match_type="vector")

    async def upsert_seed_rows(self, rows: tuple[PageNavAliasSeedRow, ...]) -> int:
        """将默认别名和本地 embedding 幂等入库为 confirmed/enabled 数据。"""
        if not rows:
            return 0

        pool = await self._get_pool()
        upserted_count = 0
        async with pool.acquire() as connection, connection.transaction():
            for row in rows:
                alias_term_id = await connection.fetchval(
                    """
                    SELECT id
                    FROM page_nav_alias_term
                    WHERE normalized_text = $1
                      AND canonical_code = $2
                      AND COALESCE(page_subtype_code, '') = COALESCE($3, '')
                    LIMIT 1
                    """,
                    row.normalized_text,
                    row.canonical_code,
                    row.page_subtype_code,
                )
                if alias_term_id is None:
                    alias_term_id = await connection.fetchval(
                        """
                        INSERT INTO page_nav_alias_term (
                            canonical_code,
                            page_subtype_code,
                            label,
                            description,
                            normalized_text,
                            embedding_text,
                            source_category,
                            confidence,
                            review_status,
                            enabled
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'confirmed', true)
                        RETURNING id
                        """,
                        row.canonical_code,
                        row.page_subtype_code,
                        row.label,
                        row.description,
                        row.normalized_text,
                        row.embedding_text,
                        row.source_category,
                        row.confidence,
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE page_nav_alias_term
                        SET
                            label = $4,
                            description = $5,
                            embedding_text = $6,
                            source_category = $7,
                            confidence = GREATEST(confidence, $8),
                            review_status = 'confirmed',
                            enabled = true,
                            updated_at = now()
                        WHERE id = $1::uuid
                          AND canonical_code = $2
                          AND COALESCE(page_subtype_code, '') = COALESCE($3, '')
                        """,
                        alias_term_id,
                        row.canonical_code,
                        row.page_subtype_code,
                        row.label,
                        row.description,
                        row.embedding_text,
                        row.source_category,
                        row.confidence,
                    )

                await connection.execute(
                    """
                    INSERT INTO page_nav_alias_embedding (
                        alias_term_id,
                        embedding_model,
                        embedding,
                        content_hash
                    )
                    VALUES ($1::uuid, $2, $3::vector, $4)
                    ON CONFLICT (alias_term_id, embedding_model, content_hash) DO NOTHING
                    """,
                    alias_term_id,
                    PAGE_NAV_ALIAS_EMBEDDING_MODEL,
                    _vector_literal(row.embedding),
                    _content_hash(row.embedding_text),
                )
                upserted_count += 1
        return upserted_count

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _row_to_match(row: Any | None, *, match_type: str) -> PageNavAliasMatch | None:
    if row is None:
        return None
    return PageNavAliasMatch(
        canonical_code=str(row["canonical_code"]),
        page_subtype_code=(
            str(row["page_subtype_code"])
            if row["page_subtype_code"] is not None
            else None
        ),
        matched_term=str(row["label"]),
        source_category=str(row["source_category"] or ""),
        confidence=float(row["confidence"] or 0.0),
        match_type=match_type,
    )


def _vector_literal(embedding: tuple[float, ...]) -> str:
    values = [
        format(float(value), ".9g") if math.isfinite(float(value)) else "0"
        for value in embedding
    ]
    return "[" + ",".join(values) + "]"


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()