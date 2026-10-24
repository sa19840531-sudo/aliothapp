"""H5 关闭后补采必须另建会话，且不能降低请求本身的证明要求。"""

from dataclasses import fields, replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_recovery_replay import EvidenceRecoveryReplayError
from services.api.app.application.h5_new_session_restoration import H5NewSessionRestorationExecutor
from services.api.app.application.h5_targeted_recapture import (
    H5TargetedRecaptureContext,
    H5TargetedRecapturePlanService,
)
from services.api.app.application.restoration import (
    RestorationExecutionError,
    RestorationExecutionResult,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSourceGrade,
    RestorationStrategy,
    StateInvariantSnapshot,
    build_evidence_request,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.app.infrastructure.browser.h5_new_session_recovery_runtime import (
    H5NewSessionRecoveryRuntime,
)
from services.api.tests.test_h5_targeted_recapture import NOW, _snapshot_request


def _closed_context():
    return H5TargetedRecaptureContext(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        session_id="h5:run-h5",
        current_capture_id="entry-capture",
        target_state_id="capture-target",
        target_identity="home",
        current_identity="home",
        current_url="https://example.test/",
        target_url="https://example.test/",
        source_session_closed=True,
        replay_session_id="h5:run-h5:recapture:0123456789abcdef0123",
        entry_url="https://example.test/",
    )


@pytest.mark.parametrize("changes,ready", [
    ({}, True),
    ({"target_identity": None}, False),
    ({"current_identity": None}, False),
    ({"entry_url": None}, False),
    ({"agent_run_id": "other-run"}, False),
    ({"finalization_epoch": 5}, False),
    ({"task_id": "other-task"}, False),
    ({"target_identity": "detail", "target_url": "https://example.test/detail"}, False),
])
async def test_current_snapshot_defer_preflights_entry_replay_without_runtime_side_effects(
    changes, ready,
):
    context = replace(_closed_context(), source_session_closed=False, **changes)
    runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
    service = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read_snapshot_replay_context=AsyncMock(return_value=context)),
        clock=lambda: NOW, new_session_runtime=runtime,
    )
    assert await service.can_defer_snapshot(
        request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4,
    ) is ready
    runtime.remember_new_session_plan.assert_not_awaited()


async def test_snapshot_defer_without_runtime_or_prospective_context_remains_blocked():
    for contexts, runtime in [
        (SimpleNamespace(), SimpleNamespace()),
        (SimpleNamespace(read_snapshot_replay_context=AsyncMock(
            return_value=replace(_closed_context(), source_session_closed=False),
        )), None),
    ]:
        service = H5TargetedRecapturePlanService(
            contexts=contexts, clock=lambda: NOW, new_session_runtime=runtime,
        )
        assert not await service.can_defer_snapshot(
            request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4,
        )


async def test_active_planner_preflights_bound_closed_session_handler_but_cannot_execute_it():
    handler = AsyncMock()
    contexts = SimpleNamespace(
        read_snapshot_replay_context=AsyncMock(return_value=replace(
            _closed_context(), source_session_closed=False,
        )), read=AsyncMock(return_value=_closed_context()),
    )
    service = H5TargetedRecapturePlanService(
        contexts=contexts, clock=lambda: NOW, closed_session_replay=handler,
    )
    assert await service.can_defer_snapshot(
        request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4,
    )
    result = await service.resolve(
        request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4,
    )
    assert not result.executable
    assert result.reason_code == "H5_SNAPSHOT_NEW_SESSION_RUNTIME_UNAVAILABLE"
    handler.assert_not_awaited()


