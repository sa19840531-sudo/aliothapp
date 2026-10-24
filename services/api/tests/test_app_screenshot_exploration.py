from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_capture_playbook import (
    resolve_app_capture_playbook,
)
from services.api.app.application.app_obstruction_controls import (
    _relative_vertical_band,
    build_app_obstruction_identity,
)
from services.api.app.application.app_screenshot_exploration import (
    AppBeforeActionVisualUnderstandingError,
    AppScreenshotExplorationCommand,
    AppScreenshotExplorationWorkflow,
    ContinueAppScreenshotExplorationCommand,
    ContinueAppScreenshotObservationCommand,
    _build_frontier,
    _coverage_attempted_action,
    _dedupe_frontier_candidates,
    _dynamic_connection_recovery_key,
    _enrich_package_boundary_analysis,
    _filter_dynamic_safety_candidates,
    _is_forbidden_dynamic_side_effect,
    _next_business_page_scope,
    _next_coverage_metadata,
    _next_popup_phase_counters,
    _next_startup_ad_phase_counters,
)
from services.api.app.application.app_script_curriculum import (
    AppCurriculumProgress,
    build_app_curriculum_progress,
)
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
)
from services.api.app.application.capture_coordination import CaptureCoordinationResult
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.surface_sessions import AppExplorationPendingFacts
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    AppVisualAnalysis,
    AppVisualCandidate,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationWork,
    NormalizedBounds,
    SideEffectPolicy,
    build_exploration_work,
)
from services.api.app.domain.exploration.app_visual_page_identity import build_app_visual_page_facts
from services.api.app.domain.interaction import (
    ActionIntent,
    AppReadyHandoff,
    CaptureDirective,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
    build_android_screenshot_only_manifest,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    SavedAppVisualObservation,
)
from services.api.tests.test_app_visual_page_identity import _observation as _identity_observation

NOW = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
SIGNATURE = "sha256:" + "a" * 64


def test_exploration_workflow_does_not_accept_inspection_publisher() -> None:
    """App 探索只产出视觉事实和 Work，不持有正式质检发布端口。"""

    parameters = inspect.signature(AppScreenshotExplorationWorkflow).parameters

    assert "inspection_publisher" not in parameters


def test_dynamic_frontier_rejects_ad_destination_but_keeps_explicit_close() -> None:
    banner = CandidateAction(
        candidate_id="candidate-banner-ad",
        semantic_target="顶部大横幅广告",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=88,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "进入横幅推广内容",
            "expected_state_hint": "content_detail",
            "frontier_scope": "page",
        },
    )
    close = CandidateAction(
        candidate_id="candidate-close-ad",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭推广弹窗后继续探索",
            "expected_state_hint": "close_popup_x",
            "frontier_scope": "local",
        },
    )

    assert _is_forbidden_dynamic_side_effect(banner) is True
    assert _is_forbidden_dynamic_side_effect(close) is False


def test_dynamic_frontier_rejects_disguised_dating_ad_content_card() -> None:
    """广告宫格即使伪装成 content_detail，也不能占用业务 Frontier。"""

    disguised_ad = CandidateAction(
        candidate_id="candidate-disguised-dating-ad",
        semantic_target="内容卡片全国约爱",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=88,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "打开内容详情",
            "expected_state_hint": "content_detail",
            "frontier_scope": "page",
        },
    )

    assert _is_forbidden_dynamic_side_effect(disguised_ad) is True


def _dynamic_safety_candidate(
    *,
    candidate_id: str,
    semantic_target: str,
    purpose: str,
    expected_state_hint: str,
    risk_level: InteractionRiskLevel,
    control_role: str | None,
) -> CandidateAction:
    """构造动态安全过滤所需的最小候选。"""

    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=risk_level,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": purpose,
            "expected_state_hint": expected_state_hint,
            "frontier_scope": "local",
            "control_role": control_role,
        },
    )


@pytest.mark.parametrize(
    (
        "semantic_target",
        "purpose",
        "expected_state_hint",
        "risk_level",
        "control_role",
        "expected_kept",
    ),
    (
        pytest.param(
            "关闭下载广告弹窗",
            "关闭当前下载广告弹窗并恢复主页",
            "close_popup_x",
            InteractionRiskLevel.LOW,
            "dismiss",
            True,
            id="dismiss-download-close",
        ),
        pytest.param(
            "关闭支付广告弹窗",
            "关闭当前支付广告弹窗并恢复主页",
            "close_popup_x",
            InteractionRiskLevel.LOW,
            "dismiss",
            True,
            id="dismiss-payment-close",
        ),
        pytest.param(
            "关闭购买广告弹窗",
            "关闭当前购买广告弹窗并恢复主页",
            "close_popup_x",
            InteractionRiskLevel.LOW,
            "dismiss",
            True,
            id="dismiss-purchase-close",
        ),
        pytest.param(
            "立即下载广告应用",
            "下载应用并安装",
            "content_detail",
            InteractionRiskLevel.LOW,
            None,
            False,
            id="download-side-effect",
        ),
        pytest.param(
            "购买会员",
            "购买并开通会员",
            "payment",
            InteractionRiskLevel.LOW,
            None,
            False,
            id="purchase-side-effect",
        ),
        pytest.param(
            "关闭下载广告弹窗",
            "关闭当前下载广告弹窗",
            "close_popup_x",
            InteractionRiskLevel.HIGH,
            "dismiss",
            False,
            id="high-risk-close",
        ),
    ),
)
def test_dynamic_safety_filter_respects_close_identity_and_risk(
    semantic_target: str,
    purpose: str,
    expected_state_hint: str,
    risk_level: InteractionRiskLevel,
    control_role: str | None,
    expected_kept: bool,
) -> None:
    """动态安全过滤区分带身份的关层与真实副作用／高风险动作。"""

    candidate = _dynamic_safety_candidate(
        candidate_id="candidate-dynamic-safety",
        semantic_target=semantic_target,
        purpose=purpose,
        expected_state_hint=expected_state_hint,
        risk_level=risk_level,
        control_role=control_role,
    )
    retained = _filter_dynamic_safety_candidates(
        candidates=(candidate,),
        analysis=_analysis_with_surface_facts(
            page_state="popup",
            overlay_signature="popup:ad",
            surface_facts={"popup_visible": True},
        ),
        coverage_metadata={},
    )

    assert retained == ((candidate,) if expected_kept else ())


def test_popup_frontier_prefers_first_ad_cta_before_body_and_close() -> None:
    """首个弹窗广告优先点击 CTA 取证，主体仅作为没有 CTA 时的回退。"""

    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            "弹窗广告主体：全国约爱",
            "点击广告并观察浏览器落地页",
            "external_browser",
        ),
        (
            InteractionOperation.CLICK,
            "关闭广告弹窗",
            "关闭推广弹窗后继续探索",
            "close_popup_x",
        ),
        (
            InteractionOperation.CLICK,
            "弹窗广告点击入口：今晚就约",
            "点击广告 CTA 并观察浏览器落地页",
            "external_browser",
        ),
        page_state="popup",
        visual_page_type="home",
        surface_facts={"topmost_ad_popup": {
            "is_ad": True, "confidence": 0.95,
            "normalized_bounds": {"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 0.9},
        }},
    )

    first = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )
    signed = first.candidates[0]
    assert signed.semantic_target == "弹窗广告点击入口：今晚就约"
    assert signed.metadata["app_ad_landing_consistency"] is True
    assert signed.metadata["frontier_scope"] == "local"
    assert dict(signed.metadata["action_parameters"]) == {
        "capture_ad_landing_evidence": True,
        "ad_landing_capability": "ad_outbound",
        "navigation_depth": 1,
        "app_ad_landing_contract": "app-ad-landing-consistency.v1",
    }
    # watcher_cascade：CTA 取证优先；几何安全 × 可留在后续候选。
    assert any(item.semantic_target == "关闭广告弹窗" for item in first.candidates)

    repeated = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"app_ad_landing_attempt_count": 1},
    )
    assert all(
        item.metadata.get("app_ad_landing_consistency") is not True
        for item in repeated.candidates
    )
    assert all(
        item.semantic_target != "弹窗广告主体：全国约爱"
        for item in repeated.candidates
    )
    assert all(
        item.semantic_target != "弹窗广告点击入口：今晚就约"
        for item in repeated.candidates
    )


def test_popup_ad_landing_deferred_when_coverage_incomplete_and_bottom_nav_visible() -> None:
    """覆盖缺口未完时，不得签发弹窗广告取证（即使已见底栏）。"""

    ad_cta = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="弹窗广告点击入口：今晚就约",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.30, 0.40, 0.70, 0.52),
        confidence=0.95,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="点击广告 CTA 并观察浏览器落地页",
        expected_state_hint="external_browser",
        frontier_scope="local",
    )
    bottom = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏「交友」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.40, 0.90, 0.60, 0.99),
        confidence=0.96,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开可见底栏导航「交友」",
        expected_state_hint="activity",
        frontier_scope="app",
        discovery_source="uia_issued",
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".MainActivity",
        visual_page_type="video_list",
        visual_summary="video_list popup",
        key_elements=("交友",),
        candidates=(ad_cta, bottom),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state="popup",
        overlay_signature="popup:video-ad",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.95,
                "normalized_bounds": {
                    "left": 0.1,
                    "top": 0.2,
                    "right": 0.9,
                    "bottom": 0.75,
                },
            },
            "trusted_accessibility_controls": {
                "bottom_navigation_count": 1,
                "bottom_navigation": (
                    {
                        "label": "交友",
                        "page_type": "activity",
                        "action_allowed": True,
                        "bounds": {
                            "left": 432,
                            "top": 1728,
                            "right": 648,
                            "bottom": 1900,
                        },
                    },
                ),
            },
        },
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "required_page_types": [
                "home",
                "content_list",
                "video_list",
                "post_list",
                "activity",
            ],
            "covered_page_types": ["video_list"],
            "dynamic_business_tab_overlay_frame_count": 1,
        },
    )
    assert all(
        item.metadata.get("app_ad_landing_consistency") is not True
        for item in frontier.candidates
    )
    assert any(
        item.semantic_target == "底栏「交友」"
        or item.metadata.get("bottom_navigation_tab_seek") is True
        for item in frontier.candidates
    )


def test_popup_ad_landing_deferred_when_coverage_incomplete_without_bottom_nav() -> None:
    """覆盖缺口未完时，即使当前帧未见底栏也不得签发广告取证，避免重复截广告。"""

    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            "弹窗广告点击入口：今晚就约",
            "点击广告 CTA 并观察浏览器落地页",
            "external_browser",
        ),
        (
            InteractionOperation.CLICK,
            "关闭广告弹窗",
            "关闭推广弹窗后继续探索",
            "close_popup_x",
        ),
        page_state="popup",
        visual_page_type="video_list",
        surface_facts={
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.95,
                "normalized_bounds": {
                    "left": 0.1,
                    "top": 0.2,
                    "right": 0.9,
                    "bottom": 0.9,
                },
            }
        },
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "required_page_types": [
                "home",
                "content_list",
                "video_list",
            ],
            "covered_page_types": [],
        },
    )
    assert all(
        item.metadata.get("app_ad_landing_consistency") is not True
        for item in frontier.candidates
    )
    # 覆盖缺口未完时不得签发广告取证；watcher_cascade 仍可保留关层 ×。
    assert any(item.semantic_target == "关闭广告弹窗" for item in frontier.candidates)


def test_business_tab_early_yield_keeps_bottom_nav_before_six_frames() -> None:
    """覆盖缺口未完时，未满 6 帧也保留真实底栏寻页。"""

    close = AppVisualCandidate.build(
        source_capture_id="capture-vl-popup-early",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="关闭弹层",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.45, 0.20, 0.55, 0.28),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭弹窗",
        expected_state_hint="close_popup_x",
        frontier_scope="local",
        control_role="dismiss",
    )
    bottom = AppVisualCandidate.build(
        source_capture_id="capture-vl-popup-early",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏「交友」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.40, 0.90, 0.60, 0.98),
        confidence=0.94,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开可见底栏导航「交友」",
        expected_state_hint="activity",
        frontier_scope="app",
        discovery_source="uia_issued",
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-vl-popup-early",
        screenshot_artifact_id="artifact-vl-popup-early",
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="uk.frtju.nnxsaf",
        activity=".MainActivity",
        visual_page_type="video_list",
        visual_summary="video_list",
        key_elements=(),
        candidates=(close, bottom),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state="popup",
        overlay_signature="popup:video-ad",
        surface_facts={"popup_visible": True},
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("uk.frtju.nnxsaf",),
        coverage_metadata={
            "required_page_types": [
                "home",
                "content_list",
                "video_list",
                "post_list",
                "activity",
            ],
            "covered_page_types": ["video_list"],
            "dynamic_business_tab_overlay_frame_count": 2,
        },
    )
    seeks = [
        candidate
        for candidate in frontier.candidates
        if candidate.metadata.get("bottom_navigation_tab_seek") is True
        or candidate.metadata.get("frontier_scope") == "app"
    ]
    assert seeks
    assert any("交友" in candidate.semantic_target for candidate in seeks)


@pytest.mark.asyncio
async def test_ad_landing_revalidation_consumes_schedule_without_counting_close_attempt() -> None:
    """动态广告取证过期后让位关闭；普通清障复验仍不算 Driver 尝试。"""

    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            "弹窗广告主体：轮播素材",
            "点击广告并观察浏览器落地页",
            "external_browser",
        ),
        (
            InteractionOperation.CLICK,
            "关闭广告弹窗",
            "关闭推广弹窗后继续探索",
            "close_popup_x",
        ),
        page_state="popup",
        visual_page_type="home",
        surface_facts={
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.95,
                "normalized_bounds": {
                    "left": 0.1,
                    "top": 0.2,
                    "right": 0.9,
                    "bottom": 0.9,
                },
            }
        },
    )
    # 覆盖分母已齐时才允许签发广告取证；本用例只验证调度一次计数。
    enriched = _enrich_package_boundary_analysis(
        analysis,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )
    frontier = _build_frontier(
        analysis=enriched,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "required_page_types": ["home"],
            "covered_page_types": ["home"],
        },
    )
    signed = frontier.candidates[0]
    assert signed.metadata.get("app_ad_landing_consistency") is True
    close = next(
        candidate
        for candidate in frontier.candidates
        if candidate.action_type is InteractionOperation.BACK
        and "底部导航栏" in candidate.semantic_target
    )
    work = (
        await AppScreenshotExplorationWorkflow(
            understanding=RecordingUnderstanding(analysis),
            observations=MemoryVisualObservations(),
            orchestrator=RecordingOrchestrator(),
        ).run(_command())
    ).work
    revalidation_turn = _action_turn(
        work,
        status=InteractionResultStatus.FAILED,
        error_code="APP_CANDIDATE_REVALIDATION_REQUIRED",
        retryable=True,
    )

    attempted = _coverage_attempted_action(
        candidate=signed,
        turn=revalidation_turn,
    )
    assert attempted is signed
    assert _next_coverage_metadata(
        goal=_coverage_goal(required_page_types=("home",), minimum_business_pages=1),
        analysis=analysis,
        prior_metadata={"covered_page_types": ["home"]},
        attempted_action=attempted,
    )["app_ad_landing_attempt_count"] == 1
    assert _coverage_attempted_action(
        candidate=close,
        turn=revalidation_turn,
    ) is None


def test_startup_ad_defers_landing_until_required_home_covered() -> None:
    """显式覆盖要求 home 且尚未命中时，开屏不得先外跳取证。"""

    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            "弹窗广告点击入口：今晚就约",
            "点击广告 CTA 并观察浏览器落地页",
            "external_browser",
        ),
        (
            InteractionOperation.CLICK,
            "跳过广告",
            "跳过启动广告进入应用主流程",
            "tap_skip",
        ),
        page_state="popup",
        visual_page_type="startup_ad",
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "required_page_types": [
                "home",
                "content_list",
                "video_list",
            ],
            "covered_page_types": [],
        },
    )
    assert all(
        item.metadata.get("app_ad_landing_consistency") is not True
        for item in frontier.candidates
    )
    assert all(
        "弹窗广告点击入口" not in item.semantic_target for item in frontier.candidates
    )


def test_popup_frontier_falls_back_to_ad_body_without_cta() -> None:
    """Provider 没有识别出 CTA 时仍允许对首个广告主体做一次有界取证。"""

    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            "弹窗广告主体：全国约爱",
            "点击广告并观察浏览器落地页",
            "external_browser",
        ),
        (
            InteractionOperation.CLICK,
            "关闭广告弹窗",
            "关闭推广弹窗后继续探索",
            "close_popup_x",
        ),
        page_state="popup",
        visual_page_type="home",
        surface_facts={"topmost_ad_popup": {
            "is_ad": True, "confidence": 0.95,
            "normalized_bounds": {"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 0.9},
        }},
    )

    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert frontier.candidates[0].semantic_target == "弹窗广告主体：全国约爱"
    assert frontier.candidates[0].metadata["app_ad_landing_consistency"] is True


def test_startup_ad_entry_is_not_signed_as_popup_ad_landing() -> None:
    """启动广告的“进入”是跳过控件，不得冒充首个弹窗广告落地入口。"""

    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            "弹窗广告点击入口：心动就点",
            "点击广告 CTA 并观察浏览器落地页",
            "external_browser",
        ),
        (
            InteractionOperation.CLICK,
            "跳过广告：进入",
            "点击目标包唯一可访问性开屏退出控件，跳过广告进入业务页",
            "tap_skip",
        ),
        page_state="startup_ad",
        visual_page_type="startup_ad",
        surface_facts={
            "trusted_accessibility_skip": {
                "artifact_id": "artifact-ui-tree",
                "package_name": "com.example.target",
                "label": "进入",
            }
        },
    )

    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert all(
        candidate.metadata.get("app_ad_landing_consistency") is not True
        for candidate in frontier.candidates
    )
    skip = next(
        candidate
        for candidate in frontier.candidates
        if candidate.semantic_target == "跳过广告：进入"
    )
    assert skip.metadata["expected_state_hint"] == "tap_skip"


def test_popup_phase_counts_same_structure_as_no_progress() -> None:
    counters = _next_popup_phase_counters(
        prior_metadata={},
        prior_overlay_signature="accessibility:popup-a",
        next_overlay_signature="accessibility:popup-a",
        action_succeeded=True,
    )

    assert counters == {
        "surface_phase_closed_popup_count": 0,
        "surface_phase_reobserve_count": 1,
        "surface_phase_same_popup_attempt_count": 1,
    }


def test_popup_phase_resets_reobserve_budget_after_structure_changes() -> None:
    counters = _next_popup_phase_counters(
        prior_metadata={
            "surface_phase_closed_popup_count": 2,
            "surface_phase_reobserve_count": 1,
            "surface_phase_same_popup_attempt_count": 1,
        },
        prior_overlay_signature="accessibility:popup-a",
        next_overlay_signature="accessibility:popup-b",
        action_succeeded=True,
    )

    assert counters == {
        "surface_phase_closed_popup_count": 3,
        "surface_phase_reobserve_count": 0,
        "surface_phase_same_popup_attempt_count": 0,
    }


def test_popup_phase_ignores_dynamic_overlay_text_when_structure_is_unchanged() -> None:
    """广告轮播改变素材签名时，同一遮挡结构仍必须累计无效果次数。"""

    counters = _next_popup_phase_counters(
        prior_metadata={"surface_phase_same_popup_attempt_count": 1},
        prior_overlay_signature="promo-copy-a",
        next_overlay_signature="promo-copy-b",
        prior_obstruction_identity="obstruction:stable-geometry",
        next_obstruction_identity="obstruction:stable-geometry",
        action_succeeded=True,
    )

    assert counters == {
        "surface_phase_closed_popup_count": 0,
        "surface_phase_reobserve_count": 1,
        "surface_phase_same_popup_attempt_count": 2,
    }


def test_popup_obstruction_identity_distinguishes_stacked_layout_but_tolerates_jitter() -> None:
    """不同弹层重置同层上限；同一层轻微框选抖动仍累计无效果次数。"""

    second_layer = _coverage_analysis(
        capture_id="capture-popup-second",
        artifact_id="artifact-popup-second",
        image_signature=SIGNATURE,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="关闭弹窗",
        candidate_purpose="关闭当前广告弹窗",
        candidate_control_role="dismiss",
        candidate_bounds=NormalizedBounds(0.43, 0.36, 0.57, 0.44),
        expected_state_hint="home",
        overlay_signature="dynamic-ad-copy-b",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.93,
                "normalized_bounds": {
                    "left": 0.06,
                    "top": 0.27,
                    "right": 0.94,
                    "bottom": 0.73,
                },
            },
        },
    )
    third_layer = _coverage_analysis(
        capture_id="capture-popup-third",
        artifact_id="artifact-popup-third",
        image_signature="sha256:" + "b" * 64,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="关闭弹窗按钮",
        candidate_purpose="关闭当前广告弹窗",
        candidate_control_role="dismiss",
        candidate_bounds=NormalizedBounds(0.43, 0.325, 0.57, 0.395),
        expected_state_hint="home",
        overlay_signature="dynamic-ad-copy-c",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.93,
                "normalized_bounds": {
                    "left": 0.08,
                    "top": 0.34,
                    "right": 0.92,
                    "bottom": 0.73,
                },
            },
        },
    )
    third_layer_jitter = _coverage_analysis(
        capture_id="capture-popup-third-jitter",
        artifact_id="artifact-popup-third-jitter",
        image_signature="sha256:" + "c" * 64,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="关闭弹窗",
        candidate_purpose="关闭当前广告弹窗",
        candidate_control_role="dismiss",
        candidate_bounds=NormalizedBounds(0.43, 0.31, 0.57, 0.38),
        expected_state_hint="home",
        overlay_signature="another-dynamic-ad-copy",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.93,
                "normalized_bounds": {
                    "left": 0.06,
                    "top": 0.33,
                    "right": 0.94,
                    "bottom": 0.72,
                },
            },
        },
    )

    second_identity = build_app_obstruction_identity(second_layer)
    third_identity = build_app_obstruction_identity(third_layer)
    jitter_identity = build_app_obstruction_identity(third_layer_jitter)
    assert second_identity is not None
    assert third_identity is not None
    assert second_identity != third_identity
    assert third_identity == jitter_identity

    changed = _next_popup_phase_counters(
        prior_metadata={"surface_phase_same_popup_attempt_count": 1},
        prior_overlay_signature=second_layer.overlay_signature,
        next_overlay_signature=third_layer.overlay_signature,
        prior_obstruction_identity=second_identity,
        next_obstruction_identity=third_identity,
        action_succeeded=True,
    )
    unchanged = _next_popup_phase_counters(
        prior_metadata=changed,
        prior_overlay_signature=third_layer.overlay_signature,
        next_overlay_signature=third_layer_jitter.overlay_signature,
        prior_obstruction_identity=third_identity,
        next_obstruction_identity=jitter_identity,
        action_succeeded=True,
    )
    assert changed["surface_phase_closed_popup_count"] == 1
    assert changed["surface_phase_same_popup_attempt_count"] == 0
    assert unchanged["surface_phase_closed_popup_count"] == 1
    assert unchanged["surface_phase_same_popup_attempt_count"] == 1


@pytest.mark.parametrize(
    ("control_bounds", "expected_band"),
    (
        pytest.param(
            NormalizedBounds(0.4, 0.201, 0.6, 0.209),
            0,
            id="top-edge-inside",
        ),
        pytest.param(
            NormalizedBounds(0.4, 0.191, 0.6, 0.199),
            0,
            id="top-edge-outside",
        ),
        pytest.param(
            NormalizedBounds(0.4, 0.791, 0.6, 0.799),
            5,
            id="bottom-edge-inside",
        ),
        pytest.param(
            NormalizedBounds(0.4, 0.801, 0.6, 0.809),
            5,
            id="bottom-edge-outside",
        ),
    ),
)
def test_relative_vertical_band_clamps_edge_jitter(
    control_bounds: NormalizedBounds,
    expected_band: int,
) -> None:
    """控件框选在弹窗边界内外轻微抖动时仍归入合法分段。"""

    popup_bounds = NormalizedBounds(0.1, 0.2, 0.9, 0.8)

    assert (
        _relative_vertical_band(control_bounds, popup_bounds=popup_bounds)
        == expected_band
    )


