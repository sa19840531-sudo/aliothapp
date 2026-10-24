import hashlib
import json
from dataclasses import FrozenInstanceError
from importlib import import_module
from types import SimpleNamespace

import pytest


def _snapshot_module():
    return import_module(
        "services.api.app.domain.exploration.web_coverage_snapshot"
    )


def _build_snapshot(**overrides):
    module = _snapshot_module()
    values = {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "finalization_epoch": 3,
        "required_page_types": [" video_detail ", "content_list", "content_list", ""],
        "observed_page_types": ["CONTENT_LIST", " "],
        "unrecognized_page_types": [" web_unknown ", "WEB_UNKNOWN", ""],
    }
    values.update(overrides)
    return module.WebCoverageSnapshot.create(**values)


def test_web_coverage_snapshot_normalizes_sets_and_derives_missing_types() -> None:
    snapshot = _build_snapshot()

    assert snapshot.required_page_types == ("content_list", "video_detail")
    assert snapshot.observed_page_types == ("content_list",)
    assert snapshot.missing_page_types == ("video_detail",)
    assert snapshot.unrecognized_page_types == ("web_unknown",)
    assert snapshot.coverage_status == "completed_with_gaps"


def test_web_coverage_snapshot_uses_only_frozen_applicable_denominator() -> None:
    snapshot = _build_snapshot(
        required_page_types=["video_list", "video_detail"],
        optional_page_types=["activity", "content_list", "search_page"],
        not_applicable_page_types=["content_detail", "home"],
        observed_page_types=["video_list", "video_detail"],
        applicability_mode="catalog_declared",
        catalog_version="2026-09-04.v3",
        catalog_hash="a" * 64,
    )

    assert snapshot.missing_page_types == ()
    assert snapshot.coverage_status == "complete"
    assert snapshot.optional_page_types == ("activity", "content_list", "search_page")
    assert snapshot.not_applicable_page_types == ("content_detail", "home")
    assert snapshot.applicability_mode == "catalog_declared"


@pytest.mark.parametrize(
    "changes",
    [
        {
            "optional_page_types": ["activity", "content_detail", "search_page"],
            "not_applicable_page_types": ["content_list", "home"],
        },
        {"applicability_mode": "strict_four"},
        {"catalog_version": "2026-09-05.v4"},
        {"catalog_hash": "b" * 64},
    ],
)
def test_web_coverage_snapshot_hash_covers_applicability_contract(
    changes: dict[str, object],
) -> None:
    values = {
        "required_page_types": ["video_list", "video_detail"],
        "optional_page_types": ["activity", "content_list", "search_page"],
        "not_applicable_page_types": ["content_detail", "home"],
        "observed_page_types": ["video_list"],
        "applicability_mode": "catalog_declared",
        "catalog_version": "2026-09-04.v3",
        "catalog_hash": "a" * 64,
    }
    baseline = _build_snapshot(**values)
    values.update(changes)

    assert _build_snapshot(**values).snapshot_hash != baseline.snapshot_hash


def test_web_coverage_snapshot_includes_optional_collection_business_type() -> None:
    module = _snapshot_module()
    assessment = import_module(
        "services.api.app.application.web_page_type_assessment"
    )
    expected = frozenset({
        "home", "content_list", "content_detail", "video_list", "video_detail",
        "activity", "search_page", "collection_page",
    })

    assert module.WEB_CANONICAL_PAGE_TYPES == expected
    assert assessment.WEB_CANONICAL_PAGE_TYPES == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("web-page-type-assessment.v2", {"content_list", "content_detail", "video_list", "video_detail"}),
        ("web-page-type-assessment.v3", {"home", "content_list", "content_detail", "video_list", "video_detail"}),
        ("web-page-type-assessment.v4", {"home", "content_list", "content_detail", "video_list", "video_detail", "activity", "search_page"}),
    ],
)
def test_coverage_version_keeps_frozen_business_denominator(version, expected) -> None:
    assert _snapshot_module()._canonical_types(version) == expected


