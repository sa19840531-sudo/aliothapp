"""Main Run 已终止、会话已闭合后仍在收口的 H5 根任务取消。"""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.exploration_work_queue import ExplorerWorkType
from services.api.app.application.workstation_tasks import WorkstationTaskOperationResult
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task.status import TaskStatus
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorerWorkRepository,
)
from services.api.app.infrastructure.db.h5_cancellation_settlement_repository import (
    PostgresH5CancellationSettlementRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.task_status_repository import (
    transition_current_task_status_in_transaction,
)


class _TransactionPool:
    """让已有清理仓储复用本次取消事务，任何一步失败整体回滚。"""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


class PostgresWorkstationTaskCancellationRepository:
    """取消根任务并保留 Main Run 原终态；不重启或改写 Agent 执行历史。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def cancel_finished_run_task(
        self,
        *,
        task_id: str,
        run_id: str,
        access_scope: AccessScope,
    ) -> WorkstationTaskOperationResult | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            # 与 Completion Gate、Run 结束和 H5 工作结算共用线性化锁。
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            task = await connection.fetchrow(
                """
                SELECT task.status, task.finalization_state, task.finalization_epoch,
                       task.finalization_agent_run_id::text AS owner_run_id
                FROM qc_task AS task
                WHERE task.id = $1::uuid
                  AND task.deleted_at IS NULL
                  AND task.target_platform = 'h5'
                  AND (
                    $2::boolean
                    OR (task.tenant_id = $3::uuid AND task.submitter_id = $4::uuid)
                  )
                FOR UPDATE
                """,
                task_id,
                access_scope.can_write_any,
                access_scope.tenant_id,
                access_scope.actor_user_id,
            )
            if task is None:
                return None
            if task["status"] == "canceled":
                return WorkstationTaskOperationResult(task_id, "canceled", run_id, "任务已取消")
            if (
                task["status"] not in {"queued", "running"}
                or task["finalization_state"] == "sealed"
                or task["owner_run_id"] != run_id
            ):
                return None
            run = await connection.fetchrow(
                """
                SELECT status
                FROM qc_agent_run
                WHERE id = $1::uuid AND task_id = $2::uuid AND agent_type = 'main'
                FOR UPDATE
                """,
                run_id,
                task_id,
            )
            if run is None or run["status"] not in {"done", "failed", "aborted", "canceled"}:
                return None
            unavailable = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM qc_surface_session
                    WHERE task_id = $1::uuid AND status <> 'closed'
                ) OR EXISTS (
                    SELECT 1 FROM qc_agent_run
                    WHERE task_id = $1::uuid AND agent_type = 'main'
                      AND id <> $2::uuid
                      AND (status IN ('pending', 'running') OR created_at > (
                          SELECT created_at FROM qc_agent_run WHERE id = $2::uuid
                      ))
                )
                """,
                task_id,
                run_id,
            )
            # 活动会话或新 Main Run 必须继续走原协作式取消，不能由管理入口抢终态。
            if unavailable:
                return None
            now = datetime.now(UTC)
            snapshot = await transition_current_task_status_in_transaction(
                connection,
                task_id=task_id,
                target_status=TaskStatus.CANCELED,
                allowed_source_statuses=(TaskStatus.QUEUED, TaskStatus.RUNNING),
                occurred_at=now,
            )
            if snapshot is None:
                return None
            shared_pool = _TransactionPool(connection)
            reason = "用户取消 Main Run 已结束且会话闭合的 H5 收口任务"
            await PostgresH5CancellationSettlementRepository(pool=shared_pool).settle_canceled(
                task_id=task_id, reason="H5_TASK_CANCELED"
            )
            await PostgresExplorerWorkRepository(pool=shared_pool).cancel_nonterminal(
                task_id=task_id,
                reason=reason,
                work_types=(ExplorerWorkType.EXPLORATION, ExplorerWorkType.PROBE),
                h5_only=True,
            )
            await PostgresActionExecutionRepository(pool=shared_pool).fail_nonterminal_for_task(
                task_id=task_id,
                error_code="WORKFLOW_CANCELED",
                error_message=reason,
                h5_only=True,
            )
            await PostgresInspectionWorkItemRepository(pool=shared_pool).cancel_nonterminal(
                task_id=task_id, reason=reason
            )
            # 取消的是根任务；原 Main 的失败/完成原因与时间保持不变。完成标志防止
            # Worker 把已结束的 Main 当作未清理取消 Run，反复覆盖历史执行事实。
            await connection.execute(
                """
                UPDATE qc_agent_run
                SET output_context = COALESCE(output_context, '{}'::jsonb) || $3::jsonb,
                    updated_at = now()
                WHERE id = $1::uuid AND task_id = $2::uuid
                """,
                run_id,
                task_id,
                json.dumps(
                    {
                        "cancel_requested": True,
                        "cancellation_cleanup_completed": True,
                        "cancellation_finalized": True,
                        "task_cancellation": {
                            "actor_user_id": access_scope.actor_user_id,
                            "requested_at": now.isoformat(),
                            "finalization_epoch": int(task["finalization_epoch"]),
                            "run_status": str(run["status"]),
                            "reason": "terminal_main_closed_session",
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        return WorkstationTaskOperationResult(
            task_id, "canceled", run_id, "任务已取消，已结束的运行记录保持原结论"
        )
