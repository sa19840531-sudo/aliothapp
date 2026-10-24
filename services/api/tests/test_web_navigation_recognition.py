"""导航容器与页面业务卡片之间的可执行边界。"""

import pytest

from services.api.app.application.web_desktop_navigation import (
    _NavigationParser,
    extract_navigation_candidates,
    navigation_snapshot_from_html,
)


def _runtime_navigation_snapshot(html: str, *, base_url: str) -> dict[str, object]:
    parser = _NavigationParser()
    parser.feed(html)
    parser.close()
    parsed_snapshot = navigation_snapshot_from_html(html, base_url=base_url)
    return {
        "schema_version": "ui-snapshot.v2",
        "viewport": {"width": 1280, "height": 900},
        "elements": [
            {
                "tag": element["tag"],
                "text": element["text"],
                "href": element["href"],
                "web_navigation_dom_path": "/".join(str(index) for index in raw.dom_path),
                "interactive": True,
                "visible": element["visible"],
                "rect": {
                    "x": 20 + index * 120,
                    "y": 20,
                    "width": 100,
                    "height": 40,
                },
                "hit_test": {
                    "center_hits_target": True,
                    "visual_occlusion_detected": False,
                    "pointer_interception": False,
                },
            }
            for index, (raw, element) in enumerate(
                zip(parser.links, parsed_snapshot["elements"], strict=True)
            )
        ],
    }


@pytest.mark.parametrize("gate", (None, "aria_modal", "web_human_verification_present", "age_gate"))
def test_nonmodal_geometric_badge_does_not_block_uncovered_navigation(gate) -> None:
    html = '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav>'
    snapshot = _runtime_navigation_snapshot(html, base_url="https://example.test/")
    overlay = {
        "tag": "span",
        "selector": "#post-card-title span",
        "visible": True,
        "web_obstruction_layer_structural": True,
        "web_obstruction_layer_kind": "geometric_overlay",
        "rect": {"x": 1316.68, "y": 280.46, "width": 162.63, "height": 162.63},
    }
    if gate == "age_gate":
        overlay["web_obstruction_layer_kind"] = "age_gate"
    elif gate:
        overlay[gate] = True
    snapshot["elements"].append(overlay)
    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=snapshot,
    )
    assert [item.label for item in candidates] == (["分类甲", "分类乙"] if gate is None else [])


def test_geometric_overlay_does_not_block_direct_href_navigation() -> None:
    html = '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a><a href="/c">分类丙</a></nav>'
    snapshot = _runtime_navigation_snapshot(html, base_url="https://example.test/")
    snapshot["elements"][2]["hit_test"]["center_hits_target"] = False
    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=snapshot,
    )
    assert [item.label for item in candidates] == ["分类甲", "分类乙", "分类丙"]


@pytest.mark.parametrize(
    "x,y",
    ((3372.22, 90.91), (100, -148.89), (100, -1), (-1, 40), (1900, 40), (100, 1060)),
)
@pytest.mark.parametrize("include_blocked", (False, True))
def test_top_navigation_href_is_kept_when_its_item_is_outside_viewport(
    x, y, include_blocked
) -> None:
    html = '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a><a href="/c">分类丙</a></nav>'
    snapshot = _runtime_navigation_snapshot(html, base_url="https://example.test/")
    snapshot["viewport"] = {"width": 1920, "height": 1080}
    for index, element in enumerate(snapshot["elements"]):
        element["rect"] = {"x": 100 + index * 100, "y": 40, "width": 88, "height": 46}
    snapshot["elements"][2]["rect"].update(x=x, y=y)
    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=snapshot,
        include_blocked=include_blocked,
    )
    assert [item.label for item in candidates] == ["分类甲", "分类乙", "分类丙"]


def test_body_route_card_is_not_menu_even_with_valid_navigation() -> None:
    html = (
        '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav>'
        '<article class="card" data-route="/post/1">正文卡片</article>'
    )
    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=_runtime_navigation_snapshot(html, base_url="https://example.test/"),
    )
    assert [item.label for item in candidates] == ["分类甲", "分类乙"]


