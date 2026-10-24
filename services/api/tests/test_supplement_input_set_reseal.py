"""P1-10：补采接纳后重封 EvidenceInputSet。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.supplement_input_set_reseal import (
    SUPPLEMENT_RESEAL_SELECTION_POLICY,
    SealedSupplementInputSet,
    SupplementInputSetResealResult,
    SupplementInputSetResealService,
    WorkItemInputSetContext,
    _manifest_capture_role,
    format_resolution_reason_with_reseal,
    parse_resealed_input_set_id,
    reseal_evidence_input_set_with_supplement,
)
from services.api.app.domain.evidence.contracts import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.evidence.input_set import (
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceInputSetValidationError,
    seal_evidence_input_set,
)

NOW = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)


def _manifest(
    capture_id: str,
    *,
    task_id: str = "task-1",
    role: str = "primary",
):
    return build_evidence_manifest_reference(
        task_id=task_id,
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=2,
        artifacts=(
            ArtifactReference(
                artifact_id=f"artifact-{capture_id}",
                artifact_type="ui_snapshot",
                role=role,
                storage_path=f"evidence/{task_id}/{capture_id}/ui.json",
                checksum="sha256:" + "a" * 64,
                size_bytes=128,
                content_type="application/json",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified",
        observation_packet_ids=(f"packet-{capture_id}",),
        created_at=NOW,
    )


def _candidate(
    capture_id: str,
    *,
    page_sequence: int,
    role: str = "primary",
) -> EvidenceInputCandidate:
    return EvidenceInputCandidate(
        manifest=_manifest(capture_id, role=role),
        capture_role=role,
        page_sequence=page_sequence,
    )


def _original():
    c1 = _candidate("capture-1", page_sequence=1)
    c2 = _candidate("capture-2", page_sequence=2)
    sealed = seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-1",
        rule_code="alioth_spec_1",
        rule_contract_hash="rule-contract",
        ruleset_snapshot_hash="ruleset",
        scope=EvidenceInputScope.TASK,
        scope_key="task-scope",
        finalization_epoch=2,
        eligibility_snapshot_id="elig-1",
        eligibility_snapshot_hash="elig-hash",
        selection_policy_hash="selection-v0",
        eligible_capture_ids=("capture-1", "capture-2"),
        candidates=(c1, c2),
        sealed_at=NOW,
    )
    return sealed, (c1, c2)


def test_reseal_appends_supplement_and_changes_identity() -> None:
    original, candidates = _original()
    supplement = _candidate("capture-supplement", page_sequence=3)

    resealed = reseal_evidence_input_set_with_supplement(
        original=original,
        original_candidates=candidates,
        supplement_candidate=supplement,
        request_id="req-1",
        restoration_attempt_id="rest-1",
        equivalence_assessment_id="eq-1",
        sealed_at=NOW + timedelta(minutes=1),
        supplement_manifest_id=supplement.manifest.manifest_id,
    )

    assert resealed.input_set_id != original.input_set_id
    assert resealed.input_set_hash != original.input_set_hash
    assert resealed.eligible_capture_ids == (
        "capture-1",
        "capture-2",
        "capture-supplement",
    )
    assert resealed.selection_policy_hash != original.selection_policy_hash
    assert SUPPLEMENT_RESEAL_SELECTION_POLICY.encode()  # policy constant exists
    assert "capture-supplement" in {m.capture_id for m in resealed.members}


def test_reseal_rejects_incomplete_original_candidates() -> None:
    original, candidates = _original()
    with pytest.raises(EvidenceInputSetValidationError, match="完整覆盖"):
        reseal_evidence_input_set_with_supplement(
            original=original,
            original_candidates=candidates[:1],
            supplement_candidate=_candidate("capture-x", page_sequence=9),
            request_id="req-1",
            restoration_attempt_id="rest-1",
            equivalence_assessment_id="eq-1",
            sealed_at=NOW,
        )


def test_reseal_page_scope_replaces_original_capture_and_preserves_lineage() -> None:
    original_candidate = _candidate(
        "capture-original",
        page_sequence=1,
        role="observation",
    )
    original = seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-1",
        rule_code="alioth_spec_1",
        rule_contract_hash="rule-contract",
        ruleset_snapshot_hash="ruleset",
        scope=EvidenceInputScope.PAGE,
        scope_key="page:capture-original",
        finalization_epoch=0,
        eligibility_snapshot_id="elig-page-1",
        eligibility_snapshot_hash="elig-page-hash",
        selection_policy_hash="selection-page-v0",
        eligible_capture_ids=("capture-original",),
        candidates=(original_candidate,),
        sealed_at=NOW,
    )
    supplement = _candidate(
        "capture-supplement",
        page_sequence=2,
        role="probe_result_replay",
    )

    resealed = reseal_evidence_input_set_with_supplement(
        original=original,
        original_candidates=(original_candidate,),
        supplement_candidate=supplement,
        request_id="req-page-1",
        restoration_attempt_id="rest-page-1",
        equivalence_assessment_id="eq-page-1",
        sealed_at=NOW + timedelta(minutes=1),
    )

    assert resealed.input_set_id != original.input_set_id
    assert resealed.eligible_capture_ids == ("capture-supplement",)
    assert resealed.members[0].capture_role == "probe_result_replay"


def test_reseal_is_idempotent_for_same_lineage() -> None:
    original, candidates = _original()
    supplement = _candidate("capture-supplement", page_sequence=3)
    first = reseal_evidence_input_set_with_supplement(
        original=original,
        original_candidates=candidates,
        supplement_candidate=supplement,
        request_id="req-1",
        restoration_attempt_id="rest-1",
        equivalence_assessment_id="eq-1",
        sealed_at=NOW,
    )
    second = reseal_evidence_input_set_with_supplement(
        original=original,
        original_candidates=candidates,
        supplement_candidate=supplement,
        request_id="req-1",
        restoration_attempt_id="rest-1",
        equivalence_assessment_id="eq-1",
        sealed_at=NOW + timedelta(hours=1),
    )
    assert first.input_set_id == second.input_set_id
    assert first.input_set_hash == second.input_set_hash


def test_resolution_reason_roundtrip_for_resealed_input_set() -> None:
    reason = format_resolution_reason_with_reseal(
        base_reason="replay_probe:supp-1",
        input_set_id="evidence-input-set:abc",
    )
    assert reason.startswith("replay_probe:supp-1|")
    assert parse_resealed_input_set_id(reason) == "evidence-input-set:abc"
    again = format_resolution_reason_with_reseal(
        base_reason=reason,
        input_set_id="evidence-input-set:def",
    )
    assert parse_resealed_input_set_id(again) == "evidence-input-set:def"
    assert again.count("resealed_input_set:") == 1


def test_parse_resealed_input_set_id_absent() -> None:
    assert parse_resealed_input_set_id(None) is None
    assert parse_resealed_input_set_id("replay_probe:x") is None


@pytest.mark.parametrize("role", ["observation", "probe_result_replay"])
def test_manifest_capture_role_preserves_signed_artifact_role(role: str) -> None:
    manifest = build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id=f"capture-{role}",
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id=f"artifact-{role}",
                artifact_type="screenshot",
                role=role,
                storage_path=f"evidence/{role}.png",
                checksum="sha256:" + "b" * 64,
                size_bytes=128,
                content_type="image/png",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified",
        created_at=NOW,
    )

    assert _manifest_capture_role(manifest) == role


async def test_page_scope_reseal_reuses_supplement_seal_and_marks_same_id() -> None:
    """页面级规则不再自己另封一份：沿用补采记录上的集合，请求上的标记写同一个编号。"""

    class _MustNotSealHere:
        def __getattr__(self, name: str):
            raise AssertionError(f"页面级规则不应再自己封存（调用了 {name}）")

    sealed_requests: list[str] = []
    annotations: list[tuple[str, str]] = []

    class _WorkItems:
        async def get_rule_input_set_context(self, *, work_item_id: str, rule_id: str):
            return WorkItemInputSetContext(
                evidence_input_set_id=None,
                contract_hash="contract-1",
                producer_agent_run_id="run-1",
                finalization_epoch=0,
                ruleset_snapshot_hash="snapshot-1",
            )

        async def seal_supplement_input_set(self, *, request_id: str):
            sealed_requests.append(request_id)
            return SealedSupplementInputSet(
                input_set_id="evidence-input-set:shared",
                input_set_hash="shared",
                created=False,
            )

    class _Requests:
        async def annotate_resealed_input_set(self, *, request_id: str, input_set_id: str):
            annotations.append((request_id, input_set_id))
            return input_set_id

    service = SupplementInputSetResealService(
        input_sets=_MustNotSealHere(),
        manifests=_MustNotSealHere(),
        work_items=_WorkItems(),
        requests=_Requests(),
    )

    result = await service.reseal_after_attach(
        stored=SimpleNamespace(
            request=SimpleNamespace(
                request_id="request-1",
                work_item_id="work-1",
                rule_id="rule-1",
            )
        ),
        supplement_manifest_id="manifest-supplement",
        restoration_attempt_id="restoration-1",
        equivalence_assessment_id="equivalence-1",
    )

    assert sealed_requests == ["request-1"]
    assert annotations == [("request-1", "evidence-input-set:shared")]
    assert result == SupplementInputSetResealResult(
        input_set_id="evidence-input-set:shared",
        input_set_hash="shared",
        created=False,
        parent_input_set_id=None,
    )
