"""首个弹窗广告的截图归属校验，不以营销文字推断弹窗层级。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    CandidateAction,
    NormalizedBounds,
)


def belongs_to_topmost_ad_popup(
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """候选必须来自同帧，且整个点击框位于已确认的最上层广告内部。"""

    popup = analysis.surface_facts.get("topmost_ad_popup")
    if not isinstance(popup, Mapping) or popup.get("is_ad") is not True:
        return False
    confidence = popup.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.8 <= confidence <= 1.0
    ):
        return False
    raw_bounds = popup.get("normalized_bounds")
    if not isinstance(raw_bounds, Mapping):
        return False
    try:
        popup_bounds = NormalizedBounds.from_dict(raw_bounds)
    except (TypeError, ValueError):
        return False
    visual = next(
        (item for item in analysis.candidates if item.candidate_id == candidate.candidate_id),
        None,
    )
    if visual is None or visual.normalized_bounds is None or visual.confidence < 0.8:
        return False
    bounds = visual.normalized_bounds
    return (
        popup_bounds.left <= bounds.left < bounds.right <= popup_bounds.right
        and popup_bounds.top <= bounds.top < bounds.bottom <= popup_bounds.bottom
    )
