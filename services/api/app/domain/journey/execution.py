"""Journey 运行时的持久化身份、步骤尝试与状态机合同。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.journey.validation import (
    JourneyValidationError,
    require_text,
    stable_hash,
)
from services.api.app.domain.protocol import parse_aware_datetime

JOURNEY_EXECUTION_PROTOCOL_VERSION = "journey-execution.v1"
JOURNEY_STEP_ATTEMPT_PROTOCOL_VERSION = "journey-step-attempt.v1"


class JourneyExecutionStatus(StrEnum):
    """Journey 整体执行状态，与规则质量结论相互独立。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not self.RUNNING


class JourneyStepAttemptStatus(StrEnum):
    """单次步骤尝试的审计终态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, kw_only=True)
class JourneyExecutionSpec:
    """一次可恢复 Journey 执行的稳定输入快照。"""

    execution_id: str
    execution_plan_id: str
    task_id: str
    session_id: str
    agent_run_id: str
    surface_kind: SurfaceKind
    execution_epoch: int
    source_capture_id: str
    deadline: datetime
    protocol_version: str = JOURNEY_EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "execution_id",
            "execution_plan_id",
            "task_id",
            "session_id",
            "agent_run_id",
            "source_capture_id",
        ):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise JourneyValidationError("surface_kind 不受支持")
        if (
            isinstance(self.execution_epoch, bool)
            or not isinstance(self.execution_epoch, int)
            or self.execution_epoch < 0
        ):
            raise JourneyValidationError("execution_epoch 必须为非负整数")
        if not isinstance(self.deadline, datetime) or self.deadline.utcoffset() is None:
            raise JourneyValidationError("deadline 必须是带时区的 datetime")
        if self.protocol_version != JOURNEY_EXECUTION_PROTOCOL_VERSION:
            raise JourneyValidationError("JourneyExecutionSpec protocol_version 不受支持")
        expected = build_journey_execution_id(
            task_id=self.task_id,
            execution_plan_id=self.execution_plan_id,
            session_id=self.session_id,
            execution_epoch=self.execution_epoch,
        )
        if self.execution_id != expected:
            raise JourneyValidationError("execution_id 与执行输入身份不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "execution_id": self.execution_id,
            "execution_plan_id": self.execution_plan_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_run_id": self.agent_run_id,
            "surface_kind": self.surface_kind.value,
            "execution_epoch": self.execution_epoch,
            "source_capture_id": self.source_capture_id,
            "deadline": self.deadline.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JourneyExecutionSpec:
        """从数据库保存的不可变输入快照恢复执行身份。"""
        try:
            return cls(
                execution_id=payload["execution_id"],
                execution_plan_id=payload["execution_plan_id"],
                task_id=payload["task_id"],
                session_id=payload["session_id"],
                agent_run_id=payload["agent_run_id"],
                surface_kind=SurfaceKind(payload["surface_kind"]),
                execution_epoch=payload["execution_epoch"],
                source_capture_id=payload["source_capture_id"],
                deadline=parse_aware_datetime(payload["deadline"], "deadline"),
                protocol_version=payload["protocol_version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JourneyValidationError("JourneyExecutionSpec 载荷无效") from exc


@dataclass(frozen=True, kw_only=True)
class JourneyStepAttempt:
    """一次步骤尝试的不可变审计记录。"""

    attempt_id: str
    execution_id: str
    step_id: str
    step_index: int
    attempt_no: int
    status: JourneyStepAttemptStatus
    source_capture_id: str
    started_at: datetime
    finished_at: datetime
    result_capture_id: str | None = None
    action_execution_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    protocol_version: str = JOURNEY_STEP_ATTEMPT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("attempt_id", "execution_id", "step_id", "source_capture_id"):
            require_text(getattr(self, field_name), field_name)
        for field_name in ("step_index", "attempt_no"):
            value = getattr(self, field_name)
            minimum = 0 if field_name == "step_index" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise JourneyValidationError(f"{field_name} 不受支持")
        if not isinstance(self.status, JourneyStepAttemptStatus):
            raise JourneyValidationError("status 不受支持")
        for field_name in ("started_at", "finished_at"):
            value = getattr(self, field_name)
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise JourneyValidationError(f"{field_name} 必须是带时区的 datetime")
        if self.finished_at < self.started_at:
            raise JourneyValidationError("finished_at 不能早于 started_at")
        for field_name in (
            "result_capture_id",
            "action_execution_id",
            "error_code",
            "error_message",
        ):
            value = getattr(self, field_name)
            if value is not None:
                require_text(value, field_name)
        if self.status is JourneyStepAttemptStatus.SUCCEEDED:
            if self.error_code is not None or self.retryable:
                raise JourneyValidationError("成功步骤不能携带错误或重试标记")
        elif self.status is JourneyStepAttemptStatus.FAILED:
            require_text(self.error_code, "failed.error_code")
        elif self.retryable:
            raise JourneyValidationError("跳过步骤不能标记为重试")
        if self.protocol_version != JOURNEY_STEP_ATTEMPT_PROTOCOL_VERSION:
            raise JourneyValidationError("JourneyStepAttempt protocol_version 不受支持")
        expected = build_journey_step_attempt_id(
            execution_id=self.execution_id,
            step_id=self.step_id,
            attempt_no=self.attempt_no,
        )
        if self.attempt_id != expected:
            raise JourneyValidationError("attempt_id 与执行、步骤和次数不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "attempt_id": self.attempt_id,
            "execution_id": self.execution_id,
            "step_id": self.step_id,
            "step_index": self.step_index,
            "attempt_no": self.attempt_no,
            "status": self.status.value,
            "source_capture_id": self.source_capture_id,
            "result_capture_id": self.result_capture_id,
            "action_execution_id": self.action_execution_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


@dataclass(frozen=True, kw_only=True)
class JourneyExecutionState:
    """数据库中的可并发恢复 Journey 游标。"""

    spec: JourneyExecutionSpec
    status: JourneyExecutionStatus
    next_step_index: int
    current_step_attempt_count: int
    current_capture_id: str
    gap_count: int
    row_version: int
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, JourneyExecutionSpec):
            raise JourneyValidationError("spec 必须为 JourneyExecutionSpec")
        if not isinstance(self.status, JourneyExecutionStatus):
            raise JourneyValidationError("status 不受支持")
        require_text(self.current_capture_id, "current_capture_id")
        for field_name in (
            "next_step_index",
            "current_step_attempt_count",
            "gap_count",
            "row_version",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JourneyValidationError(f"{field_name} 必须为非负整数")
        for field_name in ("error_code", "error_message"):
            value = getattr(self, field_name)
            if value is not None:
                require_text(value, field_name)
        if self.status is JourneyExecutionStatus.FAILED and self.error_code is None:
            raise JourneyValidationError("失败执行必须携带 error_code")
        if self.status is not JourneyExecutionStatus.FAILED and self.error_code is not None:
            raise JourneyValidationError("非失败执行不能携带 error_code")


def build_journey_execution_id(
    *,
    task_id: str,
    execution_plan_id: str,
    session_id: str,
    execution_epoch: int,
) -> str:
    """同一任务、计划、Session 和 epoch 只对应一次 Journey 执行。"""
    identity = {
        "task_id": require_text(task_id, "task_id"),
        "execution_plan_id": require_text(execution_plan_id, "execution_plan_id"),
        "session_id": require_text(session_id, "session_id"),
        "execution_epoch": execution_epoch,
    }
    return f"journey-execution:{stable_hash(identity)}"


def build_journey_step_attempt_id(
    *,
    execution_id: str,
    step_id: str,
    attempt_no: int,
) -> str:
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no <= 0:
        raise JourneyValidationError("attempt_no 必须为正整数")
    identity = {
        "execution_id": require_text(execution_id, "execution_id"),
        "step_id": require_text(step_id, "step_id"),
        "attempt_no": attempt_no,
    }
    return f"journey-attempt:{stable_hash(identity)}"
