"""H5 详情路由降级与到达页型一致性回归测试。"""

from dataclasses import replace

import pytest

from services.api.app.agents.tools.page_detection_tools import (
    _deterministic_page_type_from_business_route,
)
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5FrontierEntryStatus,
    H5TaskFrontierSnapshot,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.test_h5_exploration_workflow import (
    _AssessmentRepository,
    _Bootstrapper,
    _command,
    _coordination,
    _Detector,
    _Orchestrator,
    _workflow_module,
)


def test_movie_details_route_is_a_deterministic_video_detail_fallback() -> None:
    """模型超时后必须能用该站明确详情路由恢复页型。"""
    match = _deterministic_page_type_from_business_route(
        "https://dvgsyzqus02v5.cloudfront.net/"
        "movieDetails?vid=688dcea0876272a8767175c3&uid=100001"
    )

    assert match is not None
    assert match.page_type == "video_detail"
    assert match.dom_hint == "business_route:moviedetails"


@pytest.mark.asyncio
async def test_unexpected_arrival_records_actual_type_without_covering_expected_type() -> None:
    """预期页型不准时只覆盖实际落地页，不得虚记原预期页型。"""
    module = _workflow_module()
    workflow = module.H5ExplorationWorkflow(
        bootstrapper=_Bootstrapper(),
        detector=_Detector(),
        orchestrator=_Orchestrator(),
        assessment_repository=_AssessmentRepository([]),
    )
    initial = await workflow.run(replace(_command(), sampling_policy_summary={}))
    candidate = initial.work.frontier.candidates[0]
    turn = ExplorerTurnResult(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        decision=ActionIntent(
            task_id=initial.work.task_id,
            session_id=initial.work.session_id,
            intent_id="intent-unexpected-arrival",
            idempotency_key="intent-unexpected-arrival",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=initial.work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            purpose="验证到达页型一致性",
            capture_profile=module.H5SurfaceBootstrapper.capture_profile(),
            deadline=initial.work.deadline,
            parameters={"candidate_id": candidate.candidate_id},
            risk_level=InteractionRiskLevel.LOW,
        ),
        coordination=_coordination(
            before="capture-before-unexpected-arrival",
            after="capture-unexpected-arrival",
            source=initial.work.source_capture_id,
        ),
    )

    successor = await workflow.prepare_after_action(
        module.ContinueH5ExplorationCommand(prior_work=initial.work, turn=turn)
    )
    snapshot = H5TaskFrontierSnapshot.from_dict(
        successor.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    settled = next(
        entry
        for entry in snapshot.entries
        if entry.candidate.candidate_id == candidate.candidate_id
    )

    assert settled.status is H5FrontierEntryStatus.EXECUTED
    assert settled.last_failure_reason == (
        "arrival_page_type_mismatch:expected=profile:actual=home"
    )
    assert settled.candidate.metadata["declared_expected_next_page_type"] == "profile"
    assert settled.candidate.metadata["expected_next_page_type"] == "home"
    assert settled.candidate.metadata["observed_next_page_type"] == "home"
    assert "home" in snapshot.covered_page_types
    assert "profile" not in snapshot.covered_page_types
