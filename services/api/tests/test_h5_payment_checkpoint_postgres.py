"""受控 DOM、实际 PG 动作账本到流程 InputSet、Judge 和报告；不运行原站。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from services.api.app.application.aggregate_inspection_execution import AggregateInspectionExecutor
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.infrastructure.browser.capture_runtime import _public_state_diff
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.journey_report_binding import load_journey_report_binding
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库迁移
    append_quality,
    case,
    save_receipt,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_payment_checkpoint import payment_case
from services.api.tests.test_journey_consumption_postgres import configure


@pytest.mark.parametrize("receipt_present", [True, False])
async def test_payment_receipt_consumed_from_actual_action_row_not_selected_dom_style(
    receipt_present,
):
    checkpoint, before, after, trace, _ = payment_case()
    payload = dict(
        schema_version="journey-dsl.v2",
        definition_id="payment-" + str(uuid4()),
        version=1,
        name="只读套餐反馈",
        surface_kinds=["web"],
        checkpoints=[
            {
                **checkpoint.to_dict(),
                "id": "ready",
                "evidence_requirements": [
                    dict(role="feedback", artifact_types=["screenshot", "action_receipt"])
                ],
            }
        ],
    )
    async with case(checkpoint_payload=payload, configure=configure) as (
        connection,
        repo,
        ids,
        work,
        context,
        decision,
    ):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        receipt = turn.coordination.interaction.to_dict()
        if receipt_present:
            receipt["state_diff"] = _public_state_diff(SimpleNamespace(action_trace=trace))
        await connection.execute(
            "UPDATE qc_action_execution SET result_payload=$2::jsonb WHERE action_execution_id=$1",
            turn.coordination.interaction.action_execution_id,
            json.dumps(receipt),
        )
        await repo.bind(work=work, context=context, turn=turn)
        metadata = {
            "finalization_epoch": 0,
            "page_state": "stable",
            "pipeline_metrics": {"page_inspection_ready": True},
            "presentation": {
                "protocol_version": "presentation-stack.v1",
                "topmost_layer_kind": "page",
                "base_page_visibility": "visible",
                "blocking_scope": "none",
                "mutation_mode": "natural",
            },
        }
        await connection.execute(
            "UPDATE qc_capture SET metadata=$2::jsonb WHERE task_id=$1::uuid",
            ids["task"],
            json.dumps(metadata),
        )
        for capture in (turn.coordination.before_capture, turn.coordination.after_capture):
            await append_quality(connection, capture.capture_id, "pass", 2)
        await connection.execute(
            "UPDATE qc_task SET status='review',"
            "finalization_state='session_draining',exploration_draining_at=now(),"
            "session_draining_at=now(),finalization_agent_run_id=$2::uuid "
            "WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        publisher = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=repo._pool)
        )
        summary = await publisher.prepare(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
        )
        assert summary.published_work_item_count == 1
        queued = await connection.fetchrow(
            "SELECT id::text,row_version "
            "FROM qc_aggregate_inspection_work_item "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        consumer = AggregateInspectionExecutor(
            repository=PostgresAggregateInspectionExecutionRepository(
                pool=repo._pool,
                snapshot_reader=SimpleNamespace(
                    read_ui_snapshot=AsyncMock(side_effect=[before, after])
                ),
            ),
            lease_owner="payment-controlled",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert await consumer.execute_wakeup(
            work_item_id=queued["id"], task_id=ids["task"], message_version=queued["row_version"]
        )
        binding = await load_journey_report_binding(
            connection, task_id=ids["task"], agent_run_id=ids["run"], epoch=0
        )
        actual = await connection.fetchval(
            "SELECT execution_status FROM qc_rule_execution "
            "WHERE task_id=$1::uuid AND evidence_input_set_id IS NOT NULL", ids["task"])
        assert actual == ("pass" if receipt_present else "evidence_insufficient")
        if receipt_present:
            assert binding["gap_count"] == 0
            assert binding["results"][0]["execution_status"] == "pass"
        else:
            # 报告不能把不足结果收成正式通过；保留缺口而非补一条假完成记录。
            assert binding["gap_count"] == 1
            assert binding["results"] == []
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_action_execution WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )
