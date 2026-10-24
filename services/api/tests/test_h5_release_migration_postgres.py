"""独立临时数据库验证真实迁移与历史事实保留，不连接运行中的验收库。"""

import asyncio
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from services.api.app.infrastructure.db.h5_llm_budget_repository import (
    PostgresH5LLMBudgetRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_targeted_recapture_chain_postgres import _seed

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离数据库",
)
ROOT = Path(__file__).resolve().parents[3]
HISTORY_TABLES = (
    "qc_task", "qc_agent_run", "qc_capture", "qc_page_capture",
    "qc_inspection_work_item", "qc_inspection_work_item_rule", "qc_inspection_attempt",
    "qc_evidence_manifest",
)


@asynccontextmanager
async def migration_database():
    url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL"),
    )
    name = f"alioth_h5_migration_test_{uuid4().hex}"
    parsed = urlsplit(url)
    private_url = urlunsplit(parsed._replace(path=f"/{name}"))
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    try:
        yield private_url
    finally:
        # 只删除本用例创建的随机数据库，原隔离库与历史验收库均不在清理范围内。
        await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()


async def migrate(url, revision, *, script_location=None, expected_error=None):
    environment = dict(os.environ, ALIOTH_DATABASE_URL=url, PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-m", "alembic", "-c", "services/api/migrations/alembic.ini",
               "upgrade", revision]
    if script_location is not None:
        command = [sys.executable, "-c",
                   "import sys; from alembic.config import Config; from alembic import command; "
                   "c=Config('services/api/migrations/alembic.ini'); "
                   "c.set_main_option('script_location',sys.argv[1]); "
                   "command.upgrade(c,sys.argv[2])",
                   str(script_location), revision]
    process = await asyncio.create_subprocess_exec(
        *command, cwd=ROOT, env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
    if expected_error is not None:
        assert process.returncode != 0, output.decode()
        assert expected_error in output.decode()
    else:
        assert process.returncode == 0, output.decode()


async def history(connection):
    return {
        table: await connection.fetchval(
            f"SELECT COALESCE(jsonb_agg(to_jsonb(row) ORDER BY to_jsonb(row)::text), '[]') "
            f"FROM {table} AS row"
        )
        for table in HISTORY_TABLES
    }


@pytest.mark.parametrize("baseline", ["base", "20260930_0130", "20261010_0132", "20261016_0138"])
async def test_release_upgrade_preserves_history_and_never_refreshes_budget(baseline):
    async with migration_database() as url:
        if baseline != "base":
            await migrate(url, baseline)
            connection = await asyncpg.connect(url)
            try:
                ids = {key: str(uuid4()) for key in (
                    "tenant", "user", "ruleset", "rule", "task", "run", "work_item",
                    "attempt", "original", "restored",
                )}
                await _seed(connection, ids)
                await connection.execute(
                    "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid", ids["task"],
                )
                await connection.execute(
                    "INSERT INTO qc_evidence_manifest "
                    "(manifest_id,manifest_version,task_id,session_id,capture_id,protocol_version,"
                    "manifest_hash,payload_hash,quality_status,manifest_payload,created_at) "
                    "VALUES($1,1,$2::uuid,$3,$4::uuid,'evidence-manifest.v1',$5,$6,'pass',$7::jsonb,now())",
                    f"manifest:{uuid4().hex}", ids["task"], f"h5:{ids['run']}", ids["original"],
                    "a" * 64, "b" * 64, json.dumps({"artifacts": [], "preserved": True}),
                )
                before = await history(connection)
            finally:
                await connection.close()
        await migrate(url, "head")
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        try:
            async with pool.acquire() as connection:
                assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                    "20261023_0146"
                )
                assert await connection.fetchval(
                    'SELECT count(*) FROM qc_h5_browser_stop_receipt'
                ) == 0
                assert await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='qc_h5_session_journey_guard' "
                    "AND column_name='physical_stop_proof')"
                )
                for table in (
                    "qc_journey_checkpoint_attempt", "qc_journey_checkpoint_evidence",
                    "qc_journey_checkpoint_observation", "qc_journey_input_contract",
                ):
                    assert await connection.fetchval("SELECT to_regclass($1)", table)
                constraint = await connection.fetchval(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname='ck_qc_restoration_attempt_replay'"
                )
                assert "h5-new-session-route:" in constraint
                assert await connection.fetchval(
                    "SELECT count(*) FROM qc_h5_llm_call_budget"
                ) == 0
                if baseline != "base":
                    assert await history(connection) == before
            if baseline == "base":
                await migrate(url, "head")
                return
            repository = PostgresH5LLMBudgetRepository(pool=pool)
            deadline = datetime.now(UTC) + timedelta(minutes=180)
            await repository.initialize(
                task_id=ids["task"], agent_run_id=ids["run"], max_calls=200, deadline=deadline,
            )
            await repository.reserve(task_id=ids["task"], consumer_agent_run_id=ids["run"])
            async with pool.acquire() as connection:
                budget_before = dict(await connection.fetchrow(
                    "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"],
                ))
            await migrate(url, "head")
            async with pool.acquire() as connection:
                assert await history(connection) == before
                budget_after = dict(await connection.fetchrow(
                    "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"],
                ))
                assert budget_after == budget_before
                assert budget_after["max_calls"] == 200
                assert budget_after["deadline"] == deadline
                assert budget_after["admitted_calls"] == 1
                assert json.loads(before["qc_capture"])
                assert json.loads(before["qc_evidence_manifest"])
        finally:
            await pool.close()


