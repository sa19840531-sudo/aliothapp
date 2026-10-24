"""任务级 Draining、Completion Gate、Review Policy 与报告绑定应用端口。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.application.report_payloads import (
    normalize_report_payload,
    stable_report_payload_hash,
)
from services.api.app.domain.protocol import require_text
from services.api.app.domain.task import (
    CompletionGateDecision,
    ReportGateBinding,
    TaskExecutionStatus,
    TaskFinalizationSnapshot,
    TaskQualityOutcome,
)


@dataclass(frozen=True, kw_only=True)
class BeginExplorationDrainingCommand:
    """停止接收普通 Frontier 工作所需的乐观锁命令。"""

    task_id: str
    agent_run_id: str
    expected_row_version: int
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.agent_run_id, "agent_run_id")
        _require_nonnegative(self.expected_row_version, "expected_row_version")
        _validate_recovery_fence(self)

    def __deepcopy__(self, _memo: dict[int, object]) -> BeginExplorationDrainingCommand:
        return self


@dataclass(frozen=True, kw_only=True)
class BeginSessionDrainingCommand:
    """把任务所有活跃 Surface Session 转入排空态。"""

    task_id: str
    agent_run_id: str
    expected_finalization_epoch: int
    expected_row_version: int
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.agent_run_id, "agent_run_id")
        _require_nonnegative(
            self.expected_finalization_epoch,
            "expected_finalization_epoch",
        )
        _require_nonnegative(self.expected_row_version, "expected_row_version")
        _validate_recovery_fence(self)

    def __deepcopy__(self, _memo: dict[int, object]) -> BeginSessionDrainingCommand:
        return self


@dataclass(frozen=True, kw_only=True)
class TerminateTaskFinalizationCommand:
    """在正式收口无法继续时原子写入 Task 权威终态。"""

    task_id: str
    agent_run_id: str
    expected_finalization_epoch: int
    expected_row_version: int
    execution_status: TaskExecutionStatus
    reason_code: str
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.agent_run_id, "agent_run_id")
        require_text(self.reason_code, "reason_code")
        _require_nonnegative(
            self.expected_finalization_epoch,
            "expected_finalization_epoch",
        )
        _require_nonnegative(self.expected_row_version, "expected_row_version")
        if self.execution_status not in {
            TaskExecutionStatus.NEEDS_REVIEW,
            TaskExecutionStatus.FAILED,
        }:
            raise ValueError("收口阻断只允许 needs_review 或 failed")
        _validate_recovery_fence(self)

    def __deepcopy__(self, _memo: dict[int, object]) -> TerminateTaskFinalizationCommand:
        return self


@dataclass(frozen=True, kw_only=True)
class EvaluateCompletionGateCommand:
    """事务内重算八项事实并在放行时封存任务。"""

    task_id: str
    agent_run_id: str
    expected_finalization_epoch: int
    expected_row_version: int
    execution_status: TaskExecutionStatus
    quality_outcome: TaskQualityOutcome
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.agent_run_id, "agent_run_id")
        _require_nonnegative(
            self.expected_finalization_epoch,
            "expected_finalization_epoch",
        )
        _require_nonnegative(self.expected_row_version, "expected_row_version")
        if not isinstance(self.execution_status, TaskExecutionStatus):
            raise ValueError("execution_status 不受支持")
        if not isinstance(self.quality_outcome, TaskQualityOutcome):
            raise ValueError("quality_outcome 不受支持")
        _validate_recovery_fence(self)

    def __deepcopy__(self, _memo: dict[int, object]) -> EvaluateCompletionGateCommand:
        return self


@dataclass(frozen=True, kw_only=True)
class FinalizeTaskWithReportCommand:
    """在封存前原子写入最终报告、绑定快照并封存任务。"""

    task_id: str
    agent_run_id: str
    report_id: str
    report_version: int
    expected_finalization_epoch: int
    expected_row_version: int
    execution_status: TaskExecutionStatus
    quality_outcome: TaskQualityOutcome
    final_report: dict[str, object]
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "report_id"):
            require_text(getattr(self, field_name), field_name)
        _require_positive(self.report_version, "report_version")
        _require_nonnegative(
            self.expected_finalization_epoch,
            "expected_finalization_epoch",
        )
        _require_nonnegative(self.expected_row_version, "expected_row_version")
        if not isinstance(self.execution_status, TaskExecutionStatus):
            raise ValueError("execution_status 不受支持")
        if not isinstance(self.quality_outcome, TaskQualityOutcome):
            raise ValueError("quality_outcome 不受支持")
        if not isinstance(self.final_report, dict):
            raise ValueError("final_report 必须为对象")
        object.__setattr__(
            self,
            "final_report",
            normalize_report_payload(self.final_report),
        )
        _validate_recovery_fence(self)

    def __deepcopy__(self, _memo: dict[int, object]) -> FinalizeTaskWithReportCommand:
        return self


@dataclass(frozen=True, kw_only=True)
class RecordReviewPolicyDecisionCommand:
    """Review 模块对当前 review_generation 给出的可审计策略决议。"""

    task_id: str
    decision_id: str
    policy_hash: str
    satisfied: bool
    rationale: str
    expected_row_version: int

    def __post_init__(self) -> None:
        for field_name in ("task_id", "decision_id", "policy_hash", "rationale"):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.satisfied, bool):
            raise ValueError("satisfied 必须为布尔值")
        _require_nonnegative(self.expected_row_version, "expected_row_version")


@dataclass(frozen=True, kw_only=True)
class BindReportToGateCommand:
    """Report Service 把一个不可变报告版本绑定到放行快照。"""

    report_id: str
    report_version: int
    report_payload_hash: str
    final_report: dict[str, object]
    task_id: str
    gate_snapshot_id: str
    finalization_epoch: int
    agent_run_id: str | None = None
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "report_id",
            "report_payload_hash",
            "task_id",
            "gate_snapshot_id",
        ):
            require_text(getattr(self, field_name), field_name)
        _require_positive(self.report_version, "report_version")
        _require_nonnegative(self.finalization_epoch, "finalization_epoch")
        if not isinstance(self.final_report, dict):
            raise ValueError("final_report 必须为对象")
        normalized_report = normalize_report_payload(self.final_report)
        object.__setattr__(self, "final_report", normalized_report)
        if stable_report_payload_hash(normalized_report) != self.report_payload_hash:
            raise ValueError("report_payload_hash 与 final_report 不一致")
        if self.agent_run_id is not None:
            require_text(self.agent_run_id, "agent_run_id")
        _validate_recovery_fence(self)


@dataclass(frozen=True, kw_only=True)
class FinalizationRecoveryLease:
    """一次 Web V2 收口恢复的 fenced 租约。"""

    snapshot: TaskFinalizationSnapshot
    lease_token: str
    recovery_generation: int
    expected_status: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        require_text(self.lease_token, "lease_token")
        _require_nonnegative(self.recovery_generation, "recovery_generation")
        require_text(self.expected_status, "expected_status")
        if not isinstance(self.lease_expires_at, datetime):
            raise ValueError("lease_expires_at 必须为 datetime")

    @property
    def task_id(self) -> str:
        return self.snapshot.task_id

    @property
    def agent_run_id(self) -> str | None:
        return self.snapshot.agent_run_id


class TaskFinalizationRepository(Protocol):
    """任务完成控制面的持久化端口。"""

    async def get(self, *, task_id: str) -> TaskFinalizationSnapshot | None: ...

    async def list_session_draining(
        self,
        *,
        limit: int = 100,
    ) -> list[TaskFinalizationSnapshot]: ...

    async def list_recoverable_finalizations(
        self,
        *,
        limit: int = 100,
    ) -> list[TaskFinalizationSnapshot]: ...

    async def defer_unchanged_completion_gate(
        self,
        *,
        command: EvaluateCompletionGateCommand,
    ) -> bool: ...

    async def claim_recoverable_finalizations(
        self,
        *,
        limit: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> list[FinalizationRecoveryLease]: ...

    async def defer_finalization_retry(
        self,
        *,
        task_id: str,
        retry_after_seconds: float,
        error_message: str | None = None,
    ) -> None: ...

    async def record_finalization_retry_error(
        self,
        *,
        task_id: str,
        error_message: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: datetime | None = None,
    ) -> None: ...

    async def renew_finalization_recovery_lease(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        lease_seconds: float,
    ) -> FinalizationRecoveryLease: ...

    async def record_finalization_retry_success(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: datetime | None = None,
    ) -> None: ...

    async def quarantine_missing_sealed_report(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: datetime | None = None,
    ) -> None: ...

    async def begin_exploration_draining(
        self,
        *,
        command: BeginExplorationDrainingCommand,
    ) -> TaskFinalizationSnapshot: ...

    async def begin_session_draining(
        self,
        *,
        command: BeginSessionDrainingCommand,
    ) -> TaskFinalizationSnapshot: ...

    async def terminate(
        self,
        *,
        command: TerminateTaskFinalizationCommand,
    ) -> TaskFinalizationSnapshot: ...

    async def terminate_stalled(
        self,
        *,
        command: TerminateTaskFinalizationCommand,
        minimum_terminal_age_seconds: float,
    ) -> TaskFinalizationSnapshot | None: ...

    async def evaluate_and_seal(
        self,
        *,
        command: EvaluateCompletionGateCommand,
    ) -> CompletionGateDecision: ...

    async def finalize_task_with_report(
        self,
        *,
        command: FinalizeTaskWithReportCommand,
    ) -> CompletionGateDecision: ...

    async def record_review_policy_decision(
        self,
        *,
        command: RecordReviewPolicyDecisionCommand,
    ) -> TaskFinalizationSnapshot: ...

    async def bind_report(
        self,
        *,
        command: BindReportToGateCommand,
    ) -> ReportGateBinding: ...


class TaskFinalizationService:
    """供确定性 Orchestrator、Review 与 Report 模块共享的应用入口。"""

    def __init__(
        self,
        repository: TaskFinalizationRepository,
        *,
        completion_gate_preparer: object | None = None,
    ) -> None:
        self._repository = repository
        self._completion_gate_preparer = completion_gate_preparer

    @property
    def completion_gate_preparer(self) -> object | None:
        """返回组合根注入的 Gate 前置事实发布器。"""

        return self._completion_gate_preparer

    async def app_completion_ready_before_timeout(
        self, command: EvaluateCompletionGateCommand,
    ) -> bool:
        """真实门禁可通过时优先正式收口，不能按主 Run 年龄改成技术失败。"""

        check = getattr(self._repository, "app_completion_ready_before_timeout", None)
        return bool(callable(check) and await check(command=command))

    async def defer_unchanged_completion_gate(
        self,
        command: EvaluateCompletionGateCommand,
    ) -> bool:
        """先推进已有结算，再以数据库事实判断能否跳过报告生成。"""

        if not isinstance(command, EvaluateCompletionGateCommand):
            raise TypeError("command 必须为 EvaluateCompletionGateCommand")
        if self._completion_gate_preparer is not None:
            fence = (
                {
                    "recovery_lease_token": command.recovery_lease_token,
                    "recovery_generation": command.recovery_generation,
                    "recovery_lease_expires_at": command.recovery_lease_expires_at,
                }
                if command.recovery_lease_token is not None
                else {}
            )
            await self._completion_gate_preparer.prepare(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                finalization_epoch=command.expected_finalization_epoch,
                **fence,
            )
        return await self._repository.defer_unchanged_completion_gate(command=command)

    async def get(self, *, task_id: str) -> TaskFinalizationSnapshot | None:
        return await self._repository.get(task_id=require_text(task_id, "task_id"))

    async def list_session_draining(
        self,
        *,
        limit: int = 100,
    ) -> list[TaskFinalizationSnapshot]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1000:
            raise ValueError("limit 必须是 1 到 1000 之间的整数")
        return await self._repository.list_session_draining(limit=limit)

    async def list_recoverable_finalizations(
        self,
        *,
        limit: int = 100,
    ) -> list[TaskFinalizationSnapshot]:
        """列出仍需 Gate 评估或报告绑定重试的持久化任务。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1000:
            raise ValueError("limit 必须是 1 到 1000 之间的整数")
        return await self._repository.list_recoverable_finalizations(limit=limit)

    async def claim_recoverable_finalizations(
        self,
        *,
        limit: int = 100,
        lease_owner: str,
        lease_seconds: float = 30.0,
    ) -> list[FinalizationRecoveryLease]:
        """原子领取 Web V2 正式收口恢复租约。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1000:
            raise ValueError("limit 必须是 1 到 1000 之间的整数")
        owner = require_text(lease_owner, "lease_owner")
        if len(owner) > 160:
            raise ValueError("lease_owner 最长 160 字符")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int | float):
            raise ValueError("lease_seconds 必须为数字")
        if not 0 < lease_seconds <= 3600:
            raise ValueError("lease_seconds 必须在 0 到 3600 秒之间")
        return await self._repository.claim_recoverable_finalizations(
            limit=limit,
            lease_owner=owner,
            lease_seconds=float(lease_seconds),
        )

    async def defer_finalization_retry(
        self,
        *,
        task_id: str,
        retry_after_seconds: float,
        error_message: str | None = None,
    ) -> None:
        """持久化下一次恢复时间，让有限批次能够公平轮转。"""

        if isinstance(retry_after_seconds, bool) or not isinstance(
            retry_after_seconds, int | float
        ):
            raise ValueError("retry_after_seconds 必须为数字")
        if retry_after_seconds <= 0 or retry_after_seconds > 3600:
            raise ValueError("retry_after_seconds 必须在 0 到 3600 秒之间")
        await self._repository.defer_finalization_retry(
            task_id=require_text(task_id, "task_id"),
            retry_after_seconds=float(retry_after_seconds),
            error_message=error_message,
        )

    async def record_finalization_retry_error(
        self,
        *,
        task_id: str,
        error_message: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: datetime | None = None,
    ) -> None:
        """记录最近一次恢复失败原因，不重复增加重试次数。"""

        _validate_optional_lease_expiry(lease_expires_at)

        await self._repository.record_finalization_retry_error(
            task_id=require_text(task_id, "task_id"),
            error_message=require_text(error_message, "error_message")[:2000],
            lease_token=require_text(lease_token, "lease_token"),
            recovery_generation=recovery_generation,
            expected_status=require_text(expected_status, "expected_status"),
            lease_expires_at=lease_expires_at,
        )

    async def renew_finalization_recovery_lease(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        lease_seconds: float = 30.0,
    ) -> FinalizationRecoveryLease:
        """仅由当前未过期 holder 续租，generation 不变。"""

        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int | float):
            raise ValueError("lease_seconds 必须为数字")
        if not 0 < lease_seconds <= 3600:
            raise ValueError("lease_seconds 必须在 0 到 3600 秒之间")
        return await self._repository.renew_finalization_recovery_lease(
            task_id=require_text(task_id, "task_id"),
            lease_token=require_text(lease_token, "lease_token"),
            recovery_generation=_require_nonnegative(
                recovery_generation, "recovery_generation"
            ),
            lease_seconds=float(lease_seconds),
        )

    async def record_finalization_retry_success(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: datetime | None = None,
    ) -> None:
        """成功推进后清除连续错误签名，下一次异常重新计数。"""

        _validate_optional_lease_expiry(lease_expires_at)

        await self._repository.record_finalization_retry_success(
            task_id=require_text(task_id, "task_id"),
            lease_token=require_text(lease_token, "lease_token"),
            recovery_generation=recovery_generation,
            expected_status=require_text(expected_status, "expected_status"),
            lease_expires_at=lease_expires_at,
        )

    async def quarantine_missing_sealed_report(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: datetime | None = None,
    ) -> None:
        _validate_optional_lease_expiry(lease_expires_at)
        await self._repository.quarantine_missing_sealed_report(
            task_id=require_text(task_id, "task_id"),
            lease_token=require_text(lease_token, "lease_token"),
            recovery_generation=recovery_generation,
            expected_status=require_text(expected_status, "expected_status"),
            lease_expires_at=lease_expires_at,
        )

    async def begin_exploration_draining(
        self,
        command: BeginExplorationDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        if not isinstance(command, BeginExplorationDrainingCommand):
            raise TypeError("command 必须为 BeginExplorationDrainingCommand")
        return await self._repository.begin_exploration_draining(command=command)

    async def begin_session_draining(
        self,
        command: BeginSessionDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        if not isinstance(command, BeginSessionDrainingCommand):
            raise TypeError("command 必须为 BeginSessionDrainingCommand")
        return await self._repository.begin_session_draining(command=command)

    async def terminate(
        self,
        command: TerminateTaskFinalizationCommand,
    ) -> TaskFinalizationSnapshot:
        if not isinstance(command, TerminateTaskFinalizationCommand):
            raise TypeError("command 必须为 TerminateTaskFinalizationCommand")
        return await self._repository.terminate(command=command)

    async def terminate_stalled(
        self,
        command: TerminateTaskFinalizationCommand,
        *,
        minimum_terminal_age_seconds: float,
    ) -> TaskFinalizationSnapshot | None:
        """仅在数据库锁内确认终态超龄且所有工作已停止后结束异常收口。"""
        if not isinstance(command, TerminateTaskFinalizationCommand):
            raise TypeError("command 必须为 TerminateTaskFinalizationCommand")
        if (
            isinstance(minimum_terminal_age_seconds, bool)
            or not isinstance(minimum_terminal_age_seconds, (int, float))
            or not math.isfinite(minimum_terminal_age_seconds)
            or minimum_terminal_age_seconds < 30
        ):
            raise ValueError("异常收口宽限必须至少 30 秒")
        return await self._repository.terminate_stalled(
            command=command, minimum_terminal_age_seconds=minimum_terminal_age_seconds
        )

    async def evaluate_and_seal(
        self,
        command: EvaluateCompletionGateCommand,
    ) -> CompletionGateDecision:
        if not isinstance(command, EvaluateCompletionGateCommand):
            raise TypeError("command 必须为 EvaluateCompletionGateCommand")
        return await self._repository.evaluate_and_seal(command=command)

    async def finalize_task_with_report(
        self,
        command: FinalizeTaskWithReportCommand,
    ) -> CompletionGateDecision:
        if not isinstance(command, FinalizeTaskWithReportCommand):
            raise TypeError("command 必须为 FinalizeTaskWithReportCommand")
        return await self._repository.finalize_task_with_report(command=command)

    async def record_review_policy_decision(
        self,
        command: RecordReviewPolicyDecisionCommand,
    ) -> TaskFinalizationSnapshot:
        if not isinstance(command, RecordReviewPolicyDecisionCommand):
            raise TypeError("command 必须为 RecordReviewPolicyDecisionCommand")
        return await self._repository.record_review_policy_decision(command=command)

    async def bind_report(self, command: BindReportToGateCommand) -> ReportGateBinding:
        if not isinstance(command, BindReportToGateCommand):
            raise TypeError("command 必须为 BindReportToGateCommand")
        return await self._repository.bind_report(command=command)


def _require_nonnegative(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须为非负整数")
    return value


def _require_positive(value: object, field_name: str) -> int:
    result = _require_nonnegative(value, field_name)
    if result == 0:
        raise ValueError(f"{field_name} 必须为正整数")
    return result


def _validate_recovery_fence(command: object) -> None:
    """恢复 fence 必须整组出现；普通前台命令保持全部为空。"""

    token = getattr(command, "recovery_lease_token", None)
    generation = getattr(command, "recovery_generation", None)
    expires_at = getattr(command, "recovery_lease_expires_at", None)
    if token is None and generation is None and expires_at is None:
        return
    require_text(token, "recovery_lease_token")
    _require_nonnegative(generation, "recovery_generation")
    if not isinstance(expires_at, datetime):
        raise ValueError("recovery_lease_expires_at 必须为 datetime")


def _validate_optional_lease_expiry(value: object) -> None:
    if value is not None and not isinstance(value, datetime):
        raise ValueError("lease_expires_at 必须为 datetime")
