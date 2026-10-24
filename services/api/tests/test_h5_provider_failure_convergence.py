"""真实 500/503 无供应商故障须跨检测、探索、Judge 收敛，不扩额或伪造用量。"""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI

from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.runtime.usage_meter import bind_run_llm_usage_meter
from services.api.app.infrastructure.llm_gateway import client_factory, h5_provider_circuit


@pytest.fixture(autouse=True)
def clear_circuit(monkeypatch):
    monkeypatch.setattr(h5_provider_circuit, "_states", h5_provider_circuit.OrderedDict())


async def post(handler, *, model="test", url="https://provider.test/v1/chat/completions"):
    async with client_factory._PhysicalRequestMeteredAsyncHttpxClient(
        usage_source="page_detection",
        usage_operation="test",
        usage_scope_type="task_run",
        transport=httpx.MockTransport(handler),
    ) as client:
        return await client.post(url, json={"model": model})


async def test_402_account_failure_stops_cross_source_calls_before_budget(monkeypatch):
    admission = AsyncMock()
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", admission)

    async def unavailable(request):
        return httpx.Response(402, json={"error": {"message": "余额不足"}}, request=request)

    with bind_agent_run_context(AgentRunContext(
        task_id=str(uuid4()), agent_run_id="main", h5_llm_budget=True,
    )):
        with bind_run_llm_usage_meter() as meter:
            assert (await post(unavailable)).status_code == 402
            for source in ("explorer", "semantic_rule_judge", "page_detection"):
                async with client_factory._PhysicalRequestMeteredAsyncHttpxClient(
                    usage_source=source, usage_operation="test", usage_scope_type="task_run",
                    transport=httpx.MockTransport(unavailable),
                ) as client:
                    with pytest.raises(h5_provider_circuit.H5ModelProviderUnavailable):
                        await client.post(
                            "https://provider.test/v1/chat/completions", json={"model": "test"},
                        )
            assert meter.ai_calls == admission.await_count == 1


def test_old_success_cannot_clear_402_and_other_identity_is_unaffected():
    request = httpx.Request(
        "POST", "https://provider.test/v1/chat/completions", json={"model": "test"},
    )
    with bind_agent_run_context(AgentRunContext(
        task_id="account-task", agent_run_id="main", h5_llm_budget=True,
    )):
        old = h5_provider_circuit.admit(request)
        denied = h5_provider_circuit.admit(request)
        h5_provider_circuit.finish(denied, 402)
        h5_provider_circuit.finish(old, 200)
        with pytest.raises(h5_provider_circuit.H5ModelProviderUnavailable):
            h5_provider_circuit.admit(request)
        assert h5_provider_circuit.admit(httpx.Request(
            "POST", str(request.url), json={"model": "another"},
        )) is not None
    with bind_agent_run_context(AgentRunContext(
        task_id="other-task", agent_run_id="main", h5_llm_budget=True,
    )):
        assert h5_provider_circuit.admit(request) is not None


async def test_three_provider_failures_stop_other_h5_consumers_before_admission(monkeypatch):
    admission = AsyncMock()
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", admission)
    sent = []

    async def handler(request):
        sent.append(request)
        return httpx.Response(
            503,
            json={
                "error": {
                    "message": "No available AI provider for model 'test' "
                    "across all groups (4 groups checked)."
                }
            },
            request=request,
        )

    with bind_agent_run_context(
        AgentRunContext(task_id=str(uuid4()), agent_run_id="main", h5_llm_budget=True)
    ):
        with bind_run_llm_usage_meter() as meter:
            for source in ("page_detection", "explorer", "semantic_rule_judge", "page_detection"):
                async with client_factory._PhysicalRequestMeteredAsyncHttpxClient(
                    usage_source=source,
                    usage_operation="test",
                    usage_scope_type="task_run",
                    transport=httpx.MockTransport(handler),
                ) as client:
                    try:
                        await client.post(
                            "https://provider.test/v1/chat/completions", json={"model": "test"}
                        )
                    except RuntimeError as error:
                        assert getattr(error, "error_code", None) == "h5_model_provider_unavailable"
            assert meter.ai_calls == 3
    assert len(sent) == admission.await_count == 3


@pytest.mark.parametrize("h5,status", [(False, 503), (True, 400), (True, 429), (True, 200)])
async def test_non_h5_and_business_errors_do_not_open_provider_circuit(monkeypatch, h5, status):
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", AsyncMock())

    async def handler(request):
        return httpx.Response(status, json={}, request=request)

    with bind_agent_run_context(
        AgentRunContext(task_id=str(uuid4()), agent_run_id="main", h5_llm_budget=h5)
    ):
        for _ in range(6):
            assert (await post(handler)).status_code == status


