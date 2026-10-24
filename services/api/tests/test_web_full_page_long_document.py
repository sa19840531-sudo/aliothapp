"""长图必须等待连续稳定、限制浏览器副作用并保留主证据。"""

import asyncio
import base64
import importlib
import importlib.util
import json
import os
import struct
from dataclasses import replace
from pathlib import Path

import pytest


def module():
    name = "services.api.app.application.web_full_page_stabilization"
    assert importlib.util.find_spec(name), "缺少有界长页稳定实现"
    return importlib.import_module(name)


class Document:
    """浏览器 I/O 替身；真实稳定循环、上限判断和恢复逻辑由生产代码执行。"""

    def __init__(self, heights=(1200, 2400, 3600, 3600, 3600)):
        self.heights = iter(heights)
        self.height = 1200
        self.y = 75
        self.samples = 0
        self.hidden = False
        self.screenshot_error = None

    async def evaluate(self, script, arg):
        if arg["operation"] == "scroll":
            self.y = arg["y"]
            return None
        if arg["operation"] == "hide":
            self.hidden = True
            return []
        if arg["operation"] == "restore":
            self.hidden = False
            return None
        self.height = next(self.heights, self.height)
        self.samples += 1
        return {
            "document_height": self.height,
            "viewport_height": 4000,
            "scroll_x": 0,
            "scroll_y": self.y,
            "state_hash": f"state:{self.height}",
            "semantic_hash": "semantic",
            "pending_images": 0,
            "sticky_layers": [],
            "scan_limit_exceeded": False,
        }

    async def screenshot(self, **kwargs):
        assert self.hidden
        if self.screenshot_error:
            raise self.screenshot_error
        return b"png"


@pytest.mark.asyncio
async def test_lazy_loaded_document_requires_consecutive_stable_rounds():
    api = module()
    page = Document()
    result = await api.stabilize_for_full_page(
        page, api.WebFullPageStabilizationPolicy(stable_rounds=2)
    )
    assert result.document_height == 3600
    assert result.scroll_steps == 4
    assert result.stable is True
    assert result.limit_exceeded is False
    assert page.y == 75
    assert len(result.state_hash) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["height", "steps"])
async def test_unbounded_document_stops_and_restores_scroll(bound):
    api = module()
    page = Document(range(1200, 60000, 1200))
    policy = api.WebFullPageStabilizationPolicy(
        max_document_height=3000 if bound == "height" else 32768,
        max_scroll_steps=2 if bound == "steps" else 40,
    )
    result = await api.stabilize_for_full_page(page, policy)
    assert result.limit_exceeded is True
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
    assert result.scroll_steps <= policy.max_scroll_steps
    assert page.y == 75


@pytest.mark.asyncio
async def test_oversized_stable_document_is_captured_to_frozen_height_with_truncation_audit():
    api = module()
    page = Document((70000,) * 10)
    policy = api.WebFullPageStabilizationPolicy(max_document_height=8000)

    stabilized = await api.stabilize_for_full_page(page, policy)

    assert stabilized.stable is True
    assert stabilized.limit_exceeded is False
    assert stabilized.document_height == 70000
    assert stabilized.captured_height == 8000
    assert stabilized.truncated is True

    async def screenshot(**kwargs):
        assert kwargs == {"full_page": False, "type": "png"}
        return b"\x89PNGbounded"

    page.screenshot = screenshot
    payloads, audit = await api.capture_stabilized_screenshot_segments(page, stabilized)
    manifest = audit["segment_manifest"]
    assert payloads
    assert manifest["document_height"] == 70000
    assert manifest["captured_height"] == 8000
    assert manifest["truncated"] is True
    assert manifest["segments"][-1]["bottom_css_px"] == 8000


