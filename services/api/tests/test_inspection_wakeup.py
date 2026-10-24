"""Inspection 唤醒派发链路：Outbox 发布、DB 权威消费与重开补采工作项。"""

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from services.api.app.agents.runtime import inspection_work_item_consumer as consumer_module
from services.api.app.agents.runtime.inspection_wakeup import (
    CAPTURE_COMMITTED_MESSAGE_TYPE,
    EVIDENCE_BUNDLE_READY_MESSAGE_TYPE,
    EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE_TYPE,
    INSPECTION_STREAM_MESSAGE_TYPES,
    INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
    CompositeLiveInspectionPublisher,
    InspectionWakeupHandler,
)
from services.api.app.agents.runtime.inspection_work_item_consumer import (
    InspectionWorkItemDrainResult,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemRule,
    PublishInspectionWorkItemCommand,
    StartInspectionAttemptCommand,
)
from services.api.app.application.outbox import SUPPORTED_WAKEUP_MESSAGE_TYPES
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.queue.inbox_wakeup import InboxGuardedWakeupHandler
from services.api.app.infrastructure.queue.redis_stream import (
    DEFAULT_WAKEUP_STREAM_NAMES,
    RedisStreamWakeup,
    WakeupRoutingError,
)
from services.api.app.worker import (
    V2_INSPECTION_CONSUMER_GROUP,
    V2_INSPECTION_STREAM_NAME,
    _build_v2_inspection_consumer,
    _v2_inspection_consumer_loop,
)


def _wakeup(message_type: str) -> RedisStreamWakeup:
    return RedisStreamWakeup(
        stream_name="alioth:v2:wakeup:inspection",
        stream_entry_id="1-0",
        message_id="message:abc",
        message_type=message_type,
        task_id="task-1",
        aggregate_type="inspection_work_item",
        aggregate_id="wi-1",
        aggregate_version=0,
        idempotency_key="hash-1",
    )


class _FakeService:
    def __init__(self, *, active_run_id: str | None, events: list[str] | None = None) -> None:
        self.active_run_id = active_run_id
        self.reopen_calls: list[str] = []
        self.find_calls: list[str] = []
        self.finish_calls: list[tuple[str, str]] = []
        self.usage_records: list[dict[str, Any]] = []
        self._events = events

    async def reopen_waiting_evidence(self, *, task_id: str) -> int:
        self.reopen_calls.append(task_id)
        if self._events is not None:
            self._events.append("reopen")
        return 2

    async def find_active_consumer_run(self, *, task_id: str) -> str | None:
        self.find_calls.append(task_id)
        return self.active_run_id

    async def finish_publication_consumer_run_if_idle(
        self, *, task_id: str, consumer_agent_run_id: str
    ) -> bool:
        self.finish_calls.append((task_id, consumer_agent_run_id))
        return True

    async def record_consumer_usage(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        usage: dict[str, Any],
    ) -> bool:
        self.usage_records.append(
            {
                "task_id": task_id,
                "consumer_agent_run_id": consumer_agent_run_id,
                "usage": usage,
            }
        )
        return True


class _FakeConsumer:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._events = events
        self._usage = usage

    async def submit(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        prepare_publication: bool = False,
    ) -> bool:
        if self._events is not None:
            self._events.append("submit")
        self.calls.append(
            {
                "task_id": task_id,
                "consumer_agent_run_id": consumer_agent_run_id,
                "prepare_publication": prepare_publication,
            }
        )
        return True


# ---------------------------------------------------------------------------
# 协议词汇与路由
# ---------------------------------------------------------------------------


def test_inspection_work_available_is_supported_and_routed_to_inspection_stream() -> None:
    assert INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE in SUPPORTED_WAKEUP_MESSAGE_TYPES
    assert (
        DEFAULT_WAKEUP_STREAM_NAMES[INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE]
        == "alioth:v2:wakeup:inspection"
    )
    assert V2_INSPECTION_STREAM_NAME == "alioth:v2:wakeup:inspection"


def test_handler_message_types_cover_exactly_the_inspection_stream_routes() -> None:
    inspection_routed = {
        message_type
        for message_type, stream_name in DEFAULT_WAKEUP_STREAM_NAMES.items()
        if stream_name == "alioth:v2:wakeup:inspection"
    }
    assert INSPECTION_STREAM_MESSAGE_TYPES == inspection_routed


async def test_composite_publication_batch_caps_total_attempts_and_resumes_child() -> None:
    class _Publisher:
        def __init__(self, *, has_more: bool) -> None:
            self.has_more = has_more
            self.calls: list[dict[str, object]] = []

        async def publish_pending(self, **_kwargs: object) -> int:
            return 0

        async def publish_pending_batch(self, **kwargs: object) -> object:
            self.calls.append(dict(kwargs))
            cursor = kwargs.get("cursor")
            has_more = self.has_more and cursor is None
            return type(
                "BatchResult",
                (),
                {
                    "attempted_count": 1,
                    "created_count": 0,
                    "has_more": has_more,
                    "next_cursor": "child-cursor" if has_more else None,
                },
            )()

    first = _Publisher(has_more=False)
    second = _Publisher(has_more=True)
    composite = CompositeLiveInspectionPublisher(first, second)

    assert (
        await composite.publish_pending(
            task_id="task-1",
            agent_run_id="run-1",
            limit=None,
        )
        == 0
    )

    batch = await composite.publish_pending_batch(
        task_id="task-1",
        agent_run_id="run-1",
        limit=2,
        cursor=None,
    )

    assert batch.attempted_count == 2
    assert batch.created_count == 0
    assert batch.has_more is True
    assert [call["limit"] for call in first.calls + second.calls] == [2, 1]

    resumed = await composite.publish_pending_batch(
        task_id="task-1",
        agent_run_id="run-1",
        limit=2,
        cursor=batch.next_cursor,
    )

    assert len(first.calls) == 1
    assert second.calls[-1]["cursor"] == "child-cursor"
    assert resumed.attempted_count == 1
    assert resumed.has_more is False


# ---------------------------------------------------------------------------
# InspectionWakeupHandler
# ---------------------------------------------------------------------------


async def test_handler_rejects_foreign_message_type() -> None:
    handler = InspectionWakeupHandler(
        service=_FakeService(active_run_id=None),
        consumer=_FakeConsumer(),
    )
    with pytest.raises(WakeupRoutingError):
        await handler.handle(_wakeup("alioth.v2.exploration-work.available"))


