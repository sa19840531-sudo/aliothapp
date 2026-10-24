"""PostgreSQL 持久化质检工作项仓储 SQL 契约测试。"""

import inspect
import json
from typing import Any

import pytest

from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    FindCompletedInspectionWorkItemCommand,
    InspectionAttemptStatus,
    InspectionWorkItemRule,
    InspectionWorkItemRuleStatus,
    InspectionWorkItemStatus,
    PublishInspectionWorkItemCommand,
    RenewInspectionWorkItemLeaseCommand,
    TransitionInspectionAttemptCommand,
    TransitionInspectionWorkItemCommand,
    TransitionInspectionWorkItemRuleCommand,
)
from services.api.app.infrastructure.db.app_consumer_run_lifecycle import (
    finish_app_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.h5_consumer_run_lifecycle import (
    finish_h5_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
    _h5_probe_capture_roles,
    publish_inspection_work_item_on_connection,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        fetchrow_result: dict[str, Any] | None = None,
        fetch_result: list[dict[str, Any]] | None = None,
        fetch_results: list[list[dict[str, Any]]] | None = None,
        fetchval_result: Any = None,
    ) -> None:
        self.fetchrow_result = fetchrow_result
        self.fetch_result = fetch_result or []
        self.fetch_results = list(fetch_results) if fetch_results is not None else None
        self.fetchval_result = fetchval_result
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any):
        self.calls.append(("fetchrow", " ".join(query.split()), args))
        return self.fetchrow_result

    async def fetch(self, query: str, *args: Any):
        self.calls.append(("fetch", " ".join(query.split()), args))
        if self.fetch_results is not None:
            return self.fetch_results.pop(0)
        if "qc_evidence_supplement" in query:
            # 领取时的补采清单查询：假连接没有补采数据，返回空而不是复用规则行
            return []
        if "member.evidence_input_set_id IS NOT NULL" in query and "packet.packet_payload" in query:
            # 本夹具只提供普通未绑定成员；不能把最终链接摘要冒充原输入授权行。
            return []
        return self.fetch_result

    async def fetchval(self, query: str, *args: Any):
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return self.fetchval_result

    async def execute(self, query: str, *args: Any):
        self.calls.append(("execute", " ".join(query.split()), args))
        return "INSERT 0 1"


def test_h5_probe_capture_roles_project_runtime_lineage_to_rule_roles() -> None:
    roles = _h5_probe_capture_roles(
        {
            "capture_role": "probe_result_replay",
            "parent_capture_role": "probe_before",
            "parent_capture_id": "capture-before",
            "page_capture_id": "capture-after",
        }
    )

    assert roles == (
        ("before", "capture-before"),
        ("primary", "capture-after"),
    )


def test_h5_probe_capture_roles_preserve_requested_refreshed_role() -> None:
    roles = _h5_probe_capture_roles(
        {
            "capture_role": "probe_result_replay",
            "parent_capture_role": "probe_before",
            "parent_capture_id": "capture-before",
            "page_capture_id": "capture-after",
            "request_payload": {
                "target_invariants": {
                    "semantic": {"capture_roles": ["primary", "refreshed"]}
                }
            },
        }
    )

    assert roles == (
        ("primary", "capture-before"),
        ("refreshed", "capture-after"),
    )


@pytest.mark.parametrize("required_roles,expected", [
    (["primary"], (("primary", "capture-after"),)),
    (["before", "primary"], (("before", "capture-before"), ("primary", "capture-after"))),
    (["primary", "refreshed"], (("primary", "capture-before"), ("refreshed", "capture-after"))),
])
def test_h5_probe_uses_complete_frozen_roles_not_only_requested_missing_roles(
    required_roles, expected,
):
    assert _h5_probe_capture_roles({
        "target_platform": "h5",
        "capture_role": "probe_result_replay",
        "parent_capture_role": "probe_before",
        "parent_capture_id": "capture-before",
        "page_capture_id": "capture-after",
        "rule_input_contract": {"required_capture_roles": required_roles},
        "request_payload": {"target_invariants": {"semantic": {"capture_roles": ["primary"]}}},
    }) == expected


