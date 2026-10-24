"""H5 覆盖快照的根页面、缺失作用域与修订测试。"""

import pytest

from services.api.app.domain.exploration.h5_coverage import H5CoverageSnapshot
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel


def _assessment(*, status: str = "covered") -> dict[str, object]:
    return {
        "execution_status": "accepted",
        "page_type_assessment": {"page_type": "home", "status": status},
    }


def _assessment_for(page_type: str, *, status: str = "covered") -> dict[str, object]:
    return {
        "execution_status": "accepted",
        "page_type_assessment": {"page_type": page_type, "status": status},
    }


def test_root_without_action_completes_when_assessment_scope_is_covered() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home"]},
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/#/home",
        capture_id="capture-root",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.frontier_terminal is True
    assert coverage.assessment_scope_ready is True
    assert coverage.missing_page_types == ()
    assert coverage.completion_ready is True
    assert coverage.target_coverage_ready is True


def test_dynamic_scope_freezes_discovered_business_page_types() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": []},
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/#/discovered-home",
        capture_id="capture-root",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.required_page_types == ("home",)
    assert coverage.covered_page_types == ("home",)
    assert coverage.missing_page_types == ()
    assert coverage.frontier_terminal is True
    assert coverage.assessment_scope_ready is True
    assert coverage.completion_ready is True
    assert coverage.target_coverage_ready is True


def test_dynamic_scope_does_not_complete_without_discovered_business_page_type() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": []},
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/#/guide",
        capture_id="capture-guide",
        page_type="guide_page",
        page_state="stable",
        assessment=_assessment_for("guide_page"),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.required_page_types == ()
    assert coverage.covered_page_types == ()
    assert coverage.frontier_terminal is True
    assert coverage.assessment_scope_ready is True
    assert coverage.completion_ready is False
    assert coverage.target_coverage_ready is False


def test_completed_target_scope_can_finish_with_explainable_frontier_blockers() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=1,
        sampling_policy_summary={"include_page_types": ["home"]},
    ).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            CandidateAction(
                candidate_id="deep-settings",
                semantic_target="设置",
                action_type=InteractionOperation.TAP,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=80,
                navigation_depth=3,
                metadata={"expected_page_type": "settings"},
            ),
        ),
        max_depth=1,
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-home",
        page_type="home",
        page_state="stable",
        assessment=_assessment_for("home"),
        requires_auth=False,
        overlay_present=False,
    )

    assert frontier.pending_count == 0
    assert frontier.blocked_count == 1
    assert coverage.frontier_terminal is True
    assert coverage.assessment_scope_ready is True
    assert coverage.missing_page_types == ()
    assert coverage.completion_ready is True


