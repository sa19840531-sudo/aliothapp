"""任务级运行配置与页面探索策略中间件测试。"""

from types import SimpleNamespace

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.models import RunResult, TerminationReason
from services.api.app.agents.runtime.task_execution_policy import (
    InvalidTaskExecutionPolicyError,
    TaskExecutionPolicyMiddleware,
    resolve_task_runtime_settings,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.core.config import AgentRuntimeSettings, Settings
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore


def _context(tool_name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(name=tool_name),
        arguments=arguments,
        result=None,
    )


@pytest.fixture
def queue() -> RedisAgentRunQueue:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisAgentRunQueue(client, ttl_seconds=3600)


def test_task_loop_guard_overrides_step_and_timeout_defaults() -> None:
    defaults = AgentRuntimeSettings(
        max_steps=30,
        max_iterations=40,
        max_duration_seconds=600,
        stuck_window=3,
    )

    resolved = resolve_task_runtime_settings(
        defaults,
        {
            "max_steps": 12,
            "timeout_minutes": 2,
            "max_ai_calls": 8,
            "no_progress_limit": 5,
            "retry_max_attempts": 2,
            "retry_backoff_seconds": 0.25,
        },
    )

    assert resolved.max_steps == 12
    assert resolved.max_iterations == 8
    assert resolved.max_duration_seconds == 120
    assert resolved.stuck_window == 5
    assert resolved.retry_max_attempts == 2
    assert resolved.retry_backoff_seconds == 0.25
    assert resolved.adaptive_page_budget is False


def test_page_sampling_limit_only_preallocates_budget_and_does_not_define_completion() -> None:
    defaults = AgentRuntimeSettings(
        max_steps=30,
        page_budget_base_steps=12,
        page_budget_steps_per_page=8,
        max_steps_hard_cap=200,
    )

    resolved = resolve_task_runtime_settings(
        defaults,
        {},
        {"mode": "first_n", "max_pages": 10},
    )

    assert resolved.max_steps == 92
    assert resolved.adaptive_page_budget is True


def test_null_max_steps_keeps_adaptive_page_budget_enabled() -> None:
    resolved = resolve_task_runtime_settings(
        AgentRuntimeSettings(
            max_steps=30,
            page_budget_base_steps=12,
            page_budget_steps_per_page=16,
            max_steps_hard_cap=600,
        ),
        {"max_steps": None},
        {"max_pages": 20},
    )

    assert resolved.max_steps == 332
    assert resolved.adaptive_page_budget is True


def test_explicit_step_budget_is_capped_and_disables_adaptive_extension() -> None:
    resolved = resolve_task_runtime_settings(
        AgentRuntimeSettings(max_steps_hard_cap=200),
        {"max_steps": 500, "max_continuation_attempts": 0},
    )

    assert resolved.max_steps == 200
    assert resolved.max_continuation_attempts == 0
    assert resolved.adaptive_page_budget is False


def test_task_loop_guard_rejects_invalid_known_value() -> None:
    with pytest.raises(InvalidTaskExecutionPolicyError, match="max_steps"):
        resolve_task_runtime_settings(AgentRuntimeSettings(), {"max_steps": 0})


async def test_worker_applies_task_runtime_and_page_policy(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    built_runtime_settings = []
    run_arguments = {}

    def _build_agent(*, runtime_settings):
        built_runtime_settings.append(runtime_settings)
        return object()

    async def _fake_run_main_agent(
        agent,
        message,
        *,
        runtime_settings,
        is_cancelled=None,
        sink=None,
        extra_middleware=None,
    ):
        run_arguments["runtime_settings"] = runtime_settings
        run_arguments["extra_middleware"] = extra_middleware
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=InMemoryActionTraceSink(),
            final_text="质检完成",
        )

    monkeypatch.setattr(worker_module, "build_default_main_agent", _build_agent)
    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)

    record = await queue.enqueue(
        "质检任务",
        task_config={"max_depth": 2, "legacy_h5_compatible": True},
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
        loop_guard={"max_steps": 12, "max_duration_seconds": 90},
    )
    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=InMemoryAgentRunStateStore(),
    )

    assert built_runtime_settings[0].max_steps == 12
    assert run_arguments["runtime_settings"].max_duration_seconds == 90
    assert len(run_arguments["extra_middleware"]) == 1
    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.COMPLETED


