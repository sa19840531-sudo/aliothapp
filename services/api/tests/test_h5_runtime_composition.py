"""H5 V2 生产组合根和执行循环测试。"""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from services.api.app.agents.runtime.models import ExecutionOutcome, TerminationReason
from services.api.app.agents.runtime.run_context import require_agent_run_context
from services.api.app.agents.runtime.usage_meter import bind_run_llm_usage_meter
from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseLostError,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationResult,
    CaptureCoordinationStage,
)
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkStatus,
)
from services.api.app.application.h5_exploration_turn import H5ExplorationTurnOutcome
from services.api.app.application.h5_task_runtime_settings import H5TaskRuntimeSettings
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.core.config import Settings
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    H5AuthBootstrapResult,
    H5AuthStatus,
    build_exploration_work,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5FrontierEntryStatus,
    H5TaskFrontierSnapshot,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    build_visual_navigation_capability,
    verify_visual_navigation_capability,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


def _runtime_module():
    return import_module("services.api.app.application.h5_runtime_composition")


def test_restoration_rebind_does_not_reset_stale_source_retry_budget():
    from services.api.app.application.h5_exploration_workflow import (
        _rebind_selected_task_entry_to_fresh_frontier,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    work = _work()
    old = replace(work.frontier.candidates[0], metadata={
        **work.frontier.candidates[0].metadata, "source_refresh_attempt_count": 3,
    })
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0, candidates=(old,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    fresh = replace(work.frontier.candidates[0], candidate_id="candidate-fresh")
    result = _rebind_selected_task_entry_to_fresh_frontier(
        snapshot=snapshot, frontier=replace(work.frontier, candidates=(fresh,)),
    )
    assert result.entries[0].candidate.metadata["source_refresh_attempt_count"] == 3


class _NoopAssessments:
    async def upsert(self, **kwargs):
        return kwargs


def _h5_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-h5",
        task_id="task-h5",
        message="自动探索并质检 H5 页面",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "_task_target": {
                "platform": "h5",
                "target_url": "https://m.example.test/home",
            },
        },
    )


def _runtime_settings(
    *,
    max_actions: int = 4,
    max_depth: int = 2,
    max_ai_calls: int = 96,
    max_total_tokens: int | None = None,
) -> H5TaskRuntimeSettings:
    return H5TaskRuntimeSettings(
        scan_mode="custom",
        max_actions=max_actions,
        max_depth=max_depth,
        max_duration_seconds=120.0,
        max_pages=3,
        journey_steps=("商品详情",),
        include_page_types=("home",),
        exclude_page_types=("payment",),
        max_ai_calls=max_ai_calls,
        max_total_tokens=max_total_tokens,
    )


def _completed_outcome() -> H5ExplorationTurnOutcome:
    return H5ExplorationTurnOutcome(
        claimed=True,
        final_status=ExplorerWorkStatus.SUCCEEDED,
        turn=SimpleNamespace(
            is_terminal=True,
            decision=ExplorationTerminal(
                work_id="work-terminal",
                task_id="task-h5",
                status=ExplorationTerminalStatus.COMPLETED,
                reason="Frontier 已清空",
            ),
        ),
    )


def _auth_result(*, status: H5AuthStatus, needs_review: bool) -> H5AuthBootstrapResult:
    return H5AuthBootstrapResult(
        status=status,
        final_capture=CaptureRef(
            task_id="task-h5",
            session_id="h5:run-h5",
            capture_id="capture-auth-root",
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role="after_action",
        ),
        credential_scope="global",
        reason_code=("unsupported_login_challenge" if needs_review else "password_login_verified"),
        attempted=status is not H5AuthStatus.GUEST_ONLY,
        needs_review=needs_review,
    )


@pytest.mark.asyncio
async def test_provider_wires_enabled_h5_auth_from_global_profile(tmp_path) -> None:
    module = _runtime_module()
    config_path = tmp_path / "h5-auth.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "h5-auth-config.v1",
                "global": {
                    "phone": "AUTH_PROVIDER_PHONE_CANARY",
                    "password": "AUTH_PROVIDER_PASSWORD_CANARY",
                },
                "domains": {},
            }
        ),
        encoding="utf-8",
    )
    trusted_snapshot_reader = object()
    target_resolver = object()
    secret_form_browser = object()
    capture_runtime = SimpleNamespace(
        h5_capture_recovery=object(),
        coordinator=object(),
        h5_exploration_coordinator=object(),
        surface_driver=object(),
        surface_sessions=object(),
        session_command_gate=object(),
        trusted_ui_snapshot_reader=trusted_snapshot_reader,
        h5_target_resolver=target_resolver,
        h5_secret_form_browser=secret_form_browser,
        coordinate_secret_input_runtime=lambda runtime: runtime,
    )
    settings = Settings.model_validate(
        {
            "h5_auth": {
                "enabled": True,
                "config_path": config_path,
            }
        }
    )

    runtime = await module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5",
        capture_runtime_factory=lambda **_: capture_runtime,
    ).build(record=_h5_record(), settings=settings)

    assert runtime.workflow._orchestrator.__class__.__name__ == "ExplorationOrchestrator"
    assert runtime.turn_runner._session_command_gate is capture_runtime.session_command_gate
    adaptive = runtime.turn_runner._adaptive_journey
    assert adaptive.__class__.__name__ == "H5AdaptiveJourneyRunner"
    assert adaptive._repository._observer._snapshots is trusted_snapshot_reader
    assert adaptive._planner._delegate_factory.__name__ == "build_default_h5_explorer_agent"

    auth_bootstrap = runtime.workflow._auth_bootstrap
    assert isinstance(auth_bootstrap, module.H5AuthBootstrapService)
    credential_ref = auth_bootstrap._credential_resolver.resolve_ref("m.example.test")
    assert credential_ref is not None
    assert credential_ref.scope == "global"
    assert auth_bootstrap._page_reader._snapshot_reader is trusted_snapshot_reader
    assert auth_bootstrap._secret_runtime._target_resolver is target_resolver
    assert auth_bootstrap._secret_runtime._browser is secret_form_browser


@pytest.mark.asyncio
async def test_provider_wires_postgres_scheduler_facts_reader_into_workflow() -> None:
    """正式 H5 组合根必须使用真实事实读取器，而不是只在测试中接线。"""
    module = _runtime_module()
    capture_runtime = SimpleNamespace(
        session_command_gate=object(),
        h5_capture_recovery=object(),
        coordinator=object(),
        h5_exploration_coordinator=object(),
        surface_driver=object(),
        surface_sessions=object(),
    )

    runtime = await module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5",
        capture_runtime_factory=lambda **_: capture_runtime,
    ).build(record=_h5_record(), settings=Settings())

    assert isinstance(
        runtime.workflow._scheduler_facts_reader,
        module.PostgresExplorationSchedulerFactsReader,
    )
    pattern_knowledge = runtime.workflow._interaction_pattern_knowledge
    assert isinstance(
        pattern_knowledge,
        module.PostgresH5InteractionPatternRepository,
    )
    assert runtime.workflow._detector._interaction_pattern_knowledge is pattern_knowledge
    assert runtime.turn_runner._recovery._captures is capture_runtime.h5_capture_recovery
    assert runtime.turn_runner._recovery._surface_sessions is capture_runtime.surface_sessions


@pytest.mark.asyncio
async def test_provider_injects_lab_policy_only_into_h5_active_session(
    monkeypatch,
) -> None:
    """H5 Lab 补采权限必须经组合根注入，不能依赖生产全局开关。"""
    module = _runtime_module()
    policy = build_lab_supplemental_evidence_policy()
    captured: dict[str, object] = {}
    drain = object()

    def build_drain(**kwargs):
        captured.update(kwargs)
        return drain

    monkeypatch.setattr(
        module,
        "build_default_h5_active_session_inspection_drain_service",
        build_drain,
    )
    capture_runtime = SimpleNamespace(
        session_command_gate=object(),
        h5_capture_recovery=object(),
        surface_driver=object(),
        surface_sessions=object(),
        coordinator=object(),
        h5_exploration_coordinator=object(),
    )

    runtime = await module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5-lab",
        supplemental_evidence_policy=policy,
        capture_runtime_factory=lambda **_: capture_runtime,
    ).build(record=_h5_record(), settings=Settings())

    assert runtime.active_session_inspection is drain
    assert captured["supplemental_evidence_policy"] is policy
    assert captured["capture_runtime"] is capture_runtime
    assert captured["cancellation"] is runtime.cancellation
    assert captured["assessment_repository"] is not None


def test_h5_handler_passes_explicit_lab_policy_to_default_provider() -> None:
    module = _runtime_module()
    policy = build_lab_supplemental_evidence_policy()

    handler = module.build_h5_workflow_handler(
        Settings.model_validate({"workflow": {"h5_v2_mode": "on"}}),
        "worker-h5-lab",
        supplemental_evidence_policy=policy,
    )

    assert handler._provider._supplemental_evidence_policy is policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shadow", "expected_effective"),
    [(False, True), (True, False)],
)
async def test_provider_wires_logical_surface_decision_mode(
    shadow: bool,
    expected_effective: bool,
) -> None:
    module = _runtime_module()
    capture_runtime = SimpleNamespace(
        session_command_gate=object(),
        h5_capture_recovery=object(),
        surface_driver=object(),
        surface_sessions=object(),
        coordinator=object(),
        h5_exploration_coordinator=object(),
    )
    settings = Settings.model_validate(
        {
            "workflow": {
                "h5_logical_surface_enabled": True,
                "h5_logical_surface_shadow": shadow,
            }
        }
    )

    runtime = await module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5",
        capture_runtime_factory=lambda **_: capture_runtime,
    ).build(record=_h5_record(), settings=settings)

    assert runtime.workflow._logical_surface_effective is expected_effective


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_values", "expected_mode"),
    [
        (
            {
                "h5_visual_coordinate_mode": "enforced",
                "h5_logical_surface_enabled": False,
            },
            "off",
        ),
        (
            {
                "h5_visual_coordinate_mode": "enforced",
                "h5_logical_surface_enabled": True,
                "h5_logical_surface_shadow": True,
            },
            "shadow",
        ),
        (
            {
                "h5_visual_coordinate_mode": "enforced",
                "h5_logical_surface_enabled": True,
                "h5_logical_targets_enabled": False,
            },
            "shadow",
        ),
        (
            {
                "h5_visual_coordinate_mode": "enforced",
                "h5_logical_surface_enabled": True,
                "h5_logical_targets_enabled": True,
            },
            "enforced",
        ),
    ],
)
async def test_provider_wires_one_effective_visual_mode(
    workflow_values: dict[str, object],
    expected_mode: str,
) -> None:
    """组合根必须把唯一有效模式同时交给检测、工作流、执行与恢复层。"""
    module = _runtime_module()
    record = _h5_record()
    capture_runtime = SimpleNamespace(
        session_command_gate=object(),
        h5_capture_recovery=object(),
        surface_driver=object(),
        surface_sessions=object(),
        coordinator=object(),
        h5_exploration_coordinator=object(),
    )
    capture_runtime_modes: list[str | None] = []

    def capture_runtime_factory(
        *,
        h5_visual_coordinate_mode: str | None = None,
    ) -> SimpleNamespace:
        capture_runtime_modes.append(h5_visual_coordinate_mode)
        return capture_runtime

    settings = Settings.model_validate({"workflow": workflow_values})

    runtime = await module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5",
        capture_runtime_factory=capture_runtime_factory,
    ).build(record=record, settings=settings)

    assert runtime.workflow._h5_visual_coordinate_mode == expected_mode
    assert runtime.workflow._detector._h5_visual_coordinate_mode == expected_mode
    assert capture_runtime_modes == [expected_mode]
    explorer = runtime.turn_runner._turn_executor._explorer
    assert explorer._h5_visual_coordinate_mode == expected_mode
    assert explorer._delegate._h5_visual_coordinate_mode == expected_mode
    recovery = runtime.turn_runner._recovery
    assert recovery._h5_visual_coordinate_mode == expected_mode
    assert recovery._source_task_id == record.task_id


@pytest.mark.asyncio
async def test_persisting_detector_passes_off_without_visual_client_construction(
    monkeypatch,
) -> None:
    """若 Detector 未显式传 off，页面检测会回退读取全局配置并可能创建视觉客户端。"""
    module = _runtime_module()
    calls: list[dict[str, object]] = []

    async def fake_detect_page_for_agent(**kwargs):
        calls.append(dict(kwargs))
        return {"page_capture_id": kwargs["page_capture_id"]}

    async def fake_persist(*, result, finalize_evidence_readiness):
        assert finalize_evidence_readiness is False
        return result

    monkeypatch.setattr(module, "detect_page_for_agent", fake_detect_page_for_agent)
    monkeypatch.setattr(
        module,
        "persist_page_detection_for_current_run",
        fake_persist,
    )
    detector = module.PersistingH5PageDetector(h5_visual_coordinate_mode="off")

    result = await detector.detect(
        capture=CaptureRef(
            task_id="task-h5",
            session_id="h5:run-h5",
            capture_id="capture-mode-off",
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role="after_action",
        ),
        task_id="task-h5",
        agent_run_id="run-h5",
    )

    assert result["page_capture_id"] == "capture-mode-off"
    assert calls == [
        {
            "page_capture_id": "capture-mode-off",
            "visual_coordinate_mode": "off",
            "recognition_guide_enforced": True,
        }
    ]


@pytest.mark.asyncio
async def test_executor_runs_initial_detection_turn_loop_and_orderly_close() -> None:
    module = _runtime_module()
    calls: list[object] = []
    outcomes = [
        H5ExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            turn=SimpleNamespace(is_terminal=False),
            successor_work_id="work-2",
        ),
        _completed_outcome(),
    ]

    class Runtime:
        async def start(self, command):
            context = require_agent_run_context()
            calls.append(("start", command, context.agent_run_id))
            return SimpleNamespace(
                work=SimpleNamespace(work_id="work-1"),
                auth_result=_auth_result(
                    status=H5AuthStatus.AUTHENTICATED,
                    needs_review=False,
                ),
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            context = require_agent_run_context()
            calls.append(("turn", context.task_id))
            return outcomes.pop(0)

        async def close(self, *, task_id, session_id):
            calls.append(("close", task_id, session_id))

    runtime = Runtime()
    runtime.runtime_settings = _runtime_settings()
    run_started_at = datetime(2026, 8, 12, tzinfo=UTC)
    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        now=lambda: run_started_at,
    ).run(
        runtime,
        record=_h5_record(),
        settings=Settings(),
    )

    start_call = calls[0]
    assert start_call[0] == "start"
    assert start_call[1].target_url == "https://m.example.test/home"
    assert start_call[1].session_id == "h5:run-h5"
    assert start_call[1].objective.startswith("在只读安全边界内")
    assert "按规则集完成" not in start_call[1].objective
    assert "商品详情" in start_call[1].objective
    assert start_call[1].budget.max_actions == 4
    assert start_call[1].budget.max_depth == 2
    assert start_call[1].budget.remaining_duration_ms == 120_000
    assert start_call[1].deadline == datetime(2026, 8, 12, tzinfo=UTC) + module.timedelta(
        seconds=120
    )
    assert [item for item in calls if item[0] == "turn"] == [
        ("turn", "task-h5"),
        ("turn", "task-h5"),
    ]
    assert calls[-1] == ("close", "task-h5", "h5:run-h5")
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_executor_consumes_successor_after_retryable_action_supersede() -> None:
    module = _runtime_module()
    events: list[str] = []
    outcomes = [
        H5ExplorationTurnOutcome(
            claimed=True,
            final_status=ExplorerWorkStatus.SUPERSEDED,
            successor_work_id="work-reobserved",
            error_code="STARTUP_AD_DISMISSAL_NOT_FOUND",
        ),
        _completed_outcome(),
    ]

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=3)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("turn")
            return outcomes.pop(0)

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(worker_instance_id="worker-h5").run(
        Runtime(), record=_h5_record(), settings=Settings()
    )

    assert events == ["turn", "turn", "close"]
    assert result.termination_reason is TerminationReason.COMPLETED


@pytest.mark.asyncio
async def test_executor_closes_session_without_internal_quality_handoff() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command
            return None

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("explore")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            assert task_id == "task-h5"
            assert session_id == "h5:run-h5"
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(worker_instance_id="worker-h5").run(
        Runtime(), record=_h5_record(), settings=Settings()
    )

    assert events == ["explore", "close"]
    assert result.termination_reason is TerminationReason.COMPLETED


@pytest.mark.asyncio
async def test_executor_drains_active_session_inspection_before_close() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("explore")
            return _completed_outcome()

        async def prepare_close(self, *, task_id):
            del task_id
            events.append("prepare_close")

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    class Drainer:
        async def drain_before_close(self, *, task_id, agent_run_id, deadline):
            del task_id, agent_run_id, deadline
            events.append("drain")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        active_session_inspection=Drainer(),
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert events == ["explore", "prepare_close", "drain", "close"]
    assert result.termination_reason is TerminationReason.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "drain", "drain_external"])
async def test_executor_closes_session_when_shutdown_is_cancelled(stage) -> None:
    import asyncio

    module = _runtime_module()
    events = []
    entered_drain = asyncio.Event()

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            pass

        async def run_one_turn(self, **kwargs):
            return _completed_outcome()

        async def prepare_close(self, **kwargs):
            if stage == "prepare":
                raise asyncio.CancelledError

        async def close(self, **kwargs):
            events.append("close")

    class Drainer:
        async def drain_before_close(self, **kwargs):
            if stage == "drain_external":
                entered_drain.set()
                await asyncio.Event().wait()
            raise asyncio.CancelledError

    executor = module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5", active_session_inspection=Drainer(),
    )
    operation = asyncio.create_task(executor.run(
        Runtime(), record=_h5_record(), settings=Settings(),
    ))
    if stage == "drain_external":
        await asyncio.wait_for(entered_drain.wait(), timeout=1)
        operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert events == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restored_source", [False, True])
async def test_full_inspection_runs_h5_checkpoint_before_each_next_action(restored_source) -> None:
    module = _runtime_module()
    events: list[str] = []
    outcomes = [
        H5ExplorationTurnOutcome(
            claimed=True,
            final_status=(ExplorerWorkStatus.RETRY_WAIT if restored_source
                          else ExplorerWorkStatus.SUCCEEDED),
            turn=None if restored_source else SimpleNamespace(is_terminal=False),
            successor_work_id=None if restored_source else "work-2",
            restored_source=restored_source,
        ),
        _completed_outcome(),
    ]

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=3)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(auth_result=None)

        async def inspect_current_h5_surface(self, **kwargs):
            assert kwargs["task_id"] == "task-h5"
            assert kwargs["agent_run_id"] == "run-h5"
            events.append("checkpoint")

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("turn")
            return outcomes.pop(0)

        async def prepare_close(self, *, task_id):
            del task_id
            events.append("prepare_close")

        async def drain_active_session_inspection(self, **kwargs):
            del kwargs
            events.append("drain")

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5"
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert events == [
        "start",
        "checkpoint",
        "turn",
        "checkpoint",
        "turn",
        "checkpoint",
        "prepare_close",
        "drain",
        "close",
    ]
    assert result.termination_reason is TerminationReason.COMPLETED


@pytest.mark.asyncio
async def test_desktop_web_does_not_run_h5_incremental_checkpoint() -> None:
    module = _runtime_module()
    events: list[str] = []
    record = _h5_record()
    record.task_config["_task_target"] = {
        "platform": "web",
        "target_url": "https://www.example.test/home",
        "web": {"browser_profile_id": "profile-web"},
    }

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command
            return SimpleNamespace(auth_result=None)

        async def inspect_current_h5_surface(self, **kwargs):
            del kwargs
            events.append("checkpoint")

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            return _completed_outcome()

        async def drain_active_session_inspection(self, **kwargs):
            del kwargs

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    await module.ProductionH5V2RuntimeExecutor(worker_instance_id="worker-h5").run(
        Runtime(),
        record=record,
        settings=Settings(),
    )

    assert events == []


@pytest.mark.asyncio
async def test_exploration_only_executor_skips_active_session_inspection_drain() -> None:
    """纯探索任务不得等待只属于完整质检的活会话规则排空。"""

    module = _runtime_module()
    events: list[str] = []
    record = _h5_record()
    record.task_config["execution_mode"] = "exploration_only"

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("explore")
            return _completed_outcome()

        async def prepare_close(self, *, task_id):
            del task_id
            events.append("prepare_close")

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    class Drainer:
        async def drain_before_close(self, *, task_id, agent_run_id, deadline):
            del task_id, agent_run_id, deadline
            events.append("drain")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        active_session_inspection=Drainer(),
    ).run(Runtime(), record=record, settings=Settings())

    assert events == ["explore", "prepare_close", "close"]
    assert result.termination_reason is TerminationReason.COMPLETED


@pytest.mark.asyncio
async def test_executor_forces_incomplete_review_when_auth_is_guest_only() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(
                work=SimpleNamespace(work_id="work-1"),
                auth_result=_auth_result(
                    status=H5AuthStatus.GUEST_ONLY,
                    needs_review=True,
                ),
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            raise AssertionError("认证阶段已需要人工复核时不得继续探索")

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    runtime = Runtime()
    runtime.runtime_settings = _runtime_settings(max_actions=2)
    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(
        runtime,
        record=_h5_record(),
        settings=Settings(),
    )

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert "unsupported_login_challenge" in str(result.error_message)
    assert events == ["start", "close"]


@pytest.mark.asyncio
async def test_executor_fast_reviews_unready_h5_coverage_without_inventory() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=20)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("fast_review")
            return module.H5FastReviewSnapshot(
                coverage_revision=10,
                assessment_scope_ready=False,
                completion_ready=False,
                inventory_item_count=0,
                blocking_scope_count=1,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            raise AssertionError("coverage 无库存且不可判定时不得继续探索")

        async def close(self, *, task_id, session_id):
            assert task_id == "task-h5"
            assert session_id == "h5:run-h5"
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == module.H5_COVERAGE_NO_INVENTORY_REVIEW_CODE
    assert "覆盖范围" in str(result.final_text)
    assert events == ["start", "fast_review", "close"]


@pytest.mark.asyncio
async def test_fast_review_does_not_mask_terminal_action_failure_before_inventory_handoff() -> None:
    """Runtime 返回后才交接 Inventory，终态动作错误不得被零库存快照覆盖。"""
    module = _runtime_module()
    events: list[str] = []
    fast_review_calls = 0

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=20)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            nonlocal fast_review_calls
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            fast_review_calls += 1
            events.append(f"fast_review:{fast_review_calls}")
            if fast_review_calls == 1:
                return None
            return module.H5FastReviewSnapshot(
                coverage_revision=10,
                assessment_scope_ready=False,
                completion_ready=False,
                inventory_item_count=0,
                blocking_scope_count=1,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("turn")
            return H5ExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.FAILED,
                error_code="LOGICAL_TARGET_HIT_TEST_FAILED",
            )

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.ERROR
    assert result.error_message == "LOGICAL_TARGET_HIT_TEST_FAILED"
    assert events == ["start", "fast_review:1", "turn", "fast_review:2", "close"]


@pytest.mark.asyncio
async def test_executor_does_not_fast_review_while_exploration_work_is_claimable() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=20)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("fast_review")
            return module.H5FastReviewSnapshot(
                coverage_revision=10,
                assessment_scope_ready=False,
                completion_ready=False,
                inventory_item_count=0,
                blocking_scope_count=1,
                claimable_work_count=1,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("turn")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            assert task_id == "task-h5"
            assert session_id == "h5:run-h5"
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert events == ["start", "fast_review", "turn", "fast_review", "close"]


@pytest.mark.asyncio
async def test_executor_does_not_fast_review_after_page_coverage_exists() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=20)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("fast_review")
            return module.H5FastReviewSnapshot(
                coverage_revision=10,
                assessment_scope_ready=False,
                completion_ready=False,
                inventory_item_count=0,
                covered_page_type_count=2,
                blocking_scope_count=1,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("turn")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            assert task_id == "task-h5"
            assert session_id == "h5:run-h5"
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert events == ["start", "fast_review", "turn", "fast_review", "close"]


@pytest.mark.asyncio
async def test_executor_fast_reviews_blocked_h5_coverage_scope() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=20)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("fast_review")
            return module.H5FastReviewSnapshot(
                coverage_revision=11,
                assessment_scope_ready=False,
                completion_ready=False,
                inventory_item_count=6,
                blocking_scope_count=2,
                active_work_count=0,
                claimable_work_count=0,
                retry_waiting_work_count=0,
                exhausted_work_count=3,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            raise AssertionError("覆盖范围已锁定但存在阻塞 scope 时不得继续卡轮询")

        async def close(self, *, task_id, session_id):
            assert task_id == "task-h5"
            assert session_id == "h5:run-h5"
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == module.H5_COVERAGE_BLOCKED_SCOPE_REVIEW_CODE
    assert "阻塞终态" in str(result.final_text)
    assert events == ["start", "fast_review", "close"]


@pytest.mark.asyncio
async def test_executor_fast_reviews_h5_work_dead_end() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=20)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("fast_review")
            return module.H5FastReviewSnapshot(
                coverage_revision=13,
                assessment_scope_ready=True,
                completion_ready=False,
                inventory_item_count=6,
                blocking_scope_count=0,
                active_work_count=0,
                claimable_work_count=0,
                retry_waiting_work_count=0,
                exhausted_work_count=0,
                settled_work_count=9,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            raise AssertionError("探索工作已耗尽时不得继续等待新工作")

        async def close(self, *, task_id, session_id):
            assert task_id == "task-h5"
            assert session_id == "h5:run-h5"
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == module.H5_COVERAGE_WORK_DEAD_END_REVIEW_CODE
    assert "工作已耗尽" in str(result.final_text)
    assert events == ["start", "fast_review", "close"]


def test_h5_fast_review_does_not_flag_dead_end_after_target_coverage_ready() -> None:
    module = _runtime_module()

    snapshot = module.H5FastReviewSnapshot(
        coverage_revision=14,
        assessment_scope_ready=True,
        completion_ready=False,
        target_coverage_ready=True,
        inventory_item_count=6,
        blocking_scope_count=1,
        active_work_count=0,
        claimable_work_count=0,
        retry_waiting_work_count=0,
        exhausted_work_count=1,
        settled_work_count=9,
    )

    assert snapshot.fast_review_error_code is None
    assert snapshot.blocked_scope_requires_review is False
    assert snapshot.work_dead_end_requires_review is False


@pytest.mark.parametrize(
    "remaining_work",
    [
        {"active_work_count": 1},
        {"claimable_work_count": 1},
        {"retry_waiting_work_count": 1},
    ],
    ids=["active", "claimable", "retry-waiting"],
)
def test_h5_fast_review_keeps_exploring_without_inventory_while_work_remains(
    remaining_work: dict[str, int],
) -> None:
    """无库存不等于探索耗尽，仍有工作时不得提前转人工复核。"""
    module = _runtime_module()
    snapshot = module.H5FastReviewSnapshot(
        coverage_revision=15,
        assessment_scope_ready=False,
        completion_ready=False,
        inventory_item_count=0,
        blocking_scope_count=1,
        **remaining_work,
    )

    assert snapshot.coverage_unready_without_inventory is False
    assert snapshot.fast_review_error_code is None


@pytest.mark.asyncio
async def test_executor_converts_lease_lost_without_next_claim_to_review() -> None:
    module = _runtime_module()
    events: list[str] = []
    fast_review_calls = 0

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=20)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            nonlocal fast_review_calls
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            fast_review_calls += 1
            events.append(f"fast_review:{fast_review_calls}")
            if fast_review_calls == 1:
                return None
            return module.H5FastReviewSnapshot(
                coverage_revision=14,
                assessment_scope_ready=True,
                completion_ready=False,
                inventory_item_count=3,
                blocking_scope_count=0,
                active_work_count=0,
                claimable_work_count=0,
                retry_waiting_work_count=0,
                exhausted_work_count=1,
                settled_work_count=2,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("turn")
            raise AppExplorationLeaseLostError("租约已丢失")

        async def next_exploration_claim_at(self, *, task_id):
            assert task_id == "task-h5"
            events.append("next_claim")
            return None

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == module.H5_COVERAGE_WORK_DEAD_END_REVIEW_CODE
    assert events == [
        "start",
        "fast_review:1",
        "turn",
        "next_claim",
        "fast_review:2",
        "close",
    ]


@pytest.mark.asyncio
async def test_executor_marks_review_when_action_budget_exhausted_with_successor() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=1)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("turn")
            return H5ExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(is_terminal=False),
                successor_work_id="work-2",
            )

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == module.H5_ACTION_BUDGET_EXHAUSTED_REVIEW_CODE
    assert "动作预算" in str(result.final_text)
    assert events == ["start", "turn", "close"]


@pytest.mark.asyncio
async def test_executor_continues_when_blocked_scope_still_has_claimable_work() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("fast_review")
            return module.H5FastReviewSnapshot(
                coverage_revision=12,
                assessment_scope_ready=False,
                completion_ready=False,
                inventory_item_count=6,
                blocking_scope_count=2,
                active_work_count=0,
                claimable_work_count=1,
                retry_waiting_work_count=0,
                exhausted_work_count=0,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("turn")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert events == ["start", "fast_review", "turn", "fast_review", "close"]


@pytest.mark.asyncio
async def test_executor_keeps_exploring_unready_coverage_without_blocking_scope() -> None:
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command
            events.append("start")
            return SimpleNamespace(work=SimpleNamespace(work_id="work-1"), auth_result=None)

        async def read_fast_review_snapshot(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            events.append("fast_review")
            return module.H5FastReviewSnapshot(
                coverage_revision=3,
                assessment_scope_ready=False,
                completion_ready=False,
                inventory_item_count=0,
                blocking_scope_count=0,
            )

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            events.append("turn")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert events == ["start", "fast_review", "turn", "fast_review", "close"]


@pytest.mark.asyncio
async def test_executor_closes_session_and_returns_review_when_turn_fails() -> None:
    module = _runtime_module()
    calls: list[object] = []

    class Runtime:
        async def start(self, command):
            calls.append("start")

        async def run_one_turn(self, *, task_id, agent_run_id):
            raise RuntimeError("browser disconnected")

        async def close(self, *, task_id, session_id):
            calls.append(("close", task_id, session_id))

    runtime = Runtime()
    runtime.runtime_settings = _runtime_settings(max_actions=2)
    result = await module.ProductionH5V2RuntimeExecutor(worker_instance_id="worker-h5").run(
        runtime,
        record=_h5_record(),
        settings=Settings(),
    )

    assert calls[-1] == ("close", "task-h5", "h5:run-h5")
    assert result.termination_reason is TerminationReason.ERROR
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert "browser disconnected" in str(result.error_message)


@pytest.mark.asyncio
async def test_executor_treats_cancellation_during_drain_as_terminal_authority(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """取消与动作失败竞态时，取消事实必须覆盖错误包装且不得污染错误日志。"""

    from services.api.app.application.h5_exploration_cancellation import (
        H5ExplorationCanceled,
    )

    module = _runtime_module()
    events: list[tuple[str, str]] = []
    caplog.set_level("ERROR", logger=module.__name__)

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code="interaction_failed",
                message="动作执行未完成",
                cause_type="ActionExecutionPersistenceError",
            )

        async def close(self, *, task_id, session_id):
            events.append((task_id, session_id))

    class Drainer:
        async def drain_before_close(self, *, task_id, agent_run_id, deadline):
            del task_id, agent_run_id, deadline
            raise H5ExplorationCanceled(stage="active_session_inspection_drain")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        active_session_inspection=Drainer(),
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.CANCELED
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert events == [("task-h5", "h5:run-h5")]
    assert not caplog.records


@pytest.mark.asyncio
async def test_executor_records_safe_root_cause_fingerprint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _runtime_module()
    caplog.set_level("ERROR", logger=module.__name__)

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            try:
                raise OSError(5, "Input/output error at /secret/evidence/path")
            except OSError as exc:
                raise CaptureCoordinationError(
                    stage=CaptureCoordinationStage.BEFORE_CAPTURE,
                    error_code="before_capture_failed",
                    message="动作前 Capture 未完成",
                    cause_type=type(exc).__name__,
                ) from exc

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.error_message == (
        "动作前 Capture 未完成 "
        "[stage=before_capture, error_code=before_capture_failed, "
        "cause_type=OSError, errno=5]"
    )
    assert "/secret/evidence/path" not in str(result.error_message)
    assert any(
        "task_id=task-h5" in record.getMessage()
        and "run_id=run-h5" in record.getMessage()
        and "stage=before_capture" in record.getMessage()
        and "error_code=before_capture_failed" in record.getMessage()
        and "cause_type=OSError" in record.getMessage()
        and "errno=5" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_executor_records_call_path_for_empty_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """空消息超时必须保留调用位置，但不记录局部变量或异常链敏感消息。"""
    module = _runtime_module()
    caplog.set_level("ERROR", logger=module.__name__)

    async def raise_empty_timeout():
        try:
            raise OSError("/secret/evidence/path")
        except OSError:
            raise TimeoutError() from None

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            await raise_empty_timeout()

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.ERROR
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert any(
        "call_path=" in record.getMessage()
        and "run_one_turn" in record.getMessage()
        and "raise_empty_timeout" in record.getMessage()
        for record in caplog.records
    )
    assert "/secret/evidence/path" not in caplog.text


@pytest.mark.asyncio
async def test_executor_reclaims_expired_lease_without_failing_whole_run() -> None:
    """宿主机暂停后旧执行必须停止，但同一 Work 可按 PG 租约时间安全重领。"""
    module = _runtime_module()
    events: list[str] = []
    reclaim_at = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
    turn_count = 0

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            nonlocal turn_count
            del task_id, agent_run_id
            turn_count += 1
            events.append(f"turn:{turn_count}")
            if turn_count == 1:
                raise AppExplorationLeaseLostError("App Explorer Work 租约已失效")
            return _completed_outcome()

        async def next_exploration_claim_at(self, *, task_id):
            assert task_id == "task-h5"
            events.append("next_claim_at")
            return reclaim_at

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        now=lambda: reclaim_at,
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert events == ["turn:1", "next_claim_at", "turn:2", "close"]
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.SUCCESS
    assert result.working_memory.step_count == 1


@pytest.mark.asyncio
async def test_executor_reviews_when_lost_lease_has_no_reclaim_schedule() -> None:
    """PG 不再给出可重领事实时不得伪造成功，也不得让任务长期 running。"""
    module = _runtime_module()
    events: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=2)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            raise AppExplorationLeaseLostError("App Explorer Work 租约已失效")

        async def next_exploration_claim_at(self, *, task_id):
            del task_id
            return None

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            events.append("close")

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert events == ["close"]
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == module.H5_EXPLORATION_NO_CLAIMABLE_WORK_REVIEW_CODE


@pytest.mark.asyncio
async def test_executor_stops_at_exploration_action_cap() -> None:
    """非终态探索后继不得突破锁定的总动作数。"""
    module = _runtime_module()
    calls: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=3)

        async def start(self, command):
            return None

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            calls.append("exploration")
            return H5ExplorationTurnOutcome(
                claimed=True,
                final_status=ExplorerWorkStatus.SUCCEEDED,
                turn=SimpleNamespace(is_terminal=False),
                successor_work_id=f"work-{len(calls)}",
            )

        async def close(self, *, task_id, session_id):
            return None

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert calls == [
        "exploration",
        "exploration",
        "exploration",
    ]
    assert result.working_memory.step_count == 3


@pytest.mark.asyncio
async def test_provider_rejects_invalid_policy_before_creating_browser_runtime() -> None:
    """若先创建浏览器再解析策略，非法 Run 会泄漏会话资源。"""
    module = _runtime_module()
    factory_called = False

    def capture_runtime_factory():
        nonlocal factory_called
        factory_called = True
        raise AssertionError("非法策略不应创建浏览器 Runtime")

    record = _h5_record()
    record.task_config["scan_mode"] = "unsupported"
    provider = module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5",
        capture_runtime_factory=capture_runtime_factory,
    )

    with pytest.raises(Exception, match="scan_mode"):
        await provider.build(record=record, settings=Settings())

    assert factory_called is False


@pytest.mark.asyncio
async def test_provider_wires_browser_loss_recovery_into_real_h5_runner() -> None:
    module = _runtime_module()
    capture_runtime = SimpleNamespace(
        session_command_gate=object(),
        h5_capture_recovery=object(),
        surface_driver=SimpleNamespace(),
        surface_sessions=SimpleNamespace(),
        coordinator=SimpleNamespace(),
        h5_exploration_coordinator=SimpleNamespace(),
    )
    provider = module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5",
        capture_runtime_factory=lambda **_: capture_runtime,
    )

    runtime = await provider.build(record=_h5_record(), settings=Settings())

    recovery = runtime.turn_runner._browser_loss_recovery
    assert isinstance(recovery, module.H5CoordinatorBrowserLossRecovery)
    assert recovery._capture_runtime is capture_runtime
    assert recovery._workflow is runtime.workflow


@pytest.mark.asyncio
async def test_frontier_recovery_recognizes_observation_direct_parent_for_back() -> None:
    """详情/子页的直接父状态必须允许同会话 back，而不是重新 navigate 来源 URL。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work(capture_id="capture-activity")
    source_state = "state:profile"
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile",
        source_state_version=source_state,
        source_location="https://m.example.test/mine",
        navigation_depth=1,
        parent_state_version="state:home",
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                location="https://m.example.test/signin",
                state_version="state:activity",
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "parent_state_version": source_state,
                },
            ),
        ),
        snapshot=snapshot,
    )
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
    browser = module._CoordinatorRecoveryBrowser(
        work=work,
        agent_run_id="run-h5",
        capture_coordinator=SimpleNamespace(),
        detector=SimpleNamespace(),
        assessment_repository=SimpleNamespace(),
        cancellation=SimpleNamespace(),
        now=lambda: work.frontier.observation.observed_at,
        source_state_version=source_state,
    )

    assert await browser.is_history_ancestor(
        source_location="https://m.example.test/mine",
        current_location="https://m.example.test/signin",
    )


@pytest.mark.asyncio
async def test_frontier_recovery_preserves_history_parent_across_detail_popup_actions() -> None:
    """详情内若处理过弹窗，列表仍是浏览器历史的直接父页，恢复必须 back。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work(capture_id="capture-video-detail-popup")
    source_state = "state:video-list"
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-video-list",
        source_state_version=source_state,
        source_location="https://m.example.test/movie",
        navigation_depth=1,
        parent_state_version="state:home",
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                location="https://m.example.test/movie/1",
                state_version="state:video-detail-popup-2",
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "parent_state_version": "state:video-detail-popup-1",
                    "history_parent_state_version": source_state,
                    "history_parent_location": "https://m.example.test/movie",
                },
            ),
        ),
        snapshot=snapshot,
    )
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
    commands = []

    class Coordinator:
        async def coordinate(self, command):
            commands.append(command)
            return SimpleNamespace()

    browser = module._CoordinatorRecoveryBrowser(
        work=work,
        agent_run_id="run-h5",
        capture_coordinator=Coordinator(),
        detector=SimpleNamespace(),
        assessment_repository=SimpleNamespace(),
        cancellation=module.H5CancellationCheckpoint(),
        now=lambda: work.frontier.observation.observed_at,
        source_state_version=source_state,
    )

    assert await browser.is_history_ancestor(
        source_location="https://m.example.test/movie",
        current_location="https://m.example.test/movie/1",
    )
    await browser.back()
    assert commands[0].intent.parameters["navigation_depth"] == 1


