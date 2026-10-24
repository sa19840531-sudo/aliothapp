"""Agent Run 长期状态写入接口。

Redis 队列只负责短期分发、运行中查询和取消信号；本模块定义长期状态写入边界。
默认使用 no-op 实现；当 ``ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres`` 时，
使用 PostgreSQL 实现写入 ``qc_agent_run`` / ``qc_agent_action_trace``，不改变 API
与 Worker 主流程。
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from services.api.app.agents.runtime.models import is_review_termination_reason
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.task.status import TaskStatus, TaskStatusConflictError
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.rule_confirmation_sql import (
    requires_human_confirmation_sql,
)
from services.api.app.infrastructure.db.rule_result_scope_sql import superseded_placeholder_sql
from services.api.app.infrastructure.db.task_status_repository import (
    transition_current_task_status_in_transaction,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunQueueStatus, AgentRunRecord


@dataclass(frozen=True)
class AgentRunStateEvent:
    """内存态 Store 记录的一次生命周期事件，供单元测试验证调用顺序。"""

    event_type: str
    run_id: str
    snapshot: AgentRunRecord | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AgentRunCancellationConflict(RuntimeError):
    """持久化取消标记已赢得终态竞争，完成写入不得继续。"""


class AgentRunTerminalStateConflict(RuntimeError):
    """Run 已进入其他终态，迟到写入不得反向覆盖。"""


class AgentRunStateStore(Protocol):
    """Agent Run 长期状态写入契约。"""

    @property
    def durable_cancellation_enabled(self) -> bool:
        """当前实现是否能把取消接受点持久化为长期事实。"""

    async def record_submitted(self, record: AgentRunRecord) -> None:
        """记录 Run 已提交。"""

    async def record_started(self, record: AgentRunRecord) -> None:
        """记录 Worker 已开始处理 Run。"""

    async def record_finished(self, record: AgentRunRecord) -> None:
        """记录 Run 已进入完成、失败或取消终态。"""

    async def record_run_finished(self, record: AgentRunRecord) -> None:
        """只记录 Run 终态，不提前改写绑定任务终态。"""

    async def list_finished_records(
        self,
        *,
        task_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        """从长期状态存储恢复已结束 Run，供 Completion Gate 对账。"""

    async def list_legacy_web_recovery_records(self, *, limit: int = 100) -> list[AgentRunRecord]:
        """恢复尚未收口的历史 Web Run，不依赖 Redis TTL。"""

    async def list_cancellation_recovery_records(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        """恢复取消已接受但清理或长期终态尚未闭合的 Run。"""

    async def get_finished_record(self, *, run_id: str) -> AgentRunRecord | None:
        """按原 Run 身份读取终态事实，供 Redis 孤儿投影对账。"""

    async def get_record_snapshot(self, *, run_id: str) -> AgentRunRecord | None:
        """按原 Run 身份读取展示用快照，包含动作留痕。"""

    async def record_cancel_requested(self, run_id: str) -> bool:
        """记录用户已请求取消 Run。"""

    async def is_cancel_requested(self, run_id: str) -> bool:
        """读取已线性化的 Run 取消事实。"""


class NoopAgentRunStateStore:
    """默认长期状态 Store：未配置长期存储时不做任何写入。"""

    @property
    def durable_cancellation_enabled(self) -> bool:
        return False

    async def record_submitted(self, record: AgentRunRecord) -> None:
        pass

    async def record_started(self, record: AgentRunRecord) -> None:
        pass

    async def record_finished(self, record: AgentRunRecord) -> None:
        pass

    async def record_run_finished(self, record: AgentRunRecord) -> None:
        pass

    async def list_finished_records(
        self,
        *,
        task_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        del task_ids, limit
        return []

    async def list_legacy_web_recovery_records(self, *, limit: int = 100) -> list[AgentRunRecord]:
        return []

    async def list_cancellation_recovery_records(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        del limit
        return []

    async def get_finished_record(self, *, run_id: str) -> AgentRunRecord | None:
        del run_id
        return None

    async def get_record_snapshot(self, *, run_id: str) -> AgentRunRecord | None:
        del run_id
        return None

    async def record_cancel_requested(self, run_id: str) -> bool:
        del run_id
        return False

    async def is_cancel_requested(self, run_id: str) -> bool:
        del run_id
        return False


class InMemoryAgentRunStateStore:
    """测试用长期状态 Store：保存事件快照，不参与生产运行。"""

    def __init__(self) -> None:
        self.events: list[AgentRunStateEvent] = []

    @property
    def durable_cancellation_enabled(self) -> bool:
        return True

    async def record_submitted(self, record: AgentRunRecord) -> None:
        self._append("submitted", record)

    async def record_started(self, record: AgentRunRecord) -> None:
        self._append("started", record)

    async def record_finished(self, record: AgentRunRecord) -> None:
        self._append("finished", record)

    async def record_run_finished(self, record: AgentRunRecord) -> None:
        self._append("finished", record)

    async def list_finished_records(
        self,
        *,
        task_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        allowed = set(task_ids) if task_ids is not None else None
        records: list[AgentRunRecord] = []
        seen: set[str] = set()
        for event in reversed(self.events):
            record = event.snapshot
            if event.event_type != "finished" or record is None or record.run_id in seen:
                continue
            if allowed is not None and record.task_id not in allowed:
                continue
            seen.add(record.run_id)
            records.append(record.model_copy(deep=True))
            if len(records) >= limit:
                break
        return records

    async def list_legacy_web_recovery_records(self, *, limit: int = 100) -> list[AgentRunRecord]:
        from services.api.app.domain.exploration.web_runtime_contract import is_legacy_web_execution

        latest = {
            event.run_id: event.snapshot for event in self.events if event.snapshot is not None
        }
        return [
            record.model_copy(deep=True)
            for record in latest.values()
            if record.status in {AgentRunQueueStatus.QUEUED, AgentRunQueueStatus.RUNNING}
            and is_legacy_web_execution(record.task_config)
        ][:max(0, limit)]

    async def list_cancellation_recovery_records(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        canceled_ids = {
            event.run_id for event in self.events if event.event_type == "cancel_requested"
        }
        records: list[AgentRunRecord] = []
        seen: set[str] = set()
        for event in reversed(self.events):
            record = event.snapshot
            if record is None or record.run_id in seen or record.run_id not in canceled_ids:
                continue
            seen.add(record.run_id)
            if record.cancellation_cleanup_completed and record.cancellation_finalized:
                continue
            recovered = record.model_copy(deep=True)
            recovered.status = AgentRunQueueStatus.CANCELED
            recovered.termination_reason = "canceled"
            records.append(recovered)
            if len(records) >= limit:
                break
        return records

    async def get_finished_record(self, *, run_id: str) -> AgentRunRecord | None:
        for record in await self.list_finished_records(limit=max(1, len(self.events))):
            if record.run_id == run_id:
                return record
        return None

    async def get_record_snapshot(self, *, run_id: str) -> AgentRunRecord | None:
        for event in reversed(self.events):
            record = event.snapshot
            if record is not None and record.run_id == run_id:
                return record.model_copy(deep=True)
        return None

    async def record_cancel_requested(self, run_id: str) -> bool:
        self.events.append(AgentRunStateEvent(event_type="cancel_requested", run_id=run_id))
        return True

    async def is_cancel_requested(self, run_id: str) -> bool:
        return any(
            event.event_type == "cancel_requested" and event.run_id == run_id
            for event in self.events
        )

    def _append(self, event_type: str, record: AgentRunRecord) -> None:
        self.events.append(
            AgentRunStateEvent(
                event_type=event_type,
                run_id=record.run_id,
                snapshot=record.model_copy(deep=True),
            )
        )


class PostgresAgentRunStateStore:
    """PostgreSQL 长期状态 Store，写入 ``qc_agent_run`` 与 ``qc_agent_action_trace``。

    只有绑定 ``task_id`` 的 Agent Run 才会写入长期审计表；低层调试 Run 没有业务任务，
    不能伪造 ``qc_task``。
    """

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    @property
    def durable_cancellation_enabled(self) -> bool:
        return True

    async def record_submitted(self, record: AgentRunRecord) -> None:
        if record.task_id is None:
            return
        await self._execute(
            """
            INSERT INTO qc_agent_run (
                id, task_id, agent_type, agent_name, status, input_context, loop_guard,
                started_at, created_at, updated_at
            )
            VALUES (
                $1::uuid, $2::uuid, $3::varchar, $4::varchar, $5::varchar,
                $6::jsonb, $7::jsonb, NULL, $8::timestamptz, $8::timestamptz
            )
            ON CONFLICT (id) DO UPDATE SET
                -- 空闲 Worker 可能在提交写库前就完成 record_started（本地毫秒级抢队），
                -- 提交事件冲突时禁止把已推进的运行状态踩回 pending。
                input_context = EXCLUDED.input_context,
                loop_guard = EXCLUDED.loop_guard,
                updated_at = now()
            """,
            record.run_id,
            record.task_id,
            "main",
            "Main Agent",
            "pending",
            _json_dumps(_input_context(record)),
            _json_dumps(record.loop_guard),
            record.submitted_at,
        )

    async def record_started(self, record: AgentRunRecord) -> None:
        if record.task_id is None:
            return
        started_at = record.started_at or datetime.now(UTC)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            persisted = await connection.fetchval(
                """
                INSERT INTO qc_agent_run (
                    id, task_id, agent_type, agent_name, status, input_context, loop_guard,
                    started_at, created_at, updated_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::varchar, $4::varchar, $5::varchar,
                    $6::jsonb, $7::jsonb, $8::timestamptz, $9::timestamptz, now()
                )
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    started_at = COALESCE(qc_agent_run.started_at, EXCLUDED.started_at),
                    input_context = EXCLUDED.input_context,
                    loop_guard = EXCLUDED.loop_guard,
                    updated_at = now()
                WHERE qc_agent_run.status IN ('pending', 'running')
                  AND qc_agent_run.task_id = EXCLUDED.task_id
                RETURNING true
                """,
                record.run_id,
                record.task_id,
                "main",
                "Main Agent",
                "running",
                _json_dumps(_input_context(record)),
                _json_dumps(record.loop_guard),
                started_at,
                record.submitted_at,
            )
            if not persisted:
                raise TaskStatusConflictError("Agent Run 已终结或任务归属不一致，禁止状态复活")

            updated = await transition_current_task_status_in_transaction(
                connection,
                task_id=record.task_id,
                target_status=TaskStatus.RUNNING,
                allowed_source_statuses=(TaskStatus.QUEUED, TaskStatus.RUNNING),
                occurred_at=started_at,
                mark_locked=True,
            )
            if updated is None:
                raise TaskStatusConflictError("任务不存在、已收口或不允许启动 Agent Run")

    async def record_finished(self, record: AgentRunRecord) -> None:
        if record.task_id is None:
            return
        finished_at = record.finished_at or datetime.now(UTC)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, record.task_id)
            result = await connection.execute(
                """
                UPDATE qc_agent_run
                SET
                    status = $2::varchar,
                    output_context = CASE
                        WHEN COALESCE(output_context, '{}'::jsonb)
                             ? 'web_formal_terminalized'
                        THEN jsonb_set(
                            COALESCE($3::jsonb, '{}'::jsonb),
                            '{web_formal_terminalized}',
                            output_context->'web_formal_terminalized',
                            TRUE
                        )
                        ELSE $3::jsonb
                    END,
                    working_memory = $4::jsonb,
                    stop_reason = $5::varchar,
                    error_message = $6::text,
                    total_steps = $7::integer,
                    total_ai_calls = $8::integer,
                    total_tokens = $9::integer,
                    total_cost = $10::numeric,
                    finished_at = $11::timestamptz,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND (status IN ('pending', 'running') OR status = $2::varchar)
                  AND (
                      (
                          $13::boolean
                          AND (
                              status = 'canceled'
                              OR COALESCE(
                                  (output_context->>'cancel_requested')::boolean,
                                  FALSE
                              ) = TRUE
                              OR EXISTS (
                                  SELECT 1
                                  FROM qc_task AS task
                                  WHERE task.id = $12::uuid
                                    AND task.status = 'canceled'
                              )
                          )
                      )
                      OR (
                          NOT $13::boolean
                          AND status <> 'canceled'
                          AND COALESCE(
                              (output_context->>'cancel_requested')::boolean,
                              FALSE
                          ) = FALSE
                          AND EXISTS (
                              SELECT 1
                              FROM qc_task AS task
                              WHERE task.id = $12::uuid
                                AND task.status <> 'canceled'
                          )
                      )
                  )
                """,
                record.run_id,
                _agent_run_status(record.status),
                _json_dumps(_output_context(record)),
                _json_dumps(_working_memory(record)),
                record.termination_reason,
                record.error_message,
                record.step_count or 0,
                _total_ai_calls(record),
                _total_tokens(record),
                _total_cost(record),
                finished_at,
                record.task_id,
                record.status is AgentRunQueueStatus.CANCELED,
            )
            await _raise_if_cancel_won(connection, record, result)
            target_status = TaskStatus(
                _task_status(
                    record.status,
                    record.termination_reason,
                    record.execution_outcome,
                )
            )
            if target_status is TaskStatus.DONE and await _requires_task_review(
                connection,
                task_id=record.task_id,
            ):
                target_status = TaskStatus.REVIEW
            updated = await transition_current_task_status_in_transaction(
                connection,
                task_id=record.task_id,
                target_status=target_status,
                allowed_source_statuses=_task_finish_source_statuses(target_status),
                occurred_at=finished_at,
                last_error=record.error_message,
                update_last_error=True,
            )
            if updated is None:
                raise TaskStatusConflictError("任务不存在、已收口或结束状态已被并发推进")
        for entry in record.action_trace:
            await self.insert_action_trace_entry(
                run_id=record.run_id,
                task_id=record.task_id,
                entry=entry,
            )

    async def record_run_finished(self, record: AgentRunRecord) -> None:
        """持久化 Run 与动作轨迹，任务终态由 Completion Gate 独立封存。"""
        if record.task_id is None:
            return
        finished_at = record.finished_at or datetime.now(UTC)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, record.task_id)
            result = await connection.execute(
                """
                UPDATE qc_agent_run
                SET
                    status = $2::varchar,
                    output_context = CASE
                        WHEN COALESCE(output_context, '{}'::jsonb)
                             ? 'web_formal_terminalized'
                        THEN jsonb_set(
                            COALESCE($3::jsonb, '{}'::jsonb),
                            '{web_formal_terminalized}',
                            output_context->'web_formal_terminalized',
                            TRUE
                        )
                        ELSE $3::jsonb
                    END,
                    working_memory = $4::jsonb,
                    stop_reason = $5::varchar,
                    error_message = $6::text,
                    total_steps = $7::integer,
                    total_ai_calls = $8::integer,
                    total_tokens = $9::integer,
                    total_cost = $10::numeric,
                    finished_at = $11::timestamptz,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND task_id = $12::uuid
                  AND (status IN ('pending', 'running') OR status = $2::varchar)
                  AND (
                      (
                          $13::boolean
                          AND (
                              status = 'canceled'
                              OR COALESCE(
                                  (output_context->>'cancel_requested')::boolean,
                                  FALSE
                              ) = TRUE
                              OR EXISTS (
                                  SELECT 1
                                  FROM qc_task AS task
                                  WHERE task.id = $12::uuid
                                    AND task.status = 'canceled'
                              )
                          )
                      )
                      OR (
                          NOT $13::boolean
                          AND status <> 'canceled'
                          AND COALESCE(
                              (output_context->>'cancel_requested')::boolean,
                              FALSE
                          ) = FALSE
                          AND EXISTS (
                              SELECT 1
                              FROM qc_task AS task
                              WHERE task.id = $12::uuid
                                AND task.status <> 'canceled'
                          )
                      )
                  )
                """,
                record.run_id,
                _agent_run_status(record.status),
                _json_dumps(_output_context(record)),
                _json_dumps(_working_memory(record)),
                record.termination_reason,
                record.error_message,
                record.step_count or 0,
                _total_ai_calls(record),
                _total_tokens(record),
                _total_cost(record),
                finished_at,
                record.task_id,
                record.status is AgentRunQueueStatus.CANCELED,
            )
            await _raise_if_cancel_won(connection, record, result)
            await connection.execute(
                """
                UPDATE qc_task AS task
                SET
                    finalization_agent_run_id = $2::uuid,
                    updated_at = now()
                WHERE task.id = $1::uuid
                  AND task.status = 'running'
                  AND task.finalization_state = 'open'
                  AND task.finalization_agent_run_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS active_run
                      WHERE active_run.task_id = task.id
                        AND active_run.status IN ('pending', 'running')
                        -- enforced 通知链的持久化逻辑消费 Run 在主 Run 收场后仍
                        -- 合法 running（判定收尾由门禁把关），不得阻塞主 Run 认领
                        -- 收口所有权，否则收口快照 Run 恒为 NULL、原子收口必失败；
                        -- 仅豁免显式允许的 H5/App 消费源，且必须同一 finalization_epoch，
                        -- 陈旧 epoch、未知 source 或任意其他 rule_judge 都保守阻塞。
                        AND NOT (
                            active_run.agent_type = 'rule_judge'
                            AND active_run.input_context->>'source' IN (
                                'h5_inspection_publication',
                                'app_inspection_publication'
                            )
                            AND active_run.input_context->>'finalization_epoch'
                                = task.finalization_epoch::text
                        )
                  )
                """,
                record.task_id,
                record.run_id,
            )
        for entry in record.action_trace:
            await self.insert_action_trace_entry(
                run_id=record.run_id,
                task_id=record.task_id,
                entry=entry,
            )

    async def list_finished_records(
        self,
        *,
        task_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        """从 qc_agent_run 恢复报告和工作记忆，不依赖 Redis TTL。"""
        if limit <= 0:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    id::text AS run_id,
                    task_id::text AS task_id,
                    status,
                    input_context,
                    output_context,
                    working_memory,
                    loop_guard,
                    stop_reason,
                    error_message,
                    total_steps,
                    started_at,
                    finished_at,
                    created_at
                FROM qc_agent_run
                WHERE status IN ('done', 'failed', 'aborted', 'canceled')
                  AND status <> 'canceled'
                  AND COALESCE(
                      (output_context->>'cancel_requested')::boolean,
                      FALSE
                  ) = FALSE
                  AND ($1::uuid[] IS NULL OR task_id = ANY($1::uuid[]))
                ORDER BY finished_at DESC NULLS LAST, created_at DESC, id DESC
                LIMIT $2
                """,
                task_ids,
                limit,
            )
        return [_agent_run_from_postgres_row(row) for row in rows]

    async def list_legacy_web_recovery_records(self, *, limit: int = 100) -> list[AgentRunRecord]:
        """扫描长期非终态旧运行，失败后保留原记录供下一轮重试。"""
        from services.api.app.domain.exploration.web_runtime_contract import (
            CURRENT_WEB_RUNTIME_VERSION,
        )

        if limit <= 0:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id::text AS run_id, task_id::text AS task_id, status,
                       input_context, output_context, working_memory, loop_guard,
                       stop_reason, error_message, total_steps, started_at, finished_at, created_at
                FROM qc_agent_run
                WHERE status IN ('pending', 'running')
                  AND input_context->'task_config'->'_task_target'->>'platform' = 'web'
                  AND input_context->'task_config'->>'web_runtime_version' IS DISTINCT FROM $1
                  AND COALESCE((output_context->>'cancel_requested')::boolean, FALSE) = FALSE
                ORDER BY updated_at, created_at, id
                LIMIT $2
                """,
                CURRENT_WEB_RUNTIME_VERSION, limit,
            )
        return [_agent_run_from_postgres_row(row) for row in rows]

    async def list_cancellation_recovery_records(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentRunRecord]:
        if limit <= 0:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    id::text AS run_id,
                    task_id::text AS task_id,
                    status,
                    input_context,
                    output_context,
                    working_memory,
                    loop_guard,
                    stop_reason,
                    error_message,
                    total_steps,
                    started_at,
                    finished_at,
                    created_at
                FROM qc_agent_run
                WHERE (
                    status = 'canceled'
                    OR COALESCE(
                        (output_context->>'cancel_requested')::boolean,
                        FALSE
                    ) = TRUE
                )
                  AND (
                    COALESCE(
                        (output_context->>'cancellation_cleanup_completed')::boolean,
                        FALSE
                    ) = FALSE
                    OR COALESCE(
                        (output_context->>'cancellation_finalized')::boolean,
                        FALSE
                    ) = FALSE
                  )
                ORDER BY updated_at, created_at, id
                LIMIT $1
                """,
                limit,
            )
        return [_agent_run_from_postgres_row(row) for row in rows]

    async def get_finished_record(self, *, run_id: str) -> AgentRunRecord | None:
        """按 UUID 精确读取终态 Run；非终态或不存在时返回 None。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    id::text AS run_id,
                    task_id::text AS task_id,
                    status,
                    input_context,
                    output_context,
                    working_memory,
                    loop_guard,
                    stop_reason,
                    error_message,
                    total_steps,
                    started_at,
                    finished_at,
                    created_at
                FROM qc_agent_run
                WHERE id = $1::uuid
                  AND status IN ('done', 'failed', 'aborted', 'canceled')
                """,
                run_id,
            )
            if row is None:
                return None
            trace_rows = await _fetch_action_trace_rows(connection, run_id=run_id)
        record = _agent_run_from_postgres_row(row)
        record.action_trace = _action_trace_entries_from_postgres_rows(trace_rows)
        return record

    async def get_record_snapshot(self, *, run_id: str) -> AgentRunRecord | None:
        """读取 API 展示用 Run 快照；保持 pending/running 状态，不参与恢复重放。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    id::text AS run_id,
                    task_id::text AS task_id,
                    status,
                    input_context,
                    output_context,
                    working_memory,
                    loop_guard,
                    stop_reason,
                    error_message,
                    total_steps,
                    started_at,
                    finished_at,
                    created_at
                FROM qc_agent_run
                WHERE id = $1::uuid
                """,
                run_id,
            )
            if row is None:
                return None
            trace_rows = await _fetch_action_trace_rows(connection, run_id=run_id)
        record = _agent_run_snapshot_from_postgres_row(row)
        record.action_trace = _action_trace_entries_from_postgres_rows(trace_rows)
        return record

    async def record_cancel_requested(self, run_id: str) -> bool:
        """以任务收口锁写入取消标记，作为取消与 Completion Gate 的线性化点。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            task_id = await connection.fetchval(
                "SELECT task_id::text FROM qc_agent_run WHERE id = $1::uuid",
                run_id,
            )
            if not isinstance(task_id, str):
                return False
            await _lock_task_finalization(connection, task_id)
            result = await connection.execute(
                """
                UPDATE qc_agent_run
                SET
                    output_context = output_context || $2::jsonb,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND status NOT IN ('done', 'failed', 'aborted', 'canceled')
                """,
                run_id,
                _json_dumps({"cancel_requested": True}),
            )
        return not str(result).endswith(" 0")

    async def is_cancel_requested(self, run_id: str) -> bool:
        """读取 PostgreSQL 中已经接受的取消标记，不依赖 Redis TTL。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            requested = await connection.fetchval(
                """
                SELECT (
                    status = 'canceled'
                    OR COALESCE(
                        (output_context->>'cancel_requested')::boolean,
                        FALSE
                    )
                )
                FROM qc_agent_run
                WHERE id = $1::uuid
                """,
                run_id,
            )
        return requested is True

    async def is_h5_cancel_requested(self, run_id: str) -> bool:
        """H5 成功读取的取消事实不因连接归还超时被抹掉。"""
        from services.api.app.infrastructure.queue.h5_cancellation_check import (
            read_h5_cancel_requested,
        )

        return await read_h5_cancel_requested(pool=await self._get_pool(), run_id=run_id)

    async def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()

    async def insert_action_trace_entry(self, *, run_id: str, task_id: str, entry: dict) -> None:
        """写入/更新一条动作留痕；运行中逐步调用可实现轨迹流式落库，幂等可重放。"""
        result_payload = _result_payload(entry.get("result"))
        await self._execute(
            """
            WITH trace_row AS (
                INSERT INTO qc_agent_action_trace (
                    agent_run_id, task_id, step_seq, phase, selected_capability,
                    capability_kind, capability_input, capability_output, latency_ms,
                    status, error_code, error_message
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::integer, $4::varchar, $5::varchar,
                    $6::varchar, $7::jsonb, $8::jsonb,
                    $9::integer, $10::varchar, $11::varchar, $12::text
                )
                ON CONFLICT (agent_run_id, step_seq) DO UPDATE SET
                    selected_capability = EXCLUDED.selected_capability,
                    capability_input = EXCLUDED.capability_input,
                    capability_output = EXCLUDED.capability_output,
                    latency_ms = EXCLUDED.latency_ms,
                    status = EXCLUDED.status,
                    error_code = EXCLUDED.error_code,
                    error_message = EXCLUDED.error_message
                RETURNING id
            )
            UPDATE qc_rule_execution AS execution
            SET source_action_trace_id = trace_row.id,
                updated_at = now()
            FROM trace_row
            WHERE execution.id = ANY($13::uuid[])
              AND execution.task_id = $2::uuid
              AND execution.agent_run_id = $1::uuid
            """,
            run_id,
            task_id,
            entry.get("step"),
            None,
            entry.get("tool_name"),
            _capability_kind(entry.get("tool_name")),
            _json_dumps(_arguments_payload(entry.get("arguments"))),
            _json_dumps(result_payload),
            _latency_ms(entry.get("duration_seconds")),
            entry.get("status", "ok"),
            result_payload.get("error_code"),
            entry.get("error_message"),
            _valid_uuid_list(entry.get("source_execution_ids")),
        )

    async def _execute(self, query: str, *args: Any) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(query, *args)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def build_agent_run_state_store() -> AgentRunStateStore:
    """构建默认长期状态 Store。

    默认返回 no-op；设置 ``ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres`` 后写入 PostgreSQL。
    """
    backend = os.getenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "noop").strip().lower()
    if backend == "postgres":
        return PostgresAgentRunStateStore(database_url=os.getenv("ALIOTH_DATABASE_URL"))
    return NoopAgentRunStateStore()


