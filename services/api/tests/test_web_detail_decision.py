"""详情页模型优先与确定性兜底的用户裁决合同。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.web_assessment_errors import WebAssessmentTransientServiceError
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    web_page_structure_facts,
)
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.tests.test_web_page_classification import Assessor, _capture
from services.api.tests.web_subject_fixture_support import load_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture,model_type,confidence,expected,source,deterministic",
    [
        ("video_primary_with_recommendations.html", "content_detail", .99,
         "content_detail", "model", "video_detail"),
        ("article_with_inline_video.html", "video_detail", .99,
         "video_detail", "model", "content_detail"),
        ("article_with_inline_video.html", None, 0,
         "content_detail", "deterministic", "content_detail"),
        ("article_with_inline_video.html", "unknown", .99,
         "content_detail", "deterministic", "content_detail"),
        ("article_with_inline_video.html", "content_list", .99,
         "content_detail", "deterministic", "content_detail"),
        ("article_with_inline_video.html", "video_detail", .30,
         "content_detail", "deterministic", "content_detail"),
    ],
)
async def test_detail_model_priority_and_independent_fallback_are_persisted(
    fixture, model_type, confidence, expected, source, deterministic
):
    from services.api.tests.test_web_page_type_assessment import Repository, _v4_recorder

    capture = _capture(facts=web_page_structure_facts(load_fixture(fixture)))
    assessment = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type=model_type, confidence=confidence, evidence=("视觉证据",),
        ) if model_type is not None else None), catalog=load_web_page_type_catalog(),
    ).classify(capture)
    assert assessment.page_type == expected
    assert assessment.decision_source == source
    assert assessment.model_result["page_type"] == model_type
    assert assessment.model_result["confidence"] == confidence
    assert assessment.deterministic_result["page_type"] == deterministic
    assert assessment.confidence >= .7
    repository = Repository()
    await _v4_recorder(repository).persist(
        task_id="task", agent_run_id="run", session_id="session",
        capture=replace(capture, captured_at=datetime(2026, 9, 15, tzinfo=UTC)),
        assessment=assessment,
    )
    for payload in (
        repository.calls[0]["assessment_payload"],
        repository.calls[0]["audit_metadata"]["web_page_type_assessment"],
    ):
        assert payload["decision_source"] == source
        assert payload["model_result"] == assessment.model_result
        assert payload["deterministic_result"] == assessment.deterministic_result
        assert payload["subject_region_facts"]["primary_subject_count"] == 1
        assert payload["structure_fingerprint"] == assessment.structure_fingerprint


@pytest.mark.asyncio
async def test_high_confidence_detail_model_is_not_vetoed_by_nonsafety_rule_conflict():
    facts = web_page_structure_facts(load_fixture("gallery_primary.html"))
    facts = replace(
        facts,
        subject_region_facts=replace(
            facts.subject_region_facts,
            repeated_primary_card_count=2,
        ),
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_detail",
                confidence=.99,
                evidence=("视觉证据",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=facts))

    assert result.deterministic_result["page_type"] == "unknown"
    assert (
        "repeated_cards_conflict_with_detail"
        in result.deterministic_result["rules"]["content_detail"]["strong_conflicts"]
    )
    assert result.page_type == "content_detail"
    assert result.needs_review is False
    assert result.decision_source == "model"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    None, WebAssessmentTransientServiceError(status_code=503),
    WebAssessmentTransientServiceError(status_code=504),
])
async def test_missing_or_unavailable_model_uses_independent_detail_evidence(failure):
    class Unavailable:
        async def classify(self, **kwargs):
            if failure:
                raise failure
            return None

    result = await WebPageClassifier(
        assessor=Unavailable(), catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=web_page_structure_facts(load_fixture("gallery_primary.html"))))
    assert result.page_type == "content_detail"
    assert result.decision_source == "deterministic"
    assert result.model_result["status"] == ("unavailable" if failure else "missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["conflict", "review", "obstruction", "invalid", "multiple"])
async def test_detail_priority_never_bypasses_safety_gates(gate):
    facts = web_page_structure_facts(load_fixture(
        "conflicting_primary_regions.html" if gate == "multiple" else "gallery_primary.html"
    ))
    if gate == "obstruction":
        facts = replace(facts, top_level_obstruction_count=1)
    result = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type="not_a_catalog_type" if gate == "invalid" else "video_detail",
            confidence=.99, evidence=("视觉证据",),
            conflicts=("SAFETY_CONFLICT",) if gate == "conflict" else (),
            needs_review=gate == "review",
        )), catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=facts))
    assert result.page_type == "unknown"
    assert result.needs_review


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["capture_failed", "human_verification_blocked", "identity_drift"]
)
async def test_model_detail_cannot_promote_failed_or_unsafe_capture(status):
    from services.api.app.application.web_readiness_contracts import (
        WebEvidenceRole,
        WebReadinessStatus,
        WebSettleAssessment,
    )

    capture = _capture(facts=web_page_structure_facts(load_fixture("gallery_primary.html")))
    capture = replace(capture, inspection_readiness=WebSettleAssessment(
        role=WebEvidenceRole.CLASSIFICATION, status=WebReadinessStatus(status),
        blocking_reasons=(status,),
    ))
    result = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type="content_detail", confidence=.99, evidence=("视觉证据",),
        )), catalog=load_web_page_type_catalog(),
    ).classify(capture)
    assert result.page_type == "unknown"
    assert status in result.conflicts


@pytest.mark.asyncio
async def test_real_adapter_request_allows_model_detail_axis_but_keeps_safety_boundaries():
    import json

    from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
    from services.api.tests.test_web_exploration_assessment_agent import (
        assessment_agent,
        first_user_payload,
    )

    client = ScriptedChatClient([make_text_response(json.dumps({
        "schema_version": "web-page-assessment.v1", "page_type": "video_detail",
        "confidence": .99, "evidence": ["视觉证据"], "needs_review_reason": None,
    }))])
    result = await WebPageClassifier(
        assessor=assessment_agent(client), catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=web_page_structure_facts(load_fixture("article_with_inline_video.html"))))
    constraints = first_user_payload(client)["constraints"]
    assert constraints["detail_axis_model_priority_with_single_subject"] is True
    assert constraints["deterministic_detail_rules_are_audit_evidence"] is True
    assert constraints["detail_safety_gates_cannot_be_overridden"] == [
        "blank", "error", "unserviceable", "multiple_subjects", "explicit_model_review",
    ]
    assert "deterministic_rule_evaluations_cannot_be_overridden" not in constraints
    assert result.page_type == "video_detail"
    assert result.decision_source == "model"
    assert result.deterministic_result["page_type"] == "content_detail"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mismatch", "load", "configuration"])
async def test_real_adapter_evidence_failure_must_not_issue_deterministic_detail(failure):
    from services.api.app.agents.explorer.web_assessment_agent import WebExplorationAssessmentAgent
    from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
    from services.api.tests.test_web_exploration_assessment_agent import StaticEvidenceLoader

    class BrokenEvidenceLoader(StaticEvidenceLoader):
        async def load(self, **kwargs):
            if failure == "load":
                raise RuntimeError("EVIDENCE_STORAGE_UNAVAILABLE")
            return replace(await super().load(**kwargs), screenshot_artifact_id="wrong-artifact")

    agent = WebExplorationAssessmentAgent(
        client=ScriptedChatClient([make_text_response("{}")]),
        evidence_loader=None if failure == "configuration" else BrokenEvidenceLoader(),
    )
    reason = {
        "mismatch": "WEB_ASSESSMENT_EVIDENCE_ID_MISMATCH",
        "load": "EVIDENCE_STORAGE_UNAVAILABLE",
        "configuration": "WEB_ASSESSMENT_EVIDENCE_LOADER_MISSING",
    }[failure]
    with pytest.raises(RuntimeError, match=reason):
        await WebPageClassifier(assessor=agent, catalog=load_web_page_type_catalog()).classify(
            _capture(facts=web_page_structure_facts(load_fixture("gallery_primary.html")))
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", [None, "unknown", "content_detail"])
async def test_returned_model_review_is_never_treated_as_missing_model(page_type):
    result = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type=page_type, confidence=.95, evidence=("模型要求复核",), needs_review=True,
        )), catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=web_page_structure_facts(load_fixture("gallery_primary.html"))))
    assert result.page_type == "unknown"
    assert result.needs_review is True
    assert result.model_result["status"] == "available"
    assert result.model_result["needs_review"] is True
    assert result.needs_review_reason == "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "asyncio", "openai_cause"])
async def test_real_provider_timeout_maps_to_typed_failure_and_detail_fallback(kind):
    import asyncio

    from agent_framework.exceptions import ChatClientException
    from httpx import Request
    from openai import APITimeoutError

    from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
    from services.api.tests.test_web_exploration_assessment_agent import assessment_agent

    class TimeoutProvider(ScriptedChatClient):
        async def _inner_get_response(self, **kwargs):
            if kind == "direct":
                raise TimeoutError("provider timeout")
            if kind == "asyncio":
                async with asyncio.timeout(0):
                    await asyncio.sleep(0)
            try:
                raise APITimeoutError(request=Request("POST", "https://model.example/test"))
            except APITimeoutError as cause:
                raise ChatClientException("provider failure") from cause

    result = await WebPageClassifier(
        assessor=assessment_agent(TimeoutProvider([make_text_response("{}")])),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=web_page_structure_facts(load_fixture("gallery_primary.html"))))
    assert result.page_type == "content_detail"
    assert result.decision_source == "deterministic"
    assert result.model_result["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["loader", "non_timeout", "cancelled"])
async def test_timeout_mapping_does_not_swallow_evidence_auth_or_cancellation(kind):
    import asyncio

    from agent_framework.exceptions import ChatClientException

    from services.api.app.agents.explorer.web_assessment_agent import WebExplorationAssessmentAgent
    from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
    from services.api.tests.test_web_exploration_assessment_agent import StaticEvidenceLoader

    class ErrorProvider(ScriptedChatClient):
        async def _inner_get_response(self, **kwargs):
            if kind == "cancelled":
                raise asyncio.CancelledError()
            raise ChatClientException("401 authentication failure, timeout policy unchanged")

    class TimeoutLoader(StaticEvidenceLoader):
        async def load(self, **kwargs):
            raise TimeoutError("evidence timeout")

    agent = WebExplorationAssessmentAgent(
        client=ErrorProvider([make_text_response("{}")]),
        evidence_loader=TimeoutLoader() if kind == "loader" else StaticEvidenceLoader(),
    )
    expected_error = {
        "loader": TimeoutError, "non_timeout": ChatClientException,
        "cancelled": asyncio.CancelledError,
    }[kind]
    with pytest.raises(expected_error):
        await WebPageClassifier(assessor=agent, catalog=load_web_page_type_catalog()).classify(
            _capture(facts=web_page_structure_facts(load_fixture("gallery_primary.html")))
        )
