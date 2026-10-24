"""Web 空白审计展示和阻断诊断落库回归。"""

import json
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.task_insight import _web_logical_page_nodes
from services.api.app.application.web_page_readiness import (
    WebObstructionAssessment,
    WebObstructionReviewRequiredError,
    WebPageReadinessCoordinator,
)
from services.api.app.infrastructure.db.web_popup_blocked_repository import (
    PostgresWebPopupBlockedRepository,
    WebPopupBlockedPersistenceError,
    _stale_reason,
)
from services.api.tests.test_task_insight_service import FakeInsightRepository, _service
from services.api.tests.test_web_capture_evidence_repository import (
    Pool,
    TransactionContext,
)
from services.api.tests.test_web_page_readiness import (
    Coordinator,
    EvidenceReader,
    ScriptedAssessor,
    _completed,
    _evidence,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("page_role", ["home", "menu", "detail"])
async def test_popup_blocked_records_latest_evidence_without_inspection_notification(page_role):
    entry = _evidence("entry", text=None)
    refreshed = _evidence("refreshed", text=None)

    class Assessor:
        async def assess_obstruction(self, **kwargs):
            raise WebObstructionReviewRequiredError("存在阻断但无安全关闭候选")

    recorder = AsyncMock()
    result = await WebPageReadinessCoordinator(
        assessor=Assessor(), coordinator=Coordinator(["refreshed"]),
        evidence_reader=EvidenceReader({"entry": entry, "refreshed": refreshed}),
        sleeper=lambda _: _completed(), popup_blocked_recorder=recorder,
    ).prepare(source_capture_id="entry", page_role=page_role)
    recorder.assert_awaited_once_with(
        capture_id="refreshed", blocking_reasons=result.blocking_reasons,
    )
    assert result.status == "popup_blocked"
    assert not result.is_ready
    assert result.ready_capture_id is None


@pytest.mark.asyncio
async def test_human_verification_does_not_record_popup_type():
    entry = _evidence("entry", text="关闭")

    class Assessor:
        async def assess_obstruction(self, **kwargs):
            return WebObstructionAssessment(
                obstruction_present=True, dismiss_candidate_id=None, confidence=0.99,
                reason="人机验证", human_verification_present=True, age_gate_present=False,
            )

    recorder = AsyncMock()
    result = await WebPageReadinessCoordinator(
        assessor=Assessor(), coordinator=Coordinator([]),
        evidence_reader=EvidenceReader({"entry": entry}), popup_blocked_recorder=recorder,
    ).prepare(source_capture_id="entry", page_role="home")
    assert result.status == "human_verification_blocked"
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_model_response_does_not_invent_popup_page_type():
    evidence = [_evidence("entry", text=None), _evidence("refreshed", text=None)]

    class Assessor:
        async def assess_obstruction(self, **kwargs):
            raise ValueError("模型响应结构无效")

    recorder = AsyncMock()
    result = await WebPageReadinessCoordinator(
        assessor=Assessor(), coordinator=Coordinator(["refreshed"]),
        evidence_reader=EvidenceReader({item.capture_id: item for item in evidence}),
        sleeper=lambda _: _completed(), popup_blocked_recorder=recorder,
    ).prepare(source_capture_id="entry", page_role="home")
    assert "WEB_OBSTRUCTION_ASSESSMENT_INVALID" in result.blocking_reasons
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_popup_close_labels_only_original_blocking_frames():
    evidence = [
        _evidence("popup-1", text="关闭广告"),
        _evidence("popup-2", text="稍后再说", selector="#later"),
        _evidence("clear", text=None),
        _evidence("capture-ready", text=None),
    ]
    recorder = AsyncMock()
    result = await WebPageReadinessCoordinator(
        assessor=ScriptedAssessor({item.screenshot_artifact_id: item for item in evidence}),
        coordinator=Coordinator(["popup-2", "clear", "capture-ready"]),
        evidence_reader=EvidenceReader({item.capture_id: item for item in evidence}),
        sleeper=lambda _: _completed(), popup_blocked_recorder=recorder,
    ).prepare(source_capture_id="popup-1", page_role="home")
    assert result.is_ready
    assert result.ready_capture_id == "capture-ready"
    assert [call.kwargs["capture_id"] for call in recorder.await_args_list] == [
        "popup-1", "popup-2",
    ]


def test_blank_audit_is_hidden_without_hiding_unclassified_http_pages():
    nodes = [
        {"page_capture_id": "blank", "url": "about:blank", "node_seq": 1},
        {"page_capture_id": "loading", "url": "about:blank#loading", "node_seq": 2},
        {"page_capture_id": "blocked", "url": "https://example.test/", "node_seq": 3,
         "page_type": "popup_ad", "page_state": "popup_blocked"},
    ]
    result = _web_logical_page_nodes(nodes, assessments=[], trusted_identities={})
    assert [node["page_capture_id"] for node in result] == ["blocked"]
    assert result[0]["page_type"] == "popup_ad"
    assert len(nodes) == 3


@pytest.mark.asyncio
async def test_web_timeline_hides_blank_and_names_blocked_page():
    repository = FakeInsightRepository()
    repository.context.update(
        target_platform="web", status="failed",
        task_config={"workflow_version": "v2", "execution_mode": "exploration_only"},
    )
    repository.read_web_capture_identities = AsyncMock(return_value={})
    repository.h5_captures = [
        {"page_capture_id": "blank", "url": "about:blank", "page_seq": 1},
        {"page_capture_id": "blocked", "url": "https://example.test/", "page_seq": 2,
         "page_type": "popup_ad", "page_state": "popup_blocked"},
    ]
    timeline = await _service(repository).page_timeline("task-1")
    assert len(timeline["nodes"]) == 1
    assert timeline["nodes"][0]["page_type"] == "popup_ad"
    assert timeline["nodes"][0]["page_type_name"] == "弹窗页面"


@pytest.mark.asyncio
async def test_web_blank_only_timeline_does_not_restore_journey_placeholders():
    repository = FakeInsightRepository()
    repository.context.update(
        target_platform="web",
        task_config={"workflow_version": "v2", "journey_steps": ["首页"]},
    )
    repository.read_web_capture_identities = AsyncMock(return_value={})
    repository.h5_captures = [
        {"page_capture_id": "blank", "url": "about:blank", "page_seq": 1},
    ]
    timeline = await _service(repository).page_timeline("task-1")
    assert timeline["nodes"] == []


class Connection:
    def __init__(self, row):
        self.row = row
        self.transactions = self.commits = self.rollbacks = 0
        self.fetchrow = AsyncMock(return_value=row)

    def transaction(self):
        return TransactionContext(self)


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [True, False])
async def test_popup_repository_records_diagnostic_without_fabricating_classification(recorded):
    connection = Connection({"capture_id": "capture-1", "recorded": recorded})
    result = await PostgresWebPopupBlockedRepository(pool=Pool(connection)).record(
        task_id="task-1", agent_run_id="run-1", session_id="web:run-1", capture_id="capture-1",
        blocking_reasons=("popup_blocked", "WEB_OBSTRUCTION_REVIEW_REQUIRED", "secret:raw"),
    )
    assert result is recorded
    assert connection.commits == 1
    args = connection.fetchrow.call_args.args
    assert args[1:5] == ("task-1", "run-1", "web:run-1", "capture-1")
    metadata = json.loads(args[5])
    assert metadata["is_obstructed"] is True
    assert metadata["web_popup_blocked"]["page_type"] == "popup_ad"
    assert metadata["web_popup_blocked"]["blocking_reasons"] == [
        "popup_blocked", "WEB_OBSTRUCTION_REVIEW_REQUIRED",
    ]
    assert "web_page_type_assessment" not in metadata