def test_popup_obstruction_identity_clamps_r18_boundary_drift_and_preserves_attempt_count() -> None:
    """R18 同一弹窗的顶部和底部框选漂移不能重置同层尝试次数。"""

    close_bounds = NormalizedBounds(
        0.48465277777777777,
        0.7022697368421053,
        0.5167361111111111,
        0.7174671052631579,
    )
    before = _coverage_analysis(
        capture_id="capture-r18-before",
        artifact_id="artifact-r18-before",
        image_signature="sha256:" + "a" * 64,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="关闭广告弹窗",
        candidate_purpose="关闭当前广告弹窗",
        candidate_control_role="dismiss",
        candidate_bounds=close_bounds,
        expected_state_hint="home",
        overlay_signature="r18-overlay-before",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.93,
                "normalized_bounds": {
                    "left": 0.06,
                    "top": 0.27,
                    "right": 0.94,
                    "bottom": 0.72,
                },
            },
        },
    )
    after = _coverage_analysis(
        capture_id="capture-r18-after",
        artifact_id="artifact-r18-after",
        image_signature="sha256:" + "b" * 64,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="关闭广告弹窗",
        candidate_purpose="关闭当前广告弹窗",
        candidate_control_role="dismiss",
        candidate_bounds=close_bounds,
        expected_state_hint="home",
        overlay_signature="r18-overlay-after",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.93,
                "normalized_bounds": {
                    "left": 0.067,
                    "top": 0.268,
                    "right": 0.933,
                    "bottom": 0.672,
                },
            },
        },
    )

    before_identity = build_app_obstruction_identity(before)
    after_identity = build_app_obstruction_identity(after)
    assert before_identity is not None
    assert after_identity == before_identity

    counters = _next_popup_phase_counters(
        prior_metadata={"surface_phase_same_popup_attempt_count": 1},
        prior_overlay_signature=before.overlay_signature,
        next_overlay_signature=after.overlay_signature,
        prior_obstruction_identity=before_identity,
        next_obstruction_identity=after_identity,
        action_succeeded=True,
    )

    assert counters == {
        "surface_phase_closed_popup_count": 0,
        "surface_phase_reobserve_count": 1,
        "surface_phase_same_popup_attempt_count": 2,
    }


def test_failed_popup_touch_counts_as_attempt_but_visual_revalidation_does_not() -> None:
    attempted = _next_popup_phase_counters(
        prior_metadata={},
        prior_overlay_signature="popup-a",
        next_overlay_signature="popup-a",
        action_succeeded=False,
        action_attempted=True,
    )
    rejected_before_driver = _next_popup_phase_counters(
        prior_metadata={},
        prior_overlay_signature="popup-a",
        next_overlay_signature="popup-a",
        action_succeeded=False,
        action_attempted=False,
    )

    assert attempted["surface_phase_same_popup_attempt_count"] == 1
    assert rejected_before_driver["surface_phase_same_popup_attempt_count"] == 0


def test_startup_ad_system_back_counts_as_close_attempt() -> None:
    counters = _next_startup_ad_phase_counters(
        prior_metadata={"surface_phase_close_attempt_count": 1},
        action_type=InteractionOperation.BACK,
        action_succeeded=True,
    )

    assert counters == {"surface_phase_close_attempt_count": 2}


def test_in_app_restoration_does_not_synthesize_back_from_navigation_depth() -> None:
    analysis = _enrich_package_boundary_analysis(
        _analysis(),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )

    root = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        navigation_depth=0,
        allowed_hosts=("com.example.target",),
    )
    deeply_nested = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        navigation_depth=5,
        allowed_hosts=("com.example.target",),
    )

    assert all(
        candidate.action_type is not InteractionOperation.BACK for candidate in root.candidates
    )
    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in deeply_nested.candidates
    )


def test_stable_deep_page_enrichment_adds_low_priority_coordinate_free_back() -> None:
    analysis = _enrich_package_boundary_analysis(
        _analysis_with_candidates(
            page_state="stable",
            visual_page_type="content_detail",
        ),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )

    back = next(
        candidate
        for candidate in analysis.candidates
        if candidate.action_type is InteractionOperation.BACK
    )

    assert back.normalized_bounds is None
    assert back.semantic_name == "系统 BACK 返回上一级"
    assert back.confidence == 0.01
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        navigation_depth=3,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )
    frontier_back = next(
        candidate
        for candidate in frontier.candidates
        if candidate.action_type is InteractionOperation.BACK
    )
    assert frontier_back.candidate_id == back.candidate_id
    assert frontier_back.metadata["frontier_scope"] == "local"


def test_dynamic_paywall_enrichment_does_not_synthesize_or_strip_candidates() -> None:
    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            "关闭会员层",
            "关闭当前会员解锁层",
            "close_popup_x",
        ),
        page_state="stable",
        visual_page_type="payment",
        surface_facts={"dialog_kind": "paywall"},
    )

    enriched = _enrich_package_boundary_analysis(
        analysis,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
        dynamic_frontier=True,
    )

    assert enriched is analysis
    assert [candidate.semantic_name for candidate in enriched.candidates] == ["关闭会员层"]
    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in enriched.candidates
    )

    empty_paywall = _analysis_with_candidates(
        page_state="stable",
        visual_page_type="payment",
        surface_facts={"dialog_kind": "paywall"},
    )
    empty_enriched = _enrich_package_boundary_analysis(
        empty_paywall,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
        dynamic_frontier=True,
    )
    assert empty_enriched is empty_paywall
    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in empty_enriched.candidates
    )


def test_dynamic_paywall_frontier_preserves_real_back_without_special_metadata() -> None:
    analysis = _analysis_with_candidates(
        (
            InteractionOperation.BACK,
            "系统返回键",
            "返回上一页",
            "back",
        ),
        page_state="stable",
        visual_page_type="payment",
        surface_facts={"dialog_kind": "paywall"},
    )
    enriched = _enrich_package_boundary_analysis(
        analysis,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
        dynamic_frontier=True,
    )
    assert enriched is analysis

    frontier = _build_frontier(
        analysis=enriched,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "app_runtime_policy": {
                "exploration_mode": "dynamic_frontier",
                "quantity_unbounded": True,
            },
        },
    )

    back = next(
        candidate
        for candidate in frontier.candidates
        if candidate.action_type is InteractionOperation.BACK
    )
    assert back.candidate_id == analysis.candidates[0].candidate_id
    assert back.semantic_target == "系统返回键"
    assert back.metadata.get("payment_sheet_back") is None
    assert back.metadata.get("popup_back_recovery") is None
    assert back.metadata["expected_state_hint"] == "back"


def test_non_dynamic_paywall_keeps_paywall_back_contract() -> None:
    analysis = _analysis_with_candidates(
        page_state="stable",
        visual_page_type="payment",
        surface_facts={"dialog_kind": "paywall"},
    )

    enriched = _enrich_package_boundary_analysis(
        analysis,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )
    frontier = _build_frontier(
        analysis=enriched,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    back = next(
        candidate
        for candidate in frontier.candidates
        if candidate.action_type is InteractionOperation.BACK
    )
    assert back.semantic_target == "返回关闭付费半屏"
    assert back.metadata["payment_sheet_back"] is True
    assert back.metadata["popup_back_recovery"] is True
    assert back.metadata["expected_state_hint"] == "home"


@pytest.mark.asyncio
async def test_before_capture_paywall_revalidation_does_not_infer_runtime_policy() -> None:
    paywall = _analysis_with_candidates(
        page_state="stable",
        visual_page_type="payment",
        surface_facts={
            "dialog_kind": "paywall",
            "app_runtime_policy": {"exploration_mode": "legacy"},
        },
    )
    before_paywall = _coverage_analysis(
        capture_id="capture-before",
        artifact_id="artifact-before",
        image_signature="sha256:" + "b" * 64,
        visual_page_type="payment",
        page_state="stable",
        surface_facts={"dialog_kind": "paywall"},
    )
    observations = MemoryVisualObservations()
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            paywall,
            analyses_by_capture={"capture-before": before_paywall},
        ),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    )

    before_capture = replace(
        _capture(),
        capture_id="capture-before",
        role="before_action",
        artifact_ids=("artifact-before",),
    )
    saved = await workflow.prepare_before_action_visual(
        capture=before_capture,
        source=paywall,
    )

    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in saved.analysis.candidates
    )


def test_line_loading_enrichment_does_not_synthesize_back_without_history() -> None:
    analysis = _enrich_package_boundary_analysis(
        _analysis_with_candidates(
            page_state="loading",
            visual_page_type="line_loading",
        ),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )

    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in analysis.candidates
    )


def test_line_loading_provider_back_does_not_create_platform_back() -> None:
    analysis = _enrich_package_boundary_analysis(
        _analysis_with_candidates(
            (
                InteractionOperation.BACK,
                "system_back",
                "系统返回",
                None,
            ),
            page_state="loading",
            visual_page_type="line_loading",
        ),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )

    assert all(
        candidate.semantic_name != "系统 BACK 返回上一级"
        for candidate in analysis.candidates
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        navigation_depth=6,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in frontier.candidates
    )


@pytest.mark.parametrize(
    ("visual_page_type", "page_state", "overlay_signature"),
    [
        ("home", "loading", None),
        ("unknown", "loading", None),
        ("line_loading", "loading", "popup:loading"),
    ],
)
def test_line_loading_back_enrichment_remains_fail_closed_for_unsafe_frames(
    visual_page_type: str,
    page_state: str,
    overlay_signature: str | None,
) -> None:
    analysis = _enrich_package_boundary_analysis(
        _analysis_with_candidates(
            page_state=page_state,
            visual_page_type=visual_page_type,
            overlay_signature=overlay_signature,
        ),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )

    assert all(
        candidate.semantic_name != "系统 BACK 返回上一级" for candidate in analysis.candidates
    )


def test_startup_ad_enrichment_does_not_synthesize_root_back() -> None:
    analysis = _enrich_package_boundary_analysis(
        _analysis_with_candidates(
            page_state="startup_ad",
            visual_page_type="startup_ad",
        ),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )

    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in analysis.candidates
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )
    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in frontier.candidates
    )


def test_obstructed_frontier_keeps_only_close_and_recovery_actions() -> None:
    analysis = _enrich_package_boundary_analysis(
        _analysis_with_candidates(
            (
                InteractionOperation.CLICK,
                "关闭弹层：下次再说",
                "关闭当前广告弹层",
                "close_popup_x",
            ),
            (
                InteractionOperation.CLICK,
                "底栏「视频」",
                "打开底栏视频并探索",
                "video_list",
            ),
            page_state="popup",
            visual_page_type="home",
        ),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )

    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    # watcher_cascade：保留 A 文案关层 + D 底栏「<」；过滤穿透底栏寻页。
    assert {item.semantic_target for item in frontier.candidates} == {
        "关闭弹层：下次再说",
        "底部导航栏「<」关闭弹窗",
    }


class RecordingUnderstanding:
    def __init__(
        self,
        analysis: AppVisualAnalysis,
        *,
        errors_by_capture: dict[str, Exception] | None = None,
        signatures_by_capture: dict[str, str] | None = None,
        analyses_by_capture: dict[str, AppVisualAnalysis] | None = None,
    ) -> None:
        self.analysis = analysis
        self.errors_by_capture = errors_by_capture or {}
        self.signatures_by_capture = signatures_by_capture or {}
        self.analyses_by_capture = analyses_by_capture or {}
        self.captures: list[CaptureRef] = []

    def _signature_for(self, capture: CaptureRef) -> str:
        if capture.capture_id in self.analyses_by_capture:
            return self.analyses_by_capture[capture.capture_id].image_signature
        if capture.capture_id in self.signatures_by_capture:
            return self.signatures_by_capture[capture.capture_id]
        if capture.capture_id == self.analysis.capture_id:
            return self.analysis.image_signature
        # 默认与主分析区分，避免测试无意命中 after 内容寻址复用
        return "sha256:" + hashlib.sha256(capture.capture_id.encode("utf-8")).hexdigest()

    async def resolve_capture_image_signature(self, capture: CaptureRef) -> str:
        return self._signature_for(capture)

    async def understand(self, capture: CaptureRef) -> AppVisualAnalysis:
        self.captures.append(capture)
        error = self.errors_by_capture.get(capture.capture_id)
        if error is not None:
            raise error
        if capture.capture_id in self.analyses_by_capture:
            return self.analyses_by_capture[capture.capture_id]
        if capture.capture_id == self.analysis.capture_id:
            return self.analysis
        return AppVisualAnalysis.build(
            capture_id=capture.capture_id,
            screenshot_artifact_id=capture.artifact_ids[0],
            image_signature=self._signature_for(capture),
            width=self.analysis.width,
            height=self.analysis.height,
            orientation=self.analysis.orientation,
            package_name=self.analysis.package_name,
            activity=self.analysis.activity,
            visual_page_type=self.analysis.visual_page_type,
            visual_summary=self.analysis.visual_summary,
            key_elements=self.analysis.key_elements,
            candidates=(),
            provider_id=self.analysis.provider_id,
            model_id=self.analysis.model_id,
            page_state=self.analysis.page_state,
            overlay_signature=self.analysis.overlay_signature,
        )


def _confirmed_fixture(analysis: AppVisualAnalysis, *, object_id: str | None = None):
    """阳性到达用例通过正式身份映射构造当前截图证据，不能只改页型。"""
    facts = build_app_visual_page_facts(
        identity_observation=_identity_observation(object_id=object_id),
        local_state_observation=None, capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature, package_name=analysis.package_name,
        activity=analysis.activity, page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature, surface_facts=analysis.surface_facts,
    )
    values = {
        item.name: getattr(analysis, item.name) for item in fields(analysis)
        if item.name not in {"result_hash", "contract_version", "surface_facts"}
    }
    # 导航结算四件套：单元测试无真 UI Tree／epoch 时补齐。
    # before／after 共用同一 epoch 字符串，避免 settle 把「新截图」误判为 epoch_mismatch。
    epoch = "epoch:confirmed-fixture"
    return AppVisualAnalysis.build(**values, surface_facts={
        **dict(analysis.surface_facts),
        **facts,
        "visual_page_type_confidence": 0.94,
        "has_ui_tree": True,
        "ui_tree_artifact_id": f"ui-tree:{analysis.capture_id}",
        "current_window": "com.example.target/main",
        "capture_epoch": epoch,
        "finalization_epoch": epoch,
    })


def _confirmed_before():
    return _confirmed_fixture(_coverage_analysis(
        capture_id="capture-before", artifact_id="artifact-before",
        image_signature="sha256:" + "c" * 64, visual_page_type="home",
    ))


class MemoryVisualObservations:
    def __init__(
        self,
        *,
        observed_at_by_capture: dict[str, datetime] | None = None,
    ) -> None:
        self.saved: dict[str, SavedAppVisualObservation] = {}
        self.observed_at_by_capture = observed_at_by_capture or {
            "capture-1": NOW,
            "capture-before": NOW + timedelta(milliseconds=20),
            "capture-after": NOW + timedelta(milliseconds=25),
        }

    async def get_by_capture_id(
        self,
        *,
        capture_id: str,
    ) -> SavedAppVisualObservation | None:
        return self.saved.get(capture_id)

    async def save(self, analysis: AppVisualAnalysis) -> SavedAppVisualObservation:
        existing = self.saved.get(analysis.capture_id)
        if existing is not None:
            return existing
        saved = SavedAppVisualObservation(
            observation_id="visual-observation-1",
            analysis=analysis,
            observed_at=self.observed_at_by_capture.get(analysis.capture_id, NOW),
            created=True,
        )
        self.saved[analysis.capture_id] = SavedAppVisualObservation(
            observation_id=saved.observation_id,
            analysis=analysis,
            observed_at=saved.observed_at,
            created=False,
        )
        return saved


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.commands = []
        self.prepared_commands = []

    async def start_exploration(self, command):
        self.commands.append(command)
        return self._build(command)

    async def prepare_exploration(self, command):
        self.prepared_commands.append(command)
        return self._build(command)

    @staticmethod
    def _build(command):
        return build_exploration_work(
            task_id=command.task_id,
            session_id=command.session_id,
            branch_id=command.branch_id,
            objective=command.objective,
            surface_kind=command.surface_kind,
            source_capture_id=command.source_capture_id,
            frontier_snapshot_id=command.frontier_snapshot_id,
            frontier=command.frontier,
            boundaries=command.boundaries,
            budget=command.budget,
            deadline=command.deadline,
            app_goal=command.app_goal,
        )


class RecordingPendingFacts:
    def __init__(self, *, pending_actions: int, pending_evidence: int) -> None:
        self.pending_actions = pending_actions
        self.pending_evidence = pending_evidence
        self.calls: list[tuple[str, str]] = []

    async def exploration_pending_facts(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> AppExplorationPendingFacts:
        self.calls.append((task_id, session_id))
        return AppExplorationPendingFacts(
            task_id=task_id,
            session_id=session_id,
            pending_action_count=self.pending_actions,
            pending_evidence_count=self.pending_evidence,
        )


def _capture() -> CaptureRef:
    return CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="bootstrap",
        artifact_ids=("artifact-1",),
    )


def _handoff() -> AppReadyHandoff:
    return AppReadyHandoff(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="session-1",
        installed_package="com.example.target",
        capture=_capture(),
        capability_manifest=build_android_screenshot_only_manifest(
            adapter_id="fake-app-runtime",
            adapter_version="1.0.0",
            backend="fake",
        ),
        bootstrap_row_version=7,
    )


def _analysis(
    *,
    action_type: InteractionOperation = InteractionOperation.CLICK,
    semantic_name: str = "登录按钮",
    purpose: str = "进入系统",
    expected_state_hint: str | None = "首页",
    control_role: str | None = None,
) -> AppVisualAnalysis:
    candidate = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=action_type,
        normalized_bounds=NormalizedBounds(0.2, 0.7, 0.8, 0.8),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role=control_role,
        expected_state_hint=expected_state_hint,
    )
    return AppVisualAnalysis.build(
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".MainActivity",
        visual_page_type="login",
        visual_summary="登录页",
        key_elements=("登录按钮",),
        candidates=(candidate,),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
    )


def _analysis_with_candidates(
    *candidates: tuple[
        InteractionOperation,
        str,
        str,
        str | None,
    ],
    page_state: str = "stable",
    visual_page_type: str = "home",
    overlay_signature: str | None = None,
    surface_facts: dict[str, object] | None = None,
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW,
) -> AppVisualAnalysis:
    """构造同一截图上的多候选，供课纲 Frontier 收口回归使用。"""

    visual_candidates = tuple(
        AppVisualCandidate.build(
            source_capture_id="capture-1",
            source_image_signature=SIGNATURE,
            source_width=1080,
            source_height=1920,
            source_orientation="portrait",
            semantic_name=semantic_name,
            action_type=action_type,
            normalized_bounds=NormalizedBounds(0.2, 0.7, 0.8, 0.8),
            confidence=0.95,
            risk_level=risk_level,
            purpose=purpose,
            expected_state_hint=expected_state_hint,
        )
        for action_type, semantic_name, purpose, expected_state_hint in candidates
    )
    return AppVisualAnalysis.build(
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".MainActivity",
        visual_page_type=visual_page_type,
        visual_summary=f"{visual_page_type} 状态",
        key_elements=tuple(item[1] for item in candidates),
        candidates=visual_candidates,
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state=page_state,
        overlay_signature=(
            overlay_signature
            if overlay_signature is not None
            else ("overlay-1" if page_state in {"popup", "modal"} else None)
        ),
        surface_facts=surface_facts or {},
    )


def _external_package_analysis(
    *,
    semantic_name: str = "登录按钮",
    purpose: str = "进入系统",
    visual_page_type: str = "login",
    page_state: str | None = None,
    surface_facts: dict[str, object] | None = None,
) -> AppVisualAnalysis:
    analysis = _analysis(
        semantic_name=semantic_name,
        purpose=purpose,
    )
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        package_name="com.android.chrome",
        activity="org.chromium.chrome.browser.ChromeTabbedActivity",
        visual_page_type=visual_page_type,
        visual_summary="Chrome 页面",
        key_elements=analysis.key_elements,
        candidates=analysis.candidates,
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        page_state=page_state or analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=surface_facts or analysis.surface_facts,
    )


def _analysis_with_surface_facts(
    *,
    page_state: str,
    overlay_signature: str | None = None,
    surface_facts: dict[str, object] | None = None,
) -> AppVisualAnalysis:
    candidate = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="关闭弹窗",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.2, 0.7, 0.8, 0.8),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭遮挡层",
    )
    return AppVisualAnalysis.build(
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".MainActivity",
        visual_page_type="home",
        visual_summary="首页状态",
        key_elements=("关闭按钮",),
        candidates=(candidate,),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state=page_state,
        overlay_signature=overlay_signature,
        surface_facts=surface_facts or {"source": "screenshot", "popup_visible": True},
    )


def _command(
    *,
    app_goal: AppExplorationGoal | None = None,
    curriculum_progress: AppCurriculumProgress | None = None,
) -> AppScreenshotExplorationCommand:
    return AppScreenshotExplorationCommand(
        handoff=_handoff(),
        objective="探索登录后的关键页面",
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.target",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.SCROLL,
                InteractionOperation.BACK,
                InteractionOperation.OBSERVE,
            ),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=5,
            remaining_actions=5,
            max_depth=3,
            remaining_duration_ms=30_000,
        ),
        observed_at=NOW,
        deadline=NOW + timedelta(minutes=10),
        app_goal=app_goal
        or AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("journey:login",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        curriculum_progress=curriculum_progress,
        runtime_policy_summary=(
            {
                "protocol_version": "app-exploration-runtime-policy.v1",
                "scan_mode": "full",
                "budget_source": "agent_run.loop_guard",
            }
            if curriculum_progress is not None
            else None
        ),
    )


def _progress_at_phase(phase_id: str) -> AppCurriculumProgress:
    progress = build_app_curriculum_progress(
        resolve_app_capture_playbook(apk_filename="unknown.apk")
    )
    while progress.current_phase_id != phase_id:
        if progress.current_phase_id is None:
            raise AssertionError(f"无法定位课纲阶段：{phase_id}")
        for _ in range(progress.current_phase_required_actions):
            progress = progress.advance(
                phase_id=progress.current_phase_id,
                action_succeeded=True,
            )
    return progress


def _coverage_goal(
    *,
    required_page_types: tuple[str, ...] = ("home", "content_detail"),
    required_milestones: tuple[str, ...] = ("surface:stable", "journey:details"),
    minimum_business_pages: int = 2,
) -> AppExplorationGoal:
    return AppExplorationGoal(
        required_page_types=required_page_types,
        required_milestones=required_milestones,
        minimum_business_pages=minimum_business_pages,
        allowed_packages=("com.example.target",),
    )


def _coverage_analysis(
    *,
    capture_id: str,
    artifact_id: str,
    image_signature: str,
    visual_page_type: str | None,
    page_state: str = "stable",
    visual_summary: str | None = None,
    key_elements: tuple[str, ...] = (),
    candidate_semantic_name: str | None = None,
    candidate_action_type: InteractionOperation = InteractionOperation.CLICK,
    candidate_purpose: str = "open details",
    candidate_control_role: str | None = None,
    candidate_frontier_scope: str = "local",
    candidate_bounds: NormalizedBounds | None = None,
    expected_state_hint: str | None = "details",
    surface_facts: dict[str, object] | None = None,
    package_name: str = "com.example.target",
    overlay_signature: str | None = None,
) -> AppVisualAnalysis:
    candidates: tuple[AppVisualCandidate, ...] = ()
    if candidate_semantic_name is not None:
        candidates = (
            AppVisualCandidate.build(
                source_capture_id=capture_id,
                source_image_signature=image_signature,
                source_width=1080,
                source_height=1920,
                source_orientation="portrait",
                semantic_name=candidate_semantic_name,
                action_type=candidate_action_type,
                normalized_bounds=candidate_bounds
                or NormalizedBounds(0.2, 0.7, 0.8, 0.8),
                confidence=0.95,
                risk_level=InteractionRiskLevel.LOW,
                purpose=candidate_purpose,
                control_role=candidate_control_role,
                frontier_scope=candidate_frontier_scope,
                expected_state_hint=expected_state_hint,
            ),
        )
    return AppVisualAnalysis.build(
        capture_id=capture_id,
        screenshot_artifact_id=artifact_id,
        image_signature=image_signature,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name=package_name,
        activity=".MainActivity",
        visual_page_type=visual_page_type,
        visual_summary=visual_summary or visual_page_type or "unknown",
        key_elements=key_elements,
        candidates=candidates,
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state=page_state,
        overlay_signature=overlay_signature,
        surface_facts=surface_facts or {},
    )


def _coverage_popup_close_action() -> CandidateAction:
    """构造不应消耗业务停滞预算的弹层关闭动作。"""

    return CandidateAction(
        candidate_id="close-popup",
        semantic_target="关闭弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭当前弹窗",
            "expected_state_hint": "close_popup_x",
            "frontier_scope": "local",
            "control_role": "dismiss",
        },
    )


