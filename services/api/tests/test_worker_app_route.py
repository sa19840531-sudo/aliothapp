"""Worker 对 App Workflow 组合根的注入测试。"""

import asyncio

import pytest

from services.api.app import worker as worker_module
from services.api.app.core.config import Settings
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore


@pytest.mark.asyncio
async def test_all_worker_loops_receive_the_same_app_handler(monkeypatch) -> None:
    received: list[tuple[object, object]] = []

    async def _loop(
        *_args,
        app_workflow_handler=None,
        sealed_inspection_publisher=None,
        **_kwargs,
    ) -> None:
        received.append((app_workflow_handler, sealed_inspection_publisher))

    monkeypatch.setattr(worker_module, "_worker_loop", _loop)
    handler = object()
    publisher = object()
    tasks = worker_module._start_worker_loops(
        concurrency=3,
        queue=object(),
        settings=Settings(),
        stop_event=asyncio.Event(),
        state_store=InMemoryAgentRunStateStore(),
        worker_instance_id="worker-1",
        app_workflow_handler=handler,
        sealed_inspection_publisher=publisher,
    )
    await asyncio.gather(*tasks)

    assert received == [(handler, publisher), (handler, publisher), (handler, publisher)]