async def test_worker_fails_closed_for_invalid_task_policy(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    def _unexpected_build() -> None:
        raise AssertionError("无效策略不应构建 Agent")

    monkeypatch.setattr(worker_module, "build_default_main_agent", _unexpected_build)
    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue(
        "质检任务",
        task_config={"legacy_h5_compatible": True},
        loop_guard={"max_steps": 0},
    )

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.ERROR.value
    assert "max_steps" in (loaded.error_message or "")
    assert [event.event_type for event in state_store.events] == ["started", "finished"]


async def test_page_policy_blocks_new_page_after_sampling_limit() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={},
        page_sampling_policy={"mode": "first_n", "max_pages": 1},
        loop_guard={},
    )
    first = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _first_next() -> None:
        first.result = {
            "execution_status": "success",
            "final_url": "https://example.com/home",
        }

    await middleware.process(first, _first_next)

    second = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/detail", "navigation_depth": 1},
    )
    called = False

    async def _second_next() -> None:
        nonlocal called
        called = True

    await middleware.process(second, _second_next)

    assert called is False
    assert second.result["error_code"] == "PAGE_SAMPLING_LIMIT_REACHED"


async def test_page_policy_closes_bound_frontier_action_at_sampling_limit() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/home",
        page_title="首页",
        page_seq=1,
        artifact_ids=["artifact-1"],
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
    middleware = TaskExecutionPolicyMiddleware(
        task_config={},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/home",
        }

    await middleware.process(entry, _entry_next)
    blocked = _context(
        "page_capture_tool",
        {
            "mode": "goto",
            "url": "https://example.com/detail",
            "navigation_depth": 1,
            "exploration_action_id": "exploration-1",
        },
    )

    await middleware.process(blocked, lambda: _unexpected_call())

    frontier = memory.model_snapshot()["exploration_frontier"]
    assert blocked.result["error_code"] == "PAGE_SAMPLING_LIMIT_REACHED"
    assert frontier["pending_action_count"] == 0
    assert frontier["inactive_actions"][0]["status"] == "skipped"
    assert frontier["inactive_actions"][0]["failure_reason"] == ("page_sampling_limit_reached")


async def test_page_policy_enforces_explicit_navigation_depth() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={},
        loop_guard={"max_depth": 3},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/home",
        }

    await middleware.process(entry, _entry_next)

    missing_depth = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/detail"},
    )
    await middleware.process(missing_depth, lambda: _unexpected_call())
    assert missing_depth.result["error_code"] == "NAVIGATION_DEPTH_REQUIRED"

    too_deep = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/deep", "navigation_depth": 3},
    )
    await middleware.process(too_deep, lambda: _unexpected_call())
    assert too_deep.result["error_code"] == "NAVIGATION_DEPTH_LIMIT_REACHED"


async def test_page_policy_blocks_declared_click_navigation_after_sampling_limit() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/home",
        }

    await middleware.process(entry, _entry_next)

    click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "a.detail",
            "navigation_depth": 1,
        },
    )
    await middleware.process(click, lambda: _unexpected_call())

    assert click.result["error_code"] == "PAGE_SAMPLING_LIMIT_REACHED"

    depth_middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 0},
        page_sampling_policy={},
        loop_guard={},
    )
    await depth_middleware.process(entry, _entry_next)
    depth_click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "a.detail",
            "navigation_depth": 1,
        },
    )
    await depth_middleware.process(depth_click, lambda: _unexpected_call())

    assert depth_click.result["error_code"] == "NAVIGATION_DEPTH_LIMIT_REACHED"


async def test_page_policy_allows_same_page_click_after_sampling_limit() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/home",
        }

    await middleware.process(entry, _entry_next)

    click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "button.close-modal",
        },
    )
    called = False

    async def _click_next() -> None:
        nonlocal called
        called = True
        click.result = {
            "execution_status": "success",
            "final_url": "https://example.com/home",
        }

    await middleware.process(click, _click_next)

    assert called is True
    assert click.result["execution_status"] == "success"
    assert click.result["final_url"] == "https://example.com/home"


async def test_page_policy_allows_lifecycle_url_reached_before_popup_click() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {"execution_status": "success", "final_url": "https://example.com/"}

    await middleware.process(entry, _entry_next)
    click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "button.close-popup",
        },
    )

    async def _click_next() -> None:
        click.result = {
            "execution_status": "success",
            "final_url": "https://example.com/movie",
            "action_summary": {
                "transition_kinds": ["same_page_interaction"],
                "before_urls": ["https://example.com/movie"],
                "after_urls": ["https://example.com/movie"],
            },
        }

    await middleware.process(click, _click_next)

    assert click.result["execution_status"] == "success"
    assert click.result["final_url"] == "https://example.com/movie"


async def test_page_policy_allows_submit_probe_without_consuming_page_budget() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 0},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/form", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/form",
        }

    await middleware.process(entry, _entry_next)
    probe = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "submit_probe",
            "selector": "textarea",
            "submit_selector": "button.submit",
            "probe_text": "ALIOTH_RATE_PROBE_run_001",
        },
    )
    called = False

    async def _probe_next() -> None:
        nonlocal called
        called = True
        probe.result = {
            "execution_status": "success",
            "final_url": "https://example.com/form",
        }

    await middleware.process(probe, _probe_next)

    assert called is True
    assert probe.result["execution_status"] == "success"
    assert probe.result["final_url"] == "https://example.com/form"


