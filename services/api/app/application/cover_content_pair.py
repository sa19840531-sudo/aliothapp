"""封面必须和同一条内容的正文或播放画面配对，广告图不能混进这套材料。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COVER_CONTENT_AD_GAP = "cover_content_ad_material_mixed"
COVER_CONTENT_LIST_GAP = "cover_content_list_observation_only"
COVER_CONTENT_UNPAIRED_GAP = "cover_content_unpaired"

_LIST_PAGE_TYPES = frozenset({"home", "content_list", "video_list", "post_list"})
_OPEN_PAGE_TYPES = frozenset({"content_detail", "video_detail"})
_OPEN_KINDS = frozenset({"body", "playback"})
_AD_PAGE_TYPES = frozenset({"startup_ad", "ad", "ad_landing"})
_AD_TOKENS = ("广告", "推广", "赞助", "advert")


def assess_cover_content_pair(
    *,
    cover: Mapping[str, Any] | None,
    content: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """三张不成套时只记缺口。列表补采、广告图和另一条内容都不能充当正文侧。"""

    cover_item = dict(cover) if isinstance(cover, Mapping) else {}
    content_item = dict(content) if isinstance(content, Mapping) else {}
    if _is_ad(cover_item) or _is_ad(content_item):
        return _incomplete(COVER_CONTENT_AD_GAP, cover_item, content_item)
    if _is_list_observation(content_item) or (
        not content_item and _is_list_observation(cover_item)
    ):
        return _incomplete(COVER_CONTENT_LIST_GAP, cover_item, content_item)
    cover_id = _text(cover_item.get("capture_id"))
    content_id = _text(content_item.get("capture_id"))
    cover_key = _content_key(cover_item)
    content_key = _content_key(content_item)
    opened = _page(content_item) in _OPEN_PAGE_TYPES or _kind(content_item) in _OPEN_KINDS
    same_object = bool(cover_key and content_key and cover_key == content_key)
    distinct = bool(cover_id and content_id and cover_id != content_id)
    if cover_item and content_item and opened and same_object and distinct:
        return {
            "complete": True,
            "gap_codes": [],
            "cover_capture_id": cover_id,
            "content_capture_id": content_id,
            "content_key": cover_key,
        }
    return _incomplete(COVER_CONTENT_UNPAIRED_GAP, cover_item, content_item)


def _incomplete(
    gap: str,
    cover: Mapping[str, Any],
    content: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "complete": False,
        "gap_codes": [gap],
        "cover_capture_id": _text(cover.get("capture_id")) or None,
        "content_capture_id": _text(content.get("capture_id")) or None,
        "content_key": None,
    }


def _is_ad(item: Mapping[str, Any]) -> bool:
    if not item:
        return False
    if item.get("is_ad") is True or _kind(item) == "ad":
        return True
    if _page(item) in _AD_PAGE_TYPES:
        return True
    blob = " ".join(
        _text(item.get(key))
        for key in ("semantic_target", "title", "material_kind", "role", "content_key")
    ).casefold()
    return any(token in blob for token in _AD_TOKENS)


def _is_list_observation(item: Mapping[str, Any]) -> bool:
    if not item:
        return False
    if _kind(item) in _OPEN_KINDS:
        return False
    if _kind(item) == "list":
        return True
    return _page(item) in _LIST_PAGE_TYPES and _text(item.get("action")).casefold() in {
        "observe",
        "observation",
    }


def _content_key(item: Mapping[str, Any]) -> str:
    for key in ("content_key", "content_id", "title", "href"):
        text = _text(item.get(key))
        if text:
            return text.casefold()
    return ""


def _page(item: Mapping[str, Any]) -> str:
    return _text(item.get("page_type")).casefold()


def _kind(item: Mapping[str, Any]) -> str:
    return _text(item.get("material_kind")).casefold()


def _text(value: object) -> str:
    return str(value or "").strip()