@pytest.mark.parametrize("platform", ("app", "web"))
def test_h5_probe_projection_does_not_apply_to_other_platforms(platform) -> None:
    assert (
        _h5_probe_capture_roles(
            {
                "target_platform": platform,
                "capture_role": "probe_result_replay",
                "parent_capture_role": "probe_before",
                "parent_capture_id": "capture-before",
                "page_capture_id": "capture-after",
            }
        )
        == ()
    )


@pytest.mark.parametrize(
    "row",
    (
        {
            "capture_role": "viewport",
            "parent_capture_role": "probe_before",
            "parent_capture_id": "capture-before",
            "page_capture_id": "capture-after",
        },
        {
            "capture_role": "probe_result_replay",
            "parent_capture_role": "viewport",
            "parent_capture_id": "capture-before",
            "page_capture_id": "capture-after",
        },
        {
            "capture_role": "probe_result_replay",
            "parent_capture_role": "probe_before",
            "parent_capture_id": None,
            "page_capture_id": "capture-after",
        },
    ),
)
def test_non_h5_probe_capture_roles_do_not_change_existing_evidence(row) -> None:
    assert _h5_probe_capture_roles(row) == ()


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def test_publish_uses_connection_scoped_primitive_for_atomic_composition() -> None:
    repository_source = inspect.getsource(PostgresInspectionWorkItemRepository.publish)
    primitive_source = inspect.getsource(publish_inspection_work_item_on_connection)

    assert "connection.transaction()" in repository_source
    assert "publish_inspection_work_item_on_connection" in repository_source
    assert "qc_inspection_work_item" in primitive_source
    assert "qc_inspection_work_item_rule" in primitive_source


def test_h5_consumer_finish_reuses_the_shared_chain_lifecycle_sql() -> None:
    source = inspect.getsource(PostgresInspectionWorkItemRepository.finish_h5_consumer_run_if_idle)
    shared_source = inspect.getsource(
        PostgresInspectionWorkItemRepository.finish_publication_consumer_run_if_idle
    )

    assert "finish_publication_consumer_run_if_idle" in source
    assert "finish_h5_consumer_run_if_idle_on_connection" in shared_source
    assert "finish_app_consumer_run_if_idle_on_connection" in shared_source
    assert inspect.iscoroutinefunction(finish_h5_consumer_run_if_idle_on_connection)
    assert inspect.iscoroutinefunction(finish_app_consumer_run_if_idle_on_connection)


@pytest.mark.parametrize(
    "finish_consumer_run",
    [
        finish_h5_consumer_run_if_idle_on_connection,
        finish_app_consumer_run_if_idle_on_connection,
    ],
)
async def test_consumer_terminalization_schedules_fenced_gate_reevaluation(
    finish_consumer_run: Any,
) -> None:
    connection = FakeConnection(fetchval_result="run-judge-1")

    closed = await finish_consumer_run(
        connection,
        task_id="task-1",
        consumer_agent_run_id="run-judge-1",
    )

    assert closed is True
    retry_call = next(
        call
        for call in connection.calls
        if call[0] == "execute" and "UPDATE qc_task" in call[1]
    )
    assert "UPDATE qc_task" in retry_call[1]
    assert "finalization_retry_at = now()" in retry_call[1]
    assert "completion_gate_snapshot_id IS NOT NULL" in retry_call[1]
    assert "consumer.input_context->>'finalization_epoch'" in retry_call[1]
    assert retry_call[2] == ("task-1", "run-judge-1")


async def test_claimable_backlog_count_uses_the_actual_consumer_visibility() -> None:
    connection = FakeConnection(fetchval_result=3)
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    count = await repository.count_claimable_pending_work(
        task_id="00000000-0000-0000-0000-000000000001",
        consumer_agent_run_id="00000000-0000-0000-0000-000000000002",
    )

    assert count == 3
    _method, query, args = connection.calls[-1]
    assert "work_item.session_owner_agent_run_id = $2::uuid" in query
    assert "consumer.id = $2::uuid" in query
    assert "work_item.next_attempt_at <= now()" in query
    assert args == (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )


async def test_waitable_backlog_count_includes_future_retry_windows() -> None:
    connection = FakeConnection(fetchval_result=4)
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    count = await repository.count_waitable_nonterminal_work(
        task_id="00000000-0000-0000-0000-000000000001",
        consumer_agent_run_id="00000000-0000-0000-0000-000000000002",
    )

    assert count == 4
    _method, query, args = connection.calls[-1]
    assert "work_item.status IN ('pending', 'retry_wait')" in query
    assert "work_item.next_attempt_at <= now()" not in query
    assert "work_item.session_owner_agent_run_id = $2::uuid" in query
    assert args == (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )


def _publish_command() -> PublishInspectionWorkItemCommand:
    return PublishInspectionWorkItemCommand(
        task_id="00000000-0000-0000-0000-000000000001",
        page_capture_id="00000000-0000-0000-0000-000000000002",
        producer_agent_run_id="00000000-0000-0000-0000-000000000003",
        ruleset_id="00000000-0000-0000-0000-000000000004",
        ruleset_version=2,
        ruleset_snapshot_hash="ruleset-hash",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        rules=(
            InspectionWorkItemRule(
                rule_id="00000000-0000-0000-0000-000000000005",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="contract-hash",
            ),
        ),
    )


async def test_publish_is_transactional_and_idempotent() -> None:
    connection = FakeConnection(
        fetchrow_result={
            "id": "00000000-0000-0000-0000-000000000010",
            "status": "pending",
            "created": True,
        },
        fetchval_result="00000000-0000-0000-0000-000000000011",
    )
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    result = await repository.publish(
        command=_publish_command(),
        input_hash="input-hash",
    )

    publish_call = next(call for call in connection.calls if call[0] == "fetchrow")
    rule_call = next(call for call in connection.calls if call[0] == "fetchval")
    assert result.created is True
    assert result.status == InspectionWorkItemStatus.PENDING
    assert "uq_qc_inspection_work_item_identity" in publish_call[1]
    assert "xmax = 0" in publish_call[1]
    assert "page_capture.task_id = task.id" in publish_call[1]
    assert "page_capture.agent_run_id = producer.id" in publish_call[1]
    assert "ruleset.id = task.ruleset_id" in publish_call[1]
    assert publish_call[2][12] == "input-hash"
    assert publish_call[2][15] == "enforced"
    assert "uq_qc_inspection_work_item_rule" in rule_call[1]
    assert "rule.ruleset_id = ANY(qc_ruleset_scope(work_item.ruleset_id))" in rule_call[1]


async def test_record_consumer_usage_merges_output_context_and_total_columns() -> None:
    connection = FakeConnection(
        fetchrow_result={
            "output_context": {
                "final_text": "规则消费中",
                "usage": {
                    "input_tokens": 5,
                    "total_tokens": 5,
                    "ai_calls": 1,
                    "total_cost": 0.00005,
                },
            }
        },
    )
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    recorded = await repository.record_consumer_usage(
        task_id="00000000-0000-0000-0000-000000000001",
        consumer_agent_run_id="00000000-0000-0000-0000-000000000002",
        usage={
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "ai_calls": 1,
            "total_cost": 0.0001,
        },
    )

    fetch_call = next(call for call in connection.calls if call[0] == "fetchrow")
    execute_call = next(call for call in connection.calls if call[0] == "execute")
    output_context = json.loads(execute_call[2][2])
    assert recorded is True
    assert "agent_type IN ('main', 'rule_judge')" in fetch_call[1]
    assert "agent_type IN ('main', 'rule_judge')" in execute_call[1]
    assert output_context["final_text"] == "规则消费中"
    assert output_context["usage"]["input_tokens"] == 12
    assert output_context["usage"]["output_tokens"] == 3
    assert output_context["usage"]["total_tokens"] == 15
    assert output_context["usage"]["ai_calls"] == 2
    assert output_context["usage"]["total_cost"] == 0.00015
    assert execute_call[2][3] == 2
    assert execute_call[2][4] == 15
    assert execute_call[2][5] == 0.00015


