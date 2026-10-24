"""真实 PostgreSQL 上验证补采请求状态写回。

这条链只有补采总开关打开后才会被调用，单测里的假仓储看不出问题：真库上
``transition`` 的 ``$2`` 一边被推成 varchar、一边被推成 text，语句连准备都过不了。
默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.evidence_requests import (
    EvidenceRequestStatus,
    TransitionEvidenceRequestCommand,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.tests.test_inspection_closure_settlement_postgres import (
    _seed_task,
    _seed_work_item,
)


async def test_transition_writes_terminal_status_and_reason_on_real_postgres() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    case = f"route-plan-{uuid4().hex[:8]}"
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        await _seed_work_item(
            connection,
            ids,
            name=case,
            page_seq=1,
            status="waiting_evidence",
            attempt_count=1,
            request_status="pending",
        )
        request_id = f"request-{case}"
        # 直接读版本号：这条用例只盯状态写回那条 SQL，不牵扯请求载荷解析。
        before = await connection.fetchrow(
            "SELECT status, row_version FROM qc_evidence_request WHERE request_id = $1",
            request_id,
        )
        assert before["status"] == "pending"

        repository = PostgresEvidenceRequestRepository(pool=pool)
        row_version = await repository.transition(
            command=TransitionEvidenceRequestCommand(
                request_id=request_id,
                expected_status=EvidenceRequestStatus.PENDING,
                target_status=EvidenceRequestStatus.UNFULFILLABLE,
                row_version=before["row_version"],
                resolution_reason="TRUSTED_RECOVERY_ROUTE_UNAVAILABLE",
            )
        )

        assert row_version == before["row_version"] + 1
        settled = await connection.fetchrow(
            """
            SELECT status, resolution_reason, completed_at
            FROM qc_evidence_request WHERE request_id = $1
            """,
            request_id,
        )
        assert settled["status"] == "unfulfillable"
        assert settled["resolution_reason"] == "TRUSTED_RECOVERY_ROUTE_UNAVAILABLE"
        # 终态必须落 completed_at，否则收口会一直把它当在途请求
        assert settled["completed_at"] is not None
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()
