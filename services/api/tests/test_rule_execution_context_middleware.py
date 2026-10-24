"""规则执行上下文中间件测试。"""

from types import SimpleNamespace

from services.api.app.agents.runtime.business_middlewares.evidence_completeness_middleware import (
    EvidenceCompletenessMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.rule_execution_context_middleware import (
    RuleExecutionContextMiddleware,
)
from services.api.app.agents.runtime.rule_execution_context import RuleExecutionContext


def _context(tool_name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(name=tool_name),
        arguments=arguments,
        result=None,
    )


async def test_single_llm_write_inherits_candidate_page_evidence() -> None:
    middleware = RuleExecutionContextMiddleware(RuleExecutionContext())
    await _remember_page_and_candidates(middleware)
    record = _context(
        "record_rule_execution_tool",
        {
            "rule_code": "alioth_2",
            "execution_status": "pass",
            "confidence": 0.9,
            "judge_input": {"observation": "页面文案为简体中文"},
            "judge_output": {
                "schema_version": "1.0",
                "conclusion": "pass",
                "reasoning_summary": "未发现语言规范问题",
            },
        },
    )
    received: dict = {}

    async def _next() -> None:
        received.update(record.arguments)

    await middleware.process(record, _next)

    assert received["rule_id"] == "rule-2"
    assert received["page_capture_id"] == "capture-1"
    assert received["judge_strategy"] == "llm"
    assert received["severity"] == "low"
    assert received["evidence_artifact_ids"] == ["dom-1", "text-1", "shot-1"]
    assert received["judge_input"]["observation"] == "页面文案为简体中文"
    assert received["judge_input"]["execution_context"] == {
        "page_capture_id": "capture-1",
        "page_type": "home",
        "evidence_artifact_ids": ["dom-1", "text-1", "shot-1"],
    }


async def test_context_enrichment_runs_before_evidence_validation() -> None:
    context_middleware = RuleExecutionContextMiddleware(RuleExecutionContext())
    evidence_middleware = EvidenceCompletenessMiddleware()
    await _remember_page_and_candidates(context_middleware)
    record = _context(
        "record_rule_execution_tool",
        {
            "rule_code": "alioth_2",
            "execution_status": "pass",
            "confidence": 0.9,
            "judge_input": {"observation": "页面文案为简体中文"},
            "judge_output": {
                "schema_version": "1.0",
                "conclusion": "pass",
                "reasoning_summary": "未发现语言规范问题",
            },
        },
    )
    called = False

    async def _business_tool() -> None:
        nonlocal called
        called = True

    async def _evidence_layer() -> None:
        await evidence_middleware.process(record, _business_tool)

    await context_middleware.process(record, _evidence_layer)

    assert called is True
    assert record.arguments["evidence_artifact_ids"] == ["dom-1", "text-1", "shot-1"]


async def test_batch_write_inherits_context_per_candidate_rule() -> None:
    middleware = RuleExecutionContextMiddleware(RuleExecutionContext())
    await _remember_page_and_candidates(middleware)
    record = _context(
        "record_rule_executions_batch_tool",
        {
            "page_capture_id": "capture-1",
            "executions": [
                {
                    "rule_code": "alioth_2",
                    "execution_status": "evidence_insufficient",
                    "result_summary": "需要补充字体渲染证据",
                }
            ],
        },
    )
    received: dict = {}

    async def _next() -> None:
        received.update(record.arguments)

    await middleware.process(record, _next)

    item = received["executions"][0]
    assert item["rule_id"] == "rule-2"
    assert item["judge_strategy"] == "llm"
    assert item["evidence_artifact_ids"] == ["dom-1", "text-1", "shot-1"]
    assert item["judge_input"]["execution_context"]["page_type"] == "home"

async def test_batch_judge_inherits_candidate_page_evidence() -> None:
    middleware = RuleExecutionContextMiddleware(RuleExecutionContext())
    await _remember_page_and_candidates(middleware)
    judge = _context(
        "rule_judge_batch_tool",
        {
            "page_capture_id": "capture-1",
            "rules": [{"rule_code": "alioth_2"}],
        },
    )
    received: dict = {}

    async def _next() -> None:
        received.update(judge.arguments)

    await middleware.process(judge, _next)

    assert received["evidence_artifact_ids"] == ["dom-1", "text-1", "shot-1"]
    assert received["rules"][0]["judge_input"]["execution_context"] == {
        "page_capture_id": "capture-1",
        "page_type": "home",
        "evidence_artifact_ids": ["dom-1", "text-1", "shot-1"],
    }


async def test_non_candidate_rule_does_not_inherit_page_evidence() -> None:
    middleware = RuleExecutionContextMiddleware(RuleExecutionContext())
    await _remember_page_and_candidates(middleware)
    record = _context(
        "record_rule_execution_tool",
        {
            "rule_code": "alioth_999",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
        },
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await middleware.process(record, _next)

    assert called is False
    assert record.result["error_code"] == "RULE_NOT_IN_PAGE_CANDIDATES"


async def _remember_page_and_candidates(
    middleware: RuleExecutionContextMiddleware,
) -> None:
    capture = _context("page_capture_tool", {"mode": "goto"})

    async def _capture_next() -> None:
        capture.result = {
            "execution_status": "success",
            "page_capture_id": "capture-1",
            "artifact_ids": ["dom-1", "text-1", "shot-1"],
        }

    await middleware.process(capture, _capture_next)

    candidates = _context(
        "candidate_rules_tool",
        {"page_capture_id": "capture-1", "page_type": "home"},
    )

    async def _candidate_next() -> None:
        candidates.result = {
            "execution_status": "success",
            "page_capture_id": "capture-1",
            "page_type": "home",
            "rules": [
                {
                    "rule_id": "rule-2",
                    "rule_code": "alioth_2",
                    "executor_type": "llm",
                    "severity": "low",
                }
            ],
        }

    await middleware.process(candidates, _candidate_next)
    assert candidates.result["rule_execution_context"]["page_capture_id"] == "capture-1"
