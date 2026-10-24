"""App 终态动作结算在 Runtime Recovery 读侧的兼容与拒绝测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from services.api.app.agents.runtime.models import ExecutionOutcome, TerminationReason
from services.api.app.application.app_exploration_runtime_recovery import (
    AppExplorationRuntimeRecoveryError,
)
from services.api.app.application.app_runtime_composition import (
    ProductionAppV2RuntimeExecutor,
    _validate_app_exploration_work_recovery,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.core.config import Settings
from services.api.app.domain.evidence import AppExplorationResultOutcome
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
)
from services.api.app.infrastructure.db.app_exploration_runtime_recovery_repository import (
    PostgresAppExplorationRuntimeRecoveryRepository,
)
from services.api.tests.test_app_exploration_runtime_recovery_repository import (
    FakeConnection,
    FakePool,
    _work,
    _work_row,
)
from services.api.tests.test_app_runtime_composition import (
    _app_record,
    _recovery_checkpoint,
    _recovery_handoff,
    _recovery_work,
)


def _terminal(work, *, coverage_summary: dict[str, Any]) -> dict[str, Any]:
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
        reason="deadline 已到，停止后继提交",
        outcome="evidence_insufficient",
        reason_code="deadline_reached",
        coverage_summary=coverage_summary,
    ).to_dict()


async def _recover(
    *,
    work,
    terminal_payload: dict[str, Any] | None,
    initial_work=None,
):
    work_rows = []
    if initial_work is not None:
        work_rows.append(
            _work_row(
                initial_work,
                status=ExplorerWorkStatus.SUCCEEDED,
            )
        )
    work_rows.append(
        _work_row(
            work,
            status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
            terminal_payload=terminal_payload,
        )
    )
    repository = PostgresAppExplorationRuntimeRecoveryRepository(
        pool=FakePool(
            FakeConnection(
                work_rows=work_rows,
            )
        )
    )
    return await repository.get_work_recovery(
        task_id=work.task_id,
        agent_run_id="run-1",
        surface_session_id=work.session_id,
    )


@pytest.mark.asyncio
async def test_terminal_count_and_result_capture_are_recovered() -> None:
    # max_actions=3、末端剩余 3 表示父 Work 尚未把本轮动作反映到预算；
    # 终态显式计数 1 必须由恢复读侧保留，不能被预算回退推导掩盖。
    work = _work(remaining_actions=3)
    recovery = await _recover(
        work=work,
        terminal_payload=_terminal(
            work,
            coverage_summary={
                "consumed_action_count": 1,
                "result_capture_id": "capture-after",
            },
        ),
    )

    assert recovery.consumed_action_count == 1
    assert recovery.terminal is not None
    assert recovery.terminal.coverage_summary["consumed_action_count"] == 1
    assert recovery.terminal.coverage_summary["result_capture_id"] == "capture-after"


@pytest.mark.asyncio
async def test_legacy_terminal_without_count_keeps_budget_derived_fallback() -> None:
    work = _work(remaining_actions=1)

    recovery = await _recover(
        work=work,
        terminal_payload=_terminal(work, coverage_summary={"legacy": True}),
    )

    # 3 - 1 仍按旧 terminal payload 合同推导，不能把缺字段当作 0。
    assert recovery.consumed_action_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remaining_actions", "settled_count"),
    ((1, True), (1, False), (1, None), (1, "2"), (1, 1), (1, 4), (3, 2)),
    ids=(
        "bool_true", "bool_false", "null", "string", "below_budget",
        "above_maximum", "multiple_actions_within_maximum",
    ),
)
async def test_invalid_terminal_count_is_rejected(
    remaining_actions: int, settled_count: object,
) -> None:
    work = _work(remaining_actions=remaining_actions)

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="终态动作结算"):
        await _recover(
            work=work,
            terminal_payload=_terminal(
                work,
                coverage_summary={"consumed_action_count": settled_count},
            ),
        )


def _composition_recovery_facts(
    *,
    remaining_actions: int | None = None,
    branch_id: str = "app:capture-app",
    capture_id: str = "capture-app",
    started_at: datetime | None = None,
    deadline: datetime | None = None,
):
    started_at = started_at or datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = deadline or (started_at + timedelta(minutes=15))
    checkpoint, policy, objective = _recovery_checkpoint(
        started_at=started_at,
        deadline=deadline,
    )
    if remaining_actions is None:
        remaining_actions = policy.max_actions
    work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=remaining_actions,
        remaining_duration_ms=policy.duration_ms,
        branch_id=branch_id,
        capture_id=capture_id,
    )
    return checkpoint, policy, objective, work


def _validate_composition_recovery(
    *,
    recovery,
    checkpoint,
    policy,
    objective,
) -> None:
    _validate_app_exploration_work_recovery(
        recovery=recovery,
        checkpoint=checkpoint,
        handoff=_recovery_handoff(),
        objective=objective,
        installed_package="com.example.installed",
        runtime_policy=policy,
    )


def test_work_recovery_rejects_visual_delayed_observed_at() -> None:
    """首 Work 若写入视觉延时 observed_at，恢复校验必须拒绝并点名字段。"""

    started_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = started_at + timedelta(minutes=15)
    checkpoint, policy, objective, work = _composition_recovery_facts(
        started_at=started_at,
        deadline=deadline,
    )
    delayed = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                observed_at=started_at + timedelta(seconds=15),
            ),
        ),
    )
    recovery = SimpleNamespace(
        initial_work=delayed,
        latest_work=delayed,
        terminal=None,
        terminal_status=None,
        terminal_error_code=None,
        consumed_action_count=0,
        consumed_turn_count=0,
        has_any_work=True,
        has_nonterminal_work=True,
        next_claim_at=None,
    )

    with pytest.raises(RuntimeError, match="observed_at"):
        _validate_composition_recovery(
            recovery=recovery,
            checkpoint=checkpoint,
            policy=policy,
            objective=objective,
        )


@pytest.mark.asyncio
async def test_runtime_executor_restores_terminal_count_without_republish_or_turn() -> None:
    """进程重启后沿用 terminal count，不能重新领取或执行动作。"""

    # 以实际 Runtime Policy 的 max_actions 构造父 Work，避免测试猜测策略默认值。
    checkpoint, policy, objective, work = _composition_recovery_facts()
    recovery = await _recover(
        work=work,
        terminal_payload=_terminal(
            work,
            coverage_summary={"consumed_action_count": 1},
        ),
    )

    class Runtime:
        bootstrap_calls = 0
        publish_calls = 0
        turn_calls = 0
        close_commands = []

        async def get_persisted_exploration_result(self, *, agent_run_id):
            assert agent_run_id == "run-app"
            return None

        async def bootstrap(self, command):
            self.bootstrap_calls += 1
            return _recovery_handoff()

        async def get_exploration_runtime_checkpoint(self, *, agent_run_id):
            assert agent_run_id == "run-app"
            return checkpoint

        async def create_exploration_runtime_checkpoint(self, *, checkpoint):
            raise AssertionError("已有 Checkpoint 时不得重新创建")

        async def get_exploration_work_recovery(self, **kwargs):
            assert kwargs["task_id"] == "task-app"
            assert kwargs["agent_run_id"] == "run-app"
            return recovery

        async def publish_exploration(self, command):
            self.publish_calls += 1
            raise AssertionError("已有终态 Work 时不得重复发布")

        async def run_one_turn(self, **kwargs):
            self.turn_calls += 1
            raise AssertionError("已有终态 Work 时不得再次领取或执行")

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(
                close_error=None,
                exploration_result=SimpleNamespace(
                    outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                    terminal_reason_code="deadline_reached",
                ),
            )

        async def mark_exploration_lost(self, **kwargs):
            raise AssertionError(f"终态恢复不应标记 LOST：{kwargs}")

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        now=lambda: datetime(2026, 8, 14, 8, 5, tzinfo=UTC),
    ).run(runtime, record=_app_record(), settings=Settings())

    assert runtime.bootstrap_calls == 1
    assert runtime.publish_calls == 0
    assert runtime.turn_calls == 0
    assert len(runtime.close_commands) == 1
    coverage = runtime.close_commands[0].coverage_summary
    assert coverage["consumed_action_count"] == 1
    assert coverage["terminal_status"] == ExplorerWorkStatus.BUDGET_EXHAUSTED.value
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_composition_validator_rejects_terminal_count_mismatch() -> None:
    checkpoint, policy, objective, work = _composition_recovery_facts()
    recovery = await _recover(
        work=work,
        terminal_payload=_terminal(work, coverage_summary={"consumed_action_count": 1}),
    )

    with pytest.raises(RuntimeError, match="预算消费"):
        _validate_composition_recovery(
            recovery=replace(recovery, consumed_action_count=2),
            checkpoint=checkpoint,
            policy=policy,
            objective=objective,
        )


@pytest.mark.asyncio
async def test_composition_validator_rejects_budget_rollback() -> None:
    checkpoint, policy, objective, work = _composition_recovery_facts()
    recovery = await _recover(
        work=work,
        terminal_payload=_terminal(work, coverage_summary={"legacy": True}),
    )
    latest = replace(
        recovery.latest_work,
        budget=replace(
            recovery.latest_work.budget,
            remaining_actions=policy.max_actions - 1,
            remaining_business_actions=policy.max_actions - 1,
        ),
    )
    rolled_back = replace(
        recovery,
        latest_work=latest,
        consumed_action_count=0,
    )

    with pytest.raises(RuntimeError, match="预算消费"):
        _validate_composition_recovery(
            recovery=rolled_back,
            checkpoint=checkpoint,
            policy=policy,
            objective=objective,
        )


@pytest.mark.asyncio
async def test_composition_validator_rejects_more_than_one_unaccounted_action() -> None:
    checkpoint, policy, objective, work = _composition_recovery_facts()
    recovery = await _recover(
        work=work,
        terminal_payload=_terminal(work, coverage_summary={"consumed_action_count": 1}),
    )
    terminal = replace(
        recovery.terminal,
        coverage_summary={"consumed_action_count": 2},
    )

    with pytest.raises(RuntimeError, match="预算消费"):
        _validate_composition_recovery(
            recovery=replace(
                recovery,
                terminal=terminal,
                consumed_action_count=2,
            ),
            checkpoint=checkpoint,
            policy=policy,
            objective=objective,
        )


@pytest.mark.asyncio
async def test_legacy_terminal_count_remains_strictly_budget_derived() -> None:
    checkpoint, policy, objective, initial = _composition_recovery_facts()
    _latest_checkpoint, _latest_policy, _latest_objective, work = (
        _composition_recovery_facts(
            remaining_actions=policy.max_actions - 1,
            branch_id="app:capture-app-latest",
            capture_id="capture-app-latest",
        )
    )
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "parent_work_id": initial.work_id,
                },
            ),
        ),
    )
    recovery = await _recover(
        work=work,
        terminal_payload=_terminal(work, coverage_summary={"legacy": True}),
        initial_work=initial,
    )

    _validate_composition_recovery(
        recovery=recovery,
        checkpoint=checkpoint,
        policy=policy,
        objective=objective,
    )
    with pytest.raises(RuntimeError, match="预算消费"):
        _validate_composition_recovery(
            recovery=replace(recovery, consumed_action_count=0),
            checkpoint=checkpoint,
            policy=policy,
            objective=objective,
        )
