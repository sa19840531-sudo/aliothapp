"""App 业务页面名称、分组与展示身份。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from services.api.app.domain.exploration.app_visual_page_identity import (
    validated_app_visual_page_identity,
)
from services.api.app.domain.protocol import stable_protocol_hash

APP_VISUAL_PAGE_NAME_FACT_KEY = "visual_page_name"
APP_VISUAL_PAGE_NAME_PROTOCOL_VERSION = "app-visual-page-name.v1"
APP_BUSINESS_PAGE_GROUP_PROTOCOL_VERSION = "app-business-page-group.v3"
# v6：覆盖核心身份优先吃已确认的 app_visual_page_identity（不含 page_type）；
# 页型只留在分组展示与 covered_page_types 分母，避免页型改判导致身份漂移。
APP_BUSINESS_PAGE_IDENTITY_VERSION = "app-business-page-identity.v6"

_SUPPORTED_PAGE_NAME_SOURCES = frozenset(
    {
        "top_bar",
        "selected_tab",
        "navigation_entry",
        "page_landmark",
        "visual_inference",
        "trusted_accessibility",
        "trusted_visual_navigation",
    }
)
_MIN_IDENTITY_CONFIDENCE = 0.55
_SPACE_RE = re.compile(r"\s+")
_EDGE_PUNCTUATION = " \t\r\n-–—_:：,，。;；|/\\"
_GENERIC_HOME_PAGE_NAMES = frozenset(
    {
        "主页",
        "首页",
        "漫画主页",
        "漫画首页",
    }
)
_GENERIC_DETAIL_PAGE_NAMES = frozenset(
    {
        "详情",
        "内容详情",
        "漫画详情",
        "帖子详情",
        "视频详情",
    }
)
_DETAIL_PAGE_TYPES = frozenset({"content_detail", "video_detail"})


def build_visual_page_name_fact(
    *,
    page_name: object,
    source: object = None,
    confidence: object = None,
) -> dict[str, object] | None:
    """规范化视觉模型返回的页面名称；无可靠名称时保持缺失。"""

    normalized_name = normalize_app_page_name(page_name)
    if normalized_name is None:
        return None
    normalized_source = str(source or "visual_inference").strip().casefold()
    if normalized_source not in _SUPPORTED_PAGE_NAME_SOURCES:
        normalized_source = "visual_inference"
    normalized_confidence = _confidence(confidence, default=0.65)
    return {
        "protocol_version": APP_VISUAL_PAGE_NAME_PROTOCOL_VERSION,
        "page_name": normalized_name,
        "source": normalized_source,
        "confidence": normalized_confidence,
    }


def normalize_app_page_name(value: object) -> str | None:
    """生成适合页面分组的短名称，过滤换行、分隔符和不可见字符。"""

    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(character for character in normalized if character.isprintable())
    normalized = _SPACE_RE.sub(" ", normalized).strip(_EDGE_PUNCTUATION)
    normalized = normalized.replace("|", " ")
    normalized = _SPACE_RE.sub(" ", normalized).strip()
    if not normalized:
        return None
    return normalized[:32]


def canonicalize_app_page_name(
    page_name: str | None,
    *,
    page_type: str | None,
) -> str | None:
    """仅合并无业务分支含义的首页同义名。

    「萝莉岛」、「情涩AI」、「种子」等真实页签即使页型相同也必须保留；
    只处理视觉模型在同一 home 页上交替输出「首页/漫画首页」的别名漂移。
    """

    normalized_name = normalize_app_page_name(page_name)
    normalized_type = str(page_type or "").strip().casefold()
    if normalized_type == "home" and normalized_name in _GENERIC_HOME_PAGE_NAMES:
        return "首页"
    return normalized_name


def resolve_app_page_name(
    surface_facts: Mapping[str, Any],
    *,
    page_type: str | None = None,
) -> tuple[str | None, str | None, float | None]:
    """读取页面名称；首页内层页签优先保留，其他根页面受信导航优先。"""

    trusted_name, trusted_page_type, trusted_source = _trusted_selected_navigation_page_name(
        surface_facts
    )
    normalized_page_type = str(page_type or "").strip().casefold()
    raw_fact = surface_facts.get(APP_VISUAL_PAGE_NAME_FACT_KEY)
    raw_page_name: str | None = None
    raw_confidence: float | None = None
    raw_source = ""
    if isinstance(raw_fact, Mapping):
        raw_page_name = normalize_app_page_name(raw_fact.get("page_name"))
        raw_confidence = _optional_confidence(raw_fact.get("confidence"))
        raw_source = str(raw_fact.get("source") or "").strip().casefold()
    if _is_reliable_inner_home_tab_name(
        surface_facts=surface_facts,
        page_type=normalized_page_type,
        page_name=raw_page_name,
        source=raw_source,
        confidence=raw_confidence,
        trusted_root_name=trusted_name,
        trusted_root_page_type=trusted_page_type,
    ):
        return raw_page_name, raw_source, raw_confidence

    stable_navigation_types = {
        "home",
        "content_list",
        "video_list",
        "post_list",
        "activity",
        "profile",
    }
    if (
        trusted_name is not None
        and normalized_page_type in stable_navigation_types
        and trusted_page_type == normalized_page_type
    ):
        return trusted_name, trusted_source, 1.0

    if (
        raw_page_name is not None
        and raw_confidence is not None
        and raw_confidence >= _MIN_IDENTITY_CONFIDENCE
    ):
        return raw_page_name, raw_source or "visual_inference", raw_confidence

    if trusted_name is not None:
        return trusted_name, trusted_source, 1.0
    return None, None, None


def _is_reliable_inner_home_tab_name(
    *,
    surface_facts: Mapping[str, Any],
    page_type: str,
    page_name: str | None,
    source: str,
    confidence: float | None,
    trusted_root_name: str | None,
    trusted_root_page_type: str | None,
) -> bool:
    """识别首页内部已选页签，避免被底部根导航名称合并。"""

    if (
        page_type != "home"
        or source != "selected_tab"
        or page_name is None
        or page_name in _GENERIC_HOME_PAGE_NAMES
        or confidence is None
        or confidence < 0.85
        or trusted_root_name is None
        or trusted_root_page_type != "home"
        or page_name == trusted_root_name
    ):
        return False
    controls = surface_facts.get("trusted_accessibility_controls")
    navigation = controls.get("bottom_navigation") if isinstance(controls, Mapping) else None
    if not isinstance(navigation, (list, tuple)):
        return False
    root_labels = {
        name
        for item in navigation
        if isinstance(item, Mapping)
        if (name := normalize_app_page_name(item.get("label"))) is not None
    }
    return trusted_root_name in root_labels and page_name not in root_labels


def _trusted_selected_navigation_page_name(
    surface_facts: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """从唯一像素高亮或 Accessibility selected 导航项读取页面名称。"""

    controls = surface_facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        selected_page_type = (
            str(
                controls.get("pixel_selected_page_type") or controls.get("selected_page_type") or ""
            )
            .strip()
            .casefold()
            or None
        )
        selected_labels = controls.get("pixel_selected_labels")
        if isinstance(selected_labels, (list, tuple)):
            labels = tuple(
                name
                for value in selected_labels
                if (name := normalize_app_page_name(value)) is not None
            )
            if len(set(labels)) == 1:
                return labels[0], selected_page_type, "trusted_accessibility"
        navigation = controls.get("bottom_navigation")
        if isinstance(navigation, (list, tuple)):
            labels = tuple(
                name
                for item in navigation
                if isinstance(item, Mapping) and item.get("selected") is True
                if (name := normalize_app_page_name(item.get("label"))) is not None
            )
            if len(set(labels)) == 1:
                selected_item_type = (
                    next(
                        (
                            str(item.get("page_type") or "").strip().casefold()
                            for item in navigation
                            if isinstance(item, Mapping) and item.get("selected") is True
                        ),
                        "",
                    )
                    or None
                )
                return (
                    labels[0],
                    selected_item_type or selected_page_type,
                    "trusted_accessibility",
                )
    visual_selection = surface_facts.get("trusted_visual_navigation_selection")
    if isinstance(visual_selection, Mapping):
        contract_id = str(visual_selection.get("contract_id") or "").strip()
        page_type = str(visual_selection.get("selected_page_type") or "").strip().casefold()
        label = normalize_app_page_name(visual_selection.get("selected_label"))
        if contract_id == "app-visual-navigation-selection.v1" and page_type and label is not None:
            return label, page_type, "trusted_visual_navigation"
    return None, None, None


def build_app_business_page_group(
    *,
    package_name: str | None,
    activity: str | None,
    page_type: str | None,
    surface_facts: Mapping[str, Any],
    navigation_depth: int,
    page_scope: str,
    capture_id: str,
    navigation_entry: str | None = None,
    image_signature: str | None = None,
    screenshot_artifact_id: str | None = None,
) -> dict[str, object]:
    """生成页面实例分组；页型仍独立用于规则和 Completion Gate，不进入覆盖核心身份。"""

    package = (package_name or "unknown").strip().casefold()
    normalized_activity = (activity or "unknown").strip().casefold()
    normalized_page_type = (page_type or "unknown").strip().casefold()
    normalized_scope = page_scope.strip().casefold() or "unknown"
    page_name, page_name_source, page_name_confidence = resolve_app_page_name(
        surface_facts,
        page_type=normalized_page_type,
    )
    page_name = canonicalize_app_page_name(
        page_name,
        page_type=normalized_page_type,
    )
    entry_page_name = _detail_name_from_navigation_entry(
        navigation_entry,
        page_type=normalized_page_type,
    )
    if entry_page_name is not None and (
        page_name is None or page_name in _GENERIC_DETAIL_PAGE_NAMES
    ):
        page_name = entry_page_name
        page_name_source = "navigation_entry"
        page_name_confidence = 1.0
    # page_group_key 是兼容已有截图聚合的展示分组，继续保留已确认名称与页型细分；
    # business_page_identity 才是探索循环判断「是否回到同一业务页」的核心身份。
    stable_identity: dict[str, object] = {
        "platform": "app",
        "package_name": package,
        "activity": normalized_activity,
        "page_type": normalized_page_type,
        "page_name": (page_name or "").casefold(),
    }
    if page_name is None:
        # 历史视觉事实没有正式页面名称时沿用原作用域，避免错误合并。
        stable_identity.update(
            {
                "navigation_depth": max(navigation_depth, 0),
                "page_scope": normalized_scope,
            }
        )
    page_group_key = "app-page-group:" + stable_protocol_hash(stable_identity)
    display_name = f"{page_name or '未命名页面'}-{normalized_page_type}"

    visual_identity = validated_app_visual_page_identity(
        surface_facts.get("app_page_identity"),
        capture_id=capture_id,
        package_name=package_name,
        activity=activity,
        image_signature=image_signature,
        screenshot_artifact_id=screenshot_artifact_id,
    )
    if visual_identity is not None:
        visual_key = str(visual_identity["identity"])
        core_identity = {
            "platform": "app",
            "package_name": package,
            "activity": normalized_activity,
            "visual_identity": visual_key,
            "identity_source": "app_visual_page_identity.v2",
        }
        business_page_identity = f"{package}|{normalized_activity}|vid:{visual_key}"
    else:
        # 无已确认视觉身份时不得用 page_type 当核心键（页型改判会漂）。
        # 仅包+Activity 作兜底；对象区分依赖视觉合同。
        core_identity = {
            "platform": "app",
            "package_name": package,
            "activity": normalized_activity,
            "identity_source": "package_activity_fallback",
        }
        business_page_identity = f"{package}|{normalized_activity}"
    return {
        "protocol_version": APP_BUSINESS_PAGE_GROUP_PROTOCOL_VERSION,
        "page_group_key": page_group_key,
        "page_display_name": display_name,
        "page_name": page_name,
        "page_name_source": page_name_source,
        "page_name_confidence": page_name_confidence,
        "page_type": normalized_page_type,
        "package_name": package,
        "activity": normalized_activity,
        "first_capture_id": capture_id,
        "last_capture_id": capture_id,
        "capture_count": 1,
        "business_page_identity": business_page_identity,
        "business_page_core_key": "app-page-identity:" + stable_protocol_hash(core_identity),
    }


def _detail_name_from_navigation_entry(
    value: object,
    *,
    page_type: str,
) -> str | None:
    """从已验签到达的详情入口提取稳定名称，拒绝泛化按钮与广告入口。"""

    if page_type not in _DETAIL_PAGE_TYPES:
        return None
    normalized = normalize_app_page_name(value)
    if normalized is None:
        return None
    normalized = re.sub(
        r"^(?:帖子|漫画|视频|内容卡片|内容|卡片)[\s_：:\-—]*",
        "",
        normalized,
        flags=re.I,
    ).strip()
    lowered = normalized.casefold()
    if (
        not normalized
        or len(normalized) < 2
        or any(
            marker in lowered
            for marker in (
                "普通内容",
                "内容卡片",
                "详情",
                "detail",
                "返回",
                "按钮",
                "入口",
                "广告",
                "推广",
                "banner",
                "tab",
            )
        )
    ):
        return None
    return normalize_app_page_name(f"{normalized[:24]}详情")


def _confidence(value: object, *, default: float) -> float:
    resolved = _optional_confidence(value)
    return default if resolved is None else resolved


def _optional_confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= resolved <= 1:
        return None
    return resolved
