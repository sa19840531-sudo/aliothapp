"""H5 实际请求准入必须先于发送，SDK 重试与独立质检消费者共同计数。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.runtime.usage_meter import bind_run_llm_usage_meter
from services.api.app.infrastructure.llm_gateway.client_factory import (
    _PhysicalRequestMeteredAsyncHttpxClient,
)


async def test_h5_physical_budget_denial_never_sends_or_invents_usage(monkeypatch):
    from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
    from services.api.app.infrastructure.llm_gateway import client_factory

    admission = AsyncMock(side_effect=H5LLMBudgetExhausted())
    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", admission)
    sent = []

    async def handler(request):
        sent.append(request)
        return httpx.Response(200, json={}, request=request)

    async with _PhysicalRequestMeteredAsyncHttpxClient(
        usage_source="semantic_rule_judge",
        usage_operation="judge",
        usage_scope_type="task_run",
        transport=httpx.MockTransport(handler),
    ) as client:
        with bind_run_llm_usage_meter() as meter:
            with pytest.raises(H5LLMBudgetExhausted):
                await client.post("https://example.test/v1/chat/completions", json={})
        assert meter.ai_calls == 0
    assert not sent
    admission.assert_awaited_once()


@pytest.mark.parametrize("h5", [False, True])
async def test_only_explicit_h5_context_uses_shared_durable_budget(monkeypatch, h5):
    from services.api.app.application.h5_llm_budget import reserve_current_h5_llm_call
    from services.api.app.infrastructure.db.h5_llm_budget_repository import (
        PostgresH5LLMBudgetRepository,
    )

    reserve = AsyncMock()
    monkeypatch.setattr(PostgresH5LLMBudgetRepository, "reserve", reserve)
    with bind_agent_run_context(
        AgentRunContext(
            task_id="task",
            agent_run_id="independent-judge",
            h5_llm_budget=h5,
        )
    ):
        await reserve_current_h5_llm_call()
    assert reserve.await_count == int(h5)


async def test_three_consumers_and_retries_cannot_admit_more_than_frozen_limit(monkeypatch):
    from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
    from services.api.app.infrastructure.llm_gateway import client_factory

    used = 198
    lock = asyncio.Lock()
    sent = 0

    async def reserve():
        nonlocal used
        async with lock:
            if used >= 200:
                raise H5LLMBudgetExhausted()
            used += 1

    async def handler(request):
        nonlocal sent
        sent += 1
        await asyncio.sleep(0)
        return httpx.Response(503, json={"error": {"message": "retry"}}, request=request)

    monkeypatch.setattr(client_factory, "reserve_current_h5_llm_call", reserve)
    from openai import AsyncOpenAI

    async with AsyncOpenAI(
        api_key="test",
        base_url="https://example.test/v1",
        max_retries=2,
        http_client=_PhysicalRequestMeteredAsyncHttpxClient(
            usage_source="page_detection",
            usage_operation="detect",
            usage_scope_type="task_run",
            transport=httpx.MockTransport(handler),
        ),
    ) as client:
        with bind_run_llm_usage_meter() as meter:
            await asyncio.gather(
                *(
                    client.chat.completions.create(
                        model="test",
                        messages=[{"role": "user", "content": "test"}],
                    )
                    for _ in range(3)
                ),
                return_exceptions=True,
            )
        assert meter.ai_calls == 2
    assert used == 200 and sent == 2


@pytest.mark.parametrize("platform", ["h5", "web", "app"])
async def test_independent_judge_has_h5_admission_and_does_not_retry_exhaustion(platform):
    from services.api.app.agents.runtime.inspection_work_item_consumer import (
        InspectionWorkItemConsumer,
    )
    from services.api.app.agents.runtime.run_context import get_agent_run_context
    from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
    from services.api.tests.test_inspection_work_item_consumer import FakeResultWriter, FakeService

    events = []
    service = FakeService(events, target_platform=platform)

    class Judge:
        async def judge(self, handoff):
            assert get_agent_run_context().h5_llm_budget is (platform == "h5")
            try:
                raise H5LLMBudgetExhausted()
            except H5LLMBudgetExhausted as cause:
                raise RuntimeError("SDK 包装异常") from cause

    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=Judge(),
        result_writer=FakeResultWriter(events),
    )
    result = await consumer.drain_snapshot(
        task_id="task", consumer_agent_run_id="independent-judge"
    )
    assert result.completed_count == 0
    assert "write" not in events
    final_attempt = service.attempt_transitions[-1]
    assert final_attempt.target_status.value == (
        "terminal_error" if platform == "h5" else "retryable_error"
    )
    if platform == "h5":
        assert final_attempt.error_code == "h5_llm_call_budget_exhausted"


@pytest.mark.parametrize("initialize_fails", [False, True])
async def test_production_h5_executor_initializes_original_budget_before_open(initialize_fails):
    from services.api.app.agents.runtime.run_context import get_agent_run_context
    from services.api.app.application.h5_runtime_composition import ProductionH5V2RuntimeExecutor
    from services.api.app.core.config import Settings
    from services.api.tests.test_h5_runtime_composition import _h5_record, _runtime_settings

    now = datetime.now(UTC)
    record = _h5_record().model_copy(update={"started_at": now - timedelta(seconds=30)})
    limits = _runtime_settings(max_ai_calls=200)
    events = []

    async def initialize(**kwargs):
        assert get_agent_run_context().h5_llm_budget
        assert kwargs == {
            "task_id": record.task_id,
            "agent_run_id": record.run_id,
            "max_calls": 200,
            "deadline": record.started_at + timedelta(seconds=limits.max_duration_seconds),
        }
        events.append("budget")
        if initialize_fails:
            raise RuntimeError("预算初始化未通过身份围栏")

    async def start(command):
        assert command.deadline == record.started_at + timedelta(
            seconds=limits.max_duration_seconds
        )
        events.append("open")
        raise RuntimeError("模拟启动失败，不执行探索")

    runtime = SimpleNamespace(
        runtime_settings=limits,
        start=start,
        close=AsyncMock(),
    )
    await ProductionH5V2RuntimeExecutor(
        worker_instance_id="worker",
        now=lambda: now,
        physical_budget_repository=SimpleNamespace(initialize=initialize),
    ).run(runtime, record=record, settings=Settings())
    assert events == (["budget"] if initialize_fails else ["budget", "open"])
    assert runtime.close.await_count == int(not initialize_fails)


@pytest.mark.parametrize("budget_cause", [True, False])
def test_wrapped_budget_failure_does_not_hide_cause_or_label_other_errors(budget_cause):
    from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
    from services.api.app.application.h5_runtime_composition import _failure_context

    wrapped = RuntimeError("SDK 包装失败")
    wrapped.__cause__ = H5LLMBudgetExhausted() if budget_cause else OSError("连接失败")
    assert _failure_context(wrapped) == (
        None,
        "h5_llm_call_budget_exhausted" if budget_cause else None,
    )


@pytest.mark.parametrize("exhausted", [False, True, None])
async def test_budget_read_is_read_only_and_fenced_to_exact_main_run(exhausted):
    from services.api.app.infrastructure.db.h5_llm_budget_repository import (
        PostgresH5LLMBudgetRepository,
    )

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetchrow(self, sql, *args):
            assert sql.lstrip().startswith("SELECT")
            assert args == ("task", "main-run", 4)
            assert "task.target_platform = 'h5'" in sql
            assert "budget.agent_run_id = $2::uuid" in sql
            assert "task.finalization_epoch = $3" in sql
            assert "budget.finalization_epoch = $3" in sql
            assert "budget.deadline <= now()" in sql
            return None if exhausted is None else {"exhausted": exhausted}

    repository = PostgresH5LLMBudgetRepository(
        pool=SimpleNamespace(acquire=lambda: Connection()),
    )
    if exhausted is None:
        with pytest.raises(RuntimeError, match="围栏"):
            await repository.is_exhausted(
                task_id="task", agent_run_id="main-run", finalization_epoch=4,
            )
    else:
        assert await repository.is_exhausted(
            task_id="task", agent_run_id="main-run", finalization_epoch=4,
        ) is exhausted
