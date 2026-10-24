"""APP 正式聚合不适用处置的同源只读核验，供清单门禁及报告复用。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.aggregate_rule_config import (
    resolve_rule_evidence_input_configuration,
)
from services.api.app.domain.inspection.rule_scope import RuleScopeStatus, build_rule_scope_plan
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    read_app_evidence_index_on_connection,
)


async def read_app_aggregate_not_applicable_dispositions(
    connection: Any, *, task_id: str, inventory: Mapping[str, Any],
    source_agent_run_id: str | None = None, finalization_epoch: int | None = None,
    allow_sealed: bool = False,
) -> list[dict[str, Any]]:
    """只认可同封存材料重新生成的范围计划，不凭 Candidate 标签补完成。"""

    rows = await connection.fetch(
        """
        WITH target AS (
            SELECT task.*, gate.gate_passed,
                   COALESCE(task.finalization_agent_run_id, (
                       SELECT run.id FROM qc_agent_run AS run
                       WHERE run.task_id=task.id AND run.agent_type='main'
                       ORDER BY run.created_at DESC,run.id DESC LIMIT 1
                   )) AS source_run_id,
                   CASE WHEN task.finalization_state IN ('finalizing','sealed')
                              AND gate.gate_passed AND task.finalization_epoch>0
                        THEN task.finalization_epoch-1 ELSE task.finalization_epoch
                   END AS evidence_epoch
            FROM qc_task AS task
            LEFT JOIN qc_completion_gate_snapshot AS gate
              ON gate.task_id=task.id AND gate.gate_snapshot_id=task.completion_gate_snapshot_id
             AND gate.finalization_epoch=task.finalization_epoch
            WHERE task.id=$1::uuid AND task.target_platform='app' AND task.deleted_at IS NULL
              AND task.status<>'canceled'
              AND (task.finalization_state='session_draining' OR ($4::boolean
                   AND task.finalization_state IN ('finalizing','sealed') AND gate.gate_passed))
        )
        SELECT candidate.*, target.source_run_id::text AS verified_run_id,
               target.evidence_epoch AS verified_epoch
        FROM target
        JOIN qc_agent_run AS owner ON owner.id=target.source_run_id AND owner.task_id=target.id
        JOIN qc_aggregate_inspection_candidate AS candidate
          ON candidate.task_id=target.id AND candidate.source_agent_run_id=owner.id
         AND candidate.finalization_epoch=target.evidence_epoch
        WHERE ($2::uuid IS NULL OR owner.id=$2::uuid)
          AND ($3::integer IS NULL OR target.evidence_epoch=$3)
          AND COALESCE(owner.output_context->>'cancel_requested','false')<>'true'
          AND (target.finalization_agent_run_id IS NOT NULL OR (
              $4::boolean AND target.finalization_state IN ('finalizing','sealed')
              AND target.gate_passed))
          AND candidate.status='not_applicable'
          AND candidate.disposition_code='AGGREGATE_RULE_SCOPE_NOT_APPLICABLE'
          AND candidate.expected_member_count=0 AND candidate.finished_at IS NOT NULL
          AND length(trim(COALESCE(candidate.disposition_message,'')))>0
          AND candidate.work_item_id IS NULL AND candidate.evidence_input_set_id IS NULL
        """,
        task_id, source_agent_run_id, finalization_epoch, allow_sealed,
    )
    if not rows:
        return []
    run_id, epoch = str(rows[0]["verified_run_id"]), int(rows[0]["verified_epoch"])
    authority_row = await connection.fetchrow(
        """SELECT result.task_id::text AS task_id,result.agent_run_id::text AS source_agent_run_id,
                  $3::integer AS finalization_epoch,
                  result.surface_session_id AS app_scope_session_id,
                  result.evidence_index_id AS app_scope_evidence_index_id,
                  result.protocol_version AS app_scope_protocol_version,
                  result.outcome AS app_scope_outcome,
                  result.terminal_reason_code AS app_scope_terminal_reason_code,
                  result.evidence_index_hash AS app_scope_evidence_index_hash,
                  result.result_sha256 AS app_scope_hash,
                  result.coverage_summary AS app_coverage_summary,
                  result.evidence_gaps AS app_scope_evidence_gaps,
                  result.started_at AS app_scope_started_at,
                  result.completed_at AS app_scope_completed_at
           FROM qc_app_exploration_result AS result
           JOIN qc_app_evidence_index AS index ON index.task_id=result.task_id
             AND index.agent_run_id=result.agent_run_id AND index.status='sealed'
             AND index.index_id=result.evidence_index_id
             AND index.content_sha256=result.evidence_index_hash
             AND index.surface_session_id=result.surface_session_id
           JOIN qc_surface_session AS session ON session.task_id=result.task_id
             AND session.session_id=result.surface_session_id
             AND session.platform_build_ref=index.build_id
           JOIN qc_task AS task ON task.id=result.task_id
           WHERE result.task_id=$1::uuid AND result.agent_run_id=$2::uuid
             AND session.status IN ('closed','lost','failed')
             AND session.lease_owner IS NULL AND session.lease_token IS NULL
             AND session.lease_expires_at IS NULL
             AND (session.finalization_epoch=$3 OR ($4::boolean
                  AND task.finalization_state IN ('finalizing','sealed')
                  AND session.finalization_epoch=task.finalization_epoch))""",
        task_id, run_id, epoch, allow_sealed,
    )
    if authority_row is None:
        return []
    try:
        # 延迟引用正式发布器使用的纯解析器；不调用其连接或带锁的快照读取。
        from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
            _scope_authority_from_task,
        )

        authority = _scope_authority_from_task(authority_row)
        index = await read_app_evidence_index_on_connection(connection, run_id=run_id)
        if authority is None or index is None or index.task_id != task_id:
            return []
    except (ValueError, TypeError, KeyError):
        return []
    frozen_rules = {rule["rule_id"]: rule for rule in inventory["rules"]}
    verified = []
    for candidate in rows:
        rule = frozen_rules.get(str(candidate["rule_id"]))
        if rule is None or rule.get("applicability") != "applicable":
            continue
        try:
            contract = resolve_rule_evidence_input_configuration(rule.get("judge_config"))
            plan = build_rule_scope_plan(
                authority=authority, rule_id=rule["rule_id"],
                applicable_page_types=tuple(rule.get("applicable_page_types") or ()),
            )
            payload = candidate["rule_scope_plan"]
            payload = json.loads(payload) if isinstance(payload, str) else payload
        except (ValueError, TypeError, KeyError):
            continue
        if (
            not contract.is_aggregate or candidate["scope"] != contract.scope.value
            or candidate["scope_key"] != contract.scope_key
            or candidate["rule_code"] != rule["rule_code"]
            or candidate["rule_contract_hash"] != rule["contract_hash"]
            or plan.status is not RuleScopeStatus.NOT_APPLICABLE
            or candidate["rule_scope_plan_hash"] != plan.plan_hash or payload != plan.to_dict()
        ):
            continue
        verified.append({
            "rule_id": rule["rule_id"], "candidate_id": str(candidate["id"]),
            "status": "not_applicable", "reason_code": candidate["disposition_code"],
            "reason": candidate["disposition_message"],
            "evidence_refs": list(plan.negative_evidence_refs),
        })
    return verified
