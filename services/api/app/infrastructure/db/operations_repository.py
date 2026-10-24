"""运营总览 / 使用分析的聚合读模型（体验中心管理后台）。

口径：
- 任务数 = 时间窗内创建的任务；报告数 = 其中状态为 done / review 的任务（报告已生成）；
  生成失败 = 状态 failed。
- 活跃账号 = 时间窗内登录过或发起过任务的账号；活跃公司 = 有任务的公司。
- 平均执行时长 = 已生成报告（done / review）任务的 finished_at - started_at 平均值，
  不含排队时间；排除失败、取消、缺少起止时间及非有限或倒置的时间，合法零耗时仍计入。
只出聚合数字，不带截图、日志等明细。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_TASK_WINDOW = """
    task.created_at >= $1 AND task.created_at < $2
    AND ($3::uuid IS NULL OR task.tenant_id = $3::uuid)
    AND ($4::varchar IS NULL OR task.target_platform = $4)
"""


class PostgresOperationsRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def summarize_window(
        self,
        *,
        start: datetime,
        end: datetime,
        tenant_id: str | None,
        platform: str | None,
    ) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            task_row = await connection.fetchrow(
                f"""
                SELECT
                    COUNT(*)::int AS task_count,
                    COUNT(*) FILTER (WHERE task.status IN ('done', 'review'))::int AS report_count,
                    COUNT(*) FILTER (WHERE task.status = 'done')::int AS done_count,
                    COUNT(*) FILTER (WHERE task.status = 'review')::int AS review_count,
                    COUNT(*) FILTER (WHERE task.status = 'failed')::int AS failed_count,
                    COUNT(*) FILTER (WHERE task.status = 'canceled')::int AS canceled_count,
                    COUNT(*) FILTER (WHERE task.status IN ('queued', 'running'))::int AS active_count,
                    COUNT(DISTINCT task.tenant_id)::int AS active_tenant_count,
                    AVG(EXTRACT(EPOCH FROM (task.finished_at - task.started_at)))
                        FILTER (
                            WHERE task.status IN ('done', 'review')
                              AND task.started_at IS NOT NULL
                              AND task.finished_at IS NOT NULL
                              AND isfinite(task.started_at)
                              AND isfinite(task.finished_at)
                              AND task.finished_at >= task.started_at
                        )::float8
                        AS avg_duration_seconds
                FROM qc_task AS task
                WHERE {_TASK_WINDOW}
                """,
                start,
                end,
                tenant_id,
                platform,
            )
            account_row = await connection.fetchrow(
                """
                SELECT COUNT(*)::int AS active_account_count
                FROM qc_user AS account
                WHERE account.username <> '__history_principal__'
                  AND ($3::uuid IS NULL OR account.tenant_id = $3::uuid)
                  AND (
                      (account.last_login_at >= $1 AND account.last_login_at < $2)
                      OR EXISTS (
                          SELECT 1
                          FROM qc_task AS task
                          WHERE task.submitter_id = account.id
                            AND task.created_at >= $1 AND task.created_at < $2
                            AND ($4::varchar IS NULL OR task.target_platform = $4)
                      )
                  )
                """,
                start,
                end,
                tenant_id,
                platform,
            )
        data = dict(task_row or {})
        data["active_account_count"] = int((account_row or {}).get("active_account_count") or 0)
        return data

    async def daily_trend(
        self,
        *,
        start: datetime,
        end: datetime,
        tenant_id: str | None,
        platform: str | None,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    to_char(date_trunc('day', task.created_at), 'YYYY-MM-DD') AS day,
                    COUNT(*)::int AS task_count,
                    COUNT(*) FILTER (WHERE task.status IN ('done', 'review'))::int AS report_count,
                    COUNT(*) FILTER (WHERE task.status = 'failed')::int AS failed_count
                FROM qc_task AS task
                WHERE {_TASK_WINDOW}
                GROUP BY 1
                ORDER BY 1
                """,
                start,
                end,
                tenant_id,
                platform,
            )
        return [dict(row) for row in rows]

    async def platform_share(
        self,
        *,
        start: datetime,
        end: datetime,
        tenant_id: str | None,
        platform: str | None,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT task.target_platform AS platform, COUNT(*)::int AS task_count
                FROM qc_task AS task
                WHERE {_TASK_WINDOW}
                GROUP BY 1
                ORDER BY 2 DESC
                """,
                start,
                end,
                tenant_id,
                platform,
            )
        return [dict(row) for row in rows]

    async def tenant_rows(
        self,
        *,
        start: datetime,
        end: datetime,
        tenant_id: str | None,
        platform: str | None,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH stats AS (
                    SELECT
                        task.tenant_id,
                        COUNT(*)::int AS task_count,
                        COUNT(*) FILTER (WHERE task.status IN ('done', 'review'))::int AS report_count,
                        COUNT(*) FILTER (WHERE task.status = 'failed')::int AS failed_count,
                        MAX(task.created_at) AS last_task_at
                    FROM qc_task AS task
                    WHERE {_TASK_WINDOW}
                    GROUP BY task.tenant_id
                )
                SELECT
                    tenant.id::text AS tenant_id,
                    tenant.display_name AS tenant_name,
                    tenant.status,
                    (
                        SELECT COUNT(*)
                        FROM qc_product AS product
                        WHERE product.tenant_id = tenant.id
                           OR (product.tenant_id IS NULL AND tenant.id = '{DEFAULT_TENANT_ID}'::uuid)
                    )::int AS product_count,
                    COALESCE(stats.task_count, 0)::int AS task_count,
                    COALESCE(stats.report_count, 0)::int AS report_count,
                    COALESCE(stats.failed_count, 0)::int AS failed_count,
                    stats.last_task_at,
                    (
                        SELECT COUNT(*)
                        FROM qc_user AS account
                        WHERE account.tenant_id = tenant.id
                          AND account.username <> '__history_principal__'
                          AND (
                              (account.last_login_at >= $1 AND account.last_login_at < $2)
                              OR EXISTS (
                                  SELECT 1
                                  FROM qc_task AS task
                                  WHERE task.submitter_id = account.id
                                    AND task.created_at >= $1 AND task.created_at < $2
                                    AND ($4::varchar IS NULL OR task.target_platform = $4)
                              )
                          )
                    )::int AS active_account_count,
                    (
                        SELECT MAX(account.last_login_at)
                        FROM qc_user AS account
                        WHERE account.tenant_id = tenant.id
                          AND account.username <> '__history_principal__'
                    ) AS last_login_at
                FROM qc_tenant AS tenant
                LEFT JOIN stats ON stats.tenant_id = tenant.id
                WHERE ($3::uuid IS NULL OR tenant.id = $3::uuid)
                ORDER BY COALESCE(stats.report_count, 0) DESC, COALESCE(stats.task_count, 0) DESC,
                    tenant.created_at ASC
                """,
                start,
                end,
                tenant_id,
                platform,
            )
        return [dict(row) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
