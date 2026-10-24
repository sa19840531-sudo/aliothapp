"""视觉导航最新帧复验、唯一性判定与 CSS 坐标换算。"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from services.api.app.domain.exploration.h5_logical_surface import (
    compare_h5_logical_surfaces,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    PRIMARY_NAVIGATION_PAGE_TYPES,
    VISUAL_TARGET_EXPECTED_PAGE_TYPES,
    VisualNavigationCapability,
)

# 所有视觉安全阈值集中在本模块，Browser 只消费稳定判定结果。
MAX_RUNTIME_IMAGE_DIMENSION = 8192
MAX_RUNTIME_IMAGE_PIXELS = 16_000_000
MIN_TEMPLATE_MATCH_SCORE = 0.84
MIN_DIRECT_LOCAL_SCORE = 0.86
MIN_DIRECT_ANCHOR_SCORE = 0.76
MIN_OBSTRUCTION_ANCHOR_SCORE = 0.72
MIN_AREA_CHANGE_MATCH_SCORE = 0.6
MIN_RELOCATION_LOCAL_SCORE = 0.86
MIN_RELOCATION_ANCHOR_SCORE = 0.76
UNIQUE_MATCH_SCORE_MARGIN = 0.02
POSITION_DRIFT_TOLERANCE_CSS_PX = 2.0
MAX_RELOCATION_DISTANCE_CSS_PX = 24.0
EDGE_DENSITY_MIN_RATIO = 0.45
EDGE_DENSITY_MAX_RATIO = 1.8
ANCHOR_PADDING_RATIO = 0.5
MAX_TEMPLATE_CANDIDATES = 4
VIEWPORT_CSS_TOLERANCE = 0.1
VISUAL_VIEWPORT_SCALE_TOLERANCE = 0.01
DEVICE_PIXEL_RATIO_TOLERANCE = 0.01
PIXEL_SCALE_TOLERANCE = 0.02
SUPPORTED_AREA_SCALES = (0.75, 0.8, 1.2, 1.25, 4 / 3)
VISUAL_LAYOUT_PROTOCOL_VERSION = "visual_layout_evidence.v1"
VISUAL_TRANSITION_PROOF_PROTOCOL_VERSION = "visual_navigation_transition_proof.v1"
MAIN_STRUCTURE_CHANGE_MIN_RATIO = 0.08
AFTER_STRUCTURE_STABILITY_MAX_RATIO = 0.025
RELATIVE_OVERLAY_PIXEL_DELTA = 24
RELATIVE_OVERLAY_MIN_AREA_RATIO = 0.08
RELATIVE_OVERLAY_MIN_WIDTH_RATIO = 0.55
RELATIVE_OVERLAY_MIN_HEIGHT_RATIO = 0.15
RELATIVE_DIM_MIN_DELTA = 18.0
RELATIVE_LOW_STRUCTURE_EDGE_MAX_RATIO = 0.018
RELATIVE_MAIN_TAKEOVER_MIN_AREA_RATIO = 0.65
RELATIVE_MAIN_TAKEOVER_MIN_WIDTH_RATIO = 0.95
RELATIVE_MAIN_TAKEOVER_MIN_HEIGHT_RATIO = 0.9
RELATIVE_MAIN_TAKEOVER_EDGE_MIN_RATIO = 0.065
PAGE_FRAME_TOP_EDGE_OVERLAP_MIN_RATIO = 0.55
PAGE_FRAME_BOTTOM_EDGE_OVERLAP_MIN_RATIO = 0.45
PAGE_FRAME_MIN_BOTTOM_NAVIGATION_SLOTS = 3

_IDENTITY_MODEL_TRANSFORM = {
    "kind": "identity",
    "scale_x": 1.0,
    "scale_y": 1.0,
    "offset_x": 0.0,
    "offset_y": 0.0,
}
_VIEWPORT_FIELDS = (
    "width",
    "height",
    "offset_left",
    "offset_top",
    "page_left",
    "page_top",
    "scale",
)


@dataclass(frozen=True, slots=True)
class VisualCoordinateResolution:
    """只向 Browser 返回可执行 CSS 点与脱敏判定，不保留图像材料。"""

    css_x: float | None
    css_y: float | None
    relocated: bool
    match_score: float
    audit_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedCoordinateContext:
    source_image: np.ndarray
    current_image: np.ndarray
    source_rect: tuple[int, int, int, int]
    viewport: Mapping[str, float]
    pixel_scale_x: float
    pixel_scale_y: float


def extract_visual_layout_evidence(*, viewport_png: bytes) -> dict[str, object]:
    """从视口 PNG 提取有界结构身份；动态像素事实只进入 state audit。"""
    image = _decode_png(viewport_png)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    edges = cv2.Canny(gray, 50, 150)
    top_end = max(1, round(height * 0.17))
    main_end = max(top_end + 1, round(height * 0.86))
    top_structure = _top_structure(edges[:top_end], image_width=width)
    bottom_navigation = _bottom_navigation_structure(
        gray[main_end:],
        edges[main_end:],
        image_width=width,
    )
    (
        main_layout_category,
        main_component_count,
        destination_facts,
    ) = _main_layout_category(
        gray[top_end:main_end],
        edges=edges[top_end:main_end],
        image_width=width,
    )
    main_edge_density = float(
        np.count_nonzero(edges[top_end:main_end])
        / max(edges[top_end:main_end].size, 1)
    )
    destination_facts["page_chrome"] = str(top_structure["category"])
    destination_facts["page_chrome_texture_bucket"] = _density_bucket(
        float(np.count_nonzero(edges[:top_end]) / max(edges[:top_end].size, 1))
    )
    return {
        "protocol_version": VISUAL_LAYOUT_PROTOCOL_VERSION,
        "identity": {
            "top_structure": top_structure,
            "bottom_navigation": bottom_navigation,
            "main_layout_category": main_layout_category,
        },
        "state_audit": {
            "main_density_bucket": _density_bucket(main_edge_density),
            "main_component_count_bucket": _count_bucket(main_component_count),
            "destination_facts": destination_facts,
            "obstruction_bucket": "transition_context_required",
            "animation_bucket": "unknown",
            "full_frame_change_bucket": "not_compared",
        },
    }


def assess_visual_navigation_transition(
    *,
    source_png: bytes | None = None,
    before_png: bytes,
    after_pngs: tuple[bytes, bytes] | list[bytes],
    capability: VisualNavigationCapability | object,
    before_surface: Mapping[str, Any] | None,
    after_surface: Mapping[str, Any] | None,
) -> dict[str, object]:
    """以两张稳定 after 帧为主证明，DOM/URL/语义只保留为辅助证据。"""
    if not isinstance(after_pngs, (tuple, list)) or len(after_pngs) != 2:
        proof = _visual_transition_proof(
            status="insufficient",
            after_frames_stable=False,
            main_structure_changed=False,
            destination_structure="unknown",
            after_identity_bound=False,
            reason_codes=("stable_after_frame_pair_required",),
        )
        return compare_h5_logical_surfaces(
            before=before_surface,
            after=after_surface,
            visual_proof=proof,
        )
    try:
        immutable_source_png = before_png if source_png is None else source_png
        _decode_png(immutable_source_png)
        before_image = _decode_png(before_png)
        after_images = (_decode_png(after_pngs[0]), _decode_png(after_pngs[1]))
        source_layout = extract_visual_layout_evidence(
            viewport_png=immutable_source_png
        )
        before_layout = extract_visual_layout_evidence(viewport_png=before_png)
        after_layouts = (
            extract_visual_layout_evidence(viewport_png=after_pngs[0]),
            extract_visual_layout_evidence(viewport_png=after_pngs[1]),
        )
    except (cv2.error, TypeError, ValueError):
        proof = _visual_transition_proof(
            status="insufficient",
            after_frames_stable=False,
            main_structure_changed=False,
            destination_structure="unknown",
            after_identity_bound=False,
            reason_codes=("visual_frame_invalid",),
        )
        return compare_h5_logical_surfaces(
            before=before_surface,
            after=after_surface,
            visual_proof=proof,
        )

    after_identity_left = _layout_identity(after_layouts[0])
    after_identity_right = _layout_identity(after_layouts[1])
    after_stability_ratio = _main_structure_difference_ratio(
        after_images[0],
        after_images[1],
    )
    after_frames_stable = bool(
        after_identity_left == after_identity_right
        and _layout_destination_facts(after_layouts[0])
        == _layout_destination_facts(after_layouts[1])
        and after_stability_ratio <= AFTER_STRUCTURE_STABILITY_MAX_RATIO
    )
    main_change_ratio = min(
        _main_structure_difference_ratio(before_image, after_images[0]),
        _main_structure_difference_ratio(before_image, after_images[1]),
    )
    main_structure_changed = main_change_ratio >= MAIN_STRUCTURE_CHANGE_MIN_RATIO
    destination_structure = str(
        after_identity_right.get("main_layout_category") or "unknown"
    )
    after_identity_bound = _surface_visual_layout_is_bound(
        after_surface,
        after_identity_right,
    )
    before_identity_bound = _surface_visual_layout_is_bound(
        before_surface,
        _layout_identity(before_layout),
    )
    source_binding_reasons = _visual_source_binding_reasons(
        capability=capability,
        source_png=immutable_source_png,
        source_layout=_layout_identity(source_layout),
        live_before_layout=_layout_identity(before_layout),
        before_surface=before_surface,
        enforce_complete_binding=source_png is not None,
    )
    target_kind = str(getattr(capability, "target_kind", "") or "")
    expected_page_type = str(
        getattr(capability, "expected_page_type", "") or ""
    ).strip()
    source_page_type = str(getattr(capability, "source_page_type", "") or "").strip()
    before_page_type = _bound_page_type(before_surface)
    reasons: list[str] = list(source_binding_reasons)
    destination_facts = _layout_destination_facts(after_layouts[1])
    destination_frame_verified = _destination_page_frame_is_verified(
        before_image=before_image,
        after_image=after_images[1],
        before_layout=before_layout,
        after_layout=after_layouts[1],
    )
    surface_obstruction = _surface_visual_obstruction_present(after_surface)
    relative_obstruction = _relative_visual_obstruction_present(
        before_image=before_image,
        after_image=after_images[1],
        destination_facts=destination_facts,
        target_kind=target_kind,
        destination_structure=destination_structure,
        destination_frame_verified=destination_frame_verified,
    )
    if surface_obstruction or relative_obstruction:
        reasons.append("visual_destination_obstructed")
    if not after_frames_stable:
        reasons.append("after_frames_inconsistent")
    if not main_structure_changed:
        reasons.append("main_structure_not_changed")
    if not before_identity_bound or not after_identity_bound:
        reasons.append("visual_layout_identity_unbound")
    if not _visual_intent_is_consistent(
        target_kind=target_kind,
        expected_page_type=expected_page_type,
    ):
        reasons.append("destination_intent_mismatch")
    if before_page_type and source_page_type and before_page_type != source_page_type:
        reasons.append("source_page_type_mismatch")

    if target_kind == "primary_navigation":
        before_nav = _bottom_navigation(_layout_identity(before_layout))
        after_nav = _bottom_navigation(after_identity_right)
        target_slot = _target_navigation_slot(
            capability,
            slot_count=after_nav.get("slot_count"),
        )
        if (
            target_slot is None
            or before_nav.get("active_slot") == target_slot
            or after_nav.get("active_slot") != target_slot
        ):
            reasons.append("target_navigation_selection_not_changed")
    elif target_kind in {"content_card", "video_card"}:
        if not _detail_destination_is_sufficient(
            destination_structure=destination_structure,
            destination_facts=destination_facts,
        ):
            reasons.append("detail_destination_structure_insufficient")
        elif not destination_frame_verified:
            reasons.append("destination_page_frame_unverified")
    elif target_kind in {"profile_settings", "profile_payment", "profile_activity"}:
        expected_structure = {
            "profile_settings": "settings_form",
            "profile_payment": "payment_panel",
            "profile_activity": "activity_dashboard",
        }[target_kind]
        if source_page_type != "profile" or before_page_type not in {"", "profile"}:
            reasons.append("profile_source_required")
        if not _profile_destination_is_sufficient(
            expected_structure=expected_structure,
            destination_structure=destination_structure,
            destination_facts=destination_facts,
        ):
            reasons.append("profile_destination_structure_missing")
        elif not destination_frame_verified:
            reasons.append("destination_page_frame_unverified")
    else:
        reasons.append("visual_target_kind_unsupported")

    no_effect = bool(
        after_frames_stable
        and not main_structure_changed
        and _layout_identity(before_layout) == after_identity_right
    )
    status = (
        "stale"
        if source_binding_reasons
        else "no_effect"
        if no_effect
        else "trusted"
        if not reasons
        else "insufficient"
    )
    if no_effect and not source_binding_reasons:
        reasons = ["visual_navigation_no_effect"]
    proof = _visual_transition_proof(
        status=status,
        after_frames_stable=after_frames_stable,
        main_structure_changed=main_structure_changed,
        destination_structure=destination_structure,
        after_identity_bound=after_identity_bound,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    return compare_h5_logical_surfaces(
        before=before_surface,
        after=after_surface,
        visual_proof=proof,
    )


def _visual_source_binding_reasons(
    *,
    capability: object,
    source_png: bytes,
    source_layout: Mapping[str, object],
    live_before_layout: Mapping[str, object],
    before_surface: Mapping[str, Any] | None,
    enforce_complete_binding: bool,
) -> tuple[str, ...]:
    """独立核对不可变来源、逻辑身份/状态与实时前帧视觉身份。"""
    reasons: list[str] = []
    expected_sha = str(
        getattr(capability, "source_artifact_sha256", "") or ""
    )
    actual_sha = f"sha256:{hashlib.sha256(source_png).hexdigest()}"
    if expected_sha:
        if expected_sha != actual_sha:
            reasons.append("visual_source_artifact_mismatch")
    elif enforce_complete_binding:
        reasons.append("visual_source_artifact_unbound")

    before_identity = str(
        before_surface.get("identity_fingerprint") or ""
        if isinstance(before_surface, Mapping)
        else ""
    )
    expected_identity = str(
        getattr(capability, "source_identity_fingerprint", "") or ""
    )
    if expected_identity:
        if expected_identity != before_identity:
            reasons.append("visual_source_identity_mismatch")
    elif enforce_complete_binding:
        reasons.append("visual_source_identity_unbound")

    before_state = str(
        before_surface.get("state_fingerprint") or ""
        if isinstance(before_surface, Mapping)
        else ""
    )
    expected_state = str(
        getattr(capability, "source_state_fingerprint", "") or ""
    )
    if expected_state:
        if expected_state != before_state:
            reasons.append("visual_source_state_mismatch")
    elif enforce_complete_binding:
        reasons.append("visual_source_state_unbound")

    if dict(source_layout) != dict(live_before_layout):
        reasons.append("visual_source_layout_stale")
    return tuple(reasons)


def _visual_transition_proof(
    *,
    status: str,
    after_frames_stable: bool,
    main_structure_changed: bool,
    destination_structure: str,
    after_identity_bound: bool,
    reason_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "protocol_version": VISUAL_TRANSITION_PROOF_PROTOCOL_VERSION,
        "status": status,
        "after_frame_count": 2,
        "after_frames_stable": after_frames_stable,
        "main_structure_changed": main_structure_changed,
        "main_change_bucket": (
            "substantial" if main_structure_changed else "none_or_local"
        ),
        "destination_structure": destination_structure,
        "after_identity_bound": after_identity_bound,
        "reason_codes": list(reason_codes),
    }


def resolve_visual_navigation_coordinate(
    *,
    source_png: bytes,
    current_png: bytes,
    capability: VisualNavigationCapability,
    current_viewport: Mapping[str, object],
) -> VisualCoordinateResolution:
    """在最新视口帧复验旧签名位置；漂移只报告，不改框执行。"""
    try:
        context = _validate_coordinate_context(
            source_png=source_png,
            current_png=current_png,
            capability=capability,
            current_viewport=current_viewport,
        )
    except (cv2.error, TypeError, ValueError):
        return _failure("VISUAL_TARGET_COORDINATE_INVALID")

    left, top, right, bottom = context.source_rect
    source_gray = cv2.cvtColor(context.source_image, cv2.COLOR_BGR2GRAY)
    current_gray = cv2.cvtColor(context.current_image, cv2.COLOR_BGR2GRAY)
    template = source_gray[top:bottom, left:right]
    if template.size == 0 or min(template.shape) < 4:
        return _failure("VISUAL_TARGET_COORDINATE_INVALID")

    current_local = current_gray[top:bottom, left:right]
    local_score = _perceptual_region_score(template, current_local)
    # 零方差框没有目标特征；不同 OpenCV 会产生不同伪满分，禁止参与匹配授权。
    if np.ptp(template) == 0:
        return _failure("VISUAL_TARGET_OBSTRUCTED", match_score=local_score)
    anchor_score = _surrounding_anchor_score(
        source_gray,
        current_gray,
        context.source_rect,
    )
    score_map = _template_score_map(current_gray, template)
    candidates = _distinct_template_candidates(
        score_map,
        template_width=template.shape[1],
        template_height=template.shape[0],
    )
    best_score = candidates[0][2] if candidates else 0.0

    if len(candidates) > 1 and candidates[1][2] >= (
        candidates[0][2] - UNIQUE_MATCH_SCORE_MARGIN
    ):
        return _failure(
            "VISUAL_TARGET_NOT_UNIQUE",
            match_score=best_score,
        )

    if candidates:
        candidate_x, candidate_y, best_score = candidates[0]
        candidate_rect = (
            candidate_x,
            candidate_y,
            candidate_x + template.shape[1],
            candidate_y + template.shape[0],
        )
        candidate_local = current_gray[
            candidate_rect[1] : candidate_rect[3],
            candidate_rect[0] : candidate_rect[2],
        ]
        candidate_local_score = _perceptual_region_score(
            template,
            candidate_local,
        )
        candidate_anchor_score = _surrounding_anchor_score(
            source_gray,
            current_gray,
            context.source_rect,
            current_rect=candidate_rect,
        )
        drift_css = math.hypot(
            (candidate_x - left) / context.pixel_scale_x,
            (candidate_y - top) / context.pixel_scale_y,
        )
        if drift_css > POSITION_DRIFT_TOLERANCE_CSS_PX:
            if (
                drift_css <= MAX_RELOCATION_DISTANCE_CSS_PX
                and candidate_local_score >= MIN_RELOCATION_LOCAL_SCORE
                and candidate_anchor_score >= MIN_RELOCATION_ANCHOR_SCORE
            ):
                return VisualCoordinateResolution(
                    css_x=None,
                    css_y=None,
                    relocated=True,
                    match_score=_bounded_score(
                        min(best_score, candidate_local_score, candidate_anchor_score)
                    ),
                    audit_codes=(
                        "VISUAL_TARGET_SOURCE_STATE_STALE",
                        "relocation_candidate_found",
                    ),
                )
            return _failure(
                "VISUAL_TARGET_SOURCE_STATE_STALE",
                match_score=min(best_score, candidate_local_score),
            )
        direct_local_score = max(local_score, candidate_local_score)
        direct_anchor_score = max(anchor_score, candidate_anchor_score)
        # 局部像素相同不能证明仍是最上层目标（透明遮罩反例）；
        # 外围无害动画仍须满足周边锚点门槛，禁止用 exact-local 绕过。
        if (
            direct_local_score >= MIN_DIRECT_LOCAL_SCORE
            and direct_anchor_score >= MIN_DIRECT_ANCHOR_SCORE
        ):
            center_x = (left + right) / 2
            center_y = (top + bottom) / 2
            css_x = context.viewport["offset_left"] + center_x / context.pixel_scale_x
            css_y = context.viewport["offset_top"] + center_y / context.pixel_scale_y
            if not _point_is_inside_visual_viewport(
                x=css_x,
                y=css_y,
                viewport=context.viewport,
            ):
                return _failure("VISUAL_TARGET_COORDINATE_INVALID")
            return VisualCoordinateResolution(
                css_x=round(css_x, 6),
                css_y=round(css_y, 6),
                relocated=False,
                match_score=_bounded_score(max(best_score, local_score)),
                audit_codes=(),
            )

    if _looks_obstructed_or_resized(
        source_local=template,
        current_local=current_local,
        current_gray=current_gray,
        source_rect=context.source_rect,
        anchor_score=anchor_score,
    ):
        return _failure(
            "VISUAL_TARGET_OBSTRUCTED",
            match_score=max(best_score, local_score),
        )
    return _failure(
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        match_score=max(best_score, local_score),
    )


def _validate_coordinate_context(
    *,
    source_png: bytes,
    current_png: bytes,
    capability: VisualNavigationCapability,
    current_viewport: Mapping[str, object],
) -> _ValidatedCoordinateContext:
    source_image = _decode_png(source_png)
    current_image = _decode_png(current_png)
    source_height, source_width = source_image.shape[:2]
    current_height, current_width = current_image.shape[:2]
    source_size = _mapping_field(capability, "source_image_size")
    if set(source_size) != {"width", "height"}:
        raise ValueError("来源图片尺寸不完整")
    if (
        _positive_integer(source_size["width"]) != source_width
        or _positive_integer(source_size["height"]) != source_height
    ):
        raise ValueError("来源图片尺寸不匹配")
    transform = _mapping_field(capability, "model_image_transform")
    if dict(transform) != _IDENTITY_MODEL_TRANSFORM:
        raise ValueError("v1 只允许原始 PNG identity 变换")
    normalized_rect = _mapping_field(capability, "normalized_rect")
    source_rect = _source_pixel_rect(
        normalized_rect,
        image_width=source_width,
        image_height=source_height,
    )
    sealed_viewport = _numeric_viewport(
        _mapping_field(capability, "viewport_metrics"),
        require_runtime_fields=False,
    )
    runtime_viewport = _numeric_viewport(
        current_viewport,
        require_runtime_fields=True,
    )
    if current_viewport.get("full_page") is not False:
        raise ValueError("视觉导航只允许视口截图")
    for field_name in _VIEWPORT_FIELDS:
        tolerance = (
            VISUAL_VIEWPORT_SCALE_TOLERANCE
            if field_name == "scale"
            else VIEWPORT_CSS_TOLERANCE
        )
        if not math.isclose(
            sealed_viewport[field_name],
            runtime_viewport[field_name],
            rel_tol=0,
            abs_tol=tolerance,
        ):
            raise ValueError("实时视口与签名来源不一致")

    source_scale_x = source_width / sealed_viewport["width"]
    source_scale_y = source_height / sealed_viewport["height"]
    current_scale_x = current_width / runtime_viewport["width"]
    current_scale_y = current_height / runtime_viewport["height"]
    expected_current_scale = (
        runtime_viewport["device_pixel_ratio"] * runtime_viewport["scale"]
    )
    source_dpr_x = source_scale_x / sealed_viewport["scale"]
    source_dpr_y = source_scale_y / sealed_viewport["scale"]
    for left_value, right_value in (
        (source_scale_x, source_scale_y),
        (current_scale_x, current_scale_y),
        (source_scale_x, current_scale_x),
        (source_scale_y, current_scale_y),
        (current_scale_x, expected_current_scale),
        (current_scale_y, expected_current_scale),
    ):
        if not math.isclose(
            left_value,
            right_value,
            rel_tol=PIXEL_SCALE_TOLERANCE,
            abs_tol=PIXEL_SCALE_TOLERANCE,
        ):
            raise ValueError("截图像素密度发生变化")
    for inferred_dpr in (source_dpr_x, source_dpr_y):
        if not math.isclose(
            inferred_dpr,
            runtime_viewport["device_pixel_ratio"],
            rel_tol=DEVICE_PIXEL_RATIO_TOLERANCE,
            abs_tol=DEVICE_PIXEL_RATIO_TOLERANCE,
        ):
            raise ValueError("截图 DPR 发生变化")
    return _ValidatedCoordinateContext(
        source_image=source_image,
        current_image=current_image,
        source_rect=source_rect,
        viewport=runtime_viewport,
        pixel_scale_x=current_scale_x,
        pixel_scale_y=current_scale_y,
    )


def _decode_png(payload: object) -> np.ndarray:
    if not isinstance(payload, bytes) or len(payload) < 33:
        raise ValueError("视觉帧必须为 PNG bytes")
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("视觉帧无法解码")
    height, width = image.shape[:2]
    if (
        width <= 0
        or height <= 0
        or width > MAX_RUNTIME_IMAGE_DIMENSION
        or height > MAX_RUNTIME_IMAGE_DIMENSION
        or width * height > MAX_RUNTIME_IMAGE_PIXELS
    ):
        raise ValueError("视觉帧尺寸越界")
    return image


def _mapping_field(capability: object, field_name: str) -> Mapping[str, object]:
    value = getattr(capability, field_name, None)
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} 必须为对象")
    return value


def _numeric_viewport(
    value: Mapping[str, object],
    *,
    require_runtime_fields: bool,
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError("viewport 必须为对象")
    required = set(_VIEWPORT_FIELDS)
    if require_runtime_fields:
        required.add("device_pixel_ratio")
    if not required.issubset(value):
        raise ValueError("viewport 字段不完整")
    result = {field_name: _finite_number(value[field_name]) for field_name in required}
    for field_name in ("width", "height", "scale"):
        if result[field_name] <= 0:
            raise ValueError("viewport 尺寸或 scale 无效")
    if require_runtime_fields and result["device_pixel_ratio"] <= 0:
        raise ValueError("device_pixel_ratio 无效")
    return result


def _source_pixel_rect(
    normalized_rect: Mapping[str, object],
    *,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int]:
    if set(normalized_rect) != {"x", "y", "width", "height"}:
        raise ValueError("normalized_rect 字段不完整")
    x = _finite_number(normalized_rect["x"])
    y = _finite_number(normalized_rect["y"])
    width = _finite_number(normalized_rect["width"])
    height = _finite_number(normalized_rect["height"])
    if (
        x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > 1
        or y + height > 1
    ):
        raise ValueError("normalized_rect 越界")
    left = round(x * image_width)
    top = round(y * image_height)
    right = round((x + width) * image_width)
    bottom = round((y + height) * image_height)
    if left < 0 or top < 0 or right > image_width or bottom > image_height:
        raise ValueError("来源矩形越界")
    if right - left < 4 or bottom - top < 4:
        raise ValueError("来源矩形过小")
    return left, top, right, bottom


def _template_score_map(current_gray: np.ndarray, template: np.ndarray) -> np.ndarray:
    if (
        template.shape[0] > current_gray.shape[0]
        or template.shape[1] > current_gray.shape[1]
    ):
        return np.empty((0, 0), dtype=np.float32)
    gray_score = cv2.matchTemplate(current_gray, template, cv2.TM_CCOEFF_NORMED)
    template_edges = cv2.Canny(template, 50, 150)
    if np.count_nonzero(template_edges) < 8:
        return np.nan_to_num(gray_score, nan=-1.0)
    current_edges = cv2.Canny(current_gray, 50, 150)
    edge_score = cv2.matchTemplate(
        current_edges,
        template_edges,
        cv2.TM_CCOEFF_NORMED,
    )
    return np.nan_to_num(gray_score * 0.65 + edge_score * 0.35, nan=-1.0)


def _distinct_template_candidates(
    score_map: np.ndarray,
    *,
    template_width: int,
    template_height: int,
) -> list[tuple[int, int, float]]:
    if score_map.size == 0:
        return []
    working = score_map.copy()
    candidates: list[tuple[int, int, float]] = []
    suppress_x = max(2, template_width // 2)
    suppress_y = max(2, template_height // 2)
    for _ in range(MAX_TEMPLATE_CANDIDATES):
        _, max_value, _, max_location = cv2.minMaxLoc(working)
        if not math.isfinite(max_value) or max_value < MIN_TEMPLATE_MATCH_SCORE:
            break
        x, y = max_location
        candidates.append((x, y, float(max_value)))
        x_start = max(0, x - suppress_x)
        y_start = max(0, y - suppress_y)
        x_end = min(working.shape[1], x + suppress_x + 1)
        y_end = min(working.shape[0], y + suppress_y + 1)
        working[y_start:y_end, x_start:x_end] = -1.0
    return candidates


def _perceptual_region_score(source: np.ndarray, current: np.ndarray) -> float:
    if source.shape != current.shape or source.size == 0:
        return 0.0
    source_small = cv2.resize(source, (32, 32), interpolation=cv2.INTER_AREA)
    current_small = cv2.resize(current, (32, 32), interpolation=cv2.INTER_AREA)
    intensity_score = 1.0 - float(
        np.mean(cv2.absdiff(source_small, current_small)) / 255.0
    )
    source_edges = cv2.Canny(source_small, 50, 150) > 0
    current_edges = cv2.Canny(current_small, 50, 150) > 0
    union = np.count_nonzero(source_edges | current_edges)
    edge_score = (
        1.0
        if union == 0
        else float(np.count_nonzero(source_edges & current_edges) / union)
    )
    source_hist = cv2.calcHist([source_small], [0], None, [16], [0, 256])
    current_hist = cv2.calcHist([current_small], [0], None, [16], [0, 256])
    hist_score = (cv2.compareHist(source_hist, current_hist, cv2.HISTCMP_CORREL) + 1) / 2
    return _bounded_score(intensity_score * 0.45 + edge_score * 0.35 + hist_score * 0.2)


def _surrounding_anchor_score(
    source: np.ndarray,
    current: np.ndarray,
    rect: tuple[int, int, int, int],
    *,
    current_rect: tuple[int, int, int, int] | None = None,
) -> float:
    left, top, right, bottom = rect
    current_left, current_top, current_right, current_bottom = current_rect or rect
    width = right - left
    height = bottom - top
    if current_right - current_left != width or current_bottom - current_top != height:
        return 0.0
    padding_x = max(4, round(width * ANCHOR_PADDING_RATIO))
    padding_y = max(4, round(height * ANCHOR_PADDING_RATIO))
    anchor_left = max(0, left - padding_x)
    anchor_top = max(0, top - padding_y)
    anchor_right = min(source.shape[1], right + padding_x)
    anchor_bottom = min(source.shape[0], bottom + padding_y)
    current_anchor_left = max(0, current_left - padding_x)
    current_anchor_top = max(0, current_top - padding_y)
    current_anchor_right = min(current.shape[1], current_right + padding_x)
    current_anchor_bottom = min(current.shape[0], current_bottom + padding_y)
    source_region = source[anchor_top:anchor_bottom, anchor_left:anchor_right]
    current_region = current[
        current_anchor_top:current_anchor_bottom,
        current_anchor_left:current_anchor_right,
    ]
    if source_region.shape != current_region.shape:
        return 0.0
    mask = np.ones(source_region.shape, dtype=bool)
    mask[
        top - anchor_top : bottom - anchor_top,
        left - anchor_left : right - anchor_left,
    ] = False
    if not np.any(mask):
        return 0.0
    intensity_score = 1.0 - float(
        np.mean(np.abs(source_region[mask].astype(np.int16) - current_region[mask]))
        / 255.0
    )
    source_edges = cv2.Canny(source_region, 50, 150) > 0
    current_edges = cv2.Canny(current_region, 50, 150) > 0
    edge_mask = mask
    union = np.count_nonzero((source_edges | current_edges) & edge_mask)
    edge_score = (
        1.0
        if union == 0
        else float(
            np.count_nonzero((source_edges & current_edges) & edge_mask) / union
        )
    )
    partition_score = _partitioned_anchor_histogram_score(
        source_region=source_region,
        current_region=current_region,
        excluded_rect=(
            left - anchor_left,
            top - anchor_top,
            right - anchor_left,
            bottom - anchor_top,
        ),
    )
    return _bounded_score(
        intensity_score * 0.4 + edge_score * 0.3 + partition_score * 0.3
    )


def _partitioned_anchor_histogram_score(
    *,
    source_region: np.ndarray,
    current_region: np.ndarray,
    excluded_rect: tuple[int, int, int, int],
) -> float:
    """按目标四周分区比较灰度直方图，避免整块背景平均掩盖局部变化。"""
    left, top, right, bottom = excluded_rect
    regions = (
        (0, 0, source_region.shape[1], top),
        (0, bottom, source_region.shape[1], source_region.shape[0]),
        (0, top, left, bottom),
        (right, top, source_region.shape[1], bottom),
    )
    scores: list[float] = []
    for region_left, region_top, region_right, region_bottom in regions:
        if region_right <= region_left or region_bottom <= region_top:
            continue
        source_part = source_region[
            region_top:region_bottom,
            region_left:region_right,
        ]
        current_part = current_region[
            region_top:region_bottom,
            region_left:region_right,
        ]
        source_hist = cv2.calcHist([source_part], [0], None, [16], [0, 256])
        current_hist = cv2.calcHist([current_part], [0], None, [16], [0, 256])
        correlation = cv2.compareHist(
            source_hist,
            current_hist,
            cv2.HISTCMP_CORREL,
        )
        scores.append((correlation + 1) / 2)
    if not scores:
        return 0.0
    return _bounded_score(float(np.mean(scores)))


def _looks_obstructed_or_resized(
    *,
    source_local: np.ndarray,
    current_local: np.ndarray,
    current_gray: np.ndarray,
    source_rect: tuple[int, int, int, int],
    anchor_score: float,
) -> bool:
    if source_local.shape != current_local.shape or source_local.size == 0:
        return True
    source_density = _edge_density(source_local)
    current_density = _edge_density(current_local)
    density_ratio = current_density / max(source_density, 1e-6)
    if anchor_score >= MIN_OBSTRUCTION_ANCHOR_SCORE and not (
        EDGE_DENSITY_MIN_RATIO <= density_ratio <= EDGE_DENSITY_MAX_RATIO
    ):
        return True
    left, top, right, bottom = source_rect
    padding_x = max(4, round((right - left) * ANCHOR_PADDING_RATIO))
    padding_y = max(4, round((bottom - top) * ANCHOR_PADDING_RATIO))
    search_region = current_gray[
        max(0, top - padding_y) : min(current_gray.shape[0], bottom + padding_y),
        max(0, left - padding_x) : min(current_gray.shape[1], right + padding_x),
    ]
    for scale in SUPPORTED_AREA_SCALES:
        scaled_width = max(4, round(source_local.shape[1] * scale))
        scaled_height = max(4, round(source_local.shape[0] * scale))
        if scaled_width > search_region.shape[1] or scaled_height > search_region.shape[0]:
            continue
        scaled = cv2.resize(
            source_local,
            (scaled_width, scaled_height),
            interpolation=(cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC),
        )
        score_map = _template_score_map(search_region, scaled)
        if score_map.size and cv2.minMaxLoc(score_map)[1] >= MIN_AREA_CHANGE_MATCH_SCORE:
            return True
    return False


def _edge_density(image: np.ndarray) -> float:
    return float(np.count_nonzero(cv2.Canny(image, 50, 150)) / max(image.size, 1))


def _top_structure(edges: np.ndarray, *, image_width: int) -> dict[str, object]:
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    horizontal_segments = [
        cv2.boundingRect(contour)
        for contour in contours
        if cv2.boundingRect(contour)[2] >= image_width * 0.25
    ]
    segment_count = min(len(horizontal_segments), 4)
    category = (
        "app_bar"
        if segment_count == 1
        else "segmented_header"
        if segment_count > 1
        else "sparse_header"
    )
    return {"category": category, "segment_count": segment_count}


def _bottom_navigation_structure(
    gray: np.ndarray,
    edges: np.ndarray,
    *,
    image_width: int,
) -> dict[str, object]:
    # 底栏顶部分隔线会横跨整宽，必须先排除后再按图标/文字列聚类。
    slot_edges = edges[max(1, round(edges.shape[0] * 0.18)) :]
    projection = np.count_nonzero(slot_edges, axis=0)
    occupied = (projection >= 2).astype(np.uint8).reshape(1, -1)
    occupied = cv2.morphologyEx(
        occupied,
        cv2.MORPH_CLOSE,
        np.ones((1, max(3, image_width // 80)), dtype=np.uint8),
    ).reshape(-1)
    groups: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate((*occupied.tolist(), 0)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= max(4, image_width // 40):
                groups.append((start, index))
            start = None
    centers = [round((left + right - 1) / 2) for left, right in groups]
    if len(centers) > 8:
        centers = centers[:8]
    slot_count = len(centers)
    active_slot: int | None = None
    del gray
    marker_band = edges[
        max(0, edges.shape[0] - max(5, round(edges.shape[0] * 0.18))) :
    ]
    marker_contours, _ = cv2.findContours(
        marker_band,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    marker_centers = [
        x + width / 2
        for contour in marker_contours
        for x, _y, width, height in (cv2.boundingRect(contour),)
        if width >= image_width * 0.12 and height >= 2
    ]
    if slot_count and len(marker_centers) == 1:
        active_slot = min(
            range(slot_count),
            key=lambda index: abs(centers[index] - marker_centers[0]),
        )
    return {"slot_count": slot_count, "active_slot": active_slot}


def _main_layout_category(
    gray: np.ndarray,
    *,
    edges: np.ndarray,
    image_width: int,
) -> tuple[str, int, dict[str, int]]:
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    height = gray.shape[0]
    boxes = _deduplicate_boxes(
        cv2.boundingRect(contour)
        for contour in contours
        if cv2.contourArea(contour) >= max(10, image_width * height * 0.00015)
    )
    wide_rows = [
        box
        for box in boxes
        if box[2] >= image_width * 0.62 and height * 0.04 <= box[3] <= height * 0.18
    ]
    medium_cards = [
        box
        for box in boxes
        if image_width * 0.32 <= box[2] <= image_width * 0.52
        and height * 0.12 <= box[3] <= height * 0.36
    ]
    large_blocks = [
        box
        for box in boxes
        if box[2] >= image_width * 0.7 and box[3] >= height * 0.25
    ]
    horizontal_bands = [
        box
        for box in boxes
        if box[2] >= image_width * 0.55 and 2 <= box[3] <= height * 0.09
    ]
    profile_markers = [
        box
        for box in boxes
        if box[1] <= height * 0.25
        and image_width * 0.12 <= box[2] <= image_width * 0.28
        and 0.7 <= box[2] / max(box[3], 1) <= 1.3
    ]
    bottom_actions = [
        box
        for box in boxes
        if box[1] + box[3] >= height * 0.78
        and image_width * 0.4 <= box[2] <= image_width * 0.65
        and box[3] >= height * 0.06
    ]
    dashboard_headers = [
        box
        for box in boxes
        if box[2] >= image_width * 0.7
        and height * 0.16 <= box[3] <= height * 0.28
        and box[1] <= height * 0.3
    ]
    if large_blocks and len(wide_rows) >= 2 and bottom_actions:
        category = "payment_panel"
    elif profile_markers and len(wide_rows) >= 3:
        category = "profile_menu"
    elif len(medium_cards) >= 4 and dashboard_headers:
        category = "activity_dashboard"
    elif len(medium_cards) >= 4:
        category = "grid"
    elif large_blocks and (len(horizontal_bands) >= 2 or len(wide_rows) >= 2):
        category = "detail"
    elif len(wide_rows) >= 5:
        category = "settings_form"
    elif len(wide_rows) >= 3:
        category = "list"
    elif len(medium_cards) >= 2:
        category = "dashboard"
    else:
        category = "sparse"
    return (
        category,
        len(boxes),
        {
            "large_region_count": min(len(large_blocks), 8),
            "body_band_count": min(max(len(horizontal_bands), len(wide_rows)), 8),
            "wide_row_count": min(len(wide_rows), 8),
            "medium_card_count": min(len(medium_cards), 8),
            "profile_marker_count": min(len(profile_markers), 4),
            "bottom_action_count": min(len(bottom_actions), 4),
            "dashboard_header_count": min(len(dashboard_headers), 4),
        },
    )


def _deduplicate_boxes(
    boxes: Iterable[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """折叠同一边缘内外轮廓产生的重复几何框。"""
    unique: list[tuple[int, int, int, int]] = []
    for raw in boxes:
        box = tuple(int(value) for value in raw)
        if not any(
            all(abs(left - right) <= 6 for left, right in zip(box, existing, strict=True))
            for existing in unique
        ):
            unique.append(box)
    return unique


def _layout_destination_facts(evidence: Mapping[str, object]) -> dict[str, object]:
    state_audit = evidence.get("state_audit")
    if not isinstance(state_audit, Mapping):
        return {}
    facts = state_audit.get("destination_facts")
    return dict(facts) if isinstance(facts, Mapping) else {}


def _detail_destination_is_sufficient(
    *,
    destination_structure: str,
    destination_facts: Mapping[str, object],
) -> bool:
    """详情必须同时具备页面 chrome、媒体/内容区与多行主体结构。"""
    return bool(
        destination_structure == "detail"
        and _page_chrome_is_present(destination_facts)
        and _safe_count(destination_facts.get("large_region_count")) >= 1
        and _safe_count(destination_facts.get("body_band_count")) >= 2
    )


def _profile_destination_is_sufficient(
    *,
    expected_structure: str,
    destination_structure: str,
    destination_facts: Mapping[str, object],
) -> bool:
    """Profile 本地页必须是带页面级头部的多结构目的地，而非模态层。"""
    if (
        destination_structure != expected_structure
        or not _page_chrome_is_present(destination_facts)
    ):
        return False
    if expected_structure == "settings_form":
        return _safe_count(destination_facts.get("wide_row_count")) >= 5
    if expected_structure == "payment_panel":
        return bool(
            _safe_count(destination_facts.get("large_region_count")) >= 1
            and _safe_count(destination_facts.get("wide_row_count")) >= 2
            and _safe_count(destination_facts.get("bottom_action_count")) >= 1
        )
    if expected_structure == "activity_dashboard":
        return bool(
            _safe_count(destination_facts.get("medium_card_count")) >= 4
            and _safe_count(destination_facts.get("dashboard_header_count")) >= 1
        )
    return False


def _surface_visual_obstruction_present(
    surface: Mapping[str, Any] | None,
) -> bool:
    """只消费独立 DOM/Semantics 量化遮挡事实，不读取业务文本。"""
    if not isinstance(surface, Mapping):
        return False
    state = surface.get("state_signals")
    if not isinstance(state, Mapping):
        return False
    summary = state.get("visual_obstruction_summary")
    if isinstance(summary, Mapping):
        for key in (
            "ad_count",
            "modal_count",
            "overlay_count",
            "spinner_mask_count",
            "outbound_action_count",
        ):
            if _safe_count(summary.get(key)) > 0:
                return True
        if summary.get("close_only") is True:
            return True
    obstructions = state.get("obstructions")
    if isinstance(obstructions, (list, tuple)) and len(obstructions) > 0:
        return True
    return any(
        state.get(key) is True
        for key in ("modal_active", "overlay_active", "ad_active", "spinner_mask_active")
    )


def _destination_is_structurally_sufficient(
    *,
    target_kind: str,
    destination_structure: str,
    destination_facts: Mapping[str, object],
) -> bool:
    if target_kind == "primary_navigation":
        return bool(
            _page_chrome_is_present(destination_facts)
            and destination_structure not in {"", "unknown", "sparse"}
        )
    if target_kind in {"content_card", "video_card"}:
        return _detail_destination_is_sufficient(
            destination_structure=destination_structure,
            destination_facts=destination_facts,
        )
    expected = {
        "profile_settings": "settings_form",
        "profile_payment": "payment_panel",
        "profile_activity": "activity_dashboard",
    }.get(target_kind)
    return bool(
        expected
        and _profile_destination_is_sufficient(
            expected_structure=expected,
            destination_structure=destination_structure,
            destination_facts=destination_facts,
        )
    )


def _page_chrome_is_present(destination_facts: Mapping[str, object]) -> bool:
    category = destination_facts.get("page_chrome")
    return bool(
        category == "app_bar"
        or (
            category == "segmented_header"
            and destination_facts.get("page_chrome_texture_bucket") != "dense"
        )
    )


def _destination_page_frame_is_verified(
    *,
    before_image: np.ndarray,
    after_image: np.ndarray,
    before_layout: Mapping[str, object],
    after_layout: Mapping[str, object],
) -> bool:
    """目的地自身内容不能自证页面成立，必须保留独立页面外框。"""
    if before_image.shape[:2] != after_image.shape[:2]:
        return False
    before_identity = _layout_identity(before_layout)
    after_identity = _layout_identity(after_layout)
    before_navigation = _bottom_navigation(before_identity)
    after_navigation = _bottom_navigation(after_identity)
    before_slots = _safe_count(before_navigation.get("slot_count"))
    after_slots = _safe_count(after_navigation.get("slot_count"))
    height, width = before_image.shape[:2]
    horizontal_margin = max(1, round(width * 0.08))

    bottom_start = max(1, round(height * 0.86))
    persistent_bottom_navigation = bool(
        before_slots >= PAGE_FRAME_MIN_BOTTOM_NAVIGATION_SLOTS
        and after_slots == before_slots
        and _edge_overlap_ratio(
            before_image[bottom_start:, horizontal_margin : width - horizontal_margin],
            after_image[bottom_start:, horizontal_margin : width - horizontal_margin],
        )
        >= PAGE_FRAME_BOTTOM_EDGE_OVERLAP_MIN_RATIO
    )
    if persistent_bottom_navigation:
        return True

    before_facts = _layout_destination_facts(before_layout)
    after_facts = _layout_destination_facts(after_layout)
    if not (
        _page_chrome_is_present(before_facts)
        and _page_chrome_is_present(after_facts)
    ):
        return False
    top_end = max(2, round(height * 0.17))
    chrome_top = max(0, round(top_end * 0.2))
    chrome_bottom = max(chrome_top + 1, round(top_end * 0.8))
    return bool(
        _edge_overlap_ratio(
            before_image[
                chrome_top:chrome_bottom,
                horizontal_margin : width - horizontal_margin,
            ],
            after_image[
                chrome_top:chrome_bottom,
                horizontal_margin : width - horizontal_margin,
            ],
        )
        >= PAGE_FRAME_TOP_EDGE_OVERLAP_MIN_RATIO
    )


def _edge_overlap_ratio(left: np.ndarray, right: np.ndarray) -> float:
    """比较同一页面外框区域的边缘交集，颜色或明暗主题不参与。"""
    if left.shape[:2] != right.shape[:2] or left.size == 0:
        return 0.0
    left_edges = cv2.Canny(cv2.cvtColor(left, cv2.COLOR_BGR2GRAY), 50, 150) > 0
    right_edges = cv2.Canny(cv2.cvtColor(right, cv2.COLOR_BGR2GRAY), 50, 150) > 0
    union = np.count_nonzero(left_edges | right_edges)
    if union == 0:
        return 0.0
    return float(np.count_nonzero(left_edges & right_edges) / union)


def _relative_visual_obstruction_present(
    *,
    before_image: np.ndarray,
    after_image: np.ndarray,
    destination_facts: Mapping[str, object],
    target_kind: str,
    destination_structure: str,
    destination_frame_verified: bool,
) -> bool:
    """只从 before→after 变化拓扑识别覆盖层，不把主题亮度当作遮挡。"""
    if before_image.shape[:2] != after_image.shape[:2]:
        return True
    height, width = before_image.shape[:2]
    top = max(1, round(height * 0.17))
    bottom = max(top + 1, round(height * 0.86))
    before_gray = cv2.cvtColor(before_image[top:bottom], cv2.COLOR_BGR2GRAY)
    after_gray = cv2.cvtColor(after_image[top:bottom], cv2.COLOR_BGR2GRAY)
    difference = cv2.absdiff(before_gray, after_gray)
    changed = (difference >= RELATIVE_OVERLAY_PIXEL_DELTA).astype(np.uint8)
    changed_ratio = float(np.count_nonzero(changed) / max(changed.size, 1))
    valid_destination = _destination_is_structurally_sufficient(
        target_kind=target_kind,
        destination_structure=destination_structure,
        destination_facts=destination_facts,
    ) and destination_frame_verified
    valid_content_detail = bool(
        target_kind in {"content_card", "video_card"}
        and valid_destination
    )
    if _relative_dim_backdrop_panel_present(before_gray, after_gray) and not (
        valid_content_detail
    ):
        return True

    connected_change = cv2.morphologyEx(
        changed,
        cv2.MORPH_CLOSE,
        np.ones((7, 7), dtype=np.uint8),
    )
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        connected_change,
        connectivity=8,
    )
    main_height, main_width = changed.shape
    after_edges = cv2.Canny(after_gray, 50, 150)
    after_edge_density = float(
        np.count_nonzero(after_edges) / max(after_edges.size, 1)
    )
    for index in range(1, component_count):
        left, box_top, box_width, box_height, area = stats[index]
        if (
            area / max(changed.size, 1)
            >= RELATIVE_MAIN_TAKEOVER_MIN_AREA_RATIO
            and box_width >= main_width * RELATIVE_MAIN_TAKEOVER_MIN_WIDTH_RATIO
            and box_height >= main_height * RELATIVE_MAIN_TAKEOVER_MIN_HEIGHT_RATIO
            and after_edge_density >= RELATIVE_MAIN_TAKEOVER_EDGE_MIN_RATIO
        ):
            return True
        if (
            area / max(changed.size, 1) >= 0.3
            and box_width >= main_width * 0.9
            and box_height >= main_height * 0.75
            and not valid_destination
        ):
            return True
        inset = bool(
            left >= main_width * 0.02
            and box_top >= main_height * 0.02
            and left + box_width <= main_width * 0.98
            and box_top + box_height <= main_height * 0.98
        )
        if not (
            inset
            and area / max(changed.size, 1) >= RELATIVE_OVERLAY_MIN_AREA_RATIO
            and box_width >= main_width * RELATIVE_OVERLAY_MIN_WIDTH_RATIO
            and box_height >= main_height * RELATIVE_OVERLAY_MIN_HEIGHT_RATIO
        ):
            continue
        outside = changed.copy()
        outside[box_top : box_top + box_height, left : left + box_width] = 0
        outside_ratio = float(np.count_nonzero(outside) / max(changed.size, 1))
        if outside_ratio <= 0.12 and not valid_destination:
            return True

    return bool(
        changed_ratio >= 0.65
        and after_edge_density <= RELATIVE_LOW_STRUCTURE_EDGE_MAX_RATIO
        and not valid_destination
    )


def _relative_dim_backdrop_panel_present(
    before_gray: np.ndarray,
    after_gray: np.ndarray,
) -> bool:
    height, width = before_gray.shape
    border = max(4, min(height, width) // 16)
    outer_mask = np.ones(before_gray.shape, dtype=bool)
    outer_mask[border : height - border, border : width - border] = False
    if not np.any(outer_mask):
        return False
    signed_delta = before_gray.astype(np.int16) - after_gray.astype(np.int16)
    outer_dim = float(np.median(signed_delta[outer_mask]))
    center = signed_delta[
        max(0, round(height * 0.16)) : max(1, round(height * 0.84)),
        max(0, round(width * 0.1)) : max(1, round(width * 0.9)),
    ]
    center_delta = float(np.median(center))
    return bool(
        outer_dim >= RELATIVE_DIM_MIN_DELTA
        and outer_dim - center_delta >= RELATIVE_DIM_MIN_DELTA
    )


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, 100))


def _main_structure_difference_ratio(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape[:2] != right.shape[:2]:
        return 1.0
    height = left.shape[0]
    top = max(1, round(height * 0.17))
    bottom = max(top + 1, round(height * 0.86))
    left_gray = cv2.cvtColor(left[top:bottom], cv2.COLOR_BGR2GRAY)
    right_gray = cv2.cvtColor(right[top:bottom], cv2.COLOR_BGR2GRAY)
    left_edges = cv2.Canny(left_gray, 50, 150) > 0
    right_edges = cv2.Canny(right_gray, 50, 150) > 0
    union = np.count_nonzero(left_edges | right_edges)
    if union == 0:
        return 0.0
    return round(float(np.count_nonzero(left_edges ^ right_edges) / union), 6)


def _layout_identity(evidence: Mapping[str, object]) -> dict[str, object]:
    identity = evidence.get("identity")
    return dict(identity) if isinstance(identity, Mapping) else {}


def _surface_visual_layout_is_bound(
    surface: Mapping[str, Any] | None,
    visual_layout: Mapping[str, object],
) -> bool:
    if not isinstance(surface, Mapping):
        return False
    identity_fingerprint = str(surface.get("identity_fingerprint") or "")
    signals = surface.get("identity_signals")
    return bool(
        identity_fingerprint.startswith("sha256:")
        and isinstance(signals, Mapping)
        and signals.get("visual_layout") == visual_layout
    )


def _visual_intent_is_consistent(
    *,
    target_kind: str,
    expected_page_type: str,
) -> bool:
    """expected_page_type 只复核已签发意图，不把它当作 after 页型。"""
    if target_kind == "primary_navigation":
        return expected_page_type in PRIMARY_NAVIGATION_PAGE_TYPES
    allowed = VISUAL_TARGET_EXPECTED_PAGE_TYPES.get(target_kind)
    return allowed is not None and expected_page_type in allowed


def _bound_page_type(surface: Mapping[str, Any] | None) -> str:
    if not isinstance(surface, Mapping):
        return ""
    binding = surface.get("page_type_binding")
    if not isinstance(binding, Mapping):
        return ""
    return str(binding.get("page_type") or "").strip()


def _bottom_navigation(identity: Mapping[str, object]) -> dict[str, object]:
    value = identity.get("bottom_navigation")
    return dict(value) if isinstance(value, Mapping) else {}


def _target_navigation_slot(
    capability: object,
    *,
    slot_count: object,
) -> int | None:
    if isinstance(slot_count, bool) or not isinstance(slot_count, int) or slot_count <= 0:
        return None
    rect = getattr(capability, "normalized_rect", None)
    if not isinstance(rect, Mapping):
        return None
    try:
        center_x = _finite_number(rect["x"]) + _finite_number(rect["width"]) / 2
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= center_x <= 1:
        return None
    return min(int(center_x * slot_count), slot_count - 1)


def _density_bucket(value: float) -> str:
    if value < 0.02:
        return "sparse"
    if value < 0.08:
        return "medium"
    return "dense"


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value <= 4:
        return "1-4"
    if value <= 12:
        return "5-12"
    return "13+"


def _point_is_inside_visual_viewport(
    *,
    x: float,
    y: float,
    viewport: Mapping[str, float],
) -> bool:
    return bool(
        viewport["offset_left"] <= x < viewport["offset_left"] + viewport["width"]
        and viewport["offset_top"] <= y < viewport["offset_top"] + viewport["height"]
    )


def _failure(
    error_code: str,
    *,
    match_score: float = 0.0,
) -> VisualCoordinateResolution:
    return VisualCoordinateResolution(
        css_x=None,
        css_y=None,
        relocated=False,
        match_score=_bounded_score(match_score),
        audit_codes=(error_code,),
    )


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("坐标事实必须为数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("坐标事实必须为有限数")
    return number


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError("图片尺寸必须为正整数")
    return value


def _bounded_score(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return round(min(max(float(value), 0.0), 1.0), 6)
