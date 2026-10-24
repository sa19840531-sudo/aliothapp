"""独立库临时schema验证新增迁移，未知物理资源禁止被回滚抹去。"""

import importlib
from uuid import uuid4

import asyncpg
import pytest

from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库迁移夹具
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request


@pytest.mark.parametrize("status", ["active", "quarantined", "drained"])
async def test_guard_migration_preserves_unknown_resources_on_downgrade(monkeypatch, status):
    migration = importlib.import_module(
        "services.api.migrations.versions.20261014_0133_h5_journey_guard",
    )
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)
    async with _closed_request(check_constraint=False) as (connection, _, _, *_):
        schema = "guard_" + uuid4().hex
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET LOCAL search_path TO "{schema}",public')
        migration.upgrade()
        for sql in statements:
            await connection.execute(sql)
        token = uuid4()
        await connection.execute(
            "INSERT INTO qc_h5_session_journey_guard(token,task_id,session_id,status,drained_at) "
            "VALUES($1,'task','h5:migration',$2,CASE WHEN $2::varchar='drained' THEN now() END)",
            token, status,
        )
        statements.clear()
        migration.downgrade()
        if status != "drained":
            with pytest.raises(asyncpg.RaiseError, match="未排空"):
                async with connection.transaction():
                    for sql in statements:
                        await connection.execute(sql)
            assert await connection.fetchval(
                "SELECT status FROM qc_h5_session_journey_guard WHERE token=$1", token,
            ) == status
        else:
            for sql in statements:
                await connection.execute(sql)
            assert await connection.fetchval(
                "SELECT to_regclass($1)", schema + ".qc_h5_session_journey_guard",
            ) is None


@pytest.mark.parametrize("proven", [True, False])
async def test_stop_proof_migration_keeps_audit_on_downgrade(monkeypatch, proven):
    base = importlib.import_module(
        "services.api.migrations.versions.20261014_0133_h5_journey_guard",
    )
    migration = importlib.import_module(
        "services.api.migrations.versions.20261014_0134_h5_stop_proof",
    )
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)
    async with _closed_request(check_constraint=False) as (connection, _, _, *_):
        schema = "proof_" + uuid4().hex
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET LOCAL search_path TO "{schema}",public')
        base.upgrade()
        migration.upgrade()
        for sql in statements:
            await connection.execute(sql)
        await connection.execute(
            "INSERT INTO qc_h5_session_journey_guard(token,task_id,session_id,status,drained_at,"
            "physical_stop_proof) VALUES($1,'task','h5:migration','drained',now(),$2::jsonb)",
            uuid4(), '[{"resource_id":"isolated-test"}]' if proven else None,
        )
        statements.clear()
        migration.downgrade()
        if proven:
            with pytest.raises(asyncpg.RaiseError, match="审计"):
                async with connection.transaction():
                    for sql in statements:
                        await connection.execute(sql)
            assert await connection.fetchval(
                "SELECT physical_stop_proof IS NOT NULL FROM qc_h5_session_journey_guard",
            )
        else:
            for sql in statements:
                await connection.execute(sql)
            assert await connection.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema=$1 "
                "AND table_name='qc_h5_session_journey_guard' "
                "AND column_name='physical_stop_proof'",
                schema,
            ) == 0