async def test_claim_uses_skip_locked_and_session_owner_gate() -> None:
    connection = FakeConnection(
        fetchrow_result={
            "id": "00000000-0000-0000-0000-000000000010",
            "task_id": "00000000-0000-0000-0000-000000000001",
            "page_capture_id": "00000000-0000-0000-0000-000000000002",
            "batch_key": "hybrid:visual:snapshot",
            "strategy": "hybrid",
            "category": "visual",
            "session_dependency": "snapshot",
            "input_hash": "input-hash",
            "producer_agent_run_id": "00000000-0000-0000-0000-000000000004",
            "consumer_agent_run_id": "00000000-0000-0000-0000-000000000003",
            "lease_token": "00000000-0000-0000-0000-000000000020",
            "attempt_count": 1,
            "row_version": 1,
            "target_platform": "web",
        },
        fetch_results=[
            [{
                "rule_id": "00000000-0000-0000-0000-000000000005",
                "rule_code": "alioth_1",
                "ordinal": 0,
                "contract_hash": "contract-hash",
            }],
            [],  # 没有待封存的补采（领取时先把已接上的补采封成输入集合）。
            [],  # 没有需要重新封存的补采输入集合成员。
            [],  # 普通工作没有补采绑定，这次查询不能重复返回规则成员。
        ],
    )
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    result = await repository.claim(
        command=ClaimInspectionWorkItemCommand(
            task_id="00000000-0000-0000-0000-000000000001",
            consumer_agent_run_id="00000000-0000-0000-0000-000000000003",
            lease_owner="worker-1",
            lease_seconds=180,
            session_dependency="snapshot",
            page_capture_id="00000000-0000-0000-0000-000000000002",
            rule_codes=("alioth_1",),
        ),
        lease_token="00000000-0000-0000-0000-000000000020",
    )

    assert result is not None
    assert result.producer_agent_run_id == "00000000-0000-0000-0000-000000000004"
    assert result.consumer_agent_run_id == "00000000-0000-0000-0000-000000000003"
    assert result.target_platform == "web"
    assert result.rules[0].rule_code == "alioth_1"
    claim_query = connection.calls[0][1]
    assert "FOR UPDATE OF work_item SKIP LOCKED" in claim_query
    assert "work_item.lease_expires_at <= now()" in claim_query
    assert "work_item.attempt_count < work_item.max_attempts" in claim_query
    assert "work_item.session_owner_agent_run_id = $3::uuid" in claim_query
    assert "task.status = 'review'" in claim_query
    assert "task.finalization_state = 'session_draining'" in claim_query
    assert "recovery_consumer.agent_type = 'rule_judge'" in claim_query
    assert "'h5_inspection_publication'" in claim_query
    assert "'app_inspection_publication'" in claim_query
    assert "consumer.task_id = work_item.task_id" in claim_query
    assert "work_item.control_mode = 'enforced'" in claim_query
    assert "work_item.page_capture_id = $4::uuid" in claim_query
    assert "cardinality($5::varchar[]) = 0" in claim_query
    assert connection.calls[0][2][4] == ["alioth_1"]
    assert any(
        call[0] == "execute" and "INSPECTION_ATTEMPT_LEASE_RECLAIMED" in call[1]
        for call in connection.calls
    )


async def test_terminal_web_owner_claim_is_fenced_to_its_active_session_work() -> None:
    """终态 main Run 的恢复权限不能放宽普通、H5/App 或跨 epoch 工作。"""
    connection = FakeConnection()
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    await repository.claim(
        command=ClaimInspectionWorkItemCommand(
            task_id="00000000-0000-0000-0000-000000000001",
            consumer_agent_run_id="00000000-0000-0000-0000-000000000003",
            lease_owner="web-active-session-recovery",
            lease_seconds=180,
            session_dependency="active_session",
        ),
        lease_token="00000000-0000-0000-0000-000000000020",
    )

    claim_query = connection.calls[0][1]
    assert "work_item.producer_agent_run_id = $3::uuid" in claim_query
    assert "work_item.session_owner_agent_run_id = $3::uuid" in claim_query
    assert "work_item.session_dependency = 'active_session'" in claim_query
    assert "work_item.finalization_epoch = task.finalization_epoch" in claim_query
    assert "task.finalization_agent_run_id = $3::uuid" in claim_query
    assert "task.target_platform = 'web'" in claim_query
    assert "task.task_config->>'workflow_version' = 'v2'" in claim_query
    assert "task.task_config->>'execution_mode' = 'full_inspection'" in claim_query
    assert "task.task_config->>'web_inspection_notification_mode' = 'enforced'" in claim_query
    assert "consumer.agent_type = 'main'" in claim_query
    assert "consumer.status IN ('done', 'failed', 'aborted')" in claim_query


