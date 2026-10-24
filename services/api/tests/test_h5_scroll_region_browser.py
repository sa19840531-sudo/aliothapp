"""任意样式内部滚动区必须进入 H5 取证，不改变桌面 Web 快照。"""

import pytest
from playwright.async_api import async_playwright

from services.api.app.agents.tools.page_detection_tools import (
    CandidateAction,
    _with_list_scroll_coverage_candidate,
)
from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.application.observation_packet import _compact_ui_snapshot
from services.api.app.infrastructure.browser.capture_runtime import _merge_action_evidence
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    PlaywrightPageCaptureBrowser,
    _collect_scroll_state,
    _collect_ui_snapshot,
    _compact_scroll_ui_snapshot,
    _run_single_trigger,
)


@pytest.mark.parametrize("scroll_top, expected_scroll", [(0, True), (800, True), (5558, False)])
def test_list_scroll_candidate_uses_internal_position(scroll_top, expected_scroll):
    navigation = CandidateAction(
        action="click", target="栏目入口", risk_level="low", purpose="探索栏目",
    )
    observation = CandidateAction(
        action="observe", target="观察当前页面", risk_level="low", purpose="观察取证",
    )
    actions = _with_list_scroll_coverage_candidate(
        page_type="content_list", page_state="stable",
        candidate_actions=[navigation, observation], dom_chunks=[],
        ui_snapshot={
            "viewport": {"width": 390, "height": 844, "scroll_y": 0},
            "document": {"scroll_height": 844},
            "h5_scroll_regions": [{
                "selector": "#arbitrary-panel", "scroll_top": scroll_top,
                "scroll_height": 6144, "client_height": 586,
            }],
        },
    )
    assert actions[:2] == [navigation, observation]
    assert any(item.action == "scroll" for item in actions) is expected_scroll


@pytest.mark.parametrize("unknown_top", [None, "invalid", float("nan"), float("inf"), -1])
def test_unverified_internal_position_does_not_prove_list_bottom(unknown_top):
    actions = _with_list_scroll_coverage_candidate(
        page_type="content_list", page_state="stable", candidate_actions=[], dom_chunks=[],
        ui_snapshot={
            "viewport": {"width": 390, "height": 844, "scroll_y": 0},
            "h5_scroll_regions": [{
                "scroll_top": unknown_top, "scroll_height": 3000, "client_height": 720,
            }],
        },
    )
    assert len(actions) == 1


@pytest.mark.parametrize("second_top, expected_scroll", [(5558, False), (800, True)])
def test_other_scroll_region_must_also_prove_bottom(second_top, expected_scroll):
    actions = _with_list_scroll_coverage_candidate(
        page_type="home", page_state="stable", candidate_actions=[], dom_chunks=[],
        ui_snapshot={
            "viewport": {"width": 390, "height": 844, "scroll_y": 0},
            "h5_scroll_regions": [
                {"scroll_top": top, "scroll_height": 6144, "client_height": 586}
                for top in (5558, second_top)
            ],
        },
    )
    assert bool(actions) is expected_scroll


