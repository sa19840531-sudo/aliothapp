"""任务洞察读仓储的动态页面与 Frontier 查询测试。"""

import json
from typing import Any

import pytest

from services.api.app.application.app_inspection_publisher import (
    PostgresAppCaptureInspectionAnnotator,
    formal_inspection_status,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)


class FakeAcquire:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    async def __aenter__(self) -> "FakeConnection":
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((" ".join(query.split()), args))
        return self.rows

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(query.split()), args))
        return self.rows[0] if self.rows else None


class FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.connection = FakeConnection(rows)
        self.acquire_count = 0

    def acquire(self) -> FakeAcquire:
        self.acquire_count += 1
        return FakeAcquire(self.connection)


async def test_task_context_reads_frozen_run_budget_without_replacing_task_budget() -> None:
    pool = FakePool([{
        "task_id": "task-1",
        "task_config": "{}",
        "working_memory": "{}",
        "loop_guard": json.dumps({"max_duration_seconds": 1800, "max_steps": 10}),
        "run_loop_guard": json.dumps({"max_duration_seconds": 3600}),
    }])
    context = await PostgresTaskInsightRepository(pool=pool).fetch_task_context("task-1")
    assert context is not None
    assert context["loop_guard"] == {"max_duration_seconds": 1800, "max_steps": 10}
    assert context["run_loop_guard"] == {"max_duration_seconds": 3600}
    query, _ = pool.connection.calls[0]
    assert "run.run_loop_guard" in query
    assert "loop_guard AS run_loop_guard" in query


async def test_report_list_meta_keeps_h5_current_owner_and_judge_capture_scope() -> None:
    pool = FakePool([])
    repository = PostgresTaskInsightRepository(pool=pool)

    assert await repository.report_list_meta(["task-1"]) == {}

    query, args = pool.connection.calls[0]
    assert args == (["task-1"],)
    assert "WHEN task.target_platform = 'h5'" in query
    assert "WHEN id = task.finalization_agent_run_id THEN 0" in query
    assert "WHEN agent_type = 'main' THEN 1" in query
    assert query.count("FROM qc_task AS current_h5_task") >= 2
    assert "current_source_capture.agent_run_id = COALESCE(" in query


async def test_evidence_gap_rows_include_active_attempt_deferred_decisions() -> None:
    pool = FakePool([])
    repository = PostgresTaskInsightRepository(pool=pool)

    await repository.list_evidence_gap_rows(task_id="task-1", agent_run_id="run-1")

    query, args = pool.connection.calls[0]
    assert "jsonb_array_elements(" in query
    assert "attempt.result_payload->'deferred_decisions'" in query
    assert "req.work_item_id = work_item.id" in query
    assert "req.attempt_id = attempt.id" in query
    assert args == ("task-1", "run-1")


async def test_list_inspection_work_item_rows_reads_protocol_projection() -> None:
    class SequencedConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__([])
            self._batches = [
                [{"work_item_id": "work-1", "status": "running"}],
                [{"work_item_id": "work-1", "rule_code": "alioth_1"}],
                [
                    {
                        "work_item_id": "work-1",
                        "attempt_id": "attempt-1",
                        "has_result_payload": True,
                    }
                ],
            ]

        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.calls.append((" ".join(query.split()), args))
            return self._batches.pop(0)

    class SequencedPool:
        def __init__(self) -> None:
            self.connection = SequencedConnection()

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    pool = SequencedPool()
    repository = PostgresTaskInsightRepository(pool=pool)

    result = await repository.list_inspection_work_item_rows(
        task_id="task-1",
        control_mode="enforced",
    )

    assert result["work_items"][0]["status"] == "running"
    assert result["rules"][0]["rule_code"] == "alioth_1"
    assert result["attempts"][0]["has_result_payload"] is True
    queries = [call[0] for call in pool.connection.calls]
    assert "FROM qc_inspection_work_item AS work_item" in queries[0]
    assert "FROM qc_inspection_work_item_rule AS member" in queries[1]
    assert "FROM qc_inspection_attempt AS attempt" in queries[2]
    assert "result_payload <> '{}'::jsonb AS has_result_payload" in queries[2]
    assert all(call[1] == ("task-1", "enforced") for call in pool.connection.calls)


async def test_list_page_nodes_resolves_latest_supplemental_capture_chain() -> None:
    pool = FakePool([])
    repository = PostgresTaskInsightRepository(pool=pool)

    await repository.list_page_nodes(agent_run_id="run-1")

    query, args = pool.connection.calls[0]
    assert "WITH RECURSIVE supplemental_chain" in query
    assert "action.action IN ('scroll', 'timed_capture')" in query
    assert "action.status = 'explored'" in query
    assert "chain.depth DESC" in query
    assert args == ("run-1",)


