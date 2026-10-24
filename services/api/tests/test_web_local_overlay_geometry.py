"""Web 局部浮层只读几何不扩大点击权限的回归。"""

import json
from copy import deepcopy

import pytest

from services.api.app.application import web_obstruction_geometry as geometry
from services.api.app.domain.exploration.web_exploration import build_web_obstruction_candidates


def _rect(x=0, y=0, width=100, height=100):
    return {"x": x, "y": y, "width": width, "height": height}


def _snapshot(*rects):
    return {
        "web_detail_candidate_priority_enabled": True,
        "viewport": {"width": 100, "height": 100},
        "elements": [{"tag": "main", "visible": True, "rect": _rect()}],
        "web_overlay_geometry": {
            "schema_version": "web-overlay-geometry.v1", "complete": True,
            "layers": [{"rect": rect, "ordinary": True} for rect in rects],
            "subjects": [_rect()],
        },
    }


def test_local_overlay_geometry_can_prove_benign_without_creating_click_candidate():
    snapshot = _snapshot(_rect(90, 90, 10, 10))
    assert geometry.web_has_only_benign_ordinary_overlays(snapshot)
    assert build_web_obstruction_candidates(capture_id="capture", ui_snapshot=snapshot) == ()


@pytest.mark.parametrize("rects, expected", [
    ((_rect(0, 0, 20, 100), _rect(20, 0, 20, 100)), False),
    ((_rect(0, 0, 20, 100), _rect(0, 0, 20, 100)), True),
    ((_rect(0, 0, 30, 100),), False),
])
def test_union_coverage_neither_omits_layers_nor_double_counts(rects, expected):
    assert geometry.web_has_only_benign_ordinary_overlays(_snapshot(*rects)) is expected


@pytest.mark.parametrize("failure", [
    "incomplete", "missing_rect", "special_gate", "no_subject", "no_layers", "non_web",
    "small_subject", "other_subject_covered", "too_many_layers",
])
def test_insufficient_or_risky_geometry_never_becomes_benign(failure):
    snapshot = _snapshot(_rect(0, 0, 10, 10))
    census = snapshot["web_overlay_geometry"]
    if failure == "incomplete":
        census["complete"] = False
    elif failure == "missing_rect":
        census["layers"][0].pop("rect")
    elif failure == "special_gate":
        census["layers"][0]["ordinary"] = False
    elif failure == "no_subject":
        snapshot["elements"] = []
        census["subjects"] = []
    elif failure == "no_layers":
        census["layers"] = []
    elif failure == "non_web":
        snapshot["web_detail_candidate_priority_enabled"] = False
    elif failure == "small_subject":
        snapshot["elements"][0]["rect"] = _rect(0, 0, 10, 10)
        census["subjects"] = [_rect(0, 0, 10, 10)]
    elif failure == "other_subject_covered":
        snapshot["elements"].append({"tag": "article", "visible": True,
                                     "rect": _rect(0, 0, 10, 10)})
        census["subjects"].append(_rect(0, 0, 10, 10))
    else:
        census["layers"] *= 33
    assert not geometry.web_has_only_benign_ordinary_overlays(snapshot)


def test_structural_and_local_overlays_share_one_union():
    snapshot = _snapshot(_rect(20, 0, 20, 100))
    snapshot["elements"].append({
        "tag": "div", "visible": True, "rect": _rect(0, 0, 20, 100),
        "web_obstruction_layer_structural": True, "web_obstruction_layer_kind": "overlay",
    })
    assert not geometry.web_has_only_benign_ordinary_overlays(snapshot)


def test_geometry_summary_is_controlled_and_uses_visible_subject_denominator():
    summary = getattr(geometry, "web_obstruction_geometry_facts", None)
    assert callable(summary), "缺少向 Web 判定传递受控几何事实的接口"
    snapshot = _snapshot(_rect(0, 0, 10, 100))
    snapshot["elements"][0]["rect"] = _rect(0, -100, 100, 200)
    snapshot["web_overlay_geometry"]["subjects"] = [_rect(0, -100, 100, 200)]
    assert summary(snapshot) == {
        "status": "below_threshold", "complete": True, "layer_count": 1,
        "subject_count": 1, "subject_overlap_ratio": .1, "review_overlap_threshold": .3,
    }


