"""工作台页面查询仓储。"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.task_progress import (
    running_at_full_progress_should_enter_review,
    whole_task_progress_percent,
)
from services.api.app.application.workstation_task_deletion import DELETABLE_TASK_STATUSES
from services.api.app.application.workstation_tasks import (
    ProductSnapshot,
    WebStageDurations,
    WorkstationTaskDetail,
    WorkstationTaskListFilter,
    WorkstationTaskListItem,
    WorkstationTaskListResult,
    WorkstationTaskOperationResult,
    available_actions_for_status,
    report_status_from_task_status,
    web_execution_phase,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import (
    AdultSelfAttestation,
    TaskExecutionMode,
    TaskTargetPlatform,
)
from services.api.app.domain.task.status import (
    TaskStatus,
    TaskStatusConflictError,
    TaskStatusValidationError,
)
from services.api.app.infrastructure.db.app_report_availability_sql import (
    APP_FORMAL_REPORT_AVAILABLE_SQL,
    APP_REPORT_PREVIEW_AVAILABLE_SQL,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.rule_completion_sql import completed_rule_execution_sql
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    outside_current_h5_run_sql,
    superseded_placeholder_sql,
)
from services.api.app.infrastructure.db.task_status_repository import (
    transition_current_task_status_in_transaction,
)
from services.api.app.infrastructure.db.workstation_task_list_sql import (
    rules_settlement_lateral_sql,
    task_list_query_sql,
)

_WEB_V2_FORMAL_REPORT_AVAILABLE_SQL = """
(
    EXISTS (
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
        INNER JOIN qc_web_coverage_snapshot AS coverage
          ON coverage.task_id = binding.task_id
         AND coverage.agent_run_id::text
             = payload.report_payload->>'agent_run_id'
         AND coverage.finalization_epoch = binding.finalization_epoch
        WHERE binding.task_id = task.id
          AND task.target_platform = 'web'
          AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
          AND task.task_config->>'execution_mode' = 'full_inspection'
          AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
          AND task.finalization_state = 'sealed'
          AND task.finalization_recovery_state = 'verified'
          AND task.finalization_agent_run_id = coverage.agent_run_id
          AND task.completion_gate_snapshot_id = gate.gate_snapshot_id
          AND payload.report_payload #>> '{report_contract,target_platform}' = 'web'
          AND LOWER(payload.report_payload #>> '{report_contract,workflow_version}')
              = 'v2'
          AND payload.report_payload #>> '{report_contract,execution_mode}'
              = 'full_inspection'
          AND payload.report_payload #>> '{report_contract,notification_mode}'
              = 'enforced'
          AND payload.report_payload #>> '{task,task_id}' = task.id::text
          AND payload.report_payload->>'report_id'
              = ('task:' || task.id::text || ':run:' || coverage.agent_run_id::text)
          AND payload.report_payload->>'report_stage' = 'final'
          AND payload.report_payload->>'is_final' = 'true'
          AND payload.report_payload->>'report_binding_version'
              = binding.report_version::text
          AND payload.report_payload #>> '{completion_gate,gate_snapshot_id}'
              = gate.gate_snapshot_id
          AND payload.report_payload #>> '{completion_gate,finalization_epoch}'
              = binding.finalization_epoch::text
          AND payload.report_payload #>> '{completion_gate,gate_passed}' = 'true'
          AND payload.report_payload #>>
              '{page_exploration,web_coverage,coverage_snapshot_id}'
              = coverage.id::text
          AND payload.report_payload #>>
              '{page_exploration,web_coverage,snapshot_hash}'
              = coverage.snapshot_hash
          AND payload.report_payload #>>
              '{page_exploration,web_coverage,finalization_epoch}'
              = coverage.finalization_epoch::text
          AND payload.report_payload #>
              '{page_exploration,web_coverage,required_page_types}'
              = coverage.required_page_types
          AND payload.report_payload #>
              '{page_exploration,web_coverage,observed_page_types}'
              = coverage.observed_page_types
          AND payload.report_payload #>
              '{page_exploration,web_coverage,missing_page_types}'
              = coverage.missing_page_types
          AND payload.report_payload #>
              '{page_exploration,web_coverage,unrecognized_page_types}'
              = coverage.unrecognized_page_types
          AND payload.report_payload #>>
              '{page_exploration,web_coverage,normalization_version}'
              = coverage.normalization_version
          AND payload.report_payload #>>
              '{page_exploration,web_coverage,coverage_status}'
              = coverage.coverage_status
          AND coverage.applicability_snapshot IS NOT NULL
          AND payload.report_payload #>
              '{page_exploration,web_coverage,applicability_snapshot}'
              = coverage.applicability_snapshot
    )
)
"""

_PUBLIC_FAILURE_REASON_MAX_LENGTH = 300


def _public_failure_reason(value: object) -> str | None:
    """将内部异常转换为可公开展示的稳定失败原因。"""

    if not isinstance(value, str):
        return None
    message = " ".join(value.split()).strip()
    if not message:
        return None

    normalized = message.casefold()
    if "insufficient balance" in normalized or "insufficient_balance" in normalized:
        return "模型服务余额不足，请补充额度后重试"
    if "rate limit" in normalized or "rate_limit" in normalized:
        return "模型服务请求受限，请稍后重试"
    if "invalid api key" in normalized or "authentication failed" in normalized:
        return "模型服务认证失败，请检查服务端配置"
    if any(f"error code: {code}" in normalized for code in (500, 502, 503, 504)):
        return "模型服务暂时不可用，请稍后重试"

    sensitive_markers = (
        "authorization",
        "bearer ",
        "access_token",
        "api_key",
        "x-api-key",
        "password",
        "'token'",
        '"token"',
        "token=",
        "sk-",
        "wallet",
        "wallet_account",
        "account_id",
        "account id",
        "traceback (most recent call last)",
        "input_value=",
        "request_body",
        "response_body",
    )
    if any(marker in normalized for marker in sensitive_markers):
        return "任务执行失败，请查看服务端审计日志"
    if "error code:" in normalized:
        return "外部服务调用失败，请查看服务端审计日志"
    return message[:_PUBLIC_FAILURE_REASON_MAX_LENGTH]

_WEB_V2_REPORT_PREVIEW_AVAILABLE_SQL = """
(
    task.target_platform = 'web'
    AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
    AND task.task_config->>'execution_mode' = 'full_inspection'
    AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
    AND agent_run.id IS NOT NULL
    AND (
        EXISTS (
            SELECT 1
            FROM qc_page_capture AS preview_capture
            WHERE preview_capture.task_id = task.id
              AND preview_capture.agent_run_id = agent_run.id
              AND preview_capture.capture_status = 'captured'
        )
        OR EXISTS (
            SELECT 1
            FROM qc_rule_execution AS preview_execution
            WHERE preview_execution.task_id = task.id
              AND preview_execution.agent_run_id = agent_run.id
        )
        OR EXISTS (
            SELECT 1
            FROM qc_web_coverage_snapshot AS preview_coverage
            WHERE preview_coverage.task_id = task.id
              AND preview_coverage.agent_run_id = agent_run.id
        )
    )
)
"""

_H5_V2_FORMAL_REPORT_AVAILABLE_SQL = """
(
    task.target_platform = 'h5'
    AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
    AND task.task_config->>'execution_mode' = 'full_inspection'
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
          AND payload.report_payload->>'report_id'
              = ('task:' || task.id::text || ':run:' || agent_run.id::text)
          AND payload.report_payload->>'report_stage' = 'final'
          AND payload.report_payload->>'is_final' = 'true'
          AND payload.report_payload->>'report_binding_version'
              = binding.report_version::text
          AND payload.report_payload #>> '{completion_gate,gate_snapshot_id}'
              = gate.gate_snapshot_id
          AND payload.report_payload #>> '{completion_gate,finalization_epoch}'
              = binding.finalization_epoch::text
          AND payload.report_payload #>> '{completion_gate,gate_passed}' = 'true'
    )
)
"""

_H5_V2_REPORT_PREVIEW_AVAILABLE_SQL = f"""
(
    task.target_platform = 'h5'
    AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
    AND task.task_config->>'execution_mode' = 'full_inspection'
    AND agent_run.id IS NOT NULL
    AND task.finalization_agent_run_id = agent_run.id
    AND jsonb_typeof(
        COALESCE(agent_run.output_context, '{{}}'::jsonb)->'final_report'
    ) = 'object'
    AND EXISTS (
        SELECT 1
        FROM qc_rule_execution AS preview_execution
        WHERE preview_execution.task_id = task.id
          AND {completed_rule_execution_sql('preview_execution')}
          AND NOT {superseded_placeholder_sql('preview_execution')}
          AND NOT {outside_current_h5_run_sql('preview_execution')}
    )
)
"""

_REPORT_AVAILABILITY_SQL = f"""
CASE
    WHEN {_WEB_V2_FORMAL_REPORT_AVAILABLE_SQL} THEN 'final'
    WHEN {_H5_V2_FORMAL_REPORT_AVAILABLE_SQL} THEN 'final'
    WHEN {APP_FORMAL_REPORT_AVAILABLE_SQL} THEN 'final'
    WHEN {_WEB_V2_REPORT_PREVIEW_AVAILABLE_SQL} THEN 'preview'
    WHEN {_H5_V2_REPORT_PREVIEW_AVAILABLE_SQL} THEN 'preview'
    WHEN {APP_REPORT_PREVIEW_AVAILABLE_SQL} THEN 'preview'
    ELSE 'none'
