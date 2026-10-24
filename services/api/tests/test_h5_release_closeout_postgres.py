"""真实租约排空到补缺、复核门禁的完整时序，不把结算当通过。"""

import os
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    EvaluateCompletionGateCommand,
    TaskFinalizationService,
)
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
    _execution_resources_drained,
    _read_completion_gate_facts,
    _should_mark_task_review,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_h5_rule_inventory_postgres import seeded

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离数据库",
)


@pytest.mark.parametrize("exhaustion", ["calls", "deadline"])
async def test_lease_drain_settles_once_without_refreshing_budget_or_passing_gate(exhaustion):
    require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL"),
    )
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            "UPDATE qc_agent_run SET status='failed' WHERE id=$1::uuid", ids["run"],
        )
        deadline = datetime.now(UTC) + timedelta(minutes=180 if exhaustion == "calls" else -1)
        await connection.execute(
            "INSERT INTO qc_h5_llm_call_budget "
            "(task_id,agent_run_id,finalization_epoch,max_calls,admitted_calls,deadline) "
            "VALUES($1::uuid,$2::uuid,0,200,$3,$4)", ids["task"], ids["run"],
            200 if exhaustion == "calls" else 199, deadline,
        )
        budget = dict(await connection.fetchrow(
            "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"],
        ))
        captures = await connection.fetchval(
            "SELECT jsonb_agg(to_jsonb(c) ORDER BY id) FROM qc_capture c WHERE task_id=$1::uuid",
            ids["task"],
        )
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status='running', "
            "consumer_agent_run_id=$2::uuid, lease_token=gen_random_uuid(), "
            "lease_owner='isolated-consumer', lease_expires_at=now()+interval '60 seconds' "
            "WHERE id=$1::uuid", ids["work_item"], ids["run"],
        )
        work = PostgresInspectionWorkItemRepository(pool=pool)
        blocked = await work.settle_stalled_work_for_closure(
            task_id=ids["task"], snapshot_generation_closed=True,
        )
        assert blocked.settled_work_item_count == 0
        assert await connection.fetchval(
            "SELECT status FROM qc_inspection_attempt WHERE id=$1::uuid", ids["attempt"],
        ) == "running"
        await connection.execute(
            "UPDATE qc_inspection_work_item SET lease_expires_at=now()-interval '1 second' "
            "WHERE id=$1::uuid", ids["work_item"],
        )
        settled = await work.settle_stalled_work_for_closure(
            task_id=ids["task"], snapshot_generation_closed=True,
        )
        assert settled.settled_work_item_count == 1
        repeated = await work.settle_stalled_work_for_closure(
            task_id=ids["task"], snapshot_generation_closed=True,
        )
        assert repeated.settled_work_item_count == 0
        assert await connection.fetchval(
            "SELECT execution_status FROM qc_rule_execution WHERE task_id=$1::uuid",
            ids["task"],
        ) == "evidence_insufficient"
        service = TaskFinalizationService(PostgresTaskFinalizationRepository(pool=pool))
        snapshot = await service.begin_exploration_draining(BeginExplorationDrainingCommand(
            task_id=ids["task"], agent_run_id=ids["run"], expected_row_version=0,
        ))
        snapshot = await service.begin_session_draining(BeginSessionDrainingCommand(
            task_id=ids["task"], agent_run_id=ids["run"],
            expected_finalization_epoch=snapshot.finalization_epoch,
            expected_row_version=snapshot.row_version,
        ))
        command = EvaluateCompletionGateCommand(
            task_id=ids["task"], agent_run_id=ids["run"],
            expected_finalization_epoch=snapshot.finalization_epoch,
            expected_row_version=snapshot.row_version,
            execution_status=TaskExecutionStatus.FAILED, quality_outcome=TaskQualityOutcome.UNKNOWN,
        )
        row, facts = await _read_completion_gate_facts(connection, command)
        diagnostics = {"facts": facts, "block_reasons": facts.block_reasons,
                       "inventory_missing": row["h5_rule_inventory_missing_count"],
                       "inflight": row["h5_execution_inflight_work_count"],
                       "publication_review": row["h5_publication_review_required_count"]}
        assert _execution_resources_drained(
            facts, execution_inflight_work_count=diagnostics["inflight"],
        ), diagnostics
        assert _should_mark_task_review(
            execution_status=command.execution_status, facts=facts,
            h5_rule_inventory_missing_count=diagnostics["inventory_missing"],
            publication_review_required_count=diagnostics["publication_review"],
            allow_natural_evidence_review=True, require_execution_drained=True,
            execution_inflight_work_count=diagnostics["inflight"],
        ), diagnostics
        decision = await service.evaluate_and_seal(command)
        assert not decision.snapshot.gate_passed
        assert await connection.fetchval(
            "SELECT status FROM qc_task WHERE id=$1::uuid", ids["task"],
        ) == "review"
        assert dict(await connection.fetchrow(
            "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"],
        )) == budget
        assert await connection.fetchval(
            "SELECT jsonb_agg(to_jsonb(c) ORDER BY id) FROM qc_capture c WHERE task_id=$1::uuid",
            ids["task"],
        ) == captures
