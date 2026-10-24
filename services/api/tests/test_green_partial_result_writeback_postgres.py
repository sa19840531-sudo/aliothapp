"""隔离 PG 验证结果事务；手工结论仅测试写回合同，不代表业务判读或设备验收。"""

import json
from dataclasses import replace

import pytest

from services.api.app.application.aggregate_inspection_execution import (
    AggregateInspectionExecutor,
    evaluate_aggregate_context,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 仅迁移显式独立测试库
)
from services.api.tests.test_green_aggregate_execution_postgres import green_case


@pytest.mark.parametrize(
    "status,missing,trigger",
    [
        ("fail", ["complete_repeated_actions"], "evidence_insufficient"),
        ("manual_review", [], "manual_flag"),
        ("pass", [], None),
    ],
)
async def test_partial_failure_and_manual_review_use_real_queue_without_losing_verdict(
    tmp_path, status, missing, trigger,
):
    async with green_case(tmp_path, "s35") as (
        connection, pool, ids, work, repository, _inputs, frozen,
        _manifests, _intent, _artifacts,
    ):
        claim = await repository.claim_for_wakeup(
            work_item_id=work["id"], task_id=ids["task"],
            message_version=work["row_version"], lease_owner="partial-writeback-fixture",
            lease_seconds=60,
        )
        context = await repository.load_judge_context(lease=claim.lease)
        definition = await PostgresTaskRepository(pool=pool).get_rule_execution_definition(
            task_id=ids["task"], rule_id=ids["rule"], rule_code=frozen["rule_code"],
        )
        assert definition.judge_config == frozen["judge_config"]
        initial = evaluate_aggregate_context(context)
        green = {
            **initial.wrapper_payload["green"],
            "missing_evidence": missing,
            "subitems": [{
                "key": "usable_layout", "status": status,
                "reason": "仅测试结果写回的受控结论", "missing": [],
            }],
        }
        judgement = replace(
            initial, execution_status=status, confidence=1.0,
            result_summary="仅测试结果写回的受控结论",
            problem_desc=None if status == "pass" else "受控写回合同用例",
            review_status="pending" if trigger else None,
            review_reason="其余条件待补材料" if trigger else None,
            error_code=None,
            wrapper_payload={**initial.wrapper_payload, "green": green},
        )
        await repository.complete_judgement(context=context, judgement=judgement)
        row = await connection.fetchrow(
            """SELECT execution.execution_status,execution.review_status,
                      execution.judge_output,review.trigger_reason,review.status AS queue_status,
                      candidate.status AS candidate_status,attempt.status AS attempt_status,
                      work.status AS work_status
               FROM qc_rule_execution execution
               LEFT JOIN qc_review_item review ON review.rule_execution_id=execution.id
               JOIN qc_aggregate_inspection_work_item work
                 ON work.id=execution.aggregate_work_item_id
               JOIN qc_aggregate_inspection_candidate candidate ON candidate.work_item_id=work.id
               JOIN qc_aggregate_inspection_attempt attempt ON attempt.work_item_id=work.id
               WHERE execution.task_id=$1::uuid""",
            ids["task"],
        )
        assert row["execution_status"] == status
        assert row["candidate_status"] == "completed"
        assert row["attempt_status"] == row["work_status"] == "succeeded"
        assert row["trigger_reason"] == trigger
        assert row["queue_status"] == ("pending" if trigger else None)
        assert row["review_status"] == ("pending" if trigger else None)
        stored = row["judge_output"]
        stored = json.loads(stored) if isinstance(stored, str) else stored
        assert stored["wrapper_payload"]["green"]["missing_evidence"] == missing
        assert stored["execution_status"] == status
        assert await AggregateInspectionExecutor(
            repository=repository, lease_owner="duplicate-partial-writeback",
            lease_seconds=60, retry_delay_seconds=1,
        ).execute_wakeup(
            work_item_id=work["id"], task_id=ids["task"], message_version=work["row_version"],
        )
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_review_item WHERE task_id=$1::uuid", ids["task"],
        ) == int(trigger is not None)