@pytest.mark.asyncio
async def test_frontier_recovery_uses_intermediate_parent_for_signed_global_navigation() -> None:
    """无底栏未知子页可先回到能重新验签全局底栏的同源历史父页。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work(capture_id="capture-ai-record")
    source_state = "state:home-root"
    candidate = CandidateAction(
        candidate_id="candidate-profile",
        semantic_target=("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible"),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["bottom_navigation", "我的", "profile"],
            "expected_next_page_type": "profile",
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 6,
                "navigation_label": "我的",
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-root",
        source_state_version=source_state,
        source_location="https://m.example.test/",
        navigation_depth=1,
        parent_state_version=None,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                location="https://m.example.test/aiRecord",
                state_version="state:ai-record",
                page_type=None,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "history_parent_state_version": "state:ai",
                    "history_parent_location": "https://m.example.test/ai",
                },
            ),
        ),
        snapshot=snapshot,
    )
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
    commands = []

    class Coordinator:
        async def coordinate(self, command):
            commands.append(command)
            return SimpleNamespace()

    browser = module._CoordinatorRecoveryBrowser(
        work=work,
        agent_run_id="run-h5",
        capture_coordinator=Coordinator(),
        detector=SimpleNamespace(),
        assessment_repository=SimpleNamespace(),
        cancellation=module.H5CancellationCheckpoint(),
        now=lambda: work.frontier.observation.observed_at,
        source_state_version=source_state,
    )

    assert await browser.is_history_ancestor(
        source_location="https://m.example.test/",
        current_location="https://m.example.test/aiRecord",
    )
    await browser.back()
    assert commands[0].intent.parameters["navigation_depth"] == 1


@pytest.mark.asyncio
async def test_h5_provider_disables_unused_exploration_wakeups() -> None:
    module = _runtime_module()
    capture_runtime = SimpleNamespace(
        session_command_gate=object(),
        h5_capture_recovery=object(),
        surface_driver=SimpleNamespace(),
        surface_sessions=SimpleNamespace(),
        coordinator=SimpleNamespace(),
        h5_exploration_coordinator=SimpleNamespace(),
    )
    provider = module.PostgresH5RuntimeProvider(
        worker_instance_id="worker-h5",
        capture_runtime_factory=lambda **_: capture_runtime,
    )

    runtime = await provider.build(record=_h5_record(), settings=Settings())

    publisher = runtime.workflow._orchestrator._exploration_publisher
    assert publisher._emit_wakeup is False
    assert runtime.turn_runner._emit_wakeup is False


def test_h5_handler_is_runtime_backed_and_does_not_require_main_agent() -> None:
    module = _runtime_module()
    handler = module.build_h5_workflow_handler(
        Settings.model_validate({"workflow": {"h5_v2_mode": "on"}}),
        "worker-h5",
        provider=SimpleNamespace(build=None),
        executor=SimpleNamespace(run=None),
    )

    assert handler.__class__.__name__ == "RuntimeBackedH5WorkflowHandler"


@pytest.mark.parametrize("enabled", [False, True])
def test_h5_handler_contract_ignores_supplemental_probe_setting(
    enabled: bool,
) -> None:
    module = _runtime_module()
    handler = module.build_h5_workflow_handler(
        Settings.model_validate(
            {
                "workflow": {"h5_v2_mode": "on"},
                "supplemental_evidence": {"enabled": enabled},
            }
        ),
        "worker-h5",
        provider=SimpleNamespace(build=None),
    )

    assert not hasattr(handler._executor, "_probe_claim_enabled")


def _popup_loop_candidate(sequence: int, *, source_page_type: str) -> CandidateAction:
    signature = f"h5-obstruction:{sequence:064x}"
    capability = "dialog_content_close"
    return CandidateAction(
        candidate_id=f"candidate-popup-loop-{sequence}",
        semantic_target="关闭当前弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["popup_dismissal", "close_control", capability],
            "source_page_type": source_page_type,
            "expected_next_page_type": source_page_type,
            "obstruction_signature": signature,
            "action_parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": capability,
                "obstruction_signature": signature,
            },
        },
    )


def _popup_loop_work(
    *,
    completed_count: int,
    current_page_type: str = "home",
    historical_page_type: str = "home",
    current_popup: bool = True,
):
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    work = _work()
    location = work.frontier.observation.location
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for sequence in range(1, completed_count + 1):
        candidate = _popup_loop_candidate(
            sequence,
            source_page_type=historical_page_type,
        )
        observed_at = datetime(2026, 8, 15, 9, 0, tzinfo=UTC) + timedelta(seconds=sequence * 2)
        snapshot = snapshot.merge(
            source_capture_id=f"capture-popup-loop-{sequence}",
            source_state_version=f"state-popup-loop-{sequence}",
            source_location=location,
            navigation_depth=0,
            candidates=(candidate,),
            now=observed_at,
        )
        entry = next(
            item
            for item in snapshot.entries
            if item.candidate.candidate_id == candidate.candidate_id
        )
        snapshot = snapshot.select_entry(entry.entry_id, selected_at=observed_at)
        snapshot = snapshot.mark_executed(
            entry.entry_id,
            finished_at=observed_at + timedelta(seconds=1),
        )
    current_candidate = (
        _popup_loop_candidate(
            completed_count + 1,
            source_page_type=current_page_type,
        )
        if current_popup
        else work.frontier.candidates[0]
    )
    if current_popup:
        observed_at = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
        snapshot = snapshot.merge(
            source_capture_id=work.source_capture_id,
            source_state_version=work.frontier.observation.state_version,
            source_location=location,
            navigation_depth=0,
            candidates=(current_candidate,),
            now=observed_at,
        )
        current_entry = next(
            item
            for item in snapshot.entries
            if item.candidate.candidate_id == current_candidate.candidate_id
        )
        snapshot = snapshot.select_entry(current_entry.entry_id, selected_at=observed_at)
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type=current_page_type,
            page_state="popup" if current_popup else "stable",
        ),
        candidates=(current_candidate,),
    )
    frontier = _with_task_frontier(frontier=frontier, snapshot=snapshot)
    return replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)


@pytest.mark.asyncio
async def test_h5_popup_loop_delegates_twentieth_attempt_after_nineteen_completed() -> None:
    module = _runtime_module()
    work = _popup_loop_work(completed_count=19)
    calls = 0

    class Delegate:
        async def decide_next(self, delegated_work):
            nonlocal calls
            calls += 1
            assert delegated_work is work
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason="delegate_called",
            )

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert calls == 1
    assert decision.reason == "delegate_called"


@pytest.mark.asyncio
async def test_h5_popup_loop_delegates_after_twenty_successful_layers() -> None:
    module = _runtime_module()
    work = _popup_loop_work(completed_count=20)

    class Delegate:
        async def decide_next(self, delegated_work):
            assert delegated_work is work
            return ExplorationTerminal(
                work_id=work.work_id, task_id=work.task_id,
                status=ExplorationTerminalStatus.BOUNDED, reason="delegate_called",
            )

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.status is ExplorationTerminalStatus.BOUNDED
    assert decision.reason == "delegate_called"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_page_type", "historical_page_type"),
    [("startup_ad", "startup_ad"), ("profile", "home")],
)
async def test_h5_popup_loop_keeps_transient_or_different_page_type_delegated(
    current_page_type: str,
    historical_page_type: str,
) -> None:
    module = _runtime_module()
    work = _popup_loop_work(
        completed_count=20,
        current_page_type=current_page_type,
        historical_page_type=historical_page_type,
    )

    class Delegate:
        async def decide_next(self, delegated_work):
            return ExplorationTerminal(
                work_id=delegated_work.work_id,
                task_id=delegated_work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason="delegate_called",
            )

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.reason == "delegate_called"


@pytest.mark.asyncio
async def test_h5_popup_loop_does_not_activate_without_current_popup_candidate() -> None:
    module = _runtime_module()
    work = _popup_loop_work(completed_count=20, current_popup=False)

    class Delegate:
        async def decide_next(self, delegated_work):
            return ExplorationTerminal(
                work_id=delegated_work.work_id,
                task_id=delegated_work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason="delegate_called",
            )

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.status is ExplorationTerminalStatus.COMPLETED
    assert decision.outcome == ExplorationTerminalStatus.COMPLETED.value
    assert decision.reason_code == module.H5_FRONTIER_CLEANLY_EXHAUSTED_REASON_CODE
    assert not decision.reason.startswith("popup_layer_limit_exceeded")


@pytest.mark.asyncio
async def test_h5_popup_dismissal_reobserve_limit_bounds_without_llm() -> None:
    """无关闭控件弹窗的失败重观察跨后继累计达上限时，直接放弃该页不再调用 LLM。"""
    from services.api.app.application.h5_popup_loop_policy import (
        H5_POPUP_DISMISSAL_REOBSERVE_LIMIT,
        H5_POPUP_DISMISSAL_STALLED_REASON_CODE,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    observation = work.frontier.observation.__class__(
        capture_id=work.frontier.observation.capture_id,
        surface_kind=work.frontier.observation.surface_kind,
        location=work.frontier.observation.location,
        state_version=work.frontier.observation.state_version,
        observed_at=work.frontier.observation.observed_at,
        metadata={
            "popup_dismissal_reobserve_state": {
                "signature": "h5-obstruction:" + "a" * 64,
                "count": H5_POPUP_DISMISSAL_REOBSERVE_LIMIT,
            }
        },
    )
    frontier = work.frontier.__class__(
        snapshot_id="frontier-popup-stalled",
        observation=observation,
        candidates=work.frontier.candidates,
    )
    stalled_work = replace(
        work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    class Delegate:
        async def decide_next(self, work):
            raise AssertionError("弹窗关闭僵持达上限后不应再调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(stalled_work)

    assert decision.status is ExplorationTerminalStatus.BOUNDED
    assert decision.reason == (
        f"popup_dismissal_stalled:{H5_POPUP_DISMISSAL_REOBSERVE_LIMIT}"
        f"/{H5_POPUP_DISMISSAL_REOBSERVE_LIMIT}"
    )
    assert decision.outcome == ExplorationTerminalStatus.BOUNDED.value
    assert decision.reason_code == H5_POPUP_DISMISSAL_STALLED_REASON_CODE


@pytest.mark.asyncio
async def test_h5_completion_wrapper_finishes_empty_root_frontier_without_llm() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    observation = work.frontier.observation.__class__(
        capture_id=work.frontier.observation.capture_id,
        surface_kind=work.frontier.observation.surface_kind,
        location=work.frontier.observation.location,
        state_version=work.frontier.observation.state_version,
        observed_at=work.frontier.observation.observed_at,
        metadata={"objective_satisfied": True},
    )
    frontier = work.frontier.__class__(
        snapshot_id="frontier-empty-root",
        observation=observation,
        candidates=(),
    )
    completed_work = replace(
        work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    class Delegate:
        async def decide_next(self, work):
            raise AssertionError("根 Frontier 完成信号不应再调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(
        completed_work
    )

    assert decision.status is ExplorationTerminalStatus.COMPLETED
    assert decision.outcome == ExplorationTerminalStatus.COMPLETED.value
    assert decision.reason_code == module.H5_FRONTIER_CLEANLY_EXHAUSTED_REASON_CODE


@pytest.mark.asyncio
async def test_h5_completion_wrapper_keeps_exploring_safe_candidates_after_target_covered() -> None:
    """规则覆盖完成不等于探索完成，剩余安全分支仍应交给 Explorer。"""

    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            overlay_signature=None,
            metadata={
                **work.frontier.observation.metadata,
                "objective_satisfied": True,
                "requires_auth": False,
            },
        ),
    )
    covered_work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    calls = 0

    class Delegate:
        async def decide_next(self, delegated_work):
            nonlocal calls
            calls += 1
            return ExplorationTerminal(
                work_id=delegated_work.work_id,
                task_id=delegated_work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason="delegate_called",
            )

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(covered_work)

    assert calls == 1
    assert decision.status is ExplorationTerminalStatus.BOUNDED
    assert decision.reason == "delegate_called"


@pytest.mark.asyncio
async def test_h5_completion_wrapper_completes_clean_frontier_without_coverage_objective() -> None:
    """探索已干净耗尽时独立完成，覆盖不足留给 Completion Gate。"""

    module = _runtime_module()
    work = _popup_loop_work(completed_count=1, current_popup=False)
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **work.frontier.observation.metadata,
                    "objective_satisfied": False,
                },
            ),
        ),
    )
    calls = 0

    class Delegate:
        async def decide_next(self, delegated_work):
            nonlocal calls
            calls += 1
            return ExplorationTerminal(
                work_id=delegated_work.work_id,
                task_id=delegated_work.task_id,
                status=ExplorationTerminalStatus.COMPLETED,
                reason="delegate_completed",
            )

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.status is ExplorationTerminalStatus.COMPLETED
    assert decision.reason_code == module.H5_FRONTIER_CLEANLY_EXHAUSTED_REASON_CODE
    assert calls == 0


@pytest.mark.asyncio
async def test_h5_completion_wrapper_completes_empty_signed_frontier_without_llm() -> None:
    """根页面没有任何安全候选也是终态事实，不能退化为通用 no-candidate。"""

    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["home", "profile"]},
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    frontier = replace(
        frontier,
        candidates=(),
        observation=replace(
            frontier.observation,
            metadata={**frontier.observation.metadata, "objective_satisfied": False},
        ),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("空的受签 Frontier 不应调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.status is ExplorationTerminalStatus.COMPLETED
    assert decision.reason_code == module.H5_FRONTIER_CLEANLY_EXHAUSTED_REASON_CODE


@pytest.mark.asyncio
async def test_h5_completion_blocks_empty_frontier_with_active_obstruction() -> None:
    """活动遮挡下没有安全候选时必须显式阻断，不能退化为平台候选异常。"""

    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=3)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    frontier = replace(
        frontier,
        candidates=(),
        observation=replace(
            frontier.observation,
            overlay_signature="h5-obstruction:" + "a" * 64,
            metadata={
                **frontier.observation.metadata,
                "objective_satisfied": False,
            },
        ),
    )
    blocked_work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("活动遮挡下的空 Frontier 不应进入平台 Explorer")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(blocked_work)

    assert decision.status is ExplorationTerminalStatus.BLOCKED
    assert decision.outcome == ExplorationTerminalStatus.BLOCKED.value
    assert decision.reason_code == module.H5_FRONTIER_EMPTY_WITH_ACTIVE_OBSTRUCTION_REASON_CODE


@pytest.mark.asyncio
async def test_h5_completion_bounds_exhausted_capability_projection_reobserve() -> None:
    """能力合同连续拒绝达到上限后必须输出专门终态，不再调用模型。"""
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            metadata={
                **dict(work.frontier.observation.metadata),
                "capability_projection_reobserve_exhausted": True,
            },
        ),
        candidates=(),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("能力投影重观察耗尽后不得再次调用 Explorer")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.status is ExplorationTerminalStatus.BOUNDED
    assert decision.reason_code == (module.H5_CAPABILITY_PROJECTION_REOBSERVE_EXHAUSTED_REASON_CODE)


@pytest.mark.asyncio
async def test_h5_completion_wrapper_reports_failed_only_frontier_explicitly() -> None:
    """失败历史已经没有可续跑 sibling 时应给出稳定根因，不得声称仍未耗尽。"""

    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=3).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_failed(
        entry.entry_id,
        reason="arrival_page_type_unverified",
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    frontier = replace(frontier, candidates=())
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("全失败 Frontier 不应调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.status is ExplorationTerminalStatus.BOUNDED
    assert decision.reason == "task_frontier_objective_not_satisfied:frontier_failed"


@pytest.mark.asyncio
async def test_h5_completion_wrapper_treats_page_budget_as_bounded_not_blocked() -> None:
    """max_pages 是用户配置的采样边界，不得污染真实页面阻断统计。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(work.frontier.candidates[0],),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_blocked(
        entry.entry_id,
        reason="max_pages_reached",
    )
    frontier = replace(
        _with_task_frontier(frontier=work.frontier, snapshot=snapshot),
        candidates=(),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("页面预算耗尽后不应再次调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.status is ExplorationTerminalStatus.BOUNDED
    assert decision.reason_code == module.H5_FRONTIER_PAGE_BUDGET_EXHAUSTED_REASON_CODE


@pytest.mark.asyncio
async def test_h5_completion_wrapper_blocks_auth_frontier_without_safe_candidate() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    observation = work.frontier.observation.__class__(
        capture_id=work.frontier.observation.capture_id,
        surface_kind=work.frontier.observation.surface_kind,
        location=work.frontier.observation.location,
        state_version=work.frontier.observation.state_version,
        observed_at=work.frontier.observation.observed_at,
        metadata={"requires_auth": True},
    )
    frontier = work.frontier.__class__(
        snapshot_id="frontier-auth-blocked",
        observation=observation,
        candidates=(),
    )
    auth_work = replace(
        work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    class Delegate:
        async def decide_next(self, work):
            raise AssertionError("认证阻断信号不应再调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(auth_work)

    assert decision.status is ExplorationTerminalStatus.BLOCKED
    assert "requires_auth" in decision.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("objective_satisfied", "expected_status", "expected_reason"),
    [
        (True, ExplorationTerminalStatus.BLOCKED, "task_frontier_blocked"),
        (False, ExplorationTerminalStatus.BLOCKED, "task_frontier_blocked"),
    ],
)
async def test_h5_completion_wrapper_handles_high_risk_boundary_by_coverage(
    objective_satisfied: bool,
    expected_status: ExplorationTerminalStatus,
    expected_reason: str,
) -> None:
    """高风险候选必须留痕并阻断完成，不得由覆盖目标降级为普通跳过。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    candidate = replace(
        work.frontier.candidates[0],
        risk_level=InteractionRiskLevel.HIGH,
        semantic_target="上传脸部图片",
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            metadata={
                **frontier.observation.metadata,
                "objective_satisfied": objective_satisfied,
            },
        ),
    )
    completed_work = replace(
        work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("高风险候选耗尽不应再调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(
        completed_work
    )

    assert decision.status is expected_status
    assert expected_reason in decision.reason
    assert snapshot.blocked_count == 1
    assert snapshot.entries[0].status is H5FrontierEntryStatus.BLOCKED
    assert snapshot.entries[0].last_failure_reason == "risk_level_not_allowed"


@pytest.mark.asyncio
async def test_h5_completion_wrapper_preserves_legacy_blocked_high_risk_boundary() -> None:
    """历史快照中的高风险阻断也不得被新 Worker 提升为完成。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(work.frontier.candidates[0],),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_blocked(
        entry.entry_id,
        reason="risk_level_not_allowed",
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            metadata={**frontier.observation.metadata, "objective_satisfied": True},
        ),
    )
    completed_work = replace(
        work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("历史高风险快照不应重新调用 LLM")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(
        completed_work
    )

    assert snapshot.blocked_count == 1
    assert decision.status is ExplorationTerminalStatus.BLOCKED
    assert decision.reason == "task_frontier_blocked"


@pytest.mark.asyncio
async def test_h5_completion_rejects_candidate_other_than_selected_frontier_entry() -> None:
    """若取消候选绑定校验，模型可把合法但未选中的动作交给 Coordinator。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _action_intent, _work

    module = _runtime_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Delegate:
        async def decide_next(self, work):
            return replace(_action_intent(work), parameters={"candidate_id": "other-candidate"})

    with pytest.raises(ValueError, match="selected H5 Frontier"):
        await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)


def _selected_h5_fallback_work(
    *,
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW,
):
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    work = _work()
    candidate = replace(work.frontier.candidates[0], risk_level=risk_level)
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    return replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)


@pytest.mark.asyncio
async def test_platform_selected_explorer_executes_signed_candidate_without_model() -> None:
    module = _runtime_module()
    work = _selected_h5_fallback_work()

    decision = await module.H5PlatformSelectedExplorer().decide_next(work)

    assert decision.parameters["candidate_id"] == work.frontier.candidates[0].candidate_id
    assert decision.intent_id.startswith("h5-platform-fallback:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "risk_level",
    [InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM],
)
async def test_h5_premature_terminal_falls_back_to_unique_platform_candidate(
    risk_level: InteractionRiskLevel,
) -> None:
    module = _runtime_module()
    work = _selected_h5_fallback_work(risk_level=risk_level)
    candidate = work.frontier.candidates[0]

    class Delegate:
        async def decide_next(self, delegated_work):
            assert delegated_work is work
            raise module.ExplorerPrematureTerminalError("模型错误提前收口")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.parameters["candidate_id"] == candidate.candidate_id
    assert decision.parameters["viewport_width"] == 390
    assert decision.parameters["viewport_height"] == 844
    assert decision.intent_id.startswith("h5-platform-fallback:")
    assert decision.idempotency_key == decision.intent_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "risk_level",
    [InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM],
)
async def test_h5_model_unavailable_falls_back_to_unique_platform_candidate(
    risk_level: InteractionRiskLevel,
) -> None:
    module = _runtime_module()
    work = _selected_h5_fallback_work(risk_level=risk_level)
    candidate = work.frontier.candidates[0]

    class Delegate:
        async def decide_next(self, delegated_work):
            assert delegated_work is work
            raise module.ExplorerModelUnavailableError("上游模型服务不可用")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.parameters["candidate_id"] == candidate.candidate_id
    assert decision.intent_id.startswith("h5-platform-fallback:")


@pytest.mark.asyncio
async def test_h5_model_unavailable_keeps_high_risk_candidate_closed() -> None:
    module = _runtime_module()
    work = _selected_h5_fallback_work()
    candidate = replace(
        work.frontier.candidates[0],
        risk_level=InteractionRiskLevel.HIGH,
    )
    work = replace(work, frontier=replace(work.frontier, candidates=(candidate,)))

    class Delegate:
        async def decide_next(self, delegated_work):
            assert delegated_work is work
            raise module.ExplorerModelUnavailableError("上游模型服务不可用")

    with pytest.raises(module.ExplorerModelUnavailableError, match="模型服务不可用"):
        await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_case",
    ["multiple_candidates", "blocked", "high_risk", "selected_entry_mismatch"],
)
async def test_h5_premature_terminal_keeps_invalid_fallback_closed(
    invalid_case: str,
) -> None:
    module = _runtime_module()
    work = _selected_h5_fallback_work()
    candidate = work.frontier.candidates[0]
    frontier = work.frontier
    if invalid_case == "multiple_candidates":
        other = replace(candidate, candidate_id="candidate-other", semantic_target="其他入口")
        frontier = replace(frontier, candidates=(candidate, other))
    elif invalid_case == "blocked":
        frontier = replace(frontier, blocked_candidate_ids=(candidate.candidate_id,))
    elif invalid_case == "high_risk":
        frontier = replace(
            frontier,
            candidates=(replace(candidate, risk_level=InteractionRiskLevel.HIGH),),
        )
    else:
        frontier = replace(
            frontier,
            observation=replace(
                frontier.observation,
                metadata={
                    **frontier.observation.metadata,
                    "h5_task_frontier_selected_entry_id": "wrong-entry",
                },
            ),
        )
    invalid_work = replace(work, frontier=frontier)

    class Delegate:
        async def decide_next(self, delegated_work):
            assert delegated_work is invalid_work
            raise module.ExplorerPrematureTerminalError("模型错误提前收口")

    with pytest.raises(module.ExplorerPrematureTerminalError, match="错误提前收口"):
        await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(invalid_work)


@pytest.mark.asyncio
async def test_h5_completion_wrapper_falls_back_for_incomplete_model_output() -> None:
    from services.api.app.agents.explorer import ExplorerAgentOutputError

    module = _runtime_module()
    work = _selected_h5_fallback_work()

    class Delegate:
        async def decide_next(self, delegated_work):
            assert delegated_work is work
            raise ExplorerAgentOutputError("通用结构错误")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision.parameters["explorer_decision_fallback"] == {
        "schema_version": "h5-explorer-decision-fallback.v1",
        "reason_code": "H5_EXPLORER_OUTPUT_INVALID",
    }


@pytest.mark.asyncio
async def test_h5_completion_does_not_fallback_without_unique_signed_candidate() -> None:
    from services.api.app.agents.explorer import ExplorerAgentOutputError
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()

    class Delegate:
        async def decide_next(self, delegated_work):
            assert delegated_work is work
            raise ExplorerAgentOutputError("通用结构错误")

    with pytest.raises(ExplorerAgentOutputError, match="通用结构错误"):
        await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parameter_updates", "parameter_removals", "intent_updates", "expected"),
    [
        ({}, (), {"action_type": InteractionOperation.CLICK}, "action_type"),
        ({}, (), {"semantic_target": "个人中心"}, "semantic_target"),
        (
            {"destination_url": "https://m.example.test/other"},
            (),
            {},
            "parameters",
        ),
        (
            {"frontier_snapshot_id": "h5-task-frontier:wrong"},
            (),
            {},
            "parameters",
        ),
        (
            {"navigation_depth": 2},
            (),
            {},
            "navigation_depth",
        ),
        (
            {},
            (),
            {"risk_level": InteractionRiskLevel.MEDIUM},
            "risk_level",
        ),
        (
            {"clear_obstructions": {"dismiss": True}},
            (),
            {},
            "parameters",
        ),
        (
            {"capture_screenshot": False},
            (),
            {},
            "parameters",
        ),
        (
            {"target_selector": "#pay-now"},
            (),
            {},
            "parameters",
        ),
        (
            {
                "explorer_decision_fallback": {
                    "schema_version": "h5-explorer-decision-fallback.v1",
                    "reason_code": "H5_EXPLORER_OUTPUT_INVALID",
                }
            },
            (),
            {},
            "模型降级审计",
        ),
        (
            {},
            ("expected_next_page_type",),
            {},
            "parameters",
        ),
        ({}, (), {"purpose": "提交订单"}, "purpose"),
    ],
    ids=(
        "wrong-action-type",
        "wrong-semantic-target",
        "wrong-destination-url",
        "wrong-frontier-snapshot",
        "wrong-navigation-depth",
        "wrong-risk-level",
        "nested-clear-obstructions",
        "capture-screenshot-override",
        "semantic-alias-key",
        "forged-model-fallback-audit",
        "missing-expected-page-type",
        "wrong-purpose",
    ),
)
async def test_h5_completion_wrapper_rejects_selected_id_with_changed_action_semantics(
    parameter_updates,
    parameter_removals,
    intent_updates,
    expected,
) -> None:
    """同一 candidate_id 改变操作语义时不得跨过 H5 Frontier 的执行边界。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _action_intent, _work

    module = _runtime_module()
    work = _work()
    candidate = CandidateAction(
        candidate_id="candidate-navigate",
        semantic_target="商品详情",
        action_type=InteractionOperation.NAVIGATE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "destination_url": "https://m.example.test/product/1",
            "expected_next_page_type": "product_detail",
            "purpose": "查看商品详情",
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, candidates=(candidate,)),
        snapshot=snapshot,
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    intent = replace(
        _action_intent(work),
        action_type=InteractionOperation.NAVIGATE,
        semantic_target=candidate.semantic_target,
        risk_level=candidate.risk_level,
        purpose=candidate.metadata["purpose"],
        parameters={
            "candidate_id": candidate.candidate_id,
            "frontier_snapshot_id": work.frontier_snapshot_id,
            "navigation_depth": candidate.navigation_depth,
            "destination_url": candidate.metadata["destination_url"],
            "expected_next_page_type": candidate.metadata["expected_next_page_type"],
        },
    )
    parameters = dict(intent.parameters)
    for key in parameter_removals:
        parameters.pop(key)
    parameters.update(parameter_updates)
    intent = replace(intent, parameters=parameters, **intent_updates)
    delegate_called = False

    class Delegate:
        async def decide_next(self, work):
            nonlocal delegate_called
            delegate_called = True
            return intent

    with pytest.raises(ValueError, match=expected):
        await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert delegate_called is True


@pytest.mark.asyncio
async def test_h5_completion_wrapper_overrides_framework_intent_with_fixed_viewport() -> None:
    """通用 Agent 不得以默认桌面视口绕过 H5 应用层固定值。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _action_intent, _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-home",
        semantic_target="首页内容",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"purpose": "查看首页内容"},
    )
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, candidates=(candidate,)),
        snapshot=snapshot,
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    intent = replace(
        _action_intent(work),
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        risk_level=candidate.risk_level,
        purpose="查看首页内容",
        parameters={
            "candidate_id": candidate.candidate_id,
            "frontier_snapshot_id": work.frontier_snapshot_id,
            "navigation_depth": candidate.navigation_depth,
            "viewport_width": 1280,
            "viewport_height": 720,
        },
    )

    class Delegate:
        async def decide_next(self, work):
            return intent

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)

    assert decision == replace(
        intent,
        parameters={
            **dict(intent.parameters),
            "viewport_width": 390,
            "viewport_height": 844,
        },
    )


def test_h5_candidate_parameters_preserve_trusted_startup_ad_capability() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-startup-ad-dismissal",
        semantic_target="跳过",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "关闭开屏广告并在同一浏览器会话进入首页",
            "expected_next_page_type": "home",
            "target_keywords": [
                "startup_ad",
                "close_control",
                "startup_ad_dismissal",
            ],
            "action_parameters": {
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
                "obstruction_signature": "h5-obstruction:" + "4" * 64,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(work.frontier.observation, page_type="startup_ad"),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
        "obstruction_signature": "h5-obstruction:" + "4" * 64,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
        "expected_next_page_type": "home",
    }


@pytest.mark.parametrize("page_state", ["loading", "line_loading", "login_required"])
def test_h5_candidate_parameters_preserve_trusted_line_loading_reobserve(
    page_state: str,
) -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-line-loading-reobserve",
        semantic_target="重新观察线路加载结果",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待线路选择完成，并在当前浏览器会话重新采集页面。",
            "target_keywords": ["line_loading", "same_session_reobserve"],
            "line_loading_reobserve_attempt": 1,
            "action_parameters": {
                "target_mode": "line_loading_reobserve",
                "line_loading_reobserve_attempt": 1,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="line_loading",
            page_state=page_state,
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "line_loading_reobserve",
        "line_loading_reobserve_attempt": 1,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


def test_h5_candidate_parameters_preserve_structural_line_loading_login_retry() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    signature = "h5-obstruction:" + "a" * 64
    candidate = CandidateAction(
        candidate_id="candidate-line-loading-login-retry",
        semantic_target="重试线路页登录",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "重试线路页匿名登录并在当前浏览器会话重新采集",
            "target_keywords": [
                "line_loading_login_retry",
                "authentication_retry",
                "evidence:structure",
            ],
            "obstruction_signature": signature,
            "action_parameters": {
                "target_mode": "line_loading_login_retry",
                "line_loading_login_retry_capability": "line_loading_login_retry",
                "obstruction_signature": signature,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="line_loading",
            page_state="login_required",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "line_loading_login_retry",
        "line_loading_login_retry_capability": "line_loading_login_retry",
        "obstruction_signature": signature,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


@pytest.mark.parametrize(
    "page_state",
    [
        "countdown",
        "countdown_active",
        "stable",
        "ad_visible",
        "popup",
        "stable_ad_visible",
        "startup_ad_visible",
        "countdown_ad",
        "stable_ad_with_skip_countdown",
    ],
)
def test_h5_candidate_parameters_preserve_trusted_startup_ad_countdown_reobserve(
    page_state: str,
) -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-startup-ad-countdown-reobserve",
        semantic_target="重新观察启动广告倒计时",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待启动广告倒计时结束，并在当前浏览器会话重新采集页面。",
            "target_keywords": [
                "startup_ad",
                "countdown",
                "same_session_reobserve",
            ],
            "startup_ad_countdown_reobserve_attempt": 1,
            "action_parameters": {
                "target_mode": "startup_ad_countdown_reobserve",
                "startup_ad_countdown_reobserve_attempt": 1,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="startup_ad",
            page_state=page_state,
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "startup_ad_countdown_reobserve",
        "startup_ad_countdown_reobserve_attempt": 1,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


def test_h5_candidate_parameters_preserve_trusted_flutter_bootstrap_reobserve() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-flutter-bootstrap-reobserve",
        semantic_target="重新观察 Flutter 启动页",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待 Flutter 启动完成，并在当前浏览器会话重新采集页面。",
            "target_keywords": ["flutter_bootstrap", "same_session_reobserve"],
            "flutter_bootstrap_reobserve_attempt": 1,
            "action_parameters": {
                "target_mode": "flutter_bootstrap_reobserve",
                "flutter_bootstrap_reobserve_attempt": 1,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type=None,
            page_state="needs_more_observation",
            metadata={
                **dict(work.frontier.observation.metadata),
                "detection_status": "insufficient",
                "risk_flags": ["deterministic_flutter_bootstrap_pending"],
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "flutter_bootstrap_reobserve",
        "flutter_bootstrap_reobserve_attempt": 1,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


def test_h5_candidate_parameters_preserve_trusted_insufficient_reobserve() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-insufficient-reobserve",
        semantic_target="重新观察未稳定页面",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "等待页面稳定，并在当前浏览器会话重新采集页面。",
            "target_keywords": ["insufficient", "same_session_reobserve"],
            "insufficient_reobserve_attempt": 1,
            "action_parameters": {
                "target_mode": "insufficient_reobserve",
                "insufficient_reobserve_attempt": 1,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type=None,
            page_state="needs_more_observation",
            metadata={
                **work.frontier.observation.metadata,
                "detection_status": "insufficient",
                "risk_flags": [],
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "insufficient_reobserve",
        "insufficient_reobserve_attempt": 1,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


def test_h5_candidate_parameters_preserve_ambiguous_launch_shell_reobserve() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-ambiguous-launch-shell-reobserve",
        semantic_target="重新观察模糊启动壳层",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "启动壳层页型仍有冲突，在当前浏览器会话零触碰重新采集。",
            "target_keywords": [
                "ambiguous_launch_shell",
                "same_session_reobserve",
                "zero_touch",
            ],
            "ambiguous_launch_shell_reobserve_attempt": 1,
            "action_parameters": {
                "target_mode": "ambiguous_launch_shell_reobserve",
                "ambiguous_launch_shell_reobserve_attempt": 1,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type=None,
            page_state="loading",
            metadata={
                **work.frontier.observation.metadata,
                "detection_status": "ambiguous",
                "ambiguous_launch_shell": True,
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "ambiguous_launch_shell_reobserve",
        "ambiguous_launch_shell_reobserve_attempt": 1,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


def test_h5_candidate_parameters_preserve_trusted_logical_surface_reobserve() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    identity = "sha256:" + "1" * 64
    state = "sha256:" + "2" * 64
    candidate = CandidateAction(
        candidate_id="candidate-logical-reobserve",
        semantic_target="重新观察 SPA/Flutter 逻辑页面转换",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "复验 SPA/Flutter 逻辑页面转换",
            "target_keywords": ["logical_surface", "same_session_reobserve"],
            "logical_reobserve_attempt": 1,
            "source_identity_fingerprint": identity,
            "source_state_fingerprint": state,
            "action_parameters": {
                "target_mode": "logical_surface_reobserve",
                "logical_reobserve_attempt": 1,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            metadata={
                **work.frontier.observation.metadata,
                "logical_surface_identity_fingerprint": identity,
                "logical_surface_state_fingerprint": state,
                "logical_transition_proof": "uncertain",
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "logical_surface_reobserve",
        "logical_reobserve_attempt": 1,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 1,
    }


def test_h5_candidate_parameters_sign_logical_target_from_frontier_source() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    identity = "sha256:" + "3" * 64
    state = "sha256:" + "4" * 64
    candidate = CandidateAction(
        candidate_id="candidate-logical-target",
        semantic_target="我的",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "进入个人中心",
            "target_keywords": ["profile"],
            "expected_next_page_type": "profile",
            "source_page_type": "home",
            "source_identity_fingerprint": identity,
            "source_state_fingerprint": state,
            "logical_target_required": True,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            metadata={
                **work.frontier.observation.metadata,
                "logical_surface_identity_fingerprint": identity,
                "logical_surface_state_fingerprint": state,
                "logical_targets_effective": True,
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters["logical_target"] == {
        "protocol_version": "logical_target.v1",
        "source_identity_fingerprint": identity,
        "source_state_fingerprint": state,
        "source_page_type": "home",
        "expected_page_type": "profile",
        "expects_logical_navigation": True,
    }


@pytest.mark.parametrize("expected_page_type", [None, "video_list"])
def test_h5_scroll_parameters_require_observed_scroll_effect(expected_page_type) -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    identity = "sha256:" + "3" * 64
    state = "sha256:" + "4" * 64
    candidate = CandidateAction(
        candidate_id="candidate-scroll",
        semantic_target="向下滚动一屏查找视频标题",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_list",
            "expected_next_page_type": expected_page_type,
            "source_identity_fingerprint": identity,
            "source_state_fingerprint": state,
            "logical_target_required": True,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            metadata={
                **work.frontier.observation.metadata,
                "logical_surface_identity_fingerprint": identity,
                "logical_surface_state_fingerprint": state,
                "logical_targets_effective": True,
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters["logical_target"] == {
        "protocol_version": "logical_target.v1",
        "source_identity_fingerprint": identity,
        "source_state_fingerprint": state,
        "source_page_type": "video_list",
        "expected_page_type": expected_page_type,
        "expects_logical_navigation": False,
        "expects_scroll_effect": True,
    }


def test_h5_detail_scroll_parameters_keep_two_screen_capture_boundary() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-detail-scroll",
        semantic_target="采集详情页第二屏内容",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_detail",
            "source_page_state": "stable",
            "target_keywords": [
                "detail_scroll_coverage",
                "two_screen_sequence",
                "page_type:video_detail",
            ],
            "action_parameters": {
                "target_mode": "h5_detail_scroll_coverage",
                "scroll_max_segments": 2,
                "split_scroll_screenshots": True,
                "source_page_type": "video_detail",
            },
        },
    )
    work = _work()
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                page_type="video_detail",
                page_state="stable",
            ),
            candidates=(candidate,),
        ),
    )

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters["target_mode"] == "h5_detail_scroll_coverage"
    assert parameters["scroll_max_segments"] == 2
    assert parameters["split_scroll_screenshots"] is True


@pytest.mark.parametrize(
    ("operation", "target", "page_type", "keywords", "expected"),
    [
        (
            InteractionOperation.SCROLL, "采集列表至底部", "content_list",
            ["list_scroll_coverage", "bounded_to_bottom"],
            {"target_mode": "h5_list_scroll_coverage", "scroll_max_segments": 8,
             "split_scroll_screenshots": True, "source_page_type": "content_list"},
        ),
        (
            InteractionOperation.SCROLL, "采集列表至底部", "home",
            ["list_scroll_coverage", "bounded_to_bottom"],
            {"target_mode": "h5_list_scroll_coverage", "scroll_max_segments": 8,
             "split_scroll_screenshots": True, "source_page_type": "home"},
        ),
        (
            InteractionOperation.TIMED_CAPTURE, "采集视频播放多帧", "video_detail",
            ["video_timed_capture"],
            {"target_mode": "h5_video_timed_capture", "frame_count": 3,
             "interval_ms": 1000},
        ),
    ],
)
def test_h5_new_capture_candidates_require_exact_bounded_contract(
    operation, target, page_type, keywords, expected,
) -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-new-capture",
        semantic_target=target,
        action_type=operation,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"target_keywords": keywords, "action_parameters": expected},
    )
    work = _work()
    work = replace(work, frontier=replace(
        work.frontier,
        observation=replace(work.frontier.observation, page_type=page_type, page_state="stable"),
        candidates=(candidate,),
    ))
    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)
    assert all(parameters[key] == value for key, value in expected.items())
    altered = replace(candidate, metadata={
        "target_keywords": keywords,
        "action_parameters": {**expected, "scroll_max_segments": 99},
    })
    with pytest.raises(ValueError, match="有界采集能力无效"):
        module._h5_candidate_parameters(work=work, candidate=altered)


def test_h5_candidate_parameters_reports_stale_logical_source_as_retryable() -> None:
    """页面返回后旧候选失效时应重观察，不能把整条 Run 判为基础设施错误。"""
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    identity = "sha256:" + "5" * 64
    stale_state = "sha256:" + "6" * 64
    current_state = "sha256:" + "7" * 64
    candidate = CandidateAction(
        candidate_id="candidate-stale-logical-target",
        semantic_target="二次元",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "进入首页",
            "expected_next_page_type": "home",
            "source_page_type": "post_list",
            "source_identity_fingerprint": identity,
            "source_state_fingerprint": stale_state,
            "logical_target_required": True,
        },
    )
    work = _work(capture_id="capture-current-post-list")
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            metadata={
                **work.frontier.observation.metadata,
                "logical_surface_identity_fingerprint": identity,
                "logical_surface_state_fingerprint": current_state,
                "logical_targets_effective": True,
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    with pytest.raises(CaptureCoordinationError) as exc_info:
        module._h5_candidate_parameters(work=work, candidate=candidate)

    error = exc_info.value
    assert error.stage is CaptureCoordinationStage.ACTION
    assert error.error_code == "LOGICAL_SOURCE_STATE_STALE"
    assert error.retryable is True
    assert error.before_capture is not None
    assert error.before_capture.capture_id == work.source_capture_id
    assert error.interaction is not None
    assert error.interaction.status is InteractionResultStatus.BLOCKED
    assert error.interaction.retryable is True
    assert error.interaction.source_capture_id == work.source_capture_id


def _visual_navigation_enforced_work_and_candidate() -> tuple[Any, ...]:
    """构造由旧 Worker 在 enforced 模式持久化的视觉 Work。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work(capture_id="capture-visual-runtime")
    capability = build_visual_navigation_capability(
        source_task_id=work.task_id,
        source_capture_id=work.source_capture_id,
        source_artifact_id="artifact-visual-runtime",
        source_artifact_sha256="sha256:" + "1" * 64,
        source_identity_fingerprint="sha256:" + "2" * 64,
        source_state_fingerprint="sha256:" + "3" * 64,
        source_page_type="home",
        source_page_type_confidence=0.92,
        source_identity_bound=True,
        render_profile={
            "kind": "flutter_web",
            "status": "confirmed",
            "confidence": 0.96,
        },
        semantics_coverage_level="semantics_sparse",
        target_kind="content_card",
        label="内容卡片",
        expected_page_type="content_detail",
        visual_target_confidence=0.93,
        selected=False,
        unique=True,
        risk_flags=(),
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.3},
        source_image_size={"width": 390, "height": 844},
        viewport_metrics={
            "width": 390,
            "height": 844,
            "offset_left": 0,
            "offset_top": 0,
            "page_left": 0,
            "page_top": 0,
            "scale": 1,
        },
        model_image_transform={
            "kind": "identity",
            "scale_x": 1,
            "scale_y": 1,
            "offset_x": 0,
            "offset_y": 0,
        },
        local_visual_digest="sha256:" + "4" * 64,
        surrounding_anchor_digest="sha256:" + "5" * 64,
    )
    candidate = CandidateAction(
        candidate_id="candidate-visual-runtime",
        semantic_target="进入 content_detail 页面",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "受限视觉导航",
            "source_page_type": "home",
            "expected_next_page_type": "content_detail",
            "action_parameters": {
                "target_mode": "visual_navigation_coordinate",
                "visual_navigation_capability": capability,
            },
        },
    )
    enforced_frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="home",
            metadata={
                **work.frontier.observation.metadata,
                "h5_visual_coordinate_mode": "enforced",
                "logical_targets_effective": True,
                "logical_surface_identity_fingerprint": "sha256:" + "2" * 64,
                "logical_surface_state_fingerprint": "sha256:" + "3" * 64,
            },
        ),
        candidates=(candidate,),
    )
    enforced_work = replace(
        work,
        frontier_snapshot_id=enforced_frontier.snapshot_id,
        frontier=enforced_frontier,
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=enforced_work.source_capture_id,
        source_state_version=enforced_work.frontier.observation.state_version,
        source_location=enforced_work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    selected_frontier = _with_task_frontier(
        frontier=enforced_work.frontier,
        snapshot=snapshot,
    )
    enforced_work = replace(
        enforced_work,
        frontier_snapshot_id=selected_frontier.snapshot_id,
        frontier=selected_frontier,
    )

    return module, enforced_work, candidate, capability, enforced_frontier


@pytest.mark.asyncio
async def test_completion_aware_explorer_accepts_valid_enforced_visual_work() -> None:
    """组合根等价双层 Explorer 必须把合法视觉 ActionIntent 送到执行边界。"""
    module, enforced_work, candidate, capability, _ = (
        _visual_navigation_enforced_work_and_candidate()
    )
    explorer = module.H5CompletionAwareExplorer(
        delegate=module.H5PlatformSelectedExplorer(
            h5_visual_coordinate_mode="enforced",
        ),
        h5_visual_coordinate_mode="enforced",
    )

    decision = await explorer.decide_next(enforced_work)

    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == candidate.candidate_id
    assert (
        module.thaw_json_mapping(decision.parameters)["visual_navigation_capability"] == capability
    )


@pytest.mark.parametrize("current_mode", ["off", "shadow"])
@pytest.mark.asyncio
async def test_completion_aware_explorer_rejects_old_visual_work_when_not_enforced(
    current_mode: str,
) -> None:
    """双层 Explorer 的当前模式非 enforced 时仍须拒绝旧视觉 Work。"""
    module, enforced_work, _, _, _ = _visual_navigation_enforced_work_and_candidate()
    explorer = module.H5CompletionAwareExplorer(
        delegate=module.H5PlatformSelectedExplorer(
            h5_visual_coordinate_mode=current_mode,
        ),
        h5_visual_coordinate_mode=current_mode,
    )

    with pytest.raises(ValueError, match="视觉导航能力"):
        await explorer.decide_next(enforced_work)


def test_h5_candidate_parameters_preserve_visual_capability_only_when_enforced() -> None:
    """坐标能力必须完整到达 Resolver；非 enforced 时不得退化成普通文本点击。"""
    module, enforced_work, candidate, capability, enforced_frontier = (
        _visual_navigation_enforced_work_and_candidate()
    )

    verified = verify_visual_navigation_capability(capability)
    raw_parameters = candidate.metadata["action_parameters"]
    assert isinstance(raw_parameters, Mapping)
    assert raw_parameters["target_mode"] == "visual_navigation_coordinate"
    assert enforced_work.frontier.observation.metadata["h5_visual_coordinate_mode"] == "enforced"
    assert enforced_work.frontier.observation.metadata["logical_targets_effective"] is True
    nested_capability = raw_parameters["visual_navigation_capability"]
    assert isinstance(nested_capability, Mapping)
    nested_verified = verify_visual_navigation_capability(
        module.thaw_json_mapping(nested_capability)
    )
    assert nested_verified == verified
    assert module.VISUAL_TARGET_RISK_LEVELS.get(verified.target_kind) == "low"
    assert candidate.action_type is InteractionOperation.CLICK
    assert candidate.semantic_target == f"进入 {verified.expected_page_type} 页面"
    assert candidate.risk_level.value == "low"
    assert candidate.metadata["expected_next_page_type"] == verified.expected_page_type
    assert enforced_work.task_id == verified.source_task_id
    assert enforced_work.source_capture_id == verified.source_capture_id
    assert enforced_work.frontier.observation.page_type == verified.source_page_type
    assert (
        enforced_work.frontier.observation.metadata["logical_surface_identity_fingerprint"]
        == verified.source_identity_fingerprint
    )
    assert (
        enforced_work.frontier.observation.metadata["logical_surface_state_fingerprint"]
        == verified.source_state_fingerprint
    )
    assert module._trusted_visual_navigation_candidate_parameters(
        work=enforced_work,
        candidate=candidate,
        h5_visual_coordinate_mode="enforced",
    ) == {
        "target_mode": "visual_navigation_coordinate",
        "visual_navigation_capability": capability,
    }

    parameters = module._h5_candidate_parameters(
        work=enforced_work,
        candidate=candidate,
        h5_visual_coordinate_mode="enforced",
    )

    assert parameters["target_mode"] == "visual_navigation_coordinate"
    assert parameters["visual_navigation_capability"] == capability
    shadow_frontier = replace(
        enforced_frontier,
        observation=replace(
            enforced_frontier.observation,
            metadata={
                **enforced_frontier.observation.metadata,
                "h5_visual_coordinate_mode": "shadow",
            },
        ),
    )
    shadow_work = replace(
        enforced_work,
        frontier_snapshot_id=shadow_frontier.snapshot_id,
        frontier=shadow_frontier,
    )
    with pytest.raises(ValueError, match="视觉导航能力"):
        module._h5_candidate_parameters(
            work=shadow_work,
            candidate=candidate,
            h5_visual_coordinate_mode="enforced",
        )


@pytest.mark.parametrize("current_mode", ["off", "shadow"])
@pytest.mark.asyncio
async def test_platform_selected_explorer_rejects_persisted_visual_work_after_kill_switch(
    current_mode: str,
) -> None:
    """旧 enforced Work 在当前非 enforced 模式恢复时必须止于参数投影。"""
    module, enforced_work, _, _, _ = _visual_navigation_enforced_work_and_candidate()
    resolver_calls: list[str] = []
    browser_clicks: list[str] = []

    class Coordinator:
        async def coordinate(self, command):
            resolver_calls.append(command.intent.intent_id)
            browser_clicks.append(command.intent.intent_id)
            raise AssertionError("kill switch 后不得进入 Resolver/Browser")

    executor = module.ExplorerTurnExecutor(
        explorer=module.H5PlatformSelectedExplorer(
            h5_visual_coordinate_mode=current_mode,
        ),
        capture_coordinator=Coordinator(),
    )

    with pytest.raises(ValueError, match="视觉导航能力"):
        await executor.execute(work=enforced_work, agent_run_id="run-h5")

    assert resolver_calls == []
    assert browser_clicks == []


@pytest.mark.parametrize("tampered_target_mode", [None, "bottom_navigation"])
def test_h5_candidate_parameters_never_downgrade_visual_provenance_to_legacy(
    tampered_target_mode: str | None,
) -> None:
    """能力仍在时删除或改写 target_mode，必须拒绝而非投影文本点击。"""
    module, enforced_work, candidate, _, _ = _visual_navigation_enforced_work_and_candidate()
    raw = dict(candidate.metadata["action_parameters"])
    if tampered_target_mode is None:
        raw.pop("target_mode")
    else:
        raw["target_mode"] = tampered_target_mode
    tampered = replace(
        candidate,
        metadata={**dict(candidate.metadata), "action_parameters": raw},
    )

    with pytest.raises(ValueError, match="视觉导航能力"):
        module._h5_candidate_parameters(
            work=enforced_work,
            candidate=tampered,
            h5_visual_coordinate_mode="enforced",
        )


@pytest.mark.parametrize(
    "capability",
    [
        "dialog_content_close",
        "homepage_payment_close",
        "flutter_app_open_popup_close",
        "flutter_centered_ad_popup_close",
        "flutter_centered_ad_popup_close_above",
        "flutter_centered_scroll_popup_close_below",
        "flutter_image_popup_top_right_close",
        "flutter_account_notice_top_right_close",
        "modal_local_safe_acknowledgement",
    ],
)
def test_h5_candidate_parameters_preserve_trusted_popup_capability(
    capability: str,
) -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    signature = "h5-obstruction:" + "a" * 64
    candidate = CandidateAction(
        candidate_id="candidate-popup-dismissal",
        semantic_target="关闭当前弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
            "expected_next_page_type": None,
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                capability,
            ],
            "obstruction_signature": signature,
            "action_parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": capability,
                "obstruction_signature": signature,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(work.frontier.observation, page_state="popup"),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": capability,
        "obstruction_signature": signature,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


def test_h5_candidate_parameters_preserve_capability_projection_reobserve() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-capability-projection-reobserve",
        semantic_target="重新观察能力投影失败页面",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "能力合同未完整投影，等待页面稳定后在当前会话重新采集。",
            "target_keywords": [
                "capability_projection_rejected",
                "same_session_reobserve",
            ],
            "capability_projection_reobserve_attempt": 1,
            "action_parameters": {
                "target_mode": "capability_projection_reobserve",
                "capability_projection_reobserve_attempt": 1,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_state="popup_overlay",
            metadata={
                **dict(work.frontier.observation.metadata),
                "capability_projection_rejections": [
                    {
                        "target_mode": "popup_dismissal",
                        "semantic_target": "关闭当前弹窗",
                        "reason_code": "popup_capability_contract_invalid",
                    }
                ],
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "capability_projection_reobserve",
        "capability_projection_reobserve_attempt": 1,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 0,
    }


@pytest.mark.parametrize("knowledge_source", ["static", "contextual"])
def test_h5_candidate_parameters_preserve_trusted_bottom_navigation_capability(
    knowledge_source: str,
) -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-bottom-navigation",
        semantic_target=("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索稳定页面中可见的底部导航业务分支",
            "target_keywords": ["bottom_navigation", "视频", "video_list"],
            "expected_next_page_type": "video_list",
            "navigation_knowledge_source": knowledge_source,
            "navigation_knowledge_confidence": 1.0,
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": "视频",
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="home",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "bottom_navigation",
        "navigation_index": 2,
        "navigation_label": "视频",
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 1,
        "expected_next_page_type": "video_list",
    }


def test_h5_candidate_parameters_preserve_adaptive_page_tab_signature() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    signature = "a" * 64
    candidate = CandidateAction(
        candidate_id="candidate-adaptive-page-tab",
        semantic_target="internal:page_tab:nth-of-type(2)",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索当前页面现场识别出的页内栏目分支",
            "target_keywords": ["page_tab_navigation", "国产", "adaptive_page_tab"],
            "expected_next_page_type": "home",
            "navigation_knowledge_source": "contextual",
            "navigation_knowledge_confidence": 1.0,
            "action_parameters": {
                "target_mode": "page_tab_navigation",
                "navigation_index": 2,
                "navigation_label": "国产",
                "interaction_pattern_signature": signature,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="home",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    assert module._h5_candidate_parameters(work=work, candidate=candidate) == {
        "target_mode": "page_tab_navigation",
        "navigation_index": 2,
        "navigation_label": "国产",
        "interaction_pattern_signature": signature,
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 1,
        "expected_next_page_type": "home",
    }


@pytest.mark.parametrize(
    "list_detail_selector",
    [
        ".dynamicData-box:visible .title:visible",
        ".van-list .item_container .poi_title:visible",
    ],
)
def test_h5_candidate_parameters_preserve_trusted_list_detail_capability(
    list_detail_selector: str,
) -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "list_detail_navigation",
        "list_detail_selector": list_detail_selector,
        "source_page_type": "post_list",
        "expected_page_type": "content_detail",
    }
    candidate = CandidateAction(
        candidate_id="candidate-post-detail",
        semantic_target="当前列表首个内容卡片",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=2,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
            "target_keywords": ["nested_content", "list_detail_navigation"],
            "expected_next_page_type": "content_detail",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(work.frontier.observation, page_type="post_list", page_state="stable"),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters
    assert parameters["expected_next_page_type"] == "content_detail"


def test_h5_candidate_parameters_preserve_trusted_age_verification_capability() -> None:
    """Runtime 只接受完整双向合同中的成年确认候选。"""
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "age_verification_confirm",
        "age_verification_capability": "h5_age_verification_confirm.v1",
    }
    candidate = CandidateAction(
        candidate_id="candidate-age-verification",
        semantic_target="年满进入",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "确认已成年并进入当前 H5 业务页面",
            "target_keywords": [
                "age_verification",
                "adult_confirm",
                "evidence:structure",
            ],
            "expected_next_page_type": None,
            "source_page_state": "age_verification_blocking",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type=None,
            page_state="age_verification_blocking",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters


def test_h5_candidate_parameters_preserve_trusted_profile_settings_capability() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "profile_settings_navigation",
        "settings_entry_capability": "btn_edit",
        "source_page_type": "profile",
        "expected_page_type": "settings",
    }
    candidate = CandidateAction(
        candidate_id="candidate-profile-settings",
        semantic_target="span.btn-edit:visible",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索我的页中由设置/编辑语义属性暴露的设置入口",
            "target_keywords": [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:class",
            ],
            "source_page_type": "profile",
            "expected_next_page_type": "settings",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            location="https://m.example.test/mine",
            page_type="profile",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters
    assert parameters["expected_next_page_type"] == "settings"


def test_h5_candidate_parameters_preserve_trusted_flutter_settings_page_back() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-flutter-settings-page-back",
        semantic_target="browser_history",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "子页质检完成后返回来源页继续探索",
            "source_page_type": "settings",
            "target_keywords": [
                "nested_return",
                "flutter_settings_page_back",
                "evidence:structure",
            ],
            "action_parameters": {
                "target_mode": "settings_page_back",
                "settings_page_back_capability": "flutter_settings_page_back",
                "source_page_type": "settings",
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="settings",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters == {
        "target_mode": "settings_page_back",
        "settings_page_back_capability": "flutter_settings_page_back",
        "source_page_type": "settings",
        "viewport_width": 390,
        "viewport_height": 844,
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": 1,
    }


def test_h5_candidate_parameters_preserve_trusted_profile_favorites_back() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-flutter-profile-favorites-back",
        semantic_target="browser_history",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "收藏子页质检完成后返回个人中心继续探索",
            "source_page_type": "video_list",
            "source_page_state": "empty_state",
            "expected_next_page_type": "profile",
            "target_keywords": [
                "nested_return",
                "flutter_profile_favorites_page_back",
                "evidence:structure",
            ],
            "action_parameters": {
                "target_mode": "profile_favorites_page_back",
                "profile_favorites_page_back_capability": ("flutter_profile_favorites_page_back"),
                "source_page_type": "video_list",
                "expected_page_type": "profile",
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="video_list",
            page_state="empty_state",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters["target_mode"] == "profile_favorites_page_back"
    assert parameters["profile_favorites_page_back_capability"] == (
        "flutter_profile_favorites_page_back"
    )
    assert parameters["source_page_type"] == "video_list"
    assert parameters["expected_page_type"] == "profile"
    assert parameters["expected_next_page_type"] == "profile"


def test_h5_candidate_parameters_preserve_content_list_profile_favorites_back() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "profile_favorites_page_back",
        "profile_favorites_page_back_capability": ("flutter_profile_favorites_page_back"),
        "source_page_type": "content_list",
        "expected_page_type": "profile",
    }
    candidate = CandidateAction(
        candidate_id="candidate-content-list-profile-favorites-back",
        semantic_target="browser_history",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "content_list",
            "source_page_state": "empty",
            "expected_next_page_type": "profile",
            "target_keywords": [
                "nested_return",
                "flutter_profile_favorites_page_back",
                "evidence:structure",
            ],
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="content_list",
            page_state="empty",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters["target_mode"] == "profile_favorites_page_back"
    assert parameters["source_page_type"] == "content_list"
    assert parameters["expected_next_page_type"] == "profile"


def test_h5_candidate_parameters_preserve_trusted_profile_options_image_capability() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "profile_settings_navigation",
        "settings_entry_capability": "options_right_image",
        "source_page_type": "profile",
        "expected_page_type": "settings",
    }
    candidate = CandidateAction(
        candidate_id="candidate-profile-options-settings",
        semantic_target="个人中心设置图标",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索我的页中由设置/编辑语义属性暴露的设置入口",
            "target_keywords": [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:structure",
            ],
            "source_page_type": "profile",
            "expected_next_page_type": "settings",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            location="https://m.example.test/mine",
            page_type="profile",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters
    assert parameters["expected_next_page_type"] == "settings"


def test_h5_candidate_parameters_preserve_trusted_profile_activity_capability() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "profile_activity_navigation",
        "activity_entry_capability": "task_sign_image",
        "source_page_type": "profile",
        "expected_page_type": "activity",
    }
    candidate = CandidateAction(
        candidate_id="candidate-profile-task-sign",
        semantic_target="个人中心签到入口",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "进入签到或活动相关入口",
            "target_keywords": [
                "activity_entry",
                "profile_local_action",
                "page_type:activity",
                "evidence:structure",
            ],
            "source_page_type": "profile",
            "expected_next_page_type": "activity",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            location="https://m.example.test/mine",
            page_type="profile",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters
    assert parameters["expected_next_page_type"] == "activity"


def test_h5_candidate_parameters_preserve_trusted_profile_payment_structure() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "profile_payment_navigation",
        "payment_entry_capability": "payment_me_tab_same_node_image",
        "source_page_type": "profile",
        "expected_page_type": "payment",
    }
    candidate = CandidateAction(
        candidate_id="candidate-profile-payment-structure",
        semantic_target="个人中心会员支付入口",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索我的页会员支付入口：立即开通",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:structure",
                "capability:payment_me_tab_same_node_image",
            ],
            "source_page_type": "profile",
            "expected_next_page_type": "payment",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            location="https://m.example.test/mine",
            page_type="profile",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters
    assert parameters["expected_next_page_type"] == "payment"


def test_h5_candidate_parameters_preserve_site_three_member_center_structure() -> None:
    """会员中心备用结构必须通过 Runtime 二次参数验签。"""
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "profile_payment_navigation",
        "payment_entry_capability": "mine_vip_center_image",
        "source_page_type": "profile",
        "expected_page_type": "payment",
    }
    candidate = CandidateAction(
        candidate_id="candidate-site-three-member-center",
        semantic_target="个人中心会员支付入口",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索我的页会员支付入口：会员中心",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:会员中心",
                "evidence:structure",
                "capability:mine_vip_center_image",
            ],
            "source_page_type": "profile",
            "expected_next_page_type": "payment",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            location="http://d24jdtq5qz8ff4.cloudfront.net/profile",
            page_type="profile",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters
    assert parameters["expected_next_page_type"] == "payment"


def test_h5_candidate_parameters_preserve_trusted_profile_payment_visual_capability() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    action_parameters = {
        "target_mode": "visual_coordinate",
        "visual_entry_kind": "profile_vip_payment",
        "visual_source_capture_id": "capture-profile-source",
        "visual_source_url": "https://m.example.test/mine",
        "visual_source_artifact_id": "artifact-profile-source",
        "visual_source_sha256": "sha256:" + "a" * 64,
        "screenshot_index": 1,
        "left_ratio": 0.72,
        "top_ratio": 0.215,
        "right_ratio": 0.93,
        "bottom_ratio": 0.255,
        "visual_confidence": 0.93,
    }
    candidate = CandidateAction(
        candidate_id="candidate-profile-payment",
        semantic_target="立即开通",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "探索我的页会员支付入口：立即开通",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:screenshot",
            ],
            "source_page_type": "profile",
            "expected_next_page_type": "payment",
            "action_parameters": action_parameters,
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            location="https://m.example.test/mine",
            page_type="profile",
            page_state="stable",
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert {key: parameters[key] for key in action_parameters} == action_parameters
    assert parameters["expected_next_page_type"] == "payment"

    for mutation in (
        {"visual_source_url": "https://m.example.test/other"},
        {"visual_source_sha256": "sha256:not-a-checksum"},
        {"visual_confidence": 0.79},
        {"unexpected_model_parameter": "unsafe"},
    ):
        invalid_candidate = replace(
            candidate,
            metadata={
                **dict(candidate.metadata),
                "action_parameters": {**action_parameters, **mutation},
            },
        )

        invalid = module._h5_candidate_parameters(
            work=work,
            candidate=invalid_candidate,
        )

        assert "target_mode" not in invalid


@pytest.mark.asyncio
async def test_h5_completion_wrapper_accepts_closed_selected_navigate_intent() -> None:
    """声明目标页面类型和 destination 的标准 NAVIGATE 意图仍可执行。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _action_intent, _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-product",
        semantic_target="商品详情",
        action_type=InteractionOperation.NAVIGATE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "查看商品详情",
            "expected_next_page_type": "product_detail",
            "destination_url": "https://m.example.test/product/1",
        },
    )
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, candidates=(candidate,)),
        snapshot=snapshot,
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    intent = replace(
        _action_intent(work),
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        risk_level=candidate.risk_level,
        purpose="查看商品详情",
        parameters={
            "candidate_id": candidate.candidate_id,
            "frontier_snapshot_id": work.frontier_snapshot_id,
            "navigation_depth": candidate.navigation_depth,
            "expected_next_page_type": "product_detail",
            "destination_url": "https://m.example.test/product/1",
            "viewport_width": 390,
            "viewport_height": 844,
        },
    )

    class Delegate:
        async def decide_next(self, work):
            return intent

    assert await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work) == intent


@pytest.mark.parametrize("current_mode", ["off", "shadow", "enforced"])
@pytest.mark.asyncio
async def test_coordinator_recovery_rebaselines_before_returning_execution_work(
    current_mode: str,
) -> None:
    """若恢复绕过 Coordinator 或漏掉判定评估，sibling 会在未审计状态执行。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": "capture-rebaseline",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.9,
        "reason": "首页状态恢复",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:rebaseline"],
    }
    source_state = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    calls: list[str] = []
    recovery_parameters: list[dict[str, object]] = []
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-rebaseline",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="restore-action",
    )

    class Coordinator:
        async def coordinate(self, command):
            calls.append(command.intent.action_type.value)
            recovery_parameters.append(dict(command.intent.parameters))
            before = replace(after, capture_id="capture-before", role="before_action")
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=work.task_id,
                    session_id=work.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id="restore-action",
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            calls.append("detect")
            assert capture.capture_id == "capture-rebaseline"
            return detection

    class Assessments:
        async def upsert(
            self,
            *,
            task_id,
            agent_run_id,
            session_id,
            page_capture_id,
            detection_result,
            assessment_payload,
        ):
            calls.append("assessment")
            assert (task_id, agent_run_id, session_id, page_capture_id) == (
                work.task_id,
                "run-h5",
                work.session_id,
                "capture-rebaseline",
            )
            assert detection_result == detection
            assert assessment_payload["execution_status"] == "accepted"
            return SimpleNamespace(row_version=0)

    result = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=Assessments(),
        h5_visual_coordinate_mode=current_mode,
        source_task_id=work.task_id if current_mode == "enforced" else None,
    ).restore(
        work=work,
        agent_run_id="run-h5",
        entry=snapshot.entries[0],
    )

    assert calls == ["navigate", "detect", "assessment"]
    assert recovery_parameters[0]["viewport_width"] == 390
    assert recovery_parameters[0]["viewport_height"] == 844
    assert result.restored is True
    assert result.work.source_capture_id == "capture-rebaseline"
    restored_snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata["h5_task_frontier"]
    )
    restored_selected = next(
        entry for entry in restored_snapshot.entries if entry.status.value == "selected"
    )
    assert restored_selected.candidate == snapshot.entries[0].candidate

    class FailingAssessments:
        async def upsert(self, **kwargs):
            calls.append("assessment_failed")
            raise RuntimeError("assessment store unavailable")

    failed = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=FailingAssessments(),
        h5_visual_coordinate_mode=current_mode,
        source_task_id=work.task_id if current_mode == "enforced" else None,
    ).restore(
        work=work,
        agent_run_id="run-h5",
        entry=snapshot.entries[0],
    )

    assert failed.restored is False
    assert failed.work.source_capture_id == "capture-rebaseline"
    assert failed.work.frontier.observation.metadata["h5_frontier_source_recovery_pending"] is True
    assert failed.work.frontier.observation.metadata["h5_frontier_source_restore_failure"][
        "cause_code"
    ] == "RuntimeError"
    from services.api.app.application.h5_exploration_turn import _selected_entry_requiring_recovery

    assert _selected_entry_requiring_recovery(failed.work) is not None
    assert calls[-3:] == ["navigate", "detect", "assessment_failed"]


