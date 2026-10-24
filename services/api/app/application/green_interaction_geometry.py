"""原生命中矩形分区的精确交并计算，不引入产品容差。"""

import math
from collections.abc import Mapping
from decimal import Decimal


def _rect(value):
    if (
        not isinstance(value, Mapping)
        or not all(
            type(value.get(key)) in (int, float) and math.isfinite(value[key])
            for key in ("x", "y", "width", "height")
        )
        or value["width"] <= 0
        or value["height"] <= 0
    ):
        return None
    x, y, width, height = (Decimal(str(value[key])) for key in ("x", "y", "width", "height"))
    return x, y, x + width, y + height


def _intersection(left, right):
    box = (
        max(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        min(left[3], right[3]),
    )
    return box if box[0] < box[2] and box[1] < box[3] else None


def _box_area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _area(boxes):
    points, total = sorted({edge for box in boxes for edge in (box[0], box[2])}), Decimal(0)
    for left, right in zip(points, points[1:], strict=False):
        intervals = sorted((box[1], box[3]) for box in boxes if box[0] < right and box[2] > left)
        length, end = Decimal(0), None
        for lower, upper in intervals:
            length += (
                upper - max(lower, end if end is not None else lower)
                if end is None or upper > end
                else 0
            )
            end = max(end, upper) if end is not None else upper
        total += (right - left) * length
    return total