@pytest.mark.asyncio
async def test_web_browser_captures_small_layer_but_not_sticky_navigation():
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
    from services.api.tests.web_browser_test_support import (
        WebBrowserUnavailable,
        launch_web_test_browser,
    )

    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page(viewport={"width": 1000, "height": 800})
            await page.set_content('''<main style="width:800px;height:700px">正文</main>
                <nav style="position:fixed;top:0;width:100%;height:40px">导航</nav>
                <div style="position:fixed;right:10px;bottom:10px;width:100px;height:100px">
                <button>关闭</button></div>''')
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            census = snapshot.get("web_overlay_geometry")
            assert isinstance(census, dict), "Web 快照缺少边角浮层的只读几何"
            assert census["complete"] is True
            assert len(census["layers"]) == 1
            assert len(census["subjects"]) == 1
            assert census["layers"][0]["rect"] == _rect(890, 690, 100, 100)
            assert build_web_obstruction_candidates(
                capture_id="capture", ui_snapshot=snapshot,
            ) == ()
            for flag in [None, False]:
                args = {} if flag is None else {"web_detail_candidate_priority": flag}
                legacy = await _collect_ui_snapshot(page, **args)
                assert "web_overlay_geometry" not in legacy
            await page.set_content('''<main style="width:800px;height:700px">正文</main>
                <header><div class="overlay" style="position:fixed;inset:0">
                <main style="width:100px;height:100px">浮层内部</main></div></header>
                <div style="position:fixed;right:0;bottom:0;width:10px;height:10px"></div>''')
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            census = snapshot["web_overlay_geometry"]
            assert len(census["layers"]) == 2
            assert len(census["subjects"]) == 1
        finally:
            await browser.close()


def test_large_snapshot_keeps_geometry_bounded_and_does_not_claim_partial_census_complete():
    from services.api.app.infrastructure.browser.page_capture import (
        MAX_UI_SNAPSHOT_BYTES,
        _json_size_bytes,
        _sanitize_and_bound_ui_snapshot,
    )

    snapshot = _snapshot(_rect(90, 90, 10, 10))
    snapshot["web_overlay_geometry"]["layers"] *= 33
    bounded = _sanitize_and_bound_ui_snapshot(deepcopy(snapshot))
    assert _json_size_bytes(bounded) <= MAX_UI_SNAPSHOT_BYTES
    assert bounded["web_overlay_geometry"]["complete"] is False
    assert len(bounded["web_overlay_geometry"]["layers"]) <= 32


@pytest.mark.asyncio
async def test_assessment_receives_controlled_geometry_without_authorizing_missing_candidate():
    from services.api.app.application.web_page_readiness import WebObstructionReviewRequiredError
    from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
    from services.api.tests.test_web_exploration_assessment_agent import (
        assessment_agent,
        first_user_payload,
    )

    client = ScriptedChatClient([make_text_response(json.dumps({
        "schema_version": "web-obstruction-assessment.v1", "obstruction_present": True,
        "dismiss_candidate_id": None, "confidence": .9, "reason": "需复核",
        "human_verification_present": False, "age_gate_present": False,
    }))])
    facts = {
        "status": "review_required", "complete": True, "layer_count": 2,
        "subject_count": 1, "subject_overlap_ratio": .4, "review_overlap_threshold": .3,
        "selector": "secret-selector", "text": "secret-body",
    }
    method = assessment_agent(client).assess_obstruction
    import inspect
    assert "obstruction_geometry" in inspect.signature(method).parameters
    with pytest.raises(WebObstructionReviewRequiredError):
        await method(screenshot_artifact_id="screenshot", dom_artifact_id="dom",
                     candidates=(), page_role="menu", obstruction_geometry=facts)
    payload = first_user_payload(client)
    assert payload["obstruction_geometry"] == {k: v for k, v in facts.items()
                                               if k not in {"selector", "text"}}


@pytest.mark.asyncio
async def test_runtime_evidence_exposes_geometry_without_changing_candidate_closed_set():
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.web_runtime_evidence import WebRuntimeEvidenceSource
    from services.api.tests.test_web_runtime_evidence import IndexReader

    class Reader:
        async def read_ui_snapshot(self, **kwargs):
            return _snapshot(_rect(0, 0, 40, 100))

    source = WebRuntimeEvidenceSource(
        task_id="task-1", agent_run_id="run-1", session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=IndexReader(), snapshot_reader=Reader(),
    )
    evidence = await source.read_page(capture_id="capture-1")
    assert getattr(evidence, "obstruction_geometry", None) == {
        "status": "review_required", "complete": True, "layer_count": 1,
        "subject_count": 1, "subject_overlap_ratio": .4, "review_overlap_threshold": .3,
    }
    assert evidence.candidates == ()
    assert evidence.benign_ordinary_overlays_only is False


