"""Web CSS 视口矩形相交并集，缺少可证明几何时返回未知。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def _number(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(value) else None


def viewport_rectangles_coverage(viewport: object, rectangles: Sequence[object]) -> float | None:
    """输入 getBoundingClientRect 的 CSS 坐标，不重复应用页面滚动或 DPR。"""
    if not isinstance(viewport, Mapping):
        return None
    width, height = _number(viewport.get("width")), _number(viewport.get("height"))
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    left = top = 0.0
    visual_keys = (
        "visual_viewport_width",
        "visual_viewport_height",
        "visual_viewport_offset_left",
        "visual_viewport_offset_top",
    )
    if any(viewport.get(key) is not None for key in visual_keys):
        values = [_number(viewport.get(key)) for key in visual_keys]
        if any(value is None for value in values):
            return None
        width, height, left, top = values
        if width <= 0 or height <= 0:
            return None
    clipped: list[tuple[float, float, float, float]] = []
    for rect in rectangles:
        if not isinstance(rect, Mapping):
            return None
        values = [_number(rect.get(key)) for key in ("x", "y", "width", "height")]
        if any(value is None for value in values):
            return None
        x, y, rect_width, rect_height = values
        if rect_width < 0 or rect_height < 0:
            return None
        x1, y1 = max(left, x), max(top, y)
        x2, y2 = min(left + width, x + rect_width), min(top + height, y + rect_height)
        if x2 > x1 and y2 > y1:
            clipped.append((x1, y1, x2, y2))
    area = 0.0
    xs = sorted({x for x1, _, x2, _ in clipped for x in (x1, x2)})
    for x1, x2 in zip(xs, xs[1:], strict=False):
        segments = sorted((y1, y2) for a, y1, b, y2 in clipped if a < x2 and b > x1)
        union = 0.0
        previous_end = None
        for y1, y2 in segments:
            union += max(0.0, y2 - max(y1, previous_end if previous_end is not None else y1))
            previous_end = max(previous_end if previous_end is not None else y2, y2)
        area += (x2 - x1) * union
    return min(1.0, area / (width * height))
