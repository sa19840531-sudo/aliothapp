"""App Probe 成功后的补采证据挂接测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.app_exploration_turn import AppExplorationTurnOutcome
from services.api.app.application.app_probe_completion import (
    AppProbeCompletionError,
    AppProbeCompletionService,
)
from services.api.app.application.evidence_requests import (
    EvidenceRequestStatus,
    StoredEvidenceRequest,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.domain.inspection import EvidenceSourceGrade

NOW = datetime(2026, 9, 8, 19, tzinfo=UTC)


def _outcome() -> AppExplorationTurnOutcome:
    return AppExplorationTurnOutcome(
        claimed=True,
        work_id="probe-work-1",
        work_type="probe",
        final_status=ExplorerWorkStatus.SUCCEEDED,
        result_capture_id="capture-result",
        evidence_request_id="request-1",
        restoration_attempt_id="restoration-attempt-1",
        equivalence_assessment_id="assessment-1",
        source_grade=EvidenceSourceGrade.B,
    )


def _stored(*, status: EvidenceRequestStatus, row_version: int = 2):
    return StoredEvidenceRequest(
        request=SimpleNamespace(request_id="request-1", task_id="task-1"),
        status=status,
        row_version=row_version,
    )


async def test_probe_completion_attaches_pass_manifest_after_marking_request_running() -> None:
    routed = _stored(status=EvidenceRequestStatus.ROUTED)
    running = _stored(status=EvidenceRequestStatus.RUNNING, row_version=3)
    requests = SimpleNamespace(
        get=AsyncMock(side_effect=[routed, running]),
        transition=AsyncMock(return_value=3),
    )
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            return_value=SimpleNamespace(manifest_id="manifest-pass-1")
        )
    )
    attacher = SimpleNamespace(
        attach=AsyncMock(return_value=SimpleNamespace(supplement=SimpleNamespace()))
    )
    service = AppProbeCompletionService(
        requests=requests,
        manifests=manifests,
        attacher=attacher,
    )

    result = await service.complete(task_id="task-1", outcome=_outcome())

    assert result.attached is True
    manifests.get_latest_for_capture.assert_awaited_once_with(
        task_id="task-1",
        capture_id="capture-result",
        require_quality_pass=True,
    )
    transition = requests.transition.await_args.kwargs["command"]
    assert transition.expected_status is EvidenceRequestStatus.ROUTED
    assert transition.target_status is EvidenceRequestStatus.RUNNING
    command = attacher.attach.await_args.args[0]
    assert command.task_id == "task-1"
    assert command.request_id == "request-1"
    assert command.manifest_id == "manifest-pass-1"
    assert command.probe_work_id == "probe-work-1"
    assert command.restoration_attempt_id == "restoration-attempt-1"
    assert command.equivalence_assessment_id == "assessment-1"
    assert command.source_grade is EvidenceSourceGrade.B


async def test_probe_completion_does_not_advance_request_without_pass_manifest() -> None:
    requests = SimpleNamespace(get=AsyncMock(), transition=AsyncMock())
    manifests = SimpleNamespace(get_latest_for_capture=AsyncMock(return_value=None))
    attacher = SimpleNamespace(attach=AsyncMock())
    service = AppProbeCompletionService(
        requests=requests,
        manifests=manifests,
        attacher=attacher,
    )

    with pytest.raises(AppProbeCompletionError, match="PASS Manifest") as error:
        await service.complete(task_id="task-1", outcome=_outcome())

    assert error.value.retryable is True
    requests.get.assert_not_awaited()
    requests.transition.assert_not_awaited()
    attacher.attach.assert_not_awaited()


async def test_probe_completion_finalizes_pending_manifest_before_attach() -> None:
    running = _stored(status=EvidenceRequestStatus.RUNNING)
    requests = SimpleNamespace(get=AsyncMock(return_value=running), transition=AsyncMock())
    pending_manifest = SimpleNamespace(manifest_id="manifest-pending-1")
    pass_manifest = SimpleNamespace(manifest_id="manifest-pass-1")
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            side_effect=[None, pending_manifest, pass_manifest]
        )
    )
    finalizer = SimpleNamespace(
        finalize=AsyncMock(return_value=SimpleNamespace(manifest=pass_manifest))
    )
    attacher = SimpleNamespace(
        attach=AsyncMock(return_value=SimpleNamespace(supplement=SimpleNamespace()))
    )
    service = AppProbeCompletionService(
        requests=requests,
        manifests=manifests,
        attacher=attacher,
        manifest_finalizer=finalizer,
    )

    result = await service.complete(task_id="task-1", outcome=_outcome())

    assert result.manifest_id == "manifest-pass-1"
    finalizer.finalize.assert_awaited_once_with(pending_manifest)
    assert manifests.get_latest_for_capture.await_args_list[1].kwargs == {
        "task_id": "task-1",
        "capture_id": "capture-result",
        "require_quality_pass": False,
    }
    attacher.attach.assert_awaited_once()


async def test_probe_completion_replays_attach_for_running_request_without_transition() -> None:
    running = _stored(status=EvidenceRequestStatus.RUNNING)
    requests = SimpleNamespace(get=AsyncMock(return_value=running), transition=AsyncMock())
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            return_value=SimpleNamespace(manifest_id="manifest-pass-1")
        )
    )
    attacher = SimpleNamespace(
        attach=AsyncMock(return_value=SimpleNamespace(supplement=SimpleNamespace()))
    )
    service = AppProbeCompletionService(
        requests=requests,
        manifests=manifests,
        attacher=attacher,
    )

    result = await service.complete(task_id="task-1", outcome=_outcome())

    assert result.attached is True
    requests.transition.assert_not_awaited()
    attacher.attach.assert_awaited_once()


async def test_probe_completion_rejects_incomplete_probe_lineage() -> None:
    outcome = AppExplorationTurnOutcome(
        claimed=True,
        work_id="probe-work-1",
        work_type="probe",
        final_status=ExplorerWorkStatus.SUCCEEDED,
        result_capture_id="capture-result",
        evidence_request_id="request-1",
    )
    service = AppProbeCompletionService(
        requests=SimpleNamespace(),
        manifests=SimpleNamespace(),
        attacher=SimpleNamespace(),
    )

    with pytest.raises(AppProbeCompletionError, match="证明血缘") as error:
        await service.complete(task_id="task-1", outcome=outcome)

    assert error.value.retryable is False


async def test_probe_completion_recovers_persisted_success_after_process_interruption() -> None:
    running = _stored(status=EvidenceRequestStatus.RUNNING)
    requests = SimpleNamespace(get=AsyncMock(return_value=running), transition=AsyncMock())
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            return_value=SimpleNamespace(manifest_id="manifest-pass-1")
        )
    )
    attacher = SimpleNamespace(
        attach=AsyncMock(return_value=SimpleNamespace(supplement=SimpleNamespace()))
    )
    recovery = SimpleNamespace(find_next=AsyncMock(return_value=_outcome()))
    service = AppProbeCompletionService(
        requests=requests,
        manifests=manifests,
        attacher=attacher,
        recovery=recovery,
    )

    result = await service.recover_next(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    assert result is not None
    assert result.manifest_id == "manifest-pass-1"
    recovery.find_next.assert_awaited_once_with(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
    )
    attacher.attach.assert_awaited_once()
