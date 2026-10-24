"""Web 广告脚本渲染边界；不改变 App/H5 默认静态留证策略。"""

import asyncio
import time

WEB_AD_SCRIPT_CSP = (
    "default-src 'none'; img-src http: https: data: blob:; "
    "style-src http: https: 'unsafe-inline'; font-src http: https: data:; "
    "script-src http: https: 'unsafe-inline' 'unsafe-eval'; "
    "connect-src http: https:; frame-src http: https:; "
    "worker-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'"
)

# 浏览器代理拒绝直连；HTTP 请求另经固定地址传输。禁用不经过该传输的通信能力。
WEB_AD_SCRIPT_GUARD = r"""(() => {
  for (const name of ['RTCPeerConnection','webkitRTCPeerConnection','RTCDataChannel',
    'WebTransport','Worker','SharedWorker']) {
    try { Object.defineProperty(globalThis, name, {value: undefined, configurable: false}); }
    catch (_) {}
  }
  globalThis.open = () => null;
})()"""

WEB_AD_SCRIPT_RESOURCE_TYPES = frozenset({
    "document", "stylesheet", "image", "font", "script", "xhr", "fetch",
})


async def settle_web_ad_document(page, *, timeout_ms):
    """有限等待脚本导航和 DOM 渲染；不等待全站网络空闲，不滚动。"""
    if not callable(getattr(page, 'evaluate', None)):
        return
    deadline = time.monotonic() + min(2.5, max(0.05, timeout_ms / 1000))
    stable_since, previous = time.monotonic(), None
    while time.monotonic() < deadline:
        try:
            current = await page.evaluate('''() => [location.href, performance.timeOrigin,
                document.readyState, document.body ? document.body.childElementCount : 0]''')
            if current != previous or current[2] == 'loading':
                previous, stable_since = current, time.monotonic()
            elif time.monotonic() - stable_since >= 0.4:
                return
        except Exception:
            previous, stable_since = None, time.monotonic()
        await asyncio.sleep(0.05)
