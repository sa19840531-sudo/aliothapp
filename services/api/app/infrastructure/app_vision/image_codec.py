"""App 视觉理解前的截图压缩工具。"""

from __future__ import annotations

import io
import os

# 默认 JPEG 质量：保持最长边不变，只降体积以缩短上传／供应商处理。
# 可用 ALIOTH_APP_VISION_JPEG_QUALITY 覆盖（1–95）。
_DEFAULT_JPEG_QUALITY = 50


def resolve_app_vision_jpeg_quality(explicit: int | None = None) -> int:
    """解析送入视觉模型的 JPEG 质量（不改变分辨率策略）。"""
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int) or not 1 <= explicit <= 95:
            raise ValueError("quality 必须在 1 到 95 之间")
        return explicit
    raw = os.getenv("ALIOTH_APP_VISION_JPEG_QUALITY", "").strip()
    if not raw:
        return _DEFAULT_JPEG_QUALITY
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            "ALIOTH_APP_VISION_JPEG_QUALITY 必须为 1 到 95 的整数"
        ) from exc
    if not 1 <= value <= 95:
        raise ValueError("ALIOTH_APP_VISION_JPEG_QUALITY 必须在 1 到 95 之间")
    return value


def compress_png_bytes_for_llm(
    png_bytes: bytes,
    *,
    max_side: int = 720,
    quality: int | None = None,
) -> tuple[bytes, str]:
    """将 PNG 压缩为 LLM 可接受的体积，返回 (bytes, mime)。

    分辨率策略不变：最长边不超过 ``max_side``。体积靠 JPEG 质量与 chroma
    子采样压低，避免大图拖慢视觉判定。
    """
    if not isinstance(png_bytes, bytes) or not png_bytes:
        raise ValueError("png_bytes 必须为非空 bytes")
    if (
        isinstance(max_side, bool)
        or not isinstance(max_side, int)
        or max_side <= 0
    ):
        raise ValueError("max_side 必须为正整数")
    resolved_quality = resolve_app_vision_jpeg_quality(quality)

    try:
        from PIL import Image
    except ImportError:
        return png_bytes, "image/png"

    try:
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except OSError:
        # 损坏或非标准 PNG 时回退原字节，交由上游校验失败，而不是在压缩层崩溃
        return png_bytes, "image/png"
    width, height = image.size
    scale = min(1.0, float(max_side) / float(max(width, height)))
    if scale < 1.0:
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
        )
    buffer = io.BytesIO()
    # subsampling=2 → 4:2:0，在相同宽高下进一步减小体积
    image.save(
        buffer,
        format="JPEG",
        quality=resolved_quality,
        optimize=True,
        progressive=True,
        subsampling=2,
    )
    return buffer.getvalue(), "image/jpeg"
