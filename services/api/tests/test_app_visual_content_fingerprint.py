"""业务内容指纹：排除状态栏后比较页面。"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from services.api.app.application.app_visual_content_fingerprint import (
    compute_app_business_content_fingerprint,
    content_fingerprints_match,
    status_bar_crop_top_px,
)


def _png(*, top_color: tuple[int, int, int], body_color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (400, 800), body_color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 399, 31), fill=top_color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_status_bar_crop_skips_tiny_fixture_frames() -> None:
    assert status_bar_crop_top_px(1) == 0
    assert status_bar_crop_top_px(1920) == 77


def test_content_fingerprint_ignores_status_bar_clock() -> None:
    first = compute_app_business_content_fingerprint(
        _png(top_color=(10, 10, 10), body_color=(30, 80, 120)),
        width=400,
        height=800,
    )
    second = compute_app_business_content_fingerprint(
        _png(top_color=(250, 20, 20), body_color=(30, 80, 120)),
        width=400,
        height=800,
    )
    changed_body = compute_app_business_content_fingerprint(
        _png(top_color=(10, 10, 10), body_color=(90, 10, 10)),
        width=400,
        height=800,
    )

    assert first is not None and second is not None and changed_body is not None
    assert first["value"] == second["value"]
    assert first["value"] != changed_body["value"]
    assert content_fingerprints_match(first, second)
    assert not content_fingerprints_match(first, changed_body)
