"""Web 业务页广告落地证据调度契约。"""

from __future__ import annotations

import importlib
import importlib.util
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.infrastructure.browser import capture_runtime as capture_runtime_module
from services.api.app.infrastructure.browser.page_capture import (
    BlockedPageCaptureActionError,
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    PageTriggerAction,
)


@pytest.mark.parametrize("action", ["click", "goto", "hover", "evaluate", "direct_ad_outbound"])
def test_page_capture_request_rejects_every_ad_trigger_action(action):
    from services.api.app.infrastructure.browser.page_capture import _validate_request

    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        url="https://ads.example/landing",
        trigger_action=action,
        navigation_depth=1,
        capture_ad_landing_evidence=True,
    )
    with pytest.raises(BlockedPageCaptureActionError) as caught:
        _validate_request(request)
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"click_x_ratio": 0.5}, {"_cli_session": object()}])
async def test_page_capture_dispatcher_rejects_ad_click_before_any_driver_path(extra):
    from services.api.app.infrastructure.browser.page_capture import _run_single_trigger

    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action="click",
        selector="#ad",
        navigation_depth=1,
        capture_ad_landing_evidence=True,
        **extra,
    )
    trace = []
    with pytest.raises(BlockedPageCaptureActionError) as caught:
        await _run_single_trigger(
            page=object(),
            context=object(),
            request=request,
            action_trace=trace,
            timeout_ms=100,
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"
    assert trace == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "goto", "content", "close"])
@pytest.mark.parametrize("javascript", [False, True])
async def test_isolated_ad_href_is_readonly_and_closes_without_touching_source(
    monkeypatch,
    failure,
    javascript,
):
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
    from services.api.app.infrastructure.browser import ad_isolated_page

    events = []
    source = SimpleNamespace(url="https://source.example/list")

    class Page:
        url = "about:blank"

        async def goto(self, url, **kwargs):
            events.append(("goto", url, kwargs["wait_until"]))
            if failure == "goto":
                raise RuntimeError("模拟导航失败")
            self.url = url

        async def content(self):
            if failure == "content":
                raise RuntimeError("模拟采证失败")
            return "<html>landing</html>"

        async def title(self):
            return "landing"

        async def screenshot(self, **kwargs):
            events.append(("screenshot", self.url))
            return b"png"

    class Context:
        async def add_init_script(self, script):
            assert 'RTCPeerConnection' in script

        async def route(self, *args):
            pass

        async def route_web_socket(self, *args):
            pass

        async def new_page(self):
            return Page()

        async def unroute_all(self, *, behavior):
            assert behavior == "ignoreErrors"
            events.append("unrouted")

        async def close(self):
            events.append("closed")
            if failure == "close":
                raise RuntimeError("模拟关闭失败")

    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs["java_script_enabled"] is javascript
            assert kwargs["service_workers"] == "block"
            assert kwargs["user_agent"] == "Mozilla/5.0 Chrome/154.0.0.0 Safari/537.36"
            return Context()

    class Transport:
        async def fetch(self, url, **kwargs):
            return SimpleNamespace(url=url)

    monkeypatch.setattr(ad_isolated_page, "AdSafeTransport", lambda **kwargs: Transport())

    async def capture():
        async with ad_isolated_page.isolated_ad_page(
            browser=Browser(),
            url="https://ads.example/landing",
            source_url=source.url,
            timeout_ms=100,
            enable_javascript=javascript,
            user_agent="Mozilla/5.0 Chrome/154.0.0.0 Safari/537.36",
        ) as (landing, _):
            assert await landing.content() == "<html>landing</html>"
            assert await landing.screenshot(full_page=False) == b"png"
            for method in ("click", "locator", "mouse", "evaluate", "context", "goto"):
                with pytest.raises(AdOutboundDenied, match="WEB_AD_CLICK_FORBIDDEN"):
                    getattr(landing, method)

    if failure == "close":
        with pytest.raises(AdOutboundDenied, match="WEB_AD_LANDING_RETURN_FAILED"):
            await capture()
    elif failure:
        with pytest.raises(RuntimeError):
            await capture()
    else:
        await capture()
    assert source.url == "https://source.example/list"
    expected = [("goto", "https://ads.example/landing", "domcontentloaded")]
    if failure not in {"goto", "content"}:
        expected.append(("screenshot", "https://ads.example/landing"))
    expected.extend(("unrouted", "closed"))
    assert events == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('javascript', [False, True])
