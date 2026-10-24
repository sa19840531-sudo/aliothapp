"""0053 App Run 执行租约与 PostgreSQL-first 恢复的真实数据库验收。

测试只在显式提供 ``ALIOTH_TEST_DATABASE_URL`` 时执行，并且只迁移通过隔离校验的
测试数据库；未配置时绝不回退到开发库。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.application.app_run_recovery import (
    AcquireAppRunExecutionLeaseCommand,
    ReleaseAppRunExecutionLeaseCommand,
    RenewAppRunExecutionLeaseCommand,
)
from services.api.app.infrastructure.db.app_run_recovery_repository import (
    PostgresAppRunRecoveryRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    """只在显式测试库执行完整迁移，避免误改开发数据库。"""
    database_url = _test_database_url()
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        with patch("logging.config.fileConfig"):
            command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous


async def test_postgres_app_run_lease_acquire_renew_terminal_and_release() -> None:
    """并发领取只保留一个持有者，终态只允许原持有者续租并可释放。"""
    async with _database() as (connection, pool, ids):
        repository = PostgresAppRunRecoveryRepository(pool=pool)
        first_command = _acquire_command(ids, owner="worker-a", token_key="token_a")
        second_command = _acquire_command(ids, owner="worker-b", token_key="token_b")

        acquired = await asyncio.gather(
            repository.acquire(first_command),
            repository.acquire(second_command),
        )
        winners = [lease for lease in acquired if lease is not None]
        assert len(winners) == 1
        assert sum(lease is None for lease in acquired) == 1
        winner = winners[0]
        assert winner.lease_owner == "worker-a" or winner.lease_owner == "worker-b"
        assert winner.attempt_count == 1
        assert winner.row_version == 0

        stored = await connection.fetchrow(
            """
            SELECT lease_owner, lease_token::text, row_version, attempt_count
            FROM qc_app_run_execution_lease
            WHERE agent_run_id = $1::uuid
            """,
            ids["run"],
        )
        assert stored is not None
        assert stored["attempt_count"] == 1
        assert stored["row_version"] == 0

        winner_command = (
            first_command if winner.lease_owner == "worker-a" else second_command
        )
        renewed = await repository.renew(
            RenewAppRunExecutionLeaseCommand(
                task_id=winner_command.task_id,
                agent_run_id=winner_command.agent_run_id,
                lease_owner=winner_command.lease_owner,
                lease_token=winner_command.lease_token,
                lease_seconds=120,
            )
        )
        assert renewed.row_version == 1
        assert renewed.attempt_count == 1

        await connection.execute(
            """
            UPDATE qc_agent_run
            SET status = 'done', finished_at = now()
            WHERE id = $1::uuid
            """,
            ids["run"],
        )

        terminal_renewed = await repository.renew(
            RenewAppRunExecutionLeaseCommand(
                task_id=winner_command.task_id,
                agent_run_id=winner_command.agent_run_id,
                lease_owner=winner_command.lease_owner,
                lease_token=winner_command.lease_token,
                lease_seconds=120,
            )
        )
        assert terminal_renewed.row_version == 2
        assert terminal_renewed.attempt_count == 1

        terminal_new_acquire = await repository.acquire(
            _acquire_command(ids, owner="worker-c", token_key="token_c")
        )
        assert terminal_new_acquire is None

        released = await repository.release(
            ReleaseAppRunExecutionLeaseCommand(
                task_id=winner_command.task_id,
                agent_run_id=winner_command.agent_run_id,
                lease_owner=winner_command.lease_owner,
                lease_token=winner_command.lease_token,
            )
        )
        assert released is True
        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_app_run_execution_lease
                WHERE agent_run_id = $1::uuid
                """,
                ids["run"],
            )
            == 0
        )


async def test_postgres_expired_app_run_lease_takeover_increments_attempt_count() -> None:
    """过期租约可被新 owner 接管，row_version 与 attempt_count 都单调递增。"""
    async with _database() as (connection, pool, ids):
        repository = PostgresAppRunRecoveryRepository(pool=pool)
        first = await repository.acquire(
            _acquire_command(ids, owner="worker-a", token_key="token_a", seconds=1)
        )
        assert first is not None
        await _wait_until_expired(connection, ids["run"])

        takeover = await repository.acquire(
            _acquire_command(ids, owner="worker-b", token_key="token_b", seconds=120)
        )
        assert takeover is not None
        assert takeover.lease_owner == "worker-b"
        assert takeover.lease_token == ids["token_b"]
        assert takeover.row_version == first.row_version + 1
        assert takeover.attempt_count == first.attempt_count + 1

        released = await repository.release(
            ReleaseAppRunExecutionLeaseCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                lease_owner="worker-b",
                lease_token=ids["token_b"],
            )
        )
        assert released is True


