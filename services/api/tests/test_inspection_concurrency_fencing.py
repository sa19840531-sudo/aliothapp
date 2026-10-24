"""正式质检并发身份：旧 lease 与聚合冻结漂移的确定性回归。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from services.api.app.application.aggregate_inspection_execution import (
    evaluate_aggregate_context,
)
from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    AggregateInspectionExecutionPersistenceError,
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.task_repository import (
    _assert_inspection_write_lease,
)
from services.api.tests.test_aggregate_inspection_runtime import (
    _context,
    _ExecutionConnection,
    _ExecutionPool,
    _frozen_execution_row_for,
    _frozen_packet_row_for,
)


class _RaceLeaseConnection:
    """模拟两名消费者争抢同一 Work：仅第一人持有有效 lease。"""

    def __init__(self, *, valid_token: str) -> None:
        self.valid_token = valid_token
        self.lock = asyncio.Lock()
        self.holders: list[str] = []

    async def fetchrow(self, query: str, *args: Any):
        async with self.lock:
            lease_token = str(args[3])
            self.holders.append(lease_token)
            if lease_token != self.valid_token:
                return None
            # 稍延时放大并发窗口，确保第二人在第一人持锁期间发起。
            await asyncio.sleep(0.01)
            return {"work_item_id": str(args[0])}


@pytest.mark.asyncio
async def test_concurrent_stale_lease_writer_is_rejected() -> None:
    connection = _RaceLeaseConnection(valid_token="44444444-4444-4444-4444-444444444444")
    task_id = "11111111-1111-1111-1111-111111111111"
    work_item_id = "22222222-2222-2222-2222-222222222222"
    attempt_id = "33333333-3333-3333-3333-333333333333"

    async def write(token: str) -> str:
        await _assert_inspection_write_lease(
            connection,
            task_id=task_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            lease_token=token,
            agent_run_id=None,
        )
        return token

    valid, stale = await asyncio.gather(
        write("44444444-4444-4444-4444-444444444444"),
        write("55555555-5555-5555-5555-555555555555"),
        return_exceptions=True,
    )

    assert valid == "44444444-4444-4444-4444-444444444444"
    assert isinstance(stale, RuleExecutionLeaseConflictError)
    assert connection.holders == [
        "44444444-4444-4444-4444-444444444444",
        "55555555-5555-5555-5555-555555555555",
    ] or set(connection.holders) == {
        "44444444-4444-4444-4444-444444444444",
        "55555555-5555-5555-5555-555555555555",
    }


@pytest.mark.asyncio
async def test_concurrent_aggregate_consumers_drift_fails_closed() -> None:
    """两名消费者先后命中同一 Work：内容漂移必须 fail-closed。"""
    context = _context(
        style_two={
            "font_size": "16px",
            "text_color": "white",
            "background_color": "black",
            "background_opacity": 0.7,
        },
    )
    judgement = evaluate_aggregate_context(context)
    packet = _frozen_packet_row_for(judgement)
    execution = _frozen_execution_row_for(judgement, packet_id=packet["packet_id"])
    execution["judge_input"] = json.dumps(
        {
            "evidence_input_set_id": context.lease.evidence_input_set_id,
            "input_hash": context.lease.input_hash,
            "scope": context.lease.scope,
            "scope_key": context.lease.scope_key,
        }
    )
    # 第二名消费者看到的是已冻结但判定不同的行
    drifted = dict(execution)
    drifted["execution_status"] = "fail"
    drifted["result_summary"] = "并发消费者漂移结论"

    first = _ExecutionConnection(
        frozen_packet_row=packet,
        frozen_execution_row=execution,
    )
    second = _ExecutionConnection(
        frozen_packet_row=packet,
        frozen_execution_row=drifted,
    )
    repo_a = PostgresAggregateInspectionExecutionRepository(pool=_ExecutionPool(first))
    repo_b = PostgresAggregateInspectionExecutionRepository(pool=_ExecutionPool(second))

    await repo_a.complete_judgement(context=context, judgement=judgement)
    with pytest.raises(AggregateInspectionExecutionPersistenceError, match="RuleExecution"):
        await repo_b.complete_judgement(context=context, judgement=judgement)
