"""页面与规则局部工具异常隔离测试。"""

from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.action_trace import (
    ActionTraceMiddleware,
    InMemoryActionTraceSink,
)
from services.api.app.agents.runtime.business_middlewares.page_inspection_memory_middleware import (
    PageInspectionMemoryMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.scoped_tool_failure_middleware import (
    ScopedToolFailureMiddleware,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory


@pytest.fixture(autouse=True)
def _use_full_working_memory_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件断言完整审计快照，不受部署环境的模型压缩开关影响。"""

    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "page_inspection_memory_middleware.get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(pipeline_mode="off")
        ),
    )


def _context(tool_name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(name=tool_name),
        arguments=arguments,
        result=None,
        metadata={},
    )


async def test_page_capture_exception_closes_entry_action() -> None:
    memory = WorkingMemory()
    page_memory = PageInspectionMemoryMiddleware(memory)
    failure_isolation = ScopedToolFailureMiddleware()
    context = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com", "navigation_depth": 0},
    )

    async def _failing_tool() -> None:
        raise RuntimeError("browser crashed")

    async def _isolated_call() -> None:
        await failure_isolation.process(context, _failing_tool)

    await page_memory.process(context, _isolated_call)

    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert context.result["failure_scope"] == "page"
    assert frontier["pending_action_count"] == 0
    assert frontier["inactive_actions"][0]["origin"] == "entry"
    assert frontier["inactive_actions"][0]["failure_code"] == "PAGE_CAPTURE_FAILED"


async def test_page_detection_exception_terminalizes_only_current_page() -> None:
    memory = _memory_with_capture()
    page_memory = PageInspectionMemoryMiddleware(memory)
    failure_isolation = ScopedToolFailureMiddleware()
    context = _context("page_detection_tool", {"page_capture_id": "capture-1"})

    async def _failing_tool() -> None:
        raise OSError("detection store unavailable")

    async def _isolated_call() -> None:
        await failure_isolation.process(context, _failing_tool)

    await page_memory.process(context, _isolated_call)

    snapshot = memory.audit_snapshot()
    assert snapshot["failed_page_count"] == 1
    assert snapshot["pending_page_count"] == 0
    assert snapshot["terminal_pages"][0]["failure"]["stage"] == "page_detection"


async def test_non_retryable_page_capture_block_closes_entry_action() -> None:
    memory = WorkingMemory()
    page_memory = PageInspectionMemoryMiddleware(memory)
    context = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com", "navigation_depth": 0},
    )

    async def _blocked_tool() -> None:
        context.result = {
            "execution_status": "blocked",
            "error_code": "PAGE_CAPTURE_ACTION_BLOCKED",
            "error_message": "导航动作不允许",
        }

    await page_memory.process(context, _blocked_tool)

    frontier = memory.audit_snapshot()["exploration_frontier"]
    assert frontier["pending_action_count"] == 0
    assert frontier["inactive_actions"][0]["failure_code"] == ("PAGE_CAPTURE_ACTION_BLOCKED")


async def test_rule_exception_records_error_and_keeps_other_work_runnable() -> None:
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
        candidate_rules=[
            {"rule_id": "rule-1", "rule_code": "alioth_1"},
            {"rule_id": "rule-2", "rule_code": "alioth_2"},
        ],
    )
    memory.remember_exploration_candidates(
        page_capture_id="capture-1",
        page_type="home",
        candidate_actions=[{"action": "click", "target": "下一页"}],
        next_page_hypotheses=[],
    )
    page_memory = PageInspectionMemoryMiddleware(memory)
    failure_isolation = ScopedToolFailureMiddleware()
    context = _context(
        "record_rule_execution_tool",
        {
            "page_capture_id": "capture-1",
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "execution_status": "pass",
        },
    )

    async def _failing_tool() -> None:
        raise OSError("database unavailable")

    async def _isolated_call() -> None:
        await failure_isolation.process(context, _failing_tool)

    await page_memory.process(context, _isolated_call)

    snapshot = context.result["working_memory"]
    assert snapshot["current_page"]["completed_rule_count"] == 1
    assert snapshot["current_page"]["result"]["status_counts"] == {"error": 1}
    assert snapshot["exploration_frontier"]["pending_action_count"] == 1
    assert memory.has_runnable_work() is True


async def test_unknown_tool_exception_is_not_swallowed() -> None:
    middleware = ScopedToolFailureMiddleware()
    context = _context("unknown_tool", {})

    async def _failing_tool() -> None:
        raise RuntimeError("programming error")

    with pytest.raises(RuntimeError, match="programming error"):
        await middleware.process(context, _failing_tool)


async def test_handled_failure_is_recorded_as_error_trace() -> None:
    memory = WorkingMemory()
    sink = InMemoryActionTraceSink()
    trace = ActionTraceMiddleware(memory, sink)
    failure_isolation = ScopedToolFailureMiddleware()
    context = _context("page_capture_tool", {"mode": "goto"})

    async def _failing_tool() -> None:
        raise RuntimeError("browser crashed")

    async def _isolated_call() -> None:
        await failure_isolation.process(context, _failing_tool)

    await trace.process(context, _isolated_call)

    assert sink.entries[0].status == "error"
    assert sink.entries[0].error_message == "browser crashed"
    assert sink.entries[0].result is not None


def _memory_with_capture() -> WorkingMemory:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.com",
        page_title="首页",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    return memory
