"""Web 直登使用独立 Playwright 上下文的受限端口。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import unquote, urldefrag, urlsplit

from services.api.app.application.web_account_entry import (
    unique_account_entry,
    unique_authenticated_account_entry,
)
from services.api.app.application.web_account_preflight import (
    _large_center_overlay,
    _safe_dom_adult_entry_target,
    _safe_dom_close_target,
)
from services.api.app.application.web_login_auth_origins import WebLoginAuthOrigins
from services.api.app.application.web_login_business_result import classify_login_failure
from services.api.app.infrastructure.browser.page_capture import (
    _clear_session_observer_buffers,
    _mute_session_observers,
)
from services.api.app.infrastructure.browser.web_login_tls import verify_web_login_tls
from services.api.app.infrastructure.browser.web_user_agent import web_chrome_user_agent


@dataclass(frozen=True)
class WebLoginFrame:
    png: bytes
    url: str
    identity: str
    dom: str | None = None


class WebLoginSessionError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _same_origin(url: str, origin: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and f"https://{parsed.netloc.lower()}" == origin


def _auth_cookie_name(name: str) -> bool:
    """排除统计 SDK 会话标识，避免把访问会话误判为登录态。"""
    if re.search(
        r"(?:^|[_-])(?:sdk|analytics|tracking)[_-](?:sid|session)(?:$|[_-])",
        name,
        re.I,
    ):
        return False
    return bool(re.search(r"auth|token|session|user|login|jwt|sid", name, re.I))


_FORM_TARGETS = r"""kind => {
  // 仅在当前文档的唯一可见登录表单中选择字段和提交控件。
  const visible=e=>{
    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    if(r.width<16||r.height<16||r.left<0||r.top<0||
       r.right>innerWidth||r.bottom>innerHeight||s.display==='none'||
       s.visibility==='hidden'||Number(s.opacity)===0||s.pointerEvents==='none')
      return false;
    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    return hit===e||e.contains(hit);
  };
  const all=[...document.querySelectorAll('input')];
  const inputs=all.map((e,index)=>({e,index})).filter(x=>
    visible(x.e)&&!x.e.readOnly&&!x.e.disabled);
  const passwords=inputs.filter(x=>x.e.type==='password');
  if(passwords.length!==1) return null;
  const pass=passwords[0];
  let scope=pass.e.closest(
    'dialog,[role="dialog"],[aria-modal="true"],.login_modal,.login_dialog,.auth-modal-card,.layui-layer')||
    pass.e.closest('form');
  if(scope?.tagName==='FORM'&&!scope.querySelector(
      'button[type="submit"],input[type="submit"],[data-role="login"],'+
      '[data-action="login"],[id*="login_submit"],[class*="login-submit"],'+
      'button[class*="submit"]')) scope=scope.parentElement;
  if(!scope) {
    for(let node=pass.e.parentElement;node&&node!==document.body;
        node=node.parentElement) {
      const style=getComputedStyle(node),rect=node.getBoundingClientRect();
      if((style.position==='fixed'||style.position==='absolute'||
          /modal|popup|dialog|layer/i.test(String(node.className||'')))&&
         rect.width>=200&&node.querySelectorAll('input').length>=2) {
        scope=node;break;
      }
    }
  }
  scope=scope||document.body;
  const label=e=>[e.type,e.name,e.id,e.autocomplete,e.placeholder,
    e.getAttribute('aria-label'),...(e.labels?[...e.labels].map(x=>x.innerText):[])]
    .filter(Boolean).join(' ').toLowerCase();
  const candidates=inputs.filter(x=>x!==pass&&scope.contains(x.e)&&
    !/hidden|submit|button|checkbox|radio|file|search/.test(x.e.type)&&
    !/验证码|驗證碼|captcha|verification|code|otp/.test(label(x.e)));
  const match=x=>{
    const text=label(x.e);
    if(kind==='phone') return x.e.type==='tel'||/phone|mobile|手机|電話|手机号/.test(text);
    if(kind==='email') return x.e.type==='email'||/e-?mail|邮箱|郵箱/.test(text);
    return /user.?name|account|login.?id|账号|帳號|用户名/.test(text);
  };
  let ids=candidates.filter(match);
  if(ids.length===0&&candidates.length===1&&
     ['text','tel','number'].includes(candidates[0].e.type)&&
     scope!==document.body) ids=candidates;
  if(ids.length!==1||candidates.length!==1) return null;
  const controls=[...document.querySelectorAll(
    'button,input[type="submit"],[role="button"],div.btn,div[class*="login-submit"],[data-role="login"],[data-action="login"]')];
  const login=new RegExp(
    '^(登录|登入|登錄|立即登录|确认登录|确认|确定|继续|'+
    'log[ -]?in|sign[ -]?in|continue|confirm)$','i');
  const submits=controls.map((e,index)=>({e,index})).filter(x=>
    visible(x.e)&&scope.contains(x.e)&&
    (login.test((x.e.innerText||x.e.value||x.e.getAttribute('aria-label')||'').trim())||
     x.e.getAttribute('data-role')==='login'||
     x.e.getAttribute('data-action')==='login'||
     /(?:^|[_-])login[_-]?submit(?:$|[_-])/i.test(x.e.id||'')||
     /(?:^|[\s_-])login[\s_-]*submit(?:$|[\s_-])/i.test(String(x.e.className||''))));
  const final=submits.length===1 ? submits : submits.filter(x=>
    x.e.getAttribute('role')!=='tab'&&
    (/(?:submit|confirm|primary)/i.test(String(x.e.className||'')+' '+x.e.id)||
     x.e.getAttribute('type')==='submit'&&!!x.e.closest('form')));
  if(final.length!==1) return null;
  const action=pass.e.form?.getAttribute('action');
  if(action&&new URL(action,location.href).origin!==location.origin) return null;
  if(final[0].e.closest('a[target="_blank"]')) return null;
  return {identifier:ids[0].index,password:pass.index,submit:final[0].index};
}"""


class WebLoginSessionBrowser:
    def __init__(
        self,
        browser,
        *,
        tls_verifier=verify_web_login_tls,
        auth_policy: WebLoginAuthOrigins | None = None,
    ):
        self._browser = browser
        self._auth_policy = auth_policy
        if tls_verifier is verify_web_login_tls:
            proxy_server = getattr(browser, "_proxy_server", None)

            async def configured_tls_verifier(origin: str) -> bool:
                return await verify_web_login_tls(origin, proxy_server=proxy_server)

            self._tls_verifier = configured_tls_verifier
        else:
            self._tls_verifier = tls_verifier
        self._private_identifiers: dict[str, str] = {}
        self._isolated_sessions: dict[str, SimpleNamespace] = {}
        self._last_scrub_status: dict[str, str] = {}
        self._last_guard_summary: dict[str, tuple[tuple[str, str], ...]] = {}
        self._guard_states: dict[str, dict] = {}
        self._submit_clicks: dict[str, bool] = {}
        self._login_failure_codes: dict[str, str] = {}

    async def open_isolated(self, main_session_id: str, origin: str) -> str:
        """在同一 Chromium 中创建不共享 Cookie 的登录上下文。"""
        branch_id = f"{main_session_id}:login"
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(main_session_id, asyncio.Lock())
            async with lock:
                main = self._browser._sessions.get(main_session_id)
                if main is None or not _same_origin(str(main.page.url), origin):
                    raise WebLoginSessionError("WEB_LOGIN_ORIGIN_MISMATCH")
                if branch_id in self._isolated_sessions:
                    raise WebLoginSessionError("WEB_LOGIN_SESSION_ALREADY_OPEN")
                self._last_scrub_status.pop(branch_id, None)
                self._last_guard_summary.pop(branch_id, None)
                self._guard_states.pop(branch_id, None)
                self._submit_clicks.pop(branch_id, None)
                self._login_failure_codes.pop(branch_id, None)
                context = await main.browser.new_context(
                    ignore_https_errors=False,
                    service_workers="block",
                    viewport=main.page.viewport_size or {"width": 1920, "height": 1080},
                    user_agent=await web_chrome_user_agent(main.browser),
                )
                try:
                    await context.route_web_socket("**/*", lambda socket: socket.close())
                    guard_state = {"blocked": False, "requests": set()}
                    self._guard_states[branch_id] = guard_state
                    await context.route(
                        "**/*", self._credential_route_guard(origin, "", "", guard_state)
                    )
                    page = await context.new_page()
                    await page.goto(f"{origin}/", wait_until="domcontentloaded", timeout=30000)
                    if not _same_origin(str(page.url), origin):
                        raise WebLoginSessionError("WEB_LOGIN_ORIGIN_MISMATCH")
                except BaseException:
                    await context.close()
                    raise
                self._isolated_sessions[branch_id] = SimpleNamespace(
                    context=context,
                    page=page,
                    observers_muted=True,
                    console_logs=[],
                    network_requests=[],
                    network_inflight={},
                )
        return branch_id

    async def close_isolated(self, session_id: str) -> None:
        lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self._isolated_sessions.pop(session_id, None)
            self._private_identifiers.pop(session_id, None)
            self._login_failure_codes.pop(session_id, None)
            if session is not None:
                await session.context.close()
        self._browser._session_locks.pop(session_id, None)

    def blocked_cross_origin(self, session_id: str) -> bool:
        """读取路由拦截状态，包含提交返回后才到达的请求。"""
        return bool(self._guard_states.get(session_id, {}).get("blocked"))

    def submit_clicked(self, session_id: str) -> bool:
        return self._submit_clicks.get(session_id, False)

    def login_server_failure(self, session_id: str) -> str | None:
        """只返回认证接口的稳定失败码，不返回响应正文。"""
        return self._login_failure_codes.get(session_id)

    def blocked_request_summary(self, session_id: str) -> tuple[tuple[str, str], ...]:
        """仅返回目的源站和方法，不读取请求体。"""
        state = self._guard_states.get(session_id)
        if state is None:
            return self._last_guard_summary.get(session_id, ())
        return tuple(sorted(state["requests"]))

    async def quarantine(self, session_id: str) -> None:
        """提交不确定时销毁当前浏览器会话，防止后续普通采集读到秘密。"""
        self._private_identifiers.pop(session_id, None)
        if session_id in self._isolated_sessions:
            await self.close_isolated(session_id)
        else:
            await self._browser.close_session(session_id)

    async def _locked(self, session_id: str):
        # 调用方使用与主采集相同的槽和会话锁，禁止交叉截图与秘密窗口。
        session = self._isolated_sessions.get(session_id) or self._browser._sessions.get(session_id)
        if session is None:
            raise WebLoginSessionError("WEB_LOGIN_SESSION_MISSING")
        return session

    async def _form_target(self, page, origin: str, identifier_kind: str):
        """只接受主文档或唯一可见同源 iframe 内的登录表单。"""
        matches = []
        for frame in page.frames:
            if not _same_origin(str(frame.url), origin):
                continue
            if frame is not page.main_frame:
                host = await frame.frame_element()
                bounds = await host.bounding_box()
                if not bounds or bounds["width"] < 100 or bounds["height"] < 100:
                    continue
                uncovered = await host.evaluate("""element => {
                  const rect=element.getBoundingClientRect();
                  const hit=document.elementFromPoint(
                    rect.left+rect.width/2,rect.top+rect.height/2);
                  return hit===element||element.contains(hit);
                }""")
                if not uncovered:
                    continue
            targets = await frame.evaluate(_FORM_TARGETS, identifier_kind)
            if targets:
                matches.append((frame, targets))
        return matches[0] if len(matches) == 1 else None

    async def clear_obstructions(
        self, session_id: str, origin: str, *, adult_declared: bool = False
    ) -> str:
        """每次只关闭可唯一定位的阻断控件，重新观察后再执行下一次。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin):
                    return "WEB_LOGIN_ORIGIN_MISMATCH"
                for _ in range(12):
                    wrapper = SimpleNamespace(page=page)
                    adult = await _safe_dom_adult_entry_target(wrapper)
                    if adult is not None:
                        if not adult_declared:
                            return "WEB_LOGIN_ADULT_DECLARATION_REQUIRED"
                        box = adult
                    else:
                        password_visible = await page.evaluate("""() =>
                          [...document.querySelectorAll('input[type="password"]')]
                            .some(e => {
                              const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                              const hit=document.elementFromPoint(
                                r.left+r.width/2,r.top+r.height/2);
                              return r.width>=16&&r.height>=16&&s.display!=='none'&&
                                s.visibility!=='hidden'&&(hit===e||e.contains(hit));
                            })""")
                        if password_visible:
                            return "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
                        box = await _safe_dom_close_target(wrapper, center_overlay_only=True)
                        if box is None:
                            box = await _center_popup_close_target(page)
                    if box is None:
                        if await _large_center_overlay(wrapper):
                            await page.wait_for_timeout(650)
                            continue
                        await page.wait_for_timeout(350)
                        if await _center_popup_close_target(
                            page
                        ) is not None or await _large_center_overlay(wrapper):
                            continue
                        return "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
                    size = page.viewport_size
                    if not size:
                        return "WEB_LOGIN_VIEWPORT_MISSING"
                    await page.mouse.click(
                        (box[0] + box[2]) * size["width"] / 2,
                        (box[1] + box[3]) * size["height"] / 2,
                    )
                    await page.wait_for_timeout(450)
                    if not _same_origin(str(page.url), origin):
                        return "WEB_LOGIN_ORIGIN_MISMATCH"
                return "WEB_LOGIN_OBSTRUCTION_LIMIT"

    async def account_obstruction_present(self, session_id: str, origin: str) -> bool:
        """检查晚到的居中遮挡，供账户截图后决定是否补清障。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                if not _same_origin(str(session.page.url), origin):
                    return True
                return bool(await _large_center_overlay(SimpleNamespace(page=session.page)))

    async def wait_account_loading(self, session_id: str, origin: str) -> bool:
        """有界等待账户导航后的可见加载弹层消失。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                for _ in range(16):
                    page = session.page
                    if not _same_origin(str(page.url), origin):
                        return False
                    try:
                        loading = await page.evaluate(r"""() =>
                          [...document.querySelectorAll(
                            'dialog,[role="dialog"],[aria-modal="true"]')]
                            .some(e=>{
                              const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                              if(r.width<16||r.height<16||r.bottom<=0||
                                 r.top>=innerHeight||s.display==='none'||
                                 s.visibility==='hidden'||Number(s.opacity)===0)
                                return false;
                              const marker=[e.className,e.getAttribute('aria-busy')]
                                .join(' ');
                              if(!/loading|spinner|aria-busy|true/i.test(marker)&&
                                 !/^(?:加载中|正在加载|loading)(?:\.{0,3}|…)?$/i
                                   .test((e.innerText||'').trim())) return false;
                              const hit=document.elementFromPoint(
                                Math.max(0,Math.min(innerWidth-1,r.left+r.width/2)),
                                Math.max(0,Math.min(innerHeight-1,r.top+r.height/2)));
                              return hit===e||e.contains(hit);
                            })
                        """)
                    except Exception:  # noqa: BLE001 - 页面导航中可短暂失去执行上下文
                        await asyncio.sleep(0.3)
                        continue
                    if not loading:
                        return True
                    await asyncio.sleep(0.3)
                return False

    async def session_evidence(self, session_id: str, origin: str) -> dict[str, object]:
        """只返回会话 Cookie 是否变化等非秘密结果，不返回 Cookie 值。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                if not _same_origin(str(session.page.url), origin):
                    raise WebLoginSessionError("WEB_LOGIN_ORIGIN_MISMATCH")
                cookies = await session.context.cookies(origin)
                signatures = frozenset(
                    hashlib.sha256(f"{item['name']}={item['value']}".encode()).hexdigest()
                    for item in cookies
                    if _auth_cookie_name(item.get("name", ""))
                )
                return {"auth_cookie_signatures": signatures, "url": str(session.page.url)}

    async def wait(self, session_id: str, milliseconds: int) -> None:
        session = await self._locked(session_id)
        await session.page.wait_for_timeout(milliseconds)

    async def prepare_authenticated_view(self, session_id: str, origin: str) -> bool:
        """残留秘密时仅在隔离上下文打开全新同源文档，旧页绝不截图。"""
        if session_id not in self._isolated_sessions:
            return False
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                fresh = await session.context.new_page()
                try:
                    await fresh.goto(origin + "/", wait_until="domcontentloaded", timeout=30000)
                    if not _same_origin(str(fresh.url), origin):
                        return False
                    old = session.page
                    session.page = fresh
                    self._last_scrub_status[session_id] = "clear"
                    await old.close()
                    return True
                except Exception:  # noqa: BLE001 - 导航错误可能携带不可信 URL
                    return False
                finally:
                    if session.page is not fresh:
                        await fresh.close()

    async def capture(
        self, session_id: str, origin: str, *, include_dom: bool = False
    ) -> WebLoginFrame:
        if self._last_scrub_status.get(session_id) not in {None, "clear"}:
            raise WebLoginSessionError("WEB_LOGIN_CAPTURE_AFTER_SECRET_RESIDUE")
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin):
                    raise WebLoginSessionError("WEB_LOGIN_ORIGIN_MISMATCH")
                capture_url = str(page.url)
                identity = await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                )
                mask = [
                    frame.locator("input,textarea,[contenteditable=true]")
                    for frame in page.frames
                    if _same_origin(str(frame.url), origin)
                ]
                private = self._private_identifiers.get(session_id)
                if private:
                    # 昵称可能给账号加前缀，截图仍须遮盖包含账号的可见文字。
                    mask.append(page.get_by_text(private, exact=False))
                png = await page.screenshot(full_page=False, mask=mask, timeout=10000)
                dom = None
                if include_dom:
                    dom = await page.evaluate(
                        """() => {
                          // 仅保留账户复核所需的页面结构；敏感控件与可执行内容不出浏览器。
                          const clone=document.body.cloneNode(true);
                          clone.querySelectorAll(
                            'script,style,noscript,template,form,input,textarea,select,option,'+
                            'iframe,object,embed,canvas,video,audio,source,svg'
                          ).forEach(node=>node.remove());
                          const allowed=new Set(
                            ['id','class','role','aria-label','title','alt','href']);
                          for(const node of [clone,...clone.querySelectorAll('*')]) {
                            for(const attr of [...node.attributes]) {
                              if(!allowed.has(attr.name)) {
                                node.removeAttribute(attr.name);
                                continue;
                              }
                              if(attr.name==='href') {
                                try {
                                  const target=new URL(attr.value,location.href);
                                  if(target.origin!==location.origin ||
                                     !['http:','https:'].includes(target.protocol)) {
                                    node.removeAttribute(attr.name);
                                  } else {
                                    node.setAttribute('href',target.pathname);
                                  }
                                } catch { node.removeAttribute(attr.name); }
                              }
                            }
                          }
                          return clone.outerHTML;
                        }"""
                    )
                    private = self._private_identifiers.get(session_id)
                    if private:
                        dom = dom.replace(private, "[REDACTED_IDENTIFIER]")
                    if not dom or len(dom.encode("utf-8")) > 2 * 1024 * 1024:
                        raise WebLoginSessionError("WEB_LOGIN_ACCOUNT_DOM_INVALID")
                if identity != await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                ) or str(page.url) != capture_url:
                    raise WebLoginSessionError("WEB_LOGIN_DOCUMENT_CHANGED")
                return WebLoginFrame(png=png, url=capture_url, identity=identity, dom=dom)

    async def account_auth_state(
        self, session_id: str, origin: str, frame: WebLoginFrame
    ) -> str:
        """只用头像展开后的当前可见控件复核新文档是否仍为游客。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin) or str(page.url) != frame.url:
                    return "unknown"
                identity = await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                )
                if identity != frame.identity:
                    return "unknown"
                state = await page.evaluate(
                    r"""identifier => {
                      const visible=e=>{
                        const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                        if(r.width<10||r.height<10||r.left<0||r.top<0||
                           r.right>innerWidth||r.bottom>innerHeight||
                           s.display==='none'||s.visibility==='hidden'||
                           Number(s.opacity)===0) return false;
                        const hit=document.elementFromPoint(
                          r.left+r.width/2,r.top+r.height/2);
                        return hit===e||e.contains(hit);
                      };
                      const labels=[...document.querySelectorAll(
                        'a,button,[role="button"]')].filter(visible)
                        .map(e=>[e.innerText,e.getAttribute('aria-label'),e.title]
                          .filter(Boolean).join(' ').replace(/\s+/g,' ').trim())
                        .filter(Boolean);
                      const logoutText=/登出|退出登录|退出帐号|退出账号|注销登录/i;
                      const logoutEnglish=/log\s*out|sign\s*out/i;
                      const hasLogout=v=>logoutText.test(v)||logoutEnglish.test(v);
                      const guestText=/登录|登入|登錄|注册|註冊|log\s*in|sign\s*in|register/i;
                      const logout=labels.some(hasLogout)||
                        [...document.querySelectorAll(
                          '[class*="logout"],[id*="logout"],'
                          +'[class*="signout"],[id*="signout"]')]
                          .filter(visible).some(e=>{
                            const label=[e.innerText,e.getAttribute('aria-label'),e.title]
                              .filter(Boolean).join(' ').replace(/\s+/g,' ').trim();
                            return hasLogout(label)||
                              /^(?:退出|登出|log\s*out|sign\s*out)$/i.test(label);
                          });
                      const accountSpans=[...document.querySelectorAll('span')]
                        .filter(e=>{
                          const r=e.getBoundingClientRect();
                          return visible(e)&&r.left>=innerWidth*.55&&
                            r.top<=innerHeight*.45&&
                            e.closest('header,nav,aside,[role="dialog"],'
                              +'[class*="user"],[class*="account"],'
                              +'[class*="login"],[class*="profile"]');
                        })
                        .map(e=>(e.innerText||'').replace(/\s+/g,' ').trim());
                      const guest=labels.some(v=>!hasLogout(v)&&guestText.test(v))||
                        accountSpans.some(v=>
                          /^(?:登录|登入|登錄|注册|註冊|log\s*in|sign\s*in|register)$/i
                            .test(v));
                      const ownIdentifier=!!identifier&&
                        [...document.querySelectorAll('a,button,span')]
                          .some(e=>{
                            if(!visible(e)||
                               (e.innerText||'').trim()!==identifier) return false;
                            const r=e.getBoundingClientRect();
                            return !!e.closest('header,nav,[role="banner"]')||
                              r.left>=innerWidth*.55&&r.top<=innerHeight*.3&&
                              !e.closest('main,article');
                          });
                      const ownProfile=!!identifier&&
                        /(?:^|\/)(?:user|account|member|profile)(?:\/|$)/i
                          .test(location.pathname)&&
                        ownIdentifier&&
                        [
                          /我的档案|个人资料|my\s*profile/i,
                          /账号信息|账户信息|用户名|account\s*information|username/i,
                          /修改密码|change\s*password/i
                        ].filter(pattern=>pattern.test(document.body?.innerText||''))
                          .length>=2;
                      const ownDrawer=[...document.querySelectorAll(
                        'aside,[role="dialog"],[class*="sidemenu"],'
                        +'[class*="user-menu"],[class*="account-menu"]')]
                        .some(root=>{
                          const nickname=[...root.querySelectorAll(
                            '[class*="nickname"],[class*="user-name"],'
                            +'[data-role="nickname"]')]
                            .some(e=>{
                              const value=(e.innerText||'').replace(/\s+/g,' ').trim();
                              return visible(e)&&value.length>0&&value.length<=80&&
                                !guestText.test(value);
                            });
                          const balance=[...root.querySelectorAll('div,span')]
                            .some(e=>{
                              const value=(e.innerText||'').replace(/\s+/g,' ').trim();
                              return visible(e)&&value.length<=60&&
                                /(?:余额|balance)\s*[:：]?\s*[\d０-９]/i.test(value);
                            });
                          const privateLink=[...root.querySelectorAll('a[href]')]
                            .some(e=>{
                              if(!visible(e)) return false;
                              let target;
                              try { target=new URL(e.getAttribute('href'),location.href); }
                              catch { return false; }
                              return target.origin===location.origin&&
                                /消息中心|我的消息|评论回复|我的订单|message|notification|comment\s*repl/i
                                  .test(e.innerText||'');
                            });
                          return nickname&&balance&&privateLink;
                        });
                      const ownDashboard=(()=>{
                        if(!identifier||identifier.length<3) return false;
                        const accountMain=[...document.querySelectorAll(
                          'main,[role="main"]')].find(root=>{
                          const activeTab=[...root.querySelectorAll(
                            'li,[role="tab"],a,button')].some(e=>
                            visible(e)&&!e.closest('article')&&
                            /^(?:用户中心|个人中心|我的主页|my account|my profile)$/i
                              .test((e.innerText||'').replace(/\s+/g,' ').trim())&&
                            (e.getAttribute('aria-selected')==='true'||
                             e.getAttribute('aria-current')==='page'||
                             /(?:^|[\s_-])(?:is-active|active|selected)(?:$|[\s_-])/i
                               .test(String(e.className||''))));
                          if(!activeTab) return false;
                          return [...root.querySelectorAll(
                            '[class*="nickname"],[class*="user-name"],'
                            +'[data-role="nickname"]')].some(name=>{
                            if(!visible(name)||name.closest(
                              'article,[class*="feed"],[class*="post"]'))
                              return false;
                            const value=(name.innerText||'')
                              .replace(/\s+/g,' ').trim();
                            if(value!==identifier&&!(
                              value.endsWith('@'+identifier)&&
                              /^[A-Za-z0-9_-]{1,20}$/.test(
                                value.slice(0,-identifier.length-1)))) return false;
                            // 昵称和账户指标必须位于同一可见的账户摘要区域。
                            for(let area=name.parentElement;area&&area!==root;
                                area=area.parentElement){
                              if(!/user|account|profile|member|dashboard/i
                                .test(String(area.className||''))) continue;
                              if([...area.querySelectorAll('div,span,small')]
                                .some(e=>visible(e)&&
                                  /^(?:当前积分|我的积分|账户余额|当前余额|points|balance)$/i
                                    .test((e.innerText||'')
                                      .replace(/\s+/g,' ').trim())&&
                                  /\d/.test(e.parentElement?.innerText||'')))
                                return true;
                            }
                            return false;
                          });
                        });
                        if(!accountMain) return false;
                        return [...document.querySelectorAll('a[href]')].some(e=>{
                          if(!visible(e)||e.closest(
                            'article,[class*="feed"],[class*="post"]')||
                            !(accountMain.contains(e)||e.closest(
                              'header,nav,aside,[role="navigation"]')))
                            return false;
                          let target;
                          try { target=new URL(e.getAttribute('href'),location.href); }
                          catch { return false; }
                          return target.origin===location.origin&&
                            !target.search&&!target.hash&&
                            /^(?:\/(?:user|account|member)\/profile|\/profile|\/my)\/?$/i
                              .test(target.pathname);
                        });
                      })();
                      return logout&&!guest ? 'authenticated' :
                        guest&&!logout ? 'guest' :
                        (ownProfile||ownDrawer||ownDashboard)&&!guest ?
                          'authenticated' :
                        'unknown';
                    }""",
                    self._private_identifiers.get(session_id),
                )
                if str(page.url) != frame.url or identity != await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                ):
                    return "unknown"
                return state if state in {"authenticated", "guest"} else "unknown"

    async def account_actions(
        self, session_id: str, origin: str, frame: WebLoginFrame, ocr_hits=()
    ) -> tuple[dict, ...]:
        """只列举当前可见、无遮挡、同源链接；OCR 仅补充链接文字线索。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin) or str(page.url) != frame.url:
                    return ()
                if frame.identity != await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                ):
                    return ()
                return tuple(
                    await page.evaluate(
                        r"""hits => {
                  const result=[];
                  const norm=t=>String(t||'').replace(/\s+/g,' ').trim();
                  const kind=(t,path)=>{
                    if(/充值记录|充值历史|储值记录|recharge[-_ ]?(?:record|history)/i
                         .test(t+' '+path)) return null;
                    if(/钱包|wallet/i.test(t)||/\/wallet(?:[/.]|$)/i.test(path))
                      return 'wallet';
                    if(/去充值|立即充值|充值|recharge|top[ -]?up/i.test(t)||
                       /\/(?:recharge|top[-_]?up|deposit)(?:[/.]|$)/i.test(path)) return 'recharge';
                    if(/登录|登入|登錄|注册|註冊|退出|登出/i.test(t)||
                       /log[ -]?in|sign[ -]?in|register|logout/i.test(t)||
                       /\/(?:login|register|logout|sign[-_]?in)(?:[/.]|$)/i.test(path))
                      return null;
                    const profileText=/我的主页|我的主頁|个人中心|個人中心/i.test(t)||
                      /我的账户|我的帳戶|my\s*(?:home|account)|profile/i.test(t);
                    const accountPath=/\/my(?:\/index)?\/?(?:\.html?)?$/i.test(path)||
                      /\/(?:usercenter|profile|account)\/?(?:index(?:\.html?)?)?$/i.test(path)||
                      /\/user\/profile\/?(?:index(?:\.html?)?)?$/i.test(path);
                    return profileText||accountPath ? 'profile' : null;
                  };
                  for(const a of document.querySelectorAll('a[href]')) {
                    if(a.closest('form')||a.hasAttribute('download')) continue;
                    const r=a.getBoundingClientRect(),s=getComputedStyle(a);
                    if(r.width<12||r.height<12||r.left<0||r.top<0||
                       r.right>innerWidth||r.bottom>innerHeight||s.display==='none'||
                       s.visibility==='hidden'||Number(s.opacity)===0) continue;
                    const x=r.left+r.width/2,y=r.top+r.height/2;
                    const hit=document.elementFromPoint(x,y);
                    if(!(hit===a||a.contains(hit))) continue;
                    const href=new URL(a.getAttribute('href'),location.href);
                    if(href.origin!==location.origin||href.protocol!=='https:') continue;
                    const image=[...a.querySelectorAll('img')]
                      .map(e=>e.alt||e.title||'').join(' ');
                    const ocr=(Array.isArray(hits)?hits:[]).filter(h=>{
                      const b=h.box;
                      if(!Array.isArray(b)||b.length!==4) return false;
                      const px=(b[0]+b[2])*innerWidth/2;
                      const py=(b[1]+b[3])*innerHeight/2;
                      return px>=r.left&&px<=r.right&&py>=r.top&&py<=r.bottom;
                    }).map(h=>h.text).join(' ');
                    const label=norm([a.innerText,a.getAttribute('aria-label'),
                      a.title,image,ocr].filter(Boolean).join(' '));
                    const action=kind(label,href.pathname);
                    if(action==='profile'&&
                       href.href.split('#')[0]===location.href.split('#')[0]) continue;
                    if(action&&result.some(item=>item.kind===action&&item.href===href.href))
                      continue;
                    if(action&&result.filter(item=>item.kind===action).length<2)
                      result.push({kind:action,box:[r.left/innerWidth,
                        r.top/innerHeight,r.right/innerWidth,r.bottom/innerHeight],
                        href:href.href,label:label.slice(0,80)});
                  }
                  return result;
                }""",
                        list(ocr_hits),
                    )
                )

    async def reveal_account_action(
        self, session_id: str, origin: str, frame: WebLoginFrame, kind: str
    ) -> str:
        """仅滚动唯一同源语义入口，随后重新截图并定位。"""
        if kind not in {"wallet", "recharge", "profile"}:
            return "absent"
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin) or str(page.url) != frame.url:
                    return "absent"
                identity = await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                )
                if identity != frame.identity:
                    return "absent"
                candidates = await page.evaluate(
                    r"""wanted => {
                  const result=[];
                  const classify=(t,path)=>{
                    if(/充值记录|充值历史|储值记录|recharge[-_ ]?(?:record|history)/i
                         .test(t+' '+path)) return null;
                    if(/钱包|wallet/i.test(t)||/\/wallet(?:[/.]|$)/i.test(path))
                      return 'wallet';
                    if(/去充值|立即充值|充值|recharge|top[ -]?up/i.test(t)||
                       /\/(?:recharge|top[-_]?up|deposit)(?:[/.]|$)/i.test(path)) return 'recharge';
                    if(/登录|登入|登錄|注册|註冊|退出|登出/i.test(t)||
                       /log[ -]?in|sign[ -]?in|register|logout/i.test(t)||
                       /\/(?:login|register|logout|sign[-_]?in)(?:[/.]|$)/i.test(path))
                      return null;
                    const profileText=/我的主页|我的主頁|个人中心|個人中心/i.test(t)||
                      /我的账户|我的帳戶|my\s*(?:home|account)|profile/i.test(t);
                    const accountPath=/\/my(?:\/index)?\/?(?:\.html?)?$/i.test(path)||
                      /\/(?:usercenter|profile|account)\/?(?:index(?:\.html?)?)?$/i.test(path)||
                      /\/user\/profile\/?(?:index(?:\.html?)?)?$/i.test(path);
                    return profileText||accountPath ? 'profile' : null;
                  };
                  const links=[...document.querySelectorAll('a[href]')];
                  links.forEach((a,index)=>{
                    if(a.closest('form')||a.hasAttribute('download')) return;
                    const r=a.getBoundingClientRect(),s=getComputedStyle(a);
                    if(r.width<12||r.height<12||s.display==='none'||
                       s.visibility==='hidden'||Number(s.opacity)===0) return;
                    const href=new URL(a.getAttribute('href'),location.href);
                    if(href.origin!==location.origin||href.protocol!=='https:') return;
                    if(wanted==='profile'&&
                       href.href.split('#')[0]===location.href.split('#')[0]) return;
                    const image=[...a.querySelectorAll('img')]
                      .map(e=>e.alt||e.title||'').join(' ');
                    const label=[a.innerText,a.getAttribute('aria-label'),a.title,image]
                      .filter(Boolean).join(' ');
                    if(classify(label,href.pathname)!==wanted) return;
                    const visible=r.left>=0&&r.top>=0&&r.right<=innerWidth&&
                      r.bottom<=innerHeight;
                    const hit=visible?document.elementFromPoint(
                      r.left+r.width/2,r.top+r.height/2):null;
                    const candidate={index,href:href.href,
                      visible:visible&&(hit===a||a.contains(hit))};
                    const prior=result.findIndex(item=>item.href===href.href);
                    if(prior>=0) {
                      if(candidate.visible) result[prior]=candidate;
                    } else result.push(candidate);
                  });
                  return result.slice(0,3);
                }""",
                    kind,
                )
                if len(candidates) > 1:
                    return "ambiguous"
                if not candidates:
                    return "absent"
                if candidates[0]["visible"]:
                    return "visible"
                try:
                    await (
                        page.locator("a[href]")
                        .nth(candidates[0]["index"])
                        .scroll_into_view_if_needed(timeout=4000)
                    )
                    await page.wait_for_timeout(350)
                except Exception:  # noqa: BLE001 - 页面元素可能在滚动中消失
                    return "absent"
                if str(page.url) != frame.url or identity != await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                ):
                    return "absent"
                return "revealed"

    async def confirm_wallet_page(
        self, session_id: str, origin: str, frame: WebLoginFrame, ocr_hits=()
    ) -> bool:
        """只将可见钱包标题与数字余额共同成立的页面签为钱包页。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin) or str(page.url) != frame.url:
                    return False
                identity = await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                )
                if identity != frame.identity:
                    return False
                return bool(await page.evaluate(
                    r"""() => {
                  const visible=e=>{
                    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                    if(r.width<12||r.height<12||r.bottom<=0||r.top>=innerHeight||
                       s.display==='none'||s.visibility==='hidden') return false;
                    const x=Math.max(0,Math.min(innerWidth-1,r.left+r.width/2));
                    const y=Math.max(0,Math.min(innerHeight-1,r.top+r.height/2));
                    const hit=document.elementFromPoint(x,y);
                    return hit===e||e.contains(hit);
                  };
                  const heading=[...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
                    .some(e=>visible(e)&&/钱包|我的资产|wallet|my assets/i
                      .test(e.innerText||e.textContent||''));
                  const balance=[...document.querySelectorAll('div,span,p,li')]
                    .some(e=>{
                      const value=(e.innerText||'').replace(/\s+/g,' ').trim();
                      return value.length<=80&&visible(e)&&
                        /(?:余额|balance)\s*[:：]?\s*[\d０-９]/i.test(value);
                    });
                  const path=/wallet|balance|assets/i.test(location.pathname);
                  const body=document.body?.innerText||'';
                  const topup=/充值金额|充值数量|选择充值金额|top[ -]?up amount/i
                    .test(body)&&/支付方式|付款方式|payment method/i.test(body);
                  return balance&&(heading||path)&&!topup;
                }"""
                ))

    async def confirm_recharge_page(
        self, session_id: str, origin: str, frame: WebLoginFrame, ocr_hits=()
    ) -> bool:
        """只把当前隔离会话中可见的独立充值主体签为充值页。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin) or str(page.url) != frame.url:
                    return False
                if frame.identity != await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                ):
                    return False
                return bool(
                    await page.evaluate(
                        r"""hits => {
                  const meaningful=t=>
                    /充值|储值|recharge|top[ -]?up|deposit/i.test(t)&&
                    !/充值记录|充值历史|储值记录|recharge[-_ ]?(?:record|history)/i
                      .test(t);
                  const visible=e=>{
                    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                    if(r.width<12||r.height<12||r.bottom<=0||r.top>=innerHeight||
                       s.display==='none'||s.visibility==='hidden') return false;
                    const x=Math.max(0,Math.min(innerWidth-1,r.left+r.width/2));
                    const y=Math.max(0,Math.min(innerHeight-1,r.top+r.height/2));
                    const hit=document.elementFromPoint(x,y);
                    return hit===e||e.contains(hit);
                  };
                  const heading=[...document.querySelectorAll(
                    'main h1,main h2,main h3,[role="main"] h1,'+
                    '[role="main"] h2,h1,h2,h3,[role="heading"]')]
                    .some(e=>!e.closest('nav,header,footer')&&visible(e)&&
                      meaningful(e.innerText||e.textContent||''));
                  const path=decodeURIComponent(location.pathname);
                  if(/充值记录|充值历史|储值记录|recharge[-_ ]?(?:record|history)/i
                       .test(path)) return false;
                  const route=/recharge|top[ -]?up|deposit|充值|储值/i
                    .test(path);
                  const body=document.body?.innerText||'';
                  const ocrAmount=(Array.isArray(hits)?hits:[]).some(h=>{
                    const b=h.box;
                    return Array.isArray(b)&&b.length===4&&
                      b[1]>=0.12&&b[3]<=0.85&&meaningful(h.text||'');
                  });
                  const amount=/选择充值金额|充值金额|充值数量|金币充值|充值套餐/i
                    .test(body)||
                    /choose\s*(?:recharge\s*)?amount|top[ -]?up amount/i
                      .test(body);
                  const payment=/支付方式|付款方式|选择支付方式|payment\s*method/i
                    .test(body);
                  const submit=/立即充值|确认充值|去支付|pay\s*now/i.test(body);
                  return (heading||route)&&(amount||ocrAmount)&&
                    (payment||submit);
                }""",
                        list(ocr_hits),
                    )
                )

    async def click_account_action(
        self, session_id: str, origin: str, frame: WebLoginFrame, action: dict
    ) -> bool:
        """账户支线只允许已确认的同源 GET 链接导航，拦截一切提交。"""
        if (
            action.get("kind") not in {"wallet", "recharge", "profile"}
            or not _valid_box(action.get("box"))
        ):
            return False
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin) or str(page.url) != frame.url:
                    return False
                if frame.identity != await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                ):
                    return False
                box = action["box"]
                x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                target = await page.evaluate(
                    r"""point => {
                  const e=document.elementFromPoint(point[0]*innerWidth,point[1]*innerHeight);
                  const a=e?.closest('a[href]');
                  if(!a||a.closest('form')||a.hasAttribute('download')) return null;
                  const href=new URL(a.getAttribute('href'),location.href);
                  if(href.origin!==location.origin||href.protocol!=='https:') return null;
                  return href.href;
                }""",
                    [x, y],
                )
                if target != action.get("href"):
                    return False

                surface_script = r"""kind => {
                  const visible=e=>{
                    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                    if(r.width<12||r.height<12||r.bottom<=0||r.top>=innerHeight||
                       s.display==='none'||s.visibility==='hidden') return false;
                    const x=Math.max(0,Math.min(innerWidth-1,r.left+r.width/2));
                    const y=Math.max(0,Math.min(innerHeight-1,r.top+r.height/2));
                    const hit=document.elementFromPoint(x,y);
                    return hit===e||e.contains(hit);
                  };
                  const semantic=kind==='wallet'
                    ?/钱包|余额|wallet|balance/i
                    :/充值|支付方式|top[ -]?up|recharge|deposit/i;
                  return [...document.querySelectorAll(
                    'dialog,[role="dialog"],[aria-modal="true"],'
                    +'[class*="popup"],[class*="modal"],[class*="drawer"]')]
                    .filter(e=>visible(e)&&semantic.test(e.innerText||''))
                    .map(e=>{
                      const text=(e.innerText||'').replace(/\s+/g,' ').slice(0,2048);
                      let hash=0;
                      for(const char of text) hash=(hash*31+char.charCodeAt(0))|0;
                      const r=e.getBoundingClientRect();
                      return [Math.round(r.x),Math.round(r.y),text.length,hash].join(':');
                    }).join('|');
                }"""
                before_surface = await page.evaluate(surface_script, action["kind"])

                async def navigation_guard(route):
                    parsed = urlsplit(str(route.request.url))
                    request_origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
                    if (
                        route.request.method.upper() not in {"GET", "HEAD"}
                        or request_origin != origin
                    ):
                        await route.abort()
                    else:
                        await route.fallback()

                await session.context.route("**/*", navigation_guard)
                old_pages = set(session.context.pages)
                opened_new_page = False
                try:
                    size = page.viewport_size
                    if not size:
                        return False
                    await page.mouse.click(x * size["width"], y * size["height"])
                    for _ in range(10):
                        await asyncio.sleep(0.35)
                        new_pages = set(session.context.pages) - old_pages
                        if len(new_pages) > 1:
                            return False
                        if len(new_pages) == 1 and not opened_new_page:
                            opened = new_pages.pop()
                            await opened.wait_for_load_state(
                                "domcontentloaded", timeout=10000
                            )
                            if not _same_origin(str(opened.url), origin):
                                await opened.close()
                                return False
                            session.page = opened
                            await page.close()
                            opened_new_page = True
                        current_url = str(session.page.url)
                        if not _same_origin(current_url, origin):
                            return False
                        if opened_new_page or (
                            urldefrag(current_url).url != urldefrag(frame.url).url
                        ):
                            return True
                        try:
                            after_surface = await session.page.evaluate(
                                surface_script, action["kind"]
                            )
                        except Exception:  # noqa: BLE001 - 跳转中短暂失去执行上下文
                            continue
                        if after_surface and after_surface != before_surface:
                            return True
                    return False
                except Exception:  # noqa: BLE001 - 页面错误可能携带不可信内容
                    return False
                finally:
                    await session.context.unroute("**/*", navigation_guard)

    async def form_ready(self, session_id: str, origin: str, identifier_kind: str) -> bool:
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                if not _same_origin(str(session.page.url), origin):
                    return False
                return await self._form_target(session.page, origin, identifier_kind) is not None

    async def recover_authenticated_entry(self, session_id: str, origin: str):
        """新文档水合期间等待登录后的资料入口，避免抢先点击游客登录。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin):
                    return None
                for _ in range(11):
                    profile = await unique_authenticated_account_entry(
                        SimpleNamespace(page=page)
                    )
                    if profile is not None:
                        return tuple(profile["box"])
                    await page.wait_for_timeout(500)
                generic = await unique_account_entry(SimpleNamespace(page=page))
                if generic is not None:
                    return tuple(generic["box"])
        return await self.recover_entry(session_id, origin)

    async def recover_entry(self, session_id: str, origin: str):
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                if not _same_origin(str(session.page.url), origin):
                    return None
                for _ in range(10):
                    candidate = await unique_account_entry(SimpleNamespace(page=session.page))
                    if candidate is not None:
                        return tuple(candidate["box"])
                    await session.page.wait_for_timeout(600)
                return await session.page.evaluate(r"""() => {
                  const exact=
                  /^(?:\u767b\u5f55|\u767b\u5165|\u767b\u9304|log[ -]?in|sign[ -]?in)$/i;
                  const matches=[...document.querySelectorAll('a,button,[role="button"]')]
                    .map(e=>{
                      const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                      if(r.width<16||r.height<16||r.left<0||r.top<0||
                         r.right>innerWidth||r.bottom>innerHeight||
                         s.display==='none'||s.visibility==='hidden'||
                         s.pointerEvents==='none'||e.closest('form')) return null;
                      if(!(e.closest('header,nav,[role="navigation"]')||
                           r.left>=innerWidth*.55&&r.top<=innerHeight*.3)) return null;
                      const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                      if(!(hit===e||e.contains(hit))) return null;
                      const text=(e.innerText||e.getAttribute('aria-label')||'').trim();
                      const direct=exact.test(text);
                      const structure=[e.className,e.id,...[...e.querySelectorAll('img')]
                        .map(img=>[img.className,img.alt].join(' '))].join(' ');
                      const avatar=!direct&&/user|account|profile|member|login/i.test(structure)&&
                        !!e.querySelector('img,svg')&&r.left>=innerWidth*.55&&
                        r.top<=innerHeight*.3;
                      if(!direct&&!avatar) return null;
                      const href=e.getAttribute('href');
                      if(href&&new URL(href,location.href).origin!==location.origin) return null;
                      return {box:[r.left/innerWidth,r.top/innerHeight,
                        r.right/innerWidth,r.bottom/innerHeight],direct};
                    }).filter(Boolean);
                  const direct=matches.filter(item=>item.direct);
                  if(direct.length===1) return direct[0].box;
                  return matches.length===1 ? matches[0].box : null;
                }""")

    async def recover_login(self, session_id: str, origin: str):
        """账户弹层中优先找唯一的登录或密码登录切换控件。"""
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin):
                    return None
                return await page.evaluate(r"""() => {
                  const visible=e=>{
                    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                    if(r.width<16||r.height<16||r.left<0||r.top<0||
                       r.right>innerWidth||r.bottom>innerHeight||s.display==='none'||
                       s.visibility==='hidden'||Number(s.opacity)===0) return null;
                    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                    return hit===e||e.contains(hit) ? r : null;
                  };
                  if([...document.querySelectorAll('input[type="password"]')]
                     .some(e=>visible(e))) return null;
                  const login=/^(?:登录|登入|登錄|密码登录|账号登录|log[ -]?in|sign[ -]?in)$/i;
                  const options=[...document.querySelectorAll(
                    'a,button,[role="button"],[role="tab"],'+
                    'span[class*="login"],span[id*="login"]')].map(e=>{
                      const r=visible(e);
                      const label=(e.innerText||e.getAttribute('aria-label')||'')
                        .trim().replace(/\s+/g,' ');
                      if(e.tagName==='SPAN'&&
                         (!/(?:login|sign.?in)/i.test(String(e.className||'')+' '+e.id)||
                          !e.closest('[class*="user"],[class*="account"],'+
                            '[class*="menu"],[class*="profile"]'))) return null;
                      if(!r||!login.test(label)||e.closest('form')&&
                         e.getAttribute('type')==='submit') return null;
                      return {e,box:[r.left/innerWidth,r.top/innerHeight,
                        r.right/innerWidth,r.bottom/innerHeight]};
                    }).filter(Boolean);
                  const leaves=options.filter(item=>!options.some(other=>
                    other!==item&&item.e.contains(other.e)));
                  if(leaves.length===1) return leaves[0].box;
                  if(leaves.length>1) return null;
                  const account=[...document.querySelectorAll('a')].map(e=>{
                    const r=visible(e),href=e.getAttribute('href');
                    const label=(e.innerText||'').trim().replace(/\s+/g,' ');
                    if(!r||!href||!/^\/usercenter\/|^\/account\/|^\/profile\//i.test(href)||
                       !/^(?:我的|个人中心|我的账户|我的账号|my account|profile)/i.test(label))
                      return null;
                    if(new URL(href,location.href).origin!==location.origin) return null;
                    return [r.left/innerWidth,r.top/innerHeight,
                      r.right/innerWidth,r.bottom/innerHeight];
                  }).filter(Boolean);
                  return account.length===1 ? account[0] : null;
                }""")

    async def click(self, session_id: str, origin: str, frame: WebLoginFrame, box) -> None:
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                if not _same_origin(str(page.url), origin) or str(page.url) != frame.url:
                    raise WebLoginSessionError("WEB_LOGIN_ORIGIN_MISMATCH")
                current = await page.evaluate(
                    "() => String(performance.timeOrigin) + ':' + location.origin"
                )
                if current != frame.identity:
                    raise WebLoginSessionError("WEB_LOGIN_DOCUMENT_CHANGED")
                if not _valid_box(box):
                    raise WebLoginSessionError("WEB_LOGIN_TARGET_INVALID")
                x = (box[0] + box[2]) / 2
                y = (box[1] + box[3]) / 2
                permitted = await page.evaluate(
                    """point => {
                      const e=document.elementFromPoint(point[0]*innerWidth,point[1]*innerHeight);
                      const target=e?.closest('a,button,[role="button"],[role="tab"],'+
                        'span[class*="login"],span[id*="login"]');
                      if(!target||target.closest('form')&&target.type==='submit') return false;
                      const href=target.getAttribute('href');
                      if(href&&!href.startsWith('#')&&
                         !/^javascript:(?:;|void\\(0\\);?)$/i.test(href.trim())&&
                         new URL(href,location.href).origin!==location.origin) return false;
                      const label=[target.className,target.id,target.getAttribute('aria-label'),
                        target.getAttribute('title')].join(' ');
                      if(/advert|banner|download|share|广告|下载|分享/i.test(label)) return false;
                      const action=(target.innerText||target.getAttribute('aria-label')||'')
                        .trim().replace(/\\s+/g,' ');
                      if(target.tagName==='SPAN'&&
                  (!/^(?:登录|登入|登錄|密码登录|账号登录|log[ -]?in|sign[ -]?in)$/i.test(action)||
                          !target.closest('[class*="user"],[class*="account"],'+
                            '[class*="menu"],[class*="profile"]'))) return false;
                      if(/^(注册|註冊|register|sign[ -]?up|忘记密码|找回密码)$/i.test(action))
                        return false;
                      return true;
                    }""",
                    [x, y],
                )
                if not permitted:
                    raise WebLoginSessionError("WEB_LOGIN_TARGET_UNPROVEN")
                size = page.viewport_size
                if not size:
                    raise WebLoginSessionError("WEB_LOGIN_VIEWPORT_MISSING")
                await page.mouse.click(x * size["width"], y * size["height"])
                await page.wait_for_timeout(1600)

    async def submit(
        self,
        session_id: str,
        origin: str,
        identifier_kind: str,
        identifier: str,
        password: str,
    ) -> str:
        async with self._browser._capture_semaphore:
            lock = self._browser._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = await self._locked(session_id)
                page = session.page
                self._submit_clicks[session_id] = False
                if not _same_origin(str(page.url), origin):
                    return "WEB_LOGIN_ORIGIN_MISMATCH"
                if not await self._tls_verifier(origin):
                    return "WEB_LOGIN_TLS_UNTRUSTED"
                selected = await self._form_target(page, origin, identifier_kind)
                if not selected:
                    return "WEB_LOGIN_FORM_UNPROVEN"
                form_frame, targets = selected
                service_worker = await page.evaluate(
                    """async () => !!navigator.serviceWorker &&
                      (!!navigator.serviceWorker.controller ||
                       (await navigator.serviceWorker.getRegistrations()).length>0)"""
                )
                if service_worker:
                    return "WEB_LOGIN_SERVICE_WORKER_UNSAFE"
                guard_state = self._guard_states.setdefault(
                    session_id, {"blocked": False, "requests": set()}
                )
                guard = self._credential_route_guard(origin, identifier, password, guard_state)
                try:
                    await page.context.route("**/*", guard)
                except Exception:  # noqa: BLE001 - 错误可能含凭据
                    return "WEB_LOGIN_ROUTE_GUARD_UNAVAILABLE"
                _mute_session_observers(session)
                self._private_identifiers[session_id] = identifier
                reason = "WEB_LOGIN_SUBMIT_FAILED"
                response_tasks: list[asyncio.Task] = []
                authorized = (
                    self._auth_policy.for_site(origin)
                    if isinstance(self._auth_policy, WebLoginAuthOrigins)
                    else frozenset()
                )

                def on_auth_response(response) -> None:
                    request = response.request
                    parsed = urlsplit(str(response.url))
                    response_origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
                    if (
                        str(request.method).upper() != "POST"
                        or response_origin not in {origin, *authorized}
                        or not re.search(
                            r"login|log-in|sign-?in|auth|session", parsed.path, re.I
                        )
                    ):
                        return

                    async def classify() -> None:
                        try:
                            payload = await response.json()
                        except Exception:  # noqa: BLE001 - 响应正文不可信且不得落日志
                            return
                        failure = classify_login_failure(
                            str(response.url), response.status, payload
                        )
                        if failure is not None:
                            self._login_failure_codes[session_id] = failure

                    response_tasks.append(asyncio.create_task(classify()))

                page.on("response", on_auth_response)
                try:
                    inputs = form_frame.locator("input")
                    controls = form_frame.locator(
                        'button,input[type="submit"],[role="button"],div.btn,div[class*="login-submit"],[data-role="login"],[data-action="login"]'
                    )
                    await inputs.nth(targets["identifier"]).fill(identifier, timeout=5000)
                    await inputs.nth(targets["password"]).fill(password, timeout=5000)
                    if not _same_origin(str(page.url), origin):
                        reason = "WEB_LOGIN_ORIGIN_MISMATCH"
                    else:
                        fresh = await self._form_target(page, origin, identifier_kind)
                        if fresh != selected:
                            reason = "WEB_LOGIN_FORM_CHANGED"
                        elif not await controls.nth(targets["submit"]).is_enabled():
                            reason = "WEB_LOGIN_SUBMIT_DISABLED"
                        else:
                            await controls.nth(targets["submit"]).click(timeout=5000)
                            self._submit_clicks[session_id] = True
                            reason = "WEB_LOGIN_SUBMITTED"
                            await page.wait_for_timeout(800)
                except Exception:  # noqa: BLE001 - Playwright 错误可能包含秘密
                    reason = "WEB_LOGIN_SUBMIT_FAILED"
                finally:
                    scrub_status = await _scrub_secrets(page, origin, identifier, password)
                    self._last_scrub_status[session_id] = scrub_status
                    scrubbed = scrub_status == "clear"
                    page.remove_listener("response", on_auth_response)
                    if response_tasks:
                        await asyncio.gather(*response_tasks, return_exceptions=True)
                    # 路由保护持续到浏览器会话结束，延迟认证请求也不得跨源。
                    self._last_guard_summary[session_id] = self.blocked_request_summary(session_id)
                    _clear_session_observer_buffers(session)
                    if not scrubbed and session_id not in self._isolated_sessions:
                        await page.close()
                if not scrubbed:
                    return (
                        "WEB_LOGIN_SECRET_RESIDUE"
                        if session_id in self._isolated_sessions
                        else "WEB_LOGIN_SECRET_SCRUB_FAILED"
                    )
                if not _same_origin(str(page.url), origin):
                    return "WEB_LOGIN_ORIGIN_MISMATCH"
                return reason

    def _credential_route_guard(self, origin: str, identifier: str, password: str, state: dict):
        authorized = (
            self._auth_policy.for_site(origin)
            if isinstance(self._auth_policy, WebLoginAuthOrigins)
            else frozenset()
        )

        async def guard(route):
            url = str(route.request.url)
            decoded = unquote(url)
            parsed = urlsplit(url)
            request_origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
            if (
                (identifier and identifier in decoded)
                or (password and password in decoded)
                or request_origin not in {origin, *authorized}
            ):
                method = str(route.request.method).upper()
                if (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and parsed.username is None
                    and parsed.password is None
                ):
                    entry = (request_origin, method)
                    if len(state["requests"]) < 20:
                        state["requests"].add(entry)
                    elif method not in {"GET", "HEAD"} and entry not in state["requests"]:
                        low_priority = next(
                            (
                                item
                                for item in sorted(state["requests"])
                                if item[1] in {"GET", "HEAD"}
                            ),
                            None,
                        )
                        if low_priority is not None:
                            state["requests"].remove(low_priority)
                            state["requests"].add(entry)
                state["blocked"] = True
                await route.abort()
            else:
                await route.fallback()

        return guard