@pytest.mark.asyncio
async def test_stalled_browser_wait_is_bounded_and_cancel_restores_scroll():
    api = module()
    page = Document()
    policy = api.WebFullPageStabilizationPolicy(lazy_load_timeout_ms=50)
    result = await api.stabilize_for_full_page(page, policy)
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert result.wait_elapsed_ms < 500
    assert page.y == 75


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("截图故障"), asyncio.CancelledError()])
async def test_screenshot_failure_and_cancellation_restore_hidden_layers(error):
    api = module()
    page = Document((1200,) * 10)
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    page.screenshot_error = error
    with pytest.raises(type(error)):
        await api.capture_stabilized_screenshot(page, result)
    assert page.hidden is False
    assert page.y == 75


@pytest.mark.asyncio
async def test_browser_limit_error_consumes_claim_without_replacing_primary():
    api = module()
    from services.api.tests.test_web_full_page_supplemental_capture import (
        _Browser,
        _Claims,
        _command,
        _Persistence,
        _service,
    )

    browser, claims, persistence = _Browser(), _Claims(), _Persistence()
    stabilized = api.WebFullPageStabilizationResult(
        document_height=70000,
        captured_height=3000,
        truncated=True,
        limit_exceeded=True,
        reason_code="WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED",
    )
    browser.error = api.WebFullPageStabilizationError(stabilized)
    result = await _service(browser=browser, claims=claims, persistence=persistence).capture(
        _command()
    )
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
    assert result.source_capture_id == "source-capture"
    assert persistence.calls == []
    assert claims.finished[0]["stabilization_audit"]["document_height"] == 70000


def test_stabilization_policy_is_frozen_and_strict():
    api = module()
    from datetime import UTC, datetime

    from services.api.app.application.web_run_policy import (
        load_published_web_policy,
        materialize_web_policy,
    )
    from services.api.tests.test_web_run_policy_snapshot import snapshot

    body = load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=600)
    frozen = body["full_page_supplemental_policy"]["stabilization"]
    assert frozen == {
        "max_document_height": 3000,
        "scroll_offset_px": 1920,
        "max_scroll_steps": 80,
        "stable_rounds": 2,
        "lazy_load_timeout_ms": 8000,
    }
    frozen["max_document_height"] = 16000
    policy = materialize_web_policy(snapshot(body))
    assert policy.full_page_supplemental_policy.stabilization.max_document_height == 16000
    assert policy.full_page_supplemental_policy.stabilization.scroll_offset_px == 1920
    for value in (0, -1, True, 1.5, 1000000):
        with pytest.raises(ValueError):
            replace(api.WebFullPageStabilizationPolicy(), max_scroll_steps=value)


