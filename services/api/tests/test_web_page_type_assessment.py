import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
    WebPageTypeAssessmentRecordingClassifier,
    WebPageTypeCatalogIdentity,
)
from services.api.app.application.web_readiness_contracts import (
    WebEvidenceRole,
    WebNetworkCapability,
    WebReadinessSignals,
    assess_web_readiness,
)


class Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def persist_web_page_type_assessment(self, **kwargs):
        self.calls.append(kwargs)
        return "assessment-record-1"


def _catalog_payload() -> dict[str, object]:
    path = Path(__file__).parents[1] / "app/domain/page/web_page_type_catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog_identity() -> WebPageTypeCatalogIdentity:
    return WebPageTypeCatalogIdentity.from_frozen_catalog_payload(_catalog_payload())


def _v4_recorder(repository: Repository, **kwargs) -> WebPageTypeAssessmentRecorder:
    return WebPageTypeAssessmentRecorder(
        repository=repository,
        catalog_identity=_catalog_identity(),
        **kwargs,
    )


def test_catalog_identity_hashes_complete_canonical_frozen_payload() -> None:
    baseline = _catalog_payload()
    variants = []
    navigation = deepcopy(baseline)
    navigation["navigation_signals"]["usage_notes"][0] += "（冻结变更）"
    variants.append(navigation)
    media = deepcopy(baseline)
    media["media_rules"]["content_detail"]["embedded_media_in_body"] += "（冻结变更）"
    variants.append(media)
    recognition = deepcopy(baseline)
    recognition["page_types"][0]["recognition_rules"]["required_any"][1]["value"] = 3
    variants.append(recognition)

    baseline_identity = WebPageTypeCatalogIdentity.from_frozen_catalog_payload(baseline)

    assert baseline_identity.catalog_version == baseline["version"]
    assert baseline_identity.catalog_sha256.startswith("sha256:")
    assert len({baseline_identity.catalog_sha256, *(
        WebPageTypeCatalogIdentity.from_frozen_catalog_payload(item).catalog_sha256
        for item in variants
    )}) == 4


@pytest.mark.asyncio
async def test_v4_recorder_fails_closed_without_frozen_catalog_identity() -> None:
    with pytest.raises(ValueError, match="冻结 Web Catalog"):
        await WebPageTypeAssessmentRecorder(repository=Repository()).persist(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            capture=_capture(),
            assessment=WebPageAssessment(
                page_type="home", confidence=0.90, evidence=("首页结构",),
            ),
        )


