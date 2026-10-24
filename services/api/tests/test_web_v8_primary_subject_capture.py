"""真实浏览器采集必须提供 Web v8 主体组合事实，默认采集合同保持不变。"""

from pathlib import Path

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.application.web_primary_subject_facts import extract_subject_region_facts
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.test_web_page_recognition_rules import classify
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)

_DETAIL_PLAYER_HTML = """
<main style="position:relative;width:1920px;height:3480px">
  <section style="position:relative;left:96px;top:105px;width:1308px;height:460px">
    <div id="cyberPlayer" style="position:absolute;inset:0">
      <video id="primary-video" style="width:100%;height:100%"></video>
      <button aria-label="播放" style="position:absolute;left:50%;top:50%">▶</button>
    </div>
  </section>
</main>
"""


@pytest.mark.asyncio
async def test_primary_player_mount_is_not_a_geometric_obstruction():
    """黄果爱详情页的绝对定位播放器不能遮挡它自己的视频主体。"""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(_DETAIL_PLAYER_HTML)
            await page.evaluate("""() => {
                const video = document.querySelector('#primary-video');
                video.src = URL.createObjectURL(new Blob([new Uint8Array([0])],
                    {type: 'video/mp4'}));
            }""")
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    player = next(node for node in snapshot["elements"] if node.get("tag") == "video")
    play_button = next(
        node for node in snapshot["elements"]
        if node.get("tag") == "button" and node.get("aria_label") == "播放"
    )
    assert player["src"].startswith("blob:")
    assert player["web_obstruction_layer_structural"] is False
    assert play_button["web_obstruction_layer_structural"] is False
    assert web_page_structure_facts(snapshot).top_level_obstruction_count == 0


@pytest.mark.asyncio
async def test_real_dialog_over_player_remains_a_structural_obstruction():
    """排除播放器自身时，覆盖它的真实对话框仍须保留阻断证据。"""
    from playwright.async_api import async_playwright

    dialog = (
        '<div role="dialog" aria-modal="true" '
        'style="position:fixed;left:350px;top:150px;width:900px;height:600px;'
        'z-index:100;background:white"><button>关闭</button></div>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(_DETAIL_PLAYER_HTML + dialog)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert any(
        node.get("web_obstruction_layer_structural") is True
        and node.get("web_obstruction_layer_kind") == "dialog"
        for node in snapshot["elements"]
    )
    assert web_page_structure_facts(snapshot).top_level_obstruction_count > 0


