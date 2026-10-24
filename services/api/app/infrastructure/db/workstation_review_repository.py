"""工作台人工审核仓储。"""

from datetime import UTC, datetime
from typing import Any

from services.api.app.application.workstation_reviews import WorkstationReviewListFilter
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task.status import TaskStatus
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.review_finalization import wake_review_finalization
from services.api.app.infrastructure.db.rule_confirmation_sql import (
    requires_human_confirmation_sql,
)
from services.api.app.infrastructure.db.rule_result_scope_sql import superseded_placeholder_sql
from services.api.app.infrastructure.db.task_status_repository import (
    transition_current_task_status_in_transaction,
)


class PostgresWorkstationReviewRepository:
    """基于 PostgreSQL 的工作台人工审核仓储。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = _normalize_asyncpg_url(database_url) if database_url else None
        self._pool = pool

    async def list_reviews(
        self, filters: WorkstationReviewListFilter, *, access_scope: AccessScope
    ) -> dict:
        pool = await self._get_pool()
        offset = (filters.page - 1) * filters.page_size
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    review.id::text AS review_id,
                    review.rule_execution_id::text AS execution_id,
                    review.task_id::text AS task_id,
                    COALESCE(task.task_config->>'task_name', task.target_url) AS task_name,
                    rule.name AS rule_name,
                    review.trigger_reason,
                    review.status,
                    review.reviewer::text AS reviewer,
                    review.review_reason,
                    review.created_at,
                    COUNT(*) OVER()::int AS total_count
                FROM qc_review_item review
                JOIN qc_task task ON task.id = review.task_id
                JOIN qc_rule_execution execution ON execution.id = review.rule_execution_id
                JOIN qc_rule rule ON rule.id = execution.rule_id
                WHERE ($1::varchar IS NULL OR review.status = $1)
                  AND ($2::varchar IS NULL OR review.trigger_reason = $2)
                  AND task.deleted_at IS NULL
                  AND execution.task_id = task.id
                  AND {requires_human_confirmation_sql("execution")}
                  AND NOT {superseded_placeholder_sql("execution")}
                  AND (
                      $5::boolean
                      OR $6::boolean
                      OR (
                          task.tenant_id = $7::uuid
                          AND (
                              task.submitter_id = $8::uuid
                              OR $9::boolean
                              OR $10::boolean
                              OR task.department_name = $11::varchar
                          )
                      )
                  )
                ORDER BY review.priority ASC, review.created_at ASC
                LIMIT $3 OFFSET $4
                """,
                filters.status,
                filters.trigger_reason,
                filters.page_size,
                offset,
                access_scope.can_read_any,
                "taskObjects:review:any" in access_scope.permissions,
                access_scope.tenant_id,
                access_scope.actor_user_id,
                access_scope.can_read_tenant,
                access_scope.can_review_tenant,
                access_scope.department_scope,
            )
        return {
            "items": [
                {
                    "review_id": row["review_id"],
                    "execution_id": row["execution_id"],
                    "task_id": row["task_id"],
                    "task_name": row["task_name"],
                    "rule_name": row["rule_name"],
                    "trigger_reason": row["trigger_reason"],
                    "status": row["status"],
                    "reviewer": row["reviewer"],
                    "review_reason": row["review_reason"],
                    "created_at": _datetime_to_string(row["created_at"]),
                }
                for row in rows
            ],
            "total": int(rows[0]["total_count"]) if rows else 0,
            "page": filters.page,
            "page_size": filters.page_size,
        }

    async def update_review_status(
        self,
        *,
        review_id: str,
        status: str,
        reason: str | None,
        access_scope: AccessScope,
    ) -> dict | None:
        pool = await self._get_pool()
        # 与报告人工判定共用任务→执行→审核的锁顺序，删除和改判不能绕过写入资格。
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""
                WITH eligible_review AS MATERIALIZED (
                    SELECT review.id AS review_id
                    FROM qc_task AS task
                    JOIN qc_review_item AS review ON review.task_id = task.id
                    JOIN qc_rule_execution AS execution
                        ON execution.id = review.rule_execution_id
                       AND execution.task_id = task.id
                    WHERE review.id = $1::uuid
                      AND task.deleted_at IS NULL
                      AND {requires_human_confirmation_sql("execution")}
                      AND NOT {superseded_placeholder_sql("execution")}
                      AND (
                          $5::boolean
                          OR $6::boolean
                          OR (
                              task.tenant_id = $7::uuid
                              AND (
                                  task.submitter_id = $8::uuid
                                  OR $9::boolean
                                  OR task.department_name = $10::varchar
                              )
                          )
                      )
                    FOR UPDATE OF task, execution, review
                ),
                updated_review AS (
                    UPDATE qc_review_item AS review
                    SET
                        status = $2,
                        review_reason = $3,
                        reviewer = $4::uuid,
                        reviewed_at = now(),
                        updated_at = now()
                    FROM eligible_review
                    WHERE review.id = eligible_review.review_id
                    RETURNING
                        review.id::text AS review_id,
                        review.rule_execution_id,
                        review.task_id,
                        review.status
                ),
                updated_execution AS (
                    UPDATE qc_rule_execution execution
                    SET
                        review_status = updated_review.status,
                        review_reason = $3,
                        human_verdict = CASE updated_review.status
                            WHEN 'approved' THEN 'pass'
                            WHEN 'rejected' THEN 'fail'
                            WHEN 'needs_recheck' THEN NULL
                            ELSE execution.human_verdict
                        END,
                        human_verdict_note = CASE
                            WHEN updated_review.status IN ('approved', 'rejected')
                            THEN $3
                            WHEN updated_review.status = 'needs_recheck'
                            THEN NULL
                            ELSE execution.human_verdict_note
                        END,
                        human_verdict_at = CASE
                            WHEN updated_review.status IN ('approved', 'rejected')
                            THEN now()
                            WHEN updated_review.status = 'needs_recheck'
                            THEN NULL
                            ELSE execution.human_verdict_at
                        END,
                        human_verdict_by = CASE
                            WHEN updated_review.status IN ('approved', 'rejected')
                            THEN $4::uuid::text
                            WHEN updated_review.status = 'needs_recheck'
                            THEN NULL
                            ELSE execution.human_verdict_by
                        END,
                        updated_at = now()
                    FROM updated_review
                    WHERE execution.id = updated_review.rule_execution_id
                    RETURNING execution.id, execution.agent_run_id
                )
                SELECT
                    updated_review.review_id,
                    updated_review.task_id::text AS task_id,
                    updated_review.status,
                    updated_execution.agent_run_id::text AS agent_run_id
                FROM updated_review
                JOIN updated_execution ON TRUE
                """,
                review_id,
                status,
                reason,
                access_scope.actor_user_id,
                access_scope.can_write_any,
                "taskObjects:review:any" in access_scope.permissions,
                access_scope.tenant_id,
                access_scope.actor_user_id,
                access_scope.can_review_tenant,
                access_scope.review_department_scope,
            )
            if row is None:
                return None
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
            if status == "needs_recheck":
                await transition_current_task_status_in_transaction(
                    connection,
                    task_id=row["task_id"],
                    target_status=TaskStatus.REVIEW,
                    allowed_source_statuses=(TaskStatus.DONE, TaskStatus.REVIEW),
                    occurred_at=datetime.now(UTC),
                )
            elif not gate_managed and int(pending_confirm_count or 0) == 0:
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
                    await transition_current_task_status_in_transaction(
                        connection,
                        task_id=row["task_id"],
                        target_status=TaskStatus.DONE,
                        allowed_source_statuses=(TaskStatus.REVIEW, TaskStatus.DONE),
                        occurred_at=datetime.now(UTC),
                    )
        return {
            "review_id": row["review_id"],
            "status": row["status"],
            "message": "审核已更新",
        }

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool


def _normalize_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url


def _datetime_to_string(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