async def test_isolated_ad_goto_builds_complete_after_page_artifact_payloads(
    monkeypatch, javascript,
):
    from services.api.app.infrastructure.browser import ad_isolated_page, page_capture

    class Landing:
        url = "https://ads.example/landing"

        async def title(self):
            return "landing"

        async def content(self):
            return "<html><body>landing</body></html>"

        async def screenshot(self, *, full_page):
            assert full_page is False
            return b"png"

        async def ui_snapshot(self):
            return {
                "schema_version": "ui-snapshot.v2",
                "elements": [{"tag": "main", "text": "landing"}],
            }

        async def har_entries(self):
            return [
                {
                    "method": "GET",
                    "resource_type": "document",
                    "url": "https://ads.example/landing",
                    "status": 200,
                    "body_size": 31,
                    "duration_ms": 1,
                }
            ]

    @asynccontextmanager
    async def isolated(**kwargs):
        assert kwargs["url"] == "https://ads.example/landing?campaign=1"
        assert kwargs.get('enable_javascript', False) is javascript
        yield Landing(), SimpleNamespace(denied_reasons=set())

    monkeypatch.setattr(ad_isolated_page, "isolated_ad_page", isolated)
    trace = []
    observation = await page_capture._run_isolated_ad_goto(
        page=SimpleNamespace(url="https://source.example/menu"),
        context=SimpleNamespace(browser=object()),
        request=PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url="https://ads.example/landing?campaign=1",
            ad_landing_url="https://ads.example/landing?campaign=1",
            web_ad_javascript=javascript,
            navigation_depth=0,
            capture_ad_landing_evidence=True,
        ),
        action_trace=trace,
        timeout_ms=100,
    )

    assert trace[0]["action"] == "navigate"
    assert trace[0]["navigation_mode"] == "isolated_context_goto"
    assert trace[0]['render_mode'] == ('javascript_render' if javascript else 'static_render')
    assert trace[0]["navigation"]["isolated_new_page_count"] == 1
    assert observation["dom"]["purpose"] == "ad_landing"
    assert observation["frame"]["role"] == "landing"
    assert observation["ui_snapshot"]["purpose"] == "ad_landing"
    assert observation["ui_snapshot"]["elements"] == [{"tag": "main", "text": "landing"}]
    assert observation["har"]["purpose"] == "ad_landing"
    assert observation["har"]["entries"][0]["resource_type"] == "document"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assessment", "expected_reason"),
    [
        (RuntimeError("model unavailable"), "WEB_AD_PAGE_TYPE_MODEL_UNAVAILABLE"),
        (
            SimpleNamespace(
                page_type="ad_page",
                confidence=0.69,
                needs_review_reason=None,
            ),
            "WEB_AD_PAGE_TYPE_MODEL_LOW_CONFIDENCE",
        ),
    ],
)
async def test_ad_page_type_model_fallback_is_explicit(assessment, expected_reason):
    class Assessor:
        async def assess_ad_page(self, **kwargs):
            del kwargs
            if isinstance(assessment, Exception):
                raise assessment
            return assessment

    decision = await capture_runtime_module._assess_direct_ad_page_type(
        assessor=Assessor(),
        artifact_refs=(
            ("landing_screenshot", "shot"),
            ("landing_dom", "dom"),
            ("landing_ui_snapshot", "ui"),
            ("landing_har", "har"),
            ("landing_trace", "trace"),
        ),
    )

    assert decision["decision_source"] == "direct_ad_lineage_fallback"
    assert decision["fallback_reason"] == expected_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,ad", "file:///ad"])
async def test_ad_adapter_rejects_unsafe_href_before_browser(url):
    module = _subject()

    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            yield SimpleNamespace(command_sequence=1, finalization_epoch=1)

    class Browser:
        calls = 0

        async def capture_with_timeout(self, request, **kwargs):
            self.calls += 1
            raise RuntimeError("不应执行")

    browser = Browser()
    adapter = capture_runtime_module.WebAdLandingCaptureAdapter(
        browser=browser,
        persistence=object(),
        session_command_gate=Gate(),
    )
    with pytest.raises(module.WebAdLandingCaptureError) as caught:
        await adapter.capture(
            SimpleNamespace(
                task_id="task",
                session_id="session",
                destination_url=url,
                source_url="https://source.example/",
                source_capture_id="capture",
            )
        )
    assert caught.value.reason_code == "WEB_AD_OUTBOUND_INVALID_URL"
    assert browser.calls == 0


@pytest.mark.parametrize("extra", [{"selector": "#ad"}, {"click_x_ratio": 0.5}])
def test_isolated_ad_goto_request_cannot_smuggle_click_targets(extra):
    from services.api.app.infrastructure.browser.page_capture import _validate_request

    with pytest.raises(BlockedPageCaptureActionError) as caught:
        _validate_request(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="https://ads.example/",
                ad_landing_url="https://ads.example/",
                navigation_depth=1,
                capture_ad_landing_evidence=True,
                **extra,
            )
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"


def test_direct_ad_open_records_type_without_content_classification():
    from services.api.app.infrastructure.browser.page_capture import _attach_ad_navigation_evidence

    trace = [{"action": "direct_ad_outbound", "navigation": {"new_page_count": 1}}]
    _attach_ad_navigation_evidence(
        action_trace=trace,
        before_url="https://source.example/",
        after_url="https://source.example/",
        source_target=None,
        frame_count=2,
        landing={"url": "https://ads.example/", "dom_status": "static_content_insufficient"},
    )
    assert trace[0]["ad_navigation_evidence"]["status"] == "observed"


def test_legacy_ad_click_is_read_only_and_never_replayed():
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    payload = policy_payload()
    payload["historical_ad_capture"] = {"action_trace": [{"action": "click", "selector": "#ad"}]}
    audit = LegacyWebRunAuditAdapter.load(payload)
    assert audit.execution_allowed is False
    assert audit.frozen_fields["historical_ad_capture"]["action_trace"][0]["action"] == "click"
    assert not hasattr(audit, "navigation_commands")
    assert not hasattr(audit, "generated_gaps")


def test_ad_candidate_requires_source_capture_lineage():
    assert (
        _subject().select_web_ad_candidate(
            registered_domain="source.example",
            capture_snapshots=(
                (
                    "",
                    {
                        "elements": [{"href": "https://ads.example/", "class_name": "banner-ad"}],
                    },
                ),
            ),
        )
        is None
    )


