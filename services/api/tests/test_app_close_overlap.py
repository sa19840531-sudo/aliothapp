"""弹窗广告主体保护与像素关闭候选重叠的回归测试。"""

from __future__ import annotations

import io
import math

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.app_visual_close_calibration import (
    calibrate_popup_close_candidates,
    detect_isolated_cross_controls,
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

_IMAGE_WIDTH = 240
_IMAGE_HEIGHT = 400
_CLOSE_CENTER = (120, 298)
_CLOSE_RADIUS = 16
_SIGNATURE = "sha256:" + "e" * 64


def _popup_circular_close_png() -> bytes:
    """生成白底黑 X 圆形关闭钮，避免依赖真实 App 截图。"""

    image = Image.new("RGB", (_IMAGE_WIDTH, _IMAGE_HEIGHT), color=(32, 32, 32))
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 70, 212, 270), fill=(88, 64, 64))
    center_x, center_y = _CLOSE_CENTER
    draw.ellipse(
        (
            center_x - _CLOSE_RADIUS,
            center_y - _CLOSE_RADIUS,
            center_x + _CLOSE_RADIUS,
            center_y + _CLOSE_RADIUS,
        ),
        fill=(250, 250, 250),
    )
    arm = _CLOSE_RADIUS // 2
    draw.line(
        (center_x - arm, center_y - arm, center_x + arm, center_y + arm),
        fill=(12, 12, 12),
        width=3,
    )
    draw.line(
        (center_x + arm, center_y - arm, center_x - arm, center_y + arm),
        fill=(12, 12, 12),
        width=3,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _candidate(
    *,
    semantic_name: str,
    bounds: NormalizedBounds,
    risk_level: InteractionRiskLevel,
    purpose: str,
    control_role: str | None = None,
    expected_state_hint: str | None = None,
    confidence: float = 0.9,
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id="capture-close-overlap",
        source_image_signature=_SIGNATURE,
        source_width=_IMAGE_WIDTH,
        source_height=_IMAGE_HEIGHT,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=confidence,
        risk_level=risk_level,
        purpose=purpose,
        control_role=control_role,
        expected_state_hint=expected_state_hint,
    )


def _analysis(
    candidates: tuple[AppVisualCandidate, ...],
    *,
    page_state: str = "popup",
) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id="capture-close-overlap",
        screenshot_artifact_id="artifact-close-overlap",
        image_signature=_SIGNATURE,
        width=_IMAGE_WIDTH,
        height=_IMAGE_HEIGHT,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="首页被广告弹窗遮挡",
        key_elements=("广告弹窗",),
        candidates=candidates,
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state=page_state,
        overlay_signature="center-ad",
        surface_facts={"popup_visible": True},
    )


def _medium_ad_body() -> AppVisualCandidate:
    return _candidate(
        semantic_name="弹窗广告主体",
        bounds=NormalizedBounds(0.05, 0.30, 0.95, 0.88),
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="点击广告主体并观察浏览器落地页",
    )


def _high_ad_body() -> AppVisualCandidate:
    return _candidate(
        semantic_name="弹窗广告主体",
        bounds=NormalizedBounds(0.05, 0.30, 0.95, 0.88),
        risk_level=InteractionRiskLevel.HIGH,
        purpose="点击广告主体并观察浏览器落地页",
    )


def _close_candidate(
    *,
    role: str,
    bounds: NormalizedBounds,
    confidence: float = 0.99,
) -> AppVisualCandidate:
    return _candidate(
        semantic_name="关闭广告弹窗" if role == "dismiss" else "跳过广告",
        bounds=bounds,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭当前遮挡并继续探索",
        control_role=role,
        expected_state_hint="close_popup_x" if role == "dismiss" else "tap_skip",
        confidence=confidence,
    )


def _non_close_candidate() -> AppVisualCandidate:
    return _candidate(
        semantic_name="推荐内容",
        bounds=NormalizedBounds(0.42, 0.70, 0.54, 0.78),
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开推荐视频",
    )


def _bounds_overlap(left: NormalizedBounds, right: NormalizedBounds) -> bool:
    """判断两个矩形是否有正面积交集。"""

    return (
        max(left.left, right.left) < min(left.right, right.right)
        and max(left.top, right.top) < min(left.bottom, right.bottom)
    )


