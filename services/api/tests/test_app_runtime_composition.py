"""App Worker 生产组合根的路由与执行测试。"""

import asyncio
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.models import ExecutionOutcome, TerminationReason
from services.api.app.application.app_exploration_policy import (
    build_dynamic_app_exploration_runtime_policy,
)
from services.api.app.application.app_exploration_runtime_recovery import (
    AppExplorationWorkRecovery,
    build_app_exploration_runtime_checkpoint,
)
from services.api.app.application.app_exploration_target import (
    build_app_exploration_target_snapshot,
)
from services.api.app.application.app_exploration_turn import AppExplorationTurnOutcome
from services.api.app.application.app_inspection_publisher import (
    PostgresCapturePresentationReader,
)
from services.api.app.application.app_metrics import (
    APP_EXPLORATION_STALE_SESSION_TOTAL,
    app_metric_value,
)
from services.api.app.application.app_runtime_composition import (
    PostgresAppRuntimeProvider,
    ProductionAppV2RuntimeExecutor,
    QueueAppWorkflowCancellation,
    _app_target,
    _AppActionBudgetGuard,
    build_app_workflow_handler,
)
from services.api.app.application.app_workflow_dispatch import RuntimeBackedAppWorkflowHandler
from services.api.app.application.dynamic_app_exploration import (
    build_dynamic_app_exploration_objective,
)
from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionService,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.surface_phase_explorer import SurfacePhaseAwareExplorer
from services.api.app.core.config import Settings
from services.api.app.domain.evidence import (
    AppExplorationResult,
    AppExplorationResultOutcome,
)
from services.api.app.domain.exploration import (
    AppExplorationOutcome,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_app_exploration_goal,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    AppBootstrapStage,
    AppReadyHandoff,
    AppWorkflowFailure,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    build_android_screenshot_only_manifest,
)
from services.api.app.infrastructure.db.app_frontier_repository import (
    PostgresAppFrontierRepository,
)
from services.api.app.infrastructure.db.app_targeted_recapture_context_repository import (
    PostgresAppTargetedRecaptureContextReader,
)
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.app.infrastructure.db.surface_phase_repository import (
    PostgresSurfacePhaseRepository,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


@pytest.mark.asyncio
async def test_app_cancellation_uses_durable_marker_when_redis_signal_is_missing() -> None:
    class Queue:
        async def is_cancel_requested(self, run_id: str) -> bool:
            assert run_id == "run-app"
            return False

    class Durable:
        async def is_cancel_requested(self, run_id: str) -> bool:
            assert run_id == "run-app"
            return True

    cancellation = QueueAppWorkflowCancellation(Queue(), durable_source=Durable())

    assert (
        await cancellation.is_cancel_requested(task_id="task-app", agent_run_id="run-app") is True
    )


def _app_record(
    *,
    package_name: str | None = "com.example.target",
    scan_mode: str = "full",
    max_depth: int = 4,
    max_duration_seconds: int = 3_600,
    max_steps: int | None = None,
    max_actions: int | None = None,
    page_sampling_policy: dict | None = None,
) -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-app",
        task_id="task-app",
        message="检查登录流程",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "scan_mode": scan_mode,
            "max_depth": max_depth,
            "journey_steps": [],
            "_task_target": {
                "platform": "app",
                "target_url": None,
                "app": {
                    "platform_build_ref": "app-build:build-1",
                    "package_name": package_name,
                    "device": {"backend": "cloud_phone", "profile_id": "pixel-8"},
                },
            },
        },
        page_sampling_policy=dict(page_sampling_policy or {}),
        loop_guard={
            "max_depth": max_depth,
            "max_duration_seconds": max_duration_seconds,
            "max_steps": max_steps,
            "max_actions": max_actions,
        },
    )


def test_production_executor_does_not_contain_probe_drain() -> None:
    """App 正常探索路径不得领取 Probe；真正补采由独立门禁控制。"""

    source = inspect.getsource(ProductionAppV2RuntimeExecutor.run)

    assert "ExplorerWorkType.PROBE" not in source
    assert 'work_type="probe"' not in source


def test_production_executor_uses_package_independent_dynamic_frontier() -> None:
    source = inspect.getsource(ProductionAppV2RuntimeExecutor.run)

    assert "build_dynamic_app_exploration_runtime_policy" in source
    assert "build_dynamic_app_exploration_objective" in source
    assert "resolve_app_capture_playbook" not in source
    assert "build_playbook_objective" not in source
    assert "build_app_exploration_runtime_policy" not in source
    assert "apk_filename" not in source


def _budget_outcome(*, kind: str) -> AppExplorationTurnOutcome:
    if kind == "terminal":
        turn = SimpleNamespace(is_terminal=True, coordination=None, observation_capture=None)
    elif kind == "observation":
        turn = SimpleNamespace(
            is_terminal=False,
            coordination=None,
            observation_capture=object(),
        )
    else:
        turn = SimpleNamespace(
            is_terminal=False,
            coordination=object(),
            observation_capture=None,
        )
    return AppExplorationTurnOutcome(
        claimed=True,
        final_status=ExplorerWorkStatus.SUCCEEDED,
        turn=turn,
    )


@pytest.mark.parametrize("max_actions", (0, 1, 3))
def test_action_budget_allows_exact_actions_then_one_terminal_confirmation(
    max_actions: int,
) -> None:
    budget = _AppActionBudgetGuard(max_actions=max_actions)

    if max_actions > 0:
        budget.record(_budget_outcome(kind="observation"))
        assert budget.consumed_actions == 0
        assert budget.can_claim is True
    for expected in range(1, max_actions + 1):
        budget.record(_budget_outcome(kind="action"))
        assert budget.consumed_actions == expected

    assert budget.can_claim is True
    budget.record(_budget_outcome(kind="terminal"))

    assert budget.consumed_actions == max_actions
    assert budget.terminal_confirmation_claimed is True
    assert budget.can_claim is False


def test_action_budget_rejects_action_during_terminal_confirmation() -> None:
    budget = _AppActionBudgetGuard(max_actions=1)
    budget.record(_budget_outcome(kind="action"))

    with pytest.raises(RuntimeError, match="超出已锁定的动作预算"):
        budget.record(_budget_outcome(kind="action"))


