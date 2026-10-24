"""App Screenshot 候选解析与 SurfaceDriver 动作适配。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.app_action_observation import confirmed_app_identity
from services.api.app.application.app_capture_persistence import (
    AppCapturePersistenceService,
    PersistedAppCapture,
)
from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV,
    DISCOVERY_SOURCE_PROMOTED_COVERAGE,
    DISCOVERY_SOURCE_UIA_ALIGNED,
    DISCOVERY_SOURCE_UIA_ISSUED,
    DISCOVERY_SOURCE_VISION,
)
from services.api.app.application.app_obstruction_controls import (
    APP_OBSTRUCTION_CONTROL_REBIND_POSITION_TOLERANCE,
    can_rebind_visual_obstruction_control,
)
from services.api.app.application.app_popup_ad_scope import belongs_to_topmost_ad_popup
from services.api.app.application.app_script_curriculum import (
    APP_CURRICULUM_ACTION_VERSION,
    APP_CURRICULUM_OBSERVATION_VERSION,
)
from services.api.app.application.app_semantic_progress import (
    APP_DYNAMIC_PAGE_SAMPLING_VERSION,
    APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT,
    APP_DYNAMIC_STARTUP_OBSERVATION_VERSION,
    APP_DYNAMIC_UNSTABLE_PAGE_BACK_LIMIT,
    APP_DYNAMIC_UNSTABLE_PAGE_BACK_VERSION,
    APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_LIMIT,
    APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_VERSION,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    resolve_popup_clearance_observe_candidate,
    resolve_popup_close_corner_candidate,
    resolve_popup_scrim_dismiss_candidate,
    resolve_profile_credential_dismiss_candidate,
    resolve_profile_credential_save_candidate,
    resolve_startup_ad_skip_candidate,
)
from services.api.app.application.app_touch_target_conflicts import (
    filter_unproven_dismiss_candidates,
)
from services.api.app.application.app_visual_bottom_nav_calibration import (
    extract_pixel_bottom_nav_tabs,
    is_pixel_calibrated_bottom_nav_candidate,
)
from services.api.app.application.app_visual_close_calibration import (
    is_pixel_calibrated_close_candidate,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CapturePhase,
    CaptureWriteRequest,
    ExecuteInteractionRequest,
)
from services.api.app.application.capture_profiles import APP_CAPTURE_ARTIFACT_CONTRACT
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
    InteractionCommandValidator,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
    has_supplemental_evidence_lineage,
)
from services.api.app.application.surface_sessions import SurfaceSessionRepository
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.exploration.age_confirm import (
    is_age_confirm_candidate,
    observation_needs_age_confirm,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceDriver,
    SurfaceDriverError,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionStatus,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.device.app_tap_audit_log import log_app_tap_audit


class AppInteractionRuntimeError(RuntimeError):
    """在进入 Driver 前发现 App 候选、截图或 Session 已失效。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


APP_CANDIDATE_REVALIDATION_REQUIRED = "APP_CANDIDATE_REVALIDATION_REQUIRED"
ANDROID_POINT_IN_GESTURE_ZONE = "ANDROID_POINT_IN_GESTURE_ZONE"
# 云手机／全面屏底部系统手势带：Vision 常把底栏画进此区，点下去回桌面。
_GESTURE_ZONE_NORMALIZED_TOP = 0.92
# WebView 底栏 Vision 常画进手势带（top≥0.92）；合成落点须抬到安全触控带。
_SAFE_BOTTOM_NAV_TOUCH_BOTTOM = _GESTURE_ZONE_NORMALIZED_TOP - 0.015
_SAFE_BOTTOM_NAV_TOUCH_TOP = 0.84
_TRUSTED_A11Y_DISCOVERY_SOURCES = frozenset(
    {
        DISCOVERY_SOURCE_UIA_ALIGNED,
        DISCOVERY_SOURCE_UIA_ISSUED,
    }
)
_COORDINATE_TOUCH_ACTIONS = frozenset({InteractionOperation.CLICK, InteractionOperation.TAP})
_STABLE_TOUCH_PAGE_STATES = frozenset({"stable", "normal", "loaded", "content_available"})
_OBSTRUCTED_TOUCH_PAGE_STATES = frozenset(
    {
        "blocked",
        "loading",
        "line_loading",
        "transitional",
        "startup_ad",
        "countdown",
        "splash",
        "popup",
        "modal",
        "overlay",
        "dialog",
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
        "system_dialog",
        "permission_dialog",
        "browser_permission",
    }
)
_AD_TOUCH_PAGE_TYPES = frozenset({"ad", "advertisement", "ad_landing", "startup_ad", "countdown"})
_DISMISS_TOUCH_PAGE_STATES = frozenset(
    {
        "popup",
        "modal",
        "overlay",
        "dialog",
        "system_dialog",
        "permission_dialog",
        "browser_permission",
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
    }
)
_INVALID_CONTROL_ROLE = object()
_MIN_TOUCH_CANDIDATE_CONFIDENCE = 0.70
_APP_AD_LANDING_CONTRACT_VERSION = "app-ad-landing-consistency.v1"


def _is_coordinate_touch_action(action_type: InteractionOperation) -> bool:
    return action_type in _COORDINATE_TOUCH_ACTIONS


def _candidate_control_role(candidate: AppVisualCandidate) -> str | None | object:
    """读取 v3 控件角色，并把旧版候选的缺失字段视为无角色。"""

    role = getattr(candidate, "control_role", None)
    if role is None:
        return None
    if isinstance(role, str) and role in {"dismiss", "skip"}:
        return role
    return _INVALID_CONTROL_ROLE


def _complete_visual_identity(analysis: AppVisualAnalysis) -> bool:
    return all(
        isinstance(value, str) and bool(value.strip()) and value == value.strip()
        for value in (analysis.package_name, analysis.activity)
    )


def _same_confirmed_visual_page(
    source: AppVisualAnalysis, current: AppVisualAnalysis
) -> bool:
    """按各自截图重验业务身份，避免分类波动把同一页面判为换页。"""

    source_identity = confirmed_app_identity(source)
    return source_identity is not None and source_identity == confirmed_app_identity(current)


def _normalized_visual_label(value: object) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _is_age_confirm_visual_candidate(candidate: AppVisualCandidate) -> bool:
    return is_age_confirm_candidate(
        action_type=candidate.action_type,
        risk_level=candidate.risk_level,
        semantic_target=candidate.semantic_name,
        purpose=candidate.purpose,
        expected_state_hint=candidate.expected_state_hint,
    )


def _source_candidate_risk_matches_intent(
    candidate: AppVisualCandidate,
    intent: ActionIntent,
) -> bool:
    if candidate.risk_level == intent.risk_level:
        return True
    # 年龄确认在视觉层可标 medium，映射后的动作合同允许按 LOW 清障执行。
    return (
        candidate.risk_level == InteractionRiskLevel.MEDIUM
        and intent.risk_level == InteractionRiskLevel.LOW
        and _is_age_confirm_visual_candidate(candidate)
    )


def _current_candidate_risk_allowed(
    candidate: AppVisualCandidate,
    *,
    intent: ActionIntent,
    source_role: str | None | object,
) -> bool:
    if source_role in {"dismiss", "skip"}:
        if (
            intent.risk_level == InteractionRiskLevel.LOW
            and candidate.risk_level == InteractionRiskLevel.MEDIUM
            and _is_age_confirm_visual_candidate(candidate)
        ):
            # Vision 常把年龄门标成 medium；清障合同仍按 LOW 执行。
            return True
        return (
            source_role in {"dismiss", "skip"}
            and intent.risk_level == InteractionRiskLevel.LOW
            and candidate.risk_level == InteractionRiskLevel.LOW
        )
    if candidate.risk_level == InteractionRiskLevel.LOW:
        return intent.risk_level != InteractionRiskLevel.HIGH
    return (
        candidate.risk_level == InteractionRiskLevel.MEDIUM
        and intent.risk_level == InteractionRiskLevel.MEDIUM
    ) or (
        candidate.risk_level == InteractionRiskLevel.MEDIUM
        and intent.risk_level == InteractionRiskLevel.LOW
        and _is_age_confirm_visual_candidate(candidate)
    )


def _touch_semantic_matches(
    *,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
    intent: ActionIntent,
) -> bool:
    source_role = _candidate_control_role(source_candidate)
    if source_role in {"dismiss", "skip"}:
        # 清障角色是平台阶段事实；文案可因语言或 Vision 改写而变化。
        return _candidate_control_role(current_candidate) == source_role
    if _is_age_confirm_visual_candidate(source_candidate) and _is_age_confirm_visual_candidate(
        current_candidate
    ):
        return True
    if source_candidate.frontier_scope in {"page", "app"}:
        source_hint = _canonical_page_type_hint(source_candidate.expected_state_hint)
        current_hint = _canonical_page_type_hint(current_candidate.expected_state_hint)
        if (
            current_candidate.frontier_scope == source_candidate.frontier_scope
            and source_hint is not None
            and source_hint == current_hint
        ):
            # 跨截图导航以目录中的目标页型为稳定语义；控件文案可被 Vision
            # 改写，但当前帧仍须唯一匹配，且后续页面到达证明不会因此放宽。
            return True
    if (
        source_candidate.frontier_scope == "app"
        and current_candidate.frontier_scope == "app"
        and _bottom_nav_label_tokens_overlap(
            source_candidate.semantic_name,
            current_candidate.semantic_name,
            intent.semantic_target,
        )
    ):
        # 底栏文案常被 Vision 改写成「底栏「交友」」／「交友」等变体。
        return True
    if _text_looks_like_reload_retry(
        source_candidate.semantic_name,
        source_candidate.purpose,
        intent.semantic_target,
    ) and _text_looks_like_reload_retry(
        current_candidate.semantic_name,
        current_candidate.purpose,
        intent.semantic_target,
    ):
        # 「点击重试」↔「加载失败点击重试」等同加载恢复语义。
        return True
    return current_candidate.semantic_name == intent.semantic_target


def _bottom_nav_label_tokens_overlap(*labels: str | None) -> bool:
    """底栏标签弱匹配：去掉「底栏／底部Tab」外壳后，至少共享一个非空中文／英文词。"""

    tokens: list[set[str]] = []
    for raw in labels:
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        for prefix in (
            "底栏「",
            "底部导航「",
            "底部Tab",
            "底部tab",
            "底部",
            "底栏",
        ):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        text = text.strip("」[]()（） \t")
        parts = {
            part.casefold()
            for part in text.replace("/", " ").replace("|", " ").split()
            if part.strip()
        }
        # 中文两字标签常无整串比对
        if text.strip():
            parts.add(text.strip().casefold())
        if parts:
            tokens.append(parts)
    if len(tokens) < 2:
        return False
    shared = set.intersection(*tokens)
    return bool(shared)

def _canonical_page_type_hint(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    registry = get_default_page_type_registry()
    code = next(
        (candidate for candidate in registry.all_codes if candidate.casefold() == normalized),
        None,
    )
    if code is not None:
        return code
    alias = registry.resolve_alias(value)
    return alias.canonical_code if alias is not None else None


_HARD_OBSTRUCTION_PAGE_STATES = frozenset(
    {
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
        "startup_ad",
        "countdown",
        "splash",
        "blocked",
        "loading",
        "line_loading",
        "transitional",
        "system_dialog",
        "permission_dialog",
        "browser_permission",
    }
)


def _is_soft_home_overlay_mislabeled(analysis: AppVisualAnalysis) -> bool:
    """国庆浮标／底飘被标成 popup，但主页栏已露出、无硬弹窗。"""

    page_type = _normalized_visual_label(analysis.visual_page_type)
    if page_type != "home":
        return False
    page_state = _normalized_visual_label(analysis.page_state)
    if page_state in _HARD_OBSTRUCTION_PAGE_STATES:
        return False
    if page_type in _AD_TOUCH_PAGE_TYPES:
        return False
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    if any(
        facts.get(key) is True
        for key in (
            "auth_gate_visible",
            "startup_ad_visible",
            "countdown_visible",
            "requires_auth",
        )
    ):
        return False
    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
    )

    return not analysis_blocks_home_top_category_tabs(analysis)


def _touch_visual_is_obstructed(analysis: AppVisualAnalysis) -> bool:
    page_state = _normalized_visual_label(analysis.page_state)
    page_type = _normalized_visual_label(analysis.visual_page_type)
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    topmost_ad_popup = facts.get("topmost_ad_popup")
    obstructed = (
        page_state in _OBSTRUCTED_TOUCH_PAGE_STATES
        or page_type in _AD_TOUCH_PAGE_TYPES
        or page_type in {"popup", "modal", "overlay", "dialog"}
        or bool(analysis.overlay_signature)
        or any(
            facts.get(key) is True
            for key in (
                "popup_visible",
                "dialog_visible",
                "auth_gate_visible",
                "startup_ad_visible",
                "countdown_visible",
                "requires_auth",
            )
        )
        or (isinstance(topmost_ad_popup, Mapping) and topmost_ad_popup.get("is_ad") is True)
    )
    if obstructed and _is_soft_home_overlay_mislabeled(analysis):
        # 右下国庆浮标等装饰不得当挡路弹窗。
        return False
    return obstructed


def _known_obstruction_matches_role(
    analysis: AppVisualAnalysis,
    role: str | None | object,
) -> bool:
    """只按结构化阶段事实确认清障角色，不读取文案或固定坐标。"""

    if role not in {"dismiss", "skip"}:
        return False
    page_state = _normalized_visual_label(analysis.page_state)
    page_type = _normalized_visual_label(analysis.visual_page_type)
    if page_state == "unknown" or page_type == "unknown":
        return False
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    topmost_ad_popup = facts.get("topmost_ad_popup")
    is_ad = (
        page_state in {"startup_ad", "countdown", "splash"}
        or page_type in _AD_TOUCH_PAGE_TYPES
        or page_type == "splash"
        or facts.get("startup_ad_visible") is True
        or facts.get("countdown_visible") is True
        or (isinstance(topmost_ad_popup, Mapping) and topmost_ad_popup.get("is_ad") is True)
    )
    is_dismissible = (
        page_state in _DISMISS_TOUCH_PAGE_STATES
        or page_type in {"popup", "modal", "overlay", "dialog"}
        or bool(analysis.overlay_signature)
        or any(
            facts.get(key) is True
            for key in ("popup_visible", "dialog_visible", "auth_gate_visible")
        )
        or isinstance(facts.get("trusted_accessibility_dismiss"), Mapping)
    )
    return is_ad if role == "skip" else (is_dismissible or is_ad)


def _is_signed_ad_landing_intent(intent: ActionIntent) -> bool:
    parameters = intent.parameters
    return (
        intent.action_type in _COORDINATE_TOUCH_ACTIONS
        and parameters.get("capture_ad_landing_evidence") is True
        and parameters.get("ad_landing_capability") == "ad_outbound"
        and parameters.get("app_ad_landing_contract") == _APP_AD_LANDING_CONTRACT_VERSION
    )


def _is_post_publish_type_selection(
    *,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
) -> bool:
    """只放行当前列表页类型选择层的真实视觉候选。"""

    page_type = _normalized_visual_label(analysis.visual_page_type)
    page_state = _normalized_visual_label(analysis.page_state)
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    overlay_visible = (
        page_state in {"popup", "modal", "overlay", "dialog"}
        or bool(analysis.overlay_signature)
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
    )
    if (
        page_type not in {"post_list", "content_list"}
        or not overlay_visible
        or _is_ad_surface(analysis)
        or source_candidate.semantic_name != current_candidate.semantic_name
        or source_candidate.semantic_name != intent.semantic_target
        or _normalized_visual_label(source_candidate.expected_state_hint) != "post_publish"
        or _normalized_visual_label(current_candidate.expected_state_hint) != "post_publish"
        or not _current_candidate_risk_allowed(
            current_candidate,
            intent=intent,
            source_role=None,
        )
    ):
        return False
    terms = " ".join(
        (
            source_candidate.semantic_name,
            source_candidate.purpose,
            current_candidate.purpose,
            source_candidate.expected_state_hint or "",
        )
    ).casefold()
    if any(
        marker in terms
        for marker in (
            "提交发布",
            "确认发布",
            "立即发布",
            "submit post",
            "confirm post",
        )
    ):
        return False
    return any(marker in terms for marker in ("发帖", "发布", "post"))


def _is_controlled_system_dialog_intent(intent: ActionIntent) -> bool:
    parameters = intent.parameters
    return (
        intent.action_type is InteractionOperation.CLICK
        and parameters.get("surface_phase_key") == "system_dialog"
        and isinstance(parameters.get("surface_phase_run_id"), str)
        and bool(parameters["surface_phase_run_id"].strip())
        and isinstance(parameters.get("surface_phase_decision_id"), str)
        and bool(parameters["surface_phase_decision_id"].strip())
    )


def _is_ad_surface(analysis: AppVisualAnalysis) -> bool:
    page_state = _normalized_visual_label(analysis.page_state)
    page_type = _normalized_visual_label(analysis.visual_page_type)
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    return (
        page_state in {"startup_ad", "countdown"}
        or page_type in _AD_TOUCH_PAGE_TYPES
        or facts.get("startup_ad_visible") is True
        or facts.get("countdown_visible") is True
        or (
            isinstance(facts.get("topmost_ad_popup"), Mapping)
            and facts["topmost_ad_popup"].get("is_ad") is True
        )
    )


def _is_system_dialog_surface(analysis: AppVisualAnalysis) -> bool:
    states = {
        _normalized_visual_label(analysis.page_state),
        _normalized_visual_label(analysis.visual_page_type),
    }
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    return bool(
        states & {"system_dialog", "permission_dialog", "browser_permission"}
        or facts.get("system_dialog_visible") is True
        or facts.get("permission_dialog_visible") is True
    )


def _roleless_obstructed_touch_is_controlled(
    *,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
) -> bool:
    if (
        _is_signed_ad_landing_intent(intent)
        and _is_ad_surface(analysis)
        and belongs_to_topmost_ad_popup(current_candidate, analysis)
    ):
        return True
    if (
        _is_age_confirm_visual_candidate(source_candidate)
        and _is_age_confirm_visual_candidate(current_candidate)
        and observation_needs_age_confirm(
            page_state=analysis.page_state,
            page_type=analysis.visual_page_type,
            surface_facts=analysis.surface_facts,
        )
    ):
        return True
    if _is_post_publish_type_selection(
        analysis=analysis,
        intent=intent,
        source_candidate=source_candidate,
        current_candidate=current_candidate,
    ):
        return True
    if _is_controlled_bottom_navigation_touch(
        analysis=analysis,
        source_candidate=source_candidate,
        current_candidate=current_candidate,
    ):
        return True
    return _is_controlled_system_dialog_intent(intent) and _is_system_dialog_surface(analysis)


_BOTTOM_NAV_TOUCH_DISCOVERY_SOURCES = frozenset(
    {
        DISCOVERY_SOURCE_UIA_ISSUED,
        DISCOVERY_SOURCE_UIA_ALIGNED,
        DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV,
        DISCOVERY_SOURCE_PROMOTED_COVERAGE,
        # 黑料等 WebView／自绘底栏常无 a11y；Vision 标 frontier_scope=app 且在底部条带时须可触。
        DISCOVERY_SOURCE_VISION,
    }
)


def _normalized_center(bounds: NormalizedBounds) -> tuple[float, float]:
    return (
        (float(bounds.left) + float(bounds.right)) / 2.0,
        (float(bounds.top) + float(bounds.bottom)) / 2.0,
    )


