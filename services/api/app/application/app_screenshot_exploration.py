"""从 APP_READY 构造并发布公共 Screenshot Exploration Work。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from services.api.app.application.app_action_observation import (
    APP_ACTION_OBSERVATION_KEY,
    build_app_action_observation,
    confirmed_app_identity,
)
from services.api.app.application.app_capture_readiness_diagnosis import (
    SettleReadinessProfile,
    build_app_capture_readiness_diagnosis,
    build_app_page_stability_assessment,
    build_m1_settle_audit_bundle,
    build_m1_settle_enforcement_preview,
    build_settle_shadow_manifest_audit,
    classify_app_entry_layer,
)
from services.api.app.application.app_command_broker import (
    attach_app_adb_broker_shadow,
    driver_observation_from_analysis_facts,
)
from services.api.app.application.app_coverage_page_seek import (
    _COVERAGE_OVERLAY_STATES,
    _COVERAGE_SEEK_BLOCKING_STATES,
    CLEARANCE_WITHOUT_BUSINESS_PROGRESS_COUNT,
    DYNAMIC_BUSINESS_TAB_OVERLAY_FRAME_COUNT,
    DYNAMIC_HOME_POPUP_FRAME_COUNT,
    HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY,
    HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY,
    HOME_TOP_SAMPLE_PHASE_NEED_BACK,
    HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE,
    HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
    VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
    VISITED_HOME_TOP_CATEGORY_TABS_KEY,
    _append_bottom_navigation_tab_seek_candidate,
    _append_coverage_page_seek_candidate,
    _append_home_category_grid_seek_candidate,
    _append_home_top_category_tab_seek_candidate,
    align_visual_page_type_after_coverage_seek,
    begin_home_top_category_sample,
    complete_home_top_category_sample,
    coverage_incomplete_allows_bottom_nav_through_overlay,
    extract_selected_bottom_tab_labels,
    home_top_category_sample_in_progress,
    home_top_category_sample_label,
    home_top_category_sample_phase,
    home_top_category_seek_selection_matches,
    is_bottom_navigation_tab_candidate,
    overlay_budget_allows_real_coverage_seek,
    record_visited_bottom_navigation_label,
    record_visited_home_top_category_tab,
    resolve_selected_home_top_category_label,
    set_home_top_category_sample_phase,
)
from services.api.app.application.app_directed_discovery import (
    APP_DIRECTED_DISCOVERY_ATTEMPTS_KEY,
    HARD_APPLICABILITY_PAGE_TYPES,
    directed_discovery_attempts_from_metadata,
    guide_page_markers_present,
    hard_targets_missing_entry_on_source,
    merge_blocked_page_types,
    record_capability_blocked_attempts,
    record_entry_seen_attempts,
    record_guide_page_attempt,
    record_no_entry_attempts,
    record_seek_attempt,
)
from services.api.app.application.app_exploration_flow_contract import (
    APP_FLOW_CONTRACT_KEY,
    update_app_flow_contract,
)
from services.api.app.application.app_exploration_grayscale import (
    attach_app_m5_grayscale_shadow,
    build_composite_action_rebind_contract,
    evaluate_external_bypass,
)
from services.api.app.application.app_frontier_quantity_contract import (
    page_limit_blocks_new_page_candidates,
    policy_is_quantity_unbounded,
)
from services.api.app.application.app_frontier_restoration import (
    APP_FRONTIER_RESTORE_ATTEMPTS_KEY,
    with_incremented_app_frontier_restore_attempt,
)
from services.api.app.application.app_gate_retirement_shadow import (
    attach_app_m7_gate_retirement_shadow,
)
from services.api.app.application.app_graph_recapture import (
    attach_app_m6_recapture_shadow,
)
from services.api.app.application.app_interaction_plan import (
    AppScrollRebindClickPlan,
    build_scroll_rebind_click_plan,
    mark_plan_after_scroll,
    mark_plan_rebind_failed,
)
from services.api.app.application.app_lab_composition import (
    build_app_lab_composition_audit,
    resolve_app_lab_composition,
    resolve_effective_app_milestone_policy,
)
from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    DISCOVERY_SOURCE_UIA_ISSUED,
    DISCOVERY_SOURCE_VISION,
    tag_candidate_action_provenance,
)
from services.api.app.application.app_obstruction_controls import (
    build_app_obstruction_identity,
    build_visual_obstruction_control_identity,
    is_obstructed_surface,
    is_visual_obstruction_control,
)
from services.api.app.application.app_package_boundary import (
    ExternalActivityKind,
    app_location_within_package_boundary,
    classify_external_activity,
    is_android_launcher_package,
)
from services.api.app.application.app_page_type_assessment_adapter import (
    AppPageTypeAssessmentAdapterResult,
    _page_type_confidence,
    adapt_app_visual_analysis_to_assessment,
)
from services.api.app.application.app_popup_ad_scope import belongs_to_topmost_ad_popup
from services.api.app.application.app_runtime_capability_assessment import (
    capability_shadow_from_coverage_inputs,
)
from services.api.app.application.app_screenshot_exploration_phase import (
    next_bounded_dismiss_counters,
    next_popup_phase_counters,
    next_startup_ad_phase_counters,
    next_surface_phase_observation_counters,
)
from services.api.app.application.app_screenshot_understanding import (
    AppScreenshotCaptureContext,
    AppScreenshotUnderstandingError,
)
from services.api.app.application.app_script_curriculum import (
    APP_CURRICULUM_ACTION_VERSION,
    APP_CURRICULUM_OBSERVATION_VERSION,
    AppCurriculumProgress,
    build_script_curriculum_phases,
    candidate_explicitly_targets_tap_skip,
    curriculum_action_matches_current_phase,
    resolve_playbook_by_id,
)
from services.api.app.application.app_semantic_progress import (
    APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS,
    APP_COVERAGE_SEEK_FAILURE_SCORES,
    APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS,
    APP_DYNAMIC_PAGE_SAMPLING_VERSION,
    APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT,
    APP_DYNAMIC_STARTUP_OBSERVATION_VERSION,
    APP_DYNAMIC_UNSTABLE_PAGE_BACK_LIMIT,
    APP_DYNAMIC_UNSTABLE_PAGE_BACK_VERSION,
    APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_LIMIT,
    APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_VERSION,
    APP_SEMANTIC_ACTION_COUNTS,
    PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY,
    PROFILE_CREDENTIAL_SAVE_DONE_KEY,
    SHORT_DRAMA_END_SEEN_KEY,
    SHORT_DRAMA_EXIT_PENDING_KEY,
    SHORT_DRAMA_PLAYBACK_OBSERVE_COUNT_KEY,
    SHORT_DRAMA_PLAYBACK_STARTED_AT_KEY,
    SHORT_DRAMA_PLAYER_PAGE_TYPES,
    SHORT_DRAMA_TOP_LEFT_BACK_ATTEMPT_KEY,
    SHORT_DRAMA_TOP_LEFT_BACK_BEFORE_TAB_ADVANCE,
    _normalize_bottom_tab_label_for_visit,
    anwang_membership_landing_detected,
    bottom_tab_label_already_visited,
    build_app_semantic_action_key,
    is_anwang_bottom_tab_label,
    is_short_drama_bottom_tab_label,
    parse_app_semantic_action_counts,
    parse_bottom_nav_label_attempt_counts,
    record_app_coverage_seek_outcome,
    record_app_coverage_seek_target_attempt,
    record_app_semantic_action,
    record_bottom_nav_label_seek_attempt,
    short_drama_end_seen,
    short_drama_exit_pending,
    short_drama_playback_end_detected,
    short_drama_playback_observe_count,
    short_drama_playback_progress_near_end,
    short_drama_playback_started_at,
    short_drama_top_left_back_attempt_count,
    should_suppress_redundant_app_candidate,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    default_popup_close_bounds,
    default_startup_ad_skip_bounds,
    popup_close_corner_contract,
    profile_credential_save_contract,
    startup_ad_skip_contract,
)
from services.api.app.application.app_visual_content_fingerprint import (
    CONTENT_FINGERPRINT_REUSE_REASON,
    content_fingerprint_value,
    content_fingerprints_match,
)
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationScheduler,
    ExplorationQueue,
    evidence_gap_page_types_from_facts,
    update_scheduler_metadata,
)
from services.api.app.application.exploration_scheduler_facts import (
    ExplorationSchedulerFactsReader,
    merge_exploration_scheduler_facts,
)
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.orchestration import StartExplorationCommand
from services.api.app.application.page_graph_projection import (
    attach_app_coverage_page_graph_shadow,
)
from services.api.app.application.surface_sessions import AppExplorationPendingFacts
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    AppVisualAnalysis,
    AppVisualCandidate,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationWork,
    FrontierSnapshot,
    NormalizedBounds,
    SurfaceObservation,
    build_action_fingerprint,
    consume_exploration_action_budget,
    copy_exploration_budget_after_observation,
)
from services.api.app.domain.exploration.age_confirm import (
    is_age_confirm_candidate,
    observation_needs_age_confirm,
)
from services.api.app.domain.exploration.app_page_identity import (
    APP_BUSINESS_PAGE_IDENTITY_VERSION,
    build_app_business_page_group,
)
from services.api.app.domain.exploration.app_visual_page_identity import (
    build_app_visual_page_facts,
    validated_app_visual_page_identity,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    AppReadyHandoff,
    CaptureKind,
    CaptureRef,
    InteractionExecutionOutcome,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import (
    require_aware_datetime,
    stable_protocol_hash,
    thaw_json_mapping,
)

_LOGGER = logging.getLogger(__name__)

_STABLE_BUSINESS_BYPASS_VERSION = "app-curriculum-stable-business-bypass.v1"
_HLW_WHITE_PACKAGE = "uk.gvycg.bjdiey"
_HLW_WHITE_OPTIONAL_STARTUP_PHASES = frozenset({"tap_skip", "wait_init", "close_popup", "shenqi"})
_PAGE_TYPE_REGISTRY = get_default_page_type_registry()
_BOUNDED_SCHEDULER = BoundedExplorationScheduler()
_DYNAMIC_STARTUP_OBSERVATION_COUNT = "dynamic_startup_observation_count"
_DYNAMIC_STARTUP_RECOVERY_ACTION_COUNT = "dynamic_startup_recovery_action_count"
_DYNAMIC_STARTUP_RECOVERY_TARGETS = "dynamic_startup_recovery_targets"
_DYNAMIC_STARTUP_RECOVERY_ACTION_LIMIT = 3
_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_COUNT = "dynamic_unstable_page_reobserve_count"
_DYNAMIC_UNSTABLE_PAGE_BACK_COUNT = "dynamic_unstable_page_back_count"
_DYNAMIC_HOME_POPUP_FRAME_COUNT = DYNAMIC_HOME_POPUP_FRAME_COUNT
# 首页弹层连续清障很快就会 BACK 退 App；前几帧仍关层，达阈值后让真实寻页穿透
_DYNAMIC_BUSINESS_TAB_OVERLAY_FRAME_COUNT = DYNAMIC_BUSINESS_TAB_OVERLAY_FRAME_COUNT
_DYNAMIC_BUSINESS_TAB_OVERLAY_PAGES = frozenset(
    {
        "content_list",
        "video_list",
        "post_list",
        "activity",
        "profile",
    }
)
_DYNAMIC_NO_NEW_BUSINESS_PAGE_ACTION_COUNT = "dynamic_no_new_business_page_action_count"
_DYNAMIC_REPEATED_BUSINESS_PAGE_ACTION_COUNT = "dynamic_repeated_business_page_action_count"
_DYNAMIC_POPUP_EXTERNAL_REDIRECT_COUNT = "dynamic_popup_external_redirect_count"
_DYNAMIC_POPUP_BACK_RECOVERY_COUNT = "dynamic_popup_back_recovery_count"
_DYNAMIC_POPUP_BACK_RECOVERY_LIMIT = 2
_EXTERNAL_ACTIVITY_BOUNCE_COUNT = "external_activity_bounce_count"
_EXTERNAL_RETURN_FAILURE_COUNT = "external_return_failure_count"
_EXTERNAL_ACTIVITY_KIND = "external_activity_kind"
_EXTERNAL_PACKAGE_NAME = "external_package_name"
_APP_AD_LANDING_ATTEMPT_COUNT = "app_ad_landing_attempt_count"
_DISMISS_NOOP_STREAK = "dismiss_noop_streak"
_DISMISS_NOOP_YIELD_THRESHOLD = 2
_CLEARANCE_WITHOUT_BUSINESS_PROGRESS_COUNT = CLEARANCE_WITHOUT_BUSINESS_PROGRESS_COUNT
_HOME_DETAIL_BOUNCE_COUNT = "home_detail_bounce_count"
_POST_DETAIL_EXIT_PREFER_TABS = "post_detail_exit_prefer_tabs"
_DETAIL_LIKE_PAGE_TYPES = frozenset(
    {
        "content_detail",
        "video_detail",
        "post_detail",
        "payment",
    }
)
_ROOT_BUSINESS_PAGE_TYPES = frozenset(
    {
        "home",
        "video_list",
        "content_list",
        "post_list",
        "activity",
        "profile",
    }
)
_APP_AD_LANDING_CONTRACT_VERSION = "app-ad-landing-consistency.v1"
_COVERED_BUSINESS_EVIDENCE_IDS = "covered_business_evidence_ids"
_COVERED_BUSINESS_PAGES = "covered_business_pages"
_SAFE_STARTUP_EXIT_TARGETS = frozenset(
    {
        "enter app",
        "skip",
        "进入",
        "进入应用",
        "跳过",
        "跳过广告",
        "跳过启动广告",
    }
)
_SAFE_STARTUP_EXIT_HINTS = frozenset({"app_ready", "home", "tap_skip"})
_SAFE_STARTUP_EXIT_PURPOSE_MARKERS = (
    "dismiss startup ad",
    "enter app",
    "skip startup ad",
    "关闭启动广告",
    "进入主界面",
    "进入应用",
    "跳过开屏广告",
    "跳过启动广告",
    "开屏退出",
    "可访问性开屏退出",
)


class AppScreenshotUnderstandingPort(Protocol):
    """工作流所需的最小截图理解能力。"""

    async def understand(self, capture) -> AppVisualAnalysis: ...

    async def resolve_capture_image_signature(self, capture: CaptureRef) -> str: ...

    async def resolve_capture_visual_context(
        self,
        capture: CaptureRef,
    ) -> AppScreenshotCaptureContext: ...


class SavedAppVisualObservationPort(Protocol):
    """视觉事实仓储返回的只读投影。"""

    observation_id: str
    analysis: AppVisualAnalysis
    observed_at: datetime
    created: bool


class AppVisualObservationRepositoryPort(Protocol):
    """按 Capture 幂等读取和保存视觉事实。"""

    async def get_by_capture_id(
        self,
        *,
        capture_id: str,
    ) -> SavedAppVisualObservationPort | None: ...

    async def save(self, analysis: AppVisualAnalysis) -> SavedAppVisualObservationPort: ...


def _image_signature_prefix(image_signature: str, *, length: int = 20) -> str:
    text = str(image_signature or "")
    return text[:length] if len(text) > length else text


def rebind_app_visual_analysis_to_capture(
    *,
    source: AppVisualAnalysis,
    capture: CaptureRef,
    image_signature: str,
    reuse_reason: str,
    elapsed_ms: int = 0,
    capture_context: AppScreenshotCaptureContext | None = None,
) -> AppVisualAnalysis:
    """按已验证事实复用视觉输出，并重建目标 Capture 的身份绑定。"""

    if not isinstance(source, AppVisualAnalysis):
        raise TypeError("source 必须为 AppVisualAnalysis")
    if not isinstance(capture, CaptureRef):
        raise TypeError("capture 必须为 CaptureRef")
    if image_signature != source.image_signature:
        allow_forced_splash = reuse_reason in {
            "splash_body_observe_after",
            "splash_skip_before_reuse",
        }
        if allow_forced_splash:
            # 开屏动画帧签名会漂移；产品脚本两帧策略仍复用前帧语义到新 Capture。
            pass
        elif (
            reuse_reason != CONTENT_FINGERPRINT_REUSE_REASON
            or capture_context is None
            or not content_fingerprints_match(capture_context, source)
        ):
            raise ValueError("复用视觉分析要求 image_signature 完全一致")
    source_identity = validated_app_visual_page_identity(
        source.surface_facts.get("app_page_identity"),
        capture_id=source.capture_id,
        screenshot_artifact_id=source.screenshot_artifact_id,
        image_signature=source.image_signature,
        package_name=source.package_name,
        activity=source.activity,
    )
    if (
        source.surface_facts.get("app_page_identity") is not None
        and source_identity is None
    ):
        raise ValueError("来源视觉身份未通过当前 Capture 校验")
    if source_identity is not None and capture_context is None:
        raise ValueError("带业务身份的视觉复用必须提供已验证 Capture 上下文")
    if capture_context is not None:
        if not isinstance(capture_context, AppScreenshotCaptureContext):
            raise TypeError("capture_context 必须为 AppScreenshotCaptureContext")
        if capture_context.capture != capture:
            raise ValueError("视觉复用上下文与目标 Capture 不一致")
        if capture_context.image_signature != image_signature:
            raise ValueError("视觉复用上下文 image_signature 不一致")
        if capture_context.screenshot_artifact_id not in capture.artifact_ids:
            raise ValueError("视觉复用上下文 Screenshot Artifact 不属于目标 Capture")
        if (
            capture_context.width != source.width
            or capture_context.height != source.height
            or capture_context.orientation != source.orientation
            or capture_context.package_name != source.package_name
            or capture_context.activity != source.activity
        ):
            raise ValueError("目标 Capture 视觉上下文与来源不一致")
        screenshot_artifact_id = capture_context.screenshot_artifact_id
        target_width = capture_context.width
        target_height = capture_context.height
        target_orientation = capture_context.orientation
        target_package_name = capture_context.package_name
        target_activity = capture_context.activity
    else:
        if not capture.artifact_ids:
            raise ValueError("Capture 缺少 screenshot artifact")
        screenshot_artifact_id = capture.artifact_ids[0]
        target_width = source.width
        target_height = source.height
        target_orientation = source.orientation
        target_package_name = source.package_name
        target_activity = source.activity
    rebound_candidates = tuple(
        AppVisualCandidate.build(
            source_capture_id=capture.capture_id,
            source_image_signature=image_signature,
            source_width=target_width,
            source_height=target_height,
            source_orientation=target_orientation,
            semantic_name=item.semantic_name,
            action_type=item.action_type,
            normalized_bounds=item.normalized_bounds,
            confidence=item.confidence,
            risk_level=item.risk_level,
            purpose=item.purpose,
            control_role=item.control_role,
            expected_state_hint=item.expected_state_hint,
            frontier_scope=item.frontier_scope,
        )
        for item in source.candidates
    )
    surface_facts: dict[str, Any] = dict(source.surface_facts)
    if source_identity is not None:
        source_local_state = surface_facts.get("app_local_state_observation")
        local_state_observation = (
            source_local_state.get("anchors")
            if isinstance(source_local_state, Mapping)
            else None
        )
        for key in (
            "app_page_identity",
            "app_page_identity_assessment",
            "local_state",
            "app_local_state_observation",
        ):
            surface_facts.pop(key, None)
        surface_facts.update(
            build_app_visual_page_facts(
                identity_observation=source_identity.get("observation"),
                local_state_observation=local_state_observation,
                capture_id=capture.capture_id,
                screenshot_artifact_id=screenshot_artifact_id,
                image_signature=image_signature,
                package_name=target_package_name,
                activity=target_activity,
                page_state=source.page_state,
                overlay_signature=source.overlay_signature,
                surface_facts=surface_facts,
            )
        )
    surface_facts["vision_reuse"] = {
        "reason": reuse_reason,
        "source_capture_id": source.capture_id,
        "source_screenshot_artifact_id": source.screenshot_artifact_id,
        "source_image_signature": source.image_signature,
        "target_screenshot_artifact_id": screenshot_artifact_id,
    }
    if capture_context is not None and capture_context.content_fingerprint:
        existing_fp = (
            dict(source.surface_facts["content_fingerprint"])
            if isinstance(source.surface_facts.get("content_fingerprint"), Mapping)
            else {}
        )
        surface_facts["content_fingerprint"] = {
            **existing_fp,
            "value": capture_context.content_fingerprint,
        }
    surface_facts["visual_latency"] = {
        "elapsed_ms": int(elapsed_ms),
        "reused": True,
        "capture_id": capture.capture_id,
        "image_signature_prefix": _image_signature_prefix(image_signature),
    }
    return AppVisualAnalysis.build(
        capture_id=capture.capture_id,
        screenshot_artifact_id=screenshot_artifact_id,
        image_signature=image_signature,
        width=target_width,
        height=target_height,
        orientation=target_orientation,
        visual_page_type=source.visual_page_type,
        visual_summary=source.visual_summary,
        key_elements=source.key_elements,
        candidates=rebound_candidates,
        provider_id=source.provider_id,
        model_id=source.model_id,
        package_name=target_package_name,
        activity=target_activity,
        page_state=source.page_state,
        overlay_signature=source.overlay_signature,
        surface_facts=surface_facts,
    )


async def ensure_app_capture_visual_observation(
    *,
    capture: CaptureRef,
    understanding: AppScreenshotUnderstandingPort,
    observations: AppVisualObservationRepositoryPort,
    mismatch_message: str,
    analysis_enricher: Callable[[AppVisualAnalysis], AppVisualAnalysis] | None = None,
    reuse_from: AppVisualAnalysis | None = None,
    reuse_reason: str | None = None,
) -> SavedAppVisualObservationPort:
    """幂等补齐 Capture 视觉事实，并验证 Capture 与 Screenshot 身份。"""

    saved = await observations.get_by_capture_id(capture_id=capture.capture_id)
    capture_context: AppScreenshotCaptureContext | None = None
    if saved is None:
        analysis: AppVisualAnalysis | None = None
        if reuse_from is not None and reuse_reason:
            started = time.perf_counter()
            source_identity = validated_app_visual_page_identity(
                reuse_from.surface_facts.get("app_page_identity"),
                capture_id=reuse_from.capture_id,
                screenshot_artifact_id=reuse_from.screenshot_artifact_id,
                image_signature=reuse_from.image_signature,
                package_name=reuse_from.package_name,
                activity=reuse_from.activity,
            )
            source_identity_invalid = (
                reuse_from.surface_facts.get("app_page_identity") is not None
                and source_identity is None
            )
            context_reader = getattr(understanding, "resolve_capture_visual_context", None)
            if callable(context_reader):
                try:
                    candidate_context = await context_reader(capture)
                except AppScreenshotUnderstandingError:
                    # 真实 Capture 的归属、hash 或尺寸校验失败是硬拒绝；
                    # 不得退回宽松的旧 understand 路径。
                    raise
                except Exception as exc:  # noqa: BLE001 - 能力读取失败时回到 understand
                    _LOGGER.warning(
                        "App 视觉复用上下文读取失败，改为重新理解 capture_id=%s error=%s",
                        capture.capture_id,
                        type(exc).__name__,
                    )
                else:
                    if isinstance(candidate_context, AppScreenshotCaptureContext):
                        capture_context = candidate_context
                        reuse_match_reason = _visual_reuse_reason(
                            context=candidate_context,
                            reuse_from=reuse_from,
                            source_identity_invalid=source_identity_invalid,
                            requested_reason=reuse_reason,
                        )
                        if reuse_match_reason is not None:
                            elapsed_ms = int((time.perf_counter() - started) * 1000)
                            analysis = rebind_app_visual_analysis_to_capture(
                                source=reuse_from,
                                capture=capture,
                                image_signature=candidate_context.image_signature,
                                reuse_reason=reuse_match_reason,
                                elapsed_ms=elapsed_ms,
                                capture_context=candidate_context,
                            )
                            _LOGGER.info(
                                "App 视觉复用 capture_id=%s reason=%s source_capture_id=%s "
                                "elapsed_ms=%s signature_prefix=%s",
                                capture.capture_id,
                                reuse_match_reason,
                                reuse_from.capture_id,
                                elapsed_ms,
                                _image_signature_prefix(candidate_context.image_signature),
                            )
                        else:
                            _LOGGER.warning(
                                "App 视觉复用上下文与来源不一致，改为重新理解 capture_id=%s",
                                capture.capture_id,
                            )
                    else:
                        capture_context = None
                        _LOGGER.warning(
                            "App 视觉复用上下文类型无效，改为重新理解 capture_id=%s",
                            capture.capture_id,
                        )
            elif source_identity is None and not source_identity_invalid:
                # 兼容尚未产出新身份事实的 Fake Understanding；真实服务实现了
                # resolve_capture_visual_context，因此不会绕过正式 Capture 校验。
                signature_reader = getattr(
                    understanding,
                    "resolve_capture_image_signature",
                    None,
                )
                if callable(signature_reader):
                    try:
                        capture_signature = await signature_reader(capture)
                    except Exception as exc:  # noqa: BLE001 - 失败时重新理解
                        _LOGGER.warning(
                            "App 旧视觉复用签名读取失败，改为重新理解 capture_id=%s error=%s",
                            capture.capture_id,
                            type(exc).__name__,
                        )
                    else:
                        allow_forced_splash = (
                            reuse_reason in _FORCED_SPLASH_VISUAL_REUSE_REASONS
                        )
                        if (
                            capture_signature == reuse_from.image_signature
                            or allow_forced_splash
                        ):
                            elapsed_ms = int((time.perf_counter() - started) * 1000)
                            analysis = rebind_app_visual_analysis_to_capture(
                                source=reuse_from,
                                capture=capture,
                                image_signature=capture_signature,
                                reuse_reason=reuse_reason,
                                elapsed_ms=elapsed_ms,
                            )
                            _LOGGER.info(
                                "App 视觉复用 capture_id=%s reason=%s source_capture_id=%s "
                                "elapsed_ms=%s signature_prefix=%s",
                                capture.capture_id,
                                reuse_reason,
                                reuse_from.capture_id,
                                elapsed_ms,
                                _image_signature_prefix(capture_signature),
                            )
        if analysis is None:
            understand_from_context = getattr(understanding, "understand_from_context", None)
            if (
                callable(understand_from_context)
                and capture_context is not None
                and capture_context.screenshot_png
            ):
                analysis = await understand_from_context(capture_context)
            else:
                analysis = await understanding.understand(capture)
        if analysis_enricher is not None:
            analysis = analysis_enricher(analysis)
        _validate_app_visual_capture_binding(
            analysis, capture=capture, context=capture_context, message=mismatch_message
        )
        saved = await observations.save(analysis)
    _validate_app_visual_capture_binding(
        saved.analysis, capture=capture, context=capture_context, message=mismatch_message
    )
    return saved


# 开屏两帧证据：本体 OBSERVE 的 after、以及跳过／关闭的 before，允许跨动画帧强制复用。
_FORCED_SPLASH_VISUAL_REUSE_REASONS = frozenset(
    {
        "splash_body_observe_after",
        "splash_skip_before_reuse",
    }
)


def _visual_reuse_reason(
    *,
    context: AppScreenshotCaptureContext,
    reuse_from: AppVisualAnalysis,
    source_identity_invalid: bool,
    requested_reason: str | None,
) -> str | None:
    """全图签名一致，或仅状态栏变化的业务内容指纹一致时才复用。"""

    if source_identity_invalid:
        return None
    if (
        context.width != reuse_from.width
        or context.height != reuse_from.height
        or context.orientation != reuse_from.orientation
        or context.package_name != reuse_from.package_name
        or context.activity != reuse_from.activity
    ):
        return None
    if context.image_signature == reuse_from.image_signature:
        return requested_reason or "same_image_signature"
    if content_fingerprints_match(context, reuse_from):
        return CONTENT_FINGERPRINT_REUSE_REASON
    # 开屏动画会改像素签名；产品脚本开屏两帧策略仍复用 Surface 身份一致的前帧语义。
    if requested_reason in _FORCED_SPLASH_VISUAL_REUSE_REASONS:
        return requested_reason
    return None


def _validate_app_visual_capture_binding(
    analysis: AppVisualAnalysis,
    *,
    capture: CaptureRef,
    context: AppScreenshotCaptureContext | None,
    message: str,
) -> None:
    """写入前先验证绑定，读回再验证，禁止先保存错误事实再报错。"""

    if (
        not isinstance(analysis, AppVisualAnalysis)
        or analysis.capture_id != capture.capture_id
        or analysis.screenshot_artifact_id not in capture.artifact_ids
    ):
        raise ValueError(message)
    if context is not None and (
        analysis.screenshot_artifact_id != context.screenshot_artifact_id
        or analysis.image_signature != context.image_signature
        or analysis.width != context.width
        or analysis.height != context.height
        or analysis.orientation != context.orientation
        or analysis.package_name != context.package_name
        or analysis.activity != context.activity
    ):
        raise ValueError(message)


class AppExplorationOrchestratorPort(Protocol):
    """首次发布 Work，后继只准备并交由父 Work 事务提交。"""

    async def start_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork: ...

    async def prepare_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork: ...


class AppExplorationPendingFactsReader(Protocol):
    """读取 App 探索进行期的 PostgreSQL 待处理事实。"""

    async def exploration_pending_facts(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> AppExplorationPendingFacts: ...


class AppBeforeActionVisualUnderstandingError(RuntimeError):
    """before Capture 视觉事实不可用，禁止触控或继续派生后继 Work。"""

    error_code = "APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED"
    retryable = False

    def __init__(self, *, capture_id: str) -> None:
        super().__init__(f"App 动作前 Capture 视觉事实不可用：{capture_id}")
        self.capture_id = capture_id


@dataclass(frozen=True, kw_only=True)
class AppScreenshotExplorationCommand:
    """APP_READY 到首次 Exploration Work 的锁定上下文。"""

    handoff: AppReadyHandoff
    objective: str
    boundaries: ExplorationBoundaries
    budget: ExplorationBudget
    observed_at: datetime
    deadline: datetime
    app_goal: AppExplorationGoal
    curriculum_progress: AppCurriculumProgress | None = None
    runtime_policy_summary: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, AppReadyHandoff):
            raise TypeError("handoff 必须为 AppReadyHandoff")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("objective 必须为非空字符串")
        if self.objective != self.objective.strip():
            raise ValueError("objective 不能包含首尾空白")
        if not isinstance(self.boundaries, ExplorationBoundaries):
            raise TypeError("boundaries 必须为 ExplorationBoundaries")
        if not isinstance(self.budget, ExplorationBudget):
            raise TypeError("budget 必须为 ExplorationBudget")
        if not isinstance(self.app_goal, AppExplorationGoal):
            raise TypeError("app_goal 必须为 AppExplorationGoal")
        if self.curriculum_progress is not None and not isinstance(
            self.curriculum_progress,
            AppCurriculumProgress,
        ):
            raise TypeError("curriculum_progress 必须为 AppCurriculumProgress")
        if self.runtime_policy_summary is not None and not isinstance(
            self.runtime_policy_summary,
            Mapping,
        ):
            raise TypeError("runtime_policy_summary 必须为 Mapping")
        require_aware_datetime(self.observed_at, "observed_at")
        require_aware_datetime(self.deadline, "deadline")
        if self.deadline <= self.observed_at:
            raise ValueError("deadline 必须晚于 observed_at")


@dataclass(frozen=True, kw_only=True)
class ContinueAppScreenshotExplorationCommand:
    """动作后截图到后继 Exploration Work 的锁定上下文。"""

    prior_work: ExplorationWork
    turn: ExplorerTurnResult

    def __post_init__(self) -> None:
        if not isinstance(self.prior_work, ExplorationWork):
            raise TypeError("prior_work 必须为 ExplorationWork")
        if not isinstance(self.turn, ExplorerTurnResult):
            raise TypeError("turn 必须为 ExplorerTurnResult")
        _validate_continuation_identity(work=self.prior_work, turn=self.turn)


@dataclass(frozen=True, kw_only=True)
class ContinueAppScreenshotObservationCommand:
    """重新观察 Capture 到后继 Exploration Work 的锁定上下文。"""

    prior_work: ExplorationWork
    turn: ExplorerTurnResult

    def __post_init__(self) -> None:
        if not isinstance(self.prior_work, ExplorationWork):
            raise TypeError("prior_work 必须为 ExplorationWork")
        if not isinstance(self.turn, ExplorerTurnResult):
            raise TypeError("turn 必须为 ExplorerTurnResult")
        _validate_observation_continuation_identity(
            work=self.prior_work,
            turn=self.turn,
        )


@dataclass(frozen=True, kw_only=True)
class AppScreenshotExplorationResult:
    """视觉事实与公共 Work 的引用型交接结果。"""

    observation_id: str
    visual_created: bool
    page_type_assessment: AppPageTypeAssessmentAdapterResult
    work: ExplorationWork


class AppScreenshotExplorationWorkflow:
    """幂等消费 APP_READY，不执行 Agent 决策或 Driver 动作。"""

    def __init__(
        self,
        *,
        understanding: AppScreenshotUnderstandingPort,
        observations: AppVisualObservationRepositoryPort,
        orchestrator: AppExplorationOrchestratorPort,
        pending_facts: AppExplorationPendingFactsReader | None = None,
        scheduler_facts_reader: ExplorationSchedulerFactsReader | None = None,
        flow_foundation_runtime: object | None = None,
    ) -> None:
        self._understanding = understanding
        self._observations = observations
        self._orchestrator = orchestrator
        self._pending_facts = pending_facts
        self._scheduler_facts_reader = scheduler_facts_reader
        self._flow_foundation_runtime = flow_foundation_runtime

    async def prepare_before_action_visual(
        self,
        *,
        capture: CaptureRef,
        source: AppVisualAnalysis,
        force_reuse_reason: str | None = None,
    ) -> SavedAppVisualObservationPort:
        """在触控前封存当前视觉事实；相同像素可复用语义但必须重绑 Capture。

        force_reuse_reason 仅用于开屏跳过／关闭：动画帧签名可能漂移，
        仍复用前一帧开屏本体语义，避免同一开屏再验一张 before。
        """

        reuse_reason = (
            force_reuse_reason
            if force_reuse_reason in _FORCED_SPLASH_VISUAL_REUSE_REASONS
            else "same_image_signature"
        )
        return await self._understand_capture(
            capture,
            mismatch_message="App 视觉事实与动作前 Capture 不一致",
            allowed_hosts=(source.package_name,) if source.package_name else (),
            surface_kind=capture.surface_kind,
            # Resolver 的 before 回调没有 Work 策略，只负责重绑当前真实候选；
            # 此处禁止为 paywall 补发返回动作，避免在动作执行前写入伪候选。
            dynamic_frontier=True,
            reuse_from=source,
            reuse_reason=reuse_reason,
        )

    async def run(
        self,
        command: AppScreenshotExplorationCommand,
    ) -> AppScreenshotExplorationResult:
        if not isinstance(command, AppScreenshotExplorationCommand):
            raise TypeError("command 必须为 AppScreenshotExplorationCommand")
        capture = command.handoff.capture
        dynamic_frontier = _is_dynamic_frontier_policy(command.runtime_policy_summary)
        saved = await self._understand_capture(
            capture,
            mismatch_message="App 视觉事实与 APP_READY Capture 不一致",
            allowed_hosts=command.boundaries.allowed_hosts,
            surface_kind=SurfaceKind.ANDROID,
            dynamic_frontier=dynamic_frontier,
        )
        analysis = saved.analysis
        pending_facts = await self._read_pending_facts(
            task_id=command.handoff.task_id,
            session_id=command.handoff.session_id,
        )
        scheduler_facts = await self._read_scheduler_facts(
            task_id=command.handoff.task_id,
            agent_run_id=command.handoff.agent_run_id,
            session_id=command.handoff.session_id,
        )
        scheduler_prior_metadata = merge_exploration_scheduler_facts(
            {},
            scheduler_facts,
            surface="app",
        )
        scheduler_evidence_gaps = _app_evidence_gap_page_types(
            scheduler_prior_metadata,
            known_page_types=_app_required_page_types(command.app_goal),
        )
        # 首 Work 的 observation.observed_at 必须等于 Runtime Checkpoint.started_at
        # （即 command.observed_at）；视觉理解实际时刻可晚于冻结点，只写入审计字段。
        next_coverage = await _maybe_attach_app_journey_plans(
            task_id=command.handoff.task_id,
            agent_run_id=command.handoff.agent_run_id,
            coverage_metadata=_next_coverage_metadata(
                goal=command.app_goal,
                analysis=analysis,
                prior_metadata=scheduler_prior_metadata,
                evidence_terms=("app_ready",),
                pending_facts=pending_facts,
                curriculum_progress=command.curriculum_progress,
                runtime_policy_summary=command.runtime_policy_summary,
                business_page_scope="app_ready",
                scheduler_max_actions=command.budget.max_actions,
                scheduler_max_duration_ms=command.budget.remaining_duration_ms,
                scheduler_deadline=command.deadline,
                scheduler_required_page_types=_app_required_page_types(command.app_goal),
                scheduler_evidence_gap_page_types=scheduler_evidence_gaps,
                scheduler_state_fingerprint=analysis.image_signature,
                scheduler_now=command.observed_at,
            ),
        )
        frontier = _build_frontier(
            analysis=analysis,
            observed_at=command.observed_at,
            navigation_depth=0,
            max_navigation_depth=min(
                command.budget.max_depth,
                command.boundaries.max_navigation_depth,
            ),
            allowed_hosts=command.boundaries.allowed_hosts,
            lineage_metadata={
                "agent_run_id": command.handoff.agent_run_id,
                "visual_observed_at": saved.observed_at.isoformat(),
            },
            coverage_metadata=merge_exploration_scheduler_facts(
                next_coverage,
                scheduler_facts,
                surface="app",
            ),
        )
        frontier = await self._attach_flow_foundation_trigger(
            frontier,
            task_id=command.handoff.task_id,
            session_id=command.handoff.session_id,
            agent_run_id=command.handoff.agent_run_id,
            parent_work_id=f"app-ready:{capture.capture_id}",
            parent_version=0,
            page_type=analysis.visual_page_type,
            subject_ref=confirmed_app_identity(analysis),
        )
        work = await self._orchestrator.start_exploration(
            StartExplorationCommand(
                task_id=command.handoff.task_id,
                session_id=command.handoff.session_id,
                branch_id=f"app:{capture.capture_id}",
                objective=command.objective,
                surface_kind=SurfaceKind.ANDROID,
                source_capture_id=capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
                boundaries=command.boundaries,
                budget=command.budget,
                deadline=command.deadline,
                app_goal=command.app_goal,
            )
        )
        if work.source_capture_id != capture.capture_id:
            raise ValueError("Orchestrator 返回了错误的 App Exploration Work")
        return AppScreenshotExplorationResult(
            observation_id=saved.observation_id,
            visual_created=saved.created,
            page_type_assessment=adapt_app_visual_analysis_to_assessment(
                task_id=command.handoff.task_id,
                agent_run_id=command.handoff.agent_run_id,
                analysis=analysis,
                assessed_at=saved.observed_at,
            ),
            work=work,
        )

    async def prepare_after_action(
        self,
        command: ContinueAppScreenshotExplorationCommand,
    ) -> AppScreenshotExplorationResult:
        """理解 after capture，并幂等准备下一条带完整谱系的 Work。"""
        if not isinstance(command, ContinueAppScreenshotExplorationCommand):
            raise TypeError("command 必须为 ContinueAppScreenshotExplorationCommand")
        prior_work = command.prior_work
        coordination = command.turn.coordination
        if coordination is None:
            raise ValueError("动作轮缺少 Capture Coordination 结果")
        if coordination.interaction.error_code == "APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED":
            raise AppBeforeActionVisualUnderstandingError(
                capture_id=coordination.before_capture.capture_id,
            )
        selected = _selected_candidate(prior_work=prior_work, turn=command.turn)
        dynamic_frontier = _is_dynamic_frontier_policy(
            prior_work.frontier.observation.metadata.get("app_runtime_policy")
        )
        prior_observation = await self._observations.get_by_capture_id(
            capture_id=prior_work.frontier.observation.capture_id,
        )
        prior_analysis = (
            prior_observation.analysis
            if prior_observation is not None
            and isinstance(prior_observation.analysis, AppVisualAnalysis)
            else None
        )
        before_reuse_from: AppVisualAnalysis | None = None
        before_reuse_reason: str | None = None
        splash_skip_after_only = _selected_splash_skip_after_only(
            selected=selected,
            turn=command.turn,
        )
        splash_body_evidence = _selected_splash_body_evidence(
            selected=selected,
            turn=command.turn,
        )
        if splash_skip_after_only and prior_analysis is not None:
            # 跳过／关闭开屏：before 不重新验视觉，复用开屏本体帧。
            before_reuse_from = prior_analysis
            before_reuse_reason = "splash_skip_before_reuse"
        elif (
            prior_analysis is not None
            and prior_analysis.image_signature == prior_work.frontier.observation.state_version
        ):
            before_reuse_from = prior_analysis
            before_reuse_reason = "same_image_signature"
        try:
            before_saved = await self._understand_capture(
                coordination.before_capture,
                mismatch_message="App 视觉事实与动作前 Capture 不一致",
                allowed_hosts=prior_work.boundaries.allowed_hosts,
                surface_kind=prior_work.surface_kind,
                dynamic_frontier=dynamic_frontier,
                reuse_from=before_reuse_from,
                reuse_reason=before_reuse_reason,
            )
        except Exception as exc:  # noqa: BLE001 - 统一转为不可重试的 fail-fast 终态
            # 动作前视觉失败禁止静默续跑后继：否则仍消耗动作预算，Gap 要到 Closure 才出现。
            _LOGGER.error(
                "App 动作前 Capture 视觉事实不可用，已 fail-closed capture_id=%s",
                coordination.before_capture.capture_id,
                exc_info=True,
            )
            raise AppBeforeActionVisualUnderstandingError(
                capture_id=coordination.before_capture.capture_id,
            ) from exc
        after_capture = coordination.after_capture
        after_reuse_from: AppVisualAnalysis | None = before_saved.analysis
        after_reuse_reason = "same_image_signature"
        if splash_body_evidence:
            # 开屏本体 OBSERVE：after 不单独立验，避免动画帧把同一广告验成两张。
            after_reuse_reason = "splash_body_observe_after"
        elif selected.action_type in {
            InteractionOperation.OBSERVE,
            InteractionOperation.TIMED_CAPTURE,
        }:
            after_reuse_reason = "observe_or_timed_capture"
        saved = await self._understand_capture(
            after_capture,
            mismatch_message="App 视觉事实与动作后 Capture 不一致",
            allowed_hosts=prior_work.boundaries.allowed_hosts,
            surface_kind=prior_work.surface_kind,
            dynamic_frontier=dynamic_frontier,
            reuse_from=after_reuse_from,
            reuse_reason=after_reuse_reason,
        )
        analysis = saved.analysis
        action_succeeded = coordination.interaction.status is InteractionResultStatus.SUCCEEDED
        pending_facts = await self._read_pending_facts(
            task_id=prior_work.task_id,
            session_id=prior_work.session_id,
        )
        scheduler_facts = await self._read_scheduler_facts(
            task_id=prior_work.task_id,
            agent_run_id=_agent_run_id(prior_work),
            session_id=prior_work.session_id,
            finalization_epoch=_scheduler_fact_epoch(
                prior_work.frontier.observation.metadata
            ),
        )
        scheduler_prior_metadata = merge_exploration_scheduler_facts(
            prior_work.frontier.observation.metadata,
            scheduler_facts,
            surface="app",
        )
        scheduler_evidence_gaps = _app_evidence_gap_page_types(
            scheduler_prior_metadata,
            known_page_types=_app_required_page_types(prior_work.app_goal),
        )
        state_changed = _relevant_visual_state_changed(
            prior_observation=prior_work.frontier.observation,
            next_analysis=analysis,
        )
        if action_succeeded:
            analysis = align_visual_page_type_after_coverage_seek(
                analysis,
                completed_action=selected,
                state_changed=state_changed,
                source_page_type=prior_work.frontier.observation.page_type,
            )
        action_observation = build_app_action_observation(
            work=prior_work,
            turn=command.turn,
            candidate=selected,
            before=before_saved.analysis,
            after=saved.analysis,
            popup_close=_is_popup_close_action(selected),
        )

        arrival_proven = action_observation.arrival_proven
        curriculum_progress = _next_curriculum_progress(
            prior_work=prior_work,
            selected=selected,
            turn=command.turn,
            # 课纲推进：业务到达／关层；底栏另认 tab_seek 截图留证。
            arrival_proven=arrival_proven,
            tab_seek_evidenced=action_observation.tab_seek_evidenced,
        )
        next_navigation_depth = _next_business_navigation_depth(
            prior_work=prior_work,
            selected=selected,
            turn=command.turn,
            next_analysis=analysis,
        )
        business_page_scope = _next_business_page_scope(
            prior_work=prior_work,
            selected=selected,
            turn=command.turn,
            next_navigation_depth=next_navigation_depth,
        )
        if not arrival_proven:
            # 意图里的导航层级不能提前成为已到达的业务路径。
            next_navigation_depth = _current_business_navigation_depth(prior_work)
            business_page_scope = _current_business_page_scope(prior_work)
        if action_observation.outcome == "surface_restored":
            # 外跳后只确认当前 App 重新可见，旧业务深度和路线不能作为原状态恢复证明。
            next_navigation_depth = 0
            business_page_scope = "app_ready"
        observed_at, observed_at_clock_meta = _scheduler_monotonic_observed_at(
            sample_observed_at=saved.observed_at,
            prior_observed_at=prior_work.frontier.observation.observed_at,
        )
        frontier = _build_frontier(
            analysis=analysis,
            observed_at=observed_at,
            surface_kind=prior_work.surface_kind,
            navigation_depth=next_navigation_depth,
            max_navigation_depth=min(
                prior_work.budget.max_depth,
                prior_work.boundaries.max_navigation_depth,
            ),
            allowed_hosts=prior_work.boundaries.allowed_hosts,
            visited_state_ids=_append_unique(
                prior_work.frontier.visited_state_ids,
                prior_work.frontier.observation.state_version,
            ),
            visited_candidate_ids=_append_unique(
                prior_work.frontier.visited_candidate_ids,
                selected.candidate_id,
            )
            if action_observation.completed
            else prior_work.frontier.visited_candidate_ids,
            visited_action_fingerprints=_append_unique(
                prior_work.frontier.visited_action_fingerprints,
                build_action_fingerprint(
                    state_version=prior_work.frontier.observation.state_version,
                    candidate=selected,
                ),
            )
            if action_observation.completed
            else prior_work.frontier.visited_action_fingerprints,
            repeated_state_count=_next_repeated_state_count(
                prior_work=prior_work,
                next_state_version=analysis.image_signature,
                next_analysis=analysis,
            ),
            lineage_metadata={
                "agent_run_id": _agent_run_id(prior_work),
                "parent_work_id": prior_work.work_id,
                "parent_branch_id": prior_work.branch_id,
                "parent_capture_id": prior_work.source_capture_id,
                "parent_candidate_id": selected.candidate_id,
                "before_capture_id": coordination.before_capture.capture_id,
                "action_execution_id": coordination.after_capture.action_execution_id,
                "visual_result_hash": saved.analysis.result_hash,
                APP_ACTION_OBSERVATION_KEY: action_observation.to_dict(),
                "app_verified_navigation_path": list(action_observation.navigation_path),
                **_surface_phase_lineage(
                    prior_work=prior_work,
                    selected=selected,
                    turn=command.turn,
                    next_analysis=analysis,
                ),
                **observed_at_clock_meta,
            },
            coverage_metadata=await _maybe_attach_app_journey_plans(
                task_id=prior_work.task_id,
                agent_run_id=_agent_run_id(prior_work),
                coverage_metadata=_next_coverage_metadata(
                goal=prior_work.app_goal,
                analysis=analysis,
                prior_metadata=scheduler_prior_metadata,
                prior_state_version=prior_work.frontier.observation.state_version,
                evidence_terms=(
                    _successful_action_coverage_terms(
                        selected=selected,
                        turn=command.turn,
                    ),
                ),
                pending_facts=pending_facts,
                curriculum_progress=curriculum_progress,
                business_navigation_depth=next_navigation_depth,
                business_page_scope=business_page_scope,
                completed_action=_product_script_completed_action_for_coverage(
                    selected=selected,
                    action_succeeded=action_succeeded,
                    observation_completed=action_observation.completed,
                    observation_reason=action_observation.reason,
                ),
                attempted_action=_coverage_attempted_action(
                    candidate=selected,
                    turn=command.turn,
                ),
                action_succeeded=action_succeeded,
                arrival_proven=arrival_proven,
                observation_proven=action_observation.observation_proven,
                action_observation_outcome=action_observation.outcome,
                count_completed_action_for_business_progress=(
                    _counts_toward_business_page_progress(
                        candidate=selected,
                        turn=command.turn,
                    )
                ),
                action_decision=(
                    command.turn.decision
                    if isinstance(command.turn.decision, ActionIntent)
                    else None
                ),
                scheduler_max_actions=prior_work.budget.max_actions,
                scheduler_max_duration_ms=prior_work.budget.remaining_duration_ms,
                scheduler_deadline=prior_work.deadline,
                scheduler_required_page_types=_app_required_page_types(prior_work.app_goal),
                scheduler_evidence_gap_page_types=scheduler_evidence_gaps,
                scheduler_state_fingerprint=analysis.image_signature,
                scheduler_now=observed_at,
                scheduler_elapsed_duration_ms=_elapsed_duration_milliseconds(
                    prior_work.frontier.observation.observed_at,
                    observed_at,
                ),
                ),
            ),
        )
        frontier = replace(
            frontier,
            observation=replace(
                frontier.observation,
                metadata=merge_exploration_scheduler_facts(
                    frontier.observation.metadata,
                    scheduler_facts,
                    surface="app",
                ),
            ),
        )
        elapsed_duration_ms = _elapsed_duration_milliseconds(
            prior_work.frontier.observation.observed_at,
            observed_at,
        )
        counts_toward_business = _counts_toward_business_page_progress(
            candidate=selected,
            turn=command.turn,
        )
        charges_business_budget = counts_toward_business or (
            selected.metadata.get("package_boundary_recovery") is True
        )
        obstruction_spent_before = (
            prior_work.budget.resolved_max_obstruction_actions
            - prior_work.budget.resolved_remaining_obstruction_actions
        )
        apply_floor = (
            counts_toward_business
            and obstruction_spent_before > 0
            and prior_work.budget.business_duration_floor_applied == 0
        )
        next_budget = consume_exploration_action_budget(
            prior_work.budget,
            # 包边界恢复不算业务页面进展，但必须在清障池耗尽后仍有有界回程；
            # 因此只在预算分账上使用业务池。
            counts_toward_business=charges_business_budget,
            elapsed_duration_ms=elapsed_duration_ms,
            apply_business_duration_floor=apply_floor,
        )
        work = await self._orchestrator.prepare_exploration(
            StartExplorationCommand(
                task_id=prior_work.task_id,
                session_id=prior_work.session_id,
                branch_id=f"app:{after_capture.capture_id}",
                objective=prior_work.objective,
                surface_kind=prior_work.surface_kind,
                source_capture_id=after_capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
                boundaries=prior_work.boundaries,
                budget=next_budget,
                deadline=prior_work.deadline,
                app_goal=prior_work.app_goal,
            )
        )
        if work.source_capture_id != after_capture.capture_id:
            raise ValueError("Orchestrator 返回了错误的 App 后继 Exploration Work")
        return AppScreenshotExplorationResult(
            observation_id=saved.observation_id,
            visual_created=saved.created,
            page_type_assessment=adapt_app_visual_analysis_to_assessment(
                task_id=prior_work.task_id,
                agent_run_id=_agent_run_id(prior_work),
                analysis=analysis,
                assessed_at=observed_at,
            ),
            work=work,
        )

    async def prepare_after_observation(
        self,
        command: ContinueAppScreenshotObservationCommand,
    ) -> AppScreenshotExplorationResult:
        """理解 Observation Capture，并在不消耗动作预算的前提下准备后继 Work。"""

        if not isinstance(command, ContinueAppScreenshotObservationCommand):
            raise TypeError("command 必须为 ContinueAppScreenshotObservationCommand")
        prior_work = command.prior_work
        capture = command.turn.observation_capture
        if capture is None or not isinstance(command.turn.decision, TimedObservationIntent):
            raise ValueError("重新观察轮缺少 TimedObservationIntent 或 Observation Capture")
        decision = command.turn.decision
        dynamic_frontier = _is_dynamic_frontier_policy(
            prior_work.frontier.observation.metadata.get("app_runtime_policy")
        )
        prior_observation = await self._observations.get_by_capture_id(
            capture_id=prior_work.frontier.observation.capture_id,
        )
        prior_analysis = (
            prior_observation.analysis
            if prior_observation is not None
            and isinstance(prior_observation.analysis, AppVisualAnalysis)
            and prior_observation.analysis.image_signature
            == prior_work.frontier.observation.state_version
            else None
        )
        saved = await self._understand_capture(
            capture,
            mismatch_message="App 视觉事实与重新观察 Capture 不一致",
            allowed_hosts=prior_work.boundaries.allowed_hosts,
            surface_kind=prior_work.surface_kind,
            dynamic_frontier=dynamic_frontier,
            reuse_from=prior_analysis,
            reuse_reason="same_image_signature",
        )
        pending_facts = await self._read_pending_facts(
            task_id=prior_work.task_id,
            session_id=prior_work.session_id,
        )
        scheduler_facts = await self._read_scheduler_facts(
            task_id=prior_work.task_id,
            agent_run_id=_agent_run_id(prior_work),
            session_id=prior_work.session_id,
            finalization_epoch=_scheduler_fact_epoch(
                prior_work.frontier.observation.metadata
            ),
        )
        scheduler_prior_metadata = merge_exploration_scheduler_facts(
            prior_work.frontier.observation.metadata,
            scheduler_facts,
            surface="app",
        )
        scheduler_evidence_gaps = _app_evidence_gap_page_types(
            scheduler_prior_metadata,
            known_page_types=_app_required_page_types(prior_work.app_goal),
        )
        observed_at, observed_at_clock_meta = _scheduler_monotonic_observed_at(
            sample_observed_at=saved.observed_at,
            prior_observed_at=prior_work.frontier.observation.observed_at,
        )
        navigation_depth = _current_business_navigation_depth(prior_work)
        verified_path = _text_tuple(
            prior_work.frontier.observation.metadata.get("app_verified_navigation_path")
        )
        observed_identity = confirmed_app_identity(saved.analysis)
        if observed_identity is not None and (
            not verified_path or verified_path[-1] != observed_identity
        ):
            # 被动观察发现了另一业务页，但不能替缺失的动作补造一条转场。
            verified_path = (observed_identity,)
        frontier = _build_frontier(
            analysis=saved.analysis,
            observed_at=observed_at,
            surface_kind=prior_work.surface_kind,
            navigation_depth=navigation_depth,
            max_navigation_depth=min(
                prior_work.budget.max_depth,
                prior_work.boundaries.max_navigation_depth,
            ),
            allowed_hosts=prior_work.boundaries.allowed_hosts,
            visited_state_ids=_append_unique(
                prior_work.frontier.visited_state_ids,
                prior_work.frontier.observation.state_version,
            ),
            visited_candidate_ids=prior_work.frontier.visited_candidate_ids,
            visited_action_fingerprints=(prior_work.frontier.visited_action_fingerprints),
            repeated_state_count=_next_repeated_state_count(
                prior_work=prior_work,
                next_state_version=saved.analysis.image_signature,
                next_analysis=saved.analysis,
            ),
            lineage_metadata={
                "agent_run_id": _agent_run_id(prior_work),
                "parent_work_id": prior_work.work_id,
                "parent_branch_id": prior_work.branch_id,
                "parent_capture_id": prior_work.source_capture_id,
                "surface_phase_run_id": decision.phase_run_id,
                "surface_phase_key": decision.phase_key,
                "surface_phase_decision_id": decision.decision_id,
                "semantic_role": "surface_reobserve",
                **_app_flow_observation_lineage(prior_work, decision),
                "app_verified_navigation_path": list(verified_path),
                **_next_surface_phase_observation_counters(
                    phase_key=decision.phase_key,
                    prior_metadata=prior_work.frontier.observation.metadata,
                ),
                **observed_at_clock_meta,
            },
            coverage_metadata=await _maybe_attach_app_journey_plans(
                task_id=prior_work.task_id,
                agent_run_id=_agent_run_id(prior_work),
                coverage_metadata=_next_coverage_metadata(
                goal=prior_work.app_goal,
                analysis=saved.analysis,
                prior_metadata=scheduler_prior_metadata,
                prior_state_version=prior_work.frontier.observation.state_version,
                pending_facts=pending_facts,
                business_navigation_depth=navigation_depth,
                business_page_scope=_current_business_page_scope(prior_work),
                # 短剧挂起时的 TimedObservation 必须累加 observe_count。
                count_short_drama_playback_observation=short_drama_exit_pending(
                    scheduler_prior_metadata
                ),
                scheduler_max_actions=prior_work.budget.max_actions,
                scheduler_max_duration_ms=prior_work.budget.remaining_duration_ms,
                scheduler_deadline=prior_work.deadline,
                scheduler_required_page_types=_app_required_page_types(prior_work.app_goal),
                scheduler_evidence_gap_page_types=scheduler_evidence_gaps,
                scheduler_state_fingerprint=saved.analysis.image_signature,
                scheduler_now=observed_at,
                scheduler_elapsed_duration_ms=_elapsed_duration_milliseconds(
                    prior_work.frontier.observation.observed_at,
                    observed_at,
                ),
                ),
            ),
        )
        frontier = replace(
            frontier,
            observation=replace(
                frontier.observation,
                metadata=merge_exploration_scheduler_facts(
                    frontier.observation.metadata,
                    scheduler_facts,
                    surface="app",
                ),
            ),
        )
        frontier = await self._attach_flow_foundation_trigger(
            frontier,
            task_id=prior_work.task_id,
            session_id=prior_work.session_id,
            agent_run_id=_agent_run_id(prior_work),
            parent_work_id=prior_work.work_id,
            parent_version=int(
                prior_work.frontier.observation.metadata.get("parent_version") or 0
            )
            + 1,
            page_type=saved.analysis.visual_page_type,
            subject_ref=confirmed_app_identity(saved.analysis),
            phase_run_id=decision.phase_run_id,
        )
        elapsed_duration_ms = _elapsed_duration_milliseconds(
            prior_work.frontier.observation.observed_at,
            observed_at,
        )
        work = await self._orchestrator.prepare_exploration(
            StartExplorationCommand(
                task_id=prior_work.task_id,
                session_id=prior_work.session_id,
                branch_id=f"app:{capture.capture_id}",
                objective=prior_work.objective,
                surface_kind=prior_work.surface_kind,
                source_capture_id=capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
                boundaries=prior_work.boundaries,
                budget=copy_exploration_budget_after_observation(
                    prior_work.budget,
                    elapsed_duration_ms=elapsed_duration_ms,
                ),
                deadline=prior_work.deadline,
                app_goal=prior_work.app_goal,
            )
        )
        if work.source_capture_id != capture.capture_id:
            raise ValueError("Orchestrator 返回了错误的 App Observation 后继 Work")
        return AppScreenshotExplorationResult(
            observation_id=saved.observation_id,
            visual_created=saved.created,
            page_type_assessment=adapt_app_visual_analysis_to_assessment(
                task_id=prior_work.task_id,
                agent_run_id=_agent_run_id(prior_work),
                analysis=saved.analysis,
                assessed_at=observed_at,
            ),
            work=work,
        )

    async def _attach_flow_foundation_trigger(
        self,
        frontier,
        *,
        task_id: str,
        session_id: str,
        agent_run_id: str,
        parent_work_id: str,
        parent_version: int,
        page_type: str | None,
        subject_ref: str | None,
        phase_run_id: str | None = None,
    ):
        """观察后评估短流程触发；runtime 为 None 时原样返回，不破坏既有单测。"""

        runtime = self._flow_foundation_runtime
        if runtime is None:
            return frontier
        assert_claimable = getattr(runtime, "assert_parent_claimable", None)
        if callable(assert_claimable):
            await assert_claimable(parent_work_id)
        evaluate = getattr(runtime, "evaluate_and_maybe_claim_after_observation", None)
        if not callable(evaluate):
            return frontier
        decision = evaluate(
            task_id=task_id,
            exploration_scope_id=f"app-scope:{task_id}:{session_id}",
            phase_run_id=phase_run_id or f"flow:{parent_work_id}",
            parent_work_kind="app_exploration_work",
            parent_work_id=parent_work_id,
            parent_version=parent_version,
            owner_execution_segment_id=agent_run_id,
            page_type=page_type,
            subject_ref=subject_ref,
        )
        if isinstance(decision, dict) and decision.get("claimed"):
            persist = getattr(runtime, "persist_claim_decision", None)
            if callable(persist):
                await persist(task_id=task_id, decision=decision)
        metadata = dict(frontier.observation.metadata)
        metadata["flow_foundation_trigger"] = decision
        return replace(
            frontier,
            observation=replace(frontier.observation, metadata=metadata),
        )

    async def _understand_capture(
        self,
        capture: CaptureRef,
        *,
        mismatch_message: str,
        allowed_hosts: tuple[str, ...] = (),
        surface_kind: SurfaceKind = SurfaceKind.ANDROID,
        dynamic_frontier: bool = False,
        reuse_from: AppVisualAnalysis | None = None,
        reuse_reason: str | None = None,
    ) -> SavedAppVisualObservationPort:
        return await ensure_app_capture_visual_observation(
            capture=capture,
            understanding=self._understanding,
            observations=self._observations,
            mismatch_message=mismatch_message,
            analysis_enricher=lambda analysis: _enrich_package_boundary_analysis(
                analysis,
                surface_kind=surface_kind,
                allowed_hosts=allowed_hosts,
                dynamic_frontier=dynamic_frontier,
            ),
            reuse_from=reuse_from,
            reuse_reason=reuse_reason,
        )

    async def _read_pending_facts(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> AppExplorationPendingFacts | None:
        if self._pending_facts is None:
            return None
        facts = await self._pending_facts.exploration_pending_facts(
            task_id=task_id,
            session_id=session_id,
        )
        if facts.task_id != task_id or facts.session_id != session_id:
            raise ValueError("App 探索待处理事实与当前 Session 不一致")
        return facts

    async def _read_scheduler_facts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int | None = None,
    ) -> Mapping[str, object] | None:
        """读取当前 App Run/Epoch 的真实证据事实。"""

        if self._scheduler_facts_reader is None:
            return None
        facts = await self._scheduler_facts_reader.read_app_facts(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            finalization_epoch=finalization_epoch,
        )
        if not isinstance(facts, Mapping):
            raise TypeError("App 调度事实读取器必须返回 Mapping")
        return facts


async def _maybe_attach_app_journey_plans(
    *,
    task_id: str,
    coverage_metadata: Mapping[str, object] | dict[str, object],
    agent_run_id: str | None = None,
) -> dict[str, object]:
    """首页到达且弹层满足后锁定 P1-4 与 F01 App 短流程执行计划。"""

    from services.api.app.application.app_exploration_flow_contract import (
        APP_FLOW_CONTRACT_KEY,
    )
    from services.api.app.application.app_journey_execution_bridge import (
        attach_journey_plan_to_coverage,
        ensure_app_journey_execution_plans,
        ensure_app_shortflow_execution_plans,
        flow_ready_for_app_journey,
    )
    from services.api.app.application.app_schedule_gap_closure import (
        ensure_journey_curriculum,
    )
    from services.api.app.application.journey_definitions import JourneyDefinitionService
    from services.api.app.infrastructure.db.journey_definition_repository import (
        PostgresJourneyDefinitionRepository,
    )

    out = ensure_journey_curriculum(
        dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    )
    flow = out.get(APP_FLOW_CONTRACT_KEY)
    if not flow_ready_for_app_journey(flow if isinstance(flow, Mapping) else None):
        return out
    service = JourneyDefinitionService(repository=PostgresJourneyDefinitionRepository())
    plans: list[dict[str, str]] = []
    # F01 短流程可多份并行锁定；P1-4 任务级仍最多一份。
    if out.get("app_shortflow_plans_ready") is not True and agent_run_id:
        plans.extend(
            await ensure_app_shortflow_execution_plans(
                service=service,
                task_id=task_id,
                agent_run_id=str(agent_run_id),
            )
        )
    if out.get("app_journey_plans_ready") is not True or plans:
        if not any(str(item.get("plan_kind") or "") == "p1_4" for item in (
            *(out.get("journey_execution_plans") or ()),
            *plans,
        ) if isinstance(item, Mapping)):
            plans.extend(
                await ensure_app_journey_execution_plans(service=service, task_id=task_id)
            )
    if not plans and out.get("app_journey_plans_ready") is True:
        return out
    return attach_journey_plan_to_coverage(out, plans=plans)


def _scheduler_monotonic_observed_at(
    *,
    sample_observed_at: datetime,
    prior_observed_at: datetime,
) -> tuple[datetime, dict[str, object]]:
    """调度单调性抬升 observed_at 时标注时钟域，不得伪装为设备真采样。

    抬升后的值仅用于 frontier 排序；``observed_at_clock_domain`` 使用
    ``persisted_at_legacy``（调度合成），真采样保留 ``producer_monotonic``。
    """

    from services.api.app.domain.exploration.flow_foundation_evidence import (
        ObservationClockDomain,
    )

    floor = prior_observed_at + timedelta(microseconds=1)
    if sample_observed_at >= floor:
        return sample_observed_at, {
            "observed_at_clock_domain": ObservationClockDomain.PRODUCER_MONOTONIC.value,
            "scheduler_synthetic_observed_at": False,
        }
    return floor, {
        "observed_at_clock_domain": ObservationClockDomain.PERSISTED_AT_LEGACY.value,
        "scheduler_synthetic_observed_at": True,
        "scheduler_monotonic": True,
    }


def _build_frontier(
    *,
    analysis: AppVisualAnalysis,
    observed_at: datetime,
    surface_kind: SurfaceKind = SurfaceKind.ANDROID,
    navigation_depth: int = 1,
    max_navigation_depth: int | None = None,
    allowed_hosts: tuple[str, ...] = (),
    visited_state_ids: tuple[str, ...] = (),
    visited_candidate_ids: tuple[str, ...] = (),
    visited_action_fingerprints: tuple[str, ...] = (),
    repeated_state_count: int = 0,
    lineage_metadata: dict[str, object] | None = None,
    coverage_metadata: dict[str, object] | None = None,
) -> FrontierSnapshot:
    package_name = analysis.package_name or "unknown"
    activity = analysis.activity or "unknown"
    location = f"app://{package_name}/{activity}"
    from services.api.app.domain.app_product.profiles import (
        build_app_product_capability_facts,
    )

    observation = SurfaceObservation(
        capture_id=analysis.capture_id,
        surface_kind=surface_kind,
        location=location,
        state_version=analysis.image_signature,
        observed_at=observed_at,
        page_type=analysis.visual_page_type,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        key_elements=analysis.key_elements,
        metadata={
            "screenshot_artifact_id": analysis.screenshot_artifact_id,
            "width": analysis.width,
            "height": analysis.height,
            "orientation": analysis.orientation,
            "image_signature": analysis.image_signature,
            "package_name": analysis.package_name,
            "activity": analysis.activity,
            "visual_summary": analysis.visual_summary,
            "surface_facts": dict(analysis.surface_facts),
            "visual_result_hash": analysis.result_hash,
            "app_obstruction_identity": build_app_obstruction_identity(analysis),
            "business_navigation_depth": navigation_depth,
            "app_verified_navigation_path": (
                [identity] if (identity := confirmed_app_identity(analysis)) is not None else []
            ),
            # F04：V2 Explorer 只读注入能力事实，不授予坐标。
            "app_product_capability_facts": build_app_product_capability_facts(
                analysis.package_name
            ),
            **(lineage_metadata or {}),
            **(coverage_metadata or {}),
        },
    )
    source_obstruction_identity = build_app_obstruction_identity(analysis)
    dynamic_frontier = _is_dynamic_frontier_policy(
        coverage_metadata.get("app_runtime_policy")
        if isinstance(coverage_metadata, Mapping)
        else None
    )
    candidates = tuple(
        _map_candidate(
            item,
            navigation_depth=navigation_depth,
            surface_kind=surface_kind,
            source_obstruction_identity=source_obstruction_identity,
        )
        for item in analysis.candidates
    )
    candidates = _append_curriculum_observation_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_home_popup_sequence_observation_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_dynamic_startup_observation_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_unstable_page_reobserve_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_unstable_page_back_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_curriculum_scroll_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_dynamic_scroll_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_package_boundary_back_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        location=location,
        allowed_hosts=allowed_hosts,
    )
    candidates = _append_popup_back_recovery_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        location=location,
        allowed_hosts=allowed_hosts,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_paywall_sheet_back_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        location=location,
        allowed_hosts=allowed_hosts,
        dynamic_frontier=dynamic_frontier,
    )
    candidates = _append_hlw_curriculum_detail_back_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        location=location,
        allowed_hosts=allowed_hosts,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_startup_ad_skip_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
    )
    candidates = _sign_first_popup_ad_landing_candidate(
        candidates=candidates,
        analysis=analysis,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_popup_close_corner_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
    )
    candidates = _append_profile_credential_save_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
    )
    candidates = _append_coverage_page_seek_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
        coverage_metadata=coverage_metadata,
    )
    # 稳定首页／列表中部宫格分栏目必须轮询，不得被底栏与推荐卡挤掉。
    candidates = _append_home_category_grid_seek_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
        coverage_metadata=coverage_metadata,
    )
    # 海角主页上方分类（最新／国产／动漫／漫画；推荐不点）须在切底栏前点完。
    candidates = _append_home_top_category_tab_seek_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
        coverage_metadata=coverage_metadata,
    )
    # 根业务页底栏 Tab 必须轮询；video_list 等非 home 页不得整轮 0 点击。
    candidates = _append_bottom_navigation_tab_seek_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
        coverage_metadata=coverage_metadata,
    )
    candidates = _filter_curriculum_frontier_candidates(
        candidates=candidates,
        analysis=analysis,
        coverage_metadata=coverage_metadata,
    )
    candidates = _filter_dynamic_safety_candidates(
        candidates=candidates,
        analysis=analysis,
        coverage_metadata=coverage_metadata,
    )
    candidates = _filter_unstable_page_navigation_candidates(
        candidates=candidates,
        analysis=analysis,
    )
    candidates = _retain_only_obstruction_recovery_candidates(
        candidates=candidates,
        analysis=analysis,
        coverage_metadata=coverage_metadata,
    )
    candidates = _filter_dynamic_page_limit_candidates(
        candidates=candidates,
        coverage_metadata=coverage_metadata,
    )
    candidates = _filter_max_navigation_depth_candidates(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=navigation_depth,
        max_navigation_depth=max_navigation_depth,
    )
    if isinstance(coverage_metadata, Mapping):
        candidates = tuple(
            candidate
            for candidate in candidates
            if not should_suppress_redundant_app_candidate(
                candidate=candidate,
                coverage_metadata=coverage_metadata,
            )
        )
    # 视觉映射与合成注入可能共用同一 candidate_id；封存前按首见保序去重。
    candidates = _dedupe_frontier_candidates(candidates)
    snapshot_id = "frontier:" + stable_protocol_hash(
        {
            "capture_id": analysis.capture_id,
            "result_hash": analysis.result_hash,
            "candidate_ids": [item.candidate_id for item in candidates],
            "coverage_metadata": coverage_metadata or {},
        }
    )
    return FrontierSnapshot(
        snapshot_id=snapshot_id,
        observation=observation,
        candidates=candidates,
        visited_state_ids=visited_state_ids,
        visited_candidate_ids=visited_candidate_ids,
        visited_action_fingerprints=visited_action_fingerprints,
        repeated_state_count=repeated_state_count,
    )


def _dedupe_frontier_candidates(
    candidates: tuple[CandidateAction, ...],
) -> tuple[CandidateAction, ...]:
    """按 candidate_id 保序去重，优先保留更靠前的合成／注入候选。"""

    unique: dict[str, CandidateAction] = {}
    for candidate in candidates:
        unique.setdefault(candidate.candidate_id, candidate)
    return tuple(unique.values())


def _is_dynamic_frontier_policy(policy: object) -> bool:
    """判断当前运行是否使用动态 Frontier 合同。"""

    return isinstance(policy, Mapping) and policy.get("exploration_mode") == "dynamic_frontier"


def _filter_curriculum_frontier_candidates(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """按当前课纲阶段收口候选，避免不匹配动作反复消耗预算。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    raw_progress = coverage_metadata.get("app_curriculum_progress")
    if not isinstance(raw_progress, Mapping):
        return candidates
    try:
        progress = AppCurriculumProgress.from_dict(raw_progress)
        playbook = resolve_playbook_by_id(progress.playbook_id)
    except (TypeError, ValueError):
        return candidates
    if playbook is None or progress.complete:
        return candidates

    matching: list[CandidateAction] = []
    safe_overrides: list[CandidateAction] = []
    for candidate in candidates:
        if _curriculum_candidate_matches(
            candidate=candidate,
            playbook=playbook,
            progress=progress,
        ):
            matching.append(candidate)
        elif _is_curriculum_safe_override(
            candidate=candidate,
            analysis=analysis,
            progress=progress,
        ):
            safe_overrides.append(candidate)

    seeks = [
        candidate
        for candidate in safe_overrides
        if (
            candidate.metadata.get("coverage_page_seek") is True
            or candidate.metadata.get("bottom_navigation_tab_seek") is True
            or candidate.metadata.get("frontier_scope") == "app"
        )
    ]
    other_safe = [
        candidate
        for candidate in safe_overrides
        if (
            candidate.metadata.get("coverage_page_seek") is not True
            and candidate.metadata.get("bottom_navigation_tab_seek") is not True
            and candidate.metadata.get("frontier_scope") != "app"
        )
    ]
    # 缺页覆盖寻页／底栏必须插队：否则课纲同页 scroll／observe／关层会占满预算。
    if seeks:
        return tuple((*seeks, *matching, *other_safe))
    if matching:
        return tuple((*matching, *other_safe))
    # 没有匹配动作时不能把普通视觉点击原样放回去；只保留显式安全兜底，
    # 零触控阶段的 OBSERVE 已由 _append_curriculum_observation_candidate 补齐。
    return tuple((*seeks, *other_safe))