async def test_handler_confirms_without_drain_when_no_active_run() -> None:
    service = _FakeService(active_run_id=None)
    consumer = _FakeConsumer()
    handler = InspectionWakeupHandler(service=service, consumer=consumer)

    confirmed = await handler.handle(_wakeup(INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE))

    assert confirmed is True
    assert service.find_calls == ["task-1"]
    assert consumer.calls == []


@pytest.mark.parametrize(
    "message_type",
    ["alioth.v2.capture.committed", "alioth.v2.evidence-bundle.ready"],
)
async def test_capture_manifest_wakeups_never_reopen_supplemental_work(
    message_type: str,
) -> None:
    service = _FakeService(active_run_id=None)
    consumer = _FakeConsumer()
    handler = InspectionWakeupHandler(service=service, consumer=consumer)

    confirmed = await handler.handle(_wakeup(message_type))

    assert confirmed is True
    assert service.reopen_calls == []
    assert service.finish_calls == []
    assert consumer.calls == []


async def test_handler_submits_work_without_running_judge() -> None:
    service = _FakeService(active_run_id="run-9")
    consumer = _FakeConsumer()
    handler = InspectionWakeupHandler(service=service, consumer=consumer)

    confirmed = await handler.handle(_wakeup(INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE))

    assert confirmed is True
    assert consumer.calls == [
        {
            "task_id": "task-1",
            "consumer_agent_run_id": "run-9",
            "prepare_publication": False,
        }
    ]
    assert service.reopen_calls == []
    assert service.finish_calls == []


async def test_handler_does_not_report_judge_completion_before_ack() -> None:
    service = _FakeService(active_run_id="run-9")
    consumer = _FakeConsumer(
        usage={"input_tokens": 12, "total_tokens": 20, "ai_calls": 1},
    )
    handler = InspectionWakeupHandler(service=service, consumer=consumer)

    confirmed = await handler.handle(_wakeup(INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE))

    assert confirmed is True
    assert service.usage_records == []
    assert service.finish_calls == []


async def test_handler_reopens_waiting_evidence_before_drain_on_supplement() -> None:
    events: list[str] = []
    service = _FakeService(active_run_id="run-9", events=events)
    consumer = _FakeConsumer(events=events)
    handler = InspectionWakeupHandler(service=service, consumer=consumer)

    confirmed = await handler.handle(_wakeup(EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE_TYPE))

    assert confirmed is True
    assert service.reopen_calls == ["task-1"]
    assert len(consumer.calls) == 1
    assert events == ["reopen", "submit"]


@pytest.mark.parametrize(
    "message_type",
    [CAPTURE_COMMITTED_MESSAGE_TYPE, EVIDENCE_BUNDLE_READY_MESSAGE_TYPE],
)
async def test_capture_wakeup_only_submits_publication_intent_before_ack(
    message_type: str,
) -> None:
    """Capture 唤醒 ACK 前只提交 publication 意图，不执行慢发布器。"""
    events: list[str] = []
    service = _FakeService(active_run_id="run-9", events=events)
    consumer = _FakeConsumer(events=events)
    handler = InspectionWakeupHandler(
        service=service,
        consumer=consumer,
    )

    confirmed = await handler.handle(_wakeup(message_type))

    assert confirmed is True
    assert consumer.calls == [
        {
            "task_id": "task-1",
            "consumer_agent_run_id": "run-9",
            "prepare_publication": True,
        }
    ]
    assert events == ["submit"]


async def test_capture_wakeup_skips_live_publish_without_active_run() -> None:
    consumer = _FakeConsumer()
    handler = InspectionWakeupHandler(
        service=_FakeService(active_run_id=None),
        consumer=consumer,
    )

    confirmed = await handler.handle(_wakeup(CAPTURE_COMMITTED_MESSAGE_TYPE))

    assert confirmed is True
    assert consumer.calls == []


@pytest.mark.parametrize(
    "message_type",
    [INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE, EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE_TYPE],
)
async def test_non_evidence_wakeups_do_not_trigger_live_publish(message_type: str) -> None:
    consumer = _FakeConsumer()
    handler = InspectionWakeupHandler(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
    )

    confirmed = await handler.handle(_wakeup(message_type))

    assert confirmed is True
    assert consumer.calls[0]["prepare_publication"] is False


async def test_handler_ack_does_not_wait_for_judge_drain() -> None:
    """单条高积压 wakeup 被接收后，ACK 前不能同步等待 Judge 排空 task。"""

    class _BlockingConsumer:
        def __init__(self) -> None:
            self.submissions: list[tuple[str, str]] = []

        async def submit(
            self,
            *,
            task_id: str,
            consumer_agent_run_id: str,
            prepare_publication: bool = False,
        ) -> bool:
            self.submissions.append((task_id, consumer_agent_run_id))
            return True

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            await asyncio.Event().wait()
            raise AssertionError("不可达")

    consumer = _BlockingConsumer()
    handler = InspectionWakeupHandler(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
    )

    confirmed = await asyncio.wait_for(
        handler.handle(_wakeup(INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE)),
        timeout=0.05,
    )

    assert confirmed is True
    assert consumer.submissions == [("task-1", "run-9")]


