"""集合菜单迁移的真实 PG 升降级不改写既有证据。"""

import asyncio
import os
import sys

import pytest

from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _ALEMBIC_INI,
    _database,
    _test_database_url,
    _upgrade_isolated_postgres,  # noqa: F401 -- 隔离库迁移夹具。
)

pytestmark = pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需要隔离 PG")


async def migrate_collection(command, target):
    env = dict(os.environ, ALIOTH_DATABASE_URL=_test_database_url())
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(_ALEMBIC_INI),
        command,
        target,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode("utf-8")


async def test_empty_collection_evidence_can_downgrade_and_reupgrade():
    async with _database() as (control, _pool, _other, _ids):
        initial_revision = await control.fetchval("SELECT version_num FROM alembic_version")
        assert isinstance(initial_revision, str)
        try:
            code, _ = await migrate_collection("downgrade", "20260918_0124")
            assert code == 0
            assert (
                await control.fetchval("SELECT version_num FROM alembic_version") == "20260918_0124"
            )
        finally:
            code, _ = await migrate_collection("upgrade", initial_revision)
            assert code == 0
        assert await control.fetchval("SELECT version_num FROM alembic_version") == initial_revision