def _filter_dynamic_safety_candidates(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """动态只读模式确定性拒绝模型误降级的外部副作用入口。"""

    if isinstance(coverage_metadata, Mapping) and isinstance(
        coverage_metadata.get("app_curriculum_progress"),
        Mapping,
    ):
        return candidates
    filtered = tuple(
        candidate
        for candidate in candidates
        if not _is_forbidden_dynamic_side_effect(candidate)
        or (
            is_visual_obstruction_control(candidate)
            and _is_dynamic_popup_close_candidate(candidate=candidate, analysis=analysis)
        )
        or _is_dynamic_connection_recovery_candidate(
            candidate=candidate,
            analysis=analysis,
        )
    )
    if not _is_dynamic_startup_blocker(analysis):
        return filtered
    observation_count = 0
    recovery_action_count = 0
    recovery_targets: tuple[str, ...] = ()
    if isinstance(coverage_metadata, Mapping):
        raw_count = coverage_metadata.get(_DYNAMIC_STARTUP_OBSERVATION_COUNT)
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            observation_count = raw_count
        raw_recovery_count = coverage_metadata.get(_DYNAMIC_STARTUP_RECOVERY_ACTION_COUNT)
        if (
            isinstance(raw_recovery_count, int)
            and not isinstance(raw_recovery_count, bool)
            and raw_recovery_count >= 0
        ):
            recovery_action_count = raw_recovery_count
        recovery_targets = _text_tuple(coverage_metadata.get(_DYNAMIC_STARTUP_RECOVERY_TARGETS))
    return tuple(
        candidate
        for candidate in filtered
        if not (
            candidate.action_type is InteractionOperation.OBSERVE
            and observation_count >= APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT
        )
        if _is_allowed_dynamic_startup_candidate(
            candidate=candidate,
            analysis=analysis,
            observation_count=observation_count,
            recovery_action_count=recovery_action_count,
            recovery_targets=recovery_targets,
        )
    )


def _filter_dynamic_page_limit_candidates(
    *,
    candidates: tuple[CandidateAction, ...],
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """达到页面抽样上限后停止扩展新业务页，保留同页采样和安全恢复。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    policy = coverage_metadata.get("app_runtime_policy")
    if not _is_dynamic_frontier_policy(policy):
        return candidates
    if policy_is_quantity_unbounded(policy):
        return candidates
    page_limit = policy.get("page_limit")
    covered_count = coverage_metadata.get("covered_business_page_count")
    if not page_limit_blocks_new_page_candidates(
        quantity_unbounded=False,
        page_limit=page_limit,
        covered_count=covered_count,
    ):
        return candidates
    covered = {
        item.strip().casefold()
        for item in coverage_metadata.get("covered_page_types", ())
        if isinstance(item, str) and item.strip()
    }
    retained: list[CandidateAction] = []
    for candidate in candidates:
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            retained.append(candidate)
            continue
        if candidate.metadata.get("package_boundary_recovery") is True:
            retained.append(candidate)
            continue
        scope = candidate.metadata.get("frontier_scope")
        if scope not in {"page", "app"}:
            # 弹层关闭等 local 动作仍可为当前页补齐证据。
            retained.append(candidate)
            continue
        expected = candidate.metadata.get("expected_next_page_type")
        expected_page_type = expected.strip().casefold() if isinstance(expected, str) else ""
        if expected_page_type and expected_page_type in covered:
            retained.append(candidate)
    return tuple(retained)


def _filter_max_navigation_depth_candidates(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    max_navigation_depth: int | None,
) -> tuple[CandidateAction, ...]:
    """到达业务深度上限后，拒绝继续进入已知的下一层页面。"""

    if max_navigation_depth is None or navigation_depth < max_navigation_depth:
        return candidates
    current_page_type = str(analysis.visual_page_type or "").strip().casefold()
    retained: list[CandidateAction] = []
    for candidate in candidates:
        expected = candidate.metadata.get("expected_next_page_type")
        expected_page_type = expected.strip().casefold() if isinstance(expected, str) else ""
        enters_known_nested_page = (
            candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and candidate.metadata.get("frontier_scope") == "page"
            and expected_page_type
            and expected_page_type != current_page_type
        )
        if not enters_known_nested_page:
            retained.append(candidate)
    return tuple(retained)


def _retain_only_obstruction_recovery_candidates(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """广告／弹层未清空前禁止执行底栏、内容卡片等业务导航。

    弹层帧数达让路阈值后，保留当前帧真实覆盖／分栏目寻页，避免永久关层空转。
    详情返回后的 home|overlay 误标：若底栏可见且无挡路 dialog，不按清障帧裁剪。
    """

    if not _is_popup_surface_analysis(analysis):
        return candidates
    # 软 overlay：仅签名／popup_visible，无 ad_popup／modal → 可让底栏／业务寻页。
    # 仍须把缺失发帖分支提到关层／返回之前，避免软遮挡早退打乱 post_publish 优先级。
    if _analysis_is_soft_overlay_only(analysis) and (
        _analysis_has_real_bottom_navigation(analysis, candidates)
        or coverage_incomplete_allows_bottom_nav_through_overlay(
            coverage_metadata=(
                coverage_metadata if isinstance(coverage_metadata, Mapping) else None
            ),
        )
    ):
        soft_safe_branches = tuple(
            replace(
                candidate,
                exploration_value=max(candidate.exploration_value, 98),
                metadata={
                    **dict(candidate.metadata),
                    "coverage_page_seek": True,
                    "expected_next_page_type": "post_publish",
                },
            )
            for candidate in candidates
            if _is_safe_missing_overlay_business_branch(
                candidate=candidate,
                analysis=analysis,
                coverage_metadata=coverage_metadata,
            )
        )
        soft_safe_ids = {item.candidate_id for item in soft_safe_branches}
        soft_rest = tuple(
            candidate
            for candidate in candidates
            if candidate.candidate_id not in soft_safe_ids
            and candidate.metadata.get("home_category_grid_seek") is not True
            # 非安全发帖分支（提交／确认发布）即使软遮挡也不得放行。
            and candidate.metadata.get("expected_next_page_type") != "post_publish"
        )
        return tuple((*soft_safe_branches, *soft_rest))
    raw_progress = (
        coverage_metadata.get("app_curriculum_progress")
        if isinstance(coverage_metadata, Mapping)
        else None
    )
    if isinstance(raw_progress, Mapping):
        try:
            progress = AppCurriculumProgress.from_dict(raw_progress)
        except (TypeError, ValueError):
            progress = None
        # 神器课纲的第二步本身就是弹层内受控下滑，不属于被广告遮挡的业务寻页。
        if progress is not None and progress.current_phase_id == "shenqi":
            return candidates
    safe_business_branches = tuple(
        replace(
            candidate,
            exploration_value=max(candidate.exploration_value, 98),
            metadata={
                **dict(candidate.metadata),
                "coverage_page_seek": True,
                "expected_next_page_type": "post_publish",
            },
        )
        for candidate in candidates
        if _is_safe_missing_overlay_business_branch(
            candidate=candidate,
            analysis=analysis,
            coverage_metadata=coverage_metadata,
        )
    )
    yield_seeks = ()
    incomplete_tabs = coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=coverage_metadata if isinstance(coverage_metadata, Mapping) else None,
    )
    if overlay_budget_allows_real_coverage_seek(
        page_type=analysis.visual_page_type,
        coverage_metadata=coverage_metadata if isinstance(coverage_metadata, Mapping) else None,
    ):
        yield_seeks = tuple(
            candidate
            for candidate in candidates
            if _is_real_overlay_yield_seek_candidate(candidate)
            # 遮罩帧一律禁止上方分类／宫格：真实栏目只在关完弹层后出现。
            and candidate.metadata.get("home_top_category_tab_seek") is not True
            and candidate.metadata.get("home_category_grid_seek") is not True
        )
    elif incomplete_tabs and _analysis_has_real_bottom_navigation(analysis, candidates):
        # 覆盖缺口未完：即使未满 6 帧，也保留真实底栏，避免广告取证／关层占满预算。
        # 含未盖 bottom_navigation_tab_seek 标记的视觉底栏（frontier=app + 底栏带）。
        visual_by_id = {
            item.candidate_id: item
            for item in analysis.candidates
        }
        yield_seeks = tuple(
            candidate
            for candidate in candidates
            if _is_early_bottom_nav_overlay_yield_candidate(candidate)
            or (
                is_bottom_navigation_tab_candidate(
                    candidate=candidate,
                    visual_candidate=visual_by_id.get(candidate.candidate_id),
                )
                and candidate.metadata.get("home_category_grid_seek") is not True
                and candidate.metadata.get("home_top_category_tab_seek") is not True
            )
        )
    safe_business_branch_ids = {candidate.candidate_id for candidate in safe_business_branches}
    yield_seek_ids = {candidate.candidate_id for candidate in yield_seeks}
    recoveries = tuple(
        candidate
        for candidate in candidates
        if candidate.candidate_id not in safe_business_branch_ids
        and candidate.candidate_id not in yield_seek_ids
        and (
            candidate.action_type
            in {
                InteractionOperation.BACK,
                InteractionOperation.WARM_RESTORE,
                InteractionOperation.OBSERVE,
                InteractionOperation.TIMED_CAPTURE,
            }
            or candidate.metadata.get("package_boundary_recovery") is True
            or _is_dynamic_popup_close_candidate(candidate=candidate, analysis=analysis)
            or is_visual_obstruction_control(candidate)
            or _is_dynamic_age_confirm_candidate(candidate=candidate, analysis=analysis)
            or _is_dynamic_profile_credential_save_candidate(
                candidate=candidate, analysis=analysis
            )
            or _is_signed_app_ad_landing_candidate(candidate)
        )
    )
    # 发帖类型选择层不是广告阻断，而是 post_publish 的安全业务分支。
    # 先进入编辑页留证，最终提交仍由副作用门禁拒绝，避免反复 BACK 空转。
    # 保留同帧真实关闭控件，由决策层按当前遮挡事实选择；累计预算不删除证据。
    if yield_seeks:
        close_controls = tuple(
            candidate
            for candidate in recoveries
            if _is_dynamic_popup_close_candidate(candidate=candidate, analysis=analysis)
            or is_visual_obstruction_control(candidate)
            or _is_dynamic_age_confirm_candidate(candidate=candidate, analysis=analysis)
            or _is_dynamic_profile_credential_save_candidate(
                candidate=candidate, analysis=analysis
            )
        )
        if overlay_budget_allows_real_coverage_seek(
            page_type=analysis.visual_page_type,
            coverage_metadata=(
                coverage_metadata if isinstance(coverage_metadata, Mapping) else None
            ),
        ):
            return (*close_controls, *yield_seeks, *safe_business_branches)
        bottom_yields = tuple(
            candidate
            for candidate in yield_seeks
            if candidate.metadata.get("bottom_navigation_tab_seek") is True
        )
        # 普通浮层仍可巡栏；真实挡路弹窗的关闭候选也必须保留。
        if bottom_yields and coverage_incomplete_allows_bottom_nav_through_overlay(
            coverage_metadata=(
                coverage_metadata if isinstance(coverage_metadata, Mapping) else None
            ),
        ):
            return (*bottom_yields, *close_controls, *safe_business_branches)
        # 提早让路且尚无底栏条带：仍保留关层，避免空点。
        return (*close_controls, *yield_seeks, *safe_business_branches)
    return (*safe_business_branches, *recoveries)


def _is_real_overlay_yield_seek_candidate(candidate: CandidateAction) -> bool:
    """弹层让路只放行当前帧真实寻页，禁止旧合成坐标穿透广告。"""

    if candidate.metadata.get("coverage_page_seek_contract") is not None:
        return False
    if candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    if candidate.metadata.get("control_role") in {"dismiss", "skip"}:
        return False
    if candidate.metadata.get("home_category_grid_seek") is True:
        return True
    if candidate.metadata.get("home_top_category_tab_seek") is True:
        return True
    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
        return True
    if candidate.metadata.get("coverage_page_seek") is True:
        return True
    # 底栏全局导航在弹层帧仍常被视觉识别；达阈值后可用来切页／摆脱空转关层。
    return candidate.metadata.get("frontier_scope") == "app"


def _is_early_bottom_nav_overlay_yield_candidate(candidate: CandidateAction) -> bool:
    """覆盖缺口提早让路时只放行底栏，不放行分栏目／一般覆盖寻页。"""

    if candidate.metadata.get("coverage_page_seek_contract") is not None:
        return False
    if candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    if candidate.metadata.get("control_role") in {"dismiss", "skip"}:
        return False
    if candidate.metadata.get("home_category_grid_seek") is True:
        return False
    if candidate.metadata.get("home_top_category_tab_seek") is True:
        return False
    return candidate.metadata.get("bottom_navigation_tab_seek") is True


def _analysis_has_real_bottom_navigation(
    analysis: AppVisualAnalysis,
    candidates: tuple[CandidateAction, ...] | None = None,
) -> bool:
    """当前帧是否已有可点的真实底栏（候选或 a11y 事实）。"""

    pool = candidates if candidates is not None else ()
    visual_by_id = {
        item.candidate_id: item
        for item in (getattr(analysis, "candidates", None) or ())
    }
    for candidate in pool:
        if candidate.metadata.get("bottom_navigation_tab_seek") is True:
            return True
        if is_bottom_navigation_tab_candidate(
            candidate=candidate,
            visual_candidate=visual_by_id.get(candidate.candidate_id),
        ):
            return True
    for item in getattr(analysis, "candidates", None) or ():
        if item.frontier_scope == "app" and item.normalized_bounds is not None:
            if float(item.normalized_bounds.top) >= 0.78:
                return True
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    if not isinstance(controls, Mapping):
        return False
    count = controls.get("bottom_navigation_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
        return True
    navigation = controls.get("bottom_navigation")
    return isinstance(navigation, (list, tuple)) and bool(navigation)


def _is_safe_missing_overlay_business_branch(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """只放行进入缺失发帖编辑页的可控浮层分支，不放行最终发布。"""

    if (
        candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}
        or candidate.risk_level
        not in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        or candidate.metadata.get("frontier_scope") not in {"local", "page"}
        or candidate.metadata.get("expected_next_page_type") != "post_publish"
        or _is_forbidden_dynamic_side_effect(candidate)
        or str(analysis.visual_page_type or "").strip().casefold()
        not in {"post_list", "content_list"}
        or not isinstance(coverage_metadata, Mapping)
    ):
        return False
    required = {
        item.casefold() for item in _text_tuple(coverage_metadata.get("required_page_types"))
    }
    covered = {item.casefold() for item in _text_tuple(coverage_metadata.get("covered_page_types"))}
    if "post_publish" not in required or "post_publish" in covered:
        return False
    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    return any(marker in terms for marker in ("发帖", "图文帖子", "视频帖子", "post"))


def _is_dynamic_startup_blocker(analysis: AppVisualAnalysis) -> bool:
    states = {
        str(analysis.page_state or "").strip().casefold(),
        str(analysis.visual_page_type or "").strip().casefold(),
    }
    if bool(
        states
        & {
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
        }
    ) or _has_explicit_connection_gate_text(analysis):
        return True
    # 开屏后空页型 + 仅「重试／重新加载」时，按启动阻断处理，允许点重试离开平台。
    if not str(analysis.visual_page_type or "").strip():
        return any(
            candidate.risk_level is InteractionRiskLevel.LOW
            and candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and _has_dynamic_connection_recovery_terms(
                _map_candidate(
                    candidate,
                    navigation_depth=0,
                    surface_kind=SurfaceKind.ANDROID,
                )
            )
            for candidate in analysis.candidates
        )
    return False


def _is_unstable_page_analysis(analysis: AppVisualAnalysis) -> bool:
    """视觉页型缺失时需有界恢复；loading 启动阻断改走 startup observation 合同。"""

    if _is_dynamic_startup_blocker(analysis):
        return False
    return not str(analysis.visual_page_type or "").strip()


def _is_allowed_dynamic_startup_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
    observation_count: int,
    recovery_action_count: int,
    recovery_targets: tuple[str, ...],
) -> bool:
    """启动阻断页允许有界观察、同包恢复和受信跳过／关闭。"""

    if candidate.action_type in {
        InteractionOperation.OBSERVE,
        InteractionOperation.TIMED_CAPTURE,
    }:
        return True
    if (
        candidate.action_type
        in {InteractionOperation.BACK, InteractionOperation.WARM_RESTORE}
        and (
            candidate.metadata.get("package_boundary_recovery") is True
            or candidate.metadata.get("popup_back_recovery") is True
            or candidate.metadata.get("unstable_page_back_recovery") is True
            or _is_safe_startup_back_candidate(candidate=candidate, analysis=analysis)
            or _is_safe_line_loading_back_candidate(
                candidate=candidate,
                analysis=analysis,
            )
        )
    ):
        return True
    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    if _is_dynamic_popup_close_candidate(candidate=candidate, analysis=analysis):
        return True
    if _is_signed_app_ad_landing_candidate(candidate):
        return True
    # 合成开屏进入/跳过：不依赖 a11y，也不要求页型已标成 startup_ad。
    if _is_startup_semantic_skip_candidate(candidate):
        return True
    if _is_safe_startup_exit_candidate(candidate=candidate, analysis=analysis):
        return True
    if _is_dynamic_connection_recovery_candidate(candidate=candidate, analysis=analysis):
        recovery_key = _dynamic_connection_recovery_key(candidate)
        # 空白／line_loading 上的「重试重新加载」首帧即可点，不必空等两次观察。
        min_observations = 1 if _has_reload_retry_terms(candidate) else 2
        return (
            observation_count >= min_observations
            and recovery_action_count < _DYNAMIC_STARTUP_RECOVERY_ACTION_LIMIT
            and recovery_key not in recovery_targets
        )
    trusted_skip = analysis.surface_facts.get("trusted_accessibility_skip")
    if (
        isinstance(trusted_skip, Mapping)
        and trusted_skip.get("package_name") == analysis.package_name
    ):
        if candidate.risk_level is InteractionRiskLevel.LOW and (
            _is_safe_startup_exit_candidate(candidate=candidate, analysis=analysis)
            or candidate_explicitly_targets_tap_skip(
                action_type=candidate.action_type.value,
                risk_level=candidate.risk_level.value,
                semantic_target=candidate.semantic_target,
                purpose=str(candidate.metadata.get("purpose") or ""),
                expected_state_hint=str(candidate.metadata.get("expected_state_hint") or ""),
            )
        ):
            return True
    return False


def _is_safe_startup_back_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """只认可当前持久化启动广告事实中平台签发的无坐标 BACK。"""

    return (
        _is_startup_ad_analysis(analysis)
        and candidate.action_type is InteractionOperation.BACK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.semantic_target == "系统返回关闭启动广告"
        and candidate.metadata.get("purpose")
        == "使用 Android 系统 BACK 尝试关闭当前目标包内启动广告"
        and candidate.metadata.get("expected_state_hint") == "home"
    )


def _is_safe_line_loading_back_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """只放行平台为当前无遮挡线路过渡帧签发的无坐标 BACK。"""

    return (
        str(analysis.visual_page_type or "").strip().casefold() == "line_loading"
        and str(analysis.page_state or "").strip().casefold() in {"loading", "line_loading"}
        and analysis.overlay_signature is None
        and candidate.action_type is InteractionOperation.BACK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.semantic_target == "系统 BACK 返回上一级"
        and candidate.metadata.get("purpose")
        == "当前 line_loading 线路过渡页无法恢复来源，通过系统 BACK 返回上一级并重新截图"
        and candidate.metadata.get("expected_state_hint") == "back"
        and candidate.metadata.get("frontier_scope") == "local"
    )


def _is_dynamic_connection_recovery_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """只允许当前目标包内有事实支撑的线路切换、直连或连接重试。"""

    page_type = str(analysis.visual_page_type or "").strip().casefold()
    states = {
        str(analysis.page_state or "").strip().casefold(),
        page_type,
    }
    known_connection_state = bool(states & {
        "line_loading",
        "line_failure",
        "connection_failed",
        "connectivity_gate",
        "network_gate",
        "connecting",
        "startup_connection",
    })
    blank_reload_host = not page_type and str(analysis.page_state or "").strip().casefold() in {
        "stable",
        "normal",
        "loaded",
        "unknown",
        "",
    }
    if (
        not known_connection_state
        and not blank_reload_host
        and not _has_connection_recovery_surface_evidence(analysis)
    ):
        return False
    if (
        candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}
        or candidate.risk_level is not InteractionRiskLevel.LOW
    ):
        return False
    if not _has_dynamic_connection_recovery_terms(candidate):
        return False
    return (
        not _has_direct_connection_terms(candidate)
        or known_connection_state
        or blank_reload_host
        or _has_explicit_connection_gate_text(analysis)
    )


def _has_connection_recovery_surface_evidence(analysis: AppVisualAnalysis) -> bool:
    """用当前帧的低风险线路控件纠正无广告事实的 startup_ad 误分类。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    topmost_ad = facts.get("topmost_ad_popup")
    if (
        analysis.overlay_signature is not None
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
        or facts.get("countdown_remaining_ms") is not None
        or (isinstance(topmost_ad, Mapping) and topmost_ad.get("is_ad") is True)
    ):
        return False
    return any(
        candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and _has_dynamic_connection_recovery_terms(
            _map_candidate(
                candidate,
                navigation_depth=0,
                surface_kind=SurfaceKind.ANDROID,
            )
        )
        for candidate in analysis.candidates
    )


def _has_connection_recovery_surface_evidence(analysis: AppVisualAnalysis) -> bool:
    """用当前帧的低风险线路控件纠正无广告事实的 startup_ad 误分类。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    topmost_ad = facts.get("topmost_ad_popup")
    if (
        analysis.overlay_signature is not None
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
        or facts.get("countdown_remaining_ms") is not None
        or (isinstance(topmost_ad, Mapping) and topmost_ad.get("is_ad") is True)
    ):
        return False
    return any(
        candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and _has_dynamic_connection_recovery_terms(
            _map_candidate(
                candidate,
                navigation_depth=0,
                surface_kind=SurfaceKind.ANDROID,
            )
        )
        for candidate in analysis.candidates
    )


def _has_dynamic_connection_recovery_terms(candidate: CandidateAction) -> bool:
    joined = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    markers = (
        "备用线路",
        "切换线路",
        "重新检测",
        "重试连接",
        "重新连接",
        "网络重试",
        "点我直连",
        "直连试试",
        "直接连接",
        # 黑料等：开屏后空白／line_loading 只有「重试重新加载」按钮。
        "重试重新加载",
        "重新加载",
        "点击重试",
        "reload",
        "backup line",
        "alternate line",
        "retry connection",
        "reconnect",
        "direct connect",
    )
    return any(marker.casefold() in joined for marker in markers)


def _has_reload_retry_terms(candidate: CandidateAction) -> bool:
    """空白／加载失败页上的内容重试，与线路切换共用启动恢复预算。"""

    joined = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    return any(
        marker in joined
        for marker in ("重试重新加载", "重新加载", "点击重试", "reload")
    )


def _has_direct_connection_terms(candidate: CandidateAction) -> bool:
    joined = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    return any(
        marker in joined
        for marker in ("点我直连", "直连试试", "直接连接", "direct connect")
    )


def _has_explicit_connection_gate_text(analysis: AppVisualAnalysis) -> bool:
    """直连必须同时有当前帧的线路门禁文本，不能仅凭按钮名称自行放行。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    visual_page_name = facts.get("visual_page_name")
    page_name = (
        str(visual_page_name.get("page_name") or "")
        if isinstance(visual_page_name, Mapping)
        else ""
    )
    joined = " ".join(
        (
            str(analysis.visual_summary or ""),
            page_name,
            *(str(item) for item in analysis.key_elements),
        )
    ).casefold()
    return any(
        marker in joined
        for marker in (
            "线路检测失败",
            "线路自动检测失败",
            "检测线路",
            "正在检测线路",
            "备用线路",
            "网络连接失败",
            "连接失败",
            "connection failed",
            "detecting line",
            "alternate line",
        )
    )


def _dynamic_connection_recovery_key(candidate: CandidateAction) -> str:
    """跨截图稳定识别同一条线路，防止文案轻微变化后重复点击。"""

    joined = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
        )
    ).casefold()
    line_number = next((value for value in ("1", "2", "3", "4", "5") if value in joined), None)
    if line_number is not None:
        return f"line:{line_number}"
    return build_app_semantic_action_key(candidate)