async def test_many_capture_wakeups_ack_before_slow_publication_starts() -> None:
    """多条 Capture 唤醒不在 Redis handler 内等待慢 publisher。"""

    class _BlockingPublisher:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release_after_cancel = asyncio.Event()

        async def publish_pending_batch(self, **kwargs: object) -> object:
            self.calls.append(dict(kwargs))
            self.started.set()
            while not self.release_after_cancel.is_set():
                try:
                    await self.release_after_cancel.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
            return type(
                "BatchResult",
                (),
                {
                    "attempted_count": 0,
                    "created_count": 0,
                    "has_more": False,
                    "next_cursor": None,
                },
            )()

    class _DrainConsumer:
        def __init__(self) -> None:
            self.calls = 0

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    publisher = _BlockingPublisher()
    consumer = _DrainConsumer()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        live_publisher=publisher,
        batch_limit=1,
        publication_batch_limit=2,
        publication_timeout_seconds=30,
        queue_capacity=8,
    )
    handler = InspectionWakeupHandler(
        service=_FakeService(active_run_id="run-9"),
        consumer=dispatcher,
    )

    confirmed = await asyncio.wait_for(
        asyncio.gather(
            *(handler.handle(_wakeup(CAPTURE_COMMITTED_MESSAGE_TYPE)) for _ in range(20))
        ),
        timeout=0.05,
    )

    assert confirmed == [True] * 20
    assert publisher.calls == []

    stop_event = asyncio.Event()
    workers = asyncio.create_task(dispatcher.run(stop_event, concurrency=1))
    await asyncio.wait_for(publisher.started.wait(), timeout=0.5)
    assert publisher.calls == [
        {
            "task_id": "task-1",
            "agent_run_id": "run-9",
            "limit": 2,
            "cursor": None,
        }
    ]
    stop_event.set()
    await asyncio.wait_for(publisher.cancelled.wait(), timeout=0.5)
    try:
        await asyncio.sleep(0.15)
        assert workers.done()
        assert dispatcher.stuck_publication_count == 1
        assert dispatcher.stuck_publication_total == 1
    finally:
        publisher.release_after_cancel.set()
        await asyncio.wait_for(workers, timeout=0.5)
    for _ in range(10):
        if dispatcher.stuck_publication_count == 0:
            break
        await asyncio.sleep(0)
    assert dispatcher.stuck_publication_count == 0
    assert consumer.calls == 0


async def test_coalesced_capture_wakeups_continue_until_tail_is_scanned() -> None:
    """ACK 后即使首条幂等命中，合并 wakeup 也必须继续扫描尾部 Capture。"""
    stop_event = asyncio.Event()

    class _Publisher:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        async def publish_pending_batch(self, **kwargs: object) -> object:
            cursor = kwargs.get("cursor")
            assert cursor is None or isinstance(cursor, str)
            self.cursors.append(cursor)
            if cursor is None:
                return type(
                    "BatchResult",
                    (),
                    {
                        "attempted_count": 1,
                        "created_count": 0,
                        "has_more": True,
                        "next_cursor": "capture-1",
                    },
                )()
            stop_event.set()
            return type(
                "BatchResult",
                (),
                {
                    "attempted_count": 1,
                    "created_count": 1,
                    "has_more": False,
                    "next_cursor": None,
                },
            )()

    class _Consumer:
        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    publisher = _Publisher()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=_Consumer(),
        live_publisher=publisher,
        batch_limit=1,
        publication_batch_limit=1,
        queue_capacity=8,
    )
    handler = InspectionWakeupHandler(
        service=_FakeService(active_run_id="run-9"),
        consumer=dispatcher,
    )

    confirmed = await asyncio.gather(
        *(handler.handle(_wakeup(CAPTURE_COMMITTED_MESSAGE_TYPE)) for _ in range(10))
    )
    assert confirmed == [True] * 10

    await asyncio.wait_for(dispatcher.run(stop_event, concurrency=1), timeout=0.5)

    assert publisher.cursors == [None, "capture-1"]


async def test_judge_dispatcher_coalesces_duplicate_task_wakeups() -> None:
    """不同 Redis Entry 指向同一 task/run 时只允许一个业务 drain 在途。"""

    class _BlockingConsumer:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return InspectionWorkItemDrainResult(1, 1, 0, 0)

    dispatcher_type = consumer_module.InspectionJudgeDispatcher
    consumer = _BlockingConsumer()
    dispatcher = dispatcher_type(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        batch_limit=4,
        queue_capacity=8,
    )
    stop_event = asyncio.Event()
    worker = asyncio.create_task(dispatcher.run(stop_event, concurrency=1))
    assert await dispatcher.submit(task_id="task-1", consumer_agent_run_id="run-9")
    await consumer.started.wait()

    assert await dispatcher.submit(task_id="task-1", consumer_agent_run_id="run-9")
    assert dispatcher.coalesced_count == 1
    assert consumer.calls == 1

    stop_event.set()
    consumer.release.set()
    await asyncio.wait_for(worker, timeout=0.5)


async def test_judge_dispatcher_coalesces_identity_waiting_to_reschedule() -> None:
    """尚未 flush 的续扫身份不能被新 wakeup 再入队并占两个 worker 槽。"""

    class _BlockingConsumer:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    consumer = _BlockingConsumer()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        batch_limit=1,
        queue_capacity=8,
    )
    request = consumer_module.InspectionJudgeDispatchRequest(
        task_id="task-1",
        consumer_agent_run_id="run-9",
    )
    dispatcher._reschedule.add(request)

    assert await dispatcher.submit(task_id="task-1", consumer_agent_run_id="run-9")
    dispatcher._flush_reschedule()
    assert dispatcher._queue.qsize() == 1

    stop_event = asyncio.Event()
    workers = asyncio.create_task(dispatcher.run(stop_event, concurrency=2))
    await asyncio.wait_for(consumer.started.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert consumer.calls == 1
    stop_event.set()
    consumer.release.set()
    await asyncio.wait_for(workers, timeout=0.5)


async def test_judge_dispatcher_stop_does_not_claim_queued_task() -> None:
    """关闭信号到达后，独立 Judge worker 不再从待处理队列领取新批次。"""

    class _Consumer:
        def __init__(self) -> None:
            self.calls = 0

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    dispatcher_type = consumer_module.InspectionJudgeDispatcher
    consumer = _Consumer()
    dispatcher = dispatcher_type(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        batch_limit=1,
        queue_capacity=8,
    )
    assert await dispatcher.submit(task_id="task-1", consumer_agent_run_id="run-9")
    stop_event = asyncio.Event()
    stop_event.set()

    await asyncio.wait_for(dispatcher.run(stop_event, concurrency=2), timeout=0.5)

    assert consumer.calls == 0


async def test_judge_dispatcher_rejects_submit_after_stop_begins() -> None:
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=_FakeConsumer(),
        batch_limit=1,
        queue_capacity=8,
    )
    stop_event = asyncio.Event()
    workers = asyncio.create_task(dispatcher.run(stop_event, concurrency=1))
    await asyncio.sleep(0)

    stop_event.set()

    assert dispatcher.accepting is False
    assert dispatcher.stopping is True
    assert not await dispatcher.submit(
        task_id="task-after-stop",
        consumer_agent_run_id="run-after-stop",
    )
    await asyncio.wait_for(workers, timeout=0.5)
    assert dispatcher.stopping is False
    assert dispatcher.closed is True


