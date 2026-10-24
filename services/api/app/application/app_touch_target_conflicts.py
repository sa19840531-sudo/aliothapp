"""App 当前帧触控候选与可信普通控件的几何冲突判定。"""

from __future__ import annotations

from collections.abc import Iterable

from services.api.app.domain.exploration import AppVisualCandidate
from services.api.app.domain.interaction import InteractionOperation

_COORDINATE_TOUCH_ACTIONS = frozenset(
    {InteractionOperation.CLICK, InteractionOperation.TAP}
)
_DISMISS_CONTROL_ROLE = "dismiss"
_OBSTRUCTION_CONTROL_ROLES = frozenset({"dismiss", "skip"})


def _same_capture(
    candidate: AppVisualCandidate,
    trusted_action_control: AppVisualCandidate,
) -> bool:
    """只比较同一张截图的候选，拒绝跨 Capture 借用几何事实。"""

    return (
        candidate.source_capture_id == trusted_action_control.source_capture_id
        and candidate.source_image_signature == trusted_action_control.source_image_signature
        and candidate.source_width == trusted_action_control.source_width
        and candidate.source_height == trusted_action_control.source_height
        and candidate.source_orientation == trusted_action_control.source_orientation
    )


def _center_is_inside(
    candidate: AppVisualCandidate,
    trusted_action_control: AppVisualCandidate,
) -> bool:
    candidate_bounds = candidate.normalized_bounds
    trusted_bounds = trusted_action_control.normalized_bounds
    if candidate_bounds is None or trusted_bounds is None:
        return False
    center_x, center_y = candidate_bounds.center
    return (
        float(trusted_bounds.left) <= center_x <= float(trusted_bounds.right)
        and float(trusted_bounds.top) <= center_y <= float(trusted_bounds.bottom)
    )


def dismiss_candidate_conflicts_with_trusted_action(
    *,
    candidate: AppVisualCandidate,
    trusted_action_controls: Iterable[AppVisualCandidate],
) -> bool:
    """判断 dismiss 落点是否位于同帧可信普通动作控件内。"""

    if (
        candidate.control_role != _DISMISS_CONTROL_ROLE
        or candidate.action_type not in _COORDINATE_TOUCH_ACTIONS
        or candidate.normalized_bounds is None
    ):
        return False
    return any(
        trusted_action_control.control_role not in _OBSTRUCTION_CONTROL_ROLES
        and trusted_action_control.action_type in _COORDINATE_TOUCH_ACTIONS
        and trusted_action_control.normalized_bounds is not None
        and _same_capture(candidate, trusted_action_control)
        and _center_is_inside(candidate, trusted_action_control)
        for trusted_action_control in trusted_action_controls
    )


def filter_unproven_dismiss_candidates(
    *,
    candidates: tuple[AppVisualCandidate, ...],
    trusted_action_controls: tuple[AppVisualCandidate, ...],
    current_dismiss_proven: bool,
) -> tuple[AppVisualCandidate, ...]:
    """剔除无当前关闭证据且落入可信普通控件的 dismiss 候选。"""

    if current_dismiss_proven or not trusted_action_controls:
        return candidates
    return tuple(
        candidate
        for candidate in candidates
        if not dismiss_candidate_conflicts_with_trusted_action(
            candidate=candidate,
            trusted_action_controls=trusted_action_controls,
        )
    )


__all__ = [
    "dismiss_candidate_conflicts_with_trusted_action",
    "filter_unproven_dismiss_candidates",
]
