import json
from datetime import UTC, datetime

import pytest

from services.api.app.application.rule_execution import (
    CandidatePageReference,
    RecordRuleExecutionCommand,
    RuleExecutionValidationError,
    RuntimePageInspectionFailureFallback,
    RuntimeRuleExecutionFallback,
)
from services.api.app.application.task_submission import TaskRulesetNotFoundError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import AppTaskConfig, TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


class FakeAcquire:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    async def __aenter__(self) -> "FakeConnection":
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, row: object | None, *, rows: list[dict] | None = None) -> None:
        self.connection = FakeConnection(row, rows=rows or [])

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self, row: object | None, *, rows: list[dict]) -> None:
        self.row = row
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []
        self.inventory_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        if "SELECT task.target_platform, task.channel, task.task_config" in query:
            self.inventory_calls.append((" ".join(query.split()), args))
            return None
        self.calls.append((" ".join(query.split()), args))
        if "AS frozen_rule" in query:
            return {
                "ruleset_id": "ruleset-1", "ruleset_version": 1,
                "ruleset_snapshot_hash": "snapshot-1", "rules": [],
            }
        return self.row

    async def fetch(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.rows

    async def fetchval(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.row

    async def execute(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return "INSERT 0 2"

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()


async def test_postgres_task_repository_creates_queued_task_for_enabled_ruleset() -> None:
    row = {
        "task_id": "task-1",
        "ruleset_id": "ruleset-1",
        "target_platform": "h5",
        "target_url": "https://example.com/h5",
        "status": "queued",
        "task_config": {"browser": "chromium"},
        "page_sampling_policy": {"mode": "first_n", "max_pages": 3},
        "loop_guard": {"max_steps": 20},
    }
    pool = FakePool(row)
    repository = PostgresTaskRepository(pool=pool)

    task = await repository.create_task(
        access_scope=TEST_ACCESS_SCOPE,
        target=TaskTarget(
            platform=TaskTargetPlatform.H5,
            target_url="https://example.com/h5",
        ),
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=80,
        task_config={"browser": "chromium"},
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
        loop_guard={"max_steps": 20},
    )

    query, args = pool.connection.calls[0]
    assert "INSERT INTO qc_task" in query
    assert "status = 'enabled'" in query
    assert "page_sampling_policy || $8::jsonb" in query
    assert "jsonb_array_elements_text( rule.applicable_page_types )" in query
    assert "'rule_target_page_types', COALESCE(" in query
    assert "$7::jsonb #> '{rule_inventory,target_page_types}'" in query
    assert "jsonb_strip_nulls(loop_guard || $9::jsonb)" in query
    assert args[0] == "alioth_first_ruleset"
    assert args[1] == 1
    assert args[2] == "h5"
    assert args[3] == "https://example.com/h5"
    assert args[4] == "workbench"
    assert args[5] == 80
    assert '"browser": "chromium"' in args[6]
    assert '"mode": "first_n"' in args[7]
    assert '"max_steps": 20' in args[8]
    assert args[9:12] == (
        "test-user",
        "tenant-1",
        False,
    )
    assert task.task_id == "task-1"
    assert task.ruleset_id == "ruleset-1"
    assert task.status == "queued"
    assert task.task_config == {"browser": "chromium"}
    assert task.page_sampling_policy == {"mode": "first_n", "max_pages": 3}
    assert task.loop_guard == {"max_steps": 20}


async def test_postgres_task_repository_raises_when_enabled_ruleset_missing() -> None:
    repository = PostgresTaskRepository(pool=FakePool(None))

    with pytest.raises(TaskRulesetNotFoundError):
        await repository.create_task(
            access_scope=TEST_ACCESS_SCOPE,
            target=TaskTarget(
                platform=TaskTargetPlatform.H5,
                target_url="https://example.com/h5",
            ),
            ruleset_code="missing",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            task_config={},
            page_sampling_policy={},
            loop_guard={},
        )


async def test_postgres_task_repository_requires_verified_build_for_app_target() -> None:
    row = {
        "task_id": "task-1",
        "ruleset_id": "ruleset-1",
        "target_platform": "app",
        "target_url": None,
        "status": "queued",
        "task_config": {
            "workflow_version": "v2",
            "app": {"platform_build_ref": ("app-build:00000000-0000-0000-0000-000000000001")},
        },
        "page_sampling_policy": {},
        "loop_guard": {},
    }
    pool = FakePool(row)
    repository = PostgresTaskRepository(pool=pool)

    task = await repository.create_task(
        access_scope=TEST_ACCESS_SCOPE,
        target=TaskTarget(
            platform=TaskTargetPlatform.APP,
            app=AppTaskConfig(platform_build_ref="app-build:00000000-0000-0000-0000-000000000001"),
        ),
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="api",
        priority=100,
        task_config=row["task_config"],
        page_sampling_policy={},
        loop_guard={},
    )

    query, args = pool.connection.calls[-1]
    assert "FROM qc_platform_build" in query
    assert "build.status = 'verified'" in query
    assert "build.tenant_id = $11::uuid" in query
    assert "build.created_by = $10::uuid" in query
    assert args[11] is False
    assert "$7::jsonb #>> '{app,platform_build_ref}'" in query
    assert args[2] == "app"
    assert args[3] is None
    assert task.target.platform is TaskTargetPlatform.APP
    assert task.target.app is not None
    assert task.target.app.platform_build_ref.endswith("000000000001")


async def test_postgres_task_repository_gets_task_detail() -> None:
    created_at = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    row = {
        "task_id": "task-1",
        "target_url": "https://example.com/h5",
        "ruleset_id": "ruleset-1",
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": 1,
        "channel": "workbench",
        "status": "running",
        "priority": 80,
        "run_id": "run-1",
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "last_error": None,
    }
    pool = FakePool(row)
    repository = PostgresTaskRepository(pool=pool)

    detail = await repository.get_task_detail("task-1", access_scope=TEST_ACCESS_SCOPE)

    query, args = pool.connection.calls[0]
    assert "FROM qc_task task" in query
    assert "JOIN qc_ruleset ruleset" in query
    assert "LEFT JOIN LATERAL" in query
    assert args == (
        "task-1",
        True,
        "tenant-1",
        True,
        "test-user",
        None,
    )
    assert detail is not None
    assert detail.task_id == "task-1"
    assert detail.ruleset_code == "alioth_first_ruleset"
    assert detail.run_id == "run-1"
    assert detail.created_at == created_at


async def test_postgres_task_repository_lists_tasks_with_status_filter() -> None:
    created_at = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    rows = [
        {
            "task_id": "task-1",
            "target_url": "https://example.com/h5",
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "channel": "workbench",
            "status": "queued",
            "priority": 80,
            "run_id": "run-1",
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
        }
    ]
    pool = FakePool(None, rows=rows)
    repository = PostgresTaskRepository(pool=pool)

    tasks = await repository.list_tasks(
        status="queued",
        limit=20,
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[0]
    assert "FROM qc_task task" in query
    assert "WHERE ($1::varchar IS NULL OR task.status = $1)" in query
    assert "ORDER BY task.created_at DESC" in query
    assert args == (
        "queued",
        20,
        True,
        "tenant-1",
        True,
        "test-user",
        None,
    )
    assert len(tasks) == 1
    assert tasks[0].task_id == "task-1"
    assert tasks[0].ruleset_code == "alioth_first_ruleset"
    assert tasks[0].run_id == "run-1"
    assert tasks[0].created_at == created_at


async def test_postgres_task_repository_lists_rule_executions() -> None:
    finished_at = datetime(2026, 7, 7, 12, 2, tzinfo=UTC)
    rows = [
        {
            "execution_id": "execution-1",
            "rule_id": "rule-1",
            "rule_code": "alioth_2",
            "rule_name": "文案语言规范检查",
            "page_capture_id": "capture-1",
            "evidence_input_set_id": "input-set-1",
            "observation_packet_id": "packet-1",
            "evidence_artifact_ids": '["artifact-1", "artifact-2"]',
            "execution_status": "pass",
            "judge_strategy": "llm",
            "confidence": "0.9100",
            "severity": "low",
            "result_summary": "通过",
            "problem_desc": None,
            "review_status": None,
            "diagnostic_codes": ["semantic_provider_timeout"],
            "missing_evidence": ["点击后的页面状态"],
            "started_at": None,
            "finished_at": finished_at,
            "source": "agent",
            "source_idempotency_key": (
                "web-coverage-gap:web-snapshot-1:content_detail:alioth_2"
            ),
            "judge_input": {
                "schema_version": "web-coverage-gap.v1",
                "source": "web_coverage_gap",
                "coverage_snapshot_id": "web-snapshot-1",
                "missing_page_type": "content_detail",
                "reason": "required_page_type_not_discovered",
            },
        }
    ]
    pool = FakePool(None, rows=rows)
    repository = PostgresTaskRepository(pool=pool)

    executions = await repository.list_rule_executions(
        "task-1",
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[0]
    assert "FROM qc_rule_execution execution" in query
    assert "JOIN qc_rule rule" in query
    assert "execution.page_capture_id::text AS page_capture_id" in query
    assert "execution.evidence_input_set_id::text AS evidence_input_set_id" in query
    assert "execution.observation_packet_id::text AS observation_packet_id" in query
    assert "execution.evidence_artifact_ids" in query
    assert "execution.judge_output->'conflicts'" in query
    assert "execution.judge_output->'missing_evidence'" in query
    assert "real_execution.agent_run_id IS NOT DISTINCT FROM" in query
    assert "real_execution.page_capture_id IS NOT DISTINCT FROM" in query
    assert "real_execution.judge_input->>'bootstrap'" in query
    assert "ORDER BY rule.sort_order" in query
    assert args == (
        "task-1",
        None,
        True,
        "tenant-1",
        True,
        "test-user",
        None,
    )
    assert len(executions) == 1
    assert executions[0].rule_code == "alioth_2"
    assert executions[0].page_capture_id == "capture-1"
    assert executions[0].evidence_input_set_id == "input-set-1"
    assert executions[0].observation_packet_id == "packet-1"
    assert executions[0].confidence == 0.91
    assert executions[0].evidence_artifact_ids == ("artifact-1", "artifact-2")
    assert executions[0].diagnostic_codes == ("semantic_provider_timeout",)
    assert executions[0].missing_evidence == ("点击后的页面状态",)
    assert executions[0].finished_at == finished_at
    assert getattr(executions[0], "source_idempotency_key", None) == (
        "web-coverage-gap:web-snapshot-1:content_detail:alioth_2"
    )
    assert getattr(executions[0], "judge_input", None) == {
        "schema_version": "web-coverage-gap.v1",
        "source": "web_coverage_gap",
        "coverage_snapshot_id": "web-snapshot-1",
        "missing_page_type": "content_detail",
        "reason": "required_page_type_not_discovered",
    }


async def test_postgres_task_repository_lists_target_page_coverage() -> None:
    rows = [
        {
            "page_type": "settings",
            "capture_count": 0,
            "explored_action_count": 0,
            "failed_action_count": 1,
            "blocked_action_count": 0,
            "skipped_action_count": 0,
            "pending_action_count": 0,
            "failure_codes": ["PAGE_CAPTURE_FAILED"],
        }
    ]
    pool = FakePool(None, rows=rows)
    repository = PostgresTaskRepository(pool=pool)

    coverage = await repository.list_task_target_page_coverage(
        task_id="task-1",
        agent_run_id="run-1",
    )

    query, args = pool.connection.calls[0]
    assert "h5_required_page_type" in query
    assert "qc_h5_coverage_snapshot" in query
    assert "rule_target_page_types" in query
    assert "WHERE NOT EXISTS (SELECT 1 FROM h5_required_page_type)" in query
    assert "FROM qc_capture common_capture" in query
    assert "LEFT JOIN qc_page_capture page_capture" in query
    assert "FROM qc_page_exploration_action action" in query
    assert args == ("task-1", "run-1")
    assert len(coverage) == 1
    assert coverage[0].page_type == "settings"
    assert coverage[0].failed_action_count == 1
    assert coverage[0].failure_codes == ("PAGE_CAPTURE_FAILED",)


async def test_postgres_task_repository_lists_evidence_artifact_summaries() -> None:
    created_at = datetime(2026, 7, 7, 12, 3, tzinfo=UTC)
    pool = FakePool(
        None,
        rows=[
            {
                "artifact_id": "artifact-1",
                "page_capture_id": "capture-1",
                "rule_execution_id": None,
                "artifact_type": "screenshot",
                "source": "baseline",
                "storage_path": "tasks/task-1/screenshot.png",
                "content_type": "image/png",
                "checksum": "sha256:abc",
                "size_bytes": 1024,
                "metadata": {"width": 390},
                "created_at": created_at,
            }
        ],
    )
    repository = PostgresTaskRepository(pool=pool)

    artifacts = await repository.list_evidence_artifacts("task-1")

    query, args = pool.connection.calls[0]
    assert "FROM qc_evidence_artifact evidence" in query
    assert "capture.agent_run_id = $2::uuid" in query
    # 规则结果不分 Run，挂在规则执行上的证据跟着结果走
    assert "OR execution.id IS NOT NULL" in query
    assert "ORDER BY evidence.created_at ASC" in query
    assert args == ("task-1", None)
    assert artifacts[0].artifact_id == "artifact-1"
    assert artifacts[0].metadata == {"width": 390}


async def test_postgres_task_repository_reads_latest_final_report_snapshot() -> None:
    snapshot = {
        "report_version": "v1",
        "report_stage": "final",
        "is_final": True,
        "task": {"task_id": "task-1", "status": "failed"},
    }
    pool = FakePool({"final_report": snapshot})
    repository = PostgresTaskRepository(pool=pool)

    report = await repository.get_latest_final_report("task-1")

    query, args = pool.connection.calls[0]
    assert "payload.report_payload" in query
    assert "qc_report_payload_snapshot" in query
    assert "gate.gate_snapshot_id = binding.gate_snapshot_id" in query
    assert "gate.finalization_epoch = binding.finalization_epoch" in query
    assert "payload.report_payload_hash = binding.report_payload_hash" in query
    assert "run.output_context->'final_report'" in query
    assert "jsonb_typeof" in query
    assert "SELECT finalization_agent_run_id" in query
    assert "run.agent_type = 'main' THEN 1" in query
    assert args == ("task-1", None)
    assert report == snapshot


@pytest.mark.parametrize(
    ("target_platform", "workflow_version"),
    [("h5", "v2"), ("app", "v2"), ("web", "v1")],
)
async def test_postgres_task_repository_reads_explicit_historical_run_report_unchanged(
    target_platform: str,
    workflow_version: str,
) -> None:
    snapshot = {
        "report_version": "v1",
        "agent_run_id": "run-1",
        "report_stage": "final",
    }
    pool = FakePool(
        {
            "final_report": snapshot,
            "current_report_bound": False,
            "current_completion_gate": None,
            "current_task": None,
            "target_context": {
                "target_platform": target_platform,
                "task_config": {
                    "workflow_version": workflow_version,
                    "execution_mode": "full_inspection",
                },
                "finalization_agent_run_id": "run-current",
            },
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    report = await repository.get_final_report("task-1", agent_run_id="run-1")

    query, args = pool.connection.calls[0]
    target_task_sql, remainder = query.split("), current_binding AS", 1)
    assert "finalization_agent_run_id" not in target_task_sql
    assert "FROM qc_agent_run AS run" in remainder
    assert "run.task_id = $1::uuid" in remainder
    assert "run.id = $2::uuid" in remainder
    assert "INNER JOIN target_task AS task ON task.id = run.task_id" not in remainder
    assert args == ("task-1", "run-1")
    assert report == snapshot


async def test_postgres_task_repository_overlays_current_completion_gate() -> None:
    snapshot = {
        "report_version": "v1",
        "report_stage": "preview",
        "is_final": False,
        "termination_reason": "incomplete",
        "summary": {"overall_status": "incomplete"},
        "task": {
            "status": "running",
            "finished_at": None,
        },
        "completion_gate": {
            "gate_passed": False,
            "block_reasons": ["pending_capture_commit", "review_policy_unsatisfied"],
            "gate_snapshot_id": "gate-old",
            "finalization_epoch": 0,
        },
    }
    current_gate = {
        "gate_passed": False,
        "block_reasons": ["review_policy_unsatisfied"],
        "gate_snapshot_id": "gate-current",
        "finalization_epoch": 0,
    }
    pool = FakePool(
        {
            "final_report": snapshot,
            "current_completion_gate": current_gate,
            "current_task": {
                "status": "review",
                "target_platform": "h5",
                "task_config": {
                    "workflow_version": "v2",
                    "execution_mode": "full_inspection",
                },
                "finished_at": "2026-08-14T14:24:44.019483+00:00",
                "updated_at": "2026-08-14T14:24:44.019483+00:00",
                "finalization_state": "session_draining",
                "completion_gate_snapshot_id": "gate-current",
            },
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    report = await repository.get_final_report("task-1", agent_run_id="run-1")

    query, _args = pool.connection.calls[0]
    assert "current_completion_gate" in query
    assert "task.completion_gate_snapshot_id" in query
    assert "'target_platform', task.target_platform" in query
    assert report["completion_gate"] == current_gate
    assert report["report_stage"] == "final_review"
    assert report["is_final"] is False
    assert report["report_availability"] == "preview"
    assert report["export_allowed"] is False
    assert report["export_ready"] is False
    assert report["incomplete_reasons"] == ["H5_REPORT_NOT_FINALIZED"]
    assert report["task"]["status"] == "review"
    assert report["task"]["finished_at"] == "2026-08-14T14:24:44.019483+00:00"


async def test_h5_unbound_current_report_is_rebuilt_from_live_facts() -> None:
    pool = FakePool(
        {
            "final_report": {
                "agent_run_id": "run-1",
                "report_stage": "final",
                "is_final": True,
                "task": {"task_id": "task-1"},
                "completion_gate": {"gate_passed": True},
            },
            "current_report_bound": False,
            "current_completion_gate": {
                "gate_passed": True,
                "gate_snapshot_id": "gate-current",
                "finalization_epoch": 3,
            },
            "current_task": {
                "status": "done",
                "target_platform": "h5",
                "task_config": {
                    "workflow_version": "v2",
                    "execution_mode": "full_inspection",
                },
                "finalization_state": "sealed",
                "finalization_epoch": 3,
                "finalization_agent_run_id": "run-1",
                "completion_gate_snapshot_id": "gate-current",
            },
            "target_context": {
                "target_platform": "h5",
                "task_config": {
                    "workflow_version": "v2",
                    "execution_mode": "full_inspection",
                },
                "finalization_agent_run_id": "run-1",
            },
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    report = await repository.get_final_report("task-1")

    assert report is None


async def test_postgres_task_repository_marks_non_h5_terminal_report_for_review() -> None:
    snapshot = {
        "report_stage": "preview",
        "is_final": False,
        "termination_reason": "incomplete",
        "summary": {"overall_status": "incomplete"},
    }
    pool = FakePool(
        {
            "final_report": snapshot,
            "current_completion_gate": {"gate_passed": False},
            "current_task": {
                "status": "review",
                "target_platform": "app",
                "finalization_state": "session_draining",
            },
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    report = await repository.get_final_report("task-1", agent_run_id="run-1")

    assert report["report_stage"] == "final_review"
    assert report["is_final"] is False


@pytest.mark.parametrize(
    ("gate_passed", "finalization_state", "current_report_bound", "bound_gate_id"),
    [
        (False, "session_draining", False, None),
        (True, "sealed", False, None),
        (True, "sealed", True, "gate-stale"),
    ],
)
async def test_web_v2_full_report_rejects_unpassed_unbound_or_stale_payload(
    gate_passed: bool,
    finalization_state: str,
    current_report_bound: bool,
    bound_gate_id: str | None,
) -> None:
    legacy_run_payload = {
        "report_stage": "final",
        "is_final": True,
        "agent_run_id": "run-1",
        "summary": {"overall_status": "unknown"},
    }
    pool = FakePool(
        {
            "final_report": legacy_run_payload,
            "current_report_bound": current_report_bound,
            "bound_gate_snapshot_id": bound_gate_id,
            "bound_finalization_epoch": 4,
            "current_completion_gate": {
                "gate_passed": gate_passed,
                "gate_snapshot_id": "gate-current",
                "finalization_epoch": 4,
                "block_reasons": [] if gate_passed else ["rule_executions_terminal"],
            },
            "current_task": {
                "status": "done" if gate_passed else "review",
                "target_platform": "web",
                "task_config": {
                    "workflow_version": "v2",
                    "execution_mode": "full_inspection",
                    "web_inspection_notification_mode": "enforced",
                },
                "finalization_state": finalization_state,
                "finalization_epoch": 4,
                "finalization_agent_run_id": "run-1",
                "completion_gate_snapshot_id": "gate-current",
            },
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    report = await repository.get_final_report("task-1", agent_run_id="run-1")

    assert report is None


@pytest.mark.parametrize("notification_mode", [None, "off"])
async def test_non_enforced_web_v2_keeps_legacy_preview_compatibility(
    notification_mode: str | None,
) -> None:
    task_config = {
        "workflow_version": "v2",
        "execution_mode": "full_inspection",
    }
    if notification_mode is not None:
        task_config["web_inspection_notification_mode"] = notification_mode
    repository = PostgresTaskRepository(
        pool=FakePool(
            {
                "final_report": {
                    "agent_run_id": "run-1",
                    "report_stage": "final",
                    "is_final": True,
                },
                "current_report_bound": False,
                "bound_gate_snapshot_id": None,
                "bound_finalization_epoch": None,
                "current_completion_gate": None,
                "current_task": {
                    "status": "done",
                    "target_platform": "web",
                    "task_config": task_config,
                    "finalization_state": "sealed",
                    "finalization_epoch": 4,
                    "finalization_agent_run_id": "run-1",
                    "completion_gate_snapshot_id": "gate-current",
                },
            }
        )
    )

    report = await repository.get_final_report("task-1", agent_run_id="run-1")

    assert report is not None
    assert report["report_stage"] == "final_review"
    assert report["is_final"] is False


async def test_web_v2_full_report_returns_current_passed_bound_payload() -> None:
    bound_payload = {
        "report_stage": "final",
        "is_final": True,
        "report_binding_version": 7,
        "agent_run_id": "run-1",
        "task": {"task_id": "task-1", "status": "snapshot-status"},
        "completion_gate": {
            "gate_snapshot_id": "gate-current",
            "finalization_epoch": 4,
        },
        "summary": {"overall_status": "unknown"},
    }
    repository = PostgresTaskRepository(
        pool=FakePool(
            {
                "final_report": bound_payload,
                "current_report_bound": True,
                "bound_gate_snapshot_id": "gate-current",
                "bound_finalization_epoch": 4,
                "bound_report_version": 7,
                "current_completion_gate": {
                    "gate_passed": False,
                    "gate_snapshot_id": "gate-live-mutated",
                    "finalization_epoch": 99,
                    "block_reasons": ["live-state-mutated"],
                },
                "current_task": {
                    "status": "running",
                    "target_platform": "web",
                    "task_config": {"workflow_version": "live-mutated"},
                    "finalization_state": "open",
                    "finalization_epoch": 99,
                    "finalization_agent_run_id": "run-live-mutated",
                    "completion_gate_snapshot_id": "gate-live-mutated",
                },
            }
        )
    )

    report = await repository.get_final_report("task-1", agent_run_id="run-1")

    assert report is not None
    assert report["report_stage"] == "final"
    assert report["is_final"] is True
    assert report["completion_gate"]["gate_snapshot_id"] == "gate-current"
    assert report["task"]["status"] == "snapshot-status"


@pytest.mark.parametrize(
    "bound_payload",
    [
        {
            "agent_run_id": "run-stale",
            "task": {"task_id": "task-1"},
            "completion_gate": {
                "gate_snapshot_id": "gate-current",
                "finalization_epoch": 4,
            },
        },
        {
            "agent_run_id": "run-1",
            "task": {"task_id": "task-stale"},
            "completion_gate": {
                "gate_snapshot_id": "gate-current",
                "finalization_epoch": 4,
            },
        },
        {
            "agent_run_id": "run-1",
            "task": {"task_id": "task-1"},
            "completion_gate": {
                "gate_snapshot_id": "gate-stale",
                "finalization_epoch": 4,
            },
        },
        {
            "agent_run_id": "run-1",
            "task": {"task_id": "task-1"},
            "completion_gate": {
                "gate_snapshot_id": "gate-current",
                "finalization_epoch": 3,
            },
        },
    ],
)
async def test_web_v2_full_report_rejects_bound_payload_identity_mismatch(
    bound_payload: dict,
) -> None:
    repository = PostgresTaskRepository(
        pool=FakePool(
            {
                "final_report": bound_payload,
                "current_report_bound": True,
                "bound_gate_snapshot_id": "gate-current",
                "bound_finalization_epoch": 4,
                "current_completion_gate": {
                    "gate_passed": True,
                    "gate_snapshot_id": "gate-current",
                    "finalization_epoch": 4,
                    "block_reasons": [],
                },
                "current_task": {
                    "status": "done",
                    "target_platform": "web",
                    "task_config": {
                        "workflow_version": "v2",
                        "execution_mode": "full_inspection",
                        "web_inspection_notification_mode": "enforced",
                    },
                    "finalization_state": "sealed",
                    "finalization_epoch": 4,
                    "finalization_agent_run_id": "run-1",
                    "completion_gate_snapshot_id": "gate-current",
                },
            }
        )
    )

    assert (
        await repository.get_final_report("task-1", agent_run_id="run-1") is None
    )


@pytest.mark.parametrize(
    ("target_platform", "workflow_version"),
    [("app", "v2"), ("web", "v1")],
)
async def test_non_web_formal_report_keeps_legacy_run_fallback(
    target_platform: str,
    workflow_version: str,
) -> None:
    repository = PostgresTaskRepository(
        pool=FakePool(
            {
                "final_report": {"report_stage": "preview", "is_final": False},
                "current_report_bound": False,
                "current_completion_gate": {"gate_passed": False},
                "current_task": {
                    "status": "review",
                    "target_platform": target_platform,
                    "task_config": {
                        "workflow_version": workflow_version,
                        "execution_mode": "full_inspection",
                    },
                    "finalization_state": "session_draining",
                },
            }
        )
    )

    report = await repository.get_final_report("task-1")

    assert report is not None
    assert report["report_stage"] == "final_review"
    assert report["is_final"] is False


async def test_postgres_task_repository_validates_agent_run_ownership() -> None:
    pool = FakePool(True)
    repository = PostgresTaskRepository(pool=pool)

    exists = await repository.agent_run_exists(task_id="task-1", agent_run_id="run-1")

    query, args = pool.connection.calls[0]
    assert "SELECT EXISTS" in query
    assert "task_id = $1::uuid" in query
    assert args == ("task-1", "run-1")
    assert exists is True


async def test_postgres_task_repository_does_not_fall_back_to_older_run_report() -> None:
    repository = PostgresTaskRepository(pool=FakePool({"final_report": None}))

    report = await repository.get_latest_final_report("task-1")

    assert report is None


async def test_postgres_task_repository_lists_candidate_rules_with_page_type() -> None:
    rows = [
        {
            "rule_id": "rule-21",
            "rule_code": "alioth_21",
            "name": "首页弹窗蒙层点击行为检查",
            "description": "检查广告蒙层交互。",
            "category": "ad",
            "severity": "high",
            "executor_type": "hybrid",
            "scope": "page",
            "page_type": "home",
            "applicable_page_types": ["home"],
            "url_pattern": None,
            "steps": [{"action": "observe"}],
            "input_contract": {"required_artifacts": ["dom_snapshot"]},
            "check_point": "检查首页弹窗蒙层点击后是否符合预期行为。",
            "judge_config": {"strategy": "visual"},
            "manual_review_policy": {"min_confidence": 0.82},
            "sort_order": 210,
            "match_reason": "applicable_page_types",
        }
    ]
    pool = FakePool(None, rows=rows)
    repository = PostgresTaskRepository(pool=pool)

    rules = await repository.list_candidate_rules(
        task_id="task-1",
        page_type="home",
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[-1]
    assert "FROM qc_task task" in query
    assert "JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))" in query
    assert "rule.status = 'enabled'" in query
    assert "rule.channel IS NULL OR rule.channel = task.channel" in query
    assert "rule.input_contract->'target_clients'" in query
    assert "WHEN 'app' THEN 'APP'" in query
    assert "WHEN 'web' THEN 'WEB'" in query
    assert "ELSE 'H5'" in query
    assert "evidence_input_scope" in query
    assert ") = 'page'" in query
    assert "rule.applicable_page_types ? $2" in query
    assert "rule.page_type = $2" not in query
    assert "'global'" not in query
    assert "rule.applicable_page_types = '[]'::jsonb" not in query
    assert "ORDER BY rule.sort_order ASC" in query
    assert "rule.description" in query
    assert "rule.input_contract" in query
    assert "rule.manual_review_policy" in query
    # 登录依赖闸门：默认 auto，在登录机关闭的环境解析为拦 authenticated。
    # App 不再整平台豁免，由调用方按 session_auth 传入显式闸门。
    assert "rule.input_contract->>'auth_requirement'" in query
    assert "OR task.target_platform = 'app'" not in query
    # 读范围多了部门参数，登录态门槛参数顺延一位
    assert "$8::text IS NULL" in query
    assert args == (
        "task-1",
        "home",
        True,
        "tenant-1",
        True,
        "test-user",
        None,
        "authenticated",
    )
    assert len(rules) == 1
    assert rules[0].rule_code == "alioth_21"
    assert rules[0].description == "检查广告蒙层交互。"
    assert rules[0].applicable_page_types == ["home"]
    assert rules[0].steps == [{"action": "observe"}]
    assert rules[0].input_contract == {"required_artifacts": ["dom_snapshot"]}
    assert rules[0].judge_config == {"strategy": "visual"}
    assert rules[0].manual_review_policy == {"min_confidence": 0.82}
    assert rules[0].match_reason == "applicable_page_types"


async def test_postgres_task_repository_filters_candidate_rules_by_url_pattern() -> None:
    rows = [
        {
            "rule_id": "rule-home",
            "rule_code": "alioth_home_url",
            "name": "首页 URL 规则",
            "description": None,
            "category": "ad",
            "severity": "high",
            "executor_type": "hybrid",
            "scope": "page",
            "page_type": "home",
            "applicable_page_types": ["home"],
            "url_pattern": r"example\.com/home",
            "steps": [],
            "input_contract": {},
            "check_point": "首页",
            "judge_config": {},
            "manual_review_policy": {},
            "sort_order": 10,
            "match_reason": "applicable_page_types",
        },
        {
            "rule_id": "rule-profile",
            "rule_code": "alioth_profile_url",
            "name": "个人页 URL 规则",
            "description": None,
            "category": "ad",
            "severity": "high",
            "executor_type": "hybrid",
            "scope": "page",
            "page_type": "profile",
            "applicable_page_types": ["home"],
            "url_pattern": r"example\.com/profile",
            "steps": [],
            "input_contract": {},
            "check_point": "个人页",
            "judge_config": {},
            "manual_review_policy": {},
            "sort_order": 20,
            "match_reason": "applicable_page_types",
        },
        {
            "rule_id": "rule-global",
            "rule_code": "alioth_global",
            "name": "无 URL 限制",
            "description": None,
            "category": "text",
            "severity": "low",
            "executor_type": "hybrid",
            "scope": "page",
            "page_type": "home",
            "applicable_page_types": ["home"],
            "url_pattern": None,
            "steps": [],
            "input_contract": {},
            "check_point": "通用",
            "judge_config": {},
            "manual_review_policy": {},
            "sort_order": 30,
            "match_reason": "applicable_page_types",
        },
    ]
    repository = PostgresTaskRepository(pool=FakePool(None, rows=rows))

    rules = await repository.list_candidate_rules(
        task_id="task-1",
        page_type="home",
        page_url="https://example.com/home/index",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert [rule.rule_code for rule in rules] == ["alioth_home_url", "alioth_global"]
    assert rules[0].match_reason == "url_pattern"


async def test_candidate_rules_auth_gate_overrides_and_validation() -> None:
    pool = FakePool(None, rows=[])
    repository = PostgresTaskRepository(pool=pool)

    # 显式关闭闸门（None）与显式指定拦截值均按传入透传；无 scope 时闸门参数为 $3。
    await repository.list_candidate_rules(task_id="task-1", page_type="home", auth_gate=None)
    query, args = pool.connection.calls[-1]
    assert "$3::text IS NULL" in query
    assert args == ("task-1", "home", None)

    await repository.list_candidate_rules(task_id="task-1", page_type="home", auth_gate="guest")
    _query, args = pool.connection.calls[-1]
    assert args == ("task-1", "home", "guest")

    with pytest.raises(ValueError):
        await repository.list_candidate_rules(task_id="task-1", page_type="home", auth_gate="vip")


async def test_postgres_task_repository_gets_rule_judge_definition() -> None:
    pool = FakePool(
        {
            "rule_id": "rule-6",
            "rule_code": "alioth_6",
            "executor_type": "rule_engine",
            "judge_config": '{"engine_config":{"handler":"technical_error_text"}}',
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    definition = await repository.get_rule_judge_definition(task_id="task-1", rule_code="alioth_6")

    query, _ = pool.connection.calls[-1]
    assert "evidence_input_scope" in query
    assert ") = 'page'" in query

    query, args = pool.connection.calls[-1]
    assert "JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))" in query
    assert "rule.status = 'enabled'" in query
    assert "rule.channel IS NULL OR rule.channel = task.channel" in query
    assert args == ("task-1", "alioth_6")
    assert definition is not None
    assert definition.executor_type == "rule_engine"
    assert definition.judge_config["engine_config"]["handler"] == "technical_error_text"


async def test_postgres_task_repository_builds_app_capture_url_from_verified_identity() -> None:
    pool = FakePool(
        {
            "url": "",
            "surface_kind": "android",
            "package_name": "Com.Example.Target",
            "activity": ".MainActivity",
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    page_url = await repository.get_page_capture_url(
        task_id="task-1",
        page_capture_id="capture-1",
    )

    query, args = pool.connection.calls[0]
    assert "LEFT JOIN qc_app_capture" in query
    assert args == ("capture-1", "task-1")
    assert page_url == "app://com.example.target/.MainActivity"


async def test_postgres_task_repository_keeps_formal_page_url_before_app_fallback() -> None:
    repository = PostgresTaskRepository(
        pool=FakePool(
            {
                "url": "https://example.com/page",
                "surface_kind": "android",
                "package_name": "com.example.target",
                "activity": ".MainActivity",
            }
        )
    )

    page_url = await repository.get_page_capture_url(
        task_id="task-1",
        page_capture_id="capture-1",
    )

    assert page_url == "https://example.com/page"


async def test_postgres_task_repository_lists_all_enabled_judge_definitions() -> None:
    pool = FakePool(
        None,
        rows=[
            {
                "rule_id": "rule-6",
                "rule_code": "alioth_6",
                "executor_type": "rule_engine",
                "judge_config": (
                    '{"engine_config":{"handler":"technical_error_text","pattern":"HTTP 500"}}'
                ),
            }
        ],
    )
    repository = PostgresTaskRepository(pool=pool)

    definitions = await repository.list_enabled_rule_judge_definitions()

    query, args = pool.connection.calls[0]
    assert "JOIN qc_ruleset ruleset ON ruleset.id = rule.ruleset_id" in query
    assert "rule.status = 'enabled'" in query
    assert "ruleset.status = 'enabled'" in query
    assert "rule.executor_type = 'rule_engine'" not in query
    assert args == ()
    assert len(definitions) == 1
    assert definitions[0].rule_code == "alioth_6"
    assert definitions[0].judge_config["engine_config"]["pattern"] == "HTTP 500"


async def test_postgres_task_repository_gets_locked_rule_execution_definition() -> None:
    pool = FakePool(
        {
            "executor_type": "hybrid",
            "manual_review_policy": '{"min_confidence":0.82}',
            "severity": "high",
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    definition = await repository.get_rule_execution_definition(
        task_id="task-1",
        rule_id=None,
        rule_code="alioth_3",
    )

    query, args = pool.connection.calls[-1]
    assert "rule.executor_type" in query
    assert "rule.manual_review_policy" in query
    assert "rule.severity" in query
    assert "rule.id = $2::uuid" in query
    assert "rule.rule_code = $3" in query
    assert args == ("task-1", None, "alioth_3")
    assert definition is not None
    assert definition.executor_type == "hybrid"
    assert definition.manual_review_policy == {"min_confidence": 0.82}
    assert definition.severity == "high"


async def test_postgres_task_repository_finalizes_missing_rule_executions() -> None:
    finished_at = datetime(2026, 7, 7, 12, 5, tzinfo=UTC)
    pool = FakePool({"inserted_count": 2, "unresolved_candidate_count": 1})
    repository = PostgresTaskRepository(pool=pool)

    result = await repository.finalize_missing_rule_executions(
        task_id="task-1",
        agent_run_id="run-1",
        finished_at=finished_at,
        candidate_pages=[CandidatePageReference(page_capture_id="capture-1", page_type="home")],
        runtime_rule_fallbacks=[
            RuntimeRuleExecutionFallback(
                page_capture_id="capture-1",
                rule_id="rule-1",
                rule_code="alioth_1",
                execution_status="error",
                result_summary="规则写回失败",
                problem_desc="数据库暂不可写",
            ),
            RuntimeRuleExecutionFallback(
                page_capture_id="capture-1",
                rule_id="rule-17",
                rule_code="alioth_17",
                execution_status="skipped",
                result_summary="任务预算终止，规则未执行",
                problem_desc="达到动态步数上限",
                reason="run_budget_exhausted",
            ),
        ],
        runtime_page_failure_fallbacks=[
            RuntimePageInspectionFailureFallback(
                page_capture_id="capture-2",
                page_type="home",
                failure_code="PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
                failure_message="二维码登录遮罩无法关闭",
            )
        ],
    )

    query, args = next(
        call for call in pool.connection.calls if "INSERT INTO qc_rule_execution" in call[0]
    )
    assert "INSERT INTO qc_rule_execution" in query
    assert "JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))" in query
    assert "execution_status" in query
    assert "'skipped'" in query
    assert "'evidence_insufficient'" in query
    assert "'runtime_rule_write_failed'" in query
    assert "'runtime_page_inspection_failed'" in query
    assert "'exploration_not_started'" in query
    assert "(SELECT query_count FROM queried_summary) = 0" in query
    assert "runtime_rule_fallbacks" in query
    assert "'runtime_fallback'" in query
    assert "'manual_flag'" in query
    assert "'needs_recheck'" in query
    assert "NOT EXISTS" in query
    assert "candidate_rules_tool" in query
    assert "terminated_queried AS" in query
    assert "formal_v2_queried AS" in query
    assert "FROM qc_h5_page_type_assessment assessment" in query
    assert "assessment.status = 'covered'" in query
    assert "task.target_platform IN ('h5', 'web')" in query
    assert "task.task_config->>'workflow_version' = 'v2'" in query
    assert "trace.status = 'terminated'" in query
    assert "trace.capability_input ? 'page_capture_id'" in query
    assert "trace.capability_input ? 'page_type'" in query
    assert "规则已进入页面候选清单" in query
    assert "该规则未进入任何页面候选清单" in query
    assert "rule.channel IS NULL OR rule.channel = task.channel" in query
    assert "DELETE FROM qc_rule_execution bootstrap" in query
    assert "INSERT INTO qc_review_item" in query
    assert "jsonb_to_recordset($4::jsonb)" in query
    assert "rule.applicable_page_types ? q.page_type" in query
    assert "q.candidate_rule_codes ? rule.rule_code" in query
    assert "capture.agent_run_id = $2::uuid" in query
    assert "inspection_lifecycle_status, 'active') = 'active'" in query
    assert "existing.page_capture_id = capture.id" in query
    assert "ON CONFLICT (task_id, rule_id, page_capture_id)" in query
    assert "ANY($5::varchar[])" in query
    assert "jsonb_to_recordset($6::jsonb)" in query
    assert "jsonb_to_recordset($7::jsonb)" in query
    assert "runtime_page_failure_fallbacks" in query
    assert "runtime_page_failure" in query
    assert "rule.page_type = 'global'" not in query
    assert "rule.applicable_page_types ? 'global'" not in query
    assert "has_full_query" not in query
    # 通用 Capture 允许 App 参与候选补漏；登录依赖按规则合同统一补记，不按平台豁免。
    assert "FROM qc_capture common_capture" in query
    assert "LEFT JOIN qc_page_capture page_capture" in query
    assert "auth_requirement_blocked" in query
    assert "'auth_requirement_not_met'" in query
    assert "rule.input_contract->>'auth_requirement' = $8::text" in query
    assert "task.target_platform <> 'app'" not in query
    assert "AND NOT auth_requirement_blocked" in query
    assert "该规则需要登录后才能检查" in query
    assert args[7] == "authenticated"
    assert args[0:3] == ("task-1", "run-1", finished_at)
    assert json.loads(args[3]) == [
        {
            "page_capture_id": "capture-1",
            "page_type": "home",
            "candidate_rule_codes": None,
        }
    ]
    assert "home" in args[4]
    assert "ad_popup" not in args[4]
    assert json.loads(args[5]) == [
        {
            "page_capture_id": "capture-1",
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "execution_status": "error",
            "result_summary": "规则写回失败",
            "problem_desc": "数据库暂不可写",
            "reason": None,
        },
        {
            "page_capture_id": "capture-1",
            "rule_id": "rule-17",
            "rule_code": "alioth_17",
            "execution_status": "skipped",
            "result_summary": "任务预算终止，规则未执行",
            "problem_desc": "达到动态步数上限",
            "reason": "run_budget_exhausted",
        },
    ]
    assert json.loads(args[6]) == [
        {
            "page_capture_id": "capture-2",
            "page_type": "home",
            "failure_code": "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
            "failure_message": "二维码登录遮罩无法关闭",
        }
    ]
    assert result.inserted_count == 2
    assert result.unresolved_candidate_count == 1


async def test_postgres_task_repository_reads_rule_execution_completeness() -> None:
    pool = FakePool(
        {
            "candidate_query_count": 1,
            "candidate_rule_count": 3,
            "completed_candidate_count": 2,
            "unresolved_rule_codes": ["alioth_6"],
            "unresolved_candidate_items": [
                {"page_capture_id": "capture-2", "rule_code": "alioth_6"}
            ],
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    result = await repository.get_rule_execution_completeness(
        task_id="task-1",
        agent_run_id="run-1",
    )

    query, args = pool.connection.calls[0]
    assert "candidate_rules_tool" in query
    assert "FROM qc_h5_page_type_assessment assessment" in query
    assert "assessment.status = 'covered'" in query
    assert "task.target_platform IN ('h5', 'web')" in query
    assert "task.task_config->>'workflow_version' = 'v2'" in query
    assert "trace.capability_input ? 'page_capture_id'" in query
    assert "trace.capability_input ? 'page_type'" in query
    assert "ANY($3::varchar[])" in query
    assert "execution_candidates AS" in query
    # 候选页面按本 Run 的采集算，页面上的结论不分哪个 Run 写的
    assert "capture.agent_run_id = $2::uuid" in query
    assert "capture.agent_run_id = execution.agent_run_id" not in query
    assert "FROM qc_rule_execution AS concluded" in query
    assert "LEFT JOIN qc_task_page_node node" in query
    assert "COALESCE(capture.page_type, node.page_type, parent.page_type)" in query
    assert "rule.applicable_page_types ? q.page_type" not in query
    assert "judge_input->>'bootstrap'" in query
    assert "execution.page_capture_id IS NOT NULL" in query
    assert "execution.agent_run_id = $2::uuid" not in query
    candidate_pages_sql = query.split("candidate_pages AS", 1)[1].split("candidate_rules AS", 1)[0]
    assert "execution_candidates" not in candidate_pages_sql
    assert args[0:2] == ("task-1", "run-1")
    assert "home" in args[2]
    assert "ad_popup" not in args[2]
    assert result.candidate_query_count == 1
    assert result.candidate_rule_count == 3
    assert result.completed_candidate_count == 2
    assert result.unresolved_rule_codes == ("alioth_6",)
    assert result.unresolved_candidate_items[0].page_capture_id == "capture-2"


async def test_postgres_task_repository_exposes_run_resource_observability() -> None:
    run_row = {
        "run_id": "run-1",
        "status": "done",
        "stop_reason": "completed",
        "error_message": None,
        "total_steps": 4,
        "total_ai_calls": 2,
        "total_tokens": 120,
        "input_tokens": 100,
        "output_tokens": 20,
        "worker_rss_before_bytes": 1000,
        "worker_rss_after_bytes": 1300,
        "worker_rss_delta_bytes": 300,
        "working_memory_bytes": 2048,
        "final_text": "完成",
        "started_at": datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 7, 27, 10, 1, tzinfo=UTC),
    }
    step_rows = [
        {
            "step_seq": 1,
            "phase": None,
            "thought_summary": None,
            "selected_capability": "page_capture_tool",
            "capability_kind": "tool",
            "status": "error",
            "latency_ms": 30000,
            "error_code": "CLICK_TARGET_NOT_FOUND",
            "error_message": "当前页面未找到点击目标",
            "capability_input": {"selector": ".missing"},
            "capability_output": {"error_code": "CLICK_TARGET_NOT_FOUND"},
        }
    ]
    pool = FakePool(run_row, rows=step_rows)
    repository = PostgresTaskRepository(pool=pool)

    run = await repository.get_latest_agent_run(
        "task-1",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert run is not None
    assert run.input_tokens == 100
    assert run.output_tokens == 20
    assert run.worker_rss_delta_bytes == 300
    assert run.working_memory_bytes == 2048
    assert run.action_trace_count == 1
    assert run.steps[0].error_code == "CLICK_TARGET_NOT_FOUND"
    run_query = pool.connection.calls[0][0]
    step_query = pool.connection.calls[1][0]
    assert "worker_rss_after_bytes" in run_query
    assert "pg_column_size(working_memory)" in run_query
    assert "error_code" in step_query


async def test_postgres_task_repository_records_rule_execution_result() -> None:
    finished_at = datetime(2026, 7, 8, 10, 30, tzinfo=UTC)
    row = {
        "execution_id": "execution-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "execution_status": "fail",
        "page_capture_id": "capture-1",
        "validation_error": None,
    }
    pool = FakePool(row)
    repository = PostgresTaskRepository(pool=pool)

    result = await repository.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id=None,
            rule_code="alioth_1",
            execution_status="fail",
            page_capture_id="capture-1",
            observation_packet_id="packet-1",
            judge_strategy="hybrid",
            confidence=0.88,
            severity=None,
            result_summary="未通过",
            problem_desc="弹窗关闭按钮不可见",
            evidence_artifact_ids=["artifact-1"],
            judge_input={"page_type": "ad_popup"},
            judge_output={"status": "fail"},
            review_status=None,
            review_reason=None,
            error_code=None,
            error_message=None,
            started_at=None,
            finished_at=finished_at,
        )
    )

    query, args = pool.connection.calls[-1]
    assert "UPDATE qc_rule_execution execution" in query
    assert "INSERT INTO qc_rule_execution" in query
    assert "INSERT INTO qc_review_item" in query
    assert "ON CONFLICT (rule_execution_id)" in query
    assert "rule.rule_code = $3" in query
    assert "capture_validation" in query
    assert "packet_validation" in query
    assert "packet.created_by_agent_run_id IS NOT DISTINCT FROM $4::uuid" in query
    assert "OR $5::varchar IN" in query
    assert "'evidence_insufficient'" in query
    assert "evidence_validation" in query
    assert "reuse_validation AS" in query
    assert "rule-reuse.v1" in query
    assert "NOT (source.judge_output ? 'reuse')" in query
    assert "execution.page_capture_id IS NOT DISTINCT FROM $20::uuid" in query
    assert "ON CONFLICT (task_id, rule_id, page_capture_id) DO UPDATE" in query
    assert "page_capture_id" in query
    assert args[0] == "task-1"
    assert args[1] is None
    assert args[2] == "alioth_1"
    assert args[3] == "run-1"
    assert args[4] == "fail"
    assert args[5] == "hybrid"
    assert args[6] == 0.88
    assert args[8] == "未通过"
    assert args[9] == "弹窗关闭按钮不可见"
    assert '"page_type": "ad_popup"' in args[11]
    assert '"status": "fail"' in args[12]
    assert args[18] == finished_at
    assert args[19] == "capture-1"
    assert args[20] == "packet-1"
    assert result is not None
    assert result.execution_id == "execution-1"
    assert result.execution_status == "fail"
    assert result.page_capture_id == "capture-1"


@pytest.mark.parametrize(
    ("validation_error", "message"),
    [
        ("page_capture_not_found", "页面采集记录不属于当前任务"),
        ("evidence_artifact_mismatch", "证据附件不属于指定页面采集"),
        ("rule_reuse_source_mismatch", "规则复用来源执行或证据链不可信"),
        (
            "observation_packet_mismatch",
            "Observation Packet 不属于指定任务、规则或页面采集",
        ),
    ],
)
async def test_postgres_task_repository_rejects_invalid_evidence_ownership(
    validation_error: str,
    message: str,
) -> None:
    pool = FakePool(
        {
            "execution_id": None,
            "rule_id": None,
            "rule_code": None,
            "execution_status": None,
            "page_capture_id": "capture-1",
            "validation_error": validation_error,
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    with pytest.raises(RuleExecutionValidationError, match=message):
        await repository.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                rule_code="alioth_1",
                execution_status="fail",
                page_capture_id="capture-1",
                evidence_artifact_ids=["artifact-1"],
            )
        )


async def test_postgres_task_repository_routes_source_idempotency_without_capture() -> None:
    row = {
        "execution_id": "execution-gap-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "execution_status": "evidence_insufficient",
        "page_capture_id": None,
        "validation_error": None,
    }
    pool = FakePool(row)
    repository = PostgresTaskRepository(pool=pool)

    result = await repository.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            agent_run_id="run-1",
            rule_code="alioth_1",
            execution_status="evidence_insufficient",
            page_capture_id=None,
            source_idempotency_key=(
                "web-coverage-gap:snapshot-1:content_detail:alioth_1"
            ),
        )
    )

    query, args = pool.connection.calls[-1]
    assert "source_idempotency_key" in query
    assert "execution.source_idempotency_key = $22::varchar" in query
    assert "ON CONFLICT (task_id, rule_id, source_idempotency_key)" in query
    assert "WHERE source_idempotency_key IS NOT NULL" in query
    assert "ON CONFLICT (task_id, rule_id, page_capture_id)" in query
    assert args[19] is None
    assert args[21] == "web-coverage-gap:snapshot-1:content_detail:alioth_1"
    assert result is not None
    assert result.execution_id == "execution-gap-1"


async def test_postgres_task_repository_suppresses_review_only_when_explicit() -> None:
    row = {
        "execution_id": "execution-gap-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "execution_status": "evidence_insufficient",
        "page_capture_id": None,
        "validation_error": None,
    }
    pool = FakePool(row)
    repository = PostgresTaskRepository(pool=pool)

    await repository.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            agent_run_id="run-1",
            rule_code="alioth_1",
            execution_status="evidence_insufficient",
            source_idempotency_key=(
                "web-coverage-gap:snapshot-1:content_detail:alioth_1"
            ),
            judge_input={
                "schema_version": "web-coverage-gap.v1",
                "source": "web_coverage_gap",
                "coverage_snapshot_id": "snapshot-1",
                "missing_page_type": "content_detail",
                "reason": "required_page_type_not_discovered",
            },
            create_review_item=False,
        )
    )

    query, args = pool.connection.calls[-1]
    assert "AND $23::boolean" in query
    assert args[22] is False
    for required_sql in (
        "review_suppression_validation AS",
        "WHEN $23::boolean THEN NULL",
        "FROM qc_web_coverage_snapshot AS snapshot",
        "snapshot.task_id = target_rule.task_id",
        "snapshot.agent_run_id = $4::uuid",
        "snapshot.finalization_epoch = task.finalization_epoch",
        "task.finalization_agent_run_id = $4::uuid",
        "snapshot.id::text = $12::jsonb->>'coverage_snapshot_id'",
        "snapshot.missing_page_types ? ($12::jsonb->>'missing_page_type')",
        "rule.input_contract->'target_clients' ? 'WEB'",
        "rule.applicable_page_types ? ($12::jsonb->>'missing_page_type')",
        "= 'page'",
        "rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))",
    ):
        assert required_sql in query


async def test_postgres_task_repository_rejects_untrusted_review_suppression() -> None:
    pool = FakePool(
        {
            "execution_id": None,
            "rule_id": None,
            "rule_code": None,
            "execution_status": None,
            "page_capture_id": None,
            "validation_error": "review_suppression_not_authorized",
        }
    )
    repository = PostgresTaskRepository(pool=pool)

    with pytest.raises(RuleExecutionValidationError, match="Web Coverage Gap"):
        await repository.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="task-1",
                agent_run_id="run-1",
                rule_code="alioth_1",
                execution_status="evidence_insufficient",
                source_idempotency_key=(
                    "web-coverage-gap:snapshot-forged:content_detail:alioth_1"
                ),
                judge_input={
                    "schema_version": "web-coverage-gap.v1",
                    "source": "web_coverage_gap",
                    "coverage_snapshot_id": "snapshot-forged",
                    "missing_page_type": "content_detail",
                    "reason": "required_page_type_not_discovered",
                },
                create_review_item=False,
            )
        )


async def test_postgres_task_repository_keeps_legacy_null_source_idempotency_path() -> None:
    row = {
        "execution_id": "execution-legacy-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "execution_status": "skipped",
        "page_capture_id": None,
        "validation_error": None,
    }
    pool = FakePool(row)
    repository = PostgresTaskRepository(pool=pool)

    await repository.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_1",
            execution_status="skipped",
            page_capture_id=None,
        )
    )

    query, args = pool.connection.calls[-1]
    assert "$22::varchar IS NULL" in query
    assert "execution.page_capture_id IS NOT DISTINCT FROM $20::uuid" in query
    assert "ON CONFLICT (task_id, rule_id, page_capture_id)" in query
    assert args[21] is None
