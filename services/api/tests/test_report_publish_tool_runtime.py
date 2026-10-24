"""report_publish_tool 运行时集成测试：验证发布门禁链路端到端闭环。"""

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from agent_framework import Agent, SupportsChatGetResponse, tool

from services.api.app.agents.runtime.agent_factory import build_main_agent, run_main_agent
from services.api.app.agents.runtime.business_middlewares import base as middlewares_base
from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.tools.report_tools import report_publish_tool
from services.api.app.core.config import AgentRuntimeSettings
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


@pytest.fixture(autouse=True)
def bypass_inspection_work_item_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """本套件验证发布门禁链路；工作项门禁另有专门测试，且单测环境无 PostgreSQL。"""
    from services.api.app.agents.runtime.business_middlewares.inspection_work_item_middleware import (  # noqa: E501
        InspectionWorkItemMiddleware,
    )

    async def _passthrough(self, context, call_next) -> None:
        del self
        await call_next()

    monkeypatch.setattr(InspectionWorkItemMiddleware, "process", _passthrough)


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        max_steps=30, max_iterations=30, max_duration_seconds=10.0, stuck_window=3
    )


def _build_agent(
    client: SupportsChatGetResponse,
    settings: AgentRuntimeSettings,
    tools: Sequence[Callable[..., Any]],
) -> Agent:
    return build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=tools,
    )


@tool(name="record_rule_execution_tool", description="测试占位记录工具")
def record_rule_execution_tool() -> str:
    """测试用占位工具：模拟规则执行记录已成功写入。"""
    return "recorded"


async def test_publish_tool_is_soft_blocked_before_required_record(monkeypatch) -> None:
    async def _fake_snapshot(_self, task_id: str) -> TaskRuntimeSnapshot | None:
        return TaskRuntimeSnapshot(task_id=task_id, status="running", ruleset_version=1)

    monkeypatch.setattr(
        middlewares_base.DefaultTaskRuntimeStateProvider,
        "get_task_snapshot",
        _fake_snapshot,
    )

    responses = [
        make_function_call_response(
            "1",
            "report_publish_tool",
            '{"publish_channel": "workstation"}',
        ),
        make_text_response("done"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(client, settings, [report_publish_tool])

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await run_main_agent(agent, "go", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.INCOMPLETE
    assert result.final_text == "done"
    second_call_contents = [
        content
        for message in client.received_messages[1]
        for content in message.contents
        if content.type == "function_result"
    ]
    assert second_call_contents, "应返回函数执行结果给模型。"
    result_text = str(second_call_contents[0].result)
    assert '"execution_status": "blocked"' in result_text
    assert "REPORT_PUBLISH_CONDITION_UNMET" in result_text
    assert result.trace_sink.entries[0].status == "denied"


async def test_publish_tool_is_allowed_after_required_record(monkeypatch) -> None:
    async def _fake_snapshot(_self, task_id: str) -> TaskRuntimeSnapshot | None:
        return TaskRuntimeSnapshot(task_id=task_id, status="running", ruleset_version=1)

    monkeypatch.setattr(
        middlewares_base.DefaultTaskRuntimeStateProvider,
        "get_task_snapshot",
        _fake_snapshot,
    )

    responses = [
        make_function_call_response("1", "record_rule_execution_tool", "{}"),
        make_function_call_response(
            "2",
            "report_publish_tool",
            '{"publish_channel": "workstation", "report_version": "v1"}',
        ),
        make_text_response("done"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(client, settings, [record_rule_execution_tool, report_publish_tool])

    with bind_agent_run_context(AgentRunContext(task_id="task-2", agent_run_id="run-2")):
        result = await run_main_agent(agent, "go", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.INCOMPLETE
    assert result.final_text == "done"
    function_result_texts = [
        str(content.result)
        for messages in client.received_messages
        for message in messages
        for content in message.contents
        if content.type == "function_result"
    ]
    assert function_result_texts, "应返回函数执行结果给模型。"
    assert any("placeholder_only" in text for text in function_result_texts)
