"""桌面 Web 导航 class 模糊发现合同。"""

import pytest

from services.api.app.application.web_desktop_navigation import (
    WEB_NAVIGATION_SOURCE_SELECTOR,
    _NavigationParser,
    extract_navigation_candidates,
    navigation_snapshot_from_html,
)


def _runtime_navigation_candidates(
    html: str,
    *,
    base_url: str,
    **kwargs,
):
    parser = _NavigationParser()
    parser.feed(html)
    parser.close()
    parsed_snapshot = navigation_snapshot_from_html(html, base_url=base_url)
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "tag": element["tag"],
                "text": element["text"],
                "href": element["href"],
                "web_navigation_dom_path": "/".join(
                    str(index) for index in raw.dom_path
                ),
                "interactive": True,
                "visible": element["visible"],
                "hit_test": {
                    "center_hits_target": True,
                    "visual_occlusion_detected": False,
                    "pointer_interception": False,
                },
            }
            for raw, element in zip(parser.links, parsed_snapshot["elements"], strict=True)
        ],
    }
    return extract_navigation_candidates(
        html,
        base_url=base_url,
        snapshot=snapshot,
        **kwargs,
    )


def test_plain_html_href_navigation_is_actionable_without_hit_test() -> None:
    html = '<nav><a href="/articles">文章</a><a href="/videos">视频</a></nav>'

    candidates = extract_navigation_candidates(html, base_url="https://example.test/")
    assert [(item.label, item.url, item.actionability) for item in candidates] == [
        ("文章", "https://example.test/articles", "actionable"),
        ("视频", "https://example.test/videos", "actionable"),
    ]

    blocked = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        include_blocked=True,
    )

    assert blocked == candidates


def test_top_href_navigation_inside_main_does_not_require_click_hit_test() -> None:
    html = (
        '<main><div class="site-nav"><a href="/news">新闻</a>'
        '<a href="/videos">视频</a></div></main>'
    )
    parser = _NavigationParser()
    parser.feed(html)
    parser.close()
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "viewport": {"width": 1920, "height": 1080},
        "elements": [
            {
                "tag": "a",
                "text": raw.label,
                "href": raw.target_hint,
                "web_navigation_dom_path": "/".join(map(str, raw.dom_path)),
                "interactive": True,
                "visible": True,
                "rect": {"x": 20 + index * 100, "y": 80, "width": 80, "height": 40},
                "hit_test": {
                    "center_hits_target": False,
                    "visual_occlusion_detected": False,
                },
            }
            for index, raw in enumerate(parser.links)
        ],
    }
    candidates = extract_navigation_candidates(
        html, base_url="https://example.test/", snapshot=snapshot
    )
    assert [item.url for item in candidates] == [
        "https://example.test/news",
        "https://example.test/videos",
    ]
    assert all(item.actionability == "actionable" for item in candidates)


def test_extracts_only_safe_same_origin_links_inside_navigation() -> None:
    html = """
    <nav class="navbar navbar-expand">
      <div class="collapse navbar-collapse">
        <ul class="navbar-nav">
          <li class="nav-item"><a href="/dashboard">Dashboard</a></li>
          <li><a href="/reports?scope=today">报表</a></li>
          <li><a href="/dashboard">重复首页</a></li>
          <li><a href="https://outside.test/account">外站</a></li>
          <li><a href="javascript:void(0)">脚本</a></li>
          <li><a href="/logout">退出登录</a></li>
        </ul>
      </div>
    </nav>
    <a href="/outside-navbar">普通正文链接</a>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/home",
    )

    assert [(item.label, item.url) for item in candidates] == [
        ("Dashboard", "https://example.test/dashboard"),
        ("报表", "https://example.test/reports?scope=today"),
        ("重复首页", "https://example.test/dashboard"),
    ]
    assert candidates[0].page_type_key == "nav-class:dashboard"
    assert all(item.source_selector == WEB_NAVIGATION_SOURCE_SELECTOR for item in candidates)


@pytest.mark.parametrize("class_name", ["main-navigation", "shell topNAV"])
def test_fuzzy_matches_nav_substring_in_class_case_insensitively(
    class_name: str,
) -> None:
    html = f"""
    <header class="{class_name}">
      <ul><li><a href="/reports">报表</a></li><li><a href="/metrics">指标</a></li></ul>
    </header>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/home",
    )

    assert [item.url for item in candidates] == ["https://example.test/reports", "https://example.test/metrics"]


