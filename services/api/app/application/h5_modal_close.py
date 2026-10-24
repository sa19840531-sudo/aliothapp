"""不依赖站点 CSS 名称的 H5 顶层弹窗关闭证明。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

H5_MODAL_CLOSE_CAPABILITY = "rendered_modal_close"
H5_MODAL_CLOSE_SELECTOR = "internal:rendered_modal_close"
_CLOSE_LABELS = frozenset({
    "关闭", "关闭弹窗", "关闭广告", "忽略", "跳过", "稍后",
    "close", "dismiss", "skip", "x", "×", "✕", "✖",
})


@dataclass(frozen=True, slots=True)
class H5ModalCloseProof:
    selector: str
    modal_selector: str
    obstruction_signature: str
    outside_position: tuple[float, float] | None = None
    sibling_close_control: bool = False


def h5_modal_close(snapshot: Mapping[str, Any] | None) -> H5ModalCloseProof | None:
    """仅签发完整快照中唯一、可命中、无提交/外跳语义的顶层关闭控件。"""
    if not isinstance(snapshot, Mapping) or any(
        snapshot.get(key)
        for key in (
            "budget_truncated",
            "capture_degraded",
            "collection_error",
            "h5_modal_census_truncated",
        )
    ):
        return None
    # 只有 H5 采集器完成现场弹窗普查后才签发，Web/App 和旧不完整快照不受影响。
    if snapshot.get("h5_modal_census_truncated") is not False:
        return None
    raw = snapshot.get("elements")
    if not isinstance(raw, list):
        return None
    retained = [item for item in raw if isinstance(item, Mapping)]
    elements = [item for item in retained if _rendered(item)]
    census = snapshot.get("h5_modal_census_selectors")
    if snapshot.get("element_limit_reached") or snapshot.get("truncated"):
        # 正文截断不代表弹窗普查截断，但普查成员必须全部保留在本帧。
        selectors = {item.get("selector") for item in retained}
        if not isinstance(census, list) or not census or not set(census).issubset(selectors):
            return None
    area = _area(snapshot.get("viewport"))
    if area <= 0:
        return None
    modals = []
    for item in elements:
        style = item.get("style")
        style = style if isinstance(style, Mapping) else {}
        semantic_modal = item.get("aria_modal") is True or item.get("role") == "dialog"
        full_backdrop = (
            _area(item.get("rect")) / area >= 0.8
            and style.get("position") in {"fixed", "absolute"}
            and _number(style.get("z_index")) >= 100
        )
        regional_dismissal = (
            style.get("position") == "fixed"
            and _area(item.get("rect")) / area >= 0.02
            and any(
                control.get("dismiss_semantics") == "modal"
                and _descends_from(control, item, elements)
                for control in elements
            )
        )
        if (semantic_modal or full_backdrop or regional_dismissal) and item.get("selector"):
            modals.append(item)
    matches = []
    for control in elements:
        if not _close_control(control) or _area(control.get("rect")) / area > 0.2:
            continue
        containing = [
            modal
            for modal in modals
            if (_contains(modal, control) and _descends_from(control, modal, elements))
            or control.get("h5_modal_sibling_panel_selector") == modal.get("selector")
        ]
        if containing:
            matches.append((control, min(containing, key=lambda item: _area(item.get("rect")))))
    if len(matches) > 1:
        # 同一父链的包装层和内部图标只算一个控件；并列独立关闭键仍保持拒绝。
        matches = [pair for pair in matches if not any(
            other[0] is not pair[0]
            and _descends_from(other[0], pair[0], elements)
            for other in matches
        )]
        full_matches = [pair for pair in matches if _area(pair[1].get("rect")) / area >= 0.5]
        if full_matches:
            matches = full_matches
    if len(matches) != 1:
        if not matches and not any(
            item.get("h5_modal_action_semantics") == "acknowledge" and _hittable(item)
            for item in elements
        ):
            for modal in modals:
                outside = modal.get("h5_modal_safe_outside")
                if not isinstance(outside, Mapping):
                    continue
                panel = next((item for item in modals
                              if item.get("selector") == outside.get("panel_selector")), None)
                positions = outside.get("positions")
                if panel is None or not isinstance(positions, list) or not positions:
                    continue
                point = positions[0]
                if not isinstance(point, Mapping):
                    continue
                position = (_number(point.get("x")), _number(point.get("y")))
                rect = modal.get("rect") or {}
                if not (0 < position[0] < _number(rect.get("width"))
                        and 0 < position[1] < _number(rect.get("height"))):
                    continue
                selector, panel_selector = str(modal["selector"]), str(panel["selector"])
                payload = json.dumps([
                    H5_MODAL_CLOSE_CAPABILITY, selector, panel_selector, "outside",
                    *([panel["h5_modal_media_identity"]]
                       if panel.get("h5_modal_media_identity") else []),
                ])
                signature = hashlib.sha256(payload.encode()).hexdigest()
                return H5ModalCloseProof(
                    selector=selector, modal_selector=panel_selector,
                    obstruction_signature="h5-obstruction:" + signature,
                    outside_position=position,
                )
        return None
    control, modal = matches[0]
    selector = str(control.get("selector") or "")
    modal_selector = str(modal.get("selector") or "")
    payload = json.dumps(
        [H5_MODAL_CLOSE_CAPABILITY, selector, modal_selector,
         *([modal["h5_modal_media_identity"]] if modal.get("h5_modal_media_identity") else [])],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return H5ModalCloseProof(
        selector=selector,
        modal_selector=modal_selector,
        obstruction_signature="h5-obstruction:" + hashlib.sha256(payload.encode()).hexdigest(),
        sibling_close_control=control.get("h5_modal_sibling_panel_selector") == modal_selector,
    )


def _close_control(item: Mapping[str, Any]) -> bool:
    if not item.get("selector") or item.get("interactive") is not True or not _hittable(item):
        return False
    if (
        item.get("disabled") is True
        or str(item.get("aria_disabled") or "").casefold() == "true"
        or item.get("navigation_payload_in_subtree") is True
        or item.get("stateful_control_in_subtree") is True
    ):
        return False
    if any(item.get(key) for key in ("href", "data_href", "data_url", "data_uri", "router_to")):
        return False
    tag = str(item.get("tag") or "").casefold()
    if tag == "button" and item.get("type") != "button":
        if item.get("type") not in {None, "submit"} or item.get("form_associated") is not False:
            return False
    if tag in {"input", "select", "textarea", "form", "a"}:
        return False
    # 已有图片关闭模式只补足无标签控件的语义；仍须走完整普查、父链、命中和签名。
    image_close = (
        tag == "div"
        and set(item.get("class_tokens") or []) == {"btn"}
        and {"grid-main", "van-dialog"}.issubset(item.get("ancestor_class_tokens") or [])
        and not str(item.get("text") or "").strip()
        and not str(item.get("aria_label") or "").strip()
    )
    return image_close or item.get("dismiss_semantics") == "modal" or (
        item.get("h5_modal_action_semantics") == "close"
    ) or any(
        str(item.get(key) or "").strip().casefold() in _CLOSE_LABELS
        for key in ("text", "aria_label", "title", "alt")
    )


def _contains(modal: Mapping[str, Any], control: Mapping[str, Any]) -> bool:
    outer, inner = modal.get("rect"), control.get("rect")
    if not isinstance(outer, Mapping) or not isinstance(inner, Mapping):
        return False
    x = _number(inner.get("x")) + _number(inner.get("width")) / 2
    y = _number(inner.get("y")) + _number(inner.get("height")) / 2
    return _number(outer.get("x")) <= x <= _number(outer.get("x")) + _number(
        outer.get("width")
    ) and _number(outer.get("y")) <= y <= _number(outer.get("y")) + _number(outer.get("height"))


def _descends_from(control: Mapping[str, Any], modal: Mapping[str, Any], elements: list) -> bool:
    selector = modal.get("selector")
    if selector in (control.get("h5_modal_ancestor_selectors") or []):
        return True
    by_selector = {item.get("selector"): item for item in elements}
    parent = control.get("parent_selector")
    for _ in range(32):
        if not parent:
            return False
        if parent == selector:
            return True
        parent = by_selector.get(parent, {}).get("parent_selector")
    return False


def _hittable(item: Mapping[str, Any]) -> bool:
    hit = item.get("hit_test")
    return isinstance(hit, Mapping) and hit.get("center_hits_target") is True


def _rendered(item: Mapping[str, Any]) -> bool:
    style = item.get("style")
    style = style if isinstance(style, Mapping) else {}
    return (
        _area(item.get("rect")) > 0
        and style.get("display") != "none"
        and style.get("visibility") not in {"hidden", "collapse"}
        and _number(style.get("opacity", 1)) > 0
        and style.get("pointer_events") != "none"
    )


def _area(value: object) -> float:
    if not isinstance(value, Mapping):
        return 0
    return max(_number(value.get("width")), 0) * max(_number(value.get("height")), 0)


def _number(value: object) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0
    except (TypeError, ValueError):
        return 0
