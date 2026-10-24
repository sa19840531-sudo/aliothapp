"""规则写回门禁测试：无效调用不得污染去重状态，阻断必须进入审计。"""

import json
from types import SimpleNamespace

from services.api.app.agents.runtime.action_trace import (
    ActionTraceMiddleware,
    InMemoryActionTraceSink,
)
from services.api.app.agents.runtime.business_middlewares import (
    duplicate_submission_dedupe_middleware as dedupe_middleware,
)
from services.api.app.agents.runtime.business_middlewares.evidence_completeness_middleware import (
    EvidenceCompletenessMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.factory import (
    create_default_business_middlewares,
)
from services.api.app.agents.runtime.business_middlewares.page_inspection_memory_middleware import (
    PageInspectionMemoryMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.publish_condition_middleware import (
    PublishConditionMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.rule_judge_handoff_middleware import (
    RuleJudgeHandoffMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.scoped_tool_failure_middleware import (
    ScopedToolFailureMiddleware,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory


def _context(tool_name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(name=tool_name),
        arguments=arguments,
        result=None,
    )


async def test_evidence_guard_returns_structured_retryable_block() -> None:
    middleware = EvidenceCompletenessMiddleware()
    context = _context(
        "record_rule_execution_tool",
        {"rule_id": "rule-1", "execution_status": "pass"},
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is False
    assert context.result == {
        "execution_status": "blocked",
        "error_code": "EVIDENCE_REQUIRED",
        "retryable": True,
        "missing_fields": ["page_capture_id", "evidence_artifact_ids"],
        "message": (
            "execution_status=pass 时必须提供有效的页面采集与证据引用；"
            "请使用同一次 page_capture_tool 返回的 page_capture_id 和 artifact_ids 补齐后重试，"
            "缺少有效证据时改写为 evidence_insufficient。"
        ),
    }


async def test_evidence_guard_allows_conclusion_with_artifact_ids() -> None:
    middleware = EvidenceCompletenessMiddleware()
    context = _context(
        "record_rule_execution_tool",
        {
            "rule_id": "rule-1",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
            "evidence_artifact_ids": ["artifact-1"],
        },
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is True
    assert context.result is None


async def test_evidence_guard_binds_current_page_artifacts_for_batch_write() -> None:
    memory = WorkingMemory()
    memory.remember_page_capture(
        page_capture_id="capture-1",
        page_url="https://example.test/startup",
        page_title="启动广告",
        page_seq=1,
        artifact_ids=["artifact-dom", "artifact-shot"],
    )
    middleware = EvidenceCompletenessMiddleware(working_memory=memory)
    context = _context(
        "record_rule_executions_batch_tool",
        {
            "page_capture_id": "capture-1",
            "executions": [
                {
                    "rule_code": "alioth_2",
                    "execution_status": "pass",
                    "evidence_artifact_ids": [],
                },
                {
                    "rule_code": "alioth_3",
                    "execution_status": "evidence_insufficient",
                    "evidence_artifact_ids": [],
                },
            ],
        },
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is True
    assert context.arguments["executions"][0]["evidence_artifact_ids"] == [
        "artifact-dom",
        "artifact-shot",
    ]
    assert context.arguments["executions"][1]["evidence_artifact_ids"] == []


async def test_evidence_guard_rejects_blank_artifact_id() -> None:
    middleware = EvidenceCompletenessMiddleware()
    context = _context(
        "record_rule_execution_tool",
        {
            "rule_id": "rule-1",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
            "evidence_artifact_ids": [""],
        },
    )

    await middleware.process(context, lambda: _successful_next(context))

    assert context.result["error_code"] == "EVIDENCE_REQUIRED"


async def test_evidence_guard_rejects_invalid_batch_item_before_any_write() -> None:
    middleware = EvidenceCompletenessMiddleware()
    context = _context(
        "record_rule_executions_batch_tool",
        {
            "page_capture_id": "capture-1",
            "executions": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "evidence_artifact_ids": ["artifact-1"],
                },
                {
                    "rule_code": "alioth_2",
                    "execution_status": "fail",
                    "evidence_artifact_ids": [],
                },
            ],
        },
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(context, _next)

    assert called is False
    assert context.result["error_code"] == "EVIDENCE_REQUIRED"
    assert context.result["invalid_items"] == [
        {
            "index": 1,
            "rule_code": "alioth_2",
            "missing_fields": ["evidence_artifact_ids"],
        }
    ]


async def test_dedupe_does_not_remember_downstream_blocked_call() -> None:
    middleware = dedupe_middleware.DuplicateSubmissionDedupeMiddleware(
        mutating_tools={"record_rule_execution_tool"}
    )
    arguments = {"rule_id": "rule-1", "execution_status": "pass"}
    downstream_calls = 0

    async def _blocked_next(context: SimpleNamespace) -> None:
        nonlocal downstream_calls
        downstream_calls += 1
        context.result = {
            "execution_status": "blocked",
            "error_code": "EVIDENCE_REQUIRED",
        }

    first = _context("record_rule_execution_tool", arguments)
    second = _context("record_rule_execution_tool", arguments)
    await middleware.process(first, lambda: _blocked_next(first))
    await middleware.process(second, lambda: _blocked_next(second))

    assert downstream_calls == 2
    assert second.result["error_code"] == "EVIDENCE_REQUIRED"


async def test_dedupe_blocks_only_after_successful_mutation() -> None:
    middleware = dedupe_middleware.DuplicateSubmissionDedupeMiddleware(
        mutating_tools={"record_rule_execution_tool"}
    )
    arguments = {
        "rule_id": "rule-1",
        "execution_status": "pass",
        "page_capture_id": "capture-1",
        "evidence_artifact_ids": ["artifact-1"],
    }

    first = _context("record_rule_execution_tool", arguments)
    await middleware.process(first, lambda: _successful_next(first))

    second = _context("record_rule_execution_tool", arguments)
    await middleware.process(second, lambda: _successful_next(second))

    assert second.result["execution_status"] == "blocked"
    assert second.result["error_code"] == "DUPLICATE_SUBMISSION"


async def test_dedupe_allows_same_page_capture_arguments_for_dynamic_dom() -> None:
    middleware = dedupe_middleware.DuplicateSubmissionDedupeMiddleware(
        mutating_tools={"page_capture_tool"}
    )
    arguments = {
        "mode": "single_trigger",
        "trigger_action": "click",
        "selector": "text=关闭",
    }
    downstream_calls = 0

    async def call_next(context: SimpleNamespace) -> None:
        nonlocal downstream_calls
        downstream_calls += 1
        context.result = {
            "execution_status": "success",
            "page_capture_id": f"capture-{downstream_calls}",
        }

    first = _context("page_capture_tool", arguments)
    second = _context("page_capture_tool", arguments)
    await middleware.process(first, lambda: call_next(first))
    await middleware.process(second, lambda: call_next(second))

    assert downstream_calls == 2
    assert second.result["page_capture_id"] == "capture-2"


async def _successful_next(context: SimpleNamespace) -> None:
    context.result = {"execution_status": "pass", "execution_id": "execution-1"}


async def test_action_trace_marks_structured_block_as_denied() -> None:
    sink = InMemoryActionTraceSink()
    middleware = ActionTraceMiddleware(WorkingMemory(), sink)
    context = _context(
        "record_rule_execution_tool",
        {"rule_id": "rule-1", "execution_status": "pass"},
    )

    async def _next() -> None:
        context.result = {
            "execution_status": "blocked",
            "error_code": "EVIDENCE_REQUIRED",
        }

    await middleware.process(context, _next)

    assert sink.entries[0].status == "denied"
    assert '"error_code": "EVIDENCE_REQUIRED"' in (sink.entries[0].result or "")


async def test_action_trace_redacts_probe_and_fault_pattern_arguments() -> None:
    sink = InMemoryActionTraceSink()
    middleware = ActionTraceMiddleware(WorkingMemory(), sink)
    probe_text = "ALIOTH_RATE_PROBE_run_001"
    fault_pattern = r"ads\.example\.com/"
    context = _context(
        "page_capture_tool",
        {
            "mode": "single_trigger",
            "trigger_action": "submit_probe",
            "probe_text": probe_text,
            "fault_url_pattern": fault_pattern,
        },
    )

    async def _next() -> None:
        context.result = {"execution_status": "success"}

    await middleware.process(context, _next)

    arguments = sink.entries[0].arguments
    assert probe_text not in arguments
    assert fault_pattern not in arguments
    assert arguments.count('"redacted": true') == 2
    assert arguments.count('"sha256"') == 2


async def test_action_trace_marks_framework_wrapped_block_as_denied() -> None:
    sink = InMemoryActionTraceSink()
    middleware = ActionTraceMiddleware(WorkingMemory(), sink)
    context = _context("report_publish_tool", {})

    async def _next() -> None:
        context.result = [
            SimpleNamespace(
                text=json.dumps(
                    {
                        "execution_status": "blocked",
                        "error_code": "REPORT_PUBLISH_CONDITION_UNMET",
                    }
                )
            )
        ]

    await middleware.process(context, _next)

    assert sink.entries[0].status == "denied"
    assert "REPORT_PUBLISH_CONDITION_UNMET" in (sink.entries[0].result or "")


async def test_action_trace_extracts_single_rule_execution_id() -> None:
    sink = InMemoryActionTraceSink()
    middleware = ActionTraceMiddleware(WorkingMemory(), sink)
    context = _context("record_rule_execution_tool", {})

    async def _next() -> None:
        context.result = {
            "execution_status": "pass",
            "execution_id": "11111111-1111-1111-1111-111111111111",
        }

    await middleware.process(context, _next)

    assert sink.entries[0].source_execution_ids == (
        "11111111-1111-1111-1111-111111111111",
    )


async def test_action_trace_extracts_successful_batch_rule_execution_ids() -> None:
    sink = InMemoryActionTraceSink()
    middleware = ActionTraceMiddleware(WorkingMemory(), sink)
    context = _context("record_rule_executions_batch_tool", {})

    async def _next() -> None:
        context.result = [
            SimpleNamespace(
                text=json.dumps(
                    {
                        "execution_status": "partial_success",
                        "items": [
                            {
                                "status": "ok",
                                "execution_id": "11111111-1111-1111-1111-111111111111",
                            },
                            {"status": "error", "execution_id": "ignored"},
                            {
                                "status": "ok",
                                "execution_id": "22222222-2222-2222-2222-222222222222",
                            },
                        ],
                    }
                )
            )
        ]

    await middleware.process(context, _next)

    assert sink.entries[0].source_execution_ids == (
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    )


def test_evidence_guard_runs_before_duplicate_dedupe() -> None:
    middlewares = create_default_business_middlewares(working_memory=WorkingMemory())
    evidence_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, EvidenceCompletenessMiddleware)
    )
    dedupe_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, dedupe_middleware.DuplicateSubmissionDedupeMiddleware)
    )

    assert evidence_index < dedupe_index


def test_rule_judge_handoff_runs_before_evidence_guard() -> None:
    middlewares = create_default_business_middlewares(working_memory=WorkingMemory())
    handoff_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, RuleJudgeHandoffMiddleware)
    )
    evidence_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, EvidenceCompletenessMiddleware)
    )

    assert handoff_index < evidence_index


def test_page_memory_guard_runs_before_publish_condition() -> None:
    """页面记忆阻断不得让发布条件误记为已有成功规则写回。"""
    middlewares = create_default_business_middlewares(working_memory=WorkingMemory())
    page_memory_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, PageInspectionMemoryMiddleware)
    )
    publish_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, PublishConditionMiddleware)
    )

    assert page_memory_index < publish_index


def test_page_memory_wraps_scoped_failure_isolation() -> None:
    """页面记忆必须看到隔离后的结构化失败结果，才能写入页面终态。"""
    middlewares = create_default_business_middlewares(working_memory=WorkingMemory())
    page_memory_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, PageInspectionMemoryMiddleware)
    )
    failure_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, ScopedToolFailureMiddleware)
    )

    assert page_memory_index < failure_index
