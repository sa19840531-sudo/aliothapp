"""页面类型判断记录：裁定规则、来源拆分、台账容错、仓储 SQL、识别写库与网页评估挂接。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.page_type_judgements import (
    PageTypeJudgementInput,
    PageTypeJudgementLedger,
    group_judgements,
    h5_detection_judgements,
    judgement_from_mapping,
    source_from_confidence_source,
    web_assessment_judgements,
)
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
    WebPageTypeCatalogIdentity,
)
from services.api.app.application.web_run_policy import load_published_web_policy
from services.api.app.domain.page.type_judgement import (
    JudgementKind,
    PageTypeJudgement,
    PageTypeJudgementValidationError,
    ResolutionStatus,
    resolve_page_type,
)
from services.api.app.infrastructure.db.page_capture_repository import (
    PostgresPageCaptureRepository,
)
from services.api.app.infrastructure.db.page_type_judgement_repository import (
    PostgresPageTypeJudgementRepository,
)

TASK = "11111111-1111-1111-1111-111111111111"
RUN = "33333333-3333-3333-3333-333333333333"
CAPTURE = "44444444-4444-4444-4444-444444444444"


def _judgement(
    *,
    sequence_no: int,
    source: str,
    page_type: str | None,
    kind: JudgementKind = JudgementKind.JUDGEMENT,
    revises: str | None = None,
    finalization_state: str = "open",
    page_capture_id: str = CAPTURE,
) -> PageTypeJudgement:
    return PageTypeJudgement(
        judgement_id=f"judgement-{page_capture_id[:4]}-{sequence_no}",
        task_id=TASK,
        page_capture_id=page_capture_id,
        sequence_no=sequence_no,
        source=source,
        page_type=page_type,
        kind=kind,
        revises_judgement_id=revises,
        task_finalization_state=finalization_state,
        created_at=datetime(2026, 9, 9, 12, 0, sequence_no, tzinfo=UTC),
    )


# ---------- 裁定规则 ----------


def test_resolution_without_records_and_single_agreeing_sources() -> None:
    empty = resolve_page_type(CAPTURE, ())
    assert empty.status is ResolutionStatus.NO_JUDGEMENT
    assert empty.page_type is None

    single = resolve_page_type(
        CAPTURE, (_judgement(sequence_no=1, source="structure_rules", page_type="home"),)
    )
    assert single.status is ResolutionStatus.CONFIRMED
    assert single.page_type == "home"
    assert single.adopted_judgement_id == "judgement-4444-1"

    agreed = resolve_page_type(
        CAPTURE,
        (
            _judgement(sequence_no=1, source="structure_rules", page_type="home"),
            _judgement(sequence_no=2, source="model", page_type="home"),
        ),
    )
    assert agreed.status is ResolutionStatus.CONFIRMED
    assert agreed.current_by_source == {"structure_rules": "home", "model": "home"}
    assert agreed.adopted_judgement_id == "judgement-4444-2"


def test_conflicting_sources_resolve_to_unknown_and_keep_both() -> None:
    resolution = resolve_page_type(
        CAPTURE,
        (
            _judgement(sequence_no=1, source="structure_rules", page_type="home"),
            _judgement(sequence_no=2, source="model", page_type="content_list"),
        ),
    )

    assert resolution.status is ResolutionStatus.CONFLICTING
    assert resolution.page_type is None
    assert resolution.conflicting_page_types == ("content_list", "home")
    assert resolution.current_by_source == {"structure_rules": "home", "model": "content_list"}
    assert "不凭置信度硬定论" in resolution.reason
    assert resolution.to_dict()["status_label"] == "判断冲突"


def test_revision_appends_and_latest_agreeing_judgement_is_adopted() -> None:
    resolution = resolve_page_type(
        CAPTURE,
        (
            _judgement(sequence_no=1, source="structure_rules", page_type="home"),
            _judgement(sequence_no=2, source="model", page_type="content_list"),
            _judgement(
                sequence_no=3,
                source="structure_rules",
                page_type="content_list",
                kind=JudgementKind.REVISION,
                revises="judgement-4444-1",
            ),
        ),
    )

    assert resolution.status is ResolutionStatus.REVISED
    assert resolution.page_type == "content_list"
    assert resolution.adopted_judgement_id == "judgement-4444-3"
    assert len(resolution.revisions) == 1
    revision = resolution.revisions[0]
    assert (revision.from_page_type, revision.to_page_type) == ("home", "content_list")
    assert revision.source_label if hasattr(revision, "source_label") else True
    assert resolution.judgement_count == 3


def test_judgements_arriving_after_seal_are_kept_but_not_resolved() -> None:
    only_late = resolve_page_type(
        CAPTURE,
        (
            _judgement(
                sequence_no=1, source="model", page_type="home", finalization_state="sealed"
            ),
        ),
    )
    assert only_late.status is ResolutionStatus.UNKNOWN
    assert only_late.after_seal_count == 1

    mixed = resolve_page_type(
        CAPTURE,
        (
            _judgement(sequence_no=1, source="structure_rules", page_type="home"),
            _judgement(
                sequence_no=2,
                source="structure_rules",
                page_type="profile",
                finalization_state="finalizing",
            ),
        ),
    )
    assert mixed.status is ResolutionStatus.CONFIRMED
    assert mixed.page_type == "home"
    assert mixed.after_seal_count == 1
    assert mixed.revisions == ()


def test_all_sources_without_type_resolve_unknown_and_validation_rules() -> None:
    resolution = resolve_page_type(
        CAPTURE, (_judgement(sequence_no=1, source="web_assessment_model", page_type=None),)
    )
    assert resolution.status is ResolutionStatus.UNKNOWN

    with pytest.raises(PageTypeJudgementValidationError):
        _judgement(sequence_no=1, source="model", page_type="home", kind=JudgementKind.REVISION)
    with pytest.raises(PageTypeJudgementValidationError):
        _judgement(sequence_no=0, source="model", page_type="home")


def test_group_judgements_orders_pages_by_first_judgement() -> None:
    other = "55555555-5555-5555-5555-555555555555"
    pages = group_judgements(
        (
            _judgement(sequence_no=2, source="model", page_type="home", page_capture_id=other),
            _judgement(sequence_no=1, source="structure_rules", page_type="home"),
            _judgement(
                sequence_no=1, source="structure_rules", page_type="profile", page_capture_id=other
            ),
        )
    )
    assert [page.page_capture_id for page in pages] == [CAPTURE, other]
    assert pages[1].resolution.status is ResolutionStatus.CONFLICTING


# ---------- 来源拆分 ----------


def test_h5_detection_splits_structure_rules_and_model_judgements() -> None:
    inputs = h5_detection_judgements(
        task_id=TASK,
        agent_run_id=RUN,
        page_capture_id=CAPTURE,
        page_type="home",
        page_state="stable",
        detection_status="recognized",
        confidence=0.94,
        catalog_version="v1",
        catalog_sha256="sha256:catalog",
        confidence_source="deterministic_structure",
        model_page_type="content_list",
        model_confidence=0.61,
    )

    assert [(item.source, item.page_type) for item in inputs] == [
        ("structure_rules", "home"),
        ("model", "content_list"),
    ]
    assert inputs[0].basis["adopted_by_detection"] is True
    assert inputs[0].basis["catalog_sha256"] == "sha256:catalog"
    assert inputs[1].basis["adopted_by_detection"] is False
    assert inputs[1].confidence == 0.61
    assert source_from_confidence_source("model") == "model"
    assert source_from_confidence_source(None) == "detection"

    model_only = h5_detection_judgements(
        task_id=TASK,
        agent_run_id=RUN,
        page_capture_id=CAPTURE,
        page_type="home",
        page_state="stable",
        detection_status="recognized",
        confidence=0.7,
        catalog_version="v1",
        confidence_source="model",
        model_page_type="home",
    )
    assert [item.source for item in model_only] == ["model"]


def test_web_assessment_splits_model_and_observed_and_keeps_expected_as_basis() -> None:
    inputs = web_assessment_judgements(
        task_id=TASK,
        agent_run_id=RUN,
        page_capture_id=CAPTURE,
        proposed_page_type="home",
        observed_page_type="content_list",
        final_page_type=None,
        classification_status="needs_review",
        confidence=0.55,
        reason_code="conflict",
        conflict_reason_code="observed_vs_proposed",
        expected_page_type="home",
        evidence_artifact_ids=("shot-1", None, "text-1"),
    )

    assert [(item.source, item.page_type) for item in inputs] == [
        ("web_assessment_model", "home"),
        ("web_assessment_observed", "content_list"),
    ]
    assert inputs[0].basis["expected_page_type"] == "home"
    assert inputs[0].basis["evidence_artifact_ids"] == ["shot-1", "text-1"]
    assert inputs[0].basis["adopted_by_assessment"] is False
    assert inputs[1].basis["conflict_reason_code"] == "observed_vs_proposed"
    assert len(web_assessment_judgements(
        task_id=TASK,
        agent_run_id=RUN,
        page_capture_id=CAPTURE,
        proposed_page_type="home",
        observed_page_type=None,
        final_page_type="home",
        classification_status="classified",
        confidence=0.9,
    )) == 1


# ---------- 台账容错 ----------


class FailingRepository:
    async def record(self, *, inputs):
        raise RuntimeError("数据库不可用")

    async def list_for_task(self, *, task_id, agent_run_id=None):
        return ()


class RecordingRepository:
    def __init__(self) -> None:
        self.inputs: list[PageTypeJudgementInput] = []

    async def record(self, *, inputs):
        self.inputs.extend(inputs)
        return tuple(
            PageTypeJudgement(
                judgement_id=f"judgement-{index}",
                task_id=item.task_id,
                page_capture_id=item.page_capture_id,
                sequence_no=index + 1,
                source=item.source,
                page_type=item.page_type,
                basis=item.basis,
            )
            for index, item in enumerate(inputs)
        )

    async def list_for_task(self, *, task_id, agent_run_id=None):
        return ()


async def test_ledger_swallows_repository_failures() -> None:
    ledger = PageTypeJudgementLedger(repository=FailingRepository())
    recorded = await ledger.record(
        (
            PageTypeJudgementInput(
                task_id=TASK, page_capture_id=CAPTURE, source="model", page_type="home"
            ),
        )
    )
    assert recorded == ()
    assert await ledger.record(()) == ()


# ---------- 仓储 SQL ----------


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self, *, conflict_on_second: bool = False, fail_insert: bool = False) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.conflict_on_second = conflict_on_second
        self.fail_insert = fail_insert
        self.inserts = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return args[0]

    async def fetchrow(self, query: str, *args: Any) -> Any:
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "INSERT INTO qc_page_type_judgement" not in normalized:
            return None
        if self.fail_insert:
            raise RuntimeError("insert failed")
        self.inserts += 1
        if self.conflict_on_second and self.inserts == 2:
            return None
        return {
            "judgement_id": args[0],
            "task_id": args[1],
            "agent_run_id": args[3],
            "page_capture_id": args[2],
            "sequence_no": self.inserts,
            "source": args[4],
            "page_type": args[5],
            "page_state": args[6],
            "confidence": args[7],
            "catalog_version": args[8],
            "kind": "judgement",
            "revises_judgement_id": None,
            "basis": args[9],
            "basis_hash": args[10],
            "task_finalization_state": "open",
            "created_at": datetime(2026, 9, 9, tzinfo=UTC),
        }

    async def fetch(self, query: str, *args: Any):
        self.calls.append(("fetch", " ".join(query.split()), args))
        return []


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


async def test_repository_insert_is_append_only_revision_aware_and_idempotent() -> None:
    connection = FakeConnection(conflict_on_second=True)
    repository = PostgresPageTypeJudgementRepository(pool=FakePool(connection))

    recorded = await repository.record(
        inputs=(
            PageTypeJudgementInput(
                task_id=TASK,
                agent_run_id=RUN,
                page_capture_id=CAPTURE,
                source="structure_rules",
                page_type="home",
            ),
            PageTypeJudgementInput(
                task_id=TASK,
                agent_run_id=RUN,
                page_capture_id=CAPTURE,
                source="model",
                page_type="home",
            ),
        )
    )

    # 第二条撞了 (page_capture_id, source, basis_hash) 唯一键 → 不重复记
    assert [item.source for item in recorded] == ["structure_rules"]
    assert recorded[0].judgement_id.startswith("page-type-judgement:")
    locks = [call for call in connection.calls if "pg_advisory_xact_lock" in call[1]]
    assert len(locks) == 2 and locks[0][2] == (f"page-type-judgement:{CAPTURE}",)
    insert_query = next(
        call[1] for call in connection.calls if "INSERT INTO qc_page_type_judgement" in call[1]
    )
    assert "ON CONFLICT (page_capture_id, source, basis_hash) DO NOTHING" in insert_query
    assert "THEN 'revision'" in insert_query
    assert "previous.page_type IS DISTINCT FROM $6::varchar" in insert_query
    assert "COALESCE(MAX(sequence_no), 0) + 1" in insert_query
    assert "SELECT finalization_state FROM qc_task" in insert_query
    assert "UPDATE qc_page_capture" not in insert_query


def test_judgement_from_mapping_parses_jsonb_text_basis() -> None:
    judgement = judgement_from_mapping(
        {
            "judgement_id": "j-1",
            "task_id": TASK,
            "agent_run_id": None,
            "page_capture_id": CAPTURE,
            "sequence_no": 2,
            "source": "model",
            "page_type": "home",
            "page_state": None,
            "confidence": "0.5",
            "catalog_version": None,
            "kind": "revision",
            "revises_judgement_id": "j-0",
            "basis": '{"detection_status": "recognized"}',
            "task_finalization_state": None,
            "created_at": None,
        }
    )
    assert judgement.kind is JudgementKind.REVISION
    assert judgement.basis == {"detection_status": "recognized"}
    assert judgement.confidence == 0.5
    assert judgement.to_dict()["source_label"] == "模型判断"


# ---------- 识别写库挂接 ----------


class RecordingJudgementRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.detections: list[dict[str, Any]] = []
        self.fail = fail

    async def record_detection_on_connection(self, connection: Any, **detection: Any) -> None:
        self.detections.append(detection)
        if self.fail:
            raise RuntimeError("台账不可用")


async def test_detection_write_appends_judgements_in_the_same_transaction() -> None:
    connection = FakeConnection()
    judgements = RecordingJudgementRepository()
    repository = PostgresPageCaptureRepository(
        pool=FakePool(connection),
        page_graph_repository=None,
        page_type_judgement_repository=judgements,
    )

    updated = await repository.update_detection_result(
        task_id=TASK,
        agent_run_id=RUN,
        page_capture_id=CAPTURE,
        page_type="home",
        page_state="stable",
        detection_status="recognized",
        confidence=0.94,
        catalog_version="v1",
        catalog_sha256="sha256:catalog",
        confidence_source="deterministic_structure",
        model_page_type="content_list",
        model_confidence=0.6,
    )

    assert updated is True
    assert len(judgements.detections) == 1
    detection = judgements.detections[0]
    assert detection["page_capture_id"] == CAPTURE
    assert detection["page_type"] == "home"
    assert detection["model_page_type"] == "content_list"
    assert detection["confidence_source"] == "deterministic_structure"
    assert detection["catalog_sha256"] == "sha256:catalog"


async def test_detection_write_survives_judgement_ledger_failure() -> None:
    connection = FakeConnection()
    repository = PostgresPageCaptureRepository(
        pool=FakePool(connection),
        page_graph_repository=None,
        page_type_judgement_repository=RecordingJudgementRepository(fail=True),
    )

    updated = await repository.update_detection_result(
        task_id=TASK,
        agent_run_id=RUN,
        page_capture_id=CAPTURE,
        page_type="home",
        page_state="stable",
        detection_status="recognized",
        confidence=0.9,
        catalog_version="v1",
        catalog_sha256="sha256:catalog",
    )

    assert updated is True
    assert any("UPDATE qc_page_capture" in call[1] for call in connection.calls)


async def test_detection_write_without_ledger_stays_unchanged() -> None:
    connection = FakeConnection()
    repository = PostgresPageCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None, page_type_judgement_repository=None
    )
    assert await repository.update_detection_result(
        task_id=TASK,
        agent_run_id=RUN,
        page_capture_id=CAPTURE,
        page_type="home",
        page_state="stable",
        detection_status="recognized",
        confidence=0.9,
        catalog_version="v1",
        catalog_sha256="sha256:catalog",
    )
    assert not any("qc_page_type_judgement" in call[1] for call in connection.calls)


# ---------- 网页评估挂接 ----------


class WebAssessmentRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def persist_web_page_type_assessment(self, **kwargs):
        self.calls.append(kwargs)
        return "assessment-record-1"


def _web_capture() -> WebReadyPageCapture:
    return WebReadyPageCapture(
        capture_id="capture-1",
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="screenshot-1",
        dom_artifact_id="dom-1",
        visible_text_artifact_id="text-1",
        page_role="home",
        structure_facts=WebPageStructureFacts(
            navigation_element_count=1,
            repeated_link_count=3,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("content-grid",),
            primary_detail_link_count=3,
            repeated_primary_card_count=3,
        ),
        captured_at=datetime(2026, 9, 4, tzinfo=UTC),
        url="http://example.com/",
    )


async def test_web_recorder_appends_model_and_observed_judgements() -> None:
    ledger_repository = RecordingRepository()
    frozen_catalog = load_published_web_policy(
        now=datetime(2026, 9, 13, tzinfo=UTC),
        max_duration_seconds=600,
    )["page_catalog"]
    recorder = WebPageTypeAssessmentRecorder(
        repository=WebAssessmentRepository(),
        judgement_ledger=PageTypeJudgementLedger(repository=ledger_repository),
        catalog_identity=WebPageTypeCatalogIdentity.from_frozen_catalog_payload(
            frozen_catalog
        ),
    )

    result = await recorder.persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_web_capture(),
        assessment=WebPageAssessment(
            page_type=None,
            confidence=0.55,
            evidence=("nav",),
            needs_review_reason="observed page type differs",
            proposed_page_type="home",
            observed_page_type="content_list",
            expected_page_type="home",
            conflict_reason_code="observed_vs_proposed",
        ),
    )

    assert result.classification_status == "needs_review"
    assert [(item.source, item.page_type) for item in ledger_repository.inputs] == [
        ("web_assessment_model", "home"),
        ("web_assessment_observed", "content_list"),
    ]
    assert ledger_repository.inputs[0].basis["classification_status"] == "needs_review"
    assert ledger_repository.inputs[0].basis["evidence_artifact_ids"] == [
        "screenshot-1",
        "dom-1",
        "text-1",
    ]
    assert ledger_repository.inputs[0].page_capture_id == "capture-1"
