"""聚合质检工作项 PostgreSQL 仓储 SQL 合同。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.aggregate_inspection_work_items import (
    AggregateInspectionAttemptStatus,
    AggregateInspectionWorkItemStatus,
    ClaimAggregateInspectionWorkItemCommand,
    PublishAggregateInspectionWorkItemCommand,
    RenewAggregateInspectionWorkItemLeaseCommand,
    StartAggregateInspectionAttemptCommand,
    TransitionAggregateInspectionAttemptCommand,
    TransitionAggregateInspectionWorkItemCommand,
)
from services.api.app.domain.evidence import EvidenceInputScope
from services.api.app.infrastructure.db.aggregate_inspection_work_item_repository import (
    PostgresAggregateInspectionWorkItemRepository,
)


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> _Context:
        return _Context(self.connection)


class FakeConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_result: dict[str, Any] | None = None
        self.fetchval_result: Any = None

    def transaction(self) -> _Context:
        return _Context(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append((" ".join(query.split()), args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return self.fetchrow_result

    async def fetchval(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return self.fetchval_result


def _publish_command() -> PublishAggregateInspectionWorkItemCommand:
    return PublishAggregateInspectionWorkItemCommand(
        task_id="11111111-1111-1111-1111-111111111111",
        evidence_input_set_id="evidence-input-set:" + "a" * 64,
        producer_agent_run_id="22222222-2222-2222-2222-222222222222",
        rule_id="33333333-3333-3333-3333-333333333333",
        rule_code="alioth_spec_48",
        rule_contract_hash="contract-hash",
        scope=EvidenceInputScope.TASK,
        scope_key="toast_style",
        finalization_epoch=3,
    )


async def test_publish_joins_current_input_set_run_rule_and_epoch() -> None:
    connection = FakeConnection()
    connection.fetchrow_result = {
        "id": "44444444-4444-4444-4444-444444444444",
        "input_hash": "a" * 64,
        "status": "pending",
        "evidence_input_set_id": "evidence-input-set:" + "a" * 64,
        "finalization_epoch": 3,
        "row_version": 0,
        "created": True,
    }
    connection.fetchval_result = "outbox-message-id"
    repository = PostgresAggregateInspectionWorkItemRepository(
        pool=FakePool(connection)
    )

    published = await repository.publish(command=_publish_command())

    query, args = next(
        item for item in connection.queries if "INSERT INTO qc_aggregate" in item[0]
    )
    assert published.created is True
    assert "task.status IN ('running', 'review')" in query
    assert "task.finalization_state = 'session_draining'" in query
    assert "producer.id = input_set.source_agent_run_id" in query
    assert "ruleset.snapshot_hash = input_set.ruleset_snapshot_hash" in query
    assert "input_set.finalization_epoch = task.finalization_epoch" in query
    assert "page_capture_id" not in query
    assert args[12] == 3
    assert any(
        "INSERT INTO qc_outbox_event" in sql
        and "alioth.v2.aggregate-inspection-work.available" in str(values)
        for sql, values in connection.queries
    )


async def test_claim_uses_skip_locked_and_abandons_old_running_attempt() -> None:
    connection = FakeConnection()
    connection.fetchrow_result = {
        "id": "44444444-4444-4444-4444-444444444444",
        "task_id": "11111111-1111-1111-1111-111111111111",
        "evidence_input_set_id": "evidence-input-set:" + "a" * 64,
        "input_hash": "a" * 64,
        "rule_id": "33333333-3333-3333-3333-333333333333",
        "rule_code": "alioth_spec_48",
        "rule_contract_hash": "contract-hash",
        "scope": "task",
        "scope_key": "toast_style",
        "finalization_epoch": 3,
        "lease_token": "55555555-5555-5555-5555-555555555555",
        "attempt_count": 2,
        "row_version": 2,
    }
    repository = PostgresAggregateInspectionWorkItemRepository(
        pool=FakePool(connection)
    )

    lease = await repository.claim(
        command=ClaimAggregateInspectionWorkItemCommand(
            task_id="11111111-1111-1111-1111-111111111111",
            consumer_agent_run_id="66666666-6666-6666-6666-666666666666",
            lease_owner="worker-1",
            lease_seconds=30,
        ),
        lease_token="55555555-5555-5555-5555-555555555555",
    )

    assert lease is not None and lease.attempt_no == 2
    claim_query = next(sql for sql, _ in connection.queries if "WITH candidate AS" in sql)
    assert "task.status IN ('running', 'review')" in claim_query
    assert "task.finalization_state = 'session_draining'" in claim_query
    assert "work_item.finalization_epoch = task.finalization_epoch" in claim_query
    assert any("FOR UPDATE OF work_item SKIP LOCKED" in query for query, _ in connection.queries)
    assert any("status = 'abandoned'" in query for query, _ in connection.queries)


async def test_attempt_and_work_transition_require_same_lease_and_result_hash() -> None:
    connection = FakeConnection()
    repository = PostgresAggregateInspectionWorkItemRepository(
        pool=FakePool(connection)
    )
    connection.fetchval_result = "77777777-7777-7777-7777-777777777777"
    attempt_id = await repository.start_attempt(
        command=StartAggregateInspectionAttemptCommand(
            work_item_id="44444444-4444-4444-4444-444444444444",
            consumer_agent_run_id="66666666-6666-6666-6666-666666666666",
            attempt_no=1,
            lease_token="55555555-5555-5555-5555-555555555555",
            input_hash="a" * 64,
        )
    )
    assert attempt_id.startswith("7777")

    updated = await repository.transition_attempt(
        command=TransitionAggregateInspectionAttemptCommand(
            attempt_id=attempt_id,
            expected_status=AggregateInspectionAttemptStatus.RUNNING,
            target_status=AggregateInspectionAttemptStatus.SUCCEEDED,
            lease_token="55555555-5555-5555-5555-555555555555",
            result_payload={"conclusion": "pass"},
        ),
        result_hash="b" * 64,
    )
    assert updated is True

    connection.fetchval_result = 3
    version = await repository.transition(
        command=TransitionAggregateInspectionWorkItemCommand(
            work_item_id="44444444-4444-4444-4444-444444444444",
            expected_status=AggregateInspectionWorkItemStatus.RUNNING,
            target_status=AggregateInspectionWorkItemStatus.SUCCEEDED,
            row_version=2,
            lease_token="55555555-5555-5555-5555-555555555555",
            attempt_id=attempt_id,
            result_payload={"conclusion": "pass"},
        ),
        result_hash="b" * 64,
    )

    assert version == 3
    transition_query = connection.queries[-1][0]
    assert "task.status IN ('running', 'review')" in transition_query
    assert "task.finalization_state = 'session_draining'" in transition_query
    assert "task.finalization_epoch = work_item.finalization_epoch" in transition_query
    assert "FROM qc_aggregate_inspection_attempt AS attempt" in transition_query
    assert "attempt.id = $11::uuid" in transition_query
    assert "attempt.result_hash IS NOT DISTINCT FROM $8" in transition_query
    assert "gate_wakeup AS" in transition_query
    assert "SET finalization_retry_at = now()" in transition_query
    before_gate_wakeup, gate_wakeup = transition_query.split("gate_wakeup AS", 1)
    assert "target_platform" not in before_gate_wakeup
    assert "task.target_platform = 'h5'" in gate_wakeup

    connection.fetchval_result = 4
    renewed = await repository.renew_lease(
        command=RenewAggregateInspectionWorkItemLeaseCommand(
            work_item_id="44444444-4444-4444-4444-444444444444",
            consumer_agent_run_id="66666666-6666-6666-6666-666666666666",
            lease_token="55555555-5555-5555-5555-555555555555",
            row_version=3,
            lease_seconds=30,
        )
    )
    assert renewed == 4
    assert "lease_expires_at > now()" in connection.queries[-1][0]


async def test_finalize_recovers_terminal_attempt_and_closes_remaining_work() -> None:
    connection = FakeConnection()
    connection.fetchrow_result = {
        "recovered_count": 2,
        "failed_count": 1,
        "abandoned_count": 3,
    }
    repository = PostgresAggregateInspectionWorkItemRepository(
        pool=FakePool(connection)
    )

    result = await repository.finalize_task_work(
        task_id="11111111-1111-1111-1111-111111111111",
        error_code="AGGREGATE_FINALIZATION_INCOMPLETE",
        error_message="任务级规则缺少正式结果",
    )

    assert result.recovered_work_item_count == 2
    assert result.failed_work_item_count == 1
    assert result.abandoned_attempt_count == 3
    assert "task-finalization:" in connection.queries[0][1][0]
    query = connection.queries[1][0]
    assert "WITH target_task AS" in query
    assert "work_item.finalization_epoch = task.finalization_epoch" in query
    assert "attempt.attempt_no = work_item.attempt_count" in query
    assert "attempt.input_hash = work_item.input_hash" in query
    assert "FROM recovered" in query
    assert "status = 'abandoned'" in query
    assert "status = 'failed'" in query
