"""H5 受控底部导航的结构族与固定 selector 合同。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping

from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel

H5_BOTTOM_NAVIGATION_FAMILY_VANT = "vant"
H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT = "pages_foot"
H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP = "footer_wrap"
H5_BOTTOM_NAVIGATION_FAMILY_FLUTTER = "flutter_semantics"
H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA = "generic_spa"
H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE = "adaptive_snapshot"

# 自定义 SPA 底栏的检测与实时 Snapshot 复核共同使用同一组结构标记。
# 普通 footer/list item 不在合同内，避免把内容区误签发成全局导航能力。
H5_GENERIC_NAVIGATION_CONTAINER_CLASSES = frozenset(
    {
        "app-nav",
        "bottom-bar",
        "bottom-nav",
        "bottom-navigation",
        "bottom-navbar",
        "bottom-tabbar",
        "bottom-tabs",
        "footer-nav",
        "footer-wrap",
        "main-nav",
        "nav-bar",
        "navbar",
        "navigation-bar",
        "tab-bar",
        "tabbar",
    }
)
H5_GENERIC_NAVIGATION_ITEM_CLASSES = frozenset(
    {
        "bottom-bar-item",
        "bottom-nav-item",
        "bottom-navigation-item",
        "bottom-navbar-item",
        "bottom-tab-item",
        "footer-item",
        "menu-item",
        "nav-item",
        "navigation-item",
        "navbar-item",
        "tab-item",
        "tabbar-item",
        "type-item",
    }
)

_BOTTOM_NAVIGATION_TARGET_TEMPLATES = {
    H5_BOTTOM_NAVIGATION_FAMILY_VANT: (
        "div.van-tabbar:visible > "
        "div.van-tabbar-item:nth-of-type({index}):visible"
    ),
    H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT: (
        "ul.pages-foot-fix:visible > li:nth-of-type({index}):visible"
    ),
    H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP: (
        "div.footer-wrap:visible ul.type:visible > "
        "li.footer-item:nth-of-type({index}):visible"
    ),
    H5_BOTTOM_NAVIGATION_FAMILY_FLUTTER: (
        "internal:flutter_bottom_navigation:nth-of-type({index})"
    ),
    H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA: (
        "internal:spa_bottom_navigation:nth-of-type({index})"
    ),
    H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE: (
        "internal:adaptive_bottom_navigation:nth-of-type({index})"
    ),
}
_BOTTOM_NAVIGATION_TARGET_PATTERNS = tuple(
    (
        family,
        re.compile(
            "^"
            + re.escape(template).replace(
                re.escape("{index}"),
                r"(?P<index>[1-9]\d*)",
            )
            + "$"
        ),
    )
    for family, template in _BOTTOM_NAVIGATION_TARGET_TEMPLATES.items()
)
_FLUTTER_BOTTOM_NAVIGATION_RUNTIME_PATTERN = re.compile(
    r"^internal:flutter_bottom_navigation_item:(?P<index>[1-9]\d*):"
    r"(?P<label_hash>[0-9a-f]{16})$"
)
_PAGE_TAB_TARGET_TEMPLATE = "internal:page_tab:nth-of-type({index})"
_PAGE_TAB_TARGET_PATTERN = re.compile(
    r"^internal:page_tab:nth-of-type\((?P<index>[1-9]\d*)\)$"
)


def h5_bottom_navigation_selector(*, family: str, index: int) -> str:
    """按已知结构族生成唯一、有限的底栏 selector。"""
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 50:
        raise ValueError("底部导航位置必须为 1..50 的整数")
    template = _BOTTOM_NAVIGATION_TARGET_TEMPLATES.get(family)
    if template is None:
        raise ValueError("未知 H5 底部导航结构族")
    return template.format(index=index)


def h5_match_bottom_navigation_target(target: str) -> tuple[str, int] | None:
    """解析且仅解析平台固定生成的底栏 selector。"""
    for family, pattern in _BOTTOM_NAVIGATION_TARGET_PATTERNS:
        matched = pattern.fullmatch(target)
        if matched is not None:
            return family, int(matched.group("index"))
    return None


def h5_page_tab_target(*, index: int) -> str:
    """把平台检测出的页内 Tab 位置编码为不泄露 DOM 的语义目标。"""
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 50:
        raise ValueError("页内 Tab 位置必须为 1..50 的整数")
    return _PAGE_TAB_TARGET_TEMPLATE.format(index=index)


def h5_match_page_tab_target(target: str) -> int | None:
    """解析且仅解析平台签发的页内 Tab 语义目标。"""
    matched = _PAGE_TAB_TARGET_PATTERN.fullmatch(target)
    if matched is None:
        return None
    index = int(matched.group("index"))
    return index if 1 <= index <= 50 else None


def h5_bottom_navigation_family(value: object) -> str | None:
    """兼容旧 Vant 候选：缺少结构族时按历史合同解释。"""
    if value is None:
        return H5_BOTTOM_NAVIGATION_FAMILY_VANT
    if isinstance(value, str) and value in _BOTTOM_NAVIGATION_TARGET_TEMPLATES:
        return str(value)
    return None


def h5_bottom_navigation_action_parameters(
    *,
    family: str,
    index: int,
    label: str,
    interaction_pattern_signature: str | None = None,
) -> dict[str, object]:
    """生成持久化能力参数；默认 Vant 保持历史负载兼容。"""
    parameters: dict[str, object] = {
        "target_mode": "bottom_navigation",
        "navigation_index": index,
        "navigation_label": label,
    }
    if family != H5_BOTTOM_NAVIGATION_FAMILY_VANT:
        parameters["navigation_family"] = family
    if family == H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE:
        if not isinstance(interaction_pattern_signature, str) or not re.fullmatch(
            r"[0-9a-f]{64}", interaction_pattern_signature
        ):
            raise ValueError("H5 交互模式签名必须为 64 位十六进制摘要")
        parameters["interaction_pattern_signature"] = interaction_pattern_signature
    elif interaction_pattern_signature is not None:
        raise ValueError("只有 H5 自适应导航允许携带交互模式签名")
    return parameters


def h5_flutter_bottom_navigation_runtime_selector(*, index: int, label: str) -> str:
    """把已验签的 Flutter 底栏位置与标签摘要编码为执行层内部目标。"""
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 50:
        raise ValueError("Flutter 底部导航位置必须为 1..50 的整数")
    normalized_label = _normalized_navigation_label(label)
    if not normalized_label:
        raise ValueError("Flutter 底部导航标签不能为空")
    label_hash = hashlib.sha256(normalized_label.encode("utf-8")).hexdigest()[:16]
    return f"internal:flutter_bottom_navigation_item:{index}:{label_hash}"


def h5_match_flutter_bottom_navigation_runtime_selector(
    selector: str,
) -> tuple[int, str] | None:
    """解析执行层内部 Flutter 底栏目标，不接受任意 DOM selector。"""
    matched = _FLUTTER_BOTTOM_NAVIGATION_RUNTIME_PATTERN.fullmatch(selector)
    if matched is None:
        return None
    index = int(matched.group("index"))
    if not 1 <= index <= 50:
        return None
    return index, matched.group("label_hash")


def h5_flutter_bottom_navigation_label_hash(label: str) -> str:
    """返回与内部执行目标一致的规范化标签摘要。"""
    normalized_label = _normalized_navigation_label(label)
    return (
        hashlib.sha256(normalized_label.encode("utf-8")).hexdigest()[:16]
        if normalized_label
        else ""
    )


def h5_global_bottom_navigation_index(candidate: object) -> int | None:
    """只解析平台已完整签名的全局底栏候选。"""
    metadata = getattr(candidate, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    parameters = metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return None
    navigation_index = parameters.get("navigation_index")
    navigation_label = parameters.get("navigation_label")
    navigation_family = h5_bottom_navigation_family(
        parameters.get("navigation_family")
    )
    if navigation_family is None:
        return None
    semantic_target = getattr(candidate, "semantic_target", None)
    matched = (
        h5_match_bottom_navigation_target(semantic_target)
        if isinstance(semantic_target, str)
        else None
    )
    keywords = metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    confidence = metadata.get("navigation_knowledge_confidence")
    expected_page_type = metadata.get("expected_next_page_type")
    try:
        expected_parameters = h5_bottom_navigation_action_parameters(
            family=navigation_family,
            index=navigation_index,
            label=navigation_label,
            interaction_pattern_signature=parameters.get("interaction_pattern_signature"),
        )
    except ValueError:
        return None
    if (
        expected_parameters is None
        or dict(parameters) != expected_parameters
        or isinstance(navigation_index, bool)
        or not isinstance(navigation_index, int)
        or not 1 <= navigation_index <= 50
        or not isinstance(navigation_label, str)
        or not navigation_label.strip()
        or matched != (navigation_family, navigation_index)
        or getattr(candidate, "action_type", None) is not InteractionOperation.CLICK
        or getattr(candidate, "risk_level", None) is not InteractionRiskLevel.LOW
        or getattr(candidate, "navigation_depth", None) != 1
        or metadata.get("navigation_knowledge_source")
        not in ("static", "exact", "vector", "contextual")
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0 <= float(confidence) <= 1
        or "bottom_navigation" not in normalized_keywords
        or navigation_label not in normalized_keywords
        or not isinstance(expected_page_type, str)
        or not expected_page_type.strip()
    ):
        return None
    return navigation_index


def _normalized_navigation_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()