@pytest.mark.asyncio
async def test_age_gate_lifecycle_record_preserves_pre_gate_evidence():
    from dataclasses import replace

    from services.api.app.application import web_page_type_assessment as module
    from services.api.app.application.web_age_gate import WebAgeGateAssessment
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )
    from services.api.tests.test_web_age_gate import _evidence

    repository = Repository()
    entry = replace(_evidence(capture_id="pre-gate"),
        captured_at=datetime(2026, 9, 13, tzinfo=UTC), url="https://example.test/",
        supplemental_screenshot_artifact_ids=("full-page-pre-gate",),
        supplemental_evidence_status="captured")
    result = await module.WebAgeGatePageTypeRecorder(
        repository=repository, catalog_identity=_catalog_identity()
    ).record(
        evidence=entry, assessment=WebAgeGateAssessment(
            gate_present=True, confidence=0.75, reason="年龄门",
            confirmation_candidate_id=entry.candidates[0].candidate_id,
            human_verification_present=False))
    call = repository.calls[0]
    assert call["capture_id"] == "pre-gate"
    assert call["canonical_page_type"] == result.final_page_type == "age_gate"
    payload = call["assessment_payload"]
    assert payload["schema_version"] == "web-page-type-assessment.v4"
    assert payload["coverage_group"] == "lifecycle"
    assert payload["classification_status"] == "classified"
    assert payload["page_capture_ids"] == ["pre-gate"]
    assert payload["evidence_artifact_ids"] == ["pre-gate:screenshot", "pre-gate:dom", "full-page-pre-gate"]
    assert "inspection_evidence_role" not in payload
    assert call["audit_metadata"]["web_age_gate_lifecycle"] == {
        "pre_gate_capture_id": "pre-gate",
        "gate_present": True,
        "dismiss_action_id": None,
        "clicked_candidate_id": None,
        "post_gate_capture_id": None,
        "gate_disappeared": False,
        "supplemental_evidence_status": "captured",
        "supplemental_evidence_reason_code": None,
    }
    _validate_web_page_type_assessment_payload(canonical_page_type="age_gate",
        audit_metadata=call["audit_metadata"], assessment_payload=payload,
        expected_schema_version="web-page-type-assessment.v4",
        task_id=entry.task_id,
        agent_run_id=entry.agent_run_id,
        session_id=entry.session_id,
        capture_id=entry.capture_id,
        expected_catalog_identity=_catalog_identity())
    for version in ("web-page-type-assessment.v2", "web-page-type-assessment.v3"):
        audit = {"web_page_type_assessment": {"version": version}}
        with pytest.raises(ValueError):
            _validate_web_page_type_assessment_payload(canonical_page_type="age_gate",
                audit_metadata=audit, assessment_payload={**payload, "schema_version": version},
                expected_schema_version=version)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("schema_version", "web-page-type-assessment.v3"),
        ("evidence_artifact_ids", ["foreign-artifact"]),
        ("pre_gate_capture_id", "foreign-capture"),
        ("gate_present", 1),
        ("gate_disappeared", 0),
        ("attempt_count", True),
    ],
)
async def test_v4_age_gate_repository_rejects_direct_audit_payload_drift(
    field_name: str,
    tampered_value: object,
) -> None:
    from services.api.app.application import web_page_type_assessment as module
    from services.api.app.application.web_age_gate import WebAgeGateAssessment
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )
    from services.api.tests.test_web_age_gate import _evidence

    repository = Repository()
    identity = _catalog_identity()
    entry = replace(
        _evidence(capture_id="pre-gate"),
        captured_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    await module.WebAgeGatePageTypeRecorder(
        repository=repository,
        catalog_identity=identity,
    ).record(
        evidence=entry,
        assessment=WebAgeGateAssessment(
            gate_present=True,
            confidence=0.75,
            reason="年龄门",
            confirmation_candidate_id=entry.candidates[0].candidate_id,
            human_verification_present=False,
        ),
    )
    call = repository.calls[0]
    audit_metadata = deepcopy(call["audit_metadata"])
    audit_metadata["web_page_type_assessment"][field_name] = tampered_value

    with pytest.raises(ValueError, match="审计.*不一致"):
        _validate_web_page_type_assessment_payload(
            canonical_page_type="age_gate",
            audit_metadata=audit_metadata,
            assessment_payload=call["assessment_payload"],
            expected_schema_version="web-page-type-assessment.v4",
            task_id=entry.task_id,
            agent_run_id=entry.agent_run_id,
            session_id=entry.session_id,
            capture_id=entry.capture_id,
            expected_catalog_identity=identity,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("pre_gate_capture_id", "foreign-capture"),
        ("gate_present", False),
        ("gate_present", 1),
        ("dismiss_action_id", "foreign-action"),
        ("clicked_candidate_id", "foreign-candidate"),
        ("post_gate_capture_id", "foreign-post-capture"),
        ("gate_disappeared", True),
        ("gate_disappeared", 0),
    ],
)
async def test_v4_age_gate_repository_rejects_lifecycle_projection_drift(
    field_name: str,
    tampered_value: object,
) -> None:
    from services.api.app.application import web_page_type_assessment as module
    from services.api.app.application.web_age_gate import WebAgeGateAssessment
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )
    from services.api.tests.test_web_age_gate import _evidence

    repository = Repository()
    identity = _catalog_identity()
    entry = replace(
        _evidence(capture_id="pre-gate"),
        captured_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    await module.WebAgeGatePageTypeRecorder(
        repository=repository,
        catalog_identity=identity,
    ).record(
        evidence=entry,
        assessment=WebAgeGateAssessment(
            gate_present=True,
            confidence=0.75,
            reason="年龄门",
            confirmation_candidate_id=entry.candidates[0].candidate_id,
            human_verification_present=False,
        ),
    )
    call = repository.calls[0]
    audit_metadata = deepcopy(call["audit_metadata"])
    audit_metadata["web_age_gate_lifecycle"][field_name] = tampered_value

    with pytest.raises(ValueError, match="lifecycle.*不一致"):
        _validate_web_page_type_assessment_payload(
            canonical_page_type="age_gate",
            audit_metadata=audit_metadata,
            assessment_payload=call["assessment_payload"],
            expected_schema_version="web-page-type-assessment.v4",
            task_id=entry.task_id,
            agent_run_id=entry.agent_run_id,
            session_id=entry.session_id,
            capture_id=entry.capture_id,
            expected_catalog_identity=identity,
        )


@pytest.mark.asyncio
async def test_v4_age_gate_repository_rejects_missing_lifecycle_projection_field() -> None:
    from services.api.app.application import web_page_type_assessment as module
    from services.api.app.application.web_age_gate import WebAgeGateAssessment
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )
    from services.api.tests.test_web_age_gate import _evidence

    repository = Repository()
    identity = _catalog_identity()
    entry = replace(
        _evidence(capture_id="pre-gate"),
        captured_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    await module.WebAgeGatePageTypeRecorder(
        repository=repository,
        catalog_identity=identity,
    ).record(
        evidence=entry,
        assessment=WebAgeGateAssessment(
            gate_present=True,
            confidence=0.75,
            reason="年龄门",
            confirmation_candidate_id=entry.candidates[0].candidate_id,
            human_verification_present=False,
        ),
    )
    call = repository.calls[0]
    audit_metadata = deepcopy(call["audit_metadata"])
    del audit_metadata["web_age_gate_lifecycle"]["pre_gate_capture_id"]

    with pytest.raises(ValueError, match="lifecycle.*缺失"):
        _validate_web_page_type_assessment_payload(
            canonical_page_type="age_gate",
            audit_metadata=audit_metadata,
            assessment_payload=call["assessment_payload"],
            expected_schema_version="web-page-type-assessment.v4",
            task_id=entry.task_id,
            agent_run_id=entry.agent_run_id,
            session_id=entry.session_id,
            capture_id=entry.capture_id,
            expected_catalog_identity=identity,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("present,human,confidence", [(False, False, 0.99), (True, True, 0.99), (True, False, 0.69)])
async def test_age_gate_recorder_rejects_non_formal_lifecycle_evidence(present, human, confidence):
    from services.api.app.application import web_page_type_assessment as module
    from services.api.app.application.web_age_gate import WebAgeGateAssessment
    from services.api.tests.test_web_age_gate import _evidence

    repository = Repository()
    entry = _evidence(capture_id="pre-gate")
    assessment = WebAgeGateAssessment(gate_present=present, confidence=confidence,
        reason="不可正式分类", human_verification_present=human,
        confirmation_candidate_id=entry.candidates[0].candidate_id if present and not human else None)
    with pytest.raises(ValueError):
        await module.WebAgeGatePageTypeRecorder(
            repository=repository, catalog_identity=_catalog_identity()
        ).record(
            evidence=entry, assessment=assessment)
    assert repository.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_age_gate_recorder_hydrates_legacy_readiness_evidence_from_trusted_reader(foreign):
    from dataclasses import replace

    from services.api.app.application import web_page_type_assessment as module
    from services.api.app.application.web_age_gate import WebAgeGateAssessment
    from services.api.tests.test_web_age_gate import _evidence

    entry = _evidence(capture_id="pre-gate")
    repository = Repository()

    class Reader:
        async def read(self, *, capture_id):
            assert capture_id == "pre-gate"
            return replace(entry, task_id="foreign" if foreign else entry.task_id,
                captured_at=datetime(2026, 9, 13, tzinfo=UTC))

    recorder = module.WebAgeGatePageTypeRecorder(
        repository=repository,
        evidence_reader=Reader(),
        catalog_identity=_catalog_identity(),
    )
    assessment = WebAgeGateAssessment(gate_present=True, confidence=0.9,
        confirmation_candidate_id=entry.candidates[0].candidate_id,
        reason="年龄门", human_verification_present=False)
    if foreign:
        with pytest.raises(ValueError, match="归属"):
            await recorder.record(evidence=entry, assessment=assessment)
        assert repository.calls == []
    else:
        await recorder.record(evidence=entry, assessment=assessment)
        assert repository.calls[0]["assessment_payload"]["assessed_at"] == "2026-09-13T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", [
    "home", "content_list", "content_detail", "video_list", "video_detail",
])
@pytest.mark.parametrize("confidence", [0.699, 0.70])
async def test_recorder_direct_call_enforces_formal_confidence_floor(page_type, confidence):
    repository = Repository()
    result = await _v4_recorder(repository).persist(
        task_id="task-1", agent_run_id="run-1", session_id="web:run-1", capture=_capture(),
        assessment=WebPageAssessment(
            page_type=page_type, confidence=confidence, evidence=("候选",),
        ),
    )
    call = repository.calls[0]
    payload = call["assessment_payload"]
    audit = call["audit_metadata"]["web_page_type_assessment"]
    assert payload["confidence"] == audit["confidence"] == confidence
    if confidence < 0.70:
        assert result.proposed_page_type == "unknown"
        assert result.final_page_type == "unknown"
        assert result.classification_status == "needs_review"
        assert call["canonical_page_type"] == "unknown"
        assert payload["page_type"] == "unknown"
        assert payload["final_page_type"] == "unknown"
        assert payload["coverage_group"] == "neither"
        assert payload["status"] == "evidence_insufficient"
        assert payload["detection_status"] == "needs_review"
        assert payload["needs_review"] is audit["needs_review"] is True
        assert payload["reason_code"] == audit["reason_code"] == "WEB_PAGE_TYPE_LOW_CONFIDENCE"
        assert "inspection_evidence_role" not in payload
    else:
        assert result.proposed_page_type == page_type
        assert result.final_page_type == call["canonical_page_type"] == page_type
        assert result.classification_status == "classified"
        assert payload["detection_status"] == "recognized"


@pytest.mark.asyncio
async def test_recording_classifier_does_not_return_low_confidence_formal_type():
    repository = Repository()

    class Classifier:
        async def classify(self, capture):
            return WebPageAssessment(page_type="video_detail", confidence=0.60, evidence=("候选",))

    result = await WebPageTypeAssessmentRecordingClassifier(
        classifier=Classifier(), recorder=_v4_recorder(repository),
        task_id="task-1", agent_run_id="run-1", session_id="web:run-1",
    ).classify(_capture())
    assert result.page_type == "unknown"
    assert result.confidence == 0.60
    assert result.needs_review is True
    assert repository.calls[0]["canonical_page_type"] == "unknown"


@pytest.mark.asyncio
async def test_v4_unknown_is_persisted_as_needs_review_with_supplemental_lineage() -> None:
    repository = Repository()
    capture = replace(
        _capture(),
        supplemental_screenshot_artifact_ids=("full-page-1",),
        supplemental_evidence_status="captured",
    )

    result = await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=capture,
        assessment=WebPageAssessment(
            page_type="unknown",
            confidence=0.42,
            evidence=("主证据冲突",),
            needs_review_reason="WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT",
            needs_review=True,
        ),
    )

    call = repository.calls[0]
    payload = call["assessment_payload"]
    assert result.final_page_type == call["canonical_page_type"] == "unknown"
    assert result.classification_status == "needs_review"
    assert payload["classification_status"] == "needs_review"
    assert payload["detection_status"] == "needs_review"
    assert payload["coverage_group"] == "neither"
    assert payload["evidence_artifact_ids"] == [
        "screenshot-1", "dom-1", "text-1", "full-page-1",
    ]
    assert payload["supplemental_evidence_status"] == "captured"
    assert payload["supplemental_evidence_reason_code"] is None


