"""Web V2 Worker 路由必须避开 H5/App/Main Agent。"""

import inspect
from types import SimpleNamespace

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.models import RunResult, TerminationReason
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.app_workflow_router import WorkflowRoute
from services.api.app.core.config import Settings, WorkerSettings
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import (
    AgentRunCancellationConflict,
    InMemoryAgentRunStateStore,
)


def test_worker_settings_support_dedicated_web_explorer_role() -> None:
    settings = WorkerSettings(
        roles=frozenset({"web_explorer"}),
        web_concurrency=1,
    )

    assert settings.roles == frozenset({"web_explorer"})
    assert settings.web_concurrency == 1


def test_web_only_worker_starts_reliable_submission_projection() -> None:
    source = inspect.getsource(worker_module._run_web_only_worker)

    assert "_build_task_submission_consumer" in source
    assert "_task_submission_reconciliation_loop" in source
    assert "TaskSubmissionProjectionService" in source
    assert "web_queue=web_queue" in source


def test_worker_loop_treats_web_active_session_pending_as_expected_retry() -> None:
    source = inspect.getsource(worker_module._worker_loop)

    assert "except WebActiveSessionWorkPendingError" in source
    assert "Web active-session 正式工作尚未排空" in source
    assert "等待后台收口重试" in source


def test_publication_consumer_treats_web_active_session_pending_as_expected_retry() -> None:
    source = inspect.getsource(worker_module._v2_inspection_consumer_loop)

    assert "except WebActiveSessionWorkPendingError" in source
    assert "active-session Work 尚未就绪" in source
    assert "等待 capture.committed 补发" in source


