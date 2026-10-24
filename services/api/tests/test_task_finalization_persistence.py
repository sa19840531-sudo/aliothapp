"""Task 行锁、epoch 提升、门禁快照与报告绑定的 PostgreSQL 契约测试。"""

from __future__ import annotations

import inspect
import json
import runpy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.api.app.application.report_payloads import stable_report_payload_hash
from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    BindReportToGateCommand,
    EvaluateCompletionGateCommand,
    FinalizeTaskWithReportCommand,
    RecordReviewPolicyDecisionCommand,
    TerminateTaskFinalizationCommand,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task import (
    CompletionBlockReason,
    CompletionGateFacts,
    TaskExecutionStatus,
    TaskFinalizationState,
    TaskQualityOutcome,
    build_completion_gate_snapshot,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresCompletionGatePreparer,
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
    build_default_task_finalization_service,
)

TASK_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "00000000-0000-0000-0000-000000000002"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_default_finalization_service_injects_aggregate_gate_preparer() -> None:
    service = build_default_task_finalization_service(pool=object())

    assert isinstance(service.completion_gate_preparer, PostgresCompletionGatePreparer)


@pytest.mark.asyncio
async def test_completion_gate_preparer_closes_actions_before_aggregate_publication() -> None:
    events: list[str] = []

    class ActionExecutions:
        async def fail_nonterminal_for_finalization(self, **kwargs):
            events.append(f"actions:{kwargs['finalization_epoch']}")
            return 1

    class AggregatePublication:
        async def prepare(self, **kwargs):
            events.append(f"aggregate:{kwargs['finalization_epoch']}")
            return {"published": 2}

    preparer = PostgresCompletionGatePreparer(
        action_executions=ActionExecutions(),
        aggregate_publication=AggregatePublication(),
    )

    result = await preparer.prepare(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=4,
    )

    assert result == {"published": 2}
    assert events == ["actions:4", "aggregate:4"]


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        fetchrow_results: list[Any] | None = None,
        fetch_results: list[Any] | None = None,
        fetchval_results: list[Any] | None = None,
    ) -> None:
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_results = list(fetch_results or [])
        self.fetchval_results = list(fetchval_results or [])
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchrow", " ".join(query.split()), args))
        if "finalization_recovery_lease_token" in query and "FOR UPDATE" in query:
            return {"id": TASK_ID}
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.calls.append(("fetch", " ".join(query.split()), args))
        return self.fetch_results.pop(0) if self.fetch_results else []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return self.fetchval_results.pop(0) if self.fetchval_results else None

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "UPDATE 1"


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self._connection)


def _task_row(
    *,
    state: str,
    epoch: int,
    row_version: int,
    gate_snapshot_id: str | None = None,
    execution_status: str | None = None,
    quality_outcome: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "finalization_state": state,
        "finalization_epoch": epoch,
        "row_version": row_version,
        "finalization_agent_run_id": RUN_ID,
        "completion_gate_snapshot_id": gate_snapshot_id,
        "execution_status": execution_status,
        "quality_outcome": quality_outcome,
    }


def _facts_row(**overrides) -> dict[str, Any]:
    values = {
        "exploration_terminal": True,
        "pending_action_count": 0,
        "pending_capture_commit_count": 0,
        "nonterminal_enforced_work_item_count": 0,
        "active_session_request_count": 0,
        "candidate_rule_count": 2,
        "terminal_candidate_rule_count": 2,
        "background_execution_count": 0,
        "review_policy_satisfied": True,
        "open_review_count": 0,
        "review_generation": 3,
        "review_policy_decision_id": None,
        "evaluated_at": NOW,
    }
    values.update(overrides)
    return values


def _blocked_gate_update(connection: FakeConnection) -> tuple[str, str, tuple[Any, ...]]:
    """按结构找「门禁未通过、只挂快照」的任务更新，不绑定状态 CASE 表达式的写法。"""
    return next(
        call
        for call in connection.calls
        if call[0] == "fetchrow"
        and "UPDATE qc_task" in call[1]
        and "completion_gate_snapshot_id = $4" in call[1]
        and "finalization_state = 'session_draining'" in call[1]
        and "finalization_state = 'finalizing'" not in call[1]
    )


def _snapshot_id(*, epoch: int, facts_row: dict[str, Any]) -> str:
    facts = CompletionGateFacts(
        **{key: value for key, value in facts_row.items() if key != "evaluated_at"}
    )
    return build_completion_gate_snapshot(
        task_id=TASK_ID,
        finalization_epoch=epoch,
        evaluation_sequence=1,
        facts=facts,
        evaluated_at=NOW,
    ).gate_snapshot_id


async def test_draining_phases_lock_task_and_bulk_drain_sessions() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            _task_row(state="exploration_draining", epoch=0, row_version=1),
            _task_row(state="session_draining", epoch=0, row_version=2),
        ]
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    exploration = await repository.begin_exploration_draining(
        command=BeginExplorationDrainingCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_row_version=0,
        )
    )
    sessions = await repository.begin_session_draining(
        command=BeginSessionDrainingCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=0,
            expected_row_version=1,
        )
    )

    assert exploration.state is TaskFinalizationState.EXPLORATION_DRAINING
    assert sessions.state is TaskFinalizationState.SESSION_DRAINING
    queries = [call[1] for call in connection.calls]
    assert sum("pg_advisory_xact_lock" in query for query in queries) == 2
    assert any("UPDATE qc_surface_session" in query for query in queries)
    assert any("UPDATE qc_surface_ref" in query for query in queries)


@pytest.mark.parametrize(
    ("execution_status", "expected_task_status"),
    (
        (TaskExecutionStatus.NEEDS_REVIEW, "review"),
        (TaskExecutionStatus.FAILED, "failed"),
    ),
)
async def test_terminal_reconciliation_outcome_is_atomic_and_not_recoverable(
    execution_status: TaskExecutionStatus,
    expected_task_status: str,
) -> None:
    facts_row = _facts_row(review_policy_satisfied=False)
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 8,
            },
            facts_row,
            _task_row(
                state="sealed",
                epoch=4,
                row_version=9,
                gate_snapshot_id=gate_id,
                execution_status=execution_status.value,
                quality_outcome="unknown",
            )
        ],
        fetch_results=[[]],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    snapshot = await repository.terminate(
        command=TerminateTaskFinalizationCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=4,
            expected_row_version=8,
            execution_status=execution_status,
            reason_code="WEB_RECONCILIATION_TERMINAL",
        )
    )
    recoverable = await repository.list_recoverable_finalizations(limit=100)

    assert snapshot.execution_status is execution_status
    assert recoverable == []
    terminal_update = next(
        call
        for call in connection.calls
        if call[0] == "fetchrow" and "finalization_last_error = $8" in call[1]
    )
    assert terminal_update[2][4] == expected_task_status
    assert terminal_update[2][6] == execution_status.value
    recoverable_query = next(
        call[1]
        for call in connection.calls
        if call[0] == "fetch" and "list" not in call[1] and "qc_rule_execution" not in call[1]
    )
    assert "NOT IN ('needs_review', 'failed')" in recoverable_query


