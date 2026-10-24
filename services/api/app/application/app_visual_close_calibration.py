"""用当前截图的像素结构校准广告弹层关闭候选。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
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

_CLOSE_MARKERS = ("关闭", "close", "dismiss", "×")
# 兼容旧文案：is_pixel_calibrated_close_candidate 仍匹配该子串
_PIXEL_CALIBRATION_PURPOSE_MARKER = "当前截图唯一孤立 X 像素结构校准"
_PIXEL_ISSUED_PURPOSE = (
    "点击当前截图首选孤立关闭控件并关闭广告弹层；"
    f"{_PIXEL_CALIBRATION_PURPOSE_MARKER}"
)
# 多层弹层常检出多个 X；分数差过近仍签发 top-1（仅弱分才 fail-closed）
_CROSS_SCORE_MARGIN = 0.08
# 分数接近时，首选仍须达到该分才签发，避免乱点装饰性交叉
_CROSS_AMBIGUOUS_MIN_SCORE = 0.52
# 精准落点：相对原 bounds 收缩到该比例，避免点到框边或 X 中空
_PRECISE_TAP_SHRINK = 0.42
_AD_BODY_MARKERS = (
    "广告主体",
    "广告本体",
    "点击广告",
    "打开广告",
    "外链",
    "下载",
    "安装",
    "outbound",
    "ad body",
    "ad_body",
)


def _mapping_normalized_bounds(raw: object) -> NormalizedBounds | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        left = float(raw.get("left"))
        top = float(raw.get("top"))
        right = float(raw.get("right"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return NormalizedBounds(left=left, top=top, right=right, bottom=bottom)


def resolve_popup_panel_bounds(analysis: AppVisualAnalysis) -> NormalizedBounds | None:
    """解析当前最上层弹层／广告面板的归一化框（相对屏幕）。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    for key in (
        "topmost_ad_popup",
        "popup_bounds",
        "dialog_bounds",
        "overlay_bounds",
        "modal_bounds",
    ):
        raw = facts.get(key)
        if isinstance(raw, Mapping):
            bounds = _mapping_normalized_bounds(
                raw.get("normalized_bounds") if "normalized_bounds" in raw else raw
            )
            if bounds is not None:
                area = (bounds.right - bounds.left) * (bounds.bottom - bounds.top)
                if area >= 0.08:
                    return bounds
    # 回落：最大的非关闭候选（常是广告主体）
    best: NormalizedBounds | None = None
    best_area = 0.0
    for candidate in analysis.candidates or ():
        if candidate.normalized_bounds is None:
            continue
        if candidate.control_role in {"dismiss", "skip"}:
            continue
        if _is_low_risk_close_candidate(candidate):
            continue
        bounds = candidate.normalized_bounds
        area = (bounds.right - bounds.left) * (bounds.bottom - bounds.top)
        if area < 0.12 or area <= best_area:
            continue
        # 主体偏中部，排除全屏底栏
        cy = (bounds.top + bounds.bottom) / 2.0
        if cy < 0.18 or cy > 0.88:
            continue
        best = bounds
        best_area = area
    return best


def classify_detected_close_zone(
    selected_bounds: NormalizedBounds,
    *,
    analysis: AppVisualAnalysis,
) -> tuple[bool, str]:
    """按「相对弹层面板」验收检出的关闭点；无面板时才弱回落屏幕分区。

    返回 (是否可点, zone_id)。zone_id 用于 close_kind／审计，不是固定 Y 阈值。
    """

    sel_cx = (selected_bounds.left + selected_bounds.right) / 2.0
    sel_cy = (selected_bounds.top + selected_bounds.bottom) / 2.0
    panel = resolve_popup_panel_bounds(analysis)
    if panel is not None:
        pw = panel.right - panel.left
        ph = panel.bottom - panel.top
        if pw > 0.05 and ph > 0.05:
            pad = 0.03
            inside = (
                panel.left - pad <= sel_cx <= panel.right + pad
                and panel.top - pad <= sel_cy <= panel.bottom + pad
            )
            rel_x = (sel_cx - panel.left) / pw
            rel_y = (sel_cy - panel.top) / ph
            # 面板内右上角 X
            if inside and rel_x >= 0.55 and rel_y <= 0.38:
                return True, "panel_top_right"
            # 面板内左上角（少数产品）
            if inside and rel_x <= 0.45 and rel_y <= 0.38:
                return True, "panel_top_left"
            # 面板下沿内侧附着圆 X
            if inside and rel_y >= 0.72 and 0.15 <= rel_x <= 0.85:
                return True, "panel_bottom_edge"
            # 紧贴面板下方（海角网格／商城大图常见）
            below = (
                panel.bottom - 0.02 <= sel_cy <= min(0.96, panel.bottom + 0.24)
                and abs(sel_cx - (panel.left + panel.right) / 2.0)
                <= max(0.32, pw * 0.65)
            )
            if below:
                return True, "panel_below"
            # 面板内容区中部：禁止点穿
            if inside and 0.22 < rel_y < 0.72 and 0.18 < rel_x < 0.82:
                return False, "panel_content_reject"
    # 无可靠面板：弱屏幕分区兜底（仍跟检出点，不是写死某 Y 去点）
    if (
        sel_cy <= 0.28
        and selected_bounds.top <= 0.30
        and (sel_cx <= 0.28 or sel_cx >= 0.72)
    ):
        return True, "screen_top_corner"
    if (
        sel_cy <= 0.20
        and selected_bounds.top <= 0.18
        and sel_cx >= 0.60
        and (selected_bounds.right - selected_bounds.left)
        >= (selected_bounds.bottom - selected_bounds.top) * 1.2
    ):
        return True, "screen_top_right_label"
    if (
        0.52 <= sel_cy <= 0.90
        and selected_bounds.top >= 0.48
        and selected_bounds.bottom <= 0.96
        and 0.22 <= sel_cx <= 0.96
    ):
        return True, "screen_bottom_banner"
    if 0.28 < sel_cy < 0.52:
        return False, "screen_mid_reject"
    return False, "screen_unmatched"


# 对外别名：校准入口使用
_classify_detected_close_zone = classify_detected_close_zone


