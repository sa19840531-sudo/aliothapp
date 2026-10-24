"""正文加载故障不能封锁独立导航，也不能伪造正文质检证据。"""

import json
from datetime import UTC, datetime

import pytest

from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.tools.page_detection_tools import detect_page_for_agent
from services.api.app.application.h5_adaptive_navigation import (
    discover_h5_adaptive_navigation_patterns,
)
from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.page.catalog import load_default_page_type_catalog
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    _select_structural_navigation_item,
)
from services.api.tests.test_h5_adaptive_navigation import _snapshot
from services.api.tests.test_page_detection_tools import (
    PNG_BYTES,
    FakeArtifactReader,
    FakeChatClient,
    FakeEvidenceLoader,
    _artifact,
    _bundle,
    _recognized_output,
)


@pytest.mark.parametrize(
    "failure",
    [
        None, "shadow", "degraded", "census_missing", "census_incomplete", "modal",
        "covered", "truncated", "page_tab", "external",
    ],
)
async def test_loading_body_keeps_only_verified_independent_navigation(failure) -> None:
    snapshot = _snapshot(labels=("首页", "栏目甲", "栏目乙", "账号"))
    snapshot.update(
        h5_modal_census_selectors=[],
        h5_modal_census_truncated=False,
        logical_surface={
            "protocol_version": "logical_surface.v1",
            "shadow": failure == "shadow",
            "render_profile": {"kind": "dom_document", "status": "confirmed", "confidence": 0.9},
            "evidence_quality": "high",
            "identity_fingerprint": "sha256:" + "a" * 64,
            "state_fingerprint": "sha256:" + "b" * 64,
            "state_signals": {"loading": False},
        },
    )
    if failure == "degraded":
        snapshot["capture_degraded"] = True
    elif failure == "census_missing":
        snapshot.pop("h5_modal_census_truncated")
    elif failure == "census_incomplete":
        snapshot["h5_modal_census_truncated"] = True
    elif failure == "modal":
        snapshot["h5_modal_census_selectors"] = ["#dialog"]
    elif failure == "covered":
        for item in snapshot["elements"]:
            item["hit_test"]["center_hits_target"] = False
    elif failure == "truncated":
        snapshot["truncated"] = True
    elif failure == "page_tab":
        for item in snapshot["elements"]:
            item.update(fixed_or_sticky_ancestor=False, within_navigation=True)
            item["rect"]["y"] = 90
    elif failure == "external":
        for item in snapshot["elements"]:
            item["external"] = True
    dom = _artifact("dom-loading", "dom", content_type="text/html")
    shot = _artifact("shot-loading", "screenshot", content_type="image/png")
    ui = _artifact("ui-loading", "ui_snapshot", content_type="application/json")
    reader = FakeArtifactReader({
        "dom-loading": b"<html><body><main>Loading...</main></body></html>",
        "shot-loading": PNG_BYTES,
        "ui-loading": json.dumps(snapshot, ensure_ascii=False).encode(),
    })
    with bind_agent_run_context(AgentRunContext(
        task_id="11111111-1111-1111-1111-111111111111", agent_run_id="run-loading",
    )):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(
                dom, shot, ui, page_url="https://unknown.example.test/",
                capture_metadata={"pipeline_metrics": {"page_inspection_ready": True}},
            )),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home").model_copy(
                update={"page_state": "loading", "candidate_actions": []},
            )),
            skill_text="# page-detection", catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )
    assert result["page_state"] == "loading"
    assert result["rule_matching_allowed"] is False
    actions = result["candidate_actions"]
    if failure is not None:
        assert actions == []
        return
    assert len(actions) == 3
    assert all(action["target_keywords"][0] == "bottom_navigation" for action in actions)
    pattern = discover_h5_adaptive_navigation_patterns(snapshot)[0]
    assert all(action["interaction_pattern_signature"] == pattern.signature for action in actions)
    for count in (0, 1, 2):
        frontier = build_h5_frontier(
            detection_result=result,
            observed_at=datetime(2026, 10, 3, tzinfo=UTC),
            navigation_depth=0,
            logical_surface_effective=True,
            lineage_metadata={"logical_reobserve_count": count},
        )
        assert frontier.observation.page_state == "loading"
        assert frontier.observation.metadata["objective_satisfied"] is False
        if count < 2:
            assert len(frontier.candidates) == 1
            assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
            assert frontier.candidates[0].metadata["logical_reobserve_attempt"] == count + 1
        else:
            assert len(frontier.candidates) == 3
            assert frontier.observation.metadata["logical_reobserve_exhausted"] is True
            assert tuple(
                frontier.observation.metadata["global_bottom_navigation_indices"]
            ) == (2, 3, 4)
            for candidate in frontier.candidates:
                assert candidate.action_type is InteractionOperation.CLICK
                params = candidate.metadata["action_parameters"]
                assert _select_structural_navigation_item(
                    snapshot,
                    **params,
                    operation=InteractionOperation.CLICK,
                ) == snapshot["elements"][params["navigation_index"] - 1]["selector"]


