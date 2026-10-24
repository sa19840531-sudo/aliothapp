"""在完整迁移后的隔离 PG 事务中验证任务删除、姓名筛选及对象边界，结束后回滚。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.workstation_task_deletion import WorkstationTaskDeletionService
from services.api.app.application.workstation_tasks import (
    InvalidTaskOperationError,
    TaskNotFoundError,
    WorkstationTaskListFilter,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.db.workstation_task_deletion_repository import (
    PostgresWorkstationTaskDeletionRepository,
)


class TransactionConnectionPool:
    def __init__(self, connection: asyncpg.Connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@dataclass
class ManagementDatabase:
    connection: asyncpg.Connection
    pool: TransactionConnectionPool
    tenant: str
    actor: str
    ruleset: str
    rule: str
    prefix: str

    @property
    def admin_scope(self):
        return AccessScope(
            actor_user_id=self.actor,
            tenant_id=self.tenant,
            permissions=frozenset({"taskObjects:read:any", "taskObjects:write:any"}),
        )


@pytest.fixture
async def database():
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    database_url = require_isolated_test_database_url(
        database_url, os.getenv("ALIOTH_DATABASE_URL") or None
    )
    connection = await asyncpg.connect(database_url)
    transaction = connection.transaction()
    await transaction.start()
    try:
        ids = {key: str(uuid4()) for key in ("tenant", "actor", "ruleset", "rule")}
        prefix = f"management-{uuid4()}"
        await connection.execute(
            "INSERT INTO qc_tenant (id, tenant_key, display_name) VALUES ($1::uuid, $2, $2)",
            ids["tenant"],
            prefix,
        )
        await connection.execute(
            """
            INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
            VALUES (
                $1::uuid, $2, '立川 Alpha', 'test-only-not-a-login-secret', '平台管理员', $3::uuid
            )
            """,
            ids["actor"],
            prefix,
            ids["tenant"],
        )
        await connection.execute(
            """
            INSERT INTO qc_ruleset (
                id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
            ) VALUES ($1::uuid, $2, $2, 1, 'enabled', 'test-snapshot', 1, 'manual')
            """,
            ids["ruleset"],
            prefix,
        )
        await connection.execute(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point
            ) VALUES (
                $1::uuid, $2::uuid, $3, '测试规则', 'visual', 'hybrid',
                'high', 'enabled', '检查页面'
            )
            """,
            ids["rule"],
            ids["ruleset"],
            prefix,
        )
        yield ManagementDatabase(
            connection, TransactionConnectionPool(connection), prefix=prefix, **ids
        )
    finally:
        await transaction.rollback()
        await connection.close()


async def _task(
    database: ManagementDatabase,
    *,
    status="done",
    run_status="done",
    cancel_requested=False,
    tenant=None,
    submitter=None,
    department="质检部",
):
    ids = {key: str(uuid4()) for key in ("task", "run", "execution")}
    config = {
        "task_name": database.prefix,
        "execution_mode": "full_inspection",
        "workflow_version": "v1",
        "product": {
            "department_name": department,
            "product_code": database.prefix,
            "product_name": "测试产品",
        },
        "journey_steps": [],
    }
    await database.connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, submitter_id, tenant_id,
            target_platform, department_name, task_config, started_at, finished_at
        ) VALUES (
            $1::uuid, 'https://example.test', $2::uuid, $3, $4::uuid, $5::uuid,
            'h5', $6, $7::jsonb, now() - interval '1 minute', now()
        )
        """,
        ids["task"],
        database.ruleset,
        status,
        submitter or database.actor,
        tenant or database.tenant,
        department,
        json.dumps(config),
    )
    await database.connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status, output_context)
        VALUES ($1::uuid, $2::uuid, 'main', '测试运行', $3, $4::jsonb)
        """,
        ids["run"],
        ids["task"],
        run_status,
        json.dumps({"cancel_requested": cancel_requested}),
    )
    await database.connection.execute(
        """
        INSERT INTO qc_rule_execution (
            id, task_id, rule_id, agent_run_id, execution_status, judge_strategy
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'pass', 'hybrid')
        """,
        ids["execution"],
        ids["task"],
        database.rule,
        ids["run"],
    )
    return ids


async def _add_user(database, full_name, *, tenant=None):
    user_id = str(uuid4())
    await database.connection.execute(
        """
        INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
        VALUES ($1::uuid, $1::text, $2, 'test-only-not-a-login-secret', '审计员', $3::uuid)
        """,
        user_id,
        full_name,
        tenant or database.tenant,
    )
    return user_id


