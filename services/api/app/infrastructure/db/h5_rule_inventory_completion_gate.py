"""全规则应用清单的完成门禁：未形成真实处理记录的适用规则不能静默消失。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from services.api.app.application.task_rule_inventory import (
    TaskRuleInventoryError,
    inventory_execution_contract_hash,
    parse_task_rule_inventory,
    requires_task_rule_inventory,
)
from services.api.app.domain.task.completion import CompletionGateFacts
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.rule_completion_sql import completed_rule_execution_sql
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    _capture_page_type_sql,
    _occluded_placeholder_sql,
    _placeholder_sql,
)


async def apply_h5_rule_inventory_gate(
    connection: Any,
    *,
    task_id: str,
    row: Mapping[str, Any],
    facts: CompletionGateFacts,
) -> CompletionGateFacts:
    """清单缺口叠加既有候选非终态门禁，不把清单项写成规则执行。"""

    config = _mapping(row.get("inventory_task_config"))
    if row.get("inventory_target_platform") != "h5" or not requires_task_rule_inventory(
        "h5", config
    ):
        return facts
    # 历史任务没有提交时清单，保持原门禁审计；读侧另行验证历史规则是否可重建。
    if "rule_inventory" not in config:
        return facts
    try:
        inventory = parse_task_rule_inventory(config)
        if inventory is None or (
            inventory["ruleset_id"] != str(row.get("inventory_ruleset_id"))
            or inventory["target_platform"] != "h5"
            or inventory["channel"] != row.get("inventory_channel")
        ):
            raise TaskRuleInventoryError("规则清单与任务身份不一致")
    except (TaskRuleInventoryError, KeyError):
        # 损坏清单不能当成空集通过；已有门禁原因明确记录候选未完成。
        return replace(facts, candidate_rule_count=facts.candidate_rule_count + 1)
    required = {
        rule["rule_id"]
        for rule in inventory["rules"]
        if rule["applicability"] not in {"not_applicable", "out_of_scope"}
    }
    if not required:
        return replace(facts, no_rules_applicable=True)
    rows = await connection.fetch(
        f"""
        WITH scoped_executions AS (
        SELECT execution.rule_id::text AS rule_id,
               (
                   {completed_rule_execution_sql("execution")}
                   AND COALESCE(execution.judge_output->>'reason', '') NOT IN (
                       'auth_requirement_not_met', 'supplemental_evidence_disabled',
                       'supplemental_runtime_unavailable', 'request_failed', 'request_expired',
                       'request_cancelled', 'request_canceled', 'evidence_insufficient'
                   )
                   AND (
                       aggregate_work.rule_contract_hash = $3::jsonb->>execution.rule_id::text
                       OR EXISTS (
                           SELECT 1 FROM qc_inspection_work_item_rule AS member
                           JOIN qc_inspection_work_item AS work ON work.id = member.work_item_id
                           WHERE member.rule_execution_id = execution.id
                             AND member.rule_id = execution.rule_id
                             AND member.contract_hash = $3::jsonb->>execution.rule_id::text
                             AND work.task_id = task.id
                             AND work.page_capture_id = execution.page_capture_id
                             AND work.producer_agent_run_id = task.finalization_agent_run_id
                             AND work.finalization_epoch = task.finalization_epoch
                             AND work.control_mode = 'enforced'
                             AND work.status <> 'superseded'
                       )
                   )
               ) AS completed,
               {_placeholder_sql("execution")} AS is_placeholder,
               {_occluded_placeholder_sql("execution")} AS is_occluded,
               {_capture_page_type_sql("execution")} AS page_type
        FROM qc_task AS task
        JOIN qc_rule_execution AS execution ON execution.task_id = task.id
        CROSS JOIN LATERAL jsonb_array_elements(
            task.task_config #> '{{rule_inventory,rules}}'
        ) AS frozen_rule
        LEFT JOIN qc_capture AS capture ON capture.id = execution.page_capture_id
        LEFT JOIN qc_aggregate_inspection_work_item AS aggregate_work
          ON aggregate_work.id = execution.aggregate_work_item_id
         AND aggregate_work.task_id = task.id
         AND aggregate_work.rule_id = execution.rule_id
        WHERE task.id = $1::uuid
          AND execution.rule_id::text = ANY($2::text[])
          AND frozen_rule->>'rule_id' = execution.rule_id::text
          AND (NOT (execution.judge_input ? 'finalization_epoch')
               OR execution.judge_input->>'finalization_epoch' = task.finalization_epoch::text)
          AND (
              (
                  capture.task_id = task.id
                  AND capture.agent_run_id = task.finalization_agent_run_id
                  AND ({capture_epoch_sql("capture")}) = task.finalization_epoch
              )
              OR (
                  aggregate_work.producer_agent_run_id = task.finalization_agent_run_id
                  AND aggregate_work.finalization_epoch = task.finalization_epoch
              )
          )
        ), effective_executions AS (
            SELECT source.* FROM scoped_executions AS source
            WHERE source.is_placeholder IS NOT TRUE
               OR NOT EXISTS (
                   SELECT 1 FROM scoped_executions AS concluded
                   WHERE concluded.rule_id = source.rule_id
                     AND concluded.completed IS TRUE
                     AND concluded.is_placeholder IS NOT TRUE
                     AND (source.is_occluded IS NOT TRUE
                          OR source.page_type = concluded.page_type)
               )
        )
        SELECT rule_id FROM effective_executions
        GROUP BY rule_id HAVING bool_and(completed IS TRUE)
        """,
        task_id,
        sorted(required),
        json.dumps(
            {
                rule["rule_id"]: inventory_execution_contract_hash(rule)
                for rule in inventory["rules"]
                if rule["rule_id"] in required
            }
        ),
    )
    missing = len(required - {str(item["rule_id"]) for item in rows})
    return replace(facts, candidate_rule_count=facts.candidate_rule_count + missing)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, Mapping) else {}
