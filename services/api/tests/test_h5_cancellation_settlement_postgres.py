"""真实 PostgreSQL 上验证 H5 取消对账的原子性与幂等性。"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.h5_cancellation_settlement_repository import (
    H5CancellationSettlementSummary,
    PostgresH5CancellationSettlementRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 迁移显式隔离测试库
)
from services.api.tests.test_inspection_closure_settlement_postgres import (
    _seed_task,
    _seed_work_item,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


async def test_h5_cancellation_settles_request_work_and_leases() -> None:
    database_url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.getenv("ALIOTH_DATABASE_URL"),
    )
    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        await connection.execute(
            "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid",
            ids["task"],
        )
        inspection_work_id, capture_id = await _seed_work_item(
            connection,
            ids,
            name=f"h5-cancel-{uuid4().hex[:8]}",
            page_seq=1,
            status="waiting_evidence",
            attempt_count=1,
            request_status="pending",
        )
        await connection.execute(
            """
            UPDATE qc_inspection_work_item
            SET status='running', lease_owner='inspection-owner',
                lease_token=gen_random_uuid(),
                lease_expires_at=now() + interval '5 minutes'
            WHERE id=$1::uuid
            """,
            inspection_work_id,
        )
        request_id = await connection.fetchval(
            "SELECT request_id FROM qc_evidence_request WHERE task_id=$1::uuid",
            ids["task"],
        )
        await connection.execute(
            """
            UPDATE qc_evidence_request
            SET lease_owner='request-owner', lease_token=gen_random_uuid(),
                lease_expires_at=now() + interval '5 minutes'
            WHERE request_id=$1
            """,
            request_id,
        )
        await connection.execute(
            """
            INSERT INTO qc_v2_work_item (
                work_id, work_type, task_id, session_id, branch_id,
                source_capture_id, protocol_version, payload_hash, work_payload,
                status, attempt_count, max_attempts, deadline,
                lease_owner, lease_token, lease_expires_at, finalization_epoch
            )
            VALUES (
                $1, 'exploration', $2::uuid, 'h5-session', 'h5-cancel-branch',
                $3::uuid, 'explorer-work.v1', 'h5-cancel-payload',
                '{"surface_kind":"web"}'::jsonb,
                'running', 1, 3, now() + interval '5 minutes',
                'work-owner', gen_random_uuid(), now() + interval '5 minutes', 0
            )
            """,
            f"h5-cancel-work:{uuid4().hex}",
            ids["task"],
            capture_id,
        )
        input_hash = "a" * 64
        input_set_id = f"evidence-input-set:{input_hash}"
        await connection.execute(
            """
            INSERT INTO qc_evidence_input_set (
                input_set_id, input_set_hash, task_id, source_agent_run_id,
                rule_id, rule_code, rule_contract_hash, ruleset_snapshot_hash,
                scope, scope_key, finalization_epoch, eligibility_snapshot_id,
                eligibility_snapshot_hash, selection_policy_hash,
                eligible_capture_ids, protocol_version, input_set_payload, sealed_at
            ) VALUES (
                $1, $2, $3::uuid, $4::uuid,
                $5::uuid, 'alioth_closure_1', 'contract-1', 'snapshot-1',
                'task', 'h5_cancel', 0, 'eligibility-1',
                'eligibility-hash-1', 'selection-policy-1',
                jsonb_build_array($6::text), 'evidence-input-set.v1', '{}'::jsonb, now()
            )
            """,
            input_set_id,
            input_hash,
            ids["task"],
            ids["run"],
            ids["rule"],
            capture_id,
        )
        aggregate_work_id = str(uuid4())
        aggregate_lease_token = str(uuid4())
        await connection.execute(
            """
            INSERT INTO qc_aggregate_inspection_work_item (
                id, task_id, evidence_input_set_id, producer_agent_run_id,
                consumer_agent_run_id, rule_id, rule_code, rule_contract_hash,
                scope, scope_key, protocol_version, input_hash,
                control_mode, status, attempt_count, max_attempts,
                lease_owner, lease_token, lease_expires_at, finalization_epoch
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4::uuid,
                $4::uuid, $5::uuid, 'alioth_closure_1', 'contract-1',
                'task', 'h5_cancel', 'aggregate-inspection-work-item.v1', $6,
                'enforced', 'running', 1, 3,
                'aggregate-owner', $7::uuid, now() + interval '5 minutes', 0
            )
            """,
            aggregate_work_id,
            ids["task"],
            input_set_id,
            ids["run"],
            ids["rule"],
            input_hash,
            aggregate_lease_token,
        )
        await connection.execute(
            """
            INSERT INTO qc_aggregate_inspection_attempt (
                task_id, work_item_id, consumer_agent_run_id,
                attempt_no, lease_token, input_hash, status
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid,
                1, $4::uuid, $5, 'running'
            )
            """,
            ids["task"],
            aggregate_work_id,
            ids["run"],
            aggregate_lease_token,
            input_hash,
        )
        session_id = f"h5-cancel-{uuid4().hex[:12]}"
        await connection.execute(
            """
            INSERT INTO qc_surface_session (
                session_id, task_id, surface_kind, session_owner,
                status, finalization_epoch
            ) VALUES ($1, $2::uuid, 'web', 'h5-cancel-test', 'starting', 0)
            """,
            session_id,
            ids["task"],
        )
        assessment_id = f"h5-assessment:{uuid4().hex}"
        assessment_hash = "b" * 64
        await connection.execute(
            """
            INSERT INTO qc_h5_page_type_assessment (
                assessment_record_id, assessment_id, task_id, agent_run_id,
                session_id, page_capture_id, detection_status, page_type,
                status, assessment_payload, schema_version, payload_hash
            ) VALUES (
                $1, $1, $2::uuid, $3::uuid,
                $4, $5::uuid, 'detected', 'home',
                'covered', '{}'::jsonb, 'page-type-assessment.v1', $6
            )
            """,
            assessment_id,
            ids["task"],
            ids["run"],
            session_id,
            capture_id,
            assessment_hash,
        )
        publication_id = str(uuid4())
        await connection.execute(
            """
            INSERT INTO qc_h5_inspection_publication (
                id, task_id, source_agent_run_id, page_capture_id,
                page_type_assessment_id, assessment_hash,
                ruleset_id, ruleset_version, ruleset_snapshot_hash,
                finalization_epoch, input_hash, mode, status,
                lease_owner, lease_token, lease_expires_at,
                attempt_count, max_attempts
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                $5, $6, $7::uuid, 1, 'snapshot-1',
                0, $8, 'enforced', 'leased',
                'publication-owner', gen_random_uuid(), now() + interval '5 minutes',
                1, 3
            )
            """,
            publication_id,
            ids["task"],
            ids["run"],
            capture_id,
            assessment_id,
            assessment_hash,
            ids["ruleset"],
            f"h5-cancel-publication:{uuid4().hex}",
        )
        await connection.execute(
            "UPDATE qc_task SET status='canceled' WHERE id=$1::uuid",
            ids["task"],
        )

        repository = PostgresH5CancellationSettlementRepository(pool=pool)
        first = await repository.settle_canceled(
            task_id=ids["task"],
            reason="H5_TASK_CANCELED",
        )
        second = await repository.settle_canceled(
            task_id=ids["task"],
            reason="H5_TASK_CANCELED",
        )

        assert first == H5CancellationSettlementSummary(
            request_count=1,
            explorer_work_count=1,
            inspection_work_count=1,
            inspection_attempt_count=1,
            inspection_rule_count=1,
            aggregate_work_count=1,
            aggregate_attempt_count=1,
            publication_count=1,
        )
        assert second.total_count == 0
        request = await connection.fetchrow(
            """
            SELECT status, resolution_reason, lease_owner, lease_token,
                   lease_expires_at, completed_at
            FROM qc_evidence_request WHERE request_id=$1
            """,
            request_id,
        )
        assert request["status"] == "cancelled"
        assert request["resolution_reason"].endswith(";H5_TASK_CANCELED")
        assert request["lease_owner"] is None
        assert request["lease_token"] is None
        assert request["lease_expires_at"] is None
        assert request["completed_at"] is not None
        work = await connection.fetchrow(
            """
            SELECT status, lease_owner, lease_token, lease_expires_at,
                   last_error_code, terminal_payload, finished_at
            FROM qc_v2_work_item WHERE task_id=$1::uuid
            """,
            ids["task"],
        )
        assert work["status"] == "cancelled"
        assert work["lease_owner"] is None
        assert work["lease_token"] is None
        assert work["lease_expires_at"] is None
        assert work["last_error_code"] == "WORKFLOW_CANCELED"
        terminal_payload = json.loads(work["terminal_payload"])
        assert terminal_payload["cancel_reason"] == "H5_TASK_CANCELED"
        assert work["finished_at"] is not None
        inspection = await connection.fetchrow(
            """
            SELECT status, lease_owner, lease_token, lease_expires_at,
                   last_error_code, finished_at
            FROM qc_inspection_work_item WHERE id=$1::uuid
            """,
            inspection_work_id,
        )
        assert inspection["status"] == "cancelled"
        assert inspection["lease_owner"] is None
        assert inspection["lease_token"] is None
        assert inspection["lease_expires_at"] is None
        assert inspection["last_error_code"] == "WORKFLOW_CANCELED"
        assert inspection["finished_at"] is not None
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_inspection_attempt WHERE work_item_id=$1::uuid",
                inspection_work_id,
            )
            == "abandoned"
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item_rule WHERE work_item_id=$1::uuid",
                inspection_work_id,
            )
            == "error"
        )
        aggregate = await connection.fetchrow(
            """
            SELECT status, consumer_agent_run_id, lease_owner, lease_token,
                   lease_expires_at, last_error_code, finished_at
            FROM qc_aggregate_inspection_work_item WHERE id=$1::uuid
            """,
            aggregate_work_id,
        )
        assert aggregate["status"] == "cancelled"
        assert aggregate["consumer_agent_run_id"] is None
        assert aggregate["lease_owner"] is None
        assert aggregate["lease_token"] is None
        assert aggregate["lease_expires_at"] is None
        assert aggregate["last_error_code"] == "WORKFLOW_CANCELED"
        assert aggregate["finished_at"] is not None
        assert (
            await connection.fetchval(
                """
                SELECT status FROM qc_aggregate_inspection_attempt
                WHERE work_item_id=$1::uuid
                """,
                aggregate_work_id,
            )
            == "abandoned"
        )
        publication = await connection.fetchrow(
            """
            SELECT status, lease_owner, lease_token, lease_expires_at,
                   failure_code, finished_at
            FROM qc_h5_inspection_publication WHERE id=$1::uuid
            """,
            publication_id,
        )
        assert publication["status"] == "cancelled"
        assert publication["lease_owner"] is None
        assert publication["lease_token"] is None
        assert publication["lease_expires_at"] is None
        assert publication["failure_code"] == "WORKFLOW_CANCELED"
        assert publication["finished_at"] is not None
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id=$1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id=$1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id=$1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_h5_cancellation_does_not_settle_web_task() -> None:
    database_url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.getenv("ALIOTH_DATABASE_URL"),
    )
    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        await connection.execute(
            "UPDATE qc_task SET target_platform='web' WHERE id=$1::uuid",
            ids["task"],
        )
        _, capture_id = await _seed_work_item(
            connection,
            ids,
            name=f"web-not-h5-{uuid4().hex[:8]}",
            page_seq=1,
            status="waiting_evidence",
            attempt_count=1,
            request_status="pending",
        )
        work_id = f"web-not-h5:{uuid4().hex}"
        await connection.execute(
            """
            INSERT INTO qc_v2_work_item (
                work_id, work_type, task_id, session_id, branch_id,
                source_capture_id, protocol_version, payload_hash, work_payload,
                status, attempt_count, max_attempts, deadline,
                lease_owner, lease_token, lease_expires_at, finalization_epoch
            ) VALUES (
                $1, 'exploration', $2::uuid, 'web-session', 'web-branch',
                $3::uuid, 'explorer-work.v1', 'web-not-h5-payload',
                '{"surface_kind":"web"}'::jsonb,
                'running', 1, 3, now() + interval '5 minutes',
                'web-owner', gen_random_uuid(), now() + interval '5 minutes', 0
            )
            """,
            work_id,
            ids["task"],
            capture_id,
        )
        await connection.execute(
            "UPDATE qc_task SET status='canceled' WHERE id=$1::uuid",
            ids["task"],
        )

        summary = await PostgresH5CancellationSettlementRepository(pool=pool).settle_canceled(
            task_id=ids["task"],
            reason="H5_TASK_CANCELED",
        )

        assert summary.total_count == 0
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_evidence_request WHERE task_id=$1::uuid",
                ids["task"],
            )
            == "pending"
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_v2_work_item WHERE work_id=$1",
                work_id,
            )
            == "running"
        )
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id=$1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id=$1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id=$1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()
