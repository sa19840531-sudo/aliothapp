"""收口结算：停滞工作项按事实结算、结算排在门禁之前、排空窗口用尽不再抛错卡死。

排期「完善任务结束和资源清理流程」（01 专项 §6 步骤 9「停止新动作后结算剩余工作，
再冻结报告输入并释放资源」）。结算只碰「自己再也推进不了」的工作项，结论一律是证据不足
并留放弃原因，不把缺失证据伪装成通过。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.app_live_inspection_publish import (
    AppActiveSessionDrainState,
    AppActiveSessionInspectionDrainService,
)
from services.api.app.application.h5_live_inspection_publish import (
    H5ActiveSessionDrainState,
    H5ActiveSessionInspectionDrainService,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemClosureSettlement,
    InspectionWorkItemService,
)
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    PostgresEvidenceInputSetRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresCompletionGatePreparer,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)


# ---------------- 仓储 SQL 合同 ----------------


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.rows = rows or []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "OK"

    async def fetch(self, query: str, *args: Any):
        self.calls.append(("fetch", " ".join(query.split()), args))
        if query.rstrip().endswith("FOR UPDATE OF work, member, execution"):
            return []
        return self.rows


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


async def test_settlement_sql_only_touches_work_that_cannot_progress() -> None:
    connection = FakeConnection(
        [
            {
                "work_item_id": "work-1",
                "stall_reason": "supplement_satisfied_but_retry_exhausted",
                "settled_rule_count": 2,
            },
            {
                "work_item_id": "work-2",
                "stall_reason": "no_supplement_request",
                "settled_rule_count": 1,
            },
        ]
    )
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    result = await repository.settle_stalled_work_for_closure(task_id=TASK_ID)

    assert result.settled_work_item_count == 2
    assert result.settled_rule_count == 3
    assert result.reason_counts == {
        "supplement_satisfied_but_retry_exhausted": 1,
        "no_supplement_request": 1,
    }
    assert result.settled is True

    lock = next(call for call in connection.calls if "pg_advisory_xact_lock" in call[1])
    assert lock[2] == (f"task-finalization:{TASK_ID}",)
    proof_read = next(
        call
        for call in connection.calls
        if call[1].endswith("FOR UPDATE OF work, member, execution")
    )
    assert proof_read[2] == (TASK_ID,)
    query, args = next(call[1:] for call in connection.calls if "WITH approved_reuse" in call[1])
    assert args == (TASK_ID, None, False, False, "[]")
    # 已收口的任务不再写执行记录（有触发器拦），非终态才是候选
    assert "task.finalization_state NOT IN ('finalizing', 'sealed')" in query
    assert (
        "work_item.status NOT IN ( 'succeeded', 'partial', 'failed', 'cancelled', 'superseded' )"
    ) in query
    # 还有在途补采、或还能重试的一律不碰
    assert "inflight.status IN ('pending', 'routed', 'running')" in query
    assert "work_item.attempt_count < work_item.max_attempts" in query
    # 结算写正式的证据不足执行 + 复核项，并回写成员与工作项终态
    assert "INSERT INTO qc_rule_execution" in query
    assert "'evidence_insufficient'" in query
    assert "INSERT INTO qc_review_item" in query
    assert "'INSPECTION_CLOSURE_SETTLEMENT'" in query
    assert "UPDATE qc_inspection_attempt" in query
    assert "'abandoned'" in query
    assert "THEN 'partial'" in query
    # 放弃原因逐类落库
    assert "补采证据已获得但重试次数用尽" in query
    assert "补采请求已终结且无法再取证" in query
    assert "等待补采但没有可执行的补采请求" in query
    assert "重试次数已用尽" in query


async def test_settlement_can_close_active_session_work_when_browser_is_closing() -> None:
    connection = FakeConnection(
        [
            {
                "work_item_id": "work-1",
                "stall_reason": "active_session_closing",
                "settled_rule_count": 3,
            }
        ]
    )
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    result = await repository.settle_stalled_work_for_closure(
        task_id=TASK_ID,
        active_session_agent_run_id=RUN_ID,
    )

    assert result.reason_counts == {"active_session_closing": 1}
    query, args = next(call[1:] for call in connection.calls if "WITH approved_reuse" in call[1])
    assert args == (TASK_ID, RUN_ID, False, False, "[]")
    assert "work_item.session_dependency = 'active_session'" in query
    assert "work_item.producer_agent_run_id = $2::uuid" in query
    assert "work_item.session_owner_agent_run_id = $2::uuid" in query
    assert "work_item.consumer_agent_run_id = $2::uuid" not in query
    assert "COALESCE(" in query
    assert "浏览器会话即将关闭" in query


async def test_settlement_accepts_explicit_closed_snapshot_generation() -> None:
    connection = FakeConnection([])
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    await repository.settle_stalled_work_for_closure(
        task_id=TASK_ID,
        snapshot_generation_closed=True,
    )

    query, args = next(call[1:] for call in connection.calls if "WITH approved_reuse" in call[1])
    assert args == (TASK_ID, None, True, False, "[]")
    assert "task.finalization_state = 'session_draining' OR $3::boolean" in query
    assert "inflight.status IN ('pending', 'routed', 'running')" in query
    assert "work_item.attempt_count < work_item.max_attempts" in query


async def test_h5_checkpoint_settlement_requires_run_and_passes_narrow_scope() -> None:
    connection = FakeConnection([])
    service = InspectionWorkItemService(
        repository=PostgresInspectionWorkItemRepository(pool=FakePool(connection)),
    )
    with pytest.raises(ValueError, match="必须指定 active_session_agent_run_id"):
        await service.settle_stalled_work_for_closure(
            task_id=TASK_ID,
            active_session_exhausted_only=True,
        )
    assert connection.calls == []
    await service.settle_stalled_work_for_closure(
        task_id=TASK_ID,
        active_session_agent_run_id=RUN_ID,
        active_session_exhausted_only=True,
    )
    query, args = next(call[1:] for call in connection.calls if "WITH approved_reuse" in call[1])
    assert args == (TASK_ID, RUN_ID, False, True, "[]")
    assert "lower(task.target_platform) = 'h5'" in query
    assert "work_item.attempt_count >= work_item.max_attempts" in query
    assert "work_item.lease_token IS NULL" in query
    assert "AND NOT $4::boolean" in query


async def test_service_rejects_blank_run_and_passes_it_through() -> None:
    class Repository:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def settle_stalled_work_for_closure(self, **kwargs):
            self.calls.append(kwargs)
            return InspectionWorkItemClosureSettlement(task_id=kwargs["task_id"])

    repository = Repository()
    service = InspectionWorkItemService(repository=repository)

    await service.settle_stalled_work_for_closure(task_id=TASK_ID)
    await service.settle_stalled_work_for_closure(
        task_id=TASK_ID, active_session_agent_run_id=RUN_ID
    )
    await service.settle_stalled_work_for_closure(task_id=TASK_ID, snapshot_generation_closed=True)

    assert repository.calls == [
        {
            "task_id": TASK_ID,
            "active_session_agent_run_id": None,
            "snapshot_generation_closed": False,
        },
        {
            "task_id": TASK_ID,
            "active_session_agent_run_id": RUN_ID,
            "snapshot_generation_closed": False,
        },
        {
            "task_id": TASK_ID,
            "active_session_agent_run_id": None,
            "snapshot_generation_closed": True,
        },
    ]
    with pytest.raises(ValueError):
        await service.settle_stalled_work_for_closure(
            task_id=TASK_ID, active_session_agent_run_id="  "
        )


# ---------------- 结算排在门禁之前 ----------------


async def test_gate_preparer_settles_stalled_work_before_publishing_aggregate_facts() -> None:
    events: list[str] = []

    class ActionExecutions:
        async def fail_nonterminal_for_finalization(self, **kwargs):
            events.append("actions")
            return 1

    class Settlement:
        async def settle_stalled_work_for_closure(self, **kwargs):
            events.append(f"settle:{kwargs['task_id']}")
            return InspectionWorkItemClosureSettlement(
                task_id=kwargs["task_id"],
                settled_work_item_count=2,
                settled_rule_count=5,
                reason_counts={"no_supplement_request": 2},
            )

    class AggregatePublication:
        async def prepare(self, **kwargs):
            events.append("aggregate")
            return {"published": 1}

    preparer = PostgresCompletionGatePreparer(
        action_executions=ActionExecutions(),
        aggregate_publication=AggregatePublication(),
        inspection_work_settlement=Settlement(),
    )

    result = await preparer.prepare(task_id=TASK_ID, agent_run_id=RUN_ID, finalization_epoch=4)

    assert result == {"published": 1}
    # 结算必须在门禁评估之前跑完，否则门禁永远等不到终态
    assert events == ["actions", f"settle:{TASK_ID}", "aggregate"]


async def test_gate_preparer_survives_settlement_failure() -> None:
    events: list[str] = []

    class ActionExecutions:
        async def fail_nonterminal_for_finalization(self, **kwargs):
            return 0

    class BrokenSettlement:
        async def settle_stalled_work_for_closure(self, **kwargs):
            raise RuntimeError("数据库不可用")

    class AggregatePublication:
        async def prepare(self, **kwargs):
            events.append("aggregate")
            return {"published": 0}

    preparer = PostgresCompletionGatePreparer(
        action_executions=ActionExecutions(),
        aggregate_publication=AggregatePublication(),
        inspection_work_settlement=BrokenSettlement(),
    )

    result = await preparer.prepare(task_id=TASK_ID, agent_run_id=RUN_ID, finalization_epoch=1)

    # 结算失败只告警，门禁前置照常往下走
    assert result == {"published": 0}
    assert events == ["aggregate"]


# ---------------- 排空窗口用尽 ----------------


class _Publisher:
    async def publish_pending(self, **kwargs) -> int:
        return 0


class _Consumer:
    async def drain_available(self, **kwargs) -> None:
        return None


class _StateReader:
    def __init__(self, state: H5ActiveSessionDrainState) -> None:
        self.state = state

    async def load_drain_state(self, **kwargs) -> H5ActiveSessionDrainState:
        return self.state


def _stuck_service(settlement: Any | None) -> H5ActiveSessionInspectionDrainService:
    clock = {"now": NOW}

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    return H5ActiveSessionInspectionDrainService(
        publisher=_Publisher(),
        consumer=_Consumer(),
        state_reader=_StateReader(
            H5ActiveSessionDrainState(pending_capture_count=1, nonterminal_work_item_count=2)
        ),
        grace_seconds=0.1,
        max_wait_seconds=0.3,
        max_poll_seconds=0.05,
        now=lambda: clock["now"],
        sleep=sleep,
        closure_settlement=settlement,
    )


async def test_drain_settles_and_closes_instead_of_raising_when_grace_runs_out() -> None:
    calls: list[dict[str, Any]] = []

    class Settlement:
        async def settle_stalled_work_for_closure(self, **kwargs):
            calls.append(kwargs)
            return InspectionWorkItemClosureSettlement(
                task_id=kwargs["task_id"],
                settled_work_item_count=2,
                settled_rule_count=4,
                reason_counts={"active_session_closing": 2},
            )

    summary = await _stuck_service(Settlement()).drain_before_close(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        deadline=NOW + timedelta(minutes=5),
    )

    assert summary.settled_work_item_count == 2
    # 结算范围锁定到本 Run 的活会话工作项
    assert calls == [{"task_id": TASK_ID, "active_session_agent_run_id": RUN_ID}]


async def test_drain_still_raises_when_no_settlement_is_wired() -> None:
    with pytest.raises(RuntimeError, match="active-session 规则未排空"):
        await _stuck_service(None).drain_before_close(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            deadline=NOW + timedelta(minutes=5),
        )


class _AppStateReader:
    def __init__(self, state: AppActiveSessionDrainState) -> None:
        self.state = state

    async def load_drain_state(self, **kwargs) -> AppActiveSessionDrainState:
        return self.state


def _stuck_app_service(settlement: Any | None) -> AppActiveSessionInspectionDrainService:
    clock = {"now": NOW}

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    return AppActiveSessionInspectionDrainService(
        publisher=_Publisher(),
        consumer=_Consumer(),
        state_reader=_AppStateReader(
            AppActiveSessionDrainState(pending_capture_count=1, nonterminal_work_item_count=2)
        ),
        grace_seconds=0.1,
        max_wait_seconds=0.3,
        max_poll_seconds=0.05,
        now=lambda: clock["now"],
        sleep=sleep,
        closure_settlement=settlement,
    )


async def test_app_drain_settles_so_device_lease_release_is_still_reached() -> None:
    """App 排空抛错会让 CloseAppExploration 中断，设备租约结算根本执行不到。"""

    calls: list[dict[str, Any]] = []

    class Settlement:
        async def settle_stalled_work_for_closure(self, **kwargs):
            calls.append(kwargs)
            return InspectionWorkItemClosureSettlement(
                task_id=kwargs["task_id"],
                settled_work_item_count=1,
                settled_rule_count=3,
                reason_counts={"active_session_closing": 1},
            )

    summary = await _stuck_app_service(Settlement()).drain_before_close(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        deadline=NOW + timedelta(minutes=5),
    )

    assert summary.settled_work_item_count == 1
    assert calls == [{"task_id": TASK_ID, "active_session_agent_run_id": RUN_ID}]


async def test_app_drain_still_raises_when_no_settlement_is_wired() -> None:
    with pytest.raises(RuntimeError, match="active-session 规则未排空"):
        await _stuck_app_service(None).drain_before_close(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            deadline=NOW + timedelta(minutes=5),
        )


# ---------------- Web 排空轮次用尽 ----------------


def test_web_drainer_settles_instead_of_raising_so_browser_gets_closed() -> None:
    """Web 抛 WebActiveSessionWorkPendingError 会被 Worker 吞掉重试，浏览器永不释放。"""

    import inspect

    from services.api.app import worker

    source = inspect.getsource(worker._WebActiveSessionInspectionDrainer)
    # 没有结算能力时保持原样抛错，接了结算就结算后继续往下关会话
    assert "if self._closure_settlement is None:" in source
    assert "raise WebActiveSessionWorkPendingError(" in source
    assert "settle_stalled_work_for_closure(" in source
    assert "active_session_agent_run_id=agent_run_id," in source

    factory = inspect.getsource(worker._build_default_web_formal_inspection_dependencies)
    assert "closure_settlement=web_inspection_work_service," in factory


# ---------------- 证据集合封存的阶段口径 ----------------


def test_evidence_input_set_accepts_review_while_session_is_draining() -> None:
    repository = PostgresEvidenceInputSetRepository(pool=object())

    # 探索 Run 失败后任务转 review，收口仍在 session_draining，聚合输入集合必须还能封存
    assert repository._permitted_task_statuses == ("running", "review")
    assert repository._permitted_finalization_states == ("session_draining",)
