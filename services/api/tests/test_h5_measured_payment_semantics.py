"""031支付转场与促销倒计时：真实附件片段及受控浏览器反例。"""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.domain.exploration.h5_logical_surface import compare_h5_logical_surfaces
from services.api.app.infrastructure.browser import page_capture

CASE = json.loads((Path(__file__).parent / "fixtures/h5_031_payment_20261012_103747.json")
                  .read_text(encoding="utf-8"))
CSS = """
body {margin:0} .header {height:44px;display:flex;align-items:center;
  justify-content:space-between} .title {font-size:16px} svg {width:24px;height:24px}
.main {height:688px;overflow-y:auto} #mine0 {height:744px;overflow-y:auto}
.bottomBox {height:112px} .van-tabbar {height:50px;position:fixed;bottom:0;width:100%}
"""


def _surface(snapshot, url):
    return build_h5_logical_surface(ui_snapshot=snapshot, page_url=url,
                                   surface_session_id="h5:payment-fixture",
                                   document_token="same-document")


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        await page_capture._install_logical_surface_history_observer(context)
        result = await context.new_page()
        try:
            yield result
        finally:
            await browser.close()


async def _load(page, fragment):
    await page.set_content(f'<style>{CSS}</style><div id="app">{fragment}</div>')
    await page.evaluate("history.pushState({}, '', '#mine')")
    return await page_capture._collect_ui_snapshot(page, surface_session_id="h5:payment-fixture")


async def _navigate(page, fragment):
    await page.locator("#app").evaluate("(node, html) => node.innerHTML=html", fragment)
    await page.evaluate("history.pushState({}, '', '#member')")
    return await page_capture._collect_ui_snapshot(page, surface_session_id="h5:payment-fixture")


@pytest.mark.parametrize("case", CASE["cases"], ids=lambda c: c["name"])
async def test_measured_payment_page_has_heading_and_structural_transition(page, case):
    assert case["after_capture_id"] and case["screenshot_sha256"]
    before = await _load(page, CASE["before_fragment"])
    after = await _navigate(page, CASE["after_fragment"])
    left, right = _surface(before, "https://fixture.test/mine"), _surface(
        after, "https://fixture.test/member?id=redacted",
    )
    assert right["identity_signals"]["page_heading"]
    assert right["identity_signals"]["main_structure"]
    transition = compare_h5_logical_surfaces(before=left, after=right)
    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    # 历史Snapshot缺少新采集事实时仍失败关闭，不能补造历史通过。
    legacy_before, legacy_after = deepcopy(before), deepcopy(after)
    for snapshot in (legacy_before, legacy_after):
        snapshot["h5_main_structure"] = {
            "schema_version": "h5-native-main-shell.v1", "complete": True,
            "document_time_origin_ms": snapshot["document"]["time_origin_ms"], "roots": [],
        }
    old = compare_h5_logical_surfaces(
        before=_surface(legacy_before, "https://fixture.test/mine"),
        after=_surface(legacy_after, "https://fixture.test/member?id=redacted"),
    )
    assert old["reason_code"] == CASE["transition_reason"]


@pytest.mark.parametrize("effect", ["route", "text", "price", "title", "carousel"])
async def test_same_panel_dynamic_changes_do_not_become_navigation(page, effect):
    before = await _load(page, CASE["after_fragment"])
    if effect == "route":
        await page.evaluate("history.pushState({}, '', '#different')")
    elif effect == "title":
        await page.locator(".title").evaluate_all(
            "nodes => nodes.forEach(node => node.textContent='限时会员中心')",
        )
    elif effect == "carousel":
        await page.locator(".main").evaluate("node => node.style.background='blue'")
    else:
        await page.locator(".card-box").evaluate(
            "node => node.innerHTML='¥999 倒计时:22:53:57'",
        )
    after = await page_capture._collect_ui_snapshot(page, surface_session_id="h5:payment-fixture")
    transition = compare_h5_logical_surfaces(
        before=_surface(before, "https://fixture.test/member"),
        after=_surface(after, "https://fixture.test/other" if effect == "route"
                       else "https://fixture.test/member"),
    )
    assert transition["transition_kind"] != "logical_page_changed"


@pytest.mark.parametrize("boundary", ["modal", "card", "hidden", "covered", "oversize"])
async def test_generic_div_title_requires_visible_page_chrome(page, boundary):
    fragment = CASE["after_fragment"]
    if boundary == "modal":
        fragment = '<div role="dialog" aria-modal="true">' + fragment + '</div>'
    elif boundary == "card":
        fragment = '<div class="content-card">' + fragment + '</div>'
    elif boundary == "hidden":
        fragment = '<div hidden>' + fragment + '</div>'
    elif boundary == "covered":
        fragment += '<div role="dialog" style="position:fixed;inset:0;z-index:9999">关闭</div>'
    else:
        fragment = fragment.replace('class="header ', 'style="height:600px" class="header ')
    snapshot = await _load(page, fragment)
    assert not _surface(snapshot, "https://fixture.test/member")["identity_signals"]["page_heading"]


@pytest.mark.parametrize("boundary", ["incomplete", "epoch", "missing_geometry", "forged_basis"])
async def test_div_main_proof_is_bounded_and_same_frame(page, boundary):
    snapshot = await _load(page, CASE["after_fragment"])
    proof = snapshot["h5_main_structure"]
    if boundary == "incomplete":
        proof["complete"] = False
    elif boundary == "epoch":
        proof["document_time_origin_ms"] += 1
    elif boundary == "missing_geometry":
        proof["roots"][0].pop("viewport_coverage", None)
    else:
        proof["roots"][0]["root_basis"] = "model_summary"
    assert not _surface(snapshot, "https://fixture.test/member").get("native_main_structure")


