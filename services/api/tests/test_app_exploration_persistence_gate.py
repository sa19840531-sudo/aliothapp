"""App Persistence Gate：封存 Index，不派生 Probe / Inspection。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.models import ExecutionOutcome, TerminationReason
from services.api.app.application.app_exploration_persistence_gate import (
    AppExplorationPersistenceGate,
    MemoryEvidenceIndexRepository,
    build_memory_persistence_gate,
    merge_close_readiness_gaps,
)
from services.api.app.application.app_exploration_turn import AppExplorationTurnOutcome
from services.api.app.application.app_runtime_composition import (
    ProductionAppV2RuntimeExecutor,
)
from services.api.app.application.evidence_indexes import EvidenceInventory
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.surface_sessions import SessionCloseReadiness
from services.api.app.core.config import Settings
from services.api.app.domain.evidence.index import (
    EvidenceActionLogItem,
    EvidenceArtifactIndexItem,
    EvidenceCaptureLineage,
    EvidenceGapCode,
    EvidenceGapItem,
    ExplorationEvidenceStatus,
    build_evidence_index_reference,
)
from services.api.app.domain.exploration import (
    AppExplorationOutcome,
    ExplorationTerminal,
    ExplorationTerminalStatus,
)
from services.api.app.domain.interaction import (
    AppReadyHandoff,
    CaptureKind,
    CaptureRef,
    SurfaceKind,
    build_android_screenshot_only_manifest,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


def _now() -> datetime:
    return datetime(2026, 8, 11, 9, 30, tzinfo=UTC)


def _gap(*, code: EvidenceGapCode = EvidenceGapCode.PENDING_MANIFEST) -> EvidenceGapItem:
    return EvidenceGapItem(
        code=code,
        subject_id="capture-1",
        detail="Capture 没有 pass Manifest",
    )


def _app_record() -> AgentRunRecord:
    """构造与当前动态 App 策略合同一致的最小运行记录。"""

    return AgentRunRecord(
        run_id="run-app",
        task_id="task-app",
        message="检查",
        submitted_at=_now(),
        task_config={
            "workflow_version": "v2",
            "scan_mode": "full",
            "max_depth": 4,
            "journey_steps": [],
            "_task_target": {
                "platform": "app",
                "target_url": None,
                "app": {
                    "platform_build_ref": "app-build:build-1",
                    "package_name": "com.example.target",
                    "device": {"backend": "cloud_phone", "profile_id": "pixel-8"},
                },
            },
        },
        loop_guard={
            "max_depth": 4,
            "max_duration_seconds": 3_600,
        },
    )


def test_completed_index_rejects_gaps() -> None:
    with pytest.raises(Exception, match="不得携带"):
        build_evidence_index_reference(
            task_id="task-1",
            session_id="app:task-1",
            status=ExplorationEvidenceStatus.EXPLORATION_COMPLETED,
            sealed_at=_now(),
            evidence_gap=(_gap(),),
        )


def test_insufficient_index_requires_gap() -> None:
    with pytest.raises(Exception, match="必须说明缺口"):
        build_evidence_index_reference(
            task_id="task-1",
            session_id="app:task-1",
            status=ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT,
            sealed_at=_now(),
        )


@pytest.mark.asyncio
async def test_gate_seals_completed_without_probe_or_inspection() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=True,
            capture_ids=("capture-1",),
            manifest_ids=("manifest-1",),
            page_timeline=("2026-08-11T09:30:00+00:00|capture-1|home|首页",),
            capture_lineage=(
                EvidenceCaptureLineage(capture_id="capture-1", role="bootstrap"),
            ),
            artifacts=(
                EvidenceArtifactIndexItem(
                    artifact_id="art-1",
                    artifact_type="screenshot",
                    checksum="a" * 64,
                    capture_id="capture-1",
                ),
            ),
            action_logs=(
                EvidenceActionLogItem(
                    action_execution_id="action-1",
                    artifact_id="art-trace",
                    checksum="b" * 64,
                    capture_id="capture-1",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(repository=store, now=_now)
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EXPLORATION_COMPLETED
    assert decision.index.manifest_ids == ("manifest-1",)
    assert decision.index.evidence_gap == ()
    assert decision.index.page_timeline[0].endswith("|home|首页")
    assert decision.index.capture_lineage[0].capture_id == "capture-1"
    assert any(item.role == "exploration_terminal" for item in decision.index.capture_lineage)
    assert store.terminal_observation_calls == ["exploration_terminal"]
    assert decision.index.artifacts[0].checksum == "a" * 64
    assert decision.index.action_logs[0].action_execution_id == "action-1"
    assert decision.page_timeline[0].endswith("|home|首页")
    assert store.seal_calls == 1
    assert store.supersede_calls == 1
    assert store.retry_calls == 0
    assert not hasattr(store, "publish_for_capture")
    assert not hasattr(store, "route_evidence_request")


@pytest.mark.asyncio
async def test_gate_seals_insufficient_when_pending_manifest_times_out() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(_gap(),),
        )
    )
    gate = AppExplorationPersistenceGate(
        repository=store,
        now=_now,
        max_write_retries=2,
        retry_sleep_seconds=0,
    )
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.deferred is False
    assert any(item.code is EvidenceGapCode.PENDING_MANIFEST for item in decision.gaps)
    assert any(item.code is EvidenceGapCode.WRITE_TIMEOUT for item in decision.gaps)
    assert any(item.role == "evidence_insufficient" for item in decision.index.capture_lineage)
    assert store.terminal_observation_calls == ["evidence_insufficient"]
    assert store.seal_calls == 1
    assert store.retry_calls == 2
    assert store.fail_pending_calls == 1


@pytest.mark.asyncio
async def test_gate_seals_insufficient_when_work_still_in_flight_after_retries() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.IN_FLIGHT_WORK,
                    subject_id="work-1",
                    detail="Exploration Work 仍在途（running）",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(
        repository=store,
        now=_now,
        max_write_retries=1,
        retry_sleep_seconds=0,
    )
    decision = await gate.cutoff(
        task_id="task-1",
        session_id="app:task-1",
        agent_run_id="run-1",
    )
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.deferred is False
    assert any(item.code is EvidenceGapCode.IN_FLIGHT_WORK for item in decision.gaps)
    assert any(item.code is EvidenceGapCode.WRITE_TIMEOUT for item in decision.gaps)
    assert store.seal_calls == 1
    assert store.sealed_agent_run_ids == ["run-1"]
    assert store.terminal_observation_calls == ["evidence_insufficient"]


@pytest.mark.asyncio
async def test_gate_retries_original_write_then_seals_completed() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(_gap(),),
        ),
        retry_inventory=EvidenceInventory(
            complete=True,
            capture_ids=("capture-1",),
            manifest_ids=("manifest-1",),
        ),
    )
    gate = AppExplorationPersistenceGate(
        repository=store,
        now=_now,
        max_write_retries=2,
        retry_sleep_seconds=0,
    )
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EXPLORATION_COMPLETED
    assert store.retry_calls == 1
    assert store.seal_calls == 1


@pytest.mark.asyncio
async def test_gate_seals_insufficient_for_missing_ui_tree() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.MISSING_UI_TREE,
                    subject_id="capture-1",
                    detail="已提交 Capture 缺少 UI Tree",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(repository=store, now=_now)
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.index.evidence_gap[0].code is EvidenceGapCode.MISSING_UI_TREE
    assert store.retry_calls == 0


@pytest.mark.asyncio
async def test_gate_inventory_failure_becomes_insufficient() -> None:
    class BrokenStore(MemoryEvidenceIndexRepository):
        async def inventory(self, *, task_id: str, session_id: str):
            del task_id, session_id
            raise RuntimeError("db down")

    store = BrokenStore()
    gate = AppExplorationPersistenceGate(repository=store, now=_now)
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.index.evidence_gap[0].code is EvidenceGapCode.INVENTORY_FAILED


@pytest.mark.asyncio
async def test_executor_downgrades_completed_turn_when_evidence_insufficient() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.MISSING_UI_TREE,
                    subject_id="capture-1",
                    detail="已提交 Capture 缺少 UI Tree",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(repository=store, now=_now)

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
                installed_package="com.example.target",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None:
                return AppExplorationTurnOutcome(claimed=False)
            del task_id, agent_run_id
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

    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        persistence_gate=gate,
    ).run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.exploration_status == "evidence_insufficient"
    assert result.evidence_index_id
    assert result.evidence_gap
    assert store.seal_calls == 1


@pytest.mark.asyncio
async def test_executor_returns_completed_index_when_inventory_passes() -> None:
    gate = build_memory_persistence_gate(now=_now)

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
                installed_package="com.example.target",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            if work_type is not None:
                return AppExplorationTurnOutcome(claimed=False)
            del task_id, agent_run_id
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

    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        persistence_gate=gate,
    ).run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.exploration_status == "exploration_completed"
    assert result.evidence_index_id
    assert result.evidence_gap == ()
    assert result.page_timeline == ()


@pytest.mark.asyncio
async def test_executor_defers_cutoff_and_skips_probe_on_inflight_writes() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(complete=False, gaps=(_gap(),))
    )
    gate = AppExplorationPersistenceGate(
        repository=store,
        now=_now,
        max_write_retries=1,
        retry_sleep_seconds=0,
    )
    claimed_types: list[object] = []

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
                installed_package="com.example.target",
            )

        async def publish_exploration(self, command):
            del command

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            claimed_types.append(work_type)
            del task_id, agent_run_id
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

    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="worker-1",
        persistence_gate=gate,
    ).run(
        Runtime(),
        record=_app_record(),
        settings=Settings(),
    )
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.exploration_status == "evidence_insufficient"
    assert result.evidence_index_id
    assert result.evidence_gap
    assert store.seal_calls == 1
    assert claimed_types == [None]


def test_apply_result_copies_exploration_fields_onto_run_record() -> None:
    from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
    from services.api.app.agents.runtime.models import RunResult
    from services.api.app.agents.runtime.working_memory import WorkingMemory
    from services.api.app.infrastructure.queue.state_store import _output_context
    from services.api.app.worker import _apply_result

    result = RunResult(
        termination_reason=TerminationReason.INCOMPLETE,
        working_memory=WorkingMemory(step_count=1),
        trace_sink=InMemoryActionTraceSink(),
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        evidence_index_id="evidence-index:abc",
        exploration_status="evidence_insufficient",
        evidence_gap=("Manifest 仍为 pending",),
        page_timeline=("2026-08-11T09:30:00+00:00|capture-1|home",),
    )
    record = AgentRunRecord(
        run_id="run-1",
        task_id="task-1",
        message="检查",
        submitted_at=_now(),
    )
    _apply_result(record, result)
    assert record.evidence_index_id == "evidence-index:abc"
    assert record.exploration_status == "evidence_insufficient"
    assert record.evidence_gap == ["Manifest 仍为 pending"]
    assert record.page_timeline == ["2026-08-11T09:30:00+00:00|capture-1|home"]
    payload = _output_context(record)
    assert payload["evidence_index_id"] == "evidence-index:abc"
    assert payload["exploration_status"] == "evidence_insufficient"
    assert payload["page_timeline"] == record.page_timeline


@pytest.mark.asyncio
async def test_gate_seals_insufficient_when_device_command_still_in_flight() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.IN_FLIGHT_COMMAND,
                    subject_id="app:task-1",
                    detail="在途设备命令未排空",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(
        repository=store,
        now=_now,
        max_write_retries=1,
        retry_sleep_seconds=0,
    )
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.deferred is False
    assert any(item.code is EvidenceGapCode.IN_FLIGHT_COMMAND for item in decision.gaps)
    assert any(item.code is EvidenceGapCode.WRITE_TIMEOUT for item in decision.gaps)
    assert store.seal_calls == 1
    assert store.fail_pending_calls == 1


@pytest.mark.asyncio
async def test_gate_polls_close_readiness_and_times_out_complete_inventory() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(complete=True, capture_ids=("capture-1",))
    )
    gate = AppExplorationPersistenceGate(
        repository=store,
        now=_now,
        max_write_retries=1,
        retry_sleep_seconds=0,
    )

    class Sessions:
        async def close_readiness(self, *, task_id: str, session_id: str):
            del task_id
            return SessionCloseReadiness(
                session_id=session_id,
                ready=False,
                pending_actions=1,
                pending_capture_commits=0,
                active_evidence_requests=0,
                running_restorations=0,
                nonterminal_active_work_items=0,
                pinned_surfaces=0,
            )

    decision = await gate.cutoff(
        task_id="task-1",
        session_id="app:task-1",
        runtime=SimpleNamespace(surface_sessions=Sessions()),
        agent_run_id="run-drain",
    )
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.deferred is False
    assert any(item.code is EvidenceGapCode.IN_FLIGHT_COMMAND for item in decision.gaps)
    assert any(item.code is EvidenceGapCode.WRITE_TIMEOUT for item in decision.gaps)
    assert store.seal_calls == 1
    assert store.sealed_agent_run_ids == ["run-drain"]


@pytest.mark.asyncio
async def test_gate_seals_insufficient_when_object_write_times_out() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.PENDING_OBJECT_WRITE,
                    subject_id="app:task-1",
                    detail="对象存储仍在途",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(
        repository=store,
        now=_now,
        max_write_retries=1,
        retry_sleep_seconds=0,
    )
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert any(item.code is EvidenceGapCode.PENDING_OBJECT_WRITE for item in decision.gaps)
    assert any(item.code is EvidenceGapCode.WRITE_TIMEOUT for item in decision.gaps)
    assert store.fail_pending_calls == 1


@pytest.mark.asyncio
async def test_gate_seals_insufficient_for_corrupt_artifact_without_retry() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.CORRUPT_ARTIFACT,
                    subject_id="art-1",
                    detail="截图 MIME 不是 image/png",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(repository=store, now=_now)
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.index.evidence_gap[0].code is EvidenceGapCode.CORRUPT_ARTIFACT
    assert store.retry_calls == 0
    assert store.fail_pending_calls == 0


@pytest.mark.asyncio
async def test_gate_seals_insufficient_for_cross_task_binding_without_retry() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.CROSS_TASK_BINDING,
                    subject_id="art-1",
                    detail="制品 task_id 与 Capture 不一致",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(repository=store, now=_now)
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.index.evidence_gap[0].code is EvidenceGapCode.CROSS_TASK_BINDING
    assert store.retry_calls == 0


def test_merge_close_readiness_keeps_complete_when_ready() -> None:
    inventory = EvidenceInventory(complete=True, capture_ids=("capture-1",))
    merged = merge_close_readiness_gaps(
        inventory,
        readiness=SessionCloseReadiness(
            session_id="app:task-1",
            ready=True,
            pending_actions=0,
            pending_capture_commits=0,
            active_evidence_requests=0,
            running_restorations=0,
            nonterminal_active_work_items=0,
            pinned_surfaces=0,
        ),
        session_id="app:task-1",
    )
    assert merged.complete is True


@pytest.mark.parametrize(
    "readiness_overrides",
    (
        {"nonterminal_active_work_items": 1},
        {"nonterminal_app_frontier_items": 1},
    ),
)
def test_merge_close_readiness_explains_nonterminal_app_work(
    readiness_overrides: dict[str, int],
) -> None:
    inventory = EvidenceInventory(complete=True, capture_ids=("capture-1",))
    counts = {
        "pending_actions": 0,
        "pending_capture_commits": 0,
        "active_evidence_requests": 0,
        "running_restorations": 0,
        "nonterminal_active_work_items": 0,
        "nonterminal_app_frontier_items": 0,
        "pinned_surfaces": 0,
        "pending_manifests": 0,
        "building_evidence_indexes": 0,
    }
    counts.update(readiness_overrides)

    merged = merge_close_readiness_gaps(
        inventory,
        readiness=SessionCloseReadiness(
            session_id="app:task-1",
            ready=False,
            **counts,
        ),
        session_id="app:task-1",
    )

    assert merged.complete is False
    assert [item.code for item in merged.gaps] == [EvidenceGapCode.IN_FLIGHT_COMMAND]


@pytest.mark.parametrize(
    "readiness_overrides",
    (
        {"pending_manifests": 1},
        {"building_evidence_indexes": 1},
    ),
)
def test_merge_close_readiness_explains_pending_evidence_commit(
    readiness_overrides: dict[str, int],
) -> None:
    inventory = EvidenceInventory(complete=True, capture_ids=("capture-1",))
    counts = {
        "pending_actions": 0,
        "pending_capture_commits": 0,
        "active_evidence_requests": 0,
        "running_restorations": 0,
        "nonterminal_active_work_items": 0,
        "nonterminal_app_frontier_items": 0,
        "pinned_surfaces": 0,
        "pending_manifests": 0,
        "building_evidence_indexes": 0,
    }
    counts.update(readiness_overrides)

    merged = merge_close_readiness_gaps(
        inventory,
        readiness=SessionCloseReadiness(
            session_id="app:task-1",
            ready=False,
            **counts,
        ),
        session_id="app:task-1",
    )

    assert merged.complete is False
    assert [item.code for item in merged.gaps] == [EvidenceGapCode.PENDING_OBJECT_WRITE]


def test_corrupt_artifact_reason_rejects_bad_checksum_and_mime() -> None:
    from services.api.app.infrastructure.db.evidence_index_repository import (
        _corrupt_artifact_reason,
    )

    assert (
        _corrupt_artifact_reason(
            {
                "checksum": "",
                "size_bytes": 12,
                "artifact_type": "screenshot",
                "content_type": "image/png",
            }
        )
        is not None
    )
    assert (
        _corrupt_artifact_reason(
            {
                "checksum": "a" * 64,
                "size_bytes": 0,
                "artifact_type": "screenshot",
                "content_type": "image/png",
            }
        )
        is not None
    )
    assert (
        _corrupt_artifact_reason(
            {
                "checksum": "a" * 64,
                "size_bytes": 12,
                "artifact_type": "screenshot",
                "content_type": "image/jpeg",
            }
        )
        is not None
    )
    assert (
        _corrupt_artifact_reason(
            {
                "checksum": f"sha256:{'b' * 64}",
                "size_bytes": 12,
                "artifact_type": "screenshot",
                "content_type": "image/png",
            }
        )
        is None
    )
    assert (
        _corrupt_artifact_reason(
            {
                "checksum": "c" * 64,
                "size_bytes": 8,
                "artifact_type": "ui_tree",
                "content_type": "application/xml",
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_gate_seals_insufficient_for_missing_before_without_retry() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.MISSING_BEFORE_CAPTURE,
                    subject_id="action-1",
                    detail="已执行动作缺少 before_action Capture",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(repository=store, now=_now)
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert any(item.code is EvidenceGapCode.MISSING_BEFORE_CAPTURE for item in decision.gaps)
    assert any(item.role == "evidence_insufficient" for item in decision.index.capture_lineage)
    assert store.retry_calls == 0


@pytest.mark.asyncio
async def test_gate_seals_insufficient_for_missing_screenshot_without_retry() -> None:
    store = MemoryEvidenceIndexRepository(
        inventory=EvidenceInventory(
            complete=False,
            capture_ids=("capture-1",),
            gaps=(
                EvidenceGapItem(
                    code=EvidenceGapCode.MISSING_SCREENSHOT,
                    subject_id="capture-1",
                    detail="已提交 Capture 缺少必需截图",
                ),
            ),
        )
    )
    gate = AppExplorationPersistenceGate(repository=store, now=_now)
    decision = await gate.cutoff(task_id="task-1", session_id="app:task-1")
    assert decision.index is not None
    assert decision.index.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
    assert decision.index.evidence_gap[0].code is EvidenceGapCode.MISSING_SCREENSHOT
    assert store.retry_calls == 0