async def test_internal_list_bottom_does_not_issue_another_downward_scroll():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body {margin:0;overflow:hidden}</style>
                <div id="arbitrary-panel" style="width:390px;height:586px;overflow-y:auto">
                    <article style="height:6144px">正文</article>
                </div>
            """)
            await page.locator("#arbitrary-panel").evaluate("e => {e.scrollTop = e.scrollHeight}")
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            scroll_state = await _collect_scroll_state(page, surface_session_id="h5:fixture")
            assert snapshot["viewport"]["scroll_y"] == 0
            assert scroll_state["at_bottom"] is True
            assert snapshot["h5_scroll_regions"][0]["scroll_top"] == 5558
            actions = _with_list_scroll_coverage_candidate(
                page_type="home", page_state="stable", candidate_actions=[], dom_chunks=[],
                ui_snapshot=snapshot,
            )
            assert actions == []
        finally:
            await browser.close()


@pytest.mark.parametrize("session", ["h5:fixture", "web:fixture"])
async def test_plain_internal_scroll_region_is_visible_without_css_whitelist(session):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body {margin:0;overflow:hidden}
                  .unfamiliar-panel {height:720px;width:390px;overflow-y:auto}
                  .payload {height:3000px}</style>
                <div class="unfamiliar-panel"><div class="payload">正文</div></div>
            """)
            before = await _collect_ui_snapshot(page, surface_session_id=session)
            if session.startswith("web:"):
                assert "h5_scroll_regions" not in before
                return
            regions = before["h5_scroll_regions"]
            assert _compact_ui_snapshot(before)["h5_scroll_regions"] == regions
            assert _compact_scroll_ui_snapshot(before)["h5_scroll_regions"] == regions
            assert len(regions) == 1
            assert regions[0]["scroll_top"] == 0
            assert regions[0]["scroll_height"] == 3000
            actions = _with_list_scroll_coverage_candidate(
                page_type="home", page_state="stable", candidate_actions=[],
                dom_chunks=[], ui_snapshot=before,
            )
            assert len(actions) == 1
            before_logical = build_h5_logical_surface(
                ui_snapshot=before, page_url=page.url, surface_session_id=session,
                document_token="same-document",
            )
            await page.locator(".unfamiliar-panel").evaluate("e => {e.scrollTop = 800}")
            after = await _collect_ui_snapshot(page, surface_session_id=session)
            after_logical = build_h5_logical_surface(
                ui_snapshot=after, page_url=page.url, surface_session_id=session,
                document_token="same-document",
            )
            assert after["viewport"]["scroll_y"] == 0
            assert after_logical["identity_fingerprint"] == before_logical["identity_fingerprint"]
            assert after_logical["state_fingerprint"] != before_logical["state_fingerprint"]
        finally:
            await browser.close()


@pytest.mark.parametrize("wrapper_top", [None, 0, 6])
async def test_internal_body_scroll_persists_two_distinct_frames_and_partial_coverage(wrapper_top):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            content = """
                <style>body {margin:0;overflow:hidden}</style>
                <div id="body-list" style="height:720px;width:390px;overflow-y:auto">
                    <article style="height:3000px">正文</article>
                </div>
            """
            if wrapper_top is not None:
                content = (
                    '<style>body {margin:0;overflow:hidden}</style>'
                    '<div id="wrapper" style="height:742px;width:390px;overflow-y:auto">'
                    '<div style="height:748px">' + content + '</div></div>'
                )
            await page.set_content(content)
            if wrapper_top is not None:
                await page.locator("#wrapper").evaluate(
                    "(e, top) => {e.scrollTop = top}", wrapper_top,
                )
            state = await _collect_scroll_state(page, surface_session_id="h5:fixture")
            assert state["container_selector"] == "#body-list"
            assert state["at_bottom"] is False
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            artifacts = await PlaywrightPageCaptureBrowser()._collect_scroll_coverage_artifacts(
                page=page, context=page.context,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER, trigger_action="scroll",
                    session_id="h5:fixture", scroll_max_segments=2, split_scroll_screenshots=True,
                ),
                action_trace=[], timeout_ms=1000, timeout_error_type=TimeoutError,
                baseline_ui_snapshot=snapshot,
            )
            screenshots = [item for item in artifacts if item.artifact_type == "screenshot"]
            assert len(screenshots) == 2
            sequence = next(
                item.content for item in artifacts if item.artifact_type == "ui_snapshot"
            )
            frames = _compact_ui_snapshot(sequence)["frames"]
            assert frames[0]["h5_scroll_regions"][0]["scroll_top"] == 0
            assert frames[1]["h5_scroll_regions"][0]["scroll_top"] > 0
            coverage = next(
                item.content for item in artifacts if item.artifact_type == "page_coverage"
            )
            assert coverage["termination_reason"] == "segment_budget_exhausted"
            assert coverage["coverage_complete"] is False
            merged = _merge_action_evidence(
                capture_result=PageCaptureResult(
                    execution_status="success", mode=PageCaptureMode.OBSERVE.value,
                    reachable=True, final_url="https://fixture.test/", title="主体列表",
                    status_code=200,
                ),
                action_result=PageCaptureResult(
                    execution_status="success", mode=PageCaptureMode.SINGLE_TRIGGER.value,
                    reachable=True, final_url="https://fixture.test/", title="主体列表",
                    status_code=200, artifacts=artifacts,
                ),
                required_types={"screenshot", "ui_snapshot", "action_trace"},
            )
            # 规则读取动作后采集，滚动过程的真实多帧与未到底缺口不能在合并时丢失。
            assert all(item in merged.artifacts for item in artifacts)
            if wrapper_top is not None:
                assert await page.locator("#wrapper").evaluate("e => e.scrollTop") == wrapper_top
        finally:
            await browser.close()


