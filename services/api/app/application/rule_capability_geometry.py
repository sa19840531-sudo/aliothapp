"""layout_geometry_assertion_handler 的确定性几何断言实现。

断言族按"规则配置了哪个阈值键"决定是否执行，判定阈值一律来自规则配置
（capability_config.thresholds 逐键覆盖 packet 内 rule.criteria.thresholds），
代码不写死判定阈值；每条规则可配不同值：

- window_padding：           min_window_padding_px
- fullscreen_image_margin：  safe_margin_min_ratio（候选筛选比例可用
                             fullscreen_coverage_min_ratio 覆盖默认值）
- bottom_blank：             max_bottom_blank_viewport_ratio
- alignment：                max_column_align_deviation_px（行对齐与行内等高
                             可用 max_row_align_deviation_px、
                             max_row_height_deviation_px 单独配置，缺省复用列阈值）
- aspect_ratio：             随 alignment 附带输出比例分组事实；配置
                             max_aspect_ratio_relative_deviation 时才标记离群违规
- overlap：                  max_overlap_pairs
- spacing：                  随 overlap 附带输出最近邻间距事实；配置
                             min_control_spacing_px 时才标记违规

工具只输出结构化几何事实与越阈值标记，规则级 pass/fail 结论由语义 Judge 下。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median
from typing import Any

from services.api.app.application.rule_capability_facts import (
    elements as _elements,
)
from services.api.app.application.rule_capability_facts import (
    is_media_element as _is_media_element,
)
from services.api.app.application.rule_capability_facts import (
    latest_ui_snapshot as _latest_ui_snapshot,
)
from services.api.app.application.rule_capability_facts import (
    looks_like_ad_element as _looks_like_ad_element,
)
from services.api.app.application.rule_capability_facts import (
    mapping as _mapping,
)
from services.api.app.application.rule_capability_facts import (
    number as _number,
)
from services.api.app.application.rule_capability_facts import (
    string_list as _string_list,
)

# 以下常数只承担候选筛选与舍入容差，不承担规则判定职责；判定阈值全部读规则配置。
_FULLSCREEN_COVERAGE_MIN_RATIO = 0.85
_VIEWPORT_SPAN_RATIO = 0.98
_CONTAINMENT_EPSILON_PX = 1.0
_AT_BOTTOM_TOLERANCE_PX = 2.0
_MIN_GROUP_MEMBERS = 3
_MAX_LISTED_ITEMS = 20
_INTERACTIVE_TAGS = frozenset({"button", "a", "input", "select", "textarea"})
_INTERACTIVE_ROLES = frozenset(
    {"button", "link", "tab", "checkbox", "radio", "switch", "menuitem"}
)


@dataclass(frozen=True, slots=True)
class _Box:
    """带几何矩形的可见元素视图。"""

    element: dict[str, Any]
    x: float
    y: float
    width: float
    height: float

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.height


def run_layout_geometry_assertions(
    *,
    packet_payload: dict[str, Any],
    capability_config: dict[str, Any],
) -> dict[str, Any]:
    """执行几何断言，返回 status、facts、reason 供 hybrid 层包装成 Tool 结果。"""
    packet_facts = _mapping(packet_payload.get("facts"))
    page = _mapping(packet_payload.get("page"))
    if (
        packet_facts.get("structure_screen_mismatch") is True
        and page.get("page_type") == "payment"
    ):
        return {
            "status": "insufficient",
            "facts": {"gap_codes": ["recharge_layout_screen_mismatch"]},
            "reason": "充值页截图与结构信息不属于同一界面，无法检查控件间距。",
        }
    thresholds = _resolve_thresholds(packet_payload, capability_config)
    snapshot = _latest_ui_snapshot(packet_payload)
    viewport = _mapping(snapshot.get("viewport"))
    viewport_width = _positive(viewport.get("width"))
    viewport_height = _positive(viewport.get("height"))
    document = _mapping(snapshot.get("document"))
    scroll_width = _positive(document.get("scroll_width"))
    scroll_height = _positive(document.get("scroll_height"))
    boxes = _visible_boxes(_elements(packet_payload))

    thresholds_used: dict[str, float] = {}
    assertions: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []

    min_window_padding = _threshold(thresholds, "min_window_padding_px", thresholds_used)
    safe_margin_ratio = _threshold(thresholds, "safe_margin_min_ratio", thresholds_used)
    max_bottom_blank_ratio = _threshold(
        thresholds, "max_bottom_blank_viewport_ratio", thresholds_used
    )
    column_tolerance = _threshold(thresholds, "max_column_align_deviation_px", thresholds_used)
    row_tolerance = _threshold(thresholds, "max_row_align_deviation_px", thresholds_used)
    height_tolerance = _threshold(thresholds, "max_row_height_deviation_px", thresholds_used)
    ratio_deviation = _threshold(
        thresholds, "max_aspect_ratio_relative_deviation", thresholds_used
    )
    max_overlap_pairs = _threshold(thresholds, "max_overlap_pairs", thresholds_used)
    min_control_spacing = _threshold(thresholds, "min_control_spacing_px", thresholds_used)

    facts: dict[str, Any] = {
        "viewport": (
            {"width": viewport_width, "height": viewport_height}
            if viewport_width and viewport_height
            else {}
        ),
        "visible_element_count": len(boxes),
        "thresholds_used": thresholds_used,
        "assertions": assertions,
        "skipped_assertions": skipped,
    }
    if not thresholds_used:
        return {
            "status": "insufficient",
            "facts": facts,
            "reason": "规则未配置几何断言阈值，无法执行确定性几何检查",
        }
    if not boxes:
        return {
            "status": "insufficient",
            "facts": facts,
            "reason": "可信 UI 快照中没有可见元素的几何事实",
        }

    if min_window_padding is None:
        skipped.append(_skip("window_padding", "min_window_padding_px"))
    elif viewport_width is None or viewport_height is None:
        assertions["window_padding"] = _not_computable("缺少浏览器 viewport 几何事实")
    else:
        assertions["window_padding"] = _assert_window_padding(
            boxes,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            scroll_width=scroll_width,
            scroll_height=scroll_height,
            min_padding=min_window_padding,
        )

    if safe_margin_ratio is None:
        skipped.append(_skip("fullscreen_image_margin", "safe_margin_min_ratio"))
    elif viewport_width is None or viewport_height is None:
        assertions["fullscreen_image_margin"] = _not_computable("缺少浏览器 viewport 几何事实")
    else:
        coverage_ratio = (
            _threshold(thresholds, "fullscreen_coverage_min_ratio", thresholds_used)
            or _FULLSCREEN_COVERAGE_MIN_RATIO
        )
        assertions["fullscreen_image_margin"] = _assert_fullscreen_image_margin(
            boxes,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            safe_margin_ratio=safe_margin_ratio,
            coverage_ratio=coverage_ratio,
        )

    if max_bottom_blank_ratio is None:
        skipped.append(_skip("bottom_blank", "max_bottom_blank_viewport_ratio"))
    elif viewport_height is None:
        assertions["bottom_blank"] = _not_computable("缺少浏览器 viewport 几何事实")
    else:
        assertions["bottom_blank"] = _assert_bottom_blank(
            boxes,
            viewport=viewport,
            viewport_height=viewport_height,
            scroll_height=scroll_height,
            max_blank_ratio=max_bottom_blank_ratio,
        )

    if column_tolerance is None:
        skipped.append(_skip("alignment", "max_column_align_deviation_px"))
    else:
        assertions["alignment"] = _assert_alignment(
            boxes,
            column_tolerance=column_tolerance,
            row_tolerance=row_tolerance if row_tolerance is not None else column_tolerance,
            height_tolerance=(
                height_tolerance if height_tolerance is not None else column_tolerance
            ),
        )

    if column_tolerance is None and ratio_deviation is None:
        skipped.append(_skip("aspect_ratio", "max_aspect_ratio_relative_deviation"))
    else:
        assertions["aspect_ratio"] = _assert_aspect_ratio(boxes, ratio_deviation)

    if max_overlap_pairs is None:
        skipped.append(_skip("overlap", "max_overlap_pairs"))
    else:
        assertions["overlap"] = _assert_overlap(
            boxes,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            max_pairs=max_overlap_pairs,
        )

    if max_overlap_pairs is None and min_control_spacing is None:
        skipped.append(_skip("spacing", "min_control_spacing_px"))
    else:
        assertions["spacing"] = _assert_spacing(boxes, min_gap=min_control_spacing)

    uncomputable = [
        f"{name}：{value.get('reason')}"
        for name, value in assertions.items()
        if value.get("computable") is False
    ]
    if assertions and len(uncomputable) == len(assertions):
        return {"status": "insufficient", "facts": facts, "reason": "；".join(uncomputable)}
    return {"status": "ok", "facts": facts}


def _resolve_thresholds(
    packet_payload: dict[str, Any],
    capability_config: dict[str, Any],
) -> dict[str, Any]:
    """阈值来源：capability_config.thresholds 逐键覆盖 packet 中 rule.criteria.thresholds。"""
    criteria = _mapping(_mapping(packet_payload.get("rule")).get("criteria"))
    resolved = dict(_mapping(criteria.get("thresholds")))
    resolved.update(_mapping(capability_config.get("thresholds")))
    return resolved


def _threshold(
    thresholds: dict[str, Any],
    key: str,
    thresholds_used: dict[str, float],
) -> float | None:
    value = _number(thresholds.get(key))
    if value is None or value < 0:
        return None
    thresholds_used[key] = value
    return value


def _skip(assertion: str, threshold_key: str) -> dict[str, str]:
    return {"assertion": assertion, "reason": f"未配置阈值 {threshold_key}"}


def _not_computable(reason: str) -> dict[str, Any]:
    return {"computable": False, "reason": reason}


def _visible_boxes(elements: list[dict[str, Any]]) -> list[_Box]:
    """筛掉不可见与无几何信息的元素，转成矩形视图。"""
    boxes: list[_Box] = []
    for item in elements:
        style = _mapping(item.get("style"))
        if str(style.get("display") or "").casefold() == "none":
            continue
        if str(style.get("visibility") or "").casefold() in {"hidden", "collapse"}:
            continue
        opacity = _number(style.get("opacity"))
        if opacity is not None and opacity <= 0:
            continue
        rect = _mapping(item.get("rect"))
        x = _number(rect.get("x"))
        y = _number(rect.get("y"))
        width = _number(rect.get("width"))
        height = _number(rect.get("height"))
        if x is None or y is None or not width or not height or width <= 0 or height <= 0:
            continue
        boxes.append(_Box(element=item, x=x, y=y, width=width, height=height))
    return boxes


def _assert_window_padding(
    boxes: list[_Box],
    *,
    viewport_width: float,
    viewport_height: float,
    scroll_width: float | None,
    scroll_height: float | None,
    min_padding: float,
) -> dict[str, Any]:
    """内容元素到视口四边的最小距离；贯通视口的背景/容器元素单独归类不参与。"""
    horizontal_scrollable = (
        scroll_width is None or scroll_width > viewport_width + _AT_BOTTOM_TOLERANCE_PX
    )
    vertical_scrollable = (
        scroll_height is None or scroll_height > viewport_height + _AT_BOTTOM_TOLERANCE_PX
    )
    edges_checked = ["left", "top"]
    if not horizontal_scrollable:
        edges_checked.append("right")
    if not vertical_scrollable:
        edges_checked.append("bottom")

    spanning_count = 0
    overflow: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    measured_count = 0
    for box in boxes:
        if (
            box.width >= viewport_width * _VIEWPORT_SPAN_RATIO
            or box.height >= viewport_height * _VIEWPORT_SPAN_RATIO
        ):
            spanning_count += 1
            continue
        measured_count += 1
        edges = {
            "left": box.x,
            "top": box.y,
            "right": viewport_width - box.x2,
            "bottom": viewport_height - box.y2,
        }
        checked = {edge: edges[edge] for edge in edges_checked}
        rounded = {edge: round(value, 1) for edge, value in checked.items()}
        min_edge = min(checked.values())
        if min_edge < 0:
            overflow.append({"element": _slim_ref(box), "edges_px": rounded})
        elif min_edge < min_padding:
            violations.append(
                {
                    "element": _slim_ref(box),
                    "edges_px": rounded,
                    "min_edge_px": round(min_edge, 1),
                }
            )
    return {
        "threshold_px": min_padding,
        "edges_checked": edges_checked,
        "measured_element_count": measured_count,
        "spanning_excluded_count": spanning_count,
        "overflow_count": len(overflow),
        "overflow_elements": overflow[:_MAX_LISTED_ITEMS],
        "violation_count": len(violations),
        "violations": violations[:_MAX_LISTED_ITEMS],
    }


def _assert_fullscreen_image_margin(
    boxes: list[_Box],
    *,
    viewport_width: float,
    viewport_height: float,
    safe_margin_ratio: float,
    coverage_ratio: float,
) -> dict[str, Any]:
    """非广告整屏图片被视口裁剪的量；单边裁剪超出预留安全边距即标记。"""
    viewport_area = viewport_width * viewport_height
    images: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for box in boxes:
        if not _is_media_element(box.element) or _looks_like_ad_element(box.element):
            continue
        intersection_width = min(box.x2, viewport_width) - max(box.x, 0.0)
        intersection_height = min(box.y2, viewport_height) - max(box.y, 0.0)
        if intersection_width <= 0 or intersection_height <= 0:
            continue
        if intersection_width * intersection_height < viewport_area * coverage_ratio:
            continue
        crop_px = {
            "left": max(0.0, -box.x),
            "top": max(0.0, -box.y),
            "right": max(0.0, box.x2 - viewport_width),
            "bottom": max(0.0, box.y2 - viewport_height),
        }
        crop_ratio = {
            "left": crop_px["left"] / viewport_width,
            "right": crop_px["right"] / viewport_width,
            "top": crop_px["top"] / viewport_height,
            "bottom": crop_px["bottom"] / viewport_height,
        }
        exceeded_edges = [
            edge for edge, ratio in crop_ratio.items() if ratio > safe_margin_ratio
        ]
        image_meta = _mapping(box.element.get("image"))
        entry = {
            "element": _slim_ref(box),
            "crop_px": {edge: round(value, 1) for edge, value in crop_px.items()},
            "crop_ratio_vs_viewport": {
                edge: round(value, 4) for edge, value in crop_ratio.items()
            },
            "object_fit": _mapping(box.element.get("style")).get("object_fit"),
            "natural_size": (
                {
                    "width": image_meta.get("natural_width"),
                    "height": image_meta.get("natural_height"),
                }
                if image_meta
                else None
            ),
            "crop_exceeds_safe_margin_edges": exceeded_edges,
        }
        images.append(entry)
        if exceeded_edges:
            violations.append(entry)
    return {
        "threshold_ratio": safe_margin_ratio,
        "coverage_ratio": coverage_ratio,
        "fullscreen_image_count": len(images),
        "images": images[:_MAX_LISTED_ITEMS],
        "violation_count": len(violations),
        "violations": violations[:_MAX_LISTED_ITEMS],
    }


def _assert_bottom_blank(
    boxes: list[_Box],
    *,
    viewport: dict[str, Any],
    viewport_height: float,
    scroll_height: float | None,
    max_blank_ratio: float,
) -> dict[str, Any]:
    """滚动到底后，最后一个文档流元素底边到视口底部的空白占比。"""
    scroll_y = _number(viewport.get("scroll_y"))
    if scroll_height is None or scroll_y is None:
        return {
            **_not_computable("缺少文档高度或滚动位置事实"),
            "threshold_ratio": max_blank_ratio,
        }
    if scroll_height <= viewport_height + _AT_BOTTOM_TOLERANCE_PX:
        return {
            "threshold_ratio": max_blank_ratio,
            "page_exceeds_one_viewport": False,
        }
    at_bottom = scroll_y + viewport_height >= scroll_height - _AT_BOTTOM_TOLERANCE_PX
    if not at_bottom:
        return {
            **_not_computable("采集时未滚动到页面底部，无法测量底部空白"),
            "threshold_ratio": max_blank_ratio,
            "page_exceeds_one_viewport": True,
        }
    flow_boxes: list[_Box] = []
    pinned_count = 0
    for box in boxes:
        position = str(_mapping(box.element.get("style")).get("position") or "").casefold()
        if position in {"fixed", "sticky"}:
            pinned_count += 1
        else:
            flow_boxes.append(box)
    if not flow_boxes:
        return {
            **_not_computable("底部帧内没有可测量的文档流元素"),
            "threshold_ratio": max_blank_ratio,
            "page_exceeds_one_viewport": True,
        }
    bottom_most = max(flow_boxes, key=lambda box: min(box.y2, viewport_height))
    max_content_bottom = min(bottom_most.y2, viewport_height)
    blank_px = max(0.0, viewport_height - max_content_bottom)
    blank_ratio = blank_px / viewport_height
    return {
        "threshold_ratio": max_blank_ratio,
        "page_exceeds_one_viewport": True,
        "at_bottom": True,
        "blank_px": round(blank_px, 1),
        "blank_ratio": round(blank_ratio, 4),
        "bottom_most_element": _slim_ref(bottom_most),
        "pinned_excluded_count": pinned_count,
        "exceeds_threshold": blank_ratio > max_blank_ratio,
    }


def _assert_alignment(
    boxes: list[_Box],
    *,
    column_tolerance: float,
    row_tolerance: float,
    height_tolerance: float,
) -> dict[str, Any]:
    """同构元素组内的列左缘对齐、行上缘对齐与行内等高偏差。"""
    groups = _layout_groups(boxes)
    summaries: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for key, members in groups:
        median_width = median(box.width for box in members)
        median_height = median(box.height for box in members)
        columns = _cluster(
            members,
            axis=lambda box: box.x,
            tolerance=max(median_width / 2, column_tolerance),
        )
        rows = _cluster(
            members,
            axis=lambda box: box.y,
            tolerance=max(median_height / 2, row_tolerance),
        )
        max_column_deviation = 0.0
        for column in columns:
            if len(column) < 2:
                continue
            xs = [box.x for box in column]
            deviation = max(xs) - min(xs)
            max_column_deviation = max(max_column_deviation, deviation)
            if deviation > column_tolerance:
                violations.append(
                    _alignment_violation(
                        key, "column_left_deviation", deviation, column_tolerance, column
                    )
                )
        max_row_deviation = 0.0
        max_height_deviation = 0.0
        for row in rows:
            if len(row) < 2:
                continue
            ys = [box.y for box in row]
            deviation = max(ys) - min(ys)
            max_row_deviation = max(max_row_deviation, deviation)
            if deviation > row_tolerance:
                violations.append(
                    _alignment_violation(key, "row_top_deviation", deviation, row_tolerance, row)
                )
            heights = [box.height for box in row]
            height_deviation = max(heights) - min(heights)
            max_height_deviation = max(max_height_deviation, height_deviation)
            if height_deviation > height_tolerance:
                violations.append(
                    _alignment_violation(
                        key, "row_height_deviation", height_deviation, height_tolerance, row
                    )
                )
        summaries.append(
            {
                "group": key,
                "member_count": len(members),
                "column_count": len(columns),
                "row_count": len(rows),
                "max_column_deviation_px": round(max_column_deviation, 1),
                "max_row_deviation_px": round(max_row_deviation, 1),
                "max_row_height_deviation_px": round(max_height_deviation, 1),
            }
        )
    return {
        "column_threshold_px": column_tolerance,
        "row_threshold_px": row_tolerance,
        "height_threshold_px": height_tolerance,
        "group_count": len(groups),
        "groups": summaries[:10],
        "violation_count": len(violations),
        "violations": violations[:_MAX_LISTED_ITEMS],
    }


def _alignment_violation(
    group: str,
    kind: str,
    deviation: float,
    threshold: float,
    members: list[_Box],
) -> dict[str, Any]:
    return {
        "group": group,
        "kind": kind,
        "deviation_px": round(deviation, 1),
        "threshold_px": threshold,
        "elements": [_slim_ref(box) for box in members[:5]],
    }


def _assert_aspect_ratio(
    boxes: list[_Box],
    relative_deviation: float | None,
) -> dict[str, Any]:
    """同构媒体元素组内渲染宽高比分组；配置阈值时按中位数相对偏差标记离群。"""
    media_boxes = [box for box in boxes if _is_media_element(box.element)]
    groups = _layout_groups(media_boxes)
    summaries: list[dict[str, Any]] = []
    violation_count = 0
    for key, members in groups:
        ratios = [box.width / box.height for box in members]
        counter = Counter(round(ratio, 2) for ratio in ratios)
        median_ratio = median(ratios)
        summary: dict[str, Any] = {
            "group": key,
            "member_count": len(members),
            "distinct_ratio_count": len(counter),
            "median_ratio": round(median_ratio, 4),
            "ratio_groups": [
                {"ratio": ratio, "count": count} for ratio, count in counter.most_common(6)
            ],
        }
        if relative_deviation is not None and median_ratio > 0:
            outliers = [
                {"element": _slim_ref(box), "ratio": round(ratio, 4)}
                for box, ratio in zip(members, ratios, strict=False)
                if abs(ratio - median_ratio) / median_ratio > relative_deviation
            ]
            summary["outliers"] = outliers[:10]
            violation_count += len(outliers)
        summaries.append(summary)
    result: dict[str, Any] = {"group_count": len(groups), "groups": summaries[:10]}
    if relative_deviation is not None:
        result["threshold_relative_deviation"] = relative_deviation
        result["violation_count"] = violation_count
    return result


def _assert_overlap(
    boxes: list[_Box],
    *,
    viewport_width: float | None,
    viewport_height: float | None,
    max_pairs: float,
) -> dict[str, Any]:
    """可见元素矩形两两相交；完全包含视为正常嵌套单独计数，不计违规。"""
    scoped = boxes
    if viewport_width and viewport_height:
        scoped = [
            box
            for box in boxes
            if box.x < viewport_width and box.y < viewport_height and box.x2 > 0 and box.y2 > 0
        ]
    containment_count = 0
    pairs: list[dict[str, Any]] = []
    for index, first in enumerate(scoped):
        for second in scoped[index + 1 :]:
            intersection_width = min(first.x2, second.x2) - max(first.x, second.x)
            intersection_height = min(first.y2, second.y2) - max(first.y, second.y)
            if intersection_width <= 0 or intersection_height <= 0:
                continue
            if _contains(first, second) or _contains(second, first):
                containment_count += 1
                continue
            pairs.append(
                {
                    "first": _slim_ref(first),
                    "second": _slim_ref(second),
                    "intersection_px": {
                        "width": round(intersection_width, 1),
                        "height": round(intersection_height, 1),
                        "area": round(intersection_width * intersection_height, 1),
                    },
                }
            )
    pairs.sort(key=lambda pair: -pair["intersection_px"]["area"])
    return {
        "threshold_max_pairs": max_pairs,
        "checked_element_count": len(scoped),
        "containment_pair_count": containment_count,
        "partial_overlap_pair_count": len(pairs),
        "partial_overlap_pairs": pairs[:_MAX_LISTED_ITEMS],
        "exceeds_threshold": len(pairs) > max_pairs,
    }


def _assert_spacing(boxes: list[_Box], *, min_gap: float | None) -> dict[str, Any]:
    """交互控件间沿轴向的最近邻间距；重叠对由 overlap 族负责，此处跳过。"""
    controls = [box for box in boxes if _is_interactive(box.element)]
    gaps: list[dict[str, Any]] = []
    for index, first in enumerate(controls):
        for second in controls[index + 1 :]:
            measured = _axis_gap(first, second)
            if measured is None:
                continue
            gap, axis = measured
            gaps.append(
                {
                    "first": _slim_ref(first),
                    "second": _slim_ref(second),
                    "gap_px": round(gap, 1),
                    "axis": axis,
                }
            )
    gaps.sort(key=lambda item: item["gap_px"])
    result: dict[str, Any] = {
        "control_count": len(controls),
        "measured_pair_count": len(gaps),
        "min_gap_px": gaps[0]["gap_px"] if gaps else None,
        "closest_pairs": gaps[:_MAX_LISTED_ITEMS],
    }
    if min_gap is not None:
        violations = [item for item in gaps if item["gap_px"] < min_gap]
        result["threshold_px"] = min_gap
        result["violation_count"] = len(violations)
        result["violations"] = violations[:_MAX_LISTED_ITEMS]
    return result


def _axis_gap(first: _Box, second: _Box) -> tuple[float, str] | None:
    """轴向投影相邻的两矩形边到边距离；重叠或纯对角关系返回 None。"""
    x_overlap = min(first.x2, second.x2) - max(first.x, second.x)
    y_overlap = min(first.y2, second.y2) - max(first.y, second.y)
    if x_overlap > 0 and y_overlap > 0:
        return None
    if x_overlap > 0:
        return max(first.y, second.y) - min(first.y2, second.y2), "vertical"
    if y_overlap > 0:
        return max(first.x, second.x) - min(first.x2, second.x2), "horizontal"
    return None


def _layout_groups(boxes: list[_Box]) -> list[tuple[str, list[_Box]]]:
    """按 tag 与前两个 class token 归组，仅保留可构成列表结构的组。"""
    grouped: dict[str, list[_Box]] = {}
    for box in boxes:
        tag = str(box.element.get("tag") or "").casefold()
        if not tag:
            continue
        tokens = _string_list(box.element.get("class_tokens"))[:2]
        key = ".".join([tag, *tokens])
        grouped.setdefault(key, []).append(box)
    return sorted(
        ((key, members) for key, members in grouped.items() if len(members) >= _MIN_GROUP_MEMBERS),
        key=lambda item: -len(item[1]),
    )


def _cluster(boxes, *, axis, tolerance: float) -> list[list[_Box]]:
    """按单轴坐标聚类：与簇首坐标差超过容差即开新簇。"""
    ordered = sorted(boxes, key=axis)
    clusters: list[list[_Box]] = []
    current: list[_Box] = []
    anchor = 0.0
    for box in ordered:
        value = axis(box)
        if current and value - anchor > tolerance:
            clusters.append(current)
            current = []
        if not current:
            anchor = value
        current.append(box)
    if current:
        clusters.append(current)
    return clusters


def _contains(outer: _Box, inner: _Box) -> bool:
    return (
        outer.x <= inner.x + _CONTAINMENT_EPSILON_PX
        and outer.y <= inner.y + _CONTAINMENT_EPSILON_PX
        and outer.x2 >= inner.x2 - _CONTAINMENT_EPSILON_PX
        and outer.y2 >= inner.y2 - _CONTAINMENT_EPSILON_PX
    )


def _is_interactive(item: dict[str, Any]) -> bool:
    tag = str(item.get("tag") or "").casefold()
    role = str(item.get("role") or "").casefold()
    return tag in _INTERACTIVE_TAGS or role in _INTERACTIVE_ROLES


def _slim_ref(box: _Box) -> dict[str, Any]:
    """成对列举场景下的紧凑元素引用，避免撑爆 Judge 上下文。"""
    element = box.element
    ref: dict[str, Any] = {
        "selector": element.get("selector"),
        "tag": element.get("tag"),
        "rect": {
            "x": round(box.x, 1),
            "y": round(box.y, 1),
            "width": round(box.width, 1),
            "height": round(box.height, 1),
        },
    }
    text = str(element.get("text") or "").strip()
    if text:
        ref["text"] = text[:40]
    return {key: value for key, value in ref.items() if value not in (None, "")}


def _positive(value: Any) -> float | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return number


__all__ = ["run_layout_geometry_assertions"]
