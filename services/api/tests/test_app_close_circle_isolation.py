"""圆形关闭控件周边亮色隔离与白 X 回归测试。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.app_visual_close_calibration import (
    calibrate_popup_close_candidates,
    detect_isolated_cross_controls,
    is_pixel_calibrated_close_candidate,
)
from services.api.app.domain.exploration import AppVisualAnalysis

_IMAGE_WIDTH = 240
_IMAGE_HEIGHT = 400
_SIGNATURE = "sha256:" + "f" * 64


def _png_with_circles(
    *,
    noisy_circle: bool = False,
    distant_real_circle: bool = False,
    stroke_width: int = 3,
) -> bytes:
    """生成亮圆黑 X；可选的周围亮色图案模拟文字孔洞误检。"""

    image = Image.new("RGB", (_IMAGE_WIDTH, _IMAGE_HEIGHT), color=(28, 28, 28))
    draw = ImageDraw.Draw(image)

    def circle(center_x: int, center_y: int) -> None:
        radius = 16
        draw.ellipse(
            (
                center_x - radius,
                center_y - radius,
                center_x + radius,
                center_y + radius,
            ),
            fill=(250, 250, 250),
        )
        arm = 8
        draw.line(
            (center_x - arm, center_y - arm, center_x + arm, center_y + arm),
            fill=(12, 12, 12),
            width=stroke_width,
        )
        draw.line(
            (center_x + arm, center_y - arm, center_x - arm, center_y + arm),
            fill=(12, 12, 12),
            width=stroke_width,
        )

    if noisy_circle:
        circle(60, 140)
        # 图案与圆形控件保持间隔，仍落在圆形检测的周边窗口内。
        draw.rectangle((20, 96, 100, 112), fill=(245, 245, 245))
        draw.rectangle((20, 168, 100, 184), fill=(245, 245, 245))
        draw.rectangle((22, 118, 42, 124), fill=(235, 235, 235))
        draw.rectangle((78, 156, 98, 162), fill=(235, 235, 235))
    else:
        circle(120, 300)

    if distant_real_circle:
        circle(180, 300)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_with_circle_and_outer_bright_material(*, direction: str) -> bytes:
    """生成真圆黑 X，以及落在半宽外、全宽内的上下亮素材。

    圆钮中心 y≈0.58，对齐大图广告下方圆 X 甜区（勿落在中部假 X 拒签带）。
    中部孤立 X 仍禁止签发；本夹具只回归半宽邻域绕过远侧亮素材。
    """

    image = Image.new("RGB", (_IMAGE_WIDTH, _IMAGE_HEIGHT), color=(28, 28, 28))
    draw = ImageDraw.Draw(image)
    center_x, center_y = 120, 232
    radius = 16
    draw.ellipse(
        (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        ),
        fill=(250, 250, 250),
    )
    arm = 8
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
    if direction == "above":
        bright_top, bright_bottom = 187, 197
    elif direction == "below":
        bright_top, bright_bottom = 267, 277
    else:
        raise ValueError(direction)
    # 真圆组件约 y=216..248；半宽外框约 y=200..264，素材仍在旧全宽邻域内。
    draw.rectangle(
        (85, bright_top, 155, bright_bottom),
        fill=(245, 245, 245),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_with_eye_icon() -> bytes:
    """生成白色眼形和居中黑瞳，验证黑瞳不能伪装成四臂 X。"""

    image = Image.new("RGB", (_IMAGE_WIDTH, _IMAGE_HEIGHT), color=(28, 28, 28))
    draw = ImageDraw.Draw(image)
    center_x, center_y = 120, 300
    draw.ellipse(
        (center_x - 15, center_y - 11, center_x + 15, center_y + 11),
        fill=(250, 250, 250),
    )
    draw.ellipse(
        (center_x - 5, center_y - 5, center_x + 5, center_y + 5),
        fill=(12, 12, 12),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _isolated_white_x_png() -> bytes:
    """生成原有白笔画孤立 X，确认白 X 分支不受圆形隔离修复影响。"""

    image = Image.new("RGB", (_IMAGE_WIDTH, _IMAGE_HEIGHT), color=(28, 28, 28))
    draw = ImageDraw.Draw(image)
    center_x, center_y = (120, 300)
    arm = 8
    draw.line(
        (center_x - arm, center_y - arm, center_x + arm, center_y + arm),
        fill=(255, 255, 255),
        width=3,
    )
    draw.line(
        (center_x + arm, center_y - arm, center_x - arm, center_y + arm),
        fill=(255, 255, 255),
        width=3,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _analysis() -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id="capture-close-circle-isolation",
        screenshot_artifact_id="artifact-close-circle-isolation",
        image_signature=_SIGNATURE,
        width=_IMAGE_WIDTH,
        height=_IMAGE_HEIGHT,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="首页内容流",
        key_elements=("内容流",),
        candidates=(),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )


def _detected(png: bytes):
    return detect_isolated_cross_controls(
        png,
        expected_width=_IMAGE_WIDTH,
        expected_height=_IMAGE_HEIGHT,
    )


def test_bright_circle_with_surrounding_pattern_is_not_detected_or_calibrated() -> None:
    """周围亮色文字／图案比例过高的黑 X 不能伪装成关闭控件。"""

    screenshot = _png_with_circles(noisy_circle=True)

    assert _detected(screenshot) == ()
    analysis = _analysis()
    assert calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    ) is analysis


def test_eye_icon_black_pupil_is_not_detected_as_circular_close() -> None:
    """黑瞳只覆盖中心，不能满足黑色 X 四个外臂均延伸的条件。"""

    screenshot = _png_with_eye_icon()
    analysis = _analysis()

    assert _detected(screenshot) == ()
    assert calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    ) is analysis


def test_isolated_bright_circle_black_x_is_detected_and_calibrated() -> None:
    """周边干净的亮圆黑 X 仍可签发精准关闭候选。"""

    screenshot = _png_with_circles()
    detected = _detected(screenshot)

    assert len(detected) == 1
    assert detected[0].center == pytest.approx((0.5, 0.75), abs=0.03)
    calibrated = calibrate_popup_close_candidates(
        analysis=_analysis(),
        screenshot_png=screenshot,
    )

    assert len(calibrated.candidates) == 1
    assert is_pixel_calibrated_close_candidate(calibrated.candidates[0])
    assert calibrated.candidates[0].control_role == "dismiss"
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["mode"] == "issue"


def test_hjll_portrait_popup_bottom_circular_x_fixture_detected() -> None:
    """真人像广告截图：下方白底圆 X 不得因上方亮色素材 surrounding 漏检。"""

    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "hjll_portrait_popup_bottom_x.jpg"
    )
    screenshot = fixture.read_bytes()
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(screenshot))
    width, height = image.size
    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=width,
        expected_height=height,
    )
    assert len(detected) >= 1
    assert any(
        0.45 <= item.center[0] <= 0.55 and 0.68 <= item.center[1] <= 0.74
        for item in detected
    )


def test_bright_circle_black_x_with_realistic_stroke_is_retained() -> None:
    """接近真实 VIP 圆 X 的粗笔画仍能通过外围四臂校准。"""

    detected = _detected(_png_with_circles(stroke_width=5))

    assert len(detected) == 1
    assert detected[0].center == pytest.approx((0.5, 0.75), abs=0.03)


@pytest.mark.parametrize("direction", ("above", "below"))
def test_circle_with_outer_material_keeps_half_padding_calibration(
    direction: str,
) -> None:
    """半宽邻域应绕过远侧亮素材，并保留真圆 X 的精准校准点。"""

    screenshot = _png_with_circle_and_outer_bright_material(direction=direction)
    detected = _detected(screenshot)

    assert len(detected) == 1
    assert detected[0].center == pytest.approx((0.5, 0.58), abs=0.03)
    calibrated = calibrate_popup_close_candidates(
        analysis=_analysis(),
        screenshot_png=screenshot,
    )

    assert len(calibrated.candidates) == 1
    candidate = calibrated.candidates[0]
    assert is_pixel_calibrated_close_candidate(candidate)
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx((0.5, 0.58), abs=0.03)
    assert (
        calibrated.surface_facts["pixel_cross_close_calibration"]["detected_bounds"]
        .get("left")
        == pytest.approx(104 / _IMAGE_WIDTH)
    )


def test_mixed_noisy_circle_and_distant_real_circle_only_signs_real_circle() -> None:
    """混合假圆噪声与远处真圆时，只保留周边孤立的真圆关闭点。"""

    screenshot = _png_with_circles(
        noisy_circle=True,
        distant_real_circle=True,
    )
    detected = _detected(screenshot)

    assert len(detected) == 1
    assert detected[0].center == pytest.approx((0.75, 0.75), abs=0.03)
    calibrated = calibrate_popup_close_candidates(
        analysis=_analysis(),
        screenshot_png=screenshot,
    )

    assert len(calibrated.candidates) == 1
    issued = calibrated.candidates[0]
    assert is_pixel_calibrated_close_candidate(issued)
    assert issued.normalized_bounds is not None
    assert issued.normalized_bounds.center == pytest.approx((0.75, 0.75), abs=0.03)


def test_isolated_white_x_calibration_remains_unchanged() -> None:
    """圆形周边隔离校验不能破坏既有白笔画 X 救援。"""

    screenshot = _isolated_white_x_png()
    detected = _detected(screenshot)

    assert len(detected) == 1
    assert detected[0].center == pytest.approx((0.5, 0.75), abs=0.03)
    calibrated = calibrate_popup_close_candidates(
        analysis=_analysis(),
        screenshot_png=screenshot,
    )

    assert len(calibrated.candidates) == 1
    assert is_pixel_calibrated_close_candidate(calibrated.candidates[0])
    assert calibrated.candidates[0].control_role == "dismiss"
