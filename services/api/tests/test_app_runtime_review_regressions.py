"""APP 取消、截图重放及快取资源边界的回归。"""

import asyncio
import hashlib
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from services.api.app.application.app_exploration_cancellation import run_app_exploration_operation
from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseLostError,
    _RenewingLeaseGuard,
)
from services.api.app.application.xingyu_runtime.idempotency import (
    IdempotencyConflict,
    XingyuIdempotencyStore,
)
from services.api.app.core.config import WorkflowSettings
from services.api.app.infrastructure.app_runtime.official_api import HttpOfficialAppApiClient


@pytest.mark.parametrize("mode", ["direct", "nested_cancel", "nested_lease_lost"])
async def test_cancel_waits_for_owned_device_operation(mode):
    started, cleaned = asyncio.Event(), asyncio.Event()
    tasks = []

    async def operation():
        tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    class Cancellation:
        async def is_cancel_requested(self, **kwargs):
            return False

    wrapped = run_app_exploration_operation(
        operation(),
        cancellation=Cancellation(),
        task_id="test",
        agent_run_id="test",
        poll_interval_seconds=1,
    )
    guard = _RenewingLeaseGuard(
        queue=None,
        command=None,
        renew_interval_seconds=1,
        lost_error_type=AppExplorationLeaseLostError,
        lost_message="测试失租",
        task_name="test",
    )
    outer = asyncio.create_task(wrapped if mode == "direct" else guard.run(wrapped))
    await asyncio.wait_for(started.wait(), 1)
    try:
        if mode == "nested_lease_lost":
            guard._renew_failure = RuntimeError("测试失租")
            guard._renew_failure_event.set()
            expected = AppExplorationLeaseLostError
        else:
            outer.cancel()
            expected = asyncio.CancelledError
        with pytest.raises(expected):
            await asyncio.wait_for(outer, 1)
        assert cleaned.is_set()
        assert tasks[0].done()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ["capture_app", "capture_ui_tree"])
@pytest.mark.parametrize("lost_response", [True, False])
async def test_capture_replay_and_refresh_use_real_router_cache(monkeypatch, kind, lost_response):
    router = importlib.import_module("services.api.app.api.v1.xingyu_official.router")
    cache = XingyuIdempotencyStore()
    executions, requests = [], []
    data = b"test-image"

    def capture(payload):
        executions.append(payload.copy())
        if kind == "capture_ui_tree":
            return "<hierarchy/>", "request"
        return SimpleNamespace(
            width=1,
            height=1,
            orientation="portrait",
            png_bytes=data,
            sha256=hashlib.sha256(data).hexdigest(),
            package_name=None,
            activity=None,
        ), "request"

    service = SimpleNamespace(
        idempotency=cache,
        begin_idempotent=lambda key, payload: cache.lookup(key, payload=payload),
        capture=capture,
        capture_ui_tree=capture,
    )
    monkeypatch.setattr(router, "get_xingyu_official_service", lambda: service)

    async def transport(request):
        requests.append(request)
        if not lost_response and len(requests) == 1:
            return httpx.Response(502)

        async def receive():
            return {"type": "http.request", "body": request.content, "more_body": False}

        # ASGI 服务器提供小写标头名，httpx 的 raw 则保留原始大小写。
        incoming = Request(
            {"type": "http", "headers": [(k.lower(), v) for k, v in request.headers.raw]}, receive
        )
        response = await getattr(router, kind)(incoming, request.headers["Idempotency-Key"])
        if lost_response and len(requests) == 1:
            raise httpx.ReadTimeout("模拟服务端成功后响应遗失", request=request)
        return httpx.Response(
            response.status_code, content=response.body, headers=dict(response.headers)
        )

    client = HttpOfficialAppApiClient(
        settings=WorkflowSettings(
            official_app_base_url="https://test.invalid", official_app_auth_token="test"
        ),
        transport=httpx.MockTransport(transport),
    )
    try:
        response = await client._request_with_retry(
            method="POST",
            path="/capture",
            payload={"session_id": "test"},
            idempotency_key="capture-test",
            max_retries=1,
            capture_session_id="test",
        )
        assert response.status_code == 200
        assert len(executions) == 1
        assert requests[0].content == requests[1].content
        assert requests[1].headers["X-Alioth-Force-Adb-Refresh"] == "1"
        assert executions[0].get("force_adb_refresh", False) is (not lost_response)
        assert cache.lookup("capture-test", payload=json.loads(requests[0].content)) is not None
    finally:
        await client._client.aclose()


def test_png_cache_expiry_capacity_and_action_retention():
    now = [0.0]
    cache = XingyuIdempotencyStore(
        png_ttl_seconds=10, max_png_bytes=6, max_png_entries=2, clock=lambda: now[0]
    )
    cache.store_json("action", payload={}, status_code=200, body={"done": True})

    def put(key, data):
        cache.store_png(key, payload={}, status_code=200, png_bytes=data, headers={})

    put("a", b"1234")
    put("b", b"5678")
    assert cache.lookup("a", payload={}) is None
    assert cache.lookup("b", payload={}).png_bytes == b"5678"
    put("b", b"1")
    put("c", b"2")
    put("d", b"3")
    assert cache.lookup("b", payload={}) is None
    assert cache.lookup("c", payload={}) is not None
    put("huge", b"1234567")
    assert cache.lookup("huge", payload={}) is None
    with pytest.raises(IdempotencyConflict):
        cache.lookup("c", payload={"changed": True})
    now[0] = 10
    assert cache.lookup("c", payload={}) is None
    assert cache.lookup("d", payload={}) is None
    assert cache.lookup("action", payload={}).json_body == {"done": True}
    put("e", b"123456")
    cache.clear()
    put("f", b"123456")
    assert cache.lookup("f", payload={}) is not None
