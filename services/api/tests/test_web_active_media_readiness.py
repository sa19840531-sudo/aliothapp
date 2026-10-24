"""真实 Chromium 验证持续播放控件不会永久阻断 Web 页就绪。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.web_readiness_contracts import WebReadinessProfile
from services.api.app.infrastructure.browser.page_capture import (
    _install_page_state_monitor,
    _wait_for_web_page_settle,
)


def _chrome_executable() -> str:
    configured = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    candidate = Path(configured) if configured else Path(
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    if not candidate.is_file():
        pytest.skip("本机未安装可用的 Chromium／Chrome")
    return str(candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation_scope,include_media,generic_monitor_preinstalled,replace_clock_nodes,expected",
    [
        ("none", True, False, False, True),
        ("none", True, True, False, True),
        ("none", True, True, True, True),
        ("main", True, False, False, False),
        ("dialog", True, False, False, False),
        ("nested_article", True, False, False, False),
        ("nested_dialog", True, False, False, False),
        ("nested_button", True, False, False, False),
        ("none", False, False, False, False),
    ],
)
async def test_web_autoplay_controls_and_mini_progress_do_not_block_structural_readiness(
    mutation_scope: str,
    include_media: bool,
    generic_monitor_preinstalled: bool,
    replace_clock_nodes: bool,
    expected: bool,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, executable_path=_chrome_executable()
        )
        try:
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(
                """
                <main>
                  <h1 id="business-title">首页内容</h1><a href="/category">菜单</a>
                  <div class="xgplayer"><video width="1" height="1"></video>
                    <xg-icon class="xgplayer-time"><span class="time-current">
                      <span class="time-min-width">0</span>
                    </span></xg-icon>
                    <xg-progress><xg-played style="width:0%"></xg-played></xg-progress>
                    <xg-mini-progress style="display:block;width:100px;height:2px">
                      <xg-mini-progress-played
                        style="display:block;width:0%;height:2px"></xg-mini-progress-played>
                    </xg-mini-progress>
                  </div>
                  <div class="lottie-root"><svg width="20" height="20">
                    <g transform="translate(0,0)"><circle cx="5" cy="5" r="5"/></g>
                  </svg></div>
                </main>
                """
            )
            if not include_media:
                await page.locator(".xgplayer video").evaluate("element => element.remove()")
            if mutation_scope == "dialog":
                await page.evaluate(
                    """() => {
                      const dialog = document.createElement('div');
                      dialog.setAttribute('role', 'dialog');
                      dialog.id = 'blocking-dialog';
                      dialog.textContent = '等待确认';
                      document.body.append(dialog);
                    }"""
                )
            if mutation_scope in {"nested_article", "nested_dialog", "nested_button"}:
                await page.evaluate(
                    """kind => {
                      const progress = document.querySelector('xg-mini-progress-played');
                      if (kind === 'nested_article') {
                        progress.innerHTML = '<article><p id="nested-business">正文</p></article>';
                      } else {
                        progress.innerHTML = kind === 'nested_dialog'
                          ? '<div role="dialog" id="nested-dialog">等待确认</div>'
                          : '<button id="nested-button">继续</button>';
                      }
                    }""",
                    mutation_scope,
                )
            if generic_monitor_preinstalled:
                await _install_page_state_monitor(page)
            await page.evaluate(
                """mutationScope => {
                  let tick = 0;
                  const timer = document.querySelector('.time-min-width').firstChild;
                  const title = document.querySelector('#business-title').firstChild;
                  setInterval(() => {
                    tick += 1;
                    if (mutationScope === 'replace') {
                      document.querySelector('.time-current').innerHTML =
                        `<span class="time-min-width">${tick}</span>`;
                    } else {
                      timer.nodeValue = String(tick);
                    }
                    document.querySelector('xg-played').style.width = `${tick % 100}%`;
                    document.querySelector('xg-mini-progress-played').style.width =
                      `${tick % 100}%`;
                    document.querySelector('.lottie-root g').setAttribute(
                      'transform', `translate(${tick % 100},0)`
                    );
                    if (mutationScope === 'main') title.nodeValue = `首页内容 ${tick}`;
                    if (mutationScope === 'dialog') {
                      document.querySelector('#blocking-dialog').textContent =
                        `等待确认 ${tick}`;
                    }
                    if (mutationScope === 'nested_article') {
                      document.querySelector('#nested-business').textContent = `正文 ${tick}`;
                    }
                    if (mutationScope === 'nested_dialog') {
                      document.querySelector('#nested-dialog').textContent = `等待确认 ${tick}`;
                    }
                    if (mutationScope === 'nested_button') {
                      document.querySelector('#nested-button').textContent = `继续 ${tick}`;
                    }
                  }, 20);
                }""",
                mutation_scope if mutation_scope != "none" else (
                    "replace" if replace_clock_nodes else "clock"
                ),
            )
            diagnostics: dict[str, object] = {}
            settled = await _wait_for_web_page_settle(
                page=page,
                network_inflight={},
                max_wait_ms=1400,
                poll_interval_ms=50,
                profile=WebReadinessProfile(
                    minimum_dom_quiet_ms=100,
                    minimum_window_ms=150,
                ),
                diagnostics=diagnostics,
            )
            assert settled is expected
            assert diagnostics["quorum_met"] is expected
        finally:
            await browser.close()
