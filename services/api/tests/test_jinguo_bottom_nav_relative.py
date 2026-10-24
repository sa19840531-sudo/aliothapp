"""禁果底栏：语意图标 + 相对六等分槽位。"""

from __future__ import annotations

from services.api.app.application.app_ui_tree_features import (
    bottom_navigation_tab_tap_bounds,
    bottom_navigation_ui_tree_features,
    ordered_bottom_tab_labels,
    resolve_product_orchestrator_id,
    resolve_ui_tree_playbook_id,
)


def test_jinguo_playbook_resolves_from_package() -> None:
    assert resolve_product_orchestrator_id(package_name="dark.NVYCy.AmBPu") == "jinguo"
    assert resolve_ui_tree_playbook_id(package_name="dark.nvycy.ambpu") == "jinguo"


def test_jinguo_ordered_tabs_are_six_semantic_labels() -> None:
    labels = ordered_bottom_tab_labels(package_name="dark.nvycy.ambpu")
    assert labels == ("首页", "剧场", "视频", "社区", "福利", "我的")


def test_jinguo_bottom_nav_policy_is_semantic_icon_relative() -> None:
    bottom = bottom_navigation_ui_tree_features(package_name="dark.nvycy.ambpu")
    assert bottom.get("policy") == "semantic_icon_relative"
    layout = bottom.get("layout") or {}
    assert int(layout.get("slot_count") or 0) == 6
    ordered = bottom.get("ordered_tabs") or ()
    assert len(ordered) == 6
    assert ordered[0]["icon_kind"] == "home_blob"
    assert ordered[5]["icon_kind"] == "person"
    assert ordered[5]["slot_index"] == 5


def test_jinguo_tab_bounds_follow_relative_slots() -> None:
    home = bottom_navigation_tab_tap_bounds("首页", package_name="dark.nvycy.ambpu")
    mine = bottom_navigation_tab_tap_bounds("我的", package_name="dark.nvycy.ambpu")
    theater = bottom_navigation_tab_tap_bounds("剧场", package_name="dark.nvycy.ambpu")
    assert home is not None and mine is not None and theater is not None
    assert home["right"] <= theater["left"] or home["right"] <= 0.2
    assert mine["left"] >= 0.8
    for box in (home, theater, mine):
        assert 0.84 <= box["top"] < box["bottom"] <= 0.96
        cx = (box["left"] + box["right"]) / 2.0
        assert 0.0 < cx < 1.0


def test_jinguo_alias_me_maps_to_rightmost_slot() -> None:
    mine = bottom_navigation_tab_tap_bounds("me", package_name="dark.NVYCy.AmBPu")
    assert mine is not None
    assert (mine["left"] + mine["right"]) / 2.0 >= 0.85


def test_tangguo_ordered_tabs_are_five_semantic_labels() -> None:
    assert resolve_product_orchestrator_id(package_name="com.alz7xm2.drama") == "tangguo"
    labels = ordered_bottom_tab_labels(package_name="com.alz7xm2.drama")
    assert labels == ("首页", "剧库", "投票", "专题", "我的")


def test_tangguo_bottom_nav_relative_slots() -> None:
    bottom = bottom_navigation_ui_tree_features(package_name="com.alz7xm2.drama")
    assert bottom.get("policy") == "semantic_icon_relative"
    assert int((bottom.get("layout") or {}).get("slot_count") or 0) == 5
    home = bottom_navigation_tab_tap_bounds("首页", package_name="com.alz7xm2.drama")
    library = bottom_navigation_tab_tap_bounds("剧库", package_name="com.alz7xm2.drama")
    vote = bottom_navigation_tab_tap_bounds("投票", package_name="com.alz7xm2.drama")
    topic = bottom_navigation_tab_tap_bounds("专题", package_name="com.alz7xm2.drama")
    mine = bottom_navigation_tab_tap_bounds("我的", package_name="com.alz7xm2.drama")
    assert all(box is not None for box in (home, library, vote, topic, mine))
    assert home["right"] <= library["left"] or home["right"] <= 0.25
    assert mine["left"] >= 0.75
