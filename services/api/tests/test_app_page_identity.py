from services.api.app.domain.exploration.app_page_identity import (
    APP_BUSINESS_PAGE_IDENTITY_VERSION,
    build_app_business_page_group,
    build_visual_page_name_fact,
)
from services.api.app.domain.exploration.app_visual_page_identity import (
    build_app_visual_page_facts,
)


def _surface_facts(page_name: str) -> dict[str, object]:
    return {
        "visual_page_name": build_visual_page_name_fact(
            page_name=page_name,
            source="top_bar",
            confidence=0.96,
        )
    }


def _anchor(text: str, *, top: float = 0.1, kind: str = "label") -> dict[str, object]:
    return {
        "text": text,
        "kind": kind,
        "confidence": 0.95,
        "normalized_bounds": {
            "left": 0.1,
            "top": top,
            "right": 0.6,
            "bottom": top + 0.08,
        },
    }


def _confirmed_visual_facts(
    *,
    object_id: str,
    capture_id: str,
    image_signature: str = "sha256:" + "a" * 64,
    screenshot_artifact_id: str | None = None,
    package_name: str = "com.example.app",
    activity: str = ".MainActivity",
) -> dict[str, object]:
    return build_app_visual_page_facts(
        identity_observation={
            "page_role": "detail",
            "confidence": 0.93,
            "location": _anchor("作品详情"),
            "function": _anchor("开始阅读", top=0.3),
            "return_semantics": "back",
            "return_control": _anchor("返回箭头", top=0.01, kind="icon"),
            "business_object": [_anchor(object_id, top=0.5, kind="identifier")],
        },
        local_state_observation=None,
        capture_id=capture_id,
        screenshot_artifact_id=screenshot_artifact_id or f"artifact-{capture_id}",
        image_signature=image_signature,
        package_name=package_name,
        activity=activity,
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )


def test_same_page_name_and_type_ignore_transient_navigation_scope() -> None:
    first = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="profile",
        surface_facts=_surface_facts("个人中心"),
        navigation_depth=0,
        page_scope="nav:first",
        capture_id="capture-1",
    )
    restored = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="profile",
        surface_facts=_surface_facts("个人中心"),
        navigation_depth=3,
        page_scope="nav:restored",
        capture_id="capture-2",
    )

    assert first["page_group_key"] == restored["page_group_key"]
    assert first["business_page_identity"] == restored["business_page_identity"]
    assert first["business_page_identity"] == "com.example.app|.mainactivity"
    assert first["page_display_name"] == "个人中心-profile"


def test_different_page_names_are_distinct_even_when_page_type_matches() -> None:
    profile = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="profile",
        surface_facts=_surface_facts("个人中心"),
        navigation_depth=0,
        page_scope="profile",
        capture_id="capture-1",
    )
    invitation = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="profile",
        surface_facts=_surface_facts("分享邀请"),
        navigation_depth=1,
        page_scope="profile-detail",
        capture_id="capture-2",
    )

    assert profile["page_group_key"] != invitation["page_group_key"]
    assert profile["business_page_identity"] == invitation["business_page_identity"]
    assert invitation["page_display_name"] == "分享邀请-profile"


def test_missing_page_name_keeps_legacy_scope_as_collision_guard() -> None:
    first = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="home",
        surface_facts={},
        navigation_depth=0,
        page_scope="nav:first",
        capture_id="capture-1",
    )
    second = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="home",
        surface_facts={},
        navigation_depth=0,
        page_scope="nav:second",
        capture_id="capture-2",
    )

    assert first["page_group_key"] != second["page_group_key"]
    assert first["business_page_identity"] == second["business_page_identity"]
    assert first["page_display_name"] == "未命名页面-home"


def test_business_page_identity_ignores_title_and_navigation_action() -> None:
    titled = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts=_surface_facts("详情 A"),
        navigation_depth=1,
        page_scope="nav:open-a",
        capture_id="capture-1",
        navigation_entry="内容卡片 A",
    )
    renamed = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts=_surface_facts("详情 B"),
        navigation_depth=2,
        page_scope="nav:open-b",
        capture_id="capture-2",
        navigation_entry="内容卡片 B",
    )

    assert titled["business_page_identity"] == "com.example.app|.mainactivity"
    assert titled["business_page_identity"] == renamed["business_page_identity"]
    assert titled["business_page_core_key"] == renamed["business_page_core_key"]


def test_business_page_identity_ignores_page_type_reclassification() -> None:
    """页型改判不得漂覆盖核心身份。"""
    listed = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_list",
        surface_facts=_surface_facts("作品"),
        navigation_depth=0,
        page_scope="list",
        capture_id="capture-1",
    )
    retyped = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="home",
        surface_facts=_surface_facts("作品"),
        navigation_depth=0,
        page_scope="list",
        capture_id="capture-2",
    )

    assert listed["business_page_identity"] == retyped["business_page_identity"]
    assert listed["page_type"] != retyped["page_type"]
    assert APP_BUSINESS_PAGE_IDENTITY_VERSION.endswith(".v6")


