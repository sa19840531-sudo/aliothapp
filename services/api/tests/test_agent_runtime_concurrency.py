"""agents/runtime 并发安全性测试：验证多个 Run 并发执行时互不污染状态。

``run_main_agent`` 按设计每次调用都会新建一份 ``WorkingMemory``/护栏中间件实例
（详见 agent_factory.py 与 working_memory.py 模块文档），本测试把这一设计意图
转化为可回归验证的断言：并发跑多个 Run（各自独立的 Agent/ChatClient/Tool 调用
序列），断言每个 Run 的步数、留痕内容、最终文本都只反映"自己"的执行过程，不会
出现相互干扰、错位或计数累加。

范围说明：受限于假 ChatClient（``ScriptedChatClient``）内部用简单序号索引预设
响应、并非为"同一个客户端实例被多个并发 Run 共享"而设计，本测试让每个并发 Run
使用各自独立的 Agent + ChatClient 实例，重点验证的是 ``run_main_agent`` 编排层
（WorkingMemory/ActionTraceSink/护栏中间件）在并发场景下的隔离性，而不是断言
"同一个 Agent/ChatClient 对象能被多个并发请求安全共享"（真实生产环境使用的
``OpenAIChatClient``/``AsyncOpenAI`` 本身即为无状态、可并发调用的 HTTP 客户端，
不存在此假设失效的风险）。
"""

import asyncio

import pytest
from agent_framework import ChatResponse, Content, Message, tool

from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.agent_factory import build_main_agent, run_main_agent
from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.core.config import AgentRuntimeSettings
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


def _settings(**overrides: object) -> AgentRuntimeSettings:
    values: dict[str, object] = {
        "max_steps": 30,
        "max_iterations": 30,
        "max_duration_seconds": 10.0,
        "stuck_window": 3,
    }
    values.update(overrides)
    return AgentRuntimeSettings(**values)  # type: ignore[arg-type]


def test_gateway_single_tool_dispatch_avoids_framework_function_budget() -> None:
    client = ScriptedChatClient([make_text_response("未执行")])
    client.SUPERVISED_SINGLE_TOOL_DISPATCH = True

    build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=_settings(),
        tools=[echo_tool],
    )

    assert "max_function_calls" not in client.function_invocation_configuration


@tool(approval_mode="never_require")
def echo_tool(x: str) -> str:
    """测试用的占位工具：原样返回输入。"""
    return f"echo:{x}"


