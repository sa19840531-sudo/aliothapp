"""测试 Tool 权限校验中间件：越权调用被软阻断（不执行真实函数体，但循环继续）。"""

from services.api.app.agents.runtime.agent_factory import build_main_agent, run_main_agent
from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.agents.tools.permissions import PermissionEnforcementMiddleware
from services.api.app.agents.tools.registry import PermissionLevel, ToolRegistry, tool
from services.api.app.core.config import AgentRuntimeSettings
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        max_steps=30, max_iterations=30, max_duration_seconds=10.0, stuck_window=3
    )


async def test_sensitive_tool_is_soft_blocked_and_never_actually_invoked() -> None:
    registry = ToolRegistry()
    sensitive_call_count = 0

    @tool(
        name="sensitive_tool",
        description="高风险占位 Tool",
        category="测试",
        permission_level=PermissionLevel.SENSITIVE,
        registry=registry,
    )
    def sensitive_tool() -> str:
        nonlocal sensitive_call_count
        sensitive_call_count += 1
        return "不应该被真正执行到这里"

    @tool(name="safe_tool", description="安全占位 Tool", category="测试", registry=registry)
    def safe_tool(x: str) -> str:
        return f"safe:{x}"

    responses = [
        make_function_call_response("1", "sensitive_tool", "{}"),
        make_function_call_response("2", "safe_tool", '{"x": "ok"}'),
        make_text_response("完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=registry.list_tools(),
    )
    permission_middleware = PermissionEnforcementMiddleware(
        allowed_permission_levels=(PermissionLevel.SAFE,), registry=registry
    )

    result = await run_main_agent(
        agent, "go", runtime_settings=settings, extra_middleware=[permission_middleware]
    )

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "完成"
    assert sensitive_call_count == 0, "被权限中间件拦截的 Tool 不应该执行真实函数体"


async def test_allowed_permission_level_lets_call_through() -> None:
    registry = ToolRegistry()

    @tool(name="safe_tool", description="安全占位 Tool", category="测试", registry=registry)
    def safe_tool(x: str) -> str:
        return f"safe:{x}"

    responses = [
        make_function_call_response("1", "safe_tool", '{"x": "ok"}'),
        make_text_response("完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=registry.list_tools(),
    )
    permission_middleware = PermissionEnforcementMiddleware(
        allowed_permission_levels=(PermissionLevel.SAFE,), registry=registry
    )

    result = await run_main_agent(
        agent, "go", runtime_settings=settings, extra_middleware=[permission_middleware]
    )

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.working_memory.step_count == 1
