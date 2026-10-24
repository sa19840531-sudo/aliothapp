"""真实 Chrome 媒体终态采样，失败资源不能被网络空闲伪装成成功。"""

import asyncio
import os

import pytest

from services.api.app.infrastructure.browser.page_capture import _read_page_readiness


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"), reason="须显式配置受控 Chrome")
@pytest.mark.parametrize(
    ("outcome", "size", "position", "expected"),
    [
        ("loaded", 640, "", "ready"),
        ("failed", 640, "", "critical_failed"),
        ("failed", 24, "", "ready"),
        ("pending", 640, "", "pending"),
        ("failed", 640, "margin-top:2000px", "ready"),
    ],
)
async def test_viewport_media_persists_decode_and_terminal_resource_facts(
    outcome: str,
    size: int,
    position: str,
    expected: str,
) -> None:
    from playwright.async_api import async_playwright

    release = asyncio.Event()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"],
            headless=True,
        )
        try:
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})

            async def resource(route):
                if outcome == "pending":
                    await release.wait()
                if outcome == "loaded":
                    await route.fulfill(
                        content_type="image/svg+xml",
                        body=(
                            '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">'
                            '<rect width="640" height="480" fill="blue"/></svg>'
                        ),
                    )
                else:
                    await route.abort()

            await page.route("https://images.example.test/**", resource)
            await page.set_content(
                f'<main><h1>公开内容</h1><img style="{position}" width="{size}" '
                f'height="{size}" src="https://images.example.test/image.svg?token=test-only">'
                "</main>",
                wait_until="domcontentloaded",
            )
            if outcome != "pending":
                await page.wait_for_function("document.images[0].complete")
            readiness = await _read_page_readiness(page)
            media = readiness["viewport_media"]
            assert media["state"] == expected
            if position:
                assert media["items"] == []
            else:
                item = media["items"][0]
                assert item["complete"] is (outcome != "pending")
                assert item["natural_width"] == (640 if outcome == "loaded" else 0)
                assert item["resource_state"] == outcome
                assert item["current_src"] == "https://images.example.test/image.svg"
                assert "test-only" not in str(media)
                assert len(item["current_src_sha256"]) == 71
                if outcome == "failed" and size == 24:
                    assert media["warnings"] == ["noncritical_media_failed"]
        finally:
            release.set()
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"), reason="须显式配置受控 Chrome")
@pytest.mark.parametrize(
    "size,semantic_required,expected", [(96, False, True), (640, False, False), (96, True, False)]
)
async def test_web_stability_excludes_empty_decoration_but_keeps_required_media(
    size, semantic_required, expected
):
    from playwright.async_api import async_playwright

    from services.api.app.application.web_readiness_contracts import WebReadinessProfile
    from services.api.app.infrastructure.browser.page_capture import _wait_for_web_page_settle

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"], headless=True
        )
        try:
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            required = 'aria-required="true"' if semantic_required else ""
            await page.set_content(
                f'<main><h1>公开内容</h1><img width="{size}" height="{size}" {required}></main>'
            )
            diagnostics = {}
            settled = await _wait_for_web_page_settle(
                page=page,
                network_inflight={},
                max_wait_ms=1600,
                poll_interval_ms=50,
                profile=WebReadinessProfile(minimum_dom_quiet_ms=50, minimum_window_ms=100),
                diagnostics=diagnostics,
            )
            assert settled is expected
            media = diagnostics["readiness"]["viewport_media"]
            assert media["necessary_count"] == (0 if expected else 1)
            assert media["items"][0]["source_present"] is False
            assert media["items"][0]["semantic_required"] is semantic_required
            if expected:
                assert media["warnings"] == ["noncritical_media_unknown"]
                assert diagnostics["readiness"]["necessary_media_ready"] is False
            else:
                assert media["state"] == "unknown"
        finally:
            await browser.close()
