"""首页账户图标资源缺失时的 Web 入口定位回归。"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.api.app.application.web_account_entry import (
    active_auth_mode,
    unique_account_entry,
    unique_authenticated_account_entry,
    unique_register_entry,
    visible_login_form,
)


async def _launch_browser(playwright):
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if not executable:
        local = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        executable = str(local) if local.exists() else None
    return await playwright.chromium.launch(headless=True, executable_path=executable)


@pytest.mark.asyncio
async def test_broken_account_icon_is_unique_visible_entry():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(
                '<header><a class="nav-right-icon search-btn" href="/search">'
                '<img src="data:image/png;base64,broken"></a>'
                '<a class="nav-right-icon account-btn" href="#account" '
                'style="display:block;position:absolute;left:1045px;top:28px;'
                'width:24px;height:24px">'
                '<img class="account-default-icon" '
                'src="data:image/png;base64,broken"></a></header>'
            )
            candidate = await unique_account_entry(SimpleNamespace(page=page))
            assert candidate is not None
            assert candidate["basis"] == "broken_account_icon"
            assert 0.54 < candidate["box"][0] < candidate["box"][2] < 0.56
            await page.locator(".account-btn").evaluate("element => element.remove()")
            assert await unique_account_entry(SimpleNamespace(page=page)) is None
        finally:
            await chromium.close()


@pytest.mark.asyncio
async def test_multiple_or_external_account_candidates_are_not_guessed():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(
                '<a class="account-btn" href="#first" style="position:absolute;'
                'left:100px;top:50px;width:40px;height:40px">'
                '<img src="data:image/png;base64,broken"></a>'
                '<a class="profile-btn" href="#second" style="position:absolute;'
                'left:200px;top:50px;width:40px;height:40px">'
                '<img src="data:image/png;base64,broken"></a>'
            )
            assert await unique_account_entry(SimpleNamespace(page=page)) is None
            await page.locator(".profile-btn").evaluate("element => element.remove()")
            await page.locator(".account-btn").evaluate(
                "element => element.href = 'https://other.example/account'"
            )
            assert await unique_account_entry(SimpleNamespace(page=page)) is None
        finally:
            await chromium.close()


@pytest.mark.asyncio
async def test_authenticated_account_entry_finds_unique_global_my_navigation():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.route(
                "https://example.test/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=(
                        '<aside><nav><a class="global-my" href="/my" '
                        'style="position:fixed;left:24px;top:220px;width:120px;'
                        'height:40px">我的</a></nav></aside>'
                        '<main><a href="/my">我的</a></main>'
                    ),
                ),
            )
            await page.goto("https://example.test/")
            browser = SimpleNamespace(page=page)
            found = await unique_authenticated_account_entry(browser)
            assert found is not None
            assert found["box"][0] < 0.1
            await page.locator("nav").evaluate(
                "e => e.insertAdjacentHTML('beforeend', "
                "'<a href=\"/my\" style=\"position:fixed;left:24px;top:280px;"
                "width:120px;height:40px\">我的</a>')"
            )
            assert await unique_authenticated_account_entry(browser) is None
            await page.locator("nav a").last.evaluate("e => e.remove()")
            await page.locator(".global-my").evaluate(
                "e => e.href='https://other.example/my'"
            )
            assert await unique_authenticated_account_entry(browser) is None
            await page.locator(".global-my").evaluate("e => e.href='/my'")
            await page.locator("body").evaluate(
                "e => e.insertAdjacentHTML('beforeend', '<div style=\"position:fixed;"
                "left:24px;top:220px;width:120px;height:40px;z-index:10;"
                "background:white\"></div>')"
            )
            assert await unique_authenticated_account_entry(browser) is None
        finally:
            await chromium.close()


@pytest.mark.asyncio
async def test_traditional_login_entry_and_current_register_tab_are_grounded():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(
                '<a class="bind_login" style="position:absolute;left:1567px;top:15px;'
                'width:68px;height:35px">登入</a>'
            )
            browser = SimpleNamespace(page=page)
            entry = await unique_account_entry(browser)
            assert entry is not None
            assert entry["basis"] == "visible_account_text"
            await page.set_content(
                '<div role="tablist" style="position:fixed;left:780px;top:450px">'
                '<button role="tab" class="is-active" style="width:174px;height:34px">'
                '登录</button><button role="tab" style="width:174px;height:34px">'
                '注册</button></div><div style="position:fixed;left:1150px;top:450px;'
                'width:200px;height:200px" onclick="document.body.dataset.backdrop=1"></div>'
            )
            register = await unique_register_entry(browser)
            assert register is not None
            assert 0.49 < register["box"][0] < register["box"][2] < 0.59
            assert await active_auth_mode(browser) == "login"
            await page.locator('[role="tab"]').first.evaluate(
                "e => e.classList.remove('is-active')"
            )
            await page.locator('[role="tab"]').last.evaluate(
                "e => e.classList.add('is-active')"
            )
            assert await active_auth_mode(browser) == "register"
        finally:
            await chromium.close()


@pytest.mark.asyncio
async def test_adjacent_login_register_links_use_login_as_account_entry():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(
                '<header><div style="position:absolute;left:1544px;top:15px">'
                '<button style="width:25px;height:34px">登录</button>'
                '<button style="width:25px;height:34px">注册</button>'
                '</div></header>'
            )
            candidate = await unique_account_entry(SimpleNamespace(page=page))
            assert candidate is not None
            assert candidate["basis"] == "adjacent_login_register_entry"
            assert 0.80 < candidate["box"][0] < candidate["box"][2] < 0.82
            register = await unique_register_entry(SimpleNamespace(page=page))
            assert register is not None
            assert 0.815 < register["box"][0] < register["box"][2] < 0.84
        finally:
            await chromium.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("layer_class", ["modal", "layui-layer layui-layer-page"])
async def test_modal_register_link_uses_structural_semantics_when_text_is_corrupt(layer_class):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(
                f'<div class="{layer_class}" style="position:fixed;inset:0;z-index:99">'
                '<input type="password" style="position:absolute;left:800px;top:500px;'
                'width:300px;height:40px">'
                '<a class="bind_register" style="position:absolute;left:1096px;'
                'top:694px;width:218px;height:23px">garbled</a></div>'
            )
            candidate = await unique_register_entry(SimpleNamespace(page=page))
            assert candidate is not None
            assert candidate["basis"] == "modal_register_control"
            assert 0.57 < candidate["box"][0] < candidate["box"][2] < 0.69
        finally:
            await chromium.close()


@pytest.mark.asyncio
async def test_tabless_login_modal_is_distinct_from_register_modal():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1550, "height": 1080})
        try:
            browser = SimpleNamespace(page=page)
            await page.set_content(
                '<div style="position:fixed;left:400px;top:300px;'
                'width:700px;height:450px;background:white">'
                '<input placeholder="账号" style="position:absolute;left:200px;top:140px;'
                'width:300px;height:40px">'
                '<div style="position:absolute;left:200px;top:200px">'
                '<input type="password" placeholder="密码" '
                'style="width:300px;height:40px"></div>'
                '<button style="position:absolute;left:200px;top:300px;'
                'width:300px;height:40px">登入</button>'
                '<a style="position:absolute;left:380px;top:370px">注册只需30秒</a>'
                '</div>'
            )
            assert await active_auth_mode(browser) is None
            assert await visible_login_form(browser) is True
            await page.locator("button").evaluate("e => e.innerText = '注册'")
            assert await visible_login_form(browser) is False
        finally:
            await chromium.close()


@pytest.mark.asyncio
async def test_descriptive_register_link_in_login_modal_is_unique_entry():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        chromium = await _launch_browser(playwright)
        page = await chromium.new_page(viewport={"width": 1550, "height": 1080})
        try:
            await page.set_content(
                '<div class="auth-modal" style="position:fixed;left:565px;top:350px;'
                'width:420px;height:380px;background:#222">'
                '<h2>登录账号</h2><form><input placeholder="请输入账号">'
                '<input type="password" placeholder="请输入密码">'
                '<a class="switch" style="display:inline-block;width:95px;height:24px">'
                '注册只需30秒</a><button type="submit">登录账号</button>'
                '</form></div>'
            )
            browser = SimpleNamespace(page=page)
            candidate = await unique_register_entry(browser)
            assert candidate is not None
            assert candidate["basis"] == "modal_register_control"
            actual = await page.locator("a.switch").bounding_box()
            assert candidate["box"] == pytest.approx([
                actual["x"] / 1550, actual["y"] / 1080,
                (actual["x"] + actual["width"]) / 1550,
                (actual["y"] + actual["height"]) / 1080,
            ])
            await page.locator(".auth-modal").evaluate(
                "e => e.insertAdjacentHTML('beforeend', "
                "'<a style=\"display:inline-block;width:95px;height:24px\">注册会员</a>')"
            )
            assert await unique_register_entry(browser) is None
            await page.locator(".switch").evaluate("e => e.remove()")
            assert await unique_register_entry(browser) is not None
        finally:
            await chromium.close()