@pytest.fixture
async def chrome_page():
    from playwright.async_api import async_playwright

    executable = os.getenv("ALIOTH_WEB_TEST_BROWSER") or next(
        (
            path
            for path in (
                "C:/Program Files/Google/Chrome/Application/chrome.exe",
                "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            )
            if Path(path).is_file()
        ),
        None,
    )
    async with async_playwright() as playwright:
        if executable is None and not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("缺少真实 Playwright Chromium/Chrome")
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            yield page
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_real_sticky_decorations_hide_but_interactive_and_primary_layers_remain(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content("""<style>
      .layer {position:fixed; width:50px;height:50px;top:0;pointer-events:none}
      main{height:4000px}
    </style><div id="private-secret" aria-hidden="true" class="layer"></div>
    <header class="layer"><a href="/next">导航</a></header>
    <main><div role="presentation" class="layer"></div><p>主体</p></main>""")
    await page.evaluate("scrollTo(0, 150)")
    stabilized = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    screenshots = []
    original = page.screenshot

    async def screenshot(**kwargs):
        screenshots.append(
            await page.locator("#private-secret").evaluate(
                "node => getComputedStyle(node).visibility"
            )
        )
        return await original(**kwargs)

    page.screenshot = screenshot
    png, audit = await api.capture_stabilized_screenshot(page, stabilized)
    assert png.startswith(b"\x89PNG")
    assert screenshots == ["hidden"]
    assert [layer["action"] for layer in audit["sticky_layers"]] == [
        "hidden",
        "retained",
        "retained",
    ]
    assert all(
        layer["semantic_hash_before"] == layer["semantic_hash_after"]
        for layer in audit["sticky_layers"]
    )
    assert "private-secret" not in json.dumps(audit)
    assert await page.locator("#private-secret").get_attribute("style") is None
    assert await page.evaluate("scrollY") == 150


@pytest.mark.asyncio
async def test_real_delayed_lazy_content_and_infinite_document_are_bounded(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content("""<main style="height:1500px">正文</main><script>
      let loads = 0;
      addEventListener('scroll', () => {
        if (scrollY + innerHeight >= document.documentElement.scrollHeight && loads++ < 2)
          setTimeout(() => document.querySelector('main').style.height =
            (parseInt(document.querySelector('main').style.height) + 1000) + 'px', 50);
      });
    </script>""")
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    assert result.document_height >= 3500
    assert result.stable
    await page.set_content("""<main style="height:1500px">正文</main><script>
      addEventListener('scroll', () => {
        if (scrollY + innerHeight >= document.documentElement.scrollHeight)
          document.querySelector('main').style.height =
            (parseInt(document.querySelector('main').style.height) + 1000) + 'px';
      });
    </script>""")
    result = await api.stabilize_for_full_page(
        page, api.WebFullPageStabilizationPolicy(max_document_height=3000)
    )
    assert result.stable is True
    assert result.truncated is True
    assert result.captured_height == 3000
    assert await page.evaluate("scrollY") == 0


@pytest.mark.asyncio
async def test_failed_sticky_screenshot_exposes_restoration_audit(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content(
        '<div aria-hidden="true" style="position:fixed;'
        'width:50px;height:50px;top:0;pointer-events:none"></div><main>正文</main>'
    )
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())

    async def screenshot(**_kwargs):
        raise TimeoutError("截图超时")

    page.screenshot = screenshot
    with pytest.raises(api.WebFullPageStabilizationError) as caught:
        await api.capture_stabilized_screenshot(page, result)
    assert caught.value.reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert caught.value.result.audit["sticky_layers"][0]["action"] == "hidden"
    assert caught.value.result.audit["sticky_layers"][0]["semantic_hash_after"]
    assert await page.locator("div").evaluate("el => getComputedStyle(el).visibility") == "visible"


@pytest.mark.asyncio
async def test_phase_finish_persists_stabilization_audit_in_same_fenced_write():
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )
    from services.api.tests.test_web_full_page_supplemental_capture import _ClaimRecordingPool

    class Connection:
        async def fetchrow(self, sql, *args):
            assert "stabilization_audit" in sql
            assert json.loads(args[5]) == {"document_height": 40000}
            return {"phase_run_id": "phase"}

    await PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(Connection())
    ).finish(
        phase_id="phase",
        status="failed",
        reason_code="LIMIT",
        artifact_ids=(),
        stabilization_audit={"document_height": 40000},
    )


@pytest.mark.asyncio
async def test_ready_static_document_saves_one_direct_full_page_png(chrome_page, tmp_path):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    import services.api.app.infrastructure.browser.page_capture as capture

    page = chrome_page
    await page.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html", body='<main style="height:3500px"><h1>正文</h1></main>'
        ),
    )
    await page.goto("https://example.test/article")
    await page.evaluate("scrollTo(0, 150)")
    result = await capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
        request=capture.PageCaptureRequest(
            mode=capture.PageCaptureMode.OBSERVE,
            startup_policy="direct_home",
            full_page=True,
            capture_dom=False,
            capture_visible_text=False,
            web_capture_stage="web_full_page_supplemental",
        ),
        context=page.context,
        page=page,
        console_logs=[],
        network_requests=[],
        network_inflight={},
        timeout_error_type=PlaywrightTimeoutError,
    )
    supplemental = result.metrics["web_full_page_stabilization"]
    assert supplemental["capture_mode"] == "direct_full_page"
    assert result.metrics["web_readiness"]["capture_consistent"] is True
    screenshots = [a for a in result.artifacts if a.artifact_type == "screenshot"]
    manifests = [a for a in result.artifacts if a.artifact_type == "page_coverage"]
    assert len(screenshots) == 1
    assert manifests == []
    assert screenshots[0].content["full_page"] is True
    payload = base64.b64decode(screenshots[0].content["data"])
    width, height = struct.unpack(">II", payload[16:24])
    assert width == 1920
    assert height >= 3500
    assert await page.evaluate("scrollY") == 150
    from types import SimpleNamespace

    from services.api.app.application.web_full_page_supplemental_capture import (
        _full_page_result_failure_reason,
    )
    from services.api.app.infrastructure.storage.local_evidence_store import (
        LocalEvidenceArtifactStore,
    )

    assert _full_page_result_failure_reason(
        result=result, command=SimpleNamespace(source_url=page.url)
    ) is None
    store = LocalEvidenceArtifactStore(root=tmp_path)
    artifact = await store.store(
        task_id="task-a",
        page_capture_id="capture-a",
        artifact_id="screenshot-a",
        artifact_type="screenshot",
        content=screenshots[0].content,
    )
    assert artifact.metadata["purpose"] == "web_direct_full_page"
    assert await store.read(artifact.storage_path) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", ["auto", "hidden"])
