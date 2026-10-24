"""Web 普通浮层与可信主体的几何重叠判定。"""

from __future__ import annotations

import math
from collections.abc import Mapping

WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP = 0.30


def web_has_only_benign_ordinary_overlays(ui_snapshot: Mapping[str, object]) -> bool:
    """仅当普通浮层联合遮挡每个可信可见主体均小于 30% 时返回真。"""

    return web_obstruction_geometry_facts(ui_snapshot)["status"] == "below_threshold"


def web_obstruction_geometry_facts(ui_snapshot: Mapping[str, object]) -> dict[str, object]:
    """只读受控事实；不签发候选、不把缺几何解释为无遮挡。"""

    facts: dict[str, object] = {
        "status": "unknown", "complete": False, "layer_count": 0,
        "subject_count": 0, "subject_overlap_ratio": None,
        "review_overlap_threshold": WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP,
    }

    if ui_snapshot.get("human_verification_present") is True:
        return facts
    if (ui_snapshot.get("web_detail_candidate_priority_enabled") is True
            and not isinstance(ui_snapshot.get("web_overlay_geometry"), Mapping)):
        return facts
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return facts
    structural = [
        item
        for item in elements
        if isinstance(item, Mapping)
        and item.get("visible") is True
        and item.get("web_obstruction_layer_structural") is True
    ]
    rectangles = []
    for item in structural:
        if not web_is_ordinary_overlay(item):
            return facts
        selector = item.get("web_obstruction_layer")
        layer = next((raw for raw in elements if isinstance(raw, Mapping)
                      and selector and raw.get("selector") == selector), None)
        rect = _finite_positive_rect(
            layer.get("rect") if layer else (
                item.get("web_obstruction_layer_rect") if selector else item.get("rect")
            )
        )
        if rect is None:
            return facts
        rectangles.append(rect)
    census = ui_snapshot.get("web_overlay_geometry")
    subject_elements = elements
    if census is not None:
        if (ui_snapshot.get("web_detail_candidate_priority_enabled") is not True
                or not isinstance(census, Mapping)
                or census.get("schema_version") != "web-overlay-geometry.v1"
                or census.get("complete") is not True):
            return facts
        layers = census.get("layers")
        subjects = census.get("subjects")
        if (not isinstance(layers, list) or len(layers) > 32
                or not isinstance(subjects, list) or not subjects or len(subjects) > 32
                or any(_finite_positive_rect(rect) is None for rect in subjects)):
            return facts
        subject_elements = [{"tag": "main", "visible": True, "rect": rect} for rect in subjects]
        for layer in layers:
            if not isinstance(layer, Mapping) or layer.get("ordinary") is not True:
                return facts
            rect = _finite_positive_rect(layer.get("rect"))
            if rect is None:
                return facts
            rectangles.append(rect)
    rectangles = list(dict.fromkeys(rectangles))
    facts["layer_count"] = len(rectangles)
    subjects = _visible_subject_rects(ui_snapshot, subject_elements)
    facts["subject_count"] = len(subjects)
    if not rectangles or not subjects:
        return facts
    overlap = max(
        _rectangle_union_area([
            intersection for rect in rectangles
            if (intersection := _rect_intersection(rect, subject)) is not None
        ]) / (subject[2] * subject[3])
        for subject in subjects
    )
    facts.update(
        complete=True, subject_overlap_ratio=min(1.0, overlap),
        status="below_threshold" if overlap < WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP
        else "review_required",
    )
    return facts


def web_public_obstruction_geometry(raw: Mapping[str, object]) -> dict[str, object]:
    """模型只接收有界枚举、计数与比例，不复制任意文本或定位器。"""

    counts = {
        key: value if isinstance(value := raw.get(key), int)
        and not isinstance(value, bool) and 0 <= value <= 512 else 0
        for key in ("layer_count", "subject_count")
    }
    ratio = raw.get("subject_overlap_ratio")
    complete = (raw.get("complete") is True and _finite_number(ratio)
                and 0 <= ratio <= 1 and all(counts.values())
                and raw.get("status") in {"below_threshold", "review_required"})
    return {
        "status": ("below_threshold" if ratio < WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP
                   else "review_required") if complete else "unknown",
        "complete": complete, **counts,
        "subject_overlap_ratio": ratio if complete else None,
        "review_overlap_threshold": WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP,
    }


def _rectangle_union_area(rectangles: list[tuple[float, float, float, float]]) -> float:
    """沿横轴分片并合并纵向区间，避免重叠层重复计数。"""

    boundaries = sorted({edge for x, _, width, _ in rectangles for edge in (x, x + width)})
    area = 0.0
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        intervals = sorted((y, y + height) for x, y, width, height in rectangles
                           if x < right and x + width > left)
        top = bottom = 0.0
        covered = 0.0
        for start, end in intervals:
            if start > bottom:
                covered += bottom - top
                top, bottom = start, end
            else:
                bottom = max(bottom, end)
        area += (right - left) * (covered + bottom - top)
    return area


