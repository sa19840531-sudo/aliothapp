from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app import worker as worker_module
from services.api.app.core.config import Settings
from services.api.app.infrastructure.queue.redis_queue import AgentRunQueueStatus


@pytest.fixture(autouse=True)
def _isolate_closed_snapshot_browser(monkeypatch):
    # 本文件验证规则收口顺序；新会话的实际执行另由专用链路用例验证。
    recovery = AsyncMock(return_value=0)
    monkeypatch.setattr(worker_module, "_drain_h5_closed_snapshots", recovery)
    return recovery


async def test_h5_closed_requests_settle_after_consumer_run_has_finished(monkeypatch):
    calls = []

    class _Service:
        async def find_active_consumer_run(self, *, task_id):
            return None

        async def has_nonterminal_work(self, *, task_id):
            return False

    class _Settlement:
        async def settle_requests_after_session_close(self, *, task_id, agent_run_id):
            calls.append((task_id, agent_run_id))
            return 1

    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: _Service())
    monkeypatch.setattr(worker_module, "PostgresH5ProbeTerminalSettlementRepository", _Settlement)
    record = SimpleNamespace(
        task_id="task-1", run_id="main-run-1", status=AgentRunQueueStatus.FAILED,
        task_config={"workflow_version": "v2", "_task_target": {"platform": "h5"},
                     "h5_inspection_notification_mode": "enforced"}, usage=None,
    )
    await worker_module._drain_snapshot_inspection_work(
        record, settings=Settings(), worker_instance_id="worker-1",
    )
    assert calls == [("task-1", "main-run-1")]


@pytest.mark.parametrize("h5_mode", ["on", "off"])
def test_stream_consumer_injects_closed_session_settlement_only_for_enabled_h5(
    h5_mode: str,
) -> None:
    settings = Settings.model_validate({"workflow": {"h5_v2_mode": h5_mode}})
    _, _, dispatcher = worker_module._build_v2_inspection_consumer(
        SimpleNamespace(), settings, worker_instance_id="worker-1",
    )
    settlement = dispatcher._consumer._h5_closed_session_settlement
    if h5_mode == "on":
        assert isinstance(
            settlement, worker_module.PostgresH5ProbeTerminalSettlementRepository,
        )
    else:
        assert settlement is None


async def test_snapshot_drain_waits_for_retry_wait_instead_of_finalizing_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        SimpleNamespace(
            claimed_count=1,
            completed_count=0,
            recovered_count=0,
            deferred_count=1,
        ),
        SimpleNamespace(
            claimed_count=1,
            completed_count=1,
            recovered_count=0,
            deferred_count=0,
        ),
    ]
    calls: list[str] = []

    class _Consumer:
        def __init__(self, **kwargs):
            del kwargs

        async def drain_snapshot(self, *, task_id, consumer_agent_run_id):
            calls.append(f"{task_id}:{consumer_agent_run_id}")
            return results.pop(0)

    async def _no_sleep(seconds: float) -> None:
        assert seconds == Settings().inspection_work_items.retry_base_seconds

    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: object())
    monkeypatch.setattr(worker_module, "InspectionWorkItemConsumer", _Consumer)
    monkeypatch.setattr(worker_module.asyncio, "sleep", _no_sleep)
    record = SimpleNamespace(
        task_id="task-1",
        run_id="run-1",
        task_config={},
        status=AgentRunQueueStatus.COMPLETED,
        usage=None,
    )

    await worker_module._drain_snapshot_inspection_work(
        record,
        settings=Settings(),
        worker_instance_id="worker-1",
    )

    assert calls == ["task-1:run-1", "task-1:run-1"]
    assert record.usage == {
        "inspection_work_items_claimed": 2,
        "inspection_work_items_completed": 1,
        "inspection_work_items_recovered": 0,
        "inspection_work_items_deferred": 1,
    }


