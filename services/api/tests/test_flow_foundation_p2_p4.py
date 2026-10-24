"""跨端流程基建 P2 控制／P3 连续证据／P4 试点合同回归。"""

from __future__ import annotations

import pytest

from services.api.app.application.flow_foundation_control import (
    FlowFoundationControlError,
    FlowFoundationControlService,
    InMemoryFlowControlStore,
)
from services.api.app.domain.exploration.flow_foundation import (
    FlowEvidenceState,
    FlowExecutionResult,
    FlowFoundationContractError,
    FlowResumeResult,
    FlowTriStateOutcome,
    TargetPlatform,
)
from services.api.app.domain.exploration.flow_foundation_control import (
    BudgetReservationStatus,
    ResumeEquivalenceFacts,
    TriggerDecisionKind,
    TriggerKeyParts,
    TriggerReasonCode,
    assert_clear_data_not_ordinary_return,
    assert_short_flow_not_nested,
    assert_trigger_state_payload_allowed,
    build_trigger_key,
    evaluate_trigger,
)
from services.api.app.domain.exploration.flow_foundation_evidence import (
    CaptureAttemptIdentity,
    CaptureAttemptKind,
    EvidenceSequenceSeal,
    ObservationClockDomain,
    ObservationSequenceDraft,
    SampleFrameRef,
    SampleIntegrityKind,
    TimedObservationProtocolVersion,
    assert_app_partial_media_not_playback_proof,
    assert_app_screenshot_tree_not_same_frame_assumption,
    assert_late_fill_creates_new_revision,
    assert_missing_indices_preserved,
    assert_observed_at_not_persisted_legacy,
    assert_timed_observation_v3_enabled,
)
from services.api.app.domain.exploration.flow_foundation_pilot import (
    PilotAcceptanceStatus,
    PilotPlan,
    PilotScenario,
    assert_pilot_plan_allowed,
)


def _parts(**overrides: object) -> TriggerKeyParts:
    base = dict(
        task_id="task-1",
        exploration_scope_id="scope-1",
        target_platform=TargetPlatform.APP,
        flow_code="detail_observe",
        definition_hash="def-hash",
        target_scope="pkg:demo/account:a",
        semantic_page_identity="detail:sku-1",
        subject_or_occurrence="subject:sku-1",
        trigger_state_hash="state:ready",
        requirement_contract_hash="req:v1",
    )
    base.update(overrides)
    return TriggerKeyParts(**base)  # type: ignore[arg-type]


def test_p2_trigger_key_stable_and_forbids_volatile_fields() -> None:
    key_a = build_trigger_key(_parts())
    key_b = build_trigger_key(_parts())
    assert key_a == key_b
    key_subject = build_trigger_key(_parts(subject_or_occurrence="subject:sku-2"))
    assert key_subject != key_a
    with pytest.raises(FlowFoundationContractError, match="禁止字段"):
        assert_trigger_state_payload_allowed({"screenshot_hash": "abc"})
    with pytest.raises(FlowFoundationContractError, match="禁止字段"):
        assert_trigger_state_payload_allowed({"playback_seconds": 12})


def test_p2_evaluate_trigger_reasons() -> None:
    eligible = evaluate_trigger(
        observation_fresh=True,
        subject_confirmed=True,
        has_requirement_or_explore_goal=True,
        capabilities_ready=True,
        capabilities_partial_accepted=False,
        permission_allowed=True,
        budget_sufficient=True,
        already_claimed=False,
        trigger_key="flow-trigger:x",
    )
    assert eligible.kind is TriggerDecisionKind.ELIGIBLE
    deferred = evaluate_trigger(
        observation_fresh=True,
        subject_confirmed=True,
        has_requirement_or_explore_goal=True,
        capabilities_ready=True,
        capabilities_partial_accepted=False,
        permission_allowed=True,
        budget_sufficient=False,
        already_claimed=False,
        trigger_key="flow-trigger:x",
    )
    assert deferred.kind is TriggerDecisionKind.DEFERRED
    assert deferred.reason_code is TriggerReasonCode.BUDGET_INSUFFICIENT


