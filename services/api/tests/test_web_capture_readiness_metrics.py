"""Web Capture 就绪指标写入测试。"""

import pytest

from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
from services.api.app.infrastructure.browser.page_capture import (
    _web_readiness_metrics,
)


def test_inspectable_dynamic_is_not_stable_evidence() -> None:
    from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment

    metrics = _web_readiness_metrics(
        web_capture_stage="web_page_ready_baseline",
        identity_before="https://example.test/list",
        identity_after="https://example.test/list",
        page_settle_complete=False,
        page_inspection_ready=True,
        readiness={
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
        },
        network_observable=True,
        critical_network_pending=1,
    )
    assessment = read_web_readiness_assessment({"web_readiness": metrics})
    assert assessment is not None
    assert assessment.status.value != "stable"
    assert assessment.can_serve_evidence is False


def test_blank_document_is_a_typed_capture_failure() -> None:
    metrics = _stable_capture_metrics(
        page_settle_complete=False,
        page_inspection_ready=False,
        readiness={
            "blank_document": True,
            "core_present": False,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
        },
        settle_diagnostics={
            "outcome": "settle_timeout",
            "settle_complete": False,
            "quorum_met": False,
            "timed_out": True,
        },
    )

    assessment = read_web_readiness_assessment({"web_readiness": metrics})

    assert assessment.status.value == "capture_failed"
    assert assessment.blocking_reasons == ("blank_document",)


def test_non_web_capture_does_not_emit_web_readiness_metadata() -> None:
    assert (
        _web_readiness_metrics(
            web_capture_stage=None,
            identity_before="https://example.test/",
            identity_after="https://example.test/",
            page_settle_complete=True,
            page_inspection_ready=True,
            readiness={"core_present": True},
            network_observable=True,
            critical_network_pending=0,
        )
        is None
    )


def _stable_capture_metrics(**overrides):
    boundary = {
        "document_time_origin_ms": 1000,
        "page_url_sha256": "url-1",
        "context_page_urls_sha256": ["url-1"],
        "active_page_index": 0,
        "visibility_state": "visible",
        "top_layer_sha256": None,
        "document_observation_available": True,
    }
    values = {
        "web_capture_stage": "web_page_ready_baseline",
        "identity_before": "https://example.test/list",
        "identity_after": "https://example.test/list",
        "page_settle_complete": True,
        "page_inspection_ready": True,
        "readiness": {
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
        },
        "network_observable": True,
        "critical_network_pending": 0,
        "settle_diagnostics": {
            "outcome": "settled",
            "settle_complete": True,
            "quorum_met": True,
            "sample_count": 3,
            "stable_sample_count": 3,
            "sample_window_started_ms": 10000,
            "sample_window_ended_ms": 10600,
            "document_time_origin_ms": 1000,
            "structure_digest": "structure-1",
        },
        "capture_consistency": {
            "status": "consistent_with_capability_gaps",
            "changed_signals": [],
            "unavailable_signals": ["account_identity"],
            "before": dict(boundary),
            "after": dict(boundary),
        },
        "capture_started_ms": 10610,
        "capture_ended_ms": 10900,
        "structure_before": "structure-1",
        "structure_after": "structure-1",
    }
    values.update(overrides)
    return _web_readiness_metrics(**values)


def test_proven_window_is_admitted_by_real_reader():
    result = read_web_readiness_assessment({"web_readiness": _stable_capture_metrics()})
    assert result.can_serve_evidence is True


def test_large_page_atomic_capture_with_unchanged_boundaries_is_admitted():
    """大型 DOM/UI Snapshot 可超过旧 5 秒阈值，但仍须保持完整原子边界。"""

    metrics = _stable_capture_metrics(capture_ended_ms=18_110)

    result = read_web_readiness_assessment({"web_readiness": metrics})

    assert metrics["capture_consistent"] is True
    assert result.can_serve_evidence is True


