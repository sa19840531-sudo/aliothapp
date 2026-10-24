"""从同帧、同动作的可信材料选择补证阶段，不使用模型摘要作为进度。"""

import math
import re
from collections.abc import Mapping


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def current_probe_snapshot(payload: Mapping) -> Mapping:
    facts = _mapping(payload.get("facts"))
    snapshot = facts.get("ui_snapshot") or {}
    if not isinstance(snapshot, Mapping):
        return {}
    if snapshot.get("sequence") is not True:
        return snapshot
    frames = snapshot.get("frames")
    if not isinstance(frames, list) or not frames or len(frames) > 8:
        return {}
    if not all(isinstance(frame, Mapping) for frame in frames):
        return {}
    if snapshot.get("source") == "h5_bound_action_capture_roles":
        capture = (payload.get("page") or {}).get("page_capture_id")
        if not isinstance(capture, str) or not capture or frames[-1].get("capture_id") != capture:
            return {}
    elif snapshot.get("capture_role") not in {
        "h5_vertical_scroll_collection",
        "h5_payment_horizontal_collection",
    }:
        return {}
    return frames[-1]


def probe_scroll_context(snapshot: Mapping) -> dict | None:
    """主体滚动位置与当前来源一起绑定；不使用屏外/模态滚动区。"""
    regions = snapshot.get("h5_scroll_regions") or []
    if not isinstance(regions, list) or any(not isinstance(region, Mapping) for region in regions):
        return None
    if len(regions) > 1:
        return None
    if regions:
        region = regions[0]
        values = [region.get(key) for key in ("scroll_top", "scroll_height", "client_height")]
        selector = region.get("selector")
    else:
        viewport, document = _mapping(snapshot.get("viewport")), _mapping(snapshot.get("document"))
        values = [viewport.get("scroll_y"), document.get("scroll_height"), viewport.get("height")]
        selector = "document"
    if not selector or not all(
        isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        for value in values
    ):
        return None
    top, height, view = values
    if top < 0 or view <= 0 or height <= view or top > height - view + 2:
        return None
    return {"selector": selector, "scroll_top": top, "scroll_height": height, "client_height": view}


def scroll_feedback_ready(payload: Mapping, *, require_bottom: bool) -> bool:
    facts = _mapping(payload.get("facts"))
    sequence = _mapping(facts.get("ui_snapshot"))
    frames = sequence.get("frames") or []
    if (
        sequence.get("sequence") is not True
        or sequence.get("capture_role") != "h5_vertical_scroll_collection"
        or not 3 <= len(frames) <= 8
    ):
        return False
    contexts = [probe_scroll_context(frame) for frame in frames]
    if any(context is None for context in contexts):
        return False
    final = contexts[-1]
    if any(
        frame.get("index") != index
        or frame.get("collection_error")
        or frame.get("capture_degraded")
        or frame.get("url") != frames[-1].get("url")
        or contexts[index]["selector"] != final["selector"]
        or (index and contexts[index]["scroll_top"] <= contexts[index - 1]["scroll_top"])
        for index, frame in enumerate(frames)
    ):
        return False
    if require_bottom and not (
        final["scroll_top"] > final["client_height"]
        and final["scroll_top"] + final["client_height"] >= final["scroll_height"] - 2
    ):
        return False
    return any(
        action.get("action") == "scroll"
        and (effect := _mapping(_mapping(action.get("state_diff")).get("scroll_effect"))).get(
            "protocol_version"
        )
        == "scroll-effect.v1"
        and effect.get("observed") is True
        and effect.get("container_selector") == final["selector"]
        and effect.get("scroll_top_after") == final["scroll_top"]
        for action in facts.get("action_log") or []
        if isinstance(action, Mapping)
    )


def verified_display_subjects(payload: Mapping, *, goal: str, snapshot: Mapping) -> set[str]:
    """只复用同文档、同地址真实动作后验，不以选中类名或轮播位置补算历史操作。"""
    facts = _mapping(payload.get("facts"))
    origin = _mapping(snapshot.get("document")).get("time_origin_ms")
    if not isinstance(origin, int | float) or isinstance(origin, bool) or not math.isfinite(origin):
        return set()
    actions = list(facts.get("action_log") or [])
    for step in (facts.get("journey") or [])[:20]:
        if isinstance(step, Mapping):
            actions.extend((step.get("action_log") or [])[:10])
    return {
        effect["subject_key"]
        for action in actions
        if isinstance(action, Mapping)
        and action.get("result") == "probe_effect_observed"
        and isinstance(effect := _mapping(action.get("state_diff")).get("h5_probe_effect"), Mapping)
        and effect.get("protocol_version") == "h5-probe-display-effect.v1"
        and effect.get("goal") == goal
        and effect.get("verified") is True
        and (
            goal != "payment_plan_selection"
            or (
                effect.get("readonly_isolation") is True
                and type(effect.get("readonly_denied_count")) is int
                and effect["readonly_denied_count"] == 0
            )
        )
        and effect.get("url") == snapshot.get("url")
        and effect.get("document_time_origin_ms") == origin
        and isinstance(effect.get("subject_key"), str)
        and re.fullmatch(r"[0-9a-f]{64}", effect["subject_key"])
    }
