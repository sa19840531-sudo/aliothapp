"""复现混合 Snapshot 检查点，完整清单不等于领取新任务许可。"""

from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_targeted_recapture import H5TargetedRecaptureDrainService
from services.api.app.domain.exploration import ExplorationBudget
from services.api.app.domain.inspection import build_evidence_request
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    PostgresH5TargetedRecaptureRepository,
)
from services.api.tests.test_h5_targeted_recapture import _snapshot_request
from services.api.tests.test_v2_control_plane_repositories import FakeConnection, FakePool


def snapshot_request_with(**overrides):
    request = _snapshot_request()
    values = {
        field.name: getattr(request, field.name) for field in fields(request)
        if field.name not in {"request_id", "requirement_hash", "protocol_version"}
    }
    return build_evidence_request(**{**values, **overrides})


@pytest.mark.parametrize("owned,restorable,expected", [
    (False, True, True), (True, True, False), (False, False, False),
])
async def test_checkpoint_uses_complete_inventory_not_pending_claim_inventory(
    owned, restorable, expected,
):
    request = _snapshot_request()
    reader = SimpleNamespace(
        # 正在处理的 Probe 会阻止再次领取，但不是完整清单的读取失败。
        read_h5_epoch=AsyncMock(return_value=None),
        list_deferred_snapshot_requests=AsyncMock(return_value=()),
        list_snapshot_checkpoint_requests=AsyncMock(return_value=(SimpleNamespace(
            request=request, finalization_epoch=4, blocks_current_surface=owned,
        ),)),
    )
    consumer = SimpleNamespace(can_defer_snapshot_requests=AsyncMock(return_value=restorable))
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer, scope_reader=reader,
        budget=ExplorationBudget(
            max_actions=1, remaining_actions=1, max_depth=1, remaining_duration_ms=1_000,
        ),
    )
    assert await drain.can_defer_snapshot_requests(
        task_id="task-h5", agent_run_id="run-h5", expected_request_count=1,
    ) is expected
    reader.list_snapshot_checkpoint_requests.assert_awaited_once()
    reader.read_h5_epoch.assert_not_awaited()
    if owned:
        consumer.can_defer_snapshot_requests.assert_not_awaited()


@pytest.mark.parametrize("epochs,count", [((4, 5), 2), ((4,), 2)])
async def test_checkpoint_rejects_missing_requests_or_mixed_epochs(epochs, count):
    consumer = SimpleNamespace(can_defer_snapshot_requests=AsyncMock(return_value=True))
    reader = SimpleNamespace(
        list_snapshot_checkpoint_requests=AsyncMock(return_value=tuple(SimpleNamespace(
            request=_snapshot_request(), finalization_epoch=epoch, blocks_current_surface=False,
        ) for epoch in epochs)),
        read_h5_epoch=AsyncMock(return_value=4),
        list_deferred_snapshot_requests=AsyncMock(return_value=()),
    )
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer, scope_reader=reader,
        budget=ExplorationBudget(
            max_actions=1, remaining_actions=1, max_depth=1, remaining_duration_ms=1_000,
        ),
    )
    assert not await drain.can_defer_snapshot_requests(
        task_id="task-h5", agent_run_id="run-h5", expected_request_count=count,
    )
    consumer.can_defer_snapshot_requests.assert_not_awaited()

async def test_checkpoint_sql_reads_owned_snapshot_without_relaxing_claim_permission():
    connection = FakeConnection()
    connection.fetch = AsyncMock(return_value=[{
        "request_payload": _snapshot_request().to_dict(), "finalization_epoch": 4,
        "blocks_current_surface": True,
    }])
    repository = PostgresH5TargetedRecaptureRepository(pool=FakePool(connection))
    inventory = await repository.list_snapshot_checkpoint_requests(
        task_id="task-h5", agent_run_id="run-h5",
    )
    assert inventory[0].blocks_current_surface is True
    sql = connection.fetch.call_args.args[0]
    assert "'pending', 'routed', 'running'" in sql
    assert "request.lease_token IS NOT NULL" in sql
    assert "probe.evidence_request_id = request.request_id" in sql
    assert "capture.agent_run_id = $2::uuid" in sql
    assert "request.finalization_epoch = task.finalization_epoch" in sql
    assert "lower(task.target_platform) = 'h5'" in sql
    assert "LIMIT 51" in sql


@pytest.mark.parametrize("second_ready,owned", [(True, False), (False, False), (True, True)])
async def test_mixed_snapshot_requests_are_checked_individually(second_ready, owned):
    first = _snapshot_request()
    second = snapshot_request_with(evidence_goal="连续滚动与点击反馈")
    consumer = SimpleNamespace(
        can_defer_snapshot_requests=AsyncMock(side_effect=[True, second_ready]),
    )
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer, scope_reader=SimpleNamespace(
            list_snapshot_checkpoint_requests=AsyncMock(return_value=tuple(SimpleNamespace(
                request=request, finalization_epoch=4,
                blocks_current_surface=owned and request == second,
            ) for request in (first, second))),
        ), budget=ExplorationBudget(
            max_actions=1, remaining_actions=1, max_depth=1, remaining_duration_ms=1_000,
        ),
    )
    assert await drain.can_defer_snapshot_requests(
        task_id="task-h5", agent_run_id="run-h5", expected_request_count=2,
    ) is (second_ready and not owned)
    if owned:
        consumer.can_defer_snapshot_requests.assert_not_awaited()
    else:
        requests = [
            call.kwargs["requests"] for call in consumer.can_defer_snapshot_requests.call_args_list
        ]
        assert requests == [(first,), (second,)]


@pytest.mark.parametrize("fault", ["duplicate", "other_task", "missing_reader"])
async def test_checkpoint_rejects_untrusted_inventory(fault):
    first = _snapshot_request()
    second = (first if fault == "duplicate" else snapshot_request_with(task_id="other-task"))
    consumer = SimpleNamespace(can_defer_snapshot_requests=AsyncMock(return_value=True))
    reader = SimpleNamespace()
    if fault != "missing_reader":
        reader.list_snapshot_checkpoint_requests = AsyncMock(return_value=tuple(SimpleNamespace(
            request=request, finalization_epoch=4, blocks_current_surface=False,
        ) for request in (first, second)))
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer, scope_reader=reader, budget=ExplorationBudget(
            max_actions=1, remaining_actions=1, max_depth=1, remaining_duration_ms=1_000,
        ),
    )
    assert not await drain.can_defer_snapshot_requests(
        task_id="task-h5", agent_run_id="run-h5", expected_request_count=2,
    )
    consumer.can_defer_snapshot_requests.assert_not_awaited()