@pytest.mark.parametrize(
    ("media_state", "critical", "complete", "expected"),
    [
        ("degraded", False, True, "degraded"),
        ("critical_failed", True, True, "timeout"),
        ("pending", True, False, "timeout"),
        ("unknown", True, None, "timeout"),
    ],
)
def test_terminal_media_failure_has_an_audited_nonformal_outcome(
    media_state,
    critical,
    complete,
    expected,
):
    media = {
        "schema_version": "viewport-media.v1",
        "state": media_state,
        "sample_truncated": False,
        "items": [
            {
                "tag": "img",
                "critical": critical,
                "complete": complete,
                "natural_width": 0,
                "resource_state": (
                    "failed" if complete else "pending" if complete is False else "unknown"
                ),
                "current_src": "https://images.example.test/image.jpg",
            }
        ],
    }
    metrics = _stable_capture_metrics(
        readiness={
            "core_present": True,
            "visible_loader": True,
            "blocking_loader": False,
            "transition_active": False,
            "necessary_media_ready": False,
            "dom_quiet_ms": 1200,
            "viewport_media": media,
        }
    )
    metrics.update(timed_out=True, settle_complete=False, quorum_met=False)

    assert metrics["viewport_media"] == media
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.status.value == expected
    assert result.can_serve_evidence is False
    if expected == "degraded":
        assert result.blocking_reasons == ("noncritical_media_terminal_failure",)


def test_degraded_media_never_overrides_capture_identity_drift():
    metrics = _stable_capture_metrics()
    metrics.update(
        timed_out=True,
        identity_after="https://other.example.test/",
        viewport_media={"schema_version": "viewport-media.v1", "state": "degraded"},
    )
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.status.value == "identity_drift"


def test_aggregate_image_ratio_never_hides_explicit_failed_primary_media():
    metrics = _stable_capture_metrics(
        readiness={
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
            "viewport_media": {"schema_version": "viewport-media.v1", "state": "critical_failed"},
        }
    )
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.can_serve_evidence is False


def test_v2_necessary_media_set_replaces_legacy_whole_viewport_ratio():
    metrics = _stable_capture_metrics(
        readiness={
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": False,
            "dom_quiet_ms": 1200,
            "viewport_media": {
                "schema_version": "viewport-media.v2",
                "state": "ready",
                "necessary_count": 0,
                "necessary_ready_count": 0,
                "noncritical_resource_counts": {"unknown": 1},
                "warnings": ["noncritical_media_unknown"],
                "sample_truncated": False,
            },
        }
    )
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.can_serve_evidence is True
    assert metrics["viewport_media"]["warnings"] == ["noncritical_media_unknown"]


@pytest.mark.parametrize(
    "change",
    [
        {"necessary_count": 1},
        {"necessary_count": True},
        {"necessary_ready_count": -1},
        {"sample_truncated": True},
    ],
)
def test_v2_ready_label_cannot_override_unproven_necessary_counts(change):
    media = {
        "schema_version": "viewport-media.v2",
        "state": "ready",
        "necessary_count": 0,
        "necessary_ready_count": 0,
        "sample_truncated": False,
    }
    media.update(change)
    metrics = _stable_capture_metrics(
        readiness={
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": False,
            "dom_quiet_ms": 1200,
            "viewport_media": media,
        }
    )
    assert read_web_readiness_assessment({"web_readiness": metrics}).can_serve_evidence is False


@pytest.mark.parametrize(
    "change",
    [
        {"structure_after": "structure-2"},
        {"capture_ended_ms": 99999},
        {"network_observable": False, "critical_network_pending": None},
        {
            "settle_diagnostics": {
                "outcome": "settled",
                "quorum_met": True,
                "stable_sample_count": 1,
            }
        },
    ],
)
def test_unproven_window_cannot_enter_business_graph_or_bundle(change):
    from services.api.app.application.web_evidence_admission import (
        assess_web_capture_evidence_admission,
    )

    result = assess_web_capture_evidence_admission(
        {"web_readiness": _stable_capture_metrics(**change)},
        capture_role="after_action",
    )
    assert result.can_project_business_node is False
    assert result.can_enter_formal_bundle is False


def test_same_url_new_document_is_not_same_epoch():
    metrics = _stable_capture_metrics()
    metrics["capture_consistency"]["after"]["document_time_origin_ms"] = 2000
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.can_serve_evidence is False


def test_missing_stable_facts_are_not_upgraded_from_legacy_metadata():
    metrics = _stable_capture_metrics()
    for field in ("settle_complete", "quorum_met", "capture_consistent", "profile_version"):
        metrics.pop(field, None)
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.can_serve_evidence is False


def test_waiting_is_not_timeout_or_capture_skew():
    stable = _stable_capture_metrics()
    wait = dict(stable["settle_diagnostics"], settle_complete=False, quorum_met=False)
    metrics = _stable_capture_metrics(page_settle_complete=False, settle_diagnostics=wait)
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.status.value == "loading"
    assert metrics["capture_consistent"] is True


