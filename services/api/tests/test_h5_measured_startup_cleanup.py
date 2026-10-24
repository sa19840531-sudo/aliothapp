"""DYS18 原始 DOM 清洗后的开屏清障合同，不连接真实服务。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.tools import page_detection_tools as detection
from services.api.app.application.h5_exploration_observation import (
    build_h5_frontier,
    h5_observation_allows_completion,
)
from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.page.catalog import load_default_page_type_catalog
from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.tests.test_page_detection_tools import (
    PNG_BYTES,
    FakeArtifactReader,
    FakeChatClient,
    FakeEvidenceLoader,
    _artifact,
    _bundle,
    _recognized_output,
)

DOM = (Path(__file__).parent / "fixtures/h5_dys18_startup_20261012.html").read_text()
SNAPSHOT = json.loads(
    (Path(__file__).parent / "fixtures/h5_dys18_startup_snapshot_20261012.json").read_text()
)


class FailedClient:
    async def get_response(self, *args, **kwargs):
        raise RuntimeError("HTTP 402 Payment Required")


async def test_measured_snapshot_resolves_candidate_to_runtime_capability():
    result = await _detect(DOM, FailedClient())
    frontier = build_h5_frontier(
        detection_result=result,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    )
    candidate = frontier.candidates[0]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=SimpleNamespace(
            read_ui_snapshot=AsyncMock(return_value=SNAPSHOT),
        )
    )
    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="offline",
            session_id="h5:offline",
            source_capture_id="capture",
            action_type=InteractionOperation.CLICK,
            semantic_target=candidate.semantic_target,
            parameters=candidate.metadata["action_parameters"],
        )
    )
    assert target.selector == page_capture.FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR


@pytest.mark.parametrize("mode", ["dismiss", "hidden", "duplicate", "ineffective"])
async def test_measured_dom_native_dismissal_uses_current_unique_control(mode):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(DOM)
            if mode == "dismiss":
                await page.evaluate("""() => {
                    document.querySelector('#flt-semantic-node-6').addEventListener('click', () => {
                        document.querySelector('#flt-semantic-node-1').remove();
                        document.body.dataset.nativeDismissed = 'true';
                    });
                }""")
            if mode == "hidden":
                await page.evaluate(
                    "document.querySelector('#flt-semantic-node-6').style.display = 'none'"
                )
            if mode == "duplicate":
                await page.evaluate("""() => {
                    const original = document.querySelector('#flt-semantic-node-6');
                    const duplicate = original.cloneNode(true);
                    duplicate.id = 'duplicate-skip';
                    original.parentElement.appendChild(duplicate);
                }""")
            resolved = await page_capture._resolve_flutter_fullscreen_startup_skip_target(page)
            assert resolved["match_count"] == {"hidden": 0, "duplicate": 2}.get(mode, 1)
            if mode in {"hidden", "duplicate"}:
                assert resolved["selector"] is None
                assert await page.get_attribute("body", "data-native-dismissed") is None
            else:
                assert resolved["selector"] == "#flt-semantic-node-6"
                await page.locator(resolved["selector"]).click()
                assert (await page.get_attribute("body", "data-native-dismissed")) == (
                    "true" if mode == "dismiss" else None
                )
                hidden = await page_capture._wait_for_flutter_fullscreen_startup_skip_to_hide(
                    page=page,
                    timeout_ms=200,
                )
                assert hidden is (mode == "dismiss")
        finally:
            await browser.close()


@pytest.mark.parametrize("model_state", ["home", "ambiguous", "402"])
async def test_measured_raw_startup_proof_survives_detection_and_frontier(model_state):
    clean = "".join(detection._sanitize_and_chunk_dom(DOM))
    assert detection._has_flutter_fullscreen_startup_ad_skip(DOM)
    assert not detection._has_flutter_fullscreen_startup_ad_skip(clean)
    output = _recognized_output()
    if model_state == "ambiguous":
        output = output.model_copy(update={"detection_status": "ambiguous", "page_type": None})
    result = await _detect(DOM, FailedClient() if model_state == "402" else FakeChatClient(output))
    assert result["page_type"] == "startup_ad"
    assert result["candidate_actions"][0]["obstruction_capability"] == "startup_ad_dismissal"
    if model_state == "ambiguous":
        assert result["detection_status"] == "ambiguous"
        assert result["rule_matching_allowed"] is False
    frontier = build_h5_frontier(
        detection_result=result,
        observed_at=datetime(2026, 10, 12, 14, 15, 53, tzinfo=UTC),
        navigation_depth=0,
    )
    dismissals = [
        c
        for c in frontier.candidates
        if c.metadata.get("action_parameters", {}).get("target_mode") == "startup_ad_dismissal"
    ]
    assert len(dismissals) == 1
    assert frontier.observation.metadata["startup_ad_dismissal_count"] == 0
    assert not h5_observation_allows_completion(frontier.observation)


@pytest.mark.parametrize(
    "old,new",
    [
        ("328, 45", "100, 400"),
        (">进入<", ">立即支付<"),
        ("width: 390px; height: 844px", "width: 180px; height: 300px"),
        (
            'id="flt-semantic-node-6" tabindex="0" flt-tappable=""',
            'id="flt-semantic-node-6" tabindex="0"',
        ),
    ],
)
async def test_nearby_or_unsigned_controls_never_gain_startup_authority(old, new):
    assert old in DOM
    changed = DOM.replace(old, new)
    assert not detection._has_flutter_fullscreen_startup_ad_skip(changed)
    result = await _detect(changed, FakeChatClient(_recognized_output()))
    assert all(
        a.get("obstruction_capability") != "startup_ad_dismissal"
        for a in result["candidate_actions"]
    )


async def _detect(dom, client):
    detection._PAGE_DETECTION_RESULT_CACHE.clear()
    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="offline-dys18",
        )
    ):
        return await detection.detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    _artifact("dom", "dom", content_type="text/html"),
                    _artifact("shot", "screenshot", content_type="image/png"),
                    page_url="https://dy.ft6g7b8pfiy.cc/#/splash",
                )
            ),
            artifact_reader=FakeArtifactReader({"dom": dom.encode(), "shot": PNG_BYTES}),
            chat_client=client,
            skill_text="# 页面识别测试",
            model_name="offline",
            catalog=load_default_page_type_catalog(),
        )
