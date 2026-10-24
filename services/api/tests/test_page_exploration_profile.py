"""测试 H5 页面探索剖面的纯 DOM 信号提取。"""

from __future__ import annotations

import pytest

from services.api.app.application.page_exploration_profile import (
    build_h5_exploration_profile,
    h5_profile_risk_flags,
)


def test_h5_profile_distinguishes_line_loading_from_startup_ad() -> None:
    loading_profile = build_h5_exploration_profile(
        '<html><body><div class="loading-page">'
        '<div class="loadingText">正在获取最佳线路</div>'
        "</div></body></html>"
    )
    startup_profile = build_h5_exploration_profile(
        '<html><body><div class="loading-page">'
        '<div class="start-up"><span class="down-time">跳过</span></div>'
        "</div></body></html>"
    )

    assert loading_profile.shell == "line_loading"
    assert loading_profile.obstruction is None
    assert startup_profile.shell == "startup_ad"
    assert startup_profile.obstruction is not None
    assert startup_profile.obstruction.dismiss_selector == ".loading-page .down-time:visible"


def test_h5_profile_recognizes_global_launch_skip_control() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><main class="video-list"></main>'
        '<div class="globalLaunch"><div class="launchSwiperContent">'
        '<div class="header-title"><span>广告</span><span>跳过</span></div>'
        "</div></div></body></html>"
    )

    assert profile.shell == "startup_ad"
    assert profile.obstruction is not None
    assert profile.obstruction.dismiss_selector == (
        '.globalLaunch .header-title > span:text-is("跳过"):visible'
    )


@pytest.mark.parametrize(
    ("dom", "expected_selector"),
    [
        (
            '<div class="wrapper"><div class="countdown">'
            '<span style="display:none">0</span><div class="jump_over">跳过</div>'
            "</div></div>",
            '.countdown > .jump_over:text-is("跳过"):visible',
        ),
        (
            '<div role="dialog" class="van-popup"><div class="skip-wrap">'
            '<div>0s(跳过)</div></div></div>',
            '.van-popup:visible .skip-wrap:has(> div:text-is("0s(跳过)")):visible',
        ),
        (
            '<div role="dialog" class="van-popup"><div class="absolute">'
            '<div class="count-down">跳过</div></div></div>',
            '.van-popup:visible .count-down:text-is("跳过"):visible',
        ),
        (
            '<div class="launch-page"><div class="screen-ad">'
            '<div class="button"><div>进入</div></div></div></div>',
            '.launch-page:visible .screen-ad > .button:has(> div:text-is("进入")):visible',
        ),
    ],
)
def test_h5_profile_recognizes_trusted_startup_ad_exit_variants(
    dom: str,
    expected_selector: str,
) -> None:
    profile = build_h5_exploration_profile(dom)

    assert profile.shell == "startup_ad"
    assert profile.obstruction is not None
    assert profile.obstruction.dismiss_selector == expected_selector


def test_h5_profile_extracts_single_top_obstruction_before_underlying_banner() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="home-suspend">'
        '<div class="btn-close"><i class="van-icon-close"></i></div></div>'
        '<div class="van-overlay"></div>'
        '<div role="dialog" class="van-dialog" style="z-index: 3000">'
        '<div class="van-dialog__content">'
        '<div class="grid-main">广告</div><div class="btn">我知道了</div>'
        "</div></div></body></html>"
    )

    assert profile.shell == "stable"
    assert profile.obstruction is not None
    assert profile.obstruction.dismiss_selector == (
        ':is([role="dialog"], .van-dialog):visible '
        "> .van-dialog__content > .btn:visible > img:visible, "
        ':is([role="dialog"], .van-dialog):visible '
        "> .van-dialog__content > :is(.btn-box.end, .btn-box.close):visible"
    )
    assert profile.obstruction.z_index == 3000


def test_h5_profile_recognizes_leaf_text_dialog_close_control() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="van-overlay"></div>'
        '<div role="dialog" class="van-dialog">'
        '<div class="van-dialog__content"><div class="ad-list"><img></div>'
        '<div class="btn-box end"> 关闭 </div></div></div>'
        "</body></html>"
    )

    assert profile.obstruction is not None
    assert ".btn-box.end" in profile.obstruction.dismiss_selector


