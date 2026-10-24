"""中性受控 Chrome 自然滚动，真实稳定采样与附件边界不得替身。"""

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
from services.api.app.infrastructure.browser import page_capture as capture


@pytest.mark.parametrize("primary", [True, False])
def test_only_verified_primary_scroll_passes_its_selector_to_browser(primary):
    from services.api.app.application.interaction_runtime import ResolvedInteractionTarget
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.capture_runtime import (
        _build_browser_action_request,
    )

    request = SimpleNamespace(
        intent=SimpleNamespace(
            parameters={"target_mode": "web_primary_content_candidate"} if primary else {},
            action_type=InteractionOperation.SCROLL,
            session_id="web:controlled",
        )
    )
    result = _build_browser_action_request(
        request,
        target=ResolvedInteractionTarget(
            selector="#body",
            target_source="web_primary_content_candidate",
        ),
        timeout_seconds=10,
    )
    assert result.selector == ("#body" if primary else None)


def test_h5_detail_scroll_capture_options_are_forwarded_to_browser_request():
    from services.api.app.application.interaction_runtime import ResolvedInteractionTarget
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.capture_runtime import (
        _build_browser_action_request,
    )

    result = _build_browser_action_request(
        SimpleNamespace(
            intent=SimpleNamespace(
                parameters={
                    "target_mode": "h5_detail_scroll_coverage",
                    "scroll_max_segments": 2,
                    "split_scroll_screenshots": True,
                },
                action_type=InteractionOperation.SCROLL,
                session_id="h5:detail",
            )
        ),
        target=ResolvedInteractionTarget(selector=None),
        timeout_seconds=10,
    )

    assert result.scroll_max_segments == 2
    assert result.split_scroll_screenshots is True


@pytest.mark.parametrize(
    ("target_mode", "expected"),
    [
        ("popup_dismissal", True),
        ("h5_probe_popup_dismissal", False),
    ],
)
def test_only_h5_main_popup_exploration_can_continue_with_next_layer(
    target_mode: str,
    expected: bool,
) -> None:
    from services.api.app.application.interaction_runtime import ResolvedInteractionTarget
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.capture_runtime import (
        _build_browser_action_request,
    )

    result = _build_browser_action_request(
        SimpleNamespace(
            intent=SimpleNamespace(
                parameters={"target_mode": target_mode},
                action_type=InteractionOperation.CLICK,
                session_id="h5:popup",
            )
        ),
        target=ResolvedInteractionTarget(
            selector=".van-overlay:visible",
            clear_obstructions=True,
            allow_high_z_obstruction_hide=True,
            h5_popup_dismissal_strategy="safe_outside_then_close",
        ),
        timeout_seconds=30,
    )

    assert result.allow_h5_popup_layer_continuation is expected


def test_primary_content_stage_is_accepted_by_real_browser_request_validation():
    capture._validate_request(
        capture.PageCaptureRequest(
            mode=capture.PageCaptureMode.OBSERVE,
            startup_policy="direct_home",
            web_capture_stage="web_primary_content_evidence",
        )
    )


def _detail_scroll_request(*, target_source="web_detail_candidate"):
    from services.api.app.application.interaction_runtime import ResolvedInteractionTarget
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.capture_runtime import (
        _build_browser_action_request,
    )

    return _build_browser_action_request(
        SimpleNamespace(intent=SimpleNamespace(
            parameters={
                "target_mode": "web_detail_candidate", "web_capture_stage": "web_page_probe",
            },
            action_type=InteractionOperation.SCROLL, session_id="web:controlled",
        )),
        target=ResolvedInteractionTarget(selector="#detail", target_source=target_source),
        timeout_seconds=10,
    )


