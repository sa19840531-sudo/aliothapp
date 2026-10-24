"""工作台首页聚合仓储。"""

from datetime import datetime
from typing import Any

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.rule_result_scope_sql import superseded_placeholder_sql

# 按质检规则统一口径：被同任务同规则真实结论顶掉的占位不进首页统计
_RESULT_SCOPE_SQL = f"NOT {superseded_placeholder_sql('execution')}"

# 统计口径按业务时区 UTC+8 划分「今日/昨日」。
# 使用固定偏移，避免 Windows/精简 PG 未装时区库时 Asia/Shanghai 不可用。
_TODAY_SQL = "((now() AT TIME ZONE 'UTC') + INTERVAL '8 hours')::date"

# 质检成功率分子 = AI 已出确定结果：通过 + 未通过（含 warning）+ 本产品不适用。
# 拆分后的 skipped 已不含计划外未覆盖（coverage_gap 单列），不会虚高。
_SUCCESS_RATE_STATUSES = ("pass", "fail", "warning", "skipped")

# 识别结果分布口径与报告读侧（report_insight._display_group）对齐：
# 人工终审优先；候选未执行/预算终止单列 not_executed；bootstrap 占位的
# skipped 是「计划外未覆盖」，与真实判定出的 skipped（本产品不适用）分开。
_EXECUTION_BUCKET_SQL = """
                    CASE
                        WHEN execution.human_verdict = 'pass' THEN 'pass'
                        WHEN execution.human_verdict = 'fail' THEN 'fail'
                        WHEN execution.human_verdict = 'ignored' THEN 'ignored'
                        WHEN execution.judge_output->>'reason' IN (
                            'candidate_not_executed',
                            'exploration_not_started',
                            'run_budget_exhausted',
                            'max_steps_reached',
                            'run_timeout',
                            'continuation_limit_reached'
                        ) THEN 'not_executed'
                        WHEN execution.execution_status = 'skipped'
                             AND COALESCE(execution.judge_input->>'bootstrap', 'false') = 'true'
                        THEN 'coverage_gap'
                        ELSE execution.execution_status
                    END
""".strip()


