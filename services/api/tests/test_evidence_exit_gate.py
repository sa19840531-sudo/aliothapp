"""Evidence Exit Gate 四项公式与需求快照合同测试。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.evidence_exit_gate import (
    EvidenceExitGateBlockedError,
    build_evidence_exit_blocked_outcome,
)
from services.api.app.domain.exploration import (
    EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1,
    EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V2,
    EvidenceExitBlockReason,
    EvidenceExitBusinessOutcome,
    EvidenceExitGateDecision,
    EvidenceExitGateSnapshot,
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_gate_decision,
    build_evidence_exit_requirement_set,
)


def _snapshot(**overrides) -> EvidenceExitGateSnapshot:
    values = {
        "task_id": "task-1",
        "session_id": "session-1",
        "source_capture_id": "capture-1",
        "requirement_set_id": "requirements-1",
        "baseline_pass_manifest_count": 1,
        "nonterminal_active_session_requirement_count": 0,
        "nonterminal_inline_evidence_request_count": 0,
        "evidence_commit_in_progress_count": 0,
        "evaluated_at": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return EvidenceExitGateSnapshot(**values)


def test_exit_gate_allows_only_when_all_four_facts_pass() -> None:
    decision = build_evidence_exit_gate_decision(
        action_intent_id="intent-1",
        snapshot=_snapshot(),
    )

    assert decision.allow_leave is True
    assert decision.block_reasons == ()
    assert EvidenceExitGateDecision.from_dict(decision.to_dict()) == decision


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"baseline_pass_manifest_count": 0},
            EvidenceExitBlockReason.BASELINE_EVIDENCE_INCOMPLETE,
        ),
        (
            {"requirement_set_id": None},
            EvidenceExitBlockReason.ACTIVE_SESSION_REQUIREMENTS_NONTERMINAL,
        ),
        (
            {"nonterminal_active_session_requirement_count": 1},
            EvidenceExitBlockReason.ACTIVE_SESSION_REQUIREMENTS_NONTERMINAL,
        ),
        (
            {"nonterminal_inline_evidence_request_count": 1},
            EvidenceExitBlockReason.INLINE_EVIDENCE_REQUEST_PENDING,
        ),
        (
            {"evidence_commit_in_progress_count": 1},
            EvidenceExitBlockReason.EVIDENCE_COMMIT_IN_PROGRESS,
        ),
    ],
)
def test_exit_gate_blocks_each_failed_fact(overrides, reason) -> None:
    decision = build_evidence_exit_gate_decision(
        action_intent_id="intent-1",
        snapshot=_snapshot(**overrides),
    )

    assert decision.allow_leave is False
    assert reason in decision.block_reasons


def test_app_exit_gate_ignores_pending_inline_evidence_requests() -> None:
    """App 课纲探索不得因 inline 补证未结清而拦离场／提前收口。"""

    decision = build_evidence_exit_gate_decision(
        action_intent_id="intent-app-inline",
        snapshot=_snapshot(
            surface_kind="android",
            requirement_set_id=None,
            baseline_pass_manifest_count=1,
            nonterminal_inline_evidence_request_count=2,
        ),
    )

    assert decision.allow_leave is True
    assert decision.block_reasons == ()
    assert (
        EvidenceExitBlockReason.INLINE_EVIDENCE_REQUEST_PENDING
        not in decision.block_reasons
    )


def test_app_blocked_outcome_does_not_treat_null_requirement_as_control_plane() -> None:
    """App 无 requirement_set 时，其它可重试阻断应归 EVIDENCE_PENDING。"""

    decision = build_evidence_exit_gate_decision(
        action_intent_id="intent-app-pending",
        snapshot=_snapshot(
            surface_kind="android",
            requirement_set_id=None,
            baseline_pass_manifest_count=1,
            evidence_commit_in_progress_count=1,
            latest_manifest_id="manifest-1",
            latest_manifest_version=1,
            latest_manifest_hash="d" * 64,
            latest_manifest_quality_status="pass",
        ),
    )
    outcome = build_evidence_exit_blocked_outcome(decision=decision)

    assert decision.allow_leave is False
    assert EvidenceExitBlockReason.EVIDENCE_COMMIT_IN_PROGRESS in decision.block_reasons
    assert outcome.outcome is EvidenceExitBusinessOutcome.EVIDENCE_PENDING
    assert outcome.error_code == "EVIDENCE_COMMIT_PENDING"
    assert outcome.retryable is True


def test_evaluation_identity_ignores_audit_time_but_changes_with_facts() -> None:
    first = build_evidence_exit_gate_decision(
        action_intent_id="intent-1",
        snapshot=_snapshot(),
    )
    later = build_evidence_exit_gate_decision(
        action_intent_id="intent-1",
        snapshot=replace(
            first.snapshot,
            evaluated_at=first.snapshot.evaluated_at + timedelta(seconds=5),
        ),
    )
    changed = build_evidence_exit_gate_decision(
        action_intent_id="intent-1",
        snapshot=replace(
            first.snapshot,
            nonterminal_inline_evidence_request_count=1,
        ),
    )

    assert later.evaluation_id == first.evaluation_id
    assert changed.evaluation_id != first.evaluation_id


def test_historical_v1_snapshot_parses_without_manifest_lineage() -> None:
    snapshot = EvidenceExitGateSnapshot.from_dict(
        {
            "protocol_version": EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1,
            "task_id": "task-1",
            "session_id": "session-1",
            "source_capture_id": "capture-1",
            "requirement_set_id": "requirements-1",
            "baseline_pass_manifest_count": 0,
            "nonterminal_active_session_requirement_count": 0,
            "nonterminal_inline_evidence_request_count": 0,
            "evidence_commit_in_progress_count": 0,
            "evaluated_at": "2026-08-03T12:00:00+00:00",
            "surface_kind": "web",
        }
    )

    assert snapshot.protocol_version == EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1
    assert snapshot.latest_manifest_id is None
    assert snapshot.lineage_integrity_failed is False


def test_historical_v2_snapshot_keeps_original_identity_shape() -> None:
    snapshot = EvidenceExitGateSnapshot.from_dict(
        {
            "protocol_version": EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V2,
            "task_id": "task-1",
            "session_id": "session-1",
            "source_capture_id": "capture-1",
            "requirement_set_id": "requirements-1",
            "baseline_pass_manifest_count": 1,
            "nonterminal_active_session_requirement_count": 0,
            "nonterminal_inline_evidence_request_count": 0,
            "evidence_commit_in_progress_count": 0,
            "evaluated_at": "2026-08-03T12:00:00+00:00",
            "surface_kind": "android",
            "latest_manifest_id": "manifest-1",
            "latest_manifest_version": 1,
            "latest_manifest_hash": "a" * 64,
            "latest_manifest_quality_status": "pass",
            "latest_manifest_quality_reason": None,
            "lineage_integrity_failed": False,
        }
    )

    decision = build_evidence_exit_gate_decision(
        action_intent_id="intent-v2",
        snapshot=snapshot,
    )
    payload = decision.to_dict()

    assert "baseline_manifest_total_count" not in payload["snapshot"]
    assert EvidenceExitGateDecision.from_dict(payload) == decision


@pytest.mark.parametrize(
    ("overrides", "business_outcome", "error_code", "retryable"),
    [
        (
            {
                "baseline_pass_manifest_count": 0,
                "latest_manifest_id": "manifest-1",
                "latest_manifest_version": 1,
                "latest_manifest_hash": "a" * 64,
                "latest_manifest_quality_status": "fail",
                "latest_manifest_quality_reason": "缺少可裁决附件",
            },
            EvidenceExitBusinessOutcome.EVIDENCE_INSUFFICIENT,
            "EVIDENCE_INSUFFICIENT",
            False,
        ),
        (
            {
                "baseline_pass_manifest_count": 0,
                "latest_manifest_id": "manifest-1",
                "latest_manifest_version": 2,
                "latest_manifest_hash": "b" * 64,
                "latest_manifest_quality_status": "pending",
            },
            EvidenceExitBusinessOutcome.EVIDENCE_PENDING,
            "EVIDENCE_COMMIT_PENDING",
            True,
        ),
        (
            {"baseline_pass_manifest_count": 0, "requirement_set_id": None},
            EvidenceExitBusinessOutcome.CONTROL_PLANE_INCOMPLETE,
            "EVIDENCE_CONTROL_PLANE_INCOMPLETE",
            True,
        ),
        (
            {
                "baseline_pass_manifest_count": 0,
                "latest_manifest_id": "manifest-1",
                "latest_manifest_version": 1,
                "latest_manifest_hash": "c" * 64,
                "latest_manifest_quality_status": "fail",
                "latest_manifest_quality_reason": "身份冲突",
                "lineage_integrity_failed": True,
            },
            EvidenceExitBusinessOutcome.EVIDENCE_INTEGRITY_FAILED,
            "EVIDENCE_INTEGRITY_FAILED",
            False,
        ),
    ],
)
def test_blocked_outcome_classification(
    overrides,
    business_outcome,
    error_code,
    retryable,
) -> None:
    decision = build_evidence_exit_gate_decision(
        action_intent_id="intent-outcome-1",
        snapshot=_snapshot(**overrides),
    )
    outcome = build_evidence_exit_blocked_outcome(decision=decision)
    error = EvidenceExitGateBlockedError(decision, outcome)

    assert outcome.outcome is business_outcome
    assert outcome.error_code == error_code
    assert outcome.retryable is retryable
    assert type(outcome).from_dict(outcome.to_dict()) == outcome
    assert error.error_code == error_code
    assert error.retryable is retryable


def test_manifest_lineage_changes_identity_but_evaluated_at_does_not() -> None:
    snapshot = _snapshot(
        baseline_pass_manifest_count=0,
        latest_manifest_id="manifest-1",
        latest_manifest_version=1,
        latest_manifest_hash="a" * 64,
        latest_manifest_quality_status="fail",
        latest_manifest_quality_reason="缺少证据",
    )
    first = build_evidence_exit_gate_decision(
        action_intent_id="intent-lineage-1",
        snapshot=snapshot,
    )
    later = build_evidence_exit_gate_decision(
        action_intent_id="intent-lineage-1",
        snapshot=replace(snapshot, evaluated_at=snapshot.evaluated_at + timedelta(seconds=10)),
    )
    changed = build_evidence_exit_gate_decision(
        action_intent_id="intent-lineage-1",
        snapshot=replace(
            snapshot,
            latest_manifest_id="manifest-2",
            latest_manifest_version=2,
            latest_manifest_hash="b" * 64,
        ),
    )

    first_outcome = build_evidence_exit_blocked_outcome(decision=first)
    later_outcome = build_evidence_exit_blocked_outcome(decision=later)
    changed_outcome = build_evidence_exit_blocked_outcome(decision=changed)
    assert first.evaluation_id == later.evaluation_id
    assert first_outcome.outcome_id == later_outcome.outcome_id
    assert first.evaluation_id != changed.evaluation_id
    assert first_outcome.outcome_id != changed_outcome.outcome_id


def test_empty_requirement_set_is_explicit_terminal_snapshot() -> None:
    requirement_set = build_evidence_exit_requirement_set(
        revision=1,
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        matcher_snapshot_hash="sha256:matcher",
        requirements=(),
        created_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )

    assert requirement_set.all_terminal is True
    assert requirement_set.nonterminal_count == 0
    assert EvidenceExitRequirementSet.from_dict(requirement_set.to_dict()) == requirement_set


def test_requirement_identity_is_stable_across_status_revisions() -> None:
    pending = build_active_session_evidence_requirement(
        task_id="task-1",
        capture_id="capture-1",
        rule_id="rule-1",
        rule_code="LOGIN_VISIBLE",
        required_artifact_types=("screenshot", "ui_snapshot"),
        required_capture_roles=("primary",),
        status=EvidenceExitRequirementStatus.PENDING,
        evidence_request_id="request-1",
    )
    satisfied = build_active_session_evidence_requirement(
        task_id="task-1",
        capture_id="capture-1",
        rule_id="rule-1",
        rule_code="LOGIN_VISIBLE",
        required_artifact_types=("ui_snapshot", "screenshot"),
        required_capture_roles=("primary",),
        status=EvidenceExitRequirementStatus.SATISFIED,
        evidence_request_id="request-1",
        resolution_manifest_id="manifest-1",
    )

    assert pending.requirement_id == satisfied.requirement_id


def test_terminal_requirement_must_record_resolution() -> None:
    with pytest.raises(ValueError, match="resolution_reason"):
        build_active_session_evidence_requirement(
            task_id="task-1",
            capture_id="capture-1",
            rule_id="rule-1",
            rule_code="LOGIN_VISIBLE",
            required_artifact_types=("screenshot",),
            required_capture_roles=(),
            status=EvidenceExitRequirementStatus.EVIDENCE_INSUFFICIENT,
        )