@pytest.mark.asyncio
async def test_web_full_inspection_worker_loop_forwards_real_reconciler_only(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def _capture_loop(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(worker_module, "_worker_loop", _capture_loop)
    reconciliation = SimpleNamespace(name="web-reconciliation")
    finalization = SimpleNamespace()
    report_generator = SimpleNamespace()

    tasks = worker_module._start_web_worker_loops(
        concurrency=1,
        queue=SimpleNamespace(),
        settings=Settings(),
        stop_event=SimpleNamespace(),
        state_store=SimpleNamespace(),
        worker_instance_id="web-worker-1",
        web_workflow_handler=SimpleNamespace(),
        web_inspection_reconciliation=reconciliation,
        task_finalization_service=finalization,
        final_report_generator=report_generator,
    )
    await tasks[0]

    assert calls[0]["web_inspection_reconciliation"] is reconciliation
    assert calls[0]["task_finalization_service"] is finalization
    assert calls[0]["final_report_generator"] is report_generator
    assert calls[0].get("h5_evidence_handoff") is None


class _SequencedWebDrainConnection:
    def __init__(self, remaining_counts: list[int]) -> None:
        self._remaining_counts = remaining_counts

    async def fetchval(self, *_args, **_kwargs) -> int:
        if len(self._remaining_counts) > 1:
            return self._remaining_counts.pop(0)
        return self._remaining_counts[0]


class _SequencedWebDrainPool:
    def __init__(self, remaining_counts: list[int]) -> None:
        self.connection = _SequencedWebDrainConnection(remaining_counts)

    def acquire(self):
        connection = self.connection

        class _Acquire:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return None

        return _Acquire()


@pytest.mark.asyncio
async def test_web_active_session_drain_waits_until_persisted_work_is_terminal(
    monkeypatch,
) -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pending(self, **kwargs):
            assert kwargs == {
                "task_id": "task-web-drain",
                "agent_run_id": "run-web-drain",
            }
            events.append("publish")
            return 1

    class Consumer:
        async def drain_available(self, **kwargs):
            assert kwargs == {
                "task_id": "task-web-drain",
                "consumer_agent_run_id": "run-web-drain",
                "session_dependency": "active_session",
                "allow_terminal_web_owner_recovery": True,
            }
            events.append("drain")

    pool = _SequencedWebDrainPool([2, 1, 0])

    async def _pool(_database_url):
        return pool

    monkeypatch.setattr(worker_module, "get_postgres_pool", _pool)
    drainer = worker_module._WebActiveSessionInspectionDrainer(
        publisher=Publisher(),
        consumer=Consumer(),
        max_drain_rounds=3,
    )

    await drainer.drain_before_close(
        task_id="task-web-drain",
        agent_run_id="run-web-drain",
        finalization_epoch=4,
    )

    assert events == ["publish", "drain"] * 3


@pytest.mark.asyncio
async def test_web_active_session_drain_exhaustion_fails_for_retry(
    monkeypatch,
) -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pending(self, **_kwargs):
            events.append("publish")
            return 0

    class Consumer:
        async def drain_available(self, **_kwargs):
            events.append("drain")

    pool = _SequencedWebDrainPool([1, 1])

    async def _pool(_database_url):
        return pool

    monkeypatch.setattr(worker_module, "get_postgres_pool", _pool)
    drainer = worker_module._WebActiveSessionInspectionDrainer(
        publisher=Publisher(),
        consumer=Consumer(),
        max_drain_rounds=2,
    )

    with pytest.raises(
        worker_module.WebActiveSessionWorkPendingError,
        match="active-session 正式工作尚未排空",
    ):
        await drainer.drain_before_close(
            task_id="task-web-drain",
            agent_run_id="run-web-drain",
            finalization_epoch=4,
        )

    assert events == ["publish", "drain"] * 2


class _PersistedWebRecovery:
    def __init__(self, checkpoint: str) -> None:
        self.task_id = "task-web-recovery"
        self.agent_run_id = "run-web-recovery"
        self.finalization_epoch = 4
        self.checkpoint = checkpoint
        self.state = (
            worker_module.TaskFinalizationState.SEALED
            if checkpoint == "report"
            else worker_module.TaskFinalizationState.SESSION_DRAINING
        )
        self.rule_execution_keys = {"real:home:alioth_web_fail"}
        if checkpoint in {"rules", "report"}:
            self.rule_execution_keys.update(self._gap_keys())
        self.report_generation_count = int(checkpoint == "report")
        self.report_binding_count = 0
        self.report_bound = False
        self.events: list[str] = []
        self.record = worker_module.AgentRunRecord(
            run_id=self.agent_run_id,
            task_id=self.task_id,
            message="web task",
            submitted_at=worker_module.datetime.now(worker_module.UTC),
            status=worker_module.AgentRunQueueStatus.COMPLETED,
            finished_at=worker_module.datetime.now(worker_module.UTC),
            task_config={
                "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
                "execution_mode": "full_inspection",
                "web_inspection_notification_mode": "enforced",
                "_task_target": {"platform": "web"},
            },
            final_report=(
                {
                    "report_stage": "final",
                    "is_final": True,
                    "report_id": "report-web-1",
                    "report_binding_version": 2,
                }
                if checkpoint == "report"
                else None
            ),
        )

    @staticmethod
    def _gap_keys() -> set[str]:
        return {
            "web-coverage-gap:web-snapshot-1:content_detail:alioth_6",
            "web-coverage-gap:web-snapshot-1:video_detail:alioth_7",
        }

    async def list_recoverable_finalizations(self, *, limit: int):
        assert limit == 100
        if self.report_bound:
            return []
        return [SimpleNamespace(task_id=self.task_id, agent_run_id=self.agent_run_id)]

    async def claim_recoverable_finalizations(self, *, limit: int, **_kwargs):
        snapshots = await self.list_recoverable_finalizations(limit=limit)
        return [
            SimpleNamespace(
                task_id=item.task_id,
                agent_run_id=item.agent_run_id,
                snapshot=item,
                lease_token="00000000-0000-0000-0000-000000000099",
                recovery_generation=0,
                expected_status="running",
            )
            for item in snapshots
        ]

    async def defer_finalization_retry(self, **_kwargs):
        return None

    async def get(self, *, task_id: str):
        assert task_id == self.task_id
        return SimpleNamespace(
            task_id=task_id,
            agent_run_id=self.agent_run_id,
            finalization_epoch=self.finalization_epoch,
            state=self.state,
        )

    async def list_finished_records(self, **_kwargs):
        return [self.record]

    async def record_run_finished(self, record):
        assert record is self.record

    async def reconcile(self, command):
        assert (
            command.task_id,
            command.agent_run_id,
            command.finalization_epoch,
        ) == (self.task_id, self.agent_run_id, self.finalization_epoch)
        self.events.append("reconcile")
        self.rule_execution_keys.update(self._gap_keys())
        return SimpleNamespace(status="gate_ready")

    async def list_records(self):
        return []

    async def save(self, _record):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["publication", "snapshot", "rules", "report"])
async def test_web_recovery_uses_real_worker_path_without_duplicates(
    checkpoint: str,
    monkeypatch,
) -> None:
    persisted = _PersistedWebRecovery(checkpoint)

    async def _generate(record, _generator, **_kwargs):
        persisted.events.append("generate")
        persisted.report_generation_count += 1
        record.final_report = {"report_stage": "final", "is_final": True}

    async def _gate(record, service, lease):
        assert record is persisted.record
        assert service is persisted
        assert lease is not None
        persisted.events.append("gate")
        if checkpoint != "report":
            # 真实 Orchestrator 的非 sealed 路径会在 advance 内原子完成
            # Completion Gate、报告绑定与封存，Worker 不得再次显式绑定。
            persisted.state = worker_module.TaskFinalizationState.SEALED
            persisted.report_binding_count += 1
            persisted.report_bound = True
        return worker_module._WorkerFinalizationDecision(
            gate_passed=True,
            gate_snapshot_id="gate-web-1",
            finalization_epoch=persisted.finalization_epoch,
            block_reasons=(),
        )

    async def _bind(record, service, lease):
        assert record is persisted.record
        assert service is persisted
        assert lease is not None
        assert checkpoint == "report"
        persisted.events.append("bind")
        persisted.report_binding_count += 1
        persisted.report_bound = True
        persisted.state = worker_module.TaskFinalizationState.SEALED

    monkeypatch.setattr(worker_module, "_generate_final_report", _generate)
    monkeypatch.setattr(worker_module, "_advance_task_finalization", _gate)
    monkeypatch.setattr(worker_module, "_bind_final_report", _bind)

    for _attempt in range(2):
        await worker_module._retry_pending_task_finalizations(
            persisted,
            persisted,
            persisted,
            final_report_generator=SimpleNamespace(),
            web_inspection_reconciliation=persisted,
        )

    expected = (
        ["gate", "bind"]
        if checkpoint == "report"
        else ["reconcile", "generate", "gate"]
    )
    assert persisted.events == expected
    assert len(persisted.rule_execution_keys) == 3
    assert persisted.report_generation_count == 1
    assert persisted.report_binding_count == 1
    assert persisted.report_bound is True
    assert persisted.state is worker_module.TaskFinalizationState.SEALED


@pytest.mark.asyncio
async def test_web_recovery_failed_gate_discards_draft_and_never_binds(
    monkeypatch,
) -> None:
    persisted = _PersistedWebRecovery("rules")

    async def _generate(record, _generator, **_kwargs):
        persisted.events.append("generate")
        record.final_report = {"report_stage": "final", "is_final": True}

    async def _blocked_gate(_record, _service, lease):
        assert lease is not None
        persisted.events.append("gate")
        return worker_module._WorkerFinalizationDecision(
            gate_passed=False,
            gate_snapshot_id="gate-web-blocked",
            finalization_epoch=persisted.finalization_epoch,
            block_reasons=("rule_executions_terminal",),
        )

    async def _unexpected_bind(*_args, **_kwargs):
        raise AssertionError("Gate 未通过不得绑定 Web 正式报告")

    monkeypatch.setattr(worker_module, "_generate_final_report", _generate)
    monkeypatch.setattr(worker_module, "_advance_task_finalization", _blocked_gate)
    monkeypatch.setattr(worker_module, "_bind_final_report", _unexpected_bind)

    for _attempt in range(2):
        await worker_module._retry_pending_task_finalizations(
            persisted,
            persisted,
            persisted,
            final_report_generator=SimpleNamespace(),
            web_inspection_reconciliation=persisted,
        )
        assert persisted.record.final_report is None

    assert persisted.events == [
        "reconcile",
        "generate",
        "gate",
        "reconcile",
        "generate",
        "gate",
    ]
    assert persisted.report_binding_count == 0
    assert persisted.report_bound is False


def _completed_web_formal_record(*, run_id: str = "run-web-status"):
    return worker_module.AgentRunRecord(
        run_id=run_id,
        task_id="task-web-status",
        message="web formal status",
        submitted_at=worker_module.datetime.now(worker_module.UTC),
        status=worker_module.AgentRunQueueStatus.COMPLETED,
        finished_at=worker_module.datetime.now(worker_module.UTC),
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {"platform": "web"},
        },
    )