END
"""

_FORMAL_REPORT_AVAILABLE_SQL = f"""
({_WEB_V2_FORMAL_REPORT_AVAILABLE_SQL} OR {_H5_V2_FORMAL_REPORT_AVAILABLE_SQL}
 OR {APP_FORMAL_REPORT_AVAILABLE_SQL})
"""

_WEB_V2_TERMINAL_REPORT_STATUS_BY_TASK_STATUS = {
    "canceled": "not_started",
    "review": "failed",
}
_WEB_V2_TERMINAL_REPORT_STATUS_SQL = "\n".join(
    f"        WHEN status = '{task_status}' THEN '{report_status}'"
    for task_status, report_status in _WEB_V2_TERMINAL_REPORT_STATUS_BY_TASK_STATUS.items()
)

_TASK_ROW_REPORT_STATUS_SQL = f"""
CASE
    WHEN target_platform = 'web'
     AND LOWER(COALESCE(workflow_version, '')) = 'v2'
     AND execution_mode = 'full_inspection'
    THEN CASE
        WHEN report_availability IN ('preview', 'final') THEN 'ready'
        WHEN status = 'failed' THEN 'failed'
        WHEN status = 'queued' THEN 'not_started'
{_WEB_V2_TERMINAL_REPORT_STATUS_SQL}
        ELSE 'generating'
    END
    WHEN target_platform = 'h5'
     AND LOWER(COALESCE(workflow_version, '')) = 'v2'
     AND execution_mode = 'full_inspection'
    THEN CASE
        WHEN report_availability IN ('preview', 'final') THEN 'ready'
        WHEN status = 'failed' THEN 'failed'
        WHEN status = 'queued' THEN 'not_started'
        WHEN status = 'review' THEN 'failed'
        WHEN status = 'canceled' THEN 'not_started'
        ELSE 'generating'
    END
    WHEN target_platform = 'app' AND execution_mode IS DISTINCT FROM 'exploration_only'
    THEN CASE
        WHEN report_availability IN ('preview', 'final') THEN 'ready'
        WHEN status IN ('failed', 'review', 'done') THEN 'failed'
        WHEN status IN ('queued', 'canceled') THEN 'not_started'
        ELSE 'generating'
    END
    WHEN execution_mode = 'exploration_only'
     AND status IN ('done', 'review')
    THEN 'not_applicable'
    WHEN status = 'queued' THEN 'not_started'
    WHEN status = 'running' THEN 'generating'
    WHEN status = 'failed' THEN 'failed'
    WHEN status IN ('done', 'review') AND total_rules > 0 THEN 'ready'
    ELSE 'not_started'
