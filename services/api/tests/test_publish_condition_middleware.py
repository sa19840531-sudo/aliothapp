"""发布条件中间件测试：报告始终允许，真正发布动作才受门禁约束。"""

from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.business_middlewares.publish_condition_middleware import (
    PublishConditionMiddleware,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory


def _context(tool_name: str) -> SimpleNamespace:
    return SimpleNamespace(function=SimpleNamespace(name=tool_name), result=None)


@pytest.mark.asyncio
async def test_report_generation_is_always_allowed_before_required_tool_succeeds() -> None:
    middleware = PublishConditionMiddleware(WorkingMemory())
    context = _context("report_generation_tool")
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is True
    assert context.result is None


@pytest.mark.asyncio
async def test_report_generation_is_blocked_while_page_work_is_pending() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.test/product",
        page_title="待识别页面",
        page_seq=1,
        artifact_ids=["artifact-1"],
    )
    middleware = PublishConditionMiddleware(memory)
    context = _context("report_generation_tool")
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is False
    assert context.result["error_code"] == "PAGE_EXPLORATION_PENDING"


@pytest.mark.asyncio
async def test_final_publish_is_blocked_before_required_tool_succeeds() -> None:
    middleware = PublishConditionMiddleware(
        WorkingMemory(),
        publish_tool="final_publish_tool",
    )
    publish_context = _context("final_publish_tool")
    publish_called = False

    async def _publish_next() -> None:
        nonlocal publish_called
        publish_called = True

    await middleware.process(publish_context, _publish_next)

    assert publish_called is False
    assert publish_context.result == {
        "execution_status": "blocked",
        "error_code": "REPORT_PUBLISH_CONDITION_UNMET",
        "retryable": False,
        "missing_fields": ["rule_execution"],
        "message": "当前运行尚未记录任何规则执行结果，暂不允许发布报告。",
    }


@pytest.mark.asyncio
async def test_final_publish_is_allowed_after_required_tool_succeeds() -> None:
    middleware = PublishConditionMiddleware(
        WorkingMemory(),
        publish_tool="final_publish_tool",
    )
    required_context = _context("record_rule_execution_tool")
    publish_context = _context("final_publish_tool")
    publish_called = False

    async def _required_next() -> None:
        return

    async def _publish_next() -> None:
        nonlocal publish_called
        publish_called = True

    await middleware.process(required_context, _required_next)
    await middleware.process(publish_context, _publish_next)

    assert publish_called is True
    assert publish_context.result is None


@pytest.mark.asyncio
async def test_final_publish_remains_blocked_if_required_tool_raises() -> None:
    middleware = PublishConditionMiddleware(
        WorkingMemory(),
        publish_tool="final_publish_tool",
    )
    required_context = _context("record_rule_execution_tool")
    publish_context = _context("final_publish_tool")
    publish_called = False

    async def _required_next() -> None:
        raise RuntimeError("boom")

    async def _publish_next() -> None:
        nonlocal publish_called
        publish_called = True

    with pytest.raises(RuntimeError, match="boom"):
        await middleware.process(required_context, _required_next)
    await middleware.process(publish_context, _publish_next)

    assert publish_called is False
    assert publish_context.result["execution_status"] == "blocked"
    assert publish_context.result["error_code"] == "REPORT_PUBLISH_CONDITION_UNMET"


@pytest.mark.asyncio
async def test_final_publish_is_allowed_after_batch_write_succeeds() -> None:
    middleware = PublishConditionMiddleware(
        WorkingMemory(),
        publish_tool="final_publish_tool",
    )
    required_context = _context("record_rule_executions_batch_tool")
    publish_context = _context("final_publish_tool")
    publish_called = False

    async def _required_next() -> None:
        required_context.result = {
            "execution_status": "success",
            "succeeded_count": 2,
            "failed_count": 0,
        }

    async def _publish_next() -> None:
        nonlocal publish_called
        publish_called = True

    await middleware.process(required_context, _required_next)
    await middleware.process(publish_context, _publish_next)

    assert publish_called is True
