"""任务列表的分页与规则汇总 SQL，避免逐条重复扫描规则结论。"""

from services.api.app.infrastructure.db.rule_completion_sql import completed_rule_execution_sql
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    _occluded_placeholder_sql,
    _placeholder_sql,
    superseded_placeholder_sql,
)


def rules_settlement_lateral_sql(*, id_expr: str, epoch_expr: str) -> str:
    """主执行已结束且强制规则工作项全部终态时，给出补完时刻。"""

    return f"""
                LEFT JOIN LATERAL (
                    SELECT
                        settlement.epoch IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                            FROM qc_agent_run AS run
                            WHERE run.task_id = {id_expr}
                              AND run.agent_type = 'main'
                              AND run.status IN (
                                  'done', 'failed', 'canceled', 'aborted'
                              )
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_agent_run AS run
                            WHERE run.task_id = {id_expr}
                              AND run.agent_type = 'main'
                              AND run.status IN ('pending', 'running')
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_inspection_work_item AS work
                            WHERE work.task_id = {id_expr}
                              AND work.finalization_epoch = settlement.epoch
                              AND work.control_mode = 'enforced'
                              AND work.status NOT IN (
                                  'succeeded', 'partial', 'failed',
                                  'cancelled', 'superseded'
                              )
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_aggregate_inspection_work_item AS work
                            WHERE work.task_id = {id_expr}
                              AND work.finalization_epoch = settlement.epoch
                              AND work.control_mode = 'enforced'
                              AND work.status NOT IN (
                                  'succeeded', 'evidence_insufficient', 'failed',
                                  'cancelled', 'superseded'
                              )
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_inspection_attempt AS attempt
                            INNER JOIN qc_inspection_work_item AS work
                              ON work.id = attempt.work_item_id
                            WHERE work.task_id = {id_expr}
                              AND work.finalization_epoch = settlement.epoch
                              AND attempt.status IN ('running', 'judged')
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_aggregate_inspection_attempt AS attempt
                            INNER JOIN qc_aggregate_inspection_work_item AS work
                              ON work.id = attempt.work_item_id
                            WHERE work.task_id = {id_expr}
                              AND work.finalization_epoch = settlement.epoch
                              AND attempt.status = 'running'
                        ) AS rules_settled,
                        (
                            SELECT MAX(COALESCE(work.finished_at, work.updated_at))
                            FROM qc_inspection_work_item AS work
                            WHERE work.task_id = {id_expr}
                              AND work.finalization_epoch = settlement.epoch
                              AND work.control_mode = 'enforced'
                        ) AS rules_settled_at
                    FROM (
                        SELECT COALESCE(
                            (
                                SELECT MAX(work.finalization_epoch)
                                FROM qc_inspection_work_item AS work
                                WHERE work.task_id = {id_expr}
                                  AND work.finalization_epoch = {epoch_expr}
                                  AND work.control_mode = 'enforced'
                            ),
                            (
                                SELECT MAX(work.finalization_epoch)
                                FROM qc_inspection_work_item AS work
                                WHERE work.task_id = {id_expr}
                                  AND work.control_mode = 'enforced'
                            )
                        ) AS epoch
                    ) AS settlement
                ) AS rule_settlement ON TRUE
    """


