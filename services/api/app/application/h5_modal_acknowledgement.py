"""H5 顶层信息弹窗的安全确认能力。

能力只依赖当前 UI Snapshot 的实时几何、命中和语义事实，不依赖域名、
站点文案组合或页面类型。任何交易、账户、授权或表单事实都会 fail-closed。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY = (
    "modal_local_safe_acknowledgement"
)
H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR = (
    "internal:modal_local_safe_acknowledgement"
)
_OBSTRUCTION_SIGNATURE_PREFIX = "h5-obstruction:"
_SAFE_ACKNOWLEDGEMENT_LABELS = frozenset(
    {"确认", "确定", "知道了", "我知道了", "我知道", "好的", "ok", "关闭", "稍后"}
)
_RISK_TEXT_MARKERS = (
    "支付",
    "充值",
    "购买",
    "订单",
    "提交",
    "收货",
    "提现",
    "转账",
    "金额",
    "金币",
    "余额",
    "密码",
    "验证码",
    "银行卡",
    "授权",
    "同意",
    "隐私",
    "条款",
    "协议",
    "注册",
    "登录",
    "注销",
    "删除",
    "解绑",
    "绑定",
    "允许",
    "订阅",
    "续费",
    "兑换",
    "领取",
    "保存",
)
_RISK_AMOUNT_PATTERN = re.compile(
    r"(?:[¥￥$€£]\s*\d|\d+(?:\.\d+)?\s*(?:元|美元|金币|积分))",
    flags=re.IGNORECASE,
)
_PROMOTIONAL_GIFT_AMOUNT_PATTERN = re.compile(
    r"(?:赠送|送)\s*\d+(?:\.\d+)?\s*(?:元|美元|金币|积分)",
    flags=re.IGNORECASE,
)
_STATEFUL_TAGS = frozenset({"input", "select", "textarea"})
_STATEFUL_ROLES = frozenset(
    {"checkbox", "radio", "slider", "spinbutton", "switch", "textbox"}
)


@dataclass(frozen=True, slots=True)
class H5ModalAcknowledgementProof:
    """检测与执行可共同复核的安全确认事实。"""

    capability: str
    selector: str
    obstruction_signature: str
    modal_selector: str


def h5_modal_local_safe_acknowledgement(
    snapshot: Mapping[str, Any] | None,
) -> H5ModalAcknowledgementProof | None:
    """验签唯一、可命中的顶层信息弹窗确认控件。"""

    if not isinstance(snapshot, Mapping) or _snapshot_incomplete(snapshot):
        return None
    raw_elements = snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return None
    elements = [item for item in raw_elements if isinstance(item, Mapping)]
    viewport_area = _rect_area(snapshot.get("viewport"))
    if viewport_area <= 0:
        return None

    active_elements = [item for item in elements if _visible_and_center_hittable(item)]
    acknowledgements = [
        item
        for item in active_elements
        if _normalized_text(item.get("text")) in _SAFE_ACKNOWLEDGEMENT_LABELS
        and item.get("interactive") is True
        and (snapshot.get("h5_modal_census_truncated") is not False
             or bool(item.get("h5_modal_ancestor_selectors")))
    ]
    if len(acknowledgements) > 1 and snapshot.get("h5_modal_census_truncated") is False:
        from services.api.app.application.h5_modal_close import _descends_from

        # 同一父链的包装层和文字只保留内层；两个独立确认键仍拒绝验签。
        acknowledgements = [item for item in acknowledgements if not any(
            other is not item and _descends_from(other, item, elements)
            for other in acknowledgements
        )]
    if len(acknowledgements) != 1:
        return None
    acknowledgement = acknowledgements[0]
    informational_acknowledgement = False
    # 新快照按实际弹窗父链评估风险，背景搜索、登录或会员入口不属于确认行为。
    modal_selectors = acknowledgement.get("h5_modal_ancestor_selectors")
    if isinstance(modal_selectors, list) and modal_selectors:
        from services.api.app.application.h5_modal_close import _descends_from, _rendered

        modal = next((item for selector in modal_selectors for item in elements
                      if item.get("selector") == selector), None)
        if modal is None:
            return None
        active_elements = [item for item in elements if _rendered(item) and (
            item is modal or _descends_from(item, modal, elements)
        )]
        if any(_stateful_control(item) for item in active_elements):
            return None
        modal_text = " ".join(_normalized_text(item.get("text")) for item in active_elements)
        # 完整父链中的非承诺确认可关闭维护公告；支付、注册仍不能成为点击行为。
        informational_acknowledgement = bool(
            snapshot.get("h5_modal_census_truncated") is False
            and acknowledgement.get("h5_modal_action_semantics") == "acknowledge"
            and _normalized_text(acknowledgement.get("text")) in {"知道了", "我知道了", "我知道"}
            and not any(word in modal_text for word in (
                "同意", "授权", "允许", "隐私", "协议", "条款",
            ))
        )
        if _risk_semantics(modal_text) and not informational_acknowledgement:
            return None
        if acknowledgement.get("navigation_payload_in_subtree") is True:
            return None
        if any(acknowledgement.get(key) for key in ("href", "data_href", "data_url", "router_to")):
            return None
        if acknowledgement.get("tag") == "button" and acknowledgement.get("type") != "button":
            if acknowledgement.get("form_associated") is not False:
                return None
        blockers = [modal]
    else:
        blockers = []

    active_selectors = {
        str(item.get("selector") or "")
        for item in active_elements
        if str(item.get("selector") or "")
    }
    blockers = blockers or [
        item
        for item in elements
        if item.get("interactive") is True
        and _rect_area(item.get("rect")) / viewport_area >= 0.80
        and _center_hit_selector(item) in active_selectors
        and _center_hit_selector(item) != str(item.get("selector") or "")
    ]
    if not blockers:
        return None

    modal_content = [
        item
        for item in active_elements
        if item is not acknowledgement and _normalized_text(item.get("text"))
    ]
    if not modal_content:
        return None
    if any(_stateful_control(item) for item in active_elements):
        return None
    active_text = " ".join(
        _normalized_text(item.get("text"))
        for item in active_elements
        if _normalized_text(item.get("text"))
    )
    if _risk_semantics(active_text) and not informational_acknowledgement:
        return None

    selector = str(acknowledgement.get("selector") or "").strip()
    blocker_selector = str(blockers[0].get("selector") or "").strip()
    if not selector or not blocker_selector:
        return None
    signature_facts = {
        "acknowledgement_selector": selector,
        "blocker_selector": blocker_selector,
        "capability": H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY,
    }
    if blockers[0].get("h5_modal_media_identity"):
        signature_facts["modal_media_identity"] = blockers[0]["h5_modal_media_identity"]
    signature_payload = json.dumps(
        signature_facts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return H5ModalAcknowledgementProof(
        capability=H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY,
        selector=selector,
        modal_selector=blocker_selector,
        obstruction_signature=(
            _OBSTRUCTION_SIGNATURE_PREFIX
            + hashlib.sha256(signature_payload.encode("utf-8")).hexdigest()
        ),
    )


def _snapshot_incomplete(snapshot: Mapping[str, Any]) -> bool:
    if snapshot.get("h5_modal_census_truncated") is False:
        raw = snapshot.get("elements")
        census = snapshot.get("h5_modal_census_selectors")
        selectors = {item.get("selector") for item in raw or [] if isinstance(item, Mapping)}
        if isinstance(census, list) and census and set(census).issubset(selectors):
            return bool(snapshot.get("budget_truncated") or snapshot.get("capture_degraded")
                        or snapshot.get("collection_error"))
    return bool(
        snapshot.get("element_limit_reached") is True
        or snapshot.get("truncated") is True
        or snapshot.get("budget_truncated") is True
        or snapshot.get("capture_degraded")
        or snapshot.get("collection_error")
    )


def _visible_and_center_hittable(element: Mapping[str, Any]) -> bool:
    if _rect_area(element.get("rect")) <= 0:
        return False
    style = element.get("style")
    if isinstance(style, Mapping):
        if str(style.get("display") or "").casefold() == "none":
            return False
        if str(style.get("visibility") or "").casefold() == "hidden":
            return False
        try:
            if float(style.get("opacity", 1)) == 0:
                return False
        except (TypeError, ValueError):
            return False
    hit_test = element.get("hit_test")
    return bool(
        isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
    )


def _stateful_control(element: Mapping[str, Any]) -> bool:
    tag = str(element.get("tag") or "").strip().casefold()
    role = str(element.get("role") or "").strip().casefold()
    return tag in _STATEFUL_TAGS or role in _STATEFUL_ROLES


def _risk_semantics(text: str) -> bool:
    normalized = text.casefold()
    amount_scope = _PROMOTIONAL_GIFT_AMOUNT_PATTERN.sub("", normalized)
    return bool(
        any(marker.casefold() in normalized for marker in _RISK_TEXT_MARKERS)
        or _RISK_AMOUNT_PATTERN.search(amount_scope)
    )


def _center_hit_selector(element: Mapping[str, Any]) -> str:
    hit_test = element.get("hit_test")
    if not isinstance(hit_test, Mapping):
        return ""
    return str(hit_test.get("center_target") or "").strip()


def _rect_area(value: object) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    try:
        width = max(float(value.get("width") or 0), 0.0)
        height = max(float(value.get("height") or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0
    return width * height


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip().casefold()
