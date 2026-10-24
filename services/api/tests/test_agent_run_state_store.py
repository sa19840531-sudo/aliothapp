import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from services.api.app.domain.task.status import TaskStatus, TaskStatusConflictError
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
)
from services.api.app.infrastructure.queue.state_store import (
    AgentRunCancellationConflict,
    AgentRunTerminalStateConflict,
    PostgresAgentRunStateStore,
    _agent_run_from_postgres_row,
)


class FakeAcquire:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    async def __aenter__(self) -> "FakeConnection":
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(
        self,
        *,
        task_status: TaskStatus = TaskStatus.QUEUED,
        requires_review: bool = False,
        cancel_wins_on_run_update: bool = False,
        cancel_target_task_id: str | None = None,
        cancel_marker_accepted: bool = True,
        cancel_requested: bool = False,
    ) -> None:
        self.connection = FakeConnection(
            task_status=task_status,
            requires_review=requires_review,
            cancel_wins_on_run_update=cancel_wins_on_run_update,
            cancel_target_task_id=cancel_target_task_id,
            cancel_marker_accepted=cancel_marker_accepted,
            cancel_requested=cancel_requested,
        )

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeConnection:
    def __init__(
        self,
        *,
        task_status: TaskStatus,
        requires_review: bool,
        cancel_wins_on_run_update: bool,
        cancel_target_task_id: str | None,
        cancel_marker_accepted: bool,
        cancel_requested: bool,
    ) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.task_status = task_status
        self.task_row_version = 0
        self.requires_review = requires_review
        self.transaction_entered = 0
        self.cancel_wins_on_run_update = cancel_wins_on_run_update
        self.cancel_target_task_id = cancel_target_task_id
        self.cancel_marker_accepted = cancel_marker_accepted
        self.cancel_requested = cancel_requested
        self.fetchrow_result: dict | None = None
        self.fetch_result: list[dict] = []

    def transaction(self) -> FakeAcquire:
        self.transaction_entered += 1
        return FakeAcquire(self)

    async def execute(self, query: str, *args) -> str:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if self.cancel_wins_on_run_update and compact.startswith("UPDATE qc_agent_run"):
            return "UPDATE 0"
        if not self.cancel_marker_accepted and compact.startswith("UPDATE qc_agent_run"):
            return "UPDATE 0"
        return "OK"

    async def fetchval(self, query: str, *args) -> bool | str | None:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if "SELECT task_id::text FROM qc_agent_run" in compact:
            return self.cancel_target_task_id
        if "output_context->>'cancel_requested'" in compact:
            return self.cancel_requested
        # INSERT/UPDATE ... RETURNING true 表示 CAS 成功；SELECT EXISTS
        # 仍由 requires_review 控制，避免把状态写入误判成人工复核查询。
        if "RETURNING true" in compact and (
            compact.startswith("INSERT INTO qc_agent_run")
            or compact.startswith("UPDATE qc_agent_run")
        ):
            return True
        return self.requires_review

    async def fetch(self, query: str, *args) -> list[dict]:
        self.calls.append((" ".join(query.split()), args))
        return self.fetch_result

    async def fetchrow(self, query: str, *args) -> dict:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if compact.startswith("SELECT"):
            if self.fetchrow_result is not None:
                return self.fetchrow_result
            return {
                "task_id": str(args[0]),
                "status": self.task_status.value,
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        if compact.startswith("UPDATE qc_task"):
            self.task_status = TaskStatus(str(args[3]))
            self.task_row_version += 1
            return {
                "task_id": str(args[0]),
                "status": self.task_status.value,
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        raise AssertionError(f"未预期的 fetchrow 查询：{compact}")


def _record(*, task_id: str | None = None) -> AgentRunRecord:
    return AgentRunRecord(
        run_id=str(uuid4()),
        task_id=task_id,
        message="质检这个页面",
        submitted_at=datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
    )


def test_postgres_terminal_row_restores_report_for_gate_recovery() -> None:
    record = _agent_run_from_postgres_row(
        {
            "run_id": str(uuid4()),
            "task_id": str(uuid4()),
            "status": "done",
            "input_context": {
                "message": "恢复任务",
                "submitted_by_user_id": "user-1",
                "tenant_id": "tenant-1",
                "task_config": {"kind": "app"},
            },
            "output_context": {
                "final_report": {
                    "report_stage": "preview",
                    "completion_gate": {"gate_passed": False},
                },
                "execution_outcome": "completed",
            },
            "working_memory": {"current_page": {"url": "https://example.test"}},
            "loop_guard": {"max_steps": 10},
            "stop_reason": "completed",
            "error_message": None,
            "total_steps": 3,
            "started_at": datetime(2026, 7, 6, 10, 1, tzinfo=UTC),
            "finished_at": datetime(2026, 7, 6, 10, 2, tzinfo=UTC),
            "created_at": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        }
    )

    assert record.status is AgentRunQueueStatus.COMPLETED
    assert record.final_report["completion_gate"]["gate_passed"] is False
    assert record.working_memory["current_page"]["url"] == "https://example.test"
    assert record.task_config == {"kind": "app"}
    assert record.submitted_by_user_id == "user-1"
    assert record.tenant_id == "tenant-1"


async def test_postgres_state_store_gets_one_finished_run_by_identity() -> None:
    task_id = str(uuid4())
    run_id = str(uuid4())
    pool = FakePool()
    pool.connection.fetchrow_result = {
        "run_id": run_id,
        "task_id": task_id,
        "status": "done",
        "input_context": {"message": "恢复终态", "task_config": {"kind": "app"}},
        "output_context": {},
        "working_memory": {},
        "loop_guard": {},
        "stop_reason": "completed",
        "error_message": None,
        "total_steps": 1,
        "started_at": datetime(2026, 7, 6, 10, 1, tzinfo=UTC),
        "finished_at": datetime(2026, 7, 6, 10, 2, tzinfo=UTC),
        "created_at": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
    }
    store = PostgresAgentRunStateStore(pool=pool)

    record = await store.get_finished_record(run_id=run_id)

    assert record is not None
    assert record.run_id == run_id
    assert record.status is AgentRunQueueStatus.COMPLETED
    query, args = pool.connection.calls[0]
    assert "WHERE id = $1::uuid" in query
    assert "status IN ('done', 'failed', 'aborted', 'canceled')" in query
    assert args == (run_id,)


async def test_postgres_state_store_get_record_snapshot_includes_action_trace() -> None:
    task_id = str(uuid4())
    run_id = str(uuid4())
    pool = FakePool()
    pool.connection.fetchrow_result = {
        "run_id": run_id,
        "task_id": task_id,
        "status": "running",
        "input_context": {"message": "运行中任务", "task_config": {"kind": "h5"}},
        "output_context": {},
        "working_memory": {"current_page": {"url": "https://example.test"}},
        "loop_guard": {},
        "stop_reason": None,
        "error_message": None,
        "total_steps": 1,
        "started_at": datetime(2026, 7, 6, 10, 1, tzinfo=UTC),
        "finished_at": None,
        "created_at": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
    }
    pool.connection.fetch_result = [
        {
            "step_seq": 1,
            "selected_capability": "page_capture_tool",
            "capability_input": {"mode": "goto"},
            "capability_output": {"execution_status": "success"},
            "latency_ms": 120,
            "status": "ok",
            "error_message": None,
        }
    ]
    store = PostgresAgentRunStateStore(pool=pool)

    record = await store.get_record_snapshot(run_id=run_id)

    assert record is not None
    assert record.status is AgentRunQueueStatus.RUNNING
    assert record.action_trace == [
        {
            "step": 1,
            "tool_name": "page_capture_tool",
            "arguments": '{"mode": "goto"}',
            "status": "ok",
            "duration_seconds": 0.12,
            "error_message": None,
            "retry_count": 0,
            "result": '{"execution_status": "success"}',
            "source_execution_ids": [],
        }
    ]
    assert "FROM qc_agent_action_trace" in pool.connection.calls[1][0]
    assert pool.connection.calls[1][1] == (run_id,)


@pytest.mark.parametrize("postgres_status", ["pending", "running"])
def test_postgres_pending_or_running_row_restores_as_queued_and_keeps_started_at(
    postgres_status: str,
) -> None:
    started_at = datetime(2026, 7, 6, 10, 1, tzinfo=UTC)
    record = _agent_run_from_postgres_row(
        {
            "run_id": str(uuid4()),
            "task_id": str(uuid4()),
            "status": postgres_status,
            "input_context": {"message": "恢复中的 App Run", "task_config": {"kind": "app"}},
            "output_context": {},
            "working_memory": {"pending_pages": ["page-1"]},
            "loop_guard": {"max_steps": 10},
            "stop_reason": None,
            "error_message": None,
            "total_steps": 2,
            "started_at": started_at,
            "finished_at": None,
            "created_at": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        }
    )

    assert record.status is AgentRunQueueStatus.QUEUED
    assert record.started_at == started_at
    assert record.working_memory["pending_pages"] == ["page-1"]


async def test_postgres_state_store_inserts_pending_agent_run_on_submit() -> None:
    task_id = str(uuid4())
    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.tenant_id = "tenant-1"
    record.task_config = {"viewport": "390x844"}
    record.page_sampling_policy = {"mode": "first_n", "max_pages": 3}
    record.loop_guard = {"max_steps": 18}

    await store.record_submitted(record)

    query, args = pool.connection.calls[0]
    assert "INSERT INTO qc_agent_run" in query
    assert "loop_guard" in query
    assert args[0] == record.run_id
    assert args[1] == task_id
    assert args[4] == "pending"
    assert '"message": "质检这个页面"' in args[5]
    assert '"viewport": "390x844"' in args[5]
    assert '"max_pages": 3' in args[5]
    assert '"max_steps": 18' in args[6]
    assert '"tenant_id": "tenant-1"' in args[5]


def test_postgres_row_without_tenant_keeps_historical_record_compatible() -> None:
    record = _agent_run_from_postgres_row(
        {
            "run_id": str(uuid4()),
            "task_id": None,
            "status": "done",
            "input_context": {"message": "旧 direct Run"},
            "output_context": {},
            "working_memory": {},
            "loop_guard": {},
            "stop_reason": "completed",
            "error_message": None,
            "total_steps": 0,
            "started_at": None,
            "finished_at": datetime(2026, 7, 6, 10, 2, tzinfo=UTC),
            "created_at": datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
        }
    )

    assert record.submitted_by_user_id is None
    assert record.tenant_id is None


async def test_submit_conflict_never_downgrades_progressed_run_status() -> None:
    """空闲 Worker 可先于提交写库完成 record_started；提交冲突不得把 running 踩回 pending。"""
    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)

    await store.record_submitted(_record(task_id=str(uuid4())))

    query, _args = pool.connection.calls[0]
    conflict_clause = query.split("ON CONFLICT", 1)[1]
    assert "status = EXCLUDED.status" not in conflict_clause


async def test_postgres_state_store_marks_task_running_when_run_starts() -> None:
    task_id = str(uuid4())
    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = datetime(2026, 7, 6, 10, 1, tzinfo=UTC)

    await store.record_started(record)

    assert pool.connection.transaction_entered == 1
    run_insert_query, _run_insert_args = next(
        call
        for call in pool.connection.calls
        if call[0].startswith("INSERT INTO qc_agent_run")
    )
    assert "WHERE qc_agent_run.status IN ('pending', 'running')" in run_insert_query
    assert "qc_agent_run.task_id = EXCLUDED.task_id" in run_insert_query
    assert "RETURNING true" in run_insert_query
    task_update_query, task_update_args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_task")
    )
    assert "row_version = $3" in task_update_query
    assert "finalization_state <> 'sealed'" in task_update_query
    assert task_update_args[0] == task_id
    assert task_update_args[1] == "queued"
    assert task_update_args[3] == "running"
    assert task_update_args[4] == record.started_at


