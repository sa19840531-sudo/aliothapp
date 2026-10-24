"""在独立连接临时表验证规则组合筛选及无关联组织删除，不读取或修改业务表。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.department_repository import PostgresDepartmentRepository
from services.api.app.infrastructure.db.rule_repository import PostgresRuleRepository
from services.api.app.infrastructure.db.tenant_repository import (
    DEFAULT_TENANT_ID,
    PostgresTenantRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


class ConnectionPool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def connection():
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置 ALIOTH_TEST_DATABASE_URL，且数据库已迁移至最新版本")
    database_url = require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL") or None,
    )
    database = await asyncpg.connect(database_url)
    try:
        yield database
    finally:
        await database.close()


async def _organization_tables(connection, *, temporary=True):
    ddl = """
        CREATE TEMP TABLE qc_tenant (id uuid PRIMARY KEY);
        CREATE TEMP TABLE qc_department (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid REFERENCES qc_tenant(id), name text
        );
        CREATE TEMP TABLE qc_product (
            tenant_id uuid REFERENCES qc_tenant(id), department_name text, status text
        );
        CREATE TEMP TABLE qc_user (
            tenant_id uuid REFERENCES qc_tenant(id), department_name text, status text
        );
        CREATE TEMP TABLE qc_task (
            tenant_id uuid REFERENCES qc_tenant(id), department_name text,
            status text, task_config jsonb
        );
        CREATE TEMP TABLE qc_platform_build (tenant_id uuid REFERENCES qc_tenant(id));
        CREATE TEMP TABLE qc_product_import_record (tenant_id uuid REFERENCES qc_tenant(id));
        CREATE TEMP TABLE qc_task_draft (tenant_id uuid REFERENCES qc_tenant(id));
        CREATE TEMP TABLE qc_feedback_ticket (tenant_id uuid REFERENCES qc_tenant(id));
        CREATE TEMP TABLE qc_task_submission (tenant_id uuid);
        CREATE TEMP TABLE qc_evidence_download_audit (tenant_id text);
    """
    await connection.execute(ddl if temporary else ddl.replace("CREATE TEMP TABLE", "CREATE TABLE"))


async def test_department_delete_counts_disabled_and_history_and_keeps_tenant_scope(connection):
    await _organization_tables(connection)
    tenant_id, other_tenant, dept_id = str(uuid4()), str(uuid4()), str(uuid4())
    await connection.execute(
        "INSERT INTO qc_tenant VALUES ($1::uuid), ($2::uuid)", tenant_id, other_tenant
    )
    await connection.execute(
        "INSERT INTO qc_department VALUES ($1::uuid, $2::uuid, '一部')", dept_id, tenant_id
    )
    await connection.execute(
        "INSERT INTO qc_product VALUES ($1::uuid, '一部', 'disabled')", tenant_id
    )
    await connection.execute("INSERT INTO qc_user VALUES ($1::uuid, '一部', 'disabled')", tenant_id)
    await connection.execute(
        """
        INSERT INTO qc_task VALUES
            ($1::uuid, '一部', 'done', '{}'),
            ($1::uuid, NULL, 'canceled', '{"product": {"department_name": "一部"}}')
    """,
        tenant_id,
    )
    repository = PostgresDepartmentRepository(pool=ConnectionPool(connection))
    forbidden = await repository.delete_department(dept_id, tenant_id=other_tenant)
    assert not forbidden.deleted and not forbidden.associations
    blocked = await repository.delete_department(dept_id, tenant_id=tenant_id)
    assert blocked.associations == {"产品": 1, "账号": 1, "任务": 2}
    assert await connection.fetchval("SELECT count(*) FROM qc_department") == 1
    await connection.execute("DELETE FROM qc_product; DELETE FROM qc_user; DELETE FROM qc_task")
    # 别的公司同名部门、共享产品不算本公司关联。
    await connection.execute(
        "INSERT INTO qc_product VALUES ($1::uuid, '一部', 'enabled'), (NULL, '一部', 'enabled')",
        other_tenant,
    )
    deleted = await repository.delete_department(dept_id, tenant_id=tenant_id)
    assert deleted.deleted
    assert await connection.fetchval("SELECT count(*) FROM qc_product") == 2
    assert not (await repository.delete_department(dept_id)).deleted


async def test_headquarters_shared_product_blocks_department_delete(connection):
    await _organization_tables(connection)
    dept_id = str(uuid4())
    await connection.execute("INSERT INTO qc_tenant VALUES ($1::uuid)", DEFAULT_TENANT_ID)
    await connection.execute(
        "INSERT INTO qc_department VALUES ($1::uuid, $2::uuid, '共享部')",
        dept_id,
        DEFAULT_TENANT_ID,
    )
    await connection.execute("INSERT INTO qc_product VALUES (NULL, '共享部', 'disabled')")
    result = await PostgresDepartmentRepository(pool=ConnectionPool(connection)).delete_department(
        dept_id
    )
    assert result.associations == {"产品": 1}


@pytest.mark.parametrize(
    "table,label",
    [
        ("qc_user", "账号"),
        ("qc_department", "部门"),
        ("qc_product", "产品"),
        ("qc_task", "任务"),
        ("qc_platform_build", "安装包"),
        ("qc_product_import_record", "产品导入记录"),
        ("qc_task_draft", "任务草稿"),
        ("qc_feedback_ticket", "反馈记录"),
        ("qc_task_submission", "任务提交记录"),
        ("qc_evidence_download_audit", "证据下载审计"),
    ],
)
async def test_company_delete_protects_each_business_and_audit_reference(connection, table, label):
    await _organization_tables(connection)
    tenant_id = str(uuid4())
    await connection.execute("INSERT INTO qc_tenant VALUES ($1::uuid)", tenant_id)
    # 表名只来自固定参数矩阵，不接受外部输入。
    cast = "text" if table == "qc_evidence_download_audit" else "uuid"
    await connection.execute(f"INSERT INTO {table} (tenant_id) VALUES ($1::{cast})", tenant_id)
    result = await PostgresTenantRepository(pool=ConnectionPool(connection)).delete_tenant(
        tenant_id
    )
    assert not result.deleted and result.associations == {label: 1}
    assert await connection.fetchval("SELECT count(*) FROM qc_tenant") == 1


async def test_company_delete_allows_empty_and_never_removes_headquarters(connection):
    await _organization_tables(connection)
    tenant_id = str(uuid4())
    await connection.execute(
        "INSERT INTO qc_tenant VALUES ($1::uuid), ($2::uuid)", tenant_id, DEFAULT_TENANT_ID
    )
    repository = PostgresTenantRepository(pool=ConnectionPool(connection))
    assert not (await repository.delete_tenant(DEFAULT_TENANT_ID)).deleted
    assert (await repository.delete_tenant(tenant_id)).deleted
    assert not (await repository.delete_tenant(tenant_id)).deleted
    assert await connection.fetchval("SELECT id::text FROM qc_tenant") == DEFAULT_TENANT_ID


async def test_rule_filters_combine_before_pagination_and_keep_exact_ruleset_version(connection):
    await connection.execute("""
        CREATE TEMP TABLE qc_ruleset (LIKE public.qc_ruleset INCLUDING ALL);
        CREATE TEMP TABLE qc_rule (LIKE public.qc_rule INCLUDING ALL);
    """)
    repository = PostgresRuleRepository(pool=ConnectionPool(connection))
    sets = []
    for version, source in [(1, "manual"), (2, "spreadsheet")]:
        sets.append(
            await repository.create_ruleset(
                {
                    "ruleset_code": "filter-test",
                    "name": "分期规则集",
                    "version": version,
                    "status": "enabled",
                    "source_type": source,
                }
            )
        )
    for code, version, risk, pages, legacy in [
        ("old", 0, "high", ["home"], None),
        ("high-home", 1, "high", ["home", "detail"], None),
        ("high-legacy", 1, "high", [], "home"),
        ("low-home", 1, "low", ["home"], None),
        ("high-detail", 1, "high", ["detail"], None),
    ]:
        await repository.create_rule(
            {
                "ruleset_id": sets[version]["id"],
                "rule_code": code,
                "name": code,
                "category": "text",
                "executor_type": "llm",
                "severity": risk,
                "status": "enabled",
                "check_point": "核验页面文案",
                "page_type": legacy,
                "applicable_page_types": pages,
            }
        )
    filters = dict(
        ruleset_id=sets[1]["id"],
        keyword=None,
        category=None,
        status=None,
        severity="high",
        source_type="spreadsheet",
        page_type="home",
        page_size=1,
    )
    items, total = await repository.list_rules(page=1, **filters)
    second, second_total = await repository.list_rules(page=2, **filters)
    assert total == second_total == 2
    assert {items[0]["rule_code"], second[0]["rule_code"]} == {"high-home", "high-legacy"}
    assert items[0]["ruleset_id"] == sets[1]["id"]
    assert items[0]["source_type"] == "spreadsheet" and items[0]["ruleset_version"] == 2
    empty, empty_total = await repository.list_rules(page=1, **{**filters, "source_type": "manual"})
    assert empty == [] and empty_total == 0
    empty, _ = await repository.list_rules(page=1, **{**filters, "keyword": "' OR TRUE --"})
    assert empty == []


@pytest.mark.parametrize("release_writer", [True, False])
async def test_department_delete_waits_for_inflight_association_then_rechecks(
    connection,
    release_writer,
):
    # 独立随机 schema 供两个连接共享；最终只清理本例创建的 schema。
    schema = f"test_org_delete_{uuid4().hex}"
    tenant_id, dept_id = str(uuid4()), str(uuid4())
    test_database_url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.getenv("ALIOTH_DATABASE_URL") or None,
    )
    sibling = await asyncpg.connect(test_database_url)
    previous_timeout = await connection.fetchval("SHOW lock_timeout")
    delete_task = None
    transaction = sibling.transaction()
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}", public')
        await sibling.execute(f'SET search_path TO "{schema}", public')
        await _organization_tables(connection, temporary=False)
        await connection.execute("INSERT INTO qc_tenant VALUES ($1::uuid)", tenant_id)
        await connection.execute(
            "INSERT INTO qc_department VALUES ($1::uuid, $2::uuid, '并发部')",
            dept_id,
            tenant_id,
        )
        await transaction.start()
        await sibling.execute(
            "INSERT INTO qc_product VALUES ($1::uuid, '并发部', 'disabled')",
            tenant_id,
        )
        repository = PostgresDepartmentRepository(pool=ConnectionPool(connection))
        delete_task = asyncio.create_task(repository.delete_department(dept_id))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(delete_task), timeout=0.1)
        if release_writer:
            await transaction.commit()
            result = await asyncio.wait_for(delete_task, timeout=3)
            assert not result.deleted and result.associations == {"产品": 1}
        else:
            # 不允许删除请求长时间持锁或等待，超时后整个事务回滚。
            with pytest.raises(asyncpg.LockNotAvailableError):
                await asyncio.wait_for(delete_task, timeout=3)
        assert await connection.fetchval("SELECT count(*) FROM qc_department") == 1
        assert await connection.fetchval("SHOW lock_timeout") == previous_timeout
    finally:
        if sibling.is_in_transaction():
            await transaction.rollback()
        if delete_task and not delete_task.done():
            delete_task.cancel()
            await asyncio.gather(delete_task, return_exceptions=True)
        await sibling.close()
        await connection.execute("SET search_path TO public")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
