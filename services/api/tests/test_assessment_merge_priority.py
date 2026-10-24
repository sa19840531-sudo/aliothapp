"""同页型多条断言的收敛：取最优，不取排在最后的那条。"""

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


def _assessment(
    capture_id: str, page_type: str, *, with_evidence: bool
) -> dict[str, Any]:
    return {
        "page_type": page_type,
        "status": "covered",
        "reason_code": "h5_page_detection_covered",
        "page_capture_ids": [capture_id],
        "evidence_artifact_ids": (
            [f"artifact-{capture_id}"] if with_evidence else ["artifact-missing"]
        ),
        "exploration_action_ids": [],
    }


def _summary(assessments: list[dict[str, Any]], captures: list[dict[str, Any]]):
    return build_page_exploration_summary(
        pages=captures,
        actions=[],
        page_type_assessments=assessments,
        assessment_scope_complete=True,
        required_page_types=["content_list"],
    )["coverage"]["product_business"]


def test_one_evidenced_assessment_makes_the_page_type_covered() -> None:
    """取证齐备的那条排在前面时也必须算数。

    实测十八汇（SF-129）content_list 六条断言里三条取证齐备，只因排在
    最后的一条配不上就整个页型判成未决；同任务 video_list 同样三通过
    三不通过，只因最后一条恰好通过就算覆盖 —— 覆盖数字取决于
    page_capture_id 的 UUID 字典序。
    """
    captures = [_capture("aaa", "content_list"), _capture("zzz", "content_list")]
    assessments = [
        _assessment("aaa", "content_list", with_evidence=True),
        _assessment("zzz", "content_list", with_evidence=False),
    ]

    result = _summary(assessments, captures)

    assert result["covered_page_types"] == ["content_list"]
    assert result["unresolved_page_types"] == []


def test_order_does_not_change_the_verdict() -> None:
    """把取证齐备的那条挪到最后，结论不能变。"""
    captures = [_capture("aaa", "content_list"), _capture("zzz", "content_list")]
    reversed_order = [
        _assessment("zzz", "content_list", with_evidence=False),
        _assessment("aaa", "content_list", with_evidence=True),
    ]

    result = _summary(reversed_order, captures)

    assert result["covered_page_types"] == ["content_list"]


def test_all_assessments_without_evidence_stay_unresolved() -> None:
    """一条取证都没有时仍要判未决，fail-closed 判据不得被绕过。"""
    captures = [_capture("aaa", "content_list")]
    assessments = [
        _assessment("aaa", "content_list", with_evidence=False),
        _assessment("aaa", "content_list", with_evidence=False),
    ]

    result = _summary(assessments, captures)

    assert result["covered_page_types"] == []
    assert result["unresolved_page_types"] == ["content_list"]