def test_home_menu_only_comes_from_proved_top_header_navigation() -> None:
    cards = "".join(
        f'<article class="card"><a href="/article/{index}">卡片{index}</a></article>'
        for index in range(25)
    )
    html = (
        '<header><nav id="primary"><a href="/news">资讯</a>'
        '<a href="/videos">视频</a></nav></header>'
        f'<main><nav class="card-navigation">{cards}</nav>'
        '<nav class="pagination"><a href="/page/2">下一页</a>'
        '<a href="/page/3">第三页</a></nav></main>'
        '<form role="search" class="search-navigation"><a href="/hot">热词甲</a>'
        '<a href="/latest">热词乙</a></form>'
        '<div role="toolbar" class="content-navigation"><a href="/share">分享</a>'
        '<a href="/collect">收藏</a></div>'
        '<aside><nav><a href="/related/a">相关推荐甲</a>'
        '<a href="/related/b">相关推荐乙</a></nav></aside>'
        '<section class="advert"><nav><a href="/promo/a">推广甲</a>'
        '<a href="/promo/b">推广乙</a></nav></section>'
        '<footer><nav><a href="/about">关于</a><a href="/contact">联系</a></nav></footer>'
    )

    candidates = extract_navigation_candidates(
        html,
        base_url="https://top-navigation.test/",
        snapshot=_runtime_navigation_snapshot(
            html,
            base_url="https://top-navigation.test/",
        ),
    )

    assert [(item.label, item.url) for item in candidates] == [
        ("资讯", "https://top-navigation.test/news"),
        ("视频", "https://top-navigation.test/videos"),
    ]


def test_middle_page_nav_named_container_is_not_a_home_menu() -> None:
    html = '<div class="content-nav"><a href="/a">正文甲</a><a href="/b">正文乙</a></div>'
    snapshot = _runtime_navigation_snapshot(html, base_url="https://example.test/")
    for element in snapshot["elements"]:
        element["rect"]["y"] = 500

    assert (
        extract_navigation_candidates(
            html,
            base_url="https://example.test/",
            snapshot=snapshot,
        )
        == ()
    )


def test_bottom_positioned_header_is_not_a_home_menu() -> None:
    html = '<header><nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav></header>'
    snapshot = _runtime_navigation_snapshot(html, base_url="https://example.test/")
    for element in snapshot["elements"]:
        element["rect"]["y"] = 700

    assert (
        extract_navigation_candidates(html, base_url="https://example.test/", snapshot=snapshot)
        == ()
    )


def test_top_navigation_keeps_all_members_after_container_position_is_proven() -> None:
    html = '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a><a href="/c">分类丙</a></nav>'
    snapshot = _runtime_navigation_snapshot(html, base_url="https://example.test/")
    snapshot["elements"][0]["rect"]["y"] = 20
    snapshot["elements"][1]["rect"]["y"] = 80
    snapshot["elements"][2]["rect"]["y"] = 700

    candidates = extract_navigation_candidates(
        html, base_url="https://example.test/", snapshot=snapshot
    )
    assert [item.label for item in candidates] == ["分类甲", "分类乙", "分类丙"]


@pytest.mark.parametrize("case", ["normal", "breadcrumb", "single", "hidden", "occluded"])
def test_actual_capture_pointer_reception_does_not_mean_intercepted(case) -> None:
    """生产字段表示目标接收指针；命中失败和遮挡才是否决事实。"""
    attrs = ' aria-label="breadcrumb"' if case == "breadcrumb" else ""
    second = "" if case == "single" else '<a href="/b">分类乙</a>'
    html = f'<nav{attrs}><a href="/a">分类甲</a>{second}</nav>'
    snapshot = _runtime_navigation_snapshot(html, base_url="https://example.test/")
    for element in snapshot["elements"]:
        element["hit_test"]["pointer_interception"] = True
        if case == "hidden":
            element["visible"] = False
        if case == "occluded":
            element["hit_test"]["center_hits_target"] = False
            element["hit_test"]["visual_occlusion_detected"] = True
    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=snapshot,
        require_runtime_hit_test=True,
    )
    assert [item.label for item in candidates] == (
        [] if case in {"breadcrumb", "single"} else ["分类甲", "分类乙"]
    )


def test_logo_search_and_auth_do_not_prove_category_navigation() -> None:
    html = (
        '<nav><a class="logo" href="/">站标</a><a href="/search">搜索</a>'
        '<a href="/login">登录</a></nav>'
    )
    assert extract_navigation_candidates(html, base_url="https://example.test/") == ()


def test_one_category_in_each_separate_container_cannot_prove_either() -> None:
    html = '<nav id="one"><a href="/a">分类甲</a></nav><nav id="two"><a href="/b">分类乙</a></nav>'
    assert extract_navigation_candidates(html, base_url="https://example.test/") == ()


