"""全规则应用清单的完成门禁：未形成真实处理记录的适用规则不能静默消失。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from services.api.app.application.task_rule_inventory import (
    TaskRuleInventoryError,
    parse_task_rule_inventory,
    requires_task_rule_inventory,
)
from services.api.app.domain.task.completion import CompletionGateFacts
from services.api.app.infrastructure.db.app_aggregate_dispositions import (
    read_app_aggregate_not_applicable_dispositions,
)
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.rule_completion_sql import completed_rule_execution_sql
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    _capture_page_type_sql,
    _occluded_placeholder_sql,
    _placeholder_sql,
)


async def apply_app_rule_inventory_gate(
    connection: Any,
    *,
    task_id: str,
    row: Mapping[str, Any],
    facts: CompletionGateFacts,
) -> CompletionGateFacts:
    """清单缺口叠加既有候选非终态门禁，不把清单项写成规则执行。"""

    config = _mapping(row.get("inventory_task_config"))
    if row.get("inventory_target_platform") != "app" or not requires_task_rule_inventory(
        "app", config,
    ):
        return facts
    # 对应脚本整份跑完才放行。规则还在更新时不封口；停更后才按手动封口
    # 放过清单里没走到的规则。
    if await _recorded_script_seal_ready(connection, task_id=task_id):
        return facts
    # 历史任务没有提交时清单，保持原门禁审计；读侧另行验证历史规则是否可重建。
    if "rule_inventory" not in config:
        return facts
    try:
        inventory = parse_task_rule_inventory(config)
        if inventory is None or (
            inventory["ruleset_id"] != str(row.get("inventory_ruleset_id"))
            or inventory["target_platform"] != "app"
            or inventory["channel"] != row.get("inventory_channel")
        ):
            raise TaskRuleInventoryError("规则清单与任务身份不一致")
    except (TaskRuleInventoryError, KeyError):
        # 损坏清单不能当成空集通过；已有门禁原因明确记录候选未完成。
        return replace(facts, candidate_rule_count=facts.candidate_rule_count + 1)
    required = {
        rule["rule_id"] for rule in inventory["rules"]
        if rule["applicability"] not in {"not_applicable", "out_of_scope"}
    }
    if not required:
        return replace(facts, no_rules_applicable=True)
    rows = await connection.fetch(
        f"""
        WITH scoped_executions AS (
        SELECT execution.rule_id::text AS rule_id,
               {completed_rule_execution_sql('execution')} AS completed,
               {_placeholder_sql('execution')} AS is_placeholder,
               {_occluded_placeholder_sql('execution')} AS is_occluded,
               {_capture_page_type_sql('execution')} AS page_type
        FROM qc_task AS task
        JOIN qc_rule_execution AS execution ON execution.task_id = task.id
        LEFT JOIN qc_capture AS capture ON capture.id = execution.page_capture_id
        LEFT JOIN qc_aggregate_inspection_work_item AS aggregate_work
          ON aggregate_work.id = execution.aggregate_work_item_id
         AND aggregate_work.task_id = task.id
         AND aggregate_work.rule_id = execution.rule_id
        WHERE task.id = $1::uuid
          AND execution.rule_id::text = ANY($2::text[])
          AND (NOT (execution.judge_input ? 'finalization_epoch')
               OR execution.judge_input->>'finalization_epoch' = task.finalization_epoch::text)
          AND (
              (
                  capture.task_id = task.id
                  AND capture.agent_run_id = task.finalization_agent_run_id
                  AND ({capture_epoch_sql('capture')}) = task.finalization_epoch
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
        SELECT rule_id,bool_and(completed IS TRUE) AS completed FROM effective_executions
        GROUP BY rule_id
        """,
        task_id,
        sorted(required),
    )
    completed = {str(item["rule_id"]) for item in rows if item["completed"]}
    dispositions = await read_app_aggregate_not_applicable_dispositions(
        connection, task_id=task_id, inventory=inventory,
    )
    # 正式不适用处置无需制造 Judge；已有真实检测缺口仍不能被处置盖掉。
    completed.update(
        item["rule_id"] for item in dispositions
        if item["rule_id"] not in {str(execution["rule_id"]) for execution in rows}
    )
    missing = len(required - completed)
    return replace(facts, candidate_rule_count=facts.candidate_rule_count + missing)


async def _recorded_script_seal_ready(connection: Any, *, task_id: str) -> bool:
    """对应脚本已整份跑完，且规则、质检工作项已停更至少 3 分钟。"""

    ready = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_exploration_result AS result
            INNER JOIN qc_task AS task ON task.id = result.task_id
            WHERE result.task_id = $1::uuid
              AND result.outcome = 'exploration_completed'
              AND result.agent_run_id = task.finalization_agent_run_id
              AND (
                  result.coverage_summary->>'hlapp_script_completed' = 'true'
                  OR result.coverage_summary->>'hjll_script_completed' = 'true'
                  OR result.coverage_summary->>'hlwapp_script_completed' = 'true'
                  OR result.coverage_summary->>'jmcomic_script_completed' = 'true'
                  OR result.coverage_summary->>'jinguo_script_completed' = 'true'
                  OR result.coverage_summary->>'yms_script_completed' = 'true'
                  OR result.coverage_summary->>'pzhan_script_completed' = 'true'
                  OR result.coverage_summary->>'pc51_script_completed' = 'true'
                  OR result.coverage_summary->>'hlwwhite_script_completed' = 'true'
                  OR result.coverage_summary->>'head_script_completed' = 'true'
                  OR result.coverage_summary->>'xingba_script_completed' = 'true'
                  OR result.coverage_summary->>'latest_script_completed' = 'true'
                  OR result.coverage_summary->>'mama_script_completed' = 'true'
              )
              AND result.coverage_summary->>'coverage_complete' = 'true'
              AND result.coverage_summary->>'visual_quality_satisfied' = 'true'
              AND NOT EXISTS (
                  SELECT 1
                  FROM qc_rule_execution AS rule_activity
                  WHERE rule_activity.task_id = task.id
                    AND rule_activity.updated_at > now() - interval '3 minutes'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM qc_inspection_work_item AS work_activity
                  WHERE work_activity.task_id = task.id
                    AND work_activity.updated_at > now() - interval '3 minutes'
              )
        )
        """,
        task_id,
    )
    return bool(ready)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, Mapping) else {}
