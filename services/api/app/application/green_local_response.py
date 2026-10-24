"""既有H5本地展示动作的原件读取与因果核验。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime

from services.api.app.application.h5_probe_journey import probe_scroll_context
from services.api.app.application.h5_probe_targets import (
    back_to_top_structure,
    disclosure_structure,
    select_probe_target,
)
from services.api.app.domain.interaction import ActionIntent, InteractionOperation, SurfaceKind


def _local_response(action, before_capture, after_capture, task_id):
    intent = ActionIntent.from_dict(json.loads(action.intent_payload))
    parameters = intent.parameters
    goal, subject_key = parameters.get("goal_capability"), parameters.get("probe_subject_key")
    if (
        intent.task_id != task_id
        or intent.session_id != action.session_id
        or not action.session_id.startswith("h5:")
        or intent.surface_kind is not SurfaceKind.WEB
        or intent.action_type is not InteractionOperation.CLICK
        or parameters.get("target_mode") != "h5_probe_display_target"
        or goal not in {"scroll_button_feedback", "scroll_back_to_top"}
        or not isinstance(subject_key, str)
        or len(subject_key) != 64
        or before_capture.session_id != action.session_id
        or after_capture.session_id != action.session_id
        or before_capture.action_id != action.action_id
        or after_capture.action_id != action.action_id
        or before_capture.role != "before_action"
        or after_capture.role != "after_action"
        or before_capture.parent_capture_id != intent.source_capture_id
        or after_capture.parent_capture_id != before_capture.capture_id
        or before_capture.command_sequence != action.command_sequence
        or after_capture.command_sequence != action.command_sequence
    ):
        return None
    before, after = _snapshot(before_capture), _snapshot(after_capture)
    if before is None or after is None or not _same_document(before, after, action):
        return None
    subject = select_probe_target(
        before, goal=goal, page_type=parameters.get("source_page_type"), expected_key=subject_key
    )
    if subject is None:
        return None
    current = [item for item in after["elements"] if item.get("selector") == subject["selector"]]
    if len(current) != 1:
        return None
    current = current[0]
    if any(
        subject.get(key) != current.get(key)
        for key in ("selector", "tag", "role", "text", "aria_label", "src", "href", "aria_controls")
    ):
        return None
    trace = _native_effect(after_capture, goal, subject_key, subject, current, after)
    if trace is None:
        return None
    if back_to_top_structure(subject):
        left, right = probe_scroll_context(before), probe_scroll_context(after)
        if (
            left
            and right
            and left["selector"] == right["selector"]
            and left["scroll_top"] > left["client_height"]
            and right["scroll_top"] <= 2
        ):
            return "pass", "同一滚动容器在正式回顶部动作后实际回到顶部"
        return None
    if not disclosure_structure(subject) or not disclosure_structure(current):
        return None
    left, right = subject["h5_probe_controlled_region"], current["h5_probe_controlled_region"]
    if (
        left["id"] != right["id"]
        or subject.get("aria_controls") != current.get("aria_controls")
        or left["id"] != subject.get("aria_controls")
        or current.get("aria_expanded") == subject.get("aria_expanded")
        or not all(
            _number(region.get(key)) and region[key] >= 0
            for region in (left, right)
            for key in ("width", "height")
        )
        or not all(type(region.get("visible")) is bool for region in (left, right))
        or any(
            region["visible"] and min(region["width"], region["height"]) <= 0
            for region in (left, right)
        )
        or not any(left[key] != right[key] for key in ("visible", "height", "width"))
    ):
        return None
    # 两次几何相同不证明动画已结束；相反呈现仍缺完整窗口，不用采样等待定义业务超时线。
    expected = current["aria_expanded"] == "true"
    if right["visible"] is not expected:
        return None
    return "pass", "正式展开/收起动作与同一原生控制区的实际可见性一致"


def _snapshot(capture):
    originals = [item for item in capture.originals if item.artifact_type == "ui_snapshot"]
    if len(originals) != 1:
        return None
    value = originals[0].object_payload()
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "ui-snapshot.v2"
        or value.get("sequence") is True
        or value.get("element_limit_reached") is not False
        or any(
            value.get(key) for key in ("collection_error", "capture_degraded", "budget_truncated")
        )
        or not isinstance(value.get("elements"), list)
        or not all(isinstance(item, Mapping) for item in value["elements"])
        or type(value.get("element_count")) is not int
        or value.get("element_count") != len(value["elements"])
    ):
        return None
    return value


def _same_document(before, after, action):
    left, right = before.get("document"), after.get("document")
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    origin, start, end = (
        left.get("time_origin_ms"),
        before.get("performance_now_ms"),
        after.get("performance_now_ms"),
    )
    if not all(_number(value) for value in (origin, start, end)):
        return False
    if not (origin > 0 and 0 <= start < end and origin == right.get("time_origin_ms")):
        return False
    first, last = (
        datetime.fromisoformat(before["captured_at"]),
        datetime.fromisoformat(after["captured_at"]),
    )
    return bool(
        before.get("url")
        and before["url"] == after.get("url")
        and first.utcoffset() is not None
        and last.utcoffset() is not None
        and first <= action.started_at <= action.finished_at <= last
    )


def _native_effect(capture, goal, subject_key, subject, current, snapshot):
    originals = [item for item in capture.originals if item.artifact_type == "action_trace"]
    if len(originals) != 1:
        return None
    entries = json.loads(originals[0].payload)
    if not isinstance(entries, list) or not all(isinstance(item, Mapping) for item in entries):
        return None
    effects = []
    for entry in entries:
        state = entry.get("state_diff")
        effect = state.get("h5_probe_effect") if isinstance(state, Mapping) else None
        if isinstance(effect, Mapping):
            effects.append((entry, effect))
    if len(effects) != 1:
        return None
    entry, effect = effects[0]
    if (
        entry.get("action") != "click"
        or entry.get("selector") != subject["selector"]
        or entry.get("comparison_only") is True
        or entry.get("diagnostic_capture") is True
        or entry.get("result") != "probe_effect_observed"
        or effect.get("protocol_version") != "h5-probe-display-effect.v1"
        or effect.get("goal") != goal
        or effect.get("subject_key") != subject_key
        or effect.get("verified") is not True
        or effect.get("before_selector") != subject["selector"]
        or effect.get("before_rect") != subject.get("rect")
        or effect.get("after_selector") != current.get("selector")
        or effect.get("after_rect") != current.get("rect")
        or effect.get("before_resource") != subject.get("src")
        or effect.get("after_resource") != current.get("src")
        or effect.get("url") != snapshot.get("url")
        or effect.get("document_time_origin_ms") != snapshot["document"]["time_origin_ms"]
        or type(effect.get("readonly_denied_count")) is not int
        or effect["readonly_denied_count"] != 0
    ):
        return None
    return effect


def _number(value):
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