def test_h5_profile_extracts_tabbar_content_card_and_high_risk_entries() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="video-list">'
        '<div class="media-item"><div class="title">首个视频</div></div>'
        "</div>"
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">推荐</div>'
        '<div class="van-tabbar-item">视频</div>'
        '<div class="van-tabbar-item">充值</div>'
        "</div>"
        '<button class="pay-btn">立即充值</button>'
        "</body></html>",
        page_type="video_list",
    )

    assert [item.label for item in profile.tabbar_items] == ["推荐", "视频", "充值"]
    assert profile.tabbar_items[0].active is True
    assert profile.tabbar_items[1].selector == (
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
    )
    assert profile.tabbar_items[1].expected_page_type == "video_list"
    assert profile.content_card is not None
    assert profile.content_card.selector == ".video-list .media-item:visible"
    assert profile.content_card.expected_page_type == "video_detail"
    assert [entry.reason_code for entry in profile.risk_entries] == ["PAYMENT_SUBMIT_ACTION"]


def test_h5_profile_extracts_long_video_card() -> None:
    profile = build_h5_exploration_profile(
        '<div class="van-list"><div class="row-four-card">'
        '<div class="item-wrapper"><div class="longVideoCard">视频</div>'
        "</div></div></div>",
        page_type="video_list",
    )

    assert profile.content_card is not None
    assert profile.content_card.selector == ".longVideoCard:visible"
    assert profile.content_card.expected_page_type == "video_detail"


def test_h5_profile_maps_navigation_station_tabbar_to_home() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">导航站</div>'
        '<div class="van-tabbar-item">视频</div>'
        "</div></body></html>"
    )

    assert profile.tabbar_items[0].expected_page_type == "home"


def test_h5_profile_maps_requested_aliases_from_catalog() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">二次元</div>'
        '<div class="van-tabbar-item">星球</div>'
        '<div class="van-tabbar-item">资源</div>'
        "</div></body></html>"
    )

    assert [item.expected_page_type for item in profile.tabbar_items] == [
        "home",
        "post_list",
        "post_list",
    ]


def test_h5_profile_extracts_pages_foot_bottom_navigation() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="pages-foot-box">'
        '<ul class="pages-foot-fix justify-around">'
        '<li class="active home"><span class="pages-foot-label">首页</span></li>'
        '<li class="community"><span class="pages-foot-label">社区</span></li>'
        '<li class="mine"><span class="pages-foot-label">我的</span></li>'
        "</ul></div></body></html>"
    )

    assert [item.label for item in profile.tabbar_items] == ["首页", "社区", "我的"]
    assert profile.tabbar_items[0].active is True
    assert profile.tabbar_items[2].selector == (
        "ul.pages-foot-fix:visible > li:nth-of-type(3):visible"
    )
    assert profile.tabbar_items[2].expected_page_type == "profile"


def test_h5_profile_extracts_registered_footer_wrap_bottom_navigation() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="footer-wrap"><div><ul class="type color">'
        '<li class="footer-item home item-0"><div class="ico-act"></div>'
        '<div class="text">二次元</div></li>'
        '<li class="footer-item video item-1"><div class="text">视频</div></li>'
        '<li class="footer-item ysm item-2"><img alt=""></li>'
        '<li class="footer-item square item-3"><div class="text">广场</div></li>'
        '<li class="footer-item mine item-5"><div class="text">我的</div></li>'
        "</ul></div></div></body></html>"
    )

    assert [item.label for item in profile.tabbar_items] == [
        "二次元",
        "视频",
        "广场",
        "我的",
    ]
    assert profile.tabbar_items[0].active is True
    assert profile.tabbar_items[-1].selector == (
        "div.footer-wrap:visible ul.type:visible > "
        "li.footer-item:nth-of-type(5):visible"
    )
    assert profile.tabbar_items[-1].expected_page_type == "profile"


