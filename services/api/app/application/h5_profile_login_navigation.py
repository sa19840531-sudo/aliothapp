"""H5 Flutter 个人中心登录入口的跨层结构合同。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

H5_FLUTTER_PROFILE_LOGIN_TARGET = "个人中心 Flutter 登录入口"
H5_FLUTTER_PROFILE_LOGIN_TARGET_MODE = "flutter_profile_login_entry"
H5_FLUTTER_PROFILE_LOGIN_CAPABILITY = "profile_guest_login"
H5_FLUTTER_PROFILE_LOGIN_RUNTIME_SELECTOR = "internal:flutter_profile_login_entry"

_PROFILE_ID_PATTERN = re.compile(r"\bid\s*:\s*[a-z0-9]+\b", re.IGNORECASE)


def h5_unique_flutter_profile_login_entry(
    elements: Sequence[object],
) -> Mapping[str, Any] | None:
    """返回唯一的 Flutter 个人中心游客登录语义组。"""
    matches = [
        item
        for item in elements
        if isinstance(item, Mapping) and h5_is_flutter_profile_login_entry(item)
    ]
    return matches[0] if len(matches) == 1 else None


def h5_is_flutter_profile_login_entry(element: Mapping[str, Any]) -> bool:
    """以角色、完整标签、父层和顶部几何证明游客登录组。"""
    if (
        str(element.get("tag") or "").casefold() != "flt-semantics"
        or str(element.get("role") or "").casefold() != "group"
        or element.get("interactive") is not True
        or element.get("parent_semantics_tappable") is not False
        or not str(element.get("parent_semantics_selector") or "").strip()
    ):
        return False
    label = _normalized_text(element.get("aria_label") or element.get("text"))
    if label.count("立即登录") != 1 or _PROFILE_ID_PATTERN.search(label) is None:
        return False
    rect = _rect(element)
    if rect is None:
        return False
    x, y, width, height = rect
    return bool(
        0 <= x <= 32
        and 0 <= y <= 120
        and 320 <= width <= 390
        and 45 <= height <= 90
    )


def _normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _rect(element: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    raw = element.get("rect")
    if not isinstance(raw, Mapping):
        return None
    values = tuple(raw.get(key) for key in ("x", "y", "width", "height"))
    if any(
        isinstance(value, bool) or not isinstance(value, int | float)
        for value in values
    ):
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


__all__ = [
    "H5_FLUTTER_PROFILE_LOGIN_CAPABILITY",
    "H5_FLUTTER_PROFILE_LOGIN_RUNTIME_SELECTOR",
    "H5_FLUTTER_PROFILE_LOGIN_TARGET",
    "H5_FLUTTER_PROFILE_LOGIN_TARGET_MODE",
    "h5_is_flutter_profile_login_entry",
    "h5_unique_flutter_profile_login_entry",
]