def calibrate_popup_close_candidates(
    *,
    analysis: AppVisualAnalysis,
    screenshot_png: bytes,
) -> AppVisualAnalysis:
    """用首选孤立 X／圆形关闭钮校正或签发关闭坐标；分数过近时保持原视觉事实。"""

    if not isinstance(analysis, AppVisualAnalysis):
        raise TypeError("analysis 必须为 AppVisualAnalysis")
    if not isinstance(screenshot_png, bytes) or not screenshot_png:
        raise ValueError("screenshot_png 必须为非空 bytes")

    # 付费／抢购文案层：禁止中部假 X；面板相对安全位（右上／下沿／紧贴下方）
    # 的真圆 X 仍签发——「立即抢购」广告常有下方关闭钮，不能整层禁像素。
    payment_surface = is_payment_or_paywall_surface(analysis)

    close_indexes = tuple(
        index
        for index, candidate in enumerate(analysis.candidates)
        if _is_low_risk_close_candidate(candidate)
    )

    cross_bounds = detect_isolated_cross_controls(
        screenshot_png,
        expected_width=analysis.width,
        expected_height=analysis.height,
    )
    # 仅大面积广告主体／外链框才保护，避免 MEDIUM 大框误杀真 X
    protected_candidates = tuple(
        candidate
        for candidate in analysis.candidates
        if candidate.normalized_bounds is not None
        and _is_protective_ad_body_candidate(candidate)
    )
    eligible_crosses = tuple(
        bounds
        for bounds in cross_bounds
        if not any(
            _cross_is_protected_by_ad(
                bounds,
                ad_candidate=protected,
                close_candidates=tuple(analysis.candidates[index] for index in close_indexes),
            )
            for protected in protected_candidates
        )
    )
    selected_bounds, ambiguity_count = _select_top_cross_close(
        eligible_crosses,
        analysis=analysis,
    )
    if selected_bounds is None:
        return analysis
    # 落点跟像素检出的 X 走，禁止写死屏幕 Y。
    # 有弹层面板时：相对面板（右上／下沿／紧贴下方）；无面板时才弱回落屏幕分区。
    zone_ok, close_zone = _classify_detected_close_zone(
        selected_bounds,
        analysis=analysis,
    )
    if not zone_ok:
        return analysis
    if payment_surface and close_zone not in {
        "panel_top_right",
        "panel_top_left",
        "panel_below",
        "panel_bottom_edge",
        "screen_top_corner",
        "screen_top_right_label",
        "screen_bottom_banner",
    }:
        return analysis
    top_corner_ok = close_zone == "screen_top_corner"
    top_right_label_ok = close_zone == "screen_top_right_label"

    # 在检出框内把落点收到墨线／圆钮／文案钮有效区，避免点 X 中空或描边外。
    calibrated_bounds, tap_refined = _refine_precise_tap_bounds(
        selected_bounds,
        screenshot_png=screenshot_png,
        expected_width=analysis.width,
        expected_height=analysis.height,
    )

    # 首选孤立 X／关闭钮即视为可关闭遮挡；Vision 常把遮罩首页误标 home 导致旧逻辑跳过校准。
    popup_surface = _is_popup_surface(analysis)

    startup_surface = str(analysis.page_state or "").casefold() in {
        "startup_ad",
        "countdown",
        "splash",
    } or str(analysis.visual_page_type or "").casefold() in {
        "startup_ad",
        "countdown",
        "splash",
    }
    # 右上「关闭」文案优先标跳过／关闭开屏；下方圆 X 标关弹层。
    # close_kind 跟相对面板／屏幕分区走，不跟固定绝对 Y。
    if top_right_label_ok or (startup_surface and top_corner_ok):
        semantic_name = "关闭开屏广告"
        expected_state_hint = "tap_skip"
        close_kind = "top_right_close_label"
    elif startup_surface:
        semantic_name = "跳过启动广告"
        expected_state_hint = "tap_skip"
        close_kind = "startup_skip_or_close"
    else:
        semantic_name = "关闭广告弹窗"
        expected_state_hint = "close_popup_x"
        if close_zone in {
            "panel_below",
            "panel_bottom_edge",
            "screen_bottom_banner",
        }:
            close_kind = "bottom_circular_x"
        elif close_zone in {"panel_top_right", "panel_top_left"}:
            close_kind = "panel_corner_x"
        else:
            close_kind = "corner_x"

    if close_indexes:
        # 优先修正与检出控件相交的关闭候选，不能借另一个关闭控件的语义改写它。
        matching_indexes = tuple(
            index
            for index in close_indexes
            if _bounds_overlap(selected_bounds, analysis.candidates[index].normalized_bounds)
        )
        selected_index = max(
            matching_indexes or close_indexes,
            key=lambda index: analysis.candidates[index].confidence,
        )
        selected = analysis.candidates[selected_index]
        calibrated = AppVisualCandidate.build(
            source_capture_id=selected.source_capture_id,
            source_image_signature=selected.source_image_signature,
            source_width=selected.source_width,
            source_height=selected.source_height,
            source_orientation=selected.source_orientation,
            semantic_name=semantic_name,
            action_type=selected.action_type,
            normalized_bounds=calibrated_bounds,
            confidence=selected.confidence,
            risk_level=selected.risk_level,
            purpose=f"{selected.purpose}；{_PIXEL_CALIBRATION_PURPOSE_MARKER}",
            control_role=(
                selected.control_role
                or ("skip" if expected_state_hint == "tap_skip" else "dismiss")
            ),
            expected_state_hint=expected_state_hint,
            frontier_scope=selected.frontier_scope,
            discovery_source=selected.discovery_source,
        )
        candidates = list(analysis.candidates)
        candidates[selected_index] = calibrated
        key_extra = "pixel:isolated_cross_close"
        fact_payload = {
            "source_candidate_id": selected.candidate_id,
            "calibrated_candidate_id": calibrated.candidate_id,
            "normalized_bounds": calibrated_bounds.to_dict(),
            "detected_bounds": selected_bounds.to_dict(),
            "mode": "rebind",
            "close_kind": close_kind,
            "close_zone": close_zone,
            "vision_popup_surface": popup_surface,
            "ambiguity_count": ambiguity_count,
            "precise_tap_refined": tap_refined,
        }
    else:
        calibrated = AppVisualCandidate.build(
            source_capture_id=analysis.capture_id,
            source_image_signature=analysis.image_signature,
            source_width=analysis.width,
            source_height=analysis.height,
            source_orientation=analysis.orientation,
            semantic_name=semantic_name,
            action_type=InteractionOperation.CLICK,
            normalized_bounds=calibrated_bounds,
            confidence=0.94,
            risk_level=InteractionRiskLevel.LOW,
            purpose=_PIXEL_ISSUED_PURPOSE,
            control_role="dismiss" if expected_state_hint == "close_popup_x" else "skip",
            expected_state_hint=expected_state_hint,
            frontier_scope="local",
            discovery_source=DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
        )
        candidates = [calibrated, *analysis.candidates]
        key_extra = "pixel:issued_isolated_cross_close"
        fact_payload = {
            "calibrated_candidate_id": calibrated.candidate_id,
            "normalized_bounds": calibrated_bounds.to_dict(),
            "detected_bounds": selected_bounds.to_dict(),
            "mode": "issue",
            "close_kind": close_kind,
            "close_zone": close_zone,
            "vision_popup_surface": popup_surface,
            "ambiguity_count": ambiguity_count,
            "precise_tap_refined": tap_refined,
        }

    surface_facts = dict(analysis.surface_facts)
    surface_facts["pixel_cross_close_calibration"] = fact_payload
    surface_facts["dismiss_control_visible"] = True
    surface_facts["dismiss_control_enabled"] = True
    surface_facts["dismiss_semantic_target"] = semantic_name
    surface_facts["popup_visible"] = True
    page_state = analysis.page_state
    if not popup_surface and not startup_surface:
        page_state = "popup"
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=tuple((*analysis.key_elements, key_extra)),
        candidates=tuple(candidates),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=page_state,
        overlay_signature=analysis.overlay_signature or "pixel:isolated_cross_close",
        surface_facts=surface_facts,
    )