async def test_closed_snapshot_plans_new_session_even_when_entry_has_same_identity():
    context = _closed_context()
    runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
    service = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    )
    result = await service.resolve(
        request=_snapshot_request(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert result.executable
    assert result.plan.strategy is RestorationStrategy.NEW_SESSION_REPLAY
    assert result.plan.source_grade is EvidenceSourceGrade.C
    assert result.plan.source_session_id == context.session_id
    assert result.plan.target_session_id == context.replay_session_id
    assert result.plan.normalization_step_ids == ()
    runtime.remember_new_session_plan.assert_awaited_once()


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {"session_dependency": EvidenceSessionDependency.ACTIVE_SESSION},
            "H5_NEW_SESSION_RECAPTURE_CONSTRAINT_UNSUPPORTED",
        ),
        (
            {"replay_policy": EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED},
            "H5_NEW_SESSION_RECAPTURE_CONSTRAINT_UNSUPPORTED",
        ),
        (
            {"required_equivalence": EvidenceRequiredEquivalence.ORIGINAL_ATOMIC},
            "H5_NEW_SESSION_RECAPTURE_CONSTRAINT_UNSUPPORTED",
        ),
        (
            {"minimum_source_grade": EvidenceSourceGrade.A1},
            "H5_NEW_SESSION_SOURCE_GRADE_INSUFFICIENT",
        ),
    ],
)
async def test_closed_session_cannot_weaken_request_constraints(changes, reason):
    original = _snapshot_request()
    payload = {
        field.name: getattr(original, field.name)
        for field in fields(original)
        if field.name not in {"request_id", "requirement_hash", "protocol_version"}
    }
    runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
    service = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_closed_context())),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    )
    result = await service.resolve(
        request=build_evidence_request(**{**payload, **changes}),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert result.plan is None
    assert result.reason_code == reason
    runtime.remember_new_session_plan.assert_not_awaited()


async def test_closed_session_without_runtime_is_not_declared_ready():
    result = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_closed_context())),
        clock=lambda: NOW,
    ).resolve(request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4)
    assert result.plan is None
    assert result.reason_code == "H5_SNAPSHOT_NEW_SESSION_RUNTIME_UNAVAILABLE"


async def test_new_session_invalid_graph_contract_is_terminal_without_opening_browser():
    context = replace(
        _closed_context(), target_identity="detail", target_url="https://example.test/d"
    )
    runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
    result = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)),
        page_graph=SimpleNamespace(get_recovery_route=AsyncMock(return_value={})),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    ).resolve(request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4)
    assert result.plan is None and result.reason_code == "PAGE_GRAPH_ROUTE_CONTRACT_INVALID"
    assert not result.retryable
    runtime.remember_new_session_plan.assert_not_awaited()


async def _new_runtime(*, identity="home", starting_error=None, register_error=None):
    context = _closed_context()
    checkpoint = AsyncMock()
    capture = CaptureRef(
        task_id=context.task_id,
        session_id=context.replay_session_id,
        capture_id="new-capture",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
    )
    sessions = SimpleNamespace(
        register_active=AsyncMock(side_effect=register_error),
        get=AsyncMock(
            return_value=SimpleNamespace(
                task_id=context.task_id,
                current_capture_id=capture.capture_id,
                status=SimpleNamespace(value="active"),
            )
        ),
    )
    driver = SimpleNamespace(
        start_session=AsyncMock(
            return_value=SimpleNamespace(primary_surface_ref="surface-new"),
            side_effect=starting_error,
        )
    )
    capture_runtime = SimpleNamespace(
        surface_driver=driver,
        close_h5_exploration_session=AsyncMock(),
        close_browser_session=AsyncMock(),
    )
    coordinator = SimpleNamespace(
        coordinate=AsyncMock(
            return_value=SimpleNamespace(
                after_capture=capture,
                interaction=SimpleNamespace(
                    status=InteractionResultStatus.SUCCEEDED,
                    action_execution_id="action-new",
                ),
            )
        )
    )
    runtime = H5NewSessionRecoveryRuntime(
        surface_sessions=sessions,
        capture_coordinator=coordinator,
        detector=SimpleNamespace(
            detect=AsyncMock(
                return_value={
                    "page_capture_id": capture.capture_id,
                    "page_url": context.entry_url,
                    "page_type": "home",
                    "page_state": "stable",
                    "confidence": 0.9,
                    "matched_catalog_signals": [],
                    "candidate_actions": [],
                }
            )
        ),
        page_graph=object(),
        capture_runtime=capture_runtime,
        checkpoint=checkpoint,
        deadline=NOW + timedelta(minutes=10),
        now=lambda: NOW,
    )
    runtime._read_observation = AsyncMock(return_value=SimpleNamespace(page_identity=identity))
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    ).resolve(request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4)
    return runtime, resolution.plan, capture_runtime, coordinator, checkpoint