def test_ul_li_is_not_required_and_hidden_href_links_are_collected() -> None:
    html = """
    <div class="navbar-nav">
      <a href="/missing-list">不在列表</a>
      <ul>
        <li hidden><a href="/hidden">隐藏入口</a></li>
        <li><a aria-hidden="true" href="/aria-hidden">语义隐藏</a></li>
        <li><a href="/visible">可见入口</a></li>
      </ul>
    </div>
    <div class="menu">
      <ul><li><a href="/not-navigation">普通列表</a></li></ul>
    </div>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/home",
    )

    assert [item.url for item in candidates] == [
        "https://example.test/missing-list",
        "https://example.test/hidden",
        "https://example.test/aria-hidden",
        "https://example.test/visible",
    ]


@pytest.mark.parametrize(
    ("html", "expected_label", "expected_signal"),
    [
        ('<nav><a href="/a">A</a><a href="/other">其他分类</a></nav>', "A", "semantic_nav"),
        (
            '<div class="main-navigation"><a href="/b">B</a><a href="/other">其他分类</a></div>',
            "B",
            "class_or_id_contains_nav",
        ),
        (
            '<ul id="top-nav"><li><a href="/c">C</a></li>'
            '<li><a href="/other">其他分类</a></li></ul>',
            "C",
            "class_or_id_contains_nav",
        ),
        (
            '<div aria-label="Primary navigation"><a href="/d">D</a>'
            '<a href="/other">其他分类</a></div>',
            "D",
            "navigation_attribute",
        ),
        (
            '<section role="navigation"><a href="/e">E</a><a href="/other">其他分类</a></section>',
            "E",
            "role_navigation",
        ),
        (
            '<div data-testid="primary-nav"><a href="/f">F</a><a href="/other">其他分类</a></div>',
            "F",
            "navigation_attribute",
        ),
    ],
)
def test_extracts_semantic_and_fuzzy_navigation(
    html: str,
    expected_label: str,
    expected_signal: str,
) -> None:
    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/",
    )

    assert tuple(item.label for item in candidates) == (expected_label, "其他分类")
    assert candidates[0].source_signal == expected_signal


def test_same_url_in_two_menu_paths_keeps_two_identities() -> None:
    html = """
    <nav>
      <ul>
        <li><a href="/moviesets">精选合集</a>
          <ul><li><a href="/moviesets">最新合集</a></li></ul>
        </li>
        <li><a href="/articles">文章</a></li>
      </ul>
    </nav>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/",
    )

    assert [item.url for item in candidates] == [
        "https://example.test/moviesets",
        "https://example.test/moviesets",
        "https://example.test/articles",
    ]
    assert [item.menu_path for item in candidates] == [
        ("精选合集",),
        ("精选合集", "最新合集"),
        ("文章",),
    ]
    assert len({item.entry_id for item in candidates}) == 3
    assert len({item.dom_fingerprint for item in candidates}) == 3


