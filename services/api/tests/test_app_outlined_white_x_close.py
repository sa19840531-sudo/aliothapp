"""白描边圆白 X 的真实像素与当前帧重绑回归。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.app_obstruction_controls import (
    can_rebind_visual_obstruction_control,
)
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
    InteractionResultStatus,
    InteractionRiskLevel,
)
from services.api.tests.test_app_interaction_runtime import (
    BEFORE_CAPTURE_ID,
    OBSERVATION_CAPTURE_ID,
    _before_visual_analysis,
    _executor,
    _request,
)

WIDTH, HEIGHT = 1440, 3040
ICON = Path(__file__).with_name("fixtures") / "app_white_outline_x_icon.png"
NATIVE = NormalizedBounds(653 / WIDTH, 2060 / HEIGHT, 787 / WIDTH, 2194 / HEIGHT)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _actual_icon_frame() -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    image.paste(Image.open(ICON).convert("RGB"), (653, 2060))
    return _png(image)


def _analysis(raw: bytes, *, capture: str, wrong_vision: bool = False, payment: bool = False):
    signature = "sha256:" + hashlib.sha256(raw).hexdigest()
    candidate = AppVisualCandidate.build(
        source_capture_id=capture,
        source_image_signature=signature,
        source_width=WIDTH,
        source_height=HEIGHT,
        source_orientation="portrait",
        semantic_name="关闭弹层下方X",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.42, 0.555, 0.58, 0.62)
        if wrong_vision
        else NormalizedBounds(0.43, 0.69, 0.57, 0.75),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭当前广告弹层",
        control_role="dismiss",
        expected_state_hint="close_popup_x",
    )
    return AppVisualAnalysis.build(
        capture_id=capture,
        screenshot_artifact_id="artifact-" + capture,
        image_signature=signature,
        width=WIDTH,
        height=HEIGHT,
        orientation="portrait",
        visual_page_type="payment" if payment else "post_list",
        visual_summary="当前广告弹层",
        key_elements=("关闭控件",),
        candidates=(candidate,),
        provider_id="provider",
        model_id="model",
        package_name="com.example.app",
        activity=".Main",
        page_state="popup",
        overlay_signature="popup",
        surface_facts={"popup_visible": True},
    )


def test_native_bounds_match_real_outline_white_x_pixels():
    """真实图标分离件保留白笔画像素，检测框须匹配同帧原生控件。"""
    detected = detect_isolated_cross_controls(
        _actual_icon_frame(), expected_width=WIDTH, expected_height=HEIGHT
    )
    assert len(detected) == 1
    assert detected[0] == NATIVE


def test_current_before_independently_corrects_wrong_vision_without_source_reuse():
    """源帧中心与 before 错框相差超阈值；当前像素独立校准后才可重绑。"""
    raw = _actual_icon_frame()
    source = _analysis(raw, capture="source")
    before = _analysis(raw, capture="current-before", wrong_vision=True)
    assert not can_rebind_visual_obstruction_control(
        source_candidate=source.candidates[0], current_candidate=before.candidates[0]
    )
    current = calibrate_popup_close_candidates(analysis=before, screenshot_png=raw)
    assert current is not before
    assert current.candidates[0].source_capture_id == "current-before"
    assert current.candidates[0].source_image_signature == before.image_signature
    assert can_rebind_visual_obstruction_control(
        source_candidate=source.candidates[0], current_candidate=current.candidates[0]
    )
    cx, cy = current.candidates[0].normalized_bounds.center
    assert NATIVE.left <= cx <= NATIVE.right
    assert NATIVE.top <= cy <= NATIVE.bottom


def test_source_outline_x_does_not_create_current_missing_x():
    """新帧无 X 时，旧帧有 X 不得生成当前像素证明。"""
    raw = _png(Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20)))
    before = _analysis(raw, capture="current-missing", wrong_vision=True)
    assert calibrate_popup_close_candidates(analysis=before, screenshot_png=raw) is before


def _synthetic(kind: str) -> bytes:
    image = Image.new("RGB", (240, 400), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    if kind == "filled_black_x":
        draw.ellipse((104, 264, 136, 296), fill=(250, 250, 250))
        color = (10, 10, 10)
    else:
        color = (255, 255, 255)
        if kind != "plain_x":
            if kind == "open_ring":
                draw.arc((104, 264, 136, 296), 20, 330, fill=color, width=2)
            else:
                draw.ellipse((104, 264, 136, 296), outline=color, width=2)
    if kind in ("empty_ring",):
        return _png(image)
    if kind == "plus":
        draw.line((111, 280, 129, 280), fill=color, width=3)
        draw.line((120, 271, 120, 289), fill=color, width=3)
    else:
        draw.line((112, 272, 128, 288), fill=color, width=3)
        draw.line((128, 272, 112, 288), fill=color, width=3)
    if kind == "fake_background":
        for offset in (-25, -20, 20, 25):
            draw.line((92, 280 + offset, 148, 280 + offset), fill=color, width=3)
    return _png(image)


@pytest.mark.parametrize("kind", ["plus", "empty_ring", "open_ring", "fake_background"])
def test_non_close_outline_patterns_fail_closed(kind: str):
    """加号、空圆、非闭合圆及周围文字状图案不得新增关闭控件。"""
    assert not detect_isolated_cross_controls(
        _synthetic(kind), expected_width=240, expected_height=400
    )


@pytest.mark.parametrize("kind", ["plain_x", "filled_black_x"])
def test_existing_plain_white_x_and_filled_circle_black_x_remain_detectable(kind: str):
    assert (
        len(
            detect_isolated_cross_controls(
                _synthetic(kind), expected_width=240, expected_height=400
            )
        )
        == 1
    )


def test_real_outline_bottom_x_still_issues_on_payment_promo():
    """付费／抢购文案层若底部安全位有真圆 X，仍须签发（禁果全网通类）。"""

    raw = _actual_icon_frame()
    analysis = _analysis(raw, capture="payment", payment=True)
    calibrated = calibrate_popup_close_candidates(analysis=analysis, screenshot_png=raw)
    assert calibrated is not analysis
    fact = calibrated.surface_facts["pixel_cross_close_calibration"]
    assert fact.get("close_kind") == "bottom_circular_x"


def test_multiple_outline_x_controls_do_not_get_pixel_disambiguated():
    image = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    icon = Image.open(ICON).convert("RGB")
    image.paste(icon, (453, 2060))
    image.paste(icon, (853, 2060))
    assert not detect_isolated_cross_controls(
        _png(image), expected_width=WIDTH, expected_height=HEIGHT
    )


def test_middle_outline_x_cannot_issue_a_close():
    image = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    image.paste(Image.open(ICON).convert("RGB"), (653, 1000))
    raw = _png(image)
    analysis = _analysis(raw, capture="middle", wrong_vision=True)
    assert calibrate_popup_close_candidates(analysis=analysis, screenshot_png=raw) is analysis


@pytest.mark.parametrize("current_x", [True, False])
async def test_runtime_binds_only_current_independently_calibrated_outline_x(current_x: bool):
    """正式 Resolver/Executor 只执行当前独立校准的 X，无 X 时 Driver 调用为零。"""
    source = _analysis(_actual_icon_frame(), capture=OBSERVATION_CAPTURE_ID)
    raw = (
        _actual_icon_frame() if current_x else _png(Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20)))
    )
    signature = "sha256:" + hashlib.sha256(raw).hexdigest()
    current = _before_visual_analysis(
        source.candidates[0],
        image_signature=signature,
        width=WIDTH,
        height=HEIGHT,
        package_name=source.package_name,
        activity=source.activity,
        normalized_bounds=NormalizedBounds(0.42, 0.555, 0.58, 0.62),
        visual_page_type="post_list",
        page_state="popup",
        overlay_signature="popup",
        surface_facts={"popup_visible": True},
    )
    current = calibrate_popup_close_candidates(analysis=current, screenshot_png=raw)
    executor, driver, _ = _executor(
        candidate=source.candidates[0],
        analysis=source,
        before_analysis=current,
        before_signature=signature,
        before_width=WIDTH,
        before_height=HEIGHT,
        before_package_name=source.package_name,
        before_activity=source.activity,
        allowed_packages=(source.package_name,),
    )
    result = await executor.execute(_request(source.candidates[0]))
    if current_x:
        assert result.status is InteractionResultStatus.EVIDENCE_PENDING
        assert driver.execute_count == 1
        assert driver.last_intent.source_capture_id == BEFORE_CAPTURE_ID
        target = driver.last_intent.parameters["resolved_target"]
        assert 653 <= target["x"] < 787 and 2060 <= target["y"] < 2194
    else:
        assert result.status is InteractionResultStatus.FAILED
        assert result.error_code == "APP_CANDIDATE_REVALIDATION_REQUIRED"
        assert driver.execute_count == 0
