"""最上层首广告取证的几何与重复点击回归。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.app_screenshot_exploration import _build_frontier
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel


def _analysis(popup):
    candidates = tuple(
        AppVisualCandidate.build(
            source_capture_id="capture-popup",
            source_image_signature="sha256:" + "a" * 64,
            source_width=1080,
            source_height=1920,
            source_orientation="portrait",
            semantic_name=name,
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(*bounds),
            confidence=0.95,
            risk_level=InteractionRiskLevel.LOW if close else InteractionRiskLevel.MEDIUM,
            purpose="关闭广告弹窗" if close else "点击广告并观察浏览器落地页",
            expected_state_hint="close_popup_x" if close else "external_browser",
        )
        for name, bounds, close in (
            ("弹窗广告点击入口-背景中奖", (0.02, 0.78, 0.22, 0.87), False),
            ("弹窗广告主体-中央广告", (0.2, 0.25, 0.8, 0.65), False),
            ("关闭广告弹窗", (0.48, 0.72, 0.52, 0.76), True),
        )
    )
    return AppVisualAnalysis.build(
        capture_id="capture-popup",
        screenshot_artifact_id="artifact-popup",
        image_signature="sha256:" + "a" * 64,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        visual_page_type="home",
        page_state="popup",
        overlay_signature="central-ad",
        visual_summary="中央广告覆盖首页，角落有背景浮动广告",
        key_elements=(),
        candidates=candidates,
        provider_id="fake",
        model_id="fake",
        surface_facts=_parsed_facts(popup),
    )


def _parsed_facts(popup):
    from services.api.app.infrastructure.app_vision.llm import _surface_facts

    return _surface_facts({"popup_visible": True, "topmost_ad_popup": popup})


def _frontier(popup, attempts=0):
    return _build_frontier(
        analysis=_analysis(popup),
        observed_at=datetime(2026, 8, 31, tzinfo=UTC),
        allowed_hosts=("com.example.target",),
        coverage_metadata={"app_ad_landing_attempt_count": attempts},
    )


POPUP = {
    "is_ad": True,
    "confidence": 0.95,
    "normalized_bounds": {"left": 0.15, "top": 0.2, "right": 0.85, "bottom": 0.7},
}


def test_first_popup_rejects_background_cta_and_samples_topmost_body_once():
    frontier = _frontier(POPUP)
    assert frontier.candidates[0].semantic_target == "弹窗广告主体-中央广告"
    assert frontier.candidates[0].metadata["app_ad_landing_consistency"] is True
    assert not any("背景中奖" in c.semantic_target for c in frontier.candidates)
    # watcher_cascade：裸 _build_frontier 保留几何安全 ×（B）；
    # 底栏「<」（D）由 package-boundary enrich 注入，不在本用例路径。
    assert any(c.semantic_target == "关闭广告弹窗" for c in frontier.candidates)
    repeated = _frontier(POPUP, attempts=1)
    assert not any(c.metadata.get("app_ad_landing_consistency") for c in repeated.candidates)
    assert any(c.semantic_target == "关闭广告弹窗" for c in repeated.candidates)


@pytest.mark.parametrize(
    "popup",
    [
        None,
        {},
        {**POPUP, "is_ad": False},
        {**POPUP, "confidence": 0.79},
        {**POPUP, "confidence": True},
        {**POPUP, "confidence": 1.1},
        {**POPUP, "normalized_bounds": {"left": 0.3}},
        {**POPUP, "normalized_bounds": {"left": 0.3, "top": 0.3, "right": 0.8, "bottom": 0.6}},
    ],
)
def test_unproven_popup_scope_keeps_close_without_signing_ad(popup):
    frontier = _frontier(popup)
    assert not any(c.metadata.get("app_ad_landing_consistency") for c in frontier.candidates)
    # watcher_cascade：未证实广告范围时仍保留 ×（B），不得签发广告取证。
    assert any(c.semantic_target == "关闭广告弹窗" for c in frontier.candidates)