def test_blocked_navigation_entries_retain_reason_and_container_evidence() -> None:
    html = (
        '<nav id="main"><a href="/a">分类甲</a><a href="/b">分类乙</a>'
        '<a href="/login">登录</a><a href="/search">搜索</a><a href="/pay">支付</a>'
        '<a download href="/file">附件</a><a href="https://outside.test/">外站</a>'
        '<div class="advert"><a href="/ad">推广</a></div></nav>'
        '<footer><nav><a href="/contact">联系</a></nav></footer>'
    )
    candidates = extract_navigation_candidates(
        html, base_url="https://example.test/", include_blocked=True
    )
    by_label = {item.label: item for item in candidates}
    assert set(by_label) == {
        "分类甲",
        "分类乙",
        "登录",
        "搜索",
        "支付",
        "附件",
        "外站",
        "推广",
        "联系",
    }
    for label in ("登录", "搜索", "支付", "附件", "外站", "推广", "联系"):
        assert by_label[label].actionability == "blocked"
        assert by_label[label].navigation_evidence
    assert by_label["分类甲"].navigation_container_id == by_label["分类乙"].navigation_container_id


def test_sparse_snapshot_proves_navigation_without_container_nodes() -> None:
    from services.api.app.application.web_navigation_recognition import assess_navigation_container

    snapshot = {
        "elements": [
            {
                "tag": "a",
                "selector": f"#main > a:nth-child({i})",
                "text": label,
                "href": href,
                "within_web_navigation": True,
                "visible": True,
                "interactive": True,
                "hit_test": {"center_hits_target": True},
            }
            for i, label, href in ((1, "分类甲", "/a"), (2, "分类乙", "/b"))
        ]
    }
    assessment = assess_navigation_container(snapshot)
    assert assessment.proven is True
    assert assessment.facts.visible_category_entry_count == 2


@pytest.mark.parametrize(
    "fact", ["within_footer", "within_breadcrumb", "is_advertisement", "occluded"]
)
def test_excluded_snapshot_entries_cannot_prove_container(fact) -> None:
    from services.api.app.application.web_navigation_recognition import assess_navigation_container

    assessment = assess_navigation_container(
        {
            "elements": [
                {
                    "tag": "a",
                    "selector": f"#main > a:nth-child({i})",
                    "text": label,
                    "href": href,
                    "within_web_navigation": True,
                    "visible": True,
                    "interactive": True,
                    fact: True,
                }
                for i, label, href in ((1, "分类甲", "/a"), (2, "分类乙", "/b"))
            ]
        }
    )
    assert assessment.proven is False
    assert assessment.reason_codes == ("WEB_NAVIGATION_CONTAINER_UNPROVEN",)


def test_mobile_top_navigation_keeps_explicitly_hidden_href_children() -> None:
    html = (
        '<nav id="mobile"><button aria-expanded="true" aria-controls="menu">分类</button>'
        '<div id="menu"><a href="/a">分类甲</a><a href="/b">分类乙</a></div>'
        '<div hidden><a href="/c">隐藏</a></div></nav>'
    )
    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=_runtime_navigation_snapshot(html, base_url="https://example.test/"),
    )
    assert [item.label for item in candidates] == ["分类", "分类甲", "分类乙", "隐藏"]
    assert candidates[0].actionability == "requires_runtime_validation"


def test_fully_hidden_top_navigation_is_collected_for_direct_navigation() -> None:
    html = (
        '<nav id="mobile" hidden><a href="/movie">电影</a>'
        '<a href="/tv">电视剧</a><a href="/search">搜索</a></nav>'
    )

    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot={"schema_version": "ui-snapshot.v2", "elements": []},
    )

    assert [(item.label, item.url, item.visibility, item.actionability) for item in candidates] == [
        ("电影", "https://example.test/movie", "hidden", "actionable"),
        ("电视剧", "https://example.test/tv", "hidden", "actionable"),
    ]


def test_snapshot_obstruction_vetoes_dom_navigation() -> None:
    html = '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav>'
    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot={"human_verification_present": True},
        include_blocked=True,
    )
    assert all(item.actionability == "blocked" for item in candidates)
    assert len(candidates) == 2


def test_cross_origin_and_download_entries_cannot_prove_container() -> None:
    html = (
        '<nav><a href="/a">分类甲</a><a href="https://outside.test/">外站</a>'
        '<a download href="/file">附件</a></nav>'
    )
    assert extract_navigation_candidates(html, base_url="https://example.test/") == ()


