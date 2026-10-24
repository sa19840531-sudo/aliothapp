"""广告只读渲染窗口：全部出站由固定地址传输接管，不共享业务会话。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied, validate_ad_url
from services.api.app.application.web_browser_profile import (
    DEFAULT_WEB_BROWSER_PROFILE_ID,
    WebBrowserProfileRegistry,
)
from services.api.app.infrastructure.browser.ad_safe_transport import AdSafeTransport
from services.api.app.infrastructure.browser.web_ad_hop_diagnostics import record_ad_hop
from services.api.app.infrastructure.browser.web_ad_script_policy import (
    WEB_AD_SCRIPT_CSP,
    WEB_AD_SCRIPT_GUARD,
    WEB_AD_SCRIPT_RESOURCE_TYPES,
    settle_web_ad_document,
)


@dataclass(frozen=True, slots=True)
class AdPageEvidenceView:
    """脱离浏览器对象的只读证据，调用方不能取得 Page、Context 或定位器。"""

    url: str
    _title: str
    _html: str
    _screenshot: bytes
    _ui_snapshot: dict[str, Any]
    _har_entries: tuple[dict[str, object], ...]

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._html

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        if full_page:
            raise AdOutboundDenied("WEB_AD_OUTBOUND_RESOURCE_TYPE_DENIED")
        return self._screenshot

    async def ui_snapshot(self) -> dict[str, Any]:
        return deepcopy(self._ui_snapshot)

    async def har_entries(self) -> list[dict[str, object]]:
        return deepcopy(list(self._har_entries))

    def __getattr__(self, name: str) -> Any:
        raise AdOutboundDenied("WEB_AD_CLICK_FORBIDDEN")


@asynccontextmanager
async def isolated_ad_page(
    *,
    browser: Any,
    url: str,
    source_url: str,
    timeout_ms: int,
    snapshot_collector: Callable[[Any], Awaitable[dict[str, Any]]] | None = None,
    enable_javascript: bool = False,
    diagnostic_events: list | None = None,
    user_agent: str | None = None,
):
    try:
        url = validate_ad_url(url, previous_url=source_url)
    except AdOutboundDenied as exc:
        record_ad_hop(diagnostic_events, url=url, resource_type='document',
                      request_scope='main_document', denial_reason=exc.reason)
        raise
    transport = AdSafeTransport(
        timeout_seconds=timeout_ms / 1000,
        **({"follow_client_redirects": False} if enable_javascript else {}),
        **({"diagnostic_events": diagnostic_events} if diagnostic_events is not None else {}),
    )
    first = await transport.fetch(
        url,
        previous_url=source_url,
        resource_kind="document",
        **({"request_scope": "main_document"} if diagnostic_events is not None else {}),
    )
    profile = WebBrowserProfileRegistry().resolve(DEFAULT_WEB_BROWSER_PROFILE_ID)
    # 默认静态策略保持不变；Web 显式启用脚本时仍由固定地址传输接管网络。
    context = await browser.new_context(
        viewport={"width": profile.viewport_width, "height": profile.viewport_height},
        screen={"width": profile.screen_width, "height": profile.screen_height},
        device_scale_factor=profile.device_scale_factor,
        is_mobile=profile.is_mobile,
        has_touch=profile.has_touch,
        color_scheme=profile.color_scheme,
        service_workers="block",
        java_script_enabled=enable_javascript,
        accept_downloads=False,
        proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
        **({"user_agent": user_agent} if user_agent is not None else {}),
    )
    try:
        cached = first
        navigation_count = 0
        navigation_failure = None
        if enable_javascript:
            await context.add_init_script(WEB_AD_SCRIPT_GUARD)

        async def route_request(route):
            nonlocal cached, navigation_count, navigation_failure
            transport_started = False
            try:
                request = route.request
                previous = request.redirected_from
                validate_ad_url(request.url, previous_url=previous.url if previous else first.url)
                allowed_resources = WEB_AD_SCRIPT_RESOURCE_TYPES if enable_javascript else {
                    "document",
                    "stylesheet",
                    "image",
                    "font",
                }
                if request.method != "GET" or request.resource_type not in allowed_resources:
                    raise AdOutboundDenied("WEB_AD_OUTBOUND_RESOURCE_TYPE_DENIED")
                if request.is_navigation_request():
                    navigation_count += 1
                    if enable_javascript and navigation_count > 6:
                        raise AdOutboundDenied("WEB_AD_OUTBOUND_REDIRECT_LIMIT")
                if (
                    cached is not None
                    and request.url == cached.url
                    and request.is_navigation_request()
                ):
                    response, cached = cached, None
                elif request.is_navigation_request() and not enable_javascript:
                    raise AdOutboundDenied("WEB_AD_OUTBOUND_EXTRA_NAVIGATION_DENIED")
                else:
                    transport_started = True
                    response = await transport.fetch(
                        request.url,
                        previous_url=first.url,
                        resource_kind=request.resource_type,
                        **({"request_scope": "main_document"
                            if request.is_navigation_request() and request.frame == page.main_frame
                            else "subresource"} if diagnostic_events is not None else {}),
                    )
                if (
                    enable_javascript and request.is_navigation_request()
                    and response.url != request.url
                ):
                    # 传输层已校验每次 HTTP 重定向；浏览器地址同步到真实落地，禁止伪造最终 URL。
                    cached = response
                    await route.fulfill(status=302, headers={"location": response.url}, body="")
                    return
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key
                    in {
                        "content-type",
                        "content-encoding",
                    }
                }
                if enable_javascript:
                    for key in ("access-control-allow-origin", "access-control-allow-headers"):
                        if key in response.headers:
                            headers[key] = response.headers[key]
                headers["cache-control"] = "no-store"
                headers["content-security-policy"] = WEB_AD_SCRIPT_CSP if enable_javascript else (
                    "default-src 'none'; img-src http: https: data:; "
                    "style-src http: https: 'unsafe-inline'; font-src http: https:; "
                    "script-src 'none'; connect-src 'none'; frame-src 'none'; "
                    "object-src 'none'; form-action 'none'; base-uri 'none'"
                )
                await route.fulfill(status=response.status, headers=headers, body=response.body)
            except Exception as exc:
                if not transport_started:
                    record_ad_hop(
                        diagnostic_events, url=route.request.url,
                        resource_type=route.request.resource_type,
                        request_scope='main_document' if route.request.is_navigation_request()
                        and route.request.frame == page.main_frame else 'subresource',
                        denial_reason=exc.reason if isinstance(exc, AdOutboundDenied)
                        else 'WEB_AD_OUTBOUND_RENDER_REQUEST_BLOCKED',
                    )
                # route 失败绝不 continue/fallback，浏览器配置也没有可用直连代理。
                transport.denied_reasons.add("WEB_AD_OUTBOUND_RENDER_REQUEST_BLOCKED")
                if enable_javascript and route.request.is_navigation_request():
                    if route.request.frame == page.main_frame:
                        navigation_failure = (
                            exc.reason if isinstance(exc, AdOutboundDenied)
                            else "WEB_AD_OUTBOUND_RENDER_REQUEST_BLOCKED"
                        )
                await route.abort("blockedbyclient")

        await context.route("**/*", route_request)
        route_ws = getattr(context, "route_web_socket", None)
        if not callable(route_ws):
            raise AdOutboundDenied("WEB_AD_OUTBOUND_ISOLATION_UNAVAILABLE")
        await route_ws("**/*", lambda websocket: websocket.close())
        page = await context.new_page()
        await page.goto(first.url, wait_until="domcontentloaded", timeout=timeout_ms)
        if enable_javascript:
            await settle_web_ad_document(page, timeout_ms=timeout_ms)
            if navigation_failure:
                raise AdOutboundDenied(navigation_failure)
        identity_reader = getattr(page, 'evaluate', None)
        identity_script = '() => [location.href, performance.timeOrigin]'
        before_identity = (
            await identity_reader(identity_script)
            if enable_javascript and callable(identity_reader) else None
        )
        ui_snapshot = await snapshot_collector(page) if snapshot_collector is not None else {}
        evidence = AdPageEvidenceView(
            url=str(page.url),
            _title=await page.title(),
            _html=await page.content(),
            _screenshot=await page.screenshot(full_page=False),
            _ui_snapshot=ui_snapshot,
            _har_entries=tuple(deepcopy(getattr(transport, "har_entries", ()))),
        )
        if enable_javascript:
            if navigation_failure:
                raise AdOutboundDenied(navigation_failure)
            if before_identity is not None and (
                await identity_reader(identity_script) != before_identity
            ):
                # 不将跨文档的首屏截图、DOM 和 URL 拼接成一次成功广告留证。
                raise AdOutboundDenied('WEB_AD_LANDING_CAPTURE_INCONSISTENT')
        yield evidence, transport
    finally:
        try:
            try:
                unroute_all = getattr(context, "unroute_all", None)
                if callable(unroute_all):
                    await unroute_all(behavior="ignoreErrors")
            finally:
                await context.close()
        except Exception:
            raise AdOutboundDenied("WEB_AD_LANDING_RETURN_FAILED") from None