def _is_forbidden_dynamic_side_effect(candidate: CandidateAction) -> bool:
    if candidate.metadata.get("package_boundary_recovery") is True:
        return False
    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    joined = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    for safe_guard in (
        "已排除广告下载充值语义",
        "不点击广告主体",
        "不点击广告本体",
        "不执行下载",
        "不执行充值",
        "不执行支付",
        "do not download",
        "do not purchase",
        "do not pay",
    ):
        joined = joined.replace(safe_guard.casefold(), "")
    if _is_signed_app_ad_landing_candidate(candidate):
        return False
    if _is_dynamic_ad_destination_candidate(candidate):
        return True
    forbidden_markers = (
        "点我直连",
        "直连试试",
        "翻墙",
        "重装",
        "下载",
        "充值",
        "支付",
        "购买",
        "开通会员",
        "外链广告",
        "获取验证码",
        "发送验证码",
        "提交登录",
        "登录提交",
        "login_submit",
        "立即注册",
        "提交注册",
        "register_submit",
        "提交申请",
        "确认申请",
        "提交发布",
        "确认发布",
        "direct connect",
        "reinstall",
        "download app",
        "purchase",
        "payment",
        "send verification code",
        "submit login",
        "submit registration",
        "submit application",
        "submit post",
    )
    return any(marker.casefold() in joined for marker in forbidden_markers)


def _is_dynamic_ad_destination_candidate(candidate: CandidateAction) -> bool:
    """拒绝稳定业务页中的广告／推广落地入口，但保留显式关闭和跳过。"""

    target = str(candidate.semantic_target or "").strip().casefold()
    purpose = str(candidate.metadata.get("purpose") or "").strip().casefold()
    expected = str(candidate.metadata.get("expected_state_hint") or "").strip().casefold()
    joined = " ".join((target, purpose, expected))
    if expected in {"close_popup_x", "tap_skip", "skip_ad", "app_ready"}:
        return False
    if any(
        marker in joined
        for marker in (
            "关闭广告",
            "关闭推广",
            "跳过广告",
            "skip ad",
            "close ad",
            "dismiss ad",
        )
    ):
        return False
    if any(
        marker in joined
        for marker in (
            "横幅广告",
            "广告横幅",
            "广告入口",
            "推广入口",
            "推广内容",
            "广告落地",
            "banner ad",
            "ad banner",
            "promo content",
        )
    ):
        return True
    # 部分成人内容 App 会把广告宫格伪装成普通“内容卡片”，Provider 也可能把
    # 它误标为 content_detail。只拦截具有明确营销导流语义的组合词，避免按
    # “同城”等单个宽泛词误伤正常业务内容。
    if any(
        marker in target
        for marker in (
            "全国约爱",
            "全国速配",
            "同城约拍",
            "同城约爱",
            "附近良家",
            "极速上门",
            "真实约炮",
            "免费约炮",
        )
    ):
        return True
    return any(marker in target for marker in ("广告", "推广", "banner", "promo")) and any(
        marker in purpose for marker in ("进入", "打开", "查看", "点击", "落地", "visit", "open")
    )


def _sign_first_popup_ad_landing_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """为 alioth_17 签发一次弹窗广告点击，不放宽其他广告副作用。"""

    if not _is_ad_popup_analysis(analysis):
        return candidates
    # 显式覆盖目标尚未进入 home 前，禁止开屏广告外跳取证。
    # 否则首点广告进浏览器 → 包边界 BACK 恢复 → 再次卡在 startup_ad，覆盖率永久 0%。
    # 覆盖分母仍有缺口且当前帧已见真实底栏时，同样延后广告取证，避免挤掉底栏巡检。
    if _should_defer_ad_landing_until_home(
        analysis=analysis,
        coverage_metadata=coverage_metadata,
    ) or _should_defer_ad_landing_for_incomplete_coverage(
        coverage_metadata=coverage_metadata,
    ):
        return candidates
    attempts = (
        _nonnegative_metadata_counter(coverage_metadata, _APP_AD_LANDING_ATTEMPT_COUNT)
        if isinstance(coverage_metadata, Mapping)
        else 0
    )
    # 广告源图／落地取证一生只调度一次；重复截会挤占底栏与覆盖巡检回合。
    if attempts > 0:
        return candidates
    eligible = tuple(
        candidate
        for candidate in candidates
        if candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and candidate.risk_level
        in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        and _is_dynamic_ad_destination_candidate(candidate)
        and _is_first_popup_ad_click_candidate(candidate)
        and not _has_forbidden_ad_landing_side_effect(candidate)
        and belongs_to_topmost_ad_popup(candidate, analysis)
    )
    selected = min(eligible, key=_first_popup_ad_click_priority, default=None)
    if selected is None:
        return candidates
    action_parameters = selected.metadata.get("action_parameters")
    signed_parameters = dict(action_parameters) if isinstance(action_parameters, Mapping) else {}
    signed_parameters.update(
        {
            "capture_ad_landing_evidence": True,
            "ad_landing_capability": "ad_outbound",
            "navigation_depth": selected.navigation_depth,
            "app_ad_landing_contract": _APP_AD_LANDING_CONTRACT_VERSION,
        }
    )
    signed = replace(
        selected,
        exploration_value=100,
        metadata={
            **dict(selected.metadata),
            "purpose": "点击首个弹窗广告并采集广告源图与浏览器落地页图",
            "frontier_scope": "local",
            "app_ad_landing_consistency": True,
            "action_parameters": signed_parameters,
        },
    )
    return (
        signed,
        *(candidate for candidate in candidates if candidate.candidate_id != selected.candidate_id),
    )


def _is_ad_popup_analysis(analysis: AppVisualAnalysis) -> bool:
    page_state = str(analysis.page_state or "").strip().casefold()
    if page_state in {"startup_ad", "countdown", "splash"}:
        return False
    return _is_popup_surface_analysis(analysis)


def _should_defer_ad_landing_until_home(
    *,
    analysis: AppVisualAnalysis,
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """覆盖目标要求 home 且尚未命中时，开屏／引导阶段不得先外跳取证。"""

    if not isinstance(coverage_metadata, Mapping):
        return False
    required = {
        str(item).strip().casefold()
        for item in coverage_metadata.get("required_page_types", ())
        if isinstance(item, str) and item.strip()
    }
    if "home" not in required:
        return False
    covered = {
        str(item).strip().casefold()
        for item in coverage_metadata.get("covered_page_types", ())
        if isinstance(item, str) and item.strip()
    }
    if "home" in covered:
        return False
    page_type = str(analysis.visual_page_type or "").strip().casefold()
    return page_type in {
        "startup_ad",
        "countdown",
        "splash",
        "guide_page",
        "line_loading",
        "",
    }


def _should_defer_ad_landing_for_incomplete_coverage(
    *,
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """覆盖分母仍有缺口时延后弹窗广告落地取证，避免重复广告截图占满回合。"""

    return coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=coverage_metadata,
    )


def _should_defer_ad_landing_for_uncovered_bottom_nav(
    *,
    analysis: AppVisualAnalysis,
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """兼容旧名：覆盖缺口未完即延后（不再要求当前帧已见底栏）。"""

    del analysis
    return _should_defer_ad_landing_for_incomplete_coverage(
        coverage_metadata=coverage_metadata,
    )


def _is_signed_app_ad_landing_candidate(candidate: CandidateAction) -> bool:
    parameters = candidate.metadata.get("action_parameters")
    return bool(
        candidate.metadata.get("app_ad_landing_consistency") is True
        and isinstance(parameters, Mapping)
        and parameters.get("capture_ad_landing_evidence") is True
        and parameters.get("ad_landing_capability") == "ad_outbound"
        and parameters.get("app_ad_landing_contract") == _APP_AD_LANDING_CONTRACT_VERSION
    )


def _has_forbidden_ad_landing_side_effect(candidate: CandidateAction) -> bool:
    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    return any(
        marker in terms
        for marker in (
            "下载",
            "安装",
            "充值",
            "支付",
            "购买",
            "开通会员",
            "download",
            "install",
            "purchase",
            "payment",
        )
    )


def _is_first_popup_ad_click_candidate(candidate: CandidateAction) -> bool:
    """只接受明确的广告 CTA 或主体，禁止把泛化外链入口误签为取证。"""

    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
        )
    ).casefold()
    return any(
        marker in terms
        for marker in (
            "广告点击入口",
            "广告cta",
            "广告按钮",
            "ad cta",
            "广告主体",
            "广告卡片",
            "广告图片",
            "ad body",
        )
    )


def _first_popup_ad_click_priority(candidate: CandidateAction) -> int:
    """优先点击真实 CTA；整块主体只作为无 CTA 时的保守回退。"""

    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
        )
    ).casefold()
    return (
        0
        if any(
            marker in terms
            for marker in ("广告点击入口", "广告cta", "广告按钮", "ad cta")
        )
        else 1
    )


def _curriculum_candidate_matches(
    *,
    candidate: CandidateAction,
    playbook,
    progress: AppCurriculumProgress,
) -> bool:
    """校验候选的动作类型、当前阶段语义和低风险触控边界。"""

    if (
        candidate.action_type
        in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }
        and candidate.risk_level is not InteractionRiskLevel.LOW
    ):
        return False
    terms = (
        candidate.semantic_target,
        str(candidate.metadata.get("purpose") or ""),
        str(candidate.metadata.get("expected_state_hint") or ""),
    )
    # 广告相关语义只禁止触控广告本体；SCROLL 是无目标、只读的截图动作，
    # 即使模型把当前区域描述为广告区域，也不能因此丢掉神器阶段的下滑候选。
    if candidate.action_type in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    } and _candidate_contains_ad_body_terms(terms):
        return False
    return curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type=candidate.action_type.value,
        semantic_terms=terms,
    )


def _is_curriculum_safe_override(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
    progress: AppCurriculumProgress,
) -> bool:
    """保留不应被课纲过滤吞掉的恢复、跳过和动态弹层关闭动作。"""

    if _is_signed_app_ad_landing_candidate(candidate):
        # alioth_17 的首个弹窗广告点击是一次性受控动作，必须先留存源图再
        # 跳转浏览器留存落地页；不能被业务课纲当成普通广告点击吞掉。
        return True

    if candidate.action_type is InteractionOperation.BACK:
        # 越出目标包后的恢复 BACK、广告关层 BACK，以及全产品 cloud_back
        # （底栏「<」）可绕过当前课纲；目标包内的通用 BACK 仍不得抢占。
        purpose = str(candidate.metadata.get("purpose") or "").casefold()
        return candidate.risk_level is InteractionRiskLevel.LOW and (
            candidate.metadata.get("package_boundary_recovery") is True
            or candidate.metadata.get("popup_back_recovery") is True
            or "底部导航栏" in candidate.semantic_target
            or "cloud_back" in purpose
        )
    if candidate.action_type is InteractionOperation.WARM_RESTORE:
        # 桌面热恢复服务端最低 MEDIUM；课纲放行须与 CommandPolicy 对齐。
        return candidate.risk_level is InteractionRiskLevel.MEDIUM and (
            candidate.metadata.get("package_boundary_recovery") is True
        )
    if _is_startup_semantic_skip_candidate(candidate) or (
        _should_offer_startup_semantic_skip(analysis)
        and (
            candidate_explicitly_targets_tap_skip(
                action_type=candidate.action_type.value,
                risk_level=candidate.risk_level.value,
                semantic_target=candidate.semantic_target,
                purpose=str(candidate.metadata.get("purpose") or ""),
                expected_state_hint=str(candidate.metadata.get("expected_state_hint") or ""),
            )
            or _is_safe_startup_exit_candidate(candidate=candidate, analysis=analysis)
        )
    ):
        return True
    if _is_coverage_page_seek_override(candidate=candidate, analysis=analysis):
        return True
    if _is_bottom_navigation_curriculum_override(candidate):
        return True
    return (
        _is_dynamic_popup_close_candidate(
            candidate=candidate,
            analysis=analysis,
        )
        or _is_dynamic_age_confirm_candidate(
            candidate=candidate,
            analysis=analysis,
        )
        or _is_dynamic_profile_credential_save_candidate(
            candidate=candidate,
            analysis=analysis,
        )
        or _is_hlw_content_detail_list_restore_candidate(
            candidate=candidate,
            analysis=analysis,
            progress=progress,
        )
    )


def _is_coverage_page_seek_override(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """保留覆盖寻页；弹层宿主已覆盖时也允许，避免公告关不完锁死探索。"""

    if candidate.metadata.get("coverage_page_seek") is not True:
        return False
    allowed_risk = {InteractionRiskLevel.LOW}
    if (
        candidate.metadata.get("home_category_grid_seek") is True
        or candidate.metadata.get("home_top_category_tab_seek") is True
        or candidate.metadata.get("bottom_navigation_tab_seek") is True
        or candidate.metadata.get("frontier_scope") == "app"
    ):
        # 底栏／分栏目／上方分类常被 Vision 标 medium，仍须可穿过课纲与安全过滤。
        allowed_risk.add(InteractionRiskLevel.MEDIUM)
    if (
        candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level not in allowed_risk
    ):
        return False
    page_state = str(analysis.page_state or "").casefold()
    if page_state in _COVERAGE_SEEK_BLOCKING_STATES:
        return False
    # 上方分类：当前帧仍有弹层／遮罩事实时不得借 coverage override 插队。
    if candidate.metadata.get("home_top_category_tab_seek") is True:
        from services.api.app.application.app_exploration_flow_contract import (
            analysis_blocks_home_top_category_tabs,
        )

        if analysis_blocks_home_top_category_tabs(analysis):
            return False
    return True


def _is_bottom_navigation_curriculum_override(candidate: CandidateAction) -> bool:
    """课纲停在关层阶段时，仍须保留真实底栏，避免永远点不到。"""

    if candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    if candidate.metadata.get("control_role") in {"dismiss", "skip"}:
        return False
    return (
        candidate.metadata.get("bottom_navigation_tab_seek") is True
        or candidate.metadata.get("frontier_scope") == "app"
    )

def _is_hlw_content_detail_list_restore_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
    progress: AppCurriculumProgress,
) -> bool:
    """福利页没有普通内容卡片时，只允许切回受信吃瓜列表继续详情课纲。"""

    if progress.playbook_id != "hlwapp" or progress.current_phase_id != "content_detail":
        return False
    controls = analysis.surface_facts.get("trusted_accessibility_controls")
    if not isinstance(controls, Mapping):
        return False
    content_card_count = controls.get("content_card_count")
    bottom_navigation_count = controls.get("bottom_navigation_count")
    if (
        controls.get("contract_id") != "hlwapp-accessibility.v1"
        or controls.get("package_name") != analysis.package_name
        or controls.get("page_type") != "content_list"
        or isinstance(content_card_count, bool)
        or content_card_count != 0
        or isinstance(bottom_navigation_count, bool)
        or not isinstance(bottom_navigation_count, int)
        or bottom_navigation_count < 1
    ):
        return False
    return (
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.semantic_target == "底栏「吃瓜」"
        and candidate.metadata.get("expected_state_hint") == "tab:吃瓜"
        and str(candidate.metadata.get("purpose") or "").startswith("打开目标包底栏 tab:吃瓜")
    )


