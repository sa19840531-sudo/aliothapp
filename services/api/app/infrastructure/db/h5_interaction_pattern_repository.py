"""H5 自适应交互模式的候选、验证、晋升与隔离仓储。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from services.api.app.application.h5_adaptive_navigation import (
    H5AdaptiveNavigationPattern,
    H5InteractionPatternStatus,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

_VALID_STATUSES = frozenset({"candidate", "shadow", "trusted", "quarantined", "deprecated"})


class PostgresH5InteractionPatternRepository:
    """模式只保存可迁移结构，不保存或执行历史 CSS selector。"""

    def __init__(self, *, pool: Any | None = None, database_url: str | None = None) -> None:
        self._pool = pool
        self._database_url = database_url

    async def register_discovered(
        self,
        *,
        patterns: Sequence[H5AdaptiveNavigationPattern],
    ) -> Mapping[str, H5InteractionPatternStatus]:
        if not patterns:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            for pattern in patterns:
                _require_signature(pattern.signature)
                payload = _json_dumps(pattern.pattern_payload)
                row = await connection.fetchrow(
                    """
                    INSERT INTO qc_h5_interaction_pattern (
                        pattern_signature, pattern_kind, pattern_payload
                    )
                    VALUES ($1, $2, $3::jsonb)
                    ON CONFLICT (pattern_signature) DO UPDATE
                    SET last_seen_at = now(), updated_at = now()
                    WHERE qc_h5_interaction_pattern.pattern_payload = EXCLUDED.pattern_payload
                    RETURNING status
                    """,
                    pattern.signature,
                    pattern.pattern_kind,
                    payload,
                )
                if row is None:
                    raise ValueError("相同 H5 交互模式签名对应了不同结构载荷")
            rows = await connection.fetch(
                """
                SELECT pattern_signature, status
                FROM qc_h5_interaction_pattern
                WHERE pattern_signature = ANY($1::varchar[])
                """,
                [pattern.signature for pattern in patterns],
            )
        return {str(row["pattern_signature"]): _status(str(row["status"])) for row in rows}

    async def record_outcome(
        self,
        *,
        pattern_signature: str,
        task_id: str,
        page_capture_id: str,
        outcome: Literal["success", "failure"],
        reason_code: str | None = None,
    ) -> H5InteractionPatternStatus | None:
        _require_signature(pattern_signature)
        if outcome not in {"success", "failure"}:
            raise ValueError("H5 交互模式结果必须为 success 或 failure")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"h5-interaction-pattern:{pattern_signature}",
            )
            exists = await connection.fetchval(
                "SELECT 1 FROM qc_h5_interaction_pattern WHERE pattern_signature = $1",
                pattern_signature,
            )
            if exists is None:
                return None
            inserted = await connection.fetchval(
                """
                INSERT INTO qc_h5_interaction_pattern_observation (
                    pattern_signature, task_id, page_capture_id, outcome, reason_code
                )
                VALUES ($1, $2::uuid, $3::uuid, $4, $5)
                ON CONFLICT (pattern_signature, task_id, page_capture_id)
                DO NOTHING
                RETURNING 1
                """,
                pattern_signature,
                task_id,
                page_capture_id,
                outcome,
                _optional_reason(reason_code),
            )
            if inserted is None:
                current_status = await connection.fetchval(
                    """
                    SELECT status
                    FROM qc_h5_interaction_pattern
                    WHERE pattern_signature = $1
                    """,
                    pattern_signature,
                )
                return _status(str(current_status)) if current_status is not None else None
            counts = await connection.fetchrow(
                """
                SELECT
                    count(*) FILTER (WHERE outcome = 'success')::integer AS success_count,
                    count(*) FILTER (WHERE outcome = 'failure')::integer AS failure_count,
                    count(DISTINCT task_id) FILTER (WHERE outcome = 'success')::integer
                        AS distinct_success_task_count
                FROM qc_h5_interaction_pattern_observation
                WHERE pattern_signature = $1
                """,
                pattern_signature,
            )
            assert counts is not None
            success_count = int(counts["success_count"] or 0)
            failure_count = int(counts["failure_count"] or 0)
            distinct_task_count = int(counts["distinct_success_task_count"] or 0)
            status = _next_status(
                success_count=success_count,
                failure_count=failure_count,
                distinct_task_count=distinct_task_count,
            )
            await connection.execute(
                """
                UPDATE qc_h5_interaction_pattern
                SET status = $2,
                    success_count = $3,
                    failure_count = $4,
                    distinct_success_task_count = $5,
                    last_verified_at = CASE
                        WHEN $6 = 'success' THEN now() ELSE last_verified_at END,
                    last_failure_at = CASE
                        WHEN $6 = 'failure' THEN now() ELSE last_failure_at END,
                    updated_at = now()
                WHERE pattern_signature = $1
                """,
                pattern_signature,
                status,
                success_count,
                failure_count,
                distinct_task_count,
                outcome,
            )
        return _status(status)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _next_status(
    *,
    success_count: int,
    failure_count: int,
    distinct_task_count: int,
) -> str:
    if failure_count >= 2 and failure_count * 2 >= max(success_count, 1):
        return "quarantined"
    if distinct_task_count >= 2 and success_count >= 3 and failure_count <= 1:
        return "trusted"
    if success_count >= 1:
        return "shadow"
    return "candidate"


def _require_signature(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("H5 交互模式签名无效")
    return value


def _status(value: str) -> H5InteractionPatternStatus:
    if value not in _VALID_STATUSES:
        raise ValueError("数据库返回了未知 H5 交互模式状态")
    return value  # type: ignore[return-value]


def _optional_reason(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:128] if normalized else None


def _json_dumps(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["PostgresH5InteractionPatternRepository"]
