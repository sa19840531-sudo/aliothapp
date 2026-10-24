"""读取 H5 当前主 Run 的目标页面和业务成果，供完全技术失败收口与报告共用。"""

from typing import Any

from services.api.app.application.h5_run_failure import H5RunFailureFacts, classify_h5_run_failure
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.h5_browser_stop_receipt import unconfirmed_h5_stop_sql
from services.api.app.infrastructure.db.h5_completion_gate_sql import QUALIFIED_H5_INVENTORY_CTE_SQL
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.rule_result_scope_sql import superseded_placeholder_sql
from services.api.app.infrastructure.db.task_insight_repository import (
    _EXECUTION_REASON_SQL,
    _scope_sql,
)


class PostgresH5RunFailureFactsRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def assert_drained(self, *, task_id: str, agent_run_id: str) -> None:
        """复核 Runtime 已关闭的权威事实，不能在资源仍活动时把完全失败封存。"""
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            pending = await connection.fetchval(
                f"""
                SELECT EXISTS (
                    SELECT 1 FROM qc_surface_session AS session
                    WHERE session.task_id=$1::uuid
                      AND (session.session_id=$2 OR session.session_id LIKE $2 || ':recovery:%')
                      AND (session.status NOT IN ('closed','lost','failed')
                           OR session.lease_owner IS NOT NULL
                           OR session.lease_token IS NOT NULL
                           OR session.lease_expires_at IS NOT NULL
                           OR {unconfirmed_h5_stop_sql()})
                ) OR EXISTS (
                    SELECT 1 FROM qc_action_execution AS action
                    WHERE action.task_id=$1::uuid
                      AND (action.session_id=$2 OR action.session_id LIKE $2 || ':recovery:%')
                      AND action.status IN ('planned','executing','evidence_pending')
                )
                """,
                task_id,
                f"h5:{agent_run_id}",
            )
        if pending:
            raise RuntimeError("H5_FAILED_RUN_NOT_DRAINED：会话、命令或租约尚未释放")

    async def fetch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> H5RunFailureFacts | None:
        scope_sql, scope_args = _scope_sql(access_scope, task_alias="task", first_param=3)
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with (
            pool.acquire() as connection,
            connection.transaction(
                readonly=True,
                isolation="repeatable_read",
            ),
        ):
            context = await connection.fetchrow(
                f"""
                SELECT task.id::text AS task_id, task.target_url, task.task_config,
                       run.id::text AS run_id, run.status AS run_status, run.stop_reason,
                       run.error_message
                FROM qc_task AS task JOIN qc_agent_run AS run ON run.task_id=task.id
                WHERE task.id=$1::uuid AND run.id=$2::uuid
                  AND task.target_platform='h5' AND task.deleted_at IS NULL
                  AND (task.finalization_agent_run_id IS NULL
                       OR task.finalization_agent_run_id=run.id)
                  {scope_sql}
                """,
                task_id,
                agent_run_id,
                *scope_args,
            )
            if context is None:
                return None
            pages = await connection.fetch(
                """
                SELECT capture.id::text AS page_capture_id, capture.agent_run_id::text,
                       capture.capture_role, capture.capture_status, capture.metadata,
                       page.url, page.detection_status, page.inspection_lifecycle_status
                FROM qc_capture AS capture JOIN qc_page_capture AS page ON page.id=capture.id
                WHERE capture.task_id=$1::uuid AND capture.agent_run_id=$2::uuid
                """,
                task_id,
                agent_run_id,
            )
            inventory = await connection.fetch(
                f"""
                WITH target_task AS (SELECT * FROM qc_task WHERE id=$1::uuid),
                {QUALIFIED_H5_INVENTORY_CTE_SQL}
                SELECT after_capture_id::text, agent_run_id::text, 'pass'::text AS status
                FROM qualified_h5_inventory WHERE agent_run_id=$2::uuid
                """,
                task_id,
                agent_run_id,
            )
            rules = await connection.fetch(
                f"""
                SELECT execution.agent_run_id::text,
                       execution.page_capture_id::text,
                       common_capture.agent_run_id::text AS source_agent_run_id,
                       execution.execution_status, execution.result_summary,
                       COALESCE(execution.judge_input->>'bootstrap','false')='true' AS is_bootstrap,
                       {_EXECUTION_REASON_SQL} AS execution_reason
                FROM qc_rule_execution AS execution
                LEFT JOIN qc_capture AS common_capture
                  ON common_capture.id=execution.page_capture_id
                LEFT JOIN qc_page_capture AS capture ON capture.id=common_capture.id
                WHERE execution.task_id=$1::uuid AND (
                    execution.agent_run_id=$2::uuid OR common_capture.agent_run_id=$2::uuid
                )
                  AND NOT {superseded_placeholder_sql("execution")}
                """,
                task_id,
                agent_run_id,
            )
            actions = await connection.fetch(
                """
                SELECT action.error_code FROM qc_action_execution AS action
                WHERE action.task_id=$1::uuid AND (
                    action.session_id=$2 OR action.session_id LIKE $2 || ':recovery:%'
                )
                """,
                task_id,
                f"h5:{agent_run_id}",
            )
        return classify_h5_run_failure(
            dict(context),
            [dict(row) for row in pages],
            [dict(row) for row in rules],
            [dict(row) for row in inventory],
            [dict(row) for row in actions],
        )
