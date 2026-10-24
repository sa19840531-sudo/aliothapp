"""App UI Tree 业务控件的通用动态合同回归。"""

from __future__ import annotations

from services.api.app.application.app_surface_evidence import (
    APP_ACCESSIBILITY_CONTRACT_ID,
    app_player_progress_observation,
    find_trusted_post_publish_choice_target,
    find_trusted_post_publish_entry_target,
    inspect_trusted_app_accessibility_controls,
    project_media_state_from_ui_snapshot,
)


def test_dynamic_accessibility_controls_support_unknown_package_and_catalog_aliases() -> None:
    xml = """<hierarchy>
      <node package="com.unknown.target" content-desc="首页" selected="true"
            clickable="true" enabled="true" bounds="[0,1600][360,1920]"/>
      <node package="com.unknown.target" content-desc="首页"
            clickable="true" enabled="true" bounds="[0,1760][360,1920]"/>
      <node package="com.unknown.target" content-desc="社区"
            clickable="true" enabled="true" bounds="[360,1760][720,1920]"/>
      <node package="com.unknown.target" content-desc="我的"
            clickable="true" enabled="true" bounds="[720,1760][1080,1920]"/>
      <node package="com.other" content-desc="外部"
            clickable="true" enabled="true" bounds="[0,1760][360,1920]"/>
    </hierarchy>"""

    controls = inspect_trusted_app_accessibility_controls(
        xml,
        package_name="com.unknown.target",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    assert controls["contract_id"] == APP_ACCESSIBILITY_CONTRACT_ID
    assert controls["package_name"] == "com.unknown.target"
    assert [item["label"] for item in controls["bottom_navigation"]] == [
        "首页",
        "社区",
        "我的",
    ]
    assert controls["page_type"] == "home"
    assert controls["selected_page_type"] == "home"
    assert all(item["action_allowed"] is True for item in controls["bottom_navigation"])
    assert all(
        item["package_name"] == "com.unknown.target" for item in controls["bottom_navigation"]
    )


def test_dynamic_controls_fail_closed_for_unknown_selected_label_and_risky_nav() -> None:
    xml = """<hierarchy>
      <node package="com.unknown.target" content-desc="未知入口" selected="true"
            clickable="true" enabled="true" bounds="[0,1700][270,1920]"/>
      <node package="com.unknown.target" content-desc="未知入口"
            clickable="true" enabled="true" bounds="[0,1760][360,1920]"/>
      <node package="com.unknown.target" content-desc="充值"
            clickable="true" enabled="true" bounds="[360,1760][720,1920]"/>
      <node package="com.unknown.target" content-desc="设置"
            clickable="true" enabled="true" bounds="[720,1760][1080,1920]"/>
    </hierarchy>"""

    controls = inspect_trusted_app_accessibility_controls(
        xml,
        package_name="com.unknown.target",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    assert controls["page_type"] is None
    nav = controls["bottom_navigation"]
    assert len(nav) == 3
    assert nav[1]["page_type"] == "payment"
    assert nav[1]["action_allowed"] is False
    assert nav[1]["risk_level"] == "high"


def test_bika_named_tabs_keep_distinct_names_and_map_to_content_list() -> None:
    xml = """<hierarchy>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="二次元"
            clickable="true" enabled="true" bounds="[30,1743][200,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="视频"
            clickable="true" enabled="true" bounds="[200,1743][370,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="萝莉岛" selected="true"
            clickable="true" enabled="true" bounds="[370,1743][540,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="情涩AI"
            clickable="true" enabled="true" bounds="[540,1743][710,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="朋友圈"
            clickable="true" enabled="true" bounds="[710,1743][880,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="我的"
            clickable="true" enabled="true" bounds="[880,1743][1050,1890]"/>
    </hierarchy>"""

    controls = inspect_trusted_app_accessibility_controls(
        xml,
        package_name="com.flutter324.cblmh.dq04ke",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    nav = controls["bottom_navigation"]
    assert [(item["label"], item["page_type"]) for item in nav] == [
        ("二次元", "home"),
        ("视频", "video_list"),
        ("萝莉岛", "content_list"),
        ("情涩AI", "content_list"),
        # 导航归属使用共享目录的唯一映射；post_list 仅保留同词检测信号。
        ("朋友圈", "content_list"),
        ("我的", "profile"),
    ]
    assert controls["selected_page_type"] == "content_list"


def test_post_publish_entry_uses_bottom_navigation_as_vertical_anchor() -> None:
    xml = """<hierarchy>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="二次元"
            clickable="true" enabled="true" bounds="[30,1743][200,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="视频"
            clickable="true" enabled="true" bounds="[200,1743][370,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="萝莉岛"
            clickable="true" enabled="true" bounds="[370,1743][540,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="情涩AI"
            clickable="true" enabled="true" bounds="[540,1743][710,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="朋友圈" selected="true"
            clickable="true" enabled="true" bounds="[710,1743][880,1890]"/>
      <node package="com.flutter324.cblmh.dq04ke" content-desc="我的"
            clickable="true" enabled="true" bounds="[880,1743][1050,1890]"/>
    </hierarchy>"""

    target = find_trusted_post_publish_entry_target(
        xml,
        package_name="com.flutter324.cblmh.dq04ke",
        semantic_name="发布加号",
        expected_page_type="post_publish",
        normalized_bounds={"left": 0.84, "top": 0.74, "right": 0.96, "bottom": 0.82},
        screen_width=1080,
        screen_height=1920,
    )

    assert target is not None
    bounds = target["bounds"]
    assert bounds["left"] == 907
    assert bounds["right"] == 1037
    assert bounds["top"] == 1581
    assert bounds["bottom"] == 1713
    assert bounds["bottom"] < 1743


def test_post_publish_choice_falls_back_to_bottom_navigation_vertical_anchor() -> None:
    """主内容语义树滞后时，发帖类型按钮仍可使用同帧底栏相对校准。"""

    xml = """<hierarchy>
      <node package="com.example.target" content-desc="旧页面主体"
            clickable="true" enabled="true" bounds="[0,0][1080,1743]"/>
      <node package="com.example.target" content-desc="首页"
            clickable="true" enabled="true" bounds="[0,1743][180,1890]"/>
      <node package="com.example.target" content-desc="视频"
            clickable="true" enabled="true" bounds="[180,1743][360,1890]"/>
      <node package="com.example.target" content-desc="发现"
            clickable="true" enabled="true" bounds="[360,1743][540,1890]"/>
      <node package="com.example.target" content-desc="福利"
            clickable="true" enabled="true" bounds="[540,1743][720,1890]"/>
      <node package="com.example.target" content-desc="朋友圈" selected="true"
            clickable="true" enabled="true" bounds="[720,1743][900,1890]"/>
      <node package="com.example.target" content-desc="我的"
            clickable="true" enabled="true" bounds="[900,1743][1080,1890]"/>
    </hierarchy>"""

    target = find_trusted_post_publish_choice_target(
        xml,
        package_name="com.example.target",
        semantic_name="图文帖子",
        expected_page_type="post_publish",
        normalized_bounds={"left": 0.22, "top": 0.78, "right": 0.48, "bottom": 0.85},
        screen_width=1080,
        screen_height=1920,
    )

    assert target is not None
    assert target["bounds"] == {
        "left": 238,
        "top": 1581,
        "right": 518,
        "bottom": 1713,
        "width": 280,
        "height": 132,
        "signature": "238,1581,518,1713",
    }


def test_post_publish_entry_rejects_final_publish_action() -> None:
    assert (
        find_trusted_post_publish_entry_target(
            "<hierarchy/>",
            package_name="com.example.target",
            semantic_name="确认发布",
            expected_page_type="post_publish",
            normalized_bounds={"left": 0.8, "top": 0.7, "right": 0.95, "bottom": 0.8},
            screen_width=1080,
            screen_height=1920,
        )
        is None
    )


def test_app_player_records_progress_gap_without_inventing_time() -> None:
    missing = app_player_progress_observation(
        media_items=({"kind": "video", "source": "android_accessibility_class"},)
    )
    assert missing["playback_progress_available"] is False
    assert missing["gap_codes"] == ["playback_progress_material_missing"]
    assert missing["continuous_observation"] is False

    observed = app_player_progress_observation(
        media_items=({"current_time": 12.0, "duration": 90.0},),
        timed_samples=({"progress_ratio": 0.2}, {"progress_ratio": 0.4}),
        native_interaction="seek_media",
    )
    assert observed["playback_progress_available"] is True
    assert observed["gap_codes"] == []
    assert observed["continuous_observation"] is True
    assert observed["native_interaction"] == "seek_media"

    projected = project_media_state_from_ui_snapshot({"elements": []})
    assert projected["playback_state_available"] is False
    assert projected["gap_codes"] == ["playback_progress_material_missing"]
