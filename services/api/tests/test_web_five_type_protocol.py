"""新五类协议与冻结四类历史合同。"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.application.web_page_applicability import resolve_web_page_applicability
from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot


@pytest.mark.parametrize("mode", ["strict_catalog", "strict_four", "catalog_declared", None])
def test_new_run_uses_dynamic_home_denominator(mode):
    snapshot = resolve_web_page_applicability(
        target_url="https://unknown.example/",
        requested_mode=mode,
        requested_required_page_types=(),
    )
    assert snapshot.required_page_types == ("home",)
    assert snapshot.applicability_mode == "runtime_dynamic"
    if mode is not None:
        assert snapshot.resolution_reason == "legacy_request_mode_dynamic_alias"


def test_real_published_v3_catalog_restores_without_injecting_root_knowledge():
    from services.api.app.domain.page.web_page_catalog import parse_web_page_type_catalog

    fixture = Path(__file__).with_name("fixtures") / "web_page_catalog_20260904_v3.json"
    raw = fixture.read_bytes().replace(b"\r\n", b"\n")
    blob = b"blob " + str(len(raw)).encode() + b"\0" + raw
    assert hashlib.sha1(blob).hexdigest() == "0ecce9f88f42ba8adfb43d711d6301c894dea086"
    payload = json.loads(raw)
    original = json.dumps(payload, sort_keys=True)
    restored = parse_web_page_type_catalog(payload, legacy_frozen=True)
    assert restored.codes == ("video_list", "video_detail", "content_list", "content_detail")
    assert restored.navigation_signals is None
    assert restored.common_site_observations is None
    assert json.dumps(payload, sort_keys=True) == original
    with pytest.raises(ValueError):
        parse_web_page_type_catalog(payload)


def test_completion_gate_accepts_real_v1_coverage_normalization_name():
    from services.api.app.infrastructure.db.task_finalization_repository import _GATE_FACTS_SQL

    normalization_predicate = _GATE_FACTS_SQL.split("AND snapshot.normalization_version", 1)[
        1
    ].split("AND jsonb_array_length", 1)[0]
    assert "'web-page-type-normalization.v1'" in normalization_predicate
    assert "'web-page-type-assessment.v2'" in normalization_predicate
    assert "'web-page-type-assessment.v3'" in normalization_predicate
    assert "'web-page-type-assessment.v1'" not in normalization_predicate


def test_published_snapshot_matches_frozen_run_assessment_v4():
    from services.api.app.application.web_run_policy import (
        load_published_web_policy,
        materialize_web_policy,
    )
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot

    policy = materialize_web_policy(
        WebRunPolicySnapshot.create(
            task_id="task",
            agent_run_id="run",
            payload=load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=120),
        )
    )
    snapshot = WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=1,
        required_page_types=("home",),
        observed_page_types=("home",),
        unrecognized_page_types=(),
        normalization_version=policy.assessment_schema_version,
        snapshot_protocol_version="web-coverage-snapshot.v4",
    )
    assert (
        snapshot.normalization_version
        == policy.assessment_schema_version
        == "web-page-type-assessment.v4"
    )
    assert snapshot.snapshot_protocol_version == "web-coverage-snapshot.v4"
    assert snapshot.coverage_status == "complete"


@pytest.mark.parametrize(
    "normalization,protocol",
    [
        ("web-page-type-assessment.v2", "web-coverage-snapshot.v2"),
        ("web-page-type-assessment.v3", "web-coverage-snapshot.v3"),
        ("web-page-type-assessment.v4", "web-coverage-snapshot.v4"),
    ],
)
def test_final_report_row_reader_preserves_version_and_rejects_tampering(normalization, protocol):
    """正式 Gate 的独立读取器不能拒绝新协议或把旧协议重签为新版。"""
    from services.api.app.infrastructure.db.web_coverage_snapshot_row import (
        InvalidWebCoverageSnapshotRowError,
        parse_web_coverage_snapshot_row,
    )

    original = WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=1,
        required_page_types=("content_list",),
        observed_page_types=("content_list",),
        unrecognized_page_types=(),
        normalization_version=normalization,
        snapshot_protocol_version=protocol,
    )
    applicability = original.hash_material()
    if protocol == "web-coverage-snapshot.v4":
        applicability = {
            **applicability,
            "lifecycle_observations": applicability.pop("observed_lifecycle_page_types"),
        }
    row = {
        **original.hash_material(),
        "id": original.snapshot_id,
        "snapshot_hash": original.snapshot_hash,
        "created_at": original.created_at,
        "applicability_snapshot": applicability,
    }
    assert parse_web_coverage_snapshot_row(row) == original
    for changed in (
        dict(row, snapshot_hash="0" * 64),
        dict(
            row,
            applicability_snapshot={
                **original.hash_material(),
                "snapshot_protocol_version": "web-coverage-snapshot.v99",
            },
        ),
    ):
        with pytest.raises(InvalidWebCoverageSnapshotRowError):
            parse_web_coverage_snapshot_row(changed)


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("required_page_types", ["content_detail"]),
        ("observed_page_types", []),
        ("missing_page_types", ["home"]),
        ("unrecognized_page_types", ["unknown"]),
    ],
)
def test_v4_independent_row_reader_rejects_applicability_fact_mismatch(
    field_name: str,
    tampered_value: list[str],
) -> None:
    from services.api.app.infrastructure.db.web_coverage_snapshot_row import (
        InvalidWebCoverageSnapshotRowError,
        parse_web_coverage_snapshot_row,
    )

    original = WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=1,
        required_page_types=("home",),
        observed_page_types=("home",),
        unrecognized_page_types=(),
    )
    applicability = {
        **original.hash_material(),
        "lifecycle_observations": original.observed_lifecycle_page_types,
    }
    applicability.pop("observed_lifecycle_page_types")
    applicability[field_name] = tampered_value
    row = {
        **original.hash_material(),
        "id": original.snapshot_id,
        "snapshot_hash": original.snapshot_hash,
        "created_at": original.created_at,
        "applicability_snapshot": applicability,
    }

    with pytest.raises(InvalidWebCoverageSnapshotRowError):
        parse_web_coverage_snapshot_row(row)


@pytest.mark.parametrize(
    "normalization,protocol",
    [
        ("web-page-type-assessment.v2", "web-coverage-snapshot.v1"),
        ("web-page-type-assessment.v2", "web-coverage-snapshot.v2"),
        ("web-page-type-assessment.v3", "web-coverage-snapshot.v3"),
        ("web-page-type-assessment.v4", "web-coverage-snapshot.v4"),
    ],
)
def test_repository_restores_frozen_snapshot_hash_and_denominator(normalization, protocol):
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        parse_web_coverage_snapshot_row,
    )

    original = WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=1,
        required_page_types=("content_list",),
        observed_page_types=(),
        unrecognized_page_types=(),
        normalization_version=normalization,
        snapshot_protocol_version=protocol,
    )
    row = {
        **original.hash_material(),
        "id": original.snapshot_id,
        "snapshot_hash": original.snapshot_hash,
        "created_at": original.created_at,
    }
    if protocol != "web-coverage-snapshot.v1":
        applicability = original.hash_material()
        if protocol == "web-coverage-snapshot.v4":
            applicability = {
                **applicability,
                "lifecycle_observations": applicability.pop("observed_lifecycle_page_types"),
            }
        row["applicability_snapshot"] = applicability
    restored = parse_web_coverage_snapshot_row(row)
    assert restored == original
    assert ("home" in restored.not_applicable_page_types) == normalization.endswith(("v3", "v4"))


@pytest.mark.parametrize("protocol", ["web-coverage-snapshot.v1", "web-coverage-snapshot.v2"])
def test_legacy_snapshot_restores_exact_four_type_denominator(protocol):
    snapshot = WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=1,
        required_page_types=("content_detail", "content_list", "video_detail", "video_list"),
        observed_page_types=("content_list",),
        unrecognized_page_types=(),
        normalization_version="web-page-type-assessment.v2",
        snapshot_protocol_version=protocol,
    )
    assert snapshot.not_applicable_page_types == ()
    assert replace(snapshot).snapshot_hash == snapshot.snapshot_hash
    with pytest.raises(ValueError):
        replace(snapshot, required_page_types=("home",))


def test_frozen_legacy_policy_restores_four_types_without_modifying_published_contract():
    from services.api.app.application.web_run_policy import (
        load_published_web_policy,
    )
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot

    body = load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=120)
    fixture = Path(__file__).with_name("fixtures") / "web_page_catalog_20260904_v3.json"
    catalog = json.loads(fixture.read_text(encoding="utf-8"))
    body["page_catalog"] = catalog
    body["guide_version"] = catalog["version"]
    from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry

    body["menu_priors"] = WebSiteMenuRegistry().to_payload()
    for site in body["menu_priors"]["sites"]:
        site["applicability"]["required_page_types"].remove("home")
        site["applicability"]["optional_page_types"] = [
            page_type
            for page_type in site["applicability"]["optional_page_types"]
            if page_type not in {"activity", "search_page"}
        ]
        for entry in site["entries"]:
            if entry["decision"] == "home_baseline":
                entry["classification_hint"]["expected_page_type"] = "video_list"
    snapshot = WebRunPolicySnapshot.create(task_id="task", agent_run_id="run", payload=body)
    frozen_hash = snapshot.payload_sha256
    frozen_payload = snapshot.to_payload()
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    restored = LegacyWebRunAuditAdapter.load(snapshot)
    assert snapshot.payload_sha256 == frozen_hash
    assert snapshot.to_payload() == frozen_payload
    assert restored.assessment_schema_version == "web-page-type-assessment.v2"
    assert "home" not in restored.catalog.codes
    assert all(
        "home" not in site["applicability"]["required_page_types"]
        for site in restored.frozen_fields["menu_priors"]["sites"]
    )