def _valid_box(box) -> bool:
    return (
        isinstance(box, list | tuple)
        and len(box) == 4
        and all(isinstance(value, int | float) for value in box)
        and 0 <= box[0] < box[2] <= 1
        and 0 <= box[1] < box[3] <= 1
    )


async def _center_popup_close_target(page):
    """全页遮罩与弹窗为兄弟节点时，只选当前居中弹窗内的唯一关闭控件。"""
    return await page.evaluate(r"""() => {
      const visible=e=>{
        const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        if(r.width<12||r.height<12||r.left<0||r.top<0||
           r.right>innerWidth||r.bottom>innerHeight||s.display==='none'||
           s.visibility==='hidden'||Number(s.opacity)===0) return null;
        const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
        return hit===e||e.contains(hit) ? r : null;
      };
      const vanPopups=[...document.querySelectorAll('.van-popup')]
        .map(e=>({e,r:visible(e)})).filter(x=>x.r);
      const layers=(vanPopups.length ? vanPopups : [...document.querySelectorAll(
        'dialog,[role="dialog"],[aria-modal="true"],[class*="popup"],[class*="modal"]')]
        .map(e=>({e,r:visible(e)})).filter(x=>x.r&&
          x.r.width>=innerWidth*.15&&x.r.height>=innerHeight*.15&&
          x.r.width<innerWidth*.8&&x.r.height<innerHeight*.9&&
          x.r.left<innerWidth*.5&&x.r.right>innerWidth*.5&&
          x.r.top<innerHeight*.5&&x.r.bottom>innerHeight*.5));
      const leaves=layers.filter(x=>!layers.some(y=>y!==x&&x.e.contains(y.e)));
      if(leaves.length!==1) return null;
      const controls=[...leaves[0].e.querySelectorAll(
        'button,a,img,[role="button"],[class],[id],[aria-label],[title]')]
        .map(e=>{
          const key=[e.className,e.id,e.getAttribute('aria-label'),
            e.getAttribute('title'),e.getAttribute('alt')].join(' ');
          return /close|dismiss|关闭|取消|on-dialog-close/i.test(key)
            ? {e,r:visible(e)} : null;
        }).filter(x=>x&&x.r&&x.r.width*x.r.height<innerWidth*innerHeight*.04);
      const targets=controls.filter(x=>!controls.some(y=>y!==x&&x.e.contains(y.e)));
      if(targets.length!==1) return null;
      const r=targets[0].r;
      return [r.left/innerWidth,r.top/innerHeight,
        r.right/innerWidth,r.bottom/innerHeight];
    }""")


