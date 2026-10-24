"""从同帧视频节点证明连续视频信息流，不借用推荐链接数量。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

_ACTIVE_SLIDE = re.compile(r"(?:^|[-_])(?:slide|item)[-_](?:active|current)$", re.I)
_NEXT_SLIDE = re.compile(r"(?:^|[-_])(?:slide|item)[-_]next$", re.I)
_FEED_TRACK_MARKERS = (
    "swiper-wrapper", "slides-wrapper", "slide-track", "video-feed",
    "feed-track", "feed-list", "carousel-track",
)


def continuous_video_feed_count(snapshot: Mapping[str, object]) -> int:
    """同一纵向轨道中存在相邻、同尺寸且独立的视频时返回二。"""

    viewport = snapshot.get("viewport")
    elements = snapshot.get("elements")
    if not isinstance(viewport, Mapping) or not isinstance(elements, list):
        return 0
    width = _positive_number(viewport.get("width"))
    height = _positive_number(viewport.get("height"))
    if width is None or height is None:
        return 0
    videos = [
        item for item in elements
        if isinstance(item, Mapping)
        and item.get("tag") == "video"
        and item.get("visible") is True
        and item.get("occluded") is not True
        and item.get("obstructed") is not True
    ]
    for active in videos:
        active_rect = _rect(active)
        if (
            active_rect is None
            or not _has_slide_state(active, _ACTIVE_SLIDE)
            or not _primary_video_geometry(active_rect, width, height)
        ):
            continue
        tracks = _feed_track_selectors(active)
        if not tracks:
            continue
        for following in videos:
            if following is active or not _has_slide_state(following, _NEXT_SLIDE):
                continue
            following_rect = _rect(following)
            if (
                following_rect is not None
                and tracks & _feed_track_selectors(following)
                and _adjacent_vertical_video(active_rect, following_rect)
            ):
                return 2
    return 0


def _positive_number(value: object) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _rect(node: Mapping[str, object]) -> tuple[float, float, float, float] | None:
    rect = node.get("rect")
    if not isinstance(rect, Mapping):
        return None
    values = [rect.get(key) for key in ("x", "y", "width", "height")]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        return None
    x, y, width, height = (float(value) for value in values)
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _has_slide_state(node: Mapping[str, object], state: re.Pattern[str]) -> bool:
    tokens = node.get("ancestor_class_tokens")
    return isinstance(tokens, list) and any(
        isinstance(token, str) and state.search(token) for token in tokens
    )


def _feed_track_selectors(node: Mapping[str, object]) -> set[str]:
    ancestors = node.get("web_ancestor_structure")
    if not isinstance(ancestors, list):
        return set()
    tracks: set[str] = set()
    for ancestor in ancestors:
        if not isinstance(ancestor, Mapping):
            continue
        selector = ancestor.get("selector")
        if not isinstance(selector, str) or not selector:
            continue
        segment = selector.rsplit(" > ", 1)[-1].casefold()
        if any(marker in segment for marker in _FEED_TRACK_MARKERS):
            tracks.add(selector)
    return tracks


def _primary_video_geometry(
    rect: tuple[float, float, float, float], viewport_width: float, viewport_height: float
) -> bool:
    x, y, width, height = rect
    visible_width = max(0.0, min(x + width, viewport_width) - max(x, 0.0))
    visible_height = max(0.0, min(y + height, viewport_height) - max(y, 0.0))
    return (
        width >= viewport_width * 0.5
        and height >= viewport_height * 0.5
        and visible_width >= width * 0.5
        and visible_height >= height * 0.5
    )


def _adjacent_vertical_video(
    active: tuple[float, float, float, float], following: tuple[float, float, float, float]
) -> bool:
    x, y, width, height = active
    next_x, next_y, next_width, next_height = following
    return (
        abs(next_x - x) <= width * 0.08
        and 0.85 <= next_width / width <= 1.15
        and 0.85 <= next_height / height <= 1.15
        and 0.8 * height <= next_y - y <= 1.2 * height
    )
