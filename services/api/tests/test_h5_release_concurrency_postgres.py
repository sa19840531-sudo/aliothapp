"""多真实连接竞争同一补证请求，不能重领、刷新期限或覆盖取消。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.domain.inspection import EvidenceSessionDependency
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.tests.test_h5_release_migration_postgres import (
    migrate,
    migration_database,
)
from services.api.tests.test_h5_release_migration_postgres import (
    pytestmark as pytestmark,
)
from services.api.tests.test_targeted_recapture_chain_postgres import _request, _seed


@pytest.mark.parametrize("task_status", ["running", "canceled"])
async def test_twenty_consumers_cannot_duplicate_or_revive_snapshot_request(task_status):
    async with migration_database() as url:
        await migrate(url, "head")
        pool = await asyncpg.create_pool(url, min_size=2, max_size=5)
        try:
            ids = {key: str(uuid4()) for key in (
                "tenant", "user", "ruleset", "rule", "task", "run", "work_item",
                "attempt", "original", "restored",
            )}
            async with pool.acquire() as connection:
                await _seed(connection, ids)
                await connection.execute(
                    "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid", ids["task"],
                )
            requests = PostgresEvidenceRequestRepository(pool=pool, h5_snapshot_recovery=True)
            deadline = datetime.now(UTC) + timedelta(minutes=180)
            async with pool.acquire() as connection:
                await connection.execute(
                    "INSERT INTO qc_h5_llm_call_budget "
                    "(task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline) "
                    "VALUES($1::uuid,$2::uuid,0,200,0,$3)", ids["task"], ids["run"], deadline,
                )
                budget_before = dict(await connection.fetchrow(
                    "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"],
                ))
            request = _request(ids, rule_code="alioth_chain_1", expires_at=deadline)
            await requests.upsert(request=request)
            async with pool.acquire() as connection:
                await connection.execute(
                    "UPDATE qc_task SET status=$2 WHERE id=$1::uuid", ids["task"], task_status,
                )
                before = await connection.fetchrow(
                    "SELECT request_payload,expires_at,max_attempts FROM qc_evidence_request "
                    "WHERE request_id=$1", request.request_id,
                )
            results = await asyncio.wait_for(asyncio.gather(*(
                requests.claim_next(
                    task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0,
                    lease_owner=f"isolated-consumer-{index}", lease_seconds=30,
                    session_dependencies=(EvidenceSessionDependency.SNAPSHOT,),
                    h5_request_id=request.request_id,
                ) for index in range(20)
            )), timeout=15)
            leases = [result.lease for result in results if result.lease is not None]
            assert len(leases) == int(task_status == "running")
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT * FROM qc_evidence_request WHERE request_id=$1", request.request_id,
                )
                assert all(row[key] == before[key] for key in before.keys())
                assert row["attempt_count"] == len(leases)
                assert dict(await connection.fetchrow(
                    "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"],
                )) == budget_before
                if leases:
                    assert str(row["lease_token"]) == leases[0].lease_token
                else:
                    assert row["lease_token"] is None
                assert await connection.fetchval(
                    "SELECT count(*) FROM qc_llm_usage_event WHERE task_id=$1::uuid", ids["task"],
                ) == 0
        finally:
            await pool.close()
