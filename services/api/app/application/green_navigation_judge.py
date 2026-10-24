"""绿色32消费现有导航原件形成分项；未生产的初始状态和文字边界保持缺证。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from math import isfinite

from services.api.app.application.green_navigation_geometry import (
    centered_in_equal_regions,
    fully_in_viewport,
)
from services.api.app.application.green_navigation_structure import read_navigation_groups
from services.api.app.infrastructure.rule_judge_evidence import TrustedEvidenceBundle

_KEYS = (
    "equal_width_navigation",
    "fit_navigation_spacing",
    "overflow_scroll",
    "initial_next_label",
)
# 既有 ui-snapshot.v2 将 CSS 矩形舍入到两位小数；此值只表示读数精度，不是业务容差。
_RECT_PRECISION = 0.01


def judge_green_navigation(bundle):
    """只读取同一 primary Capture 的既有 HTML/UI 原件，不采用新增自报事实。"""
    missing = {key: [] for key in _KEYS}
    observations = {key: [] for key in _KEYS}
    if not isinstance(bundle, TrustedEvidenceBundle):
        return _finish(observations, missing, ("trusted_page_originals",))
    primary = bundle.capture_roles.get("primary")
    artifacts = [item for item in bundle.artifacts if item.page_capture_id == primary]
    snapshots = [item for item in artifacts if item.artifact_type == "ui_snapshot"]
    doms = [item for item in artifacts if item.artifact_type == "dom"]
    if len(snapshots) != 1 or len(doms) != 1:
        return _finish(observations, missing, ("same_capture_navigation_dom_and_ui_snapshot",))
    try:
        snapshot, dom = snapshots[0].decode_json(), doms[0].decode_text()
    except ValueError:
        return _finish(observations, missing, ("navigation_original_unreadable",))
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("schema_version") != "ui-snapshot.v2"
        or any(
            snapshot.get(key)
            for key in (
                "capture_degraded",
                "collection_error",
                "budget_truncated",
                "element_limit_reached",
            )
        )
    ):
        return _finish(observations, missing, ("complete_browser_navigation_snapshot",))
    viewport = snapshot.get("viewport")
    if not isinstance(viewport, Mapping) or not all(
        _number(viewport.get(key)) is not None and viewport[key] > 0
        for key in ("width", "height", "visual_viewport_scale")
    ):
        return _finish(observations, missing, ("navigation_viewport_and_scale",))
    groups, issues = read_navigation_groups(dom, snapshot)
    source_refs = [snapshots[0].artifact_id, doms[0].artifact_id]
    for group in groups:
        boxes = [_rect(row.get("rect")) for row in group.slots]
        container = _rect(group.container.get("rect")) if group.container is not None else None
        if any(box is None for box in boxes):
            for key in _KEYS:
                missing[key].append(group.identity + ":sibling_bounds")
            continue
        # 多行、旋转或离屏槽位不能借横向宽度判导航分布。
        if max(box[1] for box in boxes) - min(box[1] for box in boxes) > _RECT_PRECISION:
            for key in _KEYS:
                missing[key].append(group.identity + ":single_navigation_row")
            continue
        count = len(boxes)
        fits = container is not None and all(_contains(container, box) for box in boxes)
        widths = [box[2] for box in boxes]
        if 2 <= count <= 4:
            # JSON 十进制读数不能先用二进制浮点相减，否则精度边界会误成明确反例。
            readings = [Decimal(str(width)) for width in widths]
            if max(readings) - min(readings) > Decimal(str(_RECT_PRECISION)):
                _observed(
                    observations,
                    "equal_width_navigation",
                    group,
                    "fail",
                    "完整同级导航槽位宽度不同：" + "、".join(map(str, widths)),
                    source_refs,
                )
            elif len(set(widths)) > 1:
                missing["equal_width_navigation"].append(
                    group.identity + ":width_measurement_resolution"
                )
            elif fits and fully_in_viewport(container, viewport) and (
                _tiles(container, boxes) or centered_in_equal_regions(container, boxes)
            ):
                _observed(
                    observations,
                    "equal_width_navigation",
                    group,
                    "pass",
                    "完整同级导航槽位在可见容器中等宽且连续分布"
                    if _tiles(container, boxes)
                    else "完整同级导航控件等宽，中心分别位于可见容器各等分区域的中心",
                    source_refs,
                )
            else:
                missing["equal_width_navigation"].append(
                    group.identity + ":navigation_usable_container"
                )
            for key in ("fit_navigation_spacing", "overflow_scroll"):
                _observed(
                    observations,
                    key,
                    group,
                    "skipped",
                    "完整分类数为2—4，不适用5类以上分支",
                    source_refs,
                )
        else:
            _observed(
                observations,
                "equal_width_navigation",
                group,
                "skipped",
                "完整分类数为5类以上，不适用2—4类等宽分支",
                source_refs,
            )
            _five_or_more(group, boxes, container, fits, observations, missing, source_refs)
        # document.scroll_x 不是导航容器 scrollLeft，也不是进入页面时的初始证明。
        missing["initial_next_label"].extend(
            (
                group.identity + ":initial_navigation_state",
                group.identity + ":next_label_text_bounds",
            )
        )
    return _finish(observations, missing, issues)


def _five_or_more(group, boxes, container, fits, observations, missing, refs):
    if fits:
        possible_overflow = any(
            text_only and _possible_text_overflow(row)
            for row, text_only in zip(group.controls, group.text_only, strict=True)
        )
        if possible_overflow:
            _observed(
                observations,
                "fit_navigation_spacing",
                group,
                "evidence_insufficient",
                "标签元素存在溢出与省略样式，但装饰伪元素也可造成同样读数，尚缺真实文字边界",
                refs,
            )
        missing["fit_navigation_spacing"].extend(
            (group.identity + ":text_bounds", group.identity + ":navigation_spacing")
        )
        _observed(
            observations,
            "overflow_scroll",
            group,
            "skipped",
            "全部同级槽位已位于容器内，不适用未容纳时横向滚动分支",
            refs,
        )
    elif container is not None:
        missing["fit_navigation_spacing"].append(group.identity + ":full_text_fit_condition")
        style = group.container.get("style")
        metrics = group.container.get("text_metrics")
        if (
            isinstance(style, Mapping)
            and style.get("overflow_x") in {"auto", "scroll"}
            and isinstance(metrics, Mapping)
            and _number(metrics.get("scroll_width")) is not None
            and _number(metrics.get("client_width")) is not None
            and metrics["scroll_width"] > metrics["client_width"] > 0
        ):
            _observed(
                observations,
                "overflow_scroll",
                group,
                "evidence_insufficient",
                "原生样式和滚动宽度表明存在横向范围，尚无正式动作证明实际可横向滚动",
                refs,
            )
        missing["overflow_scroll"].append(group.identity + ":horizontal_scroll_behavior")
    else:
        for key in ("fit_navigation_spacing", "overflow_scroll"):
            missing[key].append(group.identity + ":navigation_usable_container")


def _possible_text_overflow(row):
    style, metrics = row.get("style"), row.get("text_metrics")
    return (
        isinstance(style, Mapping)
        and isinstance(metrics, Mapping)
        and style.get("white_space") == "nowrap"
        and style.get("text_overflow") == "ellipsis"
        and style.get("overflow_x") in {"hidden", "clip"}
        and metrics.get("horizontal_overflow") is True
    )


def _observed(observations, key, group, status, reason, refs):
    observations[key].append(
        {"group": group.identity, "status": status, "reason": reason, "evidence_refs": list(refs)}
    )


def _finish(observations, missing, common):
    items = []
    for key in _KEYS:
        values = observations[key]
        gaps = list(dict.fromkeys((*common, *missing[key])))
        if not values and not gaps:
            gaps.append("complete_navigation_siblings_missing")
        status = (
            "fail"
            if any(value["status"] == "fail" for value in values)
            else "evidence_insufficient"
            if gaps
            else "pass"
            if any(value["status"] == "pass" for value in values)
            else "skipped"
        )
        refs = list(dict.fromkeys(ref for value in values for ref in value["evidence_refs"]))
        evaluated = any(value["status"] in {"pass", "fail", "skipped"} for value in values)
        items.append(
            {
                "key": key,
                "status": status,
                "evaluated": evaluated,
                "confidence": 1.0 if evaluated else None,
                "reason": "；".join(dict.fromkeys(value["reason"] for value in values))
                if values
                else "现有导航原件不能证明该分项",
                "missing": gaps,
                "evidence_refs": refs,
                "observations": values,
            }
        )
    status = (
        "fail"
        if any(item["status"] == "fail" for item in items)
        else "evidence_insufficient"
        if any(item["missing"] for item in items)
        else "pass"
    )
    return {
        "execution_status": status,
        "confidence": 1.0 if status == "fail" else 0.0,
        "reason": "导航原件存在明确反例，其余缺项继续保留"
        if status == "fail"
        else "已判现有导航分项；完整文字边界或初始状态仍缺证",
        "missing_evidence": list(dict.fromkeys(gap for item in items for gap in item["missing"])),
        "subitems": items,
        "legacy_visual_fallback_allowed": False,
    }


def _number(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else None


def _rect(value):
    if not isinstance(value, Mapping):
        return None
    values = tuple(_number(value.get(key)) for key in ("x", "y", "width", "height"))
    return values if None not in values and values[2] > 0 and values[3] > 0 else None


def _contains(outer, inner):
    return (
        inner[0] >= outer[0] - _RECT_PRECISION
        and inner[1] >= outer[1] - _RECT_PRECISION
        and inner[0] + inner[2] <= outer[0] + outer[2] + _RECT_PRECISION
        and inner[1] + inner[3] <= outer[1] + outer[3] + _RECT_PRECISION
    )


def _tiles(container, boxes):
    ordered = sorted(boxes)
    return (
        abs(ordered[0][0] - container[0]) <= _RECT_PRECISION
        and abs(ordered[-1][0] + ordered[-1][2] - container[0] - container[2]) <= _RECT_PRECISION
        and all(
            abs(left[0] + left[2] - right[0]) <= _RECT_PRECISION
            for left, right in zip(ordered, ordered[1:], strict=False)
        )
    )