@pytest.mark.asyncio
async def test_coordinator_recovery_rebinds_fresh_logical_list_detail_source() -> None:
    """跨页恢复列表来源时必须重建逻辑绑定，不能把新 Capture 判成旧状态。"""
    from services.api.app.application.h5_exploration_observation import (
        build_h5_frontier,
    )
    from services.api.app.application.h5_exploration_workflow import (
        _with_task_frontier,
    )
    from services.api.tests.test_h5_exploration_observation import (
        _detection_with_logical_surface,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    identity = "sha256:" + "a" * 64
    old_state = "sha256:" + "b" * 64
    fresh_state = "sha256:" + "c" * 64
    selector = ".post-box.post .post-text .text-overflow:visible"

    def logical_detection(*, capture_id: str, state_fingerprint: str):
        detection = _detection_with_logical_surface(
            capture_id=capture_id,
            page_type="post_list",
            identity_fingerprint=identity,
            state_fingerprint=state_fingerprint,
        )
        detection["page_url"] = "https://m.example.test/community"
        detection["candidate_actions"] = [
            {
                "action": "click",
                "target": "当前列表首个内容卡片",
                "purpose": "通过当前无遮挡标题进入内容详情",
                "risk_level": "low",
                "target_keywords": [
                    "nested_content",
                    "list_detail_navigation",
                    "unobstructed_title",
                ],
                "navigation_depth": 2,
                "expected_next_page_type": "content_detail",
                "list_detail_selector": selector,
            }
        ]
        return detection

    source_frontier = build_h5_frontier(
        detection_result=logical_detection(
            capture_id="capture-community-old",
            state_fingerprint=old_state,
        ),
        observed_at=datetime.now(UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        logical_targets_effective=True,
    )
    [source_candidate] = source_frontier.candidates
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=source_frontier.observation.capture_id,
        source_state_version=source_frontier.observation.state_version,
        source_location=source_frontier.observation.location,
        navigation_depth=1,
        candidates=(source_candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)

    current_metadata = dict(source_frontier.observation.metadata)
    for key in (
        "logical_surface_effective",
        "logical_targets_effective",
        "logical_surface_identity_fingerprint",
        "logical_surface_state_fingerprint",
    ):
        current_metadata.pop(key, None)
    current_observation = replace(
        source_frontier.observation,
        capture_id="capture-setting-current",
        location="https://m.example.test/setting",
        page_type="settings",
        state_version="h5-state:setting-current",
        metadata=current_metadata,
    )
    current_frontier = _with_task_frontier(
        frontier=replace(source_frontier, observation=current_observation),
        snapshot=snapshot,
    )
    work = _work(capture_id=current_observation.capture_id)
    work = replace(
        work,
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=current_frontier.snapshot_id,
        frontier=current_frontier,
    )
    fresh_detection = logical_detection(
        capture_id="capture-community-fresh",
        state_fingerprint=fresh_state,
    )
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-community-fresh",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="restore-community-source",
    )

    class Coordinator:
        async def coordinate(self, command):
            before = replace(
                after,
                capture_id="capture-before-community-restore",
                role="before_action",
            )
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=work.task_id,
                    session_id=work.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id="restore-community-source",
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, **kwargs):
            del kwargs
            return fresh_detection

    result = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=_NoopAssessments(),
    ).restore(
        work=work,
        agent_run_id="run-h5",
        entry=snapshot.entries[0],
    )

    assert result.restored is True
    metadata = result.work.frontier.observation.metadata
    assert metadata["logical_targets_effective"] is True
    assert metadata["logical_surface_identity_fingerprint"] == identity
    assert metadata["logical_surface_state_fingerprint"] == fresh_state
    restored_snapshot = H5TaskFrontierSnapshot.from_dict(
        metadata["h5_task_frontier"]
    )
    restored_selected = next(
        entry
        for entry in restored_snapshot.entries
        if entry.status.value == "selected"
    )
    assert restored_selected.source_capture_id == after.capture_id
    assert (
        restored_selected.candidate.metadata["source_state_fingerprint"]
        == fresh_state
    )
    parameters = module._h5_candidate_parameters(
        work=result.work,
        candidate=restored_selected.candidate,
    )
    assert parameters["list_detail_selector"] == selector
    assert parameters["expected_next_page_type"] == "content_detail"