@pytest.mark.parametrize("status", ["done", "review", "failed", "canceled"])
async def test_terminal_delete_preserves_material_and_records_audit(database, status):
    ids = await _task(database, status=status)
    await database.connection.execute(
        """
        INSERT INTO qc_evidence_artifact (
            task_id, artifact_type, source, storage_path, metadata,
            checksum, size_bytes, content_type
        )
        VALUES
            (
                $1::uuid, 'screenshot', 'baseline', 'tests/original.png',
                '{"original": true}'::jsonb, 'sha256:' || repeat('a', 64), 1, 'image/png'
            ),
            (
                $1::uuid, 'report', 'report', 'tests/original.html',
                '{"summary": "原始报告"}'::jsonb, 'sha256:' || repeat('b', 64), 1, 'text/html'
            )
        """,
        ids["task"],
    )
    material_queries = {
        "rules": "SELECT * FROM qc_rule_execution WHERE task_id = $1::uuid ORDER BY id",
        "artifacts": "SELECT * FROM qc_evidence_artifact WHERE task_id = $1::uuid ORDER BY id",
        "runs": "SELECT * FROM qc_agent_run WHERE task_id = $1::uuid ORDER BY id",
    }
    original = {
        name: await database.connection.fetch(sql, ids["task"])
        for name, sql in material_queries.items()
    }
    service = WorkstationTaskDeletionService(
        PostgresWorkstationTaskDeletionRepository(pool=database.pool)
    )
    result = await service.delete_task(ids["task"], access_scope=database.admin_scope)
    assert result.status == status
    deletion = await database.connection.fetchrow(
        "SELECT status, deleted_at, deleted_by::text FROM qc_task WHERE id = $1::uuid", ids["task"]
    )
    assert deletion["status"] == status
    assert deletion["deleted_at"] is not None
    assert deletion["deleted_by"] == database.actor
    for name, sql in material_queries.items():
        assert await database.connection.fetch(sql, ids["task"]) == original[name]
    audit = await database.connection.fetchrow(
        "SELECT source, message, context FROM qc_task_log WHERE task_id = $1::uuid", ids["task"]
    )
    assert audit["source"] == "management"
    assert audit["message"] == "管理员删除任务"
    assert json.loads(audit["context"]) == {
        "action": "task_delete",
        "actor_user_id": database.actor,
        "task_status": status,
        "deletion_mode": "soft",
    }
    for task_id in (ids["task"], str(uuid4())):
        with pytest.raises(TaskNotFoundError, match="任务不存在或已删除"):
            await service.delete_task(task_id, access_scope=database.admin_scope)
    assert (
        await database.connection.fetchval(
            "SELECT count(*) FROM qc_task_log WHERE task_id = $1::uuid", ids["task"]
        )
        == 1
    )


@pytest.mark.parametrize(
    ("status", "run_status", "cancel_requested"),
    [("queued", "pending", False), ("running", "running", False), ("running", "running", True)],
)
async def test_inflight_task_cannot_be_deleted(database, status, run_status, cancel_requested):
    ids = await _task(
        database, status=status, run_status=run_status, cancel_requested=cancel_requested
    )
    with pytest.raises(InvalidTaskOperationError):
        await WorkstationTaskDeletionService(
            PostgresWorkstationTaskDeletionRepository(pool=database.pool)
        ).delete_task(ids["task"], access_scope=database.admin_scope)
    assert (
        await database.connection.fetchval(
            "SELECT deleted_at FROM qc_task WHERE id = $1::uuid", ids["task"]
        )
        is None
    )
    assert (
        await database.connection.fetchval(
            "SELECT count(*) FROM qc_task_log WHERE task_id = $1::uuid", ids["task"]
        )
        == 0
    )


@pytest.mark.parametrize("run_status", ["pending", "running"])
async def test_terminal_task_with_active_run_cannot_be_deleted(database, run_status):
    ids = await _task(database, status="done", run_status=run_status)
    with pytest.raises(InvalidTaskOperationError, match="运行记录"):
        await WorkstationTaskDeletionService(
            PostgresWorkstationTaskDeletionRepository(pool=database.pool)
        ).delete_task(ids["task"], access_scope=database.admin_scope)
    assert (
        await database.connection.fetchval(
            "SELECT deleted_at FROM qc_task WHERE id = $1::uuid", ids["task"]
        )
        is None
    )


