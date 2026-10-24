"""规则测试截图规范化：常见图片格式统一转成 PNG（判定只把 PNG 截图交给模型）。"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 超长截图等比缩到这个边长以内，避免单张图拖垮模型请求
MAX_SCREENSHOT_SIDE = 4096


class ScreenshotDecodeError(ValueError):
    """上传的文件不是可识别的图片。"""


def normalize_screenshot_png(payload: bytes) -> tuple[bytes, int, int]:
    """返回 (PNG 字节, 宽, 高)；已是尺寸合规的 PNG 时原样保留。"""
    if not payload:
        raise ScreenshotDecodeError("图片内容为空")
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            opened.load()
            image = opened.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ScreenshotDecodeError("不是可识别的图片") from exc
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ScreenshotDecodeError("图片尺寸无效")
    if payload.startswith(_PNG_SIGNATURE) and max(width, height) <= MAX_SCREENSHOT_SIDE:
        return payload, width, height
    has_alpha = image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )
    image = image.convert("RGBA" if has_alpha else "RGB")
    if max(width, height) > MAX_SCREENSHOT_SIDE:
        image.thumbnail((MAX_SCREENSHOT_SIDE, MAX_SCREENSHOT_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), image.size[0], image.size[1]
