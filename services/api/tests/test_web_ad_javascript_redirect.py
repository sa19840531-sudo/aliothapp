"""脚本计算的广告跳转走真实浏览器，同时保留非公网门禁及上下文清理。"""

import pytest

from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
from services.api.app.infrastructure.browser import ad_isolated_page
from services.api.tests.test_web_ad_outbound_security import controlled_transport
from services.api.tests.test_web_ad_outbound_security import local_target as _local_target
from services.api.tests.web_browser_test_support import launch_web_test_browser

ad_local_target = _local_target


@pytest.mark.asyncio
@pytest.mark.parametrize('private', [False, True])
async def test_computed_javascript_redirect_is_rendered_or_safely_denied(
    monkeypatch, ad_local_target, private,
):
    from playwright.async_api import async_playwright

    transport, requests = controlled_transport(monkeypatch, ad_local_target)
    transport.follow_client_redirects = False
    events = []
    transport.diagnostic_events = events
    monkeypatch.setattr(ad_isolated_page, 'AdSafeTransport', lambda **kwargs: transport)
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            async def capture():
                async with ad_isolated_page.isolated_ad_page(
                    browser=browser,
                    url='http://public.example/computed-' + ('private' if private else 'redirect'),
                    source_url='http://source.example/', timeout_ms=5000,
                    enable_javascript=True,
                    diagnostic_events=events,
                ) as (landing, _):
                    assert landing.url == 'http://landing.example/landing'
                    assert 'controlled landing' in await landing.content()
                    assert await landing.screenshot()
                    assert await browser.contexts[0].pages[0].evaluate('window.executed') is True
            if private:
                with pytest.raises(AdOutboundDenied):
                    await capture()
                assert requests == ['/computed-private']
                assert any(item['denial_reason'] and item['request_scope'] == 'main_document'
                           and '127.0.0.1' in item['url'] for item in events)
            else:
                await capture()
                assert requests == ['/computed-redirect', '/landing']
            assert browser.contexts == []
        finally:
            await browser.close()
