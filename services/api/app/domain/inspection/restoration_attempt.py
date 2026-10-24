"""恢复尝试的审计状态机。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.inspection.restoration_plan import (
    RestorationPlan,
    RestorationValidationError,
)
from services.api.app.domain.inspection.state_equivalence import (
    StateEquivalenceAssessment,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_mapping,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

RESTORATION_ATTEMPT_PROTOCOL_VERSION = "restoration-attempt.v1"


class RestorationAttemptStatus(StrEnum):
    RUNNING = "running"
    EQUIVALENT = "equivalent"
    DIVERGENT = "divergent"
    UNFULFILLABLE = "unfulfillable"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self is not self.RUNNING


@dataclass(frozen=True, kw_only=True)
class RestorationAttempt:
    """一次恢复和等价门禁的不可变审计投影。"""

    attempt_id: str
    plan: RestorationPlan
    attempt_no: int
    status: RestorationAttemptStatus
    started_at: datetime
    finished_at: datetime | None = None
    restored_capture_id: str | None = None
    assessment: StateEquivalenceAssessment | None = None
    action_execution_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    row_version: int = 0
    protocol_version: str = RESTORATION_ATTEMPT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_text(self.attempt_id, "attempt_id")
        if not isinstance(self.plan, RestorationPlan):
            raise RestorationValidationError("plan 必须为 RestorationPlan")
        for field_name, minimum in (("attempt_no", 1), ("row_version", 0)):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise RestorationValidationError(f"{field_name} 不受支持")
        if not isinstance(self.status, RestorationAttemptStatus):
            raise RestorationValidationError("status 不受支持")
        _require_aware_datetime(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_aware_datetime(self.finished_at, "finished_at")
            if self.finished_at < self.started_at:
                raise RestorationValidationError("finished_at 不能早于 started_at")
        for field_name in ("restored_capture_id", "error_code", "error_message"):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        action_execution_ids = _unique_text_tuple(
            self.action_execution_ids,
            "action_execution_ids",
        )
        expected_id = build_restoration_attempt_id(
            plan_id=self.plan.plan_id,
            attempt_no=self.attempt_no,
        )
        if self.attempt_id != expected_id:
            raise RestorationValidationError("attempt_id 与计划和次数不一致")
        if self.status is RestorationAttemptStatus.RUNNING:
            if any(
                value is not None
                for value in (
                    self.finished_at,
                    self.restored_capture_id,
                    self.assessment,
                    self.error_code,
                    self.error_message,
                )
            ):
                raise RestorationValidationError("运行中恢复尝试不能提前携带终态字段")
        elif self.finished_at is None:
            raise RestorationValidationError("终态恢复尝试必须携带 finished_at")
        if self.status in {
            RestorationAttemptStatus.EQUIVALENT,
            RestorationAttemptStatus.DIVERGENT,
        }:
            if self.restored_capture_id is None or self.assessment is None:
                raise RestorationValidationError("等价终态必须携带 Capture 与评估")
            if self.assessment.restoration_attempt_id != self.attempt_id:
                raise RestorationValidationError("评估与恢复尝试身份不一致")
            if self.assessment.restored_capture_id != self.restored_capture_id:
                raise RestorationValidationError("评估与恢复 Capture 不一致")
            if self.assessment.request_id != self.plan.request_id:
                raise RestorationValidationError("评估与恢复计划请求不一致")
            if self.assessment.task_id != self.plan.task_id:
                raise RestorationValidationError("评估与恢复计划任务不一致")
            if self.assessment.original_capture_id != self.plan.original_capture_id:
                raise RestorationValidationError("评估与恢复计划原 Capture 不一致")
            if self.assessment.source_grade is not self.plan.source_grade:
                raise RestorationValidationError("评估来源等级与恢复策略不一致")
            expected_pass = self.status is RestorationAttemptStatus.EQUIVALENT
            if self.assessment.passed != expected_pass:
                raise RestorationValidationError("恢复终态与状态等价结论不一致")
            if self.error_code is not None or self.error_message is not None:
                raise RestorationValidationError("等价终态不能携带错误信息")
        elif self.assessment is not None:
            raise RestorationValidationError("非等价终态不能携带状态评估")
        if (
            self.status
            in {
                RestorationAttemptStatus.UNFULFILLABLE,
                RestorationAttemptStatus.FAILED,
            }
            and self.error_code is None
        ):
            raise RestorationValidationError("失败或不可补采终态必须携带 error_code")
        if self.error_message is not None and self.error_code is None:
            raise RestorationValidationError("error_message 必须与 error_code 同时出现")
        if self.protocol_version != RESTORATION_ATTEMPT_PROTOCOL_VERSION:
            raise RestorationValidationError("RestorationAttempt 版本不受支持")
        object.__setattr__(self, "action_execution_ids", action_execution_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "attempt_id": self.attempt_id,
            "plan": self.plan.to_dict(),
            "attempt_no": self.attempt_no,
            "status": self.status.value,
            "restored_capture_id": self.restored_capture_id,
            "assessment": (self.assessment.to_dict() if self.assessment is not None else None),
            "action_execution_ids": list(self.action_execution_ids),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat(),
            "finished_at": (self.finished_at.isoformat() if self.finished_at is not None else None),
            "state_version": self.row_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RestorationAttempt:
        known = {
            "protocol_version",
            "attempt_id",
            "plan",
            "attempt_no",
            "status",
            "restored_capture_id",
            "assessment",
            "action_execution_ids",
            "error_code",
            "error_message",
            "started_at",
            "finished_at",
            "state_version",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="RestorationAttempt",
            )
            assessment_payload = values["assessment"]
            return cls(
                attempt_id=values["attempt_id"],
                plan=RestorationPlan.from_dict(require_mapping(values["plan"], "plan")),
                attempt_no=values["attempt_no"],
                status=RestorationAttemptStatus(values["status"]),
                restored_capture_id=values["restored_capture_id"],
                assessment=(
                    StateEquivalenceAssessment.from_dict(
                        require_mapping(assessment_payload, "assessment")
                    )
                    if assessment_payload is not None
                    else None
                ),
                action_execution_ids=tuple(
                    require_sequence(
                        values["action_execution_ids"],
                        "action_execution_ids",
                    )
                ),
                error_code=values["error_code"],
                error_message=values["error_message"],
                started_at=parse_aware_datetime(values["started_at"], "started_at"),
                finished_at=(
                    parse_aware_datetime(values["finished_at"], "finished_at")
                    if values["finished_at"] is not None
                    else None
                ),
                row_version=values["state_version"],
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise RestorationValidationError(str(exc)) from exc


def build_restoration_attempt_id(*, plan_id: str, attempt_no: int) -> str:
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no <= 0:
        raise RestorationValidationError("attempt_no 必须为正整数")
    return "restoration-attempt:" + stable_protocol_hash(
        {"plan_id": _require_text(plan_id, "plan_id"), "attempt_no": attempt_no}
    )


def _require_text(value: object, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise RestorationValidationError(str(exc)) from exc


def _require_aware_datetime(value: object, field_name: str) -> None:
    try:
        require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise RestorationValidationError(str(exc)) from exc


def _unique_text_tuple(values: Sequence[object], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise RestorationValidationError(f"{field_name} 不能重复")
    return normalized