async def test_app_capture_insight_queries_use_common_capture_fallbacks() -> None:
    pool = FakePool([])
    repository = PostgresTaskInsightRepository(pool=pool)

    await repository.list_page_nodes(agent_run_id="run-1")
    node_query = pool.connection.calls[0][0]
    assert "common_capture.metadata->>'page_type'" in node_query
    assert "common_capture.metadata->>'inspection_lifecycle_status'" in node_query
    assert "common_capture.metadata->>'url'" in node_query
    assert "qc_app_capture" in node_query
    assert "AS package_name" in node_query
    assert "AS width" in node_query

    pool.connection.calls.clear()
    await repository.list_screenshot_artifacts(task_id="task-1", agent_run_id="run-1")
    screenshot_query = pool.connection.calls[0][0]
    assert "common_capture.metadata->>'page_type'" in screenshot_query
    assert "visual.surface_facts" in screenshot_query
    assert "AS page_title" in screenshot_query
    assert "common_capture.metadata->>'url'" in screenshot_query
    # 探索过程展示：截图质量要带展示层评估与页面稳定性判定
    assert "common_capture.metadata->'presentation' AS presentation" in screenshot_query
    assert "'page_settle_diagnostics'" in screenshot_query
    assert "AS page_state" in screenshot_query
    assert "artifact.metadata->>'purpose' AS screenshot_purpose" in screenshot_query
    assert "artifact.deleted_at IS NULL" in screenshot_query
    assert "artifact.lifecycle_status IN ('active', 'archived')" in screenshot_query

    pool.connection.calls.clear()
    await repository.list_rule_execution_rows(task_id="task-1", agent_run_id="run-1")
    rule_query = pool.connection.calls[0][0]
    assert "common_capture.metadata->>'page_type'" in rule_query
    assert "common_capture.metadata->>'inspection_lifecycle_status'" in rule_query
    assert "qc_app_capture" in rule_query
    assert "AS capture_package_name" in rule_query
    # 沿用结论的记录自己不带证据附件：截图要按来源那次判定的显示（同任务内关联）
    assert "SELECT source.id, source.evidence_artifact_ids" in rule_query
    assert "source.task_id = execution.task_id" in rule_query
    assert "source.id = CASE" in rule_query
    assert "THEN reuse_source.evidence_artifact_ids" in rule_query


async def test_list_page_capture_inspection_rows_reads_all_dynamic_page_facts() -> None:
    pool = FakePool(
        [
            {
                "page_capture_id": "capture-1",
                "inspection_lifecycle_status": "active",
                "inspection_status": "degraded",
                "inspection_failure_code": "RULE_EXECUTION_WRITE_FAILED",
            }
        ]
    )
    repository = PostgresTaskInsightRepository(pool=pool)

    rows = await repository.list_page_capture_inspection_rows(
        task_id="task-1", agent_run_id="run-1"
    )

    query, args = pool.connection.calls[0]
    assert "FROM qc_capture AS common_capture" in query
    assert "LEFT JOIN qc_page_capture page_capture" in query
    assert "capture.inspection_lifecycle_status" in query
    assert "capture.inspection_failure_code" in query
    assert "capture.page_state" in query
    assert "FROM qc_evidence_artifact artifact" in query
    assert "AS artifact_ids" in query
    assert args == ("task-1", "run-1")
    assert rows[0]["inspection_status"] == "degraded"


async def test_count_recognized_pages_reads_common_capture_metadata() -> None:
    pool = FakePool([{"recognized_count": 2, "typed_count": 2}])
    repository = PostgresTaskInsightRepository(pool=pool)

    assert await repository.count_recognized_pages(task_id="task-1", agent_run_id="run-1") == 2

    query, args = pool.connection.calls[0]
    assert "FROM qc_capture AS common_capture" in query
    assert "common_capture.metadata->>'detection_status'" in query
    assert args == ("task-1", "run-1")


async def test_list_task_page_types_reads_common_capture_metadata() -> None:
    pool = FakePool([{"page_type": "settings"}])
    repository = PostgresTaskInsightRepository(pool=pool)

    assert await repository.list_task_page_types(task_id="task-1") == ["settings"]

    query, args = pool.connection.calls[0]
    assert "FROM qc_capture AS common_capture" in query
    assert "common_capture.metadata->>'page_type'" in query
    assert args == ("task-1",)