def test_coverage_counts_page_name_type_groups_without_weakening_page_type_gate() -> None:
    goal = _coverage_goal(required_page_types=("profile",), minimum_business_pages=2)
    profile = _coverage_analysis(
        capture_id="capture-profile",
        artifact_id="artifact-profile",
        image_signature="sha256:" + "b" * 64,
        visual_page_type="profile",
        surface_facts={
            "visual_page_name": {
                "protocol_version": "app-visual-page-name.v1",
                "page_name": "个人中心",
                "source": "top_bar",
                "confidence": 0.96,
            }
        },
    )
    same_profile = _coverage_analysis(
        capture_id="capture-profile-restored",
        artifact_id="artifact-profile-restored",
        image_signature="sha256:" + "c" * 64,
        visual_page_type="profile",
        surface_facts=profile.surface_facts,
    )
    invitation = _coverage_analysis(
        capture_id="capture-invitation",
        artifact_id="artifact-invitation",
        image_signature="sha256:" + "d" * 64,
        visual_page_type="profile",
        surface_facts={
            "visual_page_name": {
                "protocol_version": "app-visual-page-name.v1",
                "page_name": "分享邀请",
                "source": "page_landmark",
                "confidence": 0.92,
            }
        },
    )

    first = _next_coverage_metadata(
        goal=goal,
        analysis=profile,
        business_navigation_depth=0,
        business_page_scope="nav:profile",
    )
    repeated = _next_coverage_metadata(
        goal=goal,
        analysis=same_profile,
        prior_metadata=first,
        business_navigation_depth=3,
        business_page_scope="nav:restored-profile",
    )
    distinct = _next_coverage_metadata(
        goal=goal,
        analysis=invitation,
        prior_metadata=repeated,
        business_navigation_depth=1,
        business_page_scope="depth:1",
    )

    assert repeated["covered_business_page_count"] == 1
    assert repeated["covered_business_pages"][0]["capture_count"] == 2
    assert distinct["covered_business_page_count"] == 2
    assert distinct["covered_page_types"] == ["profile"]
    assert distinct["covered_page_type_count"] == 1
    assert [item["page_display_name"] for item in distinct["covered_business_pages"]] == [
        "个人中心-profile",
        "分享邀请-profile",
    ]


@pytest.mark.asyncio
async def test_replayed_handoff_reuses_visual_fact_and_work() -> None:
    understanding = RecordingUnderstanding(_analysis())
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    )

    first = await workflow.run(_command())
    second = await workflow.run(_command())

    assert second.observation_id == first.observation_id
    assert second.work.work_id == first.work.work_id
    assert len(understanding.captures) == 1


@pytest.mark.asyncio
async def test_workflow_maps_visual_facts_without_leaking_bounds_to_agent_work() -> None:
    orchestrator = RecordingOrchestrator()
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=orchestrator,
    )

    result = await workflow.run(_command())

    observation = result.work.frontier.observation
    candidate = result.work.frontier.candidates[0]
    assert observation.location == "app://com.example.target/.MainActivity"
    assert observation.state_version == SIGNATURE
    assert observation.metadata["screenshot_artifact_id"] == "artifact-1"
    assert candidate.candidate_id.startswith("candidate:")
    assert candidate.semantic_target == "登录按钮"
    assert "normalized_bounds" not in candidate.metadata
    # 入口 provenance 账本字段可随导航合同演进；仍禁止把视觉 bounds 泄漏给 Agent。
    assert candidate.metadata["candidate_id"] == candidate.candidate_id
    assert candidate.metadata["source_capture_id"] == "capture-1"
    assert candidate.metadata["purpose"] == "进入系统"
    assert candidate.metadata["frontier_scope"] == "local"
    assert candidate.metadata["expected_state_hint"] == "首页"
    assert candidate.metadata["expected_next_page_type"] == "home"
    assert orchestrator.commands[0].branch_id == "app:capture-1"


@pytest.mark.asyncio
async def test_app_scheduler_state_survives_multi_round_failure_and_json_restore() -> None:
    """真实 App 后继链必须累计动作/失败账本，不能被新 metadata 覆盖。"""

    class MultiRoundUnderstanding(RecordingUnderstanding):
        async def understand(self, capture: CaptureRef) -> AppVisualAnalysis:
            if capture.capture_id == self.analysis.capture_id:
                return await super().understand(capture)
            self.captures.append(capture)
            signature = self._signature_for(capture)
            candidates = tuple(
                AppVisualCandidate.build(
                    source_capture_id=capture.capture_id,
                    source_image_signature=signature,
                    source_width=item.source_width,
                    source_height=item.source_height,
                    source_orientation=item.source_orientation,
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
                for item in self.analysis.candidates
            )
            return AppVisualAnalysis.build(
                capture_id=capture.capture_id,
                screenshot_artifact_id=capture.artifact_ids[0],
                image_signature=signature,
                width=self.analysis.width,
                height=self.analysis.height,
                orientation=self.analysis.orientation,
                package_name=self.analysis.package_name,
                activity=self.analysis.activity,
                visual_page_type=self.analysis.visual_page_type,
                visual_summary=self.analysis.visual_summary,
                key_elements=self.analysis.key_elements,
                candidates=candidates,
                provider_id=self.analysis.provider_id,
                model_id=self.analysis.model_id,
                page_state=self.analysis.page_state,
                overlay_signature=self.analysis.overlay_signature,
                surface_facts=self.analysis.surface_facts,
            )

    understanding = MultiRoundUnderstanding(_analysis())
    observations = MemoryVisualObservations()
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    )

    def scheduler_state(work):
        value = work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]
        assert isinstance(value, Mapping)
        return dict(value)

    initial = await workflow.run(_command())
    assert not scheduler_state(initial.work)["consumed_actions"]

    first = await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(
                initial.work,
                action_execution_id="action-app-1",
                after_capture_id="capture-app-after-1",
                after_artifact_id="artifact-app-after-1",
                status=InteractionResultStatus.SUCCEEDED,
            ),
        )
    )
    first_state = scheduler_state(first.work)
    assert sum(first_state["consumed_actions"].values()) == 1
    assert first.work.frontier.candidates

    # 用 JSON 形态重建 Frontier metadata，模拟跨进程/持久化恢复。
    restored_metadata = dict(first.work.frontier.observation.metadata)
    restored_metadata[BOUNDED_EXPLORATION_STATE_KEY] = json.loads(
        json.dumps(
            first_state,
            default=lambda value: dict(value) if isinstance(value, Mapping) else value,
        )
    )
    restored_work = replace(
        first.work,
        frontier=replace(
            first.work.frontier,
            observation=replace(
                first.work.frontier.observation,
                metadata=restored_metadata,
            ),
        ),
    )

    second = await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=restored_work,
            turn=_action_turn(
                restored_work,
                action_execution_id="action-app-2",
                before_capture_id="capture-app-before-2",
                after_capture_id="capture-app-after-2",
                after_artifact_id="artifact-app-after-2",
                status=InteractionResultStatus.FAILED,
                error_code="driver_failed",
                retryable=True,
            ),
        )
    )
    second_state = scheduler_state(second.work)
    assert sum(second_state["consumed_actions"].values()) == 2
    assert sum(second_state["retry_attempts"].values()) == 1


@pytest.mark.asyncio
async def test_app_workflow_reads_persisted_gap_facts_on_initial_and_successor_rounds() -> None:
    """App 正式工作流应把 Evidence Index/规则缺口带入连续调度轮次。"""

    class SchedulerFactsReader:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def read_app_facts(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {
                "exploration_scheduler_facts_scope": {
                    "task_id": kwargs["task_id"],
                    "session_id": kwargs["session_id"],
                    "finalization_epoch": 0,
                    "source": "test-pg",
                },
                "app_evidence_index": {
                    "source": "qc_app_evidence_index",
                    "evidence_gaps": [{"page_type": "home"}],
                },
                "rule_evidence_requests": [
                    {
                        "source": "qc_evidence_request",
                        "target_capture_id": "capture-home-target",
                        "evidence_gap": {"page_types": ["home"]},
                    }
                ],
            }

    reader = SchedulerFactsReader()
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
        scheduler_facts_reader=reader,
    )

    initial = await workflow.run(_command())
    initial_metadata = initial.work.frontier.observation.metadata
    initial_state = initial_metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert initial_metadata["exploration_scheduler_facts_scope"]["source"] == "test-pg"
    assert initial_metadata["rule_evidence_requests"][0]["target_capture_id"] == (
        "capture-home-target"
    )
    assert "home" in initial_state["evidence_gap_page_types"]

    successor = await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(
                initial.work,
                action_execution_id="action-facts-1",
                after_capture_id="capture-facts-after-1",
                after_artifact_id="artifact-facts-after-1",
                status=InteractionResultStatus.SUCCEEDED,
            ),
        )
    )
    successor_metadata = successor.work.frontier.observation.metadata
    successor_state = successor_metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert successor_metadata["exploration_scheduler_facts_scope"]["source"] == "test-pg"
    assert "home" in successor_state["evidence_gap_page_types"]
    assert sum(successor_state["consumed_actions"].values()) == 1
    assert len(reader.calls) == 2


@pytest.mark.asyncio
async def test_external_package_persists_only_platform_back_recovery_candidate() -> None:
    observations = MemoryVisualObservations()
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_external_package_analysis()),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).run(_command())

    candidates = result.work.frontier.candidates
    back_candidates = [
        candidate for candidate in candidates if candidate.action_type is InteractionOperation.BACK
    ]
    assert len(back_candidates) == 1
    back = back_candidates[0]
    assert back.metadata["package_boundary_recovery"] is True
    assert back.metadata["external_activity_kind"] == "browser"
    assert back.semantic_target == "系统返回键"
    saved_candidates = observations.saved["capture-1"].analysis.candidates
    saved_back = [
        item for item in saved_candidates if item.action_type is InteractionOperation.BACK
    ]
    assert len(saved_back) == 1
    assert saved_back[0].candidate_id == back.candidate_id
    assert result.work.frontier.observation.metadata.get("external_activity_kind") == "browser"
    assert (
        observations.saved["capture-1"].analysis.surface_facts.get("external_activity_kind")
        == "browser"
    )


@pytest.mark.asyncio
async def test_launcher_persists_warm_restore_instead_of_repeating_back() -> None:
    base = _external_package_analysis(visual_page_type="home", page_state="stable")
    launcher = AppVisualAnalysis.build(
        capture_id=base.capture_id,
        screenshot_artifact_id=base.screenshot_artifact_id,
        image_signature=base.image_signature,
        width=base.width,
        height=base.height,
        orientation=base.orientation,
        package_name="com.android.launcher3",
        activity="com.android.launcher3.Launcher",
        visual_page_type="home",
        visual_summary="Android 系统桌面",
        key_elements=("应用图标",),
        candidates=base.candidates,
        provider_id=base.provider_id,
        model_id=base.model_id,
        page_state="stable",
    )
    observations = MemoryVisualObservations()

    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(launcher),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).run(_command())

    assert len(result.work.frontier.candidates) == 1
    restore = result.work.frontier.candidates[0]
    assert restore.action_type is InteractionOperation.WARM_RESTORE
    assert restore.semantic_target == "重新打开目标应用"
    assert restore.risk_level is InteractionRiskLevel.MEDIUM
    assert restore.metadata["package_boundary_recovery"] is True
    assert restore.metadata["action_parameters"] == {"package_name": "com.example.target"}
    saved = observations.saved["capture-1"].analysis.candidates
    assert len(saved) == 1
    assert saved[0].candidate_id == restore.candidate_id
    assert saved[0].action_type is InteractionOperation.WARM_RESTORE
    assert saved[0].risk_level is InteractionRiskLevel.MEDIUM


@pytest.mark.asyncio
async def test_samsung_launcher_upgrades_vision_back_to_warm_restore() -> None:
    """三星桌面不在旧 launcher 白名单时，不得把视觉 BACK 当包边界恢复。"""
    base = _external_package_analysis(visual_page_type="home", page_state="stable")
    launcher = AppVisualAnalysis.build(
        capture_id=base.capture_id,
        screenshot_artifact_id=base.screenshot_artifact_id,
        image_signature=base.image_signature,
        width=base.width,
        height=base.height,
        orientation=base.orientation,
        package_name="com.sec.android.app.launcher",
        activity="com.sec.android.app.launcher.activities.LauncherActivity",
        visual_page_type="home",
        visual_summary="系统桌面主屏",
        key_elements=("搜索框", "应用图标"),
        candidates=base.candidates,
        provider_id=base.provider_id,
        model_id=base.model_id,
        page_state="stable",
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(launcher),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command())
    assert len(result.work.frontier.candidates) == 1
    restore = result.work.frontier.candidates[0]
    assert restore.action_type is InteractionOperation.WARM_RESTORE
    assert restore.risk_level is InteractionRiskLevel.MEDIUM
    assert restore.metadata["package_boundary_recovery"] is True
    assert restore.metadata["action_parameters"] == {"package_name": "com.example.target"}


@pytest.mark.asyncio
async def test_package_recovery_does_not_advance_business_plateau() -> None:
    base = _external_package_analysis(visual_page_type="home", page_state="stable")
    launcher = AppVisualAnalysis.build(
        capture_id=base.capture_id,
        screenshot_artifact_id=base.screenshot_artifact_id,
        image_signature=base.image_signature,
        width=base.width,
        height=base.height,
        orientation=base.orientation,
        package_name="com.android.launcher3",
        activity="com.android.launcher3.Launcher",
        visual_page_type="home",
        visual_summary="Android 系统桌面",
        key_elements=("应用图标",),
        candidates=base.candidates,
        provider_id=base.provider_id,
        model_id=base.model_id,
        page_state="stable",
    )
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(launcher),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    prior_work = replace(
        initial.work,
        budget=ExplorationBudget(
            max_actions=5,
            remaining_actions=2,
            max_depth=3,
            remaining_duration_ms=30_000,
            max_business_actions=2,
            remaining_business_actions=2,
            max_obstruction_actions=3,
            remaining_obstruction_actions=0,
        ),
        frontier=replace(
            initial.work.frontier,
            observation=replace(
                initial.work.frontier.observation,
                metadata={
                    **initial.work.frontier.observation.metadata,
                    "dynamic_no_new_business_page_action_count": 6,
                },
            ),
        ),
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-after",
                artifact_id="artifact-after",
                image_signature="sha256:" + "b" * 64,
                visual_page_type="home",
                page_state="loading",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=prior_work,
            turn=_action_turn(
                prior_work,
                action_type=InteractionOperation.WARM_RESTORE,
            ),
        )
    )

    assert (
        result.work.frontier.observation.metadata["dynamic_no_new_business_page_action_count"] == 6
    )
    assert result.work.budget.resolved_remaining_business_actions == 1
    assert result.work.budget.resolved_remaining_obstruction_actions == 0
    assert result.work.budget.remaining_actions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_type",
    (InteractionOperation.BACK, InteractionOperation.WARM_RESTORE),
)
async def test_package_boundary_recovery_is_surface_restored_without_business_coverage(
    action_type: InteractionOperation,
) -> None:
    """外包恢复只确认目标 App 当前可见，不能恢复旧业务路线或覆盖。"""

    base = _external_package_analysis(visual_page_type="home", page_state="stable")
    if action_type is InteractionOperation.WARM_RESTORE:
        source_analysis = AppVisualAnalysis.build(
            capture_id=base.capture_id,
            screenshot_artifact_id=base.screenshot_artifact_id,
            image_signature=base.image_signature,
            width=base.width,
            height=base.height,
            orientation=base.orientation,
            package_name="com.android.launcher3",
            activity="com.android.launcher3.Launcher",
            visual_page_type="home",
            visual_summary="Android 系统桌面",
            key_elements=("应用图标",),
            candidates=base.candidates,
            provider_id=base.provider_id,
            model_id=base.model_id,
            page_state="stable",
        )
    else:
        source_analysis = base

    observations = MemoryVisualObservations()
    progress = _progress_at_phase("wait_line")
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(source_analysis),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))
    prior_metadata = dict(initial.work.frontier.observation.metadata)
    prior_work = replace(
        initial.work,
        frontier=replace(
            initial.work.frontier,
            observation=replace(
                initial.work.frontier.observation,
                metadata={
                    **prior_metadata,
                    "business_navigation_depth": 2,
                    "business_page_scope": "depth:2",
                    "app_verified_navigation_path": ["stale:home", "stale:detail"],
                },
            ),
        ),
    )

    before_analysis = _coverage_analysis(
        capture_id="capture-before",
        artifact_id="artifact-before",
        image_signature="sha256:" + "c" * 64,
        visual_page_type="home",
        package_name="com.android.chrome",
    )
    after_analysis = _confirmed_fixture(_after_capture_analysis())
    parameters: dict[str, object] = {"app_frontier_restore_only": True}
    if action_type is InteractionOperation.WARM_RESTORE:
        parameters["package_name"] = "com.example.target"
    restored = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            after_analysis,
            analyses_by_capture={"capture-before": before_analysis},
        ),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=prior_work,
            turn=_action_turn(
                prior_work,
                action_type=action_type,
                parameters=parameters,
                action_execution_id=f"action-{action_type.value}-restore",
            ),
        )
    )

    target_identity = after_analysis.surface_facts["app_page_identity"]["identity"]
    metadata = restored.work.frontier.observation.metadata
    proof = metadata["app_action_observation"]
    assert proof["outcome"] == "surface_restored"
    assert proof["business_visible"] is True
    assert tuple(proof["navigation_path"]) == (target_identity,)
    # surface_restored 只表示恢复操作完成，不是业务到达或业务观察证明。
    assert proof["outcome"] not in {"business_arrived", "obstruction_removed"}
    assert proof["outcome"] != "observation_completed"
    assert metadata["covered_business_page_count"] == prior_metadata[
        "covered_business_page_count"
    ] == 0
    assert metadata["covered_page_types"] == prior_metadata["covered_page_types"]
    assert metadata["business_navigation_depth"] == 0
    assert metadata["business_page_scope"] == "app_ready"
    assert tuple(metadata["app_verified_navigation_path"]) == (target_identity,)
    assert metadata["app_curriculum_progress"] == prior_metadata["app_curriculum_progress"]
    assert restored.work.frontier.visited_candidate_ids == (
        prior_work.frontier.candidates[0].candidate_id,
    )
    assert restored.work.source_capture_id == "capture-after"
    assert restored.work.frontier.candidates
    assert all(
        candidate.navigation_depth == 0
        and candidate.metadata.get("package_boundary_recovery") is not True
        for candidate in restored.work.frontier.candidates
    )


@pytest.mark.asyncio
async def test_external_chrome_popup_filters_translation_close_candidate_to_back_only() -> None:
    observations = MemoryVisualObservations()
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _external_package_analysis(
                semantic_name="关闭翻译条",
                purpose="关闭 Chrome 翻译条",
                page_state="popup",
                surface_facts={"popup_visible": True},
            )
        ),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).run(_command())

    candidates = result.work.frontier.candidates
    assert len(candidates) == 1
    assert candidates[0].action_type is InteractionOperation.BACK
    assert candidates[0].metadata["package_boundary_recovery"] is True
    saved_candidates = observations.saved["capture-1"].analysis.candidates
    assert len(saved_candidates) == 1
    assert saved_candidates[0].action_type is InteractionOperation.BACK


@pytest.mark.asyncio
async def test_external_package_preserves_visual_back_candidate_identity() -> None:
    base = _external_package_analysis(
        semantic_name="关闭翻译条",
        purpose="关闭 Chrome 翻译条",
        page_state="popup",
        surface_facts={"popup_visible": True},
    )
    visual_back = AppVisualCandidate.build(
        source_capture_id=base.capture_id,
        source_image_signature=base.image_signature,
        source_width=base.width,
        source_height=base.height,
        source_orientation=base.orientation,
        semantic_name="视觉识别系统返回",
        action_type=InteractionOperation.BACK,
        normalized_bounds=None,
        confidence=0.91,
        risk_level=InteractionRiskLevel.LOW,
        purpose="返回目标 App",
        expected_state_hint="back",
        frontier_scope="page",
    )
    analysis = AppVisualAnalysis.build(
        capture_id=base.capture_id,
        screenshot_artifact_id=base.screenshot_artifact_id,
        image_signature=base.image_signature,
        width=base.width,
        height=base.height,
        orientation=base.orientation,
        visual_page_type=base.visual_page_type,
        visual_summary=base.visual_summary,
        key_elements=base.key_elements,
        candidates=(*base.candidates, visual_back),
        provider_id=base.provider_id,
        model_id=base.model_id,
        package_name=base.package_name,
        activity=base.activity,
        page_state=base.page_state,
        overlay_signature=base.overlay_signature,
        surface_facts=base.surface_facts,
    )
    observations = MemoryVisualObservations()
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(analysis),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).run(_command())

    candidates = result.work.frontier.candidates
    assert len(candidates) == 1
    assert candidates[0].action_type is InteractionOperation.BACK
    assert candidates[0].candidate_id == visual_back.candidate_id
    assert candidates[0].semantic_target == visual_back.semantic_name
    assert candidates[0].metadata["purpose"] == visual_back.purpose
    assert candidates[0].metadata["frontier_scope"] == "local"
    assert candidates[0].metadata["package_boundary_recovery"] is True
    saved_back = tuple(
        item
        for item in observations.saved["capture-1"].analysis.candidates
        if item.action_type is InteractionOperation.BACK
    )
    assert saved_back == (visual_back,)


@pytest.mark.asyncio
async def test_back_recovery_keeps_curriculum_progress_and_reenters_target_package() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    progress = _progress_at_phase("wait_line")
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_external_package_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(curriculum_progress=progress))
    turn = _action_turn(
        initial.work,
        action_type=InteractionOperation.BACK,
        parameters={"app_frontier_restore_only": True},
        action_execution_id="action-boundary-back",
    )

    restored = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=turn,
        )
    )

    assert restored.work.frontier.observation.location == ("app://com.example.target/.HomeActivity")
    assert all(
        candidate.action_type is not InteractionOperation.BACK
        for candidate in restored.work.frontier.candidates
    )
    restored_progress = AppCurriculumProgress.from_dict(
        restored.work.frontier.observation.metadata["app_curriculum_progress"]
    )
    assert restored_progress.to_dict() == progress.to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase_id", "expects_observation"),
    (
        ("wait_line", True),
        ("splash_burst", True),
        ("tap_skip", False),
        ("tab:首页", False),
    ),
)
async def test_curriculum_adds_observation_only_for_zero_touch_phase(
    phase_id: str,
    expects_observation: bool,
) -> None:
    progress = _progress_at_phase(phase_id)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    observations = [
        candidate
        for candidate in result.work.frontier.candidates
        if candidate.action_type is InteractionOperation.OBSERVE
    ]
    assert bool(observations) is expects_observation
    if expects_observation:
        assert observations[0].metadata["curriculum_observation"]["phase_id"] == phase_id
        assert phase_id in observations[0].metadata["expected_state_hint"]


def _hlw_progress_at_phase(
    phase_id: str,
    *,
    current_phase_action_count: int = 0,
) -> AppCurriculumProgress:
    progress = build_app_curriculum_progress(
        resolve_app_capture_playbook(apk_filename="hlwapp_1.7.2_260819_3.apk")
    )
    while progress.current_phase_id != phase_id:
        if progress.current_phase_id is None:
            raise AssertionError(f"无法定位 HLW 课纲阶段：{phase_id}")
        for _ in range(progress.current_phase_required_actions):
            progress = progress.advance(
                phase_id=progress.current_phase_id,
                action_succeeded=True,
            )
    for _ in range(current_phase_action_count):
        progress = progress.advance(
            phase_id=phase_id,
            action_succeeded=True,
        )
    return progress


@pytest.mark.asyncio
async def test_hlw_shenqi_close_step_filters_repeated_scroll_candidate() -> None:
    progress = _hlw_progress_at_phase("shenqi", current_phase_action_count=2)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.SCROLL,
                    "继续下滑",
                    "浏览神器列表",
                    "swipe_left_edge",
                ),
                (
                    InteractionOperation.CLICK,
                    "关闭神器甄选",
                    "关闭当前神器弹层",
                    "close_popup_x",
                ),
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    assert len(candidates) == 1
    assert candidates[0].action_type is InteractionOperation.CLICK
    assert candidates[0].semantic_target == "关闭神器甄选"


@pytest.mark.asyncio
async def test_hlw_shenqi_close_step_rejects_target_package_generic_back() -> None:
    progress = _hlw_progress_at_phase("shenqi", current_phase_action_count=2)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.BACK,
                    "系统返回键",
                    "误进浏览器／详情时 BACK 回 App",
                    "back",
                ),
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    assert result.work.frontier.candidates == ()