@pytest.mark.asyncio
async def test_coordinator_recovery_rebinds_unknown_bottom_navigation_source() -> None:
    """恢复动态首页时，未知目标页型的底栏入口也必须绑定当前 Capture。"""
    from services.api.app.application.h5_exploration_observation import (
        build_h5_frontier,
    )
    from services.api.app.application.h5_exploration_workflow import (
        _with_task_frontier,
    )
    from services.api.tests.test_h5_exploration_observation import (
        _detection_with_logical_surface,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    identity = "sha256:" + "a" * 64
    old_state = "sha256:" + "b" * 64
    fresh_state = "sha256:" + "c" * 64
    target = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"

    def logical_detection(
        *,
        capture_id: str,
        state_fingerprint: str,
        transition_proof: str,
    ) -> dict[str, Any]:
        detection = _detection_with_logical_surface(
            capture_id=capture_id,
            page_type="home",
            identity_fingerprint=identity,
            state_fingerprint=state_fingerprint,
        )
        detection["page_url"] = "https://m.example.test/home"
        logical_surface = detection["logical_surface"]
        assert isinstance(logical_surface, dict)
        logical_surface["logical_transition_proof"] = transition_proof
        if transition_proof != "trusted":
            binding = logical_surface["page_type_binding"]
            assert isinstance(binding, dict)
            binding["coverage_allowed"] = False
            binding["reason_code"] = "logical_transition_untrusted"
        detection["candidate_actions"] = [
            {
                "action": "click",
                "target": target,
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "risk_level": "low",
                "target_keywords": ["bottom_navigation", "短剧"],
                "navigation_depth": 1,
                "expected_next_page_type": None,
                "navigation_index": 2,
                "navigation_label": "短剧",
                "navigation_knowledge_source": "contextual",
                "navigation_knowledge_confidence": 1.0,
            }
        ]
        return detection

    source_frontier = build_h5_frontier(
        detection_result=logical_detection(
            capture_id="capture-home-old",
            state_fingerprint=old_state,
            transition_proof="trusted",
        ),
        observed_at=datetime.now(UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
    )
    [source_candidate] = source_frontier.candidates
    assert source_candidate.metadata["expected_next_page_type"] is None
    assert module._trusted_logical_recovery_candidate(source_candidate) is True
    arbitrary_unknown_target = replace(
        source_candidate,
        metadata={
            **source_candidate.metadata,
            "action_parameters": {"target_mode": "semantic_click"},
        },
    )
    assert module._trusted_logical_recovery_candidate(arbitrary_unknown_target) is False
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=source_frontier.observation.capture_id,
        source_state_version=source_frontier.observation.state_version,
        source_location=source_frontier.observation.location,
        navigation_depth=0,
        candidates=(source_candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)

    current_observation = replace(
        source_frontier.observation,
        capture_id="capture-detail-current",
        location="https://m.example.test/moviePlayer?id=1",
        page_type="video_detail",
        state_version="h5-state:detail-current",
    )
    current_frontier = _with_task_frontier(
        frontier=replace(source_frontier, observation=current_observation),
        snapshot=snapshot,
    )
    work = replace(
        _work(capture_id=current_observation.capture_id),
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=current_frontier.snapshot_id,
        frontier=current_frontier,
    )
    fresh_detection = logical_detection(
        capture_id="capture-home-fresh",
        state_fingerprint=fresh_state,
        transition_proof="insufficient",
    )
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-home-fresh",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="restore-home-source",
    )

    class Coordinator:
        async def coordinate(self, command):
            before = replace(
                after,
                capture_id="capture-before-home-restore",
                role="before_action",
            )
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=work.task_id,
                    session_id=work.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id="restore-home-source",
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, **kwargs):
            del kwargs
            return fresh_detection

    result = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=_NoopAssessments(),
    ).restore(
        work=work,
        agent_run_id="run-h5",
        entry=snapshot.entries[0],
    )

    assert result.restored is True
    metadata = result.work.frontier.observation.metadata
    assert metadata["logical_transition_proof"] == "insufficient"
    restored_snapshot = H5TaskFrontierSnapshot.from_dict(
        metadata["h5_task_frontier"]
    )
    restored_selected = next(
        entry
        for entry in restored_snapshot.entries
        if entry.status is H5FrontierEntryStatus.SELECTED
    )
    assert restored_selected.source_capture_id == after.capture_id
    assert restored_selected.candidate.semantic_target == target
    assert restored_selected.candidate.metadata["expected_next_page_type"] is None
    assert (
        restored_selected.candidate.metadata["source_state_fingerprint"]
        == fresh_state
    )
    parameters = module._h5_candidate_parameters(
        work=result.work,
        candidate=restored_selected.candidate,
    )
    assert parameters["target_mode"] == "bottom_navigation"
    assert parameters["navigation_label"] == "短剧"
    assert parameters["logical_target"]["source_state_fingerprint"] == fresh_state


