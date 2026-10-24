"""Journey Runner 的恢复、重试、安全策略与完成语义测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationResult,
    CaptureCoordinationStage,
)
from services.api.app.application.journey_execution import (
    JourneyExecutionPolicy,
    JourneyStepExecutionError,
    JourneyStepExecutionResult,
)
from services.api.app.application.journey_runner import JourneyRunner
from services.api.app.application.journey_step_handlers import (
    JourneyActionStepHandler,
    JourneyStepRuntimeRouter,
)
from services.api.app.domain.interaction import (
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.journey import (
    JourneyExecutionSpec,
    JourneyExecutionState,
    JourneyExecutionStatus,
    JourneyStepKind,
    build_journey_execution_id,
    build_journey_execution_plan,
    parse_journey_definition,
)
from services.api.app.domain.journey.validation import stable_hash

NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)


class FakeJourneyExecutionRepository:
    def __init__(self) -> None:
        self.state = None
        self.commands = []

    async def start(self, *, spec):
        if self.state is None:
            self.state = JourneyExecutionState(
                spec=spec,
                status=JourneyExecutionStatus.RUNNING,
                next_step_index=0,
                current_step_attempt_count=0,
                current_capture_id=spec.source_capture_id,
                gap_count=0,
                row_version=0,
            )
        return self.state

    async def commit_step(self, *, command):
        assert self.state is not None
        assert command.expected_row_version == self.state.row_version
        assert command.expected_step_index == self.state.next_step_index
        assert command.expected_attempt_count == self.state.current_step_attempt_count
        self.commands.append(command)
        self.state = JourneyExecutionState(
            spec=self.state.spec,
            status=command.target_status,
            next_step_index=command.next_step_index,
            current_step_attempt_count=command.next_step_attempt_count,
            current_capture_id=command.current_capture_id,
            gap_count=command.gap_count,
            row_version=self.state.row_version + 1,
            error_code=command.error_code,
            error_message=command.error_message,
        )
        return self.state


class SequenceHandler:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeCoordinator:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.commands = []

    async def coordinate(self, command):
        self.commands.append(command)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _capture_profile() -> CaptureProfile:
    return CaptureProfile(
        profile_id="journey-default",
        before=CaptureDirective(
            role="before_action",
            artifact_types=("screenshot", "dom"),
        ),
        after=CaptureDirective(
            role="after_action",
            artifact_types=("screenshot", "dom", "action_trace"),
        ),
    )


def _policy(**overrides) -> JourneyExecutionPolicy:
    values = {
        "capture_profile": _capture_profile(),
        "allowed_operations": tuple(InteractionOperation),
        "allowed_risk_levels": tuple(InteractionRiskLevel),
    }
    values.update(overrides)
    return JourneyExecutionPolicy(**values)


def _plan(steps):
    definition = parse_journey_definition(
        {
            "schema_version": "journey-dsl.v1",
            "definition_id": "journey-runtime-test",
            "version": 1,
            "name": "Journey 运行时测试",
            "surface_kinds": ["web"],
            "steps": steps,
        }
    )
    return build_journey_execution_plan(definition, task_id="task-1")


def _spec(plan) -> JourneyExecutionSpec:
    execution_id = build_journey_execution_id(
        task_id=plan.task_id,
        execution_plan_id=plan.execution_plan_id,
        session_id="session-1",
        execution_epoch=0,
    )
    return JourneyExecutionSpec(
        execution_id=execution_id,
        execution_plan_id=plan.execution_plan_id,
        task_id=plan.task_id,
        session_id="session-1",
        agent_run_id="run-1",
        surface_kind=SurfaceKind.WEB,
        execution_epoch=0,
        source_capture_id="capture-source",
        deadline=NOW + timedelta(hours=1),
    )


def _router(handler) -> JourneyStepRuntimeRouter:
    return JourneyStepRuntimeRouter(handlers={kind: handler for kind in JourneyStepKind})


def _action_result(*, intent_id: str) -> CaptureCoordinationResult:
    before = CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-before",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id="capture-source",
    )
    after = CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-after",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id="capture-before",
        action_execution_id="action-1",
    )
    interaction = InteractionResult(
        task_id="task-1",
        session_id="session-1",
        intent_id=intent_id,
        action_execution_id="action-1",
        source_capture_id="capture-before",
        status=InteractionResultStatus.SUCCEEDED,
        result_capture_id="capture-after",
    )
    return CaptureCoordinationResult(
        before_capture=before,
        interaction=interaction,
        after_capture=after,
    )


async def test_runner_retries_only_before_action_and_reuses_action_identity() -> None:
    plan = _plan(
        [
            {
                "id": "open-login",
                "type": "action",
                "operation": "click",
                "semantic_target": "登录按钮",
                "retry": {
                    "max_attempts": 2,
                    "retry_on": ["before_capture_failed"],
                },
            }
        ]
    )
    before_failure = CaptureCoordinationError(
        stage=CaptureCoordinationStage.BEFORE_CAPTURE,
        error_code="before_capture_failed",
        message="动作前采集失败",
    )
    spec = _spec(plan)
    identity_hash = stable_hash({"execution_id": spec.execution_id, "step_id": "open-login"})
    coordinator = FakeCoordinator(
        [before_failure, _action_result(intent_id=f"journey-intent:{identity_hash}")]
    )
    repository = FakeJourneyExecutionRepository()
    handler = JourneyActionStepHandler(coordinator=coordinator)
    runner = JourneyRunner(
        repository=repository,
        router=_router(handler),
        clock=lambda: NOW,
        sleep=lambda _: _completed_sleep(),
    )

    state = await runner.run(plan=plan, spec=spec, policy=_policy())

    assert state.status is JourneyExecutionStatus.SUCCEEDED
    assert len(repository.commands) == 2
    first_intent = coordinator.commands[0].intent
    second_intent = coordinator.commands[1].intent
    assert first_intent.intent_id == second_intent.intent_id
    assert first_intent.idempotency_key == second_intent.idempotency_key
    assert repository.commands[0].next_step_attempt_count == 1
    assert repository.commands[1].current_capture_id == "capture-after"


async def test_runner_does_not_replay_after_action_started() -> None:
    plan = _plan(
        [
            {
                "id": "submit",
                "type": "action",
                "operation": "click",
                "semantic_target": "提交按钮",
                "retry": {"max_attempts": 3},
            }
        ]
    )
    coordinator = FakeCoordinator(
        [
            CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code="interaction_failed",
                message="动作状态未知",
            )
        ]
    )
    repository = FakeJourneyExecutionRepository()
    runner = JourneyRunner(
        repository=repository,
        router=_router(JourneyActionStepHandler(coordinator=coordinator)),
        clock=lambda: NOW,
    )

    state = await runner.run(plan=plan, spec=_spec(plan), policy=_policy())

    assert state.status is JourneyExecutionStatus.FAILED
    assert state.error_code == "interaction_failed"
    assert len(coordinator.commands) == 1


async def test_optional_failure_is_audited_as_gap_and_plan_continues() -> None:
    plan = _plan(
        [
            {
                "id": "optional-wait",
                "type": "wait",
                "condition": "surface_stable",
                "required": False,
            },
            {
                "id": "final-assert",
                "type": "assert",
                "assertion": "route_matches",
                "expected": "/home",
            },
        ]
    )
    handler = SequenceHandler(
        [
            JourneyStepExecutionError(
                error_code="WAIT_CONDITION_NOT_MET",
                message="条件未满足",
                retryable=False,
            ),
            JourneyStepExecutionResult(),
        ]
    )
    repository = FakeJourneyExecutionRepository()
    state = await JourneyRunner(
        repository=repository,
        router=_router(handler),
        clock=lambda: NOW,
    ).run(plan=plan, spec=_spec(plan), policy=_policy())

    assert state.status is JourneyExecutionStatus.COMPLETED_WITH_GAPS
    assert state.gap_count == 1
    assert repository.commands[0].attempt.status.value == "skipped"
    assert repository.commands[1].target_status.value == "completed_with_gaps"


async def test_high_risk_action_requires_explicit_step_approval() -> None:
    plan = _plan(
        [
            {
                "id": "dangerous-action",
                "type": "action",
                "operation": "click",
                "semantic_target": "删除按钮",
            }
        ]
    )
    coordinator = FakeCoordinator([])
    state = await JourneyRunner(
        repository=FakeJourneyExecutionRepository(),
        router=_router(JourneyActionStepHandler(coordinator=coordinator)),
        clock=lambda: NOW,
    ).run(
        plan=plan,
        spec=_spec(plan),
        policy=_policy(
            risk_by_step={"dangerous-action": InteractionRiskLevel.HIGH},
        ),
    )

    assert state.status is JourneyExecutionStatus.FAILED
    assert state.error_code == "JOURNEY_HIGH_RISK_APPROVAL_REQUIRED"
    assert coordinator.commands == []


async def test_execution_spec_round_trips_persisted_payload() -> None:
    plan = _plan(
        [
            {
                "id": "observe",
                "type": "capture",
                "role": "checkpoint",
                "artifact_types": ["screenshot"],
            }
        ]
    )
    spec = _spec(plan)
    assert JourneyExecutionSpec.from_dict(spec.to_dict()) == spec


async def _completed_sleep() -> None:
    return None
