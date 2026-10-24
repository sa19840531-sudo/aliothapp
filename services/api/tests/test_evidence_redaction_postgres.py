"""隔离 PG 验证局部脱敏的可信来源、不可变冻结与审计事务。"""

import json
from datetime import UTC, datetime
from importlib import import_module
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.evidence_download_audit_repository import (
    PostgresEvidenceDownloadAuditRepository,
)
from services.api.app.infrastructure.db.evidence_redaction_repository import (
    PostgresEvidenceRedactionRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _test_database_url,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_evidence_export_audit_postgres import (
    _apply_actual_migration,
    export_event,
)

HASH = "sha256:" + "f" * 64


async def seed_source(connection, ids, *, with_capture=True, artifact_override=None):
    artifact_id = str(uuid4())
    capture_id = ids["capture"] if with_capture else None
    await connection.execute(
        "INSERT INTO qc_evidence_artifact (id,task_id,page_capture_id,artifact_type,source,"
        "storage_path,content_type,checksum,size_bytes) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,'screenshot','baseline','case.png','image/png',$4,42)",
        artifact_id,
        ids["task"],
        capture_id,
        HASH,
    )
    if artifact_override is not None:
        # 先写入不一致的历史输入，不绕过已通过清单的不可变保护。
        field, value = artifact_override
        await connection.execute(
            f"UPDATE qc_evidence_artifact SET {field}=$2 WHERE id=$1::uuid",
            artifact_id,
            value,
        )
    manifests = []
    if with_capture:
        for version in (1, 2):
            manifest = build_evidence_manifest_reference(
                task_id=ids["task"],
                session_id=ids["session"],
                capture_id=capture_id,
                manifest_version=version,
                quality_status=EvidenceQualityStatus.PASS,
                created_at=datetime.now(UTC),
                artifacts=(
                    ArtifactReference(
                        artifact_id=artifact_id,
                        artifact_type="screenshot",
                        role="primary",
                        storage_path="case.png",
                        checksum=HASH,
                        size_bytes=42,
                        content_type="image/png",
                    ),
                ),
            )
            await connection.execute(
                "INSERT INTO qc_evidence_manifest (manifest_id,manifest_version,task_id,"
                "session_id,capture_id,protocol_version,manifest_hash,payload_hash,"
                "quality_status,manifest_payload,created_at) "
                "VALUES ($1,$2,$3::uuid,$4,$5::uuid,'evidence-manifest.v1',$6,$7,'pass',"
                "$8::jsonb,$9)",
                manifest.manifest_id,
                version,
                ids["task"],
                ids["session"],
                capture_id,
                manifest.manifest_hash,
                stable_protocol_hash(manifest.to_dict()),
                json.dumps(manifest.to_dict()),
                manifest.created_at,
            )
            manifests.append(manifest)
    return artifact_id, manifests


def frozen_plan(ids, source):
    return {
        "redaction_id": str(uuid4()),
        "tenant_id": ids["tenant"],
        "source": source,
        "policy_version": "evidence-local-mask.v1",
        "spec": {"rectangles": [{"x": 0, "y": 0, "width": 10, "height": 10}]},
        "plan_hash": "e" * 64,
        "derived_checksum": HASH,
        "derived_size_bytes": 40,
        "derived_content_type": "image/png",
        "reviewed_by": ids["user"],
        "created_at": datetime.now(UTC).isoformat(),
        "business_reviewable": True,
    }


def audit_event(ids, plan, **overrides):
    return (
        dict(
            request_id=str(uuid4()),
            operation="create",
            artifact_id=plan["source"]["artifact_id"],
            task_id=ids["task"],
            actor_user_id=ids["user"],
            tenant_id=ids["tenant"],
            result="allowed",
            redaction_id=plan["redaction_id"],
            redaction_plan_hash=plan["plan_hash"],
            source_hash=plan["source"]["source_hash"],
            original_checksum=HASH,
            derived_checksum=HASH,
            policy_version=plan["policy_version"],
        )
        | overrides
    )


async def test_source_freezes_latest_or_explicit_historical_manifest_and_preserves_original():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        artifact_id, versions = await seed_source(connection, ids)
        original = await connection.fetchrow(
            "SELECT * FROM qc_evidence_artifact WHERE id=$1::uuid",
            artifact_id,
        )
        repo = PostgresEvidenceRedactionRepository(pool=pool)
        latest = await repo.source(artifact_id)
        old = await repo.source(
            artifact_id, manifest_id=versions[0].manifest_id, manifest_version=1
        )
        assert latest["manifest_version"] == 2 and old["manifest_version"] == 1
        assert latest["source_hash"] != old["source_hash"]
        assert latest["agent_run_id"] == ids["run"]
        plan = frozen_plan(ids, old)
        # 跨租户管理员创建时，计划及审计记录操作者租户，来源仍保留任务真实租户。
        plan["tenant_id"] = str(uuid4())
        event = audit_event(ids, plan, tenant_id=plan["tenant_id"])
        await repo.save(plan, PostgresEvidenceDownloadAuditRepository(pool=pool), event)
        assert await repo.get(plan["redaction_id"]) == plan
        assert await repo.list_for_artifact(artifact_id) == [plan]
        row = await connection.fetchrow(
            "SELECT * FROM qc_evidence_download_audit WHERE ticket_id=$1",
            event["request_id"],
        )
        assert row["operation"] == "redaction_create"
        assert row["tenant_id"] == plan["tenant_id"] != old["tenant_id"]
        assert str(row["redaction_id"]) == plan["redaction_id"]
        assert row["original_checksum"] == row["derived_checksum"] == HASH
        assert (
            await connection.fetchrow(
                "SELECT * FROM qc_evidence_artifact WHERE id=$1::uuid",
                artifact_id,
            )
            == original
        )
        for operation in (
            "UPDATE qc_evidence_redaction_plan SET plan_hash='changed' WHERE id=$1::uuid",
            "DELETE FROM qc_evidence_redaction_plan WHERE id=$1::uuid",
        ):
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await connection.execute(operation, plan["redaction_id"])


async def test_plan_creation_rolls_back_if_success_audit_cannot_be_recorded():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        artifact_id, _ = await seed_source(connection, ids)
        repo = PostgresEvidenceRedactionRepository(pool=pool)
        plan = frozen_plan(ids, await repo.source(artifact_id))
        audit = AsyncMock()
        audit.record_redaction.side_effect = RuntimeError("审计写入失败")
        with pytest.raises(RuntimeError, match="审计写入失败"):
            await repo.save(plan, audit, audit_event(ids, plan))
        assert await repo.get(plan["redaction_id"]) is None
        assert audit.record_redaction.call_args.kwargs["connection"] is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("redaction_id", str(uuid4())),
        ("actor_user_id", "other-actor"),
        ("tenant_id", "other-tenant"),
        ("source_hash", "e" * 64),
        ("result", "denied"),
    ],
)
async def test_creation_cannot_bind_success_audit_to_another_identity(field, value):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        artifact_id, _ = await seed_source(connection, ids)
        repo = PostgresEvidenceRedactionRepository(pool=pool)
        plan = frozen_plan(ids, await repo.source(artifact_id))
        with pytest.raises(ValueError, match="创建审计与冻结脱敏计划不一致"):
            await repo.save(
                plan,
                PostgresEvidenceDownloadAuditRepository(pool=pool),
                audit_event(ids, plan, **{field: value}),
            )
        assert await repo.get(plan["redaction_id"]) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("derived_checksum", "invalid"),
        ("derived_size_bytes", -1),
        ("policy_version", "unsupported"),
    ],
)
async def test_database_rejects_malformed_frozen_plan_without_audit(field, value):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        artifact_id, _ = await seed_source(connection, ids)
        repo = PostgresEvidenceRedactionRepository(pool=pool)
        plan = frozen_plan(ids, await repo.source(artifact_id))
        plan[field] = value
        event = audit_event(ids, plan)
        event["derived_checksum"] = plan["derived_checksum"]
        with pytest.raises(asyncpg.CheckViolationError):
            await repo.save(plan, PostgresEvidenceDownloadAuditRepository(pool=pool), event)
        assert await repo.get(plan["redaction_id"]) is None
        assert not await connection.fetchval(
            "SELECT count(*) FROM qc_evidence_download_audit WHERE ticket_id=$1",
            event["request_id"],
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("checksum", "sha256:" + "a" * 64),
        ("size_bytes", 43),
        ("content_type", "image/jpeg"),
        ("artifact_type", "dom"),
        ("storage_path", "other.png"),
    ],
)
async def test_source_rejects_artifact_manifest_member_mismatch(field, value):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        artifact_id, _ = await seed_source(connection, ids, artifact_override=(field, value))
        with pytest.raises(ValueError, match="成员内容不一致"):
            await PostgresEvidenceRedactionRepository(pool=pool).source(artifact_id)


