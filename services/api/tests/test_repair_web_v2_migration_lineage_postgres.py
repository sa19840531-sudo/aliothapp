"""隔离 PG 验证旧链补齐、数据保留、重复调用与事务回滚。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts import repair_web_v2_migration_lineage as repairer
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _database,
    _require_web_budget_test_database_url,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_run_stage_checkpoint_postgres import seed_current

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须使用显式隔离测试库"
)


@pytest.fixture(autouse=True)
def historical_lineage_database(monkeypatch, tmp_path):
    """旧链维修只能在实际 0121 结构验收，不能把当前头伪装成旧版本。"""
    base = _require_web_budget_test_database_url(os.environ)
    parsed = urlsplit(base)
    name = "alioth_test_lineage_" + uuid4().hex
    target = urlunsplit(parsed._replace(path="/" + name))

    async def create():
        connection = await asyncpg.connect(base)
        try:
            await connection.execute(f'CREATE DATABASE "{name}"')
        finally:
            await connection.close()

    asyncio.run(create())
    environment = {**os.environ, "ALIOTH_DATABASE_URL": target}
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "services/api/migrations/alembic.ini",
         "upgrade", repairer.TARGET],
        cwd=Path(__file__).resolve().parents[3], env=environment, capture_output=True, text=True,
    )
    (tmp_path / "historical-lineage-upgrade.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    monkeypatch.setenv("ALIOTH_TEST_DATABASE_URL", target)
    # 保留隔离数据库和迁移记录，不为让回滚转绿清理其他测试的封存输入。


def restore_legacy_schema(connection):
    # 新库内事务性反向执行新增的六项，保留所有 Web DDL 和真实证据行。
    with Operations.context(MigrationContext.configure(connection)):
        for filename in reversed(repairer.MISSING):
            repairer.load_migration(filename).downgrade()
    connection.execute(
        text("UPDATE alembic_version SET version_num=:version"), {"version": repairer.LEGACY}
    )
    assert repairer.validate_audit(repairer.audit(connection)) == "repair_required"


def schema_signature(connection):
    return (
        tuple(
            connection.execute(
                text("""
            SELECT table_name,column_name,data_type,is_nullable,column_default
            FROM information_schema.columns WHERE table_schema=current_schema()
            ORDER BY 1,2
        """)
            ).all()
        ),
        tuple(
            connection.execute(
                text("""
            SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint
            WHERE connamespace=current_schema()::regnamespace ORDER BY 1,2
        """)
            ).all()
        ),
    )


@pytest.mark.parametrize("fail_midway", [False, True])
async def test_repair_keeps_real_capture_and_checkpoint_and_rolls_back(monkeypatch, fail_midway):
    async with _database() as (control, pool, _other, ids):
        await seed_current(control, ids)
        repository = PostgresWebRunStageCheckpointRepository(pool=pool)
        await repository.create(
            WebStageCursor.start(
                run_id=ids["run"],
                logical_page_key="migration-sentinel",
                cursor_payload={"capture_id": ids["capture"]},
            )
        )
        url = _require_web_budget_test_database_url(os.environ)
        engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    expected = await connection.run_sync(schema_signature)
                    evidence = await connection.run_sync(repairer.protected_fingerprints)
                    await connection.run_sync(restore_legacy_schema)
                    assert (await connection.run_sync(repairer.repair))["applied"] is False
                    if fail_midway:
                        original = repairer.load_migration

                        def fail(filename):
                            if filename == repairer.MISSING[3]:
                                raise RuntimeError("注入第四步失败")
                            return original(filename)

                        monkeypatch.setattr(repairer, "load_migration", fail)
                        with pytest.raises(RuntimeError, match="注入"):
                            async with connection.begin_nested():
                                await connection.run_sync(
                                    lambda sync: repairer.repair(
                                        sync, apply=True, writers_stopped=True
                                    )
                                )
                        assert (
                            repairer.validate_audit(await connection.run_sync(repairer.audit))
                            == "repair_required"
                        )
                        assert (
                            await connection.run_sync(repairer.protected_fingerprints) == evidence
                        )
                    else:
                        result = await connection.run_sync(
                            lambda sync: repairer.repair(sync, apply=True, writers_stopped=True)
                        )
                        assert result["applied"] is True
                        assert len(result["migration_sha256"]) == 6
                        assert await connection.run_sync(schema_signature) == expected
                        assert (
                            await connection.run_sync(repairer.protected_fingerprints) == evidence
                        )
                        monkeypatch.setattr(
                            repairer,
                            "load_migration",
                            lambda _: pytest.fail("新版本重复调用不得重跑迁移"),
                        )
                        repeated = await connection.run_sync(
                            lambda sync: repairer.repair(sync, apply=True, writers_stopped=True)
                        )
                        assert repeated["applied"] is False
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()


@pytest.mark.parametrize(
    "damage",
    [
        "ALTER TABLE qc_web_run_stage_checkpoint DROP COLUMN claim_token CASCADE",
        "ALTER TABLE qc_web_run_stage_checkpoint DROP CONSTRAINT ck_qc_web_checkpoint_claim",
        "DROP INDEX ix_qc_web_checkpoint_lease_due",
        "CREATE OR REPLACE FUNCTION qc_web_checkpoint_gap_audit_valid(gaps jsonb,codes jsonb) "
        "RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT true'",
    ],
)
async def test_damaged_legacy_schema_cannot_be_marked_current(damage):
    url = _require_web_budget_test_database_url(os.environ)
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.run_sync(restore_legacy_schema)
                await connection.execute(text(damage))
                with pytest.raises(RuntimeError, match="结构"):
                    await connection.run_sync(
                        lambda sync: repairer.repair(sync, apply=True, writers_stopped=True)
                    )
                assert (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == repairer.LEGACY
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
