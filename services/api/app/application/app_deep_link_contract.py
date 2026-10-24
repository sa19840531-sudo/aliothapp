"""App Deep Link 合同：直达观察与真实 Tap／控件点击分离。

Deep Link／Intent 只可用于受控内容观察或恢复，不得证明原生导航控件点击通过。
商店、安装、支付、拨号、短信等目标硬阻断。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

APP_DEEP_LINK_CONTRACT_VERSION = "app-deep-link.v1"

# 动作观察 outcome：完成观察但不证明原生控件点击／业务点击到达。
DEEP_LINK_DIRECT_OBSERVATION_OUTCOME = "observation_completed"
DEEP_LINK_DIRECT_OBSERVATION_REASON = "deep_link_direct_observation_not_native_tap"
DEEP_LINK_HARD_BLOCKED_REASON = "deep_link_target_hard_blocked"

_HARD_BLOCKED_SCHEMES = frozenset(
    {
        "market",
        "itms",
        "itms-apps",
        "itmss",
        "tel",
        "sms",
        "smsto",
        "mms",
        "mmsto",
        "mailto",
    }
)

_HARD_BLOCKED_HOST_MARKERS = (
    "play.google.com",
    "appstore.com",
    "apps.apple.com",
    "itunes.apple.com",
)

_HARD_BLOCKED_PACKAGE_MARKERS = (
    "vending",
    "finsky",
    "appmarket",
    "appstore",
    "packageinstaller",
    "alipay",
    "wallet",
)


def deep_link_proves_native_control_click() -> bool:
    """合同恒为 False：Deep Link 成功不等于原生控件点击通过。"""

    return False


def classify_deep_link_target(uri: object) -> dict[str, Any]:
    """解析 Deep Link／Intent URI，标记是否硬阻断。"""

    text = str(uri or "").strip()
    parsed = urlsplit(text)
    scheme = (parsed.scheme or "").casefold()
    host = (parsed.hostname or "").casefold()
    path = unquote(parsed.path or "")
    query = unquote(parsed.query or "")
    haystack = f"{text} {host} {path} {query}".casefold()

    blocked = False
    block_kind: str | None = None
    if scheme in _HARD_BLOCKED_SCHEMES:
        blocked = True
        block_kind = f"scheme:{scheme}"
    elif any(marker in host for marker in _HARD_BLOCKED_HOST_MARKERS):
        blocked = True
        block_kind = "store_or_install_host"
    elif scheme == "intent" and any(marker in haystack for marker in _HARD_BLOCKED_PACKAGE_MARKERS):
        blocked = True
        block_kind = "intent_hard_blocked_package"
    elif any(
        marker in haystack
        for marker in ("tel:", "sms:", "smsto:", "market://", "packageinstaller")
    ):
        blocked = True
        block_kind = "embedded_hard_blocked_token"

    return {
        "protocol_version": APP_DEEP_LINK_CONTRACT_VERSION,
        "uri": text or None,
        "scheme": scheme or None,
        "host": host or None,
        "hard_blocked": blocked,
        "block_kind": block_kind,
        "proves_native_control_click": False,
        "arrival_kind": "deep_link_direct_observation",
    }


def deep_link_is_hard_blocked(uri: object) -> bool:
    return classify_deep_link_target(uri)["hard_blocked"] is True


def build_deep_link_observation_fields(
    *,
    uri: object,
    within_target_package: bool,
) -> dict[str, Any]:
    """供动作观察 metadata 挂载的 Deep Link 证据字段。"""

    classified = classify_deep_link_target(uri)
    return {
        **classified,
        "within_target_package": bool(within_target_package),
        "counts_as_native_tap_proof": False,
        "counts_as_navigation_click_pass": False,
    }
