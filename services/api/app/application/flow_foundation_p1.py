"""流程基建 P1 应用接线：采集预约／承诺、执行许可、租约承接守卫。

领域不变量见 ``domain.exploration.flow_foundation``。本模块提供可注入的
应用服务与内存预约仓；PostgreSQL 落库可替换 Store 实现，不改守卫语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol

from services.api.app.application.device_pool import DeviceLease
from services.api.app.domain.exploration.flow_foundation import (
    APP_FLOW_SINGLE_ACTION_ENTRY,
    ArtifactCommitment,
    CaptureOwnershipBinding,
    CaptureReservation,
    CaptureReservationStatus,
    DeviceExecutionPermit,
    DeviceLeaseHandoff,
    FlowFoundationContractError,
    assert_capture_binding_matches,
    assert_execution_permit_active,
    assert_fencing_token_matches,
    assert_formal_evidence_has_commitment,
    assert_late_upload_stays_with_owner,
    assert_lease_handoff_safe,
    assert_pure_observation_has_no_action_id,
    assert_single_app_action_entry,
    assert_single_device_executor,
    build_capture_request_identity,
    decide_unknown_action_replay,
)
from services.api.app.domain.interaction.enums import InteractionOperation, SurfaceKind


class FlowFoundationP1Error(RuntimeError):
    """P1 接线层可路由错误。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "FLOW_FOUNDATION_P1_REJECTED",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class CaptureReservationStore(Protocol):
    """采集预约与内容承诺持久端口。"""

    def put_reservation(self, reservation: CaptureReservation) -> None: ...

    def get_reservation(self, capture_request_id: str) -> CaptureReservation | None: ...

    def put_commitment(self, commitment: ArtifactCommitment) -> None: ...

    def get_commitment(
        self,
        *,
        capture_request_id: str,
        artifact_slot: str,
    ) -> ArtifactCommitment | None: ...


@dataclass
class InMemoryCaptureReservationStore:
    """进程内预约／承诺仓；联调与单测默认实现。"""

    _reservations: dict[str, CaptureReservation] = field(default_factory=dict)
    _commitments: dict[tuple[str, str], ArtifactCommitment] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def put_reservation(self, reservation: CaptureReservation) -> None:
        with self._lock:
            existing = self._reservations.get(reservation.capture_request_id)
            if existing is not None and existing != reservation:
                raise FlowFoundationP1Error(
                    "capture_request_id 已存在且内容不一致",
                    error_code="CAPTURE_RESERVATION_CONFLICT",
                )
            self._reservations[reservation.capture_request_id] = reservation

    def get_reservation(self, capture_request_id: str) -> CaptureReservation | None:
        with self._lock:
            return self._reservations.get(capture_request_id)

    def put_commitment(self, commitment: ArtifactCommitment) -> None:
        key = (commitment.capture_request_id, commitment.artifact_slot)
        with self._lock:
            existing = self._commitments.get(key)
            if existing is not None and existing != commitment:
                raise FlowFoundationP1Error(
                    "同一预约槽位内容承诺冲突",
                    error_code="ARTIFACT_COMMITMENT_CONFLICT",
                )
            self._commitments[key] = commitment

    def get_commitment(
        self,
        *,
        capture_request_id: str,
        artifact_slot: str,
    ) -> ArtifactCommitment | None:
        with self._lock:
            return self._commitments.get((capture_request_id, artifact_slot))