def test_same_activity_detail_objects_stay_distinct_with_visual_identity() -> None:
    """同 Activity 两详情对象须靠视觉身份区分，不得因去掉 page_type 而合并。"""
    first_facts = _confirmed_visual_facts(object_id="作品编号 123", capture_id="capture-a")
    second_facts = _confirmed_visual_facts(object_id="作品编号 456", capture_id="capture-b")
    first = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts={**_surface_facts("详情 A"), **first_facts},
        navigation_depth=1,
        page_scope="detail-a",
        capture_id="capture-a",
        image_signature="sha256:" + "a" * 64,
        screenshot_artifact_id="artifact-capture-a",
    )
    second = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts={**_surface_facts("详情 B"), **second_facts},
        navigation_depth=1,
        page_scope="detail-b",
        capture_id="capture-b",
        image_signature="sha256:" + "a" * 64,
        screenshot_artifact_id="artifact-capture-b",
    )
    retyped = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="video_detail",
        surface_facts={**_surface_facts("详情 A"), **first_facts},
        navigation_depth=1,
        page_scope="detail-a",
        capture_id="capture-a",
        image_signature="sha256:" + "a" * 64,
        screenshot_artifact_id="artifact-capture-a",
    )

    assert first["business_page_identity"].startswith(
        "com.example.app|.mainactivity|vid:"
    )
    assert first["business_page_identity"] != second["business_page_identity"]
    assert first["business_page_identity"] == retyped["business_page_identity"]


def test_visual_page_name_fact_normalizes_text_and_confidence() -> None:
    fact = build_visual_page_name_fact(
        page_name="  AI 世界首页｜ ",
        source="top_bar",
        confidence="0.93",
    )

    assert fact == {
        "protocol_version": "app-visual-page-name.v1",
        "page_name": "AI 世界首页",
        "source": "top_bar",
        "confidence": 0.93,
    }


def test_trusted_selected_navigation_name_overrides_conflicting_visual_name() -> None:
    group = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="video_list",
        surface_facts={
            **_surface_facts("二次元"),
            "trusted_accessibility_controls": {
                "pixel_selected_labels": ("视频",),
                "pixel_selected_page_type": "video_list",
                "bottom_navigation": (
                    {"label": "二次元", "selected": False},
                    {"label": "视频", "selected": False},
                ),
            },
        },
        navigation_depth=0,
        page_scope="video",
        capture_id="capture-1",
    )

    assert group["page_display_name"] == "视频-video_list"
    assert group["page_name_source"] == "trusted_accessibility"
    assert group["page_name_confidence"] == 1.0


def test_inner_home_tab_name_beats_trusted_bottom_navigation_root_name() -> None:
    group = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="home",
        surface_facts={
            "visual_page_name": build_visual_page_name_fact(
                page_name="连载更新",
                source="selected_tab",
                confidence=0.96,
            ),
            "trusted_accessibility_controls": {
                "pixel_selected_labels": ("二次元",),
                "pixel_selected_page_type": "home",
                "bottom_navigation": (
                    {"label": "二次元", "selected": True, "page_type": "home"},
                    {"label": "视频", "selected": False, "page_type": "video_list"},
                ),
            },
        },
        navigation_depth=0,
        page_scope="home-serial-update",
        capture_id="capture-serial-update",
    )

    assert group["page_display_name"] == "连载更新-home"
    assert group["page_name_source"] == "selected_tab"
    assert group["page_name_confidence"] == 0.96


def test_trusted_visual_navigation_selection_names_current_page() -> None:
    group = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="video_list",
        surface_facts={
            **_surface_facts("二次元"),
            "trusted_visual_navigation_selection": {
                "contract_id": "app-visual-navigation-selection.v1",
                "selected_page_type": "video_list",
                "selected_label": "视频",
            },
        },
        navigation_depth=0,
        page_scope="video",
        capture_id="capture-1",
    )

    assert group["page_display_name"] == "视频-video_list"
    assert group["page_name_source"] == "trusted_visual_navigation"
    assert group["page_name_confidence"] == 1.0


def test_nested_detail_name_is_not_replaced_by_selected_bottom_navigation() -> None:
    group = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts={
            **_surface_facts("古寺艳鬼录"),
            "trusted_accessibility_controls": {
                "pixel_selected_labels": ("视频",),
                "pixel_selected_page_type": "video_list",
            },
        },
        navigation_depth=1,
        page_scope="video-detail",
        capture_id="capture-1",
    )

    assert group["page_display_name"] == "古寺艳鬼录-content_detail"
    assert group["page_name_source"] == "top_bar"


def test_generic_detail_name_uses_verified_navigation_entry() -> None:
    group = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts=_surface_facts("详情"),
        navigation_depth=1,
        page_scope="post-detail",
        capture_id="capture-1",
        navigation_entry="帖子_玩偶姐姐",
    )

    assert group["page_display_name"] == "玩偶姐姐详情-content_detail"
    assert group["page_name_source"] == "navigation_entry"
    assert group["page_name_confidence"] == 1.0


def test_generic_detail_rejects_advertisement_navigation_entry() -> None:
    group = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts=_surface_facts("详情"),
        navigation_depth=1,
        page_scope="post-detail",
        capture_id="capture-1",
        navigation_entry="顶部Banner广告入口",
    )

    assert group["page_display_name"] == "详情-content_detail"
    assert group["page_name_source"] == "top_bar"


def test_generic_home_aliases_merge_without_collapsing_named_home_sections() -> None:
    home = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="home",
        surface_facts=_surface_facts("首页"),
        navigation_depth=0,
        page_scope="home",
        capture_id="capture-1",
    )
    comic_home = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="home",
        surface_facts=_surface_facts("漫画首页"),
        navigation_depth=1,
        page_scope="home-restored",
        capture_id="capture-2",
    )
    ai_home = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="home",
        surface_facts=_surface_facts("情涩AI"),
        navigation_depth=0,
        page_scope="ai-home",
        capture_id="capture-3",
    )

    assert home["page_group_key"] == comic_home["page_group_key"]
    assert comic_home["page_display_name"] == "首页-home"
    assert ai_home["page_group_key"] != home["page_group_key"]
    assert ai_home["page_display_name"] == "情涩AI-home"
