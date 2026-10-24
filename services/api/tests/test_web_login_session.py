"""使用真实 Chromium 验证 Web 会话内登录表单与秘密清理。"""

import asyncio
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from services.api.app.application.web_account_preflight import _safe_dom_close_target
from services.api.app.application.web_login_allowlist import parse_web_login_allowlist
from services.api.app.application.web_login_auth_origins import parse_web_login_auth_origins
from services.api.app.infrastructure.browser.web_login_session import (
    WebLoginFrame,
    WebLoginSessionBrowser,
    WebLoginSessionError,
    _center_popup_close_target,
)


@pytest.mark.asyncio
async def test_account_dom_matches_capture_and_redacts_secret_fields():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        chromium = await playwright.chromium.launch(**options)
        context = await chromium.new_context()
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    "<main><span>tester@example.test</span>"
                    "<a href='/wallet?token=private'>钱包</a>"
                    "<a href='https://other.test/out'>外链</a>"
                    "<form><input value='private-password'></form>"
                    "<a id='guest-entry' href='/login'>登录/注册</a>"
                    "<script>window.secret='private-password'</script></main>"
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._private_identifiers["login"] = "tester@example.test"
        port._last_scrub_status["login"] = "clear"
        frame = await port.capture("login", "https://example.test", include_dom=True)
        assert frame.png.startswith(b"\x89PNG\r\n\x1a\n")
        assert frame.url == page.url
        assert frame.dom is not None
        assert "[REDACTED_IDENTIFIER]" in frame.dom
        assert 'href="/wallet"' in frame.dom
        assert "private-password" not in frame.dom
        assert "token=private" not in frame.dom
        assert "https://other.test" not in frame.dom
        assert "<form" not in frame.dom
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "guest"
        await page.evaluate("""() => {
          document.querySelector('#guest-entry').remove();
          const exit=document.createElement('a');
          exit.href='/logout';exit.textContent='登出';document.body.append(exit);
        }""")
        fresh = await port.capture("login", "https://example.test", include_dom=True)
        assert await port.account_auth_state(
            "login", "https://example.test", fresh
        ) == "authenticated"
        await page.evaluate("document.querySelector('a[href=\"/logout\"]').textContent='退出登录'")
        fresh = await port.capture("login", "https://example.test", include_dom=True)
        assert await port.account_auth_state(
            "login", "https://example.test", fresh
        ) == "authenticated"
        await chromium.close()


@pytest.mark.asyncio
async def test_account_auth_state_detects_guest_spans_in_account_drawer():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    "<aside class='user-info' style='position:fixed;right:0;top:0;"
                    "width:300px;height:400px;background:white'>"
                    "<span class='login-btn'>登录</span> 或 "
                    "<span class='register-btn'>注册</span></aside>"
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "guest"
        await browser.close()


@pytest.mark.asyncio
async def test_account_auth_state_confirms_private_drawer_without_logout_control():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    "<div class='sidemenu__list' style='position:fixed;right:0;top:0;"
                    "width:300px;height:500px;background:white'>"
                    "<div class='user-info-nickname'><div class='nickname'>用户</div></div>"
                    "<a href='/usercenter/deposit/' style='display:block;padding:12px'>"
                    "<span>金币充值</span><div class='count'>当前余额：0</div>"
                    "<span>去充值</span></a>"
                    "<a href='/user/message' style='display:block;padding:12px'>消息中心</a>"
                    "</div>"
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "authenticated"
        await page.evaluate("""() => {
          document.querySelector('.sidemenu__list').innerHTML =
            '<span class="login-btn">登录</span> 或 '
            + '<span class="register-btn">注册</span>';
        }""")
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "guest"
        await page.evaluate("""() => {
          document.querySelector('.sidemenu__list').innerHTML =
            '<div class="nickname">用户</div>'
            + '<a href="/usercenter/deposit/">去充值</a>';
        }""")
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "unknown"
        await browser.close()


@pytest.mark.asyncio
async def test_account_auth_state_accepts_visible_account_logout_controls():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8",
                body=("<aside class='user_menu' style='position:fixed;right:0;top:0;"
                      "width:300px;height:200px;background:white'>"
                      "<div class='logout_btn'>退出登录</div></aside>"),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/wallet")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "authenticated"
        await page.evaluate("""() => {
          document.querySelector('.logout_btn').outerHTML =
            '<a class="logout_btn">退出</a>';
        }""")
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "authenticated"
        await page.evaluate("""() => {
          document.querySelector('.user_menu').remove();
          document.body.innerHTML='<article>退出</article>';
        }""")
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "unknown"
        await browser.close()


@pytest.mark.asyncio
async def test_account_auth_state_confirms_own_profile_without_logout_control():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    "<header><a href='/user/profile'>member42</a></header>"
                    "<main><h1>我的档案</h1><h2>账号信息</h2>"
                    "<label>用户名</label><span>member42</span>"
                    "<a href='/account/change-password'>修改密码</a></main>"
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/user/profile")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._private_identifiers["login"] = "member42"
        frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "authenticated"
        port._private_identifiers["login"] = "other-member"
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "unknown"
        port._private_identifiers["login"] = "member42"
        await page.evaluate("""() => {
          document.querySelector('header').remove();
          const mention=document.createElement('article');
          mention.textContent='member42';document.body.append(mention);
        }""")
        mention_frame = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", mention_frame
        ) == "unknown"
        await browser.close()