@pytest.mark.asyncio
async def test_postgres_provider_uses_locked_app_target_and_runtime_injection(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _runtime_builder(**kwargs):
        captured.update(kwargs)
        return "runtime"

    provider = PostgresAppRuntimeProvider(
        worker_instance_id="worker-1",
        runtime_builder=_runtime_builder,
    )
    monkeypatch.setattr(
        provider,
        "_build_dependencies",
        lambda settings, **kwargs: "dependencies",
    )
    settings = Settings.model_validate(
        {"workflow": {"app_qc_enabled": True, "app_runtime_provider": "fake"}}
    )

    runtime = await provider.build(record=_app_record(), settings=settings)

    assert runtime == "runtime"
    assert captured["provider"] == "fake"
    assert captured["lease_owner"] == "worker-1"
    assert captured["dependencies"] == "dependencies"


def test_postgres_provider_wires_surface_phase_aware_explorer_by_default() -> None:
    provider = PostgresAppRuntimeProvider(
        worker_instance_id="worker-1",
        understanding_provider=object(),
    )
    dependencies = provider._build_dependencies(
        Settings.model_validate(
            {
                "environment": "test",
                "workflow": {
                    "app_runtime_provider": "fake",
                    "app_vision_provider": "fake",
                },
                "evidence_storage": {"backend": "local"},
            }
        ),
        target=_app_target(_app_record()),
    )

    assert isinstance(dependencies.explorer, SurfacePhaseAwareExplorer)
    assert isinstance(
        dependencies.explorer._repository,
        PostgresSurfacePhaseRepository,
    )
    assert dependencies.explorer._session_command_gate is dependencies.session_command_gate
    assert dependencies.allowed_packages == ()
    assert isinstance(dependencies.app_frontier_queue, PostgresAppFrontierRepository)
    assert isinstance(dependencies.page_graph, PostgresPageGraphRepository)
    assert isinstance(
        dependencies.restoration_attempts,
        PostgresRestorationAttemptRepository,
    )
    assert isinstance(
        dependencies.targeted_recapture_contexts,
        PostgresAppTargetedRecaptureContextReader,
    )
    assert dependencies.probe_orchestrator is dependencies.exploration_orchestrator
    result_writer = dependencies.active_session_inspection._consumer._result_writer
    assert isinstance(result_writer._request_admission, EvidenceRequestAdmissionService)
    live_publisher = dependencies.active_session_inspection._publisher._publisher
    assert isinstance(live_publisher._presentation_reader, PostgresCapturePresentationReader)

    maf_workflow = dependencies.exploration_orchestrator._workflow
    assert maf_workflow._exploration_publisher._emit_wakeup is True
    assert maf_workflow._probe_publisher._emit_wakeup is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_error", "expected_reason", "expected_outcome"),
    [
        (None, TerminationReason.COMPLETED, ExecutionOutcome.SUCCESS),
        (
            "device close failed",
            TerminationReason.INCOMPLETE,
            ExecutionOutcome.NEEDS_REVIEW,
        ),
    ],
)
@pytest.mark.parametrize("target_package", [None, "com.example.stale"])
@pytest.mark.parametrize(
    ("max_duration_seconds", "max_actions", "expected_max_actions"),
    [(3_600, None, 72), (7_200, 100, 100)],
)
async def test_executor_runs_bootstrap_publish_and_one_app_turn(
    close_error: str | None,
    expected_reason: TerminationReason,
    expected_outcome: ExecutionOutcome,
    target_package: str | None,
    max_duration_seconds: int,
    max_actions: int | None,
    expected_max_actions: int,
) -> None:
    calls: list[object] = []
    locked_target = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name="com.example.installed",
        required_page_types=("home", "content_detail"),
        optional_page_types=("profile",),
        lifecycle_page_types=("line_loading",),
        required_milestones=("surface:stable",),
        minimum_business_pages=2,
        applicability_mode="explicit_targets",
    )

    class Runtime:
        async def bootstrap(self, command):
            calls.append(command)
            return AppReadyHandoff(
                task_id="task-app",
                agent_run_id="run-app",
                session_id="app:task-app",
                capture=CaptureRef(
                    task_id="task-app",
                    session_id="app:task-app",
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            calls.append(command)

        async def inspect_current_app_surface(self, *, task_id, agent_run_id, deadline):
            calls.append(("inspection-checkpoint", task_id, agent_run_id, deadline))

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None and str(getattr(work_type, "value", work_type)) == "probe":
                return AppExplorationTurnOutcome(claimed=False)
            calls.append((task_id, agent_run_id))
            return AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id="work-app",
                        task_id="task-app",
                        status=ExplorationTerminalStatus.COMPLETED,
                        reason="当前分支探索完成",
                        outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                        reason_code="exploration_completed",
                    ),
                ),
            )

        async def cancel_exploration_work(self, *, task_id):
            calls.append(("cancel", task_id))

        async def close_exploration(self, command):
            calls.append(("close", command))
            return SimpleNamespace(close_error=close_error)

        async def mark_exploration_lost(self, **kwargs):
            calls.append(("lost", kwargs))

    record = _app_record(
        package_name=target_package,
        max_duration_seconds=max_duration_seconds,
        max_actions=max_actions,
    )
    record.task_config["app_exploration_target"] = locked_target.to_dict()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        Runtime(),
        record=record,
        settings=Settings(),
    )

    bootstrap_command, exploration_command, checkpoint, turn, cancellation, closure = calls
    assert bootstrap_command.session_id == "app:task-app"
    assert bootstrap_command.device_ref == "cloud_phone:pixel-8"
    assert exploration_command.handoff.capture.capture_id == "capture-app"
    assert exploration_command.app_goal.allowed_packages == ("com.example.installed",)
    assert exploration_command.app_goal.minimum_business_pages == 2
    assert exploration_command.app_goal.optional_page_types == ("profile",)
    assert exploration_command.app_goal.lifecycle_page_types == ("line_loading",)
    assert exploration_command.app_goal.applicability_mode == "explicit_targets"
    assert exploration_command.boundaries.allowed_hosts == ("com.example.installed",)
    assert exploration_command.boundaries.max_navigation_depth == 4
    assert exploration_command.budget.max_actions == expected_max_actions
    assert exploration_command.budget.max_depth == 4
    assert exploration_command.budget.remaining_duration_ms == max_duration_seconds * 1_000
    assert exploration_command.runtime_policy_summary["scan_mode"] == "full"
    assert exploration_command.runtime_policy_summary["budget_source"] == ("agent_run.loop_guard")
    assert exploration_command.runtime_policy_summary["exploration_mode"] == "dynamic_frontier"
    assert exploration_command.runtime_policy_summary["playbook_id"] is None
    assert exploration_command.curriculum_progress is None
    assert checkpoint == (
        "inspection-checkpoint", "task-app", "run-app", exploration_command.deadline
    )
    assert turn == ("task-app", "run-app")
    assert cancellation == ("cancel", "task-app")
    assert closure[0] == "close"
    assert closure[1].outcome.value == "exploration_completed"
    assert closure[1].coverage_summary["app_runtime_policy"]["scan_mode"] == "full"
    assert closure[1].coverage_summary["app_runtime_policy"]["max_actions"] == expected_max_actions
    assert closure[1].supplemental_evidence_budget is not None
    assert closure[1].supplemental_evidence_budget.remaining_actions == expected_max_actions
    assert closure[1].supplemental_evidence_budget.max_depth == 4
    assert result.termination_reason is expected_reason
    assert result.execution_outcome is expected_outcome
    if close_error is not None:
        assert "Session 关闭失败" in str(result.error_message)