async def test_fetch_app_stage_aggregates_exploration_index_and_session_facts() -> None:
    class AppStageConnection(FakeConnection):
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            normalized = " ".join(query.split())
            self.calls.append((normalized, args))
            if "FROM qc_app_bootstrap" in normalized:
                return {"stage": "app_ready", "ready_at": None, "updated_at": None}
            if "FROM qc_completion_gate_snapshot" in normalized:
                return None
            if "FROM qc_inspection_work_item" in normalized:
                return {
                    "work_item_count": 2,
                    "enforced_count": 2,
                    "nonterminal_count": 0,
                    "failed_count": 0,
                }
            if "FROM qc_app_exploration_failure" in normalized:
                return None
            if "WITH latest_session" in normalized:
                return {
                    "surface_phase": "popup_chain",
                    "surface_phase_status": "completed",
                    "current_page_identity": "page:home",
                    "action_count": 3,
                    "reobservation_count": 2,
                    "coverage_summary": '{"indexed_capture_count": 6,"unique_page_count": 3}',
                    "app_target_snapshot": {
                        "target_hash": "sha256:target",
                        "required_page_types": ["home", "content_detail"],
                    },
                    "degraded_provider_facts": [
                        {
                            "status": "degraded",
                            "provider_id": "llm-app-vision",
                            "error_code": "provider_call_failed",
                            "capture_id": "capture-2",
                        }
                    ],
                    "terminal_reason_code": "exploration_completed",
                    "outcome": "exploration_completed",
                    "evidence_index_id": "index-1",
                    "evidence_index_hash": "sha256:abc",
                    "evidence_index_status": "sealed",
                    "evidence_gap_count": 0,
                    "surface_session_status": "closed",
                    "final_observation_count": 1,
                    "started_at": None,
                    "completed_at": None,
                }
            raise AssertionError(normalized)

        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            normalized = " ".join(query.split())
            self.calls.append((normalized, args))
            if "FROM qc_app_inspection_publication AS publication" in normalized:
                return []
            if "FROM qc_app_inspection_publication_entry AS entry" in normalized:
                return []
            return [
                {"status": "succeeded", "count": 2},
                {"status": "pending", "count": 1},
            ]

    class AppStagePool:
        def __init__(self) -> None:
            self.connection = AppStageConnection([])

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    pool = AppStagePool()
    repository = PostgresTaskInsightRepository(pool=pool)

    result = await repository.fetch_app_stage("task-1")

    assert result["exploration_work"] == {"succeeded": 2, "pending": 1}
    assert "probe_work" not in result
    exploration = result["app_exploration"]
    assert exploration["completed_work_count"] == 2
    assert exploration["pending_work_count"] == 1
    assert exploration["coverage_summary"]["unique_page_count"] == 3
    assert exploration["coverage_summary"]["app_target_snapshot"]["target_hash"] == (
        "sha256:target"
    )
    assert exploration["coverage_summary"]["visual_provider_status"] == "degraded"
    assert exploration["coverage_summary"]["visual_quality_satisfied"] is False
    assert (
        exploration["coverage_summary"]["degraded_provider_facts"][0]["capture_id"] == "capture-2"
    )
    query = next(query for query, _ in pool.connection.calls if "WITH latest_session" in query)
    assert "qc_app_evidence_index" in query
    assert "qc_app_evidence_index_entry" in query
    assert "qc_surface_session" in query
    assert "qc_surface_phase_run" in query
    assert "qc_action_execution" in query
    assert "business_page_identity" in query
    assert "session_state.current_page_identity" not in query
    assert "ORDER BY updated_at DESC NULLS LAST, started_at DESC, session_id" in query
    assert "created_at DESC" not in query.split("FROM qc_app_exploration_result")[0]
    assert "qc_app_visual_observation" in query
    assert "app_exploration_target" in query
    assert "semantic_role = 'surface_reobserve'" in query
    work_query = next(
        query for query, _ in pool.connection.calls if "FROM qc_v2_work_item AS work" in query
    )
    assert "work.session_id" in work_query
    assert "qc_surface_session" in work_query
    assert "work_type = 'exploration'" in work_query


async def test_fetch_app_stage_projects_technical_failure_without_business_result() -> None:
    class FailureStageConnection(FakeConnection):
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            normalized = " ".join(query.split())
            self.calls.append((normalized, args))
            if "FROM qc_app_exploration_failure" in normalized:
                return {
                    "phase": "runtime",
                    "reason_code": "DEVICE_DISCONNECTED",
                    "error_type": "ConnectionError",
                    "retryable": True,
                    "observed_at": None,
                    "recovery_state": "evidence_sealed",
                    "sealed_index_id": "index-partial",
                    "result_id": None,
                    "reconciled_at": None,
                    "surface_session_status": "lost",
                    "evidence_index_status": "sealed",
                    "evidence_index_hash": "sha256:partial",
                    "evidence_gap_count": 2,
                }
            if "FROM qc_inspection_work_item" in normalized:
                return {}
            return None

        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.calls.append((" ".join(query.split()), args))
            return []

    class FailureStagePool:
        def __init__(self) -> None:
            self.connection = FailureStageConnection([])

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    repository = PostgresTaskInsightRepository(pool=FailureStagePool())

    result = await repository.fetch_app_stage("task-1")

    exploration = result["app_exploration"]
    assert exploration["outcome"] is None
    assert exploration["terminal_reason_code"] == "DEVICE_DISCONNECTED"
    assert exploration["evidence_preserved"] is True
    assert exploration["evidence_index_id"] == "index-partial"
    assert exploration["technical_failure"]["phase"] == "runtime"
    assert exploration["recovery_state"] == "evidence_sealed"