@pytest.mark.asyncio
async def test_hlw_shenqi_scroll_candidate_is_kept_when_region_mentions_ad() -> None:
    """神器阶段的安全下滑不能因截图摘要提到广告区域而被误过滤。"""

    progress = _hlw_progress_at_phase("shenqi", current_phase_action_count=1)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.SCROLL,
                    "神器广告区域左缘下滑",
                    "在神器甄选广告区域左缘滑动，继续截图，不点击广告本体",
                    "swipe_left_edge",
                ),
                (
                    InteractionOperation.CLICK,
                    "点击广告本体",
                    "点击广告本体进入外链",
                    "ad_outbound",
                ),
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    assert len(candidates) == 1
    assert candidates[0].action_type is InteractionOperation.SCROLL
    assert candidates[0].semantic_target == "神器广告区域左缘下滑"


@pytest.mark.asyncio
async def test_hlw_shenqi_generates_signed_scroll_when_model_omits_scroll() -> None:
    progress = _hlw_progress_at_phase("shenqi", current_phase_action_count=1)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "跳过按钮",
                    "保留当前跳过按钮候选",
                    "tap_skip",
                ),
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    assert len(candidates) == 1
    fallback = candidates[0]
    assert fallback.action_type is InteractionOperation.SCROLL
    assert fallback.semantic_target == "课纲安全下滑：shenqi：第2帧"
    assert fallback.metadata["curriculum_action"] == {
        "protocol_version": "app-curriculum-action.v1",
        "phase_id": "shenqi",
        "phase_action_count": 1,
        "action_type": "scroll",
        "expected_state_hint": "shenqi",
    }


@pytest.mark.asyncio
async def test_hlw_shenqi_scroll_survives_trusted_popup_close_candidate() -> None:
    """神器第 2 步必须在弹层内下滑，不能被关闭恢复候选吞掉。"""

    progress = _hlw_progress_at_phase("shenqi", current_phase_action_count=1)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "关闭神器甄选",
                    "仅关闭目标包当前神器甄选遮挡，不点击广告主体",
                    "close_popup_x",
                ),
                page_state="popup",
                surface_facts={"popup_visible": True},
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    assert candidates[0].action_type is InteractionOperation.SCROLL
    assert candidates[0].metadata["curriculum_action"]["phase_id"] == "shenqi"
    # watcher_cascade 保留文案／几何关层，但不得抢占课纲下滑。
    assert any(item.semantic_target == "关闭神器甄选" for item in candidates)
    assert candidates[0].semantic_target != "关闭神器甄选"


@pytest.mark.asyncio
async def test_hlw_content_detail_preserves_trusted_eating_tab_restore() -> None:
    """福利页无普通卡片时必须保留受信吃瓜底栏，且拒绝下载候选。"""

    progress = _hlw_progress_at_phase("content_detail")
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "立即下载",
                    "下载对应应用",
                    None,
                ),
                (
                    InteractionOperation.CLICK,
                    "底栏「吃瓜」",
                    "打开目标包底栏 tab:吃瓜 并重新截图",
                    "tab:吃瓜",
                ),
                surface_facts={
                    "trusted_accessibility_controls": {
                        "contract_id": "hlwapp-accessibility.v1",
                        "package_name": "com.example.target",
                        "page_type": "content_list",
                        "content_card_count": 0,
                        "bottom_navigation_count": 6,
                    }
                },
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    assert [item.semantic_target for item in candidates] == ["底栏「吃瓜」"]

    untrusted = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "底栏「吃瓜」",
                    "打开目标包底栏 tab:吃瓜 并重新截图",
                    "tab:吃瓜",
                )
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    assert untrusted.work.frontier.candidates == ()


@pytest.mark.asyncio
async def test_hlw_trusted_detail_adds_curriculum_system_back_when_provider_is_loading() -> None:
    """详情 Provider 降级为 loading 时，受信只读结构仍须补系统返回。"""

    progress = _hlw_progress_at_phase("content_detail_back")
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                page_state="loading",
                visual_page_type="content_detail",
                surface_facts={
                    "trusted_accessibility_controls": {
                        "contract_id": "hlwapp-accessibility.v1",
                        "package_name": "com.example.target",
                        "page_type": "content_detail",
                        "overlay_close": False,
                        "content_card_count": 0,
                        "bottom_navigation_count": 0,
                    }
                },
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    assert len(result.work.frontier.candidates) == 1
    back = result.work.frontier.candidates[0]
    assert back.action_type is InteractionOperation.BACK
    assert back.semantic_target == "系统 BACK 返回内容列表"
    assert back.metadata["expected_state_hint"] == "content_detail_back"
    assert back.metadata["curriculum_action"] == {
        "protocol_version": "app-curriculum-action.v1",
        "phase_id": "content_detail_back",
        "phase_action_count": 0,
        "action_type": "back",
        "expected_state_hint": "content_detail_back",
    }
    assert back.metadata.get("package_boundary_recovery") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "phase_id",
        "visual_page_type",
        "contract_id",
        "control_page_type",
        "overlay_close",
        "bottom_navigation_count",
    ),
    (
        ("content_detail", "content_detail", "hlwapp-accessibility.v1", "content_detail", False, 0),
        ("content_detail_back", "home", "hlwapp-accessibility.v1", "content_detail", False, 0),
        ("content_detail_back", "content_detail", "untrusted.v1", "content_detail", False, 0),
        (
            "content_detail_back",
            "content_detail",
            "hlwapp-accessibility.v1",
            "content_list",
            False,
            0,
        ),
        (
            "content_detail_back",
            "content_detail",
            "hlwapp-accessibility.v1",
            "content_detail",
            True,
            0,
        ),
        (
            "content_detail_back",
            "content_detail",
            "hlwapp-accessibility.v1",
            "content_detail",
            False,
            1,
        ),
    ),
)
async def test_hlw_detail_back_requires_exact_trusted_contract(
    phase_id: str,
    visual_page_type: str,
    contract_id: str,
    control_page_type: str,
    overlay_close: bool,
    bottom_navigation_count: int,
) -> None:
    progress = _hlw_progress_at_phase(phase_id)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                page_state="loading",
                visual_page_type=visual_page_type,
                surface_facts={
                    "trusted_accessibility_controls": {
                        "contract_id": contract_id,
                        "package_name": "com.example.target",
                        "page_type": control_page_type,
                        "overlay_close": overlay_close,
                        "content_card_count": 0,
                        "bottom_navigation_count": bottom_navigation_count,
                    }
                },
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    assert result.work.frontier.candidates == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_phase_action_count", "expected_action", "expected_target"),
    (
        (0, InteractionOperation.CLICK, "吃瓜"),
        (1, InteractionOperation.SCROLL, "继续下滑"),
    ),
)
async def test_hlw_tab_phase_only_exposes_current_required_action(
    current_phase_action_count: int,
    expected_action: InteractionOperation,
    expected_target: str,
) -> None:
    progress = _hlw_progress_at_phase(
        "tab:吃瓜",
        current_phase_action_count=current_phase_action_count,
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "吃瓜",
                    "切换到吃瓜栏目",
                    "tab:吃瓜",
                ),
                (
                    InteractionOperation.CLICK,
                    "AI科技",
                    "切换到 AI 科技栏目",
                    "tab:AI科技",
                ),
                (
                    InteractionOperation.SCROLL,
                    "继续下滑",
                    "浏览当前栏目",
                    "swipe_left_edge",
                ),
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    assert len(candidates) == 1
    assert candidates[0].action_type is expected_action
    assert candidates[0].semantic_target == expected_target


@pytest.mark.asyncio
async def test_curriculum_filter_preserves_dynamic_popup_close_recovery() -> None:
    progress = _hlw_progress_at_phase("shenqi", current_phase_action_count=1)
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "关闭推广弹窗",
                    "关闭当前遮挡弹层",
                    "close_popup_x",
                ),
                page_state="popup",
                surface_facts={"popup_visible": True},
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    assert candidates[0].action_type is InteractionOperation.SCROLL
    assert candidates[0].metadata["curriculum_action"]["phase_id"] == "shenqi"
    # cascade 保留关层恢复候选，但课纲下滑须排在其前。
    assert any(item.semantic_target == "关闭推广弹窗" for item in candidates)
    assert candidates[0].semantic_target != "关闭推广弹窗"


@pytest.mark.asyncio
async def test_curriculum_filter_preserves_popup_close_with_negative_ad_guard() -> None:
    """否定性安全说明不能被反向识别为广告主体点击。"""

    progress = _hlw_progress_at_phase("tab:吃瓜")
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "关闭神器甄选",
                    "仅关闭目标包当前神器甄选遮挡，不点击广告主体",
                    "close_popup_x",
                ),
                page_state="popup",
                surface_facts={"popup_visible": True},
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(curriculum_progress=progress))

    candidates = result.work.frontier.candidates
    # watcher_cascade 保留关层；否定文案不得被误签成广告主体取证。
    assert not any(
        item.metadata.get("app_ad_landing_consistency") is True for item in candidates
    )
    assert any(item.semantic_target == "关闭神器甄选" for item in candidates)


@pytest.mark.asyncio
async def test_coverage_metadata_uses_persisted_pending_facts() -> None:
    pending_facts = RecordingPendingFacts(pending_actions=2, pending_evidence=3)
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
        pending_facts=pending_facts,
    )

    result = await workflow.run(_command())

    metadata = result.work.frontier.observation.metadata
    assert pending_facts.calls == [("task-1", "session-1")]
    assert metadata["pending_action_count"] == 2
    assert metadata["pending_evidence_count"] == 3
    assert metadata["pending_facts_available"] is True


@pytest.mark.asyncio
async def test_coverage_metadata_does_not_fabricate_zero_without_pending_facts_reader() -> None:
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    )

    result = await workflow.run(_command())

    metadata = result.work.frontier.observation.metadata
    assert metadata["pending_action_count"] is None
    assert metadata["pending_evidence_count"] is None
    assert metadata["pending_facts_available"] is False


@pytest.mark.asyncio
async def test_visual_provider_degradation_is_persisted_and_accumulated_in_coverage() -> None:
    analysis = _analysis_with_surface_facts(
        page_state="stable",
        surface_facts={
            "visual_provider_quality": {
                "protocol_version": "app-visual-provider-quality.v1",
                "status": "degraded",
                "provider_id": "llm-app-vision",
                "model_id": "vision-model",
                "error_code": "provider_call_failed",
                "error_type": "TimeoutError",
                "retryable": True,
            }
        },
    )
    observations = MemoryVisualObservations()
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(analysis),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    )

    result = await workflow.run(_command())

    saved = observations.saved["capture-1"].analysis
    assert saved.surface_facts["visual_provider_quality"]["status"] == "degraded"
    metadata = result.work.frontier.observation.metadata
    assert metadata["degraded_provider_facts"] == ()
    assert metadata["visual_soft_degrade_count"] == 1

    repeated = _next_coverage_metadata(
        goal=_command().app_goal,
        analysis=analysis,
        prior_metadata=metadata,
    )

    assert repeated["visual_soft_degrade_count"] == 2
    assert [dict(item) for item in repeated["degraded_provider_facts"]] == [
        {
            "protocol_version": "app-visual-provider-quality.v1",
            "status": "degraded",
            "provider_id": "llm-app-vision",
            "model_id": "vision-model",
            "error_code": "provider_call_failed",
            "error_type": "TimeoutError",
            "retryable": True,
            "capture_id": "capture-1",
            "screenshot_artifact_id": "artifact-1",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_state", "overlay_signature"),
    (("popup", "overlay:popup-1"), ("unknown", None)),
)
async def test_screenshot_frontier_preserves_surface_phase_facts(
    page_state: str,
    overlay_signature: str | None,
) -> None:
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_surface_facts(
                page_state=page_state,
                overlay_signature=overlay_signature,
                surface_facts={
                    "source": "screenshot",
                    "popup_visible": True,
                    "screenshot_artifact_id": "不可信覆盖值",
                },
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    )

    result = await workflow.run(_command())
    observation = result.work.frontier.observation

    assert observation.page_state == page_state
    assert observation.overlay_signature == overlay_signature
    assert observation.metadata["surface_facts"] == {
        "source": "screenshot",
        "popup_visible": True,
        "screenshot_artifact_id": "不可信覆盖值",
    }
    assert observation.metadata["screenshot_artifact_id"] == "artifact-1"


@pytest.mark.asyncio
async def test_initial_frontier_uses_command_observed_at_not_visual_delay() -> None:
    """视觉理解晚于 Checkpoint 冻结点时，首 Work 仍须对齐 command.observed_at。"""

    visual_delay = NOW + timedelta(seconds=15)
    observations = MemoryVisualObservations(
        observed_at_by_capture={"capture-1": visual_delay}
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="startup_ad",
                page_state="loading",
            )
        ),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    )
    command = _command()

    result = await workflow.run(command)

    assert result.work.frontier.observation.observed_at == command.observed_at
    assert result.work.frontier.observation.observed_at != visual_delay
    assert (
        result.work.frontier.observation.metadata["visual_observed_at"]
        == visual_delay.isoformat()
    )


@pytest.mark.asyncio
async def test_initial_stable_business_page_starts_goal_coverage() -> None:
    goal = _coverage_goal(
        required_page_types=("home",),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="home",
                visual_summary="home",
                key_elements=("home",),
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    )

    result = await workflow.run(_command(app_goal=goal))

    metadata = result.work.frontier.observation.metadata
    assert metadata["covered_page_types"] == ("home",)
    assert metadata["covered_milestones"] == ("surface:stable",)
    assert metadata["covered_business_state_ids"] == (
        "com.example.target|.mainactivity",
    )
    assert metadata["covered_business_page_count"] == 1
    assert result.page_type_assessment.execution_status == "accepted"
    assert result.page_type_assessment.page_type_assessment is not None
    assert result.page_type_assessment.page_type_assessment["page_type"] == "home"
    assert result.page_type_assessment.page_type_assessment["status"] == "covered"
    assert result.page_type_assessment.page_type_assessment["page_capture_ids"] == ["capture-1"]
    assert result.work.app_goal == goal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_type", "page_state", "expected_covered"),
    (
        ("content_list", "loading", ()),
        ("startup_ad", "loading", ("startup_ad",)),
        ("guide_page", "stable", ("guide_page",)),
    ),
)
async def test_coverage_requires_stable_business_arrival_but_records_lifecycle_pages(
    page_type: str,
    page_state: str,
    expected_covered: tuple[str, ...],
) -> None:
    goal = _coverage_goal(
        required_page_types=(page_type,),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type=page_type,
                page_state=page_state,
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(app_goal=goal))

    assert result.work.frontier.observation.metadata["covered_page_types"] == (expected_covered)


@pytest.mark.asyncio
async def test_non_allowed_stable_content_list_does_not_count_toward_app_coverage() -> None:
    goal = _coverage_goal(
        required_page_types=("content_list",),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _external_package_analysis(
                visual_page_type="content_list",
                page_state="stable",
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command(app_goal=goal))

    metadata = result.work.frontier.observation.metadata
    assert metadata["covered_page_types"] == ()
    assert metadata["covered_milestones"] == ()
    assert metadata["covered_business_page_count"] == 0


@pytest.mark.asyncio
async def test_goal_coverage_records_visible_entry_as_applicability_evidence() -> None:
    goal = _coverage_goal(
        required_page_types=("home", "content_detail", "payment"),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="home",
                candidate_semantic_name="第一条内容",
                candidate_purpose="打开内容详情",
                expected_state_hint="content_detail",
            )
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    )

    result = await workflow.run(_command(app_goal=goal))

    [candidate] = result.work.frontier.observation.metadata["discovered_page_type_candidates"]
    assert candidate["page_type"] == "content_detail"
    assert candidate["source_capture_id"] == "capture-1"
    assert candidate["screenshot_artifact_id"] == "artifact-1"
    assert candidate["candidate_id"]


def test_dynamic_page_limit_stops_new_page_expansion_but_keeps_sampling() -> None:
    base = _coverage_analysis(
        capture_id="capture-page-limit",
        artifact_id="artifact-page-limit",
        image_signature=SIGNATURE,
        visual_page_type="home",
    )
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=base.capture_id,
        source_image_signature=base.image_signature,
        source_width=base.width,
        source_height=base.height,
        source_orientation=base.orientation,
        semantic_name="内容入口",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.1, 0.2, 0.8, 0.3),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="进入内容详情",
        expected_state_hint="content_detail",
        frontier_scope="page",
    )
    analysis = AppVisualAnalysis.build(
        capture_id=base.capture_id,
        screenshot_artifact_id=base.screenshot_artifact_id,
        image_signature=base.image_signature,
        width=base.width,
        height=base.height,
        orientation=base.orientation,
        visual_page_type=base.visual_page_type,
        visual_summary=base.visual_summary,
        key_elements=("内容入口",),
        candidates=(visual_candidate,),
        provider_id=base.provider_id,
        model_id=base.model_id,
        package_name=base.package_name,
        activity=base.activity,
        page_state=base.page_state,
    )

    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "covered_page_types": ("home",),
            "covered_business_page_count": 1,
            "business_page_scope": "app_ready",
            "app_runtime_policy": {
                "exploration_mode": "dynamic_frontier",
                # dynamic_frontier 默认数量不限量；显式关闭后 page_limit 才作抽样帽。
                "quantity_unbounded": False,
                "page_limit": 1,
            },
        },
    )

    assert all(item.semantic_target != "内容入口" for item in frontier.candidates)
    assert any(item.metadata.get("dynamic_page_sampling") is True for item in frontier.candidates)


@pytest.mark.asyncio
async def test_successful_action_accumulates_new_stable_page_and_matching_milestone() -> None:
    goal = _coverage_goal()
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="home",
                visual_summary="home",
                key_elements=("details",),
                candidate_semantic_name="details",
                candidate_purpose="open details",
                expected_state_hint="details",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(app_goal=goal))
    assert initial.work.frontier.observation.metadata["covered_milestones"] == ("surface:stable",)
    next_signature = "sha256:" + "b" * 64
    after_analysis = _confirmed_fixture(_coverage_analysis(
        capture_id="capture-after", artifact_id="artifact-after",
        image_signature=next_signature, visual_page_type="content_detail",
        visual_summary="details", key_elements=("details",),
    ), object_id="作品编号 123")
    before_analysis = _confirmed_fixture(_coverage_analysis(
        capture_id="capture-before", artifact_id="artifact-before",
        image_signature="sha256:" + "c" * 64, visual_page_type="home",
    ))
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            after_analysis,
            analyses_by_capture={"capture-before": before_analysis},
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work, semantic_target="details"),
        )
    )

    metadata = successor.work.frontier.observation.metadata
    assert metadata["covered_page_types"] == ("home", "content_detail")
    assert metadata["covered_milestones"] == ("surface:stable", "journey:details")
    assert metadata["covered_business_state_ids"] == (
        "com.example.target|.mainactivity",
        metadata["business_page_identity"],
    )
    assert metadata["business_page_identity"].startswith(
        "com.example.target|.mainactivity|vid:"
    )
    assert metadata["covered_business_page_count"] == 2
    assert metadata["dynamic_no_new_business_page_action_count"] == 0
    assert metadata["dynamic_repeated_business_page_action_count"] == 0
    assert list(metadata["app_semantic_action_counts"].values()) == [1]
    assert successor.page_type_assessment.execution_status == "accepted"
    assert successor.page_type_assessment.page_type_assessment["page_type"] == ("content_detail")
    assert successor.work.app_goal == initial.work.app_goal == goal


def test_screenshot_frontier_suppresses_repeated_click_on_covered_target() -> None:
    from services.api.app.application.app_semantic_progress import (
        APP_SEMANTIC_ACTION_COUNTS,
        build_app_semantic_action_key,
    )

    analysis = _coverage_analysis(
        capture_id="capture-home",
        artifact_id="artifact-home",
        image_signature=SIGNATURE,
        visual_page_type="home",
        candidate_semantic_name="登录按钮",
        candidate_purpose="提交登录",
        expected_state_hint="home",
    )
    initial = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        coverage_metadata={"covered_page_types": ("home",)},
    )
    semantic_key = build_app_semantic_action_key(initial.candidates[0])

    filtered = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        coverage_metadata={
            "covered_page_types": ("home",),
            APP_SEMANTIC_ACTION_COUNTS: {semantic_key: 2},
        },
    )

    assert all(item.semantic_target != "登录按钮" for item in filtered.candidates)
    assert {item.action_type for item in filtered.candidates} == {InteractionOperation.BACK}


def test_failed_coverage_seek_is_suppressed_without_repeating_same_coordinates() -> None:
    """真实寻页执行失败后应记录封顶分值，后继不再重复同一来源到目标。"""

    from services.api.app.application.app_semantic_progress import (
        APP_COVERAGE_SEEK_FAILURE_SCORES,
    )

    goal = _coverage_goal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable",),
        minimum_business_pages=2,
    )
    analysis = _coverage_analysis(
        capture_id="capture-home-failed-seek",
        artifact_id="artifact-home-failed-seek",
        image_signature=SIGNATURE,
        visual_page_type="home",
        candidate_semantic_name="账户入口",
        candidate_purpose="进入账户页面",
        expected_state_hint="profile",
    )
    initial_metadata = _next_coverage_metadata(goal=goal, analysis=analysis)
    initial_frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata=initial_metadata,
    )
    seek = next(
        item
        for item in initial_frontier.candidates
        if item.metadata.get("coverage_page_seek") is True
    )

    failed_metadata = _next_coverage_metadata(
        goal=goal,
        analysis=analysis,
        prior_metadata=initial_metadata,
        attempted_action=seek,
        action_succeeded=False,
        arrival_proven=False,
    )
    assert list(failed_metadata[APP_COVERAGE_SEEK_FAILURE_SCORES].values()) == [2]

    successor = _build_frontier(
        analysis=analysis,
        observed_at=NOW + timedelta(seconds=1),
        allowed_hosts=("com.example.target",),
        coverage_metadata=failed_metadata,
    )
    assert all(item.metadata.get("coverage_page_seek") is not True for item in successor.candidates)


def test_coverage_seek_suppresses_semantic_alias_across_page_name_instances() -> None:
    """同一分类入口无到达后，不得靠 Tab 别名或页面名称实例绕过去重。"""

    from services.api.app.application.app_semantic_progress import (
        APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS,
        should_suppress_redundant_app_candidate,
    )

    goal = _coverage_goal(
        required_page_types=("video_list", "profile"),
        minimum_business_pages=2,
    )
    analysis = _coverage_analysis(
        capture_id="capture-video-list",
        artifact_id="artifact-video-list",
        image_signature=SIGNATURE,
        visual_page_type="video_list",
        surface_facts={
            "visual_page_name": {
                "protocol_version": "app-visual-page-name.v1",
                "page_name": "二次元",
                "source": "selected_tab",
                "confidence": 0.96,
            }
        },
    )
    attempted = CandidateAction(
        candidate_id="candidate-category-original",
        semantic_target="分类_福利姬",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "profile",
        },
    )
    alias = replace(
        attempted,
        candidate_id="candidate-category-alias",
        semantic_target="分类Tab_福利姬",
    )
    distinct = replace(
        attempted,
        candidate_id="candidate-category-distinct",
        semantic_target="分类Tab_福利图",
    )

    initial = _next_coverage_metadata(goal=goal, analysis=analysis)
    failed = _next_coverage_metadata(
        goal=goal,
        analysis=analysis,
        prior_metadata=initial,
        attempted_action=attempted,
        action_succeeded=True,
        arrival_proven=False,
    )
    alias_page_metadata = {
        **failed,
        # 同包同 Activity 的另一展示名实例；寻页去重须仍命中。
        "business_page_identity": "com.example.target|.mainactivity",
    }

    assert list(failed[APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS].values()) == [1]
    assert (
        should_suppress_redundant_app_candidate(
            candidate=alias,
            coverage_metadata=alias_page_metadata,
        )
        is True
    )
    assert (
        should_suppress_redundant_app_candidate(
            candidate=distinct,
            coverage_metadata=alias_page_metadata,
        )
        is False
    )


