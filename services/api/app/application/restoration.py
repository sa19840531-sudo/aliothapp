"""恢复执行端口、状态等价门禁与持久化尝试的协调器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.domain.inspection import (
    EvidenceRequest,
    RestorationAttempt,
    RestorationAttemptStatus,
    RestorationPlan,
    RestorationStrategy,
    StateInvariantSnapshot,
    build_restoration_attempt_id,
)


class RestorationExecutionError(RuntimeError):
    """Surface 恢复端口返回的结构化失败。"""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        unfulfillable: bool = False,
    ) -> None:
        super().__init__(message)
        if not error_code.strip():
            raise ValueError("error_code 不能为空")
        self.error_code = error_code
        self.unfulfillable = unfulfillable


@dataclass(frozen=True, kw_only=True)
class RestorationExecutionRequest:
    """平台恢复适配器可消费、但不含底层句柄的请求。"""

    request: EvidenceRequest
    plan: RestorationPlan
    restoration_attempt_id: str


@dataclass(frozen=True, kw_only=True)
class RestorationExecutionResult:
    """恢复后的 Capture 状态和完整动作审计引用。"""

    restored_snapshot: StateInvariantSnapshot
    action_execution_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.restored_snapshot, StateInvariantSnapshot):
            raise TypeError("restored_snapshot 类型不正确")
        if not isinstance(self.action_execution_ids, tuple):
            raise TypeError("action_execution_ids 必须为元组")
        if any(not item.strip() for item in self.action_execution_ids):
            raise ValueError("action_execution_ids 不能包含空值")
        if len(self.action_execution_ids) != len(set(self.action_execution_ids)):
            raise ValueError("action_execution_ids 不能重复")


class RestorationExecutor(Protocol):
    """Web/App Owner 实现的恢复端口；每个动作仍须走 Interaction Runtime。"""

    async def restore(
        self,
        request: RestorationExecutionRequest,
    ) -> RestorationExecutionResult: ...


class RestorationAttemptRepository(Protocol):
    """恢复协调器使用的持久化尝试端口。"""

    async def start(
        self,
        *,
        plan: RestorationPlan,
        attempt_no: int,
    ) -> RestorationAttempt: ...

    async def complete(
        self,
        *,
        attempt: RestorationAttempt,
        expected_row_version: int,
    ) -> RestorationAttempt: ...

    async def find_equivalent_for_lease(
        self,
        *,
        fence: EvidenceRequestLeaseFence,
    ) -> RestorationAttempt | None: ...


class RestorationCoordinator:
    """固定恢复、Capture、等价评估和终态提交顺序。"""

    def __init__(
        self,
        *,
        repository: RestorationAttemptRepository,
        executor: RestorationExecutor,
        equivalence_gate: StateEquivalenceGate,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._equivalence_gate = equivalence_gate
        self._clock = clock or (lambda: datetime.now(UTC))

    async def restore(
        self,
        *,
        request: EvidenceRequest,
        plan: RestorationPlan,
        attempt_no: int,
        original_snapshot: StateInvariantSnapshot,
    ) -> RestorationAttempt:
        _validate_restoration_inputs(
            request=request,
            plan=plan,
            attempt_no=attempt_no,
            original_snapshot=original_snapshot,
        )
        running = await self._repository.start(plan=plan, attempt_no=attempt_no)
        if running.status.is_terminal:
            return running
        if plan.strategy is RestorationStrategy.UNFULFILLABLE:
            terminal = replace(
                running,
                status=RestorationAttemptStatus.UNFULFILLABLE,
                error_code="RESTORATION_UNFULFILLABLE",
                error_message="Evidence Request 的恢复约束不可满足",
                finished_at=self._clock(),
            )
            return await self._repository.complete(
                attempt=terminal,
                expected_row_version=running.row_version,
            )
        result: RestorationExecutionResult | None = None
        try:
            result = await self._executor.restore(
                RestorationExecutionRequest(
                    request=request,
                    plan=plan,
                    restoration_attempt_id=running.attempt_id,
                )
            )
            assessment = self._equivalence_gate.assess(
                request=request,
                plan=plan,
                restoration_attempt_id=running.attempt_id,
                original=original_snapshot,
                restored=result.restored_snapshot,
            )
            terminal = replace(
                running,
                status=(
                    RestorationAttemptStatus.EQUIVALENT
                    if assessment.passed
                    else RestorationAttemptStatus.DIVERGENT
                ),
                restored_capture_id=result.restored_snapshot.capture_id,
                assessment=assessment,
                action_execution_ids=result.action_execution_ids,
                finished_at=self._clock(),
            )
        except RestorationExecutionError as exc:
            terminal = replace(
                running,
                status=(
                    RestorationAttemptStatus.UNFULFILLABLE
                    if exc.unfulfillable
                    else RestorationAttemptStatus.FAILED
                ),
                error_code=exc.error_code,
                error_message=str(exc),
                finished_at=self._clock(),
            )
        except Exception as exc:
            terminal = replace(
                running,
                status=RestorationAttemptStatus.FAILED,
                restored_capture_id=(
                    result.restored_snapshot.capture_id if result is not None else None
                ),
                action_execution_ids=(result.action_execution_ids if result is not None else ()),
                error_code="RESTORATION_UNEXPECTED_ERROR",
                error_message=f"恢复执行出现未分类错误：{type(exc).__name__}",
                finished_at=self._clock(),
            )
        return await self._repository.complete(
            attempt=terminal,
            expected_row_version=running.row_version,
        )


def new_running_restoration_attempt(
    *,
    plan: RestorationPlan,
    attempt_no: int,
    started_at: datetime,
) -> RestorationAttempt:
    """供仓储和内存适配器创建一致的运行中尝试。"""
    return RestorationAttempt(
        attempt_id=build_restoration_attempt_id(
            plan_id=plan.plan_id,
            attempt_no=attempt_no,
        ),
        plan=plan,
        attempt_no=attempt_no,
        status=RestorationAttemptStatus.RUNNING,
        started_at=started_at,
    )


def _validate_restoration_inputs(
    *,
    request: EvidenceRequest,
    plan: RestorationPlan,
    attempt_no: int,
    original_snapshot: StateInvariantSnapshot,
) -> None:
    if not isinstance(request, EvidenceRequest):
        raise TypeError("request 必须为 EvidenceRequest")
    if not isinstance(plan, RestorationPlan):
        raise TypeError("plan 必须为 RestorationPlan")
    if not isinstance(original_snapshot, StateInvariantSnapshot):
        raise TypeError("original_snapshot 必须为 StateInvariantSnapshot")
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no <= 0:
        raise ValueError("attempt_no 必须为正整数")
    if plan.request_id != request.request_id or plan.task_id != request.task_id:
        raise ValueError("恢复计划与 Evidence Request 身份不一致")
    if plan.original_capture_id != request.target_capture_id:
        raise ValueError("恢复计划不是 Evidence Request 的目标 Capture")
    if (
        original_snapshot.task_id != request.task_id
        or original_snapshot.capture_id != request.target_capture_id
        or original_snapshot.session_id != plan.source_session_id
        or original_snapshot.surface_kind is not plan.surface_kind
    ):
        raise ValueError("原状态快照与恢复计划身份不一致")