@pytest.mark.asyncio
async def test_absolute_video_covering_independent_article_remains_obstruction():
    """主体内的绝对定位视频若遮住独立正文，仍是几何阻断层。"""
    from playwright.async_api import async_playwright

    html = (
        '<main style="position:relative;width:1000px;height:1600px">'
        '<article><p>' + "正文" * 450 + '</p></article>'
        '<div style="position:absolute;left:0;top:0;width:1000px;height:700px;'
        'z-index:100;background:white">'
        '<video id="floating-video" style="width:100%;height:100%"></video>'
        '</div></main>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1000, "height": 700})
            await page.set_content(html)
            await page.evaluate("""() => {
                document.querySelector('#floating-video').src = URL.createObjectURL(
                    new Blob([new Uint8Array([0])], {type: 'video/mp4'})
                );
            }""")
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    video = next(node for node in snapshot["elements"] if node.get("tag") == "video")
    assert video["web_obstruction_layer_structural"] is True
    assert web_page_structure_facts(snapshot).top_level_obstruction_count > 0


@pytest.mark.asyncio
async def test_unmarked_floating_video_over_primary_player_remains_obstruction():
    """无广告类名的视频浮层盖住独立主播放器时不能豁免遮挡。"""
    from playwright.async_api import async_playwright

    overlay = (
        '<div style="position:absolute;left:96px;top:105px;width:1308px;'
        'height:460px;z-index:100;background:#111">'
        '<video id="floating-video" style="width:100%;height:100%"></video>'
        '</div>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(_DETAIL_PLAYER_HTML.replace("</main>", overlay + "</main>"))
            await page.evaluate("""() => {
                for (const video of document.querySelectorAll('video')) {
                    video.src = URL.createObjectURL(new Blob([new Uint8Array([0])],
                        {type: 'video/mp4'}));
                }
            }""")
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    floating = next(
        node for node in snapshot["elements"]
        if node.get("tag") == "video" and node.get("selector") == "#floating-video"
    )
    assert floating["web_obstruction_layer_structural"] is True
    assert web_page_structure_facts(snapshot).top_level_obstruction_count > 0


@pytest.mark.asyncio
async def test_unmarked_floating_video_without_own_layout_slot_remains_obstruction():
    """覆盖图集的视频浮层不能仅凭一个视频节点冒充主体播放器。"""
    from playwright.async_api import async_playwright

    html = (
        '<main style="position:relative;width:1920px;height:1500px">'
        '<section id="gallery" style="position:relative;left:96px;top:105px;'
        'width:1308px;height:460px;background:#eee">'
        '<img alt="图集" style="width:100%;height:100%"></section>'
        '<div style="position:absolute;left:96px;top:105px;width:1308px;'
        'height:460px;z-index:100;background:#111">'
        '<video id="floating-video" style="width:100%;height:100%"></video>'
        '</div></main>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            await page.evaluate("""() => {
                document.querySelector('#floating-video').src = URL.createObjectURL(
                    new Blob([new Uint8Array([0])], {type: 'video/mp4'})
                );
            }""")
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    floating = next(
        node for node in snapshot["elements"]
        if node.get("tag") == "video" and node.get("selector") == "#floating-video"
    )
    assert floating["web_obstruction_layer_structural"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture,kind,players,cards,recommendations",
    [
        ("video_primary_with_recommendations.html", "video", 1, 0, 12),
        ("article_with_inline_video.html", "article", 0, 0, 0),
        ("gallery_primary.html", "gallery", 0, 0, 0),
        ("list_with_repeated_primary_cards.html", "unknown", 0, 8, 0),
        ("vendor_neutral_iframe_player.html", "video", 1, 0, 0),
        ("conflicting_primary_regions.html", "conflicting", 2, 0, 0),
    ],
)
async def test_real_collector_supplies_generic_subject_region_facts(
    fixture, kind, players, cards, recommendations
):
    from playwright.async_api import async_playwright

    html = (Path(__file__).parent / "fixtures" / "web_detail" / fixture).read_text(encoding="utf-8")
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.route("https://example.org/**", lambda route: route.abort())
            await page.set_content(
                '<base href="https://example.org/"><style>'
                "img {min-width:100px;min-height:60px} "
                "a,input,button {display:block;min-width:120px;min-height:30px}"
                "</style>" + html,
                wait_until="domcontentloaded",
            )
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_subject_kind == kind
    assert facts.primary_player_count == players
    assert facts.repeated_primary_card_count == cards
    assert facts.secondary_recommendation_card_count == recommendations
    expected = {
        "video": "video_detail",
        "article": "content_detail",
        "gallery": "content_detail",
    }.get(kind, "content_list" if cards else "unknown")
    result, _ = await classify(
        "video_detail" if kind == "conflicting" else expected, web_page_structure_facts(snapshot)
    )
    assert result.page_type == expected
    assert result.subject_region_facts == facts
    assert result.structure_fingerprint.startswith("sha256:")


@pytest.mark.asyncio
async def test_iframe_subject_capture_is_web_only_and_bounded():
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.route("https://example.org/**", lambda route: route.abort())
            await page.set_content(
                '<base href="https://example.org/">'
                + "".join(
                    '<iframe src="/embed" width="80" height="45"></iframe>' for _ in range(80)
                ),
                wait_until="domcontentloaded",
            )
            default_snapshot = await _collect_ui_snapshot(page)
            web_snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")
    assert not any(node["tag"] == "iframe" for node in default_snapshot["elements"])
    assert 0 < sum(node["tag"] == "iframe" for node in web_snapshot["elements"]) <= 60


@pytest.mark.asyncio
async def test_real_native_poster_controls_are_read_without_changing_default_snapshot():
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.route("https://example.org/**", lambda route: route.abort())
            await page.set_content(
                '<base href="https://example.org/"><article><h1>主题</h1>'
                '<video controls poster="/poster.png" width="960" height="540"></video></article>',
                wait_until="domcontentloaded",
            )
            default_snapshot = await _collect_ui_snapshot(page)
            web_snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")
    assert all("poster" not in node for node in default_snapshot["elements"])
    assert extract_subject_region_facts(web_snapshot).primary_player_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html,kind,players,bodies,inline",
    [
        (
            '<main><article><h1>主题</h1><div style="width:80px;height:60px">'
            '<video src="/sample.mp4" width="80" height="60"></video></div></article></main>',
            "unknown",
            0,
            0,
            0,
        ),
        (
            "<main><div><p>" + "文" * 300 + "</p></div><div><p>" + "章" * 300 + "</p></div></main>",
            "article",
            0,
            1,
            0,
        ),
        (
            "<article><div>" + "文" * 900 + "</div>"
            '<video src="/sample.mp4" width="320" height="180"></video></article>',
            "article",
            0,
            1,
            1,
        ),
        (
            "<main><div>" + "文" * 900 + "</div>"
            '<video src="/sample.mp4" width="320" height="180"></video></main>',
            "article",
            0,
            1,
            1,
        ),
        (
            "<main><nav><div>" + "荐" * 900 + "</div></nav></main>",
            "unknown",
            0,
            0,
            0,
        ),
        (
            "<article><div>"
            + "文" * 900
            + "</div><nav><div>"
            + "导" * 700
            + '</div></nav><section class="recommendations"><div>'
            + "荐" * 700
            + "</div></section></article>",
            "article",
            0,
            1,
            0,
        ),
        (
            '<article id="private-body-marker"><p>' + "private-body-marker" * 60 + "</p></article>",
            "article",
            0,
            1,
            0,
        ),
    ],
    ids=[
        "tiny-media",
        "two-wrappers",
        "article-no-p",
        "main-no-p",
        "navigation-only",
        "article-secondary",
        "audit-private-selector",
    ],
)
async def test_real_subject_review_boundaries(html, kind, players, bodies, inline):
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.route("https://example.org/**", lambda route: route.abort())
            await page.set_content(
                '<base href="https://example.org/">' + html, wait_until="domcontentloaded"
            )
            default_snapshot = await _collect_ui_snapshot(page)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")
    assert all(
        "text_length" not in node and "web_subject_text_length" not in node
        for node in default_snapshot["elements"]
    )
    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_subject_kind == kind
    assert facts.primary_player_count == players
    assert facts.primary_article_body_count == bodies
    assert facts.inline_article_media_count == inline
    if "文" * 900 in html:
        assert facts.primary_article_text_length == 900
    assert "private-body-marker" not in repr((facts.selector_summaries, facts.token_summaries))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "viewport", [{"width": 390, "height": 844}, {"width": 1920, "height": 1080}]
)
@pytest.mark.parametrize(
    "html,page_type,fact",
    [
        (
            '<div id="subject" class="image-gallery">'
            '<img src="/photo.png" width="100" height="100">'
            '<button class="zoom-in">放大</button></div>',
            "content_detail",
            "image_gallery_root_count",
        ),
        (
            '<div id="subject" class="campaign"><button class="claim">领取</button></div>',
            "activity",
            "activity_primary_region_count",
        ),
        (
            '<form id="subject" role="search"><input><button>搜索</button></form>',
            "search_page",
            "search_trigger_count",
        ),
        (
            '<form id="subject"><input type="search"><button>搜索</button></form>',
            "search_page",
            "search_trigger_count",
        ),
        (
            '<div id="subject" class="search-results">'
            '<a href="/one">结果一</a><a href="/two">结果二</a></div>',
            "search_page",
            "query_result_group_count",
        ),
    ],
)
async def test_real_web_collector_establishes_v8_primary_subject(html, page_type, fact, viewport):
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport=viewport)
            await page.set_content(
                '<base href="https://example.org/"><style>'
                "a,input,button {display:block;min-width:120px;min-height:30px}</style>" + html
            )
            legacy = await _collect_ui_snapshot(page)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    # 默认/H5 路径既不新增业务 div/form，也不把原始空 type 改成 HTML 默认值。
    assert any(item["selector"] == "#subject" for item in legacy["elements"]) is (
        'role="search"' in html
    )
    for item in legacy["elements"]:
        if item["tag"] == "button":
            assert item["type"] is None
    structure = web_page_structure_facts(snapshot)
    assert getattr(structure, fact) == 1
    result, _ = await classify(page_type, structure)
    assert result.page_type == page_type


@pytest.mark.asyncio
async def test_real_collector_prioritizes_generic_article_after_dense_link_inventory() -> None:
    """大量链接不得把通用 article.post/.post-content 正文挤出快照上限。"""

    from playwright.async_api import async_playwright

    links = "".join(f'<a href="/item/{index}">链接{index}</a>' for index in range(400))
    article = (
        '<article class="post"><div class="post-content"><p>'
        + "正文" * 180
        + "</p></div></article>"
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(links + article)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    structure = web_page_structure_facts(snapshot)
    result, _ = await classify("content_detail", structure)

    assert structure.article_body_count >= 1
    assert structure.paragraph_text_length >= 300
    assert result.page_type == "content_detail"


@pytest.mark.asyncio
async def test_negative_advertisement_token_does_not_hide_detail_subject() -> None:
    """no-banner/no-ad 等否定样式不能把整篇详情误标为广告区域。"""

    from playwright.async_api import async_playwright

    html = (
        '<body class="theme-dark no-banner no-ad">'
        '<article class="post"><h1 class="post-title">详情标题</h1>'
        '<div class="post-content"><p>'
        + "正文" * 180
        + '</p><div class="dplayer"><video src="/main.mp4" '
        'width="786" height="442"></video></div></div></article></body>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.route("https://example.org/**", lambda route: route.abort())
            await page.set_content('<base href="https://example.org/">' + html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    article = next(item for item in snapshot["elements"] if item["tag"] == "article")
    title = next(item for item in snapshot["elements"] if item["tag"] == "h1")
    video = next(item for item in snapshot["elements"] if item["tag"] == "video")
    assert article["is_advertisement"] is False
    assert title["is_advertisement"] is False
    assert video["is_advertisement"] is False

    facts = extract_subject_region_facts(snapshot)
    assert facts.primary_subject_count == 1
    assert facts.primary_article_body_count == 1
    assert facts.primary_article_text_length >= 360


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "layout,text_length,resource_count,page_type",
    [
        ("embedded", 600, 0, "content_detail"),
        ("embedded_direct", 600, 0, "content_detail"),
        ("embedded_source", 600, 0, "content_detail"),
        ("separate", 119, 1, "video_detail"),
        ("separate", 120, 1, "video_detail"),
        ("separate", 240, 1, "video_detail"),
        ("separate_direct", 120, 1, "video_detail"),
        ("separate_direct_plain", 120, 1, "video_detail"),
        ("separate_nested", 240, 1, "video_detail"),
        ("separate_generic_layout", 119, 1, "video_detail"),
        ("separate_generic_layout", 120, 1, "video_detail"),
        ("separate_generic_layout", 240, 1, "video_detail"),
        ("embedded_article_wrapped", 600, 0, "content_detail"),
    ],
)
async def test_real_body_subtree_does_not_absorb_sibling_player(
    layout,
    text_length,
    resource_count,
    page_type,
):
    from playwright.async_api import async_playwright

    paragraph = "<p>" + "文" * text_length + "</p>"
    video = '<video src="https://example.org/main.mp4" width="640" height="360"></video>'
    if layout in {"embedded_direct", "embedded_source"}:
        if layout == "embedded_direct":
            video = video.replace("<video ", '<video class="video-player" ')
        else:
            video = '<video class="video-player" width="640" height="360">'
            video += '<source src="https://example.org/main.mp4"></video>'
        html = "<main>" + paragraph + video + "</main>"
    elif layout == "embedded":
        html = '<main><div id="body">' + paragraph + video + "</div></main>"
    elif layout == "embedded_article_wrapped":
        html = "<main><article>" + paragraph + '<div id="player">' + video
        html += "</div></article></main>"
    elif layout == "separate_generic_layout":
        video = video.replace("<video ", "<video controls ")
        html = '<main><div id="layout"><div id="player">' + video
        html += "</div>" + paragraph + "</div></main>"
    elif layout in {"separate_direct", "separate_direct_plain"}:
        html = '<main><div id="player" class="video-player">' + video
        html += "</div>" + paragraph + "</main>"
        if layout == "separate_direct_plain":
            html = html.replace(' class="video-player"', "")
    else:
        html = '<main><div id="player" class="video-player">' + video
        html += '</div><aside id="description">' + paragraph + "</aside></main>"
        if layout == "separate_nested":
            html = html.replace("<main>", "<main><div>").replace("</main>", "</div></main>")
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.route("https://example.org/**", lambda route: route.abort())
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    structure = web_page_structure_facts(snapshot)
    assert structure.confirmed_primary_video_resource_count == resource_count
    result, _ = await classify(page_type, structure)
    assert result.page_type == page_type
