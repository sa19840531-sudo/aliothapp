"""App 活会话规则增量发布与关闭前排空测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application import (
    app_live_inspection_publish as app_live_inspection_publish_module,
)
from services.api.app.application.app_live_inspection_publish import (
    AppActiveSessionDrainState,
    AppActiveSessionInspectionDrainService,
    AppLiveInspectionCapture,
    AppLiveInspectionPublishService,
)

pytestmark = pytest.mark.asyncio


class CaptureReader:
    def __init__(self, captures=(), states=()) -> None:
        self.captures = tuple(captures)
        self.states = list(states)
        self.list_calls: list[tuple[str, str]] = []
        self.state_calls: list[tuple[str, str]] = []

    async def list_pending_captures(self, *, task_id: str, agent_run_id: str):
        self.list_calls.append((task_id, agent_run_id))
        return self.captures

    async def load_drain_state(self, *, task_id: str, agent_run_id: str):
        self.state_calls.append((task_id, agent_run_id))
        return self.states.pop(0)


class Publisher:
    def __init__(
        self,
        *,
        fail_capture_id: str | None = None,
        existing_capture_id: str | None = None,
    ) -> None:
        self.fail_capture_id = fail_capture_id
        self.existing_capture_id = existing_capture_id
        self.calls: list[dict] = []

    async def publish_for_capture(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["capture_id"] == self.fail_capture_id:
            raise RuntimeError("publish failed")
        created = kwargs["capture_id"] != self.existing_capture_id
        return SimpleNamespace(
            published_count=int(created),
            items=(
                {"work_item_id": "existing", "created": False},
                {"work_item_id": kwargs["capture_id"], "created": created},
            ),
        )


class LivePublisher:
    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.calls: list[tuple[str, str]] = []

    async def publish_pending(self, *, task_id: str, agent_run_id: str) -> int:
        self.calls.append((task_id, agent_run_id))
        return self.counts.pop(0)


class Consumer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def drain_available(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(claimed_count=1)


class MonotonicClock:
    """可控的单调时钟，用于验证排空轮次的执行耗时。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TimedLivePublisher:
    """每次发布调用推进假时钟的 App 发布替身。"""

    def __init__(self, clock: MonotonicClock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds
        self.calls = 0

    async def publish_pending(self, **kwargs) -> int:
        self.calls += 1
        self.clock.advance(self.seconds)
        return 0


class TimedConsumer:
    """每次 Judge 调用推进假时钟的 App 消费替身。"""

    def __init__(self, clock: MonotonicClock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds
        self.calls = 0

    async def drain_available(self, **kwargs) -> None:
        self.calls += 1
        self.clock.advance(self.seconds)


class TimedStateReader:
    """每次读取权威状态推进假时钟的替身。"""

    def __init__(
        self,
        clock: MonotonicClock,
        seconds: float,
        states: list[AppActiveSessionDrainState],
    ) -> None:
        self.clock = clock
        self.seconds = seconds
        self.states = states
        self.calls = 0

    async def load_drain_state(self, **kwargs) -> AppActiveSessionDrainState:
        self.calls += 1
        self.clock.advance(self.seconds)
        return self.states.pop(0)


class ClosureSettlement:
    """记录关闭前排空窗口耗尽后的既有结算调用。"""

    def __init__(self, settled_work_item_count: int = 1) -> None:
        self.settled_work_item_count = settled_work_item_count
        self.calls: list[dict] = []

    async def settle_stalled_work_for_closure(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            settled_work_item_count=self.settled_work_item_count,
            settled_rule_count=1,
        )


async def test_current_surface_checkpoint_is_bounded_and_keeps_active_rule_feedback() -> None:
    now = datetime.now(UTC)
    publisher = SimpleNamespace(
        publish_pending_batch=AsyncMock(return_value=SimpleNamespace(created_count=2))
    )
    consumer = Consumer()
    service = AppActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=consumer,
        state_reader=CaptureReader(),
        now=lambda: now,
    )

    result = await service.run_current_surface_checkpoint(
        task_id="task-1", agent_run_id="run-1", deadline=now + timedelta(seconds=10)
    )

    publisher.publish_pending_batch.assert_awaited_once_with(
        task_id="task-1", agent_run_id="run-1", limit=1, cursor=None
    )
    assert consumer.calls == [{
        "task_id": "task-1", "consumer_agent_run_id": "run-1",
        "session_dependency": "active_session", "max_items": 1,
    }]
    assert result.published_work_item_count == 2
    assert result.settled_work_item_count == 0


async def test_current_surface_checkpoint_does_not_start_after_deadline() -> None:
    now = datetime.now(UTC)
    publisher = SimpleNamespace(publish_pending_batch=AsyncMock())
    consumer = Consumer()
    service = AppActiveSessionInspectionDrainService(
        publisher=publisher, consumer=consumer, state_reader=CaptureReader(), now=lambda: now
    )

    result = await service.run_current_surface_checkpoint(
        task_id="task-1", agent_run_id="run-1", deadline=now
    )

    publisher.publish_pending_batch.assert_not_awaited()
    assert not consumer.calls
    assert result.drain_round_count == 0


async def test_current_surface_checkpoint_timeout_preserves_unfinished_work(monkeypatch) -> None:
    now = datetime.now(UTC)
    publisher = SimpleNamespace(
        publish_pending_batch=AsyncMock(return_value=SimpleNamespace(created_count=1))
    )
    interrupted = asyncio.Event()

    async def wait_for_judge(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    consumer = SimpleNamespace(drain_available=wait_for_judge)
    settlement = ClosureSettlement()
    service = AppActiveSessionInspectionDrainService(
        publisher=publisher, consumer=consumer, state_reader=CaptureReader(),
        now=lambda: now, closure_settlement=settlement,
    )
    monkeypatch.setattr(
        app_live_inspection_publish_module, "DEFAULT_CURRENT_SURFACE_CHECKPOINT_SECONDS", 0.01
    )

    result = await service.run_current_surface_checkpoint(
        task_id="task-1", agent_run_id="run-1", deadline=now + timedelta(seconds=10)
    )

    assert interrupted.is_set()
    assert result.published_work_item_count == 1
    assert result.settled_work_item_count == 0
    assert not settlement.calls


async def test_live_publish_uses_active_session_only_and_counts_created_work() -> None:
    reader = CaptureReader(
        captures=(
            AppLiveInspectionCapture(
                capture_id="capture-1",
                page_type="content_detail",
                page_state="stable",
            ),
            AppLiveInspectionCapture(
                capture_id="capture-2",
                page_type="profile",
                page_state="stable",
            ),
        )
    )
    publisher = Publisher()
    service = AppLiveInspectionPublishService(
        capture_reader=reader,
        publisher=publisher,
    )

    assert (
        await service.publish_pending(
            task_id="task-1",
            agent_run_id="run-1",
            limit=None,
        )
        == 2
    )
    assert [call["capture_id"] for call in publisher.calls] == ["capture-1", "capture-2"]
    assert all(call["active_session_only"] is True for call in publisher.calls)
    assert all(call["agent_run_id"] == "run-1" for call in publisher.calls)


async def test_live_publish_batch_advances_to_second_capture() -> None:
    reader = CaptureReader(
        captures=tuple(
            AppLiveInspectionCapture(
                capture_id=f"capture-{index}",
                page_type="content_detail",
                page_state="stable",
            )
            for index in range(2)
        )
    )
    publisher = Publisher()
    service = AppLiveInspectionPublishService(capture_reader=reader, publisher=publisher)

    first = await service.publish_pending_batch(
        task_id="task-1",
        agent_run_id="run-1",
        limit=1,
        cursor=None,
    )
    second = await service.publish_pending_batch(
        task_id="task-1",
        agent_run_id="run-1",
        limit=1,
        cursor=first.next_cursor,
    )

    assert (first.attempted_count, first.created_count, first.has_more) == (1, 1, True)
    assert (second.attempted_count, second.created_count, second.has_more) == (1, 1, False)
    assert [call["capture_id"] for call in publisher.calls] == ["capture-0", "capture-1"]


async def test_live_publish_batch_idempotent_first_capture_does_not_starve_second() -> None:
    captures = tuple(
        AppLiveInspectionCapture(
            capture_id=f"capture-{index}",
            page_type="content_detail",
            page_state="stable",
        )
        for index in range(2)
    )
    publisher = Publisher(existing_capture_id="capture-0")
    service = AppLiveInspectionPublishService(
        capture_reader=CaptureReader(captures=captures),
        publisher=publisher,
    )

    first = await service.publish_pending_batch(
        task_id="task-1", agent_run_id="run-1", limit=1, cursor=None
    )
    second = await service.publish_pending_batch(
        task_id="task-1",
        agent_run_id="run-1",
        limit=1,
        cursor=first.next_cursor,
    )

    assert (first.attempted_count, first.created_count, first.has_more) == (1, 0, True)
    assert second.created_count == 1
    assert [call["capture_id"] for call in publisher.calls] == ["capture-0", "capture-1"]


async def test_live_publish_batch_failed_first_capture_does_not_starve_second() -> None:
    captures = tuple(
        AppLiveInspectionCapture(
            capture_id=f"capture-{index}",
            page_type="content_detail",
            page_state="stable",
        )
        for index in range(2)
    )
    publisher = Publisher(fail_capture_id="capture-0")
    service = AppLiveInspectionPublishService(
        capture_reader=CaptureReader(captures=captures),
        publisher=publisher,
    )

    first = await service.publish_pending_batch(
        task_id="task-1", agent_run_id="run-1", limit=1, cursor=None
    )
    second = await service.publish_pending_batch(
        task_id="task-1",
        agent_run_id="run-1",
        limit=1,
        cursor=first.next_cursor,
    )

    assert (first.attempted_count, first.created_count, first.has_more) == (1, 0, True)
    assert second.created_count == 1
    assert [call["capture_id"] for call in publisher.calls] == ["capture-0", "capture-1"]


async def test_single_capture_publish_failure_does_not_hide_other_capture() -> None:
    reader = CaptureReader(
        captures=(
            AppLiveInspectionCapture(
                capture_id="capture-broken",
                page_type="home",
                page_state="stable",
            ),
            AppLiveInspectionCapture(
                capture_id="capture-ok",
                page_type="content_list",
                page_state="stable",
            ),
        )
    )
    service = AppLiveInspectionPublishService(
        capture_reader=reader,
        publisher=Publisher(fail_capture_id="capture-broken"),
    )

    assert await service.publish_pending(task_id="task-1", agent_run_id="run-1") == 1


async def test_close_drain_retries_until_capture_and_work_are_terminal() -> None:
    current = datetime(2026, 8, 22, tzinfo=UTC)
    clock = {"now": current}
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    reader = CaptureReader(
        states=(
            AppActiveSessionDrainState(
                pending_capture_count=0,
                nonterminal_work_item_count=1,
                next_attempt_at=current + timedelta(seconds=1),
            ),
            AppActiveSessionDrainState(
                pending_capture_count=0,
                nonterminal_work_item_count=0,
            ),
        )
    )
    publisher = LivePublisher([2, 0])
    consumer = Consumer()
    service = AppActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=consumer,
        state_reader=reader,
        grace_seconds=5,
        now=lambda: clock["now"],
        sleep=sleep,
    )

    result = await service.drain_before_close(
        task_id="task-1",
        agent_run_id="run-1",
        deadline=current + timedelta(seconds=1),
    )

    assert result.published_work_item_count == 2
    assert result.drain_round_count == 2
    assert sleeps == [1.0]
    assert all(call["session_dependency"] == "active_session" for call in consumer.calls)
    assert all(call["consumer_agent_run_id"] == "run-1" for call in consumer.calls)


async def test_close_drain_allows_progress_until_drained_within_max_wait() -> None:
    current = datetime(2026, 8, 22, tzinfo=UTC)
    clock = {"now": current}
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    reader = CaptureReader(
        states=[
            AppActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=3),
            AppActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=2),
            AppActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0),
        ]
    )
    service = AppActiveSessionInspectionDrainService(
        publisher=LivePublisher([0, 0, 0]),
        consumer=Consumer(),
        state_reader=reader,
        grace_seconds=0.2,
        max_wait_seconds=10.0,
        max_poll_seconds=1.0,
        now=lambda: clock["now"],
        sleep=sleep,
    )

    result = await service.drain_before_close(
        task_id="task-1",
        agent_run_id="run-1",
        deadline=current,
    )

    assert result.drain_round_count == 3
    assert sleeps == [1.0, 1.0]


