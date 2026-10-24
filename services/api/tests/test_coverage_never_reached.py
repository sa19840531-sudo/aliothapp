"""声明为目标却没走到的页型：算未覆盖并进分母，不再让比率留空。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.page_exploration_summary import (
    build_page_exploration_summary,
)


def _capture(capture_id: str, page_type: str) -> dict[str, Any]:
    return {
        "page_capture_id": capture_id,
        "page_type": page_type,
        "page_state": "stable",
        "inspection_status": "completed",
        "detection_status": "recognized",
        "artifact_ids": [f"artifact-{capture_id}"],
        "capture_role": "after_action",
    }


def _covered(capture_id: str, page_type: str) -> dict[str, Any]:
    return {
        "page_type": page_type,
        "status": "covered",
        "reason_code": "h5_page_detection_covered",
        "page_capture_ids": [capture_id],
        "evidence_artifact_ids": [f"artifact-{capture_id}"],
        "exploration_action_ids": [],
    }


def _summary(required: list[str], *, scope_complete: bool = True):
    captures = [_capture("cap-home", "home"), _capture("cap-list", "content_list")]
    assessments = [_covered("cap-home", "home"), _covered("cap-list", "content_list")]
    return build_page_exploration_summary(
        pages=captures,
        actions=[],
        page_type_assessments=assessments,
        assessment_scope_complete=scope_complete,
        required_page_types=required,
    )["coverage"]["product_business"]


def test_never_reached_target_counts_as_known_uncovered() -> None:
    """整轮没走到的目标页型要进分母并记「已知未覆盖」。

    此前它们只落在 missing_assessment_page_types，既不进分母也不进任何
    结论桶，于是 assessment_scope_verified 恒假、coverage_rate 永远留空
    —— 探索越不完整，那个唯一对外的比率越是空的。
    """
    result = _summary(["home", "content_list", "video_detail"])

    assert result["covered_count"] == 2
    assert result["applicable_count"] == 3
    assert result["coverage_rate"] == 0.6667
    assert result["known_uncovered_page_types"] == ["video_detail"]
    assert result["unresolved_page_types"] == []


def test_declared_scope_decides_the_denominator() -> None:
    """产品实际没有的页型不写进声明，就不会拉低覆盖率。

    实测十八汇（SF-129）：有视频详情页、没有发帖页。按完整目录 11 个页型
    算是 9/11，按产品实际 10 个页型算是 9/10 —— 分母由声明决定。
    """
    with_extra = _summary(["home", "content_list", "video_detail", "post_publish"])
    without_extra = _summary(["home", "content_list", "video_detail"])

    assert with_extra["applicable_count"] == 4
    assert without_extra["applicable_count"] == 3
    assert without_extra["coverage_rate"] > with_extra["coverage_rate"]


def test_not_applicable_stays_out_of_the_denominator() -> None:
    """判定为本产品不适用的页型仍不进分母。"""
    captures = [_capture("cap-home", "home")]
    assessments = [
        _covered("cap-home", "home"),
        {
            "page_type": "payment",
            "status": "not_applicable",
            "reason_code": "product_has_no_payment",
            "page_capture_ids": ["cap-home"],
            "evidence_artifact_ids": ["artifact-cap-home"],
            "exploration_action_ids": [],
        },
    ]

    result = build_page_exploration_summary(
        pages=captures,
        actions=[],
        page_type_assessments=assessments,
        assessment_scope_complete=True,
        required_page_types=["home", "payment"],
    )["coverage"]["product_business"]

    assert result["applicable_count"] == 1
    assert result["coverage_rate"] == 1.0
    assert result["not_applicable_page_types"] == ["payment"]


def test_no_assessment_at_all_does_not_fabricate_a_rate() -> None:
    """一条断言都没有说明这轮压根没跑起来，不得臆断成全部未覆盖。"""
    result = build_page_exploration_summary(
        pages=[],
        actions=[],
        page_type_assessments=[],
        assessment_scope_complete=False,
        required_page_types=["home", "content_list"],
    )["coverage"]["product_business"]

    assert result["coverage_rate"] is None
    assert result["known_uncovered_page_types"] == []
