"""H5 未知样式导航的现场发现与可迁移结构指纹。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

H5_ADAPTIVE_NAVIGATION_PATTERN_KIND = "bottom_navigation"
H5_ADAPTIVE_PAGE_TAB_PATTERN_KIND = "page_tab_navigation"
H5AdaptiveNavigationPatternKind = Literal[
    "bottom_navigation",
    "page_tab_navigation",
]
H5InteractionPatternStatus = Literal[
    "candidate",
    "shadow",
    "trusted",
    "quarantined",
    "deprecated",
]

_MAX_ITEMS = 12
_MAX_LABEL_LENGTH = 32
_ACTIVE_CLASS_TOKENS = frozenset(
    {"active", "checked", "current", "is-active", "is-current", "is-selected", "on", "selected"}
)


@dataclass(frozen=True, slots=True)
class H5AdaptiveNavigationItem:
    """当前 Snapshot 中一个经过动作性和几何校验的导航项。"""

    index: int
    label: str
    selector: str
    active: bool


@dataclass(frozen=True, slots=True)
class H5AdaptiveNavigationPattern:
    """不含 CSS/产品身份的导航组结构模式。"""

    signature: str
    pattern_kind: H5AdaptiveNavigationPatternKind
    pattern_payload: dict[str, object]
    items: tuple[H5AdaptiveNavigationItem, ...]


class H5InteractionPatternKnowledgePort(Protocol):
    """H5 模式知识只负责加速和降级，不能替代现场校验。"""

    async def register_discovered(
        self,
        *,
        patterns: Sequence[H5AdaptiveNavigationPattern],
    ) -> Mapping[str, H5InteractionPatternStatus]: ...

    async def record_outcome(
        self,
        *,
        pattern_signature: str,
        task_id: str,
        page_capture_id: str,
        outcome: Literal["success", "failure"],
        reason_code: str | None = None,
    ) -> H5InteractionPatternStatus | None: ...


def discover_h5_adaptive_navigation_patterns(
    snapshot: Mapping[str, Any] | None,
) -> tuple[H5AdaptiveNavigationPattern, ...]:
    """从当前快照发现未知 CSS 导航组；只使用可见行为事实，不猜产品实现。"""
    if not isinstance(snapshot, Mapping):
        return ()
    viewport = snapshot.get("viewport")
    elements = snapshot.get("elements")
    if not isinstance(viewport, Mapping) or not isinstance(elements, list):
        return ()
    viewport_width = _finite_number(viewport.get("width"))
    viewport_height = _finite_number(viewport.get("height"))
    if (
        viewport_width is None
        or viewport_height is None
        or viewport_width <= 0
        or viewport_height <= 0
    ):
        return ()

    grouped: dict[str, list[tuple[float, float, float, float, Mapping[str, Any], str]]] = (
        defaultdict(list)
    )
    for raw in elements:
        if not isinstance(raw, Mapping) or not _eligible_item(raw):
            continue
        parent_selector = str(
            raw.get("h5_adaptive_navigation_group_selector") or raw.get("parent_selector") or ""
        ).strip()
        selector = str(raw.get("selector") or "").strip()
        label = _navigation_label(raw)
        rect = _rect(raw)
        if not parent_selector or not selector or not label or rect is None:
            continue
        x, y, width, height = rect
        if (
            width < 20
            or height < 20
            or width > viewport_width * 0.5
            or height > min(160, viewport_height * 0.22)
            or y >= viewport_height
            or y + height > viewport_height + 4
        ):
            continue
        grouped[parent_selector].append((x, y, width, height, raw, label))

    patterns: list[H5AdaptiveNavigationPattern] = []
    for raw_group in grouped.values():
        group = _validated_group(
            raw_group,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )
        if group is not None:
            patterns.append(group)
    return tuple(
        sorted(
            patterns,
            key=lambda item: (-len(item.items), item.signature),
        )
    )


def adaptive_navigation_selector(
    snapshot: Mapping[str, Any],
    *,
    pattern_signature: str,
    navigation_index: int,
    navigation_label: str,
    allow_page_tab_reflow: bool = False,
) -> str | None:
    """在实时快照中按模式、位置和标签重新定位，绝不返回历史 selector。"""
    if not re.fullmatch(r"[0-9a-f]{64}", pattern_signature):
        return None
    expected_label = _normalized_text(navigation_label)
    patterns = discover_h5_adaptive_navigation_patterns(snapshot)
    matches = [
        item.selector
        for pattern in patterns
        if pattern.signature == pattern_signature
        for item in pattern.items
        if item.index == navigation_index and _normalized_text(item.label) == expected_label
    ]
    if len(matches) == 1:
        return matches[0]
    if matches or not allow_page_tab_reflow:
        return None
    # 页内 Tab 滑动后布局指纹可能变化；只在当前语义导航组中唯一时复核目标。
    fallback = [
        item.selector
        for pattern in patterns
        if pattern.pattern_kind == H5_ADAPTIVE_PAGE_TAB_PATTERN_KIND
        for item in pattern.items
        if item.index == navigation_index and _normalized_text(item.label) == expected_label
    ]
    return fallback[0] if len(fallback) == 1 else None


def _validated_group(
    raw_group: list[tuple[float, float, float, float, Mapping[str, Any], str]],
    *,
    viewport_width: float,
    viewport_height: float,
) -> H5AdaptiveNavigationPattern | None:
    ordered = sorted(raw_group, key=lambda item: (item[0] + item[2] / 2, item[1]))
    if not 2 <= len(ordered) <= _MAX_ITEMS:
        return None
    semantic_navigation = all(item[4].get("within_navigation") is True for item in ordered)
    average_center_y = sum(item[1] + item[3] / 2 for item in ordered) / len(ordered)
    bottom_navigation = average_center_y >= viewport_height * 0.68
    if not bottom_navigation and not (
        semantic_navigation
        and all(_item_has_navigation_semantics(item[4]) for item in ordered)
        and (
            any(_item_active(item[4]) for item in ordered)
            or all(str(item[4].get("role") or "").casefold() == "tab" for item in ordered)
        )
    ):
        # 页内栏目不能只凭水平排列猜测，必须由当前 DOM 同时证明
        # 它位于导航语义容器内，且每个项都具有链接或 Tab 语义。
        return None
    if len(ordered) == 2 and not semantic_navigation:
        # 固定在底部的双按钮通常是确认/取消操作区；只有显式导航语义才能
        # 把双项结构签发为全局导航，避免将业务 CTA 当成页面树入口。
        return None
    if not semantic_navigation and not (
        any(_item_active(item[4]) for item in ordered)
        or all(_item_has_navigation_semantics(item[4]) for item in ordered)
    ):
        # 纯几何只能证明“底部工具栏”，不能证明“页面导航”。缺少导航或
        # 当前激活事实时拒绝签发，防止点击发送、分享、购买等副作用动作。
        return None
    labels = [_normalized_text(item[5]) for item in ordered]
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        return None
    centers_x = [item[0] + item[2] / 2 for item in ordered]
    centers_y = [item[1] + item[3] / 2 for item in ordered]
    heights = [item[3] for item in ordered]
    minimum_span = viewport_width * (
        0.35 if len(ordered) == 2 or not bottom_navigation else 0.55
    )
    if (
        max(centers_x) - min(centers_x) < minimum_span
        or max(centers_y) - min(centers_y) > max(24, max(heights) * 0.75)
        or min(item[0] for item in ordered) > viewport_width * 0.25
        or max(item[0] + item[2] for item in ordered) < viewport_width * 0.75
    ):
        return None

    # 外站入口只参与完整导航组的结构校验，不获得可执行导航能力。
    ordered = [item for item in ordered if item[4].get("external") is not True]
    if len(ordered) < 2:
        return None
    centers_x = [item[0] + item[2] / 2 for item in ordered]

    pattern_kind: H5AdaptiveNavigationPatternKind = (
        H5_ADAPTIVE_NAVIGATION_PATTERN_KIND
        if bottom_navigation
        else H5_ADAPTIVE_PAGE_TAB_PATTERN_KIND
    )
    payload: dict[str, object] = {
        "schema_version": "h5-interaction-pattern.v1",
        "pattern_kind": pattern_kind,
        "item_count": len(ordered),
        "roles": [_semantic_role(item[4]) for item in ordered],
        "placement": (
            "page_navigation"
            if pattern_kind == H5_ADAPTIVE_PAGE_TAB_PATTERN_KIND
            else "semantic_navigation"
            if semantic_navigation
            else "fixed_dock"
        ),
        "x_bins": [round(center / viewport_width * 20) for center in centers_x],
    }
    if pattern_kind == H5_ADAPTIVE_NAVIGATION_PATTERN_KIND:
        payload["bottom_band"] = round(average_center_y / viewport_height * 10)
    else:
        payload["vertical_band"] = round(average_center_y / viewport_height * 10)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    items = tuple(
        H5AdaptiveNavigationItem(
            index=index,
            label=raw[5],
            selector=str(raw[4].get("selector") or "").strip(),
            active=_item_active(raw[4]),
        )
        for index, raw in enumerate(ordered, start=1)
    )
    return H5AdaptiveNavigationPattern(
        signature=signature,
        pattern_kind=pattern_kind,
        pattern_payload=payload,
        items=items,
    )


def _eligible_item(raw: Mapping[str, Any]) -> bool:
    hit_test = raw.get("hit_test")
    style = raw.get("style")
    position = str(style.get("position") or "").casefold() if isinstance(style, Mapping) else ""
    has_navigation_placement = bool(
        raw.get("within_navigation") is True
        or raw.get("fixed_or_sticky_ancestor") is True
        or position in {"fixed", "sticky"}
    )
    return bool(
        raw.get("visible") is True
        and raw.get("interactive") is True
        and raw.get("disabled") is not True
        and raw.get("read_only") is not True
        and raw.get("content_editable") is not True
        and raw.get("is_advertisement") is not True
        and isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
        and has_navigation_placement
    )


def _navigation_label(raw: Mapping[str, Any]) -> str:
    for key in ("aria_label", "text", "alt", "title"):
        value = re.sub(r"\s+", " ", str(raw.get(key) or "")).strip()
        if 0 < len(value) <= _MAX_LABEL_LENGTH:
            return value
    return ""


def _item_active(raw: Mapping[str, Any]) -> bool:
    class_tokens = raw.get("class_tokens")
    normalized_classes = (
        {str(item).strip().casefold() for item in class_tokens if str(item).strip()}
        if isinstance(class_tokens, list)
        else set()
    )
    return bool(
        any(_active_class_token(item) for item in normalized_classes)
        or str(raw.get("aria_selected") or "").casefold() == "true"
        or str(raw.get("aria_current") or "").casefold() in {"page", "true"}
    )


def _active_class_token(value: str) -> bool:
    return value in _ACTIVE_CLASS_TOKENS or bool(
        re.search(r"(?:^|[-_])(active|checked|current|selected|on)(?:$|[-_])", value)
    )


def _item_has_navigation_semantics(raw: Mapping[str, Any]) -> bool:
    tag = str(raw.get("tag") or "").strip().casefold()
    role = str(raw.get("role") or "").strip().casefold()
    return bool(
        tag == "a"
        or role in {"link", "menuitem", "tab"}
        or raw.get("navigation_payload_in_subtree") is True
        or any(
            str(raw.get(key) or "").strip()
            for key in ("href", "data_route", "data_href", "data_url", "router_to")
        )
    )


def _semantic_role(raw: Mapping[str, Any]) -> str:
    role = str(raw.get("role") or "").strip().casefold()
    if role:
        return role
    tag = str(raw.get("tag") or "").strip().casefold()
    return "link" if tag == "a" else "button" if tag == "button" else "custom"


def _rect(raw: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    rect = raw.get("rect")
    if not isinstance(rect, Mapping):
        return None
    values = tuple(_finite_number(rect.get(key)) for key in ("x", "y", "width", "height"))
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


__all__ = [
    "H5_ADAPTIVE_NAVIGATION_PATTERN_KIND",
    "H5_ADAPTIVE_PAGE_TAB_PATTERN_KIND",
    "H5AdaptiveNavigationItem",
    "H5AdaptiveNavigationPattern",
    "H5InteractionPatternKnowledgePort",
    "H5InteractionPatternStatus",
    "adaptive_navigation_selector",
    "discover_h5_adaptive_navigation_patterns",
]
