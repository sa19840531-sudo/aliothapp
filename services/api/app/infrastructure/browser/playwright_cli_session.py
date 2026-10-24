"""在同一个 Chromium 页面中联合使用 Python 采集和受限 CLI。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from services.api.app.infrastructure.browser.playwright_cli_process import (
    CLI_PATCH_ID,
    CLI_VERSION,
    PlaywrightCliError,
    PlaywrightCliProcess,
)

_CSS_PATH_SCRIPT = """element => {
  const escape = value => {
    if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
      return CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, value => '\\\\' + value);
  };
  const segments = [];
  let node = element;
  while (node && node.nodeType === 1) {
    let segment = node.localName;
    if (node.id) {
      segment += '#' + escape(node.id);
      segments.unshift(segment);
      node = null;
      break;
    }
    let index = 1;
    for (
      let sibling = node.previousElementSibling;
      sibling;
      sibling = sibling.previousElementSibling
    ) {
      if (sibling.localName === node.localName)
        index += 1;
    }
    segment += ':nth-of-type(' + index + ')';
    segments.unshift(segment);
    node = node.parentElement;
  }
  if (node !== null || element.getRootNode() !== element.ownerDocument)
    return '';
  return segments.join(' > ');
}"""

_ELEMENT_SEMANTIC_SUMMARY_SCRIPT = """element => ({
  aria_label: element.getAttribute('aria-label'),
  href: element.href || null,
  role: element.getAttribute('role'),
  text: element.innerText || '',
  type: element.getAttribute('type'),
})"""

_BINDING_BRIDGE_INSTALL_SCRIPT = """args => {
  const bridge = (() => {
    let current = null;
    const verify = token => {
      if (token !== args.token)
        throw new Error('CLI binding capability rejected');
    };
    return Object.freeze({
      probe(token) {
        verify(token);
        return true;
      },
      set(token, element) {
        verify(token);
        if (!element || element.nodeType !== 1)
          throw new Error('CLI binding target is not an element');
        current = element;
      },
      get(token) {
        verify(token);
        return current;
      },
      // 清空是幂等的失效操作；页面即使主动调用也只能造成拒绝服务，
      // 不能把另一个节点交给 CLI。
      clear() {
        current = null;
      },
    });
  })();
  Object.defineProperty(window, args.key, {
    configurable: false,
    enumerable: false,
    writable: false,
    value: bridge,
  });
}"""

_BINDING_BRIDGE_EXISTS_SCRIPT = """key =>
  Object.prototype.hasOwnProperty.call(window, key)"""

_BINDING_BRIDGE_HANDLE_SCRIPT = """key => window[key]"""

_BINDING_BRIDGE_PROBE_HANDLE_SCRIPT = """(bridge, token) =>
  bridge.probe(token) === true"""

_BINDING_BRIDGE_SET_HANDLE_SCRIPT = """(bridge, args) => {
  bridge.set(args.token, args.element);
}"""

_BINDING_BRIDGE_CLEAR_HANDLE_SCRIPT = """bridge => {
  bridge.clear();
}"""

_BINDING_BRIDGE_IDENTITY_SCRIPT = """args =>
  window[args.key] === args.bridge"""


class CliBrowserSession:
    """连接只由平台创建；目录、页面句柄和执行模式都不来自 Agent 参数。"""

    def __init__(self, *, session_id: str, root: Path, script: Path, mode: str) -> None:
        if mode not in {"observe", "actions"}:
            raise ValueError("CLI 模式必须为 observe 或 actions")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        prefix = "session-" + hashlib.sha256(session_id.encode()).hexdigest()[:16] + "-"
        self._directory = tempfile.TemporaryDirectory(prefix=prefix, dir=root)
        self.directory = Path(self._directory.name)
        self.mode = mode
        try:
            self.process = PlaywrightCliProcess(
                script=script,
                workspace=self.directory,
                session_id=session_id,
                actions_enabled=mode == "actions",
            )
        except BaseException:
            self._directory.cleanup()
            raise
        self._origin: str | None = None
        # 每个 CLI 会话只保留一个当前文档桥接；随机名只是避免与业务页面冲突，
        # 真正的不可替换边界是不可配置属性和不出现在页面对象中的 capability。
        self._binding_bridge_key = f"__alioth_cli_binding_bridge_{uuid4().hex}"
        self._binding_bridge_token = uuid4().hex
        self._binding_bridge_page: Any | None = None
        self._binding_bridge_handle: Any | None = None
        self._binding_bridge_installed = False

    async def start(self, playwright: Any, *, context_options: dict, proxy: dict | None) -> Any:
        context = None
        try:
            launch_options = {
                "headless": True,
                "args": ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"],
            }
            if proxy:
                launch_options["proxy"] = proxy
            context = await playwright.chromium.launch_persistent_context(
                str(self.directory / "profile"),
                **launch_options,
                ignore_https_errors=True,
                **context_options,
            )
            port_file = self.directory / "profile" / "DevToolsActivePort"
            port = int(port_file.read_text().splitlines()[0])
            if not 0 < port < 65536:
                raise PlaywrightCliError("CLI_ENDPOINT_REJECTED")
            page = context.pages[0]
            environment_before = await _environment_snapshot(page)
            self.process.configure_environment(
                viewport=page.viewport_size,
                context_options=context_options,
            )
            await self.process.attach(f"http://127.0.0.1:{port}")
            if environment_before != await _environment_snapshot(page):
                self.process.quarantine()
                raise PlaywrightCliError("CLI_ATTACH_ENVIRONMENT_CHANGED")
            return context
        except BaseException:
            try:
                await self.process.detach()
            except Exception:
                pass
            if context is not None:
                await context.close()
            self._directory.cleanup()
            raise

    async def close(self) -> None:
        await self._dispose_binding_bridge_handle()
        await self.process.detach()

    def cleanup(self) -> None:
        """浏览器与 CLI 已退出后清理本实例创建的目录。"""
        self._directory.cleanup()

    def set_initial_url(self, url: str | None) -> None:
        if self._origin is None and url:
            self._origin = _origin(url)

    async def assert_page(self, page: Any) -> dict:
        listing = await self.process.run_readonly("tab-list")
        lines = str(listing.output.get("result", "")).splitlines()
        records = []
        for line in lines:
            match = re.fullmatch(r"- (\d+): (\(current\) )?\[.*\]\((.*)\)", line)
            if match:
                records.append((int(match[1]), bool(match[2]), match[3]))
        current = [item for item in records if item[1]]
        pages = page.context.pages
        if (
            len(current) != 1
            or page not in pages
            or len(records) != len(pages)
            or len({r[0] for r in records}) != len(records)
            or current[0][0] != pages.index(page)
            or current[0][2] != page.url
        ):
            raise PlaywrightCliError("CLI_PAGE_BINDING_MISMATCH")
        return listing.output

    async def observe(self, page: Any) -> dict:
        """仅采集当前页；结果保留独立时序，不冒充原采集的同一帧。"""
        environment_before = await _environment_snapshot(page)
        try:
            observation = await self._observe(page)
        finally:
            try:
                environment_after = await _environment_snapshot(page)
            except BaseException:
                self.process.quarantine()
                raise
            if environment_before != environment_after:
                self.process.quarantine()
                raise PlaywrightCliError("CLI_OBSERVATION_ENVIRONMENT_CHANGED")
        observation.update(
            {
                "environment_before": environment_before,
                "environment_after": environment_after,
                "environment_preserved": True,
                "compatibility_patch": CLI_PATCH_ID,
            }
        )
        return observation

    async def _observe(self, page: Any) -> dict:
        before = str(page.url)
        tabs = await self.assert_page(page)
        snapshot = await self.process.run_readonly("snapshot")
        requests = await self.process.run_readonly("requests")
        output = self.directory / f"capture-{uuid4().hex}.png"
        # 仅开放完整视口设备像素截图；连接时已继承并验证原设备环境。
        receipt = await self.process.run_readonly(
            "screenshot", "--filename=" + str(output), "--hires"
        )
        if output.is_symlink() or not output.is_file() or output.stat().st_size > 20 * 1024 * 1024:
            raise PlaywrightCliError("CLI_SCREENSHOT_INVALID")
        png = output.read_bytes()
        output.unlink()
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PlaywrightCliError("CLI_SCREENSHOT_INVALID")
        await self.assert_page(page)
        if before != page.url:
            raise PlaywrightCliError("CLI_OBSERVATION_PAGE_CHANGED")
        return {
            "schema_version": "playwright-cli-observation.v1",
            "version": CLI_VERSION,
            "mode": self.mode,
            "url": before,
            "title": await page.title(),
            "tabs": tabs,
            "snapshot": snapshot.output,
            "requests": requests.output,
            "screenshot": {"encoding": "base64", "data": base64.b64encode(png).decode()},
            "screenshot_sha256": hashlib.sha256(png).hexdigest(),
            "receipt": receipt.audit(),
            "comparison_only": True,
        }

    async def navigate(self, page: Any, url: str) -> tuple[dict, Any | None]:
        await self.assert_page(page)
        self._require_business_url(url)
        responses = []

        def collect(response: Any) -> None:
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                responses.append(response)

        # CLI 负责唯一导航，Python 只观察实际主文档响应，保留既有 HTTP 状态语义。
        page.on("response", collect)
        try:
            receipt = await self.process.run("goto", url)
            return receipt.audit(), responses[-1] if responses else None
        finally:
            page.remove_listener("response", collect)

    async def trigger(self, *, page: Any, request: Any) -> dict | None:
        """在发出动作前选定执行器；特殊旧能力仍由原路径处理，无失败后重放。"""
        if self.mode != "actions":
            return None
        self.process.require_action_session()
        action = str(request.trigger_action)
        if action not in {"click", "back", "scroll"}:
            return None
        # 广告落地页必须在动作发送前交给既有专项流程，保留源页、落地页和回位证据。
        # 这里不能先让 CLI 失败后再回退，否则动作可能已经发出且结果不可逆。
        if action == "click" and (
            bool(getattr(request, "capture_ad_landing_evidence", False))
            or bool(getattr(request, "_force_legacy_ad_landing", False))
        ):
            return None
        # 特殊视觉、弹窗、卡片几何激活需要保留原专用校验，不在此降级为普通点击。
        if action == "click" and (
            not request.selector
            or request._visual_navigation_target is not None
            or request.click_x_ratio is not None
            or request.clear_obstructions
            or request.allow_high_z_obstruction_hide
            or request.verified_card_activation
            or request.expected_rect
            or request.selector.startswith("__")
        ):
            return None
        await self.assert_page(page)
        self._require_business_url(page.url)
        if action == "click":
            locator = page.locator(request.selector)
            if await locator.count() != 1:
                raise PlaywrightCliError("CLI_TARGET_NOT_UNIQUE")
            # ElementHandle 在 Python 侧先固定当前节点；CLI 收到 backendNodeId 后只解析该节点。
            handle = await locator.element_handle(timeout=request.timeout_ms)
            if handle is None:
                raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
            try:
                semantic_summary = await handle.evaluate(_ELEMENT_SEMANTIC_SUMMARY_SCRIPT)
                semantic_digest = semantic_snapshot_digest(semantic_summary)
                label = str(semantic_summary.get("text") or "")
                href = str(semantic_summary.get("href") or "")
                input_type = semantic_summary.get("type")
                if input_type in {"submit", "file", "password"} or _dangerous_label(label):
                    raise PlaywrightCliError("CLI_TARGET_RISK_REJECTED")
                if href:
                    self._require_business_url(href)
                # trial 执行可见、稳定和命中检查，不发送点击；之后由 CLI 唯一发送动作。
                backend_node_id, binding_selector = await self._backend_node_id(
                    page=page,
                    handle=handle,
                )
                await handle.click(trial=True, timeout=request.timeout_ms)
                receipt = await self.process.click_backend_node(
                    backend_node_id,
                    binding_selector,
                    semantic_digest,
                )
                receipt_audit = receipt.audit()
                receipt_audit["target_binding"] = "cdp_backend_node_id"
                return {"action": action, "selector": request.selector, **receipt_audit}
            finally:
                dispose = getattr(handle, "dispose", None)
                if callable(dispose):
                    try:
                        await dispose()
                    except Exception:  # noqa: BLE001 - 页面关闭时句柄可自然失效
                        pass
        elif action == "back":
            if not request.navigation_depth or request.navigation_depth <= 0:
                raise PlaywrightCliError("CLI_HISTORY_SOURCE_REQUIRED")
            receipt = await self.process.run("go-back")
        else:
            if request.selector:
                point = await _selector_guided_scroll_point(
                    page=page,
                    selector=request.selector,
                    timeout_ms=request.timeout_ms,
                )
                target_binding = "selector"
            else:
                # 固定只读观察器定位当前可见滚动容器，JS 文本不可由 Agent 替换。
                point = await page.evaluate("""() => {
                  const visible = e => {const r=e.getBoundingClientRect();
                    return r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight;};
                  const candidates=[...document.querySelectorAll('*')].filter(e =>
                    visible(e) && /(auto|scroll)/.test(getComputedStyle(e).overflowY)
                    && e.scrollHeight>e.clientHeight+1);
                  const e=candidates.sort((a,b)=>b.clientWidth*b.clientHeight
                    -a.clientWidth*a.clientHeight)[0];
                  const r=e?e.getBoundingClientRect():
                    {left:0,top:0,width:innerWidth,height:innerHeight};
                  return {x:Math.round(Math.max(0,Math.min(innerWidth-1,r.left+r.width/2))),
                    y:Math.round(Math.max(0,Math.min(innerHeight-1,r.top+r.height/2))),
                    distance:Math.round(Math.max(1,Math.min(r.height,innerHeight))*.85)};
                }""")
                target_binding = "visible_scroll_container"
            await self.process.run("mousemove", str(point["x"]), str(point["y"]))
            receipt = await self.process.run("mousewheel", "0", str(point["distance"]))
            return {
                "action": action,
                "selector": request.selector,
                "target_binding": target_binding,
                "requires_new_capture": True,
                **receipt.audit(),
            }
        return {"action": action, "selector": request.selector, **receipt.audit()}

    async def _backend_node_id(self, *, page: Any, handle: Any) -> tuple[int, str]:
        """通过原 ElementHandle 的对象引用绑定 backendNodeId。

        CSS 路径只能作为 CLI 侧最后一次同节点复核的辅助信息，不能再用来
        认领 backendNodeId；否则在路径查询前插入同语义节点时会把动作发给
        错误节点。页面桥接属性只在当前文档安装一次，属性本身不可重定义，
        节点引用只保存在 capability 闭包中，并在 ``finally`` 中清空，不向
        DOM 写入临时属性。
        """
        selector = await handle.evaluate(_CSS_PATH_SCRIPT)
        if not isinstance(selector, str) or not selector:
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        new_cdp_session = getattr(page.context, "new_cdp_session", None)
        if not callable(new_cdp_session):
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        cdp = await new_cdp_session(page)
        object_id: str | None = None
        bridge_object_id: str | None = None
        bridge_bound = False
        bridge_handle: Any | None = None
        try:
            await self._ensure_binding_bridge(page)
            bridge_handle = self._binding_bridge_handle
            if bridge_handle is None:
                raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
            # Playwright Python 不公开 ElementHandle 的 Chromium objectId；把
            # 同一个 JS 对象临时交给 Runtime，再由 CDP 直接 describeNode，避免
            # 通过可变化的 CSS 路径重新查找原节点。set 直接调用缓存的原桥接，
            # 不从当前 window 名称重新认领对象。
            await bridge_handle.evaluate(
                _BINDING_BRIDGE_SET_HANDLE_SCRIPT,
                {
                    "token": self._binding_bridge_token,
                    "element": handle,
                },
            )
            bridge_bound = True
            bridge_evaluated = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": f"window[{json.dumps(self._binding_bridge_key)}]",
                    "objectGroup": "alioth-cli-binding",
                    "returnByValue": False,
                },
            )
            bridge_remote = (
                bridge_evaluated.get("result")
                if isinstance(bridge_evaluated, dict)
                else None
            )
            bridge_object_id = (
                bridge_remote.get("objectId")
                if isinstance(bridge_remote, dict)
                else None
            )
            if (
                not isinstance(bridge_object_id, str)
                or not bridge_object_id
                or not isinstance(bridge_remote, dict)
                or bridge_remote.get("type") != "object"
            ):
                raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
            same_bridge = await page.evaluate(
                _BINDING_BRIDGE_IDENTITY_SCRIPT,
                {"key": self._binding_bridge_key, "bridge": bridge_handle},
            )
            if same_bridge is not True:
                raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
            evaluated = await cdp.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": bridge_object_id,
                    "functionDeclaration": "function(token) { return this.get(token); }",
                    "arguments": [{"value": self._binding_bridge_token}],
                    "objectGroup": "alioth-cli-binding",
                    "returnByValue": False,
                },
            )
            remote = evaluated.get("result") if isinstance(evaluated, dict) else None
            object_id = remote.get("objectId") if isinstance(remote, dict) else None
            if (
                not isinstance(object_id, str)
                or not object_id
                or not isinstance(remote, dict)
                or remote.get("subtype") != "node"
            ):
                raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
            described = await cdp.send("DOM.describeNode", {"objectId": object_id})
            node = described.get("node") if isinstance(described, dict) else None
            backend_node_id = node.get("backendNodeId") if isinstance(node, dict) else None
            if (
                type(backend_node_id) is not int
                or backend_node_id <= 0
                or backend_node_id > 2**31 - 1
            ):
                raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
            return backend_node_id, selector
        except PlaywrightCliError:
            raise
        except Exception as exc:  # noqa: BLE001 - CDP 只作为节点绑定，不泄露协议错误
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE") from exc
        finally:
            if bridge_bound:
                try:
                    # 先清理闭包中的页面引用，再释放 CDP 远端对象，避免短暂绑定
                    # 影响后续页面快照或把页面对象长期保留在浏览器进程中。
                    await bridge_handle.evaluate(_BINDING_BRIDGE_CLEAR_HANDLE_SCRIPT)
                except Exception:  # noqa: BLE001 - 页面关闭时引用可自然失效
                    pass
            if object_id is not None:
                try:
                    await cdp.send("Runtime.releaseObject", {"objectId": object_id})
                except Exception:  # noqa: BLE001 - 远端对象可自然失效
                    pass
            if bridge_object_id is not None:
                try:
                    await cdp.send(
                        "Runtime.releaseObject", {"objectId": bridge_object_id}
                    )
                except Exception:  # noqa: BLE001 - 远端对象可自然失效
                    pass
            detach = getattr(cdp, "detach", None)
            if callable(detach):
                try:
                    await detach()
                except Exception:  # noqa: BLE001 - 页面关闭时 CDP 分离可自然失败
                    pass

    async def _ensure_binding_bridge(self, page: Any) -> None:
        """在当前 document 安装并验证不可替换的节点绑定桥接。"""
        if self._binding_bridge_page is not page:
            await self._dispose_binding_bridge_handle()
            self._binding_bridge_page = page
            self._binding_bridge_installed = False
            self._rotate_binding_bridge_credentials()
        try:
            if self._binding_bridge_installed:
                bridge_handle = self._binding_bridge_handle
                if bridge_handle is None:
                    raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
                try:
                    valid = await bridge_handle.evaluate(
                        _BINDING_BRIDGE_PROBE_HANDLE_SCRIPT,
                        self._binding_bridge_token,
                    )
                except Exception:
                    valid = False
                if valid is True:
                    return
                # 导航会销毁缓存的执行上下文；同名属性若已被页面抢占则
                # 直接拒绝，不能把 capability 传给页面自造的 probe。
                if await page.evaluate(
                    _BINDING_BRIDGE_EXISTS_SCRIPT, self._binding_bridge_key
                ):
                    raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
                await self._dispose_binding_bridge_handle()
                self._binding_bridge_installed = False
                self._rotate_binding_bridge_credentials()
            elif await page.evaluate(
                _BINDING_BRIDGE_EXISTS_SCRIPT, self._binding_bridge_key
            ):
                raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
            args = {
                "key": self._binding_bridge_key,
                "token": self._binding_bridge_token,
            }
            await page.evaluate(_BINDING_BRIDGE_INSTALL_SCRIPT, args)
            bridge_handle = await page.evaluate_handle(
                _BINDING_BRIDGE_HANDLE_SCRIPT, self._binding_bridge_key
            )
            self._binding_bridge_handle = bridge_handle
            self._binding_bridge_installed = True
        except PlaywrightCliError:
            raise
        except Exception as exc:  # noqa: BLE001 - 页面环境变化必须失败关闭
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE") from exc

    async def _dispose_binding_bridge_handle(self) -> None:
        bridge_handle = self._binding_bridge_handle
        self._binding_bridge_handle = None
        if bridge_handle is not None:
            try:
                await bridge_handle.dispose()
            except Exception:  # noqa: BLE001 - 页面导航或关闭时句柄可自然失效
                pass

    def _rotate_binding_bridge_credentials(self) -> None:
        self._binding_bridge_key = f"__alioth_cli_binding_bridge_{uuid4().hex}"
        self._binding_bridge_token = uuid4().hex

    def _require_business_url(self, url: str) -> None:
        if self._origin is None or _origin(url) != self._origin:
            raise PlaywrightCliError("CLI_ORIGIN_REJECTED")


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise PlaywrightCliError("CLI_URL_REJECTED")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


async def _selector_guided_scroll_point(
    *,
    page: Any,
    selector: str,
    timeout_ms: int,
) -> dict[str, int]:
    """在当前 Capture 绑定唯一目标，只计算一次有界滚轮步长。"""

    locator = page.locator(selector)
    if await locator.count() != 1:
        raise PlaywrightCliError("CLI_TARGET_NOT_UNIQUE")
    handle = await locator.element_handle(timeout=timeout_ms)
    if handle is None:
        raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
    try:
        box = await handle.bounding_box()
        if not isinstance(box, dict):
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        viewport = getattr(page, "viewport_size", None)
        if not isinstance(viewport, dict):
            viewport = await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        width = float(viewport.get("width") or 0)
        height = float(viewport.get("height") or 0)
        left = float(box.get("x") or 0)
        top = float(box.get("y") or 0)
        target_width = float(box.get("width") or 0)
        target_height = float(box.get("height") or 0)
        if width <= 1 or height <= 1 or target_width <= 0 or target_height <= 0:
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        right = left + target_width
        bottom = top + target_height
        if right <= 0 or left >= width:
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        if top >= 0 and bottom <= height:
            raise PlaywrightCliError("CLI_TARGET_ALREADY_VISIBLE")
        if top >= height:
            distance = max(1, round(top - height * 0.25))
        elif bottom <= 0:
            distance = min(-1, round(bottom - height * 0.75))
        elif top < 0:
            distance = min(-1, round(top - height * 0.1))
        else:
            distance = max(1, round(bottom - height * 0.9))
        return {
            "x": round(max(0, min(width - 1, left + target_width / 2))),
            "y": round(height / 2),
            "distance": max(-10000, min(10000, distance)),
        }
    finally:
        dispose = getattr(handle, "dispose", None)
        if callable(dispose):
            try:
                await dispose()
            except Exception:  # noqa: BLE001 - 页面关闭时句柄可自然失效
                pass


async def _environment_snapshot(page: Any) -> dict:
    """固定只读脚本检查模拟环境；不读取账号数据，也不接受模型脚本文本。"""
    return await page.evaluate("""() => ({
      width: innerWidth, height: innerHeight, dpr: devicePixelRatio,
      screenWidth: screen.width, screenHeight: screen.height,
      touchPoints: navigator.maxTouchPoints, userAgent: navigator.userAgent,
      orientation: screen.orientation?.type ?? null,
      viewportScale: visualViewport?.scale ?? null
    })""")


def _dangerous_label(label: str) -> bool:
    return bool(
        re.search(
            r"支付|付款|购买|下单|充值|转账|提现|授权|删除|注销|安装|更新|"
            r"\b(?:pay|purchase|checkout|transfer|delete|install|authorize)\b",
            label,
            re.I,
        )
    )


def semantic_snapshot_digest(summary: Any) -> str:
    """为点击前后的固定语义字段生成摘要；不读取输入值或页面正文。"""
    if not isinstance(summary, dict):
        raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
    normalized: dict[str, str | None] = {}
    for key in ("aria_label", "href", "role", "text", "type"):
        value = summary.get(key)
        if value is not None and not isinstance(value, str):
            raise PlaywrightCliError("CLI_TARGET_BINDING_UNAVAILABLE")
        normalized[key] = value
    return hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def observation_digest(observation: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            observation,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