def _is_startup_ad_analysis(analysis: AppVisualAnalysis) -> bool:
    states = {
        str(analysis.page_state or "").casefold(),
        str(analysis.visual_page_type or "").casefold(),
    }
    return bool(states & {"startup_ad", "countdown", "splash"})


def _looks_like_depth0_fullscreen_startup_promo(analysis: AppVisualAnalysis) -> bool:
    """根帧全屏推广／赌博开屏：Vision 常标 unknown／overlay，但仍须点右上「进入」。

    若误走 popup_chain 点系统「<」，开屏上 BACK 会直接退出到桌面。
    """

    page_type = str(analysis.visual_page_type or "").strip().casefold()
    page_state = str(analysis.page_state or "").strip().casefold()
    if page_type in {
        "home",
        "video_list",
        "content_list",
        "post_list",
        "activity",
        "profile",
        "player",
        "post_publish",
    }:
        return False
    if page_state in {
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
        "system_dialog",
        "permission_dialog",
    }:
        return False
    blob = " ".join(
        (
            page_type,
            page_state,
            str(analysis.visual_summary or ""),
            str(analysis.overlay_signature or ""),
            " ".join(str(item) for item in (analysis.key_elements or ())),
        )
    )
    # 真弹窗（宫格／下方圆 X）不是开屏。
    if any(
        marker in blob
        for marker in (
            "关闭弹层",
            "宫格广告",
            "圆形关闭",
            "bottom_close_x",
            "grid_ad_popup",
            "grid_promo_popup",
        )
    ):
        return False
    has_enter = any(
        marker in blob
        for marker in ("进入", "跳过", "skip", "开屏", "首屏", "startup", "splash")
    )
    has_fullscreen_promo = any(
        marker in blob
        for marker in ("全屏", "推广广告", "推广", "赌博", "博彩", "未见关闭")
    )
    # overlay／popup 标签但有进入／全屏推广：仍按开屏，禁止 cloud_back 退出桌面。
    if page_state in {"popup", "modal", "dialog", "overlay"}:
        return has_enter or has_fullscreen_promo
    if has_enter or has_fullscreen_promo:
        return True
    # 根帧页型未知且无业务页信号：按开屏兜底，避免只 OBSERVE 干等。
    return page_type in {"", "unknown", "none"} and page_state in {
        "",
        "unknown",
        "none",
    }


def _is_safe_startup_exit_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """仅放行当前开屏截图中明确进入目标 App 的低风险视觉候选。"""

    if (
        not _is_startup_ad_analysis(analysis)
        or candidate.action_type
        not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }
        or candidate.risk_level is not InteractionRiskLevel.LOW
    ):
        return False
    purpose = str(candidate.metadata.get("purpose") or "")
    expected_state_hint = str(candidate.metadata.get("expected_state_hint") or "")
    terms = (candidate.semantic_target, purpose, expected_state_hint)
    if _candidate_contains_ad_body_terms(terms):
        return False
    if expected_state_hint.strip().casefold() not in _SAFE_STARTUP_EXIT_HINTS:
        return False
    if candidate_explicitly_targets_tap_skip(
        action_type=candidate.action_type.value,
        risk_level=candidate.risk_level.value,
        semantic_target=candidate.semantic_target,
        purpose=purpose,
        expected_state_hint=expected_state_hint,
    ):
        return True
    normalized_target = candidate.semantic_target.strip().casefold()
    normalized_purpose = purpose.casefold()
    return normalized_target in _SAFE_STARTUP_EXIT_TARGETS and any(
        marker.casefold() in normalized_purpose for marker in _SAFE_STARTUP_EXIT_PURPOSE_MARKERS
    )


def _provider_degraded_empty_frame(analysis: AppVisualAnalysis) -> bool:
    """视觉 Provider 降级且页型/控件为空：常见于刚进开屏广告。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    quality = facts.get("visual_provider_quality")
    degraded = isinstance(quality, Mapping) and quality.get("status") == "degraded"
    if not degraded:
        return False
    empty_type = not str(analysis.visual_page_type or "").strip()
    unknown_state = str(analysis.page_state or "").strip().casefold() in {
        "",
        "unknown",
    }
    return empty_type and unknown_state


def _should_offer_startup_semantic_skip(
    analysis: AppVisualAnalysis,
    *,
    navigation_depth: int = 0,
) -> bool:
    """开屏已识别，或 depth0 降级／全屏推广未知帧（常卡在首屏「进入」）。"""

    if _is_startup_ad_analysis(analysis):
        return True
    # 仅根帧合成右上进入/跳过，避免中途降级空帧误点广告位。
    if navigation_depth > 0:
        return False
    if _provider_degraded_empty_frame(analysis):
        return True
    return _looks_like_depth0_fullscreen_startup_promo(analysis)


def _is_startup_semantic_skip_candidate(candidate: CandidateAction) -> bool:
    """合成或视觉签发的开屏进入/跳过；空页过滤与课纲必须放行。"""

    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    if candidate.risk_level is not InteractionRiskLevel.LOW:
        return False
    if candidate.metadata.get("startup_ad_skip_fallback") is True:
        return True
    if candidate.metadata.get("control_role") != "skip":
        return False
    return candidate_explicitly_targets_tap_skip(
        action_type=candidate.action_type.value,
        risk_level=candidate.risk_level.value,
        semantic_target=candidate.semantic_target,
        purpose=str(candidate.metadata.get("purpose") or ""),
        expected_state_hint=str(candidate.metadata.get("expected_state_hint") or ""),
    )


def _append_startup_ad_skip_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
) -> tuple[CandidateAction, ...]:
    """开屏无视觉跳过时补「进入/跳过」：优先 a11y，否则右上相对区合成兜底。"""

    if surface_kind is not SurfaceKind.ANDROID:
        return candidates
    if not _should_offer_startup_semantic_skip(
        analysis,
        navigation_depth=navigation_depth,
    ):
        return candidates
    if any(
        _is_startup_semantic_skip_candidate(candidate)
        or _is_safe_startup_exit_candidate(candidate=candidate, analysis=analysis)
        for candidate in candidates
    ):
        return candidates
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    trusted = facts.get("trusted_accessibility_skip")
    has_trusted = (
        isinstance(trusted, Mapping)
        and trusted.get("package_name") == analysis.package_name
    )
    bounds, used_default = default_startup_ad_skip_bounds(analysis)
    if has_trusted:
        exit_label = str(trusted.get("label") or "进入").strip() or "进入"
        semantic_target = f"跳过开屏：{exit_label}"
        purpose = (
            "点击目标包可访问性开屏退出控件（进入/跳过），离开首屏广告进入业务页"
        )
        discovery = (
            DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC
            if used_default
            else DISCOVERY_SOURCE_UIA_ISSUED
        )
        confidence = 0.82
    else:
        # 无障碍树也允许右上相对区：语义标记「进入/跳过」，禁止干等观察烧预算。
        exit_label = "进入/跳过"
        semantic_target = f"开屏进入：{exit_label}"
        purpose = (
            "视觉未给出进入/跳过时，按右上相对区点击开屏进入/跳过"
            "（禁止脚本 ENTRY_TAP、禁止 depth0 系统 BACK 退出 App）"
        )
        discovery = DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC
        used_default = True
        confidence = 0.72
    skip_contract = startup_ad_skip_contract(
        bounds=bounds,
        purpose=purpose,
        discovery_source=discovery,
        used_default_fixed_bounds=used_default,
    )
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=confidence,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="skip",
        expected_state_hint="tap_skip",
        discovery_source=discovery,
    )
    mapped = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata=tag_candidate_action_provenance(
            {
                "candidate_id": visual_candidate.candidate_id,
                "source_capture_id": analysis.capture_id,
                "control_role": "skip",
                "purpose": purpose,
                "frontier_scope": "local",
                "expected_state_hint": "tap_skip",
                "startup_ad_skip_fallback": True,
                "startup_ad_skip_contract": skip_contract,
                "semantic_markers": ["进入", "跳过", "skip", "entry"],
            },
            discovery_source=discovery,
            source_capture_id=analysis.capture_id,
            requires_fresh_capture_after_action=True,
        ),
    )
    return (mapped, *candidates)


def _is_cloud_back_forbidden_popup_x_candidate(candidate: CandidateAction) -> bool:
    """cloud_back 策略下禁止点弹层下方／右上 X（含 Vision 文案候选）。"""

    # BACK／cloud_back 本身就是关层策略，不得因 hint=close_popup_x 被误杀。
    if candidate.action_type is InteractionOperation.BACK:
        return False
    if candidate.metadata.get("popup_close_corner_fallback") is True:
        return True
    if candidate.metadata.get("popup_close_bottom_x_fallback") is True:
        return True
    if candidate.metadata.get("popup_close_pixel_visual") is True:
        return True
    hint = str(candidate.metadata.get("expected_state_hint") or "").casefold()
    if hint == "close_popup_x":
        return True
    target = candidate.semantic_target
    return "关闭弹层下方" in target or "关闭弹层右上" in target


def _resolve_package_popup_close_policy(package_name: str | None) -> str:
    """解析产品弹窗关层策略；缺省／异常时按 watcher_cascade。"""

    try:
        from services.api.app.domain.app_product.popup_close import (
            DEFAULT_POPUP_CLOSE_POLICY,
        )
        from services.api.app.domain.app_product.profiles import load_profile

        profile = load_profile(str(package_name or ""))
        if profile is None:
            return str(DEFAULT_POPUP_CLOSE_POLICY or "watcher_cascade")
        return str(profile.popup_close_policy or "watcher_cascade")
    except Exception:  # noqa: BLE001
        return "watcher_cascade"


def _package_uses_cloud_back_popup_policy(package_name: str | None) -> bool:
    """D 档关层回退：watcher_cascade 与遗留 cloud_back 均签发底栏「<」。"""

    policy = _resolve_package_popup_close_policy(package_name).casefold()
    return policy in {"watcher_cascade", "cloud_back", "cloud_back_twice"}


def _package_strips_popup_close_x_candidates(package_name: str | None) -> bool:
    """仅「纯 cloud_back、不做 cascade」时剥掉 ×；默认 cascade 保留 A/B。

    Profile 已将 cloud_back／cloud_back_twice 升为 watcher_cascade，
    因此此分支在现行产品配置下不会命中。
    """

    return _resolve_package_popup_close_policy(package_name).casefold() == "cloud_back"


def _is_popup_blocking_analysis(analysis: AppVisualAnalysis) -> bool:
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    page_state = str(analysis.page_state or "").casefold()
    # 显式弹层态（含开屏上方的版本提示／公告）优先：保留「下次再说」等关层。
    if page_state in {"popup", "modal", "overlay", "dialog", "system_dialog"}:
        return True
    # 纯开屏／首屏广告不是弹窗：禁止走弹层下方／右上 X（应点进入／跳过）。
    if _is_startup_ad_analysis(analysis):
        return False
    if bool(analysis.overlay_signature) or facts.get("popup_visible") is True:
        return True
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {
        "ad_popup",
        "promo_popup",
        "promo_ad",
        "modal",
        "dialog",
        "system_dialog",
        "age_verification",
        "age_confirm",
        "age_gate",
    }:
        return True
    topmost = facts.get("topmost_ad_popup")
    if isinstance(topmost, Mapping) and topmost:
        return True
    # Vision 常把挡路广告标成 home + ad_popup_*，仍须合成下方 X。
    blob = " ".join(
        (
            page_state,
            str(facts.get("page_subtype") or ""),
            str(facts.get("obstruction_kind") or ""),
            str(facts.get("overlay_kind") or ""),
        )
    ).casefold()
    return "ad_popup" in blob or "bottom_ad_banner" in blob


def _append_popup_close_corner_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
) -> tuple[CandidateAction, ...]:
    """弹层挡路时补齐关层；海角优先 APP JSON 下方 X，避免假「关闭」挤掉后只剩 BACK。"""

    from services.api.app.application.app_visual_close_calibration import (
        is_payment_or_paywall_surface,
    )

    if is_payment_or_paywall_surface(analysis):
        # 尊享卡／付费半屏无真 X；角落合成会瞎点抢购钮，交由 paywall BACK。
        return candidates
    # 开屏／depth0 降级空帧只走进入／跳过，绝不合成弹层下方 X。
    if _should_offer_startup_semantic_skip(
        analysis,
        navigation_depth=navigation_depth,
    ):
        return candidates
    if not _is_popup_blocking_analysis(analysis):
        return candidates
    filtered = tuple(
        item
        for item in candidates
        if _is_signed_app_ad_landing_candidate(item)
        or not _is_risky_promo_body_close_candidate(item)
    )
    # 纯 cloud_back（无 cascade）才剥掉 Vision／合成 ×，关窗只走底栏「<」。
    # 默认 watcher_cascade 保留 A 文案／B 几何安全 X，D 档仍由 enrich 注入底栏「<」。
    if _package_strips_popup_close_x_candidates(
        getattr(analysis, "package_name", None)
    ):
        return tuple(
            item
            for item in filtered
            if not _is_cloud_back_forbidden_popup_x_candidate(item)
        )
    # 仅当已有「带坐标」的下方／角落合成关层才跳过；纯文案「关闭」不够——
    # 几何过滤后常被刷掉，Frontier 只剩系统 BACK。
    if any(
        (
            candidate.metadata.get("popup_close_bottom_x_fallback") is True
            or candidate.metadata.get("popup_close_corner_fallback") is True
            or candidate.metadata.get("popup_close_pixel_visual") is True
        )
        and isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
        for candidate in filtered
    ):
        return filtered
    # 优先同帧像素校准圆 X／角 X；再才读产品 JSON／默认框。
    bounds = None
    zone_name = ""
    from_ui_tree = False
    from_pixel = False
    facts = (
        analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    )
    calibrated = facts.get("pixel_cross_close_calibration")
    if isinstance(calibrated, Mapping):
        raw_pixel = calibrated.get("normalized_bounds")
        if isinstance(raw_pixel, Mapping):
            try:
                bounds = NormalizedBounds.from_dict(raw_pixel)
                from_pixel = True
                zone_name = str(calibrated.get("close_kind") or "").strip() or "pixel_x"
            except Exception:  # noqa: BLE001
                bounds = None
    if bounds is None:
        # 本帧受信 UIA 关闭几何优先于产品 JSON 默认槽，避免无控件时瞎点角落。
        bounds = default_popup_close_bounds(analysis)
    if bounds is None:
        try:
            from services.api.app.application.app_ui_tree_features import (
                popup_ads_preferred_close_bounds,
                resolve_ui_tree_playbook_id,
            )

            package_name = str(getattr(analysis, "package_name", "") or "") or None
            # 未映射包（如 com.example.target）会落到 hjll 兜底框，误合成下方 X。
            if package_name and resolve_ui_tree_playbook_id(package_name=package_name):
                preferred = popup_ads_preferred_close_bounds(package_name=package_name)
                if preferred is not None:
                    zone_name, raw_bounds = preferred
                    bounds = NormalizedBounds.from_dict(raw_bounds)
                    from_ui_tree = True
        except Exception:  # noqa: BLE001
            bounds = None
    if bounds is None:
        return filtered
    if from_pixel:
        bottom_x = "bottom" in zone_name or zone_name == "bottom_circular_x"
        if not bottom_x:
            cy = (bounds.top + bounds.bottom) / 2.0
            bottom_x = cy >= 0.52
        semantic_target = "关闭弹层下方X" if bottom_x else "关闭弹层右上X"
        purpose = (
            "点弹层像素检出关闭控件（pixel_cross_close_calibration），"
            "立即关闭并截图；禁止只观察／点广告本体"
        )
    elif from_ui_tree:
        semantic_target = "关闭弹层下方X"
        purpose = (
            f"点弹层下方关闭控件（ui_tree:{zone_name or 'bottom_x'}），"
            "逐层截图关闭；禁止中部假 X／系统 BACK 逃逸"
        )
    else:
        semantic_target = "关闭弹层右上X"
        purpose = "点弹层右上／角落关闭控件解除遮挡（合成兜底，禁止点广告本体）"
    close_contract = popup_close_corner_contract(
        bounds=bounds,
        purpose=purpose,
        confidence=0.95 if from_pixel else (0.88 if from_ui_tree else 0.82),
    )
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.95 if from_pixel else (0.88 if from_ui_tree else 0.82),
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="dismiss",
        expected_state_hint="close_popup_x",
    )
    mapped = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "source_capture_id": analysis.capture_id,
            "control_role": "dismiss",
            "obstruction_control_identity": (
                build_visual_obstruction_control_identity(visual_candidate)
            ),
            "source_obstruction_identity": build_app_obstruction_identity(analysis),
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "close_popup_x",
            # geometry_ok／Runtime 都读顶层 normalized_bounds；合同内嵌不够。
            "normalized_bounds": bounds.to_dict(),
            "popup_close_corner_fallback": True,
            "popup_close_bottom_x_fallback": from_ui_tree
            or (from_pixel and "bottom" in zone_name)
            or (from_pixel and (bounds.top + bounds.bottom) / 2.0 >= 0.52),
            "popup_close_pixel_visual": from_pixel,
            "popup_close_zone_name": zone_name or None,
            "popup_close_corner_contract": close_contract,
        },
    )
    signed = tuple(
        candidate
        for candidate in filtered
        if _is_signed_app_ad_landing_candidate(candidate)
    )
    remaining = tuple(
        candidate
        for candidate in filtered
        if not _is_signed_app_ad_landing_candidate(candidate)
    )
    # 受控广告取证必须排在合成关闭动作之前；否则 Frontier 即使保留了
    # 广告主体，也可能先关弹窗而永久丢失首个广告样本。
    return (*signed, mapped, *remaining)


def _profile_credential_observation_blob(
    analysis: AppVisualAnalysis,
    candidates: tuple[CandidateAction, ...] = (),
) -> str:
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    extra = " ".join(
        " ".join(
            (
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        )
        for candidate in candidates
    )
    return " ".join(
        str(part or "")
        for part in (
            analysis.visual_summary,
            analysis.visual_page_type,
            analysis.page_state,
            facts.get("dialog_kind"),
            facts.get("page_subtype"),
            " ".join(str(item) for item in (analysis.key_elements or ())),
            extra,
        )
    )


def _is_dynamic_profile_credential_save_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """课纲过滤时保留账号凭证「保存凭证」，避免被关层／我已保存挤掉。"""

    from services.api.app.application.app_ui_tree_features import (
        candidate_matches_profile_credential_save,
    )

    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    package_name = str(getattr(analysis, "package_name", None) or "") or None
    if candidate.metadata.get("profile_credential_save") is True:
        return True
    return candidate_matches_profile_credential_save(
        candidate,
        package_name=package_name,
    )


def _append_profile_credential_save_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None = None,
) -> tuple[CandidateAction, ...]:
    """我的页账号凭证：先提升「保存凭证」；保存成功后再提升「我已保存」。"""

    from services.api.app.application.app_semantic_progress import (
        PROFILE_CREDENTIAL_SAVE_DONE_KEY,
    )
    from services.api.app.application.app_synthetic_touch_contracts import (
        profile_credential_dismiss_contract,
    )
    from services.api.app.application.app_ui_tree_features import (
        candidate_matches_profile_credential_save,
        candidate_matches_profile_credential_skip,
        observation_matches_profile_credential,
        profile_credential_save_bounds,
        profile_credential_save_phase_ready,
        profile_credential_skip_bounds,
    )

    package_name = str(analysis.package_name or "") or None
    blob = _profile_credential_observation_blob(analysis, candidates)
    on_credential = observation_matches_profile_credential(
        page_type=analysis.visual_page_type,
        page_state=analysis.page_state,
        observation_text=blob,
        package_name=package_name,
    )
    has_save = any(
        candidate_matches_profile_credential_save(candidate, package_name=package_name)
        for candidate in candidates
    )
    has_skip = any(
        candidate_matches_profile_credential_skip(candidate, package_name=package_name)
        for candidate in candidates
    )
    if not on_credential and not has_save and not has_skip:
        return candidates

    prior_done = False
    prior_attempts = 0
    if isinstance(coverage_metadata, Mapping):
        prior_done = coverage_metadata.get(PROFILE_CREDENTIAL_SAVE_DONE_KEY) is True
        from services.api.app.application.app_semantic_progress import (
            PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY,
        )

        raw_attempts = coverage_metadata.get(PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY)
        if (
            isinstance(raw_attempts, int)
            and not isinstance(raw_attempts, bool)
            and raw_attempts > 0
        ):
            prior_attempts = raw_attempts
    save_done = profile_credential_save_phase_ready(
        save_done_flag=prior_done,
        save_attempt_count=prior_attempts,
        observation_text=blob,
        package_name=package_name,
    )

    if save_done:
        # 第二步：提升「我已保存」，可去掉保存钮以免重复点。
        kept = tuple(
            candidate
            for candidate in candidates
            if not candidate_matches_profile_credential_save(
                candidate,
                package_name=package_name,
            )
        )
        preferred = profile_credential_skip_bounds(package_name=package_name)
        zone_name = None
        bounds = None
        contract = None
        purpose = "账号凭证已保存成功；点灰色「我已保存」关闭弹层"
        if preferred is not None:
            zone_name, raw_bounds = preferred
            bounds = NormalizedBounds.from_dict(raw_bounds)
            purpose = (
                f"账号凭证已保存成功；点灰色「我已保存」（ui_tree:{zone_name}）关闭弹层"
            )
            contract = profile_credential_dismiss_contract(
                bounds=bounds, purpose=purpose
            )
        existing_skip = [
            candidate
            for candidate in kept
            if candidate_matches_profile_credential_skip(
                candidate,
                package_name=package_name,
            )
            or candidate.metadata.get("profile_credential_dismiss") is True
        ]
        if existing_skip:
            promoted = []
            for candidate in existing_skip:
                metadata = dict(candidate.metadata)
                metadata["profile_credential_dismiss"] = True
                metadata["control_role"] = "profile_credential_dismiss"
                metadata["expected_state_hint"] = "dismiss_profile_credential"
                metadata["purpose"] = purpose
                # Vision 框常偏高点空；强制 UI Tree 实测框＋合同。
                if bounds is not None and contract is not None:
                    metadata["normalized_bounds"] = bounds.to_dict()
                    metadata["profile_credential_dismiss_contract"] = contract
                    if zone_name:
                        metadata["ui_tree_zone"] = zone_name
                promoted.append(
                    replace(
                        candidate,
                        semantic_target="我已保存",
                        exploration_value=max(candidate.exploration_value, 100),
                        risk_level=InteractionRiskLevel.LOW,
                        metadata=metadata,
                    )
                )
            promoted_ids = {item.candidate_id for item in promoted}
            others = tuple(
                item for item in kept if item.candidate_id not in promoted_ids
            )
            return (*promoted, *others)
        if preferred is None or bounds is None or contract is None:
            return kept
        mapped = CandidateAction(
            candidate_id=f"forced-profile-credential-dismiss:{analysis.capture_id}",
            semantic_target="我已保存",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=100,
            navigation_depth=navigation_depth,
            expected_surface_kind=surface_kind,
            metadata={
                "source_capture_id": analysis.capture_id,
                "control_role": "profile_credential_dismiss",
                "purpose": purpose,
                "frontier_scope": "local",
                "expected_state_hint": "dismiss_profile_credential",
                "normalized_bounds": bounds.to_dict(),
                "profile_credential_dismiss": True,
                "profile_credential_dismiss_contract": contract,
            },
        )
        return (mapped, *kept)

    # 第一步：提升「保存凭证」，暂时剥掉「我已保存」防抢点。
    if not has_save and not on_credential:
        return candidates
    kept = tuple(
        candidate
        for candidate in candidates
        if not candidate_matches_profile_credential_skip(
            candidate,
            package_name=package_name,
        )
    )
    preferred_save = profile_credential_save_bounds(package_name=package_name)
    save_zone_name = None
    save_bounds = None
    save_contract = None
    save_purpose = (
        "点账号凭证弹层蓝色「保存凭证」；保存成功后再点「我已保存」"
    )
    if preferred_save is not None:
        save_zone_name, raw_save_bounds = preferred_save
        save_bounds = NormalizedBounds.from_dict(raw_save_bounds)
        save_purpose = (
            f"点账号凭证弹层蓝色「保存凭证」（ui_tree:{save_zone_name}）；"
            "保存成功后再点「我已保存」"
        )
        save_contract = profile_credential_save_contract(
            bounds=save_bounds, purpose=save_purpose
        )
    existing = [
        candidate
        for candidate in kept
        if _is_dynamic_profile_credential_save_candidate(
            candidate=candidate,
            analysis=analysis,
        )
        or candidate_matches_profile_credential_save(
            candidate,
            package_name=package_name,
        )
    ]
    if existing:
        promoted = []
        for candidate in existing:
            metadata = dict(candidate.metadata)
            metadata["profile_credential_save"] = True
            metadata["control_role"] = "profile_credential_confirm"
            metadata["expected_state_hint"] = "save_profile_credential"
            metadata["purpose"] = save_purpose
            if save_bounds is not None and save_contract is not None:
                metadata["normalized_bounds"] = save_bounds.to_dict()
                metadata["profile_credential_save_contract"] = save_contract
                if save_zone_name:
                    metadata["ui_tree_zone"] = save_zone_name
            promoted.append(
                replace(
                    candidate,
                    semantic_target="保存凭证",
                    exploration_value=max(candidate.exploration_value, 100),
                    risk_level=InteractionRiskLevel.LOW,
                    metadata=metadata,
                )
            )
        promoted_ids = {item.candidate_id for item in promoted}
        others = tuple(
            item for item in kept if item.candidate_id not in promoted_ids
        )
        return (*promoted, *others)
    preferred = profile_credential_save_bounds(package_name=package_name)
    if preferred is None:
        return kept
    zone_name, raw_bounds = preferred
    bounds = NormalizedBounds.from_dict(raw_bounds)
    semantic_target = "保存凭证"
    purpose = (
        f"点账号凭证弹层蓝色「保存凭证」（ui_tree:{zone_name}）；"
        "保存成功后再点「我已保存」"
    )
    contract = profile_credential_save_contract(bounds=bounds, purpose=purpose)
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.9,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="profile_credential_confirm",
        expected_state_hint="save_profile_credential",
    )
    mapped = CandidateAction(
        candidate_id=f"forced-profile-credential-save:{analysis.capture_id}",
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "source_capture_id": analysis.capture_id,
            "control_role": "profile_credential_confirm",
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "save_profile_credential",
            "normalized_bounds": bounds.to_dict(),
            "profile_credential_save": True,
            "profile_credential_save_contract": contract,
            "visual_candidate_id": visual_candidate.candidate_id,
        },
    )
    return (mapped, *kept)


def _looks_like_explicit_close_control(candidate: CandidateAction) -> bool:
    if is_visual_obstruction_control(candidate):
        return True
    joined = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    markers = (
        "close_popup_x",
        "关闭弹层右上",
        "关闭弹层下方",
        "右上关闭",
        "右上角关闭",
        "关闭推广弹窗",
        "关闭神器甄选",
        "点x",
        "点×",
        "点击x",
        "点击×",
        "白圈x",
        "关闭按钮",
    )
    return any(marker in joined for marker in markers)


def _is_risky_promo_body_close_candidate(candidate: CandidateAction) -> bool:
    """拒绝易点进广告本体／外链的“关弹层”语义（无明确 X 控件）。"""

    if is_visual_obstruction_control(candidate):
        return False

    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    if candidate.metadata.get("popup_close_corner_fallback") is True:
        return False
    if _looks_like_explicit_close_control(candidate):
        return False
    target = str(candidate.semantic_target or "").casefold()
    purpose = str(candidate.metadata.get("purpose") or "").casefold()
    risky_targets = (
        "close_promo_popup",
        "close_bottom_promo",
        "close_bottom_banner_ad",
        "close_promo_overlay",
        "close_announcement",
    )
    if target in risky_targets:
        return True
    if "推广" in purpose and ("弹层" in purpose or "弹窗" in purpose):
        if "广告本体" not in purpose and "x" not in purpose and "×" not in purpose:
            # 用途写“关闭推广弹层”但控件名未标明 X，视为高风险居中点击
            return "关闭" in purpose
    return False


def _is_dynamic_popup_close_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    if (
        candidate.action_type
        not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }
        or candidate.risk_level is not InteractionRiskLevel.LOW
    ):
        return False
    if not _is_popup_blocking_analysis(analysis):
        return False
    if is_visual_obstruction_control(candidate):
        return True
    terms = (
        candidate.semantic_target,
        str(candidate.metadata.get("purpose") or ""),
        str(candidate.metadata.get("expected_state_hint") or ""),
    )
    if _candidate_contains_ad_body_terms(terms):
        return False
    if _is_risky_promo_body_close_candidate(candidate):
        return False
    if candidate.metadata.get("popup_close_corner_fallback") is True:
        return True
    close_markers = (
        "close_popup",
        "close_overlay",
        "close_popup_x",
        "dismiss update prompt",
        "dismiss popup",
        "dismiss overlay",
        "not now",
        "关闭升级提示",
        "关闭更新提示",
        "关闭弹窗",
        "关闭弹层",
        "关闭弹层右上",
        "关闭弹层下方",
        "关闭当前遮挡",
        "关闭遮挡",
        "关闭中间层",
        "关闭推广弹窗",
        "关闭广告弹窗",
        "关闭神器甄选",
        "弹层关闭",
        "下次再说",
        "以后再说",
        "暂不更新",
        "稍后",
    )
    joined = " ".join(terms).casefold()
    return any(marker.casefold() in joined for marker in close_markers)


def _is_dynamic_age_confirm_candidate(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
) -> bool:
    """课纲过滤时保留年龄确认进入，避免卡在 auth_gate。"""

    if not observation_needs_age_confirm(
        page_state=analysis.page_state,
        page_type=analysis.visual_page_type,
        surface_facts=analysis.surface_facts,
    ):
        return False
    return is_age_confirm_candidate(
        action_type=candidate.action_type,
        risk_level=candidate.risk_level,
        semantic_target=candidate.semantic_target,
        purpose=candidate.metadata.get("purpose"),
        expected_state_hint=candidate.metadata.get("expected_state_hint"),
    )


def _candidate_contains_ad_body_terms(terms: tuple[str, ...]) -> bool:
    joined = " ".join(terms).casefold()
    # 可信关闭候选会把“不点击广告主体”写进安全用途。先移除这些明确的
    # 否定性约束，避免其中的“点击广告”反向命中广告主体拦截；若同一句
    # 仍包含真正的广告链接／图片点击，后续 marker 仍会继续拒绝。
    for guard in (
        "不点击广告主体",
        "不点击广告本体",
        "不要点击广告主体",
        "不要点击广告本体",
        "不得点击广告主体",
        "不得点击广告本体",
        "禁止点击广告主体",
        "禁止点击广告本体",
        "避免点击广告主体",
        "避免点击广告本体",
        "do not click ad body",
        "don't click ad body",
        "never click ad body",
    ):
        joined = joined.replace(guard, "")
    markers = (
        "广告本体",
        "广告内容",
        "广告链接",
        "广告区域",
        "广告图片",
        "广告卡片",
        "点击广告",
        "ad body",
        "ad content",
        "ad link",
        "ad area",
        "ad image",
        "ad card",
        "click ad",
    )
    return any(marker.casefold() in joined for marker in markers)


def _append_curriculum_observation_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """为课纲要求的零触控阶段补充确定性 OBSERVE，避免视觉点击候选卡住游标。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    raw_progress = coverage_metadata.get("app_curriculum_progress")
    if not isinstance(raw_progress, Mapping):
        return candidates
    try:
        progress = AppCurriculumProgress.from_dict(raw_progress)
        playbook = resolve_playbook_by_id(progress.playbook_id)
        if playbook is None or progress.complete or progress.current_phase_id is None:
            return candidates
        phases = {str(item["phase_id"]): item for item in build_script_curriculum_phases(playbook)}
        phase = phases.get(progress.current_phase_id)
        if phase is None:
            return candidates
        prefer_hints = tuple(
            str(item).strip() for item in phase.get("prefer_hints", ()) if str(item).strip()
        )
        semantic_terms = (
            progress.current_phase_id,
            str(phase.get("title") or ""),
            *prefer_hints,
        )
        if not curriculum_action_matches_current_phase(
            playbook=playbook,
            progress=progress,
            action_type=InteractionOperation.OBSERVE.value,
            semantic_terms=semantic_terms,
        ):
            return candidates
    except (KeyError, TypeError, ValueError):
        return candidates

    for candidate in candidates:
        if candidate.action_type is not InteractionOperation.OBSERVE:
            continue
        candidate_terms = (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
        if curriculum_action_matches_current_phase(
            playbook=playbook,
            progress=progress,
            action_type=candidate.action_type.value,
            semantic_terms=candidate_terms,
        ):
            return candidates

    phase_id = progress.current_phase_id
    phase_action_count = progress.current_phase_action_count
    semantic_target = f"课纲零触控观察：{phase_id}：第{phase_action_count + 1}帧"
    purpose = f"按课纲推进 {phase_id}，只观察当前屏幕，不执行触控"
    visual_candidate = AppVisualCandidate.build(
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
    observation = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": " ".join((phase_id, *prefer_hints)),
            "curriculum_observation": {
                "protocol_version": APP_CURRICULUM_OBSERVATION_VERSION,
                "phase_id": phase_id,
                "phase_action_count": phase_action_count,
            },
        },
    )
    # 当前阶段明确要求零触控时优先交给 Explorer，避免高价值视觉点击候选长期抢占课纲游标。
    return (observation, *candidates)


def _append_home_popup_sequence_observation_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """关闭首页弹窗后先观察下一帧；清空后再做一次连续观察。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    watch = str(coverage_metadata.get("home_popup_sequence_watch") or "")
    if watch not in {"next", "cleared"}:
        return candidates
    page_type = str(analysis.visual_page_type or "").strip().casefold()
    if page_type not in {"home", ""}:
        return candidates
    if watch == "next":
        semantic_target = "首页弹窗关闭后观察下一帧"
        purpose = "关闭首页弹窗后观察下一弹窗是否立即出现，不执行触控"
    else:
        semantic_target = "首页弹窗清空后连续观察"
        purpose = "弹窗清空后再观察一帧，确认没有稍后载入的弹窗"
    visual_candidate = AppVisualCandidate.build(
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
        expected_state_hint="home_popup_sequence",
    )
    observation = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "home_popup_sequence",
            "home_popup_sequence_observe": True,
            "home_popup_sequence_watch": watch,
        },
    )
    return (observation, *candidates)


def _append_dynamic_startup_observation_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """动态启动阻断页即使 Provider 漏报候选，也要保留有界零触控观察。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    if isinstance(coverage_metadata.get("app_curriculum_progress"), Mapping):
        return candidates
    if not _is_dynamic_startup_blocker(analysis):
        return candidates
    raw_count = coverage_metadata.get(_DYNAMIC_STARTUP_OBSERVATION_COUNT, 0)
    observation_count = (
        raw_count
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
        else 0
    )
    # 开屏／首屏：满 2 帧证据后不再补 OBSERVE，交给进入／跳过点击。
    observe_limit = (
        2
        if _is_startup_ad_analysis(analysis)
        else APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT
    )
    if observation_count >= observe_limit:
        return candidates
    if any(
        candidate.action_type in {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}
        for candidate in candidates
    ):
        return candidates

    page_hint = str(
        analysis.visual_page_type or analysis.page_state or "startup_transition"
    ).strip()
    semantic_target = f"动态启动零触控观察：第{observation_count + 1}帧"
    purpose = (
        "开屏未满 2 帧证据，先零触控观察再点进入/跳过"
        if _is_startup_ad_analysis(analysis)
        else "等待线路检测或启动表面自然变化，不执行任何触控"
    )
    visual_candidate = AppVisualCandidate.build(
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
    observation = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": page_hint,
            "dynamic_startup_observation": {
                "protocol_version": APP_DYNAMIC_STARTUP_OBSERVATION_VERSION,
                "observation_count": observation_count,
                "observation_limit": observe_limit,
            },
            "startup_connection_recovery_evidence": (
                _has_connection_recovery_surface_evidence(analysis)
            ),
        },
    )
    return (observation, *candidates)