async def test_close_drain_counts_round_execution_before_settling(monkeypatch) -> None:
    monotonic_clock = MonotonicClock()
    monkeypatch.setattr(app_live_inspection_publish_module, "monotonic", monotonic_clock)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        monotonic_clock.advance(seconds)

    state = AppActiveSessionDrainState(pending_capture_count=1, nonterminal_work_item_count=1)
    publisher = TimedLivePublisher(monotonic_clock, seconds=0.2)
    consumer = TimedConsumer(monotonic_clock, seconds=0.2)
    reader = TimedStateReader(monotonic_clock, seconds=0.2, states=[state, state])
    settlement = ClosureSettlement(settled_work_item_count=3)
    service = AppActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=consumer,
        state_reader=reader,
        grace_seconds=10.0,
        max_wait_seconds=0.5,
        max_poll_seconds=1.0,
        sleep=sleep,
        closure_settlement=settlement,
    )

    result = await service.drain_before_close(
        task_id="task-1",
        agent_run_id="run-1",
        deadline=datetime(2026, 8, 22, tzinfo=UTC),
    )

    assert result.drain_round_count == 1
    assert result.settled_work_item_count == 3
    assert publisher.calls == 1
    assert consumer.calls == 1
    assert reader.calls == 2
    assert sleeps == []
    assert settlement.calls == [{"task_id": "task-1", "active_session_agent_run_id": "run-1"}]