@pytest.mark.asyncio
async def test_executor_rejects_app_ready_package_outside_locked_target() -> None:
    class Runtime:
        published = False
        lost_calls = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.other",
            )

        async def publish_exploration(self, command):
            del command
            self.published = True

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    locked_target = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name="com.example.target",
    )
    record = _app_record(package_name="com.example.target")
    record.task_config["app_exploration_target"] = locked_target.to_dict()
    runtime = Runtime()

    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=record,
        settings=Settings(),
    )

    assert runtime.published is False
    assert runtime.lost_calls[0]["reason"] == "AppExplorationTargetError"
    assert result.termination_reason is TerminationReason.ERROR
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert "实际安装包不属于锁定目标包集合" in str(result.error_message)


@pytest.mark.asyncio
async def test_executor_rejects_invalid_locked_policy_and_records_stable_lost_reason() -> None:
    class Runtime:
        published = False
        lost_calls = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command
            self.published = True

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    record = _app_record()
    record.task_config["scan_mode"] = "mystery"
    runtime = Runtime()

    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=record,
        settings=Settings(),
    )

    assert runtime.published is False
    assert runtime.lost_calls == [
        {
            "task_id": "task-app",
            "session_id": "app:task-app",
            "reason": "APP_EXPLORATION_POLICY_INVALID",
            "expected_surface_kind": SurfaceKind.ANDROID,
            "expected_build_id": "app-build:build-1",
        }
    ]
    assert result.termination_reason is TerminationReason.ERROR
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert "scan_mode" in str(result.error_message)


@pytest.mark.asyncio
async def test_executor_cancellation_after_app_ready_seals_insufficient_without_claim() -> None:
    class Runtime:
        published = False
        turn_calls = 0
        close_commands = []
        lost_calls = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            self.published = True
            del command

        async def is_cancel_requested(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            return True

        async def run_one_turn(self, **kwargs):
            del kwargs
            self.turn_calls += 1
            raise AssertionError("APP_READY 后已取消时不得再 claim Explorer Work")

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    # APP_READY 后已取消时不得发布新的 Exploration Work。
    assert runtime.published is False
    assert runtime.turn_calls == 0
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.outcome.value == "evidence_insufficient"
    assert close_command.capture_final_observation is False
    assert close_command.supplemental_evidence_budget is None
    assert runtime.lost_calls == []
    assert result.termination_reason is TerminationReason.CANCELED
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_source", ("claimed_turn", "unclaimed_turn", "after_completed_turn")
)
async def test_executor_canceled_turn_closes_before_next_inspection(cancel_source: str) -> None:
    """单轮取消或结束后到达的取消须立即收口，不再检查下一帧。"""

    class Runtime:
        def __init__(self) -> None:
            self.cancel_requested = False
            self.publish_calls = 0
            self.inspection_calls = 0
            self.turn_calls = 0
            self.cancel_calls = []
            self.close_commands = []
            self.lost_calls = []

        async def bootstrap(self, command):
            del command
            return _recovery_handoff()

        async def publish_exploration(self, command):
            del command
            self.publish_calls += 1

        async def is_cancel_requested(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            return self.cancel_requested

        async def inspect_current_app_surface(self, **kwargs):
            del kwargs
            self.inspection_calls += 1

        async def run_one_turn(self, **kwargs):
            del kwargs
            self.turn_calls += 1
            assert self.turn_calls == 1, "取消后不得再领取下一轮"
            if cancel_source == "after_completed_turn":
                self.cancel_requested = True
                return AppExplorationTurnOutcome(
                    claimed=True,
                    final_status=ExplorerWorkStatus.SUCCEEDED,
                    turn=SimpleNamespace(
                        is_terminal=True,
                        coordination=None,
                        observation_capture=None,
                        decision=ExplorationTerminal(
                            work_id="work-app",
                            task_id="task-app",
                            status=ExplorationTerminalStatus.COMPLETED,
                            reason="当前分支探索完成",
                            outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                            reason_code="exploration_completed",
                        ),
                    ),
                )
            return AppExplorationTurnOutcome(
                claimed=cancel_source == "claimed_turn",
                final_status=ExplorerWorkStatus.CANCELLED,
                error_code="WORKFLOW_CANCELED",
            )

        async def cancel_workflow(self, *, task_id):
            self.cancel_calls.append(task_id)

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert runtime.publish_calls == 1
    assert runtime.inspection_calls == 1
    assert runtime.turn_calls == 1
    assert runtime.cancel_calls == ["task-app"]
    assert runtime.lost_calls == []
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.outcome.value == "evidence_insufficient"
    assert close_command.terminal_reason_code == "workflow_canceled"
    assert close_command.coverage_summary["terminal_status"] == "cancelled"
    assert close_command.capture_final_observation is False
    assert close_command.supplemental_evidence_budget is None
    assert result.termination_reason is TerminationReason.CANCELED
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_executor_cancellation_seal_failure_marks_session_lost() -> None:
    class Runtime:
        close_commands = []
        lost_calls = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def is_cancel_requested(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            return True

        async def run_one_turn(self, **kwargs):
            del kwargs
            raise AssertionError("取消后不得领取 Explorer Work")

        async def close_exploration(self, command):
            self.close_commands.append(command)
            raise RuntimeError("evidence seal unavailable")

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert len(runtime.close_commands) == 1
    assert runtime.close_commands[0].outcome.value == "evidence_insufficient"
    assert runtime.close_commands[0].capture_final_observation is False
    assert runtime.lost_calls


@pytest.mark.asyncio
async def test_executor_terminal_closure_failure_marks_session_lost() -> None:
    class Runtime:
        lost_calls = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def is_cancel_requested(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            return False

        async def run_one_turn(self, **kwargs):
            del kwargs
            return AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id="work-terminal",
                        task_id="task-app",
                        status=ExplorationTerminalStatus.COMPLETED,
                        reason="探索目标已完成",
                        outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                        reason_code="exploration_completed",
                    ),
                ),
            )

        async def close_exploration(self, command):
            del command
            raise RuntimeError("closure unavailable")

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert result.termination_reason is TerminationReason.ERROR
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert runtime.lost_calls == [
        {
            "task_id": "task-app",
            "session_id": "app:task-app",
            "reason": "RuntimeError",
            "expected_surface_kind": SurfaceKind.ANDROID,
            "expected_build_id": "app-build:build-1",
        }
    ]