async def test_new_session_bootstrap_uses_task_entry_and_fresh_capture():
    runtime, plan, browser, coordinator, checkpoint = await _new_runtime()
    capture, action = await runtime.prepare_new_session(
        plan=plan,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert capture.session_id == plan.target_session_id
    assert action == "action-new"
    spec = browser.surface_driver.start_session.call_args.args[0]
    assert spec.options["target_url"] == "https://example.test/"
    intent = coordinator.coordinate.call_args.args[0].intent
    assert intent.source_capture_id == "h5-entry:run-h5"
    assert intent.parameters["initial_surface_action"] is True
    assert intent.parameters["supplemental_evidence_source"] == "targeted_recapture"
    assert intent.deadline <= runtime.deadline
    assert checkpoint.await_count == 3
    assert capture.capture_id in runtime._frontiers
    await runtime.close_owned_sessions()
    browser.close_h5_exploration_session.assert_awaited_once()
    await runtime.close_owned_sessions()
    assert browser.close_h5_exploration_session.await_count == 1


async def test_new_session_drift_never_produces_equivalent_proof():
    runtime, plan, browser, _, _ = await _new_runtime(identity="login")
    with pytest.raises(EvidenceRecoveryReplayError) as failure:
        await runtime.prepare_new_session(plan=plan, agent_run_id="run-h5", finalization_epoch=4)
    assert failure.value.reason_code == "H5_NEW_SESSION_ENTRY_STATE_DIVERGED"
    assert failure.value.retryable is False
    await runtime.close_owned_sessions()
    browser.close_h5_exploration_session.assert_awaited_once()


async def test_new_session_normalizes_fresh_popup_chain_before_matching_entry(monkeypatch):
    from dataclasses import asdict

    from services.api.app.domain.exploration import CandidateAction
    from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
    from services.api.app.infrastructure.browser import h5_new_session_recovery_runtime as module

    runtime, plan, browser, coordinator, _ = await _new_runtime(identity="ad")
    runtime._plans[plan.plan_id] = SimpleNamespace(
        **{**asdict(runtime._plans[plan.plan_id]), "entry_normalization_required": True},
    )
    first = coordinator.coordinate.return_value.after_capture
    captures = [first, replace(first, capture_id="second"), replace(first, capture_id="third")]
    signature = "h5-obstruction:" + "a" * 64
    candidate = CandidateAction(
        candidate_id="close", action_type=InteractionOperation.CLICK,
        semantic_target="关闭当前弹窗", risk_level=InteractionRiskLevel.LOW,
        exploration_value=100, navigation_depth=0,
        metadata={"obstruction_signature": signature,
                  "target_keywords": [
                      "popup_dismissal", "close_control", "modal_local_safe_acknowledgement",
                  ],
                  "action_parameters": {"target_mode": "popup_dismissal",
                                        "popup_dismissal_capability": (
                                            "modal_local_safe_acknowledgement"
                                        ),
                                        "obstruction_signature": signature}},
    )
    frontiers = [SimpleNamespace(
        observation=SimpleNamespace(
            capture_id=capture.capture_id, page_type="home", page_state="popup",
        ), candidates=(candidate,), blocked_candidate_ids=(),
    ) for capture in captures[:2]]
    frontiers.append(SimpleNamespace(
        observation=SimpleNamespace(capture_id="third", page_type="home", page_state="stable"),
        candidates=(), blocked_candidate_ids=(),
    ))
    monkeypatch.setattr(module, "build_h5_frontier", lambda **_: frontiers.pop(0))
    runtime._read_observation.side_effect = [
        SimpleNamespace(page_identity=v) for v in ("ad", "ad2", "home")
    ]
    coordinator.coordinate.side_effect = [SimpleNamespace(
        after_capture=capture, interaction=SimpleNamespace(
            status=InteractionResultStatus.SUCCEEDED, action_execution_id=f"action-{index}",
        ),
    ) for index, capture in enumerate(captures)]
    try:
        capture, actions = await runtime.prepare_new_session(
            plan=plan, agent_run_id="run-h5", finalization_epoch=4,
        )
        assert capture.capture_id == "third"
        assert actions == ("action-0", "action-1", "action-2")
        intents = [call.args[0].intent for call in coordinator.coordinate.call_args_list]
        assert [intent.source_capture_id for intent in intents[1:]] == [first.capture_id, "second"]
        assert all(intent.action_type is InteractionOperation.CLICK for intent in intents[1:])
        assert all(intent.parameters["target_mode"] == "popup_dismissal" for intent in intents[1:])
    finally:
        await runtime.close_owned_sessions()


@pytest.mark.parametrize("failure", ["ambiguous", "blocked", "high_risk", "unknown", "budget"])
async def test_new_session_entry_normalization_cannot_click_unproven_or_expired_target(
    monkeypatch, failure,
):
    from services.api.app.domain.exploration import CandidateAction
    from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
    from services.api.app.infrastructure.browser import h5_new_session_recovery_runtime as module

    runtime, plan, browser, coordinator, checkpoint = await _new_runtime(identity="popup")
    runtime._plans[plan.plan_id] = replace(
        runtime._plans[plan.plan_id], entry_normalization_required=True,
    )
    signature = "h5-obstruction:" + "a" * 64
    candidate = CandidateAction(
        candidate_id="close", action_type=InteractionOperation.CLICK,
        semantic_target="关闭当前弹窗", risk_level=InteractionRiskLevel.LOW,
        exploration_value=100, navigation_depth=0,
        metadata={
            "obstruction_signature": signature,
            "target_keywords": [
                "popup_dismissal", "close_control", "modal_local_safe_acknowledgement",
            ],
            "action_parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "modal_local_safe_acknowledgement",
                "obstruction_signature": signature,
            },
        },
    )
    if failure == "high_risk":
        candidate = replace(candidate, risk_level=InteractionRiskLevel.HIGH)
    if failure == "unknown":
        candidate = replace(candidate, semantic_target="点击活动入口")
    candidates = (candidate, replace(candidate, candidate_id="other")) if (
        failure == "ambiguous"
    ) else (candidate,)
    frontier = SimpleNamespace(
        observation=SimpleNamespace(
            capture_id="new-capture", page_type="home", page_state="popup",
        ),
        candidates=candidates,
        blocked_candidate_ids=("close",) if failure == "blocked" else (),
    )
    monkeypatch.setattr(module, "build_h5_frontier", lambda **_: frontier)
    if failure == "budget":
        checkpoint.side_effect = [None, None, None, _permanent_budget_error()]
    try:
        with pytest.raises(EvidenceRecoveryReplayError) as raised:
            await runtime.prepare_new_session(
                plan=plan, agent_run_id="run-h5", finalization_epoch=4,
            )
        expected = {
            "ambiguous": "H5_NEW_SESSION_ENTRY_CLEARANCE_AMBIGUOUS",
            "budget": "H5_NEW_SESSION_SCOPE_OR_BUDGET_EXHAUSTED",
        }.get(failure, "H5_NEW_SESSION_ENTRY_STATE_DIVERGED")
        assert raised.value.reason_code == expected
        intents = [call.args[0].intent for call in coordinator.coordinate.call_args_list]
        assert intents[0].action_type is InteractionOperation.NAVIGATE
        assert all(intent.action_type is InteractionOperation.OBSERVE for intent in intents[1:])
        assert len(intents) <= 3
    finally:
        await runtime.close_owned_sessions()
        browser.close_h5_exploration_session.assert_awaited_once()