@pytest.mark.parametrize("text,expected", [
    (CASE["after_visible_text"], False), ("活动倒计时:00:00:05", False),
    ("秒后关闭促销优惠", False), ("开屏广告 5秒后跳过", True),
    ("跳过广告", True), ("广告 5秒后关闭", False),
])
def test_promotional_timer_is_not_startup_ad_authority(text, expected):
    assert page_capture._looks_like_startup_ad_text(text) is expected


@pytest.mark.parametrize("blocking", ["none", "loader", "media", "transition", "unknown"])
async def test_payment_timer_keeps_normal_readiness_checks(monkeypatch, blocking):
    readiness = {
        "core_present": True, "necessary_media_ready": blocking != "media",
        "visible_loader": blocking == "loader", "transition_active": blocking == "transition",
        "flutter_bootstrap_pending": False, "dom_quiet_ms": 1000,
    } if blocking != "unknown" else {}
    read = AsyncMock(return_value=readiness)
    monkeypatch.setattr(page_capture, "_read_page_readiness", read)
    monkeypatch.setattr(page_capture, "_has_visible_startup_ad_dismissal",
                        AsyncMock(return_value=False))
    fake = type("Page", (), {"inner_text": AsyncMock(return_value=CASE["after_visible_text"])})()
    diagnostics = {}
    settled = await page_capture._wait_for_page_settle(
        page=fake, max_wait_ms=80, poll_interval_ms=0, stable_samples=1,
        diagnostics=diagnostics,
    )
    assert settled is (blocking == "none")
    assert read.await_count >= 1
    assert diagnostics["outcome"] != CASE["settle_outcome"]


async def test_true_startup_countdown_still_uses_obstruction_capture():
    fake = type("Page", (), {"inner_text": AsyncMock(return_value="开屏广告 5秒后跳过")})()
    diagnostics = {}
    assert await page_capture._wait_for_page_settle(
        page=fake, max_wait_ms=80, poll_interval_ms=0, diagnostics=diagnostics,
    )
    assert diagnostics["outcome"] == "startup_obstruction_ready"
    assert fake.inner_text.await_count == 1


@pytest.mark.parametrize("boundary", ["web", "native_heading", "nested_scroll"])
async def test_existing_semantic_and_web_identity_remains_authoritative(page, boundary):
    fragment = CASE["after_fragment"]
    if boundary == "native_heading":
        fragment = '<h1>原生业务标题</h1>' + fragment
    elif boundary == "nested_scroll":
        fragment = '<main class="frame">' + fragment + '</main>'
    snapshot = await _load(page, fragment)
    if boundary == "web":
        snapshot = await page_capture._collect_ui_snapshot(page, surface_session_id="web:fixture")
        assert "h5_main_structure" not in snapshot
    elif boundary == "native_heading":
        headings = _surface(snapshot, "https://fixture.test/member")["identity_signals"]["page_heading"]
        assert len(headings) == 1 and headings[0]["tag"] == "h1"
    else:
        assert len(snapshot["h5_main_structure"]["roots"]) == 1
        assert snapshot["h5_main_structure"]["roots"][0]["tag"] == "main"


async def test_short_business_panel_does_not_require_long_scroll_content(page):
    fragment = CASE["after_fragment"].replace('style="height:887px"', '')
    snapshot = await _load(page, fragment)
    assert snapshot["h5_scroll_regions"] == []
    assert snapshot["h5_main_structure"]["roots"][0]["root_basis"] == "viewport_page_container"
    assert _surface(snapshot, "https://fixture.test/member").get("native_main_structure")


async def test_real_local_player_retains_time_media_and_identity_evidence(page):
    await _load(page, """
      <div class="header"><div class="title">视频详情</div></div>
      <main class="player-surface"><video muted controls loop style="width:320px;height:180px">
      </video><h1>真实播放器</h1></main>
    """)
    # 本地录制移动帧生成真实媒体，不访问站点，也不以 video 标签冒充播放证据。
    await page.evaluate("""async () => {
      const canvas = document.createElement('canvas'); canvas.width=320; canvas.height=180;
      const context = canvas.getContext('2d'); const stream=canvas.captureStream(20);
      const recorder = new MediaRecorder(stream, {mimeType:'video/webm'});
      const chunks=[]; recorder.ondataavailable=event => chunks.push(event.data);
      const finished=new Promise(resolve => recorder.onstop=resolve);
      let frame=0;
      const timer=setInterval(() => {
        context.fillStyle=frame++ % 2 ? 'blue' : 'green'; context.fillRect(0,0,320,180);
      }, 40);
      recorder.start(); await new Promise(resolve => setTimeout(resolve, 400));
      recorder.stop(); await finished; clearInterval(timer);
      stream.getTracks().forEach(track => track.stop());
      const video=document.querySelector('video');
      video.src=URL.createObjectURL(new Blob(chunks, {type:'video/webm'}));
      await video.play();
    }""")
    await page.wait_for_function("document.querySelector('video').currentTime > .05")
    first = await page_capture._collect_ui_snapshot(page, surface_session_id="h5:payment-fixture")
    media = next(item for item in first["elements"] if item["tag"] == "video")
    assert media["media"]["ready_state"] >= 2
    assert media["media"]["current_time"] > 0 and media["media"]["paused"] is False
    assert media["src"].startswith("blob:")
    await page.locator("video").evaluate("video => video.pause()")
    second = await page_capture._collect_ui_snapshot(page, surface_session_id="h5:payment-fixture")
    assert _surface(first, "https://fixture.test/video/7")["identity_fingerprint"] == \
        _surface(second, "https://fixture.test/video/7")["identity_fingerprint"]
