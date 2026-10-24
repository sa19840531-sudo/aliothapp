"""真实 PostgreSQL 验证异常终止保存清单缺口，而非仅保存排空事实。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    EvaluateCompletionGateCommand,
    FinalizeTaskWithReportCommand,
    TerminateTaskFinalizationCommand,
)
from services.api.app.application.task_rule_inventory import inventory_execution_contract_hash
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
from services.api.app.infrastructure.db.task_finalization_repository import (
    _GATE_FACTS_SQL,
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
    _count_natural_evidence_presentation_violations,
    _parse_gate_facts,
)
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    freeze_task_rule_inventory_on_connection,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库完整迁移
)
from services.api.tests.test_h5_gate_coverage_postgres import _insert_h5_snapshot
from services.api.tests.test_h5_rule_inventory_postgres import seeded

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库",
)


async def _complete_rule(connection, ids, rule, ordinal):
    execution_id = await connection.fetchval(
        """INSERT INTO qc_rule_execution
           (task_id,rule_id,page_capture_id,agent_run_id,execution_status,judge_strategy)
           VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'pass','hybrid')
           RETURNING id::text""",
        ids["task"], rule["rule_id"], ids["original"], ids["run"],
    )
    contract_hash = inventory_execution_contract_hash(rule)
    if ordinal == 0:
        await connection.execute(
            """UPDATE qc_inspection_work_item_rule
               SET status='written',rule_execution_id=$2::uuid,contract_hash=$3
               WHERE work_item_id=$1::uuid AND rule_id=$4::uuid""",
            ids["work_item"], execution_id, contract_hash, rule["rule_id"],
        )
    else:
        await connection.execute(
            """INSERT INTO qc_inspection_work_item_rule
               (work_item_id,rule_id,rule_code,ordinal,contract_hash,status,rule_execution_id)
               VALUES($1::uuid,$2::uuid,$3,$4,$5,'written',$6::uuid)""",
            ids["work_item"], rule["rule_id"], rule["rule_code"], ordinal,
            contract_hash, execution_id,
        )


@pytest.mark.parametrize("entrypoint", ["terminate", "terminate_stalled"])
@pytest.mark.parametrize("inventory_state", ["missing_rule", "corrupt", "complete"])
async def test_h5_termination_audits_inventory_after_resources_and_coverage_are_ready(
    entrypoint, inventory_state,
):
    require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL"),
    )
    async with seeded() as (connection, pool, ids, _):
        missing_rule_id = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_rule
               (id,ruleset_id,rule_code,name,category,executor_type,severity,status,
                check_point,applicable_page_types,input_contract)
               SELECT $2::uuid,ruleset_id,'alioth_chain_2','第二条规则',category,
                      executor_type,severity,status,check_point,
                      applicable_page_types,input_contract
               FROM qc_rule WHERE id=$1::uuid""",
            ids["rule"], missing_rule_id,
        )
        await connection.execute(
            "UPDATE qc_ruleset SET rule_count=2 WHERE id=$1::uuid", ids["ruleset"],
        )
        config = await freeze_task_rule_inventory_on_connection(
            connection, target_platform="h5",
            ruleset_code=f"recapture-chain-{ids['ruleset']}", ruleset_version=1,
            channel="workbench",
            task_config={"workflow_version": "v2", "execution_mode": "full_inspection"},
        )
        rules = {rule["rule_id"]: rule for rule in config["rule_inventory"]["rules"]}
        assert config["rule_inventory"]["rule_count"] == 2
        assert all(rule["applicability"] == "applicable" for rule in rules.values())
        await connection.execute(
            """UPDATE qc_capture SET metadata=metadata ||
               '{"finalization_epoch":0,"page_type":"home"}'::jsonb
               WHERE task_id=$1::uuid""",
            ids["task"],
        )
        await _complete_rule(connection, ids, rules[ids["rule"]], 0)
        if inventory_state == "complete":
            await _complete_rule(connection, ids, rules[missing_rule_id], 1)
        elif inventory_state == "corrupt":
            # 保留两条清单规则，仅损坏哈希，避免空清单混淆真实缺口。
            config["rule_inventory"]["inventory_hash"] = "0" * 64
        await connection.execute(
            "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
            ids["task"], json.dumps(config),
        )
        await connection.execute(
            """UPDATE qc_agent_run SET status='failed',
               finished_at=now()-interval '11 minutes' WHERE id=$1::uuid""",
            ids["run"],
        )
        await connection.execute(
            """UPDATE qc_inspection_work_item SET status='succeeded',finished_at=now()
               WHERE id=$1::uuid""", ids["work_item"],
        )
        await connection.execute(
            """UPDATE qc_inspection_attempt SET status='written',finished_at=now()
               WHERE id=$1::uuid""", ids["attempt"],
        )
        repository = PostgresTaskFinalizationRepository(pool=pool)
        draining = await repository.begin_exploration_draining(
            command=BeginExplorationDrainingCommand(
                task_id=ids["task"], agent_run_id=ids["run"], expected_row_version=0,
            ),
        )
        draining = await repository.begin_session_draining(
            command=BeginSessionDrainingCommand(
                task_id=ids["task"], agent_run_id=ids["run"],
                expected_finalization_epoch=draining.finalization_epoch,
                expected_row_version=draining.row_version,
            ),
        )
        coverage_id = f"h5-terminate-coverage-{uuid4()}"
        await _insert_h5_snapshot(
            connection, task_id=ids["task"], agent_run_id=ids["run"],
            finalization_epoch=0, revision=1, coverage_snapshot_id=coverage_id,
            payload_hash="a" * 64,
        )
        row = await connection.fetchrow(_GATE_FACTS_SQL, ids["task"], "needs_review")
        raw_facts = _parse_gate_facts(row)
        # 原始 SQL 所有门禁均满足；唯一遗漏是独立的冻结清单检查。
        assert raw_facts.gate_passed, raw_facts
        assert raw_facts.h5_coverage_satisfied
        assert raw_facts.h5_coverage_snapshot_id == coverage_id
        assert raw_facts.pending_action_count == raw_facts.pending_capture_commit_count == 0
        assert raw_facts.nonterminal_enforced_work_item_count == 0
        assert raw_facts.active_session_request_count == raw_facts.background_execution_count == 0
        assert await _count_natural_evidence_presentation_violations(
            connection, task_id=ids["task"],
        ) == 0
        completed_count = 2 if inventory_state == "complete" else 1
        assert raw_facts.candidate_rule_count == completed_count
        assert raw_facts.terminal_candidate_rule_count == completed_count
        command = TerminateTaskFinalizationCommand(
            task_id=ids["task"], agent_run_id=ids["run"],
            expected_finalization_epoch=draining.finalization_epoch,
            expected_row_version=draining.row_version,
            execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            reason_code="h5_partial_results",
        )
        if entrypoint == "terminate":
            result = await repository.terminate(command=command)
        else:
            result = await repository.terminate_stalled(
                command=command, minimum_terminal_age_seconds=600,
            )
        assert result is not None
        gate = await connection.fetchrow(
            "SELECT * FROM qc_completion_gate_snapshot WHERE gate_snapshot_id=$1",
            result.gate_snapshot_id,
        )
        assert gate is not None
        diagnostics = {
            "entrypoint": entrypoint, "inventory_state": inventory_state,
            "candidate": gate["candidate_rule_count"],
            "terminal": gate["terminal_candidate_rule_count"],
            "gate_passed": gate["gate_passed"], "block_reasons": gate["block_reasons"],
        }
        assert gate["gate_passed"] is (inventory_state == "complete"), diagnostics
        assert gate["candidate_rule_count"] == 2, diagnostics
        assert gate["terminal_candidate_rule_count"] == completed_count, diagnostics
        assert gate["h5_coverage_snapshot_id"] == coverage_id
        if inventory_state != "complete":
            assert json.loads(gate["block_reasons"]) == ["candidate_rule_coverage_nonterminal"]
        task = await connection.fetchrow(
            "SELECT status,finalization_state,quality_outcome FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        )
        assert dict(task) == {
            "status": "review", "finalization_state": "sealed", "quality_outcome": "unknown",
        }
        # 异常封存后不能通过再次评估或最终化，把部分成果变成正式成功报告。
        with pytest.raises(TaskFinalizationPersistenceError, match="Session Draining"):
            await repository.evaluate_and_seal(command=EvaluateCompletionGateCommand(
                task_id=ids["task"], agent_run_id=ids["run"],
                expected_finalization_epoch=result.finalization_epoch,
                expected_row_version=result.row_version,
                execution_status=TaskExecutionStatus.COMPLETED,
                quality_outcome=TaskQualityOutcome.PASS,
            ))
        with pytest.raises(TaskFinalizationPersistenceError, match="Session Draining"):
            await repository.finalize_task_with_report(command=FinalizeTaskWithReportCommand(
                task_id=ids["task"], agent_run_id=ids["run"],
                report_id=f"h5-terminate-report:{ids['task']}", report_version=1,
                expected_finalization_epoch=result.finalization_epoch,
                expected_row_version=result.row_version,
                execution_status=TaskExecutionStatus.COMPLETED,
                quality_outcome=TaskQualityOutcome.PASS,
                final_report={"report_stage": "final", "is_final": True},
            ))
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            ids["task"],
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        ) == completed_count
        assert not await connection.fetchval(
            "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid", ids["task"],
        )
