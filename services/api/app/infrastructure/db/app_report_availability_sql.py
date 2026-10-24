"""APP 报告可用性只读投影，区分当前成果预览与正式绑定。"""

import re

from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.rule_result_scope_sql import _placeholder_sql

APP_FORMAL_REPORT_AVAILABLE_SQL = """
(
    task.target_platform = 'app'
    AND COALESCE(task.task_config->>'execution_mode', 'full_inspection') = 'full_inspection'
    AND task.finalization_state = 'sealed'
    AND task.finalization_agent_run_id = agent_run.id
    AND EXISTS (
        SELECT 1
        FROM qc_report_gate_binding AS binding
        INNER JOIN qc_completion_gate_snapshot AS gate
          ON gate.task_id = binding.task_id
         AND gate.gate_snapshot_id = binding.gate_snapshot_id
         AND gate.finalization_epoch = binding.finalization_epoch
         AND gate.gate_passed
        INNER JOIN qc_report_payload_snapshot AS payload
          ON payload.report_id = binding.report_id
         AND payload.report_version = binding.report_version
         AND payload.report_payload_hash = binding.report_payload_hash
        WHERE binding.task_id = task.id
          AND binding.finalization_epoch = task.finalization_epoch
          AND task.completion_gate_snapshot_id = gate.gate_snapshot_id
          AND payload.report_payload->>'agent_run_id' = agent_run.id::text
          AND payload.report_payload #>> '{task,task_id}' = task.id::text
          AND binding.report_id = ('task:' || task.id::text || ':run:' || agent_run.id::text)
          AND payload.report_payload->>'report_id' = binding.report_id
          AND payload.report_payload->>'report_stage' = 'final'
          AND payload.report_payload->>'is_final' = 'true'
          AND payload.report_payload->>'report_binding_version' = binding.report_version::text
          AND payload.report_payload #>> '{completion_gate,gate_snapshot_id}'
              = gate.gate_snapshot_id
          AND payload.report_payload #>> '{completion_gate,finalization_epoch}'
              = binding.finalization_epoch::text
          AND payload.report_payload #>> '{completion_gate,gate_passed}' = 'true'
    )
)
"""

# 采集使用原始阶段；只有正式绑定可以将阶段提升后的报告认作完成。
APP_REPORT_PREVIEW_AVAILABLE_SQL = f"""
(
    task.target_platform = 'app'
    AND COALESCE(task.task_config->>'execution_mode', 'full_inspection') = 'full_inspection'
    AND agent_run.id IS NOT NULL
    AND (
        EXISTS (
            SELECT 1 FROM qc_capture AS preview_capture
            WHERE preview_capture.task_id = task.id
              AND preview_capture.agent_run_id = agent_run.id
              AND preview_capture.surface_kind IN ('android', 'ios')
              AND preview_capture.capture_status = 'captured'
              AND ({capture_epoch_sql('preview_capture')}) = task.finalization_epoch
        )
        OR EXISTS (
            SELECT 1 FROM qc_rule_execution AS preview_execution
            WHERE preview_execution.task_id = task.id
              AND COALESCE(preview_execution.judge_input->>'bootstrap', 'false') <> 'true'
              AND ({_placeholder_sql('preview_execution')}) IS NOT TRUE
              AND (
                  NOT (preview_execution.judge_input ? 'finalization_epoch')
                  OR preview_execution.judge_input->>'finalization_epoch'
                      = task.finalization_epoch::text
              )
              AND (
                  (
                      preview_execution.agent_run_id = agent_run.id
                      AND preview_execution.page_capture_id IS NULL
                      AND preview_execution.aggregate_work_item_id IS NULL
                  )
                  OR EXISTS (
                      SELECT 1 FROM qc_capture AS source_capture
                      WHERE source_capture.id = preview_execution.page_capture_id
                        AND source_capture.task_id = task.id
                        AND source_capture.agent_run_id = agent_run.id
                        AND ({capture_epoch_sql('source_capture')}) = task.finalization_epoch
                  )
                  OR EXISTS (
                      SELECT 1 FROM qc_aggregate_inspection_work_item AS aggregate_work
                      WHERE aggregate_work.id = preview_execution.aggregate_work_item_id
                        AND aggregate_work.task_id = task.id
                        AND aggregate_work.rule_id = preview_execution.rule_id
                        AND aggregate_work.producer_agent_run_id = agent_run.id
                        AND aggregate_work.finalization_epoch = task.finalization_epoch
                  )
              )
        )
        OR (
            jsonb_typeof(agent_run.output_context->'final_report') = 'object'
            AND agent_run.output_context #>> '{{final_report,task,task_id}}' = task.id::text
            AND agent_run.output_context #>> '{{final_report,agent_run_id}}' = agent_run.id::text
        )
    )
)
"""

APP_REPORT_AVAILABILITY_SQL = f"""
CASE
    WHEN {APP_FORMAL_REPORT_AVAILABLE_SQL} THEN 'final'
    WHEN {APP_REPORT_PREVIEW_AVAILABLE_SQL} THEN 'preview'
    ELSE 'none'
END
"""


def app_report_availability_sql(run_alias: str = "agent_run") -> str:
    """同一报告资格供列表和详情使用，表别名仅由内部代码指定。"""
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", run_alias):
        raise ValueError("报告 Run SQL 别名非法")
    return APP_REPORT_AVAILABILITY_SQL.replace("agent_run.", f"{run_alias}.")
