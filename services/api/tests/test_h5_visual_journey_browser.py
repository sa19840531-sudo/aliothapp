"""真实 Chromium 双帧证明插页可导致视觉拒绝；不冒充旧偶发失败。"""

import asyncio
import json
from contextlib import nullcontext

import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.application.h5_session_journey import H5SessionJourneyGate
from services.api.tests.test_page_capture_tool import (
    _VISUAL_NAVIGATION_LIVE_HTML,
    BlockedPageCaptureActionError,
    _run_single_trigger,
    _visual_navigation_click_request,
    _visual_navigation_runtime_target_from_png,
)


@pytest.mark.parametrize("whole_journey", [False, True])
async def test_real_visual_source_drift_requires_journey_not_click_retry(whole_journey, tmp_path):
    playwright_api = pytest.importorskip("playwright.async_api")
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    source_ready, main_attempted, mutated = (asyncio.Event() for _ in range(3))
    facts = {"whole_journey": whole_journey}
    async with playwright_api.async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 240, "height": 240})
        await page.set_content(_VISUAL_NAVIGATION_LIVE_HTML)

        async def main():
            await source_ready.wait()
            main_attempted.set()
            async with gate.hold(task_id="task", session_id="h5:run"):
                await page.evaluate(
                    "document.querySelector('#visual-target').style.left = '100px'",
                )
                mutated.set()

        task = asyncio.create_task(main())
        try:
            scope = (gate.hold_journey(task_id="task", session_id="h5:run")
                     if whole_journey else nullcontext())
            async with scope:
                async with gate.hold(task_id="task", session_id="h5:run"):
                    source = await page.screenshot(type="png")
                    target = _visual_navigation_runtime_target_from_png(
                        source_png=source, source_rect=(82, 92, 142, 152),
                    )
                    (tmp_path / "source.png").write_bytes(source)
                    (tmp_path / "source-dom.html").write_text(
                        await page.content(), encoding="utf-8",
                    )
                source_ready.set()
                await main_attempted.wait()
                if whole_journey:
                    await asyncio.sleep(0.02)
                    assert not mutated.is_set()
                else:
                    await mutated.wait()
                async with gate.hold(task_id="task", session_id="h5:run"):
                    (tmp_path / "current.png").write_bytes(await page.screenshot(type="png"))
                    (tmp_path / "current-dom.html").write_text(
                        await page.content(), encoding="utf-8",
                    )
                    try:
                        await _run_single_trigger(
                            page=page, context=page.context,
                            request=_visual_navigation_click_request(target),
                            action_trace=[], timeout_ms=30_000,
                        )
                    except BlockedPageCaptureActionError as exc:
                        facts["error_code"] = exc.error_code
                        assert not whole_journey
                        assert exc.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
                    else:
                        assert whole_journey
                    facts["clicks"] = await page.evaluate("window.visualClickCount")
                    assert facts["clicks"] == int(whole_journey)
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            (tmp_path / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
            await browser.close()