def test_ignores_comments_hidden_templates_and_href_less_groups() -> None:
    html = """
    <nav>
      <!-- <a href="/commented">注释菜单</a> -->
      <template><a href="/template">模板菜单</a></template>
      <span>无链接分组</span>
      <a href="/visible">可见菜单</a>
      <a href="/other">其他分类</a>
    </nav>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/",
    )

    assert [(item.label, item.url) for item in candidates] == [
        ("可见菜单", "https://example.test/visible"),
        ("其他分类", "https://example.test/other"),
    ]


def test_ready_navigation_excludes_ads_footer_breadcrumb_pagination_and_downloads() -> None:
    html = """
    <nav>
      <a href="/articles">文章</a>
      <a href="/videos">视频</a>
      <div class="sponsored-ad"><a href="/promo">推广</a></div>
      <nav class="breadcrumb"><a href="/">首页</a><a href="/articles">文章</a></nav>
      <div class="pagination"><a href="/articles?page=2">下一页</a></div>
      <a href="/download/client.apk">下载 APP</a>
    </nav>
    <footer class="footer-nav"><a href="/contact">联系我们</a></footer>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/",
    )

    assert [(item.label, item.url) for item in candidates] == [
        ("文章", "https://example.test/articles"),
        ("视频", "https://example.test/videos"),
    ]


def test_breadcrumb_is_not_a_menu_candidate_even_in_blocked_audit_mode() -> None:
    html = """
    <nav id="main"><a href="/articles">文章</a><a href="/videos">视频</a></nav>
    <nav class="xqbj-breadcrumb-container" aria-label="breadcrumb">
      <a href="/">首页</a><a href="/articles">文章</a>
    </nav>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/articles",
        include_blocked=True,
    )

    assert [(item.label, item.url) for item in candidates] == [
        ("文章", "https://example.test/articles"),
        ("视频", "https://example.test/videos"),
    ]


@pytest.mark.parametrize("disabled", (False, True), ids=("enabled", "disabled"))
def test_aria_label_breadcrumb_never_enters_candidate_or_audit(disabled: bool) -> None:
    disabled_attribute = ' aria-disabled="true"' if disabled else ""
    html = (
        '<nav id="main"><a href="/articles">文章</a><a href="/videos">视频</a></nav>'
        '<nav aria-label="breadcrumb"><a href="/">首页</a>'
        f'<a href="/articles"{disabled_attribute}>文章</a></nav>'
    )

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/articles",
        include_blocked=True,
    )

    assert [(item.label, item.url) for item in candidates] == [
        ("文章", "https://example.test/articles"),
        ("视频", "https://example.test/videos"),
    ]


def test_extracts_semantic_button_js_spa_but_excludes_body_card_candidates() -> None:
    html = """
    <nav>
      <span role="link" data-href="/semantic">语义入口</span>
      <button data-url="/button">按钮入口</button>
      <button onclick="location.href='/script'">脚本入口</button>
      <router-link to="/spa">SPA 入口</router-link>
      <button>无目标入口</button>
    </nav>
    <article class="video-card" data-route="/visual">视觉卡片</article>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/",
        source_capture_id="capture-ready",
        source_epoch=3,
    )

    assert [item.source_kind for item in candidates] == [
        "semantic_link",
        "button",
        "js_control",
        "spa_route",
        "button",
    ]
    assert [item.url for item in candidates] == [
        "https://example.test/semantic",
        "https://example.test/button",
        "https://example.test/script",
        "https://example.test/spa",
        None,
    ]
    assert candidates[4].actionability == "requires_runtime_validation"
    assert all(item.source_capture_id == "capture-ready" for item in candidates)
    assert all(item.source_epoch == 3 for item in candidates)


def test_hidden_safe_href_is_collected_while_ad_dangerous_and_cross_origin_are_not() -> None:
    html = """
    <nav>
      <a hidden href="/hidden">隐藏</a>
      <div class="advert"><a href="/promo">广告</a></div>
      <button data-route="/purchase">购买</button>
      <a href="https://outside.test/">外链</a>
      <a href="/safe">安全</a>
      <a href="/other">其他分类</a>
    </nav>
    """

    candidates = _runtime_navigation_candidates(
        html,
        base_url="https://example.test/",
    )

    assert [(item.label, item.url, item.risk_level) for item in candidates] == [
        ("隐藏", "https://example.test/hidden", "low"),
        ("安全", "https://example.test/safe", "low"),
        ("其他分类", "https://example.test/other", "low"),
    ]
