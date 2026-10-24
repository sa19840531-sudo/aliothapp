"""Journey 步骤类型到独立运行时适配器的确定性路由。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.journey_execution import (
    JourneyAssertionEvaluator,
    JourneyCaptureExecutor,
    JourneyConditionEvaluator,
    JourneyStepExecutionError,
    JourneyStepExecutionRequest,
    JourneyStepExecutionResult,
    JourneyStepHandler,
)
from services.api.app.domain.interaction import ActionIntent, InteractionOperation
from services.api.app.domain.journey import JourneyStepKind
from services.api.app.domain.journey.validation import stable_hash


class JourneyStepRuntimeRouter:
    """按 DSL 步骤类型路由到独立适配器。"""

    def __init__(self, *, handlers: Mapping[JourneyStepKind, JourneyStepHandler]) -> None:
        missing = set(JourneyStepKind).difference(handlers)
        if missing:
            values = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"Journey 步骤处理器不完整：{values}")
        self._handlers = dict(handlers)

    async def execute(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult:
        return await self._handlers[request.step.kind].execute(request)


class JourneyActionStepHandler:
    """把 Journey action 编译为 ActionIntent，再交给 Capture Coordinator。"""

    def __init__(self, *, coordinator: CaptureCoordinator) -> None:
        self._coordinator = coordinator

    async def execute(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult:
        risk, purpose = request.policy.authorize(request.step)
        identity_hash = stable_hash(
            {
                "execution_id": request.spec.execution_id,
                "step_id": request.step.step_id,
            }
        )
        intent = ActionIntent(
            task_id=request.spec.task_id,
            session_id=request.spec.session_id,
            intent_id=f"journey-intent:{identity_hash}",
            idempotency_key=f"journey-action:{identity_hash}",
            surface_kind=request.spec.surface_kind,
            source_capture_id=request.source_capture_id,
            action_type=InteractionOperation(request.step.payload["operation"]),
            semantic_target=request.step.payload.get("semantic_target"),
            parameters=request.step.payload.get("arguments", {}),
            purpose=purpose,
            risk_level=risk,
            capture_profile=request.policy.capture_profile,
            deadline=request.deadline,
        )
        try:
            result = await self._coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=request.spec.agent_run_id,
                    intent=intent,
                )
            )
        except CaptureCoordinationError as exc:
            raise JourneyStepExecutionError(
                error_code=exc.error_code,
                message=str(exc),
                retryable=exc.stage is CaptureCoordinationStage.BEFORE_CAPTURE,
                safe_to_continue=exc.stage is CaptureCoordinationStage.BEFORE_CAPTURE,
            ) from exc
        return JourneyStepExecutionResult(
            result_capture_id=result.after_capture.capture_id,
            action_execution_id=result.interaction.action_execution_id,
        )


class JourneyWaitStepHandler:
    def __init__(self, *, evaluator: JourneyConditionEvaluator) -> None:
        self._evaluator = evaluator

    async def execute(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult:
        return await self._evaluator.wait(request)


class JourneyCaptureStepHandler:
    def __init__(self, *, executor: JourneyCaptureExecutor) -> None:
        self._executor = executor

    async def execute(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult:
        return await self._executor.capture(request)


class JourneyAssertStepHandler:
    def __init__(self, *, evaluator: JourneyAssertionEvaluator) -> None:
        self._evaluator = evaluator

    async def execute(
        self,
        request: JourneyStepExecutionRequest,
    ) -> JourneyStepExecutionResult:
        return await self._evaluator.assert_condition(request)