@pytest.mark.asyncio
async def test_v4_explicit_unknown_forces_review_boolean_even_without_model_flag() -> None:
    repository = Repository()

    await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type="unknown",
            confidence=0.70,
            evidence=("无法确定业务页型",),
        ),
    )

    call = repository.calls[0]
    assert call["assessment_payload"]["needs_review"] is True
    assert call["audit_metadata"]["web_page_type_assessment"]["needs_review"] is True


@pytest.mark.asyncio
async def test_v3_assessment_payload_ignores_v4_supplemental_fields() -> None:
    repository = Repository()
    capture = replace(
        _capture(),
        supplemental_screenshot_artifact_ids=("full-page-v4-only",),
        supplemental_evidence_status="captured",
    )

    await WebPageTypeAssessmentRecorder(
        repository=repository,
        assessment_schema_version="web-page-type-assessment.v3",
    ).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=capture,
        assessment=WebPageAssessment(
            page_type="content_list",
            confidence=0.90,
            evidence=("重复内容卡片",),
        ),
    )

    call = repository.calls[0]
    expected = ["screenshot-1", "dom-1", "text-1"]
    assert call["assessment_payload"]["evidence_artifact_ids"] == expected
    assert call["audit_metadata"]["web_page_type_assessment"]["evidence"][
        "artifact_ids"
    ] == expected
    assert "supplemental_evidence_status" not in call["assessment_payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", ["activity", "search_page", "collection_page"])
