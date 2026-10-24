"""模型供应商拒绝页型判定时只让当前 Web 页进入待复核。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agent_framework.exceptions import ChatClientException
from httpx import Request, Response
from openai import (
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from services.api.app.agents.explorer.web_assessment_agent import (
    WebExplorationAssessmentAgent,
)
from services.api.app.application.web_assessment_errors import (
    WebAssessmentPermissionDeniedError,
)
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
    WebPageTypeAssessmentRecordingClassifier,
)
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    _validate_web_page_type_assessment_payload,
)
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_text_response,
)
from services.api.tests.test_web_exploration_assessment_agent import StaticEvidenceLoader
from services.api.tests.test_web_page_type_assessment import Repository, _catalog_identity


def _provider_error(error_type: type[Exception], status: int) -> Exception:
    response = Response(status, request=Request("POST", "https://model.example/v1/responses"))
    return error_type("provider rejected", response=response, body={})


class _RejectingProvider(ScriptedChatClient):
    def __init__(self, failure: Exception, *, wrapping: str) -> None:
        super().__init__([make_text_response("unused")])
        self.failure = failure
        self.wrapping = wrapping
        self.request_count = 0

    async def _inner_get_response(self, **_: object):
        self.request_count += 1
        if self.wrapping == "cause":
            raise ChatClientException("provider failure") from self.failure
        if self.wrapping == "args":
            raise ChatClientException("provider failure", self.failure)
        if self.wrapping == "direct":
            raise self.failure
        raise ChatClientException("PermissionDeniedError 403 written in text only")


async def _classify_with_agent(provider: ScriptedChatClient) -> WebPageAssessment:
    agent = WebExplorationAssessmentAgent(
        client=provider, evidence_loader=StaticEvidenceLoader()
    )
    return await agent.classify(
        screenshot_artifact_id="screenshot-1",
        dom_artifact_id="dom-1",
        visible_text_artifact_id="text-1",
        allowed_page_types=("video_list", "unknown"),
        page_type_catalog=({"code": "video_list"}, {"code": "unknown"}),
        page_role="menu",
        structure_facts={},
        classification_hint=None,
    )


async def _select_detail_with_agent(provider: ScriptedChatClient) -> object:
    agent = WebExplorationAssessmentAgent(
        client=provider, evidence_loader=StaticEvidenceLoader()
    )
    return await agent.select_detail_candidate(
        screenshot_artifact_id="screenshot-1",
        dom_artifact_id="dom-1",
        candidates=({
            "candidate_id": "web-candidate:1",
            "expected_page_type": "video_detail",
            "evidence": ["当前无遮挡 Capture"],
            "text": "视频标题",
            "rect": {"x": 20, "y": 40, "width": 200, "height": 140},
        },),
        list_page_type="video_list",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapping", ["cause", "args", "direct"])
async def test_detail_selection_true_403_is_typed_without_retry(wrapping: str) -> None:
    provider = _RejectingProvider(
        _provider_error(PermissionDeniedError, 403), wrapping=wrapping
    )

    with pytest.raises(WebAssessmentPermissionDeniedError) as raised:
        await _select_detail_with_agent(provider)

    assert str(raised.value) == "WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED"
    assert raised.value.assessment_kind == "web_detail_selection"
    assert provider.request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "status", "wrapping", "expected"),
    [
        (AuthenticationError, 401, "args", ChatClientException),
        (RateLimitError, 429, "cause", ChatClientException),
        (InternalServerError, 500, "args", ChatClientException),
        (PermissionDeniedError, 403, "text_only", ChatClientException),
    ],
)
async def test_detail_selection_does_not_relabel_other_provider_errors(
    error_type: type[Exception], status: int, wrapping: str, expected: type[Exception]
) -> None:
    provider = _RejectingProvider(_provider_error(error_type, status), wrapping=wrapping)

    with pytest.raises(expected):
        await _select_detail_with_agent(provider)

    assert provider.request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_detail_selection_direct_permission_type_requires_real_403(status: int) -> None:
    provider = _RejectingProvider(
        _provider_error(PermissionDeniedError, status), wrapping="direct"
    )

    with pytest.raises(PermissionDeniedError):
        await _select_detail_with_agent(provider)

    assert provider.request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapping", ["cause", "args", "direct"])
async def test_true_openai_403_is_typed_without_retry(wrapping: str) -> None:
    provider = _RejectingProvider(
        _provider_error(PermissionDeniedError, 403), wrapping=wrapping
    )

    with pytest.raises(WebAssessmentPermissionDeniedError) as raised:
        await _classify_with_agent(provider)

    assert str(raised.value) == "WEB_PAGE_TYPE_MODEL_PERMISSION_DENIED"
    assert provider.request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "status", "wrapping"),
    [
        (AuthenticationError, 401, "args"),
        (RateLimitError, 429, "cause"),
        (InternalServerError, 500, "args"),
        (PermissionDeniedError, 403, "text_only"),
    ],
)
async def test_other_provider_failures_and_text_only_403_are_not_denial(
    error_type: type[Exception], status: int, wrapping: str
) -> None:
    provider = _RejectingProvider(_provider_error(error_type, status), wrapping=wrapping)

    with pytest.raises(ChatClientException):
        await _classify_with_agent(provider)

    assert provider.request_count == 1


def _ready_capture(capture_id: str) -> WebReadyPageCapture:
    return WebReadyPageCapture(
        capture_id=capture_id,
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id=f"{capture_id}-screenshot",
        dom_artifact_id=f"{capture_id}-dom",
        visible_text_artifact_id=f"{capture_id}-text",
        page_role="menu",
        url=f"https://example.org/videos/{capture_id}",
        captured_at=datetime(2026, 9, 30, tzinfo=UTC),
        structure_facts=WebPageStructureFacts(
            navigation_element_count=0,
            repeated_link_count=3,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("video-list", "poster"),
            repeated_primary_card_count=3,
            media_card_count=3,
            primary_detail_link_count=3,
            subject_region_facts=WebSubjectRegionFacts(repeated_primary_card_count=3),
        ),
    )


@pytest.mark.asyncio
async def test_denied_ready_capture_persists_unknown_and_next_menu_can_classify() -> None:
    class _SequencedAssessor:
        calls = 0

        async def classify(self, **_: object) -> WebPageAssessment:
            self.calls += 1
            if self.calls == 1:
                raise WebAssessmentPermissionDeniedError()
            return WebPageAssessment(
                page_type="video_list", confidence=0.9, evidence=("独立视频卡片",)
            )

    repository = Repository()
    assessor = _SequencedAssessor()
    recorder = WebPageTypeAssessmentRecorder(
        repository=repository, catalog_identity=_catalog_identity()
    )
    classifier = WebPageTypeAssessmentRecordingClassifier(
        classifier=WebPageClassifier(
            assessor=assessor, catalog=load_web_page_type_catalog()
        ),
        recorder=recorder,
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
    )

    denied = await classifier.classify(_ready_capture("menu-denied"))
    continued = await classifier.classify(_ready_capture("menu-next"))

    assert assessor.calls == 2
    assert denied.page_type == "unknown"
    assert denied.needs_review_reason == "WEB_PAGE_TYPE_MODEL_PERMISSION_DENIED"
    assert denied.model_result["status"] == "permission_denied"
    assert continued.page_type == "video_list"
    assert [call["capture_id"] for call in repository.calls] == [
        "menu-denied", "menu-next"
    ]
    denied_payload = repository.calls[0]["assessment_payload"]
    assert repository.calls[0]["canonical_page_type"] == "unknown"
    assert denied_payload["classification_status"] == "needs_review"
    assert denied_payload["reason_code"] == "WEB_PAGE_TYPE_MODEL_PERMISSION_DENIED"
    assert denied_payload["page_capture_ids"] == ["menu-denied"]
    assert denied_payload["model_result"]["status"] == "permission_denied"
    assert denied_payload["decision_source"] == "provider_denial"
    _validate_web_page_type_assessment_payload(
        canonical_page_type="unknown",
        audit_metadata=repository.calls[0]["audit_metadata"],
        assessment_payload=denied_payload,
        expected_schema_version="web-page-type-assessment.v4",
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="menu-denied",
        expected_catalog_identity=_catalog_identity(),
    )
    assert repository.calls[1]["canonical_page_type"] == "video_list"


@pytest.mark.asyncio
async def test_permission_denial_does_not_mask_assessment_database_failure() -> None:
    class _DeniedAssessor:
        async def classify(self, **_: object) -> WebPageAssessment:
            raise WebAssessmentPermissionDeniedError()

    class _BrokenRepository(Repository):
        async def persist_web_page_type_assessment(self, **kwargs: object) -> str:
            raise RuntimeError("ASSESSMENT_DB_UNAVAILABLE")

    classifier = WebPageTypeAssessmentRecordingClassifier(
        classifier=WebPageClassifier(
            assessor=_DeniedAssessor(), catalog=load_web_page_type_catalog()
        ),
        recorder=WebPageTypeAssessmentRecorder(
            repository=_BrokenRepository(), catalog_identity=_catalog_identity()
        ),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
    )

    with pytest.raises(RuntimeError, match="ASSESSMENT_DB_UNAVAILABLE"):
        await classifier.classify(_ready_capture("menu-denied"))
