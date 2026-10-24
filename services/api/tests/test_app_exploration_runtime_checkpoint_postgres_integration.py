"""0054 App 探索运行时检查点的真实 PostgreSQL 验收。

测试只在显式提供 ``ALIOTH_TEST_DATABASE_URL`` 时执行；未提供时绝不回退到开发库。
"""

from __future__ import annotations

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
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"
_PROTOCOL_VERSION = "app-exploration-runtime-checkpoint.v1"
_RUNTIME_POLICY_HASH = "sha256:" + stable_protocol_hash({"scan_mode": "full"})
_OBJECTIVE_HASH = "b" * 64


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    """只在显式隔离测试库执行完整迁移。"""
    database_url = _test_database_url()
    with patch("logging.config.fileConfig"):
        _run_alembic_with_database(database_url, "upgrade", "head")


@pytest.mark.parametrize("with_h5_coverage", [False, True])
async def test_postgres_checkpoint_is_app_only_and_immutable(with_h5_coverage: bool) -> None:
    """正确事实可写入；错 H5、构建、Capture、包名和后续变更均被拒绝。"""
    async with _database() as (connection, ids):
        await _insert_checkpoint(connection, ids)
        stored = await connection.fetchrow(
            """
            SELECT agent_run_id, task_id, surface_session_id,
                   platform_build_ref, bootstrap_capture_id, installed_package,
                   protocol_version, runtime_policy, runtime_policy_sha256,
                   objective_hash, started_at, deadline
            FROM qc_app_exploration_runtime_checkpoint
            WHERE agent_run_id = $1::uuid
            """,
            ids["run"],
        )
        assert stored is not None
        assert str(stored["agent_run_id"]) == ids["run"]
        assert str(stored["bootstrap_capture_id"]) == ids["capture"]
        assert stored["installed_package"] == ids["package"]
        assert stored["runtime_policy_sha256"] == _RUNTIME_POLICY_HASH
        assert stored["objective_hash"] == _OBJECTIVE_HASH
        assert stored["deadline"] > stored["started_at"]

        # 同一 App Run 的错构建必须被 Session/Bootstrap 双重身份校验拒绝。
        with pytest.raises(asyncpg.PostgresError, match="归属无效"):
            async with connection.transaction():
                await _insert_checkpoint(
                    connection,
                    ids,
                    platform_build_ref="app-build:wrong",
                )

        # 同一 Session 的非 Bootstrap Capture 不能伪装成 Bootstrap 检查点。
        await connection.execute(
            """
            INSERT INTO qc_capture (
                id, task_id, agent_run_id, surface_kind, surface_session_id,
                capture_role, capture_status, captured_at
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'android', $4,
                    'observation', 'captured', now())
            """,
            ids["wrong_capture"],
            ids["task"],
            ids["run"],
            ids["session"],
        )
        with pytest.raises(asyncpg.PostgresError, match="归属无效"):
            async with connection.transaction():
                await _insert_checkpoint(
                    connection,
                    ids,
                    bootstrap_capture_id=ids["wrong_capture"],
                )

        with pytest.raises(asyncpg.PostgresError, match="归属无效"):
            async with connection.transaction():
                await _insert_checkpoint(
                    connection,
                    ids,
                    installed_package="com.example.other",
                )

        # 完全相同的幂等 UPDATE 可以重放，任一封存字段变化均不得通过。
        await connection.execute(
            """
            UPDATE qc_app_exploration_runtime_checkpoint
            SET agent_run_id = agent_run_id,
                updated_at = updated_at
            WHERE agent_run_id = $1::uuid
            """,
            ids["run"],
        )
        with pytest.raises(asyncpg.PostgresError, match="禁止修改"):
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_app_exploration_runtime_checkpoint
                    SET runtime_policy = '{"scan_mode": "quick"}'::jsonb
                    WHERE agent_run_id = $1::uuid
                    """,
                    ids["run"],
                )

        with pytest.raises(asyncpg.PostgresError, match="禁止直接删除"):
            async with connection.transaction():
                await connection.execute(
                    """
                    DELETE FROM qc_app_exploration_runtime_checkpoint
                    WHERE agent_run_id = $1::uuid
                    """,
                    ids["run"],
                )

        await connection.execute(
            """
            UPDATE qc_surface_session
            SET status = 'lost',
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                failure_reason = 'integration-test',
                lost_at = now(),
                row_version = row_version + 1,
                updated_at = now()
            WHERE task_id = $1::uuid AND session_id = $2
            """,
            ids["task"],
            ids["session"],
        )
        with pytest.raises(asyncpg.PostgresError, match="归属无效"):
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_app_exploration_runtime_checkpoint
                    SET agent_run_id = agent_run_id
                    WHERE agent_run_id = $1::uuid
                    """,
                    ids["run"],
                )
        with pytest.raises(asyncpg.PostgresError, match="禁止修改"):
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_app_exploration_runtime_checkpoint
                    SET deadline = deadline + interval '1 minute'
                    WHERE agent_run_id = $1::uuid
                    """,
                    ids["run"],
                )

        h5_ids = await _seed_context(
            connection,
            target_platform="h5",
            surface_kind="web",
        )
        with pytest.raises(asyncpg.PostgresError, match="归属无效"):
            async with connection.transaction():
                await _insert_checkpoint(
                    connection,
                    h5_ids,
                    runtime_policy_hash=_RUNTIME_POLICY_HASH,
                )

        if with_h5_coverage:
            from services.api.tests.test_h5_gate_coverage_postgres import _insert_h5_snapshot

            await _insert_h5_snapshot(
                connection,
                task_id=h5_ids["task"],
                agent_run_id=h5_ids["run"],
                finalization_epoch=0,
                revision=1,
                coverage_snapshot_id=f"checkpoint-coexist-{uuid4()}",
                payload_hash="a" * 64,
            )

        # 精准执行目标迁移，不让其他历史材料的降级保护遮蔽本次检查。
        coverage_count = await connection.fetchval("SELECT count(*) FROM qc_h5_coverage_snapshot")
        with pytest.raises(DBAPIError, match="检查点|0054"):
            await _run_checkpoint_downgrade_guard()
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_app_exploration_runtime_checkpoint "
            "WHERE agent_run_id=$1::uuid",
            ids["run"],
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_h5_coverage_snapshot",
        ) == coverage_count
        await connection.execute(
            "DELETE FROM qc_task WHERE id = $1::uuid",
            h5_ids["task"],
        )
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            h5_ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", h5_ids["user"])
        await connection.execute(
            "DELETE FROM qc_tenant WHERE id = $1::uuid",
            h5_ids["tenant"],
        )


@asynccontextmanager
async def _database() -> AsyncIterator[tuple[asyncpg.Connection, dict[str, str]]]:
    connection = await asyncpg.connect(_test_database_url())
    ids = await _seed_context(connection)
    try:
        yield connection, ids
    finally:
        await connection.execute(
            "DELETE FROM qc_task WHERE id = ANY($1::uuid[])",
            [ids["task"]],
        )
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute(
            "DELETE FROM qc_tenant WHERE id = $1::uuid",
            ids["tenant"],
        )
        await connection.close()


async def test_context_names_are_independent_but_duplicate_names_still_rejected() -> None:
    """隔离样本可以共存，但随机主键不能绕过生产名称唯一约束。"""
    connection = await asyncpg.connect(_test_database_url())
    transaction = connection.transaction()
    await transaction.start()
    try:
        app_ids = await _seed_context(connection)
        h5_ids = await _seed_context(
            connection, target_platform="h5", surface_kind="web",
        )
        names = await connection.fetch(
            "SELECT display_name FROM qc_tenant WHERE id = ANY($1::uuid[])",
            [app_ids["tenant"], h5_ids["tenant"]],
        )
        assert len(names) == 2
        assert len({row["display_name"].strip().lower() for row in names}) == 2
        with pytest.raises(asyncpg.UniqueViolationError, match="display_name_normalized"):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO qc_tenant(id, tenant_key, display_name) VALUES($1, $2, $3)",
                    uuid4(), f"duplicate-{uuid4()}",
                    f"  {names[0]['display_name'].swapcase()}  ",
                )
    finally:
        await transaction.rollback()
        await connection.close()


async def _seed_context(
    connection: asyncpg.Connection,
    *,
    target_platform: str = "app",
    surface_kind: str = "android",
) -> dict[str, str]:
    """建立一组最小 Run、Session、Bootstrap Capture 和 APP_READY 事实。"""
    ids = {
        "tenant": str(uuid4()),
        "user": str(uuid4()),
        "ruleset": str(uuid4()),
        "task": str(uuid4()),
        "run": str(uuid4()),
        "session": f"checkpoint-session-{uuid4()}",
        "capture": str(uuid4()),
        "wrong_capture": str(uuid4()),
        "build_ref": f"app-build:{uuid4()}",
        "package": "com.example.app",
    }
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, $3)
        """,
        ids["tenant"],
        f"runtime-checkpoint-{ids['tenant']}",
        f"Runtime Checkpoint Integration {ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (
            id, username, full_name, password_hash, role, tenant_id
        )
        VALUES (
            $1::uuid, $2, 'Runtime Checkpoint Integration', 'not-a-login-secret',
            '平台管理员', $3::uuid
        )
        """,
        ids["user"],
        f"runtime-checkpoint-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'Runtime Checkpoint Integration', 1,
                'enabled', 'runtime-checkpoint-test', 0, 'manual')
        """,
        ids["ruleset"],
        f"runtime-checkpoint-{ids['ruleset']}",
    )
    target_url = "https://example.invalid" if target_platform == "h5" else None
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, target_platform,
            tenant_id, submitter_id, task_config
        )
        VALUES ($1::uuid, $2, $3::uuid, 'running', $4::varchar, $6::uuid, $7::uuid,
                CASE WHEN $4::varchar = 'app'
                     THEN jsonb_build_object(
                         'app', jsonb_build_object('platform_build_ref', $5::text)
                     )
                     ELSE '{}'::jsonb
                END)
        """,
        ids["task"],
        target_url,
        ids["ruleset"],
        target_platform,
        ids["build_ref"],
        ids["tenant"],
        ids["user"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (
            id, task_id, agent_type, agent_name, status
        )
        VALUES ($1::uuid, $2::uuid, 'main', 'Runtime Checkpoint Integration', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_surface_session (
            session_id, task_id, surface_kind, session_owner, status,
            platform_build_ref, activated_at
        )
        VALUES ($1, $2::uuid, $3, 'checkpoint-test', 'active', $4, now())
        """,
        ids["session"],
        ids["task"],
        surface_kind,
        ids["build_ref"],
    )
    await connection.execute(
        """
        INSERT INTO qc_capture (
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, capture_status, captured_at
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5,
                'bootstrap', 'captured', now())
        """,
        ids["capture"],
        ids["task"],
        ids["run"],
        surface_kind,
        ids["session"],
    )
    await connection.execute(
        """
        INSERT INTO qc_app_bootstrap (
            session_id, task_id, platform_build_ref, stage,
            install_request_id, install_attempt, installed_package,
            open_request_id, open_attempt, bootstrap_capture_id,
            started_at, ready_at
        )
        VALUES ($1, $2::uuid, $3, 'app_ready',
                'install-request', 1, $4,
                'open-request', 1, $5::uuid,
                now() - interval '1 minute', now())
        """,
        ids["session"],
        ids["task"],
        ids["build_ref"],
        ids["package"],
        ids["capture"],
    )
    return ids