async def test_postgres_state_store_updates_finished_run_and_inserts_action_trace() -> None:
    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.COMPLETED
    record.started_at = datetime(2026, 7, 6, 10, 1, tzinfo=UTC)
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "completed"
    record.final_text = "质检完成"
    record.final_report = {
        "report_version": "v1",
        "summary": {"overall_status": "incomplete"},
    }
    record.step_count = 1
    record.working_memory = {
        "schema_version": 1,
        "current_page": None,
        "inspected_page_count": 1,
    }
    record.usage = {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
        "ai_calls": 2,
        "total_cost": 0.123456,
    }
    record.action_trace = [
        {
            "step": 1,
            "tool_name": "echo_tool",
            "arguments": '{"message": "hello"}',
            "status": "ok",
            "duration_seconds": 0.25,
            "error_message": None,
            "retry_count": 0,
            "result": '{"echo": "hello"}',
        }
    ]

    await store.record_finished(record)

    queries = [query for query, _args in pool.connection.calls]
    assert any("UPDATE qc_agent_run" in query for query in queries)
    run_update_query = next(
        query for query in queries if query.startswith("UPDATE qc_agent_run")
    )
    assert "status IN ('pending', 'running')" in run_update_query
    assert "status IN ('pending', 'running') OR status = $2::varchar" in run_update_query
    assert "output_context->>'cancel_requested'" in run_update_query
    assert "output_context->'web_formal_terminalized'" in run_update_query
    assert "'{web_formal_terminalized}'" in run_update_query
    assert any("INSERT INTO qc_agent_action_trace" in query for query in queries)
    run_update_query = next(query for query in queries if query.startswith("UPDATE qc_agent_run"))
    assert "status IN ('pending', 'running')" in run_update_query
    assert "OR status = $2::varchar" in run_update_query
    assert "status = 'canceled'" in run_update_query
    assert "task.status = 'canceled'" in run_update_query
    assert "total_cost = $10::numeric" in run_update_query
    update_args = next(
        args for query, args in pool.connection.calls if query.startswith("UPDATE qc_agent_run")
    )
    trace_args = next(
        args for query, args in pool.connection.calls if query.startswith("WITH trace_row AS")
    )
    assert update_args[1] == "done"
    assert '"final_report"' in update_args[2]
    assert '"overall_status": "incomplete"' in update_args[2]
    assert '"inspected_page_count": 1' in update_args[3]
    assert update_args[6] == 1
    assert update_args[7] == 2
    assert update_args[8] == 7
    assert update_args[9] == Decimal("0.123456")
    assert trace_args[0] == record.run_id
    assert trace_args[1] == task_id
    assert trace_args[2] == 1
    assert trace_args[4] == "echo_tool"
    assert trace_args[5] == "tool"
    assert '"echo": "hello"' in trace_args[7]
    assert trace_args[8] == 250
    review_query, review_args = next(
        call for call in pool.connection.calls if "FROM qc_rule_execution AS execution" in call[0]
    )
    task_update_query, task_update_args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_task")
    )
    assert pool.connection.transaction_entered == 1
    assert "UPDATE qc_task" in task_update_query
    assert "row_version = $3" in task_update_query
    # 仅有效业务转人工需要确认；未执行、占位和平台故障不能靠人工结论补齐。
    assert "execution.execution_status = 'manual_review'" in review_query
    assert "candidate_not_executed" in review_query
    assert "run_budget_exhausted" in review_query
    assert "semantic_provider_" in review_query
    assert "semantic_judge_unavailable" in review_query
    assert "jsonb_array_elements_text" in review_query
    assert "judge_output->>'reason', ''))) NOT IN" in review_query
    assert "!~ '^(capability_" in review_query
    assert "AND NOT EXISTS" in review_query
    assert "review_status IN" not in review_query
    assert "judge_input->>'bootstrap', 'false') <> 'true'" in review_query
    # 外层按任务汇总独立 Judge，不统一限制执行 Run；H5 占位顶替只认当前主 Run 成果。
    assert "execution.agent_run_id" not in review_query
    assert "current_h5_task.target_platform = 'h5'" in review_query
    assert "current_h5_task.task_config->>'workflow_version'" in review_query
    assert "concluded.agent_run_id IS DISTINCT FROM" in review_query
    assert "current_source_capture.agent_run_id =" in review_query
    assert "execution.human_verdict IS NULL" in review_query
    assert "FROM qc_rule_execution AS concluded" in review_query
    assert review_args == (task_id,)
    assert task_update_args[0] == task_id
    assert task_update_args[1] == "running"
    assert task_update_args[3] == "done"
    assert task_update_args[4] == record.finished_at
    assert task_update_args[7] is None


async def test_run_finish_owner_claim_ignores_logical_inspection_consumer_run() -> None:
    """enforced 逻辑消费 Run 在主 Run 收场后仍合法 running，不得阻塞收口所有权认领。

    真实回归：enforced 任务的收口快照 Run 恒为 NULL，原子收口报
    「任务收口 Run 与当前命令不匹配」（任务 5097f736 等三轮全灭根因）。
    """
    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.COMPLETED
    record.started_at = datetime(2026, 7, 6, 10, 1, tzinfo=UTC)
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "completed"
    record.final_text = "质检完成"
    record.final_report = {
        "report_version": "v1",
        "summary": {"overall_status": "completed"},
    }
    record.step_count = 1

    await store.record_run_finished(record)

    claim_query = next(
        query
        for query, _args in pool.connection.calls
        if "finalization_agent_run_id = $2::uuid" in query
    )
    assert "task.finalization_agent_run_id IS NULL" in claim_query
    assert "active_run.status IN ('pending', 'running')" in claim_query
    assert "active_run.agent_type = 'rule_judge'" in claim_query
    assert "active_run.input_context->>'source' IN (" in claim_query
    assert "'h5_inspection_publication'" in claim_query
    assert "'app_inspection_publication'" in claim_query
    assert "active_run.input_context->>'finalization_epoch' = task.finalization_epoch::text" in (
        claim_query
    )


async def test_postgres_state_store_records_run_finish_without_advancing_task() -> None:
    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "completed"
    record.usage = {
        "total_tokens": 11,
        "ai_calls": 3,
        "total_cost": "0.420001",
    }

    await store.record_run_finished(record)

    run_update_query, run_update_args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_agent_run")
    )
    assert "status IN ('pending', 'running')" in run_update_query
    assert "OR status = $2::varchar" in run_update_query
    assert "status = 'canceled'" in run_update_query
    assert "task.status = 'canceled'" in run_update_query
    assert "status <> 'canceled'" in run_update_query
    assert "task.status <> 'canceled'" in run_update_query
    run_update_query = next(
        query
        for query, _args in pool.connection.calls
        if query.startswith("UPDATE qc_agent_run")
    )
    assert "AND task_id = $12::uuid" in run_update_query
    assert "total_cost = $10::numeric" in run_update_query
    assert "status IN ('pending', 'running')" in run_update_query
    assert "OR status = $2::varchar" in run_update_query
    assert "status IN ('pending', 'running') OR status = $2::varchar" in run_update_query
    assert "output_context->>'cancel_requested'" in run_update_query
    assert "output_context->'web_formal_terminalized'" in run_update_query
    assert "'{web_formal_terminalized}'" in run_update_query
    assert run_update_args[7] == 3
    assert run_update_args[8] == 11
    assert run_update_args[9] == Decimal("0.420001")
    ownership_query, ownership_args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_task AS task")
    )
    assert "finalization_agent_run_id = $2::uuid" in ownership_query
    assert "finalization_agent_run_id IS NULL" in ownership_query
    assert "active_run.status IN ('pending', 'running')" in ownership_query
    assert "active_run.agent_type = 'rule_judge'" in ownership_query
    assert "active_run.input_context->>'source'" in ownership_query
    assert "active_run.input_context->>'source' IN (" in ownership_query
    assert "'h5_inspection_publication'" in ownership_query
    assert "'app_inspection_publication'" in ownership_query
    assert "active_run.input_context->>'finalization_epoch'" in ownership_query
    assert "= task.finalization_epoch::text" in ownership_query
    assert ownership_args == (task_id, record.run_id)
    assert pool.connection.transaction_entered == 1


async def test_run_finish_refuses_durable_cancel_marker_before_completion() -> None:
    """若完成 UPDATE 不检查持久化取消标记，Redis 检查后的取消仍可覆盖为 completed。"""

    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)

    await store.record_run_finished(record)

    run_update_query, _ = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_agent_run")
    )
    assert "output_context->>'cancel_requested'" in run_update_query


async def test_run_finish_raises_when_durable_cancel_wins_interleaving() -> None:
    """Redis 复查后写入的持久化取消标记必须中断完成收口。"""

    pool = FakePool(
        task_status=TaskStatus.RUNNING,
        cancel_wins_on_run_update=True,
        cancel_requested=True,
    )
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=str(uuid4()))
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)

    with pytest.raises(AgentRunCancellationConflict):
        await store.record_run_finished(record)

    assert not any(
        query.startswith("UPDATE qc_task AS task") for query, _args in pool.connection.calls
    )


async def test_run_finish_rejects_stale_cross_terminal_update() -> None:
    pool = FakePool(
        task_status=TaskStatus.DONE,
        cancel_wins_on_run_update=True,
        cancel_requested=False,
    )
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=str(uuid4()))
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)

    with pytest.raises(AgentRunTerminalStateConflict):
        await store.record_run_finished(record)

    assert not any(
        query.startswith("UPDATE qc_task AS task") for query, _args in pool.connection.calls
    )