async def _run_one(run_index: int) -> tuple[int, object]:
    """构建一套独立的 Agent + 假 ChatClient，跑一次带唯一标识的 Run。"""
    responses = [
        make_function_call_response("1", "echo_tool", f'{{"x": "run-{run_index}"}}'),
        make_text_response(f"完成-{run_index}"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = build_main_agent(
        client=client, instructions="test", runtime_settings=settings, tools=[echo_tool]
    )
    sink = InMemoryActionTraceSink()
    result = await run_main_agent(agent, f"go-{run_index}", runtime_settings=settings, sink=sink)
    return run_index, result


async def test_concurrent_runs_do_not_cross_contaminate_state() -> None:
    run_count = 20
    results = await asyncio.gather(*(_run_one(i) for i in range(run_count)))

    assert len(results) == run_count
    for run_index, result in results:
        assert result.termination_reason == TerminationReason.COMPLETED
        assert result.final_text == f"完成-{run_index}"
        assert result.working_memory.step_count == 1
        assert len(result.trace_sink.entries) == 1
        entry = result.trace_sink.entries[0]
        assert entry.tool_name == "echo_tool"
        assert f"run-{run_index}" in entry.arguments


async def test_concurrent_runs_sharing_the_same_underlying_registry_are_isolated() -> None:
    """多个并发 Run 共用同一个默认 Tool 注册表（default_registry 是进程级单例）时，
    读取（list_tools/get）仍应各自独立、互不影响——本测试只验证并发读取不出错/不串号，
    不涉及并发写入（注册新 Tool 目前只在应用启动时的模块导入阶段发生，不在请求路径上）。
    """
    from services.api.app.agents.tools.registry import default_registry

    async def _read_tools() -> list[str]:
        await asyncio.sleep(0)  # 让出事件循环，增加交错执行的可能性
        return sorted(tool.name for tool in default_registry.list_tools())

    results = await asyncio.gather(*(_read_tools() for _ in range(20)))
    assert all(result == results[0] for result in results)


async def test_parallel_tool_calls_receive_unique_trace_steps() -> None:
    @tool(approval_mode="never_require")
    async def parallel_tool(value: str) -> str:
        """测试同一轮并发工具调用的动作序号。"""
        await asyncio.sleep(0.01)
        return value

    parallel_response = ChatResponse(
        messages=Message(
            role="assistant",
            contents=[
                Content.from_function_call(
                    call_id="parallel-1", name="parallel_tool", arguments='{"value":"a"}'
                ),
                Content.from_function_call(
                    call_id="parallel-2", name="parallel_tool", arguments='{"value":"b"}'
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
        tools=[parallel_tool],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "并发执行", runtime_settings=settings, sink=sink)

    assert result.working_memory.step_count == 2
    assert sorted(entry.step for entry in sink.entries) == [1, 2]
    assert {entry.tool_name for entry in sink.entries} == {"parallel_tool"}


@pytest.mark.parametrize(("sequential_count", "parallel_count"), [(29, 4), (0, 33)])
async def test_parallel_batch_cannot_exceed_remaining_step_budget(
    sequential_count: int, parallel_count: int
) -> None:
    executed_values: list[str] = []

    @tool(approval_mode="never_require")
    async def budget_tool(value: str) -> str:
        """记录真正进入工具函数的调用，验证并行批次的硬上限。"""
        executed_values.append(value)
        await asyncio.sleep(0.01)
        return value

    sequential_responses = [
        make_function_call_response(str(i), "budget_tool", f'{{"value":"seq-{i}"}}')
        for i in range(sequential_count)
    ]
    parallel_response = ChatResponse(
        messages=Message(
            role="assistant",
            contents=[
                Content.from_function_call(
                    call_id=f"parallel-{i}",
                    name="budget_tool",
                    arguments=f'{{"value":"parallel-{i}"}}',
                )
                for i in range(parallel_count)
            ],
        )
    )
    client = ScriptedChatClient(
        [*sequential_responses, parallel_response, make_text_response("完成")]
    )
    client.function_invocation_configuration["max_function_calls"] = 1
    settings = _settings(max_iterations=40)
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=[budget_tool],
    )
    assert client.function_invocation_configuration["max_iterations"] == 40
    assert "max_function_calls" not in client.function_invocation_configuration
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "并行临界预算", runtime_settings=settings, sink=sink)

    entries = sorted(sink.entries, key=lambda entry: entry.step)
    assert result.termination_reason == TerminationReason.MAX_STEPS_REACHED
    assert result.working_memory.step_count == 30
    assert result.working_memory.action_sequence == 33
    assert len(executed_values) == 30
    assert [entry.status for entry in entries[:30]] == ["ok"] * 30
    assert [entry.status for entry in entries[30:]] == ["terminated"] * 3


async def test_parallel_repeated_calls_trigger_stuck_guard_and_latch_termination() -> None:
    executed_values: list[str] = []

    @tool(approval_mode="never_require")
    async def repeat_tool(value: str) -> str:
        """记录并行重复调用，验证卡死检测在执行前登记签名。"""
        executed_values.append(value)
        await asyncio.sleep(0.01)
        return value

    parallel_response = ChatResponse(
        messages=Message(
            role="assistant",
            contents=[
                *[
                    Content.from_function_call(
                        call_id=f"repeat-{i}",
                        name="repeat_tool",
                        arguments='{"value":"same"}',
                    )
                    for i in range(4)
                ],
                Content.from_function_call(
                    call_id="after-stuck",
                    name="repeat_tool",
                    arguments='{"value":"different"}',
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
        tools=[repeat_tool],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "并行卡死检测", runtime_settings=settings, sink=sink)

    entries = sorted(sink.entries, key=lambda entry: entry.step)
    assert result.termination_reason == TerminationReason.STUCK_DETECTED
    assert result.working_memory.step_count == 3
    assert result.working_memory.action_sequence == 5
    assert executed_values == ["same", "same", "same"]
    assert [entry.status for entry in entries] == [
        "ok",
        "ok",
        "ok",
        "terminated",
        "terminated",
    ]