class PostgresWorkstationDashboardRepository:
    """基于 PostgreSQL 的工作台首页聚合仓储。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = _normalize_asyncpg_url(database_url) if database_url else None
        self._pool = pool

    async def get_dashboard_overview(self, *, access_scope: AccessScope) -> dict:
        pool = await self._get_pool()
        scope_args = (
            access_scope.can_read_any,
            access_scope.tenant_id,
            access_scope.can_read_tenant,
            access_scope.actor_user_id,
            access_scope.department_scope,
        )
        async with pool.acquire() as connection, connection.transaction(readonly=True):
            # 首页规则聚合的短查询编译成本高于执行，只作用于当前只读事务。
            await connection.execute("SET LOCAL jit = off")
            status_rows = await connection.fetch(
                """
                SELECT task.status, COUNT(*)::int AS count
                FROM qc_task AS task
                WHERE (
                    $1::boolean
                    OR (
                        task.tenant_id = $2::uuid
                        AND ($3::boolean OR task.submitter_id = $4::uuid OR task.department_name = $5::varchar)
                    )
                )
                GROUP BY task.status
                ORDER BY task.status
                """,
                *scope_args,
            )
            # 规则口径只过滤一次，四组首页数据共用该次聚合结果。
            execution_rows = await connection.fetch(_execution_overview_sql(), *scope_args)
            risk_rows = [row for row in execution_rows if row["row_kind"] == "risk"]
            execution_status_rows = [
                row for row in execution_rows if row["row_kind"] == "status"
            ]
            latest_execution_rows = [row for row in execution_rows if row["row_kind"] == "latest"]
            daily_execution_rows = [row for row in execution_rows if row["row_kind"] == "daily"]
            recent_rows = await connection.fetch(
                """
                SELECT
                    task.id::text AS task_id,
                    COALESCE(task.task_config->>'task_name', task.target_url) AS task_name,
                    task.status,
                    task.created_at
                FROM qc_task task
                WHERE (
                    $1::boolean
                    OR (
                        task.tenant_id = $2::uuid
                        AND ($3::boolean OR task.submitter_id = $4::uuid OR task.department_name = $5::varchar)
                    )
                )
                ORDER BY task.created_at DESC
                LIMIT 10
                """,
                *scope_args,
            )
            daily_task_row = await connection.fetchrow(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE {_local_date_sql('task.created_at')} = {_TODAY_SQL}
                    )::int AS today,
                    COUNT(*) FILTER (
                        WHERE {_local_date_sql('task.created_at')} = {_TODAY_SQL} - 1
                    )::int AS yesterday
                FROM qc_task AS task
                WHERE (
                    $1::boolean
                    OR (
                        task.tenant_id = $2::uuid
                        AND ($3::boolean OR task.submitter_id = $4::uuid OR task.department_name = $5::varchar)
                    )
                )
                """,
                *scope_args,
            )
        total_tasks = sum(int(row["count"]) for row in status_rows)
        running_tasks = sum(int(row["count"]) for row in status_rows if row["status"] == "running")
        done_tasks = sum(int(row["count"]) for row in status_rows if row["status"] == "done")
        return {
            "summary_cards": [
                {"key": "total_tasks", "label": "任务总数", "value": total_tasks},
                {"key": "running_tasks", "label": "执行中", "value": running_tasks},
                {"key": "done_tasks", "label": "已完成", "value": done_tasks},
            ],
            "today_metrics": _build_today_metrics(
                executed_tasks_today=int(daily_task_row["today"]),
                executed_tasks_yesterday=int(daily_task_row["yesterday"]),
                daily_execution_rows=[dict(row) for row in daily_execution_rows],
            ),
            "recent_tasks": [
                {
                    "task_id": row["task_id"],
                    "task_name": row["task_name"],
                    "status": row["status"],
                    "created_at": _datetime_to_string(row["created_at"]),
                }
                for row in recent_rows
            ],
            "status_distribution": [
                {"status": row["status"], "count": int(row["count"])} for row in status_rows
            ],
            "risk_distribution": [
                {"severity": row["severity"], "count": int(row["count"])} for row in risk_rows
            ],
            "execution_status_distribution": [
                {"execution_status": row["execution_status"], "count": int(row["count"])}
                for row in execution_status_rows
            ],
            "latest_rule_executions": [
                {
                    "execution_id": row["execution_id"],
                    "rule_name": row["rule_name"],
                    "execution_status": row["execution_status"],
                    "severity": row["severity"],
                    "finished_at": _datetime_to_string(row["finished_at"]),
                }
                for row in latest_execution_rows
            ],
        }

    async def get_month_token_usage(self, *, access_scope: AccessScope) -> dict:
        """本月（本地自然月）本公司任务的模型用量：Token 合计与费用。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    COALESCE(SUM(event.total_tokens), 0)::bigint AS total_tokens,
                    COALESCE(SUM(event.total_cost), 0)::numeric AS total_cost,
                    COALESCE(MAX(event.cost_currency), 'CNY') AS currency,
                    COUNT(DISTINCT event.task_id)::int AS task_count
                FROM qc_llm_usage_event AS event
                JOIN qc_task AS task ON task.id = event.task_id
                WHERE event.created_at >= date_trunc('month', now())
                  AND (
                      $1::boolean
                      OR (
                          task.tenant_id = $2::uuid
                          AND ($3::boolean OR task.submitter_id = $4::uuid OR task.department_name = $5::varchar)
                      )
                  )
                """,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
        return {
            "month": datetime.now().strftime("%Y-%m"),
            "total_tokens": int(row["total_tokens"] or 0),
            "total_cost": float(row["total_cost"] or 0),
            "currency": str(row["currency"] or "CNY"),
            "task_count": int(row["task_count"] or 0),
        }

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool


def _execution_overview_sql() -> str:
    """共享可见规则结果，避免四组首页统计重复执行占位过滤。"""
    return f"""
        WITH scoped_execution AS MATERIALIZED (
            SELECT
                execution.id,
                execution.rule_id,
                execution.severity,
                execution.created_at,
                execution.finished_at,
                CASE execution.human_verdict
                    WHEN 'pass' THEN 'pass'
                    WHEN 'fail' THEN 'fail'
                    WHEN 'ignored' THEN 'ignored'
                    ELSE execution.execution_status
                END AS latest_status,
                {_EXECUTION_BUCKET_SQL} AS bucket,
                {_local_date_sql('execution.created_at')} AS local_day
            FROM qc_rule_execution AS execution
            JOIN qc_task AS task ON task.id = execution.task_id
            WHERE (
                $1::boolean
                OR (
                    task.tenant_id = $2::uuid
                    AND ($3::boolean OR task.submitter_id = $4::uuid
                         OR task.department_name = $5::varchar)
                )
            )
              AND {_RESULT_SCOPE_SQL}
        ), latest AS (
            SELECT execution.*, rule.name AS rule_name
            FROM scoped_execution AS execution
            JOIN qc_rule AS rule ON rule.id = execution.rule_id
            ORDER BY execution.created_at DESC
            LIMIT 10
        )
        SELECT * FROM (
            SELECT
                'risk'::text AS row_kind,
                COALESCE(severity, 'info')::text AS severity,
                COUNT(*)::int AS count,
                NULL::text AS execution_status,
                NULL::boolean AS is_today,
                NULL::text AS execution_id,
                NULL::text AS rule_name,
                NULL::timestamptz AS finished_at,
                NULL::timestamptz AS created_at
            FROM scoped_execution
            GROUP BY COALESCE(severity, 'info')
            UNION ALL
            SELECT 'status', NULL, COUNT(*)::int, bucket, NULL,
                   NULL, NULL, NULL, NULL
            FROM scoped_execution
            GROUP BY bucket
            UNION ALL
            SELECT 'daily', NULL, COUNT(*)::int, bucket, local_day = {_TODAY_SQL},
                   NULL, NULL, NULL, NULL
            FROM scoped_execution
            WHERE local_day >= {_TODAY_SQL} - 1
            GROUP BY local_day = {_TODAY_SQL}, bucket
            UNION ALL
            SELECT 'latest', severity, NULL, latest_status, NULL,
                   id::text, rule_name, finished_at, created_at
            FROM latest
        ) AS overview
        ORDER BY row_kind,
                 CASE WHEN row_kind IN ('risk', 'status')
                      THEN COALESCE(severity, execution_status) END,
                 CASE WHEN row_kind = 'latest' THEN created_at END DESC
    """


def _build_today_metrics(
    *,
    executed_tasks_today: int,
    executed_tasks_yesterday: int,
    daily_execution_rows: list[dict],
) -> dict:
    """按首页统计卡片口径聚合今日指标。

    - 忽略规则数：今日终态为本产品不适用（判定 skipped）或人工忽略的规则数量。
    - 异常规则数：AI 没给出确定结果的规则总数（计划外未覆盖 / 检测异常 /
      证据不足 / 转人工 / 未执行）。
    - 质检成功率：AI 已出确定结果（通过 + 未通过 + 本产品不适用）/ 全部。
    """
    today_counts: dict[str, int] = {}
    yesterday_counts: dict[str, int] = {}
    for row in daily_execution_rows:
        bucket = today_counts if row["is_today"] else yesterday_counts
        bucket[str(row["execution_status"])] = int(row["count"])

    return {
        "executed_tasks_today": executed_tasks_today,
        "executed_tasks_yesterday": executed_tasks_yesterday,
        "ignored_rules_today": today_counts.get("skipped", 0) + today_counts.get("ignored", 0),
        "abnormal_rules_today": sum(
            today_counts.get(status, 0)
            for status in (
                "coverage_gap",
                "error",
                "manual_review",
                "evidence_insufficient",
                "not_executed",
            )
        ),
        "qc_success_rate_today": _success_rate(today_counts),
        "qc_success_rate_yesterday": _success_rate(yesterday_counts),
    }


def _local_date_sql(column: str) -> str:
    return f"(({column} AT TIME ZONE 'UTC') + INTERVAL '8 hours')::date"


def _success_rate(status_counts: dict[str, int]) -> float | None:
    total = sum(status_counts.values())
    if total == 0:
        return None
    succeeded = sum(status_counts.get(status, 0) for status in _SUCCESS_RATE_STATUSES)
    succeeded += status_counts.get("ignored", 0)
    return round(succeeded / total * 100, 1)


def _normalize_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url


def _datetime_to_string(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
