"""提供可重复的 H5 页面行为验收夹具。"""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

MANIFEST_SCHEMA_VERSION = "h5-acceptance-fixture-manifest.v1"
FIXTURE_SCENARIOS = {
    "W46-H5-001": "/h5/home",
    "W46-H5-002": "/h5/line-loading",
    "W46-H5-003": "/h5/startup-ad",
    "W46-H5-004": "/h5/single-popup",
    "W46-H5-005": "/h5/popup-chain",
    "W46-H5-007": "/h5/auth-gate",
    "W46-H5-008": "/h5/tabbar",
    "W46-H5-009": "/h5/detail-back",
    "W46-H5-010": "/h5/safe-action",
    "W46-H5-011": "/h5/line-startup-popup",
    "W46-H5-012": "/h5/long-page",
    "W46-H5-013": "/h5/js-navigation",
    "W46-H5-014": "/h5/viewports",
    "W46-H5-015": "/h5/external-navigation",
    "W46-H5-016": "/h5/cancel",
    "W46-H5-017": "/h5/recapture",
    "W46-H5-101": "/h5/capture-retry",
    "W46-H5-102": "/h5/unstable",
}
FIXTURE_DRIVERS = {
    "W46-H5-016": "task_cancellation_driver",
    "W46-H5-017": "targeted_recapture_driver",
}
CONTROLLED_DRIVER_SCENARIOS = {
    "W46-H5-006": "browser_permission_driver",
    "W46-H5-103": "browser_crash_driver",
    "W46-H5-104": "evidence_storage_failure_driver",
    "W46-H5-105": "llm_contract_failure_driver",
}

_STYLE = """
:root { color-scheme: light; font-family: "Noto Sans SC", sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; background: #f5f1e8; color: #16352c; }
main { min-height: 100vh; padding: 24px 20px 92px; }
h1 { margin: 0 0 8px; font-size: 30px; }
p { line-height: 1.6; }
.eyebrow { color: #9a5c18; font-size: 13px; font-weight: 700; letter-spacing: .08em; }
.card { margin: 18px 0; padding: 18px; border: 1px solid #d9d2c3; border-radius: 16px;
  background: #fffdf8; box-shadow: 0 12px 30px rgba(22,53,44,.08); }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
button, .button { min-height: 44px; padding: 10px 16px; border: 0; border-radius: 12px;
  background: #0d6b54; color: white; font: inherit; font-weight: 700; }
.secondary { background: #e9e2d5; color: #16352c; }
.danger { background: #a33a2b; }
.tabbar { position: fixed; z-index: 10; right: 0; bottom: 0; left: 0; display: grid;
  grid-template-columns: repeat(4, 1fr); gap: 1px; padding: 8px; background: #fffdf8;
  border-top: 1px solid #d9d2c3; }
.tabbar button { background: transparent; color: #36554c; }
.tabbar button[aria-current="page"] { background: #d8eadf; color: #0d6b54; }
.overlay { position: fixed; z-index: 20; inset: 0; display: grid; place-items: center;
  padding: 24px; background: rgba(4, 16, 13, .74); }
.dialog { width: min(100%, 340px); padding: 22px; border-radius: 18px; background: #fffdf8;
  box-shadow: 0 24px 80px rgba(0,0,0,.35); }
.dialog h2 { margin-top: 0; }
.close { width: 100%; }
.loading { display: grid; min-height: 70vh; place-items: center; text-align: center; }
.spinner { width: 54px; height: 54px; margin: auto; border: 6px solid #d8eadf;
  border-top-color: #0d6b54; border-radius: 50%; animation: spin .8s linear infinite; }
.long-section { min-height: 72vh; margin: 16px 0; padding: 24px; border-radius: 18px;
  background: linear-gradient(145deg, #fffdf8, #e8f1e8); }
.badge { display: inline-block; padding: 4px 9px; border-radius: 999px; background: #d8eadf;
  color: #0d6b54; font-size: 12px; font-weight: 700; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 420px) { .grid { grid-template-columns: 1fr; } }
"""


def fixture_manifest(*, base_url: str) -> dict[str, Any]:
    """返回执行器可校验的夹具能力清单。"""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "base_url": base_url,
        "fixtures": {
            scenario_id: {
                "path": path,
                "url": f"{base_url}{path}",
                "driver": FIXTURE_DRIVERS.get(scenario_id, "page_behavior"),
                "deterministic": True,
            }
            for scenario_id, path in FIXTURE_SCENARIOS.items()
        },
        "requires_controlled_driver": CONTROLLED_DRIVER_SCENARIOS,
    }


