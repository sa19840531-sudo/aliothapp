"""agents/runtime 重试能力测试：验证 RetryMiddleware 在真实 Agent 循环中的行为。

沿用 test_agent_runtime_integration.py 的"真实 Agent + 假 ChatClient"集成测试方式，
理由同上：中间件的 process(context, call_next) 签名深度依赖框架内部 Context 结构。
"""

import time

from agent_framework import ChatResponse, Content, Message, tool

from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.agent_factory import build_main_agent, run_main_agent
from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.agents.runtime.retry import RetryConfig
from services.api.app.core.config import AgentRuntimeSettings
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


def _settings(**overrides: object) -> AgentRuntimeSettings:
    defaults: dict[str, object] = {
        "max_steps": 30,
        "max_iterations": 30,
        "max_duration_seconds": 10.0,
        "stuck_window": 3,
        "retry_max_attempts": 3,
        "retry_backoff_seconds": 0.0,
    }
    defaults.update(overrides)
    return AgentRuntimeSettings(**defaults)  # type: ignore[arg-type]


async def test_transient_failure_is_retried_and_eventually_succeeds() -> None:
    attempts = {"count": 0}

    @tool(approval_mode="never_require")
    def flaky_tool() -> str:
        """测试用占位工具：前两次抛出可重试异常，第三次成功。"""
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError(f"transient failure #{attempts['count']}")
        return "ok-on-3rd-try"

    responses = [
        make_function_call_response("1", "flaky_tool", "{}"),
        make_text_response("完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = build_main_agent(
        client=client, instructions="test", runtime_settings=settings, tools=[flaky_tool]
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert attempts["count"] == 3
    assert result.working_memory.step_count == 1  # 重试对 guard/trace 的步数计数透明
    assert sink.entries[0].status == "ok"
    assert sink.entries[0].retry_count == 2


async def test_retries_are_exhausted_and_recorded_as_error() -> None:
    @tool(approval_mode="never_require")
    def always_failing_tool() -> str:
        """测试用占位工具：总是抛出可重试异常，验证重试耗尽后的行为。"""
        raise ConnectionError("always fails")

    responses = [
        make_function_call_response("1", "always_failing_tool", "{}"),
        make_text_response("done-after-retries-exhausted"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings(retry_max_attempts=2)
    agent = build_main_agent(
        client=client, instructions="test", runtime_settings=settings, tools=[always_failing_tool]
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert sink.entries[0].status == "error"
    assert sink.entries[0].retry_count == 2
    assert "always fails" in sink.entries[0].error_message


async def test_non_retryable_exception_is_not_retried() -> None:
    attempts = {"count": 0}

    @tool(approval_mode="never_require")
    def non_retryable_failing_tool() -> str:
        """测试用占位工具：抛出不在可重试清单内的异常类型，应立即失败不重试。"""
        attempts["count"] += 1
        raise ValueError("not retryable")

    responses = [
        make_function_call_response("1", "non_retryable_failing_tool", "{}"),
        make_text_response("done"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=[non_retryable_failing_tool],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert attempts["count"] == 1
    assert sink.entries[0].status == "error"
    assert sink.entries[0].retry_count == 0


async def test_custom_retry_config_overrides_settings_derived_default() -> None:
    attempts = {"count": 0}

    @tool(approval_mode="never_require")
    def flaky_tool() -> str:
        """测试用占位工具：前四次失败，第五次成功；用于验证自定义 RetryConfig 生效。"""
        attempts["count"] += 1
        if attempts["count"] < 5:
            raise ConnectionError("still flaky")
        return "ok-on-5th-try"

    responses = [
        make_function_call_response("1", "flaky_tool", "{}"),
        make_text_response("完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings(retry_max_attempts=2)  # 若不传自定义配置，2 次尝试不足以成功
    agent = build_main_agent(
        client=client, instructions="test", runtime_settings=settings, tools=[flaky_tool]
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(
        agent,
        "go",
        runtime_settings=settings,
        sink=sink,
        retry_config=RetryConfig(max_attempts=5, backoff_seconds=0.0),
    )

    assert result.termination_reason == TerminationReason.COMPLETED
    assert attempts["count"] == 5
    assert sink.entries[0].status == "ok"
    assert sink.entries[0].retry_count == 4


async def test_parallel_calls_keep_retry_counts_in_their_own_context() -> None:
    attempts = {"retry": 0}

    @tool(approval_mode="never_require")
    def parallel_retry_tool(value: str) -> str:
        """一个调用失败一次，另一个调用正常完成，用于验证重试计数隔离。"""
        if value == "retry":
            attempts["retry"] += 1
            if attempts["retry"] == 1:
                raise ConnectionError("retry once")
            return value
        time.sleep(0.01)
        return value

    parallel_response = ChatResponse(
        messages=Message(
            role="assistant",
            contents=[
                Content.from_function_call(
                    call_id="retry",
                    name="parallel_retry_tool",
                    arguments='{"value":"retry"}',
                ),
                Content.from_function_call(
                    call_id="plain",
                    name="parallel_retry_tool",
                    arguments='{"value":"plain"}',
                ),
            ],
        )
    )
    client = ScriptedChatClient([parallel_response, make_text_response("完成")])
    settings = _settings()
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=[parallel_retry_tool],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(
        agent,
        "并行重试",
        runtime_settings=settings,
        sink=sink,
        retry_config=RetryConfig(max_attempts=2, backoff_seconds=0.05),
    )

    retry_counts = {entry.arguments: entry.retry_count for entry in sink.entries}
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.working_memory.step_count == 2
    assert attempts["retry"] == 2
    assert retry_counts['{"value": "retry"}'] == 1
    assert retry_counts['{"value": "plain"}'] == 0