def _permanent_budget_error():
    error = EvidenceRecoveryReplayError(
        reason_code="H5_NEW_SESSION_SCOPE_OR_BUDGET_EXHAUSTED", message="预算已耗尽",
    )
    error.retryable = False
    return error


@pytest.mark.parametrize("fault", ["task", "session", "detector"])
async def test_new_session_bootstrap_rejects_foreign_capture_or_detection(fault):
    runtime, plan, browser, coordinator, _ = await _new_runtime()
    capture = coordinator.coordinate.return_value.after_capture
    if fault == "detector":
        runtime._detector.detect.return_value["page_capture_id"] = "other-capture"
    else:
        coordinator.coordinate.return_value.after_capture = replace(
            capture, **{"task_id" if fault == "task" else "session_id": "other"},
        )
    try:
        with pytest.raises(EvidenceRecoveryReplayError) as raised:
            await runtime.prepare_new_session(
                plan=plan, agent_run_id="run-h5", finalization_epoch=4,
            )
        assert raised.value.reason_code == "H5_NEW_SESSION_CAPTURE_SCOPE_INVALID"
        assert coordinator.coordinate.await_count == 1
    finally:
        await runtime.close_owned_sessions()
        browser.close_h5_exploration_session.assert_awaited_once()


async def test_interrupted_attempt_settlement_failure_does_not_skip_physical_cleanup():
    runtime, plan, browser, _, _ = await _new_runtime()
    await runtime.prepare_new_session(plan=plan, agent_run_id="run-h5", finalization_epoch=4)
    runtime._settle_interrupted_restorations = AsyncMock(side_effect=RuntimeError("数据库暂不可用"))
    with pytest.raises(ExceptionGroup):
        await runtime.close_owned_sessions()
    browser.close_h5_exploration_session.assert_awaited_once()
    runtime._settle_interrupted_restorations.assert_awaited_once_with(
        task_id=plan.task_id,
        session_id=plan.target_session_id,
    )


