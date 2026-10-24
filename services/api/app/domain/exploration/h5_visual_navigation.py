"""H5 Flutter 视觉坐标导航的稳定领域合同。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from services.api.app.domain.exploration.h5_logical_surface import (
    stable_h5_surface_hash,
)

H5VisualCoordinateMode = Literal["off", "shadow", "enforced"]

SEMANTICS_MIN_USABLE_NODES = 12
SEMANTICS_MIN_TEXT_CHARS = 20
SEMANTICS_OCR_RATIO_REQUIRED_AT = 40
SEMANTICS_MIN_OCR_RATIO = 0.35

PRIMARY_NAVIGATION_PAGE_TYPES = frozenset(
    {"home", "content_list", "video_list", "post_list", "profile"}
)
PROFILE_LOCAL_PAGE_TYPES = frozenset({"settings", "payment", "activity"})
VISUAL_TARGET_CONFIDENCE_FLOORS = {
    "primary_navigation": 0.85,
    "content_card": 0.90,
    "video_card": 0.90,
    "profile_settings": 0.90,
    "profile_payment": 0.90,
    "profile_activity": 0.90,
}
VISUAL_TARGET_EXPECTED_PAGE_TYPES = {
    "content_card": frozenset({"content_detail"}),
    "video_card": frozenset({"video_detail"}),
    "profile_settings": frozenset({"settings"}),
    "profile_payment": frozenset({"payment"}),
    "profile_activity": frozenset({"activity"}),
}
VISUAL_TARGET_RISK_LEVELS = {
    "primary_navigation": "low",
    "content_card": "low",
    "video_card": "low",
    "profile_settings": "low",
    "profile_payment": "medium",
    "profile_activity": "low",
}
VISUAL_TARGET_GEOMETRY_LIMITS = MappingProxyType(
    {
        "primary_navigation": MappingProxyType(
            {
                "min_width": 0.04,
                "max_width": 0.40,
                "min_height": 0.025,
                "max_height": 0.20,
                "min_area": 0.001,
                "max_area": 0.08,
                "min_left": 0.0,
                "max_right": 1.0,
                "min_top": 0.0,
                "max_bottom": 1.0,
                "max_top": 0.97,
                "min_aspect_ratio": 0.5,
                "max_aspect_ratio": 8.0,
            }
        ),
        "content_card": MappingProxyType(
            {
                "min_width": 0.12,
                "max_width": 0.92,
                "min_height": 0.06,
                "max_height": 0.42,
                "min_area": 0.01,
                "max_area": 0.32,
                "min_left": 0.02,
                "max_right": 0.98,
                "min_top": 0.04,
                "max_bottom": 1.0,
                "max_top": 0.92,
                "min_aspect_ratio": 0.5,
                "max_aspect_ratio": 6.0,
            }
        ),
        "video_card": MappingProxyType(
            {
                "min_width": 0.12,
                "max_width": 0.92,
                "min_height": 0.06,
                "max_height": 0.42,
                "min_area": 0.01,
                "max_area": 0.32,
                "min_left": 0.02,
                "max_right": 0.98,
                "min_top": 0.04,
                "max_bottom": 1.0,
                "max_top": 0.92,
                "min_aspect_ratio": 0.5,
                "max_aspect_ratio": 6.0,
            }
        ),
        **{
            target_kind: MappingProxyType(
                {
                    "min_width": 0.025,
                    "max_width": 0.92,
                    "min_height": 0.025,
                    "max_height": 0.18,
                    "min_area": 0.001,
                    "max_area": 0.12,
                    "min_left": 0.02,
                    "max_right": 0.98,
                    "min_top": 0.02,
                    "max_bottom": 0.94,
                    "max_top": 0.88,
                    "min_aspect_ratio": 0.4,
                    "max_aspect_ratio": 12.0,
                }
            )
            for target_kind in (
                "profile_settings",
                "profile_payment",
                "profile_activity",
            )
        },
    }
)
VISUAL_NAVIGATION_ERROR_CODES = frozenset(
    {
        "VISUAL_TARGET_CONFIDENCE_LOW",
        "VISUAL_TARGET_NOT_ALLOWLISTED",
        "VISUAL_TARGET_NOT_UNIQUE",
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "VISUAL_TARGET_COORDINATE_INVALID",
        "VISUAL_TARGET_OBSTRUCTED",
        "VISUAL_NAVIGATION_NO_EFFECT",
        "VISUAL_NAVIGATION_UNSAFE_DESTINATION",
        "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
    }
)

VISUAL_NAVIGATION_CAPABILITY_PROTOCOL_VERSION = "visual_navigation_coordinate.v1"
_SEMANTICS_LEVELS = frozenset(
    {
        "semantics_unavailable",
        "semantics_sparse",
        "semantics_partial",
        "semantics_sufficient",
    }
)
_VISUAL_CAPABILITY_FIELDS = frozenset(
    {
        "protocol_version",
        "source_task_id",
        "source_capture_id",
        "source_artifact_id",
        "source_artifact_sha256",
        "source_identity_fingerprint",
        "source_state_fingerprint",
        "source_page_type",
        "source_page_type_confidence",
        "source_identity_bound",
        "render_profile",
        "semantics_coverage_level",
        "target_kind",
        "normalized_label_digest",
        "expected_page_type",
        "visual_target_confidence",
        "selected",
        "unique",
        "risk_flags",
        "normalized_rect",
        "source_image_size",
        "viewport_metrics",
        "model_image_transform",
        "local_visual_digest",
        "surrounding_anchor_digest",
        "target_signature",
    }
)
_VISUAL_CAPABILITY_SIGNED_FIELDS = _VISUAL_CAPABILITY_FIELDS.difference(
    {"target_signature"}
)
_DIGEST_PATTERN = re.compile(r"(?:sha256:)?[0-9a-f]{64}$")
_BODY_ROLES = frozenset({"text", "paragraph", "statictext"})
_CONTENT_MARKERS = frozenset({"article", "card", "content", "feed", "item", "post", "video"})
_PROFILE_SOURCE_CONFIDENCE_FLOOR = 0.70
_PRIMARY_BOTTOM_BAND_MIN_TOP = 0.75
_FULL_PAGE_RECT_AREA_FLOOR = 0.80
_UNSAFE_VISUAL_RISK_MARKERS = frozenset(
    {
        "ad",
        "advertisement",
        "authorization",
        "confirm_payment",
        "download",
        "external",
        "external_link",
        "login_submit",
        "purchase",
        "submit",
        "transaction",
        "交易",
        "充值",
        "外链",
        "广告",
        "授权",
        "提交",
        "确认支付",
        "立即支付",
        "购买",
    }
)


class VisualNavigationContractError(ValueError):
    """视觉导航策略或密封能力不符合稳定合同。"""


VisualNavigationCapabilityError = VisualNavigationContractError


@dataclass(frozen=True, slots=True)
class VisualViewportRect:
    """以 visualViewport CSS 像素表达的不可变矩形。"""

    left: float
    top: float
    width: float
    height: float
    coordinate_space: str = "visual_viewport_css_pixels"


@dataclass(frozen=True, slots=True)
class VisualNavigationTargetDecision:
    """视觉候选白名单判定，供应用层决定是否允许签发能力。"""

    allowed: bool
    reason_code: str | None
    target_kind: str
    expected_page_type: str
    risk_level: str | None
    semantic_target: str | None
    target_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VisualNavigationCapability:
    """已通过格式复核、可供后续签名与解析的视觉导航能力。"""

    source_task_id: str
    source_capture_id: str
    source_artifact_id: str
    source_artifact_sha256: str
    source_identity_fingerprint: str
    source_state_fingerprint: str
    source_page_type: str
    source_page_type_confidence: float
    source_identity_bound: bool
    render_profile: Mapping[str, object]
    semantics_coverage_level: str
    target_kind: str
    normalized_label_digest: str
    expected_page_type: str
    visual_target_confidence: float
    selected: bool
    unique: bool
    risk_flags: tuple[str, ...]
    normalized_rect: Mapping[str, float]
    source_image_size: Mapping[str, object]
    viewport_metrics: Mapping[str, object]
    model_image_transform: Mapping[str, object]
    local_visual_digest: str
    surrounding_anchor_digest: str
    target_signature: str
    protocol_version: str = VISUAL_NAVIGATION_CAPABILITY_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, object]:
        """还原为仅含 JSON 数据的协议载荷。"""
        return {
            "protocol_version": self.protocol_version,
            "source_task_id": self.source_task_id,
            "source_capture_id": self.source_capture_id,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_identity_fingerprint": self.source_identity_fingerprint,
            "source_state_fingerprint": self.source_state_fingerprint,
            "source_page_type": self.source_page_type,
            "source_page_type_confidence": self.source_page_type_confidence,
            "source_identity_bound": self.source_identity_bound,
            "render_profile": dict(self.render_profile),
            "semantics_coverage_level": self.semantics_coverage_level,
            "target_kind": self.target_kind,
            "normalized_label_digest": self.normalized_label_digest,
            "expected_page_type": self.expected_page_type,
            "visual_target_confidence": self.visual_target_confidence,
            "selected": self.selected,
            "unique": self.unique,
            "risk_flags": list(self.risk_flags),
            "normalized_rect": dict(self.normalized_rect),
            "source_image_size": dict(self.source_image_size),
            "viewport_metrics": dict(self.viewport_metrics),
            "model_image_transform": dict(self.model_image_transform),
            "local_visual_digest": self.local_visual_digest,
            "surrounding_anchor_digest": self.surrounding_anchor_digest,
            "target_signature": self.target_signature,
        }


def normalize_flutter_rect_to_visual_viewport(
    *,
    local_rect: Mapping[str, object],
    host_transform: Sequence[object],
    viewport_offset: Sequence[object],
) -> VisualViewportRect:
    """把 Flutter host 局部矩形经完整仿射变换映射到视觉视口。"""
    if not isinstance(local_rect, Mapping):
        raise VisualNavigationCapabilityError("local_rect 必须为对象")
    if set(local_rect) != {"left", "top", "width", "height"}:
        raise VisualNavigationCapabilityError(
            "local_rect 必须包含且仅包含 left、top、width、height"
        )
    left = _finite_geometry_number(local_rect["left"], "local_rect.left")
    top = _finite_geometry_number(local_rect["top"], "local_rect.top")
    width = _finite_geometry_number(local_rect["width"], "local_rect.width")
    height = _finite_geometry_number(local_rect["height"], "local_rect.height")
    if width < 0 or height < 0:
        raise VisualNavigationCapabilityError("local_rect 宽高不得为负数")
    if isinstance(host_transform, (str, bytes)) or len(host_transform) != 6:
        raise VisualNavigationCapabilityError("host_transform 必须包含 6 个数值")
    if isinstance(viewport_offset, (str, bytes)) or len(viewport_offset) != 2:
        raise VisualNavigationCapabilityError("viewport_offset 必须包含 2 个数值")
    a, b, c, d, e, f = (
        _finite_geometry_number(value, f"host_transform[{index}]")
        for index, value in enumerate(host_transform)
    )
    offset_left, offset_top = (
        _finite_geometry_number(value, f"viewport_offset[{index}]")
        for index, value in enumerate(viewport_offset)
    )
    corners = (
        (left, top),
        (left + width, top),
        (left, top + height),
        (left + width, top + height),
    )
    transformed = tuple(
        (
            a * x + c * y + e - offset_left,
            b * x + d * y + f - offset_top,
        )
        for x, y in corners
    )
    x_values = tuple(point[0] for point in transformed)
    y_values = tuple(point[1] for point in transformed)
    transformed_left = min(x_values)
    transformed_top = min(y_values)
    return VisualViewportRect(
        left=transformed_left,
        top=transformed_top,
        width=max(x_values) - transformed_left,
        height=max(y_values) - transformed_top,
    )


def _finite_geometry_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualNavigationCapabilityError(f"{field_name} 必须为有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise VisualNavigationCapabilityError(f"{field_name} 必须为有限数值")
    return number


def classify_flutter_semantics_coverage(
    *, elements: Sequence[Mapping[str, object]], ocr_text: object
) -> dict[str, object]:
    """按固定阈值评估 Flutter Semantics 对当前视觉页面的覆盖程度。"""
    usable_nodes = [item for item in elements if _is_usable_flutter_node(item)]
    semantic_text = "".join(_semantic_label(item) for item in usable_nodes)
    ocr_char_count = _text_char_count(ocr_text)
    semantic_char_count = len(semantic_text)
    coverage = (
        round(min(semantic_char_count / ocr_char_count, 1.0), 4)
        if ocr_char_count
        else None
    )
    degraded_reasons: list[str] = []
    if not usable_nodes:
        level = "semantics_unavailable"
        degraded_reasons.append("usable_semantics_nodes_missing")
    elif len(usable_nodes) < SEMANTICS_MIN_USABLE_NODES:
        level = "semantics_sparse"
        degraded_reasons.append("usable_node_count_below_threshold")
    elif semantic_char_count < SEMANTICS_MIN_TEXT_CHARS:
        level = "semantics_sparse"
        degraded_reasons.append("semantic_text_below_threshold")
    elif ocr_char_count >= SEMANTICS_OCR_RATIO_REQUIRED_AT:
        if coverage is not None and coverage >= SEMANTICS_MIN_OCR_RATIO:
            level = "semantics_sufficient"
        else:
            level = "semantics_partial"
            degraded_reasons.append("semantic_ocr_text_coverage_below_threshold")
    else:
        level = "semantics_sufficient"
    return {
        "level": level,
        "usable_node_count": len(usable_nodes),
        "semantic_text_char_count": semantic_char_count,
        "ocr_text_char_count": ocr_char_count,
        "semantic_ocr_text_coverage": coverage,
        "degraded_reasons": degraded_reasons,
    }


def evaluate_visual_navigation_target(
    *,
    target_kind: object,
    source_page_type: object,
    expected_page_type: object,
    visual_target_confidence: object,
    selected: object,
    risk_flags: Sequence[object],
    source_page_type_confidence: object = 1.0,
    source_identity_bound: object = True,
    normalized_rect: Mapping[str, object] | None = None,
    unique: object = True,
    label: object = None,
) -> VisualNavigationTargetDecision:
    """以固定白名单和置信度下限判定视觉目标是否允许签发。"""
    normalized_kind = _normalized_text(target_kind)
    normalized_source = _normalized_text(source_page_type)
    normalized_expected = _normalized_text(expected_page_type)
    if normalized_kind not in VISUAL_TARGET_CONFIDENCE_FLOORS:
        return _target_decision(
            False,
            "visual_target_not_allowlisted",
            normalized_kind,
            normalized_expected,
        )
    allowed_expected = VISUAL_TARGET_EXPECTED_PAGE_TYPES.get(normalized_kind)
    if allowed_expected is not None and normalized_expected not in allowed_expected:
        return _target_decision(
            False,
            "visual_target_expected_page_mismatch",
            normalized_kind,
            normalized_expected,
        )
    if (
        normalized_kind == "primary_navigation"
        and normalized_expected not in PRIMARY_NAVIGATION_PAGE_TYPES
    ):
        return _target_decision(
            False,
            "visual_target_expected_page_mismatch",
            normalized_kind,
            normalized_expected,
        )
    if normalized_kind.startswith("profile_"):
        if normalized_source != "profile":
            return _target_decision(
                False,
                "visual_profile_source_required",
                normalized_kind,
                normalized_expected,
            )
        source_confidence = _raw_unit_number(
            source_page_type_confidence,
            "source_page_type_confidence",
        )
        if source_confidence < _PROFILE_SOURCE_CONFIDENCE_FLOOR:
            return _target_decision(
                False,
                "visual_profile_source_confidence_low",
                normalized_kind,
                normalized_expected,
            )
        if source_identity_bound is not True:
            return _target_decision(
                False,
                "visual_profile_identity_required",
                normalized_kind,
                normalized_expected,
            )
    confidence = _raw_unit_number(
        visual_target_confidence,
        "visual_target_confidence",
    )
    if confidence < VISUAL_TARGET_CONFIDENCE_FLOORS[normalized_kind]:
        return _target_decision(
            False,
            "visual_target_confidence_low",
            normalized_kind,
            normalized_expected,
        )
    if normalized_kind == "primary_navigation" and selected is not False:
        return _target_decision(
            False,
            "visual_target_already_selected",
            normalized_kind,
            normalized_expected,
        )
    if unique is not True:
        return _target_decision(
            False,
            "visual_target_not_unique",
            normalized_kind,
            normalized_expected,
        )
    if _has_unsafe_visual_risk(risk_flags=risk_flags, label=label):
        return _target_decision(
            False,
            "visual_target_risk_flagged",
            normalized_kind,
            normalized_expected,
        )
    if normalized_rect is None:
        return _target_decision(
            False,
            "visual_target_coordinate_invalid",
            normalized_kind,
            normalized_expected,
        )
    rect = _normalized_rect(normalized_rect)
    if rect["width"] * rect["height"] >= _FULL_PAGE_RECT_AREA_FLOOR:
        return _target_decision(
            False,
            "visual_target_full_page_rect",
            normalized_kind,
            normalized_expected,
        )
    geometry_limits = VISUAL_TARGET_GEOMETRY_LIMITS[normalized_kind]
    rect_area = rect["width"] * rect["height"]
    aspect_ratio = rect["width"] / rect["height"]
    if (
        rect["width"] < geometry_limits["min_width"]
        or rect["width"] > geometry_limits["max_width"]
        or rect["height"] < geometry_limits["min_height"]
        or rect["height"] > geometry_limits["max_height"]
        or (
            rect_area < geometry_limits["min_area"]
            and not math.isclose(
                rect_area,
                geometry_limits["min_area"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        or (
            rect_area > geometry_limits["max_area"]
            and not math.isclose(
                rect_area,
                geometry_limits["max_area"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        or rect["x"] < geometry_limits["min_left"]
        or (
            rect["x"] + rect["width"] > geometry_limits["max_right"]
            and not math.isclose(
                rect["x"] + rect["width"],
                geometry_limits["max_right"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        or rect["y"] < geometry_limits["min_top"]
        or (
            rect["y"] + rect["height"] > geometry_limits["max_bottom"]
            and not math.isclose(
                rect["y"] + rect["height"],
                geometry_limits["max_bottom"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        or rect["y"] > geometry_limits["max_top"]
        or (
            aspect_ratio < geometry_limits["min_aspect_ratio"]
            and not math.isclose(
                aspect_ratio,
                geometry_limits["min_aspect_ratio"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        or (
            aspect_ratio > geometry_limits["max_aspect_ratio"]
            and not math.isclose(
                aspect_ratio,
                geometry_limits["max_aspect_ratio"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
    ):
        return _target_decision(
            False,
            "visual_target_geometry_invalid",
            normalized_kind,
            normalized_expected,
        )
    if (
        normalized_kind == "primary_navigation"
        and rect["y"] < _PRIMARY_BOTTOM_BAND_MIN_TOP
    ):
        return _target_decision(
            False,
            "visual_primary_navigation_not_bottom_band",
            normalized_kind,
            normalized_expected,
        )
    return _target_decision(True, None, normalized_kind, normalized_expected)


def build_visual_navigation_capability(
    *,
    source_task_id: object,
    source_capture_id: object,
    source_artifact_id: object,
    source_artifact_sha256: object,
    source_identity_fingerprint: object,
    source_state_fingerprint: object,
    source_page_type: object,
    source_page_type_confidence: object,
    source_identity_bound: object,
    render_profile: Mapping[str, object],
    semantics_coverage_level: object,
    target_kind: object,
    label: object,
    expected_page_type: object,
    visual_target_confidence: object,
    selected: object,
    unique: object,
    risk_flags: Sequence[object],
    normalized_rect: Mapping[str, object],
    source_image_size: Mapping[str, object],
    viewport_metrics: Mapping[str, object],
    model_image_transform: Mapping[str, object],
    local_visual_digest: object,
    surrounding_anchor_digest: object,
) -> dict[str, object]:
    """重跑完整策略并签发只包含可信代码密封材料的能力。"""
    normalized_label = _normalized_label(label)
    normalized_risk_flags = _risk_flags(risk_flags)
    decision = evaluate_visual_navigation_target(
        target_kind=target_kind,
        source_page_type=source_page_type,
        source_page_type_confidence=source_page_type_confidence,
        source_identity_bound=source_identity_bound,
        expected_page_type=expected_page_type,
        visual_target_confidence=visual_target_confidence,
        selected=selected,
        risk_flags=normalized_risk_flags,
        normalized_rect=normalized_rect,
        unique=unique,
        label=normalized_label,
    )
    if not decision.allowed:
        raise VisualNavigationContractError(
            f"视觉导航目标未通过签发策略：{decision.reason_code}"
        )
    signed_payload: dict[str, object] = {
        "protocol_version": VISUAL_NAVIGATION_CAPABILITY_PROTOCOL_VERSION,
        "source_task_id": _required_text(source_task_id, "source_task_id"),
        "source_capture_id": _required_text(source_capture_id, "source_capture_id"),
        "source_artifact_id": _required_text(source_artifact_id, "source_artifact_id"),
        "source_artifact_sha256": _required_digest(
            source_artifact_sha256,
            "source_artifact_sha256",
        ),
        "source_identity_fingerprint": _required_digest(
            source_identity_fingerprint,
            "source_identity_fingerprint",
        ),
        "source_state_fingerprint": _required_digest(
            source_state_fingerprint,
            "source_state_fingerprint",
        ),
        "source_page_type": _required_text(source_page_type, "source_page_type").casefold(),
        "source_page_type_confidence": _confidence(
            source_page_type_confidence,
            "source_page_type_confidence",
        ),
        "source_identity_bound": _required_true(
            source_identity_bound,
            "source_identity_bound",
        ),
        "render_profile": dict(_render_profile(render_profile)),
        "semantics_coverage_level": _sparse_semantics_level(
            semantics_coverage_level
        ),
        "target_kind": _target_kind(target_kind),
        "normalized_label_digest": stable_h5_surface_hash(normalized_label),
        "expected_page_type": _required_text(
            expected_page_type,
            "expected_page_type",
        ).casefold(),
        "visual_target_confidence": _confidence(
            visual_target_confidence,
            "visual_target_confidence",
        ),
        "selected": _required_bool(selected, "selected"),
        "unique": _required_true(unique, "unique"),
        "risk_flags": list(normalized_risk_flags),
        "normalized_rect": dict(_normalized_rect(normalized_rect)),
        "source_image_size": dict(_source_image_size(source_image_size)),
        "viewport_metrics": dict(_viewport_metrics(viewport_metrics)),
        "model_image_transform": dict(
            _identity_model_transform(model_image_transform)
        ),
        "local_visual_digest": _required_digest(
            local_visual_digest,
            "local_visual_digest",
        ),
        "surrounding_anchor_digest": _required_digest(
            surrounding_anchor_digest,
            "surrounding_anchor_digest",
        ),
    }
    capability = {
        **signed_payload,
        "target_signature": stable_h5_surface_hash(signed_payload),
    }
    return verify_visual_navigation_capability(capability).to_dict()


def verify_visual_navigation_capability(
    value: Mapping[str, object],
) -> VisualNavigationCapability:
    """先解析 canonical 载荷并验签，再重跑全部内生策略与坐标合同。"""
    if not isinstance(value, Mapping):
        raise VisualNavigationCapabilityError("视觉导航能力必须为对象")
    unknown = set(value).difference(_VISUAL_CAPABILITY_FIELDS)
    missing = _VISUAL_CAPABILITY_FIELDS.difference(value)
    if unknown:
        raise VisualNavigationCapabilityError("视觉导航能力包含未知字段")
    if missing:
        raise VisualNavigationCapabilityError("视觉导航能力缺少必填字段")
    if value["protocol_version"] != VISUAL_NAVIGATION_CAPABILITY_PROTOCOL_VERSION:
        raise VisualNavigationCapabilityError("视觉导航能力协议版本不受支持")
    signature = _required_digest(value["target_signature"], "target_signature")
    capability = VisualNavigationCapability(
        source_task_id=_required_text(value["source_task_id"], "source_task_id"),
        source_capture_id=_required_text(value["source_capture_id"], "source_capture_id"),
        source_artifact_id=_required_text(value["source_artifact_id"], "source_artifact_id"),
        source_artifact_sha256=_required_digest(
            value["source_artifact_sha256"],
            "source_artifact_sha256",
        ),
        source_identity_fingerprint=_required_digest(
            value["source_identity_fingerprint"], "source_identity_fingerprint"
        ),
        source_state_fingerprint=_required_digest(
            value["source_state_fingerprint"], "source_state_fingerprint"
        ),
        source_page_type=_required_text(
            value["source_page_type"],
            "source_page_type",
        ).casefold(),
        source_page_type_confidence=_confidence(
            value["source_page_type_confidence"],
            "source_page_type_confidence",
        ),
        source_identity_bound=_required_true(
            value["source_identity_bound"],
            "source_identity_bound",
        ),
        render_profile=_render_profile(value["render_profile"]),
        semantics_coverage_level=_sparse_semantics_level(
            value["semantics_coverage_level"]
        ),
        target_kind=_target_kind(value["target_kind"]),
        normalized_label_digest=_required_digest(
            value["normalized_label_digest"],
            "normalized_label_digest",
        ),
        expected_page_type=_required_text(value["expected_page_type"], "expected_page_type"),
        visual_target_confidence=_confidence(
            value["visual_target_confidence"], "visual_target_confidence"
        ),
        selected=_required_bool(value["selected"], "selected"),
        unique=_required_true(value["unique"], "unique"),
        risk_flags=_risk_flags(value["risk_flags"]),
        normalized_rect=_normalized_rect(value["normalized_rect"]),
        source_image_size=_source_image_size(value["source_image_size"]),
        viewport_metrics=_viewport_metrics(value["viewport_metrics"]),
        model_image_transform=_identity_model_transform(
            value["model_image_transform"]
        ),
        local_visual_digest=_required_digest(value["local_visual_digest"], "local_visual_digest"),
        surrounding_anchor_digest=_required_digest(
            value["surrounding_anchor_digest"], "surrounding_anchor_digest"
        ),
        target_signature=signature,
    )
    canonical_value = capability.to_dict()
    canonical_signed_payload = {
        key: canonical_value[key]
        for key in sorted(_VISUAL_CAPABILITY_SIGNED_FIELDS)
    }
    if stable_h5_surface_hash(canonical_signed_payload) != signature:
        raise VisualNavigationContractError("视觉导航能力 canonical 签名不匹配")
    _verify_capability_policy(capability)
    return capability


def visual_navigation_region_digests(
    *,
    pixel_bytes: bytes,
    image_width: int,
    image_height: int,
    channel_count: int,
    normalized_rect: Mapping[str, object],
) -> dict[str, str]:
    """从解码后的原图像素分别计算目标区域和周边锚点摘要。"""
    for field_name, number in (
        ("image_width", image_width),
        ("image_height", image_height),
        ("channel_count", channel_count),
    ):
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise VisualNavigationContractError(f"{field_name} 必须为正整数")
    if channel_count > 4:
        raise VisualNavigationContractError("channel_count 不受支持")
    if not isinstance(pixel_bytes, bytes):
        raise VisualNavigationContractError("pixel_bytes 必须为解码后的不可变字节")
    expected_length = image_width * image_height * channel_count
    if len(pixel_bytes) != expected_length:
        raise VisualNavigationContractError("pixel_bytes 长度与图像尺寸不匹配")
    rect = _normalized_rect(normalized_rect)
    left = max(0, min(image_width - 1, math.floor(rect["x"] * image_width)))
    top = max(0, min(image_height - 1, math.floor(rect["y"] * image_height)))
    right = max(left + 1, min(image_width, math.ceil((rect["x"] + rect["width"]) * image_width)))
    bottom = max(top + 1, min(image_height, math.ceil((rect["y"] + rect["height"]) * image_height)))
    padding_x = max(1, math.ceil((right - left) * 0.25))
    padding_y = max(1, math.ceil((bottom - top) * 0.25))
    anchor_left = max(0, left - padding_x)
    anchor_top = max(0, top - padding_y)
    anchor_right = min(image_width, right + padding_x)
    anchor_bottom = min(image_height, bottom + padding_y)
    local_digest = _pixel_region_digest(
        pixel_bytes=pixel_bytes,
        image_width=image_width,
        image_height=image_height,
        channel_count=channel_count,
        region=(left, top, right, bottom),
        excluded_region=None,
        purpose="target_local",
    )
    anchor_digest = _pixel_region_digest(
        pixel_bytes=pixel_bytes,
        image_width=image_width,
        image_height=image_height,
        channel_count=channel_count,
        region=(anchor_left, anchor_top, anchor_right, anchor_bottom),
        excluded_region=(left, top, right, bottom),
        purpose="surrounding_anchor",
    )
    return {
        "local_visual_digest": local_digest,
        "surrounding_anchor_digest": anchor_digest,
    }


def _pixel_region_digest(
    *,
    pixel_bytes: bytes,
    image_width: int,
    image_height: int,
    channel_count: int,
    region: tuple[int, int, int, int],
    excluded_region: tuple[int, int, int, int] | None,
    purpose: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "protocol_version": "visual_navigation_pixel_region.v1",
                "purpose": purpose,
                "image_width": image_width,
                "image_height": image_height,
                "channel_count": channel_count,
                "region": list(region),
                "excluded_region": (
                    list(excluded_region) if excluded_region is not None else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    left, top, right, bottom = region
    excluded_left, excluded_top, excluded_right, excluded_bottom = (
        excluded_region or (0, 0, 0, 0)
    )
    row_stride = image_width * channel_count
    sampled_pixel_count = 0
    for y in range(top, bottom):
        segments = [(left, right)]
        if excluded_region is not None and excluded_top <= y < excluded_bottom:
            segments = [
                (segment_left, segment_right)
                for segment_left, segment_right in (
                    (left, min(right, excluded_left)),
                    (max(left, excluded_right), right),
                )
                if segment_left < segment_right
            ]
        for segment_left, segment_right in segments:
            sampled_pixel_count += segment_right - segment_left
            digest.update(y.to_bytes(4, "big"))
            digest.update(segment_left.to_bytes(4, "big"))
            digest.update(segment_right.to_bytes(4, "big"))
            start = y * row_stride + segment_left * channel_count
            end = y * row_stride + segment_right * channel_count
            digest.update(pixel_bytes[start:end])
    if sampled_pixel_count <= 0:
        raise VisualNavigationContractError(f"{purpose} 采样像素不能为空")
    return "sha256:" + digest.hexdigest()


def public_visual_navigation_audit(value: Mapping[str, object]) -> dict[str, object]:
    """生成不含标签、坐标、来源标识或任何签名材料的安全摘要。"""
    capability = verify_visual_navigation_capability(value)
    return {
        "protocol_version": capability.protocol_version,
        "semantics_coverage_level": capability.semantics_coverage_level,
        "target_kind": capability.target_kind,
        "expected_page_type": capability.expected_page_type,
        "visual_target_confidence": capability.visual_target_confidence,
        "risk_level": VISUAL_TARGET_RISK_LEVELS[capability.target_kind],
    }


def _is_usable_flutter_node(item: Mapping[str, object]) -> bool:
    if item.get("visible") is not True or not _is_flutter_semantics(item):
        return False
    hit_test = item.get("hit_test")
    if not isinstance(hit_test, Mapping) or hit_test.get("center_hits_target") is not True:
        return False
    role = _normalized_text(item.get("role"))
    label = _semantic_label(item)
    interactive = item.get("interactive") is True
    if not role or role in _BODY_ROLES:
        return False
    if _has_content_context(item) and not interactive:
        return False
    return bool(label or interactive)


def _is_flutter_semantics(item: Mapping[str, object]) -> bool:
    return bool(
        _normalized_text(item.get("tag")) == "flt-semantics"
        or _normalized_text(item.get("selector")).startswith("flt-semantics")
        or _normalized_text(item.get("structural_navigation_kind")) == "flutter_bottom_navigation"
    )


def _semantic_label(item: Mapping[str, object]) -> str:
    return "".join(
        character
        for character in _normalized_text(item.get("aria_label") or item.get("text"))
        if not character.isspace()
    )


def _has_content_context(item: Mapping[str, object]) -> bool:
    tokens = [
        *(item.get("class_tokens", []) if isinstance(item.get("class_tokens"), list) else []),
        *(
            item.get("ancestor_class_tokens", [])
            if isinstance(item.get("ancestor_class_tokens"), list)
            else []
        ),
    ]
    return any(_normalized_text(token) in _CONTENT_MARKERS for token in tokens)


def _text_char_count(value: object) -> int:
    return len(
        "".join(
            character for character in _normalized_text(value) if not character.isspace()
        )
    )


def _target_decision(
    allowed: bool, reason_code: str | None, target_kind: str, expected_page_type: str
) -> VisualNavigationTargetDecision:
    semantic_target = (
        f"进入 {expected_page_type} 页面"
        if target_kind in VISUAL_TARGET_CONFIDENCE_FLOORS and expected_page_type
        else None
    )
    return VisualNavigationTargetDecision(
        allowed=allowed,
        reason_code=reason_code,
        target_kind=target_kind,
        expected_page_type=expected_page_type,
        risk_level=VISUAL_TARGET_RISK_LEVELS.get(target_kind),
        semantic_target=semantic_target,
        target_keywords=("visual_navigation", target_kind, expected_page_type),
    )


def _has_unsafe_visual_risk(
    *,
    risk_flags: Sequence[object],
    label: object,
) -> bool:
    normalized_flags = tuple(
        item for item in (_normalized_text(flag) for flag in risk_flags) if item
    )
    if normalized_flags:
        return True
    normalized_label = _normalized_text(label)
    if not normalized_label:
        return False
    label_tokens = frozenset(re.findall(r"[a-z0-9_]+", normalized_label))
    return any(
        marker in normalized_label
        if any("\u4e00" <= character <= "\u9fff" for character in marker)
        else marker in label_tokens
        for marker in _UNSAFE_VISUAL_RISK_MARKERS
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise VisualNavigationCapabilityError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _normalized_label(value: object) -> str:
    if not isinstance(value, str):
        raise VisualNavigationContractError("label 必须为文本")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    compact = "".join(normalized.split())
    if not compact:
        raise VisualNavigationContractError("label 规范化后不能为空")
    return compact


def _required_digest(value: object, field_name: str) -> str:
    text = _required_text(value, field_name)
    if not _DIGEST_PATTERN.fullmatch(text):
        raise VisualNavigationCapabilityError(f"{field_name} 必须为 SHA-256 摘要")
    return text


def _semantics_level(value: object) -> str:
    result = _required_text(value, "semantics_coverage_level")
    if result not in _SEMANTICS_LEVELS:
        raise VisualNavigationCapabilityError("semantics_coverage_level 不受支持")
    return result


def _sparse_semantics_level(value: object) -> str:
    result = _semantics_level(value)
    if result != "semantics_sparse":
        raise VisualNavigationContractError("视觉导航能力只允许 semantics_sparse")
    return result


def _target_kind(value: object) -> str:
    result = _required_text(value, "target_kind")
    if result not in VISUAL_TARGET_CONFIDENCE_FLOORS:
        raise VisualNavigationCapabilityError("target_kind 不在视觉导航白名单")
    return result


def _confidence(value: object, field_name: str) -> float:
    return round(_raw_unit_number(value, field_name), 4)


def _raw_unit_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualNavigationCapabilityError(f"{field_name} 必须为有限数值")
    result = float(value)
    if not math.isfinite(result):
        raise VisualNavigationCapabilityError(f"{field_name} 必须为有限数值")
    if not 0.0 <= result <= 1.0:
        raise VisualNavigationCapabilityError(f"{field_name} 必须在 0 到 1 之间")
    return result


def _normalized_rect(value: object) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or set(value) != {"x", "y", "width", "height"}:
        raise VisualNavigationCapabilityError("normalized_rect 必须包含 x、y、width、height")
    raw = {
        key: _raw_unit_number(value[key], f"normalized_rect.{key}")
        for key in value
    }
    if raw["x"] + raw["width"] > 1 or raw["y"] + raw["height"] > 1:
        raise VisualNavigationCapabilityError("normalized_rect 超出视觉视口")
    normalized = {key: round(number, 6) for key, number in raw.items()}
    if normalized["width"] <= 0 or normalized["height"] <= 0:
        raise VisualNavigationCapabilityError("normalized_rect 宽高必须为正数")
    if normalized["x"] + normalized["width"] > 1 or normalized["y"] + normalized["height"] > 1:
        raise VisualNavigationCapabilityError("normalized_rect 超出视觉视口")
    return MappingProxyType(normalized)


def _source_image_size(value: object) -> Mapping[str, object]:
    result = _frozen_mapping(value, "source_image_size")
    if set(result) != {"width", "height"}:
        raise VisualNavigationContractError("source_image_size 字段不完整")
    for field_name in ("width", "height"):
        number = result[field_name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise VisualNavigationContractError(
                f"source_image_size.{field_name} 必须为正整数"
            )
    return result


def _viewport_metrics(value: object) -> Mapping[str, object]:
    result = _frozen_mapping(value, "viewport_metrics")
    expected_fields = {
        "width",
        "height",
        "offset_left",
        "offset_top",
        "page_left",
        "page_top",
        "scale",
    }
    if set(result) != expected_fields:
        raise VisualNavigationContractError("viewport_metrics 字段不完整")
    for field_name in ("width", "height"):
        number = result.get(field_name)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number <= 0
        ):
            raise VisualNavigationCapabilityError(f"viewport_metrics.{field_name} 必须为正数")
    for field_name in ("offset_left", "offset_top", "page_left", "page_top"):
        _finite_geometry_number(result[field_name], f"viewport_metrics.{field_name}")
    scale = _finite_geometry_number(result["scale"], "viewport_metrics.scale")
    if scale <= 0:
        raise VisualNavigationContractError("viewport_metrics.scale 必须为正数")
    return result


def _render_profile(value: object) -> Mapping[str, object]:
    result = _frozen_mapping(value, "render_profile")
    if result.get("kind") not in {"flutter_web", "hybrid"}:
        raise VisualNavigationContractError("render_profile.kind 不允许视觉导航")
    if result.get("status") != "confirmed":
        raise VisualNavigationContractError("render_profile.status 必须为 confirmed")
    return result


def _identity_model_transform(value: object) -> Mapping[str, object]:
    result = _frozen_mapping(value, "model_image_transform")
    expected = {
        "kind": "identity",
        "scale_x": 1.0,
        "scale_y": 1.0,
        "offset_x": 0.0,
        "offset_y": 0.0,
    }
    if dict(result) != expected:
        raise VisualNavigationContractError("v1 model_image_transform 必须为 identity")
    return result


def _risk_flags(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise VisualNavigationContractError("risk_flags 必须为数组")
    if len(value) > 8:
        raise VisualNavigationContractError("risk_flags 超过数量上限")
    result = tuple(_required_text(item, "risk_flags[]").casefold() for item in value)
    if len(result) != len(set(result)):
        raise VisualNavigationContractError("risk_flags 不得重复")
    return result


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise VisualNavigationContractError(f"{field_name} 必须为布尔值")
    return value


def _required_true(value: object, field_name: str) -> bool:
    if value is not True:
        raise VisualNavigationContractError(f"{field_name} 必须为 true")
    return True


def _verify_capability_policy(capability: VisualNavigationCapability) -> None:
    decision = evaluate_visual_navigation_target(
        target_kind=capability.target_kind,
        source_page_type=capability.source_page_type,
        source_page_type_confidence=capability.source_page_type_confidence,
        source_identity_bound=capability.source_identity_bound,
        expected_page_type=capability.expected_page_type,
        visual_target_confidence=capability.visual_target_confidence,
        selected=capability.selected,
        risk_flags=capability.risk_flags,
        normalized_rect=capability.normalized_rect,
        unique=capability.unique,
    )
    if not decision.allowed:
        raise VisualNavigationContractError(
            f"视觉导航能力未通过复验策略：{decision.reason_code}"
        )


def _frozen_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise VisualNavigationCapabilityError(f"{field_name} 必须为对象")
    try:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise VisualNavigationCapabilityError(f"{field_name} 必须是 JSON 对象") from exc
    if not isinstance(normalized, dict):
        raise VisualNavigationCapabilityError(f"{field_name} 必须为对象")
    return MappingProxyType(normalized)


def _normalized_text(value: object) -> str:
    return str(value or "").strip().casefold()