@pytest.mark.asyncio
async def test_web_full_cancel_after_dispatch_skips_handoff_gate_and_report(
    monkeypatch,
) -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-full-cancel-after-dispatch",
    )
    record = await queue.enqueue(
        "web full cancel",
        task_id="task-web-cancel",
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {"platform": "web"},
        },
    )
    state_store = InMemoryAgentRunStateStore()
    events: list[str] = []

    async def _skip_cleanup(**_kwargs):
        events.append("cancel_cleanup")
        return 0

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", _skip_cleanup)
    monkeypatch.setattr(worker_module, "build_task_progress_recorder", lambda **_kwargs: None)

    class Dispatcher:
        async def dispatch_web_v2(self, **_kwargs):
            await state_store.record_cancel_requested(record.run_id)
            events.append("dispatch_returned")
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(),
                trace_sink=InMemoryActionTraceSink(),
            )

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"取消后不得执行正式副作用：{name}")

    class FormalSessionCloser:
        async def close(
            self,
            *,
            task_id: str,
            session_id: str,
            cancel_active_commands: bool,
        ) -> None:
            assert (task_id, session_id) == (
                "task-web-cancel",
                f"web:{record.run_id}",
            )
            assert cancel_active_commands is True
            events.append("close_formal_session")

    class FormalLedgerFinalizer:
        async def terminalize(self, **kwargs: object) -> None:
            assert kwargs == {
                "task_id": "task-web-cancel",
                "agent_run_id": record.run_id,
                "outcome": "canceled",
                "reason_code": "WORKFLOW_CANCELED",
                "reason_message": "Web 正式任务已取消，收束未完成的持久账本",
            }
            events.append("terminalize_formal_ledgers")

    await worker_module._process_web_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        final_report_generator=Forbidden(),
        task_finalization_service=Forbidden(),
        web_inspection_reconciliation=Forbidden(),
        web_formal_session_closer=FormalSessionCloser(),
        web_formal_terminal_ledger_finalizer=FormalLedgerFinalizer(),
    )

    persisted = await state_store.get_finished_record(run_id=record.run_id)
    assert persisted is not None
    assert persisted.status is worker_module.AgentRunQueueStatus.CANCELED
    assert persisted.final_report is None
    assert events == [
        "dispatch_returned",
        "cancel_cleanup",
        "terminalize_formal_ledgers",
        "close_formal_session",
    ]


