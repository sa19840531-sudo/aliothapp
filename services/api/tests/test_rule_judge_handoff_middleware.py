"""规则判定结果到写回 Tool 的参数交接测试。"""

from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.business_middlewares.rule_judge_handoff_middleware import (
    RuleJudgeHandoffMiddleware,
)


def _context(tool_name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(name=tool_name),
        arguments=arguments,
        result=None,
    )


async def test_single_write_inherits_rule_judge_evidence() -> None:
    middleware = RuleJudgeHandoffMiddleware()
    judge_context = _context(
        "rule_judge_tool",
        {
            "rule_code": "alioth_6",
            "page_capture_id": "capture-1",
            "evidence_artifact_ids": ["artifact-1"],
            "judge_input": {},
        },
    )

    async def _judge_next() -> None:
        judge_context.result = _judge_result()

    await middleware.process(judge_context, _judge_next)

    record_context = _context(
        "record_rule_execution_tool",
        {
            "rule_code": "alioth_6",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
            "severity": "high",
        },
    )
    received_arguments: dict = {}

    async def _record_next() -> None:
        received_arguments.update(record_context.arguments)
        record_context.result = {
            "execution_id": "execution-1",
            "rule_id": "rule-6",
            "rule_code": "alioth_6",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
        }

    await middleware.process(record_context, _record_next)

    assert received_arguments["rule_id"] == "rule-6"
    assert received_arguments["observation_packet_id"] == "packet-1"
    assert received_arguments["evidence_artifact_ids"] == ["artifact-1"]
    assert received_arguments["judge_strategy"] == "rule_engine"
    assert received_arguments["judge_input"] == {"visible_text": "页面正常"}
    assert received_arguments["judge_output"] == {"assertion": {"passed": True}}


async def test_single_write_uses_trusted_rule_judge_values() -> None:
    middleware = RuleJudgeHandoffMiddleware()
    judge_context = _context("rule_judge_tool", {})

    async def _judge_next() -> None:
        judge_context.result = _judge_result()

    await middleware.process(judge_context, _judge_next)

    record_context = _context(
        "record_rule_execution_tool",
        {
            "rule_code": "alioth_6",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
            "evidence_artifact_ids": ["artifact-other"],
        },
    )
    received_arguments: dict = {}

    async def _record_next() -> None:
        received_arguments.update(record_context.arguments)

    await middleware.process(record_context, _record_next)

    assert received_arguments["evidence_artifact_ids"] == ["artifact-1"]


async def test_single_write_blocks_conflicting_rule_identity() -> None:
    middleware = RuleJudgeHandoffMiddleware()
    judge_context = _context("rule_judge_tool", {})

    async def _judge_next() -> None:
        judge_context.result = _judge_result()

    await middleware.process(judge_context, _judge_next)

    record_context = _context(
        "record_rule_execution_tool",
        {
            "rule_id": "rule-other",
            "rule_code": "alioth_6",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
        },
    )
    called = False

    async def _record_next() -> None:
        nonlocal called
        called = True

    await middleware.process(record_context, _record_next)

    assert called is False
    assert record_context.result["error_code"] == "RULE_JUDGE_HANDOFF_CONFLICT"
    assert record_context.result["conflicting_fields"] == ["rule_id"]


@pytest.mark.parametrize(
    ("page_capture_id", "rule_code"),
    [
        ("capture-other", "alioth_6"),
        ("capture-1", "alioth_other"),
    ],
)
async def test_write_does_not_inherit_other_page_or_rule_evidence(
    page_capture_id: str,
    rule_code: str,
) -> None:
    middleware = RuleJudgeHandoffMiddleware()
    judge_context = _context("rule_judge_tool", {})

    async def _judge_next() -> None:
        judge_context.result = _judge_result()

    await middleware.process(judge_context, _judge_next)

    record_context = _context(
        "record_rule_execution_tool",
        {
            "rule_code": rule_code,
            "execution_status": "pass",
            "page_capture_id": page_capture_id,
        },
    )
    received_arguments: dict = {}

    async def _record_next() -> None:
        received_arguments.update(record_context.arguments)

    await middleware.process(record_context, _record_next)

    assert "evidence_artifact_ids" not in received_arguments


async def test_batch_write_inherits_each_rule_judge_result() -> None:
    middleware = RuleJudgeHandoffMiddleware()
    judge_context = _context("rule_judge_batch_tool", {})

    async def _judge_next() -> None:
        judge_context.result = {
            "execution_status": "success",
            "page_capture_id": "capture-1",
            "succeeded_count": 2,
            "failed_count": 0,
            "items": [
                {"status": "ok", **_judge_result()},
                {
                    "status": "ok",
                    **{
                        **_judge_result(),
                        "rule_id": "rule-14",
                        "rule_code": "alioth_14",
                    },
                },
            ],
        }

    await middleware.process(judge_context, _judge_next)

    record_context = _context(
        "record_rule_executions_batch_tool",
        {
            "page_capture_id": "capture-1",
            "executions": [
                {
                    "rule_code": "alioth_6",
                    "execution_status": "pass",
                    "judge_input": {"visible_text": "模型重组的摘要"},
                },
                {"rule_code": "alioth_14", "execution_status": "pass"},
            ],
        },
    )
    received_arguments: dict = {}

    async def _record_next() -> None:
        received_arguments.update(record_context.arguments)
        record_context.result = {
            "execution_status": "success",
            "page_capture_id": "capture-1",
            "succeeded_count": 2,
            "failed_count": 0,
            "items": [],
        }

    await middleware.process(record_context, _record_next)

    executions = received_arguments["executions"]
    assert executions[0]["rule_id"] == "rule-6"
    assert executions[0]["evidence_artifact_ids"] == ["artifact-1"]
    assert executions[0]["judge_input"] == {"visible_text": "页面正常"}
    assert executions[1]["rule_id"] == "rule-14"
    assert executions[1]["evidence_artifact_ids"] == ["artifact-1"]


def _judge_result() -> dict:
    return {
        "rule_id": "rule-6",
        "rule_code": "alioth_6",
        "executor_type": "rule_engine",
        "judge_strategy": "rule_engine",
        "execution_status": "pass",
        "confidence": 1.0,
        "result_summary": "未发现技术错误直出",
        "problem_desc": None,
        "page_capture_id": "capture-1",
        "observation_packet_id": "packet-1",
        "evidence_artifact_ids": ["artifact-1"],
        "judge_input": {"visible_text": "页面正常"},
        "judge_output": {"assertion": {"passed": True}},
    }
