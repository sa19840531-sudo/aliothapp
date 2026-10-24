"""既有 Snapshot 真实浏览器内的原生结构 census；只有中性本地 HTML。"""

import os

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"), reason="需显式本机Chrome")
async def test_existing_web_snapshot_collects_local_census_without_changing_h5():
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"], headless=True
        )
        try:
            page = await browser.new_page()
            for html, expected in [
                ("<main>正文</main>", "local_absent"),
                ("<video controls></video>", "present"),
                ("<canvas></canvas>", "unknown"),
                ('<iframe srcdoc="正文"></iframe>', "unknown"),
                ("<x-player></x-player>", "unknown"),
                ("<div></div>" * 10001, "unknown"),
            ]:
                await page.set_content(html)
                snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
                assert snapshot["web_native_structure"]["status"] == expected
                assert (
                    snapshot["web_native_structure"]["document_time_origin_ms"]
                    == snapshot["document"]["time_origin_ms"]
                )
            await page.set_content("<main>普通H5</main>")
            ordinary = await _collect_ui_snapshot(page)
            assert "web_native_structure" not in ordinary
        finally:
            await browser.close()
