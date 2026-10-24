"""Web 1920×1080 截图只允许压文件体积，不允许缩放像素。"""

from io import BytesIO

from PIL import Image

from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
    _compress_png_without_resize,
)


def test_web_screenshot_compression_keeps_1920_by_1080() -> None:
    image = Image.new("RGB", (1920, 1080), color=(30, 90, 160))
    raw_buffer = BytesIO()
    image.save(raw_buffer, format="PNG", compress_level=0)
    raw = raw_buffer.getvalue()

    optimized, declared_size = _compress_png_without_resize(raw)
    with Image.open(BytesIO(raw)) as raw_image, Image.open(
        BytesIO(optimized)
    ) as optimized_image:
        assert raw_image.size == (1920, 1080)
        assert optimized_image.size == (1920, 1080)

    assert declared_size == (1920, 1080)
    assert len(optimized) <= len(raw)
