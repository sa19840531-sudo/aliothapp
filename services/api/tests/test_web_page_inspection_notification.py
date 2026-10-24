"""Web 单 Capture 正式质检通知门禁与可靠重放测试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.h5_evidence_manifest import H5EvidenceManifestFinalizer
from services.api.app.application.outbox import (
    OutboxRelayResult,
    WakeupTransportReceipt,
)
from services.api.app.application.web_page_inspection_notification import (
    ReliableDeliveryWebPublicationWakeup,
    WebPageInspectionEvidence,
    WebPageInspectionNotification,
    WebPageInspectionNotifyingClassifier,
)
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.inspection import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.domain.protocol import build_event_message_id
from services.api.app.infrastructure.db import web_capture_evidence_repository
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)


def _ready_evidence(**overrides: object) -> WebPageInspectionEvidence:
    values: dict[str, object] = {
        "task_id": "task-web",
        "agent_run_id": "run-web",
        "session_id": "web:run-web",
        "session_owner": "web-v2-workflow",
        "session_status": "active",
        "task_status": "running",
        "task_finalization_state": "open",
        "task_finalization_epoch": 3,
        "session_finalization_epoch": 3,
        "action_finalization_epoch": 3,
        "run_status": "running",
        "current_run": True,
        "capture_id": "capture-ready",
        "capture_status": "captured",
        "capture_role": "after_action",
        "action_execution_id": "action-ready",
        "parent_capture_id": "capture-before",
        "page_type": "content_list",
        "page_state": "stable",
        "detection_status": "recognized",
        "assessment_record_id": "assessment-ready",
        "assessment_hash": "assessment-hash",
        "assessment_schema_version": "web-page-type-assessment.v3",
        "artifact_types": ("dom", "screenshot", "visible_text"),
        "before_artifact_types": ("dom", "screenshot"),
        "manifest_id": "manifest-after",
        "manifest_hash": "manifest-after-hash",
        "manifest_quality_status": "pass",
        "before_manifest_id": "manifest-before",
        "before_manifest_hash": "manifest-before-hash",
        "before_manifest_quality_status": "pass",
        "execution_mode": "full_inspection",
        "notification_mode": "enforced",
        "inspection_evidence_role": "inspection",
    }
    values.update(overrides)
    return WebPageInspectionEvidence(**values)


class _EvidenceRepository:
    def __init__(self, evidence: WebPageInspectionEvidence | None) -> None:
        self.evidence = evidence
        self.diagnostics: list[dict[str, object]] = []

    async def read_for_notification(self, **_kwargs: object) -> WebPageInspectionEvidence | None:
        return self.evidence

    async def list_ready_capture_ids(self, **_kwargs: object) -> tuple[str, ...]:
        return (self.evidence.capture_id,) if self.evidence is not None else ()

    async def list_ready_capture_ids_for_final_handoff(
        self,
        **_kwargs: object,
    ) -> tuple[str, ...]:
        return (self.evidence.capture_id,) if self.evidence is not None else ()

    async def record_notification_diagnostic(self, **kwargs: object) -> None:
        self.diagnostics.append(dict(kwargs))


@pytest.mark.asyncio
async def test_classification_retains_blocked_notification_then_published_result():
    repository = _EvidenceRepository(_ready_evidence(artifact_types=()))
    notification = WebPageInspectionNotification(
        reader=repository, inventory=_Inventory(), publication_writer=_PublicationWriter(),
    )
    assessment = object()

    class Classifier:
        async def classify(self, capture):
            return assessment

    classifier = WebPageInspectionNotifyingClassifier(
        classifier=Classifier(), notification=notification, task_id="task-web",
        agent_run_id="run-web", session_id="web:run-web",
    )
    assert await classifier.classify(SimpleNamespace(capture_id="capture-ready")) is assessment
    assert repository.diagnostics[-1]["status"] == "blocked"
    assert repository.diagnostics[-1]["reason_code"] == "WEB_CAPTURE_ARTIFACT_MISSING"
    assert repository.diagnostics[-1]["retryable"] is False
    repository.evidence = _ready_evidence()
    await classifier.classify(SimpleNamespace(capture_id="capture-ready"))
    assert repository.diagnostics[-1]["status"] == "published"


class _Inventory:
    def __init__(self) -> None:
        self.items: dict[str, object] = {}

    async def upsert_inventory_item(self, *, item: object) -> object:
        self.items.setdefault(item.action_execution_id, item)
        return SimpleNamespace(status=SimpleNamespace(value="pass"))


class _PublicationWriter:
    def __init__(self, *, outbox_status: str = "pending") -> None:
        self.publications: dict[tuple[object, ...], str] = {}
        self.outbox: set[str] = set()
        self.outbox_status = outbox_status
        self.calls: list[dict[str, object]] = []

    async def create_for_capture(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        key = (
            kwargs["task_id"],
            kwargs["source_agent_run_id"],
            kwargs["capture_id"],
            kwargs["page_type_assessment_id"],
            kwargs["assessment_hash"],
        )
        created = key not in self.publications
        publication_id = self.publications.setdefault(key, "publication-web")
        self.outbox.add(publication_id)
        return SimpleNamespace(
            publication_id=publication_id,
            created=created,
            row_version=1,
            outbox_message_id=_publication_message_id(),
            outbox_status=self.outbox_status,
        )


class _Wakeup:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0
        self.work_items: set[str] = set()

    async def wake(
        self,
        *,
        task_id: str,
        publication_id: str,
        outbox_message_id: str,
        aggregate_version: int,
    ) -> None:
        assert task_id == "task-web"
        assert publication_id == "publication-web"
        assert outbox_message_id == _publication_message_id()
        assert aggregate_version == 1
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("redis unavailable")
        self.work_items.add(publication_id)


class _SqlTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SqlConnection:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _SqlTransaction:
        return _SqlTransaction()

    async def execute(self, *_args: object) -> None:
        return None

    async def fetch(self, query: str, *args: object) -> list[dict[str, str]]:
        self.fetch_calls.append((query, args))
        return [{"capture_id": "capture-ready"}]

    async def fetchrow(self, query: str, *args: object) -> None:
        self.fetchrow_calls.append((query, args))
        return None


class _LogicalVersionConnection(_SqlConnection):
    def __init__(self, *, existing_fingerprint: str, source_fingerprint: str) -> None:
        super().__init__()
        self.existing_fingerprint = existing_fingerprint
        self.source_fingerprint = source_fingerprint

    async def fetchrow(
        self,
        query: str,
        *args: object,
    ) -> dict[str, object] | None:
        self.fetchrow_calls.append((query, args))
        if "SELECT 1 AS gate_passed" in query:
            return {"gate_passed": 1}
        if "FROM qc_task AS task" in query and "AS content_fingerprint" in query:
            return {
                "assessment_hash": "assessment-hash",
                "page_type": "content_list",
                "page_state": "stable",
                "logical_page_id": "web-page:logical-42",
                "content_fingerprint": self.source_fingerprint,
                "finalization_epoch": 3,
                "ruleset_id": "ruleset-web",
                "ruleset_version": 2,
                "ruleset_snapshot_hash": "ruleset-snapshot",
            }
        if "FROM qc_h5_inspection_publication AS publication" in query:
            query_checks_fingerprint = (
                "artifact.artifact_type IN ('dom', 'visible_text')" in query
            )
            if query_checks_fingerprint and (
                self.existing_fingerprint != self.source_fingerprint
            ):
                return None
            return {"publication_id": "publication-existing", "row_version": 4}
        if "INSERT INTO qc_h5_inspection_publication AS existing" in query:
            return {
                "publication_id": str(args[0]),
                "input_hash": str(args[10]),
                "row_version": 1,
                "created": True,
            }
        return None

    async def fetchval(self, query: str, *args: object) -> object:
        if "INSERT INTO qc_outbox_event" in query:
            return args[0]
        if "SELECT status" in query:
            return "pending"
        return None


class _SqlAcquire:
    def __init__(self, connection: _SqlConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _SqlConnection:
        return self._connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SqlPool:
    def __init__(self, connection: _SqlConnection) -> None:
        self._connection = connection

    def acquire(self) -> _SqlAcquire:
        return _SqlAcquire(self._connection)


def _service(
    evidence: WebPageInspectionEvidence | None,
    *,
    wakeup: object | None = None,
    publication_writer: _PublicationWriter | None = None,
) -> tuple[
    WebPageInspectionNotification,
    _EvidenceRepository,
    _Inventory,
    _PublicationWriter,
    object,
]:
    repository = _EvidenceRepository(evidence)
    inventory = _Inventory()
    publication = publication_writer or _PublicationWriter()
    resolved_wakeup = wakeup or _Wakeup()
    return (
        WebPageInspectionNotification(
            reader=repository,
            inventory=inventory,
            publication_writer=publication,
            wakeup=resolved_wakeup,
        ),
        repository,
        inventory,
        publication,
        resolved_wakeup,
    )


def _publication_message_id(
    *,
    task_id: str = "task-web",
    publication_id: str = "publication-web",
) -> str:
    return build_event_message_id(
        message_type=H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
        task_id=task_id,
        aggregate_type="h5_inspection_publication",
        aggregate_id=publication_id,
        aggregate_version=1,
    )


@pytest.mark.asyncio
async def test_shared_manifest_finalizer_settles_every_pending_session_capture() -> None:
    pending_a = SimpleNamespace(quality_status=EvidenceQualityStatus.PENDING)
    pending_b = SimpleNamespace(quality_status=EvidenceQualityStatus.PENDING)
    passed = SimpleNamespace(quality_status=EvidenceQualityStatus.PASS)

    class Repository:
        async def list_latest_for_session(self, **kwargs: str) -> tuple[object, ...]:
            assert kwargs == {
                "task_id": "task-web",
                "session_id": "web:run-web",
                "agent_run_id": "run-web",
            }
            return (
                SimpleNamespace(manifest=pending_a),
                SimpleNamespace(manifest=passed),
                SimpleNamespace(manifest=None),
                SimpleNamespace(manifest=pending_b),
            )

    class Finalizer:
        def __init__(self) -> None:
            self.manifests: list[object] = []

        async def finalize(self, manifest: object) -> object:
            self.manifests.append(manifest)
            return SimpleNamespace(manifest=manifest)

    underlying = Finalizer()
    service = H5EvidenceManifestFinalizer(
        repository=Repository(),
        finalizer=underlying,
    )

    finalized = await service.finalize_session(
        task_id="task-web",
        session_id="web:run-web",
        agent_run_id="run-web",
    )

    assert finalized == 2
    assert underlying.manifests == [pending_a, pending_b]


def _published_relay_result(message_id: str) -> OutboxRelayResult:
    return OutboxRelayResult(
        claimed=True,
        message_id=message_id,
        attempt_count=1,
        receipt=WakeupTransportReceipt(
            stream_name="alioth:v2:wakeup:h5-inspection-publication",
            stream_entry_id="1-0",
        ),
    )


def _reliable_wakeup(
    relay_once: Callable[[], Awaitable[tuple[OutboxRelayResult, ...]]],
) -> ReliableDeliveryWebPublicationWakeup:
    return ReliableDeliveryWebPublicationWakeup(relay_once)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "reason_code"),
    [
        (None, "WEB_CAPTURE_MISSING"),
        (
            replace(_ready_evidence(), artifact_types=("screenshot", "visible_text")),
            "WEB_CAPTURE_ARTIFACT_MISSING",
        ),
        (
            replace(_ready_evidence(), manifest_quality_status="pending"),
            "WEB_CAPTURE_MANIFEST_NOT_PASS",
        ),
        (
            replace(
                _ready_evidence(),
                detection_status="unrecognized",
                page_type="web_unknown",
            ),
            "WEB_PAGE_TYPE_ASSESSMENT_NOT_RECOGNIZED",
        ),
        (
            replace(_ready_evidence(), page_type="landing"),
            "WEB_PAGE_TYPE_NOT_CANONICAL",
        ),
        (
            replace(_ready_evidence(), session_owner="web-workflow"),
            "WEB_CAPTURE_CONTEXT_NOT_CURRENT",
        ),
    ],
    ids=(
        "capture",
        "artifact",
        "manifest",
        "assessment",
        "canonical-page-type",
        "run-epoch-task-session",
    ),
)
async def test_notification_blocks_each_missing_gate_with_stable_web_reason(
    evidence: WebPageInspectionEvidence | None,
    reason_code: str,
) -> None:
    service, _repository, inventory, publication, wakeup = _service(evidence)

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "blocked"
    assert result.reason_code == reason_code
    assert result.publication_id is None
    assert inventory.items == {}
    assert publication.publications == {}
    assert publication.outbox == set()
    assert wakeup.calls == 0


@pytest.mark.asyncio
async def test_notification_publishes_only_after_all_six_gates_pass() -> None:
    service, _repository, inventory, publication, wakeup = _service(_ready_evidence())

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "published"
    assert result.reason_code == "WEB_INSPECTION_PUBLICATION_CREATED"
    assert result.publication_id == "publication-web"
    assert result.publication_created is True
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1
    assert publication.outbox == {"publication-web"}
    assert wakeup.work_items == {"publication-web"}
    assert publication.calls[0]["strict_web_gate"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", ["home", "content_list", "content_detail", "video_list", "video_detail"])
async def test_notification_accepts_current_frozen_run_despite_new_lifecycle_schema(page_type):
    from datetime import UTC, datetime

    from services.api.app.application.web_run_policy import (
        load_published_web_policy,
        materialize_web_policy,
    )
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot

    frozen = materialize_web_policy(WebRunPolicySnapshot.create(
        task_id="task-web", agent_run_id="run-web",
        payload=load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=120),
    ))
    service, _, inventory, publication, _ = _service(_ready_evidence(
        assessment_schema_version=frozen.assessment_schema_version, page_type=page_type,
    ))
    result = await service.notify_ready_capture(task_id="task-web", agent_run_id="run-web",
        session_id="web:run-web", capture_id="capture-ready")
    assert result.status == "published"
    assert len(inventory.items) == len(publication.publications) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", ["home", "content_list", "content_detail", "video_list", "video_detail", "activity", "search_page"])
async def test_notification_accepts_v4_business_types_only(page_type: str) -> None:
    service, _, inventory, publication, _ = _service(_ready_evidence(
        assessment_schema_version="web-page-type-assessment.v4", page_type=page_type,
    ))
    result = await service.notify_ready_capture(
        task_id="task-web", agent_run_id="run-web",
        session_id="web:run-web", capture_id="capture-ready",
    )
    assert result.status == "published"
    assert len(inventory.items) == len(publication.publications) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("schema,page_type,reason", [
    ("web-page-type-assessment.v4", "age_gate", "WEB_PAGE_TYPE_NOT_CANONICAL"),
    ("web-page-type-assessment.v4", "unknown", "WEB_PAGE_TYPE_NOT_CANONICAL"),
    ("web-page-type-assessment.v99", "home", "WEB_PAGE_TYPE_ASSESSMENT_NOT_RECOGNIZED"),
    ("web-page-type-assessment.v2", "home", "WEB_PAGE_TYPE_ASSESSMENT_NOT_RECOGNIZED"),
    ("web-page-type-assessment.v3", "age_gate", "WEB_PAGE_TYPE_NOT_CANONICAL"),
    ("web-page-type-assessment.v3", "search_page", "WEB_PAGE_TYPE_NOT_CANONICAL"),
    ("web-page-type-assessment.v3", "activity", "WEB_PAGE_TYPE_NOT_CANONICAL"),
    ("web-page-type-assessment.v3", "unknown", "WEB_PAGE_TYPE_NOT_CANONICAL"),
])
async def test_current_notification_cannot_sign_future_or_misversioned_page_types(schema, page_type, reason):
    service, _, inventory, publication, _ = _service(_ready_evidence(
        assessment_schema_version=schema, page_type=page_type,
    ))
    result = await service.notify_ready_capture(task_id="task-web", agent_run_id="run-web",
        session_id="web:run-web", capture_id="capture-ready")
    assert result.status == "blocked"
    assert result.reason_code == reason
    assert inventory.items == publication.publications == {}


@pytest.mark.asyncio
async def test_notification_rejects_capture_without_inspection_role_grant() -> None:
    evidence = _ready_evidence(inspection_evidence_role="classification")
    service, _repository, inventory, publication, wakeup = _service(evidence)

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "blocked"
    assert result.reason_code == "WEB_CAPTURE_INSPECTION_ROLE_MISSING"
    assert inventory.items == {}
    assert publication.publications == {}
    assert wakeup.calls == 0


@pytest.mark.asyncio
async def test_notification_finalizes_both_manifests_before_publication() -> None:
    evidence = replace(
        _ready_evidence(),
        manifest_quality_status="pending",
        before_manifest_quality_status="pending",
    )
    service, repository, inventory, publication, wakeup = _service(evidence)

    class Finalizer:
        def __init__(self) -> None:
            self.capture_ids: list[str] = []

        async def finalize_capture(self, *, task_id: str, capture_id: str) -> None:
            assert task_id == "task-web"
            self.capture_ids.append(capture_id)
            if len(self.capture_ids) == 2:
                repository.evidence = replace(
                    evidence,
                    manifest_quality_status="pass",
                    before_manifest_quality_status="pass",
                )

    finalizer = Finalizer()
    service._manifest_finalizer = finalizer

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "published"
    assert finalizer.capture_ids == ["capture-before", "capture-ready"]
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1
    assert wakeup.work_items == {"publication-web"}


@pytest.mark.asyncio
async def test_notification_replay_is_idempotent_for_publication_outbox_and_work_item() -> None:
    service, _repository, inventory, publication, wakeup = _service(_ready_evidence())

    first = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )
    replay = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert first.publication_created is True
    assert replay.publication_created is False
    assert replay.reason_code == "WEB_INSPECTION_PUBLICATION_EXISTS"
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1
    assert publication.outbox == {"publication-web"}
    assert wakeup.work_items == {"publication-web"}


@pytest.mark.asyncio
async def test_final_handoff_publishes_once_after_exploration_stops() -> None:
    service, _repository, inventory, publication, wakeup = _service(
        _ready_evidence(task_finalization_state="exploration_draining")
    )
    finalized_sessions: list[dict[str, str]] = []

    class Finalizer:
        async def finalize_session(self, **kwargs: str) -> int:
            finalized_sessions.append(dict(kwargs))
            return 2

    service._manifest_finalizer = Finalizer()

    first = await service.publish_pending_for_final_handoff(
        task_id="task-web",
        agent_run_id="run-web",
        finalization_epoch=3,
    )
    replay = await service.publish_pending_for_final_handoff(
        task_id="task-web",
        agent_run_id="run-web",
        finalization_epoch=3,
    )

    assert first == 1
    assert replay == 0
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1
    assert publication.outbox == {"publication-web"}
    assert wakeup.work_items == {"publication-web"}
    assert finalized_sessions == [
        {
            "task_id": "task-web",
            "session_id": "web:run-web",
            "agent_run_id": "run-web",
        },
        {
            "task_id": "task-web",
            "session_id": "web:run-web",
            "agent_run_id": "run-web",
        },
    ]
    assert [call["strict_web_final_handoff_epoch"] for call in publication.calls] == [
        3,
        3,
    ]


@pytest.mark.parametrize("terminal_run_status", ["done", "failed", "aborted"])
@pytest.mark.asyncio
async def test_final_handoff_replays_current_terminal_main_run(
    terminal_run_status: str,
) -> None:
    service, _repository, inventory, publication, _wakeup = _service(
        _ready_evidence(
            task_finalization_state="exploration_draining",
            run_status=terminal_run_status,
        )
    )

    published = await service.publish_pending_for_final_handoff(
        task_id="task-web",
        agent_run_id="run-web",
        finalization_epoch=3,
    )

    assert published == 1
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1


@pytest.mark.asyncio
async def test_incremental_notification_does_not_accept_exploration_draining() -> None:
    service, _repository, inventory, publication, wakeup = _service(
        _ready_evidence(task_finalization_state="exploration_draining")
    )

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "blocked"
    assert result.reason_code == "WEB_CAPTURE_CONTEXT_NOT_CURRENT"
    assert inventory.items == {}
    assert publication.publications == {}
    assert wakeup.calls == 0


@pytest.mark.asyncio
async def test_relaying_only_a_preceding_task_does_not_confirm_current_publication() -> None:
    relay_calls = 0

    async def relay_once() -> tuple[OutboxRelayResult, ...]:
        nonlocal relay_calls
        relay_calls += 1
        return (
            _published_relay_result(
                _publication_message_id(
                    task_id="task-before",
                    publication_id="publication-before",
                )
            ),
        )

    service, repository, inventory, publication, _wakeup = _service(
        _ready_evidence(),
        wakeup=_reliable_wakeup(relay_once),
    )

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "published_wakeup_pending"
    assert result.reason_code == "WEB_INSPECTION_WAKEUP_RETRYABLE"
    assert result.diagnostic_type == "WebPublicationWakeupNotConfirmedError"
    assert relay_calls == 1
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1
    assert repository.diagnostics[-1]["publication_id"] == "publication-web"


@pytest.mark.asyncio
async def test_relay_finds_current_message_after_preceding_task_and_confirms_success() -> None:
    async def relay_once() -> tuple[OutboxRelayResult, ...]:
        return (
            _published_relay_result(
                _publication_message_id(
                    task_id="task-before",
                    publication_id="publication-before",
                )
            ),
            _published_relay_result(_publication_message_id()),
        )

    service, repository, _inventory, _publication, _wakeup = _service(
        _ready_evidence(),
        wakeup=_reliable_wakeup(relay_once),
    )

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "published"
    assert result.reason_code == "WEB_INSPECTION_PUBLICATION_CREATED"
    assert repository.diagnostics == []


@pytest.mark.asyncio
async def test_current_message_rescheduled_by_real_relay_contract_is_retryable() -> None:
    async def relay_once() -> tuple[OutboxRelayResult, ...]:
        return (
            OutboxRelayResult(
                claimed=True,
                message_id=_publication_message_id(),
                attempt_count=1,
                rescheduled=True,
                error="ConnectionError: redis unavailable",
            ),
        )

    service, repository, inventory, publication, _wakeup = _service(
        _ready_evidence(),
        wakeup=_reliable_wakeup(relay_once),
    )

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "published_wakeup_pending"
    assert result.reason_code == "WEB_INSPECTION_WAKEUP_RETRYABLE"
    assert result.diagnostic_type == "WebPublicationWakeupNotConfirmedError"
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1
    assert repository.diagnostics[-1]["publication_id"] == "publication-web"


@pytest.mark.asyncio
async def test_replay_of_already_published_outbox_event_skips_global_relay() -> None:
    relay_calls = 0

    async def relay_once() -> tuple[OutboxRelayResult, ...]:
        nonlocal relay_calls
        relay_calls += 1
        raise AssertionError("已发布事件重放不得再次驱动全局 Relay")

    writer = _PublicationWriter(outbox_status="published")
    writer.publications[(
        "task-web",
        "run-web",
        "capture-ready",
        "assessment-ready",
        "assessment-hash",
    )] = "publication-web"
    service, repository, _inventory, publication, _wakeup = _service(
        _ready_evidence(),
        wakeup=_reliable_wakeup(relay_once),
        publication_writer=writer,
    )

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "published"
    assert result.reason_code == "WEB_INSPECTION_PUBLICATION_EXISTS"
    assert result.publication_created is False
    assert relay_calls == 0
    assert len(publication.publications) == 1
    assert repository.diagnostics == []


@pytest.mark.asyncio
async def test_redis_failure_keeps_publication_and_later_replay_recovers_wakeup() -> None:
    wakeup = _Wakeup(failures=1)
    service, repository, inventory, publication, _wakeup = _service(
        _ready_evidence(),
        wakeup=wakeup,
    )

    failed_wakeup = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert failed_wakeup.status == "published_wakeup_pending"
    assert failed_wakeup.reason_code == "WEB_INSPECTION_WAKEUP_RETRYABLE"
    assert failed_wakeup.retryable is True
    assert failed_wakeup.publication_id == "publication-web"
    assert len(inventory.items) == 1
    assert len(publication.publications) == 1
    assert publication.outbox == {"publication-web"}
    assert wakeup.work_items == set()
    assert repository.diagnostics[-1]["reason_code"] == "WEB_INSPECTION_WAKEUP_RETRYABLE"

    recovered = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert recovered.status == "published"
    assert recovered.publication_created is False
    assert wakeup.work_items == {"publication-web"}
    assert len(publication.publications) == 1
    assert publication.outbox == {"publication-web"}


@pytest.mark.asyncio
async def test_exploration_only_candidate_never_publishes() -> None:
    service, _repository, inventory, publication, wakeup = _service(
        _ready_evidence(execution_mode="exploration_only", notification_mode="off")
    )

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "blocked"
    assert result.reason_code == "WEB_INSPECTION_NOTIFICATION_DISABLED"
    assert inventory.items == {}
    assert publication.publications == {}
    assert wakeup.calls == 0


@pytest.mark.asyncio
async def test_notification_rejects_global_catalog_type_outside_web_bridge_closed_set() -> None:
    service, _repository, inventory, publication, wakeup = _service(
        _ready_evidence(page_type="profile")
    )

    result = await service.notify_ready_capture(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        capture_id="capture-ready",
    )

    assert result.status == "blocked"
    assert result.reason_code == "WEB_PAGE_TYPE_NOT_CANONICAL"
    assert inventory.items == {}
    assert publication.publications == {}
    assert wakeup.calls == 0


@pytest.mark.asyncio
async def test_shared_writer_emits_complete_strict_web_gate_before_publication() -> None:
    class Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Connection:
        def __init__(self) -> None:
            self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

        def transaction(self) -> Transaction:
            return Transaction()

        async def execute(self, *_args: object) -> None:
            return None

        async def fetchrow(self, query: str, *args: object) -> None:
            self.fetchrow_calls.append((query, args))
            return None

    class Acquire:
        def __init__(self, connection: Connection) -> None:
            self._connection = connection

        async def __aenter__(self) -> Connection:
            return self._connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Pool:
        def __init__(self, connection: Connection) -> None:
            self._connection = connection

        def acquire(self) -> Acquire:
            return Acquire(self._connection)

    connection = Connection()
    repository = PostgresH5InspectionPublicationRepository(pool=Pool(connection))

    with pytest.raises(RuntimeError, match="WEB_INSPECTION_STRICT_GATE_REJECTED"):
        await repository.create_for_capture(
            task_id="task-web",
            source_agent_run_id="run-web",
            capture_id="capture-ready",
            page_type_assessment_id="assessment-ready",
            assessment_hash="assessment-hash",
            page_type="content_list",
            page_state="stable",
            strict_web_gate=True,
        )

    assert len(connection.fetchrow_calls) == 1
    query, args = connection.fetchrow_calls[0]
    assert args == (
        "task-web",
        "run-web",
        "capture-ready",
        "assessment-ready",
        sorted(
            {
                "home", "content_list", "content_detail", "video_list", "video_detail",
                "activity", "search_page", "collection_page",
            }
        ),
    )
    for required_sql in (
        "task.target_platform = 'web'",
        "task.task_config->>'execution_mode' = 'full_inspection'",
        "task.task_config->>'web_inspection_notification_mode' = 'enforced'",
        "task.finalization_state = 'open'",
        "source_run.agent_type = 'main'",
        "source_run.status IN ('pending', 'running')",
        "session.session_id = ('web:' || source_run.id::text)",
        "session.session_owner = 'web-v2-workflow'",
        "session.status = 'active'",
        "action.finalization_epoch = task.finalization_epoch",
        "session.finalization_epoch = task.finalization_epoch",
        "common_capture.capture_role = 'after_action'",
        "before_capture.capture_role = 'before_action'",
        "after_screenshot.artifact_type = 'screenshot'",
        "after_dom.artifact_type = 'dom'",
        "after_text.artifact_type = 'visible_text'",
        "before_screenshot.artifact_type = 'screenshot'",
        "before_dom.artifact_type = 'dom'",
        "after_manifest.quality_status = 'pass'",
        "before_manifest.quality_status = 'pass'",
        "assessment.detection_status = 'recognized'",
        "assessment.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')",
        "capture.page_type = ANY($5::varchar[])",
    ):
        assert required_sql in query


@pytest.mark.asyncio
async def test_same_logical_page_and_content_fingerprint_reuse_publication_version() -> None:
    connection = _LogicalVersionConnection(
        existing_fingerprint="fingerprint-v1",
        source_fingerprint="fingerprint-v1",
    )
    repository = PostgresH5InspectionPublicationRepository(pool=_SqlPool(connection))

    result = await repository.create_for_capture(
        task_id="task-web",
        source_agent_run_id="run-web",
        capture_id="capture-repeated-snapshot",
        page_type_assessment_id="assessment-repeated-snapshot",
        assessment_hash="assessment-hash",
        page_type="content_list",
        page_state="stable",
        strict_web_gate=True,
        logical_page_id="web-page:logical-42",
        content_fingerprint="fingerprint-v1",
    )

    assert result.publication_id == "publication-existing"
    assert result.created is False
    assert result.row_version == 4


@pytest.mark.asyncio
async def test_changed_content_fingerprint_creates_new_publication_version() -> None:
    connection = _LogicalVersionConnection(
        existing_fingerprint="fingerprint-v1",
        source_fingerprint="fingerprint-v2",
    )
    repository = PostgresH5InspectionPublicationRepository(pool=_SqlPool(connection))

    result = await repository.create_for_capture(
        task_id="task-web",
        source_agent_run_id="run-web",
        capture_id="capture-new-version",
        page_type_assessment_id="assessment-new-version",
        assessment_hash="assessment-hash",
        page_type="content_list",
        page_state="stable",
        strict_web_gate=True,
        logical_page_id="web-page:logical-42",
        content_fingerprint="fingerprint-v2",
    )

    assert result.publication_id != "publication-existing"
    assert result.created is True
    assert result.row_version == 1


@pytest.mark.asyncio
async def test_final_handoff_reader_requires_current_owner_epoch_and_active_session() -> None:
    connection = _SqlConnection()
    repository = web_capture_evidence_repository.PostgresWebCaptureEvidenceRepository(
        pool=_SqlPool(connection)
    )

    capture_ids = await repository.list_ready_capture_ids_for_final_handoff(
        task_id="task-web",
        agent_run_id="run-web",
        finalization_epoch=3,
    )

    assert capture_ids == ("capture-ready",)
    query, args = connection.fetch_calls[0]
    assert args == ("task-web", "run-web", 3)
    for required_sql in (
        "task.finalization_state = 'exploration_draining'",
        "task.finalization_agent_run_id = run.id",
        "task.finalization_epoch = $3",
        "run.status IN ('pending', 'running', 'done', 'failed', 'aborted')",
        "session.finalization_epoch = task.finalization_epoch",
        "session.status = 'active'",
        "assessment.detection_status IN ('unrecognized', 'needs_review')",
    ):
        assert required_sql in query


@pytest.mark.asyncio
async def test_shared_writer_rechecks_final_handoff_epoch_in_publication_transaction() -> None:
    connection = _SqlConnection()
    repository = PostgresH5InspectionPublicationRepository(
        pool=_SqlPool(connection)
    )

    with pytest.raises(RuntimeError, match="WEB_INSPECTION_STRICT_GATE_REJECTED"):
        await repository.create_for_capture(
            task_id="task-web",
            source_agent_run_id="run-web",
            capture_id="capture-ready",
            page_type_assessment_id="assessment-ready",
            assessment_hash="assessment-hash",
            page_type="content_list",
            page_state="stable",
            strict_web_gate=True,
            strict_web_final_handoff_epoch=3,
        )

    query, args = connection.fetchrow_calls[0]
    assert args[-1] == 3
    for required_sql in (
        "$6::integer IS NOT NULL",
        "task.finalization_state = 'exploration_draining'",
        "task.finalization_agent_run_id = source_run.id",
        "task.finalization_epoch = $6::integer",
        "source_run.status IN ('pending', 'running', 'done', 'failed', 'aborted')",
        "session.status = 'active'",
    ):
        assert required_sql in query


def test_notification_reader_uses_only_actual_web_session_identity() -> None:
    read_sql = web_capture_evidence_repository._READ_NOTIFICATION_EVIDENCE_SQL
    list_sql = web_capture_evidence_repository._LIST_NOTIFICATION_CAPTURE_IDS_SQL

    assert "session.session_id = ('web:' || $2::uuid::text)" in read_sql
    assert "session.session_owner = 'web-v2-workflow'" in read_sql
    assert "session.session_id = ('web:' || run.id::text)" in list_sql
    assert "session.session_owner = 'web-v2-workflow'" in list_sql
    assert "web-page-type-assessment.v3" in read_sql
    assert "stored.assessment_payload" in read_sql
    assert "assessment.assessment_payload->>'inspection_evidence_role'" in read_sql
    assert "web-v2:" not in read_sql
    assert "web-v2:" not in list_sql