@pytest.mark.asyncio
async def test_executor_cancellation_marks_session_lost_before_reraising() -> None:
    started = asyncio.Event()

    class Runtime:
        lost_calls = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def is_cancel_requested(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            return False

        async def run_one_turn(self, **kwargs):
            del kwargs
            started.set()
            await asyncio.Event().wait()

        async def close_exploration(self, command):
            raise AssertionError(f"取消前不得进入 Closure：{command}")

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    task = asyncio.create_task(
        ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
            runtime,
            record=_app_record(),
            settings=Settings(),
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.lost_calls == [
        {
            "task_id": "task-app",
            "session_id": "app:task-app",
            "reason": "runtime_cancelled",
            "expected_surface_kind": SurfaceKind.ANDROID,
            "expected_build_id": "app-build:build-1",
        }
    ]


@pytest.mark.asyncio
async def test_executor_keeps_consuming_until_a_later_turn_completes() -> None:
    outcomes = [
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            turn=SimpleNamespace(is_terminal=False),
            successor_work_id="work-successor",
        ),
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            turn=SimpleNamespace(
                is_terminal=True,
                decision=ExplorationTerminal(
                    work_id="work-terminal",
                    task_id="task-app",
                    status=ExplorationTerminalStatus.COMPLETED,
                    reason="探索目标已完成",
                    outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                    reason_code="exploration_completed",
                ),
            ),
        ),
    ]
    turn_calls = 0

    class Runtime:
        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None and str(getattr(work_type, "value", work_type)) == "probe":
                return AppExplorationTurnOutcome(claimed=False)
            nonlocal turn_calls
            assert task_id == "task-app"
            assert agent_run_id == "run-app"
            turn_calls += 1
            return outcomes.pop(0)

        async def close_exploration(self, command):
            return SimpleNamespace(close_error=None, command=command)

        async def mark_exploration_lost(self, **kwargs):
            del kwargs

    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )

    assert turn_calls == 2
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert result.working_memory.step_count == 2


@pytest.mark.asyncio
async def test_executor_preserves_closure_evidence_insufficient_for_review() -> None:
    outcome = AppExplorationTurnOutcome(
        claimed=True,
        final_status=ExplorerWorkStatus.SUCCEEDED,
        turn=SimpleNamespace(
            is_terminal=True,
            decision=ExplorationTerminal(
                work_id="work-terminal",
                task_id="task-app",
                status=ExplorationTerminalStatus.COMPLETED,
                reason="探索目标已完成",
                outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                reason_code="exploration_completed",
            ),
        ),
    )

    class Runtime:
        lost_calls: list[dict] = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            del task_id, agent_run_id, work_type
            return outcome

        async def close_exploration(self, command):
            del command
            return SimpleNamespace(
                close_error=None,
                exploration_result=SimpleNamespace(
                    outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                    terminal_reason_code="evidence_incomplete",
                ),
            )

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == "evidence_incomplete"
    assert runtime.lost_calls == []


