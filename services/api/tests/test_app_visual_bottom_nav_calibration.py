"""底栏视觉像素校准回归。"""

from __future__ import annotations

import hashlib

import cv2
import numpy as np

from services.api.app.application.app_visual_bottom_nav_calibration import (
    PIXEL_BOTTOM_NAV_CONTRACT,
    calibrate_bottom_navigation_candidates,
    detect_bottom_nav_tabs,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)


def _png_with_bottom_nav(*, width: int = 360, height: int = 780, tabs: int = 6) -> bytes:
    """绘制深色底栏 + 等距彩色图标柱，模拟自绘底栏。"""

    image = np.full((height, width, 3), 24, dtype=np.uint8)
    # 内容区杂讯，避免整屏被当底栏。
    image[40:600, 20:340] = (48, 48, 52)
    y0 = int(height * 0.86)
    y1 = int(height * 0.95)
    image[y0:y1, :] = (18, 18, 22)
    colors = [
        (40, 180, 90),
        (90, 90, 200),
        (200, 120, 40),
        (40, 200, 200),
        (180, 60, 180),
        (220, 220, 60),
    ]
    for index in range(tabs):
        cx = int((index + 0.5) * width / tabs)
        color = colors[index % len(colors)]
        # 选中「主页」更高饱和。
        if index == 0:
            color = (30, 220, 120)
        cv2.rectangle(
            image,
            (cx - 14, y0 + 8),
            (cx + 14, y0 + 36),
            color,
            thickness=-1,
        )
        cv2.rectangle(
            image,
            (cx - 10, y0 + 42),
            (cx + 10, y1 - 4),
            (200, 200, 200),
            thickness=-1,
        )
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _analysis_for_png(png: bytes, *, candidates: tuple[AppVisualCandidate, ...] = ()) -> AppVisualAnalysis:
    decoded = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    height, width = decoded.shape[:2]
    signature = f"sha256:{hashlib.sha256(png).hexdigest()}"
    return AppVisualAnalysis.build(
        capture_id="capture-bottom-nav-pixel",
        screenshot_artifact_id="artifact-bottom-nav-pixel",
        image_signature=signature,
        width=width,
        height=height,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="主页底栏可见",
        key_elements=("底栏",),
        candidates=candidates,
        provider_id="test",
        model_id="test",
        package_name="dark.wwcskrpojs.ebjhyebjbl",
        activity=".Main",
        page_state="stable",
        surface_facts={},
    )


def test_detect_bottom_nav_tabs_maps_ordered_labels() -> None:
    png = _png_with_bottom_nav()
    tabs = detect_bottom_nav_tabs(
        png,
        expected_width=360,
        expected_height=780,
        ordered_labels=("主页", "短剧", "暗网", "AI", "社区", "我的"),
    )
    assert len(tabs) == 6
    assert [tab.label for tab in tabs] == ["主页", "短剧", "暗网", "AI", "社区", "我的"]
    for tab in tabs:
        assert 0.84 <= tab.bounds.top < tab.bounds.bottom <= 0.96
        assert tab.bounds.bottom < 0.96
        cx = (tab.bounds.left + tab.bounds.right) / 2.0
        assert abs(cx - tab.center_x) < 0.02


def test_calibrate_dedupes_two_vision_hits_on_same_slot() -> None:
    """两个 Vision 候选吸附同一像素槽时不得签发重复 candidate_id。"""

    png = _png_with_bottom_nav()
    decoded = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    height, width = decoded.shape[:2]
    signature = f"sha256:{hashlib.sha256(png).hexdigest()}"
    first = AppVisualCandidate.build(
        source_capture_id="capture-bottom-nav-pixel",
        source_image_signature=signature,
        source_width=width,
        source_height=height,
        source_orientation="portrait",
        semantic_name="底栏「顶部」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.02, 0.86, 0.16, 0.94),
        confidence=0.8,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开顶部",
        expected_state_hint="tab:顶部",
        frontier_scope="app",
        discovery_source="vision",
    )
    second = AppVisualCandidate.build(
        source_capture_id="capture-bottom-nav-pixel",
        source_image_signature=signature,
        source_width=width,
        source_height=height,
        source_orientation="portrait",
        semantic_name="底栏「首页」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.03, 0.87, 0.15, 0.93),
        confidence=0.75,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开首页",
        expected_state_hint="tab:首页",
        frontier_scope="app",
        discovery_source="vision",
    )
    analysis = _analysis_for_png(png, candidates=(first, second))
    calibrated = calibrate_bottom_navigation_candidates(
        analysis=analysis,
        screenshot_png=png,
        ordered_labels=("主页", "短剧", "暗网", "AI", "社区", "我的"),
    )
    ids = [item.candidate_id for item in calibrated.candidates]
    assert len(ids) == len(set(ids))
    home_count = sum(1 for item in calibrated.candidates if item.semantic_name == "底栏「主页」")
    assert home_count == 1


