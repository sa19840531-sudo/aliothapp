"""H5 冻结调用预算：跨进程原子预留，不改写用量流水或 Run 策略。"""

from datetime import datetime
from typing import Any

from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresH5LLMBudgetRepository:
    def __init__(self, *, pool: Any | None = None):
        self._pool = pool

    async def is_exhausted(
        self, *, task_id: str, agent_run_id: str, finalization_epoch: int,
    ) -> bool:
        """只读当前主运行冻结额度；身份缺失不等于额度耗尽。"""
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT (budget.admitted_calls >= budget.max_calls
                    OR budget.deadline <= now()) AS exhausted
                FROM qc_h5_llm_call_budget AS budget
                JOIN qc_task AS task ON task.id = budget.task_id
                WHERE task.id = $1::uuid AND task.target_platform = 'h5'
                    AND task.status IN ('running', 'review')
                    AND task.finalization_epoch = $3
                    AND budget.finalization_epoch = $3
                    AND budget.agent_run_id = $2::uuid""",
                task_id, agent_run_id, finalization_epoch,
            )
        if row is None:
            raise RuntimeError("H5 补采预算读取的 Task/Run/epoch 围栏不一致")
        return row["exhausted"] is True

    async def initialize(
        self, *, task_id: str, agent_run_id: str, max_calls: int, deadline: datetime
    ) -> None:
        if max_calls <= 0:
            raise ValueError("H5 冻结调用预算必须为正数")
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """SELECT task.finalization_epoch FROM qc_task AS task
                JOIN qc_agent_run AS run ON run.task_id = task.id AND run.id = $2::uuid
                WHERE task.id = $1::uuid AND task.target_platform = 'h5'
                FOR UPDATE OF task""",
                task_id,
                agent_run_id,
            )
            if row is None:
                raise RuntimeError("H5 冻结调用预算的 Task/Run 身份不一致")
            initialized = await connection.fetchval(
                """INSERT INTO qc_h5_llm_call_budget
                    (task_id, finalization_epoch, agent_run_id, max_calls, admitted_calls, deadline)
                SELECT $1::uuid, $3, $2::uuid, $4, COALESCE(sum(ai_calls), 0), $5
                FROM qc_llm_usage_event WHERE task_id = $1::uuid
                ON CONFLICT (task_id, finalization_epoch) DO UPDATE
                SET max_calls = LEAST(qc_h5_llm_call_budget.max_calls, EXCLUDED.max_calls),
                    deadline = LEAST(qc_h5_llm_call_budget.deadline, EXCLUDED.deadline),
                    admitted_calls = GREATEST(qc_h5_llm_call_budget.admitted_calls,
                                             EXCLUDED.admitted_calls)
                WHERE qc_h5_llm_call_budget.agent_run_id = EXCLUDED.agent_run_id
                RETURNING agent_run_id""",
                task_id,
                agent_run_id,
                row["finalization_epoch"],
                max_calls,
                deadline,
            )
            if initialized is None:
                raise RuntimeError("H5 冻结调用预算已绑定另一主 Run，不能替换或刷新")

    async def reserve(self, *, task_id: str, consumer_agent_run_id: str) -> None:
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            # 单条条件 UPDATE 是跨消费者原子准入；SDK 每次重试都必须重新预留。
            admitted = await connection.fetchval(
                """UPDATE qc_h5_llm_call_budget AS budget
                SET admitted_calls = budget.admitted_calls + 1, updated_at = now()
                FROM qc_task AS task, qc_agent_run AS consumer
                WHERE budget.task_id = $1::uuid AND task.id = budget.task_id
                    AND task.target_platform = 'h5' AND task.status IN ('running', 'review')
                    AND budget.finalization_epoch = task.finalization_epoch
                    AND consumer.id = $2::uuid AND consumer.task_id = task.id
                    AND budget.deadline > now() AND budget.admitted_calls < budget.max_calls
                RETURNING budget.admitted_calls""",
                task_id,
                consumer_agent_run_id,
            )
        if admitted is None:
            raise H5LLMBudgetExhausted(
                "H5 模型请求未获冻结预算准入：调用已耗尽、截止或身份围栏不匹配"
            )
