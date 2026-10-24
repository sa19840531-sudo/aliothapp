"""H5 页面判定结果到通用 V2 Observation/Frontier 的纯适配。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from services.api.app.application.h5_age_verification import (
    H5_AGE_VERIFICATION_CAPABILITY,
    H5_AGE_VERIFICATION_KEYWORDS,
    H5_AGE_VERIFICATION_PAGE_STATE,
    H5_AGE_VERIFICATION_PURPOSE,
    H5_AGE_VERIFICATION_TARGET,
    H5_AGE_VERIFICATION_TARGET_MODE,
)
from services.api.app.application.h5_line_loading_recovery import (
    H5_LINE_LOADING_LOGIN_RETRY_CAPABILITY,
    H5_LINE_LOADING_LOGIN_RETRY_KEYWORDS,
    H5_LINE_LOADING_LOGIN_RETRY_PURPOSE,
    H5_LINE_LOADING_LOGIN_RETRY_TARGET,
    H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE,
)
from services.api.app.application.h5_list_detail_navigation import (
    H5_LIST_DETAIL_TARGET,
    h5_list_detail_expected_page_type,
    h5_list_detail_selector_allowed,
)
from services.api.app.application.h5_page_type_assessment_adapter import (
    h5_authentication_required,
    h5_trusted_obstruction_dismissal_available,
)
from services.api.app.application.h5_payment_horizontal_collection import (
    H5_PAYMENT_HORIZONTAL_TARGET,
    h5_payment_horizontal_action_parameters,
    h5_payment_horizontal_context_supported,
    h5_payment_horizontal_keywords,
    h5_payment_horizontal_purpose,
)
from services.api.app.application.h5_profile_activity_navigation import (
    H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE,
    H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE,
    H5_PROFILE_ACTIVITY_TARGET_MODE,
    h5_profile_activity_capability_for_target,
)
from services.api.app.application.h5_profile_payment_navigation import (
    H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE,
    H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE,
    H5_PROFILE_PAYMENT_TARGET,
    H5_PROFILE_PAYMENT_TARGET_MODE,
    h5_profile_payment_capability_from_keywords,
    h5_profile_payment_entry_label,
)
from services.api.app.application.h5_profile_settings_navigation import (
    H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY,
    H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE,
    H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE,
    H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE,
    H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE,
    H5_PROFILE_SETTINGS_TARGET_MODE,
    H5_SETTINGS_PAGE_BACK_CAPABILITY,
    H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE,
    H5_SETTINGS_PAGE_BACK_TARGET_MODE,
    h5_profile_favorites_page_back_source_type_allowed,
    h5_profile_settings_capability_for_target,
)
from services.api.app.application.h5_profile_visual_navigation import (
    h5_profile_visual_entry_contract,
)
from services.api.app.application.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
    h5_bottom_navigation_action_parameters,
    h5_match_bottom_navigation_target,
    h5_page_tab_target,
)
from services.api.app.domain.exploration import (
    CandidateAction,
    FrontierSnapshot,
    SurfaceObservation,
)
from services.api.app.domain.exploration.h5_frontier import canonical_h5_route_identity
from services.api.app.domain.exploration.h5_logical_surface import (
    LOGICAL_SURFACE_PROTOCOL_VERSION,
    h5_obstruction_facts_confirm_clearance,
    logical_surface_is_effective,
)
from services.api.app.domain.exploration.h5_page_state import (
    h5_page_state_has_overlay,
    h5_page_state_supports_popup_dismissal,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    VISUAL_TARGET_RISK_LEVELS,
    verify_visual_navigation_capability,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    validate_semantic_target,
)
from services.api.app.domain.protocol import require_aware_datetime, stable_protocol_hash

_RISK_LEVELS = {
    "low": InteractionRiskLevel.LOW,
    "medium": InteractionRiskLevel.MEDIUM,
    "high": InteractionRiskLevel.HIGH,
    "critical": InteractionRiskLevel.HIGH,
}
_LINE_LOADING_REOBSERVE_MAX = 3
_LINE_LOADING_REOBSERVE_TARGET = "重新观察线路加载结果"
_STARTUP_AD_COUNTDOWN_REOBSERVE_MAX = 10
_STARTUP_AD_COUNTDOWN_REOBSERVE_TARGET = "重新观察启动广告倒计时"
_STARTUP_AD_DISMISSAL_MAX = 10
# 开屏角标清障控件的文案：杏吧（Flutter）写「进入」而不是「跳过」，两者是同一个
# 「离开开屏进正片」的控件。与 infrastructure/browser 侧的 FLUTTER_STARTUP_DISMISS_TEXTS
# 同源，此处按分层单独声明（application 不依赖 infrastructure）。
_STARTUP_AD_DISMISSAL_TARGETS = frozenset({"跳过", "进入"})
_GUIDE_PAGE_TYPE = "guide_page"
_GUIDE_PAGE_DETERMINISTIC_FLAG = "deterministic_guide_page"
_FLUTTER_BOOTSTRAP_REOBSERVE_MAX = 5
_FLUTTER_BOOTSTRAP_REOBSERVE_TARGET = "重新观察 Flutter 启动页"
_FLUTTER_BOOTSTRAP_PENDING_FLAG = "deterministic_flutter_bootstrap_pending"
_INSUFFICIENT_REOBSERVE_MAX = 3
_INSUFFICIENT_REOBSERVE_TARGET = "重新观察未稳定页面"
_AMBIGUOUS_LAUNCH_SHELL_REOBSERVE_MAX = 3
_AMBIGUOUS_LAUNCH_SHELL_REOBSERVE_TARGET = "重新观察模糊启动壳层"
_LOGICAL_REOBSERVE_MAX = 2
_LOGICAL_REOBSERVE_TARGET = "重新观察 SPA/Flutter 逻辑页面转换"
_ACTION_REOBSERVATION_FAILURE_MAX = 3
_CAPABILITY_PROJECTION_REOBSERVE_MAX = 3
_CAPABILITY_PROJECTION_REOBSERVE_TARGET = "重新观察能力投影失败页面"
_POPUP_DISMISSAL_TARGET = "关闭当前弹窗"
_POPUP_ABSENCE_REOBSERVE_TARGET = "重新核对弹窗状态"
_POPUP_DISMISSAL_CAPABILITIES = frozenset(
    {
        "dialog_content_close",
        "dialog_active_ad_close",
        "dialog_text_ad_close",
        "dialog_accessible_close",
        "dialog_acknowledgement",
        "dialog_generic_close",
        "dialog_overlay",
        "home_suspend_close",
        "overlay_ad_close",
        "homepage_payment_close",
        "flutter_app_open_popup_close",
        "flutter_centered_ad_popup_close",
        "flutter_centered_ad_popup_close_above",
        "flutter_centered_scroll_popup_close_below",
        "flutter_image_popup_top_right_close",
        "flutter_account_notice_top_right_close",
        "flutter_marketing_announcement_confirm",
        "modal_local_safe_acknowledgement",
        "rendered_modal_close",
        "profile_credential_screenshot_confirm",
        "visible_text_acknowledgement",
        "visible_text_close",
        "visible_text_skip",
        "visible_text_later",
    }
)
_OBSTRUCTION_SIGNATURE_PATTERN = re.compile(r"^h5-obstruction:[0-9a-f]{64}$")
_PAGE_TAB_NAVIGATION_TARGET_PATTERN = re.compile(
    r"^(?:\.van-tabs:visible \.van-tab|internal:page_tab):nth-of-type"
    r"\((?P<index>[1-9]\d*)\)(?::visible)?$"
)

_UNSTABLE_PAGE_STATES = frozenset({
    "loading", "transitioning", "transient_loading", "partially_loaded",
    "needs_more_observation", "countdown",
})


def h5_observation_allows_completion(observation: SurfaceObservation) -> bool:
    """空候选只说明暂时没有动作，不能覆盖当前页面的未稳定或阻挡事实。"""
    metadata = observation.metadata
    return bool(
        (observation.page_state or "").casefold() not in _UNSTABLE_PAGE_STATES
        and observation.page_type not in {"line_loading", "startup_ad"}
        and not h5_page_state_has_overlay(observation.page_state or "")
        and observation.overlay_signature is None
        and metadata.get("obstruction_blocking") is not True
        and metadata.get("obstruction_without_safe_action") is not True
        and metadata.get("requires_auth") is not True
        and metadata.get("detection_status") not in {"insufficient", "ambiguous", "error"}
    )


def h5_candidate_purpose(candidate: CandidateAction) -> str:
    """返回已持久化 H5 候选的执行目的，拒绝模型在执行边界改写目的。"""
    value = candidate.metadata.get("purpose")
    if isinstance(value, str) and value.strip() and value == value.strip():
        return value
    return "执行当前 Frontier 候选。"


@dataclass(frozen=True)
class H5NavigationEligibility:
    """H5 导航候选在进入 Explorer 前的稳定准入判定。"""

    eligible: bool
    reason_code: str | None = None


def evaluate_h5_navigation_candidate(
    *,
    destination_url: object,
    entry_url: object,
    allowed_route_prefixes: tuple[str, ...],
    navigation_depth: int,
    max_depth: int,
) -> H5NavigationEligibility:
    """校验导航目标，并返回可写入审计记录的稳定拒绝码。"""
    destination = _optional_text(destination_url)
    if destination is None:
        return H5NavigationEligibility(False, "h5_navigation_missing_url")
    target = urlsplit(destination)
    if target.scheme.casefold() not in {"http", "https"} or not target.netloc:
        return H5NavigationEligibility(False, "h5_navigation_invalid_scheme")

    entry = urlsplit(_optional_text(entry_url) or "")
    if (
        entry.scheme.casefold() not in {"http", "https"}
        or not entry.netloc
        or target.scheme.casefold() != entry.scheme.casefold()
        or target.netloc.casefold() != entry.netloc.casefold()
    ):
        return H5NavigationEligibility(False, "h5_navigation_cross_origin")
    if allowed_route_prefixes and not any(
        _path_within_route_prefix(target.path, prefix) for prefix in allowed_route_prefixes
    ):
        return H5NavigationEligibility(False, "h5_navigation_route_out_of_scope")
    if navigation_depth > max_depth:
        return H5NavigationEligibility(False, "h5_navigation_depth_exceeded")
    return H5NavigationEligibility(True)


def _trusted_detection_payload(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
        if isinstance(payload, Mapping):
            return payload
    raise TypeError("detection_result 必须为对象")


def _trusted_visual_projection_attachment(
    value: object,
) -> tuple[Mapping[str, Any], Mapping[int, object], Mapping[str, object]] | None:
    from services.api.app.agents.tools.page_detection_tools import (
        _trusted_visual_projection_attachment as resolve_attachment,
    )

    attachment = resolve_attachment(value)
    if not isinstance(attachment, Mapping):
        return None
    payload = attachment.get("payload")
    actions = attachment.get("trusted_visual_actions")
    source_context = attachment.get("source_context")
    if (
        not isinstance(payload, Mapping)
        or not isinstance(actions, Mapping)
        or not isinstance(source_context, Mapping)
    ):
        return None
    return payload, actions, source_context


def build_h5_frontier(
    *,
    detection_result: object,
    observed_at: datetime,
    navigation_depth: int,
    visited_state_ids: tuple[str, ...] = (),
    executed_candidate_ids: tuple[str, ...] = (),
    lineage_metadata: Mapping[str, Any] | None = None,
    logical_surface_effective: bool = False,
    logical_targets_effective: bool = False,
    trusted_source_restoration: bool = False,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
    source_task_id: str | None = None,
) -> FrontierSnapshot:
    """把已持久化 H5 页面判定转换为不含原始页面载荷的 Frontier。"""
    trusted_visual_attachment = _trusted_visual_projection_attachment(
        detection_result
    )
    if trusted_visual_attachment is None:
        detection_payload = _trusted_detection_payload(detection_result)
        trusted_visual_actions: Mapping[int, object] = {}
        trusted_visual_source_context: Mapping[str, object] | None = None
    else:
        (
            detection_payload,
            trusted_visual_actions,
            trusted_visual_source_context,
        ) = trusted_visual_attachment
    require_aware_datetime(observed_at, "observed_at")
    if isinstance(navigation_depth, bool) or not isinstance(navigation_depth, int):
        raise TypeError("navigation_depth 必须为整数")
    if navigation_depth < 0:
        raise ValueError("navigation_depth 不能为负数")
    if not isinstance(logical_surface_effective, bool):
        raise TypeError("logical_surface_effective 必须为布尔值")
    if not isinstance(logical_targets_effective, bool):
        raise TypeError("logical_targets_effective 必须为布尔值")
    if not isinstance(trusted_source_restoration, bool):
        raise TypeError("trusted_source_restoration 必须为布尔值")
    if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
        raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
    executed_candidate_ids = _unique_text_tuple(
        executed_candidate_ids,
        "executed_candidate_ids",
    )

    capture_id = _required_text(detection_payload.get("page_capture_id"), "page_capture_id")
    location = _optional_text(detection_payload.get("page_url")) or "about:blank"
    page_type = _optional_text(detection_payload.get("page_type"))
    page_state = _optional_text(detection_payload.get("page_state")) or "unknown"
    detected_page_state = page_state
    key_elements = _key_elements(detection_payload.get("matched_catalog_signals"))
    risk_flags = _text_tuple(detection_payload.get("risk_flags"))
    raw_obstruction_facts = detection_payload.get("obstruction_facts")
    obstruction_facts = (
        raw_obstruction_facts if isinstance(raw_obstruction_facts, Mapping) else {}
    )
    trusted_obstruction = _trusted_popup_obstruction(
        detection_payload.get("candidate_actions"),
        page_state=page_state,
    )
    if (
        h5_page_state_has_overlay(page_state)
        and h5_obstruction_facts_confirm_clearance(obstruction_facts)
        and trusted_obstruction is None
    ):
        # 完整现场事实优先于模型状态标签；不改变原检测结果或规则适用性。
        page_state = "stable"
    logical_projection = _effective_logical_surface_projection(
        detection_result=detection_payload,
        enabled=logical_surface_effective,
        page_type=page_type,
    )
    logical_targets_active = bool(
        logical_targets_effective and logical_projection is not None
    )
    state_material = (
        {
            "route_identity": canonical_h5_route_identity(location),
            "page_type": page_type,
            "page_state": page_state,
            "obstruction_signature": trusted_obstruction[1],
        }
        if trusted_obstruction is not None
        else {
            "route_identity": canonical_h5_route_identity(location),
            "page_type": page_type,
            "page_state": page_state,
            "key_elements": list(key_elements),
            "risk_flags": list(risk_flags),
        }
    )
    if logical_projection is not None:
        state_material["logical_surface"] = {
            "identity_fingerprint": logical_projection["identity_fingerprint"],
            "state_fingerprint": logical_projection["state_fingerprint"],
        }
    state_version = "h5-state:" + stable_protocol_hash(state_material)
    business_link_facts = detection_payload.get("h5_business_link_facts")

    candidates, blocked_candidate_ids = _candidate_actions(
        detection_payload.get("candidate_actions"),
        default_navigation_depth=navigation_depth,
        detection_confidence=_bounded_confidence(detection_payload.get("confidence")),
        source_state_version=state_version,
        executed_candidate_ids=frozenset(executed_candidate_ids),
        source_capture_id=capture_id,
        source_page_url=location,
        source_page_type=page_type,
        source_page_state=page_state,
        source_identity_fingerprint=(
            logical_projection["identity_fingerprint"]
            if logical_projection is not None
            else None
        ),
        source_state_fingerprint=(
            logical_projection["state_fingerprint"]
            if logical_projection is not None
            else None
        ),
        source_tab_context=(
            detection_payload.get("logical_surface", {}).get("state_signals", {})
            if logical_projection is not None else None
        ),
        source_branch_context=(
            _h5_source_branch_context(detection_payload.get("logical_surface"))
            if logical_projection is not None else None
        ),
        logical_targets_effective=logical_targets_active,
        visual_navigation_enforced=(
            logical_targets_active and h5_visual_coordinate_mode == "enforced"
        ),
        trusted_visual_actions=trusted_visual_actions,
        trusted_visual_source_context=trusted_visual_source_context,
        source_task_id=source_task_id,
        independent_navigation_targets=(
            obstruction_facts.get("independent_bottom_navigation_targets", ())
            if obstruction_facts.get("schema_version") == "h5-obstruction-fact.v1"
            and obstruction_facts.get("independent_navigation_capture_id") == capture_id
            and obstruction_facts.get("modal_census_complete") is True
            else ()
        ),
        verified_business_links=(
            business_link_facts.get("links", ())
            if isinstance(business_link_facts, Mapping)
            and business_link_facts.get("source_capture_id") == capture_id
            and business_link_facts.get("source_url") == location
            else ()
        ),
    )
    capability_projection_rejections = _popup_dismissal_projection_rejections(
        detection_payload.get("candidate_actions"),
        source_page_state=page_state,
    )
    if capability_projection_rejections:
        # 声明了平台弹窗能力却未通过合同的候选必须在投影边界失败关闭，
        # 不能丢掉 action_parameters 后退化成普通文本点击。
        candidates = tuple(
            candidate
            for candidate in candidates
            if not _is_untrusted_popup_dismissal_candidate(candidate)
        )
        candidate_ids = frozenset(candidate.candidate_id for candidate in candidates)
        blocked_candidate_ids = tuple(
            candidate_id
            for candidate_id in blocked_candidate_ids
            if candidate_id in candidate_ids
        )
    captcha_verification_required = bool(
        page_state.casefold() == "verification_required"
        and "captcha_required" in {flag.casefold() for flag in risk_flags}
    )
    if captcha_verification_required:
        # 验证码不是可自动清障弹窗：无论模型是否给出“关闭／换一个”等候选，
        # 都不得把它投影成普通页面动作或通过 occurrence 重生反复尝试。
        candidates = ()
        blocked_candidate_ids = ()
    if page_state.casefold() == H5_AGE_VERIFICATION_PAGE_STATE:
        # 年龄门禁同时暴露“退出”与“成年确认”；只保留检测器由完整结构
        # 签发且已转换成专用能力的成年确认，模型候选一律不得旁路。
        candidates = tuple(
            candidate
            for candidate in candidates
            if isinstance(candidate.metadata.get("action_parameters"), Mapping)
            and candidate.metadata["action_parameters"].get("target_mode")
            == H5_AGE_VERIFICATION_TARGET_MODE
        )
        blocked_candidate_ids = ()
    is_guide_page = (
        (page_type or "").casefold() == _GUIDE_PAGE_TYPE
        and _GUIDE_PAGE_DETERMINISTIC_FLAG in {flag.casefold() for flag in risk_flags}
    )
    if is_guide_page:
        # 引导页包含下载/安装入口，按页面类型记录后终止当前分支，禁止
        # 把任何下载按钮或 App 唤起入口投影成探索候选。
        # 只认检测器由完整证据签发的引导页：内容站页脚普遍挂着下载入口，
        # 模型会据此自报 guide_page，裸信页型会把正常内容站整条分支掐断。
        candidates = ()
        blocked_candidate_ids = ()
    line_loading_reobserve_count = _non_negative_int(
        (lineage_metadata or {}).get("line_loading_reobserve_count")
    )
    if (page_type or "").casefold() == "line_loading":
        # 精确结构签发的登录失败重试先执行一次；相同候选完成后会由
        # executed_candidate_ids 去重，后续仍回到有界同会话重观察。
        login_retry_candidate = next(
            (
                candidate
                for candidate in candidates
                if candidate.metadata.get("action_parameters", {}).get("target_mode")
                == H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE
            ),
            None,
        )
        line_loading_candidate = login_retry_candidate or _line_loading_observe_candidate(
            source_state_version=state_version,
            navigation_depth=navigation_depth,
            reobserve_count=line_loading_reobserve_count,
        )
        candidates = (line_loading_candidate,) if line_loading_candidate is not None else ()
        blocked_candidate_ids = ()
    startup_ad_countdown_reobserve_count = _non_negative_int(
        (lineage_metadata or {}).get("startup_ad_countdown_reobserve_count")
    )
    startup_ad_dismissal_count = _non_negative_int(
        (lineage_metadata or {}).get("startup_ad_dismissal_count")
    )
    # startup_ad 本身就是瞬态壳层；模型可能把仍可见的倒计时广告标成 stable，
    # 不能因此把根层 Frontier 误判为业务探索完成。
    is_startup_ad_surface = (page_type or "").casefold() == "startup_ad"
    startup_ad_dismissal_exhausted = bool(
        is_startup_ad_surface
        and startup_ad_dismissal_count >= _STARTUP_AD_DISMISSAL_MAX
    )
    if startup_ad_dismissal_exhausted:
        candidates = ()
        blocked_candidate_ids = ()
    elif is_startup_ad_surface and not _has_trusted_startup_ad_dismissal_candidate(candidates):
        startup_ad_countdown_candidate = _startup_ad_countdown_observe_candidate(
            source_state_version=state_version,
            navigation_depth=navigation_depth,
            reobserve_count=startup_ad_countdown_reobserve_count,
        )
        candidates = (
            (startup_ad_countdown_candidate,)
            if startup_ad_countdown_candidate is not None
            else ()
        )
        blocked_candidate_ids = ()
    flutter_bootstrap_reobserve_count = _non_negative_int(
        (lineage_metadata or {}).get("flutter_bootstrap_reobserve_count")
    )
    is_flutter_bootstrap_pending = bool(
        detection_payload.get("detection_status") == "insufficient"
        and page_type is None
        and page_state.casefold() == "needs_more_observation"
        and _FLUTTER_BOOTSTRAP_PENDING_FLAG in risk_flags
    )
    if is_flutter_bootstrap_pending:
        flutter_bootstrap_candidate = _flutter_bootstrap_observe_candidate(
            source_state_version=state_version,
            navigation_depth=navigation_depth,
            reobserve_count=flutter_bootstrap_reobserve_count,
        )
        candidates = (
            (flutter_bootstrap_candidate,)
            if flutter_bootstrap_candidate is not None
            else ()
        )
        blocked_candidate_ids = ()
    insufficient_reobserve_count = _non_negative_int(
        (lineage_metadata or {}).get("insufficient_reobserve_count")
    )
    is_insufficient_unknown_page = bool(
        not is_flutter_bootstrap_pending
        and (page_type or "").casefold() not in {"line_loading", "startup_ad"}
        and (
            (detection_payload.get("detection_status") == "insufficient"
             and page_type is None and page_state.casefold() == "needs_more_observation")
            or (not candidates and page_state.casefold() in _UNSTABLE_PAGE_STATES)
            or (not candidates and obstruction_facts.get("blocking") is True
                and obstruction_facts.get("snapshot_evidence_status") == "incomplete")
        )
    )
    if is_insufficient_unknown_page:
        insufficient_candidate = _insufficient_observe_candidate(
            source_state_version=state_version,
            navigation_depth=navigation_depth,
            reobserve_count=insufficient_reobserve_count,
        )
        candidates = (insufficient_candidate,) if insufficient_candidate is not None else ()
        blocked_candidate_ids = ()
    ambiguous_launch_shell_reobserve_count = _non_negative_int(
        (lineage_metadata or {}).get("ambiguous_launch_shell_reobserve_count")
    )
    is_ambiguous_launch_shell = _is_ambiguous_launch_shell_detection(
        detection_payload
    )
    if is_ambiguous_launch_shell:
        ambiguous_launch_candidate = _ambiguous_launch_shell_observe_candidate(
            source_state_version=state_version,
            navigation_depth=navigation_depth,
            reobserve_count=ambiguous_launch_shell_reobserve_count,
        )
        candidates = (
            (ambiguous_launch_candidate,)
            if ambiguous_launch_candidate is not None
            else ()
        )
        blocked_candidate_ids = ()
    loading_navigation = tuple(
        candidate
        for candidate in candidates
        if page_state.casefold() == "loading"
        and candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and isinstance(candidate.metadata.get("action_parameters"), Mapping)
        and candidate.metadata["action_parameters"].get("target_mode") == "bottom_navigation"
        and candidate.metadata["action_parameters"].get("navigation_family") == "adaptive_snapshot"
        and candidate.metadata.get("interaction_pattern_signature") is not None
    )
    reobserve_surface = logical_projection
    if loading_navigation and reobserve_surface is None:
        # 普通 DOM 文档同样可能只有正文卡住，不把它伪装成 SPA 逻辑页面。
        surface = detection_payload.get("logical_surface")
        if isinstance(surface, Mapping):
            identity = _logical_fingerprint(surface.get("identity_fingerprint"))
            state = _logical_fingerprint(surface.get("state_fingerprint"))
            if identity is not None and state is not None:
                reobserve_surface = {
                    "identity_fingerprint": identity,
                    "state_fingerprint": state,
                }
    logical_identity = (
        reobserve_surface["identity_fingerprint"] if reobserve_surface is not None else None
    )
    prior_logical_reobserve_identity = _optional_text(
        (lineage_metadata or {}).get("logical_reobserve_identity_fingerprint")
    )
    logical_reobserve_count = _non_negative_int(
        (lineage_metadata or {}).get("logical_reobserve_count")
    )
    logical_surface_loading = bool(
        reobserve_surface is not None
        and (_logical_surface_loading(detection_payload) or bool(loading_navigation)
             or page_state.casefold() in _UNSTABLE_PAGE_STATES)
        and (page_type or "").casefold() not in {"line_loading", "startup_ad"}
        and not h5_page_state_has_overlay(page_state)
    )
    if not logical_surface_loading and (
        (
            logical_projection is not None
            and logical_projection["logical_transition_proof"] == "trusted"
        )
        or prior_logical_reobserve_identity not in {None, logical_identity}
    ):
        logical_reobserve_count = 0
    logical_transition_requires_reobserve = bool(
        logical_projection is not None
        and logical_projection["logical_transition_proof"]
        in {"uncertain", "insufficient"}
        and (page_type or "").casefold() not in {"line_loading", "startup_ad"}
        and not h5_page_state_has_overlay(page_state)
    )
    logical_reobserve_exhausted = bool(
        (logical_transition_requires_reobserve or logical_surface_loading)
        and logical_reobserve_count >= _LOGICAL_REOBSERVE_MAX
    )
    if logical_surface_loading or (
        logical_transition_requires_reobserve and not trusted_source_restoration
    ):
        # URL 恢复成功只证明回到了目标路由，不证明异步业务区已经稳定。
        # 先保持有界复验；正文仍未稳定时只能继续独立验签的全局导航，
        # 不允许正文候选，也不改变加载状态或授予规则匹配能力。
        assert reobserve_surface is not None
        logical_reobserve_candidate = _logical_surface_reobserve_candidate(
            source_state_version=state_version,
            navigation_depth=navigation_depth,
            reobserve_count=logical_reobserve_count,
            source_identity_fingerprint=reobserve_surface["identity_fingerprint"],
            source_state_fingerprint=reobserve_surface["state_fingerprint"],
        )
        candidates = (
            (logical_reobserve_candidate,)
            if logical_reobserve_candidate is not None
            else loading_navigation
        )
        blocked_candidate_ids = ()
    action_reobservation_capture_id = _action_reobservation_capture_id(
        lineage_metadata=lineage_metadata,
        capture_id=capture_id,
    )
    action_reobservation_candidate_identity = _action_reobservation_candidate_identity(
        lineage_metadata=lineage_metadata,
    )
    action_reobservation_failure_count = _non_negative_int(
        (lineage_metadata or {}).get("action_reobservation_failure_count")
    )
    action_reobservation_failure_max = _positive_int_or_default(
        (lineage_metadata or {}).get("action_reobservation_failure_max"),
        default=_ACTION_REOBSERVATION_FAILURE_MAX,
    )
    action_reobservation_exhausted = bool(
        action_reobservation_capture_id is not None
        and action_reobservation_candidate_identity is not None
        and action_reobservation_failure_count >= action_reobservation_failure_max
    )
    action_reobservation_retry_allowed = bool(
        (lineage_metadata or {}).get("action_reobservation_retryable") is not False
        and not action_reobservation_exhausted
    )
    if (
        action_reobservation_capture_id is not None
        and action_reobservation_candidate_identity is not None
    ):
        # 历史持久化的重观察事实没有 retryable 字段，按原有可重试语义兼容；
        # 新合同显式写入 false 或同错误连续失败 3 次时永久过滤该候选。
        if action_reobservation_retry_allowed:
            candidates = tuple(
                replace(
                    candidate,
                    metadata={
                        **dict(candidate.metadata),
                        "action_reobservation_capture_id": action_reobservation_capture_id,
                    },
                )
                if _candidate_reobservation_identity(candidate)
                == action_reobservation_candidate_identity
                else candidate
                for candidate in candidates
            )
        else:
            candidates = tuple(
                candidate
                for candidate in candidates
                if _candidate_reobservation_identity(candidate)
                != action_reobservation_candidate_identity
            )
            candidate_ids = frozenset(candidate.candidate_id for candidate in candidates)
            blocked_candidate_ids = tuple(
                candidate_id
                for candidate_id in blocked_candidate_ids
                if candidate_id in candidate_ids
            )
        if (
            action_reobservation_retry_allowed
            and (lineage_metadata or {}).get("action_reobserve_error_code")
            == "POPUP_DISMISSAL_NOT_FOUND"
            and page_state.casefold() != "entitlement_locked"
        ):
            # 未找到目标不等于退场；先无副作用重取当前状态，不能猜关闭已完成。
            candidates = (
                _popup_absence_observe_candidate(
                    source_capture_id=capture_id,
                    source_state_version=state_version,
                    navigation_depth=navigation_depth,
                    obstruction_signature=_popup_dismissal_reobserve_signature(
                        lineage_metadata
                    ),
                ),
            )
            blocked_candidate_ids = ()
    if (
        (lineage_metadata or {}).get("popup_absence_reobserve_completed") is True
        and not h5_page_state_supports_popup_dismissal(page_state)
        and obstruction_facts.get("blocking") is not True
    ):
        # 兼容旧已观察标志，但只有当前无弹层、无阻挡事实才允许抑制旧候选。
        completed_popup_signature = _popup_dismissal_reobserve_signature(lineage_metadata)
        candidates = tuple(
            candidate
            for candidate in candidates
            if not _same_popup_dismissal_signature(
                candidate,
                completed_popup_signature=completed_popup_signature,
            )
        )
        candidate_ids = frozenset(candidate.candidate_id for candidate in candidates)
        blocked_candidate_ids = tuple(
            candidate_id
            for candidate_id in blocked_candidate_ids
            if candidate_id in candidate_ids
        )
    raw_capability_projection_reobserve_count = _non_negative_int(
        (lineage_metadata or {}).get("capability_projection_reobserve_count")
    )
    capability_projection_reobserve_required = bool(
        (capability_projection_rejections
         or (not candidates and obstruction_facts.get("blocking") is True))
        and h5_page_state_supports_popup_dismissal(page_state)
        and not _has_trusted_popup_dismissal_candidate(candidates)
        and (lineage_metadata or {}).get("popup_absence_reobserve_completed") is not True
    )
    capability_projection_reobserve_count = (
        raw_capability_projection_reobserve_count
        if capability_projection_reobserve_required
        else 0
    )
    capability_projection_reobserve_exhausted = bool(
        capability_projection_reobserve_required
        and capability_projection_reobserve_count
        >= _CAPABILITY_PROJECTION_REOBSERVE_MAX
    )
    if capability_projection_reobserve_required:
        # 普查完整但暂未发现安全关闭目标，也先取新帧重识别；沿用原限次账本。
        projection_reobserve_candidate = _capability_projection_reobserve_candidate(
            source_state_version=state_version,
            navigation_depth=navigation_depth,
            reobserve_count=capability_projection_reobserve_count,
        )
        candidates = (
            (projection_reobserve_candidate,)
            if projection_reobserve_candidate is not None
            else ()
        )
        blocked_candidate_ids = ()
    requires_auth = h5_authentication_required(
        page_state=page_state,
        risk_flags=risk_flags,
        trusted_obstruction_dismissal_available=(
            h5_trusted_obstruction_dismissal_available(
                page_state=page_state,
                candidate_actions=detection_payload.get("candidate_actions"),
            )
        ),
    )
    objective_satisfied = bool(
        detection_payload.get("detection_status") == "recognized"
        and navigation_depth == 0
        and not candidates
        and not requires_auth
        and not h5_page_state_has_overlay(page_state)
        and (page_type or "").casefold() != "line_loading"
        and not is_startup_ad_surface
        and not logical_transition_requires_reobserve
        and not logical_surface_loading
        and page_state.casefold() not in _UNSTABLE_PAGE_STATES
        and obstruction_facts.get("blocking") is not True
        and obstruction_facts.get("obstruction_without_safe_action") is not True
    )
    metadata: dict[str, Any] = {
        **dict(lineage_metadata or {}),
        "schema_version": _optional_text(detection_payload.get("schema_version")),
        "detection_status": _optional_text(detection_payload.get("detection_status")),
        "detection_confidence": _bounded_confidence(detection_payload.get("confidence")),
        "confidence_source": _optional_text(detection_payload.get("confidence_source")),
        "detection_reason": _optional_text(detection_payload.get("reason")),
        "evidence_artifact_ids": list(_text_tuple(detection_payload.get("evidence_artifact_ids"))),
        "risk_flags": list(risk_flags),
        "detected_page_state": detected_page_state,
        "obstruction_kind": _optional_text(obstruction_facts.get("kind")),
        "obstruction_blocking": obstruction_facts.get("blocking") is True,
        "obstruction_snapshot_evidence_status": _optional_text(
            obstruction_facts.get("snapshot_evidence_status")
        ),
        "obstruction_modal_census_complete": obstruction_facts.get("modal_census_complete") is True,
        "obstruction_modal_census_count": obstruction_facts.get("modal_census_count"),
        "obstruction_without_safe_action": (
            obstruction_facts.get("obstruction_without_safe_action") is True
        ),
        "catalog_version": _optional_text(detection_payload.get("catalog_version")),
        "catalog_sha256": _optional_text(detection_payload.get("catalog_sha256")),
        "catalog_disposition": _optional_text(
            detection_payload.get("catalog_disposition")
        ),
        "catalog_skip_reason": _optional_text(
            detection_payload.get("catalog_skip_reason")
        ),
        "detector_model": _optional_text(detection_payload.get("model")),
        "skill_sha256": _optional_text(detection_payload.get("skill_sha256")),
        "navigation_depth": navigation_depth,
        "executed_candidate_ids": list(executed_candidate_ids),
        "objective_satisfied": objective_satisfied,
        "requires_auth": requires_auth,
        "captcha_verification_required": captcha_verification_required,
        "guide_page_terminal": is_guide_page,
        "global_bottom_navigation_indices": list(
            _global_bottom_navigation_indices(candidates)
        ),
        "global_bottom_navigation_targets": [
            {
                "semantic_target": candidate.semantic_target,
                "action_parameters": dict(candidate.metadata["action_parameters"]),
            }
            for candidate in candidates
            if isinstance(candidate.metadata.get("action_parameters"), Mapping)
            and candidate.metadata["action_parameters"].get("target_mode") == "bottom_navigation"
        ],
        "line_loading_reobserve_count": line_loading_reobserve_count,
        "line_loading_reobserve_exhausted": (
            (page_type or "").casefold() == "line_loading"
            and line_loading_reobserve_count >= _LINE_LOADING_REOBSERVE_MAX
        ),
        "startup_ad_countdown_reobserve_count": (
            startup_ad_countdown_reobserve_count
        ),
        "startup_ad_countdown_reobserve_exhausted": (
            is_startup_ad_surface
            and startup_ad_countdown_reobserve_count
            >= _STARTUP_AD_COUNTDOWN_REOBSERVE_MAX
        ),
        "startup_ad_dismissal_count": startup_ad_dismissal_count,
        "startup_ad_dismissal_exhausted": startup_ad_dismissal_exhausted,
        "flutter_bootstrap_reobserve_count": flutter_bootstrap_reobserve_count,
        "flutter_bootstrap_reobserve_exhausted": (
            is_flutter_bootstrap_pending
            and flutter_bootstrap_reobserve_count >= _FLUTTER_BOOTSTRAP_REOBSERVE_MAX
        ),
        "insufficient_reobserve_count": insufficient_reobserve_count,
        "insufficient_reobserve_exhausted": (
            is_insufficient_unknown_page
            and insufficient_reobserve_count >= _INSUFFICIENT_REOBSERVE_MAX
        ),
        "ambiguous_launch_shell": is_ambiguous_launch_shell,
        "ambiguous_launch_shell_reobserve_count": (
            ambiguous_launch_shell_reobserve_count
        ),
        "ambiguous_launch_shell_reobserve_exhausted": (
            is_ambiguous_launch_shell
            and ambiguous_launch_shell_reobserve_count
            >= _AMBIGUOUS_LAUNCH_SHELL_REOBSERVE_MAX
        ),
        "action_reobservation_failure_count": action_reobservation_failure_count,
        "action_reobservation_exhausted": action_reobservation_exhausted,
        "capability_projection_rejections": list(
            capability_projection_rejections
        ),
        "capability_projection_reobserve_count": (
            capability_projection_reobserve_count
        ),
        "capability_projection_reobserve_exhausted": (
            capability_projection_reobserve_exhausted
        ),
    }
    if reobserve_surface is not None:
        metadata.update(
            {
                "logical_reobserve_count": logical_reobserve_count,
                "logical_reobserve_exhausted": logical_reobserve_exhausted,
                "logical_reobserve_identity_fingerprint": logical_identity,
            }
        )
    if logical_projection is not None:
        candidates = tuple(replace(candidate, metadata={
            **candidate.metadata,
            "source_document_identity": logical_projection.get("document_identity"),
        }) for candidate in candidates)
        metadata.update(
            {
                "logical_surface_identity_fingerprint": logical_projection[
                    "identity_fingerprint"
                ],
                "logical_surface_state_fingerprint": logical_projection[
                    "state_fingerprint"
                ],
                "render_profile": logical_projection["render_profile"],
                "logical_transition_proof": logical_projection[
                    "logical_transition_proof"
                ],
                "logical_surface_document_identity": logical_projection.get("document_identity"),
                "logical_transition_reason": logical_projection.get("transition_reason"),
                "logical_transition_before_identity": logical_projection.get(
                    "transition_before_identity"
                ),
                "logical_transition_after_identity": logical_projection.get(
                    "transition_after_identity"
                ),
                "logical_clearance_proven": logical_projection.get("clearance_proven"),
            }
        )
        if logical_targets_active:
            metadata["logical_targets_effective"] = True
    if h5_visual_coordinate_mode != "off":
        metadata["h5_visual_coordinate_mode"] = h5_visual_coordinate_mode
    observation = SurfaceObservation(
        capture_id=capture_id,
        surface_kind=SurfaceKind.WEB,
        location=location,
        state_version=state_version,
        observed_at=observed_at,
        page_type=page_type,
        page_state=page_state,
        overlay_signature=(
            "h5-overlay:" + stable_protocol_hash(state_material)
            if h5_page_state_has_overlay(page_state)
            else None
        ),
        key_elements=key_elements,
        metadata=metadata,
    )
    snapshot_id = "h5-frontier:" + stable_protocol_hash(
        {
            "capture_id": capture_id,
            "state_version": state_version,
            "candidate_ids": [item.candidate_id for item in candidates],
            "blocked_candidate_ids": list(blocked_candidate_ids),
        }
    )
    return FrontierSnapshot(
        snapshot_id=snapshot_id,
        observation=observation,
        candidates=candidates,
        visited_state_ids=visited_state_ids,
        blocked_candidate_ids=blocked_candidate_ids,
    )


def _global_bottom_navigation_indices(
    candidates: tuple[CandidateAction, ...],
) -> tuple[int, ...]:
    """投影当前 Capture 确定性签发的全局底栏位置，供跨状态候选复核。"""
    indices: set[int] = set()
    for candidate in candidates:
        parameters = candidate.metadata.get("action_parameters")
        if not isinstance(parameters, Mapping):
            continue
        navigation_index = parameters.get("navigation_index")
        if (
            parameters.get("target_mode") == "bottom_navigation"
            and isinstance(navigation_index, int)
            and not isinstance(navigation_index, bool)
        ):
            indices.add(navigation_index)
    return tuple(sorted(indices))


def _h5_source_branch_context(logical_surface: object) -> str | None:
    """用当前全局激活模块限定局部页签，不把选中页签或 Capture 当父身份。"""
    if not isinstance(logical_surface, Mapping):
        return None
    signals = logical_surface.get("identity_signals")
    navigation = signals.get("active_navigation") if isinstance(signals, Mapping) else None
    if isinstance(navigation, (list, tuple)) and navigation:
        return stable_protocol_hash({"active_navigation": list(navigation)})
    route = signals.get("route") if isinstance(signals, Mapping) else None
    if isinstance(route, str) and route:
        return route
    identity = logical_surface.get("identity_fingerprint")
    return identity if isinstance(identity, str) and identity else None


def _candidate_actions(
    value: object,
    *,
    default_navigation_depth: int,
    detection_confidence: float,
    source_state_version: str,
    source_capture_id: str,
    source_page_url: str,
    source_page_type: str | None,
    source_page_state: str,
    source_identity_fingerprint: str | None = None,
    source_state_fingerprint: str | None = None,
    source_tab_context: Mapping[str, Any] | None = None,
    source_branch_context: str | None = None,
    logical_targets_effective: bool = False,
    visual_navigation_enforced: bool = False,
    trusted_visual_actions: Mapping[int, object] | None = None,
    trusted_visual_source_context: Mapping[str, object] | None = None,
    source_task_id: str | None = None,
    executed_candidate_ids: frozenset[str] = frozenset(),
    independent_navigation_targets: object = (),
    verified_business_links: object = (),
) -> tuple[tuple[CandidateAction, ...], tuple[str, ...]]:
    raw_items = value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()
    visual_entry_counts: dict[str, int] = {}
    for item in raw_items:
        if not isinstance(item, Mapping) or not isinstance(
            item.get("visual_bounds"),
            Mapping,
        ):
            continue
        contract = h5_profile_visual_entry_contract(item.get("target"))
        if contract is not None:
            visual_entry_counts[contract.expected_page_type] = (
                visual_entry_counts.get(contract.expected_page_type, 0) + 1
            )
    candidates: list[CandidateAction] = []
    blocked: list[str] = []
    seen: set[str] = set()
    for raw_index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            continue
        trusted_visual_action = (trusted_visual_actions or {}).get(raw_index)
        trusted_visual_capability = getattr(
            trusted_visual_action,
            "_trusted_visual_navigation_capability",
            None,
        )
        action_text = _optional_text(raw.get("action"))
        if action_text is not None and action_text.casefold() == "goto":
            action_text = InteractionOperation.NAVIGATE.value
        destination_url = _optional_text(raw.get("destination_url")) or _optional_text(
            raw.get("url")
        )
        target = _optional_text(raw.get("target")) or (
            destination_url if action_text == InteractionOperation.NAVIGATE.value else None
        )
        risk_text = (_optional_text(raw.get("risk_level")) or "high").casefold()
        if action_text is None or target is None or risk_text not in _RISK_LEVELS:
            continue
        try:
            operation = InteractionOperation(action_text.casefold())
        except ValueError:
            continue
        independent_navigation = bool(
            isinstance(independent_navigation_targets, (list, tuple))
            and sum(isinstance(proof, Mapping) and proof.get("target") == target
                    and proof.get("pattern_signature") == raw.get("interaction_pattern_signature")
                    for proof in independent_navigation_targets) == 1
            and isinstance(raw.get("interaction_pattern_signature"), str)
            and re.fullmatch(r"[0-9a-f]{64}", raw["interaction_pattern_signature"])
            and "bottom_navigation" in _text_tuple(raw.get("target_keywords"))
        )
        structural_navigation_parameters = _structural_navigation_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
            independent_navigation=independent_navigation,
        )
        if structural_navigation_parameters is None:
            try:
                # 自由模型目标禁止携带定位器；平台按 DOM 确定性签发的结构导航
                # 已由上面的固定合同验签，不再重复按自由文本目标拒绝。
                validate_semantic_target(target, "target")
            except InteractionValidationError:
                continue
        semantic_target = (
            h5_page_tab_target(
                index=int(structural_navigation_parameters["navigation_index"])
            )
            if structural_navigation_parameters is not None
            and structural_navigation_parameters.get("target_mode")
            == "page_tab_navigation"
            else target
        )
        expected_page_type = _optional_text(raw.get("expected_next_page_type"))
        verified_business_navigation = bool(
            operation is InteractionOperation.NAVIGATE and risk_text == "low"
            and isinstance(verified_business_links, (list, tuple))
            and sum(isinstance(proof, Mapping)
                    and proof.get("page_type") == expected_page_type
                    and proof.get("destination_url") == destination_url
                    for proof in verified_business_links) == 1
            and source_page_state in {"stable", "normal", "feed", "loaded", "notice"}
        )
        logical_target_required = bool(
            logical_targets_effective
            and operation
            in {
                InteractionOperation.NAVIGATE,
                InteractionOperation.CLICK,
                InteractionOperation.TAP,
                InteractionOperation.INPUT,
                InteractionOperation.HOVER,
                InteractionOperation.SEEK_MEDIA,
                InteractionOperation.BACK,
                InteractionOperation.SCROLL,
            }
        )
        line_loading_login_retry_parameters = (
            _line_loading_login_retry_action_parameters(
                raw=raw,
                operation=operation,
                target=target,
                risk_text=risk_text,
                source_page_type=source_page_type,
                source_page_state=source_page_state,
            )
        )
        startup_ad_action_parameters = _startup_ad_dismissal_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
        )
        age_verification_parameters = _age_verification_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        popup_action_parameters = _popup_dismissal_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_state=source_page_state,
        )
        detail_scroll_coverage_parameters = _detail_scroll_coverage_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        list_scroll_coverage_parameters = _list_scroll_coverage_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        video_timed_capture_parameters = _video_timed_capture_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        if target == "采集视频播放多帧" and video_timed_capture_parameters is None:
            continue
        if target == "采集列表至底部" and list_scroll_coverage_parameters is None:
            continue
        list_detail_parameters = _list_detail_navigation_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        profile_settings_parameters = _profile_settings_navigation_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        settings_page_back_parameters = _settings_page_back_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        profile_favorites_page_back_parameters = (
            _profile_favorites_page_back_action_parameters(
                raw=raw,
                operation=operation,
                target=target,
                risk_text=risk_text,
                source_page_type=source_page_type,
                source_page_state=source_page_state,
            )
        )
        profile_activity_parameters = _profile_activity_navigation_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        profile_payment_parameters = _profile_payment_navigation_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_page_type=source_page_type,
            source_page_state=source_page_state,
        )
        payment_horizontal_parameters = (
            _payment_horizontal_collection_action_parameters(
                raw=raw,
                operation=operation,
                target=target,
                risk_text=risk_text,
                source_page_type=source_page_type,
                source_page_state=source_page_state,
            )
        )
        visual_navigation_parameters = _visual_navigation_action_parameters(
            raw=raw,
            trusted_capability=trusted_visual_capability,
            operation=operation,
            target=target,
            risk_text=risk_text,
            expected_page_type=expected_page_type,
            source_capture_id=source_capture_id,
            source_page_type=source_page_type,
            source_identity_fingerprint=source_identity_fingerprint,
            source_state_fingerprint=source_state_fingerprint,
            source_task_id=source_task_id,
            trusted_source_context=trusted_visual_source_context,
            logical_targets_effective=visual_navigation_enforced,
        )
        if (
            (
                "visual_navigation_capability" in raw
                or trusted_visual_action is not None
            )
            and visual_navigation_parameters is None
        ):
            # 声明了新视觉能力却未通过完整验签时必须整条候选失败关闭，
            # 不能退化成同名自由文本点击或落入旧 visual_coordinate 分支。
            continue
        profile_local_parameters = {
            "activity": profile_activity_parameters,
            "settings": profile_settings_parameters,
            "payment": profile_payment_parameters,
        }.get((expected_page_type or "").casefold())
        visual_action_parameters = _profile_visual_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=risk_text,
            source_capture_id=source_capture_id,
            source_page_url=source_page_url,
            source_page_type=source_page_type,
        )
        visual_contract = h5_profile_visual_entry_contract(target)
        if (
            visual_action_parameters is not None
            and (
                visual_contract is None
                or visual_entry_counts.get(
                    visual_contract.expected_page_type,
                    0,
                )
                != 1
            )
        ):
            visual_action_parameters = None
        if (
            (expected_page_type or "").casefold()
            in {"activity", "settings", "payment"}
            and profile_local_parameters is None
            and not verified_business_navigation
            and visual_navigation_parameters is None
            and visual_action_parameters is None
        ):
            continue
        navigation_knowledge_metadata = _candidate_navigation_knowledge_metadata(raw)
        interaction_pattern_metadata = _candidate_interaction_pattern_metadata(raw)
        seek_fraction = _seek_fraction(raw.get("target_fraction"), operation=operation)
        stable_capture_identity = bool(
            video_timed_capture_parameters is not None
            or (
                list_scroll_coverage_parameters is not None
                and source_identity_fingerprint is not None
            )
        )
        bounded_capture_context = None
        if list_scroll_coverage_parameters is not None and isinstance(source_tab_context, Mapping):
            selected_tab = _optional_text(source_tab_context.get("selected_tab"))
            tab_key = _optional_text(source_tab_context.get("tab_key"))
            if selected_tab and tab_key:
                bounded_capture_context = {"selected_tab": selected_tab, "tab_key": tab_key}
        candidate_id = "h5-candidate:" + stable_protocol_hash(
            {
                **({"bounded_capture_context": bounded_capture_context}
                   if bounded_capture_context is not None else {}),
                "source_state_version": (
                    "bounded_capture_once"
                    if stable_capture_identity
                    else source_state_version
                ),
                **(
                    {"source_identity_fingerprint": source_identity_fingerprint}
                    if source_identity_fingerprint is not None
                    else {}
                ),
                **(
                    {"source_state_fingerprint": source_state_fingerprint}
                    if source_state_fingerprint is not None
                    and not stable_capture_identity
                    else {}
                ),
                "action": operation.value,
                "semantic_target": semantic_target.casefold(),
                "expected_page_type": expected_page_type,
                "target_fraction": seek_fraction,
                "visual_action_parameters": visual_action_parameters,
                "line_loading_login_retry_parameters": (
                    line_loading_login_retry_parameters
                ),
                "startup_ad_action_parameters": startup_ad_action_parameters,
                "age_verification_parameters": age_verification_parameters,
                "startup_ad_occurrence_capture_id": (
                    source_capture_id
                    if startup_ad_action_parameters is not None
                    else None
                ),
                "popup_action_parameters": popup_action_parameters,
                "popup_occurrence_capture_id": (
                    source_capture_id if popup_action_parameters is not None else None
                ),
                "structural_navigation_parameters": structural_navigation_parameters,
                "list_detail_parameters": list_detail_parameters,
                "profile_settings_parameters": profile_settings_parameters,
                "settings_page_back_parameters": settings_page_back_parameters,
                "profile_favorites_page_back_parameters": (
                    profile_favorites_page_back_parameters
                ),
                "profile_activity_parameters": profile_activity_parameters,
                "profile_payment_parameters": profile_payment_parameters,
                "payment_horizontal_parameters": payment_horizontal_parameters,
                "list_scroll_coverage_parameters": list_scroll_coverage_parameters,
                "video_timed_capture_parameters": video_timed_capture_parameters,
                "bounded_capture_page_url": (
                    source_page_url if stable_capture_identity else None
                ),
                **(
                    {"logical_target_required": True}
                    if logical_target_required
                    else {}
                ),
            }
        )
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        if candidate_id in executed_candidate_ids:
            continue
        raw_depth = raw.get("navigation_depth")
        depth = (
            raw_depth
            if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) and raw_depth >= 0
            else default_navigation_depth
        )
        risk_level = _RISK_LEVELS[risk_text]
        action_parameters = (
            visual_navigation_parameters
            if visual_navigation_parameters is not None
            else profile_local_parameters
            if profile_local_parameters is not None
            else line_loading_login_retry_parameters
            if line_loading_login_retry_parameters is not None
            else age_verification_parameters
            if age_verification_parameters is not None
            else startup_ad_action_parameters
            if startup_ad_action_parameters is not None
            else popup_action_parameters
            if popup_action_parameters is not None
            else visual_action_parameters
            if visual_action_parameters is not None
            else structural_navigation_parameters
            if structural_navigation_parameters is not None
            else detail_scroll_coverage_parameters
            if detail_scroll_coverage_parameters is not None
            else list_scroll_coverage_parameters
            if list_scroll_coverage_parameters is not None
            else video_timed_capture_parameters
            if video_timed_capture_parameters is not None
            else list_detail_parameters
            if list_detail_parameters is not None
            else profile_settings_parameters
            if profile_settings_parameters is not None
            else settings_page_back_parameters
            if settings_page_back_parameters is not None
            else profile_favorites_page_back_parameters
            if profile_favorites_page_back_parameters is not None
            else profile_activity_parameters
            if profile_activity_parameters is not None
            else profile_payment_parameters
            if profile_payment_parameters is not None
            else payment_horizontal_parameters
            if payment_horizontal_parameters is not None
            else {"target_fraction": seek_fraction}
            if operation is InteractionOperation.SEEK_MEDIA
            else None
        )
        candidate = CandidateAction(
            candidate_id=candidate_id,
            semantic_target=semantic_target,
            action_type=operation,
            risk_level=risk_level,
            exploration_value=_interaction_pattern_exploration_value(
                base_value=round(detection_confidence * 100),
                status=interaction_pattern_metadata.get("interaction_pattern_status"),
            ),
            navigation_depth=depth,
            expected_surface_kind=SurfaceKind.WEB,
            metadata={
                "purpose": _optional_text(raw.get("purpose")),
                "target_keywords": list(_text_tuple(raw.get("target_keywords"))),
                "expected_next_page_type": expected_page_type,
                **navigation_knowledge_metadata,
                **interaction_pattern_metadata,
                **(
                    {"destination_url": destination_url}
                    if operation is InteractionOperation.NAVIGATE
                    else {}
                ),
                **(
                    {"target_fraction": seek_fraction}
                    if operation is InteractionOperation.SEEK_MEDIA
                    else {}
                ),
                **(
                    {
                        "obstruction_signature": action_parameters[
                            "obstruction_signature"
                        ]
                    }
                    if action_parameters is not None
                    and "obstruction_signature" in action_parameters
                    else {}
                ),
                **(
                    {"source_page_type": source_page_type}
                    if source_page_type is not None
                    else {}
                ),
                "source_page_state": source_page_state,
                **({"verified_business_link": True} if verified_business_navigation else {}),
                **({"independent_bottom_navigation": True}
                   if independent_navigation and structural_navigation_parameters is not None
                   and structural_navigation_parameters.get("target_mode") == "bottom_navigation"
                   else {}),
                **({"source_branch_context": source_branch_context}
                   if source_branch_context is not None else {}),
                **({"bounded_capture_context": bounded_capture_context}
                   if bounded_capture_context is not None else {}),
                **(
                    {"source_identity_fingerprint": source_identity_fingerprint}
                    if source_identity_fingerprint is not None
                    else {}
                ),
                **(
                    {"source_state_fingerprint": source_state_fingerprint}
                    if source_state_fingerprint is not None
                    else {}
                ),
                **(
                    {"logical_target_required": True}
                    if logical_target_required
                    else {}
                ),
                **({"action_parameters": action_parameters} if action_parameters else {}),
            },
        )
        candidates.append(candidate)
        if risk_level is InteractionRiskLevel.HIGH:
            blocked.append(candidate_id)
    return tuple(candidates), tuple(blocked)


def _effective_logical_surface_projection(
    *,
    detection_result: Mapping[str, Any],
    enabled: bool,
    page_type: str | None,
) -> dict[str, Any] | None:
    """只投影已确认、非 Shadow 且与当前页型绑定的逻辑身份。"""
    if not enabled or page_type is None:
        return None
    surface = detection_result.get("logical_surface")
    if (
        not isinstance(surface, Mapping)
        or surface.get("protocol_version") != LOGICAL_SURFACE_PROTOCOL_VERSION
        or surface.get("shadow") is not False
        or not logical_surface_is_effective(surface)
    ):
        return None
    identity_fingerprint = _logical_fingerprint(surface.get("identity_fingerprint"))
    state_fingerprint = _logical_fingerprint(surface.get("state_fingerprint"))
    binding = surface.get("page_type_binding")
    if (
        identity_fingerprint is None
        or state_fingerprint is None
        or not isinstance(binding, Mapping)
        or _optional_text(binding.get("identity_fingerprint"))
        != identity_fingerprint
        or _optional_text(binding.get("page_type")) != page_type
    ):
        return None
    profile = surface.get("render_profile")
    assert isinstance(profile, Mapping)
    transition = surface.get("transition")
    transition = transition if isinstance(transition, Mapping) else {}
    signals = surface.get("state_signals")
    signals = signals if isinstance(signals, Mapping) else {}
    return {
        "identity_fingerprint": identity_fingerprint,
        "state_fingerprint": state_fingerprint,
        "render_profile": str(profile.get("kind")),
        "logical_transition_proof": (
            _optional_text(surface.get("logical_transition_proof")) or "insufficient"
        ),
        "document_identity": surface.get("document_identity"),
        "transition_reason": transition.get("reason_code"),
        "transition_before_identity": transition.get("before_identity_fingerprint"),
        "transition_after_identity": transition.get("after_identity_fingerprint"),
        "clearance_proven": bool(
            transition.get("reason_code") == "trusted_obstruction_clearance"
            and transition.get("logical_transition_proof") == "trusted"
            and transition.get("before_document_identity")
            == transition.get("after_document_identity") == surface.get("document_identity")
            and surface.get("document_identity")
            and signals.get("obstruction_count") == "0"
            and signals.get("loading") is False and surface.get("evidence_quality") == "high"
        ),
    }


def _logical_surface_loading(detection_result: Mapping[str, Any]) -> bool:
    """读取已持久化逻辑页面的加载事实，不依赖模型页状态。"""

    surface = detection_result.get("logical_surface")
    state_signals = surface.get("state_signals") if isinstance(surface, Mapping) else None
    return bool(
        isinstance(state_signals, Mapping)
        and state_signals.get("loading") is True
    )


def _logical_fingerprint(value: Any) -> str | None:
    normalized = _optional_text(value)
    if normalized is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        return None
    return normalized


def _age_verification_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """仅投影检测器由完整门禁结构签发的成年确认能力。"""
    keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        source_page_type is not None
        or source_page_state.casefold() != H5_AGE_VERIFICATION_PAGE_STATE
        or operation is not InteractionOperation.CLICK
        or target != H5_AGE_VERIFICATION_TARGET
        or risk_text != "low"
        or raw.get("purpose") != H5_AGE_VERIFICATION_PURPOSE
        or raw.get("navigation_depth") != 0
        or _optional_text(raw.get("obstruction_capability"))
        != H5_AGE_VERIFICATION_CAPABILITY
        or not H5_AGE_VERIFICATION_KEYWORDS.issubset(keywords)
    ):
        return None
    return {
        "target_mode": H5_AGE_VERIFICATION_TARGET_MODE,
        "age_verification_capability": H5_AGE_VERIFICATION_CAPABILITY,
    }


def _line_loading_login_retry_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """只投影页面识别器由精确 Dialog 结构签发的线路匿名登录重试。"""
    capability = _optional_text(raw.get("obstruction_capability"))
    signature = _optional_text(raw.get("obstruction_signature"))
    keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        (source_page_type or "").casefold() != "line_loading"
        or source_page_state.casefold() != "login_required"
        or operation is not InteractionOperation.CLICK
        or target != H5_LINE_LOADING_LOGIN_RETRY_TARGET
        or risk_text != "low"
        or raw.get("purpose") != H5_LINE_LOADING_LOGIN_RETRY_PURPOSE
        or raw.get("navigation_depth") != 0
        or capability != H5_LINE_LOADING_LOGIN_RETRY_CAPABILITY
        or signature is None
        or _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature) is None
        or not H5_LINE_LOADING_LOGIN_RETRY_KEYWORDS.issubset(keywords)
    ):
        return None
    return {
        "target_mode": H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE,
        "line_loading_login_retry_capability": capability,
        "obstruction_signature": signature,
    }


def _profile_activity_navigation_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """只给结构确定的个人中心签到入口签发活动能力。"""
    keywords = set(_text_tuple(raw.get("target_keywords")))
    capability = h5_profile_activity_capability_for_target(target)
    if (
        operation is not InteractionOperation.CLICK
        or capability is None
        or risk_text != "low"
        or (source_page_type or "").casefold() != H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE
        or source_page_state.casefold() not in {"stable", "normal", "loaded"}
        or _optional_text(raw.get("expected_next_page_type"))
        != H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE
        or not {
            "activity_entry",
            "profile_local_action",
            "page_type:activity",
            "evidence:structure",
        }.issubset(keywords)
    ):
        return None
    return {
        "target_mode": H5_PROFILE_ACTIVITY_TARGET_MODE,
        "activity_entry_capability": capability,
        "source_page_type": H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE,
        "expected_page_type": H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE,
    }


def _profile_settings_navigation_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """只给平台确定性识别的“我的”页设置入口签发结构能力。"""
    keywords = set(_text_tuple(raw.get("target_keywords")))
    capability = h5_profile_settings_capability_for_target(target)
    if (
        operation is not InteractionOperation.CLICK
        or capability is None
        or risk_text != "low"
        or (source_page_type or "").casefold() != H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE
        or source_page_state.casefold() not in {"stable", "normal", "loaded"}
        or _optional_text(raw.get("expected_next_page_type"))
        != H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE
        or not {"settings_entry", "profile_local_action", "page_type:settings"}.issubset(
            keywords
        )
        or not ({"evidence:class", "evidence:attributes", "evidence:structure"} & keywords)
    ):
        return None
    return {
        "target_mode": H5_PROFILE_SETTINGS_TARGET_MODE,
        "settings_entry_capability": capability,
        "source_page_type": H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE,
        "expected_page_type": H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE,
    }


def _profile_payment_navigation_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """只给检测器签发的确定性会员入口投影结构能力。"""
    keywords = set(_text_tuple(raw.get("target_keywords")))
    capability = h5_profile_payment_capability_from_keywords(keywords)
    entry_label = h5_profile_payment_entry_label(capability or "")
    if (
        operation is not InteractionOperation.CLICK
        or target != H5_PROFILE_PAYMENT_TARGET
        or capability is None
        or risk_text != "low"
        or (source_page_type or "").casefold()
        != H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE
        or source_page_state.casefold() not in {"stable", "normal", "loaded"}
        or _optional_text(raw.get("expected_next_page_type"))
        != H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE
        or raw.get("navigation_depth") != 1
        or raw.get("purpose") != f"探索我的页会员支付入口：{entry_label}"
        or not {
            "profile_vip_payment_entry",
            "profile_local_action",
            "page_type:payment",
            f"entry:{entry_label}",
            "evidence:structure",
        }.issubset(keywords)
    ):
        return None
    return {
        "target_mode": H5_PROFILE_PAYMENT_TARGET_MODE,
        "payment_entry_capability": capability,
        "source_page_type": H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE,
        "expected_page_type": H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE,
    }


def _payment_horizontal_collection_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, object] | None:
    """把检测器签发的会员横向集合收敛为只读有界参数。"""
    normalized_type = (source_page_type or "").strip().casefold()
    normalized_state = source_page_state.strip().casefold()
    keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        operation is not InteractionOperation.SCROLL
        or target != H5_PAYMENT_HORIZONTAL_TARGET
        or risk_text != "low"
        or not h5_payment_horizontal_context_supported(
            page_type=normalized_type,
            page_state=normalized_state,
        )
        or raw.get("navigation_depth") != 0
        or raw.get("purpose") != h5_payment_horizontal_purpose(normalized_type)
        or not set(h5_payment_horizontal_keywords(normalized_type)).issubset(keywords)
    ):
        return None
    return h5_payment_horizontal_action_parameters(source_page_type=normalized_type)


def _settings_page_back_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """只把设置页强结构返回候选投影为页面内返回能力。"""
    keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        operation is not InteractionOperation.BACK
        or target != "browser_history"
        or risk_text != "low"
        or (source_page_type or "").casefold() != H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE
        or source_page_state.casefold() not in {"stable", "normal", "loaded"}
        or raw.get("navigation_depth") != 1
        or not {
            "nested_return",
            H5_SETTINGS_PAGE_BACK_CAPABILITY,
            "evidence:structure",
        }.issubset(keywords)
    ):
        return None
    return {
        "target_mode": H5_SETTINGS_PAGE_BACK_TARGET_MODE,
        "settings_page_back_capability": H5_SETTINGS_PAGE_BACK_CAPABILITY,
        "source_page_type": H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE,
    }


def _profile_favorites_page_back_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """只投影“我的收藏”全屏页强结构返回能力。"""
    keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        operation is not InteractionOperation.BACK
        or target != "browser_history"
        or risk_text != "low"
        or not h5_profile_favorites_page_back_source_type_allowed(source_page_type)
        or source_page_state.casefold()
        not in {"stable", "normal", "loaded", "stable_empty", "empty_state", "empty"}
        or raw.get("navigation_depth") != 1
        or _optional_text(raw.get("expected_next_page_type"))
        != H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE
        or not {
            "nested_return",
            H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY,
            "evidence:structure",
        }.issubset(keywords)
    ):
        return None
    return {
        "target_mode": H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE,
        "profile_favorites_page_back_capability": (
            H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY
        ),
        "source_page_type": str(source_page_type).strip().casefold(),
        "expected_page_type": H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE,
    }


def _startup_ad_dismissal_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
) -> dict[str, str] | None:
    """仅把确定性开屏关闭语义投影为固定平台能力。"""
    capability = _optional_text(raw.get("obstruction_capability"))
    signature = _optional_text(raw.get("obstruction_signature"))
    if (
        source_page_type is None
        or source_page_type.casefold() != "startup_ad"
        or operation is not InteractionOperation.CLICK
        or target not in _STARTUP_AD_DISMISSAL_TARGETS
        or risk_text != "low"
        or _optional_text(raw.get("expected_next_page_type")) != "home"
        or capability != "startup_ad_dismissal"
        or signature is None
        or _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature) is None
    ):
        return None
    target_keywords = set(_text_tuple(raw.get("target_keywords")))
    if not {"startup_ad", "close_control", capability}.issubset(target_keywords):
        return None
    return {
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
        "obstruction_signature": signature,
    }


def _trusted_popup_obstruction(
    value: object,
    *,
    page_state: str,
) -> tuple[str, str] | None:
    raw_items = value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()
    matches: list[tuple[str, str]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        try:
            operation = InteractionOperation(str(raw.get("action") or "").casefold())
        except ValueError:
            continue
        parameters = _popup_dismissal_action_parameters(
            raw=raw,
            operation=operation,
            target=_optional_text(raw.get("target")) or "",
            risk_text=(_optional_text(raw.get("risk_level")) or "high").casefold(),
            source_page_state=page_state,
        )
        if parameters is not None:
            matches.append(
                (
                    parameters["popup_dismissal_capability"],
                    parameters["obstruction_signature"],
                )
            )
    return matches[0] if len(matches) == 1 else None


def _popup_dismissal_projection_rejections(
    value: object,
    *,
    source_page_state: str,
) -> tuple[dict[str, str], ...]:
    """记录声明了弹窗能力却未通过完整投影合同的候选。"""
    raw_items = value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()
    rejections: list[dict[str, str]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        target = _optional_text(raw.get("target")) or ""
        target_keywords = set(_text_tuple(raw.get("target_keywords")))
        declares_popup_capability = bool(
            target == _POPUP_DISMISSAL_TARGET
            and (
                _optional_text(raw.get("obstruction_capability")) is not None
                or "popup_dismissal" in target_keywords
            )
        )
        if not declares_popup_capability:
            continue
        try:
            operation = InteractionOperation(str(raw.get("action") or "").casefold())
        except ValueError:
            operation = InteractionOperation.OBSERVE
        parameters = _popup_dismissal_action_parameters(
            raw=raw,
            operation=operation,
            target=target,
            risk_text=(_optional_text(raw.get("risk_level")) or "high").casefold(),
            source_page_state=source_page_state,
        )
        if parameters is None:
            rejections.append(
                {
                    "target_mode": "popup_dismissal",
                    "semantic_target": _POPUP_DISMISSAL_TARGET,
                    "reason_code": "popup_capability_contract_invalid",
                }
            )
    return tuple(rejections)


def _popup_dismissal_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_state: str,
) -> dict[str, str] | None:
    """仅把检测器基于可信 DOM 签发的关闭候选投影为平台能力。"""
    capability = _optional_text(raw.get("obstruction_capability"))
    signature = _optional_text(raw.get("obstruction_signature"))
    target_keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        not h5_page_state_supports_popup_dismissal(source_page_state)
        or operation is not InteractionOperation.CLICK
        or target != _POPUP_DISMISSAL_TARGET
        or risk_text != "low"
        or raw.get("navigation_depth") != 0
        or capability not in _POPUP_DISMISSAL_CAPABILITIES
        or signature is None
        or _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature) is None
        or not {"popup_dismissal", "close_control", capability}.issubset(target_keywords)
    ):
        return None
    return {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": capability,
        "obstruction_signature": signature,
    }


def _candidate_navigation_knowledge_metadata(
    raw: Mapping[str, object],
) -> dict[str, object]:
    """只透传页面识别器签发的固定知识来源与有界置信度。"""
    source = _optional_text(raw.get("navigation_knowledge_source"))
    raw_confidence = raw.get("navigation_knowledge_confidence")
    if (
        source not in {"static", "exact", "vector", "contextual"}
        or isinstance(raw_confidence, bool)
        or not isinstance(raw_confidence, int | float)
    ):
        return {}
    confidence = float(raw_confidence)
    if not 0 <= confidence <= 1:
        return {}
    return {
        "navigation_knowledge_source": source,
        "navigation_knowledge_confidence": confidence,
    }


def _candidate_interaction_pattern_metadata(
    raw: Mapping[str, object],
) -> dict[str, object]:
    """透传已由现场 Snapshot 生成的模式身份，供结果反馈闭环使用。"""
    signature = _optional_text(raw.get("interaction_pattern_signature"))
    status = _optional_text(raw.get("interaction_pattern_status"))
    if (
        signature is None
        or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        or status not in {"candidate", "shadow", "trusted", "quarantined", "deprecated"}
    ):
        return {}
    return {
        "interaction_pattern_signature": signature,
        "interaction_pattern_status": status,
    }


def _interaction_pattern_exploration_value(
    *,
    base_value: int,
    status: object,
) -> int:
    """知识只调整调度顺序，不签发候选，也不突破 Frontier 预算。"""
    if status == "trusted":
        return max(base_value, 100)
    if status == "shadow":
        return max(base_value, 95)
    if status == "quarantined":
        return min(base_value, 25)
    if status == "deprecated":
        return min(base_value, 10)
    return base_value


def _structural_navigation_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
    independent_navigation: bool = False,
) -> dict[str, object] | None:
    """仅为检测器确定性生成的 H5 Tab selector 签发平台执行能力。"""
    if (
        source_page_type is None
        or source_page_type.casefold() in {"line_loading", "startup_ad"}
        or (h5_page_state_has_overlay(source_page_state) and not independent_navigation)
        or operation is not InteractionOperation.CLICK
        or risk_text != "low"
        or raw.get("navigation_depth") != 1
    ):
        return None
    source = _optional_text(raw.get("navigation_knowledge_source"))
    confidence = raw.get("navigation_knowledge_confidence")
    if (
        source not in {"static", "exact", "vector", "contextual"}
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0 <= float(confidence) <= 1
    ):
        return None
    target_keywords = _text_tuple(raw.get("target_keywords"))
    bottom_match = h5_match_bottom_navigation_target(target)
    if bottom_match is not None and "bottom_navigation" in target_keywords:
        family, navigation_index = bottom_match
        navigation_label = next(
            (
                item
                for item in target_keywords
                if item != "bottom_navigation"
            ),
            None,
        )
        if navigation_label is None:
            return None
        pattern_signature = _optional_text(raw.get("interaction_pattern_signature"))
        pattern_status = _optional_text(raw.get("interaction_pattern_status"))
        if family == H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE and (
            pattern_signature is None
            or re.fullmatch(r"[0-9a-f]{64}", pattern_signature) is None
            or pattern_status
            not in {"candidate", "shadow", "trusted", "quarantined", "deprecated"}
        ):
            return None
        return h5_bottom_navigation_action_parameters(
            family=family,
            index=navigation_index,
            label=navigation_label,
            interaction_pattern_signature=(
                pattern_signature if family == H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE else None
            ),
        )
    expected_page_type = _optional_text(raw.get("expected_next_page_type"))
    if expected_page_type is None:
        return None
    matched = _PAGE_TAB_NAVIGATION_TARGET_PATTERN.fullmatch(target)
    if matched is not None and "page_tab_navigation" in target_keywords:
        navigation_label = next(
            (item for item in target_keywords if item != "page_tab_navigation"),
            None,
        )
        if navigation_label is None:
            return None
        parameters: dict[str, object] = {
            "target_mode": "page_tab_navigation",
            "navigation_index": int(matched.group("index")),
            "navigation_label": navigation_label,
        }
        pattern_signature = _optional_text(raw.get("interaction_pattern_signature"))
        pattern_status = _optional_text(raw.get("interaction_pattern_status"))
        if pattern_signature is not None or pattern_status is not None:
            if (
                pattern_signature is None
                or re.fullmatch(r"[0-9a-f]{64}", pattern_signature) is None
                or pattern_status
                not in {"candidate", "shadow", "trusted", "quarantined", "deprecated"}
            ):
                return None
            parameters["interaction_pattern_signature"] = pattern_signature
        return parameters
    return None


def _detail_scroll_coverage_action_parameters(
    *,
    raw: Mapping[str, Any],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, Any] | None:
    """把检测器签发的详情滚动收敛为固定两屏采集合同。"""
    normalized_type = (source_page_type or "").strip().casefold()
    keywords = {item.casefold() for item in _text_tuple(raw.get("target_keywords"))}
    if (
        normalized_type not in {"content_detail", "video_detail"}
        or source_page_state.strip().casefold() not in {"stable", "normal", "loaded"}
        or operation is not InteractionOperation.SCROLL
        or risk_text != "low"
        or target != "采集详情页第二屏内容"
        or not {
            "detail_scroll_coverage",
            "two_screen_sequence",
            f"page_type:{normalized_type}",
        }.issubset(keywords)
    ):
        return None
    return {
        "target_mode": "h5_detail_scroll_coverage",
        "scroll_max_segments": 2,
        "split_scroll_screenshots": True,
        "source_page_type": normalized_type,
    }


def _list_scroll_coverage_action_parameters(
    *,
    raw: Mapping[str, Any],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, Any] | None:
    normalized_type = (source_page_type or "").strip().casefold()
    keywords = {item.casefold() for item in _text_tuple(raw.get("target_keywords"))}
    if (
        normalized_type not in {"home", "content_list", "post_list", "video_list"}
        or source_page_state.strip().casefold() not in {"stable", "normal", "loaded"}
        or operation is not InteractionOperation.SCROLL
        or risk_text != "low"
        or target != "采集列表至底部"
        or not {"list_scroll_coverage", "bounded_to_bottom"}.issubset(keywords)
    ):
        return None
    return {
        "target_mode": "h5_list_scroll_coverage",
        "scroll_max_segments": 8,
        "split_scroll_screenshots": True,
        "source_page_type": normalized_type,
    }


def _video_timed_capture_action_parameters(
    *,
    raw: Mapping[str, Any],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, Any] | None:
    keywords = {item.casefold() for item in _text_tuple(raw.get("target_keywords"))}
    if (
        (source_page_type or "").strip().casefold() != "video_detail"
        or source_page_state.strip().casefold() not in {"stable", "normal", "loaded"}
        or operation is not InteractionOperation.TIMED_CAPTURE
        or risk_text != "low"
        or target != "采集视频播放多帧"
        or "video_timed_capture" not in keywords
    ):
        return None
    return {"target_mode": "h5_video_timed_capture", "frame_count": 3, "interval_ms": 1000}


def _line_loading_observe_candidate(
    *,
    source_state_version: str,
    navigation_depth: int,
    reobserve_count: int,
) -> CandidateAction | None:
    """线路加载页只生成同会话重观察，并在第三次后停止。"""
    if reobserve_count >= _LINE_LOADING_REOBSERVE_MAX:
        return None
    attempt = reobserve_count + 1
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _LINE_LOADING_REOBSERVE_TARGET,
            "line_loading_reobserve_attempt": attempt,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_LINE_LOADING_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待线路选择完成，并在当前浏览器会话重新采集页面。",
            "target_keywords": ["line_loading", "same_session_reobserve"],
            "line_loading_reobserve_attempt": attempt,
            "action_parameters": {
                "target_mode": "line_loading_reobserve",
                "line_loading_reobserve_attempt": attempt,
            },
        },
    )


def _has_trusted_startup_ad_dismissal_candidate(
    candidates: tuple[CandidateAction, ...],
) -> bool:
    """仅让已投影为平台固定能力的开屏或覆盖层关闭动作优先于重观察。"""
    return any(
        candidate.action_type is InteractionOperation.CLICK
        and isinstance(candidate.metadata.get("action_parameters"), Mapping)
        and candidate.metadata["action_parameters"].get("target_mode")
        in {"startup_ad_dismissal", "popup_dismissal"}
        for candidate in candidates
    )


def _has_trusted_popup_dismissal_candidate(
    candidates: tuple[CandidateAction, ...],
) -> bool:
    """判断 Frontier 是否仍保有完整的弹窗关闭能力参数。"""
    return any(
        candidate.action_type is InteractionOperation.CLICK
        and isinstance(candidate.metadata.get("action_parameters"), Mapping)
        and candidate.metadata["action_parameters"].get("target_mode")
        == "popup_dismissal"
        for candidate in candidates
    )


def _is_untrusted_popup_dismissal_candidate(candidate: CandidateAction) -> bool:
    """识别已声明弹窗语义但缺少完整平台能力参数的候选。"""
    keywords = set(_text_tuple(candidate.metadata.get("target_keywords")))
    parameters = candidate.metadata.get("action_parameters")
    return bool(
        candidate.semantic_target == _POPUP_DISMISSAL_TARGET
        and "popup_dismissal" in keywords
        and not (
            isinstance(parameters, Mapping)
            and parameters.get("target_mode") == "popup_dismissal"
        )
    )


def _startup_ad_countdown_observe_candidate(
    *,
    source_state_version: str,
    navigation_depth: int,
    reobserve_count: int,
) -> CandidateAction | None:
    """开屏倒计时无可信关闭控件时，只在同一会话有限重观察。"""
    if reobserve_count >= _STARTUP_AD_COUNTDOWN_REOBSERVE_MAX:
        return None
    attempt = reobserve_count + 1
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _STARTUP_AD_COUNTDOWN_REOBSERVE_TARGET,
            "startup_ad_countdown_reobserve_attempt": attempt,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_STARTUP_AD_COUNTDOWN_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待启动广告倒计时结束，并在当前浏览器会话重新采集页面。",
            "target_keywords": [
                "startup_ad",
                "countdown",
                "same_session_reobserve",
            ],
            "startup_ad_countdown_reobserve_attempt": attempt,
            "action_parameters": {
                "target_mode": "startup_ad_countdown_reobserve",
                "startup_ad_countdown_reobserve_attempt": attempt,
            },
        },
    )


def _flutter_bootstrap_observe_candidate(
    *,
    source_state_version: str,
    navigation_depth: int,
    reobserve_count: int,
) -> CandidateAction | None:
    """空 Flutter 启动壳只允许同会话有限重观察。"""
    if reobserve_count >= _FLUTTER_BOOTSTRAP_REOBSERVE_MAX:
        return None
    attempt = reobserve_count + 1
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _FLUTTER_BOOTSTRAP_REOBSERVE_TARGET,
            "flutter_bootstrap_reobserve_attempt": attempt,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_FLUTTER_BOOTSTRAP_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待 Flutter 启动完成，并在当前浏览器会话重新采集页面。",
            "target_keywords": ["flutter_bootstrap", "same_session_reobserve"],
            "flutter_bootstrap_reobserve_attempt": attempt,
            "action_parameters": {
                "target_mode": "flutter_bootstrap_reobserve",
                "flutter_bootstrap_reobserve_attempt": attempt,
            },
        },
    )


def _insufficient_observe_candidate(
    *,
    source_state_version: str,
    navigation_depth: int,
    reobserve_count: int,
) -> CandidateAction | None:
    """普通未稳定页仅签发有界、同会话、无副作用的重观察。"""

    if reobserve_count >= _INSUFFICIENT_REOBSERVE_MAX:
        return None
    attempt = reobserve_count + 1
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _INSUFFICIENT_REOBSERVE_TARGET,
            "insufficient_reobserve_attempt": attempt,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_INSUFFICIENT_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待页面稳定，并在当前浏览器会话重新采集页面。",
            "target_keywords": ["insufficient", "same_session_reobserve"],
            "insufficient_reobserve_attempt": attempt,
            "action_parameters": {
                "target_mode": "insufficient_reobserve",
                "insufficient_reobserve_attempt": attempt,
            },
        },
    )


def _ambiguous_launch_shell_observe_candidate(
    *,
    source_state_version: str,
    navigation_depth: int,
    reobserve_count: int,
) -> CandidateAction | None:
    """生命周期页型仍冲突的启动壳层只允许限次零触碰复观察。"""
    if reobserve_count >= _AMBIGUOUS_LAUNCH_SHELL_REOBSERVE_MAX:
        return None
    attempt = reobserve_count + 1
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _AMBIGUOUS_LAUNCH_SHELL_REOBSERVE_TARGET,
            "ambiguous_launch_shell_reobserve_attempt": attempt,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_AMBIGUOUS_LAUNCH_SHELL_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "启动壳层页型仍有冲突，在当前浏览器会话零触碰重新采集。",
            "target_keywords": [
                "ambiguous_launch_shell",
                "same_session_reobserve",
                "zero_touch",
            ],
            "ambiguous_launch_shell_reobserve_attempt": attempt,
            "action_parameters": {
                "target_mode": "ambiguous_launch_shell_reobserve",
                "ambiguous_launch_shell_reobserve_attempt": attempt,
            },
        },
    )


def _is_ambiguous_launch_shell_detection(
    detection_payload: Mapping[str, Any],
) -> bool:
    """只认精确 Splash 冲突或确定性 NProgress busy 空启动壳。"""
    metrics = detection_payload.get("metrics")
    matched_signals = detection_payload.get("matched_catalog_signals")
    dom_hints = (
        {
            str(item).strip().casefold()
            for item in matched_signals.get("dom_hints", [])
            if str(item).strip()
        }
        if isinstance(matched_signals, Mapping)
        else set()
    )
    raw_candidates = detection_payload.get("page_type_candidates")
    lifecycle_candidates = {
        _optional_text(item.get("page_type")).casefold()
        for item in raw_candidates
        if isinstance(item, Mapping) and _optional_text(item.get("page_type")) is not None
    } if isinstance(raw_candidates, list) else set()
    raw_actions = detection_payload.get("candidate_actions")
    has_nprogress_busy_empty_launch_shell = bool(
        isinstance(metrics, Mapping)
        and metrics.get("nprogress_busy_empty_launch_shell_structure") is True
    )
    has_ambiguous_splash = bool(
        "splash" in dom_hints
        and {"line_loading", "startup_ad"}.issubset(lifecycle_candidates)
    )
    return bool(
        detection_payload.get("detection_status") in {"ambiguous", "insufficient"}
        and detection_payload.get("page_type") is None
        and str(detection_payload.get("page_state") or "").casefold() == "loading"
        and isinstance(metrics, Mapping)
        and metrics.get("page_settle_complete") is True
        and metrics.get("h5_business_structure_present") is False
        and (has_ambiguous_splash or has_nprogress_busy_empty_launch_shell)
        and isinstance(raw_actions, list)
        and not raw_actions
    )


def _logical_surface_reobserve_candidate(
    *,
    source_state_version: str,
    navigation_depth: int,
    reobserve_count: int,
    source_identity_fingerprint: str,
    source_state_fingerprint: str,
) -> CandidateAction | None:
    """逻辑转换证明不充分时，只允许两次同会话无副作用复验。"""
    if reobserve_count >= _LOGICAL_REOBSERVE_MAX:
        return None
    attempt = reobserve_count + 1
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "source_identity_fingerprint": source_identity_fingerprint,
            "source_state_fingerprint": source_state_fingerprint,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _LOGICAL_REOBSERVE_TARGET,
            "logical_reobserve_attempt": attempt,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_LOGICAL_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "复验 SPA/Flutter 逻辑页面转换",
            "target_keywords": ["logical_surface", "same_session_reobserve"],
            "logical_reobserve_attempt": attempt,
            "source_identity_fingerprint": source_identity_fingerprint,
            "source_state_fingerprint": source_state_fingerprint,
            "action_parameters": {
                "target_mode": "logical_surface_reobserve",
                "logical_reobserve_attempt": attempt,
            },
        },
    )


def _capability_projection_reobserve_candidate(
    *,
    source_state_version: str,
    navigation_depth: int,
    reobserve_count: int,
) -> CandidateAction | None:
    """能力合同拒绝后只允许有限次无副作用同会话重观察。"""
    if reobserve_count >= _CAPABILITY_PROJECTION_REOBSERVE_MAX:
        return None
    attempt = reobserve_count + 1
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _CAPABILITY_PROJECTION_REOBSERVE_TARGET,
            "capability_projection_reobserve_attempt": attempt,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_CAPABILITY_PROJECTION_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "能力合同未完整投影，等待页面稳定后在当前会话重新采集。",
            "target_keywords": [
                "capability_projection_rejected",
                "same_session_reobserve",
            ],
            "capability_projection_reobserve_attempt": attempt,
            "action_parameters": {
                "target_mode": "capability_projection_reobserve",
                "capability_projection_reobserve_attempt": attempt,
            },
        },
    )


def _popup_absence_observe_candidate(
    *,
    source_capture_id: str,
    source_state_version: str,
    navigation_depth: int,
    obstruction_signature: str | None = None,
) -> CandidateAction:
    """未找到可信目标只能触发重新核对，不能证明弹窗已退场。"""
    candidate_id = "h5-candidate:" + stable_protocol_hash(
        {
            "source_capture_id": source_capture_id,
            "source_state_version": source_state_version,
            "action": InteractionOperation.OBSERVE.value,
            "semantic_target": _POPUP_ABSENCE_REOBSERVE_TARGET,
            "obstruction_signature": obstruction_signature,
        }
    )
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=_POPUP_ABSENCE_REOBSERVE_TARGET,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "关闭目标暂不可用，在当前会话重新采集并核对弹窗是否仍在。",
            "target_keywords": [
                "popup_dismissal",
                "obstruction_state_unknown",
                "same_session_reobserve",
            ],
            "action_reobservation_capture_id": source_capture_id,
            "popup_absence_reobserve_capture_id": source_capture_id,
            **(
                {"popup_absence_obstruction_signature": obstruction_signature}
                if obstruction_signature is not None
                else {}
            ),
        },
    )


def _popup_dismissal_reobserve_signature(
    lineage_metadata: Mapping[str, Any] | None,
) -> str | None:
    """读取失败关闭动作绑定的签名，供一次性无副作用观察链传递。"""
    if not isinstance(lineage_metadata, Mapping):
        return None
    direct = _optional_text(lineage_metadata.get("popup_absence_obstruction_signature"))
    if direct is not None and _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(direct):
        return direct
    state = lineage_metadata.get("popup_dismissal_reobserve_state")
    if isinstance(state, Mapping):
        signature = _optional_text(state.get("signature"))
        if signature is not None and _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature):
            return signature
    return None


def _same_popup_dismissal_signature(
    candidate: CandidateAction,
    *,
    completed_popup_signature: str | None,
) -> bool:
    """仅抑制一次性观察确认不存在的同一弹层，兼容旧无签名快照。"""
    parameters = candidate.metadata.get("action_parameters")
    if not (
        isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "popup_dismissal"
    ):
        return False
    if completed_popup_signature is None:
        # 旧版本只持久化完成标志，没有签名。为避免恢复旧 Work 时重试已证伪
        # 的关闭动作，沿用旧的 fail-closed 行为；新链路会按签名精确过滤。
        return True
    return _candidate_popup_obstruction_signature(candidate) == completed_popup_signature


def _candidate_popup_obstruction_signature(candidate: CandidateAction) -> str | None:
    """读取候选的可信遮挡签名，兼容旧载荷的顶层/参数两种位置。"""
    direct = _optional_text(candidate.metadata.get("obstruction_signature"))
    if direct is not None and _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(direct):
        return direct
    parameters = candidate.metadata.get("action_parameters")
    if isinstance(parameters, Mapping):
        signature = _optional_text(parameters.get("obstruction_signature"))
        if signature is not None and _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature):
            return signature
    return None


def _action_reobservation_capture_id(
    *,
    lineage_metadata: Mapping[str, Any] | None,
    capture_id: str,
) -> str | None:
    """仅信任 Workflow 本地签发且绑定当前 Capture 的重观察 occurrence。"""
    if not isinstance(lineage_metadata, Mapping):
        return None
    audit = lineage_metadata.get("h5_action_reobservation")
    if (
        not isinstance(audit, Mapping)
        or audit.get("schema_version") != "h5-action-reobservation.v1"
        or audit.get("before_capture_id") != capture_id
    ):
        return None
    return capture_id


def _action_reobservation_candidate_identity(
    *,
    lineage_metadata: Mapping[str, Any] | None,
) -> tuple[InteractionOperation, str, str | None] | None:
    """读取 Workflow 签发的失败候选身份，避免重观察复活同页兄弟入口。"""
    if not isinstance(lineage_metadata, Mapping):
        return None
    raw = lineage_metadata.get("action_reobservation_candidate_identity")
    if not isinstance(raw, Mapping):
        return None
    action_text = _optional_text(raw.get("action_type"))
    semantic_target = _optional_text(raw.get("semantic_target"))
    expected_raw = raw.get("expected_next_page_type")
    expected_page_type = _optional_text(expected_raw)
    if (
        action_text is None
        or semantic_target is None
        or (expected_raw is not None and expected_page_type is None)
    ):
        return None
    try:
        action_type = InteractionOperation(action_text)
    except ValueError:
        return None
    return action_type, semantic_target, expected_page_type


def _candidate_reobservation_identity(
    candidate: CandidateAction,
) -> tuple[InteractionOperation, str, str | None]:
    expected_page_type = _optional_text(
        candidate.metadata.get("expected_next_page_type")
    )
    return candidate.action_type, candidate.semantic_target, expected_page_type


def _list_detail_navigation_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_page_type: str | None,
    source_page_state: str,
) -> dict[str, str] | None:
    """只为检测器签发的列表首卡能力保存结构 selector。"""
    normalized_source = (source_page_type or "").casefold()
    expected_page_type = h5_list_detail_expected_page_type(normalized_source)
    selector = _optional_text(raw.get("list_detail_selector"))
    keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        expected_page_type is None
        or source_page_state.casefold() not in {"stable", "normal", "feed", "loaded", "notice"}
        or operation is not InteractionOperation.CLICK
        or target != H5_LIST_DETAIL_TARGET
        or risk_text != "low"
        or raw.get("navigation_depth") != 2
        or _optional_text(raw.get("expected_next_page_type")) != expected_page_type
        or selector is None
        or not h5_list_detail_selector_allowed(
            source_page_type=normalized_source,
            selector=selector,
        )
        or not {"nested_content", "list_detail_navigation"}.issubset(keywords)
    ):
        return None
    return {
        "target_mode": "list_detail_navigation",
        "list_detail_selector": selector,
        "source_page_type": normalized_source,
        "expected_page_type": expected_page_type,
    }


def _visual_navigation_action_parameters(
    *,
    raw: Mapping[str, Any],
    trusted_capability: object,
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    expected_page_type: str | None,
    source_capture_id: str,
    source_page_type: str | None,
    source_identity_fingerprint: str | None,
    source_state_fingerprint: str | None,
    source_task_id: str | None,
    trusted_source_context: Mapping[str, object] | None,
    logical_targets_effective: bool,
) -> dict[str, object] | None:
    """只投影检测器持久化后仍完整验签并匹配当前状态的视觉能力。"""
    raw_capability = raw.get("visual_navigation_capability")
    if (
        not logical_targets_effective
        or not isinstance(trusted_source_context, Mapping)
        or not isinstance(raw_capability, Mapping)
        or not isinstance(trusted_capability, Mapping)
        or dict(raw_capability) != dict(trusted_capability)
    ):
        return None
    try:
        capability = verify_visual_navigation_capability(trusted_capability)
    except ValueError:
        return None
    expected_risk = VISUAL_TARGET_RISK_LEVELS.get(capability.target_kind)
    if (
        operation is not InteractionOperation.CLICK
        or target != f"进入 {capability.expected_page_type} 页面"
        or expected_page_type != capability.expected_page_type
        or risk_text != expected_risk
        or source_task_id is None
        or trusted_source_context.get("task_id") != source_task_id
        or trusted_source_context.get("capture_id") != source_capture_id
        or trusted_source_context.get("page_type") != source_page_type
        or trusted_source_context.get("identity_fingerprint")
        != source_identity_fingerprint
        or trusted_source_context.get("state_fingerprint")
        != source_state_fingerprint
        or capability.source_task_id != source_task_id
        or capability.source_capture_id != source_capture_id
        or capability.source_page_type != source_page_type
        or source_identity_fingerprint is None
        or capability.source_identity_fingerprint != source_identity_fingerprint
        or source_state_fingerprint is None
        or capability.source_state_fingerprint != source_state_fingerprint
    ):
        return None
    return {
        "target_mode": "visual_navigation_coordinate",
        "visual_navigation_capability": capability.to_dict(),
    }


def _profile_visual_action_parameters(
    *,
    raw: Mapping[str, object],
    operation: InteractionOperation,
    target: str,
    risk_text: str,
    source_capture_id: str,
    source_page_url: str,
    source_page_type: str | None,
) -> dict[str, object] | None:
    """把已验证的“我的”页白名单截图入口投影为受控执行参数。"""
    if source_page_type is None or source_page_type.casefold() != "profile":
        return None
    if operation is not InteractionOperation.CLICK:
        return None
    contract = h5_profile_visual_entry_contract(target)
    keywords = set(_text_tuple(raw.get("target_keywords")))
    if (
        contract is None
        or risk_text != "medium"
        or _optional_text(raw.get("expected_next_page_type"))
        != contract.expected_page_type
        or not {
            contract.target_keyword,
            "profile_local_action",
            f"page_type:{contract.expected_page_type}",
            f"entry:{contract.target}",
            "evidence:screenshot",
        }.issubset(keywords)
    ):
        return None
    source_url = urlsplit(source_page_url)
    if source_url.scheme.casefold() not in {"http", "https"} or not source_url.netloc:
        return None

    raw_bounds = raw.get("visual_bounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    screenshot_index = raw_bounds.get("screenshot_index")
    if (
        isinstance(screenshot_index, bool)
        or not isinstance(screenshot_index, int)
        or screenshot_index < 1
    ):
        return None
    artifact_id = _optional_text(raw_bounds.get("artifact_id"))
    if artifact_id is None or len(artifact_id) > 256:
        return None
    visual_sha256 = _optional_text(raw_bounds.get("sha256"))
    if visual_sha256 is None or not visual_sha256.startswith("sha256:"):
        return None
    visual_digest = visual_sha256.removeprefix("sha256:")
    if len(visual_digest) != 64 or any(
        character not in "0123456789abcdef" for character in visual_digest.casefold()
    ):
        return None
    visual_sha256 = f"sha256:{visual_digest.casefold()}"
    if raw_bounds.get("frame_index") is not None:
        return None
    left_ratio = _visual_ratio(raw_bounds.get("left_ratio"))
    top_ratio = _visual_ratio(raw_bounds.get("top_ratio"))
    right_ratio = _visual_ratio(raw_bounds.get("right_ratio"))
    bottom_ratio = _visual_ratio(raw_bounds.get("bottom_ratio"))
    confidence = _visual_ratio(raw_bounds.get("confidence"))
    if None in {left_ratio, top_ratio, right_ratio, bottom_ratio, confidence}:
        return None
    assert left_ratio is not None
    assert top_ratio is not None
    assert right_ratio is not None
    assert bottom_ratio is not None
    assert confidence is not None
    if left_ratio >= right_ratio or top_ratio >= bottom_ratio:
        return None
    if confidence < contract.minimum_confidence:
        return None
    area_ratio = (right_ratio - left_ratio) * (bottom_ratio - top_ratio)
    if area_ratio > contract.maximum_area_ratio:
        return None
    return {
        "target_mode": "visual_coordinate",
        "visual_entry_kind": contract.entry_kind,
        "visual_source_capture_id": source_capture_id,
        "visual_source_url": source_page_url,
        "visual_source_artifact_id": artifact_id,
        "visual_source_sha256": visual_sha256,
        "screenshot_index": screenshot_index,
        "left_ratio": left_ratio,
        "top_ratio": top_ratio,
        "right_ratio": right_ratio,
        "bottom_ratio": bottom_ratio,
        "visual_confidence": confidence,
    }


def _visual_ratio(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        return None
    return number


def _seek_fraction(value: object, *, operation: InteractionOperation) -> float | None:
    if operation is not InteractionOperation.SEEK_MEDIA:
        return None
    if value is None:
        return 0.5
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    fraction = float(value)
    return fraction if 0.05 <= fraction <= 0.95 else 0.5


def _key_elements(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    values: list[str] = []
    for key in ("ocr_keywords", "dom_hints", "layout_hints"):
        for item in _text_tuple(value.get(key)):
            if item not in values:
                values.append(item)
    return tuple(values)


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    values: list[str] = []
    for item in value:
        text = _optional_text(item)
        if text is not None and text not in values:
            values.append(text)
    return tuple(values)


def _unique_text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    values = _text_tuple(value)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise TypeError(f"{field_name} 必须为字符串序列")
    if len(values) != len(value):
        raise ValueError(f"{field_name} 不能包含空值、非字符串或重复值")
    return values


def _required_text(value: object, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} 必须为非空字符串")
    return text


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _bounded_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _positive_int_or_default(value: object, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _path_within_route_prefix(path: str, prefix: str) -> bool:
    normalized_prefix = "/" + "/".join(part for part in prefix.split("/") if part)
    if normalized_prefix == "/":
        return True
    return path == normalized_prefix or path.startswith(normalized_prefix + "/")
