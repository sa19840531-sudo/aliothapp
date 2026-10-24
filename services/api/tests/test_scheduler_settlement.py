"""验证动作在截止时间边界结算时仍完整落账。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationScheduler,
    BoundedExplorationState,
    ExplorationQueue,
    ExplorationStopReason,
    update_scheduler_metadata,
)
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.test_h5_exploration_workflow import (
    NOW,
    _AssessmentRepository,
    _Bootstrapper,
    _capture,
    _command,
    _coordination,
    _Detector,
    _Orchestrator,
    _workflow_module,
)

_DEADLINE = datetime(2026, 9, 7, 12, tzinfo=UTC)


def _candidate() -> CandidateAction:
    return CandidateAction(
        candidate_id="settings-entry",
        semantic_target="打开设置",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=1,
        metadata={
            "expected_next_page_type": "settings",
            "frontier_scope": "local",
        },
    )


def _settlement_metadata() -> dict[str, object]:
    state = BoundedExplorationState(
        max_actions=20,
        max_duration_ms=60_000,
        deadline=_DEADLINE,
        consumed_actions={"required": 3},
        consumed_duration_ms={"required": 4_000},
        retry_attempts={"required": 1},
        required_page_types=("settings",),
    )
    return {BOUNDED_EXPLORATION_STATE_KEY: state.to_dict()}


@pytest.mark.parametrize(
    ("settlement_offset", "retryable_failure"),
    [
        (-1, False),
        (-1, True),
        (0, False),
        (0, True),
        (1, False),
        (1, True),
    ],
)
def test_settlement_accounts_claimed_action_at_deadline_boundary(
    settlement_offset: int,
    retryable_failure: bool,
) -> None:
    """动作已领取后，即使结算在截止时刻之后，也必须累计实际结果。"""
    settlement_now = _DEADLINE + timedelta(seconds=settlement_offset)
    result = update_scheduler_metadata(
        _settlement_metadata(),
        scheduler=BoundedExplorationScheduler(),
        max_actions=20,
        max_duration_ms=60_000,
        deadline=_DEADLINE,
        candidate=_candidate(),
        required_page_types=("settings",),
        action_claimed=True,
        retryable_failure=retryable_failure,
        progressed=not retryable_failure,
        elapsed_duration_ms=2_000,
        now=settlement_now,
    )
    state = BoundedExplorationState.from_dict(
        result[BOUNDED_EXPLORATION_STATE_KEY],
        default_max_actions=20,
    )

    assert state.consumed_actions["required"] == 4
    assert state.consumed_duration_ms["required"] == 6_000
    assert state.retry_attempts["required"] == (2 if retryable_failure else 1)
    if settlement_offset < 0:
        assert state.stop_reason(now=settlement_now) is ExplorationStopReason.NONE
        expected = () if retryable_failure else (_candidate(),)
        assert BoundedExplorationScheduler().filter_candidates(
            (_candidate(),), state=state, now=settlement_now
        ) == expected
    else:
        assert state.stop_reason(now=settlement_now) is ExplorationStopReason.DEADLINE_EXPIRED
        assert BoundedExplorationScheduler().filter_candidates(
            (_candidate(),), state=state, now=settlement_now
        ) == ()


def test_stopped_metadata_refresh_without_candidate_does_not_claim_action() -> None:
    """停止后仅刷新 Frontier metadata 不得伪造动作或耗时。"""
    metadata = _settlement_metadata()
    before = dict(metadata[BOUNDED_EXPLORATION_STATE_KEY])

    result = update_scheduler_metadata(
        metadata,
        scheduler=BoundedExplorationScheduler(),
        max_actions=20,
        max_duration_ms=60_000,
        deadline=_DEADLINE,
        now=_DEADLINE,
    )

    assert result[BOUNDED_EXPLORATION_STATE_KEY] == before
    assert metadata[BOUNDED_EXPLORATION_STATE_KEY] == before


def test_stopped_observation_settles_elapsed_time_without_claiming_action() -> None:
    """截止后的真实观察可以结算耗时，但不能伪造动作或重试。"""
    result = update_scheduler_metadata(
        _settlement_metadata(),
        scheduler=BoundedExplorationScheduler(),
        max_actions=20,
        max_duration_ms=60_000,
        deadline=_DEADLINE,
        elapsed_duration_ms=2_000,
        elapsed_queue=ExplorationQueue.REQUIRED,
        now=_DEADLINE + timedelta(seconds=1),
    )
    state = BoundedExplorationState.from_dict(
        result[BOUNDED_EXPLORATION_STATE_KEY],
        default_max_actions=20,
    )

    assert state.consumed_actions["required"] == 3
    assert state.consumed_duration_ms["required"] == 6_000
    assert state.retry_attempts["required"] == 1
    stop_reason = state.stop_reason(now=_DEADLINE + timedelta(seconds=1))
    assert stop_reason is ExplorationStopReason.DEADLINE_EXPIRED


def test_repeating_same_prior_settlement_is_pure_and_does_not_double_count() -> None:
    """相同不可变 prior 重算时返回相同后继，而不是在结果上再次累计。"""
    prior = _settlement_metadata()
    before = dict(prior[BOUNDED_EXPLORATION_STATE_KEY])
    kwargs = {
        "scheduler": BoundedExplorationScheduler(),
        "max_actions": 20,
        "max_duration_ms": 60_000,
        "deadline": _DEADLINE,
        "candidate": _candidate(),
        "required_page_types": ("settings",),
        "action_claimed": True,
        "retryable_failure": True,
        "progressed": False,
        "elapsed_duration_ms": 2_000,
        "now": _DEADLINE + timedelta(seconds=1),
    }

    first = update_scheduler_metadata(prior, **kwargs)
    second = update_scheduler_metadata(prior, **kwargs)

    assert first == second
    assert first[BOUNDED_EXPLORATION_STATE_KEY]["consumed_actions"]["required"] == 4
    assert first[BOUNDED_EXPLORATION_STATE_KEY]["retry_attempts"]["required"] == 2
    assert prior[BOUNDED_EXPLORATION_STATE_KEY] == before


def _non_deadline_stop_state(reason: str) -> BoundedExplorationState:
    values: dict[str, object] = {
        "max_actions": 20,
        "max_duration_ms": 60_000,
        "deadline": _DEADLINE,
        "required_page_types": ("settings",),
    }
    if reason == "actions":
        values.update(max_actions=3, consumed_actions={"required": 3})
    elif reason == "duration":
        values.update(max_duration_ms=4_000, consumed_duration_ms={"required": 4_000})
    elif reason == "retry":
        values.update(
            retry_attempts={
                "required": 2,
                "evidence_gap": 2,
                "discovery": 2,
            }
        )
    elif reason == "no_progress":
        values.update(no_progress_count=3)
    else:
        raise AssertionError(f"未知停止原因: {reason}")
    return BoundedExplorationState(**values)


@pytest.mark.parametrize("reason", ["actions", "duration", "retry", "no_progress"])
@pytest.mark.parametrize("settlement_offset", [-1, 1])
def test_non_deadline_stop_does_not_settle_from_progress_or_new_fingerprints(
    reason: str,
    settlement_offset: int,
) -> None:
    """动作/时间/重试/无进展终态不得被截止结算分支误解锁。"""
    state = _non_deadline_stop_state(reason)
    settlement_now = _DEADLINE + timedelta(seconds=settlement_offset)
    result = update_scheduler_metadata(
        {BOUNDED_EXPLORATION_STATE_KEY: state.to_dict()},
        scheduler=BoundedExplorationScheduler(),
        max_actions=state.max_actions,
        max_duration_ms=state.max_duration_ms,
        deadline=_DEADLINE,
        candidate=_candidate(),
        required_page_types=("settings",),
        action_claimed=True,
        retryable_failure=True,
        progressed=True,
        state_fingerprint=f"new-state-{reason}",
        page_fingerprint=f"new-page-{reason}",
        popup_fingerprint=f"new-popup-{reason}",
        return_fingerprint=f"new-return-{reason}",
        elapsed_duration_ms=2_000,
        now=settlement_now,
    )
    after = BoundedExplorationState.from_dict(
        result[BOUNDED_EXPLORATION_STATE_KEY],
        default_max_actions=state.max_actions,
        default_max_duration_ms=state.max_duration_ms,
    )

    assert after == state
    assert after.stop_reason(now=settlement_now) is state.stop_reason(now=settlement_now)


def _late_action_turn(work, *, module, action_id: str, before: str, after: str, failed: bool):
    candidate = work.frontier.candidates[0]
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=ActionIntent(
            task_id=work.task_id,
            session_id=work.session_id,
            intent_id=f"intent:{action_id}",
            idempotency_key=f"intent:{action_id}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            purpose="截止前启动、截止后结算",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=work.deadline,
            parameters={"candidate_id": candidate.candidate_id},
            risk_level=candidate.risk_level,
        ),
        coordination=_coordination(
            before=before,
            after=after,
            source=work.source_capture_id,
            status=(
                InteractionResultStatus.FAILED
                if failed
                else InteractionResultStatus.SUCCEEDED
            ),
            error_code="driver_failed" if failed else None,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_real_h5_successor_accounts_action_after_deadline(failed: bool) -> None:
    """正式 H5 后继成功/失败结算均与 Work 动作扣减保持一致。"""
    module = _workflow_module()
    detector = _Detector()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=181),
    )
    initial = await workflow.run(_command())
    prior_actions = initial.work.budget.remaining_actions
    prior_duration = initial.work.budget.remaining_duration_ms

    result = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(
            prior_work=initial.work,
            turn=_late_action_turn(
                initial.work,
                module=module,
                action_id="late-successor",
                before="before-late-successor",
                after="after-late-successor",
                failed=failed,
            ),
        )
    )
    raw_state = result.work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert isinstance(raw_state, Mapping)

    assert result.work.budget.remaining_actions == prior_actions - 1
    assert sum(raw_state["consumed_actions"].values()) == 1
    assert sum(raw_state["retry_attempts"].values()) == int(failed)
    assert sum(raw_state["consumed_duration_ms"].values()) == prior_duration


@pytest.mark.asyncio
async def test_real_h5_failure_reobserve_accounts_action_after_deadline() -> None:
    """失败重观察同样是已派发动作，不能因截止后的 Capture 丢失账本。"""
    module = _workflow_module()
    detector = _Detector()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=detector,
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
        now=lambda: NOW + timedelta(seconds=181),
    )
    initial = await workflow.run(_command())
    before = _capture(
        "before-late-reobserve",
        role="before_action",
        parent=initial.work.source_capture_id,
        action_execution_id="late-reobserve-action",
    )

    result = await workflow.prepare_after_action_failure(
        module.ReobserveH5ExplorationCommand(
            prior_work=initial.work,
            before_capture=before,
            error_code="STARTUP_AD_DISMISSAL_NOT_FOUND",
            retryable=True,
        )
    )
    raw_state = result.work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert isinstance(raw_state, Mapping)

    assert result.work.budget.remaining_actions == initial.work.budget.remaining_actions - 1
    assert sum(raw_state["consumed_actions"].values()) == 1
    assert sum(raw_state["retry_attempts"].values()) == 1
    assert (
        sum(raw_state["consumed_duration_ms"].values())
        == initial.work.budget.remaining_duration_ms
    )
