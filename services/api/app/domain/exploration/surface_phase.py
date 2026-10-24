"""Surface 阶段运行与步骤持久化合同。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    require_aware_datetime,
    require_mapping,
    require_text,
    stable_protocol_hash,
    thaw_json_mapping,
)

SURFACE_PHASE_RUN_PROTOCOL_VERSION = "surface-phase-run.v1"
SURFACE_PHASE_STEP_PROTOCOL_VERSION = "surface-phase-step.v1"


class SurfacePhaseValidationError(ValueError):
    """Surface 阶段运行或步骤不满足稳定持久化合同。"""


class SurfacePhaseRunStatus(StrEnum):
    """Surface 阶段运行状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SurfacePhaseRunStatus.COMPLETED,
            SurfacePhaseRunStatus.BLOCKED,
            SurfacePhaseRunStatus.FAILED,
            SurfacePhaseRunStatus.CANCELED,
        }


class SurfacePhaseStepStatus(StrEnum):
    """Surface 阶段步骤状态。"""

    DECIDED = "decided"
    ACTION_PLANNED = "action_planned"
    ACTION_VERIFIED = "action_verified"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SurfacePhaseStepStatus.COMPLETED,
            SurfacePhaseStepStatus.BLOCKED,
            SurfacePhaseStepStatus.FAILED,
        }