async def test_v4_classifies_optional_business_page_types(page_type: str) -> None:
    repository = Repository()
    result = await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type=page_type,
            confidence=0.91,
            evidence=("独立业务主体",),
        ),
    )

    payload = repository.calls[0]["assessment_payload"]
    assert result.final_page_type == page_type
    assert payload["coverage_group"] == "business"
    assert payload["classification_status"] == "classified"


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", ["collection_page", "guide_page", "recharge_page", "topup_page"])
async def test_current_catalog_page_types_pass_repository_validation(page_type: str) -> None:
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )

    repository = Repository()
    identity = _catalog_identity()
    await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type=page_type,
            confidence=0.91,
            evidence=("目录页型证据",),
        ),
    )
    call = repository.calls[0]
    _validate_web_page_type_assessment_payload(
        canonical_page_type=call["canonical_page_type"],
        audit_metadata=call["audit_metadata"],
        assessment_payload=call["assessment_payload"],
        expected_schema_version="web-page-type-assessment.v4",
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        expected_catalog_identity=identity,
    )


@pytest.mark.asyncio
async def test_v4_repository_validator_rejects_tampered_identity_and_evidence_contract() -> None:
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )

    repository = Repository()
    identity = _catalog_identity()
    await WebPageTypeAssessmentRecorder(
        repository=repository,
        catalog_identity=identity,
    ).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type="home", confidence=0.91, evidence=("首页结构",),
        ),
    )
    call = repository.calls[0]
    common = {
        "canonical_page_type": "home",
        "audit_metadata": call["audit_metadata"],
        "assessment_payload": call["assessment_payload"],
        "expected_schema_version": "web-page-type-assessment.v4",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "session_id": "web:run-1",
        "capture_id": "capture-1",
        "expected_catalog_identity": identity,
    }
    _validate_web_page_type_assessment_payload(**common)

    mutations = (
        {"task_id": "foreign-task"},
        {"session_id": "web:foreign-run"},
        {"page_capture_ids": ["foreign-capture"]},
        {"catalog_version": "foreign-version"},
        {"catalog_sha256": "sha256:" + "0" * 64},
        {"primary_evidence_artifact_ids": ["screenshot-1", "dom-1"]},
        {"supplemental_screenshot_artifact_ids": ["foreign-supplement"]},
        {"supplemental_evidence_status": "captured"},
        {"status": "covered", "coverage_group": "neither"},
    )
    for mutation in mutations:
        payload = {**call["assessment_payload"], **mutation}
        with pytest.raises(ValueError):
            _validate_web_page_type_assessment_payload(
                **{**common, "assessment_payload": payload}
            )

    nonformal_unknown = {
        **call["assessment_payload"],
        "page_type": "unknown",
        "final_page_type": None,
        "coverage_group": "neither",
        "status": "evidence_insufficient",
        "classification_status": "needs_review",
        "detection_status": "needs_review",
        "needs_review": True,
    }
    nonformal_unknown_audit = deepcopy(call["audit_metadata"])
    for field_name in (
        "page_type",
        "final_page_type",
        "proposed_page_type",
        "raw_page_type",
        "coverage_group",
        "status",
        "classification_status",
        "detection_status",
        "needs_review",
    ):
        nonformal_unknown_audit["web_page_type_assessment"][field_name] = (
            nonformal_unknown[field_name]
        )
    with pytest.raises(ValueError, match="unknown"):
        _validate_web_page_type_assessment_payload(
            **{
                **common,
                "canonical_page_type": None,
                "audit_metadata": nonformal_unknown_audit,
                "assessment_payload": nonformal_unknown,
            }
        )

    required_fields = (
        "assessment_id",
        "task_id",
        "agent_run_id",
        "session_id",
        "page_type",
        "final_page_type",
        "coverage_group",
        "classification_status",
        "detection_status",
        "status",
        "needs_review",
        "confidence",
        "reason_code",
        "reason_codes",
        "assessed_at",
        "page_capture_ids",
        "evidence_artifact_ids",
        "primary_evidence_artifact_ids",
        "supplemental_screenshot_artifact_ids",
        "supplemental_evidence_status",
        "supplemental_evidence_reason_code",
        "page_states",
        "web_capture_stage",
        "page_role",
        "attempt_count",
        "catalog_version",
        "catalog_sha256",
    )
    for field_name in required_fields:
        payload = dict(call["assessment_payload"])
        del payload[field_name]
        with pytest.raises(ValueError):
            _validate_web_page_type_assessment_payload(
                **{**common, "assessment_payload": payload}
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("confidence", 0.01),
        ("classification_status", "needs_review"),
        ("status", "evidence_insufficient"),
        ("page_type", "unknown"),
        ("task_id", "foreign-task"),
        ("agent_run_id", "foreign-run"),
        ("page_capture_ids", ["foreign-capture"]),
        ("catalog_sha256", "sha256:" + "0" * 64),
        ("assessment_id", "foreign-assessment"),
        ("guide_version", "foreign-guide"),
        ("needs_review", 0),
        ("attempt_count", True),
    ],
)
async def test_v4_repository_rejects_audit_payload_duplicate_field_drift(
    field_name: str,
    tampered_value: object,
) -> None:
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )

    repository = Repository()
    identity = _catalog_identity()
    await WebPageTypeAssessmentRecorder(
        repository=repository,
        catalog_identity=identity,
    ).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type="home", confidence=0.91, evidence=("首页结构",),
        ),
    )
    call = repository.calls[0]
    audit_metadata = deepcopy(call["audit_metadata"])
    audit_metadata["web_page_type_assessment"][field_name] = tampered_value

    with pytest.raises(ValueError, match="审计.*不一致"):
        _validate_web_page_type_assessment_payload(
            canonical_page_type="home",
            audit_metadata=audit_metadata,
            assessment_payload=call["assessment_payload"],
            expected_schema_version="web-page-type-assessment.v4",
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            capture_id="capture-1",
            expected_catalog_identity=identity,
        )


