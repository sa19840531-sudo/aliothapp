"""Explorer 一轮决策应用接线测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.explorer_turn import ExplorerTurnExecutor
from services.api.app.domain.exploration import (
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
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
)


class StaticExplorer:
    def __init__(self, decision) -> None:
        self.decision = decision

    async def decide_next(self, work):
        return self.decision


class DeterministicAwareExplorer:
    def __init__(self, decision) -> None:
        self.decision = decision
        self.deterministic_calls = 0
        self.fallback_calls = 0
        self.regular_calls = 0

    async def decide_deterministic(self, work):
        del work
        self.deterministic_calls += 1
        return None

    async def decide_after_deterministic(self, work):
        del work
        self.fallback_calls += 1
        return self.decision

    async def decide_next(self, work):
        del work
        self.regular_calls += 1
        raise AssertionError("阶段检查后的兜底不得再次走普通装饰器入口")


class RecordingCoordinator:
    def __init__(self, result=None) -> None:
        self.result = result
        self.commands = []

    async def coordinate(self, command):
        self.commands.append(command)
        return self.result

    async def capture_observation(self, *, agent_run_id, intent):
        self.commands.append((agent_run_id, intent))
        return CaptureRef(
            task_id=intent.task_id,
            session_id=intent.session_id,
            capture_id="capture-observation",
            surface_kind=intent.surface_kind,
            capture_kind=CaptureKind.APP,
            role="observation",
            artifact_ids=("artifact-observation",),
            parent_capture_id=intent.source_capture_id,
            action_execution_id=None,
        )


def _work(*, surface_kind: SurfaceKind = SurfaceKind.WEB):
    observed_at = datetime.now(UTC)
    frontier = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=SurfaceObservation(
            capture_id="capture-1",
            surface_kind=surface_kind,
            location="https://example.com/",
            state_version="state-1",
            observed_at=observed_at,
        ),
        candidates=(),
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-1",
        objective="检查登录入口",
        surface_kind=surface_kind,
        source_capture_id="capture-1",
        frontier_snapshot_id="frontier-1",
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_operations=(InteractionOperation.CLICK,),
            allowed_hosts=("example.com",),
            allowed_route_prefixes=("/",),
            max_navigation_depth=2,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=2,
            remaining_actions=2,
            max_depth=2,
            remaining_duration_ms=30_000,
        ),
        deadline=observed_at + timedelta(minutes=1),
    )


def _intent(work) -> ActionIntent:
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-1",
        idempotency_key="task-1:branch-1:intent-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="登录按钮",
        purpose="检查登录入口",
        parameters={"navigation_depth": 1},
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfile(
            profile_id="base",
            before=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot", "dom", "visible_text", "ui_snapshot"),
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=("screenshot", "dom", "visible_text", "ui_snapshot"),
            ),
        ),
        deadline=work.deadline,
    )


async def test_explorer_turn_executes_action_through_capture_coordinator() -> None:
    work = _work()
    intent = _intent(work)
    coordinator = RecordingCoordinator(result="coordinated")
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(intent),
        capture_coordinator=coordinator,
    )

    result = await executor.execute(work=work, agent_run_id="run-1")

    assert result.decision is intent
    assert result.coordination == "coordinated"
    assert not result.is_terminal
    assert coordinator.commands[0].agent_run_id == "run-1"
    assert coordinator.commands[0].intent is intent


async def test_explorer_turn_does_not_repeat_phase_check_before_model_fallback() -> None:
    work = _work()
    explorer = DeterministicAwareExplorer(_intent(work))
    executor = ExplorerTurnExecutor(
        explorer=explorer,
        capture_coordinator=RecordingCoordinator(),
    )

    deterministic = await executor.decide_deterministic(work=work)
    fallback = await executor.decide_after_deterministic(work=work)

    assert deterministic is None
    assert isinstance(fallback, ActionIntent)
    assert explorer.deterministic_calls == 1
    assert explorer.fallback_calls == 1
    assert explorer.regular_calls == 0


async def test_before_side_effect_hook_can_stop_action_after_decision() -> None:
    """若遗漏钩子，决策后的取消会越过 before/action/after 审计边界。"""

    work = _work()
    intent = _intent(work)
    coordinator = RecordingCoordinator(result="coordinated")
    seen: list[tuple[object, str, object]] = []

    async def stop_side_effect(*, work, agent_run_id, decision) -> None:
        seen.append((work, agent_run_id, decision))
        raise RuntimeError("cancelled-after-decision")

    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(intent),
        capture_coordinator=coordinator,
        before_side_effect=stop_side_effect,
    )

    with pytest.raises(RuntimeError, match="cancelled-after-decision"):
        await executor.execute(work=work, agent_run_id="run-1")

    assert seen == [(work, "run-1", intent)]
    assert coordinator.commands == []


async def test_explorer_turn_returns_terminal_without_touching_surface() -> None:
    work = _work()
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="没有新的候选。",
    )
    coordinator = RecordingCoordinator()
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(terminal),
        capture_coordinator=coordinator,
    )

    result = await executor.execute(work=work, agent_run_id="run-1")

    assert result.is_terminal
    assert result.coordination is None
    assert coordinator.commands == []


async def test_explorer_turn_captures_timed_observation_without_action_coordination() -> None:
    work = _work(surface_kind=SurfaceKind.ANDROID)
    observation = TimedObservationIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        phase_run_id="phase-1",
        decision_id="decision-1",
        observation_id="observation-1",
        source_capture_id=work.source_capture_id,
        surface_kind=SurfaceKind.ANDROID,
        delay_ms=0,
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot", "ui_tree"),
            inherit_parent_semantics=True,
        ),
        deadline=work.deadline,
    )
    coordinator = RecordingCoordinator()
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(observation),
        capture_coordinator=coordinator,
    )

    result = await executor.execute(work=work, agent_run_id="run-1")

    assert result.decision is observation
    assert result.coordination is None
    assert result.observation_capture is not None
    assert result.observation_capture.role == "observation"
    assert result.observation_capture.parent_capture_id == work.source_capture_id
    assert result.observation_capture.action_execution_id is None
    assert coordinator.commands == [("run-1", observation)]


async def test_explorer_turn_rejects_cross_work_identity() -> None:
    work = _work()
    invalid = replace(_intent(work), session_id="session-other")
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(invalid),
        capture_coordinator=RecordingCoordinator(),
    )

    with pytest.raises(ValueError, match="session_id"):
        await executor.execute(work=work, agent_run_id="run-1")


async def test_explorer_turn_rejects_action_from_another_surface() -> None:
    work = _work(surface_kind=SurfaceKind.ANDROID)
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(_intent(work)),
        capture_coordinator=RecordingCoordinator(),
    )

    with pytest.raises(ValueError, match="ActionIntent surface_kind"):
        await executor.execute(work=work, agent_run_id="run-1")


async def test_explorer_turn_rejects_timed_observation_from_another_surface() -> None:
    work = _work(surface_kind=SurfaceKind.ANDROID)
    observation = TimedObservationIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        phase_run_id="phase-1",
        decision_id="decision-1",
        observation_id="observation-1",
        source_capture_id=work.source_capture_id,
        surface_kind=SurfaceKind.IOS,
        delay_ms=0,
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot", "dom"),
            inherit_parent_semantics=True,
        ),
        deadline=work.deadline,
    )
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(observation),
        capture_coordinator=RecordingCoordinator(),
    )

    with pytest.raises(ValueError, match="TimedObservationIntent surface_kind"):
        await executor.execute(work=work, agent_run_id="run-1")