def test_coverage_seek_suppresses_same_tab_suffix_but_not_distinct_business_label() -> None:
    """同一来源页类的连载更新 Tab 别名不得重复寻页，其他业务标签仍可探索。"""

    from services.api.app.application.app_semantic_progress import (
        APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS,
        record_app_coverage_seek_target_attempt,
        should_suppress_redundant_app_candidate,
    )

    attempted = CandidateAction(
        candidate_id="candidate-serial-update",
        semantic_target="连载更新",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "video_list",
        },
    )
    tab_alias = replace(
        attempted,
        candidate_id="candidate-serial-update-tab",
        semantic_target="连载更新_tab",
    )
    distinct_business_label = replace(
        attempted,
        candidate_id="candidate-serial-completed",
        semantic_target="连载完结",
    )
    attempted_page_identity = "com.example.target|.mainactivity"
    current_page_identity = "com.example.target|.mainactivity|vid:serial-update-placeholder"
    attempts = record_app_coverage_seek_target_attempt(
        {},
        candidate=attempted,
        page_identity=attempted_page_identity,
        arrival_proven=False,
    )
    metadata = {
        "business_page_identity": current_page_identity,
        APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS: attempts,
    }

    assert list(attempts.values()) == [1]
    assert should_suppress_redundant_app_candidate(
        candidate=tab_alias,
        coverage_metadata=metadata,
    )
    assert not should_suppress_redundant_app_candidate(
        candidate=distinct_business_label,
        coverage_metadata=metadata,
    )


def test_coverage_seek_suppresses_first_card_ordinal_alias() -> None:
    """同一首卡不得靠“卡片1／首卡”改写重复下钻。"""

    from services.api.app.application.app_semantic_progress import (
        record_app_coverage_seek_target_attempt,
        should_suppress_redundant_app_candidate,
    )

    attempted = CandidateAction(
        candidate_id="candidate-first-card-original",
        semantic_target="最新上线卡片1",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "video_detail",
        },
    )
    alias = replace(
        attempted,
        candidate_id="candidate-first-card-alias",
        semantic_target="最新上线首卡",
    )
    second_card = replace(
        attempted,
        candidate_id="candidate-second-card",
        semantic_target="最新上线卡片2",
    )
    page_identity = "com.example.target|.mainactivity|video_list|name:video"
    attempts = record_app_coverage_seek_target_attempt(
        {},
        candidate=attempted,
        page_identity=page_identity,
        arrival_proven=False,
    )
    metadata = {
        "business_page_identity": page_identity,
        "app_coverage_seek_target_attempt_counts": attempts,
    }

    assert should_suppress_redundant_app_candidate(
        candidate=alias,
        coverage_metadata=metadata,
    )
    assert not should_suppress_redundant_app_candidate(
        candidate=second_card,
        coverage_metadata=metadata,
    )


@pytest.mark.parametrize(
    ("semantic_name", "purpose"),
    [
        ("立即下载", "下载外部应用"),
        ("会员支付", "购买并开通会员"),
    ],
)
def test_dynamic_frontier_rejects_model_downgraded_side_effect_entry(
    semantic_name: str,
    purpose: str,
) -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-risk",
            artifact_id="artifact-risk",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            candidate_semantic_name=semantic_name,
            candidate_purpose=purpose,
            expected_state_hint="home",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert all(item.semantic_target != semantic_name for item in frontier.candidates)


def test_dynamic_frontier_allows_bounded_direct_connect_on_proven_connection_gate() -> None:
    """线路失败帧中的低风险直连可恢复启动，但仍受观察、次数和语义去重约束。"""

    analysis = _coverage_analysis(
        capture_id="capture-direct-connect-gate",
        artifact_id="artifact-direct-connect-gate",
        image_signature=SIGNATURE,
        visual_page_type=None,
        page_state="stable",
        visual_summary="线路自动检测失败，列出备用线路供切换",
        key_elements=("线路自动检测失败", "请检查网络是否连接正常", "点我直连试试"),
        candidate_semantic_name="点我直连试试",
        candidate_purpose="跳过线路检测并直连",
        expected_state_hint="home",
    )

    deferred = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        navigation_depth=0,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )
    assert all(item.semantic_target != "点我直连试试" for item in deferred.candidates)

    allowed = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        navigation_depth=0,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"dynamic_startup_observation_count": 2},
    )
    assert any(item.semantic_target == "点我直连试试" for item in allowed.candidates)
    assert all(item.action_type is not InteractionOperation.BACK for item in allowed.candidates)

    recovery = next(item for item in allowed.candidates if item.semantic_target == "点我直连试试")
    recovery_key = _dynamic_connection_recovery_key(recovery)
    blocked = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        navigation_depth=0,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "dynamic_startup_observation_count": 2,
            "dynamic_startup_recovery_action_count": 1,
            "dynamic_startup_recovery_targets": [recovery_key],
        },
    )
    assert all(item.semantic_target != "点我直连试试" for item in blocked.candidates)


def test_dynamic_frontier_rejects_direct_connect_without_connection_gate_text() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-direct-connect-business",
            artifact_id="artifact-direct-connect-business",
            image_signature=SIGNATURE,
            visual_page_type="home",
            candidate_semantic_name="点我直连试试",
            candidate_purpose="跳过线路检测并直连",
            expected_state_hint="home",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"dynamic_startup_observation_count": 2},
    )

    assert all(item.semantic_target != "点我直连试试" for item in frontier.candidates)


def test_dynamic_frontier_preserves_content_candidate_with_negative_safety_guard() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-content",
            artifact_id="artifact-content",
            image_signature=SIGNATURE,
            visual_page_type="home",
            candidate_semantic_name="资讯图文卡片",
            candidate_purpose="打开已验证普通内容卡片，已排除广告下载充值语义",
            expected_state_hint="content_detail",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert any(item.semantic_target == "资讯图文卡片" for item in frontier.candidates)


def test_popup_close_external_redirect_prefers_one_local_back_after_return() -> None:
    goal = _coverage_goal(
        required_page_types=("home", "content_list"),
        minimum_business_pages=1,
    )
    popup = _coverage_analysis(
        capture_id="capture-popup",
        artifact_id="artifact-popup",
        image_signature=SIGNATURE,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="close_ad_popup",
        candidate_purpose="关闭广告弹窗",
        expected_state_hint="home",
    )
    close_action = _build_frontier(
        analysis=popup,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    ).candidates[0]
    external_metadata = _next_coverage_metadata(
        goal=goal,
        analysis=_external_package_analysis(
            visual_page_type="content_list",
            page_state="stable",
        ),
        prior_metadata={"covered_page_types": ("home",)},
        completed_action=close_action,
    )

    assert external_metadata["covered_page_types"] == ["home"]
    assert external_metadata["dynamic_popup_external_redirect_count"] == 1
    assert external_metadata["dynamic_popup_back_recovery_count"] == 0

    returned_popup = _coverage_analysis(
        capture_id="capture-returned-popup",
        artifact_id="artifact-returned-popup",
        image_signature="sha256:" + "b" * 64,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="返回关闭广告",
        candidate_action_type=InteractionOperation.BACK,
        candidate_purpose="尝试退出当前广告覆盖层",
        expected_state_hint="home",
    )
    frontier = _build_frontier(
        analysis=returned_popup,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata=external_metadata,
    )

    recovery = frontier.candidates[0]
    assert recovery.action_type is InteractionOperation.BACK
    assert recovery.candidate_id == returned_popup.candidates[0].candidate_id
    assert recovery.metadata["frontier_scope"] == "local"
    assert recovery.metadata["popup_back_recovery"] is True


def test_popup_external_redirect_uses_platform_signed_back_when_provider_omits_it() -> None:
    popup = _coverage_analysis(
        capture_id="capture-popup-platform-back",
        artifact_id="artifact-popup-platform-back",
        image_signature=SIGNATURE,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="关闭广告弹窗",
        candidate_purpose="关闭广告弹窗",
        expected_state_hint="home",
    )

    enriched = _enrich_package_boundary_analysis(
        popup,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )
    frontier = _build_frontier(
        analysis=enriched,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "dynamic_popup_external_redirect_count": 1,
            "dynamic_popup_back_recovery_count": 0,
        },
    )

    recovery = frontier.candidates[0]
    assert recovery.action_type is InteractionOperation.BACK
    assert recovery.semantic_target == "底部导航栏「<」关闭弹窗"
    assert recovery.metadata["popup_back_recovery"] is True
    assert any(candidate.candidate_id == recovery.candidate_id for candidate in enriched.candidates)

    repeated = _build_frontier(
        analysis=enriched,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "dynamic_popup_external_redirect_count": 1,
            "dynamic_popup_back_recovery_count": 1,
        },
    )
    assert repeated.candidates[0].metadata["popup_back_recovery"] is True


def test_popup_external_redirect_prefers_back_over_visual_dismiss() -> None:
    """关层曾外跳后：即使本帧仍有关闭 X，也优先系统 BACK，禁止再点穿广告。"""

    popup = _coverage_analysis(
        capture_id="capture-popup-current-dismiss",
        artifact_id="artifact-popup-current-dismiss",
        image_signature=SIGNATURE,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="关闭广告弹窗",
        candidate_purpose="关闭当前广告弹窗",
        candidate_control_role="dismiss",
        candidate_bounds=NormalizedBounds(0.84, 0.06, 0.94, 0.12),
        expected_state_hint="home",
        surface_facts={"popup_visible": True},
    )

    enriched = _enrich_package_boundary_analysis(
        popup,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
    )
    frontier = _build_frontier(
        analysis=enriched,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "dynamic_popup_external_redirect_count": 1,
            "dynamic_popup_back_recovery_count": 0,
        },
    )

    assert frontier.candidates[0].action_type is InteractionOperation.BACK
    assert frontier.candidates[0].metadata.get("popup_back_recovery") is True
    assert frontier.candidates[0].semantic_target in {
        "底部导航栏「<」关闭弹窗",
        "返回关闭弹窗",
        "系统返回键",
    }


def test_popup_redirect_history_survives_startup_ad_transition() -> None:
    metadata = _next_coverage_metadata(
        goal=_coverage_goal(required_page_types=("home",), minimum_business_pages=1),
        analysis=_coverage_analysis(
            capture_id="capture-startup-transition",
            artifact_id="artifact-startup-transition",
            image_signature=SIGNATURE,
            visual_page_type="startup_ad",
            page_state="startup_ad",
        ),
        prior_metadata={
            "dynamic_popup_external_redirect_count": 1,
            "dynamic_popup_back_recovery_count": 0,
        },
    )

    assert metadata["dynamic_popup_external_redirect_count"] == 1
    assert metadata["dynamic_popup_back_recovery_count"] == 0


def test_popup_back_recovery_resets_after_unobstructed_target_page() -> None:
    goal = _coverage_goal(required_page_types=("home",), minimum_business_pages=1)
    popup = _coverage_analysis(
        capture_id="capture-popup-reset",
        artifact_id="artifact-popup-reset",
        image_signature=SIGNATURE,
        visual_page_type="home",
        page_state="popup",
        candidate_semantic_name="返回关闭广告",
        candidate_action_type=InteractionOperation.BACK,
        candidate_purpose="尝试退出当前广告覆盖层",
        expected_state_hint="home",
    )
    recovery = _build_frontier(
        analysis=popup,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "dynamic_popup_external_redirect_count": 1,
            "dynamic_popup_back_recovery_count": 0,
        },
    ).candidates[0]
    stable_metadata = _next_coverage_metadata(
        goal=goal,
        analysis=_coverage_analysis(
            capture_id="capture-stable-after-back",
            artifact_id="artifact-stable-after-back",
            image_signature="sha256:" + "c" * 64,
            visual_page_type="home",
        ),
        prior_metadata={
            "dynamic_popup_external_redirect_count": 1,
            "dynamic_popup_back_recovery_count": 0,
        },
        completed_action=recovery,
    )

    assert stable_metadata["dynamic_popup_external_redirect_count"] == 0
    assert stable_metadata["dynamic_popup_back_recovery_count"] == 0

    exhausted = _build_frontier(
        analysis=popup,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "dynamic_popup_external_redirect_count": 3,
            "dynamic_popup_back_recovery_count": 2,
        },
    )
    assert all(
        candidate.metadata.get("popup_back_recovery") is not True
        for candidate in exhausted.candidates
    )


def test_dynamic_frontier_defers_startup_line_switch_until_two_observations() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-line-blocker",
            artifact_id="artifact-line-blocker",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            candidate_semantic_name="备用线路1",
            candidate_purpose="尝试备用线路进入首页",
            expected_state_hint="home",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert all(item.semantic_target != "备用线路1" for item in frontier.candidates)


def test_dynamic_frontier_allows_each_backup_line_once_after_observation() -> None:
    analysis = _coverage_analysis(
        capture_id="capture-line-recovery",
        artifact_id="artifact-line-recovery",
        image_signature=SIGNATURE,
        visual_page_type="line_loading",
        candidate_semantic_name="备用线路1",
        candidate_purpose="尝试备用线路进入首页",
        expected_state_hint="home",
    )
    allowed = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"dynamic_startup_observation_count": 2},
    )
    assert any(item.semantic_target == "备用线路1" for item in allowed.candidates)

    blocked = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "dynamic_startup_observation_count": 2,
            "dynamic_startup_recovery_action_count": 1,
            "dynamic_startup_recovery_targets": ["line:1"],
        },
    )
    assert all(item.semantic_target != "备用线路1" for item in blocked.candidates)


def test_dynamic_frontier_keeps_low_risk_visual_startup_exit() -> None:
    """真实视觉候选明确跳过开屏并进入首页时不能被动态安全过滤吞掉。"""

    frontier = _build_frontier(
        analysis=_analysis_with_candidates(
            (
                InteractionOperation.CLICK,
                "进入应用",
                "跳过启动广告并进入应用",
                "home",
            ),
            page_state="startup_ad",
            visual_page_type="startup_ad",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert any(candidate.semantic_target == "进入应用" for candidate in frontier.candidates)


def test_dynamic_frontier_keeps_update_prompt_dismissal_over_startup_ad() -> None:
    """开屏上方的版本提示仍须先关闭，不能被启动阻断过滤器吞掉。"""

    frontier = _build_frontier(
        analysis=_analysis_with_candidates(
            (
                InteractionOperation.CLICK,
                "下次再说",
                "关闭升级提示并继续进入应用",
                "home",
            ),
            page_state="popup",
            visual_page_type="startup_ad",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    # 版本提示／公告类文案仍可保留；若被 cloud_back 策略替换，则至少有底栏「<」。
    assert any(
        candidate.semantic_target == "下次再说"
        or (
            candidate.action_type is InteractionOperation.BACK
            and "底部导航栏" in candidate.semantic_target
        )
        for candidate in frontier.candidates
    )


@pytest.mark.parametrize(
    ("semantic_target", "purpose", "expected_state_hint", "risk_level"),
    [
        ("进入应用", "跳过启动广告并进入应用", "home", InteractionRiskLevel.MEDIUM),
        ("进入广告内容", "点击广告内容进入落地页", None, InteractionRiskLevel.LOW),
        ("进入应用", "进入应用", "ad_outbound", InteractionRiskLevel.LOW),
    ],
)
def test_dynamic_frontier_rejects_unsafe_visual_startup_exit(
    semantic_target: str,
    purpose: str,
    expected_state_hint: str | None,
    risk_level: InteractionRiskLevel,
) -> None:
    """中风险、广告正文或预期外跳的开屏点击仍须失败关闭。"""

    analysis = _analysis_with_candidates(
        (
            InteractionOperation.CLICK,
            semantic_target,
            purpose,
            expected_state_hint,
        ),
        page_state="startup_ad",
        visual_page_type="startup_ad",
        risk_level=risk_level,
    )

    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert all(candidate.semantic_target != semantic_target for candidate in frontier.candidates)


@pytest.mark.parametrize(
    "action_type",
    [
        InteractionOperation.BACK,
        InteractionOperation.SCROLL,
    ],
)
def test_dynamic_frontier_rejects_untrusted_non_touch_action_on_startup_blocker(
    action_type: InteractionOperation,
) -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-line-non-touch",
            artifact_id="artifact-line-non-touch",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            candidate_semantic_name="尝试恢复",
            candidate_action_type=action_type,
            candidate_purpose="尝试改变当前启动状态",
            expected_state_hint="home",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert all(candidate.action_type is not action_type for candidate in frontier.candidates)
    assert [candidate.action_type for candidate in frontier.candidates] == [
        InteractionOperation.OBSERVE
    ]


def test_dynamic_frontier_keeps_same_text_on_business_page() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-route-content",
            artifact_id="artifact-route-content",
            image_signature=SIGNATURE,
            visual_page_type="content_detail",
            candidate_semantic_name="备用线路1",
            candidate_purpose="查看内容中的线路说明",
            expected_state_hint="content_detail",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    assert any(item.semantic_target == "备用线路1" for item in frontier.candidates)


def test_dynamic_frontier_root_startup_observation_exhaustion_does_not_exit_app() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-line-observe",
            artifact_id="artifact-line-observe",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            candidate_semantic_name="观察页面状态",
            candidate_action_type=InteractionOperation.OBSERVE,
            candidate_purpose="等待线路检测结果",
            expected_state_hint="line_loading",
        ),
        observed_at=NOW,
        navigation_depth=0,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"dynamic_startup_observation_count": 5},
    )

    assert frontier.candidates == ()


def test_dynamic_frontier_nested_startup_observation_may_back_to_parent() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-line-nested",
            artifact_id="artifact-line-nested",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            candidate_semantic_name="观察页面状态",
            candidate_action_type=InteractionOperation.OBSERVE,
            candidate_purpose="等待线路检测结果",
            expected_state_hint="line_loading",
        ),
        observed_at=NOW,
        navigation_depth=1,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"dynamic_startup_observation_count": 5},
    )

    assert [item.action_type for item in frontier.candidates] == [InteractionOperation.BACK]
    assert frontier.candidates[0].metadata.get("unstable_page_back_recovery") is True


def test_startup_ad_without_ad_facts_uses_bounded_connection_recovery() -> None:
    analysis = _coverage_analysis(
        capture_id="capture-startup-misclassified",
        artifact_id="artifact-startup-misclassified",
        image_signature=SIGNATURE,
        visual_page_type="startup_ad",
        page_state="startup_ad",
        candidate_semantic_name="备用线路1",
        candidate_purpose="尝试备用线路进入首页",
        expected_state_hint="home",
    )
    initial = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )
    assert any(item.action_type is InteractionOperation.OBSERVE for item in initial.candidates)
    observe = next(
        item for item in initial.candidates if item.action_type is InteractionOperation.OBSERVE
    )
    assert observe.metadata["startup_connection_recovery_evidence"] is True

    recovered = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"dynamic_startup_observation_count": 2},
    )
    assert any(item.semantic_target == "备用线路1" for item in recovered.candidates)
    assert all(item.action_type is not InteractionOperation.BACK for item in recovered.candidates)


def test_dynamic_frontier_supplements_null_page_type_with_reobserve() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-null-page",
            artifact_id="artifact-null-page",
            image_signature=SIGNATURE,
            visual_page_type=None,
            page_state="unknown",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    observes = [
        candidate
        for candidate in frontier.candidates
        if candidate.action_type is InteractionOperation.OBSERVE
    ]
    assert len(observes) == 1
    assert observes[0].metadata["dynamic_unstable_page_reobserve"]["reobserve_count"] == 0


def test_dynamic_frontier_null_page_blocks_business_clicks() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-null-click",
            artifact_id="artifact-null-click",
            image_signature=SIGNATURE,
            visual_page_type=None,
            page_state="unknown",
            candidate_semantic_name="底栏片库",
            candidate_purpose="进入视频列表",
            expected_state_hint="video_list",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "required_page_types": ["home", "video_list"],
            "covered_page_types": ["home"],
        },
    )

    assert all(
        candidate.action_type
        in {
            InteractionOperation.OBSERVE,
            InteractionOperation.BACK,
            InteractionOperation.WARM_RESTORE,
            InteractionOperation.TIMED_CAPTURE,
        }
        for candidate in frontier.candidates
    )


def test_dynamic_frontier_content_list_loading_prefers_observe() -> None:
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-list-loading",
            artifact_id="artifact-list-loading",
            image_signature=SIGNATURE,
            visual_page_type="content_list",
            page_state="loading",
            candidate_semantic_name="内容卡片",
            candidate_purpose="进入内容详情",
            expected_state_hint="content_detail",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "required_page_types": [
                "home",
                "content_list",
                "content_detail",
            ],
            "covered_page_types": ["home"],
        },
    )

    # loading 列表由 coverage seek 闸门挡住卡片；不强制注入 startup 观察
    assert all(
        not (
            candidate.action_type is InteractionOperation.CLICK
            and candidate.metadata.get("coverage_page_seek") is True
            and candidate.metadata.get("expected_next_page_type") == "content_detail"
        )
        for candidate in frontier.candidates
    )


def test_home_popup_budget_keeps_close_until_ad_cleared() -> None:
    """已观察两帧不能替代广告净空证据，继续关闭而非穿透广告寻页。"""

    enriched = _enrich_package_boundary_analysis(
        _coverage_analysis(
            capture_id="capture-home-popup",
            artifact_id="artifact-home-popup",
            image_signature=SIGNATURE,
            visual_page_type="home",
            page_state="popup",
            candidate_semantic_name="关闭弹层",
            candidate_purpose="关闭弹窗",
            expected_state_hint="close_popup_x",
            surface_facts={"popup_visible": True},
            package_name="uk.frtju.nnxsaf",
            overlay_signature="popup:home-ad",
        ),
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("uk.frtju.nnxsaf",),
    )
    frontier = _build_frontier(
        analysis=enriched,
        observed_at=NOW,
        allowed_hosts=("uk.frtju.nnxsaf",),
        coverage_metadata={
            "required_page_types": [
                "home",
                "content_list",
                "video_list",
                "post_list",
                "activity",
            ],
            "covered_page_types": ["home"],
            "dynamic_home_popup_frame_count": 2,
        },
    )

    assert all(
        candidate.metadata.get("coverage_page_seek") is not True
        for candidate in frontier.candidates
    )
    assert any(
        candidate.action_type is InteractionOperation.BACK
        and "底部导航栏" in candidate.semantic_target
        for candidate in frontier.candidates
    )



def test_business_tab_overlay_yield_keeps_real_bottom_nav_seek() -> None:
    """业务 Tab 弹层反复关不掉后，保留当前帧真实底栏覆盖寻页。"""

    close = AppVisualCandidate.build(
        source_capture_id="capture-vl-popup",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="关闭弹层",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.45, 0.20, 0.55, 0.28),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭弹窗",
        expected_state_hint="close_popup_x",
        frontier_scope="local",
    )
    bottom = AppVisualCandidate.build(
        source_capture_id="capture-vl-popup",
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏「吃瓜」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.35, 0.90, 0.50, 0.98),
        confidence=0.94,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开可见底栏导航「吃瓜」",
        expected_state_hint="post_list",
        frontier_scope="app",
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-vl-popup",
        screenshot_artifact_id="artifact-vl-popup",
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="uk.frtju.nnxsaf",
        activity=".MainActivity",
        visual_page_type="video_list",
        visual_summary="video_list",
        key_elements=(),
        candidates=(close, bottom),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state="popup",
        overlay_signature="popup:video-ad",
        surface_facts={"popup_visible": True},
    )
    frontier = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("uk.frtju.nnxsaf",),
        coverage_metadata={
            "required_page_types": [
                "home",
                "content_list",
                "video_list",
                "post_list",
                "activity",
            ],
            "covered_page_types": ["home", "video_list"],
            "dynamic_business_tab_overlay_frame_count": 6,
        },
    )
    seeks = [
        candidate
        for candidate in frontier.candidates
        if candidate.metadata.get("coverage_page_seek") is True
        or candidate.metadata.get("frontier_scope") == "app"
    ]
    assert seeks
    assert seeks[0].semantic_target == "底栏「吃瓜」"
    assert seeks[0].metadata.get("coverage_page_seek_contract") is None