@pytest.mark.asyncio
async def test_local_benign_geometry_does_not_skip_model_special_gate_review():
    from dataclasses import replace

    from services.api.app.application.web_page_readiness import (
        WebObstructionAssessment,
        WebPageReadinessCoordinator,
    )
    from services.api.app.application.web_readiness_contracts import WebReadinessStatus
    from services.api.tests.test_web_page_readiness import (
        Coordinator,
        EvidenceReader,
        _completed,
        _evidence,
    )

    facts = geometry.web_obstruction_geometry_facts(_snapshot(_rect(90, 90, 10, 10)))
    entry = replace(_evidence("capture-1", text=None),
                    benign_ordinary_overlays_only=True, obstruction_geometry=facts)

    class Assessor:
        async def assess_obstruction(self, **kwargs):
            assert kwargs["obstruction_geometry"] == facts
            return WebObstructionAssessment(
                obstruction_present=True, dismiss_candidate_id=None, confidence=.99,
                reason="特殊门禁优先", human_verification_present=True, age_gate_present=False,
            )

    ready = _evidence("capture-ready", text=None)
    coordinator = Coordinator([ready.capture_id])
    result = await WebPageReadinessCoordinator(
        assessor=Assessor(), coordinator=coordinator,
        evidence_reader=EvidenceReader({entry.capture_id: entry, ready.capture_id: ready}),
        sleeper=lambda _seconds: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="menu")
    assert result.status is WebReadinessStatus.HUMAN_VERIFICATION_BLOCKED
    assert coordinator.intents == []


@pytest.mark.asyncio
async def test_browser_census_marks_special_gates_and_collection_limits_conservatively():
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
    from services.api.tests.web_browser_test_support import (
        WebBrowserUnavailable,
        launch_web_test_browser,
    )

    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page(viewport={"width": 1000, "height": 800})
            for gate in ('class="age-gate"', 'aria-modal="true"', 'data-sitekey="test"'):
                await page.set_content(
                    '<main style="width:800px;height:700px">正文</main>'
                    f'<div {gate} style="position:fixed;right:0;bottom:0;'
                    'width:100px;height:100px">验证</div>'
                )
                snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
                assert snapshot["web_overlay_geometry"]["layers"][0]["ordinary"] is False
                assert not geometry.web_has_only_benign_ordinary_overlays(snapshot)
            await page.set_content('<main>正文</main>' + ''.join(
                f'<div style="position:fixed;left:{i}px;top:0;width:1px;height:1px"></div>'
                for i in range(33)
            ))
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            assert snapshot["web_overlay_geometry"]["complete"] is False
            assert len(snapshot["web_overlay_geometry"]["layers"]) == 32
            await page.set_content('<main>正文</main>' + '<span></span>' * 4001)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            assert snapshot["web_overlay_geometry"]["complete"] is False
        finally:
            await browser.close()


def test_crowded_web_snapshot_keeps_readonly_geometry_under_existing_byte_limit():
    from services.api.app.infrastructure.browser.page_capture import (
        MAX_UI_SNAPSHOT_BYTES,
        _json_size_bytes,
        _sanitize_and_bound_ui_snapshot,
    )
    from services.api.tests.test_web_subject_snapshot_regressions import _crowded_snapshot

    snapshot = _crowded_snapshot(web_marker=True)
    snapshot["web_overlay_geometry"] = _snapshot(_rect())["web_overlay_geometry"]
    bounded = _sanitize_and_bound_ui_snapshot(snapshot)
    assert bounded["truncated"] is True
    assert _json_size_bytes(bounded) <= MAX_UI_SNAPSHOT_BYTES
    assert bounded["web_overlay_geometry"]["complete"] is True
    assert not geometry.web_has_only_benign_ordinary_overlays(bounded)


def test_complete_census_keeps_subject_lost_from_bounded_elements_in_union_check():
    snapshot = _snapshot(_rect(0, 0, 10, 10))
    snapshot["truncated"] = True
    snapshot["web_overlay_geometry"]["subjects"].append(_rect(0, 0, 10, 10))
    assert not geometry.web_has_only_benign_ordinary_overlays(snapshot)


def test_census_missing_subject_geometry_never_infers_clear_from_remaining_elements():
    snapshot = _snapshot(_rect(90, 90, 10, 10))
    snapshot["truncated"] = True
    snapshot["web_overlay_geometry"].pop("subjects")
    assert geometry.web_obstruction_geometry_facts(snapshot)["status"] == "unknown"


def test_web_marker_with_missing_census_does_not_claim_complete_structural_geometry():
    snapshot = _snapshot()
    snapshot.pop("web_overlay_geometry")
    snapshot["elements"].append({
        "tag": "div", "visible": True, "rect": _rect(90, 90, 10, 10),
        "web_obstruction_layer_structural": True, "web_obstruction_layer_kind": "overlay",
    })
    assert geometry.web_obstruction_geometry_facts(snapshot)["status"] == "unknown"