async def test_snapshot_drain_merges_rule_judge_usage_into_main_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Consumer:
        def __init__(self, **kwargs):
            del kwargs

        async def drain_snapshot(self, *, task_id, consumer_agent_run_id):
            assert task_id == "task-1"
            assert consumer_agent_run_id == "run-1"
            return SimpleNamespace(
                claimed_count=1,
                completed_count=1,
                recovered_count=0,
                deferred_count=0,
                usage={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                    "ai_calls": 1,
                    "total_cost": 0.0001,
                },
            )

    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: object())
    monkeypatch.setattr(worker_module, "InspectionWorkItemConsumer", _Consumer)
    record = SimpleNamespace(
        task_id="task-1",
        run_id="run-1",
        task_config={},
        status=AgentRunQueueStatus.COMPLETED,
        usage={
            "input_tokens": 5,
            "output_tokens": 2,
            "total_tokens": 7,
            "ai_calls": 1,
            "total_cost": 0.00007,
        },
    )

    await worker_module._drain_snapshot_inspection_work(
        record,
        settings=Settings(),
        worker_instance_id="worker-1",
    )

    assert record.usage["input_tokens"] == 12
    assert record.usage["output_tokens"] == 5
    assert record.usage["total_tokens"] == 17
    assert record.usage["ai_calls"] == 2
    assert record.usage["total_cost"] == 0.00017
    assert record.usage["inspection_work_items_claimed"] == 1


async def test_snapshot_drain_records_usage_on_separate_consumer_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Service:
        def __init__(self) -> None:
            self.usage_records = []
            self.finish_calls = []

        async def find_active_consumer_run(self, *, task_id):
            assert task_id == "task-1"
            return "consumer-run-1"

        async def record_consumer_usage(self, *, task_id, consumer_agent_run_id, usage):
            self.usage_records.append(
                {
                    "task_id": task_id,
                    "consumer_agent_run_id": consumer_agent_run_id,
                    "usage": usage,
                }
            )
            return True

        async def finish_h5_consumer_run_if_idle(self, *, task_id, consumer_agent_run_id):
            self.finish_calls.append((task_id, consumer_agent_run_id))
            return True

    class _Consumer:
        def __init__(self, **kwargs):
            del kwargs

        async def drain_snapshot(self, *, task_id, consumer_agent_run_id):
            assert task_id == "task-1"
            assert consumer_agent_run_id == "consumer-run-1"
            return SimpleNamespace(
                claimed_count=1,
                completed_count=1,
                recovered_count=0,
                deferred_count=0,
                usage={"total_tokens": 10, "ai_calls": 1},
            )

    service = _Service()
    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: service)
    monkeypatch.setattr(worker_module, "InspectionWorkItemConsumer", _Consumer)
    record = SimpleNamespace(
        task_id="task-1",
        run_id="main-run-1",
        task_config={"h5_inspection_notification_mode": "enforced"},
        status=AgentRunQueueStatus.COMPLETED,
        usage=None,
    )

    await worker_module._drain_snapshot_inspection_work(
        record,
        settings=Settings(),
        worker_instance_id="worker-1",
    )

    assert service.usage_records == [
        {
            "task_id": "task-1",
            "consumer_agent_run_id": "consumer-run-1",
            "usage": {"total_tokens": 10, "ai_calls": 1},
        }
    ]
    assert service.finish_calls == [("task-1", "consumer-run-1")]
    assert record.usage == {
        "inspection_work_items_claimed": 1,
        "inspection_work_items_completed": 1,
        "inspection_work_items_recovered": 0,
        "inspection_work_items_deferred": 0,
    }