@pytest.mark.asyncio
async def test_web_run_finish_cancellation_conflict_uses_atomic_cancel_path(
    monkeypatch,
) -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-full-cancel-conflict",
    )
    record = _completed_web_formal_record(run_id="run-web-cancel-conflict")
    events: list[str] = []

    class StateStore(InMemoryAgentRunStateStore):
        async def record_run_finished(self, _record):
            raise AgentRunCancellationConflict("cancel won")

    class Finalization:
        async def get(self, **_kwargs):
            return SimpleNamespace(agent_run_id=record.run_id, finalization_epoch=4)

    class Reconciliation:
        async def reconcile(self, _command):
            events.append("reconcile")
            raise AssertionError("取消竞争获胜后不得进入 reconciliation")

    class FormalSessionCloser:
        async def close(
            self,
            *,
            task_id: str,
            session_id: str,
            cancel_active_commands: bool,
        ) -> None:
            assert (task_id, session_id) == (
                "task-web-status",
                "web:run-web-cancel-conflict",
            )
            assert cancel_active_commands is True
            events.append("close_formal_session")

    class FormalLedgerFinalizer:
        async def terminalize(self, **_kwargs: object) -> None:
            events.append("terminalize_formal_ledgers")

    async def _skip_cleanup(**_kwargs):
        events.append("cancel_cleanup")
        return 0

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", _skip_cleanup)
    state_store = StateStore()

    await worker_module._finish_web_formal_inspection_run(
        queue=queue,
        record=record,
        settings=Settings(),
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=SimpleNamespace(),
        task_finalization_service=Finalization(),
        reconciliation=Reconciliation(),
        worker_instance_id="web-worker-1",
        web_formal_session_closer=FormalSessionCloser(),
        web_formal_terminal_ledger_finalizer=FormalLedgerFinalizer(),
    )

    persisted = await state_store.get_finished_record(run_id=record.run_id)
    assert persisted is not None
    assert persisted.status is worker_module.AgentRunQueueStatus.CANCELED
    assert persisted.final_report is None
    assert events == [
        "cancel_cleanup",
        "terminalize_formal_ledgers",
        "close_formal_session",
    ]


@pytest.mark.asyncio
async def test_canceled_web_full_recovery_closes_durable_session_before_terminal() -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-full-cancel-recovery",
    )
    record = _completed_web_formal_record(run_id="run-web-cancel-recovery")
    record.status = worker_module.AgentRunQueueStatus.CANCELED
    record.cancellation_cleanup_completed = True
    record.cancellation_finalized = False
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()
    events: list[str] = []

    class FormalSessionCloser:
        async def close(
            self,
            *,
            task_id: str,
            session_id: str,
            cancel_active_commands: bool,
        ) -> None:
            assert (task_id, session_id) == (
                "task-web-status",
                "web:run-web-cancel-recovery",
            )
            assert cancel_active_commands is True
            events.append("close_formal_session")

    class FormalLedgerFinalizer:
        async def terminalize(self, **_kwargs: object) -> None:
            events.append("terminalize_formal_ledgers")

    await worker_module._reconcile_canceled_run(
        queue,
        state_store,
        record,
        web_formal_session_closer=FormalSessionCloser(),
        web_formal_terminal_ledger_finalizer=FormalLedgerFinalizer(),
    )

    persisted = await state_store.get_finished_record(run_id=record.run_id)
    assert persisted is not None
    assert persisted.status is worker_module.AgentRunQueueStatus.CANCELED
    assert persisted.cancellation_finalized is True
    assert events == ["terminalize_formal_ledgers", "close_formal_session"]


def _install_web_finalization_capture(monkeypatch):
    captured = []

    async def _generate(record, _generator, *, finalized=True, **_kwargs):
        record.final_report = {
            "report_stage": "final" if finalized else "preview",
            "is_final": finalized,
            "summary": {
                "overall_status": "unknown",
                "evidence_insufficient": 2,
                "coverage_gap_rules": 2,
            },
        }

    class Orchestrator:
        def __init__(self, **_kwargs):
            pass

        async def advance_task_finalization(self, command):
            captured.append(command)
            return SimpleNamespace(
                gate_passed=True,
                gate_snapshot_id="gate-web-status",
                finalization_epoch=5,
                block_reasons=(),
            )

    async def _skip_usage(_record):
        return None

    monkeypatch.setattr(worker_module, "_generate_final_report", _generate)
    monkeypatch.setattr(worker_module, "QcOrchestrator", Orchestrator)
    monkeypatch.setattr(
        worker_module,
        "_reconcile_run_llm_usage_from_events",
        _skip_usage,
    )
    return captured


