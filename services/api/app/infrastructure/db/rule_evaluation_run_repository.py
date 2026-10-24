"""独立规则质检运行账本的 PostgreSQL 实现。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from services.api.app.application.rule_evaluation_runs import (
    JudgeablePage,
    RuleEvaluationRun,
    RuleEvaluationRunStatus,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.pool import get_postgres_pool


class RuleEvaluationRunPersistenceError(RuntimeError):
    """独立规则质检运行账本状态推进不满足约束。"""


class PostgresRuleEvaluationRunRepository:
    """qc_rule_evaluation_run 账本仓储。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def insert_pending(
        self, run: RuleEvaluationRun, *, access_scope: AccessScope
    ) -> None:
        if run.status is not RuleEvaluationRunStatus.PENDING:
            raise RuleEvaluationRunPersistenceError("新建运行必须为 pending 状态")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            inserted = await connection.fetchval(
                """
                INSERT INTO qc_rule_evaluation_run (
                    run_id, task_id, status, requested_by, requested_rule_codes
                )
                SELECT
                    $1,
                    task.id,
                    $3,
                    $4,
                    $5::jsonb
                FROM qc_task AS task
                WHERE task.id = $2::uuid
                  AND (
                      $6::boolean
                      OR (
                          task.tenant_id = $7::uuid
                          AND task.submitter_id = $8::uuid
                      )
                  )
                ON CONFLICT (run_id) DO NOTHING
                RETURNING run_id
                """,
                run.run_id,
                run.task_id,
                run.status.value,
                access_scope.actor_user_id,
                json.dumps(list(run.requested_rule_codes), ensure_ascii=False),
                access_scope.can_write_any,
                access_scope.tenant_id,
                access_scope.actor_user_id,
            )
        if inserted is None:
            raise RuleEvaluationRunPersistenceError(
                f"运行 {run.run_id} 不存在、任务无权访问或已存在"
            )

    async def mark_running(self, *, run_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            updated = await connection.fetchval(
                """
                UPDATE qc_rule_evaluation_run
                SET
                    status = 'running',
                    started_at = now(),
                    updated_at = now(),
                    row_version = row_version + 1
                WHERE run_id = $1
                  AND status = 'pending'
                RETURNING run_id
                """,
                run_id,
            )
        if updated is None:
            raise RuleEvaluationRunPersistenceError(
                f"运行 {run_id} 不存在或已离开 pending，无法开始执行"
            )

    async def finish(
        self,
        *,
        run_id: str,
        status: RuleEvaluationRunStatus,
        result_payload: Sequence[dict[str, Any]] = (),
        page_count: int = 0,
        rule_count: int = 0,
        error_message: str | None = None,
    ) -> None:
        if not status.is_terminal:
            raise RuleEvaluationRunPersistenceError("finish 只接受终态状态")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            updated = await connection.fetchval(
                """
                UPDATE qc_rule_evaluation_run
                SET
                    status = $2,
                    result_payload = $3::jsonb,
                    page_count = $4,
                    rule_count = $5,
                    error_message = $6,
                    finished_at = now(),
                    updated_at = now(),
                    row_version = row_version + 1
                WHERE run_id = $1
                  AND status IN ('pending', 'running')
                RETURNING run_id
                """,
                run_id,
                status.value,
                json.dumps(list(result_payload), ensure_ascii=False),
                page_count,
                rule_count,
                error_message,
            )
        if updated is None:
            raise RuleEvaluationRunPersistenceError(f"运行 {run_id} 不存在或已终态，无法重复终结")

    async def get(
        self,
        *,
        run_id: str,
        task_id: str,
        access_scope: AccessScope,
    ) -> RuleEvaluationRun | None:
        if not _is_uuid(task_id):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    run.run_id, run.task_id::text AS task_id, run.status,
                    run.page_count, run.rule_count, run.requested_rule_codes,
                    run.result_payload, run.error_message, run.requested_by,
                    run.created_at, run.started_at, run.finished_at
                FROM qc_rule_evaluation_run AS run
                JOIN qc_task AS task ON task.id = run.task_id
                WHERE run.run_id = $1
                  AND run.task_id = $2::uuid
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
                run_id,
                task_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
        if row is None:
            return None
        return _parse_run(dict(row))

    async def list_for_task(
        self, *, task_id: str, access_scope: AccessScope
    ) -> list[RuleEvaluationRun]:
        if not _is_uuid(task_id):
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    run.run_id, run.task_id::text AS task_id, run.status,
                    run.page_count, run.rule_count, run.requested_rule_codes,
                    '[]'::jsonb AS result_payload, run.error_message,
                    run.requested_by, run.created_at, run.started_at,
                    run.finished_at
                FROM qc_rule_evaluation_run AS run
                JOIN qc_task AS task ON task.id = run.task_id
                WHERE run.task_id = $1::uuid
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
                ORDER BY run.created_at DESC
                """,
                task_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
        return [_parse_run(dict(row)) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_run(row: dict[str, Any]) -> RuleEvaluationRun:
    payload = row["result_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    requested = row.get("requested_rule_codes") or []
    if isinstance(requested, str):
        requested = json.loads(requested)
    return RuleEvaluationRun(
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        status=RuleEvaluationRunStatus(str(row["status"])),
        page_count=int(row["page_count"]),
        rule_count=int(row["rule_count"]),
        requested_rule_codes=tuple(str(item) for item in requested),
        result_payload=tuple(item for item in payload if isinstance(item, dict)),
        error_message=row["error_message"],
        requested_by=row["requested_by"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class PostgresRuleEvaluationPageSource:
    """独立规则质检的可判定页面枚举（qc_capture 统一身份表口径）。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_judgeable_pages(
        self, *, task_id: str, access_scope: AccessScope
    ) -> list[JudgeablePage]:
        if not _is_uuid(task_id):
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    common_capture.id::text AS page_capture_id,
                    common_capture.surface_kind,
                    COALESCE(
                        page_capture.page_type,
                        common_capture.metadata->>'page_type',
                        app_visual.visual_page_type
                    ) AS page_type,
                    (
                        common_capture.capture_role = 'after_action'
                        AND ad_landing.pair_complete
                    ) AS ad_navigation_pair_complete
                FROM qc_capture AS common_capture
                JOIN qc_task AS task ON task.id = common_capture.task_id
                LEFT JOIN qc_page_capture AS page_capture
                    ON page_capture.id = common_capture.id
                LEFT JOIN LATERAL (
                    SELECT observation.visual_page_type
                    FROM qc_app_visual_observation AS observation
                    WHERE observation.task_id = common_capture.task_id
                      AND observation.capture_id = common_capture.id
                    ORDER BY observation.created_at DESC, observation.observation_id DESC
                    LIMIT 1
                ) AS app_visual ON TRUE
                LEFT JOIN LATERAL (
                    SELECT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                            CASE
                                WHEN jsonb_typeof(
                                    common_capture.metadata->'action_log'
                                ) = 'array'
                                THEN common_capture.metadata->'action_log'
                                ELSE '[]'::jsonb
                            END
                        ) AS action(item)
                        WHERE action.item->'ad_navigation_evidence'->>'status'
                                = 'observed'
                          AND action.item->'ad_navigation_evidence'->>'pair_complete'
                                = 'true'
                          AND action.item->'ad_navigation_evidence'->>'navigation_mode'
                                = 'external_browser'
                          AND action.item->>'action_execution_id'
                                = common_capture.action_execution_id
                          AND action.item->'ad_navigation_evidence'->'source'->>'capture_id'
                                = common_capture.parent_capture_id::text
                          AND action.item->'ad_navigation_evidence'->'landing'->>'capture_id'
                                = common_capture.id::text
                    ) AS pair_complete
                ) AS ad_landing ON TRUE
                WHERE common_capture.task_id = $1::uuid
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
                  AND common_capture.capture_status = 'captured'
                  AND COALESCE(
                        page_capture.inspection_lifecycle_status,
                        common_capture.metadata->>'inspection_lifecycle_status',
                        ''
                      ) NOT IN ('superseded', 'abandoned')
                ORDER BY common_capture.created_at ASC
                """,
                task_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
        return [
            JudgeablePage(
                page_capture_id=str(row["page_capture_id"]),
                page_type=row["page_type"],
                surface_kind=row.get("surface_kind"),
                evidence_tags=(
                    frozenset({"ad_navigation_pair_complete"})
                    if row.get("ad_navigation_pair_complete") is True
                    else frozenset()
                ),
            )
            for row in rows
        ]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True