def task_list_query_sql(
    *, report_status_filter: bool, report_availability_sql: str, report_status_sql: str,
) -> str:
    """报告筛选保留分页前资格判断，完整规则计数只读取当前页的任务。"""
    if report_status_filter:
        page_size, offset = 9, 10
        read_any, tenant_id, read_tenant, user_id = 11, 12, 13, 14
        department, platform, scope_department, submitter = 15, 16, 17, 18
        has_effective_rules = f"""CASE
            WHEN task.status IN ('done', 'review')
             AND task.task_config->>'execution_mode' IS DISTINCT FROM 'exploration_only'
             AND NOT (
                 task.target_platform IN ('web', 'h5')
                 AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
                 AND COALESCE(task.task_config->>'execution_mode', '') = 'full_inspection'
             )
            THEN EXISTS (
                SELECT 1 FROM qc_rule_execution AS execution
                WHERE execution.task_id = task.id
                  AND (
                      execution.agent_run_id = agent_run.id
                      OR EXISTS (
                          SELECT 1 FROM qc_capture AS source_capture
                          WHERE source_capture.id = execution.page_capture_id
                            AND source_capture.agent_run_id = agent_run.id
                      )
                  )
                  AND NOT {superseded_placeholder_sql('execution')}
            )
            ELSE FALSE
        END AS has_effective_rules,"""
        status_filter = f"""WHERE (
            {report_status_sql.replace('total_rules > 0', 'has_effective_rules')}
        ) = ANY(string_to_array($8::varchar, ','))"""
    else:
        page_size, offset = 8, 9
        read_any, tenant_id, read_tenant, user_id = 10, 11, 12, 13
        department, platform, scope_department, submitter = 14, 15, 16, 17
        has_effective_rules, status_filter = "", ""
    return f"""
        WITH task_rows AS (
            SELECT
                task.id AS task_uuid,
                task.id::text AS task_id,
                COALESCE(task.task_config->>'task_name', task.target_url) AS task_name,
                task.task_config->'product' AS product,
                task.task_config->'journey_steps' AS journey_steps,
                CASE WHEN jsonb_typeof(task.task_config->'ruleset_rule_total') = 'number'
                    THEN (task.task_config->>'ruleset_rule_total')::int
                    ELSE 0 END AS ruleset_rule_total,
                task.target_url,
                task.target_platform,
                task.task_config->>'execution_mode' AS execution_mode,
                task.task_config->>'workflow_version' AS workflow_version,
                task.task_config->>'web_inspection_notification_mode'
                    AS web_inspection_notification_mode,
                task.finalization_recovery_state,
                {report_availability_sql} AS report_availability,
                CASE
                    WHEN task.status = 'running'
                     AND COALESCE(agent_run.output_context->>'cancel_requested', 'false') = 'true'
                    THEN 'canceling'
                    ELSE task.status
                END AS status,
                task.created_at, task.started_at, task.finished_at,
                task.loop_guard AS progress_task_loop_guard,
                agent_run.loop_guard AS progress_run_loop_guard,
                agent_run.status AS run_status,
                task.finalization_epoch,
                COALESCE(NULLIF(task.last_error, ''), agent_run.error_message) AS last_error,
                tenant.display_name AS tenant_name,
                submitter.full_name AS submitter_name,
                ruleset.ruleset_code, ruleset.version AS ruleset_version,
                task.channel, task.priority,
                agent_run.id AS run_uuid,
                agent_run.id::text AS run_id,
                {has_effective_rules}
                COALESCE(task.finalization_agent_run_id, (
                    SELECT main.id FROM qc_agent_run AS main
                    WHERE main.task_id = task.id AND main.agent_type = 'main'
                    ORDER BY main.created_at DESC LIMIT 1
                )) AS current_h5_owner_run_id
            FROM qc_task AS task
            JOIN qc_ruleset AS ruleset ON ruleset.id = task.ruleset_id
            LEFT JOIN LATERAL (
                SELECT id, status, loop_guard, output_context, error_message
                FROM qc_agent_run
                WHERE task_id = task.id
                ORDER BY
                    CASE
                        WHEN id = task.finalization_agent_run_id THEN 0
                        WHEN agent_type = 'main' THEN 1
                        ELSE 2
                    END,
                    created_at DESC
                LIMIT 1
            ) agent_run ON TRUE
            LEFT JOIN qc_tenant AS tenant ON tenant.id = task.tenant_id
            LEFT JOIN qc_user AS submitter ON submitter.id = task.submitter_id
            WHERE task.deleted_at IS NULL
              AND ($1::varchar IS NULL
                   OR task.task_config->>'task_name' ILIKE '%' || $1 || '%')
              AND ($2::varchar IS NULL
                   OR task.task_config->'product'->>'product_name' ILIKE '%' || $2 || '%')
              AND ($3::varchar IS NULL
                   OR task.task_config->'product'->>'product_code' ILIKE '%' || $3 || '%')
              AND ($4::varchar IS NULL OR task.target_url ILIKE '%' || $4 || '%')
              AND ($5::varchar IS NULL OR CASE
                  WHEN task.status = 'running'
                   AND COALESCE(agent_run.output_context->>'cancel_requested', 'false') = 'true'
                  THEN 'canceling' ELSE task.status END = $5)
              AND ($6::timestamptz IS NULL OR task.created_at >= $6)
              AND ($7::timestamptz IS NULL OR task.created_at <= $7)
              AND (${department}::varchar IS NULL
                   OR task.task_config->'product'->>'department_name'
                       ILIKE '%' || ${department} || '%')
              AND (${platform}::varchar IS NULL OR task.target_platform = ${platform})
              AND (${submitter}::varchar IS NULL
                   OR submitter.full_name ILIKE '%' || ${submitter} || '%')
              AND (${read_any}::boolean OR (
                  task.tenant_id = ${tenant_id}::uuid AND (
                      ${read_tenant}::boolean OR task.submitter_id = ${user_id}::uuid
                      OR task.department_name = ${scope_department}::varchar
                  )
              ))
        ), filtered_tasks AS (
            SELECT *, COUNT(*) OVER()::int AS total_count
            FROM task_rows
            {status_filter}
        ), paged_tasks AS MATERIALIZED (
            SELECT * FROM filtered_tasks
            ORDER BY created_at DESC
            LIMIT ${page_size} OFFSET ${offset}
        ), replacement_facts AS MATERIALIZED (
            SELECT execution.id, execution.task_id, execution.rule_id,
                   {completed_rule_execution_sql('execution')} AS is_completed,
                   {_placeholder_sql('execution')} AS is_placeholder,
                   {_occluded_placeholder_sql('execution')} AS is_occluded,
                   COALESCE(page.page_type, capture.metadata->>'page_type') AS page_type,
                   (execution.agent_run_id = task.run_uuid
                    OR capture.agent_run_id = task.run_uuid) IS TRUE AS selected_run,
                   CASE WHEN task.target_platform = 'h5'
                         AND LOWER(COALESCE(task.workflow_version, '')) = 'v2'
                        THEN execution.agent_run_id
                                 IS NOT DISTINCT FROM task.current_h5_owner_run_id
                             OR (capture.agent_run_id = task.current_h5_owner_run_id) IS TRUE
                        ELSE TRUE END AS current_conclusion
            FROM paged_tasks AS task
            JOIN qc_rule_execution AS execution ON execution.task_id = task.task_uuid
            LEFT JOIN qc_page_capture AS page ON page.id = execution.page_capture_id
            LEFT JOIN qc_capture AS capture ON capture.id = execution.page_capture_id
        ), concluded_rules AS MATERIALIZED (
            SELECT DISTINCT task_id, rule_id FROM replacement_facts
            WHERE NOT is_placeholder AND current_conclusion
        ), concluded_page_types AS MATERIALIZED (
            SELECT DISTINCT task_id, rule_id, page_type FROM replacement_facts
            WHERE NOT is_placeholder AND current_conclusion AND page_type IS NOT NULL
        ), effective_executions AS (
            SELECT facts.* FROM replacement_facts AS facts
            WHERE facts.selected_run
              AND NOT (
                  facts.is_placeholder IS NOT FALSE
                  AND EXISTS (SELECT 1 FROM concluded_rules AS concluded
                              WHERE concluded.task_id = facts.task_id
                                AND concluded.rule_id = facts.rule_id)
                  -- 原逐条 EXISTS 只接受 TRUE，空页型与缺失遮挡字段不能改变替代资格。
                  AND (NOT facts.is_occluded OR EXISTS (
                      SELECT 1 FROM concluded_page_types AS concluded
                      WHERE concluded.task_id = facts.task_id
                        AND concluded.rule_id = facts.rule_id
                        AND concluded.page_type = facts.page_type
                  )) IS TRUE
              )
        ), execution_stats AS (
            SELECT execution.task_id,
                   COUNT(*)::int AS total_rules,
                   COUNT(*) FILTER (WHERE execution.is_completed)::int AS finished_rules
            FROM effective_executions AS execution
            GROUP BY execution.task_id
        )
        SELECT paged_tasks.*,
               rule_settlement.rules_settled,
               rule_settlement.rules_settled_at,
               COALESCE(execution_stats.total_rules, 0)::int AS total_rules,
               COALESCE(execution_stats.finished_rules, 0)::int AS finished_rules
        FROM paged_tasks
        {rules_settlement_lateral_sql(
            id_expr='paged_tasks.task_uuid',
            epoch_expr='paged_tasks.finalization_epoch',
        )}
        LEFT JOIN execution_stats ON execution_stats.task_id = paged_tasks.task_uuid
        ORDER BY paged_tasks.created_at DESC
    """
