"""随机隔离schema验证补证回队及租约，绝不连接当前验收数据库。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from services.api.app.application.evidence_requests import EvidenceRequestProcessingLease
from services.api.app.application.h5_recapture_lease import (
    H5RecaptureDeadlineError,
    run_h5_recapture_stage,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.h5_llm_budget_repository import (
    PostgresH5LLMBudgetRepository,
)
from services.api.app.infrastructure.db.h5_shared_probe_repository import (
    PostgresH5SharedProbeEvidenceReader,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 仅迁移新建的隔离数据库
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request
from services.api.tests.test_h5_llm_budget_postgres import database as database
from services.api.tests.test_h5_llm_budget_postgres import seed
from services.api.tests.test_h5_recapture_coalescing import probe, request


async def _shared_source(pool, fault=None):
    task, run, _ = await seed(pool)
    source, current, token = (uuid4() for _ in range(3))
    item = probe(request(task_id=task, target_capture_id=str(source)),
                 source_capture_id=str(source), session_id=f"h5:{run}")
    async with pool.acquire() as connection:
        await connection.execute("""
            ALTER TABLE qc_task ADD finalization_state text DEFAULT 'open';
            CREATE TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,
                surface_session_id text,surface_kind text DEFAULT 'web',
                capture_status text DEFAULT 'captured');
            CREATE TABLE qc_surface_session(session_id text,task_id uuid,status text,
                surface_kind text,finalization_epoch int,current_capture_id uuid);
            CREATE TABLE qc_v2_work_item(work_id text,task_id uuid,status text,lease_token uuid,
                row_version int,lease_expires_at timestamptz,work_type text,work_payload jsonb,
                session_id text,source_capture_id uuid,evidence_request_id text,
                finalization_epoch int,last_error_code text,last_error_message text,
                lease_owner text,finished_at timestamptz,updated_at timestamptz);
            CREATE TABLE qc_evidence_request(request_id text,task_id uuid,finalization_epoch int,
                status text,lease_token uuid,attempt_count int,max_attempts int,
                expires_at timestamptz,resolution_reason text,row_version int,
                next_attempt_at timestamptz DEFAULT now(),updated_at timestamptz,
                completed_at timestamptz,lease_owner text,lease_expires_at timestamptz,
                target_capture_id uuid,request_payload jsonb,
                session_dependency text DEFAULT 'snapshot',created_at timestamptz DEFAULT now());
            CREATE TABLE qc_action_execution(task_id uuid,intent_payload jsonb);
            CREATE TABLE qc_restoration_attempt(attempt_id text,request_id text,task_id uuid,
                finalization_epoch int,attempt_no int,status text,assessment_payload jsonb,
                equivalence_assessment_id text,restored_capture_id uuid);
        """)
        await connection.execute("INSERT INTO qc_capture(id,task_id,agent_run_id,"
                                 "surface_session_id) VALUES($1,$2,$3,$4)",
                                 source, UUID(task), UUID(run), item.session_id)
        await connection.execute(
            "INSERT INTO qc_surface_session VALUES($1,$2,'active','web',4,$3)",
            item.session_id, UUID(task), current,
        )
        await connection.execute(
            """INSERT INTO qc_v2_work_item(work_id,task_id,status,lease_token,row_version,
                lease_expires_at,work_type,work_payload,session_id,source_capture_id,
                evidence_request_id,finalization_epoch)
                VALUES($1,$2,'running',$3,3,now()+interval '2 minutes','probe',$4,$5,$6,$7,4)""",
            item.work_id, UUID(task), token, json.dumps(item.to_dict()), item.session_id,
            source, item.evidence_request.request_id,
        )
        await connection.execute(
            """INSERT INTO qc_evidence_request(request_id,task_id,finalization_epoch,status,
                attempt_count,max_attempts,expires_at,row_version)
                VALUES($1,$2,4,'running',$3,3,now()+$4::text::interval,5)""",
            item.evidence_request.request_id, UUID(task), 3 if fault == "attempts" else 1,
            "-1 second" if fault == "expired" else "1 hour",
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET target_capture_id=$1,request_payload=$2",
            source, json.dumps(item.evidence_request.to_dict()),
        )
        if fault == "token":
            token = uuid4()
        elif fault == "epoch":
            await connection.execute("UPDATE qc_task SET finalization_epoch=5")
        elif fault == "cancelled":
            await connection.execute("UPDATE qc_task SET status='cancelled'")
        elif fault == "action":
            await connection.execute(
                "INSERT INTO qc_action_execution VALUES($1,$2)", UUID(task),
                json.dumps({"parameters": {"exploration_work_id": item.work_id}}),
            )
        elif fault == "request_lease":
            await connection.execute("UPDATE qc_evidence_request SET lease_token=$1", uuid4())
    return item, run, str(token)


@pytest.mark.parametrize("fault", [None, "attempts", "expired", "token", "epoch", "cancelled",
                                   "action", "request_lease"])
async def test_stale_unexecuted_probe_does_not_strand_exhausted_pending_request(database, fault):
    item, run, token = await _shared_source(database, fault)
    async with database.acquire() as connection:
        before = await connection.fetchrow("SELECT * FROM qc_evidence_request")
    changed = await PostgresH5SharedProbeEvidenceReader(pool=database).release_stale_unexecuted(
        work=item, agent_run_id=run, lease_token=token, row_version=3,
    )
    allowed = fault in {None, "attempts", "expired"}
    assert changed is allowed
    async with database.acquire() as connection:
        after = await connection.fetchrow("SELECT * FROM qc_evidence_request")
        work = await connection.fetchrow("SELECT * FROM qc_v2_work_item")
    if not allowed:
        assert after == before
        assert work["status"] == "running"
    else:
        expected = "unfulfillable" if fault == "attempts" else "expired" if fault == "expired" \
            else "pending"
        assert after["status"] == expected
        assert (after["completed_at"] is not None) is (expected != "pending")
        assert after["attempt_count"] == before["attempt_count"]
        assert after["max_attempts"] == before["max_attempts"]
        assert after["expires_at"] == before["expires_at"]
        assert work["status"] == "superseded"


@pytest.mark.parametrize("fault", [None, "token", "version", "epoch", "run", "expired",
                                   "deadline", "cancelled", "routed_without_proof"])
async def test_h5_request_renewal_preserves_frozen_contract_and_rejects_old_owner(fault):
    async with _closed_request(task_status="running", check_constraint=False) as fixture:
        connection, _, ids, request, _, _, requests, lease = fixture
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        # 必须带 WHERE：共享库可能残留脱敏等其它 request，全表 UPDATE 会踩
        # ck_qc_evidence_request_processing_lease（空租约行被写成半租约）。
        if fault == "token":
            await connection.execute(
                "UPDATE qc_evidence_request SET lease_token=gen_random_uuid() "
                "WHERE request_id=$1",
                request.request_id,
            )
        elif fault == "version":
            await connection.execute(
                "UPDATE qc_evidence_request SET row_version=row_version+1 WHERE request_id=$1",
                request.request_id,
            )
        elif fault == "epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1", ids["task"],
            )
        elif fault == "expired":
            await connection.execute(
                "UPDATE qc_evidence_request SET lease_expires_at=now() WHERE request_id=$1",
                request.request_id,
            )
        elif fault == "deadline":
            deadline = datetime.now(UTC) - timedelta(seconds=1)
        elif fault == "cancelled":
            await connection.execute(
                "UPDATE qc_task SET status='canceled' WHERE id=$1", ids["task"],
            )
        elif fault == "routed_without_proof":
            await connection.execute(
                "UPDATE qc_evidence_request SET status='routed',"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                "row_version=row_version+1 WHERE request_id=$1",
                request.request_id,
            )
        before = await connection.fetchrow("SELECT * FROM qc_evidence_request WHERE request_id=$1",
                                           request.request_id)
        accepted = await requests.renew_h5_lease(
            lease=lease, agent_run_id=str(uuid4()) if fault == "run" else ids["run"],
            lease_seconds=240, deadline=deadline,
        )
        assert accepted is (fault is None)
        after = await connection.fetchrow("SELECT * FROM qc_evidence_request WHERE request_id=$1",
                                          request.request_id)
        if fault is not None:
            assert after == before
        else:
            assert after["lease_expires_at"] <= deadline
            for key in set(before.keys()) - {"lease_expires_at", "updated_at"}:
                assert after[key] == before[key]


@pytest.mark.parametrize("handoff", [None, "valid", "foreign_attempt", "failed_assessment",
                                     "missing_capture"])
async def test_real_claim_competition_and_fenced_publication_handoff(database, handoff):
    item, run, _ = await _shared_source(database)
    deadline = datetime.now(UTC) + timedelta(minutes=180)
    budget = PostgresH5LLMBudgetRepository(pool=database)
    await budget.initialize(
        task_id=item.task_id, agent_run_id=run, max_calls=200, deadline=deadline,
    )
    async with database.acquire() as connection:
        await connection.execute("UPDATE qc_evidence_request SET status='pending',attempt_count=0")
        frozen = await connection.fetchrow("SELECT * FROM qc_h5_llm_call_budget")
    requests = PostgresEvidenceRequestRepository(pool=database)
    results = await asyncio.gather(*(
        requests.claim_next(task_id=item.task_id, agent_run_id=run, finalization_epoch=4,
                            lease_owner=f"worker-{index}", lease_seconds=240,
                            session_dependencies=(item.evidence_request.session_dependency,))
        for index in range(16)
    ))
    leases = [result.lease for result in results if result.lease is not None]
    assert len(leases) == 1
    lease = leases[0]
    if handoff is not None:
        async with database.acquire() as connection:
            restored = await connection.fetchval(
                "SELECT current_capture_id FROM qc_surface_session",
            )
            if handoff != "missing_capture":
                await connection.execute(
                    "INSERT INTO qc_capture(id,task_id,agent_run_id,surface_session_id)"
                    " VALUES($1,$2,$3,$4)", restored, UUID(item.task_id), UUID(run),
                    item.session_id,
                )
            await connection.execute("UPDATE qc_v2_work_item SET source_capture_id=$1", restored)
            await connection.execute(
                "INSERT INTO qc_restoration_attempt VALUES($1,$2,$3,4,$4,'equivalent',$5,$6,$7)",
                item.restoration_attempt_id, lease.request.request_id, UUID(item.task_id),
                lease.attempt_count + int(handoff == "foreign_attempt"),
                json.dumps({"passed": handoff != "failed_assessment"}),
                item.equivalence_assessment_id, restored,
            )
            await connection.execute(
                "UPDATE qc_evidence_request SET status='routed',lease_owner=NULL,lease_token=NULL,"
                "lease_expires_at=NULL,row_version=row_version+1",
            )
    accepted = await requests.renew_h5_lease(lease=lease, agent_run_id=run, lease_seconds=240,
                                            deadline=deadline)
    assert accepted is (handoff in {None, "valid"})
    async with database.acquire() as connection:
        assert await connection.fetchrow("SELECT * FROM qc_h5_llm_call_budget") == frozen
        assert await connection.fetchval("SELECT sum(ai_calls) FROM qc_llm_usage_event") == 0


async def test_real_database_lock_wait_is_cancelled_before_any_capture(database):
    item, run, token = await _shared_source(database)
    lease = EvidenceRequestProcessingLease(
        request=item.evidence_request, lease_owner="worker", lease_token=token,
        finalization_epoch=4, row_version=5, attempt_count=1, max_attempts=3,
    )
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE qc_evidence_request SET status='pending',lease_owner='worker',"
            "lease_token=$1,lease_expires_at=now()+interval '4 minutes'", UUID(token),
        )
        before = await connection.fetchrow("SELECT * FROM qc_evidence_request")
    operation = AsyncMock()
    async with database.acquire() as blocker, blocker.transaction():
        await blocker.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                              f"task-finalization:{item.task_id}")
        with pytest.raises(H5RecaptureDeadlineError):
            await asyncio.wait_for(run_h5_recapture_stage(
                operation=operation, requests=PostgresEvidenceRequestRepository(pool=database),
                lease=lease, agent_run_id=run, lease_seconds=240,
                deadline=datetime.now(UTC)+timedelta(milliseconds=40),
                now=lambda: datetime.now(UTC),
            ), timeout=1)
    operation.assert_not_awaited()
    async with database.acquire() as connection:
        assert await connection.fetchrow("SELECT * FROM qc_evidence_request") == before
        assert await connection.fetchval("SELECT count(*) FROM qc_capture") == 1
        assert await connection.fetchval("SELECT sum(ai_calls) FROM qc_llm_usage_event") == 0
