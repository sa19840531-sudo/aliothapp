"""Resolve match.view_id / text against Accessibility hierarchy → device point."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.api.app.infrastructure.device.android_runtime_observation import (
    parse_android_ui_hierarchy,
)


def resolve_match_to_device_point(
    hierarchy_xml: str,
    *,
    match: Mapping[str, Any],
    device_width: int,
    device_height: int,
) -> tuple[int, int]:
    """按 view_id/text/content_desc 命中节点中心；供 Runtime 内部绑定。"""
    if not isinstance(match, Mapping) or not match:
        raise ValueError("match 必须为非空对象")
    view_id = str(match.get("view_id") or "").strip()
    text = str(match.get("text") or "").strip()
    content_desc = str(match.get("content_desc") or "").strip()
    if not view_id and not text and not content_desc:
        raise ValueError("match 至少需要 view_id/text/content_desc 之一")
    observation = parse_android_ui_hierarchy(
        hierarchy_xml,
        width=device_width,
        height=device_height,
    )
    candidates = []
    for node in observation.nodes:
        if not node.visible_to_user or not node.enabled:
            continue
        if view_id:
            short = node.resource_id.rsplit("/", 1)[-1]
            if view_id not in {node.resource_id, short}:
                continue
        if text and text not in node.text:
            continue
        if content_desc and content_desc not in node.content_desc:
            continue
        candidates.append(node)
    if not candidates:
        raise ValueError("match 未命中任何可见节点")
    # 优先可点击、面积更小的精确控件
    candidates.sort(
        key=lambda node: (
            0 if node.clickable else 1,
            node.bounds.width * node.bounds.height,
            node.node_id,
        )
    )
    best = candidates[0]
    x = (best.bounds.left + best.bounds.right) // 2
    y = (best.bounds.top + best.bounds.bottom) // 2
    return x, y
