"""Web 模型复核说明限长，不改变分类及复核语义。"""

import json

import pytest
from pydantic import ValidationError

from services.api.app.agents.explorer.web_assessment_agent import (
    WebPageAssessmentEnvelopeModel,
)
from services.api.tests.support.fake_chat_client import make_text_response
from services.api.tests.test_web_exploration_assessment_agent import (
    ProviderParsingScriptedChatClient,
    ScriptedChatClient,
    assessment_agent,
)


@pytest.mark.parametrize("client_type", [ScriptedChatClient, ProviderParsingScriptedChatClient])
@pytest.mark.parametrize(
    "reason",
    ["需复核主体冲突。" * 80, "Rule evaluations require review. " * 30],
    ids=["chinese", "english"],
)
@pytest.mark.asyncio
async def test_long_review_reason_retains_review_without_retry(client_type, reason):
    payload = dict(
        page_type="content_detail",
        confidence=0.85,
        evidence=["正文存在，但目录信号冲突"],
        needs_review_reason=reason,
    )
    client = client_type([make_text_response(json.dumps(payload, ensure_ascii=False))])
    result = await assessment_agent(client).classify(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        allowed_page_types=("content_detail",),
        page_type_catalog=({"code": "content_detail"},),
        page_role="menu",
        structure_facts={},
        classification_hint=None,
    )
    assert result.page_type == "content_detail"
    assert result.confidence == 0.85
    assert result.needs_review_reason == reason[:200]
    assert len(client.received_messages) == 1


@pytest.mark.parametrize(
    "reason", [None, "", "需人工确认", " " * 201], ids=["null", "empty", "short", "blank"]
)
def test_short_or_empty_review_reason_preserves_existing_semantics(reason):
    result = WebPageAssessmentEnvelopeModel(
        page_type="home",
        confidence=0.8,
        evidence=[],
        needs_review_reason=reason,
    )
    assert result.needs_review_reason == (reason[:200] if isinstance(reason, str) else reason)


@pytest.mark.parametrize(
    "override",
    [
        {"needs_review_reason": 123},
        {"confidence": 2},
        {"page_type": "x" * 81},
        {"unexpected": True},
    ],
)
def test_review_reason_normalization_does_not_relax_other_contracts(override):
    with pytest.raises(ValidationError):
        WebPageAssessmentEnvelopeModel.model_validate(
            {
                "page_type": "home",
                "confidence": 0.8,
                "evidence": [],
                "needs_review_reason": "需复核" * 100,
                **override,
            }
        )


def test_long_review_reason_keeps_nonempty_signal_and_redacts_before_truncation():
    result = WebPageAssessmentEnvelopeModel(
        page_type="content_detail",
        confidence=0.85,
        evidence=[],
        needs_review_reason=" " * 250 + "需人工复核 token=private-value " + "存在冲突。" * 60,
    )
    assert result.needs_review_reason.startswith("需人工复核 token=[redacted]")
    assert "private-value" not in result.needs_review_reason
    assert len(result.needs_review_reason) <= 200