def _visible_subject_rects(ui_snapshot, elements):
    viewport_value = ui_snapshot.get("viewport")
    if not isinstance(viewport_value, Mapping):
        return []
    viewport = _finite_positive_rect({"x": 0, "y": 0,
                                     "width": viewport_value.get("width"),
                                     "height": viewport_value.get("height")})
    if viewport is None:
        return []
    return list(dict.fromkeys(
        clipped for item in elements
        if isinstance(item, Mapping) and item.get("visible") is True
        and item.get("web_obstruction_layer_structural") is not True
        and item.get("within_web_navigation") is not True
        and not _is_advertisement(item)
        and (str(item.get("tag") or "").casefold() in {"main", "article"}
             or str(item.get("role") or "").casefold() == "main")
        and (rect := _finite_positive_rect(item.get("rect"))) is not None
        and (clipped := _rect_intersection(rect, viewport)) is not None
    ))


def web_is_ordinary_overlay(element: Mapping[str, object]) -> bool:
    """年龄门、人机验证和语义 modal 不属于普通浮层。"""

    return bool(
        element.get("aria_modal") is not True
        and element.get("web_human_verification_present") is not True
        and str(element.get("web_obstruction_layer_kind") or "").casefold()
        in {"geometric_overlay", "overlay", "positioned_layer"}
    )


def web_ordinary_overlay_subject_overlap(
    ui_snapshot: Mapping[str, object],
    element: Mapping[str, object],
) -> float | None:
    """返回“浮层与主体交集面积 / 主体面积”，缺少可信几何时返回空。"""

    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list):
        return None
    layer_selector = str(element.get("web_obstruction_layer") or "").strip()
    matched_layer = next(
        (
            item
            for item in elements
            if isinstance(item, Mapping)
            and layer_selector
            and str(item.get("selector") or "").strip() == layer_selector
        ),
        None,
    )
    layer = matched_layer or element
    layer_rect = _finite_positive_rect(
        layer.get("rect")
        if matched_layer is not None or not layer_selector
        else element.get("web_obstruction_layer_rect")
    )
    if layer_rect is None:
        return None
    viewport = _finite_positive_rect(
        {
            "x": 0,
            "y": 0,
            "width": (ui_snapshot.get("viewport") or {}).get("width"),
            "height": (ui_snapshot.get("viewport") or {}).get("height"),
        }
        if isinstance(ui_snapshot.get("viewport"), Mapping)
        else None
    )
    if viewport is None:
        return None
    subject_roots = [
        visible_rect
        for item in elements
        if isinstance(item, Mapping)
        and item.get("visible") is True
        and item.get("web_obstruction_layer_structural") is not True
        and not _is_advertisement(item)
        and (
            str(item.get("tag") or "").casefold() in {"main", "article"}
            or str(item.get("role") or "").casefold() == "main"
        )
        and (rect := _finite_positive_rect(item.get("rect"))) is not None
        and (visible_rect := _rect_intersection(rect, viewport)) is not None
    ]
    subject_rect = max(subject_roots, key=lambda rect: rect[2] * rect[3], default=None)
    if subject_rect is None:
        # viewport 不是主体区域。主体根缺失时必须交给模型保守判定，
        # 否则小主体被大浮层覆盖时会因视口分母过大而被错误放行。
        return None
    x, y, width, height = subject_rect
    layer_x, layer_y, layer_width, layer_height = layer_rect
    overlap_width = max(
        0.0,
        min(x + width, layer_x + layer_width) - max(x, layer_x),
    )
    overlap_height = max(
        0.0,
        min(y + height, layer_y + layer_height) - max(y, layer_y),
    )
    return min(1.0, (overlap_width * overlap_height) / (width * height))


def _rect_intersection(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


def _is_advertisement(element: Mapping[str, object]) -> bool:
    tokens = {
        str(value).strip().casefold()
        for key in ("class_tokens", "ancestor_class_tokens")
        for value in element.get(key) or []
        if str(value).strip()
    }
    return element.get("is_advertisement") is True or bool(
        tokens & {"advertisement", "sponsor", "sponsored", "promotion", "promo-banner"}
    )


def _finite_positive_rect(raw: object) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    values = tuple(raw.get(key) for key in ("x", "y", "width", "height"))
    if not all(_finite_number(value) for value in values):
        return None
    x, y, width, height = (float(value) for value in values)
    return (x, y, width, height) if width > 0 and height > 0 else None


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


__all__ = [
    "WEB_ORDINARY_OVERLAY_REVIEW_OVERLAP",
    "web_has_only_benign_ordinary_overlays",
    "web_is_ordinary_overlay",
    "web_ordinary_overlay_subject_overlap",
    "web_obstruction_geometry_facts",
    "web_public_obstruction_geometry",
]