def _append_unstable_page_reobserve_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """视觉页型为空时补有界零触控观察，避免 Frontier 直接耗尽。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    if isinstance(coverage_metadata.get("app_curriculum_progress"), Mapping):
        return candidates
    if not _is_unstable_page_analysis(analysis):
        return candidates
    # depth0 视觉降级空帧：交给开屏语义进入/跳过，禁止 OBSERVE 抢占并烧预算。
    if (
        surface_kind is SurfaceKind.ANDROID
        and navigation_depth <= 0
        and _provider_degraded_empty_frame(analysis)
    ):
        return candidates
    reobserve_count = _nonnegative_metadata_counter(
        coverage_metadata,
        _DYNAMIC_UNSTABLE_PAGE_REOBSERVE_COUNT,
    )
    if reobserve_count >= APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_LIMIT:
        return candidates
    if any(
        candidate.action_type in {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}
        for candidate in candidates
    ):
        return candidates

    page_hint = str(analysis.page_state or "unknown_page").strip() or "unknown_page"
    semantic_target = f"不稳定页零触控观察：第{reobserve_count + 1}帧"
    purpose = "等待视觉页型稳定，不执行任何触控"
    visual_candidate = AppVisualCandidate.build(
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
    observation = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": page_hint,
            "dynamic_unstable_page_reobserve": {
                "protocol_version": APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_VERSION,
                "reobserve_count": reobserve_count,
                "reobserve_limit": APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_LIMIT,
            },
        },
    )
    return (observation, *candidates)


def _append_unstable_page_back_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """不稳定页观察达上限后签发有界 BACK，避免空候选导致过早收口。"""

    if surface_kind is not SurfaceKind.ANDROID:
        return candidates
    if navigation_depth <= 0:
        return candidates
    if not isinstance(coverage_metadata, Mapping):
        return candidates
    if isinstance(coverage_metadata.get("app_curriculum_progress"), Mapping):
        return candidates

    startup_blocker = _is_dynamic_startup_blocker(analysis)
    unstable = _is_unstable_page_analysis(analysis)
    if not startup_blocker and not unstable:
        return candidates

    startup_observation_count = _nonnegative_metadata_counter(
        coverage_metadata,
        _DYNAMIC_STARTUP_OBSERVATION_COUNT,
    )
    back_count = _nonnegative_metadata_counter(
        coverage_metadata,
        _DYNAMIC_UNSTABLE_PAGE_BACK_COUNT,
    )
    if back_count >= APP_DYNAMIC_UNSTABLE_PAGE_BACK_LIMIT:
        return candidates

    needs_startup_back = (
        startup_blocker
        and startup_observation_count >= APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT
        and str(analysis.visual_page_type or "").strip().casefold()
        in {
            "line_loading",
            "line_failure",
            "connection_failed",
            "connectivity_gate",
            "network_gate",
            "connecting",
            "startup_connection",
        }
    )
    # 空页型禁止系统 BACK：实测会退出 App／重走开屏，覆盖率更差；改为继续观察由上层预算收口
    needs_unstable_back = False
    if not needs_startup_back and not needs_unstable_back:
        return candidates
    if any(candidate.action_type is InteractionOperation.BACK for candidate in candidates):
        return candidates

    semantic_target = "系统 BACK 恢复稳定页"
    purpose = (
        "启动／加载观察已达上限，通过系统 BACK 返回上一级并重新截图"
        if needs_startup_back
        else "不稳定页观察已达上限，通过系统 BACK 返回上一级并重新截图"
    )
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.BACK,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="back",
    )
    back = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "back",
            "unstable_page_back_recovery": True,
            "dynamic_unstable_page_back": {
                "protocol_version": APP_DYNAMIC_UNSTABLE_PAGE_BACK_VERSION,
                "back_count": back_count,
                "back_limit": APP_DYNAMIC_UNSTABLE_PAGE_BACK_LIMIT,
            },
        },
    )
    return (back, *candidates)


def _filter_unstable_page_navigation_candidates(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
) -> tuple[CandidateAction, ...]:
    """仅在视觉页型缺失时禁止业务点击；loading 列表交给 coverage seek 既有闸门。"""

    if not _is_unstable_page_analysis(analysis):
        return candidates
    return tuple(
        candidate
        for candidate in candidates
        if candidate.action_type
        in {
            InteractionOperation.OBSERVE,
            InteractionOperation.TIMED_CAPTURE,
            InteractionOperation.BACK,
            InteractionOperation.WARM_RESTORE,
        }
        or _is_startup_semantic_skip_candidate(candidate)
        or _is_dynamic_popup_close_candidate(candidate=candidate, analysis=analysis)
        or _is_dynamic_age_confirm_candidate(candidate=candidate, analysis=analysis)
    )


def _append_curriculum_scroll_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """视觉模型漏报滚动时补发无坐标安全候选，并签发可恢复身份。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    raw_progress = coverage_metadata.get("app_curriculum_progress")
    if not isinstance(raw_progress, Mapping):
        return candidates
    try:
        progress = AppCurriculumProgress.from_dict(raw_progress)
        playbook = resolve_playbook_by_id(progress.playbook_id)
        if playbook is None or progress.complete or progress.current_phase_id is None:
            return candidates
        phases = {str(item["phase_id"]): item for item in build_script_curriculum_phases(playbook)}
        phase = phases.get(progress.current_phase_id)
        if phase is None:
            return candidates
        prefer_hints = tuple(
            str(item).strip() for item in phase.get("prefer_hints", ()) if str(item).strip()
        )
        phase_terms = (progress.current_phase_id, str(phase.get("title") or ""), *prefer_hints)
        if not curriculum_action_matches_current_phase(
            playbook=playbook,
            progress=progress,
            action_type=InteractionOperation.SCROLL.value,
            semantic_terms=phase_terms,
        ):
            return candidates
    except (KeyError, TypeError, ValueError):
        return candidates

    for candidate in candidates:
        if candidate.action_type is not InteractionOperation.SCROLL:
            continue
        candidate_terms = (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
        if curriculum_action_matches_current_phase(
            playbook=playbook,
            progress=progress,
            action_type=InteractionOperation.SCROLL.value,
            semantic_terms=candidate_terms,
        ):
            return candidates

    # 普通业务阶段遇到遮挡时先关闭再继续滚动；但 HLW 神器阶段第 2 步
    # 本身就是在当前弹层内执行受控下滑，不能因关闭候选存在而吞掉课纲动作。
    if progress.current_phase_id != "shenqi" and any(
        _is_dynamic_popup_close_candidate(candidate=candidate, analysis=analysis)
        for candidate in candidates
    ):
        return candidates

    phase_id = progress.current_phase_id
    phase_action_count = progress.current_phase_action_count
    semantic_target = f"课纲安全下滑：{phase_id}：第{phase_action_count + 1}帧"
    purpose = f"按课纲推进 {phase_id}，只执行安全下滑并重新截图"
    # Runtime 必须能独立重建候选身份；这里使用固定 phase id
    # 作为规范 hint，不把可变的提示词列表纳入签发协议。
    expected_state_hint = phase_id
    visual_candidate = AppVisualCandidate.build(
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
    fallback = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": expected_state_hint,
            "curriculum_action": {
                "protocol_version": APP_CURRICULUM_ACTION_VERSION,
                "phase_id": phase_id,
                "phase_action_count": phase_action_count,
                "action_type": InteractionOperation.SCROLL.value,
                "expected_state_hint": expected_state_hint,
            },
        },
    )
    return (fallback, *candidates)


def _append_dynamic_scroll_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """动态模式为每个已发现业务作用域补齐最多两帧通用下滑采样。"""

    if not isinstance(coverage_metadata, Mapping):
        return candidates
    if isinstance(coverage_metadata.get("app_curriculum_progress"), Mapping):
        return candidates
    if not _is_stable_business_page(analysis):
        return candidates
    page_type = str(analysis.visual_page_type or "").strip().casefold()
    scope = str(coverage_metadata.get("business_page_scope") or "app_ready").strip().casefold()
    purpose = f"动态采样当前业务页面下一屏；scope={scope}；page_type={page_type}"
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name="动态页面安全下滑",
        action_type=InteractionOperation.SCROLL,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=page_type,
    )
    fallback = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=visual_candidate.semantic_name,
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": page_type,
            "expected_next_page_type": page_type,
            "dynamic_page_sampling": True,
            "dynamic_page_sampling_contract": {
                "protocol_version": APP_DYNAMIC_PAGE_SAMPLING_VERSION,
                "business_page_scope": scope,
                "page_type": page_type,
            },
        },
    )
    counts = parse_app_semantic_action_counts(coverage_metadata.get(APP_SEMANTIC_ACTION_COUNTS))
    page_identity = coverage_metadata.get("business_page_identity")
    if not isinstance(page_identity, str):
        page_identity = None
    scoped_key = build_app_semantic_action_key(
        fallback,
        page_identity=page_identity,
    )
    legacy_key = build_app_semantic_action_key(fallback)
    if max(counts.get(scoped_key, 0), counts.get(legacy_key, 0)) >= 2:
        return tuple(
            item for item in candidates if item.action_type is not InteractionOperation.SCROLL
        )
    # Provider 自由生成的滚动文案不稳定，统一替换为可跨截图计数的规范候选。
    non_scroll = tuple(
        item for item in candidates if item.action_type is not InteractionOperation.SCROLL
    )
    # 业务跳转优先；规范滚动作为当前页的兜底分支，既保证两帧采样，又不抢占
    # 已发现的详情、Tab 等高价值导航入口。
    return (*non_scroll, fallback)


def _append_package_boundary_back_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    location: str,
    allowed_hosts: tuple[str, ...],
) -> tuple[CandidateAction, ...]:
    """离开目标包后只保留包边界恢复动作。"""

    if app_location_within_package_boundary(
        location=location,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
    ):
        return candidates
    external_kind = classify_external_activity(
        package_name=analysis.package_name,
        activity=analysis.activity,
    )
    recovery_meta = _package_boundary_recovery_metadata(
        kind=external_kind,
        package_name=analysis.package_name,
    )
    # 浏览器等外部包使用 BACK；系统桌面上的 BACK 无法重新打开目标 App，
    # 必须改用平台签发的 warm_restore。两者都保留当前视觉候选原 ID。
    if _is_android_launcher_analysis(analysis):
        visual_candidate = _build_launcher_warm_restore_visual_candidate(analysis)
        warm = CandidateAction(
            candidate_id=visual_candidate.candidate_id,
            semantic_target=visual_candidate.semantic_name,
            action_type=InteractionOperation.WARM_RESTORE,
            risk_level=InteractionRiskLevel.MEDIUM,
            exploration_value=100,
            navigation_depth=navigation_depth,
            expected_surface_kind=surface_kind,
            metadata={
                "candidate_id": visual_candidate.candidate_id,
                "purpose": visual_candidate.purpose,
                "frontier_scope": "local",
                "expected_state_hint": "app_ready",
                **recovery_meta,
                "action_parameters": {
                    "package_name": _target_package(allowed_hosts),
                },
            },
        )
        return (warm,)
    recovery_candidates = tuple(
        item
        for item in candidates
        if item.action_type in {InteractionOperation.BACK, InteractionOperation.WARM_RESTORE}
    )
    if recovery_candidates:
        return tuple(
            replace(
                item,
                risk_level=(
                    InteractionRiskLevel.MEDIUM
                    if item.action_type is InteractionOperation.WARM_RESTORE
                    else item.risk_level
                ),
                metadata={
                    **dict(item.metadata),
                    "frontier_scope": "local",
                    "expected_state_hint": (
                        "app_ready"
                        if item.action_type is InteractionOperation.WARM_RESTORE
                        else "back"
                    ),
                    **recovery_meta,
                    **(
                        {
                            "action_parameters": {
                                "package_name": _target_package(allowed_hosts),
                            }
                        }
                        if item.action_type is InteractionOperation.WARM_RESTORE
                        else {}
                    ),
                },
            )
            for item in recovery_candidates
        )
    visual_candidate = _build_package_boundary_back_visual_candidate(
        analysis,
        kind=external_kind,
    )
    back = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=visual_candidate.semantic_name,
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        # Frontier 快照约定 exploration_value ∈ [0, 100]
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": visual_candidate.purpose,
            "frontier_scope": "local",
            "expected_state_hint": "back",
            **recovery_meta,
        },
    )
    return (back,)


def _package_boundary_recovery_metadata(
    *,
    kind: ExternalActivityKind,
    package_name: str | None,
) -> dict[str, object]:
    return {
        "package_boundary_recovery": True,
        _EXTERNAL_ACTIVITY_KIND: kind.value,
        _EXTERNAL_PACKAGE_NAME: str(package_name or ""),
    }


def _append_popup_back_recovery_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    location: str,
    allowed_hosts: tuple[str, ...],
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """广告关闭曾跳出目标包时，优先系统 BACK，禁止再点同一假关闭 X 外跳。"""

    del navigation_depth

    if not app_location_within_package_boundary(
        location=location,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
    ):
        return candidates
    if str(analysis.page_state or "").strip().casefold() not in _COVERAGE_OVERLAY_STATES:
        return candidates
    if not isinstance(coverage_metadata, Mapping):
        return candidates
    redirect_count = _nonnegative_metadata_counter(
        coverage_metadata,
        _DYNAMIC_POPUP_EXTERNAL_REDIRECT_COUNT,
    )
    recovery_count = _nonnegative_metadata_counter(
        coverage_metadata,
        _DYNAMIC_POPUP_BACK_RECOVERY_COUNT,
    )
    if redirect_count == 0 or recovery_count >= _DYNAMIC_POPUP_BACK_RECOVERY_LIMIT:
        return candidates

    def _is_dismiss_click(candidate: CandidateAction) -> bool:
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            return False
        role = str(candidate.metadata.get("control_role") or "").casefold()
        if role in {"dismiss", "skip"}:
            return True
        terms = " ".join(
            (
                str(candidate.semantic_target or ""),
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("semantic_name") or ""),
            )
        ).casefold()
        return any(
            marker in terms
            for marker in ("关闭", "close", "dismiss", "下方x", "弹层x")
        )

    recovery = next(
        (
            candidate
            for candidate in candidates
            if candidate.action_type is InteractionOperation.BACK
            and candidate.risk_level is InteractionRiskLevel.LOW
        ),
        None,
    )
    if recovery is None:
        # enrich 应已为弹层签发 BACK；若仍缺失则无法安全合成（Runtime 拒收无源视觉 ID）。
        return candidates
    recovery = replace(
        recovery,
        metadata={
            **dict(recovery.metadata),
            "frontier_scope": "local",
            "expected_state_hint": "home",
            "popup_back_recovery": True,
            "source_obstruction_identity": build_app_obstruction_identity(analysis),
        },
    )

    # 本轮先 BACK：把 dismiss／关层点击挪到队尾，避免 deterministic 路径再次点穿广告。
    others = tuple(
        candidate
        for candidate in candidates
        if candidate.candidate_id != recovery.candidate_id
    )
    safe = tuple(item for item in others if not _is_dismiss_click(item))
    risky = tuple(item for item in others if _is_dismiss_click(item))
    return (recovery, *safe, *risky)


def _append_paywall_sheet_back_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    location: str,
    allowed_hosts: tuple[str, ...],
    dynamic_frontier: bool = False,
) -> tuple[CandidateAction, ...]:
    """付费半屏／尊享卡无关闭钮时，优先系统 BACK 离场，禁止假 X／抢购钮。"""

    del navigation_depth

    if dynamic_frontier:
        # 动态 Frontier 只接受当前截图真实提供的候选，不能把普通 BACK
        # 重标成固定的付费半屏回退，也不能借此删除当前帧的其它候选。
        return candidates

    from services.api.app.application.app_visual_close_calibration import (
        is_payment_or_paywall_surface,
    )

    if not is_payment_or_paywall_surface(analysis):
        return candidates
    if not app_location_within_package_boundary(
        location=location,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
    ):
        return candidates
    if any(candidate.metadata.get("payment_sheet_back") is True for candidate in candidates):
        return candidates
    recovery = next(
        (
            candidate
            for candidate in candidates
            if candidate.action_type is InteractionOperation.BACK
            and candidate.risk_level is not InteractionRiskLevel.HIGH
        ),
        None,
    )
    if recovery is None:
        return candidates
    normalized_recovery = replace(
        recovery,
        metadata={
            **dict(recovery.metadata),
            "frontier_scope": "local",
            "expected_state_hint": "home",
            "popup_back_recovery": True,
            "payment_sheet_back": True,
            "purpose": "付费半屏／尊享卡无关闭钮，系统 BACK 返回业务页",
            "source_obstruction_identity": build_app_obstruction_identity(analysis),
        },
    )
    # 去掉同帧 dismiss／角落假关闭，避免 popup_chain 优先点假 X。
    remaining = tuple(
        candidate
        for candidate in candidates
        if candidate.candidate_id != recovery.candidate_id
        and candidate.metadata.get("control_role") not in {"dismiss", "skip"}
        and candidate.metadata.get("popup_close_corner_fallback") is not True
        and str(candidate.metadata.get("expected_state_hint") or "").casefold()
        != "close_popup_x"
    )
    return (normalized_recovery, *remaining)


def _append_hlw_curriculum_detail_back_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    location: str,
    allowed_hosts: tuple[str, ...],
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """受信详情已取证后补系统 BACK，避免 Provider 降级吞掉返回课纲。"""

    if not app_location_within_package_boundary(
        location=location,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
    ) or not isinstance(coverage_metadata, Mapping):
        return candidates
    raw_progress = coverage_metadata.get("app_curriculum_progress")
    if not isinstance(raw_progress, Mapping):
        return candidates
    try:
        progress = AppCurriculumProgress.from_dict(raw_progress)
    except (TypeError, ValueError):
        return candidates
    if (
        progress.playbook_id != "hlwapp"
        or progress.current_phase_id != "content_detail_back"
        or analysis.visual_page_type != "content_detail"
        or analysis.overlay_signature is not None
    ):
        return candidates
    controls = analysis.surface_facts.get("trusted_accessibility_controls")
    if not isinstance(controls, Mapping):
        return candidates
    bottom_navigation_count = controls.get("bottom_navigation_count")
    if (
        controls.get("contract_id") != "hlwapp-accessibility.v1"
        or controls.get("package_name") != analysis.package_name
        or controls.get("page_type") != "content_detail"
        or controls.get("overlay_close") is not False
        or isinstance(bottom_navigation_count, bool)
        or bottom_navigation_count != 0
    ):
        return candidates
    if any(
        candidate.action_type is InteractionOperation.BACK
        and _curriculum_candidate_matches(
            candidate=candidate,
            playbook=resolve_playbook_by_id(progress.playbook_id),
            progress=progress,
        )
        for candidate in candidates
    ):
        return candidates

    semantic_target = "系统 BACK 返回内容列表"
    purpose = "详情取证完成后用系统 BACK 返回目标包内内容列表"
    expected_state_hint = "content_detail_back"
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.BACK,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=expected_state_hint,
    )
    back = CandidateAction(
        candidate_id=visual_candidate.candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata={
            "candidate_id": visual_candidate.candidate_id,
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": expected_state_hint,
            "curriculum_action": {
                "protocol_version": APP_CURRICULUM_ACTION_VERSION,
                "phase_id": progress.current_phase_id,
                "phase_action_count": progress.current_phase_action_count,
                "action_type": InteractionOperation.BACK.value,
                "expected_state_hint": expected_state_hint,
            },
        },
    )
    return (back, *candidates)


def _enrich_package_boundary_analysis(
    analysis: AppVisualAnalysis,
    *,
    surface_kind: SurfaceKind,
    allowed_hosts: tuple[str, ...],
    dynamic_frontier: bool = False,
) -> AppVisualAnalysis:
    """越出目标包时只保留平台恢复动作；包内动作必须来自当前视觉事实。"""

    location = f"app://{analysis.package_name or 'unknown'}/{analysis.activity or 'unknown'}"
    if app_location_within_package_boundary(
        location=location,
        surface_kind=surface_kind,
        allowed_hosts=allowed_hosts,
    ):
        enriched = _enrich_in_package_obstruction_back_analysis(
            analysis,
            surface_kind=surface_kind,
            dynamic_frontier=dynamic_frontier,
        )
        return _enrich_in_package_deep_page_back_analysis(
            enriched,
            surface_kind=surface_kind,
            dynamic_frontier=dynamic_frontier,
        )
    external_kind = classify_external_activity(
        package_name=analysis.package_name,
        activity=analysis.activity,
    )
    surface_facts = {
        **dict(analysis.surface_facts),
        _EXTERNAL_ACTIVITY_KIND: external_kind.value,
        _EXTERNAL_PACKAGE_NAME: str(analysis.package_name or ""),
    }
    if _is_android_launcher_analysis(analysis):
        candidate = _build_launcher_warm_restore_visual_candidate(analysis)
        return AppVisualAnalysis.build(
            capture_id=analysis.capture_id,
            screenshot_artifact_id=analysis.screenshot_artifact_id,
            image_signature=analysis.image_signature,
            width=analysis.width,
            height=analysis.height,
            orientation=analysis.orientation,
            visual_page_type=analysis.visual_page_type,
            visual_summary=analysis.visual_summary,
            key_elements=analysis.key_elements,
            candidates=(candidate,),
            provider_id=analysis.provider_id,
            model_id=analysis.model_id,
            package_name=analysis.package_name,
            activity=analysis.activity,
            page_state=analysis.page_state,
            overlay_signature=analysis.overlay_signature,
            surface_facts=surface_facts,
        )
    back_candidates = tuple(
        item for item in analysis.candidates if item.action_type is InteractionOperation.BACK
    )
    if back_candidates:
        return AppVisualAnalysis.build(
            capture_id=analysis.capture_id,
            screenshot_artifact_id=analysis.screenshot_artifact_id,
            image_signature=analysis.image_signature,
            width=analysis.width,
            height=analysis.height,
            orientation=analysis.orientation,
            visual_page_type=analysis.visual_page_type,
            visual_summary=analysis.visual_summary,
            key_elements=analysis.key_elements,
            candidates=back_candidates,
            provider_id=analysis.provider_id,
            model_id=analysis.model_id,
            package_name=analysis.package_name,
            activity=analysis.activity,
            page_state=analysis.page_state,
            overlay_signature=analysis.overlay_signature,
            surface_facts=surface_facts,
        )
    candidate = _build_package_boundary_back_visual_candidate(analysis, kind=external_kind)
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=(candidate,),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=surface_facts,
    )


def _build_package_boundary_back_visual_candidate(
    analysis: AppVisualAnalysis,
    *,
    kind: ExternalActivityKind | None = None,
) -> AppVisualCandidate:
    resolved = kind or classify_external_activity(
        package_name=analysis.package_name,
        activity=analysis.activity,
    )
    purpose_by_kind = {
        ExternalActivityKind.BROWSER: "误进浏览器／外链后 BACK 回目标 App",
        ExternalActivityKind.STORE: "误进应用商店后 BACK 回目标 App",
        ExternalActivityKind.PAYMENT: "误进支付应用后 BACK 回目标 App",
        ExternalActivityKind.SETTINGS: "误进系统设置后 BACK 回目标 App",
        ExternalActivityKind.OTHER: "误进外部 Activity 后 BACK 回目标 App",
        ExternalActivityKind.LAUNCHER: "误进系统桌面后 BACK 回目标 App",
    }
    semantic_target = "系统返回键"
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_target,
        action_type=InteractionOperation.BACK,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose_by_kind[resolved],
        expected_state_hint="back",
    )


def _build_launcher_warm_restore_visual_candidate(
    analysis: AppVisualAnalysis,
) -> AppVisualCandidate:
    """为系统桌面签发无坐标的目标 App 热恢复候选。"""

    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name="重新打开目标应用",
        action_type=InteractionOperation.WARM_RESTORE,
        normalized_bounds=None,
        confidence=1.0,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="系统桌面无法通过 BACK 返回，重新将目标 App 拉到前台",
        expected_state_hint="app_ready",
    )


def _enrich_in_package_obstruction_back_analysis(
    analysis: AppVisualAnalysis,
    *,
    surface_kind: SurfaceKind,
    dynamic_frontier: bool = False,
) -> AppVisualAnalysis:
    """为 Android 目标包内的广告／弹层签发无坐标系统 BACK 兜底。"""

    from services.api.app.application.app_visual_close_calibration import (
        is_payment_or_paywall_surface,
    )

    paywall_sheet = is_payment_or_paywall_surface(analysis)
    if dynamic_frontier and paywall_sheet:
        # 动态 Frontier 不为付费页补发关闭动作；真实候选必须原样交给调度器。
        return analysis
    if (
        surface_kind is not SurfaceKind.ANDROID
        or not (
            _is_popup_surface_analysis(analysis)
            or _is_startup_ad_analysis(analysis)
            or paywall_sheet
        )
        or any(
            candidate.action_type is InteractionOperation.BACK for candidate in analysis.candidates
        )
    ):
        return analysis
    startup_ad = _is_startup_ad_analysis(analysis)
    # 根开屏没有可验证的 App 内历史，系统 BACK 可能直接退出目标应用。
    # 启动广告只使用当前帧显式 skip/dismiss，线路恢复页交给有界观察和低风险线路候选。
    if startup_ad and not paywall_sheet:
        return analysis
    # 弹层即使已有 dismiss，仍签发 BACK：关层点进浏览器后须能回退，
    # 不得因「有关闭 X」而缺少可执行的系统返回候选。
    use_cloud_back = (
        not paywall_sheet
        and _package_uses_cloud_back_popup_policy(analysis.package_name)
    )
    if paywall_sheet:
        semantic_name = "返回关闭付费半屏"
        purpose = "付费半屏／尊享卡无关闭钮，使用 Android 系统 BACK 返回业务页"
    elif use_cloud_back:
        semantic_name = "底部导航栏「<」关闭弹窗"
        purpose = (
            "弹窗广告仍在：A/B/C 不可用时连点两下底部导航栏最右侧「<」关窗"
            "（cloud_back_tap_twice；中间禁止截图；禁止当开屏点进入／禁止keyevent4）"
        )
    else:
        semantic_name = "返回关闭弹窗"
        purpose = "使用 Android 系统 BACK 尝试关闭当前目标包内遮挡弹窗"
    back = AppVisualCandidate.build(
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
        expected_state_hint="home",
        discovery_source=(
            DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC if use_cloud_back else None
        ),
    )
    # 付费半屏去掉假 dismiss，避免后续映射优先点抢购／装饰 X。
    kept = (
        tuple(
            candidate
            for candidate in analysis.candidates
            if candidate.control_role not in {"dismiss", "skip"}
        )
        if paywall_sheet
        else analysis.candidates
    )
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=(*kept, back),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=analysis.surface_facts,
    )


def _enrich_in_package_deep_page_back_analysis(
    analysis: AppVisualAnalysis,
    *,
    surface_kind: SurfaceKind,
    dynamic_frontier: bool = False,
) -> AppVisualAnalysis:
    """只为已确认二级业务页签发无坐标系统 BACK，供 Frontier 恢复来源。"""

    from services.api.app.application.app_visual_close_calibration import (
        is_payment_or_paywall_surface,
    )

    recoverable_page_types = {
        "activity",
        "content_detail",
        "payment",
        "post_publish",
        "settings",
        "video_detail",
    }
    page_type = str(analysis.visual_page_type or "").strip().casefold()
    if dynamic_frontier and is_payment_or_paywall_surface(analysis):
        # payment 也可能是无关闭钮的会员半屏；动态模式不得从页型推造 BACK。
        return analysis
    page_state = str(analysis.page_state or "").strip().casefold()
    stable_deep_page = page_type in recoverable_page_types and page_state in {
        "stable",
        "normal",
        "loaded",
        "content_available",
    }
    back_purpose = f"当前 {page_type} 已取证，通过系统 BACK 返回上一级并重新截图"
    has_platform_back = any(
        candidate.action_type is InteractionOperation.BACK
        and candidate.semantic_name == "系统 BACK 返回上一级"
        and candidate.purpose == back_purpose
        and candidate.expected_state_hint == "back"
        for candidate in analysis.candidates
    )
    if (
        surface_kind is not SurfaceKind.ANDROID
        or not stable_deep_page
        or analysis.overlay_signature is not None
        or has_platform_back
    ):
        return analysis
    back = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name="系统 BACK 返回上一级",
        action_type=InteractionOperation.BACK,
        normalized_bounds=None,
        confidence=0.01,
        risk_level=InteractionRiskLevel.LOW,
        purpose=back_purpose,
        expected_state_hint="back",
    )
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=(*analysis.candidates, back),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=analysis.surface_facts,
    )


def _is_android_launcher_analysis(analysis: AppVisualAnalysis) -> bool:
    return is_android_launcher_package(
        package_name=analysis.package_name,
        activity=analysis.activity,
    )

def _target_package(allowed_hosts: tuple[str, ...]) -> str:
    packages = tuple(dict.fromkeys(item.strip() for item in allowed_hosts if item and item.strip()))
    if len(packages) != 1:
        raise ValueError("App 包边界热恢复要求唯一目标包")
    return packages[0]


def _map_candidate(
    item: AppVisualCandidate,
    *,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    source_obstruction_identity: str | None = None,
) -> CandidateAction:
    discovery = item.discovery_source or DISCOVERY_SOURCE_VISION
    metadata = tag_candidate_action_provenance(
        {
        "candidate_id": item.candidate_id,
        "purpose": item.purpose,
            "frontier_scope": item.frontier_scope,
            "source_capture_id": item.source_capture_id,
        },
        discovery_source=discovery,
        source_capture_id=item.source_capture_id,
    )
    if item.control_role is not None:
        metadata["control_role"] = item.control_role
        metadata["obstruction_control_identity"] = (
            build_visual_obstruction_control_identity(item)
        )
        metadata["source_obstruction_identity"] = source_obstruction_identity
    if item.expected_state_hint is not None:
        metadata["expected_state_hint"] = item.expected_state_hint
        expected_page_type = _expected_candidate_page_type(item.expected_state_hint)
        if expected_page_type is not None:
            metadata["expected_next_page_type"] = expected_page_type
    if (
        item.action_type is InteractionOperation.BACK
        and item.semantic_name == "系统返回键"
        and "BACK 回目标 App" in str(item.purpose or "")
    ):
        metadata["package_boundary_recovery"] = True
    if item.action_type is InteractionOperation.BACK and (
        "底部导航栏" in item.semantic_name
        or "cloud_back" in str(item.purpose or "").casefold()
    ):
        # 弹窗 cascade D 档 cloud_back_twice：只标关层，不得冒充 popup_back_recovery
        # （否则 Explorer 会改写成「外跳浏览器」purpose → keyevent 4 退桌面）。
        metadata["cloud_back_popup_close"] = True
    risk_level = item.risk_level
    # 年龄确认进入是质检清障必达动作；Vision 常标 medium，这里收敛为 LOW
    # 以便 auth_gate / 动态安全降级能执行，但不改写 high。
    if is_age_confirm_candidate(
        action_type=item.action_type,
        risk_level=item.risk_level,
        semantic_target=item.semantic_name,
        purpose=item.purpose,
        expected_state_hint=item.expected_state_hint,
    ):
        risk_level = InteractionRiskLevel.LOW
        metadata["age_confirm_clear"] = True
    return CandidateAction(
        candidate_id=item.candidate_id,
        semantic_target=item.semantic_name,
        action_type=item.action_type,
        risk_level=risk_level,
        exploration_value=round(item.confidence * 100),
        navigation_depth=navigation_depth,
        expected_surface_kind=surface_kind,
        metadata=metadata,
    )


def _expected_candidate_page_type(hint: str) -> str | None:
    """把 Provider 的候选预期归一到页面目录；只用于调度，不形成覆盖事实。"""

    value = hint.strip()
    if not value:
        return None
    values = (value, *value.replace(":", " ").replace("/", " ").split())
    for candidate in values:
        try:
            _PAGE_TYPE_REGISTRY.require_code(candidate)
            return candidate
        except Exception:
            alias = _PAGE_TYPE_REGISTRY.resolve_alias(candidate)
            if alias is not None:
                return alias.canonical_code
        normalized = candidate.casefold().replace("-", "_").replace(" ", "_")
        code = next(
            (item for item in _PAGE_TYPE_REGISTRY.all_codes if item.casefold() == normalized),
            None,
        )
        if code is not None:
            return code
    return None


def _validate_continuation_identity(
    *,
    work: ExplorationWork,
    turn: ExplorerTurnResult,
) -> None:
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        raise ValueError("App 后继工作只接受 App Exploration Work")
    if turn.work_id != work.work_id or turn.task_id != work.task_id:
        raise ValueError("Explorer Turn 与父 Work 身份不一致")
    if turn.is_terminal or not isinstance(turn.decision, ActionIntent):
        raise ValueError("只有动作轮可以生成后继 Exploration Work")
    if turn.coordination is None:
        raise ValueError("动作轮缺少 Capture Coordination 结果")
    decision = turn.decision
    coordination = turn.coordination
    before_capture = coordination.before_capture
    after_capture = coordination.after_capture
    interaction = coordination.interaction
    if (
        decision.task_id != work.task_id
        or decision.session_id != work.session_id
        or decision.source_capture_id != work.source_capture_id
        or before_capture.task_id != work.task_id
        or before_capture.session_id != work.session_id
        or before_capture.surface_kind is not work.surface_kind
        or before_capture.capture_kind is not CaptureKind.APP
        or before_capture.role != "before_action"
        or before_capture.parent_capture_id != work.source_capture_id
        or after_capture.task_id != work.task_id
        or after_capture.session_id != work.session_id
        or after_capture.surface_kind is not work.surface_kind
        or after_capture.capture_kind is not CaptureKind.APP
        or after_capture.role != "after_action"
        or after_capture.parent_capture_id != before_capture.capture_id
        or after_capture.action_execution_id != before_capture.action_execution_id
        or interaction.task_id != work.task_id
        or interaction.session_id != work.session_id
        or interaction.intent_id != decision.intent_id
        or interaction.source_capture_id != before_capture.capture_id
        or interaction.result_capture_id != after_capture.capture_id
        or interaction.action_execution_id != after_capture.action_execution_id
        or interaction.status
        not in {
            InteractionResultStatus.SUCCEEDED,
            InteractionResultStatus.FAILED,
            InteractionResultStatus.BLOCKED,
            InteractionResultStatus.TIMED_OUT,
        }
    ):
        raise ValueError("动作后 Capture 与父 Work 谱系不一致")


def _validate_observation_continuation_identity(
    *,
    work: ExplorationWork,
    turn: ExplorerTurnResult,
) -> None:
    decision = turn.decision
    capture = turn.observation_capture
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        raise ValueError("App 重新观察只接受 App Exploration Work")
    if turn.work_id != work.work_id or turn.task_id != work.task_id:
        raise ValueError("Explorer Turn 与父 Work 身份不一致")
    if not isinstance(decision, TimedObservationIntent) or capture is None:
        raise ValueError("重新观察后继必须携带 TimedObservationIntent 与 Capture")
    if turn.coordination is not None:
        raise ValueError("重新观察不得携带 Action Capture Coordination")
    if (
        decision.task_id != work.task_id
        or decision.session_id != work.session_id
        or decision.source_capture_id != work.source_capture_id
        or capture.task_id != work.task_id
        or capture.session_id != work.session_id
        or capture.surface_kind is not work.surface_kind
        or capture.capture_kind is not CaptureKind.APP
        or capture.role != "observation"
        or capture.parent_capture_id != work.source_capture_id
        or capture.action_execution_id is not None
    ):
        raise ValueError("Observation Capture 与父 Work 谱系不一致")


def _selected_candidate(
    *,
    prior_work: ExplorationWork,
    turn: ExplorerTurnResult,
) -> CandidateAction:
    if not isinstance(turn.decision, ActionIntent):
        raise ValueError("只有动作决策可以选择父 Frontier 候选")
    candidate_id = turn.decision.parameters.get("candidate_id")
    selected = next(
        (item for item in prior_work.frontier.candidates if item.candidate_id == candidate_id),
        None,
    )
    if selected is None:
        raise ValueError("ActionIntent candidate_id 不属于父 Work Frontier")
    return selected


def _product_script_completed_action_for_coverage(
    *,
    selected: CandidateAction,
    action_succeeded: bool,
    observation_completed: bool,
    observation_reason: object,
) -> CandidateAction | None:
    """產品腳本步成功後必須記入 completed_action，否則 ``*_script_index`` 不前進。

    觀察留證步通常 ``observation_completed=True``；點擊／返回成功時觀察合同未必
    completed。海角／禁果／禁漫已有特例；mama 等其餘 ``*-script:`` 與
    ``product_script_orchestrated`` 不得漏掉，否則會一直重點同一座標。
    """

    candidate_id = str(selected.candidate_id or "")
    if observation_completed:
        return selected
    if candidate_id.startswith("hlapp-script:"):
        from services.api.app.application.app_hlapp_script import hlapp_script_step_ready

        if hlapp_script_step_ready(
            candidate_id=candidate_id,
            action_succeeded=action_succeeded,
            observation_completed=observation_completed,
            observation_reason=observation_reason,
        ):
            return selected
        return None
    if candidate_id.startswith("hlwapp-script:"):
        from services.api.app.application.app_hlwapp_script import hlwapp_script_step_ready

        if hlwapp_script_step_ready(
            candidate_id=candidate_id,
            action_succeeded=action_succeeded,
            observation_completed=observation_completed,
            observation_reason=observation_reason,
        ):
            return selected
        return None
    if not action_succeeded:
        return None
    if "-script:" in candidate_id:
        return selected
    metadata = selected.metadata if isinstance(selected.metadata, Mapping) else {}
    if metadata.get("product_script_orchestrated") is True:
        return selected
    return None


