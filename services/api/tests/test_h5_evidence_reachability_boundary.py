"""H5 补采可达性边界。"""

from datetime import UTC, datetime, timedelta

from services.api.app.application.h5_evidence_reachability_boundary import (
    H5_EVIDENCE_TARGET_UNREACHABLE,
    apply_h5_evidence_reachability_boundary,
)
from services.api.app.domain.inspection import (
    OVERLAY_CLOSE_SEMANTIC_TARGET,
    EvidenceInteractionRequirement,
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RuleDecision,
    RuleJudgeOutcome,
    build_evidence_request,
)
from services.api.app.domain.interaction import InteractionOperation


def _request(*, page_state: str):
    return build_evidence_request(
        work_item_id="work-h5",
        attempt_id="attempt-h5",
        task_id="task-h5",
        rule_id="rule-h5",
        rule_code="alioth_h5",
        target_capture_id="capture-h5",
        reason_code="EVIDENCE_INSUFFICIENT",
        evidence_goal="需要底层页面证据",
        required_artifact_types=("screenshot", "ui_snapshot"),
        required_interaction=EvidenceInteractionRequirement(
            operation=InteractionOperation.CLICK,
            semantic_target=OVERLAY_CLOSE_SEMANTIC_TARGET,
            purpose="关闭遮挡后采集底页",
        ),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"surface_kind": "web"},
            "semantic": {"page_state": page_state},
            "structure": {"layer_target": "base_page"},
        },
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _outcome(request) -> RuleJudgeOutcome:
    decision = RuleDecision(
        work_item_id=request.work_item_id,
        attempt_id=request.attempt_id,
        rule_id=request.rule_id,
        rule_code=request.rule_code,
        execution_status="evidence_insufficient",
        payload={
            "execution_status": "evidence_insufficient",
            "judge_output": {"evidence_gap": {"gap_id": "gap-h5"}},
        },
    )
    return RuleJudgeOutcome(
        work_item_id=request.work_item_id,
        attempt_id=request.attempt_id,
        task_id=request.task_id,
        evidence_requests=(request,),
        deferred_decisions=(decision,),
    )


def test_h5_entitlement_lock_does_not_create_impossible_close_probe() -> None:
    request = _request(page_state="entitlement_locked")

    result = apply_h5_evidence_reachability_boundary(
        outcome=_outcome(request),
        target_platform="H5",
    )

    assert result.suppressed_request_ids == (request.request_id,)
    assert result.outcome.evidence_requests == ()
    assert result.outcome.deferred_decisions == ()
    decision = result.outcome.decisions[0]
    assert decision.execution_status == "evidence_insufficient"
    assert decision.payload["error_code"] == H5_EVIDENCE_TARGET_UNREACHABLE
    assert decision.payload["judge_output"]["evidence_gap"] == {"gap_id": "gap-h5"}


def test_h5_regular_popup_close_request_remains_routable() -> None:
    request = _request(page_state="popup")

    result = apply_h5_evidence_reachability_boundary(
        outcome=_outcome(request),
        target_platform="h5",
    )

    assert result.suppressed is False
    assert result.outcome.evidence_requests == (request,)


def test_web_entitlement_request_is_outside_h5_boundary() -> None:
    request = _request(page_state="entitlement_locked")

    result = apply_h5_evidence_reachability_boundary(
        outcome=_outcome(request),
        target_platform="web",
    )

    assert result.suppressed is False
    assert result.outcome.evidence_requests == (request,)