def test_capture_policy_keeps_visibility_as_audit_fact_for_dom_href_navigation() -> None:
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureMode,
        PageCaptureRequest,
        PageCaptureResult,
    )
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    snapshot = {
        "elements": [
            {"tag": "a", "text": "分类乙", "href": "/b", "interactive": True, "visible": False}
        ]
    }
    result = WebDesktopCapturePolicyBrowser(
        browser=object(), include_blocked_navigation=True
    )._apply_result_policy(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE, session_id="test", url="https://example.test/"
        ),
        PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="https://example.test/",
            title="首页",
            status_code=200,
            artifacts=[
                PageCaptureArtifact(
                    "dom", None, '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav>'
                ),
                PageCaptureArtifact("ui_snapshot", None, snapshot),
            ],
        ),
    )
    saved = result.artifacts[1].content
    assert saved["web_navigation"]["structure_facts"]["top_navigation_container_count"] == 0
    assert [
        (item["label"], item["visibility"], item["actionability"])
        for item in saved["web_navigation"]["candidates"]
    ] == [
        ("分类甲", "visible", "actionable"),
        ("分类乙", "hidden", "actionable"),
    ]


def test_home_structure_does_not_use_logo_auth_only_as_navigation_proof() -> None:
    from services.api.app.application.web_page_classification import web_page_structure_facts

    facts = web_page_structure_facts(
        {
            "elements": [
                {"tag": "nav", "selector": "#nav", "visible": True},
                {
                    "tag": "a",
                    "selector": "#nav > a",
                    "text": "登录",
                    "href": "/login",
                    "interactive": True,
                    "visible": True,
                    "within_navigation": True,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        },
        current_url="https://example.test/",
    )
    assert facts.top_navigation_container_count == 0
    assert facts.navigation_structure_facts.auth_entry_count == 1


def test_image_logo_does_not_supply_second_category() -> None:
    html = '<nav><a href="/"><img src="/logo.svg" alt="站点"></a><a href="/a">分类甲</a></nav>'
    assert extract_navigation_candidates(html, base_url="https://example.test/") == ()


def test_real_hit_test_occlusion_does_not_block_direct_href_navigation() -> None:
    html = '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav>'
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "tag": "a",
                "text": label,
                "href": href,
                "visible": True,
                "interactive": True,
                "hit_test": {"center_hits_target": False, "visual_occlusion_detected": True},
            }
            for label, href in (("分类甲", "/a"), ("分类乙", "/b"))
        ],
    }
    candidates = extract_navigation_candidates(
        html, base_url="https://example.test/", snapshot=snapshot, include_blocked=True
    )
    assert len(candidates) == 2
    assert all(item.actionability == "actionable" for item in candidates)


@pytest.mark.parametrize(
    "hit_test",
    (None, False, "center_hits_target", {}),
    ids=("missing", "false", "string", "empty_mapping"),
)
def test_direct_href_navigation_does_not_require_positive_hit_test_evidence(hit_test) -> None:
    html = '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav>'
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "tag": "a",
                "text": label,
                "href": href,
                "visible": True,
                "interactive": True,
                "hit_test": hit_test,
            }
            for label, href in (("分类甲", "/a"), ("分类乙", "/b"))
        ],
    }

    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=snapshot,
    )

    assert [item.url for item in candidates] == [
        "https://example.test/a",
        "https://example.test/b",
    ]
    assert all(item.actionability == "actionable" for item in candidates)


def test_fully_hidden_top_navigation_keeps_safe_dom_hrefs() -> None:
    html = (
        '<nav><a href="/a" style="display: none">隐藏</a>'
        '<a href="/b">分类乙</a><a href="/c">分类丙</a></nav>'
    )
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "tag": "a",
                "text": "不匹配的节点",
                "href": "/unknown",
                "visible": True,
                "interactive": True,
                "hit_test": {"center_hits_target": True},
            }
        ],
    }

    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=snapshot,
    )

    assert [(item.label, item.visibility) for item in candidates] == [
        ("隐藏", "hidden"),
        ("分类乙", "hidden"),
        ("分类丙", "hidden"),
    ]


def test_capture_policy_makes_safe_dom_href_navigation_actionable_without_snapshot() -> None:
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureMode,
        PageCaptureRequest,
        PageCaptureResult,
    )
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    result = WebDesktopCapturePolicyBrowser(browser=object())._apply_result_policy(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="test-current-snapshot",
            url="https://example.test/",
        ),
        PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="https://example.test/",
            title="首页",
            status_code=200,
            artifacts=[
                PageCaptureArtifact(
                    "dom", None, '<nav><a href="/a">分类甲</a><a href="/b">分类乙</a></nav>'
                ),
                PageCaptureArtifact("ui_snapshot", None, {}),
            ],
        ),
    )

    saved = result.artifacts[1].content
    assert [
        (item["label"], item["url"], item["actionability"])
        for item in saved["web_navigation"]["candidates"]
    ] == [
        ("分类甲", "https://example.test/a", "actionable"),
        ("分类乙", "https://example.test/b", "actionable"),
    ]


