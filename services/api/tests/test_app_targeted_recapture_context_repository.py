"""App 定向补采上下文 PostgreSQL 读取测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.app_targeted_recapture_context_repository import (
    PostgresAppTargetedRecaptureContextReader,
)
from services.api.tests.test_v2_control_plane_repositories import FakeConnection, FakePool

NOW = datetime(2026, 9, 8, 18, tzinfo=UTC)


def _request():
    return build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="rule_1",
        target_capture_id="capture-target",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采详情页截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"page_type": "content_detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=10),
    )


async def test_reader_requires_pending_request_and_live_leased_session() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": "task-1",
                "agent_run_id": "run-1",
                "finalization_epoch": 3,
                "source_session_id": "session-1",
                "surface_kind": "android",
                "current_capture_id": "capture-current",
                "target_state_id": "capture-target",
                "target_identity": "app:com.example:detail",
            }
        ]
    )
    request = _request()

    result = await PostgresAppTargetedRecaptureContextReader(
        pool=FakePool(connection)
    ).read(request=request)

    assert result is not None
    assert result.surface_kind is SurfaceKind.ANDROID
    assert result.agent_run_id == "run-1"
    query = next(call[1] for call in connection.calls if call[0] == "fetchrow")
    assert "evidence_request.status = 'pending'" in query
    assert "session.lease_expires_at > now()" in query
    assert "task.finalization_epoch = session.finalization_epoch" in query
    assert connection.calls[-1][2] == (request.request_id, request.task_id)


async def test_reader_rejects_web_context() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": "task-1",
                "agent_run_id": "run-1",
                "finalization_epoch": 3,
                "source_session_id": "session-1",
                "surface_kind": "web",
                "current_capture_id": "capture-current",
                "target_state_id": "capture-target",
                "target_identity": "web:https://example.com/detail",
            }
        ]
    )

    result = await PostgresAppTargetedRecaptureContextReader(
        pool=FakePool(connection)
    ).read(request=_request())

    assert result is None
