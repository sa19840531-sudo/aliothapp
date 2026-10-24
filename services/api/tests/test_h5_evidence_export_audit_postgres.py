"""真实隔离 PG 验证复用下载审计的事务性、错误分类和不可变追溯。"""

from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.evidence_download_audit_repository import (
    PostgresEvidenceDownloadAuditRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _test_database_url,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)

HASH = "sha256:" + "f" * 64


def export_event(ids, **overrides):
    return {
        "request_id": str(uuid4()),
        "task_id": ids["task"],
        "execution_plan_id": "plan",
        "snapshot_hash": "f" * 64,
        "mode": "redacted",
        "policy_version": "evidence-conservative-mask.v1",
        "actor_user_id": ids["user"],
        "tenant_id": ids["tenant"],
        "artifact_ids": ["original-a", "original-b"],
        "materials": [
            {"artifact_id": value, "original_checksum": HASH, "checksum": HASH}
            for value in ["original-a", "original-b"]
        ],
        "archive_checksum": HASH,
        "result": "allowed",
        **overrides,
    }


@pytest.mark.parametrize(
    "result,reason",
    [("allowed", None), ("failed", "ARTIFACT_MISSING"), ("denied", "OBJECT_ACCESS_REVOKED")],
)
async def test_export_members_share_request_and_audit_remains_immutable(result, reason):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        audit = PostgresEvidenceDownloadAuditRepository(pool=pool)
        event = export_event(ids, result=result, error_code=reason)
        await audit.record_export(**event)
        rows = await connection.fetch(
            "SELECT * FROM qc_evidence_download_audit WHERE export_request_id=$1",
            event["request_id"],
        )
        assert len(rows) == 2
        assert {row["ticket_id"] for row in rows} == {event["request_id"]}
        assert {row["result"] for row in rows} == {result}
        assert {row["denied_reason"] for row in rows} == {reason}
        assert all(
            row["export_mode"] == "redacted"
            and row["original_checksum"] == HASH
            and row["archive_checksum"] == HASH
            for row in rows
        )
        for operation in [
            "UPDATE qc_evidence_download_audit SET result='denied' WHERE id=$1",
            "DELETE FROM qc_evidence_download_audit WHERE id=$1",
        ]:
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await connection.execute(operation, rows[0]["id"])


async def test_late_member_audit_failure_rolls_back_entire_group():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        audit = PostgresEvidenceDownloadAuditRepository(pool=pool)
        event = export_event(
            ids,
            artifact_ids=["a-valid-first", "z" * 65],
            result="failed",
            error_code="ARTIFACT_MISSING",
        )
        with pytest.raises(asyncpg.StringDataRightTruncationError):
            await audit.record_export(**event)
        assert not await connection.fetchval(
            "SELECT count(*) FROM qc_evidence_download_audit WHERE export_request_id=$1",
            event["request_id"],
        )


async def test_denied_unknown_group_and_legacy_file_audit_both_remain_supported():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        audit = PostgresEvidenceDownloadAuditRepository(pool=pool)
        event = export_event(
            ids,
            artifact_ids=[],
            materials=[],
            result="denied",
            error_code="OBJECT_ACCESS_DENIED",
            archive_checksum=None,
        )
        await audit.record_export(**event)
        assert (
            await connection.fetchval(
                "SELECT artifact_id FROM qc_evidence_download_audit WHERE export_request_id=$1",
                event["request_id"],
            )
            == f"group:{ids['task']}"
        )
        await audit.record(
            artifact_id="legacy-file",
            actor_user_id=ids["user"],
            tenant_id=ids["tenant"],
            ticket_id=str(uuid4()),
            result="allowed",
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_download_audit "
                "WHERE actor_user_id=$1 AND export_mode IS NULL",
                ids["user"],
            )
            == 1
        )


async def _apply_actual_migration(connection, module, direction):
    """收集迁移实际发出的语句，并在真实 PG 事务内逐条执行。"""
    statements = []
    with patch.object(module.op, "execute", side_effect=statements.append):
        getattr(module, direction)()
    async with connection.transaction():
        for statement in statements:
            await connection.execute(str(statement))