def test_visible_hit_tested_navigation_uses_point_seven_floor_and_excludes_breadcrumb() -> None:
    html = (
        '<nav><a href="/video">视频</a><a href="/news">资讯</a>'
        '<a href="/low">低置信度</a></nav>'
        '<nav class="breadcrumb"><a href="/">首页</a><a href="/news">资讯</a></nav>'
    )
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "tag": "a",
                "text": label,
                "href": href,
                "visible": True,
                "interactive": True,
                "web_navigation_dom_path": dom_path,
                "navigation_confidence": confidence,
                "hit_test": {
                    "center_hits_target": True,
                    "visual_occlusion_detected": False,
                },
            }
            for label, href, confidence, dom_path in (
                ("视频", "/video", 0.70, "1/1"),
                ("资讯", "/news", 0.70, "1/2"),
                ("低置信度", "/low", 0.69, "1/3"),
                ("首页", "/", 0.70, "2/1"),
                ("资讯", "/news", 0.70, "2/2"),
            )
        ],
    }

    candidates = extract_navigation_candidates(
        html,
        base_url="https://example.test/",
        snapshot=snapshot,
    )

    assert [item.label for item in candidates] == ["视频", "资讯"]


@pytest.mark.parametrize("confidence", (0.69, float("nan")))
def test_navigation_confidence_below_or_outside_numeric_floor_is_not_trusted(confidence) -> None:
    from services.api.app.application.web_navigation_recognition import (
        navigation_runtime_evidence_is_trusted,
    )

    assert (
        navigation_runtime_evidence_is_trusted(
            {
                "navigation_confidence": confidence,
                "hit_test": {"center_hits_target": True},
            }
        )
        is False
    )


def test_collapsed_href_descendants_are_collected_without_click_and_can_inherit_parent() -> None:
    collapsed_html = (
        '<nav><a href="/video">视频</a><a href="/news">资讯</a>'
        '<button aria-expanded="false" aria-controls="more">更多</button>'
        '<div id="more" hidden><a href="/movie">电影</a><a href="/tv">电视剧</a></div></nav>'
    )
    collapsed = extract_navigation_candidates(
        collapsed_html,
        base_url="https://example.test/",
        snapshot={
            "schema_version": "ui-snapshot.v2",
            "elements": [
                {
                    "tag": "a",
                    "text": label,
                    "href": href,
                    "visible": True,
                    "interactive": True,
                    "navigation_confidence": 0.70,
                    "hit_test": {"center_hits_target": True},
                }
                for label, href in (("视频", "/video"), ("资讯", "/news"))
            ]
            + [
                {
                    "tag": "button",
                    "text": "更多",
                    "visible": True,
                    "interactive": True,
                    "navigation_confidence": 0.70,
                    "hit_test": {"center_hits_target": True},
                }
            ],
        },
    )
    parent = next(item for item in collapsed if item.label == "更多")

    expanded = extract_navigation_candidates(
        collapsed_html.replace('aria-expanded="false"', 'aria-expanded="true"').replace(
            ' id="more" hidden', ' id="more"'
        ),
        base_url="https://example.test/",
        snapshot={
            "schema_version": "ui-snapshot.v2",
            "elements": [
                {
                    "tag": tag,
                    "text": label,
                    "href": href,
                    "visible": True,
                    "interactive": True,
                    "navigation_confidence": 0.70,
                    "hit_test": {
                        "center_hits_target": True,
                        "visual_occlusion_detected": False,
                    },
                }
                for tag, label, href in (
                    ("a", "视频", "/video"),
                    ("a", "资讯", "/news"),
                    ("button", "更多", ""),
                    ("a", "电影", "/movie"),
                    ("a", "电视剧", "/tv"),
                )
            ],
        },
        parent_candidate=parent,
        parent_menu_entry_id="runtime:more",
    )

    assert {item.label for item in collapsed} == {
        "视频",
        "资讯",
        "更多",
        "电影",
        "电视剧",
    }
    assert all(
        item.visibility == "hidden" for item in collapsed if item.label in {"电影", "电视剧"}
    )
    children = [item for item in expanded if item.label in {"电影", "电视剧"}]
    assert [item.label for item in children] == ["电影", "电视剧"]
    assert all(item.parent_menu_entry_id == "runtime:more" for item in children)
    assert all(item.menu_depth == parent.menu_depth + 1 for item in children)
