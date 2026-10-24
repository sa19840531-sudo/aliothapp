"""跨端流程基建 P0／P1 领域合同回归。"""

from __future__ import annotations

import pytest

from services.api.app.domain.exploration.flow_foundation import (
    APP_FLOW_SINGLE_ACTION_ENTRY,
    DEFAULT_APP_FLOW_CAPABILITY_MATRIX,
    FLOW_FOUNDATION_IDENTITY_KEYS,
    ArtifactCommitment,
    CaptureOwnershipBinding,
    CaptureReservation,
    CaptureReservationStatus,
    DeviceExecutionPermit,
    DeviceLeaseHandoff,
    FlowEvidenceState,
    FlowExecutionResult,
    FlowFoundationContractError,
    FlowResumeResult,
    FlowTriStateOutcome,
    TargetPlatform,
    app_capability_status,
    assert_capture_binding_matches,
    assert_execution_permit_active,
    assert_fencing_token_matches,
    assert_formal_evidence_has_commitment,
    assert_late_upload_stays_with_owner,
    assert_lease_handoff_safe,
    assert_pilot_capabilities_allowed,
    assert_playback_proof_flow_allowed,
    assert_pure_observation_has_no_action_id,
    assert_single_app_action_entry,
    assert_single_device_executor,
    assert_target_platform_surface_separation,
    build_capture_request_identity,
    decide_unknown_action_replay,
    reject_collapsed_success_payload,
    resolve_app_device_conflict_id,
)
from services.api.app.domain.interaction.capabilities import CapabilityStatus
from services.api.app.domain.interaction.enums import SurfaceKind


def test_p0_identity_dictionary_frozen() -> None:
    assert "task_id" in FLOW_FOUNDATION_IDENTITY_KEYS
    assert "device_id" in FLOW_FOUNDATION_IDENTITY_KEYS
    assert "fencing_token" in FLOW_FOUNDATION_IDENTITY_KEYS
    assert "phase_run_id" in FLOW_FOUNDATION_IDENTITY_KEYS
    assert len(FLOW_FOUNDATION_IDENTITY_KEYS) == len(set(FLOW_FOUNDATION_IDENTITY_KEYS))


def test_p0_tri_state_rejects_collapsed_success() -> None:
    reject_collapsed_success_payload(
        {
            "execution_result": "succeeded",
            "evidence_state": "complete",
            "resume_result": "restored",
        }
    )
    with pytest.raises(FlowFoundationContractError, match="单一 success"):
        reject_collapsed_success_payload({"success": True})
    with pytest.raises(FlowFoundationContractError, match="单一 success"):
        reject_collapsed_success_payload({"status": "succeeded"})


def test_p0_tri_state_roundtrip() -> None:
    outcome = FlowTriStateOutcome(
        execution_result=FlowExecutionResult.SUCCEEDED,
        evidence_state=FlowEvidenceState.INCOMPLETE,
        resume_result=FlowResumeResult.FAILED,
    )
    restored = FlowTriStateOutcome.from_dict(outcome.to_dict())
    assert restored.evidence_state is FlowEvidenceState.INCOMPLETE
    assert restored.resume_result is FlowResumeResult.FAILED


def test_p0_single_action_entry() -> None:
    assert_single_app_action_entry(APP_FLOW_SINGLE_ACTION_ENTRY)
    with pytest.raises(FlowFoundationContractError, match="竞争"):
        assert_single_app_action_entry("parallel_cloud_phone_driver_loop")
    with pytest.raises(FlowFoundationContractError, match="capture_coordinator"):
        assert_single_app_action_entry("ad_hoc_adb")


def test_p0_platform_surface_and_device_conflict() -> None:
    assert_target_platform_surface_separation(
        target_platform=TargetPlatform.APP,
        surface_kind=SurfaceKind.ANDROID,
    )
    with pytest.raises(FlowFoundationContractError, match="WebView"):
        assert_target_platform_surface_separation(
            target_platform=TargetPlatform.APP,
            surface_kind=SurfaceKind.WEB,
        )
    native = resolve_app_device_conflict_id(
        device_id="dev-1",
        surface_kind=SurfaceKind.ANDROID,
    )
    webview = resolve_app_device_conflict_id(
        device_id="dev-1",
        surface_kind=SurfaceKind.ANDROID,
        webview_surface=True,
    )
    assert native == webview == "dev-1"


def test_p0_capability_matrix_blocks_playback_proof_and_forbidden_pilot() -> None:
    assert (
        app_capability_status("media_playback_progress_proof")
        is CapabilityStatus.UNSUPPORTED
    )
    with pytest.raises(FlowFoundationContractError, match="播放验证"):
        assert_playback_proof_flow_allowed()
    with pytest.raises(FlowFoundationContractError, match="禁止"):
        assert_pilot_capabilities_allowed(("payment_or_account_mutation",))
    with pytest.raises(FlowFoundationContractError, match="unsupported"):
        assert_pilot_capabilities_allowed(("webview_dom_debug",))
    assert_pilot_capabilities_allowed(("device_screenshot", "window_process_identity"))
    assert len(DEFAULT_APP_FLOW_CAPABILITY_MATRIX) >= 8


