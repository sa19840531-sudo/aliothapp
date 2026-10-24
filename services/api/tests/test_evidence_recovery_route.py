"""定向补采只消费同一执行域内的可信页面图路线。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.api.app.application import evidence_request_routing
from services.api.app.application.app_probe_hangback import _rebuild_plan
from services.api.app.application.evidence_recovery_route import (
    RECOVERY_ROUTE_REDRIVABLE_ACTIONS,
    EvidenceRecoveryRouteValidationError,
    RouteRiskTier,
    RouteUnavailableReason,
    explain_route_reason,
    plan_evidence_recovery_route,
)
from services.api.app.application.evidence_request_routing import (
    _load_capture_context,
    _plan_page_graph_route,
    _record_evidence_request_route_failure,
    _route_one,
    is_deterministic_route_unfulfillable,
    route_outcome_evidence_requests,
)
from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.domain.exploration import (
    ExplorationBudget,
    ExplorationValidationError,
    ProbeWork,
    build_probe_work,
)
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    InspectionHandoff,
    InspectionRuleTarget,
    RestorationStrategy,
    RuleJudgeOutcome,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)


def _response(*, steps: list[dict[str, object]]) -> dict[str, object]:
    return _response_with_routes(routes=[_route(steps=steps)])


def _response_with_routes(*, routes: list[dict[str, object]]) -> dict[str, object]:
    return {
        "protocol_version": "page_graph_recovery.v1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "finalization_epoch": 2,
        "status": "available",
        "requires_stepwise_reverification": True,
        "routes": routes,
    }


def _route(*, steps: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_state_id": "capture-current",
        "target_state_id": "capture-target",
        "target_page_key": "web:https://example.com/detail",
        "steps": steps,
    }


def _step(
    transition_id: str,
    source: str,
    target: str,
    *,
    risk_level: str = "low",
    action_kind: str = "click",
    semantic_target: str | None = "详情入口",
    reason_codes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "transition_id": transition_id,
        "source_state_id": source,
        "target_state_id": target,
        "status": "completed",
        "proof": "trusted",
        "risk_level": risk_level,
        "replay_eligible": True,
        "action_kind": action_kind,
        "semantic_target": semantic_target,
        "reason_codes": reason_codes or [],
    }


def _plan(response: dict[str, object]):
    return plan_evidence_recovery_route(
        response,
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=2,
        source_state_id="capture-current",
        target_state_id="capture-target",
        target_identity="web:https://example.com/detail",
    )


def test_plan_evidence_recovery_route_returns_stable_ordered_route() -> None:
    response = _response(
        steps=[
            _step("action-1", "capture-current", "capture-middle"),
            _step("action-2", "capture-middle", "capture-target"),
        ]
    )

    first = _plan(response)
    second = _plan(response)

    assert first == second
    assert first.route is not None
    assert first.reason_code is None
    assert first.route.route_id.startswith("page-graph-route:")
    assert first.route.transition_ids == ("action-1", "action-2")
    assert first.route.risk_tier is RouteRiskTier.STABLE
    assert first.explain()["planned"] is True


def test_plan_prefers_lower_risk_route_over_shorter_one() -> None:
    """风险档优先于步数：两步语义点击胜过一步返回。"""

    response = _response_with_routes(
        routes=[
            _route(
                steps=[
                    _step(
                        "back-1",
                        "capture-current",
                        "capture-target",
                        action_kind="back",
                        semantic_target=None,
                    )
                ]
            ),
            _route(
                steps=[
                    _step("click-1", "capture-current", "capture-middle"),
                    _step("click-2", "capture-middle", "capture-target"),
                ]
            ),
        ]
    )

    plan = _plan(response)

    assert plan.route is not None
    assert plan.route.transition_ids == ("click-1", "click-2")
    assert plan.route.risk_tier is RouteRiskTier.STABLE


def test_plan_prefers_fewer_steps_when_risk_is_equal() -> None:
    response = _response_with_routes(
        routes=[
            _route(
                steps=[
                    _step("long-1", "capture-current", "capture-middle"),
                    _step("long-2", "capture-middle", "capture-target"),
                ]
            ),
            _route(steps=[_step("short-1", "capture-current", "capture-target")]),
        ]
    )

    plan = _plan(response)

    assert plan.route is not None
    assert plan.route.transition_ids == ("short-1",)


def test_plan_marks_route_fragile_when_step_failed_before() -> None:
    """当初执行就报过错的边，重跑容易走偏，只能算将就路线。"""

    plan = _plan(
        _response(
            steps=[
                _step(
                    "click-1",
                    "capture-current",
                    "capture-target",
                    reason_codes=["error:COMMAND_TARGET_SEMANTICS_UNTRUSTED"],
                )
            ]
        )
    )

    assert plan.route is not None
    assert plan.route.risk_tier is RouteRiskTier.FRAGILE
    assert plan.route.fragile_step_ids == ("click-1",)


def test_plan_accepts_audited_h5_source_rebind_as_fragile_route() -> None:
    response = _response_with_routes(
        routes=[
            {
                **_route(
                    steps=[
                        _step(
                            "action-1",
                            "capture-historical",
                            "capture-target",
                        )
                    ]
                ),
                "source_rebind": {
                    "protocol_version": "h5-page-graph-source-rebind.v1",
                    "requested_source_state_id": "capture-current",
                    "historical_source_state_id": "capture-historical",
                    "logical_page_key": "home",
                    "verified_by": "confirmed_logical_identity_and_canonical_url",
                },
            }
        ]
    )
    response["target_platform"] = "h5"

    plan = _plan(response)

    assert plan.route is not None
    assert plan.route.risk_tier is RouteRiskTier.FRAGILE
    assert plan.route.fragile_step_ids == ("action-1",)


def test_plan_rejects_h5_source_rebind_without_projection_contract() -> None:
    response = _response(
        steps=[
            _step("action-1", "capture-historical", "capture-target")
        ]
    )
    response["target_platform"] = "h5"

    with pytest.raises(EvidenceRecoveryRouteValidationError, match="不连续"):
        _plan(response)


@pytest.mark.parametrize(
    ("action_kind", "semantic_target", "blocked"),
    [
        ("navigate", "详情入口", "navigate"),
        ("input", "搜索框", "input"),
        ("click", None, "click"),
        ("click", "   ", "click"),
    ],
)
def test_plan_refuses_routes_that_can_only_be_replayed_by_coordinates(
    action_kind: str,
    semantic_target: str | None,
    blocked: str,
) -> None:
    plan = _plan(
        _response(
            steps=[
                _step(
                    "action-1",
                    "capture-current",
                    "capture-target",
                    action_kind=action_kind,
                    semantic_target=semantic_target,
                )
            ]
        )
    )

    assert plan.route is None
    assert plan.reason_code == RouteUnavailableReason.NOT_REDRIVABLE.value
    assert plan.blocked_action_kinds == (blocked,)
    assert plan.explain()["blocked_action_kinds"] == [blocked]


def test_plan_reports_target_not_reached_when_no_route_matches() -> None:
    response = _response(steps=[_step("action-1", "capture-current", "capture-target")])
    response["routes"] = [
        {
            **_route(steps=[_step("action-1", "capture-current", "capture-other")]),
            "target_state_id": "capture-other",
        }
    ]

    plan = _plan(response)

    assert plan.route is None
    assert plan.reason_code == RouteUnavailableReason.TARGET_NOT_REACHED.value


def test_planner_only_selects_actions_the_app_runtime_can_rebind() -> None:
    """跨层守卫：规划敢选的动作，执行端必须都能按语义重新绑定。"""

    from services.api.app.application.app_evidence_recovery_runtime import (
        _SUPPORTED_REPLAY_ACTIONS,
    )

    supported = {operation.value for operation in _SUPPORTED_REPLAY_ACTIONS}

    assert RECOVERY_ROUTE_REDRIVABLE_ACTIONS <= supported


def test_every_routing_reason_has_plain_language_explanation() -> None:
    """台账上能出现的每条原因都要有人话，不能一半是代码。"""

    routed_reasons = {reason.value for reason in RouteUnavailableReason} | {
        "RECAPTURE_FORBIDDEN_BY_REQUEST",
        "ORIGINAL_SURFACE_NO_LONGER_CURRENT",
        "ORIGINAL_ATOMIC_RECAPTURE_FORBIDDEN",
        "TARGET_GRAPH_STATE_UNAVAILABLE",
        "RECAPTURE_CONTEXT_UNAVAILABLE",
        "EVIDENCE_REQUEST_EXPIRED",
        "PAGE_GRAPH_ROUTE_CONTRACT_INVALID",
    }
    for code in routed_reasons:
        explained = explain_route_reason(code)
        assert explained["reason_label"] != code
        assert explained["advice"]
        assert "找研发确认" not in explained["advice"]


def test_surface_prefixed_reason_codes_share_the_plain_language_explanation() -> None:
    """H5 补采入口发的是带端前缀的原因码，界面上不能因此漏出内部代码。"""

    h5_planning_reasons = (
        "H5_TARGET_GRAPH_STATE_UNAVAILABLE",
        "H5_RECAPTURE_CONTEXT_UNAVAILABLE",
        "H5_ACTIVE_SESSION_TARGET_DRIFTED",
        "H5_RECAPTURE_SESSION_DEPENDENCY_UNSUPPORTED",
        "H5_RECAPTURE_REQUEST_CONSTRAINT_UNSUPPORTED",
    )
    for code in h5_planning_reasons:
        explained = explain_route_reason(code)
        assert explained["reason_code"] == code
        assert explained["reason_label"] != code
        assert "H5_" not in explained["reason_label"]
    assert explain_route_reason("H5_TARGET_GRAPH_STATE_UNAVAILABLE")["reason_label"] == (
        explain_route_reason("TARGET_GRAPH_STATE_UNAVAILABLE")["reason_label"]
    )
    # 没登记过的码保持原样，由调用方兜底
    assert explain_route_reason("H5_SOMETHING_NEW")["reason_label"] == "H5_SOMETHING_NEW"


@pytest.mark.parametrize(
    ("response_patch", "error"),
    [
        ({"agent_run_id": "run-other"}, "agent_run_id"),
        ({"finalization_epoch": 3}, "finalization_epoch"),
        ({"requires_stepwise_reverification": False}, "逐边复核"),
    ],
)
def test_plan_evidence_recovery_route_rejects_scope_or_reverification_drift(
    response_patch: dict[str, object],
    error: str,
) -> None:
    response = _response(steps=[_step("action-1", "capture-current", "capture-target")])
    response.update(response_patch)

    with pytest.raises(EvidenceRecoveryRouteValidationError, match=error):
        _plan(response)


@pytest.mark.parametrize(
    "steps",
    [
        [
            _step("action-1", "capture-current", "capture-middle"),
            _step("action-2", "capture-other", "capture-target"),
        ],
        [_step("action-1", "capture-current", "capture-target", risk_level="high")],
        [
            {
                **_step("action-1", "capture-current", "capture-target"),
                "proof": "capture_pair",
            }
        ],
        [
            _step("action-1", "capture-current", "capture-middle"),
            _step("action-1", "capture-middle", "capture-target"),
        ],
    ],
)
def test_plan_evidence_recovery_route_rejects_untrusted_or_invalid_steps(
    steps: list[dict[str, object]],
) -> None:
    with pytest.raises(EvidenceRecoveryRouteValidationError):
        _plan(_response(steps=steps))


def test_plan_evidence_recovery_route_explains_unreachable_graph() -> None:
    response = _response(steps=[_step("action-1", "capture-current", "capture-other")])
    response["routes"] = []
    response["status"] = "unreachable"

    plan = _plan(response)

    assert plan.route is None
    assert plan.reason_code == RouteUnavailableReason.GRAPH_NO_TRUSTED_ROUTE.value
    assert plan.explain()["reason_label"] == "页面图里没有可信的低风险历史路线"


def test_probe_work_round_trip_preserves_full_restoration_plan() -> None:
    request = _evidence_request()
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
        replay_execution_plan_id="page-graph-route:route-1",
        normalization_step_ids=("action-1", "action-2"),
        deadline=NOW + timedelta(minutes=5),
    )
    probe = build_probe_work(
        session_id="session-1",
        branch_id="probe:request-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-current",
        restoration_plan_hash=plan.plan_hash,
        restoration_plan=plan,
        restoration_attempt_id="restoration-attempt-1",
        equivalence_assessment_id="state-equivalence-1",
        source_grade=EvidenceSourceGrade.B,
        evidence_request=request,
        budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=3,
            max_depth=2,
            remaining_duration_ms=180_000,
        ),
        deadline=NOW + timedelta(minutes=5),
    )

    restored = ProbeWork.from_dict(probe.to_dict())

    assert restored.restoration_plan == plan
    assert restored.restoration_plan_hash == plan.plan_hash


def test_probe_work_rejects_restoration_plan_hash_drift() -> None:
    request = _evidence_request()
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=5),
    )

    with pytest.raises(ExplorationValidationError, match="恢复计划哈希"):
        build_probe_work(
            session_id="session-1",
            branch_id="probe:request-1",
            surface_kind=SurfaceKind.WEB,
            source_capture_id="capture-target",
            restoration_plan_hash="wrong-hash",
            restoration_plan=plan,
            restoration_attempt_id="restoration-attempt-1",
            equivalence_assessment_id="state-equivalence-1",
            source_grade=EvidenceSourceGrade.A1,
            evidence_request=request,
            budget=ExplorationBudget(
                max_actions=1,
                remaining_actions=1,
                max_depth=0,
                remaining_duration_ms=30_000,
            ),
            deadline=NOW + timedelta(minutes=5),
        )


def test_probe_hangback_reuses_routed_restoration_plan() -> None:
    request = _evidence_request()
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
        replay_execution_plan_id="page-graph-route:route-1",
        normalization_step_ids=("action-1",),
        deadline=NOW + timedelta(minutes=5),
    )

    rebuilt = _rebuild_plan(
        request=request,
        source_session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        outcome=SimpleNamespace(restoration_plan=plan),
    )

    assert rebuilt is plan


@pytest.mark.asyncio
async def test_route_one_binds_page_graph_route_to_restoration_plan() -> None:
    request = _evidence_request()
    handoff = _handoff()
    page_graph = SimpleNamespace(
        get_recovery_route=AsyncMock(
            return_value=_response(steps=[_step("action-1", "capture-current", "capture-target")])
        )
    )
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(return_value=_capture_context()),
    ):
        plan, reason_code = await _route_one(
            handoff=handoff,
            request=request,
            page_graph_service=page_graph,
        )

    assert reason_code is None
    assert plan is not None
    # 只到计划为止：路线绑定在恢复计划上，由恢复执行器逐边重放后再评估、再发布
    assert plan.strategy is RestorationStrategy.SAME_CONTEXT_NEW_SURFACE
    assert plan.original_capture_id == "capture-target"
    assert plan.source_session_id == plan.target_session_id == "session-1"
    assert plan.replay_execution_plan_id.startswith("page-graph-route:")
    assert plan.normalization_step_ids == ("action-1",)
    page_graph.get_recovery_route.assert_awaited_once_with(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=2,
        source_state_id="capture-current",
        target_identity="web:https://example.com/detail",
        limit=20,
    )


@pytest.mark.asyncio
async def test_route_one_does_not_publish_when_graph_target_is_unreachable() -> None:
    page_graph = SimpleNamespace(
        get_recovery_route=AsyncMock(
            return_value={
                **_response(steps=[]),
                "status": "unreachable",
                "routes": [],
            }
        )
    )
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(return_value=_capture_context()),
    ):
        plan, reason_code = await _route_one(
            handoff=_handoff(),
            request=_evidence_request(),
            page_graph_service=page_graph,
        )

    assert plan is None
    assert reason_code == RouteUnavailableReason.GRAPH_NO_TRUSTED_ROUTE.value


@pytest.mark.asyncio
async def test_route_one_uses_original_surface_when_target_is_current() -> None:
    context = _capture_context()
    context["current_capture_id"] = "capture-target"
    page_graph = SimpleNamespace(get_recovery_route=AsyncMock())
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(return_value=context),
    ):
        plan, reason_code = await _route_one(
            handoff=_handoff(),
            request=_evidence_request(),
            page_graph_service=page_graph,
        )

    assert reason_code is None
    assert plan is not None
    # 原 Capture 仍是当前面：不导航，但仍要恢复执行器重新观察一张新 Capture 再做等价评估
    assert plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
    assert plan.original_capture_id == "capture-target"
    assert plan.replay_execution_plan_id is None
    assert plan.normalization_step_ids == ()
    page_graph.get_recovery_route.assert_not_awaited()


def test_routing_never_fabricates_equivalence_assessment_or_publishes_probe() -> None:
    """ProbeWork 只能由 restored_probe_publication 在等价门禁通过后发布，路由层不得伪造。

    审查报告那条「ProbeWork 无法发布」的根因就是路由先构造 state-equivalence:pending:*
    再发布；数据库要求发布前已存在 equivalent 的恢复尝试，永远发不出去。守住源码本身。
    """
    source = inspect.getsource(evidence_request_routing)
    assert "state-equivalence:pending" not in source
    assert "route_evidence_request(" not in source
    assert "RouteEvidenceRequestCommand" not in source


@pytest.mark.asyncio
async def test_load_capture_context_binds_producer_run_and_graph_epoch() -> None:
    connection = SimpleNamespace(
        fetchrow=AsyncMock(
            return_value={
                "session_id": "session-1",
                "surface_kind": "web",
                "capture_agent_run_id": "run-1",
                "current_capture_id": "capture-current",
                "target_state_id": "capture-target",
                "agent_run_id": "run-1",
                "finalization_epoch": 2,
                "target_identity": "web:https://example.com/detail",
            }
        )
    )
    pool = _FakePool(connection)
    with patch(
        "services.api.app.application.evidence_request_routing.get_postgres_pool",
        new=AsyncMock(return_value=pool),
    ):
        context = await _load_capture_context(
            task_id="task-1",
            capture_id="capture-target",
            producer_agent_run_id="run-1",
        )

    assert context == _capture_context()


@pytest.mark.asyncio
async def test_load_capture_context_rejects_other_producer_run() -> None:
    connection = SimpleNamespace(
        fetchrow=AsyncMock(
            return_value={
                "session_id": "session-1",
                "surface_kind": "web",
                "capture_agent_run_id": "run-other",
                "current_capture_id": "capture-current",
                "target_state_id": "capture-target",
                "agent_run_id": "run-other",
                "finalization_epoch": 2,
                "target_identity": "web:https://example.com/detail",
            }
        )
    )
    with patch(
        "services.api.app.application.evidence_request_routing.get_postgres_pool",
        new=AsyncMock(return_value=_FakePool(connection)),
    ):
        context = await _load_capture_context(
            task_id="task-1",
            capture_id="capture-target",
            producer_agent_run_id="run-1",
        )

    assert context is None


def test_deterministic_route_unfulfillable_whitelist() -> None:
    assert is_deterministic_route_unfulfillable("RECAPTURE_FORBIDDEN_BY_REQUEST")
    assert is_deterministic_route_unfulfillable("TRUSTED_RECOVERY_ROUTE_UNAVAILABLE")
    assert not is_deterministic_route_unfulfillable("PAGE_GRAPH_ROUTE_CONTRACT_INVALID")
    assert not is_deterministic_route_unfulfillable("PAGE_GRAPH_ROUTE_TRANSIENT_FAILURE")
    assert not is_deterministic_route_unfulfillable("TARGET_GRAPH_STATE_UNAVAILABLE")
    assert not is_deterministic_route_unfulfillable(None)


@pytest.mark.asyncio
async def test_record_route_failure_keeps_pending_for_transient() -> None:
    class RealStore:
        def __init__(self) -> None:
            self.stored = SimpleNamespace(
                status=EvidenceRequestStatus.PENDING,
                row_version=1,
            )
            self.transitions: list[object] = []

        async def get(self, *, request_id: str):
            del request_id
            return self.stored

        async def transition(self, *, command):
            self.transitions.append(command)
            self.stored.status = command.target_status
            self.stored.row_version += 1
            return self.stored.row_version

    store = RealStore()
    await _record_evidence_request_route_failure(
        request_store=store,
        request_id="req-1",
        reason="PAGE_GRAPH_ROUTE_TRANSIENT_FAILURE",
        deterministic_unfulfillable=False,
    )
    assert store.transitions == []
    assert store.stored.status is EvidenceRequestStatus.PENDING

    await _record_evidence_request_route_failure(
        request_store=store,
        request_id="req-1",
        reason="RECAPTURE_FORBIDDEN_BY_REQUEST",
        deterministic_unfulfillable=True,
    )
    assert len(store.transitions) == 1
    assert store.transitions[0].target_status is EvidenceRequestStatus.UNFULFILLABLE


@pytest.mark.asyncio
async def test_plan_page_graph_route_maps_transient_exception() -> None:
    page_graph = SimpleNamespace(
        get_recovery_route=AsyncMock(side_effect=RuntimeError("page graph down"))
    )
    plan = await _plan_page_graph_route(
        context=_capture_context(),
        task_id="task-1",
        page_graph_service=page_graph,
    )
    assert plan.route is None
    assert plan.reason_code == "PAGE_GRAPH_ROUTE_TRANSIENT_FAILURE"
    assert not is_deterministic_route_unfulfillable(plan.reason_code)


@pytest.mark.asyncio
async def test_route_outcome_keeps_pending_on_page_graph_transient() -> None:
    request = _evidence_request()
    handoff = _handoff()
    outcome = RuleJudgeOutcome(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        evidence_requests=(request,),
    )

    class RealStore:
        def __init__(self) -> None:
            self.stored = SimpleNamespace(
                status=EvidenceRequestStatus.PENDING,
                row_version=1,
            )
            self.transitions: list[object] = []

        async def get(self, *, request_id: str):
            del request_id
            return self.stored

        async def transition(self, *, command):
            self.transitions.append(command)
            self.stored.status = command.target_status
            self.stored.row_version += 1
            return self.stored.row_version

    store = RealStore()
    page_graph = SimpleNamespace(
        get_recovery_route=AsyncMock(side_effect=RuntimeError("temporary outage"))
    )
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(return_value=_capture_context()),
    ):
        result = await route_outcome_evidence_requests(
            handoff=handoff,
            outcome=outcome,
            request_store=store,
            page_graph_service=page_graph,
            supplemental_evidence_policy=SupplementalEvidencePolicy(
                enabled=True,
                runtime_ready=True,
            ),
        )

    assert result.planned_request_ids == ()
    assert store.transitions == []
    assert store.stored.status is EvidenceRequestStatus.PENDING


@pytest.mark.asyncio
async def test_route_outcome_marks_unfulfillable_for_forbidden_replay() -> None:
    request = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-target",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="不可恢复缺口",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.FORBIDDEN,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/detail"}},
        minimum_source_grade=EvidenceSourceGrade.C,
        expires_at=NOW + timedelta(hours=1),
    )
    handoff = _handoff()
    outcome = RuleJudgeOutcome(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        evidence_requests=(request,),
    )

    class RealStore:
        def __init__(self) -> None:
            self.stored = SimpleNamespace(
                status=EvidenceRequestStatus.PENDING,
                row_version=1,
            )
            self.transitions: list[object] = []

        async def get(self, *, request_id: str):
            del request_id
            return self.stored

        async def transition(self, *, command):
            self.transitions.append(command)
            self.stored.status = command.target_status
            self.stored.row_version += 1
            return self.stored.row_version

    store = RealStore()
    with patch(
        "services.api.app.application.evidence_request_routing._load_capture_context",
        new=AsyncMock(
            return_value={
                "session_id": "session-1",
                "surface_kind": SurfaceKind.ANDROID,
            }
        ),
    ):
        await route_outcome_evidence_requests(
            handoff=handoff,
            outcome=outcome,
            request_store=store,
            supplemental_evidence_policy=SupplementalEvidencePolicy(
                enabled=True,
                runtime_ready=True,
            ),
        )

    assert len(store.transitions) == 1
    assert store.transitions[0].target_status is EvidenceRequestStatus.UNFULFILLABLE
    assert store.transitions[0].resolution_reason == "RECAPTURE_FORBIDDEN_BY_REQUEST"


def _evidence_request():
    return build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-target",
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="补采目标页截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(hours=1),
    )


def _handoff() -> InspectionHandoff:
    return InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="consumer-run-1",
        input_hash="input-hash-1",
        batch_key="batch-1",
        strategy="rule_engine",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-target",
            producer_agent_run_id="run-1",
        ),
        rules=(
            InspectionRuleTarget(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=1,
                contract_hash="contract-1",
            ),
        ),
    )


def _capture_context() -> dict[str, object]:
    return {
        "session_id": "session-1",
        "surface_kind": SurfaceKind.WEB,
        "agent_run_id": "run-1",
        "finalization_epoch": 2,
        "current_capture_id": "capture-current",
        "target_state_id": "capture-target",
        "target_identity": "web:https://example.com/detail",
    }


class _FakeAcquire:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def __aenter__(self) -> object:
        return self._connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakePool:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._connection)
