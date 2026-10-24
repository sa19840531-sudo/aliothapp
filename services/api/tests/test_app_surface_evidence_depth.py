"""App §15 证据深度：logcat / network / fault / ui_snapshot / screen_id / 恢复原语。"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from services.api.app.application.app_surface_evidence import (
    APP_ALL_ARTIFACT_TYPES,
    APP_SOFT_OPTIONAL_ARTIFACTS,
    bound_device_log_payload,
    bound_fault_artifact_payload,
    bound_network_trace_payload,
    build_screen_id,
    detect_fault_kind,
    find_trusted_obstruction_dismiss_targets,
    find_unique_trusted_age_confirm_target,
    find_unique_trusted_navigation_target_by_label,
    find_unique_trusted_skip_target,
    infer_hierarchy_screen_size,
    inspect_trusted_app_accessibility_controls,
    project_ui_snapshot_from_hierarchy,
)
from services.api.app.application.capture_profiles import (
    APP_CAPTURE_ARTIFACT_CONTRACT,
    APP_MAIN_CAPTURE_ARTIFACT_TYPES,
    CaptureProfilePolicy,
)
from services.api.app.domain.interaction.capabilities import (
    build_android_screenshot_only_manifest,
)
from services.api.app.domain.interaction.enums import InteractionOperation, SurfaceKind

_SAMPLE_UI_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="首页" class="android.widget.FrameLayout"
        package="com.example.app" resource-id="com.example.app:id/root"
        clickable="true" bounds="[0,0][1080,1920]">
    <node index="0" text="推荐" class="android.widget.TextView"
          package="com.example.app" resource-id="com.example.app:id/tab_home"
          clickable="true" bounds="[100,1700][300,1880]"/>
  </node>
</hierarchy>
"""

_HLW_NAV_XML = """<hierarchy>
  <node package="uk.pnixc.juywgn" content-desc="首页&#10;第 1 个标签，共 30 个"
        clickable="true" enabled="true" selected="true" bounds="[37,199][182,324]"/>
  <node package="uk.pnixc.juywgn" class="android.view.View"
        content-desc="热&#10;一个长度足够的普通新闻内容标题用于回归测试"
        clickable="true" enabled="true" bounds="[0,799][1080,1352]">
    <node package="uk.pnixc.juywgn" class="android.widget.ImageView"
          clickable="false" enabled="true" bounds="[37,799][1043,1180]"/>
  </node>
  <node package="uk.pnixc.juywgn" content-desc="吃瓜" clickable="true"
        enabled="true" bounds="[0,1762][180,1920]"/>
  <node package="uk.pnixc.juywgn" content-desc="AI科技" clickable="true"
        enabled="true" bounds="[180,1762][360,1920]"/>
  <node package="uk.pnixc.juywgn" content-desc="社区" clickable="true"
        enabled="true" bounds="[360,1762][540,1920]"/>
  <node package="uk.pnixc.juywgn" content-desc="视频" clickable="true"
        enabled="true" bounds="[540,1762][720,1920]"/>
  <node package="uk.pnixc.juywgn" content-desc="福利" clickable="true"
        enabled="true" bounds="[720,1762][900,1920]"/>
  <node package="uk.pnixc.juywgn" content-desc="我的" clickable="true"
        enabled="true" bounds="[900,1762][1080,1920]"/>
</hierarchy>"""


