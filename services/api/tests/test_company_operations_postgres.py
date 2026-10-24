"""在连接级临时表上验证公司名称约束和已生成报告任务平均耗时，不写业务表。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.operations_repository import PostgresOperationsRepository
from services.api.app.infrastructure.db.tenant_repository import (
    TENANT_KEY_UNIQUE_CONSTRAINT,
    TENANT_NAME_UNIQUE_INDEX,
    PostgresTenantRepository,
)


class ConnectionPool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def connection():
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置 ALIOTH_TEST_DATABASE_URL；测试仅创建连接级临时表")
    database = await asyncpg.connect(database_url)
    try:
        yield database
    finally:
        await database.close()


async def test_normalized_company_name_index_and_repository(connection) -> None:
    await connection.execute(
        f"""
        CREATE TEMP TABLE qc_tenant (
            id uuid PRIMARY KEY,
            tenant_key text NOT NULL,
            display_name text NOT NULL,
            status text NOT NULL,
            contact_name text,
            remark text,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            CONSTRAINT {TENANT_KEY_UNIQUE_CONSTRAINT} UNIQUE (tenant_key)
        );
        CREATE UNIQUE INDEX {TENANT_NAME_UNIQUE_INDEX}
            ON qc_tenant (LOWER(BTRIM(display_name)));
        CREATE TEMP TABLE qc_user (
            id uuid, tenant_id uuid, username text, role text, created_at timestamptz
        );
        CREATE TEMP TABLE qc_department (tenant_id uuid);
        CREATE TEMP TABLE qc_product (tenant_id uuid);
        """
    )
    repository = PostgresTenantRepository(pool=ConnectionPool(connection))
    first = await repository.create_tenant({"tenant_key": "east", "display_name": " East Branch "})
    assert (await repository.find_by_display_name("east branch"))["id"] == first["id"]

    with pytest.raises(asyncpg.UniqueViolationError) as duplicate:
        await repository.create_tenant({"tenant_key": "other", "display_name": "EAST BRANCH"})
    assert duplicate.value.constraint_name == TENANT_NAME_UNIQUE_INDEX

    second = await repository.create_tenant({"tenant_key": "west", "display_name": "West Branch"})
    with pytest.raises(asyncpg.UniqueViolationError) as renamed:
        await repository.update_tenant(
            second["id"], {"display_name": "east branch ", "status": "enabled"}
        )
    assert renamed.value.constraint_name == TENANT_NAME_UNIQUE_INDEX
    assert (await repository.find_by_id(second["id"]))["display_name"] == "West Branch"


async def _create_operations_tables(connection) -> None:
    await connection.execute(
        """
        CREATE TEMP TABLE qc_task (
            tenant_id uuid, submitter_id uuid, target_platform text, status text,
            quality_outcome text, created_at timestamptz,
            started_at timestamptz, finished_at timestamptz
        );
        CREATE TEMP TABLE qc_user (
            id uuid, username text, tenant_id uuid, last_login_at timestamptz
        );
        """
    )


async def test_report_duration_samples_preserve_filters_and_exclude_invalid_tasks(connection):
    await _create_operations_tables(connection)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=7)
    tenant_id = str(uuid4())
    other_tenant = str(uuid4())
    created = start + timedelta(days=1)
    began = created + timedelta(hours=1)
    for status, seconds, quality in (
        ("done", 120, "pass"),
        ("done", 180, "fail"),
        ("done", 0, "unknown"),
        ("review", 900, "mixed"),
        ("failed", 600, "unknown"),
        ("canceled", 300, "unknown"),
        ("running", 60, "unknown"),
        ("done", -60, "unknown"),
    ):
        await connection.execute(
            """
            INSERT INTO qc_task (
                tenant_id, target_platform, status, quality_outcome,
                created_at, started_at, finished_at
            ) VALUES ($1::uuid, 'h5', $2, $3, $4, $5, $6)
            """,
            tenant_id, status, quality, created, began, began + timedelta(seconds=seconds),
        )
    await connection.execute(
        """
        INSERT INTO qc_task (
            tenant_id, target_platform, status, created_at, started_at, finished_at
        )
        VALUES
            ($1::uuid, 'h5', 'done', $3, NULL, $4),
            ($1::uuid, 'h5', 'done', $3, $4, NULL),
            ($1::uuid, 'h5', 'done', $3, $4, 'infinity'),
            ($1::uuid, 'h5', 'done', $3, '-infinity', $4),
            ($2::uuid, 'h5', 'done', $3, $4, $5),
            ($1::uuid, 'web', 'done', $3, $4, $5),
            ($1::uuid, 'h5', 'done', $6, $4, $5)
        """,
        tenant_id, other_tenant, created, began, began + timedelta(hours=10), end,
    )
    summary = await PostgresOperationsRepository(pool=ConnectionPool(connection)).summarize_window(
        start=start, end=end, tenant_id=tenant_id, platform="h5"
    )
    # 已完成、待复核报告及合法零耗时均计入，失败取消和非法时间不参与平均。
    assert summary["avg_duration_seconds"] == pytest.approx(300)
    assert summary["task_count"] == 12
    assert summary["review_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["canceled_count"] == 1


async def test_report_duration_remains_available_when_all_reports_await_review(connection) -> None:
    await _create_operations_tables(connection)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    await connection.execute(
        """
        INSERT INTO qc_task (target_platform, status, created_at, started_at, finished_at)
        VALUES
            ('h5', 'review', $1, $1, $1::timestamptz + interval '1 minute'),
            ('web', 'review', $1, $1, $1::timestamptz + interval '3 minutes')
        """,
        start,
    )
    summary = await PostgresOperationsRepository(pool=ConnectionPool(connection)).summarize_window(
        start=start, end=start + timedelta(days=1), tenant_id=None, platform=None
    )
    assert summary["done_count"] == 0
    assert summary["review_count"] == summary["report_count"] == 2
    assert summary["avg_duration_seconds"] == pytest.approx(120)


async def test_report_duration_is_unknown_without_valid_report_sample(connection) -> None:
    await _create_operations_tables(connection)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    await connection.execute(
        """
        INSERT INTO qc_task (target_platform, status, created_at, started_at, finished_at)
        VALUES
            ('h5', 'review', $1, $1, NULL),
            ('h5', 'done', $1, NULL, $1::timestamptz + interval '1 minute')
        """,
        start,
    )
    summary = await PostgresOperationsRepository(pool=ConnectionPool(connection)).summarize_window(
        start=start, end=start + timedelta(days=1), tenant_id=None, platform=None
    )
    assert summary["avg_duration_seconds"] is None