async def test_inner_scroller_still_saves_direct_full_page(chrome_page, overflow):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    import services.api.app.infrastructure.browser.page_capture as capture

    page = chrome_page
    await page.set_content(
        f'<main style="height:3500px"><div style="height:300px;overflow-y:{overflow}">'
        '<div style="height:900px">正文</div></div></main>'
    )
    result = await capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
        request=capture.PageCaptureRequest(
            mode=capture.PageCaptureMode.OBSERVE,
            startup_policy="direct_home",
            full_page=True,
            capture_dom=False,
            capture_visible_text=False,
            web_capture_stage="web_full_page_supplemental",
        ),
        context=page.context,
        page=page,
        console_logs=[],
        network_requests=[],
        network_inflight={},
        timeout_error_type=PlaywrightTimeoutError,
    )
    assert result.metrics["web_full_page_stabilization"]["capture_mode"] == "direct_full_page"
    screenshots = [a for a in result.artifacts if a.artifact_type == "screenshot"]
    assert len(screenshots) == 1
    assert screenshots[0].content["purpose"] == "web_direct_full_page"
    assert screenshots[0].content["full_page"] is True
    payload = base64.b64decode(screenshots[0].content["data"])
    width, height = struct.unpack(">II", payload[16:24])
    assert width == 1920
    assert height >= 3500
    assert not any(a.artifact_type == "page_coverage" for a in result.artifacts)
    assert await page.evaluate("scrollY") == 0

@pytest.mark.asyncio
async def test_direct_probe_timeout_never_selects_scroll_fallback():
    from services.api.app.application.web_full_page_stabilization import (
        probe_direct_full_page_candidate,
    )

    class BusyPage:
        async def evaluate(self, *_args, **_kwargs):
            raise TimeoutError

    direct, audit = await probe_direct_full_page_candidate(BusyPage())
    assert direct is False
    assert audit["capture_mode"] == "unavailable"
    assert audit["probe_reason"] == "timeout"


@pytest.mark.asyncio
async def test_horizontal_overflow_limit_rejects_direct_full_page_before_screenshot():
    from services.api.app.application.web_full_page_stabilization import (
        probe_direct_full_page_candidate,
    )

    class WidePage:
        async def evaluate(self, *_args, **_kwargs):
            return {
                "viewport_width": 1920,
                "document_width": 40000,
                "document_height": 2000,
            }

    direct, audit = await probe_direct_full_page_candidate(WidePage())
    assert direct is False
    assert audit["capture_mode"] == "unavailable"
    assert audit["probe_reason"] == "limit_exceeded"


@pytest.mark.asyncio
async def test_small_horizontal_overflow_allows_direct_full_page():
    from services.api.app.application.web_full_page_stabilization import (
        probe_direct_full_page_candidate,
    )

    class WidePage:
        async def evaluate(self, *_args, **_kwargs):
            return {
                "viewport_width": 1920,
                "document_width": 1930,
                "document_height": 18675,
            }

    direct, audit = await probe_direct_full_page_candidate(WidePage())
    assert direct is True
    assert audit["capture_mode"] == "direct_full_page"
    assert audit["document_width"] == 1930


