"""复现 128 晚到 Snapshot 与有效租约：预算耗尽不是通用失败覆盖权。"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_h5_release_migration_postgres import migrate, migration_database
from services.api.tests.test_h5_rule_inventory_postgres import seeded
from services.api.tests.test_inspection_closure_settlement_postgres import _seed_work_item
from services.api.tests.test_targeted_recapture_chain_postgres import _request, _seed


@pytest.fixture(autouse=True)
def isolated_database():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if url:
        require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL"))


pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="只允许显式隔离数据库"
)


@pytest.mark.parametrize("platform,contract_error", [("h5", True), ("h5", False), ("web", True)])
async def test_contract_failure_does_not_loop_on_old_satisfied_request(platform, contract_error):
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            "UPDATE qc_task SET target_platform=$2 WHERE id=$1::uuid", ids["task"], platform,
        )
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status='pending' WHERE id=$1::uuid",
            ids["work_item"],
        )
        work_id, _ = await _seed_work_item(connection, ids, name=f"contract-{uuid4().hex}",
            page_seq=99, status="waiting_evidence", attempt_count=1, request_status="satisfied")
        if contract_error:
            await connection.execute(
                "UPDATE qc_inspection_work_item "
                "SET last_error_code='H5_EVIDENCE_CONTRACT_INVALID' WHERE id=$1::uuid",
                work_id,
            )
        repository = PostgresInspectionWorkItemRepository(pool=pool)
        result = await repository.settle_stalled_work_for_closure(
            task_id=ids["task"], snapshot_generation_closed=True,
        )
        assert result.settled_work_item_count == int(platform == "h5" and contract_error)
        status = await connection.fetchval(
            "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", work_id,
        )
        assert status == (
            "partial" if platform == "h5" and contract_error else "waiting_evidence"
        )


@pytest.mark.parametrize("barrier", [
    "none", "lease", "publication", "session_active", "budget_available", "expired_deadline",
    "foreign_owner", "live_request", "closing_lease", "closing_request", "closing_publication",
    "closing_none",
])
async def test_128_closed_old_active_work_settles_without_new_consumer_claim(barrier):
    async with seeded() as (connection, pool, ids, _):
        session_id = f"h5:{ids['run']}"
        has_lease = barrier in {"lease", "closing_lease"}
        await connection.execute(
            """INSERT INTO qc_surface_session
            (session_id,task_id,surface_kind,session_owner,status,finalization_epoch,activated_at,draining_at,closed_at)
            VALUES($1,$2::uuid,'web','h5-workflow',$3::text,0,now(),
                   CASE WHEN $3::text='closed' THEN now() END,
                   CASE WHEN $3::text='closed' THEN now() END)""",
            session_id, ids["task"], "active" if barrier == "session_active" else "closed",
        )
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id=$2,agent_run_id=$3::uuid WHERE id=$1::uuid",
            ids["original"], session_id, ids["run"],
        )
        await connection.execute(
            """UPDATE qc_inspection_work_item
            SET status=CASE WHEN $3 THEN 'running' ELSE 'pending' END,
            session_dependency='active_session',consumer_agent_run_id=$2::uuid,
            session_owner_agent_run_id=$2::uuid,attempt_count=1,max_attempts=3,
            lease_owner=CASE WHEN $3 THEN 'test' ELSE NULL END,
            lease_token=CASE WHEN $3 THEN gen_random_uuid() ELSE NULL END,
            lease_expires_at=CASE WHEN $3 THEN now()+interval '60 seconds' ELSE NULL END
            WHERE id=$1::uuid""", ids["work_item"], ids["run"], has_lease,
        )
        await connection.execute(
            """INSERT INTO qc_h5_llm_call_budget
            (task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline)
            VALUES($1::uuid,$2::uuid,0,200,$3,now()+interval '180 minutes')""",
            ids["task"], ids["run"],
            199 if barrier in {"budget_available", "expired_deadline"} else 200,
        )
        if barrier == "expired_deadline":
            await connection.execute(
                "UPDATE qc_h5_llm_call_budget SET deadline=now()-interval '1 second' "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
        elif barrier == "foreign_owner":
            run = str(uuid4())
            await connection.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status) "
                "VALUES($1::uuid,$2::uuid,'rule_judge','异属消费者','running')", run, ids["task"],
            )
            await connection.execute(
                "UPDATE qc_inspection_work_item SET session_owner_agent_run_id=$2::uuid "
                "WHERE id=$1::uuid",
                ids["work_item"], run,
            )
        elif barrier in {"live_request", "closing_request"}:
            await connection.execute(
                "UPDATE qc_inspection_work_item SET status='waiting_evidence' WHERE id=$1::uuid",
                ids["work_item"],
            )
            await PostgresEvidenceRequestRepository(pool=pool).upsert(request=_request(
                ids, rule_code="alioth_chain_1", expires_at=datetime.now(UTC) + timedelta(hours=1),
            ))
            await connection.execute(
                "UPDATE qc_inspection_work_item SET status='pending' WHERE id=$1::uuid",
                ids["work_item"],
            )
        if barrier in {"publication", "closing_publication"}:
            assessment = f"h5-assessment:{uuid4().hex}"
            await connection.execute(
                """INSERT INTO qc_h5_page_type_assessment
                (assessment_record_id,assessment_id,task_id,agent_run_id,session_id,page_capture_id,
                 detection_status,page_type,status,assessment_payload,schema_version,payload_hash)
                VALUES($1,$1,$2::uuid,$3::uuid,$4,$5::uuid,'detected','home','covered','{}',
                       'page-type-assessment.v1',$6)""",
                assessment, ids["task"], ids["run"], session_id, ids["original"], "b" * 64,
            )
            await connection.execute(
                """INSERT INTO qc_h5_inspection_publication
                (id,task_id,source_agent_run_id,page_capture_id,page_type_assessment_id,assessment_hash,
                 ruleset_id,ruleset_version,finalization_epoch,input_hash,mode,status)
                VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7::uuid,1,0,$1,'enforced','pending')""",
                str(uuid4()), ids["task"], ids["run"], ids["original"],
                assessment, "b" * 64, ids["ruleset"],
            )
        repository = PostgresInspectionWorkItemRepository(pool=pool)
        result = await repository.settle_stalled_work_for_closure(
            task_id=ids["task"], snapshot_generation_closed=True,
            active_session_agent_run_id=(
                ids["run"] if barrier.startswith("closing_") else None
            ),
        )
        expected = int(barrier in {"none", "expired_deadline", "closing_none"})
        assert result.settled_work_item_count == expected
        if barrier in {"none", "expired_deadline", "closing_none"}:
            assert await connection.fetchval(
                "SELECT execution_status FROM qc_rule_execution WHERE task_id=$1::uuid",
                ids["task"],
            ) == "evidence_insufficient"


@pytest.mark.parametrize(
    "status", ["pending", "retry_wait", "waiting_evidence", "running", "expired_running"]
)
@pytest.mark.parametrize("exhausted", [False, True])
async def test_late_snapshot_settles_only_when_budget_exhausted_and_no_live_lease(
    status, exhausted
):
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            """INSERT INTO qc_h5_llm_call_budget
            (task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline)
            VALUES($1::uuid,$2::uuid,0,200,$3,now()+interval '180 minutes')""",
            ids["task"],
            ids["run"],
            200 if exhausted else 199,
        )
        await connection.execute(
            """UPDATE qc_inspection_work_item SET status=$2::text,
            consumer_agent_run_id=$3::uuid,
            lease_owner=CASE WHEN $2='running' THEN 'test-consumer' ELSE NULL END,
            lease_token=CASE WHEN $2='running' THEN gen_random_uuid() ELSE NULL END,
            lease_expires_at=CASE WHEN $2='running'
                THEN now()+($4 * interval '1 second') ELSE NULL END
            WHERE id=$1::uuid""",
            ids["work_item"],
            "running" if status == "expired_running" else status,
            ids["run"],
            -60 if status == "expired_running" else 60,
        )
        result = await PostgresInspectionWorkItemRepository(
            pool=pool
        ).settle_stalled_work_for_closure(
            task_id=ids["task"],
            snapshot_generation_closed=True,
        )
        expected = exhausted and status != "running" or status == "waiting_evidence"
        assert result.settled_work_item_count == int(expected)
        if expected:
            assert (
                await connection.fetchval(
                    "SELECT execution_status FROM qc_rule_execution WHERE task_id=$1::uuid",
                    ids["task"],
                )
                == "evidence_insufficient"
            )
        else:
            assert await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", ids["work_item"]
            ) == ("running" if status == "expired_running" else status)


async def test_finalizer_does_not_overwrite_h5_consumer_valid_lease():
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            """UPDATE qc_inspection_work_item SET status='running', lease_token=gen_random_uuid(),
            lease_owner='test-consumer', consumer_agent_run_id=$2::uuid,
            lease_expires_at=now()+interval '60 seconds' WHERE id=$1::uuid""",
            ids["work_item"],
            ids["run"],
        )
        await PostgresInspectionWorkItemRepository(pool=pool).finalize_task_work(
            task_id=ids["task"],
            error_code="INSPECTION_WORK_ITEM_FINALIZATION_INCOMPLETE",
            error_message="收口",
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", ids["work_item"]
            )
            == "running"
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_inspection_attempt WHERE id=$1::uuid", ids["attempt"]
            )
            == "running"
        )


@pytest.mark.parametrize("fault", ["not_closed", "live_request", "different_run", "other_platform"])
async def test_budget_exhaustion_does_not_settle_unrelated_or_inflight_work(fault):
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            """INSERT INTO qc_h5_llm_call_budget
            (task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline)
            VALUES($1::uuid,$2::uuid,0,200,200,now()+interval '180 minutes')""",
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status='pending' WHERE id=$1::uuid",
            ids["work_item"],
        )
        if fault == "live_request":
            await connection.execute(
                "UPDATE qc_inspection_work_item SET status='waiting_evidence' WHERE id=$1::uuid",
                ids["work_item"],
            )
            request = _request(
                ids, rule_code="alioth_chain_1", expires_at=datetime.now(UTC) + timedelta(hours=1)
            )
            await PostgresEvidenceRequestRepository(pool=pool).upsert(request=request)
            await connection.execute(
                "UPDATE qc_inspection_work_item SET status='pending' WHERE id=$1::uuid",
                ids["work_item"],
            )
            assert await PostgresInspectionWorkItemRepository(
                pool=pool
            ).count_inflight_leased_work(task_id=ids["task"]) == 1
        elif fault == "different_run":
            run = str(uuid4())
            await connection.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status) "
                "VALUES($1::uuid,$2::uuid,'rule_judge','测试消费者','running')",
                run,
                ids["task"],
            )
            await connection.execute(
                "UPDATE qc_inspection_work_item SET producer_agent_run_id=$2::uuid "
                "WHERE id=$1::uuid",
                ids["work_item"],
                run,
            )
        elif fault == "other_platform":
            await connection.execute(
                "UPDATE qc_task SET target_platform='web' WHERE id=$1::uuid", ids["task"]
            )
        result = await PostgresInspectionWorkItemRepository(
            pool=pool
        ).settle_stalled_work_for_closure(
            task_id=ids["task"],
            snapshot_generation_closed=fault != "not_closed",
        )
        assert result.settled_work_item_count == 0
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", ids["work_item"]
            )
            == "pending"
        )


async def test_concurrent_consumer_claim_commits_before_finalizer_can_inspect_lease():
    """真实两连接行锁竞争：收口必须看见消费者刚提交的租约，不能覆写。"""
    async with migration_database() as url:
        await migrate(url, "head")
        await _claim_commits_before_finalizer(url)


async def _claim_commits_before_finalizer(url):
    pool = await asyncpg.create_pool(url, min_size=2, max_size=3)
    ids = {
        key: str(uuid4())
        for key in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "work_item",
            "attempt",
            "original",
            "restored",
        )
    }
    try:
        async with pool.acquire() as connection:
            await _seed(connection, ids)
            await connection.execute(
                "UPDATE qc_tenant SET display_name='并发测试-' || id::text WHERE id=$1::uuid",
                ids["tenant"],
            )
            await connection.execute(
                "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid", ids["task"]
            )
            async with connection.transaction():
                await connection.execute(
                    """UPDATE qc_inspection_work_item
                    SET status='running', lease_token=gen_random_uuid(),
                    lease_owner='concurrent-consumer', consumer_agent_run_id=$2::uuid,
                    lease_expires_at=now()+interval '60 seconds' WHERE id=$1::uuid""",
                    ids["work_item"],
                    ids["run"],
                )
                finalizer = asyncio.create_task(
                    PostgresInspectionWorkItemRepository(pool=pool).finalize_task_work(
                        task_id=ids["task"],
                        error_code="INCOMPLETE",
                        error_message="隔离并发收口",
                    )
                )
                await asyncio.sleep(0.05)
                assert not finalizer.done()
            result = await asyncio.wait_for(finalizer, timeout=5)
            assert result.deferred_retry_count == 1
            assert (
                await connection.fetchval(
                    "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", ids["work_item"]
                )
                == "running"
            )
    finally:
        await pool.close()


@pytest.mark.parametrize("status", ["pending", "leased", "retry_wait"])
@pytest.mark.parametrize("closure", ["finalize", "budget_settlement"])
@pytest.mark.parametrize("work_status", ["pending", "retry_wait", "waiting_evidence"])
async def test_unfinished_publication_prevents_finalizer_from_failing_pending_snapshot(
    status, closure, work_status,
):
    async with seeded() as (connection, pool, ids, _):
        session = f"h5:{ids['run']}"
        assessment = f"h5-assessment:{uuid4().hex}"
        await connection.execute(
            """INSERT INTO qc_surface_session
            (session_id,task_id,surface_kind,session_owner,status,finalization_epoch)
            VALUES($1,$2::uuid,'web','h5-workflow','starting',0)""",
            session,
            ids["task"],
        )
        await connection.execute(
            """INSERT INTO qc_h5_page_type_assessment
            (assessment_record_id,assessment_id,task_id,agent_run_id,session_id,page_capture_id,
             detection_status,page_type,status,assessment_payload,schema_version,payload_hash)
            VALUES($1,$1,$2::uuid,$3::uuid,$4,$5::uuid,'detected','home','covered','{}',
                   'page-type-assessment.v1',$6)""",
            assessment,
            ids["task"],
            ids["run"],
            session,
            ids["original"],
            "b" * 64,
        )
        await connection.execute(
            """INSERT INTO qc_h5_inspection_publication
            (id,task_id,source_agent_run_id,page_capture_id,page_type_assessment_id,assessment_hash,
             ruleset_id,ruleset_version,finalization_epoch,input_hash,mode,status,
             lease_owner,lease_token,lease_expires_at)
            VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7::uuid,1,0,$1,'enforced',$8::text,
                   CASE WHEN $8::text='leased' THEN 'publisher' END,
                   CASE WHEN $8::text='leased' THEN gen_random_uuid() END,
                   CASE WHEN $8::text='leased' THEN now()+interval '1 minute' END)""",
            str(uuid4()),
            ids["task"],
            ids["run"],
            ids["original"],
            assessment,
            "b" * 64,
            ids["ruleset"],
            status,
        )
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status=$2 WHERE id=$1::uuid",
            ids["work_item"], work_status,
        )
        repository = PostgresInspectionWorkItemRepository(pool=pool)
        assert await repository.count_inflight_leased_work(task_id=ids["task"]) == 1
        if closure == "budget_settlement":
            await connection.execute(
                """INSERT INTO qc_h5_llm_call_budget
                (task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline)
                VALUES($1::uuid,$2::uuid,0,200,200,now()+interval '180 minutes')""",
                ids["task"], ids["run"],
            )
            result = await repository.settle_stalled_work_for_closure(
                task_id=ids["task"], snapshot_generation_closed=True,
            )
            assert result.settled_work_item_count == 0
            assert await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", ids["work_item"]
            ) == work_status
            await connection.execute(
                """UPDATE qc_h5_inspection_publication SET status='published', finished_at=now(),
                lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                WHERE task_id=$1::uuid""", ids["task"],
            )
            settled = await repository.settle_stalled_work_for_closure(
                task_id=ids["task"], snapshot_generation_closed=True,
            )
            assert settled.settled_work_item_count == 1
            return
        result = await repository.finalize_task_work(
            task_id=ids["task"],
            error_code="INCOMPLETE",
            error_message="隔离发布屏障",
        )
        assert result.deferred_retry_count == 1
        assert result.failed_work_item_count == 0
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid",
                ids["work_item"],
            )
            == work_status
        )


@pytest.mark.parametrize("page_type", ["payment", "video_detail"])
async def test_budget_settlement_preserves_existing_rule_pass_and_capture(page_type):
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            "UPDATE qc_page_capture SET page_type=$2 WHERE id=$1::uuid",
            ids["original"],
            page_type,
        )
        await connection.execute(
            """INSERT INTO qc_h5_llm_call_budget
            (task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline)
            VALUES($1::uuid,$2::uuid,0,200,200,now()+interval '180 minutes')""",
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status='pending' WHERE id=$1::uuid",
            ids["work_item"],
        )
        execution = await connection.fetchrow(
            """INSERT INTO qc_rule_execution
            (task_id,rule_id,page_capture_id,agent_run_id,execution_status,judge_strategy)
            VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'pass','hybrid') RETURNING *""",
            ids["task"],
            ids["rule"],
            ids["original"],
            ids["run"],
        )
        capture = await connection.fetchrow(
            "SELECT * FROM qc_page_capture WHERE id=$1::uuid", ids["original"]
        )
        result = await PostgresInspectionWorkItemRepository(
            pool=pool
        ).settle_stalled_work_for_closure(
            task_id=ids["task"],
            snapshot_generation_closed=True,
        )
        assert result.settled_work_item_count == 1
        assert dict(
            await connection.fetchrow(
                "SELECT * FROM qc_rule_execution WHERE id=$1", execution["id"]
            )
        ) == dict(execution)
        assert dict(
            await connection.fetchrow(
                "SELECT * FROM qc_page_capture WHERE id=$1::uuid", ids["original"]
            )
        ) == dict(capture)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_review_item WHERE task_id=$1::uuid", ids["task"]
            )
            == 0
        )


async def test_frozen_deadline_exhaustion_is_not_reported_as_two_hundred_calls():
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            """INSERT INTO qc_h5_llm_call_budget
            (task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline)
            VALUES($1::uuid,$2::uuid,0,200,14,now()-interval '1 second')""",
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status='pending' WHERE id=$1::uuid",
            ids["work_item"],
        )
        result = await PostgresInspectionWorkItemRepository(
            pool=pool
        ).settle_stalled_work_for_closure(
            task_id=ids["task"],
            snapshot_generation_closed=True,
        )
        assert result.reason_counts == {"h5_llm_deadline_exhausted": 1}
        assert (
            await connection.fetchval(
                "SELECT admitted_calls FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 14
        )