@pytest.mark.parametrize(
    ("frame_count", "dismiss_noop_streak", "expected_first"),
    [
        pytest.param(6, 0, "close-current-popup", id="six-frame-budget"),
        # noop≥2 现与满帧同走 overlay_budget 路径：关层仍保留在前，决策层再让路底栏。
        pytest.param(2, 2, "close-current-popup", id="noop-streak-two"),
    ],
)
def test_obstruction_filter_keeps_close_and_bottom_at_yield_limits(
    frame_count: int,
    dismiss_noop_streak: int,
    expected_first: str,
) -> None:
    """达到 6 帧或 noop=2 时仍保留当前帧合法关层和底栏候选。"""

    from services.api.app.application.app_screenshot_exploration import (
        _retain_only_obstruction_recovery_candidates,
    )

    close = CandidateAction(
        candidate_id="close-current-popup",
        semantic_target="关闭广告弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭当前广告弹窗",
            "expected_state_hint": "close_popup_x",
            "frontier_scope": "local",
            "control_role": "dismiss",
        },
    )
    bottom = CandidateAction(
        candidate_id="bottom-activity",
        semantic_target="底栏活动",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "打开当前帧可见底栏活动页",
            "expected_state_hint": "activity",
            "frontier_scope": "app",
            "bottom_navigation_tab_seek": True,
        },
    )
    untrusted_close = CandidateAction(
        candidate_id="close-promo-body",
        semantic_target="关闭推广弹层",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭推广弹层",
            "frontier_scope": "local",
        },
    )
    high_risk = CandidateAction(
        candidate_id="high-risk-ad-action",
        semantic_target="打开广告内容",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.HIGH,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "打开广告内容",
            "expected_state_hint": "content_detail",
            "frontier_scope": "app",
        },
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-yield-limit",
        screenshot_artifact_id="artifact-yield-limit",
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".MainActivity",
        visual_page_type="home",
        visual_summary="首页广告弹层",
        key_elements=(),
        candidates=(),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state="popup",
        overlay_signature="popup:home-ad",
        surface_facts={"popup_visible": True},
    )
    retained = _retain_only_obstruction_recovery_candidates(
        candidates=(close, bottom, untrusted_close, high_risk),
        analysis=analysis,
        coverage_metadata={
            "required_page_types": ["home", "activity"],
            "covered_page_types": ["home"],
            "dynamic_home_popup_frame_count": frame_count,
            "dismiss_noop_streak": dismiss_noop_streak,
        },
    )

    assert {item.candidate_id for item in retained} == {
        close.candidate_id,
        bottom.candidate_id,
    }
    assert retained[0].candidate_id == expected_first


def test_new_popup_frame_keeps_same_named_safe_close_after_repeated_history() -> None:
    """历史同名关闭动作不应抑制新视觉帧上的当前弹层关闭候选。"""

    from services.api.app.application.app_semantic_progress import (
        APP_SEMANTIC_ACTION_COUNTS,
        record_app_semantic_action,
    )

    def popup_analysis(
        *, capture_id: str, artifact_id: str, image_signature: str
    ) -> AppVisualAnalysis:
        return _enrich_package_boundary_analysis(
            _coverage_analysis(
                capture_id=capture_id,
                artifact_id=artifact_id,
                image_signature=image_signature,
                visual_page_type="home",
                page_state="popup",
                candidate_semantic_name="关闭广告弹窗",
                candidate_purpose="关闭广告弹窗",
                expected_state_hint="close_popup_x",
                surface_facts={"popup_visible": True},
                package_name="uk.frtju.nnxsaf",
                overlay_signature="popup:home-ad",
            ),
            surface_kind=SurfaceKind.ANDROID,
            allowed_hosts=("uk.frtju.nnxsaf",),
        )

    first_frontier = _build_frontier(
        analysis=popup_analysis(
            capture_id="capture-home-popup-history-1",
            artifact_id="artifact-home-popup-history-1",
            image_signature=SIGNATURE,
        ),
        observed_at=NOW,
        allowed_hosts=("uk.frtju.nnxsaf",),
        coverage_metadata={},
    )
    first_close = next(
        candidate
        for candidate in first_frontier.candidates
        if candidate.action_type is InteractionOperation.BACK
        and "底部导航栏" in candidate.semantic_target
    )
    semantic_counts: dict[str, int] = {}
    for _ in range(3):
        semantic_counts = record_app_semantic_action(
            semantic_counts,
            candidate=first_close,
        )

    next_frontier = _build_frontier(
        analysis=popup_analysis(
            capture_id="capture-home-popup-history-2",
            artifact_id="artifact-home-popup-history-2",
            image_signature="sha256:" + "b" * 64,
        ),
        observed_at=NOW + timedelta(seconds=1),
        allowed_hosts=("uk.frtju.nnxsaf",),
        coverage_metadata={APP_SEMANTIC_ACTION_COUNTS: semantic_counts},
    )

    close_candidates = [
        candidate
        for candidate in next_frontier.candidates
        if candidate.action_type is InteractionOperation.BACK
        and "底部导航栏" in candidate.semantic_target
    ]
    assert len(close_candidates) == 1


def test_dynamic_frontier_supplements_empty_line_loading_first_frame_with_observe() -> None:
    """线路检测首帧漏报候选时，平台仍须给出低风险零触控观察。"""
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-empty-line-loading",
            artifact_id="artifact-empty-line-loading",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            page_state="loading",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    observes = [
        candidate
        for candidate in frontier.candidates
        if candidate.action_type is InteractionOperation.OBSERVE
    ]
    assert len(observes) == 1
    assert observes[0].risk_level is InteractionRiskLevel.LOW
    assert observes[0].metadata["dynamic_startup_observation"]["observation_count"] == 0


def test_dynamic_frontier_does_not_supplement_after_startup_observation_limit() -> None:
    """线路检测观察达到上限后，即使仍无候选也不得继续生成观察。"""
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-limit-line-loading",
            artifact_id="artifact-limit-line-loading",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            page_state="loading",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={"dynamic_startup_observation_count": 5},
    )

    assert not any(
        candidate.action_type is InteractionOperation.OBSERVE for candidate in frontier.candidates
    )


def test_dynamic_frontier_keeps_provider_observe_without_duplicate() -> None:
    """Provider 已给出观察候选时，平台不得再补第二个观察。"""
    frontier = _build_frontier(
        analysis=_coverage_analysis(
            capture_id="capture-existing-line-loading",
            artifact_id="artifact-existing-line-loading",
            image_signature=SIGNATURE,
            visual_page_type="line_loading",
            page_state="loading",
            candidate_semantic_name="Provider 观察线路状态",
            candidate_action_type=InteractionOperation.OBSERVE,
            candidate_purpose="观察当前线路检测状态",
            expected_state_hint="line_loading",
        ),
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={},
    )

    observes = [
        candidate
        for candidate in frontier.candidates
        if candidate.action_type is InteractionOperation.OBSERVE
    ]
    assert len(observes) == 1
    assert observes[0].semantic_target == "Provider 观察线路状态"


def test_dedupe_frontier_candidates_keeps_first_by_id() -> None:
    """合成注入与视觉映射撞 id 时，保序保留首个候选。"""

    first = CandidateAction(
        candidate_id="same-id",
        semantic_target="合成跳过",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "synthetic-first"},
    )
    second = CandidateAction(
        candidate_id="same-id",
        semantic_target="视觉跳过",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "vision-second"},
    )
    other = CandidateAction(
        candidate_id="other-id",
        semantic_target="底栏首页",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=70,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "nav"},
    )

    kept = _dedupe_frontier_candidates((first, second, other, first))
    assert [item.candidate_id for item in kept] == ["same-id", "other-id"]
    assert kept[0].semantic_target == "合成跳过"


def test_dynamic_business_page_adds_bounded_scroll_after_navigation_candidates() -> None:
    from services.api.app.application.app_semantic_progress import (
        APP_SEMANTIC_ACTION_COUNTS,
        build_app_semantic_action_key,
    )

    analysis = _coverage_analysis(
        capture_id="capture-dynamic-scroll",
        artifact_id="artifact-dynamic-scroll",
        image_signature=SIGNATURE,
        visual_page_type="content_list",
        candidate_semantic_name="第一条内容",
        candidate_purpose="打开第一条内容详情",
        expected_state_hint="content_detail",
    )
    initial = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "business_page_scope": "nav:content-list",
            "required_page_types": ["content_list", "content_detail"],
        },
    )

    assert [item.semantic_target for item in initial.candidates] == [
        "第一条内容",
        "动态页面安全下滑",
    ]
    detail = initial.candidates[0]
    assert detail.metadata["coverage_page_seek"] is True
    assert detail.metadata["expected_next_page_type"] == "content_detail"
    assert detail.exploration_value >= 96
    assert all(item.semantic_target != "覆盖寻页：内容卡片" for item in initial.candidates)
    scroll = initial.candidates[-1]
    assert scroll.metadata["dynamic_page_sampling"] is True
    assert scroll.metadata["dynamic_page_sampling_contract"] == {
        "protocol_version": "app-dynamic-page-sampling.v1",
        "business_page_scope": "nav:content-list",
        "page_type": "content_list",
    }
    scroll_key = build_app_semantic_action_key(scroll)

    sampled_once = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "business_page_scope": "nav:content-list",
            APP_SEMANTIC_ACTION_COUNTS: {scroll_key: 1},
        },
    )
    assert any(
        item.metadata.get("dynamic_page_sampling") is True for item in sampled_once.candidates
    )

    exhausted = _build_frontier(
        analysis=analysis,
        observed_at=NOW,
        allowed_hosts=("com.example.target",),
        coverage_metadata={
            "business_page_scope": "nav:content-list",
            APP_SEMANTIC_ACTION_COUNTS: {scroll_key: 2},
        },
    )
    assert all(
        item.metadata.get("dynamic_page_sampling") is not True for item in exhausted.candidates
    )


@pytest.mark.asyncio
async def test_dynamic_app_navigation_assigns_stable_distinct_business_scopes() -> None:
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command())
    original = initial.work.frontier.candidates[0]
    profile = replace(
        original,
        semantic_target="底栏我的",
        metadata={**dict(original.metadata), "frontier_scope": "app", "purpose": "进入个人中心"},
    )
    home = replace(
        original,
        semantic_target="底栏首页",
        metadata={**dict(original.metadata), "frontier_scope": "app", "purpose": "进入首页"},
    )
    turn = _action_turn(initial.work)

    profile_scope = _next_business_page_scope(
        prior_work=initial.work,
        selected=profile,
        turn=turn,
        next_navigation_depth=0,
    )
    repeated_profile_scope = _next_business_page_scope(
        prior_work=initial.work,
        selected=profile,
        turn=turn,
        next_navigation_depth=0,
    )
    home_scope = _next_business_page_scope(
        prior_work=initial.work,
        selected=home,
        turn=turn,
        next_navigation_depth=0,
    )

    assert profile_scope == repeated_profile_scope
    assert profile_scope.startswith("nav:")
    assert profile_scope != home_scope


@pytest.mark.asyncio
async def test_repeated_stable_state_does_not_duplicate_business_page_coverage() -> None:
    goal = _coverage_goal(
        required_page_types=("home",),
        required_milestones=("surface:stable", "journey:details"),
        minimum_business_pages=1,
    )
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="home",
                visual_summary="home",
                key_elements=("home",),
                candidate_semantic_name="details",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(app_goal=goal))
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-after",
                artifact_id="artifact-after",
                image_signature=SIGNATURE,
                visual_page_type="home",
                visual_summary="home",
                key_elements=("home",),
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work),
        )
    )

    metadata = successor.work.frontier.observation.metadata
    assert successor.work.frontier.repeated_state_count == 1
    assert metadata["covered_page_types"] == ("home",)
    assert metadata["covered_milestones"] == ("surface:stable",)
    assert metadata["covered_business_state_ids"] == (
        "com.example.target|.mainactivity",
    )
    assert metadata["covered_business_page_count"] == 1
    assert metadata["dynamic_no_new_business_page_action_count"] == 1


@pytest.mark.asyncio
async def test_scroll_with_changed_screenshot_does_not_inflate_business_page_count() -> None:
    goal = _coverage_goal(
        required_page_types=("home",),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
    )
    observations = MemoryVisualObservations()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="home",
                candidate_semantic_name="向下浏览",
            )
        ),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).run(_command(app_goal=goal))
    scroll_candidate = initial.work.frontier.candidates[0]
    scroll_candidate = type(scroll_candidate)(
        candidate_id=scroll_candidate.candidate_id,
        semantic_target=scroll_candidate.semantic_target,
        action_type=InteractionOperation.SCROLL,
        risk_level=scroll_candidate.risk_level,
        exploration_value=scroll_candidate.exploration_value,
        navigation_depth=scroll_candidate.navigation_depth,
        expected_surface_kind=scroll_candidate.expected_surface_kind,
        prior_failure_count=scroll_candidate.prior_failure_count,
        metadata=scroll_candidate.metadata,
    )
    frontier = type(initial.work.frontier)(
        snapshot_id=initial.work.frontier.snapshot_id,
        observation=initial.work.frontier.observation,
        candidates=(scroll_candidate,),
        visited_state_ids=initial.work.frontier.visited_state_ids,
        blocked_candidate_ids=initial.work.frontier.blocked_candidate_ids,
        visited_candidate_ids=initial.work.frontier.visited_candidate_ids,
        visited_action_fingerprints=initial.work.frontier.visited_action_fingerprints,
        repeated_state_count=initial.work.frontier.repeated_state_count,
    )
    scroll_work = type(initial.work)(
        work_id=initial.work.work_id,
        task_id=initial.work.task_id,
        session_id=initial.work.session_id,
        branch_id=initial.work.branch_id,
        objective=initial.work.objective,
        objective_hash=initial.work.objective_hash,
        surface_kind=initial.work.surface_kind,
        source_capture_id=initial.work.source_capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=initial.work.boundaries,
        budget=initial.work.budget,
        deadline=initial.work.deadline,
        app_goal=initial.work.app_goal,
    )
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-after",
                artifact_id="artifact-after",
                image_signature="sha256:" + "b" * 64,
                visual_page_type="home",
            )
        ),
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=scroll_work,
            turn=_action_turn(scroll_work),
        )
    )

    metadata = successor.work.frontier.observation.metadata
    assert metadata["business_navigation_depth"] == 0
    assert metadata["covered_business_page_count"] == 1
    assert metadata["dynamic_repeated_business_page_action_count"] == 0
    assert successor.work.frontier.repeated_state_count == 0
    assert successor.page_type_assessment.execution_status == "accepted"
    assert successor.page_type_assessment.page_type_assessment is not None
    assert successor.page_type_assessment.page_type_assessment["page_capture_ids"] == [
        "capture-after"
    ]


def test_repeated_business_evidence_is_detected_across_navigation_paths() -> None:
    """同一 Activity/页型跨深度返回时仍属于重复证据，不得重置平台计数。"""

    goal = _coverage_goal(required_page_types=("home",), minimum_business_pages=1)
    analysis = _coverage_analysis(
        capture_id="capture-home",
        artifact_id="artifact-home",
        image_signature="sha256:" + "c" * 64,
        visual_page_type="home",
    )
    initial = _next_coverage_metadata(
        goal=goal,
        analysis=analysis,
        business_navigation_depth=0,
        business_page_scope="app_ready",
    )
    repeated = _next_coverage_metadata(
        goal=goal,
        analysis=analysis,
        prior_metadata=initial,
        business_navigation_depth=3,
        business_page_scope="nav:home",
        action_succeeded=True,
        arrival_proven=True,
        completed_action=_build_frontier(
            analysis=_analysis(),
            observed_at=NOW,
            navigation_depth=0,
            allowed_hosts=("com.example.target",),
        ).candidates[0],
    )

    assert repeated["covered_business_page_count"] == 2
    assert repeated["covered_business_evidence_ids"] == [
        "com.example.target|.mainactivity|sha256:" + "c" * 64
    ]
    assert repeated["dynamic_repeated_business_page_action_count"] == 1

    transient = _next_coverage_metadata(
        goal=goal,
        analysis=_coverage_analysis(
            capture_id="capture-transient",
            artifact_id="artifact-transient",
            image_signature="sha256:" + "d" * 64,
            visual_page_type="home",
            page_state="unknown",
        ),
        prior_metadata=repeated,
        business_navigation_depth=3,
        business_page_scope="nav:home",
        completed_action=_build_frontier(
            analysis=_analysis(),
            observed_at=NOW,
            navigation_depth=0,
            allowed_hosts=("com.example.target",),
        ).candidates[0],
    )

    assert transient["dynamic_repeated_business_page_action_count"] == 1

    overlay = _next_coverage_metadata(
        goal=goal,
        analysis=_coverage_analysis(
            capture_id="capture-overlay",
            artifact_id="artifact-overlay",
            image_signature="sha256:" + "e" * 64,
            visual_page_type="home",
            page_state="popup",
        ),
        prior_metadata=transient,
        business_navigation_depth=4,
        business_page_scope="nav:home",
        completed_action=_build_frontier(
            analysis=_analysis(),
            observed_at=NOW,
            navigation_depth=0,
            allowed_hosts=("com.example.target",),
        ).candidates[0],
    )

    assert overlay["dynamic_repeated_business_page_action_count"] == 2


def test_popup_close_to_new_page_type_resets_plateau_without_spending_progress() -> None:
    """关闭弹层后确认进入新页型时清零两类 plateau，关闭动作本身不计账。"""

    goal = _coverage_goal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable",),
        minimum_business_pages=2,
    )
    initial = _next_coverage_metadata(
        goal=goal,
        analysis=_confirmed_fixture(
            _coverage_analysis(
                capture_id="capture-home",
                artifact_id="artifact-home",
                image_signature="sha256:" + "a" * 64,
                visual_page_type="home",
            )
        ),
    )
    prior = {
        **initial,
        "dynamic_no_new_business_page_action_count": 6,
        "dynamic_repeated_business_page_action_count": 3,
    }
    result = _next_coverage_metadata(
        goal=goal,
        analysis=_confirmed_fixture(
            _coverage_analysis(
                capture_id="capture-profile",
                artifact_id="artifact-profile",
                image_signature="sha256:" + "b" * 64,
                visual_page_type="profile",
            ),
            object_id="profile-object",
        ),
        prior_metadata=prior,
        completed_action=_coverage_popup_close_action(),
        action_succeeded=True,
        arrival_proven=True,
        count_completed_action_for_business_progress=False,
    )

    assert result["covered_page_types"] == ["home", "profile"]
    assert result["dynamic_no_new_business_page_action_count"] == 0
    assert result["dynamic_repeated_business_page_action_count"] == 0


def test_popup_close_to_new_stable_evidence_clears_repeated_only() -> None:
    """同页型的全新稳定 evidence 清 repeated，但不冒充新页型进展。"""

    goal = _coverage_goal(
        required_page_types=("profile",),
        required_milestones=("surface:stable",),
        minimum_business_pages=2,
    )
    first = _coverage_analysis(
        capture_id="capture-profile-home",
        artifact_id="artifact-profile-home",
        image_signature="sha256:" + "c" * 64,
        visual_page_type="profile",
        surface_facts={
            "visual_page_name": {
                "protocol_version": "app-visual-page-name.v1",
                "page_name": "个人中心",
                "source": "top_bar",
                "confidence": 0.96,
            }
        },
    )
    initial = _next_coverage_metadata(goal=goal, analysis=first)
    prior = {
        **initial,
        "dynamic_no_new_business_page_action_count": 6,
        "dynamic_repeated_business_page_action_count": 3,
    }
    result = _next_coverage_metadata(
        goal=goal,
        analysis=_coverage_analysis(
            capture_id="capture-profile-invite",
            artifact_id="artifact-profile-invite",
            image_signature="sha256:" + "d" * 64,
            visual_page_type="profile",
            surface_facts={
                "visual_page_name": {
                    "protocol_version": "app-visual-page-name.v1",
                    "page_name": "分享邀请",
                    "source": "page_landmark",
                    "confidence": 0.92,
                }
            },
        ),
        prior_metadata=prior,
        completed_action=_coverage_popup_close_action(),
        action_succeeded=True,
        arrival_proven=True,
        count_completed_action_for_business_progress=False,
    )

    assert result["covered_page_types"] == ["profile"]
    assert result["covered_business_page_count"] == 2
    assert result["dynamic_no_new_business_page_action_count"] == 6
    assert result["dynamic_repeated_business_page_action_count"] == 0


@pytest.mark.parametrize(
    ("page_state", "arrival_proven", "overlay_signature", "surface_facts"),
    (
        pytest.param("stable", False, None, {}, id="unproven-arrival"),
        pytest.param(
            "popup",
            True,
            "profile-popup",
            {"popup_visible": True},
            id="obstructed-arrival",
        ),
    ),
)
def test_unproven_or_obstructed_new_page_keeps_plateau_counts(
    page_state: str,
    arrival_proven: bool,
    overlay_signature: str | None,
    surface_facts: dict[str, object],
) -> None:
    """未证明或仍被弹层遮挡时不能按新页型清掉真实 plateau。"""

    goal = _coverage_goal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable",),
        minimum_business_pages=2,
    )
    initial = _next_coverage_metadata(
        goal=goal,
        analysis=_confirmed_fixture(
            _coverage_analysis(
                capture_id="capture-home",
                artifact_id="artifact-home",
                image_signature="sha256:" + "7" * 64,
                visual_page_type="home",
            )
        ),
    )
    prior = {
        **initial,
        "dynamic_no_new_business_page_action_count": 6,
        "dynamic_repeated_business_page_action_count": 3,
    }
    result = _next_coverage_metadata(
        goal=goal,
        analysis=_coverage_analysis(
            capture_id="capture-profile-not-proven",
            artifact_id="artifact-profile-not-proven",
            image_signature="sha256:" + "8" * 64,
            visual_page_type="profile",
            page_state=page_state,
            overlay_signature=overlay_signature,
            surface_facts=surface_facts,
        ),
        prior_metadata=prior,
        completed_action=_coverage_popup_close_action(),
        action_succeeded=True,
        arrival_proven=arrival_proven,
        count_completed_action_for_business_progress=False,
    )

    assert result["covered_page_types"] == ["home"]
    assert result["covered_business_page_count"] == 1
    assert result["dynamic_no_new_business_page_action_count"] == 6
    assert result["dynamic_repeated_business_page_action_count"] == 3


def test_popup_close_on_existing_business_evidence_keeps_plateau_unchanged() -> None:
    """关闭已见同页弹层既不清零，也不增加业务停滞计数。"""

    goal = _coverage_goal(
        required_page_types=("home",),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
    )
    initial = _next_coverage_metadata(
        goal=goal,
        analysis=_coverage_analysis(
            capture_id="capture-home-before",
            artifact_id="artifact-home-before",
            image_signature="sha256:" + "e" * 64,
            visual_page_type="home",
        ),
    )
    prior = {
        **initial,
        "dynamic_no_new_business_page_action_count": 6,
        "dynamic_repeated_business_page_action_count": 3,
    }
    result = _next_coverage_metadata(
        goal=goal,
        analysis=_coverage_analysis(
            capture_id="capture-home-after",
            artifact_id="artifact-home-after",
            image_signature="sha256:" + "e" * 64,
            visual_page_type="home",
        ),
        prior_metadata=prior,
        completed_action=_coverage_popup_close_action(),
        action_succeeded=True,
        arrival_proven=True,
        count_completed_action_for_business_progress=False,
    )

    assert result["dynamic_no_new_business_page_action_count"] == 6
    assert result["dynamic_repeated_business_page_action_count"] == 3


def test_ordinary_repeated_business_action_still_increments_plateau() -> None:
    """普通已证明业务动作仍按原规则累计重复与无新增计数。"""

    goal = _coverage_goal(
        required_page_types=("home",),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
    )
    analysis = _coverage_analysis(
        capture_id="capture-home",
        artifact_id="artifact-home",
        image_signature="sha256:" + "1" * 64,
        visual_page_type="home",
    )
    initial = _next_coverage_metadata(goal=goal, analysis=analysis)
    prior = {
        **initial,
        "dynamic_no_new_business_page_action_count": 6,
        "dynamic_repeated_business_page_action_count": 3,
    }
    ordinary_action = _build_frontier(
        analysis=_analysis(),
        observed_at=NOW,
        navigation_depth=0,
        allowed_hosts=("com.example.target",),
    ).candidates[0]
    result = _next_coverage_metadata(
        goal=goal,
        analysis=analysis,
        prior_metadata=prior,
        completed_action=ordinary_action,
        action_succeeded=True,
        arrival_proven=True,
    )

    assert result["dynamic_no_new_business_page_action_count"] == 7
    assert result["dynamic_repeated_business_page_action_count"] == 4