@pytest.mark.asyncio
async def test_initial_web_completed_with_gaps_reaches_atomic_finalization(
    monkeypatch,
) -> None:
    record = _completed_web_formal_record(run_id="run-web-initial-gaps")
    captured = _install_web_finalization_capture(monkeypatch)

    class StateStore:
        async def record_run_finished(self, _record):
            return None

    class Queue:
        async def save(self, _record):
            return None

    class Finalization:
        async def get(self, *, task_id):
            assert task_id == record.task_id
            return SimpleNamespace(
                agent_run_id=record.run_id,
                finalization_epoch=4,
            )

    class Reconciliation:
        async def reconcile(self, command):
            assert command.agent_run_id == record.run_id
            return SimpleNamespace(
                status="gate_ready",
                execution_status="completed_with_gaps",
            )

    await worker_module._finish_web_formal_inspection_run(
        queue=Queue(),
        record=record,
        settings=Settings(),
        state_store=StateStore(),
        progress_recorder=None,
        final_report_generator=SimpleNamespace(),
        task_finalization_service=Finalization(),
        reconciliation=Reconciliation(),
        worker_instance_id="web-worker-status",
    )

    assert len(captured) == 1
    assert (
        captured[0].execution_status
        is worker_module.TaskExecutionStatus.COMPLETED_WITH_GAPS
    )


@pytest.mark.asyncio
async def test_recovered_web_completed_with_gaps_reaches_atomic_finalization(
    monkeypatch,
) -> None:
    record = _completed_web_formal_record(run_id="run-web-recovered-gaps")
    captured = _install_web_finalization_capture(monkeypatch)

    class PersistedFinalization:
        async def list_recoverable_finalizations(self, *, limit):
            assert limit == 100
            return [SimpleNamespace(task_id=record.task_id, agent_run_id=record.run_id)]

        async def claim_recoverable_finalizations(self, *, limit, **_kwargs):
            items = await self.list_recoverable_finalizations(limit=limit)
            return [
                SimpleNamespace(
                    task_id=item.task_id,
                    agent_run_id=item.agent_run_id,
                    snapshot=item,
                    lease_token="00000000-0000-0000-0000-000000000099",
                    recovery_generation=0,
                    expected_status="running",
                )
                for item in items
            ]

        async def defer_finalization_retry(self, **_kwargs):
            return None

        async def get(self, *, task_id):
            assert task_id == record.task_id
            return SimpleNamespace(
                state=worker_module.TaskFinalizationState.SESSION_DRAINING,
                finalization_epoch=4,
            )

    class StateStore:
        async def list_finished_records(self, **_kwargs):
            return [record]

    class Queue:
        async def list_records(self):
            return []

        async def save(self, _record):
            return None

    class Reconciliation:
        async def reconcile(self, command):
            assert command.agent_run_id == record.run_id
            return SimpleNamespace(
                status="gate_ready",
                execution_status="completed_with_gaps",
            )

    retried = await worker_module._retry_pending_task_finalizations(
        Queue(),
        StateStore(),
        PersistedFinalization(),
        final_report_generator=SimpleNamespace(),
        web_inspection_reconciliation=Reconciliation(),
    )

    assert retried == 1
    assert len(captured) == 1
    assert (
        captured[0].execution_status
        is worker_module.TaskExecutionStatus.COMPLETED_WITH_GAPS
    )


@pytest.mark.parametrize(
    ("queue_status", "execution_outcome", "overall_status", "expected"),
    [
        (
            worker_module.AgentRunQueueStatus.COMPLETED,
            None,
            "pass",
            worker_module.TaskExecutionStatus.COMPLETED,
        ),
        (
            worker_module.AgentRunQueueStatus.FAILED,
            "failed",
            "error",
            worker_module.TaskExecutionStatus.FAILED,
        ),
        (
            worker_module.AgentRunQueueStatus.FAILED,
            "needs_review",
            "manual_review",
            worker_module.TaskExecutionStatus.NEEDS_REVIEW,
        ),
    ],
)
def test_non_gap_worker_execution_status_mapping_is_unchanged(
    queue_status,
    execution_outcome,
    overall_status,
    expected,
) -> None:
    record = _completed_web_formal_record()
    record.status = queue_status
    record.execution_outcome = execution_outcome
    record.final_report = {"summary": {"overall_status": overall_status}}

    assert worker_module._task_execution_status(record) is expected