@pytest.mark.asyncio
async def test_executor_does_not_use_process_local_retry_wait_reclaim_cap() -> None:
    """retry_wait 由 PG 的 next_attempt_at 决定，不得以本进程固定次数终止 Run。"""

    outcomes = [
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.RETRY_WAIT,
            error_code="DEVICE_BUSY",
        ),
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.RETRY_WAIT,
            error_code="DEVICE_BUSY",
        ),
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            turn=SimpleNamespace(
                is_terminal=True,
                decision=ExplorationTerminal(
                    work_id="work-terminal",
                    task_id="task-app",
                    status=ExplorationTerminalStatus.COMPLETED,
                    reason="重试后完成",
                    outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                    reason_code="exploration_completed",
                ),
            ),
        ),
    ]

    class Runtime:
        turn_calls = 0
        next_claim_at_calls = 0

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def next_exploration_claim_at(self, *, task_id):
            assert task_id == "task-app"
            self.next_claim_at_calls += 1
            return datetime.now(UTC)

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            del task_id, agent_run_id, work_type
            self.turn_calls += 1
            return outcomes.pop(0)

        async def close_exploration(self, command):
            return SimpleNamespace(close_error=None, command=command)

        async def mark_exploration_lost(self, **kwargs):
            del kwargs

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
    ).run(runtime, record=_app_record(), settings=Settings())

    assert runtime.turn_calls == 3
    assert runtime.next_claim_at_calls == 2
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_executor_empty_queue_after_nonterminal_turn_is_not_success() -> None:
    outcomes = [
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            turn=SimpleNamespace(is_terminal=False),
            successor_work_id="work-successor",
        ),
        AppExplorationTurnOutcome(claimed=False),
    ]
    turn_calls = 0

    class Runtime:
        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None and str(getattr(work_type, "value", work_type)) == "probe":
                return AppExplorationTurnOutcome(claimed=False)
            nonlocal turn_calls
            del task_id, agent_run_id
            turn_calls += 1
            return outcomes.pop(0)

        async def close_exploration(self, command):
            raise AssertionError(f"非终态不得关闭：{command}")

        async def mark_exploration_lost(self, **kwargs):
            del kwargs

    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )

    assert turn_calls == 2
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.working_memory.step_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_reason"),
    [
        (
            AppExplorationTurnOutcome(claimed=False),
            TerminationReason.INCOMPLETE,
        ),
        (
            AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(is_terminal=False),
            ),
            TerminationReason.INCOMPLETE,
        ),
        (
            AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id="legacy-work",
                        task_id="task-app",
                        status=ExplorationTerminalStatus.COMPLETED,
                        reason="未经领域评估的旧完成信号",
                    ),
                ),
            ),
            TerminationReason.INCOMPLETE,
        ),
        (
            AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.BLOCKED,
                turn=SimpleNamespace(is_terminal=True),
            ),
            TerminationReason.INCOMPLETE,
        ),
        (
            AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id="insufficient-work",
                        task_id="task-app",
                        status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
                        reason="最低覆盖不足",
                        outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value,
                        reason_code="minimum_coverage_not_met",
                    ),
                ),
            ),
            TerminationReason.INCOMPLETE,
        ),
        (
            AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.BLOCKED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id="technical-work",
                        task_id="task-app",
                        status=ExplorationTerminalStatus.BLOCKED,
                        reason="Session 租约丢失",
                        outcome=AppExplorationOutcome.TECHNICAL_FAILED.value,
                        reason_code="SESSION_LEASE_LOST",
                    ),
                ),
            ),
            TerminationReason.INCOMPLETE,
        ),
        (
            AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.RETRY_WAIT,
                error_code="DEVICE_BUSY",
            ),
            TerminationReason.INCOMPLETE,
        ),
        (
            AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.FAILED,
                error_code="APP_DRIVER_FAILED",
            ),
            TerminationReason.INCOMPLETE,
        ),
    ],
)
async def test_executor_never_maps_noncompleted_turns_to_success(
    outcome: AppExplorationTurnOutcome,
    expected_reason: TerminationReason,
) -> None:
    class Runtime:
        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def next_exploration_claim_at(self, *, task_id):
            del task_id
            return None

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None and str(getattr(work_type, "value", work_type)) == "probe":
                return AppExplorationTurnOutcome(claimed=False)
            del task_id, agent_run_id
            return outcome

        async def close_exploration(self, command):
            return SimpleNamespace(close_error=None, command=command)

        async def mark_exploration_lost(self, **kwargs):
            del kwargs

    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
    ).run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )

    assert result.termination_reason is expected_reason
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_executor_seals_missing_app_after_capture_as_evidence_insufficient() -> None:
    class Runtime:
        close_commands = []
        canceled_tasks = []
        lost_calls = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            del task_id, agent_run_id, work_type
            return AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.FAILED,
                error_code="APP_ACTION_AFTER_CAPTURE_MISSING",
            )

        async def cancel_exploration_work(self, *, task_id):
            self.canceled_tasks.append(task_id)

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert runtime.canceled_tasks == ["task-app"]
    assert runtime.lost_calls == []
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert close_command.terminal_reason_code == "APP_ACTION_AFTER_CAPTURE_MISSING"
    assert close_command.run_evidence_gap_reason_code == "APP_ACTION_AFTER_CAPTURE_MISSING"
    assert close_command.coverage_summary["action_recovery_blocked"] is True
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    (
        "APP_AFTER_CAPTURE_MISSING_AFTER_CRASH",
        "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
    ),
)
async def test_executor_continues_after_crash_instead_of_sealing_session(
    error_code: str,
) -> None:
    """Worker 崩溃后不得封存整单；必须再观察前台并继续课纲。"""

    outcomes = [
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.FAILED,
            error_code=error_code,
        ),
        AppExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            turn=SimpleNamespace(
                is_terminal=True,
                decision=ExplorationTerminal(
                    work_id="work-terminal",
                    task_id="task-app",
                    status=ExplorationTerminalStatus.COMPLETED,
                    reason="探索目标已完成",
                    outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                    reason_code="exploration_completed",
                ),
            ),
        ),
    ]
    turn_calls = 0

    class Runtime:
        close_commands = []
        canceled_tasks = []

        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                capture=CaptureRef(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None and str(getattr(work_type, "value", work_type)) == "probe":
                return AppExplorationTurnOutcome(claimed=False)
            del task_id, agent_run_id
            nonlocal turn_calls
            turn_calls += 1
            return outcomes.pop(0)

        async def cancel_exploration_work(self, *, task_id):
            self.canceled_tasks.append(task_id)

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def mark_exploration_lost(self, **kwargs):
            del kwargs

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert turn_calls == 2
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert not any(
        getattr(command, "coverage_summary", {}).get("action_recovery_blocked") is True
        for command in runtime.close_commands
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("bootstrap", "turn"))
async def test_executor_marks_infrastructure_failure_for_review(stage: str) -> None:
    class Runtime:
        lost_calls = []
        close_commands = []
        canceled_tasks = []

        async def bootstrap(self, command):
            del command
            if stage == "bootstrap":
                raise RuntimeError("Official API unavailable")
            return AppReadyHandoff(
                task_id="task-app",
                agent_run_id="run-app",
                session_id="app:task-app",
                capture=CaptureRef(
                    task_id="task-app",
                    session_id="app:task-app",
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None and str(getattr(work_type, "value", work_type)) == "probe":
                return AppExplorationTurnOutcome(claimed=False)
            del task_id, agent_run_id
            raise RuntimeError("Explorer queue unavailable")

        async def close_exploration(self, command):
            if stage == "bootstrap":
                raise AssertionError(f"Bootstrap 失败还没有探索时间线：{command}")
            self.close_commands.append(command)
            return SimpleNamespace(
                close_error=None,
                exploration_result=SimpleNamespace(
                    outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                    evidence_gaps=(),
                ),
                index=SimpleNamespace(index_id="index-technical"),
            )

        async def cancel_exploration_work(self, *, task_id):
            self.canceled_tasks.append(task_id)

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )

    assert result.termination_reason is (
        TerminationReason.ERROR if stage == "bootstrap" else TerminationReason.INCOMPLETE
    )
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    if stage == "bootstrap":
        assert Runtime.lost_calls
        assert Runtime.close_commands == []
    else:
        assert Runtime.lost_calls == []
        assert Runtime.canceled_tasks == ["task-app"]
        assert len(Runtime.close_commands) == 1
        close_command = Runtime.close_commands[0]
        assert close_command.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
        assert close_command.terminal_reason_code == "RuntimeError"
        assert close_command.run_evidence_gap_reason_code == "RuntimeError"
        assert close_command.coverage_summary["technical_failure"] is True
        assert result.evidence_index_id == "index-technical"


@pytest.mark.asyncio
async def test_executor_emits_exploration_turn_progress_logs() -> None:
    """App Executor 每轮探索必须旁路通知过程观察者。"""
    events: list[tuple[str, dict[str, object]]] = []
    turn_work_types = []

    class Recorder:
        def on_app_exploration_published(self, **kwargs):
            events.append(("published", dict(kwargs)))

        def on_app_exploration_turn_started(self, **kwargs):
            events.append(("started", dict(kwargs)))

        def on_app_exploration_turn_finished(self, **kwargs):
            events.append(("finished", dict(kwargs)))

    class Runtime:
        async def bootstrap(self, command):
            return AppReadyHandoff(
                task_id="task-app",
                agent_run_id="run-app",
                session_id="app:task-app",
                capture=CaptureRef(
                    task_id="task-app",
                    session_id="app:task-app",
                    capture_id="capture-app",
                    surface_kind=SurfaceKind.ANDROID,
                    capture_kind=CaptureKind.APP,
                    role="bootstrap",
                    artifact_ids=("artifact-app",),
                ),
                capability_manifest=build_android_screenshot_only_manifest(
                    adapter_id="test",
                    adapter_version="v1",
                    backend="cloud_phone",
                ),
                bootstrap_row_version=1,
                installed_package="com.example.installed",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            del task_id, agent_run_id
            turn_work_types.append(work_type)
            if work_type is not None and str(getattr(work_type, "value", work_type)) == "probe":
                return AppExplorationTurnOutcome(claimed=False)
            return AppExplorationTurnOutcome(
                claimed=True,
                work_id="work-app",
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id="work-app",
                        task_id="task-app",
                        status=ExplorationTerminalStatus.COMPLETED,
                        reason="当前分支探索完成",
                        outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                        reason_code="exploration_completed",
                    ),
                ),
            )

        async def close_exploration(self, command):
            return SimpleNamespace(close_error=None, command=command)

        async def mark_exploration_lost(self, **kwargs):
            del kwargs

    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
        progress_recorder=Recorder(),
    )

    assert result.termination_reason is TerminationReason.COMPLETED
    assert events[0][0] == "published"
    assert events[1] == ("started", {"turn_index": 1, "work_type": "exploration"})
    assert events[2][0] == "finished"
    assert events[2][1]["claimed"] is True
    assert events[2][1]["work_id"] == "work-app"
    assert events[2][1]["terminal_status"] == "completed"
    assert all(payload.get("work_type") != "probe" for _, payload in events)
    assert turn_work_types == [None]


def test_build_handler_returns_runtime_backed_app_only_entry() -> None:
    handler = build_app_workflow_handler(Settings(), "worker-1")

    assert isinstance(handler, RuntimeBackedAppWorkflowHandler)


def test_build_handler_rejects_fake_provider_in_production_at_startup() -> None:
    settings = Settings.model_validate(
        {
            "environment": "production",
            "workflow": {"app_qc_enabled": True, "app_runtime_provider": "fake"},
        }
    )

    with pytest.raises(ValueError, match="禁止启用 Fake App Runtime Provider"):
        build_app_workflow_handler(settings, "worker-1")