def test_v4_coverage_keeps_lifecycle_unknown_and_supplemental_outside_business_partition() -> None:
    snapshot = _build_snapshot(
        required_page_types=("home", "content_list"),
        optional_page_types=("activity", "search_page"),
        not_applicable_page_types=("content_detail", "video_detail", "video_list"),
        observed_page_types=("home",),
        unrecognized_page_types=(),
        observed_lifecycle_page_types=("age_gate",),
        unknown_page_capture_ids=("capture-unknown",),
        supplemental_evidence_summary={"captured": 2, "failed": 1},
    )

    assert "age_gate" not in snapshot.required_page_types
    assert "unknown" not in snapshot.not_applicable_page_types
    assert snapshot.observed_lifecycle_page_types == ("age_gate",)
    assert snapshot.unknown_page_capture_ids == ("capture-unknown",)
    assert dict(snapshot.supplemental_evidence_summary) == {"captured": 2, "failed": 1}
    assert snapshot.snapshot_protocol_version == "web-coverage-snapshot.v4"


@pytest.mark.parametrize(
    ("normalization_version", "snapshot_protocol_version"),
    [
        ("web-page-type-assessment.v4", "web-coverage-snapshot.v3"),
        ("web-page-type-assessment.v3", "web-coverage-snapshot.v2"),
        ("web-page-type-assessment.v2", "web-coverage-snapshot.v3"),
    ],
)
def test_coverage_rejects_protocol_from_another_assessment_generation(
    normalization_version: str,
    snapshot_protocol_version: str,
) -> None:
    with pytest.raises(ValueError, match="不一致"):
        _build_snapshot(
            normalization_version=normalization_version,
            snapshot_protocol_version=snapshot_protocol_version,
        )


def test_v4_unknown_capture_ids_preserve_identifier_case() -> None:
    snapshot = _build_snapshot(unknown_page_capture_ids=("Capture-AbC",))

    assert snapshot.unknown_page_capture_ids == ("Capture-AbC",)


def test_legacy_coverage_builder_filters_to_seven_web_business_types() -> None:
    module = import_module("services.api.app.infrastructure.db.web_coverage_repository")
    inspections = tuple(
        SimpleNamespace(
            page_capture_id=f"capture-{page_type}",
            page_type=page_type,
            page_state="stable",
            assessment_status="covered",
        )
        for page_type in ("activity", "search_page", "age_gate", "unknown")
    )

    snapshot = module.build_web_coverage_snapshot(
        inspections=inspections,
        required_page_types=("activity", "search_page", "age_gate", "unknown"),
    )

    assert snapshot.covered_page_types == ("activity", "search_page")
    assert snapshot.required_page_types == ("activity", "search_page")


@pytest.mark.parametrize("field_name", ["required_page_types", "observed_page_types"])
@pytest.mark.parametrize("invalid_value", [None, 123, object()])
def test_web_coverage_snapshot_rejects_non_string_canonical_values(
    field_name: str,
    invalid_value: object,
) -> None:
    with pytest.raises(ValueError, match="字符串"):
        _build_snapshot(**{field_name: ["content_list", invalid_value]})


@pytest.mark.parametrize("field_name", ["required_page_types", "observed_page_types"])
@pytest.mark.parametrize("invalid_page_type", ["profile", "web_content_list"])
def test_web_coverage_snapshot_rejects_page_types_outside_web_canonical_set(
    field_name: str,
    invalid_page_type: str,
) -> None:
    with pytest.raises(ValueError, match="Web 标准页型"):
        _build_snapshot(**{field_name: ["content_list", invalid_page_type]})


@pytest.mark.parametrize("invalid_value", [None, 123, object()])
def test_web_coverage_snapshot_rejects_non_string_unrecognized_values(
    invalid_value: object,
) -> None:
    with pytest.raises(ValueError, match="字符串"):
        _build_snapshot(unrecognized_page_types=["raw_unknown", invalid_value])


def test_web_coverage_snapshot_normalizes_open_unrecognized_vocabulary() -> None:
    snapshot = _build_snapshot(
        unrecognized_page_types=[" Profile ", "PROFILE", "custom_page", ""],
    )

    assert snapshot.unrecognized_page_types == ("custom_page", "profile")


def test_web_coverage_snapshot_preserves_canonical_like_raw_unrecognized_values() -> None:
    snapshot = _build_snapshot(
        unrecognized_page_types=[
            " content_list ",
            "VIDEO_DETAIL",
            "content_list",
        ]
    )

    assert snapshot.unrecognized_page_types == ("content_list", "video_detail")