@pytest.mark.parametrize("error_kind", ["start", "register", "cancel"])
async def test_new_session_startup_failure_and_cancellation_release_browser(error_kind):
    import asyncio

    failure = asyncio.CancelledError() if error_kind == "cancel" else RuntimeError("启动失败")
    runtime, plan, browser, _, _ = await _new_runtime(
        starting_error=failure if error_kind in {"start", "cancel"} else None,
        register_error=failure if error_kind == "register" else None,
    )
    with pytest.raises(type(failure)):
        await runtime.prepare_new_session(plan=plan, agent_run_id="run-h5", finalization_epoch=4)
    runtime._surface_sessions.get.return_value = None
    await runtime.close_owned_sessions()
    browser.close_browser_session.assert_awaited_once_with(plan.target_session_id)


@pytest.mark.parametrize("change", ["epoch", "run", "budget"])
async def test_new_session_scope_and_budget_rejection_precede_browser(change):
    runtime, plan, browser, _, checkpoint = await _new_runtime()
    if change == "budget":
        checkpoint.side_effect = EvidenceRecoveryReplayError(
            reason_code="BUDGET_EXHAUSTED",
            message="预算耗尽",
        )
    with pytest.raises(EvidenceRecoveryReplayError):
        await runtime.prepare_new_session(
            plan=plan,
            agent_run_id="other" if change == "run" else "run-h5",
            finalization_epoch=5 if change == "epoch" else 4,
        )
    browser.surface_driver.start_session.assert_not_awaited()


async def test_new_session_plan_is_clamped_to_frozen_deadline():
    runtime = SimpleNamespace(
        deadline=NOW + timedelta(seconds=10),
        remember_new_session_plan=AsyncMock(),
    )
    planning = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_closed_context())),
        clock=lambda: NOW,
        new_session_runtime=runtime,
    )
    plan = (
        await planning.resolve(
            request=_snapshot_request(),
            agent_run_id="run-h5",
            finalization_epoch=4,
        )
    ).plan
    assert plan.deadline == runtime.deadline
    runtime.deadline = NOW
    result = await planning.resolve(
        request=_snapshot_request(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert result.reason_code == "H5_NEW_SESSION_DEADLINE_EXHAUSTED"


@pytest.mark.parametrize("route", [False, True])
@pytest.mark.parametrize("clearance", [False, True])
async def test_new_session_executor_uses_fresh_snapshot_or_existing_replay(route, clearance):
    runtime, plan, _, _, _ = await _new_runtime()
    capture = SimpleNamespace(capture_id="fresh-entry")
    actions = ("bootstrap-action", "close-first", "close-second") if clearance else (
        "bootstrap-action",
    )
    runtime.prepare_new_session = AsyncMock(
        return_value=(capture, actions if clearance else actions[0]),
    )
    snapshot = StateInvariantSnapshot(
        task_id=plan.task_id,
        session_id=plan.target_session_id,
        capture_id=capture.capture_id,
        surface_kind=SurfaceKind.WEB,
        environment={},
        business={},
        semantic={},
        structure={},
        captured_at=NOW,
    )
    snapshots = SimpleNamespace(read=AsyncMock(return_value=snapshot))
    delegate = SimpleNamespace(
        restore=AsyncMock(
            return_value=RestorationExecutionResult(
                restored_snapshot=snapshot,
                action_execution_ids=("route-action",),
            )
        )
    )
    executor = H5NewSessionRestorationExecutor(
        delegate=delegate,
        runtime=runtime,
        snapshots=snapshots,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    if route:
        # 这里只验证分派，路线自身的信任、连续性和到达证明由公共重放测试覆盖。
        plan = SimpleNamespace(
            **{
                **plan.to_dict(),
                "strategy": RestorationStrategy.NEW_SESSION_REPLAY,
                "normalization_step_ids": ("edge-1",),
            }
        )
    result = await executor.restore(SimpleNamespace(plan=plan))
    assert result.restored_snapshot is snapshot
    assert result.action_execution_ids == (
        (*actions, "route-action") if route else actions
    )
    assert snapshots.read.await_count == (0 if route else 1)
    assert delegate.restore.await_count == int(route)


async def test_new_session_executor_preserves_permanent_failure_reason():
    runtime, plan, _, _, _ = await _new_runtime()
    failure = EvidenceRecoveryReplayError(reason_code="STATE_DRIFTED", message="状态漂移")
    failure.retryable = False
    runtime.prepare_new_session = AsyncMock(side_effect=failure)
    executor = H5NewSessionRestorationExecutor(
        delegate=SimpleNamespace(restore=AsyncMock()),
        runtime=runtime,
        snapshots=SimpleNamespace(read=AsyncMock()),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    with pytest.raises(RestorationExecutionError) as error:
        await executor.restore(SimpleNamespace(plan=plan))
    assert error.value.error_code == "STATE_DRIFTED"
    assert error.value.unfulfillable