async def test_completed_result_requires_exact_batch_and_formal_execution_links() -> None:
    connection = FakeConnection(
        fetch_result=[
            {
                "work_item_id": "00000000-0000-0000-0000-000000000010",
                "page_capture_id": "00000000-0000-0000-0000-000000000002",
                "rule_id": "00000000-0000-0000-0000-000000000005",
                "rule_code": "alioth_1",
                "execution_status": "pass",
                "observation_packet_id": "00000000-0000-0000-0000-000000000030",
                "rule_execution_id": "00000000-0000-0000-0000-000000000040",
            }
        ]
    )
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    result = await repository.find_completed_result(
        command=FindCompletedInspectionWorkItemCommand(
            task_id="00000000-0000-0000-0000-000000000001",
            page_capture_id="00000000-0000-0000-0000-000000000002",
            rule_codes=("alioth_1",),
        )
    )

    assert result is not None
    assert result.work_item_id.endswith("0010")
    assert result.links[0].rule_execution_id.endswith("0040")
    query = connection.calls[0][1]
    assert "work_item.status IN ('succeeded', 'partial')" in query
    assert "work_item.finalization_epoch = task.finalization_epoch" in query
    assert "member.rule_execution_id IS NULL" in query
    assert "= $3::varchar[]" in query
    assert connection.calls[0][2][2] == ["alioth_1"]


async def test_transition_requires_status_version_and_lease_token() -> None:
    connection = FakeConnection(fetchval_result=3)
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    updated = await repository.transition(
        command=TransitionInspectionWorkItemCommand(
            work_item_id="00000000-0000-0000-0000-000000000010",
            expected_status=InspectionWorkItemStatus.RUNNING,
            target_status=InspectionWorkItemStatus.SUCCEEDED,
            row_version=2,
            lease_token="00000000-0000-0000-0000-000000000020",
        )
    )

    query = connection.calls[0][1]
    assert updated == 3
    assert "AND status = $3" in query
    assert "AND row_version = $4" in query
    assert "lease_token = $5::uuid" in query
    assert "lease_expires_at > now()" in query
    assert "finished_at = CASE" in query
    assert "$2 <> 'succeeded'" in query
    assert "$2 <> 'partial'" in query


async def test_renew_lease_cannot_resurrect_expired_owner() -> None:
    connection = FakeConnection(fetchval_result=4)
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    row_version = await repository.renew_lease(
        command=RenewInspectionWorkItemLeaseCommand(
            work_item_id="00000000-0000-0000-0000-000000000010",
            consumer_agent_run_id="00000000-0000-0000-0000-000000000003",
            lease_token="00000000-0000-0000-0000-000000000020",
            row_version=3,
            lease_seconds=180,
        )
    )

    query = connection.calls[0][1]
    assert row_version == 4
    assert "lease_expires_at > now()" in query
    assert "consumer.status = 'running'" in query
    assert "AND row_version = $4" in query


async def test_renew_lease_terminal_web_owner_exception_is_fully_fenced() -> None:
    connection = FakeConnection(fetchval_result=4)
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    row_version = await repository.renew_lease(
        command=RenewInspectionWorkItemLeaseCommand(
            work_item_id="00000000-0000-0000-0000-000000000010",
            consumer_agent_run_id="00000000-0000-0000-0000-000000000003",
            lease_token="00000000-0000-0000-0000-000000000020",
            row_version=3,
            lease_seconds=180,
            allow_terminal_web_owner_recovery=True,
        )
    )

    query = connection.calls[0][1]
    assert row_version == 4
    assert "qc_inspection_work_item.session_dependency = 'active_session'" in query
    assert "qc_inspection_work_item.producer_agent_run_id = $2::uuid" in query
    assert "qc_inspection_work_item.session_owner_agent_run_id = $2::uuid" in query
    assert "qc_inspection_work_item.finalization_epoch = task.finalization_epoch" in query
    assert "task.finalization_agent_run_id = $2::uuid" in query
    assert "task.target_platform = 'web'" in query
    assert "task.task_config->>'workflow_version' = 'v2'" in query
    assert "task.task_config->>'execution_mode' = 'full_inspection'" in query
    assert "task.task_config->>'web_inspection_notification_mode' = 'enforced'" in query
    assert "consumer.agent_type = 'main'" in query
    assert "consumer.status IN ('done', 'failed', 'aborted')" in query