async def test_source_rejects_unknown_manifest_and_accepts_unbound_legacy_original():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        artifact_id, _ = await seed_source(connection, ids)
        repo = PostgresEvidenceRedactionRepository(pool=pool)
        with pytest.raises(ValueError, match="可信 Manifest"):
            await repo.source(artifact_id, manifest_version=3)
        legacy, _ = await seed_source(connection, ids, with_capture=False)
        assert (await repo.source(legacy))["manifest_id"] is None
        with pytest.raises(ValueError, match="无 Capture"):
            await repo.source(legacy, manifest_version=1)
        assert await repo.source("invalid") is None
        assert await repo.get("invalid") is None
        assert await repo.list_for_artifact("invalid") == []


@pytest.mark.parametrize("operation", ["preview", "create", "read", "list"])
@pytest.mark.parametrize(
    "result,reason",
    [
        ("allowed", None),
        ("denied", "OBJECT_ACCESS_REVOKED"),
        ("failed", "SOURCE_MISMATCH"),
    ],
)
async def test_redaction_audit_records_each_result_and_operation(operation, result, reason):
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        artifact_id, _ = await seed_source(connection, ids)
        source = await PostgresEvidenceRedactionRepository(pool=pool).source(artifact_id)
        event = audit_event(
            ids, frozen_plan(ids, source), operation=operation, result=result, error_code=reason
        )
        await PostgresEvidenceDownloadAuditRepository(pool=pool).record_redaction(**event)
        row = await connection.fetchrow(
            "SELECT * FROM qc_evidence_download_audit WHERE ticket_id=$1",
            event["request_id"],
        )
        assert row["operation"] == f"redaction_{operation}"
        assert (row["result"], row["denied_reason"]) == (result, reason)


