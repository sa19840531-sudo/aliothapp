"""Observation Packet 可信事实的有界读取与归一化工具。"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlparse


def facts(packet_payload: dict[str, Any]) -> dict[str, Any]:
    return mapping(packet_payload.get("facts"))


def visible_text(packet_payload: dict[str, Any]) -> str:
    return str(facts(packet_payload).get("visible_text") or "")


def ui_snapshot(packet_payload: dict[str, Any]) -> dict[str, Any]:
    return mapping(facts(packet_payload).get("ui_snapshot"))


def latest_ui_snapshot(packet_payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = ui_snapshot(packet_payload)
    frames = snapshot.get("frames")
    if isinstance(frames, list):
        for item in reversed(frames):
            if isinstance(item, dict):
                return dict(item)
    return snapshot


def snapshot_sequence(packet_payload: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = ui_snapshot(packet_payload)
    frames = snapshot.get("frames")
    if isinstance(frames, list):
        return [dict(item) for item in frames if isinstance(item, dict)]
    return [snapshot] if snapshot else []


def elements(packet_payload: dict[str, Any]) -> list[dict[str, Any]]:
    values = latest_ui_snapshot(packet_payload).get("elements")
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)]


def snapshot_elements(packet_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """读取全部时序帧元素，并保留元素首次出现的帧序号。"""
    values: list[dict[str, Any]] = []
    for position, snapshot in enumerate(snapshot_sequence(packet_payload)):
        frame_index = snapshot.get("index", position)
        elements_in_frame = snapshot.get("elements")
        if not isinstance(elements_in_frame, list):
            continue
        for item in elements_in_frame:
            if not isinstance(item, dict):
                continue
            element = dict(item)
            element["frame_index"] = frame_index
            values.append(element)
    return values


def element_ref(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "selector",
            "tag",
            "role",
            "text",
            "aria_label",
            "class_tokens",
            "rect",
            "href",
            "external",
            "target",
            "rel",
            "src",
            "alt",
            "frame_index",
        )
        if item.get(key) not in (None, "", [])
    }


def last_action(packet_payload: dict[str, Any]) -> dict[str, Any]:
    actions = facts(packet_payload).get("action_log")
    if not isinstance(actions, list):
        return {}
    for item in reversed(actions):
        if isinstance(item, dict):
            return dict(item)
    return {}


def compact_action(action: dict[str, Any]) -> dict[str, Any]:
    """保留规则判定所需的动作身份、前后状态和故障结果字段。

    ``action_log`` 可能同时服务多个规则 Tool。这里仅投影已知的有界字段，
    但不能丢掉动作与 Capture 的配对身份；否则下游只能看到一个孤立的
    ``click`` 或 ``network_fault``，无法证明它对应哪一轮前后采集。
    """
    return {
        key: action.get(key)
        for key in (
            "action",
            "selector",
            "result",
            "status",
            "execution_outcome",
            "verification_outcome",
            "intent_id",
            "action_execution_id",
            "source_capture_id",
            "result_capture_id",
            "purpose",
            "semantic_target",
            "transition_kind",
            "navigation",
            "error_code",
            "before_url",
            "after_url",
            "new_page_urls",
            "target_before",
            "target_after",
            "state_diff",
            "target_fraction",
            "target_time_seconds",
            "media_before",
            "media_after",
            "fault_url_pattern_length",
            "fault_url_pattern_sha256",
            "matched_request_count",
            "matched_request_urls",
            "matched_requests_truncated",
            "route_restored",
            "reload_error",
            "transient_message_observation",
            "transient_messages",
            "ad_navigation_evidence",
        )
        if action.get(key) not in (None, "", [], {})
    }


def modal_elements(packet_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return modal_elements_from_snapshot(latest_ui_snapshot(packet_payload))


def modal_elements_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    values = snapshot.get("elements")
    values = values if isinstance(values, list) else []
    dialogs: list[dict[str, Any]] = []
    popups: list[dict[str, Any]] = []
    overlays: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        identity = " ".join(
            (
                str(item.get("role") or ""),
                " ".join(string_list(item.get("class_tokens"))),
            )
        )
        reference = element_ref(item)
        if str(item.get("role") or "").casefold() in {"dialog", "alertdialog"}:
            dialogs.append(reference)
        elif re.search(r"dialog|modal|popup", identity, re.IGNORECASE):
            popups.append(reference)
        elif re.search(r"overlay|mask|遮罩", identity, re.IGNORECASE):
            overlays.append(reference)
    if dialogs:
        return dialogs
    if popups:
        return popups
    return overlays[:1]


def first_timer_value(snapshot: dict[str, Any]) -> float | None:
    values = snapshot.get("elements")
    values = values if isinstance(values, list) else []
    for item in values:
        if not isinstance(item, dict):
            continue
        identity = " ".join(
            (
                str(item.get("text") or ""),
                " ".join(string_list(item.get("class_tokens"))),
            )
        )
        if not re.search(r"countdown|timer|倒计时|跳过", identity, re.IGNORECASE):
            continue
        if matched := re.search(r"\d+(?:\.\d+)?", str(item.get("text") or "")):
            return float(matched.group(0))
    return None


def extract_price_pairs(
    packet_payload: dict[str, Any],
    _config: dict[str, Any],
) -> list[dict[str, float]]:
    pair_pattern = re.compile(
        r"(?:优惠价|现价|折后价|活动价)\s*[¥￥$]?\s*(\d+(?:\.\d+)?)"
        r"[\s\S]{0,80}?(?:原价|划线价)\s*[¥￥$]?\s*(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    reverse_pattern = re.compile(
        r"(?:原价|划线价)\s*[¥￥$]?\s*(\d+(?:\.\d+)?)"
        r"[\s\S]{0,80}?(?:优惠价|现价|折后价|活动价)\s*[¥￥$]?\s*(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    text = visible_text(packet_payload)
    pairs = [
        {"discount_price": float(discount), "original_price": float(original)}
        for discount, original in pair_pattern.findall(text)
    ]
    pairs.extend(
        {"discount_price": float(discount), "original_price": float(original)}
        for original, discount in reverse_pattern.findall(text)
    )
    return pairs[:50]


def candidate_urls(packet_payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in elements(packet_payload):
        for key in ("href", "src"):
            value = str(item.get(key) or "").strip()
            if value and value not in urls:
                urls.append(value)
    for value in re.findall(
        r"https?://[^\s\"'<>]+",
        visible_text(packet_payload),
    ):
        if value not in urls:
            urls.append(value)
    return urls


def candidate_qr_urls(packet_payload: dict[str, Any]) -> list[str]:
    """只提取带二维码语义的目标 URL，避免普通链接被误判为二维码。"""
    urls: list[str] = []
    for item in elements(packet_payload):
        identity = " ".join(
            (
                str(item.get("selector") or ""),
                str(item.get("text") or ""),
                str(item.get("aria_label") or ""),
                str(item.get("alt") or ""),
                " ".join(string_list(item.get("class_tokens"))),
            )
        )
        if not re.search(r"qr(?:code)?|二维码|扫码", identity, re.IGNORECASE):
            continue
        for key in ("href", "src"):
            value = str(item.get(key) or "").strip()
            if value and value not in urls:
                urls.append(value)
        for value in re.findall(r"https?://[^\s\"'<>]+", identity):
            if value not in urls:
                urls.append(value)
    text = visible_text(packet_payload)
    for matched in re.finditer(r"qr(?:code)?|二维码|扫码", text, re.IGNORECASE):
        window = text[max(0, matched.start() - 200) : matched.end() + 300]
        for value in re.findall(r"https?://[^\s\"'<>]+", window):
            if value not in urls:
                urls.append(value)
    return urls


def extract_rank_values(packet_payload: dict[str, Any]) -> list[int]:
    values: list[int] = []
    patterns = (
        re.compile(r"(?:第\s*)?(\d{1,4})\s*(?:名|位|\.|、)"),
        re.compile(r"(?:rank|ranking)\s*[:#]?\s*(\d{1,4})", re.IGNORECASE),
    )
    for item in elements(packet_payload):
        identity = " ".join(
            (
                str(item.get("text") or ""),
                str(item.get("aria_label") or ""),
                " ".join(string_list(item.get("class_tokens"))),
            )
        )
        if not re.search(r"rank|ranking|榜|排名|名次", identity, re.IGNORECASE):
            continue
        for pattern in patterns:
            if matched := pattern.search(identity):
                values.append(int(matched.group(1)))
                break
    if values:
        return values[:100]
    text = visible_text(packet_payload)
    for pattern in patterns:
        values.extend(int(value) for value in pattern.findall(text))
        if values:
            break
    return values[:100]


def is_out_of_viewport(item: dict[str, Any], viewport: dict[str, Any]) -> bool:
    rect = mapping(item.get("rect"))
    values = (
        number(viewport.get("width")),
        number(viewport.get("height")),
        number(rect.get("x")),
        number(rect.get("y")),
        number(rect.get("width")),
        number(rect.get("height")),
    )
    if any(value is None for value in values):
        return False
    width, height, x, y, rect_width, rect_height = values
    assert all(value is not None for value in values)
    return bool(
        x < 0
        or y < 0
        or x + rect_width > width
        or y + rect_height > height
    )


def looks_like_ad_element(item: dict[str, Any]) -> bool:
    identity = " ".join(
        (
            str(item.get("role") or ""),
            str(item.get("selector") or ""),
            str(item.get("name") or ""),
            str(item.get("text") or ""),
            str(item.get("aria_label") or ""),
            str(item.get("alt") or ""),
            str(item.get("href") or ""),
            str(item.get("src") or ""),
            " ".join(string_list(item.get("class_tokens"))),
        )
    )
    return bool(
        re.search(
            r"\badvert(?:isement)?\b|\bad(?:s|[-_ ])|banner|startup|splash|"
            r"promo|sponsor|广告|推广|赞助",
            identity,
            re.IGNORECASE,
        )
    )


def looks_like_promotional_text(value: str) -> bool:
    """识别常见广告、推广和博彩导流文案，仅用于决定是否继续语义判定。"""
    return bool(
        re.search(
            r"广告|推广|赞助|推荐下载|立即(?:下载|安装|开通)|马上下载|点击下载|"
            r"送彩金|返水|投注|博彩|棋牌|娱乐城|casino|\bbet\d*\b",
            value,
            re.IGNORECASE,
        )
    )


def is_media_element(item: dict[str, Any]) -> bool:
    return bool(
        str(item.get("tag") or "").casefold() in {"img", "video", "canvas"}
        or str(item.get("role") or "").casefold() in {"image", "video"}
        or item.get("image")
        or item.get("media")
    )


def element_area(item: dict[str, Any]) -> float:
    rect = mapping(item.get("rect"))
    width = number(rect.get("width")) or 0.0
    height = number(rect.get("height")) or 0.0
    return max(width, 0.0) * max(height, 0.0)


def looks_like_ad_control(item: dict[str, Any]) -> bool:
    identity = " ".join(
        (
            str(item.get("selector") or ""),
            str(item.get("text") or ""),
            str(item.get("aria_label") or ""),
            " ".join(string_list(item.get("class_tokens"))),
        )
    )
    return bool(
        re.search(
            r"close|skip|dismiss|enter|关闭|跳过|进入|×",
            identity,
            re.IGNORECASE,
        )
    )


def looks_like_ugc_element(item: dict[str, Any]) -> bool:
    identity = " ".join(
        (
            str(item.get("role") or ""),
            str(item.get("selector") or ""),
            " ".join(string_list(item.get("class_tokens"))),
        )
    )
    return bool(
        re.search(
            r"ugc|comment|reply|post|user[-_ ]?content|评论|回复|帖子",
            identity,
            re.IGNORECASE,
        )
    )


def url_syntax_fact(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname or ""
    issues: list[str] = []
    if scheme not in {"http", "https"}:
        issues.append("unsupported_scheme")
    if not hostname:
        issues.append("missing_hostname")
    if parsed.username or parsed.password:
        issues.append("embedded_credentials")
    if hostname.casefold() in {"localhost", "127.0.0.1", "::1"}:
        issues.append("loopback_target")
    return {
        "url": url,
        "scheme": scheme,
        "hostname": hostname,
        "syntax_valid": not issues,
        "issues": issues,
    }


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def integer(value: Any) -> int:
    value_as_number = number(value)
    return int(value_as_number) if value_as_number is not None else 0