@pytest.mark.asyncio
async def test_ad_adapter_rejects_missing_source_capture_before_browser():
    module = _subject()

    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            yield object()

    adapter = capture_runtime_module.WebAdLandingCaptureAdapter(
        browser=object(),
        persistence=object(),
        session_command_gate=Gate(),
    )
    with pytest.raises(module.WebAdLandingCaptureError) as caught:
        await adapter.capture(
            SimpleNamespace(
                task_id="task",
                session_id="session",
                source_capture_id="",
                source_url="https://source.example/",
                destination_url="https://ads.example/",
            )
        )
    assert caught.value.reason_code == "WEB_AD_SOURCE_CAPTURE_REQUIRED"


def _subject():
    module_name = "services.api.app.application.web_ad_landing_evidence"
    assert importlib.util.find_spec(module_name) is not None, "广告证据调度模块尚未实现"
    return importlib.import_module(module_name)


@pytest.mark.asyncio
async def test_capture_adapter_rejects_consumed_phase_without_opening_browser():
    module = _subject()

    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            yield object()

    class Claims:
        async def claim(self, **kwargs):
            return None

    adapter = capture_runtime_module.WebAdLandingCaptureAdapter(
        browser=object(),
        persistence=object(),
        session_command_gate=Gate(),
        outbound_claims=Claims(),
    )
    with pytest.raises(module.WebAdLandingCaptureError) as caught:
        await adapter.capture(SimpleNamespace(task_id="task", session_id="web:run"))
    assert caught.value.reason_code == "WEB_AD_LANDING_ALREADY_ATTEMPTED"


@pytest.mark.asyncio
async def test_capture_adapter_consumes_failed_open_before_worker_rebuild():
    module = _subject()
    finished = []

    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            yield object()

    class Claims:
        consumed = False

        async def claim(self, **kwargs):
            if self.consumed:
                return None
            self.consumed = True
            return "phase-once"

        async def finish(self, **kwargs):
            finished.append(kwargs)

    class Browser:
        calls = 0

        async def capture_with_timeout(self, request, *, timeout_seconds):
            self.calls += 1
            raise RuntimeError("模拟导航失败")

    command = SimpleNamespace(
        task_id="task",
        session_id="web:run",
        destination_url="https://ad.example/",
        source_url="https://example/",
        source_capture_id="capture",
        agent_run_id="run",
    )
    browser, claims = Browser(), Claims()
    for expected in ("WEB_AD_PAGE_OPEN_FAILED", "WEB_AD_LANDING_ALREADY_ATTEMPTED"):
        adapter = capture_runtime_module.WebAdLandingCaptureAdapter(
            browser=browser,
            persistence=object(),
            session_command_gate=Gate(),
            outbound_claims=claims,
        )
        with pytest.raises(module.WebAdLandingCaptureError) as caught:
            await adapter.capture(command)
        assert caught.value.reason_code == expected
    assert browser.calls == 1
    assert finished == [
        {
            "phase_id": "phase-once",
            "status": "failed",
            "reason_code": "WEB_AD_PAGE_OPEN_FAILED",
            "artifact_ids": (),
            "detail": {"cause_type": "RuntimeError"},
        }
    ]


def test_select_ad_candidate_prefers_preserved_popup_over_final_banner() -> None:
    popup_snapshot = {
        "elements": [
            {
                "tag": "a",
                "href": "https://ads.example.net/popup",
                "text": "推广",
                "class_name": "adspop popup-ad",
                "visible": True,
            }
        ]
    }
    final_snapshot = {
        "elements": [
            {
                "tag": "a",
                "href": "https://banner.example.org/campaign",
                "text": "横幅推广",
                "class_name": "top-banner",
                "visible": True,
            }
        ]
    }

    selected = _subject().select_web_ad_candidate(
        registered_domain="heiliao.com",
        capture_snapshots=(
            ("capture-popup", popup_snapshot),
            ("capture-ready", final_snapshot),
        ),
    )

    assert selected is not None
    assert selected.url == "https://ads.example.net/popup"
    assert selected.source_kind == "popup"
    assert selected.source_capture_id == "capture-popup"


def test_select_ad_candidate_reads_current_ui_snapshot_advertisement_fields() -> None:
    """广告调度必须读取采集器当前输出的 token 数组与广告事实。"""

    snapshot = {
        "elements": [
            {
                "tag": "a",
                "href": "https://ads.example.net/campaign",
                "text": "",
                "class_tokens": ["tjtagmanager"],
                "parent_class_tokens": ["swiper-slide"],
                "ancestor_class_tokens": [
                    "swiper-wrapper",
                    "banner-swiper",
                    "xqbj-component-adfloat",
                ],
                "is_advertisement": True,
                "visible": True,
            }
        ]
    }

    selected = _subject().select_web_ad_candidate(
        registered_domain="chigua.com",
        capture_snapshots=(("capture-ready", snapshot),),
    )

    assert selected is not None
    assert selected.url == "https://ads.example.net/campaign"
    assert selected.source_kind == "banner"
    assert selected.source_capture_id == "capture-ready"


def test_select_ad_candidate_rejects_current_domain_and_unsafe_urls() -> None:
    snapshot = {
        "elements": [
            {"tag": "a", "href": "javascript:alert(1)", "class_name": "popup-ad"},
            {"tag": "a", "href": "data:text/html,ad", "class_name": "popup-ad"},
            {
                "tag": "a",
                "href": "https://cdn.heiliao.com/ad",
                "class_name": "popup-ad",
            },
            {
                "tag": "a",
                "href": "https://outside.example/ad",
                "class_name": "banner-ad",
                "text": "下载客户端",
            },
        ]
    }

    assert (
        _subject().select_web_ad_candidate(
            registered_domain="heiliao.com",
            capture_snapshots=(("capture-ready", snapshot),),
        )
        is None
    )


