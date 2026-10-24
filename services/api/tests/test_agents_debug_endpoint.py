"""测试 Agent 调试接口：HTTP 请求校验、响应结构、以及通过 monkeypatch 注入假 Agent
避免真实网络调用。
"""

from agent_framework import tool
from fastapi.testclient import TestClient

from services.api.app.agents.runtime.agent_factory import build_main_agent
from services.api.app.api.v1 import agents as agents_module
from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1.auth import AuthUserResponse
from services.api.app.core.config import AgentRuntimeSettings
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


def test_agent_debug_run_requires_login() -> None:
    app = create_app()
    pop_test_auth(app)
    client = TestClient(app)

    response = client.post("/api/v1/agents/debug-run", json={"message": "hello"})

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "缺少登录令牌"


def test_agent_debug_run_requires_agent_permission() -> None:
    app = create_app()
    pop_test_auth(app)
    app.dependency_overrides[auth_module.require_auth_user] = lambda: AuthUserResponse(
        id="user-1",
        username="rule.editor",
        full_name="规则编辑",
        role="规则配置员",
        status="enabled",
        permissions=["dashboard", "qcRules", "configCenter"],
    )
    client = TestClient(app)

    response = client.post("/api/v1/agents/debug-run", json={"message": "hello"})

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "无权限执行 Agent 运行"


def test_debug_run_rejects_empty_message() -> None:
    client = TestClient(create_app())

    response = client.post("/api/v1/agents/debug-run", json={"message": ""})

    assert response.status_code == 422


def test_debug_run_returns_structured_result_with_fake_agent(monkeypatch) -> None:
    @tool(approval_mode="never_require")
    def get_current_time() -> str:
        """测试调试端点的本地 Tool，不进入生产 Capability Manifest。"""
        return "2026-07-17T00:00:00Z"

    responses = [
        make_function_call_response("1", "get_current_time", "{}"),
        make_text_response("现在是测试时间"),
    ]
    fake_client = ScriptedChatClient(responses)
    settings = AgentRuntimeSettings(
        max_steps=30, max_iterations=30, max_duration_seconds=10.0, stuck_window=3
    )
    fake_agent = build_main_agent(
        client=fake_client,
        instructions="test",
        runtime_settings=settings,
        tools=[get_current_time],
    )
    monkeypatch.setattr(agents_module, "build_default_main_agent", lambda: fake_agent)

    client = TestClient(create_app())
    response = client.post("/api/v1/agents/debug-run", json={"message": "现在几点了？"})

    assert response.status_code == 200
    body = response.json()
    assert body["termination_reason"] == "completed"
    assert body["final_text"] == "现在是测试时间"
    assert body["step_count"] == 1
    assert len(body["action_trace"]) == 1
    assert body["action_trace"][0]["tool_name"] == "get_current_time"
    assert body["action_trace"][0]["status"] == "ok"
    assert body["working_memory"]["schema_version"] == 2
    assert body["working_memory"]["current_page"] is None
