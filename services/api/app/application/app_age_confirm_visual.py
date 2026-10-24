"""年龄门视觉清障：WebView／画布无 UIA 文案时，凭双钮几何或 Vision 证据定位进入键。

黑料不打烊等 App 的「我年满18岁-进入」常在 WebView／自绘层，Accessibility 无年满文案；
离开键为灰色，进入键为橘色。优先「橘色进入 + 上方深色离开条」几何，其次才用
Vision 年龄门语义 + 默认双钮框；禁止把开屏单一橙色 CTA 当成年确认。
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.app_login_detect_only_contract import (
    build_app_age_gate_protocol_snapshot,
    classify_login_surface,
    login_action_allowed,
)
from services.api.app.domain.exploration import NormalizedBounds
from services.api.app.domain.exploration.age_confirm import text_matches_age_confirm

APP_AGE_CONFIRM_VISUAL_VERSION = "app-age-confirm-visual.v1"
# 双钮年龄门常见布局：橘色进入键居中偏下（仅在有语义证据时作兜底）。
DEFAULT_AGE_ENTER_BOUNDS = NormalizedBounds(0.12, 0.56, 0.88, 0.66)

_AGE_GATE_TITLE_MARKERS: tuple[str, ...] = (
    "请确定你已年满18",
    "请确认你已年满18",
    "年满18岁",
    "已年满18",
    "age verification",
    "18岁",
    "18歲",
)


def analysis_has_age_gate_evidence(
    *,
    page_state: object = None,
    key_elements: Sequence[object] | None = None,
    visual_summary: object = None,
    surface_facts: Mapping[str, object] | None = None,
    candidate_texts: Sequence[object] | None = None,
) -> bool:
    """当前帧是否具备年龄确认门禁语义（不依赖 UIA）。"""

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    state = str(page_state or "").strip().casefold()
    if state in {"auth_gate", "auth_required"}:
        return True
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {"age_verification", "age_confirm", "age_gate"}:
        return True
    if facts.get("auth_gate_visible") is True:
        return True
    blobs: list[str] = []
    if isinstance(visual_summary, str) and visual_summary.strip():
        blobs.append(visual_summary)
    for item in key_elements or ():
        if isinstance(item, str) and item.strip():
            blobs.append(item)
    for item in candidate_texts or ():
        if isinstance(item, str) and item.strip():
            blobs.append(item)
    for key in ("dismiss_semantic_target", "action_semantic_target", "close_semantic_target"):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            blobs.append(value)
    joined = " ".join(blobs)
    if not joined.strip():
        return False
    if text_matches_age_confirm(joined):
        return True
    lowered = joined.casefold()
    return any(marker.casefold() in lowered for marker in _AGE_GATE_TITLE_MARKERS)


def resolve_age_confirm_visual_bounds(
    *,
    screenshot_png: bytes | None,
    allow_default_layout: bool,
    require_semantic_evidence: bool,
) -> tuple[NormalizedBounds, str]:
    """优先橘色+上方离开条检测。

    - 双钮几何命中时可不依赖 Vision 文案（WebView 无障碍常见）。
    - 默认布局框仅在已有年龄门语义证据时允许。
    """

    if screenshot_png:
        detected = detect_orange_age_enter_bounds(screenshot_png)
        if detected is not None:
            return detected, "orange_blob"
    if allow_default_layout and require_semantic_evidence:
        return DEFAULT_AGE_ENTER_BOUNDS, "default_dual_button_layout"
    raise ValueError("age_confirm_visual_bounds_unavailable")


def detect_orange_age_enter_bounds(
    screenshot_png: bytes,
) -> NormalizedBounds | None:
    """在屏下半定位唯一橘色进入键；失败返回 None（不得猜整屏）。

    额外要求正上方存在更深色条带（离开键），以区分开屏单一橙色 CTA。
    """

    if not screenshot_png:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
    except Exception:  # noqa: BLE001 - 坏图不得签发触控
        return None
    width, height = image.size
    if width < 80 or height < 160:
        return None

    y0 = int(height * 0.48)
    y1 = int(height * 0.78)
    x0 = int(width * 0.08)
    x1 = int(width * 0.92)
    step = max(2, min(width, height) // 400)

    orange_points: list[tuple[int, int]] = []
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            r, g, b = image.getpixel((x, y))
            if _is_age_enter_orange(r, g, b):
                orange_points.append((x, y))
    if len(orange_points) < 40:
        return None

    xs = [p[0] for p in orange_points]
    ys = [p[1] for p in orange_points]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    pad = step
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(width - 1, right + pad)
    bottom = min(height - 1, bottom + pad)

    bw = (right - left) / width
    bh = (bottom - top) / height
    cx = ((left + right) / 2) / width
    cy = ((top + bottom) / 2) / height
    if not (0.28 <= bw <= 0.92 and 0.018 <= bh <= 0.14):
        return None
    if not (0.28 <= cx <= 0.72 and cy >= 0.50):
        return None
    if top < height * 0.45:
        return None
    if not _has_dark_leave_band_above(
        image,
        left=left,
        right=right,
        orange_top=top,
        screen_height=height,
        step=step,
    ):
        return None
    return NormalizedBounds(
        left=left / width,
        top=top / height,
        right=right / width,
        bottom=bottom / height,
    )


def _has_dark_leave_band_above(
    image: Any,
    *,
    left: int,
    right: int,
    orange_top: int,
    screen_height: int,
    step: int,
) -> bool:
    """橘色进入键正上方应有更深色「离开」条，避免误点开屏单色 CTA。"""

    band_bottom = max(0, orange_top - step)
    band_top = max(0, orange_top - int(screen_height * 0.10))
    if band_bottom <= band_top:
        return False
    dark = 0
    total = 0
    for y in range(band_top, band_bottom, max(1, step)):
        for x in range(left, right, max(1, step)):
            r, g, b = image.getpixel((x, y))
            total += 1
            luminance = (r + g + b) / 3
            # 离开键是深灰按钮，不是纯黑背景。
            if 35 <= luminance <= 110 and abs(r - g) < 30 and abs(g - b) < 30:
                dark += 1
    if total < 20:
        return False
    return (dark / total) >= 0.35


def _is_age_enter_orange(r: int, g: int, b: int) -> bool:
    """黑料进入键橙黄渐变；排除灰色离开键与低饱和背景。"""

    return (
        r >= 190
        and 90 <= g <= 210
        and b <= 110
        and r > g + 25
        and g > b + 20
    )


def build_age_confirm_visual_fact(
    *,
    bounds: NormalizedBounds,
    source: str,
    artifact_hint: str | None = None,
) -> dict[str, Any]:
    """写入 surface_facts，供探索／审计引用。"""

    return {
        "protocol_version": APP_AGE_CONFIRM_VISUAL_VERSION,
        "normalized_bounds": bounds.to_dict(),
        "bounds_source": source,
        "label": "我年满18岁-进入",
        "artifact_hint": artifact_hint,
        "clearance_only": True,
        "long_term_fixed_coordinates_forbidden": True,
    }


def build_login_and_age_gate_alignment(
    *,
    page_state: object = None,
    surface_facts: Mapping[str, object] | None = None,
    adult_declaration_frozen: bool = False,
    unique_enter_control: bool = False,
    confidence_ok: bool = False,
    requires_identity_or_payment: bool = False,
) -> dict[str, object]:
    """登录 detect-only 与 18+ 入口协议对齐快照。"""

    login = classify_login_surface(page_state=page_state, surface_facts=surface_facts)
    age = build_app_age_gate_protocol_snapshot(
        adult_declaration_frozen=adult_declaration_frozen,
        unique_enter_control=unique_enter_control,
        confidence_ok=confidence_ok,
        requires_identity_or_payment=requires_identity_or_payment,
    )
    return {"login": login, "age_gate": age, "login_action_allowed": login_action_allowed}