async def test_list_session_draining_reads_durable_gate_backlog() -> None:
    connection = FakeConnection(
        fetch_results=[
            [
                _task_row(
                    state="session_draining",
                    epoch=4,
                    row_version=9,
                )
            ]
        ]
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    snapshots = await repository.list_session_draining(limit=20)

    assert snapshots[0].task_id == TASK_ID
    assert snapshots[0].state is TaskFinalizationState.SESSION_DRAINING
    query = connection.calls[0][1]
    assert "finalization_state = 'session_draining'" in query
    assert "session_run.output_context->>'cancel_requested'" in query
    assert "ORDER BY session_draining_at NULLS FIRST" in query
    assert connection.calls[0][2] == (20,)


async def test_recoverable_scan_requires_bound_terminal_run_and_no_active_run() -> None:
    connection = FakeConnection(fetch_results=[[]])
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    await repository.list_recoverable_finalizations(limit=25)

    query = connection.calls[0][1]
    # running + 开放收口态，或 review/done 仍停在 session_draining，均可进入恢复扫描。
    assert "qc_task.status = 'running'" in query
    assert "qc_task.status IN ('review', 'done')" in query
    assert "finalization_state IN (" in query
    assert "'open', 'exploration_draining', 'session_draining'" in query
    assert "terminal_run.id = qc_task.finalization_agent_run_id" in query
    assert "terminal_run.task_id = qc_task.id" in query
    assert "owner_run.output_context->>'cancel_requested'" in query
    assert "active_run.status IN ('pending', 'running')" in query
    assert "qc_task.target_platform = 'web'" in query
    assert "active_run.agent_type = 'rule_judge'" in query
    assert "active_run.input_context->>'source'" in query
    assert "= 'h5_inspection_publication'" in query
    assert "active_run.input_context->>'finalization_epoch'" in query
    assert "= qc_task.finalization_epoch::text" in query
    assert "finalization_agent_run_id IS NOT NULL" in query
    assert "qc_report_payload_snapshot" in query
    # 到期或未调度的任务都可扫；不再要求 snapshot_id 为空。
    assert "finalization_retry_at IS NULL" in query
    assert "finalization_retry_at <= now()" in query
    assert "qc_task.status IN ('review', 'done')" in query
    assert "finalization_state = 'sealed'" in query
    assert "qc_report_gate_binding" in query
    assert "finalization_recovery_state = 'active'" in query
    assert "finalization_recovery_state = 'verified'" not in query
    assert "CASE finalization_state" in query
    assert "WHEN 'exploration_draining' THEN 0" in query
    assert "WHEN 'open' THEN 1" in query
    assert "WHEN 'session_draining' THEN 2" in query
    assert "WHEN 'sealed' THEN 3" in query
    assert "finalization_retry_at NULLS FIRST" in query
    assert connection.calls[0][2] == (25, 600.0)


async def test_defer_finalization_retry_rotates_limited_backlog() -> None:
    connection = FakeConnection()
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    await repository.defer_finalization_retry(
        task_id=TASK_ID,
        retry_after_seconds=30.0,
        error_message="temporary report failure",
    )

    method, query, args = connection.calls[0]
    assert method == "execute"
    assert "finalization_retry_at = now()" in query
    assert "finalization_retry_count = finalization_retry_count + 1" in query
    assert args == (TASK_ID, 30.0, "temporary report failure")


async def test_claim_recoverable_finalizations_is_atomic_fenced_and_web_scoped() -> None:
    connection = FakeConnection(fetch_results=[[]])
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    leases = await repository.claim_recoverable_finalizations(
        limit=25,
        lease_owner="worker-1",
        lease_seconds=30.0,
    )

    assert leases == []
    method, query, args = connection.calls[0]
    assert method == "fetch"
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "finalization_recovery_lease_token" in query
    assert "finalization_recovery_generation" in query
    assert "finalization_recovery_generation + 1" in query
    assert "finalization_row_version = finalization_row_version + 1" in query
    assert "finalization_recovery_lease_expires_at" in query
    assert "report_payload->>'report_id'" in query
    assert "completion_gate,gate_snapshot_id" in query
    assert "page_exploration,web_coverage,coverage_snapshot_id" in query
    assert "coverage.snapshot_hash" in query
    assert "target_platform = 'web'" in query
    assert "web_inspection_notification_mode" in query
    assert args[0:3] == (25, "worker-1", 30.0)


async def test_record_finalization_retry_error_quarantines_third_identical_failure() -> None:
    connection = FakeConnection()
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    await repository.record_finalization_retry_error(
        task_id=TASK_ID,
        error_message="RuntimeError: report binding failed",
        lease_token="00000000-0000-0000-0000-000000000099",
        recovery_generation=2,
        expected_status="running",
    )

    method, query, args = connection.calls[0]
    assert method == "execute"
    assert "finalization_last_error = $2" in query
    assert "finalization_retry_count" not in query
    assert "finalization_error_signature = $3::varchar(64)" in query
    assert "finalization_same_error_count + 1" in query
    assert "THEN 'quarantined'" in query
    assert "finalization_quarantined_at = CASE" in query
    assert "THEN now()" in query
    assert "status = CASE" in query
    assert "finalization_recovery_lease_expires_at > now()" in query
    assert "THEN 'review'" in query
    assert args[0:2] == (TASK_ID, "RuntimeError: report binding failed")
    assert len(args[2]) == 64
    assert args[3] == 3


async def test_record_finalization_retry_success_clears_error_streak() -> None:
    connection = FakeConnection()
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    await repository.record_finalization_retry_success(
        task_id=TASK_ID,
        lease_token="00000000-0000-0000-0000-000000000099",
        recovery_generation=2,
        expected_status="running",
    )

    method, query, args = connection.calls[0]
    assert method == "execute"
    assert "finalization_error_signature = NULL" in query
    assert "finalization_same_error_count = 0" in query
    assert "finalization_last_error = NULL" in query
    assert "finalization_recovery_state = 'active'" in query
    assert "finalization_recovery_lease_expires_at > now()" in query
    assert args == (
        TASK_ID,
        "00000000-0000-0000-0000-000000000099",
        2,
        "running",
    )


def test_finalization_retry_migration_adds_due_schedule() -> None:
    migration = runpy.run_path(
        str(
            Path(__file__).parents[1]
            / "migrations/versions/20260901_0083_finalization_retry_schedule.py"
        )
    )
    source = inspect.getsource(migration["upgrade"])

    assert "finalization_retry_at" in source
    assert "finalization_retry_count" in source
    assert "idx_qc_task_finalization_retry_due" in source


def test_0092_migration_adds_bounded_finalization_quarantine() -> None:
    migration = Path(__file__).parents[1] / (
        "migrations/versions/20260906_0092_web_report_applicability_recovery.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert "finalization_recovery_state" in source
    assert "finalization_error_signature" in source
    assert "finalization_same_error_count" in source
    assert "finalization_quarantined_at" in source
    assert "'active', 'quarantined'" in source


def test_0093_migration_adds_fenced_recovery_lease() -> None:
    migration = Path(__file__).parents[1] / (
        "migrations/versions/20260906_0093_finalization_recovery_lease.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert "finalization_recovery_lease_token" in source
    assert "finalization_recovery_lease_owner" in source
    assert "finalization_recovery_lease_expires_at" in source


def test_0094_migration_adds_verified_final_binding_state() -> None:
    migration = Path(__file__).parents[1] / (
        "migrations/versions/20260906_0094_verified_final_report_recovery.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert "'active', 'quarantined', 'verified'" in source
    assert "finalization_recovery_state IN ('active', 'verified')" in source
    assert "WHERE finalization_recovery_state = 'verified'" in source


def test_web_finalize_and_sealed_bind_use_database_derived_binding_validator() -> None:
    finalize_source = inspect.getsource(
        PostgresTaskFinalizationRepository.finalize_task_with_report
    )
    bind_source = inspect.getsource(PostgresTaskFinalizationRepository.bind_report)

    assert "validate_web_final_report_binding" in finalize_source
    assert "web_coverage_snapshot_id" in finalize_source
    assert "validate_web_final_report_binding" in bind_source
    assert "web_coverage_snapshot_hash" in bind_source
    assert "finalization_recovery_state = CASE" in finalize_source
    assert "WHEN $8::boolean THEN 'verified'" in finalize_source
    assert "is_web_formal and command.recovery_lease_token is None" in finalize_source


async def test_blocked_gate_persists_exact_snapshot_without_raising_epoch() -> None:
    facts_row = _facts_row(pending_action_count=1)
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.evaluate_and_seal(
        command=EvaluateCompletionGateCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
        )
    )

    assert decision.snapshot.gate_passed is False
    assert decision.snapshot.block_reasons == (CompletionBlockReason.PENDING_ACTION,)
    assert decision.finalization.state is TaskFinalizationState.SESSION_DRAINING
    assert decision.finalization.finalization_epoch == 4
    blocked_update = _blocked_gate_update(connection)
    assert "execution_status =" not in blocked_update[1]
    assert "quality_outcome =" not in blocked_update[1]
    # 阻塞门禁写 snapshot 后：技术阻断短间隔复查；review_ready 则 infinity。
    assert "ELSE now() + interval '30 seconds'" in blocked_update[1]
    assert "WHEN $5::boolean THEN 'infinity'::timestamptz" in blocked_update[1]
    assert blocked_update[2] == (TASK_ID, 4, 7, gate_id, False)
    assert not any("finalization_state = 'finalizing'" in call[1] for call in connection.calls)


async def test_blocked_gate_reuses_unchanged_snapshot_without_row_version_churn() -> None:
    facts_row = _facts_row(
        pending_action_count=1,
        evaluated_at=datetime(2026, 8, 3, 12, 5, tzinfo=UTC),
    )
    facts = CompletionGateFacts(
        **{key: value for key, value in facts_row.items() if key != "evaluated_at"}
    )
    existing = build_completion_gate_snapshot(
        task_id=TASK_ID,
        finalization_epoch=4,
        evaluation_sequence=3,
        facts=facts,
        evaluated_at=NOW,
    )
    connection = FakeConnection(
        fetchrow_results=[
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=11,
                gate_snapshot_id=existing.gate_snapshot_id,
            ),
            facts_row,
            {
                "gate_snapshot_id": existing.gate_snapshot_id,
                "evaluation_sequence": existing.evaluation_sequence,
                "protocol_version": existing.protocol_version,
                "facts_hash": existing.facts_hash,
                "gate_passed": False,
                "evaluated_at": existing.evaluated_at,
            },
        ]
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.evaluate_and_seal(
        command=EvaluateCompletionGateCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=4,
            expected_row_version=11,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
        )
    )

    assert decision.snapshot.gate_snapshot_id == existing.gate_snapshot_id
    assert decision.snapshot.evaluation_sequence == 3
    assert decision.finalization.row_version == 11
    assert not any(call[0] == "fetchval" for call in connection.calls)
    assert not any(
        "INSERT INTO qc_completion_gate_snapshot" in call[1]
        for call in connection.calls
    )
    retry_schedule_update = next(
        call
        for call in connection.calls
        if call[0] == "execute" and "UPDATE qc_task" in call[1]
    )
    assert "WHEN target_platform IN ('h5', 'app') THEN 'infinity'::timestamptz" in (
        retry_schedule_update[1]
    )
    assert "WHEN status = 'review' THEN 'infinity'::timestamptz" in (
        retry_schedule_update[1]
    )
    assert "ELSE now() + interval '30 seconds'" in retry_schedule_update[1]
    assert "finalization_row_version" not in retry_schedule_update[1]
    assert retry_schedule_update[2] == (TASK_ID, 4, existing.gate_snapshot_id, False)


async def test_gate_evaluation_reconciles_stale_row_version_in_same_session_epoch() -> None:
    """门禁重试只在状态、epoch、owner 和取消标记仍有效时接受当前版本。"""

    facts_row = _facts_row(pending_action_count=1)
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            None,
            _task_row(state="session_draining", epoch=4, row_version=8),
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=9,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.evaluate_and_seal(
        command=EvaluateCompletionGateCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.FAILED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
        )
    )

    assert decision.finalization.row_version == 9
    update_call = next(
        call
        for call in connection.calls
        if call[0] == "fetchrow" and "completion_gate_snapshot_id = $4" in call[1]
    )
    assert update_call[2][2] == 8
    fallback_query = next(
        call
        for call in connection.calls
        if call[0] == "fetchrow"
        and "finalization_agent_run_id = $3::uuid" in call[1]
        and "finalization_row_version = $3" not in call[1]
    )
    assert fallback_query[2] == (TASK_ID, 4, RUN_ID)
    assert "finalization_row_version = $3" not in fallback_query[1]
    assert "finalization_epoch = $2" in fallback_query[1]
    assert "finalization_agent_run_id = $3::uuid" in fallback_query[1]


async def test_gate_evaluation_stale_row_version_does_not_cross_epoch() -> None:
    connection = FakeConnection(fetchrow_results=[None, None])
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    with pytest.raises(
        TaskFinalizationPersistenceError,
        match="任务尚未进入 Session Draining，或 epoch/版本已变化",
    ):
        await repository.evaluate_and_seal(
            command=EvaluateCompletionGateCommand(
                task_id=TASK_ID,
                agent_run_id=RUN_ID,
                expected_finalization_epoch=4,
                expected_row_version=7,
                execution_status=TaskExecutionStatus.FAILED,
                quality_outcome=TaskQualityOutcome.UNKNOWN,
            )
        )

    identity_calls = [
        (query, args)
        for operation, query, args in connection.calls
        if operation == "fetchrow"
        and "finalization_state = 'session_draining'" in query
        and "FOR UPDATE" in query
    ]
    assert identity_calls
    assert all("finalization_epoch = $2" in query for query, _args in identity_calls)
    assert all(
        (
            "finalization_agent_run_id = $3::uuid" in query
            or "finalization_agent_run_id = $4::uuid" in query
        )
        and args[-1] == RUN_ID
        and args[1] == 4
        for query, args in identity_calls
    )
    assert not any(
        "UPDATE qc_task" in query
        for _operation, query, _args in connection.calls
    )


async def test_passing_gate_raises_epoch_seals_task_and_freezes_sessions() -> None:
    facts_row = _facts_row()
    gate_id = _snapshot_id(epoch=5, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="sealed",
                epoch=5,
                row_version=9,
                gate_snapshot_id=gate_id,
                execution_status="completed_with_gaps",
                quality_outcome="fail",
            ),
        ],
        fetchval_results=[1, 5, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.evaluate_and_seal(
        command=EvaluateCompletionGateCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.COMPLETED_WITH_GAPS,
            quality_outcome=TaskQualityOutcome.FAIL,
        )
    )

    assert decision.snapshot.gate_passed is True
    assert decision.finalization.state is TaskFinalizationState.SEALED
    assert decision.finalization.finalization_epoch == 5
    assert decision.finalization.execution_status is TaskExecutionStatus.COMPLETED_WITH_GAPS
    assert decision.finalization.quality_outcome is TaskQualityOutcome.FAIL
    queries = [call[1] for call in connection.calls]
    assert any("finalization_state = 'finalizing'" in query for query in queries)
    assert any("UPDATE qc_surface_session" in query for query in queries)
    assert any("finalization_state = 'sealed'" in query for query in queries)


