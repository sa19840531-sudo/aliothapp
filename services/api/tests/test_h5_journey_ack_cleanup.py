"""排空确认写回本身也须有界；重复取消不能被当作普通成功。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.infrastructure.db import h5_session_journey_gate as module


@pytest.mark.parametrize("unresponsive", [True, False])
async def test_guard_ack_wait_is_bounded_and_preserves_owner_cancel(monkeypatch, unresponsive):
    monkeypatch.setattr(module, "H5_JOURNEY_ACK_TIMEOUT_SECONDS", 0.03, raising=False)
    started, finish = asyncio.Event(), asyncio.Event()

    class Connection:
        def __init__(self):
            self.closed = False

        def add_termination_listener(self, _):
            pass

        def remove_termination_listener(self, _):
            pass

        def is_closed(self):
            return self.closed

        async def execute(self, sql, *_):
            if sql.startswith("UPDATE"):
                started.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    await finish.wait()
                return "UPDATE 1"
            return "INSERT 0 1"

        async def close(self, **_):
            self.closed = True

        def terminate(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(module.asyncpg, "connect", AsyncMock(return_value=connection))
    gate = module.PostgresH5SessionJourneyGate(
        delegate=InProcessSessionCommandGate(), database_url="postgresql://unused/isolated",
    )

    async def owner():
        async with gate.hold_journey(task_id="task", session_id="h5:ack"):
            pass

    task = asyncio.create_task(owner())
    try:
        await started.wait()
        if unresponsive:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            assert done, "不能卡在不可取消的排空确认SQL里"
            with pytest.raises(RuntimeError, match="未确认"):
                await task
            assert connection.closed
        else:
            task.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        finish.set()
        try:
            await task
        except (RuntimeError, asyncio.CancelledError):
            pass