def test_human_verification_keeps_distinct_blocking_reason():
    readiness = dict(
        _stable_capture_metrics()["settle_diagnostics"].get("readiness") or {},
        core_present=True,
        necessary_media_ready=True,
        visible_loader=False,
        transition_active=False,
        dom_quiet_ms=1200,
        human_verification_present=True,
    )
    metrics = _stable_capture_metrics(readiness=readiness)
    result = read_web_readiness_assessment({"web_readiness": metrics})
    assert result.status.value == "human_verification_blocked"
    assert result.can_serve_evidence is False


def test_local_loader_after_settle_is_not_hidden_by_blocking_loader_flag():
    readiness = {
        "core_present": True,
        "necessary_media_ready": True,
        "visible_loader": True,
        "blocking_loader": False,
        "transition_active": False,
        "dom_quiet_ms": 1200,
    }
    result = read_web_readiness_assessment(
        {"web_readiness": _stable_capture_metrics(readiness=readiness)}
    )
    assert result.can_serve_evidence is False
    assert "visible_loader" in result.blocking_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("changing,expected", [(False, True), (True, False)])
async def test_web_waiter_requires_repeated_structure_in_same_document(
    monkeypatch, changing, expected
):
    import asyncio
    from types import SimpleNamespace

    from services.api.app.application.web_readiness_contracts import WebReadinessProfile
    from services.api.app.infrastructure.browser import page_capture as capture

    samples = 0

    async def structure(page):
        nonlocal samples
        samples += 1
        return {
            "structure_digest": str(samples) if changing else "stable-structure",
            "document_time_origin_ms": 1000,
            "observed_at_ms": capture.time.perf_counter() * 1000,
        }

    async def readiness(page):
        return {
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
        }

    async def monitor(page, **kwargs):
        return 0

    async def tick(**kwargs):
        await asyncio.sleep(0.005)
        return 0

    monkeypatch.setattr(capture, "_read_web_structure", structure)
    monkeypatch.setattr(capture, "_read_page_readiness", readiness)
    monkeypatch.setattr(capture, "_install_page_state_monitor", monitor)
    monkeypatch.setattr(capture, "_wait_for_page_state_change", tick)
    diagnostics = {}
    result = await capture._wait_for_web_page_settle(
        page=SimpleNamespace(url="https://example.test/list"),
        network_inflight={},
        max_wait_ms=50,
        poll_interval_ms=5,
        profile=WebReadinessProfile(minimum_window_ms=10),
        diagnostics=diagnostics,
    )
    assert result is expected
    assert diagnostics["quorum_met"] is expected
    assert diagnostics["timed_out"] is (not expected)
    assert diagnostics["sample_count"] >= 3


def test_default_network_scope_ignores_long_poll_and_third_party_but_counts_boot_script():
    from services.api.app.application.web_readiness_contracts import DEFAULT_WEB_READINESS_PROFILE
    from services.api.app.infrastructure.browser.page_capture import _web_critical_network_pending

    pending = _web_critical_network_pending(
        {
            1: {"url": "https://example.test/events", "resource_type": "fetch"},
            2: {"url": "https://ads.test/stream", "resource_type": "script"},
            3: {"url": "https://example.test/bootstrap.js", "resource_type": "script"},
        },
        page_url="https://example.test/list",
        profile=DEFAULT_WEB_READINESS_PROFILE,
    )
    assert pending == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "reload", "structure", "human"])