async def test_final_report_is_written_and_bound_before_task_is_sealed() -> None:
    facts_row = _facts_row()
    gate_id = _snapshot_id(epoch=5, facts_row=facts_row)
    binding_row = {
        "report_id": "task:1:run:1",
        "report_version": 2,
        "report_payload_hash": "report-hash",
        "task_id": TASK_ID,
        "gate_snapshot_id": gate_id,
        "finalization_epoch": 5,
        "execution_status": "completed",
        "quality_outcome": "pass",
        "bound_at": NOW,
    }
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            {"id": "00000000-0000-0000-0000-000000000002"},
            binding_row,
            {"report_id": "task:1:run:1"},
            _task_row(
                state="sealed",
                epoch=5,
                row_version=10,
                gate_snapshot_id=gate_id,
                execution_status="completed",
                quality_outcome="pass",
            ),
        ],
        fetchval_results=[1, 5, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id="00000000-0000-0000-0000-000000000002",
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
            final_report={
                "report_stage": "final",
                "is_final": True,
                "generated_at": NOW,
                "page_exploration": {
                    "page_results": [
                        {
                            "page_capture_id": "capture-1",
                            "captured_at": NOW,
                        }
                    ],
                },
            },
        )
    )

    assert decision.snapshot.gate_passed is True
    queries = [call[1] for call in connection.calls]
    agent_update_index = next(
        index for index, query in enumerate(queries) if "UPDATE qc_agent_run" in query
    )
    binding_index = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO qc_report_gate_binding" in query
    )
    payload_index = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO qc_report_payload_snapshot" in query
    )
    seal_index = next(
        index for index, query in enumerate(queries) if "finalization_state = 'sealed'" in query
    )
    assert agent_update_index < binding_index < payload_index < seal_index
    assert "jsonb_set" in queries[agent_update_index]
    assert "final_report" in queries[agent_update_index]
    run_payload = json.loads(connection.calls[agent_update_index][2][2])
    binding_hash = connection.calls[binding_index][2][2]
    payload_hash = connection.calls[payload_index][2][2]
    binding_payload = json.loads(connection.calls[payload_index][2][3])
    assert binding_hash == stable_protocol_hash(run_payload)
    assert payload_hash == binding_hash
    assert binding_payload == run_payload
    assert run_payload["generated_at"] == NOW.isoformat()
    assert run_payload["page_exploration"]["page_results"][0]["captured_at"] == NOW.isoformat()


