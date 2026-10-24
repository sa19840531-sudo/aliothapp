"""从 PostgreSQL Submission Fact 恢复初始 Agent Run。"""

from __future__ import annotations

from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord
from services.api.app.infrastructure.queue.state_store import agent_run_from_postgres_row


class PostgresTaskSubmissionDeliveryRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def load_pending_run(
        self,
        *,
        submission_id: str,
        task_id: str,
    ) -> AgentRunRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _PENDING_RUN_SELECT
                + """
                WHERE submission.id = $1::uuid
                  AND submission.task_id = $2::uuid
                  AND run.status = 'pending'
                  AND task.status = 'queued'
                """,
                submission_id,
                task_id,
            )
        return agent_run_from_postgres_row(row) if row is not None else None

    async def list_pending_runs(
        self,
        *,
        minimum_age_seconds: int,
        limit: int,
    ) -> tuple[AgentRunRecord, ...]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _PENDING_RUN_SELECT
                + """
                WHERE run.status = 'pending'
                  AND task.status = 'queued'
                  AND submission.committed_at <=
                      now() - make_interval(secs => $1)
                ORDER BY submission.committed_at, submission.id
                LIMIT $2
                """,
                minimum_age_seconds,
                limit,
            )
        return tuple(agent_run_from_postgres_row(row) for row in rows)

    async def _get_pool(self) -> Any:
        return self._pool or await get_postgres_pool(self._database_url)


_PENDING_RUN_SELECT = """
    SELECT
        run.id::text AS run_id,
        run.task_id::text AS task_id,
        run.status,
        run.input_context,
        run.output_context,
        run.working_memory,
        run.loop_guard,
        run.stop_reason,
        run.error_message,
        run.total_steps,
        run.started_at,
        run.finished_at,
        run.created_at
    FROM qc_task_submission AS submission
    INNER JOIN qc_task AS task ON task.id = submission.task_id
    INNER JOIN qc_agent_run AS run
        ON run.task_id = submission.task_id
       AND run.id = submission.initial_agent_run_id
"""