def _agent_run_id(work: ExplorationWork) -> str:
    value = work.frontier.observation.metadata.get("agent_run_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    # 禁止回退 session_id（形如 app:{task_uuid}）：调度事实读取器会把它当
    # qc_agent_run.id 的 UUID 绑定参数，直接触发 asyncpg DataError 并中断探索。
    raise ValueError("App Work 缺少显式 agent_run_id metadata，禁止从 Session 身份推断")


def _scheduler_fact_epoch(metadata: Mapping[str, object]) -> int | None:
    """取上一轮已读取的阶段版本，避免旧 Work 跨 epoch 借用事实。"""

    scope = metadata.get("exploration_scheduler_facts_scope")
    if not isinstance(scope, Mapping):
        return None
    value = scope.get("finalization_epoch")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _app_flow_action_lineage(prior_work, decision):
    """首次清障／脚本亦须保存签发状态，且早于恢复分支提前返回。"""
    lineage: dict[str, object] = {}
    # 糖果有状态主流程：写回阶段／预算（恢复不重置整体预算）。
    if (
        decision.parameters.get("app_exploration_contract")
        == "bounded-stateless-constrained.v1"
        and isinstance(decision.parameters.get("tangguo_flow_state"), Mapping)
    ) or decision.parameters.get("tangguo_flow_driver") is True or isinstance(
        prior_work.frontier.observation.metadata.get("tangguo_flow_state"), Mapping
    ):
        from services.api.app.application.app_tangguo_flow_state import (
            merge_flow_state_from_intent_parameters,
        )

        lineage.update(
            merge_flow_state_from_intent_parameters(
                prior_metadata=prior_work.frontier.observation.metadata,
                parameters=decision.parameters,
            )
        )
    return lineage


def _surface_phase_lineage(
    *,
    prior_work: ExplorationWork,
    selected: CandidateAction,
    turn: ExplorerTurnResult,
    next_analysis: AppVisualAnalysis,
) -> dict[str, object]:
    """把阶段动作上下文传给动作后 Work，供下一轮继续同一阶段。"""

    if not isinstance(turn.decision, ActionIntent):
        return {}
    parameters = dict(turn.decision.parameters)
    phase_run_id = parameters.get("surface_phase_run_id")
    phase_key = parameters.get("surface_phase_key")
    decision_id = parameters.get("surface_phase_decision_id")
    inherited_package_recovery = False
    if (
        not isinstance(phase_run_id, str)
        and selected.metadata.get("package_boundary_recovery") is True
    ):
        prior_metadata = prior_work.frontier.observation.metadata
        phase_run_id = prior_metadata.get("surface_phase_run_id")
        phase_key = prior_metadata.get("surface_phase_key")
        decision_id = prior_metadata.get("surface_phase_decision_id")
        inherited_package_recovery = isinstance(phase_run_id, str) and isinstance(
            phase_key,
            str,
        )
    lineage = _app_flow_action_lineage(prior_work, turn.decision)
    if isinstance(phase_run_id, str) and phase_run_id.strip():
        lineage["surface_phase_run_id"] = phase_run_id
    if isinstance(phase_key, str) and phase_key.strip():
        lineage["surface_phase_key"] = phase_key
    if isinstance(decision_id, str) and decision_id.strip():
        lineage["surface_phase_decision_id"] = decision_id
    if inherited_package_recovery:
        for key in (
            "closed_popup_count",
            "reobserve_count",
            "same_popup_attempt_count",
            "countdown_observation_count",
            "close_attempt_count",
        ):
            value = prior_work.frontier.observation.metadata.get(f"surface_phase_{key}")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                lineage[f"surface_phase_{key}"] = value
        lineage["surface_phase_package_recovery"] = True
        if turn.decision.parameters.get("bounded_dismiss_tool") is True:
            phase = turn.decision.parameters.get("bounded_dismiss_phase")
            step = turn.decision.parameters.get("bounded_dismiss_step")
            lineage.update(
                next_bounded_dismiss_counters(
                    prior_metadata=prior_work.frontier.observation.metadata,
                    phase=str(phase) if isinstance(phase, str) else None,
                    step=(
                        step
                        if isinstance(step, int) and not isinstance(step, bool)
                        else None
                    ),
                )
            )
        return lineage
    if phase_key == "popup_chain":
        lineage.update(
            _next_popup_phase_counters(
                prior_metadata=prior_work.frontier.observation.metadata,
                prior_overlay_signature=prior_work.frontier.observation.overlay_signature,
                next_overlay_signature=next_analysis.overlay_signature,
                prior_obstruction_identity=_optional_metadata_text(
                    prior_work.frontier.observation.metadata,
                    "app_obstruction_identity",
                ),
                next_obstruction_identity=build_app_obstruction_identity(next_analysis),
                action_attempted=_surface_phase_action_attempted(turn),
                action_succeeded=(
                    turn.coordination is not None
                    and turn.coordination.interaction.status is InteractionResultStatus.SUCCEEDED
                ),
            )
        )
        control_identity = _optional_metadata_text(
            selected.metadata,
            "obstruction_control_identity",
        )
        if control_identity is not None:
            lineage["surface_phase_last_obstruction_control_identity"] = control_identity
    elif phase_key == "startup_ad_countdown":
        lineage.update(
            _next_startup_ad_phase_counters(
                prior_metadata=prior_work.frontier.observation.metadata,
                action_type=turn.decision.action_type,
                action_attempted=_surface_phase_action_attempted(turn),
                action_succeeded=(
                    turn.coordination is not None
                    and turn.coordination.interaction.status is InteractionResultStatus.SUCCEEDED
                ),
            )
        )
    # 限步清障工具：无论是否在 surface_phase，都写回 splash／popup 步数。
    if turn.decision.parameters.get("bounded_dismiss_tool") is True:
        phase = turn.decision.parameters.get("bounded_dismiss_phase")
        step = turn.decision.parameters.get("bounded_dismiss_step")
        lineage.update(
            next_bounded_dismiss_counters(
                prior_metadata=prior_work.frontier.observation.metadata,
                phase=str(phase) if isinstance(phase, str) else None,
                step=step if isinstance(step, int) and not isinstance(step, bool) else None,
            )
        )
    # F01 App 短流程：写回检查点回执与进度。
    if turn.decision.parameters.get("app_shortflow_driver") is True:
        from services.api.app.application.app_shortflow_driver import (
            merge_shortflow_progress_from_intent_parameters,
        )

        after_id = None
        action_id = None
        if turn.coordination is not None:
            after_id = turn.coordination.after_capture.capture_id
            action_id = turn.coordination.after_capture.action_execution_id
        lineage.update(
            merge_shortflow_progress_from_intent_parameters(
                prior_metadata=prior_work.frontier.observation.metadata,
                parameters=turn.decision.parameters,
                result_capture_id=after_id,
                action_execution_id=action_id,
                ok=True,
            )
        )
    return lineage


def _next_popup_phase_counters(
    *,
    prior_metadata: Mapping[str, object],
    prior_overlay_signature: str | None,
    next_overlay_signature: str | None,
    action_succeeded: bool,
    prior_obstruction_identity: str | None = None,
    next_obstruction_identity: str | None = None,
    action_attempted: bool | None = None,
) -> dict[str, int]:
    """按弹窗结构变化累计真实关闭层数和同层空点次数。"""

    return next_popup_phase_counters(
        prior_metadata=prior_metadata,
        prior_overlay_signature=prior_overlay_signature,
        next_overlay_signature=next_overlay_signature,
        action_succeeded=action_succeeded,
        prior_obstruction_identity=prior_obstruction_identity,
        next_obstruction_identity=next_obstruction_identity,
        action_attempted=action_attempted,
    )


def _next_startup_ad_phase_counters(
    *,
    prior_metadata: Mapping[str, object],
    action_type: InteractionOperation,
    action_succeeded: bool,
    action_attempted: bool | None = None,
) -> dict[str, int]:
    """把真实执行过的启动广告关闭动作记入可恢复的阶段上下文。"""

    return next_startup_ad_phase_counters(
        prior_metadata=prior_metadata,
        action_type=action_type,
        action_succeeded=action_succeeded,
        action_attempted=action_attempted,
    )


def _surface_phase_action_attempted(turn: ExplorerTurnResult) -> bool:
    """候选过期发生在 Driver 前不计触控；实际失败和超时仍占同层尝试。"""

    if _needs_visual_revalidation(turn) or turn.coordination is None:
        return False
    return turn.coordination.interaction.execution_outcome in {
        InteractionExecutionOutcome.SUCCEEDED,
        InteractionExecutionOutcome.FAILED,
        InteractionExecutionOutcome.TIMEOUT,
    }


def _app_flow_observation_lineage(work, decision) -> dict[str, object]:
    """重新观察也保存阶段进度；只接收本任务主流程签发的状态。"""
    from services.api.app.application.app_tangguo_flow_state import (
        merge_flow_state_from_intent_parameters,
    )

    if (
        decision.phase_run_id != f"app-flow:{work.task_id}"
        or decision.source_capture_id != work.source_capture_id
        or not isinstance(decision.workflow_state, Mapping)
    ):
        # 清障等其他观察不得清空已启动的主流程；不接受其覆写进度。
        if isinstance(work.frontier.observation.metadata.get("tangguo_flow_state"), Mapping):
            return merge_flow_state_from_intent_parameters(
                prior_metadata=work.frontier.observation.metadata, parameters=None,
            )
        return {}

    return merge_flow_state_from_intent_parameters(
        prior_metadata=work.frontier.observation.metadata,
        parameters={"tangguo_flow_state": decision.workflow_state},
    )


def _next_surface_phase_observation_counters(
    *,
    phase_key: str | None,
    prior_metadata: Mapping[str, object],
) -> dict[str, int]:
    """重新观察也必须推进阶段计数，避免无触控弹层无限重采。"""

    return next_surface_phase_observation_counters(
        phase_key=phase_key,
        prior_metadata=prior_metadata,
    )


def _successful_action_coverage_terms(
    *,
    selected: CandidateAction,
    turn: ExplorerTurnResult,
) -> str:
    """动作成功后才把当前截图候选语义作为里程碑佐证。"""

    coordination = turn.coordination
    if (
        coordination is None
        or coordination.interaction.status is not InteractionResultStatus.SUCCEEDED
        or not isinstance(turn.decision, ActionIntent)
    ):
        return ""
    return " ".join(
        item
        for item in (
            selected.semantic_target,
            str(selected.metadata.get("purpose") or ""),
            str(selected.metadata.get("expected_state_hint") or ""),
            str(turn.decision.parameters.get("app_curriculum_phase_id") or ""),
            turn.decision.purpose,
        )
        if item
    )


def _next_curriculum_progress(
    *,
    prior_work: ExplorationWork,
    selected: CandidateAction,
    turn: ExplorerTurnResult,
    arrival_proven: bool = True,
    tab_seek_evidenced: bool = False,
) -> AppCurriculumProgress | None:
    """从父 Work 的持久化课纲进度单调推进当前阶段。"""

    raw = prior_work.frontier.observation.metadata.get("app_curriculum_progress")
    if isinstance(raw, Mapping):
        progress = AppCurriculumProgress.from_dict(raw)
    else:
        from services.api.app.application.app_script_curriculum import (
            build_app_curriculum_progress,
            extract_playbook_id_from_objective,
        )

        playbook = resolve_playbook_by_id(extract_playbook_id_from_objective(prior_work.objective))
        if playbook is None:
            return None
        progress = build_app_curriculum_progress(playbook)
    if (
        isinstance(turn.decision, ActionIntent)
        and turn.decision.parameters.get("app_frontier_restore_only") is True
    ):
        return progress
    if progress.complete:
        return progress
    if not isinstance(turn.decision, ActionIntent):
        raise ValueError("只有 ActionIntent 可以推进 App 课纲")
    phase_id = turn.decision.parameters.get("app_curriculum_phase_id")
    if not isinstance(phase_id, str) or not phase_id.strip():
        raise ValueError("ActionIntent 缺少 app_curriculum_phase_id")
    if phase_id != progress.current_phase_id:
        raise ValueError("ActionIntent 课纲阶段与持久化 phase_cursor 不一致")
    playbook = resolve_playbook_by_id(progress.playbook_id)
    if playbook is None:
        raise ValueError("AppCurriculumProgress 引用了未知 Playbook")
    coordination = turn.coordination
    interaction_ok = (
        coordination is not None
        and coordination.interaction.status is InteractionResultStatus.SUCCEEDED
    )
    # OBSERVE 课纲相（wait_line／splash_burst 等）只证明观察完成，不得要求业务到达；
    # 底栏 tab:* 可用 tab_seek_screenshot_evidenced 推进；其它 CLICK 仍须 arrival_proven。
    observe_phase = selected.action_type is InteractionOperation.OBSERVE
    tab_seek_ok = (
        tab_seek_evidenced and selected.metadata.get("bottom_navigation_tab_seek") is True
    )
    succeeded = interaction_ok and (arrival_proven or observe_phase or tab_seek_ok)
    semantic_terms = [
        selected.semantic_target,
        str(selected.metadata.get("purpose") or ""),
        str(selected.metadata.get("expected_state_hint") or ""),
    ]
    if (
        succeeded
        and "tap_skip" in progress.phase_ids
        and candidate_explicitly_targets_tap_skip(
            action_type=selected.action_type.value,
            risk_level=selected.risk_level.value,
            semantic_target=selected.semantic_target,
            purpose=str(selected.metadata.get("purpose") or ""),
            expected_state_hint=str(selected.metadata.get("expected_state_hint") or ""),
        )
    ):
        # 该见证只来自已成功执行的当前候选；决策 purpose／phase_id 不能自证跳过。
        progress = progress.record_phase_witness(
            phase_id="tap_skip",
            action_succeeded=True,
        )
    if phase_id != "tap_skip":
        # tap_skip 只能由当前候选的明确跳过语义证明；不要把模型/fallback
        # purpose 中自动带出的 phase_id=tap_skip 当作跳过证据。
        semantic_terms.append(turn.decision.purpose)
    if phase_id == "tap_skip":
        phase_action_matched = candidate_explicitly_targets_tap_skip(
            action_type=selected.action_type.value,
            risk_level=selected.risk_level.value,
            semantic_target=selected.semantic_target,
            purpose=str(selected.metadata.get("purpose") or ""),
            expected_state_hint=str(selected.metadata.get("expected_state_hint") or ""),
        )
    else:
        phase_action_matched = curriculum_action_matches_current_phase(
            playbook=playbook,
            progress=progress,
            action_type=selected.action_type.value,
            semantic_terms=semantic_terms,
        )
    progress = progress.advance(
        phase_id=phase_id,
        action_succeeded=succeeded and phase_action_matched,
    )
    return progress.consume_witnessed_current_phase(phase_id="tap_skip")


def _current_business_navigation_depth(work: ExplorationWork) -> int:
    value = work.frontier.observation.metadata.get("business_navigation_depth")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return max(
        (item.navigation_depth for item in work.frontier.candidates),
        default=0,
    )


def _current_business_page_scope(work: ExplorationWork) -> str:
    value = work.frontier.observation.metadata.get("business_page_scope")
    return value if isinstance(value, str) and value.strip() else "unknown"


def _next_business_page_scope(
    *,
    prior_work: ExplorationWork,
    selected: CandidateAction,
    turn: ExplorerTurnResult,
    next_navigation_depth: int,
) -> str:
    phase_id = (
        turn.decision.parameters.get("app_curriculum_phase_id")
        if isinstance(turn.decision, ActionIntent)
        else None
    )
    if isinstance(phase_id, str) and phase_id.startswith("tab:"):
        return phase_id
    if selected.action_type in {InteractionOperation.SCROLL, InteractionOperation.OBSERVE}:
        return _current_business_page_scope(prior_work)
    if selected.action_type is InteractionOperation.BACK:
        return "app_ready" if next_navigation_depth == 0 else f"depth:{next_navigation_depth}"
    if (
        selected.action_type is InteractionOperation.CLICK
        and selected.metadata.get("frontier_scope") == "app"
    ):
        # 动态底栏/全局导航没有逐 App 课纲 phase_id，必须仍为每个真实导航
        # 分配稳定业务作用域，否则同 Activity、同页型的多个 Tab 会被错误合并。
        return (
            "nav:"
            + stable_protocol_hash(
                {
                    "semantic_target": selected.semantic_target.casefold(),
                    "purpose": str(selected.metadata.get("purpose") or "").casefold(),
                }
            )[:16]
        )
    if next_navigation_depth == _current_business_navigation_depth(prior_work):
        return _current_business_page_scope(prior_work)
    return f"depth:{next_navigation_depth}"


def _next_business_navigation_depth(
    *,
    prior_work: ExplorationWork,
    selected: CandidateAction,
    turn: ExplorerTurnResult,
    next_analysis: AppVisualAnalysis,
) -> int:
    """只按业务页面层级更新 depth；同页滚动、Tab 和弹层动作不增深。"""

    current = _current_business_navigation_depth(prior_work)
    coordination = turn.coordination
    if (
        coordination is None
        or coordination.interaction.status is not InteractionResultStatus.SUCCEEDED
    ):
        return current
    if selected.action_type in {InteractionOperation.SCROLL, InteractionOperation.OBSERVE}:
        return current
    if selected.action_type is InteractionOperation.BACK:
        return max(0, current - 1)
    if selected.action_type is not InteractionOperation.CLICK:
        return current
    if selected.metadata.get("frontier_scope") == "app":
        # 底栏／全局导航切换到新的顶层业务作用域，不是从当前页继续下钻。
        return 0
    phase_id = (
        turn.decision.parameters.get("app_curriculum_phase_id")
        if isinstance(turn.decision, ActionIntent)
        else None
    )
    terms = " ".join(
        str(item)
        for item in (
            phase_id,
            selected.semantic_target,
            selected.metadata.get("purpose"),
            selected.metadata.get("expected_state_hint"),
        )
        if item
    ).casefold()
    same_level_markers = (
        "tab:",
        "底栏",
        "tap_skip",
        "跳过",
        "close_popup",
        "关闭",
        "confirm",
        "确认",
        "age_gate",
        "年龄",
        "system_notice",
        "公告",
    )
    if any(marker in terms for marker in same_level_markers):
        return current
    if _relevant_visual_state_token(next_analysis) == _observation_relevant_state(
        prior_work.frontier.observation
    ):
        return current
    return min(
        current + 1,
        prior_work.budget.max_depth,
        prior_work.boundaries.max_navigation_depth,
    )


def _next_coverage_metadata(
    *,
    goal: AppExplorationGoal | None,
    analysis: AppVisualAnalysis,
    prior_metadata: object | None = None,
    prior_state_version: str | None = None,
    evidence_terms: tuple[str, ...] = (),
    pending_facts: AppExplorationPendingFacts | None = None,
    curriculum_progress: AppCurriculumProgress | None = None,
    runtime_policy_summary: Mapping[str, object] | None = None,
    business_navigation_depth: int = 0,
    business_page_scope: str = "unknown",
    completed_action: CandidateAction | None = None,
    attempted_action: CandidateAction | None = None,
    action_succeeded: bool | None = None,
    arrival_proven: bool | None = None,
    observation_proven: bool = False,
    action_observation_outcome: str | None = None,
    count_completed_action_for_business_progress: bool = True,
    action_decision: ActionIntent | None = None,
    # TimedObservation 重采无 attempted_action；短剧挂起时仍须累加观察次数。
    count_short_drama_playback_observation: bool = False,
    scheduler_max_actions: int | None = None,
    scheduler_max_duration_ms: int | None = None,
    scheduler_deadline: datetime | None = None,
    scheduler_required_page_types: Sequence[str] = (),
    scheduler_evidence_gap_page_types: Sequence[str] = (),
    scheduler_state_fingerprint: str | None = None,
    scheduler_now: datetime | None = None,
    scheduler_elapsed_duration_ms: int = 0,
) -> dict[str, object]:
    """只根据持久化截图事实累计目标覆盖，禁止候选计划直接声明完成。"""

    prior = prior_metadata if isinstance(prior_metadata, dict) else {}
    if not prior and hasattr(prior_metadata, "items"):
        prior = dict(prior_metadata.items())
    if goal is None:
        result = _with_app_frontier_restore_attempts(
            {},
            prior_metadata=prior,
            action_decision=action_decision,
        )
        return thaw_json_mapping(
            _with_app_scheduler_metadata(
                result,
                prior_metadata=prior,
                scheduler_max_actions=scheduler_max_actions,
                scheduler_max_duration_ms=scheduler_max_duration_ms,
                scheduler_deadline=scheduler_deadline,
                scheduler_required_page_types=scheduler_required_page_types,
                scheduler_evidence_gap_page_types=scheduler_evidence_gap_page_types,
                scheduler_state_fingerprint=_app_scheduler_state_fingerprint(
                    analysis,
                    candidate=completed_action or attempted_action,
                    fallback=scheduler_state_fingerprint,
                ),
                scheduler_now=scheduler_now,
                scheduler_candidate=completed_action or attempted_action,
                scheduler_action_claimed=completed_action is not None
                or attempted_action is not None,
                scheduler_retryable_failure=action_succeeded is False,
                scheduler_progressed=True,
                scheduler_elapsed_duration_ms=scheduler_elapsed_duration_ms,
                scheduler_page_fingerprint=_app_page_fingerprint(analysis, prior),
                scheduler_popup_fingerprint=_app_popup_fingerprint(analysis),
                scheduler_return_fingerprint=_app_return_fingerprint(
                    prior_metadata=prior,
                    candidate=completed_action or attempted_action,
                    analysis=analysis,
                ),
                scheduler_elapsed_queue=_app_scheduler_queue(
                    analysis,
                    required_page_types=scheduler_required_page_types,
                    evidence_gap_page_types=scheduler_evidence_gap_page_types,
                ),
            )
        )
    within_target_package = _analysis_within_goal_package(analysis=analysis, goal=goal)
    popup_external_redirect_count = _nonnegative_metadata_counter(
        prior,
        _DYNAMIC_POPUP_EXTERNAL_REDIRECT_COUNT,
    )
    popup_back_recovery_count = _nonnegative_metadata_counter(
        prior,
        _DYNAMIC_POPUP_BACK_RECOVERY_COUNT,
    )
    external_activity_bounce_count = _nonnegative_metadata_counter(
        prior,
        _EXTERNAL_ACTIVITY_BOUNCE_COUNT,
    )
    external_return_failure_count = _nonnegative_metadata_counter(
        prior,
        _EXTERNAL_RETURN_FAILURE_COUNT,
    )
    prior_external_kind = prior.get(_EXTERNAL_ACTIVITY_KIND)
    prior_was_external = isinstance(prior_external_kind, str) and bool(prior_external_kind.strip())
    app_ad_landing_attempt_count = _nonnegative_metadata_counter(
        prior,
        _APP_AD_LANDING_ATTEMPT_COUNT,
    )
    if (
        attempted_action is not None
        and _is_signed_app_ad_landing_candidate(attempted_action)
    ):
        app_ad_landing_attempt_count += 1
    if completed_action is not None:
        if _is_popup_close_action(completed_action) and not within_target_package:
            popup_external_redirect_count += 1
        if completed_action.metadata.get("popup_back_recovery") is True:
            popup_back_recovery_count += 1
    if action_observation_outcome == "external_return_failed":
        external_return_failure_count += 1
    elif action_observation_outcome == "surface_restored":
        external_return_failure_count = 0
    if not within_target_package:
        # 从目标包再次落到外部，或回源失败后仍停在外部，都记一次外跳弹跳。
        jumped_out = (
            completed_action is not None
            and completed_action.metadata.get("package_boundary_recovery") is not True
            and not prior_was_external
        )
        failed_or_looped = action_observation_outcome in {
            "external_return_failed",
            "external",
        }
        if jumped_out or failed_or_looped:
            external_activity_bounce_count += 1
    if (
        within_target_package
        and _is_stable_business_page(analysis)
        and not _is_popup_surface_analysis(analysis)
    ):
        popup_external_redirect_count = 0
        popup_back_recovery_count = 0
        external_activity_bounce_count = 0
        external_return_failure_count = 0
    external_activity_kind: str | None = None
    external_package_name: str | None = None
    if not within_target_package:
        external_activity_kind = classify_external_activity(
            package_name=analysis.package_name,
            activity=analysis.activity,
        ).value
        external_package_name = str(analysis.package_name or "")
    covered_pages = list(_text_tuple(prior.get("covered_page_types")))
    prior_covered_page_count = len(covered_pages)
    covered_milestones = list(_text_tuple(prior.get("covered_milestones")))
    blocked_pages = list(_text_tuple(prior.get("blocked_page_types")))
    blocked_milestones = _text_tuple(prior.get("blocked_milestones"))
    directed_attempts = directed_discovery_attempts_from_metadata(prior)
    business_state_ids = list(_text_tuple(prior.get("covered_business_state_ids")))
    business_evidence_ids = list(_text_tuple(prior.get(_COVERED_BUSINESS_EVIDENCE_IDS)))
    covered_business_pages = _business_page_groups(prior.get(_COVERED_BUSINESS_PAGES))
    degraded_provider_facts = _provider_quality_facts(prior.get("degraded_provider_facts"))
    semantic_action_counts = parse_app_semantic_action_counts(prior.get(APP_SEMANTIC_ACTION_COUNTS))
    coverage_seek_failure_scores = parse_app_semantic_action_counts(
        prior.get(APP_COVERAGE_SEEK_FAILURE_SCORES)
    )
    coverage_seek_target_attempt_counts = parse_app_semantic_action_counts(
        prior.get(APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS)
    )
    tab_seek_evidenced = action_observation_outcome == "tab_seek_screenshot_evidenced"
    visited_bottom_navigation_labels = [
        str(item).strip()
        for item in prior.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY, ())
        if isinstance(item, str) and str(item).strip()
    ] if isinstance(prior.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY), (list, tuple)) else []
    prior_flow = prior.get(APP_FLOW_CONTRACT_KEY)
    if isinstance(prior_flow, Mapping):
        from services.api.app.application.app_exploration_flow_contract import (
            trusted_flow_visited_bottom_labels,
        )

        visited_bottom_navigation_labels = trusted_flow_visited_bottom_labels(
            prior_flow,
            allowed_packages=goal.allowed_packages,
        )
    visited_home_top_category_tabs = [
        str(item).strip()
        for item in prior.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY, ())
        if isinstance(item, str) and str(item).strip()
    ] if isinstance(prior.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY), (list, tuple)) else []
    home_top_sample_meta: dict[str, object] = {
        key: prior[key]
        for key in (
            HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY,
            HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY,
        )
        if key in prior
    }
    bottom_nav_label_attempt_counts = parse_bottom_nav_label_attempt_counts(
        prior.get(APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS)
    )
    # 弹窗未满足：不得把底栏记为 visited／completed（半透明弹层下可见选中态也不算跑到）。
    from services.api.app.application.app_exploration_flow_contract import (
        popup_phase_blocks_bottom_tabs as _popup_blocks_bottom_tabs,
    )

    popup_blocks_bottom_visits = _popup_blocks_bottom_tabs(prior)
    tab_seek_action = None
    if (
        attempted_action is not None
        and attempted_action.metadata.get("bottom_navigation_tab_seek") is True
    ):
        tab_seek_action = attempted_action
    elif (
        completed_action is not None
        and completed_action.metadata.get("bottom_navigation_tab_seek") is True
    ):
        tab_seek_action = completed_action
    if (
        tab_seek_evidenced
        and tab_seek_action is not None
        and within_target_package
        and not popup_blocks_bottom_visits
    ):
        visited_bottom_navigation_labels = record_visited_bottom_navigation_label(
            {VISITED_BOTTOM_NAVIGATION_LABELS_KEY: visited_bottom_navigation_labels},
            label=tab_seek_action.semantic_target,
        )
        bottom_nav_label_attempt_counts = record_bottom_nav_label_seek_attempt(
            bottom_nav_label_attempt_counts,
            label=tab_seek_action.semantic_target,
            evidenced=True,
        )
    elif (
        tab_seek_action is not None
        and action_succeeded is True
        and within_target_package
        and not popup_blocks_bottom_visits
    ):
        # 页型未证实但 after 选中态已落到目标栏：仍记已访，禁止死磕同栏。
        selected_norms = {
            _normalize_bottom_tab_label_for_visit(label)
            for label in extract_selected_bottom_tab_labels(analysis)
            if _normalize_bottom_tab_label_for_visit(label)
        }
        if bottom_tab_label_already_visited(
            tab_seek_action.semantic_target,
            selected_norms,
        ):
            visited_bottom_navigation_labels = record_visited_bottom_navigation_label(
                {VISITED_BOTTOM_NAVIGATION_LABELS_KEY: visited_bottom_navigation_labels},
                label=tab_seek_action.semantic_target,
            )
            bottom_nav_label_attempt_counts = record_bottom_nav_label_seek_attempt(
                bottom_nav_label_attempt_counts,
                label=tab_seek_action.semantic_target,
                evidenced=True,
            )
        else:
            bottom_nav_label_attempt_counts = record_bottom_nav_label_seek_attempt(
                bottom_nav_label_attempt_counts,
                label=tab_seek_action.semantic_target,
                evidenced=False,
            )
    elif tab_seek_action is not None and popup_blocks_bottom_visits:
        # 弹窗挡路时底栏动作即便误触成功也只记 attempt，绝不记 visited。
        bottom_nav_label_attempt_counts = record_bottom_nav_label_seek_attempt(
            bottom_nav_label_attempt_counts,
            label=tab_seek_action.semantic_target,
            evidenced=False,
        )
    # 主页上方分类：点栏成功后进入「第一片取证」微流程，回到主页才记 visited。
    # 弹层／遮罩未清时不得 begin sample；禁止把「点了最新栏」直接 soft_advance 成已访。
    top_tab_seek_action = None
    if (
        attempted_action is not None
        and attempted_action.metadata.get("home_top_category_tab_seek") is True
    ):
        top_tab_seek_action = attempted_action
    elif (
        completed_action is not None
        and completed_action.metadata.get("home_top_category_tab_seek") is True
    ):
        top_tab_seek_action = completed_action
    sample_seek_action = None
    if (
        attempted_action is not None
        and attempted_action.metadata.get("home_top_category_sample_seek") is True
    ):
        sample_seek_action = attempted_action
    elif (
        completed_action is not None
        and completed_action.metadata.get("home_top_category_sample_seek") is True
    ):
        sample_seek_action = completed_action
    from services.api.app.application.app_ui_tree_features import (
        home_top_category_sample_evidence_page_types,
        home_top_category_sample_first_entry_enabled,
    )

    pkg_for_sample = str(analysis.package_name or "") or None
    sample_first_entry = home_top_category_sample_first_entry_enabled(
        package_name=pkg_for_sample,
    )
    evidence_page_types = home_top_category_sample_evidence_page_types(
        package_name=pkg_for_sample,
    )
    page_type_now = str(getattr(analysis, "visual_page_type", None) or "").casefold()
    if top_tab_seek_action is not None and within_target_package:
        from services.api.app.application.app_exploration_flow_contract import (
            analysis_blocks_home_top_category_tabs,
            popup_phase_blocks_home_top_category_tabs,
        )

        home_top_blocked = popup_blocks_bottom_visits or popup_phase_blocks_home_top_category_tabs(
            prior, analysis=analysis
        ) or analysis_blocks_home_top_category_tabs(analysis)
        if not home_top_blocked:
            hint = str(top_tab_seek_action.metadata.get("expected_state_hint") or "")
            label = (
                hint.removeprefix("home_top_tab:")
                if hint.startswith("home_top_tab:")
                else str(top_tab_seek_action.semantic_target or "")
            )
            if "「" in label and "」" in label:
                inner = label.split("「", 1)[-1].split("」", 1)[0].strip()
                if inner:
                    label = inner
            elif "『" in label and "』" in label:
                inner = label.split("『", 1)[-1].split("』", 1)[0].strip()
                if inner:
                    label = inner
            evidenced = tab_seek_evidenced or action_observation_outcome in {
                "tab_seek_screenshot_evidenced",
                "screenshot_evidenced",
                "local_state_changed",
                "business_arrived",
            }
            soft_advance = (
                not evidenced
                and action_observation_outcome
                not in {
                    "external",
                    "external_return_failed",
                    None,
                    "",
                    "unproven",
                    "action_failed",
                    "execution_failed",
                    "outcome_unknown",
                }
                and attempted_action is not None
                and completed_action is not None
            )
            selected_top = resolve_selected_home_top_category_label(
                analysis,
                package_name=pkg_for_sample,
            )
            selection_ok = home_top_category_seek_selection_matches(
                expected_label=label,
                selected_label=selected_top,
                package_name=pkg_for_sample,
            )
            pixel_visual = (
                top_tab_seek_action.metadata.get("home_top_category_pixel_visual")
                is True
            )
            if selected_top == "__skip__":
                selection_ok = False
                soft_advance = False
            elif selected_top and not selection_ok:
                soft_advance = False
            should_begin_sample = False
            if (evidenced and selection_ok) or (
                soft_advance and (selection_ok or selected_top is None)
            ):
                should_begin_sample = True
            elif (
                evidenced
                and action_succeeded is True
                and label
                and (
                    selected_top is None
                    or (pixel_visual and selected_top == "__skip__")
                )
            ):
                should_begin_sample = True
            if sample_first_entry:
                # 点栏成功只进入第一片取证，禁止记 visited／禁止把前序栏一并假完成。
                if should_begin_sample and label:
                    begun = begin_home_top_category_sample(
                        {
                            **home_top_sample_meta,
                            VISITED_HOME_TOP_CATEGORY_TABS_KEY: visited_home_top_category_tabs,
                        },
                        label=label,
                    )
                    home_top_sample_meta = {
                        key: begun[key]
                        for key in (
                            HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY,
                            HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY,
                        )
                        if key in begun
                    }
            else:
                labels_to_record: list[str] = []
                if should_begin_sample:
                    labels_to_record.append(label)
                if action_succeeded is True and selected_top and selected_top != "__skip__":
                    from services.api.app.application.app_ui_tree_features import (
                        home_top_category_label_is_skip,
                        ordered_home_top_category_tab_labels,
                    )

                    if not home_top_category_label_is_skip(
                        selected_top,
                        package_name=pkg_for_sample,
                    ):
                        ordered = ordered_home_top_category_tab_labels(
                            package_name=pkg_for_sample,
                        )
                        for item in ordered:
                            labels_to_record.append(item)
                            if home_top_category_seek_selection_matches(
                                expected_label=item,
                                selected_label=selected_top,
                                package_name=pkg_for_sample,
                            ):
                                break
                        else:
                            labels_to_record = [
                                item
                                for item in labels_to_record
                                if item == label
                            ]
                for item in labels_to_record:
                    visited_home_top_category_tabs = record_visited_home_top_category_tab(
                        {VISITED_HOME_TOP_CATEGORY_TABS_KEY: visited_home_top_category_tabs},
                        label=item,
                    )
    # 第一片取证：进入 payment／详情后记 need_back；BACK 回 home 后才记该栏 visited。
    if sample_first_entry and within_target_package:
        phase_now = home_top_category_sample_phase(
            {**prior, **home_top_sample_meta}
        )
        label_now = home_top_category_sample_label(
            {**prior, **home_top_sample_meta}
        )
        # 已选中栏直接点第一片时可能尚未 begin：用动作上的 label 补 begin。
        if (
            phase_now is None
            and sample_seek_action is not None
            and sample_seek_action.metadata.get("home_top_category_sample_phase")
            == HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD
        ):
            raw_label = sample_seek_action.metadata.get(
                "home_top_category_sample_label"
            )
            boot_label = str(raw_label or "").strip()
            if boot_label and (
                action_succeeded is True
                or action_observation_outcome
                in {
                    "tab_seek_screenshot_evidenced",
                    "screenshot_evidenced",
                    "local_state_changed",
                    "business_arrived",
                }
            ):
                begun = begin_home_top_category_sample(
                    {
                        **home_top_sample_meta,
                        VISITED_HOME_TOP_CATEGORY_TABS_KEY: visited_home_top_category_tabs,
                    },
                    label=boot_label,
                )
                home_top_sample_meta = {
                    key: begun[key]
                    for key in (
                        HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY,
                        HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY,
                    )
                    if key in begun
                }
                phase_now = home_top_category_sample_phase(
                    {**prior, **home_top_sample_meta}
                )
                label_now = home_top_category_sample_label(
                    {**prior, **home_top_sample_meta}
                )
        sample_action_ok = (
            sample_seek_action is not None
            and (
                action_succeeded is True
                or action_observation_outcome
                in {
                    "tab_seek_screenshot_evidenced",
                    "screenshot_evidenced",
                    "local_state_changed",
                    "business_arrived",
                    "back_to_parent",
                    "parent_restored",
                }
            )
        )
        if (
            phase_now == HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD
            and page_type_now in evidence_page_types
            and (
                sample_action_ok
                or (
                    sample_seek_action is None
                    and action_succeeded is True
                    and attempted_action is not None
                    and attempted_action.action_type
                    in {InteractionOperation.CLICK, InteractionOperation.TAP}
                )
            )
        ):
            # 点击第一片的 after 截图即为取证，直接进入 need_back。
            advanced = set_home_top_category_sample_phase(
                {
                    **home_top_sample_meta,
                    HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY: label_now,
                }
                if label_now
                else home_top_sample_meta,
                phase=HOME_TOP_SAMPLE_PHASE_NEED_BACK,
            )
            home_top_sample_meta = {
                key: advanced[key]
                for key in (
                    HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY,
                    HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY,
                )
                if key in advanced
            }
        elif (
            phase_now == HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE
            and page_type_now in evidence_page_types
            and (
                sample_action_ok
                or action_observation_outcome
                in {"screenshot_evidenced", "tab_seek_screenshot_evidenced"}
            )
        ):
            advanced = set_home_top_category_sample_phase(
                {
                    **home_top_sample_meta,
                    HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY: label_now,
                }
                if label_now
                else home_top_sample_meta,
                phase=HOME_TOP_SAMPLE_PHASE_NEED_BACK,
            )
            home_top_sample_meta = {
                key: advanced[key]
                for key in (
                    HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY,
                    HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY,
                )
                if key in advanced
            }
        elif (
            phase_now == HOME_TOP_SAMPLE_PHASE_NEED_BACK
            and page_type_now == "home"
            and label_now
            and (
                sample_action_ok
                or (
                    attempted_action is not None
                    and attempted_action.action_type is InteractionOperation.BACK
                    and action_succeeded is True
                )
                or (
                    completed_action is not None
                    and completed_action.action_type is InteractionOperation.BACK
                    and action_succeeded is True
                )
                or (
                    action_succeeded is True
                    and any(
                        item is not None
                        and (
                            item.metadata.get("short_drama_top_left_back") is True
                            or "左上角" in str(item.metadata.get("purpose") or "")
                        )
                        for item in (attempted_action, completed_action)
                    )
                )
            )
        ):
            completed_meta = complete_home_top_category_sample(
                {
                    **home_top_sample_meta,
                    VISITED_HOME_TOP_CATEGORY_TABS_KEY: visited_home_top_category_tabs,
                    HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY: label_now,
                    HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY: phase_now,
                }
            )
            visited_home_top_category_tabs = [
                str(item).strip()
                for item in completed_meta.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY, ())
                if isinstance(item, str) and str(item).strip()
            ] if isinstance(
                completed_meta.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY), (list, tuple)
            ) else visited_home_top_category_tabs
            home_top_sample_meta = {}
    # 已选中底栏：仅主页／首页可由选中态自动记访（录屏只截不点）。
    # 暗网／AI 等必须 tab_seek 留证，禁止误检选中态或上方分类「漫画」同槽袋跳栏。
    if within_target_package:
        for selected_label in extract_selected_bottom_tab_labels(analysis):
            selected_norm = _normalize_bottom_tab_label_for_visit(selected_label)
            if not selected_norm:
                continue
            if not (
                bottom_tab_label_already_visited("主页", {selected_norm})
                or bottom_tab_label_already_visited("首页", {selected_norm})
            ):
                continue
            visited_bottom_navigation_labels = record_visited_bottom_navigation_label(
                {VISITED_BOTTOM_NAVIGATION_LABELS_KEY: visited_bottom_navigation_labels},
                label=selected_label,
            )
    # 短剧进入播放器：标记待 BACK；记录开播时间；观察后累加计数；离开播放器则清除。
    short_drama_pending = short_drama_exit_pending(prior)
    short_drama_ended = short_drama_end_seen(prior)
    short_drama_observe = short_drama_playback_observe_count(prior)
    short_drama_started = short_drama_playback_started_at(prior)
    short_drama_top_left_attempts = short_drama_top_left_back_attempt_count(prior)
    page_type_now = str(analysis.visual_page_type or "").strip().casefold()
    newly_visited_short_drama = False
    if (
        tab_seek_action is not None
        and is_short_drama_bottom_tab_label(tab_seek_action.semantic_target)
        and bottom_tab_label_already_visited(
            tab_seek_action.semantic_target,
            {
                _normalize_bottom_tab_label_for_visit(item)
                for item in visited_bottom_navigation_labels
            },
        )
        and not bottom_tab_label_already_visited(
            tab_seek_action.semantic_target,
            {
                _normalize_bottom_tab_label_for_visit(item)
                for item in (
                    prior.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY, ())
                    if isinstance(prior.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY), (list, tuple))
                    else ()
                )
            },
        )
    ):
        newly_visited_short_drama = True
    if (
        within_target_package
        and page_type_now in SHORT_DRAMA_PLAYER_PAGE_TYPES
        and (
            newly_visited_short_drama
            or (
                tab_seek_action is not None
                and is_short_drama_bottom_tab_label(tab_seek_action.semantic_target)
                and (
                    tab_seek_evidenced
                    or bottom_tab_label_already_visited(
                        tab_seek_action.semantic_target,
                        {
                            _normalize_bottom_tab_label_for_visit(item)
                            for item in visited_bottom_navigation_labels
                        },
                    )
                )
            )
            or bottom_tab_label_already_visited(
                "短剧",
                {
                    _normalize_bottom_tab_label_for_visit(item)
                    for item in visited_bottom_navigation_labels
                },
            )
        )
    ):
        short_drama_pending = True
        if not short_drama_started:
            from datetime import UTC, datetime

            short_drama_started = datetime.now(UTC).isoformat()
    if short_drama_pending and not short_drama_started:
        from datetime import UTC, datetime

        short_drama_started = datetime.now(UTC).isoformat()
    frame_end_blob = " ".join(
        str(part or "")
        for part in (
            analysis.visual_summary,
            analysis.visual_page_type,
            " ".join(str(item) for item in (analysis.key_elements or ())),
        )
    )
    sample_blocks_short_drama = home_top_category_sample_in_progress(
        {**prior, **home_top_sample_meta}
    )
    short_drama_tab_visited = bottom_tab_label_already_visited(
        "短剧",
        {
            _normalize_bottom_tab_label_for_visit(item)
            for item in visited_bottom_navigation_labels
        },
    )
    in_short_drama_context = bool(
        short_drama_pending or newly_visited_short_drama or short_drama_tab_visited
    )
    # 主页上方栏第一片也是 video_detail／「观看完整剧集」：不得记短剧播完。
    if (
        in_short_drama_context
        and not sample_blocks_short_drama
        and (
            short_drama_playback_end_detected(frame_end_blob)
            or short_drama_playback_progress_near_end(frame_end_blob)
            or page_type_now == "payment"
        )
    ):
        short_drama_ended = True
    # 刚完成的动作 purpose 已写见解锁／贴尾退出：同步粘性。
    if (
        in_short_drama_context
        and not sample_blocks_short_drama
        and completed_action is not None
        and (
            completed_action.metadata.get("short_drama_playback_finished_exit") is True
            or "解锁" in str(completed_action.metadata.get("purpose") or "")
            or "贴尾" in str(completed_action.metadata.get("purpose") or "")
            or "播完" in str(completed_action.metadata.get("purpose") or "")
        )
    ):
        short_drama_ended = True
    if (
        in_short_drama_context
        and not sample_blocks_short_drama
        and attempted_action is not None
        and attempted_action.metadata.get("home_top_category_sample_seek") is not True
        and (
            attempted_action.metadata.get("short_drama_top_left_back") is True
            or "解锁" in str(attempted_action.metadata.get("purpose") or "")
            or "更多精彩解锁" in str(attempted_action.metadata.get("purpose") or "")
        )
    ):
        short_drama_ended = True
    if short_drama_pending and attempted_action is not None:
        op = attempted_action.action_type
        if op in {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}:
            short_drama_observe += 1
        elif (
            op is InteractionOperation.BACK
            or (
                op in {InteractionOperation.CLICK, InteractionOperation.TAP}
                and (
                    "返回" in str(attempted_action.semantic_target or "")
                    or attempted_action.metadata.get("short_drama_top_left_back") is True
                )
            )
        ) and page_type_now not in SHORT_DRAMA_PLAYER_PAGE_TYPES:
            # 已见解锁／播完：返回到短剧 Tab 内嵌根后仍保持 pending，强制切下一栏，
            # 禁止清 pending 后再点「查看完整短剧」重进播放。
            if short_drama_ended:
                short_drama_pending = True
                short_drama_top_left_attempts = 0
            else:
                short_drama_pending = False
                short_drama_observe = 0
                short_drama_started = None
                short_drama_top_left_attempts = 0
        elif (
            action_succeeded is True
            and op in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and attempted_action.metadata.get("short_drama_top_left_back") is True
            and page_type_now in SHORT_DRAMA_PLAYER_PAGE_TYPES
        ):
            # 左上角已落点但仍停播放器：累加次数供换槽／切下一栏。
            short_drama_top_left_attempts += 1
    elif short_drama_pending and count_short_drama_playback_observation:
        # prepare_after_observation：等解锁／贴尾的 TimedObservation 必须累加，
        # 否则 Tab 内嵌三形态（未完待续／贴尾播完／解锁层）会永久空等。
        short_drama_observe += 1
    if short_drama_pending and page_type_now not in SHORT_DRAMA_PLAYER_PAGE_TYPES:
        # 已回列表／根页：未见解锁可清；已见解锁则保持 pending 直至切离短剧栏。
        if not short_drama_ended:
            short_drama_pending = False
            short_drama_observe = 0
            short_drama_started = None
            short_drama_top_left_attempts = 0
        else:
            short_drama_pending = True
    # 已切到暗网／AI／社区／我的等非短剧栏，或暗网会员解锁落地：即使页型仍是
    # payment 也必须清 pending，否则会把「立即解锁」页当短剧播放器死循环点左上角。
    selected_now_for_pending = {
        _normalize_bottom_tab_label_for_visit(item)
        for item in extract_selected_bottom_tab_labels(analysis)
        if _normalize_bottom_tab_label_for_visit(item)
    }
    left_short_drama_tab = bool(selected_now_for_pending) and not any(
        is_short_drama_bottom_tab_label(item) for item in selected_now_for_pending
    )
    anwang_landing_blob = " ".join(
        str(part or "")
        for part in (
            analysis.visual_summary,
            analysis.visual_page_type,
            " ".join(str(item) for item in (analysis.key_elements or ())),
            " ".join(selected_now_for_pending),
        )
    )
    on_anwang_membership_landing = anwang_membership_landing_detected(
        anwang_landing_blob
    ) or any(is_anwang_bottom_tab_label(item) for item in selected_now_for_pending)
    if short_drama_pending and (left_short_drama_tab or on_anwang_membership_landing):
        short_drama_pending = False
        short_drama_ended = False
        short_drama_observe = 0
        short_drama_started = None
        short_drama_top_left_attempts = 0
    # 左上角多次无效后已切课纲下一栏，或 Tab 内嵌形态直接切栏：清除挂起。
    if (
        short_drama_pending
        and completed_action is not None
        and completed_action.metadata.get("bottom_navigation_tab_seek") is True
        and (
            short_drama_top_left_attempts >= SHORT_DRAMA_TOP_LEFT_BACK_BEFORE_TAB_ADVANCE
            or completed_action.metadata.get("short_drama_in_tab_form_exit") is True
            or short_drama_ended
        )
    ):
        short_drama_pending = False
        short_drama_ended = False
        short_drama_observe = 0
        short_drama_started = None
        short_drama_top_left_attempts = 0
    # 已见解锁后若仍选中短剧栏：保持 pending，便于底栏可见时立刻切暗网。
    if (
        short_drama_ended
        and not left_short_drama_tab
        and (
            any(is_short_drama_bottom_tab_label(item) for item in selected_now_for_pending)
            or bottom_tab_label_already_visited(
                "短剧",
                {
                    _normalize_bottom_tab_label_for_visit(item)
                    for item in visited_bottom_navigation_labels
                },
            )
        )
    ):
        short_drama_pending = True
    # 账号凭证：点过「保存凭证」或同帧见「保存成功」→ 下一步可点「我已保存」。
    # toast「保存成功」常一闪而过；不得因单帧 OCR 漏标清零，也不得因 OUTCOME_UNKNOWN 不算已点。
    profile_credential_save_done = prior.get(PROFILE_CREDENTIAL_SAVE_DONE_KEY) is True
    raw_save_attempts = prior.get(PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY)
    if (
        isinstance(raw_save_attempts, int)
        and not isinstance(raw_save_attempts, bool)
        and raw_save_attempts > 0
    ):
        profile_credential_save_attempts = raw_save_attempts
    else:
        profile_credential_save_attempts = 0
    from services.api.app.application.app_ui_tree_features import (
        observation_matches_profile_credential,
        observation_shows_profile_credential_save_success,
    )

    credential_blob = " ".join(
        str(part or "")
        for part in (
            analysis.visual_summary,
            analysis.visual_page_type,
            analysis.page_state,
            " ".join(str(item) for item in (analysis.key_elements or ())),
        )
    )
    on_credential_now = observation_matches_profile_credential(
        page_type=analysis.visual_page_type,
        page_state=analysis.page_state,
        observation_text=credential_blob,
        package_name=str(analysis.package_name or "") or None,
    )
    if observation_shows_profile_credential_save_success(
        credential_blob,
        package_name=str(analysis.package_name or "") or None,
    ):
        profile_credential_save_done = True
    save_meta_hit = False
    if attempted_action is not None and (
        attempted_action.metadata.get("profile_credential_save") is True
        or isinstance(
            attempted_action.metadata.get("profile_credential_save_contract"),
            Mapping,
        )
    ):
        save_meta_hit = True
    if (
        not save_meta_hit
        and action_decision is not None
        and isinstance(action_decision.parameters, Mapping)
        and attempted_action is not None
        and (
            action_decision.parameters.get("profile_credential_save") is True
            or isinstance(
                action_decision.parameters.get("app_profile_credential_save"),
                Mapping,
            )
        )
    ):
        save_meta_hit = True
    if save_meta_hit:
        # 只要落点过保存钮即累加；未知结果也算尝试，避免卡死只点保存／乱点下方 X。
        profile_credential_save_attempts += 1
        if action_succeeded is not False or on_credential_now:
            profile_credential_save_done = True
    if (
        completed_action is not None
        and completed_action.metadata.get("profile_credential_save") is True
        and action_succeeded is not False
    ):
        profile_credential_save_done = True
    if profile_credential_save_attempts >= 1 and on_credential_now:
        profile_credential_save_done = True
    if (
        completed_action is not None
        and completed_action.metadata.get("profile_credential_dismiss") is True
        and action_succeeded is not False
    ):
        profile_credential_save_done = False
        profile_credential_save_attempts = 0
    # 仅 dismiss 成功或明确离开凭证层时清零；禁止单帧漏标打回第一步。
    if (
        not on_credential_now
        and completed_action is not None
        and completed_action.metadata.get("profile_credential_dismiss") is True
        and action_succeeded is not False
    ):
        profile_credential_save_done = False
        profile_credential_save_attempts = 0
    # 未真实点过保存且同帧仍见「保存凭证」时，禁止把误标 done 写入覆盖元数据。
    from services.api.app.application.app_ui_tree_features import (
        profile_credential_save_phase_ready,
    )

    profile_credential_save_done = profile_credential_save_phase_ready(
        save_done_flag=profile_credential_save_done,
        save_attempt_count=profile_credential_save_attempts,
        observation_text=credential_blob,
        package_name=str(analysis.package_name or "") or None,
    )
    if attempted_action is not None and isinstance(action_succeeded, bool):
        source_page_identity = prior.get("business_page_identity")
        coverage_seek_failure_scores = record_app_coverage_seek_outcome(
            coverage_seek_failure_scores,
            candidate=attempted_action,
            page_identity=(source_page_identity if isinstance(source_page_identity, str) else None),
            action_succeeded=action_succeeded,
            arrival_proven=bool(arrival_proven),
            tab_seek_evidenced=tab_seek_evidenced,
        )
        coverage_seek_target_attempt_counts = record_app_coverage_seek_target_attempt(
            coverage_seek_target_attempt_counts,
            candidate=attempted_action,
            page_identity=(source_page_identity if isinstance(source_page_identity, str) else None),
            arrival_proven=bool(arrival_proven),
            tab_seek_evidenced=tab_seek_evidenced,
        )
    if completed_action is not None:
        source_page_identity = prior.get("business_page_identity")
        semantic_action_counts = record_app_semantic_action(
            semantic_action_counts,
            candidate=completed_action,
            page_identity=(source_page_identity if isinstance(source_page_identity, str) else None),
        )
    startup_observation_count = prior.get(_DYNAMIC_STARTUP_OBSERVATION_COUNT, 0)
    if (
        not isinstance(startup_observation_count, int)
        or isinstance(startup_observation_count, bool)
        or startup_observation_count < 0
    ):
        startup_observation_count = 0
    if _is_dynamic_startup_blocker(analysis):
        if (
            completed_action is not None
            and completed_action.action_type is InteractionOperation.OBSERVE
        ):
            startup_observation_count += 1
    else:
        startup_observation_count = 0
    unstable_reobserve_count = _nonnegative_metadata_counter(
        prior,
        _DYNAMIC_UNSTABLE_PAGE_REOBSERVE_COUNT,
    )
    unstable_back_count = _nonnegative_metadata_counter(
        prior,
        _DYNAMIC_UNSTABLE_PAGE_BACK_COUNT,
    )
    if _is_unstable_page_analysis(analysis):
        if (
            completed_action is not None
            and completed_action.action_type is InteractionOperation.OBSERVE
            and isinstance(
                completed_action.metadata.get("dynamic_unstable_page_reobserve"),
                Mapping,
            )
        ):
            unstable_reobserve_count += 1
        if (
            completed_action is not None
            and completed_action.metadata.get("unstable_page_back_recovery") is True
        ):
            unstable_back_count += 1
    else:
        unstable_reobserve_count = 0
        # BACK 成功回到稳定页后清零；仍在启动阻断 loading 时由启动合同继续计数
        if not _is_dynamic_startup_blocker(analysis):
            unstable_back_count = 0
    if (
        _is_dynamic_startup_blocker(analysis)
        and completed_action is not None
        and completed_action.metadata.get("unstable_page_back_recovery") is True
    ):
        unstable_back_count += 1
    home_popup_frame_count = _nonnegative_metadata_counter(
        prior,
        _DYNAMIC_HOME_POPUP_FRAME_COUNT,
    )
    home_popup_sequence_watch = str(prior.get("home_popup_sequence_watch") or "")
    if home_popup_sequence_watch not in {"next", "cleared", "done"}:
        home_popup_sequence_watch = ""
    business_tab_overlay_frame_count = _nonnegative_metadata_counter(
        prior,
        _DYNAMIC_BUSINESS_TAB_OVERLAY_FRAME_COUNT,
    )
    page_type_for_popup = str(analysis.visual_page_type or "").strip().casefold()
    lifecycle_pages = {
        "startup_ad",
        "line_loading",
        "guide_page",
        "splash",
        "countdown",
    }
    if _is_popup_surface_analysis(analysis) and page_type_for_popup in {
        "home",
        "",
        *lifecycle_pages,
    }:
        # 首页弹层、以及弹层外链后短暂回到开屏／线路页，都继续累计，避免计数被清零永远达不到让路阈值
        home_popup_frame_count += 1
    elif page_type_for_popup and page_type_for_popup not in {
        "home",
        "",
        *lifecycle_pages,
    }:
        # 真正进入其他业务页才清零首页弹层计数。
        # 禁止在「弹窗轮播间隙的短暂 stable home」清零——否则永远满不了让路帧数。
        home_popup_frame_count = 0
    popup_now = _is_popup_surface_analysis(analysis)
    home_popup_host = page_type_for_popup in {"home", ""}
    if (
        completed_action is not None
        and home_popup_host
        and _is_popup_close_action(completed_action)
    ):
        home_popup_sequence_watch = "next"
    elif (
        completed_action is not None
        and home_popup_host
        and completed_action.metadata.get("home_popup_sequence_observe") is True
    ):
        if popup_now:
            home_popup_sequence_watch = ""
        elif home_popup_sequence_watch == "next":
            home_popup_sequence_watch = "cleared"
        else:
            home_popup_sequence_watch = "done"

    if (
        _is_popup_surface_analysis(analysis)
        and page_type_for_popup in _DYNAMIC_BUSINESS_TAB_OVERLAY_PAGES
    ):
        business_tab_overlay_frame_count += 1
    elif page_type_for_popup in _DYNAMIC_BUSINESS_TAB_OVERLAY_PAGES:
        if not _is_popup_surface_analysis(analysis):
            business_tab_overlay_frame_count = 0
    elif page_type_for_popup and page_type_for_popup not in lifecycle_pages:
        business_tab_overlay_frame_count = 0
    startup_recovery_action_count = prior.get(
        _DYNAMIC_STARTUP_RECOVERY_ACTION_COUNT,
        0,
    )
    if (
        not isinstance(startup_recovery_action_count, int)
        or isinstance(startup_recovery_action_count, bool)
        or startup_recovery_action_count < 0
    ):
        startup_recovery_action_count = 0
    startup_recovery_targets = list(_text_tuple(prior.get(_DYNAMIC_STARTUP_RECOVERY_TARGETS)))
    if completed_action is not None and _has_dynamic_connection_recovery_terms(completed_action):
        startup_recovery_action_count += 1
        recovery_key = _dynamic_connection_recovery_key(completed_action)
        if recovery_key not in startup_recovery_targets:
            startup_recovery_targets.append(recovery_key)
    provider_quality = analysis.surface_facts.get("visual_provider_quality")
    soft_degrade_count = int(prior.get("visual_soft_degrade_count") or 0)
    if isinstance(provider_quality, Mapping) and provider_quality.get("status") == "degraded":
        fact = {
            **{str(key): value for key, value in provider_quality.items()},
            "capture_id": analysis.capture_id,
            "screenshot_artifact_id": analysis.screenshot_artifact_id,
        }
        # 可重试的供应商 408／超时：首次只记 soft 计数，不写入永久 degraded 事实，
        # 连续第二次才 hard degrade，避免一次暂态故障就 visual_provider_degraded。
        soft_retryable = (
            fact.get("retryable") is True
            and str(fact.get("error_code") or "") == "provider_call_failed"
        )
        if soft_retryable:
            soft_degrade_count += 1
        should_record_hard = (not soft_retryable) or soft_degrade_count >= 2
        if should_record_hard:
            identity = (
                str(fact.get("capture_id") or ""),
                str(fact.get("provider_id") or ""),
                str(fact.get("model_id") or ""),
                str(fact.get("error_code") or ""),
            )
            existing = {
                (
                    str(item.get("capture_id") or ""),
                    str(item.get("provider_id") or ""),
                    str(item.get("model_id") or ""),
                    str(item.get("error_code") or ""),
                )
                for item in degraded_provider_facts
            }
            if identity not in existing:
                degraded_provider_facts.append(fact)
        if not soft_retryable:
            soft_degrade_count = 0
    else:
        soft_degrade_count = 0

    # 操作后的新画面仍可留证，但只有可信到达才能增加正式覆盖。
    # 初始采集和合法的被动观察没有动作结果，必须与缺失动作回执区分。
    trusted_action_arrival = (
        completed_action is not None
        and action_succeeded is True
        and arrival_proven is True
    )
    passive_observation = (
        completed_action is None
        and attempted_action is None
        and action_succeeded is None
        and arrival_proven is None
        and action_decision is None
    )
    trusted_action_observation = (
        observation_proven is True
        and action_succeeded is True
        and completed_action is not None
        and completed_action.action_type in {
            InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE,
        }
    )
    coverage_observation_trusted = (
        trusted_action_arrival or passive_observation or trusted_action_observation
    )
    stable_business_page = (
        coverage_observation_trusted
        and within_target_package
        and _is_stable_business_page(analysis)
    )
    page_type = analysis.visual_page_type
    discovered_page_type_candidates = _next_discovered_page_type_candidates(
        prior.get("discovered_page_type_candidates"),
        analysis=analysis,
        goal=goal,
        within_target_package=within_target_package,
    )
    directed_attempts = record_entry_seen_attempts(
        directed_attempts,
        discovered_page_types=tuple(
            str(item.get("page_type"))
            for item in discovered_page_type_candidates
            if isinstance(item.get("page_type"), str)
        ),
        source_page_type=(
            page_type if isinstance(page_type, str) else prior.get("business_page_type")
        ),
        source_capture_id=analysis.capture_id,
        observed_at=None,
    )
    if completed_action is not None and completed_action.metadata.get("coverage_page_seek") is True:
        seek_target = str(
            completed_action.metadata.get("expected_next_page_type")
            or completed_action.metadata.get("expected_state_hint")
            or ""
        ).casefold()
        if seek_target in HARD_APPLICABILITY_PAGE_TYPES | {"guide_page"}:
            directed_attempts = record_seek_attempt(
                directed_attempts,
                page_type=seek_target,
                source_page_type=str(prior.get("business_page_type") or "unknown"),
                source_capture_id=analysis.capture_id,
                candidate_id=completed_action.candidate_id,
            )
    page_type_reached = stable_business_page or (
        coverage_observation_trusted
        and page_type is not None
        and page_type in _PAGE_TYPE_REGISTRY.lifecycle_codes
        and within_target_package
    )
    if (
        page_type_reached
        and page_type in _app_required_page_types(goal)
        and page_type not in covered_pages
    ):
        covered_pages.append(page_type)
    if (
        stable_business_page
        and "stable_business_page" in goal.required_page_types
        and "stable_business_page" not in covered_pages
    ):
        covered_pages.append("stable_business_page")
    business_page_group = _business_page_group(
        analysis=analysis,
        navigation_depth=business_navigation_depth,
        page_scope=business_page_scope,
        navigation_entry=(
            completed_action.semantic_target if completed_action is not None else None
        ),
    )
    business_page_identity = str(business_page_group["business_page_identity"])
    repeated_business_page = stable_business_page and business_page_identity in business_state_ids
    if stable_business_page and not repeated_business_page:
        business_state_ids.append(business_page_identity)
    if stable_business_page:
        covered_business_pages = _upsert_business_page_group(
            covered_business_pages,
            business_page_group,
        )
    business_evidence_identity = _business_page_evidence_identity(
        analysis=analysis,
        business_page_group=business_page_group,
    )
    repeated_business_evidence = (
        stable_business_page and business_evidence_identity in business_evidence_ids
    )
    observed_repeated_business_evidence = (
        within_target_package
        and _is_stable_business_page(analysis)
        and business_evidence_identity in business_evidence_ids
    )
    repeated_business_evidence_under_overlay = (
        not stable_business_page
        and str(analysis.page_state or "").strip().casefold() in _COVERAGE_OVERLAY_STATES
        and any(
            identity == _business_page_evidence_group_identity(business_page_group)
            or identity.startswith(
                _business_page_evidence_group_identity(business_page_group) + "|sha256:"
            )
            for identity in business_evidence_ids
        )
    )
    if stable_business_page and not repeated_business_evidence:
        business_evidence_ids.append(business_evidence_identity)

    # 新稳定业务 evidence 本身就是进展，即使到达动作是广告清障，也应清掉
    # 旧的重复计数；关闭已经见过的同页弹层不满足这个条件。
    new_business_evidence = stable_business_page and not repeated_business_evidence
    new_business_page_type = len(covered_pages) > prior_covered_page_count

    repeated_business_page_action_count = prior.get(
        _DYNAMIC_REPEATED_BUSINESS_PAGE_ACTION_COUNT,
        0,
    )
    if (
        not isinstance(repeated_business_page_action_count, int)
        or isinstance(repeated_business_page_action_count, bool)
        or repeated_business_page_action_count < 0
    ):
        repeated_business_page_action_count = 0
    progress_attempt = attempted_action or completed_action
    if new_business_evidence:
        repeated_business_page_action_count = 0
    elif progress_attempt is not None and count_completed_action_for_business_progress:
        if observed_repeated_business_evidence or repeated_business_evidence_under_overlay:
            repeated_business_page_action_count += 1
        elif stable_business_page:
            repeated_business_page_action_count = 0

    no_new_business_page_action_count = prior.get(
        _DYNAMIC_NO_NEW_BUSINESS_PAGE_ACTION_COUNT,
        0,
    )
    if (
        not isinstance(no_new_business_page_action_count, int)
        or isinstance(no_new_business_page_action_count, bool)
        or no_new_business_page_action_count < 0
    ):
        no_new_business_page_action_count = 0
    if new_business_page_type:
        no_new_business_page_action_count = 0
    elif progress_attempt is not None and count_completed_action_for_business_progress:
        no_new_business_page_action_count += 1

    # 清障无业务进展：弹窗轮播换签名会重置 same_popup，此计数只在业务扩大时清零。
    clearance_without_business = _nonnegative_metadata_counter(
        prior,
        _CLEARANCE_WITHOUT_BUSINESS_PROGRESS_COUNT,
    )
    business_seek_progress = progress_attempt is not None and (
        progress_attempt.metadata.get("bottom_navigation_tab_seek") is True
        or progress_attempt.metadata.get("home_top_category_tab_seek") is True
        or progress_attempt.metadata.get("home_category_grid_seek") is True
        or progress_attempt.metadata.get("coverage_page_seek") is True
    )
    if (
        new_business_page_type
        or new_business_evidence
        or business_seek_progress
    ):
        clearance_without_business = 0
    elif progress_attempt is not None and _is_popup_close_action(progress_attempt):
        clearance_without_business += 1
    # 禁止因弹窗轮播间隙的短暂 stable home 清零：否则永远达不到让路阈值。

    # 定向发现：来源页无入口／能力阻断／引导页审计
    if (
        within_target_package
        and stable_business_page
        and not _is_popup_surface_analysis(analysis)
        and isinstance(page_type, str)
    ):
        missing_hard = {
            code
            for code in HARD_APPLICABILITY_PAGE_TYPES
            if code in goal.required_page_types and code not in covered_pages
        }
        entry_for = {
            str(item.get("page_type")).casefold()
            for item in discovered_page_type_candidates
            if isinstance(item.get("page_type"), str)
        }
        no_entry_targets = hard_targets_missing_entry_on_source(
            current_page_type=page_type,
            missing_page_types=missing_hard,
            analysis_has_entry_for=entry_for,
        )
        if no_entry_targets:
            directed_attempts = record_no_entry_attempts(
                directed_attempts,
                page_types=no_entry_targets,
                source_page_type=page_type,
                source_capture_id=analysis.capture_id,
            )
    auth_blocked = _capability_blocked_hard_page_types(
        analysis=analysis,
        required_page_types=goal.required_page_types,
        covered_pages=covered_pages,
    )
    if auth_blocked:
        blocked_pages = merge_blocked_page_types(
            blocked_pages,
            extra=auth_blocked,
        )
        directed_attempts = record_capability_blocked_attempts(
            directed_attempts,
            page_types=auth_blocked,
            source_page_type=str(page_type or prior.get("business_page_type") or "unknown"),
            source_capture_id=analysis.capture_id,
            blocker_code="requires_auth",
        )
    if within_target_package and guide_page_markers_present(
        page_type=page_type,
        surface_facts=(
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
        ),
    ):
        directed_attempts = record_guide_page_attempt(
            directed_attempts,
            source_page_type=page_type if isinstance(page_type, str) else None,
            source_capture_id=analysis.capture_id,
        )

    page_type_marker = (page_type or "").casefold()
    successful_action_terms = " ".join(evidence_terms).casefold()
    prior_token = _relevant_state_from_metadata(prior_metadata)
    if prior_token is None and prior_state_version is not None:
        prior_token = prior_state_version
    next_token = _relevant_visual_state_token(analysis)
    state_changed = prior_token is not None and prior_token != next_token
    for milestone in goal.required_milestones:
        if milestone in covered_milestones:
            continue
        marker = milestone.split(":", 1)[-1].strip().casefold()
        special_stable = marker in {"stable", "stable_business_page"}
        if (
            (special_stable and stable_business_page)
            or (stable_business_page and marker and marker == page_type_marker)
            or (
                stable_business_page
                and state_changed
                and marker
                and marker in successful_action_terms
            )
            or (
                coverage_observation_trusted
                and within_target_package
                and milestone == "app_ready"
                and "app_ready" in evidence_terms
            )
        ):
            covered_milestones.append(milestone)

    result: dict[str, object] = {
        "required_page_types": list(_app_required_page_types(goal)),
        "covered_page_types": covered_pages,
        "covered_milestones": covered_milestones,
        "blocked_page_types": list(blocked_pages),
        "blocked_milestones": list(blocked_milestones),
        "covered_business_state_ids": business_state_ids,
        _COVERED_BUSINESS_EVIDENCE_IDS: business_evidence_ids,
        _COVERED_BUSINESS_PAGES: covered_business_pages,
        "covered_business_page_count": (
            len(covered_business_pages) if covered_business_pages else len(business_state_ids)
        ),
        "covered_page_type_count": len(covered_pages),
        "business_page_identity": business_page_identity,
        "business_page_identity_version": APP_BUSINESS_PAGE_IDENTITY_VERSION,
        "business_page_group_key": business_page_group["page_group_key"],
        "business_page_display_name": business_page_group["page_display_name"],
        "business_page_name": business_page_group["page_name"],
        "business_page_name_source": business_page_group["page_name_source"],
        "business_page_name_confidence": business_page_group["page_name_confidence"],
        "business_page_type": page_type,
        "business_page_scope": business_page_scope,
        "discovered_page_type_candidates": discovered_page_type_candidates,
        APP_DIRECTED_DISCOVERY_ATTEMPTS_KEY: directed_attempts,
        "pending_action_count": (
            pending_facts.pending_action_count if pending_facts is not None else None
        ),
        "pending_evidence_count": (
            pending_facts.pending_evidence_count if pending_facts is not None else None
        ),
        "pending_facts_available": pending_facts is not None,
        "degraded_provider_facts": degraded_provider_facts,
        "visual_soft_degrade_count": soft_degrade_count,
        APP_SEMANTIC_ACTION_COUNTS: semantic_action_counts,
        APP_COVERAGE_SEEK_FAILURE_SCORES: coverage_seek_failure_scores,
        APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS: coverage_seek_target_attempt_counts,
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY: visited_bottom_navigation_labels,
        "selected_bottom_tab_labels": sorted(
            {
                _normalize_bottom_tab_label_for_visit(item)
                for item in extract_selected_bottom_tab_labels(analysis)
                if _normalize_bottom_tab_label_for_visit(item)
            }
        ),
        VISITED_HOME_TOP_CATEGORY_TABS_KEY: visited_home_top_category_tabs,
        HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY: home_top_sample_meta.get(
            HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY
        ),
        HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY: home_top_sample_meta.get(
            HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY
        ),
        APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS: bottom_nav_label_attempt_counts,
        SHORT_DRAMA_EXIT_PENDING_KEY: short_drama_pending,
        SHORT_DRAMA_END_SEEN_KEY: short_drama_ended,
        SHORT_DRAMA_PLAYBACK_OBSERVE_COUNT_KEY: short_drama_observe,
        SHORT_DRAMA_PLAYBACK_STARTED_AT_KEY: short_drama_started,
        SHORT_DRAMA_TOP_LEFT_BACK_ATTEMPT_KEY: short_drama_top_left_attempts,
        PROFILE_CREDENTIAL_SAVE_DONE_KEY: profile_credential_save_done,
        PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY: profile_credential_save_attempts,
        _DYNAMIC_STARTUP_OBSERVATION_COUNT: startup_observation_count,
        _DYNAMIC_STARTUP_RECOVERY_ACTION_COUNT: startup_recovery_action_count,
        _DYNAMIC_STARTUP_RECOVERY_TARGETS: startup_recovery_targets,
        _DYNAMIC_UNSTABLE_PAGE_REOBSERVE_COUNT: unstable_reobserve_count,
        _DYNAMIC_UNSTABLE_PAGE_BACK_COUNT: unstable_back_count,
        _DYNAMIC_HOME_POPUP_FRAME_COUNT: home_popup_frame_count,
        "home_popup_sequence_watch": home_popup_sequence_watch,
        _DYNAMIC_BUSINESS_TAB_OVERLAY_FRAME_COUNT: business_tab_overlay_frame_count,
        _DYNAMIC_NO_NEW_BUSINESS_PAGE_ACTION_COUNT: (no_new_business_page_action_count),
        _DYNAMIC_REPEATED_BUSINESS_PAGE_ACTION_COUNT: (repeated_business_page_action_count),
        _CLEARANCE_WITHOUT_BUSINESS_PROGRESS_COUNT: clearance_without_business,
        _DYNAMIC_POPUP_EXTERNAL_REDIRECT_COUNT: popup_external_redirect_count,
        _DYNAMIC_POPUP_BACK_RECOVERY_COUNT: popup_back_recovery_count,
        _EXTERNAL_ACTIVITY_BOUNCE_COUNT: external_activity_bounce_count,
        _EXTERNAL_RETURN_FAILURE_COUNT: external_return_failure_count,
        _EXTERNAL_ACTIVITY_KIND: external_activity_kind,
        _EXTERNAL_PACKAGE_NAME: external_package_name,
        _APP_AD_LANDING_ATTEMPT_COUNT: app_ad_landing_attempt_count,
    }
    # Lab／生产组合根：单帧只解析一次，避免重复读 env。
    lab_composition = resolve_app_lab_composition()
    raw_policy = lab_composition.get("milestone_policy")
    milestone_policy = thaw_json_mapping(
        dict(raw_policy)
        if isinstance(raw_policy, Mapping)
        else resolve_effective_app_milestone_policy()
    )
    result["app_lab_composition"] = thaw_json_mapping(
        build_app_lab_composition_audit(lab_composition)
    )
    result["app_bounded_exploration_milestone_policy"] = dict(milestone_policy)
    result["app_bounded_exploration_milestones"] = dict(milestone_policy)
    prior_frame_digests_raw = prior.get("app_page_stability_frame_digests")
    prior_frame_digests: tuple[str, ...] = ()
    if isinstance(prior_frame_digests_raw, (list, tuple)):
        prior_frame_digests = tuple(
            str(item).strip()
            for item in prior_frame_digests_raw
            if isinstance(item, str) and item.strip()
        )
    identity_after = {
        "package_name": analysis.package_name,
        "activity": analysis.activity,
    }
    # #5：页面稳定 quorum（前台／窗口／结构／连续帧）+ 网络能力缺口；Gate 不消费。
    page_stability = build_app_page_stability_assessment(
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=(
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
        ),
        package_within_boundary=within_target_package,
        has_confirmed_visual_identity=_app_confirmed_visual_identity(analysis) is not None,
        image_signature=analysis.image_signature,
        prior_frame_digests=prior_frame_digests,
        network_observable=None,
        identity_after=identity_after,
    )
    result["app_page_stability_assessment"] = page_stability
    result["app_page_stability_frame_digests"] = list(
        page_stability.get("frame_digests") or []
    )
    result["app_capture_readiness_diagnosis"] = build_app_capture_readiness_diagnosis(
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=(
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
        ),
        package_within_boundary=within_target_package,
        has_confirmed_visual_identity=_app_confirmed_visual_identity(analysis) is not None,
        identity_after=identity_after,
        milestone_policy=milestone_policy,
        package_name=analysis.package_name,
        activity=analysis.activity,
        image_signature=analysis.image_signature,
        prior_frame_digests=prior_frame_digests,
        page_stability=page_stability,
    )
    entry_layer = classify_app_entry_layer(
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=(
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
        ),
        package_within_boundary=within_target_package,
    )
    result["app_entry_layer"] = entry_layer
    # M2 产品能力影子：静态声明 ≠ 运行时已观察；未找到不得缩小冻结分母；Gate 不消费。
    result["app_runtime_capability_shadow"] = capability_shadow_from_coverage_inputs(
        goal_required_page_types=goal.required_page_types,
        analysis_page_type=page_type if isinstance(page_type, str) else None,
        entry_layer=entry_layer,
        package_within_boundary=within_target_package,
    )
    # Settle 影子：固定 ACTION_AFTER／REOBSERVE 剖面写入审计栏；Gate 不消费。
    action_after_diagnosis = build_app_capture_readiness_diagnosis(
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=(
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
        ),
        package_within_boundary=within_target_package,
        has_confirmed_visual_identity=_app_confirmed_visual_identity(analysis) is not None,
        identity_after=identity_after,
        milestone_policy=milestone_policy,
        profile=SettleReadinessProfile.ACTION_AFTER,
        package_name=analysis.package_name,
        activity=analysis.activity,
        image_signature=analysis.image_signature,
        prior_frame_digests=prior_frame_digests,
        page_stability=page_stability,
    )
    reobserve_diagnosis = build_app_capture_readiness_diagnosis(
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=(
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
        ),
        package_within_boundary=within_target_package,
        has_confirmed_visual_identity=_app_confirmed_visual_identity(analysis) is not None,
        identity_after=identity_after,
        milestone_policy=milestone_policy,
        profile=SettleReadinessProfile.REOBSERVE,
        package_name=analysis.package_name,
        activity=analysis.activity,
        image_signature=analysis.image_signature,
        prior_frame_digests=prior_frame_digests,
        page_stability=page_stability,
    )
    result["app_settle_shadow_action_after"] = build_settle_shadow_manifest_audit(
        action_after_diagnosis
    )
    result["app_settle_shadow_reobserve"] = build_settle_shadow_manifest_audit(
        reobserve_diagnosis
    )
    result["app_m1_settle_audit_bundle"] = build_m1_settle_audit_bundle(
        action_after=action_after_diagnosis,
        reobserve=reobserve_diagnosis,
        entry_layer=entry_layer,
    )
    # M1 正式 Oracle 预览：AUTHORITY_M1 时 would_block_if_enforced 可审计；Gate SQL 不消费。
    result["app_settle_enforcement_preview"] = build_m1_settle_enforcement_preview(
        result.get("app_capture_readiness_diagnosis")
        if isinstance(result.get("app_capture_readiness_diagnosis"), Mapping)
        else action_after_diagnosis
    )
    # 仅累计当前观察的流程证据，不按包名或 APK 补入产品模板。
    required_pages = _app_required_page_types(goal)
    covered_now = tuple(
        str(item).strip()
        for item in covered_pages
        if isinstance(item, str) and str(item).strip()
    )
    result[APP_FLOW_CONTRACT_KEY] = update_app_flow_contract(
        prior.get(APP_FLOW_CONTRACT_KEY)
        if isinstance(prior.get(APP_FLOW_CONTRACT_KEY), Mapping)
        else None,
        page_state=analysis.page_state,
        page_type=page_type if isinstance(page_type, str) else None,
        overlay_signature=analysis.overlay_signature,
        surface_facts=(
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
        ),
        capture_id=analysis.capture_id,
        package_within_boundary=within_target_package,
        stable_business_page=stable_business_page,
        action_observation_outcome=action_observation_outcome,
        visited_bottom_labels=visited_bottom_navigation_labels,
        required_page_types=required_pages,
        covered_page_types=covered_now,
        package_name=analysis.package_name,
        allowed_packages=goal.allowed_packages,
        image_signature=getattr(analysis, "image_signature", None),
    )
    from services.api.app.application.app_schedule_gap_closure import (
        advance_journey_curriculum,
    )

    journey_coverage = advance_journey_curriculum(
        {
            "app_journey_curriculum": result.get("app_journey_curriculum")
            or prior.get("app_journey_curriculum")
        },
        semantic_target=(
            completed_action.semantic_target if completed_action is not None else None
        ),
        capture_id=analysis.capture_id,
    )
    if journey_coverage.get("app_journey_curriculum"):
        result["app_journey_curriculum"] = journey_coverage["app_journey_curriculum"]
        result["app_journey_next_semantic_target"] = journey_coverage.get(
            "app_journey_next_semantic_target"
        )
        if journey_coverage.get("app_journey_next_definition_id"):
            result["app_journey_next_definition_id"] = journey_coverage[
                "app_journey_next_definition_id"
            ]
    # 滚动后局部状态已变：后续业务触控必须绑定新 Capture 上的候选，禁止沿用滚动前坐标。
    if (
        completed_action is not None
        and completed_action.action_type is InteractionOperation.SCROLL
        and action_observation_outcome == "local_state_changed"
    ):
        result["app_post_scroll_rebind_required"] = True
        result["app_post_scroll_source_capture_id"] = analysis.capture_id
        pending_click_id = None
        if isinstance(completed_action.metadata, Mapping):
            raw_click = completed_action.metadata.get("followup_click_candidate_id")
            if isinstance(raw_click, str) and raw_click.strip():
                pending_click_id = raw_click.strip()
        if pending_click_id is None:
            prior_plan = prior.get("app_scroll_rebind_click_plan")
            if isinstance(prior_plan, Mapping):
                raw_click = prior_plan.get("click_candidate_id")
                if isinstance(raw_click, str) and raw_click.strip():
                    pending_click_id = raw_click.strip()
        if pending_click_id is not None:
            plan = build_scroll_rebind_click_plan(
                click_candidate_id=pending_click_id,
                source_capture_id=(
                    str(prior.get("app_post_scroll_source_capture_id")).strip()
                    if isinstance(prior.get("app_post_scroll_source_capture_id"), str)
                    and str(prior.get("app_post_scroll_source_capture_id")).strip()
                    else None
                ),
            )
            result["app_scroll_rebind_click_plan"] = mark_plan_after_scroll(
                plan,
                after_scroll_capture_id=analysis.capture_id,
            ).to_dict()
    elif prior.get("app_post_scroll_rebind_required") is True:
        # 新帧已重建候选后清除；仍停在旧帧则保留。
        if prior.get("app_post_scroll_source_capture_id") == analysis.capture_id:
            result["app_post_scroll_rebind_required"] = True
            result["app_post_scroll_source_capture_id"] = analysis.capture_id
            prior_plan = prior.get("app_scroll_rebind_click_plan")
            if isinstance(prior_plan, Mapping):
                result["app_scroll_rebind_click_plan"] = dict(prior_plan)
        else:
            prior_plan = prior.get("app_scroll_rebind_click_plan")
            if isinstance(prior_plan, Mapping) and prior_plan.get("status") == "rebind_pending":
                # 已换新 Capture 但仍未成功 click：保留计划待 Resolver 重绑。
                result["app_scroll_rebind_click_plan"] = dict(prior_plan)
    elif (
        action_succeeded is False
        and isinstance(prior.get("app_scroll_rebind_click_plan"), Mapping)
        and action_observation_outcome in {None, "observation_completed", "local_state_changed"}
    ):
        # 重绑失败路径：禁止盲点旧框，计划标记 failed_rebind 待重规划。
        try:
            plan = AppScrollRebindClickPlan.from_dict(prior["app_scroll_rebind_click_plan"])
            if plan.status in {"rebind_pending", "click_pending"}:
                result["app_scroll_rebind_click_plan"] = mark_plan_rebind_failed(plan).to_dict()
        except (TypeError, ValueError):
            pass
    # Coverage↔图影子双写：恒 coverage_authority_changed=false；无图时仍写可审计摘要。
    page_graph_snapshot = prior.get("app_page_graph_snapshot")
    result = attach_app_coverage_page_graph_shadow(
        result,
        graph=page_graph_snapshot if isinstance(page_graph_snapshot, dict) else None,
        current_frame={
            "capture_id": analysis.capture_id,
            "page_state": analysis.page_state,
            "overlay_signature": analysis.overlay_signature,
            "package_within_boundary": within_target_package,
            "business_page_identity": business_page_identity,
            "stable_business_page": stable_business_page,
        },
        milestone_policy=milestone_policy,
    )
    # M4：ADB Broker 影子对账栏位；不替换正式 Capture，不执行变更命令。
    result = attach_app_adb_broker_shadow(
        result,
        driver_observation=driver_observation_from_analysis_facts(
            package_name=getattr(analysis, "package_name", None),
            activity=getattr(analysis, "activity", None),
            width=getattr(analysis, "width", None),
            height=getattr(analysis, "height", None),
            surface_facts=(
                analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None
            ),
        ),
        milestone_policy=milestone_policy,
    )
    # M5：三队列／外跳旁路／复合重绑影子审计；图去重在 eligibility 侧生效。
    result = attach_app_m5_grayscale_shadow(
        result,
        external_bypass=evaluate_external_bypass(
            external_kind=external_activity_kind,
            bounce_count=int(external_activity_bounce_count or 0),
            return_failure_count=int(external_return_failure_count or 0),
        ),
        milestone_policy=milestone_policy,
    )
    if result.get("app_post_scroll_rebind_required") is True:
        result["app_composite_action_rebind"] = build_composite_action_rebind_contract(
            source_capture_id=str(
                result.get("app_post_scroll_source_capture_id") or analysis.capture_id or ""
            ),
            requires_rebind_after_scroll=True,
        )
    # M6：图恢复／补采计划影子；生产 runtime_ready 仍 false，不执行 Probe。
    result = attach_app_m6_recapture_shadow(
        result,
        graph=result.get("app_page_graph_snapshot")
        if isinstance(result.get("app_page_graph_snapshot"), dict)
        else None,
        target_identity=str(result.get("business_page_identity") or "").strip() or None,
        parent_capture_id=str(analysis.capture_id or "").strip() or None,
        milestone_policy=milestone_policy,
    )
    # M7：Gate／工作台／退役影子；正式 Gate SQL 仍只读 coverage_summary。
    result = attach_app_m7_gate_retirement_shadow(
        result,
        graph=result.get("app_page_graph_snapshot")
        if isinstance(result.get("app_page_graph_snapshot"), dict)
        else None,
        milestone_policy=milestone_policy,
    )
    policy = runtime_policy_summary or prior.get("app_runtime_policy")
    if isinstance(policy, Mapping):
        result["app_runtime_policy"] = dict(policy)
    if curriculum_progress is not None:
        result["app_curriculum_progress"] = curriculum_progress.to_dict()
    else:
        prior_progress = prior.get("app_curriculum_progress")
        if isinstance(prior_progress, Mapping):
            result["app_curriculum_progress"] = dict(prior_progress)
    result = _with_app_frontier_restore_attempts(
        result,
        prior_metadata=prior,
        action_decision=action_decision,
    )
    dismiss_action = completed_action or attempted_action
    dismiss_noop = _is_dismiss_noop_after_close(
        analysis=analysis,
        prior_metadata=prior,
        candidate=dismiss_action,
        action_succeeded=action_succeeded,
        action_observation_outcome=action_observation_outcome,
    )
    prior_noop = _nonnegative_metadata_counter(prior, _DISMISS_NOOP_STREAK)
    dismiss_noop_streak = prior_noop + 1 if dismiss_noop else 0
    result[_DISMISS_NOOP_STREAK] = dismiss_noop_streak
    # 2.0 不推进历史产品脚本索引；主流程进度由三合同状态保存。
    # 主页↔详情振荡计数：用于下一帧切栏优先，打断假关层点穿循环。
    prior_page = str(prior.get("business_page_type") or prior.get("page_type") or "").casefold()
    current_page = str(page_type or "").casefold()
    prior_bounce = _nonnegative_metadata_counter(prior, _HOME_DETAIL_BOUNCE_COUNT)
    if (
        prior_page in _DETAIL_LIKE_PAGE_TYPES and current_page in _ROOT_BUSINESS_PAGE_TYPES
    ) or (
        prior_page in _ROOT_BUSINESS_PAGE_TYPES and current_page in _DETAIL_LIKE_PAGE_TYPES
    ):
        result[_HOME_DETAIL_BOUNCE_COUNT] = prior_bounce + 1
    elif (
        completed_action is not None
        and completed_action.metadata.get("bottom_navigation_tab_seek") is True
        and current_page in _ROOT_BUSINESS_PAGE_TYPES
    ):
        # 成功切到非详情根页后清零，避免永久禁关层。
        result[_HOME_DETAIL_BOUNCE_COUNT] = 0
    else:
        result[_HOME_DETAIL_BOUNCE_COUNT] = prior_bounce
    prefer_tabs = bool(prior.get(_POST_DETAIL_EXIT_PREFER_TABS) is True)
    if completed_action is not None and (
        completed_action.metadata.get("forced_detail_exit_back") is True
        or completed_action.metadata.get(_POST_DETAIL_EXIT_PREFER_TABS) is True
    ):
        prefer_tabs = True
    if (
        completed_action is not None
        and completed_action.metadata.get("bottom_navigation_tab_seek") is True
    ):
        prefer_tabs = False
    result[_POST_DETAIL_EXIT_PREFER_TABS] = prefer_tabs
    suppress_popup_loop = coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=result,
    )
    # 影子／里程碑策略常含 mappingproxy；写入覆盖账本前必须 JSON-native，
    # 便于持久化与回归 round-trip。
    return thaw_json_mapping(
        _with_app_scheduler_metadata(
            result,
            prior_metadata=prior,
            scheduler_max_actions=scheduler_max_actions,
            scheduler_max_duration_ms=scheduler_max_duration_ms,
            scheduler_deadline=scheduler_deadline,
            scheduler_required_page_types=scheduler_required_page_types,
            scheduler_evidence_gap_page_types=scheduler_evidence_gap_page_types,
            scheduler_state_fingerprint=_app_scheduler_state_fingerprint(
                analysis,
                candidate=completed_action or attempted_action,
                fallback=scheduler_state_fingerprint,
            ),
            scheduler_now=scheduler_now,
            scheduler_candidate=completed_action or attempted_action,
            scheduler_action_claimed=completed_action is not None
            or attempted_action is not None,
            scheduler_retryable_failure=action_succeeded is False,
            scheduler_progressed=(
                not dismiss_noop
                and (
                    len(covered_pages) > prior_covered_page_count
                    or action_observation_outcome
                    in {
                        "business_arrived",
                        "local_state_changed",
                        "obstruction_removed",
                        "surface_restored",
                        "tab_seek_screenshot_evidenced",
                        "intermediate",
                    }
                    or (
                        dismiss_action is not None
                        and _is_popup_close_action(dismiss_action)
                        and state_changed
                    )
                    # 旧调用无动作证明时保留原语义；新链路不能把动画像素当恢复成功。
                    or (
                        action_observation_outcome is None
                        and (state_changed or stable_business_page)
                    )
                    or scripted_product_step_counts_as_progress(
                        completed_action,
                        action_succeeded=action_succeeded,
                    )
                )
            ),
            scheduler_elapsed_duration_ms=scheduler_elapsed_duration_ms,
            scheduler_page_fingerprint=_app_page_fingerprint(analysis, prior),
            scheduler_popup_fingerprint=_app_popup_fingerprint(analysis),
            scheduler_return_fingerprint=_app_return_fingerprint(
                prior_metadata=prior,
                candidate=completed_action or attempted_action,
                analysis=analysis,
            ),
            scheduler_elapsed_queue=_app_scheduler_queue(
                analysis,
                required_page_types=scheduler_required_page_types,
                evidence_gap_page_types=scheduler_evidence_gap_page_types,
            ),
            suppress_popup_loop_stop=suppress_popup_loop,
        )
    )


