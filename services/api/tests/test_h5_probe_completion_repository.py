"""H5 Probe 补采挂接恢复读取测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_probe_work import H5ProbeWorkError, H5ProbeWorkOutcome
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.interaction import CaptureKind, SurfaceKind
from services.api.app.infrastructure.db.h5_probe_completion_repository import (
    PostgresH5ProbeCompletionRecoveryReader,
    PostgresH5ProbeEvidencePreparer,
)


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        del args


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


class _Connection:
    def __init__(self) -> None:
        self.query = ""
        self.args: tuple[Any, ...] = ()

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.query = " ".join(query.split())
        self.args = args
        return []


@pytest.mark.parametrize("expired", [True, False])
async def test_probe_attachment_uses_frozen_deadline_before_repeated_detection(expired):
    now = datetime.now(UTC)
    connection = _Connection()
    connection.fetchrow = AsyncMock(return_value={
        "before_capture_id": "before", "result_capture_id": "after",
        "agent_run_id": "run", "session_id": "h5:run",
        "deadline": now + timedelta(seconds=-1 if expired else 30),
        "expires_at": now + timedelta(minutes=10),
    })
    detector = SimpleNamespace(detect=AsyncMock(return_value={"detection_status": "unknown"}))
    finalizer = SimpleNamespace(finalize_capture=AsyncMock())
    preparer = PostgresH5ProbeEvidencePreparer(
        finalizer=finalizer, manifests=SimpleNamespace(), detector=detector, pool=_Pool(connection),
    )
    with pytest.raises(H5ProbeWorkError) as failure:
        await preparer.prepare(task_id="task", outcome=H5ProbeWorkOutcome(
            claimed=True, work_id="work", evidence_request_id="request", result_capture_id="after",
        ))
    assert failure.value.retryable is (not expired)
    if expired:
        assert failure.value.error_code == "H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED"
        detector.detect.assert_not_awaited()
    finalizer.finalize_capture.assert_not_awaited()


async def test_attachment_timeout_waits_for_current_detection_to_exit():
    from services.api.app.infrastructure.db.h5_probe_completion_repository import (
        _before_attachment_deadline,
    )

    stopped = []

    async def detect():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    with pytest.raises(H5ProbeWorkError) as failure:
        await _before_attachment_deadline(detect, [datetime.now(UTC) + timedelta(milliseconds=20)])
    assert failure.value.retryable is False
    assert stopped == [True]


async def test_expired_attachment_stage_never_starts_operation():
    from services.api.app.infrastructure.db.h5_probe_completion_repository import (
        _before_attachment_deadline,
    )

    operation = AsyncMock()
    with pytest.raises(H5ProbeWorkError) as failure:
        await _before_attachment_deadline(operation, [datetime.now(UTC) - timedelta(seconds=1)])
    assert failure.value.error_code == "H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED"
    operation.assert_not_called()


async def test_attachment_cannot_accept_late_result_after_operation_suppresses_cancellation():
    from services.api.app.infrastructure.db.h5_probe_completion_repository import (
        _before_attachment_deadline,
    )

    stopped = []

    async def detect():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(True)
            return {"detection_status": "recognized"}

    with pytest.raises(H5ProbeWorkError) as failure:
        await _before_attachment_deadline(detect, [datetime.now(UTC) + timedelta(milliseconds=20)])
    assert failure.value.error_code == "H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED"
    assert failure.value.retryable is False
    assert stopped == [True]


async def test_reader_requires_succeeded_web_probe_and_replay_capture() -> None:
    connection = _Connection()
    reader = PostgresH5ProbeCompletionRecoveryReader(pool=_Pool(connection))

    result = await reader.find_next(task_id="task-1", agent_run_id="run-1")

    assert result is None
    assert connection.args == ("task-1", "run-1")
    assert "work.work_payload->>'surface_kind' = 'web'" in connection.query
    assert "lower(task.target_platform) = 'h5'" in connection.query
    assert "request.session_dependency IN ('active_session', 'snapshot')" in connection.query
    assert "work.status = 'succeeded'" in connection.query
    assert "request.status IN ('routed', 'running', 'satisfied')" in connection.query
    assert "exploration_work_id" in connection.query
    assert "result_capture.capture_role = 'probe_result_replay'" in connection.query
    assert "result_capture.agent_run_id = $2::uuid" in connection.query
    assert "supplement.supplement_id IS NULL" in connection.query
    assert "supplement.evidence_input_set_id IS NULL" in connection.query
    assert "'resealed_input_set:'" in connection.query
    assert "substring(work.terminal_payload->>'reason' FROM 20)" in connection.query


@pytest.mark.parametrize("shared_pair_valid", [True, False])
@pytest.mark.parametrize("expired", [False, True])
async def test_shared_completion_revalidates_own_restoration_and_actual_pair(
    shared_pair_valid, expired,
):
    from services.api.tests.test_h5_recapture_coalescing import probe, request

    # 本例验证共享血缘；截止取本例当前时间，避免前置浏览器回归耗尽导入时样本。
    work = probe(request(), deadline=datetime.now(UTC) + timedelta(seconds=-1 if expired else 30))
    connection = _Connection()
    connection.fetchrow = AsyncMock(side_effect=[None, {
        "work_payload": work.to_dict(), "agent_run_id": "run-h5",
        "terminal_payload": {"work_id": work.work_id, "task_id": work.task_id,
                             "reason": "h5_shared_evidence:real-leader"},
    }])
    finalizer = SimpleNamespace(finalize_capture=AsyncMock())
    detector = SimpleNamespace(detect=AsyncMock(return_value={"detection_status": "recognized"}))
    manifests = SimpleNamespace(get_latest_for_capture=AsyncMock(return_value=SimpleNamespace(
        quality_status=EvidenceQualityStatus.PASS)))
    preparer = PostgresH5ProbeEvidencePreparer(
        finalizer=finalizer, detector=detector, manifests=manifests, pool=_Pool(connection),
    )
    outcome = H5ProbeWorkOutcome(
        claimed=True, work_id=work.work_id, evidence_request_id=work.evidence_request.request_id,
        result_capture_id="real-result",
    )
    # 仓储另查真实 OBSERVE、PASS 清单、Run/Epoch 与每个请求的恢复门禁。
    from unittest.mock import patch
    verified = AsyncMock(return_value={
        "before_capture_id": "real-before", "result_capture_id": "real-result",
        "agent_run_id": "run-h5", "session_id": work.session_id,
    } if shared_pair_valid else None)
    with patch(
        "services.api.app.infrastructure.db.h5_shared_probe_repository."
        "PostgresH5SharedProbeEvidenceReader.read_pair", verified,
    ):
        if shared_pair_valid and expired:
            with pytest.raises(H5ProbeWorkError) as failure:
                await preparer.prepare(task_id=work.task_id, outcome=outcome)
            assert failure.value.error_code == "H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED"
            assert failure.value.retryable is False
            detector.detect.assert_not_awaited()
            finalizer.finalize_capture.assert_not_awaited()
        elif shared_pair_valid:
            await preparer.prepare(task_id=work.task_id, outcome=outcome)
            assert detector.detect.await_count == 2
            assert finalizer.finalize_capture.await_count == 2
        else:
            with pytest.raises(H5ProbeWorkError, match="前后证据"):
                await preparer.prepare(task_id=work.task_id, outcome=outcome)
            detector.detect.assert_not_awaited()
    verified.assert_awaited_once_with(work=work, agent_run_id="run-h5",
                                      origin_work_id="real-leader", result_capture_id="real-result")


@pytest.mark.parametrize("failure", [
    None, "lineage", "manifest", "failed", "pending", "detection", "unknown", "unpersisted",
    "recognized_obstruction",
])
async def test_probe_pair_requires_verified_lineage_and_both_pass_manifests(
    failure: str | None,
) -> None:
    connection = _Connection()
    connection.fetchrow = AsyncMock(
        return_value=(
            None
            if failure == "lineage"
            else {
                "before_capture_id": "capture-before",
                "result_capture_id": "capture-result",
                "agent_run_id": "run-1",
                "session_id": "session-1",
            }
        )
    )
    finalizer = SimpleNamespace(finalize_capture=AsyncMock())
    detector = SimpleNamespace(detect=AsyncMock(return_value={"detection_status": "recognized"}))
    events = []

    async def detect(**kwargs):
        events.append(("detect", kwargs["capture"].capture_id))
        if failure in {"detection", "unknown", "unpersisted"}:
            return {
                "detection_status": "unknown" if failure == "unknown" else "error",
                "rule_matching_allowed": False,
            }
        if failure == "recognized_obstruction":
            return {
                "detection_status": "recognized", "page_type": "home",
                "page_state": "popup", "rule_matching_allowed": False,
            }
        return {"detection_status": "recognized"}

    async def finalize(**kwargs):
        events.append(("finalize", kwargs["capture_id"]))

    detector.detect.side_effect = detect
    finalizer.finalize_capture.side_effect = finalize
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            return_value=None
            if failure == "manifest"
            else SimpleNamespace(
                quality_status=(
                    EvidenceQualityStatus.FAIL
                    if failure == "failed"
                    else EvidenceQualityStatus.PENDING
                    if failure == "pending"
                    else EvidenceQualityStatus.PASS
                ),
            ),
        )
    )
    preparer = PostgresH5ProbeEvidencePreparer(
        finalizer=finalizer,
        manifests=manifests,
        detector=detector,
        pool=_Pool(connection),
    )
    outcome = H5ProbeWorkOutcome(
        claimed=True,
        work_id="work-1",
        evidence_request_id="request-1",
        result_capture_id="capture-result",
    )
    if failure and failure != "recognized_obstruction":
        with pytest.raises(H5ProbeWorkError) as error:
            await preparer.prepare(task_id="task-1", outcome=outcome)
        assert error.value.retryable is (
            failure in {"manifest", "pending", "detection", "unknown", "unpersisted"}
        )
    else:
        await preparer.prepare(task_id="task-1", outcome=outcome)
        assert events == [
            ("detect", "capture-before"),
            ("detect", "capture-result"),
            ("finalize", "capture-before"),
            ("finalize", "capture-result"),
        ]
        assert finalizer.finalize_capture.await_count == 2
        assert manifests.get_latest_for_capture.await_count == 2
        assert all(
            "require_quality_pass" not in call.kwargs
            for call in manifests.get_latest_for_capture.await_args_list
        )
    if failure == "lineage":
        finalizer.finalize_capture.assert_not_awaited()
        detector.detect.assert_not_awaited()
    elif failure in {"detection", "unknown", "unpersisted"}:
        finalizer.finalize_capture.assert_not_awaited()
        detector.detect.assert_awaited_once()
        assert error.value.error_code == "H5_PROBE_PAGE_DETECTION_UNAVAILABLE"
    else:
        assert detector.detect.await_count == 2
        detection = detector.detect.await_args.kwargs
        assert detection["task_id"] == "task-1"
        assert detection["agent_run_id"] == "run-1"
        capture = detection["capture"]
        assert capture.capture_id == "capture-result"
        assert capture.session_id == "session-1"
        assert capture.role == "probe_result_replay"
        assert capture.surface_kind is SurfaceKind.WEB
        assert capture.capture_kind is CaptureKind.PAGE
    query, *args = connection.fetchrow.await_args_list[0].args
    assert args == ["task-1", "work-1", "request-1", "capture-result"]
    for fence in (
        "task.target_platform = 'h5'",
        "task.finalization_epoch = work.finalization_epoch",
        "before_capture.agent_run_id = result_capture.agent_run_id",
        "before_capture.action_execution_id = result_capture.action_execution_id",
        "source_capture.agent_run_id = result_capture.agent_run_id",
        "work.status = 'succeeded'",
        "work.work_payload->>'surface_kind' = 'web'",
    ):
        assert fence in query