@pytest.mark.parametrize(
    (
        "outcome",
        "action_succeeded",
        "after_popup",
        "arrival_proven",
        "after_overlay_signature",
        "expected_noop",
    ),
    (
        pytest.param(
            "unproven",
            True,
            True,
            False,
            "popup-after",
            0,
            id="unproven-different-overlay",
        ),
        pytest.param(
            "unproven",
            True,
            True,
            False,
            "popup-before",
            1,
            id="unproven-same-overlay",
        ),
        pytest.param(
            "obstruction_removed",
            True,
            False,
            True,
            None,
            0,
            id="obstruction-removed",
        ),
        pytest.param(
            "intermediate",
            True,
            True,
            False,
            "popup-after",
            0,
            id="intermediate-structure",
        ),
        pytest.param(
            "unproven",
            False,
            True,
            False,
            "popup-after",
            0,
            id="touch-not-succeeded",
        ),
    ),
)
def test_popup_close_coverage_metadata_distinguishes_overlay_fingerprint(
    outcome: str,
    action_succeeded: bool,
    after_popup: bool,
    arrival_proven: bool,
    after_overlay_signature: str | None,
    expected_noop: int,
) -> None:
    """尚未清障时仍比较弹层指纹，不把下一张广告直接记为无效点击。"""

    close = CandidateAction(
        candidate_id="close-popup",
        semantic_target="关闭弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭当前弹窗",
            "expected_state_hint": "close_popup_x",
            "frontier_scope": "local",
        },
    )
    before = _coverage_analysis(
        capture_id="capture-popup-before",
        artifact_id="artifact-popup-before",
        image_signature="sha256:" + "a" * 64,
        visual_page_type="home",
        page_state="popup",
        overlay_signature="popup-before",
        surface_facts={"popup_visible": True},
    )
    after = _coverage_analysis(
        capture_id="capture-popup-after",
        artifact_id="artifact-popup-after",
        image_signature="sha256:" + "b" * 64,
        visual_page_type="home",
        page_state="popup" if after_popup else "stable",
        overlay_signature=after_overlay_signature if after_popup else None,
        surface_facts={"popup_visible": True} if after_popup else {},
    )
    assert before.image_signature != after.image_signature
    if after_popup and after_overlay_signature != before.overlay_signature:
        assert before.overlay_signature != after.overlay_signature

    prior_metadata = {
        BOUNDED_EXPLORATION_STATE_KEY: {
            "max_actions": 5,
            "no_progress_count": 0,
            "last_popup_fingerprint": before.overlay_signature,
        },
        "dismiss_noop_streak": 0,
    }
    result = _next_coverage_metadata(
        goal=_coverage_goal(
            required_page_types=("home",),
            required_milestones=("surface:stable",),
            minimum_business_pages=1,
        ),
        analysis=after,
        prior_metadata=prior_metadata,
        prior_state_version=before.image_signature,
        completed_action=close,
        action_succeeded=action_succeeded,
        arrival_proven=arrival_proven,
        action_observation_outcome=outcome,
    )

    scheduler_state = result[BOUNDED_EXPLORATION_STATE_KEY]
    assert isinstance(scheduler_state, Mapping)
    assert result["dismiss_noop_streak"] == expected_noop
    assert scheduler_state["no_progress_count"] == expected_noop
    if after_popup:
        assert result["covered_page_types"] == []
        assert result["covered_business_page_count"] == 0


def test_popup_close_same_overlay_signature_accumulates_noop_streak() -> None:
    """同一弹层连续两次无效点击分别累计 1 和 2 次无进展。"""

    close = CandidateAction(
        candidate_id="close-popup",
        semantic_target="关闭弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭当前弹窗",
            "expected_state_hint": "close_popup_x",
            "frontier_scope": "local",
        },
    )
    analysis = _coverage_analysis(
        capture_id="capture-popup-same-overlay",
        artifact_id="artifact-popup-same-overlay",
        image_signature="sha256:" + "c" * 64,
        visual_page_type="home",
        page_state="popup",
        overlay_signature="popup-same",
        surface_facts={"popup_visible": True},
    )
    prior_metadata: dict[str, object] = {
        BOUNDED_EXPLORATION_STATE_KEY: {
            "max_actions": 5,
            "no_progress_count": 0,
            "last_popup_fingerprint": analysis.overlay_signature,
        },
        "dismiss_noop_streak": 0,
    }

    for expected_noop in (1, 2):
        result = _next_coverage_metadata(
            goal=_coverage_goal(
                required_page_types=("home",),
                required_milestones=("surface:stable",),
                minimum_business_pages=1,
            ),
            analysis=analysis,
            prior_metadata=prior_metadata,
            prior_state_version=analysis.image_signature,
            completed_action=close,
            action_succeeded=True,
            arrival_proven=False,
            action_observation_outcome="unproven",
        )
        scheduler_state = result[BOUNDED_EXPLORATION_STATE_KEY]
        assert isinstance(scheduler_state, Mapping)
        assert result["dismiss_noop_streak"] == expected_noop
        assert scheduler_state["no_progress_count"] == expected_noop
        assert result["covered_page_types"] == []
        assert result["covered_business_page_count"] == 0
        prior_metadata = result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (InteractionResultStatus.FAILED, InteractionResultStatus.BLOCKED),
)
async def test_failed_or_blocked_action_candidate_does_not_cover_milestone(
    status: InteractionResultStatus,
) -> None:
    goal = _coverage_goal(
        required_page_types=("home",),
        required_milestones=("journey:details",),
        minimum_business_pages=1,
    )
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="home",
                visual_summary="home",
                key_elements=("home",),
                candidate_semantic_name="details",
                candidate_purpose="open details",
                expected_state_hint="details",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(app_goal=goal))
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-after",
                artifact_id="artifact-after",
                image_signature="sha256:" + "b" * 64,
                visual_page_type="home",
                visual_summary="home",
                key_elements=("home",),
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work, status=status),
        )
    )

    metadata = successor.work.frontier.observation.metadata
    assert metadata["covered_page_types"] == ("home",)
    assert metadata["covered_milestones"] == ()
    assert metadata["covered_business_page_count"] == 1


@pytest.mark.asyncio
async def test_successor_work_round_trip_preserves_coverage_metadata_and_goal() -> None:
    goal = _coverage_goal()
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-1",
                artifact_id="artifact-1",
                image_signature=SIGNATURE,
                visual_page_type="home",
                visual_summary="home",
                key_elements=("details",),
                candidate_semantic_name="details",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(app_goal=goal))
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _coverage_analysis(
                capture_id="capture-after",
                artifact_id="artifact-after",
                image_signature="sha256:" + "b" * 64,
                visual_page_type="content_detail",
                visual_summary="details",
                key_elements=("details",),
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work),
        )
    )

    replay = ExplorationWork.from_dict(successor.work.to_dict())

    assert replay.to_dict() == successor.work.to_dict()
    assert replay.frontier.observation.metadata == successor.work.frontier.observation.metadata
    assert replay.app_goal == successor.work.app_goal == goal


def _after_capture_analysis(
    *,
    capture_id: str = "capture-after",
    artifact_id: str = "artifact-after",
    action_type: InteractionOperation = InteractionOperation.CLICK,
    semantic_name: str = "订单入口",
    purpose: str = "进入订单页",
) -> AppVisualAnalysis:
    signature = "sha256:" + "b" * 64
    candidate = AppVisualCandidate.build(
        source_capture_id=capture_id,
        source_image_signature=signature,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=action_type,
        normalized_bounds=NormalizedBounds(0.1, 0.2, 0.7, 0.3),
        confidence=0.9,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
    )
    return AppVisualAnalysis.build(
        capture_id=capture_id,
        screenshot_artifact_id=artifact_id,
        image_signature=signature,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".HomeActivity",
        visual_page_type="home",
        visual_summary="首页",
        key_elements=(semantic_name,),
        candidates=(candidate,),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state="stable",
    )


def _action_turn(
    work,
    *,
    before_capture_id: str = "capture-before",
    after_capture_id: str = "capture-after",
    action_execution_id: str = "action-login",
    after_artifact_id: str = "artifact-after",
    parameters: dict[str, object] | None = None,
    status: InteractionResultStatus = InteractionResultStatus.SUCCEEDED,
    error_code: str | None = None,
    retryable: bool = False,
    action_type: InteractionOperation | None = None,
    semantic_target: str | None = None,
    purpose: str = "进入系统",
) -> ExplorerTurnResult:
    selected = next(
        (
            candidate
            for candidate in work.frontier.candidates
            if action_type is None or candidate.action_type is action_type
            if semantic_target is None or candidate.semantic_target == semantic_target
        ),
        work.frontier.candidates[0],
    )
    intent = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"intent:{action_execution_id}",
        idempotency_key=f"intent:{action_execution_id}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=selected.action_type,
        semantic_target=selected.semantic_target,
        parameters={"candidate_id": selected.candidate_id, **(parameters or {})},
        purpose=purpose,
        risk_level=selected.risk_level,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            selected.action_type,
        ),
        deadline=work.deadline,
    )
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id=before_capture_id,
        surface_kind=work.surface_kind,
        capture_kind=CaptureKind.APP,
        role="before_action",
        artifact_ids=("artifact-before",),
        parent_capture_id=work.source_capture_id,
        action_execution_id=action_execution_id,
    )
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id=after_capture_id,
        surface_kind=work.surface_kind,
        capture_kind=CaptureKind.APP,
        role="after_action",
        artifact_ids=(after_artifact_id,),
        parent_capture_id=before.capture_id,
        action_execution_id=action_execution_id,
    )
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=intent,
        coordination=CaptureCoordinationResult(
            before_capture=before,
            interaction=InteractionResult(
                task_id=work.task_id,
                session_id=work.session_id,
                intent_id=intent.intent_id,
                action_execution_id=action_execution_id,
                source_capture_id=before.capture_id,
                result_capture_id=after.capture_id,
                status=status,
                retryable=retryable,
                error_code=error_code or (
                    "driver_failed"
                    if status is InteractionResultStatus.FAILED
                    else "driver_blocked"
                    if status is InteractionResultStatus.BLOCKED
                    else None
                ),
            ),
            after_capture=after,
        ),
    )


def _observation_turn(
    work,
    *,
    capture_id: str = "capture-after",
    artifact_id: str = "artifact-after",
    phase_key: str | None = None,
) -> ExplorerTurnResult:
    intent = TimedObservationIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        phase_run_id="phase-1",
        decision_id="decision-1",
        observation_id="observation-1",
        source_capture_id=work.source_capture_id,
        surface_kind=work.surface_kind,
        delay_ms=0,
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot", "ui_tree"),
            inherit_parent_semantics=True,
        ),
        deadline=work.deadline,
        phase_key=phase_key,
        protocol_version=(
            "timed-observation-intent.v2"
            if phase_key is not None
            else "timed-observation-intent.v1"
        ),
    )
    capture = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id=capture_id,
        surface_kind=work.surface_kind,
        capture_kind=CaptureKind.APP,
        role="observation",
        artifact_ids=(artifact_id,),
        parent_capture_id=work.source_capture_id,
        action_execution_id=None,
    )
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=intent,
        coordination=None,
        observation_capture=capture,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_type", "expected_navigation_depth"),
    (
        (InteractionOperation.CLICK, 0),
        (InteractionOperation.SCROLL, 0),
        (InteractionOperation.BACK, 0),
    ),
)
async def test_unproven_action_builds_idempotent_successor_without_false_visits(
    action_type: InteractionOperation,
    expected_navigation_depth: int,
) -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    bootstrap_workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis(action_type=action_type)),
        observations=observations,
        orchestrator=orchestrator,
    )
    initial = await bootstrap_workflow.run(_command())
    after_analysis = _after_capture_analysis()
    understanding = RecordingUnderstanding(after_analysis)
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=orchestrator,
    )
    turn = _action_turn(initial.work)
    command = ContinueAppScreenshotExplorationCommand(
        prior_work=initial.work,
        turn=turn,
    )

    first = await workflow.prepare_after_action(command)
    replay = await workflow.prepare_after_action(command)

    successor = first.work
    assert replay.work.work_id == successor.work_id
    assert successor.work_id != initial.work.work_id
    assert successor.source_capture_id == "capture-after"
    assert successor.branch_id == "app:capture-after"
    assert successor.budget.remaining_actions == 4
    assert successor.budget.remaining_duration_ms == 29_975
    assert successor.budget.max_actions == initial.work.budget.max_actions
    assert successor.frontier.candidates[0].navigation_depth == expected_navigation_depth
    assert successor.frontier.visited_state_ids == (
        initial.work.frontier.observation.state_version,
    )
    assert successor.frontier.visited_candidate_ids == ()
    assert successor.frontier.visited_action_fingerprints == ()
    assert (
        successor.frontier.observation.metadata["app_action_observation"]["outcome"] == "unproven"
    )
    assert successor.frontier.repeated_state_count == 0
    assert successor.frontier.observation.metadata["parent_work_id"] == initial.work.work_id
    assert successor.frontier.observation.metadata["parent_branch_id"] == initial.work.branch_id
    assert successor.frontier.observation.metadata["parent_capture_id"] == "capture-1"
    assert (
        successor.frontier.observation.metadata["parent_candidate_id"]
        == initial.work.frontier.candidates[0].candidate_id
    )
    assert successor.frontier.observation.metadata["action_execution_id"] == "action-login"
    assert set(observations.saved) == {
        "capture-1",
        "capture-before",
        "capture-after",
    }
    assert [capture.capture_id for capture in understanding.captures] == [
        "capture-before",
        "capture-after",
    ]
    assert len(orchestrator.commands) == 1
    assert len(orchestrator.prepared_commands) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "action_type", "expected_cursor"),
    (
        # wait_line 为 OBSERVE 课纲相：交互成功且阶段匹配即可推进，不要求 arrival_proven。
        (InteractionResultStatus.SUCCEEDED, InteractionOperation.OBSERVE, 1),
        (InteractionResultStatus.FAILED, InteractionOperation.OBSERVE, 0),
    ),
)
async def test_action_success_monotonically_persists_curriculum_progress(
    status: InteractionResultStatus,
    action_type: InteractionOperation,
    expected_cursor: int,
) -> None:
    progress = _progress_at_phase("wait_line")
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis(action_type=action_type)),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(curriculum_progress=progress))
    turn = _action_turn(
        initial.work,
        parameters={"app_curriculum_phase_id": "wait_line"},
        status=status,
        action_type=action_type,
    )

    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=turn,
        )
    )

    restored = AppCurriculumProgress.from_dict(
        successor.work.frontier.observation.metadata["app_curriculum_progress"]
    )
    assert restored.current_phase_action_count == 0
    assert restored.phase_cursor == expected_cursor
    assert successor.work.frontier.observation.metadata["app_runtime_policy"]["scan_mode"] == "full"


@pytest.mark.asyncio
async def test_surface_phase_skip_handoff_advances_only_explicit_hlw_skip_candidate() -> None:
    playbook = resolve_app_capture_playbook(apk_filename="hlwapp_1.7.2_260819_3.apk")
    progress = build_app_curriculum_progress(playbook)
    while progress.current_phase_id != "tap_skip":
        assert progress.current_phase_id is not None
        progress = progress.advance(
            phase_id=progress.current_phase_id,
            action_succeeded=True,
        )

    async def _successor_for(
        *,
        semantic_name: str,
        purpose: str,
        expected_state_hint: str | None,
        action_purpose: str,
        page_state: str = "stable",
        action_type: InteractionOperation = InteractionOperation.CLICK,
    ) -> AppCurriculumProgress:
        observations = MemoryVisualObservations()
        orchestrator = RecordingOrchestrator()
        initial = await AppScreenshotExplorationWorkflow(
            understanding=RecordingUnderstanding(
                _analysis_with_candidates(
                    (
                        action_type,
                        semantic_name,
                        purpose,
                        expected_state_hint,
                    ),
                    page_state=page_state,
                    surface_facts=({"popup_visible": True} if page_state == "popup" else None),
                )
            ),
            observations=observations,
            orchestrator=orchestrator,
        ).run(_command(curriculum_progress=progress))
        turn = _action_turn(
            initial.work,
            parameters={
                "app_curriculum_phase_id": "tap_skip",
                "surface_phase_run_id": "phase-popup-1",
                "surface_phase_key": "popup_chain",
                "surface_phase_decision_id": "decision-popup-1",
            },
            purpose=action_purpose,
            action_type=action_type,
        )
        before = _coverage_analysis(
            capture_id="capture-before", artifact_id="artifact-before",
            image_signature="sha256:" + "c" * 64, visual_page_type=(
                "home" if page_state == "popup" else "startup_ad"
            ), page_state=page_state,
        )
        successor = await AppScreenshotExplorationWorkflow(
            understanding=RecordingUnderstanding(
                _confirmed_fixture(_after_capture_analysis()),
                analyses_by_capture={"capture-before": before},
            ),
            observations=observations,
            orchestrator=orchestrator,
        ).prepare_after_action(
            ContinueAppScreenshotExplorationCommand(
                prior_work=initial.work,
                turn=turn,
            )
        )
        return AppCurriculumProgress.from_dict(
            successor.work.frontier.observation.metadata["app_curriculum_progress"]
        )

    skipped = await _successor_for(
        semantic_name="跳过",
        purpose="跳过开屏广告",
        expected_state_hint="tap_skip",
        action_purpose="Surface Phase 成功交接右上跳过动作 phase_id=tap_skip",
    )
    assert skipped.phase_cursor == progress.phase_cursor + 1
    assert skipped.current_phase_id == "wait_init"

    popup_closed = await _successor_for(
        semantic_name="底部导航栏「<」关闭弹窗",
        purpose="弹窗广告仍在：连点两下底部系统导航栏最右侧「<」关窗（cloud_back_tap_twice）",
        expected_state_hint="close_popup_x",
        action_purpose="fallback phase_id=tap_skip 但当前仅为弹层 cloud_back 关闭",
        page_state="popup",
        action_type=InteractionOperation.BACK,
    )
    assert popup_closed.phase_cursor == progress.phase_cursor
    assert popup_closed.current_phase_id == "tap_skip"


@pytest.mark.asyncio
async def test_successful_skip_click_witness_is_consumed_after_wait_and_splash_observations() -> (
    None
):
    playbook = resolve_app_capture_playbook(apk_filename="hlwapp_1.7.2_260819_3.apk")
    progress = build_app_curriculum_progress(playbook)
    while progress.current_phase_id != "wait_line":
        assert progress.current_phase_id is not None
        progress = progress.advance(
            phase_id=progress.current_phase_id,
            action_succeeded=True,
        )
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.CLICK,
                    "跳过广告",
                    "关闭启动广告并进入应用",
                    "广告关闭后显示主页面",
                ),
                page_state="startup_ad",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(curriculum_progress=progress))

    skip_turn = _action_turn(
        initial.work,
        action_execution_id="action-skip",
        parameters={"app_curriculum_phase_id": "wait_line"},
        action_type=InteractionOperation.CLICK,
        purpose="模型 phase_id=wait_line，但候选自身明确为跳过",
    )
    current = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _confirmed_fixture(_after_capture_analysis(
                action_type=InteractionOperation.OBSERVE,
                semantic_name="课纲观察",
                purpose="观察当前画面",
            )),
            analyses_by_capture={"capture-before": _coverage_analysis(
                capture_id="capture-before", artifact_id="artifact-before",
                image_signature="sha256:" + "c" * 64, visual_page_type="startup_ad",
                page_state="startup_ad",
            )},
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=skip_turn,
        )
    )
    first_after = True
    max_iters = 20
    for _ in range(max_iters):
        restored = AppCurriculumProgress.from_dict(
            current.work.frontier.observation.metadata["app_curriculum_progress"]
        )
        if first_after:
            assert restored.current_phase_id == "wait_line"
            assert restored.witnessed_phase_ids == ("tap_skip",)
        if restored.current_phase_id == "wait_init":
            assert restored.witnessed_phase_ids == ()
            break
        assert restored.current_phase_id in {"wait_line", "splash_burst"}
        capture_index = len(orchestrator.prepared_commands) + 1
        capture_id = f"capture-observe-{capture_index}"
        artifact_id = f"artifact-observe-{capture_index}"
        turn = _action_turn(
            current.work,
            before_capture_id=f"capture-before-{capture_index}",
            after_capture_id=capture_id,
            after_artifact_id=artifact_id,
            action_execution_id=f"action-observe-{capture_index}",
            action_type=InteractionOperation.OBSERVE,
            parameters={"app_curriculum_phase_id": restored.current_phase_id},
            purpose=f"观察课纲阶段 {restored.current_phase_id}",
        )
        current = await AppScreenshotExplorationWorkflow(
            understanding=RecordingUnderstanding(
                _after_capture_analysis(
                    capture_id=capture_id,
                    artifact_id=artifact_id,
                    action_type=InteractionOperation.OBSERVE,
                    semantic_name="课纲观察",
                    purpose="观察当前画面",
                )
            ),
            observations=observations,
            orchestrator=orchestrator,
        ).prepare_after_action(
            ContinueAppScreenshotExplorationCommand(
                prior_work=current.work,
                turn=turn,
            )
        )
        first_after = False
    else:
        restored = AppCurriculumProgress.from_dict(
            current.work.frontier.observation.metadata["app_curriculum_progress"]
        )
        raise AssertionError(
            f"课纲观察推进超过 {max_iters} 次仍未到 wait_init："
            f"current_phase_id={restored.current_phase_id!r}"
        )


@pytest.mark.asyncio
async def test_tab_click_keeps_business_depth_while_detail_click_increases_it() -> None:
    progress = _progress_at_phase("tab:首页")
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(curriculum_progress=progress))

    tab_successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _confirmed_fixture(_after_capture_analysis()),
            analyses_by_capture={"capture-before": _confirmed_before()},
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(
                initial.work,
                parameters={"app_curriculum_phase_id": "tab:首页"},
            ),
        )
    )

    assert tab_successor.work.frontier.observation.metadata["business_navigation_depth"] == 0

    plain_initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command())
    detail_successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _confirmed_fixture(_after_capture_analysis(), object_id="作品编号 123"),
            analyses_by_capture={"capture-before": _confirmed_before()},
        ),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=plain_initial.work,
            turn=_action_turn(plain_initial.work),
        )
    )

    assert detail_successor.work.frontier.observation.metadata["business_navigation_depth"] == 1


@pytest.mark.asyncio
async def test_unproven_global_navigation_preserves_depth_and_is_not_visited() -> None:
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command())
    selected = initial.work.frontier.candidates[0]
    global_candidate = replace(
        selected,
        metadata={
            **dict(selected.metadata),
            "frontier_scope": "app",
            "expected_next_page_type": "content_detail",
        },
    )
    prior = replace(
        initial.work,
        frontier=replace(
            initial.work.frontier,
            observation=replace(
                initial.work.frontier.observation,
                metadata={
                    **dict(initial.work.frontier.observation.metadata),
                    "business_navigation_depth": 3,
                },
            ),
            candidates=(global_candidate,),
        ),
    )
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=prior,
            turn=_action_turn(prior),
        )
    )

    metadata = successor.work.frontier.observation.metadata
    assert metadata["business_navigation_depth"] == 3
    assert global_candidate.candidate_id not in successor.work.frontier.visited_candidate_ids
    assert metadata["app_semantic_action_counts"] == {}


@pytest.mark.asyncio
async def test_nested_navigation_depth_never_exceeds_budget_limit() -> None:
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).run(_command())
    prior = replace(
        initial.work,
        frontier=replace(
            initial.work.frontier,
            observation=replace(
                initial.work.frontier.observation,
                metadata={
                    **dict(initial.work.frontier.observation.metadata),
                    "business_navigation_depth": 3,
                },
            ),
        ),
    )
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=MemoryVisualObservations(),
        orchestrator=RecordingOrchestrator(),
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=prior,
            turn=_action_turn(prior),
        )
    )

    assert successor.work.frontier.observation.metadata["business_navigation_depth"] == 3


@pytest.mark.asyncio
async def test_back_from_nested_business_page_decrements_depth() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    nested = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _confirmed_fixture(_after_capture_analysis(
                action_type=InteractionOperation.BACK,
                semantic_name="返回上一页",
                purpose="返回首页",
            ), object_id="作品编号 123"),
            analyses_by_capture={"capture-before": _confirmed_before()},
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work),
        )
    )
    restored = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _confirmed_fixture(_coverage_analysis(
                capture_id="capture-after-2", artifact_id="artifact-after-2",
                image_signature="sha256:" + "d" * 64, visual_page_type="home",
            )),
            analyses_by_capture={"capture-before-2": _confirmed_fixture(
                _after_capture_analysis(
                    capture_id="capture-before-2", artifact_id="artifact-before",
                ), object_id="作品编号 123",
            )},
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=nested.work,
            turn=_action_turn(
                nested.work,
                before_capture_id="capture-before-2",
                after_capture_id="capture-after-2",
                after_artifact_id="artifact-after-2",
                action_execution_id="action-back",
            ),
        )
    )

    assert nested.work.frontier.observation.metadata["business_navigation_depth"] == 1
    assert nested.work.frontier.observation.metadata["business_page_scope"] == "depth:1"
    proof = restored.work.frontier.observation.metadata["app_action_observation"]
    assert proof["outcome"] == "business_arrived", proof["reason"]
    assert restored.work.frontier.observation.metadata["business_navigation_depth"] == 0
    assert restored.work.frontier.observation.metadata["business_page_scope"] == "app_ready"


