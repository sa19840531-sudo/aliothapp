"""现有 Worker 内 V2 可靠派发泵的停止与故障隔离测试。"""

import asyncio

from services.api.app.worker import _v2_reliable_delivery_loop


class RecordingRuntime:
    def __init__(self, stop_event: asyncio.Event, *, fail_once: bool = False) -> None:
        self.stop_event = stop_event
        self.fail_once = fail_once
        self.sweep_calls = 0
        self.relay_calls = 0

    async def sweep_once(self):
        self.sweep_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("postgres unavailable")
        self.stop_event.set()

    async def relay_once(self):
        self.relay_calls += 1


async def test_delivery_loop_runs_initial_database_sweep_and_stops() -> None:
    stop_event = asyncio.Event()
    runtime = RecordingRuntime(stop_event)

    await _v2_reliable_delivery_loop(
        runtime,
        stop_event,
        relay_interval_seconds=0.001,
        sweep_interval_seconds=60,
    )

    assert runtime.sweep_calls == 1
    assert runtime.relay_calls == 0


async def test_delivery_loop_survives_transient_database_failure() -> None:
    stop_event = asyncio.Event()
    runtime = RecordingRuntime(stop_event, fail_once=True)

    await _v2_reliable_delivery_loop(
        runtime,
        stop_event,
        relay_interval_seconds=0.001,
        sweep_interval_seconds=60,
    )

    assert runtime.sweep_calls == 2