async def test_handler_does_not_confirm_wakeup_after_dispatcher_stops() -> None:
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=_FakeConsumer(),
        batch_limit=1,
        queue_capacity=8,
    )
    stop_event = asyncio.Event()
    workers = asyncio.create_task(dispatcher.run(stop_event, concurrency=1))
    await asyncio.sleep(0)
    stop_event.set()

    confirmed = await InspectionWakeupHandler(
        service=_FakeService(active_run_id="run-9"),
        consumer=dispatcher,
    ).handle(_wakeup(INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE))

    assert confirmed is False
    await asyncio.wait_for(workers, timeout=0.5)


async def test_judge_dispatcher_queue_full_keeps_new_wakeup_unconfirmed() -> None:
    dispatcher_type = consumer_module.InspectionJudgeDispatcher
    dispatcher = dispatcher_type(
        service=_FakeService(active_run_id="run-9"),
        consumer=_FakeConsumer(),
        batch_limit=1,
        queue_capacity=1,
    )

    assert await dispatcher.submit(task_id="task-1", consumer_agent_run_id="run-1")
    assert not await dispatcher.submit(
        task_id="task-2",
        consumer_agent_run_id="run-2",
    )


async def test_judge_dispatcher_round_robins_high_backlog_tasks() -> None:
    """单 task 达到批上限后排到队尾，不能跨批垄断单个 Judge worker。"""

    class _Consumer:
        def __init__(self, stop_event: asyncio.Event) -> None:
            self.task_order: list[str] = []
            self._stop_event = stop_event

        async def drain_available(self, **kwargs: Any) -> InspectionWorkItemDrainResult:
            task_id = str(kwargs["task_id"])
            assert kwargs["max_items"] == 1
            self.task_order.append(task_id)
            if self.task_order == ["task-hot", "task-small", "task-hot"]:
                self._stop_event.set()
            claimed = 1 if task_id == "task-hot" else 0
            return InspectionWorkItemDrainResult(claimed, claimed, 0, 0)

    stop_event = asyncio.Event()
    consumer = _Consumer(stop_event)
    dispatcher_type = consumer_module.InspectionJudgeDispatcher
    dispatcher = dispatcher_type(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        batch_limit=1,
        queue_capacity=8,
    )
    assert await dispatcher.submit(
        task_id="task-hot",
        consumer_agent_run_id="run-hot",
    )
    assert await dispatcher.submit(
        task_id="task-small",
        consumer_agent_run_id="run-small",
    )

    await asyncio.wait_for(dispatcher.run(stop_event, concurrency=1), timeout=0.5)

    assert consumer.task_order == ["task-hot", "task-small", "task-hot"]


async def test_publication_stage_uses_own_batch_limit_and_round_robins_tasks() -> None:
    """慢 publication 与 Judge 分批独立配置，热任务续扫必须回到队尾。"""
    stop_event = asyncio.Event()

    class _Publisher:
        def __init__(self) -> None:
            self.task_order: list[str] = []

        async def publish_pending_batch(self, **kwargs: object) -> object:
            assert kwargs["limit"] == 2
            task_id = str(kwargs["task_id"])
            self.task_order.append(task_id)
            if self.task_order == ["task-hot", "task-small", "task-hot"]:
                stop_event.set()
            return type(
                "BatchResult",
                (),
                {
                    "attempted_count": 2 if task_id == "task-hot" else 0,
                    "created_count": 0,
                    "has_more": task_id == "task-hot",
                    "next_cursor": "hot-cursor" if task_id == "task-hot" else None,
                },
            )()

    class _Consumer:
        async def drain_available(self, **kwargs: Any) -> InspectionWorkItemDrainResult:
            assert kwargs["max_items"] == 1
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    publisher = _Publisher()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=_Consumer(),
        live_publisher=publisher,
        batch_limit=1,
        publication_batch_limit=2,
        queue_capacity=8,
    )
    assert await dispatcher.submit(
        task_id="task-hot",
        consumer_agent_run_id="run-hot",
        prepare_publication=True,
    )
    assert await dispatcher.submit(
        task_id="task-small",
        consumer_agent_run_id="run-small",
        prepare_publication=True,
    )

    await asyncio.wait_for(dispatcher.run(stop_event, concurrency=1), timeout=0.5)

    assert publisher.task_order == ["task-hot", "task-small", "task-hot"]


async def test_publication_failure_reschedules_without_claiming_judge_complete() -> None:
    """publication 异常在独立 worker 内重试，Redis ACK 不代表 Judge 完成。"""
    stop_event = asyncio.Event()

    class _Publisher:
        def __init__(self) -> None:
            self.calls = 0

        async def publish_pending_batch(self, **_kwargs: object) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("publication unavailable")
            return type(
                "BatchResult",
                (),
                {
                    "attempted_count": 0,
                    "created_count": 0,
                    "has_more": False,
                    "next_cursor": None,
                },
            )()

    class _Consumer:
        def __init__(self) -> None:
            self.calls = 0

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            stop_event.set()
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    publisher = _Publisher()
    consumer = _Consumer()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        live_publisher=publisher,
        batch_limit=1,
        publication_batch_limit=1,
        queue_capacity=8,
        retry_delay_seconds=0.001,
    )
    assert await dispatcher.submit(
        task_id="task-1",
        consumer_agent_run_id="run-9",
        prepare_publication=True,
    )

    await asyncio.wait_for(dispatcher.run(stop_event, concurrency=1), timeout=0.5)

    assert publisher.calls == 2
    assert consumer.calls == 1