def test_p2_claim_unique_under_contention() -> None:
    service = FlowFoundationControlService(store=InMemoryFlowControlStore())
    decision = service.evaluate_trigger_candidate(
        parts=_parts(),
        trigger_state_payload={"login_state": "signed_in"},
        observation_fresh=True,
        subject_confirmed=True,
        has_requirement_or_explore_goal=True,
        capabilities_ready=True,
        capabilities_partial_accepted=False,
        permission_allowed=True,
        budget_sufficient=True,
    )
    assert decision.kind is TriggerDecisionKind.ELIGIBLE
    first = service.claim_flow(
        decision=decision,
        phase_run_id="phase-1",
        parent_work_kind="app_frontier",
        parent_work_id="work-1",
        parent_version=3,
        owner_execution_segment_id="seg-1",
        budget_units=2,
        task_id="task-1",
        expected_parent_version=3,
    )
    second = service.claim_flow(
        decision=decision,
        phase_run_id="phase-2",
        parent_work_kind="app_frontier",
        parent_work_id="work-1",
        parent_version=3,
        owner_execution_segment_id="seg-2",
        budget_units=2,
        task_id="task-1",
        expected_parent_version=3,
    )
    assert first.phase_run_id == "phase-1"
    assert second.phase_run_id == first.phase_run_id
    assert second.hold_id == first.hold_id
    hold = service._store.get_active_hold_for_parent(parent_work_id="work-1")
    assert hold is not None
    assert hold.frontier_ref.startswith("app-frontier:")


def test_p2_resume_rejects_same_activity_different_subject() -> None:
    service = FlowFoundationControlService()
    expected = ResumeEquivalenceFacts(
        package_name="com.demo",
        activity=".Detail",
        process_or_data_container="pid:1",
        account_context="user:a",
        subject_ref="sku:1",
    )
    observed = ResumeEquivalenceFacts(
        package_name="com.demo",
        activity=".Detail",
        process_or_data_container="pid:1",
        account_context="user:a",
        subject_ref="sku:2",
    )
    assert service.assess_resume(expected=expected, observed=observed) == "reject_replan"
    assert service.assess_resume(expected=expected, observed=expected) == "restored"


def test_p2_clear_data_and_nested_forbidden() -> None:
    with pytest.raises(FlowFoundationContractError):
        assert_clear_data_not_ordinary_return(using_clear_data=True)
    with pytest.raises(FlowFoundationContractError):
        assert_short_flow_not_nested(active_short_flow_depth=1)
    service = FlowFoundationControlService()
    decision = service.evaluate_trigger_candidate(
        parts=_parts(),
        trigger_state_payload={},
        observation_fresh=True,
        subject_confirmed=True,
        has_requirement_or_explore_goal=True,
        capabilities_ready=True,
        capabilities_partial_accepted=False,
        permission_allowed=True,
        budget_sufficient=True,
    )
    with pytest.raises(FlowFoundationControlError, match="清数据"):
        service.claim_flow(
            decision=decision,
            phase_run_id="phase-1",
            parent_work_kind="app_frontier",
            parent_work_id="work-1",
            parent_version=1,
            owner_execution_segment_id="seg-1",
            budget_units=1,
            task_id="task-1",
            expected_parent_version=1,
            clear_data_return=True,
        )