@pytest.mark.asyncio
async def test_web_reconciliation_failure_persists_terminal_run_for_recovery(
    monkeypatch,
) -> None:
    record = worker_module.AgentRunRecord(
        run_id="run-web-retry",
        task_id="task-web-retry",
        message="web task",
        submitted_at=worker_module.datetime.now(worker_module.UTC),
        status=worker_module.AgentRunQueueStatus.COMPLETED,
        finished_at=worker_module.datetime.now(worker_module.UTC),
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {"platform": "web"},
        },
    )
    events: list[object] = []

    async def _skip_usage(_record):
        return None

    monkeypatch.setattr(
        worker_module,
        "_reconcile_run_llm_usage_from_events",
        _skip_usage,
    )

    class DurableStateStore:
        def __init__(self) -> None:
            self.finished: list[worker_module.AgentRunRecord] = []

        async def record_run_finished(self, saved_record):
            events.append("run_terminal")
            self.finished.append(saved_record)

        async def list_finished_records(self, **_kwargs):
            return list(self.finished)

    class Finalization:
        async def get(self, *, task_id):
            assert task_id == "task-web-retry"
            return SimpleNamespace(
                task_id=task_id,
                agent_run_id="run-web-retry",
                finalization_epoch=4,
                state=worker_module.TaskFinalizationState.SESSION_DRAINING,
            )

        async def list_recoverable_finalizations(self, *, limit):
            assert limit == 100
            return [
                SimpleNamespace(
                    task_id="task-web-retry",
                    agent_run_id="run-web-retry",
                )
            ]

        async def claim_recoverable_finalizations(self, *, limit, **_kwargs):
            items = await self.list_recoverable_finalizations(limit=limit)
            return [
                SimpleNamespace(
                    task_id=item.task_id,
                    agent_run_id=item.agent_run_id,
                    snapshot=item,
                    lease_token="00000000-0000-0000-0000-000000000099",
                    recovery_generation=0,
                    expected_status="running",
                )
                for item in items
            ]

        async def defer_finalization_retry(self, **_kwargs):
            return None

    class Reconciliation:
        def __init__(self) -> None:
            self.commands = []

        async def reconcile(self, command):
            self.commands.append(command)
            events.append(
                (
                    "reconcile",
                    command.task_id,
                    command.agent_run_id,
                    command.finalization_epoch,
                )
            )
            if len(self.commands) == 1:
                raise RuntimeError("active-session 正式工作尚未排空")
            return SimpleNamespace(status="waiting_for_formal_work")

    class Queue:
        async def list_records(self):
            return []

    state_store = DurableStateStore()
    finalization = Finalization()
    reconciliation = Reconciliation()
    queue = Queue()

    with pytest.raises(RuntimeError, match="active-session 正式工作尚未排空"):
        await worker_module._finish_web_formal_inspection_run(
            queue=queue,
            record=record,
            settings=Settings(),
            state_store=state_store,
            progress_recorder=None,
            final_report_generator=None,
            task_finalization_service=finalization,
            reconciliation=reconciliation,
            worker_instance_id="web-worker-1",
        )

    assert events[:2] == [
        "run_terminal",
        ("reconcile", "task-web-retry", "run-web-retry", 4),
    ]
    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        web_inspection_reconciliation=reconciliation,
    )

    assert retried == 0
    assert len(reconciliation.commands) == 2
    assert reconciliation.commands[-1].recovery_lease_token == (
        "00000000-0000-0000-0000-000000000099"
    )
    assert reconciliation.commands[-1].recovery_generation == 0
    assert record.final_report is None


def test_judge_consumer_registers_web_live_publication_adapter(monkeypatch) -> None:
    app_publisher = SimpleNamespace(name="app")
    active_session_publisher = SimpleNamespace(name="active-session")
    web_publisher = SimpleNamespace(name="web")
    monkeypatch.setattr(
        worker_module,
        "build_default_app_live_inspection_publish_service",
        lambda: app_publisher,
    )
    monkeypatch.setattr(
        worker_module,
        "build_default_h5_live_inspection_publish_service",
        lambda: active_session_publisher,
    )
    monkeypatch.setattr(
        worker_module,
        "build_default_web_live_inspection_publish_service",
        lambda **_kwargs: web_publisher,
    )
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings.model_validate({"workflow": {"web_v2_mode": "on"}})

    _consumer, dispatcher, _judge_dispatcher = worker_module._build_v2_inspection_consumer(
        redis_client,
        settings,
        worker_instance_id="judge-worker-1",
    )

    live_publisher = _judge_dispatcher._live_publisher
    assert live_publisher._publishers == (
        app_publisher,
        active_session_publisher,
        web_publisher,
    )


@pytest.mark.asyncio
async def test_judge_consumer_registers_one_active_session_publisher_when_h5_and_web_on(
    monkeypatch,
) -> None:
    class Publisher:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls = 0

        async def publish_pending(self, **_kwargs) -> int:
            self.calls += 1
            return 1

    app_publisher = Publisher("app")
    active_session_publisher = Publisher("active-session")
    web_publisher = Publisher("web")
    build_active_calls: list[str] = []
    monkeypatch.setattr(
        worker_module,
        "build_default_app_live_inspection_publish_service",
        lambda: app_publisher,
    )

    def _build_active_session_publisher():
        build_active_calls.append("build")
        return active_session_publisher

    monkeypatch.setattr(
        worker_module,
        "build_default_h5_live_inspection_publish_service",
        _build_active_session_publisher,
    )
    monkeypatch.setattr(
        worker_module,
        "build_default_web_live_inspection_publish_service",
        lambda **_kwargs: web_publisher,
    )
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings.model_validate(
        {"workflow": {"h5_v2_mode": "on", "web_v2_mode": "on"}}
    )

    _consumer, dispatcher, _judge_dispatcher = worker_module._build_v2_inspection_consumer(
        redis_client,
        settings,
        worker_instance_id="judge-worker-1",
    )

    live_publisher = _judge_dispatcher._live_publisher
    published = await live_publisher.publish_pending(
        task_id="task-web",
        agent_run_id="run-web",
    )
    assert build_active_calls == ["build"]
    assert live_publisher._publishers == (
        app_publisher,
        active_session_publisher,
        web_publisher,
    )
    assert published == 3
    assert active_session_publisher.calls == 1