@pytest.mark.asyncio
async def test_account_auth_state_confirms_bound_nickname_on_selected_dashboard():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    '<header><a href="/user/profile">个人设置</a>'
                    '<div style="display:none"><a href="/logout">退出登录</a></div>'
                    '</header><main><ul class="tabs">'
                    '<li class="is-active">用户中心</li><li>收藏</li></ul>'
                    '<section class="user-card"><span class="user-nickname">'
                    'dy@member42</span><div><strong>0</strong><div>当前积分</div></div>'
                    '</section></main>'
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._private_identifiers["login"] = "member42"
        frame = await port.capture("login", "https://example.test", include_dom=True)
        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "authenticated"
        assert "member42" not in frame.dom
        assert (255, 0, 255) in Image.open(io.BytesIO(frame.png)).convert(
            "RGB"
        ).get_flattened_data()
        await page.locator(".user-nickname").evaluate("e => e.textContent='dy@other'")
        wrong = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", wrong
        ) == "unknown"
        await page.locator(".user-nickname").evaluate("e => e.textContent='dy@member42'")
        await page.locator("header").evaluate(
            "e => e.insertAdjacentHTML('beforeend', '<a href=\"/login\">登录</a>')"
        )
        guest = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", guest
        ) == "guest"
        await page.locator('a[href="/login"]').evaluate("e => e.remove()")
        await page.locator("main").evaluate("e => e.remove()")
        hidden_logout_only = await port.capture("login", "https://example.test")
        assert await port.account_auth_state(
            "login", "https://example.test", hidden_logout_only
        ) == "unknown"
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_link_visible", (False, True))
async def test_account_auth_state_rejects_public_article_with_scattered_markers(
    account_link_visible: bool,
):
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        account_link_style = "" if account_link_visible else "display:none"
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    f'<header><a href="/my" style="{account_link_style}">我的</a>'
                    '</header><main><ul><li class="is-active">用户中心</li></ul>'
                    '<article><span class="user-nickname">dy@member42</span>'
                    '<div><strong>0</strong><span>当前积分</span></div>'
                    '</article></main>'
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._private_identifiers["login"] = "member42"
        frame = await port.capture("login", "https://example.test")

        assert await port.account_auth_state(
            "login", "https://example.test", frame
        ) == "unknown"
        await browser.close()


@pytest.mark.asyncio
async def test_session_evidence_ignores_sdk_sid_but_keeps_auth_token():
    class Context:
        async def cookies(self, origin):
            return [
                {"name": "__web_sdk_sid_v1", "value": "instrumentation"},
                {"name": "user_token_v1", "value": "authenticated"},
                {"name": "sdk_auth_token", "value": "also-authenticated"},
            ]

    page = SimpleNamespace(url="https://example.test/")
    driver = SimpleNamespace(
        _sessions={"login": SimpleNamespace(page=page, context=Context())},
        _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
    )
    port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
    both = await port.session_evidence("login", "https://example.test")
    assert len(both["auth_cookie_signatures"]) == 2


@pytest.mark.asyncio
async def test_capture_rejects_same_document_url_change_between_image_and_dom():
    class ChangingPage:
        url = "https://example.test/"
        frames = ()

        async def evaluate(self, script, *args):
            if "cloneNode" in script:
                return "<body>账户</body>"
            return "document-1:https://example.test"

        async def screenshot(self, **kwargs):
            self.url = "https://example.test/account"
            return b"\x89PNG\r\n\x1a\nimage"

    page = ChangingPage()
    driver = SimpleNamespace(
        _sessions={"login": SimpleNamespace(page=page)},
        _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
    )
    port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
    with pytest.raises(WebLoginSessionError) as error:
        await port.capture("login", "https://example.test", include_dom=True)
    assert error.value.reason_code == "WEB_LOGIN_DOCUMENT_CHANGED"


