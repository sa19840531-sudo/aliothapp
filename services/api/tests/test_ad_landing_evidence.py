"""广告源页与落地页一致性证据契约测试。

这些用例刻意覆盖「页面探索负责留证、规则工具负责判定」之间的窄接口：

* 页面采集请求只能从安全 href 隔离直开动作开启广告证据窗口；
* Observation Packet 只投影有界的 ``ad_navigation_evidence``；
* Hybrid 工具必须同时看到受控导航事实和成对的源页／落地页截图引用；
* H5 Frontier 候选的广告语义由 Runtime 派生为固定动作参数，模型不能自行扩展。

首批契约尚未全部接入生产实现，因此当前分支允许这些测试先以失败形式
暴露缺口；测试本身不使用 Playwright、真实网络或外部广告点击。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from services.api.app.application.h5_runtime_composition import (
    _h5_candidate_parameters,
)
from services.api.app.application.observation_packet import (
    ObservationArtifact,
    _compact_action_log,
    _decode_artifacts,
)
from services.api.app.application.rule_capability_hybrid import (
    run_hybrid_capability_tools,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser import page_capture as page_capture_module
from services.api.app.infrastructure.browser.page_capture import (
    BlockedPageCaptureActionError,
    PageCaptureMode,
    PageCaptureRequest,
    PageTriggerAction,
    _attach_ad_navigation_evidence,
    _block_undeclared_click_navigation,
    _capture_ad_landing_observation,
    _click_target_external_anchor_href,
    _close_unclaimed_new_pages,
    _is_offsite_navigation,
    _record_navigation_outcome,
    _run_isolated_ad_goto,
    _validate_request,
)


def _ad_navigation_evidence(
    *,
    status: str = "observed",
    source_screenshot_ref: str | None = "ad_landing_consistency#frame-0",
    landing_screenshot_ref: str | None = "ad_landing_consistency#frame-1",
) -> dict[str, Any]:
    """构造最小广告导航证据；其余页面内容不是本规则输入。"""

    return {
        "schema_version": "ad-navigation-evidence.v1",
        "status": status,
        "navigation_mode": "new_page",
        "source": {
            "url": "https://publisher.example.test/article?token=secret#source",
            "screenshot_ref": source_screenshot_ref,
            "target": {"selector": "#ad", "text": "外链广告"},
        },
        "landing": {
            "url": "https://advertiser.example.test/campaign?session=secret#landing",
            "title": "Campaign",
            "semantic_anchors": ["广告活动", "立即了解"],
            "screenshot_ref": landing_screenshot_ref,
        },
        "pair_complete": bool(source_screenshot_ref and landing_screenshot_ref),
    }


def _packet_payload(
    *,
    evidence: dict[str, Any] | None = None,
    screenshot_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action": "click",
        "selector": "#ad",
        "result": "success",
    }
    if evidence is not None:
        action["ad_navigation_evidence"] = evidence
    return {
        "facts": {
            "action_log": [action],
            "screenshot_evidence": screenshot_evidence or [],
        }
    }


def _screenshot_pair(
    *,
    source_ref: str | None = "shot-source#frame-0",
    landing_ref: str | None = "shot-landing#frame-1",
    purpose: str = "ad_landing_consistency",
) -> list[dict[str, Any]]:
    frames = []
    if source_ref is not None:
        frames.append(
            {
                "index": 0,
                "role": "source",
                "artifact_ref": "ad-consistency-sequence-1#frame-0",
                "logical_ref": "ad_landing_consistency#frame-0",
                "payload_valid": True,
            }
        )
    if landing_ref is not None:
        frames.append(
            {
                "index": 1,
                "role": "landing",
                "artifact_ref": "ad-consistency-sequence-1#frame-1",
                "logical_ref": "ad_landing_consistency#frame-1",
                "payload_valid": True,
            }
        )
    return [
        {
            "artifact_id": "ad-consistency-sequence-1",
            "purpose": purpose,
            "frame_count": 2,
            "frames": frames,
        }
    ]


def _h5_candidate(*, semantic_target: str, purpose: str, target_keywords: tuple[str, ...]):
    return SimpleNamespace(
        candidate_id="candidate-ad-1",
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        navigation_depth=1,
        metadata={
            "purpose": purpose,
            "target_keywords": list(target_keywords),
        },
    )


def _h5_work() -> SimpleNamespace:
    return SimpleNamespace(frontier_snapshot_id="h5-frontier-snapshot-1")


def test_ad_landing_capture_rejects_explicit_click_even_with_depth_and_screenshot() -> None:
    """广告证据窗口不得从 goto/observe 或无边界动作中隐式开启。"""

    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=PageTriggerAction.CLICK,
        selector="#ad",
        navigation_depth=1,
        capture_screenshot=True,
        capture_ad_landing_evidence=True,
    )
    with pytest.raises(BlockedPageCaptureActionError) as caught:
        _validate_request(request)
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"

    with pytest.raises((BlockedPageCaptureActionError, ValueError)):
        _validate_request(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="https://publisher.example.test/article",
                navigation_depth=1,
                capture_ad_landing_evidence=True,
            )
        )
    with pytest.raises((BlockedPageCaptureActionError, ValueError)):
        _validate_request(
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.BACK,
                navigation_depth=1,
                capture_ad_landing_evidence=True,
            )
        )
    with pytest.raises((BlockedPageCaptureActionError, ValueError)):
        _validate_request(
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.CLICK,
                selector="#ad",
                capture_screenshot=True,
                capture_ad_landing_evidence=True,
            )
        )
    with pytest.raises((BlockedPageCaptureActionError, ValueError)):
        _validate_request(
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.CLICK,
                selector="#ad",
                navigation_depth=1,
                capture_screenshot=False,
                capture_ad_landing_evidence=True,
            )
        )


@pytest.mark.asyncio
async def test_direct_ad_outbound_opens_locked_href_without_clicking_source_page(
    monkeypatch,
) -> None:
    """Web 业务页广告只允许读取 href 后在新页直开，不点击广告元素。"""

    class LandingPage:
        url = "about:blank"

        async def goto(self, url, *, wait_until, timeout):
            assert wait_until == "domcontentloaded"
            assert timeout == 3000
            self.url = url

    class Context:
        def __init__(self) -> None:
            self.landing = LandingPage()
            self.new_page_count = 0

        async def new_page(self):
            self.new_page_count += 1
            return self.landing

    class SourcePage:
        url = "https://heiliao.com/menu"

        def locator(self, selector):  # pragma: no cover - 调用即代表错误点击
            raise AssertionError(f"禁止点击广告元素：{selector}")

    context = Context()
    _stub_safe_isolation(monkeypatch, context)
    trace: list[dict[str, Any]] = []
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        url="https://ads.example.net/campaign?token=private#fragment",
        ad_landing_url="https://ads.example.net/campaign?token=private#fragment",
        navigation_depth=0,
        capture_screenshot=True,
        capture_ad_landing_evidence=True,
    )

    _validate_request(request)
    await _run_isolated_ad_goto(
        page=SourcePage(),
        context=context,
        request=request,
        action_trace=trace,
        timeout_ms=3000,
    )

    assert context.new_page_count == 1
    assert context.landing.url == "https://ads.example.net/campaign?token=private#fragment"
    assert len(trace) == 1
    assert trace[0]["destination_url"] == "https://ads.example.net/campaign"
    assert trace[0]["transport_mode"] == "isolated_safe_transport"
    assert trace[0]["navigation"]["closed_after_observation_count"] == 1


async def test_desktop_direct_ad_never_replays_outbound_in_popup_chain(monkeypatch) -> None:
    from services.api.app.infrastructure.browser import page_capture as module
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        _enforce_web_desktop_request,
    )

    class Landing:
        async def goto(self, url, **kwargs):
            self.url = url

    class Context:
        opened = 0

        async def new_page(self):
            self.opened += 1
            return Landing()

    async def popup(**kwargs):
        return {"dialog_present": True, "kind": "close_control"}

    async def remains(page):
        return True

    async def frame(**kwargs):
        return {}

    monkeypatch.setattr(module, "_wait_for_next_obstruction_target", popup)
    monkeypatch.setattr(module, "_visible_popup_remains", remains)
    monkeypatch.setattr(module, "_capture_obstruction_frame", frame)
    context, trace = Context(), []
    _stub_safe_isolation(monkeypatch, context)
    request = _enforce_web_desktop_request(
        PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url="https://ads.example.net/campaign",
            ad_landing_url="https://ads.example.net/campaign",
            navigation_depth=0,
            capture_ad_landing_evidence=True,
            capture_screenshot=True,
        )
    )
    await _run_isolated_ad_goto(
        page=SimpleNamespace(url="https://source.example/"),
        context=context,
        request=request,
        action_trace=trace,
        timeout_ms=1000,
    )
    if request.clear_obstructions:
        await module._clear_remaining_obstructions(
            page=object(),
            context=context,
            request=request,
            action_trace=trace,
            obstruction_frames=[],
            timeout_ms=1000,
            timeout_error_type=TimeoutError,
        )
    assert context.opened == 1
    assert len(trace) == 1 and trace[0]["action"] == "navigate"
    assert trace[0]["navigation_mode"] == "isolated_context_goto"


@pytest.mark.asyncio
async def test_direct_ad_outbound_dns_failure_is_closed_before_browser_open(monkeypatch) -> None:
    """DNS 未能证明公网时不得打开浏览器错误页或降级直连。"""
    from contextlib import asynccontextmanager

    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
    from services.api.app.infrastructure.browser import ad_isolated_page

    @asynccontextmanager
    async def denied(**kwargs):
        raise AdOutboundDenied("WEB_AD_OUTBOUND_DNS_FAILED")
        yield

    monkeypatch.setattr(ad_isolated_page, "isolated_ad_page", denied)

    class LandingPage:
        url = "about:blank"

        async def goto(self, url, *, wait_until, timeout):
            del url, wait_until, timeout
            self.url = "chrome-error://chromewebdata/"
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED at private-url")

        async def wait_for_timeout(self, timeout):
            assert timeout <= 250

    class Context:
        browser = object()

        def __init__(self) -> None:
            self.landing = LandingPage()

        async def new_page(self):
            return self.landing

    trace: list[dict[str, Any]] = []
    with pytest.raises(BlockedPageCaptureActionError) as caught:
        await _run_isolated_ad_goto(
            page=SimpleNamespace(url="https://hl365.com/"),
            context=Context(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="https://dead-ad.example/path?secret=1",
                ad_landing_url="https://dead-ad.example/path?secret=1",
                navigation_depth=0,
                capture_screenshot=True,
                capture_ad_landing_evidence=True,
            ),
            action_trace=trace,
            timeout_ms=3_000,
        )

    assert caught.value.error_code == "WEB_AD_OUTBOUND_DNS_FAILED"
    assert len(trace) == 1 and "result" not in trace[0]
    assert "private-url" not in str(trace)


def _stub_safe_isolation(monkeypatch, context):
    """旧动作轨迹夹具只替换隔离边界，真实隔离由 Chromium 用例覆盖。"""
    from contextlib import asynccontextmanager

    from services.api.app.infrastructure.browser import ad_isolated_page, page_capture

    context.browser = object()

    @asynccontextmanager
    async def isolated(**kwargs):
        landing = await context.new_page()
        await landing.goto(
            kwargs["url"],
            wait_until="domcontentloaded",
            timeout=kwargs["timeout_ms"],
        )
        yield landing, SimpleNamespace(denied_reasons=set())

    async def observation(**kwargs):
        return {"url": kwargs["page"].url, "dom_status": "readable"}

    async def chrome_user_agent(browser):
        del browser
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

    monkeypatch.setattr(ad_isolated_page, "isolated_ad_page", isolated)
    monkeypatch.setattr(page_capture, "_capture_ad_landing_observation", observation)
    monkeypatch.setattr(page_capture, "web_chrome_user_agent", chrome_user_agent)


@pytest.mark.asyncio
async def test_ad_landing_observation_keeps_bounded_dom_for_artifact_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LandingPage:
        url = "https://ads.example.net/campaign?session=secret#fragment"

        async def title(self):
            return "Campaign"

        async def content(self):
            return "<html><main>Campaign body</main></html>"

        async def screenshot(self, *, full_page):
            assert full_page is False
            return b"png"

    async def snapshot(page):
        del page
        return {"elements": [{"text": "Campaign body"}]}

    monkeypatch.setattr(page_capture_module, "_collect_ui_snapshot", snapshot)

    observed = await _capture_ad_landing_observation(page=LandingPage(), index=1)

    assert observed["url"] == "https://ads.example.net/campaign"
    assert observed["dom"] == {
        "purpose": "ad_landing",
        "url": "https://ads.example.net/campaign",
        "html": "<html><main>Campaign body</main></html>",
    }
    assert observed["dom_ref"] == "ad_landing_dom"


def test_observation_packet_compacts_bounded_ad_navigation_evidence() -> None:
    action = {
        "action": "click",
        "selector": "#ad",
        "ad_navigation_evidence": _ad_navigation_evidence(),
        "untrusted_raw_page_dump": "x" * 100_000,
    }

    compacted = _compact_action_log((action,))

    assert len(compacted) == 1
    evidence = compacted[0]["ad_navigation_evidence"]
    assert evidence["schema_version"] == "ad-navigation-evidence.v1"
    assert evidence["status"] == "observed"
    assert evidence["navigation_mode"] == "new_page"
    assert evidence["source"]["screenshot_ref"] == "ad_landing_consistency#frame-0"
    assert evidence["landing"]["screenshot_ref"] == "ad_landing_consistency#frame-1"
    assert evidence["source"]["url"] == "https://publisher.example.test/article"
    assert evidence["landing"]["url"] == "https://advertiser.example.test/campaign"
    assert "secret" not in str(evidence)
    assert evidence["pair_complete"] is True
    assert "untrusted_raw_page_dump" not in compacted[0]


def test_observation_packet_exposes_ad_frame_roles_without_image_payload() -> None:
    payload = json.dumps(
        {
            "sequence": True,
            "purpose": "ad_landing_consistency",
            "frames": [
                {
                    "index": 0,
                    "role": "source",
                    "encoding": "base64",
                    "data": "iVBORw0KGgo=",
                },
                {
                    "index": 1,
                    "role": "landing",
                    "encoding": "base64",
                    "data": "iVBORw0KGgo=",
                },
            ],
        }
    ).encode()

    decoded = _decode_artifacts(
        {
            "screenshot": [
                ObservationArtifact(
                    artifact_id="artifact-ad-pair",
                    artifact_type="screenshot",
                    content_type="application/json",
                    payload=payload,
                )
            ]
        }
    )

    summary = decoded["screenshot_evidence"][0]
    assert summary == {
        "artifact_id": "artifact-ad-pair",
        "purpose": "ad_landing_consistency",
        "frame_count": 2,
        "frames": [
            {
                "index": 0,
                "role": "source",
                "artifact_ref": "artifact-ad-pair#frame-0",
                "logical_ref": "ad_landing_consistency#frame-0",
                "payload_valid": True,
            },
            {
                "index": 1,
                "role": "landing",
                "artifact_ref": "artifact-ad-pair#frame-1",
                "logical_ref": "ad_landing_consistency#frame-1",
                "payload_valid": True,
            },
        ],
        "truncated": False,
    }
    assert "secret" not in str(summary)


def test_invalid_ad_screenshot_payload_cannot_form_real_pair() -> None:
    decoded = _decode_artifacts(
        {
            "screenshot": [
                ObservationArtifact(
                    artifact_id="artifact-broken-ad-pair",
                    artifact_type="screenshot",
                    content_type="application/json",
                    payload=json.dumps(
                        {
                            "sequence": True,
                            "purpose": "ad_landing_consistency",
                            "frames": [
                                {
                                    "index": 0,
                                    "role": "source",
                                    "encoding": "base64",
                                    "data": "broken",
                                },
                                {
                                    "index": 1,
                                    "role": "landing",
                                    "encoding": "base64",
                                    "data": "broken",
                                },
                            ],
                        }
                    ).encode(),
                )
            ]
        }
    )
    packet = _packet_payload(
        evidence=_ad_navigation_evidence(),
        screenshot_evidence=decoded["screenshot_evidence"],
    )

    results = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "ad-landing",
            "deterministic_tools": [
                "safe_click_observer_tool",
                "landing_page_probe_tool",
                "visual_semantic_compare_tool",
            ],
        },
    )

    assert {item["status"] for item in results} == {"insufficient"}


@pytest.mark.parametrize("group", ["generic-pair", "cover-content"])
def test_ad_contract_does_not_change_generic_visual_pair_semantics(group) -> None:
    packet = {
        "facts": {
            "action_log": [
                {
                    "action": "click",
                    "action_execution_id": "action-generic-1",
                    "source_capture_id": "capture-generic-before",
                    "result_capture_id": "capture-generic-after",
                    "target_before": {"selector": "#cover"},
                    "target_after": {"selector": "#cover"},
                    "before_url": "https://example.test/home",
                    "after_url": "https://example.test/detail",
                    "source_screenshot_id": "source-shot",
                    "target_screenshot_id": "target-shot",
                }
            ]
        }
    }

    results = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": group,
            "deterministic_tools": [
                "safe_click_observer_tool",
                "visual_semantic_compare_tool",
            ],
        },
    )

    assert {item["tool_name"]: item["status"] for item in results} == {
        "safe_click_observer_tool": "ok",
        "visual_semantic_compare_tool": "ok" if group == "generic-pair" else "insufficient",
    }


@pytest.mark.parametrize("same_content", [True, False])
def test_cover_pair_requires_same_content_not_only_two_screenshot_ids(same_content):
    results = run_hybrid_capability_tools(
        packet_payload={"facts": {"cover_content_materials": {
            "cover": {"capture_id": "cover", "content_key": "item-1", "material_kind": "cover"},
            "content": {"capture_id": "body", "content_key": "item-1" if same_content else "item-2",
                        "page_type": "video_detail", "material_kind": "playback"},
        }}},
        capability_config={"batch_group": "cover-content",
                           "deterministic_tools": ["visual_semantic_compare_tool"]},
    )
    assert results[0]["status"] == ("ok" if same_content else "insufficient")


def test_app_external_browser_ad_pair_satisfies_hybrid_prerequisites() -> None:
    evidence = {
        "schema_version": "ad-navigation-evidence.v1",
        "status": "observed",
        "navigation_mode": "external_browser",
        "pair_complete": True,
        "source": {
            "capture_id": "capture-ad-source",
            "artifact_id": "artifact-ad-source",
            "package_name": "com.example.target",
            "page_type": "startup_ad",
            "screenshot_ref": "ad_landing_consistency#frame-0",
        },
        "landing": {
            "capture_id": "capture-ad-landing",
            "artifact_id": "artifact-ad-landing",
            "package_name": "com.android.chrome",
            "screenshot_ref": "ad_landing_consistency#frame-1",
        },
    }
    packet = _packet_payload(
        evidence=evidence,
        screenshot_evidence=[
            {
                "artifact_id": "action-ad-1",
                "purpose": "ad_landing_consistency",
                "frame_count": 2,
                "frames": [
                    {
                        "index": 0,
                        "role": "source",
                        "artifact_ref": "artifact-ad-source",
                        "logical_ref": "ad_landing_consistency#frame-0",
                        "payload_valid": True,
                    },
                    {
                        "index": 1,
                        "role": "landing",
                        "artifact_ref": "artifact-ad-landing",
                        "logical_ref": "ad_landing_consistency#frame-1",
                        "payload_valid": True,
                    },
                ],
            }
        ],
    )

    results = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "ad-landing",
            "deterministic_tools": [
                "ad_region_detector_tool",
                "safe_click_observer_tool",
                "landing_page_probe_tool",
                "visual_semantic_compare_tool",
            ],
        },
    )

    assert {item["tool_name"]: item["status"] for item in results} == {
        "ad_region_detector_tool": "ok",
        "safe_click_observer_tool": "ok",
        "landing_page_probe_tool": "ok",
        "visual_semantic_compare_tool": "ok",
    }


async def test_new_tab_is_captured_before_runtime_closes_it() -> None:
    class MainPage:
        url = "https://publisher.example.test/article?token=source#private"

    class LandingPage:
        url = "https://advertiser.example.test/campaign?token=landing#private"

        def __init__(self) -> None:
            self.waited = False
            self.closed = False
            self.screenshot_before_close = False

        async def wait_for_load_state(self, state: str, timeout: int) -> None:
            assert state == "domcontentloaded"
            assert timeout == 1_000
            self.waited = True

        async def title(self) -> str:
            return "Campaign"

        async def evaluate(self, script: str) -> dict[str, Any]:
            assert script
            return {
                "schema_version": "ui-snapshot.v2",
                "url": self.url,
                "elements": [{"selector": "h1", "tag": "h1", "text": "Campaign"}],
                "element_count": 1,
            }

        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            assert self.closed is False
            self.screenshot_before_close = True
            return b"landing-frame"

        async def close(self) -> None:
            assert self.screenshot_before_close is True
            self.closed = True

    main_page = MainPage()
    landing_page = LandingPage()
    action_trace = [{"action": "direct_ad_outbound", "selector": None}]
    context = SimpleNamespace(pages=[main_page, landing_page])

    landing = await _record_navigation_outcome(
        page=main_page,
        context=context,
        before_url=main_page.url,
        pages_before=[main_page],
        observed_new_pages=[landing_page],
        action_trace=action_trace,
        timeout_ms=30_000,
        capture_ad_landing_evidence=True,
    )

    assert landing is not None
    assert landing["url"] == "https://advertiser.example.test/campaign"
    assert landing["title"] == "Campaign"
    assert landing["semantic_anchors"] == ["Campaign"]
    assert landing["frame"]["role"] == "landing"
    assert landing_page.waited is True
    assert landing_page.closed is True
    assert action_trace[0]["navigation"]["closed_after_observation_count"] == 1
    assert action_trace[0]["navigation"]["before_url"] == ("https://publisher.example.test/article")
    assert action_trace[0]["navigation"]["after_url"] == ("https://publisher.example.test/article")
    assert action_trace[0]["navigation"]["new_page_urls"] == [
        "https://advertiser.example.test/campaign"
    ]
    _attach_ad_navigation_evidence(
        action_trace=action_trace,
        before_url=main_page.url,
        after_url=main_page.url,
        source_target=None,
        landing=landing,
        frame_count=2,
    )
    assert action_trace[0]["ad_navigation_evidence"]["source"]["url"] == (
        "https://publisher.example.test/article"
    )


@pytest.mark.asyncio
async def test_unreachable_direct_new_tab_retains_requested_url_dom_and_screenshot() -> None:
    class MainPage:
        url = "https://hl365.com/"

    class LandingPage:
        url = "chrome-error://chromewebdata/"

        async def wait_for_load_state(self, state: str, timeout: int) -> None:
            del state, timeout

        async def title(self) -> str:
            return ""

        async def evaluate(self, script: str) -> dict[str, Any]:
            del script
            return {"elements": []}

        async def content(self) -> str:
            return "<html><body></body></html>"

        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            return b"browser-error"

        async def close(self) -> None:
            return None

    main_page = MainPage()
    landing_page = LandingPage()
    action_trace = [
        {
            "action": "direct_ad_outbound",
            "selector": None,
            "destination_url": "https://dead-ad.example/path",
            "result": "opened_new_page_unreachable",
            "navigation_error_code": "name_not_resolved",
        }
    ]
    landing = await _record_navigation_outcome(
        page=main_page,
        context=SimpleNamespace(pages=[main_page, landing_page]),
        before_url=main_page.url,
        pages_before=[main_page],
        observed_new_pages=[landing_page],
        action_trace=action_trace,
        timeout_ms=3_000,
        capture_ad_landing_evidence=True,
    )

    assert landing is not None
    assert landing["url"] == "https://dead-ad.example/path"
    assert landing["reachable"] is False
    assert landing["reason_code"] == "WEB_AD_LANDING_UNREACHABLE"
    assert landing["dom"]["url"] == "https://dead-ad.example/path"
    assert landing["frame"]["data"] == "YnJvd3Nlci1lcnJvcg=="
    assert action_trace[0]["navigation"]["new_page_urls"] == ["https://dead-ad.example/path"]
    _attach_ad_navigation_evidence(
        action_trace=action_trace,
        before_url=main_page.url,
        after_url=main_page.url,
        source_target=None,
        landing=landing,
        frame_count=2,
    )
    assert action_trace[0]["ad_navigation_evidence"]["status"] == ("observed_unreachable")
    assert action_trace[0]["ad_navigation_evidence"]["pair_complete"] is True


def test_multiple_new_pages_are_ambiguous_instead_of_selecting_first_landing() -> None:
    action_trace = [
        {
            "action": "click",
            "navigation": {
                "new_page_count": 2,
                "new_page_urls": [
                    "https://tracker.example.test/redirect",
                    "https://advertiser.example.test/campaign",
                ],
            },
        }
    ]

    _attach_ad_navigation_evidence(
        action_trace=action_trace,
        before_url="https://publisher.example.test/article",
        after_url="https://publisher.example.test/article",
        source_target={"selector": "#ad"},
        landing={
            "url": "https://tracker.example.test/redirect",
            "title": "Redirect",
        },
        frame_count=2,
    )

    evidence = action_trace[0]["ad_navigation_evidence"]
    assert evidence["status"] == "evidence_insufficient"
    assert evidence["navigation_mode"] == "ambiguous_new_pages"
    assert evidence["pair_complete"] is False


async def test_action_failure_closes_only_new_pages() -> None:
    class Page:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    existing_page = Page()
    observed_new_page = Page()
    unobserved_new_page = Page()
    context = SimpleNamespace(pages=[existing_page, observed_new_page, unobserved_new_page])

    await _close_unclaimed_new_pages(
        context=context,
        pages_before=[existing_page],
        observed_new_pages=[observed_new_page],
    )

    assert existing_page.closed is False
    assert observed_new_page.closed is True
    assert unobserved_new_page.closed is True


@pytest.mark.parametrize(
    ("status", "source_ref", "landing_ref", "purpose", "expected"),
    [
        (
            "observed",
            "ad_landing_consistency#frame-0",
            "ad_landing_consistency#frame-1",
            "ad_landing_consistency",
            "ok",
        ),
        (
            "insufficient",
            "ad_landing_consistency#frame-0",
            "ad_landing_consistency#frame-1",
            "ad_landing_consistency",
            "insufficient",
        ),
        (
            "observed",
            None,
            "ad_landing_consistency#frame-1",
            "ad_landing_consistency",
            "insufficient",
        ),
        (
            "observed",
            "ad_landing_consistency#frame-0",
            None,
            "ad_landing_consistency",
            "insufficient",
        ),
        (
            "observed",
            "ad_landing_consistency#frame-0",
            "ad_landing_consistency#frame-1",
            "other_screenshot",
            "insufficient",
        ),
    ],
)
def test_ad_hybrid_tools_require_observed_navigation_and_real_screenshot_pair(
    status: str,
    source_ref: str | None,
    landing_ref: str | None,
    purpose: str,
    expected: str,
) -> None:
    packet = _packet_payload(
        evidence=_ad_navigation_evidence(
            status=status,
            source_screenshot_ref=source_ref,
            landing_screenshot_ref=landing_ref,
        ),
        screenshot_evidence=_screenshot_pair(
            source_ref=source_ref,
            landing_ref=landing_ref,
            purpose=purpose,
        ),
    )
    config = {
        "batch_group": "ad-landing",
        "deterministic_tools": [
            "safe_click_observer_tool",
            "landing_page_probe_tool",
            "visual_semantic_compare_tool",
        ],
    }

    results = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config=config,
    )

    assert {item["tool_name"]: item["status"] for item in results} == {
        "safe_click_observer_tool": expected,
        "landing_page_probe_tool": expected,
        "visual_semantic_compare_tool": expected,
    }


@pytest.mark.parametrize(
    ("semantic_target", "purpose", "target_keywords"),
    [
        ("ad_outbound", "点击广告", ("ad_outbound",)),
        ("promotion", "广告落地页取证", ("AD_OUTBOUND",)),
    ],
)
def test_h5_ad_candidate_does_not_derive_quality_capture_parameters(
    semantic_target: str,
    purpose: str,
    target_keywords: tuple[str, ...],
) -> None:
    candidate = _h5_candidate(
        semantic_target=semantic_target,
        purpose=purpose,
        target_keywords=target_keywords,
    )

    parameters = _h5_candidate_parameters(work=_h5_work(), candidate=candidate)

    assert "capture_ad_landing_evidence" not in parameters
    assert "ad_landing_capability" not in parameters


def test_h5_ordinary_click_does_not_derive_ad_landing_evidence_parameter() -> None:
    candidate = _h5_candidate(
        semantic_target="settings",
        purpose="点击设置按钮",
        target_keywords=("设置",),
    )

    parameters = _h5_candidate_parameters(work=_h5_work(), candidate=candidate)

    assert "capture_ad_landing_evidence" not in parameters


@pytest.mark.parametrize(
    ("purpose", "target_keywords"),
    [
        ("点击外链广告", ("外链广告",)),
        ("点击广告外链", ("external_navigation",)),
        ("广告落地页取证", ("广告落地",)),
    ],
)
def test_h5_natural_language_does_not_replace_ad_outbound_capability(
    purpose: str,
    target_keywords: tuple[str, ...],
) -> None:
    candidate = _h5_candidate(
        semantic_target="promotion",
        purpose=purpose,
        target_keywords=target_keywords,
    )

    parameters = _h5_candidate_parameters(work=_h5_work(), candidate=candidate)

    assert "capture_ad_landing_evidence" not in parameters


class _AnchorProbeLocator:
    def __init__(self, href: str | Exception) -> None:
        self._href = href

    @property
    def first(self) -> _AnchorProbeLocator:
        return self

    async def evaluate(self, script: str, timeout: int | None = None) -> str:
        assert "closest" in script
        if isinstance(self._href, Exception):
            raise self._href
        return self._href


class _AnchorProbePage:
    def __init__(self, url: str, href: str | Exception) -> None:
        self.url = url
        self._href = href

    def locator(self, selector: str) -> _AnchorProbeLocator:
        assert selector
        return _AnchorProbeLocator(self._href)


async def test_click_target_probe_returns_external_anchor_href() -> None:
    page = _AnchorProbePage(
        "https://publisher.example.test/article",
        "https://advertiser.example.test/campaign",
    )

    href = await _click_target_external_anchor_href(page=page, selector="#ad")

    assert href == "https://advertiser.example.test/campaign"


@pytest.mark.parametrize(
    ("page_url", "anchor_href"),
    [
        # 同站锚点不是外链，不得开启证据窗口
        ("https://publisher.example.test/a", "https://publisher.example.test/b"),
        # 非 http(s) 目标一律忽略
        ("https://publisher.example.test/a", "javascript:void(0)"),
        # 点击目标不在任何锚点内
        ("https://publisher.example.test/a", ""),
    ],
)
async def test_click_target_probe_ignores_non_external_targets(
    page_url: str, anchor_href: str
) -> None:
    page = _AnchorProbePage(page_url, anchor_href)

    assert await _click_target_external_anchor_href(page=page, selector="#ad") is None


async def test_click_target_probe_failure_falls_back_to_plain_click() -> None:
    page = _AnchorProbePage(
        "https://publisher.example.test/article",
        RuntimeError("probe unavailable"),
    )

    assert await _click_target_external_anchor_href(page=page, selector="#ad") is None
    assert await _click_target_external_anchor_href(page=page, selector=None) is None


def test_offsite_navigation_detection() -> None:
    assert _is_offsite_navigation(
        before_url="https://publisher.example.test/a",
        after_url="https://advertiser.example.test/b",
    )
    assert not _is_offsite_navigation(
        before_url="https://publisher.example.test/a",
        after_url="https://publisher.example.test/b",
    )
    assert not _is_offsite_navigation(
        before_url="about:blank",
        after_url="https://advertiser.example.test/b",
    )


class _RestorablePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.restored_to: str | None = None

    async def go_back(self, wait_until: str, timeout: int) -> None:
        del wait_until, timeout

    async def goto(self, url: str, wait_until: str, timeout: int) -> None:
        del wait_until, timeout
        self.url = url
        self.restored_to = url


async def test_same_tab_offsite_click_is_restored_inside_ad_landing_window() -> None:
    """证据窗口内同页外跳：先采证后回位，动作按受控观察成功返回。"""
    page = _RestorablePage("https://advertiser.example.test/campaign")
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=PageTriggerAction.CLICK,
        selector="#ad",
    )

    kind = await _block_undeclared_click_navigation(
        page=page,
        request=request,
        before_url="https://publisher.example.test/article",
        timeout_ms=1_000,
        ad_landing_window=True,
    )

    assert kind == "external_landing_observed"
    assert page.restored_to == "https://publisher.example.test/article"


async def test_same_tab_offsite_click_without_window_stays_blocked() -> None:
    page = _RestorablePage("https://advertiser.example.test/campaign")
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=PageTriggerAction.CLICK,
        selector="#ad",
    )

    with pytest.raises(BlockedPageCaptureActionError):
        await _block_undeclared_click_navigation(
            page=page,
            request=request,
            before_url="https://publisher.example.test/article",
            timeout_ms=1_000,
            ad_landing_window=False,
        )
