"""App 探索 Evidence Index 领域合同测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.evidence import (
    APP_EXPLORATION_RESULT_PROTOCOL_VERSION,
    AppEvidenceGap,
    AppEvidenceIndexEntry,
    AppEvidenceIndexStatus,
    AppEvidenceSemanticRole,
    AppExplorationResult,
    AppExplorationResultOutcome,
    EvidenceContractValidationError,
    app_exploration_result_context_sha256,
    build_app_evidence_index,
    seal_app_evidence_index,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
RESULT_CONTEXT_SHA256 = app_exploration_result_context_sha256(
    coverage_summary={"stable_page_count": 2},
    started_at=NOW,
)


def _entry(
    *,
    capture_id: str,
    captured_at: datetime,
    role: AppEvidenceSemanticRole,
    sequence_no: int = 99,
) -> AppEvidenceIndexEntry:
    return AppEvidenceIndexEntry(
        sequence_no=sequence_no,
        capture_id=capture_id,
        evidence_manifest_id=f"manifest:{capture_id}",
        evidence_manifest_version=2,
        semantic_role=role,
        captured_at=captured_at,
        metadata={"source": "app_runtime"},
    )


def test_build_index_orders_entries_and_assigns_stable_sequence() -> None:
    later = _entry(
        capture_id="capture-b",
        captured_at=NOW + timedelta(seconds=1),
        role=AppEvidenceSemanticRole.ACTION_AFTER,
    )
    earlier = _entry(
        capture_id="capture-a",
        captured_at=NOW,
        role=AppEvidenceSemanticRole.BOOTSTRAP,
    )

    index = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(later, earlier),
        created_at=NOW,
        result_context_sha256=RESULT_CONTEXT_SHA256,
    )

    assert index.status is AppEvidenceIndexStatus.BUILDING
    assert [item.capture_id for item in index.entries] == ["capture-a", "capture-b"]
    assert [item.sequence_no for item in index.entries] == [1, 2]
    assert index.result_context_sha256 == RESULT_CONTEXT_SHA256


def test_seal_requires_result_context_and_round_trip_preserves_binding() -> None:
    building = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(
            _entry(
                capture_id="capture-final",
                captured_at=NOW,
                role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
            ),
        ),
        created_at=NOW,
    )

    with pytest.raises(EvidenceContractValidationError, match="结果上下文"):
        seal_app_evidence_index(
            building,
            outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
            terminal_reason_code="exploration_completed",
            sealed_at=NOW + timedelta(seconds=1),
        )

    sealed = seal_app_evidence_index(
        replace(building, result_context_sha256=RESULT_CONTEXT_SHA256),
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        sealed_at=NOW + timedelta(seconds=1),
    )

    assert sealed.result_context_sha256 == RESULT_CONTEXT_SHA256
    assert type(sealed).from_dict(sealed.to_dict()) == sealed


def test_large_long_running_index_round_trip_exceeds_single_message_limit() -> None:
    entries = tuple(
        replace(
            _entry(
                capture_id=f"capture-{sequence_no:03d}",
                captured_at=NOW + timedelta(seconds=sequence_no),
                role=AppEvidenceSemanticRole.SURFACE_REOBSERVE,
            ),
            metadata={"source": "app_runtime", "details": "长链证据" * 600},
        )
        for sequence_no in range(100)
    )
    index = build_app_evidence_index(
        task_id="task-long-running",
        run_id="run-long-running",
        build_id="build-1",
        surface_session_id="session-1",
        entries=entries,
        created_at=NOW,
        result_context_sha256=RESULT_CONTEXT_SHA256,
    )
    payload = index.to_dict()

    assert len(str(payload).encode("utf-8")) > 256_000
    assert type(index).from_dict(payload) == index


def test_seal_hash_is_recomputable_and_same_request_is_idempotent() -> None:
    index = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(
            _entry(
                capture_id="capture-a",
                captured_at=NOW,
                role=AppEvidenceSemanticRole.BOOTSTRAP,
            ),
        ),
        created_at=NOW,
        result_context_sha256=RESULT_CONTEXT_SHA256,
    )
    gap = AppEvidenceGap(
        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
        reason_code="final_observation_missing",
        description="最终观察缺失",
        metadata={"scope": "run"},
    )

    sealed = seal_app_evidence_index(
        index,
        outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
        terminal_reason_code="final_state_unconfirmed",
        evidence_gaps=(gap,),
        sealed_at=NOW + timedelta(seconds=2),
    )
    replay = seal_app_evidence_index(
        sealed,
        outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
        terminal_reason_code="final_state_unconfirmed",
        evidence_gaps=(gap,),
        sealed_at=NOW + timedelta(minutes=1),
    )

    assert replay is sealed
    assert sealed.status is AppEvidenceIndexStatus.SEALED
    assert sealed.content_sha256 == sealed.recompute_content_sha256()
    assert sealed.content_sha256.startswith("sha256:")


def test_sealed_index_rejects_changed_content() -> None:
    index = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(
            _entry(
                capture_id="capture-a",
                captured_at=NOW,
                role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
            ),
        ),
        created_at=NOW,
        result_context_sha256=RESULT_CONTEXT_SHA256,
    )
    sealed = seal_app_evidence_index(
        index,
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        sealed_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(EvidenceContractValidationError, match="已经封存"):
        seal_app_evidence_index(
            sealed,
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="minimum_coverage_not_met",
            sealed_at=NOW + timedelta(seconds=2),
        )

    with pytest.raises(EvidenceContractValidationError, match="哈希"):
        replace(sealed, content_sha256="sha256:" + "0" * 64)


def test_completed_index_rejects_evidence_gaps() -> None:
    index = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(),
        created_at=NOW,
        result_context_sha256=RESULT_CONTEXT_SHA256,
    )
    gap = AppEvidenceGap(
        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
        reason_code="manifest_missing",
        description="最终观察缺少 Manifest",
        metadata={"scope": "run"},
    )

    with pytest.raises(EvidenceContractValidationError, match="不能携带证据缺口"):
        seal_app_evidence_index(
            index,
            outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
            terminal_reason_code="exploration_completed",
            evidence_gaps=(gap,),
            sealed_at=NOW + timedelta(seconds=1),
        )


def test_completed_index_requires_final_observation() -> None:
    index = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(
            _entry(
                capture_id="capture-a",
                captured_at=NOW,
                role=AppEvidenceSemanticRole.BOOTSTRAP,
            ),
        ),
        created_at=NOW,
        result_context_sha256=RESULT_CONTEXT_SHA256,
    )

    with pytest.raises(EvidenceContractValidationError, match="Final Observation"):
        seal_app_evidence_index(
            index,
            outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
            terminal_reason_code="exploration_completed",
            sealed_at=NOW + timedelta(seconds=1),
        )


def test_run_level_gap_requires_explicit_scope_and_rejects_duplicates() -> None:
    index = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(),
        created_at=NOW,
        result_context_sha256=RESULT_CONTEXT_SHA256,
    )
    unscoped = AppEvidenceGap(
        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
        reason_code="final_observation_missing",
        description="最终观察缺失",
    )

    with pytest.raises(EvidenceContractValidationError, match="run scope"):
        seal_app_evidence_index(
            index,
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="evidence_incomplete",
            evidence_gaps=(unscoped,),
            sealed_at=NOW + timedelta(seconds=1),
        )

    scoped = replace(unscoped, metadata={"scope": "run"})
    with pytest.raises(EvidenceContractValidationError, match="重复证据缺口"):
        seal_app_evidence_index(
            index,
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="evidence_incomplete",
            evidence_gaps=(scoped, scoped),
            sealed_at=NOW + timedelta(seconds=1),
        )


def test_app_exploration_result_requires_sealed_index_identity() -> None:
    result = AppExplorationResult(
        task_id="task-1",
        run_id="run-1",
        surface_session_id="session-1",
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        evidence_index_id="app-evidence-index:abc",
        evidence_index_hash="sha256:" + "a" * 64,
        coverage_summary={"unique_state_count": 3},
        evidence_gaps=(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
    )

    payload = result.to_dict()

    assert payload["outcome"] == "exploration_completed"
    assert payload["protocol_version"] == APP_EXPLORATION_RESULT_PROTOCOL_VERSION
    assert payload["result_sha256"].startswith("sha256:")
    assert result.result_sha256 == result.recompute_result_sha256()
    assert AppExplorationResult.from_dict(payload).to_dict() == payload


def test_app_exploration_result_rejects_explicit_hash_mismatch() -> None:
    result = AppExplorationResult(
        task_id="task-1",
        run_id="run-1",
        surface_session_id="session-1",
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        evidence_index_id="app-evidence-index:abc",
        evidence_index_hash="sha256:" + "a" * 64,
        coverage_summary={"unique_state_count": 3},
        evidence_gaps=(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(EvidenceContractValidationError, match="result_sha256|哈希"):
        replace(result, result_sha256="sha256:" + "0" * 64)


def test_app_exploration_result_v1_without_hash_is_readable_and_hashed() -> None:
    result = AppExplorationResult(
        task_id="task-1",
        run_id="run-1",
        surface_session_id="session-1",
        outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
        terminal_reason_code="minimum_coverage_not_met",
        evidence_index_id="app-evidence-index:abc",
        evidence_index_hash="sha256:" + "b" * 64,
        coverage_summary={"unique_state_count": 1},
        evidence_gaps=(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
    )
    payload = result.to_dict()
    payload["protocol_version"] = "app-exploration-result.v1"
    payload.pop("result_sha256")

    restored = AppExplorationResult.from_dict(payload)

    assert restored.result_sha256.startswith("sha256:")
    assert restored.result_sha256 == restored.recompute_result_sha256()


def test_insufficient_result_round_trip_keeps_gap_outcome_and_hash_consistent() -> None:
    gap = AppEvidenceGap(
        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
        reason_code="manifest_missing",
        description="最终观察缺少 Evidence Manifest",
        capture_id="capture-final",
    )
    result = AppExplorationResult(
        task_id="task-1",
        run_id="run-1",
        surface_session_id="session-1",
        outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
        terminal_reason_code="evidence_incomplete",
        evidence_index_id="app-evidence-index:abc",
        evidence_index_hash="sha256:" + "c" * 64,
        coverage_summary={"missing_page_types": ["details"]},
        evidence_gaps=(gap,),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
    )

    restored = AppExplorationResult.from_dict(result.to_dict())

    assert restored.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert restored.terminal_reason_code == "evidence_incomplete"
    assert restored.evidence_gaps == (gap,)
    assert restored.result_sha256 == result.result_sha256
    assert restored.result_sha256 == restored.recompute_result_sha256()
