"""Explorer/Evidence 到 Rule Judge 中转协议测试。"""

from dataclasses import replace

import pytest

from services.api.app.application.inspection_handoff import build_inspection_handoff
from services.api.app.application.inspection_work_items import (
    InspectionRuleEvidenceBinding,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    build_inspection_attempt_input_hash,
)
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    InspectionHandoff,
    InspectionHandoffValidationError,
    InspectionRuleTarget,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    _parse_rule_evidence_binding,
)


def _lease() -> InspectionWorkItemLease:
    return InspectionWorkItemLease(
        work_item_id="work-1",
        task_id="task-1",
        page_capture_id="capture-1",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        input_hash="input-hash-1",
        producer_agent_run_id="producer-run-1",
        consumer_agent_run_id="consumer-run-1",
        lease_token="secret-lease-token",
        attempt_no=2,
        row_version=7,
        rules=(
            InspectionWorkItemRule(
                rule_id="rule-2",
                rule_code="alioth_2",
                ordinal=1,
                contract_hash="contract-2",
            ),
            InspectionWorkItemRule(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )


def test_handoff_exposes_only_judge_read_contract() -> None:
    handoff = build_inspection_handoff(_lease(), attempt_id="attempt-2")

    assert handoff.idempotency_key == "input-hash-1"
    assert handoff.attempt_id == "attempt-2"
    assert handoff.agent_run_id == "consumer-run-1"
    assert handoff.evidence.page_capture_id == "capture-1"
    assert handoff.evidence.producer_agent_run_id == "producer-run-1"
    assert [rule.rule_code for rule in handoff.rules] == ["alioth_1", "alioth_2"]
    payload = handoff.to_dict()
    serialized = repr(payload)
    assert "secret-lease-token" not in serialized
    assert "lease_token" not in payload
    assert "row_version" not in payload
    assert "attempt_no" not in payload
    assert payload["idempotency_key"] == "input-hash-1"


def test_legacy_handoff_serialization_omits_optional_manifest_role_map() -> None:
    evidence = EvidenceBundleReference(
        page_capture_id="capture-1",
        producer_agent_run_id="producer-run-1",
        artifact_ids=("shot-1",),
    )

    payload = evidence.to_dict()

    assert payload["page_capture_id"] == "capture-1"
    assert "evidence_manifest_refs" not in payload


def test_handoff_binds_rule_to_exact_supplemental_evidence() -> None:
    supplemental = EvidenceBundleReference(
        page_capture_id="capture-2",
        producer_agent_run_id="producer-run-2",
        manifest_id="manifest-2",
        manifest_version="2",
        artifact_ids=("shot-2", "trace-2"),
        evidence_capture_roles={"primary": "capture-2"},
    )

    handoff = build_inspection_handoff(
        _lease(),
        attempt_id="attempt-2",
        rule_evidence={"alioth_1": supplemental},
    )

    assert handoff.evidence.page_capture_id == "capture-1"
    assert handoff.evidence_for_rule("alioth_1") == supplemental
    assert handoff.evidence_for_rule("alioth_2") == handoff.evidence
    assert handoff.to_dict()["rule_evidence"]["alioth_1"]["manifest_id"] == "manifest-2"


def test_handoff_keeps_baseline_before_role_when_supplement_replaces_primary() -> None:
    supplemental = EvidenceBundleReference(
        page_capture_id="capture-2",
        producer_agent_run_id="producer-run-1",
        manifest_id="manifest-2",
        manifest_version="2",
        observation_packet_ids=("packet-after",),
        artifact_ids=("shot-after",),
        evidence_capture_roles={"primary": "capture-2"},
    )
    handoff = build_inspection_handoff(
        _lease(),
        attempt_id="attempt-2",
        evidence_capture_roles={
            "before": "capture-before",
            "primary": "capture-1",
        },
        rule_evidence={"alioth_1": supplemental},
    )
    handoff = replace(
        handoff,
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
            observation_packet_ids=("packet-before", "packet-old-primary"),
            artifact_ids=("shot-before", "shot-old-primary"),
            evidence_capture_roles={
                "before": "capture-before",
                "primary": "capture-1",
            },
            evidence_manifest_refs={
                "before": {
                    "manifest_id": "manifest-before",
                    "manifest_version": "1",
                },
                "primary": {
                    "manifest_id": "manifest-1",
                    "manifest_version": "1",
                },
            },
        ),
    )

    evidence = handoff.evidence_for_rule("alioth_1")

    assert evidence.evidence_capture_roles == {
        "before": "capture-before",
        "primary": "capture-2",
    }
    assert evidence.page_capture_id == "capture-2"
    assert evidence.manifest_id == "manifest-2"
    assert evidence.observation_packet_ids == ("packet-after",)
    assert evidence.artifact_ids == ("shot-after",)
    assert evidence.retained_baseline_artifact_ids == {
        "before": ("shot-before", "shot-old-primary")
    }
    assert evidence.evidence_manifest_refs == {
        "before": {"manifest_id": "manifest-before", "manifest_version": "1"},
        "primary": {"manifest_id": "manifest-2", "manifest_version": "2"},
    }


def test_handoff_projects_supplemental_evidence_from_lease() -> None:
    lease = replace(
        _lease(),
        rule_evidence=(
            InspectionRuleEvidenceBinding(
                rule_id="rule-1",
                rule_code="alioth_1",
                page_capture_id="capture-2",
                producer_agent_run_id="producer-run-2",
                manifest_id="manifest-2",
                manifest_version=2,
                artifact_ids=("shot-2",),
            ),
        ),
    )

    handoff = build_inspection_handoff(lease, attempt_id="attempt-2")

    assert handoff.evidence.page_capture_id == "capture-1"
    assert handoff.evidence_for_rule("alioth_1").page_capture_id == "capture-2"
    assert handoff.evidence_for_rule("alioth_1").manifest_id == "manifest-2"
    assert handoff.evidence_for_rule("alioth_1").evidence_capture_roles == {"primary": "capture-2"}


def test_handoff_projects_h5_probe_before_and_primary_roles_from_lease() -> None:
    lease = replace(
        _lease(),
        rule_evidence=(
            InspectionRuleEvidenceBinding(
                rule_id="rule-1",
                rule_code="alioth_1",
                page_capture_id="capture-after",
                producer_agent_run_id="producer-run-2",
                manifest_id="manifest-2",
                manifest_version=2,
                artifact_ids=("shot-before", "shot-after"),
                evidence_capture_roles=(
                    ("before", "capture-before"),
                    ("primary", "capture-after"),
                ),
            ),
        ),
    )

    handoff = build_inspection_handoff(lease, attempt_id="attempt-2")

    assert handoff.evidence_for_rule("alioth_1").evidence_capture_roles == {
        "before": "capture-before",
        "primary": "capture-after",
    }


@pytest.mark.parametrize(
    "requested_roles, expected_roles, primary_capture, primary_manifest",
    (
        (
            [],
            {"primary": "capture-after"},
            "capture-after",
            "manifest-after",
        ),
        (
            ["primary"],
            {"primary": "capture-after"},
            "capture-after",
            "manifest-after",
        ),
        (
            ["before", "primary"],
            {"before": "capture-before", "primary": "capture-after"},
            "capture-after",
            "manifest-after",
        ),
        (
            ["source"],
            {"source": "capture-before", "primary": "capture-after"},
            "capture-after",
            "manifest-after",
        ),
        (
            ["primary", "refreshed"],
            {"primary": "capture-before", "refreshed": "capture-after"},
            "capture-before",
            "manifest-before",
        ),
        (
            ["primary", "landing"],
            {"primary": "capture-before", "landing": "capture-after"},
            "capture-before",
            "manifest-before",
        ),
    ),
)
def test_h5_probe_roles_reach_handoff_with_exact_capture_and_manifest(
    requested_roles,
    expected_roles,
    primary_capture,
    primary_manifest,
) -> None:
    binding = _parse_rule_evidence_binding(
        {
            "target_platform": "h5",
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "page_capture_id": "capture-after",
            "producer_agent_run_id": "producer-run-1",
            "capture_role": "probe_result_replay",
            "parent_capture_role": "probe_before",
            "parent_capture_id": "capture-before",
            "manifest_id": "manifest-after",
            "manifest_version": 2,
            "manifest_payload": {"artifacts": [{"artifact_id": "shot-after"}]},
            "parent_manifest_id": "manifest-before",
            "parent_manifest_version": 1,
            "parent_manifest_payload": {"artifacts": [{"artifact_id": "shot-before"}]},
            "request_payload": {
                "target_invariants": {
                    "semantic": {
                        "capture_roles": requested_roles,
                    }
                }
            },
        }
    )
    handoff = build_inspection_handoff(
        replace(_lease(), target_platform="h5", rule_evidence=(binding,)),
        attempt_id="attempt-2",
    )
    evidence = handoff.evidence_for_rule("alioth_1")
    assert evidence.page_capture_id == primary_capture
    assert evidence.manifest_id == primary_manifest
    assert evidence.evidence_capture_roles == expected_roles
    assert set(evidence.artifact_ids) == (
        {"shot-before", "shot-after"} if "capture-before" in expected_roles.values()
        else {"shot-after"}
    )
    assert evidence.evidence_manifest_refs == {
        role: {
            "manifest_id": "manifest-before" if capture == "capture-before" else "manifest-after",
            "manifest_version": "1" if capture == "capture-before" else "2",
        }
        for role, capture in expected_roles.items()
    }
    changed = replace(
        binding,
        evidence_manifest_refs=tuple(
            (role, manifest_id, "3") for role, manifest_id, _ in binding.evidence_manifest_refs
        ),
    )
    assert build_inspection_attempt_input_hash(
        work_item_input_hash="baseline",
        rule_evidence=(binding,),
    ) != build_inspection_attempt_input_hash(
        work_item_input_hash="baseline",
        rule_evidence=(changed,),
    )


def test_handoff_keeps_before_primary_roles_for_matching_supplemental_primary() -> None:
    lease = replace(
        _lease(),
        page_capture_id="capture-2",
        rule_evidence=(
            InspectionRuleEvidenceBinding(
                rule_id="rule-1",
                rule_code="alioth_1",
                page_capture_id="capture-2",
                producer_agent_run_id="producer-run-2",
                manifest_id="manifest-2",
                manifest_version=2,
                artifact_ids=("shot-2",),
            ),
        ),
    )

    handoff = build_inspection_handoff(
        lease,
        attempt_id="attempt-2",
        evidence_capture_roles={"before": "capture-1", "primary": "capture-2"},
    )

    assert handoff.evidence_for_rule("alioth_1").evidence_capture_roles == {
        "before": "capture-1",
        "primary": "capture-2",
    }


def test_evidence_manifest_identity_must_be_complete() -> None:
    with pytest.raises(InspectionHandoffValidationError, match="必须同时提供"):
        EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
            manifest_id="manifest-1",
        )


def test_handoff_rejects_duplicate_or_unordered_rules() -> None:
    handoff = build_inspection_handoff(_lease(), attempt_id="attempt-2")
    duplicate = replace(handoff.rules[0], ordinal=1)

    with pytest.raises(InspectionHandoffValidationError, match="ordinal"):
        replace(handoff, rules=(duplicate, handoff.rules[1]))


def test_handoff_rejects_unknown_strategy() -> None:
    with pytest.raises(InspectionHandoffValidationError, match="strategy"):
        InspectionHandoff(
            work_item_id="work-1",
            attempt_id="attempt-1",
            task_id="task-1",
            agent_run_id="consumer-run-1",
            input_hash="input-1",
            batch_key="unknown:all:snapshot",
            strategy="unknown",
            session_dependency="snapshot",
            evidence=EvidenceBundleReference(
                page_capture_id="capture-1",
                producer_agent_run_id="producer-run-1",
            ),
            rules=(
                InspectionRuleTarget(
                    rule_id="rule-1",
                    rule_code="alioth_1",
                    ordinal=0,
                    contract_hash="contract-1",
                ),
            ),
        )