@pytest.mark.asyncio
async def test_legacy_scrolled_viewport_helper_restores_scroll_position():
    from services.api.app.application.web_full_page_stabilization import (
        WebFullPageStabilizationPolicy,
        capture_scrolled_viewport,
    )

    class DensePage:
        y = 0

        async def evaluate(self, script, arg):
            if arg["operation"] == "scroll":
                self.y = arg["y"]
            return {
                "document_height": 5000,
                "viewport_height": 1000,
                "scroll_x": 0,
                "scroll_y": self.y,
            }

        async def screenshot(self, **kwargs):
            assert kwargs == {"full_page": False, "type": "png"}
            return b"viewport-png"

    page = DensePage()
    result = await capture_scrolled_viewport(
        page, WebFullPageStabilizationPolicy(scroll_offset_px=1920)
    )
    assert result.stable is True
    assert result.screenshot_bytes == b"viewport-png"
    assert result.actual_scroll_y == 1920
    assert page.y == 0


@pytest.mark.asyncio
async def test_pending_lazy_image_still_saves_direct_full_page(chrome_page):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    import services.api.app.infrastructure.browser.page_capture as capture

    page = chrome_page
    await page.set_content(
        '<main style="height:4000px"><img loading="lazy" '
        'src="https://example.test/pending.png" style="position:absolute;top:3600px"></main>'
    )
    result = await capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
        request=capture.PageCaptureRequest(
            mode=capture.PageCaptureMode.OBSERVE,
            startup_policy="direct_home",
            full_page=True,
            capture_dom=False,
            capture_visible_text=False,
            web_capture_stage="web_full_page_supplemental",
        ),
        context=page.context,
        page=page,
        console_logs=[],
        network_requests=[],
        network_inflight={},
        timeout_error_type=PlaywrightTimeoutError,
    )
    assert result.metrics["web_full_page_stabilization"]["capture_mode"] == "direct_full_page"
    screenshots = [a for a in result.artifacts if a.artifact_type == "screenshot"]
    assert len(screenshots) == 1
    assert screenshots[0].content["purpose"] == "web_direct_full_page"
    width, height = struct.unpack(
        ">II", base64.b64decode(screenshots[0].content["data"])[16:24]
    )
    assert width == 1920
    assert height >= 4000
    assert not any(a.artifact_type == "page_coverage" for a in result.artifacts)


@pytest.mark.asyncio
async def test_scrolled_viewport_accepts_browser_snapped_actual_position() -> None:
    api = module()

    class SnappingPage:
        y = 0

        async def evaluate(self, _script, arg):
            if arg["operation"] == "scroll":
                self.y = 2048 if arg["y"] else 0
                return None
            return {
                "document_height": 9000,
                "viewport_height": 1080,
                "scroll_x": 0,
                "scroll_y": self.y,
            }

        async def screenshot(self, **kwargs):
            assert kwargs == {"full_page": False, "type": "png"}
            return b"png-after-scroll-stop"

    result = await api.capture_scrolled_viewport(
        SnappingPage(), api.WebFullPageStabilizationPolicy(scroll_offset_px=1920)
    )

    assert result.stable is True
    assert result.actual_scroll_y == 2048
    assert result.screenshot_bytes == b"png-after-scroll-stop"
    assert result.scroll_restore_status == "restored"


@pytest.mark.asyncio
async def test_scrolled_viewport_keeps_screenshot_when_scroll_restore_fails() -> None:
    api = module()

    class RestoreFailingPage:
        y = 0

        async def evaluate(self, _script, arg):
            if arg["operation"] == "scroll":
                if arg["y"] == 0 and self.y != 0:
                    raise RuntimeError("restore failed")
                self.y = arg["y"]
                return None
            return {
                "document_height": 9000,
                "viewport_height": 1080,
                "scroll_x": 0,
                "scroll_y": self.y,
            }

        async def screenshot(self, **kwargs):
            assert kwargs == {"full_page": False, "type": "png"}
            return b"png-before-restore-failure"

    result = await api.capture_scrolled_viewport(
        RestoreFailingPage(), api.WebFullPageStabilizationPolicy(scroll_offset_px=1920)
    )

    assert result.stable is True
    assert result.reason_code is None
    assert result.screenshot_bytes == b"png-before-restore-failure"
    assert result.restored is False
    assert result.scroll_restore_status == "error"


