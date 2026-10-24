"""连接级临时表验证报告列表的 H5 当前 Run 口径，不修改业务表。"""

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


class ConnectionPool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def connection():
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离 ALIOTH_TEST_DATABASE_URL；测试只使用连接级临时表")
    database_url = require_isolated_test_database_url(
        database_url, os.getenv("ALIOTH_DATABASE_URL") or None,
    )
    database = await asyncpg.connect(database_url)
    try:
        await database.execute("""
            CREATE TEMP TABLE qc_task (
                id uuid PRIMARY KEY, status text, target_platform text,
                task_config jsonb, finalization_agent_run_id uuid
            );
            CREATE TEMP TABLE qc_agent_run (
                id uuid PRIMARY KEY, task_id uuid, agent_type text,
                created_at timestamptz, finished_at timestamptz
            );
            CREATE TEMP TABLE qc_capture (id uuid PRIMARY KEY, agent_run_id uuid, metadata jsonb);
            CREATE TEMP TABLE qc_page_capture (id uuid PRIMARY KEY, page_type text);
            CREATE TEMP TABLE qc_rule_execution (
                id uuid PRIMARY KEY, task_id uuid, rule_id uuid, agent_run_id uuid,
                page_capture_id uuid, execution_status text, human_verdict text,
                judge_input jsonb DEFAULT '{}'::jsonb, judge_output jsonb DEFAULT '{}'::jsonb,
                result_summary text
            );
        """)
        yield database
    finally:
        await database.close()


async def _seed(connection, *, platform="h5", owner_bound=True):
    ids = {key: str(uuid4()) for key in ("task", "old", "current", "judge", "capture")}
    now = datetime.now(UTC)
    finishes = {
        "old": now - timedelta(days=3),
        "current": now - timedelta(days=2),
        "judge": now - timedelta(days=1),
    }
    await connection.execute(
        "INSERT INTO qc_task VALUES ($1::uuid, 'failed', $2, $3::jsonb, $4::uuid)",
        ids["task"], platform,
        json.dumps({"workflow_version": "v2", "execution_mode": "full_inspection"}),
        ids["current"] if owner_bound else None,
    )
    await connection.executemany(
        "INSERT INTO qc_agent_run VALUES ($1::uuid, $2::uuid, $3, $4, $4)",
        [
            (ids[key], ids["task"], "judge" if key == "judge" else "main", finish)
            for key, finish in finishes.items()
        ],
    )
    await connection.execute(
        "INSERT INTO qc_capture VALUES ($1::uuid, $2::uuid, '{}'::jsonb)",
        ids["capture"], ids["current"],
    )
    # 旧 Run 的通过和待复核在 H5 重试后均不得充当本次成果。
    await _execution(connection, ids, run="old", status="pass")
    await _execution(connection, ids, run="old", status="manual_review")
    return ids, finishes, PostgresTaskInsightRepository(pool=ConnectionPool(connection))


async def _execution(connection, ids, *, run, status, capture=None):
    await connection.execute(
        """
        INSERT INTO qc_rule_execution (
            id, task_id, rule_id, agent_run_id, page_capture_id, execution_status, judge_output
        ) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6, $7::jsonb)
        """,
        str(uuid4()), ids["task"], str(uuid4()), ids[run], capture, status,
        json.dumps({"reason": "low_confidence"} if status == "manual_review" else {}),
    )


@pytest.mark.parametrize("owner_bound", [True, False])
async def test_h5_failed_current_run_does_not_borrow_old_pass_or_review(connection, owner_bound):
    ids, finishes, repository = await _seed(connection, owner_bound=owner_bound)

    meta = (await repository.report_list_meta([ids["task"]]))[ids["task"]]

    assert meta == {
        "generated_at": finishes["current"], "pending_confirm_count": 0,
        "passed_count": 0, "failed_count": 0,
    }


@pytest.mark.parametrize("platform", ["h5", "web", "app"])
async def test_current_capture_independent_judge_counts_and_other_platforms_keep_task_scope(
    connection, platform,
):
    ids, finishes, repository = await _seed(connection, platform=platform)
    for status in ("pass", "warning", "manual_review"):
        await _execution(connection, ids, run="judge", status=status, capture=ids["capture"])

    meta = (await repository.report_list_meta([ids["task"]]))[ids["task"]]

    assert meta == {
        "generated_at": finishes["current" if platform == "h5" else "judge"],
        "pending_confirm_count": 1 if platform == "h5" else 2,
        "passed_count": 1 if platform == "h5" else 2,
        "failed_count": 1,
    }