async def test_real_browser_loading_body_can_reach_independent_navigation() -> None:
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            html = """
                <style>body {margin:0} nav {position:fixed;bottom:0;width:390px}
                  nav ul {display:flex;padding:0;list-style:none;justify-content:space-around}
                  a {display:block;width:55px;height:48px}</style>
                <main>Loading...</main><nav><ul>
                  <li><a href="/" aria-current="page">首页</a></li>
                  <li><a href="/section-a">甲区</a></li>
                  <li><a href="/section-b">乙区</a></li>
                  <li><a href="/section-c">丙区</a></li>
                </ul></nav>
            """
            await page.route("https://fixture.test/**", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body=(
                    html if route.request.url == "https://fixture.test/"
                    else "<main>栏目内容</main>"
                ),
            ))
            await page.goto("https://fixture.test/")
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            snapshot["logical_surface"] = build_h5_logical_surface(
                ui_snapshot=snapshot, page_url=page.url,
                surface_session_id="h5:fixture", document_token="fixture-document",
            )
            snapshot["logical_surface"]["shadow"] = False
            reader = FakeArtifactReader({
                "dom": (await page.content()).encode(),
                "shot": await page.screenshot(),
                "ui": json.dumps(snapshot, ensure_ascii=False).encode(),
            })
            bundle = _bundle(
                _artifact("dom", "dom", content_type="text/html"),
                _artifact("shot", "screenshot", content_type="image/png"),
                _artifact("ui", "ui_snapshot", content_type="application/json"),
                page_url=page.url,
                capture_metadata={"pipeline_metrics": {"page_inspection_ready": True}},
            )
            with bind_agent_run_context(AgentRunContext(
                task_id=bundle.task_id, agent_run_id="run-browser-loading",
            )):
                result = await detect_page_for_agent(
                    page_capture_id=bundle.page_capture_id,
                    evidence_loader=FakeEvidenceLoader(bundle), artifact_reader=reader,
                    chat_client=FakeChatClient(_recognized_output("home").model_copy(
                        update={"page_state": "loading", "candidate_actions": []},
                    )),
                    skill_text="# page-detection", catalog=load_default_page_type_catalog(),
                    model_name="vision-test",
                )
            assert result["rule_matching_allowed"] is False
            frontier = build_h5_frontier(
                detection_result=result, observed_at=datetime(2026, 10, 3, tzinfo=UTC),
                navigation_depth=0, lineage_metadata={"logical_reobserve_count": 2},
            )
            assert len(frontier.candidates) == 3
            selector = _select_structural_navigation_item(
                snapshot, **frontier.candidates[0].metadata["action_parameters"],
                operation=InteractionOperation.CLICK,
            )
            await page.locator(selector).click()
            await page.wait_for_url("https://fixture.test/section-a")
            assert await page.locator("main").inner_text() == "栏目内容"
        finally:
            await browser.close()