async def test_canceled_run_finish_preserves_durable_cancel_marker() -> None:
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=str(uuid4()))
    record.status = AgentRunQueueStatus.CANCELED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "canceled"

    await store.record_run_finished(record)

    _query, args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_agent_run")
    )
    assert json.loads(args[2])["cancel_requested"] is True



async def test_finished_record_scan_skips_durable_cancel_marker() -> None:
    """Completion Gate 后台扫描不能恢复已经赢得竞争的取消 Run。"""

    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)

    assert await store.list_finished_records(limit=10) == []

    query, _args = pool.connection.calls[0]
    assert "output_context->>'cancel_requested'" in query
    assert "status <> 'canceled'" in query


async def test_cancel_acceptance_is_false_after_run_is_already_terminal() -> None:
    """完成先线性化后，取消请求不得仍被报告为已接受。"""

    task_id = str(uuid4())
    pool = FakePool(
        cancel_target_task_id=task_id,
        cancel_marker_accepted=False,
    )
    store = PostgresAgentRunStateStore(pool=pool)

    accepted = await store.record_cancel_requested(str(uuid4()))

    assert accepted is False
    lock_index = next(
        index
        for index, (query, _args) in enumerate(pool.connection.calls)
        if query.startswith("SELECT pg_advisory_xact_lock")
    )
    marker_index = next(
        index
        for index, (query, _args) in enumerate(pool.connection.calls)
        if query.startswith("UPDATE qc_agent_run")
    )
    assert lock_index < marker_index


async def test_postgres_state_store_reads_durable_cancel_marker() -> None:
    """H5 检查点必须能在 Redis 记录缺失时读取 PostgreSQL 取消事实。"""

    pool = FakePool(cancel_requested=True)
    store = PostgresAgentRunStateStore(pool=pool)
    run_id = str(uuid4())

    assert await store.is_cancel_requested(run_id) is True

    query, args = pool.connection.calls[0]
    assert "output_context->>'cancel_requested'" in query
    assert "status = 'canceled'" in query
    assert args == (run_id,)


async def test_postgres_state_store_maps_guard_run_to_review_task() -> None:
    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.FAILED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "timeout"

    await store.record_finished(record)

    run_update_query, run_update_args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_agent_run")
    )
    task_update_query, task_update_args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_task")
    )
    assert "UPDATE qc_agent_run" in run_update_query
    assert run_update_args[1] == "failed"
    assert run_update_args[4] == "timeout"
    assert "UPDATE qc_task" in task_update_query
    assert task_update_args[3] == "review"


async def test_postgres_state_store_maps_completed_degraded_run_to_review_task() -> None:
    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "completed"
    record.execution_outcome = "needs_review"

    await store.record_finished(record)

    run_update_args = next(
        args for query, args in pool.connection.calls if query.startswith("UPDATE qc_agent_run")
    )
    task_update_args = next(
        args for query, args in pool.connection.calls if query.startswith("UPDATE qc_task")
    )
    assert run_update_args[1] == "done"
    assert '"execution_outcome": "needs_review"' in run_update_args[2]
    assert task_update_args[3] == "review"