def _business_page(*, logical_page_id: str = "page-home", page_type: str | None = None):
    module = _subject()
    return module.WebAdBusinessPage(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="session-1",
        logical_page_id=logical_page_id,
        registered_domain="heiliao.com",
        page_role="home" if logical_page_id == "page-home" else "menu",
        page_type=page_type,
        source_url="https://heiliao.com/"
        if logical_page_id == "page-home"
        else "https://heiliao.com/menu",
        ready_capture_id="capture-ready",
        capture_snapshots=(
            (
                "capture-ready",
                {
                    "elements": [
                        {
                            "tag": "a",
                            "href": "https://ads.example.net/campaign",
                            "class_name": "banner-ad",
                            "visible": True,
                        }
                    ]
                },
            ),
        ),
    )


@pytest.mark.asyncio
async def test_scheduler_attempts_home_even_when_page_type_needs_review_and_is_idempotent() -> None:
    module = _subject()

    class CapturePort:
        def __init__(self) -> None:
            self.commands = []

        async def capture(self, command):
            self.commands.append(command)
            return module.WebAdLandingCaptureReceipt(
                capture_id="capture-ad-1",
                landing_url="https://ads.example.net/campaign",
                artifact_ids=("dom-source", "shot-source", "dom-landing", "shot-landing"),
                artifact_refs=(
                    ("source_dom", "dom-source"),
                    ("source_screenshot", "shot-source"),
                    ("landing_dom", "dom-landing"),
                    ("landing_screenshot", "shot-landing"),
                ),
                source_restored=True,
            )

    port = CapturePort()
    scheduler = module.WebAdLandingEvidenceScheduler(capture_port=port)
    page = _business_page(page_type="needs_review")

    first = await scheduler.attempt(page)
    second = await scheduler.attempt(replace(page, ready_capture_id="capture-new"))

    assert first.status == "observed"
    assert first.page_type == "ad_page"
    assert second == first
    assert len(port.commands) == 1
    assert port.commands[0].logical_page_id == "page-home"
    assert port.commands[0].destination_url == "https://ads.example.net/campaign"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reachable", "reason_code", "expected_status"),
    (
        (True, None, "observed"),
        (False, "WEB_AD_LANDING_EVIDENCE_INSUFFICIENT", "evidence_insufficient"),
    ),
)
async def test_legacy_audit_preserves_original_business_page_type_and_status(
    reachable, reason_code, expected_status
) -> None:
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    body = policy_payload()
    body["direct_ad_page_enabled"] = False
    body["ad_evidence"] = {
        "page_type": "content_list",
        "status": expected_status,
        "capture_id": "capture-legacy-ad",
        "landing_reachable": reachable,
        "reason_code": reason_code,
    }
    audit = LegacyWebRunAuditAdapter.load(body)
    assert audit.execution_allowed is False
    assert dict(audit.frozen_fields["ad_evidence"]) == body["ad_evidence"]
    assert audit.frozen_fields["ad_evidence"]["page_type"] == "content_list"


@pytest.mark.asyncio
async def test_scheduler_records_no_candidate_without_fake_success() -> None:
    module = _subject()

    class CapturePort:
        async def capture(self, command):  # pragma: no cover - 不应调用
            raise AssertionError(command)

    page = replace(_business_page(logical_page_id="page-menu"), capture_snapshots=())
    result = await module.WebAdLandingEvidenceScheduler(capture_port=CapturePort()).attempt(page)

    assert result.status == "no_candidate"
    assert result.fail_closed is True
    assert result.capture_id is None
    assert result.reason_code == "WEB_AD_CANDIDATE_NOT_FOUND"


@pytest.mark.asyncio
async def test_scheduler_selects_ad_only_from_same_ready_capture() -> None:
    module = _subject()

    class CapturePort:
        async def capture(self, command):  # pragma: no cover - 不应调用
            raise AssertionError(command)

    page = replace(
        _business_page(logical_page_id="page-menu"),
        ready_capture_id="capture-ready",
        capture_snapshots=(
            (
                "capture-earlier",
                {
                    "elements": [
                        {
                            "tag": "a",
                            "href": "https://ads.example.net/earlier",
                            "class_name": "popup-ad",
                            "visible": True,
                        }
                    ]
                },
            ),
            ("capture-ready", {"elements": []}),
        ),
    )

    result = await module.WebAdLandingEvidenceScheduler(capture_port=CapturePort()).attempt(page)

    assert result.status == "no_candidate"
    assert result.reason_code == "WEB_AD_CANDIDATE_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_role", "page_type"),
    (("age_gate", None), ("search", "age_gate"), ("detail", "ad_page")),
)
async def test_scheduler_excludes_age_gate_and_ad_page_recursion(
    page_role: str,
    page_type: str | None,
) -> None:
    module = _subject()

    class CapturePort:
        calls = 0

        async def capture(self, command):  # pragma: no cover - 不应调用
            self.calls += 1
            raise AssertionError(command)

    port = CapturePort()
    result = await module.WebAdLandingEvidenceScheduler(capture_port=port).attempt(
        replace(
            _business_page(logical_page_id=f"page-{page_role}"),
            page_role=page_role,
            page_type=page_type,
        )
    )

    assert result.status == "not_applicable"
    assert result.reason_code == "WEB_AD_BUSINESS_PAGE_NOT_APPLICABLE"
    assert port.calls == 0


