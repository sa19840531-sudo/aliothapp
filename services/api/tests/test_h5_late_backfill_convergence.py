"""晚到正式工作先排空再补记，窗口不可刷新冻结截止。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app import worker


@pytest.mark.parametrize("deadline_seconds, settles", [(20, True), (3, False), (0, True)])
async def test_backfill_rereads_late_settlement_within_original_deadline(
    monkeypatch, deadline_seconds, settles
):
    elapsed = [0.0]
    monkeypatch.setattr(
        worker.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: elapsed[0])
    )

    async def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(worker.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        worker,
        "_h5_inspection_deadline",
        lambda record: datetime.now(UTC) + timedelta(seconds=deadline_seconds),
    )
    calls = []

    async def inflight(**kwargs):
        calls.append(elapsed[0])
        return int(not settles or len(calls) == 1)

    service = SimpleNamespace(
        settle_stalled_work_for_closure=AsyncMock(),
        count_inflight_leased_work=AsyncMock(side_effect=inflight),
        has_nonterminal_work=AsyncMock(return_value=False),
    )
    record = SimpleNamespace(
        task_id="task",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "h5"},
        },
    )
    if settles and deadline_seconds > 0:
        await worker._guard_h5_rule_backfill(record, service)
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError, match="拒绝提前规则补记"):
            await worker._guard_h5_rule_backfill(record, service)
    assert elapsed[0] <= max(deadline_seconds, 0)