async def _scrub_secrets(page, origin: str, identifier: str, password: str) -> str:
    """页面导航后只检查仍存活的同源文档；脱离的登录 iframe 已无残留 DOM。"""
    for _ in range(2):
        await page.wait_for_timeout(150)
        for frame in tuple(page.frames):
            if frame.is_detached() or not _same_origin(str(frame.url), origin):
                continue
            for attempt in range(3):
                try:
                    residue = await frame.evaluate(
                        """values => {
                          for(const e of document.querySelectorAll('input,textarea')) {
                            if(values.includes(e.value)) e.value='';
                            if(values.includes(e.getAttribute('value')))
                              e.removeAttribute('value');
                          }
                          return {
                            input:[...document.querySelectorAll('input,textarea')]
                              .some(e=>values.includes(e.value)),
                            markup:document.documentElement.outerHTML.includes(values[1])
                          };
                        }""",
                        [identifier, password],
                    )
                    if residue["input"]:
                        return "residual_input"
                    if residue["markup"]:
                        return "residual_markup"
                    break
                except Exception:  # noqa: BLE001 - 页面导航错误可能携带秘密
                    if frame.is_detached():
                        break
                    if attempt == 2:
                        return "frame_evaluation_failed"
                    await page.wait_for_timeout(100)
    return "clear" if _same_origin(str(page.url), origin) else "origin_changed"