async def test_close_drain_counts_round_execution_and_reaches_terminal_state(monkeypatch) -> None:
    monotonic_clock = MonotonicClock()
    monkeypatch.setattr(app_live_inspection_publish_module, "monotonic", monotonic_clock)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        monotonic_clock.advance(seconds)

    publisher = TimedLivePublisher(monotonic_clock, seconds=0.1)
    consumer = TimedConsumer(monotonic_clock, seconds=0.1)
    reader = TimedStateReader(
        monotonic_clock,
        seconds=0.1,
        states=[
            AppActiveSessionDrainState(pending_capture_count=1, nonterminal_work_item_count=1),
            AppActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0),
        ],
    )
    settlement = ClosureSettlement()
    service = AppActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=consumer,
        state_reader=reader,
        grace_seconds=10.0,
        max_wait_seconds=2.0,
        max_poll_seconds=0.5,
        sleep=sleep,
        closure_settlement=settlement,
    )

    result = await service.drain_before_close(
        task_id="task-1",
        agent_run_id="run-1",
        deadline=datetime(2026, 8, 22, tzinfo=UTC),
    )

    assert result.drain_round_count == 2
    assert result.settled_work_item_count == 0
    assert publisher.calls == 2
    assert consumer.calls == 2
    assert reader.calls == 2
    assert sleeps == [0.5]
    assert settlement.calls == []


async def test_close_drain_fails_closed_when_active_work_never_finishes() -> None:
    current = datetime(2026, 8, 22, tzinfo=UTC)
    clock = {"now": current}

    async def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    reader = CaptureReader(
        states=[
            AppActiveSessionDrainState(
                pending_capture_count=1,
                nonterminal_work_item_count=1,
            )
            for _ in range(3)
        ]
    )
    service = AppActiveSessionInspectionDrainService(
        publisher=LivePublisher([0, 0, 0]),
        consumer=Consumer(),
        state_reader=reader,
        grace_seconds=0.1,
        max_poll_seconds=0.05,
        now=lambda: clock["now"],
        sleep=sleep,
    )

    with pytest.raises(RuntimeError, match="active-session 规则未排空"):
        await service.drain_before_close(
            task_id="task-1",
            agent_run_id="run-1",
            deadline=current,
        )