_FENCES_ALL_PASS = {
    "target_platform": "web", "execution_status": "running", "finalization_state": "none",
    "finalization_epoch": 0, "workflow_version": "v2", "latest_run_id": "run-1",
    "session_status": "active", "session_epoch": 0, "session_capture_id": "capture-1",
    "capture_status": "captured", "capture_run_id": "run-1", "capture_session_id": "web:run-1",
    "capture_epoch": "0", "page_status": "captured", "page_session_id": "web:run-1",
    "page_epoch": "0", "page_url": "https://example.test/",
}


@pytest.mark.asyncio
async def test_popup_repository_reports_why_evidence_is_stale():
    """写入没选中行时要给出具体失效原因，事后才能定位；这里是会话已在排空。"""
    connection = Connection(None)
    connection.fetchrow = AsyncMock(
        side_effect=[None, {**_FENCES_ALL_PASS, "session_status": "draining"}]
    )
    with pytest.raises(WebPopupBlockedPersistenceError) as raised:
        await PostgresWebPopupBlockedRepository(pool=Pool(connection)).record(
            task_id="task-1", agent_run_id="run-1", session_id="web:run-1",
            capture_id="capture-1", blocking_reasons=("popup_blocked",),
        )
    assert raised.value.reason_code == "session_draining"
    assert "session_draining" in str(raised.value)
    assert connection.rollbacks == 1


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"finalization_state": "sealed"}, "task_sealed"),
        ({"latest_run_id": "run-2"}, "run_not_latest"),
        ({"session_status": None}, "session_missing"),
        ({"session_capture_id": "capture-2"}, "capture_not_current"),
        ({"capture_epoch": "1"}, "capture_epoch_mismatch"),
        ({"page_url": "about:blank"}, "page_url_not_http"),
        ({}, "unknown"),
    ],
)
def test_stale_reason_names_first_failed_fence(override, reason):
    assert _stale_reason(
        {**_FENCES_ALL_PASS, **override},
        agent_run_id="run-1", session_id="web:run-1", capture_id="capture-1",
    ) == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"capture_id": "foreign", "recorded": True}])
async def test_popup_repository_rolls_back_invalid_evidence(row):
    connection = Connection(row)
    with pytest.raises(WebPopupBlockedPersistenceError):
        await PostgresWebPopupBlockedRepository(pool=Pool(connection)).record(
            task_id="task-1", agent_run_id="run-1", session_id="web:run-1",
            capture_id="capture-1", blocking_reasons=("popup_blocked",),
        )
    assert connection.commits == 0
    assert connection.rollbacks == 1
