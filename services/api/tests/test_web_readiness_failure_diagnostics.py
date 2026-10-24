"""阻断判定失败保留安全原因码，不改变页面准入或触发额外点击。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.web_assessment_errors import WebAssessmentTransientServiceError
from services.api.app.application.web_page_readiness import (
    WebObstructionReviewRequiredError,
    WebPageReadinessCoordinator,
)
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_page_readiness import (
    Coordinator,
    EvidenceReader,
    _completed,
    _evidence,
)
from services.api.tests.test_web_runtime_stage_effects import Evidence, command, cursor


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TypeError("不落库的模型原文"), "WEB_OBSTRUCTION_ASSESSMENT_INVALID"),
        (WebAssessmentTransientServiceError(status_code=503),
         "WEB_OBSTRUCTION_ASSESSMENT_UNAVAILABLE"),
        (WebObstructionReviewRequiredError("不落库的模型原文"),
         "WEB_OBSTRUCTION_CANDIDATES_MISSING"),
        (WebObstructionReviewRequiredError(
            "不落库的模型原文", reason_code="WEB_OBSTRUCTION_CANDIDATES_AMBIGUOUS"),
         "WEB_OBSTRUCTION_CANDIDATES_AMBIGUOUS"),
        (WebObstructionReviewRequiredError(
            "不落库的模型原文", reason_code="非可信原文/不可保存"),
         "WEB_OBSTRUCTION_REVIEW_REQUIRED"),
    ],
)
async def test_obstruction_failure_cause_survives_readiness_and_stage_boundary(error, reason):
    class Assessor:
        async def assess_obstruction(self, **kwargs):
            raise error

    entry = _evidence("capture-1", text=None)
    refreshed = _evidence("capture-refreshed", text=None)
    coordinator = Coordinator(["capture-refreshed"])
    result = await WebPageReadinessCoordinator(
        assessor=Assessor(), coordinator=coordinator,
        evidence_reader=EvidenceReader({entry.capture_id: entry, refreshed.capture_id: refreshed}),
        sleeper=lambda _: _completed(),
    ).prepare(source_capture_id=entry.capture_id, page_role="menu")
    assert result.status == "popup_blocked" and result.ready_capture_id is None
    assert reason in result.blocking_reasons
    assert all(intent.action_type.value == "observe" for intent in coordinator.intents)

    class Readiness:
        async def prepare(self, **kwargs):
            return result

    class CurrentEvidence(Evidence):
        async def current_capture_reference(self):
            return {"capture_id": refreshed.capture_id, "current_url": "https://example.test/"}

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(page_readiness=Readiness()),
        evidence=CurrentEvidence(), command=command(),
    )
    with pytest.raises(WebStageFailure) as raised:
        await effects.execute(cursor(WebRunStage.READINESS))
    assert raised.value.code == "WEB_READINESS_POPUP_BLOCKED"
    assert raised.value.detail == {
        "raw_error_code": reason, "capture_id": refreshed.capture_id,
        "current_url": "https://example.test/",
    }