async def _schema_contract(connection, schema):
    columns = await connection.fetch(
        "SELECT column_name,data_type,is_nullable,column_default "
        "FROM information_schema.columns WHERE table_schema=$1 "
        "AND table_name='qc_evidence_download_audit' ORDER BY ordinal_position",
        schema,
    )
    constraints = await connection.fetch(
        "SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conrelid='qc_evidence_download_audit'::regclass ORDER BY conname",
    )
    indexes = await connection.fetch(
        "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=$1 "
        "AND tablename='qc_evidence_download_audit' ORDER BY indexname",
        schema,
    )
    triggers = await connection.fetch(
        "SELECT tgname,pg_get_triggerdef(oid) AS definition FROM pg_trigger "
        "WHERE tgrelid='qc_evidence_download_audit'::regclass AND NOT tgisinternal "
        "ORDER BY tgname",
    )
    return {
        "columns": list(columns),
        "constraints": list(constraints),
        "indexes": list(indexes),
        "triggers": list(triggers),
    }


async def test_actual_export_migration_roundtrip_and_data_preserving_downgrade_refusal():
    baseline = import_module(
        "services.api.migrations.versions.20260822_0075_evidence_download_audit"
    )
    migration = import_module(
        "services.api.migrations.versions.20261023_0145_evidence_group_export_audit"
    )
    schema = "evidence_export_migration_test_" + uuid4().hex
    connection = await asyncpg.connect(_test_database_url())
    created = False
    try:
        # 唯一 schema 内重建真实旧下载审计基线，不触及 public 或其他用例数据。
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await connection.execute("SELECT set_config('search_path',$1,false)", f"{schema},public")
        assert await connection.fetchval("SELECT current_schema()") == schema
        await _apply_actual_migration(connection, baseline, "upgrade")
        assert (
            await connection.fetchval(
                "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.oid='qc_evidence_download_audit'::regclass",
            )
            == schema
        )
        await _apply_actual_migration(connection, migration, "upgrade")
        upgraded = await _schema_contract(connection, schema)
        assert "export_request_id" in {row["column_name"] for row in upgraded["columns"]}
        await _apply_actual_migration(connection, migration, "downgrade")
        downgraded = await _schema_contract(connection, schema)
        assert "export_request_id" not in {row["column_name"] for row in downgraded["columns"]}
        assert downgraded["triggers"] == upgraded["triggers"]
        assert not await connection.fetchval("SELECT count(*) FROM qc_evidence_download_audit")
        await _apply_actual_migration(connection, migration, "upgrade")
        assert await _schema_contract(connection, schema) == upgraded

        request_id = str(uuid4())
        await connection.execute(
            "INSERT INTO qc_evidence_download_audit (artifact_id,actor_user_id,tenant_id,"
            "ticket_id,result,denied_reason,export_request_id,task_id,execution_plan_id,"
            "snapshot_hash,export_mode,policy_version) "
            "VALUES ('owned-original','owned-actor','owned-tenant',$1,'failed','ARTIFACT_MISSING',"
            "$1,'owned-task','owned-plan',$2,'original','evidence-original.v1')",
            request_id,
            "f" * 64,
        )
        original_row = await connection.fetchrow("SELECT * FROM qc_evidence_download_audit")
        with pytest.raises(asyncpg.RaiseError, match="cannot remove evidence export audit"):
            await _apply_actual_migration(connection, migration, "downgrade")
        assert await _schema_contract(connection, schema) == upgraded
        assert await connection.fetch("SELECT * FROM qc_evidence_download_audit") == [original_row]
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            await connection.execute(
                "UPDATE qc_evidence_download_audit SET denied_reason='changed' WHERE id=$1",
                original_row["id"],
            )
    finally:
        try:
            if created:
                # 只清理本用例刚创建的独占 schema，保留主测试库现存不可变审计。
                assert schema.startswith("evidence_export_migration_test_")
                await connection.execute("SET search_path TO public")
                await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
                assert await connection.fetchval("SELECT to_regnamespace($1)", schema) is None
        finally:
            await connection.close()