def _point_inside_normalized_bounds(
    *,
    x: float,
    y: float,
    bounds: NormalizedBounds,
) -> bool:
    return (
        float(bounds.left) <= x <= float(bounds.right)
        and float(bounds.top) <= y <= float(bounds.bottom)
    )


def _candidate_center_clears_overlay_body(
    candidate: AppVisualCandidate,
    analysis: AppVisualAnalysis,
) -> bool:
    bounds = candidate.normalized_bounds
    if bounds is None:
        return False
    cx, cy = _normalized_center(bounds)
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    topmost = facts.get("topmost_ad_popup")
    if isinstance(topmost, Mapping) and topmost.get("is_ad") is True:
        raw = topmost.get("normalized_bounds")
        if isinstance(raw, Mapping):
            try:
                ad_bounds = NormalizedBounds.from_dict(raw)
            except (TypeError, ValueError):
                ad_bounds = None
            if ad_bounds is not None and _point_inside_normalized_bounds(
                x=cx, y=cy, bounds=ad_bounds
            ):
                return False
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        overlay_close = controls.get("overlay_close")
        # facts 里 overlay_close 常为 bool；完整 bounds 仅在候选列表的 dismiss 上。
        if overlay_close is True:
            for item in analysis.candidates:
                if item.control_role != "dismiss" or item.normalized_bounds is None:
                    continue
                if _point_inside_normalized_bounds(
                    x=cx, y=cy, bounds=item.normalized_bounds
                ):
                    return False
    return True


def _is_controlled_bottom_navigation_touch(
    *,
    analysis: AppVisualAnalysis,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
    intent: ActionIntent | None = None,
) -> bool:
    """遮挡页允许点击底部条带上、中心不落蒙层的受信底栏。"""

    seek = bool(
        intent is not None
        and (
            intent.parameters.get("bottom_navigation_tab_seek") is True
            or intent.parameters.get("require_runtime_bottom_nav_rebind") is True
        )
    )
    # inject／跨页重绑源候选可能尚未标 frontier_scope=app，但意图已声明底栏巡检。
    if current_candidate.frontier_scope != "app" and not seek:
        return False
    if source_candidate.frontier_scope != "app" and not seek:
        return False
    bounds = current_candidate.normalized_bounds
    if bounds is None or float(bounds.top) < 0.78:
        return False
    discovery = (
        current_candidate.discovery_source
        or source_candidate.discovery_source
        or ""
    )
    if discovery not in _BOTTOM_NAV_TOUCH_DISCOVERY_SOURCES and not seek:
        return False
    return _candidate_center_clears_overlay_body(current_candidate, analysis)


def _is_controlled_home_top_category_touch(
    *,
    analysis: AppVisualAnalysis,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
    intent: ActionIntent | None = None,
) -> bool:
    """主页上方分类：允许在 page_state=unknown 的 home 帧上触控课纲栏。"""

    del source_candidate
    if intent is None or intent.parameters.get("home_top_category_tab_seek") is not True:
        return False
    if current_candidate.action_type not in _COORDINATE_TOUCH_ACTIONS:
        return False
    bounds = current_candidate.normalized_bounds
    if bounds is None:
        return False
    center_y = (float(bounds.top) + float(bounds.bottom)) / 2.0
    if not (0.10 <= center_y <= 0.18):
        return False
    page_type = _normalized_visual_label(analysis.visual_page_type)
    if page_type and page_type != "home":
        return False
    # 与 Explorer／popup gate 对齐：软浮层误标 page_state=popup（国庆贴／侧标）
    # 且主页栏已露出时放行。禁止再用 _touch_visual_is_obstructed——
    # 它把任意 popup 当挡路，像素槽已对准「最新」仍 REVALIDATION 废轮。
    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
    )

    return not analysis_blocks_home_top_category_tabs(analysis)


def _is_controlled_home_top_sample_touch(
    *,
    analysis: AppVisualAnalysis,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
    intent: ActionIntent | None = None,
) -> bool:
    """上方分类第一片／3D漫画取证：软浮层误标 popup 时仍允许点列表卡。"""

    del source_candidate
    if intent is None:
        return False
    if (
        intent.parameters.get("home_top_category_sample_seek") is not True
        and intent.parameters.get("forced_pixel_home_top_sample") is not True
    ):
        return False
    if current_candidate.action_type not in _COORDINATE_TOUCH_ACTIONS:
        return False
    bounds = current_candidate.normalized_bounds
    if bounds is None:
        return False
    center_y = (float(bounds.top) + float(bounds.bottom)) / 2.0
    # 列表左上第一片约 y=0.40；漫画 3D 卡可到下半屏，避开顶栏与底栏。
    if not (0.22 <= center_y <= 0.88):
        return False
    page_type = _normalized_visual_label(analysis.visual_page_type)
    if page_type and page_type != "home":
        return False
    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
    )

    return not analysis_blocks_home_top_category_tabs(analysis)


def _touch_visual_allows_candidate(
    analysis: AppVisualAnalysis,
    role: str | None | object,
    *,
    intent: ActionIntent,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
) -> bool:
    page_state = _normalized_visual_label(analysis.page_state)
    page_type = _normalized_visual_label(analysis.visual_page_type)
    # 年龄确认常出现在 WebView／自绘层：动作前帧可能尚未给出业务 page_type，
    # 但只要源／当前帧都是年龄进入候选，就必须允许清障触控。
    if (
        (
            intent.parameters.get("hjll_script") is True
            or intent.parameters.get("hlapp_script") is True
            or intent.parameters.get("hlwapp_script") is True
            or intent.parameters.get("pc51_script") is True
            or intent.parameters.get("product_script_orchestrated") is True
        )
        and current_candidate.normalized_bounds is not None
        and current_candidate.action_type in _COORDINATE_TOUCH_ACTIONS
    ):
        # 产品脚本的固定落点不靠当帧视觉框。
        return True
    if _age_confirm_touch_allowed(
        role=role,
        source_candidate=source_candidate,
        current_candidate=current_candidate,
    ):
        return True
    if _trusted_accessibility_obstruction_touch_allowed(
        analysis=analysis,
        role=role,
        current_candidate=current_candidate,
    ):
        # 同帧唯一 a11y 关闭／跳过已证明时，允许在 Vision 页型 unknown／splash 上触控。
        return True
    # 无 X 促销：合成蒙层空白点击不依赖 Vision 同语义控件／像素 X。
    if (
        role == "dismiss"
        and isinstance(intent.parameters.get("app_popup_scrim_dismiss"), Mapping)
        and current_candidate.normalized_bounds is not None
        and current_candidate.action_type in _COORDINATE_TOUCH_ACTIONS
    ):
        return True
    # 短剧解锁层左上角返回：合成坐标，不依赖 Vision 同语义「返回」控件。
    if (
        intent.parameters.get("short_drama_top_left_back") is True
        and current_candidate.normalized_bounds is not None
        and current_candidate.action_type in _COORDINATE_TOUCH_ACTIONS
    ):
        return True
    if (
        role == "dismiss"
        and _touch_visual_is_obstructed(analysis)
        and _prefer_pixel_calibrated_dismiss_candidate(
            analysis=analysis,
            candidates=(current_candidate,),
        )
        is not None
    ):
        # 当前帧已重验的像素关闭控件不依赖被弹层遮住的业务页型。
        return True
    if _is_controlled_bottom_navigation_touch(
        analysis=analysis,
        source_candidate=source_candidate,
        current_candidate=current_candidate,
        intent=intent,
    ):
        # 降级帧 page_type/page_state=unknown 时，仍允许受信底栏条带触控。
        return True
    if _is_controlled_home_top_category_touch(
        analysis=analysis,
        source_candidate=source_candidate,
        current_candidate=current_candidate,
        intent=intent,
    ):
        # 上方分类：page_type=home 但 page_state 抖动为 unknown 时仍允许点课纲栏。
        return True
    if _is_controlled_home_top_sample_touch(
        analysis=analysis,
        source_candidate=source_candidate,
        current_candidate=current_candidate,
        intent=intent,
    ):
        # 最新列表第一片：home|popup 软浮层不得 REVALIDATION 废轮。
        return True
    if _is_reload_retry_touch_allowed(
        analysis=analysis,
        intent=intent,
        source_candidate=source_candidate,
        current_candidate=current_candidate,
    ):
        # 开屏后空白／line_loading「重试重新加载」允许在空页型上触控。
        return True
    if page_state in {"", "unknown"} or page_type in {"", "unknown"}:
        return False
    if not _touch_visual_is_obstructed(analysis):
        if page_state in _STABLE_TOUCH_PAGE_STATES:
            return True
        # 主页软浮层常被标 popup，栏位已露出时仍须能点列表／分类。
        return _is_soft_home_overlay_mislabeled(analysis)
    return _known_obstruction_matches_role(analysis, role) or (
        role is None
        and _roleless_obstructed_touch_is_controlled(
            analysis=analysis,
            intent=intent,
            source_candidate=source_candidate,
            current_candidate=current_candidate,
        )
    )


