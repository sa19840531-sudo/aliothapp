"""App Surface 补充证据：UI Snapshot、screen_id、log/网络/故障制品投影。

禁止把 UI Hierarchy 伪装成 DOM；制品必须有界、可审计。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.capture_profiles import (
    APP_CAPTURE_ARTIFACT_CONTRACT,
    APP_CAPTURE_ARTIFACT_TYPES,
)
from services.api.app.domain.exploration.age_confirm import (
    text_matches_age_confirm,
    text_matches_age_leave,
)
from services.api.app.domain.page.registry import get_default_page_type_registry

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_MAX_UI_SNAPSHOT_NODES = 80
_MAX_TRUSTED_SKIP_SCAN_NODES = 500
_MAX_TRUSTED_APP_CONTROL_SCAN_NODES = 500
_MAX_DEVICE_LOG_CHARS = 24_000
_MAX_NETWORK_ENTRIES = 40
_MAX_FAULT_CHARS = 16_000

# App Capture 的制品词汇、soft-optional 状态和持久化边界统一来自正式合同；
# 这里仅保留历史导出名，避免 Official/Fake Adapter 与旧调用方漂移。
APP_ALL_ARTIFACT_TYPES = frozenset(APP_CAPTURE_ARTIFACT_TYPES)
APP_EXTENDED_ARTIFACT_TYPES = frozenset(
    APP_ALL_ARTIFACT_TYPES - {"screenshot", "action_trace", "ui_tree"}
)
# 采集失败可降级，不得阻断主截图链路；required 由 Capture Profile 角色合同裁决。
APP_SOFT_OPTIONAL_ARTIFACTS = frozenset(APP_CAPTURE_ARTIFACT_CONTRACT.soft_optional_artifact_types)

_MAX_ACCESSIBILITY_VISIBLE_CHARS = 8_000
# 元素级 text／content-desc 保留完整可读串（39/45/48/13/14）；包级 visible_text 仍受 8k 约束
_MAX_UI_SNAPSHOT_ELEMENT_TEXT_CHARS = 4_000
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>\]\)}]+", re.IGNORECASE)
_INTENT_DATA_URL_RE = re.compile(
    r"(?:dat(?:a)?|url|mUrl|currentUrl|loadUrl)\s*[=:]\s*[\"']?((?:https?://)[^\s\]\"']+)",
    re.IGNORECASE,
)
_VOLATILE_TEXT_RE = re.compile(r"^[\d\s:./\-:+]+$")
_DYNAMIC_CLASS_HINTS = (
    "progressbar",
    "chronometer",
    "advertisement",
    "adview",
)
APP_ACCESSIBILITY_CONTRACT_ID = "app-accessibility-dynamic.v1"
_PAGE_TYPE_REGISTRY = get_default_page_type_registry()
_MAX_NAV_LABEL_CHARS = 18
_NAVIGATION_RISK_MARKERS = (
    "支付",
    "充值",
    "购买",
    "开通",
    "提交订单",
    "立即支付",
    "外链",
    "下载",
    "安装",
    "博彩",
    "开户",
)
_TRUSTED_DISMISS_LABEL_MARKERS = (
    "关闭",
    "取消",
    "返回",
    "跳过",
    "知道",
    "下次",
    "稍后",
    "以后",
    "暂不",
    "忽略",
    "不再",
    "close",
    "cancel",
    "back",
    "skip",
    "later",
    "got it",
    "not now",
    "dismiss",
)
_TRUSTED_DISMISS_RISK_MARKERS = (
    "支付",
    "付款",
    "购买",
    "充值",
    "开通",
    "转账",
    "提交订单",
    "删除",
    "卸载",
    "注销",
    "退出登录",
)
_TRUSTED_STARTUP_EXIT_LABELS = frozenset(
    {
        "跳过",
        "skip",
        "进入",
        "进入应用",
        "enter",
        "enter app",
    }
)
_TRUSTED_OBSTRUCTION_DISMISS_LABELS = frozenset(
    {
        "关闭",
        "取消",
        "跳过",
        "知道了",
        "我知道了",
        "下次再说",
        "以后再说",
        "稍后",
        "暂不",
        "暂不更新",
        "忽略",
        "不再提示",
        "close",
        "cancel",
        "skip",
        "later",
        "not now",
        "dismiss",
        "got it",
    }
)
_CONTENT_CARD_BLOCK_MARKERS = (
    "广告",
    "推广",
    "投放",
    "下载",
    "安装",
    "充值",
    "支付",
    "开户",
    "红包",
    "约炮",
    "博彩",
    "直连",
    "重装",
    "ad ",
    "advert",
    "download",
    "install",
    "recharge",
)
_DETAIL_BYLINE_RE = re.compile(r"^.{1,80}[•·|｜].{0,80}20\d{2}年\d{2}月\d{2}日$")


def project_visible_text_from_hierarchy(
    xml_text: str,
    *,
    max_chars: int = _MAX_ACCESSIBILITY_VISIBLE_CHARS,
) -> dict[str, Any]:
    """从 Accessibility / View Hierarchy 投影有界可读文本（不是 OCR）。"""
    text = (xml_text or "").strip()
    if "<hierarchy" not in text and not text.startswith("<?xml"):
        raise ValueError("ui_tree 必须为 hierarchy XML")
    root = ET.fromstring(text)
    parts: list[str] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        for attr in ("text", "content-desc"):
            value = (node.attrib.get(attr) or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            parts.append(value)
            if sum(len(item) + 1 for item in parts) >= max_chars:
                break
        if sum(len(item) + 1 for item in parts) >= max_chars:
            break
    joined = "\n".join(parts)
    truncated = len(joined) > max_chars
    if truncated:
        joined = joined[:max_chars]
    return {
        "source": "accessibility_hierarchy",
        "surface_kind": "android",
        "text": joined,
        "char_count": len(joined),
        "truncated": truncated,
        "node_text_count": len(parts),
    }


def project_ui_snapshot_from_hierarchy(
    xml_text: str,
    *,
    package_name: str | None = None,
    activity: str | None = None,
    max_nodes: int = _MAX_UI_SNAPSHOT_NODES,
) -> dict[str, Any]:
    """把 uiautomator hierarchy 投影为有界 ui_snapshot（非 DOM）。"""
    text = (xml_text or "").strip()
    if "<hierarchy" not in text and not text.startswith("<?xml"):
        raise ValueError("ui_tree 必须为 hierarchy XML")
    root = ET.fromstring(text)
    # 建立父子索引，供导航容器祖先扫描（对齐 Web within_navigation）
    parent_by_child: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in list(parent):
            parent_by_child[child] = parent
    nodes = list(root.iter("node"))
    nav_container_nodes: set[ET.Element] = set()
    for node in nodes:
        if _android_is_navigation_container(
            class_name=(node.attrib.get("class") or "").strip(),
            resource_id=(node.attrib.get("resource-id") or "").strip(),
        ):
            nav_container_nodes.add(node)

    def _under_navigation(node: ET.Element) -> bool:
        current: ET.Element | None = node
        while current is not None:
            if current in nav_container_nodes:
                return True
            current = parent_by_child.get(current)
        return False

    elements: list[dict[str, Any]] = []
    signature_parts: list[str] = []
    viewport_width = 0
    viewport_height = 0
    for index, node in enumerate(nodes):
        node_bounds = _parse_bounds(node.attrib.get("bounds"))
        if node_bounds is not None:
            viewport_width = max(viewport_width, int(node_bounds["right"]))
            viewport_height = max(viewport_height, int(node_bounds["bottom"]))
        if index >= max_nodes:
            continue
        bounds = node_bounds
        class_name = (node.attrib.get("class") or "").strip()
        resource_id = (node.attrib.get("resource-id") or "").strip()
        text_value = _bound_element_text(node.attrib.get("text"))
        content_desc = _bound_element_text(node.attrib.get("content-desc"))
        clickable = (node.attrib.get("clickable") or "").strip().casefold() == "true"
        rect = _rect_from_bounds(bounds)
        role = _android_accessibility_role(class_name)
        within_navigation = _under_navigation(node)
        # 导航容器内的可点／有文案子项归一为 tab，供 alioth_38 只比导航标签
        if within_navigation and node not in nav_container_nodes:
            if clickable or text_value or content_desc:
                if role in {None, "text", "button", "image"}:
                    role = "tab"
        elif node in nav_container_nodes and role is None:
            role = "tablist"
        media = _android_media_projection(class_name)
        attribution = _android_content_attribution(text_value, content_desc)
        entry = {
            "element_index": index,
            "class_name": class_name or None,
            "class_tokens": _android_class_tokens(class_name),
            "resource_id": resource_id or None,
            "selector": f"resource-id:{resource_id}" if resource_id else None,
            "role": role,
            "platform_role": class_name.rsplit(".", 1)[-1] if class_name else None,
            "within_navigation": True if within_navigation else None,
            "text": text_value or None,
            "content_desc": content_desc or None,
            "aria_label": content_desc or None,
            "name": content_desc or text_value or None,
            "clickable": clickable,
            "enabled": _optional_android_bool(node, "enabled"),
            "selected": _optional_android_bool(node, "selected"),
            "checked": _optional_android_bool(node, "checked"),
            "checkable": _optional_android_bool(node, "checkable"),
            "scrollable": _optional_android_bool(node, "scrollable"),
            "visible": _optional_android_bool(node, "visible-to-user"),
            "bounds": bounds,
            "rect": rect,
            "image": media,
            "attribution": attribution,
        }
        elements.append(
            {key: value for key, value in entry.items() if value is not None and value != ()}
        )
        if _is_stable_signature_node(
            class_name=class_name,
            resource_id=resource_id,
            text_value=text_value,
            content_desc=content_desc,
            clickable=clickable,
        ):
            signature_parts.append(
                "|".join(
                    [
                        class_name.rsplit(".", 1)[-1],
                        resource_id.rsplit("/", 1)[-1] if resource_id else "",
                        "c" if clickable else "n",
                        _bucket_bounds(bounds),
                    ]
                )
            )
    structure_signature = _sha256_hex("\n".join(signature_parts) or "empty")
    pkg = package_name or _first_attr(root, "package")
    act = activity or _first_attr(root, "activity")
    screen_id = build_screen_id(
        package_name=pkg,
        activity=act,
        structure_signature=structure_signature,
    )
    return {
        "source": "android_ui_hierarchy",
        "surface_kind": "android",
        "package_name": pkg,
        "activity": act,
        "element_count": len(elements),
        "truncated": len(nodes) > len(elements),
        "viewport": (
            {"width": viewport_width, "height": viewport_height}
            if viewport_width > 0 and viewport_height > 0
            else None
        ),
        "structure_signature": f"sha256:{structure_signature}",
        "screen_id": screen_id,
        "elements": elements,
    }


PLAYBACK_PROGRESS_MATERIAL_GAP = "playback_progress_material_missing"


def app_player_progress_observation(
    *,
    media_items: Sequence[Mapping[str, Any]] | None = None,
    timed_samples: Sequence[Mapping[str, Any]] | None = None,
    native_interaction: str | None = None,
) -> dict[str, Any]:
    """原生交互加连续观察。没有进度、时长或可比较的连续帧时明确记缺口。"""

    items = [item for item in (media_items or ()) if isinstance(item, Mapping)]
    samples = [item for item in (timed_samples or ()) if isinstance(item, Mapping)]
    has_timing = any(_media_item_has_progress(item) for item in items) or any(
        _media_item_has_progress(item) for item in samples
    )
    gaps: list[str] = []
    if not has_timing:
        gaps.append(PLAYBACK_PROGRESS_MATERIAL_GAP)
    interaction = str(native_interaction or "").strip() or "not_observed"
    return {
        "native_interaction": interaction,
        "continuous_observation": bool(samples),
        "playback_progress_available": has_timing,
        "gap_codes": gaps,
    }


def _media_item_has_progress(item: Mapping[str, Any]) -> bool:
    current = item.get("current_time")
    duration = item.get("duration")
    if isinstance(current, bool) or isinstance(duration, bool):
        return False
    if isinstance(current, (int, float)) and isinstance(duration, (int, float)):
        return duration > 0 and current >= 0
    progress = item.get("progress_ratio")
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        return False
    return 0 <= float(progress) <= 1


def project_media_state_from_ui_snapshot(
    ui_snapshot: Mapping[str, Any],
    *,
    player_wake: Mapping[str, Any] | None = None,
    timed_samples: Sequence[Mapping[str, Any]] | None = None,
    native_interaction: str | None = None,
) -> dict[str, Any]:
    """从 Android Accessibility 控件投影媒体候选，不伪造播放或资源真值。"""

    raw_elements = ui_snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    items: list[dict[str, Any]] = []
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        media = element.get("image")
        if not isinstance(media, Mapping):
            continue
        if media.get("source") != "android_accessibility_class":
            continue
        kind = str(media.get("kind") or "").strip()
        if kind not in {"image", "video"}:
            continue
        items.append(
            {
                "media_index": len(items),
                "selector": str(element.get("selector") or "")[:512],
                "tag": kind,
                "kind": kind,
                "source": "android_accessibility_class",
                "bounds": dict(element.get("rect") or {}),
            }
        )
        if len(items) >= 20:
            break
    limitations = [
        "accessibility_hierarchy_does_not_expose_playback_timing_or_resource_url",
        "hidden_player_controls_project_empty_media_state_wake_before_capture",
    ]
    progress = app_player_progress_observation(
        media_items=items,
        timed_samples=timed_samples,
        native_interaction=native_interaction,
    )
    payload: dict[str, Any] = {
        "schema_version": "app-media-state.v1",
        "source": "android_accessibility_class",
        "item_count": len(items),
        "items_truncated": len(items) >= 20,
        "capture_degraded": not progress["playback_progress_available"],
        "playback_state_available": progress["playback_progress_available"],
        "native_interaction": progress["native_interaction"],
        "continuous_observation": progress["continuous_observation"],
        "gap_codes": progress["gap_codes"],
        "limitations": limitations,
        "items": items,
        "display_candidates": [
            {
                "selector": item["selector"],
                "text": item["kind"],
                "bounds": item["bounds"],
            }
            for item in items
        ],
    }
    if isinstance(player_wake, Mapping) and player_wake:
        payload["player_wake_before_capture"] = dict(player_wake)
    return payload


def _bound_element_text(raw: str | None) -> str:
    """元素级文案去空白后有界保留，避免再截成 160 丢长标题／分享链接。"""

    value = (raw or "").strip()
    if len(value) > _MAX_UI_SNAPSHOT_ELEMENT_TEXT_CHARS:
        return value[:_MAX_UI_SNAPSHOT_ELEMENT_TEXT_CHARS]
    return value


def extract_http_urls_from_android_text(
    text: str,
    *,
    limit: int = 8,
) -> tuple[str, ...]:
    """从 dumpsys／层级 XML／可见文案中抽取真实 http(s) URL，不 invent。

    优先 Intent data／dat= 行，再扫通用 https?://；仅返回原文出现的链接。
    """

    raw = str(text or "")
    if not raw.strip():
        return ()
    ordered: list[str] = []
    seen: set[str] = set()

    def _push(candidate: str) -> None:
        cleaned = candidate.strip().rstrip(".,;:)\"'")
        if not cleaned.startswith(("http://", "https://")):
            return
        if len(cleaned) > 2048:
            cleaned = cleaned[:2048]
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        ordered.append(cleaned)

    for match in _INTENT_DATA_URL_RE.finditer(raw):
        _push(match.group(1))
        if len(ordered) >= limit:
            return tuple(ordered)
    for match in _HTTP_URL_RE.finditer(raw):
        _push(match.group(0))
        if len(ordered) >= limit:
            break
    return tuple(ordered)


def build_outbound_landing_url_metadata(
    *text_sources: str,
    source_hint: str | None = None,
) -> dict[str, Any] | None:
    """合并多路文本中的外跳候选 URL，供 Capture metadata／广告落地证据消费。"""

    candidates: list[str] = []
    seen: set[str] = set()
    for source in text_sources:
        for url in extract_http_urls_from_android_text(source):
            key = url.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(url)
            if len(candidates) >= 8:
                break
        if len(candidates) >= 8:
            break
    if not candidates:
        return None
    return {
        "url": candidates[0],
        "url_candidates": candidates,
        "source": str(source_hint or "android_text_or_dumpsys").strip()
        or "android_text_or_dumpsys",
    }


_OMNIBOX_ID_MARKERS = (
    "url_bar",
    "omnibox",
    "url_field",
    "location_bar",
    "search_box",
    "address_bar",
    "urlbar",
)


def extract_browser_omnibox_urls_from_hierarchy(xml_text: str) -> tuple[str, ...]:
    """从浏览器／Custom Tabs Accessibility 树抽地址栏 URL，不 invent。"""

    text = (xml_text or "").strip()
    if "<hierarchy" not in text and not text.startswith("<?xml"):
        return ()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ()
    ordered: list[str] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        resource_id = (node.attrib.get("resource-id") or "").casefold()
        class_name = (node.attrib.get("class") or "").casefold()
        looks_omnibox = any(marker in resource_id for marker in _OMNIBOX_ID_MARKERS)
        looks_edit = "edittext" in class_name or class_name.endswith(".edittext")
        if not looks_omnibox and not looks_edit:
            continue
        blob = " ".join(
            (node.attrib.get(attr) or "").strip()
            for attr in ("text", "content-desc", "hint")
        )
        for url in extract_http_urls_from_android_text(blob, limit=2):
            key = url.casefold()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(url)
            if len(ordered) >= 4:
                return tuple(ordered)
    return tuple(ordered)


def resolve_outbound_landing_from_channels(
    *,
    clipboard_text: str = "",
    dumpsys_text: str = "",
    hierarchy_xml: str = "",
    visible_text: str = "",
    package_name: str | None = None,
) -> dict[str, Any] | None:
    """按优先级合并外跳 URL：地址栏 → 剪贴板 → Intent/dumpsys → 层级／可见文案。"""

    omnibox = extract_browser_omnibox_urls_from_hierarchy(hierarchy_xml)
    if omnibox:
        return {
            "url": omnibox[0],
            "url_candidates": list(omnibox),
            "source": "browser_address_bar_uia",
            "package_name": package_name,
        }
    clip_urls = extract_http_urls_from_android_text(clipboard_text)
    if clip_urls:
        return {
            "url": clip_urls[0],
            "url_candidates": list(clip_urls),
            "source": "clipboard",
            "package_name": package_name,
        }
    # dumpsys：Intent dat／mUrl 优先于裸 https 噪点
    dump_urls = extract_http_urls_from_android_text(dumpsys_text)
    if dump_urls:
        source = "dumpsys_intent_or_murl"
        lowered = dumpsys_text.casefold()
        if "dat=" in lowered or "data=" in lowered:
            source = "intent_dat"
        elif "murl=" in lowered or "currenturl" in lowered:
            source = "dumpsys_murl"
        return {
            "url": dump_urls[0],
            "url_candidates": list(dump_urls),
            "source": source,
            "package_name": package_name,
        }
    return build_outbound_landing_url_metadata(
        hierarchy_xml,
        visible_text,
        source_hint="android_text_or_dumpsys",
    )


def should_wake_player_controls_before_media_capture(
    *,
    profile_id: str,
    requested_artifact_types: set[str] | frozenset[str] | Sequence[str],
    purpose: str = "",
) -> bool:
    """请求 media_state 时先唤醒隐藏控件；排除广告／开屏档案以免误点。"""

    requested = {
        str(item).strip()
        for item in requested_artifact_types
        if str(item).strip()
    }
    if "media_state" not in requested:
        return False
    haystack = f"{profile_id} {purpose}".casefold()
    if any(
        token in haystack
        for token in ("ad_outbound", "startup", "splash", "countdown", "开屏")
    ):
        return False
    return True


def _rect_from_bounds(bounds: Mapping[str, Any] | None) -> dict[str, int] | None:
    if bounds is None:
        return None
    return {
        "x": int(bounds["left"]),
        "y": int(bounds["top"]),
        "width": int(bounds["width"]),
        "height": int(bounds["height"]),
    }


def _android_class_tokens(class_name: str) -> tuple[str, ...]:
    if not class_name:
        return ()
    parts = tuple(item for item in class_name.split(".") if item)
    return tuple(dict.fromkeys((class_name, *parts)))


def _android_accessibility_role(class_name: str) -> str | None:
    """把真实 Android class 归一成跨端可读 role，不推断业务语义。"""

    lowered = class_name.casefold()
    tail = lowered.rsplit(".", 1)[-1]
    if "dialog" in tail:
        return "dialog"
    if tail in {
        "tablayout",
        "tabwidget",
        "bottomnavigationview",
        "bottomnavigation",
        "navigationbarview",
        "navigationrailview",
    } or "bottomnavigation" in tail:
        return "tablist"
    if "tabview" in tail or tail in {"tabitem", "tablayout$tabview"}:
        return "tab"
    return {
        "button": "button",
        "imagebutton": "button",
        "edittext": "textbox",
        "checkbox": "checkbox",
        "radiobutton": "radio",
        "switch": "switch",
        "togglebutton": "switch",
        "progressbar": "progressbar",
        "seekbar": "slider",
        "listview": "list",
        "recyclerview": "list",
        "textview": "text",
        "imageview": "image",
    }.get(tail)


_ANDROID_NAV_CONTAINER_TAILS = frozenset(
    {
        "tablayout",
        "tabwidget",
        "bottomnavigationview",
        "bottomnavigation",
        "navigationbarview",
        "navigationrailview",
    }
)


def _android_is_navigation_container(*, class_name: str, resource_id: str) -> bool:
    """仅按控件类型／常见 id 识别导航容器，不把内容区列表当成导航。"""

    tail = class_name.casefold().rsplit(".", 1)[-1]
    if tail in _ANDROID_NAV_CONTAINER_TAILS or "bottomnavigation" in tail:
        return True
    rid = resource_id.casefold()
    return any(
        marker in rid
        for marker in (
            "tablayout",
            "bottom_nav",
            "bottomnav",
            "bottom_navigation",
            ":id/tabs",
            "/tabs",
            "tab_layout",
        )
    )


def _android_media_projection(class_name: str) -> dict[str, str] | None:
    """仅按 Android 控件类型投影媒体候选，不推断素材内容或来源 URL。"""

    tail = class_name.casefold().rsplit(".", 1)[-1]
    if tail == "imageview":
        kind = "image"
    elif tail in {"videoview", "surfaceview", "textureview"}:
        kind = "video"
    else:
        return None
    return {
        "kind": kind,
        "source": "android_accessibility_class",
    }


def _android_content_attribution(
    text_value: str,
    content_desc: str,
) -> dict[str, str] | None:
    """从明确的作者与日期署名投影来源事实；不把普通文案猜成 UGC。"""

    for raw in (text_value, content_desc):
        label = " ".join(raw.split())
        if not label or _DETAIL_BYLINE_RE.fullmatch(label) is None:
            continue
        parts = re.split(r"[•·|｜]", label, maxsplit=1)
        if len(parts) != 2:
            continue
        author, published_at = (part.strip() for part in parts)
        if author and published_at:
            return {
                "author_text": author[:80],
                "published_at_text": published_at[:80],
                "source": "android_accessibility_byline",
            }
    return None


def _optional_android_bool(node: ET.Element, name: str) -> bool | None:
    raw = node.attrib.get(name)
    if raw is None:
        return None
    normalized = raw.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def infer_hierarchy_screen_size(xml_text: str) -> tuple[int, int] | None:
    """从 UI Hierarchy 根节点推断其 Accessibility 坐标窗口尺寸。

    Accessibility bounds 使用设备像素；根节点可能只是应用窗口，不能直接把它
    当作包含系统栏的全屏截图尺寸。调用方应把结果交给
    :func:`resolve_ui_tree_screen_size`，再做校验／归一化。
    """

    text = (xml_text or "").strip()
    if not text or ("<hierarchy" not in text and not text.startswith("<?xml")):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    candidates: list[tuple[int, int]] = []
    nodes: list[ET.Element] = [root]
    nodes.extend(list(root)[:8])
    for element in nodes:
        bounds = _parse_bounds(element.attrib.get("bounds"))
        if bounds is None:
            continue
        if (
            int(bounds["left"]) != 0
            or int(bounds["top"]) != 0
            or int(bounds["width"]) < 320
            or int(bounds["height"]) < 480
        ):
            continue
        candidates.append((int(bounds["width"]), int(bounds["height"])))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0] * item[1])


def resolve_ui_tree_screen_size(
    hierarchy_size: tuple[int, int] | None,
    *,
    capture_width: int,
    capture_height: int,
) -> tuple[int, int]:
    """推导 UIA bounds 应使用的统一归一化屏幕尺寸。

    UIA 根节点可能裁掉系统导航栏，而 Capture 仍包含完整屏幕；Capture 也可能
    是保持自身宽高比的缩略图。以 Capture 宽高比做统一缩放，并把它扩展到能
    容纳 UIA 根窗口的最小尺寸，避免对两个轴分别拉伸导致控件位置漂移。
    """

    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (capture_width, capture_height)
    ):
        raise ValueError("capture_width/capture_height 必须为正整数")
    if hierarchy_size is None:
        return capture_width, capture_height
    if (
        not isinstance(hierarchy_size, tuple)
        or len(hierarchy_size) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in hierarchy_size
        )
    ):
        raise ValueError("hierarchy_size 必须为两个正整数")

    root_width, root_height = hierarchy_size
    scale = max(
        1.0,
        root_width / capture_width,
        root_height / capture_height,
    )
    return (
        max(capture_width, math.ceil(capture_width * scale)),
        max(capture_height, math.ceil(capture_height * scale)),
    )


def find_unique_trusted_skip_target(
    xml_text: str,
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    """从当前目标包的 Accessibility 树中识别唯一、低风险的开屏退出控件。

    只接受精确的跳过／进入标签，并同时要求控件位于右上角、可点击、已启用、
    包名一致且占屏面积足够小。任何歧义、越界或过大的触控区域都 fail-closed。
    """

    text = (xml_text or "").strip()
    if not package_name.strip() or ("<hierarchy" not in text and not text.startswith("<?xml")):
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (screen_width, screen_height)
    ):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_SKIP_SCAN_NODES:
        return None
    matches: list[dict[str, Any]] = []
    for node in nodes:
        labels = {
            " ".join((node.attrib.get(attr) or "").split()).casefold()
            for attr in ("text", "content-desc")
        }
        labels.discard("")
        matched_labels = labels.intersection(_TRUSTED_STARTUP_EXIT_LABELS)
        if len(matched_labels) != 1:
            continue
        if (
            (node.attrib.get("clickable") or "").strip().casefold() != "true"
            or (node.attrib.get("enabled") or "").strip().casefold() != "true"
            or (node.attrib.get("package") or "").strip() != package_name.strip()
        ):
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None or not _is_safe_skip_bounds(
            bounds,
            screen_width=screen_width,
            screen_height=screen_height,
        ):
            continue
        matches.append(
            {
                "label": next(iter(matched_labels)),
                "package_name": package_name.strip(),
                "bounds": bounds,
            }
        )
    return matches[0] if len(matches) == 1 else None


def find_unique_trusted_age_confirm_target(
    xml_text: str,
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    """从当前目标包 UI Tree 识别唯一的「我年满18岁-进入」类确认控件。

    黑料不打烊等 App 首屏会卡在年龄门；仅当同帧 Accessibility 文案证明成年确认
    进入且唯一、可点、包内、几何有界时才签发。禁止「未满18／离开」与整屏猜测。
    """

    package = package_name.strip()
    text = (xml_text or "").strip()
    if not package or ("<hierarchy" not in text and not text.startswith("<?xml")):
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (screen_width, screen_height)
    ):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_APP_CONTROL_SCAN_NODES:
        return None

    matches: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package):
            continue
        labels = {
            " ".join((node.attrib.get(attr) or "").split())
            for attr in ("text", "content-desc")
        }
        labels.discard("")
        enter_labels = {
            label
            for label in labels
            if text_matches_age_confirm(label) and not text_matches_age_leave(label)
        }
        if len(enter_labels) != 1:
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None or not _is_safe_age_confirm_bounds(
            bounds,
            screen_width=screen_width,
            screen_height=screen_height,
        ):
            continue
        label = next(iter(enter_labels))
        matches[str(bounds["signature"])] = {
            "label": label,
            "package_name": package,
            "bounds": bounds,
        }
    if len(matches) != 1:
        return None
    return next(iter(matches.values()))


def find_trusted_obstruction_dismiss_targets(
    xml_text: str,
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> tuple[dict[str, Any], ...]:
    """提取当前目标包中带精确安全文案的弹层关闭控件。

    该投影只提供同帧 UI Tree 已证明的控件，不根据图片位置猜测 X，也不接受
    包含支付、删除、注销等副作用语义的按钮。
    """

    package = package_name.strip()
    text = (xml_text or "").strip()
    if not package or ("<hierarchy" not in text and not text.startswith("<?xml")):
        return ()
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (screen_width, screen_height)
    ):
        return ()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ()
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_APP_CONTROL_SCAN_NODES:
        return ()

    matches: dict[tuple[str, str], dict[str, Any]] = {}
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package):
            continue
        labels = {
            " ".join((node.attrib.get(attr) or "").split())
            for attr in ("text", "content-desc")
        }
        labels.discard("")
        safe_labels = {
            label
            for label in labels
            if label.casefold() in _TRUSTED_OBSTRUCTION_DISMISS_LABELS
            and not any(
                marker.casefold() in label.casefold()
                for marker in _TRUSTED_DISMISS_RISK_MARKERS
            )
        }
        if len(safe_labels) != 1:
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None or not _is_safe_labeled_action_bounds(
            bounds,
            screen_width=screen_width,
            screen_height=screen_height,
        ):
            continue
        label = next(iter(safe_labels))
        matches[(label.casefold(), str(bounds["signature"]))] = {
            "label": label,
            "package_name": package,
            "bounds": bounds,
        }
    return tuple(
        sorted(
            matches.values(),
            key=lambda item: (
                int(item["bounds"]["top"]),
                int(item["bounds"]["left"]),
                str(item["label"]).casefold(),
            ),
        )[:4]
    )


def find_unique_trusted_action_target_by_label(
    xml_text: str,
    *,
    package_name: str,
    semantic_name: str,
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    """用当前封存 UI Tree 校准唯一的低风险关闭类视觉候选。

    视觉 Provider 负责识别动作语义，Accessibility 只在同帧、同包、
    精确同名且唯一时提供几何校准。支付、删除、注销等高风险文案
    始终 fail-closed，禁止因坐标校准放大触控权限。
    """

    package = package_name.strip()
    label = " ".join(semantic_name.split())
    normalized_label = label.casefold()
    text = (xml_text or "").strip()
    if (
        not package
        or not label
        or len(label) > 40
        or ("<hierarchy" not in text and not text.startswith("<?xml"))
        or not any(marker in normalized_label for marker in _TRUSTED_DISMISS_LABEL_MARKERS)
        or any(marker in normalized_label for marker in _TRUSTED_DISMISS_RISK_MARKERS)
    ):
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (screen_width, screen_height)
    ):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_APP_CONTROL_SCAN_NODES:
        return None

    matches: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package):
            continue
        labels = {
            " ".join((node.attrib.get(attr) or "").split())
            for attr in ("text", "content-desc")
        }
        labels.discard("")
        if label not in labels:
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None or not _is_safe_labeled_action_bounds(
            bounds,
            screen_width=screen_width,
            screen_height=screen_height,
        ):
            continue
        matches[str(bounds["signature"])] = {
            "label": label,
            "package_name": package,
            "bounds": bounds,
        }
    return next(iter(matches.values())) if len(matches) == 1 else None


def find_unique_trusted_navigation_target_by_label(
    xml_text: str,
    *,
    package_name: str,
    semantic_name: str,
    expected_page_type: str | None,
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    """用同帧 UI Tree 的唯一低风险导航标签校准视觉候选坐标。"""

    package = package_name.strip()
    semantic = " ".join(semantic_name.split()).casefold()
    expected = (expected_page_type or "").strip()
    if (
        not package
        or not semantic
        or not expected
        or ("<hierarchy" not in xml_text and not xml_text.startswith("<?xml"))
    ):
        return None
    try:
        expected_code = _PAGE_TYPE_REGISTRY.require_code(expected).code
    except (KeyError, ValueError):
        alias = _PAGE_TYPE_REGISTRY.resolve_alias(expected)
        if alias is None:
            return None
        expected_code = alias.canonical_code
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_APP_CONTROL_SCAN_NODES:
        return None

    matches: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package):
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None or not _is_safe_labeled_action_bounds(
            bounds,
            screen_width=screen_width,
            screen_height=screen_height,
        ):
            continue
        for label in _node_label_lines(node):
            normalized_label = label.casefold()
            alias = _PAGE_TYPE_REGISTRY.resolve_alias(label)
            if (
                len(label) < 2
                or normalized_label not in semantic
                or alias is None
                or alias.canonical_code != expected_code
                or _is_high_risk_navigation(
                    label=label,
                    page_type=alias.canonical_code,
                )
            ):
                continue
            matches[str(bounds["signature"])] = {
                "label": label,
                "package_name": package,
                "page_type": alias.canonical_code,
                "bounds": bounds,
            }
            break
    return next(iter(matches.values())) if len(matches) == 1 else None


def find_trusted_post_publish_choice_target(
    xml_text: str,
    *,
    package_name: str,
    semantic_name: str,
    expected_page_type: str | None,
    normalized_bounds: Mapping[str, Any],
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    """用无标签点击横栏或同帧底栏锚点校准发帖类型候选。

    Flutter 可能把“图文帖子/视频帖子”两个视觉按钮合并为一个无标签
    Accessibility 点击横栏。这里只在视觉候选已明确指向 post_publish、语义为
    发帖类型选择时校准；优先使用唯一邻近横栏，主内容语义树滞后时可退到同帧
    底栏的相对锚点。左右分支始终沿用视觉候选，最终发布按钮不满足该合同。
    """

    package = package_name.strip()
    semantic = " ".join(semantic_name.split()).casefold()
    expected = str(expected_page_type or "").strip().casefold()
    text = (xml_text or "").strip()
    if (
        not package
        or expected != "post_publish"
        or not any(marker in semantic for marker in ("图文帖子", "视频帖子", "发帖"))
        or any(marker in semantic for marker in ("提交", "确认", "立即发布"))
        or screen_width <= 0
        or screen_height <= 0
        or ("<hierarchy" not in text and not text.startswith("<?xml"))
    ):
        return None
    try:
        left = float(normalized_bounds["left"])
        top = float(normalized_bounds["top"])
        right = float(normalized_bounds["right"])
        bottom = float(normalized_bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (
        0 <= left < right <= 1
        and 0.08 <= right - left <= 0.45
        and 0.6 <= top < bottom <= 0.93
    ):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_APP_CONTROL_SCAN_NODES:
        return None

    center_x = (left + right) / 2 * screen_width
    center_y = (top + bottom) / 2 * screen_height
    matches: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package):
            continue
        if _node_label_lines(node):
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None:
            continue
        width_ratio = int(bounds["width"]) / screen_width
        height_ratio = int(bounds["height"]) / screen_height
        row_center_y = (int(bounds["top"]) + int(bounds["bottom"])) / 2
        if not (
            0.45 <= width_ratio <= 0.95
            and 0.035 <= height_ratio <= 0.15
            and screen_height * 0.6 <= int(bounds["top"]) <= screen_height * 0.9
            and int(bounds["bottom"]) <= screen_height * 0.92
            and int(bounds["left"]) <= center_x <= int(bounds["right"])
            and abs(row_center_y - center_y) <= screen_height * 0.18
        ):
            continue
        calibrated_left = max(int(bounds["left"]), round(left * screen_width))
        calibrated_right = min(int(bounds["right"]), round(right * screen_width))
        if calibrated_right - calibrated_left < screen_width * 0.08:
            continue
        calibrated = {
            "left": calibrated_left,
            "top": int(bounds["top"]),
            "right": calibrated_right,
            "bottom": int(bounds["bottom"]),
            "width": calibrated_right - calibrated_left,
            "height": int(bounds["height"]),
            "signature": (
                f"{calibrated_left},{int(bounds['top'])},"
                f"{calibrated_right},{int(bounds['bottom'])}"
            ),
        }
        matches[str(bounds["signature"])] = {
            "label": semantic_name,
            "package_name": package,
            "bounds": calibrated,
        }
    if len(matches) == 1:
        return next(iter(matches.values()))
    if matches:
        return None
    return _target_above_trusted_bottom_navigation(
        nodes,
        package_name=package,
        semantic_name=semantic_name,
        left=left,
        right=right,
        screen_width=screen_width,
        screen_height=screen_height,
        app_window_bounds=_app_window_bounds_from_hierarchy(
            root,
            nodes,
            package_name=package,
            screen_width=screen_width,
            screen_height=screen_height,
        ),
    )


def find_trusted_post_publish_entry_target(
    xml_text: str,
    *,
    package_name: str,
    semantic_name: str,
    expected_page_type: str | None,
    normalized_bounds: Mapping[str, Any],
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    """用当前 UI Tree 的底栏几何校准圈子页悬浮发帖入口。

    Flutter 的悬浮加号可能不出现在 Accessibility 节点中，但同帧底栏通常可靠。
    这里只用底栏顶部和高度推导悬浮按钮纵向范围，横向仍沿用视觉候选；发帖
    类型选择和最终发布按钮不满足该合同。
    """

    package = package_name.strip()
    semantic = " ".join(semantic_name.split()).casefold()
    expected = str(expected_page_type or "").strip().casefold()
    text = (xml_text or "").strip()
    if (
        not package
        or expected != "post_publish"
        or not any(marker in semantic for marker in ("发帖入口", "发布加号", "发帖", "加号"))
        or any(
            marker in semantic
            for marker in ("图文帖子", "视频帖子", "提交", "确认", "立即发布")
        )
        or screen_width <= 0
        or screen_height <= 0
        or ("<hierarchy" not in text and not text.startswith("<?xml"))
    ):
        return None
    try:
        left = float(normalized_bounds["left"])
        top = float(normalized_bounds["top"])
        right = float(normalized_bounds["right"])
        bottom = float(normalized_bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (
        0.5 <= left < right <= 1
        and 0.06 <= right - left <= 0.4
        and 0.5 <= top < bottom <= 1
    ):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_APP_CONTROL_SCAN_NODES:
        return None
    app_window_bounds = _app_window_bounds_from_hierarchy(
        root,
        nodes,
        package_name=package,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    return _target_above_trusted_bottom_navigation(
        nodes,
        package_name=package,
        semantic_name=semantic_name,
        left=left,
        right=right,
        screen_width=screen_width,
        screen_height=screen_height,
        app_window_bounds=app_window_bounds,
    )


def _target_above_trusted_bottom_navigation(
    nodes: list[ET.Element],
    *,
    package_name: str,
    semantic_name: str,
    left: float,
    right: float,
    screen_width: int,
    screen_height: int,
    app_window_bounds: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """只用当前 UI Tree 的底栏拓扑推导其上方单行操作区。"""

    navigation = _trusted_bottom_navigation(
        nodes,
        package_name=package_name,
        screen_width=screen_width,
        screen_height=screen_height,
        app_window_bounds=app_window_bounds,
    )
    if not navigation:
        return None
    nav_top = min(int(item["bounds"]["top"]) for item in navigation)
    nav_bottom = max(int(item["bounds"]["bottom"]) for item in navigation)
    nav_height = nav_bottom - nav_top
    if nav_height <= 0:
        return None

    center_y = nav_top - round(nav_height * 0.65)
    target_height = max(1, round(nav_height * 0.9))
    calibrated_top = max(0, center_y - target_height // 2)
    calibrated_bottom = min(nav_top - 1, calibrated_top + target_height)
    calibrated_left = round(left * screen_width)
    calibrated_right = round(right * screen_width)
    if not (
        calibrated_top >= screen_height * 0.65
        and calibrated_bottom < nav_top
        and calibrated_right - calibrated_left >= screen_width * 0.06
    ):
        return None
    bounds = {
        "left": calibrated_left,
        "top": calibrated_top,
        "right": calibrated_right,
        "bottom": calibrated_bottom,
        "width": calibrated_right - calibrated_left,
        "height": calibrated_bottom - calibrated_top,
        "signature": (
            f"{calibrated_left},{calibrated_top},"
            f"{calibrated_right},{calibrated_bottom}"
        ),
    }
    return {
        "label": semantic_name,
        "package_name": package_name,
        "bounds": bounds,
    }


def inspect_trusted_app_accessibility_controls(
    xml_text: str,
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
    screenshot_png: bytes | None = None,
) -> dict[str, Any] | None:
    """从目标包 UI Tree 动态识别低风险业务控件。

    这里不维护 APK 或 package 白名单。合同只消费已封存并校验过的
    Accessibility Hierarchy：底栏由目标包一致性、底部几何分组、唯一短标签、
    可点击启用状态和屏幕边界共同确认；选中项只有命中 PageType Catalog 别名时
    才映射页面类型，未知标签保持未知。弹层关闭和普通内容卡片也只在结构证据
    足够强时输出，绝不根据坐标猜测高风险触控。
    """

    package = package_name.strip()
    text = (xml_text or "").strip()
    if not package or ("<hierarchy" not in text and not text.startswith("<?xml")):
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (screen_width, screen_height)
    ):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    nodes = list(root.iter("node"))
    if len(nodes) > _MAX_TRUSTED_APP_CONTROL_SCAN_NODES:
        return None
    app_window_bounds = _app_window_bounds_from_hierarchy(
        root,
        nodes,
        package_name=package,
        screen_width=screen_width,
        screen_height=screen_height,
    )

    navigation = _trusted_bottom_navigation(
        nodes,
        package_name=package,
        screen_width=screen_width,
        screen_height=screen_height,
        app_window_bounds=app_window_bounds,
    )
    overlay_close = _trusted_overlay_close(
        nodes,
        package_name=package,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    content_cards = (
        ()
        if overlay_close is not None
        else _trusted_content_cards(
            nodes,
            package_name=package,
            screen_width=screen_width,
            screen_height=screen_height,
        )
    )
    selected_page_type = _selected_page_type(nodes, package_name=package)
    pixel_selected_page_type, pixel_selected_labels = _pixel_selected_page_type(
        nodes,
        package_name=package,
        screen_width=screen_width,
        screen_height=screen_height,
        screenshot_png=screenshot_png,
    )
    if pixel_selected_page_type is not None and selected_page_type in {None, "home"}:
        selected_page_type = pixel_selected_page_type
    page_type = None if overlay_close is not None else selected_page_type
    if page_type is None and overlay_close is None and content_cards:
        page_type = "content_list"
    if (
        page_type is None
        and overlay_close is None
        and _has_generic_content_detail(
            nodes,
            package_name=package,
            screen_width=screen_width,
            screen_height=screen_height,
        )
    ):
        page_type = "content_detail"
    return {
        "contract_id": APP_ACCESSIBILITY_CONTRACT_ID,
        "package_name": package,
        "bottom_navigation": navigation,
        "overlay_close": overlay_close,
        "content_cards": content_cards,
        "page_type": page_type,
        "selected_page_type": selected_page_type,
        "pixel_selected_page_type": pixel_selected_page_type,
        "pixel_selected_labels": pixel_selected_labels,
    }


def _trusted_bottom_navigation(
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
    app_window_bounds: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package_name):
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        label = _short_navigation_label(node)
        if (
            label is None
            or bounds is None
            or not _is_safe_bottom_navigation_bounds(
                bounds,
                screen_width=screen_width,
                screen_height=screen_height,
                app_window_bounds=app_window_bounds,
            )
        ):
            continue
        key = (label.casefold(), str(bounds["signature"]))
        if key in seen:
            continue
        seen.add(key)
        alias = _PAGE_TYPE_REGISTRY.resolve_alias(label)
        page_type = alias.canonical_code if alias is not None else None
        high_risk = _is_high_risk_navigation(label=label, page_type=page_type)
        candidates.append(
            {
                "label": label,
                "package_name": package_name,
                "bounds": bounds,
                "selected": _is_selected_node(node),
                "page_type": page_type,
                "action_allowed": not high_risk,
                "risk_level": "high" if high_risk else "low",
            }
        )
    if not candidates:
        return ()

    baseline_tolerance = max(8, round(screen_height * 0.02))
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (int(item["bounds"]["top"]), int(item["bounds"]["left"])),
    ):
        bounds = candidate["bounds"]
        placed = False
        for group in groups:
            group_bounds = group[0]["bounds"]
            if (
                abs(int(bounds["top"]) - int(group_bounds["top"])) <= baseline_tolerance
                and abs(int(bounds["bottom"]) - int(group_bounds["bottom"])) <= baseline_tolerance
            ):
                group.append(candidate)
                placed = True
                break
        if not placed:
            groups.append([candidate])

    valid_groups: list[tuple[dict[str, Any], ...]] = []
    for group in groups:
        ordered = tuple(sorted(group, key=lambda item: int(item["bounds"]["left"])))
        if not 3 <= len(ordered) <= 8:
            continue
        labels = [str(item["label"]).casefold() for item in ordered]
        if len(set(labels)) != len(labels):
            continue
        if int(ordered[0]["bounds"]["left"]) > screen_width * 0.06:
            continue
        if int(ordered[-1]["bounds"]["right"]) < screen_width * 0.94:
            continue
        max_gap = max(
            (
                int(current["bounds"]["left"]) - int(previous["bounds"]["right"])
                for previous, current in zip(ordered, ordered[1:], strict=False)
            ),
            default=0,
        )
        if max_gap > max(12, round(screen_width * 0.04)):
            continue
        valid_groups.append(ordered)
    if not valid_groups:
        return ()
    return max(
        valid_groups,
        key=lambda group: (
            len(group),
            -sum(int(item["bounds"]["top"]) for item in group),
        ),
    )


def _short_navigation_label(node: ET.Element) -> str | None:
    """提取一个短且稳定的导航文案，过滤标签序号等动态尾缀。"""

    labels: list[str] = []
    for attr in ("content-desc", "text"):
        for raw_line in (node.attrib.get(attr) or "").splitlines():
            label = " ".join(raw_line.split())
            if not 1 <= len(label) <= _MAX_NAV_LABEL_CHARS:
                continue
            if "标签" in label or "tab" == label.casefold():
                continue
            if _VOLATILE_TEXT_RE.fullmatch(label):
                continue
            if label.casefold() not in {item.casefold() for item in labels}:
                labels.append(label)
    return labels[0] if len(labels) == 1 else None


def _is_selected_node(node: ET.Element) -> bool:
    selected = (node.attrib.get("selected") or "").strip().casefold()
    return selected == "true" or (
        (node.attrib.get("checked") or "").strip().casefold() == "true"
        and (node.attrib.get("checkable") or "").strip().casefold() == "true"
    )


def _is_high_risk_navigation(*, label: str, page_type: str | None) -> bool:
    lowered = label.casefold()
    if any(marker.casefold() in lowered for marker in _NAVIGATION_RISK_MARKERS):
        return True
    if page_type is None:
        return False
    try:
        return bool(_PAGE_TYPE_REGISTRY.require_code(page_type).risk_actions)
    except (KeyError, ValueError):
        return False


def _selected_page_type(
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
) -> str | None:
    """仅将目标包中选中的可读别名映射为正式页面类型。"""

    matches: list[str] = []
    for node in nodes:
        if (node.attrib.get("package") or "").strip() != package_name:
            continue
        if not _is_selected_node(node):
            continue
        for label in _node_label_lines(node):
            alias = _PAGE_TYPE_REGISTRY.resolve_alias(label)
            if alias is not None:
                matches.append(alias.canonical_code)
                break
    unique = tuple(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _pixel_selected_page_type(
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
    screenshot_png: bytes | None,
) -> tuple[str | None, tuple[str, ...]]:
    """用同帧导航文字/图标的唯一高饱和态补足 Flutter selected=false。"""

    if not isinstance(screenshot_png, bytes) or not screenshot_png:
        return None, ()
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, ()
    encoded = np.frombuffer(screenshot_png, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (screen_height, screen_width):
        return None, ()
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    selected: list[tuple[str, str]] = []
    for group in _trusted_horizontal_navigation_groups(
        nodes,
        package_name=package_name,
        screen_width=screen_width,
        screen_height=screen_height,
    ):
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in group:
            bounds = item["bounds"]
            crop = hsv[
                int(bounds["top"]) : int(bounds["bottom"]),
                int(bounds["left"]) : int(bounds["right"]),
            ]
            if crop.size == 0:
                continue
            score = float(
                (
                    (crop[:, :, 1] >= 70)
                    & (crop[:, :, 2] >= 100)
                ).mean()
            )
            scored.append((score, item))
        if len(scored) < 2:
            continue
        scored.sort(key=lambda pair: pair[0], reverse=True)
        best_score, best = scored[0]
        second_score = scored[1][0]
        if (
            best_score < 0.02
            or best_score < second_score + 0.015
            or best_score < second_score * 1.75
        ):
            continue
        alias = _PAGE_TYPE_REGISTRY.resolve_alias(str(best["label"]))
        if alias is not None:
            selected.append((alias.canonical_code, str(best["label"])))

    unique = tuple(dict.fromkeys(page_type for page_type, _ in selected))
    specific = tuple(page_type for page_type in unique if page_type != "home")
    if len(specific) == 1:
        page_type = specific[0]
    elif not specific and len(unique) == 1:
        page_type = unique[0]
    else:
        page_type = None
    labels = tuple(dict.fromkeys(label for _, label in selected))
    return page_type, labels


def _trusted_horizontal_navigation_groups(
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> tuple[tuple[dict[str, Any], ...], ...]:
    """提取顶部或底部同基线导航组，排除页面中部内容卡片。"""

    candidates: list[dict[str, Any]] = []
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package_name):
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        label = _short_navigation_label(node)
        if bounds is None or label is None:
            continue
        top = int(bounds["top"])
        bottom = int(bounds["bottom"])
        width_ratio = int(bounds["width"]) / screen_width
        height_ratio = int(bounds["height"]) / screen_height
        if (
            not (bottom <= screen_height * 0.45 or top >= screen_height * 0.75)
            or not 0.02 <= width_ratio <= 0.5
            or not 0.02 <= height_ratio <= 0.2
        ):
            continue
        candidates.append({"label": label, "bounds": bounds})

    tolerance = max(8, round(screen_height * 0.02))
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (int(item["bounds"]["top"]), int(item["bounds"]["left"])),
    ):
        bounds = candidate["bounds"]
        for group in groups:
            reference = group[0]["bounds"]
            if (
                abs(int(bounds["top"]) - int(reference["top"])) <= tolerance
                and abs(int(bounds["bottom"]) - int(reference["bottom"])) <= tolerance
            ):
                group.append(candidate)
                break
        else:
            groups.append([candidate])

    valid: list[tuple[dict[str, Any], ...]] = []
    for group in groups:
        ordered = tuple(sorted(group, key=lambda item: int(item["bounds"]["left"])))
        if not 3 <= len(ordered) <= 10:
            continue
        labels = tuple(str(item["label"]).casefold() for item in ordered)
        if len(set(labels)) != len(labels):
            continue
        span = int(ordered[-1]["bounds"]["right"]) - int(ordered[0]["bounds"]["left"])
        if span < screen_width * 0.4:
            continue
        max_gap = max(
            (
                int(current["bounds"]["left"]) - int(previous["bounds"]["right"])
                for previous, current in zip(ordered, ordered[1:], strict=False)
            ),
            default=0,
        )
        if max_gap > max(48, round(screen_width * 0.08)):
            continue
        valid.append(ordered)
    return tuple(valid)


def _trusted_overlay_close(
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for parent in nodes:
        if not _is_enabled_target_click(parent, package_name=package_name):
            continue
        parent_bounds = _parse_bounds(parent.attrib.get("bounds"))
        if parent_bounds is None or not _covers_screen(
            parent_bounds,
            screen_width=screen_width,
            screen_height=screen_height,
            minimum_ratio=0.95,
        ):
            continue
        children = list(parent)
        descendants = list(parent.iter("node"))[1:]
        has_underlay = any(
            _is_enabled_target_click(child, package_name=package_name)
            and (bounds := _parse_bounds(child.attrib.get("bounds"))) is not None
            and _covers_screen(
                bounds,
                screen_width=screen_width,
                screen_height=screen_height,
                minimum_ratio=0.95,
            )
            for child in descendants
        )
        if not has_underlay:
            continue
        bodies = [
            bounds
            for child in children
            if _is_unlabelled_naf_image_click(child, package_name=package_name)
            and (bounds := _parse_bounds(child.attrib.get("bounds"))) is not None
            and _is_overlay_body_bounds(
                bounds,
                screen_width=screen_width,
                screen_height=screen_height,
            )
        ]
        closes = [
            bounds
            for child in children
            if _is_unlabelled_naf_image_click(child, package_name=package_name)
            and (bounds := _parse_bounds(child.attrib.get("bounds"))) is not None
            and _is_overlay_close_bounds(
                bounds,
                screen_width=screen_width,
                screen_height=screen_height,
            )
        ]
        if len(bodies) != 1 or len(closes) != 1:
            continue
        if int(closes[0]["top"]) <= int(bodies[0]["bottom"]):
            continue
        matches.append(
            {
                "label": "关闭弹层",
                "package_name": package_name,
                "bounds": closes[0],
            }
        )
    unique = {item["bounds"]["signature"]: item for item in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _trusted_content_cards(
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> tuple[dict[str, Any], ...]:
    matches: list[dict[str, Any]] = []
    for node in nodes:
        if not _is_enabled_target_click(node, package_name=package_name):
            continue
        original_lines = _node_label_lines(node)
        title = _content_card_title(original_lines)
        if title is None:
            continue
        lowered = " ".join(original_lines).casefold()
        if any(marker in lowered for marker in _CONTENT_CARD_BLOCK_MARKERS):
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None or not _is_content_card_bounds(
            bounds,
            screen_width=screen_width,
            screen_height=screen_height,
        ):
            continue
        if not any(
            (child.attrib.get("class") or "").casefold().endswith(("imageview", "videoview"))
            and (child.attrib.get("package") or "").strip() == package_name
            and (child.attrib.get("clickable") or "").strip().casefold() != "true"
            for child in node.iter("node")
            if child is not node
        ):
            continue
        matches.append(
            {
                "label": "普通内容卡片",
                "package_name": package_name,
                "bounds": bounds,
                "title_sha256": _sha256_hex(title),
            }
        )
    return tuple(
        sorted(
            matches,
            key=lambda item: (
                int(item["bounds"]["top"]),
                int(item["bounds"]["left"]),
            ),
        )[:3]
    )


def _content_card_title(lines: Sequence[str]) -> str | None:
    """从卡片可访问文案中提取标题，不依赖某个 App 的徽标。"""

    normalized = tuple(" ".join(line.split()) for line in lines if line.strip())
    if len(normalized) >= 2:
        title = " ".join(normalized[1:])
        if len(title) >= 12:
            return title[:240]
    long_lines = tuple(line for line in normalized if len(line) >= 20)
    if long_lines:
        return max(long_lines, key=len)[:240]
    return None


def _has_generic_content_detail(
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> bool:
    """用通用的详情页结构补足页型，不据此签发任何触控候选。"""
    title_bottoms: list[int] = []
    byline_tops: list[int] = []
    has_detail_scroll = False
    for node in nodes:
        if (node.attrib.get("package") or "").strip() != package_name:
            continue
        bounds = _parse_bounds(node.attrib.get("bounds"))
        if bounds is None:
            continue
        label = " ".join(_node_label_lines(node))
        clickable = (node.attrib.get("clickable") or "").strip().casefold() == "true"
        class_name = (node.attrib.get("class") or "").strip()
        if (
            not clickable
            and 20 <= len(label) <= 240
            and not _DETAIL_BYLINE_RE.fullmatch(label)
            and int(bounds["left"]) <= screen_width * 0.08
            and int(bounds["right"]) >= screen_width * 0.92
            and screen_height * 0.1 <= int(bounds["top"]) <= screen_height * 0.3
            and int(bounds["bottom"]) <= screen_height * 0.35
        ):
            title_bottoms.append(int(bounds["bottom"]))
        if (
            not clickable
            and _DETAIL_BYLINE_RE.fullmatch(label)
            and int(bounds["left"]) <= screen_width * 0.08
            and screen_height * 0.15 <= int(bounds["top"]) <= screen_height * 0.35
        ):
            byline_tops.append(int(bounds["top"]))
        if (
            class_name.endswith("ScrollView")
            and int(bounds["left"]) <= screen_width * 0.02
            and int(bounds["right"]) >= screen_width * 0.98
            and screen_height * 0.05 <= int(bounds["top"]) <= screen_height * 0.2
            and int(bounds["bottom"]) >= screen_height * 0.85
            and int(bounds["height"]) >= screen_height * 0.65
        ):
            has_detail_scroll = True
    has_title_byline_order = any(
        title_bottom < byline_top for title_bottom in title_bottoms for byline_top in byline_tops
    )
    return has_detail_scroll and has_title_byline_order


def _exact_node_labels(node: ET.Element) -> set[str]:
    return {
        " ".join((node.attrib.get(attr) or "").split())
        for attr in ("text", "content-desc")
        if " ".join((node.attrib.get(attr) or "").split())
    }


def _node_label_lines(node: ET.Element) -> tuple[str, ...]:
    for attr in ("content-desc", "text"):
        lines = tuple(
            item.strip() for item in (node.attrib.get(attr) or "").splitlines() if item.strip()
        )
        if lines:
            return lines
    return ()


def _is_enabled_target_click(node: ET.Element, *, package_name: str) -> bool:
    return (
        (node.attrib.get("package") or "").strip() == package_name
        and (node.attrib.get("clickable") or "").strip().casefold() == "true"
        and (node.attrib.get("enabled") or "").strip().casefold() == "true"
    )


def _is_unlabelled_naf_image_click(node: ET.Element, *, package_name: str) -> bool:
    return (
        _is_enabled_target_click(node, package_name=package_name)
        and (node.attrib.get("class") or "").endswith("ImageView")
        and (node.attrib.get("NAF") or "").strip().casefold() == "true"
        and not _exact_node_labels(node)
        and not (node.attrib.get("resource-id") or "").strip()
    )


def _covers_screen(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
    minimum_ratio: float,
) -> bool:
    return (
        int(bounds["left"]) <= screen_width * (1 - minimum_ratio)
        and int(bounds["top"]) <= screen_height * (1 - minimum_ratio)
        and int(bounds["right"]) >= screen_width * minimum_ratio
        and int(bounds["bottom"]) >= screen_height * minimum_ratio
    )


def _app_window_bounds_from_hierarchy(
    hierarchy_root: ET.Element,
    nodes: Sequence[ET.Element],
    *,
    package_name: str,
    screen_width: int,
    screen_height: int,
) -> dict[str, Any] | None:
    """识别包含目标包内容的全宽应用窗口，供裁剪根坐标做相对校验。"""

    package = package_name.strip()
    if not package or screen_width <= 0 or screen_height <= 0:
        return None
    target_bounds = [
        bounds
        for node in nodes
        if (node.attrib.get("package") or "").strip() == package
        for bounds in (_parse_bounds(node.attrib.get("bounds")),)
        if bounds is not None
    ]
    if not target_bounds:
        return None
    max_target_right = max(int(bounds["right"]) for bounds in target_bounds)
    max_target_bottom = max(int(bounds["bottom"]) for bounds in target_bounds)
    root_candidates = [hierarchy_root, *list(hierarchy_root)[:8]]
    matches: list[dict[str, Any]] = []
    for element in root_candidates:
        if (element.attrib.get("package") or "").strip() != package:
            continue
        bounds = _parse_bounds(element.attrib.get("bounds"))
        if bounds is None:
            continue
        if (
            int(bounds["left"]) != 0
            or int(bounds["top"]) != 0
            or int(bounds["right"]) > screen_width
            or int(bounds["bottom"]) > screen_height
            or int(bounds["width"]) < screen_width * 0.98
            or int(bounds["right"]) < max_target_right
            or int(bounds["bottom"]) < max_target_bottom
        ):
            continue
        matches.append(bounds)
    if not matches:
        return None
    return max(
        matches,
        key=lambda bounds: (int(bounds["width"]) * int(bounds["height"]), int(bounds["bottom"])),
    )


def _is_safe_bottom_navigation_bounds(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
    app_window_bounds: Mapping[str, Any] | None = None,
) -> bool:
    left = int(bounds["left"])
    top = int(bounds["top"])
    right = int(bounds["right"])
    bottom = int(bounds["bottom"])
    if left < 0 or top < 0 or right > screen_width or bottom > screen_height:
        return False
    if app_window_bounds is None:
        minimum_top = screen_height * 0.76
        minimum_bottom = screen_height * 0.95
        minimum_height = screen_height * 0.03
    else:
        root_left = int(app_window_bounds["left"])
        root_top = int(app_window_bounds["top"])
        root_right = int(app_window_bounds["right"])
        root_bottom = int(app_window_bounds["bottom"])
        root_height = root_bottom - root_top
        if (
            root_left < 0
            or root_top < 0
            or root_right > screen_width
            or root_bottom > screen_height
        ):
            return False
        if (
            root_height <= 0
            or left < root_left
            or right > root_right
            or top < root_top
            or bottom > root_bottom
        ):
            return False
        minimum_top = max(screen_height * 0.76, root_top + root_height * 0.76)
        minimum_bottom = root_bottom - max(24, round(root_height * 0.05))
        minimum_height = max(24, round(screen_height * 0.015))
    width_ratio = (right - left) / screen_width
    height_ratio = (bottom - top) / screen_height
    return (
        top >= minimum_top
        and bottom >= minimum_bottom
        and 0.04 <= width_ratio <= 0.5
        and bottom - top >= minimum_height
        and height_ratio <= 0.2
        and width_ratio * height_ratio <= 0.1
    )


def _is_safe_labeled_action_bounds(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
) -> bool:
    """关闭类标签候选必须是屏内有界控件，不接受整屏点击层。"""

    left = int(bounds["left"])
    top = int(bounds["top"])
    right = int(bounds["right"])
    bottom = int(bounds["bottom"])
    width = int(bounds["width"])
    height = int(bounds["height"])
    area = width * height
    return (
        0 <= left < right <= screen_width
        and 0 <= top < bottom <= screen_height
        and width >= max(24, round(screen_width * 0.02))
        and height >= max(24, round(screen_height * 0.015))
        and width <= screen_width * 0.9
        and height <= screen_height * 0.3
        and area <= screen_width * screen_height * 0.3
    )


def _is_safe_age_confirm_bounds(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
) -> bool:
    """年龄确认进入键须在屏下半有界区域，避免误点首屏广告橙色 CTA。"""

    if not _is_safe_labeled_action_bounds(
        bounds,
        screen_width=screen_width,
        screen_height=screen_height,
    ):
        return False
    # 对齐 untitled1：点击点／控件须落在屏幕下半，否则 fail-closed。
    return int(bounds["top"]) >= int(screen_height * 0.45)


def _is_overlay_body_bounds(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
) -> bool:
    width_ratio = int(bounds["width"]) / screen_width
    height_ratio = int(bounds["height"]) / screen_height
    return (
        0.6 <= width_ratio <= 0.95
        and 0.35 <= height_ratio <= 0.65
        and int(bounds["top"]) >= screen_height * 0.15
        and int(bounds["bottom"]) <= screen_height * 0.8
    )


def _is_overlay_close_bounds(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
) -> bool:
    width_ratio = int(bounds["width"]) / screen_width
    height_ratio = int(bounds["height"]) / screen_height
    center_x_ratio = (int(bounds["left"]) + int(bounds["right"])) / 2 / screen_width
    return (
        0.05 <= width_ratio <= 0.15
        and 0.03 <= height_ratio <= 0.12
        and width_ratio * height_ratio <= 0.02
        and 0.4 <= center_x_ratio <= 0.6
        and screen_height * 0.6 <= int(bounds["top"]) <= screen_height * 0.85
    )


def _is_content_card_bounds(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
) -> bool:
    width_ratio = int(bounds["width"]) / screen_width
    height_ratio = int(bounds["height"]) / screen_height
    area_ratio = width_ratio * height_ratio
    return (
        int(bounds["left"]) <= screen_width * 0.05
        and int(bounds["right"]) >= screen_width * 0.95
        and screen_height * 0.15 <= int(bounds["top"]) < screen_height * 0.85
        and 0.08 <= height_ratio <= 0.4
        and 0.1 <= area_ratio <= 0.4
    )


def build_screen_id(
    *,
    package_name: str | None,
    activity: str | None,
    structure_signature: str,
    language: str | None = None,
    region: str | None = None,
) -> str:
    """稳定 screen 身份：包名 + Activity + 结构签名（可选语区盐）。"""
    locale = f"{(language or '').strip()}|{(region or '').strip()}"
    digest = _sha256_hex(
        f"{(package_name or '').strip()}|{(activity or '').strip()}|"
        f"{structure_signature.strip()}|{locale}"
    )
    return f"screen:{digest[:32]}"


def bound_view_hierarchy_payload(
    *,
    raw_text: str,
    package_name: str | None = None,
    activity: str | None = None,
    status: str = "captured",
    reason: str | None = None,
) -> dict[str, Any]:
    """独立 View Hierarchy 有界制品（dumpsys，非 Accessibility XML）。"""
    text = (raw_text or "").replace("\x00", "")
    truncated = len(text) > 24_000
    body = text[:24_000]
    return {
        "artifact_kind": "view_hierarchy",
        "source": "dumpsys_activity_top",
        "surface_kind": "android",
        "package_name": package_name,
        "activity": activity,
        "status": status,
        "reason": reason or "view_server_channel",
        "truncated": truncated,
        "char_count": len(body),
        "checksum": f"sha256:{_sha256_hex(body)}",
        "excerpt": body,
    }


def _is_stable_signature_node(
    *,
    class_name: str,
    resource_id: str,
    text_value: str,
    content_desc: str,
    clickable: bool,
) -> bool:
    lowered_class = class_name.casefold()
    if any(hint in lowered_class for hint in _DYNAMIC_CLASS_HINTS):
        return False
    if resource_id:
        return True
    if clickable and (content_desc or (text_value and not _VOLATILE_TEXT_RE.fullmatch(text_value))):
        return True
    return False


def _bucket_bounds(bounds: dict[str, Any] | None) -> str:
    if not bounds:
        return ""
    try:
        left = int(bounds["left"]) // 40
        top = int(bounds["top"]) // 40
        width = int(bounds["width"]) // 40
        height = int(bounds["height"]) // 40
    except (KeyError, TypeError, ValueError):
        return ""
    return f"{left},{top},{width},{height}"


def bound_device_log_payload(
    *,
    raw_text: str,
    package_name: str | None,
    max_chars: int = _MAX_DEVICE_LOG_CHARS,
) -> dict[str, Any]:
    """有界 device_log 制品；正文截断并保留 checksum。"""
    text = (raw_text or "").replace("\x00", "")
    truncated = len(text) > max_chars
    body = text[-max_chars:] if truncated else text
    return {
        "artifact_kind": "device_log",
        "source": "logcat",
        "package_name": package_name,
        "truncated": truncated,
        "char_count": len(body),
        "checksum": f"sha256:{_sha256_hex(body)}",
        "lines": body.splitlines()[-400:],
        "status": "captured" if body.strip() else "empty",
    }


def bound_network_trace_payload(
    *,
    entries: Sequence[Mapping[str, Any]] | None = None,
    package_name: str | None = None,
    proxy_attached: bool = False,
    raw_summary: str | None = None,
    status: str = "captured",
    reason: str | None = None,
) -> dict[str, Any]:
    """有界 network_trace（对标 Web HAR 的 App 侧网络证据）。"""
    safe_entries: list[dict[str, Any]] = []
    for item in list(entries or [])[:_MAX_NETWORK_ENTRIES]:
        if not isinstance(item, Mapping):
            continue
        safe_entries.append(
            {
                "method": str(item.get("method") or "")[:16] or None,
                "url_host": str(item.get("url_host") or item.get("host") or "")[:200] or None,
                "path": str(item.get("path") or "")[:200] or None,
                "status_code": item.get("status_code"),
                "bytes": item.get("bytes"),
            }
        )
    summary = (raw_summary or "").replace("\x00", "")[:8_000]
    return {
        "artifact_kind": "network_trace",
        "source": "device_network_summary" if not proxy_attached else "session_proxy",
        "package_name": package_name,
        "proxy_attached": proxy_attached,
        "status": status,
        "reason": reason,
        "entry_count": len(safe_entries),
        "entries": safe_entries,
        "summary_text": summary or None,
        "checksum": f"sha256:{_sha256_hex(json.dumps(safe_entries, ensure_ascii=False) + summary)}",
    }


def project_har_from_network_trace(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 App network_trace 投影为 Web 兼容的 har（format=network_events）。

    - 仅 session_proxy 条目可产出真实 response status；dumpsys 摘要不得伪造状态码。
    - 无代理或无条目时仍返回空 entries，作为负证据，供 rate_limit 等合同消费。
    - 永不写入请求/响应 body（与 App MITM 政策一致）。
    """
    source = dict(payload or {})
    proxy_attached = bool(source.get("proxy_attached")) or str(source.get("source") or "") == (
        "session_proxy"
    )
    projected: list[dict[str, Any]] = []
    if proxy_attached:
        raw_entries = source.get("entries")
        items = raw_entries if isinstance(raw_entries, list) else []
        for item in items[:_MAX_NETWORK_ENTRIES]:
            if not isinstance(item, Mapping):
                continue
            method = str(item.get("method") or "GET")[:16] or "GET"
            host = str(item.get("url_host") or item.get("host") or "")[:200]
            path = str(item.get("path") or "")[:200]
            if host and path.startswith("/"):
                url = f"https://{host}{path}"
            elif host:
                url = f"https://{host}/{path.lstrip('/')}" if path else f"https://{host}"
            else:
                url = path or "app://unknown"
            projected.append(
                {
                    "event": "request",
                    "method": method,
                    "url": url[:500],
                    "resource_type": "xhr",
                }
            )
            status_code = item.get("status_code")
            if isinstance(status_code, bool) or not isinstance(status_code, int):
                continue
            if not 100 <= status_code <= 599:
                continue
            projected.append(
                {
                    "event": "response",
                    "url": url[:500],
                    "status": status_code,
                }
            )
    return {
        "format": "network_events",
        "entries": projected,
        "projected_from": "network_trace",
        "proxy_attached": proxy_attached,
        "network_trace_source": source.get("source"),
        "network_trace_status": source.get("status"),
        "package_name": source.get("package_name"),
    }


