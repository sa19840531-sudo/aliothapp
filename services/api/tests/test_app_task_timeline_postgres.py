"""真实 APP 采集的时间线不能把未知候选数量写成零。"""

from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.tests.test_app_capture_session_auth_postgres import _database
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 仅迁移显式隔离数据库
)


async def test_app_capture_without_rule_matching_keeps_candidate_count_unknown(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            "'{\"page_type\":\"post_list\",\"page_state\":\"stable\"}'::jsonb "
            "WHERE id=$1::uuid",
            saved.capture_id,
        )
        rows = await PostgresTaskInsightRepository(pool=pool).list_page_nodes(
            agent_run_id=ids["run"]
        )
        current = next(row for row in rows if row["page_capture_id"] == saved.capture_id)
        assert current["page_type"] == "post_list"
        assert current["candidate_rule_total"] is None