@pytest.mark.parametrize("legacy_head", ["20261014_0133", "20261014_0134"])
async def test_applied_legacy_app_chain_is_not_mistaken_for_h5_or_silently_stamped(
    tmp_path, legacy_head,
):
    legacy = tmp_path / "legacy-migrations"
    shutil.copytree(ROOT / "services/api/migrations", legacy)
    versions = legacy / "versions"
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "services/api/migrations/alembic.ini")))
    descendants = {
        Path(item.path).name for item in scripts.iterate_revisions("head", "20261010_0132")
    }
    # 只在临时副本重建旧App图，真实执行原SQL；不修改任何当前或历史业务库。
    for path in versions.glob("*.py"):
        if path.name in descendants:
            if "rule_remark_copy" in path.name:
                source = path.read_text(encoding="utf-8").replace(
                    'revision = "20261016_0139"', 'revision = "20261014_0133"',
                ).replace('down_revision = "20261016_0138"', 'down_revision = "20261010_0132"')
                path.write_text(source, encoding="utf-8")
            elif "measurable_feedback_copy" in path.name:
                source = path.read_text(encoding="utf-8").replace(
                    'revision = "20261016_0140"', 'revision = "20261014_0134"',
                ).replace('down_revision = "20261016_0139"', 'down_revision = "20261014_0133"')
                path.write_text(source, encoding="utf-8")
            else:
                path.unlink()
    async with migration_database() as url:
        await migrate(url, "20261010_0132", script_location=legacy)
        connection = await asyncpg.connect(url)
        ids = {key: str(uuid4()) for key in (
            "tenant", "user", "ruleset", "rule", "task", "run", "work_item",
            "attempt", "original", "restored",
        )}
        try:
            await _seed(connection, ids)
            await connection.execute(
                "UPDATE qc_rule SET rule_code='alioth_5', judge_config='{}' WHERE id=$1::uuid",
                ids["rule"],
            )
        finally:
            await connection.close()
        await migrate(url, legacy_head, script_location=legacy)
        connection = await asyncpg.connect(url)
        try:
            assert await connection.fetchval(
                "SELECT to_regclass('qc_h5_session_journey_guard')"
            ) is None
            before = await history(connection)
            rule_before = dict(await connection.fetchrow(
                "SELECT * FROM qc_rule WHERE id=$1::uuid", ids["rule"],
            ))
            await migrate(url, "head", expected_error="旧迁移编号与H5结构不符")
            assert await connection.fetchval(
                "SELECT version_num FROM alembic_version"
            ) == legacy_head
            assert await history(connection) == before
            assert dict(await connection.fetchrow(
                "SELECT * FROM qc_rule WHERE id=$1::uuid", ids["rule"],
            )) == rule_before
        finally:
            await connection.close()


async def test_legacy_h5_budget_and_unconfirmed_stop_remain_frozen_through_app_chain():
    async with migration_database() as url:
        await migrate(url, "20261016_0138")
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        ids = {key: str(uuid4()) for key in (
            "tenant", "user", "ruleset", "rule", "task", "run", "work_item",
            "attempt", "original", "restored",
        )}
        session = f"h5:{ids['run']}"
        deadline = datetime.now(UTC) + timedelta(minutes=180)
        try:
            async with pool.acquire() as connection:
                await _seed(connection, ids)
                await connection.execute(
                    "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid", ids["task"],
                )
                await connection.execute(
                    "INSERT INTO qc_h5_session_journey_guard(token,task_id,session_id,status,"
                    "reason_code) VALUES($1::uuid,$2,$3,'quarantined','STOP_UNCONFIRMED')",
                    uuid4(), ids["task"], session,
                )
            repository = PostgresH5LLMBudgetRepository(pool=pool)
            await repository.initialize(
                task_id=ids["task"], agent_run_id=ids["run"], max_calls=200, deadline=deadline,
            )
            await repository.reserve(task_id=ids["task"], consumer_agent_run_id=ids["run"])
            async with pool.acquire() as connection:
                before = {table: dict(await connection.fetchrow(
                    f"SELECT * FROM {table} WHERE task_id=$1", ids["task"],
                )) for table in ("qc_h5_session_journey_guard", "qc_h5_llm_call_budget")}
                assert not await connection.fetchval(
                    "SELECT alioth_h5_journey_available($1)", session,
                )
            await migrate(url, "head")
            async with pool.acquire() as connection:
                after = {table: dict(await connection.fetchrow(
                    f"SELECT * FROM {table} WHERE task_id=$1", ids["task"],
                )) for table in before}
                assert after == before
                assert after["qc_h5_llm_call_budget"]["deadline"] == deadline
                assert after["qc_h5_llm_call_budget"]["max_calls"] == 200
                assert not await connection.fetchval(
                    "SELECT alioth_h5_journey_available($1)", session,
                )
        finally:
            await pool.close()