def render_fixture(path: str, *, external_origin: str) -> str | None:
    """按路径渲染单文件 H5 页面，不读取宿主机文件。"""
    scenario_id = next(
        (key for key, value in FIXTURE_SCENARIOS.items() if value == path),
        None,
    )
    if scenario_id is None:
        if path == "/external-landing":
            return _document(
                title="外部落地页",
                scenario_id="external-landing",
                body="""
                <main>
                  <p class="eyebrow">外部域名夹具</p>
                  <h1>第三方活动页</h1>
                  <p>此页不属于被质检产品，不应增加产品页面覆盖。</p>
                  <button type="button" onclick="history.back()">返回原产品</button>
                </main>
                """,
            )
        return None

    body, script = _fixture_content(scenario_id, external_origin=external_origin)
    return _document(
        title=f"{scenario_id} H5 验收夹具",
        scenario_id=scenario_id,
        body=body,
        script=script,
    )


def _fixture_content(scenario_id: str, *, external_origin: str) -> tuple[str, str]:
    if scenario_id == "W46-H5-002":
        return (
            _loading_body("正在选择最佳线路", "线路检测完成后自动进入首页"),
            "setTimeout(() => { document.body.innerHTML = window.fixtureHome(); }, 1200);",
        )
    if scenario_id == "W46-H5-003":
        return _home_with_startup_ad(popup_count=0)
    if scenario_id == "W46-H5-004":
        return _home_with_popups(1)
    if scenario_id == "W46-H5-005":
        return _home_with_popups(3)
    if scenario_id == "W46-H5-011":
        body = _loading_body("正在加载业务线路", "完成后进入开屏广告")
        script = """
        setTimeout(() => {
          document.body.innerHTML = window.fixtureHome();
          window.fixtureShowStartupAd(2);
        }, 1000);
        """
        return body, script
    if scenario_id == "W46-H5-007":
        return (
            """
            <main>
              <p class="eyebrow">访问验证</p><h1>登录后继续</h1>
              <section class="card"><label>手机号<input aria-label="手机号"></label>
              <label>密码<input type="password" aria-label="密码"></label>
              <button type="button">登录</button></section>
              <p>当前产品未提供游客入口，质检不得伪造登录状态。</p>
            </main>
            """,
            "",
        )
    if scenario_id == "W46-H5-008":
        return _tabbar_fixture()
    if scenario_id == "W46-H5-009":
        return _detail_fixture()
    if scenario_id == "W46-H5-010":
        return _safe_action_fixture()
    if scenario_id == "W46-H5-012":
        sections = "".join(
            f'<section class="long-section" data-segment="{index}">'
            f'<span class="badge">第 {index} 段</span><h2>内容专题 {index}</h2>'
            f'<p>用于验证滚动位置与分段原始截图，段落编号 {index}。</p></section>'
            for index in range(1, 7)
        )
        return (
            f'<main><p class="eyebrow">长页面</p>'
            f'<h1>专题内容列表</h1>{sections}</main>',
            "",
        )
    if scenario_id == "W46-H5-013":
        return (
            """
            <main id="app" data-fixture-view="landing"><p class="eyebrow">SPA 首页</p>
              <h1>精选内容</h1><button id="spa-entry" type="button">查看全部内容</button>
            </main>
            """,
            """
            document.querySelector('#spa-entry').addEventListener('click', () => {
              history.pushState({page: 'content_list'}, '', '#/content');
              document.querySelector('#app').dataset.fixtureView = 'articles';
              document.querySelector('#app').innerHTML = '<p class="eyebrow">内容频道</p><h1>全部内容</h1><div class="card">内容一</div><div class="card">内容二</div>';
            });
            """,
        )
    if scenario_id == "W46-H5-014":
        return (
            """
            <main><p class="eyebrow">响应式首页</p><h1>视口一致性</h1>
              <div class="grid"><article class="card">推荐内容</article><article class="card">最新内容</article></div>
              <p id="viewport"></p></main>
            """,
            """
            const viewport = document.querySelector('#viewport');
            const update = () => { viewport.textContent = `viewport=${innerWidth}x${innerHeight}`; };
            addEventListener('resize', update); update();
            """,
        )
    if scenario_id == "W46-H5-015":
        return (
            f"""
            <main><p class="eyebrow">产品首页</p><h1>外部跳转隔离</h1>
              <section class="card"><p>合作方活动入口会离开当前产品域名。</p>
              <a class="button" data-external="true" href="{external_origin}/external-landing">打开合作方活动</a></section>
            </main>
            """,
            "",
        )
    if scenario_id == "W46-H5-016":
        return (
            """
            <main><p class="eyebrow">任务取消验收</p><h1>持续探索目录</h1>
              <p>页面提供足够多的安全入口，让验收器在首批事实落库后发起取消。</p>
              <section id="cancel-grid" class="grid"></section>
              <p id="cancel-status">等待探索动作</p></main>
            """,
            """
            const grid = document.querySelector('#cancel-grid');
            for (let index = 1; index <= 24; index += 1) {
              const button = document.createElement('button');
              button.type = 'button'; button.className = 'secondary';
              button.textContent = `频道 ${index}`; button.dataset.channel = String(index);
              button.addEventListener('click', () => {
                history.pushState({channel: index}, '', `#channel-${index}`);
                document.querySelector('#cancel-status').textContent = `已进入频道 ${index}`;
              });
              grid.appendChild(button);
            }
            """,
        )
    if scenario_id == "W46-H5-017":
        return (
            """
            <main class="content-list"><p class="eyebrow">内容列表</p>
              <h1>补采验收内容列表</h1>
              <article class="card"><h2>受控详情样本</h2>
              <p>打开详情后会出现需要补采处理的遮挡。</p>
              <button type="button" class="detail-entry" id="show-recapture-overlay">打开内容详情</button></article>
            </main>
            """,
            """
            const runKey = new URLSearchParams(location.search).get('fixture_run') || '';
            const stateUrl = `/_drivers/targeted-recapture/state?fixture_run=${encodeURIComponent(runKey)}`;
            document.querySelector('#show-recapture-overlay')?.addEventListener('click', () => {
              if (document.querySelector('#recapture-overlay')) return;
              history.pushState({}, '', `/h5/recapture/detail?fixture_run=${encodeURIComponent(runKey)}`);
              const main = document.querySelector('main');
              if (main) {
                main.className = 'content-detail';
                main.innerHTML = `<p class="eyebrow">内容详情</p><h1>补采后的底层页面</h1>
                  <section class="card"><p>温馨提示：操作已完成。</p>
                  <p>关闭受控遮挡后，当前详情页才能成为可信证据。</p></section>`;
              }
              const overlay = document.createElement('div');
              overlay.className = 'overlay'; overlay.id = 'recapture-overlay';
              overlay.setAttribute('role', 'dialog'); overlay.setAttribute('aria-modal', 'true');
              overlay.innerHTML = `<section class="dialog"><span class="badge">受控遮挡</span>
                <h2>证据缺口受控遮挡</h2><p>正式补采请求出现前不提供关闭控件。</p>
                <div id="recapture-control"></div></section>`;
              document.body.appendChild(overlay);
            });
            const timer = setInterval(async () => {
              const response = await fetch(stateUrl, {cache: 'no-store'});
              const state = await response.json();
              if (!state.released) return;
              const slot = document.querySelector('#recapture-control');
              if (!slot || slot.querySelector('button')) return;
              clearInterval(timer);
              const button = document.createElement('button');
              button.type = 'button'; button.className = 'close';
              button.setAttribute('aria-label', '关闭受控遮挡');
              button.textContent = '关闭';
              button.addEventListener('click', () => document.querySelector('#recapture-overlay')?.remove());
              slot.appendChild(button);
            }, 500);
            """,
        )
    if scenario_id == "W46-H5-101":
        return _home_body(), ""
    if scenario_id == "W46-H5-102":
        return (
            _loading_body("页面持续更新中", "此页面故意不进入稳定态"),
            """
            let revision = 0;
            setInterval(() => {
              revision += 1;
              document.querySelector('#loading-status').textContent = `正在同步第 ${revision} 批数据`;
              document.body.dataset.revision = String(revision);
            }, 200);
            """,
        )
    return _home_body(), ""