@pytest.mark.asyncio
async def test_scheduler_retains_unreachable_landing_artifacts_without_fake_reachability() -> None:
    module = _subject()

    class CapturePort:
        async def capture(self, command):
            del command
            return module.WebAdLandingCaptureReceipt(
                capture_id="capture-ad-error",
                landing_url="https://dead-ad.example/path",
                artifact_ids=("dom-source", "shot-pair", "dom-landing"),
                artifact_refs=(
                    ("source_dom", "dom-source"),
                    ("source_screenshot", "shot-pair"),
                    ("landing_dom", "dom-landing"),
                    ("landing_screenshot", "shot-pair"),
                ),
                source_restored=True,
                landing_reachable=False,
                reason_code="WEB_AD_LANDING_UNREACHABLE",
            )

    result = await module.WebAdLandingEvidenceScheduler(capture_port=CapturePort()).attempt(
        _business_page()
    )

    assert result.status == "observed_unreachable"
    assert result.fail_closed is True
    assert result.reason_code == "WEB_AD_LANDING_UNREACHABLE"
    assert result.landing_url == "https://dead-ad.example/path"
    assert result.capture_id == "capture-ad-error"
    assert dict(result.artifact_refs)["landing_dom"] == "dom-landing"


@pytest.mark.asyncio
async def test_scheduler_closes_failed_capture_and_keeps_structured_return_failure() -> None:
    module = _subject()

    class CapturePort:
        async def capture(self, command):
            del command
            raise module.WebAdLandingCaptureError(
                reason_code="WEB_AD_LANDING_RETURN_FAILED",
                message="secret destination detail",
            )

    result = await module.WebAdLandingEvidenceScheduler(capture_port=CapturePort()).attempt(
        _business_page(logical_page_id="page-menu")
    )

    assert result.status == "return_failed"
    assert result.fail_closed is True
    assert result.capture_id is None
    assert result.reason_code == "WEB_AD_LANDING_RETURN_FAILED"
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_scheduler_claims_same_logical_page_once_even_for_concurrent_failed_attempts():
    import asyncio

    module = _subject()
    calls = []

    class CapturePort:
        async def capture(self, command):
            calls.append(command)
            await asyncio.sleep(0)
            raise module.WebAdLandingCaptureError(
                reason_code="WEB_AD_PAGE_OPEN_FAILED",
                message="failed",
            )

    scheduler = module.WebAdLandingEvidenceScheduler(capture_port=CapturePort())
    first, second = await asyncio.gather(
        scheduler.attempt(_business_page()),
        scheduler.attempt(_business_page()),
    )
    third = await scheduler.attempt(replace(_business_page(), ready_capture_id="revisited"))
    assert len(calls) == 1
    assert first == second == third
    assert first.status == "open_failed"


@pytest.mark.asyncio
async def test_scheduler_keeps_return_failure_artifacts_for_audit() -> None:
    module = _subject()

    class CapturePort:
        async def capture(self, command):
            del command
            return module.WebAdLandingCaptureReceipt(
                capture_id="capture-ad-return-failed",
                landing_url="https://ads.example.net/campaign",
                artifact_ids=("dom-source", "shot-pair", "dom-landing"),
                artifact_refs=(
                    ("source_dom", "dom-source"),
                    ("source_screenshot", "shot-pair"),
                    ("landing_dom", "dom-landing"),
                    ("landing_screenshot", "shot-pair"),
                ),
                source_restored=False,
            )

    result = await module.WebAdLandingEvidenceScheduler(capture_port=CapturePort()).attempt(
        _business_page(logical_page_id="page-menu")
    )

    assert result.status == "return_failed"
    assert result.fail_closed is True
    assert result.reason_code == "WEB_AD_LANDING_RETURN_FAILED"
    assert result.capture_id == "capture-ad-return-failed"
    assert result.landing_url == "https://ads.example.net/campaign"
    assert result.artifact_ids == ("dom-source", "shot-pair", "dom-landing")
    assert dict(result.artifact_refs)["landing_dom"] == "dom-landing"


