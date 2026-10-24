"""Web V2 正式报告生成 attempt 的 PostgreSQL 审计仓储。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.web_report_generation_audit import (
    WebReportGenerationAttempt,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class WebReportGenerationAttemptRejectedError(RuntimeError):
    """当前 Task/Run 已不允许启动新的 Web 正式报告生成。"""


class PostgresWebReportGenerationAttemptRepository:
    """用 Task 行锁串行分配 attempt 序号，并幂等提交终态。"""

    def __init__(self, *, database_url: str | None = None, pool: Any = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def start_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        agent_run_id: str,
    ) -> WebReportGenerationAttempt:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            task = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    task.finalization_epoch
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS run
                  ON run.id = $2::uuid
                 AND run.task_id = task.id
                 AND run.agent_type = 'main'
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'web'
                  AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND task.task_config->>'web_inspection_notification_mode'
                      = 'enforced'
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state = 'session_draining'
                  AND task.finalization_agent_run_id = run.id
                  AND task.execution_status IS NULL
                  AND task.quality_outcome IS NULL
                  AND run.status IN ('done', 'failed', 'aborted')
                  AND COALESCE(
                        (run.output_context->>'cancel_requested')::boolean,
                        FALSE
                      ) = FALSE
                  AND NOT (
                      COALESCE(run.output_context, '{}'::jsonb)
                      ? 'web_formal_terminalized'
                  )
                  AND run.id = (
                      SELECT current_run.id
                      FROM qc_agent_run AS current_run
                      WHERE current_run.task_id = task.id
                        AND current_run.agent_type = 'main'
                      ORDER BY current_run.created_at DESC, current_run.id DESC
                      LIMIT 1
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM qc_web_coverage_snapshot AS coverage
                      WHERE coverage.task_id = task.id
                        AND coverage.agent_run_id = run.id
                        AND coverage.finalization_epoch = task.finalization_epoch
                        AND coverage.coverage_status IN (
                            'complete', 'completed_with_gaps'
                        )
                  )
                FOR UPDATE OF task, run
                """,
                task_id,
                agent_run_id,
            )
            if task is None:
                raise WebReportGenerationAttemptRejectedError(
                    "Web 正式报告生成 attempt 当前不可启动"
                )
            finalization_epoch = int(task["finalization_epoch"])
            row = await connection.fetchrow(
                """
                WITH interrupted AS (
                    UPDATE qc_web_report_generation_attempt AS stale
                    SET status = 'failed',
                        failure_code = 'worker_interrupted',
                        finished_at = clock_timestamp()
                    WHERE stale.task_id = $2::uuid
                      AND stale.agent_run_id = $3::uuid
                      AND stale.finalization_epoch = $4
                      AND stale.status = 'running'
                      AND stale.id <> $1::uuid
                    RETURNING stale.id
                ),
                inserted AS (
                    INSERT INTO qc_web_report_generation_attempt AS existing (
                        id,
                        task_id,
                        agent_run_id,
                        finalization_epoch,
                        attempt_number,
                        status,
                        started_at
                    )
                    VALUES (
                        $1::uuid,
                        $2::uuid,
                        $3::uuid,
                        $4,
                        (
                            SELECT COALESCE(MAX(existing.attempt_number), 0) + 1
                            FROM qc_web_report_generation_attempt AS existing
                            WHERE existing.task_id = $2::uuid
                              AND existing.agent_run_id = $3::uuid
                              AND existing.finalization_epoch = $4
                        ),
                        'running',
                        clock_timestamp()
                    )
                    ON CONFLICT (id) DO NOTHING
                    RETURNING
                        id::text AS attempt_id,
                        task_id::text AS task_id,
                        agent_run_id::text AS agent_run_id,
                        finalization_epoch,
                        attempt_number
                )
                SELECT * FROM inserted
                UNION ALL
                SELECT
                    existing.id::text AS attempt_id,
                    existing.task_id::text AS task_id,
                    existing.agent_run_id::text AS agent_run_id,
                    existing.finalization_epoch,
                    existing.attempt_number
                FROM qc_web_report_generation_attempt AS existing
                WHERE existing.id = $1::uuid
                  AND existing.task_id = $2::uuid
                  AND existing.agent_run_id = $3::uuid
                  AND existing.finalization_epoch = $4
                LIMIT 1
                """,
                attempt_id,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
        if row is None:
            raise RuntimeError("Web 正式报告生成 attempt 未能持久化")
        return WebReportGenerationAttempt(
            attempt_id=str(row["attempt_id"]),
            task_id=str(row["task_id"]),
            agent_run_id=str(row["agent_run_id"]),
            finalization_epoch=int(row["finalization_epoch"]),
            attempt_number=int(row["attempt_number"]),
        )

    async def finish_attempt(
        self,
        *,
        attempt_id: str,
        attempt_number: int,
        status: str,
        failure_code: str | None,
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Web 正式报告生成 attempt 终态无效")
        normalized_failure_code = (
            None if status == "succeeded" else (failure_code or "unknown_failure")[:128]
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH updated AS (
                    UPDATE qc_web_report_generation_attempt
                    SET status = $3,
                        failure_code = $4,
                        finished_at = clock_timestamp()
                    WHERE id = $1::uuid
                      AND attempt_number = $2
                      AND status = 'running'
                    RETURNING id::text AS attempt_id
                )
                SELECT attempt_id FROM updated
                UNION ALL
                SELECT existing.id::text AS attempt_id
                FROM qc_web_report_generation_attempt AS existing
                WHERE existing.id = $1::uuid
                  AND existing.attempt_number = $2
                  AND existing.status = $3
                  AND existing.failure_code IS NOT DISTINCT FROM $4
                LIMIT 1
                """,
                attempt_id,
                attempt_number,
                status,
                normalized_failure_code,
            )
        if row is None:
            raise RuntimeError("Web 正式报告生成 attempt 终态冲突或记录不存在")

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
