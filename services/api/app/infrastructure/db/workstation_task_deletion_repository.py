"""任务软删除仓储，保留报告、规则结果与原始证据。"""

import json
from typing import Any
from uuid import UUID

from services.api.app.application.workstation_task_deletion import DELETABLE_TASK_STATUSES
from services.api.app.application.workstation_tasks import (
    InvalidTaskOperationError,
    WorkstationTaskOperationResult,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWorkstationTaskDeletionRepository:
    """锁定任务后执行状态检查、软删除和审计，避免删除在途任务。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def delete_task(
        self, task_id: str, *, access_scope: AccessScope
    ) -> WorkstationTaskOperationResult | None:
        if not access_scope.can_write_any:
            return None
        try:
            UUID(task_id)
        except ValueError:
            return None
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            task = await connection.fetchrow(
                """
                SELECT id::text AS task_id, status
                FROM qc_task
                WHERE id = $1::uuid AND deleted_at IS NULL
                FOR UPDATE
                """,
                task_id,
            )
            if task is None:
                return None
            if task["status"] not in DELETABLE_TASK_STATUSES:
                raise InvalidTaskOperationError("排队、执行或取消中的任务不能删除")
            active_run = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM qc_agent_run
                    WHERE task_id = $1::uuid AND status IN ('pending', 'running')
                )
                """,
                task_id,
            )
            if active_run:
                raise InvalidTaskOperationError("任务仍有执行中的运行记录，暂不能删除")
            await connection.execute(
                """
                UPDATE qc_task
                SET deleted_at = now(), deleted_by = $2::uuid, updated_at = now()
                WHERE id = $1::uuid
                """,
                task_id,
                access_scope.actor_user_id,
            )
            await connection.execute(
                """
                INSERT INTO qc_task_log (task_id, level, source, message, context)
                VALUES ($1::uuid, 'INFO', 'management', '管理员删除任务', $2::jsonb)
                """,
                task_id,
                json.dumps(
                    {
                        "action": "task_delete",
                        "actor_user_id": access_scope.actor_user_id,
                        "task_status": task["status"],
                        "deletion_mode": "soft",
                    },
                    ensure_ascii=False,
                ),
            )
        return WorkstationTaskOperationResult(
            task_id=task_id,
            status=task["status"],
            run_id=None,
            message="任务已删除",
        )
