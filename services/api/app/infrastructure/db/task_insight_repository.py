"""任务详情过程可视化读仓储：时间线 / 规则结果 / 日志 / 截图证据的查询出口。

只服务工作台聚合读取（``application/task_insight.py``），全部为只读查询；
写入侧见 ``task_progress_store.py``。
"""

import json
from datetime import UTC, datetime
from typing import Any

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task.status import TaskStatus
from services.api.app.infrastructure.db.app_report_availability_sql import (
    app_report_availability_sql,
)
from services.api.app.infrastructure.db.inspection_scope_sql import (
    page_type_in_inspection_scope_sql,
    task_config_of_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.review_finalization import wake_review_finalization
from services.api.app.infrastructure.db.rule_confirmation_sql import (
    requires_human_confirmation_sql,
)
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    outside_current_h5_run_sql,
    rule_replacement_ctes_sql,
    superseded_placeholder_sql,
)
from services.api.app.infrastructure.db.task_status_repository import (
    transition_current_task_status_in_transaction,
)

_EXECUTION_REASON_SQL = """
CASE
    WHEN execution.execution_status = 'skipped'
     AND lower(btrim(COALESCE(execution.error_code, '')))
         IN ('supplemental_evidence_disabled', 'supplemental_evidence_not_ready')
    THEN lower(btrim(execution.error_code))
    WHEN execution.execution_status = 'skipped'
     AND lower(btrim(COALESCE(execution.judge_output->>'error_code', '')))
         IN ('supplemental_evidence_disabled', 'supplemental_evidence_not_ready')
    THEN lower(btrim(execution.judge_output->>'error_code'))
    WHEN lower(btrim(execution.judge_output->>'reason')) = 'runtime_rule_write_failed'
     AND upper(COALESCE(
         capture.inspection_failure_code,
         common_capture.metadata->>'inspection_failure_code'
     )) IN (
        'MAX_STEPS_REACHED',
        'STUCK_DETECTED',
        'CONTINUATION_LIMIT_REACHED',
        'RUN_TIMEOUT'
     )
    THEN 'run_budget_exhausted'
    ELSE COALESCE(
        (
            SELECT lower(btrim(code)) FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(execution.judge_output->'model_output'->'conflicts') = 'array'
                THEN execution.judge_output->'model_output'->'conflicts' ELSE '[]'::jsonb END
            ) AS diagnostic(code)
            WHERE lower(btrim(code)) ~ '^(capability_|deterministic_dependency_|judge_provider_|llm_provider_|platform_|semantic_provider_)'
               OR lower(btrim(code)) IN ('prompt_input_token_budget', 'semantic_batch_single_failed', 'semantic_judge_unavailable')
            LIMIT 1
        ),
        NULLIF(lower(btrim(execution.judge_output->>'reason')), '')
    )
END
""".strip()


def _scope_sql(
    access_scope: AccessScope | None,
    *,
    task_alias: str,
    first_param: int,
    include_review: bool = False,
) -> tuple[str, tuple[object, ...]]:
    """返回任务 owner/tenant 过滤片段；无 scope 时保持历史内部查询形态。"""

    if access_scope is None:
        return "", ()
    any_param = first_param
    tenant_param = first_param + 1
    tenant_read_param = first_param + 2
    owner_param = first_param + 3
    department_param = first_param + 4
    department_scope = access_scope.department_scope or (
        access_scope.review_department_scope if include_review else None
    )
    return (
        f"""
          AND (
              ${any_param}::boolean
              OR (
                  {task_alias}.tenant_id = ${tenant_param}::uuid
                  AND (
                      ${tenant_read_param}::boolean
                      OR {task_alias}.submitter_id = ${owner_param}::uuid
                      OR {task_alias}.department_name = ${department_param}::varchar
                  )
              )
          )
        """,
        (
            access_scope.can_read_any
            or (include_review and "taskObjects:review:any" in access_scope.permissions),
            access_scope.tenant_id,
            access_scope.can_read_tenant or (include_review and access_scope.can_review_tenant),
            access_scope.actor_user_id,
            department_scope,
        ),
    )


