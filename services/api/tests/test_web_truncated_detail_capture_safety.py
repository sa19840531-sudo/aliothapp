"""真实 Chromium 同帧媒体摘要需保留被元素裁剪后无法重建的安全事实。"""

import base64

import pytest

from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("player_control", "advertisement", "popup"))
async def test_video_media_summary_preserves_same_frame_context_and_hit_test(case: str):
    from playwright.async_api import async_playwright

    player_class = "xgplayer advertisement" if case == "advertisement" else "xgplayer"
    control = (
        '<button class="xgplayer-play" style="position:absolute;left:50%;top:50%;'
        'transform:translate(-50%,-50%);width:180px;height:180px;z-index:2">▶</button>'
        if case == "player_control" else ""
    )
    popup = (
        '<div role="dialog" aria-modal="true" style="position:fixed;left:660px;'
        'top:350px;width:600px;height:400px;z-index:100;background:white">弹窗</div>'
        if case == "popup" else ""
    )
    html = (
        '<main><div id="mse" class="' + player_class
        + '" style="position:relative;margin:200px auto 0;width:1200px;height:680px">'
        '<video id="video" style="display:block;width:100%;height:100%"></video>'
        + control + "</div></main>" + popup
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    video = next(item for item in snapshot["media"] if item["tag"] == "video")
    assert video["selector"] == "#video"
    assert video["is_advertisement"] is (case == "advertisement")
    assert video["within_web_navigation"] is False
    assert video["within_repeated_structure"] is False
    assert video["hit_test"]["center_obstruction_layer"] is (case == "popup")
    assert video["hit_test"]["center_within_player_surface"] is (
        case != "popup"
    )
    if case == "player_control":
        assert video["hit_test"]["center_hits_target"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "transparent_ad_click_overlay",
        "advertisement_video",
        "opaque_ad_overlay",
        "box_shadow_overlay",
        "backdrop_blur_overlay",
        "pseudo_shadow_overlay",
        "border_overlay",
        "outline_overlay",
        "filter_overlay",
        "mix_blend_overlay",
        "element_content_image_overlay",
    ),
)
async def test_player_ad_config_is_separate_from_video_ad_assignment(case: str):
    from playwright.async_api import async_playwright

    wrapper_class = "advertisement" if case == "advertisement_video" else "content"
    background = "black" if case == "opaque_ad_overlay" else "transparent"
    effects = {
        "box_shadow_overlay": "box-shadow:inset 0 0 0 9999px black;",
        "backdrop_blur_overlay": "backdrop-filter:blur(12px);",
        "border_overlay": "border:30px solid black;",
        "outline_overlay": "outline:30px solid black;",
        "filter_overlay": "filter:blur(12px);",
        "mix_blend_overlay": "mix-blend-mode:multiply;",
        "element_content_image_overlay": (
            "content:url(data:image/svg+xml;base64,"
            + base64.b64encode(
                b'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="680">'
                b'<rect width="1200" height="680" fill="red"/></svg>'
            ).decode("ascii")
            + ");"
        ),
    }
    pseudo = (
        '<style>#mse a.dx-ad-plugin::before{content:"";position:absolute;inset:0;'
        'box-shadow:inset 0 0 0 9999px black;}</style>'
        if case == "pseudo_shadow_overlay" else ""
    )
    html = (
        pseudo
        + f'<main><div class="{wrapper_class}"><div id="mse" class="xgplayer" '
        'data-video="https://example.test/main.mp4" data-video_id="42" '
        'data-ads_url="https://ads.example.test/landing" '
        'style="position:relative;margin:200px auto 0;width:1200px;height:680px">'
        '<video id="video" style="display:block;width:100%;height:100%"></video>'
        '<a class="dx-ad-plugin" href="https://ads.example.test/landing" '
        f'style="position:absolute;inset:0;z-index:2;background:{background};'
        f'{effects.get(case, "")}'
        'pointer-events:auto"></a></div></div></main>'
    )
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    video = next(item for item in snapshot["media"] if item["tag"] == "video")
    assert video["selector"] == "#video"
    assert video["is_advertisement"] is (case == "advertisement_video")
    assert video["hit_test"]["center_ad_click_overlay"] is (
        case in {"transparent_ad_click_overlay", "advertisement_video"}
    )
    assert video["hit_test"]["center_pointer_interception"] is True
    assert video["hit_test"]["center_hits_target"] is False