@pytest.mark.asyncio
async def test_v4_repository_rejects_int_float_duplicate_field_type_confusion() -> None:
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )

    repository = Repository()
    identity = _catalog_identity()
    await WebPageTypeAssessmentRecorder(
        repository=repository,
        catalog_identity=identity,
    ).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type="home",
            confidence=1.0,
            evidence=("首页结构",),
        ),
    )
    call = repository.calls[0]
    audit_metadata = deepcopy(call["audit_metadata"])
    audit_metadata["web_page_type_assessment"]["confidence"] = 1

    with pytest.raises(ValueError, match="审计.*不一致"):
        _validate_web_page_type_assessment_payload(
            canonical_page_type="home",
            audit_metadata=audit_metadata,
            assessment_payload=call["assessment_payload"],
            expected_schema_version="web-page-type-assessment.v4",
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            capture_id="capture-1",
            expected_catalog_identity=identity,
        )


@pytest.mark.asyncio
async def test_v4_repository_duplicate_object_order_is_not_semantic_drift() -> None:
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        _validate_web_page_type_assessment_payload,
    )

    repository = Repository()
    identity = _catalog_identity()
    await WebPageTypeAssessmentRecorder(
        repository=repository,
        catalog_identity=identity,
    ).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type="home",
            confidence=0.91,
            evidence=("首页结构",),
        ),
    )
    call = repository.calls[0]
    payload = {
        **call["assessment_payload"],
        "comparison_probe": {"first": 1, "second": ["a", "b"]},
    }
    audit_metadata = deepcopy(call["audit_metadata"])
    audit_metadata["web_page_type_assessment"]["comparison_probe"] = {
        "second": ["a", "b"],
        "first": 1,
    }

    _validate_web_page_type_assessment_payload(
        canonical_page_type="home",
        audit_metadata=audit_metadata,
        assessment_payload=payload,
        expected_schema_version="web-page-type-assessment.v4",
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        expected_catalog_identity=identity,
    )


