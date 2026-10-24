"""App V2 组合根的共享 Runtime 与显式入口测试。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.api.app.application.app_v2_runtime as app_v2_runtime_module
from services.api.app.application.app_exploration_turn import AppExplorationTurnOutcome
from services.api.app.application.app_interaction_runtime import (
    DriverAppRuntimeIdentityReader,
)
from services.api.app.application.app_targeted_recapture_composition import (
    AppTargetedRecaptureRuntimeFactory,
)
from services.api.app.application.app_v2_runtime import (
    AppV2Runtime,
    AppV2RuntimeDependencies,
    build_app_v2_runtime,
)
from services.api.app.application.device_pool import InProcessDevicePool
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.infrastructure.app_runtime.factory import (
    AppRuntimeBundle,
    AppRuntimeConfigurationError,
)


def _dependencies() -> AppV2RuntimeDependencies:
    dependency = object()
    return AppV2RuntimeDependencies(
        platform_builds=dependency,
        surface_sessions=dependency,
        bootstraps=dependency,
        captures=dependency,
        app_ready_publisher=dependency,
        artifact_reader=dependency,
        understanding_provider=dependency,
        visual_observations=dependency,
        exploration_orchestrator=dependency,
        exploration_queue=dependency,
        explorer=dependency,
        evidence_exit_gate=dependency,
        action_executions=dependency,
        session_command_gate=dependency,
        active_session_inspection=dependency,
        allowed_packages=("com.example.target",),
    )


def test_runtime_dependencies_do_not_expose_inspection_publisher() -> None:
    """正式质检发布必须发生在 Evidence Index 封存之后。"""

    assert "inspection_publisher" not in AppV2RuntimeDependencies.__dataclass_fields__


def test_app_v2_runtime_reuses_one_runtime_bundle() -> None:
    dependencies = _dependencies()
    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=dependencies,
        lease_owner="app-explorer-1",
    )

    assert runtime.bootstrap_driver is runtime.interaction_driver
    assert runtime.bootstrap_port is runtime.bootstrap_driver
    assert runtime.bootstrap_workflow._driver is runtime.bootstrap_driver
    coordinator = runtime.turn_runner._turn_executor._capture_coordinator
    assert coordinator._capture_writer._driver is runtime.interaction_driver
    assert coordinator._interaction_executor._driver is runtime.interaction_driver
    assert isinstance(
        coordinator._interaction_executor._runtime_identity_reader,
        DriverAppRuntimeIdentityReader,
    )
    assert coordinator._interaction_executor._runtime_identity_reader._driver is (
        runtime.interaction_driver
    )
    assert coordinator._interaction_executor._allowed_packages == ("com.example.target",)
    assert runtime.interaction_executor is coordinator._interaction_executor
    assert runtime.interaction_executor._candidate_resolver._prepare_before_visual == (
        runtime.exploration_workflow.prepare_before_action_visual
    )
    closure = runtime.closure_service
    assert closure is not None
    assert closure._capture_coordinator is coordinator
    assert closure._captures is dependencies.captures
    assert closure._understanding is runtime.exploration_workflow._understanding
    assert closure._visual_observations is dependencies.visual_observations
    assert closure._sessions is dependencies.surface_sessions
    assert closure._driver is runtime.interaction_driver
    assert runtime.turn_runner._queue is dependencies.exploration_queue
    assert runtime.runtime_recovery is dependencies.runtime_recovery


def test_app_v2_runtime_passes_scheduler_facts_reader_to_exploration_workflow() -> None:
    reader = object()
    dependencies = replace(_dependencies(), scheduler_facts_reader=reader)

    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=dependencies,
        lease_owner="app-explorer-1",
    )

    assert runtime.exploration_workflow._scheduler_facts_reader is reader


def test_app_v2_runtime_wires_active_session_inspection_before_close() -> None:
    drainer = object()
    dependencies = replace(_dependencies(), active_session_inspection=drainer)

    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=dependencies,
        lease_owner="app-explorer-1",
    )

    assert runtime.closure_service is not None
    assert runtime.closure_service._active_session_inspection is drainer
    assert runtime.active_session_inspection is drainer


async def test_app_runtime_current_surface_checkpoint_uses_existing_run_and_deadline() -> None:
    drainer = SimpleNamespace(run_current_surface_checkpoint=AsyncMock(return_value="checked"))
    runtime = build_app_v2_runtime(
        provider="fake", environment="test",
        dependencies=replace(_dependencies(), active_session_inspection=drainer),
        lease_owner="app-explorer-1",
    )
    deadline = datetime.now(UTC)

    assert await runtime.inspect_current_app_surface(
        task_id="task-1", agent_run_id="run-1", deadline=deadline
    ) == "checked"
    drainer.run_current_surface_checkpoint.assert_awaited_once_with(
        task_id="task-1", agent_run_id="run-1", deadline=deadline
    )


def test_app_v2_runtime_rejects_missing_active_session_inspection() -> None:
    with pytest.raises(ValueError, match="活会话规则排空器"):
        replace(_dependencies(), active_session_inspection=None)


def test_app_v2_runtime_wires_one_shared_global_frontier_queue() -> None:
    frontier_queue = object()
    dependencies = replace(_dependencies(), app_frontier_queue=frontier_queue)

    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=dependencies,
        lease_owner="app-explorer-1",
    )

    assert runtime.turn_runner._frontier_queue is frontier_queue
    assert runtime.turn_runner._global_frontier is not None
    assert runtime.turn_runner._global_frontier._repository is frontier_queue


def test_app_v2_runtime_wires_targeted_recapture_factory_when_ports_are_complete() -> None:
    page_graph = object()
    targeted_recapture_contexts = object()
    restoration_attempts = object()
    probe_orchestrator = object()
    dependencies = replace(
        _dependencies(),
        page_graph=page_graph,
        targeted_recapture_contexts=targeted_recapture_contexts,
        restoration_attempts=restoration_attempts,
        probe_orchestrator=probe_orchestrator,
    )

    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=dependencies,
        lease_owner="app-explorer-1",
    )

    factory = runtime.require_targeted_recapture_factory()
    assert isinstance(factory, AppTargetedRecaptureRuntimeFactory)
    assert factory._page_graph is page_graph
    assert factory._planning_contexts is targeted_recapture_contexts
    assert factory._restoration_attempts is restoration_attempts
    assert factory._probe_orchestrator is probe_orchestrator


def test_app_v2_runtime_rejects_partial_targeted_recapture_ports() -> None:
    dependencies = replace(_dependencies(), page_graph=object())

    with pytest.raises(ValueError, match="必须同时提供"):
        build_app_v2_runtime(
            provider="fake",
            environment="test",
            dependencies=dependencies,
            lease_owner="app-explorer-1",
        )


def test_app_v2_runtime_wires_supplemental_drain_only_when_runtime_is_ready() -> None:
    dependencies = replace(
        _dependencies(),
        page_graph=object(),
        targeted_recapture_contexts=object(),
        restoration_attempts=object(),
        probe_orchestrator=object(),
        probe_completion=object(),
        evidence_requests=object(),
        inspection_work_items=object(),
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    )

    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=dependencies,
        lease_owner="app-explorer-1",
    )

    assert runtime.closure_service is not None
    drainer = runtime.closure_service._supplemental_evidence_drain
    assert drainer is not None
    assert drainer._inspection is dependencies.active_session_inspection
    assert drainer._probe_completion is dependencies.probe_completion
    assert drainer._inspection_wakeup is dependencies.inspection_work_items
    assert drainer._recapture._requests is dependencies.evidence_requests


def test_app_v2_runtime_rejects_incomplete_ready_supplemental_ports() -> None:
    dependencies = replace(
        _dependencies(),
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    )

    with pytest.raises(ValueError, match="App 补采排空必须提供"):
        build_app_v2_runtime(
            provider="fake",
            environment="test",
            dependencies=dependencies,
            lease_owner="app-explorer-1",
        )


@pytest.mark.asyncio
async def test_app_v2_runtime_preserves_cancellation_port_for_exploration() -> None:
    class Cancellation:
        async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
            assert (task_id, agent_run_id) == ("task-1", "run-1")
            return True

    cancellation = Cancellation()
    runtime = build_app_v2_runtime(
        provider="fake",
        environment="test",
        dependencies=replace(_dependencies(), cancellation=cancellation),
        lease_owner="app-explorer-1",
    )

    assert runtime.cancellation is cancellation
    assert await runtime.is_cancel_requested(task_id="task-1", agent_run_id="run-1")


def test_app_v2_runtime_rejects_split_bootstrap_and_interaction_runtime() -> None:
    bootstrap = RecordingWorkflow(result="ready")
    exploration = RecordingWorkflow(result="published")
    turn = RecordingTurnRunner(result="completed")

    with pytest.raises(ValueError, match="共享同一 Runtime"):
        AppV2Runtime(
            runtime_bundle=AppRuntimeBundle(
                bootstrap_port=object(),
                driver=object(),
            ),
            bootstrap_workflow=bootstrap,
            exploration_workflow=exploration,
            turn_runner=turn,
        )


def test_app_v2_runtime_rejects_fake_in_production_before_composition() -> None:
    with pytest.raises(AppRuntimeConfigurationError, match="production"):
        build_app_v2_runtime(
            provider="fake",
            environment="production",
            dependencies=_dependencies(),
            lease_owner="app-explorer-1",
        )


@dataclass
class RecordingWorkflow:
    result: object
    calls: list[object] = field(default_factory=list)

    async def run(self, command: object) -> object:
        self.calls.append(command)
        return self.result


@dataclass
class RecordingTurnRunner:
    result: object
    calls: list[dict[str, object]] = field(default_factory=list)

    async def run_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        work_type=None,  # noqa: ANN001
    ) -> object:
        call: dict[str, object] = {"task_id": task_id, "agent_run_id": agent_run_id}
        if work_type is not None:
            call["work_type"] = work_type
        self.calls.append(call)
        return self.result


@dataclass
class RecordingClosure:
    result: object
    calls: list[object] = field(default_factory=list)

    async def close(self, command: object) -> object:
        self.calls.append(command)
        return self.result

    async def mark_lost(self, **kwargs) -> object:
        self.calls.append(kwargs)
        return self.result


async def test_app_v2_runtime_exposes_four_explicit_entrypoints() -> None:
    shared_driver = object()
    bootstrap = RecordingWorkflow(result="ready")
    exploration = RecordingWorkflow(result="published")
    turn_outcome = AppExplorationTurnOutcome(claimed=False)
    turn = RecordingTurnRunner(result=turn_outcome)
    closure = RecordingClosure(result="closed")
    runtime = AppV2Runtime(
        runtime_bundle=AppRuntimeBundle(
            bootstrap_port=shared_driver,
            driver=shared_driver,
        ),
        bootstrap_workflow=bootstrap,
        exploration_workflow=exploration,
        turn_runner=turn,
        closure_service=closure,
    )
    bootstrap_command = object()
    exploration_command = object()
    closure_command = object()

    assert await runtime.bootstrap(bootstrap_command) == "ready"
    assert await runtime.publish_exploration(exploration_command) == "published"
    assert await runtime.run_one_turn(
        task_id="task-1", agent_run_id="run-1"
    ) is turn_outcome
    assert await runtime.close_exploration(closure_command) == "closed"
    assert bootstrap.calls == [bootstrap_command]
    assert exploration.calls == [exploration_command]
    assert turn.calls == [{"task_id": "task-1", "agent_run_id": "run-1"}]
    assert closure.calls == [closure_command]


async def test_app_v2_runtime_completes_successful_probe_before_returning() -> None:
    shared_driver = object()
    outcome = AppExplorationTurnOutcome(
        claimed=True,
        work_id="probe-work-1",
        work_type="probe",
        final_status=ExplorerWorkStatus.SUCCEEDED,
        result_capture_id="capture-result",
        evidence_request_id="request-1",
    )
    completion = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace()))
    runtime = AppV2Runtime(
        runtime_bundle=AppRuntimeBundle(
            bootstrap_port=shared_driver,
            driver=shared_driver,
        ),
        bootstrap_workflow=RecordingWorkflow(result="ready"),
        exploration_workflow=RecordingWorkflow(result="published"),
        turn_runner=RecordingTurnRunner(result=outcome),
        probe_completion=completion,
    )

    result = await runtime.run_one_turn(
        task_id="task-1",
        agent_run_id="run-1",
        work_type=app_v2_runtime_module.ExplorerWorkType.PROBE,
    )

    assert result is outcome
    completion.complete.assert_awaited_once_with(task_id="task-1", outcome=outcome)


async def test_app_v2_runtime_rejects_successful_probe_without_completion_service() -> None:
    shared_driver = object()
    outcome = AppExplorationTurnOutcome(
        claimed=True,
        work_id="probe-work-1",
        work_type="probe",
        final_status=ExplorerWorkStatus.SUCCEEDED,
        result_capture_id="capture-result",
        evidence_request_id="request-1",
    )
    runtime = AppV2Runtime(
        runtime_bundle=AppRuntimeBundle(
            bootstrap_port=shared_driver,
            driver=shared_driver,
        ),
        bootstrap_workflow=RecordingWorkflow(result="ready"),
        exploration_workflow=RecordingWorkflow(result="published"),
        turn_runner=RecordingTurnRunner(result=outcome),
    )

    with pytest.raises(RuntimeError, match="Probe Completion"):
        await runtime.run_one_turn(
            task_id="task-1",
            agent_run_id="run-1",
            work_type=app_v2_runtime_module.ExplorerWorkType.PROBE,
        )


async def test_app_v2_runtime_binds_installed_package_after_bootstrap() -> None:
    class IdentityBoundary:
        def __init__(self) -> None:
            self.packages: list[str] = []

        def bind_installed_package(self, package_name: str) -> None:
            self.packages.append(package_name)

    shared_driver = object()
    handoff = SimpleNamespace(installed_package="com.example.actual")
    boundary = IdentityBoundary()
    runtime = AppV2Runtime(
        runtime_bundle=AppRuntimeBundle(
            bootstrap_port=shared_driver,
            driver=shared_driver,
        ),
        bootstrap_workflow=RecordingWorkflow(result=handoff),
        exploration_workflow=RecordingWorkflow(result="published"),
        turn_runner=RecordingTurnRunner(result="completed"),
        interaction_executor=boundary,  # type: ignore[arg-type]
    )

    assert await runtime.bootstrap(object()) is handoff
    assert boundary.packages == ["com.example.actual"]


async def test_app_v2_runtime_keeps_device_lease_guard_until_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = InProcessDevicePool(slots=["cloud_phone:a"])
    pool.claim(
        task_id="task-1",
        session_id="session-1",
        preferred_device_ref="cloud_phone:a",
        lease_owner="worker-1",
    )

    async def get_lease(**kwargs):
        assert kwargs == {"session_id": "session-1"}
        return pool.get(session_id="session-1")

    async def get_pool():
        return pool

    monkeypatch.setattr(app_v2_runtime_module, "get_device_lease", get_lease)
    monkeypatch.setattr(app_v2_runtime_module, "get_runtime_device_pool", get_pool)
    shared_driver = object()
    closure = RecordingClosure(result="closed")
    runtime = AppV2Runtime(
        runtime_bundle=AppRuntimeBundle(
            bootstrap_port=shared_driver,
            driver=shared_driver,
        ),
        bootstrap_workflow=RecordingWorkflow(result="ready"),
        exploration_workflow=RecordingWorkflow(result="published"),
        turn_runner=RecordingTurnRunner(result="completed"),
        closure_service=closure,
    )

    assert await runtime.bootstrap(SimpleNamespace(session_id="session-1")) == "ready"
    assert pool.get(session_id="session-1").state == "active"
    assert "session-1" in runtime._device_lease_guards
    assert await runtime.publish_exploration(object()) == "published"
    assert await runtime.close_exploration(SimpleNamespace(session_id="session-1")) == "closed"
    assert runtime._device_lease_guards == {}
    assert pool.release(lease=pool.get(session_id="session-1")) is True
