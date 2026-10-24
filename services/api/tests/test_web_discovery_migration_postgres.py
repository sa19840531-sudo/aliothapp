"""在目标迁移的真实旧结构验证拒绝降级，不用当前 head 冒充旧库。"""

import asyncio
import json
import os
import subprocess
import sys
from uuid import uuid4

import asyncpg
import pytest

from services.api.tests.test_h5_release_migration_postgres import migration_database
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _seed_web_run,
    _test_database_url,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="只允许显式隔离PG"
)


def migrate(direction, revision, *, database_url=None):
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "-c",
         "services/api/migrations/alembic.ini", direction, revision],
        env={**os.environ, "ALIOTH_DATABASE_URL": database_url or _test_database_url()},
        capture_output=True, text=True, encoding="utf-8", timeout=45,
    )


@pytest.mark.asyncio
async def test_downgrade_refuses_valid_discovery_without_deleting_audit_then_roundtrips_empty():
    # 独占随机库，从真实 0102 DDL 开始，避免 0114 的谱系保护先于 0102 拒绝。
    async with migration_database() as url:
        result = await asyncio.to_thread(migrate, "upgrade", "20260910_0102", database_url=url)
        assert result.returncode == 0, result.stderr
        connection = await asyncpg.connect(url)
        try:
            ids = {
                key: str(uuid4())
                for key in ("tenant", "user", "ruleset", "task", "run", "capture")
            }
            ids["session"] = "web:" + ids["run"]
            await _seed_web_run(connection, ids)
            await connection.execute(
                """INSERT INTO qc_web_exploration_budget(
                    agent_run_id,site_id,task_id,session_id,target_origin_sha256,
                    catalog_version,catalog_sha256)
                VALUES($1::uuid,'isolated',$2::uuid,$3,$4,'historical-0102',$4)""",
                ids["run"], ids["task"], ids["session"], "a" * 64,
            )
            await connection.execute(
                """INSERT INTO qc_web_menu_ledger_entry(
                    agent_run_id,site_id,menu_entry_id,catalog_position,decision,
                    menu_path,canonical_url_sha256,status)
                VALUES($1::uuid,'isolated','catalog',1,'allowlisted_observe_only',
                    '["目录"]'::jsonb,$2,'menu_limit_skipped')""", ids["run"], "b" * 64,
            )
            manifest = {"version": "web-discovery-manifest.v1", "entry_id": "discovered",
                        "candidate": {"entry_id": "observed-news", "url": "https://example.test/news"}}
            await connection.execute(
                """INSERT INTO qc_web_menu_ledger_entry(
                    agent_run_id,site_id,menu_entry_id,catalog_position,decision,
                    menu_path,canonical_url_sha256,status,source_kind,
                    discovery_source_capture_id,discovery_manifest)
                VALUES($1::uuid,'isolated','discovered',2,'runtime_record_only',
                    '["资讯"]'::jsonb,$2,'runtime_discovered_unreviewed',
                    'runtime_discovered',$3::uuid,$4::jsonb)""",
                ids["run"], "c" * 64, ids["capture"], json.dumps(manifest),
            )
            entries_query = "SELECT * FROM qc_web_menu_ledger_entry ORDER BY menu_entry_id"
            version_query = "SELECT version_num FROM alembic_version"
            before = await connection.fetch(entries_query)
            result = await asyncio.to_thread(
                migrate, "downgrade", "20260909_0101", database_url=url,
            )
            assert result.returncode != 0
            assert "WEB_DISCOVERY_DOWNGRADE_REQUIRES_NO_DISCOVERY_RECORDS" in result.stderr
            assert await connection.fetchval(version_query) == "20260910_0102"
            assert await connection.fetch(entries_query) == before
            # 仅清除本隔离样本的发现行，再证明目录与其它审计不被空发现降级删除。
            await connection.execute(
                "DELETE FROM qc_web_menu_ledger_entry "
                "WHERE agent_run_id=$1::uuid AND menu_entry_id='discovered'",
                ids["run"],
            )
            result = await asyncio.to_thread(
                migrate, "downgrade", "20260909_0101", database_url=url,
            )
            assert result.returncode == 0, result.stderr
            assert await connection.fetchval(version_query) == "20260909_0101"
            assert await connection.fetchval("SELECT count(*) FROM qc_web_menu_ledger_entry") == 1
            result = await asyncio.to_thread(migrate, "upgrade", "head", database_url=url)
            assert result.returncode == 0, result.stderr
            restored = await connection.fetchrow(
                "SELECT * FROM qc_web_menu_ledger_entry WHERE menu_entry_id='catalog'",
            )
            assert all(restored[key] == value for key, value in dict(before[0]).items())
            assert await connection.fetchval(
                "SELECT count(*) FROM qc_capture WHERE id=$1::uuid", ids["capture"],
            ) == 1
        finally:
            await connection.close()