async def test_web_v2_formal_report_reuses_reconciliation_epoch_when_sealing() -> None:
    """Web Coverage 已在当前 epoch 固化，报告封存不得再切换身份。"""

    facts_row = _facts_row()
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    binding_row = {
        "report_id": "task:1:run:1",
        "report_version": 2,
        "report_payload_hash": "report-hash",
        "task_id": TASK_ID,
        "gate_snapshot_id": gate_id,
        "finalization_epoch": 4,
        "execution_status": "completed",
        "quality_outcome": "pass",
        "bound_at": NOW,
    }
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
                "target_platform": "web",
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
            },
            facts_row,
            {"id": RUN_ID},
            binding_row,
            {"report_id": "task:1:run:1"},
            _task_row(
                state="sealed",
                epoch=4,
                row_version=10,
                gate_snapshot_id=gate_id,
                execution_status="completed",
                quality_outcome="pass",
            ),
        ],
        fetchval_results=[1, 4, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.snapshot.finalization_epoch == 4
    assert decision.finalization.finalization_epoch == 4
    task_queries = [
        query
        for method, query, _args in connection.calls
        if method == "fetchrow" and "FROM qc_task" in query
    ]
    assert any(
        "target_platform" in query
        and "task_config->>'workflow_version'" in query
        and "task_config->>'execution_mode'" in query
        for query in task_queries
    )
    finalizing_call = next(
        call
        for call in connection.calls
        if call[0] == "fetchval" and "finalization_state = 'finalizing'" in call[1]
    )
    assert finalizing_call[2][3] == 4


async def test_blocked_atomic_finalization_does_not_write_final_report_or_seal() -> None:
    facts_row = _facts_row(pending_action_count=1)
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id="00000000-0000-0000-0000-000000000002",
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.snapshot.gate_passed is False
    assert decision.finalization.state is TaskFinalizationState.SESSION_DRAINING
    queries = [call[1] for call in connection.calls]
    assert not any("UPDATE qc_agent_run" in query for query in queries)
    assert not any("finalization_state = 'sealed'" in query for query in queries)
    blocked_update = next(
        query
        for query in queries
        if "completion_gate_snapshot_id = $4" in query
    )
    assert "ELSE now() + interval '30 seconds'" in blocked_update
    assert "WHEN $5::boolean THEN 'infinity'::timestamptz" in blocked_update
    assert "OR status = 'done'" in blocked_update


async def test_blocked_finalization_reconciles_stale_row_version_in_same_session_epoch() -> None:
    """恢复器与主 Worker 竞争时只允许同一 owner/epoch 读取当前版本继续。"""

    facts_row = _facts_row(pending_action_count=1)
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            None,
            _task_row(state="session_draining", epoch=4, row_version=8),
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=9,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.FAILED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.finalization.row_version == 9
    update_call = next(
        call
        for call in connection.calls
        if call[0] == "fetchrow" and "completion_gate_snapshot_id = $4" in call[1]
    )
    assert update_call[2][2] == 8
    fallback_query = next(
        call
        for call in connection.calls
        if call[0] == "fetchrow"
        and "finalization_agent_run_id = $3::uuid" in call[1]
        and "finalization_row_version = $3" not in call[1]
    )
    assert fallback_query[2] == (TASK_ID, 4, RUN_ID)
    assert "finalization_row_version = $3" not in fallback_query[1]
    assert "finalization_epoch = $2" in fallback_query[1]
    assert "finalization_agent_run_id = $3::uuid" in fallback_query[1]


async def test_review_only_blocked_gate_marks_task_review_without_sealing() -> None:
    facts_row = _facts_row(
        review_policy_satisfied=False,
        open_review_count=3,
        review_generation=4,
    )
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.snapshot.gate_passed is False
    assert decision.finalization.state is TaskFinalizationState.SESSION_DRAINING
    blocked_update = _blocked_gate_update(connection)
    assert blocked_update[2][4] is True
    assert "execution_status =" not in blocked_update[1]
    assert "quality_outcome =" not in blocked_update[1]
    assert "WHEN $5::boolean THEN 'infinity'::timestamptz" in blocked_update[1]
    assert blocked_update[2] == (TASK_ID, 4, 7, gate_id, True)
    queries = [call[1] for call in connection.calls]
    assert not any("UPDATE qc_agent_run" in query for query in queries)
    assert not any("finalization_state = 'sealed'" in query for query in queries)


async def test_h5_coverage_gap_with_open_reviews_marks_task_review_without_sealing() -> None:
    facts_row = _facts_row(
        review_policy_satisfied=False,
        open_review_count=10,
        h5_coverage_satisfied=False,
    )
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.FAILED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.snapshot.gate_passed is False
    assert decision.finalization.state is TaskFinalizationState.SESSION_DRAINING
    blocked_update = _blocked_gate_update(connection)
    assert blocked_update[2][4] is True
    queries = [call[1] for call in connection.calls]
    assert not any("UPDATE qc_agent_run" in query for query in queries)
    assert not any("finalization_state = 'sealed'" in query for query in queries)


@pytest.mark.parametrize(
    "execution_status",
    (
        TaskExecutionStatus.NEEDS_REVIEW,
        TaskExecutionStatus.FAILED,
        TaskExecutionStatus.COMPLETED_WITH_GAPS,
    ),
)
async def test_h5_coverage_gap_without_open_reviews_marks_task_review_without_sealing(
    execution_status: TaskExecutionStatus,
) -> None:
    facts_row = _facts_row(h5_coverage_satisfied=False)
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=execution_status,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.snapshot.gate_passed is False
    assert decision.snapshot.block_reasons == (CompletionBlockReason.H5_COVERAGE_SCOPE_INCOMPLETE,)
    assert decision.finalization.state is TaskFinalizationState.SESSION_DRAINING
    blocked_update = _blocked_gate_update(connection)
    assert blocked_update[2][4] is True
    queries = [call[1] for call in connection.calls]
    assert not any("UPDATE qc_agent_run" in query for query in queries)
    assert not any("finalization_state = 'sealed'" in query for query in queries)


@pytest.mark.parametrize(
    "execution_status",
    (
        TaskExecutionStatus.NEEDS_REVIEW,
        TaskExecutionStatus.FAILED,
        TaskExecutionStatus.COMPLETED_WITH_GAPS,
    ),
)
async def test_app_inventory_and_coverage_gap_marks_task_review_without_sealing(
    execution_status: TaskExecutionStatus,
) -> None:
    facts_row = _facts_row(
        require_nonempty_candidate_inventory=True,
        no_rules_applicable=False,
        candidate_rule_count=0,
        terminal_candidate_rule_count=0,
        app_coverage_satisfied=False,
    )
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=execution_status,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.snapshot.gate_passed is False
    assert set(decision.snapshot.block_reasons) == {
        CompletionBlockReason.APP_CANDIDATE_INVENTORY_EMPTY,
        CompletionBlockReason.APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE,
    }
    assert decision.finalization.state is TaskFinalizationState.SESSION_DRAINING
    blocked_update = _blocked_gate_update(connection)
    assert blocked_update[2][4] is True
    queries = [call[1] for call in connection.calls]
    assert not any("UPDATE qc_agent_run" in query for query in queries)
    assert not any("finalization_state = 'sealed'" in query for query in queries)


async def test_app_publication_review_and_coverage_gap_marks_task_review() -> None:
    facts_row = _facts_row(
        nonterminal_enforced_work_item_count=1,
        review_policy_satisfied=False,
        open_review_count=1,
        app_coverage_satisfied=False,
    )
    gate_id = _snapshot_id(epoch=4, facts_row=facts_row)
    facts_row["h5_publication_review_required_count"] = 1
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.finalize_task_with_report(
        command=FinalizeTaskWithReportCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            report_id="task:1:run:1",
            report_version=2,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
            final_report={"report_stage": "final", "is_final": True},
        )
    )

    assert decision.snapshot.gate_passed is False
    assert set(decision.snapshot.block_reasons) == {
        CompletionBlockReason.NONTERMINAL_ENFORCED_WORK_ITEM,
        CompletionBlockReason.REVIEW_POLICY_UNSATISFIED,
        CompletionBlockReason.APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE,
    }
    blocked_update = _blocked_gate_update(connection)
    assert blocked_update[2][4] is True
    assert "WHEN $5::boolean THEN 'infinity'::timestamptz" in blocked_update[1]
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "review_generation": 6,
                "finalization_state": "session_draining",
                "finalization_epoch": 0,
                "row_version": 3,
            },
            _task_row(state="session_draining", epoch=0, row_version=4),
        ],
        fetchval_results=[2, "review-policy-1"],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    result = await repository.record_review_policy_decision(
        command=RecordReviewPolicyDecisionCommand(
            task_id=TASK_ID,
            decision_id="review-policy-1",
            policy_hash="policy-hash",
            satisfied=True,
            rationale="允许带两项人工复核进入 needs_review 终态",
            expected_row_version=3,
        )
    )

    assert result.row_version == 4
    insert_call = next(
        call for call in connection.calls if "qc_review_policy_gate_decision" in call[1]
    )
    assert insert_call[2][2] == 6
    assert insert_call[2][4] == 2


