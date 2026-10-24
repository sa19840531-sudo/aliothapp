"""Web V2 遗留 evidence_pending 动作的 PostgreSQL 收敛仓储。"""

from __future__ import annotations

from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool

WEB_EXPLORATION_ABANDONED_EVIDENCE = "WEB_EXPLORATION_ABANDONED_EVIDENCE"


class PostgresWebExplorationSessionCleanupRepository:
    """以严格 Session 身份门限终结 Web V2 遗留证据动作。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def fail_abandoned_evidence_pending(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> int:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id 不能为空")
        if not isinstance(session_id, str) or not session_id.startswith("web:"):
            raise ValueError("session_id 必须以 web: 开头")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            count = await connection.fetchval(
                """
                WITH eligible_session AS (
                    SELECT session.task_id, session.session_id
                    FROM qc_surface_session AS session
                    WHERE session.task_id = $1::uuid
                      AND session.session_id = $2
                      AND session.session_id LIKE 'web:%'
                      AND session.surface_kind = 'web'
                      AND session.session_owner = 'web-v2-workflow'
                      AND session.status = 'draining'
                      AND session.lease_token IS NULL
                    FOR UPDATE
                ), failed AS (
                    UPDATE qc_action_execution AS action
                    SET
                        status = 'evidence_failed',
                        error_code = $3::varchar(128),
                        error_message = $4::text,
                        evidence_finished_at = now(),
                        row_version = action.row_version + 1,
                        result_payload = action.result_payload || jsonb_build_object(
                            'web_exploration_cleanup', true,
                            'cleanup_error_code', ($3::varchar(128))::text
                        ),
                        updated_at = now()
                    FROM eligible_session AS session
                    WHERE action.task_id = session.task_id
                      AND action.session_id = session.session_id
                      AND action.status = 'evidence_pending'
                    RETURNING action.action_execution_id
                )
                SELECT count(*)::integer FROM failed
                """,
                task_id.strip(),
                session_id,
                WEB_EXPLORATION_ABANDONED_EVIDENCE,
                "Web V2 会话排空时动作后证据未能完成，已保守终结",
            )
        return int(count or 0)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


__all__ = [
    "PostgresWebExplorationSessionCleanupRepository",
    "WEB_EXPLORATION_ABANDONED_EVIDENCE",
]