async def _raise_if_cancel_won(
    connection: Any,
    record: AgentRunRecord,
    result: object,
) -> None:
    """把 UPDATE 0 区分为长期取消获胜或已有其他终态。"""

    if not (isinstance(result, str) and result.endswith(" 0")):
        return
    if record.status is AgentRunQueueStatus.CANCELED:
        raise AgentRunTerminalStateConflict(
            "Run 已进入其他终态，取消写入未获准且任务状态不得继续推进"
        )
    cancellation_won = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM qc_agent_run AS run
            LEFT JOIN qc_task AS task ON task.id = run.task_id
            WHERE run.id = $1::uuid
              AND (
                  run.status = 'canceled'
                  OR COALESCE(
                      (run.output_context->>'cancel_requested')::boolean,
                      FALSE
                  ) = TRUE
                  OR task.status = 'canceled'
              )
        )
        """,
        record.run_id,
    )
    if cancellation_won is True:
        raise AgentRunCancellationConflict("持久化取消标记阻止了 Run 完成写入")
    raise AgentRunTerminalStateConflict("Run 已进入不同终态，拒绝跨终态覆盖")


async def _lock_task_finalization(connection: Any, task_id: str) -> None:
    """使 Run 终态与取消标记共享任务收口的串行化边界。"""

    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"task-finalization:{task_id}",
    )


def _json_dumps(value: dict | list) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _input_context(record: AgentRunRecord) -> dict[str, Any]:
    registry = get_default_page_type_registry()
    return {
        "run_id": record.run_id,
        "submitted_by_user_id": record.submitted_by_user_id,
        "tenant_id": record.tenant_id,
        "message": record.message,
        "queue_status": record.status.value,
        "task_config": record.task_config,
        "page_sampling_policy": record.page_sampling_policy,
        "loop_guard": record.loop_guard,
        "page_type_catalog": {
            "version": registry.catalog.version,
            "sha256": registry.content_sha256,
        },
    }


def agent_run_input_context(record: AgentRunRecord) -> dict[str, Any]:
    """返回可持久化的初始 Run 输入；供任务提交事务复用同一合同。"""

    return _input_context(record)


def agent_run_from_postgres_row(row: Mapping[str, Any]) -> AgentRunRecord:
    """把 qc_agent_run 载荷还原成可执行或可继续收口的 Run 快照。"""
    input_context = _json_dict(row.get("input_context"))
    output_context = _json_dict(row.get("output_context"))
    working_memory = _json_dict(row.get("working_memory"))
    status = {
        "done": AgentRunQueueStatus.COMPLETED,
        "failed": AgentRunQueueStatus.FAILED,
        "aborted": AgentRunQueueStatus.FAILED,
        "canceled": AgentRunQueueStatus.CANCELED,
        "pending": AgentRunQueueStatus.QUEUED,
        "running": AgentRunQueueStatus.QUEUED,
    }.get(str(row.get("status") or "failed"), AgentRunQueueStatus.FAILED)
    created_at = row.get("created_at") or row.get("started_at") or row.get("finished_at")
    submitted_at = created_at or datetime.now(UTC)
    return AgentRunRecord(
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]) if row.get("task_id") is not None else None,
        submitted_by_user_id=(
            str(input_context["submitted_by_user_id"])
            if input_context.get("submitted_by_user_id") is not None
            else None
        ),
        tenant_id=(
            str(input_context["tenant_id"])
            if input_context.get("tenant_id") is not None
            else None
        ),
        message=str(input_context.get("message") or "PostgreSQL 恢复的终态 Run"),
        task_config=_json_dict(input_context.get("task_config")),
        page_sampling_policy=_json_dict(input_context.get("page_sampling_policy")),
        loop_guard=_json_dict(row.get("loop_guard")),
        status=status,
        submitted_at=submitted_at,
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        termination_reason=(
            str(row["stop_reason"]) if row.get("stop_reason") is not None else None
        ),
        final_text=(
            str(output_context["final_text"])
            if output_context.get("final_text") is not None
            else None
        ),
        step_count=int(row.get("total_steps") or 0),
        error_message=(str(row["error_message"]) if row.get("error_message") is not None else None),
        error_category=(
            str(output_context["error_category"])
            if output_context.get("error_category") is not None
            else None
        ),
        execution_outcome=(
            str(output_context["execution_outcome"])
            if output_context.get("execution_outcome") is not None
            else None
        ),
        usage=(
            dict(output_context["usage"])
            if isinstance(output_context.get("usage"), Mapping)
            else None
        ),
        working_memory=working_memory,
        cancellation_cleanup_completed=(
            output_context.get("cancellation_cleanup_completed") is True
        ),
        cancellation_finalized=output_context.get("cancellation_finalized") is True,
        final_report=(
            dict(output_context["final_report"])
            if isinstance(output_context.get("final_report"), Mapping)
            else None
        ),
        evidence_index_id=(
            str(output_context["evidence_index_id"])
            if output_context.get("evidence_index_id") is not None
            else None
        ),
        exploration_status=(
            str(output_context["exploration_status"])
            if output_context.get("exploration_status") is not None
            else None
        ),
        evidence_gap=[
            str(item) for item in output_context.get("evidence_gap") or () if str(item).strip()
        ],
        page_timeline=[
            str(item) for item in output_context.get("page_timeline") or () if str(item).strip()
        ],
    )


# 保留历史私有名称，避免现有定向测试和内部调用一次性迁移。
_agent_run_from_postgres_row = agent_run_from_postgres_row


def _agent_run_snapshot_from_postgres_row(row: Mapping[str, Any]) -> AgentRunRecord:
    """把 qc_agent_run 载荷还原成 API 展示快照，保留运行中状态。"""

    record = agent_run_from_postgres_row(row)
    record.status = {
        "done": AgentRunQueueStatus.COMPLETED,
        "failed": AgentRunQueueStatus.FAILED,
        "aborted": AgentRunQueueStatus.FAILED,
        "canceled": AgentRunQueueStatus.CANCELED,
        "pending": AgentRunQueueStatus.QUEUED,
        "running": AgentRunQueueStatus.RUNNING,
    }.get(str(row.get("status") or "failed"), AgentRunQueueStatus.FAILED)
    return record


async def _fetch_action_trace_rows(connection: Any, *, run_id: str) -> list[Mapping[str, Any]]:
    rows = await connection.fetch(
        """
        SELECT
            step_seq,
            selected_capability,
            capability_input,
            capability_output,
            latency_ms,
            status,
            error_message
        FROM qc_agent_action_trace
        WHERE agent_run_id = $1::uuid
        ORDER BY step_seq ASC
        """,
        run_id,
    )
    return list(rows)


def _action_trace_entries_from_postgres_rows(
    rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        latency_ms = row.get("latency_ms")
        entries.append(
            {
                "step": int(row.get("step_seq") or 0),
                "tool_name": str(row.get("selected_capability") or ""),
                "arguments": _json_dumps(_json_dict(row.get("capability_input"))),
                "status": str(row.get("status") or "error"),
                "duration_seconds": (
                    float(latency_ms) / 1000 if isinstance(latency_ms, int | float) else 0.0
                ),
                "error_message": (
                    str(row["error_message"]) if row.get("error_message") is not None else None
                ),
                "retry_count": 0,
                "result": _json_dumps(_json_dict(row.get("capability_output"))),
                "source_execution_ids": [],
            }
        )
    return entries


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _output_context(record: AgentRunRecord) -> dict[str, Any]:
    return {
        "final_text": record.final_text,
        "final_report": record.final_report,
        "execution_outcome": record.execution_outcome,
        "usage": record.usage,
        "error_category": record.error_category,
        "cancel_requested": record.status is AgentRunQueueStatus.CANCELED,
        "cancellation_cleanup_completed": record.cancellation_cleanup_completed,
        "cancellation_finalized": record.cancellation_finalized,
        "evidence_index_id": record.evidence_index_id,
        "exploration_status": record.exploration_status,
        "evidence_gap": record.evidence_gap,
        "page_timeline": record.page_timeline,
    }


def _working_memory(record: AgentRunRecord) -> dict[str, Any]:
    snapshot = dict(record.working_memory)
    snapshot.setdefault("schema_version", 1)
    snapshot.setdefault("current_page", None)
    snapshot.setdefault("inspected_pages", [])
    snapshot.setdefault("pending_pages", [])
    snapshot.setdefault("inspected_page_count", 0)
    snapshot.setdefault("pending_page_count", 0)
    snapshot["step_count"] = record.step_count or 0
    snapshot["action_trace_count"] = len(record.action_trace)
    return snapshot


def _agent_run_status(status: AgentRunQueueStatus) -> str:
    return {
        AgentRunQueueStatus.QUEUED: "pending",
        AgentRunQueueStatus.RUNNING: "running",
        AgentRunQueueStatus.COMPLETED: "done",
        AgentRunQueueStatus.FAILED: "failed",
        AgentRunQueueStatus.CANCELED: "canceled",
    }[status]


def _task_status(
    status: AgentRunQueueStatus,
    termination_reason: str | None = None,
    execution_outcome: str | None = None,
) -> str:
    """将队列 Run 终态映射为业务任务终态。"""
    if status == AgentRunQueueStatus.COMPLETED and execution_outcome == "needs_review":
        return "review"
    if status == AgentRunQueueStatus.FAILED and is_review_termination_reason(termination_reason):
        return "review"
    return {
        AgentRunQueueStatus.QUEUED: "queued",
        AgentRunQueueStatus.RUNNING: "running",
        AgentRunQueueStatus.COMPLETED: "done",
        AgentRunQueueStatus.FAILED: "failed",
        AgentRunQueueStatus.CANCELED: "canceled",
    }[status]


def _task_finish_source_statuses(target_status: TaskStatus) -> tuple[TaskStatus, ...]:
    """结束事件只可从 running 推进；失败或取消兼容尚未启动的排队任务。"""
    if target_status in {TaskStatus.FAILED, TaskStatus.CANCELED}:
        return (TaskStatus.QUEUED, TaskStatus.RUNNING, target_status)
    return (TaskStatus.RUNNING, target_status)


async def _requires_task_review(connection: Any, *, task_id: str) -> bool:
    """判断任务是否仍有必须人工确认的正式规则结果（按质检规则统一口径，不分 Run）。"""
    return bool(
        await connection.fetchval(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM qc_rule_execution AS execution
                WHERE execution.task_id = $1::uuid
                  AND execution.human_verdict IS NULL
                  AND NOT {superseded_placeholder_sql("execution")}
                  AND {requires_human_confirmation_sql("execution")}
            )
            """,
            task_id,
        )
    )


