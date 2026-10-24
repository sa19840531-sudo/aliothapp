"""Journey 的可恢复顺序执行、重试和完成状态机。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from services.api.app.application.journey_execution import (
    CommitJourneyStepCommand,
    JourneyExecutionPersistenceError,
    JourneyExecutionPolicy,
    JourneyExecutionRepository,
    JourneyStepExecutionError,
    JourneyStepExecutionRequest,
    JourneyStepExecutionResult,
)
from services.api.app.application.journey_step_handlers import JourneyStepRuntimeRouter
from services.api.app.domain.journey import (
    JourneyExecutionPlan,
    JourneyExecutionSpec,
    JourneyExecutionState,
    JourneyExecutionStatus,
    JourneyStep,
    JourneyStepAttempt,
    JourneyStepAttemptStatus,
    JourneyStepKind,
    JourneyValidationError,
    build_journey_step_attempt_id,
)
from services.api.app.domain.journey.definitions import JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION


class JourneyRunner:
    """从持久化游标恢复，逐步执行并原子记录每次尝试。"""

    def __init__(
        self,
        *,
        repository: JourneyExecutionRepository,
        router: JourneyStepRuntimeRouter,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._router = router
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep

    async def run(
        self,
        *,
        plan: JourneyExecutionPlan,
        spec: JourneyExecutionSpec,
        policy: JourneyExecutionPolicy,
    ) -> JourneyExecutionState:
        _validate_run_inputs(plan=plan, spec=spec)
        state = await self._repository.start(spec=spec)
        while not state.status.is_terminal:
            if state.next_step_index >= len(plan.steps):
                raise JourneyExecutionPersistenceError("运行中 Journey 游标超过计划边界")
            step = plan.steps[state.next_step_index]
            state = await self._run_attempt(
                state=state,
                step=step,
                total_steps=len(plan.steps),
                policy=policy,
            )
        return state

    async def _run_attempt(
        self,
        *,
        state: JourneyExecutionState,
        step: JourneyStep,
        total_steps: int,
        policy: JourneyExecutionPolicy,
    ) -> JourneyExecutionState:
        started_at = self._clock()
        attempt_no = state.current_step_attempt_count + 1
        deadline = min(
            state.spec.deadline,
            started_at + timedelta(milliseconds=step.timeout_ms),
        )
        result: JourneyStepExecutionResult | None = None
        failure: JourneyStepExecutionError | None = None
        if deadline <= started_at:
            failure = JourneyStepExecutionError(
                error_code="JOURNEY_DEADLINE_EXCEEDED",
                message="Journey 或步骤已超过截止时间",
                retryable=False,
            )
        else:
            request = JourneyStepExecutionRequest(
                spec=state.spec,
                step=step,
                step_index=state.next_step_index,
                attempt_no=attempt_no,
                source_capture_id=state.current_capture_id,
                deadline=deadline,
                policy=policy,
            )
            try:
                result = await asyncio.wait_for(
                    self._router.execute(request),
                    timeout=(deadline - started_at).total_seconds(),
                )
                if not isinstance(result, JourneyStepExecutionResult):
                    raise TypeError("Journey 步骤处理器返回类型不正确")
            except TimeoutError:
                failure = JourneyStepExecutionError(
                    error_code="JOURNEY_STEP_TIMED_OUT",
                    message="Journey 步骤执行超时",
                    retryable=step.kind is not JourneyStepKind.ACTION,
                    safe_to_continue=step.kind is not JourneyStepKind.ACTION,
                )
            except JourneyStepExecutionError as exc:
                failure = exc
            except Exception as exc:
                failure = JourneyStepExecutionError(
                    error_code="JOURNEY_STEP_UNEXPECTED_ERROR",
                    message=f"Journey 步骤出现未分类错误：{type(exc).__name__}",
                    retryable=False,
                    safe_to_continue=step.kind is not JourneyStepKind.ACTION,
                )

        finished_at = self._clock()
        if failure is None:
            assert result is not None
            return await self._commit_success(
                state=state,
                step=step,
                attempt_no=attempt_no,
                result=result,
                started_at=started_at,
                finished_at=finished_at,
                total_steps=total_steps,
            )
        return await self._commit_failure(
            state=state,
            step=step,
            attempt_no=attempt_no,
            failure=failure,
            started_at=started_at,
            finished_at=finished_at,
            total_steps=total_steps,
        )

    async def _commit_success(
        self,
        *,
        state: JourneyExecutionState,
        step: JourneyStep,
        attempt_no: int,
        result: JourneyStepExecutionResult,
        started_at: datetime,
        finished_at: datetime,
        total_steps: int,
    ) -> JourneyExecutionState:
        next_index = state.next_step_index + 1
        target = _success_status(
            is_last=next_index == total_steps,
            gap_count=state.gap_count,
        )
        attempt = _build_attempt(
            state=state,
            step=step,
            attempt_no=attempt_no,
            status=JourneyStepAttemptStatus.SUCCEEDED,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
        )
        return await self._repository.commit_step(
            command=CommitJourneyStepCommand(
                execution_id=state.spec.execution_id,
                expected_row_version=state.row_version,
                expected_step_index=state.next_step_index,
                expected_attempt_count=state.current_step_attempt_count,
                attempt=attempt,
                target_status=target,
                next_step_index=next_index,
                next_step_attempt_count=0,
                current_capture_id=result.result_capture_id or state.current_capture_id,
                gap_count=state.gap_count,
            )
        )

    async def _commit_failure(
        self,
        *,
        state: JourneyExecutionState,
        step: JourneyStep,
        attempt_no: int,
        failure: JourneyStepExecutionError,
        started_at: datetime,
        finished_at: datetime,
        total_steps: int,
    ) -> JourneyExecutionState:
        retry = (
            failure.retryable
            and attempt_no < step.retry.max_attempts
            and (not step.retry.retry_on or failure.error_code in step.retry.retry_on)
            and finished_at < state.spec.deadline
        )
        if retry:
            status = JourneyStepAttemptStatus.FAILED
            target = JourneyExecutionStatus.RUNNING
            next_index = state.next_step_index
            next_attempt_count = attempt_no
            gap_count = state.gap_count
            final_error_code = None
            final_error_message = None
        elif step.required or not failure.safe_to_continue:
            status = JourneyStepAttemptStatus.FAILED
            target = JourneyExecutionStatus.FAILED
            next_index = state.next_step_index
            next_attempt_count = attempt_no
            gap_count = state.gap_count
            final_error_code = failure.error_code
            final_error_message = str(failure)
        else:
            status = JourneyStepAttemptStatus.SKIPPED
            next_index = state.next_step_index + 1
            gap_count = state.gap_count + 1
            target = _success_status(
                is_last=next_index == total_steps,
                gap_count=gap_count,
            )
            next_attempt_count = 0
            final_error_code = None
            final_error_message = None
        attempt = _build_attempt(
            state=state,
            step=step,
            attempt_no=attempt_no,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            error=failure,
        )
        committed = await self._repository.commit_step(
            command=CommitJourneyStepCommand(
                execution_id=state.spec.execution_id,
                expected_row_version=state.row_version,
                expected_step_index=state.next_step_index,
                expected_attempt_count=state.current_step_attempt_count,
                attempt=attempt,
                target_status=target,
                next_step_index=next_index,
                next_step_attempt_count=next_attempt_count,
                current_capture_id=state.current_capture_id,
                gap_count=gap_count,
                error_code=final_error_code,
                error_message=final_error_message,
            )
        )
        if retry and step.retry.backoff_ms > 0:
            remaining = (state.spec.deadline - self._clock()).total_seconds()
            if remaining > 0:
                await self._sleep(min(step.retry.backoff_ms / 1000, remaining))
        return committed


def _build_attempt(
    *,
    state: JourneyExecutionState,
    step: JourneyStep,
    attempt_no: int,
    status: JourneyStepAttemptStatus,
    started_at: datetime,
    finished_at: datetime,
    result: JourneyStepExecutionResult | None = None,
    error: JourneyStepExecutionError | None = None,
) -> JourneyStepAttempt:
    return JourneyStepAttempt(
        attempt_id=build_journey_step_attempt_id(
            execution_id=state.spec.execution_id,
            step_id=step.step_id,
            attempt_no=attempt_no,
        ),
        execution_id=state.spec.execution_id,
        step_id=step.step_id,
        step_index=state.next_step_index,
        attempt_no=attempt_no,
        status=status,
        source_capture_id=state.current_capture_id,
        result_capture_id=result.result_capture_id if result is not None else None,
        action_execution_id=(result.action_execution_id if result is not None else None),
        error_code=error.error_code if error is not None else None,
        error_message=str(error) if error is not None else None,
        retryable=error.retryable if status is JourneyStepAttemptStatus.FAILED else False,
        started_at=started_at,
        finished_at=finished_at,
    )


def _success_status(*, is_last: bool, gap_count: int) -> JourneyExecutionStatus:
    if not is_last:
        return JourneyExecutionStatus.RUNNING
    if gap_count:
        return JourneyExecutionStatus.COMPLETED_WITH_GAPS
    return JourneyExecutionStatus.SUCCEEDED


def _validate_run_inputs(
    *,
    plan: JourneyExecutionPlan,
    spec: JourneyExecutionSpec,
) -> None:
    if plan.schema_version != JOURNEY_EXECUTION_PLAN_SCHEMA_VERSION:
        raise JourneyValidationError("顺序 Runner 不支持检查点计划，禁止伪造流程完成")
    if plan.execution_plan_id != spec.execution_plan_id or plan.task_id != spec.task_id:
        raise JourneyValidationError("Journey 执行计划与执行身份不一致")