def _trusted_accessibility_obstruction_touch_allowed(
    *,
    analysis: AppVisualAnalysis,
    role: str | None | object,
    current_candidate: AppVisualCandidate,
) -> bool:
    """同帧唯一 a11y 清障事实可覆盖 Vision 页型抖动。"""

    if role not in {"dismiss", "skip"}:
        return False
    if current_candidate.control_role != role:
        return False
    if current_candidate.normalized_bounds is None:
        return False
    if analysis.package_name is None:
        return False
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    key = (
        "trusted_accessibility_dismiss"
        if role == "dismiss"
        else "trusted_accessibility_skip"
    )
    trusted = facts.get(key)
    if not isinstance(trusted, Mapping):
        return False
    if trusted.get("package_name") != analysis.package_name:
        return False
    label = trusted.get("label")
    raw_bounds = trusted.get("normalized_bounds")
    if not isinstance(label, str) or not label.strip() or not isinstance(raw_bounds, Mapping):
        return False
    expected_prefix = "关闭弹层：" if role == "dismiss" else "跳过广告："
    if current_candidate.semantic_name != f"{expected_prefix}{label.strip()}":
        return False
    try:
        trusted_bounds = NormalizedBounds(
            float(raw_bounds["left"]),
            float(raw_bounds["top"]),
            float(raw_bounds["right"]),
            float(raw_bounds["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    current_bounds = current_candidate.normalized_bounds
    return (
        abs(current_bounds.center[0] - trusted_bounds.center[0])
        <= APP_OBSTRUCTION_CONTROL_REBIND_POSITION_TOLERANCE
        and abs(current_bounds.center[1] - trusted_bounds.center[1])
        <= APP_OBSTRUCTION_CONTROL_REBIND_POSITION_TOLERANCE
    )


_RELOAD_RETRY_MARKERS = (
    "重试重新加载",
    "重新加载",
    "点击重试",
    "加载失败",
    "reload",
)


def _joined_visual_retry_text(
    *parts: object,
) -> str:
    return " ".join(str(part or "") for part in parts).casefold()


def _text_looks_like_reload_retry(*parts: object) -> bool:
    joined = _joined_visual_retry_text(*parts)
    return any(marker in joined for marker in _RELOAD_RETRY_MARKERS)


def _is_reload_retry_touch_allowed(
    *,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
) -> bool:
    """空白／line_loading 加载失败页上的重试键，允许在空 page_type 触控。"""

    if not _text_looks_like_reload_retry(
        source_candidate.semantic_name,
        source_candidate.purpose,
        source_candidate.expected_state_hint,
        current_candidate.semantic_name,
        current_candidate.purpose,
        current_candidate.expected_state_hint,
        intent.semantic_target,
        intent.parameters.get("purpose"),
    ):
        return False
    page_state = _normalized_visual_label(analysis.page_state)
    page_type = _normalized_visual_label(analysis.visual_page_type)
    if page_type not in {"", "unknown", "line_loading"} and page_state not in {
        "",
        "unknown",
        "stable",
        "normal",
        "loaded",
    }:
        return False
    bounds = current_candidate.normalized_bounds
    if bounds is None:
        return False
    if _candidate_in_system_gesture_zone(current_candidate):
        return False
    return True


def _age_confirm_touch_allowed(
    *,
    role: str | None | object,
    source_candidate: AppVisualCandidate,
    current_candidate: AppVisualCandidate,
) -> bool:
    """年龄门进入键允许在 page_type 未知时触控，禁止误放行其它 dismiss。"""

    if role not in {"dismiss", None}:
        return False
    return (
        _is_age_confirm_visual_candidate(source_candidate)
        and _is_age_confirm_visual_candidate(current_candidate)
    )


def _candidate_revalidation_error(
    message: str,
    *,
    error_code: str = APP_CANDIDATE_REVALIDATION_REQUIRED,
) -> AppInteractionRuntimeError:
    return AppInteractionRuntimeError(
        message,
        error_code=error_code,
        retryable=True,
    )


def _prefer_trusted_accessibility_skip_candidate(
    *,
    analysis: AppVisualAnalysis,
    candidates: tuple[AppVisualCandidate, ...],
) -> tuple[AppVisualCandidate, ...] | None:
    """当前帧已证明唯一 a11y 开屏退出时，优先采用该控件坐标。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    trusted = facts.get("trusted_accessibility_skip")
    if not isinstance(trusted, Mapping):
        return None
    if trusted.get("package_name") != analysis.package_name:
        return None
    label = trusted.get("label")
    raw_bounds = trusted.get("normalized_bounds")
    if not isinstance(label, str) or not label.strip() or not isinstance(raw_bounds, Mapping):
        return None
    try:
        bounds = NormalizedBounds(
            float(raw_bounds["left"]),
            float(raw_bounds["top"]),
            float(raw_bounds["right"]),
            float(raw_bounds["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    semantic_name = f"跳过广告：{label.strip()}"
    matching = tuple(
        item
        for item in candidates
        if item.control_role == "skip"
        and item.action_type in _COORDINATE_TOUCH_ACTIONS
        and item.semantic_name == semantic_name
        and item.normalized_bounds is not None
    )
    if len(matching) == 1:
        return matching
    built = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose="点击目标包唯一可访问性开屏退出控件，跳过广告进入业务页",
        control_role="skip",
        expected_state_hint="tap_skip",
    )
    return (built,)


def _prefer_trusted_accessibility_dismiss_candidate(
    *,
    analysis: AppVisualAnalysis,
    candidates: tuple[AppVisualCandidate, ...],
) -> tuple[AppVisualCandidate, ...] | None:
    """当前帧已证明唯一 a11y 关闭控件时，优先采用该控件坐标。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    trusted = facts.get("trusted_accessibility_dismiss")
    if not isinstance(trusted, Mapping):
        return None
    if trusted.get("package_name") != analysis.package_name:
        return None
    label = trusted.get("label")
    raw_bounds = trusted.get("normalized_bounds")
    if not isinstance(label, str) or not label.strip() or not isinstance(raw_bounds, Mapping):
        return None
    try:
        bounds = NormalizedBounds(
            float(raw_bounds["left"]),
            float(raw_bounds["top"]),
            float(raw_bounds["right"]),
            float(raw_bounds["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    semantic_name = f"关闭弹层：{label.strip()}"
    matching = tuple(
        item
        for item in candidates
        if item.control_role == "dismiss"
        and item.action_type in _COORDINATE_TOUCH_ACTIONS
        and item.semantic_name == semantic_name
        and item.normalized_bounds is not None
    )
    if len(matching) == 1:
        return matching
    built = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=f"点击目标包当前弹层的可信关闭控件「{label.strip()}」并继续清障",
        control_role="dismiss",
        expected_state_hint="close_popup_x",
    )
    return (built,)


def _prefer_pixel_calibrated_dismiss_candidate(
    *,
    analysis: AppVisualAnalysis,
    candidates: tuple[AppVisualCandidate, ...],
) -> tuple[AppVisualCandidate, ...] | None:
    """只在已通过重验的候选中优先选择当前帧唯一像素关闭控件。"""

    from services.api.app.application.app_visual_close_calibration import (
        is_payment_or_paywall_surface,
    )

    if is_payment_or_paywall_surface(analysis):
        return None
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    calibrated = facts.get("pixel_cross_close_calibration")
    if not isinstance(calibrated, Mapping):
        return None
    raw_bounds = calibrated.get("normalized_bounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    try:
        bounds = NormalizedBounds(
            float(raw_bounds["left"]),
            float(raw_bounds["top"]),
            float(raw_bounds["right"]),
            float(raw_bounds["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    # 中部／内容区孤立 X（含 ONIRENT 水印）禁止签发：会点穿进视频详情。
    # 顶角与底带圆 X（大图广告下可达 y≈0.55）与校准甜区对齐后放行。
    cy = (bounds.top + bounds.bottom) / 2.0
    cx = (bounds.left + bounds.right) / 2.0
    top_corner_ok = (
        cy <= 0.28 and bounds.top <= 0.30 and (cx <= 0.28 or cx >= 0.72)
    )
    # 与 app_visual_close_calibration 甜区对齐：大图模态下 X 可落在 0.52–0.66，可偏右至 cx≈0.96。
    bottom_banner_ok = (
        0.52 <= cy <= 0.90
        and bounds.top >= 0.48
        and bounds.bottom <= 0.96
        and 0.22 <= cx <= 0.96
    )
    if not (top_corner_ok or bottom_banner_ok):
        return None
    matching = tuple(
        item
        for item in candidates
        if item.control_role == "dismiss"
        and item.action_type in _COORDINATE_TOUCH_ACTIONS
        and item.normalized_bounds == bounds
        and (
            is_pixel_calibrated_close_candidate(item)
            or item.semantic_name in {"关闭广告弹窗", "跳过启动广告"}
        )
    )
    if len(matching) == 1:
        return matching
    # candidates 已通过语义、位置、风险和置信度校验；校准事实不能重造被排除的控件。
    return None


def _dismiss_a11y_diverges_from_pixel(
    *,
    a11y_candidate: AppVisualCandidate,
    pixel_candidate: AppVisualCandidate,
) -> bool:
    """a11y 关闭框相对像素精准点偏得过远或过大时，改信像素落点。"""

    a11y_bounds = a11y_candidate.normalized_bounds
    pixel_bounds = pixel_candidate.normalized_bounds
    if a11y_bounds is None or pixel_bounds is None:
        return False
    ax, ay = a11y_bounds.center
    px, py = pixel_bounds.center
    distance = ((ax - px) ** 2 + (ay - py) ** 2) ** 0.5
    a11y_area = max(0.0, (a11y_bounds.right - a11y_bounds.left) * (
        a11y_bounds.bottom - a11y_bounds.top
    ))
    return distance > 0.06 or a11y_area > 0.045


def _candidate_normalized_center_y(candidate: AppVisualCandidate) -> float | None:
    bounds = candidate.normalized_bounds
    if bounds is None:
        return None
    return (float(bounds.top) + float(bounds.bottom)) / 2.0


def _candidate_in_system_gesture_zone(candidate: AppVisualCandidate) -> bool:
    """落点中心或顶边进入系统手势带则视为危险 Vision 坐标。"""

    bounds = candidate.normalized_bounds
    if bounds is None:
        return False
    center_y = _candidate_normalized_center_y(candidate)
    if center_y is not None and center_y >= _GESTURE_ZONE_NORMALIZED_TOP:
        return True
    return float(bounds.top) >= _GESTURE_ZONE_NORMALIZED_TOP


def _candidate_from_trusted_a11y(candidate: AppVisualCandidate) -> bool:
    source = getattr(candidate, "discovery_source", None)
    return source in _TRUSTED_A11Y_DISCOVERY_SOURCES


def _trusted_a11y_action_controls(
    analysis: AppVisualAnalysis,
) -> tuple[AppVisualCandidate, ...]:
    """读取当前 Capture 已由既有 UIA provenance 认可的普通动作控件。"""

    return tuple(
        candidate
        for candidate in analysis.candidates
        if (
            _candidate_from_trusted_a11y(candidate)
            and candidate.action_type in _COORDINATE_TOUCH_ACTIONS
            and candidate.control_role is None
            and candidate.normalized_bounds is not None
            and candidate.source_capture_id == analysis.capture_id
        )
    )


def _prefer_reload_retry_rebind_candidate(
    *,
    source_candidate: AppVisualCandidate,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_role: str | None | object,
) -> tuple[AppVisualCandidate, ...] | None:
    """空白／加载失败页：按「重试／重新加载」语义重绑到当前帧唯一控件。"""

    if source_candidate.action_type not in _COORDINATE_TOUCH_ACTIONS:
        return None
    source_bounds = source_candidate.normalized_bounds
    source_center_x = 0.5
    source_center_y = 0.5
    if source_bounds is not None:
        source_center_x = (float(source_bounds.left) + float(source_bounds.right)) / 2.0
        source_center_y = (float(source_bounds.top) + float(source_bounds.bottom)) / 2.0
    pool = [
        item
        for item in analysis.candidates
        if item.action_type is intent.action_type
        and item.normalized_bounds is not None
        and not _candidate_in_system_gesture_zone(item)
        and _candidate_control_role(item) is not _INVALID_CONTROL_ROLE
        and (
            source_role is _INVALID_CONTROL_ROLE
            or _candidate_control_role(item) == source_role
            or _candidate_control_role(item) is None
        )
        and item.confidence >= _MIN_TOUCH_CANDIDATE_CONFIDENCE
        and _text_looks_like_reload_retry(
            item.semantic_name,
            item.purpose,
            item.expected_state_hint,
            intent.semantic_target,
        )
        and _current_candidate_risk_allowed(
            item,
            intent=intent,
            source_role=source_role,
        )
    ]
    if not pool:
        return None
    if len(pool) == 1:
        return (pool[0],)

    def _distance(item: AppVisualCandidate) -> float:
        bounds = item.normalized_bounds
        assert bounds is not None
        cx = (float(bounds.left) + float(bounds.right)) / 2.0
        cy = (float(bounds.top) + float(bounds.bottom)) / 2.0
        return abs(cx - source_center_x) + abs(cy - source_center_y)

    pool.sort(key=_distance)
    best = pool[0]
    if len(pool) > 1 and abs(_distance(pool[1]) - _distance(best)) < 0.05:
        return None
    return (best,)


def _is_current_bottom_navigation_candidate(
    candidate: AppVisualCandidate,
    *,
    intent: ActionIntent,
    source_role: str | None | object,
) -> bool:
    """当前底栏池只接受导航事实，不把相邻关闭或正文提升为导航。"""

    role = _candidate_control_role(candidate)
    if role is not None or source_role is not None:
        return False
    return candidate.frontier_scope == "app" or (
        _canonical_page_type_hint(candidate.expected_state_hint) is not None
        and _bottom_nav_label_tokens_overlap(candidate.semantic_name, intent.semantic_target)
    )


def _prefer_bottom_navigation_rebind_candidate(
    *,
    source_candidate: AppVisualCandidate,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_role: str | None | object,
) -> tuple[AppVisualCandidate, ...] | None:
    """底栏触控在文案改写后按底部带 + 水平中心重绑到唯一当前帧候选。"""

    if (
        source_candidate.frontier_scope != "app"
        and intent.parameters.get("bottom_navigation_tab_seek") is not True
        and intent.parameters.get("require_runtime_bottom_nav_rebind") is not True
    ):
        return None
    if source_candidate.action_type not in _COORDINATE_TOUCH_ACTIONS:
        return None
    source_bounds = source_candidate.normalized_bounds
    if source_bounds is None:
        return None
    source_center_x = (float(source_bounds.left) + float(source_bounds.right)) / 2.0
    tab_seek = (
        intent.parameters.get("bottom_navigation_tab_seek") is True
        or intent.parameters.get("require_runtime_bottom_nav_rebind") is True
    )
    # 同帧视觉像素底栏优先：WebView／自绘无 a11y 时比 Vision 手势区坐标准。
    pixel_pool = list(
        _rebuild_bottom_nav_candidates_from_pixel(
            analysis=analysis,
            intent=intent,
            source_role=source_role,
        )
    )
    # 有 a11y 底栏时次选像素条带重绑：Vision 常把 Tab 画进系统手势区
    # （top≥0.92），点下去会直接回桌面；不得先吃错误 Vision 池。
    a11y_pool = list(
        _rebuild_bottom_nav_candidates_from_accessibility(
            analysis=analysis,
            intent=intent,
            source_role=source_role,
            include_selected=tab_seek
            or source_candidate.frontier_scope == "app",
        )
    )
    pool: list[AppVisualCandidate] = []
    if pixel_pool:
        pool = pixel_pool
    elif a11y_pool:
        pool = a11y_pool
    else:
        pool = []
        for item in analysis.candidates:
            if item.action_type is not intent.action_type:
                continue
            if not _is_current_bottom_navigation_candidate(
                item, intent=intent, source_role=source_role,
            ):
                continue
            if item.normalized_bounds is None or float(item.normalized_bounds.top) < 0.78:
                continue
            # 当前框落入系统手势带时重新观察，不按固定高度补造触点。
            if _candidate_in_system_gesture_zone(item):
                continue
            if item.confidence < _MIN_TOUCH_CANDIDATE_CONFIDENCE:
                continue
            if not _current_candidate_risk_allowed(
                item,
                intent=intent,
                source_role=source_role,
            ):
                continue
            pool.append(item)
    if not pool:
        # WebView／自绘底栏常无 a11y：按底部带水平槽位从 Vision 候选重建。
        # 保留当前框的位置，不从手势带外推一个可能不存在的可点条带。
        rebuilt = _rebuild_bottom_nav_candidates_from_vision_band(
            analysis=analysis,
            intent=intent,
            source_role=source_role,
        )
        pool = [
            item for item in rebuilt if not _candidate_in_system_gesture_zone(item)
        ]
    if not pool:
        return None
    # 先按标签弱匹配，再按水平距离选最近且唯一。
    labeled = [
        item
        for item in pool
        if _bottom_nav_label_tokens_overlap(
            source_candidate.semantic_name,
            item.semantic_name,
            intent.semantic_target,
        )
    ]
    # 标签已唯一：跨页／Vision 改写时直接采纳，避免水平槽位微歧义烧死首页等栏。
    if tab_seek and len(labeled) == 1:
        return (labeled[0],)
    candidates = labeled or pool

    def _distance(item: AppVisualCandidate) -> float:
        bounds = item.normalized_bounds
        assert bounds is not None
        center_x = (float(bounds.left) + float(bounds.right)) / 2.0
        return abs(center_x - source_center_x)

    candidates.sort(key=_distance)
    best = candidates[0]
    max_distance = 0.28 if tab_seek else 0.18
    if _distance(best) > max_distance:
        return None
    # 次近项过近则视为歧义，拒绝重绑；巡检路径略收紧歧义门槛并优先标签池。
    ambiguity = 0.02 if tab_seek and labeled else 0.05
    if len(candidates) > 1 and abs(_distance(candidates[1]) - _distance(best)) < ambiguity:
        return None
    return (best,)


def _surface_blocks_bottom_nav_slot_synthesize(analysis: AppVisualAnalysis) -> bool:
    """付费墙／详情遮罩挡住底栏时，禁止用安全条带合成落点（避免空点烧回合）。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {"unlock_paywall", "paywall", "purchase", "vip_unlock"}:
        return True
    page_type = str(analysis.visual_page_type or "").casefold()
    if page_type not in {"video_detail", "content_detail"}:
        return False
    page_state = str(analysis.page_state or "").casefold()
    overlay_marked = (
        page_state in {"overlay", "popup", "modal", "dialog", "blocked"}
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
    )
    if not overlay_marked:
        return False
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        navigation = controls.get("bottom_navigation")
        if isinstance(navigation, (list, tuple)) and len(navigation) > 0:
            return False
    return True


def _synthesize_bottom_nav_slot_from_source(
    *,
    source_candidate: AppVisualCandidate,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
) -> tuple[AppVisualCandidate, ...] | None:
    """Flutter 无 a11y／滚动后 Vision 空池时，用源水平槽 + 安全底栏条带合成落点。

    仅用于底栏寻页／底栏 scope；禁止用于普通内容点击。
    """

    if _surface_blocks_bottom_nav_slot_synthesize(analysis):
        return None
    bounds = source_candidate.normalized_bounds
    if bounds is None:
        return None
    left = float(bounds.left)
    right = float(bounds.right)
    if right <= left:
        return None
    # 保留源水平槽，垂直落到安全底栏触控带，避开系统手势区。
    bottom = _SAFE_BOTTOM_NAV_TOUCH_BOTTOM
    top = min(_SAFE_BOTTOM_NAV_TOUCH_TOP, bottom - 0.04)
    top = max(0.78, top)
    semantic = (
        intent.semantic_target.strip()
        if isinstance(intent.semantic_target, str) and intent.semantic_target.strip()
        else source_candidate.semantic_name
    )
    purpose = (
        source_candidate.purpose
        or f"按底栏水平槽位重绑「{semantic}」并重新截图"
    )
    built = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic,
        action_type=intent.action_type,
        normalized_bounds=NormalizedBounds(left, top, right, bottom),
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role=source_candidate.control_role,
        expected_state_hint=source_candidate.expected_state_hint,
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV,
    )
    if _candidate_in_system_gesture_zone(built):
        return None
    if not _candidate_center_clears_overlay_body(built, analysis):
        return None
    return (built,)


def _rebuild_bottom_nav_candidates_from_pixel(
    *,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_role: str | None | object,
) -> tuple[AppVisualCandidate, ...]:
    """从同帧像素底栏校准事实重建重绑池。"""

    if source_role in {"dismiss", "skip"}:
        return ()
    tabs = extract_pixel_bottom_nav_tabs(analysis)
    if not tabs:
        # 候选 purpose 已带像素标记时亦可入池。
        rebuilt = [
            item
            for item in analysis.candidates
            if is_pixel_calibrated_bottom_nav_candidate(item)
            and item.action_type is intent.action_type
            and item.normalized_bounds is not None
            and float(item.normalized_bounds.top) >= 0.78
            and not _candidate_in_system_gesture_zone(item)
        ]
        return tuple(rebuilt)
    result: list[AppVisualCandidate] = []
    for tab in tabs:
        built = AppVisualCandidate.build(
            source_capture_id=analysis.capture_id,
            source_image_signature=analysis.image_signature,
            source_width=analysis.width,
            source_height=analysis.height,
            source_orientation=analysis.orientation,
            semantic_name=f"底栏「{tab.label}」",
            action_type=intent.action_type,
            normalized_bounds=tab.bounds,
            confidence=0.96,
            risk_level=InteractionRiskLevel.LOW,
            purpose=(
                f"点击底栏「{tab.label}」并截图留证；当前截图视觉像素底栏校准"
            ),
            expected_state_hint=f"tab:{tab.label}",
            frontier_scope="app",
            discovery_source=DISCOVERY_SOURCE_VISION,
        )
        if not _current_candidate_risk_allowed(
            built,
            intent=intent,
            source_role=source_role,
        ):
            continue
        if not _candidate_center_clears_overlay_body(built, analysis):
            continue
        if _candidate_in_system_gesture_zone(built):
            continue
        result.append(built)
    return tuple(result)


def _rebuild_bottom_nav_candidates_from_vision_band(
    *,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_role: str | None | object,
) -> tuple[AppVisualCandidate, ...]:
    """a11y 底栏为空时，用当前帧底部带 Vision 候选按水平槽位重建重绑池。"""

    if source_role in {"dismiss", "skip"}:
        return ()
    rebuilt: list[AppVisualCandidate] = []
    for item in analysis.candidates:
        if item.action_type is not intent.action_type:
            continue
        if not _is_current_bottom_navigation_candidate(
            item, intent=intent, source_role=source_role,
        ):
            continue
        bounds = item.normalized_bounds
        if bounds is None or float(bounds.top) < 0.78:
            continue
        if _candidate_in_system_gesture_zone(item):
            continue
        if item.confidence < _MIN_TOUCH_CANDIDATE_CONFIDENCE:
            continue
        if not _current_candidate_risk_allowed(
            item,
            intent=intent,
            source_role=source_role,
        ):
            continue
        if not _candidate_center_clears_overlay_body(item, analysis):
            continue
        # 强制 frontier_scope=app，便于后续触控闸门识别为底栏。
        if item.frontier_scope == "app":
            rebuilt.append(item)
            continue
        rebuilt.append(
            replace(
                item,
                frontier_scope="app",
            )
        )
    return tuple(rebuilt)


def _rebuild_bottom_nav_candidates_from_accessibility(
    *,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
    source_role: str | None | object,
    include_selected: bool = False,
) -> tuple[AppVisualCandidate, ...]:
    """candidates 被弹层裁掉时，从 a11y 底栏事实重建可重绑池。"""

    if source_role in {"dismiss", "skip"}:
        return ()
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    if not isinstance(controls, Mapping):
        return ()
    navigation = controls.get("bottom_navigation")
    if not isinstance(navigation, (list, tuple)) or not navigation:
        return ()
    width = int(analysis.width or 0)
    height = int(analysis.height or 0)
    if width <= 0 or height <= 0:
        return ()
    rebuilt: list[AppVisualCandidate] = []
    for item in navigation:
        if not isinstance(item, Mapping):
            continue
        if item.get("selected") is True and not include_selected:
            continue
        if item.get("action_allowed") is False:
            continue
        label = item.get("label")
        raw_bounds = item.get("bounds")
        if not isinstance(label, str) or not label.strip() or not isinstance(raw_bounds, Mapping):
            continue
        try:
            bounds = NormalizedBounds(
                float(raw_bounds["left"]) / width,
                float(raw_bounds["top"]) / height,
                float(raw_bounds["right"]) / width,
                float(raw_bounds["bottom"]) / height,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if float(bounds.top) < 0.78:
            continue
        page_type = item.get("page_type")
        if isinstance(page_type, str) and page_type.strip():
            expected_hint = page_type.strip()
            purpose = f"打开可见底栏导航「{label.strip()}」并重新截图，预期页面类型:{expected_hint}"
        else:
            expected_hint = f"tab:{label.strip()}"
            purpose = f"打开可见底栏导航「{label.strip()}」并重新截图"
        built = AppVisualCandidate.build(
            source_capture_id=analysis.capture_id,
            source_image_signature=analysis.image_signature,
            source_width=analysis.width,
            source_height=analysis.height,
            source_orientation=analysis.orientation,
            semantic_name=f"底栏「{label.strip()}」",
            action_type=intent.action_type,
            normalized_bounds=bounds,
            confidence=1.0,
            risk_level=InteractionRiskLevel.LOW,
            purpose=purpose,
            expected_state_hint=expected_hint,
            frontier_scope="app",
            discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
        )
        if not _current_candidate_risk_allowed(
            built,
            intent=intent,
            source_role=source_role,
        ):
            continue
        if not _candidate_center_clears_overlay_body(built, analysis):
            continue
        rebuilt.append(built)
    return tuple(rebuilt)


class AppCaptureReader(Protocol):
    async def get_capture(self, *, capture_id: str) -> PersistedAppCapture | None: ...


class AppVisualObservationRecord(Protocol):
    analysis: AppVisualAnalysis


class AppVisualObservationReader(Protocol):
    async def get_by_capture_id(
        self,
        *,
        capture_id: str,
    ) -> AppVisualObservationRecord | None: ...


@dataclass(frozen=True, kw_only=True)
class AppRuntimeIdentity:
    """动作执行前从真实设备读取的前台包与 Activity。"""

    package_name: str
    activity: str | None

    def __post_init__(self) -> None:
        if not self.package_name or self.package_name != self.package_name.strip():
            raise ValueError("package_name 必须为无首尾空白的非空字符串")
        if self.activity is not None and (
            not self.activity or self.activity != self.activity.strip()
        ):
            raise ValueError("activity 必须为 None 或无首尾空白的非空字符串")


class AppRuntimeIdentityReader(Protocol):
    """动作执行前读取真实设备前台身份的只读端口。"""

    async def get_runtime_identity(
        self,
        *,
        session: SurfaceSessionRef,
    ) -> AppRuntimeIdentity | None: ...


class DriverAppRuntimeIdentityReader:
    """动作前读取真实前台身份；优先 dumpsys，禁止为核包再拉全图。"""

    def __init__(self, driver: SurfaceDriver) -> None:
        self._driver = driver

    async def get_runtime_identity(
        self,
        *,
        session: SurfaceSessionRef,
    ) -> AppRuntimeIdentity | None:
        observation = await self._observe_runtime_identity(session=session)
        if (
            observation.task_id != session.task_id
            or observation.session_id != session.session_id
            or observation.surface_kind is not session.surface_kind
            or observation.surface_ref != session.primary_surface_ref
        ):
            return None
        payload = dict(observation.payload)
        package_name = payload.get("package_name")
        activity = payload.get("activity")
        if (
            not isinstance(package_name, str)
            or not package_name.strip()
            or package_name != package_name.strip()
            or not isinstance(activity, str)
            or not activity.strip()
            or activity != activity.strip()
        ):
            return None
        return AppRuntimeIdentity(package_name=package_name, activity=activity)

    async def _observe_runtime_identity(self, *, session: SurfaceSessionRef):
        """优先轻量身分查询；测试替身仍可走 observe()。"""

        lightweight = getattr(self._driver, "read_runtime_identity", None)
        if callable(lightweight):
            return await lightweight(session=session)
        return await self._driver.observe(
            session=session,
            profile=CaptureProfile(
                profile_id="app-runtime-identity-v1",
                before=CaptureDirective(role="runtime_identity", artifact_types=("screenshot",)),
                after=CaptureDirective(role="runtime_identity", artifact_types=("screenshot",)),
            ),
        )


@dataclass(frozen=True, kw_only=True)
class ResolvedAppCandidate:
    """只在 Runtime 内部存在的候选与像素目标。"""

    candidate: AppVisualCandidate
    before_capture: PersistedAppCapture
    resolved_target: dict[str, int | str] | None
    is_dynamic_page_sampling: bool = False


class AppCandidateResolver:
    """把候选身份绑定到最新 before Capture，并在内部推导像素坐标。"""

    def __init__(
        self,
        *,
        visual_observations: AppVisualObservationReader,
        captures: AppCaptureReader,
        prepare_before_visual: Callable[..., Awaitable[object]] | None = None,
    ) -> None:
        self._visual_observations = visual_observations
        self._captures = captures
        self._prepare_before_visual = prepare_before_visual

    async def resolve(self, request: ExecuteInteractionRequest) -> ResolvedAppCandidate:
        intent = request.intent
        from services.api.app.application.app_product_orchestration_policy import (
            is_fixed_script_parameters,
        )

        if is_fixed_script_parameters(intent.parameters):
            raise AppInteractionRuntimeError(
                "aliothapp2.0 已停用产品固定脚本，请重新发起通用 Agent 探索",
                error_code="LEGACY_PRODUCT_SCRIPT_DISABLED",
            )
        if intent.surface_kind is SurfaceKind.WEB:
            raise AppInteractionRuntimeError(
                "App Runtime 不接受 Web ActionIntent",
                error_code="SURFACE_KIND_MISMATCH",
            )
        parameters = dict(intent.parameters)
        if "app_coverage_page_seek" in parameters:
            raise AppInteractionRuntimeError(
                "已禁用 app_coverage_page_seek 合同，必须重新观察并选择真实视觉候选",
                error_code="VISUAL_CANDIDATE_MISMATCH",
            )
        candidate_id = parameters.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise AppInteractionRuntimeError(
                "App ActionIntent 缺少 candidate_id",
                error_code="VISUAL_CANDIDATE_REQUIRED",
            )
        saved = await self._visual_observations.get_by_capture_id(
            capture_id=intent.source_capture_id
        )
        if saved is None:
            raise AppInteractionRuntimeError(
                "App 视觉观察不存在",
                error_code="VISUAL_OBSERVATION_NOT_FOUND",
            )
        candidate = next(
            (item for item in saved.analysis.candidates if item.candidate_id == candidate_id),
            None,
        )
        if candidate is None:
            candidate = _resolve_curriculum_observation_candidate(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_dynamic_startup_observation_candidate(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_unstable_page_reobserve_candidate(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_unstable_page_back_candidate(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_curriculum_scroll_candidate(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_dynamic_page_sampling_candidate(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_popup_close_corner_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_startup_ad_skip_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_profile_credential_save_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_profile_credential_dismiss_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_popup_clearance_observe_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_popup_scrim_dismiss_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_forced_pixel_bottom_nav_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_forced_home_top_category_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_forced_home_top_sample_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_short_drama_top_left_back_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_forced_system_back_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_hjll_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_hlapp_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_hlwapp_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_51pc_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_hlwwhite_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_pzhan_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_jinguo_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_mama_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            candidate = _resolve_jmcomic_script_from_intent(
                intent=intent,
                analysis=saved.analysis,
                candidate_id=candidate_id,
            )
        if candidate is None:
            raise AppInteractionRuntimeError(
                "candidate_id 不属于源视觉观察",
                error_code="VISUAL_CANDIDATE_NOT_FOUND",
            )
        if (
            candidate.action_type is not intent.action_type
            or candidate.semantic_name != intent.semantic_target
            or not _source_candidate_risk_matches_intent(candidate, intent)
        ):
            raise AppInteractionRuntimeError(
                "ActionIntent 与视觉候选合同不一致",
                error_code="VISUAL_CANDIDATE_MISMATCH",
            )
        before = await self._captures.get_capture(capture_id=request.source_capture_id)
        if before is None:
            raise AppInteractionRuntimeError(
                "动作前 App Capture 不存在",
                error_code="BEFORE_CAPTURE_NOT_FOUND",
            )
        if (
            before.capture.task_id != intent.task_id
            or before.capture.session_id != intent.session_id
            or before.capture.surface_kind is not intent.surface_kind
            or before.capture.role != "before_action"
            or before.capture.capture_id != request.source_capture_id
            or before.capture.parent_capture_id != intent.source_capture_id
            or before.capture.action_execution_id != request.action_execution_id
        ):
            raise AppInteractionRuntimeError(
                "动作前 App Capture 身份不一致",
                error_code="BEFORE_CAPTURE_IDENTITY_MISMATCH",
            )
        analysis = saved.analysis
        if _is_coordinate_touch_action(intent.action_type):
            return await self._resolve_touch_candidate(
                request=request,
                source_analysis=analysis,
                source_candidate=candidate,
                before=before,
            )
        if (
            # 候选必须仍来自当前已验证的视觉事实；不能把别的 Capture 的候选
            # 借给本次动作。AppVisualAnalysis 也会在构造时校验这些字段，
            # 这里再次显式校验是为了让 Runtime 的 fail-closed 边界自洽。
            candidate.source_capture_id != intent.source_capture_id
            or candidate.source_image_signature != analysis.image_signature
            or candidate.source_width != analysis.width
            or candidate.source_height != analysis.height
            or candidate.source_orientation != analysis.orientation
            # Coordinator 在同一动作原子边界内生成 before_action 保全截图，
            # 截图像素可能因系统弹窗/动画发生变化，但设备 Surface 身份、
            # 尺寸和方向仍必须与已验证来源一致。
            or before.width != analysis.width
            or before.height != analysis.height
            or before.orientation != analysis.orientation
            or analysis.package_name != before.package_name
            or analysis.activity != before.activity
        ):
            raise AppInteractionRuntimeError(
                "动作前截图已偏离候选来源",
                error_code="STALE_VISUAL_CANDIDATE",
                retryable=True,
            )
        resolved_target = _resolve_pixel_target(candidate, before)
        return ResolvedAppCandidate(
            candidate=candidate,
            before_capture=before,
            resolved_target=resolved_target,
            is_dynamic_page_sampling=(
                candidate.normalized_bounds is None
                and resolved_target is None
                and intent.parameters.get("direction", "down") == "down"
                and not {
                    "point",
                    "x",
                    "y",
                    "coordinate_space",
                    "space",
                    "resolved_target",
                    "from",
                    "to",
                }.intersection(intent.parameters)
                and _resolve_dynamic_page_sampling_candidate(
                    intent=intent,
                    analysis=analysis,
                    candidate_id=candidate.candidate_id,
                ) == candidate
            ),
        )

    async def _resolve_touch_candidate(
        self,
        *,
        request: ExecuteInteractionRequest,
        source_analysis: AppVisualAnalysis,
        source_candidate: AppVisualCandidate,
        before: PersistedAppCapture,
    ) -> ResolvedAppCandidate:
        """只用动作前 Capture 对应的最新视觉候选计算触控坐标。"""

        if (
            source_analysis.capture_id != request.intent.source_capture_id
            or source_candidate.source_capture_id != source_analysis.capture_id
            or source_candidate.source_image_signature != source_analysis.image_signature
            or source_candidate.source_width != source_analysis.width
            or source_candidate.source_height != source_analysis.height
            or source_candidate.source_orientation != source_analysis.orientation
            or before.width != source_analysis.width
            or before.height != source_analysis.height
            or before.orientation != source_analysis.orientation
            or not _complete_visual_identity(source_analysis)
            or source_analysis.package_name != before.package_name
            or source_analysis.activity != before.activity
        ):
            raise _candidate_revalidation_error("动作前截图的 App 身份、尺寸或方向无法确认")

        source_role = _candidate_control_role(source_candidate)
        if source_role is _INVALID_CONTROL_ROLE:
            raise _candidate_revalidation_error("源视觉候选携带未知控件角色")

        product_script_intent = (
            request.intent.parameters.get("hjll_script") is True
            or request.intent.parameters.get("hlapp_script") is True
            or request.intent.parameters.get("hlwapp_script") is True
            or request.intent.parameters.get("pc51_script") is True
            or request.intent.parameters.get("product_script_orchestrated") is True
        )
        bottom_nav_intent = (
            not product_script_intent
            and (
                request.intent.parameters.get("bottom_navigation_tab_seek") is True
                or request.intent.parameters.get("require_runtime_bottom_nav_rebind") is True
                or source_candidate.frontier_scope == "app"
            )
        )
        home_top_intent = (
            request.intent.parameters.get("home_top_category_tab_seek") is True
        )
        home_top_sample_intent = (
            request.intent.parameters.get("home_top_category_sample_seek") is True
            or request.intent.parameters.get("forced_pixel_home_top_sample") is True
            or request.intent.parameters.get("home_top_sample_pixel_visual") is True
            or str(request.intent.parameters.get("candidate_id") or "").startswith(
                "forced-pixel-home-top-sample-"
            )
            or str(request.intent.parameters.get("candidate_id") or "").startswith(
                "forced-home-top-sample-"
            )
        )
        short_drama_back_intent = (
            request.intent.parameters.get("short_drama_top_left_back") is True
        )
        # 清障角色使用自己的当前控件重绑，不能由重试文本兜底复活已剔除目标。
        reload_retry_intent = source_role not in {"dismiss", "skip"} and (
            _text_looks_like_reload_retry(
                source_candidate.semantic_name,
                source_candidate.purpose,
                source_candidate.expected_state_hint,
                request.intent.semantic_target,
                request.intent.parameters.get("purpose"),
            )
        )

        if self._prepare_before_visual is not None:
            try:
                prepare_kwargs: dict[str, object] = {
                    "capture": before.capture,
                    "source": source_analysis,
                }
                if request.intent.parameters.get("splash_skip_after_only") is True:
                    # 开屏跳过／关闭：不重验 before，只留 after 证明广告已消失。
                    prepare_kwargs["force_reuse_reason"] = "splash_skip_before_reuse"
                await self._prepare_before_visual(**prepare_kwargs)
            except Exception as exc:  # noqa: BLE001 - before understanding 必须 fail-fast
                if (
                    getattr(exc, "error_code", None)
                    == "APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED"
                    and getattr(exc, "retryable", False) is False
                ):
                    raise
                raise AppInteractionRuntimeError(
                    "动作前 App Capture 视觉理解失败",
                    error_code="APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED",
                    retryable=False,
                ) from exc

        saved = await self._visual_observations.get_by_capture_id(
            capture_id=before.capture.capture_id
        )
        current_analysis = getattr(saved, "analysis", None) if saved is not None else None
        if not isinstance(current_analysis, AppVisualAnalysis):
            raise _candidate_revalidation_error(
                "动作前 Capture 缺少当前视觉观察，需重新观察后重选候选"
            )
        if (
            current_analysis.capture_id != before.capture.capture_id
            or current_analysis.screenshot_artifact_id not in before.capture.artifact_ids
            or current_analysis.image_signature != before.image_signature
            or current_analysis.width != before.width
            or current_analysis.height != before.height
            or current_analysis.orientation != before.orientation
            or not _complete_visual_identity(current_analysis)
            or current_analysis.package_name != before.package_name
            or current_analysis.activity != before.activity
            or current_analysis.package_name != source_analysis.package_name
            or current_analysis.activity != source_analysis.activity
            or (
                source_role not in {"dismiss", "skip"}
                and not bottom_nav_intent
                and not home_top_intent
                and not short_drama_back_intent
                and not reload_retry_intent
                and not product_script_intent
                and current_analysis.visual_page_type
                != source_analysis.visual_page_type
                and not _same_confirmed_visual_page(source_analysis, current_analysis)
            )
        ):
            raise _candidate_revalidation_error("动作前 Capture 的当前视觉事实已偏离源页面")
        current_candidates = tuple(
            item
            for item in current_analysis.candidates
            if (
                item.action_type is request.intent.action_type
                and _touch_semantic_matches(
                    source_candidate=source_candidate,
                    current_candidate=item,
                    intent=request.intent,
                )
                and _current_candidate_risk_allowed(
                    item,
                    intent=request.intent,
                    source_role=source_role,
                )
                and _candidate_control_role(item) == source_role
                and _candidate_control_role(item) is not _INVALID_CONTROL_ROLE
                and item.confidence >= _MIN_TOUCH_CANDIDATE_CONFIDENCE
            )
        )
        if source_role in {"dismiss", "skip"}:
            current_candidates = tuple(
                item
                for item in current_candidates
                if can_rebind_visual_obstruction_control(
                    source_candidate=source_candidate,
                    current_candidate=item,
                )
            )
        if source_role == "skip":
            # 开屏唯一 a11y 退出控件优先于易抖动的 Vision 同角色候选。
            preferred = _prefer_trusted_accessibility_skip_candidate(
                analysis=current_analysis,
                candidates=current_candidates,
            )
            if preferred is not None:
                current_candidates = preferred
            # 视觉／JSON 合成开屏关闭：无 a11y 时用合同坐标 rebound，禁止空池卡死。
            if (
                len(current_candidates) != 1
                and isinstance(
                    request.intent.parameters.get("app_startup_ad_skip"),
                    Mapping,
                )
                and source_candidate.normalized_bounds is not None
            ):
                rebound = AppVisualCandidate.build(
                    source_capture_id=current_analysis.capture_id,
                    source_image_signature=current_analysis.image_signature,
                    source_width=current_analysis.width,
                    source_height=current_analysis.height,
                    source_orientation=current_analysis.orientation,
                    semantic_name=source_candidate.semantic_name,
                    action_type=source_candidate.action_type,
                    normalized_bounds=source_candidate.normalized_bounds,
                    confidence=source_candidate.confidence,
                    risk_level=source_candidate.risk_level,
                    purpose=source_candidate.purpose,
                    control_role="skip",
                    expected_state_hint=source_candidate.expected_state_hint,
                )
                current_candidates = (rebound,)
        if source_role == "dismiss":
            # 像素校准与 a11y 同时存在时：a11y 偏得太远／框过大则改信像素精准点。
            preferred_a11y = _prefer_trusted_accessibility_dismiss_candidate(
                analysis=current_analysis,
                candidates=current_candidates,
            )
            preferred_pixel = _prefer_pixel_calibrated_dismiss_candidate(
                analysis=current_analysis,
                candidates=current_candidates,
            )
            trusted_dismiss_candidate = any(
                _candidate_from_trusted_a11y(item)
                and item.control_role == "dismiss"
                and item.action_type in _COORDINATE_TOUCH_ACTIONS
                and item.normalized_bounds is not None
                for item in current_candidates
            )
            current_candidates = filter_unproven_dismiss_candidates(
                candidates=current_candidates,
                trusted_action_controls=_trusted_a11y_action_controls(current_analysis),
                current_dismiss_proven=(
                    preferred_a11y is not None
                    or preferred_pixel is not None
                    or trusted_dismiss_candidate
                ),
            )
            if (
                preferred_pixel is not None
                and preferred_a11y is not None
                and _dismiss_a11y_diverges_from_pixel(
                    a11y_candidate=preferred_a11y[0],
                    pixel_candidate=preferred_pixel[0],
                )
            ):
                current_candidates = preferred_pixel
            elif preferred_a11y is not None:
                current_candidates = preferred_a11y
            elif preferred_pixel is not None:
                current_candidates = preferred_pixel
            # 显式产品脚本保留声明点位；普通探索必须由当前 before 的真实控件重绑，
            # 不能仅凭源帧合同把旧坐标贴成当前 Capture 的可信关闭控件。
            if (
                product_script_intent
                and len(current_candidates) != 1
                and isinstance(
                    request.intent.parameters.get("app_popup_close_corner"),
                    Mapping,
                )
                and source_candidate.normalized_bounds is not None
            ):
                rebound = AppVisualCandidate.build(
                    source_capture_id=current_analysis.capture_id,
                    source_image_signature=current_analysis.image_signature,
                    source_width=current_analysis.width,
                    source_height=current_analysis.height,
                    source_orientation=current_analysis.orientation,
                    semantic_name=source_candidate.semantic_name,
                    action_type=source_candidate.action_type,
                    normalized_bounds=source_candidate.normalized_bounds,
                    confidence=source_candidate.confidence,
                    risk_level=source_candidate.risk_level,
                    purpose=source_candidate.purpose,
                    control_role="dismiss",
                    expected_state_hint=source_candidate.expected_state_hint,
                )
                current_candidates = (rebound,)
            # 无 X 终身卡：点蒙层空白处。Vision 无同语义控件，合同坐标 rebound。
            if (
                len(current_candidates) != 1
                and isinstance(
                    request.intent.parameters.get("app_popup_scrim_dismiss"),
                    Mapping,
                )
                and source_candidate.normalized_bounds is not None
            ):
                rebound = AppVisualCandidate.build(
                    source_capture_id=current_analysis.capture_id,
                    source_image_signature=current_analysis.image_signature,
                    source_width=current_analysis.width,
                    source_height=current_analysis.height,
                    source_orientation=current_analysis.orientation,
                    semantic_name=source_candidate.semantic_name,
                    action_type=source_candidate.action_type,
                    normalized_bounds=source_candidate.normalized_bounds,
                    confidence=source_candidate.confidence,
                    risk_level=source_candidate.risk_level,
                    purpose=source_candidate.purpose,
                    control_role="dismiss",
                    expected_state_hint=source_candidate.expected_state_hint
                    or "dismiss_popup_scrim",
                )
                current_candidates = (rebound,)
            # 账号凭证「保存凭证」：before 帧常只给出「我已保存」，合同坐标 rebound。
            if (
                len(current_candidates) != 1
                and isinstance(
                    request.intent.parameters.get("app_profile_credential_save"),
                    Mapping,
                )
                and source_candidate.normalized_bounds is not None
            ):
                rebound = AppVisualCandidate.build(
                    source_capture_id=current_analysis.capture_id,
                    source_image_signature=current_analysis.image_signature,
                    source_width=current_analysis.width,
                    source_height=current_analysis.height,
                    source_orientation=current_analysis.orientation,
                    semantic_name=source_candidate.semantic_name,
                    action_type=source_candidate.action_type,
                    normalized_bounds=source_candidate.normalized_bounds,
                    confidence=source_candidate.confidence,
                    risk_level=source_candidate.risk_level,
                    purpose=source_candidate.purpose,
                    control_role="profile_credential_confirm",
                    expected_state_hint=source_candidate.expected_state_hint
                    or "save_profile_credential",
                )
                current_candidates = (rebound,)
            # 账号凭证「我已保存」：保存成功后 before 帧可能漏同语义，合同坐标 rebound。
            if (
                len(current_candidates) != 1
                and (
                    isinstance(
                        request.intent.parameters.get("app_profile_credential_dismiss"),
                        Mapping,
                    )
                    or request.intent.parameters.get("profile_credential_dismiss") is True
                )
                and source_candidate.normalized_bounds is not None
            ):
                rebound = AppVisualCandidate.build(
                    source_capture_id=current_analysis.capture_id,
                    source_image_signature=current_analysis.image_signature,
                    source_width=current_analysis.width,
                    source_height=current_analysis.height,
                    source_orientation=current_analysis.orientation,
                    semantic_name=source_candidate.semantic_name,
                    action_type=source_candidate.action_type,
                    normalized_bounds=source_candidate.normalized_bounds,
                    confidence=source_candidate.confidence,
                    risk_level=source_candidate.risk_level,
                    purpose=source_candidate.purpose,
                    control_role="profile_credential_dismiss",
                    expected_state_hint=source_candidate.expected_state_hint
                    or "dismiss_profile_credential",
                )
                current_candidates = (rebound,)
            # 年龄门进入键：before 帧常漏唯一同语义控件（或 enter/leave 双匹配），
            # 有合同坐标时 rebound 到当前 Capture，禁止空池／多选卡死（黑料 756cf695）。
            if (
                len(current_candidates) != 1
                and _is_age_confirm_visual_candidate(source_candidate)
                and source_candidate.normalized_bounds is not None
            ):
                page_state_now = str(current_analysis.page_state or "").casefold()
                facts_now = (
                    current_analysis.surface_facts
                    if isinstance(current_analysis.surface_facts, Mapping)
                    else {}
                )
                age_gate_frame = (
                    page_state_now in {"auth_gate", "dialog", "modal", "popup"}
                    or facts_now.get("dialog_kind") == "age_verification"
                    or facts_now.get("auth_gate_visible") is True
                    or observation_needs_age_confirm(
                        page_state=current_analysis.page_state,
                        page_type=current_analysis.visual_page_type,
                        surface_facts=facts_now,
                    )
                )
                if age_gate_frame:
                    rebound = AppVisualCandidate.build(
                        source_capture_id=current_analysis.capture_id,
                        source_image_signature=current_analysis.image_signature,
                        source_width=current_analysis.width,
                        source_height=current_analysis.height,
                        source_orientation=current_analysis.orientation,
                        semantic_name=source_candidate.semantic_name,
                        action_type=source_candidate.action_type,
                        normalized_bounds=source_candidate.normalized_bounds,
                        confidence=source_candidate.confidence,
                        risk_level=source_candidate.risk_level,
                        purpose=source_candidate.purpose,
                        control_role="dismiss",
                        expected_state_hint=source_candidate.expected_state_hint
                        or "confirm_age_18_enter",
                    )
                    current_candidates = (rebound,)
            elif len(current_candidates) > 1 and _is_age_confirm_visual_candidate(
                source_candidate
            ):
                # 同帧 enter／leave 皆 dismiss 时，优先下半屏进入键
                lower_half = tuple(
                    item
                    for item in current_candidates
                    if item.normalized_bounds is not None
                    and float(item.normalized_bounds.top) >= 0.55
                    and _is_age_confirm_visual_candidate(item)
                )
                if len(lower_half) == 1:
                    current_candidates = lower_half
        if bottom_nav_intent:
            # 底栏优先当前 a11y；Vision 也必须保留当前真实框，不能补造触点。
            rebound = _prefer_bottom_navigation_rebind_candidate(
                source_candidate=source_candidate,
                analysis=current_analysis,
                intent=request.intent,
                source_role=source_role,
            )
            current_candidates = rebound if rebound is not None else ()
        elif home_top_sample_intent:
            # 上方分类第一片／3D漫画取证：合成 forced-pixel 槽位。
            # before Capture 像素微变时不得因找不到「同语义唯一控件」而
            # POST_SCROLL_REBIND_REQUIRED 死循环（QC da8e96ee：最新已选中却
            # 连点第一片十余次全失败）。优先同帧左上内容卡，否则保留课纲框。
            sample_hit = _prefer_home_top_sample_rebind_candidate(
                source_candidate=source_candidate,
                analysis=current_analysis,
                intent=request.intent,
            )
            if sample_hit is not None:
                current_candidates = (sample_hit,)
            elif source_candidate.normalized_bounds is not None:
                current_candidates = (
                    AppVisualCandidate.build(
                        source_capture_id=current_analysis.capture_id,
                        source_image_signature=current_analysis.image_signature,
                        source_width=current_analysis.width,
                        source_height=current_analysis.height,
                        source_orientation=current_analysis.orientation,
                        semantic_name=source_candidate.semantic_name,
                        action_type=source_candidate.action_type,
                        normalized_bounds=source_candidate.normalized_bounds,
                        confidence=max(float(source_candidate.confidence or 0.0), 0.93),
                        risk_level=source_candidate.risk_level,
                        purpose=source_candidate.purpose,
                        expected_state_hint=source_candidate.expected_state_hint,
                        frontier_scope="local",
                        discovery_source=DISCOVERY_SOURCE_VISION,
                    ),
                )
            else:
                current_candidates = ()
        elif home_top_intent:
            # 上方分类：执行前优先同帧像素槽，再视觉文字框，禁止死用旧坐标。
            label = (
                str(request.intent.parameters.get("expected_state_hint") or "")
                .removeprefix("home_top_tab:")
                .strip()
                or str(request.intent.semantic_target or "")
            )
            from services.api.app.application.app_semantic_progress import (
                bottom_tab_label_already_visited,
            )
            from services.api.app.application.app_visual_home_top_category_calibration import (
                extract_pixel_home_top_category_tabs,
            )

            pixel_tabs = extract_pixel_home_top_category_tabs(current_analysis)
            pixel_hit = next(
                (
                    tab
                    for tab in pixel_tabs
                    if bottom_tab_label_already_visited(tab.label, {label})
                    or tab.label in label
                    or label in tab.label
                ),
                None,
            )
            if pixel_hit is not None:
                current_candidates = (
                    AppVisualCandidate.build(
                        source_capture_id=current_analysis.capture_id,
                        source_image_signature=current_analysis.image_signature,
                        source_width=current_analysis.width,
                        source_height=current_analysis.height,
                        source_orientation=current_analysis.orientation,
                        semantic_name=source_candidate.semantic_name,
                        action_type=source_candidate.action_type,
                        normalized_bounds=pixel_hit.bounds,
                        confidence=max(float(source_candidate.confidence or 0.0), 0.96),
                        risk_level=source_candidate.risk_level,
                        purpose=source_candidate.purpose,
                        expected_state_hint=source_candidate.expected_state_hint,
                        frontier_scope="local",
                        discovery_source=DISCOVERY_SOURCE_VISION,
                    ),
                )
            else:
                vision_hit = _match_home_top_category_visual_on_analysis(
                    analysis=current_analysis,
                    label=label,
                    intent_semantic=str(request.intent.semantic_target or ""),
                )
                if vision_hit is not None:
                    current_candidates = (vision_hit,)
                elif (
                    len(current_candidates) != 1
                    and source_candidate.normalized_bounds is not None
                ):
                    # 无像素／视觉文字框时保留源框（槽位合成），仍落到当前 Capture 身份。
                    current_candidates = (
                        AppVisualCandidate.build(
                            source_capture_id=current_analysis.capture_id,
                            source_image_signature=current_analysis.image_signature,
                            source_width=current_analysis.width,
                            source_height=current_analysis.height,
                            source_orientation=current_analysis.orientation,
                            semantic_name=source_candidate.semantic_name,
                            action_type=source_candidate.action_type,
                            normalized_bounds=source_candidate.normalized_bounds,
                            confidence=source_candidate.confidence,
                            risk_level=source_candidate.risk_level,
                            purpose=source_candidate.purpose,
                            expected_state_hint=source_candidate.expected_state_hint,
                            frontier_scope="local",
                            discovery_source=DISCOVERY_SOURCE_VISION,
                        ),
                    )
        elif short_drama_back_intent:
            # 短剧解锁层左上角 < ：Vision 常无同语义返回；合同坐标 rebound 到当前帧。
            bounds = source_candidate.normalized_bounds
            if bounds is None:
                from services.api.app.application.app_semantic_progress import (
                    SHORT_DRAMA_TOP_LEFT_BACK_BOUNDS,
                )

                try:
                    bounds = NormalizedBounds.from_dict(SHORT_DRAMA_TOP_LEFT_BACK_BOUNDS)
                except Exception:  # noqa: BLE001
                    bounds = None
            if bounds is not None:
                current_candidates = (
                    AppVisualCandidate.build(
                        source_capture_id=current_analysis.capture_id,
                        source_image_signature=current_analysis.image_signature,
                        source_width=current_analysis.width,
                        source_height=current_analysis.height,
                        source_orientation=current_analysis.orientation,
                        semantic_name=source_candidate.semantic_name or "左上角返回",
                        action_type=source_candidate.action_type,
                        normalized_bounds=bounds,
                        confidence=max(float(source_candidate.confidence or 0.0), 0.95),
                        risk_level=source_candidate.risk_level
                        or InteractionRiskLevel.LOW,
                        purpose=source_candidate.purpose
                        or "短剧解锁层后点击左上角返回",
                        expected_state_hint=source_candidate.expected_state_hint
                        or "back",
                        frontier_scope="local",
                        discovery_source=DISCOVERY_SOURCE_VISION,
                    ),
                )
        elif reload_retry_intent and len(current_candidates) != 1:
            rebound = _prefer_reload_retry_rebind_candidate(
                source_candidate=source_candidate,
                analysis=current_analysis,
                intent=request.intent,
                source_role=source_role,
            )
            if rebound is not None:
                current_candidates = rebound
        if (
            product_script_intent
            and source_candidate.normalized_bounds is not None
        ):
            # 腳本落點已寫死。動作前新截圖沒有同名框時仍點 hjll.py 的座標，
            # 禁止因重驗失敗把「關閉」留在 planned。
            current_candidates = (
                AppVisualCandidate.build(
                    source_capture_id=current_analysis.capture_id,
                    source_image_signature=current_analysis.image_signature,
                    source_width=current_analysis.width,
                    source_height=current_analysis.height,
                    source_orientation=current_analysis.orientation,
                    semantic_name=source_candidate.semantic_name,
                    action_type=source_candidate.action_type,
                    normalized_bounds=source_candidate.normalized_bounds,
                    confidence=max(float(source_candidate.confidence or 0.0), 0.99),
                    risk_level=source_candidate.risk_level or InteractionRiskLevel.LOW,
                    purpose=source_candidate.purpose,
                    expected_state_hint=source_candidate.expected_state_hint or "hjll_script",
                    frontier_scope="local",
                ),
            )
        if len(current_candidates) != 1:
            stale_after_motion = (
                source_analysis.image_signature != current_analysis.image_signature
                and source_role not in {"dismiss", "skip"}
                and not short_drama_back_intent
                and not home_top_sample_intent
            )
            raise _candidate_revalidation_error(
                (
                    "滚动或画面变化后未在新 Capture 上确认唯一同语义控件，禁止复用旧坐标"
                    if stale_after_motion
                    else "动作前 Capture 未确认唯一的同语义视觉控件"
                ),
                error_code=(
                    "POST_SCROLL_REBIND_REQUIRED"
                    if stale_after_motion
                    else APP_CANDIDATE_REVALIDATION_REQUIRED
                ),
            )
        current_candidate = current_candidates[0]
        source_w = int(source_candidate.source_width or 0)
        source_h = int(source_candidate.source_height or 0)
        current_w = int(current_analysis.width or 0)
        current_h = int(current_analysis.height or 0)
        aspect_drift = False
        if source_w > 0 and source_h > 0 and current_w > 0 and current_h > 0:
            source_ratio = source_w / source_h
            current_ratio = current_w / current_h
            aspect_drift = abs(source_ratio - current_ratio) > 0.02
        if (
            current_candidate.source_capture_id != current_analysis.capture_id
            or current_candidate.source_image_signature != current_analysis.image_signature
            or current_candidate.source_width != current_analysis.width
            or current_candidate.source_height != current_analysis.height
            or current_candidate.source_orientation != current_analysis.orientation
            or aspect_drift
            or current_candidate.normalized_bounds is None
            or not _touch_visual_allows_candidate(
                current_analysis,
                source_role,
                intent=request.intent,
                source_candidate=source_candidate,
                current_candidate=current_candidate,
            )
        ):
            raise _candidate_revalidation_error(
                (
                    "截图方向或宽高比已变化，禁止复用旧视觉坐标"
                    if aspect_drift
                    or current_candidate.source_orientation != current_analysis.orientation
                    else "动作前 Capture 未确认可安全触控的当前页面或控件"
                ),
                error_code=(
                    "STALE_VISUAL_CANDIDATE"
                    if aspect_drift
                    or current_candidate.source_orientation != current_analysis.orientation
                    else APP_CANDIDATE_REVALIDATION_REQUIRED
                ),
            )

        resolved_target = _resolve_pixel_target(current_candidate, before)
        return ResolvedAppCandidate(
            candidate=current_candidate,
            before_capture=before,
            resolved_target=resolved_target,
        )


def _resolve_curriculum_observation_candidate(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """只恢复平台签发的课纲 OBSERVE；任一身份字段不符都拒绝。"""

    if (
        intent.action_type is not InteractionOperation.OBSERVE
        or intent.risk_level is not InteractionRiskLevel.LOW
    ):
        return None
    payload = intent.parameters.get("app_curriculum_observation")
    if not isinstance(payload, Mapping):
        return None
    if set(payload) != {"protocol_version", "phase_id", "phase_action_count"}:
        return None
    if payload.get("protocol_version") != APP_CURRICULUM_OBSERVATION_VERSION:
        return None
    phase_id = payload.get("phase_id")
    action_count = payload.get("phase_action_count")
    if (
        not isinstance(phase_id, str)
        or not phase_id.strip()
        or phase_id != phase_id.strip()
        or isinstance(action_count, bool)
        or not isinstance(action_count, int)
        or action_count < 0
        or intent.parameters.get("app_curriculum_phase_id") != phase_id
    ):
        return None
    semantic_target = f"课纲零触控观察：{phase_id}：第{action_count + 1}帧"
    purpose = f"按课纲推进 {phase_id}，只观察当前屏幕，不执行触控"
    if intent.semantic_target != semantic_target or intent.purpose != purpose:
        return None
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.OBSERVE,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=phase_id,
    )
    return candidate if candidate.candidate_id == candidate_id else None


def _resolve_curriculum_scroll_candidate(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """只恢复平台签发的课纲安全滚动；滚动不携带臆造坐标。"""

    if (
        intent.action_type is not InteractionOperation.SCROLL
        or intent.risk_level is not InteractionRiskLevel.LOW
    ):
        return None
    payload = intent.parameters.get("app_curriculum_action")
    if not isinstance(payload, Mapping):
        return None
    if set(payload) != {
        "protocol_version",
        "phase_id",
        "phase_action_count",
        "action_type",
        "expected_state_hint",
    }:
        return None
    if payload.get("protocol_version") != APP_CURRICULUM_ACTION_VERSION:
        return None
    phase_id = payload.get("phase_id")
    action_count = payload.get("phase_action_count")
    expected_state_hint = payload.get("expected_state_hint")
    if (
        not isinstance(phase_id, str)
        or not phase_id.strip()
        or phase_id != phase_id.strip()
        or isinstance(action_count, bool)
        or not isinstance(action_count, int)
        or action_count < 0
        or payload.get("action_type") != InteractionOperation.SCROLL.value
        or not isinstance(expected_state_hint, str)
        or not expected_state_hint.strip()
        or expected_state_hint != expected_state_hint.strip()
        or expected_state_hint != phase_id
        or intent.parameters.get("app_curriculum_phase_id") != phase_id
    ):
        return None
    semantic_target = f"课纲安全下滑：{phase_id}：第{action_count + 1}帧"
    purpose = f"按课纲推进 {phase_id}，只执行安全下滑并重新截图"
    if intent.semantic_target != semantic_target or intent.purpose != purpose:
        return None
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.SCROLL,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=expected_state_hint,
    )
    return candidate if candidate.candidate_id == candidate_id else None


def _resolve_dynamic_startup_observation_candidate(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """只恢复平台签发的动态启动零触控观察，拒绝模型扩大次数或改写语义。"""

    if (
        intent.action_type is not InteractionOperation.OBSERVE
        or intent.risk_level is not InteractionRiskLevel.LOW
    ):
        return None
    payload = intent.parameters.get("app_dynamic_startup_observation")
    if not isinstance(payload, Mapping):
        return None
    if set(payload) != {"protocol_version", "observation_count", "observation_limit"}:
        return None
    observation_count = payload.get("observation_count")
    if (
        payload.get("protocol_version") != APP_DYNAMIC_STARTUP_OBSERVATION_VERSION
        or isinstance(observation_count, bool)
        or not isinstance(observation_count, int)
        or observation_count < 0
        or observation_count >= APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT
        or payload.get("observation_limit") != APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT
    ):
        return None
    states = {
        str(analysis.page_state or "").strip().casefold(),
        str(analysis.visual_page_type or "").strip().casefold(),
    }
    if not states & {
        "line_loading",
        "line_failure",
        "connection_failed",
        "connectivity_gate",
        "network_gate",
        "connecting",
        "startup_connection",
        "startup_ad",
        "countdown",
        "splash",
    }:
        return None
    page_hint = str(
        analysis.visual_page_type or analysis.page_state or "startup_transition"
    ).strip()
    semantic_target = f"动态启动零触控观察：第{observation_count + 1}帧"
    purpose = "等待线路检测或启动表面自然变化，不执行任何触控"
    if intent.semantic_target != semantic_target or intent.purpose != purpose:
        return None
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.OBSERVE,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=page_hint,
    )
    return candidate if candidate.candidate_id == candidate_id else None


def _resolve_unstable_page_reobserve_candidate(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """只恢复平台签发的不稳定页零触控观察。"""

    if (
        intent.action_type is not InteractionOperation.OBSERVE
        or intent.risk_level is not InteractionRiskLevel.LOW
    ):
        return None
    payload = intent.parameters.get("app_dynamic_unstable_page_reobserve")
    if not isinstance(payload, Mapping):
        return None
    if set(payload) != {"protocol_version", "reobserve_count", "reobserve_limit"}:
        return None
    reobserve_count = payload.get("reobserve_count")
    if (
        payload.get("protocol_version") != APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_VERSION
        or isinstance(reobserve_count, bool)
        or not isinstance(reobserve_count, int)
        or reobserve_count < 0
        or reobserve_count >= APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_LIMIT
        or payload.get("reobserve_limit") != APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_LIMIT
    ):
        return None
    if str(analysis.visual_page_type or "").strip():
        return None
    page_hint = str(analysis.page_state or "unknown_page").strip() or "unknown_page"
    semantic_target = f"不稳定页零触控观察：第{reobserve_count + 1}帧"
    purpose = "等待视觉页型稳定，不执行任何触控"
    if intent.semantic_target != semantic_target or intent.purpose != purpose:
        return None
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.OBSERVE,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=page_hint,
    )
    return candidate if candidate.candidate_id == candidate_id else None


def _resolve_unstable_page_back_candidate(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """只恢复平台签发的不稳定／加载观察达上限后的 BACK。"""

    if (
        intent.action_type is not InteractionOperation.BACK
        or intent.risk_level is not InteractionRiskLevel.LOW
    ):
        return None
    payload = intent.parameters.get("app_dynamic_unstable_page_back")
    if not isinstance(payload, Mapping):
        return None
    if set(payload) != {"protocol_version", "back_count", "back_limit"}:
        return None
    back_count = payload.get("back_count")
    if (
        payload.get("protocol_version") != APP_DYNAMIC_UNSTABLE_PAGE_BACK_VERSION
        or isinstance(back_count, bool)
        or not isinstance(back_count, int)
        or back_count < 0
        or back_count >= APP_DYNAMIC_UNSTABLE_PAGE_BACK_LIMIT
        or payload.get("back_limit") != APP_DYNAMIC_UNSTABLE_PAGE_BACK_LIMIT
    ):
        return None
    if intent.semantic_target != "系统 BACK 恢复稳定页":
        return None
    if intent.purpose not in {
        "启动／加载观察已达上限，通过系统 BACK 返回上一级并重新截图",
        "不稳定页观察已达上限，通过系统 BACK 返回上一级并重新截图",
    }:
        return None
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=intent.semantic_target,
        action_type=InteractionOperation.BACK,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=intent.purpose,
        expected_state_hint="back",
    )
    return candidate if candidate.candidate_id == candidate_id else None


def _resolve_dynamic_page_sampling_candidate(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """只恢复平台签发的动态页面采样滚动，拒绝模型改写页面身份。"""

    if (
        intent.action_type is not InteractionOperation.SCROLL
        or intent.risk_level is not InteractionRiskLevel.LOW
    ):
        return None
    payload = intent.parameters.get("app_dynamic_page_sampling")
    if not isinstance(payload, Mapping):
        return None
    if set(payload) != {"protocol_version", "business_page_scope", "page_type"}:
        return None
    if payload.get("protocol_version") != APP_DYNAMIC_PAGE_SAMPLING_VERSION:
        return None
    scope = payload.get("business_page_scope")
    page_type = payload.get("page_type")
    if (
        not isinstance(scope, str)
        or not scope.strip()
        or scope != scope.strip().casefold()
        or not isinstance(page_type, str)
        or not page_type.strip()
        or page_type != page_type.strip().casefold()
        or str(analysis.visual_page_type or "").strip().casefold() != page_type
    ):
        return None
    semantic_target = "动态页面安全下滑"
    purpose = f"动态采样当前业务页面下一屏；scope={scope}；page_type={page_type}"
    if intent.semantic_target != semantic_target or intent.purpose != purpose:
        return None
    candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.SCROLL,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=page_type,
    )
    return candidate if candidate.candidate_id == candidate_id else None


def _resolve_coverage_page_seek_candidate(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """旧覆盖寻页合同永久禁用，不从参数重建候选或坐标。"""

    return None


def _resolve_popup_close_corner_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    payload = intent.parameters.get("app_popup_close_corner")
    if not isinstance(payload, Mapping):
        return None
    return resolve_popup_close_corner_candidate(
        intent_semantic_target=str(intent.semantic_target or ""),
        intent_purpose=str(intent.purpose or ""),
        analysis=analysis,
        candidate_id=candidate_id,
        payload=payload,
    )


def _resolve_profile_credential_save_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    payload = intent.parameters.get("app_profile_credential_save")
    if not isinstance(payload, Mapping):
        return None
    return resolve_profile_credential_save_candidate(
        intent_semantic_target=str(intent.semantic_target or ""),
        intent_purpose=str(intent.purpose or ""),
        analysis=analysis,
        candidate_id=candidate_id,
        payload=payload,
    )


def _resolve_profile_credential_dismiss_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    payload = intent.parameters.get("app_profile_credential_dismiss")
    if not isinstance(payload, Mapping):
        return None
    return resolve_profile_credential_dismiss_candidate(
        intent_semantic_target=str(intent.semantic_target or ""),
        intent_purpose=str(intent.purpose or ""),
        analysis=analysis,
        candidate_id=candidate_id,
        payload=payload,
    )


def _resolve_startup_ad_skip_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    payload = intent.parameters.get("app_startup_ad_skip")
    if not isinstance(payload, Mapping):
        return None
    return resolve_startup_ad_skip_candidate(
        intent_semantic_target=str(intent.semantic_target or ""),
        intent_purpose=str(intent.purpose or ""),
        analysis=analysis,
        candidate_id=candidate_id,
        payload=payload,
    )


def _resolve_popup_clearance_observe_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    if intent.action_type is not InteractionOperation.OBSERVE:
        return None
    payload = intent.parameters.get("app_popup_clearance_observe")
    if not isinstance(payload, Mapping):
        return None
    return resolve_popup_clearance_observe_candidate(
        intent_semantic_target=str(intent.semantic_target or ""),
        intent_purpose=str(intent.purpose or ""),
        analysis=analysis,
        candidate_id=candidate_id,
        payload=payload,
    )


def _resolve_popup_scrim_dismiss_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    payload = intent.parameters.get("app_popup_scrim_dismiss")
    if not isinstance(payload, Mapping):
        return None
    return resolve_popup_scrim_dismiss_candidate(
        intent_semantic_target=str(intent.semantic_target or ""),
        intent_purpose=str(intent.purpose or ""),
        analysis=analysis,
        candidate_id=candidate_id,
        payload=payload,
    )


def _resolve_forced_pixel_bottom_nav_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """合成 forced-pixel-bottom-nav:*：从同帧像素槽或 Intent bounds 重建。"""

    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    if not candidate_id.startswith("forced-pixel-bottom-nav:"):
        return None
    if intent.parameters.get("bottom_navigation_tab_seek") is not True:
        return None
    tabs = extract_pixel_bottom_nav_tabs(analysis)
    label_hint = ""
    parts = candidate_id.split(":", 2)
    if len(parts) >= 3:
        label_hint = parts[-1].strip()
    semantic = str(intent.semantic_target or "")
    for prefix in ("底栏「", "底部导航「", "底栏"):
        if semantic.startswith(prefix):
            semantic = semantic[len(prefix) :].strip("「」 ")
            break
    needle = label_hint or semantic
    hit = None
    if needle and tabs:
        from services.api.app.application.app_semantic_progress import (
            bottom_tab_label_already_visited,
        )

        hit = next(
            (
                tab
                for tab in tabs
                if bottom_tab_label_already_visited(tab.label, {needle})
            ),
            None,
        )
    bounds = None
    label = needle or "底栏"
    if hit is not None:
        bounds = hit.bounds
        label = hit.label
    else:
        raw = intent.parameters.get("normalized_bounds")
        if not isinstance(raw, Mapping):
            return None
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
    if bounds is None:
        return None
    purpose = str(intent.purpose or "").strip() or (
        f"点击底栏「{label}」并截图留证；当前截图视觉像素底栏校准"
    )
    # 语义须与 Intent 全等，避免 VISUAL_CANDIDATE_MISMATCH。
    # candidate_id 保持视觉哈希（与 forced-pixel-popup-close 相同）；Intent 侧仍用 forced 前缀找解析器。
    semantic_name = str(intent.semantic_target or "").strip() or f"底栏「{label}」"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=bounds,
        confidence=0.96,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=f"tab:{label}",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _resolve_hjll_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """HJLL 腳本步的落點來自 hjll.py，不要求當幀視覺框同名。"""

    if intent.parameters.get("hjll_script") is not True:
        return None
    if not candidate_id.startswith("hjll-script:"):
        return None
    bounds = None
    raw = intent.parameters.get("normalized_bounds")
    if isinstance(raw, Mapping):
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
    if intent.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP} and bounds is None:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "HJLL 脚本步骤"
    purpose = str(intent.purpose or "").strip() or "按 hjll.py 执行固定步骤并截图留证"
    # 支付鏈步驟的意圖是 medium。候選若仍寫死 low，會在執行前被
    # VISUAL_CANDIDATE_MISMATCH 退回，點到不了手機。
    risk_level = (
        intent.risk_level
        if intent.risk_level in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        else InteractionRiskLevel.LOW
    )
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=bounds,
        confidence=0.99,
        risk_level=risk_level,
        purpose=purpose,
        expected_state_hint="hjll_script",
        frontier_scope="local",
    )


def _resolve_hlapp_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """HLAPP 脚本步的落点来自 hlapp.py，不要求当帧视觉框同名。"""

    if intent.parameters.get("hlapp_script") is not True:
        return None
    if not candidate_id.startswith("hlapp-script:"):
        return None
    bounds = None
    raw = intent.parameters.get("normalized_bounds")
    if isinstance(raw, Mapping):
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
    if intent.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP} and bounds is None:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "HLAPP 脚本步骤"
    purpose = str(intent.purpose or "").strip() or "按 hlapp.py 执行固定步骤并截图留证"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=bounds,
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="hlapp_script",
        frontier_scope="local",
    )


def _resolve_hlwapp_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """黑料网脚本步的落点来自 hlwapp.py，不要求当帧视觉框同名。"""

    if intent.parameters.get("hlwapp_script") is not True:
        return None
    if not candidate_id.startswith("hlwapp-script:"):
        return None
    bounds = None
    raw = intent.parameters.get("normalized_bounds")
    if isinstance(raw, Mapping):
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
    if intent.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP} and bounds is None:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "黑料网脚本步骤"
    purpose = str(intent.purpose or "").strip() or "按 hlwapp.py 执行固定步骤并截图留证"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=bounds,
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="hlwapp_script",
        frontier_scope="local",
    )


def _resolve_51pc_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """51pc 只整份執行錄製腳本，不從當幀視覺框取點。"""

    if intent.parameters.get("pc51_script") is not True:
        return None
    if candidate_id != "pc51-script:run":
        return None
    if intent.action_type is not InteractionOperation.OBSERVE:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "执行51pc录制脚本"
    purpose = str(intent.purpose or "").strip() or "启动 51pc.py，截图交给 Agent 记录与验证"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=None,
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="pc51_script",
        frontier_scope="local",
    )


def _resolve_hlwwhite_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """hlwwhite 只整份執行錄製腳本，不從當幀視覺框取點。"""

    if intent.parameters.get("hlwwhite_script") is not True:
        return None
    if candidate_id != "hlwwhite-script:run":
        return None
    if intent.action_type is not InteractionOperation.OBSERVE:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "执行hlwwhite录制脚本"
    purpose = str(intent.purpose or "").strip() or "启动 hlwwhite.py，截图交给 Agent 记录与验证"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=None,
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="hlwwhite_script",
        frontier_scope="local",
    )


def _resolve_jmcomic_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """禁漫脚本步的落点来自 jmcomic.py，留证截图不要求当帧视觉框。"""

    if intent.parameters.get("jmcomic_script") is not True:
        return None
    if not candidate_id.startswith("jmcomic-script:"):
        return None
    bounds = None
    raw = intent.parameters.get("normalized_bounds")
    if isinstance(raw, Mapping):
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
    if intent.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP} and bounds is None:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "禁漫脚本步骤"
    purpose = str(intent.purpose or "").strip() or "按 jmcomic.py 执行固定步骤并截图留证"
    risk_level = (
        intent.risk_level
        if intent.risk_level in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        else InteractionRiskLevel.LOW
    )
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=bounds,
        confidence=0.99,
        risk_level=risk_level,
        purpose=purpose,
        expected_state_hint="jmcomic_script",
        frontier_scope="local",
    )


def _resolve_jinguo_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """禁果腳本步的落點來自 jinguo.py，留證截圖不要求當幀視覺框。"""

    if intent.parameters.get("jinguo_script") is not True:
        return None
    if not candidate_id.startswith("jinguo-script:"):
        return None
    bounds = None
    raw = intent.parameters.get("normalized_bounds")
    if isinstance(raw, Mapping):
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
    if intent.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP} and bounds is None:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "禁果脚本步骤"
    purpose = str(intent.purpose or "").strip() or "按 jinguo.py 执行固定步骤并截图留证"
    risk_level = (
        intent.risk_level
        if intent.risk_level in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        else InteractionRiskLevel.LOW
    )
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=bounds,
        confidence=0.99,
        risk_level=risk_level,
        purpose=purpose,
        expected_state_hint="jinguo_script",
        frontier_scope="local",
    )


def _resolve_mama_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """MaMa腳本步的落點來自 mama.py，留證截圖不要求當幀視覺框。"""

    if intent.parameters.get("mama_script") is not True:
        return None
    if not candidate_id.startswith("mama-script:"):
        return None
    bounds = None
    raw = intent.parameters.get("normalized_bounds")
    if isinstance(raw, Mapping):
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
    if intent.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP} and bounds is None:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "MaMa脚本步骤"
    purpose = str(intent.purpose or "").strip() or "按 mama.py 执行固定步骤并截图留证"
    risk_level = (
        intent.risk_level
        if intent.risk_level in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        else InteractionRiskLevel.LOW
    )
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=bounds,
        confidence=0.99,
        risk_level=risk_level,
        purpose=purpose,
        expected_state_hint="mama_script",
        frontier_scope="local",
    )


def _resolve_pzhan_script_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """P站只整份執行錄製腳本，不從當幀視覺框取點。"""

    if intent.parameters.get("pzhan_script") is not True:
        return None
    if candidate_id != "pzhan-script:run":
        return None
    if intent.action_type is not InteractionOperation.OBSERVE:
        return None
    semantic_name = str(intent.semantic_target or "").strip() or "执行P录制脚本"
    purpose = str(intent.purpose or "").strip() or "启动 P.py，截图交给 Agent 记录与验证"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=intent.action_type,
        normalized_bounds=None,
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="pzhan_script",
        frontier_scope="local",
    )


def _resolve_forced_home_top_category_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """合成 forced-home-top-category:*：优先当前帧视觉文字框，其次课纲槽位。"""

    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    if not candidate_id.startswith("forced-home-top-category:") and not (
        candidate_id.startswith("forced-pixel-home-top-category:")
    ):
        return None
    if intent.parameters.get("home_top_category_tab_seek") is not True:
        return None
    purpose = (
        str(intent.purpose or "").strip()
        or "点击主页上方分类并截图留证；当前截图视觉像素上方分类校准"
    )
    semantic_name = str(intent.semantic_target or "").strip() or "上方分类"
    hint = str(intent.parameters.get("expected_state_hint") or "").strip()
    label = hint.removeprefix("home_top_tab:") if hint.startswith("home_top_tab:") else ""
    # 执行前优先同帧像素校准事实。
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )
    from services.api.app.application.app_visual_home_top_category_calibration import (
        extract_pixel_home_top_category_tabs,
    )

    pixel_tabs = extract_pixel_home_top_category_tabs(analysis)
    needle = label or semantic_name
    pixel_hit = next(
        (
            tab
            for tab in pixel_tabs
            if bottom_tab_label_already_visited(tab.label, {needle})
            or tab.label in needle
            or needle in tab.label
        ),
        None,
    )
    if pixel_hit is not None:
        return AppVisualCandidate.build(
            source_capture_id=str(getattr(analysis, "capture_id", "") or "home-top"),
            source_image_signature=str(
                getattr(analysis, "image_signature", "") or "home-top"
            ),
            source_width=int(getattr(analysis, "width", 0) or 0),
            source_height=int(getattr(analysis, "height", 0) or 0),
            source_orientation=str(
                getattr(analysis, "orientation", "") or "portrait"
            ),
            semantic_name=semantic_name,
            action_type=InteractionOperation.CLICK,
            normalized_bounds=pixel_hit.bounds,
            confidence=0.96,
            risk_level=InteractionRiskLevel.LOW,
            purpose=purpose,
            expected_state_hint=hint or f"home_top_tab:{pixel_hit.label}",
            frontier_scope="local",
            discovery_source=DISCOVERY_SOURCE_VISION,
        )
    # 执行前再扫当前帧 Vision：有文字框则用真实坐标点，避免死槽位偏到搜索栏。
    vision_hit = _match_home_top_category_visual_on_analysis(
        analysis=analysis,
        label=label or semantic_name,
        intent_semantic=semantic_name,
    )
    if vision_hit is not None:
        return vision_hit
    raw = intent.parameters.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return None
    try:
        bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return None
    return AppVisualCandidate.build(
        source_capture_id=str(getattr(analysis, "capture_id", "") or "home-top"),
        source_image_signature=str(
            getattr(analysis, "image_signature", "") or "home-top"
        ),
        source_width=int(getattr(analysis, "width", 0) or 0),
        source_height=int(getattr(analysis, "height", 0) or 0),
        source_orientation=str(getattr(analysis, "orientation", "") or "portrait"),
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.92,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=hint or None,
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _prefer_home_top_sample_rebind_candidate(
    *,
    source_candidate: AppVisualCandidate,
    analysis: AppVisualAnalysis,
    intent: ActionIntent,
) -> AppVisualCandidate | None:
    """第一片取证重绑：优先同帧左上／语义「首卡／视频卡片」，否则 None 交课纲框。"""

    del intent  # bounds 由调用方在无 hit 时用 source 重建
    tokens = (
        "首卡",
        "第一片",
        "视频卡片",
        "視頻卡片",
        "热门推荐视频",
        "熱門推薦視頻",
        "列表左上",
    )
    scored: list[tuple[float, AppVisualCandidate]] = []
    for item in getattr(analysis, "candidates", ()) or ():
        if not isinstance(item, AppVisualCandidate):
            continue
        if item.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        if item.normalized_bounds is None:
            continue
        blob = " ".join(
            str(part or "")
            for part in (
                item.semantic_name,
                item.purpose,
                item.expected_state_hint,
            )
        )
        if any(
            token in blob
            for token in (
                "欧美",
                "歐美",
                "制服",
                "OnlyFans",
                "日本AV",
                "乱伦",
                "亂倫",
            )
        ):
            continue
        try:
            left = float(item.normalized_bounds.left)
            top = float(item.normalized_bounds.top)
            right = float(item.normalized_bounds.right)
            bottom = float(item.normalized_bounds.bottom)
        except (TypeError, ValueError):
            continue
        semantic_hit = any(token in blob for token in tokens)
        # 左上内容卡带（避开上方分类／今日最新滤签与底栏）
        band_hit = (
            0.0 <= left <= 0.55
            and right <= 0.70
            and 0.20 <= top <= 0.62
            and bottom <= 0.82
            and (right - left) >= 0.10
            and (bottom - top) >= 0.08
        )
        if not (semantic_hit or band_hit):
            continue
        # 越靠左上分越高
        score = top * 10.0 + left * 5.0 - (10.0 if semantic_hit else 0.0)
        scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0])
    best = scored[0][1]
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=source_candidate.semantic_name or best.semantic_name,
        action_type=source_candidate.action_type,
        normalized_bounds=best.normalized_bounds,
        confidence=max(float(source_candidate.confidence or 0.0), 0.94),
        risk_level=source_candidate.risk_level or InteractionRiskLevel.LOW,
        purpose=source_candidate.purpose or best.purpose,
        expected_state_hint=source_candidate.expected_state_hint
        or "home_top_sample_first_card",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _resolve_forced_home_top_sample_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """合成上方分类列表取证入口（第一片／3D漫画）。"""

    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return None
    if not (
        candidate_id.startswith("forced-home-top-sample-first-card:")
        or candidate_id.startswith("forced-home-top-sample-match:")
        or candidate_id.startswith("forced-pixel-home-top-sample-first-card:")
        or candidate_id.startswith("forced-pixel-home-top-sample-match:")
        or candidate_id.startswith("forced-pixel-home-content-entry:")
    ):
        return None
    if (
        intent.parameters.get("home_top_category_sample_seek") is not True
        and intent.parameters.get("home_content_entry_seek") is not True
    ):
        return None
    raw = intent.parameters.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return None
    try:
        bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return None
    purpose = (
        str(intent.purpose or "").strip()
        or "点击上方分类列表取证入口并截图留证"
    )
    semantic_name = str(intent.semantic_target or "").strip() or "列表取证入口"
    hint = str(intent.parameters.get("expected_state_hint") or "").strip()
    return AppVisualCandidate.build(
        source_capture_id=str(getattr(analysis, "capture_id", "") or "home-top-sample"),
        source_image_signature=str(
            getattr(analysis, "image_signature", "") or "home-top-sample"
        ),
        source_width=int(getattr(analysis, "width", 0) or 0),
        source_height=int(getattr(analysis, "height", 0) or 0),
        source_orientation=str(getattr(analysis, "orientation", "") or "portrait"),
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.93,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=hint or "home_top_sample_first_card",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _match_home_top_category_visual_on_analysis(
    *,
    analysis: AppVisualAnalysis,
    label: str,
    intent_semantic: str,
) -> AppVisualCandidate | None:
    """在当前视觉分析中按标签命中上方分类真实框。"""

    from services.api.app.application.app_coverage_page_seek import (
        _candidate_matches_home_top_category_label,
        _home_top_category_band,
    )
    from services.api.app.domain.exploration import CandidateAction

    pkg = getattr(analysis, "package_name", None)
    package_name = str(pkg).strip() if isinstance(pkg, str) and str(pkg).strip() else None
    band_top, band_bottom = _home_top_category_band(package_name=package_name)
    needle = str(label or "").strip() or str(intent_semantic or "").strip()
    if not needle:
        return None
    scored: list[tuple[float, AppVisualCandidate]] = []
    raw_candidates = getattr(analysis, "candidates", ()) or ()
    for item in raw_candidates:
        if not isinstance(item, AppVisualCandidate):
            continue
        if item.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        if item.normalized_bounds is None:
            continue
        bounds = item.normalized_bounds
        try:
            cy = (float(bounds.top) + float(bounds.bottom)) / 2.0
            left = float(bounds.left)
            right = float(bounds.right)
        except (TypeError, ValueError):
            continue
        if not (
            0.09 <= cy <= 0.18
            and band_top - 0.02 <= cy <= band_bottom + 0.02
            and float(bounds.top) <= 0.20
            and left < 0.90
        ):
            continue
        probe = CandidateAction(
            candidate_id=item.candidate_id,
            semantic_target=str(item.semantic_name or ""),
            action_type=item.action_type,
            risk_level=item.risk_level or InteractionRiskLevel.LOW,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={
                "purpose": str(item.purpose or ""),
                "expected_state_hint": str(item.expected_state_hint or ""),
            },
        )
        if not _candidate_matches_home_top_category_label(
            probe,
            next_label=needle,
            package_name=package_name,
            visual=item,
        ):
            continue
        width = max(0.001, right - left)
        score = float(item.confidence or 0.0) + (0.18 - abs(cy - 0.135)) * 10.0
        score -= width * 2.0
        scored.append((score, item))
    if not scored:
        return None
    chosen = max(scored, key=lambda pair: pair[0])[1]
    # 重建到当前 analysis 身份，供触控重绑使用。
    assert chosen.normalized_bounds is not None
    return AppVisualCandidate.build(
        source_capture_id=str(getattr(analysis, "capture_id", "") or chosen.source_capture_id),
        source_image_signature=str(
            getattr(analysis, "image_signature", "") or chosen.source_image_signature
        ),
        source_width=int(getattr(analysis, "width", 0) or chosen.source_width or 0),
        source_height=int(getattr(analysis, "height", 0) or chosen.source_height or 0),
        source_orientation=str(
            getattr(analysis, "orientation", "") or chosen.source_orientation or "portrait"
        ),
        semantic_name=str(chosen.semantic_name or intent_semantic or "上方分类"),
        action_type=InteractionOperation.CLICK,
        normalized_bounds=chosen.normalized_bounds,
        confidence=max(float(chosen.confidence or 0.0), 0.93),
        risk_level=InteractionRiskLevel.LOW,
        purpose=str(chosen.purpose or "") or f"视觉点击上方分类「{needle}」",
        expected_state_hint=f"home_top_tab:{needle}",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _resolve_forced_system_back_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """合成 forced-system-back:*：付费／详情无 Vision BACK 时执行系统返回。

    不得要求 candidate_id 落在源视觉观察哈希里，否则会整晚空转 BACK。
    """

    if intent.action_type is not InteractionOperation.BACK:
        return None
    if not candidate_id.startswith("forced-system-back:"):
        return None
    purpose = str(intent.purpose or "").strip() or "系统 BACK 离开当前遮罩后继续探索"
    semantic_name = str(intent.semantic_target or "").strip() or "系统 BACK"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=InteractionOperation.BACK,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="back",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _resolve_short_drama_top_left_back_from_intent(
    *,
    intent: ActionIntent,
    analysis: AppVisualAnalysis,
    candidate_id: str,
) -> AppVisualCandidate | None:
    """F02：拒绝再把 forced-short-drama-top-left-back 重建为 VISION 候选。"""

    _ = (intent, analysis, candidate_id)
    return None


class AppInteractionExecutor:
    """唯一把 App ActionIntent 交给 SurfaceDriver 的应用层适配器。"""

    def __init__(
        self,
        *,
        driver: SurfaceDriver,
        surface_sessions: SurfaceSessionRepository,
        candidate_resolver: AppCandidateResolver,
        runtime_identity_reader: AppRuntimeIdentityReader | None = None,
        allowed_packages: Sequence[str] = (),
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
        command_validator: InteractionCommandValidator | None = None,
    ) -> None:
        self._driver = driver
        self._surface_sessions = surface_sessions
        self._candidate_resolver = candidate_resolver
        self._runtime_identity_reader = runtime_identity_reader
        normalized_packages = tuple(str(item).strip() for item in allowed_packages)
        if any(not item for item in normalized_packages) or len(set(normalized_packages)) != len(
            normalized_packages
        ):
            raise ValueError("allowed_packages 必须为无空值、无重复的包名集合")
        self._allowed_packages = normalized_packages
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )
        self._command_validator = command_validator

    def bind_installed_package(self, package_name: str) -> None:
        """在 APP_READY 后把已验证安装身份锁定为唯一动作边界。"""

        if (
            not isinstance(package_name, str)
            or not package_name.strip()
            or package_name != package_name.strip()
        ):
            raise AppInteractionRuntimeError(
                "APP_READY 缺少已验证安装包名",
                error_code="APP_INSTALLED_PACKAGE_MISSING",
            )
        normalized = package_name
        if self._allowed_packages and self._allowed_packages != (normalized,):
            raise AppInteractionRuntimeError(
                "安装结果与已锁定 App 包边界冲突",
                error_code="APP_INSTALLED_PACKAGE_CONFLICT",
            )
        self._allowed_packages = (normalized,)

    async def execute(self, request: ExecuteInteractionRequest) -> InteractionResult:
        if has_supplemental_evidence_lineage(request.intent.parameters):
            self._supplemental_evidence_policy.require_enabled(
                SupplementalEvidenceOperation.EXECUTE_PROBE
            )
        if request.intent.action_type is InteractionOperation.TIMED_CAPTURE:
            raise AppInteractionRuntimeError(
                "TIMED_CAPTURE 必须通过 TimedObservationIntent 执行",
                error_code="TIMED_OBSERVATION_INTENT_REQUIRED",
            )
        if request.action_execution_id is None:
            raise AppInteractionRuntimeError(
                "App 动作缺少 action_execution_id",
                error_code="ACTION_EXECUTION_REQUIRED",
            )
        # 弹窗未清：底栏／上方分类 seek 不得进 Driver，禁止假 completed。
        if request.intent.parameters.get("bottom_navigation_tab_seek") is True:
            blocked = _bottom_tab_seek_blocked_by_unsatisfied_popup(request)
            if blocked is not None:
                return blocked
        if request.intent.parameters.get("home_top_category_tab_seek") is True:
            blocked = _home_top_tab_seek_blocked_by_unsatisfied_popup(request)
            if blocked is not None:
                return blocked
        try:
            resolved = await self._candidate_resolver.resolve(request)
        except AppInteractionRuntimeError as exc:
            # Candidate Resolver 只做持久化视觉事实与 before Capture 的验签，
            # 此时 Driver 尚未收到动作。可重试的页面漂移应写成“明确未执行”的
            # 失败结果，让 Coordinator 补采 after_action 并从当前页面重建 Frontier；
            # 不能把它升级成设备侧结果未知，提前终止整段动态探索。
            if exc.retryable:
                return _failed_result(
                    request,
                    error_code=exc.error_code,
                    retryable=True,
                    error_message=str(exc),
                )
            raise
        session = await _load_active_session(
            repository=self._surface_sessions,
            task_id=request.intent.task_id,
            session_id=request.intent.session_id,
            surface_kind=request.intent.surface_kind,
        )
        try:
            runtime_package_name = await self._require_runtime_identity(
                request=request,
                resolved=resolved,
                session=session,
            )
        except AppInteractionRuntimeError as exc:
            # 运行时身份检查同样发生在 Driver 之前。可重试拒绝都保留原始
            # error_code，并交给下一张真实 Capture 重新建立候选；非重试合同
            # 错误继续失败关闭，不能伪装成普通页面漂移。
            if exc.retryable:
                return _failed_result(
                    request,
                    error_code=exc.error_code,
                    retryable=True,
                    error_message=str(exc),
                )
            raise
        if self._command_validator is not None and not _external_package_action_allowed(request):
            try:
                await self._command_validator.validate_runtime_location_for(
                    CaptureCoordinationCommand(
                        agent_run_id=request.agent_run_id,
                        intent=request.intent,
                    ),
                    package_name=runtime_package_name or resolved.before_capture.package_name,
                    expected_finalization_epoch=request.finalization_epoch,
                )
            except InteractionCommandValidationError as exc:
                return _failed_result(
                    request,
                    error_code=exc.error_code,
                    retryable=exc.retryable,
                    error_message=str(exc),
                    state_summary={
                        "failure_stage": "runtime_location_policy",
                        "driver_invoked": False,
                    },
                )
        parameters = dict(request.intent.parameters)
        # Work 身份只用于 PostgreSQL 恢复，不属于设备 Driver 参数合同。
        parameters.pop("exploration_work_id", None)
        if resolved.resolved_target is not None:
            # Driver 的 point 优先级高于 resolved_target；清除所有旧坐标，
            # 防止动作参数中的历史点覆盖当前 before Capture 的重定位结果。
            for key in ("point", "x", "y", "coordinate_space", "space"):
                parameters.pop(key, None)
            parameters["resolved_target"] = resolved.resolved_target
        driver_intent = replace(
            request.intent,
            source_capture_id=request.source_capture_id,
            parameters=parameters,
        )
        if self._command_validator is not None:
            try:
                await self._command_validator.validate_resolved_target_for(
                    CaptureCoordinationCommand(
                        agent_run_id=request.agent_run_id,
                        intent=driver_intent,
                    ),
                    # 平台验签的整页采样不触发页面上的支付等控件；页型和
                    # 采样说明是观察上下文，不能当作当前控件的副作用。
                    trusted_target_texts=(resolved.candidate.semantic_name,)
                    if getattr(resolved, "is_dynamic_page_sampling", False)
                    else (
                        resolved.candidate.semantic_name,
                        resolved.candidate.purpose,
                        resolved.candidate.expected_state_hint,
                    ),
                    target_trust="app_visual_candidate",
                    target_role=resolved.candidate.control_role,
                    expected_finalization_epoch=request.finalization_epoch,
                )
            except InteractionCommandValidationError as exc:
                # 目标风险在视觉候选绑定后才完整可见；此时仍未调用 Driver，
                # 返回结构化失败并保留“未执行”事实，不能把它当设备未知结果。
                return _failed_result(
                    request,
                    error_code=exc.error_code,
                    retryable=exc.retryable,
                    error_message=str(exc),
                    state_summary={
                        "failure_stage": "resolved_target_policy",
                        "driver_invoked": False,
                    },
                )
        try:
            execution_context = {
                **_execution_context_from_request(request),
                # 下游以 ID 回读同一 Capture 的平台候选，不接受请求自报角色。
                "resolved_candidate_id": resolved.candidate.candidate_id,
            }
            execute_with_context = getattr(self._driver, "execute_with_context", None)
            from services.api.app.application.app_command_broker import (
                execute_app_driver_action_via_broker,
            )
            from services.api.app.application.app_lab_composition import (
                resolve_effective_app_milestone_policy,
            )

            raw, broker_receipt = await execute_app_driver_action_via_broker(
                driver=self._driver,
                session=session,
                intent=driver_intent,
                action_execution_id=request.action_execution_id,
                execution_context=(
                    execution_context
                    if _requires_context_capable_driver(request) or callable(execute_with_context)
                    else None
                ),
                milestone_policy=resolve_effective_app_milestone_policy(),
                package_allowlist=self._allowed_packages,
                capture_epoch=str(
                    getattr(resolved.before_capture, "capture_id", "") or ""
                )
                or None,
                foreground_package=getattr(
                    resolved.before_capture, "package_name", None
                ),
                agent_run_id=request.agent_run_id,
                source_capture_id=request.source_capture_id,
                command_validator=self._command_validator,
            )
        except SurfaceDriverError as exc:
            # 仅保留驱动合同声明的错误码与重试属性；不保存异常正文或堆栈，
            # 也不采信任意异常自报的同名属性。
            return _failed_result(
                request,
                error_code=exc.error_code,
                retryable=exc.retryable,
            )
        except Exception as exc:  # noqa: BLE001 - Driver 细节不得进入跨模块结果
            # 诊断旁路：记录被脱敏吞掉的异常类型，便于对照 Official 403 业务码。
            try:
                from pathlib import Path

                Path("/workspace/.tmp_app_driver_failed.log").open(
                    "a", encoding="utf-8"
                ).write(
                    f"task_id={request.intent.task_id} "
                    f"action_execution_id={request.action_execution_id} "
                    f"exc_type={type(exc).__name__} "
                    f"exc_error_code={getattr(exc, 'error_code', None)}\n"
                )
            except Exception:  # noqa: BLE001
                pass
            return _failed_result(
                request,
                error_code="APP_DRIVER_FAILED",
                retryable=True,
            )
        if not raw.succeeded:
            return _failed_result(
                request,
                error_code=raw.error_code or "APP_ACTION_FAILED",
                retryable=raw.retryable,
                broker_receipt=broker_receipt,
            )
        # 产品编排动作采用脚本已验证的节奏：真实触控后先等待
        # 页面稳定，再确认前台仍在目标包。这个 Hook 不参与选点。
        recovery = None
        hlapp_recovery = None
        if request.intent.parameters.get("hjll_orchestrated") is True:
            settle = getattr(self._driver, "settle_hjll_orchestrated_action", None)
            if callable(settle):
                try:
                    recovery = await settle(
                        expected_package=str(
                            request.intent.parameters.get("hjll_expected_package") or ""
                        ).strip(),
                        settle_ms=request.intent.parameters.get(
                            "hjll_settle_after_action_ms"
                        ),
                    )
                except SurfaceDriverError as exc:
                    return _failed_result(
                        request,
                        error_code=exc.error_code,
                        retryable=exc.retryable,
                        broker_receipt=broker_receipt,
                    )
            else:
                recovery = {"supported": False}
        elif request.intent.parameters.get("hlapp_orchestrated") is True:
            settle = getattr(self._driver, "settle_hjll_orchestrated_action", None)
            if callable(settle):
                try:
                    hlapp_recovery = await settle(
                        expected_package=str(
                            request.intent.parameters.get("hlapp_expected_package") or ""
                        ).strip(),
                        settle_ms=request.intent.parameters.get(
                            "hlapp_settle_after_action_ms"
                        ),
                    )
                except SurfaceDriverError as exc:
                    return _failed_result(
                        request,
                        error_code=exc.error_code,
                        retryable=exc.retryable,
                        broker_receipt=broker_receipt,
                    )
            else:
                hlapp_recovery = {"supported": False}
        elif request.intent.parameters.get("hlwapp_orchestrated") is True:
            settle = getattr(self._driver, "settle_hjll_orchestrated_action", None)
            if callable(settle):
                try:
                    hlapp_recovery = await settle(
                        expected_package=str(
                            request.intent.parameters.get("hlwapp_expected_package") or ""
                        ).strip(),
                        settle_ms=request.intent.parameters.get(
                            "hlwapp_settle_after_action_ms"
                        ),
                        recover_foreground=request.intent.parameters.get(
                            "hlwapp_browser_hold"
                        )
                        is not True,
                    )
                except SurfaceDriverError as exc:
                    return _failed_result(
                        request,
                        error_code=exc.error_code,
                        retryable=exc.retryable,
                        broker_receipt=broker_receipt,
                    )
            else:
                hlapp_recovery = {"supported": False}
        elif request.intent.parameters.get("product_script_orchestrated") is True:
            settle = getattr(self._driver, "settle_hjll_orchestrated_action", None)
            if callable(settle):
                try:
                    hlapp_recovery = await settle(
                        expected_package=str(
                            request.intent.parameters.get("product_expected_package") or ""
                        ).strip(),
                        settle_ms=request.intent.parameters.get(
                            "product_settle_after_action_ms"
                        ),
                    )
                except SurfaceDriverError as exc:
                    return _failed_result(
                        request,
                        error_code=exc.error_code,
                        retryable=exc.retryable,
                        broker_receipt=broker_receipt,
                    )
            else:
                hlapp_recovery = {"supported": False}
        return InteractionResult(
            task_id=request.intent.task_id,
            session_id=request.intent.session_id,
            intent_id=request.intent.intent_id,
            action_execution_id=request.action_execution_id,
            source_capture_id=request.source_capture_id,
            status=InteractionResultStatus.EVIDENCE_PENDING,
            state_summary={
                "surface_kind": request.intent.surface_kind.value,
                "app_command_broker_receipt": broker_receipt,
                "hjll_orchestration": recovery,
                "hlapp_orchestration": hlapp_recovery,
                "pc51_recording": (
                    dict(raw.payload.get("pc51_recording"))
                    if isinstance(raw.payload.get("pc51_recording"), Mapping)
                    else None
                ),
            },
        )

    async def _require_runtime_identity(
        self,
        *,
        request: ExecuteInteractionRequest,
        resolved: ResolvedAppCandidate,
        session: SurfaceSessionRef,
    ) -> str | None:
        allowed = self._allowed_packages or tuple(
            item
            for item in (resolved.before_capture.package_name,)
            if isinstance(item, str) and item
        )
        before_package = resolved.before_capture.package_name
        if before_package not in allowed and not _external_package_action_allowed(request):
            raise AppInteractionRuntimeError(
                "动作前 Capture 已离开目标 App 包边界",
                error_code="APP_PACKAGE_BOUNDARY_VIOLATION",
                retryable=True,
            )
        if self._runtime_identity_reader is None:
            return before_package
        try:
            identity = await self._runtime_identity_reader.get_runtime_identity(session=session)
        except Exception as exc:
            raise AppInteractionRuntimeError(
                "无法读取动作执行前的 App 运行时身份",
                error_code="APP_RUNTIME_IDENTITY_UNAVAILABLE",
                retryable=True,
            ) from exc
        if identity is None or identity.activity is None:
            raise AppInteractionRuntimeError(
                "动作执行前的 App 运行时身份不完整",
                error_code="APP_RUNTIME_IDENTITY_UNAVAILABLE",
                retryable=True,
            )
        external_action_allowed = _external_package_action_allowed(request)
        if identity.package_name not in allowed:
            if external_action_allowed:
                if request.intent.action_type is InteractionOperation.BACK:
                    return identity.package_name
                if (
                    identity.package_name == before_package
                    and identity.activity == resolved.before_capture.activity
                ):
                    return identity.package_name
                raise AppInteractionRuntimeError(
                    "系统弹窗动作前设备页面已偏离已持久化 Capture",
                    error_code="STALE_APP_RUNTIME_IDENTITY",
                    retryable=True,
                )
            raise AppInteractionRuntimeError(
                "设备当前前台包已超出目标 App 包边界",
                error_code="APP_PACKAGE_BOUNDARY_VIOLATION",
                retryable=True,
            )
        if (
            identity.package_name != before_package
            or identity.activity != resolved.before_capture.activity
        ):
            raise AppInteractionRuntimeError(
                "动作前设备页面已偏离已持久化 Capture",
                error_code="STALE_APP_RUNTIME_IDENTITY",
                retryable=True,
            )
        return identity.package_name


def _execution_context_from_request(request: ExecuteInteractionRequest) -> Mapping[str, object]:
    """构造仅供下游 Driver 校验的外层命令上下文。"""

    context: dict[str, object] = {
        "task_id": request.intent.task_id,
        "session_id": request.intent.session_id,
        "surface_kind": request.intent.surface_kind.value,
        "action_execution_id": request.action_execution_id,
        "source_capture_id": request.source_capture_id,
        "finalization_epoch": request.finalization_epoch,
        "command_sequence": request.command_sequence,
        "lease_owner": request.lease_owner,
        "lease_token": request.lease_token,
    }
    return context


def _requires_context_capable_driver(request: ExecuteInteractionRequest) -> bool:
    """判断请求是否已经进入 Coordinator 的正式序列/租约执行模式。"""

    # 只有 finalization_epoch 的历史 Web 请求仍保留裸 Driver 兼容形状；
    # command_sequence 或 lease_owner 一旦出现，就必须把正式上下文传到底层。
    return request.command_sequence is not None or request.lease_owner is not None


class AppCaptureWriter:
    """在当前 App Session 上采集并持久化动作前后证据。"""

    def __init__(
        self,
        *,
        driver: SurfaceDriver,
        surface_sessions: SurfaceSessionRepository,
        persistence: AppCapturePersistenceService,
        flow_foundation_runtime: object | None = None,
    ) -> None:
        self._driver = driver
        self._surface_sessions = surface_sessions
        self._persistence = persistence
        self._flow_foundation_runtime = flow_foundation_runtime

    async def capture(self, request: CaptureWriteRequest) -> CaptureRef:
        if request.surface_kind is SurfaceKind.WEB:
            raise ValueError("AppCaptureWriter 不接受 Web Surface")
        if (
            request.session_command_sequence is None
            or request.finalization_epoch is None
            or request.parent_capture_id is None
            or request.lease_owner is None
            or request.lease_token is None
        ):
            raise ValueError("App Capture 缺少父 Capture、命令围栏或 Session Lease")
        is_observation = request.phase is CapturePhase.OBSERVATION
        if is_observation:
            if request.directive.role != "observation":
                raise ValueError("App Observation Capture role 必须为 observation")
            if request.action_execution_id is not None:
                raise ValueError("App Observation Capture 不得绑定 Action Execution")
        elif (
            request.directive.role not in {"before_action", "after_action"}
            or request.action_execution_id is None
        ):
            raise ValueError("App Action Capture 缺少动作身份或 role 不受支持")
        remaining = (request.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("App Capture deadline 已到")
        session = await _load_active_session(
            repository=self._surface_sessions,
            task_id=request.task_id,
            session_id=request.session_id,
            surface_kind=request.surface_kind,
        )
        timed = "temporal_trace" in request.directive.artifact_types
        phase_key = ""
        if isinstance(request.observation_context, Mapping):
            phase_key = str(request.observation_context.get("phase_key") or "")
        profile_token = request.phase.value
        if timed and any(
            token in phase_key.casefold() for token in ("startup", "splash", "countdown")
        ):
            profile_token = f"{request.phase.value}-startup-timed_capture"
        elif timed and "popup" in phase_key.casefold():
            profile_token = f"{request.phase.value}-popup-timed_capture"
        elif timed:
            profile_token = f"{request.phase.value}-timed_capture"
        profile = CaptureProfile(
            profile_id=(
                f"app-{profile_token}-v1:"
                f"{request.action_execution_id or 'observation'}"
            ),
            before=request.directive,
            after=request.directive,
        )
        # Capture 侧诊断微手势（如 player wake）需透传 Coordinator 租约上下文，
        # 否则星语上游会以 EXECUTION_CONTEXT_REQUIRED 拒绝轻触。
        from services.api.app.application.app_capture_lease_context import (
            bind_capture_lease_context,
        )

        lease_context = {
            "task_id": request.task_id,
            "session_id": request.session_id,
            "surface_kind": request.surface_kind.value,
            "action_execution_id": request.action_execution_id
            or f"capture:{request.phase.value}:{request.intent_id}",
            "source_capture_id": request.parent_capture_id,
            "finalization_epoch": request.finalization_epoch,
            "command_sequence": request.session_command_sequence,
            "lease_owner": request.lease_owner,
            "lease_token": request.lease_token,
        }
        with bind_capture_lease_context(lease_context):
            raw = await asyncio.wait_for(
                self._driver.capture(session=session, profile=profile),
                timeout=remaining,
            )
        if raw.surface_ref != session.primary_surface_ref:
            raise AppInteractionRuntimeError(
                "Driver Capture 返回的 Surface 身份不一致",
                error_code="APP_CAPTURE_SURFACE_MISMATCH",
            )
        selection = _select_capture_artifacts(
            requested=request.directive.artifact_types,
            produced=tuple(item.artifact_type for item in raw.artifacts),
            role="observation" if is_observation else request.directive.role,
        )
        # 将 soft-optional 缺失/Provider-only 状态随截图 metadata 写入正式
        # Persistence；它们不参与 required equality，也不会伪造主证据失败。
        from services.api.app.application.app_capture_evidence_fields import (
            enrich_capture_evidence_fields,
        )
        from services.api.app.application.app_capture_role_supplement import (
            normalize_requirement_role,
        )
        from services.api.app.application.app_p2_timing_capabilities import (
            APP_TOAST_OBSERVATION_KEY,
            build_toast_observation,
        )

        requirement_role = None
        if is_observation:
            context_preview = dict(request.observation_context)
            requirement_role = normalize_requirement_role(
                context_preview.get("semantic_role") or "refreshed"
            )
        else:
            requirement_role = normalize_requirement_role(request.directive.role)
        # attempt_N：同一动作重试序号写入观察上下文。
        attempt_no = None
        if isinstance(request.observation_context, Mapping):
            raw_attempt = request.observation_context.get("attempt_no")
            if isinstance(raw_attempt, int) and raw_attempt >= 1:
                attempt_no = min(raw_attempt, 3)
        if attempt_no is not None:
            requirement_role = f"attempt_{attempt_no}"
        # location_1：价格／第二区位补证标记。
        if (
            isinstance(request.observation_context, Mapping)
            and request.observation_context.get("price_location") == 1
        ):
            requirement_role = "location_1"

        merged_meta = enrich_capture_evidence_fields(
            {
                **dict(raw.metadata),
                "capture_artifact_contract": selection.to_metadata(),
            },
            requirement_role=requirement_role,
        )
        if request.session_id and "network_trace" not in merged_meta:
            from services.api.app.application.app_network_proxy import (
                get_app_network_proxy_hub,
            )
            from services.api.app.application.app_surface_evidence import (
                bound_network_trace_payload,
                project_har_from_network_trace,
            )

            entries = get_app_network_proxy_hub().peek(request.session_id)
            if entries:
                trace = bound_network_trace_payload(
                    entries=entries,
                    package_name=merged_meta.get("package_name")
                    if isinstance(merged_meta.get("package_name"), str)
                    else None,
                    proxy_attached=True,
                    status="captured",
                    reason="session_proxy",
                )
                merged_meta["network_trace"] = trace
                merged_meta["har"] = project_har_from_network_trace(trace)
        # timed_capture 帧扫描 Toast（P2-10）
        timed_meta = merged_meta.get("timed_capture")
        if isinstance(timed_meta, Mapping):
            frames = timed_meta.get("frames") or timed_meta.get("samples")
            toast = build_toast_observation(
                frames=frames if isinstance(frames, (list, tuple)) else None
            )
            if toast is not None:
                merged_meta[APP_TOAST_OBSERVATION_KEY] = toast
        raw = replace(
            raw,
            artifacts=tuple(
                item
                for item in raw.artifacts
                if item.artifact_type in selection.persisted_artifact_types
            ),
            metadata=merged_meta,
        )
        common = {
            "task_id": request.task_id,
            "agent_run_id": request.agent_run_id,
            "session_id": request.session_id,
            "surface_kind": request.surface_kind,
            "artifacts": raw,
            "required_artifact_types": selection.persisted_artifact_types,
            "parent_capture_id": request.parent_capture_id,
            "session_command_sequence": request.session_command_sequence,
            "finalization_epoch": request.finalization_epoch,
            "lease_owner": request.lease_owner,
            "lease_token": request.lease_token,
        }
        if is_observation:
            context = dict(request.observation_context)
            persisted = await self._persistence.persist_observation_capture(
                **common,
                observation_id=context.get("observation_id", request.intent_id),
                surface_phase_run_id=context.get("surface_phase_run_id"),
                surface_phase_decision_id=context.get("surface_phase_decision_id"),
                semantic_role=context.get("semantic_role", "surface_reobserve"),
            )
        else:
            persisted = await self._persistence.persist_action_capture(
                **common,
                role=request.directive.role,
                action_execution_id=request.action_execution_id,
            )
        await self._record_flow_foundation_after_persist(
            request=request,
            persisted=persisted,
            raw=raw,
            is_observation=is_observation,
        )
        return persisted.capture

    async def _record_flow_foundation_after_persist(
        self,
        *,
        request: CaptureWriteRequest,
        persisted: object,
        raw: object,
        is_observation: bool,
    ) -> None:
        """正式路径：落库成功后登记预约／承诺并分配观察事件序。"""

        runtime = self._flow_foundation_runtime
        if runtime is None:
            return
        record = getattr(runtime, "record_capture_reservation_and_commitment", None)
        allocate = getattr(runtime, "allocate_observation_event", None)
        if not callable(record) or not callable(allocate):
            return
        context = dict(request.observation_context or {})
        phase_run_id = str(
            context.get("surface_phase_run_id")
            or context.get("phase_run_id")
            or request.agent_run_id
        )
        checkpoint_key = str(
            context.get("checkpoint_key")
            or context.get("surface_phase_key")
            or request.phase.value
        )
        # 缺省用 command_sequence，避免连续动作 before_capture 共用 sample_index=0
        # → capture_request_id 冲突（QC b26 第 2 次关层 before_capture_failed）。
        sample_index_raw = (
            context.get("sample_index")
            or context.get("attempt_no")
            or request.session_command_sequence
            or "0"
        )
        try:
            sample_index = int(sample_index_raw)
        except (TypeError, ValueError):
            sample_index = 0
        producer_epoch = str(
            context.get("producer_epoch")
            or f"session:{request.session_id}:epoch:{request.finalization_epoch}"
        )
        fencing_token = str(
            context.get("fencing_token")
            or getattr(request, "lease_token", None)
            or "0"
        )
        content_digest = str(getattr(persisted, "image_signature", "") or "")
        content_length = 0
        artifacts = getattr(raw, "artifacts", ()) or ()
        for item in artifacts:
            if getattr(item, "artifact_type", None) == "screenshot":
                content = getattr(item, "content", b"")
                if isinstance(content, (bytes, bytearray)):
                    content_length = len(content)
                    if not content_digest:
                        import hashlib

                        content_digest = f"sha256:{hashlib.sha256(bytes(content)).hexdigest()}"
                break
        if not content_digest:
            content_digest = f"capture:{persisted.capture.capture_id}"
        capture_id = persisted.capture.capture_id
        await record(
            task_id=request.task_id,
            phase_run_id=phase_run_id,
            checkpoint_key=checkpoint_key,
            sample_index=sample_index,
            producer_epoch=producer_epoch,
            capture_id=capture_id,
            content_digest=content_digest,
            content_length=content_length,
            fencing_token=fencing_token,
            session_id=request.session_id,
            action_execution_id=request.action_execution_id,
            execution_segment_id=context.get("execution_segment_id") or request.agent_run_id,
            artifact_slot="screenshot",
            pure_observation=is_observation,
        )
        await allocate(
            task_id=request.task_id,
            phase_run_id=phase_run_id,
            resource_id=str(
                context.get("resource_id") or f"session:{request.session_id}"
            ),
            producer_epoch=producer_epoch,
            sample_index=sample_index,
            capture_id=capture_id,
            content_digest=content_digest,
        )


def _align_required_artifacts(
    *,
    requested: tuple[str, ...],
    produced: tuple[str, ...],
    role: str = "before_action",
) -> tuple[str, ...]:
    """按正式合同对齐 requested/produced，保留旧调用方的 tuple 返回形状。"""

    return _select_capture_artifacts(
        requested=requested,
        produced=produced,
        role=role,
    ).persisted_artifact_types


def _select_capture_artifacts(
    *,
    requested: tuple[str, ...],
    produced: tuple[str, ...],
    role: str,
):
    """统一裁决 required、soft-optional 和可落库制品。"""

    try:
        return APP_CAPTURE_ARTIFACT_CONTRACT.select(
            role=role,
            requested=requested,
            produced=produced,
        )
    except ValueError as exc:
        raise ValueError("App Capture 产物与 Capture Profile 不一致：" + str(exc)) from exc


def _external_package_action_allowed(request: ExecuteInteractionRequest) -> bool:
    """外部包仅允许返回目标 App，或由系统弹窗阶段机批准的单次动作。"""

    intent = request.intent
    if intent.action_type in {
        InteractionOperation.BACK,
        InteractionOperation.WARM_RESTORE,
    }:
        return True
    if (
        intent.parameters.get("hlwapp_browser_hold") is True
        and intent.action_type is InteractionOperation.OBSERVE
    ):
        return True
    parameters = dict(intent.parameters)
    return (
        intent.action_type is InteractionOperation.CLICK
        and parameters.get("surface_phase_key") == "system_dialog"
        and isinstance(parameters.get("surface_phase_run_id"), str)
        and isinstance(parameters.get("surface_phase_decision_id"), str)
    )


async def _load_active_session(
    *,
    repository: SurfaceSessionRepository,
    task_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
) -> SurfaceSessionRef:
    snapshot = await repository.get(session_id=session_id)
    if (
        snapshot is None
        or snapshot.task_id != task_id
        or snapshot.surface_kind is not surface_kind
        or snapshot.status is not SurfaceSessionStatus.ACTIVE
        or snapshot.current_surface_ref is None
    ):
        raise AppInteractionRuntimeError(
            "App Surface Session 不可执行",
            error_code="APP_SESSION_NOT_ACTIVE",
            retryable=True,
        )
    return SurfaceSessionRef(
        task_id=snapshot.task_id,
        session_id=snapshot.session_id,
        surface_kind=snapshot.surface_kind,
        primary_surface_ref=snapshot.current_surface_ref,
    )


def _resolve_pixel_target(
    candidate: AppVisualCandidate,
    capture: PersistedAppCapture,
) -> dict[str, int | str] | None:
    if candidate.action_type not in _COORDINATE_TOUCH_ACTIONS:
        return None
    if candidate.normalized_bounds is None:
        raise AppInteractionRuntimeError(
            "Click/Tap 候选缺少归一化区域",
            error_code="VISUAL_TARGET_MISSING",
        )
    center_x, center_y = candidate.normalized_bounds.center
    # 非受信 a11y 不得点进系统手势带（云手机易回桌面）。
    if _candidate_in_system_gesture_zone(candidate) and not _candidate_from_trusted_a11y(
        candidate
    ):
        raise AppInteractionRuntimeError(
            "视觉落点位于系统手势带，禁止点击；请等待 a11y 底栏或新 Capture 重绑",
            error_code=ANDROID_POINT_IN_GESTURE_ZONE,
            retryable=True,
        )
    # 截圖座標系像素；不得標記為 device_px——雲手機 API 預覽圖常小於 wm size。
    capture_x = min(capture.width - 1, max(0, round(center_x * capture.width)))
    capture_y = min(capture.height - 1, max(0, round(center_y * capture.height)))
    bounds = candidate.normalized_bounds
    log_app_tap_audit(
        "capture_target",
        candidate_id=getattr(candidate, "candidate_id", None),
        semantic_name=getattr(candidate, "semantic_name", None),
        control_role=getattr(candidate, "control_role", None),
        discovery_source=getattr(candidate, "discovery_source", None),
        norm_left=bounds.left,
        norm_top=bounds.top,
        norm_right=bounds.right,
        norm_bottom=bounds.bottom,
        norm_center_x=center_x,
        norm_center_y=center_y,
        capture_x=capture_x,
        capture_y=capture_y,
        capture_width=capture.width,
        capture_height=capture.height,
        capture_id=getattr(capture, "capture_id", None),
        package_name=getattr(capture, "package_name", None),
        space="capture_px",
    )
    return {
        "x": capture_x,
        "y": capture_y,
        "space": "capture_px",
        "base_width": capture.width,
        "base_height": capture.height,
    }


def _bottom_tab_seek_blocked_by_unsatisfied_popup(
    request: ExecuteInteractionRequest,
) -> InteractionResult | None:
    """弹窗流程未满足时拒绝底栏执行（driver 未调用，不得写成 completed）。"""

    from services.api.app.application.app_exploration_flow_contract import (
        APP_FLOW_CONTRACT_KEY,
        popup_phase_blocks_bottom_tabs,
    )

    params = request.intent.parameters if isinstance(request.intent.parameters, Mapping) else {}
    meta: dict[str, object] = {}
    raw_meta = params.get("coverage_metadata")
    if isinstance(raw_meta, Mapping):
        meta.update(dict(raw_meta))
    flow = params.get(APP_FLOW_CONTRACT_KEY) or params.get("app_flow_contract")
    if isinstance(flow, Mapping):
        meta[APP_FLOW_CONTRACT_KEY] = dict(flow)
    # 无契约字段时不在此拦（交由 Explorer）；有 popup_seen 未满足则硬拒绝。
    if not popup_phase_blocks_bottom_tabs(meta):
        return None
    return _failed_result(
        request,
        error_code="APP_POPUP_PHASE_BLOCKS_BOTTOM_TABS",
        retryable=True,
        error_message=(
            "弹窗流程未满足（popup_satisfied=false），禁止执行底栏；"
            "不得记为 completed／visited"
        ),
        state_summary={
            "failure_stage": "popup_phase_gate",
            "driver_invoked": False,
            "bottom_navigation_tab_seek": True,
        },
    )


def _home_top_tab_seek_blocked_by_unsatisfied_popup(
    request: ExecuteInteractionRequest,
) -> InteractionResult | None:
    """弹窗未清／当前帧仍有遮罩时拒绝上方分类（真实栏目尚未露出）。"""

    from services.api.app.application.app_exploration_flow_contract import (
        APP_FLOW_CONTRACT_KEY,
        analysis_blocks_home_top_category_tabs,
        popup_phase_blocks_home_top_category_tabs,
    )

    params = request.intent.parameters if isinstance(request.intent.parameters, Mapping) else {}
    meta: dict[str, object] = {}
    raw_meta = params.get("coverage_metadata")
    if isinstance(raw_meta, Mapping):
        meta.update(dict(raw_meta))
    flow = params.get(APP_FLOW_CONTRACT_KEY) or params.get("app_flow_contract")
    if isinstance(flow, Mapping):
        meta[APP_FLOW_CONTRACT_KEY] = dict(flow)
    analysis = params.get("visual_analysis") or params.get("analysis")
    blocked_by_frame = analysis_blocks_home_top_category_tabs(analysis)
    # Explorer 已签发上方分类：只认当前帧真挡路。滞后 popup_satisfied=false
    # （国庆贴／侧浮层）不得空拒「最新」。
    if not blocked_by_frame:
        return None
    blocked_by_flow = popup_phase_blocks_home_top_category_tabs(meta, analysis=analysis)
    return _failed_result(
        request,
        error_code="APP_POPUP_PHASE_BLOCKS_HOME_TOP_TABS",
        retryable=True,
        error_message=(
            "弹窗／遮罩未清完，主页上方栏目尚未露出，禁止执行上方分类；"
            "不得记为 completed／visited"
        ),
        state_summary={
            "failure_stage": "popup_phase_gate",
            "driver_invoked": False,
            "home_top_category_tab_seek": True,
            "blocked_by_flow": blocked_by_flow,
            "blocked_by_frame": blocked_by_frame,
        },
    )


def _failed_result(
    request: ExecuteInteractionRequest,
    *,
    error_code: str,
    retryable: bool,
    error_message: str = "App Driver 执行失败",
    state_summary: Mapping[str, object] | None = None,
    broker_receipt: Mapping[str, object] | None = None,
) -> InteractionResult:
    summary = dict(state_summary or {})
    if broker_receipt is not None:
        summary["app_command_broker_receipt"] = dict(broker_receipt)
    return InteractionResult(
        task_id=request.intent.task_id,
        session_id=request.intent.session_id,
        intent_id=request.intent.intent_id,
        action_execution_id=request.action_execution_id or "unplanned",
        source_capture_id=request.source_capture_id,
        status=InteractionResultStatus.FAILED,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
        state_summary=summary,
    )