def _selected_splash_skip_after_only(
    *,
    selected: CandidateAction,
    turn: ExplorerTurnResult,
) -> bool:
    """开屏跳过／关闭：只验 after，before 强制复用开屏本体。"""

    if selected.metadata.get("splash_skip_after_only") is True:
        return True
    if isinstance(turn.decision, ActionIntent):
        return turn.decision.parameters.get("splash_skip_after_only") is True
    return False


def _selected_splash_body_evidence(
    *,
    selected: CandidateAction,
    turn: ExplorerTurnResult,
) -> bool:
    """开屏本体 OBSERVE：只验一张，after 强制复用 before。"""

    if selected.metadata.get("splash_body_evidence") is True:
        return True
    if isinstance(turn.decision, ActionIntent):
        return turn.decision.parameters.get("splash_body_evidence") is True
    return False


def scripted_product_step_counts_as_progress(
    action: CandidateAction | None,
    *,
    action_succeeded: bool | None,
) -> bool:
    """錄製腳本的留證截圖可以不改畫面，仍須清掉連續無進展計數。

    否則开屏本体 OBSERVE 再点「跳过」时，无画面变化会被无进展闸截断。
    """

    if action is None or action_succeeded is False:
        return False
    meta = action.metadata if isinstance(action.metadata, Mapping) else {}
    if (
        meta.get("hlapp_script") is True
        or meta.get("hjll_script") is True
        or meta.get("hlwapp_script") is True
    ):
        return True
    candidate_id = str(getattr(action, "candidate_id", "") or "")
    return candidate_id.startswith(("hlapp-script:", "hjll-script:", "hlwapp-script:"))