def _capture() -> WebReadyPageCapture:
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
        url="http://EXAMPLE.com/?utm_source=feed",
        inspection_readiness=assess_web_readiness(
            WebReadinessSignals(
                identity_before="https://example.com/",
                identity_after="https://example.com/",
                artifacts_same_epoch=True,
                capture_failed=False,
                timed_out=False,
                core_present=True,
                visible_loader=False,
                transition_active=False,
                necessary_media_ready=True,
                dom_quiet_ms=1200,
                minimum_dom_quiet_ms=800,
                network_capability=WebNetworkCapability.OBSERVABLE,
                network_required=False,
                critical_network_pending=0,
                settle_complete=True,
                quorum_met=True,
                capture_consistent=True,
                profile_version="web-structure.v1",
            ),
            role=WebEvidenceRole.INSPECTION,
        ),
    )


@pytest.mark.asyncio
async def test_recorder_persists_formal_type_without_legacy_mapping_metadata() -> None:
    repository = Repository()
    result = await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type="content_list",
            confidence=0.94,
            evidence=("首页主体是重复文章卡片",),
        ),
    )

    call = repository.calls[0]
    assert result.classification_status == "classified"
    assert result.final_page_type == "content_list"
    assert call["canonical_page_type"] == "content_list"
    assert "web_page_type_normalization" not in call["audit_metadata"]
    assert call["audit_metadata"]["web_page_type_assessment"]["version"] == (
        "web-page-type-assessment.v4"
    )
    assert call["assessment_payload"]["page_type"] == "content_list"
    assert call["assessment_payload"]["page_states"] == ["stable"]
    assert call["assessment_payload"]["schema_version"] == (
        "web-page-type-assessment.v4"
    )
    assert call["assessment_payload"]["page_role"] == "home"
    assert call["audit_metadata"]["logical_page_id"].startswith("web-logical:")
    assert call["audit_metadata"]["capture_purpose"] == "web_page_ready_baseline"
    assert call["audit_metadata"]["web_product_capability_assessment"] == {
        "capabilities": ["navigation", "content"],
        "entry_status": "known",
        "visible_layer": "business",
        "reason_codes": [],
    }
    guide_version = call["audit_metadata"]["web_page_type_assessment"]["guide_version"]
    assert guide_version
    assert call["assessment_payload"]["guide_version"] == guide_version
    assert call["assessment_payload"]["needs_review"] is False
    assert call["audit_metadata"]["web_inspection_readiness"] == {
        "role": "inspection",
        "status": "stable",
        "blocking_reasons": [],
    }
    assert call["assessment_payload"]["inspection_evidence_role"] == "inspection"


