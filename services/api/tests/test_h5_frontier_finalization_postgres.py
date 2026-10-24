"""在隔离 PostgreSQL 事务回滚验证规则缺口与活动工作不会混淆。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    EvaluateCompletionGateCommand,
    TaskFinalizationService,
)
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
from services.api.app.infrastructure.db.app_rule_inventory_completion_gate import (
    apply_app_rule_inventory_gate,
)
from services.api.app.infrastructure.db.h5_rule_inventory_completion_gate import (
    apply_h5_rule_inventory_gate,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    _GATE_FACTS_SQL,
    PostgresTaskFinalizationRepository,
    _defer_blocked_completion_gate,
    _execution_resources_drained,
    _parse_gate_facts,
    _read_completion_gate_facts,
    _should_mark_task_review,
)
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    freeze_task_rule_inventory_on_connection,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_h5_rule_inventory_postgres import seeded

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需显式提供事务回滚测试连接",
)


@pytest.fixture(autouse=True)
def isolated_database():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if url:
        require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL"))


async def test_app_inventory_gate_does_not_add_a_phantom_gap_to_h5_inventory():
    async with seeded() as (connection, _, ids, _):
        row = await connection.fetchrow(_GATE_FACTS_SQL, ids["task"], "failed")
        original = _parse_gate_facts(row)
        h5_facts = await apply_h5_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=original,
        )
        assert h5_facts.candidate_rule_count == original.candidate_rule_count + 1
        app_facts = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=h5_facts,
        )
        assert app_facts == h5_facts, {
            "platform": row["inventory_target_platform"],
            "before_app_gate": h5_facts,
            "after_app_gate": app_facts,
        }


@pytest.mark.parametrize("corrupt_inventory", [False, True])
async def test_app_inventory_gate_still_blocks_missing_or_corrupt_app_inventory(
    corrupt_inventory,
):
    async with seeded() as (connection, _, ids, _):
        await connection.execute(
            'UPDATE qc_rule SET input_contract=\'{"target_clients":["APP"]}\'::jsonb '
            "WHERE id=$1::uuid", ids["rule"],
        )
        config = await freeze_task_rule_inventory_on_connection(
            connection, target_platform="app",
            ruleset_code=f"recapture-chain-{ids['ruleset']}", ruleset_version=1,
            channel="workbench",
            task_config={"workflow_version": "v2", "execution_mode": "full_inspection"},
        )
        assert config["rule_inventory"]["target_platform"] == "app"
        assert config["rule_inventory"]["rules"][0]["applicability"] == "applicable"
        config["app"] = {"platform_build_ref": f"app-build:{uuid4()}"}
        if corrupt_inventory:
            config["rule_inventory"]["rules"] = []
        await connection.execute(
            "UPDATE qc_task SET target_platform='app', target_url=NULL, task_config=$2::jsonb "
            "WHERE id=$1::uuid",
            ids["task"], json.dumps(config),
        )
        row = await connection.fetchrow(_GATE_FACTS_SQL, ids["task"], "failed")
        original = _parse_gate_facts(row)
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=original,
        )
        assert result.candidate_rule_count == original.candidate_rule_count + 1
        assert result.terminal_candidate_rule_count == original.terminal_candidate_rule_count
        assert not result.gate_passed
        assert not await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        )


@pytest.mark.parametrize("active_work", [False, True])
@pytest.mark.parametrize("publication_status", [None, "pending", "needs_review"])
async def test_real_inventory_gap_reviews_only_after_technical_work_drains(
    active_work, publication_status,
):
    async with seeded() as (connection, pool, ids, _):
        await connection.execute(
            "UPDATE qc_agent_run SET status='failed' WHERE id=$1::uuid",
            ids["run"],
        )
        repository = PostgresTaskFinalizationRepository(pool=pool)
        service = TaskFinalizationService(repository)
        snapshot = await service.begin_exploration_draining(
            BeginExplorationDrainingCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                expected_row_version=0,
            )
        )
        snapshot = await service.begin_session_draining(
            BeginSessionDrainingCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                expected_finalization_epoch=snapshot.finalization_epoch,
                expected_row_version=snapshot.row_version,
            )
        )
        if not active_work:
            await connection.execute(
                "UPDATE qc_inspection_work_item SET status='failed', finished_at=now() "
                "WHERE id=$1::uuid",
                ids["work_item"],
            )
            await connection.execute(
                "UPDATE qc_inspection_work_item_rule SET status='error' "
                "WHERE work_item_id=$1::uuid",
                ids["work_item"],
            )
            await connection.execute(
                "UPDATE qc_inspection_attempt SET status='terminal_error', finished_at=now() "
                "WHERE id=$1::uuid",
                ids["attempt"],
            )
        if publication_status:
            session, assessment = f"h5:{ids['run']}", f"h5-assessment:{uuid4().hex}"
            await connection.execute(
                """INSERT INTO qc_surface_session
                (session_id,task_id,surface_kind,session_owner,status,finalization_epoch,
                 activated_at,draining_at,closed_at)
                VALUES($1,$2::uuid,'web','h5-workflow','closed',0,now(),now(),now())""",
                session, ids["task"],
            )
            await connection.execute(
                """INSERT INTO qc_h5_page_type_assessment
                (assessment_record_id,assessment_id,task_id,agent_run_id,session_id,page_capture_id,
                 detection_status,page_type,status,assessment_payload,schema_version,payload_hash)
                VALUES($1,$1,$2::uuid,$3::uuid,$4,$5::uuid,'detected','home','covered','{}',
                       'page-type-assessment.v1',$6)""",
                assessment, ids["task"], ids["run"], session, ids["original"], "b" * 64,
            )
            await connection.execute(
                """INSERT INTO qc_h5_inspection_publication
                (id,task_id,source_agent_run_id,page_capture_id,page_type_assessment_id,
                 assessment_hash,ruleset_id,ruleset_version,finalization_epoch,input_hash,
                 mode,status,finished_at)
                VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7::uuid,1,0,$1,
                       'enforced',$8::text,CASE WHEN $8::text='needs_review' THEN now() END)""",
                str(uuid4()), ids["task"], ids["run"], ids["original"], assessment,
                "b" * 64, ids["ruleset"], publication_status,
            )
        command = EvaluateCompletionGateCommand(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            expected_finalization_epoch=snapshot.finalization_epoch,
            expected_row_version=snapshot.row_version,
            execution_status=TaskExecutionStatus.FAILED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
        )
        row, facts = await _read_completion_gate_facts(connection, command)
        assert row["h5_rule_inventory_missing_count"] == 1
        assert not facts.gate_passed
        inflight = active_work or publication_status == "pending"
        assert row["h5_execution_inflight_work_count"] == (
            int(active_work) + int(publication_status == "pending")
        )
        if publication_status == "needs_review":
            assert row["h5_publication_review_required_count"] == 1
            assert facts.nonterminal_enforced_work_item_count > 0

        diagnostics = {"facts": facts, "block_reasons": facts.block_reasons,
                       "inventory_missing": row["h5_rule_inventory_missing_count"],
                       "inflight": row["h5_execution_inflight_work_count"],
                       "publication_review": row["h5_publication_review_required_count"]}
        assert _execution_resources_drained(
            facts, execution_inflight_work_count=diagnostics["inflight"],
        ) is (not inflight), diagnostics
        assert _should_mark_task_review(
            execution_status=command.execution_status, facts=facts,
            h5_rule_inventory_missing_count=diagnostics["inventory_missing"],
            publication_review_required_count=diagnostics["publication_review"],
            allow_natural_evidence_review=True, require_execution_drained=True,
            execution_inflight_work_count=diagnostics["inflight"],
        ) is (not inflight), diagnostics
        decision = await repository.evaluate_and_seal(command=command)
        assert not decision.snapshot.gate_passed
        task = await connection.fetchrow(
            "SELECT status, finalization_state, finalization_retry_at='infinity'::timestamptz "
            "AS retry_paused FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        )
        assert task["status"] == ("running" if inflight else "review")
        assert task["retry_paused"] is (not inflight)
        assert task["finalization_state"] == "session_draining"
        await _defer_blocked_completion_gate(
            connection, task_id=ids["task"],
            finalization_epoch=decision.snapshot.finalization_epoch,
            gate_snapshot_id=decision.snapshot.gate_snapshot_id, facts=decision.snapshot.facts,
            execution_inflight_work_count=row["h5_execution_inflight_work_count"],
        )
        assert await connection.fetchval(
            "SELECT finalization_retry_at='infinity'::timestamptz FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        ) is (not inflight)
        assert not await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid",
            ids["task"],
        )
