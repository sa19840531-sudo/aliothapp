import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.task_progress import whole_task_progress_percent
from services.api.app.application.workstation_tasks import (
    WorkstationTaskListFilter,
    available_actions_for_status,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import TaskExecutionMode, TaskTargetPlatform
from services.api.app.infrastructure.db import workstation_query_repository
from services.api.app.infrastructure.db.app_report_availability_sql import (
    APP_FORMAL_REPORT_AVAILABLE_SQL,
    APP_REPORT_PREVIEW_AVAILABLE_SQL,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    _FORMAL_REPORT_AVAILABLE_SQL,
    _H5_V2_FORMAL_REPORT_AVAILABLE_SQL,
    _H5_V2_REPORT_PREVIEW_AVAILABLE_SQL,
    _REPORT_AVAILABILITY_SQL,
    _WEB_V2_FORMAL_REPORT_AVAILABLE_SQL,
    PostgresWorkstationQueryRepository,
    _execution_mode_from_row,
    _task_list_item_from_row,
    _web_stage_durations_from_row,
)
from services.api.app.infrastructure.db.workstation_task_list_sql import task_list_query_sql

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any", "taskObjects:write:any"}),
)


class FakeAcquire:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    async def __aenter__(self) -> "FakeConnection":
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.task_status = "queued"
        self.task_row_version = 2
        self.transaction_entered = False
        self.transaction_options: list[dict[str, Any]] = []
        self.executed_statements: list[str] = []
        self.operation_order: list[str] = []

    def transaction(self, *, readonly: bool = False) -> FakeAcquire:
        self.transaction_entered = True
        self.transaction_options.append({"readonly": readonly})
        return FakeAcquire(self)

    async def execute(self, query: str, *args: Any) -> str:
        compact = " ".join(query.split())
        self.executed_statements.append(compact)
        self.operation_order.append(compact)
        return "SET"

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        self.operation_order.append("fetch")
        self.calls.append((" ".join(query.split()), args))
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if "row_version" in compact and compact.startswith("SELECT"):
            return {
                "task_id": str(args[0]),
                "status": self.task_status,
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        if compact.startswith("UPDATE qc_task"):
            self.task_status = str(args[3])
            self.task_row_version += 1
            return {
                "task_id": str(args[0]),
                "status": self.task_status,
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        return None

    async def fetchval(self, query: str, *args: Any) -> str:
        self.calls.append((" ".join(query.split()), args))
        return "run-1"


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _h5_exploration_row() -> dict[str, Any]:
    return {
        "task_id": "task-h5",
        "task_name": "H5 页面探索",
        "product": {
            "department_name": "质量保障部",
            "product_code": "H5-001",
            "product_name": "移动站点",
        },
        "target_url": "https://example.test",
        "target_platform": "h5",
        "execution_mode": "exploration_only",
        "journey_steps": [],
        "status": "done",
        "total_rules": 0,
        "finished_rules": 0,
        "last_error": None,
        "created_at": "2026-08-13T00:00:00Z",
    }


def _formal_list_row(
    *,
    platform: str = "web",
    workflow_version: str = "v2",
    report_available: bool = False,
    report_availability: str | None = None,
) -> dict[str, Any]:
    return {
        **_h5_exploration_row(),
        "task_id": "task-formal",
        "task_name": "正式质检",
        "target_platform": platform,
        "execution_mode": "full_inspection",
        "workflow_version": workflow_version,
        "status": "done",
        "total_rules": 2,
        "finished_rules": 2,
        "report_available": report_available,
        "report_availability": report_availability or ("final" if report_available else "none"),
    }


def test_workstation_row_mapper_preserves_formal_platform_and_execution_mode() -> None:
    item = _task_list_item_from_row(_h5_exploration_row())

    assert item.target_platform is TaskTargetPlatform.H5
    assert item.execution_mode is TaskExecutionMode.EXPLORATION_ONLY
    assert item.report_status == "not_applicable"


@pytest.mark.parametrize("status", ["review", "done", "failed", "canceled"])
@pytest.mark.parametrize("platform", ["app", "h5", "web"])
def test_task_progress_reaches_end_without_completing_rule_checks(
    status: str, platform: str,
) -> None:
    row = {
        **_formal_list_row(platform=platform),
        "status": status,
        "total_rules": 208,
        "finished_rules": 116,
    }
    assert _task_list_item_from_row(row).progress == 100
    row["finished_rules"] = 0
    assert _task_list_item_from_row(row).progress == 100


@pytest.mark.parametrize("run_budget", [
    {"max_duration_seconds": 3600},
    {"timeout_minutes": 60},
    {"max_duration_seconds": "bad"},
    {},
])
def test_running_task_progress_uses_frozen_run_budget_and_keeps_finalization_below_complete(
    monkeypatch: pytest.MonkeyPatch,
    run_budget: dict[str, Any],
) -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        workstation_query_repository,
        "whole_task_progress_percent",
        lambda status, **kwargs: whole_task_progress_percent(status, now=now, **kwargs),
    )
    row = {
        **_formal_list_row(),
        "status": "running",
        "started_at": now - timedelta(seconds=1800),
        "run_status": "running",
        "progress_task_loop_guard": {"max_duration_seconds": 1800, "max_steps": 10},
        "progress_run_loop_guard": run_budget,
        "total_rules": 208,
        "finished_rules": 116,
    }
    expected = whole_task_progress_percent(
        "running", started_at=row["started_at"],
        loop_guard=run_budget or row["progress_task_loop_guard"],
        run_status="running", now=now,
    )
    assert _task_list_item_from_row(row).progress == expected
    row.update(total_rules=0, finished_rules=0)
    assert _task_list_item_from_row(row).progress == expected
    row.update(run_status="failed", finished_at=now)
    assert _task_list_item_from_row(row).progress == 99
    settled_at = now - timedelta(minutes=5)
    row.update(finished_at=None, rules_settled=True, rules_settled_at=settled_at)
    settled = _task_list_item_from_row(row)
    assert settled.progress == 100
    assert settled.execution_finished_at == settled_at
    row.update(finished_at=now)
    assert _task_list_item_from_row(row).execution_finished_at == settled_at
    row.update(rules_settled_at=now + timedelta(minutes=1))
    assert _task_list_item_from_row(row).execution_finished_at == now


@pytest.mark.parametrize("report_status_filter", [False, True])
def test_task_list_reads_existing_run_budget_for_loading_progress(
    report_status_filter: bool,
) -> None:
    query = task_list_query_sql(
        report_status_filter=report_status_filter,
        report_availability_sql="'none'",
        report_status_sql="'not_started'",
    )
    assert "task.loop_guard AS progress_task_loop_guard" in query
    assert "agent_run.loop_guard AS progress_run_loop_guard" in query
    assert "agent_run.status AS run_status" in query


async def test_task_detail_reads_same_budget_projection_as_list() -> None:
    pool = FakePool()
    repository = PostgresWorkstationQueryRepository(pool=pool)
    assert await repository.get_task_detail("task-1", access_scope=TEST_ACCESS_SCOPE) is None
    query, _ = pool.connection.calls[0]
    assert "task.loop_guard AS progress_task_loop_guard" in query
    assert "agent_run.loop_guard AS progress_run_loop_guard" in query
    assert "SELECT id, status, loop_guard, output_context, error_message" in query


@pytest.mark.parametrize("platform", ["app", "h5", "web"])
@pytest.mark.parametrize("status", ["done", "review", "failed", "canceled"])
def test_repeated_admin_reads_do_not_accumulate_or_leak_delete_actions(
    platform: str, status: str,
) -> None:
    row = {**_formal_list_row(platform=platform), "status": status}
    baseline = available_actions_for_status(status)
    first = _task_list_item_from_row(row, can_delete_tasks=True)

    # 模拟同进程连续读取多行及反复刷新，已生成的响应也不能被后续请求改写。
    for _ in range(20):
        admin = _task_list_item_from_row(row, can_delete_tasks=True)
        regular = _task_list_item_from_row(row, can_delete_tasks=False)
        assert admin.available_actions.count("delete") == 1
        assert "delete" not in regular.available_actions
        assert len(admin.available_actions) == len(set(admin.available_actions))
    assert first.available_actions.count("delete") == 1
    assert available_actions_for_status(status) == baseline
    assert "delete" not in baseline


def test_workstation_row_mapper_hides_provider_account_from_failure_reason() -> None:
    row = {
        **_formal_list_row(),
        "status": "failed",
        "last_error": (
            "Error code: 402 - {'error': {'message': 'Insufficient Balance', "
            "'wallet_account': 'acct-sensitive-123'}}"
        ),
    }

    item = _task_list_item_from_row(row)

    assert item.failure_reason == "模型服务余额不足，请补充额度后重试"
    assert "acct-sensitive-123" not in (item.failure_reason or "")


def test_workstation_row_mapper_hides_unknown_external_error_payload() -> None:
    row = {
        **_formal_list_row(),
        "status": "failed",
        "last_error": "Error code: 400 - {'token': 'secret-value'}",
    }

    item = _task_list_item_from_row(row)

    assert item.failure_reason == "任务执行失败，请查看服务端审计日志"
    assert "secret-value" not in (item.failure_reason or "")


def test_workstation_row_mapper_preserves_safe_application_failure_reason() -> None:
    row = {
        **_formal_list_row(),
        "status": "failed",
        "last_error": "Worker 心跳超时，运行已安全终止，请重新执行任务",
    }

    item = _task_list_item_from_row(row)

    assert item.failure_reason == "Worker 心跳超时，运行已安全终止，请重新执行任务"


def test_workstation_execution_mode_mapper_uses_legacy_default_only_for_none() -> None:
    assert _execution_mode_from_row({"execution_mode": None}) is TaskExecutionMode.FULL_INSPECTION
    assert (
        _execution_mode_from_row({"execution_mode": "exploration_only"})
        is TaskExecutionMode.EXPLORATION_ONLY
    )


def test_workstation_execution_mode_mapper_rejects_unknown_nonempty_value() -> None:
    with pytest.raises(ValueError, match="execution_mode 不受支持: report_anyway"):
        _task_list_item_from_row({**_h5_exploration_row(), "execution_mode": "report_anyway"})


@pytest.mark.parametrize(
    ("report_available", "expected_status", "has_report_action"),
    [
        pytest.param(False, "generating", False, id="blocked"),
        pytest.param(False, "generating", False, id="unbound"),
        pytest.param(False, "generating", False, id="stale"),
        pytest.param(True, "ready", True, id="current"),
    ],
)
def test_web_v2_formal_list_uses_authoritative_report_availability(
    report_available: bool,
    expected_status: str,
    has_report_action: bool,
) -> None:
    item = _task_list_item_from_row(_formal_list_row(report_available=report_available))

    assert item.report_status == expected_status
    assert ("report" in item.available_actions) is has_report_action


def test_web_v2_formal_list_exposes_preview_report_action() -> None:
    item = _task_list_item_from_row(
        _formal_list_row(
            report_available=False,
            report_availability="preview",
        )
    )

    assert item.report_availability == "preview"
    assert item.report_status == "ready"
    assert "report" in item.available_actions


def test_h5_review_list_exposes_persisted_report_as_preview() -> None:
    item = _task_list_item_from_row(
        {
            **_formal_list_row(
                platform="h5",
                report_available=False,
                report_availability="preview",
            ),
            "status": "review",
        }
    )

    assert item.report_availability == "preview"
    assert item.report_status == "ready"
    assert "report" in item.available_actions


def test_h5_review_without_persisted_report_does_not_expose_report_action() -> None:
    item = _task_list_item_from_row(
        {
            **_formal_list_row(
                platform="h5",
                report_available=False,
                report_availability="unavailable",
            ),
            "status": "review",
        }
    )

    assert item.report_status == "failed"
    assert "report" not in item.available_actions


def test_h5_v2_report_availability_keeps_preview_and_final_authorities_separate() -> None:
    assert "task.target_platform = 'h5'" in _H5_V2_REPORT_PREVIEW_AVAILABLE_SQL
    assert "workflow_version" in _H5_V2_REPORT_PREVIEW_AVAILABLE_SQL
    assert "execution_mode' = 'full_inspection'" in _H5_V2_REPORT_PREVIEW_AVAILABLE_SQL
    assert "task.finalization_agent_run_id = agent_run.id" in (
        _H5_V2_REPORT_PREVIEW_AVAILABLE_SQL
    )
    assert "agent_run.output_context" in _H5_V2_REPORT_PREVIEW_AVAILABLE_SQL
    assert "qc_report_gate_binding" not in _H5_V2_REPORT_PREVIEW_AVAILABLE_SQL

    assert "qc_report_gate_binding" in _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    assert "qc_report_payload_snapshot" in _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    assert "task.completion_gate_snapshot_id = gate.gate_snapshot_id" in (
        _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    )
    assert "task.finalization_agent_run_id = agent_run.id" in (
        _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    )
    assert "task.finalization_recovery_state = 'verified'" not in (
        _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    )
    assert "payload.report_payload->>'report_id'" in _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    assert "payload.report_payload->>'report_binding_version'" in (
        _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    )
    assert "payload.report_payload->>'is_final' = 'true'" in (
        _H5_V2_FORMAL_REPORT_AVAILABLE_SQL
    )
    assert "WHEN" in _REPORT_AVAILABILITY_SQL
    assert "THEN 'preview'" in _REPORT_AVAILABILITY_SQL
    assert _WEB_V2_FORMAL_REPORT_AVAILABLE_SQL in _FORMAL_REPORT_AVAILABLE_SQL
    assert _H5_V2_FORMAL_REPORT_AVAILABLE_SQL in _FORMAL_REPORT_AVAILABLE_SQL


def test_app_report_preview_and_formal_are_wired_into_list_availability() -> None:
    assert APP_FORMAL_REPORT_AVAILABLE_SQL in _REPORT_AVAILABILITY_SQL
    assert APP_REPORT_PREVIEW_AVAILABLE_SQL in _REPORT_AVAILABILITY_SQL
    assert _REPORT_AVAILABILITY_SQL.index(APP_FORMAL_REPORT_AVAILABLE_SQL) < (
        _REPORT_AVAILABILITY_SQL.index(APP_REPORT_PREVIEW_AVAILABLE_SQL)
    )


def test_app_report_availability_is_not_changed_by_h5_preview_contract() -> None:
    item = _task_list_item_from_row(
        _formal_list_row(
            platform="app",
            report_available=False,
            report_availability="unavailable",
        )
    )

    assert item.report_availability == "unavailable"


def test_only_admin_sees_retry_action_for_quarantined_finalization() -> None:
    row = {
        **_formal_list_row(report_availability="preview"),
        "status": "review",
        "finalization_recovery_state": "quarantined",
        "web_inspection_notification_mode": "enforced",
    }

    admin_item = _task_list_item_from_row(row, can_retry_quarantine=True)
    regular_item = _task_list_item_from_row(row, can_retry_quarantine=False)

    assert "retry-finalization" in admin_item.available_actions
    assert "retry-finalization" not in regular_item.available_actions

    non_enforced = {**row, "web_inspection_notification_mode": "off"}
    assert (
        "retry-finalization"
        not in _task_list_item_from_row(
            non_enforced,
            can_retry_quarantine=True,
        ).available_actions
    )


def test_formal_report_availability_uses_only_immutable_binding_identity() -> None:
    sql = _WEB_V2_FORMAL_REPORT_AVAILABLE_SQL

    assert "task.task_config->>'web_inspection_notification_mode' = 'enforced'" in sql
    assert "task.finalization_state = 'sealed'" in sql
    assert "task.finalization_recovery_state = 'verified'" in sql
    assert "task.finalization_agent_run_id = coverage.agent_run_id" in sql
    assert "task.completion_gate_snapshot_id = gate.gate_snapshot_id" in sql
    assert "agent_run.id" not in sql
    assert "payload.report_version = binding.report_version" in sql
    assert "payload.report_payload_hash = binding.report_payload_hash" in sql
    assert "'{completion_gate,gate_passed}' = 'true'" in sql
    assert "('task:' || task.id::text || ':run:'" in sql
    assert "coverage.applicability_snapshot IS NOT NULL" in sql
    assert "= coverage.applicability_snapshot" in sql


@pytest.mark.parametrize(
    ("task_status", "expected_report_status"),
    [
        pytest.param("canceled", "not_started", id="canceled"),
        pytest.param("review", "failed", id="review"),
    ],
)
@pytest.mark.parametrize("platform", ["h5", "web"])
def test_v2_formal_terminal_without_report_never_projects_generating(
    task_status: str,
    expected_report_status: str,
    platform: str,
) -> None:
    item = _task_list_item_from_row(
        {
            **_formal_list_row(platform=platform, report_available=False),
            "status": task_status,
        }
    )

    assert item.report_status == expected_report_status
    assert "report" not in item.available_actions


@pytest.mark.parametrize("platform", ["h5", "web"])
@pytest.mark.parametrize("status", ["canceled", "failed"])
@pytest.mark.parametrize("availability", ["preview", "final"])
def test_v2_terminal_with_actual_report_keeps_ready_and_report_action(
    platform: str, status: str, availability: str,
) -> None:
    item = _task_list_item_from_row({
        **_formal_list_row(
            platform=platform, report_available=False, report_availability=availability,
        ),
        "status": status,
    })

    assert item.report_status == "ready"
    assert item.report_availability == availability
    assert "report" in item.available_actions


@pytest.mark.parametrize("platform", ["h5", "web"])
def test_failed_v2_without_report_keeps_failed_projection(platform: str) -> None:
    item = _task_list_item_from_row({
        **_formal_list_row(platform=platform, report_available=False), "status": "failed",
    })

    assert item.report_status == "failed"
    assert "report" not in item.available_actions


@pytest.mark.parametrize("platform,workflow_version", [("web", "v1")])
def test_non_web_v2_formal_list_keeps_legacy_report_projection(
    platform: str,
    workflow_version: str,
) -> None:
    item = _task_list_item_from_row(
        _formal_list_row(
            platform=platform,
            workflow_version=workflow_version,
            report_available=False,
        )
    )

    assert item.report_status == "ready"
    assert "report" in item.available_actions


@pytest.mark.parametrize("report_status", [None, "ready"])
async def test_task_list_disables_jit_only_inside_its_readonly_transaction(
    report_status: str | None,
) -> None:
    pool = FakePool()
    repository = PostgresWorkstationQueryRepository(pool=pool)

    await repository.list_tasks(
        WorkstationTaskListFilter(report_status=report_status),
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert pool.connection.transaction_options == [{"readonly": True}]
    assert pool.connection.executed_statements == ["SET LOCAL jit = off"]
    assert pool.connection.operation_order == ["SET LOCAL jit = off", "fetch"]
    query, _args = pool.connection.calls[0]
    # 列表已有报告可用性，不能重复校验正式绑定；详情与旧行兼容仍保留原字段。
    assert "AS report_available," not in query
    assert "AS report_availability," in query
    assert query.count("FROM qc_report_gate_binding AS binding") == 3


async def test_task_list_counts_business_results_without_superseded_placeholders() -> None:
    pool = FakePool()
    repository = PostgresWorkstationQueryRepository(pool=pool)

    await repository.list_tasks(
        WorkstationTaskListFilter(),
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[0]
    assert query.index("LIMIT $8 OFFSET $9") < query.index("replacement_facts AS MATERIALIZED")
    assert "execution.agent_run_id = task.run_uuid" in query
    assert "capture.agent_run_id = task.run_uuid" in query
    assert "execution.task_id = task.task_uuid" in query
    assert "concluded.task_id = facts.task_id" in query
    assert "SELECT execution.*" not in query
    assert "COUNT(*) FILTER (WHERE execution.is_completed)" in query
    assert "FROM qc_rule_execution AS concluded" not in query.split(
        "replacement_facts AS MATERIALIZED", 1,
    )[1]
    assert "agent_run.output_context->>'cancel_requested'" in query
    assert "NULLIF(task.last_error, '')" in query
    assert "agent_run.error_message" in query
    assert "AS last_error" in query
    assert "SELECT id, status, loop_guard, output_context, error_message" in query
    assert "THEN 'canceling'" in query
    assert "task.target_platform" in query
    assert "task.task_config->>'execution_mode' AS execution_mode" in query
    assert "task.task_config->>'workflow_version' AS workflow_version" in query
    assert "payload.report_payload #>> '{task,task_id}' = task.id::text" in query
    assert "coverage.agent_run_id::text" in query
    assert "payload.report_payload #>> '{completion_gate,gate_snapshot_id}'" in query
    assert "payload.report_payload #>> '{completion_gate,finalization_epoch}'" in query
    assert "task.target_platform = 'h5'" in query
    assert "FROM qc_page_capture AS preview_capture" in query
    assert "FROM qc_rule_execution AS preview_execution" in query
    assert "preview_action.agent_run_id" not in query
    assert "FROM qc_web_coverage_snapshot AS preview_coverage" in query
    assert "qc_web_exploration_budget AS preview_budget" not in query
    assert args[7:9] == (20, 0)
    assert args[9:] == (True, "tenant-1", True, "test-user", None, None, None, None)


async def test_workstation_task_list_keeps_report_status_filter_on_aggregated_query() -> None:
    pool = FakePool()
    repository = PostgresWorkstationQueryRepository(pool=pool)

    await repository.list_tasks(
        WorkstationTaskListFilter(report_status="ready"),
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[0]
    # 报告资格须在分页前检查存在性，完整计数和批量占位替代只处理当前页。
    assert "LEFT JOIN LATERAL (" in query
    assert "THEN EXISTS ( SELECT 1 FROM qc_rule_execution AS execution" in query
    assert "execution.agent_run_id = agent_run.id" in query
    assert "COALESCE(execution_stats.total_rules, 0)::int AS total_rules" in query
    assert "GROUP BY task.id, ruleset.id, agent_run.id" not in query
    assert query.index("AND has_effective_rules THEN 'ready'") < query.index("LIMIT $9 OFFSET $10")
    assert query.index("LIMIT $9 OFFSET $10") < query.index("replacement_facts AS MATERIALIZED")
    assert "FROM effective_executions AS execution GROUP BY execution.task_id" in query
    assert "execution_mode = 'exploration_only'" in query
    assert "THEN 'not_applicable'" in query
    assert "payload.report_payload #>> '{task,task_id}' = task.id::text" in query
    assert "coverage.agent_run_id::text" in query
    assert "payload.report_payload #>> '{completion_gate,gate_snapshot_id}'" in query
    assert "payload.report_payload #>> '{completion_gate,finalization_epoch}'" in query
    assert "WHEN report_availability IN ('preview', 'final') THEN 'ready'" in query
    assert "WHEN status = 'canceled' THEN 'not_started'" in query
    assert "WHEN status = 'canceled' THEN 'not_applicable'" not in query
    assert "WHEN status = 'review' THEN 'failed'" in query
    assert args[7] == "ready"
    assert args[10:] == (True, "tenant-1", True, "test-user", None, None, None, None)


async def test_workstation_task_detail_counts_only_latest_run_results() -> None:
    pool = FakePool()
    repository = PostgresWorkstationQueryRepository(pool=pool)

    result = await repository.get_task_detail(
        "task-1",
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[0]
    assert "execution.agent_run_id = agent_run.id" in query
    assert "agent_run.output_context->>'cancel_requested'" in query
    assert "NULLIF(task.last_error, '')" in query
    assert "agent_run.error_message" in query
    assert "AS last_error" in query
    assert "queued_ahead" not in query
    assert "AS tasks_ahead" not in query
    assert "SELECT id, status, loop_guard, output_context, error_message" in query
    assert "task.target_platform" in query
    assert "task.task_config->>'execution_mode' AS execution_mode" in query
    assert args == ("task-1", True, "tenant-1", True, "test-user", None)
    assert "task.task_config->>'workflow_version' AS workflow_version" in query
    assert "browser_profile_snapshot,profile_id" in query
    assert "browser_profile_snapshot,viewport_width" in query
    assert "browser_profile_snapshot,viewport_height" in query
    assert "payload.report_payload #>> '{task,task_id}' = task.id::text" in query
    assert "coverage.agent_run_id::text" in query
    assert "payload.report_payload #>> '{completion_gate,gate_snapshot_id}'" in query
    assert "payload.report_payload #>> '{completion_gate,finalization_epoch}'" in query
    assert result is None


async def test_web_stage_duration_query_uses_current_run_epoch_and_auditable_anchors() -> None:
    pool = FakePool()
    repository = PostgresWorkstationQueryRepository(pool=pool)

    result = await repository.get_web_stage_durations(
        "00000000-0000-0000-0000-000000000001",
        agent_run_id="00000000-0000-0000-0000-000000000002",
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[0]
    assert result is None
    assert "agent_run.finished_at AS exploration_finished_at" in query
    assert "FROM qc_h5_inspection_publication AS publication" in query
    assert "FROM qc_inspection_work_item AS work" in query
    assert "FROM qc_rule_execution AS execution" in query
    assert "FROM qc_aggregate_inspection_work_item AS aggregate_work" in query
    assert "FROM qc_aggregate_inspection_candidate AS aggregate_candidate" in query
    assert "aggregate_candidate.source_agent_run_id = agent_run.id" in query
    assert "THEN MAX(candidate.finished_at)" in query
    assert "BOOL_AND(candidate.is_terminal)" in query
    assert "publication.mode = 'enforced'" in query
    assert "work.control_mode = 'enforced'" in query
    assert "aggregate_work.control_mode = 'enforced'" in query
    assert "member.rule_execution_id IS NULL" in query
    assert "execution.aggregate_work_item_id IS NULL" in query
    assert "FROM qc_evidence_request AS request" in query
    assert "request.session_dependency = 'active_session'" in query
    assert "request.status IN ('pending', 'routed', 'running')" in query
    assert "request.finalization_epoch = task.finalization_epoch" in query
    assert "gate.evaluated_at AS inspection_finished_at" not in query
    assert "FROM qc_web_report_generation_attempt AS attempt" in query
    assert "attempt.started_at" in query
    assert "attempt.finished_at" in query
    assert "binding.bound_at IS NOT NULL" in query
    assert "attempt.status = 'running'" in query
    assert "FROM qc_report_gate_binding AS report_binding" in query
    assert "payload.report_payload->>'agent_run_id' = agent_run.id::text" in query
    assert "attempt.finalization_epoch = task.finalization_epoch" in query
    assert args == (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        True,
        "tenant-1",
        True,
        "test-user",
        None,
    )


def test_web_stage_duration_projection_uses_null_for_unfinished_and_clamps_clock_drift() -> None:
    started_at = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    durations = _web_stage_durations_from_row(
        {
            "task_created_at": started_at,
            "exploration_started_at": started_at + timedelta(seconds=5),
            "exploration_finished_at": started_at + timedelta(seconds=125),
            "inspection_started_at": started_at + timedelta(seconds=35),
            "inspection_finished_at": started_at + timedelta(seconds=20),
            "report_generation_started_at": started_at + timedelta(seconds=30),
            "report_generation_finished_at": None,
            "report_finished_at": None,
        }
    )

    assert durations.to_dict() == {
        "exploration_ms": 120_000,
        "inspection_ms": 0,
        "report_generation_ms": None,
        "total_ms": None,
    }


def test_web_stage_duration_projection_uses_independent_persisted_stage_boundaries() -> None:
    task_created_at = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    durations = _web_stage_durations_from_row(
        {
            "task_created_at": task_created_at,
            "exploration_started_at": task_created_at + timedelta(minutes=1),
            "exploration_finished_at": task_created_at + timedelta(minutes=6),
            "inspection_started_at": task_created_at + timedelta(minutes=2),
            "inspection_finished_at": task_created_at + timedelta(minutes=5),
            "report_generation_started_at": task_created_at + timedelta(minutes=6, seconds=30),
            "report_generation_finished_at": task_created_at + timedelta(minutes=8),
            "report_finished_at": task_created_at + timedelta(minutes=10),
        }
    )

    assert durations.to_dict() == {
        "exploration_ms": 300_000,
        "inspection_ms": 180_000,
        "report_generation_ms": 90_000,
        "total_ms": 600_000,
    }


async def test_update_task_status_uses_shared_versioned_transition() -> None:
    pool = FakePool()
    repository = PostgresWorkstationQueryRepository(pool=pool)

    result = await repository.update_task_status(
        task_id="task-1",
        status="canceled",
        allowed_statuses=["queued", "running"],
        access_scope=TEST_ACCESS_SCOPE,
    )

    queries = [query for query, _args in pool.connection.calls]
    task_update_query, task_update_args = next(
        call for call in pool.connection.calls if call[0].startswith("UPDATE qc_task")
    )
    assert pool.connection.transaction_entered is True
    assert any(query.endswith("FOR UPDATE") for query in queries)
    assert "row_version = $3" in task_update_query
    assert "finalization_state <> 'sealed'" in task_update_query
    assert task_update_args[:4] == ("task-1", "queued", 2, "canceled")
    assert result is not None
    assert result.task_id == "task-1"
    assert result.status == "canceled"
    assert result.run_id == "run-1"


def test_workstation_queries_keep_main_run_authority_when_judge_run_is_newer() -> None:
    source = inspect.getsource(PostgresWorkstationQueryRepository)

    assert source.count("WHEN id = task.finalization_agent_run_id THEN 0") == 2
    assert source.count("WHEN agent_type = 'main' THEN 1") == 2
    for report_filter in (False, True):
        query = task_list_query_sql(
            report_status_filter=report_filter,
            report_availability_sql=_REPORT_AVAILABILITY_SQL,
            report_status_sql="total_rules > 0",
        )
        assert "WHEN id = task.finalization_agent_run_id THEN 0" in query
        assert "WHEN agent_type = 'main' THEN 1" in query
