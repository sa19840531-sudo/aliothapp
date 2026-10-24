"""H5 图片型年龄确认门禁的确定性结构合同。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

H5_AGE_VERIFICATION_TARGET = "年满进入"
H5_AGE_VERIFICATION_PURPOSE = "确认已成年并进入当前 H5 业务页面"
H5_AGE_VERIFICATION_TARGET_MODE = "age_verification_confirm"
H5_AGE_VERIFICATION_CAPABILITY = "h5_age_verification_confirm.v1"
H5_AGE_VERIFICATION_PAGE_STATE = "age_verification_blocking"
H5_AGE_VERIFICATION_KEYWORDS = frozenset(
    {"age_verification", "adult_confirm", "evidence:structure"}
)

_POPUP_CLASS = "age-verify-popup"
_CONTENT_CLASS = "age-verify-content"
_BUTTONS_CLASS = "age-verify-buttons"
_CONFIRM_CLASS = "btn-confirm"
_EXIT_CLASS = "btn-exit"
_BACKGROUND_CLASS = "age-verify-bg"
_BACKGROUND_SOURCE_MARKER = "eighteenyearsbg"
# 文字型年龄门禁：同族站点换皮后不再用整张背景图，改为 role=dialog 容器 +
# 两个文案按钮（实测 91吃瓜 air.mykxxsyu.cc）。图片型判据一条都不中，
# 门禁签发不出来，page_state 停在 popup，整站只剩首页一帧。
_TEXT_OVERLAY_CLASS = "age-verify-overlay"
_TEXT_MODAL_CLASS = "age-verify-modal"
_TEXT_ACTIONS_CLASS = "age-verify-actions"
_TEXT_CONFIRM_CLASS = "age-verify-btn-enter"
_TEXT_EXIT_CLASS = "age-verify-btn-leave"
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _AgeVerificationStructureParser(HTMLParser):
    """只统计完整门禁祖先链下的确认、退出和背景图结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[frozenset[str]] = []
        self.confirm_count = 0
        self.exit_count = 0
        self.background_count = 0
        self.text_confirm_count = 0
        self.text_exit_count = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {str(name).casefold(): str(value or "") for name, value in attrs}
        classes = frozenset(attributes.get("class", "").split())
        ancestor_classes = frozenset().union(*self._stack) if self._stack else frozenset()
        if tag.casefold() == "div" and {
            _POPUP_CLASS,
            _CONTENT_CLASS,
            _BUTTONS_CLASS,
        }.issubset(ancestor_classes):
            if _CONFIRM_CLASS in classes:
                self.confirm_count += 1
            if _EXIT_CLASS in classes:
                self.exit_count += 1
        if tag.casefold() == "button" and {
            _TEXT_OVERLAY_CLASS,
            _TEXT_MODAL_CLASS,
            _TEXT_ACTIONS_CLASS,
        }.issubset(ancestor_classes):
            if _TEXT_CONFIRM_CLASS in classes:
                self.text_confirm_count += 1
            if _TEXT_EXIT_CLASS in classes:
                self.text_exit_count += 1
        if (
            tag.casefold() == "img"
            and _BACKGROUND_CLASS in classes
            and {_POPUP_CLASS, _CONTENT_CLASS}.issubset(ancestor_classes)
            and _BACKGROUND_SOURCE_MARKER in attributes.get("src", "").casefold()
        ):
            self.background_count += 1
        if tag.casefold() not in _VOID_TAGS:
            self._stack.append(classes)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() not in _VOID_TAGS and self._stack:
            self._stack.pop()


def h5_dom_has_supported_age_verification_gate(dom: str) -> bool:
    """确认 DOM 中存在唯一且完整的图片型年龄门禁结构。"""
    parser = _AgeVerificationStructureParser()
    try:
        parser.feed(dom)
        parser.close()
    except Exception:  # noqa: BLE001
        return False
    image_gate = (
        parser.confirm_count == 1
        and parser.exit_count == 1
        and parser.background_count == 1
    )
    # 文字型没有背景图可验；改由「唯一进入 + 唯一离开」成对证明，
    # 缺任意一侧都不签发，避免把普通营销弹窗当成年龄门。
    text_gate = parser.text_confirm_count == 1 and parser.text_exit_count == 1
    return image_gate or text_gate


