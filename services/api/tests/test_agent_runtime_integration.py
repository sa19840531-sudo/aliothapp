"""agents/runtime 集成测试：验证 ActionTraceMiddleware + LoopGuardMiddleware +
agent_factory 在真实 Agent 循环中协同工作的行为。

不发起真实网络调用，改用 tests/support 下的假 ChatClient；采用"真实 Agent + 假
ChatClient"的集成测试方式而非对中间件做隔离单测，是因为这些中间件的
``process(context, call_next)`` 签名深度依赖 Agent Framework 内部的
``FunctionInvocationContext`` 结构，手工构造该对象既脆弱又无法反映真实行为；通过
公开的 ``Agent.run()`` API 驱动更贴近真实使用场景，也更不容易产生"测试通过但实际
不工作"的假阳性（本仓库此前正是靠这种方式发现了 max_function_calls 只是软上限）。
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import Agent, AgentSession, FunctionMiddleware, SupportsChatGetResponse, tool
from agent_framework.exceptions import ChatClientException

from services.api.app.agents.runtime import agent_factory as agent_factory_module
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.agent_factory import build_main_agent, run_main_agent
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    TerminationReason,
)
from services.api.app.agents.runtime.retry import RetryConfig
from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.skills.loader import (
    AliothSkillsProvider,
    build_skills_context_provider,
)
from services.api.app.core.config import AgentRuntimeSettings
from services.api.app.infrastructure.llm_gateway.client_factory import (
    RequestCountingChatClientMixin,
)
from services.api.tests.support.fake_chat_client import (
    HangingChatClient,
    NonCompliantChatClient,
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


@pytest.fixture(autouse=True)
def bypass_inspection_work_item_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """本套件验证 Agent 循环行为；工作项门禁另有专门测试，且单测环境无 PostgreSQL。"""
    from services.api.app.agents.runtime.business_middlewares.inspection_work_item_middleware import (  # noqa: E501
        InspectionWorkItemMiddleware,
    )

    async def _passthrough(self, context, call_next) -> None:
        del self
        await call_next()

    monkeypatch.setattr(InspectionWorkItemMiddleware, "process", _passthrough)


@tool(approval_mode="never_require")
def echo_tool(x: str) -> str:
    """测试用的占位工具：原样返回输入。"""
    return f"echo:{x}"


@tool(name="page_capture_tool", approval_mode="never_require")
def fake_page_capture_tool(mode: str) -> dict:
    """返回测试页面采集。"""
    if mode == "observe":
        return {
            "execution_status": "success",
            "page_capture_id": "capture-2",
            "final_url": "https://example.com/settings",
            "title": "设置",
            "page_seq": 2,
            "artifact_ids": ["artifact-2"],
        }
    return {
        "execution_status": "success",
        "page_capture_id": "capture-1",
        "final_url": "https://example.com/home",
        "title": "首页",
        "page_seq": 1,
        "artifact_ids": ["artifact-1"],
    }


@tool(name="candidate_rules_tool", approval_mode="never_require")
def fake_candidate_rules_tool(
    page_type: str,
    page_capture_id: str,
    page_state: str | None = None,
) -> dict:
    """返回测试候选规则。"""
    del page_state
    return {
        "page_type": page_type,
        "page_capture_id": page_capture_id,
        "rules": (
            [] if page_type == "settings" else [{"rule_id": "rule-1", "rule_code": "alioth_1"}]
        ),
    }


@tool(name="candidate_rules_tool", approval_mode="never_require")
def fake_multi_candidate_rules_tool(
    page_type: str,
    page_capture_id: str,
    page_state: str | None = None,
) -> dict:
    """返回两条测试候选规则。"""
    rules = [
        {"rule_id": "rule-1", "rule_code": "alioth_1"},
        {"rule_id": "rule-2", "rule_code": "alioth_2"},
    ]
    if page_state == "popup":
        rules = rules[:1]
    return {
        "page_type": page_type,
        "page_capture_id": page_capture_id,
        "rules": rules,
    }


@tool(name="candidate_rules_tool", approval_mode="never_require")
def fake_rule_engine_candidate_rules_tool(page_type: str, page_capture_id: str) -> dict:
    """返回一条具备确定性执行路由的候选规则。"""
    return {
        "page_type": page_type,
        "page_capture_id": page_capture_id,
        "rules": [
            {
                "rule_id": "rule-1",
                "rule_code": "alioth_6",
                "execution_route": {
                    "strategy": "rule_engine",
                    "rule_engine_ready": True,
                    "tool_name": "rule_judge_tool",
                },
            }
        ],
    }


@tool(name="page_detection_tool", approval_mode="never_require")
def fake_page_detection_tool(page_capture_id: str) -> dict:
    """返回测试页面识别结果。"""
    page_type = "settings" if page_capture_id == "capture-2" else "home"
    return {
        "page_capture_id": page_capture_id,
        "detection_status": "recognized",
        "page_type": page_type,
        "rule_matching_allowed": True,
    }


@tool(name="page_detection_tool", approval_mode="never_require")
def fake_page_detection_with_frontier_tool(page_capture_id: str) -> dict:
    """返回带下一页候选动作的已识别页面结果。"""
    return {
        "page_capture_id": page_capture_id,
        "detection_status": "recognized",
        "page_type": "home",
        "rule_matching_allowed": True,
        "candidate_actions": [
            {
                "action": "click",
                "target": "detail_entry",
                "risk_level": "low",
                "purpose": "cover detail page rules",
                "expected_next_page_type": "content_detail",
            }
        ],
    }


@tool(name="page_capture_tool", approval_mode="never_require")
def fake_dynamic_page_capture_tool(
    mode: str,
    exploration_action_id: str | None = None,
    navigation_depth: int | None = None,
) -> dict:
    """根据 Frontier 关联关系动态返回入口页或后续发现页。"""
    del mode, navigation_depth
    is_discovered_page = exploration_action_id is not None
    capture_id = "capture-2" if is_discovered_page else "capture-1"
    return {
        "execution_status": "success",
        "page_capture_id": capture_id,
        "final_url": f"https://example.com/{capture_id}",
        "title": "设置" if is_discovered_page else "首页",
        "page_seq": 2 if is_discovered_page else 1,
        "artifact_ids": [f"artifact-{2 if is_discovered_page else 1}"],
    }


@tool(name="page_detection_tool", approval_mode="never_require")
def fake_dynamic_page_detection_tool(page_capture_id: str) -> dict:
    """仅入口页发现一个动态后续页面，第二页不再扩展 Frontier。"""
    is_entry = page_capture_id == "capture-1"
    result = {
        "page_capture_id": page_capture_id,
        "detection_status": "recognized",
        "page_type": "home" if is_entry else "settings",
        "rule_matching_allowed": True,
    }
    if is_entry:
        result["candidate_actions"] = [
            {
                "action": "click",
                "target": "settings_entry",
                "risk_level": "low",
                "purpose": "探索设置页",
                "expected_next_page_type": "settings",
            }
        ]
    return result


@tool(name="page_capture_tool", approval_mode="never_require")
def fake_multi_branch_page_capture_tool(
    mode: str,
    exploration_action_id: str | None = None,
    navigation_depth: int | None = None,
    trigger_action: str | None = None,
    selector: str | None = None,
) -> dict:
    """模拟三个动态导航分支，其中第一个分支采集失败。"""
    del mode, navigation_depth, trigger_action, selector
    if exploration_action_id == "exploration-2":
        return {
            "execution_status": "error",
            "handled": True,
            "failure_scope": "page",
            "failure_stage": "page_capture",
            "error_code": "BRANCH_CAPTURE_FAILED",
            "error_message": "第一个业务导航分支采集失败",
            "retryable": False,
        }
    capture_ids = {
        None: "capture-1",
        "exploration-3": "capture-2",
        "exploration-4": "capture-3",
    }
    capture_id = capture_ids[exploration_action_id]
    sequence = int(capture_id.rsplit("-", 1)[1])
    return {
        "execution_status": "success",
        "page_capture_id": capture_id,
        "final_url": f"https://example.com/{capture_id}",
        "title": f"页面 {sequence}",
        "page_seq": sequence,
        "artifact_ids": [f"artifact-{sequence}"],
    }


@tool(name="page_detection_tool", approval_mode="never_require")
def fake_multi_branch_page_detection_tool(page_capture_id: str) -> dict:
    """首页动态发现三个业务导航，后续业务页不再扩展。"""
    result: dict[str, Any] = {
        "page_capture_id": page_capture_id,
        "detection_status": "recognized",
        "page_type": "home" if page_capture_id == "capture-1" else "content_list",
        "rule_matching_allowed": True,
    }
    if page_capture_id == "capture-1":
        result["candidate_actions"] = [
            {
                "action": "click",
                "target": f"business_tab_{index}",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": f"探索业务导航 {index}",
                "expected_next_page_type": "content_list",
                "navigation_depth": 1,
            }
            for index in range(1, 4)
        ]
    return result


@tool(name="page_capture_tool", approval_mode="never_require")
def fake_nested_branch_page_capture_tool(
    mode: str,
    exploration_action_id: str | None = None,
    navigation_depth: int | None = None,
    trigger_action: str | None = None,
    selector: str | None = None,
) -> dict:
    """模拟首页、列表、详情、返回列表和另一业务分支。"""
    del mode, navigation_depth, trigger_action, selector
    capture_ids = {
        None: "capture-1",
        "exploration-2": "capture-2",
        "exploration-4": "capture-3",
        "exploration-5": "capture-4",
        "exploration-3": "capture-5",
    }
    capture_id = capture_ids[exploration_action_id]
    sequence = int(capture_id.rsplit("-", 1)[1])
    return {
        "execution_status": "success",
        "page_capture_id": capture_id,
        "final_url": f"https://example.com/{capture_id}",
        "title": f"嵌套页面 {sequence}",
        "page_seq": sequence,
        "artifact_ids": [f"artifact-{sequence}"],
    }


@tool(name="page_detection_tool", approval_mode="never_require")
def fake_nested_branch_page_detection_tool(page_capture_id: str) -> dict:
    """按采集序列动态发现底部导航、详情入口和返回动作。"""
    page_types = {
        "capture-1": "home",
        "capture-2": "content_list",
        "capture-3": "content_detail",
        "capture-4": "content_list",
        "capture-5": "settings",
    }
    result: dict[str, Any] = {
        "page_capture_id": page_capture_id,
        "detection_status": "recognized",
        "page_type": page_types[page_capture_id],
        "rule_matching_allowed": True,
    }
    if page_capture_id == "capture-1":
        result["candidate_actions"] = [
            {
                "action": "click",
                "target": f"business_tab_{index}",
                "target_keywords": ["bottom_navigation"],
                "risk_level": "low",
                "purpose": "探索稳定页面中可见的底部导航业务分支",
                "expected_next_page_type": "content_list",
                "navigation_depth": 1,
            }
            for index in range(1, 3)
        ]
    elif page_capture_id in {"capture-2", "capture-4"}:
        result["candidate_actions"] = [
            {
                "action": "click",
                "target": ".masonry-grid:visible .data-item:visible",
                "target_keywords": ["nested_content", "bottom_tab:2"],
                "risk_level": "low",
                "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
                "expected_next_page_type": "content_detail",
                "navigation_depth": 2,
            }
        ]
    elif page_capture_id == "capture-3":
        result["candidate_actions"] = [
            {
                "action": "back",
                "target": "browser_history",
                "target_keywords": ["nested_return"],
                "risk_level": "low",
                "purpose": "详情或播放页质检完成后返回来源列表继续探索",
                "navigation_depth": 1,
            }
        ]
    return result


@tool(name="page_capture_tool", approval_mode="never_require")
def fake_product_lifecycle_page_capture_tool(
    mode: str,
    exploration_action_id: str | None = None,
    supersedes_page_capture_id: str | None = None,
    trigger_action: str | None = None,
    selector: str | None = None,
) -> dict:
    """模拟线路加载、开屏广告、首页弹窗和完整首页的连续采集。"""
    del mode, trigger_action, selector
    capture_ids = {
        None: "capture-1",
        "exploration-2": "capture-2",
        "exploration-3": "capture-3",
        "exploration-4": "capture-4",
    }
    capture_id = capture_ids[exploration_action_id]
    sequence = int(capture_id.rsplit("-", 1)[1])
    return {
        "execution_status": "success",
        "page_capture_id": capture_id,
        "final_url": "https://example.com/product",
        "title": "测试产品",
        "page_seq": sequence,
        "artifact_ids": [f"artifact-{sequence}"],
        "supersedes_page_capture_id": supersedes_page_capture_id,
    }


@tool(name="page_detection_tool", approval_mode="never_require")
def fake_product_lifecycle_page_detection_tool(page_capture_id: str) -> dict:
    """按真实产品入口时序返回页面类型和单层弹窗关闭动作。"""
    if page_capture_id == "capture-1":
        return {
            "page_capture_id": page_capture_id,
            "detection_status": "recognized",
            "page_type": "line_loading",
            "page_state": "loading",
            "rule_matching_allowed": False,
        }
    if page_capture_id == "capture-2":
        return {
            "page_capture_id": page_capture_id,
            "detection_status": "recognized",
            "page_type": "startup_ad",
            "page_state": "countdown",
            "rule_matching_allowed": True,
        }
    if page_capture_id == "capture-3":
        return {
            "page_capture_id": page_capture_id,
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "popup",
            "rule_matching_allowed": True,
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "text=关闭",
                    "target_keywords": ["关闭"],
                    "risk_level": "low",
                    "purpose": "关闭当前首页弹窗",
                }
            ],
        }
    return {
        "page_capture_id": page_capture_id,
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "rule_matching_allowed": True,
    }


@tool(name="exploration_closure_tool", approval_mode="never_require")
def fake_exploration_closure_tool(
    reason: str,
    action_status: str = "skipped",
    close_all_pending_actions: bool = False,
    abandon_all_pending_pages: bool = False,
) -> dict:
    """返回测试探索收口请求。"""
    return {
        "execution_status": "accepted",
        "reason": reason,
        "action_status": action_status,
        "close_all_pending_actions": close_all_pending_actions,
        "abandon_all_pending_pages": abandon_all_pending_pages,
    }


class WorkingMemoryBindingMiddleware(FunctionMiddleware):
    """记录 Agent 工厂注入的 Run 级工作记忆。"""

    def __init__(self) -> None:
        self.working_memory = None

    def bind_working_memory(self, working_memory) -> None:
        self.working_memory = working_memory

    async def process(self, context, call_next) -> None:
        await call_next()


@tool(name="page_capture_tool", approval_mode="never_require")
def fake_sequential_page_capture_tool(
    mode: str, supersedes_page_capture_id: str | None = None
) -> dict:
    """按调用次数返回不同采集 ID，模拟补采。"""
    calls = getattr(fake_sequential_page_capture_tool, "calls", 0) + 1
    fake_sequential_page_capture_tool.calls = calls
    capture_id = f"capture-{calls}"
    return {
        "execution_status": "success",
        "page_capture_id": capture_id,
        "final_url": f"https://example.com/{capture_id}",
        "title": "测试页面",
        "page_seq": calls,
        "artifact_ids": [f"artifact-{calls}"],
        "supersedes_page_capture_id": supersedes_page_capture_id,
    }


@tool(name="page_detection_tool", approval_mode="never_require")
def fake_supplemental_page_detection_tool(page_capture_id: str) -> dict:
    """首次识别证据不足，补采后识别成功。"""
    if page_capture_id == "capture-1":
        return {
            "page_capture_id": page_capture_id,
            "detection_status": "insufficient",
            "page_type": None,
            "rule_matching_allowed": False,
        }
    return {
        "page_capture_id": page_capture_id,
        "detection_status": "recognized",
        "page_type": "home",
        "rule_matching_allowed": True,
    }


@tool(name="record_rule_execution_tool", approval_mode="never_require")
def fake_record_rule_execution_tool(
    execution_status: str,
    rule_id: str,
    page_capture_id: str,
    evidence_artifact_ids: list[str],
) -> dict:
    """返回测试规则写回结果。"""
    return {
        "execution_id": "execution-1",
        "rule_id": rule_id,
        "rule_code": "alioth_1",
        "execution_status": execution_status,
        "page_capture_id": page_capture_id,
        "evidence_artifact_ids": evidence_artifact_ids,
    }


@tool(name="rule_judge_tool", approval_mode="never_require")
def fake_rule_judge_tool(
    rule_code: str,
    page_capture_id: str,
    evidence_artifact_ids: list[str],
    judge_input: dict[str, Any],
) -> dict:
    """返回可直接写回的确定性规则判定结果。"""
    return {
        "rule_id": "rule-1",
        "rule_code": rule_code,
        "executor_type": "rule_engine",
        "judge_strategy": "rule_engine",
        "execution_status": "pass",
        "confidence": 1.0,
        "result_summary": "确定性规则通过",
        "problem_desc": None,
        "page_capture_id": page_capture_id,
        "evidence_artifact_ids": evidence_artifact_ids,
        "judge_input": judge_input,
        "judge_output": {"assertion": {"passed": True}},
    }


@tool(name="record_rule_execution_tool", approval_mode="never_require")
def fake_handoff_record_rule_execution_tool(
    execution_status: str,
    page_capture_id: str,
    rule_id: str | None = None,
    rule_code: str | None = None,
    judge_strategy: str | None = None,
    confidence: float | None = None,
    result_summary: str | None = None,
    problem_desc: str | None = None,
    evidence_artifact_ids: list[str] | None = None,
    judge_input: dict[str, Any] | None = None,
    judge_output: dict[str, Any] | None = None,
) -> dict:
    """返回包含实际接收证据的测试写回结果。"""
    return {
        "execution_id": "execution-1",
        "rule_id": rule_id,
        "rule_code": rule_code,
        "execution_status": execution_status,
        "page_capture_id": page_capture_id,
        "evidence_artifact_ids": evidence_artifact_ids,
        "judge_strategy": judge_strategy,
        "confidence": confidence,
        "result_summary": result_summary,
        "problem_desc": problem_desc,
        "judge_input": judge_input,
        "judge_output": judge_output,
    }


@tool(name="record_rule_executions_batch_tool", approval_mode="never_require")
def fake_record_rule_executions_batch_tool(
    page_capture_id: str,
    executions: list[dict[str, Any]],
) -> dict:
    """一次返回多条测试规则写回结果。"""
    return {
        "execution_status": "success",
        "page_capture_id": page_capture_id,
        "succeeded_count": len(executions),
        "failed_count": 0,
        "items": [
            {
                "status": "ok",
                "execution_id": f"execution-{index}",
                "rule_id": item["rule_id"],
                "rule_code": item["rule_code"],
                "execution_status": item["execution_status"],
                "page_capture_id": page_capture_id,
            }
            for index, item in enumerate(executions, start=1)
        ],
    }


def _settings(**overrides: object) -> AgentRuntimeSettings:
    defaults: dict[str, object] = {
        "max_steps": 30,
        "max_iterations": 30,
        "max_duration_seconds": 10.0,
        "stuck_window": 3,
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


class ServerStoredScriptedChatClient(ScriptedChatClient):
    """模拟默认依赖服务端保存 Responses 历史的客户端。"""

    STORES_BY_DEFAULT = True


class RequestCountingScriptedChatClient(RequestCountingChatClientMixin, ScriptedChatClient):
    """使用假响应验证每次模型请求都会计入累计 usage。

    这个通用夹具默认保留 ScriptedChatClient 的完整 Tool 循环语义；
    需要模拟真实兼容端点单 Tool 派发时，测试必须显式将该标记设为 True。
    """

    SUPERVISED_SINGLE_TOOL_DISPATCH = False


async def test_normal_completion_reports_completed_and_final_text() -> None:
    responses = [
        make_function_call_response("1", "echo_tool", '{"x": "a"}'),
        make_function_call_response("2", "echo_tool", '{"x": "b"}'),
        make_text_response("任务完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "任务完成"
    assert result.working_memory.step_count == 2
    assert [entry.status for entry in sink.entries] == ["ok", "ok"]


async def test_bound_task_without_page_capture_cannot_report_completed() -> None:
    client = RequestCountingScriptedChatClient(
        [
            make_text_response("任务完成"),
            make_text_response("任务已完成"),
            make_text_response("无需继续处理"),
        ]
    )
    settings = _settings()
    agent = _build_agent(client, settings, [])

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
        )
    ):
        result = await run_main_agent(agent, "质检页面", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.INCOMPLETE
    assert result.working_memory.step_count == 0
    assert result.usage is not None
    assert result.usage.ai_calls == 3
    assert any(
        "尚未形成任何页面采集" in message.text
        for messages in client.received_messages
        for message in messages
    )


async def test_page_inspection_memory_flows_through_real_agent_tool_loop() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_executions_batch_tool",
            (
                '{"page_capture_id":"capture-1","executions":['
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"rule_code":"alioth_1",'
                '"evidence_artifact_ids":["artifact-1"]}]}'
            ),
        ),
        make_text_response("页面质检完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_executions_batch_tool,
        ],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "质检页面", runtime_settings=settings, sink=sink)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert snapshot["current_page"] is None
    assert snapshot["inspected_page_count"] == 1
    assert snapshot["inspected_pages"][0]["result"]["overall_status"] == "pass"
    assert all('"working_memory"' in (entry.result or "") for entry in sink.entries)


async def test_supervisor_forces_seeded_entry_capture_on_first_turn() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_text_response("入口页面已采集"),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_executions_batch_tool",
            (
                '{"page_capture_id":"capture-1","executions":['
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"rule_code":"alioth_1",'
                '"evidence_artifact_ids":["artifact-1"]}]}'
            ),
        ),
        make_text_response("入口页面质检完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_executions_batch_tool,
        ],
    )

    result = await run_main_agent(
        agent,
        "请执行任务。\n目标URL：https://example.com/\n规则集：test@1",
        runtime_settings=settings,
    )

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert client.received_options[0]["tool_choice"] == {
        "mode": "required",
        "required_function_name": "page_capture_tool",
    }
    first_request_text = "\n".join(
        content.text or ""
        for message in client.received_messages[0]
        for content in message.contents
        if content.type == "text"
    )
    assert "mode='goto'" in first_request_text
    assert "https://example.com/" in first_request_text
    assert snapshot["exploration_frontier"]["explored_action_count"] == 1
    assert snapshot["exploration_frontier"]["explored_actions"][0]["target"] == (
        "https://example.com/"
    )


async def test_gateway_dispatch_boundary_returns_after_each_required_tool() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_executions_batch_tool",
            (
                '{"page_capture_id":"capture-1","executions":['
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"rule_code":"alioth_1",'
                '"evidence_artifact_ids":["artifact-1"]}]}'
            ),
        ),
    ]
    client = ScriptedChatClient(responses)
    client.SUPERVISED_SINGLE_TOOL_DISPATCH = True
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_executions_batch_tool,
        ],
    )

    result = await run_main_agent(
        agent,
        "请执行任务。\n目标URL：https://example.com/\n规则集：test@1",
        runtime_settings=settings,
    )

    assert result.termination_reason == TerminationReason.COMPLETED
    choices = [item.get("tool_choice") for item in client.received_options]
    assert choices == [
        {"mode": "required", "required_function_name": tool_name}
        for tool_name in [
            "page_capture_tool",
            "page_detection_tool",
            "candidate_rules_tool",
            "record_rule_executions_batch_tool",
        ]
    ]
    assert len(client.received_options) == 4


async def test_gateway_dispatch_boundary_blocks_unexpected_tool_before_execution() -> None:
    unexpected_calls = 0

    @tool(name="unexpected_tool", approval_mode="never_require")
    def unexpected_tool() -> str:
        nonlocal unexpected_calls
        unexpected_calls += 1
        return "must-not-run"

    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response("4", "unexpected_tool", "{}"),
        make_function_call_response(
            "5",
            "record_rule_executions_batch_tool",
            (
                '{"page_capture_id":"capture-1","executions":['
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"rule_code":"alioth_1",'
                '"evidence_artifact_ids":["artifact-1"]}]}'
            ),
        ),
    ]
    client = ScriptedChatClient(responses)
    client.SUPERVISED_SINGLE_TOOL_DISPATCH = True
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_executions_batch_tool,
            unexpected_tool,
        ],
    )

    result = await run_main_agent(
        agent,
        "请执行任务。\n目标URL：https://example.com/\n规则集：test@1",
        runtime_settings=settings,
    )

    assert result.termination_reason == TerminationReason.COMPLETED
    assert unexpected_calls == 0
    assert result.working_memory.step_count == 4
    assert [item.get("tool_choice") for item in client.received_options] == [
        {"mode": "required", "required_function_name": tool_name}
        for tool_name in [
            "page_capture_tool",
            "page_detection_tool",
            "candidate_rules_tool",
            "record_rule_executions_batch_tool",
            "record_rule_executions_batch_tool",
        ]
    ]


async def test_supervisor_records_error_when_rule_route_is_not_ready() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_text_response("页面规则尚未写回"),
        make_function_call_response(
            "4",
            "record_rule_executions_batch_tool",
            (
                '{"page_capture_id":"capture-1","executions":['
                '{"execution_status":"error","rule_id":"rule-1",'
                '"rule_code":"alioth_1","evidence_artifact_ids":["artifact-1"],'
                '"error_code":"RULE_JUDGE_ROUTE_NOT_READY",'
                '"error_message":"候选规则未返回可执行 Judge 路由"}]}'
            ),
        ),
        make_text_response("页面质检完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_executions_batch_tool,
        ],
    )

    result = await run_main_agent(agent, "质检页面", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.COMPLETED
    constrained_choices = [
        item["tool_choice"]
        for item in client.received_options
        if isinstance(item.get("tool_choice"), dict)
        and item["tool_choice"].get("required_function_name") == "record_rule_executions_batch_tool"
    ]
    assert constrained_choices == [
        {
            "mode": "required",
            "required_function_name": "record_rule_executions_batch_tool",
        }
    ]
    continuation_messages = client.received_messages[4]
    continuation_content_types = {
        content.type for message in continuation_messages for content in message.contents
    }
    assert {"function_call", "function_result"} <= continuation_content_types
    continuation_text = "\n".join(
        content.text or ""
        for message in continuation_messages
        for content in message.contents
        if content.type == "text"
    )
    assert "RULE_JUDGE_ROUTE_NOT_READY" in continuation_text
    assert "不得由模型猜测结论" in continuation_text


async def test_supervisor_forces_rule_judge_for_unresolved_engine_rule() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_text_response("确定性规则尚未判定"),
        make_function_call_response(
            "4",
            "rule_judge_tool",
            (
                '{"rule_code":"alioth_6","page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"],"judge_input":{}}'
            ),
        ),
        make_text_response("确定性判定已完成，等待规则写回"),
        make_function_call_response(
            "5",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("页面质检完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_rule_engine_candidate_rules_tool,
            fake_rule_judge_tool,
            fake_handoff_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "质检页面", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.COMPLETED
    forced_choices = [
        item["tool_choice"]
        for item in client.received_options
        if isinstance(item.get("tool_choice"), dict)
        and item["tool_choice"].get("required_function_name") == "rule_judge_tool"
    ]
    assert forced_choices == [{"mode": "required", "required_function_name": "rule_judge_tool"}]
    forced_writes = [
        item["tool_choice"]
        for item in client.received_options
        if isinstance(item.get("tool_choice"), dict)
        and item["tool_choice"].get("required_function_name") == "record_rule_execution_tool"
    ]
    assert forced_writes == [
        {
            "mode": "required",
            "required_function_name": "record_rule_execution_tool",
        }
    ]


async def test_stalled_frontier_action_is_isolated_without_global_incomplete() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("page inspection finished"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_with_frontier_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect page", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["current_page"] is None
    assert snapshot["inspected_page_count"] == 1
    assert snapshot["continuation_count"] == 0
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0
    assert snapshot["exploration_frontier"]["inactive_actions"][-1]["status"] == "failed"
    assert (
        snapshot["exploration_frontier"]["inactive_actions"][-1]["failure_code"]
        == "EXPLORATION_ACTION_STALLED"
    )
    required_choices = [
        item.get("tool_choice")
        for item in client.received_options
        if isinstance(item.get("tool_choice"), dict)
    ]
    assert required_choices[-1]["required_function_name"] == "page_capture_tool"


async def test_noncompliant_noop_does_not_block_other_frontier_work() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("入口页已完成，但动态动作尚未执行"),
        make_function_call_response(
            "5",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("重复规则调用没有改变页面终态"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings(max_continuation_attempts=1)
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_with_frontier_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect page", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["continuation_count"] == 0
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0
    assert (
        snapshot["exploration_frontier"]["inactive_actions"][-1]["failure_code"]
        == "EXPLORATION_ACTION_STALLED"
    )


async def test_stalled_normal_page_rules_degrade_without_global_incomplete() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("首页仍有一条规则没有结果"),
        make_text_response("规则没有新增进展"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings(max_continuation_attempts=1)
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_multi_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect page", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert result.working_memory.termination_reason is None
    assert snapshot["terminal_page_count"] == 1
    assert snapshot["inspected_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["inspected_pages"][0]["failure"]["code"] == ("RULE_EXECUTION_BUDGET_EXHAUSTED")


async def test_unstarted_normal_page_rules_do_not_block_dynamic_frontier() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_text_response("首页候选规则尚未查询"),
        make_text_response("候选规则查询仍无进展"),
        make_function_call_response(
            "3",
            "page_capture_tool",
            (
                '{"mode":"single_trigger","exploration_action_id":"exploration-2",'
                '"navigation_depth":1}'
            ),
        ),
        make_function_call_response(
            "4",
            "page_detection_tool",
            '{"page_capture_id":"capture-2"}',
        ),
        make_function_call_response(
            "5",
            "candidate_rules_tool",
            '{"page_type":"settings","page_capture_id":"capture-2"}',
        ),
        make_text_response("动态设置页已完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings(max_continuation_attempts=1)
    agent = _build_agent(
        client,
        settings,
        [
            fake_dynamic_page_capture_tool,
            fake_dynamic_page_detection_tool,
            fake_candidate_rules_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect dynamic pages", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "RULE_MATCHING_ABORTED_FOR_PAGE_CONTINUATION"
    )
    assert snapshot["exploration_frontier"]["explored_action_count"] == 2
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0


async def test_supervisor_continues_dynamic_frontier_with_same_run_state() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("入口页已完成"),
        make_function_call_response(
            "5",
            "page_capture_tool",
            (
                '{"mode":"single_trigger","exploration_action_id":"exploration-2",'
                '"navigation_depth":1}'
            ),
        ),
        make_function_call_response("6", "page_detection_tool", '{"page_capture_id":"capture-2"}'),
        make_function_call_response(
            "7",
            "candidate_rules_tool",
            '{"page_type":"settings","page_capture_id":"capture-2"}',
        ),
        make_text_response("所有动态发现页面已完成"),
    ]
    client = RequestCountingScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_dynamic_page_capture_tool,
            fake_dynamic_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect dynamic pages", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "所有动态发现页面已完成"
    assert snapshot["continuation_count"] == 0
    assert snapshot["terminal_page_count"] == 2
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0
    assert snapshot["exploration_frontier"]["explored_action_count"] == 2
    assert snapshot["exploration_frontier"]["explored_actions"][-1]["status"] == "explored"
    assert len(client.received_messages) == 9
    page_transition_content_types = {
        content.type for message in client.received_messages[5] for content in message.contents
    }
    assert "function_call" not in page_transition_content_types
    assert "function_result" not in page_transition_content_types
    assert result.usage is not None
    assert result.usage.ai_calls == 9


async def test_supervisor_executes_remaining_business_pages_after_one_branch_fails() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("首页质检完成，导航动作待执行"),
        make_function_call_response(
            "5",
            "page_capture_tool",
            '{"mode":"single_trigger","exploration_action_id":"exploration-2"}',
        ),
        make_text_response("第一个业务分支采集失败"),
        make_function_call_response(
            "6",
            "page_capture_tool",
            '{"mode":"single_trigger","exploration_action_id":"exploration-3"}',
        ),
        make_function_call_response("7", "page_detection_tool", '{"page_capture_id":"capture-2"}'),
        make_function_call_response(
            "8",
            "candidate_rules_tool",
            '{"page_type":"content_list","page_capture_id":"capture-2"}',
        ),
        make_function_call_response(
            "9",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-2",'
                '"evidence_artifact_ids":["artifact-2"]}'
            ),
        ),
        make_text_response("第二个业务分支质检完成"),
        make_function_call_response(
            "10",
            "page_capture_tool",
            '{"mode":"single_trigger","exploration_action_id":"exploration-4"}',
        ),
        make_function_call_response("11", "page_detection_tool", '{"page_capture_id":"capture-3"}'),
        make_function_call_response(
            "12",
            "candidate_rules_tool",
            '{"page_type":"content_list","page_capture_id":"capture-3"}',
        ),
        make_function_call_response(
            "13",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-3",'
                '"evidence_artifact_ids":["artifact-3"]}'
            ),
        ),
        make_text_response("其余动态业务页面全部质检完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_multi_branch_page_capture_tool,
            fake_multi_branch_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(
        agent, "inspect all dynamic business pages", runtime_settings=settings
    )

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["terminal_page_count"] == 3
    assert snapshot["inspected_page_count"] == 3
    assert all(
        page["completed_rule_count"] == page["candidate_rule_count"] == 1
        for page in snapshot["inspected_pages"]
    )
    frontier = snapshot["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["explored_action_count"] == 3
    assert frontier["inactive_action_count"] == 1
    assert frontier["inactive_actions"][0]["failure_code"] == "BRANCH_CAPTURE_FAILED"
    forced_page_captures = [
        item["tool_choice"]
        for item in client.received_options
        if isinstance(item.get("tool_choice"), dict)
        and item["tool_choice"].get("required_function_name") == "page_capture_tool"
    ]
    assert len(forced_page_captures) == 3


async def test_supervisor_explores_nested_detail_then_resumes_other_branch() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_text_response("首页质检完成"),
        make_function_call_response(
            "5",
            "page_capture_tool",
            '{"mode":"single_trigger","exploration_action_id":"exploration-2"}',
        ),
        make_function_call_response("6", "page_detection_tool", '{"page_capture_id":"capture-2"}'),
        make_function_call_response(
            "7",
            "candidate_rules_tool",
            '{"page_type":"content_list","page_capture_id":"capture-2"}',
        ),
        make_function_call_response(
            "8",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-2",'
                '"evidence_artifact_ids":["artifact-2"]}'
            ),
        ),
        make_text_response("列表页质检完成"),
        make_function_call_response(
            "9",
            "page_capture_tool",
            '{"mode":"single_trigger","exploration_action_id":"exploration-4"}',
        ),
        make_function_call_response("10", "page_detection_tool", '{"page_capture_id":"capture-3"}'),
        make_function_call_response(
            "11",
            "candidate_rules_tool",
            '{"page_type":"content_detail","page_capture_id":"capture-3"}',
        ),
        make_function_call_response(
            "12",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-3",'
                '"evidence_artifact_ids":["artifact-3"]}'
            ),
        ),
        make_text_response("详情页质检完成"),
        make_function_call_response(
            "13",
            "page_capture_tool",
            '{"mode":"single_trigger","exploration_action_id":"exploration-5"}',
        ),
        make_function_call_response("14", "page_detection_tool", '{"page_capture_id":"capture-4"}'),
        make_function_call_response(
            "15",
            "page_capture_tool",
            '{"mode":"single_trigger","exploration_action_id":"exploration-3"}',
        ),
        make_function_call_response("16", "page_detection_tool", '{"page_capture_id":"capture-5"}'),
        make_function_call_response(
            "17",
            "candidate_rules_tool",
            '{"page_type":"settings","page_capture_id":"capture-5"}',
        ),
        make_text_response("嵌套详情和其余业务分支均已完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_nested_branch_page_capture_tool,
            fake_nested_branch_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(
        agent, "inspect nested and sibling pages", runtime_settings=settings
    )

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.SUCCESS
    # 详情返回采集显式替代详情快照，因此活跃终态页为首页、原列表和设置页；
    # 已完成质检的详情仍保留在 inspected_pages 审计中。
    assert snapshot["terminal_page_count"] == 3
    assert snapshot["inspected_page_count"] == 4
    assert [page["page_type"] for page in snapshot["inspected_pages"]] == [
        "home",
        "content_list",
        "content_detail",
        "settings",
    ]
    assert [page["completed_rule_count"] for page in snapshot["inspected_pages"]] == [1, 1, 1, 0]
    inactive_pages = {page["page_capture_id"]: page for page in snapshot["inactive_pages"]}
    assert inactive_pages["capture-3"]["status"] == "superseded"
    assert inactive_pages["capture-3"]["superseded_by_page_capture_id"] == ("capture-4")
    assert inactive_pages["capture-4"]["abandon_reason"] == ("nested_detail_return_revisit")
    frontier = snapshot["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["explored_action_count"] == 5
    assert [action["action"] for action in frontier["explored_actions"]] == [
        "goto",
        "click",
        "click",
        "click",
        "back",
    ]
    action_results = {
        action["exploration_action_id"]: action["result_page_capture_id"]
        for action in frontier["explored_actions"]
    }
    assert action_results == {
        "exploration-1": "capture-1",
        "exploration-2": "capture-2",
        "exploration-3": "capture-5",
        "exploration-4": "capture-3",
        "exploration-5": "capture-4",
    }


async def test_supervisor_marks_line_loading_evidence_insufficient() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_text_response("线路加载需要补采，已按证据不足收口"),
    ]
    client = RequestCountingScriptedChatClient(responses)
    settings = _settings(max_continuation_attempts=1)
    agent = _build_agent(
        client,
        settings,
        [
            fake_product_lifecycle_page_capture_tool,
            fake_product_lifecycle_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect product lifecycle", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["continuation_count"] == 0
    assert snapshot["inspected_page_count"] == 0
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    terminal = snapshot["terminal_pages"][0]
    assert terminal["inspection_status"] == "degraded"
    assert terminal["page_detection"]["page_type"] == "line_loading"
    assert terminal["failure"]["code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    frontier = snapshot["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["explored_action_count"] == 1


async def test_supervisor_stops_before_startup_after_line_loading() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_detection_tool", '{"page_capture_id":"capture-1"}'),
        make_text_response("线路加载暂不补采，未进入开屏广告规则流程"),
    ]
    client = RequestCountingScriptedChatClient(responses)
    settings = _settings(max_continuation_attempts=1)
    agent = _build_agent(
        client,
        settings,
        [
            fake_product_lifecycle_page_capture_tool,
            fake_product_lifecycle_page_detection_tool,
            fake_multi_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect product lifecycle", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["inspected_pages"] == []
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    terminal = snapshot["terminal_pages"][0]
    assert terminal["inspection_status"] == "degraded"
    assert terminal["page_detection"]["page_type"] == "line_loading"
    assert terminal["failure"]["code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    frontier = snapshot["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["explored_action_count"] == 1
    assert [action["action"] for action in frontier["explored_actions"]] == ["goto"]


async def test_explicit_frontier_closure_allows_completed_termination() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"]}'
            ),
        ),
        make_function_call_response(
            "5",
            "exploration_closure_tool",
            (
                '{"reason":"max_pages_reached","action_status":"skipped",'
                '"close_all_pending_actions":true}'
            ),
        ),
        make_text_response("page inspection finished"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_with_frontier_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
            fake_exploration_closure_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect page", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert snapshot["pending_page_count"] == 0
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0
    assert snapshot["exploration_frontier"]["inactive_actions"][0]["status"] == "skipped"


async def test_extra_middleware_receives_run_working_memory() -> None:
    client = ScriptedChatClient(
        [
            make_function_call_response("1", "echo_tool", '{"x":"ok"}'),
            make_text_response("finished"),
        ]
    )
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])
    middleware = WorkingMemoryBindingMiddleware()

    result = await run_main_agent(
        agent,
        "test binding",
        runtime_settings=settings,
        extra_middleware=[middleware],
    )

    assert middleware.working_memory is result.working_memory


async def test_rule_judge_result_evidence_is_inherited_by_real_agent_tool_loop() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "rule_judge_tool",
            (
                '{"rule_code":"alioth_1","page_capture_id":"capture-1",'
                '"evidence_artifact_ids":["artifact-1"],"judge_input":{}}'
            ),
        ),
        make_function_call_response(
            "5",
            "record_rule_execution_tool",
            ('{"execution_status":"pass","rule_code":"alioth_1","page_capture_id":"capture-1"}'),
        ),
        make_text_response("页面质检完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
            fake_rule_judge_tool,
            fake_handoff_record_rule_execution_tool,
        ],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "质检页面", runtime_settings=settings, sink=sink)

    snapshot = result.working_memory.audit_snapshot()
    record_trace = next(
        entry for entry in sink.entries if entry.tool_name == "record_rule_execution_tool"
    )
    assert result.termination_reason == TerminationReason.COMPLETED
    assert snapshot["inspected_page_count"] == 1
    assert '"evidence_artifact_ids": ["artifact-1"]' in record_trace.arguments


async def test_supplemental_capture_completion_reports_completed() -> None:
    fake_sequential_page_capture_tool.calls = 0
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "page_capture_tool",
            '{"mode":"observe","supersedes_page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "page_detection_tool",
            '{"page_capture_id":"capture-2"}',
        ),
        make_function_call_response(
            "5",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-2"}',
        ),
        make_function_call_response(
            "6",
            "record_rule_execution_tool",
            (
                '{"execution_status":"pass","rule_id":"rule-1",'
                '"page_capture_id":"capture-2",'
                '"evidence_artifact_ids":["artifact-2"]}'
            ),
        ),
        make_text_response("page inspection finished"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_sequential_page_capture_tool,
            fake_supplemental_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_execution_tool,
        ],
    )

    result = await run_main_agent(agent, "check page", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert snapshot["pending_page_count"] == 0
    assert snapshot["inactive_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["inactive_pages"][0]["status"] == "superseded"


async def test_multi_candidate_batch_binds_page_evidence_and_completes() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response(
            "2",
            "page_detection_tool",
            '{"page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "3",
            "candidate_rules_tool",
            '{"page_type":"home","page_capture_id":"capture-1"}',
        ),
        make_function_call_response(
            "4",
            "record_rule_executions_batch_tool",
            (
                '{"page_capture_id":"capture-1","executions":['
                '{"rule_id":"rule-1","rule_code":"alioth_1",'
                '"execution_status":"pass","evidence_artifact_ids":[]},'
                '{"rule_id":"rule-2","rule_code":"alioth_2",'
                '"execution_status":"skipped"}]}'
            ),
        ),
        make_text_response("多候选规则完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings(max_steps=4)
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_multi_candidate_rules_tool,
            fake_record_rule_executions_batch_tool,
        ],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(
        agent,
        "质检页面",
        runtime_settings=settings,
        sink=sink,
    )

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.working_memory.step_count == 4
    assert snapshot["inspected_page_count"] == 1
    assert snapshot["inspected_pages"][0]["completed_rule_count"] == 2
    batch_trace = next(
        entry for entry in sink.entries if entry.tool_name == "record_rule_executions_batch_tool"
    )
    assert '"evidence_artifact_ids": ["artifact-1"]' in batch_trace.arguments


async def test_page_budget_expands_from_runtime_discovery_without_known_page_count() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"observe"}'),
        *[
            make_function_call_response(str(index), "echo_tool", f'{{"x":"{index}"}}')
            for index in range(2, 7)
        ],
        make_function_call_response("7", "page_detection_tool", '{"page_capture_id":"capture-2"}'),
        make_function_call_response(
            "8",
            "candidate_rules_tool",
            '{"page_type":"settings","page_capture_id":"capture-2"}',
        ),
        make_text_response("动态页面工作已完成"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings(max_steps=4, adaptive_page_budget=True)
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            echo_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
        ],
    )

    result = await run_main_agent(agent, "inspect unknown pages", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.working_memory.step_count == 8
    assert result.working_memory.discovered_page_work_count() == 1


async def test_uninspected_capture_remains_incomplete_after_switching_pages() -> None:
    responses = [
        make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
        make_function_call_response("2", "page_capture_tool", '{"mode":"observe"}'),
        make_function_call_response(
            "3",
            "page_detection_tool",
            '{"page_capture_id":"capture-2"}',
        ),
        make_function_call_response(
            "4",
            "candidate_rules_tool",
            '{"page_type":"settings","page_capture_id":"capture-2"}',
        ),
        make_text_response("page inspection finished"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [fake_page_capture_tool, fake_page_detection_tool, fake_candidate_rules_tool],
    )

    result = await run_main_agent(agent, "check page", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.INCOMPLETE
    assert snapshot["current_page"] is None
    assert snapshot["pending_page_count"] == 0
    terminal_capture = next(
        item for item in snapshot["terminal_pages"] if item["page_capture_id"] == "capture-1"
    )
    assert terminal_capture["inspection_status"] == "failed"


async def test_normal_text_does_not_complete_captured_page() -> None:
    client = ScriptedChatClient(
        [
            make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
            make_text_response("页面已经处理完成"),
        ]
    )
    settings = _settings()
    agent = _build_agent(client, settings, [fake_page_capture_tool])

    result = await run_main_agent(agent, "质检页面", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.INCOMPLETE
    assert result.final_text == "页面已经处理完成"
    assert snapshot["current_page"] is None
    assert snapshot["terminal_pages"][0]["inspection_status"] == "failed"


async def test_normal_text_degrades_page_with_unwritten_candidates() -> None:
    client = ScriptedChatClient(
        [
            make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
            make_function_call_response(
                "2",
                "page_detection_tool",
                '{"page_capture_id":"capture-1"}',
            ),
            make_function_call_response(
                "3",
                "candidate_rules_tool",
                '{"page_type":"home","page_capture_id":"capture-1"}',
            ),
            make_text_response("候选规则已经处理完成"),
        ]
    )
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [fake_page_capture_tool, fake_page_detection_tool, fake_candidate_rules_tool],
    )

    result = await run_main_agent(agent, "质检页面", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["current_page"] is None
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["terminal_pages"][0]["candidate_rule_count"] == 1
    assert snapshot["terminal_pages"][0]["completed_rule_count"] == 1


async def test_premature_final_text_continues_and_completes_unwritten_candidates() -> None:
    client = RequestCountingScriptedChatClient(
        [
            make_function_call_response("1", "page_capture_tool", '{"mode":"goto"}'),
            make_function_call_response(
                "2",
                "page_detection_tool",
                '{"page_capture_id":"capture-1"}',
            ),
            make_function_call_response(
                "3",
                "candidate_rules_tool",
                '{"page_type":"home","page_capture_id":"capture-1"}',
            ),
            make_text_response("候选规则已经处理完成"),
            make_function_call_response(
                "4",
                "record_rule_executions_batch_tool",
                (
                    '{"page_capture_id":"capture-1","executions":['
                    '{"execution_status":"pass","rule_id":"rule-1",'
                    '"rule_code":"alioth_1",'
                    '"evidence_artifact_ids":["artifact-1"]}]}'
                ),
            ),
            make_text_response("页面质检完成"),
        ]
    )
    settings = _settings()
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
            fake_candidate_rules_tool,
            fake_record_rule_executions_batch_tool,
        ],
    )

    result = await run_main_agent(agent, "质检页面", runtime_settings=settings)

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "页面质检完成"
    assert snapshot["current_page"] is None
    assert snapshot["inspected_page_count"] == 1
    assert result.usage is not None
    assert result.usage.ai_calls == 6
    assert any(
        "结构化页面工作记忆尚未收敛" in message.text
        for messages in client.received_messages
        for message in messages
    )


async def test_normal_completion_reports_model_call_count() -> None:
    client = RequestCountingScriptedChatClient(
        [
            make_function_call_response("1", "echo_tool", '{"x": "a"}'),
            make_function_call_response("2", "echo_tool", '{"x": "b"}'),
            make_text_response("任务完成"),
        ]
    )
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])

    result = await run_main_agent(agent, "go", runtime_settings=settings)

    assert result.usage is not None
    assert result.usage.ai_calls == 3


async def test_read_only_skill_executes_with_local_call_history(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: 测试只读 Skill 加载\n---\n\n执行测试流程。\n",
        encoding="utf-8",
    )
    client = ServerStoredScriptedChatClient(
        [
            make_function_call_response(
                "skill-call-1", "load_skill", '{"skill_name":"test-skill"}'
            ),
            make_text_response("Skill 已加载"),
        ]
    )
    settings = _settings()
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        context_providers=[AliothSkillsProvider.from_paths(tmp_path / "skills")],
    )

    result = await run_main_agent(agent, "加载测试 Skill", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "Skill 已加载"
    assert result.working_memory.step_count == 1
    assert len(client.received_messages) == 2
    second_call_content_types = {
        content.type for message in client.received_messages[1] for content in message.contents
    }
    assert {"function_call", "function_result"} <= second_call_content_types


async def test_empty_response_without_tool_call_is_not_completed() -> None:
    client = ScriptedChatClient([make_text_response("")])
    settings = _settings()
    agent = _build_agent(client, settings, [])

    result = await run_main_agent(agent, "go", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.ERROR
    assert result.error_message == "Agent 未返回文本且未执行任何工具，不能判定为完成。"
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW


async def test_read_only_skill_tools_execute_and_are_traced() -> None:
    """读取 Skill 正文是只读动作，应直接执行并进入动作留痕。"""
    responses = [
        make_function_call_response("1", "load_skill", '{"skill_name": "qc-workflow"}'),
        make_text_response("workflow loaded"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        context_providers=[build_skills_context_provider(sub_dirs=("qc-workflow",))],
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "workflow loaded"
    assert result.working_memory.step_count == 1
    assert len(sink.entries) == 1
    assert sink.entries[0].tool_name == "load_skill"
    assert sink.entries[0].status == "ok"
    assert "qc-workflow" in sink.entries[0].arguments


async def test_max_steps_hard_cap_terminates_even_when_provider_ignores_tool_choice_none() -> None:
    """即便供应商完全不遵守框架下发的 tool_choice=none（软上限失效），
    自定义 LoopGuardMiddleware 仍必须硬性终止循环，且被拦截的尝试需出现在留痕里。
    """
    responses = [
        make_function_call_response(str(i), "echo_tool", f'{{"x": "{i}"}}') for i in range(10)
    ]
    client = NonCompliantChatClient(responses)
    settings = _settings(max_steps=3)
    agent = _build_agent(client, settings, [echo_tool])
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.MAX_STEPS_REACHED
    assert result.working_memory.step_count == 3
    assert result.working_memory.action_sequence == 4
    assert [entry.status for entry in sink.entries] == ["ok", "ok", "ok", "terminated"]


async def test_optimized_direct_dispatch_stops_before_another_provider_call_at_max_steps(
    monkeypatch,
) -> None:
    """结构化直接派发命中护栏后必须原地收口，不能再请求一次 Main Agent。"""
    monkeypatch.setattr(
        agent_factory_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(pipeline_mode="on")
        ),
    )
    monkeypatch.setattr(
        agent_factory_module,
        "get_agent_run_context",
        lambda: AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
        ),
    )
    client = ScriptedChatClient([])
    # 本用例验证固定硬步数护栏；自适应页面预算会在首采后按逻辑页扩容，
    # 与“第二次直接派发立即被 max_steps 拒绝”的断言不是同一场景。
    settings = _settings(max_steps=1, adaptive_page_budget=False)
    agent = _build_agent(
        client,
        settings,
        [
            fake_page_capture_tool,
            fake_page_detection_tool,
        ],
    )
    direct_tools = {
        "page_capture_tool": SimpleNamespace(tool=fake_page_capture_tool),
        "page_detection_tool": SimpleNamespace(tool=fake_page_detection_tool),
    }
    registry_get = agent_factory_module.default_registry.get
    monkeypatch.setattr(
        agent_factory_module.default_registry,
        "get",
        lambda name: direct_tools.get(name) or registry_get(name),
    )
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(
        agent,
        "请执行任务。\n目标URL：https://example.com/\n规则集：test@1",
        runtime_settings=settings,
        sink=sink,
    )

    assert result.termination_reason == TerminationReason.MAX_STEPS_REACHED
    assert result.working_memory.step_count == 1
    assert result.working_memory.action_sequence == 2
    assert client.received_messages == []
    assert [entry.status for entry in sink.entries] == ["ok", "terminated"]


async def test_optimized_loop_terminalizes_undispatchable_page_work(
    monkeypatch,
) -> None:
    """结构化派发器无下一步时，未终态页面必须转复核，禁止再请求模型空转。"""
    monkeypatch.setattr(
        agent_factory_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(pipeline_mode="on")
        ),
    )
    monkeypatch.setattr(agent_factory_module, "prepare_work_dispatch", lambda memory: None)

    memory = agent_factory_module.WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/movie",
        page_title="视频列表弹窗",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        page_state="popup",
        rule_matching_allowed=True,
    )
    client = ScriptedChatClient([])
    settings = _settings()
    agent = _build_agent(client, settings, [])
    scope = agent_factory_module.AgentRunScope(
        session=AgentSession(),
        working_memory=memory,
        trace_sink=InMemoryActionTraceSink(),
        middleware=[],
    )

    await agent_factory_module._run_supervised_agent_loop(
        agent=agent,
        messages="check page",
        scope=scope,
        runtime_settings=settings,
        background_tasks={},
    )

    snapshot = memory.audit_snapshot()
    assert memory.termination_reason == "page_work_undispatchable"
    assert agent_factory_module._classify_termination(memory) == TerminationReason.INCOMPLETE
    assert memory.requires_review() is True
    assert snapshot["failed_page_count"] == 1
    assert snapshot["pending_page_count"] == 0
    assert client.received_messages == []


async def test_stuck_detection_terminates_on_repeated_identical_calls() -> None:
    responses = [
        make_function_call_response(str(i), "echo_tool", '{"x": "same"}') for i in range(10)
    ]
    client = NonCompliantChatClient(responses)
    settings = _settings(stuck_window=3)
    agent = _build_agent(client, settings, [echo_tool])
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.STUCK_DETECTED
    assert sink.entries[-1].status == "terminated"


async def test_wall_clock_timeout_is_reported_without_raising() -> None:
    client = HangingChatClient()
    settings = _settings(max_duration_seconds=0.05)
    agent = _build_agent(client, settings, [echo_tool])

    result = await run_main_agent(agent, "go", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.TIMEOUT
    assert result.final_text is None


async def test_wrapped_chat_client_error_is_classified_as_llm_provider_failure(
    monkeypatch,
) -> None:
    async def fail_supervised_agent(**kwargs: Any) -> None:
        del kwargs
        raise ChatClientException("模型网关连接失败")

    monkeypatch.setattr(
        agent_factory_module,
        "_run_supervised_agent",
        fail_supervised_agent,
    )
    client = ScriptedChatClient([make_text_response("不会返回")])
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])

    result = await run_main_agent(agent, "go", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.ERROR
    assert result.error_category == ErrorCategory.LLM_PROVIDER, result.error_message
    assert result.working_memory.termination_reason == "llm_provider_error"


async def test_retryable_provider_connection_error_continues_same_run(
    monkeypatch,
) -> None:
    class FlakyAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> str:
            del args, kwargs
            self.calls += 1
            if self.calls == 1:
                raise ChatClientException(
                    "模型网关连接瞬断",
                    ConnectionError("temporary connection failure"),
                )
            return "连接恢复后完成"

    monkeypatch.setattr(
        agent_factory_module,
        "PROVIDER_RETRY_BACKOFF_SECONDS",
        0,
    )
    agent = FlakyAgent()

    result = await agent_factory_module._run_agent_with_provider_retry(
        agent=agent,
        messages="go",
        middleware=[],
        session=object(),
        options=None,
    )

    assert result == "连接恢复后完成"
    assert agent.calls == 2


async def test_retryable_provider_http_503_continues_same_run(monkeypatch) -> None:
    class ProviderOverloadedError(Exception):
        status_code = 503

    class FlakyAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> str:
            del args, kwargs
            self.calls += 1
            if self.calls == 1:
                raise ChatClientException(
                    "Error code: 503 - system cpu overloaded",
                    ProviderOverloadedError(),
                )
            return "服务恢复后完成"

    monkeypatch.setattr(
        agent_factory_module,
        "PROVIDER_OVERLOAD_RETRY_BACKOFF_SECONDS",
        0,
    )
    agent = FlakyAgent()

    result = await agent_factory_module._run_agent_with_provider_retry(
        agent=agent,
        messages="go",
        middleware=[],
        session=object(),
        options=None,
    )

    assert result == "服务恢复后完成"
    assert agent.calls == 2


async def test_persistent_provider_http_503_uses_safe_diagnostic(monkeypatch) -> None:
    class FailingAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.calls += 1
            raise ChatClientException(
                "Error code: 503 - system cpu overloaded; secret response body",
            )

    monkeypatch.setattr(
        agent_factory_module,
        "PROVIDER_OVERLOAD_RETRY_BACKOFF_SECONDS",
        0,
    )
    failing_agent = FailingAgent()
    with pytest.raises(ChatClientException) as exc_info:
        await agent_factory_module._run_agent_with_provider_retry(
            agent=failing_agent,
            messages="go",
            middleware=[],
            session=object(),
            options=None,
        )
    assert failing_agent.calls == 3

    async def fail_supervised_agent(**kwargs: Any) -> None:
        del kwargs
        raise exc_info.value

    monkeypatch.setattr(
        agent_factory_module,
        "_run_supervised_agent",
        fail_supervised_agent,
    )
    client = ScriptedChatClient([make_text_response("不会返回")])
    settings = _settings()
    agent = _build_agent(client, settings, [echo_tool])

    result = await run_main_agent(agent, "go", runtime_settings=settings)

    assert result.termination_reason == TerminationReason.ERROR
    assert result.error_category == ErrorCategory.LLM_PROVIDER, result.error_message
    assert result.error_message == (
        "模型 API 返回 HTTP 503（服务暂时不可用），Main Agent 未能完成本轮处理。"
    )
    assert "secret response body" not in result.error_message


async def test_provider_http_401_is_not_retried() -> None:
    class UnauthorizedAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.calls += 1
            raise ChatClientException("Error code: 401 - invalid api key")

    agent = UnauthorizedAgent()
    with pytest.raises(ChatClientException):
        await agent_factory_module._run_agent_with_provider_retry(
            agent=agent,
            messages="go",
            middleware=[],
            session=object(),
            options=None,
        )

    assert agent.calls == 1


async def test_tool_exception_is_recorded_as_error_and_loop_continues_to_completion() -> None:
    @tool(approval_mode="never_require")
    def failing_tool() -> str:
        """测试用的占位工具：总是抛出异常。"""
        raise RuntimeError("boom")

    responses = [
        make_function_call_response("1", "failing_tool", "{}"),
        make_text_response("done-after-error"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(client, settings, [failing_tool])
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "done-after-error"
    assert sink.entries[0].status == "error"
    assert sink.entries[0].error_message is not None
    assert "boom" in sink.entries[0].error_message


async def test_page_tool_exception_becomes_terminal_action_and_returns_to_agent() -> None:
    @tool(name="page_capture_tool", approval_mode="never_require")
    def failing_page_capture_tool(
        mode: str,
        url: str | None = None,
        navigation_depth: int | None = None,
    ) -> dict:
        """测试用页面采集工具：模拟单页浏览器异常。"""
        del mode, url, navigation_depth
        raise RuntimeError("browser crashed")

    responses = [
        make_function_call_response(
            "1",
            "page_capture_tool",
            '{"mode":"goto","url":"https://example.com","navigation_depth":0}',
        ),
        make_text_response("continue-after-page-error"),
    ]
    client = ScriptedChatClient(responses)
    settings = _settings()
    agent = _build_agent(client, settings, [failing_page_capture_tool])
    sink = InMemoryActionTraceSink()

    result = await run_main_agent(agent, "go", runtime_settings=settings, sink=sink)

    frontier = result.working_memory.audit_snapshot()["exploration_frontier"]
    assert result.termination_reason == TerminationReason.COMPLETED
    assert result.final_text == "continue-after-page-error"
    assert frontier["pending_action_count"] == 0
    assert frontier["inactive_actions"][0]["failure_code"] == "PAGE_CAPTURE_FAILED"
    assert sink.entries[0].status == "error"
    assert sink.entries[0].error_message == "browser crashed"


async def test_consecutive_infrastructure_failures_open_global_circuit() -> None:
    @tool(name="page_capture_tool", approval_mode="never_require")
    def unavailable_page_capture_tool(mode: str, url: str | None = None) -> dict:
        """测试用页面采集工具：模拟浏览器基础设施持续不可用。"""
        del mode, url
        raise OSError("browser service unavailable")

    responses = [
        make_function_call_response(
            str(index),
            "page_capture_tool",
            f'{{"mode":"goto","url":"https://example.com/{index}"}}',
        )
        for index in range(1, 4)
    ]
    responses.append(make_text_response("无法继续"))
    client = ScriptedChatClient(responses)
    settings = _settings(max_consecutive_infrastructure_failures=3)
    agent = _build_agent(client, settings, [unavailable_page_capture_tool])

    result = await run_main_agent(
        agent,
        "inspect pages",
        runtime_settings=settings,
        retry_config=RetryConfig(max_attempts=1, backoff_seconds=0),
    )

    snapshot = result.working_memory.audit_snapshot()
    assert result.termination_reason == TerminationReason.INFRASTRUCTURE_UNAVAILABLE
    assert result.error_category == ErrorCategory.INFRASTRUCTURE
    assert result.execution_outcome == ExecutionOutcome.NEEDS_REVIEW
    assert snapshot["consecutive_infrastructure_failures"] == 3
    assert snapshot["exploration_frontier"]["inactive_action_count"] == 3


def test_normalize_result_unwraps_single_item_content_list() -> None:
    """agent_framework 把工具返回包成单元素 Content 列表时，留痕应还原为原始文本。"""
    from services.api.app.agents.runtime.action_trace import normalize_result

    class FakeTextContent:
        def __init__(self, text: str) -> None:
            self.text = text

    assert normalize_result([FakeTextContent('{"execution_status": "success"}')]) == (
        '{"execution_status": "success"}'
    )
    assert normalize_result(['{"execution_status": "success"}']) == (
        '{"execution_status": "success"}'
    )
    assert normalize_result([FakeTextContent("纯文本 Skill 内容")]) == "纯文本 Skill 内容"
    assert normalize_result({"key": "值"}) == '{"key": "值"}'
    assert normalize_result(None) is None


def test_normalize_result_truncates_oversized_payload() -> None:
    from services.api.app.agents.runtime.action_trace import normalize_result

    normalized = normalize_result("x" * 5000, limit=100)

    assert normalized is not None
    assert normalized.startswith("x" * 100)
    assert "截断" in normalized
    assert "5000" in normalized