async def test_report_binding_requires_current_passed_gate_and_keeps_two_dimensions() -> None:
    binding_row = {
        "report_id": "report-1",
        "report_version": 1,
        "report_payload_hash": "report-hash",
        "task_id": TASK_ID,
        "gate_snapshot_id": "gate-1",
        "finalization_epoch": 5,
        "execution_status": "needs_review",
        "quality_outcome": "unknown",
        "bound_at": NOW,
    }
    connection = FakeConnection(
        fetchrow_results=[
            {"execution_status": "needs_review", "quality_outcome": "unknown"},
            binding_row,
            {"report_id": "report-1"},
        ]
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))
    final_report = {
        "report_stage": "final",
        "is_final": True,
        "generated_at": NOW,
        "evidence": [{"captured_at": NOW}],
    }

    binding = await repository.bind_report(
        command=BindReportToGateCommand(
            report_id="report-1",
            report_version=1,
            report_payload_hash=stable_report_payload_hash(final_report),
            final_report=final_report,
            task_id=TASK_ID,
            gate_snapshot_id="gate-1",
            finalization_epoch=5,
        )
    )

    assert binding.execution_status is TaskExecutionStatus.NEEDS_REVIEW
    assert binding.quality_outcome is TaskQualityOutcome.UNKNOWN
    task_query = next(
        call[1]
        for call in connection.calls
        if "INNER JOIN qc_completion_gate_snapshot AS gate" in call[1]
    )
    assert "gate.gate_passed" in task_query
    assert "task.finalization_state = 'sealed'" in task_query
    payload_call = next(
        call
        for call in connection.calls
        if "INSERT INTO qc_report_payload_snapshot" in call[1]
    )
    payload_query, payload_args = payload_call[1:]
    assert "qc_report_payload_snapshot" in payload_query
    assert payload_args[2] == stable_report_payload_hash(final_report)
    payload = json.loads(payload_args[3])
    assert payload["generated_at"] == NOW.isoformat()
    assert payload["evidence"][0]["captured_at"] == NOW.isoformat()