def h5_snapshot_age_verification_confirm_selectors(
    snapshot: Mapping[str, Any],
) -> tuple[str, ...]:
    """从封存 Snapshot 中返回完整门禁内唯一可点击的成年确认 selector。"""
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    confirm_selectors: set[str] = set()
    text_confirm_selectors: set[str] = set()
    exit_count = 0
    text_exit_count = 0
    background_count = 0
    for raw in elements:
        if not isinstance(raw, Mapping):
            continue
        classes = _tokens(raw.get("class_tokens"))
        ancestors = _tokens(raw.get("ancestor_class_tokens"))
        if (
            str(raw.get("tag") or "").casefold() == "div"
            and {_POPUP_CLASS, _CONTENT_CLASS, _BUTTONS_CLASS}.issubset(ancestors)
            and _snapshot_element_is_clickable(raw)
        ):
            if _CONFIRM_CLASS in classes:
                selector = str(raw.get("selector") or "").strip()
                if selector:
                    confirm_selectors.add(selector)
            if _EXIT_CLASS in classes:
                exit_count += 1
        if (
            str(raw.get("tag") or "").casefold() == "button"
            and {
                _TEXT_OVERLAY_CLASS,
                _TEXT_MODAL_CLASS,
                _TEXT_ACTIONS_CLASS,
            }.issubset(ancestors)
            and _snapshot_element_is_clickable(raw)
        ):
            if _TEXT_CONFIRM_CLASS in classes:
                selector = str(raw.get("selector") or "").strip()
                if selector:
                    text_confirm_selectors.add(selector)
            if _TEXT_EXIT_CLASS in classes:
                text_exit_count += 1
        if (
            str(raw.get("tag") or "").casefold() == "img"
            and _BACKGROUND_CLASS in classes
            and {_POPUP_CLASS, _CONTENT_CLASS}.issubset(ancestors)
            and _BACKGROUND_SOURCE_MARKER in str(raw.get("src") or "").casefold()
        ):
            background_count += 1
    if confirm_selectors and exit_count == 1 and background_count == 1:
        return tuple(sorted(confirm_selectors))
    if text_confirm_selectors and text_exit_count == 1:
        return tuple(sorted(text_confirm_selectors))
    return _semantic_age_gate_selectors(snapshot, elements)


def _semantic_age_gate_selectors(snapshot: Mapping[str, Any], elements: list) -> tuple[str, ...]:
    """完整 H5 同帧弹窗内，唯一成年进入与未成年离开成对出现才签发。"""
    if snapshot.get("h5_modal_census_truncated") is not False or any(
        snapshot.get(key) for key in (
            "truncated", "element_limit_reached", "budget_truncated",
            "capture_degraded", "collection_error",
        )
    ):
        return ()
    elements = [item for item in elements if isinstance(item, Mapping)]
    enters = []
    leaves = []
    for item in elements:
        if not _snapshot_element_is_clickable(item):
            continue
        text = re.sub(r"\s+", "", str(item.get("text") or ""))
        if re.fullmatch(r"(?:我|本人)?(?:已)?年满(?:18|十八)岁[-，,：:·]*(?:进入|继续|访问)", text):
            enters.append(item)
        elif re.fullmatch(r"(?:我|本人)?(?:未|不)满(?:18|十八)岁[-，,：:·]*(?:离开|退出)", text):
            leaves.append(item)
    if len(enters) != 1 or len(leaves) != 1:
        return ()
    enter, leave = enters[0], leaves[0]
    common_layers = set(enter.get("h5_modal_ancestor_selectors") or []) & set(
        leave.get("h5_modal_ancestor_selectors") or []
    )
    modals = [item for item in elements if item.get("selector") in common_layers
              and (item.get("role") == "dialog" or item.get("aria_modal") is True)]
    for modal in modals:
        members = [item for item in elements if modal.get("selector") in (
            item.get("h5_modal_ancestor_selectors") or []
        )]
        if any(item.get("tag") in {"input", "select", "textarea", "form"}
               or item.get("role") in {"checkbox", "radio", "switch", "textbox"}
               for item in members):
            continue
        if all(_safe_semantic_age_control(item, modal) for item in (enter, leave)):
            return (str(enter["selector"]),)
    return ()


def _safe_semantic_age_control(item: Mapping[str, Any], modal: Mapping[str, Any]) -> bool:
    if (not item.get("selector") or item.get("tag") in {"a", "input", "select", "textarea"}
        or (item.get("tag") == "button" and item.get("type") != "button")
        or item.get("navigation_payload_in_subtree") is True
        or str(item.get("aria_disabled") or "").casefold() == "true"
        or any(item.get(key) for key in (
            "href", "data_href", "data_url", "data_uri", "router_to",
        ))):
        return False
    style = item.get("style") or {}
    if style.get("display") == "none" or style.get("visibility") in {"hidden", "collapse"}:
        return False
    try:
        rect, outer = item["rect"], modal["rect"]
        values = [float(rect.get(key, 0)) for key in ("x", "y", "width", "height")]
        bounds = [float(outer.get(key, 0)) for key in ("x", "y", "width", "height")]
        if not all(math.isfinite(value) for value in [*values, *bounds]):
            return False
        x, y, width, height = values
        left, top, outer_width, outer_height = bounds
        return (float(style.get("opacity", 1)) > 0 and style.get("pointer_events") != "none"
                and width > 0 and height > 0
                and left <= x + width / 2 <= left + outer_width
                and top <= y + height / 2 <= top + outer_height)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _tokens(value: object) -> frozenset[str]:
    if not isinstance(value, list | tuple | set | frozenset):
        return frozenset()
    return frozenset(str(item).strip() for item in value if str(item).strip())


def _snapshot_element_is_clickable(raw: Mapping[str, Any]) -> bool:
    hit_test = raw.get("hit_test")
    return bool(
        raw.get("interactive") is True
        and raw.get("disabled") is not True
        and isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
    )