async def test_rule_transition_checks_packet_and_execution_ownership() -> None:
    connection = FakeConnection(fetchval_result="00000000-0000-0000-0000-000000000011")
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    updated = await repository.transition_rule(
        command=TransitionInspectionWorkItemRuleCommand(
            work_item_id="00000000-0000-0000-0000-000000000010",
            rule_id="00000000-0000-0000-0000-000000000005",
            expected_status=InspectionWorkItemRuleStatus.JUDGED,
            target_status=InspectionWorkItemRuleStatus.WRITTEN,
            lease_token="00000000-0000-0000-0000-000000000020",
            observation_packet_id="00000000-0000-0000-0000-000000000030",
            rule_execution_id="00000000-0000-0000-0000-000000000040",
        )
    )

    query = connection.calls[0][1]
    assert updated is True
    assert "packet.task_id = work_item.task_id" in query
    assert "packet.page_capture_id = work_item.page_capture_id" in query
    assert "packet.created_by_agent_run_id = work_item.consumer_agent_run_id" in query
    assert "execution.rule_id = member.rule_id" in query
    assert "execution.agent_run_id = work_item.consumer_agent_run_id" in query
    assert "work_item.lease_expires_at > now()" in query


async def test_attempt_transition_requires_current_unexpired_lease() -> None:
    connection = FakeConnection(fetchval_result="00000000-0000-0000-0000-000000000050")
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    updated = await repository.transition_attempt(
        command=TransitionInspectionAttemptCommand(
            attempt_id="00000000-0000-0000-0000-000000000050",
            expected_status=InspectionAttemptStatus.RUNNING,
            target_status=InspectionAttemptStatus.JUDGED,
            lease_token="00000000-0000-0000-0000-000000000020",
            result_payload={"execution_status": "pass"},
        )
    )

    query = connection.calls[0][1]
    assert updated is True
    assert "attempt.lease_token = $7::uuid" in query
    assert "work_item.lease_token = $7::uuid" in query
    assert "work_item.lease_expires_at > now()" in query


async def test_nonterminal_gate_reads_persisted_control_plane() -> None:
    connection = FakeConnection(fetchval_result=True)
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    assert (
        await repository.has_nonterminal_work(task_id="00000000-0000-0000-0000-000000000001")
        is True
    )
    assert "status NOT IN" in connection.calls[0][1]
    assert "control_mode = 'enforced'" in connection.calls[0][1]


async def test_sync_formal_results_requires_live_enforced_lease() -> None:
    connection = FakeConnection(
        fetchrow_result={
            "expected_rule_count": 1,
            "terminal_rule_count": 1,
            "pending_rule_count": 0,
            "written_rule_count": 1,
        },
        fetch_result=[
            {
                "rule_id": "00000000-0000-0000-0000-000000000005",
                "rule_code": "alioth_1",
                "execution_status": "pass",
                "observation_packet_id": "00000000-0000-0000-0000-000000000030",
                "rule_execution_id": "00000000-0000-0000-0000-000000000040",
            }
        ],
    )
    repository = PostgresInspectionWorkItemRepository(pool=FakePool(connection))

    result = await repository.sync_formal_results(
        work_item_id="00000000-0000-0000-0000-000000000010",
        lease_token="00000000-0000-0000-0000-000000000020",
    )

    # 成员关联前会先读本次 Attempt 的补采绑定，按语句结构定位成员更新
    update_query = next(
        call[1]
        for call in connection.calls
        if call[0] == "execute" and "UPDATE qc_inspection_work_item_rule" in call[1]
    )
    assert "work_item.control_mode = 'enforced'" in update_query
    assert "work_item.lease_token = $2::uuid" in update_query
    assert "work_item.lease_expires_at > now()" in update_query
    assert "execution.agent_run_id = work_item.consumer_agent_run_id" in update_query
    assert "packet.created_by_agent_run_id = work_item.consumer_agent_run_id" in update_query
    assert result.pending_rule_count == 0
    assert result.terminal_status == InspectionWorkItemStatus.SUCCEEDED
    assert result.links[0].rule_execution_id.endswith("0040")