async def test_publication_timeout_reschedules_identity(caplog) -> None:
    """Publication 超时必须取消当前 await 并保留身份重试，不能宣称 Judge 完成。"""
    stop_event = asyncio.Event()

    class _Publisher:
        def __init__(self) -> None:
            self.calls = 0
            self.cancelled = 0

        async def publish_pending_batch(self, **_kwargs: object) -> object:
            self.calls += 1
            if self.calls == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
            return type(
                "BatchResult",
                (),
                {
                    "attempted_count": 0,
                    "created_count": 0,
                    "has_more": False,
                    "next_cursor": None,
                },
            )()

    class _Consumer:
        def __init__(self) -> None:
            self.calls = 0

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            stop_event.set()
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    publisher = _Publisher()
    consumer = _Consumer()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        live_publisher=publisher,
        batch_limit=1,
        publication_batch_limit=1,
        publication_timeout_seconds=0.01,
        queue_capacity=8,
        retry_delay_seconds=0.001,
    )
    assert await dispatcher.submit(
        task_id="task-1",
        consumer_agent_run_id="run-9",
        prepare_publication=True,
    )

    with caplog.at_level("ERROR"):
        await asyncio.wait_for(dispatcher.run(stop_event, concurrency=1), timeout=0.5)

    assert publisher.calls == 2
    assert publisher.cancelled == 1
    assert consumer.calls == 1
    assert "stage=publication" in caplog.text


async def test_stuck_publication_does_not_duplicate_identity_and_recovers(caplog) -> None:
    """吞取消的 Publication 仍占身份；释放前不能热循环复制或进入 Judge。"""
    stop_event = asyncio.Event()

    class _Publisher:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def publish_pending_batch(self, **_kwargs: object) -> object:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if self.calls == 1:
                    self.started.set()
                    while not self.release.is_set():
                        try:
                            await self.release.wait()
                        except asyncio.CancelledError:
                            continue
                return type(
                    "BatchResult",
                    (),
                    {
                        "attempted_count": 0,
                        "created_count": 0,
                        "has_more": False,
                        "next_cursor": None,
                    },
                )()
            finally:
                self.active -= 1

    class _Consumer:
        def __init__(self) -> None:
            self.calls = 0

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            stop_event.set()
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    publisher = _Publisher()
    consumer = _Consumer()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        live_publisher=publisher,
        batch_limit=1,
        publication_batch_limit=1,
        publication_timeout_seconds=0.01,
        queue_capacity=8,
        retry_delay_seconds=0.001,
    )
    assert await dispatcher.submit(
        task_id="task-1",
        consumer_agent_run_id="run-9",
        prepare_publication=True,
    )
    workers = asyncio.create_task(dispatcher.run(stop_event, concurrency=1))
    await asyncio.wait_for(publisher.started.wait(), timeout=0.5)

    with caplog.at_level("ERROR"):
        await asyncio.sleep(0.15)
        for _ in range(5):
            assert await dispatcher.submit(
                task_id="task-1",
                consumer_agent_run_id="run-9",
                prepare_publication=True,
            )
        await asyncio.sleep(0.05)

    assert publisher.calls == 1
    assert publisher.max_active == 1
    assert consumer.calls == 0
    assert dispatcher.stuck_publication_count == 1
    assert dispatcher.stuck_publication_total == 1
    assert "inspection_publication_stuck_fatal" in caplog.text

    publisher.release.set()
    await asyncio.wait_for(workers, timeout=0.5)

    assert publisher.calls == 2
    assert publisher.max_active == 1
    assert consumer.calls == 1
    assert dispatcher.stuck_publication_count == 0


async def test_stuck_publication_holds_global_publication_slot() -> None:
    """orphan Publication 必须继续占全局槽，不能在 worker 上限之外启动第二条。"""
    stop_event = asyncio.Event()

    class _Publisher:
        def __init__(self) -> None:
            self.task_order: list[str] = []
            self.active = 0
            self.max_active = 0
            self.first_started = asyncio.Event()
            self.second_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def publish_pending_batch(self, **kwargs: object) -> object:
            task_id = str(kwargs["task_id"])
            self.task_order.append(task_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if task_id == "task-1":
                    self.first_started.set()
                    while not self.release_first.is_set():
                        try:
                            await self.release_first.wait()
                        except asyncio.CancelledError:
                            continue
                else:
                    self.second_started.set()
                return type(
                    "BatchResult",
                    (),
                    {
                        "attempted_count": 0,
                        "created_count": 0,
                        "has_more": False,
                        "next_cursor": None,
                    },
                )()
            finally:
                self.active -= 1

    class _Consumer:
        async def drain_available(self, **kwargs: Any) -> InspectionWorkItemDrainResult:
            if kwargs["task_id"] == "task-2":
                stop_event.set()
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    publisher = _Publisher()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=_Consumer(),
        live_publisher=publisher,
        batch_limit=1,
        publication_batch_limit=1,
        publication_timeout_seconds=0.01,
        queue_capacity=8,
        retry_delay_seconds=0.001,
    )
    assert await dispatcher.submit(
        task_id="task-1",
        consumer_agent_run_id="run-1",
        prepare_publication=True,
    )
    assert await dispatcher.submit(
        task_id="task-2",
        consumer_agent_run_id="run-2",
        prepare_publication=True,
    )
    workers = asyncio.create_task(dispatcher.run(stop_event, concurrency=1))
    await asyncio.wait_for(publisher.first_started.wait(), timeout=0.5)
    await asyncio.sleep(0.15)

    assert publisher.task_order == ["task-1"]
    assert publisher.max_active == 1

    publisher.release_first.set()
    await asyncio.wait_for(publisher.second_started.wait(), timeout=0.5)
    await asyncio.wait_for(workers, timeout=0.5)

    assert publisher.max_active == 1


async def test_stop_wins_when_publication_completes_simultaneously() -> None:
    """Publication 返回前已触发 stop 时，dispatcher 不得接受结果后继续 claim。"""
    stop_event = asyncio.Event()

    class _Publisher:
        async def publish_pending_batch(self, **_kwargs: object) -> object:
            stop_event.set()
            return type(
                "BatchResult",
                (),
                {
                    "attempted_count": 1,
                    "created_count": 1,
                    "has_more": False,
                    "next_cursor": None,
                },
            )()

    class _Consumer:
        def __init__(self) -> None:
            self.calls = 0

        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            self.calls += 1
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    consumer = _Consumer()
    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=consumer,
        live_publisher=_Publisher(),
        batch_limit=1,
        publication_batch_limit=1,
        publication_timeout_seconds=0.1,
        queue_capacity=8,
    )
    assert await dispatcher.submit(
        task_id="task-1",
        consumer_agent_run_id="run-9",
        prepare_publication=True,
    )

    await asyncio.wait_for(dispatcher.run(stop_event, concurrency=1), timeout=0.5)

    assert consumer.calls == 0


