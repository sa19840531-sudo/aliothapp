"""用当前截图像素校准主页上方分类落点（对齐底栏 pixel 校准方式）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_VISION,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)

PIXEL_HOME_TOP_CATEGORY_CONTRACT = "app-visual-home-top-category-calibration.v1"
_PIXEL_PURPOSE_MARKER = "当前截图视觉像素上方分类校准"
# 与 hjll_ui_tree_features.home_top_category_tabs 对齐：搜索栏下文字栏。
# 七格文字铺到接近右缘；0.88 会把「动漫」中心压到「乱伦」右侧空隙（约 x=0.56），连点仍停在乱伦。
_BAND_TOP = 0.105
_BAND_BOTTOM = 0.175
_GLYPH_TOP = 0.118
_GLYPH_BOTTOM = 0.150
_DEFAULT_SLOT_COUNT = 7
_USABLE_RIGHT = 0.995
_HORIZONTAL_PAD = 0.015
# 与底栏一致：从左到右完整栏序（含 skip），再过滤可点课纲。
_DEFAULT_LAYOUT_LABELS = (
    "最新",
    "精选短剧",
    "推荐",
    "国产",
    "乱伦",
    "动漫",
    "漫画",
)
_DEFAULT_CLICKABLE = (
    ("最新", 0),
    ("国产", 3),
    ("动漫", 5),
    ("漫画", 6),
)
_CURRICULUM_CLICKABLE_LABELS = frozenset({"最新", "国产", "动漫", "漫画"})
_FULL_STRIP_LABELS = (
    "最新",
    "精选短剧",
    "推荐",
    "国产",
    "乱伦",
    "动漫",
    "漫画",
    "日本AV",
    "欧美",
    "制服",
    "OnlyFans",
    "华语原创",
    "三级片",
)
_STRIP_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "精选短剧": ("精选短剧", "精選短劇"),
    "推荐": ("推荐", "推薦"),
    "国产": ("国产", "國產"),
    "乱伦": ("乱伦", "亂倫", "乱伦综", "亂倫綜"),
    "动漫": ("动漫", "動漫"),
    "漫画": ("漫画", "漫畫"),
    "日本AV": ("日本AV", "日本av"),
    "欧美": ("欧美", "歐美"),
    "OnlyFans": ("OnlyFans", "onlyfans", "Only Fans", "only fans"),
    "华语原创": ("华语原创", "華語原創"),
    "三级片": ("三级片", "三級片"),
}
_OFF_CURRICULUM_MARKERS = (
    "制服",
    "OnlyFans",
    "onlyfans",
    "Only Fans",
    "欧美",
    "歐美",
    "华语原创",
    "華語原創",
    "日本AV",
    "日本av",
    "三级片",
    "三級片",
)


def visible_home_top_strip_labels(blob: str) -> tuple[str, ...]:
    """从 OCR／摘要还原当前可见上方栏序；横滑后不再假装还是最新起七格。"""

    text = str(blob or "")
    if not text.strip():
        return ()
    present: list[str] = []
    for label in _FULL_STRIP_LABELS:
        aliases = _STRIP_LABEL_ALIASES.get(label, (label,))
        if any(alias in text for alias in aliases) or label in text:
            present.append(label)
    if "日本AV" in present and "最新" not in present:
        present = [
            item
            for item in present
            if item not in {"最新", "精选短剧", "推荐", "国产"}
        ]
    # 已滑到 OnlyFans 且课纲栏都不在 OCR 里：只保留右侧 skip 栏，强制滑回。
    if (
        "OnlyFans" in present
        and "最新" not in present
        and "动漫" not in present
        and "漫画" not in present
    ):
        present = [
            item
            for item in present
            if item
            not in {"最新", "精选短剧", "推荐", "国产", "乱伦", "动漫", "漫画"}
        ]
    return tuple(present)


def home_top_strip_uses_visible_layout(
    frame_blob: str,
    visible: Sequence[str] | None = None,
) -> bool:
    """当前帧是否已离开「最新起七格」，必须按可见栏序贴峰。"""

    text = str(frame_blob or "")
    if any(marker in text for marker in _OFF_CURRICULUM_MARKERS):
        return True
    labels = tuple(visible or ())
    if labels and "最新" not in labels:
        return True
    return False


@dataclass(frozen=True, slots=True)
class PixelHomeTopCategoryTab:
    """同帧像素检出的上方分类槽位。"""

    label: str
    bounds: NormalizedBounds
    center_x: float
    score: float
    selected: bool = False
    slot_index: int = 0


def calibrate_home_top_category_candidates(
    *,
    analysis: AppVisualAnalysis,
    screenshot_png: bytes,
    ordered_clickable: Sequence[tuple[str, int]] | None = None,
    slot_count: int | None = None,
    max_candidates: int | None = None,
) -> AppVisualAnalysis:
    """用上方文字栏像素槽位校正／补齐上方分类候选。

    不插值臆造：只有当前 PNG 上方带可辨时才签发；标签按课纲槽位下标映射。
    """

    if not isinstance(analysis, AppVisualAnalysis):
        raise TypeError("analysis 必须为 AppVisualAnalysis")
    if not isinstance(screenshot_png, bytes) or not screenshot_png:
        raise ValueError("screenshot_png 必须为非空 bytes")
    page_type = str(analysis.visual_page_type or "").strip().casefold()
    if page_type and page_type != "home":
        return analysis
    frame_blob = " ".join(
        (
            str(analysis.visual_summary or ""),
            str((analysis.surface_facts or {}).get("ocr_text") or "")
            if isinstance(analysis.surface_facts, Mapping)
            else "",
        )
    )
    clickable = _normalize_clickable(ordered_clickable)
    visible = visible_home_top_strip_labels(frame_blob)
    # 横滑到日本AV／OnlyFans 后，默认七格下标已错位（国产→日本AV、漫画→OnlyFans）。
    strip_scrolled = home_top_strip_uses_visible_layout(frame_blob, visible)
    if strip_scrolled and not visible:
        return analysis
    slots = int(slot_count or (len(visible) if strip_scrolled and visible else _DEFAULT_SLOT_COUNT))
    detected = detect_home_top_category_tabs(
        screenshot_png,
        expected_width=analysis.width,
        expected_height=analysis.height,
        ordered_clickable=clickable,
        slot_count=slots,
        layout_labels=visible if strip_scrolled and visible else None,
    )
    if not detected:
        return analysis

    surface_facts = dict(analysis.surface_facts)
    selected_labels = tuple(tab.label for tab in detected if tab.selected)
    clickable_slots = {int(tab.slot_index) for tab in detected}
    selected_slot = getattr(
        detect_home_top_category_tabs, "last_selected_slot_index", None
    )
    selected_center_x = getattr(
        detect_home_top_category_tabs, "last_selected_center_x", None
    )
    selected_is_skip = (
        isinstance(selected_slot, int)
        and selected_slot >= 0
        and selected_slot not in clickable_slots
    )
    surface_facts["pixel_home_top_category_calibration"] = {
        "contract_id": PIXEL_HOME_TOP_CATEGORY_CONTRACT,
        "tab_count": len(detected),
        "tabs": [
            {
                "label": tab.label,
                "bounds": tab.bounds.to_dict(),
                "center_x": round(tab.center_x, 6),
                "score": round(tab.score, 6),
                "selected": tab.selected,
                "slot_index": tab.slot_index,
            }
            for tab in detected
        ],
        "source_capture_id": analysis.capture_id,
        "screenshot_artifact_id": analysis.screenshot_artifact_id,
        "selected_slot_index": selected_slot if isinstance(selected_slot, int) else None,
        "selected_center_x": (
            round(float(selected_center_x), 6)
            if isinstance(selected_center_x, (int, float))
            else None
        ),
        "selected_is_skip": selected_is_skip,
    }
    if selected_labels:
        surface_facts["pixel_home_top_selected_labels"] = list(selected_labels)
    if selected_is_skip:
        surface_facts["home_top_selected_is_skip"] = True
        surface_facts["home_top_selected_slot_index"] = selected_slot
        if isinstance(selected_center_x, (int, float)):
            surface_facts["home_top_selected_center_x"] = float(selected_center_x)


    by_label = {tab.label: tab for tab in detected}
    rewritten: list[AppVisualCandidate] = []
    matched_labels: set[str] = set()
    seen_ids: set[str] = set()
    for candidate in analysis.candidates:
        label = _match_candidate_label(candidate, by_label)
        if label is None:
            if candidate.candidate_id not in seen_ids:
                rewritten.append(candidate)
                seen_ids.add(candidate.candidate_id)
            continue
        if label in matched_labels:
            continue
        tab = by_label[label]
        matched_labels.add(label)
        built = _build_pixel_tab_candidate(
            analysis=analysis,
            tab=tab,
            semantic_name=f"上方分类「{tab.label}」",
            purpose=(
                f"点击主页上方分类「{tab.label}」并截图留证；{_PIXEL_PURPOSE_MARKER}"
            ),
            expected_state_hint=f"home_top_tab:{tab.label}",
            confidence=max(float(candidate.confidence), 0.96),
        )
        if built.candidate_id in seen_ids:
            continue
        rewritten.append(built)
        seen_ids.add(built.candidate_id)

    for tab in detected:
        if tab.label in matched_labels:
            continue
        built = _build_pixel_tab_candidate(
            analysis=analysis,
            tab=tab,
            semantic_name=f"上方分类「{tab.label}」",
            purpose=(
                f"点击主页上方分类「{tab.label}」并截图留证；{_PIXEL_PURPOSE_MARKER}"
            ),
            expected_state_hint=f"home_top_tab:{tab.label}",
            confidence=0.95,
        )
        if built.candidate_id in seen_ids:
            continue
        rewritten.append(built)
        seen_ids.add(built.candidate_id)
        matched_labels.add(tab.label)

    if max_candidates is not None and max_candidates > 0:
        pixel_first = [
            item
            for item in rewritten
            if _PIXEL_PURPOSE_MARKER in str(item.purpose or "")
        ]
        others = [
            item
            for item in rewritten
            if _PIXEL_PURPOSE_MARKER not in str(item.purpose or "")
        ]
        rewritten = [*pixel_first, *others][:max_candidates]

    key_elements = tuple(
        dict.fromkeys((*analysis.key_elements, "pixel:home_top_category"))
    )
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=key_elements,
        candidates=tuple(rewritten),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=surface_facts,
    )


def _glyph_slot_centers(image_bgr, *, slot_count: int) -> tuple[float, ...] | None:
    """从文字栏亮字团估槽位中心。团数必须等于 slot_count，否则交回等宽槽。"""

    import cv2
    import numpy as np

    height, width = image_bgr.shape[:2]
    y0 = int(round(_GLYPH_TOP * height))
    y1 = int(round(_GLYPH_BOTTOM * height))
    if y1 - y0 < 8 or width < 32 or slot_count < 3:
        return None
    glyph = cv2.cvtColor(image_bgr[y0:y1, :], cv2.COLOR_BGR2GRAY)
    threshold = float(np.percentile(glyph, 78))
    if threshold < 12.0:
        return None
    mask = glyph > max(threshold, 22.0)
    column = mask.mean(axis=0)
    kernel = max(5, width // 160)
    if kernel % 2 == 0:
        kernel += 1
    weights = np.ones(kernel, dtype=np.float32) / float(kernel)
    column = np.convolve(column, weights, mode="same")
    min_run = max(6, width // 180)
    gap = max(12, width // 90)
    runs: list[list[int]] = []
    in_run = False
    start = 0
    for index, value in enumerate(column):
        if value > 0.08 and not in_run:
            in_run = True
            start = index
        elif value <= 0.08 and in_run:
            in_run = False
            if index - start >= min_run:
                runs.append([start, index])
    if in_run and width - start >= min_run:
        runs.append([start, width - 1])
    merged: list[list[int]] = []
    for left, right in runs:
        if merged and left - merged[-1][1] < gap:
            merged[-1][1] = right
        else:
            merged.append([left, right])
    if len(merged) != slot_count:
        return None
    return tuple((left + right) / 2.0 / float(width) for left, right in merged)


def _slot_luminance_scores(image_bgr, centers: Sequence[float]) -> list[float]:
    """用文字行亮度打分；选中栏明显更亮，空带饱和度会被空白稀释。"""

    import cv2

    height, width = image_bgr.shape[:2]
    y0 = int(round(0.120 * height))
    y1 = int(round(0.146 * height))
    if y1 - y0 < 4:
        return [0.0 for _ in centers]
    gray = cv2.cvtColor(image_bgr[y0:y1, :], cv2.COLOR_BGR2GRAY)
    scores: list[float] = []
    for center_x in centers:
        left = max(0, int((float(center_x) - 0.02) * width))
        right = min(width, int((float(center_x) + 0.02) * width))
        crop = gray[:, left:right]
        scores.append(float(crop.mean()) if crop.size else 0.0)
    return scores


def _selected_slot_from_luminance(scores: Sequence[float]) -> int:
    """选中槽须明显亮于次高；分不出则不标选中，避免乱伦误记课纲已访。"""

    if not scores:
        return -1
    best = max(range(len(scores)), key=lambda index: scores[index])
    ordered = sorted(scores, reverse=True)
    second = ordered[1] if len(ordered) > 1 else 0.0
    if scores[best] >= 28.0 and scores[best] >= second + 6.0:
        return best
    return -1


def nudge_home_top_bounds_off_skip(
    bounds: Mapping[str, float],
    *,
    selected_slot_index: int | None = None,
    selected_center_x: float | None = None,
) -> tuple[dict[str, float], str]:
    """停在 skip 槽时相对选中态左右纠偏。

    旧逻辑一律右移：停在乱伦时尚可，但横滑后停在日本AV 再右移会越过漫画。
    选中偏右（日本AV）→ 左移一格落到漫画／动漫；选中偏左（乱伦）→ 右移一格。
    """

    try:
        out = {
            "left": float(bounds["left"]),
            "top": float(bounds["top"]),
            "right": float(bounds["right"]),
            "bottom": float(bounds["bottom"]),
        }
    except (KeyError, TypeError, ValueError):
        return dict(bounds) if isinstance(bounds, dict) else {}, ""

    width = max(0.02, out["right"] - out["left"])
    base_cx = selected_center_x
    if base_cx is None and selected_slot_index is not None and selected_slot_index >= 0:
        # 与七格铺满右缘的等宽中心对齐，仅作无像素中心时的兜底。
        base_cx = 0.015 + (0.995 - 0.03) / 7.0 * (selected_slot_index + 0.5)
    if base_cx is None:
        base_cx = (out["left"] + out["right"]) / 2.0

    # 必须用真实选中中心：横滑后日本AV 常被等宽槽误标成 slot=3（乱伦位），
    # 若只看 slot 会继续右移越过漫画。中心 >=0.45 一律当右侧 skip → 左移一格。
    go_left = float(base_cx) >= 0.45
    direction = -1 if go_left else 1
    step = 0.12
    cx = max(0.05, min(0.93, float(base_cx) + direction * step))
    half = max(0.02, min(0.03, width / 2.0))
    out["left"] = cx - half
    out["right"] = cx + half
    tag = "；skip 槽左移纠偏" if direction < 0 else "；skip 槽右移纠偏"
    return out, tag


def detect_home_top_category_tabs(
    screenshot_png: bytes,
    *,
    expected_width: int,
    expected_height: int,
    ordered_clickable: Sequence[tuple[str, int]] | None = None,
    slot_count: int = _DEFAULT_SLOT_COUNT,
    layout_labels: Sequence[str] | None = None,
) -> tuple[PixelHomeTopCategoryTab, ...]:
    """从 PNG 上方文字栏检出槽位——对齐底栏：同帧从左到右找峰再映射标签。

    不再用「课纲 slot 几何 + skip 左右挪点」；可点栏取完整栏序中对应峰的中心。
    """

    if not isinstance(screenshot_png, bytes) or not screenshot_png:
        return ()
    if expected_width <= 0 or expected_height <= 0:
        return ()
    try:
        import cv2
        import numpy as np
    except ImportError:
        return ()

    encoded = np.frombuffer(screenshot_png, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        return ()
    height, width = image.shape[:2]
    if width != expected_width or height != expected_height:
        return ()

    clickable = _normalize_clickable(ordered_clickable)
    clickable_by_slot = {slot: label for label, slot in clickable}
    layout = tuple(str(item).strip() for item in (layout_labels or ()) if str(item).strip())
    if not layout:
        layout = _DEFAULT_LAYOUT_LABELS
    slots = max(3, int(slot_count or len(layout) or _DEFAULT_SLOT_COUNT))
    y0 = int(round(_BAND_TOP * height))
    y1 = int(round(_BAND_BOTTOM * height))
    if y1 - y0 < 8:
        return ()
    band = image[y0:y1, :]
    if band.size == 0:
        return ()
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 35, 110)
    if float(edges.mean()) < 1.2 and not _band_looks_like_text_strip(band):
        return ()

    # 与底栏相同：边缘能量峰 → 不足再用等宽槽。
    col_energy = edges.mean(axis=0).astype(float)
    kernel = max(5, width // 40)
    if kernel % 2 == 0:
        kernel += 1
    smooth = cv2.GaussianBlur(col_energy.reshape(1, -1), (kernel, 1), 0).ravel()
    threshold = float(smooth.mean() + 0.35 * (smooth.max() - smooth.mean()))
    peaks_px = _find_top_band_energy_peaks(
        smooth, threshold=threshold, min_distance=max(8, width // 16)
    )
    if len(peaks_px) < 3:
        glyph = _glyph_slot_centers(image, slot_count=slots)
        if glyph is not None:
            peaks_px = [int(round(cx * width)) for cx in glyph]
        else:
            pad = _HORIZONTAL_PAD
            span = max(0.2, _USABLE_RIGHT - 2.0 * pad)
            slot_w = span / float(slots)
            peaks_px = [
                int(round((pad + slot_w * (index + 0.5)) * width))
                for index in range(slots)
            ]
    elif abs(len(peaks_px) - slots) > 1:
        peaks_px = [
            int(round((index + 0.5) * width * _USABLE_RIGHT / slots))
            for index in range(slots)
        ]
    else:
        # 峰数接近课纲：补齐／裁成 slot_count，保证与完整栏序对齐。
        if len(peaks_px) != slots:
            peaks_px = [
                int(round((index + 0.5) * width * _USABLE_RIGHT / slots))
                for index in range(slots)
            ]
        peaks_px = peaks_px[:slots]

    centers = [float(px) / float(width) for px in peaks_px]
    scores = _slot_luminance_scores(image, centers)
    selected_index = _selected_slot_from_luminance(scores)
    detect_home_top_category_tabs.last_selected_slot_index = (  # type: ignore[attr-defined]
        selected_index if selected_index >= 0 else None
    )
    detect_home_top_category_tabs.last_selected_center_x = (  # type: ignore[attr-defined]
        float(centers[selected_index]) if selected_index >= 0 else None
    )

    half = 0.028
    top_n = _BAND_TOP + 0.004
    bottom_n = min(_BAND_BOTTOM - 0.004, 0.17)
    if bottom_n - top_n < 0.028:
        bottom_n = top_n + 0.03

    # 完整栏序或当前可见窗从左到右贴峰；只签发课纲可点栏（乱伦／日本AV 不进入可点列表）。
    # 横滑后的可见栏序不得再用默认 slot_index 把日本AV 当成国产。
    using_default_layout = layout == _DEFAULT_LAYOUT_LABELS
    tabs: list[PixelHomeTopCategoryTab] = []
    for slot_index, center_x in enumerate(centers):
        if slot_index >= len(layout):
            break
        layout_label = layout[slot_index]
        if layout_label in _CURRICULUM_CLICKABLE_LABELS:
            label = layout_label
        elif using_default_layout:
            label = clickable_by_slot.get(slot_index)
        else:
            continue
        if label is None or label not in _CURRICULUM_CLICKABLE_LABELS:
            continue
        slot_half = half
        if slot_index > 0:
            slot_half = min(slot_half, (center_x - centers[slot_index - 1]) * 0.32)
        if slot_index + 1 < len(centers):
            slot_half = min(slot_half, (centers[slot_index + 1] - center_x) * 0.32)
        slot_half = max(0.012, slot_half)
        left = max(0.0, center_x - slot_half)
        right = min(_USABLE_RIGHT, center_x + slot_half)
        if label == "漫画":
            # 右邻日本AV：只点漫画左半，禁止点缝。
            if slot_index + 1 < len(centers):
                right = min(right, (center_x + centers[slot_index + 1]) / 2.0 - 0.02)
            right = min(right, center_x)
            left = max(0.0, center_x - slot_half * 1.25)
            if right <= left:
                continue
        tabs.append(
            PixelHomeTopCategoryTab(
                label=label,
                bounds=NormalizedBounds(left, top_n, right, bottom_n),
                center_x=center_x,
                score=float(scores[slot_index]) if slot_index < len(scores) else 0.0,
                selected=slot_index == selected_index,
                slot_index=slot_index,
            )
        )
    return tuple(tabs)


def _find_top_band_energy_peaks(
    smooth,
    *,
    threshold: float,
    min_distance: int,
) -> list[int]:
    """上方文字栏边缘能量峰（与底栏 _find_energy_peaks 同思路）。"""

    peaks: list[int] = []
    last = -min_distance
    values = smooth
    for index in range(1, len(values) - 1):
        if float(values[index]) < threshold:
            continue
        if not (
            float(values[index]) >= float(values[index - 1])
            and float(values[index]) >= float(values[index + 1])
        ):
            continue
        if index - last < min_distance:
            if peaks and float(values[index]) > float(values[peaks[-1]]):
                peaks[-1] = index
                last = index
            continue
        peaks.append(index)
        last = index
    return peaks


def extract_pixel_home_top_category_tabs(
    analysis: AppVisualAnalysis | Mapping[str, object] | None,
) -> tuple[PixelHomeTopCategoryTab, ...]:
    """从 Analysis.surface_facts 读取已签发的像素上方分类。"""

    if analysis is None:
        return ()
    if isinstance(analysis, AppVisualAnalysis):
        facts = analysis.surface_facts
    elif isinstance(analysis, Mapping):
        facts = analysis
    elif hasattr(analysis, "surface_facts"):
        facts = getattr(analysis, "surface_facts", None)
    else:
        return ()
    payload = (
        facts.get("pixel_home_top_category_calibration")
        if isinstance(facts, Mapping)
        else None
    )
    if not isinstance(payload, Mapping):
        # 部分路径把校准挂在 surface_facts 外层副本。
        if isinstance(analysis, Mapping):
            nested = analysis.get("surface_facts")
            if isinstance(nested, Mapping):
                payload = nested.get("pixel_home_top_category_calibration")
        if not isinstance(payload, Mapping):
            return ()
    raw_tabs = payload.get("tabs")
    if not isinstance(raw_tabs, (list, tuple)):
        return ()
    result: list[PixelHomeTopCategoryTab] = []
    for item in raw_tabs:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip()
        raw_bounds = item.get("bounds")
        if not label or not isinstance(raw_bounds, Mapping):
            continue
        try:
            bounds = NormalizedBounds(
                float(raw_bounds["left"]),
                float(raw_bounds["top"]),
                float(raw_bounds["right"]),
                float(raw_bounds["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        center_x = float(item.get("center_x") or ((bounds.left + bounds.right) / 2.0))
        score = float(item.get("score") or 0.0)
        selected = item.get("selected") is True
        try:
            slot_index = int(item.get("slot_index") or 0)
        except (TypeError, ValueError):
            slot_index = 0
        result.append(
            PixelHomeTopCategoryTab(
                label=label,
                bounds=bounds,
                center_x=center_x,
                score=score,
                selected=selected,
                slot_index=slot_index,
            )
        )
    return tuple(result)


def is_pixel_calibrated_home_top_category_candidate(
    candidate: AppVisualCandidate,
) -> bool:
    """是否为像素上方分类校准签发的候选。"""

    return _PIXEL_PURPOSE_MARKER in str(candidate.purpose or "")


def _normalize_clickable(
    ordered_clickable: Sequence[tuple[str, int]] | None,
) -> tuple[tuple[str, int], ...]:
    if not ordered_clickable:
        return _DEFAULT_CLICKABLE
    result: list[tuple[str, int]] = []
    for item in ordered_clickable:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        label = str(item[0]).strip()
        try:
            slot_index = int(item[1])
        except (TypeError, ValueError):
            continue
        if label:
            result.append((label, slot_index))
    return tuple(result) if result else _DEFAULT_CLICKABLE


def _band_looks_like_text_strip(band) -> bool:
    """上方带是否像横向文字分类（非纯色／非大图）。"""

    import cv2

    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 30, 100)
    mean_edge = float(edges.mean())
    # 文字栏通常有中等边缘密度；纯色条接近 0，大图广告往往更高且块状。
    return 1.0 <= mean_edge <= 45.0


def _match_candidate_label(
    candidate: AppVisualCandidate,
    by_label: Mapping[str, PixelHomeTopCategoryTab],
) -> str | None:
    if candidate.action_type is not InteractionOperation.CLICK:
        return None
    if candidate.normalized_bounds is not None:
        cy = (
            float(candidate.normalized_bounds.top)
            + float(candidate.normalized_bounds.bottom)
        ) / 2.0
        if cy < _BAND_TOP - 0.02 or cy > _BAND_BOTTOM + 0.03:
            return None
    text = " ".join(
        (
            str(candidate.semantic_name or ""),
            str(candidate.purpose or ""),
            str(candidate.expected_state_hint or ""),
        )
    )
    for label in by_label:
        if label and label in text:
            return label
    return None


def _build_pixel_tab_candidate(
    *,
    analysis: AppVisualAnalysis,
    tab: PixelHomeTopCategoryTab,
    semantic_name: str,
    purpose: str,
    expected_state_hint: str,
    confidence: float,
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=tab.bounds,
        confidence=confidence,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        expected_state_hint=expected_state_hint,
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