async def test_page_policy_enriches_unique_mandatory_click_before_navigation_guard() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="开屏广告",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/product", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/product",
        }

    await middleware.process(entry, _entry_next)
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="startup_ad",
        candidate_actions=[
            {
                "action": "click",
                "target": "关闭",
                "risk_level": "low",
                "purpose": "关闭开屏广告",
                "navigation_depth": None,
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
    click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "url": "https://example.com/product/",
        },
    )

    async def _click_next() -> None:
        assert click.arguments["exploration_action_id"] == action_id
        assert click.arguments["selector"] == "text=关闭"
        assert "url" not in click.arguments
        click.result = {
            "execution_status": "success",
            "final_url": "https://example.com/product",
        }

    await middleware.process(click, _click_next)

    assert click.result["execution_status"] == "success"


async def test_page_policy_enriches_unique_frontier_click_before_navigation_guard() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="首页",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/product", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/product",
        }

    await middleware.process(entry, _entry_next)
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible",
                "risk_level": "low",
                "purpose": "探索底部导航业务分支",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "url": "https://example.com/product/",
            "navigation_depth": 0,
        },
    )

    async def _click_next() -> None:
        assert click.arguments["exploration_action_id"] == action_id
        assert click.arguments["selector"] == (
            "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
        )
        assert click.arguments["navigation_depth"] == 1
        assert "url" not in click.arguments
        click.result = {
            "execution_status": "success",
            "final_url": "https://example.com/product/video",
        }

    await middleware.process(click, _click_next)

    assert click.result["execution_status"] == "success"


async def test_page_policy_rejects_closure_of_runnable_low_risk_frontier() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="首页",
        page_seq=8,
        artifact_ids=["artifact-1"],
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible",
                "risk_level": "low",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 4},
        page_sampling_policy={},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    closure = _context(
        "exploration_closure_tool",
        {
            "reason": "误把 page_seq=8 当成 max_depth=4",
            "action_status": "blocked",
            "exploration_action_ids": [action_id],
        },
    )

    await middleware.process(closure, lambda: _unexpected_call())

    assert closure.result["error_code"] == "EXPLORATION_CLOSURE_RUNNABLE_FRONTIER"
    pending = memory.model_snapshot()["exploration_frontier"]["pending_actions"]
    assert pending[0]["exploration_action_id"] == action_id


async def test_page_policy_allows_closure_when_page_budget_is_exhausted() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/product",
        page_title="首页",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[
            {
                "action": "click",
                "target": "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible",
                "risk_level": "low",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 4},
        page_sampling_policy={"mode": "first_n", "max_pages": 1},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    closure = _context(
        "exploration_closure_tool",
        {
            "reason": "快速巡检页面预算已耗尽",
            "action_status": "skipped",
            "exploration_action_ids": [action_id],
        },
    )
    called = False

    async def _closure_next() -> None:
        nonlocal called
        called = True
        closure.result = {"execution_status": "success"}

    await middleware.process(closure, _closure_next)

    assert called is True
    assert closure.result["execution_status"] == "success"


async def test_page_policy_blocks_undeclared_click_navigation_after_result() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/home",
        }

    await middleware.process(entry, _entry_next)

    click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "a.detail",
        },
    )
    called = False

    async def _click_next() -> None:
        nonlocal called
        called = True
        click.result = {
            "execution_status": "success",
            "final_url": "https://example.com/detail",
        }

    await middleware.process(click, _click_next)

    assert called is True
    assert click.result["error_code"] == "PAGE_SAMPLING_LIMIT_REACHED"


