"""事务回滚验证 H5 逻辑消费 Run 不阻塞关闭后的补采扫描。"""

import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.tests.test_targeted_recapture_chain_postgres import _seed

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需显式提供 PostgreSQL 事务测试连接",
)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.parametrize(
    "case,eligible",
    [
        ("logical_consumer", True),
        ("foreign_source", False),
        ("foreign_epoch", False),
        ("main_running", False),
        ("ordinary_judge", False),
        ("notification_off", False),
        ("exploration_only", False),
        ("canceled", False),
        ("web", False),
        ("app", False),
    ],
)
async def test_h5_snapshot_scan_only_ignores_its_epoch_logical_consumer(case, eligible):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        ids = {
            name: str(uuid4())
            for name in (
                "tenant", "user", "ruleset", "rule", "task", "run", "work_item",
                "attempt", "original", "restored",
            )
        }
        await _seed(connection, ids)
        config = {
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "h5_inspection_notification_mode": "enforced",
        }
        if case == "notification_off":
            config["h5_inspection_notification_mode"] = "shadow"
        if case == "exploration_only":
            config["execution_mode"] = "exploration_only"
        if case == "web":
            config["web_inspection_notification_mode"] = "enforced"
        if case == "app":
            config["app"] = {"platform_build_ref": "snapshot-scan-test"}
        await connection.execute(
            "UPDATE qc_task SET target_platform=$2,task_config=$3::jsonb,"
            "finalization_state='session_draining',finalization_agent_run_id=$4::uuid,"
            "exploration_draining_at=now(),session_draining_at=now(),"
            "finalization_retry_at=NULL,status=$5,target_url=$6 WHERE id=$1::uuid",
            ids["task"], case if case in {"web", "app"} else "h5", json.dumps(config),
            ids["run"], "canceled" if case == "canceled" else "running",
            None if case == "app" else "https://example.com",
        )
        await connection.execute(
            "UPDATE qc_agent_run SET status=$2 WHERE id=$1::uuid",
            ids["run"], "running" if case == "main_running" else "failed",
        )
        consumer_context = {
            "source": "other" if case == "foreign_source" else "h5_inspection_publication",
            "finalization_epoch": 1 if case == "foreign_epoch" else 0,
        }
        await connection.execute(
            "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,input_context)"
            " VALUES($1::uuid,$2::uuid,$3,'H5 Snapshot Scan','running',$4::jsonb)",
            str(uuid4()), ids["task"], "main" if case == "ordinary_judge" else "rule_judge",
            json.dumps(consumer_context),
        )
        snapshots = await PostgresTaskFinalizationRepository(
            pool=_Pool(connection),
        ).list_recoverable_finalizations(limit=10000)
        assert (ids["task"] in {item.task_id for item in snapshots}) is eligible
        # 扫描不结算工作项，也不能改变 Gate 或任务终态。
        assert await connection.fetchval(
            "SELECT finalization_state FROM qc_task WHERE id=$1::uuid", ids["task"],
        ) == "session_draining"
        assert await connection.fetchval(
            "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", ids["work_item"],
        ) == "waiting_evidence"
    finally:
        await transaction.rollback()
        await connection.close()