def test_p2_cancel_tri_state_and_budget_unknown() -> None:
    service = FlowFoundationControlService()
    outcome = service.settle_cancel_deadline(
        cancelled=True,
        hard_deadline_passed=False,
        resume_attempted=True,
        resume_succeeded=True,
        evidence_complete=False,
    )
    assert outcome.execution_result is FlowExecutionResult.CANCELLED
    assert outcome.evidence_state is FlowEvidenceState.INCOMPLETE
    assert outcome.resume_result is FlowResumeResult.CANCELLED

    from services.api.app.domain.exploration.flow_foundation_control import (
        BudgetReservation,
    )

    reserved = BudgetReservation(
        consumption_id="c1",
        task_id="task-1",
        phase_run_id="phase-1",
        units=1,
        status=BudgetReservationStatus.RESERVED,
    )
    service._store.put_budget(reserved)
    unknown = service.settle_budget_unknown(
        reserved,
        outcome_unknown=True,
        proved_not_consumed=False,
    )
    assert unknown.status is BudgetReservationStatus.OUTCOME_UNKNOWN
    with pytest.raises(FlowFoundationControlError, match="不得先退款"):
        service._store.put_budget(
            BudgetReservation(
                consumption_id="c1",
                task_id="task-1",
                phase_run_id="phase-1",
                units=1,
                status=BudgetReservationStatus.RELEASED,
            )
        )


def test_p3_resource_event_seq_and_clock_domain() -> None:
    service = FlowFoundationControlService()
    a = service.allocate_resource_event_seq(
        resource_id="device-1",
        producer_epoch="epoch-1",
        sample_index=0,
    )
    b = service.allocate_resource_event_seq(
        resource_id="device-1",
        producer_epoch="epoch-1",
        sample_index=1,
    )
    assert a.resource_event_seq == 1
    assert b.resource_event_seq == 2
    with pytest.raises(FlowFoundationControlError):
        service.allocate_resource_event_seq(
            resource_id="device-1",
            producer_epoch="epoch-1",
            sample_index=2,
            clock_domain=ObservationClockDomain.PERSISTED_AT_LEGACY,
        )
    with pytest.raises(FlowFoundationContractError):
        assert_observed_at_not_persisted_legacy(
            clock_domain=ObservationClockDomain.PERSISTED_AT_LEGACY
        )


def test_p3_same_bytes_two_samples_and_missing_index() -> None:
    draft = ObservationSequenceDraft(series_id="series-1")
    draft.append_frame(
        SampleFrameRef(
            capture_id="c1",
            sample_index=0,
            content_digest="same-bytes",
        )
    )
    draft.append_frame(
        SampleFrameRef(
            capture_id="c1",
            sample_index=1,
            content_digest="same-bytes",
        )
    )
    draft.mark_missing(sample_index=2, reason="ui_budget_trim")
    assert len(draft.frames) == 3
    assert draft.frames[2].integrity is SampleIntegrityKind.MISSING
    assert_missing_indices_preserved(
        planned_indices=(0, 1, 2),
        frames=tuple(draft.frames),
    )
    with pytest.raises(FlowFoundationContractError, match="缺口"):
        assert_missing_indices_preserved(
            planned_indices=(0, 1, 2, 3),
            frames=tuple(draft.frames),
        )


def test_p3_app_partial_and_recapture_vs_reupload() -> None:
    assert_app_partial_media_not_playback_proof()
    with pytest.raises(FlowFoundationContractError):
        assert_app_screenshot_tree_not_same_frame_assumption(assume_same_frame=True)
    CaptureAttemptIdentity(
        kind=CaptureAttemptKind.RECAPTURE,
        capture_request_id="req-2",
        prior_capture_request_id="req-1",
        new_observation_window=True,
    )
    CaptureAttemptIdentity(
        kind=CaptureAttemptKind.REUPLOAD,
        capture_request_id="req-1",
        prior_capture_request_id="req-1",
        new_observation_window=False,
    )
    with pytest.raises(FlowFoundationContractError):
        CaptureAttemptIdentity(
            kind=CaptureAttemptKind.RECAPTURE,
            capture_request_id="req-1",
            prior_capture_request_id="req-1",
            new_observation_window=True,
        )


