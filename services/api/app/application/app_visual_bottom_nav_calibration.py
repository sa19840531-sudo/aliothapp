"""用当前截图的像素结构校准底栏 Tab 落点（无障碍常空时的视觉命中）。"""

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

PIXEL_BOTTOM_NAV_CONTRACT = "app-visual-bottom-nav-calibration.v1"
_PIXEL_PURPOSE_MARKER = "当前截图视觉像素底栏校准"
# 底栏触控安全带：避开系统手势区（约 top≥0.92）
_BAND_TOP = 0.84
_BAND_BOTTOM = 0.955
_MIN_TABS = 4
_MAX_TABS = 7
_DEFAULT_LABELS = ("主页", "短剧", "暗网", "AI", "社区", "我的")


@dataclass(frozen=True, slots=True)
class PixelBottomNavTab:
    """同帧像素检出的底栏槽位。"""

    label: str
    bounds: NormalizedBounds
    center_x: float
    score: float
    selected: bool = False


def calibrate_bottom_navigation_candidates(
    *,
    analysis: AppVisualAnalysis,
    screenshot_png: bytes,
    ordered_labels: Sequence[str] | None = None,
    max_candidates: int | None = None,
) -> AppVisualAnalysis:
    """用底部带像素槽位校正／补齐 frontier_scope=app 底栏候选。

    不插值臆造坐标：只有当前 PNG 底部带检出成组图标柱时才签发。
    """

    if not isinstance(analysis, AppVisualAnalysis):
        raise TypeError("analysis 必须为 AppVisualAnalysis")
    if not isinstance(screenshot_png, bytes) or not screenshot_png:
        raise ValueError("screenshot_png 必须为非空 bytes")

    labels = _normalize_ordered_labels(ordered_labels)
    detected = detect_bottom_nav_tabs(
        screenshot_png,
        expected_width=analysis.width,
        expected_height=analysis.height,
        ordered_labels=labels,
    )
    if not detected:
        return analysis

    surface_facts = dict(analysis.surface_facts)
    selected_labels = tuple(tab.label for tab in detected if tab.selected)
    surface_facts["pixel_bottom_nav_calibration"] = {
        "contract_id": PIXEL_BOTTOM_NAV_CONTRACT,
        "tab_count": len(detected),
        "tabs": [
            {
                "label": tab.label,
                "bounds": tab.bounds.to_dict(),
                "center_x": round(tab.center_x, 6),
                "score": round(tab.score, 6),
                "selected": tab.selected,
            }
            for tab in detected
        ],
        "source_capture_id": analysis.capture_id,
        "screenshot_artifact_id": analysis.screenshot_artifact_id,
    }
    # 与 a11y／导航选择口径对齐：已选中槽写入 pixel_selected_labels，供到访／跳过主页。
    if selected_labels:
        surface_facts["pixel_selected_labels"] = list(selected_labels)

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
            # 同槽已被签发：丢弃重复 Vision，避免改写后 candidate_id 撞车导致整帧失败。
            continue
        tab = by_label[label]
        matched_labels.add(label)
        # 语义名必须用课纲槽位标签（主页），禁止保留 OCR 误读（如「顶部」），
        # 否则 playbook 匹配／下一栏过滤会把整栏 demote 掉导致主页后停跑。
        built = _build_pixel_tab_candidate(
            analysis=analysis,
            tab=tab,
            semantic_name=f"底栏「{tab.label}」",
            purpose=(
                f"点击底栏「{tab.label}」并截图留证；{_PIXEL_PURPOSE_MARKER}"
            ),
            expected_state_hint=f"tab:{tab.label}",
            confidence=max(float(candidate.confidence), 0.96),
        )
        if built.candidate_id in seen_ids:
            continue
        rewritten.append(built)
        seen_ids.add(built.candidate_id)

    # Vision 漏栏：用像素槽位补齐（仍要求同帧真实检出）。
    for tab in detected:
        if tab.label in matched_labels:
            continue
        built = _build_pixel_tab_candidate(
            analysis=analysis,
            tab=tab,
            semantic_name=f"底栏「{tab.label}」",
            purpose=f"点击底栏「{tab.label}」并截图留证；{_PIXEL_PURPOSE_MARKER}",
            expected_state_hint=f"tab:{tab.label}",
            confidence=0.95,
        )
        if built.candidate_id in seen_ids:
            continue
        rewritten.append(built)
        seen_ids.add(built.candidate_id)
        matched_labels.add(tab.label)

    if max_candidates is not None and max_candidates > 0:
        # 优先保留底栏像素候选，再截断其它。
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
        dict.fromkeys((*analysis.key_elements, "pixel:bottom_navigation"))
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


