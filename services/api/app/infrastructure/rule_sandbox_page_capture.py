"""规则测试：按网址用正式采集同一套程序打开一次页面，抓下源码、文字、界面快照和截图。

复用 ``PlaywrightPageCaptureBrowser`` 的单次（不绑定会话）采集路径，与正式 H5 / Web 采集走
同一个产物收集函数，抓下来的格式和正式任务一致；不建任务、不写库、不留证。

这个接口让服务器替人打开网址，必须防止被拿去访问服务器内网：只接受 http / https，
网址和页面里的每个请求（含跳转、图片、脚本）都先解析地址，内网、本机、保留地址一律拦下。
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from services.api.app.core.config import get_settings

# 页面稳定最多等 15 秒、导航最多重试两次，给足整次抓取的时间
CAPTURE_TIMEOUT_SECONDS = 120.0
logger = logging.getLogger(__name__)

NAVIGATION_TIMEOUT_MS = 30_000
# 每次抓取都会起一个 Chromium，同一进程同时最多两个，免得把接口进程拖垮
_CAPTURE_SLOTS = asyncio.Semaphore(2)
_INTERNAL_SUFFIXES = (".internal", ".local", ".localhost", ".lan", ".intranet")
# 代理软件的 fake-ip 段：开了代理的机器上所有域名都解析到这里，真实去向由代理决定
_FAKE_IP_PROXY_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class RuleTestPageCaptureError(RuntimeError):
    """网址不能抓或抓取失败；消息直接给页面看。"""


@dataclass(frozen=True, kw_only=True)
class RuleTestPageCapture:
    """抓下来的一页证据，交给测试页填进各个输入框。"""

    final_url: str
    title: str
    visible_text: str
    dom_html: str
    ui_snapshot: dict[str, Any] | None
    screenshot_png: bytes | None
    notices: tuple[str, ...]


def validate_capture_url(url: str) -> str:
    """只接受带主机名的 http / https 网址，不允许在网址里带账号密码。"""
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
        # 端口越界时 urlsplit 才会在读 port 时报错
        valid_port = parts.port is None or parts.port > 0
    except ValueError as exc:
        raise RuleTestPageCaptureError("网址格式不对") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise RuleTestPageCaptureError("只能抓 http:// 或 https:// 开头的网址")
    if not parts.hostname or not valid_port:
        raise RuleTestPageCaptureError("网址里没有有效的域名或端口")
    if parts.username or parts.password:
        raise RuleTestPageCaptureError("网址里不能带账号密码")
    return text


async def ensure_public_host(hostname: str) -> None:
    """域名解析出的每个地址都必须是公网地址。"""
    if not await _is_public_host(hostname):
        raise RuleTestPageCaptureError("不能抓取内网、本机或保留地址的页面")


# 开屏 / 线路加载层：先给页面几秒让倒计时自己走完，还没进去再点正式采集认得的「跳过」控件
STARTUP_LAYER_AUTO_DISMISS_WAIT_SECONDS = 6.0
STARTUP_LAYER_DISMISSED_NOTICE = (
    "抓到的第一帧是开屏广告 / 线路加载页，已等它进入（或自动点了「跳过」）后再抓，"
    "下面是进入后的页面"
)
STARTUP_LAYER_DISMISS_FAILED_NOTICE = (
    "抓到的是开屏广告 / 线路加载页，等了几秒、也试过点「跳过」都没进到目标页面；"
    "查首页、充值页这类页面的规则请改传截图，查开屏广告的规则可以直接用"
)


# 进站后的弹窗广告：同一会话里最多连点几次「关闭」；只认带关闭语义的控件，点不到就停
ENTRY_POPUP_MAX_DISMISSALS = 3
ENTRY_POPUP_DISMISSAL_SELECTOR = "text=关闭"
ENTRY_POPUP_CLICK_TIMEOUT_MS = 8_000
ENTRY_POPUP_DISMISSED_NOTICE = (
    "进入后页面被弹窗广告盖着，已自动点「关闭」{count} 次后再抓，下面是关掉后的页面"
)
ENTRY_POPUP_REMAINS_NOTICE = (
    "进入后页面一直被弹窗盖着，自动点「关闭」没能全部关掉；查底层页面的规则请改传截图"
)
_POPUP_WALL_MAX_TEXT_CHARACTERS = 60
_POPUP_WALL_WORDS = ("关闭", "我知道了", "×", "X", "跳过")


def _base_page_occluded(ui_snapshot: Any) -> bool:
    """按正式展示层判定看底层页面是不是被遮挡层盖住（快照里的真实矩形与 hit-test）。"""
    if not isinstance(ui_snapshot, dict):
        return False
    from services.api.app.application.evidence_presentation import derive_h5_presentation

    try:
        assessment = derive_h5_presentation(ui_snapshot=ui_snapshot)
    except Exception:  # noqa: BLE001 - 判不出来就当没遮挡，交给后面的门禁
        return False
    kind = str(getattr(assessment.topmost_layer_kind, "value", assessment.topmost_layer_kind))
    visibility = str(
        getattr(assessment.base_page_visibility, "value", assessment.base_page_visibility)
    )
    return kind in {"overlay", "system_dialog"} and visibility not in {
        "visible",
        "partially_occluded",
    }


def _covered_by_popup(page: RuleTestPageCapture) -> bool:
    return looks_like_popup_wall(page.visible_text) or _base_page_occluded(page.ui_snapshot)


def looks_like_popup_wall(visible_text: str) -> bool:
    """整屏被弹窗盖住时，视口里只剩弹窗自己的一两个词（关闭 / 我知道了 / ×）。"""
    text = " ".join(visible_text.split())
    if not text or len(text) > _POPUP_WALL_MAX_TEXT_CHARACTERS:
        return False
    return any(word in text for word in _POPUP_WALL_WORDS)


def _resolve_startup_dismissal_selector(dom_html: str) -> str | None:
    """用正式采集判开屏关闭控件的同一套结构合同，从原始源码里认出「跳过」控件。"""
    if not dom_html.strip():
        return None
    from services.api.app.agents.tools.page_detection_tools import (
        _startup_ad_dismissal_selector,
    )

    return _startup_ad_dismissal_selector(page_type="startup_ad", dom=dom_html)


async def capture_rule_test_page(
    *, url: str, platform: str, keep_startup_layer: bool = False
) -> RuleTestPageCapture:
    """打开一次网址并按正式采集口径收集产物；H5 用手机视口，Web 用桌面视口。

    H5 抓到的第一帧还是开屏 / 线路加载层时，同一个浏览器会话里先等倒计时走完，
    还没进去就点正式采集认得的「跳过」控件，返回进入后的页面；查开屏本身的规则
    （keep_startup_layer）不做这一步，抓到的就是要的那一帧。
    """
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
    )

    target = validate_capture_url(url)
    await ensure_public_host(str(urlsplit(target).hostname))
    guard = _PublicAddressGuard()
    if platform == "web":
        from services.api.app.application.web_browser_profile import (
            DEFAULT_WEB_BROWSER_PROFILE_ID,
            WebBrowserProfileRegistry,
        )
        from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
            WebDesktopCapturePolicyBrowser,
        )

        profile = WebBrowserProfileRegistry().resolve(DEFAULT_WEB_BROWSER_PROFILE_ID)
        parameters = dict(profile.to_action_parameters())
        parameters.pop("browser_profile_id", None)
        # 入口探测阶段只观察、不清弹窗，与正式 Web 进站第一帧同口径
        request = PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url=target,
            timeout_ms=NAVIGATION_TIMEOUT_MS,
            web_capture_stage="web_entry_probe",
            **parameters,
        )
        browser: Any = WebDesktopCapturePolicyBrowser(browser=_web_capture_browser(guard))
        async with _CAPTURE_SLOTS:
            result = await _capture_once(request, browser)
        return _page_capture_from_result(result, requested_url=target, guard=guard)

    from services.api.app.application.h5_viewport import h5_viewport_parameters

    viewport = h5_viewport_parameters()
    # 会话让后面的等待和点「跳过」落在同一个已打开的页面上，而不是重新导航再撞一次开屏
    session_id = f"rule-test-{uuid4()}"
    # 截视口（手机一屏），与页面文字只取视口内一致，也避免长图被压缩到看不清
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        url=target,
        timeout_ms=NAVIGATION_TIMEOUT_MS,
        full_page=False,
        session_id=session_id,
        **viewport,
    )
    browser = _h5_capture_browser(guard)
    try:
        async with _CAPTURE_SLOTS:
            first = _page_capture_from_result(
                await _capture_once(request, browser),
                requested_url=target,
                guard=guard,
                warn_startup_layer=not keep_startup_layer,
            )
            if keep_startup_layer:
                return first
            if looks_like_startup_layer(first.visible_text):
                return await _enter_past_startup_layer(
                    first=first,
                    browser=browser,
                    session_id=session_id,
                    target=target,
                    guard=guard,
                    viewport=viewport,
                )
            return await _clear_entry_popups(
                first,
                browser=browser,
                session_id=session_id,
                target=target,
                guard=guard,
                viewport=viewport,
            )
    finally:
        close_session = getattr(browser, "close_session", None)
        if close_session is not None:
            try:
                await close_session(session_id)
            except Exception:  # noqa: BLE001 - 收尾失败不影响已经拿到的结果
                logger.warning("规则测试抓页面：关闭浏览器会话失败 session_id=%s", session_id)


async def _capture_once(request: Any, browser: Any) -> Any:
    from services.api.app.infrastructure.browser.page_capture import capture_page_with_timeout

    try:
        return await capture_page_with_timeout(
            request=request,
            browser=browser,
            timeout_seconds=CAPTURE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise RuleTestPageCaptureError("页面两分钟内没有打开完，请稍后再试或换个网址") from exc
    except RuleTestPageCaptureError:
        raise
    except Exception as exc:  # noqa: BLE001 - 浏览器错误原文对运营没有意义
        raise RuleTestPageCaptureError(_browser_error_text(exc)) from exc


async def _enter_past_startup_layer(
    *,
    first: RuleTestPageCapture,
    browser: Any,
    session_id: str,
    target: str,
    guard: _PublicAddressGuard,
    viewport: dict[str, Any],
) -> RuleTestPageCapture:
    """第一帧是开屏层：先等倒计时自己走完再看一眼，还在就点「跳过」；都进不去退回开屏帧并说明。"""
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
        PageTriggerAction,
    )

    current = first
    try:
        await asyncio.sleep(STARTUP_LAYER_AUTO_DISMISS_WAIT_SECONDS)
        observed = _page_capture_from_result(
            await _capture_once(
                PageCaptureRequest(
                    mode=PageCaptureMode.OBSERVE,
                    session_id=session_id,
                    timeout_ms=NAVIGATION_TIMEOUT_MS,
                    full_page=False,
                    **viewport,
                ),
                browser,
            ),
            requested_url=target,
            guard=guard,
            warn_startup_layer=False,
        )
        if not looks_like_startup_layer(observed.visible_text):
            return await _clear_entry_popups(
                _with_notices(observed, add=(STARTUP_LAYER_DISMISSED_NOTICE,)),
                browser=browser,
                session_id=session_id,
                target=target,
                guard=guard,
                viewport=viewport,
            )
        current = observed
        selector = _resolve_startup_dismissal_selector(observed.dom_html or first.dom_html)
        if selector:
            clicked = _page_capture_from_result(
                await _capture_once(
                    PageCaptureRequest(
                        mode=PageCaptureMode.SINGLE_TRIGGER,
                        session_id=session_id,
                        trigger_action=PageTriggerAction.CLICK,
                        selector=selector,
                        timeout_ms=NAVIGATION_TIMEOUT_MS,
                        full_page=False,
                        **viewport,
                    ),
                    browser,
                ),
                requested_url=target,
                guard=guard,
                warn_startup_layer=False,
            )
            if not looks_like_startup_layer(clicked.visible_text):
                return await _clear_entry_popups(
                    _with_notices(clicked, add=(STARTUP_LAYER_DISMISSED_NOTICE,)),
                    browser=browser,
                    session_id=session_id,
                    target=target,
                    guard=guard,
                    viewport=viewport,
                )
            current = clicked
    except RuleTestPageCaptureError as exc:
        logger.info("规则测试抓页面：进开屏后的页面没成功 %s", exc)
    except Exception:  # noqa: BLE001 - 进不去就退回开屏帧，把情况写成提示
        logger.exception("规则测试抓页面：跳过开屏层时出错")
    return _with_notices(
        current,
        drop=(STARTUP_LAYER_NOTICE,),
        add=(STARTUP_LAYER_DISMISS_FAILED_NOTICE,),
    )


async def _clear_entry_popups(
    frame: RuleTestPageCapture,
    *,
    browser: Any,
    session_id: str,
    target: str,
    guard: _PublicAddressGuard,
    viewport: dict[str, Any],
) -> RuleTestPageCapture:
    """进站后被弹窗广告盖住：同一会话里点「关闭」，最多几次；关不掉就把弹窗帧连同说明一起给出去。"""
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
        PageTriggerAction,
    )

    current = frame
    count = 0
    try:
        while count < ENTRY_POPUP_MAX_DISMISSALS and _covered_by_popup(current):
            current = _page_capture_from_result(
                await _capture_once(
                    PageCaptureRequest(
                        mode=PageCaptureMode.SINGLE_TRIGGER,
                        session_id=session_id,
                        trigger_action=PageTriggerAction.CLICK,
                        selector=ENTRY_POPUP_DISMISSAL_SELECTOR,
                        timeout_ms=ENTRY_POPUP_CLICK_TIMEOUT_MS,
                        full_page=False,
                        **viewport,
                    ),
                    browser,
                ),
                requested_url=target,
                guard=guard,
                warn_startup_layer=False,
            )
            count += 1
    except RuleTestPageCaptureError as exc:
        logger.info("规则测试抓页面：关进站弹窗没成功 %s", exc)
    except Exception:  # noqa: BLE001 - 关不掉就退回当前帧，把情况写成提示
        logger.exception("规则测试抓页面：关进站弹窗时出错")
    if _covered_by_popup(current):
        return _with_notices(current, add=(*frame.notices, ENTRY_POPUP_REMAINS_NOTICE))
    if count == 0:
        return frame
    return _with_notices(
        current, add=(*frame.notices, ENTRY_POPUP_DISMISSED_NOTICE.format(count=count))
    )


def _with_notices(
    page: RuleTestPageCapture,
    *,
    add: tuple[str, ...] = (),
    drop: tuple[str, ...] = (),
) -> RuleTestPageCapture:
    notices = tuple(item for item in page.notices if item not in drop)
    notices += tuple(item for item in add if item not in notices)
    return replace(page, notices=notices)


def _page_capture_from_result(
    result: Any,
    *,
    requested_url: str,
    guard: _PublicAddressGuard,
    warn_startup_layer: bool = True,
) -> RuleTestPageCapture:
    if result.execution_status != "success" and result.error_message:
        raise RuleTestPageCaptureError(f"页面没打开：{str(result.error_message)[:200]}")
    notices: list[str] = []
    artifacts: dict[str, Any] = {}
    for artifact in result.artifacts:
        artifacts.setdefault(str(artifact.artifact_type), artifact.content)
    screenshot_content = artifacts.get("screenshot")
    snapshot_content = artifacts.get("ui_snapshot")
    if _is_sequence(screenshot_content) or _is_sequence(snapshot_content):
        notices.append("页面在变化（例如倒计时），抓了多帧，这里取最后一帧")
    screenshot_frame = _last_frame(screenshot_content)
    snapshot_frame = _last_frame(snapshot_content)
    metrics = result.metrics if isinstance(result.metrics, dict) else {}
    status_code = result.status_code
    if isinstance(status_code, int) and status_code >= 400:
        notices.append(f"网站返回了 {status_code}，抓到的可能是错误页")
    if metrics.get("page_settle_complete") is False:
        notices.append("页面一直没加载稳，抓到的可能不完整")
    readiness = metrics.get("web_readiness")
    if isinstance(readiness, dict) and readiness.get("human_verification_present"):
        notices.append("页面要求人机验证，抓到的可能是验证页")
    if guard.blocked_hosts:
        blocked = "、".join(sorted(guard.blocked_hosts))
        notices.append(f"页面里有请求指向内网或本机地址，已拦下没加载：{blocked}")
    visible_text = str(artifacts.get("visible_text") or "")
    if warn_startup_layer and looks_like_startup_layer(visible_text):
        notices.append(STARTUP_LAYER_NOTICE)
    return RuleTestPageCapture(
        final_url=str(result.final_url or requested_url),
        title=str(result.title or ""),
        visible_text=visible_text,
        dom_html=str(artifacts.get("dom") or ""),
        ui_snapshot=dict(snapshot_frame) if isinstance(snapshot_frame, dict) else None,
        screenshot_png=_decode_screenshot(screenshot_frame),
        notices=tuple(notices),
    )


# 开屏页、线路加载页上的字很少，且带倒计时 / 跳过 / 线路读取字样；正文页不会同时满足这两条
STARTUP_LAYER_MAX_TEXT_CHARACTERS = 300
_STARTUP_COUNTDOWN_PATTERN = re.compile(r"\d+\s*[sS秒]?\s*[（(]?\s*跳过|跳过\s*[（(]?\s*\d+")
STARTUP_LAYER_NOTICE = (
    "抓到的是开屏广告 / 线路加载页，不是目标页面（页面上只有倒计时、跳过或线路读取字样）。"
    "查开屏广告的规则可以直接用；查首页、充值页这类页面的规则，请等它自己进到目标页后截图上传，"
    "否则会按开屏页判"
)


def looks_like_startup_layer(visible_text: str) -> bool:
    """抓到的那一帧还停在开屏 / 线路加载层：文案标记与正式采集判开屏的同一套。"""
    from services.api.app.infrastructure.browser.page_capture import (
        LINE_LOADING_TEXT_MARKERS,
        STARTUP_AD_TEXT_MARKERS,
    )

    text = " ".join(visible_text.split())
    if not text or len(text) > STARTUP_LAYER_MAX_TEXT_CHARACTERS:
        return False
    if _STARTUP_COUNTDOWN_PATTERN.search(text):
        return True
    return any(marker in text for marker in (*LINE_LOADING_TEXT_MARKERS, *STARTUP_AD_TEXT_MARKERS))


def _is_sequence(content: Any) -> bool:
    return isinstance(content, dict) and content.get("sequence") is True


def _last_frame(content: Any) -> Any:
    if not _is_sequence(content):
        return content
    frames = content.get("frames")
    if not isinstance(frames, list) or not frames:
        return None
    frame = dict(frames[-1]) if isinstance(frames[-1], dict) else None
    if frame is not None:
        frame.pop("index", None)
    return frame


def _decode_screenshot(content: Any) -> bytes | None:
    if not isinstance(content, dict) or content.get("encoding") != "base64":
        return None
    data = content.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except ValueError:
        return None


def _browser_error_text(exc: Exception) -> str:
    text = str(exc)
    if "ERR_NAME_NOT_RESOLVED" in text:
        return "网址的域名解析不到，请检查网址"
    if "ERR_CONNECTION" in text or "ERR_ADDRESS" in text or "ERR_TIMED_OUT" in text:
        return "连不上这个网址，请检查网址或稍后再试"
    if "ERR_BLOCKED_BY_CLIENT" in text:
        return "页面跳转到了内网或本机地址，已拦下"
    if "ERR_CERT" in text or "ERR_SSL" in text:
        return "网站证书有问题，打不开"
    return "页面没打开，请检查网址或稍后再试"


class _PublicAddressGuard:
    """页面里的每个请求先过地址检查：公网放行，其余中止。"""

    def __init__(self) -> None:
        self._verdicts: dict[str, bool] = {}
        self.blocked_hosts: set[str] = set()

    async def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        # data: / blob: / about: 不出网；file: 等本地协议一律不放
        if scheme in {"data", "blob", "about"}:
            return True
        if scheme not in {"http", "https", "ws", "wss"}:
            return False
        hostname = (parts.hostname or "").lower()
        if not hostname:
            return False
        if hostname not in self._verdicts:
            self._verdicts[hostname] = await _is_public_host(hostname)
        if not self._verdicts[hostname]:
            self.blocked_hosts.add(hostname)
        return self._verdicts[hostname]

    async def handle(self, route: Any) -> None:
        request = getattr(route, "request", None)
        if await self.allows(str(getattr(request, "url", "") or "")):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")


class _GuardedChromium:
    """包住启动好的 Chromium：新开的浏览器上下文先装地址检查，Service Worker 不许绕过拦截。"""

    def __init__(self, browser: Any, guard: _PublicAddressGuard) -> None:
        self._browser = browser
        self._guard = guard

    async def new_context(self, **options: Any) -> Any:
        options.setdefault("service_workers", "block")
        context = await self._browser.new_context(**options)
        await context.route("**/*", self._guard.handle)
        return context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)


def _guarded_browser_class() -> type:
    from services.api.app.infrastructure.browser.page_capture import (
        PlaywrightPageCaptureBrowser,
    )

    class _RuleTestCaptureBrowser(PlaywrightPageCaptureBrowser):
        """单次采集的浏览器：和正式采集同参数启动，再套上地址检查。"""

        def __init__(self, *, guard: _PublicAddressGuard, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._guard = guard

        async def _launch_browser(self, playwright: Any) -> Any:
            # 与正式采集同样无头启动、同样走代理；接口进程共享内存小，改用临时目录
            options: dict[str, Any] = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if self._proxy_server is not None:
                proxy = {"server": self._proxy_server}
                if self._proxy_bypass is not None:
                    proxy["bypass"] = self._proxy_bypass
                options["proxy"] = proxy
            return _GuardedChromium(await playwright.chromium.launch(**options), self._guard)

    return _RuleTestCaptureBrowser


def _h5_capture_browser(guard: _PublicAddressGuard) -> Any:
    workflow = get_settings().workflow
    return _guarded_browser_class()(
        guard=guard,
        max_concurrent_captures=1,
        logical_surface_enabled=workflow.h5_logical_surface_enabled,
        logical_surface_shadow=workflow.h5_logical_surface_shadow,
        proxy_server=workflow.h5_browser_proxy_server,
        proxy_bypass=workflow.h5_browser_proxy_bypass,
    )


def _web_capture_browser(guard: _PublicAddressGuard) -> Any:
    workflow = get_settings().workflow
    return _guarded_browser_class()(
        guard=guard,
        max_concurrent_captures=1,
        proxy_server=workflow.web_browser_proxy_server,
        proxy_bypass=workflow.web_browser_proxy_bypass,
    )


async def _is_public_host(hostname: str) -> bool:
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return _address_is_public(str(literal))
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    except (OSError, UnicodeError):
        # 解析不到交给浏览器报「域名解析不到」，这里不放行也不当内网
        return _resolution_failure_allowed(hostname)
    addresses = {str(info[4][0]) for info in infos}
    return bool(addresses) and all(_address_is_public(address) for address in addresses)


def _resolution_failure_allowed(hostname: str) -> bool:
    lowered = hostname.lower()
    return lowered != "localhost" and not lowered.endswith(_INTERNAL_SUFFIXES)


def _address_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _FAKE_IP_PROXY_NETWORK:
        return True
    return ip.is_global