async def test_list_exploration_actions_normalizes_frontier_keywords() -> None:
    pool = FakePool(
        [
            {
                "exploration_action_id": "exploration-1",
                "status": "failed",
                "target_keywords": '["下一步", "继续"]',
            }
        ]
    )
    repository = PostgresTaskInsightRepository(pool=pool)

    rows = await repository.list_exploration_actions(agent_run_id="run-1")

    query, args = pool.connection.calls[0]
    assert "FROM qc_page_exploration_action" in query
    assert args == ("run-1",)
    assert rows[0]["target_keywords"] == ["下一步", "继续"]


async def test_list_exploration_actions_without_run_skips_database() -> None:
    pool = FakePool([])
    repository = PostgresTaskInsightRepository(pool=pool)

    assert await repository.list_exploration_actions(agent_run_id=None) == []
    assert pool.acquire_count == 0


async def test_h5_v2_fact_queries_are_separate_and_strictly_task_run_scoped() -> None:
    pool = FakePool([])
    repository = PostgresTaskInsightRepository(pool=pool)

    await repository.list_h5_v2_work_items(task_id="task-1", agent_run_id="run-1")
    await repository.list_h5_action_executions(task_id="task-1", agent_run_id="run-1")
    await repository.list_h5_page_captures(task_id="task-1", agent_run_id="run-1")
    await repository.list_h5_page_type_assessments(task_id="task-1", agent_run_id="run-1")
    await repository.list_h5_evidence_inventory(task_id="task-1", agent_run_id="run-1")

    assert len(pool.connection.calls) == 5
    work_query, work_args = pool.connection.calls[0]
    assert "FROM qc_v2_work_item AS work" in work_query
    assert "work.task_id = $1::uuid" in work_query
    assert "WITH scoped_h5_sessions AS" in work_query
    assert "trusted.work_type = 'exploration'" in work_query
    assert (
        "trusted.work_payload #>> '{frontier,observation,metadata,agent_run_id}' = $2" in work_query
    )
    assert "work.session_id = scoped.session_id" in work_query
    assert "work.work_type IN ('exploration', 'probe')" in work_query
    assert (
        "work.work_payload #>> '{frontier,observation,metadata,agent_run_id}' = $2"
        not in work_query
    )
    assert "work.work_payload #> '{frontier,observation,metadata,h5_task_frontier}'" in work_query
    assert "'{frontier,observation,metadata,h5_browser_recovery}'" in work_query
    assert "'{frontier,observation,metadata,h5_browser_recovery_attempt}'" in work_query
    assert "'{frontier,observation,metadata,h5_browser_recovery_attempt_history}'" in work_query
    assert (
        "'{frontier,observation,metadata,h5_browser_recovery_parent_attempt_audit}'"
        in work_query
    )
    assert work_args == ("task-1", "run-1")

    action_query, action_args = pool.connection.calls[1]
    assert "FROM qc_action_execution AS action" in action_query
    assert "action.task_id = $1::uuid" in action_query
    assert "work.session_id = action.session_id" in action_query
    assert (
        "work.work_payload #>> '{frontier,observation,metadata,agent_run_id}' = $2" in action_query
    )
    assert "'discovered'::text AS origin" in action_query
    assert "expected_next_page_type" in action_query
    assert "requested_source.page_type AS source_page_type" in action_query
    assert "#> '{state_diff,logical_surface_transition}'" in action_query
    assert action_args == ("task-1", "run-1")

    capture_query, capture_args = pool.connection.calls[2]
    assert "FROM qc_capture AS common_capture" in capture_query
    assert "INNER JOIN qc_page_capture AS page_capture" in capture_query
    assert "common_capture.task_id = $1::uuid" in capture_query
    assert "common_capture.agent_run_id = $2::uuid" in capture_query
    assert "page_capture.agent_run_id = $2::uuid" in capture_query
    assert "page_capture.inspection_status" in capture_query
    assert "AS logical_surface" in capture_query
    assert "FROM qc_h5_page_type_assessment AS stored" in capture_query


    assert "common_capture.metadata #>> '{inspection,status}'" in capture_query
    assert "common_capture.metadata->>'inspection_status'" in capture_query
    assert "page_capture.metadata #>> '{page_detection,detection_status}'" in capture_query
    assert "common_capture.metadata #>> '{page_detection,confidence}'" in capture_query
    assert "page_capture.capture_role" in capture_query
    assert "{pipeline_metrics,navigation_retry}" in capture_query
    assert "{pipeline_metrics,page_settle_diagnostics,outcome}" in capture_query
    assert "AS page_settle_diagnostics" in capture_query
    assert "logical_assessment.assessment_payload->>'page_role'" in capture_query
    assert "{web_page_type_assessment,page_role}" in capture_query
    assert capture_args == ("task-1", "run-1")

    assessment_query, assessment_args = pool.connection.calls[3]
    assert "FROM qc_h5_page_type_assessment" in assessment_query
    assert "task_id = $1::uuid" in assessment_query
    assert "agent_run_id = $2::uuid" in assessment_query
    assert "PARTITION BY page_capture_id" in assessment_query
    assert "assessment_rank = 1" in assessment_query
    assert assessment_args == ("task-1", "run-1")

    inventory_query, inventory_args = pool.connection.calls[4]
    assert "FROM qc_h5_evidence_inventory_item" in inventory_query
    assert "task_id = $1::uuid" in inventory_query
    assert "agent_run_id = $2::uuid" in inventory_query
    assert inventory_args == ("task-1", "run-1")


