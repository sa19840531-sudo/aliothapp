"""Web Interaction Runtime 的确定性语义目标解析器。"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import cv2
import numpy as np

from services.api.app.application.h5_adaptive_navigation import (
    adaptive_navigation_selector,
)
from services.api.app.application.h5_age_verification import (
    H5_AGE_VERIFICATION_CAPABILITY,
    H5_AGE_VERIFICATION_TARGET,
    H5_AGE_VERIFICATION_TARGET_MODE,
    h5_snapshot_age_verification_confirm_selectors,
)
from services.api.app.application.h5_interaction_capabilities import (
    STARTUP_AD_DISMISSAL_SELECTOR,
    h5_startup_dismissal_selector_from_snapshot_element,
)
from services.api.app.application.h5_line_loading_recovery import (
    H5_LINE_LOADING_LOGIN_RETRY_CAPABILITY,
    H5_LINE_LOADING_LOGIN_RETRY_TARGET,
    H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE,
)
from services.api.app.application.h5_list_detail_navigation import (
    H5_COMIC_CARD_NAME_SELECTOR,
    H5_LIST_DETAIL_TARGET,
    H5_POST_TITLE_SELECTOR,
    H5_VIDEO_ITEM_SELECTOR,
    h5_list_detail_expected_page_type,
    h5_list_detail_selector_allowed,
    h5_list_detail_selector_requires_visible_title,
    h5_list_detail_snapshot_element_matches,
)
from services.api.app.application.h5_modal_acknowledgement import (
    H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY,
    H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR,
    h5_modal_local_safe_acknowledgement,
)
from services.api.app.application.h5_modal_close import (
    H5_MODAL_CLOSE_CAPABILITY,
    H5_MODAL_CLOSE_SELECTOR,
    h5_modal_close,
)
from services.api.app.application.h5_obstruction_facts import assess_h5_ui_obstruction
from services.api.app.application.h5_payment_horizontal_collection import (
    H5_PAYMENT_HORIZONTAL_MAX_SEGMENTS,
    H5_PAYMENT_HORIZONTAL_SOURCE_PAGE_TYPES,
    H5_PAYMENT_HORIZONTAL_TARGET,
    H5_PAYMENT_HORIZONTAL_TARGET_MODE,
    h5_payment_horizontal_container_selector,
)
from services.api.app.application.h5_profile_activity_navigation import (
    H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE,
    H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE,
    H5_PROFILE_ACTIVITY_TARGET_MODE,
    h5_profile_activity_capability_for_target,
    h5_profile_activity_snapshot_element_matches,
)
from services.api.app.application.h5_profile_credential_popup import (
    H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY,
    H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_SELECTOR,
    h5_profile_credential_screenshot_confirm,
)
from services.api.app.application.h5_profile_login_navigation import (
    H5_FLUTTER_PROFILE_LOGIN_CAPABILITY,
    H5_FLUTTER_PROFILE_LOGIN_RUNTIME_SELECTOR,
    H5_FLUTTER_PROFILE_LOGIN_TARGET,
    H5_FLUTTER_PROFILE_LOGIN_TARGET_MODE,
    h5_unique_flutter_profile_login_entry,
)
from services.api.app.application.h5_profile_payment_navigation import (
    H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE,
    H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE,
    H5_PROFILE_PAYMENT_TARGET,
    H5_PROFILE_PAYMENT_TARGET_MODE,
    h5_profile_payment_snapshot_selector,
)
from services.api.app.application.h5_profile_settings_navigation import (
    H5_PROFILE_ACTIONS_SETTINGS_CAPABILITY,
    H5_PROFILE_CLASS_SETTINGS_CAPABILITY,
    H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY,
    H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE,
    H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE,
    H5_PROFILE_FLUTTER_SETTINGS_CAPABILITY,
    H5_PROFILE_HEADER_SETTINGS_CAPABILITY,
    H5_PROFILE_OPTIONS_SETTINGS_CAPABILITY,
    H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE,
    H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE,
    H5_PROFILE_SETTINGS_TARGET_MODE,
    H5_PROFILE_TOP_RIGHT_SETTINGS_CAPABILITY,
    H5_SETTINGS_PAGE_BACK_CAPABILITY,
    H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE,
    H5_SETTINGS_PAGE_BACK_TARGET_MODE,
    h5_profile_favorites_page_back_snapshot_element_matches,
    h5_profile_favorites_page_back_source_type_allowed,
    h5_profile_settings_capability_for_target,
    h5_profile_settings_snapshot_element_matches,
    h5_settings_page_back_snapshot_element_matches,
)
from services.api.app.application.h5_profile_visual_navigation import (
    h5_profile_visual_entry_contract,
)
from services.api.app.application.h5_safety_interstitial import (
    H5_SAFETY_INTERSTITIAL_CAPABILITY,
    H5_SAFETY_INTERSTITIAL_SELECTOR,
    H5_SAFETY_INTERSTITIAL_TARGET,
    H5_SAFETY_INTERSTITIAL_TARGET_MODE,
    h5_snapshot_is_safety_interstitial,
    h5_snapshot_safety_checkbox_selectors,
)
from services.api.app.application.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
    H5_BOTTOM_NAVIGATION_FAMILY_FLUTTER,
    H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP,
    H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA,
    H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT,
    H5_BOTTOM_NAVIGATION_FAMILY_VANT,
    H5_GENERIC_NAVIGATION_CONTAINER_CLASSES,
    H5_GENERIC_NAVIGATION_ITEM_CLASSES,
    h5_bottom_navigation_family,
    h5_bottom_navigation_selector,
    h5_flutter_bottom_navigation_runtime_selector,
    h5_page_tab_target,
)
from services.api.app.application.interaction_runtime import (
    H5_DYNAMIC_LOGICAL_TARGET_MODES,
    ResolvedInteractionTarget,
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
    TrustedUiSnapshotReader,
    VisualNavigationRuntimeTarget,
    logical_target_snapshot_identity_signature,
    logical_target_snapshot_signature,
)
from services.api.app.application.web_detail_candidate_extraction import (
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_logical_page import web_logical_page_id
from services.api.app.application.web_primary_content_capture import (
    resolve_web_primary_content_selector,
)
from services.api.app.domain.exploration.h5_logical_surface import (
    LOGICAL_SURFACE_PROTOCOL_VERSION,
    stable_h5_surface_hash,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    VISUAL_NAVIGATION_CAPABILITY_PROTOCOL_VERSION,
    VisualNavigationCapability,
    evaluate_visual_navigation_target,
    verify_visual_navigation_capability,
    visual_navigation_region_digests,
)
from services.api.app.domain.exploration.web_exploration import (
    build_web_obstruction_candidates,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceSessionStatus
from services.api.app.domain.page.web_url_identity import (
    DEFAULT_WEB_URL_IDENTITY_POLICY,
    WebUrlIdentityPolicy,
)
from services.api.app.infrastructure.browser.page_capture import (
    FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR,
    FLUTTER_STARTUP_DISMISS_TEXTS,
    GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR,
    POPUP_DIALOG_ACCESSIBLE_CLOSE_SELECTOR,
    POPUP_DIALOG_ACKNOWLEDGEMENT_SELECTOR,
    POPUP_DIALOG_ACTIVE_AD_CLOSE_SELECTOR,
    POPUP_DIALOG_CONTENT_CLOSE_SELECTOR,
    POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
    POPUP_DIALOG_TEXT_AD_CLOSE_SELECTOR,
    POPUP_FLUTTER_ACCOUNT_NOTICE_TOP_RIGHT_CLOSE_SELECTOR,
    POPUP_FLUTTER_APP_OPEN_CLOSE_SELECTOR,
    POPUP_FLUTTER_CENTERED_AD_CLOSE_ABOVE_SELECTOR,
    POPUP_FLUTTER_CENTERED_AD_CLOSE_SELECTOR,
    POPUP_FLUTTER_CENTERED_SCROLL_CLOSE_BELOW_SELECTOR,
    POPUP_FLUTTER_IMAGE_TOP_RIGHT_CLOSE_SELECTOR,
    POPUP_FLUTTER_MARKETING_ANNOUNCEMENT_CONFIRM_SELECTOR,
    POPUP_HOME_SUSPEND_CLOSE_SELECTOR,
    POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR,
    POPUP_OVERLAY_AD_CLOSE_SELECTOR,
)

_TARGETED_OPERATIONS = frozenset(
    {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.INPUT,
        InteractionOperation.HOVER,
        InteractionOperation.SEEK_MEDIA,
    }
)
_MAX_TARGET_TEXT = 2_000
_GENERIC_MEDIA_SEEK_TARGETS = frozenset(
    {
        "播放进度条",
        "视频播放进度条",
        "媒体播放进度条",
        "media progress",
        "video progress",
    }
)
_POPUP_DISMISSAL_SELECTORS = {
    H5_MODAL_CLOSE_CAPABILITY: H5_MODAL_CLOSE_SELECTOR,
    H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY: (H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR),
    H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY: (
        H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_SELECTOR
    ),
    "dialog_content_close": POPUP_DIALOG_CONTENT_CLOSE_SELECTOR,
    "dialog_active_ad_close": POPUP_DIALOG_ACTIVE_AD_CLOSE_SELECTOR,
    "dialog_text_ad_close": POPUP_DIALOG_TEXT_AD_CLOSE_SELECTOR,
    "dialog_accessible_close": POPUP_DIALOG_ACCESSIBLE_CLOSE_SELECTOR,
    "dialog_acknowledgement": POPUP_DIALOG_ACKNOWLEDGEMENT_SELECTOR,
    "dialog_generic_close": POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
    "dialog_overlay": ".van-overlay:visible",
    "home_suspend_close": POPUP_HOME_SUSPEND_CLOSE_SELECTOR,
    "overlay_ad_close": POPUP_OVERLAY_AD_CLOSE_SELECTOR,
    "homepage_payment_close": POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR,
    "flutter_app_open_popup_close": POPUP_FLUTTER_APP_OPEN_CLOSE_SELECTOR,
    "flutter_centered_ad_popup_close": POPUP_FLUTTER_CENTERED_AD_CLOSE_SELECTOR,
    "flutter_centered_ad_popup_close_above": (POPUP_FLUTTER_CENTERED_AD_CLOSE_ABOVE_SELECTOR),
    "flutter_centered_scroll_popup_close_below": (
        POPUP_FLUTTER_CENTERED_SCROLL_CLOSE_BELOW_SELECTOR
    ),
    "flutter_image_popup_top_right_close": (POPUP_FLUTTER_IMAGE_TOP_RIGHT_CLOSE_SELECTOR),
    "flutter_account_notice_top_right_close": (
        POPUP_FLUTTER_ACCOUNT_NOTICE_TOP_RIGHT_CLOSE_SELECTOR
    ),
    "flutter_marketing_announcement_confirm": (
        POPUP_FLUTTER_MARKETING_ANNOUNCEMENT_CONFIRM_SELECTOR
    ),
    "visible_text_acknowledgement": "text=我知道了",
    "visible_text_close": "text=关闭",
    "visible_text_skip": "text=跳过",
    "visible_text_later": "text=稍后",
}
_DIALOG_POPUP_CAPABILITIES = frozenset(
    capability for capability in _POPUP_DISMISSAL_SELECTORS if capability.startswith("dialog_")
)

_PAGE_TAB_NAVIGATION_TARGET_TEMPLATE = ".van-tabs:visible .van-tab:nth-of-type({index}):visible"
_VISUAL_NAVIGATION_TARGET_MODE = "visual_navigation_coordinate"
_VISUAL_NAVIGATION_CAPABILITY_FIELDS = frozenset(
    item.name for item in fields(VisualNavigationCapability)
)
_VISUAL_NAVIGATION_SIGNED_FIELDS = _VISUAL_NAVIGATION_CAPABILITY_FIELDS.difference(
    {"target_signature"}
)
_VISUAL_NAVIGATION_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_VISUAL_NAVIGATION_VIEWPORT_FIELDS = {
    "width": "visual_viewport_width",
    "height": "visual_viewport_height",
    "offset_left": "visual_viewport_offset_left",
    "offset_top": "visual_viewport_offset_top",
    "page_left": "visual_viewport_page_left",
    "page_top": "visual_viewport_page_top",
    "scale": "visual_viewport_scale",
}
_VISUAL_NAVIGATION_IDENTITY_TRANSFORM = {
    "kind": "identity",
    "scale_x": 1.0,
    "scale_y": 1.0,
    "offset_x": 0.0,
    "offset_y": 0.0,
}


class UiSnapshotSemanticTargetResolver:
    """只在平台层读取 selector，并要求最高分候选唯一。"""

    def __init__(
        self,
        *,
        snapshot_reader: TrustedUiSnapshotReader,
        logical_targets_enabled: bool = False,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
        web_identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY,
        web_discovery_authorizer: object | None = None,
    ) -> None:
        self._web_discovery_authorizer = web_discovery_authorizer
        if not isinstance(logical_targets_enabled, bool):
            raise TypeError("logical_targets_enabled 必须为布尔值")
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        self._snapshot_reader = snapshot_reader
        self._web_identity_policy = web_identity_policy
        self._logical_targets_enabled = logical_targets_enabled
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode

    def resolve_unique_selector(
        self,
        *,
        snapshot: Mapping[str, Any],
        semantic_target: str,
        operation: InteractionOperation,
        h5_navigation_target: str | None = None,
    ) -> str:
        """供平台内部专用 Runtime 从已读取的受信 Snapshot 解析唯一目标。"""
        target = _normalized_text(semantic_target)
        if not target:
            raise SemanticTargetResolutionError(
                "目标动作缺少 semantic_target",
                error_code="SEMANTIC_TARGET_MISSING",
            )
        if operation is InteractionOperation.SEEK_MEDIA:
            return _select_unique_seek_target(
                snapshot,
                semantic_target=target,
            )
        return _select_unique_target(
            snapshot,
            semantic_target=target,
            operation=operation,
            h5_navigation_target=h5_navigation_target,
        )

    async def resolve(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        target_mode = request.parameters.get("target_mode")
        visual_navigation_provenance = bool(
            target_mode == _VISUAL_NAVIGATION_TARGET_MODE
            or "visual_navigation_capability" in request.parameters
            or request.parameters.get("visual_navigation_provenance") is not None
            or request.parameters.get("visual_target_kind") is not None
        )
        if visual_navigation_provenance:
            if not self._logical_targets_enabled or self._h5_visual_coordinate_mode != "enforced":
                raise SemanticTargetResolutionError(
                    "当前运行时未启用可信视觉导航目标",
                    error_code="VISUAL_TARGET_NOT_ALLOWLISTED",
                )
            if target_mode != _VISUAL_NAVIGATION_TARGET_MODE:
                raise SemanticTargetResolutionError(
                    "视觉导航能力与目标模式不一致",
                    error_code="VISUAL_TARGET_INVALID",
                )
            return await self._resolve_visual_navigation_target(request)
        logical_target = request.parameters.get("logical_target")
        if not self._logical_targets_enabled or logical_target is None:
            return await self._resolve_legacy(request)
        if not isinstance(logical_target, Mapping):
            raise SemanticTargetResolutionError(
                "逻辑目标合同无效",
                error_code="LOGICAL_TARGET_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        if request.parameters.get("target_mode") == "visual_coordinate":
            raise SemanticTargetResolutionError(
                "纯视觉坐标未绑定结构目标，禁止授权逻辑页面点击",
                error_code="VISUAL_TARGET_REQUIRES_STRUCTURAL_ANCHOR",
                retryable=True,
            )
        resolved = await self._resolve_legacy(request)
        source_state = _validate_logical_source_state(
            snapshot=snapshot,
            contract=logical_target,
            request=request,
            resolved=resolved,
        )
        return _bind_trusted_logical_target(
            snapshot=snapshot,
            request=request,
            resolved=resolved,
            source_state=source_state,
        )

    async def _resolve_visual_navigation_target(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        raw_capability = request.parameters.get("visual_navigation_capability")
        capability = _visual_navigation_schema_and_signature(raw_capability)
        _validate_visual_navigation_request_source(
            request=request,
            capability=capability,
        )
        try:
            snapshot = await self._snapshot_reader.read_ui_snapshot(
                task_id=request.task_id,
                page_capture_id=request.source_capture_id,
            )
        except SemanticTargetResolutionError as exc:
            raise SemanticTargetResolutionError(
                "视觉导航来源 Snapshot 无法通过可信读取",
                error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
                retryable=True,
            ) from exc
        if not isinstance(snapshot, Mapping):
            raise SemanticTargetResolutionError(
                "视觉导航来源 Snapshot 无效",
                error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
                retryable=True,
            )
        _validate_visual_navigation_logical_state(
            snapshot=snapshot,
            capability=capability,
        )
        _validate_visual_navigation_render_gate(
            snapshot=snapshot,
            capability=capability,
        )
        source_png = await _read_visual_navigation_source_png(
            snapshot_reader=self._snapshot_reader,
            request=request,
            capability=capability,
        )
        decoded = _validate_visual_navigation_viewport_and_transform(
            snapshot=snapshot,
            capability=capability,
            source_png=source_png,
        )
        _validate_visual_navigation_target_policy(
            snapshot=snapshot,
            request=request,
            capability=capability,
        )
        try:
            verified = verify_visual_navigation_capability(capability)
        except ValueError as exc:
            raise SemanticTargetResolutionError(
                "视觉导航坐标合同无效",
                error_code="VISUAL_TARGET_COORDINATE_INVALID",
            ) from exc
        _validate_visual_navigation_region_material(
            decoded=decoded,
            capability=verified,
        )
        return ResolvedInteractionTarget(
            visual_navigation_target=VisualNavigationRuntimeTarget(
                capability=verified,
                source_png=source_png,
                source_image_sha256=verified.source_artifact_sha256,
            ),
            target_trust="visual_coordinate",
            trusted_target_texts=(
                f"进入{verified.expected_page_type}页面",
                verified.target_kind,
                *verified.risk_flags,
            ),
        )

    async def _resolve_legacy(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        operation = request.action_type
        if request.parameters.get("target_mode") == "web_discovery_candidate":
            if (
                self._web_discovery_authorizer is None
                or not await self._web_discovery_authorizer.authorize(request)
            ):
                raise SemanticTargetResolutionError(
                    "Web 目录外发现缺少当前 Run 的冻结授权",
                    error_code="WEB_DISCOVERY_NOT_AUTHORIZED",
                )
            from services.api.app.infrastructure.browser.web_discovery_target import (
                resolve_web_discovery_target,
            )

            return await resolve_web_discovery_target(
                request,
                snapshot_reader=self._snapshot_reader,
            )
        if request.parameters.get("target_mode") == "web_search_discovery_candidate":
            return await self._resolve_web_auxiliary_navigation_candidate(request)
        if request.parameters.get("target_mode") == "web_detail_candidate_url":
            return await self._resolve_web_detail_candidate_url(request)
        if operation is InteractionOperation.NAVIGATE:
            return ResolvedInteractionTarget(
                destination_url=_validated_web_url(
                    _required_parameter(request.parameters, "destination_url")
                )
            )
        if operation is InteractionOperation.NETWORK_FAULT:
            return ResolvedInteractionTarget(
                fault_url_pattern=_required_parameter(
                    request.parameters,
                    "fault_url_pattern",
                )
            )
        if request.parameters.get("target_mode") == "h5_probe_popup_dismissal":
            return await self._resolve_h5_probe_popup_dismissal(request)
        if (
            operation is InteractionOperation.CLICK
            and _normalized_text(request.semantic_target) == "关闭当前弹窗"
            and request.parameters.get("target_mode") != "popup_dismissal"
        ):
            # “关闭当前弹窗”是平台能力的虚拟语义，不对应页面上的真实文案。
            # 旧 Work 或异常载荷缺少能力参数时必须在读取 Snapshot 前失败关闭，
            # 禁止退化成普通文本匹配并误报 SEMANTIC_TARGET_NOT_FOUND。
            raise SemanticTargetResolutionError(
                "弹窗关闭动作缺少已验签的平台能力参数",
                error_code="ACTION_CAPABILITY_MISSING",
                retryable=True,
            )
        if request.parameters.get("target_mode") == "startup_ad_dismissal":
            return await self._resolve_startup_ad_dismissal(request)
        if request.parameters.get("target_mode") == H5_AGE_VERIFICATION_TARGET_MODE:
            return await self._resolve_age_verification_confirm(request)
        if request.parameters.get("target_mode") == "web_sealed_candidate":
            return await self._resolve_web_sealed_candidate(request)
        if request.parameters.get("target_mode") == "web_navigation_candidate":
            return await self._resolve_web_navigation_candidate(request)
        if request.parameters.get("target_mode") == "web_detail_candidate":
            return await self._resolve_web_detail_candidate(request)
        if request.parameters.get("target_mode") == "web_primary_content_candidate":
            return await self._resolve_web_primary_content_candidate(request)
        if request.parameters.get("target_mode") == H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE:
            return await self._resolve_line_loading_login_retry(request)
        if request.parameters.get("target_mode") == "popup_dismissal":
            return await self._resolve_popup_dismissal(request)
        if request.parameters.get("target_mode") == H5_FLUTTER_PROFILE_LOGIN_TARGET_MODE:
            return await self._resolve_flutter_profile_login_entry(request)
        if request.parameters.get("target_mode") in {
            "bottom_navigation",
            "page_tab_navigation",
        }:
            return await self._resolve_structural_navigation(request)
        if request.parameters.get("target_mode") == "list_detail_navigation":
            return await self._resolve_list_detail_navigation(request)
        if request.parameters.get("target_mode") == "h5_probe_display_target":
            return await self._resolve_probe_display_target(request)
        if request.parameters.get("target_mode") == H5_PROFILE_ACTIVITY_TARGET_MODE:
            return await self._resolve_profile_activity_navigation(request)
        if request.parameters.get("target_mode") == H5_PROFILE_PAYMENT_TARGET_MODE:
            return await self._resolve_profile_payment_navigation(request)
        if request.parameters.get("target_mode") == H5_PAYMENT_HORIZONTAL_TARGET_MODE:
            return await self._resolve_payment_horizontal_collection(request)
        if request.parameters.get("target_mode") == H5_PROFILE_SETTINGS_TARGET_MODE:
            return await self._resolve_profile_settings_navigation(request)
        if request.parameters.get("target_mode") == H5_SETTINGS_PAGE_BACK_TARGET_MODE:
            return await self._resolve_settings_page_back(request)
        if request.parameters.get("target_mode") == H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE:
            return await self._resolve_profile_favorites_page_back(request)
        if request.parameters.get("target_mode") == H5_SAFETY_INTERSTITIAL_TARGET_MODE:
            return await self._resolve_safety_interstitial_bypass(request)
        if request.parameters.get("target_mode") == "visual_coordinate":
            return await self._resolve_profile_payment_visual_target(request)
        if operation not in _TARGETED_OPERATIONS:
            return ResolvedInteractionTarget()

        semantic_target = _normalized_text(request.semantic_target)
        if not semantic_target:
            raise SemanticTargetResolutionError(
                "目标动作缺少 semantic_target",
                error_code="SEMANTIC_TARGET_MISSING",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        selector = self.resolve_unique_selector(
            snapshot=snapshot,
            semantic_target=semantic_target,
            operation=operation,
            h5_navigation_target=(
                request.semantic_target
                if request.session_id.startswith("h5:")
                and operation in {InteractionOperation.CLICK, InteractionOperation.TAP,
                                  InteractionOperation.HOVER}
                else None
            ),
        )
        trusted_target_texts = _trusted_target_semantics_for_selector(
            snapshot,
            selector,
        )
        if operation is not InteractionOperation.INPUT:
            return ResolvedInteractionTarget(
                selector=selector,
                clear_obstructions=request.parameters.get("clear_obstructions") is True,
                trusted_target_texts=trusted_target_texts,
                trusted_target_role=_trusted_target_role_for_selector(snapshot, selector),
            )
        input_text = _required_parameter(request.parameters, "input_text")
        if len(input_text) > _MAX_TARGET_TEXT:
            raise SemanticTargetResolutionError(
                f"input_text 最多允许 {_MAX_TARGET_TEXT} 字符",
                error_code="INPUT_TEXT_TOO_LONG",
            )
        submit_target = _optional_parameter(request.parameters, "submit_semantic_target")
        submit_selector = None
        if submit_target is not None:
            submit_selector = _select_unique_target(
                snapshot,
                semantic_target=_normalized_text(submit_target),
                operation=InteractionOperation.CLICK,
            )
            trusted_target_texts = tuple(
                dict.fromkeys(
                    (
                        *trusted_target_texts,
                        *_trusted_target_semantics_for_selector(snapshot, submit_selector),
                    )
                )
            )
        return ResolvedInteractionTarget(
            selector=selector,
            submit_selector=submit_selector,
            input_text=input_text,
            trusted_target_texts=trusted_target_texts,
            trusted_target_role=_trusted_target_role_for_selector(snapshot, selector),
        )

    async def _resolve_web_auxiliary_navigation_candidate(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从同一可信来源 Snapshot 重建搜索或活动 href，绝不接受 selector。"""

        if request.action_type is not InteractionOperation.NAVIGATE:
            raise SemanticTargetResolutionError(
                "Web 辅助入口只允许 URL 导航",
                error_code="WEB_AUXILIARY_CANDIDATE_OPERATION_NOT_ALLOWED",
            )
        if "selector" in request.parameters:
            raise SemanticTargetResolutionError(
                "Web 辅助入口参数不得携带 selector",
                error_code="WEB_AUXILIARY_CANDIDATE_TARGET_FORBIDDEN",
            )
        source_capture_id = _required_parameter(
            request.parameters,
            "web_auxiliary_source_capture_id",
        )
        before_capture_id = _required_parameter(
            request.parameters,
            "web_auxiliary_before_action_capture_id",
        )
        if before_capture_id != request.source_capture_id:
            raise SemanticTargetResolutionError(
                "Web 辅助入口动作前 Capture 不一致",
                error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
            )
        candidate_source_capture_id = str(
            request.parameters.get("web_auxiliary_candidate_source_capture_id") or source_capture_id
        ).strip()
        if not candidate_source_capture_id:
            raise SemanticTargetResolutionError(
                "Web 辅助入口候选来源 Capture 缺失",
                error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
            )
        if candidate_source_capture_id != source_capture_id:
            raise SemanticTargetResolutionError(
                "Web 辅助入口候选必须来自本次动作来源 Capture",
                error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
            )
        origin = _required_parameter(request.parameters, "web_auxiliary_origin")
        candidate_id = _required_parameter(
            request.parameters,
            "web_auxiliary_candidate_id",
        )
        declared_kind = _required_parameter(request.parameters, "web_auxiliary_kind")
        if declared_kind not in {"search_href", "activity_href"}:
            raise SemanticTargetResolutionError(
                "跨域推广不得进入普通页面导航",
                error_code="WEB_AUXILIARY_CANDIDATE_KIND_NOT_NAVIGABLE",
            )
        before_snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=before_capture_id,
        )
        binding_reader = getattr(
            self._snapshot_reader,
            "read_web_before_action_binding",
            None,
        )
        if not isinstance(before_snapshot, Mapping) or not callable(binding_reader):
            raise _web_auxiliary_source_mismatch("Web 辅助入口动作前 Capture 缺少可信证据")
        before_snapshot_capture_id = str(before_snapshot.get("capture_id") or before_capture_id)
        if before_snapshot_capture_id != before_capture_id:
            raise _web_auxiliary_source_mismatch("Web 辅助入口动作前 Snapshot 已漂移")
        binding = await binding_reader(
            task_id=request.task_id,
            page_capture_id=before_capture_id,
        )
        if not isinstance(binding, Mapping):
            raise _web_auxiliary_source_mismatch("Web 辅助入口动作前 Capture 绑定不存在")
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=candidate_source_capture_id,
        )
        if not isinstance(snapshot, Mapping):
            raise SemanticTargetResolutionError(
                "Web 辅助入口来源 Snapshot 无效",
                error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
            )
        snapshot_capture_id = str(snapshot.get("capture_id") or candidate_source_capture_id)
        if snapshot_capture_id != candidate_source_capture_id:
            raise SemanticTargetResolutionError(
                "Web 辅助入口来源 Snapshot 已漂移",
                error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
            )
        _validate_web_auxiliary_before_action_binding(
            request=request,
            binding=binding,
            discovery_snapshot=snapshot,
            before_snapshot=before_snapshot,
            discovery_capture_id=candidate_source_capture_id,
            before_capture_id=before_capture_id,
        )
        from services.api.app.application.web_auxiliary_navigation import (
            discover_web_auxiliary_navigation_candidates,
            web_auxiliary_semantic_target,
        )

        candidates = discover_web_auxiliary_navigation_candidates(
            {**snapshot, "capture_id": candidate_source_capture_id},
            origin=origin,
        )
        matches = [
            item
            for item in candidates
            if item.candidate_id == candidate_id and item.kind == declared_kind
        ]
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "Web 辅助入口已失效或不唯一",
                error_code="WEB_AUXILIARY_CANDIDATE_STALE",
                retryable=True,
            )
        candidate = matches[0]
        destination_url = _validated_web_url(
            _required_parameter(request.parameters, "destination_url")
        )
        if destination_url != candidate.url:
            raise SemanticTargetResolutionError(
                "Web 辅助入口目标 URL 与可信 href 不一致",
                error_code="WEB_AUXILIARY_CANDIDATE_TARGET_MISMATCH",
            )
        expected_semantic_target = web_auxiliary_semantic_target(candidate.kind)
        if _normalized_text(request.semantic_target) != expected_semantic_target:
            raise SemanticTargetResolutionError(
                "Web 辅助入口语义目标不一致",
                error_code="WEB_AUXILIARY_CANDIDATE_TARGET_MISMATCH",
            )
        return ResolvedInteractionTarget(
            destination_url=destination_url,
            target_source="web_auxiliary_navigation_candidate",
            target_signature=candidate.candidate_id,
            target_trust="trusted",
            trusted_target_texts=(candidate.label,),
        )

    async def _resolve_web_sealed_candidate(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从源 Capture 重建候选 ID，并在平台层绑定可信 selector。"""

        if request.action_type is not InteractionOperation.CLICK:
            raise SemanticTargetResolutionError(
                "Web 密封候选只允许 click",
                error_code="WEB_SEALED_CANDIDATE_OPERATION_NOT_ALLOWED",
            )
        if "selector" in request.parameters:
            raise SemanticTargetResolutionError(
                "Web 密封候选参数不得携带 selector",
                error_code="WEB_SEALED_CANDIDATE_SELECTOR_FORBIDDEN",
            )
        candidate_id = _optional_parameter(
            request.parameters,
            "web_sealed_candidate_id",
        )
        sealed_source_capture_id = _optional_parameter(
            request.parameters,
            "web_sealed_source_capture_id",
        )
        capability = _optional_parameter(
            request.parameters,
            "web_sealed_candidate_capability",
        )
        allowed_capabilities = {
            "web_age_gate_confirm",
            "web_obstruction_dismiss",
        }
        if (
            candidate_id is None
            or sealed_source_capture_id is None
            or capability not in allowed_capabilities
        ):
            raise SemanticTargetResolutionError(
                "Web 成年确认密封候选参数无效",
                error_code="WEB_SEALED_CANDIDATE_CAPABILITY_INVALID",
            )
        sealed_snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=sealed_source_capture_id,
        )
        sealed_candidates = build_web_obstruction_candidates(
            capture_id=sealed_source_capture_id,
            ui_snapshot=sealed_snapshot,
        )
        if not any(item.candidate_id == candidate_id for item in sealed_candidates):
            raise SemanticTargetResolutionError(
                "Web 密封候选不属于声明的原始 Capture",
                error_code="WEB_SEALED_CANDIDATE_SOURCE_MISMATCH",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        candidates = build_web_obstruction_candidates(
            capture_id=request.source_capture_id,
            ui_snapshot=snapshot,
        )
        candidate = next(
            (item for item in candidates if item.candidate_id == candidate_id),
            None,
        )
        if candidate is None:
            raise SemanticTargetResolutionError(
                "Web 密封候选已漂移或不属于源 Capture",
                error_code="WEB_SEALED_CANDIDATE_STALE",
                retryable=True,
            )
        normalized_text = re.sub(r"\s+", "", candidate.text).casefold()
        if candidate.capability != capability or not _web_candidate_text_is_safe(
            capability=capability,
            normalized_text=normalized_text,
        ):
            raise SemanticTargetResolutionError(
                "Web 密封候选未通过成年确认风险复验",
                error_code="WEB_SEALED_CANDIDATE_RISK_BLOCKED",
            )
        expected_target = {
            "web_age_gate_confirm": "确认已成年并进入",
            "web_obstruction_dismiss": "关闭当前阻断弹窗",
        }[capability]
        if _normalized_text(request.semantic_target) != expected_target:
            raise SemanticTargetResolutionError(
                "Web 成年确认语义目标不一致",
                error_code="WEB_SEALED_CANDIDATE_TARGET_MISMATCH",
            )
        return ResolvedInteractionTarget(
            selector=candidate.selector,
            expected_rect=dict(candidate.rect),
            target_source="web_sealed_candidate",
            target_signature=candidate.candidate_id,
            source_state_fingerprint=str(snapshot.get("structure_signature") or "") or None,
            target_trust="trusted",
        )

    async def _resolve_web_detail_candidate(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从源无遮挡列表 Snapshot 重建一层详情候选。"""

        if request.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.SCROLL,
        }:
            raise SemanticTargetResolutionError(
                "Web 详情候选只允许 click 或 scroll",
                error_code="WEB_DETAIL_CANDIDATE_OPERATION_NOT_ALLOWED",
            )
        if "selector" in request.parameters or "destination_url" in request.parameters:
            raise SemanticTargetResolutionError(
                "Web 详情候选参数不得携带 selector 或目标 URL",
                error_code="WEB_DETAIL_CANDIDATE_TARGET_FORBIDDEN",
            )
        candidate_id = _required_parameter(
            request.parameters,
            "web_detail_candidate_id",
        )
        sealed_source_capture_id = _required_parameter(
            request.parameters,
            "web_detail_source_capture_id",
        )
        list_page_type = _required_parameter(
            request.parameters,
            "web_detail_list_page_type",
        )
        origin = _required_parameter(request.parameters, "web_detail_origin")
        sealed_snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=sealed_source_capture_id,
        )
        sealed_snapshot = _bind_detail_snapshot_capture(
            sealed_snapshot,
            expected_capture_id=sealed_source_capture_id,
        )
        current_url = str(
            request.parameters.get("web_detail_current_url") or sealed_snapshot.get("url") or origin
        )
        sealed_candidates = detail_candidates_from_ready_snapshot(
            sealed_snapshot,
            list_page_type=list_page_type,
            origin=origin,
            current_url=current_url,
        )
        sealed_candidate = next(
            (item for item in sealed_candidates if item.candidate_id == candidate_id),
            None,
        )
        if sealed_candidate is None:
            raise SemanticTargetResolutionError(
                "Web 详情候选不属于声明的原始 Capture",
                error_code="WEB_DETAIL_CANDIDATE_SOURCE_MISMATCH",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        snapshot = _bind_detail_snapshot_capture(
            snapshot,
            expected_capture_id=request.source_capture_id,
        )
        candidates = detail_candidates_from_ready_snapshot(
            snapshot,
            list_page_type=list_page_type,
            origin=origin,
            current_url=current_url,
        )
        rebound_candidates = tuple(
            item
            for item in candidates
            if item.canonical_url == sealed_candidate.canonical_url
            and item.expected_page_type == sealed_candidate.expected_page_type
        )
        candidate = rebound_candidates[0] if len(rebound_candidates) == 1 else None
        if candidate is None:
            raise SemanticTargetResolutionError(
                "Web 详情候选已漂移或不属于源 Capture",
                error_code="WEB_DETAIL_CANDIDATE_STALE",
                retryable=True,
            )
        expected_target = (
            "滚动到当前列表可信详情"
            if request.action_type is InteractionOperation.SCROLL
            else "打开当前列表可信详情"
        )
        if _normalized_text(request.semantic_target) != expected_target:
            raise SemanticTargetResolutionError(
                "Web 详情候选语义目标不一致",
                error_code="WEB_DETAIL_CANDIDATE_TARGET_MISMATCH",
            )
        return ResolvedInteractionTarget(
            selector=candidate.selector,
            expected_rect=dict(candidate.rect),
            target_source="web_detail_candidate",
            target_signature=candidate_id,
            source_state_fingerprint=str(snapshot.get("structure_signature") or "") or None,
            target_trust="trusted",
        )

    async def _resolve_web_navigation_candidate(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """复核无 URL 导航候选并在同一 Capture 上绑定唯一可点击控件。"""

        if request.action_type is not InteractionOperation.CLICK:
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选只允许 click",
                error_code="WEB_NAVIGATION_CANDIDATE_OPERATION_NOT_ALLOWED",
            )
        if "selector" in request.parameters or "destination_url" in request.parameters:
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选不得携带 selector 或目标 URL",
                error_code="WEB_NAVIGATION_CANDIDATE_TARGET_FORBIDDEN",
            )
        candidate_id = _required_parameter(
            request.parameters,
            "web_navigation_candidate_id",
        )
        source_capture_id = _required_parameter(
            request.parameters,
            "web_navigation_source_capture_id",
        )
        if source_capture_id != request.source_capture_id:
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选来源 Capture 不一致",
                error_code="WEB_NAVIGATION_CANDIDATE_SOURCE_MISMATCH",
            )
        dom_fingerprint = _required_parameter(
            request.parameters,
            "web_navigation_dom_fingerprint",
        )
        target_hint = _required_parameter(
            request.parameters,
            "web_navigation_target_hint",
        )
        source_kind = _required_parameter(
            request.parameters,
            "web_navigation_source_kind",
        )
        if source_kind not in {
            "semantic_link",
            "button",
            "js_control",
            "spa_route",
            "visual_card",
        }:
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选来源类型不允许",
                error_code="WEB_NAVIGATION_CANDIDATE_RISK_BLOCKED",
            )
        source_epoch = request.parameters.get("web_navigation_source_epoch")
        menu_path = request.parameters.get("web_navigation_menu_path")
        if (
            isinstance(source_epoch, bool)
            or not isinstance(source_epoch, int)
            or source_epoch < 0
            or not isinstance(menu_path, list)
            or not menu_path
            or any(not isinstance(item, str) or not item.strip() for item in menu_path)
        ):
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选结构参数无效",
                error_code="WEB_NAVIGATION_CANDIDATE_INVALID",
            )
        _validated_web_url(_required_parameter(request.parameters, "web_navigation_expected_url"))
        if _normalized_text(request.semantic_target) != _normalized_text(menu_path[-1]):
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选语义目标不一致",
                error_code="WEB_NAVIGATION_CANDIDATE_TARGET_MISMATCH",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        stored_capture_id = snapshot.get("capture_id")
        if stored_capture_id is not None and stored_capture_id != request.source_capture_id:
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选 Snapshot 来源不一致",
                error_code="WEB_NAVIGATION_CANDIDATE_SOURCE_MISMATCH",
            )
        navigation = snapshot.get("web_navigation")
        raw_candidates = navigation.get("candidates") if isinstance(navigation, Mapping) else None
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        matches = [
            item
            for item in candidates
            if isinstance(item, Mapping)
            and item.get("url") is None
            and str(item.get("dom_fingerprint") or "") == dom_fingerprint
            and tuple(str(value).strip() for value in item.get("menu_path") or ())
            == tuple(str(value).strip() for value in menu_path)
            and str(item.get("target_hint") or "") == target_hint
            and str(item.get("source_kind") or "") == source_kind
            and item.get("source_epoch") == source_epoch
            and item.get("visibility") == "visible"
            and item.get("actionability") == "requires_runtime_validation"
            and item.get("risk_level") == "low"
        ]
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选已漂移或不唯一",
                error_code="WEB_NAVIGATION_CANDIDATE_STALE",
                retryable=True,
            )
        selector = _select_unique_target(
            snapshot,
            semantic_target=_normalized_text(request.semantic_target),
            operation=InteractionOperation.CLICK,
        )
        elements = snapshot.get("elements")
        raw_elements = elements if isinstance(elements, list) else []
        targets = [
            item
            for item in raw_elements
            if isinstance(item, Mapping)
            and str(item.get("selector") or "") == selector
            and item.get("visible") is True
            and item.get("interactive") is True
            and item.get("disabled") is not True
            and item.get("is_advertisement") is not True
            and (item.get("within_navigation") is True or source_kind == "visual_card")
        ]
        if len(targets) != 1:
            raise SemanticTargetResolutionError(
                "Web 运行时导航候选未通过可见导航结构复验",
                error_code="WEB_NAVIGATION_CANDIDATE_RISK_BLOCKED",
            )
        rect = targets[0].get("rect")
        return ResolvedInteractionTarget(
            selector=selector,
            expected_rect=dict(rect) if isinstance(rect, Mapping) else None,
            target_source="web_navigation_candidate",
            target_signature=candidate_id,
            source_state_fingerprint=str(snapshot.get("structure_signature") or "") or None,
            target_trust="trusted",
        )

    async def _resolve_web_primary_content_candidate(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从当前 Capture 重建非广告主体候选，仅允许受限滚动。"""

        if request.action_type is not InteractionOperation.SCROLL:
            raise SemanticTargetResolutionError(
                "Web 主体候选只允许 scroll",
                error_code="WEB_PRIMARY_CONTENT_OPERATION_NOT_ALLOWED",
            )
        if "selector" in request.parameters:
            raise SemanticTargetResolutionError(
                "Web 主体候选参数不得携带 selector",
                error_code="WEB_PRIMARY_CONTENT_SELECTOR_FORBIDDEN",
            )
        discovery_capture_id = _required_parameter(
            request.parameters,
            "web_primary_content_discovery_capture_id",
        )
        if discovery_capture_id != _required_parameter(
            request.parameters,
            "web_primary_content_source_capture_id",
        ):
            raise SemanticTargetResolutionError(
                "Web 主体候选发现来源不一致",
                error_code="WEB_PRIMARY_CONTENT_SOURCE_MISMATCH",
            )
        before_capture_id = _required_parameter(
            request.parameters,
            "web_primary_content_before_action_capture_id",
        )
        if before_capture_id != request.source_capture_id:
            raise SemanticTargetResolutionError(
                "Web 主体候选动作前 Capture 不一致",
                error_code="WEB_PRIMARY_CONTENT_BEFORE_CAPTURE_MISMATCH",
            )
        candidate_id = _required_parameter(
            request.parameters,
            "web_primary_content_candidate_id",
        )
        expected_page_type = _required_parameter(
            request.parameters,
            "expected_page_type",
        )
        logical_page_id = _required_parameter(request.parameters, "logical_page_id")
        if _normalized_text(request.semantic_target) != "滚动到非广告主体内容":
            raise SemanticTargetResolutionError(
                "Web 主体滚动语义目标不一致",
                error_code="WEB_PRIMARY_CONTENT_TARGET_MISMATCH",
            )
        discovery_snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=discovery_capture_id,
        )
        discovery_url = discovery_snapshot.get("url") or discovery_snapshot.get("page_url")
        try:
            discovery_logical_page_id = web_logical_page_id(
                str(discovery_url or ""),
                identity_policy=self._web_identity_policy,
            )
        except (TypeError, ValueError):
            discovery_logical_page_id = None
        if discovery_logical_page_id != logical_page_id:
            raise SemanticTargetResolutionError(
                "Web 主体候选发现逻辑页面已变化",
                error_code="WEB_PRIMARY_CONTENT_DISCOVERY_LOGICAL_PAGE_MISMATCH",
                retryable=True,
            )
        if (
            resolve_web_primary_content_selector(
                capture_id=discovery_capture_id,
                ui_snapshot=discovery_snapshot,
                expected_page_type=expected_page_type,
                candidate_id=candidate_id,
            )
            is None
        ):
            raise SemanticTargetResolutionError(
                "Web 主体候选不属于声明的发现 Capture",
                error_code="WEB_PRIMARY_CONTENT_DISCOVERY_CANDIDATE_MISMATCH",
                retryable=True,
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        current_url = snapshot.get("url") or snapshot.get("page_url")
        try:
            current_logical_page_id = web_logical_page_id(
                str(current_url or ""),
                identity_policy=self._web_identity_policy,
            )
        except (TypeError, ValueError):
            current_logical_page_id = None
        if current_logical_page_id != logical_page_id:
            raise SemanticTargetResolutionError(
                "Web 主体候选动作前逻辑页面已变化",
                error_code="WEB_PRIMARY_CONTENT_LOGICAL_PAGE_MISMATCH",
                retryable=True,
            )
        resolved = resolve_web_primary_content_selector(
            capture_id=request.source_capture_id,
            ui_snapshot=snapshot,
            expected_page_type=expected_page_type,
            candidate_id=candidate_id,
        )
        if resolved is None:
            raise SemanticTargetResolutionError(
                "Web 主体候选已漂移或不属于源 Capture",
                error_code="WEB_PRIMARY_CONTENT_CANDIDATE_STALE",
                retryable=True,
            )
        selector, target = resolved
        return ResolvedInteractionTarget(
            selector=selector,
            target_source="web_primary_content_candidate",
            target_signature=target.candidate_id,
            source_state_fingerprint=str(snapshot.get("structure_signature") or "") or None,
            target_trust="trusted",
        )

    async def _resolve_web_detail_candidate_url(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从原始无遮挡 Capture 复核候选 ID 与目标 URL 后授权导航。"""

        if request.action_type is not InteractionOperation.NAVIGATE:
            raise SemanticTargetResolutionError(
                "Web 详情 URL 候选只允许 navigate",
                error_code="WEB_DETAIL_CANDIDATE_OPERATION_NOT_ALLOWED",
            )
        if "selector" in request.parameters:
            raise SemanticTargetResolutionError(
                "Web 详情 URL 候选不得携带 selector",
                error_code="WEB_DETAIL_CANDIDATE_TARGET_FORBIDDEN",
            )
        candidate_id = _required_parameter(
            request.parameters,
            "web_detail_candidate_id",
        )
        sealed_source_capture_id = _required_parameter(
            request.parameters,
            "web_detail_source_capture_id",
        )
        before_capture_id = _required_parameter(
            request.parameters,
            "web_detail_before_action_capture_id",
        )
        if before_capture_id != request.source_capture_id or (
            before_capture_id == sealed_source_capture_id
        ):
            raise _web_detail_source_mismatch("Web 详情动作前 Capture 与发现 Capture 无法绑定")
        list_page_type = _required_parameter(
            request.parameters,
            "web_detail_list_page_type",
        )
        origin = _required_parameter(request.parameters, "web_detail_origin")
        current_url = _required_parameter(
            request.parameters,
            "web_detail_current_url",
        )
        destination_url = _validated_web_url(
            _required_parameter(request.parameters, "destination_url")
        )
        sealed_snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=sealed_source_capture_id,
        )
        sealed_snapshot = _bind_detail_snapshot_capture(
            sealed_snapshot,
            expected_capture_id=sealed_source_capture_id,
        )
        before_snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=before_capture_id,
        )
        before_snapshot = _bind_detail_snapshot_capture(
            before_snapshot,
            expected_capture_id=before_capture_id,
        )
        binding_reader = getattr(
            self._snapshot_reader,
            "read_web_before_action_binding",
            None,
        )
        if not callable(binding_reader):
            raise _web_detail_source_mismatch("Web 详情动作前 Capture 缺少可信绑定读取器")
        binding = await binding_reader(
            task_id=request.task_id,
            page_capture_id=before_capture_id,
        )
        if not isinstance(binding, Mapping):
            raise _web_detail_source_mismatch("Web 详情动作前 Capture 绑定不存在")
        trusted_source_url = _snapshot_web_url(sealed_snapshot, capability="Web 详情发现")
        if _normalized_web_location(current_url) != _normalized_web_location(
            trusted_source_url
        ) or _web_origin_identity(origin) != _web_origin_identity(trusted_source_url):
            raise _web_detail_source_mismatch("Web 详情声明页面与可信发现 Snapshot 已漂移")
        _validate_web_detail_before_action_binding(
            request=request,
            binding=binding,
            discovery_snapshot=sealed_snapshot,
            before_snapshot=before_snapshot,
            discovery_capture_id=sealed_source_capture_id,
            before_capture_id=before_capture_id,
            trusted_source_url=trusted_source_url,
        )
        candidate = next(
            (
                item
                for item in detail_candidates_from_ready_snapshot(
                    sealed_snapshot,
                    list_page_type=list_page_type,
                    origin=origin,
                    current_url=trusted_source_url,
                )
                if item.candidate_id == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise SemanticTargetResolutionError(
                "Web 详情 URL 候选不属于声明的原始 Capture",
                error_code="WEB_DETAIL_CANDIDATE_SOURCE_MISMATCH",
            )
        if destination_url != candidate.canonical_url:
            raise SemanticTargetResolutionError(
                "Web 详情候选与目标 URL 不一致",
                error_code="WEB_DETAIL_CANDIDATE_URL_MISMATCH",
            )
        if _normalized_text(request.semantic_target) != "打开当前列表可信详情":
            raise SemanticTargetResolutionError(
                "Web 详情候选语义目标不一致",
                error_code="WEB_DETAIL_CANDIDATE_TARGET_MISMATCH",
            )
        return ResolvedInteractionTarget(
            destination_url=candidate.canonical_url,
            target_source="web_detail_candidate_url",
            target_signature=candidate.candidate_id,
            source_state_fingerprint=str(sealed_snapshot.get("structure_signature") or "") or None,
            target_trust="trusted",
        )

    async def _resolve_flutter_profile_login_entry(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """把动态 Flutter 登录组转换为需实时复验的内部目标。"""
        if (
            request.action_type is not InteractionOperation.CLICK
            or _normalized_text(request.semantic_target)
            != _normalized_text(H5_FLUTTER_PROFILE_LOGIN_TARGET)
            or _optional_parameter(
                request.parameters,
                "flutter_profile_login_capability",
            )
            != H5_FLUTTER_PROFILE_LOGIN_CAPABILITY
        ):
            raise SemanticTargetResolutionError(
                "Flutter 个人中心登录入口参数无效",
                error_code="FLUTTER_PROFILE_LOGIN_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        if h5_unique_flutter_profile_login_entry(elements) is None:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未唯一证明 Flutter 个人中心登录入口",
                error_code="FLUTTER_PROFILE_LOGIN_ENTRY_NOT_FOUND",
                retryable=True,
            )
        return ResolvedInteractionTarget(
            selector=H5_FLUTTER_PROFILE_LOGIN_RUNTIME_SELECTOR,
        )

    async def _resolve_startup_ad_dismissal(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        if request.action_type is not InteractionOperation.CLICK:
            raise SemanticTargetResolutionError(
                "开屏关闭能力只允许 click",
                error_code="STARTUP_AD_OPERATION_NOT_ALLOWED",
            )
        if _normalized_text(request.semantic_target) not in FLUTTER_STARTUP_DISMISS_TEXTS:
            raise SemanticTargetResolutionError(
                "开屏关闭能力只接受已登记的退场语义目标",
                error_code="STARTUP_AD_TARGET_NOT_ALLOWED",
            )
        if _optional_parameter(request.parameters, "startup_ad_capability") != "countdown_close":
            raise SemanticTargetResolutionError(
                "开屏关闭能力标记无效",
                error_code="STARTUP_AD_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        matches: list[tuple[Mapping[str, Any], str]] = []
        for raw in elements:
            if not isinstance(raw, Mapping):
                continue
            structural_selector = _startup_ad_selector_from_snapshot_element(raw)
            if structural_selector is None:
                continue
            hit_test = raw.get("hit_test")
            if (
                structural_selector == STARTUP_AD_DISMISSAL_SELECTOR
                and _operation_compatible(raw, InteractionOperation.CLICK)
            ) or (
                structural_selector != STARTUP_AD_DISMISSAL_SELECTOR
                and isinstance(hit_test, Mapping)
                and hit_test.get("center_hits_target") is True
            ):
                matches.append((raw, structural_selector))
        flutter_skip_matches = _flutter_fullscreen_startup_skip_snapshot_matches(
            snapshot=snapshot,
            elements=elements,
        )
        matches.extend(
            (raw, FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR) for raw in flutter_skip_matches
        )
        if not matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明可交互的开屏倒计时关闭控件",
                error_code="STARTUP_AD_DISMISSAL_NOT_FOUND",
                retryable=True,
            )
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中开屏倒计时关闭控件不唯一",
                error_code="STARTUP_AD_DISMISSAL_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(
            selector=matches[0][1],
        )

    async def _resolve_popup_dismissal(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        if request.action_type is not InteractionOperation.CLICK:
            raise SemanticTargetResolutionError(
                "弹窗关闭能力只允许 click",
                error_code="POPUP_DISMISSAL_OPERATION_NOT_ALLOWED",
            )
        if _normalized_text(request.semantic_target) != "关闭当前弹窗":
            raise SemanticTargetResolutionError(
                "弹窗关闭能力只接受语义目标“关闭当前弹窗”",
                error_code="POPUP_DISMISSAL_TARGET_NOT_ALLOWED",
            )
        capability = _optional_parameter(
            request.parameters,
            "popup_dismissal_capability",
        )
        selector = _POPUP_DISMISSAL_SELECTORS.get(capability or "")
        if selector is None:
            raise SemanticTargetResolutionError(
                "弹窗关闭能力标记无效",
                error_code="POPUP_DISMISSAL_CAPABILITY_INVALID",
            )
        signature = _optional_parameter(request.parameters, "obstruction_signature")
        if not _valid_obstruction_signature(signature):
            raise SemanticTargetResolutionError(
                "弹窗关闭能力缺少合法遮挡指纹",
                error_code="POPUP_DISMISSAL_SIGNATURE_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        if capability == H5_MODAL_CLOSE_CAPABILITY:
            proof = h5_modal_close(snapshot)
            if proof is None or proof.obstruction_signature != signature:
                raise SemanticTargetResolutionError(
                    "当前顶层弹窗关闭控件与检测证明不一致",
                    error_code="POPUP_MODAL_SCOPE_MISMATCH",
                    retryable=True,
                )
            return _h5_probe_popup_target(
                selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
            )
        bottom_popup_overlay_selector = (
            _signed_bottom_popup_overlay_selector(elements)
            if capability == "dialog_overlay"
            else None
        )
        if capability == "dialog_overlay":
            # 能力固定选择器只会点遮罩背景；站点若不支持点遮罩关闭，
            # 每次都机械成功而遮挡不散，前沿随之饿死。证到唯一内部
            # 关闭键时改点该控件本身，与信息弹窗确认走同一范式。
            grid_dialog_close_selector = _grid_dialog_image_close_selector(elements)
            if grid_dialog_close_selector is not None:
                return ResolvedInteractionTarget(
                    selector=grid_dialog_close_selector,
                    clear_obstructions=True,
                    allow_high_z_obstruction_hide=True,
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                )
            homepage_payment_close = _homepage_payment_close_snapshot_element(elements)
            if homepage_payment_close is not None:
                return ResolvedInteractionTarget(
                    selector=POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR,
                    expected_rect=_trusted_logical_target_rect(homepage_payment_close),
                    clear_obstructions=True,
                    allow_high_z_obstruction_hide=True,
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                )
        if capability in {
            H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY,
            "flutter_marketing_announcement_confirm",
        }:
            acknowledgement = h5_modal_local_safe_acknowledgement(snapshot)
            if acknowledgement is None:
                raise SemanticTargetResolutionError(
                    "当前顶层信息弹窗未通过确认控件风险验签",
                    error_code="POPUP_ACKNOWLEDGEMENT_RISK_BLOCKED",
                )
            if (
                capability == H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY
                and acknowledgement.obstruction_signature != signature
            ):
                raise SemanticTargetResolutionError(
                    "当前信息弹窗与检测阶段签发的遮挡指纹不一致",
                    error_code="POPUP_MODAL_SCOPE_MISMATCH",
                    retryable=True,
                )
            if request.session_id.startswith("h5:"):
                return _h5_probe_popup_target(
                    selector=(
                        f"{H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR}:"
                        f"{acknowledgement.obstruction_signature}"
                    ),
                )
            return ResolvedInteractionTarget(
                selector=acknowledgement.selector,
                clear_obstructions=True,
                allow_high_z_obstruction_hide=True,
                h5_popup_dismissal_strategy="safe_outside_then_close",
            )
        if capability == H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY:
            proof = h5_profile_credential_screenshot_confirm(snapshot)
            if proof is None:
                raise SemanticTargetResolutionError(
                    "当前个人中心阻断弹窗未证明唯一保存确认按钮",
                    error_code="PROFILE_CREDENTIAL_CONFIRM_NOT_FOUND",
                    retryable=True,
                )
            if proof.obstruction_signature != signature:
                raise SemanticTargetResolutionError(
                    "当前个人中心阻断弹窗与检测阶段遮挡指纹不一致",
                    error_code="POPUP_MODAL_SCOPE_MISMATCH",
                    retryable=True,
                )
            source_element = next(
                (
                    item
                    for item in elements
                    if isinstance(item, Mapping)
                    and str(item.get("selector") or "").strip() == proof.source_selector
                ),
                None,
            )
            if source_element is None:
                raise SemanticTargetResolutionError(
                    "当前个人中心阻断弹窗确认按钮缺少可绑定源元素",
                    error_code="PROFILE_CREDENTIAL_CONFIRM_NOT_FOUND",
                    retryable=True,
                )
            return ResolvedInteractionTarget(
                selector=proof.selector,
                expected_rect=_trusted_logical_target_rect(source_element),
                trusted_target_role=(
                    "profile_credential_confirm" if request.session_id.startswith("h5:") else None
                ),
                clear_obstructions=True,
                allow_high_z_obstruction_hide=True,
                h5_popup_dismissal_strategy="safe_outside_then_close",
            )
        matches: list[Mapping[str, Any]] = []
        text_by_capability = {
            "visible_text_acknowledgement": "我知道了",
            "visible_text_close": "关闭",
            "visible_text_skip": "跳过",
            "visible_text_later": "稍后",
        }
        flutter_popup_close_selectors = (
            _flutter_popup_close_selectors(
                elements,
                require_app_open_label=(capability == "flutter_app_open_popup_close"),
                close_above=(capability == "flutter_centered_ad_popup_close_above"),
                scroll_body=(capability == "flutter_centered_scroll_popup_close_below"),
                image_body_top_right=(capability == "flutter_image_popup_top_right_close"),
                account_notice_child=(capability == "flutter_account_notice_top_right_close"),
            )
            if capability
            in {
                "flutter_app_open_popup_close",
                "flutter_centered_ad_popup_close",
                "flutter_centered_ad_popup_close_above",
                "flutter_centered_scroll_popup_close_below",
                "flutter_image_popup_top_right_close",
                "flutter_account_notice_top_right_close",
            }
            else frozenset()
        )
        generic_dialog_close_elements = (
            _dialog_generic_close_snapshot_elements(elements)
            if capability == "dialog_generic_close"
            else []
        )
        homepage_payment_close_element = (
            _homepage_payment_close_snapshot_element(elements)
            if capability == "homepage_payment_close"
            else None
        )
        for raw in elements:
            if not isinstance(raw, Mapping):
                continue
            if capability in {
                "flutter_app_open_popup_close",
                "flutter_centered_ad_popup_close",
                "flutter_centered_ad_popup_close_above",
                "flutter_centered_scroll_popup_close_below",
                "flutter_image_popup_top_right_close",
                "flutter_account_notice_top_right_close",
            }:
                if str(raw.get("selector") or "") in flutter_popup_close_selectors:
                    matches.append(raw)
                continue
            raw_class_tokens = raw.get("class_tokens")
            class_tokens = (
                {str(item).strip().casefold() for item in raw_class_tokens if str(item).strip()}
                if isinstance(raw_class_tokens, list)
                else set()
            )
            raw_ancestor_class_tokens = raw.get("ancestor_class_tokens")
            ancestor_class_tokens = (
                {
                    str(item).strip().casefold()
                    for item in raw_ancestor_class_tokens
                    if str(item).strip()
                }
                if isinstance(raw_ancestor_class_tokens, list)
                else set()
            )
            if capability == "dialog_generic_close":
                if any(raw is item for item in generic_dialog_close_elements):
                    matches.append(raw)
                continue
            if capability in _DIALOG_POPUP_CAPABILITIES:
                van_center_popup_close = bool(
                    capability == "dialog_overlay"
                    and {"van-popup", "van-popup--center"} & ancestor_class_tokens
                    and {"close-img", "close-image", "on-dialog-close"} & class_tokens
                    and _operation_compatible(raw, InteractionOperation.CLICK)
                    and isinstance(raw.get("hit_test"), Mapping)
                    and raw["hit_test"].get("center_hits_target") is True
                )
                account_credential_saved_confirm = bool(
                    capability == "dialog_overlay"
                    and _normalized_text(raw.get("text")) == "我已保存"
                    and {"promotebtn", "btn1"}.issubset(class_tokens)
                    and {
                        "promotebtnbox",
                        "van-popup",
                        "van-popup--center",
                    }.issubset(ancestor_class_tokens)
                    and isinstance(raw.get("hit_test"), Mapping)
                    and raw["hit_test"].get("center_hits_target") is True
                )
                generic_dialog_match = bool(
                    str(raw.get("role") or "").casefold() == "dialog"
                    or "van-dialog" in class_tokens
                    or raw.get("aria_modal") is True
                )
                if (
                    capability == "dialog_overlay"
                    and {
                        "van-popup",
                        "van-popup--center",
                    }
                    & class_tokens
                ):
                    generic_dialog_match = False
                if (
                    generic_dialog_match
                    or van_center_popup_close
                    or account_credential_saved_confirm
                    or (
                        bottom_popup_overlay_selector is not None
                        and str(raw.get("selector") or "") == bottom_popup_overlay_selector
                    )
                    or (
                        capability == "dialog_overlay"
                        and "van-overlay" in class_tokens
                        and "persalepopup" in ancestor_class_tokens
                        and isinstance(raw.get("hit_test"), Mapping)
                        and raw["hit_test"].get("center_hits_target") is True
                    )
                ):
                    matches.append(raw)
                continue
            if capability == "home_suspend_close":
                raw_selector = str(raw.get("selector") or "").casefold()
                if "home-suspend" in class_tokens or (
                    "btn-close" in class_tokens and "home-suspend" in raw_selector
                ):
                    matches.append(raw)
                continue
            if capability == "overlay_ad_close":
                if _overlay_ad_close_snapshot_element(raw):
                    matches.append(raw)
                continue
            if capability == "homepage_payment_close":
                if raw is homepage_payment_close_element:
                    matches.append(raw)
                continue
            expected_text = text_by_capability.get(capability)
            if (
                expected_text is not None
                and _normalized_text(raw.get("text")) == _normalized_text(expected_text)
                and _operation_compatible(raw, InteractionOperation.CLICK)
            ):
                matches.append(raw)
        if not matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明可关闭的唯一弹窗",
                error_code="POPUP_DISMISSAL_NOT_FOUND",
                retryable=True,
            )
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中待关闭弹窗不唯一",
                error_code="POPUP_DISMISSAL_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(
            selector=selector,
            clear_obstructions=True,
            allow_high_z_obstruction_hide=True,
            h5_popup_dismissal_strategy="safe_outside_then_close",
            expected_rect=_trusted_logical_target_rect(matches[0]),
            target_trust="trusted",
            trusted_target_texts=("关闭当前弹窗",),
            trusted_target_role="dismiss",
        )

    async def _resolve_h5_probe_popup_dismissal(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从补采来源 Snapshot 重新签发 H5 弹窗关闭能力。"""

        if (
            request.action_type is not InteractionOperation.CLICK
            or _normalized_text(request.semantic_target) != "关闭当前弹窗"
        ):
            raise SemanticTargetResolutionError(
                "H5 补采弹窗关闭请求无效",
                error_code="H5_PROBE_POPUP_DISMISSAL_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        if (
            h5_payment_horizontal_container_selector(
                snapshot,
                source_page_type="video_detail",
            )
            is not None
        ):
            raise SemanticTargetResolutionError(
                "H5 补采来源 Snapshot 包含待采集的权益套餐集合，禁止关闭",
                error_code="H5_PROBE_ENTITLEMENT_COLLECTION_PRESERVED",
                retryable=False,
            )
        if not assess_h5_ui_obstruction(snapshot).blocking:
            raise SemanticTargetResolutionError(
                "H5 补采来源 Snapshot 未证明当前存在阻断弹窗",
                error_code="H5_PROBE_POPUP_DISMISSAL_NOT_PROVEN",
                retryable=True,
            )
        raw_elements = snapshot.get("elements")
        elements = (
            [item for item in raw_elements if isinstance(item, Mapping)]
            if isinstance(raw_elements, list)
            else []
        )

        homepage_payment_close = _homepage_payment_close_snapshot_element(elements)
        if homepage_payment_close is not None:
            return _h5_probe_popup_target(
                selector=POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR,
                element=homepage_payment_close,
            )
        grid_dialog_close = _grid_dialog_image_close_selector(elements)
        if grid_dialog_close is not None:
            return _h5_probe_popup_target(selector=grid_dialog_close)
        bottom_popup_overlay = _signed_bottom_popup_overlay_element(elements)
        if bottom_popup_overlay is not None:
            return _h5_probe_popup_target(
                selector=".van-overlay:visible",
                element=bottom_popup_overlay,
            )
        close_proof = h5_modal_close(snapshot)
        if close_proof is not None:
            return _h5_probe_popup_target(
                selector=f"{H5_MODAL_CLOSE_SELECTOR}:{close_proof.obstruction_signature}",
            )
        acknowledgement = h5_modal_local_safe_acknowledgement(snapshot)
        if acknowledgement is not None:
            return _h5_probe_popup_target(
                selector=(f"{H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR}:"
                          f"{acknowledgement.obstruction_signature}"),
            )
        profile_confirmation = h5_profile_credential_screenshot_confirm(snapshot)
        if profile_confirmation is not None:
            source_element = next(
                (
                    item
                    for item in elements
                    if str(item.get("selector") or "").strip()
                    == profile_confirmation.source_selector
                ),
                None,
            )
            return _h5_probe_popup_target(
                selector=profile_confirmation.selector,
                element=source_element,
            )

        generic_close = _dialog_generic_close_snapshot_elements(elements)
        if len(generic_close) == 1:
            return _h5_probe_popup_target(
                selector=POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
                element=generic_close[0],
            )
        overlay_ad_close = [item for item in elements if _overlay_ad_close_snapshot_element(item)]
        if len(overlay_ad_close) == 1:
            return _h5_probe_popup_target(
                selector=POPUP_OVERLAY_AD_CLOSE_SELECTOR,
                element=overlay_ad_close[0],
            )
        dialog_overlay = _registered_popup_dismissal_elements(
            elements,
            capability="dialog_overlay",
        )
        if len(dialog_overlay) == 1:
            return _h5_probe_popup_target(
                selector=".van-overlay:visible",
                element=dialog_overlay[0],
            )
        live_revalidation_root = _h5_probe_live_revalidation_root(
            snapshot=snapshot,
            elements=elements,
        )
        if live_revalidation_root is not None:
            # 补采请求可能让页面延迟展示关闭控件。来源快照只授权对唯一整页
            # 阻断弹层进行实时复验；实际控件和点击位置仍由浏览器运行时重新证明。
            return _h5_probe_popup_target(
                selector="text=关闭",
                element=live_revalidation_root,
            )
        raise SemanticTargetResolutionError(
            "H5 补采来源 Snapshot 未唯一证明安全关闭入口",
            error_code="H5_PROBE_POPUP_DISMISSAL_NOT_PROVEN",
            retryable=True,
        )

    async def _resolve_line_loading_login_retry(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """只解析唯一“用户登录失败”Dialog 内的“重试”确认按钮。"""
        signature = _optional_parameter(request.parameters, "obstruction_signature")
        if (
            request.action_type is not InteractionOperation.CLICK
            or request.semantic_target != H5_LINE_LOADING_LOGIN_RETRY_TARGET
            or _optional_parameter(
                request.parameters,
                "line_loading_login_retry_capability",
            )
            != H5_LINE_LOADING_LOGIN_RETRY_CAPABILITY
            or not _valid_obstruction_signature(signature)
        ):
            raise SemanticTargetResolutionError(
                "线路登录重试能力参数无效",
                error_code="LINE_LOADING_LOGIN_RETRY_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        dialogs: list[Mapping[str, Any]] = []
        retry_buttons: list[Mapping[str, Any]] = []
        for raw in elements:
            if not isinstance(raw, Mapping):
                continue
            class_tokens = _class_tokens(raw)
            ancestor_tokens = _ancestor_class_tokens(raw)
            normalized_text = re.sub(r"\s+", "", str(raw.get("text") or ""))
            if (
                (str(raw.get("role") or "").casefold() == "dialog" or "van-dialog" in class_tokens)
                and any(message in normalized_text for message in ("用户登陆失败", "用户登录失败"))
                and _snapshot_element_visible_and_hittable(raw, snapshot=snapshot)
            ):
                dialogs.append(raw)
            if (
                str(raw.get("tag") or "").casefold() == "button"
                and "van-dialog__confirm" in class_tokens
                and "van-dialog" in ancestor_tokens
                and normalized_text == "重试"
                and _operation_compatible(raw, InteractionOperation.CLICK)
                and _snapshot_element_visible_and_hittable(raw, snapshot=snapshot)
            ):
                retry_buttons.append(raw)
        if not dialogs or not retry_buttons:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明线路登录失败重试按钮",
                error_code="LINE_LOADING_LOGIN_RETRY_NOT_FOUND",
                retryable=True,
            )
        if len(dialogs) != 1 or len(retry_buttons) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中线路登录失败重试目标不唯一",
                error_code="LINE_LOADING_LOGIN_RETRY_AMBIGUOUS",
            )
        selector = str(retry_buttons[0].get("selector") or "").strip()
        if not selector:
            raise SemanticTargetResolutionError(
                "线路登录失败重试按钮缺少选择器",
                error_code="LINE_LOADING_LOGIN_RETRY_NOT_FOUND",
                retryable=True,
            )
        return ResolvedInteractionTarget(selector=selector)

    async def _resolve_structural_navigation(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """将已验签的 H5 Tab 能力绑定到平台固定 selector。"""
        if request.action_type is not InteractionOperation.CLICK:
            raise SemanticTargetResolutionError(
                "H5 Tab 导航能力只允许 click",
                error_code="STRUCTURAL_NAVIGATION_OPERATION_NOT_ALLOWED",
            )
        target_mode = request.parameters.get("target_mode")
        navigation_index = request.parameters.get("navigation_index")
        navigation_label = _optional_parameter(request.parameters, "navigation_label")
        navigation_family = (
            h5_bottom_navigation_family(request.parameters.get("navigation_family"))
            if target_mode == "bottom_navigation"
            else None
        )
        if (
            target_mode not in {"bottom_navigation", "page_tab_navigation"}
            or isinstance(navigation_index, bool)
            or not isinstance(navigation_index, int)
            or not 1 <= navigation_index <= 50
            or navigation_label is None
            or (target_mode == "bottom_navigation" and navigation_family is None)
            or (
                target_mode == "page_tab_navigation"
                and request.parameters.get("navigation_family") is not None
            )
        ):
            raise SemanticTargetResolutionError(
                "H5 Tab 导航能力参数无效",
                error_code="STRUCTURAL_NAVIGATION_CAPABILITY_INVALID",
            )
        selector = (
            h5_bottom_navigation_selector(
                family=navigation_family,
                index=navigation_index,
            )
            if target_mode == "bottom_navigation" and navigation_family is not None
            else _PAGE_TAB_NAVIGATION_TARGET_TEMPLATE.format(index=navigation_index)
        )
        expected_semantic_target = (
            selector
            if target_mode == "bottom_navigation"
            else h5_page_tab_target(index=navigation_index)
        )
        if request.semantic_target != expected_semantic_target:
            raise SemanticTargetResolutionError(
                "H5 Tab 导航目标与平台能力不一致",
                error_code="STRUCTURAL_NAVIGATION_TARGET_MISMATCH",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        resolved_snapshot_selector = _select_structural_navigation_item(
            snapshot,
            target_mode=target_mode,
            navigation_index=navigation_index,
            navigation_label=navigation_label,
            navigation_family=navigation_family,
            interaction_pattern_signature=_optional_parameter(
                request.parameters,
                "interaction_pattern_signature",
            ),
            operation=InteractionOperation.CLICK,
        )
        if navigation_family == H5_BOTTOM_NAVIGATION_FAMILY_FLUTTER:
            return ResolvedInteractionTarget(
                selector=h5_flutter_bottom_navigation_runtime_selector(
                    index=navigation_index,
                    label=navigation_label,
                )
            )
        if navigation_family in {
            H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA,
            H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
        }:
            return ResolvedInteractionTarget(selector=resolved_snapshot_selector)
        if target_mode == "page_tab_navigation":
            return ResolvedInteractionTarget(selector=resolved_snapshot_selector)
        if not resolved_snapshot_selector:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未找到结构化导航目标",
                error_code="SEMANTIC_TARGET_NOT_FOUND",
                retryable=True,
            )
        return ResolvedInteractionTarget(selector=selector)

    async def _resolve_probe_display_target(self, request):
        """重验补证对象及展示语义，拒绝漂移、遮挡、表单提交和模型 selector。"""
        from services.api.app.application.h5_probe_targets import (
            PROBE_TARGET_LABELS,
            probe_subject_key,
            select_probe_target,
        )

        goal = request.parameters.get("goal_capability")
        if (
            request.action_type is not InteractionOperation.CLICK
            or goal not in PROBE_TARGET_LABELS
            or request.semantic_target != PROBE_TARGET_LABELS[goal]
        ):
            raise SemanticTargetResolutionError(
                "补证展示动作合同无效", error_code="PROBE_TARGET_INVALID"
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        subject = select_probe_target(
            snapshot, goal=goal, page_type=request.parameters.get("source_page_type"),
            expected_key=request.parameters.get("probe_subject_key"),
        )
        if subject is None or probe_subject_key(subject) != request.parameters.get(
            "probe_subject_key"
        ):
            raise SemanticTargetResolutionError(
                "补证展示对象丢失或漂移", error_code="PROBE_TARGET_STALE"
            )
        return ResolvedInteractionTarget(
            selector=subject["selector"],
            expected_rect=dict(subject["rect"]),
            target_trust="trusted",
            trusted_target_texts=(PROBE_TARGET_LABELS[goal],),
            trusted_target_role=("video" if goal == "native_video_fullscreen"
                                 else "img" if goal == "media_fullscreen_pair"
                                 else "radio" if goal == "payment_plan_selection" else "button"),
        )

    async def _resolve_list_detail_navigation(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从当前 Snapshot 选择签发列表结构中的首个可见卡片叶节点。"""
        selector = _optional_parameter(request.parameters, "list_detail_selector")
        source_page_type = _optional_parameter(request.parameters, "source_page_type")
        expected_page_type = _optional_parameter(request.parameters, "expected_page_type")
        if (
            request.action_type is not InteractionOperation.CLICK
            or _normalized_text(request.semantic_target) != H5_LIST_DETAIL_TARGET
            or selector is None
            or source_page_type is None
            or expected_page_type is None
            or h5_list_detail_expected_page_type(source_page_type) != expected_page_type
            or not h5_list_detail_selector_allowed(
                source_page_type=source_page_type,
                selector=selector,
            )
        ):
            raise SemanticTargetResolutionError(
                "H5 列表详情能力参数无效",
                error_code="LIST_DETAIL_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        visible_matches: list[tuple[float, float, str, Mapping[str, Any], Mapping[str, Any]]] = []
        below_fold_matches: list[
            tuple[float, float, str, Mapping[str, Any], Mapping[str, Any]]
        ] = []
        requires_visible_title = h5_list_detail_selector_requires_visible_title(selector)
        for raw in elements:
            if (
                not isinstance(raw, Mapping)
                or raw.get("disabled") is True
                or not h5_list_detail_snapshot_element_matches(
                    element=raw,
                    source_page_type=source_page_type,
                    selector=selector,
                )
            ):
                continue
            resolved_selector = str(raw.get("selector") or "").strip()
            rect = raw.get("rect")
            if not resolved_selector or not isinstance(rect, Mapping):
                continue
            key = (
                float(rect.get("y") or 0),
                float(rect.get("x") or 0),
                resolved_selector,
                rect,
                raw,
            )
            if _snapshot_element_visible_and_hittable(raw, snapshot=snapshot):
                visible_matches.append(key)
            elif not requires_visible_title and _center_below_viewport(raw, snapshot=snapshot):
                # 首屏下方的卡片命中测试不可信但执行端会先滚动到位；
                # 仅在无可见卡片时兜底，保持「首个可见卡片」优先语义。
                below_fold_matches.append(key)
        matches = visible_matches or below_fold_matches
        if not matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明可见的列表内容卡片",
                error_code="LIST_DETAIL_TARGET_NOT_FOUND",
                retryable=True,
            )
        chosen = min(matches, key=lambda item: (item[0], item[1], item[2]))
        if request.parameters.get("probe_subject_key"):
            from services.api.app.application.h5_probe_targets import probe_subject_key

            if probe_subject_key(chosen[4]) != request.parameters["probe_subject_key"]:
                raise SemanticTargetResolutionError(
                    "封面补证内容对象已漂移", error_code="PROBE_TARGET_STALE",
                )
        verified_card_activation: str | None = None
        if source_page_type == "post_list" and selector == H5_POST_TITLE_SELECTOR:
            verified_card_activation = "post_card"
        elif (
            source_page_type == "video_list"
            and selector == H5_VIDEO_ITEM_SELECTOR
            and _snapshot_element_intercepted_by_inactive_launch_shell(chosen[4])
        ):
            verified_card_activation = "video_item"
        elif (
            source_page_type == "content_list"
            and selector == H5_COMIC_CARD_NAME_SELECTOR
            and _snapshot_element_intercepted_by_inactive_launch_shell(chosen[4])
        ):
            verified_card_activation = "comic_item"
        resolved_selector = (
            selector if verified_card_activation in {"video_item", "comic_item"} else chosen[2]
        )
        return ResolvedInteractionTarget(
            selector=resolved_selector,
            # 位置型 selector 在活页可能同时命中容器与叶节点；携带解析时
            # 的元素矩形供执行端在多命中里选几何最接近的目标。
            expected_rect={
                key: float(chosen[3].get(key) or 0.0) for key in ("x", "y", "width", "height")
            },
            verified_card_activation=verified_card_activation,
            # 这里验证的是平台签发的“列表卡片进入详情”能力，不是卡片文案本身。
            # 卡片标题可能包含购买、授权等业务词，但点击副作用仍只是站内导航；
            # 用固定能力语义做风险复核，避免把内容标题误判成支付或授权提交。
            target_trust="trusted",
            trusted_target_texts=(H5_LIST_DETAIL_TARGET,),
            trusted_target_role="navigation",
        )

    async def _resolve_profile_settings_navigation(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从当前 Snapshot 唯一解析平台签发的“我的”页设置图标。"""
        capability = _optional_parameter(request.parameters, "settings_entry_capability")
        expected_capability = h5_profile_settings_capability_for_target(request.semantic_target)
        if (
            request.action_type is not InteractionOperation.CLICK
            or expected_capability is None
            or capability != expected_capability
            or _optional_parameter(request.parameters, "source_page_type")
            != H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE
            or _optional_parameter(request.parameters, "expected_page_type")
            != H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE
        ):
            raise SemanticTargetResolutionError(
                "H5 我的页设置入口能力参数无效",
                error_code="PROFILE_SETTINGS_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        matches = [
            str(raw.get("selector") or "").strip()
            for raw in elements
            if isinstance(raw, Mapping)
            and (
                capability
                in {
                    H5_PROFILE_CLASS_SETTINGS_CAPABILITY,
                    H5_PROFILE_OPTIONS_SETTINGS_CAPABILITY,
                    H5_PROFILE_FLUTTER_SETTINGS_CAPABILITY,
                    H5_PROFILE_HEADER_SETTINGS_CAPABILITY,
                    H5_PROFILE_TOP_RIGHT_SETTINGS_CAPABILITY,
                    H5_PROFILE_ACTIONS_SETTINGS_CAPABILITY,
                }
                or _operation_compatible(raw, InteractionOperation.CLICK)
            )
            and h5_profile_settings_snapshot_element_matches(
                raw,
                capability=capability,
            )
            and str(raw.get("selector") or "").strip()
        ]
        unique_matches = sorted(set(matches))
        if not unique_matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明可交互的我的页设置入口",
                error_code="PROFILE_SETTINGS_TARGET_NOT_FOUND",
                retryable=True,
            )
        if len(unique_matches) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中我的页设置入口不唯一",
                error_code="PROFILE_SETTINGS_TARGET_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(selector=unique_matches[0])

    async def _resolve_profile_payment_navigation(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从当前 Snapshot 唯一解析平台签发的会员入口。"""
        capability = _optional_parameter(
            request.parameters,
            "payment_entry_capability",
        )
        if (
            request.action_type is not InteractionOperation.CLICK
            or request.semantic_target != H5_PROFILE_PAYMENT_TARGET
            or capability is None
            or _optional_parameter(request.parameters, "source_page_type")
            != H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE
            or _optional_parameter(request.parameters, "expected_page_type")
            != H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE
        ):
            raise SemanticTargetResolutionError(
                "H5 我的页会员支付入口能力参数无效",
                error_code="PROFILE_PAYMENT_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        matches = sorted(
            [
                selector
                for raw in elements
                if isinstance(raw, Mapping)
                and (
                    selector := h5_profile_payment_snapshot_selector(
                        raw,
                        capability=capability,
                    )
                )
            ]
        )
        if not matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明可命中的会员支付入口",
                error_code="PROFILE_PAYMENT_TARGET_NOT_FOUND",
                retryable=True,
            )
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中会员支付入口不唯一",
                error_code="PROFILE_PAYMENT_TARGET_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(selector=matches[0])

    async def _resolve_payment_horizontal_collection(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从来源 Snapshot 重验并解析唯一会员套餐横向容器。"""
        if (
            request.action_type is not InteractionOperation.SCROLL
            or request.semantic_target != H5_PAYMENT_HORIZONTAL_TARGET
            or request.parameters.get("horizontal_scroll_max_segments")
            != H5_PAYMENT_HORIZONTAL_MAX_SEGMENTS
            or request.parameters.get("split_scroll_screenshots") is not True
            or _optional_parameter(request.parameters, "source_page_type")
            not in H5_PAYMENT_HORIZONTAL_SOURCE_PAGE_TYPES
        ):
            raise SemanticTargetResolutionError(
                "H5 会员横向集合能力参数无效",
                error_code="PAYMENT_HORIZONTAL_COLLECTION_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        selector = h5_payment_horizontal_container_selector(
            snapshot,
            source_page_type=_optional_parameter(
                request.parameters,
                "source_page_type",
            ),
        )
        if selector is None:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未唯一证明会员套餐横向容器",
                error_code="PAYMENT_HORIZONTAL_COLLECTION_NOT_FOUND",
                retryable=True,
            )
        return ResolvedInteractionTarget(selector=selector)

    async def _resolve_settings_page_back(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从可信 Snapshot 唯一解析 Flutter 设置页左上返回控件。"""
        if (
            request.action_type is not InteractionOperation.BACK
            or _normalized_text(request.semantic_target) != "browser_history"
            or _optional_parameter(request.parameters, "settings_page_back_capability")
            != H5_SETTINGS_PAGE_BACK_CAPABILITY
            or _optional_parameter(request.parameters, "source_page_type")
            != H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE
        ):
            raise SemanticTargetResolutionError(
                "Flutter 设置页返回能力参数无效",
                error_code="SETTINGS_PAGE_BACK_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        matches = sorted(
            {
                str(raw.get("selector") or "").strip()
                for raw in elements
                if isinstance(raw, Mapping)
                and h5_settings_page_back_snapshot_element_matches(
                    raw,
                    snapshot=snapshot,
                )
                and str(raw.get("selector") or "").strip()
            }
        )
        if not matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明 Flutter 设置页左上返回控件",
                error_code="SETTINGS_PAGE_BACK_TARGET_NOT_FOUND",
                retryable=True,
            )
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中 Flutter 设置页返回控件不唯一",
                error_code="SETTINGS_PAGE_BACK_TARGET_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(selector=matches[0])

    async def _resolve_profile_favorites_page_back(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从可信 Snapshot 唯一解析“我的收藏”页左上返回控件。"""
        if (
            request.action_type is not InteractionOperation.BACK
            or _normalized_text(request.semantic_target) != "browser_history"
            or _optional_parameter(
                request.parameters,
                "profile_favorites_page_back_capability",
            )
            != H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY
            or not h5_profile_favorites_page_back_source_type_allowed(
                _optional_parameter(request.parameters, "source_page_type")
            )
            or _optional_parameter(request.parameters, "expected_page_type")
            != H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE
        ):
            raise SemanticTargetResolutionError(
                "Flutter 我的收藏页返回能力参数无效",
                error_code="PROFILE_FAVORITES_PAGE_BACK_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        matches = sorted(
            {
                str(raw.get("selector") or "").strip()
                for raw in elements
                if isinstance(raw, Mapping)
                and h5_profile_favorites_page_back_snapshot_element_matches(
                    raw,
                    snapshot=snapshot,
                )
                and str(raw.get("selector") or "").strip()
            }
        )
        if not matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明 Flutter 我的收藏页左上返回控件",
                error_code="PROFILE_FAVORITES_PAGE_BACK_TARGET_NOT_FOUND",
                retryable=True,
            )
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中 Flutter 我的收藏页返回控件不唯一",
                error_code="PROFILE_FAVORITES_PAGE_BACK_TARGET_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(selector=matches[0])

    async def _resolve_profile_activity_navigation(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从当前 Snapshot 唯一解析平台签发的个人中心签到入口。"""
        expected_capability = h5_profile_activity_capability_for_target(request.semantic_target)
        if (
            request.action_type is not InteractionOperation.CLICK
            or expected_capability is None
            or _optional_parameter(request.parameters, "activity_entry_capability")
            != expected_capability
            or _optional_parameter(request.parameters, "source_page_type")
            != H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE
            or _optional_parameter(request.parameters, "expected_page_type")
            != H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE
        ):
            raise SemanticTargetResolutionError(
                "H5 我的页签到入口能力参数无效",
                error_code="PROFILE_ACTIVITY_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        raw_elements = snapshot.get("elements")
        elements = raw_elements if isinstance(raw_elements, list) else []
        matches = sorted(
            {
                str(raw.get("selector") or "").strip()
                for raw in elements
                if isinstance(raw, Mapping)
                and h5_profile_activity_snapshot_element_matches(
                    raw,
                    capability=expected_capability,
                )
                and str(raw.get("selector") or "").strip()
            }
        )
        if not matches:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明可命中的个人中心签到入口",
                error_code="PROFILE_ACTIVITY_TARGET_NOT_FOUND",
                retryable=True,
            )
        if len(matches) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中个人中心签到入口不唯一",
                error_code="PROFILE_ACTIVITY_TARGET_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(selector=matches[0])

    async def _resolve_safety_interstitial_bypass(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """把已验签 H5 安全中间页旁路动作绑定到唯一 checkbox。"""
        if (
            request.action_type is not InteractionOperation.CLICK
            or _normalized_text(request.semantic_target)
            != _normalized_text(H5_SAFETY_INTERSTITIAL_TARGET)
            or _optional_parameter(request.parameters, "safety_interstitial_capability")
            != H5_SAFETY_INTERSTITIAL_CAPABILITY
        ):
            raise SemanticTargetResolutionError(
                "H5 安全中间页旁路能力参数无效",
                error_code="SAFETY_INTERSTITIAL_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        if not h5_snapshot_is_safety_interstitial(snapshot):
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明 H5 安全中间页可见",
                error_code="SAFETY_INTERSTITIAL_NOT_FOUND",
                retryable=True,
            )
        selectors = h5_snapshot_safety_checkbox_selectors(snapshot)
        if not selectors:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明安全中间页 checkbox 可交互",
                error_code="SAFETY_INTERSTITIAL_CHECKBOX_NOT_FOUND",
                retryable=True,
            )
        if len(selectors) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中安全中间页 checkbox 不唯一",
                error_code="SAFETY_INTERSTITIAL_CHECKBOX_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(selector=H5_SAFETY_INTERSTITIAL_SELECTOR)

    async def _resolve_age_verification_confirm(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        """从封存 Snapshot 唯一解析完整年龄门禁中的成年确认控件。"""
        if (
            request.action_type is not InteractionOperation.CLICK
            or _normalized_text(request.semantic_target)
            != _normalized_text(H5_AGE_VERIFICATION_TARGET)
            or _optional_parameter(request.parameters, "age_verification_capability")
            != H5_AGE_VERIFICATION_CAPABILITY
        ):
            raise SemanticTargetResolutionError(
                "H5 年龄门禁确认能力参数无效",
                error_code="AGE_VERIFICATION_CAPABILITY_INVALID",
            )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
        )
        selectors = h5_snapshot_age_verification_confirm_selectors(snapshot)
        if not selectors:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未证明完整年龄门禁中的成年确认控件",
                error_code="AGE_VERIFICATION_TARGET_NOT_FOUND",
                retryable=True,
            )
        if len(selectors) != 1:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 中成年确认控件不唯一",
                error_code="AGE_VERIFICATION_TARGET_AMBIGUOUS",
            )
        return ResolvedInteractionTarget(selector=selectors[0])

    async def _resolve_profile_payment_visual_target(
        self,
        request: SemanticTargetResolutionRequest,
    ) -> ResolvedInteractionTarget:
        if request.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            raise SemanticTargetResolutionError(
                "视觉坐标入口只允许 click 或 tap",
                error_code="VISUAL_OPERATION_NOT_ALLOWED",
            )
        semantic_target = _normalized_text(request.semantic_target)
        visual_entry_kind = _optional_parameter(
            request.parameters,
            "visual_entry_kind",
        )
        contract = h5_profile_visual_entry_contract(
            semantic_target,
            entry_kind=visual_entry_kind,
        )
        if contract is None:
            raise SemanticTargetResolutionError(
                "视觉坐标入口不在个人中心白名单",
                error_code="VISUAL_TARGET_NOT_ALLOWED",
            )
        visual_capture_id = _optional_parameter(
            request.parameters,
            "visual_source_capture_id",
        )
        if visual_capture_id is None:
            raise SemanticTargetResolutionError(
                "视觉坐标缺少来源 Capture",
                error_code="VISUAL_SOURCE_CAPTURE_INVALID",
            )
        visual_source_url = _optional_parameter(
            request.parameters,
            "visual_source_url",
        )
        if visual_source_url is None:
            raise SemanticTargetResolutionError(
                "视觉坐标缺少来源 URL",
                error_code="VISUAL_SOURCE_URL_INVALID",
            )
        normalized_source_url = _normalized_visual_source_url(visual_source_url)
        visual_artifact_id = _optional_parameter(
            request.parameters,
            "visual_source_artifact_id",
        )
        if visual_artifact_id is None:
            raise SemanticTargetResolutionError(
                "视觉坐标缺少来源截图证据",
                error_code="VISUAL_SOURCE_ARTIFACT_INVALID",
            )
        if request.parameters.get("visual_source_frame_index") is not None:
            raise SemanticTargetResolutionError(
                "视觉坐标入口不接受时序或滚动截图帧",
                error_code="VISUAL_SOURCE_ARTIFACT_INVALID",
            )
        screenshot_index = request.parameters.get("screenshot_index")
        if (
            isinstance(screenshot_index, bool)
            or not isinstance(screenshot_index, int)
            or screenshot_index < 1
        ):
            raise _visual_bounds_error()
        left_ratio = _visual_ratio_parameter(request.parameters.get("left_ratio"))
        top_ratio = _visual_ratio_parameter(request.parameters.get("top_ratio"))
        right_ratio = _visual_ratio_parameter(request.parameters.get("right_ratio"))
        bottom_ratio = _visual_ratio_parameter(request.parameters.get("bottom_ratio"))
        confidence = _visual_ratio_parameter(request.parameters.get("visual_confidence"))
        if None in {left_ratio, top_ratio, right_ratio, bottom_ratio, confidence}:
            raise _visual_bounds_error()
        assert left_ratio is not None
        assert top_ratio is not None
        assert right_ratio is not None
        assert bottom_ratio is not None
        assert confidence is not None
        if left_ratio >= right_ratio or top_ratio >= bottom_ratio:
            raise _visual_bounds_error()
        if confidence < contract.minimum_confidence:
            raise _visual_bounds_error()
        if (right_ratio - left_ratio) * (bottom_ratio - top_ratio) > contract.maximum_area_ratio:
            raise _visual_bounds_error()
        expected_visual_checksum = _visual_sha256_parameter(
            request.parameters.get("visual_source_sha256")
        )
        source_visual_checksum = await self._snapshot_reader.read_visual_evidence_checksum(
            task_id=request.task_id,
            page_capture_id=visual_capture_id,
            artifact_id=visual_artifact_id,
            frame_index=None,
        )
        if source_visual_checksum.casefold() != expected_visual_checksum:
            raise SemanticTargetResolutionError(
                "视觉坐标候选与来源截图证据不一致",
                error_code="VISUAL_SOURCE_ARTIFACT_MISMATCH",
            )
        source_region_checksum = _visual_sha256_parameter(
            await self._snapshot_reader.read_visual_evidence_region_checksum(
                task_id=request.task_id,
                page_capture_id=visual_capture_id,
                artifact_id=visual_artifact_id,
                left_ratio=left_ratio,
                top_ratio=top_ratio,
                right_ratio=right_ratio,
                bottom_ratio=bottom_ratio,
            )
        )
        source_snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=request.task_id,
            page_capture_id=visual_capture_id,
        )
        source_snapshot_url = _normalized_visual_source_url(source_snapshot.get("url"))
        if source_snapshot_url != normalized_source_url:
            raise SemanticTargetResolutionError(
                "视觉坐标来源 UI Snapshot 与来源 URL 不一致",
                error_code="VISUAL_SOURCE_URL_MISMATCH",
            )
        current_snapshot = source_snapshot
        if visual_capture_id != request.source_capture_id:
            current_snapshot = await self._snapshot_reader.read_ui_snapshot(
                task_id=request.task_id,
                page_capture_id=request.source_capture_id,
            )
        current_snapshot_url = _normalized_visual_source_url(current_snapshot.get("url"))
        if current_snapshot_url != normalized_source_url:
            raise SemanticTargetResolutionError(
                "动作前 UI Snapshot 与视觉坐标来源 URL 不一致",
                error_code="VISUAL_SOURCE_URL_MISMATCH",
            )
        if _visual_snapshot_state(current_snapshot) != _visual_snapshot_state(source_snapshot):
            raise SemanticTargetResolutionError(
                "动作前页面状态已偏离视觉坐标来源 Capture",
                error_code="VISUAL_SOURCE_STATE_STALE",
                retryable=True,
            )
        if visual_capture_id != request.source_capture_id:
            current_region_checksum = _visual_sha256_parameter(
                await self._snapshot_reader.read_visual_evidence_region_checksum(
                    task_id=request.task_id,
                    page_capture_id=request.source_capture_id,
                    artifact_id=None,
                    left_ratio=left_ratio,
                    top_ratio=top_ratio,
                    right_ratio=right_ratio,
                    bottom_ratio=bottom_ratio,
                )
            )
            if current_region_checksum != source_region_checksum:
                raise SemanticTargetResolutionError(
                    "动作前目标区域已偏离视觉坐标来源帧",
                    error_code="VISUAL_SOURCE_STATE_STALE",
                    retryable=True,
                )
        return ResolvedInteractionTarget(
            click_x_ratio=(left_ratio + right_ratio) / 2,
            click_y_ratio=(top_ratio + bottom_ratio) / 2,
            left_ratio=left_ratio,
            top_ratio=top_ratio,
            right_ratio=right_ratio,
            bottom_ratio=bottom_ratio,
            visual_confidence=confidence,
            visual_source_url=visual_source_url,
            visual_entry_kind=contract.entry_kind,
            visual_expected_text=semantic_target,
            screenshot_index=screenshot_index,
            target_trust="visual_coordinate",
            trusted_target_texts=(semantic_target, contract.entry_kind),
        )


def _visual_navigation_schema_and_signature(
    value: object,
) -> dict[str, object]:
    """先验证封闭字段集与协议，再验证原始 canonical 签名。"""
    if not isinstance(value, Mapping):
        raise SemanticTargetResolutionError(
            "视觉导航能力合同无效",
            error_code="VISUAL_TARGET_NOT_ALLOWLISTED",
        )
    capability = dict(value)
    if (
        set(capability) != _VISUAL_NAVIGATION_CAPABILITY_FIELDS
        or capability.get("protocol_version") != VISUAL_NAVIGATION_CAPABILITY_PROTOCOL_VERSION
    ):
        raise SemanticTargetResolutionError(
            "视觉导航能力协议或字段无效",
            error_code="VISUAL_TARGET_NOT_ALLOWLISTED",
        )
    signature = capability.get("target_signature")
    if (
        not isinstance(signature, str)
        or _VISUAL_NAVIGATION_DIGEST_PATTERN.fullmatch(signature) is None
    ):
        raise SemanticTargetResolutionError(
            "视觉导航能力签名无效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
        )
    try:
        expected_signature = stable_h5_surface_hash(
            _canonical_visual_navigation_signed_payload(capability)
        )
    except (TypeError, ValueError) as exc:
        raise SemanticTargetResolutionError(
            "视觉导航能力 canonical 载荷无效",
            error_code="VISUAL_TARGET_NOT_ALLOWLISTED",
        ) from exc
    if not hmac.compare_digest(signature, expected_signature):
        raise SemanticTargetResolutionError(
            "视觉导航能力签名无效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
        )
    return capability


def _canonical_visual_navigation_signed_payload(
    capability: Mapping[str, object],
) -> dict[str, object]:
    """仅规范化签名表示，不提前运行来源、渲染或白名单策略。"""
    payload = {key: capability[key] for key in sorted(_VISUAL_NAVIGATION_SIGNED_FIELDS)}
    for field_name in ("source_page_type", "expected_page_type"):
        value = payload[field_name]
        if isinstance(value, str):
            payload[field_name] = value.casefold()
    for field_name in (
        "source_page_type_confidence",
        "visual_target_confidence",
    ):
        value = payload[field_name]
        if (
            not isinstance(value, bool)
            and isinstance(value, int | float)
            and math.isfinite(float(value))
        ):
            payload[field_name] = round(float(value), 4)
    raw_rect = payload["normalized_rect"]
    if isinstance(raw_rect, Mapping) and set(raw_rect) == {
        "x",
        "y",
        "width",
        "height",
    }:
        rect: dict[str, object] = {}
        for key in ("x", "y", "width", "height"):
            value = raw_rect[key]
            rect[key] = (
                round(float(value), 6)
                if not isinstance(value, bool)
                and isinstance(value, int | float)
                and math.isfinite(float(value))
                else value
            )
        payload["normalized_rect"] = rect
    risk_flags = payload["risk_flags"]
    if isinstance(risk_flags, list | tuple):
        payload["risk_flags"] = [
            item.casefold() if isinstance(item, str) else item for item in risk_flags
        ]
    return payload


def _validate_visual_navigation_request_source(
    *,
    request: SemanticTargetResolutionRequest,
    capability: Mapping[str, object],
) -> None:
    """使用独立 Runtime 请求复核 task 与来源 Capture，禁止载荷自证。"""
    if (
        capability.get("source_task_id") != request.task_id
        or capability.get("source_capture_id") != request.source_capture_id
    ):
        raise SemanticTargetResolutionError(
            "视觉导航来源任务或 Capture 已失效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )


def _visual_navigation_logical_surface(
    snapshot: Mapping[str, Any],
) -> Mapping[str, object]:
    surface = snapshot.get("logical_surface")
    if (
        not isinstance(surface, Mapping)
        or surface.get("protocol_version") != LOGICAL_SURFACE_PROTOCOL_VERSION
    ):
        raise SemanticTargetResolutionError(
            "视觉导航来源缺少可信逻辑页面",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )
    return surface


def _validate_visual_navigation_logical_state(
    *,
    snapshot: Mapping[str, Any],
    capability: Mapping[str, object],
) -> None:
    surface = _visual_navigation_logical_surface(snapshot)
    identity = capability.get("source_identity_fingerprint")
    state = capability.get("source_state_fingerprint")
    if (
        not isinstance(identity, str)
        or _VISUAL_NAVIGATION_DIGEST_PATTERN.fullmatch(identity) is None
        or not isinstance(state, str)
        or _VISUAL_NAVIGATION_DIGEST_PATTERN.fullmatch(state) is None
        or surface.get("identity_fingerprint") != identity
        or surface.get("state_fingerprint") != state
    ):
        raise SemanticTargetResolutionError(
            "视觉导航来源逻辑页面状态已失效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )


def _validate_visual_navigation_render_gate(
    *,
    snapshot: Mapping[str, Any],
    capability: Mapping[str, object],
) -> None:
    surface = _visual_navigation_logical_surface(snapshot)
    render_profile = surface.get("render_profile")
    capability_render_profile = capability.get("render_profile")
    semantics_coverage = surface.get("semantics_coverage")
    if (
        surface.get("shadow") is not False
        or not isinstance(render_profile, Mapping)
        or not isinstance(capability_render_profile, Mapping)
        or dict(render_profile) != dict(capability_render_profile)
        or render_profile.get("kind") not in {"flutter_web", "hybrid"}
        or render_profile.get("status") != "confirmed"
        or not isinstance(semantics_coverage, Mapping)
        or semantics_coverage.get("level") != "semantics_sparse"
        or capability.get("semantics_coverage_level") != "semantics_sparse"
    ):
        raise SemanticTargetResolutionError(
            "当前渲染与 Semantics 状态不允许视觉导航",
            error_code="VISUAL_TARGET_NOT_ALLOWLISTED",
        )


async def _read_visual_navigation_source_png(
    *,
    snapshot_reader: TrustedUiSnapshotReader,
    request: SemanticTargetResolutionRequest,
    capability: Mapping[str, object],
) -> bytes:
    artifact_id = capability.get("source_artifact_id")
    expected_sha256 = capability.get("source_artifact_sha256")
    if (
        not isinstance(artifact_id, str)
        or not artifact_id.strip()
        or artifact_id != artifact_id.strip()
        or not isinstance(expected_sha256, str)
        or _VISUAL_NAVIGATION_DIGEST_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise SemanticTargetResolutionError(
            "视觉导航来源 Artifact 合同无效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
        )
    try:
        source_png = await snapshot_reader.read_visual_evidence_image(
            task_id=request.task_id,
            page_capture_id=request.source_capture_id,
            artifact_id=artifact_id,
            expected_sha256=expected_sha256,
        )
    except SemanticTargetResolutionError as exc:
        raise SemanticTargetResolutionError(
            "视觉导航来源 Artifact 无法通过可信读取",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        ) from exc
    if not isinstance(source_png, bytes) or not source_png:
        raise SemanticTargetResolutionError(
            "视觉导航来源 Artifact 无法通过可信读取",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )
    actual_sha256 = f"sha256:{hashlib.sha256(source_png).hexdigest()}"
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise SemanticTargetResolutionError(
            "视觉导航来源 Artifact 已失效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )
    return source_png


def _decode_visual_navigation_source_png(
    payload: bytes,
) -> tuple[bytes, int, int, int]:
    try:
        image = cv2.imdecode(
            np.frombuffer(payload, dtype=np.uint8),
            cv2.IMREAD_UNCHANGED,
        )
    except (cv2.error, ValueError) as exc:
        raise SemanticTargetResolutionError(
            "视觉导航来源 PNG 无法解码",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        ) from exc
    if image is None or image.dtype != np.uint8:
        raise SemanticTargetResolutionError(
            "视觉导航来源 PNG 无法解码",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    try:
        if image.ndim == 2:
            normalized = cv2.cvtColor(image, cv2.COLOR_GRAY2RGBA)
        elif image.ndim == 3 and image.shape[2] == 1:
            normalized = cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2RGBA)
        elif image.ndim == 3 and image.shape[2] == 2:
            gray = image[:, :, 0]
            alpha = image[:, :, 1]
            normalized = np.stack((gray, gray, gray, alpha), axis=2)
        elif image.ndim == 3 and image.shape[2] == 3:
            normalized = cv2.cvtColor(image, cv2.COLOR_BGR2RGBA)
        elif image.ndim == 3 and image.shape[2] == 4:
            normalized = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)
        else:
            raise ValueError("不受支持的 PNG 通道")
    except (cv2.error, ValueError) as exc:
        raise SemanticTargetResolutionError(
            "视觉导航来源 PNG 通道无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        ) from exc
    normalized = np.ascontiguousarray(normalized, dtype=np.uint8)
    image_height, image_width, channel_count = normalized.shape
    if image_width <= 0 or image_height <= 0 or channel_count != 4:
        raise SemanticTargetResolutionError(
            "视觉导航来源 PNG 尺寸无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    return normalized.tobytes(), image_width, image_height, channel_count


def _validate_visual_navigation_viewport_and_transform(
    *,
    snapshot: Mapping[str, Any],
    capability: Mapping[str, object],
    source_png: bytes,
) -> tuple[bytes, int, int, int]:
    decoded = _decode_visual_navigation_source_png(source_png)
    _pixel_bytes, image_width, image_height, _channel_count = decoded
    visual_capture = snapshot.get("visual_navigation_capture")
    if not isinstance(visual_capture, Mapping):
        raise SemanticTargetResolutionError(
            "视觉导航 Capture 坐标合同缺失",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    if visual_capture.get("screenshot_sha256") != capability.get("source_artifact_sha256"):
        raise SemanticTargetResolutionError(
            "视觉导航 Capture 截图已失效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )
    if (
        visual_capture.get("protocol_version") != "visual_navigation_capture.v1"
        or visual_capture.get("purpose") != "visual_navigation_viewport"
        or visual_capture.get("coordinate_space") != "visual_viewport_css_pixels"
        or visual_capture.get("image_width") != image_width
        or visual_capture.get("image_height") != image_height
    ):
        raise SemanticTargetResolutionError(
            "视觉导航 Capture 坐标合同无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    source_image_size = capability.get("source_image_size")
    if (
        not isinstance(source_image_size, Mapping)
        or set(source_image_size) != {"width", "height"}
        or source_image_size.get("width") != image_width
        or source_image_size.get("height") != image_height
    ):
        raise SemanticTargetResolutionError(
            "视觉导航来源图像尺寸无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    viewport = snapshot.get("viewport")
    viewport_metrics = capability.get("viewport_metrics")
    if (
        not isinstance(viewport, Mapping)
        or not isinstance(viewport_metrics, Mapping)
        or set(viewport_metrics) != set(_VISUAL_NAVIGATION_VIEWPORT_FIELDS)
        or any(
            not _same_finite_visual_number(
                viewport_metrics.get(capability_key),
                viewport.get(snapshot_key),
            )
            for capability_key, snapshot_key in _VISUAL_NAVIGATION_VIEWPORT_FIELDS.items()
        )
        or not _positive_visual_number(viewport_metrics.get("width"))
        or not _positive_visual_number(viewport_metrics.get("height"))
        or not _positive_visual_number(viewport_metrics.get("scale"))
    ):
        raise SemanticTargetResolutionError(
            "视觉导航视口合同无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    transform = capability.get("model_image_transform")
    if not isinstance(transform, Mapping) or dict(transform) != (
        _VISUAL_NAVIGATION_IDENTITY_TRANSFORM
    ):
        raise SemanticTargetResolutionError(
            "视觉导航模型图像变换无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    return decoded


def _same_finite_visual_number(left: object, right: object) -> bool:
    if (
        isinstance(left, bool)
        or isinstance(right, bool)
        or not isinstance(left, int | float)
        or not isinstance(right, int | float)
    ):
        return False
    return (
        math.isfinite(float(left)) and math.isfinite(float(right)) and float(left) == float(right)
    )


def _positive_visual_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _same_visual_confidence(left: object, right: object) -> bool:
    if (
        isinstance(left, bool)
        or isinstance(right, bool)
        or not isinstance(left, int | float)
        or not isinstance(right, int | float)
        or not math.isfinite(float(left))
        or not math.isfinite(float(right))
    ):
        return False
    return round(float(left), 4) == round(float(right), 4)


def _validate_visual_navigation_target_policy(
    *,
    snapshot: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
    capability: Mapping[str, object],
) -> None:
    surface = _visual_navigation_logical_surface(snapshot)
    binding = surface.get("page_type_binding")
    source_page_type = capability.get("source_page_type")
    source_identity = capability.get("source_identity_fingerprint")
    if (
        not isinstance(binding, Mapping)
        or binding.get("coverage_allowed") is not True
        or binding.get("page_type") != source_page_type
        or binding.get("identity_fingerprint") != source_identity
        or capability.get("source_identity_bound") is not True
    ):
        raise SemanticTargetResolutionError(
            "视觉导航来源页型或身份绑定不在白名单",
            error_code="VISUAL_TARGET_NOT_ALLOWLISTED",
        )
    binding_confidence = binding.get("page_type_confidence")
    capability_confidence = capability.get("source_page_type_confidence")
    if not _same_visual_confidence(binding_confidence, capability_confidence):
        raise SemanticTargetResolutionError(
            "视觉导航来源页型状态已失效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )
    expected_page_type = capability.get("expected_page_type")
    if (
        request.action_type is not InteractionOperation.CLICK
        or not isinstance(expected_page_type, str)
        or _normalized_text(request.semantic_target)
        != _normalized_text(f"进入 {expected_page_type} 页面")
    ):
        raise SemanticTargetResolutionError(
            "视觉导航目标或动作不在白名单",
            error_code="VISUAL_TARGET_NOT_ALLOWLISTED",
        )
    try:
        decision = evaluate_visual_navigation_target(
            target_kind=capability.get("target_kind"),
            source_page_type=source_page_type,
            source_page_type_confidence=binding_confidence,
            source_identity_bound=True,
            expected_page_type=expected_page_type,
            visual_target_confidence=capability.get("visual_target_confidence"),
            selected=capability.get("selected"),
            risk_flags=capability.get("risk_flags"),
            normalized_rect=capability.get("normalized_rect"),
            unique=capability.get("unique"),
        )
    except (TypeError, ValueError) as exc:
        raise SemanticTargetResolutionError(
            "视觉导航目标区域合同无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        ) from exc
    if decision.allowed:
        return
    reason_code = decision.reason_code
    if reason_code == "visual_target_confidence_low":
        error_code = "VISUAL_TARGET_CONFIDENCE_LOW"
    elif reason_code == "visual_target_not_unique":
        error_code = "VISUAL_TARGET_NOT_UNIQUE"
    elif reason_code in {
        "visual_target_coordinate_invalid",
        "visual_target_full_page_rect",
        "visual_target_geometry_invalid",
        "visual_primary_navigation_not_bottom_band",
    }:
        error_code = "VISUAL_TARGET_COORDINATE_INVALID"
    else:
        error_code = "VISUAL_TARGET_NOT_ALLOWLISTED"
    raise SemanticTargetResolutionError(
        "视觉导航目标未通过白名单复验",
        error_code=error_code,
    )


def _validate_visual_navigation_region_material(
    *,
    decoded: tuple[bytes, int, int, int],
    capability: VisualNavigationCapability,
) -> None:
    pixel_bytes, image_width, image_height, channel_count = decoded
    try:
        actual_digests = visual_navigation_region_digests(
            pixel_bytes=pixel_bytes,
            image_width=image_width,
            image_height=image_height,
            channel_count=channel_count,
            normalized_rect=capability.normalized_rect,
        )
    except ValueError as exc:
        raise SemanticTargetResolutionError(
            "视觉导航来源区域合同无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        ) from exc
    if not (
        _same_visual_digest(
            actual_digests["local_visual_digest"],
            capability.local_visual_digest,
        )
        and _same_visual_digest(
            actual_digests["surrounding_anchor_digest"],
            capability.surrounding_anchor_digest,
        )
    ):
        raise SemanticTargetResolutionError(
            "视觉导航来源区域已失效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            retryable=True,
        )


def _same_visual_digest(left: str, right: str) -> bool:
    normalized_left = left.removeprefix("sha256:")
    normalized_right = right.removeprefix("sha256:")
    return hmac.compare_digest(normalized_left, normalized_right)


def _validate_logical_source_state(
    *,
    snapshot: Mapping[str, Any],
    contract: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
    resolved: ResolvedInteractionTarget,
) -> str:
    """动作前复核逻辑身份与状态，防止复用旧 Capture 的目标。"""
    if contract.get("protocol_version") != "logical_target.v1":
        raise SemanticTargetResolutionError(
            "逻辑目标协议版本无效",
            error_code="LOGICAL_TARGET_INVALID",
        )
    expected_identity = _logical_target_fingerprint(contract.get("source_identity_fingerprint"))
    expected_state = _logical_target_fingerprint(contract.get("source_state_fingerprint"))
    surface = snapshot.get("logical_surface")
    current_state = (
        _logical_target_fingerprint(surface.get("state_fingerprint"))
        if isinstance(surface, Mapping)
        else None
    )
    if (
        expected_identity is None
        or expected_state is None
        or not isinstance(surface, Mapping)
        or surface.get("protocol_version") != LOGICAL_SURFACE_PROTOCOL_VERSION
        or surface.get("shadow") is not False
        or surface.get("identity_fingerprint") != expected_identity
        or current_state is None
    ):
        raise SemanticTargetResolutionError(
            "动作前逻辑页面状态已偏离候选来源 Capture",
            error_code="LOGICAL_SOURCE_STATE_STALE",
            retryable=True,
        )
    if current_state == expected_state:
        return expected_state
    if _startup_ad_state_drift_revalidated(snapshot=snapshot, request=request):
        return current_state
    if _popup_dismissal_state_drift_revalidated(snapshot=snapshot, request=request):
        return current_state
    if _h5_navigation_state_drift_revalidated(
        snapshot=snapshot,
        request=request,
        resolved=resolved,
    ):
        return current_state
    raise SemanticTargetResolutionError(
        "动作前逻辑页面状态已偏离候选来源 Capture",
        error_code="LOGICAL_SOURCE_STATE_STALE",
        retryable=True,
    )


def _bind_trusted_logical_target(
    *,
    snapshot: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
    resolved: ResolvedInteractionTarget,
    source_state: str,
) -> ResolvedInteractionTarget:
    """将旧解析结果绑定到唯一、可命中的结构目标并生成稳定签名。"""
    if request.action_type is InteractionOperation.NAVIGATE:
        return replace(
            resolved,
            target_source="url",
            target_signature=stable_h5_surface_hash(
                {
                    "source": "url",
                    "destination_url": resolved.destination_url,
                }
            ),
            source_state_fingerprint=source_state,
            target_trust="trusted",
        )
    if resolved.selector is None:
        if request.action_type in {
            InteractionOperation.BACK,
            InteractionOperation.REFRESH,
            InteractionOperation.SCROLL,
        }:
            return replace(
                resolved,
                target_source="platform_action",
                target_signature=stable_h5_surface_hash(
                    {"source": "platform_action", "action": request.action_type.value}
                ),
                source_state_fingerprint=source_state,
                target_trust="trusted",
            )
        raise SemanticTargetResolutionError(
            "逻辑目标未绑定结构控件",
            error_code="LOGICAL_TARGET_NOT_UNIQUE",
            retryable=True,
        )

    elements = _logical_target_elements(
        snapshot=snapshot,
        request=request,
        resolved=resolved,
    )
    if len(elements) != 1:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 未唯一绑定逻辑目标",
            error_code="LOGICAL_TARGET_NOT_UNIQUE",
            retryable=not elements,
        )
    element = elements[0]
    rect = _trusted_logical_target_rect(element)
    hit_test = element.get("hit_test")
    outside_point_authorized = _h5_modal_outside_point_authorized(
        snapshot=snapshot, request=request, element=element,
    )
    page_tab_horizontally_offscreen = bool(
        request.parameters.get("target_mode") == "page_tab_navigation"
        and request.action_type is InteractionOperation.CLICK
        and _center_horizontally_outside_viewport(element, snapshot=snapshot)
    )
    if (
        not (
            _logical_target_operation_compatible(
                element=element,
                request=request,
                resolved=resolved,
                snapshot=snapshot,
            )
            or page_tab_horizontally_offscreen
            or outside_point_authorized
        )
        or not isinstance(hit_test, Mapping)
        or (
            hit_test.get("center_hits_target") is not True
            and not page_tab_horizontally_offscreen
            and not outside_point_authorized
        )
        or rect is None
    ):
        raise SemanticTargetResolutionError(
            "逻辑目标未通过可交互区域命中测试",
            error_code="LOGICAL_TARGET_HIT_TEST_FAILED",
            retryable=True,
        )

    label, label_source = _logical_target_label(element)
    tag = str(element.get("tag") or "").strip().casefold()
    selector = str(element.get("selector") or "").strip()
    is_flutter_semantics = bool(
        tag == "flt-semantics"
        or selector.casefold().startswith("flt-semantics")
        or element.get("semantics_node_id") is not None
    )
    target_source = (
        "flutter_semantics"
        if is_flutter_semantics
        else "hybrid"
        if label_source == "ocr_text"
        else "dom"
    )
    signature = logical_target_snapshot_signature(
        element,
        target_source=target_source,
    )
    identity_signature = logical_target_snapshot_identity_signature(
        element,
        target_source=target_source,
    )
    if signature is None or identity_signature is None:
        raise SemanticTargetResolutionError(
            "逻辑目标缺少可签名的结构信息",
            error_code="LOGICAL_TARGET_INVALID",
            retryable=True,
        )
    return replace(
        resolved,
        expected_rect=resolved.expected_rect or rect,
        target_source=target_source,
        target_signature=signature,
        target_identity_signature=identity_signature,
        source_state_fingerprint=source_state,
        target_trust="trusted",
        trusted_target_texts=(
            resolved.trusted_target_texts or _trusted_target_semantics(element)
        ),
        trusted_target_role=(
            resolved.trusted_target_role
            or _optional_snapshot_text(element.get("role"))
        ),
    )


def _logical_target_elements(
    *,
    snapshot: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
    resolved: ResolvedInteractionTarget,
) -> list[Mapping[str, Any]]:
    raw_elements = snapshot.get("elements")
    elements = (
        [item for item in raw_elements if isinstance(item, Mapping)]
        if isinstance(raw_elements, list)
        else []
    )
    target_mode = request.parameters.get("target_mode")
    if target_mode == "startup_ad_dismissal":
        if resolved.selector == FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR:
            return list(
                _flutter_fullscreen_startup_skip_snapshot_matches(
                    snapshot=snapshot,
                    elements=elements,
                )
            )
        return [
            element
            for element in elements
            if _startup_ad_selector_from_snapshot_element(element) == resolved.selector
        ]
    if target_mode == "popup_dismissal":
        capability = _optional_parameter(
            request.parameters,
            "popup_dismissal_capability",
        )
        if capability == H5_MODAL_CLOSE_CAPABILITY:
            return _rendered_modal_logical_target_elements(snapshot=snapshot, request=request)
        if capability == H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY:
            proof = h5_modal_local_safe_acknowledgement(snapshot)
            if proof is None or proof.obstruction_signature != request.parameters.get(
                "obstruction_signature"
            ):
                return []
            return [element for element in elements if element.get("selector") == proof.selector]
        if capability == H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY:
            proof = h5_profile_credential_screenshot_confirm(snapshot)
            if proof is not None:
                return [
                    element
                    for element in elements
                    if str(element.get("selector") or "").strip() == proof.source_selector
                ]
        registered = _registered_popup_dismissal_elements(
            elements,
            capability=capability,
        )
        if registered:
            if resolved.expected_rect is not None:
                rect_matches = [
                    element
                    for element in registered
                    if _rect_matches(element.get("rect"), resolved.expected_rect)
                ]
                if rect_matches:
                    return rect_matches
            return registered
    if target_mode in {"bottom_navigation", "page_tab_navigation"}:
        navigation_index = request.parameters.get("navigation_index")
        navigation_label = _optional_parameter(request.parameters, "navigation_label")
        if isinstance(navigation_index, int) and navigation_label is not None:
            snapshot_selector = _select_structural_navigation_item(
                snapshot,
                target_mode=str(target_mode),
                navigation_index=navigation_index,
                navigation_label=navigation_label,
                navigation_family=(
                    h5_bottom_navigation_family(request.parameters.get("navigation_family"))
                    if target_mode == "bottom_navigation"
                    else None
                ),
                interaction_pattern_signature=_optional_parameter(
                    request.parameters,
                    "interaction_pattern_signature",
                ),
                operation=request.action_type,
            )
            return [
                element
                for element in elements
                if str(element.get("selector") or "").strip() == snapshot_selector
            ]
    selector_matches = [
        element
        for element in elements
        if str(element.get("selector") or "").strip() == resolved.selector
    ]
    if selector_matches:
        return selector_matches
    if resolved.expected_rect is not None:
        rect_matches = [
            element
            for element in elements
            if _rect_matches(element.get("rect"), resolved.expected_rect)
        ]
        if rect_matches:
            return rect_matches
    semantic_target = _normalized_text(request.semantic_target)
    scored = [
        (_match_score(element, semantic_target), element) for element in elements if semantic_target
    ]
    best_score = max((score for score, _element in scored), default=0)
    return [element for score, element in scored if score == best_score and score > 0]


def _startup_ad_selector_from_snapshot_element(
    element: Mapping[str, Any],
) -> str | None:
    """复用签发阶段的开屏结构合同，禁止逻辑绑定退化为同文案猜测。"""
    structural_selector = h5_startup_dismissal_selector_from_snapshot_element(element)
    if structural_selector is not None:
        return structural_selector
    class_tokens = _class_tokens(element)
    ancestor_tokens = element.get("ancestor_class_tokens")
    normalized_ancestors = (
        {str(item).strip().casefold() for item in ancestor_tokens if str(item).strip()}
        if isinstance(ancestor_tokens, list)
        else set()
    )
    if (
        "count-down" in class_tokens
        and "van-popup" not in normalized_ancestors
        and _normalized_text(element.get("text")) in FLUTTER_STARTUP_DISMISS_TEXTS
    ):
        return GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR
    return None


def _startup_ad_state_drift_revalidated(
    *,
    snapshot: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
) -> bool:
    """倒计时刷新只允许唯一、可命中的开屏结构在动作前使用当前状态重签。"""
    if (
        request.action_type is not InteractionOperation.CLICK
        or request.parameters.get("target_mode") != "startup_ad_dismissal"
    ):
        return False
    raw_elements = snapshot.get("elements")
    elements = (
        [item for item in raw_elements if isinstance(item, Mapping)]
        if isinstance(raw_elements, list)
        else []
    )
    matches = [
        element
        for element in elements
        if _startup_ad_selector_from_snapshot_element(element) is not None
    ]
    if len(matches) != 1:
        return False
    match = matches[0]
    hit_test = match.get("hit_test")
    return bool(
        isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
        and _trusted_logical_target_rect(match) is not None
        and _registered_non_native_target_enabled(match)
    )


def _popup_dismissal_state_drift_revalidated(
    *,
    snapshot: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
) -> bool:
    """弹窗动态内容变化时，只允许唯一受信关闭结构使用当前状态重签。"""
    if (
        request.action_type is not InteractionOperation.CLICK
        or request.parameters.get("target_mode") != "popup_dismissal"
    ):
        return False
    capability = _optional_parameter(
        request.parameters,
        "popup_dismissal_capability",
    )
    signature = _optional_parameter(request.parameters, "obstruction_signature")
    if capability is None or not _valid_obstruction_signature(signature):
        return False
    if (
        request.session_id.startswith("h5:")
        and capability == H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY
    ):
        proof = h5_modal_local_safe_acknowledgement(snapshot)
        return bool(proof is not None and proof.obstruction_signature == signature)
    raw_elements = snapshot.get("elements")
    elements = (
        [item for item in raw_elements if isinstance(item, Mapping)]
        if isinstance(raw_elements, list)
        else []
    )
    matches = (
        _rendered_modal_logical_target_elements(snapshot=snapshot, request=request)
        if capability == H5_MODAL_CLOSE_CAPABILITY
        else _registered_popup_dismissal_elements(elements, capability=capability)
    )
    if len(matches) != 1:
        return False
    match = matches[0]
    return bool(
        (_registered_popup_target_hittable(match) or _h5_modal_outside_point_authorized(
            snapshot=snapshot, request=request, element=match,
        ))
        and _trusted_logical_target_rect(match) is not None
    )


def _h5_modal_outside_point_authorized(
    *, snapshot: Mapping[str, Any], request: SemanticTargetResolutionRequest,
    element: Mapping[str, Any],
) -> bool:
    """遮罩中心可被弹窗遮住；只接受完整普查签发的弹窗外命中点。"""
    if not (
        request.session_id.startswith("h5:")
        and request.action_type is InteractionOperation.CLICK
        and request.parameters.get("target_mode") == "popup_dismissal"
        and request.parameters.get("popup_dismissal_capability") == H5_MODAL_CLOSE_CAPABILITY
    ):
        return False
    proof = h5_modal_close(snapshot)
    return bool(
        proof is not None and proof.outside_position is not None
        and proof.selector == element.get("selector")
        and proof.obstruction_signature == request.parameters.get("obstruction_signature")
    )


def _rendered_modal_logical_target_elements(
    *, snapshot: Mapping[str, Any], request: SemanticTargetResolutionRequest
) -> list[Mapping[str, Any]]:
    """虚拟关闭能力必须映射到同一遮挡证明中的真实按钮，不走文案猜测。"""
    proof = h5_modal_close(snapshot)
    if (
        proof is None
        or proof.obstruction_signature != request.parameters.get("obstruction_signature")
    ):
        return []
    return [
        element
        for element in snapshot.get("elements", [])
        if isinstance(element, Mapping)
        and str(element.get("selector") or "").strip() == proof.selector
    ]


def _h5_navigation_state_drift_revalidated(
    *,
    snapshot: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
    resolved: ResolvedInteractionTarget,
) -> bool:
    """动态 H5 页面只对当前快照中的唯一低风险导航目标重新签名。"""

    frontier_snapshot_id = request.parameters.get("frontier_snapshot_id")
    if not (
        request.action_type is InteractionOperation.CLICK
        and request.parameters.get("target_mode")
        in H5_DYNAMIC_LOGICAL_TARGET_MODES
        and isinstance(frontier_snapshot_id, str)
        and frontier_snapshot_id.startswith("h5-task-frontier:")
    ):
        return False
    matches = _logical_target_elements(
        snapshot=snapshot,
        request=request,
        resolved=resolved,
    )
    if len(matches) != 1:
        return False
    match = matches[0]
    hit_test = match.get("hit_test")
    page_tab_horizontally_offscreen = bool(
        request.parameters.get("target_mode") == "page_tab_navigation"
        and _center_horizontally_outside_viewport(match, snapshot=snapshot)
    )
    return bool(
        (
            _logical_target_operation_compatible(
                element=match,
                request=request,
                resolved=resolved,
                snapshot=snapshot,
            )
            or page_tab_horizontally_offscreen
        )
        and isinstance(hit_test, Mapping)
        and (
            hit_test.get("center_hits_target") is True
            or page_tab_horizontally_offscreen
        )
        and _trusted_logical_target_rect(match) is not None
        and _registered_non_native_target_enabled(match)
    )


def _dialog_generic_close_snapshot_elements(
    elements: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """优先返回 Dialog 的关闭包装层，避免与同框子图标形成双重绑定。"""
    dialog_scope_tokens = {
        "dialog",
        "modal",
        "van-dialog",
        "van-popup",
        "van-popup--center",
        "app-list-pop",
    }
    wrappers = [
        element
        for element in elements
        if _class_tokens(element) & {"close_btn", "closebtn", "close-btn", "btn-close"}
        and _ancestor_class_tokens(element) & dialog_scope_tokens
        and _registered_popup_target_hittable(element)
    ]
    if wrappers:
        return wrappers
    return [
        element
        for element in elements
        if _class_tokens(element) & {"van-icon-close", "van-popup__close-icon"}
        and _ancestor_class_tokens(element) & dialog_scope_tokens
        and _registered_popup_target_hittable(element)
    ]


def _h5_probe_live_revalidation_root(
    *,
    snapshot: Mapping[str, Any],
    elements: list[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """返回唯一整页阻断根节点；它只授权实时复验，不直接授权点击。"""

    viewport = snapshot.get("viewport")
    if not isinstance(viewport, Mapping):
        return None
    try:
        viewport_width = float(viewport.get("width") or 0)
        viewport_height = float(viewport.get("height") or 0)
    except (TypeError, ValueError):
        return None
    viewport_area = viewport_width * viewport_height
    if viewport_area <= 0:
        return None
    modal_tokens = {"overlay", "modal", "dialog", "popup"}
    roots: list[Mapping[str, Any]] = []
    for element in elements:
        rect = _trusted_logical_target_rect(element)
        hit_test = element.get("hit_test")
        tokens = _class_tokens(element)
        role = str(element.get("role") or "").strip().casefold()
        if (
            rect is None
            or rect["width"] * rect["height"] / viewport_area < 0.50
            or _ancestor_class_tokens(element) & modal_tokens
            or not (role == "dialog" or tokens & modal_tokens)
            or not isinstance(hit_test, Mapping)
            or hit_test.get("topmost") is not True
            or hit_test.get("pointer_interception") is not True
            or tokens & {"float", "floating", "suspend"}
        ):
            continue
        roots.append(element)
    return roots[0] if len(roots) == 1 else None


def _overlay_ad_close_snapshot_element(element: Mapping[str, Any]) -> bool:
    """复用遮罩广告签发结构，禁止逻辑绑定阶段退化为任意 close 类名。"""
    class_tokens = _class_tokens(element)
    ancestor_tokens = _ancestor_class_tokens(element)
    from services.api.app.application.h5_popup_close_contract import VANT_CENTER_CLOSE_WRAPPERS

    if (
        str(element.get("tag") or "").strip().casefold() == "a"
        or str(element.get("role") or "").strip().casefold() == "link"
        or any(
            str(element.get(field) or "").strip()
            for field in (
                "href",
                "data_href",
                "data_url",
                "data_uri",
                "data_route",
                "router_to",
            )
        )
        or element.get("navigation_payload_in_subtree") is True
        or element.get("stateful_control_in_subtree") is True
        or element.get("disabled") is True
        or element.get("visible") is False
        or (str(element.get("tag") or "").casefold() == "button"
            and element.get("type") != "button" and element.get("form_associated") is not False)
    ):
        return False
    hit_test = element.get("hit_test")
    center_hit = bool(isinstance(hit_test, Mapping) and hit_test.get("center_hits_target") is True)
    return bool(
        (
            class_tokens == {"popup-close"}
            and {"adspop", "popup-container", "popup-content"}.issubset(ancestor_tokens)
            and center_hit
        )
        or ({"on-popup-close", "close-img"} & class_tokens and "van-overlay" in ancestor_tokens)
        or ({"close", "btn-close"} & class_tokens and "ad-dialog" in ancestor_tokens)
        or (
            "close-btn" in class_tokens
            and {
                "recommend-card",
                "recommend-wrapper",
                "recommend-layer",
            }.issubset(ancestor_tokens)
            and element.get("web_recommend_ad_close_direct_chain") is True
            and element.get("navigation_payload_in_subtree") is False
            and center_hit
        )
        or (
            class_tokens == {"close"}
            and {"ninegridpopup", "van-overlay", "wrapper"}.issubset(ancestor_tokens)
            and center_hit
        )
        or (
            class_tokens == {"close"}
            and {"announcementpopup", "van-overlay", "imgwrapper"}.issubset(ancestor_tokens)
            and center_hit
        )
        or (
            class_tokens == {"closebtn"}
            and {"textpopcontent", "van-popup", "van-popup--center"}.issubset(ancestor_tokens)
            and center_hit
        )
        or (
            class_tokens
            & {
                "close",
                "closebtn",
                "close-btn",
                "btn-close",
                "van-icon-close",
                "van-popup__close-icon",
            }
            and {"van-popup", "van-popup--center"}.issubset(ancestor_tokens)
            and ancestor_tokens & VANT_CENTER_CLOSE_WRAPPERS
            and center_hit
        )
    )


def _registered_popup_dismissal_elements(
    elements: list[Mapping[str, Any]],
    *,
    capability: str | None,
) -> list[Mapping[str, Any]]:
    """返回可在逻辑动作层继续信任的已注册弹窗关闭结构。"""
    if capability == "dialog_generic_close":
        return _dialog_generic_close_snapshot_elements(elements)
    if capability == "overlay_ad_close":
        return [element for element in elements if _overlay_ad_close_snapshot_element(element)]
    if capability == "homepage_payment_close":
        target = _homepage_payment_close_snapshot_element(elements)
        return [target] if target is not None else []
    if capability != "dialog_overlay":
        return []
    return [
        element
        for element in elements
        if (
            "van-overlay" in _class_tokens(element)
            and "persalepopup" in _ancestor_class_tokens(element)
            and _registered_popup_target_hittable(element)
        )
        or (
            {"close-img", "close-image", "on-dialog-close"} & _class_tokens(element)
            and {"van-popup", "van-popup--center"} & _ancestor_class_tokens(element)
            and _registered_popup_target_hittable(element)
        )
        or (
            _normalized_text(element.get("text")) == "我已保存"
            and {"promotebtn", "btn1"}.issubset(_class_tokens(element))
            and {"promotebtnbox", "van-popup", "van-popup--center"}.issubset(
                _ancestor_class_tokens(element)
            )
            and _registered_popup_target_hittable(element)
        )
    ]


def _registered_non_native_target_enabled(element: Mapping[str, Any]) -> bool:
    return bool(
        element.get("disabled") is not True
        and str(element.get("aria_disabled") or "").casefold() != "true"
    )


def _registered_popup_target_hittable(element: Mapping[str, Any]) -> bool:
    hit_test = element.get("hit_test")
    return bool(
        isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
        and _registered_non_native_target_enabled(element)
    )


def _logical_target_operation_compatible(
    *,
    element: Mapping[str, Any],
    request: SemanticTargetResolutionRequest,
    resolved: ResolvedInteractionTarget,
    snapshot: Mapping[str, Any],
) -> bool:
    if _operation_compatible(element, request.action_type, snapshot=snapshot):
        return True
    if (
        request.action_type is InteractionOperation.CLICK
        and request.parameters.get("target_mode") == "startup_ad_dismissal"
        and resolved.selector is not None
        and _startup_ad_selector_from_snapshot_element(element) == resolved.selector
    ):
        return _registered_non_native_target_enabled(element)
    if request.action_type is not InteractionOperation.CLICK:
        return False
    target_mode = request.parameters.get("target_mode")
    if target_mode == "h5_probe_display_target":
        from services.api.app.application.h5_probe_targets import (
            probe_subject_key,
            select_probe_target,
        )

        subject = select_probe_target(snapshot, goal=request.parameters.get("goal_capability"),
                                      page_type=request.parameters.get("source_page_type"),
                                      expected_key=request.parameters.get("probe_subject_key"))
        return bool(subject is not None and subject.get("selector") == resolved.selector
                    and probe_subject_key(subject) == request.parameters.get("probe_subject_key")
                    and _registered_non_native_target_enabled(element))
    if target_mode == "list_detail_navigation":
        selector = _optional_parameter(request.parameters, "list_detail_selector")
        source_page_type = _optional_parameter(request.parameters, "source_page_type")
        expected_page_type = _optional_parameter(request.parameters, "expected_page_type")
        return bool(
            selector is not None
            and source_page_type is not None
            and expected_page_type is not None
            and h5_list_detail_expected_page_type(source_page_type) == expected_page_type
            and h5_list_detail_selector_allowed(
                source_page_type=source_page_type,
                selector=selector,
            )
            and h5_list_detail_snapshot_element_matches(
                element=element,
                source_page_type=source_page_type,
                selector=selector,
            )
            and _logical_target_matches_resolved_identity(element, resolved)
            and _registered_non_native_target_enabled(element)
        )
    if target_mode in {"bottom_navigation", "page_tab_navigation"}:
        navigation_index = request.parameters.get("navigation_index")
        navigation_label = _optional_parameter(
            request.parameters,
            "navigation_label",
        )
        if not isinstance(navigation_index, int) or navigation_label is None:
            return False
        selected = _select_structural_navigation_item(
            snapshot,
            target_mode=str(target_mode),
            navigation_index=navigation_index,
            navigation_label=navigation_label,
            navigation_family=(
                h5_bottom_navigation_family(request.parameters.get("navigation_family"))
                if target_mode == "bottom_navigation"
                else None
            ),
            interaction_pattern_signature=_optional_parameter(
                request.parameters,
                "interaction_pattern_signature",
            ),
            operation=request.action_type,
        )
        return bool(
            str(element.get("selector") or "").strip() == selected
            and _registered_non_native_target_enabled(element)
        )
    if target_mode == H5_PROFILE_ACTIVITY_TARGET_MODE:
        expected_capability = h5_profile_activity_capability_for_target(request.semantic_target)
        return bool(
            expected_capability is not None
            and _optional_parameter(
                request.parameters,
                "activity_entry_capability",
            )
            == expected_capability
            and _optional_parameter(request.parameters, "source_page_type")
            == H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE
            and _optional_parameter(request.parameters, "expected_page_type")
            == H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE
            and h5_profile_activity_snapshot_element_matches(
                element,
                capability=expected_capability,
            )
            and _logical_target_matches_resolved_identity(element, resolved)
            and _registered_non_native_target_enabled(element)
        )
    if target_mode == H5_PROFILE_SETTINGS_TARGET_MODE:
        expected_capability = h5_profile_settings_capability_for_target(request.semantic_target)
        return bool(
            expected_capability is not None
            and _optional_parameter(
                request.parameters,
                "settings_entry_capability",
            )
            == expected_capability
            and _optional_parameter(request.parameters, "source_page_type")
            == H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE
            and _optional_parameter(request.parameters, "expected_page_type")
            == H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE
            and h5_profile_settings_snapshot_element_matches(
                element,
                capability=expected_capability,
            )
            and _logical_target_matches_resolved_identity(element, resolved)
            and _registered_non_native_target_enabled(element)
        )
    if target_mode == H5_PROFILE_PAYMENT_TARGET_MODE:
        capability = _optional_parameter(
            request.parameters,
            "payment_entry_capability",
        )
        return bool(
            request.semantic_target == H5_PROFILE_PAYMENT_TARGET
            and capability is not None
            and _optional_parameter(request.parameters, "source_page_type")
            == H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE
            and _optional_parameter(request.parameters, "expected_page_type")
            == H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE
            and _profile_payment_logical_target_authorized(
                snapshot=snapshot,
                capability=capability,
                resolved=resolved,
            )
            and _logical_target_matches_resolved_identity(element, resolved)
            and _registered_non_native_target_enabled(element)
        )
    if target_mode != "popup_dismissal":
        return False
    capability = _optional_parameter(
        request.parameters,
        "popup_dismissal_capability",
    )
    if capability == H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_CAPABILITY:
        proof = h5_profile_credential_screenshot_confirm(snapshot)
        return bool(
            proof is not None
            and str(element.get("selector") or "").strip() == proof.source_selector
            and _registered_non_native_target_enabled(element)
        )
    raw_elements = snapshot.get("elements")
    elements = (
        [item for item in raw_elements if isinstance(item, Mapping)]
        if isinstance(raw_elements, list)
        else []
    )
    registered = _registered_popup_dismissal_elements(
        elements,
        capability=capability,
    )
    return bool(
        any(element is item for item in registered)
        and _registered_non_native_target_enabled(element)
    )


def _profile_payment_logical_target_authorized(
    *,
    snapshot: Mapping[str, Any],
    capability: str,
    resolved: ResolvedInteractionTarget,
) -> bool:
    """用唯一来源入口证明会员按钮叶节点到实际点击父容器的映射。"""
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    selectors = [
        selector
        for raw in elements
        if isinstance(raw, Mapping)
        and (
            selector := h5_profile_payment_snapshot_selector(
                raw,
                capability=capability,
            )
        )
    ]
    return bool(
        len(selectors) == 1 and resolved.selector is not None and selectors[0] == resolved.selector
    )


def _logical_target_matches_resolved_identity(
    element: Mapping[str, Any],
    resolved: ResolvedInteractionTarget,
) -> bool:
    selector = str(element.get("selector") or "").strip()
    return bool(
        (resolved.selector is not None and selector == resolved.selector)
        or (
            resolved.expected_rect is not None
            and _rect_matches(element.get("rect"), resolved.expected_rect)
        )
    )


def _trusted_logical_target_rect(
    element: Mapping[str, Any],
) -> dict[str, float] | None:
    raw = element.get("rect")
    if not isinstance(raw, Mapping):
        return None
    values: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        values[key] = float(value)
    if values["width"] <= 0 or values["height"] <= 0:
        return None
    return values


def _h5_probe_popup_target(
    *,
    selector: str,
    element: Mapping[str, Any] | None = None,
) -> ResolvedInteractionTarget:
    return ResolvedInteractionTarget(
        selector=selector,
        expected_rect=(
            _trusted_logical_target_rect(element) if element is not None else None
        ),
        clear_obstructions=True,
        allow_high_z_obstruction_hide=True,
        h5_popup_dismissal_strategy="safe_outside_then_close",
        target_trust="trusted",
        trusted_target_texts=("关闭当前弹窗",),
        trusted_target_role="dismiss",
    )


def _signed_bottom_popup_overlay_selector(
    elements: list[object],
) -> str | None:
    """只为唯一底部抽屉签发唯一可点击全屏遮罩。"""
    overlay = _signed_bottom_popup_overlay_element(elements)
    return str(overlay.get("selector") or "").strip() if overlay is not None else None


def _signed_bottom_popup_overlay_element(
    elements: list[object],
) -> Mapping[str, Any] | None:
    """返回唯一底部抽屉对应的可点击全屏遮罩。"""
    bottom_popups = [
        element
        for element in elements
        if isinstance(element, Mapping)
        and {"van-popup", "van-popup--bottom"}.issubset(_class_tokens(element))
        and str(element.get("role") or "").casefold() == "dialog"
        and _trusted_logical_target_rect(element) is not None
    ]
    overlays = [
        element
        for element in elements
        if isinstance(element, Mapping)
        and "van-overlay" in _class_tokens(element)
        and str(element.get("role") or "").casefold() == "button"
        and element.get("interactive") is True
        and _trusted_logical_target_rect(element) is not None
        and str(element.get("selector") or "").strip()
    ]
    if len(bottom_popups) != 1 or len(overlays) != 1:
        return None
    popup_rect = _trusted_logical_target_rect(bottom_popups[0])
    overlay_rect = _trusted_logical_target_rect(overlays[0])
    if popup_rect is None or overlay_rect is None:
        return None
    overlay_contains_popup = bool(
        overlay_rect["x"] <= popup_rect["x"]
        and overlay_rect["y"] <= popup_rect["y"]
        and overlay_rect["x"] + overlay_rect["width"] >= popup_rect["x"] + popup_rect["width"]
        and overlay_rect["y"] + overlay_rect["height"] >= popup_rect["y"] + popup_rect["height"]
    )
    if not overlay_contains_popup:
        return None
    return overlays[0]


def _grid_dialog_image_close_selector(
    elements: list[object],
) -> str | None:
    """只为唯一九宫格广告 Dialog 签发其内部纯图片关闭键。

    这类弹窗（实测性巴克 SF02）不吃点遮罩关闭：唯一关闭控件是
    <div class="btn"><img alt=""></div>，自身无文字、无 aria-label，
    通用「关闭／跳过」词库一条都匹配不到，能力固定选择器
    .van-overlay:visible 点上去只是点了背景，机械成功而弹窗不动。
    因此按 van-dialog + grid-main 两层祖先限定作用域，并要求中心命中
    自身，避免把弹窗内的广告位误认成关闭键。
    """
    dialogs = [
        element
        for element in elements
        if isinstance(element, Mapping) and "van-dialog" in _class_tokens(element)
    ]
    closes = [
        element
        for element in elements
        if isinstance(element, Mapping)
        and _class_tokens(element) == {"btn"}
        and {"grid-main", "van-dialog"}.issubset(_ancestor_class_tokens(element))
        and not _normalized_text(element.get("text"))
        and not _normalized_text(element.get("aria_label"))
        and _operation_compatible(element, InteractionOperation.CLICK)
        and isinstance(element.get("hit_test"), Mapping)
        and element["hit_test"].get("center_hits_target") is True
        and str(element.get("selector") or "").strip()
    ]
    if len(dialogs) != 1 or len(closes) != 1:
        return None
    return str(closes[0]["selector"]).strip()


def _homepage_payment_close_snapshot_element(
    elements: list[object],
) -> Mapping[str, Any] | None:
    """返回当前 H5 付费弹层中唯一、可命中的关闭控件。"""
    matches = [
        element
        for element in elements
        if isinstance(element, Mapping)
        and _class_tokens(element) == {"close"}
        and {"imgwrapper", "homepagepaymentpopup"}.issubset(
            _ancestor_class_tokens(element)
        )
        and isinstance(element.get("hit_test"), Mapping)
        and element["hit_test"].get("center_hits_target") is True
    ]
    return matches[0] if len(matches) == 1 else None


def _rect_matches(left: object, right: Mapping[str, float]) -> bool:
    if not isinstance(left, Mapping):
        return False
    try:
        return all(
            abs(float(left.get(key)) - float(right.get(key))) <= 1.0
            for key in ("x", "y", "width", "height")
        )
    except (TypeError, ValueError):
        return False


def _logical_target_label(element: Mapping[str, Any]) -> tuple[str, str]:
    for key in ("text", "accessible_name", "aria_label"):
        value = _normalized_text(element.get(key))
        if value:
            return value, key
    value = _normalized_text(element.get("ocr_text"))
    return value, "ocr_text" if value else "none"


def _trusted_target_semantics(element: Mapping[str, Any]) -> tuple[str, ...]:
    """只提取 Snapshot 中目标元素本身的语义字段，供执行前风险复核。"""

    values: list[str] = []
    for key in (
        "text",
        "accessible_name",
        "aria_label",
        "ocr_text",
        "placeholder",
        "name",
        "type",
        "role",
        "tag",
    ):
        value = _normalized_text(element.get(key))
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _trusted_target_semantics_for_selector(
    snapshot: Mapping[str, Any],
    selector: str,
) -> tuple[str, ...]:
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    matches = [
        item
        for item in elements
        if isinstance(item, Mapping) and str(item.get("selector") or "").strip() == selector
    ]
    if len(matches) != 1:
        # selector 多映射时不能把任意一个元素的文案当作可信目标。
        return ()
    return _trusted_target_semantics(matches[0])


def _trusted_target_role_for_selector(
    snapshot: Mapping[str, Any],
    selector: str,
) -> str | None:
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    matches = [
        item
        for item in elements
        if isinstance(item, Mapping) and str(item.get("selector") or "").strip() == selector
    ]
    if len(matches) != 1:
        return None
    return _optional_snapshot_text(matches[0].get("role"))


def _optional_snapshot_text(value: object) -> str | None:
    normalized = _normalized_text(value)
    return normalized or None


def _logical_target_parent_signature(element: Mapping[str, Any]) -> str | None:
    for key in ("parent_selector", "parent_semantics_node_id", "semantics_path"):
        value = element.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:500]
    ancestors = element.get("ancestor_class_tokens")
    if isinstance(ancestors, list):
        values = sorted(
            {str(value).strip().casefold() for value in ancestors if str(value).strip()}
        )
        return " ".join(values)[:500] or None
    return None


def _logical_target_fingerprint(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized) else None


def _select_unique_target(
    snapshot: Mapping[str, Any],
    *,
    semantic_target: str,
    operation: InteractionOperation,
    h5_navigation_target: str | None = None,
) -> str:
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    scored: list[tuple[int, str]] = []
    href_match = (
        re.fullmatch(r'''a\[href=(["'])([^"'\\<>]+)\1\](?::visible)?''',
                     h5_navigation_target.strip())
        if h5_navigation_target is not None else None
    )
    # 只核对快照中已有链接，不把模型字符串作为浏览器选择器执行。
    href_target = (
        urljoin(str(snapshot.get("url") or ""), href_match.group(2))
        if href_match is not None else None
    )
    if h5_navigation_target is not None and h5_navigation_target.strip().startswith("a[") \
            and href_match is None:
        raise SemanticTargetResolutionError(
            "H5 链接目标格式无效", error_code="SEMANTIC_TARGET_INVALID"
        )
    for raw in elements:
        if not isinstance(raw, Mapping) or not _operation_compatible(
            raw, operation, snapshot=snapshot
        ):
            continue
        if (
            h5_navigation_target is not None
            and operation in {InteractionOperation.CLICK, InteractionOperation.TAP}
            and str(raw.get("role") or "").casefold()
            in {"tabpanel", "dialog", "navigation", "main", "region", "list", "grid"}
            and not raw.get("href")
        ):
            # H5 正文容器的聚合文本不是卡片能力；仍由具体按钮/链接签发点击。
            continue
        selector = str(raw.get("selector") or "").strip()
        if not selector:
            continue
        score = (
            120 if str(raw.get("tag") or "").casefold() == "a"
            and urljoin(str(snapshot.get("url") or ""), str(raw.get("href") or ""))
            == href_target else 0
        ) if href_target is not None else _match_score(raw, semantic_target)
        if score > 0:
            scored.append((score, selector))
    if not scored:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 未找到语义目标",
            error_code="SEMANTIC_TARGET_NOT_FOUND",
            retryable=True,
        )
    best_score = max(item[0] for item in scored)
    best = sorted({selector for score, selector in scored if score == best_score})
    if len(best) != 1:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 中语义目标不唯一",
            error_code="SEMANTIC_TARGET_AMBIGUOUS",
        )
    return best[0]


def _select_unique_seek_target(
    snapshot: Mapping[str, Any],
    *,
    semantic_target: str,
) -> str:
    """优先使用有语义标签的进度控件，否则绑定当前页唯一媒体元素。"""
    try:
        return _select_unique_target(
            snapshot,
            semantic_target=semantic_target,
            operation=InteractionOperation.SEEK_MEDIA,
        )
    except SemanticTargetResolutionError as exc:
        if exc.error_code != "SEMANTIC_TARGET_NOT_FOUND":
            raise
    if semantic_target not in _GENERIC_MEDIA_SEEK_TARGETS:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 未找到语义目标",
            error_code="SEMANTIC_TARGET_NOT_FOUND",
            retryable=True,
        )
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    selectors = {
        str(raw.get("selector") or "").strip()
        for raw in elements
        if isinstance(raw, Mapping)
        and str(raw.get("tag") or "").casefold() in {"video", "audio"}
        and _operation_compatible(raw, InteractionOperation.SEEK_MEDIA, snapshot=snapshot)
        and str(raw.get("selector") or "").strip()
    }
    if not selectors:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 未找到可绑定的媒体元素",
            error_code="SEMANTIC_TARGET_NOT_FOUND",
            retryable=True,
        )
    if len(selectors) != 1:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 中媒体元素不唯一",
            error_code="SEMANTIC_TARGET_AMBIGUOUS",
        )
    return next(iter(selectors))


def _select_structural_navigation_item(
    snapshot: Mapping[str, Any],
    *,
    target_mode: str,
    navigation_index: int,
    navigation_label: str,
    navigation_family: str | None = None,
    interaction_pattern_signature: str | None = None,
    operation: InteractionOperation,
) -> str:
    """按直接 Tab 项、同父级序号和标签复核结构化导航目标。"""
    if target_mode == "page_tab_navigation" and interaction_pattern_signature is not None:
        selector = adaptive_navigation_selector(
            snapshot,
            pattern_signature=interaction_pattern_signature,
            navigation_index=navigation_index,
            navigation_label=navigation_label,
            allow_page_tab_reflow=True,
        )
        if selector is None:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未通过页内栏目模式、位置与标签复核",
                error_code="SEMANTIC_TARGET_NOT_FOUND",
                retryable=True,
            )
        return selector
    if (
        target_mode == "bottom_navigation"
        and h5_bottom_navigation_family(navigation_family)
        == H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE
    ):
        selector = adaptive_navigation_selector(
            snapshot,
            pattern_signature=interaction_pattern_signature or "",
            navigation_index=navigation_index,
            navigation_label=navigation_label,
        )
        if selector is None:
            raise SemanticTargetResolutionError(
                "当前 UI Snapshot 未通过自适应导航模式、位置与标签复核",
                error_code="SEMANTIC_TARGET_NOT_FOUND",
                retryable=True,
            )
        return selector
    if (
        target_mode == "bottom_navigation"
        and h5_bottom_navigation_family(navigation_family) == H5_BOTTOM_NAVIGATION_FAMILY_FLUTTER
    ):
        return _select_flutter_bottom_navigation_item(
            snapshot,
            navigation_index=navigation_index,
            navigation_label=navigation_label,
        )
    if target_mode == "bottom_navigation":
        resolved_family = h5_bottom_navigation_family(navigation_family)
        if resolved_family == H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT:
            item_class = None
            item_tag = "li"
            container_class = "pages-foot-fix"
        elif resolved_family == H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP:
            item_class = "footer-item"
            item_tag = "li"
            container_class = "footer-wrap"
        elif resolved_family == H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA:
            item_class = None
            item_tag = None
            container_class = None
        else:
            item_class = "van-tabbar-item"
            item_tag = None
            container_class = "van-tabbar"
    else:
        item_class = "van-tab"
        item_tag = None
        container_class = "van-tabs__nav"
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    expected_leaf_marker = f":nth-of-type({navigation_index})"
    expected_label = _normalized_text(navigation_label)
    page_tab_positions: dict[int, int] = {}
    if target_mode == "page_tab_navigation":
        for raw in elements:
            if not isinstance(raw, Mapping):
                continue
            class_tokens = raw.get("class_tokens")
            normalized_classes = (
                {str(item).strip().casefold() for item in class_tokens if str(item).strip()}
                if isinstance(class_tokens, list)
                else set()
            )
            if "van-tab" not in normalized_classes or "van-tabbar-item" in normalized_classes:
                continue
            selector = str(raw.get("selector") or "").strip()
            leaf = selector.rsplit(" > ", maxsplit=1)[-1]
            position = re.search(r":nth-of-type\((\d+)\)$", leaf)
            dom_position = raw.get("dom_sibling_index")
            if dom_position is not None:
                # ID 选择器不携带序号；使用采集时的直接同类兄弟序号，不猜RTL或排序后坐标。
                page_tab_positions[id(raw)] = (
                    dom_position if type(dom_position) is int and dom_position > 0
                    and (position is None or int(position.group(1)) == dom_position) else -1
                )
            elif position:
                page_tab_positions[id(raw)] = int(position.group(1))
    matches: set[str] = set()
    for raw in elements:
        if not isinstance(raw, Mapping):
            continue
        class_tokens = raw.get("class_tokens")
        normalized_classes = (
            {str(item).strip().casefold() for item in class_tokens if str(item).strip()}
            if isinstance(class_tokens, list)
            else set()
        )
        ancestor_tokens = raw.get("ancestor_class_tokens")
        normalized_ancestors = (
            {str(item).strip().casefold() for item in ancestor_tokens if str(item).strip()}
            if isinstance(ancestor_tokens, list)
            else set()
        )
        selector = str(raw.get("selector") or "").strip()
        leaf_selector = selector.rsplit(" > ", maxsplit=1)[-1]
        raw_role = str(raw.get("role") or "").strip().casefold()
        raw_tag = str(raw.get("tag") or "").strip().casefold()
        generic_item_structure = bool(
            normalized_classes.intersection(H5_GENERIC_NAVIGATION_ITEM_CLASSES)
            or raw_role in {"button", "link", "menuitem", "tab"}
            or raw_tag in {"button", "li"}
            or "item" in normalized_classes
        )
        generic_spa_structure = bool(
            navigation_family == H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA
            and generic_item_structure
            and normalized_ancestors.intersection(H5_GENERIC_NAVIGATION_CONTAINER_CLASSES)
        )
        position_matches = expected_leaf_marker in leaf_selector
        if target_mode == "page_tab_navigation" and id(raw) in page_tab_positions:
            position_matches = page_tab_positions[id(raw)] == navigation_index
        if (
            (
                navigation_family == H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA
                and not generic_spa_structure
            )
            or (item_class is not None and item_class not in normalized_classes)
            or (item_tag is not None and str(raw.get("tag") or "").casefold() != item_tag)
            or (container_class is not None and container_class not in normalized_ancestors)
            or not position_matches
            or _normalized_text(raw.get("text")) != expected_label
            or not selector
            or raw.get("visible") is False
            or not _registered_non_native_target_enabled(raw)
        ):
            continue
        operation_compatible = _operation_compatible(
            raw,
            operation,
            snapshot=snapshot,
        )
        page_tab_horizontally_offscreen = bool(
            target_mode == "page_tab_navigation"
            and operation is InteractionOperation.CLICK
            and _is_interactive(raw)
            and _center_horizontally_outside_viewport(raw, snapshot=snapshot)
        )
        if not operation_compatible and not (
            page_tab_horizontally_offscreen
            or (
                target_mode == "bottom_navigation"
                and (
                    navigation_family
                    in {
                        H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT,
                        H5_BOTTOM_NAVIGATION_FAMILY_FOOTER_WRAP,
                        H5_BOTTOM_NAVIGATION_FAMILY_GENERIC_SPA,
                    }
                    or _bottom_navigation_intercepted_by_inactive_launch_shell(
                        raw,
                        snapshot=snapshot,
                    )
                    or (
                        h5_bottom_navigation_family(navigation_family)
                        == H5_BOTTOM_NAVIGATION_FAMILY_VANT
                        and _bottom_navigation_intercepted_by_registered_content_overlay(
                            raw,
                            snapshot=snapshot,
                        )
                    )
                )
            )
        ):
            continue
        matches.add(selector)
    if not matches:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 未找到结构化导航目标",
            error_code="SEMANTIC_TARGET_NOT_FOUND",
            retryable=True,
        )
    if len(matches) != 1:
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 中结构化导航目标不唯一",
            error_code="SEMANTIC_TARGET_AMBIGUOUS",
        )
    return next(iter(matches))


def _select_flutter_bottom_navigation_item(
    snapshot: Mapping[str, Any],
    *,
    navigation_index: int,
    navigation_label: str,
) -> str:
    """按同一底部行、横向顺序和标签验签 Flutter 语义导航项。"""
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    candidates: list[tuple[float, float, Mapping[str, Any]]] = []
    for raw in elements:
        if not isinstance(raw, Mapping):
            continue
        rect = raw.get("rect")
        if (
            raw.get("structural_navigation_kind") != "flutter_bottom_navigation"
            or str(raw.get("tag") or "").casefold() != "flt-semantics"
            or raw.get("interactive") is not True
            or not _normalized_text(raw.get("text"))
            or not isinstance(rect, Mapping)
        ):
            continue
        try:
            x = float(rect.get("x"))
            y = float(rect.get("y"))
            width = float(rect.get("width"))
            height = float(rect.get("height"))
        except (TypeError, ValueError):
            continue
        if not (20 <= width <= 100 and 36 <= height <= 80):
            continue
        candidates.append((x, y, raw))

    rows: list[list[tuple[float, float, Mapping[str, Any]]]] = []
    for candidate in sorted(candidates, key=lambda item: (item[1], item[0])):
        matching_row = next(
            (row for row in rows if abs(row[0][1] - candidate[1]) <= 4),
            None,
        )
        if matching_row is None:
            rows.append([candidate])
        else:
            matching_row.append(candidate)
    valid_rows = [
        sorted(row, key=lambda item: item[0])
        for row in rows
        if 5 <= len(row) <= 10
        and max(item[0] for item in row) - min(item[0] for item in row) >= 220
    ]
    if len(valid_rows) != 1 or navigation_index > len(valid_rows[0]):
        raise SemanticTargetResolutionError(
            "当前 UI Snapshot 未证明唯一 Flutter 底部导航行",
            error_code="SEMANTIC_TARGET_NOT_FOUND",
            retryable=True,
        )
    selected = valid_rows[0][navigation_index - 1][2]
    selector = str(selected.get("selector") or "").strip()
    if _normalized_text(selected.get("text")) != _normalized_text(navigation_label) or not selector:
        raise SemanticTargetResolutionError(
            "Flutter 底部导航位置与标签不一致",
            error_code="STRUCTURAL_NAVIGATION_TARGET_MISMATCH",
            retryable=True,
        )
    return selector


def _bottom_navigation_intercepted_by_inactive_launch_shell(
    element: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
) -> bool:
    """仅为空启动壳放行已签名底栏，真实开屏与普通遮罩继续失败关闭。"""
    if not _is_interactive(element):
        return False
    hit_test = element.get("hit_test")
    center_target = (
        str(hit_test.get("center_target") or "").strip()
        if isinstance(hit_test, Mapping) and hit_test.get("center_hits_target") is False
        else ""
    )
    if not center_target:
        return False
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    shells = [
        raw
        for raw in elements
        if isinstance(raw, Mapping)
        and str(raw.get("selector") or "").strip() == center_target
        and raw.get("inactive_launch_shell") is True
        and raw.get("interactive") is False
        and not _normalized_text(raw.get("text"))
        and _class_tokens(raw) == {"launch-page"}
        and _rect_covers_viewport(raw, snapshot=snapshot)
    ]
    if len(shells) == 1:
        return True
    return _bottom_navigation_hit_profiles_prove_inactive_launch_shell(
        element,
        snapshot=snapshot,
        center_target=center_target,
    )


def _bottom_navigation_hit_profiles_prove_inactive_launch_shell(
    element: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    center_target: str,
) -> bool:
    """零矩形空壳只由整排底栏一致的实时命中剖面证明。"""
    viewport = snapshot.get("viewport")
    viewport_width = (
        _optional_finite_number(viewport.get("width")) if isinstance(viewport, Mapping) else None
    )
    viewport_height = (
        _optional_finite_number(viewport.get("height")) if isinstance(viewport, Mapping) else None
    )
    if viewport_width is None or viewport_height is None:
        return False
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    profiled_items: list[tuple[float, str, str]] = []
    for raw in elements:
        if not isinstance(raw, Mapping):
            continue
        raw_hit_test = raw.get("hit_test")
        if not (
            "van-tabbar-item" in _class_tokens(raw)
            and "van-tabbar" in _ancestor_class_tokens(raw)
            and isinstance(raw_hit_test, Mapping)
            and raw_hit_test.get("center_hits_target") is False
            and str(raw_hit_test.get("center_target") or "").strip() == center_target
            and raw_hit_test.get("center_target_inactive_launch_shell") is True
            and _normalized_token_set(raw_hit_test.get("center_target_class_tokens"))
            == {"launch-page"}
        ):
            continue
        rect = raw.get("rect")
        if not isinstance(rect, Mapping):
            continue
        x = _optional_finite_number(rect.get("x"))
        y = _optional_finite_number(rect.get("y"))
        width = _optional_finite_number(rect.get("width"))
        height = _optional_finite_number(rect.get("height"))
        if None in {x, y, width, height}:
            continue
        assert x is not None
        assert y is not None
        assert width is not None
        assert height is not None
        if width <= 0 or height <= 0 or y >= viewport_height or y + height < viewport_height - 160:
            continue
        selector = str(raw.get("selector") or "").strip()
        label = _normalized_text(raw.get("text"))
        if not selector or not label:
            continue
        profiled_items.append((x + width / 2, selector, label))
    if len(profiled_items) < 3:
        return False
    centers = [item[0] for item in profiled_items]
    selectors = {item[1] for item in profiled_items}
    labels = {item[2] for item in profiled_items}
    return bool(
        len(selectors) == len(profiled_items)
        and len(labels) == len(profiled_items)
        and max(centers) - min(centers) >= viewport_width * 0.65
        and any(item[1] == str(element.get("selector") or "").strip() for item in profiled_items)
    )


def _bottom_navigation_intercepted_by_registered_content_overlay(
    element: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
) -> bool:
    """整排 Vant 底栏被已登记媒体卡片层截获时，交给执行端唯一性与效果验签。"""
    if not _is_interactive(element):
        return False
    hit_test = element.get("hit_test")
    center_target = (
        str(hit_test.get("center_target") or "").strip()
        if isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is False
        and _trusted_content_overlay_hit_profile(hit_test)
        else ""
    )
    if not center_target:
        return False
    viewport = snapshot.get("viewport")
    viewport_width = (
        _optional_finite_number(viewport.get("width")) if isinstance(viewport, Mapping) else None
    )
    viewport_height = (
        _optional_finite_number(viewport.get("height")) if isinstance(viewport, Mapping) else None
    )
    if viewport_width is None or viewport_height is None:
        return False
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    profiled_items: list[tuple[float, str, str]] = []
    for raw in elements:
        if not isinstance(raw, Mapping):
            continue
        raw_hit_test = raw.get("hit_test")
        if not (
            "van-tabbar-item" in _class_tokens(raw)
            and "van-tabbar" in _ancestor_class_tokens(raw)
            and isinstance(raw_hit_test, Mapping)
            and raw_hit_test.get("center_hits_target") is False
            and str(raw_hit_test.get("center_target") or "").strip() == center_target
            and _trusted_content_overlay_hit_profile(raw_hit_test)
        ):
            continue
        rect = raw.get("rect")
        if not isinstance(rect, Mapping):
            continue
        x = _optional_finite_number(rect.get("x"))
        y = _optional_finite_number(rect.get("y"))
        width = _optional_finite_number(rect.get("width"))
        height = _optional_finite_number(rect.get("height"))
        if None in {x, y, width, height}:
            continue
        assert x is not None
        assert y is not None
        assert width is not None
        assert height is not None
        if width <= 0 or height <= 0 or y >= viewport_height or y + height < viewport_height - 160:
            continue
        selector = str(raw.get("selector") or "").strip()
        label = _normalized_text(raw.get("text"))
        if not selector or not label:
            continue
        profiled_items.append((x + width / 2, selector, label))
    if len(profiled_items) < 3:
        return False
    centers = [item[0] for item in profiled_items]
    selectors = {item[1] for item in profiled_items}
    labels = {item[2] for item in profiled_items}
    return bool(
        len(selectors) == len(profiled_items)
        and len(labels) == len(profiled_items)
        and max(centers) - min(centers) >= viewport_width * 0.65
        and any(item[1] == str(element.get("selector") or "").strip() for item in profiled_items)
    )


def _trusted_content_overlay_hit_profile(hit_test: Mapping[str, Any]) -> bool:
    """只接受普通媒体卡片的 overlay；模态、公告和弹窗作用域继续失败关闭。"""
    class_tokens = _normalized_token_set(hit_test.get("center_target_class_tokens"))
    ancestor_tokens = _normalized_token_set(hit_test.get("center_target_ancestor_class_tokens"))
    modal_tokens = {
        "dialog",
        "modal",
        "popup",
        "van-dialog",
        "van-overlay",
        "van-popup",
    }
    content_families = {"ad-item", "swiper-link", "video-item"}
    return bool(
        class_tokens == {"overlay"}
        and "aspect-ratio" in ancestor_tokens
        and content_families.intersection(ancestor_tokens)
        and not modal_tokens.intersection(ancestor_tokens)
    )


def _class_tokens(element: Mapping[str, Any]) -> set[str]:
    raw_tokens = element.get("class_tokens")
    return (
        {str(item).strip().casefold() for item in raw_tokens if str(item).strip()}
        if isinstance(raw_tokens, list)
        else set()
    )


def _ancestor_class_tokens(element: Mapping[str, Any]) -> set[str]:
    return _normalized_token_set(element.get("ancestor_class_tokens"))


def _normalized_token_set(value: object) -> set[str]:
    return (
        {str(item).strip().casefold() for item in value if str(item).strip()}
        if isinstance(value, list)
        else set()
    )


def _rect_covers_viewport(
    element: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
) -> bool:
    rect = element.get("rect")
    viewport = snapshot.get("viewport")
    if not isinstance(rect, Mapping) or not isinstance(viewport, Mapping):
        return False
    x = _optional_finite_number(rect.get("x"))
    y = _optional_finite_number(rect.get("y"))
    width = _optional_finite_number(rect.get("width"))
    height = _optional_finite_number(rect.get("height"))
    viewport_width = _optional_finite_number(viewport.get("width"))
    viewport_height = _optional_finite_number(viewport.get("height"))
    if None in {x, y, width, height, viewport_width, viewport_height}:
        return False
    assert x is not None
    assert y is not None
    assert width is not None
    assert height is not None
    assert viewport_width is not None
    assert viewport_height is not None
    tolerance = 2.0
    return bool(
        x <= tolerance
        and y <= tolerance
        and x + width >= viewport_width - tolerance
        and y + height >= viewport_height - tolerance
    )


def _snapshot_element_visible_and_hittable(
    element: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
) -> bool:
    """要求卡片叶节点与 390×844 当前视口相交且中心命中自身。"""
    hit_test = element.get("hit_test")
    rect = element.get("rect")
    viewport = snapshot.get("viewport")
    if (
        not isinstance(hit_test, Mapping)
        or hit_test.get("center_hits_target") is not True
        or not isinstance(rect, Mapping)
        or not isinstance(viewport, Mapping)
    ):
        return False
    x = _optional_finite_number(rect.get("x"))
    y = _optional_finite_number(rect.get("y"))
    width = _optional_finite_number(rect.get("width"))
    height = _optional_finite_number(rect.get("height"))
    viewport_width = _optional_finite_number(viewport.get("width"))
    viewport_height = _optional_finite_number(viewport.get("height"))
    if None in {x, y, width, height, viewport_width, viewport_height}:
        return False
    assert x is not None
    assert y is not None
    assert width is not None
    assert height is not None
    assert viewport_width is not None
    assert viewport_height is not None
    return (
        width > 0
        and height > 0
        and x + width > 0
        and y + height > 0
        and x < viewport_width
        and y < viewport_height
    )


def _snapshot_element_intercepted_by_inactive_launch_shell(
    element: Mapping[str, Any],
) -> bool:
    """只接受采集端验明为空壳的 launch-page 指针截获。"""
    hit_test = element.get("hit_test")
    if not isinstance(hit_test, Mapping):
        return False
    raw_tokens = hit_test.get("center_target_class_tokens")
    tokens = (
        {str(item).strip().casefold() for item in raw_tokens if str(item).strip()}
        if isinstance(raw_tokens, list)
        else set()
    )
    return bool(
        hit_test.get("center_hits_target") is False
        and hit_test.get("center_target_inactive_launch_shell") is True
        and tokens == {"launch-page"}
    )


def _center_below_viewport(
    element: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
) -> bool:
    """元素中心位于采集视口下缘之下且横向在屏内：命中测试不可信但可滚动触达。"""
    rect = element.get("rect")
    viewport = snapshot.get("viewport")
    if not isinstance(rect, Mapping) or not isinstance(viewport, Mapping):
        return False
    x = _optional_finite_number(rect.get("x"))
    y = _optional_finite_number(rect.get("y"))
    width = _optional_finite_number(rect.get("width"))
    height = _optional_finite_number(rect.get("height"))
    viewport_width = _optional_finite_number(viewport.get("width"))
    viewport_height = _optional_finite_number(viewport.get("height"))
    if None in {x, y, width, height, viewport_width, viewport_height}:
        return False
    assert x is not None
    assert y is not None
    assert width is not None
    assert height is not None
    assert viewport_width is not None
    assert viewport_height is not None
    center_x = x + width / 2
    center_y = y + height / 2
    return (
        width > 0 and height > 0 and center_y >= viewport_height and 0 <= center_x < viewport_width
    )


def _center_horizontally_outside_viewport(
    element: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
) -> bool:
    """元素仅横向离屏时，允许已验签页内 Tab 由浏览器滚动后点击。"""
    rect = element.get("rect")
    viewport = snapshot.get("viewport")
    if not isinstance(rect, Mapping) or not isinstance(viewport, Mapping):
        return False
    x = _optional_finite_number(rect.get("x"))
    y = _optional_finite_number(rect.get("y"))
    width = _optional_finite_number(rect.get("width"))
    height = _optional_finite_number(rect.get("height"))
    viewport_width = _optional_finite_number(viewport.get("width"))
    viewport_height = _optional_finite_number(viewport.get("height"))
    if None in {x, y, width, height, viewport_width, viewport_height}:
        return False
    assert x is not None
    assert y is not None
    assert width is not None
    assert height is not None
    assert viewport_width is not None
    assert viewport_height is not None
    center_x = x + width / 2
    center_y = y + height / 2
    return bool(
        width > 0
        and height > 0
        and 0 <= center_y < viewport_height
        and (center_x < 0 or center_x >= viewport_width)
    )


def _optional_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _nearest_interactive_semantics_ancestor(
    item: Mapping[str, Any],
    by_selector: dict[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """沿 parent_semantics_selector 向上找最近的可交互语义祖先。

    与浏览器侧 closest('flt-semantics[flt-tappable]') 同义：中间的
    dialog / group 壳层不可交互，不能因此判定控件不属于全屏开屏层。
    """
    seen: set[str] = set()
    current = by_selector.get(str(item.get("parent_semantics_selector") or ""))
    while current is not None:
        selector = str(current.get("selector") or "")
        if selector in seen:
            return None
        seen.add(selector)
        if current.get("interactive") is True:
            return current
        current = by_selector.get(str(current.get("parent_semantics_selector") or ""))
    return None


def _flutter_fullscreen_startup_skip_snapshot_matches(
    *,
    snapshot: Mapping[str, Any],
    elements: list[object],
) -> list[Mapping[str, Any]]:
    """从可信快照中验签全屏 Flutter 开屏层右上角唯一清障控件。"""
    viewport = snapshot.get("viewport")
    if not isinstance(viewport, Mapping):
        return []
    viewport_width = _optional_finite_number(viewport.get("width"))
    viewport_height = _optional_finite_number(viewport.get("height"))
    if viewport_width is None or viewport_height is None:
        return []
    semantic_elements = [
        item
        for item in elements
        if isinstance(item, Mapping) and str(item.get("tag") or "").casefold() == "flt-semantics"
    ]
    by_selector = {
        str(item.get("selector") or ""): item
        for item in semantic_elements
        if str(item.get("selector") or "").strip()
    }
    matches: list[Mapping[str, Any]] = []
    for item in semantic_elements:
        rect = _snapshot_rect(item)
        hit_test = item.get("hit_test")
        style = item.get("style")
        # Flutter 会在全屏可点层与角标控件之间插 dialog/group 壳层（杏吧实测），
        # 只看直接父节点会把整个开屏清障能力判丢，这里取最近的可交互祖先。
        parent = _nearest_interactive_semantics_ancestor(item, by_selector)
        parent_rect = _snapshot_rect(parent) if parent is not None else None
        if rect is None or parent_rect is None or not isinstance(hit_test, Mapping):
            continue
        x, y, width, height = rect
        parent_x, parent_y, parent_width, parent_height = parent_rect
        z_index = str(style.get("z_index") or "") if isinstance(style, Mapping) else ""
        if not re.fullmatch(r"[1-9]\d*", z_index):
            continue
        if (
            item.get("interactive") is True
            and str(item.get("role") or "").casefold() in {"", "text"}
            and _normalized_text(item.get("text")) in FLUTTER_STARTUP_DISMISS_TEXTS
            and hit_test.get("center_hits_target") is True
            and 30 <= width <= 100
            and 20 <= height <= 60
            and x >= viewport_width * 0.65
            and x + width <= viewport_width + 2
            and 0 <= y <= viewport_height * 0.1
            and parent.get("interactive") is True
            and abs(parent_x) <= 2
            and abs(parent_y) <= 2
            and parent_width >= viewport_width * 0.95
            and parent_height >= viewport_height * 0.95
        ):
            matches.append(item)
    return matches if len(matches) == 1 else []


def _flutter_popup_close_selectors(
    elements: list[object],
    *,
    require_app_open_label: bool,
    close_above: bool = False,
    scroll_body: bool = False,
    image_body_top_right: bool = False,
    account_notice_child: bool = False,
) -> frozenset[str]:
    """从可信快照中确认推广卡片相邻的唯一 Flutter tap 关闭节点。"""
    semantic_elements = [
        item
        for item in elements
        if isinstance(item, Mapping) and str(item.get("tag") or "").casefold() == "flt-semantics"
    ]
    label_present = any(
        item.get("interactive") is True
        and str(item.get("role") or "").casefold() == "text"
        and _normalized_text(item.get("text")).casefold().endswith("app内打开".casefold())
        for item in semantic_elements
    )
    if require_app_open_label and not label_present:
        return frozenset()
    empty_controls = [
        item
        for item in semantic_elements
        if item.get("interactive") is True
        and not _normalized_text(item.get("text"))
        and not str(item.get("role") or "").strip()
        and isinstance(item.get("hit_test"), Mapping)
        and item["hit_test"].get("center_hits_target") is True
    ]
    if account_notice_child:
        popup_bodies = [item for item in semantic_elements if _is_flutter_account_notice_body(item)]
    elif image_body_top_right:
        popup_bodies = [item for item in semantic_elements if _is_flutter_image_popup_body(item)]
    elif scroll_body:
        popup_bodies = [item for item in semantic_elements if _is_flutter_scroll_popup_body(item)]
    else:
        popup_bodies = [item for item in empty_controls if _is_flutter_popup_body(item)]
    close_controls = [item for item in empty_controls if _is_flutter_popup_close(item)]
    matched_selectors = {
        str(close_control.get("selector") or "")
        for popup_body in popup_bodies
        for close_control in close_controls
        if _flutter_popup_geometry_matches(
            popup_body,
            close_control,
            close_above=close_above,
            top_right=image_body_top_right or account_notice_child,
        )
        and str(close_control.get("selector") or "").strip()
    }
    return frozenset(matched_selectors) if len(matched_selectors) == 1 else frozenset()


def _snapshot_rect(item: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    raw = item.get("rect")
    if not isinstance(raw, Mapping):
        return None
    values = tuple(raw.get(key) for key in ("x", "y", "width", "height"))
    if any(isinstance(value, bool) or not isinstance(value, int | float) for value in values):
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def _is_flutter_popup_body(item: Mapping[str, Any]) -> bool:
    rect = _snapshot_rect(item)
    return rect is not None and 240 <= rect[2] <= 360 and 240 <= rect[3] <= 500


def _is_flutter_scroll_popup_body(item: Mapping[str, Any]) -> bool:
    """只接受快照证明为不可交互且确有纵向溢出的 Flutter 滚动容器。"""
    style = item.get("style")
    text_metrics = item.get("text_metrics")
    hit_test = item.get("hit_test")
    return bool(
        item.get("interactive") is False
        and not str(item.get("role") or "").strip()
        and isinstance(style, Mapping)
        and str(style.get("overflow_y") or "").casefold() in {"auto", "scroll"}
        and isinstance(text_metrics, Mapping)
        and text_metrics.get("vertical_overflow") is True
        and isinstance(hit_test, Mapping)
        and isinstance(hit_test.get("center_hits_target"), bool)
        and _is_flutter_popup_body(item)
    )


def _is_flutter_image_popup_body(item: Mapping[str, Any]) -> bool:
    """只接受快照证明为不可交互的 Flutter 图片弹窗主体。"""
    return bool(
        item.get("interactive") is False
        and str(item.get("role") or "").casefold() == "img"
        and not _normalized_text(item.get("text"))
        and _is_flutter_popup_body(item)
    )


def _is_flutter_account_notice_body(item: Mapping[str, Any]) -> bool:
    """只接受带完整账号找回提示标记的不可交互 Flutter group。"""
    aria_label = re.sub(r"\s+", "", str(item.get("aria_label") or ""))
    return bool(
        item.get("interactive") is False
        and str(item.get("role") or "").casefold() == "group"
        and all(
            marker in aria_label
            for marker in ("重要提示", "首次安装APP请先保存此信息", "账号找回", "邀请码")
        )
        and _is_flutter_popup_body(item)
    )


def _is_flutter_popup_close(item: Mapping[str, Any]) -> bool:
    rect = _snapshot_rect(item)
    return bool(
        rect is not None
        and 20 <= rect[2] <= 64
        and 20 <= rect[3] <= 64
        and abs(rect[2] - rect[3]) <= 8
    )


def _flutter_popup_geometry_matches(
    popup_body: Mapping[str, Any],
    close_control: Mapping[str, Any],
    *,
    close_above: bool = False,
    top_right: bool = False,
) -> bool:
    body_rect = _snapshot_rect(popup_body)
    close_rect = _snapshot_rect(close_control)
    if body_rect is None or close_rect is None:
        return False
    body_x, body_y, body_width, body_height = body_rect
    close_x, close_y, close_width, close_height = close_rect
    if top_right:
        return bool(
            0 <= close_y - body_y <= 48
            and 0 <= (body_x + body_width) - (close_x + close_width) <= 48
        )
    gap = body_y - (close_y + close_height) if close_above else close_y - (body_y + body_height)
    return bool(
        abs((body_x + body_width / 2) - (close_x + close_width / 2)) <= 4 and 0 <= gap <= 80
    )


def _operation_compatible(
    element: Mapping[str, Any],
    operation: InteractionOperation,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> bool:
    if element.get("disabled") is True:
        return False
    if operation is InteractionOperation.INPUT:
        if element.get("read_only") is True:
            return False
        return (
            str(element.get("tag") or "").casefold()
            in {
                "input",
                "textarea",
                "select",
            }
            or element.get("content_editable") is True
        )
    if operation in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        if not _is_interactive(element):
            return False
        hit_test = element.get("hit_test")
        if isinstance(hit_test, Mapping) and hit_test.get("center_hits_target") is False:
            # 命中测试只在元素中心位于采集视口内时可信；首屏下缘之下的元素
            # elementFromPoint 必然打到别处，不能当作遮挡证据——执行端
            # Playwright 点击前会先滚动到元素，签发这类目标是安全的。
            if snapshot is None or not _center_below_viewport(element, snapshot=snapshot):
                return False
    if operation is InteractionOperation.SEEK_MEDIA:
        tag = str(element.get("tag") or "").casefold()
        role = str(element.get("role") or "").casefold()
        input_type = str(element.get("type") or "").casefold()
        return (
            tag in {"video", "audio"}
            or role in {"slider", "progressbar"}
            or (tag == "input" and input_type == "range")
        )
    return True


def _is_interactive(element: Mapping[str, Any]) -> bool:
    if element.get("interactive") is True:
        return True
    tag = str(element.get("tag") or "").casefold()
    role = str(element.get("role") or "").casefold()
    raw_class_tokens = element.get("class_tokens")
    class_tokens = (
        {str(item).strip().casefold() for item in raw_class_tokens if str(item).strip()}
        if isinstance(raw_class_tokens, list)
        else set()
    )
    return (
        tag in {"a", "button", "input", "select", "textarea", "summary"}
        or "btn" in class_tokens
        or role
        in {
            "button",
            "checkbox",
            "link",
            "menuitem",
            "option",
            "radio",
            "switch",
            "tab",
        }
        or bool(element.get("href"))
    )


def _match_score(element: Mapping[str, Any], target: str) -> int:
    weighted_fields = (
        ("aria_label", 120),
        ("text", 110),
        ("placeholder", 100),
        ("name", 90),
        ("alt", 80),
        ("role", 60),
        ("tag", 40),
    )
    best = 0
    combined: list[str] = []
    for field_name, weight in weighted_fields:
        value = _normalized_text(element.get(field_name))
        if not value:
            continue
        combined.append(value)
        if value == target:
            best = max(best, weight)
        elif target in value:
            # 大型 tabpanel 会聚合整页文本；同样包含目标时，应让文字范围更紧的
            # 可交互卡片胜出。完全相同的重复按钮仍保持同分并触发歧义保护。
            specificity_bonus = max(
                1,
                min(20, round(20 * len(target) / max(len(value), 1))),
            )
            structured_card_bonus = (
                int(
                    field_name == "text"
                    and {"horizontalvideobox", "videobox"}.issubset(_class_tokens(element))
                )
                * 20
            )
            best = max(
                best,
                weight - 25 + specificity_bonus + structured_card_bonus,
            )
        elif value in target:
            best = max(best, weight - 35)
    tokens = tuple(item for item in target.split(" ") if item)
    haystack = " ".join(combined)
    if tokens and all(token in haystack for token in tokens):
        best = max(best, 50)
    return best


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _web_candidate_text_is_safe(*, capability: str, normalized_text: str) -> bool:
    """分别复验成年确认与普通关闭文本，禁止两种能力互相冒充。"""

    always_denied = (
        "验证码",
        "人机",
        "captcha",
        "滑块",
        "登录",
        "注册",
        "支付",
        "购买",
        "下载",
    )
    if any(marker in normalized_text for marker in always_denied):
        return False
    if capability == "web_age_gate_confirm":
        if any(marker in normalized_text for marker in ("退出", "离开", "未成年")):
            return False
        return any(marker in normalized_text for marker in ("18", "十八", "成年")) and any(
            marker in normalized_text for marker in ("进入", "确认", "同意", "继续", "yes")
        )
    if capability == "web_obstruction_dismiss":
        if any(marker in normalized_text for marker in ("18", "十八", "成年", "退出", "离开")):
            return False
        return any(
            marker in normalized_text
            for marker in (
                "关闭",
                "取消",
                "稍后",
                "知道",
                "跳过",
                "不再提示",
                "close",
                "×",
            )
        )
    return False


def _required_parameter(parameters: Mapping[str, Any], name: str) -> str:
    value = _optional_parameter(parameters, name)
    if value is None:
        raise SemanticTargetResolutionError(
            f"动作参数缺少 {name}",
            error_code="INTERACTION_PARAMETER_MISSING",
        )
    return value


def _validate_web_auxiliary_before_action_binding(
    *,
    request: SemanticTargetResolutionRequest,
    binding: Mapping[str, Any],
    discovery_snapshot: Mapping[str, Any],
    before_snapshot: Mapping[str, Any],
    discovery_capture_id: str,
    before_capture_id: str,
) -> None:
    """严格验签发现 Capture A 到动作前 Capture B 的持久化链。"""

    sequence = binding.get("session_command_sequence")
    action_sequence = binding.get("action_command_sequence")
    surface_sequence = binding.get("surface_command_sequence")
    action_epoch = binding.get("action_finalization_epoch")
    surface_epoch = binding.get("surface_finalization_epoch")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (
            sequence,
            action_sequence,
            surface_sequence,
            action_epoch,
            surface_epoch,
        )
    ) or not (
        sequence > 0
        and sequence == action_sequence == surface_sequence
        and action_epoch >= 0
        and action_epoch == surface_epoch
    ):
        raise _web_auxiliary_source_mismatch("Web 辅助入口动作序列或 Finalization Epoch 已漂移")

    agent_run_id = _binding_text(binding, "agent_run_id")
    if not agent_run_id or request.session_id != f"web:{agent_run_id}":
        raise _web_auxiliary_source_mismatch("Web 辅助入口 Agent Run 与 Session 归属不一致")
    if binding.get("surface_status") not in {
        SurfaceSessionStatus.ACTIVE.value,
        SurfaceSessionStatus.DRAINING.value,
    }:
        raise _web_auxiliary_source_mismatch("Web 辅助入口 Surface 已不可执行")
    expected_values = {
        "capture_id": before_capture_id,
        "task_id": request.task_id,
        "surface_kind": "web",
        "session_id": request.session_id,
        "parent_capture_id": discovery_capture_id,
        "capture_role": "before_action",
        "capture_status": "captured",
        "parent_agent_run_id": agent_run_id,
        "parent_surface_kind": "web",
        "parent_session_id": request.session_id,
        "parent_capture_status": "captured",
        "action_task_id": request.task_id,
        "action_session_id": request.session_id,
        "action_requested_source_capture_id": discovery_capture_id,
        "action_source_capture_id": before_capture_id,
        "action_status": "executing",
    }
    if any(binding.get(key) != value for key, value in expected_values.items()):
        raise _web_auxiliary_source_mismatch("Web 辅助入口动作前 Capture 归属或动作身份已漂移")
    if binding.get("surface_current_capture_id") not in {
        discovery_capture_id,
        before_capture_id,
    }:
        raise _web_auxiliary_source_mismatch("Web 辅助入口动作前 Capture 归属或动作身份已漂移")
    if not _binding_text(binding, "action_execution_id"):
        raise _web_auxiliary_source_mismatch("Web 辅助入口缺少动作执行身份")

    action_intent = binding.get("action_intent")
    action_parameters = (
        action_intent.get("parameters") if isinstance(action_intent, Mapping) else None
    )
    if not isinstance(action_intent, Mapping) or not isinstance(
        action_parameters,
        Mapping,
    ):
        raise _web_auxiliary_source_mismatch("Web 辅助入口动作意图无效")
    expected_intent = {
        "task_id": request.task_id,
        "session_id": request.session_id,
        "source_capture_id": discovery_capture_id,
        "action_type": request.action_type.value,
        "semantic_target": request.semantic_target,
    }
    if any(action_intent.get(key) != value for key, value in expected_intent.items()):
        raise _web_auxiliary_source_mismatch("Web 辅助入口动作意图身份已漂移")
    bound_parameter_names = (
        "target_mode",
        "web_auxiliary_candidate_id",
        "web_auxiliary_source_capture_id",
        "web_auxiliary_candidate_source_capture_id",
        "web_auxiliary_origin",
        "web_auxiliary_kind",
        "destination_url",
    )
    if any(
        action_parameters.get(name) != request.parameters.get(name)
        for name in bound_parameter_names
    ):
        raise _web_auxiliary_source_mismatch("Web 辅助入口动作参数身份已漂移")

    final_url = _binding_text(binding, "final_url")
    parent_final_url = _binding_text(binding, "parent_final_url")
    action_requested_url = _binding_text(binding, "action_requested_url")
    if action_requested_url != request.parameters.get("destination_url"):
        raise _web_auxiliary_source_mismatch("Web 辅助入口请求 URL 已漂移")
    if before_snapshot.get("url") != final_url or (
        discovery_snapshot.get("url") != parent_final_url
    ):
        raise _web_auxiliary_source_mismatch("Web 辅助入口 Snapshot URL 已漂移")
    capture_requested_url = binding.get("capture_requested_url")
    if capture_requested_url is not None and not (
        isinstance(capture_requested_url, str) and capture_requested_url.strip()
    ):
        raise _web_auxiliary_source_mismatch("Web 辅助入口 Capture 请求 URL 无效")
    from services.api.app.application.web_navigation_safety import (
        WebNavigationSafetyError,
        validate_web_final_url,
    )

    try:
        origin = _required_parameter(request.parameters, "web_auxiliary_origin")
        validate_web_final_url(
            site_url=origin,
            requested_url=action_requested_url,
            final_url=final_url,
        )
        validate_web_final_url(
            site_url=origin,
            requested_url=parent_final_url,
            final_url=parent_final_url,
        )
        if isinstance(capture_requested_url, str):
            validate_web_final_url(
                site_url=origin,
                requested_url=capture_requested_url,
                final_url=final_url,
            )
    except WebNavigationSafetyError:
        raise _web_auxiliary_source_mismatch("Web 辅助入口动作前页面已跨域或降级") from None


def _validate_web_detail_before_action_binding(
    *,
    request: SemanticTargetResolutionRequest,
    binding: Mapping[str, Any],
    discovery_snapshot: Mapping[str, Any],
    before_snapshot: Mapping[str, Any],
    discovery_capture_id: str,
    before_capture_id: str,
    trusted_source_url: str,
) -> None:
    """严格验签详情发现 Capture A 到动作前 Capture B 的持久化链。"""

    sequence = binding.get("session_command_sequence")
    action_sequence = binding.get("action_command_sequence")
    surface_sequence = binding.get("surface_command_sequence")
    action_epoch = binding.get("action_finalization_epoch")
    surface_epoch = binding.get("surface_finalization_epoch")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (
            sequence,
            action_sequence,
            surface_sequence,
            action_epoch,
            surface_epoch,
        )
    ) or not (
        sequence > 0
        and sequence == action_sequence == surface_sequence
        and action_epoch >= 0
        and action_epoch == surface_epoch
    ):
        raise _web_detail_source_mismatch("Web 详情动作序列或 Finalization Epoch 已漂移")

    agent_run_id = _binding_text(binding, "agent_run_id")
    if not agent_run_id or request.session_id != f"web:{agent_run_id}":
        raise _web_detail_source_mismatch("Web 详情 Agent Run 与 Session 归属不一致")
    if binding.get("surface_status") not in {
        SurfaceSessionStatus.ACTIVE.value,
        SurfaceSessionStatus.DRAINING.value,
    }:
        raise _web_detail_source_mismatch("Web 详情 Surface 已不可执行")
    expected_values = {
        "capture_id": before_capture_id,
        "task_id": request.task_id,
        "surface_kind": "web",
        "session_id": request.session_id,
        "parent_capture_id": discovery_capture_id,
        "capture_role": "before_action",
        "capture_status": "captured",
        "parent_agent_run_id": agent_run_id,
        "parent_surface_kind": "web",
        "parent_session_id": request.session_id,
        "parent_capture_status": "captured",
        "action_task_id": request.task_id,
        "action_session_id": request.session_id,
        "action_requested_source_capture_id": discovery_capture_id,
        "action_source_capture_id": before_capture_id,
        "action_status": "executing",
    }
    mismatched_fields = [
        key for key, value in expected_values.items() if binding.get(key) != value
    ]
    # before_action 已经由动作、父 Capture、序列与 Epoch 完整绑定；命令持锁期间
    # Surface 游标可以仍指向发现 Capture，也可以已推进到 before_action Capture。
    if binding.get("surface_current_capture_id") not in {
        discovery_capture_id,
        before_capture_id,
    }:
        mismatched_fields.append("surface_current_capture_id")
    if mismatched_fields:
        raise _web_detail_source_mismatch(
            "Web 详情动作前 Capture 归属或动作身份已漂移："
            + ",".join(mismatched_fields)
        )
    if not _binding_text(binding, "action_execution_id"):
        raise _web_detail_source_mismatch("Web 详情缺少动作执行身份")

    action_intent = binding.get("action_intent")
    action_parameters = (
        action_intent.get("parameters") if isinstance(action_intent, Mapping) else None
    )
    if not isinstance(action_intent, Mapping) or not isinstance(action_parameters, Mapping):
        raise _web_detail_source_mismatch("Web 详情动作意图无效")
    expected_intent = {
        "task_id": request.task_id,
        "session_id": request.session_id,
        "source_capture_id": discovery_capture_id,
        "action_type": request.action_type.value,
        "semantic_target": request.semantic_target,
    }
    if any(action_intent.get(key) != value for key, value in expected_intent.items()):
        raise _web_detail_source_mismatch("Web 详情动作意图身份已漂移")
    bound_parameter_names = (
        "target_mode",
        "web_detail_candidate_id",
        "web_detail_source_capture_id",
        "web_detail_list_page_type",
        "web_detail_origin",
        "web_detail_current_url",
        "destination_url",
    )
    if any(
        action_parameters.get(name) != request.parameters.get(name)
        for name in bound_parameter_names
    ):
        raise _web_detail_source_mismatch("Web 详情动作参数身份已漂移")

    final_url = _binding_text(binding, "final_url")
    parent_final_url = _binding_text(binding, "parent_final_url")
    action_requested_url = _binding_text(binding, "action_requested_url")
    if action_requested_url != request.parameters.get("destination_url"):
        raise _web_detail_source_mismatch("Web 详情请求 URL 已漂移")
    before_url = _snapshot_web_url(before_snapshot, capability="Web 详情动作前")
    discovery_url = _snapshot_web_url(discovery_snapshot, capability="Web 详情发现")
    trusted_location = _normalized_web_location(trusted_source_url)
    if any(
        _normalized_web_location(value) != trusted_location
        for value in (before_url, discovery_url, final_url, parent_final_url)
    ):
        raise _web_detail_source_mismatch("Web 详情动作前页面路径或 Snapshot URL 已漂移")
    capture_requested_url = binding.get("capture_requested_url")
    if capture_requested_url is not None and (
        not isinstance(capture_requested_url, str)
        or _normalized_web_location(capture_requested_url) != trusted_location
    ):
        raise _web_detail_source_mismatch("Web 详情动作前 Capture 请求 URL 已漂移")
    if _web_origin_identity(action_requested_url) != _web_origin_identity(trusted_source_url):
        raise _web_detail_source_mismatch("Web 详情目标 URL 已跨域")


def _binding_text(binding: Mapping[str, Any], name: str) -> str:
    value = binding.get(name)
    return value if isinstance(value, str) and value.strip() == value else ""


def _web_auxiliary_source_mismatch(message: str) -> SemanticTargetResolutionError:
    return SemanticTargetResolutionError(
        message,
        error_code="WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH",
    )


def _web_detail_source_mismatch(message: str) -> SemanticTargetResolutionError:
    return SemanticTargetResolutionError(
        message,
        error_code="WEB_DETAIL_CANDIDATE_SOURCE_MISMATCH",
    )


def _snapshot_web_url(snapshot: Mapping[str, Any], *, capability: str) -> str:
    value = snapshot.get("url") or snapshot.get("page_url")
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise _web_detail_source_mismatch(f"{capability} Snapshot URL 无效")
    try:
        _validated_web_url(value)
    except SemanticTargetResolutionError:
        raise _web_detail_source_mismatch(f"{capability} Snapshot URL 无效") from None
    return value


def _normalized_web_location(value: str) -> tuple[str, str, int, str, str, str]:
    try:
        _validated_web_url(value)
        parsed = urlsplit(value)
    except SemanticTargetResolutionError:
        raise _web_detail_source_mismatch("Web 详情绑定 URL 无效") from None
    scheme = parsed.scheme.casefold()
    default_port = 443 if scheme == "https" else 80
    return (
        scheme,
        str(parsed.hostname).casefold(),
        parsed.port or default_port,
        parsed.path or "/",
        parsed.query,
        parsed.fragment,
    )


def _web_origin_identity(value: str) -> tuple[str, str, int]:
    scheme, hostname, port, *_ = _normalized_web_location(value)
    return scheme, hostname, port


def _optional_parameter(parameters: Mapping[str, Any], name: str) -> str | None:
    value = parameters.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SemanticTargetResolutionError(
            f"动作参数 {name} 必须为无首尾空白的非空字符串",
            error_code="INTERACTION_PARAMETER_INVALID",
        )
    return value


def _bind_detail_snapshot_capture(
    snapshot: Mapping[str, object] | object,
    *,
    expected_capture_id: str,
) -> dict[str, object]:
    """把可信仓储查询结果绑定到查询使用的 Capture，冲突时失败关闭。"""

    if not isinstance(snapshot, Mapping):
        raise _web_detail_source_mismatch("Web 详情 Snapshot 不存在或格式无效")
    if "capture_id" not in snapshot:
        return {**snapshot, "capture_id": expected_capture_id}
    stored_capture_id = snapshot["capture_id"]
    if not isinstance(stored_capture_id, str) or (stored_capture_id != expected_capture_id):
        raise SemanticTargetResolutionError(
            "Web 详情 Snapshot 与查询 Capture 不一致",
            error_code="WEB_DETAIL_SNAPSHOT_CAPTURE_MISMATCH",
        )
    return dict(snapshot)


def _valid_obstruction_signature(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("h5-obstruction:"):
        return False
    digest = value.removeprefix("h5-obstruction:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _normalized_visual_source_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SemanticTargetResolutionError(
            "视觉坐标来源 URL 无效",
            error_code="VISUAL_SOURCE_URL_INVALID",
        )
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SemanticTargetResolutionError(
            "视觉坐标来源 URL 必须是无凭据的 HTTP(S) 绝对地址",
            error_code="VISUAL_SOURCE_URL_INVALID",
        )
    try:
        _ = parsed.port
    except ValueError as exc:
        raise SemanticTargetResolutionError(
            "视觉坐标来源 URL 端口无效",
            error_code="VISUAL_SOURCE_URL_INVALID",
        ) from exc
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, parsed.query, "")
    )


def _visual_sha256_parameter(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise SemanticTargetResolutionError(
            "视觉坐标来源截图摘要无效",
            error_code="VISUAL_SOURCE_ARTIFACT_INVALID",
        )
    normalized = value.casefold()
    digest = normalized.removeprefix("sha256:")
    if (
        not normalized.startswith("sha256:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SemanticTargetResolutionError(
            "视觉坐标来源截图摘要无效",
            error_code="VISUAL_SOURCE_ARTIFACT_INVALID",
        )
    return f"sha256:{digest}"


def _visual_snapshot_state(snapshot: Mapping[str, Any]) -> tuple[object, ...]:
    """提取会影响全页比例坐标的几何状态；内容稳定性由目标区域像素证明。"""
    viewport = snapshot.get("viewport")
    document = snapshot.get("document")
    if not isinstance(viewport, Mapping) or not isinstance(document, Mapping):
        raise SemanticTargetResolutionError(
            "视觉坐标所需的 UI Snapshot 状态字段不完整",
            error_code="VISUAL_SOURCE_STATE_UNAVAILABLE",
            retryable=True,
        )
    viewport_state = tuple(
        _required_visual_state_number(viewport.get(name), name=name)
        for name in (
            "width",
            "height",
            "device_pixel_ratio",
            "scroll_x",
            "scroll_y",
        )
    )
    document_state = tuple(
        _required_visual_state_number(document.get(name), name=name)
        for name in ("scroll_width", "scroll_height")
    )
    fullscreen = document.get("fullscreen")
    if not isinstance(fullscreen, bool):
        raise SemanticTargetResolutionError(
            "视觉坐标所需的 UI Snapshot fullscreen 状态无效",
            error_code="VISUAL_SOURCE_STATE_UNAVAILABLE",
            retryable=True,
        )
    return (
        *viewport_state,
        *document_state,
        fullscreen,
    )


def _required_visual_state_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SemanticTargetResolutionError(
            f"视觉坐标所需的 UI Snapshot {name} 状态无效",
            error_code="VISUAL_SOURCE_STATE_UNAVAILABLE",
            retryable=True,
        )
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SemanticTargetResolutionError(
            f"视觉坐标所需的 UI Snapshot {name} 状态无效",
            error_code="VISUAL_SOURCE_STATE_UNAVAILABLE",
            retryable=True,
        )
    return number


def _visual_ratio_parameter(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        return None
    return number


def _visual_bounds_error() -> SemanticTargetResolutionError:
    return SemanticTargetResolutionError(
        "视觉坐标矩形、置信度或截图编号无效",
        error_code="VISUAL_BOUNDS_INVALID",
    )


def _validated_web_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SemanticTargetResolutionError(
            "destination_url 必须是无凭据的 HTTP(S) 绝对地址",
            error_code="NAVIGATION_TARGET_INVALID",
        )
    try:
        _ = parsed.port
    except ValueError as exc:
        raise SemanticTargetResolutionError(
            "destination_url 端口无效",
            error_code="NAVIGATION_TARGET_INVALID",
        ) from exc
    return value