def test_web_coverage_snapshot_hash_is_independent_of_input_order() -> None:
    left = _build_snapshot()
    right = _build_snapshot(
        required_page_types=["content_list", "video_detail"],
        observed_page_types=["content_list", "CONTENT_LIST"],
        unrecognized_page_types=["web_unknown"],
    )

    assert right.required_page_types == left.required_page_types
    assert right.observed_page_types == left.observed_page_types
    assert right.unrecognized_page_types == left.unrecognized_page_types
    assert right.snapshot_hash == left.snapshot_hash


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("task_id", "task-2"),
        ("agent_run_id", "run-2"),
        ("finalization_epoch", 4),
        ("required_page_types", ["content_detail", "content_list", "video_detail"]),
        ("observed_page_types", ["content_list", "video_detail"]),
        ("unrecognized_page_types", ["another_unknown"]),
        ("coverage_status", "failed"),
    ],
)
def test_web_coverage_snapshot_hash_covers_authoritative_material(
    changed: str,
    value: object,
) -> None:
    baseline = _build_snapshot()
    changed_snapshot = _build_snapshot(**{changed: value})

    assert changed_snapshot.snapshot_hash != baseline.snapshot_hash


def test_web_coverage_snapshot_hash_includes_fixed_normalization_version() -> None:
    snapshot = _build_snapshot(normalization_version="web-page-type-assessment.v2", snapshot_protocol_version="web-coverage-snapshot.v1")
    expected_material = {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "finalization_epoch": 3,
        "required_page_types": ["content_list", "video_detail"],
        "observed_page_types": ["content_list"],
        "missing_page_types": ["video_detail"],
        "unrecognized_page_types": ["web_unknown"],
        "normalization_version": "web-page-type-assessment.v2",
        "coverage_status": "completed_with_gaps",
    }
    expected = hashlib.sha256(
        json.dumps(
            expected_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert snapshot.snapshot_hash == expected


def test_v3_coverage_snapshot_hash_remains_frozen_without_v4_material() -> None:
    snapshot = _snapshot_module().WebCoverageSnapshot.create(
        task_id="task-history",
        agent_run_id="run-history",
        finalization_epoch=7,
        required_page_types=("home", "content_list"),
        optional_page_types=("content_detail",),
        not_applicable_page_types=("video_list", "video_detail"),
        observed_page_types=("home",),
        unrecognized_page_types=("legacy_raw",),
        normalization_version="web-page-type-assessment.v3",
        snapshot_protocol_version="web-coverage-snapshot.v3",
        applicability_mode="catalog_declared",
        catalog_version="2026-09-04.web.v3",
        catalog_hash="a" * 64,
    )

    assert snapshot.snapshot_hash == (
        "03761e7abdfd6b79a47300497bb8422c87834f3f1db5a95fa73cd0551a5fb8fe"
    )
    assert "observed_lifecycle_page_types" not in snapshot.hash_material()
    assert "unknown_page_capture_ids" not in snapshot.hash_material()
    assert "supplemental_evidence_summary" not in snapshot.hash_material()


def test_web_coverage_snapshot_rejects_unknown_normalization_version() -> None:
    with pytest.raises(ValueError, match="normalization_version"):
        _build_snapshot(normalization_version="web-page-type-assessment.v999")


def test_web_coverage_snapshot_derives_complete_status_without_missing_types() -> None:
    snapshot = _build_snapshot(
        required_page_types=["content_list"],
        observed_page_types=["content_list"],
    )

    assert snapshot.missing_page_types == ()
    assert snapshot.coverage_status == "complete"


def test_web_coverage_snapshot_allows_explicit_terminal_foundation_failure() -> None:
    snapshot = _build_snapshot(coverage_status="failed")

    assert snapshot.coverage_status == "failed"


@pytest.mark.parametrize("coverage_status", ["", "pending", "running", "unknown"])
def test_web_coverage_snapshot_rejects_invalid_status(coverage_status: str) -> None:
    with pytest.raises(ValueError, match="coverage_status"):
        _build_snapshot(coverage_status=coverage_status)


def test_web_coverage_snapshot_rejects_normal_status_inconsistent_with_facts() -> None:
    with pytest.raises(ValueError, match="覆盖事实"):
        _build_snapshot(coverage_status="complete")


def test_web_coverage_snapshot_is_immutable() -> None:
    snapshot = _build_snapshot()

    with pytest.raises(FrozenInstanceError):
        snapshot.coverage_status = "failed"