def _with_app_scheduler_metadata(
    metadata: Mapping[str, object],
    *,
    prior_metadata: Mapping[str, object],
    scheduler_max_actions: int | None,
    scheduler_max_duration_ms: int | None,
    scheduler_deadline: datetime | None,
    scheduler_required_page_types: Sequence[str],
    scheduler_evidence_gap_page_types: Sequence[str],
    scheduler_candidate: CandidateAction | None,
    scheduler_action_claimed: bool,
    scheduler_retryable_failure: bool,
    scheduler_progressed: bool,
    scheduler_elapsed_duration_ms: int,
    scheduler_state_fingerprint: str | None,
    scheduler_now: datetime | None,
    scheduler_page_fingerprint: str | None,
    scheduler_popup_fingerprint: str | None,
    scheduler_return_fingerprint: str | None,
    scheduler_elapsed_queue: ExplorationQueue,
    suppress_popup_loop_stop: bool | None = None,
) -> dict[str, object]:
    """把共用调度账本写入现有 Frontier metadata；缺参数时保持兼容。"""

    result = dict(metadata)
    prior_scheduler_state = prior_metadata.get(BOUNDED_EXPLORATION_STATE_KEY)
    if (
        isinstance(prior_scheduler_state, Mapping)
        and BOUNDED_EXPLORATION_STATE_KEY not in result
    ):
        # App 后继 Frontier 会重建覆盖 metadata；先把旧调度账本放回新 payload，
        # 再由 update_scheduler_metadata 累计本轮动作/时间，禁止跨轮清零。
        result[BOUNDED_EXPLORATION_STATE_KEY] = dict(prior_scheduler_state)

    max_actions = scheduler_max_actions
    if max_actions is None:
        raw = prior_metadata.get(BOUNDED_EXPLORATION_STATE_KEY)
        max_actions = (
            raw.get("max_actions")
            if isinstance(raw, Mapping) and isinstance(raw.get("max_actions"), int)
            else None
        )
    if max_actions is None:
        return result
    return update_scheduler_metadata(
        result,
        scheduler=_BOUNDED_SCHEDULER,
        max_actions=max_actions,
        max_duration_ms=scheduler_max_duration_ms,
        deadline=scheduler_deadline,
        candidate=scheduler_candidate,
        required_page_types=scheduler_required_page_types,
        evidence_gap_page_types=scheduler_evidence_gap_page_types,
        action_claimed=scheduler_action_claimed,
        retryable_failure=scheduler_retryable_failure,
        progressed=scheduler_progressed,
        state_fingerprint=scheduler_state_fingerprint or scheduler_page_fingerprint,
        page_fingerprint=scheduler_page_fingerprint,
        popup_fingerprint=scheduler_popup_fingerprint,
        return_fingerprint=scheduler_return_fingerprint,
        elapsed_duration_ms=scheduler_elapsed_duration_ms,
        elapsed_queue=scheduler_elapsed_queue,
        now=scheduler_now,
        suppress_popup_loop_stop=suppress_popup_loop_stop,
    )


def _app_required_page_types(goal: AppExplorationGoal | None) -> tuple[str, ...]:
    from services.api.app.application.app_schedule_gap_closure import (
        merge_required_arrival_page_types,
    )

    if goal is None:
        return merge_required_arrival_page_types(())
    return merge_required_arrival_page_types(goal.required_page_types)


def _app_evidence_gap_page_types(
    metadata: Mapping[str, object],
    *,
    known_page_types: Sequence[str] = (),
) -> tuple[str, ...]:
    """读取 App Evidence/Rule writer 已落盘的证据缺口。"""

    return evidence_gap_page_types_from_facts(
        metadata,
        known_page_types=known_page_types,
    )


def _app_page_fingerprint(
    analysis: AppVisualAnalysis,
    prior_metadata: Mapping[str, object],
) -> str | None:
    identity = _app_confirmed_visual_identity(analysis)
    if identity is not None:
        return stable_protocol_hash(
            {
                "package_name": analysis.package_name,
                "activity": analysis.activity,
                "business_identity": identity["identity"],
            }
        )
    package_name = analysis.package_name
    activity = analysis.activity
    page_type = analysis.visual_page_type
    if any(
        isinstance(value, str) and value.strip()
        for value in (package_name, activity, page_type)
    ):
        page_material = {
            "package_name": package_name,
            "activity": activity,
            "page_type": page_type,
            "page_state": analysis.page_state,
            "business_page_scope": prior_metadata.get("business_page_scope"),
            "scroll_state": _app_scroll_state_material(analysis, prior_metadata),
        }
        return stable_protocol_hash(page_material)
    value = analysis.image_signature
    return value.strip() if isinstance(value, str) and value.strip() else None


def _app_confirmed_visual_identity(analysis: AppVisualAnalysis) -> Mapping[str, Any] | None:
    return validated_app_visual_page_identity(
        analysis.surface_facts.get("app_page_identity"),
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        package_name=analysis.package_name,
        activity=analysis.activity,
    )


def _app_scheduler_state_fingerprint(
    analysis: AppVisualAnalysis,
    *,
    candidate: CandidateAction | None,
    fallback: str | None,
) -> str | None:
    """局部状态与页面身份分开；动画像素不能重置普通操作的重复计数。"""
    if _app_confirmed_visual_identity(analysis) is None:
        return fallback or analysis.image_signature
    # 只使用当前帧的滚动事实，不能把上一页面的偏移继承为当前状态。
    scroll = _app_scroll_state_material(analysis, {})
    material: dict[str, object] = {
        "page": _app_page_fingerprint(analysis, {}),
        "local_state": analysis.surface_facts.get("local_state", {}),
        "scroll": scroll,
    }
    if candidate is not None and candidate.action_type is InteractionOperation.SCROLL:
        # 自绘列表缺少可读滚动范围时保留像素变化作为探索提示，仍受总动作预算限制。
        if not any(key not in {"active_tab", "active_tab_index"} for key in scroll):
            material["unstructured_scroll_frame"] = analysis.image_signature
    return stable_protocol_hash(material)


def _app_scroll_state_material(
    analysis: AppVisualAnalysis,
    prior_metadata: Mapping[str, object],
) -> dict[str, object]:
    """将视觉事实中的滚动/Tab 状态并入页面签名，区分同页合法状态。"""

    sources: tuple[Mapping[str, object], ...] = tuple(
        source
        for source in (
            prior_metadata,
            analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else None,
        )
        if isinstance(source, Mapping)
    )
    keys = (
        "scroll_state",
        "scroll_y",
        "scroll_offset",
        "scroll_position",
        "scroll_top",
        "visible_range",
        "viewport_offset",
        "scroll_signature",
        "scroll_container_signature",
        "active_tab",
        "active_tab_index",
    )
    result: dict[str, object] = {}
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, (str, int, float, bool, list, tuple, dict)):
                result[key] = value
    return result


def _app_popup_fingerprint(analysis: AppVisualAnalysis) -> str | None:
    value = analysis.overlay_signature
    if isinstance(value, str) and value.strip():
        return value.strip()
    if _is_popup_surface_analysis(analysis):
        return build_app_obstruction_identity(analysis)
    return None


def _app_return_fingerprint(
    *,
    prior_metadata: Mapping[str, object],
    candidate: CandidateAction | None,
    analysis: AppVisualAnalysis,
) -> str | None:
    if candidate is None or candidate.action_type is not InteractionOperation.BACK:
        return None
    scheduler_state = prior_metadata.get(BOUNDED_EXPLORATION_STATE_KEY)
    source = (
        scheduler_state.get("last_page_fingerprint")
        if isinstance(scheduler_state, Mapping)
        else None
    )
    return stable_protocol_hash(
        {
            "source": source
            or prior_metadata.get("business_page_identity")
            or prior_metadata.get("business_page_scope"),
            # image_signature 可能随截图 artifact 改变；返回循环需要绑定
            # 稳定的页面身份，否则 A→B→A→B 会被每轮 Capture 打散。
            "target": _app_page_fingerprint(analysis, prior_metadata),
        }
    )


def _app_scheduler_queue(
    analysis: AppVisualAnalysis,
    *,
    required_page_types: Sequence[str],
    evidence_gap_page_types: Sequence[str],
) -> ExplorationQueue:
    page_type = str(analysis.visual_page_type or "").strip().casefold()
    if _is_popup_surface_analysis(analysis):
        return ExplorationQueue.REQUIRED
    if page_type in {value.strip().casefold() for value in required_page_types}:
        return ExplorationQueue.REQUIRED
    if page_type in {value.strip().casefold() for value in evidence_gap_page_types}:
        return ExplorationQueue.EVIDENCE_GAP
    return ExplorationQueue.DISCOVERY


def _with_app_frontier_restore_attempts(
    metadata: Mapping[str, object],
    *,
    prior_metadata: Mapping[str, object],
    action_decision: ActionIntent | None,
) -> dict[str, object]:
    """延续 restore_only 计数，并在本轮恢复动作后单调 +1。"""

    result = dict(metadata)
    prior_attempts = prior_metadata.get(APP_FRONTIER_RESTORE_ATTEMPTS_KEY)
    if isinstance(prior_attempts, Mapping) and APP_FRONTIER_RESTORE_ATTEMPTS_KEY not in result:
        result[APP_FRONTIER_RESTORE_ATTEMPTS_KEY] = dict(prior_attempts)
    if (
        action_decision is not None
        and action_decision.parameters.get("app_frontier_restore_only") is True
    ):
        item_id = action_decision.parameters.get("app_frontier_item_id")
        if isinstance(item_id, str) and item_id.strip():
            result = with_incremented_app_frontier_restore_attempt(
                result,
                frontier_item_id=item_id.strip(),
            )
    return result


def _next_discovered_page_type_candidates(
    value: object,
    *,
    analysis: AppVisualAnalysis,
    goal: AppExplorationGoal,
    within_target_package: bool,
) -> list[dict[str, object]]:
    """累计截图中真实可见入口的页型预期，供终态区分未到达与不适用。"""

    result: list[dict[str, object]] = []
    seen_page_types: set[str] = set()
    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            page_type = item.get("page_type")
            if (
                isinstance(page_type, str)
                and page_type in goal.required_page_types
                and page_type not in seen_page_types
            ):
                result.append({str(key): entry for key, entry in item.items()})
                seen_page_types.add(page_type)
    if not within_target_package:
        return result
    for candidate in analysis.candidates:
        hint = candidate.expected_state_hint
        if not isinstance(hint, str):
            continue
        page_type = _expected_candidate_page_type(hint)
        if (
            page_type is None
            or page_type not in goal.required_page_types
            or page_type in seen_page_types
        ):
            continue
        result.append(
            {
                "page_type": page_type,
                "source_capture_id": analysis.capture_id,
                "screenshot_artifact_id": analysis.screenshot_artifact_id,
                "candidate_id": candidate.candidate_id,
                "semantic_target": candidate.semantic_name,
                "frontier_scope": candidate.frontier_scope,
            }
        )
        seen_page_types.add(page_type)
    return result


def _analysis_within_goal_package(
    *,
    analysis: AppVisualAnalysis,
    goal: AppExplorationGoal,
) -> bool:
    package_name = str(analysis.package_name or "").strip().casefold()
    return bool(package_name) and package_name in {
        package.strip().casefold() for package in goal.allowed_packages
    }


def _capability_blocked_hard_page_types(
    *,
    analysis: AppVisualAnalysis,
    required_page_types: tuple[str, ...],
    covered_pages: Sequence[str],
) -> tuple[str, ...]:
    """认证／安全门挡住来源页时，把对应硬页型标为 capability blocker。"""

    page_state = str(analysis.page_state or "").strip().casefold()
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    auth_like = (
        page_state
        in {
            "auth_gate",
            "auth_required",
            "login_required",
            "requires_auth",
            "verification_required",
        }
        or facts.get("authentication_required") is True
        or facts.get("requires_auth") is True
    )
    safety_like = facts.get("captcha_required") is True or page_state in {
        "safety_interstitial",
        "permission_dialog",
    }
    if not auth_like and not safety_like:
        return ()
    current = str(analysis.visual_page_type or "").strip().casefold()
    covered = {item.casefold() for item in covered_pages}
    required = {item.casefold() for item in required_page_types}
    blocked: list[str] = []
    # 当前已在来源页或认证门本身：阻断从该来源可达的硬页型
    source_hits = {
        "profile": ("payment",),
        "post_list": ("post_publish",),
        "video_list": ("video_detail",),
        "home": ("video_detail",),
    }
    targets = source_hits.get(current, ())
    if not targets and auth_like:
        # 未知来源认证门：保守阻断 payment（常见 VIP 墙）
        targets = ("payment",)
    for page_type in targets:
        if page_type in required and page_type not in covered:
            blocked.append(page_type)
    return tuple(dict.fromkeys(blocked))


def _is_popup_surface_analysis(analysis: AppVisualAnalysis) -> bool:
    return (
        str(analysis.page_state or "").strip().casefold() in _COVERAGE_OVERLAY_STATES
        or analysis.overlay_signature is not None
        or analysis.surface_facts.get("popup_visible") is True
    )


def _analysis_is_soft_overlay_only(analysis: AppVisualAnalysis) -> bool:
    """详情返回常被标 home|overlay，但无中部广告模态——不得因此强制关层。"""

    page_state = str(analysis.page_state or "").strip().casefold()
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    if page_state in {"popup", "modal", "dialog", "system_dialog", "startup_ad"}:
        return False
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {
        "ad_popup",
        "promo_popup",
        "promo_ad",
        "modal",
        "dialog",
        "system_dialog",
        "age_verification",
        "age_confirm",
        "age_gate",
    }:
        return False
    topmost = facts.get("topmost_ad_popup")
    if isinstance(topmost, Mapping) and topmost:
        return False
    # overlay／仅 popup_visible／仅 signature → 软遮挡
    return (
        page_state == "overlay"
        or analysis.overlay_signature is not None
        or facts.get("popup_visible") is True
    )


def _is_popup_close_action(candidate: CandidateAction) -> bool:
    if is_visual_obstruction_control(candidate):
        return True
    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    markers = (
        "close_popup",
        "close_ad_popup",
        "close_promo_popup",
        "close_promo_ad",
        "关闭弹窗",
        "关闭弹层",
        "关闭广告",
        "关闭推广",
        "关闭宣传",
    )
    return any(marker.casefold() in terms for marker in markers)


def _is_dismiss_noop_after_close(
    *,
    analysis: AppVisualAnalysis,
    prior_metadata: Mapping[str, object],
    candidate: CandidateAction | None,
    action_succeeded: bool | None,
    action_observation_outcome: str | None,
) -> bool:
    """关闭后仍为相同弹层才累计 noop；尚未清障不等同点击无效。"""

    if candidate is None or action_succeeded is not True:
        return False
    if not _is_popup_close_action(candidate):
        return False
    if not _is_popup_surface_analysis(analysis):
        return False
    outcome = str(action_observation_outcome or "").casefold()
    if outcome in {
        "obstruction_removed",
        "business_arrived",
        "tab_seek_screenshot_evidenced",
    }:
        return False
    after_fp = _app_popup_fingerprint(analysis)
    scheduler_state = prior_metadata.get(BOUNDED_EXPLORATION_STATE_KEY)
    prior_fp = (
        scheduler_state.get("last_popup_fingerprint")
        if isinstance(scheduler_state, Mapping)
        else None
    )
    if isinstance(prior_fp, str) and prior_fp.strip() and after_fp:
        return after_fp == prior_fp.strip()
    # 无先验指纹：仍挡路即视为未真正关层
    return True


def _nonnegative_metadata_counter(
    metadata: Mapping[str, object],
    name: str,
) -> int:
    value = metadata.get(name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_metadata_text(metadata: Mapping[str, object], name: str) -> str | None:
    value = metadata.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_stable_business_page(analysis: AppVisualAnalysis) -> bool:
    business_page_types = set(get_default_page_type_registry().business_codes)
    return (
        analysis.page_state == "stable"
        and analysis.visual_page_type is not None
        and analysis.visual_page_type in business_page_types
        and analysis.package_name is not None
        and _page_type_confidence(analysis.surface_facts) >= 0.70
        and not is_obstructed_surface(
            page_state=analysis.page_state,
            page_type=analysis.visual_page_type,
            overlay_signature=analysis.overlay_signature,
            surface_facts=analysis.surface_facts,
        )
    )


def _counts_toward_business_page_progress(
    *,
    candidate: CandidateAction,
    turn: ExplorerTurnResult,
) -> bool:
    """广告清障和包边界恢复不消耗业务 Frontier 的停滞预算。"""

    if not isinstance(turn.decision, ActionIntent):
        return False
    if _needs_visual_revalidation(turn):
        return False
    if turn.decision.parameters.get("surface_phase_key") is not None:
        return False
    if candidate.metadata.get("package_boundary_recovery") is True:
        return False
    return not _is_popup_close_action(candidate)


def _needs_visual_revalidation(turn: ExplorerTurnResult) -> bool:
    """未触控的视觉过期不构成业务入口失败，仍消耗有界调度时间与清障预算。"""

    interaction = turn.coordination.interaction if turn.coordination is not None else None
    return (
        interaction is not None
        and interaction.status is InteractionResultStatus.FAILED
        and interaction.retryable
        and interaction.error_code
        in {"STALE_VISUAL_CANDIDATE", "APP_CANDIDATE_REVALIDATION_REQUIRED"}
    )


def _coverage_attempted_action(
    *,
    candidate: CandidateAction,
    turn: ExplorerTurnResult,
) -> CandidateAction | None:
    """广告外跳取证只调度一次；其他视觉过期仍不记为真实动作尝试。"""

    if not _needs_visual_revalidation(turn):
        return candidate
    if _is_signed_app_ad_landing_candidate(candidate):
        # 广告素材持续轮播时，源候选可能永远赶不上动作前 Capture。
        # 取证是可选的一次性调度，视觉过期后必须让位给当前关闭控件；
        # 清障动作仍由阶段计数按 Driver 是否真正执行来扣账。
        return candidate
    return None


def _business_page_group(
    *,
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    page_scope: str,
    navigation_entry: str | None = None,
) -> dict[str, object]:
    """按视觉身份（或包+Activity 兜底）生成覆盖核心身份；页型仅作展示分组。"""

    return build_app_business_page_group(
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_type=analysis.visual_page_type,
        surface_facts=analysis.surface_facts,
        navigation_depth=navigation_depth,
        page_scope=page_scope,
        capture_id=analysis.capture_id,
        navigation_entry=navigation_entry,
        image_signature=analysis.image_signature,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
    )


def _business_page_groups(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        group_key = item.get("page_group_key")
        if not isinstance(group_key, str) or not group_key.strip() or group_key in seen:
            continue
        seen.add(group_key)
        result.append({str(key): entry for key, entry in item.items()})
    return result


def _business_page_evidence_identity(
    *,
    analysis: AppVisualAnalysis,
    business_page_group: Mapping[str, object],
) -> str:
    """同业务页的新截图仍是新证据；不修改页数和导航分组身份。"""

    group_identity = _business_page_evidence_group_identity(business_page_group)
    return f"{group_identity}|{analysis.image_signature}"


def _business_page_evidence_group_identity(business_page_group: Mapping[str, object]) -> str:
    """弹层不证明底层业务内容变化，按已见业务分组保留真实停滞计数。"""

    if business_page_group.get("page_name") is not None:
        return str(business_page_group["page_group_key"])
    return str(business_page_group["business_page_identity"])


def _upsert_business_page_group(
    groups: list[dict[str, object]],
    current: Mapping[str, object],
) -> list[dict[str, object]]:
    """相同名称-页型只累计 Capture；不同名称即使页型相同也独立计页。"""

    current_key = str(current["page_group_key"])
    result: list[dict[str, object]] = []
    matched = False
    for group in groups:
        if group.get("page_group_key") != current_key:
            result.append(dict(group))
            continue
        count = group.get("capture_count")
        normalized_count = (
            count if isinstance(count, int) and not isinstance(count, bool) and count >= 1 else 1
        )
        result.append(
            {
                **dict(group),
                "last_capture_id": current["last_capture_id"],
                "capture_count": normalized_count + 1,
            }
        )
        matched = True
    if not matched:
        result.append(dict(current))
    return result


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item == item.strip() and item not in result:
            result.append(item)
    return tuple(result)


def _provider_quality_facts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        {str(key): item for key, item in fact.items()}
        for fact in value
        if isinstance(fact, Mapping)
    ]


def _append_unique(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    return values if value in values else (*values, value)


def _next_repeated_state_count(
    *,
    prior_work: ExplorationWork,
    next_state_version: str,
    next_analysis: AppVisualAnalysis | None = None,
) -> int:
    prior_token = _observation_relevant_state(prior_work.frontier.observation)
    next_token = (
        _relevant_visual_state_token(next_analysis)
        if next_analysis is not None
        else next_state_version
    )
    if next_token != prior_token:
        return 0
    return prior_work.frontier.repeated_state_count + 1


def _relevant_visual_state_token(analysis: AppVisualAnalysis) -> str:
    return content_fingerprint_value(analysis) or analysis.image_signature


def _observation_relevant_state(observation: SurfaceObservation) -> str:
    facts = observation.metadata.get("surface_facts")
    return content_fingerprint_value(facts if isinstance(facts, Mapping) else None) or (
        observation.state_version
    )


def _relevant_state_from_metadata(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    facts = metadata.get("surface_facts")
    fingerprint = content_fingerprint_value(facts if isinstance(facts, Mapping) else None)
    if fingerprint:
        return fingerprint
    signature = metadata.get("image_signature")
    return str(signature) if isinstance(signature, str) and signature else None


def _relevant_visual_state_changed(
    *,
    prior_observation: SurfaceObservation,
    next_analysis: AppVisualAnalysis,
) -> bool:
    return _observation_relevant_state(prior_observation) != _relevant_visual_state_token(
        next_analysis
    )


def _elapsed_duration_milliseconds(started_at: datetime, finished_at: datetime) -> int:
    """把持久化观测时间差向上取整，确保每轮至少消耗一毫秒。"""
    elapsed = finished_at - started_at
    elapsed_microseconds = (
        elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    )
    return max(1, (elapsed_microseconds + 999) // 1_000)