@pytest.mark.parametrize(
    ("fresh_variant", "prior_attempt_count"),
    [
        ("missing", 0),
        ("missing", 1),
        ("source_page_type_changed", 1),
        ("new_available", 1),
        ("logical_identity_changed", 1),
    ],
)
@pytest.mark.asyncio
async def test_coordinator_recovery_retries_once_before_skipping_selected_sibling(
    monkeypatch: pytest.MonkeyPatch,
    fresh_variant: str,
    prior_attempt_count: int,
) -> None:
    """首次瞬态缺失保留分支，连续两次不可信才改选后续 sibling。"""
    from services.api.app.application.h5_exploration_observation import (
        build_h5_frontier,
    )
    from services.api.app.application.h5_exploration_recovery import (
        H5RestorationResult,
    )
    from services.api.app.application.h5_exploration_turn import (
        _selected_entry_requiring_recovery,
    )
    from services.api.app.application.h5_exploration_workflow import (
        _with_task_frontier,
    )
    from services.api.tests.test_h5_exploration_observation import (
        _detection_with_logical_surface,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    identity = "sha256:" + "a" * 64
    old_state = "sha256:" + "b" * 64
    fresh_state = "sha256:" + "c" * 64

    def logical_detection(
        *,
        capture_id: str,
        page_type: str,
        candidate_present: bool,
    ) -> dict[str, Any]:
        detection = _detection_with_logical_surface(
            capture_id=capture_id,
            page_type=page_type,
            identity_fingerprint=(
                "sha256:" + "d" * 64
                if fresh_variant == "logical_identity_changed" and not capture_id.endswith("old")
                else identity
            ),
            state_fingerprint=(old_state if capture_id.endswith("old") else fresh_state),
        )
        detection["page_url"] = "https://m.example.test/community"
        detection["candidate_actions"] = (
            [
                {
                    "action": "click",
                    "target": "当前列表首个内容卡片",
                    "purpose": "通过当前无遮挡标题进入内容详情",
                    "risk_level": "low",
                    "target_keywords": [
                        "nested_content",
                        "list_detail_navigation",
                        "unobstructed_title",
                    ],
                    "navigation_depth": 2,
                    "expected_next_page_type": "content_detail",
                    "list_detail_selector": (
                        ".post-box.post .post-text .text-overflow:visible"
                        if page_type == "post_list"
                        else ".content-list .content-card .title:visible"
                    ),
                }
            ]
            if candidate_present
            else []
        )
        return detection

    source_frontier = build_h5_frontier(
        detection_result=logical_detection(
            capture_id="capture-community-old",
            page_type="post_list",
            candidate_present=True,
        ),
        observed_at=datetime.now(UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        logical_targets_effective=True,
    )
    [stale_candidate] = source_frontier.candidates
    if fresh_variant == "new_available":
        stale_candidate = replace(stale_candidate, semantic_target="已消失的旧入口")
    sibling = replace(
        stale_candidate,
        candidate_id="candidate-next-sibling",
        semantic_target="下一条待探索入口",
        metadata={
            **dict(stale_candidate.metadata),
            "expected_next_page_type": "payment",
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=source_frontier.observation.capture_id,
        source_state_version=source_frontier.observation.state_version,
        source_location=source_frontier.observation.location,
        navigation_depth=1,
        candidates=(stale_candidate,) if fresh_variant == "new_available"
        else (stale_candidate, sibling),
    )
    stale_entry = next(
        entry
        for entry in snapshot.pending_entries
        if entry.candidate.candidate_id == stale_candidate.candidate_id
    )
    snapshot = snapshot.select_entry(stale_entry.entry_id)
    for _ in range(prior_attempt_count):
        snapshot = snapshot.record_restoration_attempt(stale_entry.entry_id)
    stale_entry = next(
        item for item in snapshot.entries if item.entry_id == stale_entry.entry_id
    )

    current_observation = replace(
        source_frontier.observation,
        capture_id="capture-setting-current",
        location="https://m.example.test/setting",
        page_type="settings",
        state_version="h5-state:setting-current",
    )
    current_frontier = _with_task_frontier(
        frontier=replace(source_frontier, observation=current_observation),
        snapshot=snapshot,
    )
    work = replace(
        _work(capture_id=current_observation.capture_id),
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=current_frontier.snapshot_id,
        frontier=current_frontier,
    )
    fresh_detection = logical_detection(
        capture_id="capture-community-fresh",
        page_type=(
            "content_list"
            if fresh_variant == "source_page_type_changed"
            else "post_list"
        ),
        candidate_present=fresh_variant != "missing",
    )
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-community-fresh",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="restore-community-source",
    )

    async def restored_source(**kwargs):
        assert kwargs["entry"].entry_id == stale_entry.entry_id
        return H5RestorationResult(
            entry_id=stale_entry.entry_id,
            restored=True,
            restoration_attempt_count=stale_entry.restoration_attempt_count + 1,
            route="navigate",
            capture=after,
            detection=fresh_detection,
            assessment={"execution_status": "accepted"},
        )

    monkeypatch.setattr(module, "restore_h5_frontier_source", restored_source)
    result = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=object(),
        detector=object(),
        assessment_repository=_NoopAssessments(),
    ).restore(
        work=work,
        agent_run_id="run-h5",
        entry=stale_entry,
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_fresh_rebind_rejected"
    if prior_attempt_count == 0:
        assert result.reselected_sibling is False
        retry_snapshot = H5TaskFrontierSnapshot.from_dict(
            result.work.frontier.observation.metadata["h5_task_frontier"]
        )
        retry_entry = next(
            entry for entry in retry_snapshot.entries if entry.entry_id == stale_entry.entry_id
        )
        assert retry_entry.status.value == "selected"
        assert retry_entry.restoration_attempt_count == 1
        assert tuple(result.work.frontier.candidates) == (retry_entry.candidate,)
        return
    assert result.reselected_sibling is True
    assert result.work.source_capture_id == after.capture_id
    restored_snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata["h5_task_frontier"]
    )
    skipped_entry = next(
        entry for entry in restored_snapshot.entries if entry.entry_id == stale_entry.entry_id
    )
    assert skipped_entry.status.value == "skipped"
    assert skipped_entry.last_failure_reason == "h5_frontier_fresh_rebind_rejected"
    if fresh_variant == "new_available":
        assert result.reselected_sibling is True
        fresh_entry = next(entry for entry in restored_snapshot.entries
                           if entry.status.value == "selected")
        assert fresh_entry.source_capture_id == after.capture_id
        assert fresh_entry.candidate.semantic_target == "当前列表首个内容卡片"
        assert result.work.budget == work.budget
        assert result.work.deadline == work.deadline
        return
    selected_entry = next(
        entry for entry in restored_snapshot.entries if entry.status.value == "selected"
    )
    assert selected_entry.candidate.candidate_id == sibling.candidate_id
    assert tuple(result.work.frontier.candidates) == (selected_entry.candidate,)
    assert _selected_entry_requiring_recovery(result.work) == selected_entry


@pytest.mark.parametrize(
    "fresh_variant",
    [
        "logical_projection_missing",
        "ordinary_candidate",
        "logical_identity_changed",
        "source_page_type_changed",
    ],
)
def test_logical_source_rebind_rejects_fresh_candidate_without_same_trusted_binding(
    fresh_variant: str,
) -> None:
    """来源恢复不得把旧逻辑候选降级成普通语义点击或换绑到另一业务页。"""
    from services.api.app.application.h5_exploration_observation import (
        build_h5_frontier,
    )
    from services.api.app.application.h5_exploration_workflow import (
        _rebind_selected_task_entry_to_fresh_frontier,
    )
    from services.api.tests.test_h5_exploration_observation import (
        _detection_with_logical_surface,
    )

    old_identity = "sha256:" + "1" * 64
    old_state = "sha256:" + "2" * 64
    fresh_identity = (
        "sha256:" + "3" * 64
        if fresh_variant == "logical_identity_changed"
        else old_identity
    )
    fresh_page_type = (
        "content_list"
        if fresh_variant == "source_page_type_changed"
        else "post_list"
    )

    def detection(
        *,
        capture_id: str,
        identity: str,
        state: str,
        page_type: str,
    ) -> dict[str, Any]:
        result = _detection_with_logical_surface(
            capture_id=capture_id,
            page_type=page_type,
            identity_fingerprint=identity,
            state_fingerprint=state,
        )
        result["page_url"] = "https://m.example.test/community"
        result["candidate_actions"] = [
            {
                "action": "click",
                "target": "当前列表首个内容卡片",
                "purpose": "进入内容详情",
                "risk_level": "low",
                "target_keywords": [
                    "nested_content",
                    "list_detail_navigation",
                ],
                "navigation_depth": 2,
                "expected_next_page_type": "content_detail",
                "list_detail_selector": (
                    ".post-box.post .post-text .text-overflow:visible"
                    if page_type == "post_list"
                    else ".content-list .content-card .title:visible"
                ),
            }
        ]
        return result

    old_frontier = build_h5_frontier(
        detection_result=detection(
            capture_id="capture-logical-old",
            identity=old_identity,
            state=old_state,
            page_type="post_list",
        ),
        observed_at=datetime.now(UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        logical_targets_effective=True,
    )
    [old_candidate] = old_frontier.candidates
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=old_frontier.observation.capture_id,
        source_state_version=old_frontier.observation.state_version,
        source_location=old_frontier.observation.location,
        navigation_depth=1,
        candidates=(old_candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)

    fresh_detection = detection(
        capture_id="capture-logical-fresh",
        identity=fresh_identity,
        state="sha256:" + "4" * 64,
        page_type=fresh_page_type,
    )
    fresh_frontier = build_h5_frontier(
        detection_result=fresh_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=1,
        logical_surface_effective=(
            fresh_variant != "logical_projection_missing"
        ),
        logical_targets_effective=(
            fresh_variant not in {"logical_projection_missing", "ordinary_candidate"}
        ),
    )

    with pytest.raises(ValueError, match="可信逻辑绑定"):
        _rebind_selected_task_entry_to_fresh_frontier(
            snapshot=snapshot,
            frontier=fresh_frontier,
        )


@pytest.mark.parametrize("current_mode", ["off", "shadow"])
@pytest.mark.parametrize(
    ("target_mode_variant", "replacement_target_mode"),
    [
        ("original", "visual_navigation_coordinate"),
        ("deleted", None),
        ("rewritten", "bottom_navigation"),
    ],
)
@pytest.mark.asyncio
async def test_visual_sibling_recovery_rejects_current_mode_before_side_effects(
    current_mode: str,
    target_mode_variant: str,
    replacement_target_mode: str | None,
) -> None:
    """跨状态恢复必须从受签 snapshot 识别视觉来源，并在任何副作用前门禁。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module, source_work, candidate, _, _ = _visual_navigation_enforced_work_and_candidate()
    action_parameters = dict(candidate.metadata["action_parameters"])
    if target_mode_variant == "deleted":
        action_parameters.pop("target_mode")
    else:
        action_parameters["target_mode"] = replacement_target_mode
    persisted_candidate = replace(
        candidate,
        metadata={
            **dict(candidate.metadata),
            "action_parameters": action_parameters,
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=source_work.source_capture_id,
        source_state_version=source_work.frontier.observation.state_version,
        source_location=source_work.frontier.observation.location,
        navigation_depth=0,
        candidates=(persisted_candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    source_frontier = _with_task_frontier(
        frontier=replace(source_work.frontier, candidates=(persisted_candidate,)),
        snapshot=snapshot,
    )
    current_observation = replace(
        source_frontier.observation,
        capture_id="capture-visual-detail-current",
        location="https://m.example.test/content/1",
        page_type="content_detail",
        state_version="h5-state:visual-detail-current",
    )
    current_frontier = replace(source_frontier, observation=current_observation)
    current_work = replace(
        source_work,
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=current_frontier.snapshot_id,
        frontier=current_frontier,
    )
    persisted_selected = next(
        item
        for item in H5TaskFrontierSnapshot.from_dict(
            current_work.frontier.observation.metadata["h5_task_frontier"]
        ).entries
        if item.status.value == "selected"
    )
    assert persisted_selected.source_location != current_observation.location
    calls = {"coordinator": 0, "detector": 0, "assessment": 0}

    restored_capture = CaptureRef(
        task_id=current_work.task_id,
        session_id=current_work.session_id,
        capture_id="capture-visual-home-restored",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=current_work.source_capture_id,
        action_execution_id="restore-visual-source",
    )

    class Coordinator:
        async def coordinate(self, command):
            calls["coordinator"] += 1
            before = replace(
                restored_capture,
                capture_id="capture-before-visual-restore",
                role="before_action",
            )
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=current_work.task_id,
                    session_id=current_work.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id="restore-visual-source",
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=restored_capture.capture_id,
                ),
                after_capture=restored_capture,
            )

    class Detector:
        async def detect(self, **kwargs):
            del kwargs
            calls["detector"] += 1
            return {
                "schema_version": "page-detection.v2",
                "page_capture_id": restored_capture.capture_id,
                "page_url": persisted_selected.source_location,
                "detection_status": "recognized",
                "page_type": "home",
                "page_state": "stable",
                "confidence": 0.9,
                "reason": "来源状态恢复",
                "matched_catalog_signals": {"ocr_keywords": ["首页"]},
                "candidate_actions": [],
                "risk_flags": [],
                "evidence_artifact_ids": ["artifact:visual-home-restored"],
            }

    class Assessments:
        async def upsert(self, **kwargs):
            del kwargs
            calls["assessment"] += 1
            return SimpleNamespace(row_version=0)

    recovery = module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=Assessments(),
        h5_visual_coordinate_mode=current_mode,
    )

    with pytest.raises(
        ValueError,
        match="H5 Frontier 视觉导航能力无效或未处于 enforced 模式",
    ):
        await recovery.restore(
            work=current_work,
            agent_run_id="run-h5",
            entry=persisted_selected,
        )

    assert calls == {"coordinator": 0, "detector": 0, "assessment": 0}


@pytest.mark.asyncio
async def test_visual_recovery_with_tampered_mode_never_uses_ordinary_path() -> None:
    """保留视觉能力但改写 target_mode 时，fresh 无匹配只关闭该候选。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module, enforced_work, candidate, _, _ = _visual_navigation_enforced_work_and_candidate()
    action_parameters = dict(candidate.metadata["action_parameters"])
    action_parameters["target_mode"] = "bottom_navigation"
    tampered_candidate = replace(
        candidate,
        metadata={
            **dict(candidate.metadata),
            "action_parameters": action_parameters,
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=enforced_work.source_capture_id,
        source_state_version=enforced_work.frontier.observation.state_version,
        source_location=enforced_work.frontier.observation.location,
        navigation_depth=0,
        candidates=(tampered_candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    snapshot = snapshot.record_restoration_attempt(snapshot.entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(enforced_work.frontier, candidates=(tampered_candidate,)),
        snapshot=snapshot,
    )
    tampered_work = replace(
        enforced_work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )
    fresh_capture_id = "capture-visual-tampered-fresh"
    detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": fresh_capture_id,
        "page_url": tampered_work.frontier.observation.location,
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.9,
        "reason": "来源已恢复但视觉目标消失",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:visual-tampered-fresh"],
    }
    after = CaptureRef(
        task_id=tampered_work.task_id,
        session_id=tampered_work.session_id,
        capture_id=fresh_capture_id,
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=tampered_work.source_capture_id,
        action_execution_id="restore-tampered-visual",
    )

    class Coordinator:
        async def coordinate(self, command):
            before = replace(after, capture_id="capture-before-tampered", role="before_action")
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=tampered_work.task_id,
                    session_id=tampered_work.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id="restore-tampered-visual",
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del capture, task_id, agent_run_id
            return detection

    recovery = module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=_NoopAssessments(),
        h5_visual_coordinate_mode="enforced",
        source_task_id=tampered_work.task_id,
    )

    result = await recovery.restore(
        work=tampered_work,
        agent_run_id="run-h5",
        entry=snapshot.entries[0],
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_fresh_rebind_rejected"
    assert result.reselected_sibling is False
    assert result.work.frontier.candidates == ()
    restored_snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata["h5_task_frontier"]
    )
    assert restored_snapshot.entries[0].status.value == "skipped"
    assert (
        restored_snapshot.entries[0].last_failure_reason
        == "h5_frontier_fresh_rebind_rejected"
    )


async def _owned_visual_recovery_detection(
    *,
    task_id: str,
    capture_id: str,
    state_fingerprint: str,
) -> dict[str, Any]:
    """通过真实 ownership finalizer 构造两个可恢复的视觉 sibling。"""
    from services.api.app.agents.runtime.run_context import (
        AgentRunContext,
        bind_agent_run_context,
    )
    from services.api.app.agents.tools.page_detection_tools import (
        detect_page_for_agent,
    )
    from services.api.app.domain.page.catalog import load_default_page_type_catalog
    from services.api.tests import test_page_detection_tools as detection_test_support

    bundle, reader, ui_snapshot = detection_test_support._visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    bundle = replace(bundle, task_id=task_id, page_capture_id=capture_id)
    logical_surface = ui_snapshot["logical_surface"]
    assert isinstance(logical_surface, dict)
    logical_surface["state_fingerprint"] = state_fingerprint
    reader.payloads["visual-snapshot"] = json.dumps(
        ui_snapshot,
        ensure_ascii=False,
    ).encode()
    visual_output = detection_test_support._visual_navigation_output().model_dump(mode="json")
    visual_output["predictions"].append(
        {
            "target_kind": "video_card",
            "recognized_label": "视频卡片",
            "label_source": "visual_ocr",
            "expected_page_type": "video_detail",
            "visual_target_confidence": 0.94,
            "selected": False,
            "bounds": {
                "screenshot_index": 1,
                "left_ratio": 0.08,
                "top_ratio": 0.55,
                "right_ratio": 0.92,
                "bottom_ratio": 0.82,
                "confidence": 0.95,
            },
            "risk_flags": [],
        }
    )
    visual_model_output = type(detection_test_support._visual_navigation_output()).model_validate(
        visual_output
    )
    with bind_agent_run_context(
        AgentRunContext(task_id=task_id, agent_run_id="run-visual-recovery")
    ):
        return await detect_page_for_agent(
            page_capture_id=capture_id,
            evidence_loader=detection_test_support.FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=detection_test_support.FakeChatClient(
                detection_test_support._recognized_output("home")
            ),
            visual_navigation_chat_client=detection_test_support.FakeChatClient(
                visual_model_output
            ),
            visual_coordinate_mode="enforced",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=detection_test_support.FakeNavAliasResolver({}),
        )


@pytest.mark.asyncio
async def test_visual_sibling_recovery_rebinds_fresh_capture_capability() -> None:
    """首个视觉详情后恢复来源时，sibling 必须换成新 Capture 的新能力。"""
    from urllib.parse import urlsplit

    from services.api.app.application.h5_exploration_observation import build_h5_frontier
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5FrontierEntryStatus
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    base = _work(capture_id="capture-visual-home-old")
    old_detection = await _owned_visual_recovery_detection(
        task_id=base.task_id,
        capture_id=base.source_capture_id,
        state_fingerprint="sha256:" + "3" * 64,
    )
    source_frontier = build_h5_frontier(
        detection_result=old_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=base.task_id,
    )
    assert len(source_frontier.candidates) == 2
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=base.source_capture_id,
        source_state_version=source_frontier.observation.state_version,
        source_location=source_frontier.observation.location,
        navigation_depth=0,
        candidates=source_frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    first_frontier = _with_task_frontier(frontier=source_frontier, snapshot=snapshot)
    first_work = replace(
        base,
        frontier_snapshot_id=first_frontier.snapshot_id,
        frontier=first_frontier,
    )
    explorer = module.H5CompletionAwareExplorer(
        delegate=module.H5PlatformSelectedExplorer(
            h5_visual_coordinate_mode="enforced",
        ),
        h5_visual_coordinate_mode="enforced",
    )
    first_intent = await explorer.decide_next(first_work)
    assert isinstance(first_intent, ActionIntent)
    first_entry = next(
        item for item in snapshot.entries if item.status is H5FrontierEntryStatus.SELECTED
    )
    snapshot = snapshot.mark_executed(first_entry.entry_id)
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    old_selected = next(
        item for item in snapshot.entries if item.status is H5FrontierEntryStatus.SELECTED
    )
    old_capability = old_selected.candidate.metadata["action_parameters"][
        "visual_navigation_capability"
    ]
    source_url = urlsplit(source_frontier.observation.location)
    detail_observation = replace(
        source_frontier.observation,
        capture_id="capture-visual-detail",
        location=f"{source_url.scheme}://{source_url.netloc}/video/1",
        page_type="video_detail",
        state_version="h5-state:visual-detail",
    )
    detail_frontier = _with_task_frontier(
        frontier=replace(source_frontier, observation=detail_observation),
        snapshot=snapshot,
    )
    detail_work = replace(
        first_work,
        source_capture_id="capture-visual-detail",
        frontier_snapshot_id=detail_frontier.snapshot_id,
        frontier=detail_frontier,
    )
    fresh_capture_id = "capture-visual-home-fresh"
    fresh_detection = await _owned_visual_recovery_detection(
        task_id=base.task_id,
        capture_id=fresh_capture_id,
        state_fingerprint="sha256:" + "7" * 64,
    )
    after = CaptureRef(
        task_id=base.task_id,
        session_id=base.session_id,
        capture_id=fresh_capture_id,
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=detail_work.source_capture_id,
        action_execution_id="restore-visual-source",
    )

    class Coordinator:
        async def coordinate(self, command):
            before = replace(after, capture_id="capture-before-restore", role="before_action")
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=base.task_id,
                    session_id=base.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id="restore-visual-source",
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            assert (capture.capture_id, task_id, agent_run_id) == (
                fresh_capture_id,
                base.task_id,
                "run-h5",
            )
            return fresh_detection

    result = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=_NoopAssessments(),
        h5_visual_coordinate_mode="enforced",
        source_task_id=base.task_id,
    ).restore(
        work=detail_work,
        agent_run_id="run-h5",
        entry=old_selected,
    )

    assert result.restored is True
    assert result.work.source_capture_id == fresh_capture_id
    restored_snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata["h5_task_frontier"]
    )
    restored_selected = next(
        item for item in restored_snapshot.entries if item.status is H5FrontierEntryStatus.SELECTED
    )
    fresh_capability = restored_selected.candidate.metadata["action_parameters"][
        "visual_navigation_capability"
    ]
    assert restored_selected.entry_id == old_selected.entry_id
    assert restored_selected.source_capture_id == fresh_capture_id
    assert fresh_capability["source_capture_id"] == fresh_capture_id
    assert fresh_capability["source_state_fingerprint"] == "sha256:" + "7" * 64
    assert fresh_capability["target_signature"] != old_capability["target_signature"]
    assert fresh_capability != old_capability
    assert len(restored_snapshot.entries) == 2
    assert restored_snapshot.pending_count == 0
    assert restored_selected.restoration_attempt_count == (
        old_selected.restoration_attempt_count + 1
    )
    assert restored_snapshot.executed_candidate_ids == snapshot.executed_candidate_ids
    assert (
        sum(entry.status is H5FrontierEntryStatus.EXECUTED for entry in restored_snapshot.entries)
        == 1
    )
    sibling_intent = await explorer.decide_next(result.work)
    assert isinstance(sibling_intent, ActionIntent)
    assert module.thaw_json_mapping(sibling_intent.parameters)[
        "visual_navigation_capability"
    ] == module.thaw_json_mapping(fresh_capability)


@pytest.mark.asyncio
async def test_coordinator_recovery_closes_signed_startup_ad_retriggered_by_navigation() -> None:
    """来源导航重触发开屏广告时，仅执行检测层签发的关闭能力。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-square",
        semantic_target="广场",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "post_list",
        },
    )
    home_detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": "capture-home-rebaseline",
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.95,
        "reason": "首页状态恢复",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:home"],
    }
    startup_detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": "capture-startup-ad",
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "startup_ad",
        "page_state": "countdown",
        "confidence": 0.98,
        "reason": "受控导航重触发开屏广告",
        "matched_catalog_signals": {"ocr_keywords": ["跳过"]},
        "candidate_actions": [
            {
                "action": "click",
                "target": "跳过",
                "risk_level": "low",
                "navigation_depth": 0,
                "purpose": "关闭开屏广告并在同一浏览器会话进入首页",
                "target_keywords": [
                    "startup_ad",
                    "close_control",
                    "startup_ad_dismissal",
                ],
                "expected_next_page_type": "home",
                "obstruction_capability": "startup_ad_dismissal",
                "obstruction_signature": "h5-obstruction:" + "2" * 64,
            }
        ],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:startup-ad"],
    }
    popup_signature = "h5-obstruction:" + "1" * 64
    popup_detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": "capture-home-popup",
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "popup",
        "confidence": 0.96,
        "reason": "首页仍有一层业务弹窗",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [
            {
                "action": "click",
                "target": "关闭当前弹窗",
                "risk_level": "low",
                "navigation_depth": 0,
                "purpose": "关闭当前最上层弹窗后继续探索",
                "target_keywords": [
                    "popup_dismissal",
                    "close_control",
                    "dialog_content_close",
                ],
                "expected_next_page_type": "home",
                "obstruction_capability": "dialog_content_close",
                "obstruction_signature": popup_signature,
            }
        ],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:home-popup"],
    }
    source_state = build_h5_frontier(
        detection_result=home_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    work = _work()
    current_observation = replace(
        work.frontier.observation,
        location="https://m.example.test/movie-player/1",
        page_type="video_detail",
        page_state="stable",
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=current_observation, candidates=(candidate,)),
        snapshot=snapshot,
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    calls: list[str] = []
    intents: list[ActionIntent] = []

    def capture_ref(capture_id: str, *, action_execution_id: str) -> CaptureRef:
        return CaptureRef(
            task_id=work.task_id,
            session_id=work.session_id,
            capture_id=capture_id,
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role="after_action",
            parent_capture_id=work.source_capture_id,
            action_execution_id=action_execution_id,
        )

    class Coordinator:
        async def coordinate(self, command):
            intents.append(command.intent)
            calls.append(command.intent.action_type.value)
            target_mode = command.intent.parameters.get("target_mode")
            action_id = {
                "startup_ad_dismissal": "startup-dismiss-action",
                "popup_dismissal": "popup-dismiss-action",
            }.get(target_mode, "navigate-action")
            capture_id = {
                "startup_ad_dismissal": "capture-home-popup",
                "popup_dismissal": "capture-home-rebaseline",
            }.get(target_mode, "capture-startup-ad")
            after = capture_ref(
                capture_id,
                action_execution_id=action_id,
            )
            before = replace(after, capture_id=f"before-{action_id}", role="before_action")
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=work.task_id,
                    session_id=work.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id=action_id,
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            calls.append(f"detect:{capture.capture_id}")
            return {
                "capture-startup-ad": startup_detection,
                "capture-home-popup": popup_detection,
                "capture-home-rebaseline": home_detection,
            }[capture.capture_id]

    class Assessments:
        async def upsert(self, **kwargs):
            calls.append(f"assessment:{kwargs['detection_result']['page_type']}")
            return SimpleNamespace(row_version=0)

    result = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=Assessments(),
    ).restore(
        work=work,
        agent_run_id="run-h5",
        entry=snapshot.entries[0],
    )

    assert result.restored is True
    assert result.work.source_capture_id == "capture-home-rebaseline"
    assert calls == [
        "navigate",
        "detect:capture-startup-ad",
        "assessment:startup_ad",
        "click",
        "detect:capture-home-popup",
        "assessment:home",
        "click",
        "detect:capture-home-rebaseline",
        "assessment:home",
    ]
    assert intents[1].source_capture_id == "capture-startup-ad"
    assert intents[1].semantic_target == "跳过"
    assert len(intents[1].intent_id) <= 128
    assert intents[1].parameters["target_mode"] == "startup_ad_dismissal"
    assert intents[2].source_capture_id == "capture-home-popup"
    assert intents[2].semantic_target == "关闭当前弹窗"
    assert len(intents[2].intent_id) <= 128
    assert intents[2].parameters["target_mode"] == "popup_dismissal"


@pytest.mark.asyncio
async def test_frontier_recovery_attempts_use_distinct_restore_and_obstruction_identities() -> None:
    """同一 Frontier 重试时，恢复与开屏清障动作都不得复用首次幂等身份。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    home_detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": "capture-home",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.95,
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:home"],
    }
    startup_detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": "capture-startup",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "startup_ad",
        "page_state": "countdown",
        "confidence": 0.98,
        "matched_catalog_signals": {"ocr_keywords": ["跳过"]},
        "candidate_actions": [
            {
                "action": "click",
                "target": "跳过",
                "risk_level": "low",
                "navigation_depth": 0,
                "purpose": "关闭开屏广告并在同一浏览器会话进入首页",
                "target_keywords": [
                    "startup_ad",
                    "close_control",
                    "startup_ad_dismissal",
                ],
                "expected_next_page_type": "home",
                "obstruction_capability": "startup_ad_dismissal",
                "obstruction_signature": "h5-obstruction:" + "1" * 64,
            }
        ],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:startup"],
    }
    source_state = build_h5_frontier(
        detection_result=home_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    base = _work()
    candidate = replace(
        base.frontier.candidates[0],
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "post_list",
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)

    def make_work(restoration_attempt_count: int):
        current = snapshot
        for _ in range(restoration_attempt_count):
            current = current.record_restoration_attempt(current.entries[0].entry_id)
        frontier = _with_task_frontier(
            frontier=replace(base.frontier, candidates=(candidate,)),
            snapshot=current,
        )
        return replace(
            base,
            frontier_snapshot_id=frontier.snapshot_id,
            frontier=frontier,
        )

    first_work = make_work(0)
    second_work = make_work(1)
    intents: list[ActionIntent] = []
    captures = {
        "capture-startup": CaptureRef(
            task_id=first_work.task_id,
            session_id=first_work.session_id,
            capture_id="capture-startup",
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role="after_action",
            parent_capture_id="capture-home",
            action_execution_id="restore-action",
        ),
        "capture-home": CaptureRef(
            task_id=first_work.task_id,
            session_id=first_work.session_id,
            capture_id="capture-home",
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role="after_action",
            parent_capture_id="capture-startup",
            action_execution_id="dismiss-action",
        ),
    }

    class Coordinator:
        async def coordinate(self, command):
            intents.append(command.intent)
            before = replace(
                captures["capture-startup"],
                capture_id="before-action",
                role="before_action",
            )
            target_mode = command.intent.parameters.get("target_mode")
            after = captures["capture-home" if target_mode else "capture-startup"]
            return CaptureCoordinationResult(
                before_capture=before,
                interaction=InteractionResult(
                    task_id=command.intent.task_id,
                    session_id=command.intent.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id=("dismiss-action" if target_mode else "restore-action"),
                    source_capture_id=before.capture_id,
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            del task_id, agent_run_id
            return startup_detection if capture.capture_id == "capture-startup" else home_detection

    class Assessments:
        async def upsert(self, *, detection_result, **kwargs):
            del kwargs
            page_type = detection_result["page_type"]
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={
                    "page_type": page_type,
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": [detection_result["page_capture_id"]],
                    "evidence_artifact_ids": list(detection_result["evidence_artifact_ids"]),
                    "page_states": [detection_result["page_state"]],
                },
            )

    recovery = module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=Assessments(),
    )
    first_entry = H5TaskFrontierSnapshot.from_dict(
        first_work.frontier.observation.metadata["h5_task_frontier"]
    ).entries[0]
    second_entry = H5TaskFrontierSnapshot.from_dict(
        second_work.frontier.observation.metadata["h5_task_frontier"]
    ).entries[0]
    assert (
        await recovery.restore(
            work=first_work,
            agent_run_id="run-h5",
            entry=first_entry,
        )
    ).restored is True
    assert (
        await recovery.restore(
            work=second_work,
            agent_run_id="run-h5",
            entry=second_entry,
        )
    ).restored is True

    restore_intents = [intent for intent in intents if "target_mode" not in intent.parameters]
    obstruction_intents = [intent for intent in intents if "target_mode" in intent.parameters]
    assert len(restore_intents) == 2
    assert len(obstruction_intents) == 2
    assert restore_intents[0].intent_id != restore_intents[1].intent_id
    assert restore_intents[0].idempotency_key != restore_intents[1].idempotency_key
    assert obstruction_intents[0].intent_id != obstruction_intents[1].intent_id
    assert obstruction_intents[0].idempotency_key != obstruction_intents[1].idempotency_key


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,error_code", [
    ("startup_ad_dismissal", "STARTUP_AD_DISMISSAL_NOT_OBSERVED"),
    ("popup_dismissal", "POPUP_DISMISSAL_NOT_FOUND"),
    ("popup_dismissal", "POPUP_MODAL_SCOPE_MISMATCH"),
])
async def test_recovery_reuses_fresh_before_capture_when_startup_ad_expires(
    operation, error_code,
) -> None:
    """恢复 Capture 已过期时，应采用动作前新证据，不能重放失效关闭动作。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    base = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=base.source_capture_id,
        source_state_version=base.frontier.observation.state_version,
        source_location=base.frontier.observation.location,
        navigation_depth=0,
        candidates=base.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=base.frontier, snapshot=snapshot)
    work = replace(
        base,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    def capture_ref(capture_id: str) -> CaptureRef:
        return CaptureRef(
            task_id=work.task_id,
            session_id=work.session_id,
            capture_id=capture_id,
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role="before_action",
            parent_capture_id=work.source_capture_id,
            action_execution_id="startup-dismiss-action",
        )

    startup_capture = replace(capture_ref("capture-startup-ad"), role="after_action")
    fresh_home_capture = capture_ref("capture-home-fresh")
    startup_detection = {
        "schema_version": "page-detection.v2",
        "page_capture_id": startup_capture.capture_id,
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "startup_ad",
        "page_state": "countdown",
        "confidence": 0.98,
        "reason": "导航后短暂出现开屏广告",
        "matched_catalog_signals": {"ocr_keywords": ["跳过"]},
        "candidate_actions": [
            {
                "action": "click",
                "target": "跳过",
                "risk_level": "low",
                "navigation_depth": 0,
                "purpose": "关闭开屏广告并进入首页",
                "target_keywords": [
                    "startup_ad",
                    "close_control",
                    "startup_ad_dismissal",
                ],
                "expected_next_page_type": "home",
                "obstruction_capability": "startup_ad_dismissal",
                "obstruction_signature": "h5-obstruction:" + "3" * 64,
            }
        ],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:startup-ad"],
    }
    home_detection = {
        **startup_detection,
        "page_capture_id": fresh_home_capture.capture_id,
        "page_type": "home",
        "page_state": "stable",
        "reason": "倒计时已自然结束",
        "candidate_actions": [],
        "evidence_artifact_ids": ["artifact:home"],
    }
    detected: list[str] = []

    class Coordinator:
        async def coordinate(self, command):
            del command
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code=error_code,
                message="开屏关闭动作执行时来源状态已经变化",
                before_capture=fresh_home_capture,
                retryable=True,
            )

    class Detector:
        async def detect(self, *, capture, task_id, agent_run_id):
            assert (task_id, agent_run_id) == (work.task_id, "run-h5")
            detected.append(capture.capture_id)
            return home_detection

    browser = module._CoordinatorRecoveryBrowser(
        work=work,
        agent_run_id="run-h5",
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=_NoopAssessments(),
        cancellation=module.H5CancellationCheckpoint(),
        now=lambda: work.frontier.observation.observed_at,
        source_state_version=work.frontier.observation.state_version,
    )

    if operation == "startup_ad_dismissal":
        result = await browser.dismiss_startup_ad(
            capture=startup_capture, detection=startup_detection,
        )
    else:
        obstruction_frontier = module.build_h5_frontier(
            detection_result=startup_detection,
            observed_at=work.frontier.observation.observed_at,
            navigation_depth=0,
        )
        result = await browser._coordinate_obstruction_candidate(
            capture=startup_capture, frontier=obstruction_frontier,
            candidate=obstruction_frontier.candidates[0],
            operation=operation,
        )

    assert result == fresh_home_capture
    assert detected == (
        [fresh_home_capture.capture_id] if operation == "startup_ad_dismissal" else []
    )


@pytest.mark.parametrize("boundary", [
    "valid", "other_task", "other_session", "same_capture", "wrong_role",
    "after_capture_error", "other_error", "missing_capture",
])
@pytest.mark.parametrize("error_code", [
    "POPUP_DISMISSAL_NOT_FOUND", "POPUP_MODAL_SCOPE_MISMATCH",
])
async def test_recovery_popup_refresh_requires_current_pre_action_capture(boundary, error_code):
    """重新识别不能复用跨会话、旧来源或已经执行后的错误现场。"""
    from unittest.mock import AsyncMock

    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    source = CaptureRef(
        task_id=work.task_id, session_id=work.session_id,
        capture_id=work.source_capture_id, surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE, role="after_action",
    )
    fresh = replace(source, capture_id="capture-fresh", role="before_action")
    if boundary == "other_task":
        fresh = replace(fresh, task_id="task-other")
    elif boundary == "other_session":
        fresh = replace(fresh, session_id="session-other")
    elif boundary == "same_capture":
        fresh = replace(fresh, capture_id=source.capture_id)
    elif boundary == "wrong_role":
        fresh = replace(fresh, role="after_action")
    elif boundary == "missing_capture":
        fresh = None
    error = CaptureCoordinationError(
        stage=(CaptureCoordinationStage.AFTER_CAPTURE if boundary == "after_capture_error"
               else CaptureCoordinationStage.ACTION),
        error_code="OTHER_ERROR" if boundary == "other_error" else error_code,
        message="关闭前现场变化", before_capture=fresh,
    )
    detector = SimpleNamespace(detect=AsyncMock())
    browser = module._CoordinatorRecoveryBrowser(
        work=work, agent_run_id="run-h5", capture_coordinator=SimpleNamespace(),
        detector=detector, assessment_repository=SimpleNamespace(),
        cancellation=module.H5CancellationCheckpoint(),
        now=lambda: work.frontier.observation.observed_at,
        source_state_version=work.frontier.observation.state_version,
    )
    result = await browser._fresh_capture_after_obstruction_changed(
        error=error, operation="popup_dismissal", source_capture=source,
    )
    assert result == (fresh if boundary == "valid" else None)
    detector.detect.assert_not_called()


@pytest.mark.asyncio
async def test_browser_recovery_work_persists_facts_budget_and_stable_identity() -> None:
    from services.api.app.application.h5_exploration_workflow import (
        H5ExplorationWorkflow,
        RecoverH5ExplorationCommand,
        _with_task_frontier,
    )
    from services.api.app.application.h5_page_type_assessment_adapter import (
        H5PageTypeAssessmentAdapterResult,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    prior = _work()
    prior = replace(
        prior,
        budget=replace(
            prior.budget,
            max_actions=5,
            remaining_actions=5,
            max_business_actions=5,
            remaining_business_actions=5,
        ),
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=3).merge(
        source_capture_id=prior.source_capture_id,
        source_state_version=prior.frontier.observation.state_version,
        source_location=prior.frontier.observation.location,
        navigation_depth=0,
        candidates=prior.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=prior.frontier, snapshot=snapshot)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            metadata={**dict(frontier.observation.metadata), "agent_run_id": "run-h5"},
        ),
    )
    prior = replace(prior, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    from services.api.app.application.h5_exploration_recovery import (
        H5BrowserRecoveryAttemptFacts,
        H5FailureStage,
        build_h5_recovery_session_id,
    )

    before_unknown = CaptureRef(
        task_id=prior.task_id,
        session_id=prior.session_id,
        capture_id="capture-before-unknown",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id=prior.source_capture_id,
        action_execution_id="unknown-action-1",
    )
    reservation = (
        await _runtime_module()
        .H5CoordinatorBrowserLossRecovery(
            capture_runtime=SimpleNamespace(),
            detector=SimpleNamespace(),
            workflow=SimpleNamespace(),
            now=lambda: prior.frontier.observation.observed_at,
        )
        .reserve_attempt(
            work=prior,
            agent_run_id="run-h5",
            error=CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code="page_capture_session_crashed",
                message="browser lost",
                cause_type="BrowserSessionCrashedError",
                before_capture=before_unknown,
            ),
        )
    )
    assert reservation.reserved is True
    prior = reservation.work
    first_attempt = H5BrowserRecoveryAttemptFacts.from_dict(
        prior.frontier.observation.metadata["h5_browser_recovery_attempt"]
    ).complete(reason_code="reopen_failed")
    failed_frontier = replace(
        prior.frontier,
        observation=replace(
            prior.frontier.observation,
            metadata={
                **dict(prior.frontier.observation.metadata),
                "h5_browser_recovery_attempt": first_attempt.to_dict(),
            },
        ),
    )
    prior = replace(
        prior,
        frontier=failed_frontier,
        frontier_snapshot_id=failed_frontier.snapshot_id,
    )
    reservation = (
        await _runtime_module()
        .H5CoordinatorBrowserLossRecovery(
            capture_runtime=SimpleNamespace(),
            detector=SimpleNamespace(),
            workflow=SimpleNamespace(),
            now=lambda: prior.frontier.observation.observed_at,
        )
        .reserve_attempt(
            work=prior,
            agent_run_id="run-h5",
            error=CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code="page_capture_session_crashed",
                message="browser lost again",
                cause_type="BrowserSessionCrashedError",
                before_capture=before_unknown,
            ),
        )
    )
    assert reservation.reserved is True
    prior = reservation.work

    recovery_session_id = build_h5_recovery_session_id(
        agent_run_id="run-h5",
        work_id=prior.work_id,
        stage=H5FailureStage.ACTION,
        recovery_attempt_count=2,
    )
    capture = CaptureRef(
        task_id=prior.task_id,
        session_id=recovery_session_id,
        capture_id="capture-browser-recovery",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id="capture-recovery-before",
        action_execution_id="recovery-navigation-action",
    )
    detection = {
        "page_capture_id": capture.capture_id,
        "page_url": prior.frontier.observation.location,
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
    }

    class Orchestrator:
        async def prepare_exploration(self, command):
            return build_exploration_work(
                task_id=command.task_id,
                session_id=command.session_id,
                branch_id=command.branch_id,
                objective=command.objective,
                surface_kind=command.surface_kind,
                source_capture_id=command.source_capture_id,
                frontier_snapshot_id=command.frontier_snapshot_id,
                frontier=command.frontier,
                boundaries=command.boundaries,
                budget=command.budget,
                deadline=command.deadline,
            )

    class Assessments:
        calls = []

        async def upsert(self, **kwargs):
            self.calls.append(kwargs)
            return kwargs

    workflow = H5ExplorationWorkflow(
        bootstrapper=SimpleNamespace(),
        detector=SimpleNamespace(),
        orchestrator=Orchestrator(),
        assessment_repository=Assessments(),
        now=lambda: prior.frontier.observation.observed_at,
    )
    command = RecoverH5ExplorationCommand(
        prior_work=prior,
        capture=capture,
        detection_result=detection,
        assessment=H5PageTypeAssessmentAdapterResult(execution_status="skipped"),
        failure_stage="action",
        recovery_attempt_count=2,
        elapsed_ms=250,
        restoration_attempt_id="restoration-attempt:stable",
        equivalence_assessment_id="state-equivalence:stable",
        action_execution_id="unknown-action-1",
        inventory_status="insufficient",
    )

    first = await workflow.prepare_browser_recovery(command)
    replay = await workflow.prepare_browser_recovery(command)

    assert first.work.work_id == replay.work.work_id
    assert first.work.work_id != prior.work_id
    assert first.work.session_id == recovery_session_id
    assert first.work.budget.remaining_actions == prior.budget.remaining_actions
    assert first.work.budget.remaining_duration_ms == (prior.budget.remaining_duration_ms - 250)
    facts_payload = first.work.frontier.observation.metadata["h5_browser_recovery"]
    from services.api.app.application.h5_exploration_recovery import (
        H5BrowserRecoveryFacts,
    )

    facts = H5BrowserRecoveryFacts.from_dict(facts_payload)
    assert facts.failure_stage.value == "action"
    assert facts.source_session_id == prior.session_id
    assert facts.target_session_id == capture.session_id
    assert facts.original_source_capture_id == prior.source_capture_id
    assert facts.rebaseline_before_capture_id == "capture-recovery-before"
    assert facts.rebaseline_after_capture_id == capture.capture_id
    assert facts.failed_action_execution_id == "unknown-action-1"
    assert facts.restoration_action_execution_id == "recovery-navigation-action"
    assert facts.inventory_status == "insufficient"
    assert facts.equivalent is True
    assert facts.original_action_replay_authorized is False
    assert facts.unknown_action_replayed is False
    successor_metadata = first.work.frontier.observation.metadata
    assert "h5_browser_recovery_attempt" not in successor_metadata
    assert "h5_browser_recovery_attempt_history" not in successor_metadata
    parent_audit = successor_metadata["h5_browser_recovery_parent_attempt_audit"]
    audited = [H5BrowserRecoveryAttemptFacts.from_dict(item) for item in parent_audit]
    assert [item.recovery_attempt_count for item in audited] == [1, 2]
    assert audited[0].reason_code == "reopen_failed"
    audited_attempt = audited[-1]
    assert audited_attempt.status == "completed"
    assert audited_attempt.failed_work_id == prior.work_id
    assert audited_attempt.reason_code is None
    from services.api.app.infrastructure.db.explorer_work_repository import _parse_work

    parsed = _parse_work("exploration", first.work.to_dict())
    assert parsed.work_id == first.work.work_id
    assert "h5_browser_recovery_attempt" not in parsed.frontier.observation.metadata

    from services.api.app.application.exploration_work_queue import (
        CommitExplorerWorkSuccessorCommand,
    )
    from services.api.app.infrastructure.db.explorer_work_repository import (
        PostgresExplorerWorkRepository,
    )
    from services.api.tests.test_v2_control_plane_repositories import (
        FakeConnection,
        FakePool,
    )

    connection = FakeConnection(
        fetchrow_results=[
            {"status": "pending", "created": True},
            {"coverage_snapshot_id": "coverage-1"},
        ],
        fetchval_results=[2, 0, "message-1"],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))
    await repository.commit_successor(
        command=CommitExplorerWorkSuccessorCommand(
            parent_work_id=prior.work_id,
            task_id=prior.task_id,
            session_id=prior.session_id,
            lease_owner="h5-worker:h5-workflow",
            lease_token="11111111-1111-4111-8111-111111111111",
            row_version=1,
            successor=first.work,
            parent_target_status=ExplorerWorkStatus.SUPERSEDED,
        )
    )
    inserted = next(
        call for call in connection.calls if "INSERT INTO qc_v2_work_item AS existing" in call[1]
    )
    persisted = _parse_work("exploration", json.loads(inserted[2][7]))
    assert persisted == first.work
    assert "h5_browser_recovery_attempt" not in persisted.frontier.observation.metadata
    assert Assessments.calls[0]["session_id"] == recovery_session_id
    recovered_snapshot = H5TaskFrontierSnapshot.from_dict(
        first.work.frontier.observation.metadata["h5_task_frontier"]
    )
    original_candidate_id = snapshot.entries[0].candidate.candidate_id
    original_entries = [
        entry
        for entry in recovered_snapshot.entries
        if entry.candidate.candidate_id == original_candidate_id
    ]
    assert len(original_entries) == 1
    assert original_entries[0].status.value == "failed"
    assert original_entries[0].last_failure_reason == "h5_browser_loss_action_unknown"
    assert all(
        entry.candidate.candidate_id != original_candidate_id
        for entry in recovered_snapshot.pending_entries
    )

    divergent = await workflow.prepare_browser_recovery(
        replace(
            command,
            equivalent=False,
            reason_code="h5_browser_recovery_state_mismatch",
        )
    )
    divergent_facts = divergent.work.frontier.observation.metadata["h5_browser_recovery"]
    divergent_typed = H5BrowserRecoveryFacts.from_dict(divergent_facts)
    assert divergent.work.work_id == prior.work_id
    assert divergent_typed.equivalent is False
    assert divergent_typed.reason_code == "h5_browser_recovery_state_mismatch"


@pytest.mark.asyncio
async def test_browser_recovery_keeps_source_lost_and_registers_strict_new_session() -> None:
    from services.api.app.application.h5_exploration_recovery import (
        H5FailureStage,
        H5RecoveryContext,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    sessions_by_id = {
        work.session_id: SimpleNamespace(
            status=SimpleNamespace(value="active"),
            row_version=7,
        )
    }
    events = []

    class Sessions:
        async def get(self, *, session_id):
            return sessions_by_id.get(session_id)

        async def mark_lost(self, *, task_id, session_id, expected_row_version, reason):
            events.append(("lost", task_id, session_id, expected_row_version, reason))
            snapshot = SimpleNamespace(
                status=SimpleNamespace(value="lost"),
                row_version=expected_row_version + 1,
            )
            sessions_by_id[session_id] = snapshot
            return snapshot

        async def register_active(self, *, spec, session_owner, primary_surface_ref):
            events.append(("active", spec.session_id, session_owner, primary_surface_ref))
            snapshot = SimpleNamespace(
                status=SimpleNamespace(value="active"),
                row_version=0,
            )
            sessions_by_id[spec.session_id] = snapshot
            return snapshot

    class Driver:
        async def start_session(self, spec):
            events.append(("driver", spec.session_id, dict(spec.options)))
            return SimpleNamespace(primary_surface_ref=f"web-primary:{spec.session_id}")

    class RecoveryCoordinator:
        async def coordinate(self, command):
            events.append(("navigate", dict(command.intent.parameters)))
            return SimpleNamespace()

    class CaptureRuntime:
        surface_sessions = Sessions()
        surface_driver = Driver()
        coordinator = SimpleNamespace()
        h5_exploration_coordinator = RecoveryCoordinator()

        async def close_browser_session(self, session_id):
            events.append(("close", session_id))

    context = H5RecoveryContext(
        task_id=work.task_id,
        agent_run_id="run-h5",
        session_id=work.session_id,
        stage=H5FailureStage.ACTION,
        last_safe_location=work.frontier.observation.location,
        action_execution_id="unknown-action-1",
        frontier_snapshot=H5TaskFrontierSnapshot.create(max_pages=3),
        recovery_attempt_count=0,
        max_recovery_attempts=2,
        entry_origin="https://m.example.test",
        failed_work=work,
        failure_error_code="browser_session_lost",
    )
    recovery_runtime = module._H5BrowserLossRuntime(
        capture_runtime=CaptureRuntime(),
        detector=SimpleNamespace(),
        workflow=SimpleNamespace(),
        cancellation=module.H5CancellationCheckpoint(),
        now=lambda: datetime.now(UTC),
    )

    await recovery_runtime.record_source_loss(context=context)
    await recovery_runtime.reopen(context=context)
    await recovery_runtime.navigate(
        context=context,
        destination_url="https://m.example.test/home",
    )
    target_session_id = recovery_runtime._target_session_id

    assert target_session_id is not None
    assert target_session_id != work.session_id
    assert target_session_id.startswith("h5:run-h5:recovery:")
    assert len(target_session_id.rsplit(":", 1)[1]) == 20
    assert target_session_id.count(":recovery:") == 1
    assert len(target_session_id) <= 128
    assert sessions_by_id[work.session_id].status.value == "lost"
    assert sessions_by_id[target_session_id].status.value == "active"
    lost_event = events[0]
    assert lost_event[:4] == ("lost", work.task_id, work.session_id, 7)
    assert "stage=action" in lost_event[4]
    assert "action=unknown-action-1" in lost_event[4]
    assert "attempt=1" in lost_event[4]
    active_events = [event for event in events if event[0] == "active"]
    assert len(active_events) == 1
    assert active_events[0][2] == "h5-browser-recovery:run-h5"
    driver_event = next(event for event in events if event[0] == "driver")
    assert driver_event[2]["viewport_width"] == 390
    assert driver_event[2]["viewport_height"] == 844
    navigate_event = next(event for event in events if event[0] == "navigate")
    assert navigate_event[1]["viewport_width"] == 390
    assert navigate_event[1]["viewport_height"] == 844


@pytest.mark.asyncio
async def test_browser_recovery_cancellation_marks_new_session_lost() -> None:
    from services.api.app.application.h5_exploration_cancellation import (
        H5CancellationCheckpoint,
        H5ExplorationCanceled,
    )
    from services.api.app.application.h5_exploration_recovery import (
        H5FailureStage,
        H5RecoveryContext,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    target_session_id = "h5:run-h5:recovery:0123456789abcdefabcd"
    lost = []

    class Sessions:
        async def get(self, *, session_id):
            if session_id == target_session_id:
                return SimpleNamespace(status=SimpleNamespace(value="active"), row_version=4)
            return None

        async def mark_lost(self, **kwargs):
            lost.append(kwargs)

    class Cancelled:
        async def is_cancel_requested(self, *, task_id, agent_run_id):
            return True

    runtime = module._H5BrowserLossRuntime(
        capture_runtime=SimpleNamespace(surface_sessions=Sessions()),
        detector=SimpleNamespace(),
        workflow=SimpleNamespace(),
        cancellation=H5CancellationCheckpoint(Cancelled()),
        now=lambda: datetime.now(UTC),
    )
    runtime._target_session_id = target_session_id
    context = H5RecoveryContext(
        task_id=work.task_id,
        agent_run_id="run-h5",
        session_id=work.session_id,
        stage=H5FailureStage.ACTION,
        last_safe_location=work.frontier.observation.location,
        action_execution_id="unknown-action-1",
        frontier_snapshot=H5TaskFrontierSnapshot.create(max_pages=3),
        recovery_attempt_count=0,
        max_recovery_attempts=2,
        entry_origin="https://m.example.test",
        failed_work=work,
    )

    with pytest.raises(H5ExplorationCanceled):
        await runtime.checkpoint(context=context, stage="before_navigate")

    assert lost[0]["session_id"] == target_session_id
    assert lost[0]["expected_row_version"] == 4
    assert lost[0]["reason"] == "h5_browser_recovery:canceled"


@pytest.mark.asyncio
async def test_unknown_action_marks_ledger_before_attempt_bound_stops() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    from services.api.app.application.h5_exploration_recovery import (
        H5BrowserRecoveryAttemptFacts,
        H5FailureStage,
    )

    attempt = H5BrowserRecoveryAttemptFacts.create(
        task_id=work.task_id,
        agent_run_id="run-h5",
        source_session_id=work.session_id,
        failed_work_id=work.work_id,
        source_state_version=work.frontier.observation.state_version,
        frontier_snapshot_id=work.frontier_snapshot_id,
        failure_stage=H5FailureStage.ACTION,
        recovery_attempt_count=2,
        status="started",
        error_code="browser_session_lost",
        action_execution_id="unknown-action-1",
        before_capture_id="capture-before-unknown",
        after_capture_id=None,
        interaction_status=None,
        interaction_result_capture_id=None,
        started_at=work.frontier.observation.observed_at.isoformat(),
        deadline=work.deadline.isoformat(),
        remaining_duration_ms=work.budget.remaining_duration_ms,
        action_cost=2,
        remaining_actions_after_reservation=work.budget.remaining_actions,
        last_safe_location="https://m.example.test/home",
        reason_code=None,
    )
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                location="https://m.example.test/home",
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "h5_task_frontier": H5TaskFrontierSnapshot.create(max_pages=3).to_dict(),
                    "agent_run_id": "run-h5",
                    "h5_browser_recovery_attempt": attempt.to_dict(),
                },
            ),
        ),
    )
    source = SimpleNamespace(
        task_id=work.task_id,
        status=SimpleNamespace(value="active"),
        row_version=4,
    )
    lost = []

    class Sessions:
        async def get(self, *, session_id):
            assert session_id == work.session_id
            return source

        async def mark_lost(self, **kwargs):
            lost.append(kwargs)
            return SimpleNamespace(
                task_id=work.task_id,
                status=SimpleNamespace(value="lost"),
                row_version=5,
            )

    class Ledger:
        def __init__(self) -> None:
            self.calls = []

        async def fail_h5_browser_loss_unknown(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(status=SimpleNamespace(value="failed"))

    ledger = Ledger()
    stopped_sources = []

    async def close_source(session_id):
        assert session_id == work.session_id
        stopped_sources.append(session_id)

    recovery = module.H5CoordinatorBrowserLossRecovery(
        capture_runtime=SimpleNamespace(
            surface_sessions=Sessions(), close_browser_session=close_source,
        ),
        detector=SimpleNamespace(),
        workflow=SimpleNamespace(),
        action_ledger=ledger,
    )
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-before-unknown",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="unknown-action-1",
    )

    outcome = await recovery.recover(
        work=work,
        agent_run_id="run-h5",
        error=CaptureCoordinationError(
            stage=CaptureCoordinationStage.ACTION,
            error_code="browser_session_lost",
            message="lost",
            cause_type="TargetClosedError",
            before_capture=before,
        ),
    )

    assert outcome.reason_code == "h5_browser_recovery_attempts_exhausted"
    assert stopped_sources == [work.session_id]
    assert len(lost) == 1
    assert len(ledger.calls) == 1
    assert ledger.calls[0]["action_execution_id"] == "unknown-action-1"
    assert ledger.calls[0]["source_capture_id"] == before.capture_id


@pytest.mark.asyncio
async def test_recovery_checks_cancellation_immediately_before_coordinator_action() -> None:
    """若 Runner 检查后恢复适配器不复查，BACK/NAVIGATE 仍可穿过取消窗口。"""

    from services.api.app.application.h5_exploration_cancellation import (
        H5CancellationCheckpoint,
        H5ExplorationCanceled,
    )
    from services.api.app.application.h5_exploration_observation import build_h5_frontier
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    detection = {
        "page_capture_id": "capture-home",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
    }
    source_state = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.entries[0].entry_id)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)

    class Cancelled:
        async def is_cancel_requested(self, *, task_id, agent_run_id):
            return True

    class Coordinator:
        async def coordinate(self, command):
            raise AssertionError("取消命中后不得调用恢复 Coordinator")

    class Detector:
        async def detect(self, **kwargs):
            raise AssertionError("取消命中后不得检测")

    recovery = module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=_NoopAssessments(),
        cancellation=H5CancellationCheckpoint(Cancelled()),
    )

    with pytest.raises(H5ExplorationCanceled):
        await recovery.restore(
            work=work,
            agent_run_id="run-h5",
            entry=snapshot.entries[0],
        )


@pytest.mark.asyncio
async def test_recovery_preserves_action_evidence_then_stops_before_detection() -> None:
    """若恢复动作后的取消仍执行 detect/assess，会在已有 before/after 后继续扩张副作用。"""

    from services.api.app.application.h5_exploration_cancellation import (
        H5CancellationCheckpoint,
        H5ExplorationCanceled,
    )
    from services.api.app.application.h5_exploration_observation import build_h5_frontier
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    detection = {
        "page_capture_id": "capture-home",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
    }
    source_state = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.entries[0].entry_id)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
    requested = False
    captures: list[str] = []

    class Source:
        async def is_cancel_requested(self, *, task_id, agent_run_id):
            return requested

    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-after",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="restore-action",
    )

    class Coordinator:
        async def coordinate(self, command):
            nonlocal requested
            captures.extend(["before_action", "after_action"])
            requested = True
            return CaptureCoordinationResult(
                before_capture=replace(after, capture_id="capture-before", role="before_action"),
                interaction=InteractionResult(
                    task_id=work.task_id,
                    session_id=work.session_id,
                    intent_id=command.intent.intent_id,
                    action_execution_id="restore-action",
                    source_capture_id="capture-before",
                    status=InteractionResultStatus.SUCCEEDED,
                    result_capture_id=after.capture_id,
                ),
                after_capture=after,
            )

    class Detector:
        async def detect(self, **kwargs):
            raise AssertionError("恢复动作完成后命中取消不得调用 detect")

    recovery = module.H5CoordinatorFrontierRecovery(
        capture_coordinator=Coordinator(),
        detector=Detector(),
        assessment_repository=_NoopAssessments(),
        cancellation=H5CancellationCheckpoint(Source()),
    )

    with pytest.raises(H5ExplorationCanceled):
        await recovery.restore(work=work, agent_run_id="run-h5", entry=snapshot.entries[0])
    assert captures == ["before_action", "after_action"]


def test_cancelled_turn_outcome_maps_without_rereading_redis() -> None:
    """若取消 outcome 被映射为 incomplete，Redis 键过期后会把已取消 Run 误投影为未完成。"""

    module = _runtime_module()
    result = module._outcome_result(
        module.H5ExplorationTurnOutcome(
            claimed=True,
            work_id="work-1",
            final_status=ExplorerWorkStatus.CANCELLED,
            error_code="WORKFLOW_CANCELED",
        ),
        step_count=2,
    )

    assert result.termination_reason is TerminationReason.CANCELED
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW


def test_recovery_exhaustion_maps_to_incomplete_needs_review() -> None:
    """有界恢复耗尽是业务证据不完整，不得伪装为基础设施失败。"""

    module = _runtime_module()
    result = module._outcome_result(
        module.H5ExplorationTurnOutcome(
            claimed=True,
            work_id="work-1",
            final_status=ExplorerWorkStatus.BOUNDED,
            error_code="h5_browser_recovery_attempts_exhausted",
        ),
        step_count=2,
    )

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_category is None


def test_retry_wait_exhaustion_maps_to_incomplete_needs_review() -> None:
    """LLM 网关抖动重试耗尽属于暂时性基础设施中止，已探索证据必须转人工而非报错。"""

    module = _runtime_module()
    result = module._outcome_result(
        module.H5ExplorationTurnOutcome(
            claimed=True,
            work_id="work-1",
            final_status=ExplorerWorkStatus.RETRY_WAIT,
            error_code="H5_EXPLORATION_TURN_FAILED",
        ),
        step_count=2,
    )

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.error_message == "H5_EXPLORATION_TURN_FAILED"


@pytest.mark.asyncio
async def test_prepare_close_bounds_leftover_exploration_work_before_session_close() -> None:
    """收场必须先冻结残余探索工作，再允许规则排空和 Session 关闭。"""

    module = _runtime_module()
    events: list[object] = []

    class _Runner:
        async def cancel_nonterminal(self, *, task_id: str, reason: str = "") -> int:
            events.append(("cancel_nonterminal", task_id, reason))
            return 1

    class _CaptureRuntime:
        surface_sessions = None

        async def close_h5_exploration_session(self, *, task_id: str, session_id: str) -> None:
            events.append(("close_session", task_id, session_id))

    runtime = module.H5V2Runtime(
        capture_runtime=_CaptureRuntime(),
        workflow=object(),
        turn_runner=_Runner(),
        runtime_settings=_runtime_settings(max_actions=3),
    )

    await runtime.prepare_close(task_id="task-h5")
    await runtime.close(task_id="task-h5", session_id="h5:run-1")

    assert events[0][0] == "cancel_nonterminal"
    assert events[0][1] == "task-h5"
    assert "有界收束" in events[0][2]
    assert events[1] == ("close_session", "task-h5", "h5:run-1")


@pytest.mark.asyncio
async def test_prepare_close_reconciles_terminal_frontier_before_session_close() -> None:
    """H5 收场必须在关闭 Session 前固化最终 Frontier 与 Coverage。"""

    module = _runtime_module()
    events: list[object] = []

    class _Runner:
        async def cancel_nonterminal(self, *, task_id: str, reason: str = "") -> int:
            events.append(("cancel_nonterminal", task_id, reason))
            return 0

        async def reconcile_terminal_frontier(self, *, task_id: str) -> bool:
            events.append(("reconcile_terminal_frontier", task_id))
            return True

    class _CaptureRuntime:
        surface_sessions = None

        async def close_h5_exploration_session(self, *, task_id: str, session_id: str) -> None:
            events.append(("close_session", task_id, session_id))

    runtime = module.H5V2Runtime(
        capture_runtime=_CaptureRuntime(),
        workflow=object(),
        turn_runner=_Runner(),
        runtime_settings=_runtime_settings(max_actions=3),
    )

    await runtime.prepare_close(task_id="task-h5")
    await runtime.close(task_id="task-h5", session_id="h5:run-1")

    assert events[0][0] == "cancel_nonterminal"
    assert events[1] == ("reconcile_terminal_frontier", "task-h5")
    assert events[2] == ("close_session", "task-h5", "h5:run-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ("assess", "state_equivalent", "prepare_browser_recovery"),
)
async def test_every_post_reopen_failure_marks_target_recovery_session_lost(
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application.h5_exploration_recovery import (
        H5FailureStage,
        H5RecoveryContext,
        build_h5_recovery_session_id,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    target_session_id = build_h5_recovery_session_id(
        agent_run_id="run-h5",
        work_id=work.work_id,
        stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
    )
    lost: list[dict[str, object]] = []

    class Sessions:
        async def get(self, *, session_id):
            assert session_id == target_session_id
            return SimpleNamespace(
                task_id=work.task_id,
                session_id=session_id,
                status=SimpleNamespace(value="active"),
                row_version=7,
            )

        async def mark_lost(self, **kwargs):
            lost.append(kwargs)
            return SimpleNamespace(status=SimpleNamespace(value="lost"), row_version=8)

    class Workflow:
        async def prepare_browser_recovery(self, command):
            del command
            raise RuntimeError("prepare failed")

    runtime = module._H5BrowserLossRuntime(
        capture_runtime=SimpleNamespace(surface_sessions=Sessions()),
        detector=SimpleNamespace(),
        workflow=Workflow(),
        cancellation=module.H5CancellationCheckpoint(),
        now=lambda: work.frontier.observation.observed_at,
    )
    runtime._target_session_id = target_session_id
    context = H5RecoveryContext(
        task_id=work.task_id,
        agent_run_id="run-h5",
        session_id=work.session_id,
        stage=H5FailureStage.ACTION,
        last_safe_location=work.frontier.observation.location,
        action_execution_id="action-unknown",
        frontier_snapshot=H5TaskFrontierSnapshot.create(max_pages=3),
        recovery_attempt_count=0,
        max_recovery_attempts=2,
        failed_work=work,
    )
    capture = CaptureRef(
        task_id=work.task_id,
        session_id=target_session_id,
        capture_id="capture-after-recovery",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id="capture-before-recovery",
        action_execution_id="action-recovery",
    )
    detection = {
        "page_capture_id": capture.capture_id,
        "page_url": work.frontier.observation.location,
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "candidate_actions": [],
    }
    if failure_point == "assess":
        monkeypatch.setattr(
            module,
            "adapt_h5_page_detection_to_assessment",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("assess failed")),
        )
        call = runtime.assess(context=context, capture=capture, detection=detection)
    elif failure_point == "state_equivalent":
        monkeypatch.setattr(
            module,
            "build_h5_frontier",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("equivalence failed")),
        )
        call = runtime.state_equivalent(
            context=context,
            capture=capture,
            detection=detection,
            assessment=SimpleNamespace(execution_status="skipped"),
        )
    else:
        from services.api.app.application.h5_page_type_assessment_adapter import (
            H5PageTypeAssessmentAdapterResult,
        )

        call = runtime.persist_recovery(
            context=context,
            capture=capture,
            detection=detection,
            assessment=H5PageTypeAssessmentAdapterResult(execution_status="skipped"),
            equivalent=True,
            reason_code=None,
            recovery_attempt_count=1,
        )

    with pytest.raises(RuntimeError):
        await call

    assert lost[0]["session_id"] == target_session_id
    assert lost[0]["expected_row_version"] == 7
    assert str(lost[0]["reason"]).startswith("h5_browser_recovery:")


@pytest.mark.asyncio
async def test_failed_unknown_recovery_reserves_new_attempt_and_preserves_history() -> None:
    from services.api.app.application.h5_exploration_recovery import (
        H5BrowserRecoveryAttemptFacts,
        H5FailureStage,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    first = H5BrowserRecoveryAttemptFacts.create(
        task_id=work.task_id,
        agent_run_id="run-h5",
        source_session_id=work.session_id,
        failed_work_id=work.work_id,
        source_state_version=work.frontier.observation.state_version,
        frontier_snapshot_id=work.frontier_snapshot_id,
        failure_stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
        status="started",
        error_code="page_capture_session_crashed",
        action_execution_id="unknown-action-1",
        before_capture_id="capture-before-unknown",
        after_capture_id=None,
        interaction_status="failed",
        interaction_result_capture_id=None,
        started_at=work.frontier.observation.observed_at.isoformat(),
        deadline=work.deadline.isoformat(),
        remaining_duration_ms=work.budget.remaining_duration_ms,
        action_cost=2,
        remaining_actions_after_reservation=work.budget.remaining_actions,
        last_safe_location=work.frontier.observation.location,
        reason_code=None,
    ).complete(reason_code="h5_browser_recovery_side_effect_failed")
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "agent_run_id": "run-h5",
                    "h5_browser_recovery_attempt": first.to_dict(),
                },
            ),
        ),
    )
    recovery = module.H5CoordinatorBrowserLossRecovery(
        capture_runtime=SimpleNamespace(),
        detector=SimpleNamespace(),
        workflow=SimpleNamespace(),
    )

    reservation = await recovery.reserve_attempt(
        work=work,
        agent_run_id="run-h5",
        error=CaptureCoordinationError(
            stage=CaptureCoordinationStage.ACTION,
            error_code="page_capture_session_crashed",
            message="resume",
            cause_type="PersistedBrowserLossAttempt",
        ),
    )

    metadata = reservation.work.frontier.observation.metadata
    second = H5BrowserRecoveryAttemptFacts.from_dict(metadata["h5_browser_recovery_attempt"])
    assert second.recovery_attempt_count == 2
    assert second.action_execution_id == first.action_execution_id
    assert second.before_capture_id == first.before_capture_id
    history = metadata["h5_browser_recovery_attempt_history"]
    assert history == (first.to_dict(),) or history == [first.to_dict()]
    assert history[0]["payload_hash"] != second.payload_hash


@pytest.mark.asyncio
async def test_completion_wrapper_rejects_delegate_completed_after_auth_history() -> None:
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_blocked(
        entry.entry_id,
        reason="authentication_required",
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    blocked_work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    delegate_called = False

    class Delegate:
        async def decide_next(self, delegated_work):
            nonlocal delegate_called
            delegate_called = True
            return ExplorationTerminal(
                work_id=delegated_work.work_id,
                task_id=delegated_work.task_id,
                status=ExplorationTerminalStatus.COMPLETED,
                reason="model declared done",
            )

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(blocked_work)

    assert decision.status is ExplorationTerminalStatus.BLOCKED
    assert "authentication" in decision.reason
    assert delegate_called is False


@pytest.mark.asyncio
async def test_completion_wrapper_keeps_real_blocker_when_target_covered() -> None:
    """业务目标已覆盖也不能把真实动作阻断提升为探索完成。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_blocked(
        entry.entry_id,
        reason="semantic_target_not_found",
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            metadata={**frontier.observation.metadata, "objective_satisfied": True},
        ),
    )
    blocked_work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Delegate:
        async def decide_next(self, delegated_work):
            del delegated_work
            raise AssertionError("目标页面覆盖完成后不应继续调用 LLM 处理阻塞 Frontier")

    decision = await module.H5CompletionAwareExplorer(delegate=Delegate()).decide_next(blocked_work)

    assert decision.status is ExplorationTerminalStatus.BLOCKED
    assert decision.reason == "task_frontier_blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("restored_source", [False, True])