@pytest.mark.asyncio
async def test_capture_adapter_persists_direct_new_page_artifacts_under_source_capture() -> None:
    module = _subject()
    phases = []
    model_calls = []
    detections = []

    class PageTypeAssessor:
        async def assess_ad_page(self, **kwargs):
            model_calls.append(kwargs)
            return SimpleNamespace(page_type="ad_page", confidence=0.96)

    class Claims:
        async def claim(self, **kwargs):
            phases.append("claimed")
            return "phase-ad"

        async def finish(self, **kwargs):
            phases.append(kwargs)

    class Browser:
        def __init__(self) -> None:
            self.request = None

        async def capture_with_timeout(self, request, *, timeout_seconds):
            assert phases == ["claimed"]
            assert timeout_seconds == 30.0
            self.request = request
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://heiliao.com/menu",
                title="menu",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact("dom", None, "<html>source</html>"),
                    PageCaptureArtifact("screenshot", None, b"source"),
                    PageCaptureArtifact(
                        "dom",
                        None,
                        {"purpose": "ad_landing", "html": "<html>landing</html>"},
                    ),
                    PageCaptureArtifact(
                        "screenshot",
                        None,
                        {"purpose": "ad_landing_consistency", "frames": [{}, {}]},
                    ),
                    PageCaptureArtifact(
                        "screenshot",
                        None,
                        {
                            "purpose": "ad_landing",
                            "encoding": "base64",
                            "data": "iVBORw0KGgo=",
                        },
                    ),
                    PageCaptureArtifact(
                        "ui_snapshot",
                        None,
                        {"purpose": "ad_landing", "elements": []},
                    ),
                    PageCaptureArtifact(
                        "har",
                        None,
                        {"purpose": "ad_landing", "entries": []},
                    ),
                    PageCaptureArtifact(
                        "action_trace",
                        None,
                        [{"action": "navigate", "navigation_mode": "isolated_context_goto"}],
                    ),
                ],
                action_trace=[
                    {
                        "action": "navigate",
                        "navigation_mode": "isolated_context_goto",
                        "navigation": {
                            "closed_after_observation_count": 1,
                            "new_page_count": 1,
                        },
                        "ad_navigation_evidence": {
                            "status": "observed",
                            "landing": {"url": "https://ads.example.net/campaign"},
                        },
                    }
                ],
            )

    class Persistence:
        def __init__(self) -> None:
            self.kwargs = None

        async def persist(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                page_capture_id="capture-ad-1",
                artifacts=[SimpleNamespace(artifact_id=f"artifact-{index}") for index in range(8)],
            )

        async def record_ad_page_detection(self, **kwargs):
            detections.append(kwargs)
            return True

    class Gate:
        def __init__(self) -> None:
            self.permit = None
            self.recorded_capture_ids = []

        @asynccontextmanager
        async def hold(self, **kwargs):
            assert kwargs["task_id"] == "task-1"
            permit = SimpleNamespace(
                command_sequence=9,
                finalization_epoch=2,
                current_capture_id=None,
            )

            def record_current_capture(capture_id):
                self.recorded_capture_ids.append(capture_id)
                permit.current_capture_id = capture_id

            permit.record_current_capture = record_current_capture
            self.permit = permit
            yield permit

    browser = Browser()
    persistence = Persistence()
    gate = Gate()
    assert hasattr(capture_runtime_module, "WebAdLandingCaptureAdapter")
    adapter = capture_runtime_module.WebAdLandingCaptureAdapter(
        browser=browser,
        persistence=persistence,
        session_command_gate=gate,
        timeout_seconds=30.0,
        outbound_claims=Claims(),
        page_type_assessor=PageTypeAssessor(),
    )

    receipt = await adapter.capture(
        module.WebAdDirectCaptureCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="session-1",
            logical_page_id="page-menu",
            source_capture_id="capture-ready",
            source_url="https://heiliao.com/menu",
            destination_url="https://ads.example.net/campaign?secret=1",
            candidate_capture_id="capture-popup",
            candidate_kind="popup",
            page_role="menu",
            page_type="needs_review",
        )
    )

    assert browser.request.mode is PageCaptureMode.GOTO
    assert browser.request.trigger_action is None
    assert browser.request.url == "https://ads.example.net/campaign?secret=1"
    assert browser.request.ad_landing_url == "https://ads.example.net/campaign?secret=1"
    assert browser.request.web_ad_javascript is True
    assert browser.request.timeout_ms == 10_000
    assert persistence.kwargs["task_id"] == "task-1"
    assert persistence.kwargs["agent_run_id"] == "run-1"
    assert persistence.kwargs["parent_page_capture_id"] == "capture-ready"
    assert persistence.kwargs["capture_role"] == "supplemental"
    assert persistence.kwargs["capture_purpose"] == "web_ad_page"
    assert persistence.kwargs["direct_page_type"] == "ad_page"
    assert persistence.kwargs["capture_url"] == "https://ads.example.net/campaign"
    assert persistence.kwargs["requested_url"] == ("https://ads.example.net/campaign?secret=1")
    assert persistence.kwargs["session_command_sequence"] == 9
    assert receipt.capture_id == "capture-ad-1"
    assert receipt.page_type == "ad_page"
    assert receipt.requested_url == "https://ads.example.net/campaign?secret=1"
    assert receipt.source_restored is True
    assert phases[1]["status"] == "completed"
    assert phases[1]["artifact_ids"] == tuple(f"artifact-{index}" for index in range(8))
    assert receipt.landing_url == "https://ads.example.net/campaign"
    assert len(receipt.artifact_ids) == 8
    assert dict(receipt.artifact_refs) == {
        "source_dom": "artifact-0",
        "source_screenshot": "artifact-3",
        "landing_dom": "artifact-2",
        "landing_screenshot": "artifact-4",
        "landing_ui_snapshot": "artifact-5",
        "landing_har": "artifact-6",
        "landing_trace": "artifact-7",
    }
    assert model_calls == [
        {
            "screenshot_artifact_id": "artifact-4",
            "dom_artifact_id": "artifact-2",
            "ui_snapshot_artifact_id": "artifact-5",
            "har_artifact_id": "artifact-6",
            "action_trace_artifact_id": "artifact-7",
        }
    ]
    assert detections[0]["decision_source"] == "model"
    assert detections[0]["fallback_reason"] is None
    assert detections[0]["model_page_type"] == "ad_page"
    assert detections[0]["model_confidence"] == 0.96
    assert set(dict(receipt.artifact_refs).values()) <= set(receipt.artifact_ids)
    assert gate.recorded_capture_ids == []


