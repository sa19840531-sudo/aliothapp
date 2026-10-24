"""七站问题的通用回归：结构静默、辅助链接和模型闭集重试。"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_detail_candidate_extraction import (
    web_card_anchor_is_auxiliary,
)
from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult
from services.api.app.application.web_runtime_stage_discovery import WebRuntimeStageDiscovery
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics
from services.api.tests.test_web_exploration_assessment_agent import (
    ScriptedChatClient,
    assessment_agent,
    make_text_response,
)
from services.api.tests.test_web_runtime_stage_effects import command, cursor


@pytest.mark.parametrize("drift,expected", [(False, True), (True, False)])
def test_proven_window_survives_dom_mutation_but_not_structure_drift(drift, expected):
    raw = _stable_capture_metrics()
    raw["dom_quiet_ms"] = 582
    if drift:
        raw["structure_after"] = "different"
    result = read_web_readiness_assessment({"web_readiness": raw})
    assert result.can_serve_evidence is expected
    assert result.signals.dom_quiet_ms == 582


@pytest.mark.parametrize("field,value", [
    ("visible_loader", True), ("transition_active", True),
    ("critical_network_pending", 1), ("necessary_media_ready", False),
])
def test_structural_window_never_bypasses_other_readiness_requirements(field, value):
    raw = _stable_capture_metrics()
    raw["dom_quiet_ms"] = 582
    raw[field] = value
    assert not read_web_readiness_assessment({"web_readiness": raw}).can_serve_evidence


@pytest.mark.parametrize("token,expected", [
    ("detail-comments", True), ("comments", True), ("comment-section", True),
    ("commentary-card", False), ("uncommented-card", False), ("article-card", False),
])
def test_comment_auxiliary_links_are_not_primary_cards(token, expected):
    assert web_card_anchor_is_auxiliary({
        "href": "https://example.test/item/7", "ancestor_class_tokens": [token],
    }) is expected


def test_recommendation_inside_comments_keeps_secondary_card_evidence():
    from services.api.app.application.web_primary_subject_facts import extract_subject_region_facts
    from services.api.tests.test_web_four_site_feature_regressions import _node

    nodes = [_node("#body", "article", web_subject_text_length=400)] + [
        _node(f"#card-{i}", "a", href=f"https://example.test/item/{i}",
              repeat_group_size=3, ancestor_class_tokens=["detail-comments", "widget-recommend"])
        for i in range(3)
    ]
    facts = extract_subject_region_facts({"elements": nodes})
    assert facts.primary_article_body_count == 1
    assert facts.repeated_primary_card_count == 0
    assert facts.secondary_recommendation_card_count == 3


async def test_detail_selection_retries_invented_id_on_same_evidence():
    client = ScriptedChatClient([
        make_text_response(json.dumps({
            "schema_version": "web-detail-selection.v1", "selected_candidate_id": selected,
            "confidence": 0.9, "reason": "选择当前卡片",
        })) for selected in ("invented", "card-1")
    ])
    result = await assessment_agent(client).select_detail_candidate(
        screenshot_artifact_id="artifact-screenshot", dom_artifact_id="artifact-dom",
        candidates=({"candidate_id": "card-1", "text": "当前卡片"},),
        list_page_type="content_list",
    )
    assert result.selected_candidate_id == "card-1"
    assert len(client.received_messages) == 2


async def test_mismatched_detail_arrival_still_offers_actual_capture_to_ad_scheduler():
    scheduled = []

    class Frontier:
        async def detail_authority(self, *_args):
            return None

    class Ads:
        async def schedule(self, source, checkpoints):
            scheduled.append(source)

    source = cursor(WebRunStage.CANDIDATE_NAVIGATION)
    discovery = WebRuntimeStageDiscovery(
        command=command(), evidence=None, frontier=Frontier(), checkpoints=SimpleNamespace(),
        ads=Ads(),
    )
    outcome = WebStageEffectResult(cursor_payload={"capture_id": "actual-list-capture"})
    await discovery.schedule_detail(source, outcome)
    assert scheduled[0].logical_page_key == "detail:" + source.logical_page_key
    assert scheduled[0].cursor_payload["capture_id"] == "actual-list-capture"
    scheduled.clear()
    await discovery.schedule_detail(source, replace(outcome, cursor_payload=source.cursor_payload))
    assert not scheduled