async def test_local_redaction_export_audit_keeps_each_plan_identity():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        event = export_event(ids)
        expected = {}
        for index, member in enumerate(event["materials"]):
            member["redaction_id"] = str(uuid4())
            member["redaction_plan_hash" if index == 0 else "plan_hash"] = "a" * 64
            expected[member["artifact_id"]] = member["redaction_id"]
        await PostgresEvidenceDownloadAuditRepository(pool=pool).record_export(**event)
        rows = await connection.fetch(
            "SELECT * FROM qc_evidence_download_audit WHERE export_request_id=$1",
            event["request_id"],
        )
        assert {row["artifact_id"]: str(row["redaction_id"]) for row in rows} == expected
        assert all(row["redaction_plan_hash"] == "a" * 64 for row in rows)


async def test_actual_local_redaction_migration_roundtrip_and_non_destructive_downgrade():
    modules = [
        import_module("services.api.migrations.versions." + name)
        for name in (
            "20260822_0075_evidence_download_audit",
            "20261023_0145_evidence_group_export_audit",
            "20261023_0146_evidence_local_redaction",
        )
    ]
    schema = "evidence_redaction_migration_test_" + uuid4().hex
    connection = await asyncpg.connect(_test_database_url())
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute("SELECT set_config('search_path',$1,false)", f"{schema},public")
        for module in modules:
            await _apply_actual_migration(connection, module, "upgrade")
        await _apply_actual_migration(connection, modules[-1], "downgrade")
        assert (
            await connection.fetchval(
                "SELECT to_regclass($1)",
                f"{schema}.qc_evidence_redaction_plan",
            )
            is None
        )
        await _apply_actual_migration(connection, modules[-1], "upgrade")
        transaction = connection.transaction()
        await transaction.start()
        try:
            ids = {key: str(uuid4()) for key in ("task", "user", "tenant")}
            source = {
                "artifact_id": str(uuid4()),
                "task_id": ids["task"],
                "source_hash": "f" * 64,
                "original_checksum": HASH,
            }
            plan = frozen_plan(ids, source)
            await connection.execute(
                "INSERT INTO qc_evidence_redaction_plan "
                "(id,artifact_id,task_id,tenant_id,plan_hash,plan_payload,created_by,created_at) "
                "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6::jsonb,$7,$8)",
                plan["redaction_id"],
                source["artifact_id"],
                ids["task"],
                ids["tenant"],
                plan["plan_hash"],
                json.dumps(plan),
                ids["user"],
                datetime.fromisoformat(plan["created_at"]),
            )
            with pytest.raises(asyncpg.RaiseError, match="plan or audit rows exist"):
                await _apply_actual_migration(connection, modules[-1], "downgrade")
            assert await connection.fetchval("SELECT count(*) FROM qc_evidence_redaction_plan") == 1
        finally:
            # 未提交夹具事务回滚，不通过删除或关闭触发器破坏不可变计划。
            await transaction.rollback()
        await connection.execute(
            "INSERT INTO qc_evidence_download_audit (artifact_id,actor_user_id,tenant_id,"
            "ticket_id,result,denied_reason,operation) "
            "VALUES ('unavailable','actor','tenant','request','denied','REVOKED','redaction_read')",
        )
        before = await connection.fetch("SELECT * FROM qc_evidence_download_audit")
        with pytest.raises(asyncpg.RaiseError, match="plan or audit rows exist"):
            await _apply_actual_migration(connection, modules[-1], "downgrade")
        assert await connection.fetch("SELECT * FROM qc_evidence_download_audit") == before
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            await connection.execute("DELETE FROM qc_evidence_download_audit")
    finally:
        # 仅移除本用例刚创建的唯一 schema，不触及 public 中保留的证据和审计。
        assert schema.startswith("evidence_redaction_migration_test_")
        await connection.execute("SET search_path TO public")
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()
