"""Toast 聚合只采用动作窗口中可见、独立且样式完整的反馈根。"""

import math
import re
from collections.abc import Mapping


def valid_toast_sample(message: Mapping) -> bool:
    if message.get("kind") not in {"toast", "snackbar", "notification", "alert", "status"}:
        return False
    text = str(message.get("text") or "").strip()
    if not text or re.fullmatch(r"(?:加载中|正在加载|loading|请稍候|请稍等)[.。…\s]*", text, re.I):
        return False
    rect, viewport, style = (message.get(key) for key in ("rect", "viewport", "style"))
    if not all(isinstance(value, Mapping) for value in (rect, viewport, style)):
        return False
    try:
        x, y, width, height = (float(rect[key]) for key in ("x", "y", "width", "height"))
        vw, vh = (float(viewport[key]) for key in ("width", "height"))
        confidence = float(message["platform_ui_confidence"])
        font = float(str(style["font_size"]).removesuffix("px"))
        opacity = float(style["background_opacity"])
    except (KeyError, ValueError, TypeError):
        return False
    return bool(
        all(
            math.isfinite(value)
            for value in (x, y, width, height, vw, vh, confidence, font, opacity)
        )
        and min(width, height, vw, vh, font) > 0
        and 0.7 <= confidence <= 1
        and 0 <= opacity <= 1
        and x < vw
        and y < vh
        and x + width > 0
        and y + height > 0
        and style.get("text_color")
        and style.get("background_color")
        and message.get("position_zone")
        and message.get("event_id")
    )