@pytest.mark.asyncio
async def test_worker_routes_web_only_to_web_v2_handler(monkeypatch) -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-v2",
    )
    record = await queue.enqueue(
        "web task",
        task_id="task-web",
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "exploration_only",
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.test/dashboard",
                "app": None,
                "web": {
                    "browser_profile_id": "desktop_fhd_1920x1080_v1",
                    "adult_self_attestation": {
                        "declared": True,
                        "statement_version": "adult-v1",
                        "declared_by": "operator-1",
                        "declared_at": "2026-08-30T12:00:00Z",
                    },
                },
            },
        },
    )
    routed: list[WorkflowRoute] = []

    def _unexpected_main_agent(*_args, **_kwargs):
        raise AssertionError("Web Run 不得构造通用 Main Agent")

    async def _capture_web(*_args, dispatcher, **_kwargs):
        routed.append(dispatcher.route(record, Settings()))

    monkeypatch.setattr(worker_module, "build_default_main_agent", _unexpected_main_agent)
    monkeypatch.setattr(worker_module, "_process_web_v2_one", _capture_web)

    class WebHandler:
        async def run(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            raise AssertionError("测试在专用处理函数边界截获，不应执行 Handler")

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings.model_validate(
            {"workflow": {"web_v2_mode": "exploration_only"}}
        ),
        state_store=InMemoryAgentRunStateStore(),
        worker_instance_id="web-worker-1",
        web_workflow_handler=WebHandler(),
    )

    assert routed == [WorkflowRoute.WEB_V2]


@pytest.mark.asyncio
async def test_web_full_preflight_failure_uses_reconciliation_without_report(
    monkeypatch,
) -> None:
    """Runtime 尚未启动时失败，不得绕过 Web 对账生成缺 Coverage 报告。"""

    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-v2-preflight-failure",
    )
    record = await queue.enqueue(
        "web preflight failure",
        task_id="task-web-preflight",
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.test",
                "web": {
                    "browser_profile_id": "desktop_fhd_1920x1080_v1",
                    "adult_self_attestation": {
                        "declared": False,
                        "statement_version": "adult-v1",
                        "declared_by": "operator-1",
                        "declared_at": "2026-08-30T12:00:00Z",
                    },
                },
            },
        },
    )
    formal_calls: list[dict[str, object]] = []

    async def finish_web_formal(**kwargs) -> None:
        formal_calls.append(kwargs)

    async def forbidden_standard_finish(**_kwargs) -> None:
        raise AssertionError("Web full 前置失败不得进入通用报告生成")

    monkeypatch.setattr(
        worker_module,
        "_finish_web_formal_inspection_run",
        finish_web_formal,
    )
    monkeypatch.setattr(
        worker_module,
        "_finish_standard_terminal_run",
        forbidden_standard_finish,
    )
    reconciliation = object()
    finalization = object()
    generator = object()

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings.model_validate({"workflow": {"web_v2_mode": "on"}}),
        state_store=InMemoryAgentRunStateStore(),
        worker_instance_id="web-worker-preflight",
        web_workflow_handler=SimpleNamespace(formal_session_closer=object()),
        web_inspection_reconciliation=reconciliation,
        task_finalization_service=finalization,
        final_report_generator=generator,
    )

    assert len(formal_calls) == 1
    assert formal_calls[0]["reconciliation"] is reconciliation
    assert formal_calls[0]["task_finalization_service"] is finalization
    assert formal_calls[0]["final_report_generator"] is generator
    assert formal_calls[0]["record"].status is worker_module.AgentRunQueueStatus.FAILED


def test_apply_result_persists_web_exploration_summary() -> None:
    record = worker_module.AgentRunRecord(
        run_id="run-web-summary",
        task_id="task-web",
        message="web task",
        submitted_at=worker_module.datetime.now(worker_module.UTC),
    )
    result = RunResult(
        termination_reason=TerminationReason.COMPLETED,
        working_memory=WorkingMemory(),
        trace_sink=InMemoryActionTraceSink(),
        web_exploration_summary={
            "site_id": "91porna_com",
            "menu_total": 65,
            "frontier_remaining": 0,
        },
    )

    worker_module._apply_result(record, result)

    assert record.web_exploration_summary == result.web_exploration_summary


