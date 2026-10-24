"""H5 遮挡事实的通用、渲染态判定。

本模块不识别域名，也不授权浏览器动作。它只把 UI Snapshot 中的
computed-style、矩形与 hit-test 事实归一化，供页面检测和 Frontier 使用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

H5ObstructionKind = Literal[
    "none",
    "nonblocking_notification",
    "blocking_overlay",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class H5ObstructionAssessment:
    """由实时 UI Snapshot 推导的有限遮挡事实。"""

    kind: H5ObstructionKind
    blocking: bool
    notification_count: int = 0
    blocking_element_count: int = 0
    full_viewport_gate_count: int = 0
    # 阻断元素占视口面积的最大比例（0～1）；供展示层评估区分整页遮挡与局部遮挡。
    max_blocking_coverage: float = 0.0


def assess_h5_ui_obstruction(snapshot: Mapping[str, Any] | None) -> H5ObstructionAssessment:
    """区分短命非阻断提示与真正拦截页面的遮挡。

    判定只使用通用语义、面积和 hit-test；框架 class 仅作为提示类别，
    不作为单独的阻断或点击授权依据。
    """

    if not isinstance(snapshot, Mapping):
        return H5ObstructionAssessment(kind="unknown", blocking=False)
    raw_elements = snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return H5ObstructionAssessment(kind="unknown", blocking=False)

    viewport = snapshot.get("viewport")
    viewport_area = _rect_area(viewport) or 1.0
    notifications: list[Mapping[str, Any]] = []
    blockers: list[Mapping[str, Any]] = []
    full_viewport_gates: list[Mapping[str, Any]] = []
    max_blocking_coverage = 0.0
    census = snapshot.get("h5_modal_census_selectors")
    trusted_modal_selectors = (
        set(census) if isinstance(census, list)
        and snapshot.get("h5_modal_census_truncated") is False else None
    )

    for raw in raw_elements:
        if not isinstance(raw, Mapping) or not _rendered(raw):
            continue
        coverage = min(_rect_area(raw.get("rect")) / viewport_area, 1.0)
        if _notification_semantics(raw) and coverage <= 0.20:
            notifications.append(raw)
            continue
        if _blocking_semantics(
            raw, viewport_coverage=coverage, trusted_modal_selectors=trusted_modal_selectors,
        ):
            blockers.append(raw)
            if _full_viewport_gate_semantics(raw, viewport_coverage=coverage):
                full_viewport_gates.append(raw)
            max_blocking_coverage = max(max_blocking_coverage, coverage)

    if blockers:
        return H5ObstructionAssessment(
            kind="blocking_overlay",
            blocking=True,
            notification_count=len(notifications),
            blocking_element_count=len(blockers),
            full_viewport_gate_count=len(full_viewport_gates),
            max_blocking_coverage=max_blocking_coverage,
        )
    if notifications and all(_hit_test_falls_through(item) for item in notifications):
        return H5ObstructionAssessment(
            kind="nonblocking_notification",
            blocking=False,
            notification_count=len(notifications),
        )
    return H5ObstructionAssessment(kind="none", blocking=False)


def _notification_semantics(element: Mapping[str, Any]) -> bool:
    role = str(element.get("role") or "").strip().casefold()
    tokens = _class_tokens(element)
    return bool(
        role in {"status", "alert"}
        or any(
            marker in token
            for token in tokens
            for marker in ("toast", "snackbar", "notification")
        )
    )


def is_h5_nonblocking_inline_state(
    element: Mapping[str, Any], *, snapshot: Mapping[str, Any],
) -> bool:
    """完整现场父链、低层级和局部几何才能排除正文状态节点，缺事实时保守。"""
    census = snapshot.get("h5_modal_census_selectors")
    ancestors = element.get("h5_modal_ancestor_selectors")
    style = element.get("style")
    hit_test = element.get("hit_test")
    z_index = element.get("effective_z_index")
    viewport_area = _rect_area(snapshot.get("viewport"))
    coverage = _rect_area(element.get("rect")) / viewport_area if viewport_area else 0
    if (
        not isinstance(census, list) or snapshot.get("h5_modal_census_truncated") is not False
        or not isinstance(ancestors, list) or ancestors or element.get("selector") in census
        or not isinstance(style, Mapping)
        or style.get("position") not in {"static", "relative", "absolute"}
        or isinstance(z_index, bool) or not isinstance(z_index, int | float)
        or not 0 <= z_index < 100
        or not 0 < coverage < 0.5
        or not isinstance(hit_test, Mapping)
        or hit_test.get("visual_occlusion_detected") is not False
        or element.get("aria_modal") is True
        or str(element.get("role") or "").casefold() in {"dialog", "alertdialog"}
    ):
        return False
    return not _blocking_semantics(
        element, viewport_coverage=coverage, trusted_modal_selectors=set(census),
    )


def _blocking_semantics(
    element: Mapping[str, Any],
    *,
    viewport_coverage: float,
    trusted_modal_selectors: set[str] | None = None,
) -> bool:
    role = str(element.get("role") or "").strip().casefold()
    tokens = _class_tokens(element)
    hit_test = element.get("hit_test")
    center_hits = isinstance(hit_test, Mapping) and bool(
        hit_test.get("center_hits_target") is True
        or hit_test.get("pointer_interception") is True
    )
    explicit_modal = bool(element.get("aria_modal") is True or role == "dialog")
    overlay_family = any(
        marker in token
        for token in tokens
        for marker in ("overlay", "modal", "dialog", "popup")
    )
    ancestor_overlay_family = _ancestor_blocking_overlay_family(element)
    pointer_falls_through = _hit_test_falls_through(element)
    full_viewport_gate = _full_viewport_gate_semantics(
        element,
        viewport_coverage=viewport_coverage,
    )
    if trusted_modal_selectors is not None and not explicit_modal and not full_viewport_gate:
        modal_member = bool(
            element.get("selector") in trusted_modal_selectors
            or trusted_modal_selectors.intersection(
                element.get("h5_modal_ancestor_selectors") or [],
            )
        )
        style = element.get("style")
        style = style if isinstance(style, Mapping) else {}
        try:
            effective_z_index = float(element.get("effective_z_index") or 0)
        except (TypeError, ValueError):
            effective_z_index = 0.0
        floating_layer = bool(
            style.get("position") in {"fixed", "absolute"}
            and effective_z_index >= 100
        )
        # 新 H5 快照已有现场父链；正文卡片里的 mask/overlay 类名不能证明拦截层。
        if not modal_member and not floating_layer:
            overlay_family = False
            ancestor_overlay_family = False
    # 小型 Dialog 本身只有在命中测试仍落在自身时才算阻断；全屏遮罩即使
    # 中心命中子节点，也依然是有效遮挡。
    return bool(
        (
            viewport_coverage >= 0.50
            and (explicit_modal or (overlay_family and not pointer_falls_through))
        )
        or (viewport_coverage >= 0.02 and center_hits and (explicit_modal or overlay_family))
        or (center_hits and ancestor_overlay_family)
        or full_viewport_gate
    )


def _ancestor_blocking_overlay_family(element: Mapping[str, Any]) -> bool:
    """祖先只有通用 overlay 类名时，不足以证明当前元素属于弹窗。"""

    for token in _ancestor_class_tokens(element):
        if any(nonblocking in token for nonblocking in ("float", "suspend")):
            continue
        if any(marker in token for marker in ("modal", "dialog", "popup", "backdrop", "mask")):
            return True
        if "overlay" in token and token != "overlay":
            return True
    return False


def _full_viewport_gate_semantics(
    element: Mapping[str, Any],
    *,
    viewport_coverage: float,
) -> bool:
    """识别视觉上占满业务区的通用警告/权限门槛媒体层。"""

    if viewport_coverage < 0.75:
        return False
    tag = str(element.get("tag") or "").strip().casefold()
    if tag not in {"canvas", "img", "video"}:
        return False
    tokens = _class_tokens(element) | _ancestor_class_tokens(element)
    gate_semantics = any(
        marker in token
        for token in tokens
        for marker in (
            "entitlement",
            "gate",
            "lock",
            "paywall",
            "warning",
            "waring",
        )
    )
    if not gate_semantics:
        return False
    hit_test = element.get("hit_test")
    if not isinstance(hit_test, Mapping):
        return False
    intercepts_pointer = bool(
        hit_test.get("pointer_interception") is True
        and hit_test.get("topmost") is True
    )
    try:
        effective_z_index = float(element.get("effective_z_index") or 0)
    except (TypeError, ValueError):
        effective_z_index = 0.0
    return intercepts_pointer and effective_z_index >= 100


def _hit_test_falls_through(element: Mapping[str, Any]) -> bool:
    style = element.get("style")
    if (
        isinstance(style, Mapping)
        and str(style.get("pointer_events") or "").casefold() == "none"
    ):
        return True
    hit_test = element.get("hit_test")
    return bool(
        isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is False
    )


def _rendered(element: Mapping[str, Any]) -> bool:
    style = element.get("style")
    if isinstance(style, Mapping):
        if str(style.get("display") or "").casefold() == "none":
            return False
        if str(style.get("visibility") or "").casefold() == "hidden":
            return False
        try:
            if float(style.get("opacity", 1)) == 0:
                return False
        except (TypeError, ValueError):
            pass
    return _rect_area(element.get("rect")) > 0


def _rect_area(value: object) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    try:
        width = max(float(value.get("width") or 0), 0.0)
        height = max(float(value.get("height") or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0
    return width * height


def _class_tokens(element: Mapping[str, Any]) -> set[str]:
    raw = element.get("class_tokens")
    return {
        str(item).strip().casefold()
        for item in raw
        if str(item).strip()
    } if isinstance(raw, list) else set()


def _ancestor_class_tokens(element: Mapping[str, Any]) -> set[str]:
    raw = element.get("ancestor_class_tokens")
    return {
        str(item).strip().casefold()
        for item in raw
        if str(item).strip()
    } if isinstance(raw, list) else set()
