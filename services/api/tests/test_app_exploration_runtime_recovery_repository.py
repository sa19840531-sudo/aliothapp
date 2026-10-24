"""App 探索 Runtime Checkpoint 与 Work 时间线仓储测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.app_exploration_runtime_recovery import (
    AppExplorationRuntimeRecoveryError,
    build_app_exploration_runtime_checkpoint,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.db.app_exploration_runtime_recovery_repository import (
    PostgresAppExplorationRuntimeRecoveryRepository,
)

NOW = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


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


class FakeConnection:
    """按 SQL 关键片段返回预设行，模拟 asyncpg 的最小接口。"""

    def __init__(
        self,
        *,
        checkpoint_row: dict[str, Any] | None = None,
        insert_row: dict[str, Any] | None = None,
        work_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.checkpoint_row = checkpoint_row
        self.insert_row = insert_row
        self.work_rows = work_rows or []
        self.queries: list[str] = []
        self.arguments: list[tuple[Any, ...]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        self.arguments.append(args)
        if "INSERT INTO qc_app_exploration_runtime_checkpoint" in normalized:
            return self.insert_row
        if "FROM qc_app_exploration_runtime_checkpoint" in normalized:
            return self.checkpoint_row
        raise AssertionError(f"未处理 fetchrow SQL：{normalized}")

    async def fetch(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        self.arguments.append(args)
        if "FROM qc_v2_work_item" in normalized:
            return self.work_rows
        raise AssertionError(f"未处理 fetch SQL：{normalized}")


def _policy() -> dict[str, object]:
    return {"scan_mode": "full", "max_actions": 3, "nested": {"enabled": True}}


def _checkpoint():
    return build_app_exploration_runtime_checkpoint(
        task_id="task-1",
        agent_run_id="run-1",
        surface_session_id="app:task-1",
        platform_build_ref="app-build:one",
        bootstrap_capture_id="capture-bootstrap",
        installed_package="com.example.target",
        runtime_policy=_policy(),
        objective="覆盖关键页面",
        started_at=NOW,
        deadline=NOW + timedelta(minutes=30),
        created_at=NOW,
    )


def _work(
    *,
    task_id: str = "task-1",
    session_id: str = "app:task-1",
    branch_id: str = "app:capture-source",
    capture_id: str = "capture-source",
    remaining_actions: int = 3,
    remaining_duration_ms: int = 1_800_000,
    deadline: datetime = NOW + timedelta(minutes=30),
    parent_work_id: str | None = None,
):
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier-{capture_id}",
        observation=SurfaceObservation(
            capture_id=capture_id,
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.target/.MainActivity",
            state_version="sha256:" + "a" * 64,
            observed_at=NOW,
            page_state="stable",
            metadata=(
                {"parent_work_id": parent_work_id}
                if parent_work_id is not None
                else {}
            ),
        ),
        candidates=(
            CandidateAction(
                candidate_id=f"candidate-{capture_id}",
                semantic_target="关键入口",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=90,
                navigation_depth=1,
                expected_surface_kind=SurfaceKind.ANDROID,
            ),
        ),
    )
    return build_exploration_work(
        task_id=task_id,
        session_id=session_id,
        branch_id=branch_id,
        objective="覆盖关键页面",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.target",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=remaining_actions,
            max_depth=3,
            remaining_duration_ms=remaining_duration_ms,
        ),
        deadline=deadline,
    )


def _checkpoint_row(checkpoint) -> dict[str, Any]:
    return {
        "task_id": checkpoint.task_id,
        "agent_run_id": checkpoint.agent_run_id,
        "surface_session_id": checkpoint.surface_session_id,
        "platform_build_ref": checkpoint.platform_build_ref,
        "bootstrap_capture_id": checkpoint.bootstrap_capture_id,
        "installed_package": checkpoint.installed_package,
        "protocol_version": checkpoint.protocol_version,
        "runtime_policy": dict(checkpoint.runtime_policy),
        "runtime_policy_sha256": checkpoint.runtime_policy_sha256,
        "objective_hash": checkpoint.objective_hash,
        "started_at": checkpoint.started_at,
        "deadline": checkpoint.deadline,
        "created_at": checkpoint.created_at,
    }


def _work_row(
    work,
    *,
    status: ExplorerWorkStatus,
    terminal_payload: dict[str, Any] | None = None,
    last_error_code: str | None = None,
    next_attempt_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "work_id": work.work_id,
        "work_payload": work.to_dict(),
        "status": status.value,
        "terminal_payload": terminal_payload,
        "last_error_code": last_error_code,
        "next_attempt_at": next_attempt_at,
        "started_at": NOW,
        "finished_at": NOW,
        "created_at": NOW,
        "lease_expires_at": lease_expires_at,
        "row_version": 1,
    }


@pytest.mark.asyncio
async def test_checkpoint_create_and_get_use_the_same_immutable_row() -> None:
    checkpoint = _checkpoint()
    connection = FakeConnection(
        checkpoint_row=_checkpoint_row(checkpoint),
        insert_row=_checkpoint_row(checkpoint),
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    created = await repository.create_checkpoint(checkpoint=checkpoint)
    loaded = await repository.get_checkpoint(agent_run_id=checkpoint.agent_run_id)

    assert created == checkpoint
    assert loaded == checkpoint
    assert any(
        "ON CONFLICT (agent_run_id)" in query and "runtime_policy_sha256" in query
        for query in connection.queries
    )
    assert connection.arguments[0][7].startswith("{")


@pytest.mark.asyncio
async def test_checkpoint_identity_conflict_fails_closed() -> None:
    checkpoint = _checkpoint()
    connection = FakeConnection(insert_row=None)
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="不同"):
        await repository.create_checkpoint(checkpoint=checkpoint)


@pytest.mark.asyncio
async def test_work_timeline_returns_initial_latest_budget_deadline_and_retry_time() -> None:
    initial = _work()
    latest = _work(
        branch_id="app:capture-latest",
        capture_id="capture-latest",
        remaining_actions=1,
        remaining_duration_ms=900_000,
        parent_work_id=initial.work_id,
    )
    retry_at = NOW + timedelta(seconds=20)
    connection = FakeConnection(
        work_rows=[
            _work_row(initial, status=ExplorerWorkStatus.SUCCEEDED),
            _work_row(
                latest,
                status=ExplorerWorkStatus.RETRY_WAIT,
                next_attempt_at=retry_at,
            ),
        ]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    recovery = await repository.get_work_recovery(
        task_id="task-1",
        agent_run_id="run-1",
        surface_session_id="app:task-1",
    )

    assert recovery.initial_work == initial
    assert recovery.latest_work == latest
    assert recovery.initial_work.budget.remaining_actions == 3
    assert recovery.latest_work.budget.remaining_actions == 1
    assert recovery.initial_work.deadline == latest.deadline
    assert recovery.next_claim_at == retry_at
    assert recovery.consumed_turn_count == 1
    assert recovery.consumed_action_count == 2
    assert recovery.has_nonterminal_work is True


@pytest.mark.asyncio
async def test_work_timeline_reads_active_lease_expiry_from_postgres() -> None:
    work = _work()
    lease_expires_at = NOW + timedelta(minutes=2)
    connection = FakeConnection(
        work_rows=[
            _work_row(
                work,
                status=ExplorerWorkStatus.RUNNING,
                lease_expires_at=lease_expires_at,
            )
        ]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    recovery = await repository.get_work_recovery(
        task_id="task-1",
        agent_run_id="run-1",
        surface_session_id="app:task-1",
    )

    assert recovery.has_nonterminal_work is True
    assert recovery.next_claim_at == lease_expires_at


@pytest.mark.asyncio
async def test_terminal_payload_is_recovered_without_synthetic_replacement() -> None:
    work = _work(remaining_actions=1, remaining_duration_ms=900_000)
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="已完成",
        outcome="exploration_completed",
        reason_code="exploration_completed",
        coverage_summary={"covered": 2},
    )
    connection = FakeConnection(
        work_rows=[
            _work_row(
                work,
                status=ExplorerWorkStatus.SUCCEEDED,
                terminal_payload=terminal.to_dict(),
            )
        ]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    recovery = await repository.get_work_recovery(
        task_id="task-1",
        agent_run_id="run-1",
        surface_session_id="app:task-1",
    )

    assert recovery.terminal == terminal
    assert recovery.terminal_status is ExplorerWorkStatus.SUCCEEDED
    assert recovery.terminal_error_code is None
    assert recovery.has_nonterminal_work is False


@pytest.mark.asyncio
async def test_terminal_payload_status_mismatch_is_rejected() -> None:
    """读侧必须拒绝 payload completed、行状态 failed 这类分裂终态。"""

    work = _work(remaining_actions=1, remaining_duration_ms=900_000)
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="payload 声称完成",
        outcome="exploration_completed",
        reason_code="exploration_completed",
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(
            FakeConnection(
                work_rows=[
                    _work_row(
                        work,
                        status=ExplorerWorkStatus.FAILED,
                        terminal_payload=terminal.to_dict(),
                    )
                ]
            )
        )
    )

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="状态不一致"):
        await repository.get_work_recovery(
            task_id="task-1",
            agent_run_id="run-1",
            surface_session_id="app:task-1",
        )


@pytest.mark.asyncio
async def test_terminal_without_payload_is_synthetic_fail_closed() -> None:
    work = _work(remaining_actions=0, remaining_duration_ms=0)
    connection = FakeConnection(
        work_rows=[
            _work_row(
                work,
                status=ExplorerWorkStatus.FAILED,
                last_error_code="driver_crash",
            )
        ]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    recovery = await repository.get_work_recovery(
        task_id="task-1",
        agent_run_id="run-1",
        surface_session_id="app:task-1",
    )

    assert recovery.terminal is not None
    assert recovery.terminal.outcome == "technical_failed"
    assert recovery.terminal.reason_code == "driver_crash"
    assert recovery.terminal.coverage_summary["terminal_payload_missing"] is True
    assert recovery.terminal_status is ExplorerWorkStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        (
            "EXPLORER_WORK_DEADLINE_EXCEEDED",
            ExplorationTerminalStatus.BUDGET_EXHAUSTED,
        ),
        (
            "EXPLORER_WORK_ATTEMPTS_EXHAUSTED",
            ExplorationTerminalStatus.BLOCKED,
        ),
    ],
)
async def test_expired_or_exhausted_work_without_payload_seals_as_insufficient(
    error_code: str,
    expected_status: ExplorationTerminalStatus,
) -> None:
    """PG 明确耗尽的 Work 是有界业务终态，不能伪装成 Driver 技术故障。"""

    work = _work(remaining_actions=0, remaining_duration_ms=0)
    connection = FakeConnection(
        work_rows=[
            _work_row(
                work,
                status=ExplorerWorkStatus.FAILED,
                last_error_code=error_code,
            )
        ]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    recovery = await repository.get_work_recovery(
        task_id="task-1",
        agent_run_id="run-1",
        surface_session_id="app:task-1",
    )

    assert recovery.terminal is not None
    assert recovery.terminal.status is expected_status
    assert recovery.terminal.outcome == "evidence_insufficient"
    assert recovery.terminal.reason_code == error_code


@pytest.mark.asyncio
async def test_work_ownership_drift_and_terminal_ownership_drift_are_rejected() -> None:
    foreign_work = _work(task_id="task-other", session_id="app:task-other")
    connection = FakeConnection(
        work_rows=[_work_row(foreign_work, status=ExplorerWorkStatus.PENDING)]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )
    with pytest.raises(AppExplorationRuntimeRecoveryError, match="归属"):
        await repository.get_work_recovery(
            task_id="task-1",
            agent_run_id="run-1",
            surface_session_id="app:task-1",
        )

    work = _work()
    foreign_terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id="task-other",
        status=ExplorationTerminalStatus.COMPLETED,
        reason="归属漂移",
        outcome="exploration_completed",
        reason_code="exploration_completed",
    )
    connection = FakeConnection(
        work_rows=[
            _work_row(
                work,
                status=ExplorerWorkStatus.SUCCEEDED,
                terminal_payload=foreign_terminal.to_dict(),
            )
        ]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )
    with pytest.raises(AppExplorationRuntimeRecoveryError, match="归属"):
        await repository.get_work_recovery(
            task_id="task-1",
            agent_run_id="run-1",
            surface_session_id="app:task-1",
        )


@pytest.mark.asyncio
async def test_multiple_terminal_rows_are_rejected_as_ambiguous_timeline() -> None:
    first = _work()
    second = _work(branch_id="app:capture-second", capture_id="capture-second")
    connection = FakeConnection(
        work_rows=[
            _work_row(
                first,
                status=ExplorerWorkStatus.SUCCEEDED,
                terminal_payload=ExplorationTerminal(
                    work_id=first.work_id,
                    task_id=first.task_id,
                    status=ExplorationTerminalStatus.COMPLETED,
                    reason="第一个终态",
                    outcome="exploration_completed",
                    reason_code="exploration_completed",
                ).to_dict(),
            ),
            _work_row(
                second,
                status=ExplorerWorkStatus.BOUNDED,
                terminal_payload=ExplorationTerminal(
                    work_id=second.work_id,
                    task_id=second.task_id,
                    status=ExplorationTerminalStatus.BOUNDED,
                    reason="第二个终态",
                    outcome="evidence_insufficient",
                    reason_code="bounded",
                ).to_dict(),
            ),
        ]
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(connection)
    )

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="多个"):
        await repository.get_work_recovery(
            task_id="task-1",
            agent_run_id="run-1",
            surface_session_id="app:task-1",
        )