def test_gate_sql_counts_database_truth_instead_of_redis_or_agent_text() -> None:
    from services.api.app.infrastructure.db import task_finalization_repository as module

    query = " ".join(module._GATE_FACTS_SQL.split())

    for table_name in (
        "qc_v2_work_item",
        "qc_action_execution",
        "qc_capture",
        "qc_evidence_manifest",
        "qc_inspection_work_item",
        "qc_inspection_work_item_rule",
        "qc_aggregate_inspection_work_item",
        "qc_aggregate_inspection_candidate",
        "qc_aggregate_inspection_attempt",
        "qc_evidence_request",
        "qc_journey_execution",
        "qc_restoration_attempt",
        "qc_agent_run",
        "qc_surface_session",
        "qc_outbox_event",
        "qc_h5_evidence_inventory_item",
        "qc_h5_inspection_publication",
        "qc_app_evidence_index",
        "qc_app_inspection_publication",
        "qc_app_inspection_publication_entry",
        "qc_app_inspection_publication_rule",
        "qc_app_inspection_publication_work_item",
    ):
        assert table_name in query
    assert "qc_agent_run WHERE task_id = $1::uuid AND status IN ('pending', 'running')" in query
    assert "SELECT DISTINCT ON (manifest.capture_id)" in query
    assert "manifest.manifest_version DESC" in query
    assert "latest_manifest.quality_status = 'pending'" in query
    assert "task.target_platform NOT IN ('h5', 'web')" in query
    assert "coverage.coverage_satisfied" in query
    assert "snapshot.required_page_types" in query
    # Web coverage 以 coverage_status + required/missing 判定，不再使用 covered_page_types。
    assert "snapshot.covered_page_types" not in query
    assert "snapshot.coverage_status" in query
    nonempty_coverage_guard = (
        "snapshot.completion_ready "
        "AND jsonb_array_length(snapshot.required_page_types) > 0"
    )
    assert nonempty_coverage_guard in query
    assert "FROM qc_h5_coverage_snapshot AS snapshot" in query
    assert "snapshot.missing_page_types" in query
    assert "jsonb_array_elements_text" in query
    assert "target_platform = 'app'" in query
    assert "no_rules_applicable" in query
    assert "page_enforced_work.nonterminal + aggregate_work.nonterminal" in query
    assert "page_candidate_coverage.total + aggregate_candidate_gate.total" in query
    assert "page_candidate_coverage.terminal + aggregate_candidate_gate.terminal" in query
    assert "aggregate_expected.total - aggregate_candidate_gate.total" in query
    assert "evidence_input_scope" in query
    assert "aggregate_candidate_gate.review_required" in query
    assert "work_item.finalization_epoch = task.finalization_epoch" in query
    assert "attempt.status = 'running'" in query
    assert "task.task_config->>'h5_inspection_notification_mode' = 'enforced'" in query
    assert "task.task_config->>'execution_mode' = 'full_inspection'" in query
    assert "INNER JOIN qc_page_capture AS page_capture" in query
    assert "page_capture.page_type = inventory.page_type" in query
    assert (
        "COALESCE( page_capture.inspection_lifecycle_status, "
        "capture.metadata->>'inspection_lifecycle_status', '' )"
    ) in query
    assert "publication.id IS NULL" in query
    assert "publication.status IN ('failed', 'needs_review')" in query
    assert "qualified.published_inspection_count IS DISTINCT FROM" in query
    assert "live_work.session_dependency = 'active_session'" in query
    assert "live_link.work_item_id = live_work.id" in query
    assert "live_member.execution_plan->>'disposition'" in query
    assert "work_member.rule_id = live_member.rule_id" in query
    assert "h5_publication_gate.review_required" in query
    assert "app_sealed_index.missing_publication_count" in query
    assert "app_publication_gate.active_or_inconsistent" in query
    assert "app_publication_gate.review_required" in query
    assert "publication.expected_entry_count IS DISTINCT FROM" in query
    assert "publication.expected_rule_count IS DISTINCT FROM" in query
    assert "member.session_dependency = 'active_session'" in query
    assert "member.disposition NOT IN ( 'linked', 'needs_review' )" in query
    assert "link.work_item_id = member.work_item_id" in query
    assert "'alioth.v2.app-inspection-publication.available'" in query
    assert "'alioth.v2.aggregate-inspection-work.available'" in query
    assert "action.finalization_epoch = surface_session.finalization_epoch" in query
    assert "surface_session.finalization_epoch = task.finalization_epoch" in query
    assert "qualified_h5_captures.missing_publication_count" in query
    assert "redis" not in query.lower()