@pytest.mark.asyncio
async def test_web_final_report_uses_run_menu_ledger_instead_of_h5_timeline() -> None:
    record = worker_module.AgentRunRecord(
        run_id="run-web-report",
        task_id="task-web-report",
        message="web task",
        submitted_at=worker_module.datetime.now(worker_module.UTC),
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "_task_target": {"platform": "web"},
        },
        execution_outcome="needs_review",
        web_exploration_summary={
            "site_id": "heiliao_com",
            "menu_total": 25,
            "frontier_remaining": 0,
            "menu_ledger": [{"entry_id": "home"}],
        },
    )

    class H5TimelineReader:
        async def page_timeline(self, task_id):
            raise AssertionError(f"Web 不得读取 H5 页面时间线：{task_id}")

    summary = await worker_module._page_exploration_for_report(
        record,
        reader=H5TimelineReader(),
    )

    assert summary["site_id"] == "heiliao_com"
    assert summary["menu_ledger"] == [{"entry_id": "home"}]
    assert summary["complete"] is True
    assert summary["requires_review"] is True


@pytest.mark.asyncio
async def test_web_worker_persists_final_menu_report_without_h5_app_reader() -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-v2-final-report",
    )
    record = await queue.enqueue(
        "web task",
        task_id="task-web-final-report",
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "exploration_only",
            "_task_target": {"platform": "web"},
        },
    )
    ledger = [{"entry_id": "heiliao.home", "status": "home_baseline"}]

    class Dispatcher:
        async def dispatch_web_v2(self, **_kwargs):
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(),
                trace_sink=InMemoryActionTraceSink(),
                web_exploration_summary={
                    "site_id": "heiliao_com",
                    "menu_total": 1,
                    "frontier_remaining": 0,
                    "menu_ledger": ledger,
                },
            )

    class FinalReportGenerator:
        async def generate_task_report(self, task_id, **kwargs):
            assert task_id == "task-web-final-report"
            assert kwargs["page_exploration"]["menu_ledger"] == ledger
            return {
                "summary": {"candidate_execution_complete": True},
                "page_exploration": kwargs["page_exploration"],
            }

    state_store = InMemoryAgentRunStateStore()
    await worker_module._process_web_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        final_report_generator=FinalReportGenerator(),
    )

    persisted = await state_store.get_finished_record(run_id=record.run_id)
    assert persisted is not None
    assert persisted.final_report["report_stage"] == "final"
    assert persisted.final_report["is_final"] is True
    assert persisted.final_report["page_exploration"]["menu_ledger"] == ledger


@pytest.mark.asyncio
async def test_web_dispatch_failure_still_persists_final_failure_report() -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-v2-dispatch-failure",
    )
    record = await queue.enqueue(
        "web task",
        task_id="task-web-dispatch-failure",
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "exploration_only",
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.test/",
            },
        },
    )

    class Dispatcher:
        async def dispatch_web_v2(self, **_kwargs):
            raise RuntimeError("browser unavailable")

    class FinalReportGenerator:
        async def generate_task_report(self, _task_id, **kwargs):
            return {
                "summary": {"candidate_execution_complete": True},
                "page_exploration": kwargs["page_exploration"],
            }

    state_store = InMemoryAgentRunStateStore()
    await worker_module._process_web_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        final_report_generator=FinalReportGenerator(),
    )

    persisted = await state_store.get_finished_record(run_id=record.run_id)
    assert persisted is not None
    assert persisted.status is worker_module.AgentRunQueueStatus.FAILED
    assert persisted.final_report["report_stage"] == "final"
    assert persisted.final_report["page_exploration"]["complete"] is False
    assert (
        persisted.final_report["page_exploration"]["error_code"]
        == "WEB_EXPLORATION_SUMMARY_UNAVAILABLE"
    )


@pytest.mark.asyncio
async def test_web_report_generation_failure_cannot_skip_terminal_persistence() -> None:
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key="test:web-v2-report-failure",
    )
    record = await queue.enqueue(
        "web task",
        task_id="task-web-report-failure",
        task_config={
            "workflow_version": "v2",
                "web_runtime_version": "web-runtime.v2",
            "execution_mode": "exploration_only",
            "_task_target": {"platform": "web"},
        },
    )

    class Dispatcher:
        async def dispatch_web_v2(self, **_kwargs):
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(),
                trace_sink=InMemoryActionTraceSink(),
                web_exploration_summary={
                    "site_id": "heiliao_com",
                    "menu_total": 1,
                    "frontier_remaining": 0,
                    "menu_ledger": [],
                },
            )

    class BrokenFinalReportGenerator:
        async def generate_task_report(self, _task_id, **_kwargs):
            raise RuntimeError("report storage unavailable")

    state_store = InMemoryAgentRunStateStore()
    await worker_module._process_web_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        final_report_generator=BrokenFinalReportGenerator(),
    )

    persisted = await state_store.get_finished_record(run_id=record.run_id)
    assert persisted is not None
    assert persisted.status is worker_module.AgentRunQueueStatus.FAILED
    assert persisted.execution_outcome == "needs_review"
    assert persisted.final_report["report_stage"] == "final"
    assert persisted.final_report["is_final"] is True
    assert persisted.final_report["summary"]["overall_status"] == "error"
    assert (
        persisted.final_report["summary"]["error_code"]
        == "WEB_FINAL_REPORT_GENERATION_FAILED"
    )