def test_incomplete_lifecycle_scope_does_not_block_covered_business_scope() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": []},
    )
    loading = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/loading",
        capture_id="capture-loading",
        page_type="unknown",
        page_state="loading",
        assessment={"execution_status": "blocked"},
        requires_auth=False,
        overlay_present=False,
    )
    coverage = H5CoverageSnapshot.advance(
        previous=loading.to_dict(),
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-home",
        page_type="home",
        page_state="stable",
        assessment=_assessment_for("home"),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.required_page_types == ("home",)
    assert coverage.assessment_scope_ready is True
    assert coverage.completion_ready is True


def test_terminal_settlement_clears_selected_and_pending_entries() -> None:
    candidates = tuple(
        CandidateAction(
            candidate_id=f"candidate-{index}",
            semantic_target=f"入口 {index}",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=80,
            navigation_depth=1,
        )
        for index in range(3)
    )
    frontier = H5TaskFrontierSnapshot.create(max_pages=3).merge(
        source_capture_id="capture-root",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=candidates,
    )
    frontier = frontier.select_entry(frontier.pending_entries[0].entry_id)

    successful = frontier.settle_remaining(
        successful=True,
        reason="h5_terminal_objective_satisfied",
    )
    reviewed = frontier.settle_remaining(
        successful=False,
        reason="h5_terminal_review",
    )

    assert successful.pending_count == 0
    assert {entry.status.value for entry in successful.entries} == {"skipped"}
    assert reviewed.pending_count == 0
    assert reviewed.blocked_count == 1
    assert [entry.status.value for entry in reviewed.entries].count("skipped") == 2


def test_missing_required_page_type_and_incomplete_assessment_fail_closed() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home", "payment"]},
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-root",
        page_type="home",
        page_state="stable",
        assessment={"execution_status": "blocked", "error_code": "ASSESSMENT_INCOMPLETE"},
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.assessment_scope_ready is False
    assert coverage.missing_page_types == ("home", "payment")
    assert coverage.completion_ready is False
    assert coverage.target_coverage_ready is False


def test_target_coverage_ready_can_be_true_before_frontier_terminal() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home"]},
    )
    candidate = CandidateAction(
        candidate_id="candidate-profile",
        semantic_target="我的",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        metadata={"expected_next_page_type": "home"},
    )
    frontier = frontier.merge(
        source_capture_id="capture-root",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-root",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.frontier_terminal is False
    assert coverage.completion_ready is False
    assert coverage.target_coverage_ready is True


@pytest.mark.parametrize("terminal_method", ["mark_blocked", "mark_failed"])
def test_terminal_frontier_failure_does_not_leave_coverage_active(
    terminal_method: str,
) -> None:
    """已审计的阻断或失败候选是终态事实，不能让 Frontier 永久未完成。"""
    candidate = CandidateAction(
        candidate_id="candidate-profile",
        semantic_target="个人中心",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        metadata={"expected_next_page_type": "home"},
    )
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home"]},
    ).merge(
        source_capture_id="capture-root",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry_id = frontier.pending_entries[0].entry_id
    frontier = frontier.select_entry(entry_id)
    frontier = getattr(frontier, terminal_method)(
        entry_id,
        reason="command_policy_blocked",
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-root",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.frontier_terminal is True
    assert coverage.completion_ready is False
    assert frontier.blocked_count + frontier.failed_count == 1


def test_policy_skipped_candidate_does_not_create_completion_gap() -> None:
    """明确排除的候选不执行但也不能伪造成覆盖阻断。"""
    candidate = CandidateAction(
        candidate_id="candidate-payment",
        semantic_target="充值",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        metadata={"expected_next_page_type": "payment"},
    )
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"exclude_page_types": ["payment"]},
    ).merge(
        source_capture_id="capture-root",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-root",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )

    assert frontier.pending_count == 0
    assert frontier.blocked_count == 0
    assert frontier.failed_count == 0
    assert coverage.frontier_terminal is True
    assert coverage.completion_ready is True


def test_high_risk_candidate_remains_a_completion_blocker() -> None:
    """安全策略拒绝执行高风险动作时，任务必须进入复核而不是伪造完成。"""
    candidate = CandidateAction(
        candidate_id="candidate-payment",
        semantic_target="充值",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.HIGH,
        exploration_value=80,
        navigation_depth=1,
        metadata={"expected_next_page_type": "payment"},
    )
    frontier = H5TaskFrontierSnapshot.create(max_pages=3).merge(
        source_capture_id="capture-root",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    coverage = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-root",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )

    assert frontier.blocked_count == 1
    assert frontier.entries[0].last_failure_reason == "risk_level_not_allowed"
    assert coverage.frontier_terminal is True
    assert coverage.completion_ready is False


def test_equivalent_route_replay_keeps_revision_and_hash() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home"]},
    )
    first = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="HTTPS://EXAMPLE.TEST:443/home/?b=2&a=1#anchor",
        capture_id="capture-1",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )
    replay = H5CoverageSnapshot.advance(
        previous=first.to_dict(),
        frontier=frontier,
        location="https://example.test/home?a=1&b=2",
        capture_id="capture-1",
        page_type="home",
        page_state="stable",
        assessment=_assessment(),
        requires_auth=False,
        overlay_present=False,
    )

    assert replay.revision == first.revision
    assert replay.payload_hash == first.payload_hash


def test_lifecycle_not_applicable_is_kept_as_scope_but_not_business_coverage() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={
            "include_page_types": ["home", "startup_ad", "line_loading"],
        },
    )
    first = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-home",
        page_type="home",
        page_state="stable",
        assessment=_assessment_for("home"),
        requires_auth=False,
        overlay_present=False,
    )
    coverage = H5CoverageSnapshot.advance(
        previous=first.to_dict(),
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-startup-ad",
        page_type="startup_ad",
        page_state="startup_ad",
        assessment=_assessment_for("startup_ad", status="not_applicable"),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.required_page_types == ("home",)
    assert coverage.covered_page_types == ("home",)
    assert coverage.missing_page_types == ()
    assert any(scope["page_type"] == "startup_ad" for scope in coverage.scopes)


def test_business_not_applicable_resolves_required_type_without_counting_covered() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home", "payment"]},
    )
    first = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-home",
        page_type="home",
        page_state="stable",
        assessment=_assessment_for("home"),
        requires_auth=False,
        overlay_present=False,
    )
    coverage = H5CoverageSnapshot.advance(
        previous=first.to_dict(),
        frontier=frontier,
        location="https://example.test/payment-unavailable",
        capture_id="capture-payment-na",
        page_type="payment",
        page_state="stable",
        assessment=_assessment_for("payment", status="not_applicable"),
        requires_auth=False,
        overlay_present=False,
    )

    assert coverage.covered_page_types == ("home",)
    assert coverage.missing_page_types == ()
    assert coverage.completion_ready is True