def test_h5_profile_extracts_page_tabs_and_business_structure() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="page-wraper van-safe-area-top">'
        '<div class="product-page">'
        '<div class="van-tabs van-tabs--line">'
        '<div class="van-tabs__nav van-tabs__nav--line">'
        '<div class="van-tab van-tab--active"><span>推荐</span></div>'
        '<div class="van-tab"><span>排行</span></div>'
        '<div class="van-tab"><span>任务</span></div>'
        "</div></div></div></div></body></html>"
    )

    assert profile.business_structure_present is True
    assert [item.label for item in profile.page_tab_items] == ["推荐", "排行", "任务"]
    assert profile.page_tab_items[0].active is True
    assert profile.page_tab_items[1].expected_page_type is None
    assert profile.page_tab_items[2].expected_page_type == "activity"


def test_h5_profile_keeps_payment_navigation_but_flags_payment_submit() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
        '<div class="van-tabbar-item">充值</div>'
        "</div>"
        '<button class="pay-btn">立即支付</button>'
        "</body></html>"
    )

    assert profile.tabbar_items[1].expected_page_type == "payment"
    assert [entry.reason_code for entry in profile.risk_entries] == ["PAYMENT_SUBMIT_ACTION"]


def test_h5_profile_projects_explicit_risk_actions_without_signing_clicks() -> None:
    profile = build_h5_exploration_profile(
        '<button data-risk-action="payment">充值购买</button>'
        '<button data-risk-action="post_publish">发布内容</button>'
        '<button data-risk-action="upload">上传文件</button>'
    )

    assert h5_profile_risk_flags(profile) == [
        "high_risk_payment_entry",
        "high_risk_publish_entry",
        "high_risk_upload_entry",
    ]


def test_h5_profile_does_not_use_unsafe_download_card_as_content_detail() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="content-list">'
        '<div class="download-card"><div class="media-item">下载APP</div></div>'
        "</div></body></html>",
        page_type="content_list",
    )

    assert profile.content_card is None


def test_h5_profile_extracts_custom_spa_footer_items_without_guessing_unknown_labels() -> None:
    profile = build_h5_exploration_profile(
        '<html><body><div class="footer-wrap bg-#fff"><div><ul class="type color">'
        '<li class="footer-item home"><div class="text">二次元</div></li>'
        '<li class="footer-item video"><div class="text">视频</div></li>'
        '<li class="footer-item ysm"><div class="text">有声漫</div></li>'
        '<li class="footer-item square"><div class="text">广场</div></li>'
        '<li class="footer-item mine active"><div class="text">我的</div></li>'
        '<li class="footer-item mystery"><div class="text">未收录入口</div></li>'
        '</ul></div></div></body></html>'
    )

    assert [item.label for item in profile.tabbar_items] == [
        "二次元",
        "视频",
        "有声漫",
        "广场",
        "我的",
        "未收录入口",
    ]
    assert [item.expected_page_type for item in profile.tabbar_items] == [
        "home",
        "video_list",
        "content_list",
        "post_list",
        "profile",
        None,
    ]
    assert profile.tabbar_items[4].active is True
    assert profile.tabbar_items[1].selector == (
        "div.footer-wrap:visible ul.type:visible > "
        "li.footer-item:nth-of-type(2):visible"
    )
    assert "visible_tabbar" in profile.stability_hints


def test_h5_profile_extracts_explicit_spa_nav_and_ignores_hidden_or_anchor_only_nav() -> None:
    profile = build_h5_exploration_profile(
        '<html><body>'
        '<nav class="bottom-nav"><div class="item active" role="tab">首页</div>'
        '<div class="item" role="tab">视频</div><div class="item" role="tab">未知</div></nav>'
        '<nav class="bottom-nav" style="display:none"><div role="tab">社区</div>'
        '<div role="tab">我的</div></nav>'
        '<div class="bottom-nav"><a href="/home">首页</a><a href="/mine">我的</a></div>'
        '</body></html>'
    )

    assert [item.label for item in profile.tabbar_items] == ["首页", "视频", "未知"]
    assert profile.tabbar_items[0].active is True
    assert profile.tabbar_items[1].expected_page_type == "video_list"
    assert profile.tabbar_items[2].expected_page_type is None
