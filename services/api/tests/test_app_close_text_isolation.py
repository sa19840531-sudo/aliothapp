"""正文交叉笔画与真正孤立关闭控件的近邻隔离回归。"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.app_visual_close_calibration import (
    calibrate_popup_close_candidates,
    detect_isolated_cross_controls,
)
from services.api.app.domain.exploration import AppVisualAnalysis


def _screenshot(*, scale: int, glyph: bool, real_close: bool) -> bytes:
    image = Image.new("RGB", (240 * scale, 400 * scale), (15, 15, 15))
    draw = ImageDraw.Draw(image)

    def line(points: tuple[int, ...], width: int = 2) -> None:
        draw.line(
            tuple(value * scale for value in points), fill=(220, 220, 220), width=width * scale
        )

    if glyph:
        # 交叉笔画和三边外框互不连通，模拟「区」字而不依赖系统字体。
        line((100, 110, 126, 136))
        line((126, 110, 100, 136))
        line((130, 104, 94, 104, 94, 142, 130, 142))
    if real_close:
        line((172, 292, 188, 308), 3)
        line((188, 292, 172, 308), 3)
    result = io.BytesIO()
    image.save(result, format="PNG")
    return result.getvalue()


def _analysis(scale: int) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id="capture-text-cross",
        screenshot_artifact_id="artifact-text-cross",
        image_signature="sha256:" + "a" * 64,
        width=240 * scale,
        height=400 * scale,
        orientation="portrait",
        visual_page_type="content_detail",
        visual_summary="帖子详情正文",
        key_elements=("正文",),
        candidates=(),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        page_state="stable",
        surface_facts={"popup_visible": False, "dialog_visible": False},
    )


@pytest.mark.parametrize("scale", (1, 2, 4))
def test_text_cross_does_not_create_popup_or_dismiss_candidate(scale: int) -> None:
    screenshot = _screenshot(scale=scale, glyph=True, real_close=False)
    analysis = _analysis(scale)

    assert (
        detect_isolated_cross_controls(
            screenshot,
            expected_width=analysis.width,
            expected_height=analysis.height,
        )
        == ()
    )
    assert (
        calibrate_popup_close_candidates(analysis=analysis, screenshot_png=screenshot) is analysis
    )
    assert analysis.page_state == "stable"
    assert analysis.surface_facts["popup_visible"] is False
    assert analysis.candidates == ()


@pytest.mark.parametrize("glyph", (False, True))
def test_real_close_remains_available_beside_text(glyph: bool) -> None:
    analysis = _analysis(2)
    screenshot = _screenshot(scale=2, glyph=glyph, real_close=True)

    calibrated = calibrate_popup_close_candidates(analysis=analysis, screenshot_png=screenshot)

    assert len(calibrated.candidates) == 1
    candidate = calibrated.candidates[0]
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx((0.75, 0.75), abs=0.02)
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["ambiguity_count"] == 1
