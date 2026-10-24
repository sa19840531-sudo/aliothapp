"""平台合成触控合同：弹层关闭与开屏跳过（不在 Vision 候选列表内时由 Runtime 重建）。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel

APP_POPUP_CLOSE_CORNER_VERSION = "app-popup-close-corner.v1"
APP_STARTUP_AD_SKIP_VERSION = "app-startup-ad-skip.v1"
APP_POPUP_CLEARANCE_OBSERVE_VERSION = "app-popup-clearance-observe.v1"
APP_POPUP_SCRIM_DISMISS_VERSION = "app-popup-scrim-dismiss.v1"
APP_PROFILE_CREDENTIAL_SAVE_VERSION = "app-profile-credential-save.v1"
APP_PROFILE_CREDENTIAL_DISMISS_VERSION = "app-profile-credential-dismiss.v1"
# 开屏无视觉/可访问性候选时的合成兜底区域（略放大至右上常见跳过钮）
DEFAULT_STARTUP_AD_SKIP_BOUNDS = NormalizedBounds(0.78, 0.02, 0.98, 0.10)
_STARTUP_AD_SKIP_BOUNDS_INFLATE = 0.015
# 无 X 终身卡／抢购层：点卡片下方蒙层空白处关闭（避开中部抢购钮、底飘 X、上方分类、底栏）。
# 左侧中部常落在卡片图上关不掉；用户截图空白在「立即抢购」下方、底飘上方。
DEFAULT_NO_CLOSE_PROMO_SCRIM_BOUNDS = NormalizedBounds(0.35, 0.775, 0.65, 0.835)
# 备用：左上角蒙层空白（卡片外缘）。
ALT_NO_CLOSE_PROMO_SCRIM_BOUNDS = NormalizedBounds(0.01, 0.22, 0.10, 0.32)


def popup_clearance_observe_contract(*, purpose: str) -> dict[str, object]:
    """弹窗未清且无可靠关闭点时的零触控重采合同。"""

    return {
        "protocol_version": APP_POPUP_CLEARANCE_OBSERVE_VERSION,
        "purpose": purpose,
        "expected_state_hint": "popup_clearance_reobserve",
    }


def popup_scrim_dismiss_contract(
    *,
    bounds: NormalizedBounds,
    purpose: str,
    confidence: float = 0.88,
) -> dict[str, object]:
    """无关闭 X 的促销层：点蒙层空白处关闭。"""

    return {
        "protocol_version": APP_POPUP_SCRIM_DISMISS_VERSION,
        "normalized_bounds": bounds.to_dict(),
        "purpose": purpose,
        "expected_state_hint": "dismiss_popup_scrim",
        "confidence": confidence,
    }


def popup_close_corner_contract(
    *,
    bounds: NormalizedBounds,
    purpose: str,
    confidence: float = 0.82,
) -> dict[str, object]:
    return {
        "protocol_version": APP_POPUP_CLOSE_CORNER_VERSION,
        "normalized_bounds": bounds.to_dict(),
        "purpose": purpose,
        "expected_state_hint": "close_popup_x",
        "confidence": confidence,
    }


def profile_credential_save_contract(
    *,
    bounds: NormalizedBounds,
    purpose: str,
    confidence: float = 0.9,
) -> dict[str, object]:
    """个人中心账号凭证「保存凭证」合成触控合同。"""

    return {
        "protocol_version": APP_PROFILE_CREDENTIAL_SAVE_VERSION,
        "normalized_bounds": bounds.to_dict(),
        "purpose": purpose,
        "expected_state_hint": "save_profile_credential",
        "confidence": confidence,
    }


def profile_credential_dismiss_contract(
    *,
    bounds: NormalizedBounds,
    purpose: str,
    confidence: float = 0.9,
) -> dict[str, object]:
    """个人中心账号凭证「我已保存」合成触控合同（须先保存成功）。"""

    return {
        "protocol_version": APP_PROFILE_CREDENTIAL_DISMISS_VERSION,
        "normalized_bounds": bounds.to_dict(),
        "purpose": purpose,
        "expected_state_hint": "dismiss_profile_credential",
        "confidence": confidence,
    }


def startup_ad_skip_contract(
    *,
    bounds: NormalizedBounds,
    purpose: str,
    confidence: float = 0.75,
    discovery_source: str = "clearance_synthetic",
    used_default_fixed_bounds: bool = False,
) -> dict[str, object]:
    """开屏跳过合同；固定右上角仅清障，不得当长期业务导航入口。"""

    from services.api.app.application.app_navigation_entry_provenance import (
        DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
        DISCOVERY_SOURCE_UIA_ISSUED,
        normalize_discovery_source,
        tag_navigation_entry_provenance,
    )

    source = normalize_discovery_source(discovery_source) or DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC
    if used_default_fixed_bounds:
        source = DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC
    provenance = tag_navigation_entry_provenance(
        discovery_source=source,
        source_capture_id=None,
        requires_fresh_capture_after_action=True,
    )
    return {
        "protocol_version": APP_STARTUP_AD_SKIP_VERSION,
        "normalized_bounds": bounds.to_dict(),
        "purpose": purpose,
        "expected_state_hint": "tap_skip",
        "confidence": confidence,
        "discovery_source": source,
        "long_term_fixed_coordinates_forbidden": True,
        "clearance_only": source == DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
        "requires_fresh_capture_after_action": True,
        "cross_capture_coordinate_replay_forbidden": True,
        "used_default_fixed_bounds": bool(used_default_fixed_bounds),
        "navigation_entry_provenance": provenance,
        # 受信 UIA 跳过仍可标 uia_issued；默认右上角永不进业务导航分母。
        "business_navigation_entry": source == DISCOVERY_SOURCE_UIA_ISSUED,
    }


def default_popup_close_bounds(
    analysis: AppVisualAnalysis,
) -> NormalizedBounds | None:
    """只使用本帧可验证的关闭控件，不猜测屏幕角落坐标。"""

    return _trusted_overlay_close_bounds(analysis)


def default_startup_ad_skip_bounds(
    analysis: AppVisualAnalysis,
) -> tuple[NormalizedBounds, bool]:
    """优先复用持久化 trusted skip 坐标，否则回退平台默认右上区域。

    返回 ``(bounds, used_default_fixed_bounds)``。
    """

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    trusted = facts.get("trusted_accessibility_skip")
    if isinstance(trusted, Mapping):
        parsed = _normalized_bounds_from_mapping(trusted.get("normalized_bounds"))
        if parsed is not None:
            return _inflate_normalized_bounds(parsed), False
    return DEFAULT_STARTUP_AD_SKIP_BOUNDS, True


def _normalized_bounds_from_mapping(raw_bounds: object) -> NormalizedBounds | None:
    if not isinstance(raw_bounds, Mapping):
        return None
    try:
        left = float(raw_bounds["left"])
        top = float(raw_bounds["top"])
        right = float(raw_bounds["right"])
        bottom = float(raw_bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return NormalizedBounds(left, top, right, bottom)


def _inflate_normalized_bounds(
    bounds: NormalizedBounds,
    *,
    pad: float = _STARTUP_AD_SKIP_BOUNDS_INFLATE,
) -> NormalizedBounds:
    return NormalizedBounds(
        max(0.0, bounds.left - pad),
        max(0.0, bounds.top - pad),
        min(1.0, bounds.right + pad),
        min(1.0, bounds.bottom + pad),
    )


def _trusted_overlay_close_bounds(analysis: AppVisualAnalysis) -> NormalizedBounds | None:
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    if not isinstance(controls, Mapping):
        return None
    overlay_close = controls.get("overlay_close")
    if not isinstance(overlay_close, Mapping):
        return None
    width = analysis.width if isinstance(analysis.width, int) and analysis.width > 0 else 0
    height = analysis.height if isinstance(analysis.height, int) and analysis.height > 0 else 0
    if width <= 0 or height <= 0:
        return None
    raw_bounds = overlay_close.get("bounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    try:
        left = float(raw_bounds["left"]) / width
        top = float(raw_bounds["top"]) / height
        right = float(raw_bounds["right"]) / width
        bottom = float(raw_bounds["bottom"]) / height
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return NormalizedBounds(left, top, right, bottom)


def attach_synthetic_touch_contracts(
    parameters: dict[str, object],
    candidate_metadata: Mapping[str, object],
) -> None:
    """把合成弹层／开屏跳过合同写入 ActionIntent 参数，供 Runtime 重建候选。"""

    popup_contract = candidate_metadata.get("popup_close_corner_contract")
    if (
        (
            candidate_metadata.get("popup_close_corner_fallback") is True
            or candidate_metadata.get("forced_pixel_popup_close") is True
            or candidate_metadata.get("popup_close_pixel_visual") is True
            or candidate_metadata.get("popup_close_bottom_x_fallback") is True
        )
        and isinstance(popup_contract, Mapping)
    ):
        parameters["app_popup_close_corner"] = dict(popup_contract)
    skip_contract = candidate_metadata.get("startup_ad_skip_contract")
    if (
        candidate_metadata.get("startup_ad_skip_fallback") is True
        and isinstance(skip_contract, Mapping)
    ):
        parameters["app_startup_ad_skip"] = dict(skip_contract)
    observe_contract = candidate_metadata.get("popup_clearance_observe_contract")
    if (
        candidate_metadata.get("forced_popup_clearance_observe") is True
        and isinstance(observe_contract, Mapping)
    ):
        parameters["app_popup_clearance_observe"] = dict(observe_contract)
    scrim_contract = candidate_metadata.get("popup_scrim_dismiss_contract")
    if (
        candidate_metadata.get("forced_popup_scrim_dismiss") is True
        and isinstance(scrim_contract, Mapping)
    ):
        parameters["app_popup_scrim_dismiss"] = dict(scrim_contract)
    credential_contract = candidate_metadata.get("profile_credential_save_contract")
    if (
        candidate_metadata.get("profile_credential_save") is True
        and isinstance(credential_contract, Mapping)
    ):
        parameters["app_profile_credential_save"] = dict(credential_contract)
    dismiss_contract = candidate_metadata.get("profile_credential_dismiss_contract")
    if candidate_metadata.get("profile_credential_dismiss") is True:
        parameters["profile_credential_dismiss"] = True
        if isinstance(dismiss_contract, Mapping):
            parameters["app_profile_credential_dismiss"] = dict(dismiss_contract)


def resolve_popup_close_corner_candidate(
    *,
    intent_semantic_target: str,
    intent_purpose: str,
    analysis: AppVisualAnalysis,
    candidate_id: str,
    payload: Mapping[str, object],
) -> AppVisualCandidate | None:
    required_keys = {
        "protocol_version",
        "normalized_bounds",
        "purpose",
        "expected_state_hint",
        "confidence",
    }
    if set(payload) != required_keys:
        return None
    if payload.get("protocol_version") != APP_POPUP_CLOSE_CORNER_VERSION:
        return None
    purpose = payload.get("purpose")
    hint = payload.get("expected_state_hint")
    confidence = payload.get("confidence")
    raw_bounds = payload.get("normalized_bounds")
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or purpose != purpose.strip()
        or hint != "close_popup_x"
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not (0.0 <= float(confidence) <= 1.0)
        or not isinstance(raw_bounds, Mapping)
        or "关闭弹层" not in str(intent_semantic_target or "")
    ):
        return None
    # Frontier sibling 常把 intent.purpose 改写成「先清除遮挡…／禁止底栏 sibling…」，
    # 与合同 purpose「点弹层下方关闭控件…」不一致；不得因此拒解析——
    # 否则 /apps/execute 根本发不出，录屏可见圆 X 却连点 OUTCOME_UNKNOWN。
    intent_purpose_text = str(intent_purpose or "").strip()
    if intent_purpose_text and intent_purpose_text != purpose:
        wrapper_ok = (
            "清除遮挡" in intent_purpose_text
            or "关闭弹层" in intent_purpose_text
            or "先关闭弹层" in intent_purpose_text
            or "禁止底栏" in intent_purpose_text
            or "下方" in intent_purpose_text
            or "像素" in intent_purpose_text
            or "圆 X" in intent_purpose_text
            or "圆X" in intent_purpose_text
            or "18+" in intent_purpose_text
            or "成人" in intent_purpose_text
            # 限步清障工具包装文案（糖果短剧试点）
            or "限步清障" in intent_purpose_text
            or "清障工具" in intent_purpose_text
        )
        if not wrapper_ok:
            return None
    try:
        bounds = NormalizedBounds.from_dict(raw_bounds)
    except Exception:  # noqa: BLE001
        return None
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=intent_semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=float(confidence),
        risk_level=InteractionRiskLevel.LOW,
        # 候选 purpose 必须与合同一致（candidate_id 由此派生）；
        # Intent.purpose 允许是 Frontier 包装文案。
        purpose=purpose,
        control_role="dismiss",
        expected_state_hint="close_popup_x",
    )
    # 合成强制像素关层：candidate_id 用 forced-pixel-popup-close:{capture}
    # 与视觉哈希不一致；不得因此 VISUAL_CANDIDATE_NOT_FOUND → OUTCOME_UNKNOWN。
    if isinstance(candidate_id, str) and candidate_id.startswith(
        "forced-pixel-popup-close:"
    ):
        suffix = candidate_id.split(":", 1)[-1].strip()
        if (
            suffix
            and suffix != "unknown"
            and suffix != str(analysis.capture_id)
        ):
            return None
        return candidate
    # Vision 哈希（candidate:…）与重建 id 常不一致：18+ 成人专区下方 X
    # 已有合同坐标时仍须点，禁止 VISUAL_CANDIDATE_NOT_FOUND 连废整轮。
    return candidate


def resolve_startup_ad_skip_candidate(
    *,
    intent_semantic_target: str,
    intent_purpose: str,
    analysis: AppVisualAnalysis,
    candidate_id: str,
    payload: Mapping[str, object],
) -> AppVisualCandidate | None:
    """重建开屏关闭／跳过候选：视觉像素／JSON 合同优先，a11y 兜底。"""

    required_keys = {
        "protocol_version",
        "normalized_bounds",
        "purpose",
        "expected_state_hint",
        "confidence",
    }
    if not required_keys.issubset(set(payload)):
        return None
    if payload.get("protocol_version") != APP_STARTUP_AD_SKIP_VERSION:
        return None
    purpose = payload.get("purpose")
    hint = payload.get("expected_state_hint")
    confidence = payload.get("confidence")
    raw_bounds = payload.get("normalized_bounds")
    target = str(intent_semantic_target or "")
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or purpose != purpose.strip()
        or hint != "tap_skip"
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not (0.0 <= float(confidence) <= 1.0)
        or not isinstance(raw_bounds, Mapping)
        or not any(
            marker in target
            for marker in (
                "进入",
                "進入",
                "跳过",
                "跳過",
                "关闭",
                "關閉",
                "開屏",
                "开屏",
                "首屏",
                "skip",
                "entry",
                "enter",
            )
        )
    ):
        return None
    # Frontier 包装目的串不得否决合同。
    intent_purpose_text = str(intent_purpose or "").strip()
    if intent_purpose_text and intent_purpose_text != purpose:
        wrapper_ok = (
            "清除遮挡" in intent_purpose_text
            or "关闭" in intent_purpose_text
            or "關閉" in intent_purpose_text
            or "跳过" in intent_purpose_text
            or "跳過" in intent_purpose_text
            or "进入" in intent_purpose_text
            or "進入" in intent_purpose_text
            or "开屏" in intent_purpose_text
            or "開屏" in intent_purpose_text
            or "首屏" in intent_purpose_text
            or "skip" in intent_purpose_text.casefold()
            or "entry" in intent_purpose_text.casefold()
        )
        if not wrapper_ok:
            return None
    try:
        bounds = NormalizedBounds.from_dict(raw_bounds)
    except Exception:  # noqa: BLE001
        return None

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    trusted = facts.get("trusted_accessibility_skip")
    pixel = facts.get("pixel_cross_close_calibration")
    has_a11y = (
        isinstance(trusted, Mapping)
        and trusted.get("package_name") == analysis.package_name
    )
    has_pixel = isinstance(pixel, Mapping) and isinstance(
        pixel.get("normalized_bounds"), Mapping
    )
    # 视觉／合同坐标：允许与默认右上角框不同；a11y 旧路径仍可对齐默认框。
    if has_a11y and not has_pixel:
        expected_bounds, _used_default = default_startup_ad_skip_bounds(analysis)
        if bounds.to_dict() != expected_bounds.to_dict():
            # 合同带了 ui_tree／视觉框时仍放行，不强制等于默认框。
            pass
    if not has_a11y and not has_pixel:
        # 无障碍事实也允许合同坐标（JSON／合成兜底），避免开屏永久卡住。
        cy = (bounds.top + bounds.bottom) / 2.0
        cx = (bounds.left + bounds.right) / 2.0
        if not (cy <= 0.22 and (cx >= 0.55 or cx <= 0.45)):
            return None

    discovery = str(payload.get("discovery_source") or "").strip() or (
        "clearance_synthetic"
        if payload.get("used_default_fixed_bounds") is True
        else "uia_issued"
    )
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=intent_semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=float(confidence),
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="skip",
        expected_state_hint="tap_skip",
        discovery_source=discovery,
    )
    return candidate if candidate.candidate_id == candidate_id else None


def resolve_popup_clearance_observe_candidate(
    *,
    intent_semantic_target: str,
    intent_purpose: str,
    analysis: AppVisualAnalysis,
    candidate_id: str,
    payload: Mapping[str, object],
) -> AppVisualCandidate | None:
    """重建弹窗清障零触控观察；forced-popup-clearance-observe:* 不要求视觉哈希 id。"""

    if not (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-popup-clearance-observe:")
    ):
        return None
    required_keys = {"protocol_version", "purpose", "expected_state_hint"}
    if set(payload) != required_keys:
        return None
    if payload.get("protocol_version") != APP_POPUP_CLEARANCE_OBSERVE_VERSION:
        return None
    purpose = payload.get("purpose")
    hint = payload.get("expected_state_hint")
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or purpose != purpose.strip()
        or hint != "popup_clearance_reobserve"
        or "观察" not in str(intent_semantic_target or "")
    ):
        return None
    intent_purpose_text = str(intent_purpose or "").strip()
    if intent_purpose_text and intent_purpose_text != purpose:
        wrapper_ok = (
            "弹窗" in intent_purpose_text
            or "弹层" in intent_purpose_text
            or "关闭" in intent_purpose_text
            or "关层" in intent_purpose_text
            or "观察" in intent_purpose_text
            or "重采" in intent_purpose_text
        )
        if not wrapper_ok:
            return None
    suffix = candidate_id.split(":", 1)[-1].strip()
    if (
        suffix
        and suffix != "unknown"
        and suffix != str(analysis.capture_id)
    ):
        return None
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=intent_semantic_target,
        action_type=InteractionOperation.OBSERVE,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="popup_clearance_reobserve",
    )


def resolve_popup_scrim_dismiss_candidate(
    *,
    intent_semantic_target: str,
    intent_purpose: str,
    analysis: AppVisualAnalysis,
    candidate_id: str,
    payload: Mapping[str, object],
) -> AppVisualCandidate | None:
    """重建无 X 促销层「点蒙层空白处关闭」合成候选。"""

    if not (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-popup-scrim-dismiss:")
    ):
        return None
    required_keys = {
        "protocol_version",
        "normalized_bounds",
        "purpose",
        "expected_state_hint",
        "confidence",
    }
    if set(payload) != required_keys:
        return None
    if payload.get("protocol_version") != APP_POPUP_SCRIM_DISMISS_VERSION:
        return None
    purpose = payload.get("purpose")
    hint = payload.get("expected_state_hint")
    confidence = payload.get("confidence")
    raw_bounds = payload.get("normalized_bounds")
    target = str(intent_semantic_target or "")
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or purpose != purpose.strip()
        or hint != "dismiss_popup_scrim"
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not (0.0 <= float(confidence) <= 1.0)
        or not isinstance(raw_bounds, Mapping)
        or not any(marker in target for marker in ("空白", "蒙层", "遮罩", "scrim"))
    ):
        return None
    intent_purpose_text = str(intent_purpose or "").strip()
    if intent_purpose_text and intent_purpose_text != purpose:
        wrapper_ok = (
            "空白" in intent_purpose_text
            or "蒙层" in intent_purpose_text
            or "遮罩" in intent_purpose_text
            or "终身卡" in intent_purpose_text
            or "促销" in intent_purpose_text
            or "关闭弹层" in intent_purpose_text
            or "抢购" in intent_purpose_text
        )
        if not wrapper_ok:
            return None
    try:
        bounds = NormalizedBounds.from_dict(raw_bounds)
    except Exception:  # noqa: BLE001
        return None
    suffix = candidate_id.split(":", 1)[-1].strip()
    if (
        suffix
        and suffix != "unknown"
        and suffix != str(analysis.capture_id)
    ):
        return None
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=intent_semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=float(confidence),
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="dismiss",
        expected_state_hint="dismiss_popup_scrim",
    )


def resolve_profile_credential_save_candidate(
    *,
    intent_semantic_target: str,
    intent_purpose: str,
    analysis: AppVisualAnalysis,
    candidate_id: str,
    payload: Mapping[str, object],
) -> AppVisualCandidate | None:
    """重建个人中心「保存凭证」合成候选。"""

    required_keys = {
        "protocol_version",
        "normalized_bounds",
        "purpose",
        "expected_state_hint",
        "confidence",
    }
    if set(payload) != required_keys:
        return None
    if payload.get("protocol_version") != APP_PROFILE_CREDENTIAL_SAVE_VERSION:
        return None
    purpose = payload.get("purpose")
    hint = payload.get("expected_state_hint")
    confidence = payload.get("confidence")
    raw_bounds = payload.get("normalized_bounds")
    target = str(intent_semantic_target or "")
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or purpose != purpose.strip()
        or hint != "save_profile_credential"
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not (0.0 <= float(confidence) <= 1.0)
        or not isinstance(raw_bounds, Mapping)
        or not any(marker in target for marker in ("保存凭证", "保存憑證", "立即保存"))
    ):
        return None
    intent_purpose_text = str(intent_purpose or "").strip()
    if intent_purpose_text and intent_purpose_text != purpose:
        wrapper_ok = (
            "保存凭证" in intent_purpose_text
            or "保存憑證" in intent_purpose_text
            or "账号凭证" in intent_purpose_text
            or "帳號憑證" in intent_purpose_text
        )
        if not wrapper_ok:
            return None
    try:
        bounds = NormalizedBounds.from_dict(raw_bounds)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(candidate_id, str) and candidate_id.startswith(
        "forced-profile-credential-save:"
    ):
        suffix = candidate_id.split(":", 1)[-1].strip()
        if (
            suffix
            and suffix != "unknown"
            and suffix != str(analysis.capture_id)
        ):
            return None
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=intent_semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=float(confidence),
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="profile_credential_confirm",
        expected_state_hint="save_profile_credential",
    )


def resolve_profile_credential_dismiss_candidate(
    *,
    intent_semantic_target: str,
    intent_purpose: str,
    analysis: AppVisualAnalysis,
    candidate_id: str,
    payload: Mapping[str, object],
) -> AppVisualCandidate | None:
    """重建个人中心「我已保存」合成候选（保存成功后关闭弹层）。"""

    required_keys = {
        "protocol_version",
        "normalized_bounds",
        "purpose",
        "expected_state_hint",
        "confidence",
    }
    if set(payload) != required_keys:
        return None
    if payload.get("protocol_version") != APP_PROFILE_CREDENTIAL_DISMISS_VERSION:
        return None
    purpose = payload.get("purpose")
    hint = payload.get("expected_state_hint")
    confidence = payload.get("confidence")
    raw_bounds = payload.get("normalized_bounds")
    target = str(intent_semantic_target or "")
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or purpose != purpose.strip()
        or hint != "dismiss_profile_credential"
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not (0.0 <= float(confidence) <= 1.0)
        or not isinstance(raw_bounds, Mapping)
        or "我已保存" not in target
    ):
        return None
    intent_purpose_text = str(intent_purpose or "").strip()
    if intent_purpose_text and intent_purpose_text != purpose:
        wrapper_ok = (
            "我已保存" in intent_purpose_text
            or "账号凭证" in intent_purpose_text
            or "帳號憑證" in intent_purpose_text
        )
        if not wrapper_ok:
            return None
    try:
        bounds = NormalizedBounds.from_dict(raw_bounds)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(candidate_id, str) and candidate_id.startswith(
        "forced-profile-credential-dismiss:"
    ):
        suffix = candidate_id.split(":", 1)[-1].strip()
        if (
            suffix
            and suffix != "unknown"
            and suffix != str(analysis.capture_id)
        ):
            return None
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=intent_semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=float(confidence),
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="profile_credential_dismiss",
        expected_state_hint="dismiss_profile_credential",
    )
