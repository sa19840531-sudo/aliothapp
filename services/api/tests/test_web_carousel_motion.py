"""Web 采证暂停组件移动，其他脚本与原暂停状态保持不变。"""

import pytest

from services.api.app.infrastructure.browser.web_carousel_motion import (
    pause_web_carousels,
    restore_web_carousels,
)
from services.api.tests.web_browser_test_support import launch_web_test_browser


@pytest.mark.asyncio
@pytest.mark.parametrize('running', [True, False])
async def test_component_guard_restores_methods_and_original_autoplay_state(running):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<div class="owl-carousel">轮播</div>')
            await page.evaluate('''running => {
                window.moves = 0; window.ticks = 0; window.events = [];
                const proto = {next() { window.moves++; }, prev() {}, to() {}};
                window.owl = Object.assign(Object.create(proto), {
                    _plugins: {autoplay: {_paused: !running}},
                    is: () => running
                });
                window.jQuery = () => ({data: () => owl, trigger: name => events.push(name)});
                window.timer = setInterval(() => { ticks++; owl.next(); }, 10);
            }''', running)
            result = await pause_web_carousels(page)
            assert result['owl_motion_guarded'] == 3
            assert result['owl_paused'] == int(running)
            before = await page.evaluate('({moves,ticks})')
            await page.wait_for_function('previous => ticks > previous + 3', arg=before['ticks'])
            assert await page.evaluate('moves') == before['moves']
            restored = await restore_web_carousels(page)
            assert restored['owl_motion_suppressed'] > 0
            assert restored['owl_restored'] == int(running)
            assert not await page.evaluate('Object.hasOwn(owl,"next")')
            await page.wait_for_function('previous => moves > previous', arg=before['moves'])
            assert await page.evaluate('events') == (
                ['stop.owl.autoplay', 'play.owl.autoplay'] if running else []
            )
        finally:
            await browser.close()