@pytest.mark.parametrize("hidden", ["display:none", "visibility:hidden", "height:40px"])
async def test_hidden_or_small_scroll_region_does_not_issue_body_scroll(hidden):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(f"""
                <style>body {{margin:0;overflow:hidden}}</style>
                <div style="height:720px;width:390px;overflow-y:auto;{hidden}">
                  <div style="height:3000px">正文</div>
                </div>
            """)
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            assert snapshot["h5_scroll_regions"] == []
        finally:
            await browser.close()


@pytest.mark.parametrize("session, body_style, expected", [
    ("web:fixture", "", "#wrapper"),
    ("h5:fixture", "width:100px", "#wrapper"),
    ("h5:fixture", "height:40px", "#wrapper"),
    ("h5:fixture", "visibility:hidden", "#wrapper"),
])
async def test_nested_scroll_priority_preserves_web_and_rejects_non_body_regions(
    session, body_style, expected,
):
    """小控件或不可见内层不能替代主体，桌面 Web 的原排序不变。"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(f"""
                <style>body {{margin:0;overflow:hidden}}</style>
                <div id="wrapper" style="height:742px;width:390px;overflow-y:auto">
                  <div style="height:748px">
                    <div id="body-list"
                      style="height:720px;width:390px;overflow-y:auto;{body_style}">
                      <article style="height:3000px">正文</article>
                    </div>
                  </div>
                </div>
            """)
            state = await _collect_scroll_state(page, surface_session_id=session)
            assert state["container_selector"] == expected
            await _run_single_trigger(
                page=page, context=page.context,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER, trigger_action="scroll",
                    session_id=session,
                ),
                action_trace=[], timeout_ms=1000,
            )
            assert await page.locator(expected).evaluate("e => e.scrollTop") == 6
            assert await page.locator("#body-list").evaluate("e => e.scrollTop") == 0
        finally:
            await browser.close()


@pytest.mark.parametrize("session", ["h5:fixture", "web:fixture"])
async def test_h5_scroll_executes_on_visible_body_not_larger_hidden_panel(session):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body {margin:0;overflow:hidden}</style>
                <div id="hidden" style="position:fixed;visibility:hidden;width:390px;
                    height:840px;overflow-y:auto"><div style="height:4000px">隐藏</div></div>
                <div id="body" style="width:390px;height:720px;overflow-y:auto">
                    <div style="height:3000px">正文</div>
                </div>
            """)
            await _run_single_trigger(
                page=page, context=page.context,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER, trigger_action="scroll",
                    session_id=session,
                ),
                action_trace=[], timeout_ms=1000,
            )
            expected = "#body" if session.startswith("h5:") else "#hidden"
            other = "#hidden" if session.startswith("h5:") else "#body"
            assert await page.locator(expected).evaluate("e => e.scrollTop") > 0
            assert await page.locator(other).evaluate("e => e.scrollTop") == 0
            state = await _collect_scroll_state(page, surface_session_id=session)
            assert state["container_selector"] == expected
            assert state["scroll_top"] > 0
        finally:
            await browser.close()