def _total_tokens(record: AgentRunRecord) -> int:
    if record.usage is None:
        return 0
    return int(record.usage.get("total_tokens") or 0)


def _total_ai_calls(record: AgentRunRecord) -> int:
    if record.usage is None:
        return 0
    return int(record.usage.get("ai_calls") or 0)


def _total_cost(record: AgentRunRecord) -> Decimal:
    if record.usage is None:
        return Decimal("0")
    return Decimal(str(record.usage.get("total_cost") or 0))


def _arguments_payload(raw_arguments: Any) -> dict[str, Any]:
    if raw_arguments is None or raw_arguments == "":
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    try:
        payload = json.loads(str(raw_arguments))
    except json.JSONDecodeError:
        return {"raw": str(raw_arguments)}
    return payload if isinstance(payload, dict) else {"value": payload}


def _result_payload(raw_result: Any) -> dict[str, Any]:
    """工具返回值留痕：JSON 可解析则原样入库，截断/纯文本则包成 text 字段。"""
    if raw_result is None or raw_result == "":
        return {}
    if isinstance(raw_result, dict):
        return raw_result
    try:
        payload = json.loads(str(raw_result))
    except json.JSONDecodeError:
        return {"text": str(raw_result)}
    return payload if isinstance(payload, dict) else {"value": payload}


def _capability_kind(tool_name: Any) -> str:
    """load_skill 调用记为 skill 加载，其余均为 tool 调用。"""
    return "skill" if tool_name == "load_skill" else "tool"


def _latency_ms(duration_seconds: Any) -> int | None:
    if duration_seconds is None:
        return None
    return int(float(duration_seconds) * 1000)


def _valid_uuid_list(values: Any) -> list[UUID]:
    """只允许合法执行记录 UUID 进入关联 SQL。"""
    if not isinstance(values, (list, tuple)):
        return []
    result: list[UUID] = []
    for value in values:
        try:
            result.append(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    return result
