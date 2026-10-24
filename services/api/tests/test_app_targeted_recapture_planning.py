"""App 定向补采计划解析测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.app_targeted_recapture_planning import (
    AppTargetedRecaptureContext,
    AppTargetedRecapturePlanService,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationStrategy,
    build_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 9, 8, 18, tzinfo=UTC)


def _request(
    *,
    replay_policy: EvidenceReplayPolicy = EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
    equivalence: EvidenceRequiredEquivalence = EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
):
    return build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="rule_1",
        target_capture_id="capture-target",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采详情页截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=replay_policy,
        required_equivalence=equivalence,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"page_type": "content_detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=30),
    )


def _context(*, current_capture_id: str = "capture-current", agent_run_id: str = "run-1"):
    return AppTargetedRecaptureContext(
        task_id="task-1",
        agent_run_id=agent_run_id,
        finalization_epoch=3,
        source_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        current_capture_id=current_capture_id,
        target_state_id="capture-target",
        target_identity="app:com.example:content-detail",
    )


def _route_response():
    return {
        "protocol_version": "page_graph_recovery.v1",
        "status": "available",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "finalization_epoch": 3,
        "requires_stepwise_reverification": True,
        "routes": [
            {
                "route_id": "ignored-source-id",
                "source_state_id": "capture-current",
                "target_state_id": "capture-target",
                "target_page_key": "app:com.example:content-detail",
                "steps": [
                    {
                        "transition_id": "transition-1",
                        "source_state_id": "capture-current",
                        "target_state_id": "capture-target",
                        "status": "completed",
                        "proof": "trusted",
                        "risk_level": "low",
                        "replay_eligible": True,
                        "action_kind": "click",
                        "semantic_target": "内容详情入口",
                        "reason_codes": [],
                    }
                ],
            }
        ],
    }


async def test_current_target_uses_original_surface_without_graph() -> None:
    contexts = SimpleNamespace(
        read=AsyncMock(return_value=_context(current_capture_id="capture-target"))
    )
    graph = SimpleNamespace(get_recovery_route=AsyncMock())
    resolution = await AppTargetedRecapturePlanService(
        contexts=contexts,
        page_graph=graph,
        clock=lambda: NOW,
    ).resolve(request=_request(), agent_run_id="run-1", finalization_epoch=3)

    assert resolution.executable
    assert resolution.plan is not None
    assert resolution.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
    assert resolution.plan.replay_execution_plan_id is None
    graph.get_recovery_route.assert_not_awaited()


async def test_moved_target_uses_trusted_same_session_route() -> None:
    graph = SimpleNamespace(get_recovery_route=AsyncMock(return_value=_route_response()))
    resolution = await AppTargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_context())),
        page_graph=graph,
        clock=lambda: NOW,
    ).resolve(request=_request(), agent_run_id="run-1", finalization_epoch=3)

    assert resolution.executable
    assert resolution.plan is not None
    assert resolution.plan.strategy is RestorationStrategy.SAME_APP_SESSION
    assert resolution.plan.normalization_step_ids == ("transition-1",)
    assert resolution.plan.source_session_id == resolution.plan.target_session_id


async def test_route_without_semantic_target_is_not_planned() -> None:
    """没有语义落点的边只能照历史坐标点，规划阶段就该挡住。"""

    response = _route_response()
    response["routes"][0]["steps"][0]["semantic_target"] = None
    resolution = await AppTargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_context())),
        page_graph=SimpleNamespace(get_recovery_route=AsyncMock(return_value=response)),
        clock=lambda: NOW,
    ).resolve(request=_request(), agent_run_id="run-1", finalization_epoch=3)

    assert not resolution.executable
    assert resolution.reason_code == "RECOVERY_ROUTE_NOT_REDRIVABLE"


async def test_same_surface_request_becomes_unfulfillable_after_surface_moves() -> None:
    graph = SimpleNamespace(get_recovery_route=AsyncMock())
    resolution = await AppTargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_context())),
        page_graph=graph,
        clock=lambda: NOW,
    ).resolve(
        request=_request(
            replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
            equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        ),
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    assert not resolution.executable
    assert resolution.plan is not None
    assert resolution.plan.strategy is RestorationStrategy.UNFULFILLABLE
    assert resolution.reason_code == "ORIGINAL_SURFACE_NO_LONGER_CURRENT"
    graph.get_recovery_route.assert_not_awaited()


async def test_invalid_graph_contract_does_not_create_executable_plan() -> None:
    response = _route_response()
    response["agent_run_id"] = "another-run"
    resolution = await AppTargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_context())),
        page_graph=SimpleNamespace(get_recovery_route=AsyncMock(return_value=response)),
        clock=lambda: NOW,
    ).resolve(request=_request(), agent_run_id="run-1", finalization_epoch=3)

    assert resolution.plan is None
    assert resolution.reason_code == "PAGE_GRAPH_ROUTE_CONTRACT_INVALID"


async def test_unavailable_graph_becomes_unfulfillable_plan() -> None:
    resolution = await AppTargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_context())),
        page_graph=SimpleNamespace(
            get_recovery_route=AsyncMock(
                return_value={"status": "unavailable", "routes": []}
            )
        ),
        clock=lambda: NOW,
    ).resolve(request=_request(), agent_run_id="run-1", finalization_epoch=3)

    assert not resolution.executable
    assert resolution.plan is not None
    assert resolution.plan.strategy is RestorationStrategy.UNFULFILLABLE
    assert resolution.reason_code == "TRUSTED_RECOVERY_ROUTE_UNAVAILABLE"


async def test_expired_request_stops_before_context_lookup() -> None:
    contexts = SimpleNamespace(read=AsyncMock())
    request = _request()
    resolution = await AppTargetedRecapturePlanService(
        contexts=contexts,
        page_graph=SimpleNamespace(),
        clock=lambda: request.expires_at,
    ).resolve(request=request, agent_run_id="run-1", finalization_epoch=3)

    assert resolution.plan is None
    assert resolution.reason_code == "EVIDENCE_REQUEST_EXPIRED"
    contexts.read.assert_not_awaited()


async def test_context_from_another_run_is_rejected() -> None:
    service = AppTargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=_context(agent_run_id="run-2"))),
        page_graph=SimpleNamespace(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="agent_run_id"):
        await service.resolve(
            request=_request(),
            agent_run_id="run-1",
            finalization_epoch=3,
        )