class PostgresTaskInsightRepository:
    """基于 PostgreSQL 的任务详情读模型仓储。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None, artifact_store=None) -> None:
        self._database_url = _normalize_asyncpg_url(database_url) if database_url else None
        self._pool = pool
        self._green_artifact_store = artifact_store

    async def read_green_rule_results(self, *, task_id, execution_ids, access_scope=None):
        """只读补充绿色八条的正式分项结果；复用任务权限，不增加对外接口。"""
        from services.api.app.core.config import get_settings
        from services.api.app.infrastructure.db.green_rule_results import read_green_rule_results
        from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

        if not execution_ids:
            return {}
        if self._green_artifact_store is None:
            self._green_artifact_store = build_evidence_artifact_store(get_settings().evidence_storage)
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=2)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction(isolation="repeatable_read", readonly=True):
            return await read_green_rule_results(
                connection, task_id=task_id, execution_ids=execution_ids, artifact_store=self._green_artifact_store,
                scope_sql=scope_sql, scope_args=scope_args,
            )

    async def read_green_confirmation_results(self, *, task_id, execution_ids, access_scope):
        """仅确认服务内部核验；审核对象授权不扩展公开任务读取或原图接口。"""
        from services.api.app.core.config import get_settings
        from services.api.app.infrastructure.db.green_rule_results import read_green_rule_results
        from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

        if access_scope is None or not execution_ids:
            return {}
        if self._green_artifact_store is None:
            self._green_artifact_store = build_evidence_artifact_store(get_settings().evidence_storage)
        scope_sql, scope_args = _scope_sql(
            access_scope, task_alias="task", first_param=2, include_review=True
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction(
            isolation="repeatable_read", readonly=True
        ):
            return await read_green_rule_results(
                connection,
                task_id=task_id,
                execution_ids=execution_ids,
                artifact_store=self._green_artifact_store,
                scope_sql=scope_sql,
                scope_args=scope_args,
            )

    async def fetch_web_report_explanation(self, *, task_id, agent_run_id, access_scope=None):
        """单次一致性读取公开 Web 事实，权限过滤先于所有材料查询。"""
        from services.api.app.infrastructure.db.web_report_explanation_repository import (
            read_web_report_explanation,
        )

        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction(
            isolation="repeatable_read", readonly=True,
        ):
            return await read_web_report_explanation(connection, task_id=task_id,
                agent_run_id=agent_run_id, scope_sql=scope_sql, scope_args=scope_args)

    async def read_web_capture_identities(self, *, task_id: str, agent_run_id: str):
        """仅由已校验任务读取范围的 Web 聚合分支调用。"""
        from services.api.app.infrastructure.db.web_capture_identity import (
            read_web_capture_identities,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return await read_web_capture_identities(
                connection, task_id=task_id, agent_run_id=agent_run_id,
            )

    async def fetch_task_context(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=2)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT
                    task.id::text AS task_id,
                    task.status,
                    task.target_platform,
                    task.target_url,
                    task.channel,
                    task.created_at,
                    task.started_at,
                    task.finished_at,
                    task.last_error,
                    task.task_config,
                    task.loop_guard,
                    task.finalization_state,
                    task.finalization_epoch,
                    task.completion_gate_snapshot_id,
                    {app_report_availability_sql("run")} AS app_report_availability,
                    COALESCE((
                        SELECT gate.gate_passed
                        FROM qc_completion_gate_snapshot AS gate
                        WHERE gate.task_id = task.id
                          AND gate.gate_snapshot_id = task.completion_gate_snapshot_id
                          AND gate.finalization_epoch = task.finalization_epoch
                        LIMIT 1
                    ), FALSE) AS completion_gate_passed,
                    EXISTS (
                        SELECT 1
                        FROM qc_report_gate_binding AS binding
                        INNER JOIN qc_report_payload_snapshot AS payload
                            ON payload.report_id = binding.report_id
                           AND payload.report_version = binding.report_version
                        WHERE binding.task_id = task.id
                          AND binding.gate_snapshot_id = task.completion_gate_snapshot_id
                          AND binding.finalization_epoch = task.finalization_epoch
                    ) AS sealed_report_bound,
                    ruleset.ruleset_code,
                    ruleset.version AS ruleset_version,
                    ruleset.name AS ruleset_name,
                    -- 报告「规则总数」：建任务时记下的启用规则条数；老任务没记，按现在启用的算
                    COALESCE(
                        CASE
                            WHEN jsonb_typeof(task.task_config->'ruleset_rule_total') = 'number'
                            THEN (task.task_config->>'ruleset_rule_total')::int
                        END,
                        (
                            SELECT count(*)::int
                            FROM qc_rule AS scoped_rule
                            WHERE scoped_rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                              AND scoped_rule.status = 'enabled'
                        )
                    ) AS ruleset_rule_total,
                    run.run_id,
                    run.run_status,
                    run.run_loop_guard,
                    run.stop_reason,
                    run.run_error,
                    run.working_memory
                FROM qc_task task
                JOIN qc_ruleset ruleset ON ruleset.id = task.ruleset_id
                LEFT JOIN LATERAL (
                    SELECT
                        id,
                        output_context,
                        id::text AS run_id,
                        status AS run_status,
                        loop_guard AS run_loop_guard,
                        stop_reason,
                        error_message AS run_error,
                        working_memory
                    FROM qc_agent_run
                    WHERE task_id = task.id
                    ORDER BY
                        CASE
                            WHEN task.target_platform IN ('h5', 'app')
                             AND id = task.finalization_agent_run_id
                            THEN 0
                            WHEN agent_type = 'main' THEN 1
                            ELSE 2
                        END,
                        created_at DESC
                    LIMIT 1
                ) run ON TRUE
                WHERE task.id = $1::uuid
                {scope_sql}
                """,
                task_id,
                *scope_args,
            )
        if row is None:
            return None
        context = dict(row)
        context["task_config"] = _json_dict(context.get("task_config"))
        context["loop_guard"] = _json_dict(context.get("loop_guard"))
        context["run_loop_guard"] = _json_dict(context.get("run_loop_guard"))
        context["working_memory"] = _json_dict(context.get("working_memory"))
        return context

    async def list_page_nodes(self, *, agent_run_id: str) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    node.id::text AS node_id,
                    node.node_seq,
                    node.source,
                    node.step_name,
                    COALESCE(
                        capture.page_type,
                        common_capture.metadata->>'page_type',
                        node.page_type
                    ) AS page_type,
                    COALESCE(
                        app_capture.package_name,
                        common_capture.metadata->>'package_name'
                    ) AS package_name,
                    COALESCE(
                        app_capture.activity,
                        common_capture.metadata->>'activity'
                    ) AS activity,
                    COALESCE(
                        app_capture.width,
                        CASE
                            WHEN common_capture.metadata->>'width' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'width')::integer
                        END
                    ) AS width,
                    COALESCE(
                        app_capture.height,
                        CASE
                            WHEN common_capture.metadata->>'height' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'height')::integer
                        END
                    ) AS height,
                    node.status,
                    capture.id::text AS page_capture_id,
                    node.candidate_rule_total,
                    node.current_rule_code,
                    node.first_visited_at,
                    node.finished_at,
                    COALESCE(capture.url, common_capture.metadata->>'url', '') AS url,
                    capture.captured_at,
                    capture.page_state,
                    capture.detection_status,
                    capture.detection_confidence,
                    capture.metadata #>> '{page_detection,confidence_source}'
                        AS detection_confidence_source,
                    capture.metadata #>> '{page_detection,model_page_type}'
                        AS detection_model_page_type,
                    capture.metadata #>> '{page_detection,model_confidence}'
                        AS detection_model_confidence,
                    capture.detection_duration_ms,
                    capture.element_count,
                    capture.parent_page_capture_id::text AS parent_page_capture_id,
                    COALESCE(
                        capture.inspection_lifecycle_status,
                        common_capture.metadata->>'inspection_lifecycle_status'
                    ) AS inspection_lifecycle_status,
                    COALESCE(
                        capture.inspection_status,
                        common_capture.metadata #>> '{inspection,status}',
                        common_capture.metadata->>'inspection_status'
                    ) AS inspection_status,
                    COALESCE(
                        capture.inspection_failure_stage,
                        common_capture.metadata->>'inspection_failure_stage'
                    ) AS inspection_failure_stage,
                    COALESCE(
                        capture.inspection_failure_code,
                        common_capture.metadata->>'inspection_failure_code'
                    ) AS inspection_failure_code,
                    COALESCE(
                        capture.inspection_failure_message,
                        common_capture.metadata->>'inspection_failure_message'
                    ) AS inspection_failure_message,
                    COALESCE(
                        capture.inspection_failure_retryable,
                        CASE
                            WHEN common_capture.metadata->>'inspection_failure_retryable'
                                IN ('true', 'false')
                            THEN (common_capture.metadata->>'inspection_failure_retryable')::boolean
                        END
                    ) AS inspection_failure_retryable,
                    COALESCE(
                        capture.inspection_abandon_reason,
                        common_capture.metadata->>'inspection_abandon_reason'
                    ) AS inspection_abandon_reason,
                    shot.artifact_id AS screenshot_artifact_id
                FROM qc_task_page_node node
                LEFT JOIN qc_capture common_capture
                  ON common_capture.id = node.page_capture_id
                LEFT JOIN qc_app_capture app_capture
                  ON app_capture.capture_id = common_capture.id
                LEFT JOIN LATERAL (
                    WITH RECURSIVE supplemental_chain AS (
                        SELECT source.id, 0 AS depth
                        FROM qc_page_capture source
                        WHERE source.id = node.page_capture_id
                        UNION ALL
                        SELECT next_capture.id, chain.depth + 1
                        FROM supplemental_chain chain
                        JOIN qc_page_exploration_action action
                          ON action.source_page_capture_id = chain.id
                         AND action.action IN ('scroll', 'timed_capture')
                         AND action.status = 'explored'
                         AND action.result_page_capture_id IS NOT NULL
                        JOIN qc_page_capture next_capture
                          ON next_capture.id = action.result_page_capture_id
                         AND next_capture.agent_run_id = node.agent_run_id
                        WHERE chain.depth < 8
                    )
                    SELECT resolved.*
                    FROM supplemental_chain chain
                    JOIN qc_page_capture resolved ON resolved.id = chain.id
                    ORDER BY
                        (resolved.inspection_lifecycle_status = 'active') DESC,
                        chain.depth DESC,
                        resolved.page_seq DESC
                    LIMIT 1
                ) capture ON TRUE
                LEFT JOIN LATERAL (
                    SELECT artifact.id::text AS artifact_id
                    FROM qc_evidence_artifact artifact
                    WHERE artifact.page_capture_id = capture.id
                      AND artifact.artifact_type = 'screenshot'
                      AND artifact.deleted_at IS NULL
                      AND artifact.lifecycle_status IN ('active', 'archived')
                    ORDER BY artifact.created_at
                    LIMIT 1
                ) shot ON TRUE
                WHERE node.agent_run_id = $1::uuid
                ORDER BY node.node_seq
                """,
                agent_run_id,
            )
            app_rows = await connection.fetch(
                """
                SELECT
                    common_capture.id::text AS node_id,
                    COALESCE(common_capture.session_command_sequence, 0) AS node_seq,
                    'app_runtime' AS source,
                    common_capture.capture_role AS step_name,
                    -- 缺页型保持 NULL；禁止用平台名 app 冒充目录页型（否则时间轴显示
                    -- 「页面类型: app」且候选规则数为 0，弹窗帧会像「没规则」）。
                    common_capture.metadata->>'page_type' AS page_type,
                    common_capture.capture_status AS status,
                    common_capture.id::text AS page_capture_id,
                    NULL::integer AS candidate_rule_total,
                    NULL::text AS current_rule_code,
                    common_capture.captured_at AS first_visited_at,
                    NULL::timestamptz AS finished_at,
                    COALESCE(common_capture.metadata->>'url', '') AS url,
                    common_capture.captured_at,
                    common_capture.metadata->>'page_state' AS page_state,
                    common_capture.metadata->>'detection_status' AS detection_status,
                    NULL::numeric AS detection_confidence,
                    NULL::text AS detection_confidence_source,
                    NULL::text AS detection_model_page_type,
                    NULL::numeric AS detection_model_confidence,
                    NULL::integer AS detection_duration_ms,
                    NULL::integer AS element_count,
                    common_capture.parent_capture_id::text AS parent_page_capture_id,
                    common_capture.metadata->>'inspection_lifecycle_status'
                        AS inspection_lifecycle_status,
                    COALESCE(
                        common_capture.metadata #>> '{inspection,status}',
                        common_capture.metadata->>'inspection_status'
                    ) AS inspection_status,
                    common_capture.metadata->>'inspection_failure_stage'
                        AS inspection_failure_stage,
                    common_capture.metadata->>'inspection_failure_code'
                        AS inspection_failure_code,
                    common_capture.metadata->>'inspection_failure_message'
                        AS inspection_failure_message,
                    CASE
                        WHEN common_capture.metadata->>'inspection_failure_retryable'
                            IN ('true', 'false')
                        THEN (common_capture.metadata->>'inspection_failure_retryable')::boolean
                    END AS inspection_failure_retryable,
                    common_capture.metadata->>'inspection_abandon_reason'
                        AS inspection_abandon_reason,
                    app_capture.package_name,
                    app_capture.activity,
                    app_capture.width,
                    app_capture.height,
                    shot.artifact_id AS screenshot_artifact_id
                FROM qc_capture AS common_capture
                INNER JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = common_capture.id
                LEFT JOIN LATERAL (
                    SELECT artifact.id::text AS artifact_id
                    FROM qc_evidence_artifact artifact
                    WHERE artifact.page_capture_id = common_capture.id
                      AND artifact.artifact_type = 'screenshot'
                      AND artifact.deleted_at IS NULL
                      AND artifact.lifecycle_status IN ('active', 'archived')
                    ORDER BY artifact.created_at
                    LIMIT 1
                ) shot ON TRUE
                WHERE common_capture.agent_run_id = $1::uuid
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_task_page_node node
                      WHERE node.agent_run_id = $1::uuid
                        AND node.page_capture_id = common_capture.id
                  )
                ORDER BY common_capture.session_command_sequence,
                         common_capture.captured_at,
                         common_capture.id
                """,
                agent_run_id,
            )
        return [dict(row) for row in [*rows, *app_rows]]

    async def list_page_capture_inspection_rows(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        """读取 Capture 的质检与生命周期事实，兼容页面明细和 App 公共 Capture。"""
        pool = await self._get_pool()
        task_join = (
            "JOIN qc_task AS task ON task.id = common_capture.task_id"
            if access_scope is not None
            else ""
        )
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    common_capture.id::text AS page_capture_id,
                    COALESCE(
                        page_capture.parent_page_capture_id,
                        common_capture.parent_capture_id
                    )::text AS parent_page_capture_id,
                    COALESCE(
                        page_capture.page_seq,
                        common_capture.session_command_sequence,
                        0
                    ) AS page_seq,
                    COALESCE(
                        page_capture.page_type,
                        common_capture.metadata->>'page_type'
                    ) AS page_type,
                    COALESCE(
                        app_capture.package_name,
                        common_capture.metadata->>'package_name'
                    ) AS package_name,
                    COALESCE(
                        app_capture.activity,
                        common_capture.metadata->>'activity'
                    ) AS activity,
                    COALESCE(
                        app_capture.width,
                        CASE
                            WHEN common_capture.metadata->>'width' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'width')::integer
                        END
                    ) AS width,
                    COALESCE(
                        app_capture.height,
                        CASE
                            WHEN common_capture.metadata->>'height' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'height')::integer
                        END
                    ) AS height,
                    COALESCE(
                        page_capture.page_state,
                        common_capture.metadata->>'page_state'
                    ) AS page_state,
                    COALESCE(
                        page_capture.url,
                        common_capture.metadata->>'url',
                        ''
                    ) AS url,
                    common_capture.captured_at,
                    COALESCE(
                        page_capture.detection_status,
                        common_capture.metadata->>'detection_status'
                    ) AS detection_status,
                    COALESCE(
                        page_capture.metadata #> '{{page_detection,logical_surface}}',
                        common_capture.metadata #> '{{page_detection,logical_surface}}',
                        logical_assessment.assessment_payload
                            #> '{{detection_result,logical_surface}}'
                    ) AS logical_surface,
                    COALESCE(
                        page_capture.metadata #>> '{{page_detection,confidence_source}}',
                        common_capture.metadata #>> '{{page_detection,confidence_source}}',
                        logical_assessment.assessment_payload
                            #>> '{{detection_result,confidence_source}}'
                    ) AS confidence_source,
                    ARRAY(
                        SELECT artifact.id::text
                        FROM qc_evidence_artifact artifact
                        WHERE artifact.page_capture_id = common_capture.id
                        ORDER BY artifact.created_at, artifact.id
                    ) AS artifact_ids,
                    COALESCE(
                        page_capture.inspection_lifecycle_status,
                        common_capture.metadata->>'inspection_lifecycle_status'
                    ) AS inspection_lifecycle_status,
                    COALESCE(
                        page_capture.inspection_status,
                        common_capture.metadata #>> '{{inspection,status}}',
                        common_capture.metadata->>'inspection_status'
                    ) AS inspection_status,
                    COALESCE(
                        page_capture.inspection_failure_stage,
                        common_capture.metadata->>'inspection_failure_stage'
                    ) AS inspection_failure_stage,
                    COALESCE(
                        page_capture.inspection_failure_code,
                        common_capture.metadata->>'inspection_failure_code'
                    ) AS inspection_failure_code,
                    COALESCE(
                        page_capture.inspection_failure_message,
                        common_capture.metadata->>'inspection_failure_message'
                    ) AS inspection_failure_message,
                    COALESCE(
                        page_capture.inspection_failure_retryable,
                        CASE
                            WHEN common_capture.metadata->>'inspection_failure_retryable'
                                IN ('true', 'false')
                            THEN (common_capture.metadata->>'inspection_failure_retryable')::boolean
                        END
                    ) AS inspection_failure_retryable,
                    COALESCE(
                        page_capture.inspection_abandon_reason,
                        common_capture.metadata->>'inspection_abandon_reason'
                    ) AS inspection_abandon_reason
                FROM qc_capture AS common_capture
                {task_join}
                LEFT JOIN qc_page_capture page_capture
                  ON page_capture.id = common_capture.id
                LEFT JOIN qc_app_capture app_capture
                  ON app_capture.capture_id = common_capture.id
                LEFT JOIN LATERAL (
                    SELECT stored.assessment_payload
                    FROM qc_h5_page_type_assessment AS stored
                    WHERE stored.task_id = common_capture.task_id
                      AND stored.page_capture_id = common_capture.id
                      AND (
                          $2::uuid IS NULL
                          OR stored.agent_run_id = $2::uuid
                      )
                    ORDER BY stored.updated_at DESC, stored.assessment_record_id DESC
                    LIMIT 1
                ) AS logical_assessment ON TRUE
                WHERE common_capture.task_id = $1::uuid
                  AND ($2::uuid IS NULL OR common_capture.agent_run_id = $2::uuid)
                  {scope_sql}
                  -- 经过的页面只留认路记录，不进页面质检列表
                  AND {page_type_in_inspection_scope_sql(
                      task_config_of_sql("common_capture.task_id"),
                      "COALESCE(page_capture.page_type, common_capture.metadata->>'page_type', '')",
                  )}
                ORDER BY
                    COALESCE(
                        page_capture.page_seq,
                        common_capture.session_command_sequence,
                        0
                    ),
                    common_capture.captured_at,
                    common_capture.id
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
        return [dict(row) for row in rows]

    async def list_exploration_actions(
        self,
        *,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        """读取动态 Frontier 动作；无 Run 时返回空列表，不把动作伪装成页面。"""
        if agent_run_id is None:
            return []
        pool = await self._get_pool()
        task_join = (
            "JOIN qc_agent_run AS run ON run.id = action.agent_run_id\n"
            "JOIN qc_task AS task ON task.id = run.task_id"
            if access_scope is not None
            else ""
        )
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=2)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    action.exploration_action_id,
                    action.source_page_capture_id::text AS source_page_capture_id,
                    action.source_page_type,
                    action.action,
                    action.origin,
                    action.target,
                    action.target_keywords,
                    action.risk_level,
                    action.purpose,
                    action.expected_next_page_type,
                    action.navigation_depth,
                    action.status,
                    action.result_page_capture_id::text AS result_page_capture_id,
                    action.failure_reason,
                    action.failure_code,
                    action.failure_message,
                    action.failure_retryable,
                    action.attempt_count,
                    action.created_at,
                    action.updated_at
                FROM qc_page_exploration_action AS action
                {task_join}
                WHERE action.agent_run_id = $1::uuid
                  {scope_sql}
                ORDER BY action.created_at, action.exploration_action_id
                """,
                agent_run_id,
                *scope_args,
            )
        result = []
        for row in rows:
            item = dict(row)
            item["target_keywords"] = _json_str_list(item.get("target_keywords"))
            result.append(item)
        return result

    async def list_h5_v2_work_items(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        """读取当前 H5 Run 的 Work 历史和正式任务 Frontier。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH scoped_h5_sessions AS (
                    SELECT DISTINCT trusted.session_id
                    FROM qc_v2_work_item AS trusted
                    INNER JOIN qc_surface_session AS session
                      ON session.task_id = trusted.task_id
                     AND session.session_id = trusted.session_id
                     AND session.surface_kind = 'web'
                    WHERE trusted.task_id = $1::uuid
                      AND trusted.work_type = 'exploration'
                      AND trusted.work_payload #>>
                            '{frontier,observation,metadata,agent_run_id}' = $2
                )
                SELECT
                    work.work_id,
                    work.work_type,
                    work.session_id,
                    work.branch_id,
                    work.source_capture_id::text AS source_capture_id,
                    work.status,
                    work.work_payload #>> '{frontier,observation,metadata,parent_work_id}'
                        AS parent_work_id,
                    work.work_payload #> '{frontier,observation,metadata,h5_task_frontier}'
                        AS frontier,
                    work.work_payload #>>
                        '{frontier,observation,metadata,h5_visual_coordinate_mode}'
                        AS h5_visual_coordinate_mode,
                    work.work_payload #>
                        '{frontier,observation,metadata,h5_browser_recovery}'
                        AS browser_recovery,
                    work.work_payload #>
                        '{frontier,observation,metadata,h5_browser_recovery_attempt}'
                        AS browser_recovery_attempt,
                    work.work_payload #>
                        '{frontier,observation,metadata,h5_browser_recovery_attempt_history}'
                        AS browser_recovery_attempt_history,
                    work.work_payload #>
                        '{frontier,observation,metadata,h5_browser_recovery_parent_attempt_audit}'
                        AS browser_recovery_parent_attempt_audit,
                    work.terminal_payload,
                    work.last_error_code,
                    work.last_error_message,
                    work.created_at,
                    work.started_at,
                    work.finished_at,
                    work.updated_at
                FROM qc_v2_work_item AS work
                INNER JOIN scoped_h5_sessions AS scoped
                  ON work.session_id = scoped.session_id
                WHERE work.task_id = $1::uuid
                  AND work.work_type IN ('exploration', 'probe')
                ORDER BY work.created_at, work.work_id
                """,
                task_id,
                agent_run_id,
            )
        result = []
        for row in rows:
            item = dict(row)
            item["frontier"] = _json_dict(item.get("frontier"))
            item["browser_recovery"] = _json_dict(item.get("browser_recovery"))
            item["browser_recovery_attempt"] = _json_dict(
                item.get("browser_recovery_attempt")
            )
            item["browser_recovery_attempt_history"] = _json_list(
                item.get("browser_recovery_attempt_history")
            )
            item["browser_recovery_parent_attempt_audit"] = _json_list(
                item.get("browser_recovery_parent_attempt_audit")
            )
            item["terminal_payload"] = _json_dict(item.get("terminal_payload"))
            result.append(item)
        return result

    async def list_h5_action_executions(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        """通过同 Run 的 H5 Work Session 限域读取动作账本。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    action.action_execution_id,
                    action.session_id,
                    action.command_sequence,
                    action.intent_payload->>'action_type' AS action,
                    CASE
                        WHEN visual_provenance.is_visual_navigation_provenance
                        THEN NULL
                        ELSE action.intent_payload->>'semantic_target'
                    END AS target,
                    visual_provenance.is_visual_navigation_provenance,
                    action.intent_payload->>'purpose' AS purpose,
                    action.intent_payload->>'risk_level' AS risk_level,
                    jsonb_strip_nulls(jsonb_build_object(
                        'target_mode',
                            action.intent_payload #> '{parameters,target_mode}',
                        'popup_dismissal_capability',
                            action.intent_payload #> '{parameters,popup_dismissal_capability}',
                        'startup_ad_capability',
                            action.intent_payload #> '{parameters,startup_ad_capability}',
                        'navigation_depth',
                            action.intent_payload #> '{parameters,navigation_depth}',
                        'explorer_decision_fallback',
                            action.intent_payload #>
                                '{parameters,explorer_decision_fallback}'
                    )) AS parameters,
                    'discovered'::text AS origin,
                    action.intent_payload->'parameters'->>'expected_next_page_type'
                        AS expected_next_page_type,
                    requested_source.page_type AS source_page_type,
                    action.requested_source_capture_id::text
                        AS requested_source_capture_id,
                    action.source_capture_id::text AS source_page_capture_id,
                    action.result_capture_id::text AS result_page_capture_id,
                    CASE
                        WHEN action.status = 'completed' THEN 'explored'
                        WHEN action.status = 'blocked' THEN 'blocked'
                        WHEN action.status IN (
                            'evidence_failed', 'failed', 'timed_out'
                        ) THEN 'failed'
                        WHEN action.status = 'planned' THEN 'candidate'
                        ELSE 'selected'
                    END AS status,
                    action.status AS execution_status,
                    jsonb_build_object(
                        'state_diff',
                        jsonb_strip_nulls(jsonb_build_object(
                            'before_location', action.result_payload #>
                                '{state_diff,before_location}',
                            'after_location', action.result_payload #>
                                '{state_diff,after_location}',
                            'location_changed', action.result_payload #>
                                '{state_diff,location_changed}',
                            'visual_action_committed', action.result_payload #>
                                '{state_diff,visual_action_committed}'
                        ))
                    ) AS result_payload,
                    action.result_payload
                        #> '{state_diff,logical_surface_transition}'
                        AS logical_surface_transition,
                    CASE
                        WHEN COALESCE(
                            action.result_payload #>>
                                '{state_diff,visual_navigation_transition,protocol_version}',
                            action.result_payload #>>
                                '{state_diff,logical_surface_transition,visual_proof,protocol_version}'
                        ) IN (
                            'visual_navigation_transition.v1',
                            'visual_navigation_transition_proof.v1'
                        )
                        THEN jsonb_strip_nulls(jsonb_build_object(
                            'protocol_version', COALESCE(
                                action.result_payload #>
                                    '{state_diff,visual_navigation_transition,protocol_version}',
                                action.result_payload #>
                                    '{state_diff,logical_surface_transition,visual_proof,protocol_version}'
                            ),
                            'status', COALESCE(
                                action.result_payload #>
                                    '{state_diff,visual_navigation_transition,status}',
                                action.result_payload #>
                                    '{state_diff,logical_surface_transition,visual_proof,status}'
                            ),
                            'logical_transition_proof', action.result_payload #>
                                '{state_diff,logical_surface_transition,logical_transition_proof}',
                            'after_identity_bound', COALESCE(
                                action.result_payload #>
                                    '{state_diff,visual_navigation_transition,after_identity_bound}',
                                action.result_payload #>
                                    '{state_diff,logical_surface_transition,visual_proof,after_identity_bound}'
                            ),
                            'reason_codes', COALESCE(
                                action.result_payload #>
                                    '{state_diff,visual_navigation_transition,reason_codes}',
                                action.result_payload #>
                                    '{state_diff,logical_surface_transition,visual_proof,reason_codes}'
                            )
                        ))
                    END AS visual_navigation_transition,
                    action.error_code AS failure_code,
                    action.error_message AS failure_message,
                    action.created_at,
                    action.started_at,
                    action.action_finished_at,
                    action.evidence_finished_at,
                    action.updated_at
                FROM qc_action_execution AS action
                CROSS JOIN LATERAL (
                    SELECT COALESCE(
                        action.intent_payload #>> '{parameters,target_mode}'
                            = 'visual_navigation_coordinate'
                        OR action.intent_payload->'parameters'
                            ? 'visual_navigation_capability'
                        OR NULLIF(
                            action.intent_payload #>
                                '{parameters,visual_navigation_provenance}',
                            'null'::jsonb
                        ) IS NOT NULL
                        OR NULLIF(
                            action.intent_payload #>
                                '{parameters,visual_target_kind}',
                            'null'::jsonb
                        ) IS NOT NULL,
                        FALSE
                    ) AS is_visual_navigation_provenance
                ) AS visual_provenance
                LEFT JOIN qc_page_capture AS requested_source
                  ON requested_source.id = action.requested_source_capture_id
                 AND requested_source.task_id = action.task_id
                WHERE action.task_id = $1::uuid
                  AND EXISTS (
                      SELECT 1
                      FROM qc_v2_work_item AS work
                      WHERE work.task_id = action.task_id
                        AND work.session_id = action.session_id
                        AND work.work_payload #>>
                              '{frontier,observation,metadata,agent_run_id}' = $2
                  )
                ORDER BY action.command_sequence, action.created_at,
                         action.action_execution_id
                """,
                task_id,
                agent_run_id,
            )
        result = []
        for row in rows:
            item = dict(row)
            item["parameters"] = _json_dict(item.get("parameters"))
            item["result_payload"] = _json_dict(item.get("result_payload"))
            item["logical_surface_transition"] = _json_dict(item.get("logical_surface_transition"))
            item["visual_navigation_transition"] = _json_dict(
                item.get("visual_navigation_transition")
            )
            item["exploration_action_id"] = item["action_execution_id"]
            result.append(item)
        return result

    async def fetch_surface_session_summary(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        """读取当前 Run 的 Surface Session 与引用资源收口状态。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH scoped_sessions AS (
                    SELECT session.*
                    FROM qc_surface_session AS session
                    WHERE session.task_id = $1::uuid
                      AND (
                          EXISTS (
                              SELECT 1
                              FROM qc_capture AS capture
                              WHERE capture.task_id = session.task_id
                                AND capture.agent_run_id = $2::uuid
                                AND capture.surface_session_id = session.session_id
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM qc_v2_work_item AS work
                              WHERE work.task_id = session.task_id
                                AND work.session_id = session.session_id
                                AND work.work_payload #>>
                                    '{frontier,observation,metadata,agent_run_id}' = $2::text
                          )
                      )
                )
                SELECT
                    session.session_id,
                    session.surface_kind,
                    session.status,
                    session.command_sequence,
                    session.current_capture_id::text AS current_capture_id,
                    session.failure_reason,
                    session.started_at,
                    session.updated_at,
                    count(reference.surface_ref_id)::integer AS reference_count,
                    count(reference.surface_ref_id) FILTER (
                        WHERE reference.status NOT IN ('released', 'lost')
                    )::integer AS nonterminal_reference_count
                FROM scoped_sessions AS session
                LEFT JOIN qc_surface_ref AS reference
                  ON reference.task_id = session.task_id
                 AND reference.session_id = session.session_id
                GROUP BY
                    session.session_id,
                    session.surface_kind,
                    session.status,
                    session.command_sequence,
                    session.current_capture_id,
                    session.failure_reason,
                    session.started_at,
                    session.updated_at
                ORDER BY session.started_at, session.session_id
                """,
                task_id,
                agent_run_id,
            )
        items = [dict(row) for row in rows]
        terminal_statuses = {"closed", "lost", "failed"}
        status_counts: dict[str, int] = {}
        for item in items:
            status = str(item.get("status") or "")
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "total_count": len(items),
            "terminal_count": sum(1 for item in items if item.get("status") in terminal_statuses),
            "nonterminal_count": sum(
                1 for item in items if item.get("status") not in terminal_statuses
            ),
            "reference_count": sum(int(item.get("reference_count") or 0) for item in items),
            "nonterminal_reference_count": sum(
                int(item.get("nonterminal_reference_count") or 0) for item in items
            ),
            "status_counts": status_counts,
            "items": items,
        }

    async def list_h5_page_captures(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        """读取当前 H5 Run 的正式 Capture；不依赖旧页面节点。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    common_capture.id::text AS page_capture_id,
                    common_capture.agent_run_id::text AS agent_run_id,
                    jsonb_build_object(
                        'navigation_scope', COALESCE(
                            common_capture.metadata->'navigation_scope',
                            common_capture.metadata #> '{pipeline_metrics,navigation_scope}'
                        ),
                        'diagnostic_capture', common_capture.metadata->'diagnostic_capture'
                    ) AS metadata,
                    COALESCE(
                        page_capture.parent_page_capture_id,
                        common_capture.parent_capture_id
                    )::text AS parent_page_capture_id,
                    page_capture.page_seq,
                    page_capture.page_type,
                    page_capture.page_state,
                    page_capture.url,
                    common_capture.captured_at,
                    COALESCE(
                        common_capture.metadata #> '{pipeline_metrics,navigation_retry}',
                        page_capture.metadata #> '{pipeline_metrics,navigation_retry}'
                    ) AS navigation_retry,
                    CASE
                        WHEN COALESCE(
                            common_capture.metadata #>
                                '{pipeline_metrics,page_settle_diagnostics}',
                            page_capture.metadata #>
                                '{pipeline_metrics,page_settle_diagnostics}'
                        ) IS NOT NULL
                        THEN jsonb_strip_nulls(jsonb_build_object(
                            'outcome', COALESCE(
                                common_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,outcome}',
                                page_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,outcome}'
                            ),
                            'blocking_reasons', COALESCE(
                                common_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,blocking_reasons}',
                                page_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,blocking_reasons}'
                            ),
                            'dynamic_reasons', COALESCE(
                                common_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,dynamic_reasons}',
                                page_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,dynamic_reasons}'
                            ),
                            'sample_count', COALESCE(
                                common_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,sample_count}',
                                page_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,sample_count}'
                            ),
                            'stable_sample_count', COALESCE(
                                common_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,stable_sample_count}',
                                page_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,stable_sample_count}'
                            ),
                            'page_url_sha256', COALESCE(
                                common_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,page_url_sha256}',
                                page_capture.metadata #>
                                    '{pipeline_metrics,page_settle_diagnostics,page_url_sha256}'
                            )
                        ))
                    END AS page_settle_diagnostics,
                    COALESCE(
                        page_capture.detection_status,
                        page_capture.metadata #>> '{page_detection,detection_status}',
                        common_capture.metadata #>> '{page_detection,detection_status}'
                    ) AS detection_status,
                    COALESCE(
                        page_capture.metadata #> '{page_detection,logical_surface}',
                        common_capture.metadata #> '{page_detection,logical_surface}',
                        logical_assessment.assessment_payload
                            #> '{detection_result,logical_surface}'
                    ) AS logical_surface,
                    CASE
                        WHEN COALESCE(
                            page_capture.metadata #>>
                                '{page_detection,visual_navigation_audit,protocol_version}',
                            common_capture.metadata #>>
                                '{page_detection,visual_navigation_audit,protocol_version}',
                            logical_assessment.assessment_payload #>>
                                '{detection_result,visual_navigation_audit,protocol_version}'
                        ) = 'visual_navigation_audit.v1'
                        THEN jsonb_strip_nulls(jsonb_build_object(
                            'protocol_version', 'visual_navigation_audit.v1',
                            'mode', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,mode}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,mode}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,mode}'
                            ),
                            'detector_status', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,detector_status}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,detector_status}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,detector_status}'
                            ),
                            'prediction_count', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,prediction_count}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,prediction_count}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,prediction_count}'
                            ),
                            'target_kind_counts', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,target_kind_counts}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,target_kind_counts}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,target_kind_counts}'
                            ),
                            'conflict_codes', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,conflict_codes}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,conflict_codes}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,conflict_codes}'
                            ),
                            'allowed_prediction_count', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,allowed_prediction_count}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,allowed_prediction_count}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,allowed_prediction_count}'
                            ),
                            'signed_capability_count', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,signed_capability_count}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,signed_capability_count}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,signed_capability_count}'
                            ),
                            'verified_capability_count', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,verified_capability_count}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,verified_capability_count}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,verified_capability_count}'
                            ),
                            'enforced_candidate_count', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,enforced_candidate_count}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,enforced_candidate_count}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,enforced_candidate_count}'
                            ),
                            'policy_rejection_counts', COALESCE(
                                page_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,policy_rejection_counts}',
                                common_capture.metadata #>
                                    '{page_detection,visual_navigation_audit,policy_rejection_counts}',
                                logical_assessment.assessment_payload #>
                                    '{detection_result,visual_navigation_audit,policy_rejection_counts}'
                            ),
                            'navigation_knowledge', jsonb_strip_nulls(jsonb_build_object(
                                'match_type_counts', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,navigation_knowledge,match_type_counts}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,navigation_knowledge,match_type_counts}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,navigation_knowledge,match_type_counts}'
                                ),
                                'repository_degraded_count', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,navigation_knowledge,repository_degraded_count}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,navigation_knowledge,repository_degraded_count}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,navigation_knowledge,repository_degraded_count}'
                                )
                            )),
                            'model_usage', jsonb_strip_nulls(jsonb_build_object(
                                'request_count', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,request_count}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,request_count}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,model_usage,request_count}'
                                ),
                                'elapsed_ms', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,elapsed_ms}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,elapsed_ms}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,model_usage,elapsed_ms}'
                                ),
                                'input_tokens', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,input_tokens}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,input_tokens}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,model_usage,input_tokens}'
                                ),
                                'output_tokens', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,output_tokens}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,output_tokens}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,model_usage,output_tokens}'
                                ),
                                'cache_hit_tokens', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,cache_hit_tokens}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,cache_hit_tokens}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,model_usage,cache_hit_tokens}'
                                ),
                                'total_tokens', COALESCE(
                                    page_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,total_tokens}',
                                    common_capture.metadata #>
                                        '{page_detection,visual_navigation_audit,model_usage,total_tokens}',
                                    logical_assessment.assessment_payload #>
                                        '{detection_result,visual_navigation_audit,model_usage,total_tokens}'
                                )
                            ))
                        ))
                    END AS visual_navigation_audit,
                    COALESCE(
                        page_capture.metadata #>> '{page_detection,confidence_source}',
                        common_capture.metadata #>> '{page_detection,confidence_source}',
                        logical_assessment.assessment_payload
                            #>> '{detection_result,confidence_source}'
                    ) AS confidence_source,
                    COALESCE(
                        page_capture.detection_confidence,
                        CASE
                            WHEN page_capture.metadata #>> '{page_detection,confidence}'
                                ~ '^[0-9]+([.][0-9]+)?$'
                            THEN (
                                page_capture.metadata #>> '{page_detection,confidence}'
                            )::double precision
                        END,
                        CASE
                            WHEN common_capture.metadata #>> '{page_detection,confidence}'
                                ~ '^[0-9]+([.][0-9]+)?$'
                            THEN (
                                common_capture.metadata #>> '{page_detection,confidence}'
                            )::double precision
                        END
                    ) AS detection_confidence,
                    page_capture.detection_duration_ms,
                    page_capture.element_count,
                    page_capture.capture_status,
                    page_capture.inspection_lifecycle_status,
                    COALESCE(
                        page_capture.inspection_status,
                        common_capture.metadata #>> '{inspection,status}',
                        common_capture.metadata->>'inspection_status'
                    ) AS inspection_status,
                    COALESCE(
                        page_capture.inspection_failure_stage,
                        common_capture.metadata->>'inspection_failure_stage'
                    ) AS inspection_failure_stage,
                    COALESCE(
                        page_capture.inspection_failure_code,
                        common_capture.metadata->>'inspection_failure_code'
                    ) AS inspection_failure_code,
                    COALESCE(
                        page_capture.inspection_failure_message,
                        common_capture.metadata->>'inspection_failure_message'
                    ) AS inspection_failure_message,
                    page_capture.inspection_failure_retryable,
                    page_capture.inspection_abandon_reason,
                    common_capture.action_execution_id,
                    common_capture.session_command_sequence,
                    page_capture.capture_role,
                    COALESCE(
                        logical_assessment.assessment_payload->>'page_role',
                        common_capture.metadata #>> '{web_page_type_assessment,page_role}',
                        page_capture.metadata #>> '{web_page_type_assessment,page_role}',
                        common_capture.metadata->>'page_role',
                        page_capture.metadata->>'page_role'
                    ) AS page_role,
                    COALESCE(
                        common_capture.metadata->>'logical_page_id',
                        page_capture.metadata->>'logical_page_id'
                    ) AS logical_page_id,
                    COALESCE(
                        common_capture.metadata->>'capture_purpose',
                        page_capture.metadata->>'capture_purpose',
                        page_capture.capture_role
                    ) AS capture_purpose,
                    COALESCE(
                        common_capture.metadata->>'menu_entry_id',
                        page_capture.metadata->>'menu_entry_id'
                    ) AS menu_entry_id,
                    COALESCE(
                        common_capture.metadata->'menu_entry_ids',
                        page_capture.metadata->'menu_entry_ids',
                        '[]'::jsonb
                    ) AS menu_entry_ids,
                    ARRAY(
                        SELECT artifact.id::text
                        FROM qc_evidence_artifact AS artifact
                        WHERE artifact.page_capture_id = common_capture.id
                          AND artifact.deleted_at IS NULL
                          AND artifact.lifecycle_status IN ('active', 'archived')
                        ORDER BY artifact.created_at, artifact.id
                    ) AS artifact_ids,
                    (
                        SELECT artifact.id::text
                        FROM qc_evidence_artifact AS artifact
                        WHERE artifact.page_capture_id = common_capture.id
                          AND artifact.artifact_type = 'screenshot'
                          AND artifact.deleted_at IS NULL
                          AND artifact.lifecycle_status IN ('active', 'archived')
                        ORDER BY artifact.created_at, artifact.id
                        LIMIT 1
                    ) AS screenshot_artifact_id
                FROM qc_capture AS common_capture
                INNER JOIN qc_page_capture AS page_capture
                  ON page_capture.id = common_capture.id
                 AND page_capture.task_id = common_capture.task_id
                 AND page_capture.agent_run_id = $2::uuid
                LEFT JOIN LATERAL (
                    SELECT stored.assessment_payload
                    FROM qc_h5_page_type_assessment AS stored
                    WHERE stored.task_id = common_capture.task_id
                      AND stored.agent_run_id = $2::uuid
                      AND stored.page_capture_id = common_capture.id
                    ORDER BY stored.updated_at DESC, stored.assessment_record_id DESC
                    LIMIT 1
                ) AS logical_assessment ON TRUE
                WHERE common_capture.task_id = $1::uuid
                  AND common_capture.agent_run_id = $2::uuid
                ORDER BY page_capture.page_seq, common_capture.captured_at,
                         common_capture.id
                """,
                task_id,
                agent_run_id,
            )
        result = []
        for row in rows:
            item = dict(row)
            item["visual_navigation_audit"] = _json_dict(item.get("visual_navigation_audit"))
            item["metadata"] = _json_dict(item.get("metadata"))
            raw_navigation_retry = item.get("navigation_retry")
            item["navigation_retry"] = (
                _json_dict(raw_navigation_retry)
                if raw_navigation_retry is not None
                else None
            )
            raw_settle_diagnostics = item.get("page_settle_diagnostics")
            item["page_settle_diagnostics"] = (
                _json_dict(raw_settle_diagnostics)
                if raw_settle_diagnostics is not None
                else None
            )
            result.append(item)
        return result

    async def fetch_h5_visual_navigation_usage(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        """按固定 operation 汇总视觉模型安全数值用量。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    COALESCE(sum(event.ai_calls), 0)::bigint AS request_count,
                    COALESCE(sum(event.input_tokens), 0)::bigint AS input_tokens,
                    COALESCE(sum(event.output_tokens), 0)::bigint AS output_tokens,
                    COALESCE(sum(event.cache_hit_tokens), 0)::bigint
                        AS cache_hit_tokens,
                    COALESCE(sum(event.total_tokens), 0)::bigint AS total_tokens
                FROM qc_llm_usage_event AS event
                WHERE event.task_id = $1::uuid
                  AND event.agent_run_id = $2::uuid
                  AND event.operation = 'h5_visual_navigation_detection'
                """,
                task_id,
                agent_run_id,
            )
        return dict(row) if row is not None else {}

    async def list_h5_page_type_assessments(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        """读取 0049 H5 页面类型 Assessment 事实。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH ranked_assessments AS (
                SELECT
                    assessment_record_id,
                    assessment_id,
                    session_id,
                    page_capture_id::text AS page_capture_id,
                    detection_status,
                    page_type,
                    page_state,
                    status,
                    reason_codes,
                    safety_summary,
                    assessment_payload,
                    schema_version,
                    payload_hash,
                    row_version,
                    created_at,
                    updated_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY page_capture_id
                        ORDER BY
                            CASE status
                                WHEN 'covered' THEN 3
                                WHEN 'requires_auth' THEN 3
                                WHEN 'blocked_by_safety' THEN 3
                                WHEN 'not_applicable' THEN 3
                                WHEN 'not_found' THEN 2
                                ELSE 1
                            END DESC,
                            COALESCE(
                                (regexp_match(schema_version, '\\.v([0-9]+)$'))[1]::integer,
                                0
                            ) DESC,
                            updated_at DESC,
                            assessment_record_id DESC
                    ) AS assessment_rank
                FROM qc_h5_page_type_assessment
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                )
                SELECT
                    assessment_record_id, assessment_id, session_id,
                    page_capture_id, detection_status, page_type, page_state,
                    status, reason_codes, safety_summary, assessment_payload,
                    schema_version, payload_hash, row_version, created_at, updated_at
                FROM ranked_assessments
                WHERE assessment_rank = 1
                ORDER BY page_capture_id, assessment_record_id
                """,
                task_id,
                agent_run_id,
            )
        result = []
        for row in rows:
            item = dict(row)
            item["reason_codes"] = _json_loads(item.get("reason_codes")) or []
            item["safety_summary"] = _json_dict(item.get("safety_summary"))
            item["assessment_payload"] = _json_dict(item.get("assessment_payload"))
            result.append(item)
        return result

    async def list_h5_evidence_inventory(
        self, *, task_id: str, agent_run_id: str
    ) -> list[dict[str, Any]]:
        """读取 0049 H5 Evidence Inventory 事实。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    inventory_item_id,
                    session_id,
                    action_execution_id,
                    before_capture_id::text AS before_capture_id,
                    after_capture_id::text AS after_capture_id,
                    page_type,
                    page_state,
                    status,
                    reason_codes,
                    required_artifacts,
                    found_artifacts,
                    verification_summary,
                    before_manifest_id,
                    before_manifest_hash,
                    after_manifest_id,
                    after_manifest_hash,
                    authority_source,
                    published_inspection_count,
                    schema_version,
                    payload_hash,
                    row_version,
                    verified_at,
                    created_at,
                    updated_at
                FROM qc_h5_evidence_inventory_item
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                ORDER BY updated_at, inventory_item_id
                """,
                task_id,
                agent_run_id,
            )
        result = []
        for row in rows:
            item = dict(row)
            for key in ("reason_codes", "required_artifacts", "found_artifacts"):
                item[key] = _json_loads(item.get(key)) or []
            item["verification_summary"] = _json_dict(item.get("verification_summary"))
            result.append(item)
        return result

    async def fetch_h5_coverage_snapshot(
        self, *, task_id: str, agent_run_id: str
    ) -> dict[str, Any] | None:
        """读取本轮 H5 覆盖；门禁已绑定时只读它实际消费的快照。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    coverage.agent_run_id::text AS agent_run_id,
                    gate.gate_snapshot_id,
                    gate.finalization_epoch AS gate_finalization_epoch,
                    coverage.coverage_snapshot_id,
                    coverage.finalization_epoch,
                    coverage.revision,
                    coverage.authority_source,
                    coverage.required_page_types,
                    coverage.covered_page_types,
                    coverage.missing_page_types,
                    coverage.coverage_scopes AS scopes,
                    coverage.assessment_scope_ready,
                    coverage.frontier_terminal,
                    coverage.completion_ready,
                    coverage.schema_version,
                    coverage.payload_hash,
                    coverage.created_at
                FROM qc_task AS task
                LEFT JOIN qc_completion_gate_snapshot AS gate
                    ON gate.gate_snapshot_id = task.completion_gate_snapshot_id
                   AND gate.task_id = task.id
                   AND gate.finalization_epoch = task.finalization_epoch
                INNER JOIN LATERAL (
                    SELECT *
                    FROM qc_h5_coverage_snapshot AS snapshot
                    WHERE snapshot.task_id = task.id
                      AND snapshot.agent_run_id = $2::uuid
                      AND (
                          (task.completion_gate_snapshot_id IS NULL
                           AND snapshot.finalization_epoch = task.finalization_epoch)
                          OR
                          (snapshot.coverage_snapshot_id = gate.h5_coverage_snapshot_id
                           AND snapshot.payload_hash = gate.h5_coverage_snapshot_hash
                           AND snapshot.finalization_epoch = gate.h5_coverage_snapshot_epoch)
                      )
                    ORDER BY snapshot.revision DESC
                    LIMIT 1
                ) AS coverage ON TRUE
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'h5'
                  AND (task.finalization_agent_run_id IS NULL
                       OR task.finalization_agent_run_id = $2::uuid)
                """,
                task_id,
                agent_run_id,
            )
        if row is None:
            return None
        result = dict(row)
        for key in (
            "required_page_types",
            "covered_page_types",
            "missing_page_types",
            "scopes",
        ):
            result[key] = _json_loads(result.get(key)) or []
        return result

    async def fetch_web_coverage_snapshot(
        self, *, task_id: str, agent_run_id: str
    ) -> dict[str, Any] | None:
        """读取当前 Web 收口 epoch 的不可变 Coverage Snapshot 安全投影。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    snapshot.id::text AS coverage_snapshot_id,
                    snapshot.finalization_epoch,
                    snapshot.required_page_types,
                    snapshot.observed_page_types,
                    snapshot.missing_page_types,
                    snapshot.unrecognized_page_types,
                    snapshot.normalization_version,
                    COALESCE(normalization.records, '[]'::jsonb)
                        AS normalization_records,
                    snapshot.coverage_status,
                    snapshot.snapshot_hash,
                    snapshot.created_at,
                    snapshot.applicability_snapshot,
                    snapshot.applicability_snapshot->'lifecycle_observations'
                        AS observed_lifecycle_page_types,
                    snapshot.applicability_snapshot->'unknown_page_capture_ids'
                        AS unknown_page_capture_ids,
                    snapshot.applicability_snapshot->'supplemental_evidence_summary'
                        AS supplemental_evidence_summary,
                    COALESCE(gaps.rule_codes, ARRAY[]::varchar[])
                        AS gap_rule_codes
                FROM qc_task AS task
                INNER JOIN qc_web_coverage_snapshot AS snapshot
                    ON snapshot.task_id = task.id
                   AND snapshot.agent_run_id = $2::uuid
                   AND snapshot.finalization_epoch = task.finalization_epoch
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'raw_page_type', grouped.raw_page_type,
                            'canonical_page_type', grouped.canonical_page_type,
                            'capture_count', grouped.capture_count
                        )
                        ORDER BY grouped.raw_page_type, grouped.canonical_page_type
                    ) AS records
                    FROM (
                        SELECT
                            COALESCE(
                                NULLIF(BTRIM(
                                    page.metadata
                                        ->'web_page_type_assessment'
                                        ->>'proposed_page_type'
                                ), ''),
                                NULLIF(BTRIM(
                                    page.metadata
                                        ->'web_page_type_normalization'
                                        ->>'raw_page_type'
                                ), ''),
                                page.page_type
                            ) AS raw_page_type,
                            COALESCE(
                                NULLIF(BTRIM(
                                    page.metadata
                                        ->'web_page_type_assessment'
                                        ->>'final_page_type'
                                ), ''),
                                CASE NULLIF(BTRIM(
                                    page.metadata
                                        ->'web_page_type_normalization'
                                        ->>'canonical_page_type'
                                ), '')
                                    WHEN 'web_video_list' THEN 'video_list'
                                    WHEN 'web_video_detail' THEN 'video_detail'
                                    WHEN 'web_content_list' THEN 'content_list'
                                    WHEN 'web_content_detail' THEN 'content_detail'
                                    ELSE NULLIF(BTRIM(
                                        page.metadata
                                            ->'web_page_type_normalization'
                                            ->>'canonical_page_type'
                                    ), '')
                                END
                            ) AS canonical_page_type,
                            count(*)::integer AS capture_count
                        FROM qc_page_capture AS page
                        INNER JOIN qc_capture AS capture
                            ON capture.task_id = task.id
                           AND capture.id = page.id
                           AND capture.agent_run_id = snapshot.agent_run_id
                           AND capture.surface_kind = 'web'
                           AND capture.capture_status = 'captured'
                           AND capture.capture_role = 'after_action'
                        WHERE (
                                page.metadata
                                    ->'web_page_type_assessment'
                                    ->>'version' = snapshot.normalization_version
                                OR page.metadata
                                    ->'web_page_type_normalization'
                                    ->>'version' = snapshot.normalization_version
                              )
                          AND COALESCE(
                                page.inspection_lifecycle_status,
                                capture.metadata->>'inspection_lifecycle_status',
                                ''
                              ) NOT IN ('superseded', 'abandoned')
                        GROUP BY 1, 2
                    ) AS grouped
                ) AS normalization ON TRUE
                LEFT JOIN LATERAL (
                    SELECT array_agg(
                        DISTINCT execution_rule.rule_code
                        ORDER BY execution_rule.rule_code
                    ) AS rule_codes
                    FROM qc_rule_execution AS execution
                    INNER JOIN qc_rule AS execution_rule
                        ON execution_rule.id = execution.rule_id
                    WHERE execution.task_id = task.id
                      AND execution.agent_run_id = snapshot.agent_run_id
                      AND execution.execution_status = 'evidence_insufficient'
                      AND execution.page_capture_id IS NULL
                      AND execution.source_idempotency_key LIKE
                          ('web-coverage-gap:' || snapshot.id::text || ':%')
                      AND execution.judge_input->>'schema_version'
                          = 'web-coverage-gap.v1'
                      AND execution.judge_input->>'source' = 'web_coverage_gap'
                      AND execution.judge_input->>'coverage_snapshot_id'
                          = snapshot.id::text
                      AND execution.judge_input->>'reason'
                          = 'required_page_type_not_discovered'
                      AND snapshot.missing_page_types
                          ? (execution.judge_input->>'missing_page_type')
                ) AS gaps ON TRUE
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'web'
                  AND task.finalization_agent_run_id = snapshot.agent_run_id
                """,
                task_id,
                agent_run_id,
            )
        if row is None:
            return None
        result = dict(row)
        for key in (
            "required_page_types",
            "observed_page_types",
            "missing_page_types",
            "unrecognized_page_types",
            "normalization_records",
        ):
            result[key] = _json_loads(result.get(key)) or []
        result["gap_rule_codes"] = list(result.get("gap_rule_codes") or [])
        applicability = _json_dict(result.get("applicability_snapshot"))
        if applicability:
            result["applicability_snapshot"] = applicability
            if applicability.get("snapshot_protocol_version") == "web-coverage-snapshot.v4":
                for key in ("observed_lifecycle_page_types", "unknown_page_capture_ids", "supplemental_evidence_summary"):
                    # 精确投影封存正文；缺字段保留为空缺，正式报告绑定继续拒绝。
                    result[key] = _json_loads(result.get(key))
        return result

    async def fetch_h5_full_inspection_closure(
        self, *, task_id: str, agent_run_id: str
    ) -> dict[str, Any]:
        """读取 full_inspection 专属的 Inspection 与当前 Completion Gate。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            inspection = await connection.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS inspection_count,
                    COUNT(*) FILTER (
                        WHERE work.status NOT IN (
                            'succeeded', 'partial', 'failed',
                            'cancelled', 'superseded'
                        )
                    )::int AS nonterminal_inspection_count,
                    COUNT(*) FILTER (WHERE work.status = 'failed')::int
                        AS failed_inspection_count
                FROM qc_inspection_work_item AS work
                INNER JOIN qc_page_capture AS capture
                  ON capture.id = work.page_capture_id
                 AND capture.task_id = work.task_id
                 AND capture.agent_run_id = $2::uuid
                WHERE work.task_id = $1::uuid
                  AND work.producer_agent_run_id = $2::uuid
                """,
                task_id,
                agent_run_id,
            )
            gate = await connection.fetchrow(
                """
                SELECT
                    gate.gate_snapshot_id,
                    gate.gate_passed,
                    gate.block_reasons AS gate_block_reasons,
                    gate.evaluated_at
                FROM qc_task AS task
                INNER JOIN qc_completion_gate_snapshot AS gate
                  ON gate.task_id = task.id
                 AND gate.gate_snapshot_id = task.completion_gate_snapshot_id
                 AND gate.finalization_epoch = task.finalization_epoch
                WHERE task.id = $1::uuid
                  AND task.finalization_agent_run_id = $2::uuid
                """,
                task_id,
                agent_run_id,
            )
            publication_rows = await connection.fetch(
                """
                SELECT
                    publication.id::text AS publication_id,
                    publication.mode,
                    publication.status,
                    publication.row_version,
                    publication.attempt_count,
                    publication.max_attempts,
                    publication.next_attempt_at,
                    publication.page_capture_id::text AS page_capture_id,
                    capture.page_type,
                    publication.candidate_rule_count,
                    publication.expected_batch_count,
                    publication.published_work_item_count,
                    publication.excluded_active_session_rule_count,
                    publication.failure_code,
                    publication.failure_message,
                    publication.created_at,
                    publication.updated_at,
                    publication.finished_at,
                    (
                        SELECT count(*)::integer
                        FROM qc_outbox_event AS outbox
                        WHERE outbox.aggregate_type = 'h5_inspection_publication'
                          AND outbox.aggregate_id = publication.id::text
                    ) AS outbox_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE outbox.status = 'published'
                        )::integer
                        FROM qc_outbox_event AS outbox
                        WHERE outbox.aggregate_type = 'h5_inspection_publication'
                          AND outbox.aggregate_id = publication.id::text
                    ) AS outbox_published_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE outbox.status IN (
                                'pending', 'publishing', 'retry_wait'
                            )
                        )::integer
                        FROM qc_outbox_event AS outbox
                        WHERE outbox.aggregate_type = 'h5_inspection_publication'
                          AND outbox.aggregate_id = publication.id::text
                    ) AS outbox_pending_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE outbox.status = 'dead_letter'
                        )::integer
                        FROM qc_outbox_event AS outbox
                        WHERE outbox.aggregate_type = 'h5_inspection_publication'
                          AND outbox.aggregate_id = publication.id::text
                    ) AS outbox_dead_letter_count,
                    (
                        SELECT count(*)::integer
                        FROM qc_event_inbox AS inbox
                        WHERE inbox.aggregate_type = 'h5_inspection_publication'
                          AND inbox.aggregate_id = publication.id::text
                    ) AS inbox_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE inbox.status = 'completed'
                        )::integer
                        FROM qc_event_inbox AS inbox
                        WHERE inbox.aggregate_type = 'h5_inspection_publication'
                          AND inbox.aggregate_id = publication.id::text
                    ) AS inbox_completed_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE inbox.status IN (
                                'pending', 'processing', 'retry_wait'
                            )
                        )::integer
                        FROM qc_event_inbox AS inbox
                        WHERE inbox.aggregate_type = 'h5_inspection_publication'
                          AND inbox.aggregate_id = publication.id::text
                    ) AS inbox_pending_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE inbox.status = 'dead_letter'
                        )::integer
                        FROM qc_event_inbox AS inbox
                        WHERE inbox.aggregate_type = 'h5_inspection_publication'
                          AND inbox.aggregate_id = publication.id::text
                    ) AS inbox_dead_letter_count,
                    (
                        SELECT count(*)::integer
                        FROM qc_h5_inspection_publication_work_item AS link
                        WHERE link.publication_id = publication.id
                    ) AS linked_work_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE work.attempt_count > 0
                               OR work.status NOT IN ('pending', 'retry_wait')
                        )::integer
                        FROM qc_h5_inspection_publication_work_item AS link
                        INNER JOIN qc_inspection_work_item AS work
                            ON work.id = link.work_item_id
                        WHERE link.publication_id = publication.id
                    ) AS claimed_work_count,
                    (
                        SELECT count(*) FILTER (
                            WHERE work.status IN (
                                'succeeded', 'partial', 'failed',
                                'cancelled', 'superseded'
                            )
                        )::integer
                        FROM qc_h5_inspection_publication_work_item AS link
                        INNER JOIN qc_inspection_work_item AS work
                            ON work.id = link.work_item_id
                        WHERE link.publication_id = publication.id
                    ) AS terminal_work_count,
                    (
                        SELECT count(*) FILTER (WHERE work.status = 'failed')::integer
                        FROM qc_h5_inspection_publication_work_item AS link
                        INNER JOIN qc_inspection_work_item AS work
                            ON work.id = link.work_item_id
                        WHERE link.publication_id = publication.id
                    ) AS failed_work_count,
                    (
                        SELECT count(*)::integer
                        FROM qc_h5_inspection_publication_work_item AS link
                        INNER JOIN qc_inspection_attempt AS attempt
                            ON attempt.work_item_id = link.work_item_id
                        WHERE link.publication_id = publication.id
                    ) AS inspection_attempt_count
                FROM qc_h5_inspection_publication AS publication
                INNER JOIN qc_task AS task
                    ON task.id = publication.task_id
                   AND publication.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_page_capture AS capture
                    ON capture.id = publication.page_capture_id
                   AND capture.task_id = task.id
                WHERE publication.task_id = $1::uuid
                  AND publication.source_agent_run_id = $2::uuid
                ORDER BY publication.created_at, publication.id
                """,
                task_id,
                agent_run_id,
            )
        result = (
            dict(inspection)
            if inspection is not None
            else {
                "inspection_count": 0,
                "nonterminal_inspection_count": 0,
                "failed_inspection_count": 0,
            }
        )
        if gate is None:
            result.update(gate_snapshot_id=None, gate_passed=None, gate_block_reasons=[])
        else:
            result.update(dict(gate))
            result["gate_block_reasons"] = _json_loads(result.get("gate_block_reasons")) or []
        publications = []
        status_counts: dict[str, int] = {}
        stage_counts = {
            "publication_persisted": 0,
            "wakeup_delivered": 0,
            "work_claimed": 0,
            "inspection_completed": 0,
        }
        for row in publication_rows:
            item = dict(row)
            status = str(item.get("status") or "")
            status_counts[status] = status_counts.get(status, 0) + 1
            stage = _h5_publication_stage(item)
            stage_counts[stage] += 1
            item["current_stage"] = stage
            publications.append(item)
        result["publication_projection"] = {
            "current_stage": _h5_publication_projection_stage(stage_counts),
            "publication_count": len(publications),
            "status_counts": status_counts,
            "stage_counts": stage_counts,
            "publication_persisted_count": len(publications),
            "wakeup_delivered_count": sum(
                int(item.get("outbox_published_count") or 0) > 0
                or int(item.get("inbox_count") or 0) > 0
                for item in publications
            ),
            "work_claimed_count": sum(
                int(item.get("claimed_work_count") or 0) > 0 for item in publications
            ),
            "inspection_completed_count": stage_counts["inspection_completed"],
            "outbox_pending_count": sum(
                int(item.get("outbox_pending_count") or 0) for item in publications
            ),
            "outbox_dead_letter_count": sum(
                int(item.get("outbox_dead_letter_count") or 0) for item in publications
            ),
            "inbox_pending_count": sum(
                int(item.get("inbox_pending_count") or 0) for item in publications
            ),
            "inbox_dead_letter_count": sum(
                int(item.get("inbox_dead_letter_count") or 0) for item in publications
            ),
            "failed_count": sum(
                status in {"failed", "needs_review", "cancelled", "superseded"}
                for status in (str(item.get("status") or "") for item in publications)
            ),
            "items": publications,
        }
        return result

    async def list_inspection_work_item_rows(
        self,
        *,
        task_id: str,
        control_mode: str | None,
    ) -> dict[str, list[dict[str, Any]]]:
        """读取持久化质检协议的工作项、成员与尝试投影，不返回 Judge 正文。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            work_items = await connection.fetch(
                """
                SELECT
                    work_item.id::text AS work_item_id,
                    work_item.protocol_version,
                    work_item.control_mode,
                    work_item.status,
                    work_item.page_capture_id::text AS page_capture_id,
                    work_item.producer_agent_run_id::text AS producer_agent_run_id,
                    work_item.consumer_agent_run_id::text AS consumer_agent_run_id,
                    work_item.session_owner_agent_run_id::text
                        AS session_owner_agent_run_id,
                    work_item.batch_key,
                    work_item.strategy,
                    work_item.category,
                    work_item.session_dependency,
                    work_item.input_hash,
                    work_item.priority,
                    work_item.attempt_count,
                    work_item.max_attempts,
                    work_item.lease_owner,
                    work_item.lease_expires_at,
                    work_item.next_attempt_at,
                    work_item.last_error_code,
                    work_item.last_error_message,
                    work_item.created_at,
                    work_item.started_at,
                    work_item.finished_at,
                    work_item.updated_at
                FROM qc_inspection_work_item AS work_item
                WHERE work_item.task_id = $1::uuid
                  AND ($2::varchar IS NULL OR work_item.control_mode = $2::varchar)
                ORDER BY work_item.created_at, work_item.id
                """,
                task_id,
                control_mode,
            )
            rules = await connection.fetch(
                """
                SELECT
                    member.work_item_id::text AS work_item_id,
                    member.rule_id::text AS rule_id,
                    member.rule_code,
                    member.ordinal,
                    member.contract_hash,
                    member.evidence_input_set_id,
                    member.status,
                    member.observation_packet_id::text AS observation_packet_id,
                    member.rule_execution_id::text AS rule_execution_id,
                    member.last_error_code,
                    member.last_error_message,
                    member.updated_at
                FROM qc_inspection_work_item_rule AS member
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = member.work_item_id
                WHERE work_item.task_id = $1::uuid
                  AND ($2::varchar IS NULL OR work_item.control_mode = $2::varchar)
                ORDER BY work_item.created_at, work_item.id, member.ordinal
                """,
                task_id,
                control_mode,
            )
            attempts = await connection.fetch(
                """
                SELECT
                    attempt.work_item_id::text AS work_item_id,
                    attempt.id::text AS attempt_id,
                    attempt.consumer_agent_run_id::text AS consumer_agent_run_id,
                    attempt.attempt_no,
                    attempt.status,
                    attempt.input_hash,
                    attempt.result_payload <> '{}'::jsonb AS has_result_payload,
                    attempt.error_code,
                    attempt.error_message,
                    attempt.started_at,
                    attempt.judged_at,
                    attempt.finished_at,
                    attempt.updated_at
                FROM qc_inspection_attempt AS attempt
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = attempt.work_item_id
                WHERE work_item.task_id = $1::uuid
                  AND ($2::varchar IS NULL OR work_item.control_mode = $2::varchar)
                ORDER BY work_item.created_at, work_item.id, attempt.attempt_no
                """,
                task_id,
                control_mode,
            )
        return {
            "work_items": [dict(row) for row in work_items],
            "rules": [dict(row) for row in rules],
            "attempts": [dict(row) for row in attempts],
        }

    async def list_rule_execution_rows(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        task_join = (
            "JOIN qc_task AS task ON task.id = execution.task_id"
            if access_scope is not None
            else ""
        )
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                {rule_replacement_ctes_sql(scope_sql)}
                SELECT
                    execution.id::text AS execution_id,
                    execution.rule_id::text AS rule_id,
                    rule.rule_code,
                    rule.name AS rule_name,
                    rule.category,
                    rule.severity AS rule_severity,
                    rule.rule_package,
                    rule.trigger_display,
                    rule.check_point,
                    rule.applicable_page_types,
                    rule.sort_order,
                    execution.execution_status,
                    execution.judge_strategy,
                    execution.confidence,
                    execution.severity,
                    execution.result_summary,
                    execution.problem_desc,
                    execution.review_status,
                    execution.review_reason,
                    execution.error_code,
                    execution.error_message,
                    coalesce(
                        execution.judge_output #>> '{{model_output,conflicts,0}}',
                        execution.judge_output #>> '{{conflicts,0}}'
                    ) AS diagnostic_code,
                    -- 沿用结论的记录自己不带证据附件：截图按来源那次判定的显示，
                    -- 否则同一页面被重复采集时，规则结果表里只有真判的那一行有截图
                    CASE
                        WHEN reuse_source.id IS NOT NULL
                         AND COALESCE(
                                jsonb_array_length(execution.evidence_artifact_ids), 0
                             ) = 0
                        THEN reuse_source.evidence_artifact_ids
                        ELSE execution.evidence_artifact_ids
                    END AS evidence_artifact_ids,
                    execution.evidence_input_set_id::text AS evidence_input_set_id,
                    execution.task_id::text AS journey_task_id,
                    execution.judge_output->'wrapper_payload' AS journey_wrapper,
                    journey_contract.contract_payload AS journey_contract_payload,
                    journey_contract.selection_hash AS journey_selection_hash,
                    journey_inputs.input_set_payload AS journey_input_payload,
                    ARRAY(
                        SELECT DISTINCT missing_item
                        FROM (
                            SELECT missing_item
                            FROM jsonb_array_elements_text(
                                CASE
                                    WHEN jsonb_typeof(
                                        execution.judge_output->'missing_evidence'
                                    ) = 'array'
                                    THEN execution.judge_output->'missing_evidence'
                                    ELSE '[]'::jsonb
                                END
                            ) AS missing_item
                            UNION ALL
                            SELECT missing_item
                            FROM jsonb_array_elements_text(
                                CASE
                                    WHEN jsonb_typeof(
                                        execution.judge_output
                                        ->'model_output'
                                        ->'missing_evidence'
                                    ) = 'array'
                                    THEN execution.judge_output
                                        ->'model_output'
                                        ->'missing_evidence'
                                    ELSE '[]'::jsonb
                                END
                            ) AS missing_item
                        ) AS missing_items
                        WHERE btrim(missing_item) <> ''
                        ORDER BY missing_item
                    ) AS missing_evidence,
                    execution.page_capture_id::text AS page_capture_id,
                    execution.agent_run_id::text AS agent_run_id,
                    common_capture.agent_run_id::text AS source_agent_run_id,
                    execution.source_idempotency_key,
                    execution.judge_input,
                    execution.started_at,
                    execution.finished_at,
                    execution.human_verdict,
                    execution.human_verdict_note,
                    execution.human_verdict_by,
                    execution.human_verdict_at,
                    COALESCE(
                        NULLIF(capture.page_type, ''),
                        NULLIF(common_capture.metadata->>'page_type', ''),
                        visual.visual_page_type
                    ) AS capture_page_type,
                    COALESCE(execution.judge_input->>'bootstrap', 'false') = 'true'
                        AS is_bootstrap,
                    __EXECUTION_REASON_SQL__ AS execution_reason,
                    COALESCE(
                        capture.inspection_lifecycle_status,
                        common_capture.metadata->>'inspection_lifecycle_status'
                    ) AS capture_lifecycle_status,
                    COALESCE(
                        capture.inspection_failure_code,
                        common_capture.metadata->>'inspection_failure_code'
                    ) AS capture_failure_code,
                    COALESCE(
                        app_capture.package_name,
                        common_capture.metadata->>'package_name'
                    ) AS capture_package_name,
                    COALESCE(
                        app_capture.activity,
                        common_capture.metadata->>'activity'
                    ) AS capture_activity,
                    COALESCE(
                        app_capture.width,
                        CASE
                            WHEN common_capture.metadata->>'width' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'width')::integer
                        END
                    ) AS capture_width,
                    COALESCE(
                        app_capture.height,
                        CASE
                            WHEN common_capture.metadata->>'height' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'height')::integer
                        END
                    ) AS capture_height
                FROM qc_rule_execution execution
                {task_join}
                JOIN qc_rule rule ON rule.id = execution.rule_id
                LEFT JOIN qc_journey_input_contract journey_contract
                    ON journey_contract.input_set_id=execution.evidence_input_set_id
                    AND journey_contract.task_id=execution.task_id
                LEFT JOIN qc_evidence_input_set journey_inputs
                    ON journey_inputs.input_set_id=journey_contract.input_set_id
                    AND journey_inputs.task_id=execution.task_id
                LEFT JOIN qc_capture AS common_capture
                    ON common_capture.id = execution.page_capture_id
                LEFT JOIN qc_page_capture capture
                    ON capture.id = common_capture.id
                LEFT JOIN qc_app_capture app_capture
                    ON app_capture.capture_id = common_capture.id
                LEFT JOIN qc_app_visual_observation visual
                    ON visual.capture_id = common_capture.id
                -- 原生 UUID 查询避免每条沿用记录扫描同任务的全部结论；严格格式保持旧文本比较。
                LEFT JOIN LATERAL (
                    SELECT source.id, source.evidence_artifact_ids
                    FROM qc_rule_execution AS source
                    WHERE source.task_id = execution.task_id
                      AND source.id = CASE
                          WHEN execution.judge_output->'reuse'->>'source_execution_id'
                              ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
                          THEN (execution.judge_output->'reuse'->>'source_execution_id')::uuid
                      END
                    LIMIT 1
                ) AS reuse_source ON TRUE
                WHERE execution.task_id = $1::uuid
                  AND ($2::uuid IS NULL OR execution.agent_run_id = $2::uuid)
                  {scope_sql}
                  AND execution.id NOT IN (
                      SELECT execution_id FROM superseded_execution_ids
                  )
                  AND NOT {outside_current_h5_run_sql("execution")}
                  AND NOT (
                      COALESCE(
                          capture.inspection_lifecycle_status,
                          common_capture.metadata->>'inspection_lifecycle_status'
                      ) IN ('superseded', 'abandoned')
                      AND COALESCE(execution.judge_input->>'bootstrap', 'false') = 'true'
                      AND execution.judge_output->>'reason' = 'candidate_not_executed'
                  )
                ORDER BY execution.finished_at NULLS LAST, execution.created_at
                """.replace("__EXECUTION_REASON_SQL__", _EXECUTION_REASON_SQL),
                task_id,
                agent_run_id,
                *scope_args,
            )
        normalized = []
        for row in rows:
            item = dict(row)
            item["applicable_page_types"] = _json_str_list(item.get("applicable_page_types"))
            item["evidence_artifact_ids"] = _json_str_list(item.get("evidence_artifact_ids"))
            item["missing_evidence"] = _json_str_list(item.get("missing_evidence"))
            item["judge_input"] = _json_dict(item.get("judge_input"))
            item["confidence"] = (
                float(item["confidence"]) if item.get("confidence") is not None else None
            )
            normalized.append(item)
        return normalized

    async def list_rule_rulesets(self, *, rule_ids: list[str]) -> dict[str, dict[str, Any]]:
        """规则 → 它自己所属的规则集（1期 / 2期 / 3期瑞升…），报告按「几期规则」分组用。

        任务跑组合规则集时，规则仍挂在成员那一期上（组合只是成员的并集），直接看规则自己的规则集。
        按文本比较编号：快照里带着不是 UUID 的编号也只是查不到，不会让整份报告报错。
        """
        if not rule_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    rule.id::text AS rule_id,
                    ruleset.ruleset_code,
                    ruleset.name AS ruleset_name
                FROM qc_rule AS rule
                JOIN qc_ruleset AS ruleset ON ruleset.id = rule.ruleset_id
                WHERE rule.id::text = ANY($1::text[])
                """,
                rule_ids,
            )
        return {
            row["rule_id"]: {
                "ruleset_code": row["ruleset_code"],
                "ruleset_name": row["ruleset_name"],
            }
            for row in rows
        }

    async def fetch_closure_outcome_facts(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
    ) -> dict[str, Any]:
        """读取收口四维所需的事实：完成检查快照、证据缺口、补采请求分布、页型覆盖。

        调用方已经用 fetch_task_context 校验过可见性。完成检查以任务当前收口轮次绑定的快照为准，
        其余三维按同一次运行过滤，不混入别的轮次或运行的事实。
        """
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            # 任务挂着快照就只认那一份；刚通过、报告还在绑定时任务不挂快照，取本轮最新一次
            gate = await connection.fetchrow(
                """
                SELECT
                    gate.gate_passed,
                    gate.block_reasons::text AS block_reasons,
                    gate.exploration_terminal,
                    gate.pending_action_count,
                    gate.pending_capture_commit_count,
                    gate.background_execution_count,
                    gate.nonterminal_enforced_work_item_count,
                    gate.open_review_count,
                    gate.evaluated_at
                FROM qc_task AS task
                INNER JOIN qc_completion_gate_snapshot AS gate
                    ON gate.task_id = task.id
                   AND gate.finalization_epoch = task.finalization_epoch
                WHERE task.id = $1::uuid
                  AND (
                      task.completion_gate_snapshot_id IS NULL
                      OR gate.gate_snapshot_id = task.completion_gate_snapshot_id
                  )
                ORDER BY gate.evaluated_at DESC, gate.evaluation_sequence DESC
                LIMIT 1
                """,
                task_id,
            )
            evidence = await connection.fetchrow(
                """
                SELECT
                    count(*) FILTER (
                        WHERE execution.judge_output ? 'evidence_gap'
                    )::int AS gap_count,
                    count(*) FILTER (
                        WHERE execution.judge_output->'evidence_gap'->'missing'
                              ->'presentation'->>'code' IS NOT NULL
                    )::int AS presentation_violation_count
                FROM qc_rule_execution AS execution
                -- 按质检规则统一口径：规则结果不分 Run
                WHERE execution.task_id = $1::uuid
                """,
                task_id,
            )
            # 补采请求挂在工作项上，按产出工作项的运行归属
            requests = await connection.fetch(
                """
                SELECT request.status, count(*)::int AS request_count
                FROM qc_evidence_request AS request
                WHERE request.task_id = $1::uuid
                  AND (
                      $2::uuid IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM qc_inspection_work_item AS work_item
                          WHERE work_item.id = request.work_item_id
                            AND work_item.producer_agent_run_id = $2::uuid
                      )
                  )
                GROUP BY request.status
                """,
                task_id,
                agent_run_id,
            )
            visited = await connection.fetchval(
                """
                SELECT count(DISTINCT page_type)::int
                FROM qc_page_capture
                WHERE task_id = $1::uuid
                  AND ($2::uuid IS NULL OR agent_run_id = $2::uuid)
                  AND page_type IS NOT NULL
                  AND btrim(page_type) <> ''
                """,
                task_id,
                agent_run_id,
            )
        return {
            "gate": dict(gate) if gate is not None else None,
            "evidence": dict(evidence) if evidence is not None else {},
            "supplement_request_counts": {
                str(row["status"]): int(row["request_count"]) for row in requests
            },
            "visited_page_type_count": int(visited or 0),
        }

    async def list_page_type_judgement_rows(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        """读取页面类型判断记录，连同页面的正式页型与页面图逻辑键，只读。"""
        pool = await self._get_pool()
        task_join = (
            "JOIN qc_task AS task ON task.id = judgement.task_id"
            if access_scope is not None
            else ""
        )
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    judgement.judgement_id,
                    judgement.task_id::text AS task_id,
                    judgement.agent_run_id::text AS agent_run_id,
                    judgement.page_capture_id::text AS page_capture_id,
                    judgement.sequence_no,
                    judgement.source,
                    judgement.page_type,
                    judgement.page_state,
                    judgement.confidence,
                    judgement.catalog_version,
                    judgement.kind,
                    judgement.revises_judgement_id,
                    judgement.basis::text AS basis,
                    judgement.task_finalization_state,
                    judgement.created_at,
                    page.page_type AS formal_page_type,
                    page.page_seq,
                    page.page_slug,
                    page.url AS page_url,
                    graph_state.logical_page_key
                FROM qc_page_type_judgement AS judgement
                {task_join}
                LEFT JOIN qc_page_capture AS page ON page.id = judgement.page_capture_id
                LEFT JOIN qc_page_graph_state AS graph_state
                    ON graph_state.state_id = judgement.page_capture_id
                WHERE judgement.task_id = $1::uuid
                  AND ($2::uuid IS NULL OR judgement.agent_run_id = $2::uuid)
                  {scope_sql}
                ORDER BY page.page_seq NULLS LAST, judgement.page_capture_id, judgement.sequence_no
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
        return [dict(row) for row in rows]

    async def list_rule_reuse_rows(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        """读取沿用了别处结论的规则执行（judge_output.reuse），连同来源执行所在页面，只读。"""
        pool = await self._get_pool()
        task_join = (
            "JOIN qc_task AS task ON task.id = execution.task_id"
            if access_scope is not None
            else ""
        )
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    execution.id::text AS execution_id,
                    execution.rule_id::text AS rule_id,
                    rule.rule_code,
                    rule.name AS rule_name,
                    rule.category,
                    rule.severity AS rule_severity,
                    execution.execution_status,
                    execution.page_capture_id::text AS page_capture_id,
                    COALESCE(page.page_type, '') AS page_type,
                    COALESCE(page.url, '') AS page_url,
                    execution.result_summary,
                    execution.judge_output->>'reuse' AS reuse,
                    source.execution_status AS source_execution_status,
                    COALESCE(source_page.page_type, '') AS source_page_type,
                    COALESCE(source_page.url, '') AS source_page_url,
                    execution.created_at
                FROM qc_rule_execution execution
                {task_join}
                JOIN qc_rule rule ON rule.id = execution.rule_id
                LEFT JOIN qc_page_capture AS page ON page.id = execution.page_capture_id
                LEFT JOIN qc_rule_execution AS source
                    ON source.id::text
                        = execution.judge_output->'reuse'->>'source_execution_id'
                LEFT JOIN qc_page_capture AS source_page
                    ON source_page.id = source.page_capture_id
                WHERE execution.task_id = $1::uuid
                  AND ($2::uuid IS NULL OR execution.agent_run_id = $2::uuid)
                  AND execution.judge_output ? 'reuse'
                  {scope_sql}
                ORDER BY execution.created_at, rule.sort_order NULLS LAST
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["reuse"] = _json_loads(item.get("reuse"))
            normalized.append(item)
        return normalized

    async def list_evidence_gap_rows(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        """读取规则执行里记录的结构化证据缺口（judge_output.evidence_gap），只读。"""
        pool = await self._get_pool()
        execution_task_join = (
            "JOIN qc_task AS task ON task.id = execution.task_id"
            if access_scope is not None
            else ""
        )
        work_task_join = (
            "JOIN qc_task AS task ON task.id = work_item.task_id"
            if access_scope is not None
            else ""
        )
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH scoped_attempts AS MATERIALIZED (
                    -- 先限定任务、Run 与权限，再提取大结果包中的待补证据子对象。
                    SELECT attempt.id, attempt.work_item_id,
                           attempt.judged_at, attempt.updated_at, attempt.created_at,
                           attempt.finished_at,
                           attempt.result_payload->'deferred_decisions' AS deferred_decisions
                    FROM qc_inspection_attempt AS attempt
                    INNER JOIN qc_inspection_work_item AS work_item
                        ON work_item.id = attempt.work_item_id
                    {work_task_join}
                    WHERE work_item.task_id = $1::uuid
                      AND ($2::uuid IS NULL OR work_item.producer_agent_run_id = $2::uuid)
                      {scope_sql}
                ), persisted_gap AS (
                SELECT
                    execution.id::text AS execution_id,
                    execution.rule_id::text AS rule_id,
                    rule.rule_code,
                    rule.name AS rule_name,
                    rule.category,
                    rule.severity AS rule_severity,
                    execution.execution_status,
                    execution.page_capture_id::text AS page_capture_id,
                    execution.agent_run_id::text AS agent_run_id,
                    execution.judge_output->>'evidence_gap' AS evidence_gap,
                    graph_state.logical_page_key,
                    graph_state.state_fingerprint,
                    graph_state.page_type AS graph_page_type,
                    execution.created_at,
                    execution.finished_at,
                    request.status AS request_status,
                    request.resolution_reason AS request_resolution_reason,
                    rule.sort_order AS rule_sort_order
                FROM qc_rule_execution execution
                {execution_task_join}
                JOIN qc_rule rule ON rule.id = execution.rule_id
                LEFT JOIN qc_page_graph_state AS graph_state
                    ON graph_state.state_id = execution.page_capture_id
                -- 补采选路结果：同任务同规则同目标 Capture 的最近一条请求。
                LEFT JOIN LATERAL (
                    SELECT req.status, req.resolution_reason
                    FROM qc_evidence_request AS req
                    WHERE req.task_id = execution.task_id
                      AND req.rule_id = execution.rule_id
                      AND req.target_capture_id = execution.page_capture_id
                    ORDER BY req.created_at DESC
                    LIMIT 1
                ) AS request ON TRUE
                WHERE execution.task_id = $1::uuid
                  AND ($2::uuid IS NULL OR execution.agent_run_id = $2::uuid)
                  AND execution.judge_output ? 'evidence_gap'
                  {scope_sql}
                ),
                deferred_gap AS (
                SELECT
                    'deferred:' || attempt.id::text || ':' || member.rule_id::text
                        AS execution_id,
                    member.rule_id::text AS rule_id,
                    rule.rule_code,
                    rule.name AS rule_name,
                    rule.category,
                    rule.severity AS rule_severity,
                    deferred->>'execution_status' AS execution_status,
                    work_item.page_capture_id::text AS page_capture_id,
                    work_item.consumer_agent_run_id::text AS agent_run_id,
                    deferred->'judge_output'->>'evidence_gap' AS evidence_gap,
                    graph_state.logical_page_key,
                    graph_state.state_fingerprint,
                    graph_state.page_type AS graph_page_type,
                    COALESCE(attempt.judged_at, attempt.updated_at, attempt.created_at)
                        AS created_at,
                    attempt.finished_at,
                    request.status AS request_status,
                    request.resolution_reason AS request_resolution_reason,
                    rule.sort_order AS rule_sort_order
                FROM scoped_attempts AS attempt
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = attempt.work_item_id
                {work_task_join}
                CROSS JOIN LATERAL jsonb_array_elements(
                    attempt.deferred_decisions
                ) AS deferred
                INNER JOIN qc_inspection_work_item_rule AS member
                    ON member.work_item_id = work_item.id
                   AND member.rule_id::text = deferred->>'rule_id'
                INNER JOIN qc_rule AS rule
                    ON rule.id = member.rule_id
                LEFT JOIN qc_page_graph_state AS graph_state
                    ON graph_state.state_id = work_item.page_capture_id
                LEFT JOIN LATERAL (
                    SELECT req.status, req.resolution_reason
                    FROM qc_evidence_request AS req
                    WHERE req.work_item_id = work_item.id
                      AND req.attempt_id = attempt.id
                      AND req.rule_id = member.rule_id
                    ORDER BY req.created_at DESC
                    LIMIT 1
                ) AS request ON TRUE
                WHERE work_item.task_id = $1::uuid
                  AND ($2::uuid IS NULL OR work_item.producer_agent_run_id = $2::uuid)
                  AND jsonb_typeof(attempt.deferred_decisions) = 'array'
                  AND deferred->'judge_output' ? 'evidence_gap'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_rule_execution AS existing
                      WHERE existing.task_id = work_item.task_id
                        AND existing.rule_id = member.rule_id
                        AND existing.page_capture_id = work_item.page_capture_id
                        AND existing.judge_output ? 'evidence_gap'
                  )
                  {scope_sql}
                )
                SELECT * FROM persisted_gap
                UNION ALL
                SELECT * FROM deferred_gap
                ORDER BY created_at, rule_sort_order NULLS LAST
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.pop("rule_sort_order", None)
            item["evidence_gap"] = _json_loads(item.get("evidence_gap"))
            normalized.append(item)
        return normalized

    async def count_rules_by_page_type(self, *, task_id: str) -> dict[str, int]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT pt AS page_type, COUNT(*)::int AS rule_count
                FROM qc_task task
                JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                CROSS JOIN LATERAL jsonb_array_elements_text(rule.applicable_page_types) AS pt
                WHERE task.id = $1::uuid
                  AND rule.status = 'enabled'
                  AND (rule.channel IS NULL OR rule.channel = task.channel)
                  AND jsonb_typeof(rule.applicable_page_types) = 'array'
                GROUP BY pt
                """,
                task_id,
            )
        return {row["page_type"]: int(row["rule_count"]) for row in rows}

    async def list_applicable_rules(
        self, *, task_id: str, page_types: list[str] | None
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    rule.id::text AS rule_id,
                    rule.rule_code,
                    rule.name AS rule_name,
                    rule.category,
                    rule.severity,
                    rule.executor_type,
                    rule.page_type,
                    rule.rule_package,
                    rule.trigger_display,
                    rule.applicable_page_types,
                    rule.check_point,
                    rule.sort_order
                FROM qc_task task
                JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                WHERE task.id = $1::uuid
                  AND rule.status = 'enabled'
                  AND (rule.channel IS NULL OR rule.channel = task.channel)
                  AND (
                      $2::text[] IS NULL
                      OR (
                          jsonb_typeof(rule.applicable_page_types) = 'array'
                          AND rule.applicable_page_types ?| $2::text[]
                      )
                  )
                ORDER BY rule.sort_order, rule.rule_code
                """,
                task_id,
                page_types,
            )
        normalized = []
        for row in rows:
            item = dict(row)
            item["applicable_page_types"] = _json_str_list(item.get("applicable_page_types"))
            normalized.append(item)
        return normalized

    async def list_logs(
        self,
        *,
        task_id: str,
        level: str | None,
        limit: int | None,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            total = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM qc_task_log
                WHERE task_id = $1::uuid AND ($2::varchar IS NULL OR level = $2)
                """,
                task_id,
                level,
            )
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    ts,
                    level,
                    source,
                    message,
                    page_capture_id::text AS page_capture_id,
                    rule_execution_id::text AS rule_execution_id,
                    context
                FROM qc_task_log
                WHERE task_id = $1::uuid AND ($2::varchar IS NULL OR level = $2)
                ORDER BY id
                LIMIT $3 OFFSET $4
                """,
                task_id,
                level,
                limit,
                offset,
            )
        items = []
        for row in rows:
            item = dict(row)
            item["context"] = _json_dict(item.get("context"))
            items.append(item)
        return items, int(total or 0)

    async def list_screenshot_artifacts(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        artifact_in_scope = page_type_in_inspection_scope_sql(
            task_config_of_sql("artifact.task_id"),
            "COALESCE(capture.page_type, common_capture.metadata->>'page_type', "
            "visual.visual_page_type, '')",
        )
        pool = await self._get_pool()
        task_join = ""  # artifact.task_id 已绑定任务；scope 只需复用该任务别名。
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        if access_scope is not None:
            task_join = "JOIN qc_task AS task ON task.id = artifact.task_id"
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    artifact.id::text AS artifact_id,
                    artifact.page_capture_id::text AS page_capture_id,
                    artifact.checksum,
                    artifact.content_type,
                    artifact.size_bytes,
                    artifact.storage_path,
                    artifact.created_at,
                    artifact.metadata->>'purpose' AS screenshot_purpose,
                    COALESCE(
                        capture.page_type,
                        common_capture.metadata->>'page_type',
                        visual.visual_page_type
                    ) AS page_type,
                    COALESCE(
                        NULLIF(common_capture.metadata->>'title', ''),
                        NULLIF(
                            visual.surface_facts
                                ->'visual_page_name'
                                ->>'page_name',
                            ''
                        )
                    ) AS page_title,
                    COALESCE(
                        app_capture.package_name,
                        common_capture.metadata->>'package_name'
                    ) AS package_name,
                    COALESCE(
                        app_capture.activity,
                        common_capture.metadata->>'activity'
                    ) AS activity,
                    COALESCE(
                        app_capture.width,
                        CASE
                            WHEN common_capture.metadata->>'width' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'width')::integer
                        END
                    ) AS width,
                    COALESCE(
                        app_capture.height,
                        CASE
                            WHEN common_capture.metadata->>'height' ~ '^[0-9]+$'
                            THEN (common_capture.metadata->>'height')::integer
                        END
                    ) AS height,
                    COALESCE(
                        capture.inspection_lifecycle_status,
                        common_capture.metadata->>'inspection_lifecycle_status'
                    ) AS inspection_lifecycle_status,
                    COALESCE(
                        capture.inspection_failure_code,
                        common_capture.metadata->>'inspection_failure_code'
                    ) AS inspection_failure_code,
                    COALESCE(
                        capture.inspection_failure_message,
                        common_capture.metadata->>'inspection_failure_message'
                    ) AS inspection_failure_message,
                    COALESCE(
                        capture.inspection_failure_retryable,
                        CASE
                            WHEN common_capture.metadata->>'inspection_failure_retryable'
                                IN ('true', 'false')
                            THEN (common_capture.metadata->>'inspection_failure_retryable')::boolean
                        END
                    ) AS inspection_failure_retryable,
                    COALESCE(
                        capture.inspection_abandon_reason,
                        common_capture.metadata->>'inspection_abandon_reason'
                    ) AS inspection_abandon_reason,
                    capture.page_seq,
                    common_capture.capture_role,
                    COALESCE(
                        NULLIF(capture.metadata->>'capture_purpose', ''),
                        NULLIF(common_capture.metadata->>'capture_purpose', '')
                    ) AS capture_purpose,
                    COALESCE(
                        assessment.assessment_payload
                            ->'detection_result'
                            ->'logical_surface'
                            ->>'identity_fingerprint',
                        capture.metadata
                            ->'page_detection'
                            ->'logical_surface'
                            ->>'identity_fingerprint'
                    ) AS logical_identity_fingerprint,
                    COALESCE(
                        assessment.assessment_payload
                            ->'detection_result'
                            ->'logical_surface'
                            ->>'state_fingerprint',
                        capture.metadata
                            ->'page_detection'
                            ->'logical_surface'
                            ->>'state_fingerprint'
                    ) AS logical_state_fingerprint,
                    COALESCE(
                        assessment.assessment_payload
                            ->'detection_result'
                            ->'logical_surface'
                            ->'state_signals',
                        capture.metadata
                            ->'page_detection'
                            ->'logical_surface'
                            ->'state_signals'
                    ) AS logical_state_signals,
                    common_capture.captured_at,
                    common_capture.capture_role,
                    COALESCE(visual.overlay_signature, common_capture.metadata->>'overlay_signature')
                        AS overlay_signature,
                    COALESCE(capture.url, common_capture.metadata->>'url', '') AS url,
                    COALESCE(capture.page_state, common_capture.metadata->>'page_state')
                        AS page_state,
                    -- 探索过程展示：展示层评估（最上层/底页可见/诊断）与页面稳定性判定
                    common_capture.metadata->'presentation' AS presentation,
                    common_capture.metadata->'pipeline_metrics'->'page_settle_diagnostics'
                        AS settle_diagnostics
                FROM qc_evidence_artifact artifact
                {task_join}
                JOIN qc_capture AS common_capture
                  ON common_capture.id = artifact.page_capture_id
                LEFT JOIN qc_page_capture capture
                  ON capture.id = common_capture.id
                LEFT JOIN qc_app_capture app_capture
                  ON app_capture.capture_id = common_capture.id
                LEFT JOIN qc_app_visual_observation visual
                  ON visual.capture_id = common_capture.id
                LEFT JOIN LATERAL (
                    SELECT stored.assessment_payload
                    FROM qc_h5_page_type_assessment AS stored
                    WHERE stored.task_id = artifact.task_id
                      AND stored.page_capture_id = artifact.page_capture_id
                    ORDER BY stored.updated_at DESC, stored.assessment_record_id
                    LIMIT 1
                ) AS assessment ON TRUE
                WHERE artifact.task_id = $1::uuid
                  AND artifact.artifact_type = 'screenshot'
                  -- 已清理／生命周期删除的截图不再进画廊，避免前端票证成功后拉图 404。
                  AND artifact.deleted_at IS NULL
                  AND artifact.lifecycle_status IN ('active', 'archived')
                  AND ($2::uuid IS NULL OR common_capture.agent_run_id = $2::uuid)
                  {scope_sql}
                  -- 经过的页面截图不当证据展示，被规则结论用到的除外
                  AND (
                      {artifact_in_scope}
                      OR EXISTS (
                          SELECT 1
                          FROM qc_rule_execution used
                          WHERE used.task_id = artifact.task_id
                            AND used.evidence_artifact_ids ? artifact.id::text
                      )
                  )
                ORDER BY capture.page_seq NULLS LAST,
                         common_capture.session_command_sequence NULLS LAST,
                         artifact.created_at
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
        return [dict(row) for row in rows]

    async def fetch_evidence_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    artifact.id::text AS artifact_id,
                    artifact.task_id::text AS task_id,
                    artifact.page_capture_id::text AS page_capture_id,
                    artifact.artifact_type,
                    artifact.source,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                    artifact.created_at
                FROM qc_evidence_artifact artifact
                WHERE artifact.id = $1::uuid
                """,
                artifact_id,
            )
        return dict(row) if row is not None else None

    async def list_rule_execution_reasons(
        self,
        *,
        task_id: str,
        executions: list[dict[str, Any]],
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> dict[str, str | None]:
        """旧封存正文缺执行原因时，只补同任务原执行记录的原因，不重算冻结结论。"""
        if not executions:
            return {}
        scope_sql, scope_args = _scope_sql(
            access_scope, task_alias="task", first_param=4
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT execution.id::text AS execution_id,
                       {_EXECUTION_REASON_SQL} AS execution_reason
                FROM qc_rule_execution AS execution
                JOIN jsonb_to_recordset($2::jsonb) AS frozen(
                    execution_id text, execution_status text,
                    started_at timestamptz, finished_at timestamptz
                ) ON execution.id::text = frozen.execution_id
                JOIN qc_task AS task ON task.id = execution.task_id
                LEFT JOIN qc_page_capture AS capture ON capture.id = execution.page_capture_id
                LEFT JOIN qc_capture AS common_capture ON common_capture.id = execution.page_capture_id
                WHERE execution.task_id = $1::uuid
                  AND execution.agent_run_id = $3::uuid
                  AND execution.execution_status = frozen.execution_status
                  AND execution.started_at IS NOT DISTINCT FROM frozen.started_at
                  AND execution.finished_at IS NOT DISTINCT FROM frozen.finished_at
                  {scope_sql}
                """,
                task_id,
                json.dumps(executions),
                agent_run_id,
                *scope_args,
            )
        return {row["execution_id"]: row["execution_reason"] for row in rows}

    async def fetch_rule_execution(
        self,
        execution_id: str,
        *,
        task_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any] | None:
        """按执行 ID 取单行，并在用户确认路径复核 task + scope。"""
        if access_scope is not None and task_id is None:
            return None
        pool = await self._get_pool()
        task_path_sql = "AND execution.task_id = $2::uuid" if access_scope is not None else ""
        scope_sql, scope_args = _scope_sql(
            access_scope,
            task_alias="task",
            first_param=3,
            include_review=True,
        )
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT
                    execution.id::text AS execution_id,
                    execution.task_id::text AS task_id,
                    execution.rule_id::text AS rule_id,
                    jsonb_build_object(
                        'task_config',task.task_config,
                        'target_platform',task.target_platform,
                        'finalization_state',task.finalization_state,
                        'app_report_availability',{app_report_availability_sql("run")},
                        'sealed_report_bound',EXISTS (
                            SELECT 1 FROM qc_report_gate_binding binding
                            INNER JOIN qc_report_payload_snapshot payload
                              ON payload.report_id=binding.report_id
                             AND payload.report_version=binding.report_version
                            WHERE binding.task_id=task.id
                              AND binding.gate_snapshot_id=task.completion_gate_snapshot_id
                              AND binding.finalization_epoch=task.finalization_epoch
                        ),
                        'completion_gate_passed',COALESCE((
                            SELECT gate.gate_passed FROM qc_completion_gate_snapshot gate
                            WHERE gate.task_id=task.id
                              AND gate.gate_snapshot_id=task.completion_gate_snapshot_id
                              AND gate.finalization_epoch=task.finalization_epoch LIMIT 1
                        ),FALSE)
                    ) AS green_confirmation_context,
                    execution.execution_status,
                    execution.result_summary,
                    execution.judge_input,
                    execution.judge_strategy,
                    __EXECUTION_REASON_SQL__ AS execution_reason,
                    execution.review_status,
                    coalesce(execution.judge_input->>'bootstrap', 'false') = 'true'
                        AS is_bootstrap,
                    execution.human_verdict,
                    rule.rule_code,
                    rule.name AS rule_name,
                    rule.category
                FROM qc_rule_execution execution
                JOIN qc_task AS task ON task.id = execution.task_id
                LEFT JOIN LATERAL (
                    SELECT id,output_context FROM qc_agent_run
                    WHERE task_id=task.id
                    ORDER BY CASE
                        WHEN task.target_platform IN ('h5','app')
                         AND id=task.finalization_agent_run_id THEN 0
                        WHEN agent_type='main' THEN 1 ELSE 2 END,created_at DESC
                    LIMIT 1
                ) run ON TRUE
                JOIN qc_rule rule ON rule.id = execution.rule_id
                LEFT JOIN qc_capture AS common_capture
                  ON common_capture.id = execution.page_capture_id
                LEFT JOIN qc_page_capture capture
                  ON capture.id = common_capture.id
                WHERE execution.id = $1::uuid
                  {task_path_sql}
                  {scope_sql}
                """.replace("__EXECUTION_REASON_SQL__", _EXECUTION_REASON_SQL),
                execution_id,
                *((task_id,) if access_scope is not None else ()),
                *scope_args,
            )
        if row is None:
            return None
        result = dict(row)
        if "green_confirmation_context" in result:
            result["green_confirmation_context"] = _json_dict(result["green_confirmation_context"])
        return result

    async def update_human_verdict(
        self,
        *,
        execution_id: str,
        verdict: str,
        note: str | None,
        operator: str | None,
        task_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any] | None:
        """写入人工确认终审结论，并同步既有复核流的状态字段。

        review 侧同步口径：pass/ignored → approved、fail → rejected；只在存在
        关联 review_item 或既有 review_status 时生效，不会为无复核流的执行行凭空造复核。
        """
        review_status = "rejected" if verdict == "fail" else "approved"
        if access_scope is not None and task_id is None:
            return None
        # 保持现有 verdict/note/operator 仍占 $2-$5；授权 CTE 使用更高编号，
        # 这样历史内部调用的 SQL 与参数顺序不变。
        scope_sql, scope_args = _scope_sql(
            access_scope,
            task_alias="task",
            first_param=7,
            include_review=True,
        )
        authorization_exists_sql = (
            "AND EXISTS ("
            "SELECT 1 FROM authorized_execution authorized "
            "WHERE authorized.id = qc_rule_execution.id)"
            if access_scope is not None
            else ""
        )
        authorization_cte = (
            f"""
            WITH authorized_execution AS (
                SELECT execution.id
                FROM qc_rule_execution AS execution
                JOIN qc_task AS task ON task.id = execution.task_id
                WHERE execution.id = $1::uuid
                  AND execution.task_id = $6::uuid
                  AND task.deleted_at IS NULL
                  {scope_sql}
            )
            """
            if access_scope is not None
            else ""
        )
        lock_scope_sql, lock_scope_args = _scope_sql(
            access_scope,
            task_alias="task",
            first_param=3,
            include_review=True,
        )
        lock_task_path_sql = (
            "AND execution.task_id = $2::uuid" if access_scope is not None else ""
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            # 与独立审核共用任务→执行→审核的锁顺序；删除持有任务锁时，
            # 等待后重新检查 deleted_at，避免读到删除前快照后再写入人工结论。
            task_status = await connection.fetchval(
                f"""
                SELECT status FROM qc_task AS task
                WHERE task.id = (
                    SELECT execution.task_id FROM qc_rule_execution AS execution
                    WHERE execution.id = $1::uuid
                      {lock_task_path_sql}
                )
                  AND task.deleted_at IS NULL
                  {lock_scope_sql}
                FOR UPDATE OF task
                """,
                execution_id,
                *((task_id,) if access_scope is not None else ()),
                *lock_scope_args,
            )
            if task_status is None:
                return None
            row = await connection.fetchrow(
                f"""
                {authorization_cte}
                UPDATE qc_rule_execution
                SET
                    human_verdict = $2,
                    human_verdict_note = $3,
                    human_verdict_by = $4,
                    human_verdict_at = now(),
                    review_status = CASE
                        WHEN review_status IS NOT NULL OR EXISTS (
                            SELECT 1
                            FROM qc_review_item review
                            WHERE review.rule_execution_id = qc_rule_execution.id
                        ) THEN $5
                        ELSE review_status
                    END,
                    review_reason = CASE
                        WHEN review_status IS NOT NULL OR EXISTS (
                            SELECT 1
                            FROM qc_review_item review
                            WHERE review.rule_execution_id = qc_rule_execution.id
                        ) THEN COALESCE($3, review_reason)
                        ELSE review_reason
                    END,
                    updated_at = now()
                WHERE id = $1::uuid
                  {authorization_exists_sql}
                  AND {requires_human_confirmation_sql("qc_rule_execution")}
                  AND EXISTS (
                      SELECT 1 FROM qc_task AS current_task
                      WHERE current_task.id = qc_rule_execution.task_id
                        AND current_task.deleted_at IS NULL
                  )
                RETURNING
                    id::text AS execution_id,
                    task_id::text AS task_id,
                    agent_run_id::text AS agent_run_id,
                    human_verdict,
                    human_verdict_note,
                    human_verdict_by,
                    human_verdict_at,
                    review_status
                """,
                execution_id,
                verdict,
                note,
                operator,
                review_status,
                *((task_id,) if access_scope is not None else ()),
                *scope_args,
            )
            if row is None:
                return None

            await connection.execute(
                """
                UPDATE qc_review_item
                SET
                    status = $2,
                    review_reason = COALESCE($3, review_reason),
                    reviewed_at = now(),
                    updated_at = now()
                WHERE rule_execution_id = $1::uuid
                """,
                execution_id,
                review_status,
                note,
            )
            gate_managed = await wake_review_finalization(
                connection, task_id=row["task_id"],
            )
            # 按质检规则统一口径：待确认数不分 Run，被真实结论顶掉的占位不算
            pending_confirm_count = await connection.fetchval(
                f"""
                SELECT COUNT(*)
                FROM qc_rule_execution AS execution
                WHERE execution.task_id = $1::uuid
                  AND execution.human_verdict IS NULL
                  AND {requires_human_confirmation_sql("execution")}
                  AND NOT {superseded_placeholder_sql("execution")}
                """,
                row["task_id"],
            )
            if (
                not gate_managed
                and int(pending_confirm_count or 0) == 0
                and task_status == "review"
            ):
                run_completed = row["agent_run_id"] is None or bool(
                    await connection.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM qc_agent_run AS run
                            WHERE run.id = $1::uuid
                              AND run.status = 'done'
                        )
                        """,
                        row["agent_run_id"],
                    )
                )
                if run_completed:
                    completed = await transition_current_task_status_in_transaction(
                        connection,
                        task_id=row["task_id"],
                        target_status=TaskStatus.DONE,
                        allowed_source_statuses=(TaskStatus.REVIEW, TaskStatus.DONE),
                        occurred_at=datetime.now(UTC),
                    )
                    if completed is not None:
                        task_status = completed.status.value
            result = dict(row)
            result["task_status"] = task_status
            return result

    async def count_recognized_pages(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        access_scope: AccessScope | None = None,
    ) -> int:
        """页面识别数：识别成功的采集数，旧数据（无识别列）退化为出现过的页面类型数。"""
        pool = await self._get_pool()
        task_join = (
            "JOIN qc_task AS task ON task.id = common_capture.task_id"
            if access_scope is not None
            else ""
        )
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE COALESCE(
                            page_capture.detection_status,
                            common_capture.metadata->>'detection_status'
                        ) = 'recognized'
                    )::int
                        AS recognized_count,
                    COUNT(DISTINCT COALESCE(
                        page_capture.page_type,
                        common_capture.metadata->>'page_type'
                    ))::int
                        AS typed_count
                FROM qc_capture AS common_capture
                {task_join}
                LEFT JOIN qc_page_capture page_capture
                  ON page_capture.id = common_capture.id
                WHERE common_capture.task_id = $1::uuid
                  AND (
                      $2::uuid IS NULL
                      OR common_capture.agent_run_id = $2::uuid
                  )
                  {scope_sql}
                  -- 经过的页面只留认路记录，不算识别页面数
                  AND {page_type_in_inspection_scope_sql(
                      task_config_of_sql("common_capture.task_id"),
                      "COALESCE(page_capture.page_type, common_capture.metadata->>'page_type', '')",
                  )}
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
        recognized = int(row["recognized_count"] or 0)
        return recognized if recognized > 0 else int(row["typed_count"] or 0)

    async def report_list_meta(
        self,
        task_ids: list[str],
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, dict[str, Any]]:
        """报告列表增强元信息：每个任务的待人工确认数与真实报告生成时间（run 完成时间）。"""
        if not task_ids:
            return {}
        pool = await self._get_pool()
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=2)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    task.id::text AS task_id,
                    run.finished_at AS generated_at,
                    COALESCE(pending.pending_count, 0)::int AS pending_confirm_count,
                    COALESCE(verdicts.passed_count, 0)::int AS passed_count,
                    COALESCE(verdicts.failed_count, 0)::int AS failed_count
                FROM qc_task task
                LEFT JOIN LATERAL (
                    SELECT id, finished_at
                    FROM qc_agent_run
                    WHERE task_id = task.id
                    ORDER BY
                        CASE
                            WHEN task.target_platform = 'h5'
                             AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
                            THEN CASE
                                WHEN id = task.finalization_agent_run_id THEN 0
                                WHEN agent_type = 'main' THEN 1
                                ELSE 2
                            END
                            ELSE 0
                        END,
                        created_at DESC
                    LIMIT 1
                ) run ON TRUE
                -- H5 V2 只统计当前主 Run 及其采集发起的独立判定，其余平台沿用任务级口径。
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS pending_count
                    FROM qc_rule_execution execution
                    WHERE execution.task_id = task.id
                      AND execution.human_verdict IS NULL
                      AND {requires_human_confirmation_sql("execution")}
                      AND NOT {superseded_placeholder_sql("execution")}
                      AND NOT {outside_current_h5_run_sql("execution")}
                ) pending ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) FILTER (
                            WHERE execution.human_verdict = 'pass'
                               OR (execution.human_verdict IS NULL
                                   AND execution.execution_status = 'pass')
                        ) AS passed_count,
                        COUNT(*) FILTER (
                            WHERE execution.human_verdict = 'fail'
                               OR (
                                   execution.human_verdict IS NULL
                                   AND execution.execution_status IN ('fail', 'warning')
                               )
                        ) AS failed_count
                    FROM qc_rule_execution execution
                    WHERE execution.task_id = task.id
                      AND NOT {outside_current_h5_run_sql("execution")}
                ) verdicts ON TRUE
                WHERE task.id = ANY($1::uuid[])
                  {scope_sql}
                """,
                task_ids,
                *scope_args,
            )
        return {
            row["task_id"]: {
                "generated_at": row["generated_at"],
                "pending_confirm_count": int(row["pending_confirm_count"] or 0),
                "passed_count": int(row["passed_count"] or 0),
                "failed_count": int(row["failed_count"] or 0),
            }
            for row in rows
        }

    async def queued_task_overview(self) -> dict[str, Any]:
        """排队中任务概览：数量与最早入队时间，供系统状态接口判断积压/停摆。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT COUNT(*)::int AS queued_count, MIN(created_at) AS oldest_created_at
                FROM qc_task
                WHERE status = 'queued'
                """
            )
        return {
            "queued_count": int(row["queued_count"] or 0),
            "oldest_created_at": row["oldest_created_at"],
        }

    async def list_task_page_types(self, *, task_id: str) -> list[str]:
        """任务实际采集到的页面类型集合（最近一次 Run 优先，含历史兜底）。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT COALESCE(
                    page_capture.page_type,
                    common_capture.metadata->>'page_type'
                ) AS page_type
                FROM qc_capture AS common_capture
                LEFT JOIN qc_page_capture page_capture
                  ON page_capture.id = common_capture.id
                WHERE common_capture.task_id = $1::uuid
                  AND COALESCE(
                      page_capture.page_type,
                      common_capture.metadata->>'page_type'
                  ) IS NOT NULL
                """,
                task_id,
            )
        return [row["page_type"] for row in rows]

    async def fetch_app_stage(self, task_id: str) -> dict[str, Any]:
        """聚合 App Bootstrap / Explorer Work / Completion Gate 只读投影。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            bootstrap = await connection.fetchrow(
                """
                SELECT
                    session_id,
                    stage,
                    installed_package,
                    install_attempt,
                    open_attempt,
                    error_code,
                    error_message,
                    ready_at,
                    updated_at
                FROM qc_app_bootstrap
                WHERE task_id = $1::uuid
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """,
                task_id,
            )
            work_rows = await connection.fetch(
                """
                SELECT
                    work.status::text AS status,
                    COUNT(*)::int AS count
                FROM qc_v2_work_item AS work
                INNER JOIN qc_surface_session AS session
                   ON session.task_id = work.task_id
                  AND session.session_id = work.session_id
                WHERE work.task_id = $1::uuid
                  AND work.work_type = 'exploration'
                GROUP BY work.status
                """,
                task_id,
            )
            gate = await connection.fetchrow(
                """
                SELECT
                    gate_passed,
                    block_reasons,
                    require_nonempty_candidate_inventory,
                    no_rules_applicable,
                    candidate_inventory_satisfied,
                    app_coverage_satisfied,
                    evaluated_at
                FROM qc_completion_gate_snapshot
                WHERE task_id = $1::uuid
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                task_id,
            )
            app_exploration = await connection.fetchrow(
                """
                WITH latest_session AS (
                    SELECT session_id, status
                    FROM qc_surface_session
                    WHERE task_id = $1::uuid
                    ORDER BY updated_at DESC NULLS LAST, started_at DESC, session_id
                    LIMIT 1
                )
                SELECT
                    phase.phase_key AS surface_phase,
                    phase.status AS surface_phase_status,
                    current_page.current_page_identity,
                    result.coverage_summary,
                    task.task_config->'app_exploration_target' AS app_target_snapshot,
                    result.outcome,
                    result.terminal_reason_code,
                    result.evidence_index_id,
                    result.evidence_index_hash,
                    result.started_at,
                    result.completed_at,
                    app_index.status AS evidence_index_status,
                    jsonb_array_length(result.evidence_gaps)::int AS evidence_gap_count,
                    latest_session.status AS surface_session_status,
                    COALESCE(action_stats.action_count, 0)::int AS action_count,
                    COALESCE(reobserve_stats.reobservation_count, 0)::int AS reobservation_count,
                    COALESCE(final_stats.final_observation_count, 0)::int
                        AS final_observation_count,
                    COALESCE(
                        visual_quality.degraded_provider_facts,
                        '[]'::jsonb
                    ) AS degraded_provider_facts
                FROM qc_app_exploration_result AS result
                INNER JOIN qc_task AS task ON task.id = result.task_id
                LEFT JOIN latest_session
                  ON latest_session.session_id = result.surface_session_id
                LEFT JOIN qc_surface_phase_run AS phase
                  ON phase.task_id = result.task_id
                 AND phase.session_id = result.surface_session_id
                 AND phase.phase_key = 'exploration'
                LEFT JOIN LATERAL (
                    SELECT
                        NULLIF(
                            work.work_payload
                                ->'frontier'
                                ->'observation'
                                ->'metadata'
                                ->>'business_page_identity',
                            ''
                        ) AS current_page_identity
                    FROM qc_v2_work_item AS work
                    WHERE work.task_id = result.task_id
                      AND work.session_id = result.surface_session_id
                      AND work.work_type = 'exploration'
                    ORDER BY work.created_at DESC, work.work_id DESC
                    LIMIT 1
                ) AS current_page ON TRUE
                LEFT JOIN qc_app_evidence_index AS app_index
                  ON app_index.index_id = result.evidence_index_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS reobservation_count
                    FROM qc_app_evidence_index_entry AS entry
                    WHERE entry.index_id = result.evidence_index_id
                      AND entry.semantic_role = 'surface_reobserve'
                ) AS reobserve_stats ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS final_observation_count
                    FROM qc_app_evidence_index_entry AS entry
                    WHERE entry.index_id = result.evidence_index_id
                      AND entry.semantic_role = 'final_observation'
                ) AS final_stats ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS action_count
                    FROM qc_action_execution AS action
                    WHERE action.task_id = result.task_id
                      AND action.session_id = result.surface_session_id
                ) AS action_stats ON TRUE
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(
                        visual.surface_facts->'visual_provider_quality'
                        || jsonb_build_object(
                            'capture_id', visual.capture_id::text,
                            'screenshot_artifact_id',
                                visual.screenshot_artifact_id::text
                        )
                        ORDER BY visual.created_at, visual.capture_id
                    ) AS degraded_provider_facts
                    FROM qc_app_visual_observation AS visual
                    WHERE visual.task_id = result.task_id
                      AND visual.session_id = result.surface_session_id
                      AND visual.surface_facts
                          ->'visual_provider_quality'
                          ->>'status' = 'degraded'
                ) AS visual_quality ON TRUE
                WHERE result.task_id = $1::uuid
                ORDER BY result.completed_at DESC
                LIMIT 1
                """,
                task_id,
            )
            app_failure = await connection.fetchrow(
                """
                SELECT
                    failure.phase,
                    failure.reason_code,
                    failure.error_type,
                    failure.retryable,
                    failure.observed_at,
                    failure.recovery_state,
                    failure.sealed_index_id,
                    failure.result_id,
                    failure.reconciled_at,
                    COALESCE(
                        failure.session_status,
                        (
                            SELECT session.status
                            FROM qc_surface_session AS session
                            WHERE session.task_id = failure.task_id
                              AND session.session_id = failure.surface_session_id
                            LIMIT 1
                        )
                    ) AS surface_session_status,
                    app_index.status AS evidence_index_status,
                    app_index.content_sha256 AS evidence_index_hash,
                    COALESCE(jsonb_array_length(app_index.evidence_gaps), 0)::int
                        AS evidence_gap_count
                FROM qc_app_exploration_failure AS failure
                LEFT JOIN qc_app_evidence_index AS app_index
                  ON app_index.index_id = failure.sealed_index_id
                WHERE failure.task_id = $1::uuid
                ORDER BY failure.observed_at DESC
                LIMIT 1
                """,
                task_id,
            )
            inspection = await connection.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS work_item_count,
                    COUNT(*) FILTER (
                        WHERE control_mode = 'enforced'
                    )::int AS enforced_count,
                    COUNT(*) FILTER (
                        WHERE status NOT IN (
                            'succeeded', 'partial', 'failed', 'cancelled', 'superseded'
                        )
                    )::int AS nonterminal_count,
                    COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_count
                FROM qc_inspection_work_item
                WHERE task_id = $1::uuid
                """,
                task_id,
            )
            publication_rows = await connection.fetch(
                """
                SELECT
                    publication.id::text AS publication_id,
                    publication.status,
                    publication.evidence_index_id,
                    publication.evidence_index_hash,
                    publication.row_version,
                    publication.attempt_count,
                    publication.max_attempts,
                    publication.expected_entry_count,
                    publication.terminal_entry_count,
                    publication.expected_rule_count,
                    publication.linked_work_item_count,
                    publication.explicit_exclusion_count,
                    publication.failure_code,
                    publication.failure_message,
                    publication.next_attempt_at,
                    publication.created_at,
                    publication.updated_at,
                    publication.finished_at,
                    COALESCE(outbox.total, 0)::integer AS outbox_count,
                    COALESCE(outbox.published, 0)::integer AS outbox_published_count,
                    COALESCE(outbox.pending, 0)::integer AS outbox_pending_count,
                    COALESCE(outbox.dead_letter, 0)::integer AS outbox_dead_letter_count,
                    COALESCE(inbox.total, 0)::integer AS inbox_count,
                    COALESCE(inbox.pending, 0)::integer AS inbox_pending_count,
                    COALESCE(inbox.dead_letter, 0)::integer AS inbox_dead_letter_count,
                    COALESCE(work.claimed, 0)::integer AS claimed_work_count,
                    COALESCE(work.terminal, 0)::integer AS terminal_work_count
                FROM qc_app_inspection_publication AS publication
                INNER JOIN qc_task AS task
                    ON task.id = publication.task_id
                   AND publication.finalization_epoch = task.finalization_epoch
                LEFT JOIN LATERAL (
                    SELECT count(*) AS total,
                        count(*) FILTER (WHERE status = 'published') AS published,
                        count(*) FILTER (
                            WHERE status IN ('pending', 'publishing', 'retry_wait')
                        ) AS pending,
                        count(*) FILTER (WHERE status = 'dead_letter') AS dead_letter
                    FROM qc_outbox_event
                    WHERE aggregate_type = 'app_inspection_publication'
                      AND aggregate_id = publication.id::text
                ) AS outbox ON TRUE
                LEFT JOIN LATERAL (
                    SELECT count(*) AS total,
                        count(*) FILTER (
                            WHERE status IN ('pending', 'processing', 'retry_wait')
                        ) AS pending,
                        count(*) FILTER (WHERE status = 'dead_letter') AS dead_letter
                    FROM qc_event_inbox
                    WHERE aggregate_type = 'app_inspection_publication'
                      AND aggregate_id = publication.id::text
                ) AS inbox ON TRUE
                LEFT JOIN LATERAL (
                    SELECT count(*) FILTER (
                            WHERE item.attempt_count > 0
                               OR item.status NOT IN ('pending', 'retry_wait')
                        ) AS claimed,
                        count(*) FILTER (
                            WHERE item.status IN (
                                'succeeded', 'partial', 'failed',
                                'cancelled', 'superseded'
                            )
                        ) AS terminal
                    FROM qc_app_inspection_publication_work_item AS link
                    INNER JOIN qc_inspection_work_item AS item
                        ON item.id = link.work_item_id
                    WHERE link.publication_id = publication.id
                ) AS work ON TRUE
                WHERE publication.task_id = $1::uuid
                ORDER BY publication.created_at, publication.id
                """,
                task_id,
            )
            publication_entry_rows = await connection.fetch(
                """
                SELECT
                    entry.publication_id::text AS publication_id,
                    entry.entry_id,
                    entry.sequence_no,
                    entry.capture_id::text AS capture_id,
                    entry.evidence_manifest_id,
                    entry.semantic_role,
                    entry.page_type,
                    entry.page_state,
                    entry.status,
                    entry.candidate_rule_count,
                    entry.linked_work_item_count,
                    entry.explicit_exclusion_count,
                    entry.failure_code,
                    entry.failure_message,
                    entry.created_at,
                    entry.updated_at,
                    entry.finished_at,
                    COALESCE(work.claimed, 0)::integer AS claimed_work_count,
                    COALESCE(work.terminal, 0)::integer AS terminal_work_count
                FROM qc_app_inspection_publication_entry AS entry
                INNER JOIN qc_app_inspection_publication AS publication
                    ON publication.id = entry.publication_id
                INNER JOIN qc_task AS task
                    ON task.id = publication.task_id
                   AND publication.finalization_epoch = task.finalization_epoch
                LEFT JOIN LATERAL (
                    SELECT count(*) FILTER (
                            WHERE item.attempt_count > 0
                               OR item.status NOT IN ('pending', 'retry_wait')
                        ) AS claimed,
                        count(*) FILTER (
                            WHERE item.status IN (
                                'succeeded', 'partial', 'failed',
                                'cancelled', 'superseded'
                            )
                        ) AS terminal
                    FROM qc_app_inspection_publication_work_item AS link
                    INNER JOIN qc_inspection_work_item AS item
                        ON item.id = link.work_item_id
                    WHERE link.publication_id = entry.publication_id
                      AND link.entry_id = entry.entry_id
                ) AS work ON TRUE
                WHERE publication.task_id = $1::uuid
                ORDER BY entry.sequence_no
                """,
                task_id,
            )
        exploration: dict[str, int] = {}
        for row in work_rows:
            exploration[str(row["status"])] = int(row["count"])
        app_exploration_projection = (
            _app_exploration_row_projection(app_exploration)
            if app_exploration is not None
            else None
        )
        failure_projection = (
            _app_failure_row_projection(app_failure) if app_failure is not None else None
        )
        if app_exploration_projection is None:
            app_exploration_projection = failure_projection
        elif failure_projection is not None:
            app_exploration_projection["technical_failure"] = failure_projection[
                "technical_failure"
            ]
            app_exploration_projection["recovery_state"] = failure_projection["recovery_state"]
            app_exploration_projection["evidence_preserved"] = failure_projection[
                "evidence_preserved"
            ]
        if app_exploration_projection is not None:
            app_exploration_projection["completed_work_count"] = sum(
                exploration.get(status, 0)
                for status in (
                    "succeeded",
                    "bounded",
                    "budget_exhausted",
                    "blocked",
                    "no_progress",
                    "failed",
                    "cancelled",
                    "superseded",
                )
            )
            app_exploration_projection["pending_work_count"] = sum(
                exploration.get(status, 0)
                for status in ("pending", "leased", "running", "retry_wait")
            )
        publication_projection = _app_publication_projection(
            publication_rows=publication_rows,
            entry_rows=publication_entry_rows,
        )
        return {
            "bootstrap": dict(bootstrap) if bootstrap is not None else None,
            "exploration_work": exploration,
            "gate": dict(gate) if gate is not None else None,
            "inspection_summary": dict(inspection) if inspection is not None else {},
            "publication_projection": publication_projection,
            "evidence_index": None,
            "app_exploration": app_exploration_projection,
        }

    async def fetch_evidence_index(self, task_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                -- 表名为 qc_app_evidence_index（迁移 0046），且表内没有
                -- index_payload 列；这里按解析方期待的形状用真实列拼出来。
                SELECT
                    index_id,
                    status,
                    jsonb_build_object('evidence_gap', evidence_gaps) AS index_payload,
                    sealed_at
                FROM qc_app_evidence_index
                WHERE task_id = $1::uuid
                ORDER BY sealed_at DESC
                LIMIT 1
                """,
                task_id,
            )
        return _evidence_index_projection(row)

    async def close(self) -> None:
        # 连接池由进程级 PoolManager 统一持有，仓储只释放自身引用。
        self._pool = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool


def _evidence_index_projection(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = _json_dict(row["index_payload"])
    sealed_at = row["sealed_at"]
    return {
        "index_id": str(row["index_id"]),
        "status": str(row["status"]),
        "sealed_at": sealed_at.isoformat() if hasattr(sealed_at, "isoformat") else str(sealed_at),
        "page_timeline": payload.get("page_timeline") or [],
        "capture_lineage": payload.get("capture_lineage") or [],
        "artifacts": payload.get("artifacts") or [],
        "action_logs": payload.get("action_logs") or [],
        "evidence_gap": payload.get("evidence_gap") or [],
        "capture_ids": payload.get("capture_ids") or [],
        "manifest_ids": payload.get("manifest_ids") or [],
        "index_hash": payload.get("index_hash"),
        "session_id": payload.get("session_id"),
    }


def _app_exploration_row_projection(row: Any) -> dict[str, Any]:
    started_at = row["started_at"]
    completed_at = row["completed_at"]
    coverage_summary = _json_dict(row["coverage_summary"])
    target_snapshot = _json_dict(_row_value(row, "app_target_snapshot"))
    degraded_provider_facts = _json_list(_row_value(row, "degraded_provider_facts"))
    if target_snapshot:
        coverage_summary["app_target_snapshot"] = target_snapshot
    coverage_summary["degraded_provider_facts"] = degraded_provider_facts
    coverage_summary["visual_provider_status"] = (
        "degraded" if degraded_provider_facts else "healthy"
    )
    # 对应脚本整份跑完后，封存结果里的视觉标记就是收口依据，不再被降级行改写。
    script_sealed = (
        coverage_summary.get("hlapp_script_completed") is True
        or coverage_summary.get("hjll_script_completed") is True
        or coverage_summary.get("hlwapp_script_completed") is True
        or coverage_summary.get("jmcomic_script_completed") is True
        or coverage_summary.get("jinguo_script_completed") is True
        or coverage_summary.get("yms_script_completed") is True
        or coverage_summary.get("pzhan_script_completed") is True
        or coverage_summary.get("pc51_script_completed") is True
        or coverage_summary.get("hlwwhite_script_completed") is True
        or coverage_summary.get("head_script_completed") is True
        or coverage_summary.get("xingba_script_completed") is True
        or coverage_summary.get("latest_script_completed") is True
        or coverage_summary.get("mama_script_completed") is True
    )
    if not script_sealed:
        coverage_summary["visual_quality_satisfied"] = not degraded_provider_facts
    return {
        "surface_phase": row["surface_phase"],
        "surface_phase_status": row["surface_phase_status"],
        "current_page_identity": row["current_page_identity"],
        "action_count": int(row["action_count"] or 0),
        "reobservation_count": int(row["reobservation_count"] or 0),
        "coverage_summary": coverage_summary,
        "terminal_reason_code": row["terminal_reason_code"],
        "outcome": row["outcome"],
        "evidence_index_id": row["evidence_index_id"],
        "evidence_index_hash": row["evidence_index_hash"],
        "evidence_index_status": row["evidence_index_status"],
        "evidence_gap_count": int(row["evidence_gap_count"] or 0),
        "surface_session_status": row["surface_session_status"],
        "final_observation_count": int(row["final_observation_count"] or 0),
        "started_at": (started_at.isoformat() if hasattr(started_at, "isoformat") else started_at),
        "completed_at": (
            completed_at.isoformat() if hasattr(completed_at, "isoformat") else completed_at
        ),
        "technical_failure": None,
        "recovery_state": None,
        "evidence_preserved": bool(row["evidence_index_id"]),
    }


def _app_failure_row_projection(row: Any) -> dict[str, Any]:
    observed_at = row["observed_at"]
    reconciled_at = row["reconciled_at"]
    sealed_index_id = row["sealed_index_id"]
    recovery_state = str(row["recovery_state"])
    return {
        "surface_phase": row["phase"],
        "surface_phase_status": "failed",
        "current_page_identity": None,
        "action_count": 0,
        "reobservation_count": 0,
        "coverage_summary": {},
        "terminal_reason_code": row["reason_code"],
        "outcome": None,
        "evidence_index_id": sealed_index_id,
        "evidence_index_hash": row["evidence_index_hash"],
        "evidence_index_status": row["evidence_index_status"],
        "evidence_gap_count": int(row["evidence_gap_count"] or 0),
        "surface_session_status": row["surface_session_status"],
        "final_observation_count": 0,
        "started_at": None,
        "completed_at": (
            reconciled_at.isoformat()
            if hasattr(reconciled_at, "isoformat")
            else (observed_at.isoformat() if hasattr(observed_at, "isoformat") else observed_at)
        ),
        "technical_failure": {
            "phase": row["phase"],
            "reason_code": row["reason_code"],
            "error_type": row["error_type"],
            "retryable": bool(row["retryable"]),
            "observed_at": (
                observed_at.isoformat() if hasattr(observed_at, "isoformat") else observed_at
            ),
            "recovery_state": recovery_state,
            "reconciled_at": (
                reconciled_at.isoformat() if hasattr(reconciled_at, "isoformat") else reconciled_at
            ),
        },
        "recovery_state": recovery_state,
        "evidence_preserved": bool(sealed_index_id),
    }


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = _json_loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    parsed = _json_loads(value)
    return parsed if isinstance(parsed, list) else []


def _h5_publication_stage(item: dict[str, Any]) -> str:
    """按当前 epoch 的数据库事实投影 Publication 四阶段。"""

    status = str(item.get("status") or "")
    expected = int(item.get("expected_batch_count") or 0)
    published = int(item.get("published_work_item_count") or 0)
    linked = int(item.get("linked_work_count") or 0)
    terminal = int(item.get("terminal_work_count") or 0)
    completed = (
        status in {"published", "not_applicable"}
        and expected == published == linked
        and linked == terminal
    )
    if completed:
        return "inspection_completed"
    if int(item.get("claimed_work_count") or 0) > 0:
        return "work_claimed"
    if int(item.get("outbox_published_count") or 0) > 0 or int(item.get("inbox_count") or 0) > 0:
        return "wakeup_delivered"
    return "publication_persisted"


def _h5_publication_projection_stage(stage_counts: dict[str, int]) -> str:
    total = sum(stage_counts.values())
    if total == 0:
        return "not_started"
    if stage_counts["inspection_completed"] == total:
        return "inspection_completed"
    if stage_counts["work_claimed"] > 0:
        return "work_claimed"
    if stage_counts["wakeup_delivered"] > 0:
        return "wakeup_delivered"
    return "publication_persisted"


def _app_publication_projection(
    *,
    publication_rows: list[Any],
    entry_rows: list[Any],
) -> dict[str, Any]:
    """把 App Publication 权威事实投影为 sealed/delivered/claimed/judged。"""

    entries_by_publication: dict[str, list[dict[str, Any]]] = {}
    for row in entry_rows:
        entry = dict(row)
        publication_id = str(entry.pop("publication_id"))
        entry["current_stage"] = _app_publication_entry_stage(entry)
        entries_by_publication.setdefault(publication_id, []).append(entry)

    items: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    stage_counts = {"sealed": 0, "delivered": 0, "claimed": 0, "judged": 0}
    for row in publication_rows:
        item = dict(row)
        publication_id = str(item["publication_id"])
        item_entries = entries_by_publication.get(publication_id, [])
        item["entries"] = item_entries
        item["current_stage"] = _app_publication_stage(item, item_entries)
        stage_counts[item["current_stage"]] += 1
        status = str(item.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        items.append(item)

    total = len(items)
    if total == 0:
        current_stage = "not_started"
    elif stage_counts["judged"] == total:
        current_stage = "judged"
    elif stage_counts["claimed"] > 0:
        current_stage = "claimed"
    elif stage_counts["delivered"] > 0:
        current_stage = "delivered"
    else:
        current_stage = "sealed"
    return {
        "current_stage": current_stage,
        "publication_count": total,
        "status_counts": status_counts,
        "stage_counts": stage_counts,
        "sealed_count": total,
        "delivered_count": sum(
            int(item.get("outbox_published_count") or 0) > 0
            or int(item.get("inbox_count") or 0) > 0
            for item in items
        ),
        "claimed_count": sum(int(item.get("claimed_work_count") or 0) > 0 for item in items),
        "judged_count": stage_counts["judged"],
        "outbox_pending_count": sum(int(item.get("outbox_pending_count") or 0) for item in items),
        "outbox_dead_letter_count": sum(
            int(item.get("outbox_dead_letter_count") or 0) for item in items
        ),
        "inbox_pending_count": sum(int(item.get("inbox_pending_count") or 0) for item in items),
        "inbox_dead_letter_count": sum(
            int(item.get("inbox_dead_letter_count") or 0) for item in items
        ),
        "needs_review_count": sum(
            str(item.get("status") or "") == "needs_review" for item in items
        ),
        "failed_count": sum(str(item.get("status") or "") == "failed" for item in items),
        "items": items,
    }


def _app_publication_entry_stage(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    expected = int(item.get("linked_work_item_count") or 0)
    terminal = int(item.get("terminal_work_count") or 0)
    if status in {"not_applicable", "needs_review"} or (
        status == "linked" and expected == terminal
    ):
        return "judged"
    if int(item.get("claimed_work_count") or 0) > 0:
        return "claimed"
    return "sealed"


def _app_publication_stage(
    item: dict[str, Any],
    entries: list[dict[str, Any]],
) -> str:
    status = str(item.get("status") or "")
    expected_entries = int(item.get("expected_entry_count") or 0)
    terminal_entries = int(item.get("terminal_entry_count") or 0)
    linked = int(item.get("linked_work_item_count") or 0)
    terminal_work = int(item.get("terminal_work_count") or 0)
    if (
        status in {"published", "not_applicable", "needs_review"}
        and expected_entries == terminal_entries == len(entries)
        and linked == terminal_work
    ):
        return "judged"
    if int(item.get("claimed_work_count") or 0) > 0:
        return "claimed"
    if int(item.get("outbox_published_count") or 0) > 0 or int(item.get("inbox_count") or 0) > 0:
        return "delivered"
    return "sealed"


def _json_str_list(value: Any) -> list[str]:
    parsed = _json_loads(value)
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _normalize_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url
