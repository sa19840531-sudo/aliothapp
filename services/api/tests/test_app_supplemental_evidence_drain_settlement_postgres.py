"""App 补采耗尽收口的真实 PostgreSQL 隔离回归。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.infrastructure.db.app_supplemental_evidence_drain_repository import (
    PostgresAppSupplementalEvidenceDrainSettlementRepository,
    PostgresAppSupplementalEvidenceDrainStateReader,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _test_database_url,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    database_url = _test_database_url()
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        with patch("logging.config.fileConfig"):
            command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous


@pytest.mark.asyncio
async def test_settlement_is_run_epoch_exact_and_protects_any_action_ledger() -> None:
    async with _database(surface_kind="android", target_platform="app") as (
        connection,
        pool,
        ids,
    ):
        now = datetime.now(UTC)
        stale_epoch_request = await _insert_inspection_request(
            connection,
            ids,
            request_id=f"settlement-stale-epoch-request-{uuid4()}",
            work_id=uuid4(),
            attempt_id=uuid4(),
            capture_id=ids["capture"],
            run_id=ids["run"],
            finalization_epoch=0,
            requirement_suffix="stale-epoch",
            now=now,
        )

        await connection.execute(
            "UPDATE qc_task SET finalization_epoch = 1 WHERE id = $1::uuid",
            ids["task"],
        )

        stale_run_id = str(uuid4())
        stale_capture_id = str(uuid4())
        await connection.execute(
            """
            INSERT INTO qc_agent_run(id, task_id, agent_type, agent_name)
            VALUES($1::uuid, $2::uuid, 'explorer', 'settlement-stale-run')
            """,
            stale_run_id,
            ids["task"],
        )
        await connection.execute(
            """
            INSERT INTO qc_capture(
                id, task_id, agent_run_id, surface_kind, surface_session_id,
                capture_role, session_command_sequence, capture_status, captured_at,
                metadata
            )
            VALUES($1::uuid, $2::uuid, $3::uuid, 'android', $4,
                   'observation', 3, 'captured', $5, '{}'::jsonb)
            """,
            stale_capture_id,
            ids["task"],
            stale_run_id,
            ids["session"],
            now,
        )

        candidate_request = await _insert_inspection_request(
            connection,
            ids,
            request_id=f"settlement-candidate-request-{uuid4()}",
            work_id=uuid4(),
            attempt_id=uuid4(),
            capture_id=ids["capture"],
            run_id=ids["run"],
            finalization_epoch=1,
            requirement_suffix="candidate",
            now=now,
        )
        protected_request = await _insert_inspection_request(
            connection,
            ids,
            request_id=f"settlement-protected-request-{uuid4()}",
            work_id=uuid4(),
            attempt_id=uuid4(),
            capture_id=ids["capture"],
            run_id=ids["run"],
            finalization_epoch=1,
            requirement_suffix="protected",
            now=now,
            status="routed",
        )
        stale_run_request = await _insert_inspection_request(
            connection,
            ids,
            request_id=f"settlement-stale-run-request-{uuid4()}",
            work_id=uuid4(),
            attempt_id=uuid4(),
            capture_id=stale_capture_id,
            run_id=stale_run_id,
            finalization_epoch=1,
            requirement_suffix="stale-run",
            now=now,
        )

        protected_restoration_id = f"settlement-restoration-{uuid4()}"
        protected_assessment_id = f"settlement-assessment-{uuid4()}"
        protected_probe_id = f"settlement-probe-{uuid4()}"
        await _insert_equivalent_restoration(
            connection,
            ids,
            request_id=protected_request,
            restoration_id=protected_restoration_id,
            assessment_id=protected_assessment_id,
            now=now,
        )
        await _insert_probe(
            connection,
            ids,
            request_id=protected_request,
            work_id=protected_probe_id,
            restoration_id=protected_restoration_id,
            assessment_id=protected_assessment_id,
            source_grade="A1",
            status="retry_wait",
            now=now,
        )
        await connection.execute(
            """
            INSERT INTO qc_action_execution(
                action_execution_id, task_id, session_id, intent_id,
                idempotency_key, intent_hash, intent_protocol_version,
                intent_payload, command_sequence, finalization_epoch,
                requested_source_capture_id, status, result_payload,
                error_code, action_finished_at, created_at, updated_at
            )
            VALUES($1, $2::uuid, $3, $4, $5, $6,
                   'action-intent.v1', $7::jsonb, 3, 1, $8::uuid,
                   'failed', '{}'::jsonb, 'PROBE_ACTION_FAILED', $9, $9, $9)
            """,
            f"settlement-action-{uuid4()}",
            ids["task"],
            ids["session"],
            f"settlement-action-intent-{uuid4()}",
            f"settlement-action-key-{uuid4()}",
            f"settlement-action-hash-{uuid4()}",
            json.dumps({"parameters": {"exploration_work_id": protected_probe_id}}),
            ids["capture"],
            now,
        )

        candidate_restoration_id = f"settlement-restoration-{uuid4()}"
        candidate_assessment_id = f"settlement-assessment-{uuid4()}"
        candidate_probe_id = f"settlement-probe-{uuid4()}"
        await _insert_equivalent_restoration(
            connection,
            ids,
            request_id=candidate_request,
            restoration_id=candidate_restoration_id,
            assessment_id=candidate_assessment_id,
            now=now,
        )
        await _insert_probe(
            connection,
            ids,
            request_id=candidate_request,
            work_id=candidate_probe_id,
            restoration_id=candidate_restoration_id,
            assessment_id=candidate_assessment_id,
            source_grade="A1",
            status="pending",
            now=now,
        )

        repository = PostgresAppSupplementalEvidenceDrainSettlementRepository(pool=pool)
        result = await repository.settle_exhausted(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=1,
            reason_code="SUPPLEMENTAL_DRAIN_ACTION_BUDGET_EXHAUSTED",
        )

        assert result.settled_request_count == 1
        assert result.cancelled_probe_count == 1
        assert await connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE request_id = $1",
            candidate_request,
        ) == "unfulfillable"
        assert await connection.fetchval(
            "SELECT status FROM qc_v2_work_item WHERE work_id = $1",
            candidate_probe_id,
        ) == "cancelled"
        assert await connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE request_id = $1",
            protected_request,
        ) == "routed"
        assert await connection.fetchval(
            "SELECT status FROM qc_v2_work_item WHERE work_id = $1",
            protected_probe_id,
        ) == "retry_wait"
        assert await connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE request_id = $1",
            stale_epoch_request,
        ) == "pending"
        assert await connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE request_id = $1",
            stale_run_request,
        ) == "pending"

        repeated = await repository.settle_exhausted(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=1,
            reason_code="SUPPLEMENTAL_DRAIN_ACTION_BUDGET_EXHAUSTED",
        )
        assert repeated.settled_request_count == 0
        assert repeated.cancelled_probe_count == 0

        state = await PostgresAppSupplementalEvidenceDrainStateReader(pool=pool).load(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=1,
        )
        assert state.nonterminal_request_count == 1
        assert state.nonterminal_probe_count == 1
        assert state.pending_completion_count == 0


@pytest.mark.asyncio
async def test_settlement_waits_for_probe_claim_and_preserves_new_running_status() -> None:
    async with _database(surface_kind="android", target_platform="app") as (
        connection,
        pool,
        ids,
    ):
        now = datetime.now(UTC)
        await connection.execute(
            "UPDATE qc_task SET finalization_epoch = 1 WHERE id = $1::uuid",
            ids["task"],
        )
        request_id = await _insert_inspection_request(
            connection,
            ids,
            request_id=f"settlement-lock-request-{uuid4()}",
            work_id=uuid4(),
            attempt_id=uuid4(),
            capture_id=ids["capture"],
            run_id=ids["run"],
            finalization_epoch=1,
            requirement_suffix="lock",
            now=now,
        )
        restoration_id = f"settlement-lock-restoration-{uuid4()}"
        assessment_id = f"settlement-lock-assessment-{uuid4()}"
        probe_id = f"settlement-lock-probe-{uuid4()}"
        await _insert_equivalent_restoration(
            connection,
            ids,
            request_id=request_id,
            restoration_id=restoration_id,
            assessment_id=assessment_id,
            now=now,
        )
        await _insert_probe(
            connection,
            ids,
            request_id=request_id,
            work_id=probe_id,
            restoration_id=restoration_id,
            assessment_id=assessment_id,
            source_grade="A1",
            status="pending",
            now=now,
        )

        blocker = await asyncpg.connect(_test_database_url())
        transaction = blocker.transaction()
        await transaction.start()
        committed = False
        try:
            await blocker.fetchrow(
                """
                SELECT work_id
                FROM qc_v2_work_item
                WHERE work_id = $1
                FOR UPDATE
                """,
                probe_id,
            )
            repository = PostgresAppSupplementalEvidenceDrainSettlementRepository(pool=pool)
            settling = asyncio.create_task(
                repository.settle_exhausted(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    finalization_epoch=1,
                    reason_code="SUPPLEMENTAL_DRAIN_ACTION_BUDGET_EXHAUSTED",
                )
            )
            await asyncio.sleep(0.2)
            assert not settling.done()
            await blocker.execute(
                """
                UPDATE qc_v2_work_item
                SET status = 'running',
                    lease_owner = 'settlement-lock-claim',
                    lease_token = gen_random_uuid(),
                    lease_expires_at = now() + interval '5 minutes',
                    started_at = now(),
                    updated_at = now()
                WHERE work_id = $1
                """,
                probe_id,
            )
            await transaction.commit()
            committed = True
            result = await asyncio.wait_for(settling, timeout=5)
        except BaseException:
            if not committed:
                await transaction.rollback()
            raise
        finally:
            await blocker.close()

        assert result.settled_request_count == 0
        assert result.cancelled_probe_count == 0
        assert await connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE request_id = $1",
            request_id,
        ) == "pending"
        assert await connection.fetchval(
            "SELECT status FROM qc_v2_work_item WHERE work_id = $1",
            probe_id,
        ) == "running"


async def _insert_inspection_request(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    request_id: str,
    work_id: str,
    attempt_id: str,
    capture_id: str,
    run_id: str,
    finalization_epoch: int,
    requirement_suffix: str,
    now: datetime,
    status: str = "pending",
) -> str:
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item(
            id, task_id, page_capture_id, producer_agent_run_id,
            consumer_agent_run_id, session_owner_agent_run_id, ruleset_id,
            ruleset_version, protocol_version, batch_key, strategy, category,
            session_dependency, input_hash, status, priority, max_attempts,
            next_attempt_at, finalization_epoch
        )
        VALUES($1::uuid, $2::uuid, $3::uuid, $4::uuid, $4::uuid, $4::uuid,
               $5::uuid, 1, 'inspection-work-item.v1', $6, 'rule_engine',
               'text', 'active_session', $7, 'pending', 100, 3, $8, $9)
        """,
        work_id,
        ids["task"],
        capture_id,
        run_id,
        ids["ruleset"],
        f"settlement-{requirement_suffix}-{uuid4()}",
        f"settlement-input-{requirement_suffix}-{uuid4()}",
        now,
        finalization_epoch,
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_attempt(
            id, work_item_id, consumer_agent_run_id, attempt_no, lease_token,
            status, input_hash, result_payload
        )
        VALUES($1::uuid, $2::uuid, $3::uuid, 1, gen_random_uuid(), 'running',
               $4, '{}'::jsonb)
        """,
        attempt_id,
        work_id,
        run_id,
        f"settlement-attempt-input-{requirement_suffix}-{uuid4()}",
    )
    await connection.execute(
        """
        INSERT INTO qc_evidence_request(
            request_id, requirement_hash, payload_hash, protocol_version,
            work_item_id, attempt_id, task_id, rule_id, rule_code,
            target_capture_id, session_dependency, replay_policy,
            required_equivalence, side_effect_policy, minimum_source_grade,
            request_payload, status, expires_at, finalization_epoch
        )
        VALUES($1, $2, $3, 'evidence-request.v1', $4::uuid, $5::uuid,
               $6::uuid, $7::uuid, 'FACT-GAP-1', $8::uuid, 'active_session',
               'same_surface_only', 'original_atomic', 'read_only', 'A0',
               '{}'::jsonb, $9, $10, $11)
        """,
        request_id,
        f"settlement-requirement-{requirement_suffix}-{uuid4()}",
        f"settlement-payload-{requirement_suffix}-{uuid4()}",
        work_id,
        attempt_id,
        ids["task"],
        ids["rule"],
        capture_id,
        status,
        now + timedelta(hours=1),
        finalization_epoch,
    )
    return request_id


async def _insert_equivalent_restoration(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    request_id: str,
    restoration_id: str,
    assessment_id: str,
    now: datetime,
) -> None:
    await connection.execute(
        """
        INSERT INTO qc_restoration_attempt(
            attempt_id, plan_id, plan_hash, plan_protocol_version, plan_payload,
            request_id, task_id, original_capture_id, source_session_id,
            target_session_id, surface_kind, strategy, attempt_no, status,
            source_grade, restored_capture_id, equivalence_assessment_id,
            assessment_hash, assessment_payload, attempt_protocol_version,
            started_at, finished_at, finalization_epoch
        )
        VALUES($1, $2, $3, 'restoration-plan.v1', '{}'::jsonb, $4, $5::uuid,
               $6::uuid, $7, $7, 'android', 'original_surface_pin', 1,
               'equivalent', 'A1', $6::uuid, $8, $9,
               '{"passed": true}'::jsonb, 'restoration-attempt.v1',
               $10, $10, 1)
        """,
        restoration_id,
        f"settlement-plan-{uuid4()}",
        f"settlement-plan-hash-{uuid4()}",
        request_id,
        ids["task"],
        ids["capture"],
        ids["session"],
        assessment_id,
        f"settlement-assessment-hash-{uuid4()}",
        now,
    )


async def _insert_probe(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    request_id: str,
    work_id: str,
    restoration_id: str,
    assessment_id: str,
    source_grade: str,
    status: str,
    now: datetime,
) -> None:
    await connection.execute(
        """
        INSERT INTO qc_v2_work_item(
            work_id, work_type, task_id, session_id, branch_id, source_capture_id,
            evidence_request_id, protocol_version, payload_hash, work_payload,
            status, deadline, restoration_attempt_id, equivalence_assessment_id,
            source_grade, finalization_epoch
        )
        VALUES($1, 'probe', $2::uuid, $3, $4, $5::uuid, $6,
               'probe-work.v1', $7, $8::jsonb, $9, $10, $11, $12, $13, 1)
        """,
        work_id,
        ids["task"],
        ids["session"],
        f"settlement-branch-{uuid4()}",
        ids["capture"],
        request_id,
        f"settlement-probe-payload-{uuid4()}",
        json.dumps({"surface_kind": "android", "work_id": work_id}),
        status,
        now + timedelta(hours=1),
        restoration_id,
        assessment_id,
        source_grade,
    )