def test_h5_publication_review_with_coverage_gap_marks_task_review() -> None:
    """Publication 待复核与覆盖缺口并存时不得永久停在 session_draining。"""
    from services.api.app.infrastructure.db import task_finalization_repository as module

    facts_row = _facts_row(
        nonterminal_enforced_work_item_count=5,
        review_policy_satisfied=False,
        h5_coverage_satisfied=False,
    )
    facts = CompletionGateFacts(
        **{key: value for key, value in facts_row.items() if key != "evaluated_at"}
    )

    assert module._should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED,
        facts=facts,
        publication_review_required_count=5,
    ) is True


def test_h5_completion_inventory_excludes_targeted_recapture_from_normal_publication() -> None:
    from services.api.app.infrastructure.db.h5_completion_gate_sql import (
        QUALIFIED_H5_INVENTORY_CTE_SQL,
    )

    normalized = " ".join(QUALIFIED_H5_INVENTORY_CTE_SQL.split())

    assert "supplemental_evidence_source" in normalized
    assert "targeted_recapture" in normalized


def test_h5_natural_evidence_violation_can_enter_review_without_affecting_others() -> None:
    """H5 展示证据违规已是复核事实，不应让任务永久保持 running。"""
    from services.api.app.infrastructure.db import task_finalization_repository as module

    facts_row = _facts_row(
        nonterminal_enforced_work_item_count=1,
        review_policy_satisfied=False,
        h5_coverage_satisfied=False,
        natural_evidence_presentation_violation_count=1,
    )
    facts = CompletionGateFacts(
        **{key: value for key, value in facts_row.items() if key != "evaluated_at"}
    )

    assert module._should_mark_task_review(
        execution_status=TaskExecutionStatus.NEEDS_REVIEW,
        facts=facts,
        publication_review_required_count=1,
        allow_natural_evidence_review=True,
    ) is True
    assert module._should_mark_task_review(
        execution_status=TaskExecutionStatus.NEEDS_REVIEW,
        facts=facts,
        publication_review_required_count=1,
    ) is False


def test_app_gate_accepts_completed_active_session_members_only_with_linked_identity() -> None:
    """已完成的活会话规则不是在途事实，但 linked 身份漂移仍必须阻断。"""

    from services.api.app.infrastructure.db import task_finalization_repository as module

    query = " ".join(module._GATE_FACTS_SQL.split())

    assert "member.session_dependency = 'active_session'" in query
    assert "member.disposition NOT IN ( 'linked', 'needs_review' )" in query
    assert "member.disposition <> 'needs_review'" not in query

    assert "publication.linked_work_item_count IS DISTINCT FROM" in query
    assert "member.disposition = 'linked' AND NOT EXISTS" in query
    assert "link.publication_id = publication.id" in query
    assert "link.entry_id = member.entry_id" in query
    assert "link.work_item_id = member.work_item_id" in query
    assert "work_member.rule_id = member.rule_id" in query


def test_recovery_scan_includes_sealed_tasks_without_report_binding() -> None:
    source = inspect.getsource(PostgresTaskFinalizationRepository.list_recoverable_finalizations)
    assert "'session_draining'" in source
    assert "'exploration_draining'" in source
    assert "finalization_state = 'sealed'" in source
    assert "qc_report_gate_binding" in source
    assert "NOT EXISTS" in source


def test_recovery_scan_includes_open_tasks_with_finished_runs() -> None:
    source = inspect.getsource(PostgresTaskFinalizationRepository.list_recoverable_finalizations)
    assert "'open'" in source
    assert "qc_agent_run" in source
    assert "status IN ('done', 'failed', 'aborted', 'canceled')" in source
    assert "qc_task.status IN ('review', 'done')" in source
    assert "AND finalization_state = 'session_draining'" in source


def test_review_session_draining_tasks_can_retry_gate_evaluation() -> None:
    finalize_source = inspect.getsource(
        PostgresTaskFinalizationRepository.finalize_task_with_report
    )
    evaluate_source = inspect.getsource(PostgresTaskFinalizationRepository.evaluate_and_seal)

    assert "status IN ('running', 'review', 'done')" in finalize_source
    assert "finalization_state = 'session_draining'" in finalize_source
    assert "status IN ('running', 'review', 'done')" in evaluate_source
    assert "finalization_state = 'session_draining'" in evaluate_source


def test_finalization_migration_enforces_epoch_ownership_and_immutable_audit() -> None:
    migration_path = (
        Path(__file__).parents[1] / "migrations/versions/20260803_0032_v2_task_completion_gate.py"
    )
    statements = []

    class RecordingMigrationOp:
        def execute(self, statement) -> None:
            statements.append(str(statement))

    namespace = runpy.run_path(str(migration_path))
    namespace["upgrade"].__globals__["op"] = RecordingMigrationOp()
    namespace["upgrade"]()
    migration = "\n".join(statements)

    assert "id, completion_gate_snapshot_id, finalization_epoch" in migration
    assert "task_id, gate_snapshot_id, finalization_epoch" in migration
    assert "id, review_generation, review_policy_decision_id" in migration
    assert "task_id, review_generation, decision_id" in migration
    assert "review_policy_decision_id = NULL" in migration
    assert "qc_guard_immutable_audit_record" in migration
    assert "trg_qc_agent_run_finalization_guard" in migration
    assert "拒绝新增或改写 Agent Run" in migration
    for table_name in (
        "qc_review_policy_gate_decision",
        "qc_completion_gate_snapshot",
        "qc_report_gate_binding",
    ):
        assert f"trg_{table_name}_immutable" in migration


