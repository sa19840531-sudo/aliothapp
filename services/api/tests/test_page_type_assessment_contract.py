"""页面类型评估领域合同单元测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain.exploration import (
    PageTypeAssessment,
    PageTypeAssessmentSet,
    PageTypeAssessmentStatus,
    PageTypeAssessmentValidationError,
    build_page_type_assessment,
    build_page_type_assessment_id,
    build_page_type_assessment_set,
)
from services.api.app.domain.page.registry import get_default_page_type_registry

_NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)


def test_build_covered_assessment_validates_catalog_and_evidence() -> None:
    registry = get_default_page_type_registry()

    assessment = build_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        page_type="home",
        status=PageTypeAssessmentStatus.COVERED,
        reason_code="recognized_with_evidence",
        page_capture_ids=("capture-home",),
        evidence_artifact_ids=("artifact-home",),
        page_states=("stable",),
        assessed_at=_NOW,
        registry=registry,
    )

    assert assessment.assessment_id == build_page_type_assessment_id(
        task_id="task-1",
        agent_run_id="run-1",
        page_type="home",
        catalog_sha256=registry.content_sha256,
    )
    assert assessment.catalog_version == registry.catalog.version
    assert assessment.catalog_sha256 == registry.content_sha256
    assert assessment.to_tool_payload() == {
        "page_type": "home",
        "status": "covered",
        "reason_code": "recognized_with_evidence",
        "page_capture_ids": ["capture-home"],
        "evidence_artifact_ids": ["artifact-home"],
        "exploration_action_ids": [],
    }


def test_unknown_page_type_fails_closed_before_assessment_is_built() -> None:
    with pytest.raises(PageTypeAssessmentValidationError, match="不属于业务覆盖 Catalog"):
        build_page_type_assessment(
            task_id="task-1",
            agent_run_id="run-1",
            page_type="unknown_detail",
            status=PageTypeAssessmentStatus.NOT_FOUND,
            reason_code="catalog_miss",
            assessed_at=_NOW,
            registry=get_default_page_type_registry(),
        )


def test_covered_assessment_requires_capture_evidence_and_page_state() -> None:
    with pytest.raises(PageTypeAssessmentValidationError, match="page_capture_ids"):
        build_page_type_assessment(
            task_id="task-1",
            agent_run_id="run-1",
            page_type="home",
            status=PageTypeAssessmentStatus.COVERED,
            reason_code="recognized_without_evidence",
            assessed_at=_NOW,
        )

    with pytest.raises(PageTypeAssessmentValidationError, match="page_states"):
        build_page_type_assessment(
            task_id="task-1",
            agent_run_id="run-1",
            page_type="home",
            status=PageTypeAssessmentStatus.COVERED,
            reason_code="recognized_without_state",
            page_capture_ids=("capture-home",),
            evidence_artifact_ids=("artifact-home",),
            assessed_at=_NOW,
        )


def test_reference_status_requires_auditable_reference() -> None:
    with pytest.raises(PageTypeAssessmentValidationError, match="必须引用可审计"):
        build_page_type_assessment(
            task_id="task-1",
            agent_run_id="run-1",
            page_type="payment",
            status=PageTypeAssessmentStatus.NOT_APPLICABLE,
            reason_code="not_in_product",
            assessed_at=_NOW,
        )


def test_assessment_rejects_duplicate_reference_ids() -> None:
    with pytest.raises(PageTypeAssessmentValidationError, match="不能包含重复值"):
        build_page_type_assessment(
            task_id="task-1",
            agent_run_id="run-1",
            page_type="home",
            status=PageTypeAssessmentStatus.COVERED,
            reason_code="recognized_with_evidence",
            page_capture_ids=("capture-home", "capture-home"),
            evidence_artifact_ids=("artifact-home",),
            page_states=("stable",),
            assessed_at=_NOW,
        )


def test_assessment_from_dict_round_trip_ignores_future_fields() -> None:
    assessment = build_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        page_type="home",
        status="covered",
        reason_code="recognized_with_evidence",
        page_capture_ids=("capture-home",),
        evidence_artifact_ids=("artifact-home",),
        page_states=("stable",),
        assessed_at=_NOW,
    )
    payload = assessment.to_dict()
    payload["future_field"] = "ignored"

    assert PageTypeAssessment.from_dict(payload).to_dict() == assessment.to_dict()


def test_assessment_from_dict_rejects_unknown_page_type_and_string_arrays() -> None:
    assessment = build_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        page_type="home",
        status="covered",
        reason_code="recognized_with_evidence",
        page_capture_ids=("capture-home",),
        evidence_artifact_ids=("artifact-home",),
        page_states=("stable",),
        assessed_at=_NOW,
    )

    unknown_page_type_payload = assessment.to_dict() | {
        "page_type": "unknown_detail",
        "assessment_id": build_page_type_assessment_id(
            task_id="task-1",
            agent_run_id="run-1",
            page_type="unknown_detail",
            catalog_sha256=assessment.catalog_sha256,
        ),
    }
    with pytest.raises(PageTypeAssessmentValidationError, match="不属于业务覆盖 Catalog"):
        PageTypeAssessment.from_dict(unknown_page_type_payload)

    string_array_payload = assessment.to_dict() | {"page_capture_ids": "capture-home"}
    with pytest.raises(PageTypeAssessmentValidationError, match="page_capture_ids 必须为数组"):
        PageTypeAssessment.from_dict(string_array_payload)


def test_assessment_id_changes_with_catalog_snapshot() -> None:
    base_id = build_page_type_assessment_id(
        task_id="task-1",
        agent_run_id="run-1",
        page_type="home",
        catalog_sha256="sha256:one",
    )
    changed_id = build_page_type_assessment_id(
        task_id="task-1",
        agent_run_id="run-1",
        page_type="home",
        catalog_sha256="sha256:two",
    )

    assert base_id != changed_id


def test_assessment_set_requires_complete_product_scope_when_marked_complete() -> None:
    registry = get_default_page_type_registry()
    first_page_type = registry.coverage_required_codes[0]
    assessment = build_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        page_type=first_page_type,
        status=PageTypeAssessmentStatus.EVIDENCE_INSUFFICIENT,
        reason_code="not_observed",
        assessed_at=_NOW,
        registry=registry,
    )

    with pytest.raises(PageTypeAssessmentValidationError, match="评估范围不完整"):
        build_page_type_assessment_set(
            task_id="task-1",
            agent_run_id="run-1",
            assessments=(assessment,),
            assessment_scope_complete=True,
            required_page_types=(first_page_type, registry.coverage_required_codes[1]),
            registry=registry,
        )


def test_assessment_set_defaults_to_dynamic_submitted_scope() -> None:
    registry = get_default_page_type_registry()
    page_type = registry.coverage_required_codes[0]
    assessment = build_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        page_type=page_type,
        status=PageTypeAssessmentStatus.EVIDENCE_INSUFFICIENT,
        reason_code="not_observed",
        assessed_at=_NOW,
        registry=registry,
    )

    assessment_set = build_page_type_assessment_set(
        task_id="task-1",
        agent_run_id="run-1",
        assessments=(assessment,),
        assessment_scope_complete=True,
        registry=registry,
    )

    assert assessment_set.required_page_types == (page_type,)
    assert PageTypeAssessmentSet.from_dict(assessment_set.to_dict()) == assessment_set


def test_complete_assessment_set_round_trip_and_tool_payloads() -> None:
    registry = get_default_page_type_registry()
    assessments = tuple(
        build_page_type_assessment(
            task_id="task-1",
            agent_run_id="run-1",
            page_type=page_type,
            status=PageTypeAssessmentStatus.EVIDENCE_INSUFFICIENT,
            reason_code="not_observed",
            assessed_at=_NOW,
            registry=registry,
        )
        for page_type in registry.coverage_required_codes
    )

    assessment_set = build_page_type_assessment_set(
        task_id="task-1",
        agent_run_id="run-1",
        assessments=assessments,
        assessment_scope_complete=True,
        registry=registry,
    )
    payload = assessment_set.to_dict()

    assert PageTypeAssessmentSet.from_dict(payload, registry=registry).to_dict() == payload
    assert len(assessment_set.to_tool_payloads()) == len(registry.coverage_required_codes)


def test_assessment_set_rejects_duplicate_page_type() -> None:
    assessment = build_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        page_type="home",
        status=PageTypeAssessmentStatus.EVIDENCE_INSUFFICIENT,
        reason_code="not_observed",
        assessed_at=_NOW,
    )

    with pytest.raises(PageTypeAssessmentValidationError, match="页面类型评估重复"):
        PageTypeAssessmentSet(
            task_id="task-1",
            agent_run_id="run-1",
            catalog_version=assessment.catalog_version,
            catalog_sha256=assessment.catalog_sha256,
            assessments=(assessment, assessment),
            assessment_scope_complete=False,
        )
