"""合成触控合同与 Surface Phase 弹层关闭优先级。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.api.app.application.app_synthetic_touch_contracts import (
    DEFAULT_STARTUP_AD_SKIP_BOUNDS,
    default_popup_close_bounds,
    default_startup_ad_skip_bounds,
    popup_close_corner_contract,
    resolve_popup_close_corner_candidate,
    resolve_startup_ad_skip_candidate,
    startup_ad_skip_contract,
)
from services.api.app.application.surface_phase_explorer import (
    _first_close_candidate,
)
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


def _popup_work(*candidates: CandidateAction):
    observation = SimpleNamespace(
        page_type="home",
        page_state="popup",
        overlay_signature="overlay:1",
        metadata={"surface_facts": {"popup_visible": True}},
    )
    frontier = SimpleNamespace(
        candidates=candidates,
        observation=observation,
    )
    return SimpleNamespace(
        frontier=frontier,
        source_capture_id="cap-1",
        surface_kind=SurfaceKind.ANDROID,
    )


def test_popup_close_prefers_geometry_x_before_cloud_back_twice() -> None:
    """popup_chain cascade：几何安全 X 优先于 D 档 cloud_back_twice。"""

    cloud_back = CandidateAction(
        candidate_id="cloud-back",
        semantic_target="底部导航栏「<」关闭弹窗",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": (
                "弹窗广告仍在：连点两下底部系统导航栏最右侧「<」关窗"
                "（cloud_back_tap_twice）"
            ),
            "frontier_scope": "local",
            "source_capture_id": "cap-1",
        },
    )
    close_x = CandidateAction(
        candidate_id="close",
        semantic_target="关闭弹层右上X",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "点弹层关闭",
            "expected_state_hint": "close_popup_x",
            "popup_close_corner_fallback": True,
        },
    )
    work = _popup_work(close_x, cloud_back)
    picked = _first_close_candidate(work, phase_key="popup_chain")
    assert picked is not None
    assert picked.candidate_id == "close"


def test_bottom_circular_x_near_nav_band_is_allowed() -> None:
    """宫格正下方圆 X（y≈0.85）不得被底栏禁区误杀。"""

    from services.api.app.agents.explorer.maf_agent import (
        _popup_close_avoids_bottom_navigation_band,
    )

    bottom_x = CandidateAction(
        candidate_id="bottom-x",
        semantic_target="关闭弹层下方圆X",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "popup_close_bottom_x_fallback": True,
            "normalized_bounds": {
                "left": 0.44,
                "top": 0.80,
                "right": 0.56,
                "bottom": 0.90,
            },
        },
    )
    assert _popup_close_avoids_bottom_navigation_band(bottom_x) is True


def test_popup_close_requires_current_control_geometry() -> None:
    analysis = SimpleNamespace(width=1080, height=1920, surface_facts={})
    assert default_popup_close_bounds(analysis) is None
    analysis.surface_facts = {"trusted_accessibility_controls": {"overlay_close": {
        "bounds": {"left": 486, "top": 1382.4, "right": 594, "bottom": 1497.6},
    }}}
    bounds = default_popup_close_bounds(analysis)
    assert bounds is not None
    assert bounds.to_dict() == pytest.approx(
        {"left": 0.45, "top": 0.72, "right": 0.55, "bottom": 0.78}
    )


def test_resolve_popup_close_corner_from_contract() -> None:
    from services.api.app.domain.exploration import AppVisualCandidate, NormalizedBounds

    purpose = "点弹层右上／角落关闭控件解除遮挡（合成兜底，禁止点广告本体）"
    bounds = NormalizedBounds(0.86, 0.08, 0.98, 0.20)
    contract = popup_close_corner_contract(bounds=bounds, purpose=purpose)
    analysis = SimpleNamespace(
        capture_id="cap-popup",
        image_signature="sha256:" + ("ef" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
    )
    built = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name="关闭弹层右上X",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.82,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="dismiss",
        expected_state_hint="close_popup_x",
    )
    resolved = resolve_popup_close_corner_candidate(
        intent_semantic_target="关闭弹层右上X",
        intent_purpose=purpose,
        analysis=analysis,
        candidate_id=built.candidate_id,
        payload=contract,
    )
    assert resolved is not None
    assert resolved.candidate_id == built.candidate_id


def test_forced_pixel_popup_close_resolves_without_visual_hash_id() -> None:
    """forced-pixel-popup-close:* 不得因哈希 id 不一致被 VISUAL_CANDIDATE_NOT_FOUND。"""

    from services.api.app.domain.exploration import NormalizedBounds

    purpose = "点击当前截图视觉像素弹层下方圆 X 并关闭；禁止只观察不点。"
    bounds = NormalizedBounds(0.48, 0.70, 0.52, 0.72)
    contract = popup_close_corner_contract(bounds=bounds, purpose=purpose, confidence=0.96)
    analysis = SimpleNamespace(
        capture_id="f204af0c-1761-5918-bafa-86d1deb38ded",
        image_signature="sha256:" + ("cd" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
    )
    resolved = resolve_popup_close_corner_candidate(
        intent_semantic_target="关闭弹层下方X",
        intent_purpose="当前截图像素检出弹层下方圆 X；点击关闭并截图，禁止只观察不点。",
        analysis=analysis,
        candidate_id=f"forced-pixel-popup-close:{analysis.capture_id}",
        payload=contract,
    )
    assert resolved is not None
    assert resolved.normalized_bounds.to_dict() == bounds.to_dict()
    # 错误 capture 后缀仍拒绝
    assert (
        resolve_popup_close_corner_candidate(
            intent_semantic_target="关闭弹层下方X",
            intent_purpose="当前截图像素检出弹层下方圆 X；点击关闭并截图，禁止只观察不点。",
            analysis=analysis,
            candidate_id="forced-pixel-popup-close:other-capture",
            payload=contract,
        )
        is None
    )


def test_popup_close_corner_resolves_vision_hash_candidate_id() -> None:
    """18+ 下方 X：Vision 哈希 candidate_id 对不上重建 id 时，仍须用合同坐标点。"""

    from services.api.app.domain.exploration import NormalizedBounds

    purpose = (
        "点弹层像素检出关闭控件（pixel_cross_close_calibration），"
        "立即关闭并截图；禁止只观察／点广告本体"
    )
    bounds = NormalizedBounds(0.4847, 0.7023, 0.5167, 0.7175)
    contract = popup_close_corner_contract(bounds=bounds, purpose=purpose, confidence=0.95)
    analysis = SimpleNamespace(
        capture_id="68c48dec-0954-5c03-aa06-9659c365b655",
        image_signature="sha256:" + ("ef" * 32),
        width=1440,
        height=3040,
        orientation="portrait",
    )
    resolved = resolve_popup_close_corner_candidate(
        intent_semantic_target="关闭弹层下方X",
        intent_purpose="先关闭18+成人专区警告弹层下方X清障，待新Capture核实门禁消失后再探索未覆盖业务页",
        analysis=analysis,
        candidate_id="candidate:855e54166f01efc2da9c31c266c8ccfbc70f84249ee77893d4dfb342e50ce42a",
        payload=contract,
    )
    assert resolved is not None
    assert resolved.normalized_bounds.to_dict() == bounds.to_dict()
    assert resolved.control_role == "dismiss"


def test_legacy_startup_skip_contract_cannot_authorize_guessed_touch() -> None:
    from services.api.app.domain.exploration import AppVisualCandidate, NormalizedBounds

    purpose = "开屏广告右上跳过一次以进入 App（合成兜底，禁止点广告本体）"
    bounds = NormalizedBounds(0.78, 0.02, 0.98, 0.10)
    contract = startup_ad_skip_contract(bounds=bounds, purpose=purpose)
    analysis = SimpleNamespace(
        capture_id="cap-startup",
        image_signature="sha256:" + ("ab" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        surface_facts={},
    )
    built = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name="右上跳过开屏",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.75,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint="tap_skip",
    )
    resolved = resolve_startup_ad_skip_candidate(
        intent_semantic_target="右上跳过开屏",
        intent_purpose=purpose,
        analysis=analysis,
        candidate_id=built.candidate_id,
        payload=contract,
    )
    assert resolved is None


def test_trusted_startup_skip_contract_rebuilds_from_current_frame() -> None:
    from services.api.app.domain.exploration import AppVisualCandidate, NormalizedBounds

    purpose = "点击目标包唯一可访问性开屏退出控件，跳过广告进入业务页"
    raw_bounds = NormalizedBounds(0.80, 0.05, 0.95, 0.10)
    analysis = SimpleNamespace(
        capture_id="cap-startup",
        image_signature="sha256:" + ("ab" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        surface_facts={
            "trusted_accessibility_skip": {
                "artifact_id": "ui-tree-1",
                "package_name": "com.example.target",
                "label": "进入",
                "normalized_bounds": raw_bounds.to_dict(),
            }
        },
    )
    # 与生产路径一致：合同与候选都使用 default_startup_ad_skip_bounds 放大后的框。
    bounds, used_default = default_startup_ad_skip_bounds(analysis)
    assert used_default is False
    contract = startup_ad_skip_contract(
        bounds=bounds,
        purpose=purpose,
        discovery_source="uia_issued",
        used_default_fixed_bounds=False,
    )
    built = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name="跳过开屏：进入",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.75,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="skip",
        expected_state_hint="tap_skip",
        discovery_source="uia_issued",
    )
    resolved = resolve_startup_ad_skip_candidate(
        intent_semantic_target="跳过开屏：进入",
        intent_purpose=purpose,
        analysis=analysis,
        candidate_id=built.candidate_id,
        payload=contract,
    )
    assert resolved is not None
    assert resolved.expected_state_hint == "tap_skip"
    assert resolved.discovery_source == "uia_issued"


def test_default_startup_ad_skip_bounds_uses_platform_default() -> None:
    analysis = SimpleNamespace(
        surface_facts={},
        width=1080,
        height=1920,
    )
    bounds, used_default = default_startup_ad_skip_bounds(analysis)
    assert bounds == DEFAULT_STARTUP_AD_SKIP_BOUNDS
    assert used_default is True


def test_default_startup_ad_skip_bounds_inflates_trusted_skip() -> None:
    analysis = SimpleNamespace(
        surface_facts={
            "trusted_accessibility_skip": {
                "normalized_bounds": {
                    "left": 0.80,
                    "top": 0.05,
                    "right": 0.95,
                    "bottom": 0.10,
                }
            }
        },
        width=1080,
        height=1920,
    )
    bounds, used_default = default_startup_ad_skip_bounds(analysis)
    assert used_default is False
    assert bounds.left == pytest.approx(0.785)
    assert bounds.top == pytest.approx(0.035)
    assert bounds.right == pytest.approx(0.965)
    assert bounds.bottom == pytest.approx(0.115)
