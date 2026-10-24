"""Interaction Runtime 到 Explorer/Orchestrator 的执行结果合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from services.api.app.domain.interaction.enums import (
    InteractionExecutionOutcome,
    InteractionResultStatus,
    InteractionVerificationOutcome,
)
from services.api.app.domain.interaction.validation import (
    RUNTIME_HANDLE_KEYS,
    InteractionValidationError,
    freeze_mapping,
    optional_text,
    require_text,
    thaw_mapping,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    read_known_fields,
    require_mapping,
)

INTERACTION_RESULT_PROTOCOL_VERSION = "interaction-result.v1"


@dataclass(frozen=True, kw_only=True)
class InteractionResult:
    """动作执行与证据提交状态，不包含 Driver 返回对象。"""

    task_id: str
    session_id: str
    intent_id: str
    action_execution_id: str
    source_capture_id: str
    status: InteractionResultStatus
    execution_outcome: InteractionExecutionOutcome | None = None
    verification_outcome: InteractionVerificationOutcome | None = None
    result_capture_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    state_summary: Mapping[str, Any] = field(default_factory=dict)
    state_diff: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = INTERACTION_RESULT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "session_id",
            "intent_id",
            "action_execution_id",
            "source_capture_id",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.protocol_version != INTERACTION_RESULT_PROTOCOL_VERSION:
            raise InteractionValidationError("InteractionResult protocol_version 不受支持")
        if not isinstance(self.status, InteractionResultStatus):
            raise InteractionValidationError("status 不受支持")
        execution_outcome = self.execution_outcome or _execution_outcome_for_status(
            self.status
        )
        verification_outcome = (
            self.verification_outcome or _verification_outcome_for_status(self.status)
        )
        if not isinstance(execution_outcome, InteractionExecutionOutcome):
            raise InteractionValidationError("execution_outcome 不受支持")
        if not isinstance(verification_outcome, InteractionVerificationOutcome):
            raise InteractionValidationError("verification_outcome 不受支持")
        if execution_outcome is not _execution_outcome_for_status(self.status):
            raise InteractionValidationError("execution_outcome 与 status 执行事实不一致")
        if (
            self.status is InteractionResultStatus.EVIDENCE_PENDING
            and verification_outcome is not InteractionVerificationOutcome.PENDING
        ):
            raise InteractionValidationError("待证据结果的 verification_outcome 必须为 pending")
        if (
            self.status
            in {
                InteractionResultStatus.FAILED,
                InteractionResultStatus.BLOCKED,
                InteractionResultStatus.TIMED_OUT,
                InteractionResultStatus.EVIDENCE_FAILED,
            }
            and verification_outcome is not InteractionVerificationOutcome.UNVERIFIED
        ):
            raise InteractionValidationError("失败结果的 verification_outcome 必须为 unverified")
        object.__setattr__(self, "execution_outcome", execution_outcome)
        object.__setattr__(self, "verification_outcome", verification_outcome)
        optional_text(self.result_capture_id, "result_capture_id")
        optional_text(self.error_code, "error_code")
        optional_text(self.error_message, "error_message")
        if not isinstance(self.retryable, bool):
            raise InteractionValidationError("retryable 必须为布尔值")
        if self.status == InteractionResultStatus.SUCCEEDED:
            require_text(self.result_capture_id, "succeeded.result_capture_id")
            if self.error_code is not None or self.retryable:
                raise InteractionValidationError("成功结果不能携带错误或重试标记")
        elif self.status == InteractionResultStatus.EVIDENCE_PENDING:
            if self.result_capture_id is not None or self.error_code is not None:
                raise InteractionValidationError("待证据结果不能提前携带 Capture 或错误")
            if self.retryable:
                raise InteractionValidationError("待证据结果不能标记为重试")
        else:
            require_text(self.error_code, f"{self.status.value}.error_code")
        object.__setattr__(
            self,
            "state_summary",
            freeze_mapping(
                self.state_summary,
                "state_summary",
                forbidden_keys=RUNTIME_HANDLE_KEYS,
            ),
        )
        object.__setattr__(
            self,
            "state_diff",
            freeze_mapping(
                self.state_diff,
                "state_diff",
                forbidden_keys=RUNTIME_HANDLE_KEYS,
            ),
        )

    def complete_with_capture(self, page_capture_id: str) -> InteractionResult:
        """后 Capture 提交成功后，把待证据状态推进为成功。"""
        if self.status != InteractionResultStatus.EVIDENCE_PENDING:
            raise InteractionValidationError("只有 evidence_pending 可以完成证据提交")
        return replace(
            self,
            status=InteractionResultStatus.SUCCEEDED,
            result_capture_id=require_text(page_capture_id, "result_capture_id"),
        )

    def mark_evidence_failed(
        self,
        *,
        error_code: str,
        error_message: str,
        retryable: bool = True,
    ) -> InteractionResult:
        """动作完成但后 Capture 失败时形成独立证据失败状态。"""
        if self.status != InteractionResultStatus.EVIDENCE_PENDING:
            raise InteractionValidationError("只有 evidence_pending 可以转为证据失败")
        return replace(
            self,
            status=InteractionResultStatus.EVIDENCE_FAILED,
            verification_outcome=InteractionVerificationOutcome.UNVERIFIED,
            error_code=require_text(error_code, "error_code"),
            error_message=require_text(error_message, "error_message"),
            retryable=retryable,
        )

    def attach_capture_preserving_execution(
        self,
        page_capture_id: str,
    ) -> InteractionResult:
        """为失败、拒绝或超时动作附加终态证据，不改写执行真值。"""
        if self.status not in {
            InteractionResultStatus.FAILED,
            InteractionResultStatus.BLOCKED,
            InteractionResultStatus.TIMED_OUT,
        }:
            raise InteractionValidationError("只有动作失败终态可以保留执行状态并附加证据")
        if self.result_capture_id is not None:
            raise InteractionValidationError("动作失败终态已经绑定结果 Capture")
        return replace(
            self,
            result_capture_id=require_text(page_capture_id, "result_capture_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "action_execution_id": self.action_execution_id,
            "source_capture_id": self.source_capture_id,
            "status": self.status.value,
            "execution_outcome": self.execution_outcome.value,
            "verification_outcome": self.verification_outcome.value,
            "result_capture_id": self.result_capture_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "state_summary": thaw_mapping(self.state_summary),
            "state_diff": thaw_mapping(self.state_diff),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InteractionResult:
        """从持久化消息恢复结果并容忍未来版本新增字段。"""
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(
                    {
                        "protocol_version",
                        "task_id",
                        "session_id",
                        "intent_id",
                        "action_execution_id",
                        "source_capture_id",
                        "status",
                        "execution_outcome",
                        "verification_outcome",
                        "result_capture_id",
                        "error_code",
                        "error_message",
                        "retryable",
                        "state_summary",
                        "state_diff",
                    }
                ),
                required_fields=frozenset(
                    {
                        "protocol_version",
                        "task_id",
                        "session_id",
                        "intent_id",
                        "action_execution_id",
                        "source_capture_id",
                        "status",
                    }
                ),
                field_name="InteractionResult",
            )
            return cls(
                task_id=values["task_id"],
                session_id=values["session_id"],
                intent_id=values["intent_id"],
                action_execution_id=values["action_execution_id"],
                source_capture_id=values["source_capture_id"],
                status=InteractionResultStatus(values["status"]),
                execution_outcome=(
                    InteractionExecutionOutcome(values["execution_outcome"])
                    if values.get("execution_outcome") is not None
                    else None
                ),
                verification_outcome=(
                    InteractionVerificationOutcome(values["verification_outcome"])
                    if values.get("verification_outcome") is not None
                    else None
                ),
                result_capture_id=values.get("result_capture_id"),
                error_code=values.get("error_code"),
                error_message=values.get("error_message"),
                retryable=values.get("retryable", False),
                state_summary=require_mapping(
                    values.get("state_summary", {}),
                    "state_summary",
                ),
                state_diff=require_mapping(values.get("state_diff", {}), "state_diff"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise InteractionValidationError(str(exc)) from exc


def _execution_outcome_for_status(
    status: InteractionResultStatus,
) -> InteractionExecutionOutcome:
    return {
        InteractionResultStatus.SUCCEEDED: InteractionExecutionOutcome.SUCCEEDED,
        InteractionResultStatus.EVIDENCE_PENDING: InteractionExecutionOutcome.SUCCEEDED,
        InteractionResultStatus.EVIDENCE_FAILED: InteractionExecutionOutcome.SUCCEEDED,
        InteractionResultStatus.FAILED: InteractionExecutionOutcome.FAILED,
        InteractionResultStatus.BLOCKED: InteractionExecutionOutcome.REJECTED,
        InteractionResultStatus.TIMED_OUT: InteractionExecutionOutcome.TIMEOUT,
    }[status]


def _verification_outcome_for_status(
    status: InteractionResultStatus,
) -> InteractionVerificationOutcome:
    if status is InteractionResultStatus.EVIDENCE_PENDING:
        return InteractionVerificationOutcome.PENDING
    return InteractionVerificationOutcome.UNVERIFIED
