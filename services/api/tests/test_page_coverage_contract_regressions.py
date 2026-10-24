"""页面覆盖契约的高风险回归测试。"""

from services.api.app.application.page_exploration_profile import (
    build_h5_exploration_profile,
)
from services.api.app.application.page_exploration_summary import (
    build_page_exploration_summary,
)
from services.api.app.domain.page.catalog import load_default_page_type_catalog

_BUSINESS_PAGE_TYPES = tuple(
    page_type.code
    for page_type in load_default_page_type_catalog().page_types
    if page_type.coverage_required
)


def test_business_structure_with_local_loading_is_not_line_loading_shell() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><main class="home-page">'
        '<div class="video-list"><div class="media-item">内容卡片</div></div>'
        '<div class="loading-page"><span>loading</span></div>'
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
        '<div class="van-tabbar-item">视频</div>'
        "</div></main></body></html>",
        page_type="home",
    )

    assert profile.business_structure_present is True
    assert profile.shell == "stable"


def test_business_loading_state_is_observed_without_counting_line_loading() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-home",
                "artifact_ids": ["artifact-home"],
                "page_type": "home",
                "page_state": "line_loading",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            }
        ],
        actions=[],
    )

    lifecycle = summary["coverage"]["lifecycle_shells"]
    assert lifecycle["strict_line_loading_observed"] is False
    assert lifecycle["loading_state_observed"] is True


def test_scope_complete_uses_discovered_business_page_types_when_unlocked() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-home",
                "artifact_ids": ["artifact-home"],
                "page_type": "home",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            }
        ],
        actions=[],
        page_type_assessments=[
            {
                "page_type": "home",
                "status": "covered",
                "reason_code": "recognized_with_evidence",
                "page_capture_ids": ["cap-home"],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
            }
        ],
        assessment_scope_complete=True,
    )

    product = summary["coverage"]["product_business"]
    assert product["assessment_scope_complete"] is True
    assert product["assessment_scope_verified"] is True
    assert product["assessment_complete"] is True
    assert product["coverage_rate"] == 1.0
    assert product["missing_assessment_page_types"] == []


def test_h5_locked_required_page_types_define_business_denominator() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-home",
                "artifact_ids": ["artifact-home"],
                "page_type": "home",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            },
            {
                "page_capture_id": "cap-list",
                "artifact_ids": ["artifact-list"],
                "page_type": "content_list",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            },
        ],
        actions=[],
        page_type_assessments=[
            {
                "page_type": "home",
                "status": "covered",
                "reason_code": "recognized_with_evidence",
                "page_capture_ids": ["cap-home"],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
            },
            {
                "page_type": "content_list",
                "status": "covered",
                "reason_code": "recognized_with_evidence",
                "page_capture_ids": ["cap-list"],
                "evidence_artifact_ids": ["artifact-list"],
                "exploration_action_ids": [],
            },
        ],
        assessment_scope_complete=True,
        required_page_types=["home", "content_list"],
    )

    product = summary["coverage"]["product_business"]
    assert product["assessment_scope_verified"] is True
    assert product["assessment_complete"] is True
    assert product["missing_assessment_page_types"] == []
    assert product["applicable_page_types"] == ["home", "content_list"]
    assert product["coverage_rate"] == 1.0


def test_empty_business_frontier_keeps_acceptance_unknown() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-home",
                "artifact_ids": ["artifact-home"],
                "page_type": "home",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            }
        ],
        actions=[],
        page_type_assessments=_complete_assessments(),
        assessment_scope_complete=True,
    )

    coverage = summary["coverage"]
    assert coverage["product_business"]["coverage_rate"] == 1.0
    assert coverage["exploration"]["terminal_rate"] is None
    assert coverage["acceptance_complete"] is None


def test_covered_assessment_requires_capture_artifact_pair() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-a",
                "artifact_ids": ["artifact-a"],
                "page_type": "home",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            },
            {
                "page_capture_id": "cap-b",
                "artifact_ids": ["artifact-b"],
                "page_type": "home",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            },
        ],
        actions=[],
        page_type_assessments=[
            {
                "page_type": "home",
                "status": "covered",
                "reason_code": "recognized_with_evidence",
                "page_capture_ids": ["cap-a"],
                "evidence_artifact_ids": ["artifact-b"],
                "exploration_action_ids": [],
            }
        ],
    )

    assert summary["coverage"]["gaps"][0]["status"] == "evidence_insufficient"
    assert summary["coverage"]["gaps"][0]["reason_code"] == ("covered_without_matching_evidence")


def test_evidence_only_pages_have_independent_count() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-evidence",
                "artifact_ids": ["artifact-evidence"],
                "page_type": "content_detail",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "evidence_only",
                "inspection_status": "completed",
            }
        ],
        actions=[],
    )

    assert summary["active_page_count"] == 0
    assert summary["evidence_only_page_count"] == 1


def _complete_assessments() -> list[dict[str, object]]:
    assessments: list[dict[str, object]] = []
    for page_type in _BUSINESS_PAGE_TYPES:
        if page_type == "home":
            assessments.append(
                {
                    "page_type": page_type,
                    "status": "covered",
                    "reason_code": "recognized_with_evidence",
                    "page_capture_ids": ["cap-home"],
                    "evidence_artifact_ids": ["artifact-home"],
                    "exploration_action_ids": [],
                }
            )
            continue
        assessments.append(
            {
                "page_type": page_type,
                "status": "not_applicable",
                "reason_code": "no_product_entry_in_full_observation",
                "page_capture_ids": ["cap-home"],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
            }
        )
    return assessments