def _navigation_accent_png(*, accent_bounds: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGB", (1080, 1920), color=(248, 248, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle(accent_bounds, fill=(255, 74, 170))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

_HLW_WHITE_NAV_XML = """<hierarchy>
  <node package="uk.gvycg.bjdiey" content-desc="首页&#10;第 1 个标签，共 30 个"
        clickable="true" enabled="true" selected="true" bounds="[37,199][182,324]"/>
  <node package="uk.gvycg.bjdiey" class="android.view.View"
        content-desc="热&#10;一个长度足够的白包普通新闻内容标题用于回归测试"
        clickable="true" enabled="true" bounds="[0,799][1080,1352]">
    <node package="uk.gvycg.bjdiey" class="android.widget.ImageView"
          clickable="false" enabled="true" bounds="[37,799][1043,1180]"/>
  </node>
  <node package="uk.gvycg.bjdiey" content-desc="吃瓜" clickable="true"
        enabled="true" bounds="[0,1762][216,1920]"/>
  <node package="uk.gvycg.bjdiey" content-desc="热榜" clickable="true"
        enabled="true" bounds="[216,1762][432,1920]"/>
  <node package="uk.gvycg.bjdiey" content-desc="社区" clickable="true"
        enabled="true" bounds="[432,1762][648,1920]"/>
  <node package="uk.gvycg.bjdiey" content-desc="消息" clickable="true"
        enabled="true" bounds="[648,1762][864,1920]"/>
  <node package="uk.gvycg.bjdiey" content-desc="我的" clickable="true"
        enabled="true" bounds="[864,1762][1080,1920]"/>
</hierarchy>"""

_HLW_OVERLAY_XML = """<hierarchy>
  <node package="uk.pnixc.juywgn" class="android.view.View" clickable="true"
        enabled="true" bounds="[0,0][1080,1920]">
    <node package="uk.pnixc.juywgn" class="android.view.View" clickable="true"
          enabled="true" bounds="[0,0][1080,1920]"/>
    <node NAF="true" package="uk.pnixc.juywgn" class="android.widget.ImageView"
          clickable="true" enabled="true" bounds="[101,391][979,1370]"/>
    <node NAF="true" package="uk.pnixc.juywgn" class="android.widget.ImageView"
          clickable="true" enabled="true" bounds="[490,1428][590,1529]"/>
  </node>
</hierarchy>"""

_HLW_CONTENT_DETAIL_XML = """<hierarchy>
  <node package="uk.pnixc.juywgn" class="android.widget.FrameLayout"
        clickable="false" enabled="true" bounds="[0,0][1080,1920]">
    <node package="uk.pnixc.juywgn" class="android.view.View" text="黑料网"
          clickable="false" enabled="true" bounds="[465,100][615,169]"/>
    <node package="uk.pnixc.juywgn" class="android.widget.ScrollView"
          clickable="false" enabled="true" bounds="[0,199][1080,1751]">
      <node package="uk.pnixc.juywgn" class="android.view.View"
            text="一个长度足够并且已经进入目标包详情页面的文章标题"
            clickable="false" enabled="true" bounds="[37,242][1043,446]"/>
      <node package="uk.pnixc.juywgn" class="android.view.View"
            text="黑料网-作者•2026年08月21日" clickable="false" enabled="true"
            bounds="[37,460][428,505]"/>
    </node>
  </node>
</hierarchy>"""


def test_project_ui_snapshot_and_stable_screen_id() -> None:
    snap = project_ui_snapshot_from_hierarchy(
        _SAMPLE_UI_XML,
        package_name="com.example.app",
        activity=".MainActivity",
    )
    assert snap["source"] == "android_ui_hierarchy"
    assert snap["screen_id"].startswith("screen:")
    assert snap["structure_signature"].startswith("sha256:")
    assert snap["element_count"] >= 1
    assert snap["viewport"] == {"width": 1080, "height": 1920}
    root, tab = snap["elements"]
    assert root["selector"] == "resource-id:com.example.app:id/root"
    assert root["rect"] == {"x": 0, "y": 0, "width": 1080, "height": 1920}
    assert root["class_tokens"] == (
        "android.widget.FrameLayout",
        "android",
        "widget",
        "FrameLayout",
    )
    assert tab["role"] == "text"
    assert tab["name"] == "推荐"
    same = build_screen_id(
        package_name="com.example.app",
        activity=".MainActivity",
        structure_signature=snap["structure_signature"].removeprefix("sha256:"),
    )
    # screen_id 对同一结构签名稳定
    again = project_ui_snapshot_from_hierarchy(
        _SAMPLE_UI_XML,
        package_name="com.example.app",
        activity=".MainActivity",
    )
    assert again["screen_id"] == snap["screen_id"]
    del same


def test_project_ui_snapshot_exposes_generic_media_and_byline_attribution() -> None:
    snapshot = project_ui_snapshot_from_hierarchy(
        """<hierarchy>
          <node package="com.example.app" class="android.widget.ImageView"
                resource-id="com.example.app:id/cover" bounds="[0,0][400,300]"/>
          <node package="com.example.app" class="android.widget.TextView"
                text="示例作者•2026年08月23日" bounds="[0,320][400,380]"/>
        </hierarchy>""",
        package_name="com.example.app",
        activity=".MainActivity",
    )

    cover, byline = snapshot["elements"]
    assert cover["image"] == {
        "kind": "image",
        "source": "android_accessibility_class",
    }
    assert byline["attribution"] == {
        "author_text": "示例作者",
        "published_at_text": "2026年08月23日",
        "source": "android_accessibility_byline",
    }


def test_unique_trusted_skip_target_requires_exact_safe_target_node() -> None:
    xml = """<hierarchy><node package="com.example.app" text="" content-desc="跳过"
        clickable="true" enabled="true" bounds="[860,101][1037,202]"/></hierarchy>"""

    target = find_unique_trusted_skip_target(
        xml,
        package_name="com.example.app",
        screen_width=1080,
        screen_height=1920,
    )

    assert target == {
        "label": "跳过",
        "package_name": "com.example.app",
        "bounds": {
            "left": 860,
            "top": 101,
            "right": 1037,
            "bottom": 202,
            "width": 177,
            "height": 101,
            "signature": "860,101,1037,202",
        },
    }


def test_unique_trusted_startup_exit_accepts_real_top_right_enter_control() -> None:
    xml = """<hierarchy><node package="com.example.app" content-desc="进入"
        clickable="true" enabled="true" bounds="[855,87][1050,177]"/></hierarchy>"""

    target = find_unique_trusted_skip_target(
        xml,
        package_name="com.example.app",
        screen_width=1080,
        screen_height=1920,
    )

    assert target is not None
    assert target["label"] == "进入"
    assert target["bounds"]["signature"] == "855,87,1050,177"


def test_unique_trusted_age_confirm_prefers_enter_rejects_leave() -> None:
    """黑料年龄门：只签发「我年满18岁-进入」，永不签发离开。"""

    xml = """<hierarchy>
      <node package="uk.frtju.nnxsaf" text="我未满18岁-离开" clickable="true"
            enabled="true" bounds="[120,1000][960,1120]"/>
      <node package="uk.frtju.nnxsaf" text="我年满18岁-进入" clickable="true"
            enabled="true" bounds="[120,1160][960,1280]"/>
    </hierarchy>"""

    target = find_unique_trusted_age_confirm_target(
        xml,
        package_name="uk.frtju.nnxsaf",
        screen_width=1080,
        screen_height=1920,
    )

    assert target is not None
    assert target["label"] == "我年满18岁-进入"
    assert target["bounds"]["signature"] == "120,1160,960,1280"


def test_unique_trusted_age_confirm_rejects_upper_half_or_ambiguous() -> None:
    upper = """<hierarchy><node package="com.example.app" text="我年满18岁-进入"
        clickable="true" enabled="true" bounds="[120,200][960,320]"/></hierarchy>"""
    ambiguous = """<hierarchy>
      <node package="com.example.app" text="我年满18岁-进入" clickable="true"
            enabled="true" bounds="[120,1160][960,1280]"/>
      <node package="com.example.app" text="我已满18岁-进入" clickable="true"
            enabled="true" bounds="[120,1320][960,1440]"/>
    </hierarchy>"""

    assert (
        find_unique_trusted_age_confirm_target(
            upper,
            package_name="com.example.app",
            screen_width=1080,
            screen_height=1920,
        )
        is None
    )
    assert (
        find_unique_trusted_age_confirm_target(
            ambiguous,
            package_name="com.example.app",
            screen_width=1080,
            screen_height=1920,
        )
        is None
    )


def test_infer_hierarchy_screen_size_from_root_bounds() -> None:
    xml = """<hierarchy rotation="0">
      <node bounds="[0,0][1440,3040]" package="uerma.zqeluk.cwsxx">
        <node package="uerma.zqeluk.cwsxx" content-desc="进入" clickable="true"
              enabled="true" bounds="[1171,144][1363,240]"/>
      </node>
    </hierarchy>"""

    assert infer_hierarchy_screen_size(xml) == (1440, 3040)


def test_unique_trusted_enter_survives_scaled_capture_when_hierarchy_size_used() -> None:
    """设备 1440 宽、截图预览 720 宽时，必须按 hierarchy 尺寸校验，不能用截图宽。"""

    xml = """<hierarchy rotation="0">
      <node bounds="[0,0][1440,3040]" package="uerma.zqeluk.cwsxx">
        <node package="uerma.zqeluk.cwsxx" content-desc="进入" clickable="true"
              enabled="true" bounds="[1171,144][1363,240]"/>
      </node>
    </hierarchy>"""
    screen_w, screen_h = infer_hierarchy_screen_size(xml) or (0, 0)

    assert find_unique_trusted_skip_target(
        xml,
        package_name="uerma.zqeluk.cwsxx",
        screen_width=720,
        screen_height=1520,
    ) is None
    target = find_unique_trusted_skip_target(
        xml,
        package_name="uerma.zqeluk.cwsxx",
        screen_width=screen_w,
        screen_height=screen_h,
    )
    assert target is not None
    assert target["label"] == "进入"
    assert abs(target["bounds"]["left"] / screen_w - 1171 / 1440) < 1e-9


def test_trusted_obstruction_dismiss_targets_keep_only_exact_safe_labels() -> None:
    xml = """<hierarchy>
      <node package="com.example.app" content-desc="下次再说" clickable="true"
            enabled="true" bounds="[138,1355][528,1472]"/>
      <node package="com.example.app" content-desc="立即升级" clickable="true"
            enabled="true" bounds="[552,1341][942,1485]"/>
      <node package="com.example.app" content-desc="取消支付" clickable="true"
            enabled="true" bounds="[300,1500][700,1650]"/>
    </hierarchy>"""

    targets = find_trusted_obstruction_dismiss_targets(
        xml,
        package_name="com.example.app",
        screen_width=1080,
        screen_height=1920,
    )

    assert [item["label"] for item in targets] == ["下次再说"]


def test_unique_trusted_skip_target_rejects_ambiguous_or_unsafe_nodes() -> None:
    duplicate = """<hierarchy>
      <node package="com.example.app" content-desc="跳过" clickable="true"
            enabled="true" bounds="[800,80][1000,180]"/>
      <node package="com.example.app" text="skip" clickable="true"
            enabled="true" bounds="[800,200][1000,300]"/>
    </hierarchy>"""
    full_screen = """<hierarchy><node package="com.example.app" text="skip"
        clickable="true" enabled="true" bounds="[0,0][1080,1920]"/></hierarchy>"""
    wrong_package = """<hierarchy><node package="com.other" text="skip"
        clickable="true" enabled="true" bounds="[800,80][1000,180]"/></hierarchy>"""

    for xml in (duplicate, full_screen, wrong_package):
        assert (
            find_unique_trusted_skip_target(
                xml,
                package_name="com.example.app",
                screen_width=1080,
                screen_height=1920,
            )
            is None
        )


def test_dynamic_accessibility_controls_require_unique_complete_bottom_row() -> None:
    controls = inspect_trusted_app_accessibility_controls(
        _HLW_NAV_XML,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    assert controls["contract_id"] == "app-accessibility-dynamic.v1"
    assert [item["label"] for item in controls["bottom_navigation"]] == [
        "吃瓜",
        "AI科技",
        "社区",
        "视频",
        "福利",
        "我的",
    ]
    assert controls["page_type"] == "home"
    assert len(controls["content_cards"]) == 1
    assert controls["content_cards"][0]["label"] == "普通内容卡片"
    assert controls["content_cards"][0]["title_sha256"]

    incomplete = _HLW_NAV_XML.replace('content-desc="视频"', 'content-desc="社区"')
    rejected = inspect_trusted_app_accessibility_controls(
        incomplete,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )
    assert rejected is not None
    assert rejected["bottom_navigation"] == ()
    # 底栏结构歧义只阻断该组点击候选；页面内独立的选中「首页」证据仍可判定页型。
    assert rejected["page_type"] == "home"


def test_pixel_selected_bottom_navigation_overrides_stale_flutter_selected_home() -> None:
    controls = inspect_trusted_app_accessibility_controls(
        _HLW_NAV_XML,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
        screenshot_png=_navigation_accent_png(
            accent_bounds=(570, 1800, 690, 1840),
        ),
    )

    assert controls is not None
    assert controls["pixel_selected_page_type"] == "video_list"
    assert controls["pixel_selected_labels"] == ("视频",)
    assert controls["selected_page_type"] == "video_list"
    assert controls["page_type"] == "video_list"


def test_pixel_selected_top_navigation_resolves_content_list() -> None:
    xml = """<hierarchy>
      <node package="com.example.app" content-desc="首页" clickable="true"
            enabled="true" selected="false" bounds="[100,120][300,220]"/>
      <node package="com.example.app" content-desc="小说" clickable="true"
            enabled="true" selected="false" bounds="[300,120][500,220]"/>
      <node package="com.example.app" content-desc="视频" clickable="true"
            enabled="true" selected="false" bounds="[500,120][700,220]"/>
    </hierarchy>"""

    controls = inspect_trusted_app_accessibility_controls(
        xml,
        package_name="com.example.app",
        screen_width=1080,
        screen_height=1920,
        screenshot_png=_navigation_accent_png(
            accent_bounds=(330, 145, 470, 190),
        ),
    )

    assert controls is not None
    assert controls["pixel_selected_page_type"] == "content_list"
    assert controls["pixel_selected_labels"] == ("小说",)
    assert controls["page_type"] == "content_list"


def test_unique_trusted_navigation_alignment_requires_matching_page_type() -> None:
    xml = """<hierarchy>
      <node package="com.example.app" content-desc="动漫" clickable="true"
            enabled="true" bounds="[246,120][389,189]"/>
      <node package="com.example.app" content-desc="小说" clickable="true"
            enabled="true" bounds="[389,120][531,189]"/>
      <node package="com.example.app" content-desc="剧场" clickable="true"
            enabled="true" bounds="[531,120][674,189]"/>
    </hierarchy>"""

    target = find_unique_trusted_navigation_target_by_label(
        xml,
        package_name="com.example.app",
        semantic_name="top_tab_小说",
        expected_page_type="content_list",
        screen_width=1080,
        screen_height=1920,
    )
    mismatched = find_unique_trusted_navigation_target_by_label(
        xml,
        package_name="com.example.app",
        semantic_name="top_tab_小说",
        expected_page_type="video_list",
        screen_width=1080,
        screen_height=1920,
    )

    assert target is not None
    assert target["label"] == "小说"
    assert target["page_type"] == "content_list"
    assert target["bounds"]["signature"] == "389,120,531,189"
    assert mismatched is None


def test_dynamic_accessibility_contract_matches_another_real_bottom_row() -> None:
    controls = inspect_trusted_app_accessibility_controls(
        _HLW_WHITE_NAV_XML,
        package_name="uk.gvycg.bjdiey",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    assert controls["contract_id"] == "app-accessibility-dynamic.v1"
    assert [item["label"] for item in controls["bottom_navigation"]] == [
        "吃瓜",
        "热榜",
        "社区",
        "消息",
        "我的",
    ]
    assert controls["page_type"] == "home"
    assert len(controls["content_cards"]) == 1

    old_package = inspect_trusted_app_accessibility_controls(
        _HLW_WHITE_NAV_XML,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )
    assert old_package is not None
    assert old_package["bottom_navigation"] == ()
    assert old_package["page_type"] is None


def test_dynamic_overlay_close_requires_body_and_full_screen_underlay() -> None:
    controls = inspect_trusted_app_accessibility_controls(
        _HLW_OVERLAY_XML,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    assert controls["overlay_close"] == {
        "label": "关闭弹层",
        "package_name": "uk.pnixc.juywgn",
        "bounds": {
            "left": 490,
            "top": 1428,
            "right": 590,
            "bottom": 1529,
            "width": 100,
            "height": 101,
            "signature": "490,1428,590,1529",
        },
    }
    assert controls["content_cards"] == ()

    without_body = _HLW_OVERLAY_XML.replace("[101,391][979,1370]", "[101,391][200,500]")
    rejected = inspect_trusted_app_accessibility_controls(
        without_body,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )
    assert rejected is not None
    assert rejected["overlay_close"] is None


def test_hlw_trusted_content_card_excludes_advertising_terms() -> None:
    advertising = _HLW_NAV_XML.replace(
        "一个长度足够的普通新闻内容标题用于回归测试",
        "广告推广下载充值入口不能作为普通内容卡片",
    )

    controls = inspect_trusted_app_accessibility_controls(
        advertising,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    assert controls["content_cards"] == ()


def test_hlw_trusted_content_detail_is_read_only_page_type() -> None:
    controls = inspect_trusted_app_accessibility_controls(
        _HLW_CONTENT_DETAIL_XML,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )

    assert controls is not None
    assert controls["page_type"] == "content_detail"
    assert controls["bottom_navigation"] == ()
    assert controls["content_cards"] == ()

    incomplete = _HLW_CONTENT_DETAIL_XML.replace("黑料网-作者•2026年08月21日", "作者")
    rejected = inspect_trusted_app_accessibility_controls(
        incomplete,
        package_name="uk.pnixc.juywgn",
        screen_width=1080,
        screen_height=1920,
    )
    assert rejected is not None
    assert rejected["page_type"] is None


_NAV_SAMPLE_UI_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout"
        package="com.example.app" resource-id="com.example.app:id/root"
        clickable="true" bounds="[0,0][1080,1920]">
    <node index="0" text="推荐" class="android.widget.TextView"
          resource-id="com.example.app:id/title" clickable="false"
          bounds="[40,80][200,140]"/>
    <node index="1" class="com.google.android.material.tabs.TabLayout"
          resource-id="com.example.app:id/top_tabs" clickable="false"
          bounds="[0,140][1080,260]">
      <node index="0" text="热门" class="android.widget.TextView"
            clickable="true" selected="true" bounds="[20,150][200,250]"/>
      <node index="1" text="精选" class="android.widget.TextView"
            clickable="true" selected="false" bounds="[220,150][400,250]"/>
    </node>
    <node index="2" class="com.google.android.material.bottomnavigation.BottomNavigationView"
          resource-id="com.example.app:id/bottom_nav" clickable="false"
          bounds="[0,1800][1080,1920]">
      <node index="0" text="首页" class="android.widget.TextView"
            clickable="true" selected="true" bounds="[40,1820][200,1900]"/>
      <node index="1" text="我的" class="android.widget.TextView"
            clickable="true" selected="false" bounds="[880,1820][1040,1900]"/>
    </node>
  </node>
</hierarchy>
"""


def test_ui_snapshot_marks_tablayout_and_bottom_nav_as_navigation() -> None:
    snapshot = project_ui_snapshot_from_hierarchy(
        _NAV_SAMPLE_UI_XML,
        package_name="com.example.app",
        activity=".MainActivity",
    )
    elements = snapshot["elements"]
    by_text = {
        str(item.get("text")): item
        for item in elements
        if isinstance(item, dict) and item.get("text")
    }
    assert by_text["热门"].get("within_navigation") is True
    assert by_text["热门"].get("role") == "tab"
    assert by_text["首页"].get("within_navigation") is True
    assert by_text["首页"].get("role") == "tab"
    # 内容区标题不在导航容器内
    content_title = next(
        item
        for item in elements
        if item.get("resource_id") == "com.example.app:id/title"
    )
    assert content_title.get("within_navigation") is not True
    containers = [
        item
        for item in elements
        if str(item.get("role") or "") == "tablist"
        or "TabLayout" in str(item.get("class_name") or "")
        or "BottomNavigation" in str(item.get("class_name") or "")
    ]
    assert containers
    assert all(item.get("within_navigation") is True for item in containers)


def test_ui_snapshot_keeps_long_element_text_beyond_160() -> None:
    long_title = "标" * 220
    long_desc = "https://share.example.test/path/" + ("x" * 180)
    xml = f"""<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout"
        package="com.example.app" bounds="[0,0][1080,1920]">
    <node index="0" text="{long_title}" content-desc="{long_desc}"
          class="android.widget.TextView" clickable="false"
          bounds="[40,200][1040,400]"/>
  </node>
</hierarchy>
"""
    snapshot = project_ui_snapshot_from_hierarchy(
        xml,
        package_name="com.example.app",
        activity=".MainActivity",
    )
    element = next(
        item
        for item in snapshot["elements"]
        if isinstance(item, dict) and item.get("content_desc")
    )
    assert element["text"] == long_title
    assert len(element["text"]) > 160
    assert element["content_desc"] == long_desc
    assert long_desc.startswith("https://")


def test_extract_http_urls_prefers_intent_data_from_dumpsys() -> None:
    from services.api.app.application.app_surface_evidence import (
        extract_browser_omnibox_urls_from_hierarchy,
        extract_http_urls_from_android_text,
        resolve_app_timed_capture_params,
        resolve_app_timed_capture_settle_ms,
        resolve_outbound_landing_from_channels,
        should_wake_player_controls_before_media_capture,
    )

    dump = (
        "ACTIVITY com.android.chrome/com.google.android.apps.chrome.Main\n"
        "  Intent { act=android.intent.action.VIEW dat=https://landing.example.test/a?x=1 }\n"
        "  mUrl=https://webview.example.test/page\n"
        "  unrelated https://cdn.example.test/pixel.gif\n"
    )
    urls = extract_http_urls_from_android_text(dump)
    assert urls[0].startswith("https://landing.example.test/a")
    assert any(item.startswith("https://webview.example.test/") for item in urls)
    clip_meta = resolve_outbound_landing_from_channels(
        clipboard_text="https://share.example.test/from-clip",
        dumpsys_text=dump,
    )
    assert clip_meta is not None
    assert clip_meta["source"] == "clipboard"
    assert clip_meta["url"].startswith("https://share.example.test/")
    intent_meta = resolve_outbound_landing_from_channels(dumpsys_text=dump)
    assert intent_meta is not None
    assert intent_meta["source"] == "intent_dat"
    omnibox_xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.EditText"
        resource-id="com.android.chrome:id/url_bar"
        text="https://omnibox.example.test/path" bounds="[0,80][1080,160]"/>
</hierarchy>
"""
    omnibox = extract_browser_omnibox_urls_from_hierarchy(omnibox_xml)
    assert omnibox == ("https://omnibox.example.test/path",)
    bar_meta = resolve_outbound_landing_from_channels(
        clipboard_text="https://share.example.test/from-clip",
        hierarchy_xml=omnibox_xml,
    )
    assert bar_meta is not None
    assert bar_meta["source"] == "browser_address_bar_uia"
    assert should_wake_player_controls_before_media_capture(
        profile_id="app-observe-v1",
        requested_artifact_types={"screenshot", "media_state"},
    )
    assert not should_wake_player_controls_before_media_capture(
        profile_id="app-startup-ad-v1",
        requested_artifact_types={"screenshot", "media_state"},
    )
    weak = resolve_app_timed_capture_params(
        profile_id="app-observe-weak_network_approx-timed_capture-v1",
        requested_artifact_types={"temporal_trace"},
    )
    assert weak == (8, 2000)
    assert resolve_app_timed_capture_settle_ms(
        profile_id="app-observe-weak_network_approx-timed_capture-v1"
    ) == 1500


def test_proxy_weak_network_shape_marks_session_http_proxy_only() -> None:
    from services.api.app.application.app_network_proxy import AppNetworkProxyHub
    from services.api.app.application.app_surface_evidence import (
        build_app_timed_capture_audit,
    )

    hub = AppNetworkProxyHub()
    idle = hub.apply_weak_network_shape(latency_ms=500)
    assert idle["network_throttle_applied"] is False
    assert idle["throttle_kind"] is None
    audit = build_app_timed_capture_audit(
        profile_id="app-observe-weak_network_approx-timed_capture-v1",
        frame_count=8,
        requested_interval_ms=2000,
        produced_offsets_ms=[0.0, 2000.0],
        settle_delay_ms=1500,
        proxy_shape=idle,
    )
    assert audit["network_throttle_applied"] is False
    assert audit["approximation"] == "cold_start_or_delayed_window"
    shaped = {
        "enabled": True,
        "latency_ms": 800,
        "throttle_kind": "session_http_proxy",
        "network_throttle_applied": True,
        "scope": "http_proxy_traffic_only",
    }
    audit2 = build_app_timed_capture_audit(
        profile_id="app-observe-weak_network_approx-timed_capture-v1",
        frame_count=8,
        requested_interval_ms=2000,
        produced_offsets_ms=[0.0, 2000.0],
        proxy_shape=shaped,
    )
    assert audit2["network_throttle_applied"] is True
    assert audit2["throttle_kind"] == "session_http_proxy"
    assert audit2["approximation"] == "session_http_proxy_shape"


def test_device_log_network_fault_payloads_are_bounded() -> None:
    log = bound_device_log_payload(
        raw_text=("line\n" * 5000) + "com.example.app FATAL",
        package_name="com.example.app",
    )
    assert log["status"] == "captured"
    assert log["truncated"] is True
    assert log["checksum"].startswith("sha256:")

    net = bound_network_trace_payload(
        package_name="com.example.app",
        proxy_attached=False,
        raw_summary="connectivity ok",
        status="captured",
    )
    assert net["artifact_kind"] == "network_trace"
    assert net["proxy_attached"] is False

    assert detect_fault_kind("FATAL EXCEPTION: main") == "crash"
    assert detect_fault_kind("ANR in com.example.app") == "anr"
    fault = bound_fault_artifact_payload(
        raw_text="FATAL EXCEPTION: main\ncom.example.app",
        package_name="com.example.app",
        fault_kind="crash",
    )
    assert fault["detected"] is True
    assert fault["fault_kind"] == "crash"


def test_android_manifest_marks_extended_evidence_ready() -> None:
    manifest = build_android_screenshot_only_manifest(
        adapter_id="test",
        adapter_version="1",
        backend="fake",
    )
    assert manifest.actions["deep_link"].status.value == "ready"
    assert manifest.actions["warm_restore"].status.value == "ready"
    assert manifest.artifacts["device_log"].status.value == "ready"
    assert manifest.artifacts["ui_snapshot"].status.value == "ready"
    assert manifest.artifacts["media_state"].status.value == "partial"
    assert manifest.artifacts["fault_artifact"].status.value == "ready"
    assert manifest.artifacts["network_trace"].status.value == "partial"
    assert manifest.observations["identity.screen_id"].status.value == "ready"


def test_app_capture_profile_requests_extended_artifacts() -> None:
    profile = CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID)
    assert "ui_snapshot" in profile.after.artifact_types
    assert "device_log" in profile.after.artifact_types
    assert "network_trace" in profile.after.artifact_types
    assert "fault_artifact" in profile.after.artifact_types
    assert CaptureProfilePolicy.app_allowed_artifact_types() == APP_ALL_ARTIFACT_TYPES
    assert "ui_tree" in APP_SOFT_OPTIONAL_ARTIFACTS
    assert "device_log" in APP_SOFT_OPTIONAL_ARTIFACTS


def test_app_main_capture_contract_separates_required_and_soft_optional() -> None:
    contract = APP_CAPTURE_ARTIFACT_CONTRACT
    assert contract.persistable_artifact_types == APP_MAIN_CAPTURE_ARTIFACT_TYPES
    assert contract.required_for("before_action") == ("screenshot",)
    assert contract.required_for("after_action") == ("screenshot", "action_trace")
    assert set(contract.required_artifact_types).isdisjoint(contract.soft_optional_artifact_types)
    before = contract.select(
        role="before_action",
        requested=("screenshot",),
        produced=("screenshot",),
    )
    assert before.persisted_artifact_types == ("screenshot",)
    selection = contract.select(
        role="after_action",
        requested=APP_MAIN_CAPTURE_ARTIFACT_TYPES,
        produced=("screenshot", "action_trace"),
    )
    assert selection.persisted_artifact_types == ("screenshot", "action_trace")
    assert set(selection.missing_soft_optional_artifact_types) == {
        "ui_tree",
        "view_hierarchy",
        "ui_snapshot",
        "visible_text",
        "media_state",
        "device_log",
        "network_trace",
        "har",
        "temporal_trace",
        "fault_artifact",
    }


def test_interaction_operations_include_restore_primitives() -> None:
    assert InteractionOperation.DEEP_LINK.value == "deep_link"
    assert InteractionOperation.WARM_RESTORE.value == "warm_restore"
    assert InteractionOperation.RESET_APP.value == "reset_app"
