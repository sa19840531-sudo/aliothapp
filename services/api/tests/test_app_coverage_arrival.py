"""正式覆盖只接受可信动作到达或合法的被动观察。"""

from datetime import timedelta
from hashlib import sha256

import pytest

from services.api.app.application.app_screenshot_exploration import _next_coverage_metadata
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.surface_sessions import AppExplorationPendingFacts
from services.api.app.domain.exploration import (
    AppExplorationOutcome,
    AppExplorationTerminalEvaluator,
)
from services.api.app.domain.interaction import ActionIntent, SurfaceKind
from services.api.tests.test_app_exploration_terminal import NOW, _work
from services.api.tests.test_app_exploration_terminal import _candidate as _action
from services.api.tests.test_app_screenshot_exploration import (
    _confirmed_fixture,
    _coverage_analysis,
    _coverage_goal,
)


def _analysis(page_type, *, package_name="com.example.target"):
    """覆盖到达用例必须带已确认视觉身份，否则同 Activity 会并成一个核心键。"""
    base = _coverage_analysis(
        capture_id=f"capture-{page_type}",
        artifact_id=f"artifact-{page_type}",
        image_signature="sha256:" + sha256(page_type.encode()).hexdigest(),
        visual_page_type=page_type,
        package_name=package_name,
    )
    if package_name != "com.example.target":
        return base
    return _confirmed_fixture(
        base,
        object_id=None if page_type == "home" else f"对象-{page_type}",
    )


def _decision():
    action = _action()
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-home",
        action_type=action.action_type,
        semantic_target=action.semantic_target,
        parameters={"candidate_id": action.candidate_id},
        purpose="打开详情",
        risk_level=action.risk_level,
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID, action.action_type),
        deadline=NOW + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    "completed,attempted,succeeded,proven,decision,accepted",
    [
        pytest.param(False, True, False, False, False, False, id="failed"),
        pytest.param(True, True, False, True, False, False, id="contradictory-failure"),
        pytest.param(False, True, True, False, False, False, id="unproven-success"),
        pytest.param(True, True, True, None, False, False, id="missing-arrival-proof"),
        pytest.param(False, True, None, None, False, False, id="unknown-result"),
        pytest.param(False, True, True, True, False, False, id="missing-completed-action"),
        pytest.param(False, False, None, None, True, False, id="decision-without-result"),
        pytest.param(True, True, True, True, True, True, id="trusted-arrival"),
        pytest.param(False, False, None, None, False, True, id="passive-observation"),
    ],
)
def test_coverage_metadata_and_terminal_require_trusted_arrival(
    completed, attempted, succeeded, proven, decision, accepted
):
    goal = _coverage_goal(required_milestones=("surface:stable", "journey:details", "app_ready"))
    before = _analysis("home")
    prior = _next_coverage_metadata(goal=goal, analysis=before)
    metadata = _next_coverage_metadata(
        goal=goal,
        analysis=_analysis("content_detail"),
        prior_metadata=prior,
        prior_state_version=before.image_signature,
        evidence_terms=("details", "app_ready"),
        pending_facts=AppExplorationPendingFacts(
            task_id="task-1",
            session_id="session-1",
            pending_action_count=0,
            pending_evidence_count=0,
        ),
        completed_action=_action() if completed else None,
        attempted_action=_action() if attempted else None,
        action_succeeded=succeeded,
        arrival_proven=proven,
        action_decision=_decision() if decision else None,
    )

    # 拒绝增加覆盖不等于删除新画面的诊断信息。
    assert metadata["business_page_type"] == "content_detail"
    assert metadata["pending_facts_available"] is True
    assert metadata["covered_business_page_count"] == (2 if accepted else 1)
    if accepted:
        assert metadata["covered_page_types"] == ["home", "content_detail"]
        assert metadata["covered_milestones"] == list(goal.required_milestones)
        assert len(metadata["covered_business_pages"]) == 2
        assert len(metadata["covered_business_state_ids"]) == 2
        assert len(metadata["covered_business_evidence_ids"]) == 2
    else:
        for field in (
            "covered_page_types",
            "covered_page_type_count",
            "covered_milestones",
            "covered_business_pages",
            "covered_business_state_ids",
            "covered_business_evidence_ids",
        ):
            assert metadata[field] == prior[field], field

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=goal, page_type="content_detail", metadata=metadata),
        eligible_candidate_count=0,
    )
    assert evaluation.coverage.covered_business_page_count == (2 if accepted else 1)
    assert evaluation.coverage.missing_page_types == (() if accepted else ("content_detail",))
    assert evaluation.outcome is (
        AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    )
    # 可信到达不代替支付、视频等其他必达页型，不放松新增流程门禁。
    assert evaluation.coverage.flow_contract_complete is False
    assert {"payment", "video_detail"} <= set(
        metadata["app_flow_contract"]["missing_page_types"]
    )


def test_all_mandatory_arrivals_complete_without_removing_payment_or_video():
    goal = _coverage_goal(required_milestones=("surface:stable", "journey:details", "app_ready"))
    metadata = None
    before = _analysis("home")
    for page_type in (
        "home", "content_detail", "video_detail", "payment", "post_publish", "settings",
    ):
        metadata = _next_coverage_metadata(
            goal=goal, analysis=_analysis(page_type), prior_metadata=metadata,
            evidence_terms=("details", "app_ready"),
            pending_facts=AppExplorationPendingFacts(
                task_id="task-1", session_id="session-1", pending_action_count=0,
                pending_evidence_count=0,
            ),
            prior_state_version=before.image_signature if page_type == "content_detail" else None,
            completed_action=_action() if page_type == "content_detail" else None,
            attempted_action=_action() if page_type == "content_detail" else None,
            action_succeeded=True if page_type == "content_detail" else None,
            arrival_proven=True if page_type == "content_detail" else None,
            action_decision=_decision() if page_type == "content_detail" else None,
        )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=goal, page_type="settings", metadata=metadata), eligible_candidate_count=0,
    )
    assert metadata["app_flow_contract"]["missing_page_types"] == []
    assert evaluation.coverage.flow_contract_complete is True
    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED


@pytest.mark.parametrize("trusted", [False, True])
def test_lifecycle_and_app_ready_coverage_require_trusted_observation(trusted):
    goal = _coverage_goal(required_page_types=("startup_ad",), required_milestones=("app_ready",))
    metadata = _next_coverage_metadata(
        goal=goal,
        analysis=_analysis("startup_ad"),
        evidence_terms=("app_ready",),
        attempted_action=None if trusted else _action(),
        action_succeeded=None if trusted else False,
    )
    assert metadata["covered_page_types"] == (["startup_ad"] if trusted else [])
    assert metadata["covered_milestones"] == (["app_ready"] if trusted else [])
    assert metadata["covered_business_page_count"] == 0


def test_external_passive_observation_does_not_claim_app_ready_or_business_coverage():
    metadata = _next_coverage_metadata(
        goal=_coverage_goal(required_milestones=("app_ready",)),
        analysis=_analysis("content_detail", package_name="com.external.browser"),
        evidence_terms=("app_ready",),
    )
    assert metadata["covered_page_types"] == []
    assert metadata["covered_milestones"] == []
    assert metadata["covered_business_page_count"] == 0
