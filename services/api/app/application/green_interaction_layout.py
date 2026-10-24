"""完整交互布局窗口及合法叠层的确定性判读。"""

from collections.abc import Mapping

from services.api.app.application.green_interaction_fact_values import _ids, _time, _window
from services.api.app.application.green_interaction_geometry import (
    _area,
    _box_area,
    _intersection,
    _rect,
)


def _layout(view):
    window, frames = view.get("layout_window"), view.get("layout_frames")
    if (
        not view.capture_ids
        or not _window(window)
        or not isinstance(frames, (list, tuple))
        or not frames
    ):
        return "evidence_insufficient", "尚缺完整布局窗口与原件", ["complete_interactive_layout"]
    if (
        not isinstance(frames[0], Mapping)
        or not isinstance(frames[-1], Mapping)
        or frames[0].get("at_ms") != window["start_ms"]
        or frames[-1].get("at_ms") != window["end_ms"]
        or len(frames) < 2
    ):
        return (
            "evidence_insufficient",
            "布局帧没有覆盖完整观察窗口",
            ["complete_layout_window_boundaries"],
        )
    gaps, states, previous, target_states = [], [], None, {}
    for frame in frames:
        if (
            not isinstance(frame, Mapping)
            or not _time(frame.get("at_ms"))
            or frame.get("clock_id") != window["clock_id"]
            or not window["start_ms"] <= frame["at_ms"] <= window["end_ms"]
            or (previous is not None and frame["at_ms"] <= previous)
        ):
            gaps.append("ordered_layout_frames_in_window")
            continue
        previous = frame["at_ms"]
        viewport, targets, layers = (
            _rect(frame.get("viewport")),
            frame.get("targets"),
            frame.get("layers"),
        )
        census = frame.get("interactive_target_ids")
        if (
            viewport is None
            or not isinstance(targets, (list, tuple))
            or not isinstance(layers, (list, tuple))
            or not _ids(census)
            or not all(isinstance(item, Mapping) for item in (*targets, *layers))
            or len({item.get("target_id") for item in targets}) != len(targets)
            or set(census) != {item.get("target_id") for item in targets}
            or not targets
        ):
            gaps.append("complete_layout_target_census")
            continue
        layer_map = {layer.get("layer_id"): layer for layer in layers}
        if len(layer_map) != len(layers):
            gaps.append("unique_layer_identity")
            continue
        for target in targets:
            target_id = target["target_id"]
            if (
                type(target.get("required_in_current_state")) is not bool
                or type(target.get("enabled")) is not bool
            ):
                gaps.append(target_id + ":current_interactive_state")
                continue
            if not target["enabled"] or not target["required_in_current_state"]:
                states.append("skipped")
                target_states.setdefault(target_id, []).append("skipped")
                continue
            suspended = [
                layer for layer in layers if target_id in layer.get("suspended_target_ids", ())
            ]
            if suspended:
                if (
                    len(suspended) == 1
                    and suspended[0].get("kind") == "modal_dialog"
                    and suspended[0].get("active") is True
                    and suspended[0].get("modal") is True
                    and target.get("owner_layer_id") != suspended[0]["layer_id"]
                    and target.get("inert") is True
                ):
                    states.append("skipped")
                    target_states.setdefault(target_id, []).append("skipped")
                    continue
                gaps.append(target_id + ":legal_modal_suspension")
                continue
            status, target_gaps = _target_geometry(target, viewport)
            states.append(status)
            target_states.setdefault(target_id, []).append(status)
            gaps.extend(target_id + ":" + gap for gap in target_gaps)
    # 一次过渡帧不等于持续不可使用；反例须在覆盖完整窗口的各帧保持全部输入区域被拦截。
    failed = any(
        len(values) == len(frames) and set(values) == {"fail"} for values in target_states.values()
    )
    if "fail" in states and not failed:
        gaps.append("transient_occlusion_effect_on_use")
    status = (
        "fail"
        if failed
        else "evidence_insufficient"
        if gaps or not states
        else "pass"
        if "pass" in states
        else "skipped"
    )
    reason = (
        "必需交互目标的全部有效输入区域被其他对象拦截"
        if status == "fail"
        else "完整布局中的当前必需目标均可直接接收输入"
        if status == "pass"
        else "当前状态没有需要操作的交互目标"
        if status == "skipped"
        else "布局范围、命中分区或合法叠层关系仍不完整"
    )
    return status, reason, gaps


def _target_geometry(target, viewport):
    rect, regions, hits = (
        _rect(target.get("rect")),
        target.get("activation_regions"),
        target.get("hit_regions"),
    )
    if (
        rect is None
        or not isinstance(regions, (list, tuple))
        or not regions
        or not isinstance(hits, (list, tuple))
        or target.get("hit_regions_complete") is not True
    ):
        return "evidence_insufficient", ["complete_activation_and_hit_regions"]
    regions = [_rect(item) for item in regions]
    if any(item is None or _intersection(item, rect) != item for item in regions):
        return "evidence_insufficient", ["activation_regions_within_target"]
    hit_rows = []
    for hit in hits:
        if (
            not isinstance(hit, Mapping)
            or _rect(hit.get("rect")) is None
            or not isinstance(hit.get("receiver_id"), str)
            or not hit["receiver_id"]
            or not _ids(hit.get("ancestor_ids"), allow_empty=True)
        ):
            return "evidence_insufficient", ["native_hit_receiver_identity"]
        own = (
            hit["receiver_id"] == target["target_id"] or target["target_id"] in hit["ancestor_ids"]
        )
        hit_rows.append((_rect(hit["rect"]), own))
    all_visible, all_accessible, all_blocked = True, True, True
    for region in regions:
        visible = _intersection(region, viewport)
        if visible != region:
            all_visible = False
        if visible is None:
            continue
        covered = [part for box, _own in hit_rows if (part := _intersection(box, visible))]
        own = [part for box, is_own in hit_rows if is_own and (part := _intersection(box, visible))]
        foreign = [
            part for box, is_own in hit_rows if not is_own and (part := _intersection(box, visible))
        ]
        if _area(covered) != _box_area(visible):
            return "evidence_insufficient", ["complete_native_hit_partition"]
        # 同一区域不能同时声称命中目标和外部对象，这代表原生分区互相矛盾。
        if any(_intersection(left, right) is not None for left in own for right in foreign):
            return "evidence_insufficient", ["unambiguous_native_hit_partition"]
        all_accessible = all_accessible and _area(own) == _box_area(visible)
        all_blocked = all_blocked and not own
    if not all_visible:
        return "evidence_insufficient", ["viewport_clipping_or_scroll_reachability"]
    if all_accessible:
        return "pass", []
    if all_blocked:
        return "fail", []
    return "evidence_insufficient", ["partial_occlusion_effect_on_use"]