async def test_executor_does_not_count_sibling_reselection_against_retry_cap(
    restored_source,
) -> None:
    """换一条 Frontier sibling 是进展；若计入连投上限，前沿还剩几条就被
    2 次连投掐死（实测 91蜜桃 R5 剩 6 条 pending 即收场）。"""
    module = _runtime_module()
    turn_count = 0

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=6)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            nonlocal turn_count
            del task_id, agent_run_id
            turn_count += 1
            # 连续 4 次改选重投：旧口径在第 3 次就会 break
            if turn_count <= 4:
                return H5ExplorationTurnOutcome(
                    claimed=True,
                    work_id=f"work-{turn_count}",
                    final_status=ExplorerWorkStatus.RETRY_WAIT,
                    error_code="h5_frontier_restore_exhausted",
                    reselected_sibling=not restored_source,
                    restored_source=restored_source,
                )
            return _completed_outcome()

        async def next_exploration_claim_at(self, *, task_id):
            del task_id
            return None

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        retry_wait_seconds=0,
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert turn_count == 5
    assert result.termination_reason is TerminationReason.COMPLETED


@pytest.mark.asyncio
async def test_executor_still_caps_plain_retry_wait_without_reselection() -> None:
    """原地重投（未改选）仍受连投上限约束，防基础设施抖动打转。"""
    module = _runtime_module()
    turn_count = 0

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=6)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            nonlocal turn_count
            del task_id, agent_run_id
            turn_count += 1
            return H5ExplorationTurnOutcome(
                claimed=True,
                work_id=f"work-{turn_count}",
                final_status=ExplorerWorkStatus.RETRY_WAIT,
                error_code="h5_frontier_restore_state_mismatch",
            )

        async def next_exploration_claim_at(self, *, task_id):
            del task_id
            return None

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        retry_wait_seconds=0,
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert turn_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("close_failure", [False, True])
@pytest.mark.parametrize(
    ("runtime_settings", "expected_error"),
    [
        (
            _runtime_settings(max_actions=6, max_ai_calls=10, max_total_tokens=100),
            "h5_llm_token_budget_exhausted",
        ),
        (
            _runtime_settings(max_actions=6, max_ai_calls=2, max_total_tokens=10_000),
            "h5_llm_call_budget_exhausted",
        ),
    ],
)
async def test_executor_stops_before_next_turn_when_llm_budget_is_exhausted(
    runtime_settings: H5TaskRuntimeSettings,
    expected_error: str,
    close_failure: bool,
) -> None:
    module = _runtime_module()
    turn_count = 0

    class Runtime:
        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            nonlocal turn_count
            del task_id, agent_run_id
            turn_count += 1
            meter.record(
                {
                    "input_tokens": 40,
                    "output_tokens": 20,
                    "total_tokens": 60,
                }
            )
            return H5ExplorationTurnOutcome(
                claimed=True,
                work_id=f"work-{turn_count}",
                final_status=ExplorerWorkStatus.SUPERSEDED,
                successor_work_id=f"work-{turn_count + 1}",
            )

        async def next_exploration_claim_at(self, *, task_id):
            del task_id
            return None

        async def close(self, *, task_id, session_id):
            del task_id, session_id
            if close_failure:
                raise RuntimeError("active-session 未排空：pending_actions=1")

    runtime = Runtime()
    runtime.runtime_settings = runtime_settings
    with bind_run_llm_usage_meter() as meter:
        result = await module.ProductionH5V2RuntimeExecutor(
            worker_instance_id="worker-h5",
            retry_wait_seconds=0,
        ).run(runtime, record=_h5_record(), settings=Settings())

    assert turn_count == 2
    assert result.termination_reason is (
        TerminationReason.ERROR if close_failure else TerminationReason.INCOMPLETE
    )
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    if close_failure:
        assert result.error_message.startswith(expected_error)
        assert "pending_actions=1" in result.error_message
    else:
        assert result.error_message == expected_error


