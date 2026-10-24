"""Web 活会话质检发布所需的主 Run PostgreSQL 读取。"""

from __future__ import annotations

from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWebLiveInspectionSourceRunReader:
    """按当前 Web V2 Session 与 epoch 定位唯一主探索 Run。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool

    async def find_source_agent_run_id(self, *, task_id: str) -> str | None:
        task = str(task_id or "").strip()
        if not task:
            raise ValueError("task_id 不能为空")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT run.id::text
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS run
                    ON run.task_id = task.id
                   AND run.agent_type = 'main'
                   AND run.status IN ('pending', 'running')
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = task.id
                   AND session.session_id = ('web:' || run.id::text)
                   AND session.surface_kind = 'web'
                   AND session.session_owner = 'web-v2-workflow'
                   AND session.status = 'active'
                   AND session.finalization_epoch = task.finalization_epoch
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'web'
                  AND task.status = 'running'
                  AND task.finalization_state IN ('open', 'exploration_draining')
                  AND task.task_config->>'workflow_version' = 'v2'
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
                  AND (
                        task.finalization_agent_run_id = run.id
                        OR (
                            task.finalization_agent_run_id IS NULL
                            AND NOT EXISTS (
                                SELECT 1
                                FROM qc_agent_run AS competing_run
                                WHERE competing_run.task_id = task.id
                                  AND competing_run.id <> run.id
                                  AND competing_run.agent_type = 'main'
                                  AND competing_run.status IN ('pending', 'running')
                            )
                        )
                  )
                ORDER BY run.started_at DESC NULLS LAST, run.created_at DESC
                LIMIT 1
                """,
                task,
            )
        return str(value) if value is not None else None


__all__ = ["PostgresWebLiveInspectionSourceRunReader"]