async def test_transient_failure_resets_and_other_task_model_endpoint_remain_usable(monkeypatch):
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", AsyncMock())

    async def unavailable(request):
        return httpx.Response(503, json={}, request=request)

    async def available(request):
        return httpx.Response(200, json={}, request=request)

    with bind_agent_run_context(
        AgentRunContext(task_id=str(uuid4()), agent_run_id="main", h5_llm_budget=True)
    ):
        for _ in range(2):
            await post(unavailable)
        await post(available)
        for _ in range(3):
            await post(unavailable)
        for _ in range(10):
            with pytest.raises(h5_provider_circuit.H5ModelProviderUnavailable):
                await post(unavailable)
        assert (await post(available, model="another")).status_code == 200
        assert (await post(available, url="https://other.test/v1/responses")).status_code == 200
    with bind_agent_run_context(
        AgentRunContext(task_id=str(uuid4()), agent_run_id="main", h5_llm_budget=True)
    ):
        assert (await post(available)).status_code == 200


async def test_cooldown_allows_one_probe_and_releases_cancelled_probe(monkeypatch):
    admission = AsyncMock()
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", admission)
    now = [10.0]
    monkeypatch.setattr(h5_provider_circuit, "monotonic", lambda: now[0])

    async def unavailable(request):
        return httpx.Response(500, json={}, request=request)

    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await asyncio.Event().wait()

    async def available(request):
        return httpx.Response(200, json={}, request=request)

    with bind_agent_run_context(
        AgentRunContext(task_id=str(uuid4()), agent_run_id="main", h5_llm_budget=True)
    ):
        for _ in range(3):
            await post(unavailable)
        now[0] += 31
        probe = asyncio.create_task(post(slow))
        await entered.wait()
        with pytest.raises(h5_provider_circuit.H5ModelProviderUnavailable):
            await post(unavailable)
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        now[0] += 31
        await post(available)
        await post(available)
    assert admission.await_count == 6


async def test_failed_cooldown_probe_converges_without_endless_physical_retries(monkeypatch):
    admission = AsyncMock()
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", admission)
    now = [0.0]
    monkeypatch.setattr(h5_provider_circuit, "monotonic", lambda: now[0])

    async def unavailable(request):
        return httpx.Response(503, json={}, request=request)

    with bind_agent_run_context(
        AgentRunContext(
            task_id=str(uuid4()),
            agent_run_id="main",
            h5_llm_budget=True,
        )
    ):
        with bind_run_llm_usage_meter() as meter:
            for _ in range(3):
                await post(unavailable)
            now[0] = 31
            await post(unavailable)
            for _ in range(10):
                now[0] += 300
                with pytest.raises(h5_provider_circuit.H5ModelProviderUnavailable):
                    await post(unavailable)
            assert meter.ai_calls == admission.await_count == 4


async def test_sdk_wrapped_retries_do_not_meter_local_circuit_rejections(monkeypatch):
    admission = AsyncMock()
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", admission)
    monkeypatch.setattr(AsyncOpenAI, "_sleep_for_retry", AsyncMock())

    async def unavailable(request):
        return httpx.Response(
            503, json={"error": {"message": "No available AI provider"}}, request=request
        )

    with bind_agent_run_context(
        AgentRunContext(
            task_id=str(uuid4()),
            agent_run_id="main",
            h5_llm_budget=True,
        )
    ):
        with bind_run_llm_usage_meter() as meter:
            for source in ("page_detection", "semantic_rule_judge"):
                transport = client_factory._PhysicalRequestMeteredAsyncHttpxClient(
                    usage_source=source,
                    usage_operation="test",
                    usage_scope_type="task_run",
                    transport=httpx.MockTransport(unavailable),
                )
                async with AsyncOpenAI(
                    api_key="test", max_retries=5, http_client=transport
                ) as client:
                    with pytest.raises(APIConnectionError) as raised:
                        await client.chat.completions.create(model="test", messages=[])
                    assert isinstance(
                        raised.value.__cause__, h5_provider_circuit.H5ModelProviderUnavailable
                    )
            assert meter.ai_calls == admission.await_count == 3


@pytest.mark.parametrize("status", [503, 200, None])
def test_old_inflight_response_cannot_release_or_fail_a_new_probe(monkeypatch, status):
    now = [0.0]
    monkeypatch.setattr(h5_provider_circuit, "monotonic", lambda: now[0])
    request = httpx.Request(
        "POST", "https://provider.test/v1/chat/completions", json={"model": "test"}
    )
    with bind_agent_run_context(
        AgentRunContext(task_id=str(uuid4()), agent_run_id="main", h5_llm_budget=True)
    ):
        old_request = h5_provider_circuit.admit(request)
        for _ in range(3):
            h5_provider_circuit.finish(h5_provider_circuit.admit(request), 503)
        now[0] = 31
        probe = h5_provider_circuit.admit(request)
        h5_provider_circuit.finish(old_request, status)
        assert probe[1].probing and not probe[1].unavailable
        with pytest.raises(h5_provider_circuit.H5ModelProviderUnavailable):
            h5_provider_circuit.admit(request)
        h5_provider_circuit.finish(probe, 200)
        assert h5_provider_circuit.admit(request) is not None