@pytest.mark.asyncio
async def test_recorder_keeps_product_capability_unknown_without_inspection_readiness() -> None:
    """恢复硬编码 stable 时，本测试应阻止缺少就绪事实的能力被记为已知。"""

    repository = Repository()
    capture = _capture()
    capture = WebReadyPageCapture(
        **{
            **capture.__dict__,
            "inspection_readiness": None,
        }
    )

    await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=capture,
        assessment=WebPageAssessment(
            page_type="content_list",
            confidence=0.94,
            evidence=("页面存在重复文章卡片",),
        ),
    )

    capability = repository.calls[0]["audit_metadata"]["web_product_capability_assessment"]
    assert capability == {
        "capabilities": [],
        "entry_status": "unknown",
        "visible_layer": "business",
        "reason_codes": ["business_layer_not_stable"],
    }


@pytest.mark.asyncio
async def test_recorder_persists_zero_structure_as_not_observed() -> None:
    """真实保存出口不得把稳定 Capture 的零结构计数写成确认不存在。"""

    repository = Repository()
    capture = _capture()
    capture = WebReadyPageCapture(
        **{
            **capture.__dict__,
            "structure_facts": WebPageStructureFacts(
                navigation_element_count=0,
                repeated_link_count=0,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=(),
            ),
        }
    )

    await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=capture,
        assessment=WebPageAssessment(
            page_type=None,
            confidence=0.2,
            evidence=("当前结构无法观察业务能力",),
            needs_review_reason="WEB_PAGE_STRUCTURE_INSUFFICIENT",
        ),
    )

    capability = repository.calls[0]["audit_metadata"]["web_product_capability_assessment"]
    assert capability == {
        "capabilities": [],
        "entry_status": "unknown",
        "visible_layer": "business",
        "reason_codes": ["business_capability_not_observed"],
    }


@pytest.mark.asyncio
async def test_recorder_keeps_proposed_type_and_reason_for_review() -> None:
    repository = Repository()
    result = await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type=None,
            raw_page_type="video_list",
            confidence=0.61,
            evidence=("列表结构与内容类型冲突",),
            needs_review_reason="URL_HINT_DOM_CONFLICT",
        ),
    )

    payload = repository.calls[0]["assessment_payload"]
    assert result.classification_status == "needs_review"
    assert result.proposed_page_type == "video_list"
    assert payload["page_type"] == "video_list"
    assert payload["reason_code"] == "URL_HINT_DOM_CONFLICT"
    assert payload["page_states"] == ["stable"]


@pytest.mark.asyncio
async def test_recorder_persists_sanitized_raw_candidate_separately_from_proposed() -> None:
    repository = Repository()
    raw_candidate = "  Mystery Candidate token=do-not-store " + ("x" * 100)

    result = await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type=None,
            raw_page_type=raw_candidate,
            proposed_page_type="  MYSTERY_CANDIDATE  ",
            confidence=0.42,
            evidence=("目录外模型候选",),
            needs_review_reason="WEB_PAGE_TYPE_UNSUPPORTED",
        ),
    )

    call = repository.calls[0]
    metadata = call["audit_metadata"]["web_page_type_assessment"]
    payload = call["assessment_payload"]
    assert result.final_page_type is None
    assert result.proposed_page_type == "mystery_candidate"
    assert metadata["raw_page_type"] == payload["raw_page_type"]
    assert metadata["raw_page_type"].startswith(
        "Mystery Candidate token=[redacted]"
    )
    assert len(metadata["raw_page_type"]) == 80
    assert metadata["proposed_page_type"] == "mystery_candidate"
    assert payload["proposed_page_type"] == "mystery_candidate"
    assert payload["final_page_type"] is None