async def test_web_collector_proves_real_browser_boundary(monkeypatch, mutation):
    import os
    from pathlib import Path

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser import page_capture as capture

    executable = os.environ.get("ALIOTH_WEB_TEST_BROWSER")
    if not executable:
        executable = next(
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
    if executable is None:
        pytest.skip("本地 Web 专属浏览器测试需要 ALIOTH_WEB_TEST_BROWSER")
    original = capture._collect_artifacts

    async def collect(**kwargs):
        result = await original(**kwargs)
        if mutation == "reload":
            await kwargs["page"].reload()
        elif mutation == "structure":
            await kwargs["page"].evaluate(
                "document.querySelector('main').append(document.createElement('button'))"
            )
        return result

    monkeypatch.setattr(capture, "_collect_artifacts", collect)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context()
            html = (
                "<html><title>本地证据</title><body><main><h1>内容列表</h1>"
                "<a href='/detail/1'>正文</a></main>"
            )
            if mutation == "human":
                html += (
                    "<div role='dialog' aria-modal='true' id='captcha'>"
                    "<button>人机验证</button></div>"
                )
            await context.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=html + "</body></html>",
                ),
            )
            page = await context.new_page()
            console_logs, network_requests, inflight = [], [], {}
            capture._attach_observers(page, console_logs, network_requests, inflight)
            await page.goto("https://example.test/list")
            result = await capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
                request=capture.PageCaptureRequest(
                    mode=capture.PageCaptureMode.OBSERVE,
                    startup_policy="direct_home",
                    web_capture_stage="web_page_ready_baseline",
                ),
                context=context,
                page=page,
                console_logs=console_logs,
                network_requests=network_requests,
                network_inflight=inflight,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assessment = read_web_readiness_assessment(result.metrics)
            assert assessment.can_serve_evidence is (mutation is None), assessment
            if mutation == "human":
                assert assessment.status.value == "human_verification_blocked"
            assert any(x.artifact_type == "screenshot" for x in result.artifacts)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_web_collector_freezes_swiper_during_atomic_artifact_window(monkeypatch):
    """轮播周期长于 settle 窗口时，正式制品仍须来自同一完整结构。"""
    import os
    from pathlib import Path

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser import page_capture as capture

    executable = os.environ.get("ALIOTH_WEB_TEST_BROWSER")
    if not executable:
        executable = next(
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
    if executable is None:
        pytest.skip("本地 Web 专属浏览器测试需要 ALIOTH_WEB_TEST_BROWSER")
    original = capture._collect_artifacts

    async def collect(**kwargs):
        result = await original(**kwargs)
        await kwargs["page"].wait_for_timeout(2500)
        return result

    monkeypatch.setattr(capture, "_collect_artifacts", collect)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context()
            await context.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=(
                        "<html><title>轮播证据</title><body><main>"
                        "<div class='swiper'><div id='track'>广告一</div></div>"
                        "<a href='/detail/1'>正文</a></main>"
                        "<script>"
                        "const root=document.querySelector('.swiper');"
                        "let n=0; let timer=setInterval(()=>{"
                        "track.style.transform=`translateX(${++n}px)`;"
                        "},2500);"
                        "root.swiper={autoplay:{running:true,stop(){"
                        "clearInterval(timer);this.running=false;"
                        "},start(){this.running=true;}}};"
                        "</script></body></html>"
                    ),
                ),
            )
            page = await context.new_page()
            console_logs, network_requests, inflight = [], [], {}
            capture._attach_observers(page, console_logs, network_requests, inflight)
            await page.goto("https://example.test/list")
            result = await capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
                request=capture.PageCaptureRequest(
                    mode=capture.PageCaptureMode.OBSERVE,
                    startup_policy="direct_home",
                    web_capture_stage="web_page_ready_baseline",
                ),
                context=context,
                page=page,
                console_logs=console_logs,
                network_requests=network_requests,
                network_inflight=inflight,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assessment = read_web_readiness_assessment(result.metrics)
            assert assessment.can_serve_evidence is True, assessment
            stabilization = result.metrics["web_capture_stabilization"]
            assert stabilization["schema_version"] == "web-capture-stabilization.v1"
            assert stabilization["status"] == "restored"
            assert stabilization["swiper_autoplay_paused"] == 1
            assert stabilization["bounded_settle_complete"] is True
            readiness = result.metrics["web_readiness"]
            assert readiness["structure_before"] == readiness["structure_after"]
        finally:
            await browser.close()


def test_ready_baseline_emits_classification_role_and_identity_sandwich() -> None:
    result = _stable_capture_metrics()
    assert result["role"] == "classification"
    assert result["artifacts_same_epoch"] is True
    assert result["profile_version"] == "web-structure.v1"
    assert read_web_readiness_assessment({"web_readiness": result}).can_serve_evidence


def test_page_probe_uses_interaction_after_role_and_unknown_network() -> None:
    result = _web_readiness_metrics(
        web_capture_stage="web_page_probe",
        identity_before="https://example.test/list",
        identity_after="https://example.test/detail/1",
        page_settle_complete=False,
        page_inspection_ready=False,
        readiness={
            "core_present": False,
            "blocking_loader": True,
            "blocking_transition": False,
            "necessary_media_ready": False,
            "dom_quiet_ms": 100,
        },
        network_observable=False,
        critical_network_pending=None,
    )

    assert result is not None
    assert result["role"] == "interaction_after"
    assert result["identity_before"] == "https://example.test/list"
    assert result["identity_after"] == "https://example.test/detail/1"
    assert result["artifacts_same_epoch"] is False
    assert result["timed_out"] is False
    assert result["settle_complete"] is False
    assert result["network_capability"] == "unknown"
    assert result["critical_network_pending"] is None
