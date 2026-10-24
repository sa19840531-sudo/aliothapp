"""任务级、旅程级与环境矩阵证据输入集合合同测试。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.evidence.contracts import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.evidence.input_set import (
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceInputSet,
    EvidenceInputSetValidationError,
    seal_evidence_input_set,
)

NOW = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)


def _manifest(
    capture_id: str,
    *,
    task_id: str = "task-1",
    quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS,
    artifact_role: str = "primary",
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
                role=artifact_role,
                storage_path=f"evidence/{task_id}/{capture_id}/ui.json",
                checksum="sha256:" + "a" * 64,
                size_bytes=128,
                content_type="application/json",
            ),
        ),
        quality_status=quality_status,
        quality_reason=(
            "artifact_integrity_verified"
            if quality_status is EvidenceQualityStatus.PASS
            else "artifact_incomplete"
        ),
        observation_packet_ids=(f"packet-{capture_id}",),
        created_at=NOW,
    )


def _candidate(
    capture_id: str,
    *,
    page_sequence: int,
    journey_execution_id: str | None = None,
    environment_profile_id: str | None = None,
    quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS,
    task_id: str = "task-1",
    capture_role: str = "primary",
    artifact_role: str = "primary",
) -> EvidenceInputCandidate:
    return EvidenceInputCandidate(
        manifest=_manifest(
            capture_id,
            task_id=task_id,
            quality_status=quality_status,
            artifact_role=artifact_role,
        ),
        capture_role=capture_role,
        page_sequence=page_sequence,
        journey_execution_id=journey_execution_id,
        environment_profile_id=environment_profile_id,
    )


def _seal(
    *,
    scope: EvidenceInputScope = EvidenceInputScope.TASK,
    eligible_capture_ids: tuple[str, ...] = ("capture-1", "capture-2"),
    candidates: tuple[EvidenceInputCandidate, ...] | None = None,
    sealed_at: datetime = NOW,
) -> EvidenceInputSet:
    actual_candidates = candidates or (
        _candidate("capture-2", page_sequence=2),
        _candidate("capture-1", page_sequence=1),
    )
    return seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-toast-style",
        rule_code="alioth_spec_48",
        rule_contract_hash="rule-contract-hash",
        ruleset_snapshot_hash="ruleset-snapshot-hash",
        scope=scope,
        scope_key="toast_style",
        finalization_epoch=3,
        eligibility_snapshot_id="frontier-snapshot-9",
        eligibility_snapshot_hash="frontier-snapshot-hash",
        selection_policy_hash="selection-policy-hash",
        eligible_capture_ids=eligible_capture_ids,
        candidates=actual_candidates,
        sealed_at=sealed_at,
    )


def test_task_scope_seals_authoritative_capture_order_and_round_trips() -> None:
    sealed = _seal()

    assert sealed.eligible_capture_ids == ("capture-1", "capture-2")
    assert tuple(item.capture_id for item in sealed.members) == sealed.eligible_capture_ids
    assert tuple(item.ordinal for item in sealed.members) == (1, 2)
    assert sealed.members[0].artifact_ids == ("artifact-capture-1",)
    assert EvidenceInputSet.from_dict(sealed.to_dict()) == sealed


def test_sealed_identity_is_idempotent_across_retry_time() -> None:
    first = _seal(sealed_at=NOW)
    replay = _seal(sealed_at=NOW + timedelta(minutes=5))

    assert replay.input_set_id == first.input_set_id
    assert replay.input_set_hash == first.input_set_hash
    assert replay.sealed_at != first.sealed_at


def test_seal_rejects_partial_candidate_set_and_non_pass_manifest() -> None:
    with pytest.raises(EvidenceInputSetValidationError, match="完整覆盖"):
        _seal(candidates=(_candidate("capture-1", page_sequence=1),))

    with pytest.raises(EvidenceInputSetValidationError, match="PASS Manifest"):
        _seal(
            candidates=(
                _candidate("capture-1", page_sequence=1),
                _candidate(
                    "capture-2",
                    page_sequence=2,
                    quality_status=EvidenceQualityStatus.FAIL,
                ),
            )
        )


def test_seal_rejects_manifest_from_another_task() -> None:
    with pytest.raises(EvidenceInputSetValidationError, match="同一任务"):
        _seal(
            candidates=(
                _candidate("capture-1", page_sequence=1),
                _candidate("capture-2", page_sequence=2, task_id="task-2"),
            )
        )


def test_seal_rejects_manifest_artifact_role_mismatch() -> None:
    with pytest.raises(EvidenceInputSetValidationError, match="附件角色"):
        _seal(
            candidates=(
                _candidate("capture-1", page_sequence=1),
                _candidate(
                    "capture-2",
                    page_sequence=2,
                    artifact_role="after_action",
                ),
            )
        )


def test_page_scope_cannot_anchor_multiple_captures() -> None:
    with pytest.raises(EvidenceInputSetValidationError, match="只能绑定一个"):
        _seal(scope=EvidenceInputScope.PAGE)


def test_journey_scope_requires_one_shared_journey_execution() -> None:
    valid = _seal(
        scope=EvidenceInputScope.JOURNEY,
        candidates=(
            _candidate(
                "capture-1",
                page_sequence=1,
                journey_execution_id="journey-1",
            ),
            _candidate(
                "capture-2",
                page_sequence=2,
                journey_execution_id="journey-1",
            ),
        ),
    )
    assert {item.journey_execution_id for item in valid.members} == {"journey-1"}

    with pytest.raises(EvidenceInputSetValidationError, match="同一 Journey"):
        _seal(
            scope=EvidenceInputScope.JOURNEY,
            candidates=(
                _candidate(
                    "capture-1",
                    page_sequence=1,
                    journey_execution_id="journey-1",
                ),
                _candidate(
                    "capture-2",
                    page_sequence=2,
                    journey_execution_id="journey-2",
                ),
            ),
        )


def test_environment_matrix_requires_distinct_environment_profiles() -> None:
    valid = _seal(
        scope=EvidenceInputScope.ENVIRONMENT_MATRIX,
        candidates=(
            _candidate(
                "capture-1",
                page_sequence=1,
                environment_profile_id="chromium-1024x768",
            ),
            _candidate(
                "capture-2",
                page_sequence=1,
                environment_profile_id="webkit-1920x1080",
            ),
        ),
    )
    assert tuple(item.environment_profile_id for item in valid.members) == (
        "chromium-1024x768",
        "webkit-1920x1080",
    )

    with pytest.raises(EvidenceInputSetValidationError, match="不能重复"):
        _seal(
            scope=EvidenceInputScope.ENVIRONMENT_MATRIX,
            candidates=(
                _candidate(
                    "capture-1",
                    page_sequence=1,
                    environment_profile_id="chromium-1024x768",
                ),
                _candidate(
                    "capture-2",
                    page_sequence=1,
                    environment_profile_id="chromium-1024x768",
                ),
            ),
        )

    with pytest.raises(EvidenceInputSetValidationError, match="同一逻辑页面"):
        _seal(
            scope=EvidenceInputScope.ENVIRONMENT_MATRIX,
            candidates=(
                _candidate(
                    "capture-1",
                    page_sequence=1,
                    environment_profile_id="chromium-1024x768",
                ),
                _candidate(
                    "capture-2",
                    page_sequence=2,
                    environment_profile_id="webkit-1920x1080",
                ),
            ),
        )


def test_input_set_rejects_member_or_hash_tampering() -> None:
    sealed = _seal()

    with pytest.raises(EvidenceInputSetValidationError, match="完整覆盖"):
        replace(sealed, eligible_capture_ids=("capture-2", "capture-1"))
    with pytest.raises(EvidenceInputSetValidationError, match="input_set_hash"):
        replace(sealed, input_set_hash="tampered")