async def test_judge_dispatcher_runs_distinct_tasks_with_bounded_concurrency() -> None:
    started_tasks: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()
    stop_event = asyncio.Event()

    class _Consumer:
        async def drain_available(self, **kwargs: Any) -> InspectionWorkItemDrainResult:
            started_tasks.add(str(kwargs["task_id"]))
            if len(started_tasks) == 2:
                both_started.set()
            await release.wait()
            return InspectionWorkItemDrainResult(0, 0, 0, 0)

    dispatcher = consumer_module.InspectionJudgeDispatcher(
        service=_FakeService(active_run_id="run-9"),
        consumer=_Consumer(),
        batch_limit=1,
        queue_capacity=8,
    )
    assert await dispatcher.submit(task_id="task-1", consumer_agent_run_id="run-1")
    assert await dispatcher.submit(task_id="task-2", consumer_agent_run_id="run-2")
    workers = asyncio.create_task(dispatcher.run(stop_event, concurrency=2))

    await asyncio.wait_for(both_started.wait(), timeout=0.5)
    stop_event.set()
    release.set()
    await asyncio.wait_for(workers, timeout=0.5)

    assert started_tasks == {"task-1", "task-2"}


async def test_judge_dispatcher_records_usage_only_after_batch_completion() -> None:
    service = _FakeService(active_run_id="run-9")
    stop_event = asyncio.Event()

    class _Consumer:
        async def drain_available(self, **_kwargs: Any) -> InspectionWorkItemDrainResult:
            stop_event.set()
            return InspectionWorkItemDrainResult(
                0,
                0,
                0,
                0,
                usage={"input_tokens": 12, "total_tokens": 20, "ai_calls": 1},
            )

    dispatcher_type = consumer_module.InspectionJudgeDispatcher
    dispatcher = dispatcher_type(
        service=service,
        consumer=_Consumer(),
        batch_limit=1,
        queue_capacity=8,
    )
    assert await dispatcher.submit(task_id="task-1", consumer_agent_run_id="run-9")

    await asyncio.wait_for(dispatcher.run(stop_event, concurrency=1), timeout=0.5)

    assert service.usage_records == [
        {
            "task_id": "task-1",
            "consumer_agent_run_id": "run-9",
            "usage": {"input_tokens": 12, "total_tokens": 20, "ai_calls": 1},
        }
    ]
    assert service.finish_calls == [("task-1", "run-9")]


