"""流程基建 P1 应用接线：预约服务与 CaptureCoordinator 入口守卫。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.capture_coordinator import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CaptureCoordinationStage,
    CaptureCoordinator,
)
from services.api.app.application.device_pool import DeviceLease
from services.api.app.application.flow_foundation_p1 import (
    FlowFoundationP1Error,
    FlowFoundationP1Service,
    InMemoryCaptureReservationStore,
)
from services.api.app.domain.exploration.flow_foundation import DeviceLeaseHandoff
from services.api.app.domain.interaction import (
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.tests.test_capture_coordinator import (
    RecordingCaptureWriter,
    RecordingEvidenceExitGate,
    RecordingInteractionExecutor,
    _command,
)


def test_p1_reserve_commit_and_formal_evidence() -> None:
    service = FlowFoundationP1Service(store=InMemoryCaptureReservationStore())
    reservation = service.reserve_capture(
        task_id="task-a",
        phase_run_id="phase-1",
        checkpoint_key="cp-home",
        sample_index=0,
        producer_epoch="epoch-1",
        capture_id="capture-1",
        session_id="session-1",
        pure_observation=True,
    )
    assert reservation.capture_request_id
    commitment = service.commit_artifact(
        reservation=reservation,
        artifact_slot="screenshot",
        content_digest="digest-1",
        content_length=12,
        fencing_token=7,
    )
    loaded, loaded_commitment = service.require_formal_evidence(
        capture_request_id=reservation.capture_request_id,
    )
    assert loaded.capture_id == "capture-1"
    assert loaded_commitment.content_digest == commitment.content_digest


def test_p1_pure_observation_rejects_forged_action_id() -> None:
    service = FlowFoundationP1Service()
    with pytest.raises(FlowFoundationP1Error) as exc_info:
        service.reserve_capture(
            task_id="task-a",
            phase_run_id="phase-1",
            checkpoint_key="cp-home",
            sample_index=0,
            producer_epoch="epoch-1",
            capture_id="capture-1",
            action_execution_id="forged-action",
            pure_observation=True,
        )
    assert exc_info.value.error_code == "PURE_OBSERVATION_ACTION_FORBIDDEN"


def test_p1_permit_and_lease_handoff() -> None:
    service = FlowFoundationP1Service()
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    lease = DeviceLease(
        lease_id="lease-1",
        device_ref="device-1",
        task_id="task-1",
        session_id="session-old",
        claimed_at=now,
        expires_at=now + timedelta(minutes=10),
        backend="cloud_phone",
        profile_id="profile-1",
        fencing_token=3,
        state="active",
    )
    permit = service.permit_from_device_lease(lease, execution_segment_id="seg-1")
    service.require_permit(permit, presented_fencing_token=3)
    with pytest.raises(FlowFoundationP1Error):
        service.require_permit(permit, presented_fencing_token=99)

    handoff = service.build_lease_handoff(
        predecessor=lease,
        successor_session_id="session-new",
        successor_fencing_token=4,
        predecessor_channel_silenced=True,
    )
    assert isinstance(handoff, DeviceLeaseHandoff)
    with pytest.raises(FlowFoundationP1Error):
        service.build_lease_handoff(
            predecessor=lease,
            successor_session_id="session-new",
            successor_fencing_token=4,
            predecessor_channel_silenced=False,
        )


def test_p1_unknown_replay_gate() -> None:
    service = FlowFoundationP1Service()
    assert (
        service.decide_replay_after_unknown(
            outcome_unknown=True,
            proved_not_occurred=True,
        )
        == "allow_replay"
    )
    with pytest.raises(FlowFoundationP1Error) as exc_info:
        service.decide_replay_after_unknown(
            outcome_unknown=True,
            proved_not_occurred=False,
        )
    assert exc_info.value.error_code == "UNKNOWN_ACTION_REPLAY_FORBIDDEN"


@pytest.mark.asyncio
async def test_coordinator_android_passes_flow_foundation_p1_gate() -> None:
    events: list[str] = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        flow_foundation_p1=FlowFoundationP1Service(),
    )
    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))
    assert events == ["capture:before_action", "execute", "capture:after_action"]
    assert result.interaction.status == InteractionResultStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_coordinator_rejects_when_flow_foundation_p1_blocks_android() -> None:
    class _BlockingGateway:
        def gate_android_coordination(self, *, surface_kind: SurfaceKind) -> None:
            if surface_kind is SurfaceKind.ANDROID:
                raise FlowFoundationP1Error(
                    "禁止第二执行入口",
                    error_code="APP_ACTION_ENTRY_VIOLATION",
                )

    events: list[str] = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        flow_foundation_p1=_BlockingGateway(),
    )
    with pytest.raises(CaptureCoordinationError) as exc_info:
        await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))
    assert exc_info.value.stage is CaptureCoordinationStage.BEFORE_CAPTURE
    assert exc_info.value.error_code == "APP_ACTION_ENTRY_VIOLATION"
    assert events == []


@pytest.mark.asyncio
async def test_coordinator_rejects_observe_with_forged_action_execution_id() -> None:
    base = _command(surface_kind=SurfaceKind.ANDROID)
    command = CaptureCoordinationCommand(
        agent_run_id=base.agent_run_id,
        intent=replace(
            base.intent,
            action_type=InteractionOperation.OBSERVE,
            semantic_target=None,
            purpose="纯观察采样",
            parameters={"action_execution_id": "forged-action"},
            capture_profile=CaptureProfile(
                profile_id="observe",
                before=CaptureDirective(role="before", artifact_types=("screenshot",)),
                after=CaptureDirective(role="primary", artifact_types=("screenshot",)),
            ),
        ),
    )
    events: list[str] = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        flow_foundation_p1=FlowFoundationP1Service(),
    )
    with pytest.raises(CaptureCoordinationError) as exc_info:
        await coordinator.coordinate(command)
    assert exc_info.value.error_code == "PURE_OBSERVATION_ACTION_FORBIDDEN"
    assert events == []


@pytest.mark.asyncio
async def test_coordinator_web_skips_android_entry_gate() -> None:
    class _AndroidOnlyBlocker:
        def gate_android_coordination(self, *, surface_kind: SurfaceKind) -> None:
            if surface_kind is SurfaceKind.ANDROID:
                raise FlowFoundationP1Error("should not run for web")

    events: list[str] = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        flow_foundation_p1=_AndroidOnlyBlocker(),
    )
    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.WEB))
    assert result.interaction.status == InteractionResultStatus.SUCCEEDED
    assert "execute" in events
