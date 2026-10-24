"""agents/runtime 取消能力测试：验证 CancellationCheckMiddleware 在真实 Agent 循环中的行为。

沿用 test_agent_runtime_integration.py 的"真实 Agent + 假 ChatClient"集成测试方式。
"""

from collections.abc import Callable, Sequence
from typing import Any

from agent_framework import Agent, SupportsChatGetResponse, tool

from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.agent_factory import build_main_agent, run_main_agent
from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.core.config import AgentRuntimeSettings
from services.api.tests.support.fake_chat_client import (
    NonCompliantChatClient,
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


@tool(approval_mode="never_require")
def echo_tool(x: str) -> str:
    """测试用的占位工具：原样返回输入。"""
    return f"echo:{x}"


def _settings(**overrides: object) -> AgentRuntimeSettings:
    defaults: dict[str, object] = {
        "max_steps": 30,
        "max_iterations": 30,
        "max_duration_seconds": 10.0,
        "stuck_window": 10,
    }
    defaults.update(overrides)
    return AgentRuntimeSettings(**defaults)  # type: ignore[arg-type]


def _build_agent(
    client: SupportsChatGetResponse,
    settings: AgentRuntimeSettings,
    tools: Sequence[Callable[..., Any]],
) -> Agent:
    return build_main_agent(
        client=client, instructions="test", runtime_settings=settings, tools=tools
    )


async def test_cancellation_mid_run_terminates_and_is_recorded() -> None:
    responses = [
        make_function_call_response(str(i), "echo_tool", f'{{"x": "{i}"}}') for i in range(10)
    ]
    client = NonCompliantChatClient(responses)
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])
    sink = InMemoryActionTraceSink()

    call_count = {"n": 0}

    async def is_cancelled() -> bool:
        call_count["n"] += 1
        return call_count["n"] > 2  # 前两次放行，第三次判定已取消

    result = await run_main_agent(
        agent, "go", runtime_settings=settings, sink=sink, is_cancelled=is_cancelled
    )

    assert result.termination_reason == TerminationReason.CANCELED
    assert result.working_memory.step_count == 3  # 2 次放行 + 1 次被取消拦截
    assert [entry.status for entry in sink.entries] == ["ok", "ok", "terminated"]


async def test_cancellation_requested_before_first_call_terminates_immediately() -> None:
    responses = [
        make_function_call_response(str(i), "echo_tool", f'{{"x": "{i}"}}') for i in range(10)
    ]
    client = NonCompliantChatClient(responses)
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])
    sink = InMemoryActionTraceSink()

    async def is_cancelled() -> bool:
        return True

    result = await run_main_agent(
        agent, "go", runtime_settings=settings, sink=sink, is_cancelled=is_cancelled
    )

    assert result.termination_reason == TerminationReason.CANCELED
    assert result.working_memory.step_count == 1
    assert [entry.status for entry in sink.entries] == ["terminated"]


async def test_is_cancelled_always_false_does_not_affect_normal_completion() -> None:
    responses = [
        make_function_call_response("1", "echo_tool", '{"x": "a"}'),
    ]
    client = ScriptedChatClient([*responses, make_text_response("完成")])
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])
    sink = InMemoryActionTraceSink()

    async def is_cancelled() -> bool:
        return False

    result = await run_main_agent(
        agent, "go", runtime_settings=settings, sink=sink, is_cancelled=is_cancelled
    )

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "完成"


async def test_without_is_cancelled_callback_no_cancellation_middleware_is_active() -> None:
    """未传 is_cancelled 时（如调试端点场景），循环应正常完成，不受取消检查影响。"""
    client = ScriptedChatClient(
        [make_function_call_response("1", "echo_tool", '{"x": "a"}'), make_text_response("完成")]
    )
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.COMPLETED