def test_build_handler_rejects_enabled_but_unready_supplemental_runtime() -> None:
    settings = Settings.model_validate({"supplemental_evidence": {"enabled": True}})

    with pytest.raises(RuntimeError, match="运行时闭环尚未就绪"):
        build_app_workflow_handler(settings, "worker-1")


def _recovery_handoff() -> AppReadyHandoff:
    """构造与组合根一致的 APP_READY Handoff。"""
    return AppReadyHandoff(
        task_id="task-app",
        agent_run_id="run-app",
        session_id="app:task-app",
        capture=CaptureRef(
            task_id="task-app",
            session_id="app:task-app",
            capture_id="capture-app",
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="bootstrap",
            artifact_ids=("artifact-app",),
        ),
        capability_manifest=build_android_screenshot_only_manifest(
            adapter_id="test",
            adapter_version="v1",
            backend="cloud_phone",
        ),
        bootstrap_row_version=1,
        installed_package="com.example.installed",
    )


def _recovery_policy_and_objective():
    """按执行器真实输入计算检查点所需的冻结策略。"""
    record = _app_record()
    policy = build_dynamic_app_exploration_runtime_policy(
        task_config=record.task_config or {},
        loop_guard=record.loop_guard or {},
        page_sampling_policy=record.page_sampling_policy or {},
        journey_steps=[],
    )
    goal = build_app_exploration_goal(
        package_name="com.example.installed",
        journey_steps=(),
        fallback_milestones=(),
        minimum_business_pages=policy.minimum_business_pages,
    )
    return policy, build_dynamic_app_exploration_objective(
        goal,
        completion_mode=(
            policy.app_target_snapshot.completion_mode
            if policy.app_target_snapshot is not None
            else "explicit_targets"
        ),
    )


def _recovery_checkpoint(*, started_at: datetime, deadline: datetime):
    policy, objective = _recovery_policy_and_objective()
    return (
        build_app_exploration_runtime_checkpoint(
            task_id="task-app",
            agent_run_id="run-app",
            surface_session_id="app:task-app",
            platform_build_ref="app-build:build-1",
            bootstrap_capture_id="capture-app",
            installed_package="com.example.installed",
            runtime_policy=policy.to_dict(),
            objective=objective,
            started_at=started_at,
            deadline=deadline,
            created_at=started_at,
        ),
        policy,
        objective,
    )


def _recovery_work(
    *,
    policy,
    objective: str,
    started_at: datetime,
    deadline: datetime,
    remaining_actions: int,
    remaining_duration_ms: int,
    branch_id: str = "app:capture-app",
    capture_id: str = "capture-app",
):
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier-{capture_id}",
        observation=SurfaceObservation(
            capture_id=capture_id,
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.installed/.MainActivity",
            state_version="sha256:" + "b" * 64,
            observed_at=started_at,
            page_state="stable",
        ),
        candidates=(
            CandidateAction(
                candidate_id="candidate-recovery",
                semantic_target="关键入口",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=90,
                navigation_depth=1,
                expected_surface_kind=SurfaceKind.ANDROID,
            ),
        ),
    )
    return build_exploration_work(
        task_id="task-app",
        session_id="app:task-app",
        branch_id=branch_id,
        objective=objective,
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.installed",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.SCROLL,
                InteractionOperation.BACK,
                InteractionOperation.OBSERVE,
            ),
            max_navigation_depth=policy.max_navigation_depth,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=policy.max_actions,
            remaining_actions=remaining_actions,
            max_depth=policy.max_navigation_depth,
            remaining_duration_ms=remaining_duration_ms,
        ),
        deadline=deadline,
        app_goal=build_app_exploration_goal(
            package_name="com.example.installed",
            journey_steps=[],
            fallback_milestones=policy.active_tabs,
            minimum_business_pages=policy.minimum_business_pages,
        ),
    )


def _persisted_result(*, outcome: AppExplorationResultOutcome) -> AppExplorationResult:
    return AppExplorationResult(
        task_id="task-app",
        run_id="run-app",
        surface_session_id="app:task-app",
        outcome=outcome,
        terminal_reason_code=(
            "exploration_completed"
            if outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED
            else "evidence_insufficient"
        ),
        evidence_index_id="app-evidence-index:recovery",
        evidence_index_hash="sha256:" + "a" * 64,
        coverage_summary={"consumed_turn_count": 2},
        evidence_gaps=(),
        started_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 8, 14, 9, 5, tzinfo=UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_reason", "expected_execution"),
    [
        (
            AppExplorationResultOutcome.EXPLORATION_COMPLETED,
            TerminationReason.COMPLETED,
            ExecutionOutcome.SUCCESS,
        ),
        (
            AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            TerminationReason.INCOMPLETE,
            ExecutionOutcome.NEEDS_REVIEW,
        ),
    ],
)
async def test_executor_restores_sealed_result_without_bootstrap_publish_or_driver(
    outcome: AppExplorationResultOutcome,
    expected_reason: TerminationReason,
    expected_execution: ExecutionOutcome,
) -> None:
    """已有封存 Result 时恢复只读终态，绝不能再次触达设备或 Explorer。"""

    class Runtime:
        async def get_persisted_exploration_result(self, *, agent_run_id):
            assert agent_run_id == "run-app"
            return _persisted_result(outcome=outcome)

        async def bootstrap(self, command):
            raise AssertionError("已有 Result 时不得重新 Bootstrap")

        async def publish_exploration(self, command):
            raise AssertionError("已有 Result 时不得重复发布 Work")

        async def run_one_turn(self, **kwargs):
            raise AssertionError("已有 Result 时不得调用 Explorer/Driver")

    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )

    assert result.termination_reason is expected_reason
    assert result.execution_outcome is expected_execution
    assert result.working_memory.step_count == 2