def _document(*, title: str, scenario_id: str, body: str, script: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="alioth-fixture-scenario" content="{scenario_id}">
<title>{title}</title><style>{_STYLE}</style></head>
<body data-fixture-scenario="{scenario_id}">{body}
<script>
window.fixtureHome = () => `{_home_body()}`;
window.fixtureShowPopups = (count) => {{
  let layer = 1;
  const show = () => {{
    if (layer > count) return;
    const overlay = document.createElement('div');
    overlay.className = 'overlay'; overlay.dataset.popupLayer = String(layer);
    overlay.innerHTML = `<section class="dialog" role="dialog" aria-modal="true"><span class="badge">公告 ${{layer}} / ${{count}}</span><h2>业务提醒</h2><p>这是第 ${{layer}} 层弹窗。</p><button class="close" type="button" aria-label="关闭第 ${{layer}} 层弹窗">关闭</button></section>`;
    overlay.querySelector('.close').addEventListener('click', () => {{ overlay.remove(); layer += 1; show(); }});
    document.body.appendChild(overlay);
  }}; show();
}};
window.fixtureShowStartupAd = (popupCount) => {{
  let remaining = 2;
  const overlay = document.createElement('div'); overlay.className = 'overlay'; overlay.dataset.startupAd = 'true';
  overlay.innerHTML = '<section class="dialog" role="dialog" aria-modal="true"><span class="badge">开屏广告</span><h2>今日精选</h2><p>倒计时结束后可安全关闭。</p><button class="close" type="button" disabled aria-label="跳过开屏广告">2 秒后跳过</button></section>';
  const button = overlay.querySelector('.close'); document.body.appendChild(overlay);
  const timer = setInterval(() => {{ remaining -= 1; button.textContent = remaining > 0 ? `${{remaining}} 秒后跳过` : '跳过广告'; button.disabled = remaining > 0; if (remaining <= 0) clearInterval(timer); }}, 500);
  button.addEventListener('click', () => {{ if (button.disabled) return; overlay.remove(); if (popupCount) window.fixtureShowPopups(popupCount); }});
}};
{script}
</script></body></html>"""


def _home_body() -> str:
    return """
    <main><p class="eyebrow">H5 产品首页</p><h1>今日推荐</h1>
      <section class="card"><span class="badge">热门</span><h2>内容精选</h2><p>稳定首页业务内容。</p></section>
      <section class="grid"><article class="card">频道一</article><article class="card">频道二</article></section>
    </main>
    """


def _loading_body(title: str, message: str) -> str:
    return f"""
    <main class="loading"><section>
      <div class="spinner" aria-label="加载中"></div><h1>{title}</h1>
      <p id="loading-status">{message}</p></section></main>
    """


def _home_with_startup_ad(*, popup_count: int) -> tuple[str, str]:
    return _home_body(), f"window.fixtureShowStartupAd({popup_count});"


def _home_with_popups(count: int) -> tuple[str, str]:
    return _home_body(), f"window.fixtureShowPopups({count});"


def _tabbar_fixture() -> tuple[str, str]:
    body = """
    <main id="tab-content" data-fixture-view="landing"><p class="eyebrow">首页</p><h1>推荐首页</h1><div class="card">首页内容</div></main>
    <nav class="tabbar" aria-label="底部导航">
      <button type="button" data-view="landing" aria-current="page">首页</button>
      <button type="button" data-view="articles">内容</button>
      <button type="button" data-view="videos">视频</button>
      <button type="button" data-view="account">我的</button>
    </nav>
    """
    script = """
    const names = {landing: '推荐首页', articles: '内容列表', videos: '视频列表', account: '个人中心'};
    document.querySelectorAll('.tabbar button').forEach((button) => button.addEventListener('click', () => {
      const view = button.dataset.view;
      document.querySelectorAll('.tabbar button').forEach((item) => item.removeAttribute('aria-current'));
      button.setAttribute('aria-current', 'page');
      const content = document.querySelector('#tab-content'); content.dataset.fixtureView = view;
      content.innerHTML = `<p class="eyebrow">底部导航</p><h1>${names[view]}</h1><div class="card">${names[view]}稳定业务内容</div>`;
    }));
    """
    return body, script


def _detail_fixture() -> tuple[str, str]:
    body = """
    <main id="detail-app" class="content-list" data-fixture-view="list"><p class="eyebrow">内容频道</p><h1>内容列表</h1>
      <button class="card secondary detail-entry" type="button" data-item="1">文章一：探索质量</button>
      <button class="card secondary detail-entry" type="button" data-item="2">文章二：证据留痕</button>
    </main>
    """
    script = """
    const app = document.querySelector('#detail-app');
    const renderList = () => { app.className = 'content-list'; app.dataset.fixtureView = 'list'; app.innerHTML = '<p class="eyebrow">内容频道</p><h1>内容列表</h1><button class="card secondary detail-entry" type="button" data-item="1">文章一：探索质量</button><button class="card secondary detail-entry" type="button" data-item="2">文章二：证据留痕</button>'; bindEntries(); };
    const renderDetail = (item) => { history.pushState({item}, '', `#detail-${item}`); app.className = 'content-detail'; app.dataset.fixtureView = 'detail'; app.innerHTML = `<p class="eyebrow">文章详情</p><h1>文章 ${item}</h1><p class="card">详情正文用于验证下钻、取证和返回。</p><button id="back" type="button">返回列表</button>`; document.querySelector('#back').addEventListener('click', () => history.back()); };
    const bindEntries = () => document.querySelectorAll('.detail-entry').forEach((entry) => entry.addEventListener('click', () => renderDetail(entry.dataset.item)));
    addEventListener('popstate', renderList); bindEntries();
    """
    return body, script


def _safe_action_fixture() -> tuple[str, str]:
    return (
        """
        <main class="profile-page"><p class="eyebrow">安全动作识别</p><h1>个人中心</h1>
          <section class="grid">
            <button class="danger" type="button" data-risk-action="payment">充值购买</button>
            <button class="danger" type="button" data-risk-action="post_publish">发布内容</button>
            <button class="danger" type="button" data-risk-action="upload">上传文件</button>
            <button class="secondary" type="button">查看帮助</button>
          </section><p id="risk-result">高风险按钮只用于识别，不提供真实业务提交能力。</p>
        </main>
        """,
        """
        document.querySelectorAll('[data-risk-action]').forEach((button) => button.addEventListener('click', () => {
          document.querySelector('#risk-result').textContent = `夹具检测到不应执行的高风险动作：${button.dataset.riskAction}`;
          document.body.dataset.unsafeActionExecuted = button.dataset.riskAction;
        }));
        """,
    )


class H5AcceptanceFixtureServer(ThreadingHTTPServer):
    """为每个验收批次仅断开一次首个采集请求。"""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
    ) -> None:
        super().__init__(server_address, handler)
        self._capture_retry_lock = threading.Lock()
        self._capture_retry_requests: dict[str, int] = {}
        self._targeted_recapture_releases: set[str] = set()

    def should_drop_capture_retry(self, attempt_key: str) -> bool:
        with self._capture_retry_lock:
            count = self._capture_retry_requests.get(attempt_key, 0)
            self._capture_retry_requests[attempt_key] = count + 1
            return count == 0

    def targeted_recapture_state(self, fixture_run: str) -> dict[str, object]:
        with self._capture_retry_lock:
            return {
                "driver": "targeted_recapture_driver",
                "fixture_run": fixture_run,
                "released": fixture_run in self._targeted_recapture_releases,
            }

    def release_targeted_recapture(self, fixture_run: str) -> dict[str, object]:
        with self._capture_retry_lock:
            self._targeted_recapture_releases.add(fixture_run)
        return self.targeted_recapture_state(fixture_run)


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "AliothH5Fixture/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        base_url = f"http://{self.headers.get('Host', '127.0.0.1')}"
        if parsed.path == "/healthz":
            self._send_json({"status": "ok", "schema_version": MANIFEST_SCHEMA_VERSION})
            return
        if parsed.path == "/_manifest":
            self._send_json(fixture_manifest(base_url=base_url))
            return
        if parsed.path == "/_drivers/targeted-recapture/state":
            server = self._fixture_server()
            self._send_json(server.targeted_recapture_state(_fixture_run(parsed.query)))
            return
        if parsed.path == FIXTURE_SCENARIOS["W46-H5-101"]:
            server = self.server
            if not isinstance(server, H5AcceptanceFixtureServer):
                raise RuntimeError("W46-H5-101 必须由 H5AcceptanceFixtureServer 承载")
            attempt_key = parsed.query or "default"
            if server.should_drop_capture_retry(attempt_key):
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
        external_host = "localhost" if base_url.startswith("http://127.0.0.1") else "127.0.0.1"
        port = urlsplit(base_url).port
        external_origin = f"http://{external_host}{f':{port}' if port else ''}"
        html = render_fixture(parsed.path, external_origin=external_origin)
        if html is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path != "/_drivers/targeted-recapture/release":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        fixture_run = _fixture_run(parsed.query)
        self._send_json(self._fixture_server().release_targeted_recapture(fixture_run))

    def _fixture_server(self) -> H5AcceptanceFixtureServer:
        if not isinstance(self.server, H5AcceptanceFixtureServer):
            raise RuntimeError("H5 受控驱动必须由 H5AcceptanceFixtureServer 承载")
        return self.server

    def log_message(self, format: str, *args: object) -> None:
        if self.path.startswith("/_drivers/targeted-recapture/state?"):
            return
        print(f"{self.address_string()} - {format % args}")

    def _send_json(self, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _fixture_run(query: str) -> str:
    value = (parse_qs(query).get("fixture_run") or [""])[0].strip()
    if not value:
        raise ValueError("targeted recapture driver 缺少 fixture_run")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9080)
    args = parser.parse_args()
    server = H5AcceptanceFixtureServer((args.host, args.port), FixtureHandler)
    print(f"H5 acceptance fixture server: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