@pytest.mark.asyncio
async def test_post_login_fresh_document_and_recharge_navigation_stay_isolated():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        chromium = await playwright.chromium.launch(**options)
        context = await chromium.new_context()
        account_html = (
            "<a href='/wallet'>\u94b1\u5305</a>" * 13 + "<a href='/recharge'>\u53bb\u5145\u503c</a>"
        )

        async def fulfill(route):
            body = (
                "<main><h1>充值页</h1><section>充值金额</section>"
                "<section>支付方式</section></main>"
                if route.request.url.endswith("/recharge") else account_html
            )
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page, context=context)
        driver = SimpleNamespace(
            _sessions={}, _session_locks={}, _capture_semaphore=asyncio.Semaphore(2)
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._isolated_sessions["web:1:login"] = session
        port._last_scrub_status["web:1:login"] = "residual_markup"
        assert await port.prepare_authenticated_view("web:1:login", "https://example.test")
        frame = await port.capture("web:1:login", "https://example.test")
        actions = await port.account_actions("web:1:login", "https://example.test", frame, ())
        assert {action["kind"] for action in actions} == {"wallet", "recharge"}
        recharge = next(action for action in actions if action["kind"] == "recharge")
        assert await port.click_account_action(
            "web:1:login", "https://example.test", frame, recharge
        )
        assert page.is_closed()
        assert session.page.url == "https://example.test/recharge"
        recharge_frame = await port.capture("web:1:login", "https://example.test")
        assert await port.confirm_recharge_page(
            "web:1:login", "https://example.test", recharge_frame, ()
        )
        old_account_page = session.page
        assert await port.prepare_authenticated_view("web:1:login", "https://example.test")
        assert old_account_page.is_closed()
        assert session.page.url == "https://example.test/"
        await chromium.close()


@pytest.mark.asyncio
async def test_image_wallet_and_topup_popups_count_as_same_url_navigation():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        body = (
            "<a id='wallet-link' href='#' style='display:block;width:120px;height:50px'>"
            "<img alt='钱包' width='30' height='30'></a>"
            "<div id='wallet-popup' role='dialog' style='display:none;position:fixed;"
            "left:100px;top:100px;width:300px;height:250px;background:white'>"
            "<h2>我的钱包</h2><div>当前余额：0</div>"
            "<a id='topup-link' href='#' style='display:block;width:100px;height:40px'>"
            "去充值</a></div>"
            "<div id='topup-popup' role='dialog' style='display:none;position:fixed;"
            "left:420px;top:100px;width:300px;height:250px;background:white'>"
            "<h2>充值</h2><div>充值金额</div><div>支付方式</div></div>"
            "<script>document.querySelector('#wallet-link').onclick=e=>{"
            "e.preventDefault();document.querySelector('#wallet-popup').style.display='block'"
            "};document.querySelector('#topup-link').onclick=e=>{"
            "e.preventDefault();document.querySelector('#topup-popup').style.display='block'"
            "}</script>"
        )
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=body
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page, context=context)
        driver = SimpleNamespace(
            _sessions={}, _session_locks={}, _capture_semaphore=asyncio.Semaphore(2)
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._isolated_sessions["login"] = session
        frame = await port.capture("login", "https://example.test")
        wallet = next(action for action in await port.account_actions(
            "login", "https://example.test", frame, ()
        ) if action["kind"] == "wallet")
        assert await port.click_account_action("login", "https://example.test", frame, wallet)
        wallet_frame = await port.capture("login", "https://example.test")
        assert await port.confirm_wallet_page(
            "login", "https://example.test", wallet_frame, ()
        )
        topup = next(action for action in await port.account_actions(
            "login", "https://example.test", wallet_frame, ()
        ) if action["kind"] == "recharge")
        assert await port.click_account_action(
            "login", "https://example.test", wallet_frame, topup
        )
        topup_frame = await port.capture("login", "https://example.test")
        assert await port.confirm_recharge_page(
            "login", "https://example.test", topup_frame, ()
        )
        await browser.close()


@pytest.mark.asyncio
async def test_delayed_account_link_navigation_is_confirmed_before_reporting_failure():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})

        async def fulfill(route):
            if route.request.url.endswith("/wallet"):
                body = "<main><h1>我的钱包</h1><p>余额：0</p></main>"
            else:
                body = (
                    "<a href='/wallet' style='display:block;width:120px;height:50px' "
                    "onclick=\"event.preventDefault();setTimeout(()=>"
                    "location.href='/wallet',1100)\">钱包</a>"
                )
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page, context=context)
        driver = SimpleNamespace(
            _sessions={}, _session_locks={}, _capture_semaphore=asyncio.Semaphore(2)
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._isolated_sessions["login"] = session
        frame = await port.capture("login", "https://example.test")
        wallet = next(action for action in await port.account_actions(
            "login", "https://example.test", frame, ()
        ) if action["kind"] == "wallet")
        assert await port.click_account_action("login", "https://example.test", frame, wallet)
        assert session.page.url == "https://example.test/wallet"
        await browser.close()


@pytest.mark.asyncio
async def test_offscreen_recharge_is_revealed_before_visible_wallet():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        chromium = await playwright.chromium.launch(**options)
        context = await chromium.new_context(viewport={"width": 1000, "height": 700})
        html = (
            "<a href='/wallet'>钱包</a><div style='height:1600px'></div>"
            "<a href='/recharge'><img alt='去充值' width='120' height='40' "
            "src='data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='></a>"
        )
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page, context=context)
        driver = SimpleNamespace(
            _sessions={"login": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._last_scrub_status["login"] = "clear"
        frame = await port.capture("login", "https://example.test")
        initial = await port.account_actions("login", "https://example.test", frame, ())
        assert {item["kind"] for item in initial} == {"wallet"}
        assert (
            await port.reveal_account_action("login", "https://example.test", frame, "recharge")
            == "revealed"
        )
        scrolled = await port.capture("login", "https://example.test")
        actions = await port.account_actions("login", "https://example.test", scrolled, ())
        assert {item["kind"] for item in actions} == {"recharge"}
        await chromium.close()


@pytest.mark.asyncio
async def test_image_only_recharge_anchor_can_use_ocr_hit():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        chromium = await playwright.chromium.launch(**options)
        context = await chromium.new_context(viewport={"width": 1000, "height": 700})
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<a href='/recharge' style='display:block;width:120px;height:60px'>"
                "<img width='120' height='60' "
                "src='data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='></a>",
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page, context=context)
        driver = SimpleNamespace(
            _sessions={"login": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._last_scrub_status["login"] = "clear"
        frame = await port.capture("login", "https://example.test")
        hits = ({"text": "去充值", "box": [0.01, 0.01, 0.12, 0.09]},)
        actions = await port.account_actions("login", "https://example.test", frame, hits)
        assert len(actions) == 1
        assert actions[0]["kind"] == "recharge"
        await chromium.close()


@pytest.mark.asyncio
async def test_avatar_popup_deduplicates_profile_destination_and_finds_href_only_recharge():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        chromium = await playwright.chromium.launch(**options)
        context = await chromium.new_context(viewport={"width": 1000, "height": 700})

        async def fulfill(route):
            if route.request.url.endswith("/my/index"):
                body = (
                    "<main><h1>我的主页</h1>"
                    "<a href='/recharge' style='display:block;width:120px;height:60px'>"
                    "<img width='120' height='60' "
                    "src='data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='></a>"
                    "</main>"
                )
            else:
                body = (
                    "<a href='/my/history'>观看历史</a>"
                    "<a href='/my/index'>我的主页</a>"
                    "<a href='/my/index'>我的主页</a>"
                    "<a href='/my/watchlater'>稍后再看</a>"
                )
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page, context=context)
        driver = SimpleNamespace(
            _sessions={"login": session}, _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        actions = await port.account_actions("login", "https://example.test", frame, ())
        profiles = [action for action in actions if action["kind"] == "profile"]
        assert len(profiles) == 1
        assert await port.click_account_action(
            "login", "https://example.test", frame, profiles[0]
        )
        profile_frame = await port.capture("login", "https://example.test")
        profile_actions = await port.account_actions(
            "login", "https://example.test", profile_frame, ()
        )
        assert [action["kind"] for action in profile_actions] == ["recharge"]
        await chromium.close()


def _executable():
    configured = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if configured:
        return configured
    local = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    return str(local) if local.exists() else None


async def _trusted_tls(origin):
    return True


@pytest.mark.asyncio
async def test_same_session_phone_login_scrubs_secret_before_next_capture():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        html = """<form id='login'>
          <input type='tel' placeholder='手机号' />
          <input type='password' placeholder='密码' />
          <button type='button' onclick='authenticate()'>登录</button>
        </form><div id='result'></div><script>
          async function authenticate() {
            await fetch('/session', {method:'POST',body:'login=1'});
            document.querySelector('#login').style.display='none';
            document.querySelector('#result').textContent='我的账户 退出';
          }
        </script>"""

        async def fulfill(route):
            if route.request.url.endswith("/session"):
                await route.fulfill(status=200, body="ok")
            else:
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(
            page=page,
            console_logs=[],
            network_requests=[],
            network_inflight=[],
            observers_muted=False,
        )
        driver = SimpleNamespace(
            _sessions={"web:1": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )

        async def untrusted_tls(origin):
            return False

        rejected = WebLoginSessionBrowser(driver, tls_verifier=untrusted_tls)
        assert (
            await rejected.submit(
                "web:1", "https://example.test", "phone", "13800138000", "private-secret"
            )
            == "WEB_LOGIN_TLS_UNTRUSTED"
        )
        assert await page.locator("input[type=password]").input_value() == ""
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.form_ready("web:1", "https://example.test", "phone")
        port._guard_states["web:1"] = {
            "blocked": True,
            "requests": {("https://stats.example", "GET")},
            "blocked_count": 1,
        }
        result = await port.submit(
            "web:1", "https://example.test", "phone", "13800138000", "private-secret"
        )
        assert result == "WEB_LOGIN_SUBMITTED"
        assert port.submit_clicked("web:1") is True
        assert await page.locator("input[type=password]").input_value() == ""
        assert await page.locator("input[type=tel]").input_value() == ""
        assert session.observers_muted is True
        assert (await port.capture("web:1", "https://example.test")).png
        await browser.close()


@pytest.mark.asyncio
async def test_submit_keeps_only_safe_same_origin_login_rejection_code():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        html = """<form><input type='text' placeholder='用户名'>
          <input type='password'><button type='button' onclick='login()'>登录</button></form>
          <script>async function login(){await fetch('/index/login',{
            method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username:document.querySelector('input').value,
              password:document.querySelector('input[type=password]').value})});}</script>"""

        async def fulfill(route):
            if route.request.url.endswith("/index/login"):
                await route.fulfill(
                    status=200, content_type="application/json",
                    body='{"code":-9999,"data":null,"msg":"账号或密码错误"}',
                )
            else:
                await route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                )

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(
            page=page, context=context, console_logs=[], network_requests=[],
            network_inflight=[], observers_muted=False,
        )
        driver = SimpleNamespace(
            _sessions={"login": session}, _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.submit(
            "login", "https://example.test", "username", "member42", "private-secret"
        ) == "WEB_LOGIN_SUBMITTED"
        assert port.login_server_failure("login") == "WEB_LOGIN_CREDENTIALS_REJECTED"
        assert await page.locator("input[type=password]").input_value() == ""
        await browser.close()


@pytest.mark.asyncio
async def test_external_form_action_is_not_authorized():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<form action='https://other.test/login'><input type='tel' "
                "placeholder='手机号'><input type='password'><button>登录</button></form>",
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"web:1": SimpleNamespace(page=page)},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert not await port.form_ready("web:1", "https://example.test", "phone")
        await browser.close()


@pytest.mark.asyncio
async def test_cross_origin_request_is_blocked_and_password_is_scrubbed():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        html = """<form><input type='tel' placeholder='手机号'>
          <input type='password'><button type='button'
          onclick="fetch('https://other.test/receive', {method:'POST',body:'x'})">登录</button>
          </form>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(
            page=page,
            console_logs=[],
            network_requests=[],
            network_inflight=[],
            observers_muted=False,
        )
        driver = SimpleNamespace(
            _sessions={"web:1": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        result = await port.submit(
            "web:1", "https://example.test", "phone", "13800138000", "private-secret"
        )
        assert result == "WEB_LOGIN_SUBMITTED"
        assert port.blocked_cross_origin("web:1") is True
        assert await page.locator("input[type=password]").input_value() == ""
        await browser.close()


@pytest.mark.asyncio
async def test_explicit_auth_origin_can_receive_login_post_without_disabling_guard():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        html = """<form><input type='tel' placeholder='手机号'>
          <input type='password'><button type='button'
          onclick="fetch('https://auth.test/receive', {method:'POST',
            body:document.querySelector('input[type=password]').value})">登录</button>
          </form>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        received = []

        async def auth_response(route):
            received.append((route.request.method, route.request.post_data))
            await route.fulfill(
                status=200,
                headers={"access-control-allow-origin": "*"},
                body="ok",
            )

        await context.route("https://auth.test/**", auth_response)
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(
            page=page,
            console_logs=[],
            network_requests=[],
            network_inflight=[],
            observers_muted=False,
        )
        driver = SimpleNamespace(
            _sessions={"web:1": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        policy = parse_web_login_auth_origins(
            '{"version":1,"sites":{"https://example.test":["https://auth.test"]}}',
            allowlist=parse_web_login_allowlist(
                "网址：https://example.test\n账号：13800138000\n密码：private-secret\n"
            ),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls, auth_policy=policy)

        result = await port.submit(
            "web:1",
            "https://example.test",
            "phone",
            "13800138000",
            "private-secret",
        )

        assert result == "WEB_LOGIN_SUBMITTED"
        assert received == [("POST", "private-secret")]
        assert await page.locator("input[type=password]").input_value() == ""
        await browser.close()


@pytest.mark.asyncio
async def test_auth_origin_redirect_cannot_send_secret_to_unknown_origin():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        html = """<form><input type='tel' placeholder='手机号'>
          <input type='password'><button type='button'
          onclick="fetch('https://auth.test/receive',{method:'POST',mode:'no-cors',
            body:document.querySelector('input[type=password]').value})">登录</button>
          </form>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=html),
        )
        await context.route(
            "https://auth.test/**",
            lambda route: route.fulfill(
                status=307, headers={"location": "https://unknown.test/leak"}, body=""
            ),
        )
        received_unknown = []

        async def unknown_response(route):
            received_unknown.append(route.request.post_data)
            await route.fulfill(status=200, body="ok")

        await context.route("https://unknown.test/**", unknown_response)
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(
            page=page,
            console_logs=[],
            network_requests=[],
            network_inflight=[],
            observers_muted=False,
        )
        driver = SimpleNamespace(
            _sessions={"web:redirect": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        allowlist = parse_web_login_allowlist(
            "网址：https://example.test\n账号：13800138000\n密码：private-secret\n"
        )
        policy = parse_web_login_auth_origins(
            '{"version":1,"sites":{"https://example.test":["https://auth.test"]}}',
            allowlist=allowlist,
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls, auth_policy=policy)
        await port.submit(
            "web:redirect",
            "https://example.test",
            "phone",
            "13800138000",
            "private-secret",
        )
        await page.wait_for_timeout(300)
        assert received_unknown == []
        await browser.close()


@pytest.mark.asyncio
async def test_register_button_cannot_be_used_as_login_entry():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body='<button onclick="window.clicked=true">注册</button>',
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"web:1": SimpleNamespace(page=page)},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = WebLoginFrame(
            png=b"",
            url=page.url,
            identity=await page.evaluate(
                "() => String(performance.timeOrigin) + ':' + location.origin"
            ),
        )
        bounds = await page.locator("button").bounding_box()
        viewport = page.viewport_size
        box = (
            bounds["x"] / viewport["width"],
            bounds["y"] / viewport["height"],
            (bounds["x"] + bounds["width"]) / viewport["width"],
            (bounds["y"] + bounds["height"]) / viewport["height"],
        )
        with pytest.raises(WebLoginSessionError) as error:
            await port.click("web:1", "https://example.test", frame, box)
        assert error.value.reason_code == "WEB_LOGIN_TARGET_UNPROVEN"
        assert await page.evaluate("window.clicked === true") is False
        await browser.close()


@pytest.mark.asyncio
async def test_same_origin_iframe_login_can_submit_and_scrub_after_frame_removal():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        main_html = "<iframe src='/login' style='width:600px;height:400px'></iframe>"
        login_html = """<div class='login_modal'>
          <input type='text' placeholder='账户或手机号'>
          <input type='password' placeholder='密码'>
          <div data-role='login' style='width:100px;height:40px;cursor:pointer'
            onclick="parent.document.querySelector('iframe').remove()">登录</div>
        </div>"""

        async def fulfill(route):
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=login_html if route.request.url.endswith("/login") else main_html,
            )

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/")
        await page.frame_locator("iframe").locator("input[type=password]").wait_for()
        session = SimpleNamespace(
            page=page,
            console_logs=[],
            network_requests=[],
            network_inflight=[],
            observers_muted=False,
        )
        driver = SimpleNamespace(
            _sessions={"web:iframe": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.form_ready("web:iframe", "https://example.test", "phone")
        assert (
            await port.submit(
                "web:iframe", "https://example.test", "phone", "13800138000", "secret"
            )
            == "WEB_LOGIN_SUBMITTED"
        )
        assert await page.locator("iframe").count() == 0
        await browser.close()


@pytest.mark.asyncio
async def test_modal_submit_outside_form_can_start_disabled_and_enable_after_fill():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        html = """<div class='auth-modal-card'>
          <form><input type='text' name='login_user' placeholder='账号'>
            <input type='password' name='login_pass'></form>
          <button id='login_submit' disabled onclick='window.clicked=true'>登录账号</button>
        </div><script>
          for (const input of document.querySelectorAll('input'))
            input.addEventListener('input', () => {
              document.querySelector('#login_submit').disabled =
                ![...document.querySelectorAll('input')].every(e => e.value.length > 0);
            });
        </script>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=html),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(
            page=page,
            console_logs=[],
            network_requests=[],
            network_inflight=[],
            observers_muted=False,
        )
        driver = SimpleNamespace(
            _sessions={"web:modal": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.form_ready("web:modal", "https://example.test", "phone")
        assert (
            await port.submit("web:modal", "https://example.test", "phone", "13800138000", "secret")
            == "WEB_LOGIN_SUBMITTED"
        )
        assert await page.evaluate("window.clicked === true")
        await browser.close()


@pytest.mark.asyncio
async def test_center_popup_close_target_ignores_unrelated_float_close():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        page = await browser.new_page(viewport={"width": 1000, "height": 800})
        await page.set_content("""<style>
          .van-overlay {position:fixed;inset:0;background:#888;z-index:99}
          .van-popup {position:fixed;left:350px;top:200px;width:300px;
            height:400px;background:white;z-index:100}
          .close-image {position:absolute;left:135px;top:350px;width:30px;
            height:30px;background:red}
          .slider-close {position:fixed;right:100px;bottom:100px;width:30px;
            height:30px;z-index:100;background:blue}
        </style><div class='van-overlay'></div>
        <div class='van-popup'><div class='close-image'></div></div>
        <div class='slider-close'></div>""")
        assert await _center_popup_close_target(page) == pytest.approx(
            [0.485, 0.6875, 0.515, 0.725]
        )
        await browser.close()


@pytest.mark.asyncio
async def test_login_closes_modal_with_icon_and_unique_explicit_close_button():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        html = (
            "<a href='/login'>登录</a>"
            "<div class='modal' style='position:fixed;inset:0;z-index:1050;"
            "background:rgba(0,0,0,.8)'>"
            "<div style='position:absolute;left:250px;top:120px;"
            "width:500px;height:450px;background:#fff'>"
            "<button class='close' style='position:absolute;right:10px;top:10px'"
            " onclick='this.closest(\".modal\").remove()'>×</button>"
            "<button class='btn-primary' style='position:absolute;"
            "left:190px;bottom:30px'"
            " onclick='this.closest(\".modal\").remove()'>立即关闭</button>"
            "</div></div>"
        )
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        assert await _safe_dom_close_target(
            SimpleNamespace(page=page), center_overlay_only=True
        ) is not None
        driver = SimpleNamespace(
            _sessions={}, _session_locks={}, _capture_semaphore=asyncio.Semaphore(2)
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._isolated_sessions["login"] = SimpleNamespace(page=page, context=context)
        assert await port.clear_obstructions("login", "https://example.test") == (
            "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
        )
        assert await page.locator(".modal").count() == 0
        await browser.close()


@pytest.mark.asyncio
async def test_low_z_fullscreen_van_overlay_is_closed_before_avatar_search():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        html = """<a id='avatar' href='#' style='position:absolute;right:20px;top:20px;
          width:40px;height:40px'>账户</a>
          <div class='van-overlay' style='position:fixed;inset:0;z-index:1;
            background:rgba(0,0,0,.5)'>
            <div class='close-img on-popup-close' style='position:absolute;
              left:485px;top:320px;width:30px;height:30px'
              onclick="this.closest('.van-overlay').remove()"></div>
          </div>"""
        await context.route("https://example.test/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html
        ))
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={}, _session_locks={}, _capture_semaphore=asyncio.Semaphore(2)
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._isolated_sessions["login"] = SimpleNamespace(page=page, context=context)
        assert await port.clear_obstructions("login", "https://example.test") == (
            "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
        )
        assert await page.locator(".van-overlay").count() == 0
        assert await page.evaluate("""() => document.elementFromPoint(960,40).id""") == (
            "avatar"
        )
        await browser.close()


@pytest.mark.asyncio
async def test_account_capture_waits_for_visible_loading_dialog_to_disappear():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        html = """<main><h1>金币充值</h1><div>选择充值金额</div>
          <div>支付方式</div></main>
          <div id='loading' role='dialog' class='van-toast--loading'
            style='position:fixed;left:400px;top:280px;width:200px;height:100px;
            background:black;color:white;z-index:20'>加载中...</div>
          <script>setTimeout(()=>document.querySelector('#loading').remove(),900)</script>"""
        await context.route("https://example.test/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html
        ))
        page = await context.new_page()
        await page.goto("https://example.test/deposit")
        driver = SimpleNamespace(
            _sessions={}, _session_locks={}, _capture_semaphore=asyncio.Semaphore(2)
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._isolated_sessions["login"] = SimpleNamespace(page=page, context=context)
        assert await port.wait_account_loading("login", "https://example.test")
        assert await page.locator("#loading").count() == 0
        await browser.close()


@pytest.mark.asyncio
async def test_account_menu_login_span_is_grounded_without_clicking_register():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(ignore_https_errors=True)
        html = """<div class='user-menu'>
          <span class='login-btn'
             onclick="document.querySelector('#login').hidden=false">登录</span>
          <span class='register-btn' onclick='window.registered=true'>注册</span>
        </div><div id='login' hidden><form>
          <input type='text' placeholder='账号'><input type='password'>
          <button type='button'>登录</button></form></div>
        <style>.user-menu span {display:inline-block;
          width:80px;height:40px;cursor:pointer}</style>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page)
        driver = SimpleNamespace(
            _sessions={"web:menu": session},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        box = await port.recover_login("web:menu", "https://example.test")
        assert box is not None
        identity = await page.evaluate(
            "() => String(performance.timeOrigin) + ':' + location.origin"
        )
        await port.click(
            "web:menu",
            "https://example.test",
            WebLoginFrame(png=b"", url=page.url, identity=identity),
            box,
        )
        assert await page.locator("#login").is_visible()
        assert await page.evaluate("window.registered === true") is False
        await browser.close()


@pytest.mark.asyncio
async def test_isolated_login_blocks_unknown_origin_before_first_navigation():
    events = []

    class Page:
        url = "https://example.test/"
        viewport_size = {"width": 800, "height": 600}

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Mozilla/5.0 HeadlessChrome/154.0.0.0 Safari/537.36"

        async def goto(self, url, **kwargs):
            events.append("goto")
            self.url = url

    class Context:
        async def route_web_socket(self, pattern, handler):
            events.append("websocket")

        async def route(self, pattern, handler):
            events.append("route")

        async def new_page(self):
            return Page()

        async def close(self):
            pass

    class Chromium:
        async def new_context(self, **kwargs):
            if kwargs:
                assert kwargs["user_agent"] == (
                    "Mozilla/5.0 Chrome/154.0.0.0 Safari/537.36"
                )
            return Context()

    main = SimpleNamespace(page=Page(), browser=Chromium())
    driver = SimpleNamespace(
        _sessions={"main": main},
        _session_locks={},
        _capture_semaphore=asyncio.Semaphore(2),
    )
    port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
    assert await port.open_isolated("main", "https://example.test") == "main:login"
    assert events == ["websocket", "route", "goto"]


@pytest.mark.asyncio
async def test_unknown_origin_guard_rejects_without_credentials():
    port = WebLoginSessionBrowser(SimpleNamespace(), tls_verifier=_trusted_tls)
    state = {"blocked": False, "requests": set()}
    guard = port._credential_route_guard("https://example.test", "", "", state)

    class Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url, method="GET")
            self.result = None

        async def fallback(self):
            self.result = "allowed"

        async def abort(self):
            self.result = "blocked"

    same = Route("https://example.test/home")
    unknown = Route("https://other.test/pixel")
    await guard(same)
    await guard(unknown)
    assert same.result == "allowed"
    assert unknown.result == "blocked"


@pytest.mark.asyncio
async def test_visible_login_tab_does_not_compete_with_submit_button():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        html = """<div class='hg-auth-modal' style='position:fixed;left:35%;top:20%;
          width:30%;height:50%'>
          <button class='hg-auth-tabs__item is-active'>登录</button>
          <input type='text' name='username' placeholder='请输入账号'>
          <input type='password' name='password' placeholder='请输入密码'>
          <button class='hg-auth-submit'>登录</button>
        </div>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"web:1": SimpleNamespace(page=page)},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.form_ready("web:1", "https://example.test", "username")
        await browser.close()


@pytest.mark.asyncio
async def test_visible_header_login_link_wins_over_other_account_hint():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        html = """<header style='height:80px'>
          <a href='#' class='bind_login' style='position:absolute;right:30px;top:15px'>
            <img width='25' height='25' alt=''>登入</a>
          <a href='/account' class='user-profile'
             style='position:absolute;left:20px;top:15px'>我的账户</a>
        </header>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"web:1": SimpleNamespace(page=page)},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.recover_entry("web:1", "https://example.test") is not None
        await browser.close()


@pytest.mark.asyncio
async def test_authenticated_header_avatar_can_reopen_account_menu():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        html = """<header style='height:80px'>
          <a href='#' class='bind_login' style='position:absolute;right:30px;top:15px'>
            <img width='25' height='25' alt=''>member</a>
          <a href='/account' class='user-profile'
             style='position:absolute;left:20px;top:15px'>我的账户</a>
        </header>"""
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"web:1": SimpleNamespace(page=page)},
            _session_locks={},
            _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.recover_entry("web:1", "https://example.test") is not None
        await browser.close()


@pytest.mark.asyncio
async def test_post_login_entry_waits_for_hydrated_profile_link():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    "<header><a id='guest' href='/login' style='position:absolute;"
                    "left:900px;top:10px;width:80px;height:30px'>登录</a></header>"
                    "<script>setTimeout(() => {document.querySelector('#guest').remove();"
                    "const a=document.createElement('a');a.href='/user/profile';"
                    "a.textContent='member42';a.style.cssText='position:absolute;"
                    "left:700px;top:10px;width:100px;height:30px';"
                    "document.querySelector('header').append(a)}, 400)</script>"
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"web:1": SimpleNamespace(page=page)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        box = await port.recover_authenticated_entry("web:1", "https://example.test")
        assert box is not None
        assert 0.69 <= box[0] <= 0.71
        await browser.close()


@pytest.mark.asyncio
async def test_post_login_entry_rejects_content_author_profile_link():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(viewport={"width": 1000, "height": 700})
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    "<main><article><a href='/user/profile?author=42' "
                    "style='position:absolute;left:700px;top:10px;"
                    "width:100px;height:30px'>author</a></article></main>"
                ),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"web:1": SimpleNamespace(page=page)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        assert await port.recover_authenticated_entry(
            "web:1", "https://example.test"
        ) is None
        await browser.close()


@pytest.mark.asyncio
async def test_blocked_post_remains_auditable_after_many_background_gets():
    port = WebLoginSessionBrowser(SimpleNamespace(), tls_verifier=_trusted_tls)
    state = {"blocked": False, "requests": set()}
    guard = port._credential_route_guard("https://example.test", "", "", state)

    class Route:
        def __init__(self, url, method):
            self.request = SimpleNamespace(url=url, method=method)
            self.result = None

        async def abort(self):
            self.result = "blocked"

        async def fallback(self):
            self.result = "allowed"

    for number in range(20):
        await guard(Route(f"https://noise{number}.test/pixel", "GET"))
    post = Route("https://unknown-auth.test/login", "POST")
    await guard(post)
    assert post.result == "blocked"
    assert ("https://unknown-auth.test", "POST") in state["requests"]
    assert len(state["requests"]) <= 20


@pytest.mark.asyncio
async def test_recharge_label_without_navigation_is_not_a_recharge_page():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        chromium = await playwright.chromium.launch(**options)
        context = await chromium.new_context()
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<a href='#'>\u53bb\u5145\u503c</a>",
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        session = SimpleNamespace(page=page, context=context)
        driver = SimpleNamespace(
            _sessions={}, _session_locks={}, _capture_semaphore=asyncio.Semaphore(2)
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        port._isolated_sessions["login"] = session
        frame = await port.capture("login", "https://example.test")
        actions = await port.account_actions("login", "https://example.test", frame, ())
        assert len(actions) == 1
        assert not await port.click_account_action(
            "login", "https://example.test", frame, actions[0]
        )
        await chromium.close()


@pytest.mark.asyncio
async def test_recharge_route_with_amount_and_payment_body_is_confirmed():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8",
                body=("<main><h1>我的钱包</h1><section>选择充值金额</section>"
                      "<section>支付方式</section></main>"),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/ai/my-recharge/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        assert await port.confirm_recharge_page(
            "login", "https://example.test", frame, ()
        )
        await browser.close()


@pytest.mark.asyncio
async def test_wallet_page_needs_visible_balance_and_topup_needs_independent_body():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()

        async def fulfill(route):
            path = route.request.url.split("example.test", 1)[-1]
            body = (
                "<main><h1>我的钱包</h1><span>当前余额：0</span></main>"
                if path.startswith("/wallet")
                else "<main><h1>金币充值</h1></main>"
            )
            await route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=body
            )

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/wallet")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        assert await port.confirm_wallet_page(
            "login", "https://example.test", frame, ()
        )
        assert not await port.confirm_recharge_page(
            "login", "https://example.test", frame, ()
        )
        await page.goto("https://example.test/deposit")
        frame = await port.capture("login", "https://example.test")
        assert not await port.confirm_wallet_page(
            "login", "https://example.test", frame, ()
        )
        assert not await port.confirm_recharge_page(
            "login", "https://example.test", frame, ()
        )
        await page.evaluate("""() => {
          document.querySelector('main').insertAdjacentHTML('beforeend',
            '<section>选择充值金额</section><section>支付方式</section>');
        }""")
        frame = await port.capture("login", "https://example.test")
        assert await port.confirm_recharge_page(
            "login", "https://example.test", frame, ()
        )
        await browser.close()


@pytest.mark.asyncio
async def test_wallet_label_takes_priority_over_recharge_like_url():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8",
                body=("<a href='/my/recharge/' style='display:block;width:150px;"
                      "height:50px'>我的钱包</a>"),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        actions = await port.account_actions("login", "https://example.test", frame, ())
        assert len(actions) == 1
        assert actions[0]["kind"] == "wallet"
        await browser.close()


@pytest.mark.asyncio
async def test_textless_deposit_url_is_a_topup_candidate():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8",
                body=("<a href='/usercenter/deposit/' style='display:block;"
                      "width:120px;height:50px'><svg width='24' height='24'></svg></a>"),
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        actions = await port.account_actions("login", "https://example.test", frame, ())
        assert len(actions) == 1
        assert actions[0]["kind"] == "recharge"
        await browser.close()


@pytest.mark.asyncio
async def test_recharge_history_link_and_heading_are_not_recharge_actions():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()

        async def fulfill(route):
            body = (
                "<h1>充值记录</h1>"
                if "recharge-record" in route.request.url
                else "<a href='/my/recharge-record/'>充值记录</a>"
            )
            await route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=body
            )

        await context.route("https://example.test/**", fulfill)
        page = await context.new_page()
        await page.goto("https://example.test/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        assert not await port.account_actions("login", "https://example.test", frame, ())
        await page.goto("https://example.test/my/recharge-record/")
        frame = await port.capture("login", "https://example.test")
        assert not await port.confirm_recharge_page(
            "login", "https://example.test", frame, ()
        )
        await browser.close()

@pytest.mark.asyncio
async def test_current_profile_page_link_is_not_reclicked():
    async with async_playwright() as playwright:
        options = {"headless": True}
        executable = _executable()
        if executable:
            options["executable_path"] = executable
        chromium = await playwright.chromium.launch(**options)
        context = await chromium.new_context()
        await context.route(
            "https://example.test/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8",
                body="<a href='/profile/'>个人中心</a><a href='/profile/'>我的主页</a>",
            ),
        )
        page = await context.new_page()
        await page.goto("https://example.test/profile/")
        driver = SimpleNamespace(
            _sessions={"login": SimpleNamespace(page=page, context=context)},
            _session_locks={}, _capture_semaphore=asyncio.Semaphore(2),
        )
        port = WebLoginSessionBrowser(driver, tls_verifier=_trusted_tls)
        frame = await port.capture("login", "https://example.test")
        assert await port.account_actions("login", "https://example.test", frame, ()) == ()
        assert await port.reveal_account_action(
            "login", "https://example.test", frame, "profile"
        ) == "absent"
        await chromium.close()