END
"""


class PostgresWorkstationQueryRepository:
    """基于 PostgreSQL 的工作台页面读模型仓储。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = _normalize_asyncpg_url(database_url) if database_url else None
        self._pool = pool

    async def list_tasks(
        self,
        filters: WorkstationTaskListFilter,
        *,
        access_scope: AccessScope,
    ) -> WorkstationTaskListResult:
        pool = await self._get_pool()
        offset = (filters.page - 1) * filters.page_size
        has_report_filter = filters.report_status is not None
        arguments = [
            filters.task_name,
            filters.product_name,
            filters.product_code,
            filters.target_url,
            filters.status,
            filters.created_from,
            filters.created_to,
        ]
        if has_report_filter:
            arguments.append(filters.report_status)
        arguments.extend([
            filters.page_size,
            offset,
            access_scope.can_read_any,
            access_scope.tenant_id,
            access_scope.can_read_tenant,
            access_scope.actor_user_id,
            filters.department_name,
            filters.target_platform,
            access_scope.department_scope,
            filters.submitter_name,
        ])
        async with pool.acquire() as connection, connection.transaction(readonly=True):
            # LOCAL 设置随事务结束恢复，不能影响连接池中的其他读写请求。
            await connection.execute("SET LOCAL jit = off")
            rows = await connection.fetch(
                task_list_query_sql(
                    report_status_filter=has_report_filter,
                    report_availability_sql=_REPORT_AVAILABILITY_SQL,
                    report_status_sql=_TASK_ROW_REPORT_STATUS_SQL,
                ),
                *arguments,
            )
        total = int(rows[0]["total_count"]) if rows else 0
        promoted = await self._mark_review_when_rules_idle(rows)
        return WorkstationTaskListResult(
            items=[
                _task_list_item_from_row(
                    row,
                    can_retry_quarantine=access_scope.can_write_any,
                    can_delete_tasks=access_scope.can_write_any,
                    status_override=(
                        "review" if str(row["task_id"]) in promoted else None
                    ),
                )
                for row in rows
            ],
            total=total,
            page=filters.page,
            page_size=filters.page_size,
        )

    async def get_task_detail(
        self,
        task_id: str,
        *,
        access_scope: AccessScope,
        for_write: bool = False,
    ) -> WorkstationTaskDetail | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT
                    task.id::text AS task_id,
                    COALESCE(task.task_config->>'task_name', task.target_url) AS task_name,
                    task.task_config->'product' AS product,
                    task.task_config->'journey_steps' AS journey_steps,
                    CASE WHEN jsonb_typeof(task.task_config->'ruleset_rule_total') = 'number'
                        THEN (task.task_config->>'ruleset_rule_total')::int
                        ELSE 0 END AS ruleset_rule_total,
                    task.task_config->>'scan_mode' AS scan_mode,
                    task.task_config->>'remark' AS remark,
                    COALESCE(
                        task.task_config#>'{{web_exploration_target,required_page_types}}',
                        task.task_config#>'{{h5_exploration_target,required_page_types}}'
                    ) AS required_page_types,
                    task.task_config#>'{{inspection_scope,page_types}}'
                        AS inspection_scope_page_types,
                    task.task_config#>'{{web_exploration_target,optional_page_types}}'
                        AS optional_page_types,
                    task.task_config#>'{{web_exploration_target,not_applicable_page_types}}'
                        AS not_applicable_page_types,
                    task.task_config#>>'{{web_exploration_target,applicability_mode}}'
                        AS applicability_mode,
                    task.task_config#>>'{{web_exploration_target,catalog_version}}'
                        AS applicability_catalog_version,
                    task.task_config#>>'{{web_exploration_target,catalog_hash}}'
                        AS applicability_catalog_hash,
                    COALESCE(
                        task.task_config#>>'{{browser_profile_snapshot,profile_id}}',
                        task.task_config#>>'{{_task_target,web,browser_profile_id}}'
                    ) AS browser_profile_id,
                    task.task_config#>'{{_task_target,web,adult_self_attestation}}'
                        AS adult_self_attestation,
                    NULLIF(
                        task.task_config#>>'{{browser_profile_snapshot,viewport_width}}',
                        ''
                    )::int AS viewport_width,
                    NULLIF(
                        task.task_config#>>'{{browser_profile_snapshot,viewport_height}}',
                        ''
                    )::int AS viewport_height,
                    task.target_url,
                    task.target_platform,
                    task.task_config->>'execution_mode' AS execution_mode,
                    task.task_config->>'workflow_version' AS workflow_version,
                    task.task_config->>'web_inspection_notification_mode'
                        AS web_inspection_notification_mode,
                    task.finalization_recovery_state,
                    task.finalization_state,
                    task.execution_status,
                    COALESCE((
                        SELECT gate.gate_passed
                        FROM qc_completion_gate_snapshot AS gate
                        WHERE gate.task_id = task.id
                          AND gate.gate_snapshot_id = task.completion_gate_snapshot_id
                          AND gate.finalization_epoch = task.finalization_epoch
                        LIMIT 1
                    ), FALSE) AS completion_gate_passed,
                    {_FORMAL_REPORT_AVAILABLE_SQL} AS report_available,
                    {_REPORT_AVAILABILITY_SQL} AS report_availability,
                    (
                        SELECT jsonb_build_object(
                            'coverage_status', snapshot.coverage_status,
                            'observed_page_types', snapshot.observed_page_types,
                            'missing_page_types', snapshot.missing_page_types,
                            'unrecognized_page_types', snapshot.unrecognized_page_types,
                            'normalization_version', snapshot.normalization_version
                        )
                        FROM qc_web_coverage_snapshot AS snapshot
                        WHERE snapshot.task_id = task.id
                          AND snapshot.agent_run_id = agent_run.id
                          AND snapshot.finalization_epoch = task.finalization_epoch
                        LIMIT 1
                    ) AS web_coverage,
                    CASE
                        WHEN task.status = 'running'
                         AND coalesce(
                             agent_run.output_context->>'cancel_requested',
                             'false'
                         ) = 'true'
                        THEN 'canceling'
                        ELSE task.status
                    END AS status,
                    task.created_at,
                    task.started_at,
                    task.finished_at,
                    task.loop_guard AS progress_task_loop_guard,
                    agent_run.loop_guard AS progress_run_loop_guard,
                    COALESCE(
                        NULLIF(task.last_error, ''),
                        agent_run.error_message
                    ) AS last_error,
                    ruleset.ruleset_code,
                    ruleset.version AS ruleset_version,
                    task.channel,
                    task.priority,
                    agent_run.id::text AS run_id,
                    agent_run.status AS run_status,
                    rule_settlement.rules_settled,
                    rule_settlement.rules_settled_at,
                    submitter.full_name AS submitter_name,
                    execution_stats.total_rules,
                    execution_stats.finished_rules
                FROM qc_task task
                JOIN qc_ruleset ruleset ON ruleset.id = task.ruleset_id
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
                {rules_settlement_lateral_sql(
                    id_expr='task.id',
                    epoch_expr='task.finalization_epoch',
                )}
                -- 先聚合规则计数，避免将大体积运行上下文复制到每条规则后分组排序。
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(execution.id)::int AS total_rules,
                        COUNT(execution.id) FILTER (
                            WHERE {completed_rule_execution_sql('execution')}
                        )::int AS finished_rules
                    FROM qc_rule_execution AS execution
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
                ) execution_stats ON TRUE
                LEFT JOIN qc_user submitter ON submitter.id = task.submitter_id
                WHERE task.id = $1::uuid
                  AND task.deleted_at IS NULL
                  AND (
                      $2::boolean
                      OR (
                          task.tenant_id = $3::uuid
                          AND (
                              $4::boolean
                              OR task.submitter_id = $5::uuid
                              OR task.department_name = $6::varchar
                          )
                      )
                  )
                """,
                task_id,
                access_scope.can_write_any if for_write else access_scope.can_read_any,
                access_scope.tenant_id,
                False if for_write else access_scope.can_read_tenant,
                access_scope.actor_user_id,
                None if for_write else access_scope.department_scope,
            )
        if row is None:
            return None
        promoted = await self._mark_review_when_rules_idle([row])
        item = _task_list_item_from_row(
            row,
            can_retry_quarantine=access_scope.can_write_any,
            can_delete_tasks=access_scope.can_write_any,
            status_override="review" if str(row["task_id"]) in promoted else None,
        )
        adult_self_attestation_payload = _json_dict(row["adult_self_attestation"])
        web_coverage = _json_dict(row["web_coverage"])
        report_available = bool(row["report_available"])
        report_availability = _optional_text(_row_value(row, "report_availability")) or (
            "final" if report_available else "none"
        )
        run_status = _optional_text(row["run_status"])
        execution_phase = web_execution_phase(
            task_status=item.status,
            run_status=run_status,
            failure_reason=item.failure_reason,
            coverage_unrecognized_page_types=tuple(
                _string_list(web_coverage.get("unrecognized_page_types"))
            ),
        )
        detail = WorkstationTaskDetail(
            submitter_name=item.submitter_name,
            task_id=item.task_id,
            task_name=item.task_name,
            product=item.product,
            target_url=item.target_url,
            target_platform=item.target_platform,
            execution_mode=item.execution_mode,
            journey_steps=item.journey_steps,
            status=item.status,
            progress=item.progress,
            report_status=item.report_status,
            failure_reason=item.failure_reason,
            available_actions=item.available_actions,
            created_at=item.created_at,
            tenant_name=item.tenant_name,
            execution_started_at=item.execution_started_at,
            execution_finished_at=item.execution_finished_at,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            ruleset_code=row["ruleset_code"],
            ruleset_version=row["ruleset_version"],
            channel=row["channel"],
            priority=row["priority"],
            run_id=row["run_id"],
            workflow_version=row["workflow_version"] or "v1",
            scan_mode=row["scan_mode"] or "full",
            remark=row["remark"],
            required_page_types=tuple(_json_list(row["required_page_types"])),
            inspection_scope_page_types=tuple(
                _json_list(row.get("inspection_scope_page_types"))
            ),
            optional_page_types=tuple(_json_list(row["optional_page_types"])),
            not_applicable_page_types=tuple(_json_list(row["not_applicable_page_types"])),
            applicability_mode=_optional_text(row["applicability_mode"]),
            applicability_catalog_version=_optional_text(row["applicability_catalog_version"]),
            applicability_catalog_hash=_optional_text(row["applicability_catalog_hash"]),
            browser_profile_id=row["browser_profile_id"],
            adult_self_attestation=(
                AdultSelfAttestation.from_dict(adult_self_attestation_payload)
                if adult_self_attestation_payload
                else None
            ),
            viewport_width=row["viewport_width"],
            viewport_height=row["viewport_height"],
            execution_status=row["execution_status"],
            finalization_state=row["finalization_state"],
            completion_gate_passed=bool(row["completion_gate_passed"]),
            report_available=report_available,
            report_availability=report_availability,
            coverage_status=_optional_text(web_coverage.get("coverage_status")),
            coverage_observed_page_types=tuple(
                _string_list(web_coverage.get("observed_page_types"))
            ),
            coverage_missing_page_types=tuple(_string_list(web_coverage.get("missing_page_types"))),
            coverage_unrecognized_page_types=tuple(
                _string_list(web_coverage.get("unrecognized_page_types"))
            ),
            coverage_normalization_protocol_version=_optional_text(
                web_coverage.get("normalization_version")
            ),
            run_status=run_status,
            execution_phase=execution_phase,
            tasks_ahead=None,
            queue_position=None,
            available_worker_count=None,
            start_condition=(
                "等待 Worker 领取当前 Run；排队状态暂未确认"
                if execution_phase in {"queued", "pending"}
                else None
            ),
        )
        if (
            detail.target_platform is TaskTargetPlatform.WEB
            and detail.execution_mode is TaskExecutionMode.FULL_INSPECTION
            and detail.workflow_version.casefold() == "v2"
        ):
            detail = replace(
                detail,
                stage_durations=await self.get_web_stage_durations(
                    task_id,
                    agent_run_id=detail.run_id,
                    access_scope=access_scope,
                ),
            )
        return detail

    async def get_web_stage_durations(
        self,
        task_id: str,
        *,
        agent_run_id: str | None,
        access_scope: AccessScope,
    ) -> WebStageDurations | None:
        """从持久化边界事实计算 Web V2 正式链路阶段耗时。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    task.created_at AS task_created_at,
                    agent_run.started_at AS exploration_started_at,
                    agent_run.finished_at AS exploration_finished_at,
                    inspection.started_at AS inspection_started_at,
                    inspection.finished_at AS inspection_finished_at,
                    report_generation.started_at AS report_generation_started_at,
                    report_generation.finished_at AS report_generation_finished_at,
                    binding.bound_at AS report_finished_at
                FROM qc_task AS task
                LEFT JOIN LATERAL (
                    SELECT run.id, run.started_at, run.finished_at
                    FROM qc_agent_run AS run
                    WHERE run.task_id = task.id
                      AND ($2::uuid IS NULL OR run.id = $2::uuid)
                    ORDER BY
                        CASE
                            WHEN run.id = task.finalization_agent_run_id THEN 0
                            WHEN run.agent_type = 'main' THEN 1
                            ELSE 2
                        END,
                        run.created_at DESC
                    LIMIT 1
                ) AS agent_run ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        MIN(candidate.started_at) AS started_at,
                        CASE
                            WHEN BOOL_AND(candidate.is_terminal)
                             AND NOT EXISTS (
                                SELECT 1
                                FROM qc_evidence_request AS request
                                INNER JOIN qc_inspection_work_item AS request_work
                                  ON request_work.id = request.work_item_id
                                WHERE request.task_id = task.id
                                  AND request.finalization_epoch
                                      = task.finalization_epoch
                                  AND request.session_dependency = 'active_session'
                                  AND request.status IN ('pending', 'routed', 'running')
                                  AND request_work.producer_agent_run_id
                                      = agent_run.id
                                  AND request_work.finalization_epoch
                                      = task.finalization_epoch
                                  AND request_work.control_mode = 'enforced'
                             )
                            THEN MAX(candidate.finished_at)
                            ELSE NULL
                        END AS finished_at
                    FROM (
                        SELECT
                            publication.created_at AS started_at,
                            publication.finished_at AS finished_at,
                            publication.status IN (
                                'published', 'not_applicable', 'needs_review',
                                'failed', 'cancelled', 'superseded'
                            ) AND publication.finished_at IS NOT NULL AS is_terminal
                        FROM qc_h5_inspection_publication AS publication
                        WHERE publication.task_id = task.id
                          AND publication.source_agent_run_id = agent_run.id
                          AND publication.finalization_epoch = task.finalization_epoch
                          AND publication.mode = 'enforced'
                        UNION ALL
                        SELECT
                            COALESCE(work.started_at, work.created_at),
                            work.finished_at,
                            work.status IN (
                                'succeeded', 'partial', 'failed',
                                'cancelled', 'superseded'
                            ) AND work.finished_at IS NOT NULL
                        FROM qc_inspection_work_item AS work
                        WHERE work.task_id = task.id
                          AND work.producer_agent_run_id = agent_run.id
                          AND work.finalization_epoch = task.finalization_epoch
                          AND work.control_mode = 'enforced'
                        UNION ALL
                        SELECT
                            COALESCE(execution.started_at, execution.created_at),
                            execution.finished_at,
                            execution.finished_at IS NOT NULL
                        FROM qc_rule_execution AS execution
                        LEFT JOIN qc_inspection_work_item_rule AS member
                          ON member.rule_execution_id = execution.id
                        LEFT JOIN qc_inspection_work_item AS execution_work
                          ON execution_work.id = member.work_item_id
                        LEFT JOIN qc_aggregate_inspection_work_item AS execution_aggregate
                          ON execution_aggregate.id = execution.aggregate_work_item_id
                        WHERE execution.task_id = task.id
                          AND (
                              (
                                  execution.agent_run_id = agent_run.id
                                  AND member.rule_execution_id IS NULL
                                  AND execution.aggregate_work_item_id IS NULL
                              )
                              OR (
                                  execution_work.producer_agent_run_id = agent_run.id
                                  AND execution_work.finalization_epoch
                                      = task.finalization_epoch
                                  AND execution_work.control_mode = 'enforced'
                              )
                              OR (
                                  execution_aggregate.producer_agent_run_id = agent_run.id
                                  AND execution_aggregate.finalization_epoch
                                      = task.finalization_epoch
                                  AND execution_aggregate.control_mode = 'enforced'
                              )
                          )
                        UNION ALL
                        SELECT
                            aggregate_candidate.created_at,
                            aggregate_candidate.finished_at,
                            aggregate_candidate.status IN (
                                'completed', 'not_applicable',
                                'needs_review', 'failed', 'evidence_insufficient'
                            ) AND aggregate_candidate.finished_at IS NOT NULL
                        FROM qc_aggregate_inspection_candidate AS aggregate_candidate
                        WHERE aggregate_candidate.task_id = task.id
                          AND aggregate_candidate.source_agent_run_id = agent_run.id
                          AND aggregate_candidate.finalization_epoch
                              = task.finalization_epoch
                        UNION ALL
                        SELECT
                            COALESCE(aggregate_work.started_at,
                                     aggregate_work.created_at),
                            aggregate_work.finished_at,
                            aggregate_work.status IN (
                                'succeeded', 'evidence_insufficient', 'failed',
                                'cancelled', 'superseded'
                            ) AND aggregate_work.finished_at IS NOT NULL
                        FROM qc_aggregate_inspection_work_item AS aggregate_work
                        WHERE aggregate_work.task_id = task.id
                          AND aggregate_work.producer_agent_run_id = agent_run.id
                          AND aggregate_work.finalization_epoch = task.finalization_epoch
                          AND aggregate_work.control_mode = 'enforced'
                    ) AS candidate
                ) AS inspection ON TRUE
                LEFT JOIN LATERAL (
                    SELECT report_binding.bound_at
                    FROM qc_report_gate_binding AS report_binding
                    INNER JOIN qc_report_payload_snapshot AS payload
                      ON payload.report_id = report_binding.report_id
                     AND payload.report_version = report_binding.report_version
                     AND payload.report_payload_hash = report_binding.report_payload_hash
                    WHERE report_binding.task_id = task.id
                      AND report_binding.finalization_epoch = task.finalization_epoch
                      AND payload.report_payload->>'agent_run_id' = agent_run.id::text
                    ORDER BY report_binding.bound_at DESC
                    LIMIT 1
                ) AS binding ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        MIN(attempt.started_at) AS started_at,
                        CASE
                            WHEN binding.bound_at IS NOT NULL
                             AND count(*) FILTER (
                                WHERE attempt.status = 'running'
                             ) = 0
                            THEN MAX(attempt.finished_at) FILTER (
                                WHERE attempt.status = 'succeeded'
                            )
                            ELSE NULL
                        END AS finished_at
                    FROM qc_web_report_generation_attempt AS attempt
                    WHERE attempt.task_id = task.id
                      AND attempt.agent_run_id = agent_run.id
                      AND attempt.finalization_epoch = task.finalization_epoch
                ) AS report_generation ON TRUE
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'web'
                  AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND agent_run.id IS NOT NULL
                  AND (
                      $3::boolean
                      OR (
                          task.tenant_id = $4::uuid
                          AND (
                              $5::boolean
                              OR task.submitter_id = $6::uuid
                              OR task.department_name = $7::varchar
                          )
                      )
                  )
                """,
                task_id,
                agent_run_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
        if row is None:
            return None
        return _web_stage_durations_from_row(row)

    async def update_task_status(
        self,
        *,
        task_id: str,
        status: str,
        allowed_statuses: list[str],
        access_scope: AccessScope,
    ) -> WorkstationTaskOperationResult | None:
        target_status = TaskStatus(status)
        allowed = tuple(TaskStatus(item) for item in allowed_statuses)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            has_write_access = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM qc_task AS task
                    WHERE task.id = $1::uuid
                      AND task.deleted_at IS NULL
                      AND (
                          $2::boolean
                          OR (
                              task.tenant_id = $3::uuid
                              AND task.submitter_id = $4::uuid
                          )
                      )
                )
                """,
                task_id,
                access_scope.can_write_any,
                access_scope.tenant_id,
                access_scope.actor_user_id,
            )
            if not has_write_access:
                return None
            snapshot = await transition_current_task_status_in_transaction(
                connection,
                task_id=task_id,
                target_status=target_status,
                allowed_source_statuses=allowed,
                occurred_at=datetime.now(UTC),
            )
            if snapshot is None:
                return None
            run_id = await connection.fetchval(
                """
                SELECT agent_run.id::text
                FROM qc_task AS task
                LEFT JOIN LATERAL (
                    SELECT id
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
                ) AS agent_run ON TRUE
                WHERE task.id = $1::uuid
                """,
                task_id,
            )
        return WorkstationTaskOperationResult(
            task_id=snapshot.task_id,
            status=snapshot.status.value,
            run_id=str(run_id) if run_id is not None else None,
            message="任务操作已提交",
        )

    async def retry_quarantined_finalization(
        self,
        *,
        task_id: str,
        access_scope: AccessScope,
    ) -> WorkstationTaskOperationResult | None:
        """管理员通过既有任务动作显式恢复已隔离的收口。"""

        if not access_scope.can_write_any:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_task
                SET finalization_recovery_state = 'active',
                    finalization_error_signature = NULL,
                    finalization_same_error_count = 0,
                    finalization_retry_at = now(),
                    finalization_recovery_generation =
                        finalization_recovery_generation + 1,
                    finalization_recovery_lease_token = NULL,
                    finalization_recovery_lease_owner = NULL,
                    finalization_recovery_lease_expires_at = NULL,
                    finalization_recovered_at = now(),
                    updated_at = now()
                WHERE id = $1::uuid
                  AND deleted_at IS NULL
                  AND $2::boolean
                  AND finalization_recovery_state = 'quarantined'
                  AND target_platform = 'web'
                  AND LOWER(COALESCE(task_config->>'workflow_version', '')) = 'v2'
                  AND task_config->>'execution_mode' = 'full_inspection'
                  AND task_config->>'web_inspection_notification_mode' = 'enforced'
                RETURNING
                    id::text AS task_id,
                    status,
                    finalization_agent_run_id::text AS run_id
                """,
                task_id,
                access_scope.can_write_any,
            )
        if row is None:
            return None
        return WorkstationTaskOperationResult(
            task_id=str(row["task_id"]),
            status=str(row["status"]),
            run_id=_optional_text(row.get("run_id")),
            message="正式报告收口重试已恢复",
        )

    async def _mark_review_when_rules_idle(self, rows: list[Any]) -> set[str]:
        """执行中 100% 且规则不再更新时，把任务状态写成待复核。"""

        candidates = [
            str(row["task_id"])
            for row in rows
            if running_at_full_progress_should_enter_review(
                status=str(row["status"]),
                rules_settled=bool(_row_value(row, "rules_settled")),
            )
        ]
        if not candidates:
            return set()
        promoted: set[str] = set()
        pool = await self._get_pool()
        now = datetime.now(UTC)
        async with pool.acquire() as connection, connection.transaction():
            for task_id in candidates:
                try:
                    snapshot = await transition_current_task_status_in_transaction(
                        connection,
                        task_id=task_id,
                        target_status=TaskStatus.REVIEW,
                        allowed_source_statuses=(TaskStatus.RUNNING,),
                        occurred_at=now,
                    )
                except (TaskStatusConflictError, TaskStatusValidationError):
                    snapshot = None
                if snapshot is not None and snapshot.status is TaskStatus.REVIEW:
                    promoted.add(task_id)
        return promoted

    async def close(self) -> None:
        # 连接池由进程级 PoolManager 统一持有，仓储只释放自身引用。
        self._pool = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool


def _task_list_item_from_row(
    row: Any,
    *,
    can_retry_quarantine: bool = False,
    can_delete_tasks: bool = False,
    status_override: str | None = None,
) -> WorkstationTaskListItem:
    status = status_override or row["status"]
    task_loop_guard = _json_dict(_row_value(row, "progress_task_loop_guard"))
    run_loop_guard = _json_dict(_row_value(row, "progress_run_loop_guard"))
    # Run 的两种时长字段属于同一预算，不能和任务级别名混合后改变优先级。
    progress_loop_guard = (
        run_loop_guard
        if any(key in run_loop_guard for key in ("max_duration_seconds", "timeout_minutes"))
        else task_loop_guard
    )
    total_rules = max(
        int(row["total_rules"] or 0),
        int(_row_value(row, "ruleset_rule_total") or 0),
    )
    execution_mode = _execution_mode_from_row(row)
    target_platform = TaskTargetPlatform(str(row["target_platform"]))
    report_status = report_status_from_task_status(
        status, total_rules, execution_mode=execution_mode
    )
    report_available = _row_value(row, "report_available") is True
    report_availability = _optional_text(_row_value(row, "report_availability")) or (
        "final" if report_available else "none"
    )
    available_actions = available_actions_for_status(status, execution_mode=execution_mode)
    if can_delete_tasks and status in DELETABLE_TASK_STATUSES:
        available_actions.append("delete")
    is_web_v2_formal = (
        target_platform is TaskTargetPlatform.WEB
        and execution_mode is TaskExecutionMode.FULL_INSPECTION
        and str(_row_value(row, "workflow_version") or "").lower() == "v2"
    )
    is_h5_v2_formal = (
        target_platform is TaskTargetPlatform.H5
        and execution_mode is TaskExecutionMode.FULL_INSPECTION
        and str(_row_value(row, "workflow_version") or "").lower() == "v2"
    )
    is_app_full_inspection = (
        target_platform is TaskTargetPlatform.APP
        and execution_mode is TaskExecutionMode.FULL_INSPECTION
    )
    if is_web_v2_formal or is_h5_v2_formal or is_app_full_inspection:
        terminal_report_status = _WEB_V2_TERMINAL_REPORT_STATUS_BY_TASK_STATUS.get(status)
        if report_availability in {"preview", "final"}:
            report_status = "ready"
        elif status == "failed" or (is_app_full_inspection and status == "done"):
            report_status = "failed"
        elif status == "queued":
            report_status = "not_started"
        elif terminal_report_status is not None:
            report_status = terminal_report_status
        else:
            report_status = "generating"
        available_actions = [action for action in available_actions if action != "report"]
        if report_availability in {"preview", "final"}:
            available_actions.append("report")
    if (
        can_retry_quarantine
        and is_web_v2_formal
        and _row_value(row, "web_inspection_notification_mode") == "enforced"
        and _row_value(row, "finalization_recovery_state") == "quarantined"
    ):
        available_actions.append("retry-finalization")
    return WorkstationTaskListItem(
        task_id=row["task_id"],
        task_name=row["task_name"],
        product=_product_from_json(row["product"]),
        target_url=row["target_url"],
        target_platform=target_platform,
        execution_mode=execution_mode,
        journey_steps=_json_list(row["journey_steps"]),
        status=status,
        progress=whole_task_progress_percent(
            status,
            started_at=_row_value(row, "started_at"),
            loop_guard=progress_loop_guard,
            run_status=_row_value(row, "run_status"),
            rules_settled=bool(_row_value(row, "rules_settled")),
        ),
        report_status=report_status,
        report_availability=report_availability,
        failure_reason=_public_failure_reason(row["last_error"]),
        available_actions=available_actions,
        created_at=row["created_at"],
        tenant_name=_optional_text(_row_value(row, "tenant_name")),
        submitter_name=_optional_text(_row_value(row, "submitter_name")),
        execution_started_at=_row_value(row, "started_at"),
        execution_finished_at=_execution_finished_at(row),
    )


def _execution_finished_at(row: Any) -> datetime | None:
    """规则补完后停表；任务若更早结束，不把时长延到补完时刻之后。"""

    finished_at = _row_value(row, "finished_at")
    if not isinstance(finished_at, datetime):
        finished_at = None
    settled_at = (
        _row_value(row, "rules_settled_at")
        if bool(_row_value(row, "rules_settled"))
        else None
    )
    if not isinstance(settled_at, datetime):
        settled_at = None
    if finished_at is not None and settled_at is not None:
        return min(finished_at, settled_at)
    return finished_at or settled_at


def _execution_mode_from_row(row: Any) -> TaskExecutionMode:
    try:
        value = row["execution_mode"]
    except (KeyError, TypeError):
        value = None
    if value is None:
        return TaskExecutionMode.FULL_INSPECTION
    try:
        return TaskExecutionMode(str(value))
    except ValueError as exc:
        raise ValueError(f"execution_mode 不受支持: {value}") from exc


def _row_value(row: Any, key: str) -> Any | None:
    try:
        return row[key]
    except (KeyError, TypeError):
        return None


def _product_from_json(value: Any) -> ProductSnapshot:
    data = _json_dict(value)
    return ProductSnapshot(
        department_name=str(data.get("department_name") or "-"),
        product_code=str(data.get("product_code") or "-"),
        product_name=str(data.get("product_name") or "-"),
    )


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = _json_loads(value, default={})
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[str]:
    parsed = _json_loads(value, default=[])
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _web_stage_durations_from_row(row: Any) -> WebStageDurations:
    exploration_started_at = _row_value(row, "exploration_started_at")
    exploration_finished_at = _row_value(row, "exploration_finished_at")
    inspection_started_at = _row_value(row, "inspection_started_at")
    inspection_finished_at = _row_value(row, "inspection_finished_at")
    report_generation_started_at = _row_value(row, "report_generation_started_at")
    report_generation_finished_at = _row_value(row, "report_generation_finished_at")
    report_finished_at = _row_value(row, "report_finished_at")
    return WebStageDurations(
        exploration_ms=_elapsed_milliseconds(
            exploration_started_at,
            exploration_finished_at,
        ),
        inspection_ms=_elapsed_milliseconds(
            inspection_started_at,
            inspection_finished_at,
        ),
        report_generation_ms=_elapsed_milliseconds(
            report_generation_started_at,
            report_generation_finished_at,
        ),
        total_ms=_elapsed_milliseconds(
            _row_value(row, "task_created_at"),
            report_finished_at,
        ),
    )


def _elapsed_milliseconds(start: Any, end: Any) -> int | None:
    """阶段边界未齐全时返回空，异常时钟顺序统一钳制为零。"""

    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _json_loads(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _normalize_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url
