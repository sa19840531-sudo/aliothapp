"""H5“我的”页活动阻断弹窗的保存确认能力。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY = (
    "profile_credential_screenshot_confirm"
)
H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_SELECTOR = (
    ".mine-pop:visible .account-cert-popup:visible .btn:visible"
)
H5_PROFILE_CREDENTIAL_SAVE_CONFIRM_SELECTOR = (
    ".van-popup.code-popup:visible .btn:visible, "
    ".van-popup.van-popup--center:visible .promoteBtn.btn2:visible"
)
_SCREENSHOT_CONFIRM_ANCESTOR_CLASSES = frozenset(
    {"account-cert-popup", "van-popup", "van-popup--center", "mine-pop"}
)
_SAVE_CONFIRM_DIALOG_CLASSES = frozenset({"van-popup", "van-popup--center"})
_CODE_SAVE_CONFIRM_ANCESTOR_CLASSES = frozenset(
    {"code-popup", *_SAVE_CONFIRM_DIALOG_CLASSES}
)
_PROMOTE_SAVE_CONFIRM_ANCESTOR_CLASSES = frozenset(
    {"promotebtnbox", *_SAVE_CONFIRM_DIALOG_CLASSES}
)
_SAVE_CONFIRM_TEXTS = frozenset({"立即保存", "保存"})
@dataclass(frozen=True, slots=True)
class H5ProfileCredentialPopupProof:
    """检测与执行阶段共同复验的个人中心弹窗关闭事实。"""

    capability: str
    selector: str
    source_selector: str
    obstruction_signature: str


def h5_profile_credential_screenshot_confirm(
    snapshot: Mapping[str, Any] | None,
) -> H5ProfileCredentialPopupProof | None:
    """验签个人中心活动弹窗内唯一、顶层可命中的保存确认按钮。"""

    if not isinstance(snapshot, Mapping) or _snapshot_unusable(snapshot):
        return None
    raw_elements = snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return None
    elements = [item for item in raw_elements if isinstance(item, Mapping)]
    screenshot_matches = [
        item
        for item in elements
        if _matches_screenshot_confirm(item)
    ]
    save_dialogs = [item for item in elements if _matches_save_confirm_dialog(item)]
    save_matches = [item for item in elements if _matches_save_confirm(item)]
    if screenshot_matches and save_matches:
        return None
    if screenshot_matches:
        if len(screenshot_matches) != 1:
            return None
        return _build_proof(
            element=screenshot_matches[0],
            selector=H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_SELECTOR,
            variant="screenshot_confirm",
        )
    if len(save_dialogs) != 1 or len(save_matches) != 1:
        return None
    return _build_proof(
        element=save_matches[0],
        selector=H5_PROFILE_CREDENTIAL_SAVE_CONFIRM_SELECTOR,
        variant=(
            "promote_save_confirm"
            if {"promotebtn", "btn2"}.issubset(
                _normalized_tokens(save_matches[0].get("class_tokens"))
            )
            else "save_confirm"
        ),
    )


def _build_proof(
    *,
    element: Mapping[str, Any],
    selector: str,
    variant: str,
) -> H5ProfileCredentialPopupProof | None:
    """从已经完成结构验签的唯一控件生成跨阶段一致证明。"""
    source_selector = str(element.get("selector") or "").strip()
    if not source_selector:
        return None
    text = _compact_text(element.get("text"))
    payload = json.dumps(
        {
            "capability": H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY,
            "selector": source_selector,
            "text": text,
            "variant": variant,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return H5ProfileCredentialPopupProof(
        capability=H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY,
        selector=selector,
        source_selector=source_selector,
        obstruction_signature=(
            "h5-obstruction:"
            + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        ),
    )


def _matches_screenshot_confirm(element: Mapping[str, Any]) -> bool:
    class_tokens = _normalized_tokens(element.get("class_tokens"))
    ancestor_tokens = _normalized_tokens(element.get("ancestor_class_tokens"))
    hit_test = element.get("hit_test")
    return bool(
        str(element.get("tag") or "").strip().casefold() == "div"
        and _compact_text(element.get("text")) == "请截图保存"
        and class_tokens == {"btn"}
        and _SCREENSHOT_CONFIRM_ANCESTOR_CLASSES.issubset(ancestor_tokens)
        and _rect_area(element.get("rect")) > 0
        and _element_enabled(element)
        and isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
        and hit_test.get("topmost_at_center") is not False
    )


def _matches_save_confirm_dialog(element: Mapping[str, Any]) -> bool:
    class_tokens = _normalized_tokens(element.get("class_tokens"))
    hit_test = element.get("hit_test")
    return bool(
        str(element.get("role") or "").strip().casefold() == "dialog"
        and _SAVE_CONFIRM_DIALOG_CLASSES.issubset(class_tokens)
        and _rect_area(element.get("rect")) > 0
        and _element_enabled(element)
        and isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
        and hit_test.get("topmost_at_center") is not False
    )


def _matches_save_confirm(element: Mapping[str, Any]) -> bool:
    class_tokens = _normalized_tokens(element.get("class_tokens"))
    ancestor_tokens = _normalized_tokens(element.get("ancestor_class_tokens"))
    hit_test = element.get("hit_test")
    structure_matches = bool(
        (
            "btn" in class_tokens
            and _CODE_SAVE_CONFIRM_ANCESTOR_CLASSES.issubset(ancestor_tokens)
        )
        or (
            {"promotebtn", "btn2"}.issubset(class_tokens)
            and _PROMOTE_SAVE_CONFIRM_ANCESTOR_CLASSES.issubset(ancestor_tokens)
        )
    )
    return bool(
        str(element.get("tag") or "").strip().casefold() in {"button", "div"}
        and _compact_text(element.get("text")) in _SAVE_CONFIRM_TEXTS
        and structure_matches
        and _rect_area(element.get("rect")) > 0
        and _element_enabled(element)
        and isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
        and hit_test.get("topmost_at_center") is not False
    )


def _element_enabled(element: Mapping[str, Any]) -> bool:
    return bool(
        element.get("disabled") is not True
        and str(element.get("aria_disabled") or "").strip().casefold() != "true"
    )


def _compact_text(value: object) -> str:
    return "".join(str(value or "").split())


def _snapshot_unusable(snapshot: Mapping[str, Any]) -> bool:
    """元素预算截断不否定已采集的精确顶层命中，采集故障仍失败关闭。"""

    return bool(
        snapshot.get("capture_degraded")
        or snapshot.get("collection_error")
    )


def _normalized_tokens(value: object) -> set[str]:
    return (
        {str(item).strip().casefold() for item in value if str(item).strip()}
        if isinstance(value, list)
        else set()
    )


def _rect_area(value: object) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    try:
        return max(float(value.get("width") or 0), 0.0) * max(
            float(value.get("height") or 0), 0.0
        )
    except (TypeError, ValueError):
        return 0.0
