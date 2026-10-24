"""上方分类像素落点：后段栏不得压进乱伦右侧空隙。"""

from __future__ import annotations

import cv2
import numpy as np

from services.api.app.application.app_visual_home_top_category_calibration import (
    detect_home_top_category_tabs,
)


def _png_with_seven_tabs(*, bright_slot: int) -> tuple[bytes, int, int]:
    """合成 7 格文字栏；亮槽模拟选中态。"""

    width, height = 1440, 800
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = (12, 12, 16)
    centers = (0.07, 0.21, 0.35, 0.49, 0.63, 0.77, 0.94)
    y0, y1 = int(0.122 * height), int(0.142 * height)
    for index, center_x in enumerate(centers):
        half = int(0.018 * width)
        left = int(center_x * width) - half
        right = int(center_x * width) + half
        color = (40, 40, 48)
        if index == bright_slot:
            color = (220, 180, 40)
        image[y0:y1, left:right] = color
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes(), width, height


def test_anime_slot_center_stays_right_of_skip_gap() -> None:
    """与底栏同思路：七峰从左到右映射后，动漫中心须在乱伦右侧。"""

    png, width, height = _png_with_seven_tabs(bright_slot=3)
    tabs = detect_home_top_category_tabs(
        png,
        expected_width=width,
        expected_height=height,
        layout_labels=("最新", "推荐", "国产", "乱伦", "动漫", "漫画", "日本AV"),
        ordered_clickable=(
            ("最新", 0),
            ("推荐", 1),
            ("国产", 2),
            ("动漫", 4),
            ("漫画", 5),
        ),
        slot_count=7,
    )
    by_label = {tab.label: tab for tab in tabs}
    anime = by_label["动漫"]
    assert anime.center_x >= 0.55
    assert by_label["国产"].selected is False
    assert detect_home_top_category_tabs.last_selected_slot_index == 3


def test_featured_short_drama_occupies_slot_but_is_not_clickable() -> None:
    """新增「精选短剧」只占槽，课纲仍只签发最新／国产／动漫／漫画。"""

    png, width, height = _png_with_seven_tabs(bright_slot=1)
    tabs = detect_home_top_category_tabs(
        png,
        expected_width=width,
        expected_height=height,
    )
    by_label = {tab.label: tab for tab in tabs}
    assert set(by_label) == {"最新", "国产", "动漫", "漫画"}
    assert by_label["最新"].slot_index == 0
    assert by_label["国产"].slot_index == 3
    assert by_label["动漫"].slot_index == 5
    assert by_label["漫画"].slot_index == 6
    assert by_label["国产"].center_x > by_label["最新"].center_x
    assert detect_home_top_category_tabs.last_selected_slot_index == 1


def test_visible_strip_drops_unseen_left_tabs_when_japan_av_shown() -> None:
    from services.api.app.application.app_visual_home_top_category_calibration import (
        visible_home_top_strip_labels,
    )

    labels = visible_home_top_strip_labels(
        "乱伦 动漫 漫画 日本AV 欧美 制服 OnlyFans 热门推荐"
    )
    assert labels == ("乱伦", "动漫", "漫画", "日本AV", "欧美", "制服", "OnlyFans")
    assert "推荐" not in labels
    assert "最新" not in labels


def test_scrolled_layout_maps_manga_left_of_japan_av() -> None:
    """横滑后第三格是漫画，不得再按默认第6格去点日本AV。"""

    png, width, height = _png_with_seven_tabs(bright_slot=3)
    tabs = detect_home_top_category_tabs(
        png,
        expected_width=width,
        expected_height=height,
        layout_labels=("乱伦", "动漫", "漫画", "日本AV", "欧美", "制服", "OnlyFans"),
        slot_count=7,
    )
    by_label = {tab.label: tab for tab in tabs}
    assert "日本AV" not in by_label
    assert "国产" not in by_label
    assert "最新" not in by_label
    manga = by_label["漫画"]
    anime = by_label["动漫"]
    assert set(by_label) == {"动漫", "漫画"}
    assert anime.center_x < manga.center_x
    assert manga.bounds.right <= manga.center_x + 1e-6
    assert manga.slot_index == 2
    assert anime.slot_index == 1


def test_japan_av_in_ocr_forces_visible_strip_layout() -> None:
    """有日本AV 且无最新时，必须按可见栏序，禁止默认槽把日本AV 当国产。"""

    from services.api.app.application.app_visual_home_top_category_calibration import (
        home_top_strip_uses_visible_layout,
        visible_home_top_strip_labels,
    )

    blob = "乱伦 动漫 漫画 日本AV 欧美 制服 OnlyFans 热门推荐"
    visible = visible_home_top_strip_labels(blob)
    assert home_top_strip_uses_visible_layout(blob, visible) is True
    assert visible[0] == "乱伦"
    assert "国产" not in visible

    png, width, height = _png_with_seven_tabs(bright_slot=3)
    tabs = detect_home_top_category_tabs(
        png,
        expected_width=width,
        expected_height=height,
        layout_labels=visible,
        slot_count=len(visible),
    )
    by_label = {tab.label: tab for tab in tabs}
    assert "国产" not in by_label
    assert by_label["漫画"].center_x < 0.45


def test_nudge_helpers_still_directional() -> None:
    """保留 nudge 工具函数；MAF 默认不再挪点，仅作回归。"""

    from services.api.app.application.app_visual_home_top_category_calibration import (
        nudge_home_top_bounds_off_skip,
    )

    bounds = {"left": 0.60, "top": 0.109, "right": 0.66, "bottom": 0.17}
    left_out, left_tag = nudge_home_top_bounds_off_skip(
        bounds, selected_center_x=0.50
    )
    right_out, right_tag = nudge_home_top_bounds_off_skip(
        bounds, selected_center_x=0.40
    )
    assert (left_out["left"] + left_out["right"]) / 2.0 < 0.50
    assert "左移" in left_tag
    assert (right_out["left"] + right_out["right"]) / 2.0 > 0.40
    assert "右移" in right_tag