async def test_postgres_state_store_keeps_error_run_as_failed_task() -> None:
    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.FAILED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "error"

    await store.record_finished(record)

    task_update_args = next(
        args for query, args in pool.connection.calls if query.startswith("UPDATE qc_task")
    )
    assert task_update_args[3] == "failed"


async def test_postgres_state_store_rejects_late_finish_after_task_terminal() -> None:
    task_id = str(uuid4())
    pool = FakePool(task_status=TaskStatus.DONE)
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=task_id)
    record.status = AgentRunQueueStatus.FAILED
    record.finished_at = datetime(2026, 7, 6, 10, 2, tzinfo=UTC)
    record.termination_reason = "error"

    with pytest.raises(TaskStatusConflictError, match="已收口"):
        await store.record_finished(record)

    assert not any(query.startswith("UPDATE qc_task") for query, _args in pool.connection.calls)


async def test_postgres_state_store_persists_block_error_code() -> None:
    task_id = str(uuid4())
    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)

    await store.insert_action_trace_entry(
        run_id=str(uuid4()),
        task_id=task_id,
        entry={
            "step": 1,
            "tool_name": "report_publish_tool",
            "arguments": "{}",
            "status": "denied",
            "duration_seconds": 0.1,
            "result": (
                '{"execution_status":"blocked","error_code":"REPORT_PUBLISH_CONDITION_UNMET"}'
            ),
        },
    )

    query, args = pool.connection.calls[0]
    assert "error_code" in query
    assert args[9] == "denied"
    assert args[10] == "REPORT_PUBLISH_CONDITION_UNMET"


async def test_postgres_state_store_binds_rule_executions_to_action_trace() -> None:
    task_id = str(uuid4())
    run_id = str(uuid4())
    execution_ids = [str(uuid4()), str(uuid4())]
    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)

    await store.insert_action_trace_entry(
        run_id=run_id,
        task_id=task_id,
        entry={
            "step": 3,
            "tool_name": "record_rule_executions_batch_tool",
            "arguments": "{}",
            "status": "ok",
            "duration_seconds": 0.1,
            "result": '{"execution_status":"success"}',
            "source_execution_ids": execution_ids,
        },
    )

    query, args = pool.connection.calls[0]
    assert "RETURNING id" in query
    assert "UPDATE qc_rule_execution" in query
    assert "source_action_trace_id = trace_row.id" in query
    assert "execution.task_id = $2::uuid" in query
    assert "execution.agent_run_id = $1::uuid" in query
    assert [str(value) for value in args[12]] == execution_ids


async def test_postgres_state_store_skips_record_without_task_id() -> None:
    pool = FakePool()
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=None)

    await store.record_submitted(record)
    record.status = AgentRunQueueStatus.RUNNING
    await store.record_started(record)

    assert pool.connection.calls == []


async def test_postgres_state_store_lists_incomplete_cancellation_recovery_records() -> None:
    pool = FakePool(task_status=TaskStatus.RUNNING)
    store = PostgresAgentRunStateStore(pool=pool)

    records = await store.list_cancellation_recovery_records(limit=25)

    assert records == []
    query, args = next(
        call for call in pool.connection.calls if "FROM qc_agent_run" in call[0]
    )
    assert "status = 'canceled'" in query
    assert "output_context->>'cancel_requested'" in query
    assert "cancellation_cleanup_completed" in query
    assert "cancellation_finalized" in query
    assert args == (25,)


async def test_canceled_finish_rejects_zero_row_terminal_cas() -> None:
    pool = FakePool(
        task_status=TaskStatus.DONE,
        cancel_wins_on_run_update=True,
    )
    store = PostgresAgentRunStateStore(pool=pool)
    record = _record(task_id=str(uuid4()))
    record.status = AgentRunQueueStatus.CANCELED
    record.termination_reason = "canceled"
    record.finished_at = datetime.now(UTC)

    with pytest.raises(AgentRunTerminalStateConflict):
        await store.record_finished(record)