@pytest.mark.asyncio
async def test_capture_adapter_persists_artifacts_before_reporting_return_failure() -> None:
    module = _subject()

    class Browser:
        async def capture_with_timeout(self, request, *, timeout_seconds):
            del timeout_seconds
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://unexpected.example/",
                title="unexpected",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact("dom", None, "<html>source</html>"),
                    PageCaptureArtifact("screenshot", None, b"source"),
                    PageCaptureArtifact(
                        "dom",
                        None,
                        {"purpose": "ad_landing", "html": "<html>landing</html>"},
                    ),
                    PageCaptureArtifact(
                        "screenshot",
                        None,
                        {"purpose": "ad_landing_consistency", "frames": [{}, {}]},
                    ),
                    PageCaptureArtifact(
                        "screenshot",
                        None,
                        {"purpose": "ad_landing", "encoding": "base64", "data": ""},
                    ),
                    PageCaptureArtifact(
                        "ui_snapshot", None, {"purpose": "ad_landing", "elements": []}
                    ),
                    PageCaptureArtifact("har", None, {"purpose": "ad_landing", "entries": []}),
                    PageCaptureArtifact(
                        "action_trace",
                        None,
                        [{"action": "navigate", "navigation_mode": "isolated_context_goto"}],
                    ),
                ],
                action_trace=[
                    {
                        "action": "navigate",
                        "navigation_mode": "isolated_context_goto",
                        "navigation": {
                            "closed_after_observation_count": 1,
                            "new_page_count": 1,
                        },
                        "ad_navigation_evidence": {
                            "status": "observed",
                            "landing": {"url": "https://ads.example.net/campaign"},
                        },
                    }
                ],
            )

    class Persistence:
        def __init__(self) -> None:
            self.calls = 0

        async def persist(self, **kwargs):
            del kwargs
            self.calls += 1
            return SimpleNamespace(
                page_capture_id="capture-ad-return-failed",
                artifacts=[SimpleNamespace(artifact_id=f"artifact-{index}") for index in range(8)],
            )

    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            del kwargs
            yield SimpleNamespace(
                command_sequence=1,
                finalization_epoch=1,
                record_current_capture=lambda capture_id: None,
            )

    persistence = Persistence()
    receipt = await capture_runtime_module.WebAdLandingCaptureAdapter(
        browser=Browser(),
        persistence=persistence,
        session_command_gate=Gate(),
        timeout_seconds=30.0,
    ).capture(
        module.WebAdDirectCaptureCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="session-1",
            logical_page_id="page-menu",
            source_capture_id="capture-ready",
            source_url="https://heiliao.com/menu",
            destination_url="https://ads.example.net/campaign",
            candidate_capture_id="capture-ready",
            candidate_kind="banner",
            page_role="menu",
            page_type="content_list",
        )
    )

    assert persistence.calls == 1
    assert receipt.source_restored is False
    assert receipt.page_type == "ad_page"
    assert receipt.capture_id == "capture-ad-return-failed"
    assert receipt.landing_url == "https://ads.example.net/campaign"
    assert len(receipt.artifact_ids) == 8


def test_direct_ad_capture_outcome_accepts_complete_unreachable_error_page_pair() -> None:
    result = PageCaptureResult(
        execution_status="success",
        mode="single_trigger",
        reachable=True,
        final_url="https://hl365.com/?private=1",
        title="home",
        status_code=None,
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "closed_after_observation_count": 1,
                    "new_page_count": 1,
                    "landing_reachable": False,
                },
                "ad_navigation_evidence": {
                    "status": "observed_unreachable",
                    "landing": {
                        "url": "https://dead-ad.example/path",
                        "reachable": False,
                        "reason_code": "WEB_AD_LANDING_UNREACHABLE",
                    },
                },
            }
        ],
    )

    assert capture_runtime_module._direct_ad_capture_outcome(
        result=result,
        source_url="https://hl365.com/?private=1",
    ) == (
        True,
        True,
        "https://dead-ad.example/path",
        False,
        "WEB_AD_LANDING_UNREACHABLE",
    )


def test_direct_ad_capture_keeps_isolated_landing_when_source_opens_popup() -> None:
    result = PageCaptureResult(
        execution_status="success",
        mode="single_trigger",
        reachable=True,
        final_url="https://source.example/menu",
        title="source",
        status_code=200,
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "new_page_count": 2,
                    "closed_after_observation_count": 2,
                    "isolated_new_page_count": 1,
                    "isolated_closed_after_observation_count": 1,
                    "source_context_new_page_count": 1,
                    "source_context_contaminated": True,
                },
                "ad_navigation_evidence": {
                    "status": "observed",
                    "landing": {"url": "https://ads.example/campaign"},
                },
            }
        ],
    )

    assert capture_runtime_module._direct_ad_capture_outcome(
        result=result,
        source_url="https://source.example/menu",
    ) == (True, False, "https://ads.example/campaign", True, None)


def test_runtime_does_not_upgrade_legacy_insufficient_ad_observation() -> None:
    result = PageCaptureResult(
        execution_status="success",
        mode="single_trigger",
        reachable=True,
        final_url="https://heiliao.com/menu",
        title="menu",
        status_code=200,
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "closed_after_observation_count": 1,
                    "new_page_count": 1,
                },
                "ad_navigation_evidence": {
                    "status": "evidence_insufficient",
                    "landing": {"url": "https://ads.example.net/campaign"},
                },
            }
        ],
    )

    assert capture_runtime_module._direct_ad_capture_outcome(
        result=result,
        source_url="https://heiliao.com/menu",
    ) == (False, True, "https://ads.example.net/campaign", True, None)


@pytest.mark.asyncio
async def test_web_dispatcher_rejects_implicit_legacy_ad_click():
    from services.api.app.infrastructure.browser.page_capture import _run_single_trigger

    with pytest.raises(BlockedPageCaptureActionError) as caught:
        await _run_single_trigger(
            page=object(),
            context=object(),
            action_trace=[],
            timeout_ms=100,
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector="#ad",
                web_capture_stage="web_page_probe",
                _force_legacy_ad_landing=True,
            ),
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"