@pytest.mark.asyncio
async def test_executor_counts_judge_usage_from_other_consumer_before_claim() -> None:
    """规则消费者有独立 Run 时，H5 仍须遵守同 Task 预算。"""
    module = _runtime_module()

    class Reader:
        async def usage_totals_for_task(self, *, task_id):
            assert task_id
            return 96, 1000

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=6, max_ai_calls=96)

        async def start(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id):
            pytest.fail("审计预算耗尽后不应领取下一轮探索工作")

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    result = await module.ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker-h5",
        audited_usage_reader=Reader(),
    ).run(Runtime(), record=_h5_record(), settings=Settings())

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.error_message == "h5_llm_call_budget_exhausted"


@pytest.mark.asyncio
async def test_executor_waits_for_local_usage_audit_before_cross_consumer_budget_read() -> None:
    """本地调用尚在异步落库时，不能用旧审计汇总放行下一轮。"""
    module = _runtime_module()
    audited_calls = 0

    async def persist_usage() -> None:
        nonlocal audited_calls
        await asyncio.sleep(0.01)
        audited_calls = 2

    class Reader:
        async def usage_totals_for_task(self, *, task_id):
            assert task_id == "task-1"
            return audited_calls, 100

    with bind_run_llm_usage_meter() as meter:
        meter.track_audit_task(asyncio.create_task(persist_usage()))
        result = await module.ProductionH5V2RuntimeExecutor(
            worker_instance_id="worker-h5",
            audited_usage_reader=Reader(),
        )._usage_budget_error(
            _runtime_settings(max_actions=6, max_ai_calls=2),
            task_id="task-1",
        )

    assert result == "h5_llm_call_budget_exhausted"


@pytest.mark.asyncio
async def test_executor_rejects_failed_local_usage_audit_before_budget_read() -> None:
    """本地用量流水写入失败时不能靠不完整的跨消费者汇总放行。"""
    module = _runtime_module()

    async def fail_persist() -> None:
        raise RuntimeError("audit storage unavailable")

    class Reader:
        async def usage_totals_for_task(self, *, task_id):
            pytest.fail(f"审计写入失败后不应读取不完整的汇总：{task_id}")

    with bind_run_llm_usage_meter() as meter:
        meter.track_audit_task(asyncio.create_task(fail_persist()))
        with pytest.raises(RuntimeError, match="h5_llm_usage_audit_unavailable"):
            await module.ProductionH5V2RuntimeExecutor(
                worker_instance_id="worker-h5",
                audited_usage_reader=Reader(),
            )._usage_budget_error(
                _runtime_settings(max_actions=6, max_ai_calls=2),
                task_id="task-1",
            )


def test_h5_payment_collection_parameters_keep_read_only_horizontal_boundary() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-payment-horizontal",
        semantic_target="浏览全部会员卡",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "payment",
            "source_page_state": "stable",
            "purpose": "横向浏览当前支付页全部会员套餐",
            "target_keywords": [
                "payment_horizontal_collection",
                "read_only_collection",
                "page_type:payment",
            ],
            "action_parameters": {
                "target_mode": "h5_payment_horizontal_collection",
                "horizontal_scroll_max_segments": 6,
                "split_scroll_screenshots": True,
                "source_page_type": "payment",
            },
        },
    )
    work = _work()
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                page_type="payment",
                page_state="stable",
            ),
            candidates=(candidate,),
        ),
    )

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters["target_mode"] == "h5_payment_horizontal_collection"
    assert parameters["horizontal_scroll_max_segments"] == 6
    assert parameters["split_scroll_screenshots"] is True


async def test_source_restore_audit_keeps_underlying_failure_before_exhaustion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """终态耗尽不应抹掉是哪一步恢复失败，且审计必须有任务和工作身份。"""
    from services.api.app.application.h5_exploration_recovery import H5RestorationResult
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).record_restoration_attempt(entry.entry_id)
    entry = next(item for item in snapshot.entries if item.entry_id == entry.entry_id)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)

    async def failed_restore(**kwargs):
        assert kwargs["entry"].entry_id == entry.entry_id
        return H5RestorationResult(
            entry_id=entry.entry_id, restored=False, restoration_attempt_count=2,
            route="navigate", reason_code="h5_frontier_restore_popup_blocked",
        )

    monkeypatch.setattr(module, "restore_h5_frontier_source", failed_restore)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        result = await module.H5CoordinatorFrontierRecovery(
            capture_coordinator=object(), detector=object(),
            assessment_repository=_NoopAssessments(),
        ).restore(work=work, agent_run_id="run-h5", entry=entry)

    assert result.reason_code == "h5_frontier_restore_exhausted"
    assert "reason=h5_frontier_restore_popup_blocked" in caplog.text
    assert f"task_id={work.task_id}" in caplog.text
    assert f"work_id={work.work_id}" in caplog.text
    assert f"entry_id={entry.entry_id}" in caplog.text
    assert "attempt=2 route=navigate" in caplog.text


def test_h5_entitlement_collection_parameters_keep_read_only_horizontal_boundary() -> None:
    from services.api.tests.test_h5_exploration_turn import _work

    module = _runtime_module()
    candidate = CandidateAction(
        candidate_id="candidate-entitlement-horizontal",
        semantic_target="浏览全部会员卡",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_detail",
            "source_page_state": "entitlement_locked",
            "purpose": "横向浏览当前详情页权益层全部会员套餐",
            "target_keywords": [
                "payment_horizontal_collection",
                "read_only_collection",
                "page_type:video_detail",
            ],
            "action_parameters": {
                "target_mode": "h5_payment_horizontal_collection",
                "horizontal_scroll_max_segments": 6,
                "split_scroll_screenshots": True,
                "source_page_type": "video_detail",
            },
        },
    )
    work = _work()
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                page_type="video_detail",
                page_state="entitlement_locked",
            ),
            candidates=(candidate,),
        ),
    )

    parameters = module._h5_candidate_parameters(work=work, candidate=candidate)

    assert parameters["target_mode"] == "h5_payment_horizontal_collection"
    assert parameters["source_page_type"] == "video_detail"
    assert parameters["horizontal_scroll_max_segments"] == 6
