"""App 截图业务内容指纹：排除状态栏后比较页面，避免全图 hash 一票否决。

正式证据仍使用完整 ``image_signature``；本指纹只用于视觉复用、重复状态计数
与报告折叠，不得替代 Capture checksum。
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Mapping
from typing import Any

_LOGGER = logging.getLogger(__name__)

CONTENT_FINGERPRINT_PROTOCOL_VERSION = "app-business-content-fingerprint.v1"
CONTENT_FINGERPRINT_REUSE_REASON = "same_business_content_fingerprint"
# 真实手机截图高度下限；更小的夹具图不裁切，避免 1x1 PNG 测试被掏空。
_MIN_CROP_FRAME_HEIGHT = 200
_STATUS_BAR_RATIO = 0.04
_STATUS_BAR_MIN_PX = 24
_STATUS_BAR_MAX_PX = 88


def status_bar_crop_top_px(height: int) -> int:
    """状态栏裁切高度；小图返回 0。"""

    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or height < _MIN_CROP_FRAME_HEIGHT
    ):
        return 0
    return max(_STATUS_BAR_MIN_PX, min(_STATUS_BAR_MAX_PX, int(round(height * _STATUS_BAR_RATIO))))


def compute_app_business_content_fingerprint(
    png: bytes,
    *,
    width: int,
    height: int,
) -> dict[str, Any] | None:
    """对去状态栏后的 RGB 像素做 sha256；失败返回 None，不得抛给主链。"""

    if not isinstance(png, (bytes, bytearray)) or not png:
        return None
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
    ):
        return None
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 缺依赖时退回全图签名比较
        return None
    try:
        image = Image.open(io.BytesIO(bytes(png))).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 非法 PNG 不阻断正式 checksum 路径
        _LOGGER.warning(
            "App 业务内容指纹解码失败 error=%s",
            type(exc).__name__,
        )
        return None
    actual_width, actual_height = image.size
    crop_top = status_bar_crop_top_px(actual_height)
    if crop_top >= actual_height:
        crop_top = 0
    cropped = image.crop((0, crop_top, actual_width, actual_height))
    digest = hashlib.sha256(cropped.tobytes()).hexdigest()
    return {
        "protocol_version": CONTENT_FINGERPRINT_PROTOCOL_VERSION,
        "value": f"sha256:{digest}",
        "excluded_band": "status_bar" if crop_top else "none",
        "crop_top_px": crop_top,
        "width": actual_width,
        "height": actual_height,
    }


def content_fingerprint_value(source: object) -> str | None:
    """从 Analysis／Context／surface_facts 读取业务内容指纹。"""

    if source is None:
        return None
    direct = getattr(source, "content_fingerprint", None)
    text = _sha256_text(direct)
    if text is not None:
        return text
    facts: object | None = getattr(source, "surface_facts", None)
    if facts is None and isinstance(source, Mapping):
        if "content_fingerprint" in source:
            facts = source
        elif "value" in source:
            return _sha256_text(source.get("value"))
        else:
            facts = source
    if not isinstance(facts, Mapping):
        return None
    raw = facts.get("content_fingerprint")
    if isinstance(raw, Mapping):
        return _sha256_text(raw.get("value"))
    return _sha256_text(raw)


def content_fingerprints_match(left: object, right: object) -> bool:
    first = content_fingerprint_value(left)
    second = content_fingerprint_value(right)
    return bool(first and second and first == second)


def _sha256_text(value: object) -> str | None:
    if isinstance(value, str) and value.startswith("sha256:") and len(value) > 7:
        return value
    return None
