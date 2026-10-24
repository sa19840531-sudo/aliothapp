"""App 截图理解失败时的通用、无包名依赖探索兜底。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.application.app_screenshot_understanding import (
    UnderstandAppScreenshotRequest,
)
from services.api.app.domain.exploration import AppVisualAnalysis, AppVisualCandidate
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)

PROVIDER_ID = "heuristic-app-vision"
MODEL_ID = "dynamic-safe-frontier-v2"


def build_first_screen_escape_analysis(
    request: UnderstandAppScreenshotRequest,
    *,
    reason: str,
    degradation_fact: Mapping[str, object] | None = None,
) -> AppVisualAnalysis:
    """视觉 Provider 不可用时签发观察/滚动；开屏进入/跳过由 exploration 合成右上相对区。"""
    if not isinstance(request, UnderstandAppScreenshotRequest):
        raise TypeError("request 必须为 UnderstandAppScreenshotRequest")
    # depth0 开屏勿优先 BACK（会退出 App）；进入/跳过由 _append_startup_ad_skip_candidate 补发。
    candidates = (
        _safe_candidate(
            request,
            semantic_name="重新观察当前页面",
            action_type=InteractionOperation.OBSERVE,
            purpose="等待当前 App 页面稳定后重新采集并动态识别 Frontier",
            expected_state_hint="reobserve_stable_page",
            confidence=0.7,
        ),
        _safe_candidate(
            request,
            semantic_name="安全下滑发现更多内容",
            action_type=InteractionOperation.SCROLL,
            purpose="在当前页面执行只读下滑并重新识别可见业务候选",
            expected_state_hint="discover_more_content",
            confidence=0.6,
        ),
        _safe_candidate(
            request,
            semantic_name="返回上一页避开卡住界面",
            action_type=InteractionOperation.BACK,
            purpose="非开屏卡死时再尝试系统返回；开屏优先进入/跳过相对区",
            expected_state_hint="escape_stuck_overlay",
            confidence=0.55,
        ),
    )
    surface_facts: dict[str, object] = {"fallback_reason": reason}
    if degradation_fact is not None:
        surface_facts["visual_provider_quality"] = dict(degradation_fact)
    return AppVisualAnalysis.build(
        capture_id=request.capture_id,
        screenshot_artifact_id=request.screenshot_artifact_id,
        image_signature=request.image_signature,
        width=request.width,
        height=request.height,
        orientation=request.orientation,
        visual_page_type=None,
        visual_summary="视觉理解降级，执行通用安全探索",
        key_elements=(),
        candidates=candidates,
        provider_id=PROVIDER_ID,
        model_id=MODEL_ID,
        package_name=request.package_name,
        activity=request.activity,
        page_state="unknown",
        surface_facts=surface_facts,
    )


def ensure_exploration_candidates(
    analysis: AppVisualAnalysis,
    request: UnderstandAppScreenshotRequest,
) -> AppVisualAnalysis:
    """仅在视觉结果为空时补通用安全候选，不注入历史坐标或包专用节奏。"""
    if not analysis.candidates:
        return build_first_screen_escape_analysis(
            request,
            reason="模型未给出可执行候选，使用通用动态 Frontier 兜底继续截图",
            degradation_fact={
                "protocol_version": "app-visual-provider-quality.v1",
                "status": "degraded",
                "provider_id": analysis.provider_id,
                "model_id": analysis.model_id,
                "error_code": "provider_no_candidates",
                "error_type": "EmptyCandidates",
                "retryable": False,
            },
        )
    return analysis


def _safe_candidate(
    request: UnderstandAppScreenshotRequest,
    *,
    semantic_name: str,
    action_type: InteractionOperation,
    purpose: str,
    expected_state_hint: str,
    confidence: float,
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=request.capture_id,
        source_image_signature=request.image_signature,
        source_width=request.width,
        source_height=request.height,
        source_orientation=request.orientation,
        semantic_name=semantic_name,
        action_type=action_type,
        normalized_bounds=None,
        confidence=confidence,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=expected_state_hint,
        frontier_scope="local",
    )