async def test_page_policy_enriches_bound_browser_back_action() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-list",
        page_url="https://example.com/list",
        page_title="列表",
        page_seq=1,
        artifact_ids=["artifact-list"],
    )
    detail_action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-list",
        page_type="content_list",
        candidate_actions=[
            {
                "action": "click",
                "target": "a[href='/detail']",
                "target_keywords": ["internal_navigation", "path:/detail"],
                "risk_level": "low",
                "purpose": "进入同源详情页继续质检",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    memory.mark_exploration_action_selected(detail_action_id)
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/detail",
        page_title="详情",
        page_seq=2,
        artifact_ids=["artifact-1"],
        exploration_action_id=detail_action_id,
        supersedes_page_capture_id="capture-list",
    )
    action_id = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="content_detail",
        candidate_actions=[
            {
                "action": "back",
                "target": "browser_history",
                "target_keywords": ["nested_return"],
                "risk_level": "low",
                "purpose": "详情或播放页质检完成后返回来源列表继续探索",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )[0]
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 3},
        page_sampling_policy={},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    context = _context(
        "page_capture_tool",
        {
            "mode": "observe",
            "selector": ".wrong",
            "exploration_action_id": action_id,
        },
    )

    async def call_next() -> None:
        context.result = {
            "execution_status": "success",
            "final_url": "https://example.com/list",
        }

    await middleware.process(context, call_next)

    assert context.arguments["mode"] == "single_trigger"
    assert context.arguments["trigger_action"] == "back"
    assert "selector" not in context.arguments
    assert context.arguments["navigation_depth"] == 1
    assert context.arguments["supersedes_page_capture_id"] == "capture-1"


async def test_page_policy_counts_same_url_spa_captures_against_page_budget() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/movie",
        page_title="视频",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_actions=[
            {
                "action": "click",
                "target": ".van-tab:nth-of-type(4)",
                "target_keywords": ["page_tab_navigation", "home"],
                "risk_level": "low",
                "expected_next_page_type": "home",
                "navigation_depth": 1,
            }
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="video_list",
        candidate_rules=[],
    )
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/movie", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {
            "execution_status": "success",
            "final_url": "https://example.com/movie",
        }

    await middleware.process(entry, _entry_next)

    click = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": ".van-tab:nth-of-type(4)",
            "exploration_action_id": action_ids[0],
            "navigation_depth": 1,
        },
    )
    await middleware.process(click, lambda: _unexpected_call())

    frontier = memory.model_snapshot()["exploration_frontier"]
    assert click.result["error_code"] == "PAGE_SAMPLING_LIMIT_REACHED"
    assert frontier["inactive_actions"][0]["failure_reason"] == "page_sampling_limit_reached"


async def test_page_policy_blocks_observe_with_new_url_after_sampling_limit() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {"execution_status": "success", "final_url": "https://example.com/home"}

    await middleware.process(entry, _entry_next)

    observe = _context(
        "page_capture_tool",
        {"mode": "observe", "url": "https://example.com/detail", "navigation_depth": 1},
    )
    await middleware.process(observe, lambda: _unexpected_call())

    assert observe.result["error_code"] == "PAGE_SAMPLING_LIMIT_REACHED"


async def test_page_policy_requires_depth_for_non_goto_new_url() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {"execution_status": "success", "final_url": "https://example.com/home"}

    await middleware.process(entry, _entry_next)

    hover = _context(
        "page_capture_tool",
        {"mode": "single_trigger", "trigger_action": "hover", "url": "https://example.com/detail"},
    )
    await middleware.process(hover, lambda: _unexpected_call())

    assert hover.result["error_code"] == "NAVIGATION_DEPTH_REQUIRED"


async def test_page_policy_enforces_page_type_include_and_exclude_lists() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={},
        page_sampling_policy={
            "include_page_types": ["home", "content_detail"],
            "exclude_page_types": ["content_detail"],
        },
        loop_guard={},
    )

    excluded = _context("candidate_rules_tool", {"page_type": "content_detail"})
    await middleware.process(excluded, lambda: _unexpected_call())
    assert excluded.result["error_code"] == "PAGE_TYPE_EXCLUDED_BY_SAMPLING"

    outside = _context("candidate_rules_tool", {"page_type": "profile"})
    await middleware.process(outside, lambda: _unexpected_call())
    assert outside.result["error_code"] == "PAGE_TYPE_NOT_INCLUDED_BY_SAMPLING"


def test_page_policy_prioritizes_local_entry_covered_by_ruleset() -> None:
    memory = WorkingMemory()
    middleware = TaskExecutionPolicyMiddleware(
        task_config={},
        page_sampling_policy={"rule_target_page_types": ["settings"]},
        loop_guard={},
    )
    middleware.bind_working_memory(memory)
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com/mine",
        page_title="个人中心",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="recognized",
        page_type="profile",
        rule_matching_allowed=True,
    )
    action_ids = memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_actions=[
            {
                "action": "click",
                "target": "积分任务",
                "target_keywords": ["活动"],
                "risk_level": "low",
                "purpose": "进入活动页",
                "expected_next_page_type": "activity",
            },
            {
                "action": "click",
                "target": "系统设置",
                "target_keywords": ["设置"],
                "risk_level": "low",
                "purpose": "进入设置页",
                "expected_next_page_type": "settings",
            },
        ],
        next_page_hypotheses=[],
    )
    memory.start_page_inspection(
        page_capture_id="capture-1",
        page_type="profile",
        candidate_rules=[],
    )

    selected = memory.prepare_next_exploration_action()

    assert selected is not None
    assert selected["exploration_action_id"] == action_ids[1]
    assert selected["expected_next_page_type"] == "settings"


async def _unexpected_call() -> None:
    raise AssertionError("被策略阻断的 Tool 不应继续执行")
