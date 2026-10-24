"""隔离 PG 的原件→实际判读→结果/复核事务；受控夹具不代表设备或产品验收。"""

import json

import pytest

from services.api.app.application.aggregate_inspection_execution import (
    AggregateInspectionExecutor,
    AggregateWakeupClaimDisposition,
    evaluate_aggregate_context,
)
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只升级显式独立测试库
)
from services.api.tests.test_green_aggregate_execution_postgres import green_case
from services.api.tests.test_green_process_judge import process_case


def _local_materials(*, transition):
    """复用与真实生产函数一致的形状，原件字节/时间和业务材料仍是显式受控夹具。"""
    _, evidence = process_case(conflict=transition)
    return {
        "parameters": json.loads(evidence.actions[0].intent_payload)["parameters"],
        "before": evidence.captures[0].originals[0].object_payload(),
        "after": evidence.captures[1].originals[0].object_payload(),
        "trace": json.loads(evidence.captures[1].originals[1].payload),
    }


@pytest.mark.parametrize(
    "transition,expected",
    [
        (False, "pass"),
        (True, "evidence_insufficient"),
    ],
)
async def test_actual_s35_original_judge_and_writer_keep_partial_result_and_review(
    tmp_path,
    transition,
    expected,
):
    async with green_case(
        tmp_path,
        "s35",
        local_display=_local_materials(transition=transition),
    ) as (
        connection,
        _pool,
        ids,
        work,
        repository,
        inputs,
        _frozen,
        manifests,
        _intent,
        artifacts,
    ):
        assert len(manifests) == 3 and len(artifacts) == 6
        assert {item.artifact_type for item in artifacts} == {
            "screenshot",
            "ui_snapshot",
            "action_trace",
        }
        claim = await repository.claim_for_wakeup(
            work_item_id=work["id"],
            task_id=ids["task"],
            message_version=work["row_version"],
            lease_owner="actual-s35-consumer",
            lease_seconds=60,
        )
        assert claim.disposition is AggregateWakeupClaimDisposition.CLAIMED
        context = await repository.load_judge_context(lease=claim.lease)
        assert context.green_evidence.verified_read and not context.green_evidence.issues
        assert context.green_evidence.target_platform == "h5"
        assert len(context.green_evidence.actions) == 1
        assert len(context.input_set.members) == 3
        judgement = evaluate_aggregate_context(context)
        assert judgement.execution_status == "evidence_insufficient"
        assert judgement.confidence is None
        assert judgement.review_status == "pending" and judgement.review_reason
        subitems = judgement.wrapper_payload["green"]["subitems"]
        assert subitems[0]["key"] == "correct_response" and subitems[0]["status"] == expected
        assert subitems[0]["evaluated"] is (not transition)
        assert subitems[0]["confidence"] == (None if transition else 1.0)
        assert all(
            item["status"] == "evidence_insufficient" and item["missing"] for item in subitems[1:]
        )
        await repository.complete_judgement(context=context, judgement=judgement)
        row = await connection.fetchrow(
            """SELECT execution.execution_status,execution.review_status,execution.review_reason,
                      execution.judge_output,execution.error_code,review.status AS queue_status,
                      review.trigger_reason,packet.evidence_input_set_id,
                      candidate.status AS candidate_status
               FROM qc_rule_execution execution
               JOIN qc_review_item review ON review.rule_execution_id=execution.id
               JOIN qc_observation_packet packet ON packet.id=execution.observation_packet_id
               JOIN qc_aggregate_inspection_candidate candidate
                 ON candidate.work_item_id=execution.aggregate_work_item_id
               WHERE execution.task_id=$1::uuid""",
            ids["task"],
        )
        assert row["execution_status"] == "evidence_insufficient"
        assert row["review_status"] == row["queue_status"] == "pending"
        assert row["review_reason"] and row["error_code"] == "GREEN_NATIVE_PROCESS_FACTS_MISSING"
        assert row["trigger_reason"] == "evidence_insufficient"
        assert row["candidate_status"] == "completed"
        assert row["evidence_input_set_id"] == inputs.input_set_id
        stored = (
            json.loads(row["judge_output"])
            if isinstance(row["judge_output"], str)
            else row["judge_output"]
        )
        assert stored["wrapper_payload"]["green"]["subitems"] == subitems
        assert stored["wrapper_payload"]["green"]["missing_evidence"]
        assert "storage_path" not in json.dumps(stored)
        assert await AggregateInspectionExecutor(
            repository=repository,
            lease_owner="duplicate-actual-s35-consumer",
            lease_seconds=60,
            retry_delay_seconds=1,
        ).execute_wakeup(
            work_item_id=work["id"],
            task_id=ids["task"],
            message_version=work["row_version"],
        )
        for table in ("qc_rule_execution", "qc_review_item", "qc_aggregate_inspection_attempt"):
            assert (
                await connection.fetchval(
                    f"SELECT count(*) FROM {table} WHERE task_id=$1::uuid",
                    ids["task"],
                )
                == 1
            )
