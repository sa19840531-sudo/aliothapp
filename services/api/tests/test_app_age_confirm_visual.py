"""年龄门视觉清障：橘色进入键 + 上方离开条。"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from services.api.app.application.app_age_confirm_visual import (
    analysis_has_age_gate_evidence,
    detect_orange_age_enter_bounds,
    resolve_age_confirm_visual_bounds,
)
from services.api.app.application.app_screenshot_understanding import (
    _append_visual_age_confirm_candidate,
)
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel


def _age_gate_png(*, width: int = 360, height: int = 780) -> bytes:
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    # 灰色离开键
    draw.rounded_rectangle(
        (40, int(height * 0.52), width - 40, int(height * 0.58)),
        radius=16,
        fill=(70, 70, 70),
    )
    # 橘色进入键
    draw.rounded_rectangle(
        (40, int(height * 0.60), width - 40, int(height * 0.66)),
        radius=16,
        fill=(255, 150, 40),
    )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_detect_orange_age_enter_requires_dark_leave_band() -> None:
    bounds = detect_orange_age_enter_bounds(_age_gate_png())
    assert bounds is not None
    assert bounds.top >= 0.45
    assert 0.28 <= ((bounds.left + bounds.right) / 2) <= 0.72

    # 仅橘色、无上方离开条 → 拒绝（开屏 CTA）
    image = Image.new("RGB", (360, 780), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((40, 470, 320, 520), radius=16, fill=(255, 150, 40))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    assert detect_orange_age_enter_bounds(buf.getvalue()) is None


def test_resolve_default_layout_requires_semantic_evidence() -> None:
    bounds, source = resolve_age_confirm_visual_bounds(
        screenshot_png=None,
        allow_default_layout=True,
        require_semantic_evidence=True,
    )
    assert source == "default_dual_button_layout"
    assert bounds.top >= 0.5
    try:
        resolve_age_confirm_visual_bounds(
            screenshot_png=None,
            allow_default_layout=True,
            require_semantic_evidence=False,
        )
        raise AssertionError("expected unavailable")
    except ValueError:
        pass


def test_analysis_has_age_gate_evidence_from_title() -> None:
    assert analysis_has_age_gate_evidence(
        page_state="startup_ad",
        key_elements=("请确定你已年满18岁", "我年满18岁-进入"),
        visual_summary="年龄确认弹窗",
    )
    assert not analysis_has_age_gate_evidence(
        page_state="startup_ad",
        key_elements=("跳过广告",),
        visual_summary="开屏轮播",
    )


def test_append_visual_age_confirm_from_orange_blob() -> None:
    analysis = AppVisualAnalysis.build(
        capture_id="cap-age",
        screenshot_artifact_id="art-age",
        image_signature="sha256:" + ("a" * 64),
        width=360,
        height=780,
        orientation="portrait",
        visual_page_type="guide_page",
        visual_summary="开屏",
        key_elements=("轮播",),
        candidates=(),
        provider_id="test",
        model_id="test",
        package_name="uk.frtju.nnxsaf",
        page_state="startup_ad",
    )
    updated = _append_visual_age_confirm_candidate(
        analysis=analysis,
        screenshot_png=_age_gate_png(),
        max_candidates=8,
    )
    assert updated.page_state == "auth_gate"
    assert updated.candidates
    first = updated.candidates[0]
    assert first.semantic_name == "我年满18岁-进入"
    assert first.action_type is InteractionOperation.CLICK
    assert first.risk_level is InteractionRiskLevel.LOW
    assert updated.surface_facts.get("dialog_kind") == "age_verification"