DEFAULT_APP_TIMED_CAPTURE_FRAMES = 3
DEFAULT_APP_TIMED_CAPTURE_INTERVAL_MS = 1000
STARTUP_APP_TIMED_CAPTURE_FRAMES = 12
# 弱网近似：无云机限速 API 时，用更长间隔连拍 + settle，并显式标注非真限速
WEAK_NETWORK_APPROX_FRAMES = 8
WEAK_NETWORK_APPROX_INTERVAL_MS = 2000
WEAK_NETWORK_APPROX_SETTLE_MS = 1500
MAX_APP_TIMED_CAPTURE_FRAMES = 100
MAX_APP_TIMED_CAPTURE_INTERVAL_MS = 60_000
_MAX_TEMPORAL_VISIBLE_TEXT = 20_000


def resolve_app_timed_capture_params(
    *,
    profile_id: str,
    requested_artifact_types: set[str] | frozenset[str],
) -> tuple[int, int] | None:
    """若请求 temporal_trace，返回 (frame_count, interval_ms)；否则 None。"""
    if "temporal_trace" not in requested_artifact_types:
        return None
    frames = DEFAULT_APP_TIMED_CAPTURE_FRAMES
    interval_ms = DEFAULT_APP_TIMED_CAPTURE_INTERVAL_MS
    pid = str(profile_id or "").casefold()
    if any(
        token in pid
        for token in ("weak_network", "loading_approx", "line_loading")
    ):
        frames = WEAK_NETWORK_APPROX_FRAMES
        interval_ms = WEAK_NETWORK_APPROX_INTERVAL_MS
    elif any(token in pid for token in ("startup", "splash", "countdown")):
        frames = STARTUP_APP_TIMED_CAPTURE_FRAMES
    elif "popup" in pid:
        frames = max(frames, 4)
    frames = max(1, min(int(frames), MAX_APP_TIMED_CAPTURE_FRAMES))
    interval_ms = max(0, min(int(interval_ms), MAX_APP_TIMED_CAPTURE_INTERVAL_MS))
    return frames, interval_ms