# ---------------------------------------------------------------------------
# 仓储：发布事务内写 Outbox、重开与消费 Run 查询
# ---------------------------------------------------------------------------


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> "_RecordingConnection":
        return self

    async def __aenter__(self) -> "_RecordingConnection":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.executed.append((query, args))
        return {"id": "wi-1", "status": "pending", "created": True}

    async def fetchval(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        if "qc_outbox_event" in query:
            return str(args[0])
        return "member-1"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append((query, args))
        if query.rstrip().endswith("FOR UPDATE OF work, member, execution"):
            return []
        return [
            {
                "id": "wi-1",
                "task_id": "task-1",
                "page_capture_id": "capture-1",
                "protocol_version": "inspection-work-item.v1",
                "batch_key": "batch-1",
                "strategy": "llm",
                "category": "text",
                "session_dependency": "snapshot",
                "input_hash": "input-hash-1",
                "control_mode": "enforced",
                "row_version": 2,
            },
            {
                "id": "wi-2",
                "task_id": "task-1",
                "page_capture_id": "capture-2",
                "protocol_version": "inspection-work-item.v1",
                "batch_key": "batch-2",
                "strategy": "llm",
                "category": "text",
                "session_dependency": "snapshot",
                "input_hash": "input-hash-2",
                "control_mode": "enforced",
                "row_version": 3,
            },
        ]


class _RecordingPool:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    def acquire(self) -> _RecordingConnection:
        return self._connection


def _publish_command(control_mode: str) -> PublishInspectionWorkItemCommand:
    return PublishInspectionWorkItemCommand(
        task_id="task-1",
        page_capture_id="capture-1",
        producer_agent_run_id="run-1",
        ruleset_id="ruleset-1",
        ruleset_version=1,
        batch_key="batch-1",
        strategy="llm",
        session_dependency="snapshot",
        rules=(
            InspectionWorkItemRule(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="hash-rule-1",
            ),
        ),
        control_mode=control_mode,
    )


async def test_publish_enforced_emits_inspection_work_available_in_transaction() -> None:
    connection = _RecordingConnection()
    repository = PostgresInspectionWorkItemRepository(pool=_RecordingPool(connection))

    published = await repository.publish(
        command=_publish_command("enforced"),
        input_hash="input-hash-1",
    )

    assert published.work_item_id == "wi-1"
    outbox_writes = [
        (query, args)
        for query, args in connection.executed
        if "INSERT INTO qc_outbox_event" in query
    ]
    assert len(outbox_writes) == 1
    _, outbox_args = outbox_writes[0]
    assert INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE in outbox_args
    assert "input-hash-1" in outbox_args
    assert outbox_args[2:5] == ("inspection_work_item", "wi-1", 0)


async def test_publish_shadow_does_not_emit_wakeup_event() -> None:
    connection = _RecordingConnection()
    repository = PostgresInspectionWorkItemRepository(pool=_RecordingPool(connection))

    await repository.publish(command=_publish_command("shadow"), input_hash="input-hash-1")

    assert not any("INSERT INTO qc_outbox_event" in query for query, _ in connection.executed)


async def test_h5_publication_can_defer_work_wakeup_until_links_are_atomic() -> None:
    connection = _RecordingConnection()
    repository = PostgresInspectionWorkItemRepository(pool=_RecordingPool(connection))

    await repository.publish(
        command=replace(_publish_command("enforced"), defer_wakeup=True),
        input_hash="input-hash-1",
    )

    assert not any("INSERT INTO qc_outbox_event" in query for query, _ in connection.executed)


async def test_reopen_waiting_evidence_sql_guards_epoch_requests_and_attempts() -> None:
    connection = _RecordingConnection()
    repository = PostgresInspectionWorkItemRepository(pool=_RecordingPool(connection))

    count = await repository.reopen_waiting_evidence(task_id="task-1")

    assert count == 2
    proof_reads = [
        (query, args)
        for query, args in connection.executed
        if query.rstrip().endswith("FOR UPDATE OF work, member, execution")
    ]
    assert len(proof_reads) == 1
    assert proof_reads[0][1] == ("task-1",)
    query = next(
        executed_query
        for executed_query, _args in connection.executed
        if "UPDATE qc_inspection_work_item AS work_item" in executed_query
    )
    assert "status = 'waiting_evidence'" in query
    assert "task.status IN ('running', 'review')" in query
    assert "work_item.finalization_epoch = task.finalization_epoch" in query
    assert "candidate_work AS MATERIALIZED" in query
    assert "eligible_work AS MATERIALIZED" in query
    assert "terminal_execution_source AS MATERIALIZED" in query
    assert "DISTINCT ON (" in query
    assert "request.task_id" in query
    assert "request.rule_id" in query
    assert "work_item.page_capture_id" in query
    assert "work_item.attempt_count < work_item.max_attempts" in query
    assert "work_item.finalization_epoch = request.finalization_epoch" in query
    assert "work_item.page_capture_id = request.target_capture_id" in query
    assert "attempt.consumer_agent_run_id = work_item.consumer_agent_run_id" in query
    # 尝试次数用尽只挡「重开」，不挡终态结算：条件必须位于可重开工作项分支。
    eligible_sql = query.split("eligible_work AS MATERIALIZED", 1)[1].split(
        "satisfied_evidence AS MATERIALIZED", 1
    )[0]
    terminalization_sql = query.split("satisfied_evidence AS MATERIALIZED", 1)[1].split(
        "reopened AS", 1
    )[0]
    assert "work_item.attempt_count < work_item.max_attempts" in eligible_sql
    assert "work_item.attempt_count < work_item.max_attempts" not in terminalization_sql
    reopened_sql = query.split("reopened AS", 1)[1]
    assert "work_item.attempt_count < work_item.max_attempts" in reopened_sql
    # 终态执行、复核项、成员回写都按 (task, rule, capture) 关联，同一规则多页不交叉
    assert "SELECT DISTINCT ON (request.task_id, request.rule_id, request.page_capture_id)" in query
    # 重用终态、复核和成员回填三个分支都必须独立绑定，不能靠字符串出现次数。
    for start, end in (
        ("terminal_execution AS MATERIALIZED", "review_item AS"),
        ("review_item AS", "terminal_member AS"),
        ("terminal_member AS", "closed_work AS"),
    ):
        branch = query.split(start, 1)[1].split(end, 1)[0]
        assert "request.task_id = execution.task_id" in branch
        assert "request.rule_id = execution.rule_id" in branch
        assert "request.page_capture_id = execution.page_capture_id" in branch
    assert "eligible_work.attempt_count < eligible_work.max_attempts" in query
    assert "work_item.page_capture_id = execution.page_capture_id" in query
    assert "request.status IN (" in query
    assert "'unfulfillable', 'expired', 'cancelled'" in query
    assert "satisfied_evidence AS MATERIALIZED" in query
    assert "supplement.manifest_id = request.resolution_manifest_id" in query
    assert "manifest.quality_status = 'pass'" in query
    assert "capture.capture_status = 'captured'" in query
    assert "request.status = 'satisfied'" in query
    assert "INSERT INTO qc_rule_execution" in query
    assert "'evidence_insufficient'" in query
    assert "'evidence-request-terminal:' || request.request_id" in query
    assert "INSERT INTO qc_review_item" in query
    assert "work_item.page_capture_id = execution.page_capture_id" in query
    assert "work_item.consumer_agent_run_id=execution.agent_run_id" in query
    assert "verified.proof=live.proof" in query
    assert "proof.work_id=work_item.id" in query
    assert "proof.rule_id=execution.rule_id" in query
    assert "proof.execution_id=execution.id" in query
    assert "request.work_item_id = work_item.id" in query
    assert "terminal_member AS" in query
    assert "closed_work AS" in query
    assert "FROM candidate_work AS candidate" in query
    assert "THEN 'partial'" in query
    assert "reopened AS" in query
    assert "status = 'pending'" in query
    assert "SELECT * FROM reopened" in query
    outbox_writes = [
        (executed_query, args)
        for executed_query, args in connection.executed
        if "INSERT INTO qc_outbox_event" in executed_query
    ]
    assert len(outbox_writes) == 2
    assert all(INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE in args for _query, args in outbox_writes)
    assert outbox_writes[0][1][8] == "evidence-reopen:wi-1:2"
    assert outbox_writes[1][1][8] == "evidence-reopen:wi-2:3"


async def test_attempt_persists_effective_hash_but_fences_on_work_hash() -> None:
    connection = _RecordingConnection()
    repository = PostgresInspectionWorkItemRepository(pool=_RecordingPool(connection))

    attempt_id = await repository.start_attempt(
        command=StartInspectionAttemptCommand(
            work_item_id="wi-1",
            consumer_agent_run_id="run-1",
            attempt_no=2,
            lease_token="lease-1",
            input_hash="supplemental-attempt-hash",
            work_item_input_hash="base-work-hash",
        )
    )

    assert attempt_id == "member-1"
    query, args = connection.executed[-1]
    assert "work_item.input_hash = $6::varchar" in query
    assert args[-2:] == ("supplemental-attempt-hash", "base-work-hash")


async def test_find_active_consumer_run_requires_running_run_and_open_task() -> None:
    connection = _RecordingConnection()
    repository = PostgresInspectionWorkItemRepository(pool=_RecordingPool(connection))

    run_id = await repository.find_active_consumer_run(task_id="task-1")

    assert run_id == "member-1"
    query = connection.executed[-1][0]
    assert "run.status = 'running'" in query
    assert "task.finalization_state IN" in query
    assert "qc_h5_inspection_publication_work_item" in query
    assert "qc_app_inspection_publication_work_item" in query
    assert "app_inspection_publication" in query
    assert "priority" in query


async def test_h5_logical_consumer_run_closes_only_after_linked_work_is_terminal() -> None:
    connection = _RecordingConnection()
    repository = PostgresInspectionWorkItemRepository(pool=_RecordingPool(connection))

    closed = await repository.finish_h5_consumer_run_if_idle(
        task_id="task-1",
        consumer_agent_run_id="run-judge-1",
    )

    assert closed is True
    lifecycle_query = next(
        query
        for query, _args in connection.executed
        if "consumer.agent_type = 'rule_judge'" in query
    )
    assert "h5_inspection_publication" in lifecycle_query
    assert "qc_h5_inspection_publication_work_item" in lifecycle_query
    assert "work_item.status NOT IN" in lifecycle_query
    retry_query = next(query for query, _args in connection.executed if "UPDATE qc_task" in query)
    assert "finalization_retry_at = now()" in retry_query
    assert "finalization_epoch" in retry_query
    assert "consumer.input_context->>'finalization_epoch'" in retry_query
    assert "$2::uuid" in retry_query
    retry_args = next(args for query, args in connection.executed if "UPDATE qc_task" in query)
    assert retry_args == ("task-1", "run-judge-1")


# ---------------------------------------------------------------------------
# Worker 消费循环
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("h5_v2_mode", ["on", "off"])
def test_worker_wires_inspection_stream_through_stable_inbox_consumer(
    h5_v2_mode: str,
) -> None:
    class _InspectionSettings:
        lease_seconds = 180
        retry_base_seconds = 2.0
        max_attempts = 3

    class _SupplementalEvidenceSettings:
        enabled = False

    class _WorkflowSettings:
        pass

    class _Settings:
        inspection_work_items = _InspectionSettings()
        supplemental_evidence = _SupplementalEvidenceSettings()
        workflow = _WorkflowSettings()

    settings = _Settings()
    settings.workflow.h5_v2_mode = h5_v2_mode

    stream_consumer, dispatcher, judge_dispatcher = _build_v2_inspection_consumer(
        object(),
        settings,
        worker_instance_id="worker-instance-a",
    )

    assert stream_consumer._group_name == V2_INSPECTION_CONSUMER_GROUP
    assert stream_consumer._consumer_name == "worker-instance-a:inspection"
    assert isinstance(dispatcher._handler, InboxGuardedWakeupHandler)
    assert dispatcher._handler._consumer_name == V2_INSPECTION_CONSUMER_GROUP
    assert dispatcher._handler._lease_owner == "worker-instance-a:inspection-inbox"
    policy = judge_dispatcher._consumer._supplemental_evidence_policy
    assert policy.enabled is False
    assert policy.runtime_ready is False
    live_publisher = judge_dispatcher._live_publisher
    assert isinstance(live_publisher, CompositeLiveInspectionPublisher)
    assert len(live_publisher._publishers) == (2 if h5_v2_mode == "on" else 1)


def test_worker_inspection_consumer_uses_explicit_h5_lab_policy() -> None:
    class _InspectionSettings:
        lease_seconds = 180
        retry_base_seconds = 2.0
        max_attempts = 3

    class _SupplementalEvidenceSettings:
        enabled = False

    class _WorkflowSettings:
        h5_v2_mode = "on"
        web_v2_mode = "off"

    class _Settings:
        inspection_work_items = _InspectionSettings()
        supplemental_evidence = _SupplementalEvidenceSettings()
        workflow = _WorkflowSettings()

    policy = build_lab_supplemental_evidence_policy()
    _stream_consumer, _dispatcher, judge_dispatcher = _build_v2_inspection_consumer(
        object(),
        _Settings(),
        worker_instance_id="h5-lab-worker",
        supplemental_evidence_policy=policy,
        supplemental_evidence_target_platforms=frozenset({"h5"}),
    )

    assert judge_dispatcher._consumer._supplemental_evidence_policy is policy
    assert judge_dispatcher._consumer._supplemental_evidence_target_platforms == frozenset({"h5"})


def test_worker_h5_inspection_consumer_uses_exact_manifest_judge(monkeypatch) -> None:
    class _InspectionSettings:
        lease_seconds = 180
        retry_base_seconds = 2.0
        max_attempts = 3

    class _SupplementalEvidenceSettings:
        enabled = True

    class _WorkflowSettings:
        h5_v2_mode = "on"
        web_v2_mode = "off"

    class _Settings:
        inspection_work_items = _InspectionSettings()
        supplemental_evidence = _SupplementalEvidenceSettings()
        workflow = _WorkflowSettings()

    judge = object()
    monkeypatch.setattr(
        "services.api.app.worker.build_h5_targeted_recapture_rule_judge_agent",
        lambda: judge,
    )

    _stream_consumer, _dispatcher, judge_dispatcher = _build_v2_inspection_consumer(
        object(),
        _Settings(),
        worker_instance_id="h5-judge-worker",
    )

    assert judge_dispatcher._consumer._judge is judge


class _LoopStreamConsumer:
    def __init__(self) -> None:
        self.ensure_calls = 0
        self.claim_calls = 0

    async def ensure_groups(self) -> None:
        self.ensure_calls += 1

    async def claim_stale(self, *, stream_name: str, min_idle_milliseconds: int) -> Any:
        self.claim_calls += 1

        class _Batch:
            wakeups: tuple[Any, ...] = ()

        return _Batch()


class _LoopDispatcher:
    def __init__(self, stop_event: asyncio.Event, *, fail_once: bool = False) -> None:
        self.stop_event = stop_event
        self.fail_once = fail_once
        self.receive_calls = 0

    async def dispatch(self, wakeup: Any) -> None:
        raise AssertionError("空批次不应触发 dispatch")

    async def receive_and_dispatch(self, *, count: int, block_milliseconds: int) -> tuple:
        self.receive_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("redis unavailable")
        self.stop_event.set()
        return ()


async def test_inspection_loop_ensures_groups_then_receives_and_stops() -> None:
    stop_event = asyncio.Event()
    stream_consumer = _LoopStreamConsumer()
    dispatcher = _LoopDispatcher(stop_event)

    await _v2_inspection_consumer_loop(
        stream_consumer,
        dispatcher,
        stop_event,
        receive_block_milliseconds=1,
        stale_claim_interval_seconds=60,
    )

    assert stream_consumer.ensure_calls == 1
    assert stream_consumer.claim_calls == 1
    assert dispatcher.receive_calls == 1


async def test_inspection_loop_survives_transient_redis_failure() -> None:
    stop_event = asyncio.Event()
    stream_consumer = _LoopStreamConsumer()
    dispatcher = _LoopDispatcher(stop_event, fail_once=True)

    await _v2_inspection_consumer_loop(
        stream_consumer,
        dispatcher,
        stop_event,
        receive_block_milliseconds=1,
        stale_claim_interval_seconds=60,
    )

    assert dispatcher.receive_calls == 2