def detect_isolated_cross_controls(
    screenshot_png: bytes,
    *,
    expected_width: int,
    expected_height: int,
) -> tuple[NormalizedBounds, ...]:
    """检测录屏约定的视觉关闭控件：白笔 X、白底黑 X 圆钮、右上「关闭」暗色胶囊。"""

    if not isinstance(screenshot_png, bytes) or not screenshot_png:
        return ()
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (expected_width, expected_height)
    ):
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

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    white_crosses = _detect_white_stroke_crosses(
        gray=gray,
        hsv=hsv,
        width=width,
        height=height,
    )
    circular_closes = _detect_circular_close_buttons(
        gray=gray,
        hsv=hsv,
        width=width,
        height=height,
    )
    outlined_closes = _detect_outlined_white_cross_buttons(
        gray=gray,
        hsv=hsv,
        width=width,
        height=height,
    )
    top_right_closes = _detect_top_right_close_pills(
        gray=gray,
        hsv=hsv,
        width=width,
        height=height,
    )
    return _dedupe_normalized_bounds(
        (*white_crosses, *circular_closes, *outlined_closes, *top_right_closes)
    )


def _detect_top_right_close_pills(
    *,
    gray,
    hsv,
    width: int,
    height: int,
) -> tuple[NormalizedBounds, ...]:
    """录屏开屏／全屏广告：右上半透明暗色胶囊「关闭」文案钮。"""

    import cv2
    import numpy as np

    # 只扫右上角，避免把中部暗色 UI 当成关闭。
    x0 = int(width * 0.55)
    y1 = int(height * 0.22)
    if x0 >= width - 4 or y1 <= 4:
        return ()
    roi_gray = gray[0:y1, x0:width]
    roi_hsv = hsv[0:y1, x0:width]
    # 半透明黑／深灰胶囊：低亮、低饱和
    dark = (
        (roi_gray <= 110)
        & (roi_hsv[:, :, 1] <= 90)
        & (roi_hsv[:, :, 2] <= 130)
    ).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 3))
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, kernel, iterations=1)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        dark,
        connectivity=8,
    )
    min_w = max(28, round(width * 0.045))
    max_w = max(min_w, round(width * 0.28))
    min_h = max(14, round(height * 0.012))
    max_h = max(min_h, round(height * 0.055))
    matches: list[NormalizedBounds] = []
    for component_index in range(1, component_count):
        left, top, component_width, component_height, area = (
            int(value) for value in stats[component_index]
        )
        if not (
            min_w <= component_width <= max_w
            and min_h <= component_height <= max_h
            and 1.4 <= component_width / max(component_height, 1) <= 4.2
        ):
            continue
        fill_ratio = area / (component_width * component_height)
        if not 0.35 <= fill_ratio <= 0.95:
            continue
        abs_left = x0 + left
        abs_top = top
        # 必须偏右上
        cx = (abs_left + component_width / 2.0) / width
        cy = (abs_top + component_height / 2.0) / height
        if cx < 0.62 or cy > 0.18:
            continue
        patch = roi_gray[top : top + component_height, left : left + component_width]
        mask = (
            labels[top : top + component_height, left : left + component_width]
            == component_index
        )
        if not mask.any():
            continue
        # 「关闭」白字：胶囊内须有足够亮像素
        bright_ratio = float((patch[mask] >= 170).sum()) / float(mask.sum())
        if bright_ratio < 0.04 or bright_ratio > 0.55:
            continue
        dark_mean = float(patch[mask].mean())
        if dark_mean > 95:
            continue
        matches.append(
            NormalizedBounds(
                left=abs_left / width,
                top=abs_top / height,
                right=(abs_left + component_width) / width,
                bottom=(abs_top + component_height) / height,
            )
        )
    return tuple(matches)


def is_pixel_calibrated_close_candidate(candidate: AppVisualCandidate) -> bool:
    """判断候选是否经过当前截图唯一孤立 X 校准。"""

    return _PIXEL_CALIBRATION_PURPOSE_MARKER in candidate.purpose