def resolve_app_timed_capture_settle_ms(*, profile_id: str) -> int:
    """连拍前等待毫秒；仅弱网近似档案使用，不得冒充厂商限速。"""

    pid = str(profile_id or "").casefold()
    if any(
        token in pid
        for token in ("weak_network", "loading_approx", "line_loading")
    ):
        return WEAK_NETWORK_APPROX_SETTLE_MS
    return 0


def build_app_timed_capture_audit(
    *,
    profile_id: str,
    frame_count: int,
    requested_interval_ms: int,
    produced_offsets_ms: Sequence[float],
    settle_delay_ms: int = 0,
    proxy_shape: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """组装 timed_capture 审计字段；弱网近似必须标明是否真有会话代理塑形。"""

    pid = str(profile_id or "").casefold()
    is_weak_approx = any(
        token in pid
        for token in ("weak_network", "loading_approx", "line_loading")
    )
    shape = dict(proxy_shape or {})
    proxy_applied = shape.get("network_throttle_applied") is True
    payload: dict[str, Any] = {
        "frame_count": int(frame_count),
        "requested_interval_ms": int(requested_interval_ms),
        "produced_offsets_ms": [float(item) for item in produced_offsets_ms],
        # 仅会话 HTTP 代理塑形成功时为 true；冷启动连拍不得冒充设备 tc
        "network_throttle_applied": bool(proxy_applied),
    }
    if settle_delay_ms > 0:
        payload["settle_delay_ms"] = int(settle_delay_ms)
    if proxy_applied:
        payload["throttle_kind"] = str(shape.get("throttle_kind") or "session_http_proxy")
        payload["proxy_shape_latency_ms"] = int(shape.get("latency_ms") or 0)
        payload["throttle_scope"] = str(shape.get("scope") or "http_proxy_traffic_only")
    if is_weak_approx:
        payload["approximation"] = (
            "session_http_proxy_shape"
            if proxy_applied
            else "cold_start_or_delayed_window"
        )
        payload["weak_network_approx"] = True
    elif any(token in pid for token in ("startup", "splash", "countdown")):
        payload["approximation"] = "cold_start_or_startup_window"
    return payload


def build_app_temporal_trace(
    *,
    samples: Sequence[Mapping[str, Any]],
    requested_interval_ms: int,
    package_name: str | None = None,
    activity: str | None = None,
) -> dict[str, Any]:
    """组装与 H5/Web 同形的 App temporal_trace（clock=monotonic）。"""
    safe_samples: list[dict[str, Any]] = []
    last_offset = -1.0
    for index, raw in enumerate(list(samples)[:MAX_APP_TIMED_CAPTURE_FRAMES]):
        if not isinstance(raw, Mapping):
            continue
        try:
            offset_ms = float(raw.get("offset_ms"))
        except (TypeError, ValueError) as exc:
            raise ValueError("temporal_trace.offset_ms 必须为有限数值") from exc
        if not math.isfinite(offset_ms) or offset_ms < 0:
            raise ValueError("temporal_trace.offset_ms 必须为非负有限数值")
        if offset_ms <= last_offset:
            raise ValueError("temporal_trace.offset_ms 必须严格递增")
        last_offset = offset_ms
        text = str(raw.get("visible_text") or "")[:_MAX_TEMPORAL_VISIBLE_TEXT]
        sample: dict[str, Any] = {
            "index": index,
            "offset_ms": offset_ms,
            "visible_text": text,
            "visible_text_truncated": len(str(raw.get("visible_text") or ""))
            > _MAX_TEMPORAL_VISIBLE_TEXT,
        }
        checksum = raw.get("image_sha256")
        if isinstance(checksum, str) and checksum.strip():
            sample["image_sha256"] = checksum.strip()[:80]
        safe_samples.append(sample)
    if len(safe_samples) < 1:
        raise ValueError("temporal_trace.samples 不能为空")
    return {
        "schema_version": "1.0",
        "clock": "monotonic",
        "requested_interval_ms": int(requested_interval_ms),
        "samples": safe_samples,
        "countdown_events": [],
        "surface": "app",
        "package_name": package_name,
        "activity": activity,
    }


def bound_fault_artifact_payload(
    *,
    raw_text: str,
    package_name: str | None = None,
    fault_kind: str = "unknown",
    status: str = "captured",
    reason: str | None = None,
) -> dict[str, Any]:
    """crash / ANR 有界故障制品。"""
    text = (raw_text or "").replace("\x00", "")
    truncated = len(text) > _MAX_FAULT_CHARS
    body = text[:_MAX_FAULT_CHARS]
    kind = fault_kind if fault_kind in {"crash", "anr", "unknown", "none"} else "unknown"
    detected = kind in {"crash", "anr"} and bool(body.strip())
    return {
        "artifact_kind": "fault_artifact",
        "fault_kind": kind if detected else ("none" if status == "captured" else kind),
        "package_name": package_name,
        "status": status,
        "reason": reason,
        "detected": detected,
        "truncated": truncated,
        "checksum": f"sha256:{_sha256_hex(body)}",
        "excerpt": body,
    }


def detect_fault_kind(text: str) -> str:
    """从 dropbox / traces 摘要判断故障类型。"""
    lowered = (text or "").casefold()
    if "anr in" in lowered or "am_anr" in lowered or "application not responding" in lowered:
        return "anr"
    if (
        "fatal exception" in lowered
        or "crash_exception" in lowered
        or "tombstone" in lowered
        or "signal 11" in lowered
        or "force finishing" in lowered
    ):
        return "crash"
    return "unknown"


def _parse_bounds(raw: str | None) -> dict[str, Any] | None:
    match = _BOUNDS_RE.fullmatch((raw or "").strip())
    if not match:
        return None
    left, top, right, bottom = (int(v) for v in match.groups())
    if right <= left or bottom <= top:
        return None
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": right - left,
        "height": bottom - top,
        "signature": f"{left},{top},{right},{bottom}",
    }


def _is_safe_skip_bounds(
    bounds: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
) -> bool:
    left = int(bounds["left"])
    top = int(bounds["top"])
    right = int(bounds["right"])
    bottom = int(bounds["bottom"])
    if left < 0 or top < 0 or right > screen_width or bottom > screen_height:
        return False
    width_ratio = (right - left) / screen_width
    height_ratio = (bottom - top) / screen_height
    area_ratio = width_ratio * height_ratio
    return (
        left >= screen_width * 0.6
        and right >= screen_width * 0.8
        and top <= screen_height * 0.2
        and bottom <= screen_height * 0.3
        and width_ratio <= 0.4
        and height_ratio <= 0.2
        and area_ratio <= 0.05
    )


def _first_attr(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        value = (element.attrib.get(name) or "").strip()
        if value:
            return value
    return None


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