@pytest.mark.asyncio
async def test_page_capture_impl_rejects_before_page_setup():
    from services.api.app.infrastructure.browser.page_capture import PlaywrightPageCaptureBrowser

    with pytest.raises(BlockedPageCaptureActionError) as caught:
        await PlaywrightPageCaptureBrowser()._capture_on_page_impl(
            page=object(),
            context=object(),
            console_logs=[],
            network_requests=[],
            timeout_error_type=TimeoutError,
            request=PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="https://ads.example/",
                capture_ad_landing_evidence=True,
            ),
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [PageCaptureMode.GOTO, PageCaptureMode.OBSERVE])
async def test_private_ad_capture_rejects_direct_action_in_non_trigger_mode(monkeypatch, mode):
    from services.api.app.infrastructure.browser import page_capture

    calls = []

    class Page:
        url = "https://source.example/"

        def set_default_timeout(self, timeout):
            calls.append("page_setup")

    async def unexpected_navigation(**kwargs):
        calls.append("goto")
        raise BlockedPageCaptureActionError("未提前阻止导航")

    async def unexpected_settle(**kwargs):
        calls.append("settle")
        raise BlockedPageCaptureActionError("未提前阻止页面等待")

    monkeypatch.setattr(page_capture, "_goto_with_retry", unexpected_navigation)
    monkeypatch.setattr(page_capture, "_wait_for_page_settle", unexpected_settle)
    with pytest.raises(BlockedPageCaptureActionError) as caught:
        await page_capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
            page=Page(),
            context=object(),
            console_logs=[],
            network_requests=[],
            timeout_error_type=TimeoutError,
            request=PageCaptureRequest(
                mode=mode,
                url="https://source.example/",
                trigger_action=PageTriggerAction.DIRECT_AD_OUTBOUND,
                ad_landing_url="https://ads.example/",
                capture_ad_landing_evidence=True,
            ),
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"
    assert calls == []


@pytest.mark.asyncio
async def test_discovered_external_web_click_is_rejected_before_source_frame(monkeypatch):
    from services.api.app.infrastructure.browser import page_capture

    calls = {"href_probe": 0, "frame": 0, "dispatcher": 0, "click": 0}

    class Locator:
        @property
        def first(self):
            return self

        async def evaluate(self, script, **kwargs):
            calls["href_probe"] += 1
            return "https://ads.example/landing"

    class Page:
        url = "https://source.example/"

        def set_default_timeout(self, timeout):
            pass

        def locator(self, selector):
            assert selector == "#ad"
            return Locator()

        async def click(self, *args, **kwargs):
            calls["click"] += 1

    async def snapshot(*args, **kwargs):
        return {"elements": []}

    async def frame(**kwargs):
        calls["frame"] += 1
        return None

    actual_dispatcher = page_capture._run_single_trigger

    async def dispatcher(**kwargs):
        calls["dispatcher"] += 1
        return await actual_dispatcher(**kwargs)

    monkeypatch.setattr(page_capture, "_collect_ui_snapshot", snapshot)
    monkeypatch.setattr(page_capture, "_capture_ad_navigation_frame", frame)
    monkeypatch.setattr(page_capture, "_run_single_trigger", dispatcher)
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=PageTriggerAction.CLICK,
        selector="#ad",
        web_capture_stage="web_page_probe",
    )
    assert request.capture_ad_landing_evidence is False
    assert request._force_legacy_ad_landing is False
    with pytest.raises(BlockedPageCaptureActionError) as caught:
        await page_capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
            page=Page(),
            context=object(),
            console_logs=[],
            network_requests=[],
            timeout_error_type=TimeoutError,
            request=request,
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"
    assert calls == {"href_probe": 1, "frame": 0, "dispatcher": 0, "click": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("page_role", ["home", "menu"])
async def test_ready_business_page_bridge_schedules_home_and_menu_from_full_capture_sequence(
    page_role: str,
) -> None:
    module = _subject()

    class SnapshotReader:
        def __init__(self) -> None:
            self.reads = []

        async def read_snapshot(self, *, capture_id):
            self.reads.append(capture_id)
            return {
                "elements": [
                    {
                        "href": "https://ads.example.net/campaign",
                        "class_name": (
                            "popup-ad" if capture_id == "capture-before-popup" else "banner-ad"
                        ),
                    }
                ]
            }

    class Scheduler:
        def __init__(self) -> None:
            self.pages = []

        async def attempt(self, page):
            self.pages.append(page)
            return SimpleNamespace(status="observed")

    reader = SnapshotReader()
    scheduler = Scheduler()
    result = await module.schedule_ready_web_business_page_ad_evidence(
        scheduler=scheduler,
        snapshot_reader=reader,
        identity_policy=SimpleNamespace(logical_id=lambda url: f"logical:{url}"),
        registered_domain="heiliao.com",
        task_id="task-1",
        agent_run_id="run-1",
        session_id="session-1",
        page_role=page_role,
        page_type=None,
        ready_capture=SimpleNamespace(
            capture_id="capture-ready",
            url="https://heiliao.com/" if page_role == "home" else "https://heiliao.com/menu",
        ),
        evidence_capture_ids=("capture-before-popup", "capture-ready"),
    )

    assert result.status == "observed"
    assert reader.reads == ["capture-before-popup", "capture-ready"]
    assert len(scheduler.pages) == 1
    assert scheduler.pages[0].page_role == page_role
    assert scheduler.pages[0].page_type is None
    assert scheduler.pages[0].logical_page_id.startswith("logical:https://heiliao.com")
