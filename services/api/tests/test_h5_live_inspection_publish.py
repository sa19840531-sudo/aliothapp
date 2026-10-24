"""H5 活会话增量质检发布：任务门禁、可判定过滤与单点失败隔离。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionService,
)
from services.api.app.application.h5_evidence_handoff import H5EvidenceCapture
from services.api.app.application.h5_evidence_inventory import H5EvidenceArtifactRef
from services.api.app.application.h5_live_inspection_publish import (
    H5ActiveSessionDrainState,
    H5ActiveSessionInspectionDrainService,
    H5LiveInspectionPublishService,
    H5LiveInspectionTaskGate,
    PostgresH5ActiveSessionInspectionStateReader,
    PostgresH5InspectionEvidenceWaitRepository,
    build_default_h5_active_session_inspection_drain_service,
)
from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
)
from services.api.app.application.supplemental_evidence_policy import (
    build_h5_supplemental_evidence_policy,
    build_lab_supplemental_evidence_policy,
)
from services.api.app.core.config import Settings
from services.api.app.domain.exploration import ExplorationBudget
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    H5ClaimedEvidenceRequestReference,
)

pytestmark = pytest.mark.asyncio

_TASK_ID = "22222222-2222-2222-2222-222222222222"
_RUN_ID = "33333333-3333-3333-3333-333333333333"


async def test_h5_active_session_composition_admits_without_compatibility_route() -> None:
    repository = object()
    policy = build_lab_supplemental_evidence_policy()

    service = build_default_h5_active_session_inspection_drain_service(
        worker_instance_id="worker-1",
        settings=Settings.model_validate({"environment": "test"}),
        supplemental_evidence_policy=policy,
        evidence_request_repository=repository,
    )

    writer = service._consumer._result_writer
    probe_publisher = writer._orchestrator._workflow._probe_publisher
    assert isinstance(writer._request_admission, EvidenceRequestAdmissionService)
    assert writer._request_admission._repository is repository
    assert writer._request_admission._policy is policy
    assert writer._route_after_admission is False
    assert probe_publisher._repository._supplemental_evidence_policy is policy
    assert probe_publisher._supplemental_evidence_policy is policy
    assert writer._supplemental_evidence_policy is policy


@pytest.mark.parametrize("formal_h5", [False, True])
async def test_h5_active_session_composition_wires_h5_owned_recapture(formal_h5: bool) -> None:
    detector = object()
    class CaptureRuntime:
        session_command_gate = object()
        surface_sessions = object()
        h5_capture_recovery = object()
        trusted_ui_snapshot_reader = object()

        def build_h5_probe_coordinator(self, **kwargs: Any) -> object:
            assert kwargs["supplemental_evidence_policy"].runtime_ready is True
            return object()

    service = build_default_h5_active_session_inspection_drain_service(
        worker_instance_id="worker-1",
        settings=Settings.model_validate({"environment": "test"}),
        supplemental_evidence_policy=(
            build_h5_supplemental_evidence_policy(enabled=True, h5_v2_mode="on")
            if formal_h5 else build_lab_supplemental_evidence_policy()
        ),
        capture_runtime=CaptureRuntime(),
        detector=detector,
        recapture_budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=3,
            max_depth=2,
            remaining_duration_ms=180_000,
        ),
    )

    assert service._targeted_recapture_drain is not None
    assert service._probe_drain is not None
    assert (service._probe_drain._runner._session_command_gate
            is CaptureRuntime.session_command_gate)
    assert (service._targeted_recapture_drain._consumer._session_command_gate
            is CaptureRuntime.session_command_gate)
    assert service._targeted_recapture_drain._consumer._lease_seconds == 240
    runtime_factory = service._targeted_recapture_drain._consumer._runtime_factory
    from services.api.app.application.h5_closed_session_recapture import (
        drain_h5_closed_session_snapshots,
    )
    assert callable(runtime_factory._contexts.read_snapshot_replay_context)
    assert runtime_factory._closed_session_replay is drain_h5_closed_session_snapshots
    planning = runtime_factory.build(agent_run_id=_RUN_ID, finalization_epoch=0).planning
    assert planning._new_session_runtime is None
    assert planning._closed_session_replay is drain_h5_closed_session_snapshots
    assert runtime_factory._manifest_finalizer is not None
    assert service._probe_drain._completion._consumption._input_set_resealer is not None
    assert service._probe_drain._completion._evidence_preparation._detector is detector
    probe_adapter = service._probe_drain._runner._turn_executor._explorer
    assert probe_adapter._goal_planner is not None
    assert probe_adapter._goal_planner._frontiers is runtime_factory._recovery_runtime
    assert probe_adapter._goal_planner._snapshot_reader is CaptureRuntime.trusted_ui_snapshot_reader
    probe_repository = runtime_factory._probe_orchestrator._workflow._probe_publisher._repository
    assert isinstance(
        probe_repository._evidence_requests,
        H5ClaimedEvidenceRequestReference,
    )


def _capture(
    *,
    capture_id: str,
    capture_role: str = "after_action",
    detection_status: str | None = "recognized",
    page_type: str | None = "video_list",
    page_state: str | None = "stable",
    inspection_status: str | None = "captured",
    session_status: str = "active",
    task_finalization_state: str = "open",
    session_finalization_epoch: int = 0,
    task_finalization_epoch: int = 0,
    supplemental_evidence_source: str | None = None,
    logical_state_previously_published: bool = False,
    logical_identity_fingerprint: str | None = None,
    logical_state_fingerprint: str | None = None,
    rule_matching_allowed: bool = True,
    url: str = "https://m.example.test/list",
    artifact_refs: tuple[H5EvidenceArtifactRef, ...] = (),
) -> H5EvidenceCapture:
    return H5EvidenceCapture(
        capture_id=capture_id,
        inspection_ready=True,
        page_type=page_type,
        page_state=page_state,
        detection_status=detection_status,
        capture_role=capture_role,
        url=url,
        action_execution_id=f"action-{capture_id}",
        parent_capture_id=f"before-{capture_id}",
        before_url="https://m.example.test/home",
        inspection_status=inspection_status,
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id=f"h5:{_RUN_ID}",
        session_owner="h5-workflow",
        session_status=session_status,
        task_finalization_state=task_finalization_state,
        session_finalization_epoch=session_finalization_epoch,
        task_finalization_epoch=task_finalization_epoch,
        supplemental_evidence_source=supplemental_evidence_source,
        logical_state_previously_published=logical_state_previously_published,
        logical_identity_fingerprint=logical_identity_fingerprint,
        logical_state_fingerprint=logical_state_fingerprint,
        rule_matching_allowed=rule_matching_allowed,
        artifact_refs=artifact_refs,
    )


def _artifact_ref(
    *,
    artifact_type: str,
    checksum: str,
    lifecycle_status: str = "active",
) -> H5EvidenceArtifactRef:
    return H5EvidenceArtifactRef(
        artifact_id=f"artifact-{artifact_type}-{checksum}",
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        capture_id="capture",
        storage_path=f"evidence/{artifact_type}",
        artifact_type=artifact_type,
        checksum=checksum,
        size_bytes=10,
        lifecycle_status=lifecycle_status,
    )


class _GateReader:
    def __init__(self, gate: H5LiveInspectionTaskGate | None) -> None:
        self.gate = gate
        self.calls: list[str] = []

    async def load_task_gate(self, *, task_id: str) -> H5LiveInspectionTaskGate | None:
        self.calls.append(task_id)
        return self.gate


class _CaptureReader:
    def __init__(self, captures: list[H5EvidenceCapture]) -> None:
        self.captures = captures
        self.calls: list[tuple[str, str]] = []

    async def list_judgeable_captures(
        self, *, task_id: str, agent_run_id: str
    ) -> list[H5EvidenceCapture]:
        self.calls.append((task_id, agent_run_id))
        return self.captures


class _Acquire:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def __aenter__(self) -> Any:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        del args


class _Pool:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


class _DrainConnection:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.calls.append((query, args))
        return self._row


class _EvidenceWaitConnection:
    def __init__(self, parked_count: int) -> None:
        self._parked_count = parked_count
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> int:
        self.calls.append((query, args))
        return self._parked_count


@dataclass(frozen=True)
class _PublishResult:
    published_count: int
    items: tuple[dict[str, Any], ...] = ()


class _Publisher:
    def __init__(
        self,
        *,
        fail_capture_ids: frozenset[str] = frozenset(),
        published_count: int = 2,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_capture_ids = fail_capture_ids
        self._published_count = published_count

    async def publish_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        visual_page_type: str | None,
        page_state: str = "stable",
        agent_run_id: str | None = None,
        snapshot_only: bool = False,
        active_session_only: bool = False,
    ) -> _PublishResult:
        self.calls.append(
            {
                "task_id": task_id,
                "capture_id": capture_id,
                "visual_page_type": visual_page_type,
                "page_state": page_state,
                "agent_run_id": agent_run_id,
                "snapshot_only": snapshot_only,
                "active_session_only": active_session_only,
            }
        )
        if capture_id in self._fail_capture_ids:
            raise RuntimeError("任务锁定规则集快照不存在")
        return _PublishResult(published_count=self._published_count)


def _h5_gate(
    execution_mode: str = "full_inspection",
    notification_mode: str = "off",
) -> H5LiveInspectionTaskGate:
    return H5LiveInspectionTaskGate(
        target_platform="h5",
        task_config={
            "execution_mode": execution_mode,
            "workflow_version": "v2",
            "h5_inspection_notification_mode": notification_mode,
        },
    )


def _service(
    gate: H5LiveInspectionTaskGate | None,
    captures: list[H5EvidenceCapture],
    publisher: _Publisher | None = None,
) -> tuple[H5LiveInspectionPublishService, _GateReader, _CaptureReader, _Publisher]:
    gate_reader = _GateReader(gate)
    capture_reader = _CaptureReader(captures)
    resolved_publisher = publisher or _Publisher()

    class WebIdentityReader:
        async def read_for_run(self, **kwargs):
            from services.api.app.domain.page.web_url_identity import (
                DEFAULT_WEB_URL_IDENTITY_POLICY,
            )

            return {
                capture.capture_id: {
                    "logical_page_id": DEFAULT_WEB_URL_IDENTITY_POLICY.logical_id(capture.url),
                    "logical_page_identity_status": "verified",
                }
                for capture in captures
            }

    service = H5LiveInspectionPublishService(
        gate_reader=gate_reader,
        capture_reader=capture_reader,
        publisher=resolved_publisher,
        web_identity_reader=WebIdentityReader() if gate and gate.target_platform == "web" else None,
    )
    return service, gate_reader, capture_reader, resolved_publisher


async def test_publishes_pending_judgeable_captures_with_live_session_rules() -> None:
    service, _gate, reader, publisher = _service(
        _h5_gate(),
        [
            _capture(capture_id="cap-1"),
            _capture(capture_id="cap-2", page_state=None),
        ],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 4
    assert reader.calls == [(_TASK_ID, _RUN_ID)]
    assert [call["capture_id"] for call in publisher.calls] == ["cap-1", "cap-2"]
    assert all(call["snapshot_only"] is False for call in publisher.calls)
    assert all(call["active_session_only"] is False for call in publisher.calls)
    assert all(call["agent_run_id"] == _RUN_ID for call in publisher.calls)
    assert publisher.calls[1]["page_state"] == "stable"


async def test_drain_state_counts_nonterminal_evidence_requests_in_same_run() -> None:
    next_attempt_at = datetime(2026, 9, 10, tzinfo=UTC)
    connection = _DrainConnection(
        {
            "nonterminal_work_item_count": 1,
            "exhausted_work_item_count": 1,
            "nonterminal_evidence_request_count": 2,
            "next_attempt_at": next_attempt_at,
        }
    )
    reader = PostgresH5ActiveSessionInspectionStateReader(
        capture_reader=_CaptureReader([]),
        pool=_Pool(connection),
    )

    state = await reader.load_drain_state(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert state == H5ActiveSessionDrainState(
        pending_capture_count=0,
        nonterminal_work_item_count=1,
        exhausted_work_item_count=1,
        nonterminal_evidence_request_count=2,
        next_attempt_at=next_attempt_at,
    )
    query, args = connection.calls[0]
    assert "FROM qc_evidence_request AS request" in query
    assert "work_item.attempt_count >= work_item.max_attempts" in query
    assert "work_item.status <> 'waiting_evidence'" in query
    assert "inflight.work_item_id = work_item.id" in query
    assert "capture.agent_run_id = $2::uuid" in query
    assert "request.finalization_epoch = task.finalization_epoch" in query
    assert args == (_TASK_ID, _RUN_ID)


async def test_h5_evidence_wait_parks_only_current_run_with_open_request() -> None:
    connection = _EvidenceWaitConnection(1)
    repository = PostgresH5InspectionEvidenceWaitRepository(
        pool=_Pool(connection),
    )

    parked = await repository.park_open_requests(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
    )

    assert parked == 1
    query, args = connection.calls[0]
    assert "lower(task.target_platform) = 'h5'" in query
    assert "work_item.producer_agent_run_id = $2::uuid" in query
    assert "work_item.session_owner_agent_run_id = $2::uuid" in query
    assert "work_item.status = 'retry_wait'" in query
    assert "status = 'waiting_evidence'" in query
    assert "next_attempt_at = NULL" not in query
    assert "request.status IN ('pending', 'routed', 'running')" in query
    assert args == (_TASK_ID, _RUN_ID)


async def test_h5_live_publish_batch_skips_ineligible_inventory_before_advancing_cursor() -> None:
    service, _gate, _reader, publisher = _service(
        _h5_gate(),
        [
            _capture(capture_id="cap-before", capture_role="before_action"),
            _capture(capture_id="cap-1"),
            _capture(capture_id="cap-2"),
            _capture(capture_id="cap-3"),
        ],
    )

    first = await service.publish_pending_batch(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        limit=1,
        cursor=None,
    )
    second = await service.publish_pending_batch(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        limit=1,
        cursor=first.next_cursor,
    )

    assert (first.attempted_count, first.created_count, first.has_more) == (1, 2, True)
    assert (second.attempted_count, second.created_count, second.has_more) == (1, 2, True)
    assert [call["capture_id"] for call in publisher.calls] == ["cap-1", "cap-2"]


async def test_h5_completed_inventory_does_not_requeue_empty_publication_passes():
    service, _gate, _reader, publisher = _service(
        _h5_gate(),
        [_capture(capture_id=f"cap-{index}", inspection_status="completed")
         for index in range(100)],
    )
    result = await service.publish_pending_batch(
        task_id=_TASK_ID, agent_run_id=_RUN_ID, limit=1, cursor=None,
    )
    assert result == LiveInspectionPublicationBatchResult(0, 0, False)
    assert publisher.calls == []


async def test_web_live_publish_does_not_resolve_identity_for_consumer_run_without_captures() -> (
    None
):
    """通用消费 Run 无 Web Capture 时不得误读其策略，主 Run 由 Web 适配器另行发布。"""

    class UnexpectedIdentityReader:
        async def read_for_run(self, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("无 Capture 的规则消费 Run 不得读取 Web 策略身份")

    capture_reader = _CaptureReader([])
    service = H5LiveInspectionPublishService(
        gate_reader=_GateReader(
            H5LiveInspectionTaskGate(
                target_platform="web",
                task_config={
                    "execution_mode": "full_inspection",
                    "workflow_version": "v2",
                    "web_inspection_notification_mode": "enforced",
                },
            )
        ),
        capture_reader=capture_reader,
        publisher=_Publisher(),
        web_identity_reader=UnexpectedIdentityReader(),
    )

    result = await service.publish_pending_batch(
        task_id=_TASK_ID,
        agent_run_id="inspection-consumer-run",
        limit=1,
        cursor=None,
    )

    assert result == LiveInspectionPublicationBatchResult(0, 0, False)
    assert capture_reader.calls == [(_TASK_ID, "inspection-consumer-run")]


async def test_skips_captures_that_are_not_live_publishable() -> None:
    service, _gate, _reader, publisher = _service(
        _h5_gate(),
        [
            _capture(capture_id="cap-before", capture_role="before_action"),
            _capture(capture_id="cap-undetected", detection_status=None),
            _capture(capture_id="cap-no-page-type", page_type=None),
            _capture(capture_id="cap-published", inspection_status="evaluating"),
            _capture(capture_id="cap-completed", inspection_status="completed"),
            _capture(capture_id="cap-fresh", inspection_status=None),
            _capture(capture_id="cap-ok"),
        ],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 4
    assert [call["capture_id"] for call in publisher.calls] == ["cap-fresh", "cap-ok"]


async def test_h5_live_publish_skips_targeted_recapture_capture() -> None:
    service, _gate, _reader, publisher = _service(
        _h5_gate(),
        [
            _capture(capture_id="cap-normal"),
            _capture(
                capture_id="cap-restored",
                supplemental_evidence_source="targeted_recapture",
            ),
        ],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert [call["capture_id"] for call in publisher.calls] == ["cap-normal"]


async def test_h5_live_publish_honors_page_detection_rule_matching_gate() -> None:
    service, _gate, _reader, publisher = _service(
        _h5_gate(),
        [
            _capture(capture_id="cap-judgeable"),
            _capture(
                capture_id="cap-entitlement-locked",
                page_type="video_detail",
                page_state="entitlement_locked",
                rule_matching_allowed=False,
            ),
        ],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert [call["capture_id"] for call in publisher.calls] == ["cap-judgeable"]


async def test_h5_live_publish_skips_previously_published_logical_state() -> None:
    service, _gate, _reader, publisher = _service(
        _h5_gate(),
        [
            _capture(capture_id="cap-first"),
            _capture(
                capture_id="cap-duplicate",
                logical_state_previously_published=True,
            ),
        ],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert [call["capture_id"] for call in publisher.calls] == ["cap-first"]


async def test_h5_live_publish_deduplicates_same_state_in_one_batch() -> None:
    service, _gate, _reader, publisher = _service(
        _h5_gate(),
        [
            _capture(
                capture_id="cap-first",
                logical_identity_fingerprint="sha256:identity",
                logical_state_fingerprint="sha256:state",
            ),
            _capture(
                capture_id="cap-duplicate",
                logical_identity_fingerprint="sha256:identity",
                logical_state_fingerprint="sha256:state",
            ),
        ],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert [call["capture_id"] for call in publisher.calls] == ["cap-first"]


async def test_h5_live_publish_retries_same_state_after_first_capture_fails() -> None:
    publisher = _Publisher(fail_capture_ids=frozenset({"cap-first"}))
    service, _gate, _reader, _publisher = _service(
        _h5_gate(),
        [
            _capture(
                capture_id="cap-first",
                logical_identity_fingerprint="sha256:identity",
                logical_state_fingerprint="sha256:state",
            ),
            _capture(
                capture_id="cap-retry",
                logical_identity_fingerprint="sha256:identity",
                logical_state_fingerprint="sha256:state",
            ),
        ],
        publisher=publisher,
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert [call["capture_id"] for call in publisher.calls] == [
        "cap-first",
        "cap-retry",
    ]


async def test_web_live_publish_ignores_h5_logical_state_projection() -> None:
    artifact_refs = (
        _artifact_ref(artifact_type="dom", checksum="dom-v1"),
        _artifact_ref(artifact_type="visible_text", checksum="text-v1"),
    )
    service, _gate, _reader, publisher = _service(
        H5LiveInspectionTaskGate(
            target_platform="web",
            task_config={"execution_mode": "full_inspection"},
        ),
        [
            _capture(
                capture_id="cap-web",
                logical_state_previously_published=True,
                rule_matching_allowed=False,
                artifact_refs=artifact_refs,
            )
        ],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert [call["capture_id"] for call in publisher.calls] == ["cap-web"]


@pytest.mark.parametrize(
    "capture",
    [
        _capture(capture_id="cap-closed", session_status="closed"),
        _capture(capture_id="cap-draining", task_finalization_state="session_draining"),
        _capture(capture_id="cap-old-epoch", session_finalization_epoch=1),
    ],
)
async def test_live_publish_requires_active_current_epoch_session(
    capture: H5EvidenceCapture,
) -> None:
    service, _gate, _reader, publisher = _service(_h5_gate(), [capture])

    assert await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID) == 0
    assert publisher.calls == []


async def test_non_h5_task_publishes_nothing() -> None:
    service, gate_reader, reader, publisher = _service(
        H5LiveInspectionTaskGate(
            target_platform="app",
            task_config={"execution_mode": "full_inspection"},
        ),
        [_capture(capture_id="cap-1")],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 0
    assert gate_reader.calls == [_TASK_ID]
    assert reader.calls == []
    assert publisher.calls == []


async def test_exploration_only_task_publishes_nothing() -> None:
    service, _gate, reader, publisher = _service(
        _h5_gate(execution_mode="exploration_only"),
        [_capture(capture_id="cap-1")],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 0
    assert reader.calls == []
    assert publisher.calls == []


async def test_shadow_notification_mode_never_publishes_formal_live_work() -> None:
    service, _gate, reader, publisher = _service(
        _h5_gate(notification_mode="shadow"),
        [_capture(capture_id="cap-1")],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 0
    assert reader.calls == []
    assert publisher.calls == []


async def test_enforced_h5_publishes_snapshot_rules_before_browser_close() -> None:
    service, _gate, _reader, publisher = _service(
        _h5_gate(notification_mode="enforced"),
        [_capture(capture_id="cap-1")],
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert publisher.calls == [
        {
            "task_id": _TASK_ID,
            "capture_id": "cap-1",
            "visual_page_type": "video_list",
            "page_state": "stable",
            "agent_run_id": _RUN_ID,
            "snapshot_only": False,
            "active_session_only": False,
        }
    ]


async def test_missing_task_publishes_nothing() -> None:
    service, _gate, reader, publisher = _service(None, [_capture(capture_id="cap-1")])

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 0
    assert reader.calls == []
    assert publisher.calls == []


async def test_single_capture_failure_does_not_block_other_captures() -> None:
    publisher = _Publisher(fail_capture_ids=frozenset({"cap-broken"}))
    service, _gate, _reader, _publisher = _service(
        _h5_gate(),
        [
            _capture(capture_id="cap-broken"),
            _capture(capture_id="cap-ok"),
        ],
        publisher=publisher,
    )

    published = await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID)

    assert published == 2
    assert [call["capture_id"] for call in publisher.calls] == ["cap-broken", "cap-ok"]


async def test_web_failed_version_can_retry_same_version_replay() -> None:
    artifact_refs = (
        _artifact_ref(artifact_type="dom", checksum="dom-v1"),
        _artifact_ref(artifact_type="visible_text", checksum="text-v1"),
    )
    publisher = _Publisher(
        fail_capture_ids=frozenset({"cap-broken"}),
        published_count=1,
    )
    service, _gate, _reader, _publisher = _service(
        H5LiveInspectionTaskGate(
            target_platform="web",
            task_config={"execution_mode": "full_inspection"},
        ),
        [
            _capture(capture_id="cap-broken", artifact_refs=artifact_refs),
            _capture(capture_id="cap-replay", artifact_refs=artifact_refs),
        ],
        publisher=publisher,
    )

    published = await service.publish_pending(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        limit=None,
    )

    assert published == 1
    assert [call["capture_id"] for call in publisher.calls] == [
        "cap-broken",
        "cap-replay",
    ]


async def test_web_nonformal_artifacts_do_not_change_content_version() -> None:
    formal_refs = (
        _artifact_ref(artifact_type="dom", checksum="dom-v1"),
        _artifact_ref(
            artifact_type="visible_text",
            checksum="text-v1",
            lifecycle_status="archived",
        ),
    )
    replay_refs = formal_refs + (
        _artifact_ref(
            artifact_type="dom",
            checksum="dom-pending",
            lifecycle_status="pending",
        ),
        _artifact_ref(
            artifact_type="visible_text",
            checksum="text-deleted",
            lifecycle_status="deleted",
        ),
    )
    publisher = _Publisher(published_count=1)
    service, _gate, _reader, _publisher = _service(
        H5LiveInspectionTaskGate(
            target_platform="web",
            task_config={"execution_mode": "full_inspection"},
        ),
        [
            _capture(capture_id="cap-first", artifact_refs=formal_refs),
            _capture(capture_id="cap-replay", artifact_refs=replay_refs),
        ],
        publisher=publisher,
    )

    published = await service.publish_pending(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        limit=None,
    )

    assert published == 1
    assert [call["capture_id"] for call in publisher.calls] == ["cap-first"]


async def test_web_successful_version_skips_same_version_replay() -> None:
    artifact_refs = (
        _artifact_ref(artifact_type="dom", checksum="dom-v1"),
        _artifact_ref(artifact_type="visible_text", checksum="text-v1"),
    )
    publisher = _Publisher(published_count=1)
    service, _gate, _reader, _publisher = _service(
        H5LiveInspectionTaskGate(
            target_platform="web",
            task_config={"execution_mode": "full_inspection"},
        ),
        [
            _capture(capture_id="cap-first", artifact_refs=artifact_refs),
            _capture(capture_id="cap-replay", artifact_refs=artifact_refs),
        ],
        publisher=publisher,
    )

    published = await service.publish_pending(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        limit=None,
    )

    assert published == 1
    assert [call["capture_id"] for call in publisher.calls] == ["cap-first"]


async def test_live_publish_count_uses_newly_created_work_items() -> None:
    class ReplayPublisher(_Publisher):
        async def publish_for_capture(self, **kwargs: Any) -> _PublishResult:
            self.calls.append(dict(kwargs))
            return _PublishResult(
                published_count=2,
                items=(
                    {"work_item_id": "work-existing", "created": False},
                    {"work_item_id": "work-new", "created": True},
                ),
            )

    publisher = ReplayPublisher()
    service, _gate, _reader, _publisher = _service(
        _h5_gate(notification_mode="enforced"),
        [_capture(capture_id="cap-1")],
        publisher=publisher,
    )

    assert await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID) == 1


async def test_blank_identifiers_are_rejected() -> None:
    service, _gate, _reader, _publisher = _service(_h5_gate(), [])

    with pytest.raises(ValueError):
        await service.publish_pending(task_id=" ", agent_run_id=_RUN_ID)
    with pytest.raises(ValueError):
        await service.publish_pending(task_id=_TASK_ID, agent_run_id="")


class _LivePublisher:
    def __init__(self, counts: list[int]) -> None:
        self._counts = counts
        self.calls: list[tuple[str, str]] = []

    async def publish_pending(self, *, task_id: str, agent_run_id: str) -> int:
        self.calls.append((task_id, agent_run_id))
        return self._counts.pop(0) if self._counts else 0


class _ActiveConsumer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def drain_available(self, **kwargs: Any) -> object:
        self.calls.append(dict(kwargs))
        return object()


class _DrainStateReader:
    def __init__(self, states: list[H5ActiveSessionDrainState]) -> None:
        self._states = states

    async def load_drain_state(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ActiveSessionDrainState:
        del task_id, agent_run_id
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]


@pytest.mark.parametrize("checkpoint", [True, False])
async def test_h5_past_retry_timestamp_does_not_force_hot_polling(checkpoint):
    current = datetime(2026, 10, 2, tzinfo=UTC)
    clock = {"now": current}
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader([
            H5ActiveSessionDrainState(
                pending_capture_count=0, nonterminal_work_item_count=1,
                next_attempt_at=current - timedelta(seconds=30),
            ),
            H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0),
        ]), now=lambda: clock["now"], sleep=sleep,
    )
    method = service.run_current_surface_checkpoint if checkpoint else service.drain_before_close
    await method(task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=current + timedelta(minutes=1))
    assert sleeps == [5.0]


async def test_h5_stall_grace_includes_consumption_wall_time():
    current = datetime(2026, 10, 2, tzinfo=UTC)
    clock = {"now": current}
    calls = []

    class Consumer:
        async def drain_available(self, **kwargs):
            calls.append(kwargs)
            clock["now"] += timedelta(seconds=10)
            return SimpleNamespace(claimed_count=0)

    async def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)

    settlement = SimpleNamespace(settle_stalled_work_for_closure=AsyncMock(return_value=
        SimpleNamespace(settled_work_item_count=1, settled_rule_count=1)))
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=Consumer(),
        state_reader=_DrainStateReader([H5ActiveSessionDrainState(
            pending_capture_count=0, nonterminal_work_item_count=1,
            next_attempt_at=current - timedelta(seconds=30),
        )]), grace_seconds=15, max_wait_seconds=100,
        now=lambda: clock["now"], sleep=sleep, closure_settlement=settlement,
    )
    result = await service.drain_before_close(
        task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=current + timedelta(minutes=2),
    )
    assert result.settled_work_item_count == 1
    assert len(calls) <= 3
    assert (clock["now"] - current).total_seconds() <= 40


@pytest.mark.parametrize("remaining_snapshot,blocked", [
    (0, None), (1, None), (0, "active"), (0, "request"), (0, "capture"),
])
async def test_close_reloads_snapshot_state_after_closure_settlement(remaining_snapshot, blocked):
    current = datetime(2026, 10, 8, tzinfo=UTC)
    clock = {"now": current}
    settled = False

    class Reader:
        async def load_drain_state(self, **kwargs):
            return H5ActiveSessionDrainState(
                pending_capture_count=int(settled and blocked == "capture"),
                nonterminal_work_item_count=int(blocked == "active") if settled else 1,
                nonterminal_evidence_request_count=int(settled and blocked == "request"),
                snapshot_work_item_count=remaining_snapshot if settled else 1,
            )

    async def settle(**kwargs):
        nonlocal settled
        settled = True
        return SimpleNamespace(settled_work_item_count=1, settled_rule_count=1)

    async def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=_ActiveConsumer(), state_reader=Reader(),
        grace_seconds=0.05, max_poll_seconds=0.05, max_wait_seconds=1,
        now=lambda: clock["now"], sleep=sleep,
        closure_settlement=SimpleNamespace(settle_stalled_work_for_closure=settle),
    )
    arguments = dict(task_id=_TASK_ID, agent_run_id=_RUN_ID,
                     deadline=current + timedelta(minutes=1))
    if blocked:
        with pytest.raises(RuntimeError, match="尚未排空"):
            await service.drain_before_close(**arguments)
        assert settled
        return
    result = await service.drain_before_close(**arguments)
    assert settled
    assert result.deferred_snapshot_work_item_count == remaining_snapshot


async def test_close_keeps_h5_session_until_snapshot_judge_and_recapture_are_drained() -> None:
    events: list[str] = []

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            events.append(kwargs["session_dependency"])
            return SimpleNamespace(claimed_count=1)

    class Recapture:
        async def drain_one(self, **kwargs: Any) -> bool:
            assert kwargs["include_snapshot"] is True
            events.append("recapture")
            return False

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=Consumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                    snapshot_work_item_count=1,
                ),
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                    snapshot_evidence_request_count=1,
                ),
                H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0),
            ]
        ),
        targeted_recapture_drain=Recapture(),
        now=lambda: datetime(2026, 9, 11, tzinfo=UTC),
        sleep=AsyncMock(),
    )
    await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert "snapshot" in events
    assert events.index("snapshot") < events.index("recapture")


async def test_snapshot_judge_elapsed_time_counts_toward_close_absolute_limit() -> None:
    clock = {"now": datetime(2026, 9, 11, tzinfo=UTC)}

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            if kwargs["session_dependency"] == "snapshot":
                clock["now"] += timedelta(seconds=20)
            return SimpleNamespace(claimed_count=1)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=Consumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                    snapshot_work_item_count=2,
                ),
            ]
        ),
        max_wait_seconds=10,
        now=lambda: clock["now"],
        sleep=AsyncMock(),
    )
    result = await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert result.deferred_snapshot_work_item_count == 2


@pytest.mark.parametrize("dependency", ["active_session", "snapshot"])
async def test_close_waits_for_owned_or_scheduled_judge_without_false_stall(
    dependency: str,
) -> None:
    clock = {"now": datetime(2026, 10, 2, tzinfo=UTC)}
    sleeps: list[float] = []

    busy = H5ActiveSessionDrainState(
        pending_capture_count=0,
        nonterminal_work_item_count=int(dependency == "active_session"),
        snapshot_work_item_count=int(dependency == "snapshot"),
        scheduled_work_item_count=1,
        next_attempt_at=clock["now"] + timedelta(seconds=9),
    )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader(
            [
                busy,
                busy,
                busy,
                busy,
                H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0),
            ]
        ),
        grace_seconds=1,
        max_wait_seconds=10,
        max_poll_seconds=1,
        now=lambda: clock["now"],
        sleep=sleep,
    )
    result = await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=clock["now"] + timedelta(minutes=1),
    )
    assert result.drain_round_count == 5
    assert sleeps == [1, 1, 1, 1]


async def test_scheduled_snapshot_judge_still_obeys_close_absolute_limit() -> None:
    clock = {"now": datetime(2026, 10, 2, tzinfo=UTC)}

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                    snapshot_work_item_count=1,
                    scheduled_work_item_count=1,
                    next_attempt_at=clock["now"] + timedelta(seconds=9),
                ),
            ]
        ),
        grace_seconds=1,
        max_wait_seconds=4,
        max_poll_seconds=1,
        now=lambda: clock["now"],
        sleep=sleep,
    )
    started = clock["now"]
    result = await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=started + timedelta(minutes=1),
    )
    assert result.deferred_snapshot_work_item_count == 1
    assert (clock["now"] - started).total_seconds() == 4


class _ProbeDrain:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def drain_one(self, *, task_id: str, agent_run_id: str) -> bool:
        self.calls.append((task_id, agent_run_id))
        return True


class _TargetedRecaptureDrain:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.calls: list[tuple[str, str, datetime, bool]] = []

    async def drain_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
        include_snapshot: bool = False,
    ) -> bool:
        self._events.append("recapture")
        self.calls.append((task_id, agent_run_id, deadline, include_snapshot))
        return len(self.calls) == 1


@pytest.mark.parametrize("checkpoint", [True, False])
async def test_h5_drains_existing_probe_before_restoring_another_request(checkpoint):
    events = []

    class Probe:
        async def drain_one(self, **kwargs):
            events.append("execute-existing-probe")
            return True

    class Recapture:
        async def drain_one(self, **kwargs):
            raise AssertionError("旧 Probe 尚在本轮排空，不得先移动其来源 Capture")

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader([H5ActiveSessionDrainState(
            pending_capture_count=0, nonterminal_work_item_count=0,
        )]), targeted_recapture_drain=Recapture(), probe_drain=Probe(),
        now=lambda: datetime(2026, 10, 4, tzinfo=UTC),
    )
    drain = service.run_current_surface_checkpoint if checkpoint else service.drain_before_close
    await drain(task_id=_TASK_ID, agent_run_id=_RUN_ID,
                deadline=datetime(2026, 10, 4, 1, tzinfo=UTC))
    assert events


class _Cancellation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def raise_if_requested(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        stage: str,
    ) -> None:
        self.calls.append((task_id, agent_run_id, stage))
        raise RuntimeError("cancelled")


async def test_current_surface_checkpoint_finishes_before_h5_can_leave_page() -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pending(self, *, task_id: str, agent_run_id: str) -> int:
            assert (task_id, agent_run_id) == (_TASK_ID, _RUN_ID)
            events.append("publish")
            return 1

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            assert kwargs["max_items"] == 1
            events.append("judge")
            return object()

    class Recapture:
        async def drain_one(self, **kwargs: Any) -> bool:
            assert kwargs["deadline"] == datetime(2026, 9, 12, tzinfo=UTC)
            assert kwargs["include_snapshot"] is True
            events.append("recapture")
            return True

    class Probe:
        async def drain_one(self, **kwargs: Any) -> bool:
            del kwargs
            events.append("probe")
            return events.count("probe") > 1

    class CoverageAuthority:
        async def reconcile_available(self, **kwargs: Any) -> int:
            del kwargs
            events.append("coverage")
            return 1

    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=Consumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                )
            ]
        ),
        targeted_recapture_drain=Recapture(),
        probe_drain=Probe(),
        coverage_authority=CoverageAuthority(),
        now=lambda: datetime(2026, 9, 11, 23, 59, tzinfo=UTC),
    )

    summary = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, tzinfo=UTC),
    )

    assert events == [
        "coverage",
        "publish",
        "judge",
        "probe",
        "recapture",
        "probe",
        "coverage",
        "judge",
    ]
    assert summary.published_work_item_count == 1
    assert summary.drain_round_count == 1


async def test_current_surface_checkpoint_drains_requested_snapshot_evidence_before_leaving() -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pending(self, **kwargs: Any) -> int:
            return 0

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            assert kwargs["session_dependency"] == "active_session"
            return object()

    class Recapture:
        async def drain_one(self, **kwargs: Any) -> bool:
            events.append("snapshot" if kwargs.get("include_snapshot") else "active_only")
            return len(events) == 2

    async def sleep(seconds: float) -> None:
        assert seconds > 0

    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=Consumer(),
        state_reader=_DrainStateReader([
            H5ActiveSessionDrainState(
                pending_capture_count=0, nonterminal_work_item_count=0,
                snapshot_evidence_request_count=1, snapshot_work_item_count=9,
            ),
            H5ActiveSessionDrainState(
                pending_capture_count=0, nonterminal_work_item_count=0,
                snapshot_evidence_request_count=1, snapshot_work_item_count=9,
            ),
            H5ActiveSessionDrainState(
                pending_capture_count=0, nonterminal_work_item_count=0,
                snapshot_work_item_count=9,
            ),
        ]),
        targeted_recapture_drain=Recapture(),
        now=lambda: datetime(2026, 9, 11, 23, 59, tzinfo=UTC),
        sleep=sleep,
    )

    summary = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID, agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, tzinfo=UTC),
    )

    assert events == ["snapshot", "snapshot"]
    assert summary.drain_round_count == 2


@pytest.mark.parametrize("recoverable", [True, False])
async def test_checkpoint_retains_recoverable_snapshot_while_active_judge_drains(recoverable):
    recapture = SimpleNamespace(
        can_defer_snapshot_requests=AsyncMock(return_value=recoverable),
        drain_one=AsyncMock(return_value=False),
    )
    consumer = SimpleNamespace(drain_available=AsyncMock(return_value=SimpleNamespace(
        claimed_count=1, completed_count=1,
    )))
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=consumer,
        state_reader=_DrainStateReader([
            H5ActiveSessionDrainState(
                pending_capture_count=0, nonterminal_work_item_count=1,
                snapshot_evidence_request_count=1,
            ),
            H5ActiveSessionDrainState(
                pending_capture_count=0, nonterminal_work_item_count=0,
                snapshot_evidence_request_count=int(recoverable),
            ),
        ]),
        targeted_recapture_drain=recapture,
        now=lambda: datetime(2026, 10, 4, tzinfo=UTC),
    )
    result = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID, agent_run_id=_RUN_ID,
        deadline=datetime(2026, 10, 4, 1, tzinfo=UTC),
    )
    assert result.deferred_snapshot_evidence_request_count == int(recoverable)
    consumer.drain_available.assert_awaited_once()
    assert recapture.drain_one.await_args.kwargs["include_snapshot"] is not recoverable


async def test_current_surface_checkpoint_parks_open_request_before_retry() -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pending(self, **kwargs: Any) -> int:
            del kwargs
            return 0

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            del kwargs
            events.append("judge")
            return SimpleNamespace(claimed_count=1)

    class EvidenceWait:
        async def park_open_requests(self, **kwargs: Any) -> int:
            del kwargs
            events.append("park")
            return 1

    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=Consumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=1,
                ),
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                ),
            ]
        ),
        evidence_wait=EvidenceWait(),
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )

    await service.run_current_surface_checkpoint(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, 0, 5, tzinfo=UTC),
    )

    assert events == ["park", "judge", "park"]


async def test_current_surface_checkpoint_waits_for_external_judge_before_navigation() -> None:
    states = _DrainStateReader(
        [
            H5ActiveSessionDrainState(
                pending_capture_count=0,
                nonterminal_work_item_count=1,
            ),
            H5ActiveSessionDrainState(
                pending_capture_count=0,
                nonterminal_work_item_count=0,
            ),
        ]
    )

    class Publisher:
        calls = 0

        async def publish_pending(self, **kwargs: Any) -> int:
            del kwargs
            self.calls += 1
            return 1 if self.calls == 1 else 0

    class Consumer:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def drain_available(self, **kwargs: Any) -> object:
            self.calls.append(dict(kwargs))
            return SimpleNamespace(claimed_count=0)

    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=Consumer(),
        state_reader=states,
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )

    summary = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, 0, 5, tzinfo=UTC),
    )

    assert summary.published_work_item_count == 1
    assert summary.drain_round_count == 2


@pytest.mark.parametrize("owned_judge", [False, True])
@pytest.mark.parametrize("rounds", [4, 13])
async def test_checkpoint_keeps_progressing_judge_beyond_idle_limit(
    owned_judge: bool, rounds: int,
) -> None:
    started = datetime(2026, 10, 3, tzinfo=UTC)
    clock = {"now": started}
    calls = []

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            calls.append(kwargs)
            clock["now"] += timedelta(seconds=100)
            return SimpleNamespace(
                claimed_count=0 if owned_judge else 1,
                completed_count=0 if owned_judge else 1,
            )

    class StateReader:
        async def load_drain_state(self, **kwargs: Any) -> H5ActiveSessionDrainState:
            return H5ActiveSessionDrainState(
                pending_capture_count=0,
                nonterminal_work_item_count=int(len(calls) < rounds),
                scheduled_work_item_count=int(owned_judge and len(calls) < rounds),
                terminal_fact_count=len(calls) if owned_judge else 0,
            )

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=Consumer(), state_reader=StateReader(),
        checkpoint_max_wait_seconds=300, max_wait_seconds=900,
        now=lambda: clock["now"], sleep=sleep,
    )
    result = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=started + timedelta(hours=1),
    )
    assert result.drain_round_count == rounds
    assert len(calls) == rounds
    assert rounds * 100 <= (clock["now"] - started).total_seconds() < 3600


@pytest.mark.parametrize("scheduled", [False, True])
async def test_checkpoint_stall_and_absolute_limits_remain_bounded(scheduled: bool) -> None:
    started = datetime(2026, 10, 3, tzinfo=UTC)
    clock = {"now": started}
    calls = []

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            calls.append(kwargs)
            clock["now"] += timedelta(seconds=10)
            return SimpleNamespace(claimed_count=0)

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=Consumer(),
        state_reader=_DrainStateReader([H5ActiveSessionDrainState(
            pending_capture_count=0, nonterminal_work_item_count=1,
            scheduled_work_item_count=int(scheduled),
        )]), checkpoint_max_wait_seconds=30, max_wait_seconds=60,
        now=lambda: clock["now"], sleep=sleep,
    )
    with pytest.raises(RuntimeError, match="active-session 质检未排空"):
        await service.run_current_surface_checkpoint(
            task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=started + timedelta(hours=1),
        )
    assert (clock["now"] - started).total_seconds() == 30
    assert len(calls) == 2


async def test_repeated_claim_without_completion_does_not_reset_checkpoint_idle_clock():
    started = datetime(2026, 10, 6, tzinfo=UTC)
    clock = {"now": started}

    class Consumer:
        async def drain_available(self, **kwargs):
            clock["now"] += timedelta(seconds=10)
            return SimpleNamespace(claimed_count=1, completed_count=0)

    async def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=Consumer(),
        state_reader=_DrainStateReader([H5ActiveSessionDrainState(
            pending_capture_count=0, nonterminal_work_item_count=1,
        )]), checkpoint_max_wait_seconds=30, max_wait_seconds=60,
        now=lambda: clock["now"], sleep=sleep,
    )
    with pytest.raises(RuntimeError, match="active-session 质检未排空"):
        await service.run_current_surface_checkpoint(
            task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=started + timedelta(hours=1),
        )
    assert (clock["now"] - started).total_seconds() == 30


@pytest.mark.parametrize("active_request", [False, True])
async def test_only_recoverable_snapshot_can_leave_current_page_and_still_blocks_close(active_request):
    requests = H5ActiveSessionDrainState(
        pending_capture_count=0, nonterminal_work_item_count=0,
        nonterminal_evidence_request_count=int(active_request), snapshot_evidence_request_count=1,
    )
    recapture = SimpleNamespace(
        drain_one=AsyncMock(return_value=False),
        can_defer_snapshot_requests=AsyncMock(return_value=True),
    )
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader([requests]), targeted_recapture_drain=recapture,
        checkpoint_max_wait_seconds=0.05, max_wait_seconds=0.05,
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
        sleep=lambda _delay: _completed_sleep(),
    )
    args = dict(task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=datetime(2026, 9, 12, 0, 5, tzinfo=UTC))
    if active_request:
        with pytest.raises(RuntimeError, match="active-session 质检未排空"):
            await service.run_current_surface_checkpoint(**args)
        recapture.can_defer_snapshot_requests.assert_not_awaited()
    else:
        result = await service.run_current_surface_checkpoint(**args)
        assert result.deferred_snapshot_evidence_request_count == 1
        recapture.can_defer_snapshot_requests.assert_awaited_once()
        recapture.drain_one.assert_not_awaited()
    with pytest.raises(RuntimeError, match="Snapshot 判定或补采尚未排空"):
        await service.drain_before_close(**args)


@pytest.mark.parametrize("snapshot_request", [False, True])
async def test_current_surface_checkpoint_fails_closed_when_request_does_not_drain(
    snapshot_request: bool,
) -> None:
    state = H5ActiveSessionDrainState(
        pending_capture_count=0,
        nonterminal_work_item_count=0,
        nonterminal_evidence_request_count=0 if snapshot_request else 1,
        snapshot_evidence_request_count=1 if snapshot_request else 0,
    )

    class Publisher:
        async def publish_pending(self, **kwargs: Any) -> int:
            del kwargs
            return 0

    class Consumer:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def drain_available(self, **kwargs: Any) -> object:
            self.calls.append(dict(kwargs))
            return SimpleNamespace(claimed_count=0)

    consumer = Consumer()
    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=consumer,
        state_reader=_DrainStateReader([state]),
        checkpoint_max_wait_seconds=0.05,
        max_poll_seconds=0.05,
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
        sleep=lambda _delay: _completed_sleep(),
    )

    expected = (
        "snapshot_evidence_requests=1" if snapshot_request else "nonterminal_evidence_requests=1"
    )
    with pytest.raises(RuntimeError, match=expected):
        await service.run_current_surface_checkpoint(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            deadline=datetime(2026, 9, 12, 0, 5, tzinfo=UTC),
        )

    if snapshot_request:
        assert len(consumer.calls) == 1
        assert consumer.calls[0]["session_dependency"] == "active_session"
    else:
        assert consumer.calls == []


@pytest.mark.parametrize("snapshots", [0, 7, 12])
async def test_current_surface_checkpoint_settles_exhausted_h5_work_before_navigation(
    snapshots: int,
) -> None:
    class Publisher:
        async def publish_pending(self, **kwargs: Any) -> int:
            del kwargs
            return 0

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            del kwargs
            return SimpleNamespace(claimed_count=0)

    class Settlement:
        calls: list[dict[str, Any]] = []

        async def settle_stalled_work_for_closure(self, **kwargs: Any) -> object:
            self.calls.append(kwargs)
            return SimpleNamespace(settled_work_item_count=1, settled_rule_count=1)

    settlement = Settlement()
    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=Consumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=1,
                    exhausted_work_item_count=1,
                    snapshot_evidence_request_count=snapshots,
                ),
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=1,
                    exhausted_work_item_count=1,
                    snapshot_evidence_request_count=snapshots,
                ),
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                    snapshot_evidence_request_count=snapshots,
                ),
            ]
        ),
        closure_settlement=settlement,
        targeted_recapture_drain=SimpleNamespace(
            can_defer_snapshot_requests=AsyncMock(return_value=True),
            drain_one=AsyncMock(return_value=False),
        ),
        checkpoint_max_wait_seconds=0.05,
        max_poll_seconds=0.05,
        sleep=lambda _delay: _completed_sleep(),
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )

    summary = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, 0, 5, tzinfo=UTC),
    )

    assert summary.settled_work_item_count == 1
    assert summary.deferred_snapshot_evidence_request_count == snapshots
    assert settlement.calls == [
        {
            "task_id": _TASK_ID,
            "active_session_agent_run_id": _RUN_ID,
            "active_session_exhausted_only": True,
        }
    ]


async def test_active_session_drain_state_rejects_exhausted_count_above_total() -> None:
    with pytest.raises(ValueError, match="不能大于非终态工作项数"):
        H5ActiveSessionDrainState(
            pending_capture_count=0,
            nonterminal_work_item_count=0,
            exhausted_work_item_count=1,
        )


@pytest.mark.parametrize("settled_count,recoverable,expected_error", [
    (1, True, "nonterminal_work_items=1"),
    (0, True, "nonterminal_work_items=2"),
    (1, False, "snapshot_evidence_requests=7"),
])
async def test_checkpoint_mixed_work_settlement_never_substitutes_for_authority_read(
    settled_count, recoverable, expected_error,
):
    before = H5ActiveSessionDrainState(
        pending_capture_count=0, nonterminal_work_item_count=2,
        exhausted_work_item_count=1, snapshot_evidence_request_count=7,
    )
    after = H5ActiveSessionDrainState(
        pending_capture_count=0,
        nonterminal_work_item_count=1 if recoverable and settled_count else 2 if not settled_count else 0,
        snapshot_evidence_request_count=7,
    )
    settlement = SimpleNamespace(settle_stalled_work_for_closure=AsyncMock(
        return_value=SimpleNamespace(
            settled_work_item_count=settled_count, settled_rule_count=settled_count,
        ),
    ))
    reader = _DrainStateReader([before, after])
    recapture = SimpleNamespace(
        can_defer_snapshot_requests=AsyncMock(return_value=recoverable),
        drain_one=AsyncMock(return_value=False),
    )
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=_ActiveConsumer(), state_reader=reader,
        closure_settlement=settlement, targeted_recapture_drain=recapture,
        checkpoint_max_wait_seconds=0.05, max_wait_seconds=0.05,
        max_poll_seconds=0.05, now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
        sleep=lambda _delay: _completed_sleep(),
    )
    with pytest.raises(RuntimeError, match=expected_error):
        await service.run_current_surface_checkpoint(
            task_id=_TASK_ID, agent_run_id=_RUN_ID,
            deadline=datetime(2026, 9, 12, 0, 5, tzinfo=UTC),
        )
    settlement.settle_stalled_work_for_closure.assert_awaited_once()
    assert settlement.settle_stalled_work_for_closure.await_args.kwargs[
        "active_session_exhausted_only"
    ] is True


async def test_healthy_checkpoint_does_not_extend_frozen_run_deadline():
    started = datetime(2026, 10, 3, tzinfo=UTC)
    clock = {"now": started}
    consumer = AsyncMock()

    async def consume(**kwargs):
        clock["now"] += timedelta(seconds=100)
        return SimpleNamespace(claimed_count=1, completed_count=1)

    consumer.drain_available.side_effect = consume
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=consumer,
        state_reader=_DrainStateReader([H5ActiveSessionDrainState(
            pending_capture_count=0, nonterminal_work_item_count=1,
        )]), now=lambda: clock["now"],
    )
    with pytest.raises(RuntimeError, match="nonterminal_work_items=1"):
        await service.run_current_surface_checkpoint(
            task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=started + timedelta(seconds=300),
        )
    assert clock["now"] == started + timedelta(seconds=300)
    assert consumer.drain_available.await_count == 3


async def test_checkpoint_settles_only_exhausted_subset_then_finishes_healthy_judge():
    facts = {"settled": False, "judged": False, "calls": 0}

    class Reader:
        async def load_drain_state(self, **kwargs):
            return H5ActiveSessionDrainState(
                pending_capture_count=0,
                nonterminal_work_item_count=2 - int(facts["settled"]) - int(facts["judged"]),
                exhausted_work_item_count=int(not facts["settled"]),
                snapshot_evidence_request_count=7,
            )

    class Consumer:
        async def drain_available(self, **kwargs):
            facts["calls"] += 1
            facts["judged"] = facts["settled"]
            return SimpleNamespace(
                claimed_count=int(facts["judged"]), completed_count=int(facts["judged"]),
            )

    async def settle(**kwargs):
        assert kwargs["active_session_exhausted_only"] is True
        facts["settled"] = True
        return SimpleNamespace(settled_work_item_count=1, settled_rule_count=1)

    settlement = SimpleNamespace(settle_stalled_work_for_closure=AsyncMock(side_effect=settle))
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=Consumer(), state_reader=Reader(),
        closure_settlement=settlement, targeted_recapture_drain=SimpleNamespace(
            can_defer_snapshot_requests=AsyncMock(return_value=True),
            drain_one=AsyncMock(return_value=False),
        ), now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )
    result = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID, agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 12, 0, 5, tzinfo=UTC),
    )
    assert facts["calls"] == 2
    settlement.settle_stalled_work_for_closure.assert_awaited_once()
    assert result.settled_work_item_count == 1
    assert result.deferred_snapshot_evidence_request_count == 7


async def test_current_surface_checkpoint_accepts_drained_state_at_task_deadline() -> None:
    class Publisher:
        async def publish_pending(self, **kwargs: Any) -> int:
            raise AssertionError(f"截止时间已到不得继续发布：{kwargs}")

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            raise AssertionError(f"截止时间已到不得继续消费：{kwargs}")

    now = datetime(2026, 9, 12, tzinfo=UTC)
    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=Consumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                )
            ]
        ),
        now=lambda: now,
    )

    summary = await service.run_current_surface_checkpoint(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=now,
    )

    assert summary.drain_round_count == 0


@pytest.mark.parametrize("recoverable", [False, True])
async def test_checkpoint_final_read_rechecks_recoverable_snapshot(recoverable):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    recapture = SimpleNamespace(can_defer_snapshot_requests=AsyncMock(return_value=recoverable))
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]), consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader([H5ActiveSessionDrainState(
            pending_capture_count=0, nonterminal_work_item_count=0,
            snapshot_evidence_request_count=2,
        )]), targeted_recapture_drain=recapture, now=lambda: now,
    )
    args = dict(task_id=_TASK_ID, agent_run_id=_RUN_ID, deadline=now)
    if recoverable:
        result = await service.run_current_surface_checkpoint(**args)
        assert result.deferred_snapshot_evidence_request_count == 2
    else:
        with pytest.raises(RuntimeError, match="snapshot_evidence_requests=2"):
            await service.run_current_surface_checkpoint(**args)
    recapture.can_defer_snapshot_requests.assert_awaited_once()


async def _completed_sleep() -> None:
    return None


async def test_close_drain_checks_cancellation_before_publishing() -> None:
    publisher = _LivePublisher([1])
    cancellation = _Cancellation()
    service = H5ActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader([]),
        cancellation=cancellation,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        await service.drain_before_close(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            deadline=datetime(2026, 9, 11, tzinfo=UTC),
        )

    assert cancellation.calls == [(_TASK_ID, _RUN_ID, "active_session_inspection_drain")]
    assert publisher.calls == []


async def test_close_drain_runs_h5_probe_inside_active_session() -> None:
    probe = _ProbeDrain()
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                )
            ]
        ),
        probe_drain=probe,
        now=lambda: datetime(2026, 9, 10, tzinfo=UTC),
    )

    await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 11, tzinfo=UTC),
    )

    assert probe.calls == [(_TASK_ID, _RUN_ID)]


async def test_close_drain_reconciles_coverage_after_last_work_finishes() -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pending(self, **kwargs: Any) -> int:
            del kwargs
            events.append("publish")
            return 0

    class Consumer:
        async def drain_available(self, **kwargs: Any) -> object:
            del kwargs
            events.append("judge")
            return SimpleNamespace(claimed_count=1)

    class CoverageAuthority:
        def __init__(self) -> None:
            self.results = [0, 1, 0, 0]

        async def reconcile_available(self, **kwargs: Any) -> int:
            del kwargs
            events.append("coverage")
            return self.results.pop(0)

    service = H5ActiveSessionInspectionDrainService(
        publisher=Publisher(),
        consumer=Consumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=1,
                ),
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                ),
            ]
        ),
        coverage_authority=CoverageAuthority(),
        now=lambda: datetime(2026, 9, 10, tzinfo=UTC),
    )

    summary = await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=datetime(2026, 9, 11, tzinfo=UTC),
    )

    assert events == [
        "coverage",
        "publish",
        "judge",
        "coverage",
        "coverage",
        "publish",
        "judge",
        "coverage",
    ]
    assert summary.drain_round_count == 2


async def test_close_drain_routes_h5_request_before_claiming_probe() -> None:
    events: list[str] = []
    recapture = _TargetedRecaptureDrain(events)

    class OrderedProbe(_ProbeDrain):
        async def drain_one(self, *, task_id: str, agent_run_id: str) -> bool:
            events.append("probe")
            await super().drain_one(task_id=task_id, agent_run_id=agent_run_id)
            return len(self.calls) == 2

    probe = OrderedProbe()
    deadline = datetime(2026, 9, 12, tzinfo=UTC)
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                    nonterminal_evidence_request_count=0,
                )
            ]
        ),
        targeted_recapture_drain=recapture,
        probe_drain=probe,
        now=lambda: datetime(2026, 9, 11, tzinfo=UTC),
    )

    await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=deadline,
    )

    assert events == ["probe", "recapture", "probe"]
    assert recapture.calls == [
        (_TASK_ID, _RUN_ID, deadline, True),
    ]


async def test_close_drain_waits_for_scheduled_retry_before_session_close() -> None:
    """尚未到期的 retry_wait 仍是活会话工作，不能被误判为已排空。"""

    current = datetime(2026, 8, 30, tzinfo=UTC)
    clock = {"now": current}
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    publisher = _LivePublisher([2, 0])
    consumer = _ActiveConsumer()
    service = H5ActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=consumer,
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=1,
                    next_attempt_at=current + timedelta(seconds=1),
                ),
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                ),
            ]
        ),
        grace_seconds=5,
        now=lambda: clock["now"],
        sleep=sleep,
    )

    result = await service.drain_before_close(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        deadline=current + timedelta(minutes=1),
    )

    assert result.published_work_item_count == 2
    assert result.drain_round_count == 2
    assert sleeps == [1.0]
    assert all(call["session_dependency"] == "active_session" for call in consumer.calls)
    assert all(call["max_items"] == 1 for call in consumer.calls)


async def test_close_drain_fails_closed_when_active_work_never_finishes() -> None:
    current = datetime(2026, 8, 30, tzinfo=UTC)
    clock = {"now": current}

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0, 0, 0]),
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=1,
                    nonterminal_work_item_count=1,
                )
            ]
        ),
        grace_seconds=0.1,
        max_poll_seconds=0.05,
        now=lambda: clock["now"],
        sleep=sleep,
    )

    with pytest.raises(RuntimeError, match="active-session 规则未排空"):
        await service.drain_before_close(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            deadline=current,
        )


async def test_close_drain_rejects_unresolved_evidence_request() -> None:
    current = datetime(2026, 8, 30, tzinfo=UTC)
    clock = {"now": current}

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    class Settlement:
        called = False

        async def settle_stalled_work_for_closure(self, **kwargs: Any) -> object:
            del kwargs
            self.called = True
            return object()

    settlement = Settlement()
    consumer = _ActiveConsumer()
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0, 0, 0]),
        consumer=consumer,
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(
                    pending_capture_count=0,
                    nonterminal_work_item_count=0,
                    nonterminal_evidence_request_count=1,
                )
            ]
        ),
        grace_seconds=0.1,
        max_poll_seconds=0.05,
        now=lambda: clock["now"],
        sleep=sleep,
        closure_settlement=settlement,
    )

    with pytest.raises(RuntimeError, match="补证请求未排空"):
        await service.drain_before_close(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            deadline=current,
        )

    assert settlement.called is False
    assert consumer.calls == []