@pytest.mark.asyncio
async def test_executor_restores_checkpoint_and_terminal_work_without_republish_or_model_call() -> (
    None
):
    """已有 Checkpoint 与终态 Work 时沿用原时间线并直接进入 Closure。"""
    started_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = started_at + timedelta(minutes=15)
    checkpoint, policy, objective = _recovery_checkpoint(
        started_at=started_at,
        deadline=deadline,
    )
    initial_work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=policy.max_actions,
        remaining_duration_ms=policy.duration_ms,
    )
    work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=policy.max_actions - 4,
        remaining_duration_ms=policy.duration_ms - 120_000,
        branch_id="app:capture-app-latest",
        capture_id="capture-app-latest",
    )
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="持久化 Work 已完成",
        outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
        reason_code="exploration_completed",
    )
    recovery = AppExplorationWorkRecovery(
        initial_work=initial_work,
        latest_work=work,
        terminal=terminal,
        terminal_status=ExplorerWorkStatus.SUCCEEDED,
        terminal_error_code=None,
        consumed_turn_count=1,
        consumed_action_count=4,
        has_nonterminal_work=False,
    )

    class Runtime:
        bootstrap_calls = 0
        publish_calls = 0
        turn_calls = 0
        close_commands = []
        create_calls = 0

        async def get_persisted_exploration_result(self, *, agent_run_id):
            return None

        async def bootstrap(self, command):
            self.bootstrap_calls += 1
            return _recovery_handoff()

        async def get_exploration_runtime_checkpoint(self, *, agent_run_id):
            return checkpoint

        async def create_exploration_runtime_checkpoint(self, *, checkpoint):
            self.create_calls += 1
            raise AssertionError("已有 Checkpoint 时不得重新创建")

        async def get_exploration_work_recovery(
            self,
            *,
            task_id,
            agent_run_id,
            surface_session_id,
        ):
            return recovery

        async def publish_exploration(self, command):
            self.publish_calls += 1
            raise AssertionError("已有 Work 时不得重复发布")

        async def run_one_turn(self, **kwargs):
            self.turn_calls += 1
            raise AssertionError("终态 Work 不得再次调用模型或 Driver")

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(
                close_error=None,
                exploration_result=SimpleNamespace(
                    outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED
                ),
            )

        async def mark_exploration_lost(self, **kwargs):
            raise AssertionError(f"终态 Work 不应标记 LOST：{kwargs}")

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        now=lambda: datetime(2026, 8, 14, 8, 5, tzinfo=UTC),
    ).run(runtime, record=_app_record(), settings=Settings())

    assert runtime.bootstrap_calls == 1
    assert runtime.create_calls == 0
    assert runtime.publish_calls == 0
    assert runtime.turn_calls == 0
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.started_at == started_at
    assert close_command.deadline == deadline
    assert close_command.coverage_summary["consumed_action_count"] == 4
    assert close_command.coverage_summary["consumed_turn_count"] == 1
    assert close_command.coverage_summary["app_runtime_policy"]["max_actions"] == policy.max_actions
    assert close_command.supplemental_evidence_budget is not None
    assert close_command.supplemental_evidence_budget.remaining_actions == policy.max_actions - 4
    assert close_command.supplemental_evidence_budget.remaining_duration_ms == 600_000
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_executor_resumes_nonterminal_work_with_frozen_budget_without_republish() -> None:
    """恢复中的非终态 Work 继续从 PG 领取，并沿用已消费预算。"""

    started_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = started_at + timedelta(minutes=15)
    checkpoint, policy, objective = _recovery_checkpoint(
        started_at=started_at,
        deadline=deadline,
    )
    initial_work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=policy.max_actions,
        remaining_duration_ms=policy.duration_ms,
    )
    latest_work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=policy.max_actions - 4,
        remaining_duration_ms=policy.duration_ms - 120_000,
        branch_id="app:capture-app-latest",
        capture_id="capture-app-latest",
    )
    recovery = AppExplorationWorkRecovery(
        initial_work=initial_work,
        latest_work=latest_work,
        terminal=None,
        terminal_status=None,
        terminal_error_code=None,
        consumed_turn_count=2,
        consumed_action_count=4,
        has_nonterminal_work=True,
    )
    closing_work = replace(
        latest_work,
        budget=replace(
            latest_work.budget,
            remaining_duration_ms=1234,
            max_business_actions=policy.max_actions - 5,
            remaining_business_actions=policy.max_actions - 9,
            max_obstruction_actions=5,
            remaining_obstruction_actions=5,
            business_duration_floor_ms=3000,
            business_duration_floor_applied=1,
        ),
    )

    class Runtime:
        publish_calls = 0
        turn_calls = 0
        close_commands = []

        async def get_persisted_exploration_result(self, *, agent_run_id):
            return None

        async def get_exploration_runtime_checkpoint(self, *, agent_run_id):
            return checkpoint

        async def bootstrap(self, command):
            return _recovery_handoff()

        async def get_exploration_work_recovery(self, **kwargs):
            if self.turn_calls:
                return replace(recovery, latest_work=closing_work)
            return recovery

        async def publish_exploration(self, command):
            self.publish_calls += 1
            raise AssertionError("已有非终态 Work 时不得重复发布")

        async def run_one_turn(self, **kwargs):
            self.turn_calls += 1
            return AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id=latest_work.work_id,
                        task_id=latest_work.task_id,
                        status=ExplorationTerminalStatus.COMPLETED,
                        reason="恢复领取后已完成",
                        outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                        reason_code="exploration_completed",
                    ),
                ),
            )

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def mark_exploration_lost(self, **kwargs):
            raise AssertionError(f"可恢复 Work 不应标记 LOST：{kwargs}")

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        now=lambda: datetime(2026, 8, 14, 8, 5, tzinfo=UTC),
    ).run(runtime, record=_app_record(), settings=Settings())

    assert runtime.publish_calls == 0
    assert runtime.turn_calls == 1
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.started_at == started_at
    assert close_command.deadline == deadline
    assert close_command.coverage_summary["consumed_turn_count"] == 3
    assert close_command.coverage_summary["consumed_action_count"] == 4
    assert close_command.supplemental_evidence_budget == closing_work.budget
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_executor_persisted_technical_terminal_returns_error_after_marking_lost() -> None:
    """终态 Work 缺 payload/技术失败时不得因跳过循环而落入 INCOMPLETE。"""

    started_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = started_at + timedelta(minutes=15)
    checkpoint, policy, objective = _recovery_checkpoint(
        started_at=started_at,
        deadline=deadline,
    )
    work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=policy.max_actions,
        remaining_duration_ms=policy.duration_ms,
    )
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.BLOCKED,
        reason="持久化技术终态",
        outcome=AppExplorationOutcome.TECHNICAL_FAILED.value,
        reason_code="APP_DRIVER_CRASH",
    )
    recovery = AppExplorationWorkRecovery(
        initial_work=work,
        latest_work=work,
        terminal=terminal,
        terminal_status=ExplorerWorkStatus.FAILED,
        terminal_error_code="APP_DRIVER_CRASH",
        consumed_turn_count=1,
        consumed_action_count=0,
        has_nonterminal_work=False,
    )

    class Runtime:
        lost_calls = []
        close_commands = []
        canceled_tasks = []

        async def get_persisted_exploration_result(self, *, agent_run_id):
            return None

        async def get_exploration_runtime_checkpoint(self, *, agent_run_id):
            return checkpoint

        async def bootstrap(self, command):
            return _recovery_handoff()

        async def get_exploration_work_recovery(self, **kwargs):
            return recovery

        async def publish_exploration(self, command):
            raise AssertionError("已有 Work 时不得发布")

        async def run_one_turn(self, **kwargs):
            raise AssertionError("终态 Work 不得再次领取")

        async def cancel_exploration_work(self, *, task_id):
            self.canceled_tasks.append(task_id)

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(
                close_error=None,
                exploration_result=SimpleNamespace(
                    outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                    evidence_gaps=(),
                ),
                index=SimpleNamespace(index_id="index-technical-terminal"),
            )

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert runtime.lost_calls == []
    assert runtime.canceled_tasks == ["task-app"]
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.terminal_reason_code == "APP_DRIVER_CRASH"
    assert close_command.coverage_summary["technical_failure"] is True
    assert close_command.coverage_summary["coverage_snapshot_source"] == (
        "latest_persisted_work"
    )
    assert close_command.coverage_summary["required_page_types"]
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.evidence_index_id == "index-technical-terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovery_error_code",
    ["APP_SESSION_REATTACH_REQUIRED", "APP_SESSION_REATTACH_FAILED"],
)
async def test_executor_seals_checkpoint_evidence_when_app_ready_session_is_unavailable(
    recovery_error_code: str,
) -> None:
    """APP_READY 会话已丢失时不得重跑设备动作，只能封存既有证据。"""

    started_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = started_at + timedelta(minutes=15)
    checkpoint, _policy, _objective = _recovery_checkpoint(
        started_at=started_at,
        deadline=deadline,
    )

    class Runtime:
        close_commands = []
        cancel_calls = 0
        lost_calls = []

        async def get_persisted_exploration_result(self, *, agent_run_id):
            return None

        async def get_exploration_runtime_checkpoint(self, *, agent_run_id):
            return checkpoint

        async def bootstrap(self, command):
            raise AppWorkflowFailure(
                "APP_READY 对应的 Surface Session 不可恢复",
                stage=AppBootstrapStage.APP_READY,
                error_code=recovery_error_code,
                retryable=True,
            )

        async def cancel_exploration_work(self, *, task_id):
            self.cancel_calls += 1

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error="surface_session_lost")

        async def publish_exploration(self, command):
            raise AssertionError("失效 Session 不得发布 Work")

        async def run_one_turn(self, **kwargs):
            raise AssertionError("失效 Session 不得调用 Explorer/Driver")

        async def mark_exploration_lost(self, **kwargs):
            self.lost_calls.append(kwargs)

    runtime = Runtime()
    metric_before = app_metric_value(APP_EXPLORATION_STALE_SESSION_TOTAL)
    result = await ProductionAppV2RuntimeExecutor(worker_instance_id="worker-1").run(
        runtime,
        record=_app_record(),
        settings=Settings(),
    )

    assert runtime.cancel_calls == 1
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.capture_final_observation is False
    assert close_command.run_evidence_gap_reason_code == recovery_error_code
    assert close_command.started_at == started_at
    assert close_command.deadline == deadline
    assert runtime.lost_calls == []
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert app_metric_value(APP_EXPLORATION_STALE_SESSION_TOTAL) == metric_before + 1