def test_p3_timed_observation_v3_and_seal_revision() -> None:
    assert (
        assert_timed_observation_v3_enabled(
            writer_supports_v3=True,
            reader_supports_v3=True,
            scheduler_supports_v3=True,
            adapter_supports_v3=True,
            requested_version=TimedObservationProtocolVersion.V3,
        )
        is TimedObservationProtocolVersion.V3
    )
    with pytest.raises(FlowFoundationContractError, match="均声明支持"):
        assert_timed_observation_v3_enabled(
            writer_supports_v3=True,
            reader_supports_v3=False,
            scheduler_supports_v3=True,
            adapter_supports_v3=True,
            requested_version="v3",
        )
    sealed = EvidenceSequenceSeal(
        series_id="s1",
        revision=1,
        previous_revision=None,
        input_set_id="input-1",
        task_terminal=False,
    )
    newer = assert_late_fill_creates_new_revision(
        sealed=sealed,
        late_fill=True,
        mutate_old_input_set=False,
        reopen_terminal_task=False,
    )
    assert newer.revision == 2
    assert newer.previous_revision == 1
    with pytest.raises(FlowFoundationContractError, match="InputSet"):
        assert_late_fill_creates_new_revision(
            sealed=sealed,
            late_fill=True,
            mutate_old_input_set=True,
            reopen_terminal_task=False,
        )
    with pytest.raises(FlowFoundationContractError, match="已结束"):
        assert_late_fill_creates_new_revision(
            sealed=EvidenceSequenceSeal(
                series_id="s1",
                revision=1,
                previous_revision=None,
                input_set_id="input-1",
                task_terminal=True,
            ),
            late_fill=True,
            mutate_old_input_set=False,
            reopen_terminal_task=True,
        )


def test_p4_pure_observation_pilot() -> None:
    service = FlowFoundationControlService()
    plan = PilotPlan(
        scenario=PilotScenario.PURE_OBSERVATION,
        flow_code="app_detail_observe",
        capability_ids=("device_screenshot", "uia_tree"),
        detail_business_state_reached=True,
    )
    passed = service.accept_pure_observation_pilot(
        plan,
        triggered=True,
        samples_recorded=True,
        seal_or_gap_explained=True,
    )
    assert passed.status is PilotAcceptanceStatus.PASSED
    failed = service.accept_pure_observation_pilot(
        plan,
        triggered=True,
        samples_recorded=True,
        seal_or_gap_explained=True,
        claimed_playback_pass=True,
    )
    assert failed.status is PilotAcceptanceStatus.FAILED
    with pytest.raises(FlowFoundationControlError):
        service.gate_pilot(
            PilotPlan(
                scenario=PilotScenario.PURE_OBSERVATION,
                flow_code="bad",
                capability_ids=("payment_or_account_mutation",),
                detail_business_state_reached=True,
            )
        )


def test_p4_low_risk_interact_return_pilot() -> None:
    service = FlowFoundationControlService()
    plan = PilotPlan(
        scenario=PilotScenario.LOW_RISK_INTERACT_RETURN,
        flow_code="app_expand_close_return",
        capability_ids=("device_screenshot", "window_process_identity"),
        interact_authorized=True,
        resume_verification_required=True,
    )
    tri = FlowTriStateOutcome(
        execution_result=FlowExecutionResult.SUCCEEDED,
        evidence_state=FlowEvidenceState.COMPLETE,
        resume_result=FlowResumeResult.RESTORED,
    )
    passed = service.accept_low_risk_interact_return_pilot(
        plan,
        action_ledger_present=True,
        resume_verified=True,
        tri_state=tri,
    )
    assert passed.status is PilotAcceptanceStatus.PASSED
    assert passed.tri_state is not None
    with pytest.raises(FlowFoundationContractError, match="清数据"):
        assert_pilot_plan_allowed(
            PilotPlan(
                scenario=PilotScenario.LOW_RISK_INTERACT_RETURN,
                flow_code="bad",
                capability_ids=("device_screenshot",),
                interact_authorized=True,
                resume_verification_required=True,
                uses_clear_data=True,
            )
        )
