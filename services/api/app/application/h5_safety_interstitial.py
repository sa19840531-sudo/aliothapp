"""H5 安全中间页的确定性识别与平台动作合同。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

H5_SAFETY_INTERSTITIAL_TARGET = "通过安全中间页"
H5_SAFETY_INTERSTITIAL_TARGET_MODE = "safety_interstitial_bypass"
H5_SAFETY_INTERSTITIAL_CAPABILITY = "xcdn_checkbox"
H5_SAFETY_INTERSTITIAL_SELECTOR = 'input[type="checkbox"]:visible'

H5_SAFETY_INTERSTITIAL_ERROR_CODE = "PAGE_TYPE_ASSESSMENT_BLOCKED_BY_SAFETY"
H5_SAFETY_INTERSTITIAL_STATES = frozenset(
    {
        "automation_blocked",
        "bot_detection",
        "captcha_blocked",
        "safety_interstitial",
        "security_check",
    }
)
H5_SAFETY_INTERSTITIAL_FLAGS = frozenset(
    {
        "automation_blocked",
        "bot_detection",
        "captcha_blocked",
        "deterministic_safety_interstitial",
        "safety_interstitial",
    }
)
H5_SAFETY_INTERSTITIAL_TEXT_MARKERS = (
    "this site is safe",
    "click to visit now",
)


def h5_detection_is_safety_interstitial(detection_result: Mapping[str, Any]) -> bool:
    """按页面识别事实判断当前 Capture 是否停在已知安全中间页。"""
    page_state = _optional_text(detection_result.get("page_state")) or ""
    flags = _string_list(detection_result.get("risk_flags"))
    if _normalized(page_state) in H5_SAFETY_INTERSTITIAL_STATES:
        return True
    if {_normalized(flag) for flag in flags} & H5_SAFETY_INTERSTITIAL_FLAGS:
        return True
    return False


def h5_snapshot_is_safety_interstitial(snapshot: Mapping[str, Any]) -> bool:
    """按受信 UI Snapshot 验证安全页文案，避免在普通页面误点 checkbox。"""
    text_parts: list[str] = []
    for key in ("title", "url"):
        value = snapshot.get(key)
        if isinstance(value, str):
            text_parts.append(value)
    raw_elements = snapshot.get("elements")
    if isinstance(raw_elements, Sequence) and not isinstance(raw_elements, (str, bytes)):
        for raw in raw_elements:
            if isinstance(raw, Mapping):
                text = raw.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
    haystack = _normalized(" ".join(text_parts))
    return all(marker in haystack for marker in H5_SAFETY_INTERSTITIAL_TEXT_MARKERS)


def h5_snapshot_safety_checkbox_selectors(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """返回当前安全中间页可交互 checkbox selector 集合。"""
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, Sequence) else ()
    selectors: list[str] = []
    for raw in elements:
        if not isinstance(raw, Mapping):
            continue
        if _normalized(raw.get("tag")) != "input":
            continue
        if _normalized(raw.get("type")) != "checkbox":
            continue
        if raw.get("disabled") is True:
            continue
        if raw.get("interactive") is not True:
            continue
        hit_test = raw.get("hit_test")
        if isinstance(hit_test, Mapping) and hit_test.get("center_hits_target") is False:
            continue
        selector = _optional_text(raw.get("selector"))
        if selector is None:
            continue
        selectors.append(selector)
    return tuple(dict.fromkeys(selectors))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _normalized(value: Any) -> str:
    return str(value or "").strip().casefold()