def _tap_is_inside_close_button(bounds: NormalizedBounds) -> bool:
    center_x, center_y = bounds.center
    distance = math.hypot(
        center_x * _IMAGE_WIDTH - _CLOSE_CENTER[0],
        center_y * _IMAGE_HEIGHT - _CLOSE_CENTER[1],
    )
    return distance <= _CLOSE_RADIUS


@pytest.mark.parametrize(
    ("role", "page_state"),
    [("dismiss", "popup"), ("skip", "startup_ad")],
)
def test_medium_ad_body_is_exempt_when_detected_x_overlaps_low_risk_close(
    role: str,
    page_state: str,
) -> None:
    """dismiss/skip 框稍有偏移但仍相交时，MEDIUM 广告框不得吞掉真 X。"""

    screenshot = _popup_circular_close_png()
    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=_IMAGE_WIDTH,
        expected_height=_IMAGE_HEIGHT,
    )
    assert len(detected) == 1
    close = _close_candidate(
        role=role,
        # 相对圆钮检测框故意向左上轻微偏移，仍保留正面积交集。
        bounds=NormalizedBounds(0.42, 0.70, 0.54, 0.78),
    )
    analysis = _analysis((_medium_ad_body(), close), page_state=page_state)
    assert _bounds_overlap(detected[0], close.normalized_bounds)

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert calibrated is not analysis
    ad_candidate = calibrated.candidates[0]
    assert ad_candidate is analysis.candidates[0]
    assert ad_candidate.normalized_bounds == analysis.candidates[0].normalized_bounds
    close_candidate = calibrated.candidates[1]
    assert close_candidate.control_role == role
    assert close_candidate.normalized_bounds is not None
    assert _tap_is_inside_close_button(close_candidate.normalized_bounds)
    if role == "dismiss":
        assert close_candidate.semantic_name == "关闭广告弹窗"
        assert close_candidate.expected_state_hint == "close_popup_x"
    else:
        assert close_candidate.semantic_name == "跳过启动广告"
        assert close_candidate.expected_state_hint == "tap_skip"


def test_high_ad_body_remains_protected_even_when_low_risk_close_overlaps() -> None:
    """HIGH 广告区域即使与关闭框重叠，也必须继续阻断像素落点。"""

    screenshot = _popup_circular_close_png()
    close = _close_candidate(
        role="dismiss",
        bounds=NormalizedBounds(0.42, 0.70, 0.54, 0.78),
    )
    analysis = _analysis((_high_ad_body(), close))
    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=_IMAGE_WIDTH,
        expected_height=_IMAGE_HEIGHT,
    )
    assert len(detected) == 1
    assert _bounds_overlap(detected[0], close.normalized_bounds)

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert calibrated is analysis
    assert calibrated.candidates[0] is analysis.candidates[0]
    assert calibrated.candidates[1] is analysis.candidates[1]


@pytest.mark.parametrize(
    "close_candidate",
    [
        _non_close_candidate(),
        _close_candidate(
            role="dismiss",
            # 有 close 语义但离检测 X 很远，MEDIUM 广告主体仍须保护。
            bounds=NormalizedBounds(0.80, 0.05, 0.92, 0.13),
        ),
    ],
    ids=["no-close-semantics", "close-is-far"],
)
def test_medium_ad_body_remains_protected_without_nearby_close_semantics(
    close_candidate: AppVisualCandidate,
) -> None:
    """没有近处 dismiss/skip 证据时，MEDIUM 广告主体保护不能放宽。"""

    screenshot = _popup_circular_close_png()
    candidates = (_medium_ad_body(), close_candidate)
    analysis = _analysis(candidates)
    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=_IMAGE_WIDTH,
        expected_height=_IMAGE_HEIGHT,
    )
    assert len(detected) == 1
    if close_candidate.semantic_name == "推荐内容":
        assert _bounds_overlap(detected[0], close_candidate.normalized_bounds)
    else:
        assert not _bounds_overlap(detected[0], close_candidate.normalized_bounds)

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert calibrated is analysis
    assert calibrated.candidates == analysis.candidates
