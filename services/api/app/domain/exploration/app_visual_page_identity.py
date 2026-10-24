"""从当前截图的可见锚点建立业务身份，局部状态不参与身份摘要。

平台校验表示观察来源及合同成立，不表示视觉模型永不误判。没有独立
功能锚点、对象依据或稳定画面时保持未知，不能用页名和页型补齐事实。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.domain.protocol import stable_protocol_hash, thaw_json_mapping

APP_VISUAL_PAGE_IDENTITY_VERSION = "app-visual-page-identity.v2"
APP_VISUAL_LOCAL_STATE_VERSION = "app-visual-local-state.v1"
_PAGE_ROLES = frozenset(
    {
        "home",
        "navigation",
        "collection",
        "detail",
        "search",
        "profile",
        "form",
        "reader",
        "player",
        "settings",
    }
)
_OBJECT_ROLES = frozenset({"detail", "reader", "player"})
_LOCAL_KEYS = frozenset(
    {"selected_tab", "selected_filter", "sort_key", "page_index", "expanded_item_id"}
)
_STABLE_STATES = frozenset({"stable", "normal", "loaded", "content_available"})


@dataclass(frozen=True, slots=True)
class VisualIdentityAnchor:
    """截图中的可定位文字或图标；坐标只留作依据，不进入身份。"""

    text: str
    kind: str
    bounds: tuple[float, float, float, float]
    confidence: float

    @classmethod
    def parse(cls, raw: Any) -> VisualIdentityAnchor:
        if not isinstance(raw, Mapping):
            raise ValueError("anchor_missing")
        text = _text(raw.get("text"))
        kind = raw.get("kind")
        confidence = _confidence(raw.get("confidence"))
        box = raw.get("normalized_bounds")
        if (
            not text
            or not isinstance(kind, str)
            or kind not in {"label", "icon", "identifier"}
            or confidence < 0.8
        ):
            raise ValueError("anchor_unconfirmed")
        if not isinstance(box, Mapping):
            raise ValueError("anchor_bounds_missing")
        coords = tuple(box.get(key) for key in ("left", "top", "right", "bottom"))
        if any(
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not 0 <= value <= 1
            or not math.isfinite(value)
            for value in coords
        ):
            raise ValueError("anchor_bounds_invalid")
        left, top, right, bottom = (float(value) for value in coords)
        if left >= right or top >= bottom or (right - left) * (bottom - top) > 0.6:
            raise ValueError("anchor_bounds_invalid")
        return cls(text, str(kind), (left, top, right, bottom), confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "normalized_bounds": dict(
                zip(("left", "top", "right", "bottom"), self.bounds, strict=True)
            ),
            "confidence": self.confidence,
        }


def build_app_visual_page_facts(
    *,
    identity_observation: Any,
    local_state_observation: Any,
    capture_id: str,
    screenshot_artifact_id: str,
    image_signature: str,
    package_name: str | None,
    activity: str | None,
    page_state: str,
    overlay_signature: str | None,
    surface_facts: Mapping[str, Any],
) -> dict[str, Any]:
    """只从白名单观察字段生成平台身份，忽略模型自报的 verified/identity。"""
    binding = {
        "capture_id": capture_id,
        "screenshot_artifact_id": screenshot_artifact_id,
        "image_signature": image_signature,
        "package_name": package_name,
        "activity": activity,
    }
    try:
        if not isinstance(surface_facts, Mapping):
            raise ValueError("business_surface_visibility_unknown")
        if not all(isinstance(value, str) and value.strip() for value in binding.values()):
            raise ValueError("runtime_binding_missing")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_signature) is None:
            raise ValueError("image_signature_invalid")
        layer_keys = ("popup_visible", "dialog_visible", "auth_gate_visible")
        if any(
            key in surface_facts and not isinstance(surface_facts[key], bool) for key in layer_keys
        ):
            raise ValueError("business_surface_visibility_unknown")
        if (
            page_state not in _STABLE_STATES
            or overlay_signature
            or any(surface_facts.get(key) is True for key in layer_keys)
        ):
            raise ValueError("business_surface_not_visible")
        if surface_facts.get("topmost_ad_popup") is not None and not isinstance(
            surface_facts["topmost_ad_popup"], Mapping
        ):
            raise ValueError("business_surface_visibility_unknown")
        if (
            isinstance(surface_facts.get("topmost_ad_popup"), Mapping)
            and surface_facts["topmost_ad_popup"].get("is_ad") is True
        ):
            raise ValueError("business_surface_not_visible")
        canonical, observation = _identity_components(identity_observation)
    except ValueError as exc:
        return {"app_page_identity_assessment": {"status": "unknown", "reason": str(exc)}}

    result: dict[str, Any] = {
        "app_page_identity": {
            "protocol_version": APP_VISUAL_PAGE_IDENTITY_VERSION,
            "validated_by": "app_visual_observation_contract",
            **binding,
            "identity": stable_protocol_hash(canonical),
            "components": canonical,
            "observation": observation,
        },
        "app_page_identity_assessment": {
            "status": "confirmed",
            "reason": "grounded_visual_observation",
        },
    }
    if isinstance(local_state_observation, Mapping):
        state, anchors = {}, {}
        for key in sorted(_LOCAL_KEYS):
            if key not in local_state_observation:
                continue
            raw = local_state_observation[key]
            items = raw if isinstance(raw, (list, tuple)) else [raw]
            try:
                if not 1 <= len(items) <= 8:
                    raise ValueError("state_anchor_count_invalid")
                parsed = tuple(VisualIdentityAnchor.parse(item) for item in items)
                # 不接受只有图标描述的筛选值或分页号，防止把视觉猜测当状态。
                if any(item.kind == "icon" for item in parsed):
                    raise ValueError("state_value_not_visible")
            except ValueError:
                continue
            state[key] = sorted({_key_text(item.text) for item in parsed})
            anchors[key] = [item.to_dict() for item in parsed]
        if state:
            result["local_state"] = state
            result["app_local_state_observation"] = {
                "protocol_version": APP_VISUAL_LOCAL_STATE_VERSION,
                **binding,
                "anchors": anchors,
            }
    return result


def validated_app_visual_page_identity(
    raw: Any,
    *,
    capture_id: str,
    package_name: str | None,
    activity: str | None,
    image_signature: str | None = None,
    screenshot_artifact_id: str | None = None,
) -> Mapping[str, Any] | None:
    """消费方重新验证摘要与归属，不接纳历史无截图依据的自报身份。"""
    if not isinstance(raw, Mapping):
        return None
    if (
        raw.get("protocol_version") != APP_VISUAL_PAGE_IDENTITY_VERSION
        or raw.get("validated_by") != "app_visual_observation_contract"
    ):
        return None
    for key, expected in (
        ("capture_id", capture_id),
        ("package_name", package_name),
        ("activity", activity),
    ):
        if not expected or raw.get(key) != expected:
            return None
    signature = raw.get("image_signature")
    if not isinstance(signature, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", signature) is None:
        return None
    if not isinstance(raw.get("screenshot_artifact_id"), str) or not raw["screenshot_artifact_id"]:
        return None
    if image_signature is not None and signature != image_signature:
        return None
    if (
        screenshot_artifact_id is not None
        and raw.get("screenshot_artifact_id") != screenshot_artifact_id
    ):
        return None
    try:
        components, _ = _identity_components(raw.get("observation"))
    except ValueError:
        return None
    raw_components = raw.get("components")
    if (
        not isinstance(raw_components, Mapping)
        or thaw_json_mapping(raw_components) != components
        or raw.get("identity") != stable_protocol_hash(components)
    ):
        return None
    return raw


def validate_app_visual_local_state(
    *,
    facts: Mapping[str, Any],
    capture_id: str,
    screenshot_artifact_id: str,
    image_signature: str,
    package_name: str | None,
    activity: str | None,
) -> bool:
    """验证局部状态仍由同一截图的锚点导出，不能只校验页面身份。"""
    raw = facts.get("app_local_state_observation")
    if raw is None:
        return "app_page_identity" not in facts or "local_state" not in facts
    identity = facts.get("app_page_identity")
    if not isinstance(raw, Mapping) or not isinstance(identity, Mapping):
        return False
    if raw.get("protocol_version") != APP_VISUAL_LOCAL_STATE_VERSION:
        return False
    for key, expected in (
        ("capture_id", capture_id),
        ("screenshot_artifact_id", screenshot_artifact_id),
        ("image_signature", image_signature),
        ("package_name", package_name),
        ("activity", activity),
    ):
        if raw.get(key) != expected:
            return False
    rebuilt = build_app_visual_page_facts(
        identity_observation=identity.get("observation"),
        local_state_observation=raw.get("anchors"),
        capture_id=capture_id,
        screenshot_artifact_id=screenshot_artifact_id,
        image_signature=image_signature,
        package_name=package_name,
        activity=activity,
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )
    actual_state = facts.get("local_state")
    rebuilt_raw = rebuilt.get("app_local_state_observation")
    return (
        isinstance(actual_state, Mapping)
        and isinstance(rebuilt_raw, Mapping)
        and thaw_json_mapping(actual_state) == rebuilt.get("local_state")
        and thaw_json_mapping(raw) == rebuilt_raw
    )


def _identity_components(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise ValueError("identity_observation_missing")
    role, return_semantics = raw.get("page_role"), raw.get("return_semantics")
    if not isinstance(role, str) or role not in _PAGE_ROLES:
        raise ValueError("page_role_unknown")
    if not isinstance(return_semantics, str) or return_semantics not in {"root", "back", "close"}:
        raise ValueError("return_semantics_unknown")
    if _confidence(raw.get("confidence")) < 0.8:
        raise ValueError("identity_confidence_low")
    location = VisualIdentityAnchor.parse(raw.get("location"))
    function = VisualIdentityAnchor.parse(raw.get("function"))
    return_control = VisualIdentityAnchor.parse(raw.get("return_control"))
    if (
        location.kind == "icon"
        or _key_text(location.text) == _key_text(function.text)
        or location.bounds == function.bounds
    ):
        raise ValueError("independent_function_anchor_missing")
    objects_raw = raw.get("business_object")
    if objects_raw is None:
        objects_raw = []
    if (
        not isinstance(objects_raw, Sequence)
        or isinstance(objects_raw, (str, bytes))
        or len(objects_raw) > 3
    ):
        raise ValueError("business_object_invalid")
    objects = tuple(VisualIdentityAnchor.parse(item) for item in objects_raw)
    if any(item.kind == "icon" for item in objects):
        raise ValueError("business_object_not_visible")
    object_keys = sorted({_key_text(item.text) for item in objects})
    if role in _OBJECT_ROLES and not (
        any(item.kind == "identifier" for item in objects)
        or (len(object_keys) >= 2 and len({item.bounds for item in objects}) >= 2)
    ):
        raise ValueError("business_object_unconfirmed")
    components = {
        "page_role": role,
        "location": _key_text(location.text),
        "return_semantics": return_semantics,
        "business_object": object_keys,
    }
    observation = {
        "page_role": role,
        "confidence": _confidence(raw.get("confidence")),
        "location": location.to_dict(),
        "function": function.to_dict(),
        "return_semantics": return_semantics,
        "return_control": return_control.to_dict(),
        "business_object": [item.to_dict() for item in objects],
    }
    return components, observation


def _text(raw: Any) -> str | None:
    if not isinstance(raw, str) or len(raw) > 256:
        return None
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()
    if not text or any(not character.isprintable() for character in text):
        return None
    return text


def _key_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _confidence(raw: Any) -> float:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not 0 <= raw <= 1
        or not math.isfinite(raw)
    ):
        return 0.0
    return float(raw)