def test_stronger_capture_promotes_same_scope_to_covered() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    )
    initial = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/movie?vid=1&uid=2",
        capture_id="capture-low",
        page_type="video_detail",
        page_state="entitlement_locked",
        assessment=_assessment_for("video_detail", status="evidence_insufficient"),
        requires_auth=False,
        overlay_present=False,
    )

    promoted = initial.promote_assessment(
        location="https://example.test/movie?uid=2&vid=1",
        capture_id="capture-high",
        page_type="video_detail",
        page_state="entitlement_locked",
        assessment=_assessment_for("video_detail"),
        detection_confidence=0.92,
        completion_allowed=True,
    )

    assert promoted.revision == initial.revision + 1
    assert promoted.covered_page_types == ("video_detail",)
    assert promoted.missing_page_types == ()
    assert promoted.assessment_scope_ready is True
    assert promoted.completion_ready is True
    assert promoted.scopes[0]["page_capture_id"] == "capture-high"
    assert promoted.scopes[0]["detection_confidence"] == 0.92


def test_weaker_capture_cannot_replace_covered_authority() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    )
    initial = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/movie?vid=1",
        capture_id="capture-covered",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail"),
        requires_auth=False,
        overlay_present=False,
    )

    replay = initial.promote_assessment(
        location="https://example.test/movie?vid=1",
        capture_id="capture-low",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail", status="evidence_insufficient"),
        detection_confidence=0.42,
        completion_allowed=True,
    )

    assert replay is initial
    assert replay.scopes[0]["page_capture_id"] == "capture-covered"


def test_advance_cannot_replace_covered_scope_with_weaker_reobservation() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    )
    initial = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/movie?vid=1",
        capture_id="capture-covered",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail"),
        requires_auth=False,
        overlay_present=False,
    )

    replay = H5CoverageSnapshot.advance(
        previous=initial.to_dict(),
        frontier=frontier,
        location="https://example.test/movie?vid=1",
        capture_id="capture-low",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail", status="evidence_insufficient"),
        requires_auth=False,
        overlay_present=False,
    )

    assert replay.covered_page_types == ("video_detail",)
    assert replay.assessment_scope_ready is True
    assert replay.scopes[0]["page_capture_id"] == "capture-covered"


def test_legacy_covered_scope_is_not_rewritten_only_to_add_confidence() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    )
    initial = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/movie?vid=1",
        capture_id="capture-covered",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail"),
        requires_auth=False,
        overlay_present=False,
    )

    replay = initial.promote_assessment(
        location="https://example.test/movie?vid=1",
        capture_id="capture-later",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail"),
        detection_confidence=0.96,
        completion_allowed=True,
    )

    assert replay is initial


def test_promotion_does_not_hide_frontier_blockers() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    )
    initial = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/movie",
        capture_id="capture-low",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail", status="evidence_insufficient"),
        requires_auth=False,
        overlay_present=False,
    )

    promoted = initial.promote_assessment(
        location="https://example.test/movie",
        capture_id="capture-high",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail"),
        detection_confidence=0.92,
        completion_allowed=False,
    )

    assert promoted.missing_page_types == ()
    assert promoted.assessment_scope_ready is True
    assert promoted.completion_ready is False


def test_selected_scope_keeps_transit_pages_out_of_coverage_and_completion() -> None:
    """只选详情页时，经过的首页和列表页只留记录：不算已覆盖，评估没结论也不挡完成。"""
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=None,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    )
    home = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/home",
        capture_id="capture-home",
        page_type="home",
        page_state="stable",
        assessment=_assessment_for("home"),
        requires_auth=False,
        overlay_present=False,
    )
    video_list = H5CoverageSnapshot.advance(
        previous=home.to_dict(),
        frontier=frontier,
        location="https://example.test/list",
        capture_id="capture-list",
        page_type="video_list",
        page_state="stable",
        assessment={"execution_status": "blocked"},
        requires_auth=False,
        overlay_present=False,
    )

    assert video_list.covered_page_types == ()
    assert video_list.missing_page_types == ("video_detail",)
    assert video_list.assessment_scope_ready is True
    assert video_list.completion_ready is False

    detail = H5CoverageSnapshot.advance(
        previous=video_list.to_dict(),
        frontier=frontier,
        location="https://example.test/detail/1",
        capture_id="capture-detail",
        page_type="video_detail",
        page_state="stable",
        assessment=_assessment_for("video_detail"),
        requires_auth=False,
        overlay_present=False,
    )

    assert detail.required_page_types == ("video_detail",)
    assert detail.covered_page_types == ("video_detail",)
    assert detail.completion_ready is True
    assert {scope["page_type"] for scope in detail.scopes} == {
        "home",
        "video_list",
        "video_detail",
    }
    assert (
        detail.reconcile_frontier(frontier=frontier, requires_auth=False, overlay_present=False)
        is detail
    )
    promoted = detail.promote_assessment(
        location="https://example.test/list",
        capture_id="capture-list-strong",
        page_type="video_list",
        page_state="stable",
        assessment=_assessment_for("video_list"),
        detection_confidence=0.95,
        completion_allowed=True,
        include_page_types=("video_detail",),
    )
    assert promoted.covered_page_types == ("video_detail",)
    assert promoted.completion_ready is True