async def test_h5_snapshot_drain_closes_requests_without_new_session_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class _Service:
        def __init__(self) -> None:
            self.reopen_results = [2, 0]

        async def reopen_waiting_evidence(self, *, task_id: str) -> int:
            calls.append(("reopen", task_id))
            return self.reopen_results.pop(0)

        async def settle_stalled_work_for_closure(
            self,
            *,
            task_id: str,
            snapshot_generation_closed: bool = False,
        ) -> object:
            calls.append(("close", task_id, snapshot_generation_closed))
            return SimpleNamespace(
                settled=True,
                settled_work_item_count=1,
                settled_rule_count=3,
                reason_counts={"supplement_unfulfillable": 1},
            )

    class _Consumer:
        def __init__(self, **kwargs):
            del kwargs

        async def drain_snapshot(self, *, task_id, consumer_agent_run_id):
            calls.append(("drain", task_id, consumer_agent_run_id))
            return SimpleNamespace(
                claimed_count=1,
                completed_count=0,
                recovered_count=0,
                deferred_count=1,
            )

    class _SettlementRepository:
        def __init__(self, **kwargs) -> None:
            del kwargs
            self.settlement_results = [2, 0]

        async def settle_requests_after_session_close(
            self,
            *,
            task_id: str,
            agent_run_id: str,
        ) -> int:
            calls.append(("settle", task_id, agent_run_id))
            return self.settlement_results.pop(0)

    async def _no_sleep(_seconds: float) -> None:
        return None

    service = _Service()
    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: service)
    monkeypatch.setattr(worker_module, "InspectionWorkItemConsumer", _Consumer)
    monkeypatch.setattr(
        worker_module,
        "PostgresH5ProbeTerminalSettlementRepository",
        _SettlementRepository,
        raising=False,
    )
    monkeypatch.setattr(worker_module.asyncio, "sleep", _no_sleep)
    record = SimpleNamespace(
        task_id="task-h5",
        run_id="run-h5",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "h5"},
        },
        status=AgentRunQueueStatus.COMPLETED,
        usage=None,
    )

    await worker_module._drain_snapshot_inspection_work(
        record,
        settings=Settings(),
        worker_instance_id="worker-1",
    )

    assert ("settle", "task-h5", "run-h5") in calls
    assert ("reopen", "task-h5") in calls
    assert ("close", "task-h5", True) in calls
    assert record.usage["h5_snapshot_recapture_unfulfillable_count"] == 2
    assert record.usage["h5_snapshot_recapture_reopened_work_count"] == 2
    assert record.usage["h5_snapshot_settled_work_count"] == 1
    assert record.usage["h5_snapshot_settled_rule_count"] == 3


async def test_h5_snapshot_drain_uses_exact_manifest_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_arguments: dict[str, object] = {}

    class _Service:
        async def reopen_waiting_evidence(self, *, task_id: str) -> int:
            assert task_id == "task-h5"
            return 0

        async def settle_stalled_work_for_closure(
            self,
            *,
            task_id: str,
            snapshot_generation_closed: bool = False,
        ) -> object:
            assert task_id == "task-h5"
            assert snapshot_generation_closed is True
            return SimpleNamespace(
                settled=False,
                settled_work_item_count=0,
                settled_rule_count=0,
                reason_counts={},
            )

    class _Consumer:
        def __init__(self, **kwargs):
            consumer_arguments.update(kwargs)

        async def drain_snapshot(self, *, task_id, consumer_agent_run_id):
            assert (task_id, consumer_agent_run_id) == ("task-h5", "run-h5")
            return SimpleNamespace(
                claimed_count=0,
                completed_count=0,
                recovered_count=0,
                deferred_count=0,
                usage=None,
            )

    class _SettlementRepository:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def settle_requests_after_session_close(
            self,
            *,
            task_id: str,
            agent_run_id: str,
        ) -> int:
            assert (task_id, agent_run_id) == ("task-h5", "run-h5")
            return 0

    judge = object()
    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: _Service())
    monkeypatch.setattr(worker_module, "InspectionWorkItemConsumer", _Consumer)
    monkeypatch.setattr(
        worker_module,
        "PostgresH5ProbeTerminalSettlementRepository",
        _SettlementRepository,
    )
    monkeypatch.setattr(
        worker_module,
        "build_h5_targeted_recapture_rule_judge_agent",
        lambda: judge,
    )
    record = SimpleNamespace(
        task_id="task-h5",
        run_id="run-h5",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "h5"},
        },
        status=AgentRunQueueStatus.COMPLETED,
        usage=None,
    )
    settings = Settings.model_validate(
        {"supplemental_evidence": {"enabled": True}}
    )

    await worker_module._drain_snapshot_inspection_work(
        record,
        settings=settings,
        worker_instance_id="worker-1",
    )

    assert consumer_arguments["judge"] is judge