async def test_h5_work_items_normalize_browser_recovery_audit_payloads() -> None:
    pool = FakePool(
        [
            {
                "work_id": "work-1",
                "frontier": '{"snapshot_id":"frontier-1"}',
                "browser_recovery": '{"payload_hash":"recovery-1"}',
                "browser_recovery_attempt": '{"payload_hash":"attempt-2"}',
                "browser_recovery_attempt_history": (
                    '[{"payload_hash":"attempt-1"}]'
                ),
                "browser_recovery_parent_attempt_audit": (
                    '[{"payload_hash":"attempt-1"},{"payload_hash":"attempt-2"}]'
                ),
                "terminal_payload": None,
            }
        ]
    )

    rows = await PostgresTaskInsightRepository(pool=pool).list_h5_v2_work_items(
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert rows[0]["browser_recovery"] == {"payload_hash": "recovery-1"}
    assert rows[0]["browser_recovery_attempt"] == {"payload_hash": "attempt-2"}
    assert rows[0]["browser_recovery_attempt_history"] == [
        {"payload_hash": "attempt-1"}
    ]
    assert rows[0]["browser_recovery_parent_attempt_audit"] == [
        {"payload_hash": "attempt-1"},
        {"payload_hash": "attempt-2"},
    ]


async def test_h5_action_query_projects_logical_transition_as_json_object() -> None:
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": "logical_page_changed",
        "logical_transition_proof": "trusted",
    }
    pool = FakePool(
        [
            {
                "action_execution_id": "action-logical",
                "parameters": {},
                "result_payload": {"state_diff": {}},
                "logical_surface_transition": transition,
            }
        ]
    )
    repository = PostgresTaskInsightRepository(pool=pool)

    rows = await repository.list_h5_action_executions(
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert rows[0]["logical_surface_transition"] == transition


async def test_h5_action_query_projects_model_contract_fallback_leaf() -> None:
    fallback = {
        "schema_version": "h5-explorer-decision-fallback.v1",
        "reason_code": "H5_EXPLORER_OUTPUT_INVALID",
    }
    pool = FakePool(
        [
            {
                "action_execution_id": "action-fallback",
                "parameters": {"explorer_decision_fallback": fallback},
                "result_payload": {"state_diff": {}},
                "logical_surface_transition": None,
                "visual_navigation_transition": None,
            }
        ]
    )

    rows = await PostgresTaskInsightRepository(pool=pool).list_h5_action_executions(
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert rows[0]["parameters"]["explorer_decision_fallback"] == fallback


async def test_h5_capture_query_normalizes_settle_diagnostics() -> None:
    diagnostics = {
        "outcome": "blocked",
        "blocking_reasons": ["blocking_loader_visible"],
        "sample_count": 10,
    }
    pool = FakePool(
        [
            {
                "page_capture_id": "capture-unstable",
                "visual_navigation_audit": None,
                "navigation_retry": None,
                "page_settle_diagnostics": json.dumps(diagnostics),
            }
        ]
    )

    rows = await PostgresTaskInsightRepository(pool=pool).list_h5_page_captures(
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert rows[0]["page_settle_diagnostics"] == diagnostics


async def test_surface_session_summary_is_run_scoped_and_counts_open_resources() -> None:
    pool = FakePool(
        [
            {
                "session_id": "web:run-1",
                "surface_kind": "web",
                "status": "closed",
                "command_sequence": 4,
                "current_capture_id": "capture-1",
                "failure_reason": None,
                "started_at": None,
                "updated_at": None,
                "reference_count": 2,
                "nonterminal_reference_count": 0,
            },
            {
                "session_id": "web:run-1-recovery",
                "surface_kind": "web",
                "status": "active",
                "command_sequence": 1,
                "current_capture_id": None,
                "failure_reason": None,
                "started_at": None,
                "updated_at": None,
                "reference_count": 1,
                "nonterminal_reference_count": 1,
            },
        ]
    )

    result = await PostgresTaskInsightRepository(pool=pool).fetch_surface_session_summary(
        task_id="task-1", agent_run_id="run-1"
    )

    assert result["total_count"] == 2
    assert result["terminal_count"] == 1
    assert result["nonterminal_count"] == 1
    assert result["reference_count"] == 3
    assert result["nonterminal_reference_count"] == 1
    assert result["status_counts"] == {"closed": 1, "active": 1}
    query, args = pool.connection.calls[0]
    assert "FROM qc_surface_session AS session" in query
    assert "capture.agent_run_id = $2::uuid" in query
    assert "work.work_payload #>>" in query
    assert "'{frontier,observation,metadata,agent_run_id}' = $2::text" in query
    assert "qc_surface_ref AS reference" in query
    assert args == ("task-1", "run-1")


async def test_web_assessment_reader_preserves_conflict_audit_payload() -> None:
    payload = {
        "classification_status": "classified",
        "raw_page_type": "Content_Detail",
        "proposed_page_type": "content_detail",
        "observed_page_type": "content_detail",
        "expected_page_type": "video_list",
        "conflict_reason_code": "URL_HINT_DOM_CONFLICT",
        "diagnostic_message": "目录提示与 DOM 主体冲突",
        "confidence": 0.96,
    }
    pool = FakePool(
        [
            {
                "assessment_record_id": "record-1",
                "assessment_id": "assessment-1",
                "session_id": "web:run-1",
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "content_detail",
                "page_state": "stable",
                "status": "covered",
                "reason_codes": [],
                "safety_summary": {},
                "assessment_payload": payload,
                "schema_version": "web-page-type-assessment.v2",
                "payload_hash": "hash-1",
                "row_version": 1,
            }
        ]
    )

    rows = await PostgresTaskInsightRepository(
        pool=pool
    ).list_h5_page_type_assessments(task_id="task-1", agent_run_id="run-1")

    assert rows[0]["assessment_payload"] == payload


async def test_h5_full_inspection_closure_uses_separate_run_scoped_queries() -> None:
    class ClosureConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__([])
            self._rows = [
                {
                    "inspection_count": 1,
                    "nonterminal_inspection_count": 1,
                    "failed_inspection_count": 0,
                },
                {
                    "gate_snapshot_id": "gate-1",
                    "gate_passed": False,
                    "gate_block_reasons": ["inspection_pending"],
                },
            ]

        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            self.calls.append((" ".join(query.split()), args))
            return self._rows.pop(0)

        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.calls.append((" ".join(query.split()), args))
            return []

    class ClosurePool:
        def __init__(self) -> None:
            self.connection = ClosureConnection()

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    pool = ClosurePool()
    repository = PostgresTaskInsightRepository(pool=pool)

    result = await repository.fetch_h5_full_inspection_closure(
        task_id="task-1", agent_run_id="run-1"
    )

    assert result["nonterminal_inspection_count"] == 1
    assert result["publication_projection"]["publication_count"] == 0
    assert len(pool.connection.calls) == 3
    inspection_query, inspection_args = pool.connection.calls[0]
    assert "FROM qc_inspection_work_item AS work" in inspection_query
    assert "capture.agent_run_id = $2::uuid" in inspection_query
    assert "work.producer_agent_run_id = $2::uuid" in inspection_query
    assert inspection_args == ("task-1", "run-1")
    gate_query, gate_args = pool.connection.calls[1]
    assert "gate.gate_snapshot_id = task.completion_gate_snapshot_id" in gate_query
    assert "task.finalization_agent_run_id = $2::uuid" in gate_query
    assert gate_args == ("task-1", "run-1")
    publication_query, publication_args = pool.connection.calls[2]
    assert "FROM qc_h5_inspection_publication AS publication" in publication_query
    assert "publication.finalization_epoch = task.finalization_epoch" in publication_query
    assert publication_args == ("task-1", "run-1")


def test_inspection_publisher_writes_nested_and_flat_status() -> None:
    from services.api.app.application.app_inspection_publisher import (
        build_capture_metadata_patch,
    )

    patch = build_capture_metadata_patch(
        page_type="home",
        page_state="stable",
        inspection_status="published",
        published_count=2,
    )

    assert patch["inspection"]["status"] == "published"
    assert patch["inspection_status"] == "published"


@pytest.mark.parametrize(
    ("business_status", "formal_status"),
    [
        ("unmatched_page_type", "degraded"),
        ("no_rules_applicable", "completed"),
        ("active_session_rules_excluded", "degraded"),
        ("published", "evaluating"),
    ],
)
def test_publisher_business_status_maps_to_legal_formal_status(
    business_status: str, formal_status: str
) -> None:
    assert formal_inspection_status(business_status) == formal_status
    assert formal_status in {
        "captured",
        "detecting",
        "matching",
        "evaluating",
        "completed",
        "degraded",
        "failed",
    }


async def test_inspection_publisher_updates_formal_and_compatible_metadata_in_one_transaction() -> (
    None
):
    class InspectionConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__([])
            self.transaction_entered = False

        def transaction(self) -> "InspectionTransaction":
            return InspectionTransaction(self)

        async def fetchval(self, query: str, *args: Any) -> str | None:
            self.calls.append((" ".join(query.split()), args))
            return "capture-1"

        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            self.calls.append((" ".join(query.split()), args))
            return {"capture_id": "capture-1", "target_platform": "h5"}

    class InspectionTransaction:
        def __init__(self, connection: InspectionConnection) -> None:
            self.connection = connection

        async def __aenter__(self) -> None:
            self.connection.transaction_entered = True

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

    class InspectionPool:
        def __init__(self) -> None:
            self.connection = InspectionConnection()

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    pool = InspectionPool()
    annotator = PostgresAppCaptureInspectionAnnotator(pool=pool)

    await annotator.annotate_inspection(
        task_id="task-1",
        capture_id="capture-1",
        page_type="home",
        page_state="stable",
        inspection_status="published",
        published_count=2,
    )

    assert pool.connection.transaction_entered is True
    assert len(pool.connection.calls) == 2
    formal_query, formal_args = pool.connection.calls[0]
    assert "UPDATE qc_page_capture" in formal_query
    assert "inspection_status =" in formal_query
    assert formal_args == ("capture-1", "task-1", "evaluating", "published")
    metadata_query, metadata_args = pool.connection.calls[1]
    assert "UPDATE qc_capture" in metadata_query
    patch = metadata_args[2]
    assert '"inspection_status": "published"' in patch
    assert '"inspection": {"status": "published"' in patch


@pytest.mark.parametrize(
    ("target_platform", "formal_capture_id", "raises"),
    [("h5", None, True), ("web", None, True), ("app", None, False)],
)
async def test_inspection_publisher_requires_formal_subtype_for_h5_only(
    target_platform: str, formal_capture_id: str | None, raises: bool
) -> None:
    class PlatformConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__([])

        def transaction(self) -> "InspectionTransaction":
            return InspectionTransaction(self)

        async def fetchval(self, query: str, *args: Any) -> str | None:
            self.calls.append((" ".join(query.split()), args))
            return formal_capture_id

        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            self.calls.append((" ".join(query.split()), args))
            return {"capture_id": "capture-1", "target_platform": target_platform}

    class InspectionTransaction:
        def __init__(self, connection: PlatformConnection) -> None:
            self.connection = connection

        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

    class PlatformPool:
        def __init__(self) -> None:
            self.connection = PlatformConnection()

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    annotator = PostgresAppCaptureInspectionAnnotator(pool=PlatformPool())
    operation = annotator.annotate_inspection(
        task_id="task-1",
        capture_id="capture-1",
        page_type="home",
        page_state="stable",
        inspection_status="published",
        published_count=1,
    )

    if raises:
        with pytest.raises(RuntimeError, match="缺少 qc_page_capture 正式子类型"):
            await operation
    else:
        await operation


async def test_list_rule_execution_rows_reads_diagnostic_reason() -> None:
    pool = FakePool(
        [
            {
                "rule_code": "alioth_16",
                "applicable_page_types": '["video_list"]',
                "evidence_artifact_ids": "[]",
                "evidence_input_set_id": "evidence-input-set:h5",
                "missing_evidence": ["after_action_screenshot"],
                "confidence": 0,
                "diagnostic_code": "semantic_provider_http_503",
            }
        ]
    )
    repository = PostgresTaskInsightRepository(pool=pool)

    rows = await repository.list_rule_execution_rows(
        task_id="task-1",
        agent_run_id="run-1",
    )

    query, args = pool.connection.calls[0]
    assert "execution.review_reason" in query
    assert "execution.error_code" in query
    assert "AS diagnostic_code" in query
    assert "AS missing_evidence" in query
    assert "execution.evidence_input_set_id::text" in query
    assert "AS execution_reason" in query
    assert "run_budget_exhausted" in query
    assert "upper(COALESCE(" in query
    assert "common_capture.metadata->>'inspection_failure_code'" in query
    assert "candidate_not_executed" in query
    assert "superseded" in query
    assert args == ("task-1", "run-1")
    assert rows[0]["diagnostic_code"] == "semantic_provider_http_503"
    assert rows[0]["missing_evidence"] == ["after_action_screenshot"]
    assert rows[0]["evidence_input_set_id"] == "evidence-input-set:h5"


async def test_fetch_rule_execution_reuses_derived_execution_reason_sql() -> None:
    pool = FakePool(
        [
            {
                "execution_id": "execution-1",
                "task_id": "task-1",
                "execution_status": "manual_review",
                "execution_reason": None,
                "review_status": "pending",
                "is_bootstrap": False,
                "human_verdict": None,
                "rule_code": "alioth_4",
                "rule_name": "错别字检查",
            }
        ]
    )
    repository = PostgresTaskInsightRepository(pool=pool)

    row = await repository.fetch_rule_execution("execution-1")

    query, args = pool.connection.calls[0]
    assert "execution.execution_reason" not in query
    assert "execution.judge_output->>'reason'" in query
    assert "LEFT JOIN qc_page_capture capture" in query
    assert "AS execution_reason" in query
    assert args == ("execution-1",)
    assert row is not None
    assert row["execution_status"] == "manual_review"


class FakeTransaction:
    def __init__(self, connection: "VerdictConnection") -> None:
        self.connection = connection

    async def __aenter__(self) -> None:
        self.connection.transaction_entered = True

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class VerdictConnection:
    def __init__(self, completed_task_status: str | None) -> None:
        self.completed_task_status = completed_task_status
        self.task_row_version = 2
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_entered = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if compact.startswith("SELECT") and "FROM qc_task" in compact:
            return {
                "task_id": str(args[0]),
                "status": "review",
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        if compact.startswith("UPDATE qc_task"):
            self.task_row_version += 1
            return {
                "task_id": str(args[0]),
                "status": str(args[3]),
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        return {
            "execution_id": "execution-1",
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "human_verdict": "pass",
            "human_verdict_note": "确认通过",
            "human_verdict_at": None,
            "review_status": "approved",
        }

    async def fetchval(self, query: str, *args: Any) -> Any:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if "SELECT status FROM qc_task" in compact:
            return "review"
        if "SELECT COUNT(*) FROM qc_rule_execution" in compact:
            return 0
        if "SELECT EXISTS" in compact and "FROM qc_agent_run" in compact:
            return self.completed_task_status is not None
        raise AssertionError(f"未预期的 fetchval 查询：{compact}")

    async def execute(self, query: str, *args: Any) -> None:
        self.calls.append((" ".join(query.split()), args))


class VerdictPool:
    def __init__(self, completed_task_status: str | None) -> None:
        self.connection = VerdictConnection(completed_task_status)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


async def test_last_human_verdict_closes_review_task_after_completed_run() -> None:
    pool = VerdictPool("done")
    repository = PostgresTaskInsightRepository(pool=pool)

    result = await repository.update_human_verdict(
        execution_id="execution-1",
        verdict="pass",
        note="确认通过",
        operator="reviewer",
    )

    queries = [query for query, _args in pool.connection.calls]
    assert pool.connection.transaction_entered is True
    assert any("FOR UPDATE" in query for query in queries)
    task_lock_index = next(
        index for index, query in enumerate(queries) if "FOR UPDATE OF task" in query
    )
    execution_update_index = next(
        index for index, query in enumerate(queries) if query.startswith("UPDATE qc_rule_execution")
    )
    review_update_index = next(
        index for index, query in enumerate(queries) if query.startswith("UPDATE qc_review_item")
    )
    assert task_lock_index < execution_update_index < review_update_index
    assert "task.deleted_at IS NULL" in queries[task_lock_index]
    # 按质检规则统一口径：待确认数不分 Run，被同规则真实结论顶掉的占位不算
    assert not any("agent_run_id IS NOT DISTINCT FROM" in query for query in queries)
    assert any("FROM qc_rule_execution AS concluded" in query for query in queries)
    assert any("run.status = 'done'" in query for query in queries)
    task_update_query = next(query for query in queries if query.startswith("UPDATE qc_task"))
    assert "row_version = $3" in task_update_query
    assert "finalization_state <> 'sealed'" in task_update_query
    assert result is not None
    assert result["task_status"] == "done"


async def test_last_human_verdict_does_not_close_incomplete_run() -> None:
    pool = VerdictPool(None)
    repository = PostgresTaskInsightRepository(pool=pool)

    result = await repository.update_human_verdict(
        execution_id="execution-1",
        verdict="fail",
        note=None,
        operator=None,
    )

    assert result is not None
    assert result["task_status"] == "review"