@pytest.mark.asyncio
async def test_before_visual_failure_stops_before_after_frontier() -> None:
    """动作前 Vision 失败必须 fail-closed，禁止静默续跑后继并白烧预算。"""
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    visual_error = RuntimeError("vision unavailable")
    understanding = RecordingUnderstanding(
        _after_capture_analysis(),
        errors_by_capture={"capture-before": visual_error},
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=orchestrator,
    )

    with pytest.raises(AppBeforeActionVisualUnderstandingError) as exc_info:
        await workflow.prepare_after_action(
            ContinueAppScreenshotExplorationCommand(
                prior_work=initial.work,
                turn=_action_turn(initial.work),
            )
        )

    assert exc_info.value.error_code == "APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED"
    assert exc_info.value.retryable is False
    assert exc_info.value.capture_id == "capture-before"
    assert exc_info.value.__cause__ is visual_error
    assert "capture-before" not in observations.saved
    assert "capture-after" not in observations.saved
    assert [capture.capture_id for capture in understanding.captures] == ["capture-before"]
    assert orchestrator.prepared_commands == []


@pytest.mark.asyncio
async def test_action_capture_preserves_surface_phase_lineage_for_next_turn() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    bootstrap_workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    )
    initial = await bootstrap_workflow.run(_command())
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    )
    turn = _action_turn(
        initial.work,
        parameters={
            "surface_phase_run_id": "phase-startup-1",
            "surface_phase_key": "startup_ad_countdown",
            "surface_phase_decision_id": "decision-close-1",
        },
    )

    result = await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=turn,
        )
    )

    metadata = result.work.frontier.observation.metadata
    assert metadata["surface_phase_run_id"] == "phase-startup-1"
    assert metadata["surface_phase_key"] == "startup_ad_countdown"
    assert metadata["surface_phase_decision_id"] == "decision-close-1"
    assert metadata["surface_phase_close_attempt_count"] == 1


@pytest.mark.asyncio
async def test_surface_phase_popup_close_does_not_advance_business_plateau() -> None:
    """连续广告清障不能耗尽业务 Frontier 的无新增计数。"""

    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_surface_facts(
                page_state="popup",
                overlay_signature="popup-layer-1",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    prior_work = replace(
        initial.work,
        frontier=replace(
            initial.work.frontier,
            observation=replace(
                initial.work.frontier.observation,
                metadata={
                    **initial.work.frontier.observation.metadata,
                    "dynamic_no_new_business_page_action_count": 6,
                },
            ),
        ),
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_surface_facts(
                page_state="popup",
                overlay_signature="popup-layer-2",
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=prior_work,
            turn=_action_turn(
                prior_work,
                parameters={
                    "surface_phase_run_id": "phase-popup-1",
                    "surface_phase_key": "popup_chain",
                    "surface_phase_decision_id": "decision-popup-1",
                },
            ),
        )
    )

    metadata = result.work.frontier.observation.metadata
    assert metadata["dynamic_no_new_business_page_action_count"] == 6


@pytest.mark.asyncio
async def test_package_boundary_back_preserves_active_popup_phase_lineage() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(
            _analysis_with_candidates(
                (
                    InteractionOperation.BACK,
                    "系统返回键",
                    "误进浏览器／外链后 BACK 回目标 App",
                    "back",
                )
            )
        ),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    prior_observation = replace(
        initial.work.frontier.observation,
        metadata={
            **initial.work.frontier.observation.metadata,
            "surface_phase_run_id": "phase-popup-1",
            "surface_phase_key": "popup_chain",
            "surface_phase_decision_id": "decision-popup-1",
            "surface_phase_closed_popup_count": 2,
            "surface_phase_reobserve_count": 1,
            "surface_phase_same_popup_attempt_count": 1,
        },
    )
    prior_work = replace(
        initial.work,
        frontier=replace(initial.work.frontier, observation=prior_observation),
    )

    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=prior_work,
            turn=_action_turn(prior_work, action_type=InteractionOperation.BACK),
        )
    )

    metadata = successor.work.frontier.observation.metadata
    assert metadata["surface_phase_run_id"] == "phase-popup-1"
    assert metadata["surface_phase_key"] == "popup_chain"
    assert metadata["surface_phase_decision_id"] == "decision-popup-1"
    assert metadata["surface_phase_closed_popup_count"] == 2
    assert metadata["surface_phase_reobserve_count"] == 1
    assert metadata["surface_phase_same_popup_attempt_count"] == 1
    assert metadata["surface_phase_package_recovery"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_close_attempt_count"),
    (
        (InteractionResultStatus.SUCCEEDED, 3),
        (InteractionResultStatus.FAILED, 3),
    ),
)
async def test_startup_ad_close_attempt_counter_tracks_actual_driver_attempts(
    status: InteractionResultStatus,
    expected_close_attempt_count: int,
) -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    prior_observation = replace(
        initial.work.frontier.observation,
        metadata={
            **initial.work.frontier.observation.metadata,
            "surface_phase_close_attempt_count": 2,
        },
    )
    prior_work = replace(
        initial.work,
        frontier=replace(initial.work.frontier, observation=prior_observation),
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=prior_work,
            turn=_action_turn(
                prior_work,
                status=status,
                parameters={
                    "surface_phase_run_id": "phase-startup-1",
                    "surface_phase_key": "startup_ad_countdown",
                    "surface_phase_decision_id": "decision-close-1",
                },
            ),
        )
    )

    assert (
        result.work.frontier.observation.metadata["surface_phase_close_attempt_count"]
        == expected_close_attempt_count
    )


@pytest.mark.asyncio
async def test_failed_action_with_after_capture_advances_frontier_truthfully() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    )
    turn = _action_turn(initial.work, status=InteractionResultStatus.FAILED)

    successor = await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=turn,
        )
    )

    assert successor.work.source_capture_id == "capture-after"
    assert successor.work.budget.remaining_actions == 4
    assert successor.work.frontier.visited_candidate_ids == ()
    assert successor.work.frontier.visited_action_fingerprints == ()
    assert turn.coordination is not None
    assert turn.coordination.interaction.status is InteractionResultStatus.FAILED


@pytest.mark.asyncio
async def test_observation_capture_builds_successor_without_consuming_action_budget() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial_workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    )
    initial = await initial_workflow.run(_command())
    workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    )
    turn = _observation_turn(initial.work, phase_key="popup_chain")

    successor = await workflow.prepare_after_observation(
        ContinueAppScreenshotObservationCommand(
            prior_work=initial.work,
            turn=turn,
        )
    )

    work = successor.work
    assert work.source_capture_id == "capture-after"
    assert work.budget.remaining_actions == initial.work.budget.remaining_actions
    assert work.frontier.visited_candidate_ids == ()
    assert work.frontier.visited_action_fingerprints == ()
    assert work.frontier.candidates[0].navigation_depth == 0
    assert work.frontier.observation.metadata["parent_capture_id"] == "capture-1"
    assert work.frontier.observation.metadata["surface_phase_run_id"] == "phase-1"
    assert work.frontier.observation.metadata["surface_phase_key"] == "popup_chain"
    assert work.frontier.observation.metadata["surface_phase_decision_id"] == "decision-1"
    assert work.frontier.observation.metadata["surface_phase_reobserve_count"] == 1
    assert work.frontier.observation.metadata["semantic_role"] == "surface_reobserve"
    assert "action_execution_id" not in work.frontier.observation.metadata


@pytest.mark.asyncio
async def test_reobservation_reuses_prior_when_image_signature_is_unchanged() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    understanding = RecordingUnderstanding(
        _after_capture_analysis(),
        signatures_by_capture={"capture-after": SIGNATURE},
    )

    successor = await AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_observation(
        ContinueAppScreenshotObservationCommand(
            prior_work=initial.work,
            turn=_observation_turn(initial.work, phase_key="popup_chain"),
        )
    )

    assert understanding.captures == []
    saved = observations.saved["capture-after"].analysis
    assert saved.surface_facts["vision_reuse"]["reason"] == "same_image_signature"
    assert saved.surface_facts["vision_reuse"]["source_capture_id"] == "capture-1"
    assert saved.surface_facts["visual_latency"]["reused"] is True
    assert successor.work.source_capture_id == "capture-after"


@pytest.mark.asyncio
async def test_action_and_observation_track_consecutive_unchanged_state() -> None:
    observations = MemoryVisualObservations(
        observed_at_by_capture={
            "capture-1": NOW,
            "capture-after": NOW + timedelta(milliseconds=1),
            "capture-final": NOW + timedelta(milliseconds=2),
        }
    )
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())

    def unchanged_analysis(capture_id: str, artifact_id: str) -> AppVisualAnalysis:
        return AppVisualAnalysis.build(
            capture_id=capture_id,
            screenshot_artifact_id=artifact_id,
            image_signature=SIGNATURE,
            width=1080,
            height=1920,
            orientation="portrait",
            package_name="com.example.target",
            activity=".MainActivity",
            visual_page_type="login",
            visual_summary="页面无变化",
            key_elements=("登录按钮",),
            candidates=(),
            provider_id="fake-app-vision",
            model_id="deterministic-v1",
        )

    after_action = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(unchanged_analysis("capture-after", "artifact-after")),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work),
        )
    )
    after_observation = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(unchanged_analysis("capture-final", "artifact-final")),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_observation(
        ContinueAppScreenshotObservationCommand(
            prior_work=after_action.work,
            turn=_observation_turn(
                after_action.work,
                capture_id="capture-final",
                artifact_id="artifact-final",
            ),
        )
    )

    assert after_action.work.frontier.repeated_state_count == 1
    assert after_observation.work.frontier.repeated_state_count == 2
    assert (
        after_observation.work.frontier.visited_candidate_ids
        == after_action.work.frontier.visited_candidate_ids
    )
    assert (
        after_observation.work.frontier.visited_action_fingerprints
        == after_action.work.frontier.visited_action_fingerprints
    )


@pytest.mark.asyncio
async def test_persisted_observation_time_depletes_duration_deterministically_to_zero() -> None:
    observations = MemoryVisualObservations(
        observed_at_by_capture={
            "capture-1": NOW,
            "capture-after": NOW + timedelta(milliseconds=4),
            "capture-final": NOW + timedelta(milliseconds=10),
        }
    )
    orchestrator = RecordingOrchestrator()
    initial_workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    )
    command = _command()
    command = AppScreenshotExplorationCommand(
        handoff=command.handoff,
        objective=command.objective,
        boundaries=command.boundaries,
        budget=ExplorationBudget(
            max_actions=5,
            remaining_actions=5,
            max_depth=3,
            remaining_duration_ms=10,
        ),
        observed_at=command.observed_at,
        deadline=command.deadline,
        app_goal=command.app_goal,
    )
    initial = await initial_workflow.run(command)
    first_workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    )
    first_command = ContinueAppScreenshotExplorationCommand(
        prior_work=initial.work,
        turn=_action_turn(initial.work),
    )

    first = await first_workflow.prepare_after_action(first_command)
    replay = await first_workflow.prepare_after_action(first_command)
    final_analysis = AppVisualAnalysis.build(
        capture_id="capture-final",
        screenshot_artifact_id="artifact-final",
        image_signature="sha256:" + "c" * 64,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".FinalActivity",
        visual_page_type="final",
        visual_summary="最终页",
        key_elements=(),
        candidates=(),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
    )
    second_workflow = AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(final_analysis),
        observations=observations,
        orchestrator=orchestrator,
    )
    second_turn = _action_turn(
        first.work,
        before_capture_id="capture-before-final",
        after_capture_id="capture-final",
        action_execution_id="action-final",
        after_artifact_id="artifact-final",
    )
    second = await second_workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=first.work,
            turn=second_turn,
        )
    )

    assert replay.work.to_dict() == first.work.to_dict()
    assert first.work.budget.remaining_duration_ms == 6
    assert second.work.budget.remaining_duration_ms == 0


@pytest.mark.asyncio
async def test_before_vision_reuses_prior_when_image_signature_matches() -> None:
    """同画面 before 必须复用 prior 观测并重绑 capture_id，禁止再打 LLM。"""
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    after_signature = "sha256:" + "b" * 64
    after_analysis = _after_capture_analysis()
    understanding = RecordingUnderstanding(
        after_analysis,
        signatures_by_capture={
            "capture-before": SIGNATURE,
            "capture-after": after_signature,
        },
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=orchestrator,
    )

    result = await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work),
        )
    )

    assert [item.capture_id for item in understanding.captures] == ["capture-after"]
    before_saved = observations.saved["capture-before"]
    assert before_saved.analysis.capture_id == "capture-before"
    assert before_saved.analysis.image_signature == SIGNATURE
    assert before_saved.analysis.screenshot_artifact_id == "artifact-before"
    reuse = before_saved.analysis.surface_facts["vision_reuse"]
    assert reuse["reason"] == "same_image_signature"
    assert reuse["source_capture_id"] == "capture-1"
    latency = before_saved.analysis.surface_facts["visual_latency"]
    assert latency["reused"] is True
    assert latency["capture_id"] == "capture-before"
    assert result.work.source_capture_id == "capture-after"


@pytest.mark.asyncio
async def test_before_vision_calls_provider_when_image_signature_differs() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    before_signature = "sha256:" + "c" * 64
    after_signature = "sha256:" + "d" * 64
    after_analysis = AppVisualAnalysis.build(
        capture_id="capture-after",
        screenshot_artifact_id="artifact-after",
        image_signature=after_signature,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".HomeActivity",
        visual_page_type="home",
        visual_summary="首页",
        key_elements=("订单入口",),
        candidates=(),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
    )
    understanding = RecordingUnderstanding(
        after_analysis,
        signatures_by_capture={
            "capture-before": before_signature,
            "capture-after": after_signature,
        },
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=orchestrator,
    )

    await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=_action_turn(initial.work),
        )
    )

    assert [item.capture_id for item in understanding.captures] == [
        "capture-before",
        "capture-after",
    ]
    assert "vision_reuse" not in observations.saved["capture-before"].analysis.surface_facts


@pytest.mark.asyncio
async def test_observe_after_reuses_before_when_signature_unchanged() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis(action_type=InteractionOperation.OBSERVE)),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    shared_signature = "sha256:" + "e" * 64
    after_analysis = AppVisualAnalysis.build(
        capture_id="capture-after",
        screenshot_artifact_id="artifact-after",
        image_signature=shared_signature,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".HomeActivity",
        visual_page_type="home",
        visual_summary="首页",
        key_elements=("观察",),
        candidates=(),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
    )
    understanding = RecordingUnderstanding(
        after_analysis,
        signatures_by_capture={
            "capture-before": shared_signature,
            "capture-after": shared_signature,
        },
    )
    turn = _action_turn(
        initial.work,
        action_type=InteractionOperation.OBSERVE,
    )
    result = await AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=turn,
        )
    )

    assert [item.capture_id for item in understanding.captures] == ["capture-before"]
    after_saved = observations.saved["capture-after"].analysis
    assert after_saved.capture_id == "capture-after"
    assert after_saved.surface_facts["vision_reuse"]["reason"] == "observe_or_timed_capture"
    assert after_saved.surface_facts["vision_reuse"]["source_capture_id"] == "capture-before"
    assert after_saved.surface_facts["visual_latency"]["reused"] is True
    assert result.work.source_capture_id == "capture-after"


@pytest.mark.parametrize(
    "role,label", [("dismiss", "Fermer"), ("dismiss", "×"), ("skip", "スキップ")]
)
def test_signed_control_role_survives_obstruction_filter_without_label_whitelist(
    role, label
) -> None:
    from services.api.app.application.app_screenshot_exploration import (
        _map_candidate,
        _retain_only_obstruction_recovery_candidates,
    )

    analysis = _analysis_with_surface_facts(page_state="popup", overlay_signature="notice")
    control = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=label,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.6, 0.1, 0.7, 0.2),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="解除当前遮挡",
        control_role=role,
    )
    candidate = _map_candidate(control, navigation_depth=0, surface_kind=SurfaceKind.ANDROID)
    retained = _retain_only_obstruction_recovery_candidates(
        candidates=(candidate,),
        analysis=analysis,
        coverage_metadata={},
    )
    assert retained == (candidate,)
    assert candidate.metadata["source_capture_id"] == analysis.capture_id
    assert candidate.metadata["control_role"] == role


@pytest.mark.parametrize(
    "facts,expected",
    [
        ({"visual_page_type_confidence": 0.69}, False),
        ({"visual_page_type_confidence": 0.7}, True),
        ({"startup_ad_visible": True}, False),
        ({"countdown_visible": True}, False),
        ({"popup_visible": True}, False),
    ],
)
def test_business_coverage_requires_confident_unobstructed_frame(facts, expected) -> None:
    from services.api.app.application.app_screenshot_exploration import _is_stable_business_page

    analysis = _coverage_analysis(
        capture_id="capture-1",
        artifact_id="artifact-1",
        image_signature=SIGNATURE,
        visual_page_type="content_detail",
        surface_facts=facts,
    )
    assert _is_stable_business_page(analysis) is expected


@pytest.mark.asyncio
async def test_pre_action_visual_preparation_rebinds_role_and_reuses_identical_pixels() -> None:
    source = _analysis(control_role="dismiss", semantic_name="×", purpose="解除遮挡")
    capture = CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-before",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="before_action",
        parent_capture_id=source.capture_id,
        artifact_ids=("artifact-before",),
    )
    observations = MemoryVisualObservations()
    understanding = RecordingUnderstanding(
        source,
        signatures_by_capture={capture.capture_id: source.image_signature},
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    )
    saved = await workflow.prepare_before_action_visual(capture=capture, source=source)
    repeated = await workflow.prepare_before_action_visual(capture=capture, source=source)
    current = saved.analysis.candidates[0]
    assert understanding.captures == []
    assert repeated.analysis.result_hash == saved.analysis.result_hash
    assert current.control_role == "dismiss"
    assert current.candidate_id != source.candidates[0].candidate_id
    assert current.source_capture_id == capture.capture_id
    assert current.source_image_signature == source.image_signature
    assert observations.saved[capture.capture_id].analysis == saved.analysis


@pytest.mark.asyncio
async def test_splash_skip_before_force_reuses_despite_signature_drift() -> None:
    """开屏跳过 before 即使像素签名漂移，也复用开屏本体，不再二次视觉理解。"""

    source = _analysis(control_role="skip", semantic_name="跳过", purpose="跳过开屏")
    capture = CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-splash-skip-before",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="before_action",
        parent_capture_id=source.capture_id,
        artifact_ids=("artifact-splash-skip-before",),
    )
    # 另一枚合法 sha256，模拟开屏动画导致的签名漂移。
    drifted = "sha256:" + "b" * 64
    assert drifted != source.image_signature
    observations = MemoryVisualObservations()
    understanding = RecordingUnderstanding(
        source,
        signatures_by_capture={capture.capture_id: drifted},
    )
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=RecordingOrchestrator(),
    )
    saved = await workflow.prepare_before_action_visual(
        capture=capture,
        source=source,
        force_reuse_reason="splash_skip_before_reuse",
    )
    assert understanding.captures == []
    assert saved.analysis.capture_id == capture.capture_id
    assert saved.analysis.image_signature == drifted
    assert saved.analysis.candidates[0].semantic_name == "跳过"


@pytest.mark.asyncio
async def test_failed_preflight_visual_does_not_retry_or_publish_successor() -> None:
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command())
    turn = _action_turn(initial.work)
    assert turn.coordination is not None
    turn = replace(
        turn,
        coordination=replace(
            turn.coordination,
            interaction=replace(
                turn.coordination.interaction,
                status=InteractionResultStatus.FAILED,
                error_code="APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED",
                execution_outcome=None,
                verification_outcome=None,
                retryable=False,
            ),
        ),
    )
    understanding = RecordingUnderstanding(_after_capture_analysis())
    workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=observations,
        orchestrator=orchestrator,
    )
    with pytest.raises(AppBeforeActionVisualUnderstandingError) as error:
        await workflow.prepare_after_action(
            ContinueAppScreenshotExplorationCommand(prior_work=initial.work, turn=turn),
        )
    assert error.value.retryable is False
    assert understanding.captures == []
    assert orchestrator.prepared_commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code", ["STALE_VISUAL_CANDIDATE", "APP_CANDIDATE_REVALIDATION_REQUIRED"]
)
async def test_preflight_revalidation_does_not_exhaust_unexecuted_business_entry(
    error_code,
) -> None:
    from services.api.app.application.app_semantic_progress import (
        APP_COVERAGE_SEEK_FAILURE_SCORES,
        APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS,
    )

    source = _coverage_analysis(
        capture_id="capture-1",
        artifact_id="artifact-1",
        image_signature=SIGNATURE,
        visual_page_type="home",
        candidate_semantic_name="账户入口",
        candidate_purpose="进入账户页面",
        expected_state_hint="profile",
    )
    observations = MemoryVisualObservations()
    orchestrator = RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(source),
        observations=observations,
        orchestrator=orchestrator,
    ).run(_command(app_goal=_coverage_goal(required_page_types=("home", "profile"))))
    assert initial.work.frontier.candidates[0].metadata["coverage_page_seek"] is True
    turn = _action_turn(initial.work)
    assert turn.coordination is not None
    turn = replace(
        turn,
        coordination=replace(
            turn.coordination,
            interaction=replace(
                turn.coordination.interaction,
                status=InteractionResultStatus.FAILED,
                execution_outcome=None,
                verification_outcome=None,
                error_code=error_code,
                retryable=True,
            ),
        ),
    )
    after = _coverage_analysis(
        capture_id="capture-after",
        artifact_id="artifact-after",
        image_signature="sha256:" + "b" * 64,
        visual_page_type="startup_ad",
        page_state="countdown",
    )
    successor = await AppScreenshotExplorationWorkflow(
        understanding=RecordingUnderstanding(after),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial.work,
            turn=turn,
        )
    )
    metadata = successor.work.frontier.observation.metadata
    assert not metadata.get(APP_COVERAGE_SEEK_FAILURE_SCORES)
    assert not metadata.get(APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS)
    assert "profile" not in metadata["covered_page_types"]
    assert not successor.work.frontier.visited_candidate_ids
    assert successor.work.budget.remaining_actions < initial.work.budget.remaining_actions


@pytest.mark.parametrize("package", ("dark.wwcskrpojs.ebjhyebjbl", "com.unknown.newapp"))
def test_dynamic_flow_uses_actual_entries_without_package_or_apk_template(package: str) -> None:
    """已知及未知产品都只累计本次截图发现的栏目。"""
    analysis = _coverage_analysis(
        capture_id="dynamic-observed-capture", artifact_id="dynamic-observed-artifact",
        image_signature=SIGNATURE, visual_page_type="home", package_name=package,
        surface_facts={"trusted_accessibility_controls": {"bottom_navigation": [
            {"label": "从当前帧发现的新栏目", "action_allowed": True},
        ]}},
    )
    goal = AppExplorationGoal(
        required_page_types=("home",), required_milestones=("surface:stable",),
        minimum_business_pages=1, allowed_packages=(package,),
    )
    metadata = _next_coverage_metadata(
        goal=goal, analysis=analysis,
        prior_metadata={"apk_filename": "hjll3.3.0.apk", "app_runtime_policy": {
            "exploration_mode": "dynamic_frontier", "playbook_id": None,
        }},
    )
    flow = metadata["app_flow_contract"]
    assert flow["required_bottom_tabs"] == ["从当前帧发现的新栏目"]
    assert flow["observed_bottom_tabs"] == ["从当前帧发现的新栏目"]
    assert flow["missing_bottom_tabs"] == ["从当前帧发现的新栏目"]
    assert flow["playbook_id"] is None
    assert flow["denominator_source"] == "current_observation"


@pytest.mark.parametrize("is_bottom", (False, True))
def test_global_scope_requires_current_bottom_geometry_for_bottom_recovery(is_bottom: bool) -> None:
    """真实全局顶栏保留，但不能借 app 范围变成底栏恢复证据。"""
    from services.api.app.application.app_screenshot_exploration import (
        _analysis_has_real_bottom_navigation,
        _is_early_bottom_nav_overlay_yield_candidate,
    )

    analysis = _coverage_analysis(
        capture_id="global-navigation-capture", artifact_id="global-navigation-artifact",
        image_signature=SIGNATURE, visual_page_type="home",
        candidate_semantic_name="栏目入口", candidate_frontier_scope="app",
        candidate_bounds=NormalizedBounds(
            0.1, 0.84 if is_bottom else 0.10, 0.25, 0.90 if is_bottom else 0.16,
        ),
    )
    frontier = _build_frontier(
        analysis=analysis, observed_at=NOW,
        allowed_hosts=("com.example.target",),
    )
    candidate = next(c for c in frontier.candidates if c.semantic_target == "栏目入口")
    assert _analysis_has_real_bottom_navigation(analysis, (candidate,)) is is_bottom
    assert _is_early_bottom_nav_overlay_yield_candidate(candidate) is is_bottom