def test_calibrate_rewrites_ocr_dingbu_to_playbook_home_label() -> None:
    """OCR「顶部」吸附到主页槽时，语义必须改写为课纲「主页」，禁止保留误读。"""

    png = _png_with_bottom_nav()
    decoded = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    height, width = decoded.shape[:2]
    signature = f"sha256:{hashlib.sha256(png).hexdigest()}"
    ocr_home = AppVisualCandidate.build(
        source_capture_id="capture-bottom-nav-pixel",
        source_image_signature=signature,
        source_width=width,
        source_height=height,
        source_orientation="portrait",
        semantic_name="底栏「顶部」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.02, 0.86, 0.16, 0.94),
        confidence=0.8,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开顶部",
        expected_state_hint="tab:顶部",
        frontier_scope="app",
        discovery_source="vision",
    )
    analysis = _analysis_for_png(png, candidates=(ocr_home,))
    calibrated = calibrate_bottom_navigation_candidates(
        analysis=analysis,
        screenshot_png=png,
        ordered_labels=("主页", "短剧", "暗网", "AI", "社区", "我的"),
    )
    home = next(
        (
            item
            for item in calibrated.candidates
            if item.semantic_name == "底栏「主页」"
        ),
        None,
    )
    assert home is not None
    assert "顶部" not in (home.semantic_name or "")
    assert "主页" in (home.purpose or "")
    assert all("顶部" not in (item.semantic_name or "") for item in calibrated.candidates)
    # 选中槽随像素亮度变化；此处只要求校准写入了 pixel_selected_labels 列表。
    selected = calibrated.surface_facts.get("pixel_selected_labels")
    assert isinstance(selected, (list, tuple))
    assert selected


def test_calibrate_rewrites_vision_and_fills_missing_tabs() -> None:
    png = _png_with_bottom_nav()
    decoded = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    height, width = decoded.shape[:2]
    signature = f"sha256:{hashlib.sha256(png).hexdigest()}"
    # Vision 只给一栏且偏手势区。
    vision_only = AppVisualCandidate.build(
        source_capture_id="capture-bottom-nav-pixel",
        source_image_signature=signature,
        source_width=width,
        source_height=height,
        source_orientation="portrait",
        semantic_name="底栏「短剧」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.12, 0.93, 0.28, 0.99),
        confidence=0.7,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开短剧",
        expected_state_hint="tab:短剧",
        frontier_scope="app",
        discovery_source="vision",
    )
    analysis = _analysis_for_png(png, candidates=(vision_only,))
    calibrated = calibrate_bottom_navigation_candidates(
        analysis=analysis,
        screenshot_png=png,
        ordered_labels=("主页", "短剧", "暗网", "AI", "社区", "我的"),
    )
    facts = calibrated.surface_facts["pixel_bottom_nav_calibration"]
    assert facts["contract_id"] == PIXEL_BOTTOM_NAV_CONTRACT
    assert facts["tab_count"] == 6
    labels = {
        item.semantic_name
        for item in calibrated.candidates
        if "底栏" in item.semantic_name
    }
    assert any("短剧" in name for name in labels)
    assert any("暗网" in name for name in labels)
    short = next(item for item in calibrated.candidates if "短剧" in item.semantic_name)
    assert short.normalized_bounds is not None
    assert short.normalized_bounds.top < 0.93
    assert "视觉像素底栏校准" in (short.purpose or "")