async def test_postgres_recovery_snapshot_bootstrap_not_started_is_ready() -> None:
    """没有 Bootstrap 事实时，运行中的 App Run 应被 PostgreSQL 标记为可恢复。"""
    async with _database() as (connection, pool, ids):
        repository = PostgresAppRunRecoveryRepository(pool=pool)

        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_app_bootstrap
                WHERE task_id = $1::uuid
                """,
                ids["task"],
            )
            == 0
        )
        snapshot = await repository.get_recovery_snapshot(agent_run_id=ids["run"])

        assert snapshot is not None
        assert snapshot.ready is True
        assert snapshot.reason == "bootstrap_not_started"
        assert snapshot.execution_lease_active is False
        assert snapshot.next_recovery_at is None
        assert snapshot.record.run_id == ids["run"]


@asynccontextmanager
async def _database() -> AsyncIterator[
    tuple[asyncpg.Connection, asyncpg.Pool, dict[str, str]]
]:
    """每个测试使用独立任务，结束时依赖任务级联清理全部租约与 Run。"""
    ids = _ids()
    database_url = _test_database_url()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    try:
        await _seed_app_run(connection, ids)
        yield connection, pool, ids
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


def _ids() -> dict[str, str]:
    return {
        "tenant": str(uuid4()),
        "user": str(uuid4()),
        "ruleset": str(uuid4()),
        "task": str(uuid4()),
        "run": str(uuid4()),
        "token_a": str(uuid4()),
        "token_b": str(uuid4()),
        "token_c": str(uuid4()),
        "build_ref": f"app-build:{uuid4()}",
    }


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )


async def _seed_app_run(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'App Run Recovery Integration')
        """,
        ids["tenant"],
        f"app-run-recovery-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (
            id, username, full_name, password_hash, role, tenant_id
        )
        VALUES (
            $1::uuid, $2, 'App Run Recovery Integration', 'not-a-login-secret',
            '平台管理员', $3::uuid
        )
        """,
        ids["user"],
        f"app-run-recovery-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES (
            $1::uuid, $2, 'App Run Recovery Integration', 1,
            'enabled', 'app-run-test', 0, 'manual'
        )
        """,
        ids["ruleset"],
        f"app-run-recovery-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, target_platform,
            tenant_id, submitter_id, task_config
        )
        VALUES (
            $1::uuid, NULL, $2::uuid, 'running', 'app', $3::uuid, $4::uuid,
            jsonb_build_object(
                'app', jsonb_build_object('platform_build_ref', $5::text)
            )
        )
        """,
        ids["task"],
        ids["ruleset"],
        ids["tenant"],
        ids["user"],
        ids["build_ref"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (
            id, task_id, agent_type, agent_name, status, input_context, loop_guard
        )
        VALUES (
            $1::uuid, $2::uuid, 'main', 'App Run Recovery Integration', 'running',
            jsonb_build_object(
                'message', '恢复 App Run',
                'task_config', jsonb_build_object('kind', 'app')
            ),
            jsonb_build_object('max_steps', 10)
        )
        """,
        ids["run"],
        ids["task"],
    )


def _acquire_command(
    ids: dict[str, str],
    *,
    owner: str,
    token_key: str,
    seconds: int = 120,
) -> AcquireAppRunExecutionLeaseCommand:
    return AcquireAppRunExecutionLeaseCommand(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        lease_owner=owner,
        lease_token=ids[token_key],
        lease_seconds=seconds,
    )


async def _wait_until_expired(connection: asyncpg.Connection, run_id: str) -> None:
    """等待数据库时钟确认租约到期，避免固定 sleep 的竞态。"""
    for _ in range(60):
        if await connection.fetchval(
            """
            SELECT lease_expires_at <= now()
            FROM qc_app_run_execution_lease
            WHERE agent_run_id = $1::uuid
            """,
            run_id,
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待 App Run 执行租约到期超时")
