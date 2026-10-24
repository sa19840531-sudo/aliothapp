"""广告加载失败兜底：设备必须真的注入广告加载故障，并留下生效与关闭或跳过结果。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

AD_LOAD_FAULT_UNSUPPORTED = "ad_load_fault_injection_unsupported"
AD_LOAD_FAULT_EFFECT_MISSING = "ad_load_fault_effect_missing"
AD_LOAD_CLOSE_OR_SKIP_MISSING = "ad_load_fallback_close_or_skip_missing"
_CLOSE_OR_SKIP = frozenset({"closed", "skipped"})


def assess_ad_load_fallback(action: Mapping[str, Any] | None) -> dict[str, Any]:
    """没有设备侧广告加载故障时只记缺口，不用整段断网或空白蒙层冒充兜底结论。"""

    item = dict(action) if isinstance(action, Mapping) else {}
    scope = str(item.get("fault_scope") or "").strip().casefold()
    supported = item.get("ad_load_fault_supported") is True and scope == "ad_load"
    matched = item.get("matched_request_count")
    effect = bool(
        supported
        and item.get("action") == "network_fault"
        and item.get("result") == "network_fault_injected"
        and isinstance(matched, int)
        and not isinstance(matched, bool)
        and matched >= 1
        and item.get("fault_effective") is True
    )
    outcome = str(item.get("fallback_outcome") or "").strip().casefold()
    gaps: list[str] = []
    if not supported:
        gaps.append(AD_LOAD_FAULT_UNSUPPORTED)
    if not effect:
        gaps.append(AD_LOAD_FAULT_EFFECT_MISSING)
    if outcome not in _CLOSE_OR_SKIP:
        gaps.append(AD_LOAD_CLOSE_OR_SKIP_MISSING)
    return {
        "complete": not gaps,
        "gap_codes": gaps,
        "ad_load_fault_supported": supported,
        "fault_effective": effect,
        "fallback_outcome": outcome or None,
    }