@dataclass(frozen=True, kw_only=True)
class SurfacePhaseRunSnapshot:
    """一次阶段运行的可恢复持久化投影。"""

    phase_run_id: str
    task_id: str
    session_id: str
    surface_kind: SurfaceKind
    phase_key: str
    phase_protocol_version: str
    idempotency_key: str
    source_capture_id: str
    status: SurfacePhaseRunStatus
    command_sequence: int
    finalization_epoch: int
    row_version: int
    context_payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    step_count: int = 0
    terminal_reason_code: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    protocol_version: str = SURFACE_PHASE_RUN_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "phase_run_id",
            "task_id",
            "session_id",
            "phase_key",
            "phase_protocol_version",
            "idempotency_key",
            "source_capture_id",
        ):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise SurfacePhaseValidationError("surface_kind 不受支持")
        if not isinstance(self.status, SurfacePhaseRunStatus):
            raise SurfacePhaseValidationError("status 不受支持")
        for field_name in ("command_sequence", "finalization_epoch", "row_version", "step_count"):
            _require_non_negative_int(getattr(self, field_name), field_name)
        if self.command_sequence <= 0:
            raise SurfacePhaseValidationError("command_sequence 必须为正整数")
        if self.protocol_version != SURFACE_PHASE_RUN_PROTOCOL_VERSION:
            raise SurfacePhaseValidationError("SurfacePhaseRunSnapshot 版本不受支持")
        context_payload = freeze_json_mapping(self.context_payload, "context_payload")
        object.__setattr__(self, "context_payload", context_payload)
        object.__setattr__(
            self,
            "evidence_artifact_ids",
            _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids"),
        )
        if self.status.is_terminal:
            require_text(self.terminal_reason_code, "terminal_reason_code")
            if self.finished_at is None:
                raise SurfacePhaseValidationError("终态阶段必须记录 finished_at")
        elif self.terminal_reason_code is not None or self.finished_at is not None:
            raise SurfacePhaseValidationError("非终态阶段不能携带终态字段")
        for field_name in ("started_at", "finished_at"):
            value = getattr(self, field_name)
            if value is not None:
                try:
                    require_aware_datetime(value, field_name)
                except ProtocolContractError as exc:
                    raise SurfacePhaseValidationError(str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "phase_run_id": self.phase_run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "surface_kind": self.surface_kind.value,
            "phase_key": self.phase_key,
            "phase_protocol_version": self.phase_protocol_version,
            "idempotency_key": self.idempotency_key,
            "source_capture_id": self.source_capture_id,
            "status": self.status.value,
            "command_sequence": self.command_sequence,
            "finalization_epoch": self.finalization_epoch,
            "row_version": self.row_version,
            "context_payload": thaw_json_mapping(self.context_payload),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "step_count": self.step_count,
            "terminal_reason_code": self.terminal_reason_code,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


@dataclass(frozen=True, kw_only=True)
class SurfacePhaseStepSnapshot:
    """阶段机一次判定或动作建议的不可变审计投影。"""

    phase_step_id: str
    phase_run_id: str
    task_id: str
    session_id: str
    step_key: str
    step_index: int
    status: SurfacePhaseStepStatus
    decision_status: str
    reason_code: str
    source_capture_id: str
    decision_payload: Mapping[str, Any]
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    next_action_payload: Mapping[str, Any] | None = None
    action_execution_id: str | None = None
    result_capture_id: str | None = None
    created_at: datetime | None = None
    protocol_version: str = SURFACE_PHASE_STEP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "phase_step_id",
            "phase_run_id",
            "task_id",
            "session_id",
            "step_key",
            "decision_status",
            "reason_code",
            "source_capture_id",
        ):
            require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(self.step_index, "step_index")
        if self.step_index <= 0:
            raise SurfacePhaseValidationError("step_index 必须为正整数")
        if not isinstance(self.status, SurfacePhaseStepStatus):
            raise SurfacePhaseValidationError("status 不受支持")
        if self.protocol_version != SURFACE_PHASE_STEP_PROTOCOL_VERSION:
            raise SurfacePhaseValidationError("SurfacePhaseStepSnapshot 版本不受支持")
        payload = freeze_json_mapping(self.decision_payload, "decision_payload")
        object.__setattr__(self, "decision_payload", payload)
        if self.next_action_payload is not None:
            action_payload = freeze_json_mapping(self.next_action_payload, "next_action_payload")
            object.__setattr__(self, "next_action_payload", action_payload)
        for field_name in ("action_execution_id", "result_capture_id"):
            value = getattr(self, field_name)
            if value is not None:
                require_text(value, field_name)
        object.__setattr__(
            self,
            "evidence_artifact_ids",
            _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids"),
        )
        if self.created_at is not None:
            try:
                require_aware_datetime(self.created_at, "created_at")
            except ProtocolContractError as exc:
                raise SurfacePhaseValidationError(str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "phase_step_id": self.phase_step_id,
            "phase_run_id": self.phase_run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "step_key": self.step_key,
            "step_index": self.step_index,
            "status": self.status.value,
            "decision_status": self.decision_status,
            "reason_code": self.reason_code,
            "source_capture_id": self.source_capture_id,
            "decision_payload": thaw_json_mapping(self.decision_payload),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "next_action_payload": (
                thaw_json_mapping(self.next_action_payload)
                if self.next_action_payload is not None
                else None
            ),
            "action_execution_id": self.action_execution_id,
            "result_capture_id": self.result_capture_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def build_surface_phase_run_id(
    *,
    task_id: str,
    session_id: str,
    phase_key: str,
    idempotency_key: str,
) -> str:
    """根据任务、会话、阶段和幂等键生成稳定阶段运行 ID。"""

    return "surface_phase_run:" + stable_protocol_hash(
        {
            "task_id": require_text(task_id, "task_id"),
            "session_id": require_text(session_id, "session_id"),
            "phase_key": require_text(phase_key, "phase_key"),
            "idempotency_key": require_text(idempotency_key, "idempotency_key"),
        }
    )


def build_surface_phase_step_id(
    *,
    phase_run_id: str,
    step_key: str,
) -> str:
    """根据阶段运行和步骤幂等键生成稳定阶段步骤 ID。"""

    return "surface_phase_step:" + stable_protocol_hash(
        {
            "phase_run_id": require_text(phase_run_id, "phase_run_id"),
            "step_key": require_text(step_key, "step_key"),
        }
    )


def build_surface_phase_payload_hash(payload: Mapping[str, Any]) -> str:
    """生成阶段上下文或决策载荷的稳定哈希。"""

    return stable_protocol_hash(require_mapping(payload, "payload"))


def _unique_text_tuple(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise SurfacePhaseValidationError(f"{field_name} 必须为数组")
    result = tuple(require_text(value, field_name) for value in values)
    if len(set(result)) != len(result):
        raise SurfacePhaseValidationError(f"{field_name} 不能包含重复值")
    return result


def _require_non_negative_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SurfacePhaseValidationError(f"{field_name} 必须为非负整数")