async def _insert_checkpoint(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    agent_run_id: str | None = None,
    task_id: str | None = None,
    surface_session_id: str | None = None,
    platform_build_ref: str | None = None,
    bootstrap_capture_id: str | None = None,
    installed_package: str | None = None,
    runtime_policy_hash: str = _RUNTIME_POLICY_HASH,
) -> None:
    await connection.execute(
        """
        INSERT INTO qc_app_exploration_runtime_checkpoint (
            agent_run_id, task_id, surface_session_id, platform_build_ref,
            bootstrap_capture_id, installed_package, protocol_version,
            runtime_policy, runtime_policy_sha256, objective_hash,
            started_at, deadline
        )
        VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6, $7,
                '{"scan_mode":"full"}'::jsonb, $8, $9,
                now() - interval '10 seconds', now() + interval '10 minutes')
        """,
        agent_run_id or ids["run"],
        task_id or ids["task"],
        surface_session_id or ids["session"],
        platform_build_ref or ids["build_ref"],
        bootstrap_capture_id or ids["capture"],
        installed_package or ids["package"],
        _PROTOCOL_VERSION,
        runtime_policy_hash,
        _OBJECTIVE_HASH,
    )


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )


async def _run_checkpoint_downgrade_guard() -> None:
    """在回滚事务中调用真实迁移，保留所有其他迁移与封存样本。"""
    from services.api.tests.test_app_exploration_runtime_checkpoint_migration import (
        _load_migration,
    )

    engine = create_async_engine(
        _test_database_url().replace("postgresql://", "postgresql+asyncpg://", 1),
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                def downgrade(sync_connection):
                    migration = _load_migration()
                    migration.op = Operations(MigrationContext.configure(sync_connection))
                    migration.downgrade()

                await connection.run_sync(downgrade)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _run_alembic_with_database(database_url: str, action: str, revision: str) -> None:
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        config = AlembicConfig(str(_ALEMBIC_INI))
        if action == "upgrade":
            command.upgrade(config, revision)
        else:
            command.downgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous
