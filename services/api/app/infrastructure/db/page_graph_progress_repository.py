"""从既有采集、质检和 Frontier 事实读取树进度，不写入第二份覆盖结果。"""

from __future__ import annotations

import json
from typing import Any


async def read_page_graph_progress(
    connection: Any, *, task_id: str, agent_run_id: str, finalization_epoch: int | None
) -> dict[str, Any]:
    """与图查询共用数据库快照，保证节点和进度来自同一次读取。"""
    rows = await connection.fetch(_CAPTURE_PROGRESS_SQL, task_id, agent_run_id, finalization_epoch)
    app_candidates = await connection.fetch(
        """
        SELECT frontier.frontier_item_id AS candidate_id,
            frontier.source_capture_id::text AS source_capture_id,
            frontier.semantic_target, frontier.status,
            frontier.last_error_code AS reason_code
        FROM qc_app_exploration_frontier AS frontier
        JOIN qc_capture AS capture ON capture.id = frontier.source_capture_id
          AND capture.task_id = frontier.task_id
          AND capture.agent_run_id = frontier.agent_run_id
          AND capture.surface_session_id = frontier.surface_session_id
        JOIN qc_page_graph_state AS state ON state.state_id = capture.id
          AND state.task_id = capture.task_id AND state.agent_run_id = capture.agent_run_id
          AND state.surface_session_id = capture.surface_session_id
        WHERE frontier.task_id = $1::uuid AND frontier.agent_run_id = $2::uuid
          AND state.finalization_epoch IS NOT DISTINCT FROM $3::integer
        ORDER BY frontier.discovered_at, frontier.frontier_item_id
        """,
        task_id,
        agent_run_id,
        finalization_epoch,
    )
    h5_frontiers = await connection.fetch(
        """
        SELECT DISTINCT ON (work.session_id) work.session_id,
            work.work_payload #> '{frontier,observation,metadata,h5_task_frontier}' AS frontier
        FROM qc_v2_work_item AS work
        JOIN qc_surface_session AS session ON session.task_id = work.task_id
          AND session.session_id = work.session_id AND session.surface_kind = 'web'
        WHERE work.task_id = $1::uuid
          AND work.work_type IN ('exploration', 'probe')
          AND work.work_payload #>> '{frontier,observation,metadata,agent_run_id}' = $2
          AND work.finalization_epoch IS NOT DISTINCT FROM $3::integer
          AND session.finalization_epoch IS NOT DISTINCT FROM $3::integer
          AND jsonb_typeof(work.work_payload #>
              '{frontier,observation,metadata,h5_task_frontier}') = 'object'
        ORDER BY work.session_id, work.created_at DESC, work.work_id DESC
        """,
        task_id,
        agent_run_id,
        finalization_epoch,
    )
    candidates = [dict(row) for row in app_candidates]
    scoped_capture_ids = {str(row["capture_id"]) for row in rows}
    for row in h5_frontiers:
        for entry in _object(row["frontier"]).get("entries", []):
            if not isinstance(entry, dict):
                continue
            # Frontier 会继承历史候选；当前工作轮次正确不代表候选来源也属于该轮次。
            if str(entry.get("source_capture_id")) not in scoped_capture_ids:
                continue
            candidate = _object(entry.get("candidate"))
            candidates.append(
                {
                    "candidate_id": entry.get("entry_id"),
                    "source_capture_id": entry.get("source_capture_id"),
                    "semantic_target": candidate.get("semantic_target"),
                    "status": entry.get("status"),
                    "reason_code": entry.get("last_failure_reason"),
                }
            )
    return {
        "progress_facts": [_progress_fact(dict(row)) for row in rows],
        "candidates": candidates,
        "progress_source": "capture_manifest_inspection_frontier",
    }


def _progress_fact(row: dict[str, Any]) -> dict[str, Any]:
    """文件质量通过不是证据充分，规则失败也不是证据缺失。"""
    reasons = []
    evidence_status = "unknown"
    if row.get("unresolved_evidence_requests") or row.get("rule_evidence_gaps"):
        evidence_status = "insufficient"
        reasons.append("unresolved_rule_evidence")
    elif row.get("quality_status") == "fail":
        evidence_status = "failed"
        reasons.append("manifest_quality_failed")
    elif row.get("inventory_status") in {"failed", "insufficient"}:
        evidence_status = "insufficient"
        reasons.append("inventory_evidence_gap")
    elif row.get("quality_status") == "pass" and row.get("inspection_status") in {
        "completed",
        "no_rules_applicable",
    }:
        evidence_status = "complete"
    inspection = row.get("inspection_status") or "unknown"
    if inspection == "no_rules_applicable":
        inspection = "completed"
    if inspection == "completed" and row.get("unfinished_rules"):
        inspection = "evaluating"
        reasons.append("rule_evaluation_not_completed")
    # App 的 published 表示已经发出规则工作，而不是规则执行完毕。
    expected = row.get("published_count")
    if inspection == "published" and isinstance(expected, int) and expected > 0:
        if row.get("completed_rule_count", 0) >= expected and not row.get("unfinished_rules"):
            inspection = "completed"
            if evidence_status == "unknown" and row.get("quality_status") == "pass":
                evidence_status = "complete"
    blocked = inspection in {"failed", "degraded", "unmatched_page_type"}
    if blocked:
        reasons.append(row.get("inspection_failure_code") or "inspection_not_completed")
    return {
        "capture_id": row["capture_id"],
        "evidence_status": evidence_status,
        "inspection_status": inspection,
        "exploration_finished": row.get("exploration_finished") is True,
        "blocked": blocked,
        "reason_codes": reasons,
    }


def _object(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


_CAPTURE_PROGRESS_SQL = """
SELECT state.state_id::text AS capture_id,
    COALESCE(page.inspection_status, capture.metadata #>> '{inspection,status}',
        capture.metadata->>'inspection_status') AS inspection_status,
    COALESCE(page.inspection_failure_code,
        capture.metadata->>'inspection_failure_code') AS inspection_failure_code,
    CASE WHEN capture.metadata #>> '{inspection,published_count}' ~ '^[0-9]{1,8}$'
        THEN (capture.metadata #>> '{inspection,published_count}')::integer END AS published_count,
    manifest.quality_status, inventory.status AS inventory_status,
    EXISTS (
        SELECT 1 FROM qc_evidence_request AS request
        WHERE request.task_id = state.task_id AND request.target_capture_id = state.state_id
          AND request.finalization_epoch IS NOT DISTINCT FROM state.finalization_epoch
          AND request.status IN ('pending', 'routed', 'running', 'unfulfillable', 'expired')
    ) AS unresolved_evidence_requests,
    rules.rule_evidence_gaps, rules.completed_rule_count, rules.unfinished_rules,
    COALESCE(coverage.frontier_terminal, result.completed_at IS NOT NULL, false)
        AS exploration_finished
FROM qc_page_graph_state AS state
JOIN qc_capture AS capture ON capture.id = state.state_id
  AND capture.task_id = state.task_id AND capture.agent_run_id = state.agent_run_id
LEFT JOIN qc_page_capture AS page ON page.id = capture.id
LEFT JOIN LATERAL (
    SELECT stored.quality_status FROM qc_evidence_manifest AS stored
    WHERE stored.task_id = state.task_id AND stored.capture_id = state.state_id
      AND stored.session_id = state.surface_session_id
    ORDER BY stored.manifest_version DESC, stored.persisted_at DESC LIMIT 1
) AS manifest ON TRUE
LEFT JOIN LATERAL (
    SELECT stored.status FROM qc_h5_evidence_inventory_item AS stored
    WHERE stored.task_id = state.task_id AND stored.agent_run_id = state.agent_run_id
      AND stored.session_id = state.surface_session_id
      AND state.state_id IN (stored.before_capture_id, stored.after_capture_id)
    ORDER BY stored.updated_at DESC, stored.inventory_item_id DESC LIMIT 1
) AS inventory ON TRUE
LEFT JOIN LATERAL (
    SELECT bool_or(latest.execution_status = 'evidence_insufficient') AS rule_evidence_gaps,
        count(*) FILTER (WHERE latest.execution_status IN ('pass', 'fail', 'warning'))
            AS completed_rule_count,
        bool_or(latest.execution_status NOT IN ('pass', 'fail', 'warning')) AS unfinished_rules
    FROM (
        SELECT DISTINCT ON (execution.rule_id) execution.execution_status
        FROM qc_rule_execution AS execution
        WHERE execution.task_id = state.task_id AND execution.agent_run_id = state.agent_run_id
          AND execution.page_capture_id = state.state_id
        ORDER BY execution.rule_id, execution.updated_at DESC, execution.id DESC
    ) AS latest
) AS rules ON TRUE
LEFT JOIN LATERAL (
    SELECT snapshot.frontier_terminal FROM qc_h5_coverage_snapshot AS snapshot
    WHERE snapshot.task_id = state.task_id AND snapshot.agent_run_id = state.agent_run_id
      AND snapshot.finalization_epoch IS NOT DISTINCT FROM state.finalization_epoch
    ORDER BY snapshot.revision DESC LIMIT 1
) AS coverage ON TRUE
LEFT JOIN qc_app_exploration_result AS result ON result.task_id = state.task_id
  AND result.agent_run_id = state.agent_run_id
  AND result.surface_session_id = state.surface_session_id
  AND EXISTS (
      SELECT 1 FROM qc_app_evidence_index_entry AS entry
      WHERE entry.index_id = result.evidence_index_id AND entry.task_id = state.task_id
        AND entry.capture_id = state.state_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM qc_app_evidence_index_entry AS entry
      LEFT JOIN qc_page_graph_state AS indexed ON indexed.state_id = entry.capture_id
        AND indexed.task_id = state.task_id AND indexed.agent_run_id = state.agent_run_id
        AND indexed.surface_session_id = state.surface_session_id
      WHERE entry.index_id = result.evidence_index_id
        AND (indexed.state_id IS NULL
          OR indexed.finalization_epoch IS DISTINCT FROM state.finalization_epoch)
  )
WHERE state.task_id = $1::uuid AND state.agent_run_id = $2::uuid
  AND state.finalization_epoch IS NOT DISTINCT FROM $3::integer
ORDER BY state.captured_at, state.state_id
"""
