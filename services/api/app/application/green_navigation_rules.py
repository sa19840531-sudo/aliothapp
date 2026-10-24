"""绿色32的共享几何与动作判规；仅接收服务端核验绑定的导航事实。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from math import isfinite

from services.api.app.application.green_rule_evidence import GreenSubitem

_KEYS = frozenset(
    {
        "equal_width_navigation",
        "fit_navigation_spacing",
        "overflow_scroll",
        "initial_next_label",
    }
)


def judge_navigation_subitem(key, views) -> GreenSubitem:
    """逐个完整导航对象判规；反例优先，其他对象的缺口仍随结果保留。"""
    from services.api.app.application.green_fact_bindings import GreenFactView

    if key not in _KEYS:
        raise ValueError("不支持的绿色32分项")
    results, captures, actions = [], [], []
    for view in views:
        if not isinstance(view, GreenFactView):
            results.append(
                ("evidence_insufficient", "导航事实未经服务端绑定", ("verified_navigation_view",))
            )
            continue
        captures.extend(view.capture_ids)
        actions.extend(view.action_ids)
        if view.issues:
            results.append(
                ("evidence_insufficient", "导航原件存在读取或绑定缺口", tuple(view.issues))
            )
            continue
        navigation, missing = _navigation(view.get("navigation"))
        if missing:
            result = ("evidence_insufficient", "完整导航或坐标事实不足", missing)
        elif key == "equal_width_navigation":
            result = _equal_width(navigation)
        elif (
            key in {"fit_navigation_spacing", "overflow_scroll"} and len(navigation["siblings"]) < 5
        ):
            result = ("skipped", "完整分类数为2—4，不适用5类以上分支", ())
        elif key == "fit_navigation_spacing":
            result = _fit_spacing(navigation)
        elif key == "overflow_scroll":
            result = _overflow_scroll(navigation, view)
        else:
            result = _initial_label(navigation)
        status, reason, gaps = result
        results.append((status, reason, tuple(f"{view.subject_id}:{gap}" for gap in gaps)))
    if not results:
        results.append(
            ("evidence_insufficient", "没有已核验的完整导航对象", ("verified_navigation_view",))
        )
    missing = tuple(dict.fromkeys(gap for _, _, gaps in results for gap in gaps))
    status = (
        "fail"
        if any(value[0] == "fail" for value in results)
        else "evidence_insufficient"
        if missing or any(value[0] == "evidence_insufficient" for value in results)
        else "pass"
        if any(value[0] == "pass" for value in results)
        else "skipped"
    )
    return GreenSubitem(
        key,
        status,
        "；".join(dict.fromkeys(value[1] for value in results)),
        missing,
        tuple(dict.fromkeys(captures)),
        tuple(dict.fromkeys(actions)),
    )


def _navigation(value):
    if not isinstance(value, Mapping):
        return None, ("complete_navigation_geometry",)
    if (
        value.get("siblings_complete") is not True
        or value.get("coordinate_space") != "viewport_css_px"
        or value.get("geometry_precision") != "raw"
        or value.get("geometry_axis_aligned") is not True
    ):
        return None, ("complete_axis_aligned_navigation",)
    viewport, container, siblings = (
        value.get("viewport"),
        value.get("container"),
        value.get("siblings"),
    )
    if (
        not isinstance(viewport, Mapping)
        or not isinstance(container, Mapping)
        or not isinstance(siblings, (list, tuple))
        or not 2 <= len(siblings) <= 300
        or not all(isinstance(item, Mapping) for item in siblings)
    ):
        return None, ("complete_navigation_siblings_and_container",)
    if (
        not _identity(container.get("id"))
        or not _identity(container.get("selector"))
        or not _identity(value.get("document_id"))
        or container.get("direction") != "ltr"
    ):
        return None, ("navigation_document_container_and_direction",)
    ids = [item.get("id") for item in siblings]
    ordinals = [item.get("ordinal") for item in siblings]
    if (
        not all(_identity(identity) for identity in ids)
        or len(set(ids)) != len(ids)
        or any(type(ordinal) is not int for ordinal in ordinals)
        or sorted(ordinals) != list(range(1, len(siblings) + 1))
    ):
        return None, ("unique_complete_navigation_siblings",)
    width, height, scale = (_number(viewport.get(key)) for key in ("width", "height", "scale"))
    outer, clip = _rect(container.get("rect")), _rect(container.get("clip_rect"))
    slots = [_rect(item.get("slot_rect")) for item in siblings]
    if (
        None in (width, height, scale)
        or min(width, height, scale) <= 0
        or outer is None
        or clip is None
        or any(rect is None for rect in slots)
        or not _contains(outer, clip)
        or not _contains((Decimal(0), Decimal(0), width, height), clip)
    ):
        return None, ("navigation_usable_clip_and_viewport",)
    result = dict(value)
    result.update(
        container=dict(container), siblings=tuple(siblings), slots=tuple(slots), clip=clip
    )
    return result, ()


def _equal_width(navigation):
    slots, clip = sorted(navigation["slots"]), navigation["clip"]
    count = len(slots)
    if count >= 5:
        return "skipped", "完整分类数为5类以上，不适用2—4类等宽分支", ()
    if len({slot[2] for slot in slots}) > 1:
        return "fail", "同级完整导航槽位的未舍入原始宽度不同", ()
    if not _single_row(slots) or not all(_contains(clip, slot) for slot in slots):
        return (
            "evidence_insufficient",
            "尚不能证明完整同级槽位位于同一可用横向区域",
            ("visible_single_navigation_row",),
        )
    centered = all(
        count * (2 * (slot[0] - clip[0]) + slot[2]) == (2 * index + 1) * clip[2]
        for index, slot in enumerate(slots)
    )
    if centered and slots[0][2] * count <= clip[2]:
        return "pass", "完整同级槽位等宽，分别居于导航可用区域各等分区域的中心", ()
    gaps = [right[0] - left[0] - left[2] for left, right in zip(slots, slots[1:], strict=False)]
    if any(gap < 0 for gap in gaps) or len(set(gaps)) > 1:
        return "fail", "同级等宽槽位的实际间隔不均或存在重叠，不能等分导航区域", ()
    return (
        "evidence_insufficient",
        "槽位宽度相同，但现有几何尚未证明可用区域被等分",
        ("navigation_equal_area_distribution",),
    )


def _fit_spacing(navigation):
    if not all(_contains(navigation["clip"], slot) for slot in navigation["slots"]):
        return "skipped", "完整分类未全部容纳，转入横向滚动分支", ()
    labels, missing = _labels(navigation)
    if missing:
        return "evidence_insufficient", "尚缺完整的真实文字Range与有效裁剪范围", missing
    if any(not complete for _, complete in labels):
        return "fail", "可容纳导航中存在被实际裁剪的分类文字", ()
    boxes = sorted(box for box, _ in labels)
    if not _single_row(boxes):
        return (
            "evidence_insufficient",
            "文字不在可核验的同一横向行，不能比较横向间距",
            ("single_navigation_text_row",),
        )
    gaps = [right[0] - left[0] - left[2] for left, right in zip(boxes, boxes[1:], strict=False)]
    if any(gap < 0 for gap in gaps) or len(set(gaps)) > 1:
        return "fail", "完整分类文字的实际相邻间距不均衡或文字范围重叠", ()
    return "pass", "全部分类文字完整呈现，实际相邻文字间距相等", ()


def _overflow_scroll(navigation, view):
    if all(_contains(navigation["clip"], slot) for slot in navigation["slots"]):
        return "skipped", "完整分类槽位已全部容纳，不触发未容纳时横滚分支", ()
    container, probe = navigation["container"], navigation.get("scroll_probe")
    if not isinstance(probe, Mapping):
        return (
            "evidence_insufficient",
            "未容纳导航尚缺同容器正式横向手势的前后原件",
            ("formal_horizontal_scroll_probe",),
        )
    required_true = ("completed", "window_complete", "gesture_delivered")
    if (
        any(probe.get(key) is not True for key in required_true)
        or probe.get("interrupted") is not False
        or probe.get("position_kind") != "scroll_left"
        or probe.get("action_id") not in view.action_ids
        or probe.get("before_capture_id") not in view.capture_ids
        or probe.get("after_capture_id") not in view.capture_ids
        or probe.get("before_capture_id") == probe.get("after_capture_id")
        or probe.get("before_document_id") != navigation["document_id"]
        or probe.get("after_document_id") != navigation["document_id"]
        or probe.get("before_container_id") != container["id"]
        or probe.get("after_container_id") != container["id"]
    ):
        return (
            "evidence_insufficient",
            "横向手势缺完整过程或同动作、文档、容器的真实归属",
            ("same_container_completed_native_gesture",),
        )
    before, after, anchor, client, scroll = (
        _number(value)
        for value in (
            probe.get("before_scroll_left"),
            probe.get("after_scroll_left"),
            container.get("scroll_left"),
            container.get("client_width"),
            container.get("scroll_width"),
        )
    )
    if (
        None in (before, after, anchor, client, scroll)
        or client <= 0
        or scroll <= client
        or anchor != before
        or min(before, after) < 0
        or max(before, after) > scroll - client
    ):
        return (
            "evidence_insufficient",
            "同容器滚动位置或可滚范围不能由原件一致确认",
            ("consistent_native_scroll_offsets",),
        )
    if after != before:
        return "pass", "同一正式横向手势使同导航容器的真实滚动位置改变", ()
    direction = probe.get("direction")
    can_move = (direction == "forward" and before < scroll - client) or (
        direction == "backward" and before > 0
    )
    if not can_move:
        return (
            "evidence_insufficient",
            "手势方向位于边界或方向不明，零位移不能证明横滚不可用",
            ("nonboundary_horizontal_gesture",),
        )
    return "fail", "同容器在可移动方向收到正式手势且完整过程结束，实际滚动位置未改变", ()


def _initial_label(navigation):
    container, initial = navigation["container"], navigation.get("initial")
    if (
        not isinstance(initial, Mapping)
        or initial.get("is_initial") is not True
        or type(initial.get("prior_horizontal_actions")) is not int
        or initial["prior_horizontal_actions"] != 0
        or _number(initial.get("scroll_left")) is None
        or _number(initial.get("scroll_left")) != _number(container.get("scroll_left"))
    ):
        return (
            "evidence_insufficient",
            "尚缺进入导航时的初始状态及同容器滚动位置证明",
            ("initial_navigation_state_and_scroll",),
        )
    labels, missing = _labels(navigation)
    if missing:
        return "evidence_insufficient", "初始露出判定尚缺完整真实文字Range与裁剪范围", missing
    boxes = [box for box, _ in labels]
    if not _single_row(boxes):
        return (
            "evidence_insufficient",
            "初始导航文字没有可核验的同一横向行",
            ("single_navigation_text_row",),
        )
    clip = navigation["clip"]
    next_labels = sorted(
        (box, complete, index)
        for index, (box, complete) in enumerate(labels)
        if box[0] + box[2] > clip[0] + clip[2]
    )
    if not next_labels:
        return "skipped", "完整初始枚举证明右侧没有尚待显示的分类文字", ()
    box, _, index = next_labels[0]
    row = navigation["siblings"][index]
    effective_clip = _rect(row.get("clip_rect"), allow_empty=True)
    shown = _intersection_width(box, clip, effective_clip)
    if Decimal(0) < shown < box[2]:
        return "pass", "初始右侧仍有分类，下一项的真实文字范围已部分露出", ()
    return "fail", "初始右侧仍有分类，但下一项真实文字没有部分露出", ()


def _labels(navigation):
    if navigation.get("text_ranges_complete") is not True:
        return (), ("complete_nonwhitespace_text_ranges",)
    labels = []
    for row in navigation["siblings"]:
        box = _label_box(row)
        clip = _rect(row.get("clip_rect"), allow_empty=True)
        if box is None or clip is None:
            return (), ("complete_label_text_ranges_and_effective_clip",)
        complete = all(
            _contains(clip, rect) and _contains(navigation["clip"], rect)
            for rect in (_rect(value) for value in row["text_rects"])
        )
        labels.append((box, complete))
    return tuple(labels), ()


def _label_box(row):
    rects = row.get("text_rects")
    if (
        not isinstance(row.get("text"), str)
        or not row["text"].strip()
        or not isinstance(rects, (list, tuple))
        or not 1 <= len(rects) <= 300
    ):
        return None
    boxes = [_rect(value) for value in rects]
    if any(box is None for box in boxes) or not _single_row(boxes):
        return None
    left, top = min(box[0] for box in boxes), min(box[1] for box in boxes)
    return (
        left,
        top,
        max(box[0] + box[2] for box in boxes) - left,
        max(box[1] + box[3] for box in boxes) - top,
    )


def _single_row(boxes):
    return bool(boxes) and max(box[1] for box in boxes) < min(box[1] + box[3] for box in boxes)


def _intersection_width(box, *clips):
    left, right = box[0], box[0] + box[2]
    for clip in clips:
        if clip is None or box[1] >= clip[1] + clip[3] or box[1] + box[3] <= clip[1]:
            return Decimal(0)
        left, right = max(left, clip[0]), min(right, clip[0] + clip[2])
    return max(Decimal(0), right - left)


def _rect(value, *, allow_empty=False):
    if not isinstance(value, Mapping):
        return None
    rect = tuple(_number(value.get(key)) for key in ("x", "y", "width", "height"))
    if None in rect or min(rect[2:]) < 0 or (not allow_empty and min(rect[2:]) == 0):
        return None
    return rect


def _contains(outer, inner):
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[0] + inner[2] <= outer[0] + outer[2]
        and inner[1] + inner[3] <= outer[1] + outer[3]
    )


def _number(value):
    try:
        return Decimal(str(value)) if type(value) in (int, float) and isfinite(value) else None
    except OverflowError:
        return None


def _identity(value):
    return isinstance(value, str) and bool(value.strip())