@pytest.mark.asyncio
async def test_all_audited_hidden_layers_are_actually_hidden_at_the_layer_bound(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content(
        "<main>正文</main>"
        + '<div aria-hidden="true" style="position:fixed;width:10px;height:10px;'
        'pointer-events:none"></div>' * 33
    )
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    seen = []

    async def screenshot(**_kwargs):
        seen.extend(
            await page.locator("div").evaluate_all(
                "nodes => nodes.map(node => getComputedStyle(node).visibility)"
            )
        )
        return b"png"

    page.screenshot = screenshot
    _, audit = await api.capture_stabilized_screenshot(page, result)
    assert len(audit["sticky_layers"]) == 32
    assert seen[:32] == ["hidden"] * 32
    assert seen[32] == "visible"


@pytest.mark.asyncio
async def test_cancellation_audit_is_saved_with_phase_terminal():
    api = module()
    from services.api.tests.test_web_full_page_supplemental_capture import (
        _Browser,
        _Claims,
        _command,
        _service,
    )

    error = asyncio.CancelledError()
    error.stabilization_result = api.WebFullPageStabilizationResult(
        document_height=1200, stable=True, sticky_layers=({"action": "hidden"},)
    )
    browser, claims = _Browser(), _Claims()
    browser.error = error
    with pytest.raises(asyncio.CancelledError):
        await _service(browser=browser, claims=claims).capture(_command())
    assert claims.finished[0]["reason_code"] == "WEB_FULL_PAGE_SCREENSHOT_CANCELLED"
    assert claims.finished[0]["stabilization_audit"]["document_height"] == 1200


@pytest.mark.asyncio
async def test_state_hash_detects_changes_beyond_text_preview(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content("<main>" + "正文" * 200 + "原文</main>")
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())

    async def screenshot(**_kwargs):
        await page.locator("main").evaluate("node => node.append('改变')")
        return b"png"

    page.screenshot = screenshot
    with pytest.raises(api.WebFullPageStabilizationError) as caught:
        await api.capture_stabilized_screenshot(page, result)
    assert caught.value.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"


@pytest.mark.asyncio
async def test_cancellation_is_not_replaced_by_late_state_drift(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content("<main>正文</main>")
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())

    async def screenshot(**_kwargs):
        await page.locator("main").evaluate("node => node.append('变化')")
        raise asyncio.CancelledError()

    page.screenshot = screenshot
    with pytest.raises(asyncio.CancelledError):
        await api.capture_stabilized_screenshot(page, result)


@pytest.mark.asyncio
async def test_nested_decorative_layer_cannot_be_proven_outside_primary_subject(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content(
        '<div><div aria-hidden="true" style="position:fixed;'
        'width:50px;height:50px"></div>自定义主体</div>'
    )
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    _, audit = await api.capture_stabilized_screenshot(page, result)
    assert audit["sticky_layers"][0]["action"] == "retained"


@pytest.mark.asyncio
async def test_limit_gap_advances_to_classification_and_href_discovery():
    from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
    from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
    from services.api.tests.test_web_runtime_recovery import RUN_ID, setup

    checkpoints, effects, orchestrator = setup(WebRunStage.FULL_PAGE_CAPTURE)
    effects.failure = WebStageFailure("WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED")
    failed = await orchestrator.advance_once(RUN_ID, "page:1")
    assert failed.next_stage == "classification"
    assert failed.cursor.gaps[-1].error_code == "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
    effects.failure = None
    classified = await orchestrator.advance_once(RUN_ID, "page:1")
    assert classified.next_stage == "href_discovery"
    assert checkpoints.cursor.cursor_payload["capture_id"]


@pytest.mark.asyncio
async def test_custom_element_names_are_not_copied_into_sticky_audit(chrome_page):
    api = module()
    page = chrome_page
    await page.set_content(
        '<private-secret style="position:fixed;width:50px;'
        'height:50px;display:block">内容</private-secret>'
    )
    result = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    _, audit = await api.capture_stabilized_screenshot(page, result)
    assert "private-secret" not in json.dumps(audit)
    assert audit["sticky_layers"][0]["action"] == "retained"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "css,attributes",
    [
        ('.layer::before {content:"重要通知"}', ""),
        ('.layer::after {content:"警示"}', ""),
        (".layer {background:linear-gradient(red,blue)}", ""),
        (".layer {background:red}", ""),
        (".layer {mask-image:linear-gradient(red,blue)}", ""),
        ('.layer::before {content:"";background:red;width:20px;height:20px;display:block}', ""),
        ('.layer::after {content:"";border:10px solid red}', ""),
        (".layer {pointer-events:auto}", ""),
        ("", 'onpointerdown="void(0)"'),
        ("", 'role="checkbox"'),
    ],
)
async def test_generated_or_interactive_sticky_is_retained(chrome_page, css, attributes):
    api = module()
    page = chrome_page
    await page.set_content(
        "<style>.layer {position:fixed;width:50px;height:50px;"
        "pointer-events:none}" + css + '</style><div class="layer" '
        'aria-hidden="true" ' + attributes + "></div><main>正文</main>"
    )
    stabilized = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    _, audit = await api.capture_stabilized_screenshot(page, stabilized)
    assert audit["sticky_layers"][0]["action"] == "retained"


@pytest.mark.asyncio
async def test_service_deadline_preserves_real_helper_restoration_audit(chrome_page):
    from datetime import UTC, datetime, timedelta

    from services.api.tests.test_web_full_page_supplemental_capture import (
        _Claims,
        _command,
        _Persistence,
        _service,
    )

    api = module()
    page = chrome_page
    await page.set_content(
        '<div aria-hidden="true" style="position:fixed;width:50px;'
        'height:50px;pointer-events:none"></div><main>正文</main>'
    )
    screenshot_started = False

    async def screenshot(**_kwargs):
        nonlocal screenshot_started
        screenshot_started = True
        await asyncio.Event().wait()

    page.screenshot = screenshot

    class Browser:
        async def capture(self, request):
            stabilized = await api.stabilize_for_full_page(
                page, request.web_full_page_stabilization_policy
            )
            return await api.capture_stabilized_screenshot(page, stabilized)

    claims, persistence = _Claims(), _Persistence()
    command = replace(_command(), deadline=datetime.now(UTC) + timedelta(milliseconds=800))
    result = await _service(browser=Browser(), claims=claims, persistence=persistence).capture(
        command
    )
    assert screenshot_started
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert persistence.calls == []
    audit = claims.finished[0]["stabilization_audit"]
    assert audit["sticky_layers"][0]["action"] == "hidden"
    assert audit["scroll_restore_status"] == "restored"
    assert audit["style_restore_status"] == "restored"
    assert audit["restored"] is True
    assert (
        await page.locator("div").evaluate("node => getComputedStyle(node).visibility") == "visible"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_scroll_restore_failure_preserves_cancellation_and_finishes_cleanup(failure, cancel):
    api = module()
    scrolled = asyncio.Event()

    class BrokenRestoration(Document):
        cleanup_active = False

        async def evaluate(self, script, arg):
            if arg["operation"] == "scroll":
                if arg["y"] == 75:
                    self.cleanup_active = True
                    try:
                        if failure == "error":
                            raise RuntimeError("滚动恢复失败")
                        await asyncio.Event().wait()
                    finally:
                        self.cleanup_active = False
                scrolled.set()
            return await super().evaluate(script, arg)

    page = BrokenRestoration((1200,) * 10)
    task = asyncio.create_task(
        api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    )
    if cancel:
        await scrolled.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        result = caught.value.stabilization_result
    else:
        result = await task
        assert result.stable is False
        assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert result.audit["scroll_restore_status"] == failure
    assert result.audit["style_restore_status"] == "not_attempted"
    assert result.restored is False
    assert page.cleanup_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("heights,timeout", [((70000,), 8000), ((1200,), 10)])
async def test_limit_and_lazy_timeout_audits_report_successful_scroll_restore(heights, timeout):
    api = module()
    result = await api.stabilize_for_full_page(
        Document(heights), api.WebFullPageStabilizationPolicy(lazy_load_timeout_ms=timeout)
    )
    if heights == (70000,):
        assert result.reason_code is None
        assert result.stable is True
        assert result.truncated is True
    else:
        assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert result.restored is True
    assert result.audit["scroll_restore_status"] == "restored"
    assert result.audit["style_restore_status"] == "not_attempted"


@pytest.mark.asyncio
async def test_cancellation_during_successful_scroll_cleanup_keeps_true_restore_status():
    api = module()
    restoring, release = asyncio.Event(), asyncio.Event()

    class SlowRestoration(Document):
        async def evaluate(self, script, arg):
            if arg["operation"] == "scroll" and arg["y"] == 75:
                restoring.set()
                await release.wait()
            return await super().evaluate(script, arg)

    page = SlowRestoration((1200,) * 10)
    task = asyncio.create_task(
        api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    )
    await restoring.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.stabilization_result.scroll_restore_status == "restored"
    assert caught.value.stabilization_result.restored is True
    assert page.y == 75


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pseudo,property_name",
    [
        ("", "filter"),
        ("", "backdrop-filter"),
        ("::before", "filter"),
        ("::before", "backdrop-filter"),
        ("::after", "filter"),
        ("::after", "backdrop-filter"),
        ("", "mix-blend-mode"),
    ],
)
async def test_filtered_transparent_layers_remain_visible(chrome_page, pseudo, property_name):
    api = module()
    page = chrome_page
    value = "multiply" if property_name == "mix-blend-mode" else "blur(4px)"
    await page.set_content(
        "<style>.layer {position:fixed;top:0;left:0;width:100px;"
        "height:100px;pointer-events:none}.layer"
        + pseudo
        + '{content:"";'
        + property_name
        + ":"
        + value
        + "}</style>"
        '<div aria-hidden="true" class="layer"></div>'
        "<main>清晰可读的主体内容</main>"
    )
    computed = await page.locator(".layer").evaluate(
        "(node, arg) => getComputedStyle(node, arg.pseudo).getPropertyValue(arg.name)",
        {"pseudo": pseudo or None, "name": property_name},
    )
    assert computed == value
    stabilized = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    _, audit = await api.capture_stabilized_screenshot(page, stabilized)
    assert audit["sticky_layers"][0]["action"] == "retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_late_probe_cancel_survives_previous_style_restore_failure(failure):
    api = module()
    probing = asyncio.Event()

    class FailedStyleRestoration(Document):
        restore_attempted = False
        cleanup_active = False

        async def evaluate(self, script, arg):
            if arg["operation"] == "restore":
                self.restore_attempted = True
                self.cleanup_active = True
                try:
                    if failure == "error":
                        raise RuntimeError("样式恢复失败")
                    await asyncio.Event().wait()
                finally:
                    self.cleanup_active = False
            if arg["operation"] == "sample" and self.restore_attempted:
                probing.set()
                await asyncio.Event().wait()
            return await super().evaluate(script, arg)

    page = FailedStyleRestoration((1200,) * 10)
    stabilized = await api.stabilize_for_full_page(page, api.WebFullPageStabilizationPolicy())
    task = asyncio.create_task(api.capture_stabilized_screenshot(page, stabilized))
    await probing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert task.cancelled() is True
    assert page.cleanup_active is False
    assert caught.value.stabilization_result.style_restore_status == failure
    assert caught.value.stabilization_result.scroll_restore_status == "restored"
    assert caught.value.stabilization_result.restored is False
