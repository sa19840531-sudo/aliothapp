"""PostgreSQL 质检任务仓储实现。"""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from services.api.app.application.observation_packet import (
    build_app_observation_location,
)
from services.api.app.application.report_payloads import stable_report_payload_hash
from services.api.app.application.rule_auth_gate import (
    AUTH_GATE_AUTO,
    normalize_auth_gate,
)
from services.api.app.application.rule_execution import (
    CandidatePageReference,
    RecordedRuleExecution,
    RecordRuleExecutionCommand,
    RuleExecutionDefinition,
    RuleExecutionFinalizationResult,
    RuleExecutionLeaseConflictError,
    RuleExecutionValidationError,
    RuntimePageInspectionFailureFallback,
    RuntimeRuleExecutionFallback,
    derive_quality_outcome,
)
from services.api.app.application.rule_judge import RuleJudgeDefinition
from services.api.app.application.task_query import (
    AgentRunDetail,
    AgentRunStep,
    CandidateRule,
    EvidenceArtifactSummary,
    RuleExecutionCompleteness,
    RuleExecutionSummary,
    TaskDetail,
    TaskListItem,
    TaskTargetPageCoverage,
    UnresolvedCandidateRule,
)
from services.api.app.application.task_rule_inventory import (
    inventory_candidate_rules,
)
from services.api.app.application.task_submission import (
    CreatedTask,
    CreateTaskSubmissionCommand,
    PersistedTaskSubmission,
    TaskRulesetNotFoundError,
    TaskSubmissionIdempotencyConflictError,
    build_task_message,
)
from services.api.app.application.web_final_report_binding import (
    WebFinalReportBindingError,
    WebFinalReportBindingExpectation,
    is_database_web_v2_formal,
    validate_web_final_report_binding,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.rule.url_scope import candidate_rule_matches_page_url
from services.api.app.domain.task import build_task_submission_committed
from services.api.app.domain.task.inspection_scope import inspection_scope_page_types
from services.api.app.domain.task.targets import TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.db.execution_ruleset_repository import (
    resolve_execution_ruleset_on_connection,
)
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.inspection_scope_sql import (
    page_type_in_inspection_scope_sql,
    rule_in_inspection_scope_sql,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    rule_replacement_ctes_sql,
    superseded_placeholder_sql,
)
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    freeze_task_rule_inventory_on_connection,
    read_frozen_task_rule_inventory_on_connection,
)
from services.api.app.infrastructure.db.task_rule_inventory_sql import rule_execution_target_sql
from services.api.app.infrastructure.db.web_coverage_snapshot_row import (
    InvalidWebCoverageSnapshotRowError,
    parse_web_coverage_snapshot_row,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord
from services.api.app.infrastructure.queue.state_store import agent_run_input_context


def _optional_read_scope_sql(
    access_scope: AccessScope | None,
    *,
    task_alias: str,
    first_param: int,
) -> tuple[str, tuple[object, ...]]:
    """为 HTTP 读取追加 owner/tenant 条件；Worker 内部调用保持可信任务链读取。"""

    if access_scope is None:
        return "", ()
    return (
        f"""
          AND (
              ${first_param}::boolean
              OR (
                  {task_alias}.tenant_id = ${first_param + 1}::uuid
                  AND (
                      ${first_param + 2}::boolean
                      OR {task_alias}.submitter_id = ${first_param + 3}::uuid
                      OR {task_alias}.department_name = ${first_param + 4}::varchar
                  )
              )
          )
        """,
        (
            access_scope.can_read_any,
            access_scope.tenant_id,
            access_scope.can_read_tenant,
            access_scope.actor_user_id,
            access_scope.department_scope,
        ),
    )


async def _can_read_task(
    connection: Any,
    *,
    task_id: str,
    access_scope: AccessScope | None,
) -> bool:
    """在聚合查询前用 task + tenant + owner 复合条件复核用户主体。"""

    if access_scope is None:
        return True
    value = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM qc_task AS task
            WHERE task.id = $1::uuid
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
        )
        """,
        task_id,
        access_scope.can_read_any,
        access_scope.tenant_id,
        access_scope.can_read_tenant,
        access_scope.actor_user_id,
        access_scope.department_scope,
    )
    return bool(value)


class PostgresTaskRepository:
    """基于 PostgreSQL 的 `qc_task` 仓储。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def resolve_execution_ruleset(
        self,
        ruleset_codes: Sequence[str] | None,
    ) -> tuple[str, int]:
        """任务要跑的规则集落成一个执行规则集：一个用本身，多个（或全部）找或建组合规则集。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            return await resolve_execution_ruleset_on_connection(connection, ruleset_codes)

    async def create_task(
        self,
        *,
        access_scope: AccessScope,
        target: TaskTarget,
        ruleset_code: str,
        ruleset_version: int,
        channel: str,
        priority: int,
        task_config: dict,
        page_sampling_policy: dict,
        loop_guard: dict,
    ) -> CreatedTask:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await _insert_task_on_connection(
                connection,
                access_scope=access_scope,
                target=target,
                ruleset_code=ruleset_code,
                ruleset_version=ruleset_version,
                channel=channel,
                priority=priority,
                task_config=task_config,
                page_sampling_policy=page_sampling_policy,
                loop_guard=loop_guard,
            )
        return _created_task_from_row(
            row,
            target=target,
            ruleset_code=ruleset_code,
            ruleset_version=ruleset_version,
        )

    async def create_submission(
        self,
        command: CreateTaskSubmissionCommand,
    ) -> PersistedTaskSubmission:
        """原子提交 Task、初始 Run、Submission Fact 与 Outbox。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            lock_identity = (
                f"{command.access_scope.tenant_id}:{command.access_scope.actor_user_id}:"
                f"{command.submission_kind}:{command.idempotency_key}"
            )
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-submission:{lock_identity}",
            )
            existing = await connection.fetchrow(
                """
                SELECT
                    submission.id::text AS submission_id,
                    submission.task_id::text AS task_id,
                    submission.initial_agent_run_id::text AS run_id,
                    submission.request_hash,
                    task.ruleset_id::text AS ruleset_id,
                    task.target_platform,
                    task.target_url,
                    task.status,
                    task.task_config,
                    task.page_sampling_policy,
                    task.loop_guard
                FROM qc_task_submission AS submission
                INNER JOIN qc_task AS task ON task.id = submission.task_id
                WHERE submission.tenant_id = $1::uuid
                  AND submission.submitter_id = $2::uuid
                  AND submission.submission_kind = $3
                  AND submission.idempotency_key = $4
                FOR UPDATE OF submission
                """,
                command.access_scope.tenant_id,
                command.access_scope.actor_user_id,
                command.submission_kind,
                command.idempotency_key,
            )
            if existing is not None:
                if str(existing["request_hash"]) != command.request_hash:
                    raise TaskSubmissionIdempotencyConflictError(
                        "同一 Idempotency-Key 已绑定不同任务请求"
                    )
                return PersistedTaskSubmission(
                    task=_created_task_from_row(
                        existing,
                        target=command.target,
                        ruleset_code=command.ruleset_code,
                        ruleset_version=command.ruleset_version,
                    ),
                    run_id=str(existing["run_id"]),
                    replayed=True,
                )

            row = await _insert_task_on_connection(
                connection,
                access_scope=command.access_scope,
                target=command.target,
                ruleset_code=command.ruleset_code,
                ruleset_version=command.ruleset_version,
                channel=command.channel,
                priority=command.priority,
                task_config=command.task_config,
                page_sampling_policy=command.page_sampling_policy,
                loop_guard=command.loop_guard,
            )
            task = _created_task_from_row(
                row,
                target=command.target,
                ruleset_code=command.ruleset_code,
                ruleset_version=command.ruleset_version,
            )
            now = datetime.now(UTC)
            run_id = str(uuid4())
            record = AgentRunRecord(
                run_id=run_id,
                task_id=task.task_id,
                submitted_by_user_id=command.access_scope.actor_user_id,
                tenant_id=command.access_scope.tenant_id,
                message=build_task_message(
                    task,
                    command.ruleset_code,
                    command.ruleset_version,
                ),
                task_config={
                    **task.task_config,
                    "_task_target": task.target.to_dict(),
                    **(
                        {"_trusted_target_url": task.target_url}
                        if task.target_url is not None
                        else {}
                    ),
                },
                page_sampling_policy=task.page_sampling_policy,
                loop_guard=task.loop_guard,
                submitted_at=now,
            )
            await connection.execute(
                """
                INSERT INTO qc_agent_run (
                    id, task_id, agent_type, agent_name, status,
                    input_context, loop_guard, started_at, created_at, updated_at
                ) VALUES (
                    $1::uuid, $2::uuid, 'main', 'Main Agent', 'pending',
                    $3::jsonb, $4::jsonb, NULL, $5, $5
                )
                """,
                run_id,
                task.task_id,
                _json_dumps(agent_run_input_context(record)),
                _json_dumps(record.loop_guard),
                now,
            )
            submission_id = str(uuid4())
            await connection.execute(
                """
                INSERT INTO qc_task_submission (
                    id, task_id, initial_agent_run_id, tenant_id, submitter_id,
                    submission_kind, idempotency_key, request_hash,
                    request_payload, committed_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                    $6, $7, $8, $9::jsonb, $10
                )
                """,
                submission_id,
                task.task_id,
                run_id,
                command.access_scope.tenant_id,
                command.access_scope.actor_user_id,
                command.submission_kind,
                command.idempotency_key,
                command.request_hash,
                _json_dumps(command.request_payload),
                now,
            )
            await insert_outbox_event(
                connection,
                aggregate_type="task_submission",
                aggregate_id=submission_id,
                envelope=build_task_submission_committed(
                    submission_id=submission_id,
                    task_id=task.task_id,
                    run_id=run_id,
                    request_hash=command.request_hash,
                    occurred_at=now,
                ),
            )
        return PersistedTaskSubmission(task=task, run_id=run_id, replayed=False)

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope | None = None
    ) -> TaskDetail | None:
        pool = await self._get_pool()
        scope_sql, scope_args = _optional_read_scope_sql(
            access_scope,
            task_alias="task",
            first_param=2,
        )
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT
                    task.id::text AS task_id,
                    task.target_platform,
                    task.target_url,
                    task.ruleset_id::text AS ruleset_id,
                    ruleset.ruleset_code,
                    ruleset.version AS ruleset_version,
                    ruleset.snapshot_hash AS ruleset_snapshot_hash,
                    task.channel,
                    task.status,
                    task.priority,
                    agent_run.id::text AS run_id,
                    task.created_at,
                    task.started_at,
                    task.finished_at,
                    task.last_error,
                    task.task_config AS inspection_task_config,
                    NULLIF(BTRIM(COALESCE(task.task_config ->> 'remark', '')), '')
                        AS remark,
                    evidence_index.index_id AS evidence_index_id,
                    evidence_index.status AS exploration_status,
                    evidence_index.index_payload
                FROM qc_task task
                JOIN qc_ruleset ruleset ON ruleset.id = task.ruleset_id
                -- 默认 Run 与工作台任务详情一致：H5 收口 Run 优先，其次探索主 Run；
                -- 独立判定 Run 没有页面采集，不能当报告和任务详情的默认 Run
                LEFT JOIN LATERAL (
                    SELECT id
                    FROM qc_agent_run
                    WHERE task_id = task.id
                    ORDER BY
                        CASE
                            WHEN task.target_platform = 'h5'
                             AND id = task.finalization_agent_run_id
                            THEN 0
                            WHEN agent_type = 'main' THEN 1
                            ELSE 2
                        END,
                        created_at DESC
                    LIMIT 1
                ) agent_run ON TRUE
                LEFT JOIN LATERAL (
                    -- 表名为 qc_app_evidence_index（迁移 0046），且表内没有
                    -- index_payload 列；这里按解析方期待的形状用真实列拼出来。
                    SELECT
                        index_id,
                        status,
                        jsonb_build_object('evidence_gap', evidence_gaps)
                            AS index_payload
                    FROM qc_app_evidence_index
                    WHERE task_id = task.id
                    ORDER BY sealed_at DESC
                    LIMIT 1
                ) evidence_index ON TRUE
                WHERE task.id = $1::uuid
                  {scope_sql}
                """,
                task_id,
                *scope_args,
            )
        if row is None:
            return None
        return TaskDetail(
            task_id=row["task_id"],
            target_platform=row.get("target_platform", "h5"),
            target_url=row["target_url"],
            ruleset_id=row["ruleset_id"],
            ruleset_code=row["ruleset_code"],
            ruleset_version=row["ruleset_version"],
            channel=row["channel"],
            status=row["status"],
            priority=row["priority"],
            run_id=row["run_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            last_error=row["last_error"],
            ruleset_snapshot_hash=row.get("ruleset_snapshot_hash"),
            inspection_scope=tuple(sorted(inspection_scope_page_types(
                _json_dict(row.get("inspection_task_config")),
            ))),
            evidence_index_id=row.get("evidence_index_id"),
            exploration_status=row.get("exploration_status"),
            evidence_gap=_exploration_text_tuple(row.get("index_payload"), "evidence_gap"),
            page_timeline=_exploration_text_tuple(row.get("index_payload"), "page_timeline"),
            remark=row.get("remark"),
        )

    async def list_tasks(
        self,
        *,
        status: str | None,
        limit: int,
        access_scope: AccessScope,
    ) -> list[TaskListItem]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    task.id::text AS task_id,
                    task.target_platform,
                    task.target_url,
                    ruleset.ruleset_code,
                    ruleset.version AS ruleset_version,
                    task.channel,
                    task.status,
                    task.priority,
                    agent_run.id::text AS run_id,
                    task.created_at,
                    task.started_at,
                    task.finished_at
                FROM qc_task task
                JOIN qc_ruleset ruleset ON ruleset.id = task.ruleset_id
                LEFT JOIN LATERAL (
                    SELECT id
                    FROM qc_agent_run
                    WHERE task_id = task.id
                    ORDER BY
                        CASE
                            WHEN task.target_platform = 'h5'
                             AND id = task.finalization_agent_run_id
                            THEN 0
                            WHEN agent_type = 'main' THEN 1
                            ELSE 2
                        END,
                        created_at DESC
                    LIMIT 1
                ) agent_run ON TRUE
                WHERE ($1::varchar IS NULL OR task.status = $1)
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
                ORDER BY task.created_at DESC
                LIMIT $2
                """,
                status,
                limit,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
        return [
            TaskListItem(
                task_id=row["task_id"],
                target_platform=row.get("target_platform", "h5"),
                target_url=row["target_url"],
                ruleset_code=row["ruleset_code"],
                ruleset_version=row["ruleset_version"],
                channel=row["channel"],
                status=row["status"],
                priority=row["priority"],
                run_id=row["run_id"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
            for row in rows
        ]

    async def list_rule_executions(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[RuleExecutionSummary]:
        pool = await self._get_pool()
        scope_sql, scope_args = _optional_read_scope_sql(
            access_scope,
            task_alias="task",
            first_param=3,
        )
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                {rule_replacement_ctes_sql(scope_sql)}
                SELECT
                    execution.id::text AS execution_id,
                    execution.rule_id::text AS rule_id,
                    rule.rule_code,
                    rule.name AS rule_name,
                    execution.page_capture_id::text AS page_capture_id,
                    execution.evidence_input_set_id::text AS evidence_input_set_id,
                    execution.observation_packet_id::text AS observation_packet_id,
                    execution.evidence_artifact_ids,
                    execution.execution_status,
                    execution.judge_strategy,
                    execution.confidence,
                    execution.severity,
                    execution.result_summary,
                    execution.problem_desc,
                    execution.review_status,
                    (
                        ARRAY_REMOVE(
                            ARRAY[
                                NULLIF(execution.error_code, ''),
                                NULLIF(execution.judge_output->>'error_code', '')
                            ],
                            NULL
                        )
                        ||
                        ARRAY(
                            SELECT diagnostic_code
                            FROM jsonb_array_elements_text(
                                CASE
                                    WHEN jsonb_typeof(
                                        execution.judge_output->'conflicts'
                                    ) = 'array'
                                    THEN execution.judge_output->'conflicts'
                                    ELSE '[]'::jsonb
                                END
                            ) AS diagnostic_code
                        )
                    ) AS diagnostic_codes,
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
                        ) missing_items
                        WHERE btrim(missing_item) <> ''
                        ORDER BY missing_item
                    ) AS missing_evidence,
                    execution.started_at,
                    execution.finished_at,
                    execution.source_idempotency_key,
                    execution.judge_input,
                    CASE
                        WHEN execution.judge_input->>'bootstrap' = 'true' THEN 'bootstrap'
                        ELSE 'agent'
                    END AS source
                FROM qc_rule_execution execution
                JOIN qc_rule rule ON rule.id = execution.rule_id
                JOIN qc_task task ON task.id = execution.task_id
                WHERE execution.task_id = $1::uuid
                  AND ($2::uuid IS NULL OR execution.agent_run_id = $2::uuid)
                  {scope_sql}
                  AND execution.id NOT IN (
                      SELECT execution_id FROM superseded_execution_ids
                  )
                  AND (
                      coalesce(execution.judge_input->>'bootstrap', 'false') <> 'true'
                      OR NOT EXISTS (
                          SELECT 1
                          FROM qc_rule_execution real_execution
                          WHERE real_execution.task_id = execution.task_id
                            AND real_execution.agent_run_id IS NOT DISTINCT FROM
                                execution.agent_run_id
                            AND real_execution.rule_id = execution.rule_id
                            AND real_execution.page_capture_id IS NOT DISTINCT FROM
                                execution.page_capture_id
                            AND coalesce(
                                real_execution.judge_input->>'bootstrap',
                                'false'
                            ) <> 'true'
                      )
                  )
                ORDER BY rule.sort_order ASC, execution.created_at ASC
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
        return [
            RuleExecutionSummary(
                execution_id=row["execution_id"],
                rule_id=row["rule_id"],
                rule_code=row["rule_code"],
                rule_name=row["rule_name"],
                page_capture_id=row["page_capture_id"],
                evidence_input_set_id=row.get("evidence_input_set_id"),
                observation_packet_id=row.get("observation_packet_id"),
                execution_status=row["execution_status"],
                judge_strategy=row["judge_strategy"],
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                severity=row["severity"],
                result_summary=row["result_summary"],
                problem_desc=row["problem_desc"],
                review_status=row["review_status"],
                diagnostic_codes=tuple(row["diagnostic_codes"] or ()),
                missing_evidence=tuple(row["missing_evidence"] or ()),
                evidence_artifact_ids=tuple(_json_list(row["evidence_artifact_ids"])),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                source=row["source"],
                source_idempotency_key=row.get("source_idempotency_key"),
                judge_input=_json_dict(row.get("judge_input")),
            )
            for row in rows
        ]

    async def list_evidence_artifacts(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[EvidenceArtifactSummary]:
        capture_in_scope = page_type_in_inspection_scope_sql(
            "task.task_config",
            "COALESCE(page.page_type, capture.metadata->>'page_type', '')",
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            if not await _can_read_task(
                connection,
                task_id=task_id,
                access_scope=access_scope,
            ):
                return []
            rows = await connection.fetch(
                f"""
                SELECT
                    evidence.id::text AS artifact_id,
                    evidence.page_capture_id::text AS page_capture_id,
                    evidence.rule_execution_id::text AS rule_execution_id,
                    evidence.artifact_type,
                    evidence.source,
                    evidence.storage_path,
                    evidence.content_type,
                    evidence.checksum,
                    evidence.size_bytes,
                    evidence.metadata,
                    evidence.created_at
                FROM qc_evidence_artifact evidence
                INNER JOIN qc_task task
                  ON task.id = evidence.task_id
                LEFT JOIN qc_capture capture
                  ON capture.id = evidence.page_capture_id
                LEFT JOIN qc_page_capture page
                  ON page.id = evidence.page_capture_id
                LEFT JOIN qc_rule_execution execution
                  ON execution.id = evidence.rule_execution_id
                WHERE evidence.task_id = $1::uuid
                  AND (
                      $2::uuid IS NULL
                      OR capture.agent_run_id = $2::uuid
                      -- 规则结果不分 Run，挂在规则执行上的证据跟着结果走
                      OR execution.id IS NOT NULL
                      OR evidence.metadata->>'agent_run_id' = $2::uuid::text
                  )
                  -- 经过的页面只留认路记录：它的截图和页面结构不当证据，被规则结论用到的除外
                  AND (
                      {capture_in_scope}
                      OR execution.id IS NOT NULL
                      OR evidence.page_capture_id IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM qc_rule_execution used
                          WHERE used.task_id = evidence.task_id
                            AND used.evidence_artifact_ids ? evidence.id::text
                      )
                  )
                ORDER BY evidence.created_at ASC, evidence.id ASC
                """,
                task_id,
                agent_run_id,
            )
        return [
            EvidenceArtifactSummary(
                artifact_id=row["artifact_id"],
                page_capture_id=row["page_capture_id"],
                rule_execution_id=row["rule_execution_id"],
                artifact_type=row["artifact_type"],
                source=row["source"],
                storage_path=row["storage_path"],
                content_type=row["content_type"],
                checksum=row["checksum"],
                size_bytes=row["size_bytes"],
                metadata=_json_dict(row["metadata"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def list_task_target_page_coverage(
        self,
        *,
        task_id: str,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[TaskTargetPageCoverage]:
        """按任务锁定目标页面返回当前 Run 的覆盖事实，不推断业务适用性。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            if not await _can_read_task(
                connection,
                task_id=task_id,
                access_scope=access_scope,
            ):
                return []
            rows = await connection.fetch(
                """
                WITH task_context AS (
                    SELECT
                        task.id,
                        task.target_platform,
                        task.finalization_epoch,
                        task.page_sampling_policy
                    FROM qc_task AS task
                    WHERE task.id = $1::uuid
                ),
                h5_required_page_type AS (
                    SELECT DISTINCT required.page_type
                    FROM task_context AS task
                    INNER JOIN LATERAL (
                        SELECT snapshot.required_page_types
                        FROM qc_h5_coverage_snapshot AS snapshot
                        WHERE snapshot.task_id = task.id
                          AND (
                                $2::uuid IS NULL
                                OR snapshot.agent_run_id = $2::uuid
                              )
                          AND snapshot.finalization_epoch <= task.finalization_epoch
                        ORDER BY snapshot.finalization_epoch DESC, snapshot.revision DESC
                        LIMIT 1
                    ) AS coverage ON task.target_platform IN ('h5', 'web')
                    CROSS JOIN LATERAL jsonb_array_elements_text(
                        CASE
                            WHEN jsonb_typeof(coverage.required_page_types) = 'array'
                            THEN coverage.required_page_types
                            ELSE '[]'::jsonb
                        END
                    ) AS required(page_type)
                ),
                target_page_type AS (
                    SELECT page_type
                    FROM h5_required_page_type
                    UNION
                    SELECT DISTINCT jsonb_array_elements_text(
                        CASE
                            WHEN jsonb_typeof(
                                task.page_sampling_policy->'rule_target_page_types'
                            ) = 'array'
                            THEN task.page_sampling_policy->'rule_target_page_types'
                            ELSE '[]'::jsonb
                        END
                    ) AS page_type
                    FROM task_context AS task
                    WHERE NOT EXISTS (SELECT 1 FROM h5_required_page_type)
                ),
                capture_stats AS (
                    SELECT
                        COALESCE(
                            page_capture.page_type,
                            common_capture.metadata->>'page_type'
                        ) AS page_type,
                        count(*)::int AS capture_count
                    FROM qc_capture common_capture
                    LEFT JOIN qc_page_capture page_capture
                      ON page_capture.id = common_capture.id
                    WHERE common_capture.task_id = $1::uuid
                      AND (
                          $2::uuid IS NULL
                          OR common_capture.agent_run_id = $2::uuid
                      )
                      AND (
                          COALESCE(
                              page_capture.detection_status,
                              common_capture.metadata->>'detection_status'
                          ) = 'recognized'
                          OR (
                              -- App 历史标注只写了 page_type；与 recognized 等价计入覆盖。
                              COALESCE(
                                  page_capture.detection_status,
                                  common_capture.metadata->>'detection_status',
                                  ''
                              ) = ''
                              AND COALESCE(
                                  page_capture.page_type,
                                  common_capture.metadata->>'page_type'
                              ) IS NOT NULL
                              AND EXISTS (
                                  SELECT 1
                                  FROM qc_task AS task_platform
                                  WHERE task_platform.id = common_capture.task_id
                                    AND task_platform.target_platform = 'app'
                              )
                          )
                      )
                      AND COALESCE(
                          page_capture.page_type,
                          common_capture.metadata->>'page_type'
                      ) IS NOT NULL
                    GROUP BY COALESCE(
                        page_capture.page_type,
                        common_capture.metadata->>'page_type'
                    )
                ),
                action_stats AS (
                    SELECT
                        action.expected_next_page_type AS page_type,
                        count(*) FILTER (
                            WHERE action.status = 'explored'
                        )::int AS explored_action_count,
                        count(*) FILTER (
                            WHERE action.status = 'failed'
                        )::int AS failed_action_count,
                        count(*) FILTER (
                            WHERE action.status = 'blocked'
                        )::int AS blocked_action_count,
                        count(*) FILTER (
                            WHERE action.status = 'skipped'
                        )::int AS skipped_action_count,
                        count(*) FILTER (
                            WHERE action.status IN ('candidate', 'selected', 'pending')
                        )::int AS pending_action_count,
                        array_remove(
                            array_agg(DISTINCT action.failure_code),
                            NULL
                        ) AS failure_codes
                    FROM qc_page_exploration_action action
                    WHERE action.task_id = $1::uuid
                      AND (
                          $2::uuid IS NULL
                          OR action.agent_run_id = $2::uuid
                      )
                      AND action.expected_next_page_type IS NOT NULL
                    GROUP BY action.expected_next_page_type
                )
                SELECT
                    target.page_type,
                    coalesce(capture.capture_count, 0)::int AS capture_count,
                    coalesce(action.explored_action_count, 0)::int
                        AS explored_action_count,
                    coalesce(action.failed_action_count, 0)::int
                        AS failed_action_count,
                    coalesce(action.blocked_action_count, 0)::int
                        AS blocked_action_count,
                    coalesce(action.skipped_action_count, 0)::int
                        AS skipped_action_count,
                    coalesce(action.pending_action_count, 0)::int
                        AS pending_action_count,
                    coalesce(action.failure_codes, ARRAY[]::varchar[])
                        AS failure_codes
                FROM target_page_type target
                LEFT JOIN capture_stats capture USING (page_type)
                LEFT JOIN action_stats action USING (page_type)
                ORDER BY target.page_type
                """,
                task_id,
                agent_run_id,
            )
        return [
            TaskTargetPageCoverage(
                page_type=row["page_type"],
                capture_count=row["capture_count"],
                explored_action_count=row["explored_action_count"],
                failed_action_count=row["failed_action_count"],
                blocked_action_count=row["blocked_action_count"],
                skipped_action_count=row["skipped_action_count"],
                pending_action_count=row["pending_action_count"],
                failure_codes=tuple(row["failure_codes"] or ()),
            )
            for row in rows
        ]

    async def get_final_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict | None:
        """优先读取当前 Gate 的不可变正文，迁移前数据回退到 Agent Run。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            if not await _can_read_task(
                connection,
                task_id=task_id,
                access_scope=access_scope,
            ):
                return None
            row = await connection.fetchrow(
                """
                WITH target_task AS (
                    SELECT task.*
                    FROM qc_task AS task
                    WHERE task.id = $1::uuid
                ), current_binding AS (
                    SELECT
                        payload.report_payload,
                        binding.report_id,
                        binding.gate_snapshot_id,
                        binding.finalization_epoch,
                        binding.report_version,
                        binding.report_payload_hash,
                        coverage.id::text AS coverage_snapshot_id,
                        coverage.task_id::text AS coverage_task_id,
                        coverage.agent_run_id::text AS coverage_agent_run_id,
                        coverage.finalization_epoch AS coverage_finalization_epoch,
                        coverage.required_page_types AS coverage_required_page_types,
                        coverage.observed_page_types AS coverage_observed_page_types,
                        coverage.missing_page_types AS coverage_missing_page_types,
                        coverage.unrecognized_page_types
                            AS coverage_unrecognized_page_types,
                        coverage.normalization_version
                            AS coverage_normalization_version,
                        coverage.coverage_status AS coverage_status,
                        coverage.snapshot_hash AS coverage_snapshot_hash,
                        coverage.created_at AS coverage_created_at,
                        coverage.applicability_snapshot
                            AS coverage_applicability_snapshot
                    FROM target_task AS task
                    INNER JOIN qc_report_gate_binding AS binding
                      ON binding.task_id = task.id
                    INNER JOIN qc_completion_gate_snapshot AS gate
                      ON gate.task_id = binding.task_id
                     AND gate.gate_snapshot_id = binding.gate_snapshot_id
                     AND gate.finalization_epoch = binding.finalization_epoch
                     AND gate.gate_passed
                    INNER JOIN qc_report_payload_snapshot AS payload
                      ON payload.report_id = binding.report_id
                     AND payload.report_version = binding.report_version
                     AND payload.report_payload_hash = binding.report_payload_hash
                    LEFT JOIN qc_web_coverage_snapshot AS coverage
                      ON coverage.task_id = binding.task_id
                     AND coverage.agent_run_id::text
                         = payload.report_payload->>'agent_run_id'
                     AND coverage.finalization_epoch = binding.finalization_epoch
                    WHERE $2::uuid IS NULL
                       OR payload.report_payload->>'agent_run_id' = $2::uuid::text
                    ORDER BY binding.finalization_epoch DESC, binding.report_version DESC
                    LIMIT 1
                ), legacy_report AS (
                    SELECT run.output_context->'final_report' AS report_payload
                    FROM qc_agent_run AS run
                    WHERE run.task_id = $1::uuid
                      AND ($2::uuid IS NULL OR run.id = $2::uuid)
                      AND jsonb_typeof(
                          COALESCE(run.output_context, '{}'::jsonb)->'final_report'
                      ) = 'object'
                    ORDER BY
                        CASE
                            WHEN (
                                SELECT
                                    target_platform = 'h5'
                                    AND LOWER(COALESCE(
                                        task_config->>'workflow_version',
                                        ''
                                    )) = 'v2'
                                FROM target_task
                            )
                            AND run.id = (
                                SELECT finalization_agent_run_id
                                FROM target_task
                            ) THEN 0
                            WHEN (
                                SELECT
                                    target_platform = 'h5'
                                    AND LOWER(COALESCE(
                                        task_config->>'workflow_version',
                                        ''
                                    )) = 'v2'
                                FROM target_task
                            )
                            AND run.agent_type = 'main' THEN 1
                            ELSE 2
                        END,
                        run.created_at DESC
                    LIMIT 1
                )
                SELECT
                    COALESCE(
                        current_binding.report_payload,
                        legacy_report.report_payload
                    ) AS final_report,
                    current_binding.report_payload IS NOT NULL AS current_report_bound,
                    current_binding.report_id AS bound_report_id,
                    current_binding.gate_snapshot_id AS bound_gate_snapshot_id,
                    current_binding.finalization_epoch AS bound_finalization_epoch,
                    current_binding.report_version AS bound_report_version,
                    current_binding.report_payload_hash AS bound_report_payload_hash,
                    current_binding.coverage_snapshot_id AS bound_coverage_snapshot_id,
                    current_binding.coverage_task_id AS bound_coverage_task_id,
                    current_binding.coverage_agent_run_id
                        AS bound_coverage_agent_run_id,
                    current_binding.coverage_finalization_epoch
                        AS bound_coverage_finalization_epoch,
                    current_binding.coverage_required_page_types
                        AS bound_coverage_required_page_types,
                    current_binding.coverage_observed_page_types
                        AS bound_coverage_observed_page_types,
                    current_binding.coverage_missing_page_types
                        AS bound_coverage_missing_page_types,
                    current_binding.coverage_unrecognized_page_types
                        AS bound_coverage_unrecognized_page_types,
                    current_binding.coverage_normalization_version
                        AS bound_coverage_normalization_version,
                    current_binding.coverage_status AS bound_coverage_status,
                    current_binding.coverage_snapshot_hash AS bound_coverage_snapshot_hash,
                    current_binding.coverage_created_at AS bound_coverage_created_at,
                    current_binding.coverage_applicability_snapshot
                        AS bound_coverage_applicability_snapshot,
                    CASE WHEN gate.gate_snapshot_id IS NULL THEN NULL ELSE
                        jsonb_build_object(
                            'gate_passed', gate.gate_passed,
                            'block_reasons', gate.block_reasons,
                            'gate_snapshot_id', gate.gate_snapshot_id,
                            'finalization_epoch', gate.finalization_epoch
                        )
                    END AS current_completion_gate,
                    CASE WHEN current_task.id IS NULL THEN NULL ELSE
                        jsonb_build_object(
                            'status', current_task.status,
                            'finished_at', current_task.finished_at,
                            'updated_at', current_task.updated_at,
                            'target_platform', current_task.target_platform,
                            'task_config', current_task.task_config,
                            'finalization_state', current_task.finalization_state,
                            'finalization_epoch', current_task.finalization_epoch,
                            'finalization_agent_run_id',
                            current_task.finalization_agent_run_id,
                            'completion_gate_snapshot_id',
                            current_task.completion_gate_snapshot_id
                        )
                    END AS current_task,
                    jsonb_build_object(
                        'target_platform', task.target_platform,
                        'task_config', task.task_config,
                        'finalization_state', task.finalization_state,
                        'finalization_epoch', task.finalization_epoch,
                        'finalization_agent_run_id',
                        task.finalization_agent_run_id,
                        'completion_gate_snapshot_id',
                        task.completion_gate_snapshot_id
                    ) AS target_context
                FROM target_task AS task
                LEFT JOIN target_task AS current_task
                  ON $2::uuid IS NULL
                  OR current_task.finalization_agent_run_id = $2::uuid
                LEFT JOIN current_binding ON TRUE
                LEFT JOIN legacy_report ON TRUE
                LEFT JOIN qc_completion_gate_snapshot AS gate
                  ON gate.gate_snapshot_id = current_task.completion_gate_snapshot_id
                 AND gate.task_id = current_task.id
                 AND gate.finalization_epoch = current_task.finalization_epoch
                """,
                task_id,
                agent_run_id,
            )
        if row is None or row["final_report"] is None:
            return None
        report = _json_dict(row["final_report"])
        target_context_value = (
            row.get("target_context") if hasattr(row, "get") else row["target_context"]
        )
        target_context = _json_dict(target_context_value)
        task_config = _json_dict(target_context.get("task_config"))
        is_web_formal = is_database_web_v2_formal(
            target_platform=target_context.get("target_platform"),
            task_config=task_config,
        )
        is_h5_v2 = (
            str(target_context.get("target_platform") or "").casefold() == "h5"
            and str(task_config.get("workflow_version") or "").casefold() == "v2"
            and task_config.get("execution_mode") == "full_inspection"
        )
        current_report_bound = (
            row.get("current_report_bound")
            if hasattr(row, "get")
            else row["current_report_bound"]
        )
        if is_h5_v2 and current_report_bound is not True:
            current_owner = target_context.get("finalization_agent_run_id")
            if agent_run_id is None or (
                current_owner is not None and agent_run_id == str(current_owner)
            ):
                return None
        if current_report_bound is True:
            bound_gate_snapshot_id = row.get("bound_gate_snapshot_id")
            bound_finalization_epoch = row.get("bound_finalization_epoch")
            bound_report_version = row.get("bound_report_version")
            bound_report_payload_hash = row.get("bound_report_payload_hash")
            if is_web_formal:
                if (
                    row.get("bound_coverage_snapshot_id") is None
                    or row.get("bound_coverage_snapshot_hash") is None
                ):
                    return None
                try:
                    coverage_snapshot = parse_web_coverage_snapshot_row(
                        {
                            "id": row.get("bound_coverage_snapshot_id"),
                            "task_id": row.get("bound_coverage_task_id"),
                            "agent_run_id": row.get(
                                "bound_coverage_agent_run_id"
                            ),
                            "finalization_epoch": row.get(
                                "bound_coverage_finalization_epoch"
                            ),
                            "required_page_types": row.get(
                                "bound_coverage_required_page_types"
                            ),
                            "observed_page_types": row.get(
                                "bound_coverage_observed_page_types"
                            ),
                            "missing_page_types": row.get(
                                "bound_coverage_missing_page_types"
                            ),
                            "unrecognized_page_types": row.get(
                                "bound_coverage_unrecognized_page_types"
                            ),
                            "normalization_version": row.get(
                                "bound_coverage_normalization_version"
                            ),
                            "coverage_status": row.get("bound_coverage_status"),
                            "snapshot_hash": row.get(
                                "bound_coverage_snapshot_hash"
                            ),
                            "created_at": row.get("bound_coverage_created_at"),
                            "applicability_snapshot": row.get(
                                "bound_coverage_applicability_snapshot"
                            ),
                        }
                    )
                    validate_web_final_report_binding(
                        report,
                        expectation=WebFinalReportBindingExpectation(
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                            report_id=(
                                str(row.get("bound_report_id"))
                                if row.get("bound_report_id") is not None
                                else None
                            ),
                            report_version=bound_report_version,
                            report_payload_hash=bound_report_payload_hash,
                            gate_snapshot_id=bound_gate_snapshot_id,
                            finalization_epoch=bound_finalization_epoch,
                            coverage_snapshot_id=(
                                str(row.get("bound_coverage_snapshot_id"))
                                if row.get("bound_coverage_snapshot_id") is not None
                                else None
                            ),
                            coverage_snapshot_hash=(
                                str(row.get("bound_coverage_snapshot_hash"))
                                if row.get("bound_coverage_snapshot_hash") is not None
                                else None
                            ),
                            coverage_snapshot=coverage_snapshot,
                        ),
                    )
                except (
                    WebFinalReportBindingError,
                    InvalidWebCoverageSnapshotRowError,
                    TypeError,
                    ValueError,
                ):
                    return None
            else:
                report_task_id = _json_dict(report.get("task")).get("task_id")
                report_gate = _json_dict(report.get("completion_gate"))
                report_owner = report.get("agent_run_id")
                target_owner = target_context.get("finalization_agent_run_id")
                bound_report_id = row.get("bound_report_id")
                if not (
                    report.get("report_stage") == "final"
                    and report.get("is_final") is True
                    and report_task_id == task_id
                    and report_gate.get("gate_snapshot_id") == bound_gate_snapshot_id
                    and report_gate.get("finalization_epoch") == bound_finalization_epoch
                    and (
                        bound_report_version is None
                        or report.get("report_binding_version") == bound_report_version
                    )
                    and (
                        bound_report_payload_hash is None
                        or stable_report_payload_hash(report) == bound_report_payload_hash
                    )
                    and (agent_run_id is None or report.get("agent_run_id") == agent_run_id)
                ):
                    return None
                if is_h5_v2 and not (
                    target_context.get("finalization_state") == "sealed"
                    and target_context.get("finalization_epoch")
                    == bound_finalization_epoch
                    and target_context.get("completion_gate_snapshot_id")
                    == bound_gate_snapshot_id
                    and target_owner is not None
                    and report_owner == str(target_owner)
                    and (agent_run_id is None or agent_run_id == str(target_owner))
                    and bound_report_id
                    == f"task:{task_id}:run:{target_owner}"
                    and report.get("report_id") == bound_report_id
                    and isinstance(bound_report_version, int)
                    and not isinstance(bound_report_version, bool)
                    and bound_report_version > 0
                    and report_gate.get("gate_passed") is True
                ):
                    return None
            return report
        current_gate_value = (
            row.get("current_completion_gate")
            if hasattr(row, "get")
            else row["current_completion_gate"]
        )
        current_completion_gate = _json_dict(current_gate_value)
        current_task_value = row.get("current_task") if hasattr(row, "get") else row["current_task"]
        current_task = _json_dict(current_task_value)
        target_context = target_context or current_task
        task_config = _json_dict(target_context.get("task_config"))
        is_web_formal = is_database_web_v2_formal(
            target_platform=target_context.get("target_platform"),
            task_config=task_config,
        )
        if is_web_formal:
            bound_gate_snapshot_id = (
                row.get("bound_gate_snapshot_id")
                if hasattr(row, "get")
                else row["bound_gate_snapshot_id"]
            )
            bound_finalization_epoch = (
                row.get("bound_finalization_epoch")
                if hasattr(row, "get")
                else row["bound_finalization_epoch"]
            )
            task_epoch = current_task.get("finalization_epoch")
            task_owner = current_task.get("finalization_agent_run_id")
            report_owner = report.get("agent_run_id")
            report_task_id = _json_dict(report.get("task")).get("task_id")
            report_gate = _json_dict(report.get("completion_gate"))
            current_gate_id = current_completion_gate.get("gate_snapshot_id")
            if not (
                current_task.get("finalization_state") == "sealed"
                and current_completion_gate.get("gate_passed") is True
                and bound_gate_snapshot_id == current_gate_id
                and bound_gate_snapshot_id
                == current_task.get("completion_gate_snapshot_id")
                and bound_finalization_epoch == task_epoch
                and current_completion_gate.get("finalization_epoch") == task_epoch
                and task_owner is not None
                and report_owner == str(task_owner)
                and report_task_id == task_id
                and report_gate.get("gate_snapshot_id") == current_gate_id
                and report_gate.get("finalization_epoch") == task_epoch
                and (agent_run_id is None or agent_run_id == str(task_owner))
            ):
                return None
        # Web V2 正式报告必须逐字段保持 Gate 绑定正文，当前任务行只用于校验归属，
        # 不得反向覆盖已封存 payload。
        if current_task and not is_web_formal:
            task_payload = _json_dict(report.get("task"))
            task_payload.update(current_task)
            report["task"] = task_payload
        if current_completion_gate and not is_web_formal:
            report["completion_gate"] = current_completion_gate
        if (current_task or current_completion_gate) and not is_web_formal:
            gate_passed = current_completion_gate.get("gate_passed") is True
            is_terminal = current_task.get("status") in {"done", "failed", "canceled", "review"}
            is_h5_v2 = (
                str(target_context.get("target_platform") or "").casefold() == "h5"
                and str(task_config.get("workflow_version") or "").casefold() == "v2"
            )
            report_is_final = gate_passed and not is_h5_v2
            report["report_stage"] = (
                "final" if report_is_final else ("final_review" if is_terminal else "preview")
            )
            report["is_final"] = report_is_final
            if is_h5_v2:
                report["report_availability"] = "preview"
                report["export_allowed"] = False
                report["export_ready"] = False
                report["incomplete_reasons"] = ["H5_REPORT_NOT_FINALIZED"]
        return report

    async def get_latest_final_report(self, task_id: str) -> dict | None:
        """兼容读取任务最近一次 Agent Run 的终态报告快照。"""
        return await self.get_final_report(task_id)

    async def agent_run_exists(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> bool:
        """校验 Agent Run 是否属于指定任务。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            if not await _can_read_task(
                connection,
                task_id=task_id,
                access_scope=access_scope,
            ):
                return False
            value = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM qc_agent_run
                    WHERE id = $2::uuid
                      AND task_id = $1::uuid
                )
                """,
                task_id,
                agent_run_id,
            )
        return bool(value)

    async def get_page_capture_url(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> str | None:
        """读取页面采集关联 URL；App 缺 URL 时使用已取证包名与 Activity。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    COALESCE(page_capture.url, common_capture.metadata->>'url', '') AS url,
                    common_capture.surface_kind,
                    app_capture.package_name,
                    app_capture.activity
                FROM qc_capture AS common_capture
                LEFT JOIN qc_page_capture AS page_capture
                  ON page_capture.id = common_capture.id
                LEFT JOIN qc_app_capture AS app_capture
                  ON app_capture.capture_id = common_capture.id
                WHERE common_capture.id = $1::uuid
                  AND common_capture.task_id = $2::uuid
                """,
                page_capture_id,
                task_id,
            )
        if row is None:
            return None
        text = str(row["url"] or "").strip()
        if text:
            return text
        if str(row["surface_kind"] or "").casefold() not in {"android", "ios"}:
            return None
        return build_app_observation_location(
            package_name=row["package_name"],
            activity=row["activity"],
        )

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
        access_scope: AccessScope | None = None,
        auth_gate: str | None = AUTH_GATE_AUTO,
    ) -> list[CandidateRule]:
        blocked_auth_requirement = normalize_auth_gate(auth_gate)
        pool = await self._get_pool()
        scope_sql, scope_args = _optional_read_scope_sql(
            access_scope,
            task_alias="task",
            first_param=3,
        )
        auth_param = 3 + len(scope_args)
        async with pool.acquire() as connection:
            inventory = await read_frozen_task_rule_inventory_on_connection(
                connection, task_id=task_id, access_scope=access_scope
            )
            if inventory is not None:
                rows = inventory_candidate_rules(
                    inventory,
                    page_type=page_type,
                    page_url=page_url,
                    blocked_auth_requirement=blocked_auth_requirement,
                )
                return [_candidate_rule_from_inventory(row) for row in rows]
            rows = await connection.fetch(
                f"""
                SELECT
                    rule.id::text AS rule_id,
                    rule.rule_code,
                    rule.name,
                    rule.description,
                    rule.category,
                    rule.severity,
                    rule.executor_type,
                    rule.scope,
                    rule.page_type,
                    rule.applicable_page_types,
                    rule.url_pattern,
                    rule.steps,
                    rule.input_contract,
                    rule.check_point,
                    rule.judge_config,
                    rule.manual_review_policy,
                    rule.sort_order,
                    'applicable_page_types' AS match_reason
                FROM qc_task task
                JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                WHERE task.id = $1::uuid
                  {scope_sql}
                  AND rule.status = 'enabled'
                  AND (rule.channel IS NULL OR rule.channel = task.channel)
                  AND (
                        -- 监测端以规范表为准：未配置/空数组视为三端通用（历史兼容），
                        -- 配置后要求包含任务平台对应端（h5→H5 / app→APP / web→WEB）。
                        jsonb_typeof(rule.input_contract->'target_clients')
                            IS DISTINCT FROM 'array'
                        OR jsonb_array_length(rule.input_contract->'target_clients') = 0
                        OR rule.input_contract->'target_clients' ? (
                            CASE task.target_platform
                                WHEN 'app' THEN 'APP'
                                WHEN 'web' THEN 'WEB'
                                ELSE 'H5'
                            END
                        )
                      )
                  AND COALESCE(
                        rule.judge_config->'capability_config'->>'evidence_input_scope',
                        'page'
                      ) = 'page'
                  AND (
                        -- 登录依赖闸门：会话登录态确定时，拦下与之矛盾的规则。
                        -- App 与 H5 共用：调用方按 Capture session_auth／任务无账号
                        -- 传入 auth_gate；不得再整平台豁免。
                        ${auth_param}::text IS NULL
                        OR COALESCE(rule.input_contract->>'auth_requirement', '')
                            <> ${auth_param}::text
                      )
                  AND rule.applicable_page_types ? $2
                  -- 质检范围外的页面（经过的页面、没选中的首页和开屏）不判规则，也不提采证要求
                  AND {page_type_in_inspection_scope_sql("task.task_config", "$2")}
                ORDER BY rule.sort_order ASC, rule.rule_code ASC
                """,
                task_id,
                page_type,
                *scope_args,
                blocked_auth_requirement,
            )
        filtered_rows: list[dict[str, Any]] = []
        for row in rows:
            from services.api.app.application.web_rule_evidence_contract import (
                read_web_rule_evidence_contract,
            )

            web_contract = read_web_rule_evidence_contract(_json_dict(row["input_contract"]))
            if web_contract is not None and "stable_page" not in web_contract.accepted_roles:
                continue
            matches, match_reason = candidate_rule_matches_page_url(
                page_url=page_url,
                url_pattern=row["url_pattern"],
            )
            if not matches:
                continue
            if match_reason is not None:
                row = dict(row)
                row["match_reason"] = match_reason
            filtered_rows.append(row)
        return [
            CandidateRule(
                rule_id=row["rule_id"],
                rule_code=row["rule_code"],
                name=row["name"],
                description=row["description"],
                category=row["category"],
                severity=row["severity"],
                executor_type=row["executor_type"],
                scope=row["scope"],
                page_type=row["page_type"],
                applicable_page_types=_json_list(row["applicable_page_types"]),
                url_pattern=row["url_pattern"],
                steps=_json_array(row["steps"]),
                input_contract=_json_dict(row["input_contract"]),
                check_point=row["check_point"],
                judge_config=_json_dict(row["judge_config"]),
                manual_review_policy=_json_dict(row["manual_review_policy"]),
                sort_order=row["sort_order"],
                match_reason=row["match_reason"],
            )
            for row in filtered_rows
        ]

    async def get_rule_judge_definition(
        self, *, task_id: str, rule_code: str
    ) -> RuleJudgeDefinition | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            inventory = await read_frozen_task_rule_inventory_on_connection(
                connection, task_id=task_id
            )
            if inventory is not None:
                rules = [rule for rule in inventory["rules"] if rule["rule_code"] == rule_code]
                if len(rules) != 1 or rules[0]["applicability"] != "applicable":
                    return None
                rule = rules[0]
                capability = (rule.get("judge_config") or {}).get("capability_config") or {}
                if capability.get("evidence_input_scope", "page") != "page":
                    return None
                return _rule_judge_definition_from_inventory(rule)
            row = await connection.fetchrow(
                """
                SELECT
                    rule.id::text AS rule_id,
                    rule.rule_code,
                    rule.name,
                    rule.category,
                    rule.executor_type,
                    rule.severity,
                    rule.check_point,
                    rule.steps,
                    rule.input_contract,
                    rule.judge_config,
                    rule.manual_review_policy
                FROM qc_task task
                JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                WHERE task.id = $1::uuid
                  AND rule.rule_code = $2
                  AND rule.status = 'enabled'
                  AND (rule.channel IS NULL OR rule.channel = task.channel)
                  AND COALESCE(
                        rule.judge_config->'capability_config'->>'evidence_input_scope',
                        'page'
                      ) = 'page'
                """,
                task_id,
                rule_code,
            )
        if row is None:
            return None
        return RuleJudgeDefinition(
            rule_id=row["rule_id"],
            rule_code=row["rule_code"],
            executor_type=row["executor_type"],
            judge_config=_json_dict(row["judge_config"]),
            rule_name=row.get("name") or row["rule_code"],
            category=row.get("category") or "",
            severity=row.get("severity") or "low",
            check_point=row.get("check_point") or "",
            steps=_json_array(row.get("steps")),
            input_contract=_json_dict(row.get("input_contract")),
            manual_review_policy=_json_dict(row.get("manual_review_policy")),
        )

    async def list_enabled_rule_judge_definitions(self) -> list[RuleJudgeDefinition]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    rule.id::text AS rule_id,
                    rule.rule_code,
                    rule.name,
                    rule.category,
                    rule.executor_type,
                    rule.severity,
                    rule.check_point,
                    rule.steps,
                    rule.input_contract,
                    rule.judge_config,
                    rule.manual_review_policy
                FROM qc_rule rule
                JOIN qc_ruleset ruleset ON ruleset.id = rule.ruleset_id
                WHERE rule.status = 'enabled'
                  AND ruleset.status = 'enabled'
                ORDER BY rule.rule_code ASC
                """
            )
        return [
            RuleJudgeDefinition(
                rule_id=row["rule_id"],
                rule_code=row["rule_code"],
                executor_type=row["executor_type"],
                judge_config=_json_dict(row["judge_config"]),
                rule_name=row.get("name") or row["rule_code"],
                category=row.get("category") or "",
                severity=row.get("severity") or "low",
                check_point=row.get("check_point") or "",
                steps=_json_array(row.get("steps")),
                input_contract=_json_dict(row.get("input_contract")),
                manual_review_policy=_json_dict(row.get("manual_review_policy")),
            )
            for row in rows
        ]

    async def agent_run_is_terminal(self, *, agent_run_id: str) -> bool:
        """判断 Agent Run 是否已进入终态。

        Run 终态后产出的判定必然在工作项收尾时被作废（无宿主可领），
        继续判定纯属烧钱。查不到记录时返回 False（放行），只有明确读到
        终态才拦——宁可多判一次，也不能误杀正在进行的判定。
        """
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            status = await connection.fetchval(
                """
                SELECT status FROM qc_agent_run WHERE id = $1::uuid
                """,
                agent_run_id,
            )
        return status is not None and str(status) not in {"pending", "running"}

    async def get_rule_execution_definition(
        self, *, task_id: str, rule_id: str | None, rule_code: str | None
    ) -> RuleExecutionDefinition | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            inventory = await read_frozen_task_rule_inventory_on_connection(
                connection, task_id=task_id
            )
            if inventory is not None:
                rules = [
                    rule for rule in inventory["rules"]
                    if (rule_id is None or rule["rule_id"] == rule_id)
                    and (rule_code is None or rule["rule_code"] == rule_code)
                    and rule["applicability"] == "applicable"
                ]
                if len(rules) != 1 or (rule_id is None and rule_code is None):
                    return None
                rule = rules[0]
                return RuleExecutionDefinition(
                    executor_type=rule["executor_type"],
                    manual_review_policy=rule.get("manual_review_policy") or {},
                    severity=rule["severity"],
                    judge_config=rule.get("judge_config") or {},
                )
            row = await connection.fetchrow(
                """
                SELECT
                    rule.executor_type,
                    rule.manual_review_policy,
                    rule.severity,
                    rule.judge_config
                FROM qc_task task
                JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                WHERE task.id = $1::uuid
                  AND ($2::uuid IS NULL OR rule.id = $2::uuid)
                  AND ($3::varchar IS NULL OR rule.rule_code = $3)
                  AND rule.status = 'enabled'
                  AND (rule.channel IS NULL OR rule.channel = task.channel)
                """,
                task_id,
                rule_id,
                rule_code,
            )
        if row is None:
            return None
        return RuleExecutionDefinition(
            executor_type=row["executor_type"],
            manual_review_policy=_json_dict(row["manual_review_policy"]),
            severity=row["severity"],
            judge_config=_json_dict(row.get("judge_config")),
        )

    async def get_rule_execution_completeness(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> RuleExecutionCompleteness:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            if not await _can_read_task(
                connection,
                task_id=task_id,
                access_scope=access_scope,
            ):
                return RuleExecutionCompleteness(
                    candidate_query_count=0,
                    candidate_rule_count=0,
                    completed_candidate_count=0,
                    unresolved_rule_codes=(),
                    unresolved_candidate_items=(),
                )
            # 候选页面按本 Run 的采集算；页面上的规则结论不分哪个 Run 写的（判定可能在独立判定 Run）
            row = await connection.fetchrow(
                f"""
                WITH capture_facts AS (
                    SELECT
                        common_capture.id,
                        common_capture.task_id,
                        common_capture.agent_run_id,
                        COALESCE(
                            page_capture.page_type,
                            common_capture.metadata->>'page_type'
                        ) AS page_type,
                        COALESCE(
                            page_capture.parent_page_capture_id,
                            common_capture.parent_capture_id
                        ) AS parent_page_capture_id,
                        COALESCE(
                            page_capture.inspection_lifecycle_status,
                            common_capture.metadata->>'inspection_lifecycle_status'
                        ) AS inspection_lifecycle_status
                    FROM qc_capture common_capture
                    LEFT JOIN qc_page_capture page_capture
                      ON page_capture.id = common_capture.id
                ),
                queried AS (
                    SELECT DISTINCT
                        trace.capability_input->>'page_capture_id' AS page_capture_id,
                        trace.capability_input->>'page_type' AS page_type
                    FROM qc_agent_action_trace trace
                    WHERE trace.task_id = $1::uuid
                      AND trace.agent_run_id = $2::uuid
                      AND trace.selected_capability = 'candidate_rules_tool'
                      AND trace.status = 'ok'
                      AND trace.capability_input ? 'page_capture_id'
                      AND trace.capability_input ? 'page_type'
                      AND trace.capability_input->>'page_type' = ANY($3::varchar[])
                    UNION
                    SELECT DISTINCT
                        assessment.page_capture_id::text AS page_capture_id,
                        assessment.page_type
                    FROM qc_h5_page_type_assessment assessment
                    JOIN qc_task task ON task.id = assessment.task_id
                    WHERE assessment.task_id = $1::uuid
                      AND assessment.agent_run_id = $2::uuid
                      AND assessment.status = 'covered'
                      AND assessment.page_type = ANY($3::varchar[])
                      AND task.target_platform IN ('h5', 'web')
                      AND task.task_config->>'workflow_version' = 'v2'
                ),
                runtime_failed_pages AS (
                    SELECT DISTINCT
                        capture.id::text AS page_capture_id,
                        COALESCE(capture.page_type, node.page_type, parent.page_type)
                            AS page_type
                    FROM qc_rule_execution execution
                    JOIN capture_facts capture
                      ON capture.id = execution.page_capture_id
                    LEFT JOIN qc_task_page_node node
                      ON node.task_id = execution.task_id
                     AND node.agent_run_id = capture.agent_run_id
                     AND node.page_capture_id = execution.page_capture_id
                    LEFT JOIN capture_facts parent
                      ON parent.id = capture.parent_page_capture_id
                     AND parent.task_id = capture.task_id
                    WHERE execution.task_id = $1::uuid
                      AND capture.agent_run_id = $2::uuid
                      AND execution.judge_input->>'runtime_page_failure' = 'true'
                      AND COALESCE(
                          capture.page_type,
                          node.page_type,
                          parent.page_type
                      ) = ANY($3::varchar[])
                ),
                execution_candidates AS (
                    -- Worker 先完成规则收尾再生成最终报告；页面级执行记录因此是
                    -- 候选规则的精确集合，不能再按 page_type 反向扩张规则全集。
                    SELECT DISTINCT
                        execution.page_capture_id::text AS page_capture_id,
                        COALESCE(capture.page_type, node.page_type, parent.page_type)
                            AS page_type,
                        execution.rule_id,
                        rule.rule_code,
                        rule.sort_order,
                        coalesce(execution.judge_input->>'bootstrap', 'false') = 'true'
                            AS is_unresolved
                    FROM qc_rule_execution execution
                    JOIN capture_facts capture
                      ON capture.id = execution.page_capture_id
                     AND capture.task_id = execution.task_id
                     AND capture.agent_run_id = $2::uuid
                    JOIN qc_rule rule ON rule.id = execution.rule_id
                    LEFT JOIN qc_task_page_node node
                      ON node.task_id = execution.task_id
                     AND node.agent_run_id = capture.agent_run_id
                     AND node.page_capture_id = execution.page_capture_id
                    LEFT JOIN capture_facts parent
                      ON parent.id = capture.parent_page_capture_id
                     AND parent.task_id = capture.task_id
                    WHERE execution.task_id = $1::uuid
                      AND execution.page_capture_id IS NOT NULL
                      AND NOT {superseded_placeholder_sql("execution")}
                ),
                candidate_pages AS (
                    SELECT page_capture_id, page_type FROM queried
                    UNION
                    SELECT page_capture_id, page_type FROM runtime_failed_pages
                ),
                candidate_rules AS (
                    SELECT
                        page_capture_id,
                        rule_id,
                        rule_code,
                        sort_order,
                        is_unresolved
                    FROM execution_candidates
                ),
                completed AS (
                    SELECT page_capture_id, rule_id
                    FROM candidate_rules
                    WHERE NOT is_unresolved
                ),
                unresolved AS (
                    SELECT
                        candidate.page_capture_id,
                        candidate.rule_code,
                        candidate.sort_order
                    FROM candidate_rules candidate
                    WHERE candidate.is_unresolved
                )
                SELECT
                    (SELECT count(*) FROM candidate_pages)::integer AS candidate_query_count,
                    (SELECT count(*) FROM candidate_rules)::integer AS candidate_rule_count,
                    (
                        SELECT count(*)
                        FROM candidate_rules candidate
                        JOIN completed
                          ON completed.rule_id = candidate.rule_id
                         AND completed.page_capture_id = candidate.page_capture_id
                    )::integer AS completed_candidate_count,
                    coalesce(
                        (
                            SELECT array_agg(rule_code ORDER BY sort_order, rule_code)
                            FROM unresolved
                        ),
                        ARRAY[]::varchar[]
                    ) AS unresolved_rule_codes,
                    coalesce(
                        (
                            SELECT jsonb_agg(
                                jsonb_build_object(
                                    'page_capture_id', page_capture_id,
                                    'rule_code', rule_code
                                )
                                ORDER BY page_capture_id, sort_order, rule_code
                            )
                            FROM unresolved
                        ),
                        '[]'::jsonb
                    ) AS unresolved_candidate_items
                """,
                task_id,
                agent_run_id,
                list(get_default_page_type_registry().catalog.codes),
            )
        unresolved_items = _json_loads(
            row["unresolved_candidate_items"],
            default=[],
        )
        return RuleExecutionCompleteness(
            candidate_query_count=row["candidate_query_count"],
            candidate_rule_count=row["candidate_rule_count"],
            completed_candidate_count=row["completed_candidate_count"],
            unresolved_rule_codes=tuple(row["unresolved_rule_codes"]),
            unresolved_candidate_items=tuple(
                UnresolvedCandidateRule(
                    page_capture_id=str(item["page_capture_id"]),
                    rule_code=str(item["rule_code"]),
                )
                for item in unresolved_items
                if isinstance(item, dict) and item.get("page_capture_id") and item.get("rule_code")
            ),
        )

    async def get_latest_agent_run(
        self, task_id: str, *, access_scope: AccessScope
    ) -> AgentRunDetail | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            run_row = await connection.fetchrow(
                """
                SELECT
                    id::text AS run_id,
                    status,
                    stop_reason,
                    error_message,
                    total_steps,
                    total_ai_calls,
                    total_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'input_tokens'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'input_tokens'
                        )::bigint
                    END AS input_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'output_tokens'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'output_tokens'
                        )::bigint
                    END AS output_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'cache_hit_tokens'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'cache_hit_tokens'
                        )::bigint
                    END AS cache_hit_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'input_cost'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'input_cost'
                        )::double precision
                    END AS input_cost,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'output_cost'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'output_cost'
                        )::double precision
                    END AS output_cost,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'cache_hit_cost'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'cache_hit_cost'
                        )::double precision
                    END AS cache_hit_cost,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'total_cost'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'total_cost'
                        )::double precision
                    END AS total_cost,
                    output_context->'usage'->>'cost_currency' AS cost_currency,
                    output_context->'usage'->>'pricing_source' AS pricing_source,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'pricing_configured'
                        ) = 'boolean'
                        THEN (
                            output_context->'usage'->>'pricing_configured'
                        )::boolean
                    END AS pricing_configured,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'applied_pricing_tiers'
                        ) = 'array'
                        THEN ARRAY(
                            SELECT jsonb_array_elements_text(
                                output_context->'usage'->'applied_pricing_tiers'
                            )
                        )
                        ELSE ARRAY[]::text[]
                    END AS applied_pricing_tiers,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'worker_rss_before_bytes'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'worker_rss_before_bytes'
                        )::bigint
                    END AS worker_rss_before_bytes,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'worker_rss_after_bytes'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'worker_rss_after_bytes'
                        )::bigint
                    END AS worker_rss_after_bytes,
                    CASE
                        WHEN jsonb_typeof(
                            output_context->'usage'->'worker_rss_delta_bytes'
                        ) = 'number'
                        THEN (
                            output_context->'usage'->>'worker_rss_delta_bytes'
                        )::bigint
                    END AS worker_rss_delta_bytes,
                    pg_column_size(working_memory)::int AS working_memory_bytes,
                    output_context->>'final_text' AS final_text,
                    output_context->>'evidence_index_id' AS evidence_index_id,
                    output_context->>'exploration_status' AS exploration_status,
                    output_context->'evidence_gap' AS evidence_gap,
                    output_context->'page_timeline' AS page_timeline,
                    started_at,
                    finished_at
                FROM qc_agent_run
                WHERE task_id = $1::uuid
                  AND EXISTS (
                      SELECT 1
                      FROM qc_task AS task
                      WHERE task.id = qc_agent_run.task_id
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
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                task_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
            if run_row is None:
                return None
            step_rows = await connection.fetch(
                """
                SELECT
                    step_seq,
                    phase,
                    thought_summary,
                    selected_capability,
                    capability_kind,
                    status,
                    latency_ms,
                    error_code,
                    error_message,
                    capability_input,
                    capability_output
                FROM qc_agent_action_trace
                WHERE agent_run_id = $1::uuid
                ORDER BY step_seq ASC
                """,
                run_row["run_id"],
            )
        return AgentRunDetail(
            run_id=run_row["run_id"],
            status=run_row["status"],
            stop_reason=run_row["stop_reason"],
            error_message=run_row["error_message"],
            total_steps=run_row["total_steps"],
            total_ai_calls=run_row["total_ai_calls"],
            total_tokens=run_row["total_tokens"],
            input_tokens=run_row["input_tokens"],
            output_tokens=run_row["output_tokens"],
            cache_hit_tokens=_row_value(run_row, "cache_hit_tokens"),
            input_cost=_row_value(run_row, "input_cost"),
            output_cost=_row_value(run_row, "output_cost"),
            cache_hit_cost=_row_value(run_row, "cache_hit_cost"),
            total_cost=_row_value(run_row, "total_cost"),
            cost_currency=_row_value(run_row, "cost_currency"),
            pricing_source=_row_value(run_row, "pricing_source"),
            pricing_configured=_row_value(run_row, "pricing_configured"),
            applied_pricing_tiers=tuple(_row_value(run_row, "applied_pricing_tiers", ()) or ()),
            worker_rss_before_bytes=run_row["worker_rss_before_bytes"],
            worker_rss_after_bytes=run_row["worker_rss_after_bytes"],
            worker_rss_delta_bytes=run_row["worker_rss_delta_bytes"],
            working_memory_bytes=run_row["working_memory_bytes"],
            action_trace_count=len(step_rows),
            final_text=run_row["final_text"],
            started_at=run_row["started_at"],
            finished_at=run_row["finished_at"],
            evidence_index_id=_row_value(run_row, "evidence_index_id"),
            exploration_status=_row_value(run_row, "exploration_status"),
            evidence_gap=_json_text_tuple(_row_value(run_row, "evidence_gap")),
            page_timeline=_json_text_tuple(_row_value(run_row, "page_timeline")),
            steps=[
                AgentRunStep(
                    step_seq=row["step_seq"],
                    phase=row["phase"],
                    thought_summary=row["thought_summary"],
                    selected_capability=row["selected_capability"],
                    capability_kind=row["capability_kind"],
                    status=row["status"],
                    latency_ms=row["latency_ms"],
                    error_code=row["error_code"],
                    error_message=row["error_message"],
                    capability_input=_json_dict(row["capability_input"]),
                    capability_output=_json_dict(row["capability_output"]),
                )
                for row in step_rows
            ],
        )

    async def finalize_missing_rule_executions(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        finished_at: datetime,
        candidate_pages: list[CandidatePageReference],
        runtime_rule_fallbacks: list[RuntimeRuleExecutionFallback],
        runtime_page_failure_fallbacks: list[RuntimePageInspectionFailureFallback],
        auth_gate: str | None = AUTH_GATE_AUTO,
    ) -> RuleExecutionFinalizationResult:
        blocked_auth_requirement = normalize_auth_gate(auth_gate)
        allowed_page_types = get_default_page_type_registry().catalog.codes
        candidate_pages = [
            page
            for page in candidate_pages
            if page.page_capture_id and page.page_type in allowed_page_types
        ]
        queried_page_in_scope = page_type_in_inspection_scope_sql(
            "task.task_config", "q.page_type"
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            inventory = await read_frozen_task_rule_inventory_on_connection(
                connection, task_id=task_id,
            )
            row = await connection.fetchrow(
                f"""
                WITH task_scope AS (
                    SELECT task.*,
                        COALESCE(
                            task.target_platform = 'app'
                            AND lower(task.task_config->>'workflow_version') = 'v2'
                            AND COALESCE(task.task_config->>'execution_mode', 'full_inspection')
                                = 'full_inspection'
                            AND task.task_config ? 'rule_inventory',
                            false
                        ) AS has_frozen_app_inventory
                    FROM qc_task task WHERE task.id = $1::uuid
                ),
                rule_facts AS (
                    SELECT task.id AS task_id, rule.id, rule.rule_code, rule.executor_type,
                           rule.severity, rule.applicable_page_types, rule.input_contract,
                           rule.judge_config, rule.channel, rule.status
                    FROM task_scope task
                    JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                    WHERE NOT task.has_frozen_app_inventory
                    UNION ALL
                    -- 新 APP 只消费已校验的冻结契约，不能借收尾改用后续变更的 live rule。
                    SELECT task.id, frozen.rule_id::uuid, frozen.rule_code,
                           frozen.executor_type, frozen.severity, frozen.applicable_page_types,
                           frozen.input_contract, frozen.judge_config, frozen.channel,
                           'enabled'::varchar
                    FROM task_scope task
                    CROSS JOIN jsonb_to_recordset($9::jsonb) AS frozen(
                        rule_id text, rule_code varchar, executor_type varchar, severity varchar,
                        applicable_page_types jsonb, input_contract jsonb, judge_config jsonb,
                        channel varchar, applicability varchar
                    )
                    WHERE task.has_frozen_app_inventory
                      AND frozen.applicability = 'applicable'
                ),
                capture_facts AS (
                    SELECT
                        common_capture.id,
                        common_capture.task_id,
                        common_capture.agent_run_id,
                        COALESCE(
                            page_capture.inspection_lifecycle_status,
                            common_capture.metadata->>'inspection_lifecycle_status',
                            'active'
                        ) AS inspection_lifecycle_status
                    FROM qc_capture common_capture
                    LEFT JOIN qc_page_capture page_capture
                      ON page_capture.id = common_capture.id
                ),
                runtime_queried AS (
                    -- 优先使用本次 Worker 内存轨迹，避免依赖实时动作留痕的写入时序
                    SELECT DISTINCT page_capture_id, page_type, candidate_rule_codes
                    FROM jsonb_to_recordset($4::jsonb) AS item(
                        page_capture_id text,
                        page_type varchar(64),
                        candidate_rule_codes jsonb
                    )
                    WHERE page_capture_id IS NOT NULL
                      AND page_type = ANY($5::varchar[])
                ),
                runtime_rule_fallbacks AS (
                    SELECT DISTINCT ON (page_capture_id, rule_id, rule_code)
                        page_capture_id,
                        rule_id,
                        rule_code,
                        execution_status,
                        result_summary,
                        problem_desc,
                        reason
                    FROM jsonb_to_recordset($6::jsonb) AS item(
                        page_capture_id text,
                        rule_id text,
                        rule_code varchar,
                        execution_status varchar,
                        result_summary varchar,
                        problem_desc varchar,
                        reason varchar
                    )
                    WHERE execution_status IN ('error', 'skipped')
                      AND page_capture_id IS NOT NULL
                      AND (rule_id IS NOT NULL OR rule_code IS NOT NULL)
                    ORDER BY page_capture_id, rule_id, rule_code
                ),
                runtime_page_failure_fallbacks AS (
                    SELECT DISTINCT ON (page_capture_id)
                        page_capture_id,
                        page_type,
                        failure_code,
                        failure_message
                    FROM jsonb_to_recordset($7::jsonb) AS item(
                        page_capture_id text,
                        page_type varchar(64),
                        failure_code varchar,
                        failure_message varchar
                    )
                    WHERE page_capture_id IS NOT NULL
                      AND page_type = ANY($5::varchar[])
                    ORDER BY page_capture_id
                ),
                terminated_queried AS (
                    -- 候选查询已发起但被运行预算终止：该页规则属于“未执行”，不能回落成“不适用”
                    SELECT DISTINCT
                        trace.capability_input->>'page_capture_id' AS page_capture_id,
                        trace.capability_input->>'page_type' AS page_type,
                        NULL::jsonb AS candidate_rule_codes
                    FROM qc_agent_action_trace trace
                    WHERE trace.task_id = $1::uuid
                      AND trace.agent_run_id = $2::uuid
                      AND trace.selected_capability = 'candidate_rules_tool'
                      AND trace.status = 'terminated'
                      AND trace.capability_input ? 'page_capture_id'
                      AND trace.capability_input ? 'page_type'
                      AND trace.capability_input->>'page_type' = ANY($5::varchar[])
                ),
                persisted_queried AS (
                    -- 已落库动作留痕用于任务恢复和历史兼容
                    SELECT DISTINCT
                        trace.capability_input->>'page_capture_id' AS page_capture_id,
                        trace.capability_input->>'page_type' AS page_type,
                        NULL::jsonb AS candidate_rule_codes
                    FROM qc_agent_action_trace trace
                    WHERE trace.task_id = $1::uuid
                      AND trace.agent_run_id = $2::uuid
                      AND trace.selected_capability = 'candidate_rules_tool'
                      AND trace.status = 'ok'
                      AND trace.capability_input ? 'page_capture_id'
                      AND trace.capability_input ? 'page_type'
                      AND trace.capability_input->>'page_type' = ANY($5::varchar[])
                ),
                formal_v2_queried AS (
                    -- V2 以正式页面类型评估作为规则质检已启动的权威事实。
                    SELECT DISTINCT
                        assessment.page_capture_id::text AS page_capture_id,
                        assessment.page_type,
                        NULL::jsonb AS candidate_rule_codes
                    FROM qc_h5_page_type_assessment assessment
                    JOIN qc_task task ON task.id = assessment.task_id
                    WHERE assessment.task_id = $1::uuid
                      AND assessment.agent_run_id = $2::uuid
                      AND assessment.status = 'covered'
                      AND assessment.page_type = ANY($5::varchar[])
                      AND task.target_platform IN ('h5', 'web')
                      AND task.task_config->>'workflow_version' = 'v2'
                ),
                queried AS (
                    SELECT page_capture_id, page_type, candidate_rule_codes
                    FROM terminated_queried
                    UNION ALL
                    SELECT page_capture_id, page_type, candidate_rule_codes
                    FROM runtime_queried runtime
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM terminated_queried terminated
                        WHERE terminated.page_capture_id = runtime.page_capture_id
                          AND terminated.page_type = runtime.page_type
                    )
                    UNION ALL
                    SELECT page_capture_id, page_type, candidate_rule_codes
                    FROM persisted_queried persisted
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM terminated_queried terminated
                        WHERE terminated.page_capture_id = persisted.page_capture_id
                          AND terminated.page_type = persisted.page_type
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM runtime_queried runtime
                        WHERE runtime.page_capture_id = persisted.page_capture_id
                          AND runtime.page_type = persisted.page_type
                    )
                    UNION ALL
                    SELECT page_capture_id, page_type, candidate_rule_codes
                    FROM formal_v2_queried formal
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM terminated_queried terminated
                        WHERE terminated.page_capture_id = formal.page_capture_id
                          AND terminated.page_type = formal.page_type
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM runtime_queried runtime
                        WHERE runtime.page_capture_id = formal.page_capture_id
                          AND runtime.page_type = formal.page_type
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM persisted_queried persisted
                        WHERE persisted.page_capture_id = formal.page_capture_id
                          AND persisted.page_type = formal.page_type
                    )
                    UNION ALL
                    SELECT page_capture_id, page_type, NULL::jsonb
                    FROM runtime_page_failure_fallbacks failure
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM terminated_queried terminated
                        WHERE terminated.page_capture_id = failure.page_capture_id
                          AND terminated.page_type = failure.page_type
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM runtime_queried runtime
                        WHERE runtime.page_capture_id = failure.page_capture_id
                          AND runtime.page_type = failure.page_type
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM formal_v2_queried formal
                        WHERE formal.page_capture_id = failure.page_capture_id
                          AND formal.page_type = failure.page_type
                    )
                ),
                queried_summary AS (
                    SELECT
                        count(*) AS query_count,
                        string_agg(DISTINCT '「' || page_type || '」', '、') AS page_type_list
                    FROM queried q
                    -- 补记说明里只提质检范围内的页面
                    JOIN qc_task task ON task.id = $1::uuid
                    WHERE {queried_page_in_scope}
                ),
                legacy_candidate_bootstrap AS (
                    DELETE FROM qc_rule_execution bootstrap
                    WHERE bootstrap.task_id = $1::uuid
                      AND bootstrap.agent_run_id = $2::uuid
                      AND bootstrap.page_capture_id IS NULL
                      AND bootstrap.judge_input->>'bootstrap' = 'true'
                      AND bootstrap.judge_input->>'candidate_matched' = 'true'
                    RETURNING id
                ),
                candidate_gaps AS (
                    SELECT
                        task.id AS task_id,
                        rule.id AS rule_id,
                        capture.id AS page_capture_id,
                        rule.rule_code,
                        rule.executor_type,
                        rule.severity,
                        q.page_type AS matched_page_type,
                        fallback.execution_status AS runtime_execution_status,
                        fallback.result_summary AS runtime_result_summary,
                        fallback.problem_desc AS runtime_problem_desc,
                        fallback.reason AS runtime_execution_reason,
                        page_failure.failure_code AS runtime_page_failure_code,
                        page_failure.failure_message AS runtime_page_failure_message,
                        true AS is_candidate,
                        (
                            NOT task.has_frozen_app_inventory
                            AND $8::text IS NOT NULL
                            AND rule.input_contract->>'auth_requirement' = $8::text
                        ) AS auth_requirement_blocked
                    FROM task_scope task
                    JOIN rule_facts rule ON rule.task_id = task.id
                    JOIN queried q
                      ON rule.applicable_page_types ? q.page_type
                     AND (
                        q.candidate_rule_codes IS NULL
                        OR q.candidate_rule_codes ? rule.rule_code
                     )
                    JOIN capture_facts capture
                      ON capture.id::text = q.page_capture_id
                     AND capture.task_id = task.id
                     AND capture.agent_run_id = $2::uuid
                     AND COALESCE(capture.inspection_lifecycle_status, 'active') = 'active'
                    LEFT JOIN runtime_rule_fallbacks fallback
                      ON fallback.page_capture_id = capture.id::text
                     AND (
                        fallback.rule_id = rule.id::text
                        OR (
                            fallback.rule_id IS NULL
                            AND fallback.rule_code = rule.rule_code
                        )
                     )
                    LEFT JOIN runtime_page_failure_fallbacks page_failure
                      ON page_failure.page_capture_id = capture.id::text
                    WHERE task.id = $1::uuid
                      AND rule.status = 'enabled'
                      AND (rule.channel IS NULL OR rule.channel = task.channel)
                      -- 整任务判一次的聚合规则由聚合发布单独收口，不按页面补「候选没执行」占位
                      -- （09-22 本地黑料网：官方群入口、首页弹窗连续在每个首页各多一条「未执行」）
                      AND COALESCE(
                            rule.judge_config->'capability_config'->>'evidence_input_scope',
                            'page'
                          ) = 'page'
                      -- 经过的页面不判规则，也不补「候选没执行」占位
                      AND {queried_page_in_scope}
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_rule_execution existing
                          WHERE existing.task_id = task.id
                            AND existing.agent_run_id = $2::uuid
                            AND existing.rule_id = rule.id
                            AND existing.page_capture_id = capture.id
                      )
                ),
                non_candidate_gaps AS (
                    SELECT
                        task.id AS task_id,
                        rule.id AS rule_id,
                        NULL::uuid AS page_capture_id,
                        rule.rule_code,
                        rule.executor_type,
                        rule.severity,
                        NULL::varchar AS matched_page_type,
                        NULL::varchar AS runtime_execution_status,
                        NULL::varchar AS runtime_result_summary,
                        NULL::varchar AS runtime_problem_desc,
                        NULL::varchar AS runtime_execution_reason,
                        NULL::varchar AS runtime_page_failure_code,
                        NULL::varchar AS runtime_page_failure_message,
                        false AS is_candidate,
                        (
                            NOT task.has_frozen_app_inventory
                            AND $8::text IS NOT NULL
                            AND rule.input_contract->>'auth_requirement' = $8::text
                        ) AS auth_requirement_blocked
                    FROM task_scope task
                    JOIN rule_facts rule ON rule.task_id = task.id
                    WHERE task.id = $1::uuid
                      -- 全清单读侧已展示真实未执行项，不生成没有页面与候选依据的执行占位。
                      AND NOT task.has_frozen_app_inventory
                      AND rule.status = 'enabled'
                      AND (rule.channel IS NULL OR rule.channel = task.channel)
                      -- 只挂在质检范围外页面类型上的规则不进这次任务，不补「没匹配到」占位
                      AND {rule_in_inspection_scope_sql("task.task_config", "rule")}
                      AND NOT EXISTS (
                          SELECT 1
                          FROM queried q
                          WHERE rule.applicable_page_types ? q.page_type
                            AND {queried_page_in_scope}
                            AND (
                                q.candidate_rule_codes IS NULL
                                OR q.candidate_rule_codes ? rule.rule_code
                            )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_rule_execution existing
                          WHERE existing.task_id = task.id
                            AND existing.agent_run_id = $2::uuid
                            AND existing.rule_id = rule.id
                      )
                ),
                missing_rules AS (
                    SELECT * FROM candidate_gaps
                    UNION ALL
                    SELECT * FROM non_candidate_gaps
                ),
                inserted AS (
                    INSERT INTO qc_rule_execution (
                        task_id,
                        rule_id,
                        page_capture_id,
                        agent_run_id,
                        execution_status,
                        judge_strategy,
                        severity,
                        result_summary,
                        problem_desc,
                        review_status,
                        review_reason,
                        judge_input,
                        judge_output,
                        started_at,
                        finished_at
                    )
                    SELECT
                        task_id,
                        rule_id,
                        page_capture_id,
                        $2::uuid,
                        CASE
                            WHEN runtime_execution_status = 'error' THEN 'error'
                            WHEN runtime_execution_status = 'skipped' THEN 'skipped'
                            WHEN runtime_page_failure_code IS NOT NULL THEN 'error'
                            WHEN is_candidate THEN 'skipped'
                            ELSE 'skipped'
                        END,
                        executor_type,
                        severity,
                        CASE
                            WHEN runtime_execution_status = 'error' THEN coalesce(
                                runtime_result_summary,
                                '规则写回失败，已按页面级终态隔离。'
                            )
                            WHEN runtime_execution_status = 'skipped' THEN coalesce(
                                runtime_result_summary,
                                '任务预算或护栏终止，规则未执行'
                            )
                            WHEN runtime_page_failure_code IS NOT NULL THEN
                                '页面失败，规则未能完成判定。'
                            WHEN auth_requirement_blocked THEN CASE $8::text
                                WHEN 'authenticated' THEN
                                    '该规则需要登录后才能检查，本次为未登录（游客）浏览，不适用'
                                ELSE '该规则只在未登录状态下有意义，本次为已登录浏览，不适用'
                            END
                            WHEN is_candidate THEN '规则已匹配但任务结束前未执行判定'
                            ELSE CASE
                                WHEN (SELECT query_count FROM queried_summary) = 0 THEN
                                    '页面探索未启动，规则未进入判定'
                                ELSE '本次运行未匹配该规则，收尾补记为跳过'
                            END
                        END,
                        CASE
                            WHEN runtime_execution_status = 'error' THEN coalesce(
                                runtime_problem_desc,
                                '运行时规则写回失败，收尾持久化页面级 error 终态。'
                            )
                            WHEN runtime_execution_status = 'skipped' THEN coalesce(
                                runtime_problem_desc,
                                '任务预算或护栏终止，规则未形成自动判定'
                            )
                            WHEN runtime_page_failure_code IS NOT NULL THEN coalesce(
                                runtime_page_failure_message,
                                '页面在规则匹配完成前失败，规则无法继续判定。'
                            )
                            WHEN auth_requirement_blocked THEN NULL
                            WHEN is_candidate THEN
                                '规则已进入页面候选清单（page_capture_id='
                                || page_capture_id::text
                                || '，page_type='
                                || coalesce(matched_page_type, '未识别')
                                || '），但运行结束前 Agent 未完成判定与写回'
                            ELSE
                                CASE
                                    WHEN (SELECT query_count FROM queried_summary) = 0 THEN
                                        '本次运行没有探索到该规则适用的页面，规则未进入判定'
                                    ELSE
                                        '本次运行匹配到的页面类型为 '
                                        || coalesce(
                                            (SELECT page_type_list FROM queried_summary),
                                            '（无）'
                                        )
                                        || '，该规则未进入任何页面候选清单'
                                END
                        END,
                        CASE
                            WHEN runtime_execution_status = 'error' THEN 'needs_recheck'
                            WHEN runtime_page_failure_code IS NOT NULL THEN 'needs_recheck'
                        END,
                        CASE
                            WHEN runtime_execution_status = 'error' THEN
                                '运行时已隔离规则写回失败，等待人工复核'
                            WHEN runtime_execution_status = 'skipped' THEN NULL
                            WHEN runtime_page_failure_code IS NOT NULL THEN
                                '页面在规则匹配完成前失败，等待人工复核'
                            WHEN auth_requirement_blocked THEN NULL
                            ELSE 'Agent 运行结束时未产生该规则的判定记录，收尾兜底补记'
                        END,
                        jsonb_build_object(
                            'schema_version', 'v1',
                            'task_id', task_id::text,
                            'rule_id', rule_id::text,
                            'rule_code', rule_code,
                            'agent_run_id', $2::uuid::text,
                            'page_capture_id', page_capture_id::text,
                            'page_type', matched_page_type,
                            'bootstrap',
                                runtime_execution_status IS DISTINCT FROM 'error'
                                AND runtime_page_failure_code IS NULL
                                AND NOT auth_requirement_blocked,
                            'candidate_matched', is_candidate,
                            'runtime_fallback', runtime_execution_status = 'error',
                            'runtime_termination', runtime_execution_status = 'skipped',
                            'runtime_page_failure', runtime_page_failure_code IS NOT NULL
                        ),
                        jsonb_build_object(
                            'schema_version', 'v1',
                            'status', CASE
                                WHEN runtime_execution_status = 'error' THEN 'error'
                                WHEN runtime_execution_status = 'skipped' THEN 'skipped'
                                WHEN runtime_page_failure_code IS NOT NULL THEN 'error'
                                WHEN is_candidate THEN 'skipped'
                                ELSE 'skipped'
                            END,
                            'reason', CASE
                                WHEN runtime_execution_status = 'error' THEN
                                    'runtime_rule_write_failed'
                                WHEN runtime_execution_status = 'skipped' THEN
                                    coalesce(runtime_execution_reason, 'run_budget_exhausted')
                                WHEN runtime_page_failure_code IS NOT NULL THEN
                                    'runtime_page_inspection_failed'
                                WHEN auth_requirement_blocked THEN 'auth_requirement_not_met'
                                WHEN is_candidate THEN 'candidate_not_executed'
                                ELSE CASE
                                    WHEN (SELECT query_count FROM queried_summary) = 0 THEN
                                        'exploration_not_started'
                                    ELSE 'not_matched_by_run'
                                END
                            END
                        ),
                        $3,
                        $3
                    FROM missing_rules
                    ON CONFLICT (task_id, rule_id, page_capture_id) DO NOTHING
                    RETURNING id, task_id, execution_status, severity, review_reason, judge_output
                ),
                review_items AS (
                    INSERT INTO qc_review_item (
                        rule_execution_id,
                        task_id,
                        trigger_reason,
                        status,
                        priority,
                        review_reason
                    )
                    SELECT
                        id,
                        task_id,
                        CASE
                            WHEN execution_status = 'error' THEN 'manual_flag'
                            ELSE 'evidence_insufficient'
                        END,
                        'needs_recheck',
                        CASE severity
                            WHEN 'critical' THEN 10
                            WHEN 'high' THEN 20
                            WHEN 'medium' THEN 50
                            ELSE 100
                        END,
                        review_reason
                    FROM inserted
                    WHERE execution_status IN ('evidence_insufficient', 'error')
                    ON CONFLICT (rule_execution_id) DO UPDATE SET
                        trigger_reason = EXCLUDED.trigger_reason,
                        status = EXCLUDED.status,
                        priority = EXCLUDED.priority,
                        review_reason = EXCLUDED.review_reason,
                        reviewed_at = NULL,
                        updated_at = now()
                    RETURNING id
                )
                SELECT
                    count(*)::integer AS inserted_count,
                    count(*) FILTER (
                        WHERE judge_output->>'reason' = 'candidate_not_executed'
                    )::integer AS unresolved_candidate_count
                FROM inserted
                """,
                task_id,
                agent_run_id,
                finished_at,
                _json_dumps(
                    [
                        {
                            "page_capture_id": page.page_capture_id,
                            "page_type": page.page_type,
                            "candidate_rule_codes": (
                                list(page.candidate_rule_codes)
                                if page.candidate_rule_codes is not None
                                else None
                            ),
                        }
                        for page in candidate_pages
                    ]
                ),
                list(allowed_page_types),
                _json_dumps(
                    [
                        {
                            "page_capture_id": fallback.page_capture_id,
                            "rule_id": fallback.rule_id,
                            "rule_code": fallback.rule_code,
                            "execution_status": fallback.execution_status,
                            "result_summary": fallback.result_summary,
                            "problem_desc": fallback.problem_desc,
                            "reason": fallback.reason,
                        }
                        for fallback in runtime_rule_fallbacks
                    ]
                ),
                _json_dumps(
                    [
                        {
                            "page_capture_id": fallback.page_capture_id,
                            "page_type": fallback.page_type,
                            "failure_code": fallback.failure_code,
                            "failure_message": fallback.failure_message,
                        }
                        for fallback in runtime_page_failure_fallbacks
                    ]
                ),
                blocked_auth_requirement,
                _json_dumps(inventory["rules"] if inventory is not None else []),
            )
        return RuleExecutionFinalizationResult(
            inserted_count=row["inserted_count"] if row is not None else 0,
            unresolved_candidate_count=(
                row["unresolved_candidate_count"] if row is not None else 0
            ),
        )

    async def record_rule_execution(
        self, command: RecordRuleExecutionCommand
    ) -> RecordedRuleExecution | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            from services.api.app.infrastructure.db.web_sequence_result_projection import (
                lock_web_projection_scope,
                project_web_canonical,
            )

            web_scope = await lock_web_projection_scope(connection,command)
            if command.recovery_lease_token is not None:
                await assert_finalization_recovery_fence(
                    connection,
                    task_id=command.task_id,
                    recovery_lease_token=command.recovery_lease_token,
                    recovery_generation=command.recovery_generation,
                    recovery_lease_expires_at=command.recovery_lease_expires_at,
                )
            if command.lease_token:
                await _assert_inspection_write_lease(
                    connection,
                    task_id=command.task_id,
                    work_item_id=str(command.work_item_id or ""),
                    attempt_id=str(command.attempt_id or ""),
                    lease_token=str(command.lease_token),
                    agent_run_id=command.agent_run_id,
                )
            if web_scope is not None:
                command = await project_web_canonical(connection,command,web_scope)
            inventory = await read_frozen_task_rule_inventory_on_connection(
                connection, task_id=command.task_id
            )
            if inventory is not None:
                rules = [
                    rule for rule in inventory["rules"]
                    if (command.rule_id is None or rule["rule_id"] == command.rule_id)
                    and (command.rule_code is None or rule["rule_code"] == command.rule_code)
                    and rule["applicability"] == "applicable"
                ]
                if len(rules) != 1 or (command.rule_id is None and command.rule_code is None):
                    return None
                frozen_rule = rules[0]
                # Packet 身份按冻结 rule_id 核对，不因 live rule_code 改名而失效。
                command = replace(
                    command, rule_id=frozen_rule["rule_id"], rule_code=frozen_rule["rule_code"],
                    judge_strategy=command.judge_strategy or frozen_rule["executor_type"],
                    severity=command.severity or frozen_rule["severity"],
                )
            row = await connection.fetchrow(
                """
                WITH capture_validation AS (
                    SELECT CASE
                        WHEN $20::uuid IS NOT NULL AND NOT EXISTS (
                            SELECT 1
                            FROM qc_capture capture
                            WHERE capture.id = $20::uuid
                              AND capture.task_id = $1::uuid
                        ) THEN 'page_capture_not_found'
                        ELSE NULL
                    END AS validation_error
                ),
                packet_validation AS (
                    SELECT CASE
                        WHEN capture_validation.validation_error IS NOT NULL
                            THEN capture_validation.validation_error
                        WHEN $21::uuid IS NOT NULL AND NOT EXISTS (
                            SELECT 1
                            FROM qc_observation_packet packet
                            WHERE packet.id = $21::uuid
                              AND packet.task_id = $1::uuid
                              AND packet.page_capture_id = $20::uuid
                              AND packet.created_by_agent_run_id IS NOT DISTINCT FROM $4::uuid
                              AND (
                                  packet.quality_gate_status = 'pass'
                                  OR $5::varchar IN (
                                      'evidence_insufficient',
                                      'manual_review',
                                      'error',
                                      'skipped'
                                  )
                              )
                              AND EXISTS (
                                  SELECT 1
                                  FROM qc_rule packet_rule
                                  WHERE packet_rule.id = packet.rule_id
                                    AND (
                                        ($2::uuid IS NOT NULL AND packet_rule.id = $2::uuid)
                                        OR (
                                            $3::varchar IS NOT NULL
                                            AND packet_rule.rule_code = $3::varchar
                                        )
                                    )
                              )
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM jsonb_array_elements_text($11::jsonb) evidence_id
                                  WHERE NOT packet.evidence_artifact_ids ? evidence_id.value
                              )
                        ) THEN 'observation_packet_mismatch'
                        ELSE NULL
                    END AS validation_error
                    FROM capture_validation
                ),
                evidence_validation AS (
                    SELECT CASE
                        WHEN packet_validation.validation_error IS NOT NULL
                            THEN packet_validation.validation_error
                        WHEN jsonb_array_length($11::jsonb) > 0 AND (
                            $20::uuid IS NULL
                            OR EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements_text($11::jsonb) evidence_id
                                WHERE NOT EXISTS (
                                    SELECT 1
                                    FROM qc_evidence_artifact artifact
                                    WHERE artifact.id::text = evidence_id.value
                                      AND artifact.task_id = $1::uuid
                                      AND (
                                          (
                                              $21::uuid IS NULL
                                              AND artifact.page_capture_id = $20::uuid
                                          )
                                          OR (
                                              $21::uuid IS NOT NULL
                                              AND EXISTS (
                                                  SELECT 1
                                                  FROM qc_observation_packet packet
                                                  WHERE packet.id = $21::uuid
                                                    AND packet.evidence_artifact_ids
                                                        ? artifact.id::text
                                              )
                                          )
                                      )
                                )
                            )
                        ) THEN 'evidence_artifact_mismatch'
                        ELSE NULL
                    END AS validation_error
                    FROM packet_validation
                ),
                target_rule AS (
                    __RULE_EXECUTION_TARGET_SQL__
                ),
                reuse_validation AS (
                    SELECT CASE
                        WHEN evidence_validation.validation_error IS NOT NULL
                            THEN evidence_validation.validation_error
                        WHEN jsonb_array_length($11::jsonb) = 0
                          AND $5::varchar IN ('pass', 'fail', 'warning')
                          AND NOT EXISTS (
                              SELECT 1
                              FROM qc_rule_execution AS source
                              WHERE source.id::text = $13::jsonb->'reuse'->>'source_execution_id'
                                AND source.task_id = $1::uuid
                                AND source.rule_id = target_rule.rule_id
                                AND source.page_capture_id::text
                                      = $13::jsonb->'reuse'->>'source_page_capture_id'
                                AND source.execution_status = $5::varchar
                                AND source.execution_status
                                      = $13::jsonb->'reuse'->>'source_execution_status'
                                AND $13::jsonb->'reuse'->>'protocol_version' = 'rule-reuse.v1'
                                AND $13::jsonb->'reuse'->>'scope' IN (
                                    'per_task', 'same_object', 'same_material'
                                )
                                AND COALESCE($13::jsonb->'reuse'->>'reuse_key', '') <> ''
                                AND jsonb_array_length(source.evidence_artifact_ids) > 0
                                AND NOT (source.judge_output ? 'reuse')
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM jsonb_array_elements_text(
                                        source.evidence_artifact_ids
                                    ) AS evidence_id
                                    WHERE NOT EXISTS (
                                        SELECT 1
                                        FROM qc_evidence_artifact AS artifact
                                        WHERE artifact.id::text = evidence_id.value
                                          AND artifact.task_id = source.task_id
                                    )
                                )
                          ) THEN 'rule_reuse_source_mismatch'
                        ELSE NULL
                    END AS validation_error
                    FROM evidence_validation
                    CROSS JOIN target_rule
                ),
                review_suppression_validation AS (
                    SELECT CASE
                        WHEN $23::boolean THEN NULL
                        WHEN NOT EXISTS (
                            SELECT 1
                            FROM qc_web_coverage_snapshot AS snapshot
                            INNER JOIN qc_task AS task
                              ON task.id = target_rule.task_id
                            INNER JOIN qc_agent_run AS run
                              ON run.id = $4::uuid
                             AND run.task_id = task.id
                             AND run.agent_type = 'main'
                             AND run.status = 'done'
                            INNER JOIN qc_rule AS rule
                              ON rule.id = target_rule.rule_id
                             AND rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                             AND rule.status = 'enabled'
                            WHERE snapshot.task_id = target_rule.task_id
                              AND snapshot.agent_run_id = $4::uuid
                              AND snapshot.finalization_epoch = task.finalization_epoch
                              AND snapshot.id::text
                                  = $12::jsonb->>'coverage_snapshot_id'
                              AND snapshot.coverage_status = 'completed_with_gaps'
                              AND jsonb_typeof(snapshot.missing_page_types) = 'array'
                              AND snapshot.missing_page_types
                                  ? ($12::jsonb->>'missing_page_type')
                              AND task.target_platform = 'web'
                              AND LOWER(task.task_config->>'workflow_version') = 'v2'
                              AND LOWER(
                                    task.task_config #>> '{_task_target,platform}'
                                  ) = 'web'
                              AND task.status = 'running'
                              AND task.finalization_state = 'session_draining'
                              AND task.finalization_agent_run_id = $4::uuid
                              AND task.task_config->>'execution_mode'
                                  = 'full_inspection'
                              AND task.task_config->>'web_inspection_notification_mode'
                                  = 'enforced'
                              AND run.id = (
                                  SELECT latest.id
                                  FROM qc_agent_run AS latest
                                  WHERE latest.task_id = task.id
                                    AND latest.agent_type = 'main'
                                  ORDER BY latest.created_at DESC, latest.id DESC
                                  LIMIT 1
                              )
                              AND $5::varchar = 'evidence_insufficient'
                              AND $20::uuid IS NULL
                              AND $21::uuid IS NULL
                              AND jsonb_array_length($11::jsonb) = 0
                              AND $12::jsonb->>'schema_version'
                                  = 'web-coverage-gap.v1'
                              AND $12::jsonb->>'source' = 'web_coverage_gap'
                              AND $12::jsonb->>'reason'
                                  = 'required_page_type_not_discovered'
                              AND $22::varchar = (
                                  'web-coverage-gap:' || snapshot.id::text || ':'
                                  || ($12::jsonb->>'missing_page_type') || ':'
                                  || rule.rule_code
                              )
                              AND (rule.channel IS NULL OR rule.channel = task.channel)
                              AND (
                                    jsonb_typeof(
                                        rule.input_contract->'target_clients'
                                      ) IS DISTINCT FROM 'array'
                                    OR jsonb_array_length(
                                        rule.input_contract->'target_clients'
                                      ) = 0
                                    OR rule.input_contract->'target_clients' ? 'WEB'
                                  )
                              AND COALESCE(
                                    rule.input_contract->>'auth_requirement', ''
                                  ) <> 'authenticated'
                              AND jsonb_typeof(rule.applicable_page_types) = 'array'
                              AND rule.applicable_page_types
                                  ? ($12::jsonb->>'missing_page_type')
                              AND COALESCE(
                                    rule.judge_config->'capability_config'
                                        ->>'evidence_input_scope',
                                    'page'
                                  ) = 'page'
                        ) THEN 'review_suppression_not_authorized'
                        ELSE NULL
                    END AS validation_error
                    FROM target_rule
                ),
                observation_validation AS (
                    SELECT CASE
                        WHEN reuse_validation.validation_error IS NOT NULL
                            THEN reuse_validation.validation_error
                        WHEN review_suppression_validation.validation_error IS NOT NULL
                            THEN review_suppression_validation.validation_error
                        WHEN $21::uuid IS NOT NULL AND NOT EXISTS (
                            SELECT 1
                            FROM qc_observation_packet packet
                            JOIN target_rule
                              ON target_rule.rule_id = packet.rule_id
                            WHERE packet.id = $21::uuid
                              AND packet.task_id = $1::uuid
                              AND packet.page_capture_id IS NOT DISTINCT FROM $20::uuid
                              AND packet.created_by_agent_run_id IS NOT DISTINCT FROM $4::uuid
                        ) THEN 'observation_packet_mismatch'
                        ELSE NULL
                    END AS validation_error
                    FROM reuse_validation
                    CROSS JOIN review_suppression_validation
                ),
                updated AS (
                    UPDATE qc_rule_execution execution
                    SET
                        agent_run_id = $4::uuid,
                        observation_packet_id = $21::uuid,
                        execution_status = $5,
                        judge_strategy = COALESCE($6::varchar, target_rule.executor_type),
                        confidence = $7,
                        severity = COALESCE($8::varchar, target_rule.severity),
                        result_summary = $9,
                        problem_desc = $10,
                        evidence_artifact_ids = $11::jsonb,
                        judge_input = $12::jsonb,
                        judge_output = $13::jsonb,
                        review_status = $14,
                        review_reason = $15,
                        error_code = $16,
                        error_message = $17,
                        started_at = COALESCE($18, execution.started_at),
                        finished_at = $19,
                        updated_at = now()
                    FROM target_rule
                    CROSS JOIN observation_validation
                    WHERE execution.task_id = target_rule.task_id
                      AND execution.rule_id = target_rule.rule_id
                      AND (
                          (
                              $22::varchar IS NOT NULL
                              AND execution.source_idempotency_key = $22::varchar
                          )
                          OR (
                              $22::varchar IS NULL
                              AND execution.source_idempotency_key IS NULL
                              AND execution.page_capture_id
                                  IS NOT DISTINCT FROM $20::uuid
                          )
                      )
                      AND observation_validation.validation_error IS NULL
                    RETURNING
                        execution.id::text AS execution_id,
                        execution.task_id,
                        target_rule.rule_id::text AS rule_id,
                        target_rule.rule_code,
                        execution.execution_status,
                        execution.page_capture_id::text AS page_capture_id
                ),
                source_inserted AS (
                    INSERT INTO qc_rule_execution (
                        task_id,
                        rule_id,
                        page_capture_id,
                        source_idempotency_key,
                        observation_packet_id,
                        agent_run_id,
                        execution_status,
                        judge_strategy,
                        confidence,
                        severity,
                        result_summary,
                        problem_desc,
                        evidence_artifact_ids,
                        judge_input,
                        judge_output,
                        review_status,
                        review_reason,
                        error_code,
                        error_message,
                        started_at,
                        finished_at
                    )
                    SELECT
                        target_rule.task_id,
                        target_rule.rule_id,
                        $20::uuid,
                        $22::varchar,
                        $21::uuid,
                        $4::uuid,
                        $5,
                        COALESCE($6::varchar, target_rule.executor_type),
                        $7,
                        COALESCE($8::varchar, target_rule.severity),
                        $9,
                        $10,
                        $11::jsonb,
                        $12::jsonb,
                        $13::jsonb,
                        $14,
                        $15,
                        $16,
                        $17,
                        $18,
                        $19
                    FROM target_rule
                    CROSS JOIN observation_validation
                    WHERE observation_validation.validation_error IS NULL
                      AND $22::varchar IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM updated)
                    ON CONFLICT (task_id, rule_id, source_idempotency_key)
                    WHERE source_idempotency_key IS NOT NULL
                    DO UPDATE SET
                        agent_run_id = EXCLUDED.agent_run_id,
                        page_capture_id = EXCLUDED.page_capture_id,
                        observation_packet_id = EXCLUDED.observation_packet_id,
                        execution_status = EXCLUDED.execution_status,
                        judge_strategy = EXCLUDED.judge_strategy,
                        confidence = EXCLUDED.confidence,
                        severity = EXCLUDED.severity,
                        result_summary = EXCLUDED.result_summary,
                        problem_desc = EXCLUDED.problem_desc,
                        evidence_artifact_ids = EXCLUDED.evidence_artifact_ids,
                        judge_input = EXCLUDED.judge_input,
                        judge_output = EXCLUDED.judge_output,
                        review_status = EXCLUDED.review_status,
                        review_reason = EXCLUDED.review_reason,
                        error_code = EXCLUDED.error_code,
                        error_message = EXCLUDED.error_message,
                        started_at = COALESCE(EXCLUDED.started_at, qc_rule_execution.started_at),
                        finished_at = EXCLUDED.finished_at,
                        updated_at = now()
                    RETURNING
                        id::text AS execution_id,
                        task_id,
                        rule_id::text AS rule_id,
                        (SELECT rule_code FROM target_rule) AS rule_code,
                        execution_status,
                        page_capture_id::text AS page_capture_id
                ),
                legacy_inserted AS (
                    INSERT INTO qc_rule_execution (
                        task_id,
                        rule_id,
                        page_capture_id,
                        source_idempotency_key,
                        observation_packet_id,
                        agent_run_id,
                        execution_status,
                        judge_strategy,
                        confidence,
                        severity,
                        result_summary,
                        problem_desc,
                        evidence_artifact_ids,
                        judge_input,
                        judge_output,
                        review_status,
                        review_reason,
                        error_code,
                        error_message,
                        started_at,
                        finished_at
                    )
                    SELECT
                        target_rule.task_id,
                        target_rule.rule_id,
                        $20::uuid,
                        NULL::varchar,
                        $21::uuid,
                        $4::uuid,
                        $5,
                        COALESCE($6::varchar, target_rule.executor_type),
                        $7,
                        COALESCE($8::varchar, target_rule.severity),
                        $9,
                        $10,
                        $11::jsonb,
                        $12::jsonb,
                        $13::jsonb,
                        $14,
                        $15,
                        $16,
                        $17,
                        $18,
                        $19
                    FROM target_rule
                    CROSS JOIN observation_validation
                    WHERE observation_validation.validation_error IS NULL
                      AND $22::varchar IS NULL
                      AND NOT EXISTS (SELECT 1 FROM updated)
                    ON CONFLICT (task_id, rule_id, page_capture_id) DO UPDATE SET
                        agent_run_id = EXCLUDED.agent_run_id,
                        observation_packet_id = EXCLUDED.observation_packet_id,
                        execution_status = EXCLUDED.execution_status,
                        judge_strategy = EXCLUDED.judge_strategy,
                        confidence = EXCLUDED.confidence,
                        severity = EXCLUDED.severity,
                        result_summary = EXCLUDED.result_summary,
                        problem_desc = EXCLUDED.problem_desc,
                        evidence_artifact_ids = EXCLUDED.evidence_artifact_ids,
                        judge_input = EXCLUDED.judge_input,
                        judge_output = EXCLUDED.judge_output,
                        review_status = EXCLUDED.review_status,
                        review_reason = EXCLUDED.review_reason,
                        error_code = EXCLUDED.error_code,
                        error_message = EXCLUDED.error_message,
                        started_at = COALESCE(EXCLUDED.started_at, qc_rule_execution.started_at),
                        finished_at = EXCLUDED.finished_at,
                        updated_at = now()
                    RETURNING
                        id::text AS execution_id,
                        task_id,
                        rule_id::text AS rule_id,
                        (SELECT rule_code FROM target_rule) AS rule_code,
                        execution_status,
                        page_capture_id::text AS page_capture_id
                ),
                written AS (
                    SELECT * FROM updated
                    UNION ALL
                    SELECT * FROM source_inserted
                    UNION ALL
                    SELECT * FROM legacy_inserted
                    LIMIT 1
                ),
                review_item AS (
                    INSERT INTO qc_review_item (
                        rule_execution_id,
                        task_id,
                        trigger_reason,
                        status,
                        priority,
                        review_reason
                    )
                    SELECT
                        written.execution_id::uuid,
                        written.task_id,
                        CASE
                            WHEN $5 = 'evidence_insufficient' THEN 'evidence_insufficient'
                            WHEN $7 IS NOT NULL AND $7 < 0.75 THEN 'low_confidence'
                            WHEN COALESCE($8::varchar, target_rule.severity) IN ('critical', 'high')
                                THEN 'high_risk'
                            ELSE 'manual_flag'
                        END,
                        CASE
                            WHEN $14::varchar = 'needs_recheck' THEN 'needs_recheck'
                            ELSE 'pending'
                        END,
                        CASE COALESCE($8::varchar, target_rule.severity)
                            WHEN 'critical' THEN 10
                            WHEN 'high' THEN 20
                            WHEN 'medium' THEN 50
                            ELSE 100
                        END,
                        $15
                    FROM written
                    CROSS JOIN target_rule
                    WHERE (
                        $5 IN ('manual_review', 'evidence_insufficient')
                        OR $14::varchar IN ('pending', 'needs_recheck')
                    )
                      AND $23::boolean
                    ON CONFLICT (rule_execution_id) DO UPDATE SET
                        trigger_reason = EXCLUDED.trigger_reason,
                        status = EXCLUDED.status,
                        priority = EXCLUDED.priority,
                        review_reason = EXCLUDED.review_reason,
                        reviewed_at = NULL,
                        updated_at = now()
                    RETURNING id
                )
                SELECT
                    execution_id,
                    rule_id,
                    rule_code,
                    execution_status,
                    page_capture_id,
                    NULL::varchar AS validation_error
                FROM written
                UNION ALL
                SELECT
                    NULL::text AS execution_id,
                    NULL::text AS rule_id,
                    NULL::varchar AS rule_code,
                    NULL::varchar AS execution_status,
                    $20::uuid::text AS page_capture_id,
                    validation_error
                FROM observation_validation
                WHERE validation_error IS NOT NULL
                LIMIT 1
                """.replace(
                    "__RULE_EXECUTION_TARGET_SQL__",
                    rule_execution_target_sql(frozen=inventory is not None),
                ),
                command.task_id,
                command.rule_id,
                command.rule_code,
                command.agent_run_id,
                command.execution_status,
                command.judge_strategy,
                command.confidence,
                command.severity,
                command.result_summary,
                command.problem_desc,
                _json_dumps(command.evidence_artifact_ids),
                _json_dumps(command.judge_input),
                _json_dumps(command.judge_output),
                command.review_status,
                command.review_reason,
                command.error_code,
                command.error_message,
                command.started_at,
                command.finished_at,
                command.page_capture_id,
                command.observation_packet_id,
                command.source_idempotency_key,
                command.create_review_item,
            )
        if row is None:
            return None
        validation_error = row.get("validation_error")
        if validation_error == "page_capture_not_found":
            raise RuleExecutionValidationError("页面采集记录不属于当前任务")
        if validation_error == "evidence_artifact_mismatch":
            raise RuleExecutionValidationError("证据附件不属于指定页面采集")
        if validation_error == "rule_reuse_source_mismatch":
            raise RuleExecutionValidationError("规则复用来源执行或证据链不可信")
        if validation_error == "observation_packet_mismatch":
            raise RuleExecutionValidationError("Observation Packet 不属于指定任务、规则或页面采集")
        if validation_error == "review_suppression_not_authorized":
            raise RuleExecutionValidationError(
                "Web Coverage Gap 免 Review 身份未通过数据库权威校验"
            )
        return RecordedRuleExecution(
            execution_id=row["execution_id"],
            rule_id=row["rule_id"],
            rule_code=row["rule_code"],
            execution_status=row["execution_status"],
            page_capture_id=row["page_capture_id"],
            quality_outcome=derive_quality_outcome(str(row["execution_status"])),
        )

    async def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


async def _assert_inspection_write_lease(
    connection: Any,
    *,
    task_id: str,
    work_item_id: str,
    attempt_id: str,
    lease_token: str,
    agent_run_id: str | None,
) -> None:
    """同事务锁定有效 Work Item／Attempt；旧 lease 禁止改写正式规则结果。"""
    row = await connection.fetchrow(
        """
        SELECT work_item.id::text AS work_item_id
        FROM qc_inspection_work_item AS work_item
        INNER JOIN qc_inspection_attempt AS attempt
            ON attempt.id = $3::uuid
           AND attempt.work_item_id = work_item.id
           AND attempt.status IN ('running', 'judged')
           AND attempt.lease_token = $4::uuid
        WHERE work_item.id = $1::uuid
          AND work_item.task_id = $2::uuid
          AND work_item.control_mode = 'enforced'
          AND work_item.status = 'running'
          AND work_item.lease_token = $4::uuid
          AND work_item.lease_expires_at > now()
          AND (
              $5::uuid IS NULL
              OR work_item.consumer_agent_run_id IS NOT DISTINCT FROM $5::uuid
          )
        FOR UPDATE OF work_item, attempt
        """,
        work_item_id,
        task_id,
        attempt_id,
        lease_token,
        agent_run_id,
    )
    if row is None:
        raise RuleExecutionLeaseConflictError(
            "工作项租约已失效或 Attempt 不匹配，拒绝写回正式规则结果"
        )


async def _insert_task_on_connection(
    connection: Any,
    *,
    access_scope: AccessScope,
    target: TaskTarget,
    ruleset_code: str,
    ruleset_version: int,
    channel: str,
    priority: int,
    task_config: dict,
    page_sampling_policy: dict,
    loop_guard: dict,
) -> Any:
    """复用调用方事务写入任务；规则集策略与 App 制品权限仍由 SQL 锁定。"""

    task_config = await freeze_task_rule_inventory_on_connection(
        connection,
        target_platform=target.platform.value,
        ruleset_code=ruleset_code,
        ruleset_version=ruleset_version,
        channel=channel,
        task_config=task_config,
    )
    if target.platform is TaskTargetPlatform.H5 and task_config.get("h5_shortflows"):
        from services.api.app.infrastructure.db.h5_shortflow_activation import (
            freeze_h5_shortflows_on_connection,
        )

        task_config = await freeze_h5_shortflows_on_connection(
            connection, task_config=task_config,
        )
    return await connection.fetchrow(
        """
        WITH selected_ruleset AS (
            SELECT
                ruleset.id,
                ruleset.page_sampling_policy,
                ruleset.loop_guard,
                COALESCE(
                    (
                        SELECT jsonb_agg(
                            DISTINCT applicable.page_type
                            ORDER BY applicable.page_type
                        )
                        FROM qc_rule AS rule
                        CROSS JOIN LATERAL jsonb_array_elements_text(
                            rule.applicable_page_types
                        ) AS applicable(page_type)
                        WHERE rule.ruleset_id = ANY(qc_ruleset_scope(ruleset.id))
                          AND rule.status = 'enabled'
                          -- 选了质检范围时只算范围内的页面类型
                          AND (
                              COALESCE(
                                  jsonb_array_length($7::jsonb #> '{inspection_scope,page_types}'),
                                  0
                              ) = 0
                              OR $7::jsonb #> '{inspection_scope,page_types}'
                                  ? applicable.page_type
                          )
                    ),
                    '[]'::jsonb
                ) AS rule_target_page_types,
                (
                    SELECT count(*)
                    FROM qc_rule AS rule
                    WHERE rule.ruleset_id = ANY(qc_ruleset_scope(ruleset.id))
                      AND rule.status = 'enabled'
                ) AS enabled_rule_total
            FROM qc_ruleset AS ruleset
            WHERE ruleset.ruleset_code = $1
              AND ruleset.version = $2
              AND ruleset.status = 'enabled'
        ),
        selected_build AS (
            SELECT build.id
            FROM qc_platform_build AS build
            CROSS JOIN LATERAL (
                SELECT REPLACE(
                    $7::jsonb #>> '{app,platform_build_ref}',
                    'app-build:',
                    ''
                ) AS build_id
            ) AS ref
            -- asyncpg：$3 同时与 text 字面量比较并写入 varchar，需显式转型。
            WHERE $3::varchar = 'app'
              AND build.id = CASE
                  WHEN ref.build_id ~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
                  THEN ref.build_id::uuid
                  ELSE NULL
              END
              AND build.platform = 'android'
              AND build.status = 'verified'
              AND (
                  $12::boolean
                  OR (
                      build.tenant_id = $11::uuid
                      AND build.created_by = $10::uuid
                  )
              )
        )
        INSERT INTO qc_task (
            target_platform,
            target_url,
            ruleset_id,
            channel,
            status,
            priority,
            submitter_id,
            tenant_id,
            task_config,
            page_sampling_policy,
            loop_guard,
            department_name
        )
        SELECT
            $3::varchar,
            $4::text,
            id,
            $5::varchar,
            'queued',
            $6,
            $10::uuid,
            $11::uuid,
            -- 记下建任务时规则集里启用的规则条数：报告「规则总数」用它，之后规则再启停也不变
            $7::jsonb || jsonb_build_object(
                'ruleset_rule_total', COALESCE(
                    ($7::jsonb #>> '{rule_inventory,rule_count}')::integer,
                    enabled_rule_total
                )
            ),
            (page_sampling_policy || $8::jsonb)
                || jsonb_build_object(
                    'rule_target_page_types',
                    COALESCE(
                        $7::jsonb #> '{rule_inventory,target_page_types}',
                        rule_target_page_types
                    )
                ),
            jsonb_strip_nulls(loop_guard || $9::jsonb),
            NULLIF(NULLIF(btrim($7::jsonb->'product'->>'department_name'), ''), '-')
        FROM selected_ruleset
        WHERE $3::varchar <> 'app' OR EXISTS (SELECT 1 FROM selected_build)
        RETURNING
            id::text AS task_id,
            ruleset_id::text AS ruleset_id,
            target_platform,
            target_url,
            status,
            task_config,
            page_sampling_policy,
            loop_guard
        """,
        ruleset_code,
        ruleset_version,
        target.platform.value,
        target.target_url,
        channel,
        priority,
        _json_dumps(task_config),
        _json_dumps(page_sampling_policy),
        _json_dumps(loop_guard),
        access_scope.actor_user_id,
        access_scope.tenant_id,
        access_scope.can_write_any,
    )


def _candidate_rule_from_inventory(row: dict[str, Any]) -> CandidateRule:
    """冻结契约沿用候选接口，额外应用台账字段不进入判定输入。"""

    return CandidateRule(
        rule_id=row["rule_id"], rule_code=row["rule_code"], name=row["name"],
        description=row.get("description"), category=row["category"], severity=row["severity"],
        executor_type=row["executor_type"], scope=row.get("scope"), page_type=row.get("page_type"),
        applicable_page_types=row.get("applicable_page_types") or [],
        url_pattern=row.get("url_pattern"), steps=row.get("steps") or [],
        input_contract=row.get("input_contract") or {}, check_point=row.get("check_point"),
        judge_config=row.get("judge_config") or {},
        manual_review_policy=row.get("manual_review_policy") or {},
        sort_order=row.get("sort_order") or 0, match_reason=row["match_reason"],
    )


def _rule_judge_definition_from_inventory(row: dict[str, Any]) -> RuleJudgeDefinition:
    """正式判定使用同一冻结规则，后续启停与编辑不能改变任务合同。"""

    return RuleJudgeDefinition(
        rule_id=row["rule_id"], rule_code=row["rule_code"], executor_type=row["executor_type"],
        judge_config=row.get("judge_config") or {}, rule_name=row.get("name") or row["rule_code"],
        category=row.get("category") or "", severity=row.get("severity") or "low",
        check_point=row.get("check_point") or "", steps=row.get("steps") or [],
        input_contract=row.get("input_contract") or {},
        manual_review_policy=row.get("manual_review_policy") or {},
    )


def _created_task_from_row(
    row: Any,
    *,
    target: TaskTarget,
    ruleset_code: str,
    ruleset_version: int,
) -> CreatedTask:
    if row is None:
        if target.platform is TaskTargetPlatform.APP:
            raise TaskRulesetNotFoundError("未找到启用规则集或已验证的 App 构建制品")
        raise TaskRulesetNotFoundError(
            f"未找到启用规则集：ruleset_code={ruleset_code}, version={ruleset_version}"
        )
    return CreatedTask(
        task_id=str(row["task_id"]),
        ruleset_id=str(row["ruleset_id"]),
        target=target,
        status=str(row["status"]),
        task_config=_json_dict(row.get("task_config")),
        page_sampling_policy=_json_dict(row.get("page_sampling_policy")),
        loop_guard=_json_dict(row.get("loop_guard")),
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _json_list(value: Any) -> list[str]:
    parsed = _json_loads(value, default=[])
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _json_array(value: Any) -> list[Any]:
    parsed = _json_loads(value, default=[])
    if not isinstance(parsed, list):
        return []
    return parsed


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = _json_loads(value, default={})
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _json_text_tuple(value: Any) -> tuple[str, ...]:
    items = _json_array(value)
    return tuple(str(item).strip() for item in items if str(item).strip())


def _exploration_text_tuple(payload: Any, field_name: str) -> tuple[str, ...]:
    parsed = _json_dict(payload)
    raw = parsed.get(field_name)
    if not isinstance(raw, list):
        return ()
    texts: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            detail = str(item.get("detail") or "").strip()
            if detail:
                texts.append(detail)
            continue
        text = str(item).strip()
        if text:
            texts.append(text)
    return tuple(texts)


def _json_loads(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value