class FlowFoundationP1Service:
    """App 组合根可注入的 P1 守卫与预约服务。"""

    def __init__(self, *, store: CaptureReservationStore | None = None) -> None:
        self._store: CaptureReservationStore = store or InMemoryCaptureReservationStore()

    def gate_app_action_entry(self) -> None:
        try:
            assert_single_app_action_entry(APP_FLOW_SINGLE_ACTION_ENTRY)
        except FlowFoundationContractError as exc:
            raise FlowFoundationP1Error(str(exc), error_code="APP_ACTION_ENTRY_VIOLATION") from exc

    def gate_android_coordination(self, *, surface_kind: SurfaceKind) -> None:
        if surface_kind is SurfaceKind.ANDROID:
            self.gate_app_action_entry()

    def permit_from_device_lease(
        self,
        lease: DeviceLease,
        *,
        execution_segment_id: str,
        revoked: bool = False,
    ) -> DeviceExecutionPermit:
        state = str(lease.state or "").casefold()
        effective_revoked = revoked or state in {"released", "quarantined"}
        return DeviceExecutionPermit(
            device_id=str(lease.device_ref),
            resource_id=str(lease.device_ref),
            fencing_token=str(int(lease.fencing_token)),
            session_id=str(lease.session_id),
            execution_segment_id=str(execution_segment_id),
            task_id=str(lease.task_id),
            revoked=effective_revoked,
        )

    def require_permit(
        self,
        permit: DeviceExecutionPermit,
        *,
        presented_fencing_token: str | int | None = None,
    ) -> None:
        try:
            assert_execution_permit_active(permit)
            if presented_fencing_token is not None:
                assert_fencing_token_matches(
                    permit,
                    presented_fencing_token=str(presented_fencing_token),
                )
        except FlowFoundationContractError as exc:
            raise FlowFoundationP1Error(
                str(exc),
                error_code="DEVICE_EXECUTION_PERMIT_REJECTED",
            ) from exc

    def require_single_device_executor(
        self,
        *,
        device_id: str,
        active_session_id: str,
        claimant_session_id: str,
    ) -> None:
        try:
            assert_single_device_executor(
                device_id=device_id,
                active_session_id=active_session_id,
                claimant_session_id=claimant_session_id,
            )
        except FlowFoundationContractError as exc:
            raise FlowFoundationP1Error(
                str(exc),
                error_code="DEVICE_EXECUTOR_CONFLICT",
            ) from exc

    def require_lease_handoff(self, handoff: DeviceLeaseHandoff) -> None:
        try:
            assert_lease_handoff_safe(handoff)
        except FlowFoundationContractError as exc:
            raise FlowFoundationP1Error(
                str(exc),
                error_code="DEVICE_LEASE_HANDOFF_REJECTED",
            ) from exc

    def build_lease_handoff(
        self,
        *,
        predecessor: DeviceLease,
        successor_session_id: str,
        successor_fencing_token: int,
        predecessor_channel_silenced: bool,
    ) -> DeviceLeaseHandoff:
        handoff = DeviceLeaseHandoff(
            device_id=str(predecessor.device_ref),
            task_id=str(predecessor.task_id),
            predecessor_session_id=str(predecessor.session_id),
            successor_session_id=str(successor_session_id),
            predecessor_fencing_token=str(int(predecessor.fencing_token)),
            successor_fencing_token=str(int(successor_fencing_token)),
            predecessor_channel_silenced=predecessor_channel_silenced,
        )
        self.require_lease_handoff(handoff)
        return handoff

    def reserve_capture(
        self,
        *,
        task_id: str,
        phase_run_id: str,
        checkpoint_key: str,
        sample_index: int,
        producer_epoch: str,
        capture_id: str,
        session_id: str | None = None,
        action_execution_id: str | None = None,
        execution_segment_id: str | None = None,
        artifact_slots: tuple[str, ...] = ("screenshot",),
        pure_observation: bool = False,
    ) -> CaptureReservation:
        if pure_observation:
            try:
                assert_pure_observation_has_no_action_id(action_execution_id)
            except FlowFoundationContractError as exc:
                raise FlowFoundationP1Error(
                    str(exc),
                    error_code="PURE_OBSERVATION_ACTION_FORBIDDEN",
                ) from exc
        request_id = build_capture_request_identity(
            task_id=task_id,
            phase_run_id=phase_run_id,
            checkpoint_key=checkpoint_key,
            sample_index=sample_index,
            producer_epoch=producer_epoch,
        )
        ownership = CaptureOwnershipBinding(
            task_id=task_id,
            capture_id=capture_id,
            owner_phase_run_id=phase_run_id,
            owner_execution_segment_id=execution_segment_id,
            action_execution_id=action_execution_id,
            session_id=session_id,
        )
        reservation = CaptureReservation(
            capture_request_id=request_id,
            capture_id=capture_id,
            task_id=task_id,
            status=CaptureReservationStatus.RESERVED,
            ownership=ownership,
            artifact_slots=artifact_slots,
        )
        self._store.put_reservation(reservation)
        return reservation

    def commit_artifact(
        self,
        *,
        reservation: CaptureReservation,
        artifact_slot: str,
        content_digest: str,
        content_length: int,
        fencing_token: str | int,
    ) -> ArtifactCommitment:
        if artifact_slot not in reservation.artifact_slots and reservation.artifact_slots:
            raise FlowFoundationP1Error(
                f"制品槽位不在预约内: {artifact_slot}",
                error_code="ARTIFACT_SLOT_NOT_RESERVED",
            )
        commitment = ArtifactCommitment(
            capture_request_id=reservation.capture_request_id,
            capture_id=reservation.capture_id,
            artifact_slot=artifact_slot,
            content_digest=content_digest,
            content_length=content_length,
            produced_under_fencing_token=str(fencing_token),
        )
        self._store.put_commitment(commitment)
        return commitment

    def require_formal_evidence(
        self,
        *,
        capture_request_id: str,
        artifact_slot: str = "screenshot",
    ) -> tuple[CaptureReservation, ArtifactCommitment]:
        reservation = self._store.get_reservation(capture_request_id)
        if reservation is None:
            raise FlowFoundationP1Error(
                "采集预约不存在",
                error_code="CAPTURE_RESERVATION_MISSING",
            )
        commitment = self._store.get_commitment(
            capture_request_id=capture_request_id,
            artifact_slot=artifact_slot,
        )
        try:
            assert_formal_evidence_has_commitment(
                reservation=reservation,
                commitment=commitment,
            )
        except FlowFoundationContractError as exc:
            raise FlowFoundationP1Error(
                str(exc),
                error_code="FORMAL_EVIDENCE_COMMITMENT_REQUIRED",
            ) from exc
        assert commitment is not None
        return reservation, commitment

    def require_binding(
        self,
        expected: CaptureOwnershipBinding,
        **presented: object,
    ) -> None:
        try:
            assert_capture_binding_matches(expected, **presented)  # type: ignore[arg-type]
        except FlowFoundationContractError as exc:
            raise FlowFoundationP1Error(
                str(exc),
                error_code="CAPTURE_BINDING_MISMATCH",
            ) from exc

    def require_late_upload_owner(
        self,
        *,
        material_owner_task_id: str,
        receiving_runtime_task_id: str,
    ) -> None:
        try:
            assert_late_upload_stays_with_owner(
                material_owner_task_id=material_owner_task_id,
                receiving_runtime_task_id=receiving_runtime_task_id,
            )
        except FlowFoundationContractError as exc:
            raise FlowFoundationP1Error(
                str(exc),
                error_code="LATE_UPLOAD_OWNER_MISMATCH",
            ) from exc

    def decide_replay_after_unknown(
        self,
        *,
        outcome_unknown: bool,
        proved_not_occurred: bool,
    ) -> str:
        decision = decide_unknown_action_replay(
            outcome_unknown=outcome_unknown,
            proved_not_occurred=proved_not_occurred,
        )
        if decision == "forbid_replay":
            raise FlowFoundationP1Error(
                "动作结果未知且无法证明未发生，禁止重放",
                error_code="UNKNOWN_ACTION_REPLAY_FORBIDDEN",
            )
        return decision

    def gate_interaction_operation(
        self,
        *,
        operation: InteractionOperation,
        action_execution_id: str | None,
    ) -> None:
        if operation in {
            InteractionOperation.OBSERVE,
            InteractionOperation.TIMED_CAPTURE,
        }:
            try:
                assert_pure_observation_has_no_action_id(action_execution_id)
            except FlowFoundationContractError as exc:
                raise FlowFoundationP1Error(
                    str(exc),
                    error_code="PURE_OBSERVATION_ACTION_FORBIDDEN",
                ) from exc