async def test_h5_snapshot_drain_replays_already_satisfied_evidence_before_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class _Service:
        def __init__(self) -> None:
            self.reopen_results = [1, 0]

        async def reopen_waiting_evidence(self, *, task_id: str) -> int:
            calls.append(("reopen", task_id))
            return self.reopen_results.pop(0)

        async def settle_stalled_work_for_closure(
            self,
            *,
            task_id: str,
            snapshot_generation_closed: bool = False,
        ) -> object:
            calls.append(("close", task_id, snapshot_generation_closed))
            return SimpleNamespace(
                settled=False,
                settled_work_item_count=0,
                settled_rule_count=0,
                reason_counts={},
            )

    class _Consumer:
        def __init__(self, **kwargs):
            del kwargs
            self.call_count = 0

        async def drain_snapshot(self, *, task_id: str, consumer_agent_run_id: str):
            self.call_count += 1
            calls.append(("drain", task_id, consumer_agent_run_id, self.call_count))
            if self.call_count == 1:
                return SimpleNamespace(
                    claimed_count=0,
                    completed_count=0,
                    recovered_count=0,
                    deferred_count=0,
                    usage=None,
                )
            if self.call_count == 2:
                return SimpleNamespace(
                    claimed_count=1,
                    completed_count=0,
                    recovered_count=0,
                    deferred_count=1,
                    usage=None,
                )
            return SimpleNamespace(
                claimed_count=1,
                completed_count=1,
                recovered_count=0,
                deferred_count=0,
                usage=None,
            )

    class _SettlementRepository:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def settle_requests_after_session_close(
            self,
            *,
            task_id: str,
            agent_run_id: str,
        ) -> int:
            calls.append(("settle", task_id, agent_run_id))
            return 0

    async def _no_sleep(_seconds: float) -> None:
        return None

    service = _Service()
    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: service)
    monkeypatch.setattr(worker_module, "InspectionWorkItemConsumer", _Consumer)
    monkeypatch.setattr(
        worker_module,
        "PostgresH5ProbeTerminalSettlementRepository",
        _SettlementRepository,
    )
    monkeypatch.setattr(worker_module.asyncio, "sleep", _no_sleep)
    record = SimpleNamespace(
        task_id="task-h5",
        run_id="run-h5",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "h5"},
        },
        status=AgentRunQueueStatus.COMPLETED,
        usage=None,
    )

    await worker_module._drain_snapshot_inspection_work(
        record,
        settings=Settings(),
        worker_instance_id="worker-1",
    )

    assert calls == [
        ("drain", "task-h5", "run-h5", 1),
        ("settle", "task-h5", "run-h5"),
        ("reopen", "task-h5"),
        ("drain", "task-h5", "run-h5", 2),
        ("drain", "task-h5", "run-h5", 3),
        ("settle", "task-h5", "run-h5"),
        ("reopen", "task-h5"),
        ("close", "task-h5", True),
    ]
    assert record.usage["inspection_work_items_claimed"] == 2
    assert record.usage["inspection_work_items_completed"] == 1
    assert record.usage["inspection_work_items_deferred"] == 1
    assert record.usage["h5_snapshot_recapture_reopened_work_count"] == 1
    assert record.usage["h5_snapshot_recapture_replayed_work_count"] == 2
