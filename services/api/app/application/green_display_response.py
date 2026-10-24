"""从现有H5展示生产器原件判读选档与图片预览，不发起新动作。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from services.api.app.application.green_local_response import _number, _same_document, _snapshot
from services.api.app.application.h5_probe_targets import (
    probe_subject_key,
    select_probe_target,
    visible_probe_element,
)
from services.api.app.domain.interaction import ActionIntent, InteractionOperation, SurfaceKind

# 原生视频全屏由Driver直接调用API，不能作为产品点击响应的合格证明。
_GOALS = {"payment_plan_selection", "media_fullscreen_pair"}


def read_extended_display_response(action, before_capture, after_capture, task_id):
    """只认现有h5-probe-display-effect.v1及其真实前后对象，不按结果布尔值直接通过。"""
    intent = ActionIntent.from_dict(json.loads(action.intent_payload))
    parameters = intent.parameters
    goal, subject_key = parameters.get("goal_capability"), parameters.get("probe_subject_key")
    if (
        goal not in _GOALS
        or parameters.get("target_mode") != "h5_probe_display_target"
        or intent.task_id != task_id
        or intent.session_id != action.session_id
        or not action.session_id.startswith("h5:")
        or intent.surface_kind is not SurfaceKind.WEB
        or intent.action_type is not InteractionOperation.CLICK
        or before_capture.session_id != action.session_id
        or after_capture.session_id != action.session_id
        or before_capture.capture_id != action.source_capture_id
        or after_capture.capture_id != action.result_capture_id
        or before_capture.capture_id == after_capture.capture_id
        or before_capture.action_id != action.action_id
        or after_capture.action_id != action.action_id
        or before_capture.role != "before_action"
        or after_capture.role != "after_action"
        or before_capture.parent_capture_id != intent.source_capture_id
        or after_capture.parent_capture_id != before_capture.capture_id
        or before_capture.command_sequence != action.command_sequence
        or after_capture.command_sequence != action.command_sequence
        or not isinstance(subject_key, str)
        or len(subject_key) != 64
    ):
        return None
    before, after = _snapshot(before_capture), _snapshot(after_capture)
    if before is None or after is None or not _same_document(before, after, action):
        return None
    source = select_probe_target(
        before,
        goal=goal,
        page_type=parameters.get("source_page_type"),
        expected_key=subject_key,
    )
    if source is None:
        return None
    sources = [item for item in after["elements"] if item.get("selector") == source["selector"]]
    if len(sources) != 1 or probe_subject_key(sources[0]) != subject_key:
        return None
    if goal == "media_fullscreen_pair":
        current = _new_image_preview(before, after, source)
    else:
        current = sources[0]
    if current is None:
        return None
    effect = _native_effect(after_capture, goal, subject_key, source, current, after)
    if effect is None:
        return None
    if goal == "payment_plan_selection":
        if effect.get("readonly_isolation") is not True:
            return None
        fields = ("aria_checked",) if source.get("role") == "radio" else ("aria_selected",)
        if not any(source.get(key) == "false" and current.get(key) == "true" for key in fields):
            return None
        return "pass", "只读隔离下正式选档动作后同一档位的原生选中语义实际改变"
    return "pass", "正式图片预览动作后新出现唯一预览根，同一图片资源已实际呈现"


def _native_effect(capture, goal, subject_key, source, current, snapshot):
    originals = [item for item in capture.originals if item.artifact_type == "action_trace"]
    if len(originals) != 1:
        return None
    entries = json.loads(originals[0].payload)
    if not isinstance(entries, list) or not all(isinstance(item, Mapping) for item in entries):
        return None
    matches = [
        (entry, state["h5_probe_effect"])
        for entry in entries
        if isinstance(state := entry.get("state_diff"), Mapping)
        and isinstance(state.get("h5_probe_effect"), Mapping)
    ]
    if len(matches) != 1:
        return None
    entry, effect = matches[0]
    if (
        entry.get("action") != "click"
        or entry.get("selector") != source["selector"]
        or entry.get("comparison_only") is True
        or entry.get("diagnostic_capture") is True
        or entry.get("result") != "probe_effect_observed"
        or effect.get("protocol_version") != "h5-probe-display-effect.v1"
        or effect.get("goal") != goal
        or effect.get("subject_key") != subject_key
        or effect.get("verified") is not True
        or effect.get("before_selector") != source["selector"]
        or effect.get("before_resource") != source.get("src")
        or effect.get("before_rect") != source.get("rect")
        or effect.get("after_selector") != current.get("selector")
        or effect.get("after_resource") != current.get("src")
        or effect.get("after_rect") != current.get("rect")
        or effect.get("url") != snapshot.get("url")
        or effect.get("document_time_origin_ms") != snapshot["document"]["time_origin_ms"]
        or type(effect.get("readonly_denied_count")) is not int
        or effect["readonly_denied_count"] != 0
    ):
        return None
    if goal == "media_fullscreen_pair" and (
        effect.get("preview_root_id") != current.get("h5_probe_preview_root")
    ):
        return None
    return effect


def _preview_roots(snapshot):
    if snapshot.get("h5_probe_preview_roots_truncated") is not False:
        return None
    roots = snapshot.get("h5_probe_preview_roots")
    states = snapshot.get("h5_probe_preview_root_states")
    if (
        not isinstance(roots, list)
        or not all(isinstance(root, str) and root for root in roots)
        or len(roots) != len(set(roots))
        or not isinstance(states, list)
        or not all(isinstance(state, Mapping) for state in states)
    ):
        return None
    result = {state.get("root"): state.get("visible") for state in states}
    if len(result) != len(states) or set(result) != set(roots):
        return None
    return result if all(type(value) is bool for value in result.values()) else None


def _new_image_preview(before, after, source):
    left, right = _preview_roots(before), _preview_roots(after)
    if left is None or right is None:
        return None
    roots = set(right) - set(left)
    if len(roots) != 1 or any(right.get(root) != value for root, value in left.items()):
        return None
    root = next(iter(roots))
    if right[root] is not True:
        return None
    images = [
        item
        for item in after["elements"]
        if item.get("h5_probe_preview_root") == root
        and item.get("tag") == "img"
        and item.get("src") == source.get("src")
    ]
    if len(images) != 1:
        return None
    image, view = images[0], after.get("viewport") or {}
    resource, rect = image.get("image") or {}, image.get("rect") or {}
    source_rect = source.get("rect") or {}
    if (
        resource.get("complete") is not True
        or image.get("visible") is not True
        or not visible_probe_element(image, after)
        or not all(
            _number(resource.get(key)) and resource[key] > 0
            for key in ("natural_width", "natural_height")
        )
        or not all(_number(rect.get(key)) for key in ("x", "y", "width", "height"))
        or min(rect["width"], rect["height"]) <= 0
        or rect["width"] * rect["height"] <= source_rect["width"] * source_rect["height"]
        or not all(_number(view.get(key)) and view[key] > 0 for key in ("width", "height"))
        or max(0, rect["x"]) >= min(view["width"], rect["x"] + rect["width"])
        or max(0, rect["y"]) >= min(view["height"], rect["y"] + rect["height"])
    ):
        return None
    return image