def test_epoch_raise_clears_previous_denied_gate_before_sealing() -> None:
    source = inspect.getsource(PostgresTaskFinalizationRepository.evaluate_and_seal)

    epoch_raise = source.index("finalization_epoch = finalization_epoch + 1")
    clear_snapshot = source.index("completion_gate_snapshot_id = NULL")
    seal = source.index("finalization_state = 'sealed'")
    assert epoch_raise < clear_snapshot < seal


def test_finalization_gate_refuses_durable_cancel_marker() -> None:
    """Gate 取得收口所有权后仍须以持久化取消标记作为最终仲裁。"""

    for method in (
        PostgresTaskFinalizationRepository.evaluate_and_seal,
        PostgresTaskFinalizationRepository.finalize_task_with_report,
    ):
        source = inspect.getsource(method)
        assert "output_context->>'cancel_requested'" in source
        assert "finalization_agent_run_id = $4::uuid" in source


def test_0096_migration_adds_presentation_violation_to_gate_formula() -> None:
    migration = Path(__file__).parents[1] / (
        "migrations/versions/20260907_0096_presentation_gate_formula.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert 'down_revision = "20260907_0095"' in source
    assert "NOT (block_reasons ? 'natural_evidence_presentation_violation')" in source
    assert "NOT (block_reasons ? 'web_coverage_snapshot_incomplete')" in source
    downgrade = source.split("def downgrade", 1)[1]
    assert "natural_evidence_presentation_violation" not in downgrade
    assert "NOT VALID" in downgrade


def _presentation_row(
    *,
    category: str = "text",
    status: str = "pass",
    presentation: dict[str, Any] | None = None,
    judge_config: dict[str, Any] | None = None,
    surface_kind: str = "web",
    visual_page_state: str | None = None,
) -> dict[str, Any]:
    return {
        "execution_status": status,
        "category": category,
        "rule_name": "页面文案检查",
        "check_point": "页面文案应当规范",
        "judge_config": json.dumps(judge_config or {}),
        "surface_kind": surface_kind,
        "metadata": json.dumps({"presentation": presentation} if presentation else {}),
        "page_state": "stable",
        "visual_page_state": visual_page_state,
        "visual_overlay_signature": None,
        "visual_surface_facts": None,
    }


_OCCLUDED_PRESENTATION = {
    "protocol_version": "presentation-stack.v1",
    "topmost_layer_kind": "overlay",
    "base_page_visibility": "fully_occluded",
    "blocking_scope": "page",
    "mutation_mode": "natural",
    "hit_test_status": "available",
    "evidence_sources": ["h5_ui_snapshot"],
    "diagnostic_operations": [],
    "uncertainty": [],
}
_DIAGNOSTIC_PRESENTATION = {
    **_OCCLUDED_PRESENTATION,
    "topmost_layer_kind": "page",
    "base_page_visibility": "visible",
    "blocking_scope": "none",
    "mutation_mode": "instrumented_diagnostic",
    "diagnostic_operations": [
        {"kind": "style_hide_obstruction", "source": "verified_high_z_obstruction_hide"}
    ],
}


async def test_gate_counts_natural_conclusions_built_on_occluded_or_diagnostic_material() -> None:
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _NATURAL_EVIDENCE_PRESENTATION_SQL,
        _count_natural_evidence_presentation_violations,
    )

    rows = [
        # 文案规则拿了被遮挡底页 → 违规
        _presentation_row(presentation=_OCCLUDED_PRESENTATION),
        # 广告规则要的就是浮层 → 合法
        _presentation_row(category="ad", presentation=_OCCLUDED_PRESENTATION),
        # 诊断材料 → 违规
        _presentation_row(status="fail", presentation=_DIAGNOSTIC_PRESENTATION),
        _presentation_row(
            presentation=_DIAGNOSTIC_PRESENTATION,
            judge_config={"accepts_diagnostic_evidence": True},
        ),  # 规则显式接受诊断材料 → 合法
        _presentation_row(surface_kind="android", visual_page_state="popup"),  # App 弹窗帧 → 违规
        _presentation_row(presentation=None),  # 历史采集无评估 → 不拒
    ]
    connection = FakeConnection(fetch_results=[rows])

    count = await _count_natural_evidence_presentation_violations(connection, task_id=TASK_ID)

    assert count == 3
    query = " ".join(_NATURAL_EVIDENCE_PRESENTATION_SQL.split())
    assert "execution.execution_status IN ('pass', 'fail', 'warning')" in query
    assert "LEFT JOIN qc_app_visual_observation AS visual" in query
    assert "= task.finalization_epoch" in query
    assert connection.calls[0][2] == (TASK_ID,)


async def test_blocked_gate_records_presentation_violation_reason() -> None:
    facts_row = _facts_row()
    violation_rows = [_presentation_row(presentation=_OCCLUDED_PRESENTATION)]
    facts = CompletionGateFacts(
        **{key: value for key, value in facts_row.items() if key != "evaluated_at"},
        natural_evidence_presentation_violation_count=1,
    )
    gate_id = build_completion_gate_snapshot(
        task_id=TASK_ID,
        finalization_epoch=4,
        evaluation_sequence=1,
        facts=facts,
        evaluated_at=NOW,
    ).gate_snapshot_id
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": TASK_ID,
                "finalization_state": "session_draining",
                "finalization_epoch": 4,
                "row_version": 7,
            },
            facts_row,
            _task_row(
                state="session_draining",
                epoch=4,
                row_version=8,
                gate_snapshot_id=gate_id,
            ),
        ],
        fetch_results=[violation_rows],
        fetchval_results=[1, gate_id],
    )
    repository = PostgresTaskFinalizationRepository(pool=FakePool(connection))

    decision = await repository.evaluate_and_seal(
        command=EvaluateCompletionGateCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
        )
    )

    assert decision.snapshot.gate_passed is False
    assert decision.snapshot.block_reasons == (
        CompletionBlockReason.NATURAL_EVIDENCE_PRESENTATION_VIOLATION,
    )
    assert decision.snapshot.facts.natural_evidence_presentation_violation_count == 1
    insert_call = next(
        call for call in connection.calls if "INSERT INTO qc_completion_gate_snapshot" in call[1]
    )
    assert "natural_evidence_presentation_violation" in insert_call[2][24]
