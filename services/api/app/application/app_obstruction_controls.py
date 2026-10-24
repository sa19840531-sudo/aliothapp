"""共享当前视觉清障语义；不从控件文案、包名或坐标猜测关闭入口。"""

from collections.abc import Mapping

from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    CandidateAction,
    NormalizedBounds,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.app.domain.protocol import stable_protocol_hash

APP_OBSTRUCTION_IDENTITY_VERSION = "app-obstruction-identity.v2"
APP_OBSTRUCTION_CONTROL_IDENTITY_VERSION = "app-obstruction-control-identity.v2"
APP_OBSTRUCTION_CONTROL_REBIND_POSITION_TOLERANCE = 0.12


def is_visual_obstruction_control(candidate: CandidateAction) -> bool:
    """角色仅用于调度，执行时仍须通过当前截图的视觉合同复验。"""

    return (
        candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.metadata.get("control_role") in {"dismiss", "skip"}
        and candidate.metadata.get("coverage_page_seek_contract") is None
    )


def build_visual_obstruction_control_identity(
    candidate: AppVisualCandidate,
) -> str | None:
    """按控件角色和粗粒度位置生成跨截图稳定身份，不读取动态广告文案。"""

    if (
        candidate.control_role not in {"dismiss", "skip"}
        or candidate.normalized_bounds is None
    ):
        return None
    payload: dict[str, object] = {
        "version": APP_OBSTRUCTION_CONTROL_IDENTITY_VERSION,
        "role": candidate.control_role,
        "action_type": candidate.action_type.value,
        "screen_position_zone": _coarse_screen_position_zone(
            candidate.normalized_bounds,
        ),
    }
    return "obstruction-control:" + stable_protocol_hash(payload)


def can_rebind_visual_obstruction_control(
    *,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
) -> bool:
    """只按签名角色、动作和位置判断是否可重绑当前帧关闭控件。"""

    source_role = source_candidate.control_role
    if (
        source_role not in {"dismiss", "skip"}
        or current_candidate.control_role != source_role
        or current_candidate.action_type is not source_candidate.action_type
    ):
        return False
    source_bounds = source_candidate.normalized_bounds
    current_bounds = current_candidate.normalized_bounds
    if source_bounds is None or current_bounds is None:
        return False
    source_center_x, source_center_y = source_bounds.center
    current_center_x, current_center_y = current_bounds.center
    return (
        abs(source_center_x - current_center_x)
        <= APP_OBSTRUCTION_CONTROL_REBIND_POSITION_TOLERANCE
        and abs(source_center_y - current_center_y)
        <= APP_OBSTRUCTION_CONTROL_REBIND_POSITION_TOLERANCE
    )