def _detect_white_stroke_crosses(
    *,
    gray,
    hsv,
    width: int,
    height: int,
) -> tuple[NormalizedBounds, ...]:
    import cv2
    import numpy as np

    white_mask = ((gray >= 190) & (hsv[:, :, 1] <= 70)).astype(np.uint8)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        white_mask,
        connectivity=8,
    )
    minimum_side = max(12, round(min(width, height) * 0.012))
    maximum_side = max(minimum_side, round(min(width, height) * 0.12))
    matches: list[NormalizedBounds] = []
    for component_index in range(1, component_count):
        left, top, component_width, component_height, area = (
            int(value) for value in stats[component_index]
        )
        if not (
            minimum_side <= component_width <= maximum_side
            and minimum_side <= component_height <= maximum_side
            and 0.6 <= component_width / component_height <= 1.67
        ):
            continue
        fill_ratio = area / (component_width * component_height)
        if not 0.12 <= fill_ratio <= 0.55:
            continue

        component = (
            labels[
                top : top + component_height,
                left : left + component_width,
            ]
            == component_index
        )
        y_grid, x_grid = np.mgrid[0:component_height, 0:component_width]
        x_normalized = x_grid / max(component_width - 1, 1)
        y_normalized = y_grid / max(component_height - 1, 1)
        main_diagonal = abs(y_normalized - x_normalized) <= 0.16
        anti_diagonal = abs(y_normalized - (1 - x_normalized)) <= 0.16
        diagonal_ratio = (
            component & (main_diagonal | anti_diagonal)
        ).sum() / area
        minimum_arm_pixels = max(2, round(area * 0.025))
        arm_pixels = (
            (component & main_diagonal & (x_normalized < 0.45) & (y_normalized < 0.45)).sum(),
            (component & main_diagonal & (x_normalized > 0.55) & (y_normalized > 0.55)).sum(),
            (component & anti_diagonal & (x_normalized > 0.55) & (y_normalized < 0.45)).sum(),
            (component & anti_diagonal & (x_normalized < 0.45) & (y_normalized > 0.55)).sum(),
        )
        if diagonal_ratio < 0.88 or min(arm_pixels) < minimum_arm_pixels:
            continue

        # 只在半个笔画框宽的近邻判断孤立；更大的窗口会用背景稀释
        # 「区」等字形的外侧笔画，使正文交叉被误签发为关闭控件。
        padding = max(1, round(max(component_width, component_height) * 0.5))
        outer_left = max(0, left - padding)
        outer_top = max(0, top - padding)
        outer_right = min(width, left + component_width + padding)
        outer_bottom = min(height, top + component_height + padding)
        surrounding_area = max(
            1,
            (outer_right - outer_left) * (outer_bottom - outer_top)
            - component_width * component_height,
        )
        surrounding_white = (
            int(
                white_mask[
                    outer_top:outer_bottom,
                    outer_left:outer_right,
                ].sum()
            )
            - area
        )
        if surrounding_white / surrounding_area > 0.08:
            continue

        matches.append(
            NormalizedBounds(
                left=left / width,
                top=top / height,
                right=(left + component_width) / width,
                bottom=(top + component_height) / height,
            )
        )
    return tuple(matches)