@pytest.mark.asyncio
async def test_executor_seals_expired_checkpoint_without_bootstrap_or_publish() -> None:
    """检查点已过期且尚无 Work 时，直接封存既有 Bootstrap 事实。"""

    started_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = started_at + timedelta(minutes=15)
    checkpoint, _policy, _objective = _recovery_checkpoint(
        started_at=started_at,
        deadline=deadline,
    )
    empty_recovery = AppExplorationWorkRecovery(
        initial_work=None,
        latest_work=None,
        terminal=None,
        terminal_status=None,
        terminal_error_code=None,
        consumed_turn_count=0,
        consumed_action_count=0,
        has_nonterminal_work=False,
    )

    class Runtime:
        close_commands = []
        cancel_calls = 0

        async def get_persisted_exploration_result(self, *, agent_run_id):
            return None

        async def get_exploration_runtime_checkpoint(self, *, agent_run_id):
            return checkpoint

        async def get_exploration_work_recovery(self, **kwargs):
            return empty_recovery

        async def bootstrap(self, command):
            raise AssertionError("过期且无 Work 的 Checkpoint 不得重新挂接设备")

        async def cancel_exploration_work(self, *, task_id):
            self.cancel_calls += 1

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def publish_exploration(self, command):
            raise AssertionError("过期 Checkpoint 不得发布 Work")

        async def run_one_turn(self, **kwargs):
            raise AssertionError("过期 Checkpoint 不得调用 Explorer/Driver")

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        now=lambda: deadline + timedelta(seconds=1),
    ).run(runtime, record=_app_record(), settings=Settings())

    assert runtime.cancel_calls == 1
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.capture_final_observation is False
    assert close_command.run_evidence_gap_reason_code == "APP_EXPLORATION_RUNTIME_DEADLINE_EXCEEDED"
    assert close_command.started_at == started_at
    assert close_command.deadline == deadline
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_executor_waits_for_persisted_retry_time_before_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启后必须先等待 PG 冻结的 next_claim_at，再重新领取 RETRY_WAIT Work。"""

    started_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    deadline = started_at + timedelta(minutes=15)
    next_claim_at = started_at + timedelta(minutes=5)
    checkpoint, policy, objective = _recovery_checkpoint(
        started_at=started_at,
        deadline=deadline,
    )
    initial_work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=policy.max_actions,
        remaining_duration_ms=policy.duration_ms,
    )
    work = _recovery_work(
        policy=policy,
        objective=objective,
        started_at=started_at,
        deadline=deadline,
        remaining_actions=policy.max_actions - 1,
        remaining_duration_ms=policy.duration_ms - 30_000,
        branch_id="app:capture-app-retry",
        capture_id="capture-app-retry",
    )
    recovery = AppExplorationWorkRecovery(
        initial_work=initial_work,
        latest_work=work,
        terminal=None,
        terminal_status=None,
        terminal_error_code=None,
        consumed_turn_count=0,
        consumed_action_count=1,
        has_nonterminal_work=True,
        next_claim_at=next_claim_at,
    )
    wait_calls = []

    async def fake_wait(runtime, **kwargs):
        wait_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        "services.api.app.application.app_runtime_composition._wait_for_database_retry",
        fake_wait,
    )

    class Runtime:
        turn_calls = 0
        close_commands = []

        async def get_persisted_exploration_result(self, *, agent_run_id):
            return None

        async def get_exploration_runtime_checkpoint(self, *, agent_run_id):
            return checkpoint

        async def get_exploration_work_recovery(self, **kwargs):
            return recovery

        async def bootstrap(self, command):
            return _recovery_handoff()

        async def publish_exploration(self, command):
            raise AssertionError("RETRY_WAIT Work 不得重复发布")

        async def run_one_turn(self, **kwargs):
            self.turn_calls += 1
            return AppExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(
                    is_terminal=True,
                    decision=ExplorationTerminal(
                        work_id=work.work_id,
                        task_id=work.task_id,
                        status=ExplorationTerminalStatus.COMPLETED,
                        reason="重领后已完成",
                        outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
                        reason_code="exploration_completed",
                    ),
                ),
            )

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def mark_exploration_lost(self, **kwargs):
            raise AssertionError(f"可恢复 RETRY_WAIT 不应标记 LOST：{kwargs}")

    runtime = Runtime()
    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        now=lambda: started_at + timedelta(minutes=1),
    ).run(runtime, record=_app_record(), settings=Settings())

    assert len(wait_calls) == 1
    assert wait_calls[0]["next_claim_at"] == next_claim_at
    assert wait_calls[0]["deadline"] == deadline
    assert runtime.turn_calls == 1
    assert len(runtime.close_commands) == 1
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