def build_app_obstruction_identity(analysis: AppVisualAnalysis) -> str | None:
    """生成遮挡结构身份，让动态素材变化不能伪装成关闭成功。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    control_roles = tuple(
        sorted(
            {
                candidate.control_role
                for candidate in analysis.candidates
                if candidate.control_role in {"dismiss", "skip"}
            }
        )
    )
    # 轮播换图时 Vision 常丢 dismiss 候选；同帧像素圆 X 仍在则结构身份保持 dismiss，
    # 避免 overlay_signature 抖动把同层当成新层并反复 OBSERVE 采证。
    if not control_roles:
        calibrated = facts.get("pixel_cross_close_calibration")
        if isinstance(calibrated, Mapping) and isinstance(
            calibrated.get("normalized_bounds"), Mapping
        ):
            control_roles = ("dismiss",)
    if not control_roles and not is_obstructed_surface(
        page_state=analysis.page_state,
        page_type=analysis.visual_page_type,
        overlay_signature=analysis.overlay_signature,
        surface_facts=facts,
    ):
        return None

    obstruction_kind = _obstruction_kind(analysis, control_roles=control_roles)
    topmost_ad = facts.get("topmost_ad_popup")
    payload = {
        "version": APP_OBSTRUCTION_IDENTITY_VERSION,
        "obstruction_kind": obstruction_kind,
        "control_roles": control_roles,
        "topmost_ad": (
            isinstance(topmost_ad, Mapping) and topmost_ad.get("is_ad") is True
        ),
        "layout_profile": _obstruction_layout_profile(
            analysis=analysis,
            topmost_ad=topmost_ad,
        ),
    }
    return "obstruction:" + stable_protocol_hash(payload)


def _obstruction_kind(
    analysis: AppVisualAnalysis,
    *,
    control_roles: tuple[str, ...],
) -> str:
    page_state = str(analysis.page_state or "unknown").strip().casefold()
    if page_state in {"system_dialog", "permission_dialog", "browser_permission"}:
        return "system_dialog"
    if page_state in {"auth_gate", "auth_required", "login_required", "requires_auth"}:
        return "auth_gate"
    if "skip" in control_roles or page_state in {"startup_ad", "countdown"}:
        return "startup_ad"
    if "dismiss" in control_roles or page_state in {"popup", "modal", "overlay", "dialog"}:
        return "popup"
    if page_state in {"loading", "line_loading", "transitional"}:
        return "transitional"
    return "obstruction"


def _obstruction_layout_profile(
    *,
    analysis: AppVisualAnalysis,
    topmost_ad: object,
) -> Mapping[str, object] | None:
    """只记录归一化结构分段，区分堆叠层且容忍同层素材与像素抖动。"""

    popup_bounds = _topmost_popup_bounds(topmost_ad)
    controls = tuple(
        candidate
        for candidate in analysis.candidates
        if candidate.control_role in {"dismiss", "skip"}
        and candidate.normalized_bounds is not None
        and candidate.confidence >= 0.8
    )
    # Vision 丢控件时：用像素校准关闭点维持同层结构身份（轮播换图不重置）。
    if not controls:
        facts = (
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
        )
        calibrated = facts.get("pixel_cross_close_calibration")
        if isinstance(calibrated, Mapping):
            raw = calibrated.get("normalized_bounds")
            if isinstance(raw, Mapping):
                try:
                    from types import SimpleNamespace

                    from services.api.app.domain.exploration import NormalizedBounds

                    pixel_bounds = NormalizedBounds.from_dict(raw)
                    controls = (
                        SimpleNamespace(
                            control_role="dismiss",
                            normalized_bounds=pixel_bounds,
                            confidence=0.95,
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
    if popup_bounds is not None:
        popup_height = popup_bounds.bottom - popup_bounds.top
        relative_control_bands = tuple(
            sorted(
                {
                    (
                        str(candidate.control_role),
                        _relative_vertical_band(
                            candidate.normalized_bounds,
                            popup_bounds=popup_bounds,
                        ),
                    )
                    for candidate in controls
                }
            )
        )
        return {
            "popup_height_band": _fraction_band(popup_height, partitions=10),
            "relative_control_bands": relative_control_bands,
        }
    if not controls:
        return None
    return {
        "screen_control_bands": tuple(
            sorted(
                {
                    (
                        str(candidate.control_role),
                        _fraction_band(candidate.normalized_bounds.center[1], partitions=10),
                        _fraction_band(
                            candidate.normalized_bounds.bottom
                            - candidate.normalized_bounds.top,
                            partitions=20,
                        ),
                    )
                    for candidate in controls
                }
            )
        )
    }


def _topmost_popup_bounds(value: object) -> NormalizedBounds | None:
    if not isinstance(value, Mapping) or value.get("is_ad") is not True:
        return None
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or confidence < 0.8
    ):
        return None
    raw_bounds = value.get("normalized_bounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    try:
        return NormalizedBounds.from_dict(raw_bounds)
    except (TypeError, ValueError):
        return None


def _relative_vertical_band(
    bounds: NormalizedBounds,
    *,
    popup_bounds: NormalizedBounds,
) -> int:
    popup_height = popup_bounds.bottom - popup_bounds.top
    relative_center = (bounds.center[1] - popup_bounds.top) / popup_height
    # 控件贴近弹窗边界时，框选抖动不应凭空增加一个内外结构档位。
    return _fraction_band(relative_center, partitions=5)


def _fraction_band(value: float, *, partitions: int) -> int:
    return int(round(max(0.0, min(1.0, value)) * partitions))


def _coarse_screen_position_zone(bounds: NormalizedBounds) -> tuple[int, int]:
    """四分区足以隔离不同角落，并容忍 Vision 对同一控件的小幅框选抖动。"""

    center_x, center_y = bounds.center
    return (
        min(3, int(max(0.0, min(1.0, center_x)) * 4)),
        min(3, int(max(0.0, min(1.0, center_y)) * 4)),
    )


def is_obstructed_surface(
    *,
    page_state: str | None,
    page_type: str | None,
    overlay_signature: str | None,
    surface_facts: Mapping[str, object],
) -> bool:
    """临时遮挡与业务页面身份分开判断，避免把清障前的候选缺席当成失效。"""

    topmost_ad = surface_facts.get("topmost_ad_popup")
    return (
        bool(overlay_signature)
        or str(page_state or "").casefold()
        in {
            "popup",
            "modal",
            "overlay",
            "dialog",
            "countdown",
            "startup_ad",
            "loading",
            "line_loading",
            "blocked",
            "transitional",
            "auth_gate",
            "auth_required",
            "login_required",
            "requires_auth",
            "system_dialog",
            "permission_dialog",
            "browser_permission",
        }
        or str(page_type or "").casefold() in {"startup_ad", "line_loading"}
        or any(
            surface_facts.get(key) is True
            for key in (
                "popup_visible",
                "startup_ad_visible",
                "countdown_visible",
                "dialog_visible",
                "auth_gate_visible",
                "requires_auth",
                "system_dialog_visible",
                "permission_dialog_visible",
            )
        )
        or (isinstance(topmost_ad, Mapping) and topmost_ad.get("is_ad") is True)
    )
