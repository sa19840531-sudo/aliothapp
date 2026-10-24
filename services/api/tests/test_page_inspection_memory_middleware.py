"""页面质检工作记忆中间件测试。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.action_trace import (
    ActionTraceEntry,
    InMemoryActionTraceSink,
)
from services.api.app.agents.runtime.business_middlewares.page_inspection_memory_middleware import (
    PageInspectionMemoryMiddleware,
)
from services.api.app.agents.runtime.models import RunResult, TerminationReason
from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.runtime.work_dispatcher import prepare_work_dispatch
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


@pytest.fixture(autouse=True)
def _default_unoptimized_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="off")),
    )


def _context(tool_name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(name=tool_name),
        arguments=arguments,
        result=None,
    )


async def test_page_capture_result_initializes_current_page_memory() -> None:
    memory = WorkingMemory()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_capture_tool", {"mode": "goto"})

    async def _next() -> None:
        context.result = {
            "execution_status": "success",
            "page_capture_id": "capture-1",
            "final_url": "https://example.com/home",
            "title": "首页",
            "page_seq": 1,
            "artifact_ids": ["artifact-1"],
        }

    await middleware.process(context, _next)

    assert context.result["working_memory"]["current_page"]["page_capture_id"] == ("capture-1")
    assert memory.has_page_capture("capture-1") is True


async def test_obstruction_chain_limit_terminalizes_current_page() -> None:
    memory = WorkingMemory()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_capture_tool", {"mode": "single_trigger"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "execution_status": "success",
                "page_capture_id": "capture-1",
                "final_url": "https://example.com/home",
                "title": "首页",
                "page_seq": 1,
                "artifact_ids": ["artifact-1"],
                "metrics": {
                    "obstruction_chain": {
                        "outcome": "dismissal_limit_reached",
                        "remaining_obstruction": True,
                    }
                },
            },
        ),
    )

    snapshot = context.result["working_memory"]
    assert snapshot["current_page"] is None
    assert snapshot["terminal_pages"][0]["inspection_status"] == "failed"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "PAGE_OBSTRUCTION_CLEAR_LIMIT_REACHED"
    )


async def test_countdown_capture_stays_inspectable_before_detection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = WorkingMemory()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_capture_tool", {"mode": "goto"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "execution_status": "success",
                "page_capture_id": "capture-1",
                "mode": "goto",
                "final_url": "https://example.com/",
                "page_seq": 1,
                "artifact_ids": ["artifact-1"],
                "artifacts": [],
                "visible_text_excerpt": "5s(跳过)",
            },
        ),
    )

    snapshot = memory.audit_snapshot()
    assert snapshot["current_page"]["page_capture_id"] == "capture-1"
    assert snapshot["degraded_page_count"] == 0
    assert snapshot["failed_page_count"] == 0
    assert snapshot["terminal_pages"] == []
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0


async def test_page_detection_result_is_recorded_for_same_page_capture() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "home",
                "rule_matching_allowed": True,
            },
        ),
    )

    detection = context.result["working_memory"]["current_page"]["page_detection"]
    assert detection == {
        "detection_status": "recognized",
        "page_type": "home",
        "rule_matching_allowed": True,
    }


async def test_requirement_publish_failure_terminalizes_page_with_specific_code() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "error",
                "page_type": "home",
                "page_state": "stable",
                "rule_matching_allowed": False,
                "failure_kind": "infrastructure",
                "error_code": "EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
                "error_message": "页面证据需求未能发布，当前页面已停止规则匹配。",
                "retryable": False,
            },
        ),
    )

    snapshot = memory.audit_snapshot()
    assert snapshot["current_page"] is None
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "EVIDENCE_REQUIREMENT_PUBLISH_FAILED"
    )
    assert snapshot["page_flow_blocking_failure"]["page_capture_id"] == "capture-1"


async def test_requirement_publish_failure_allows_real_frontier_capture() -> None:
    memory = _memory_with_capture()
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "text=详情",
                "risk_level": "low",
                "purpose": "进入已发现详情页",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=False,
        supersedes_source_page=False,
        requires_source_inspection_complete=False,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    detection = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection,
        lambda: _set_result(
            detection,
            {
                "page_capture_id": "capture-1",
                "detection_status": "error",
                "page_type": "home",
                "rule_matching_allowed": False,
                "error_code": "EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
                "error_message": "页面证据需求未能发布，当前页面已停止规则匹配。",
                "retryable": False,
            },
        ),
    )

    frontier_capture = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "text=详情",
            "exploration_action_id": action_id,
        },
    )
    await middleware.process(
        frontier_capture,
        lambda: _set_result(
            frontier_capture,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/detail",
                "title": "详情",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
            },
        ),
    )

    assert frontier_capture.result["page_capture_id"] == "capture-2"
    assert memory.termination_reason is None

    next_detection = _context("page_detection_tool", {"page_capture_id": "capture-2"})
    await middleware.process(
        next_detection,
        lambda: _set_result(
            next_detection,
            {
                "page_capture_id": "capture-2",
                "detection_status": "recognized",
                "page_type": "content_detail",
                "rule_matching_allowed": True,
            },
        ),
    )

    assert memory.page_flow_blocking_failure is None


@pytest.mark.parametrize("tool_name", ["page_capture_tool", "page_detection_tool"])
async def test_requirement_publish_failure_stops_blind_page_flow_retry(
    tool_name: str,
) -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection,
        lambda: _set_result(
            detection,
            {
                "page_capture_id": "capture-1",
                "detection_status": "error",
                "page_type": "home",
                "rule_matching_allowed": False,
                "error_code": "EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
                "error_message": "页面证据需求未能发布，当前页面已停止规则匹配。",
                "retryable": False,
            },
        ),
    )
    arguments = (
        {"mode": "observe"}
        if tool_name == "page_capture_tool"
        else {"page_capture_id": "capture-1"}
    )
    retry = _context(tool_name, arguments)
    called = False

    async def _unexpected_call() -> None:
        nonlocal called
        called = True

    await middleware.process(retry, _unexpected_call)

    assert called is False
    assert retry.result["error_code"] == "EVIDENCE_REQUIREMENT_PUBLISH_FAILED"
    assert retry.result["retryable"] is False
    assert memory.termination_reason == "infrastructure_unavailable"


@pytest.mark.parametrize("action_state", ["unknown", "failed"])
async def test_requirement_publish_failure_rejects_nonrunnable_frontier_id(
    action_state: str,
) -> None:
    memory = _memory_with_capture()
    if action_state == "unknown":
        action_id = "fabricated-action-id"
    else:
        action_id = memory.remember_exploration_candidates(
            page_capture_id="capture-1",
            page_type="home",
            candidate_actions=[
                {
                    "action": "click",
                    "target": "text=已失效入口",
                    "risk_level": "low",
                    "purpose": "构造已终态 Frontier",
                }
            ],
            next_page_hypotheses=[],
        )[0]
        assert memory.fail_exploration_action(
            exploration_action_id=action_id,
            failure_code="ACTION_ALREADY_FAILED",
            failure_message="动作已进入失败终态",
            retryable=False,
        )
    middleware = PageInspectionMemoryMiddleware(memory)
    detection = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection,
        lambda: _set_result(
            detection,
            {
                "page_capture_id": "capture-1",
                "detection_status": "error",
                "page_type": "home",
                "rule_matching_allowed": False,
                "error_code": "EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
                "error_message": "页面证据需求未能发布，当前页面已停止规则匹配。",
                "retryable": False,
            },
        ),
    )
    retry = _context(
        "page_capture_tool",
        {"mode": "observe", "exploration_action_id": action_id},
    )
    called = False

    async def _unexpected_call() -> None:
        nonlocal called
        called = True

    await middleware.process(retry, _unexpected_call)

    assert called is False
    assert retry.result["error_code"] == "EVIDENCE_REQUIREMENT_PUBLISH_FAILED"
    assert retry.result["retryable"] is False
    assert memory.termination_reason == "infrastructure_unavailable"


@pytest.mark.parametrize(
    ("detection_status", "failure_code"),
    [
        ("ambiguous", "PAGE_DETECTION_AMBIGUOUS"),
        ("insufficient", "PAGE_DETECTION_INSUFFICIENT"),
    ],
)
async def test_stable_unresolved_detection_becomes_terminal_instead_of_redetection(
    detection_status: str,
    failure_code: str,
) -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "detection_status": detection_status,
                "page_type": None,
                "page_state": "stable",
                "reason": "页面无法在内容详情和个人主页之间唯一归类。",
                "rule_matching_allowed": False,
            },
        ),
    )

    snapshot = context.result["working_memory"]
    assert snapshot["current_page"] is None
    assert snapshot["failed_page_count"] == 1
    assert snapshot["terminal_pages"][0]["inspection_status"] == "failed"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == failure_code
    assert prepare_work_dispatch(memory) is None


async def test_transient_ambiguous_detection_marks_evidence_insufficient() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "ambiguous",
                "page_type": None,
                "page_state": "loading",
                "reason": "页面仍在加载，暂时无法归类。",
                "rule_matching_allowed": False,
            },
        ),
    )

    snapshot = memory.audit_snapshot()
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )
    assert memory.prepare_next_exploration_action(blocking_page_capture_id="capture-1") is None


def test_dispatcher_prioritizes_startup_ad_rule_judge_before_close_action() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        page_state="countdown",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": "text=跳过",
                "target_keywords": ["跳过", "广告"],
                "risk_level": "low",
                "purpose": "关闭开屏广告并进入业务页",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=False,
        requires_source_inspection_complete=False,
    )
    assert memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {
                "rule_id": "rule-16",
                "rule_code": "alioth_16",
                "category": "ad",
                "execution_route": {
                    "strategy": "hybrid",
                    "tool_name": "rule_judge_tool",
                    "session_dependency": "snapshot",
                    "ready": True,
                    "rule_engine_ready": False,
                },
            }
        ],
    )

    dispatch = prepare_work_dispatch(memory)

    assert dispatch is not None
    assert dispatch.tool_name == "rule_judge_tool"
    assert dispatch.arguments == {
        "rule_code": "alioth_16",
        "page_capture_id": "capture-1",
    }


async def test_page_detection_rejects_unknown_page_capture_before_tool_call() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "invented-capture"})
    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, call_next)

    assert called is False
    assert context.result["execution_status"] == "blocked"
    assert context.result["error_code"] == "WORKING_MEMORY_PAGE_NOT_FOUND"
    assert context.result["requested_page_capture_id"] == "invented-capture"
    assert "page_capture_id" not in context.result


async def test_page_detection_is_locked_after_rule_matching_started() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, call_next)

    assert called is False
    assert context.result["execution_status"] == "blocked"
    assert context.result["error_code"] == "PAGE_DETECTION_LOCKED_AFTER_RULE_MATCHING"
    assert memory.page_detection_result("capture-1") == {
        "detection_status": "recognized",
        "page_type": "startup_ad",
        "rule_matching_allowed": True,
    }


async def test_page_capture_without_frontier_is_blocked_while_page_inspection_in_progress() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_capture_tool", {"mode": "observe"})
    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, call_next)

    assert called is False
    assert context.result["execution_status"] == "blocked"
    assert context.result["error_code"] == "PAGE_INSPECTION_IN_PROGRESS"
    assert context.result["retryable"] is True
    assert context.result["working_memory"]["current_page"]["page_capture_id"] == "capture-1"


async def test_atomic_rule_judge_result_completes_page_without_pending_write() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_6"}],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    assert memory.discovered_page_work_count() == 1
    context = _context(
        "rule_judge_tool",
        {"rule_code": "alioth_6", "page_capture_id": "capture-1"},
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "execution_id": "execution-1",
                "rule_id": "rule-1",
                "rule_code": "alioth_6",
                "execution_status": "pass",
                "page_capture_id": "capture-1",
                "persisted": True,
            },
        ),
    )

    snapshot = context.result["working_memory"]
    completed_page = snapshot["inspected_pages"][0]
    assert completed_page["completed_rule_count"] == 1
    assert completed_page["pending_rule_judgements"] == []


async def test_page_detection_candidate_actions_are_written_to_frontier() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "home",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "detail_entry",
                        "target_keywords": ["detail"],
                        "risk_level": "low",
                        "purpose": "cover detail page rules",
                        "expected_next_page_type": "content_detail",
                    }
                ],
                "next_page_hypotheses": [],
            },
        ),
    )

    frontier = context.result["working_memory"]["exploration_frontier"]
    assert frontier["pending_action_count"] == 1
    assert frontier["pending_actions"][0]["source_page_capture_id"] == "capture-1"
    assert frontier["pending_actions"][0]["expected_next_page_type"] == "content_detail"


async def test_stable_obstruction_result_reuses_completed_source_inspection() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/moviePlayer?videoId=1",
        page_title="视频详情",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_detail",
        page_state="stable",
        rule_matching_allowed=True,
    )
    close_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_detail",
        candidate_actions=[
            {
                "action": "click",
                "target": "关闭",
                "target_keywords": ["关闭"],
                "risk_level": "low",
                "purpose": "关闭播放器区域上层广告/遮罩以露出播放控件",
                "expected_next_page_type": "video_detail",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=close_action_id,
        blocks_page_inspection=False,
        supersedes_source_page=True,
        requires_source_inspection_complete=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_detail",
        candidate_rules=[],
    )
    selected_close = memory.prepare_next_exploration_action()
    assert selected_close is not None
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/moviePlayer?videoId=1",
        page_title="视频详情",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=close_action_id,
        supersedes_page_capture_id="capture-1",
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-2"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-2",
                "detection_status": "recognized",
                "page_type": "video_detail",
                "page_state": "stable",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "相关推荐",
                        "target_keywords": ["相关推荐"],
                        "risk_level": "low",
                        "purpose": "探索详情页相关推荐",
                    }
                ],
            },
        ),
    )

    snapshot = context.result["working_memory"]
    reused = next(
        page for page in snapshot["inactive_pages"] if page["page_capture_id"] == "capture-2"
    )
    assert reused["inspection_status"] == "completed"
    assert reused["abandon_reason"] == "obstruction_result_reuses_source_inspection"
    assert memory.has_started_page_inspection("capture-2") is False
    next_action = memory.prepare_next_exploration_action()
    assert next_action is not None
    assert next_action["source_page_capture_id"] == "capture-2"
    assert next_action["target"] == "相关推荐"


async def test_authentication_required_branch_is_skipped_and_returns_before_next_branch() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    memory.record_page_rule_result(
        page_capture_id="capture-1",
        result={
            "execution_id": "execution-1",
            "rule_id": "rule-1",
            "execution_status": "pass",
        },
        arguments={"rule_id": "rule-1", "execution_status": "pass"},
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "text=我的",
                "target_keywords": ["我的"],
                "risk_level": "low",
                "purpose": "探索底部导航业务分支",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "text=影片",
                "target_keywords": ["影片"],
                "risk_level": "low",
                "purpose": "探索底部导航业务分支",
                "expected_next_page_type": "video_list",
                "navigation_depth": 1,
            },
        ],
        next_page_hypotheses=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/profile",
        page_title="登录",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=action_ids[0],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-2"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-2",
                "detection_status": "recognized",
                "page_type": "profile",
                "page_state": "login_required",
                "risk_flags": ["authentication_required"],
                "rule_matching_allowed": False,
            },
        ),
    )

    snapshot = context.result["working_memory"]
    skipped = next(
        page for page in snapshot["inactive_pages"] if page["page_capture_id"] == "capture-2"
    )
    assert skipped["status"] == "abandoned"
    assert skipped["abandon_reason"] == "authentication_required"
    pending = snapshot["exploration_frontier"]["pending_actions"]
    return_action = next(action for action in pending if action["action"] == "back")
    assert return_action["mandatory"] is True
    assert return_action["target_keywords"] == ["authentication_required_return"]
    assert return_action["navigation_depth"] == 1
    assert (
        memory.prepare_next_exploration_action()["exploration_action_id"]
        == (return_action["exploration_action_id"])
    )
    assert any(action["target"] == "text=影片" for action in pending)


async def test_same_url_authentication_popup_closes_without_browser_back() -> None:
    """同地址身份弹层应先关闭遮挡，不能误用浏览器历史返回。"""
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_rules=[],
    )
    open_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "身份凭证",
                "target_keywords": ["identity_credential"],
                "risk_level": "low",
                "purpose": "查看身份凭证",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/home",
        page_title="我的",
        page_seq=2,
        artifact_ids=["artifact-2"],
        exploration_action_id=open_action_id,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-2"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-2",
                "detection_status": "recognized",
                "page_type": "profile",
                "page_state": "popup",
                "risk_flags": ["authentication_required"],
                "rule_matching_allowed": False,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": ".van-overlay:visible",
                        "target_keywords": ["close_control"],
                        "risk_level": "low",
                        "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                        "expected_next_page_type": "profile",
                    }
                ],
            },
        ),
    )

    frontier = context.result["working_memory"]["exploration_frontier"]
    pending = frontier["pending_actions"]
    assert not any(action["action"] == "back" for action in pending)
    close_action = next(
        action for action in pending if action["target_keywords"] == ["close_control"]
    )
    assert close_action["mandatory"] is True
    assert close_action["blocks_page_inspection"] is True
    assert close_action["supersedes_source_page"] is True
    assert (
        memory.prepare_next_exploration_action(blocking_page_capture_id="capture-2")[
            "exploration_action_id"
        ]
        == close_action["exploration_action_id"]
    )


async def test_frontier_click_keeps_navigation_depth_and_drops_stale_url() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection_context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection_context,
        lambda: _set_result(
            detection_context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "home",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "text=影片",
                        "risk_level": "low",
                        "purpose": "探索底部导航业务分支",
                        "navigation_depth": 1,
                    }
                ],
                "next_page_hypotheses": [],
            },
        ),
    )
    action_id = detection_context.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ][0]["exploration_action_id"]
    capture_context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "text=影片",
            "url": "https://example.com/entry",
            "exploration_action_id": action_id,
        },
    )

    async def _capture_next() -> None:
        assert capture_context.arguments["navigation_depth"] == 1
        assert "url" not in capture_context.arguments
        capture_context.result = {
            "execution_status": "success",
            "page_capture_id": "capture-2",
            "final_url": "https://example.com/video",
            "title": "影片",
            "page_seq": 2,
            "artifact_ids": ["artifact-2"],
        }

    await middleware.process(capture_context, _capture_next)

    assert capture_context.result["working_memory"]["current_page"]["page_capture_id"] == (
        "capture-2"
    )


async def test_line_loading_marks_evidence_insufficient_without_supplemental_capture() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "line_loading",
                "rule_matching_allowed": False,
            },
        ),
    )

    snapshot = memory.audit_snapshot()
    assert snapshot["terminal_pages"][0]["page_detection"]["page_type"] == "line_loading"
    assert snapshot["pending_page_count"] == 0
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0
    assert memory.has_incomplete_page_inspection() is False


async def test_loading_state_on_recognized_business_page_marks_evidence_insufficient() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "home",
                "page_state": "loading",
                "rule_matching_allowed": False,
            },
        ),
    )

    snapshot = memory.audit_snapshot()
    assert snapshot["terminal_pages"][0]["page_detection"]["page_state"] == "loading"
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0


async def test_popup_close_waits_until_popup_rule_matching_finishes() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection_context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection_context,
        lambda: _set_result(
            detection_context,
            {
                "page_capture_id": "capture-1",
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
                        "purpose": "关闭第一层弹窗",
                    },
                    {
                        "action": "click",
                        "target": "text=我的",
                        "risk_level": "low",
                        "purpose": "进入个人页",
                    },
                ],
            },
        ),
    )

    frontier = detection_context.result["working_memory"]["exploration_frontier"]
    assert frontier["pending_action_count"] == 1
    assert frontier["pending_actions"][0]["target"] == "text=关闭"
    assert frontier["pending_actions"][0]["mandatory"] is True

    candidate_context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )
    called = False

    async def _candidate_next() -> None:
        nonlocal called
        called = True
        candidate_context.result = {
            "page_capture_id": "capture-1",
            "page_type": "home",
            "rules": [],
        }

    await middleware.process(candidate_context, _candidate_next)

    assert called is True
    assert candidate_context.arguments["page_state"] == "popup"
    assert candidate_context.result["working_memory"]["inspected_pages"][0]["status"] == "completed"

    close_action_id = frontier["pending_actions"][0]["exploration_action_id"]
    capture_context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "exploration_action_id": close_action_id,
        },
    )
    await middleware.process(
        capture_context,
        lambda: _set_result(
            capture_context,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/home",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
            },
        ),
    )

    assert capture_context.arguments["clear_obstructions"] is True


async def test_popup_chain_continuation_closes_before_repeating_rule_matching() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    first_detection = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        first_detection,
        lambda: _set_result(
            first_detection,
            {
                "page_capture_id": "capture-1",
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
                        "purpose": "关闭广告弹窗",
                    }
                ],
            },
        ),
    )
    candidate_context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )
    await middleware.process(
        candidate_context,
        lambda: _set_result(
            candidate_context,
            {
                "page_capture_id": "capture-1",
                "page_type": "home",
                "rules": [],
            },
        ),
    )
    close_action = memory.prepare_next_exploration_action()
    assert close_action is not None
    capture_context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "exploration_action_id": close_action["exploration_action_id"],
        },
    )
    await middleware.process(
        capture_context,
        lambda: _set_result(
            capture_context,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/home",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
            },
        ),
    )

    second_detection = _context("page_detection_tool", {"page_capture_id": "capture-2"})
    await middleware.process(
        second_detection,
        lambda: _set_result(
            second_detection,
            {
                "page_capture_id": "capture-2",
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
                        "purpose": "关闭下一层广告弹窗",
                    }
                ],
            },
        ),
    )

    current_page = second_detection.result["working_memory"]["current_page"]
    assert current_page["candidate_rule_count"] == 0
    pending_action = second_detection.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ][0]
    assert pending_action["source_page_capture_id"] == "capture-2"
    assert pending_action["blocks_page_inspection"] is True
    assert pending_action["requires_source_inspection_complete"] is False
    next_action = memory.prepare_next_exploration_action(blocking_page_capture_id="capture-2")
    assert next_action is not None
    assert next_action["clear_obstructions"] is True


async def test_page_capture_metrics_remember_productive_obstruction_dismissal_count() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        page_state="popup",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": '[role="dialog"] .btn-close:visible',
                "target_keywords": ["close_control"],
                "risk_level": "low",
                "purpose": "close popup layer",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=True,
    )
    assert memory.mark_exploration_action_selected(action_id)
    capture_context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "exploration_action_id": action_id,
            "supersedes_page_capture_id": "capture-1",
        },
    )

    await middleware.process(
        capture_context,
        lambda: _set_result(
            capture_context,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/movie",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
                "metrics": {"obstruction_chain": {"dismissal_count": 1}},
            },
        ),
    )

    capture = memory._page_inspection_state._page_captures["capture-2"]
    assert capture.obstruction_dismissal_count == 1


async def test_repeated_popup_close_blocks_rule_matching_until_closed() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    first_detection = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        first_detection,
        lambda: _set_result(
            first_detection,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "video_list",
                "page_state": "popup",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "text=关闭",
                        "risk_level": "low",
                        "purpose": "关闭第一层弹窗",
                    }
                ],
            },
        ),
    )
    first_action_id = first_detection.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ][0]["exploration_action_id"]
    first_candidate = _context(
        "candidate_rules_tool",
        {"page_type": "video_list", "page_capture_id": "capture-1"},
    )
    await middleware.process(
        first_candidate,
        lambda: _set_result(
            first_candidate,
            {
                "page_capture_id": "capture-1",
                "page_type": "video_list",
                "rules": [],
            },
        ),
    )
    first_close = _context(
        "page_capture_tool",
        {"exploration_action_id": first_action_id},
    )
    await middleware.process(
        first_close,
        lambda: _set_result(
            first_close,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/movie",
                "title": "视频列表弹窗",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
            },
        ),
    )
    second_detection = _context("page_detection_tool", {"page_capture_id": "capture-2"})
    await middleware.process(
        second_detection,
        lambda: _set_result(
            second_detection,
            {
                "page_capture_id": "capture-2",
                "detection_status": "recognized",
                "page_type": "video_list",
                "page_state": "popup",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "text=继续关闭",
                        "risk_level": "low",
                        "purpose": "关闭第二层弹窗",
                    }
                ],
            },
        ),
    )

    repeated_action = second_detection.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ][0]
    assert repeated_action["mandatory"] is True
    assert repeated_action["blocks_page_inspection"] is True

    second_candidate = _context(
        "candidate_rules_tool",
        {"page_type": "video_list", "page_capture_id": "capture-2"},
    )
    called = False

    async def _candidate_next() -> None:
        nonlocal called
        called = True

    await middleware.process(second_candidate, _candidate_next)

    assert called is False
    assert second_candidate.result["error_code"] == "PAGE_OBSTRUCTION_RECOVERY_REQUIRED"


async def test_page_obstruction_recovery_exhausts_after_twenty_attempts() -> None:
    memory = WorkingMemory()
    source_capture_id = "capture-1"
    memory.remember_page_capture(
        page_capture_id=source_capture_id,
        page_url="https://example.com/movie",
        page_title="Video list popup",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )

    for index in range(1, 22):
        memory.remember_page_detection(
            page_capture_id=source_capture_id,
            detection_status="recognized",
            page_type="video_list",
            page_state="popup",
            rule_matching_allowed=True,
        )
        action_ids = memory.remember_exploration_candidates(
            page_capture_id=source_capture_id,
            page_type="video_list",
            candidate_actions=[
                {
                    "action": "click",
                    "target": f"text=close-{index}",
                    "target_keywords": ["close", "popup"],
                    "risk_level": "low",
                    "purpose": f"close popup layer {index}",
                }
            ],
            next_page_hypotheses=[],
        )
        assert len(action_ids) == 1
        accepted = memory.require_exploration_action(
            exploration_action_id=action_ids[0],
            blocks_page_inspection=index > 1,
            supersedes_source_page=True,
            requires_source_inspection_complete=index == 1,
        )

        if index == 21:
            assert accepted is False
            snapshot = memory.audit_snapshot()
            failed = next(
                action
                for action in snapshot["exploration_frontier"]["inactive_actions"]
                if action["exploration_action_id"] == action_ids[0]
            )
            assert failed["status"] == "failed"
            assert failed["failure_code"] == "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
            assert all(
                action["exploration_action_id"] != action_ids[0]
                for action in snapshot["exploration_frontier"]["pending_actions"]
            )
            break

        assert accepted is True
        next_capture_id = f"capture-{index + 1}"
        memory.remember_page_capture(
            page_capture_id=next_capture_id,
            page_url="https://example.com/movie",
            page_title="Video list popup",
            page_seq=index + 1,
            artifact_ids=[f"artifact-{index + 1}"],
            exploration_action_id=action_ids[0],
            supersedes_page_capture_id=source_capture_id,
        )
        source_capture_id = next_capture_id


async def test_failed_popup_close_requires_reobservation_before_rule_matching() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection_context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection_context,
        lambda: _set_result(
            detection_context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "home",
                "page_state": "popup",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": '[role="dialog"] .btn',
                        "risk_level": "low",
                        "purpose": "关闭当前弹窗",
                    }
                ],
            },
        ),
    )
    click_action_id = detection_context.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ][0]["exploration_action_id"]
    candidate_context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )
    await middleware.process(
        candidate_context,
        lambda: _set_result(
            candidate_context,
            {
                "page_capture_id": "capture-1",
                "page_type": "home",
                "rules": [],
            },
        ),
    )
    capture_context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "exploration_action_id": click_action_id,
        },
    )
    await middleware.process(
        capture_context,
        lambda: _set_result(
            capture_context,
            {
                "execution_status": "error",
                "handled": True,
                "failure_scope": "page",
                "error_code": "PAGE_CAPTURE_FAILED",
                "error_message": "关闭控件暂时不可点击",
                "retryable": False,
            },
        ),
    )

    assert capture_context.arguments["mode"] == "single_trigger"
    assert capture_context.arguments["trigger_action"] == "click"
    assert capture_context.arguments["selector"] == '[role="dialog"] .btn'
    frontier = capture_context.result["working_memory"]["exploration_frontier"]
    assert frontier["inactive_actions"][0]["exploration_action_id"] == click_action_id
    assert frontier["inactive_actions"][0]["status"] == "failed"
    reobserve = frontier["pending_actions"][0]
    assert reobserve["action"] == "observe"
    assert reobserve["mandatory"] is True
    assert reobserve["supersedes_source_page"] is True

    candidate_context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )
    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await middleware.process(candidate_context, call_next)

    assert called is False
    assert candidate_context.result["error_code"] == "PAGE_OBSTRUCTION_RECOVERY_REQUIRED"


async def test_browser_session_crash_requires_goto_recovery() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": '[role="dialog"] .btn',
                "risk_level": "low",
                "purpose": "关闭当前弹窗",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=True,
        requires_source_inspection_complete=False,
    )
    context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "exploration_action_id": action_id,
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "execution_status": "error",
                "handled": True,
                "failure_scope": "page",
                "failure_kind": "infrastructure",
                "error_code": "PAGE_CAPTURE_SESSION_CRASHED",
                "error_message": "Page.content: Target crashed",
                "retryable": True,
            },
        ),
    )

    pending = context.result["working_memory"]["exploration_frontier"]["pending_actions"]
    assert len(pending) == 1
    assert pending[0]["action"] == "goto"
    assert pending[0]["target"] == "https://example.com/home"
    assert pending[0]["purpose"] == "browser_session_recovery_after_crash"


async def test_page_capture_circuit_blocks_calls_after_infrastructure_threshold(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(
            agent_runtime=SimpleNamespace(max_consecutive_infrastructure_failures=3),
            quality_optimization=SimpleNamespace(pipeline_mode="on"),
        ),
    )
    memory = _memory_with_capture()
    memory.record_tool_health(infrastructure_failure=True)
    memory.record_tool_health(infrastructure_failure=True)
    memory.record_tool_health(infrastructure_failure=True)
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_capture_tool", {"mode": "observe"})
    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, call_next)

    assert called is False
    assert context.result["error_code"] == "INFRASTRUCTURE_UNAVAILABLE"
    assert context.result["failure_kind"] == "infrastructure"
    assert context.result["retryable"] is False


async def test_startup_ad_countdown_is_evidence_insufficient_without_observation() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection_context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection_context,
        lambda: _set_result(
            detection_context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "startup_ad",
                "page_state": "countdown",
                "rule_matching_allowed": True,
                "candidate_actions": [],
            },
        ),
    )

    snapshot = detection_context.result["working_memory"]
    assert snapshot["exploration_frontier"]["pending_actions"] == []
    assert snapshot["degraded_page_count"] == 1
    assert snapshot["failed_page_count"] == 0
    assert snapshot["current_page"] is None
    assert snapshot["terminal_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )


async def test_startup_ad_close_uses_frontier_target_as_selector() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection_context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection_context,
        lambda: _set_result(
            detection_context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "startup_ad",
                "page_state": "ad",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "关闭",
                        "target_keywords": ["关闭"],
                        "risk_level": "low",
                        "purpose": "关闭开屏广告",
                    }
                ],
            },
        ),
    )
    action_id = detection_context.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ][0]["exploration_action_id"]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[],
    )
    context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "exploration_action_id": action_id,
        },
    )

    async def call_next() -> None:
        assert context.arguments["selector"] == "text=关闭"
        assert "clear_obstructions" not in context.arguments
        context.result = {
            "execution_status": "success",
            "page_capture_id": "capture-2",
            "final_url": "https://example.com/home",
            "page_seq": 2,
            "artifact_ids": ["artifact-2"],
        }

    await middleware.process(context, call_next)

    assert context.result["working_memory"]["current_page"]["page_capture_id"] == "capture-2"


async def test_closure_degrades_unfinished_startup_rules_but_keeps_transition() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection_context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection_context,
        lambda: _set_result(
            detection_context,
            {
                "page_capture_id": "capture-1",
                "detection_status": "recognized",
                "page_type": "startup_ad",
                "page_state": "ad",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "text=关闭",
                        "risk_level": "low",
                        "purpose": "关闭开屏广告",
                    }
                ],
            },
        ),
    )
    action_id = detection_context.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ][0]["exploration_action_id"]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_2"},
            {"rule_id": "rule-2", "rule_code": "alioth_4"},
        ],
    )
    context = _context(
        "exploration_closure_tool",
        {
            "reason": "agent_cannot_finish_remaining_rules",
            "action_status": "blocked",
            "exploration_action_ids": [action_id],
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(context, {"execution_status": "accepted"}),
    )

    assert context.result["closure_result"]["rejected_action_ids"] == [action_id]
    assert context.result["degraded_page_capture_ids"] == ["capture-1"]
    assert context.result["failure_isolated"] is True
    snapshot = context.result["working_memory"]
    assert snapshot["terminal_pages"][0]["status"] == "degraded"
    assert snapshot["terminal_pages"][0]["result"]["status_counts"] == {"evidence_insufficient": 2}
    assert (
        snapshot["exploration_frontier"]["pending_actions"][0]["exploration_action_id"] == action_id
    )


async def test_exploration_closure_updates_frontier_and_pending_pages() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="unknown",
        rule_matching_allowed=False,
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "detail_entry",
                "risk_level": "low",
                "expected_next_page_type": "content_detail",
            }
        ],
        next_page_hypotheses=[],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "exploration_closure_tool",
        {
            "reason": "max_pages_reached",
            "action_status": "skipped",
            "close_all_pending_actions": True,
            "abandon_all_pending_pages": True,
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(context, {"execution_status": "accepted"}),
    )

    closure = context.result["closure_result"]
    snapshot = context.result["working_memory"]
    assert closure["closed_action_ids"] == ["exploration-1"]
    assert closure["abandoned_page_capture_ids"] == ["capture-1"]
    assert snapshot["pending_page_count"] == 0
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0


async def test_exploration_closure_rejects_captured_page_before_detection() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "exploration_closure_tool",
        {
            "reason": "agent_returned_early",
            "action_status": "skipped",
            "abandon_all_pending_pages": True,
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(context, {"execution_status": "accepted"}),
    )

    assert context.result["closure_result"]["rejected_page_capture_ids"] == ["capture-1"]
    assert context.result["working_memory"]["current_page"]["page_capture_id"] == "capture-1"

    recapture_context = _context(
        "page_capture_tool",
        {"mode": "observe"},
    )
    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await middleware.process(recapture_context, call_next)

    assert called is False
    assert recapture_context.result["execution_status"] == "blocked"
    assert recapture_context.result["error_code"] == "PAGE_DETECTION_REQUIRED"
    assert recapture_context.result["page_capture_id"] == "capture-1"


async def test_exploration_closure_blocks_assessment_without_run_context() -> None:
    memory = WorkingMemory()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "exploration_closure_tool",
        {
            "reason": "page_type_scope_assessed",
            "page_type_assessments": [
                {
                    "page_type": "home",
                    "status": "evidence_insufficient",
                    "reason_code": "not_observed",
                }
            ],
            "assessment_scope_complete": False,
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(context, {"execution_status": "accepted"}),
    )

    result = context.result["page_type_assessment_result"]
    assert context.result["execution_status"] == "blocked"
    assert result["error_code"] == "PAGE_TYPE_ASSESSMENT_CONTEXT_REQUIRED"
    assert memory.audit_snapshot()["page_type_assessments"] == []


async def test_exploration_closure_persists_contract_assessments_with_page_state() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        page_state="stable",
        rule_matching_allowed=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "exploration_closure_tool",
        {
            "reason": "page_type_scope_assessed",
            "page_type_assessments": [
                {
                    "page_type": "home",
                    "status": "covered",
                    "reason_code": "recognized_with_evidence",
                    "page_capture_ids": ["capture-1"],
                    "evidence_artifact_ids": ["artifact-1"],
                }
            ],
            "assessment_scope_complete": False,
        },
    )

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(
            context,
            lambda: _set_result(context, {"execution_status": "accepted"}),
        )

    assert context.result["page_type_assessment_result"] == {
        "execution_status": "accepted",
        "assessment_count": 1,
        "assessment_scope_complete": False,
        "assessment_scope_page_types": ["home"],
    }
    [assessment] = memory.audit_snapshot()["page_type_assessments"]
    assert assessment["assessment_id"].startswith("page-type-assessment:")
    assert assessment["task_id"] == "task-1"
    assert assessment["agent_run_id"] == "run-1"
    assert assessment["page_type"] == "home"
    assert assessment["status"] == "covered"
    assert assessment["page_states"] == ["stable"]
    assert assessment["catalog_sha256"].startswith("sha256:")


async def test_exploration_closure_cannot_omit_observed_business_page_type_from_scope() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        page_state="stable",
        rule_matching_allowed=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "exploration_closure_tool",
        {
            "reason": "page_type_scope_assessed",
            "page_type_assessments": [
                {
                    "page_type": "content_list",
                    "status": "not_found",
                    "reason_code": "entry_not_found",
                }
            ],
            "assessment_scope_page_types": ["content_list"],
            "assessment_scope_complete": True,
        },
    )

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(
            context,
            lambda: _set_result(context, {"execution_status": "accepted"}),
        )

    result = context.result["page_type_assessment_result"]
    assert context.result["execution_status"] == "blocked"
    assert result["error_code"] == "PAGE_TYPE_ASSESSMENT_CONTRACT_INVALID"
    assert "home" in result["message"]
    assert memory.audit_snapshot()["page_type_assessments"] == []


async def test_exploration_closure_cannot_hide_incomplete_rule_results() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "exploration_closure_tool",
        {
            "reason": "max_pages_reached",
            "action_status": "skipped",
            "page_capture_ids": ["capture-1"],
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(context, {"execution_status": "accepted"}),
    )

    assert context.result["closure_result"]["rejected_page_capture_ids"] == ["capture-1"]
    assert context.result["working_memory"]["current_page"]["status"] == "inspecting"


async def test_blocked_batch_rule_results_are_isolated_as_page_errors() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_2"},
            {"rule_id": "rule-2", "rule_code": "alioth_4"},
        ],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "record_rule_executions_batch_tool",
        {
            "page_capture_id": "capture-1",
            "executions": [
                {"rule_id": "rule-1", "rule_code": "alioth_2"},
                {"rule_id": "rule-2", "rule_code": "alioth_4"},
            ],
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "execution_status": "blocked",
                "page_capture_id": "capture-1",
                "items": [
                    {
                        "status": "blocked",
                        "item_index": 0,
                        "error": {
                            "code": "RULE_EXECUTION_WRITE_REJECTED",
                            "message": "llm/hybrid 判定必须提供 confidence",
                        },
                    },
                    {
                        "status": "blocked",
                        "item_index": 1,
                        "error": {
                            "code": "RULE_EXECUTION_WRITE_REJECTED",
                            "message": "llm/hybrid 判定必须提供 confidence",
                        },
                    },
                ],
            },
        ),
    )

    assert context.result["failure_isolated"] is True
    assert context.result["retryable"] is False
    inspected = context.result["working_memory"]["inspected_pages"][0]
    assert inspected["completed_rule_count"] == 2
    assert inspected["result"]["status_counts"] == {"error": 2}


async def test_repeated_blocked_rule_writes_degrade_remaining_page_rules() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_2"},
            {"rule_id": "rule-2", "rule_code": "alioth_4"},
            {"rule_id": "rule-3", "rule_code": "alioth_5"},
        ],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    arguments = {
        "page_capture_id": "capture-1",
        "executions": [
            {"rule_id": "rule-1", "rule_code": "alioth_2"},
            {"rule_id": "rule-2", "rule_code": "alioth_4"},
        ],
    }

    async def blocked_result(context: SimpleNamespace) -> None:
        context.result = {
            "execution_status": "blocked",
            "page_capture_id": "capture-1",
            "items": [
                {
                    "status": "blocked",
                    "item_index": 0,
                    "error": {"code": "RULE_EXECUTION_WRITE_REJECTED"},
                },
                {
                    "status": "blocked",
                    "item_index": 1,
                    "error": {"code": "RULE_EXECUTION_WRITE_REJECTED"},
                },
            ],
        }

    first = _context("record_rule_executions_batch_tool", arguments)
    await middleware.process(first, lambda: blocked_result(first))
    assert first.result.get("page_degraded") is None

    second = _context("record_rule_executions_batch_tool", arguments)
    await middleware.process(second, lambda: blocked_result(second))

    assert second.result["page_degraded"] is True
    inspected = second.result["working_memory"]["inspected_pages"][0]
    assert inspected["inspection_status"] == "degraded"
    assert inspected["completed_rule_count"] == 3
    assert inspected["failure"]["code"] == "RULE_EXECUTION_WRITE_REJECTED_REPEATED"


async def test_page_capture_result_marks_bound_exploration_action_explored() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    detection_context = _context("page_detection_tool", {"page_capture_id": "capture-1"})
    await middleware.process(
        detection_context,
        lambda: _set_result(
            detection_context,
            {
                "page_capture_id": "capture-1",
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
            },
        ),
    )

    capture_context = _context(
        "page_capture_tool",
        {"mode": "single_trigger", "exploration_action_id": "exploration-1"},
    )
    await middleware.process(
        capture_context,
        lambda: _set_result(
            capture_context,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/detail",
                "title": "Detail",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
            },
        ),
    )

    assert capture_context.arguments["mode"] == "single_trigger"
    assert capture_context.arguments["trigger_action"] == "click"
    assert capture_context.arguments["selector"] == "text=detail_entry"
    frontier = capture_context.result["working_memory"]["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["explored_action_count"] == 1
    assert frontier["explored_actions"][0]["result_page_capture_id"] == "capture-2"


async def test_page_detection_result_with_mismatched_capture_id_is_not_recorded() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-2",
                "detection_status": "recognized",
                "page_type": "home",
                "rule_matching_allowed": True,
            },
        ),
    )

    assert memory.page_detection_result("capture-1") is None


async def test_candidate_rules_rejects_page_capture_outside_current_run() -> None:
    memory = WorkingMemory()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "unknown-capture"},
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is False
    assert context.result["error_code"] == "WORKING_MEMORY_PAGE_NOT_FOUND"


async def test_candidate_rules_requires_page_detection_for_same_capture() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is False
    assert context.result["error_code"] == "PAGE_DETECTION_REQUIRED"


@pytest.mark.parametrize("detection_status", ["ambiguous", "insufficient", "error"])
async def test_candidate_rules_rejects_non_matchable_detection_status(
    detection_status: str,
) -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status=detection_status,
        page_type=None,
        rule_matching_allowed=False,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    assert memory.discovered_page_work_count() == 1
    context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )

    await middleware.process(context, lambda: _set_result(context, {}))

    assert context.result["error_code"] == "PAGE_DETECTION_NOT_RULE_MATCHABLE"
    assert context.result["detection_status"] == detection_status


async def test_candidate_rules_requires_rule_matching_allowed_true() -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=False,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )

    await middleware.process(context, lambda: _set_result(context, {}))

    assert context.result["error_code"] == "PAGE_DETECTION_NOT_RULE_MATCHABLE"
    assert context.result["rule_matching_allowed"] is False


async def test_candidate_rules_requires_detected_page_type_to_match() -> None:
    memory = _memory_with_recognized_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_type": "settings", "page_capture_id": "capture-1"},
    )

    await middleware.process(context, lambda: _set_result(context, {}))

    assert context.result["error_code"] == "PAGE_DETECTION_PAGE_TYPE_MISMATCH"
    assert context.result["requested_page_type"] == "settings"
    assert context.result["detected_page_type"] == "home"


async def test_rule_result_rejects_page_before_candidate_matching() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "record_rule_execution_tool",
        {
            "page_capture_id": "capture-1",
            "rule_id": "rule-1",
            "execution_status": "pass",
        },
    )

    await middleware.process(context, lambda: _set_result(context, {}))

    assert context.result["error_code"] == "PAGE_INSPECTION_NOT_STARTED"


async def test_candidate_and_rule_results_complete_page_automatically() -> None:
    memory = _memory_with_recognized_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    candidate_context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )

    async def _candidate_next() -> None:
        candidate_context.result = {
            "task_id": "task-1",
            "page_capture_id": "capture-1",
            "page_type": "home",
            "rules": [{"rule_id": "rule-1", "rule_code": "alioth_1"}],
        }

    await middleware.process(candidate_context, _candidate_next)
    assert candidate_context.result["working_memory"]["current_page"]["status"] == ("inspecting")

    result_context = _context(
        "record_rule_execution_tool",
        {
            "page_capture_id": "capture-1",
            "rule_id": "rule-1",
            "execution_status": "pass",
            "result_summary": "通过",
            "evidence_artifact_ids": ["artifact-1"],
        },
    )

    async def _result_next() -> None:
        result_context.result = {
            "execution_id": "execution-1",
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
        }

    await middleware.process(result_context, _result_next)

    snapshot = result_context.result["working_memory"]
    assert snapshot["current_page"] is None
    assert snapshot["inspected_page_count"] == 1
    assert snapshot["inspected_pages"][0]["result"]["overall_status"] == "pass"


async def test_supplemental_capture_supersedes_old_unmatchable_capture() -> None:
    memory = _memory_with_capture()
    middleware = PageInspectionMemoryMiddleware(memory)
    first_detection = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    await middleware.process(
        first_detection,
        lambda: _set_result(
            first_detection,
            {
                "page_capture_id": "capture-1",
                "detection_status": "insufficient",
                "page_type": None,
                "rule_matching_allowed": False,
            },
        ),
    )

    second_capture = _context(
        "page_capture_tool",
        {"mode": "observe", "supersedes_page_capture_id": "capture-1"},
    )
    await middleware.process(
        second_capture,
        lambda: _set_result(
            second_capture,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/capture-2",
                "title": "补采页面",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
            },
        ),
    )

    second_detection = _context("page_detection_tool", {"page_capture_id": "capture-2"})
    await middleware.process(
        second_detection,
        lambda: _set_result(
            second_detection,
            {
                "page_capture_id": "capture-2",
                "detection_status": "recognized",
                "page_type": "home",
                "rule_matching_allowed": True,
            },
        ),
    )

    candidate_context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-2"},
    )
    await middleware.process(
        candidate_context,
        lambda: _set_result(
            candidate_context,
            {
                "task_id": "task-1",
                "page_capture_id": "capture-2",
                "page_type": "home",
                "rules": [{"rule_id": "rule-1", "rule_code": "alioth_1"}],
            },
        ),
    )

    result_context = _context(
        "record_rule_execution_tool",
        {
            "page_capture_id": "capture-2",
            "rule_id": "rule-1",
            "execution_status": "pass",
            "evidence_artifact_ids": ["artifact-2"],
        },
    )
    await middleware.process(
        result_context,
        lambda: _set_result(
            result_context,
            {
                "execution_id": "execution-1",
                "rule_id": "rule-1",
                "rule_code": "alioth_1",
                "execution_status": "pass",
                "page_capture_id": "capture-2",
            },
        ),
    )

    snapshot = result_context.result["working_memory"]
    assert snapshot["pending_page_count"] == 0
    assert snapshot["inactive_page_count"] == 1
    assert snapshot["inactive_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["inactive_pages"][0]["status"] == "superseded"


async def test_skipped_result_still_completes_bound_page() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "record_rule_execution_tool",
        {
            "page_capture_id": "capture-1",
            "rule_id": "rule-1",
            "execution_status": "skipped",
            "result_summary": "当前页面无判定对象",
        },
    )

    async def _next() -> None:
        context.result = {
            "execution_id": "execution-1",
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "execution_status": "skipped",
            "page_capture_id": "capture-1",
        }

    await middleware.process(context, _next)

    page = context.result["working_memory"]["inspected_pages"][0]
    assert page["result"]["overall_status"] == "skipped"


async def test_batch_rule_results_complete_page_in_one_tool_call() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_1"},
            {"rule_id": "rule-2", "rule_code": "alioth_2"},
        ],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "record_rule_executions_batch_tool",
        {
            "page_capture_id": "capture-1",
            "executions": [
                {
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "evidence_artifact_ids": ["artifact-1"],
                },
                {
                    "rule_id": "rule-2",
                    "rule_code": "alioth_2",
                    "execution_status": "skipped",
                    "result_summary": "无适用对象",
                },
            ],
        },
    )

    async def _next() -> None:
        context.result = {
            "execution_status": "success",
            "succeeded_count": 2,
            "failed_count": 0,
            "items": [
                {
                    "execution_id": "execution-1",
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "page_capture_id": "capture-1",
                },
                {
                    "execution_id": "execution-2",
                    "rule_id": "rule-2",
                    "rule_code": "alioth_2",
                    "execution_status": "skipped",
                    "page_capture_id": "capture-1",
                },
            ],
        }

    await middleware.process(context, _next)

    snapshot = context.result["working_memory"]
    assert snapshot["current_page"] is None
    assert snapshot["inspected_page_count"] == 1
    assert snapshot["inspected_pages"][0]["completed_rule_count"] == 2


async def test_partial_batch_only_advances_successful_rule_results() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_1"},
            {"rule_id": "rule-2", "rule_code": "alioth_2"},
        ],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "record_rule_executions_batch_tool",
        {
            "page_capture_id": "capture-1",
            "executions": [
                {"rule_id": "rule-1", "rule_code": "alioth_1", "execution_status": "pass"},
                {"rule_id": "rule-2", "rule_code": "alioth_2", "execution_status": "fail"},
            ],
        },
    )

    async def _next() -> None:
        context.result = {
            "execution_status": "partial_success",
            "succeeded_count": 1,
            "failed_count": 1,
            "items": [
                {
                    "status": "blocked",
                    "rule_id": "rule-2",
                    "rule_code": "alioth_2",
                },
                {
                    "status": "ok",
                    "execution_id": "execution-1",
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "page_capture_id": "capture-1",
                },
            ],
        }

    await middleware.process(context, _next)

    page = context.result["working_memory"]["current_page"]
    assert page["completed_rule_count"] == 1
    assert page["candidate_rule_count"] == 2
    assert page["status"] == "inspecting"


def test_worker_applies_complete_working_memory_snapshot_to_run_record() -> None:
    memory = WorkingMemory(step_count=2)
    sink = InMemoryActionTraceSink()
    sink.record(
        ActionTraceEntry(
            step=1,
            tool_name="echo_tool",
            arguments="{}",
            status="ok",
            duration_seconds=0.01,
        )
    )
    record = AgentRunRecord(
        run_id="run-1",
        message="测试任务",
        submitted_at=datetime(2026, 7, 16, tzinfo=UTC),
    )

    worker_module._apply_result(
        record,
        RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=memory,
            trace_sink=sink,
        ),
    )

    assert record.working_memory["schema_version"] == 2
    assert record.working_memory["step_count"] == 2
    assert record.working_memory["action_trace_count"] == 1


async def _set_result(context: SimpleNamespace, value: dict) -> None:
    context.result = value


def _memory_with_capture() -> WorkingMemory:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/home",
        page_title="首页",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    return memory


def _memory_with_recognized_capture() -> WorkingMemory:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    return memory


async def test_blocked_nonretryable_judge_is_terminalized_without_retry() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[
            {
                "rule_id": "rule-bad-contract",
                "rule_code": "alioth_bad_contract",
            }
        ],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "rule_judge_tool",
        {
            "page_capture_id": "capture-1",
            "rule_code": "alioth_bad_contract",
            "evidence_artifact_ids": [],
            "judge_input": {},
        },
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "status": "blocked",
                "retryable": False,
                "rule_code": "alioth_bad_contract",
                "error": {
                    "code": "RULE_ENGINE_NOT_READY",
                    "message": "确定性契约错误",
                },
            },
        ),
    )

    assert context.result["failure_isolated"] is True
    assert context.result["retryable"] is False
    inspected = context.result["working_memory"]["inspected_pages"][0]
    assert inspected["completed_rule_count"] == 1
    assert inspected["result"]["status_counts"] == {"error": 1}
    assert inspected["result"]["rules"][0]["rule_id"] == "rule-bad-contract"

    repeated = _context(
        "rule_judge_tool",
        {
            "page_capture_id": "capture-1",
            "rule_code": "alioth_bad_contract",
        },
    )
    called = False

    async def should_not_repeat() -> None:
        nonlocal called
        called = True

    await middleware.process(repeated, should_not_repeat)

    assert called is False
    assert repeated.result["already_terminal"] is True
    assert repeated.result["execution_status"] == "error"
    assert repeated.result["retryable"] is False


async def test_candidate_capture_plan_marks_rules_evidence_insufficient_without_scroll(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    assert memory.discovered_page_work_count() == 1
    context = _context(
        "candidate_rules_tool",
        {"page_capture_id": "capture-1", "page_type": "video_list"},
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "page_type": "video_list",
                "rules": [{"rule_code": "alioth_video_list"}],
                "capture_plan": {"scroll": True},
            },
        ),
    )

    snapshot = memory.audit_snapshot()
    rule_result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_video_list",
    )

    assert memory.prepare_next_exploration_action(blocking_page_capture_id="capture-1") is None
    assert rule_result is not None
    assert rule_result["execution_status"] == "evidence_insufficient"
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert snapshot["terminal_pages"][0]["failure"]["code"] == (
        "SUPPLEMENTAL_EVIDENCE_DISABLED"
    )
    assert snapshot["exploration_frontier"]["pending_action_count"] == 0
    assert memory.pending_detection_page_capture_id() is None
    assert memory.discovered_page_work_count() == 1


async def test_candidate_capture_plan_marks_interaction_evidence_insufficient(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": "text=跳过",
                "target_keywords": ["跳过", "广告"],
                "risk_level": "low",
                "purpose": "关闭开屏广告",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_capture_id": "capture-1", "page_type": "startup_ad"},
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "page_type": "startup_ad",
                "rules": [
                    {
                        "rule_id": "rule-24",
                        "rule_code": "alioth_24",
                        "name": "启动广告虚假跳过按钮检查",
                        "category": "ad",
                        "input_contract": {
                            "required_capture_roles": ["before", "primary"],
                        },
                    }
                ],
                "capture_plan": {
                    "interaction": True,
                    "required_capture_roles": ["before", "primary"],
                },
            },
        ),
    )

    rule_result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id="rule-24",
        rule_code="alioth_24",
    )
    snapshot = memory.audit_snapshot()
    action = next(
        item
        for item in snapshot["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )

    assert rule_result is not None
    assert rule_result["execution_status"] == "evidence_insufficient"
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert action["mandatory"] is False
    assert action["blocks_page_inspection"] is False
    assert action["purpose"] == "关闭开屏广告"
    assert action["evidence_source_roles"] == []


async def test_candidate_capture_plan_preserves_mandatory_startup_transition(
    monkeypatch,
) -> None:
    """规则匹配不得把既有开屏生命周期动作改写为规则取证动作。"""
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="startup_ad",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": "text=跳过",
                "target_keywords": ["跳过", "广告"],
                "risk_level": "low",
                "purpose": "关闭开屏广告并进入业务页",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=False,
        supersedes_source_page=False,
        requires_source_inspection_complete=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_capture_id": "capture-1", "page_type": "startup_ad"},
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "page_type": "startup_ad",
                "rules": [
                    {
                        "rule_id": "rule-24",
                        "rule_code": "alioth_24",
                        "name": "启动广告虚假跳过按钮检查",
                        "category": "ad",
                        "input_contract": {
                            "required_capture_roles": ["before", "primary"],
                        },
                    }
                ],
                "capture_plan": {
                    "interaction": True,
                    "required_capture_roles": ["before", "primary"],
                },
            },
        ),
    )

    rule_result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id="rule-24",
        rule_code="alioth_24",
    )
    snapshot = memory.audit_snapshot()
    assert rule_result is not None
    assert rule_result["execution_status"] == "evidence_insufficient"
    assert snapshot["terminal_pages"][0]["inspection_status"] == "degraded"
    assert memory.prepare_next_exploration_action(blocking_page_capture_id="capture-1") is None
    action = next(
        item
        for item in snapshot["exploration_frontier"]["pending_actions"]
        if item["exploration_action_id"] == action_id
    )
    assert action["purpose"] == "关闭开屏广告并进入业务页"
    assert action["mandatory"] is True
    assert action["blocks_page_inspection"] is False
    assert action["requires_source_inspection_complete"] is True
    assert action["supersedes_source_page"] is False
    assert action["resumes_page_exploration"] is False
    assert action["evidence_source_roles"] == []


async def test_candidate_capture_plan_does_not_take_over_generic_settings_navigation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "设置",
                "target_keywords": ["settings", "navigation"],
                "risk_level": "low",
                "purpose": "进入设置页面",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_capture_id": "capture-1", "page_type": "profile"},
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-1",
                "page_type": "profile",
                "rules": [
                    {
                        "rule_id": "rule-17",
                        "rule_code": "alioth_17",
                        "name": "广告内容与落地页一致性检查",
                        "category": "ad",
                        "input_contract": {
                            "required_capture_roles": ["source", "primary"],
                        },
                    },
                    {
                        "rule_id": "rule-42",
                        "rule_code": "alioth_42",
                        "name": "失效活动展示同步检查",
                        "category": "copy",
                        "input_contract": {
                            "required_capture_roles": ["source", "primary"],
                        },
                    },
                ],
                "capture_plan": {
                    "interaction": True,
                    "required_capture_roles": ["source", "primary"],
                },
            },
        ),
    )

    assert memory.prepare_next_exploration_action(blocking_page_capture_id="capture-1") is None
    assert memory.mandatory_page_action_capture_defaults(action_id) is None
    assert memory.frontier_action_capture_defaults(action_id) == {
        "action": "click",
        "target": "设置",
    }


async def test_source_restore_capture_keeps_trusted_goto_url() -> None:
    memory = _memory_with_capture()
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tab:nth-of-type(4)",
                "risk_level": "low",
                "purpose": "探索页内 Tab",
            }
        ],
        next_page_hypotheses=[],
    )
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="settings",
        candidate_rules=[],
    )
    restore = memory.prepare_next_exploration_action()
    assert restore is not None
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "page_capture_tool",
        {
            "mode": "goto",
            "exploration_action_id": restore["exploration_action_id"],
        },
    )

    enriched = middleware._enrich_mandatory_page_capture(context, context.arguments)

    assert enriched["mode"] == "goto"
    assert enriched["url"] == restore["target"]
    assert "selector" not in enriched


@pytest.mark.parametrize("supersedes_source_page", [True, False])
async def test_mandatory_browser_back_keeps_trusted_navigation_depth(
    supersedes_source_page: bool,
) -> None:
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="content_list",
        page_state="stable",
        rule_matching_allowed=True,
    )
    entry_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "a[href='/detail']",
                "target_keywords": ["nested_content", "path:/detail"],
                "risk_level": "low",
                "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
                "expected_next_page_type": "content_detail",
                "navigation_depth": 2,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="content_list",
        candidate_rules=[],
    )
    assert memory.mark_exploration_action_selected(entry_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-detail",
        page_url="https://example.com/detail",
        page_title="详情",
        page_seq=2,
        artifact_ids=["artifact-detail"],
        exploration_action_id=entry_action_id,
    )
    memory.remember_page_detection(
        page_capture_id="capture-detail",
        detection_status="recognized",
        page_type="content_detail",
        page_state="stable",
        rule_matching_allowed=True,
    )
    back_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-detail",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "back",
                "target": "browser_history",
                "target_keywords": ["nested_return"],
                "risk_level": "low",
                "purpose": "详情或播放页质检完成后返回来源列表继续探索",
                "expected_next_page_type": "content_list",
                "navigation_depth": 2,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=back_action_id,
        blocks_page_inspection=False,
        supersedes_source_page=supersedes_source_page,
        requires_source_inspection_complete=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "page_capture_tool",
        {
            "mode": "goto",
            "url": "https://malicious.example/",
            "selector": "错误选择器",
            "supersedes_page_capture_id": "untrusted-capture",
            "exploration_action_id": back_action_id,
        },
    )

    enriched = middleware._enrich_mandatory_page_capture(context, context.arguments)

    assert enriched["mode"] == "single_trigger"
    assert enriched["trigger_action"] == "back"
    assert enriched["navigation_depth"] == 2
    assert "url" not in enriched
    assert "selector" not in enriched
    if supersedes_source_page:
        assert enriched["supersedes_page_capture_id"] == "capture-detail"
    else:
        assert "supersedes_page_capture_id" not in enriched


async def test_source_restore_waits_through_obstructions_before_rebinding() -> None:
    """恢复落到开屏和弹窗时只清障，稳定命中原页后才执行旧动作。"""
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        page_state="stable",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )
    original_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "更多",
                "target_keywords": ["更多", "van-icon-arrow"],
                "risk_level": "low",
                "purpose": "查看更多同分类视频",
                "expected_next_page_type": "video_list",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/settings",
        page_title="设置",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="settings",
        candidate_rules=[],
    )
    restore = memory.prepare_next_exploration_action()
    assert restore is not None and restore["action"] == "goto"
    memory.remember_page_capture(
        page_capture_id="capture-3",
        page_url="https://example.com/home",
        page_title="开屏广告",
        page_seq=3,
        artifact_ids=["artifact-3"],
        exploration_action_id=restore["exploration_action_id"],
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    first_detection = _context("page_detection_tool", {"page_capture_id": "capture-3"})
    await middleware.process(
        first_detection,
        lambda: _set_result(
            first_detection,
            {
                "page_capture_id": "capture-3",
                "detection_status": "recognized",
                "page_type": "startup_ad",
                "page_state": "countdown",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": ".startup-ad .close",
                        "target_keywords": ["close_control"],
                        "risk_level": "low",
                        "purpose": "关闭当前遮挡广告并在同一浏览器会话重新采集",
                        "expected_next_page_type": "video_list",
                    }
                ],
            },
        ),
    )
    first_close = memory.prepare_next_exploration_action(blocking_page_capture_id="capture-3")
    assert first_close is not None
    assert first_close["target_keywords"] == ["close_control"]
    pending_after_landing = first_detection.result["working_memory"]["exploration_frontier"][
        "pending_actions"
    ]
    original_after_landing = next(
        action
        for action in pending_after_landing
        if action["exploration_action_id"] == original_action_id
    )
    assert original_after_landing["source_page_capture_id"] == "capture-1"
    assert memory.has_started_page_inspection("capture-3") is False

    memory.remember_page_capture(
        page_capture_id="capture-4",
        page_url="https://example.com/home",
        page_title="视频弹层",
        page_seq=4,
        artifact_ids=["artifact-4"],
        exploration_action_id=first_close["exploration_action_id"],
        supersedes_page_capture_id="capture-3",
    )
    popup_detection = _context("page_detection_tool", {"page_capture_id": "capture-4"})
    await middleware.process(
        popup_detection,
        lambda: _set_result(
            popup_detection,
            {
                "page_capture_id": "capture-4",
                "detection_status": "recognized",
                "page_type": "video_list",
                "page_state": "popup",
                "rule_matching_allowed": True,
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": ".van-overlay:visible",
                        "target_keywords": ["close_control"],
                        "risk_level": "low",
                        "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                        "expected_next_page_type": "video_list",
                    }
                ],
            },
        ),
    )
    second_close = memory.prepare_next_exploration_action(blocking_page_capture_id="capture-4")
    assert second_close is not None
    memory.remember_page_capture(
        page_capture_id="capture-5",
        page_url="https://example.com/home",
        page_title="视频列表",
        page_seq=5,
        artifact_ids=["artifact-5"],
        exploration_action_id=second_close["exploration_action_id"],
        supersedes_page_capture_id="capture-4",
    )
    stable_detection = _context("page_detection_tool", {"page_capture_id": "capture-5"})
    await middleware.process(
        stable_detection,
        lambda: _set_result(
            stable_detection,
            {
                "page_capture_id": "capture-5",
                "detection_status": "recognized",
                "page_type": "video_list",
                "page_state": "stable",
                "rule_matching_allowed": True,
                "candidate_actions": [],
            },
        ),
    )

    next_action = memory.prepare_next_exploration_action()
    assert next_action is not None
    assert next_action["exploration_action_id"] == original_action_id
    assert next_action["source_page_capture_id"] == "capture-5"
    assert memory.has_started_page_inspection("capture-5") is False
    assert memory.discovered_page_work_count() == 2
    restored_capture = next(
        page
        for page in stable_detection.result["working_memory"]["inactive_pages"]
        if page["page_capture_id"] == "capture-5"
    )
    assert restored_capture["abandon_reason"] == "source_restore_transit"
    explored_restore_count = sum(
        action["origin"] == "recovery" and action["action"] == "goto"
        for action in stable_detection.result["working_memory"]["exploration_frontier"][
            "explored_actions"
        ]
    )
    assert explored_restore_count == 1


async def test_bound_click_overrides_untrusted_model_selector() -> None:
    memory = _memory_with_capture()
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="post_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "风骚御姐",
                "risk_level": "low",
                "purpose": "进入内容详情",
            }
        ],
        next_page_hypotheses=[],
    )[0]
    assert memory.require_exploration_action(
        exploration_action_id=action_id,
        blocks_page_inspection=True,
        supersedes_source_page=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "page_capture_tool",
        {
            "mode": "goto",
            "selector": "错误选择器",
            "trigger_action": "scroll",
            "exploration_action_id": action_id,
        },
    )

    enriched = middleware._enrich_mandatory_page_capture(context, context.arguments)

    assert enriched["mode"] == "single_trigger"
    assert enriched["trigger_action"] == "click"
    assert enriched["selector"] == "text=风骚御姐"
    assert "url" not in enriched


async def test_scroll_and_temporal_capture_plan_marks_rule_evidence_insufficient(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="video_list",
        rule_matching_allowed=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    candidate_context = _context(
        "candidate_rules_tool",
        {"page_capture_id": "capture-1", "page_type": "video_list"},
    )
    await middleware.process(
        candidate_context,
        lambda: _set_result(
            candidate_context,
            {
                "page_capture_id": "capture-1",
                "page_type": "video_list",
                "rules": [{"rule_code": "alioth_video"}],
                "capture_plan": {"scroll": True, "temporal": True},
            },
        ),
    )
    rule_result = memory.page_rule_result(
        page_capture_id="capture-1",
        rule_id=None,
        rule_code="alioth_video",
    )
    frontier = memory.audit_snapshot()["exploration_frontier"]

    assert memory.prepare_next_exploration_action(blocking_page_capture_id="capture-1") is None
    assert rule_result is not None
    assert rule_result["execution_status"] == "evidence_insufficient"
    assert frontier["pending_action_count"] == 0


async def test_optimized_model_context_compacts_candidate_rules_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = _memory_with_capture()
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="home",
        candidate_rules=[{"rule_id": "old-rule", "rule_code": "old_rule"}],
    )
    memory.fail_page_inspection(
        page_capture_id="capture-1",
        failure_stage="test",
        failure_code="TEST_FAILURE",
        failure_message="x" * 5000,
        retryable=False,
    )
    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/home?page=2",
        page_title="home 2",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-2",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "candidate_rules_tool",
        {"page_capture_id": "capture-2", "page_type": "home"},
    )

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "page_capture_id": "capture-2",
                "page_type": "home",
                "rules": [
                    {
                        "rule_id": "rule-1",
                        "rule_code": "alioth_home",
                        "category": "text",
                        "description": "x" * 5000,
                        "input_contract": {"schema": "x" * 5000},
                        "judge_config": {"prompt": "x" * 5000},
                        "review_policy": {"detail": "x" * 5000},
                        "target_clients": ["web"],
                        "execution_route": {
                            "strategy": "agent_judge",
                            "judge_tool_name": "rule_judge_tool",
                            "session_dependency": "independent",
                            "rule_engine_ready": False,
                            "route_blocked_reason": None,
                        },
                    }
                ],
                "execution_plan": {"batches": [{"detail": "x" * 5000}]},
                "capture_plan": {"scroll": False},
            },
        ),
    )

    result = context.result
    assert result["model_context_compacted"] is True
    assert result["rule_count"] == 1
    assert "execution_plan" not in result
    assert result["execution_plan_summary"]["batch_count"] == 1
    assert set(result["rules"][0]) == {
        "category",
        "execution_route",
        "rule_code",
        "rule_id",
    }
    assert result["rules"][0]["execution_route"] == {
        "strategy": "agent_judge",
        "judge_tool_name": "rule_judge_tool",
        "session_dependency": "independent",
        "rule_engine_ready": False,
        "route_blocked_reason": None,
    }
    assert "inspected_pages" not in result["working_memory"]
    assert "terminal_pages" not in result["working_memory"]
    assert "inactive_pages" not in result["working_memory"]
    assert len(json.dumps(result, ensure_ascii=False)) < 5000
    assert memory.model_snapshot()["current_page"]["candidate_rule_count"] == 1


async def test_optimized_model_context_compacts_page_capture_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="on")),
    )
    memory = WorkingMemory()
    middleware = PageInspectionMemoryMiddleware(memory)
    context = _context("page_capture_tool", {"mode": "goto"})

    await middleware.process(
        context,
        lambda: _set_result(
            context,
            {
                "execution_status": "success",
                "page_capture_id": "capture-2",
                "final_url": "https://example.com/home",
                "title": "home",
                "page_seq": 2,
                "artifact_ids": ["artifact-2"],
                "visible_text_excerpt": "v" * 5000,
                "ui_summary": {
                    "element_count": 60,
                    "elements": [
                        {
                            "role": "button",
                            "text": "button-" + ("x" * 200),
                            "aria_label": "label-" + ("y" * 200),
                            "selector": f"#button-{index}",
                            "text_metrics": {
                                "scroll_width": 160,
                                "client_width": 80,
                                "scroll_height": 40,
                                "client_height": 20,
                                "horizontal_overflow": True,
                                "vertical_overflow": False,
                                "text_overflow_ellipsis": True,
                                "line_clamp": "1",
                            },
                            "truncation": {
                                "horizontal_overflow": True,
                                "vertical_overflow": False,
                                "text_overflow": "ellipsis",
                                "white_space": "nowrap",
                                "line_clamp": "1",
                                "clipped": True,
                            },
                        }
                        for index in range(60)
                    ],
                    "media": [
                        {
                            "media_index": 0,
                            "tag": "video",
                            "state": {
                                "paused": False,
                                "current_time": 15,
                                "duration": 90,
                                "ready_state": 4,
                            },
                        }
                    ],
                },
            },
        ),
    )

    result = context.result
    assert result["model_context_compacted"] is True
    assert len(result["visible_text_excerpt"]) <= 800
    assert len(result["ui_summary"]["elements"]) == 12
    assert len(result["ui_summary"]["elements"][0]["text"]) <= 100
    assert result["ui_summary"]["elements"][0]["text_metrics"] == {
        "scroll_width": 160,
        "client_width": 80,
        "horizontal_overflow": True,
        "vertical_overflow": False,
        "text_overflow_ellipsis": True,
        "line_clamp": "1",
    }
    assert result["ui_summary"]["elements"][0]["truncation"] == {
        "horizontal_overflow": True,
        "vertical_overflow": False,
        "text_overflow": "ellipsis",
        "white_space": "nowrap",
        "line_clamp": "1",
        "clipped": True,
    }
    assert result["ui_summary"]["media"][0]["state"]["duration"] == 90
    assert "inspected_pages" not in result["working_memory"]
    assert "terminal_pages" not in result["working_memory"]
    assert "inactive_pages" not in result["working_memory"]
    assert len(json.dumps(result, ensure_ascii=False)) < 6000