def test_p1_capture_binding_rejects_wrong_action() -> None:
    expected = CaptureOwnershipBinding(
        task_id="task-a",
        capture_id="cap-1",
        action_execution_id="act-1",
        session_id="sess-1",
        owner_phase_run_id="phase-1",
    )
    assert_capture_binding_matches(
        expected,
        task_id="task-a",
        capture_id="cap-1",
        action_execution_id="act-1",
        session_id="sess-1",
        phase_run_id="phase-1",
    )
    with pytest.raises(FlowFoundationContractError, match="错误动作"):
        assert_capture_binding_matches(
            expected,
            task_id="task-a",
            capture_id="cap-1",
            action_execution_id="act-other",
            session_id="sess-1",
            phase_run_id="phase-1",
        )
    with pytest.raises(FlowFoundationContractError, match="跨任务"):
        assert_capture_binding_matches(
            expected,
            task_id="task-b",
            capture_id="cap-1",
        )


def test_p1_reservation_requires_commitment_for_formal_evidence() -> None:
    ownership = CaptureOwnershipBinding(task_id="task-a", capture_id="cap-1")
    reservation = CaptureReservation(
        capture_request_id="req-1",
        capture_id="cap-1",
        task_id="task-a",
        status=CaptureReservationStatus.RESERVED,
        ownership=ownership,
        artifact_slots=("screenshot",),
    )
    with pytest.raises(FlowFoundationContractError, match="没有内容承诺"):
        assert_formal_evidence_has_commitment(reservation=reservation, commitment=None)
    commitment = ArtifactCommitment(
        capture_request_id="req-1",
        capture_id="cap-1",
        artifact_slot="screenshot",
        content_digest="sha256:ab",
        content_length=12,
        produced_under_fencing_token="fence-1",
    )
    assert_formal_evidence_has_commitment(reservation=reservation, commitment=commitment)


def test_p1_late_upload_and_pure_observation() -> None:
    with pytest.raises(FlowFoundationContractError, match="原预约"):
        assert_late_upload_stays_with_owner(
            material_owner_task_id="task-a",
            receiving_runtime_task_id="task-b",
        )
    assert_late_upload_stays_with_owner(
        material_owner_task_id="task-a",
        receiving_runtime_task_id="task-a",
    )
    assert_pure_observation_has_no_action_id(None)
    with pytest.raises(FlowFoundationContractError, match="纯观察"):
        assert_pure_observation_has_no_action_id("act-1")


def test_p1_device_permit_fence_and_single_executor() -> None:
    permit = DeviceExecutionPermit(
        device_id="dev-1",
        resource_id="res-1",
        fencing_token="fence-1",
        session_id="sess-1",
        execution_segment_id="seg-1",
        task_id="task-a",
    )
    assert_fencing_token_matches(permit, presented_fencing_token="fence-1")
    with pytest.raises(FlowFoundationContractError, match="fencing"):
        assert_fencing_token_matches(permit, presented_fencing_token="fence-old")
    revoked = DeviceExecutionPermit(
        device_id="dev-1",
        resource_id="res-1",
        fencing_token="fence-1",
        session_id="sess-1",
        execution_segment_id="seg-1",
        task_id="task-a",
        revoked=True,
    )
    with pytest.raises(FlowFoundationContractError, match="撤销"):
        assert_execution_permit_active(revoked)
    assert_single_device_executor(
        device_id="dev-1",
        active_session_id="sess-1",
        claimant_session_id="sess-1",
    )
    with pytest.raises(FlowFoundationContractError, match="绕锁"):
        assert_single_device_executor(
            device_id="dev-1",
            active_session_id="sess-1",
            claimant_session_id="sess-2",
        )


def test_p1_lease_handoff_and_unknown_replay() -> None:
    handoff = DeviceLeaseHandoff(
        device_id="dev-1",
        task_id="task-a",
        predecessor_session_id="sess-1",
        successor_session_id="sess-2",
        predecessor_fencing_token="fence-1",
        successor_fencing_token="fence-2",
        predecessor_channel_silenced=True,
    )
    assert_lease_handoff_safe(handoff)
    dirty = DeviceLeaseHandoff(
        device_id="dev-1",
        task_id="task-a",
        predecessor_session_id="sess-1",
        successor_session_id="sess-2",
        predecessor_fencing_token="fence-1",
        successor_fencing_token="fence-2",
        predecessor_channel_silenced=False,
    )
    with pytest.raises(FlowFoundationContractError, match="静默"):
        assert_lease_handoff_safe(dirty)
    assert decide_unknown_action_replay(
        outcome_unknown=True, proved_not_occurred=False
    ) == "forbid_replay"
    assert decide_unknown_action_replay(
        outcome_unknown=True, proved_not_occurred=True
    ) == "allow_replay"


def test_p1_capture_request_identity_stable() -> None:
    first = build_capture_request_identity(
        task_id="task-a",
        phase_run_id="phase-1",
        checkpoint_key="observe",
        sample_index=0,
        producer_epoch="p1",
    )
    second = build_capture_request_identity(
        task_id="task-a",
        phase_run_id="phase-1",
        checkpoint_key="observe",
        sample_index=0,
        producer_epoch="p1",
    )
    third = build_capture_request_identity(
        task_id="task-a",
        phase_run_id="phase-1",
        checkpoint_key="observe",
        sample_index=1,
        producer_epoch="p1",
    )
    assert first == second
    assert first != third
    assert first.startswith("capture-request:")
