"""验证实际选路与派发分支不能绕过有界调度。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.application.app_candidate_eligibility import eligible_app_candidates
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationState,
)
from services.api.app.application.explorer_turn import ExplorerTurnExecutor
from services.api.app.application.h5_exploration_workflow import _select_next_task_entry
from services.api.app.domain.exploration import (
    H5_TASK_FRONTIER_METADATA_KEY,
    AppExplorationGoal,
    CandidateAction,
    ExplorationTerminal,
)
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
)
from services.api.tests.support.fake_chat_client import ScriptedChatClient
from services.api.tests.test_explorer_turn import RecordingCoordinator, StaticExplorer, _intent
from services.api.tests.test_maf_explorer_agent import _work


def _scheduler_work(*, surface_kind=SurfaceKind.ANDROID, **state_updates):
    now = datetime.now(UTC)
    candidate = CandidateAction(
        candidate_id="settings-entry",
        semantic_target="打开设置",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        metadata={"expected_next_page_type": "settings", "frontier_scope": "local"},
    )
    state = BoundedExplorationState(
        max_actions=20,
        max_duration_ms=60_000,
        required_page_types=("settings",),
        **state_updates,
    )
    work = _work(
        candidates=(candidate,),
        surface_kind=surface_kind,
        allowed_operations=(InteractionOperation.CLICK,),
        app_goal=(
            AppExplorationGoal(
                required_page_types=("home", "settings"),
                required_milestones=("surface:stable_business_page",),
                minimum_business_pages=2,
                allowed_packages=("com.example.target",),
            )
            if surface_kind is SurfaceKind.ANDROID
            else None
        ),
        page_type="profile",
        page_state="stable",
        observation_metadata={
            "covered_page_types": ["home"],
            "app_runtime_policy": {"exploration_mode": "dynamic_frontier"},
            BOUNDED_EXPLORATION_STATE_KEY: state.to_dict(),
        },
    )
    return replace(
        work,
        deadline=now + timedelta(minutes=5),
        frontier=replace(
            work.frontier, observation=replace(work.frontier.observation, observed_at=now)
        ),
    )


@pytest.mark.parametrize(
    "limits",
    [{"popup_repeat_count": 3}, {"retry_attempts": {"required": 2}}],
)
async def test_app_current_frame_cannot_bypass_scheduler_limits(limits):
    work = _scheduler_work(**limits)
    work = AppGlobalFrontierService(repository=object()).retain_registered_candidates(
        work=work,
        registered=(),
    )
    agent = MafExplorerAgent(client=ScriptedChatClient([]))

    assert eligible_app_candidates(work)[0] == []
    # 三合同独占：空候选须由状态机给出观察／终态，不得回 None 让自由规划绕过闸门。
    decision = await agent.decide_deterministic(work)
    assert isinstance(
        decision, (ExplorationTerminal, TimedObservationIntent, ActionIntent)
    )


@pytest.mark.parametrize("branch", ["profile_local", "history_parent"])
@pytest.mark.parametrize(
    ("limits", "expected_status", "settlement_reason"),
    [
        (
            {"popup_repeat_count": 3},
            "skipped",
            "bounded_exploration:popup_loop_limit",
        ),
        (
            {"retry_attempts": {"required": 2}},
            "selected",
            None,
        ),
    ],
)
def test_h5_shortcuts_respect_global_and_candidate_scoped_limits(
    branch,
    limits,
    expected_status,
    settlement_reason,
):
    work = _scheduler_work(surface_kind=SurfaceKind.WEB, **limits)
    candidate = replace(
        work.frontier.candidates[0],
        metadata={
            **work.frontier.candidates[0].metadata,
            "source_page_type": "profile",
            "target_keywords": ["profile_local_action"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.com/profile",
        navigation_depth=0,
        candidates=(candidate,),
    )
    state = BoundedExplorationState.from_dict(
        work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY],
        default_max_actions=20,
    )
    extra = (
        {
            "preferred_source_state_version": "state-profile",
            "preferred_source_location": "https://example.com/profile",
        }
        if branch == "history_parent"
        else {}
    )

    result = _select_next_task_entry(
        snapshot,
        active_state_version="state-profile",
        active_source_location="https://example.com/profile",
        active_page_type="profile",
        active_page_state="stable",
        active_observed_at=work.frontier.observation.observed_at,
        scheduler_state=state,
        **extra,
    )

    assert [entry.status.value for entry in result.entries] == [expected_status]
    assert result.entries[0].last_failure_reason == settlement_reason
    assert result.covered_page_types == snapshot.covered_page_types


@pytest.mark.parametrize("surface_kind", [SurfaceKind.ANDROID, SurfaceKind.WEB])
@pytest.mark.parametrize(
    "limits",
    [{"return_repeat_count": 3}, {"retry_attempts": {"required": 2}}],
)
async def test_final_dispatch_rejects_exhausted_recovered_decision(surface_kind, limits):
    work = _scheduler_work(surface_kind=surface_kind, **limits)
    intent = replace(
        _intent(work),
        surface_kind=surface_kind,
        semantic_target=work.frontier.candidates[0].semantic_target,
        parameters={"candidate_id": "settings-entry"},
    )
    coordinator = RecordingCoordinator()
    hooks = []

    async def before_side_effect(**kwargs):
        hooks.append(kwargs)

    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(intent),
        capture_coordinator=coordinator,
        before_side_effect=before_side_effect,
    )

    result = await executor.execute_decision(work=work, agent_run_id="run-1", decision=intent)

    assert result.is_terminal
    assert result.decision.outcome == "evidence_insufficient"
    assert result.decision.reason_code.startswith("bounded_exploration_")
    assert coordinator.commands == []
    assert hooks == []


@pytest.mark.parametrize("surface_kind", [SurfaceKind.ANDROID, SurfaceKind.WEB])
async def test_final_dispatch_keeps_available_candidate(surface_kind):
    work = _scheduler_work(surface_kind=surface_kind)
    intent = replace(
        _intent(work),
        surface_kind=surface_kind,
        parameters={"candidate_id": "settings-entry"},
    )
    coordinator = RecordingCoordinator(result="coordinated")
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(intent), capture_coordinator=coordinator
    )

    result = await executor.execute_decision(work=work, agent_run_id="run-1", decision=intent)

    assert not result.is_terminal
    assert result.coordination == "coordinated"
    assert len(coordinator.commands) == 1


async def test_h5_final_dispatch_does_not_apply_queue_retry_failures_to_sibling():
    """H5 队列累计失败不得在唯一派发门禁连坐未失败的 Frontier sibling。"""
    work = _scheduler_work(
        surface_kind=SurfaceKind.WEB,
        retry_attempts={
            "required": 2,
            "discovery": 2,
            "evidence_gap": 2,
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.com/profile",
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **work.frontier.observation.metadata,
                    H5_TASK_FRONTIER_METADATA_KEY: snapshot.to_dict(),
                },
            ),
        ),
    )
    intent = replace(
        _intent(work),
        surface_kind=SurfaceKind.WEB,
        parameters={"candidate_id": "settings-entry"},
    )
    coordinator = RecordingCoordinator(result="coordinated")
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(intent),
        capture_coordinator=coordinator,
    )

    result = await executor.execute_decision(
        work=work,
        agent_run_id="run-1",
        decision=intent,
    )

    assert not result.is_terminal
    assert result.coordination == "coordinated"
    assert len(coordinator.commands) == 1


def test_h5_wrapper_uses_snapshot_missing_page_types_for_queue_classification():
    """未覆盖且同时位于 required/evidence-gap 的页型仍应按 required 借用额度。"""
    work = _scheduler_work(
        surface_kind=SurfaceKind.WEB,
        consumed_actions={"required": 14},
        retry_attempts={"evidence_gap": 2},
        evidence_gap_page_types=("settings",),
    )
    candidate = replace(
        work.frontier.candidates[0],
        metadata={
            **work.frontier.candidates[0].metadata,
            "source_page_type": "profile",
            "target_keywords": ["profile_local_action"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=None,
        sampling_policy_summary={"journey_steps": ["settings"]},
    ).merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.com/profile",
        navigation_depth=0,
        candidates=(candidate,),
    )
    state = BoundedExplorationState.from_dict(
        work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY],
        default_max_actions=20,
    )

    result = _select_next_task_entry(
        snapshot,
        active_state_version="state-profile",
        active_source_location="https://example.com/profile",
        active_page_type="profile",
        active_page_state="stable",
        active_observed_at=work.frontier.observation.observed_at,
        scheduler_state=state,
    )

    assert [entry.status.value for entry in result.entries] == ["selected"]


async def test_app_dispatch_reuses_safe_candidate_pool_when_borrowing_queue_budget():
    """App 最终派发不得被不可执行的其它队列候选阻断。"""
    work = _scheduler_work(
        surface_kind=SurfaceKind.ANDROID,
        consumed_actions={"required": 14},
    )
    unsafe_candidate = CandidateAction(
        candidate_id="unsafe-discovery",
        semantic_target="跳转到未授权地址",
        action_type=InteractionOperation.NAVIGATE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=1,
        navigation_depth=1,
        metadata={"expected_next_page_type": "discovery"},
    )
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            candidates=(*work.frontier.candidates, unsafe_candidate),
        ),
    )
    assert [item.candidate_id for item in eligible_app_candidates(work)[0]] == [
        "settings-entry"
    ]
    intent = replace(
        _intent(work),
        surface_kind=SurfaceKind.ANDROID,
        parameters={"candidate_id": "settings-entry"},
    )
    coordinator = RecordingCoordinator(result="coordinated")
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(intent),
        capture_coordinator=coordinator,
    )

    result = await executor.execute_decision(
        work=work,
        agent_run_id="run-1",
        decision=intent,
    )

    assert not result.is_terminal
    assert result.coordination == "coordinated"
    assert len(coordinator.commands) == 1


async def test_timed_observation_rejects_deadline_crossed_during_wait(monkeypatch):
    """Timed Observation 等待跨过自身 deadline 后不得再调用采集器。"""
    import services.api.app.application.explorer_turn as explorer_turn_module

    work = _scheduler_work(surface_kind=SurfaceKind.ANDROID)
    observed_at = work.frontier.observation.observed_at
    observation_deadline = observed_at + timedelta(seconds=1)
    observation = TimedObservationIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        phase_run_id="phase-1",
        decision_id="decision-1",
        observation_id="observation-1",
        source_capture_id=work.source_capture_id,
        surface_kind=SurfaceKind.ANDROID,
        delay_ms=100,
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot", "ui_tree"),
            inherit_parent_semantics=True,
        ),
        deadline=observation_deadline,
    )
    clock = [observed_at]

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock[0]

    async def jump_past_deadline(_delay):
        clock[0] = observation_deadline + timedelta(seconds=1)

    monkeypatch.setattr(explorer_turn_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(explorer_turn_module.asyncio, "sleep", jump_past_deadline)
    coordinator = RecordingCoordinator()
    executor = ExplorerTurnExecutor(
        explorer=StaticExplorer(observation),
        capture_coordinator=coordinator,
    )

    with pytest.raises(TimeoutError, match="等待后已超出探索截止时间"):
        await executor.execute_decision(
            work=work,
            agent_run_id="run-1",
            decision=observation,
        )

    assert coordinator.commands == []
