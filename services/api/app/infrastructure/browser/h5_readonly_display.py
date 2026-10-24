"""充值展示封闭当前文档出站；不临时放行后台选档，也不在取帧后解除隔离。"""

from urllib.parse import urldefrag

_CHANNEL_CENSUS = """(() => {
  const key=Symbol.for('alioth.h5.display.channels.v1');
  if(globalThis[key])return;
  let used=false,sealed=false,blocked=0;
  const channels=['Worker','SharedWorker','WebTransport',
    'RTCPeerConnection','webkitRTCPeerConnection'];
  for(const name of channels) {
    const original=globalThis[name];
    if(typeof original==='function')globalThis[name]=new Proxy(original,{
      construct(target,args){used=true;
        if(sealed){blocked++;throw new DOMException('H5 readonly display','SecurityError');}
        return Reflect.construct(target,args);}
    });
  }
  const guard=(owner,name)=>{const original=owner?.[name];if(typeof original!=='function')return;
    owner[name]=function(...args){
      if(sealed){blocked++;const error=new DOMException('H5 readonly display','SecurityError');
        if(name==='fetch'||name==='register')return Promise.reject(error);
        if(name==='sendBeacon')return false;throw error;}
      return Reflect.apply(original,this,args);
    };
  };
  guard(globalThis,'fetch');guard(XMLHttpRequest.prototype,'send');
  guard(Navigator.prototype,'sendBeacon');
  guard(globalThis.ServiceWorkerContainer?.prototype,'register');
  Object.defineProperty(globalThis,key,{
    value:(seal=false)=>{if(seal)sealed=true;
      return {installed:true,unsupported_channel_used:used,sealed,blocked};},configurable:false});
})()"""


class H5ReadonlyDisplayGuard:
    """同一H5上下文只有一个页面时封闭出站，明确新文档导航后才解封。"""

    def __init__(self, context):
        self.context = context
        self.page = None
        self.origin = None
        self.sealed_origins = set()
        self.exit_url = None
        self.blocked = 0
        self.inflight = set()
        self.channels = set()
        self._watched = set()

    def watch(self, page):
        if page in self._watched:
            return
        self._watched.add(page)

        def channel_open(channel):
            self.channels.add(channel)
            channel.on("close", lambda: self.channels.discard(channel))

        page.on("websocket", channel_open)
        page.on("worker", channel_open)
        page.on("framenavigated", self._document_committed)

    async def _document_committed(self, frame):
        if self.page is None or frame != self.page.main_frame or self.exit_url is None:
            return
        if urldefrag(frame.url)[0] != self.exit_url:
            return
        try:
            origin = await frame.evaluate("performance.timeOrigin")
        except Exception:
            return
        if origin != self.origin and origin not in self.sealed_origins:
            self.page, self.origin, self.exit_url = None, None, None

    async def route(self, route):
        if self.page is not None and self.exit_url is not None:
            await self._document_committed(route.request.frame)
        if self.page is None:
            await route.fallback()
            return
        request = route.request
        if (
            self.exit_url is not None
            and request.method == "GET"
            and request.is_navigation_request()
            and request.frame == self.page.main_frame
            and urldefrag(request.url)[0] == self.exit_url
        ):
            await route.fallback()
            return
        self.blocked += 1
        await route.abort()

    async def websocket(self, socket):
        if self.page is not None:
            self.blocked += 1
            await socket.close()
        else:
            socket.connect_to_server()

    async def seal(self, page):
        from services.api.app.infrastructure.browser.page_capture import (
            BlockedPageCaptureActionError,
        )

        state = await page.evaluate("""async()=>({
          census:globalThis[Symbol.for('alioth.h5.display.channels.v1')]?.(),
          origin:performance.timeOrigin,
          service_worker:!!navigator.serviceWorker &&
            (!!navigator.serviceWorker.controller ||
             (await navigator.serviceWorker.getRegistrations()).length>0)
        })""")
        census = state.get("census") or {}
        if (
            len(self.context.pages) != 1
            or len(page.frames) != 1
            or self.inflight
            or self.channels
            or state.get("service_worker") is not False
            or census.get("installed") is not True
            or census.get("unsupported_channel_used") is not False
        ):
            raise BlockedPageCaptureActionError(
                "充值只读隔离缺失或仍有活动通信通道", error_code="H5_PAYMENT_READONLY_UNAVAILABLE"
            )
        if self.page is not None and (self.page != page or self.origin != state["origin"]):
            raise BlockedPageCaptureActionError(
                "充值只读来源文档已漂移", error_code="H5_PAYMENT_READONLY_UNAVAILABLE"
            )
        # 后注册的精确处理器置顶，现有路由保留；旧定时回调在返回后仍被拦截。
        if self.page is None:
            self.page, self.origin = page, state["origin"]
            await self.context.route("**/*", self.route)
            await page.route("**/*", self.route)
        self.exit_url = None
        self.sealed_origins.add(state["origin"])
        await page.evaluate("globalThis[Symbol.for('alioth.h5.display.channels.v1')](true)")

    async def denied_count(self, page):
        state = await page.evaluate("globalThis[Symbol.for('alioth.h5.display.channels.v1')]?.()")
        return self.blocked + (state or {}).get("blocked", 0)

    def authorize_goto(self, page, url):
        if self.page == page:
            self.exit_url = urldefrag(url)[0]


async def install_h5_readonly_display_guard(context):
    existing = getattr(context, "_alioth_h5_readonly_display_guard", None)
    if isinstance(existing, H5ReadonlyDisplayGuard):
        return existing
    if not all(
        callable(getattr(context, name, None))
        for name in ("add_init_script", "route_web_socket", "route", "on")
    ):
        return None
    guard = H5ReadonlyDisplayGuard(context)
    # 必须在首个业务文档脚本运行前建立通信普查，已有页面不能事后补造证明。
    if any(page.url != "about:blank" for page in context.pages):
        return None
    await context.add_init_script(_CHANNEL_CENSUS)
    await context.route_web_socket("**/*", guard.websocket)
    context.on("page", guard.watch)
    context.on("request", lambda request: guard.inflight.add(request))
    context.on("requestfinished", lambda request: guard.inflight.discard(request))
    context.on("requestfailed", lambda request: guard.inflight.discard(request))
    for page in context.pages:
        guard.watch(page)
        if await page.evaluate("!document.body || document.body.childElementCount===0"):
            await page.evaluate(_CHANNEL_CENSUS)
    context._alioth_h5_readonly_display_guard = guard
    return guard


async def seal_h5_payment_display(page):
    from services.api.app.infrastructure.browser.page_capture import BlockedPageCaptureActionError

    guard = getattr(page.context, "_alioth_h5_readonly_display_guard", None)
    if not isinstance(guard, H5ReadonlyDisplayGuard):
        raise BlockedPageCaptureActionError(
            "充值缺少只读出站隔离", error_code="H5_PAYMENT_READONLY_UNAVAILABLE"
        )
    await guard.seal(page)
    return guard


def authorize_h5_readonly_goto(page, url):
    guard = getattr(getattr(page, "context", None), "_alioth_h5_readonly_display_guard", None)
    if isinstance(guard, H5ReadonlyDisplayGuard):
        guard.authorize_goto(page, url)