@pytest.mark.asyncio
async def test_recorder_persists_expected_observed_conflict_without_downgrade() -> None:
    repository = Repository()
    assessment = WebPageAssessment(
        page_type="content_detail",
        raw_page_type="content_detail",
        proposed_page_type="content_detail",
        observed_page_type="content_detail",
        expected_page_type="video_list",
        confidence=0.94,
        evidence=("正文根和长段落构成高置信 DOM 事实",),
        conflict_reason_code="URL_HINT_DOM_CONFLICT",
    )

    result = await _v4_recorder(repository).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_capture(),
        assessment=assessment,
    )

    call = repository.calls[0]
    metadata = call["audit_metadata"]["web_page_type_assessment"]
    payload = call["assessment_payload"]
    assert result.classification_status == "classified"
    assert result.final_page_type == "content_detail"
    assert metadata["expected_page_type"] == "video_list"
    assert metadata["observed_page_type"] == "content_detail"
    assert metadata["proposed_page_type"] == "content_detail"
    assert metadata["conflict_reason_code"] == "URL_HINT_DOM_CONFLICT"
    assert payload["expected_page_type"] == "video_list"
    assert payload["observed_page_type"] == "content_detail"
    assert payload["conflict_reason_code"] == "URL_HINT_DOM_CONFLICT"


@pytest.mark.asyncio
async def test_recording_classifier_retries_unclassified_same_evidence_at_most_twice() -> None:
    repository = Repository()

    class Classifier:
        def __init__(self) -> None:
            self.calls = 0

        async def classify(self, capture):
            assert capture.capture_id == "capture-1"
            self.calls += 1
            return WebPageAssessment(
                page_type=None,
                confidence=0.3,
                evidence=("结构证据不足",),
                needs_review_reason="WEB_PAGE_STRUCTURE_INSUFFICIENT",
            )

    classifier = Classifier()
    result = await WebPageTypeAssessmentRecordingClassifier(
        classifier=classifier,
        recorder=_v4_recorder(repository),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
    ).classify(_capture())

    assert classifier.calls == 2
    assert result.page_type is None
    assert len(repository.calls) == 1
    assert repository.calls[0]["assessment_payload"]["attempt_count"] == 2


@pytest.mark.asyncio
async def test_recording_classifier_classifies_primary_supplement_only_once() -> None:
    repository = Repository()

    class Classifier:
        def __init__(self) -> None:
            self.calls = 0

        async def classify(self, capture):
            assert capture.capture_purpose == "web_primary_content_evidence"
            self.calls += 1
            return WebPageAssessment(
                page_type=None,
                raw_page_type="content_list",
                confidence=0.7,
                evidence=("补图仍被固定广告遮挡",),
                needs_review_reason="WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT",
            )

    classifier = Classifier()
    supplement = _capture()
    supplement = WebReadyPageCapture(
        **{
            **supplement.__dict__,
            "capture_id": "capture-supplement",
            "web_capture_stage": "web_primary_content_evidence",
            "capture_purpose": "web_primary_content_evidence",
        }
    )
    result = await WebPageTypeAssessmentRecordingClassifier(
        classifier=classifier,
        recorder=_v4_recorder(repository),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
    ).classify(supplement)

    assert result.page_type is None
    assert classifier.calls == 1
    assert repository.calls[0]["assessment_payload"]["attempt_count"] == 1

@pytest.mark.asyncio
async def test_resumed_four_type_run_keeps_v2_assessment_schema() -> None:
    repository = Repository()
    await WebPageTypeAssessmentRecorder(
        repository=repository, assessment_schema_version="web-page-type-assessment.v2"
    ).persist(
        task_id="task-1", agent_run_id="run-1", session_id="web:run-1",
        capture=_capture(),
        assessment=WebPageAssessment(
            page_type="content_list", confidence=0.94, evidence=("重复文章卡片",),
        ),
    )
    call = repository.calls[0]
    assert call["assessment_payload"]["schema_version"] == "web-page-type-assessment.v2"
    assert call["audit_metadata"]["web_page_type_assessment"]["version"] == (
        "web-page-type-assessment.v2"
    )
