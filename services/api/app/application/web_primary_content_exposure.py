"""Web 正文补证的只读曝光，不授予点击或播放器交互权限。"""

from __future__ import annotations

import math
from collections.abc import Mapping

from services.api.app.application.web_article_body_structure import is_web_article_body_node
from services.api.app.application.web_interaction_facts import web_target_exposed


def web_primary_content_exposed(
    snapshot: Mapping[str, object], *, selector: str, page_type: str
) -> bool:
    """仅判断已分类正文容器的几何曝光，不替代模型正文分类。"""
    if page_type != "content_detail":
        return web_target_exposed(snapshot, selector=selector)
    elements, viewport = snapshot.get("elements"), snapshot.get("viewport")
    if not isinstance(elements, list) or not isinstance(viewport, Mapping):
        return False
    matching = [
        item for item in elements
        if isinstance(item, Mapping) and item.get("selector") == selector
    ]
    if len(matching) != 1 or matching[0].get("visible") is not True:
        return False
    element = matching[0]
    if element.get("tag") != "main" and not is_web_article_body_node(element):
        return web_target_exposed(snapshot, selector=selector)
    rect = element.get("rect")
    if not isinstance(rect, Mapping):
        return False
    visual_keys = (
        "visual_viewport_width", "visual_viewport_height",
        "visual_viewport_offset_left", "visual_viewport_offset_top",
    )
    visual = any(viewport.get(key) is not None for key in visual_keys)
    values = [rect.get(key) for key in ("x", "y", "width", "height")]
    values += (
        [viewport.get(key) for key in visual_keys]
        if visual else [viewport.get("width"), viewport.get("height"), 0, 0]
    )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in values
    ):
        return False
    x, y, width, height, view_width, view_height, left, top = values
    if width <= 0 or height <= 0 or view_width <= 0 or view_height <= 0:
        return False
    if height <= view_height:
        return (
            left <= x + width / 2 <= left + view_width
            and top <= y + height / 2 <= top + view_height
        )
    # 超高正文宽度至少为视口十分之一，避免塌缩成细线的容器被升级为正文曝光。
    if width < view_width * 0.1:
        return False
    visible_width = max(0, min(x + width, left + view_width) - max(x, left))
    visible_height = max(0, min(y + height, top + view_height) - max(y, top))
    # 正文至少占半个可见视口高度，且可见宽度不少于可容纳正文宽度的一半。
    # 不能用整篇正文面积作分母，也不能让一像素露出被当成有效主体证据。
    return visible_height >= view_height * 0.5 and visible_width >= min(width, view_width) * 0.5
