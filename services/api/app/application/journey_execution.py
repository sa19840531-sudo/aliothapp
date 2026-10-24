"""Journey 执行的应用层合同、持久化端口与确定性安全策略。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from services.api.app.domain.interaction import (
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.journey import (
    JourneyExecutionSpec,
    JourneyExecutionState,
    JourneyExecutionStatus,
    JourneyStep,
    JourneyStepAttempt,
    JourneyValidationError,
)
from services.api.app.domain.journey.validation import require_text


class JourneyExecutionPersistenceError(RuntimeError):
    """执行身份漂移、游标竞争或审计提交失败。"""


class JourneyStepExecutionError(RuntimeError):
    """步骤端口返回给 Runner 的可分类失败。"""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        retryable: bool,
        safe_to_continue: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_code = require_text(error_code, "error_code")
        if not isinstance(retryable, bool) or not isinstance(safe_to_continue, bool):
            raise TypeError("retryable 和 safe_to_continue 必须为布尔值")
        self.retryable = retryable
        self.safe_to_continue = safe_to_continue


@dataclass(frozen=True, kw_only=True)
class JourneyExecutionPolicy:
    """任务启动时锁定的动作风险、采集和审批策略。"""

    capture_profile: CaptureProfile
    allowed_operations: tuple[InteractionOperation, ...]
    allowed_risk_levels: tuple[InteractionRiskLevel, ...] = (InteractionRiskLevel.LOW,)
    risk_by_step: Mapping[str, InteractionRiskLevel] = field(default_factory=dict)
    purpose_by_step: Mapping[str, str] = field(default_factory=dict)
    approved_high_risk_step_ids: tuple[str, ...] = ()
    default_purpose: str = "执行已锁定 Journey 步骤"

    def __post_init__(self) -> None:
        if not isinstance(self.capture_profile, CaptureProfile):
            raise JourneyValidationError("capture_profile 类型不正确")
        _validate_unique_enum_tuple(
            self.allowed_operations,
            InteractionOperation,
            "allowed_operations",
        )
        _validate_unique_enum_tuple(
            self.allowed_risk_levels,
            InteractionRiskLevel,
            "allowed_risk_levels",
        )
        if not self.allowed_operations:
            raise JourneyValidationError("allowed_operations 不能为空")
        if not self.allowed_risk_levels:
            raise JourneyValidationError("allowed_risk_levels 不能为空")
        require_text(self.default_purpose, "default_purpose")
        risks: dict[str, InteractionRiskLevel] = {}
        for step_id, risk in self.risk_by_step.items():
            require_text(step_id, "risk_by_step.step_id")
            if not isinstance(risk, InteractionRiskLevel):
                raise JourneyValidationError("risk_by_step 包含不受支持的风险等级")
            risks[step_id] = risk
        purposes: dict[str, str] = {}
        for step_id, purpose in self.purpose_by_step.items():
            purposes[require_text(step_id, "purpose_by_step.step_id")] = require_text(
                purpose,
                "purpose_by_step.purpose",
            )
        if not isinstance(self.approved_high_risk_step_ids, tuple):
            raise JourneyValidationError("approved_high_risk_step_ids 必须为元组")
        approved = self.approved_high_risk_step_ids
        if len(approved) != len(set(approved)):
            raise JourneyValidationError("approved_high_risk_step_ids 不能重复")
        for step_id in approved:
            require_text(step_id, "approved_high_risk_step_ids")
        object.__setattr__(self, "risk_by_step", MappingProxyType(risks))
        object.__setattr__(self, "purpose_by_step", MappingProxyType(purposes))
        object.__setattr__(self, "approved_high_risk_step_ids", approved)

    def authorize(self, step: JourneyStep) -> tuple[InteractionRiskLevel, str]:
        """在进入 Driver 前确定性校验操作、风险等级和人工审批。"""
        operation = InteractionOperation(step.payload["operation"])
        if operation not in self.allowed_operations:
            raise JourneyStepExecutionError(
                error_code="JOURNEY_OPERATION_NOT_ALLOWED",
                message="Journey 步骤操作不在任务允许范围内",
                retryable=False,
            )
        risk = self.risk_by_step.get(step.step_id, InteractionRiskLevel.LOW)
        if risk not in self.allowed_risk_levels:
            raise JourneyStepExecutionError(
                error_code="JOURNEY_RISK_NOT_ALLOWED",
                message="Journey 步骤风险等级不在任务允许范围内",
                retryable=False,
            )
        if (
            risk is InteractionRiskLevel.HIGH
            and step.step_id not in self.approved_high_risk_step_ids
        ):
            raise JourneyStepExecutionError(
                error_code="JOURNEY_HIGH_RISK_APPROVAL_REQUIRED",
                message="高风险 Journey 步骤缺少显式审批",
                retryable=False,
            )
        return risk, self.purpose_by_step.get(step.step_id, self.default_purpose)


@dataclass(frozen=True, kw_only=True)
class JourneyStepExecutionRequest:
    """步骤适配器只接收领域数据，不接触 Driver 或页面句柄。"""

    spec: JourneyExecutionSpec
    step: JourneyStep
    step_index: int
    attempt_no: int
    source_capture_id: str
    deadline: datetime
    policy: JourneyExecutionPolicy


@dataclass(frozen=True, kw_only=True)
class JourneyStepExecutionResult:
    """步骤成功后的最小审计引用。"""

    result_capture_id: str | None = None
    action_execution_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("result_capture_id", "action_execution_id"):
            value = getattr(self, field_name)
            if value is not None:
                require_text(value, field_name)


class JourneyStepHandler(Protocol):
    """一种 Journey 步骤的执行端口。"""

    async def execute(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult: ...


class JourneyConditionEvaluator(Protocol):
    async def wait(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult: ...


class JourneyCaptureExecutor(Protocol):
    async def capture(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult: ...


class JourneyAssertionEvaluator(Protocol):
    async def assert_condition(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult: ...


class JourneyExecutionRepository(Protocol):
    """Journey Runner 的持久化游标端口。"""

    async def start(self, *, spec: JourneyExecutionSpec) -> JourneyExecutionState: ...

    async def commit_step(
        self,
        *,
        command: CommitJourneyStepCommand,
    ) -> JourneyExecutionState: ...


@dataclass(frozen=True, kw_only=True)
class CommitJourneyStepCommand:
    """把步骤审计记录和执行游标作为一个原子提交。"""

    execution_id: str
    expected_row_version: int
    expected_step_index: int
    expected_attempt_count: int
    attempt: JourneyStepAttempt
    target_status: JourneyExecutionStatus
    next_step_index: int
    next_step_attempt_count: int
    current_capture_id: str
    gap_count: int
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        require_text(self.execution_id, "execution_id")
        if self.attempt.execution_id != self.execution_id:
            raise JourneyValidationError("步骤尝试与 Journey 执行身份不一致")
        for field_name in (
            "expected_row_version",
            "expected_step_index",
            "expected_attempt_count",
            "next_step_index",
            "next_step_attempt_count",
            "gap_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JourneyValidationError(f"{field_name} 必须为非负整数")
        require_text(self.current_capture_id, "current_capture_id")
        if self.target_status is JourneyExecutionStatus.FAILED:
            require_text(self.error_code, "error_code")
        elif self.error_code is not None or self.error_message is not None:
            raise JourneyValidationError("非失败执行不能携带最终错误")


def _validate_unique_enum_tuple(
    values: tuple[object, ...],
    enum_type: type,
    field_name: str,
) -> None:
    if not isinstance(values, tuple) or any(not isinstance(item, enum_type) for item in values):
        raise JourneyValidationError(f"{field_name} 类型不正确")
    if len(values) != len(set(values)):
        raise JourneyValidationError(f"{field_name} 不能重复")