@pytest.mark.parametrize(
    "source", ("web_detail_candidate", "web_primary_content_candidate", "untrusted"),
)
def test_detail_scroll_keeps_only_the_matching_resolved_selector(source):
    assert _detail_scroll_request(target_source=source).selector == (
        "#detail" if source == "web_detail_candidate" else None
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"), reason="须显式配置受控Chrome")
async def test_detail_scroll_recaptures_target_below_fixed_header_with_positive_hit_test():
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"], headless=True,
        )
        try:
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})
            await context.route("**/*", lambda route: route.fulfill(
                content_type="text/html",
                body='<html><title>受控详情列表</title><body style="margin:0">'
                '<nav style="position:fixed;top:0;height:210px;width:100%;'
                'z-index:999;background:white">固定导航</nav>'
                '<main style="padding-top:1877.5px;margin-left:638px;width:644px">'
                '<article class="card"><a id="detail" href="/archives/114291/" '
                'style="display:block;height:180px">详情一</a></article>'
                '<article class="card"><a href="/archives/114292/">详情二</a></article>'
                '</main><footer style="height:3000px">页尾</footer></body></html>',
            ))
            page = await context.new_page()
            logs, requests, inflight = [], [], {}
            capture._attach_observers(page, logs, requests, inflight)
            await page.goto("https://example.test/list/")
            before = await capture._collect_ui_snapshot(page)
            before_target = next(
                item for item in before["elements"] if item.get("selector") == "#detail"
            )
            assert before_target["rect"]["y"] > 1080
            result = await capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
                request=replace(_detail_scroll_request(),
                    full_page=False, startup_policy="direct_home", clear_obstructions=False,
                ),
                context=context, page=page, console_logs=logs, network_requests=requests,
                network_inflight=inflight, timeout_error_type=PlaywrightTimeoutError,
            )
            assert result.execution_status == "success"
            after = next(
                item.content for item in result.artifacts if item.artifact_type == "ui_snapshot"
            )
            if after.get("sequence") is True:
                after = after["frames"][-1]
            target = next(item for item in after["elements"] if item.get("selector") == "#detail")
            assert target["href"] == before_target["href"]
            assert 210 <= target["rect"]["y"] < 900
            assert target["hit_test"]["center_hits_target"] is True
            scrolls = [item for item in result.action_trace if item.get("action") == "scroll"]
            assert len(scrolls) == 1
        finally:
            await browser.close()


def test_h5_payment_horizontal_options_are_forwarded_without_affecting_web_scroll():
    from services.api.app.application.interaction_runtime import ResolvedInteractionTarget
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.capture_runtime import (
        _build_browser_action_request,
    )

    result = _build_browser_action_request(
        SimpleNamespace(
            intent=SimpleNamespace(
                parameters={
                    "target_mode": "h5_payment_horizontal_collection",
                    "horizontal_scroll_max_segments": 6,
                    "split_scroll_screenshots": True,
                },
                action_type=InteractionOperation.SCROLL,
                session_id="h5:payment",
            )
        ),
        target=ResolvedInteractionTarget(selector=".vip-plan-list"),
        timeout_seconds=10,
    )

    assert result.selector == ".vip-plan-list"
    assert result.horizontal_scroll_max_segments == 6
    assert result.split_scroll_screenshots is True


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"), reason="须显式配置受控Chrome")
async def test_natural_primary_scroll_collects_its_own_stable_window_and_single_viewport():
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    executable = os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"]
    assert Path(executable).is_file()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1000, "height": 800})
            await context.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body='<html><title>受控正文</title><body style="margin:0">'
                    '<aside class="advertisement" style="height:900px">中性广告占位</aside>'
                    '<article id="body" style="height:600px">'
                    '<h1>真实正文</h1><p>中性文字</p></article>'
                    '<footer style="height:400px">页脚</footer></body></html>',
                ),
            )
            page = await context.new_page()
            logs, requests, inflight = [], [], {}
            capture._attach_observers(page, logs, requests, inflight)
            await page.goto("https://example.test/article/1")
            result = await capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
                request=capture.PageCaptureRequest(
                    mode=capture.PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=capture.PageTriggerAction.SCROLL,
                    selector="#body",
                    full_page=False,
                    startup_policy="direct_home",
                    clear_obstructions=False,
                    web_capture_stage="web_primary_content_evidence",
                ),
                context=context,
                page=page,
                console_logs=logs,
                network_requests=requests,
                network_inflight=inflight,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert result.execution_status == "success"
            readiness = read_web_readiness_assessment(result.metrics)
            assert readiness.can_serve_evidence, result.metrics.get("web_readiness")
            assert await page.evaluate("window.scrollY") > 0
            screenshot = [item for item in result.artifacts if item.artifact_type == "screenshot"]
            assert len(screenshot) == 1
            assert screenshot[0].content.get("sequence") is not True
            assert (
                len([item for item in result.action_trace if item.get("action") == "scroll"]) == 1
            )
        finally:
            await browser.close()