@pytest.mark.parametrize("report_status", [None, "ready"])
async def test_deleted_task_hidden_from_both_list_paths_and_detail(database, report_status):
    retained = await _task(database)
    deleted = await _task(database)
    query = PostgresWorkstationQueryRepository(pool=database.pool)
    filters = WorkstationTaskListFilter(task_name=database.prefix, report_status=report_status)
    before = await query.list_tasks(filters, access_scope=database.admin_scope)
    assert {item.task_id for item in before.items} == {retained["task"], deleted["task"]}
    assert before.total == 2
    assert (
        await query.get_task_detail(deleted["task"], access_scope=database.admin_scope) is not None
    )
    await WorkstationTaskDeletionService(
        PostgresWorkstationTaskDeletionRepository(pool=database.pool)
    ).delete_task(deleted["task"], access_scope=database.admin_scope)
    after = await query.list_tasks(filters, access_scope=database.admin_scope)
    assert [item.task_id for item in after.items] == [retained["task"]]
    assert after.total == 1
    assert await query.get_task_detail(deleted["task"], access_scope=database.admin_scope) is None
    assert (
        await query.get_task_detail(
            deleted["task"], access_scope=database.admin_scope, for_write=True
        )
        is None
    )


@pytest.mark.parametrize("report_status", [None, "ready"])
@pytest.mark.parametrize("role", ["platform", "company", "department"])
async def test_submitter_fuzzy_filter_preserves_tenant_department_and_owner_scope(
    database, report_status, role
):
    own = await _task(database, department="外部部")
    peer = await _add_user(database, "立川 Beta")
    same_department = await _task(database, submitter=peer)
    other_department = await _task(database, submitter=peer, department="研发部")
    unmatched_user = await _add_user(database, "其他人员")
    await _task(database, submitter=unmatched_user)
    other_tenant = str(uuid4())
    await database.connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $1::text, $1::text)
        """,
        other_tenant,
    )
    foreign_user = await _add_user(database, "立川 Gamma", tenant=other_tenant)
    foreign = await _task(database, tenant=other_tenant, submitter=foreign_user)
    permissions = {
        "platform": {"taskObjects:read:any", "taskObjects:write:any"},
        "company": {"taskObjects:read:tenant"},
        "department": {"taskObjects:read:department"},
    }
    scope = AccessScope(
        actor_user_id=database.actor,
        tenant_id=database.tenant,
        permissions=frozenset(permissions[role]),
        department_name="质检部",
    )
    expected = {own["task"], same_department["task"]}
    if role in {"platform", "company"}:
        expected.add(other_department["task"])
    if role == "platform":
        expected.add(foreign["task"])
    query = PostgresWorkstationQueryRepository(pool=database.pool)
    result = await query.list_tasks(
        WorkstationTaskListFilter(
            task_name=database.prefix, submitter_name="川", report_status=report_status
        ),
        access_scope=scope,
    )
    assert {item.task_id for item in result.items} == expected
    assert result.total == len(expected)
    case_insensitive = await query.list_tasks(
        WorkstationTaskListFilter(
            task_name=database.prefix, submitter_name="ALPHA", report_status=report_status
        ),
        access_scope=scope,
    )
    assert [item.task_id for item in case_insensitive.items] == [own["task"]]
    foreign_detail = await query.get_task_detail(foreign["task"], access_scope=scope)
    if role == "platform":
        assert foreign_detail is not None
    else:
        assert foreign_detail is None


async def test_deleted_quarantined_task_cannot_retry_finalization_or_update_status(database):
    retained = await _task(database)
    deleted = await _task(database)
    for ids in (retained, deleted):
        await database.connection.execute(
            """
            UPDATE qc_task
            SET target_platform = 'web',
                task_config = task_config || '{
                    "workflow_version": "v2",
                    "web_inspection_notification_mode": "enforced"
                }'::jsonb,
                finalization_recovery_state = 'quarantined',
                finalization_error_signature = 'test-only-quarantine',
                finalization_same_error_count = 3,
                finalization_quarantined_at = now()
            WHERE id = $1::uuid
            """,
            ids["task"],
        )
    query = PostgresWorkstationQueryRepository(pool=database.pool)
    restored = await query.retry_quarantined_finalization(
        task_id=retained["task"], access_scope=database.admin_scope
    )
    assert restored is not None
    assert (
        await database.connection.fetchval(
            "SELECT finalization_recovery_state FROM qc_task WHERE id = $1::uuid", retained["task"]
        )
        == "active"
    )

    await WorkstationTaskDeletionService(
        PostgresWorkstationTaskDeletionRepository(pool=database.pool)
    ).delete_task(deleted["task"], access_scope=database.admin_scope)
    snapshot = await database.connection.fetchrow(
        "SELECT * FROM qc_task WHERE id = $1::uuid", deleted["task"]
    )
    assert (
        await query.retry_quarantined_finalization(
            task_id=deleted["task"], access_scope=database.admin_scope
        )
        is None
    )
    assert (
        await query.update_task_status(
            task_id=deleted["task"],
            status="review",
            allowed_statuses=["done"],
            access_scope=database.admin_scope,
        )
        is None
    )
    assert (
        await database.connection.fetchrow(
            "SELECT * FROM qc_task WHERE id = $1::uuid", deleted["task"]
        )
        == snapshot
    )