def detect_bottom_nav_tabs(
    screenshot_png: bytes,
    *,
    expected_width: int,
    expected_height: int,
    ordered_labels: Sequence[str] | None = None,
) -> tuple[PixelBottomNavTab, ...]:
    """从 PNG 底部带检出成组图标柱，并按从左到右映射课纲标签。"""

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

    labels = _normalize_ordered_labels(ordered_labels)
    y0 = int(round(_BAND_TOP * height))
    y1 = int(round(_BAND_BOTTOM * height))
    if y1 - y0 < 12:
        return ()
    band = image[y0:y1, :]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    # 相对底栏背景的高对比柱：图标／文字比纯色条更“碎”。
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 40, 120)
    col_energy = edges.mean(axis=0).astype(float)
    if float(col_energy.max()) < 2.0:
        return ()
    # 平滑后找峰：每个 Tab 对应一个能量峰。
    kernel = max(5, width // 40)
    if kernel % 2 == 0:
        kernel += 1
    smooth = cv2.GaussianBlur(col_energy.reshape(1, -1), (kernel, 1), 0).ravel()
    threshold = float(smooth.mean() + 0.35 * (smooth.max() - smooth.mean()))
    peaks = _find_energy_peaks(smooth, threshold=threshold, min_distance=width // 14)
    expected_count = len(labels)
    if expected_count < _MIN_TABS:
        expected_count = 5
    if len(peaks) < _MIN_TABS:
        # 能量峰不足：若底栏条带整体可辨，退回等宽槽（仍基于同帧条带几何）。
        if not _band_looks_like_nav_strip(band):
            return ()
        peaks = _equal_slot_centers(width, expected_count)
    elif abs(len(peaks) - expected_count) > 1 and _band_looks_like_nav_strip(band):
        # 峰数与课纲差超过 1：优先课纲等宽，避免广告角标当 Tab。
        peaks = _equal_slot_centers(width, expected_count)
    else:
        peaks = peaks[:_MAX_TABS]
        if len(peaks) != expected_count and abs(len(peaks) - expected_count) <= 1:
            # 多／少 1 个时仍用等宽对齐课纲，保证标签映射稳定。
            peaks = _equal_slot_centers(width, expected_count)

    slot_width = 1.0 / max(len(peaks), 1)
    half = min(0.07, slot_width * 0.42)
    top_n = _BAND_TOP + 0.01
    bottom_n = min(_BAND_BOTTOM - 0.005, 0.945)
    if bottom_n - top_n < 0.04:
        bottom_n = top_n + 0.045

    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    scores: list[float] = []
    for center_px in peaks:
        left_px = max(0, int(center_px - half * width))
        right_px = min(width, int(center_px + half * width))
        crop = hsv[:, left_px:right_px]
        if crop.size == 0:
            scores.append(0.0)
            continue
        sat = float(((crop[:, :, 1] >= 55) & (crop[:, :, 2] >= 70)).mean())
        scores.append(sat)

    selected_index = -1
    if scores:
        best = max(range(len(scores)), key=lambda i: scores[i])
        second = sorted(scores, reverse=True)[1] if len(scores) > 1 else 0.0
        if scores[best] >= 0.02 and scores[best] >= second + 0.01:
            selected_index = best

    tabs: list[PixelBottomNavTab] = []
    for index, center_px in enumerate(peaks):
        label = labels[index] if index < len(labels) else f"tab{index + 1}"
        center_x = float(center_px) / float(width)
        left = max(0.0, center_x - half)
        right = min(1.0, center_x + half)
        tabs.append(
            PixelBottomNavTab(
                label=label,
                bounds=NormalizedBounds(left, top_n, right, bottom_n),
                center_x=center_x,
                score=float(scores[index]) if index < len(scores) else 0.0,
                selected=index == selected_index,
            )
        )
    return tuple(tabs)


def extract_pixel_bottom_nav_tabs(
    analysis: AppVisualAnalysis | Mapping[str, object] | None,
) -> tuple[PixelBottomNavTab, ...]:
    """从 Analysis.surface_facts 读取已签发的像素底栏。"""

    if analysis is None:
        return ()
    if isinstance(analysis, AppVisualAnalysis):
        facts = analysis.surface_facts
    elif isinstance(analysis, Mapping):
        facts = analysis
    elif hasattr(analysis, "surface_facts"):
        # 单测／SimpleNamespace 等鸭子类型
        facts = getattr(analysis, "surface_facts", None)
    else:
        return ()
    payload = facts.get("pixel_bottom_nav_calibration") if isinstance(facts, Mapping) else None
    if not isinstance(payload, Mapping):
        return ()
    raw_tabs = payload.get("tabs")
    if not isinstance(raw_tabs, (list, tuple)):
        return ()
    result: list[PixelBottomNavTab] = []
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
        result.append(
            PixelBottomNavTab(
                label=label,
                bounds=bounds,
                center_x=center_x,
                score=score,
                selected=selected,
            )
        )
    return tuple(result)


def is_pixel_calibrated_bottom_nav_candidate(candidate: AppVisualCandidate) -> bool:
    """是否为像素底栏校准签发的候选。"""

    return _PIXEL_PURPOSE_MARKER in str(candidate.purpose or "")


def _normalize_ordered_labels(ordered_labels: Sequence[str] | None) -> tuple[str, ...]:
    if not ordered_labels:
        return _DEFAULT_LABELS
    labels = tuple(str(item).strip() for item in ordered_labels if str(item).strip())
    return labels if labels else _DEFAULT_LABELS


def _match_candidate_label(
    candidate: AppVisualCandidate,
    by_label: Mapping[str, PixelBottomNavTab],
) -> str | None:
    if candidate.action_type is not InteractionOperation.CLICK:
        return None
    if candidate.frontier_scope != "app" and candidate.normalized_bounds is not None:
        top = float(candidate.normalized_bounds.top)
        if top < 0.78:
            return None
    elif candidate.frontier_scope != "app":
        return None
    text = " ".join(
        str(part)
        for part in (
            candidate.semantic_name,
            candidate.purpose,
            candidate.expected_state_hint,
        )
        if part
    )
    for label in by_label:
        if label and label in text:
            return label
    # 无文案时：底部带候选按水平中心吸附最近槽。
    bounds = candidate.normalized_bounds
    if bounds is None or float(bounds.top) < 0.78:
        return None
    center_x = (float(bounds.left) + float(bounds.right)) / 2.0
    nearest = min(by_label.values(), key=lambda tab: abs(tab.center_x - center_x))
    if abs(nearest.center_x - center_x) <= 0.12:
        return nearest.label
    return None


def _build_pixel_tab_candidate(
    *,
    analysis: AppVisualAnalysis,
    tab: PixelBottomNavTab,
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
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _find_energy_peaks(
    smooth: object,
    *,
    threshold: float,
    min_distance: int,
) -> list[int]:
    import numpy as np

    values = np.asarray(smooth, dtype=float)
    peaks: list[int] = []
    last = -min_distance
    for index in range(1, len(values) - 1):
        if values[index] < threshold:
            continue
        if not (values[index] >= values[index - 1] and values[index] >= values[index + 1]):
            continue
        if index - last < min_distance:
            if peaks and values[index] > values[peaks[-1]]:
                peaks[-1] = index
                last = index
            continue
        peaks.append(index)
        last = index
    return peaks


def _equal_slot_centers(width: int, count: int) -> list[int]:
    count = max(_MIN_TABS, min(_MAX_TABS, int(count)))
    return [int(round((index + 0.5) * width / count)) for index in range(count)]


def _band_looks_like_nav_strip(band: object) -> bool:
    """底部条带是否像导航栏（相对统一背景 + 横向能量）。"""

    import cv2
    import numpy as np

    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    # 横向方差低、纵向边缘存在 → 更像独立底栏。
    row_std = float(np.std(gray.mean(axis=1)))
    col_std = float(np.std(gray.mean(axis=0)))
    if col_std < 4.0:
        return False
    return row_std < max(18.0, col_std * 0.85)