def _detect_circular_close_buttons(
    *,
    gray,
    hsv,
    width: int,
    height: int,
) -> tuple[NormalizedBounds, ...]:
    """检测亮色近圆按钮内部的黑色对角 X（X 不在亮色连通域上）。"""

    import cv2
    import numpy as np

    bright_mask = ((gray >= 140) & (hsv[:, :, 1] <= 120)).astype(np.uint8)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        bright_mask,
        connectivity=8,
    )
    minimum_side = max(12, round(min(width, height) * 0.012))
    # 低分辨率截图／浅灰大圆 X 直径可略超旧 0.14（240 宽上约 37px）。
    maximum_side = max(minimum_side, round(min(width, height) * 0.16))
    matches: list[NormalizedBounds] = []
    for component_index in range(1, component_count):
        left, top, component_width, component_height, area = (
            int(value) for value in stats[component_index]
        )
        if not (
            minimum_side <= component_width <= maximum_side
            and minimum_side <= component_height <= maximum_side
            and 0.75 <= component_width / component_height <= 1.35
        ):
            continue
        fill_ratio = area / (component_width * component_height)
        # 实心白圆；底栏镂空图标等半透明环 fill 偏低，直接剔除。
        if not 0.45 <= fill_ratio <= 0.92:
            continue
        center_y = (top + component_height / 2.0) / height
        # 避开系统手势带；弹层下方圆 X 可达 ~0.82，勿过早剔除。
        if center_y > 0.90:
            continue

        component_mask = (
            labels[
                top : top + component_height,
                left : left + component_width,
            ]
            == component_index
        ).astype(np.uint8)
        bright_mean = float(gray[top : top + component_height, left : left + component_width][
            component_mask.astype(bool)
        ].mean())
        # 宫格／半透明遮罩下的圆 X 常为浅灰（mean≈150–180），不是纯白 220+。
        # 底部中央带放宽亮度；仍靠圆度＋四臂黑 X 防误检。
        center_x = (left + component_width / 2.0) / width
        min_bright = 220.0
        if 0.52 <= center_y <= 0.90 and 0.22 <= center_x <= 0.80:
            min_bright = 145.0
        if bright_mean < min_bright:
            continue
        contours, _ = cv2.findContours(
            component_mask * 255,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        if not contours:
            continue
        contour_area = float(cv2.contourArea(contours[0]))
        perimeter = float(cv2.arcLength(contours[0], True))
        if perimeter <= 0:
            continue
        circularity = 4.0 * np.pi * contour_area / (perimeter * perimeter)
        if circularity < 0.65:
            continue

        # 文字孔洞／素材也可满足圆度与黑色对角线；须证明按钮近邻孤立，
        # 半个按钮宽之外的广告素材不属于控件周边，不能据此否定真实圆 X。
        padding = max(1, round(max(component_width, component_height) * 0.5))
        outer_left = max(0, left - padding)
        outer_top = max(0, top - padding)
        outer_right = min(width, left + component_width + padding)
        outer_bottom = min(height, top + component_height + padding)
        surrounding_area = max(
            1,
            (outer_right - outer_left) * (outer_bottom - outer_top)
            - component_width * component_height,
        )
        surrounding_bright = (
            int(bright_mask[outer_top:outer_bottom, outer_left:outer_right].sum())
            - int(bright_mask[top : top + component_height, left : left + component_width].sum())
        )
        surrounding_ratio = surrounding_bright / surrounding_area
        # 弹层下方圆 X：上方常紧贴广告亮色素材，周边亮像素易略超 0.08；
        # 大图广告下 X 可落在 y≈0.55–0.66，须与签发甜区对齐放宽。
        surrounding_limit = 0.08
        if 0.52 <= center_y <= 0.90 and 0.22 <= center_x <= 0.80:
            surrounding_limit = 0.16
        if surrounding_ratio > surrounding_limit:
            continue

        patch = gray[top : top + component_height, left : left + component_width]
        y_grid, x_grid = np.mgrid[0:component_height, 0:component_width]
        x_normalized = x_grid / max(component_width - 1, 1)
        y_normalized = y_grid / max(component_height - 1, 1)
        # 黑 X 落在亮圆“空洞”上，不能要求仍属于亮色连通域。
        interior = ((x_normalized - 0.5) ** 2 + (y_normalized - 0.5) ** 2) <= 0.48**2
        dark = (patch < 110) & interior
        dark_count = int(dark.sum())
        if dark_count < max(8, round(area * 0.04)):
            continue
        main_diagonal = abs(y_normalized - x_normalized) <= 0.22
        anti_diagonal = abs(y_normalized - (1 - x_normalized)) <= 0.22
        diagonal_ratio = int((dark & (main_diagonal | anti_diagonal)).sum()) / dark_count
        arm_pixels = (
            int((dark & main_diagonal & (x_normalized < 0.35) & (y_normalized < 0.35)).sum()),
            int((dark & main_diagonal & (x_normalized > 0.65) & (y_normalized > 0.65)).sum()),
            int((dark & anti_diagonal & (x_normalized > 0.65) & (y_normalized < 0.35)).sum()),
            int((dark & anti_diagonal & (x_normalized < 0.35) & (y_normalized > 0.65)).sum()),
        )
        # 黑 X 的四臂应分别延伸到圆钮外圈；以外围暗像素作分母，避免
        # 圆心的大片黑瞳／孔洞把最低臂长门槛抬高或反向掩盖形状差异。
        peripheral_dark = dark & ~(
            ((x_normalized - 0.5) ** 2 + (y_normalized - 0.5) ** 2) < 0.18**2
        )
        peripheral_dark_count = int(peripheral_dark.sum())
        if peripheral_dark_count == 0:
            continue
        # 浅灰小圆钮上的细黑 X 四臂常仅 3px；底部中央带略放宽臂长门槛。
        arm_ratio = 0.12
        arm_floor = 3
        if 0.52 <= center_y <= 0.90 and 0.22 <= center_x <= 0.80:
            arm_ratio = 0.07
            arm_floor = 2
        minimum_arm_pixels = max(arm_floor, round(peripheral_dark_count * arm_ratio))
        if diagonal_ratio < 0.72 or min(arm_pixels) < minimum_arm_pixels:
            continue

        matches.append(
            NormalizedBounds(
                left=left / width,
                top=top / height,
                right=(left + component_width) / width,
                bottom=(top + component_height) / height,
            )
        )
    return tuple(matches)


def _detect_outlined_white_cross_buttons(
    *,
    gray,
    hsv,
    width: int,
    height: int,
) -> tuple[NormalizedBounds, ...]:
    """检测闭合白描边圆内的四臂白 X，允许抗锯齿把 X 分成多个连通域。"""

    import cv2
    import numpy as np

    # 保持普通白 X 的阈值；描边圆及四臂结构单独取证，不放宽文字隔离。
    white_mask = ((gray >= 190) & (hsv[:, :, 1] <= 70)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(white_mask, connectivity=8)
    minimum_side = max(12, round(min(width, height) * 0.012))
    maximum_side = max(minimum_side, round(min(width, height) * 0.14))
    matches: list[NormalizedBounds] = []
    for index in range(1, count):
        left, top, side_x, side_y, area = (int(value) for value in stats[index])
        if not (
            minimum_side <= side_x <= maximum_side
            and minimum_side <= side_y <= maximum_side
            and 0.82 <= side_x / side_y <= 1.22
            and 0.035 <= area / (side_x * side_y) <= 0.30
        ):
            continue
        component = (labels[top : top + side_y, left : left + side_x] == index)
        contours, hierarchy = cv2.findContours(
            component.astype(np.uint8) * 255, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
        )
        if hierarchy is None:
            continue
        outer_indexes = [i for i, value in enumerate(hierarchy[0]) if value[3] == -1]
        if len(outer_indexes) != 1:
            continue
        outer_index = outer_indexes[0]
        contour = contours[outer_index]
        contour_area = float(cv2.contourArea(contour))
        perimeter = float(cv2.arcLength(contour, True))
        if perimeter <= 0 or 4.0 * np.pi * contour_area / perimeter**2 < 0.78:
            continue
        holes = [
            float(cv2.contourArea(contours[i]))
            for i, value in enumerate(hierarchy[0])
            if value[3] == outer_index
        ]
        # 必须有闭合空心圆；弧线、括号及带叉正文不能代替按钮描边。
        if not holes or max(holes) < contour_area * 0.60:
            continue
        y_grid, x_grid = np.mgrid[0:side_y, 0:side_x]
        x_normalized = x_grid / max(side_x - 1, 1)
        y_normalized = y_grid / max(side_y - 1, 1)
        interior = (
            (x_normalized - 0.5) ** 2 + (y_normalized - 0.5) ** 2
        ) <= 0.36**2
        # 内部四臂可分属多个白像素连通域，但都必须来自当前截图。
        inner_white = white_mask[top : top + side_y, left : left + side_x].astype(bool) & interior
        inner_count = int(inner_white.sum())
        if not max(8, round(side_x * side_y * 0.015)) <= inner_count <= side_x * side_y * 0.20:
            continue
        main = abs(y_normalized - x_normalized) <= 0.10
        anti = abs(y_normalized - (1 - x_normalized)) <= 0.10
        arms = (
            int((inner_white & main & (x_normalized < 0.43) & (y_normalized < 0.43)).sum()),
            int((inner_white & main & (x_normalized > 0.57) & (y_normalized > 0.57)).sum()),
            int((inner_white & anti & (x_normalized > 0.57) & (y_normalized < 0.43)).sum()),
            int((inner_white & anti & (x_normalized < 0.43) & (y_normalized > 0.57)).sum()),
        )
        if (
            int((inner_white & (main | anti)).sum()) / inner_count < 0.88
            or min(arms) < max(3, round(inner_count * 0.08))
        ):
            continue
        padding = max(1, round(max(side_x, side_y) * 0.5))
        x0, y0 = max(0, left - padding), max(0, top - padding)
        x1, y1 = min(width, left + side_x + padding), min(height, top + side_y + padding)
        surrounding_area = max(1, (x1 - x0) * (y1 - y0) - side_x * side_y)
        surrounding_white = int(white_mask[y0:y1, x0:x1].sum()) - int(
            white_mask[top : top + side_y, left : left + side_x].sum()
        )
        if surrounding_white / surrounding_area > 0.08:
            continue
        matches.append(
            NormalizedBounds(left / width, top / height, (left + side_x) / width, (top + side_y) / height)
        )
    # 新描边形状只能提供唯一当前控件；多圆多叉时不能靠像素选择强行消歧。
    return tuple(matches) if len(matches) == 1 else ()


def _dedupe_normalized_bounds(
    bounds_list: Sequence[NormalizedBounds],
) -> tuple[NormalizedBounds, ...]:
    unique: list[NormalizedBounds] = []
    for bounds in bounds_list:
        if any(_bounds_nearly_same(bounds, existing) for existing in unique):
            continue
        unique.append(bounds)
    return tuple(unique)


def _bounds_nearly_same(
    left: NormalizedBounds,
    right: NormalizedBounds,
    *,
    center_tolerance: float = 0.04,
) -> bool:
    left_center = left.center
    right_center = right.center
    return (
        abs(left_center[0] - right_center[0]) <= center_tolerance
        and abs(left_center[1] - right_center[1]) <= center_tolerance
    )


def is_payment_or_paywall_surface(analysis: AppVisualAnalysis) -> bool:
    """付费页／解锁墙／尊享卡／终身卡促销层：禁止像素假 X／点抢购。"""

    page_type = str(analysis.visual_page_type or "").strip().casefold()
    if page_type == "payment":
        return True
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {
        "unlock_paywall",
        "paywall",
        "purchase",
        "vip_unlock",
        "vip_purchase",
        "payment",
        "membership",
        "lifetime_card",
        "vip_promo",
    }:
        return True
    summary = " ".join(
        (
            str(getattr(analysis, "visual_summary", None) or ""),
            str(facts.get("dialog_title") or ""),
            str(facts.get("popup_title") or ""),
            " ".join(str(item) for item in (getattr(analysis, "key_elements", None) or ()) if item),
        )
    )
    # 强促销文案：终身卡／抢购。禁止用「永久免费／国庆」单独命中——底飘常有会误挡上方栏。
    strong = (
        "尊享卡",
        "至尊终身卡",
        "终身卡",
        "立即抢购",
        "立即去抢购",
        "限时特价",
        "解锁暗网",
    )
    if any(marker in summary for marker in strong):
        return True
    return "全站永久免费" in summary and (
        "抢购" in summary or "搶購" in summary or "终身" in summary or "終身" in summary
    )


def _candidate_dismiss_text_and_bounds(
    candidate: object,
) -> tuple[str, str, object]:
    """兼容 Vision 候选与 Frontier CandidateAction。"""

    metadata = getattr(candidate, "metadata", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    role = str(getattr(candidate, "control_role", "") or metadata.get("control_role") or "").casefold()
    terms = " ".join(
        (
            str(getattr(candidate, "semantic_name", "") or ""),
            str(getattr(candidate, "semantic_target", "") or ""),
            str(getattr(candidate, "purpose", "") or ""),
            str(metadata.get("purpose") or ""),
            str(getattr(candidate, "candidate_id", "") or ""),
        )
    )
    bounds = getattr(candidate, "normalized_bounds", None)
    if bounds is None:
        bounds = metadata.get("normalized_bounds")
    return role, terms, bounds


def _normalized_center_y(bounds: object) -> float | None:
    if bounds is None:
        return None
    center = getattr(bounds, "center", None)
    if isinstance(center, (tuple, list)) and len(center) >= 2:
        try:
            return float(center[1])
        except (TypeError, ValueError):
            return None
    if not isinstance(bounds, Mapping):
        return None
    try:
        if "top" in bounds or "bottom" in bounds:
            top = float(bounds.get("top") or 0.0)
            bottom = float(bounds.get("bottom") or top)
            return (top + bottom) / 2.0
        raw_center = bounds.get("center")
        if isinstance(raw_center, (tuple, list)) and len(raw_center) >= 2:
            return float(raw_center[1])
    except (TypeError, ValueError):
        return None
    return None


def _is_peripheral_dismiss_candidate(candidate: object) -> bool:
    """底飘／贴底横幅 X 关的是另一层，不能当成中部促销卡的关闭点。

    弹层主体正下方的圆 X（常见 y≈0.55–0.82，名称里常带 bottom_banner）
    属于本层关闭点，不得当外围底飘误杀。
    """

    _role, terms, bounds = _candidate_dismiss_text_and_bounds(candidate)
    blob = terms.casefold()
    center_y = _normalized_center_y(bounds)
    bannerish = any(
        marker in blob
        for marker in (
            "bottom_banner",
            "底飘",
            "底部横幅",
            "ticker",
            "ui_tree:bottom_banner",
        )
    )
    if bannerish:
        # 仅贴底条带算外围；面板下方圆 X 仍算本层。
        return center_y is None or center_y >= 0.86
    return center_y is not None and center_y >= 0.88


def is_no_close_auto_dismiss_promo(analysis: AppVisualAnalysis) -> bool:
    """无关闭 X、仅抢购 CTA 的促销层：应点蒙层空白处关闭，禁止点抢购／假 X／提前点上方栏。

    底部横幅／底飘上的 X 不算本层关闭点。
    短剧播放器解锁层（更多精彩解锁即享／金币解锁）不算终身卡促销，走左上角返回。
    """

    if not is_payment_or_paywall_surface(analysis):
        return False
    # 短剧解锁层：有返回 chevron，不是点蒙层空白。
    summary = " ".join(
        (
            str(analysis.visual_summary or ""),
            str(
                (analysis.surface_facts or {}).get("dialog_title")
                if isinstance(analysis.surface_facts, Mapping)
                else ""
            ),
            str(
                (analysis.surface_facts or {}).get("popup_title")
                if isinstance(analysis.surface_facts, Mapping)
                else ""
            ),
            " ".join(str(item) for item in (analysis.key_elements or ()) if item),
        )
    )
    if any(
        marker in summary
        for marker in (
            "更多精彩解锁即享",
            "更多精彩解锁",
            "30金币解锁",
            "开通短剧卡",
            "剩余金币数",
            "观看完整剧集",
        )
    ):
        return False
    # 已有明确 dismiss／skip 关闭点则走关层，不进入「等消失」。
    for candidate in analysis.candidates or ():
        if _is_peripheral_dismiss_candidate(candidate):
            continue
        role, terms, _bounds = _candidate_dismiss_text_and_bounds(candidate)
        if role in {"dismiss", "skip"}:
            return False
        if any(
            marker in terms
            for marker in ("关闭", "關閉", "跳过", "跳過", "close", "dismiss", "skip")
        ):
            # 排除抢购文案误含「关闭」
            if any(bad in terms for bad in ("抢购", "搶購", "购买", "購買", "开通", "開通")):
                continue
            return False
    return True


def _is_popup_surface(analysis: AppVisualAnalysis) -> bool:
    return (
        str(analysis.page_state or "").casefold()
        in {"popup", "modal", "overlay", "dialog", "startup_ad"}
        or analysis.overlay_signature is not None
        or analysis.surface_facts.get("popup_visible") is True
    )


def _is_low_risk_close_candidate(candidate: AppVisualCandidate) -> bool:
    if (
        candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.normalized_bounds is None
    ):
        return False
    if candidate.control_role in {"dismiss", "skip"}:
        return True
    text = f"{candidate.semantic_name} {candidate.purpose}".casefold()
    return any(marker.casefold() in text for marker in _CLOSE_MARKERS)


def _is_protective_ad_body_candidate(candidate: AppVisualCandidate) -> bool:
    """仅大面积广告主体／外链框才作为保护区，避免 MEDIUM 框吞掉真关闭钮。"""

    if candidate.action_type is not InteractionOperation.CLICK:
        return False
    if candidate.risk_level is InteractionRiskLevel.LOW:
        return False
    bounds = candidate.normalized_bounds
    if bounds is None:
        return False
    area = max(0.0, (bounds.right - bounds.left) * (bounds.bottom - bounds.top))
    text = f"{candidate.semantic_name} {candidate.purpose}".casefold()
    ad_marked = any(marker.casefold() in text for marker in _AD_BODY_MARKERS)
    if candidate.risk_level is InteractionRiskLevel.HIGH:
        return ad_marked or area >= 0.05
    # MEDIUM：只有明确广告主体文案，或超大框才保护
    return ad_marked or area >= 0.18


def _bounds_overlap(left: NormalizedBounds, right: NormalizedBounds | None) -> bool:
    """只接受两个当前帧控件框的实际交集，不按历史位置扩大关闭区域。"""

    return right is not None and (
        max(left.left, right.left) < min(left.right, right.right)
        and max(left.top, right.top) < min(left.bottom, right.bottom)
    )


def _cross_is_protected_by_ad(
    bounds: NormalizedBounds,
    *,
    ad_candidate: AppVisualCandidate,
    close_candidates: Sequence[AppVisualCandidate],
) -> bool:
    """粗略广告框不能吞掉双重确认的关闭钮，高风险广告区域仍保持保护。"""

    assert ad_candidate.normalized_bounds is not None
    if not _point_inside(bounds.center, ad_candidate.normalized_bounds):
        return False
    if ad_candidate.risk_level is InteractionRiskLevel.HIGH:
        return True
    # 像素 X 与独立低风险关闭语义同位时，才从 MEDIUM 广告主体粗框中识别关闭控件。
    return not any(
        _bounds_overlap(bounds, candidate.normalized_bounds)
        for candidate in close_candidates
    )


def _select_top_cross_close(
    crosses: Sequence[NormalizedBounds],
    *,
    analysis: AppVisualAnalysis,
) -> tuple[NormalizedBounds | None, int]:
    """按几何打分取首选关闭点；分数接近时仍签发强 top-1，避免整段放弃精准点击。"""

    ambiguity_count = len(crosses)
    if ambiguity_count == 0:
        return None, 0
    scored = sorted(
        (
            (
                _score_cross_close_bounds(bounds, analysis=analysis),
                bounds,
            )
            for bounds in crosses
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_bounds = scored[0]
    if ambiguity_count == 1:
        return best_bounds, 1
    second_score = scored[1][0]
    if best_score - second_score >= _CROSS_SCORE_MARGIN:
        return best_bounds, ambiguity_count
    # 多层／双 X 分数接近：仍点 top-1（通常是弹层底部圆钮），弱分才 fail-closed。
    if best_score >= _CROSS_AMBIGUOUS_MIN_SCORE:
        return best_bounds, ambiguity_count
    return None, ambiguity_count


def _score_cross_close_bounds(
    bounds: NormalizedBounds,
    *,
    analysis: AppVisualAnalysis,
) -> float:
    """居中偏下圆钮优先；弱化「必须绝对水平居中」，压低顶角假阳性。"""

    center_x, center_y = bounds.center
    pixel_width = max((bounds.right - bounds.left) * analysis.width, 1e-6)
    pixel_height = max((bounds.bottom - bounds.top) * analysis.height, 1e-6)
    aspect = pixel_width / pixel_height
    square_score = 1.0 - min(abs(aspect - 1.0), 0.5) * 2.0
    # hjll：下方圆 X 甜区约 0.80；开屏右上「关闭」胶囊另计强分
    if center_y <= 0.20 and center_x >= 0.60 and aspect >= 1.3:
        # 录屏开屏右上「关闭」胶囊：强优先于装饰交叉
        y_score = 1.0
        x_score = 1.0 - min(abs(center_x - 0.82) * 1.2, 0.4)
        square_score = 0.85
    elif center_y > 0.90:
        y_score = 0.05
        x_score = 1.0 - min(abs(center_x - 0.5) * 1.7, 1.0)
    elif 0.52 <= center_y <= 0.88:
        # 大图广告下方圆 X 可落在 0.55–0.66，仍按底带真钮强分。
        y_score = 1.0 - min(abs(center_y - 0.72) * 1.4, 0.35)
        x_score = 1.0 - min(abs(center_x - 0.5) * 1.7, 1.0)
    elif 0.40 <= center_y < 0.52:
        y_score = 0.45
        x_score = 1.0 - min(abs(center_x - 0.5) * 1.7, 1.0)
    else:
        # 顶部／右上角装饰交叉常见，压低以免盖过底部真关闭钮
        y_score = 0.20
        x_score = 1.0 - min(abs(center_x - 0.5) * 1.7, 1.0)
    overlap_bonus = 0.0
    for candidate in analysis.candidates:
        if not _is_low_risk_close_candidate(candidate):
            continue
        assert candidate.normalized_bounds is not None
        if _point_inside((center_x, center_y), candidate.normalized_bounds):
            overlap_bonus = 1.0
            break
    return (
        square_score * 0.22
        + y_score * 0.50
        + x_score * 0.18
        + overlap_bonus * 0.10
    )


def _refine_precise_tap_bounds(
    bounds: NormalizedBounds,
    *,
    screenshot_png: bytes,
    expected_width: int,
    expected_height: int,
) -> tuple[NormalizedBounds, bool]:
    """把点击落到墨线／圆钮有效区中心，避免 X 中空或外框。"""

    try:
        import cv2
        import numpy as np
    except ImportError:
        return _shrink_bounds_toward_center(bounds, factor=_PRECISE_TAP_SHRINK), True

    encoded = np.frombuffer(screenshot_png, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        return _shrink_bounds_toward_center(bounds, factor=_PRECISE_TAP_SHRINK), True
    height, width = image.shape[:2]
    if width != expected_width or height != expected_height:
        return _shrink_bounds_toward_center(bounds, factor=_PRECISE_TAP_SHRINK), True

    left = max(0, int(bounds.left * width))
    top = max(0, int(bounds.top * height))
    right = min(width, max(left + 1, int(math.ceil(bounds.right * width))))
    bottom = min(height, max(top + 1, int(math.ceil(bounds.bottom * height))))
    patch = cv2.cvtColor(image[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
    patch_h, patch_w = patch.shape[:2]
    if patch_h < 3 or patch_w < 3:
        return _shrink_bounds_toward_center(bounds, factor=_PRECISE_TAP_SHRINK), True

    local_cx = (patch_w - 1) / 2.0
    local_cy = (patch_h - 1) / 2.0
    bright_ratio = float((patch >= 180).mean())
    center_val = int(patch[int(round(local_cy)), int(round(local_cx))])
    # 空心圆描边 X（中心暗、环亮）：几何中心落在中空，Android 常点不中；
    # 糖果短剧等广告圆 X 属此类——禁止再向中心收缩。
    hollow_ring = center_val < 150 and bright_ratio >= 0.06

    def _tap_nearest_ink() -> tuple[NormalizedBounds, bool] | None:
        ink = patch >= 185
        if int(ink.sum()) < 3:
            return None
        ys, xs = np.where(ink)
        distances = (xs.astype(np.float64) - local_cx) ** 2 + (
            ys.astype(np.float64) - local_cy
        ) ** 2
        best = int(np.argmin(distances))
        tap_x = left + int(xs[best])
        tap_y = top + int(ys[best])
        half = max(3, round(min(patch_w, patch_h) * 0.18))
        refined = NormalizedBounds(
            left=max(0.0, (tap_x - half) / width),
            top=max(0.0, (tap_y - half) / height),
            right=min(1.0, (tap_x + half + 1) / width),
            bottom=min(1.0, (tap_y + half + 1) / height),
        )
        return refined, True

    if hollow_ring:
        ink_hit = _tap_nearest_ink()
        if ink_hit is not None:
            return ink_hit
        # 找不到亮墨时保留检出框，勿缩到中空。
        return bounds, False

    # 白底圆钮：几何中心通常已在黑 X 交点，收缩即可提高命中。
    if bright_ratio >= 0.35:
        return _shrink_bounds_toward_center(bounds, factor=_PRECISE_TAP_SHRINK), True

    # 白笔画／黑底白 X：几何中心常落在中空，改点最近亮墨像素。
    ink_hit = _tap_nearest_ink()
    if ink_hit is not None:
        return ink_hit
    return _shrink_bounds_toward_center(bounds, factor=_PRECISE_TAP_SHRINK), True


def _shrink_bounds_toward_center(
    bounds: NormalizedBounds,
    *,
    factor: float,
) -> NormalizedBounds:
    """向中心收缩 bounds，使 tap 更贴近控件内部。"""

    center_x, center_y = bounds.center
    half_w = max((bounds.right - bounds.left) * factor / 2.0, 1e-4)
    half_h = max((bounds.bottom - bounds.top) * factor / 2.0, 1e-4)
    return NormalizedBounds(
        left=max(0.0, center_x - half_w),
        top=max(0.0, center_y - half_h),
        right=min(1.0, center_x + half_w),
        bottom=min(1.0, center_y + half_h),
    )


def _point_inside(
    point: Sequence[float],
    bounds: NormalizedBounds,
) -> bool:
    x_value, y_value = point
    return (
        bounds.left <= x_value <= bounds.right
        and bounds.top <= y_value <= bounds.bottom
    )
