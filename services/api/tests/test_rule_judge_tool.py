import asyncio
from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.inspection_work_item_context import (
    bind_inspection_judgement_sink,
)
from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools import rule_judge_tools as rule_judge_tools_module
from services.api.app.agents.tools.registry import PermissionLevel, default_registry
from services.api.app.agents.tools.rule_judge_tools import (
    RuleJudgeBatchItem,
    judge_and_record_rule_for_current_run,
    judge_and_record_rules_for_current_run,
    judge_rule_for_current_run,
    judge_rules_for_current_run,
)
from services.api.app.application.rule_judge import (
    RuleJudgeNotConfiguredError,
    RuleJudgeRequest,
    RuleJudgeResult,
    RuleJudgeValidationError,
)


class FakeRuleJudgeService:
    def __init__(self) -> None:
        self.requests: list[RuleJudgeRequest] = []

    async def judge(self, request: RuleJudgeRequest) -> RuleJudgeResult:
        self.requests.append(request)
        return RuleJudgeResult(
            rule_id="rule-6",
            rule_code=request.rule_code,
            executor_type="rule_engine",
            judge_strategy="rule_engine",
            execution_status="pass",
            confidence=1.0,
            result_summary="未发现技术错误直出",
            problem_desc=None,
            page_capture_id=request.page_capture_id,
            evidence_artifact_ids=request.evidence_artifact_ids,
            judge_input={"visible_text": "页面正常"},
            judge_output={"assertion": {"passed": True}},
        )


class FakeBatchRuleJudgeService(FakeRuleJudgeService):
    def __init__(self, *, candidate_status: str = "pass") -> None:
        super().__init__()
        self.candidate_status = candidate_status
        self.batch_requests: list[list[RuleJudgeRequest]] = []

    async def judge_many(self, requests: list[RuleJudgeRequest]) -> list[RuleJudgeResult]:
        self.batch_requests.append(requests)
        return [
            RuleJudgeResult(
                rule_id=f"rule-{index}",
                rule_code=request.rule_code,
                executor_type="semantic",
                judge_strategy="semantic",
                execution_status=self.candidate_status,
                confidence=0.99,
                result_summary="候选批量结论",
                problem_desc=None,
                page_capture_id=request.page_capture_id,
                evidence_artifact_ids=request.evidence_artifact_ids,
                judge_input=request.judge_input,
                judge_output={"conclusion": self.candidate_status},
            )
            for index, request in enumerate(requests, start=1)
        ]


class FakeShadowService:
    def __init__(self) -> None:
        self.comparisons = []

    async def record(self, comparison):
        self.comparisons.append(comparison)
        return f"comparison-{len(self.comparisons)}"


class BlockedRuleJudgeService:
    async def judge(self, request: RuleJudgeRequest) -> RuleJudgeResult:
        raise RuleJudgeNotConfiguredError(f"规则 {request.rule_code} 的 Handler 尚未注册")


class InvalidEvidenceRuleJudgeService:
    async def judge(self, request: RuleJudgeRequest) -> RuleJudgeResult:
        raise RuleJudgeValidationError(f"规则 {request.rule_code} 缺少当前页面 visible_text 证据")


class UnavailableRuleJudgeService:
    def __init__(self) -> None:
        self.requests: list[RuleJudgeRequest] = []

    async def judge(self, request: RuleJudgeRequest) -> RuleJudgeResult:
        self.requests.append(request)
        raise OSError("judge database unavailable")


class BrokenSemanticRuleJudgeService:
    async def judge(self, request: RuleJudgeRequest) -> RuleJudgeResult:
        raise ValueError("semantic output invalid")


class SlowSemanticRuleJudgeService(FakeBatchRuleJudgeService):
    async def judge_many(self, requests: list[RuleJudgeRequest]) -> list[RuleJudgeResult]:
        await asyncio.sleep(0.05)
        return await super().judge_many(requests)


async def _record_judged_batch(*, page_capture_id, executions, service=None):
    del service
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


async def test_rule_judge_tool_uses_bound_task_context() -> None:
    service = FakeRuleJudgeService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rule_for_current_run(
            rule_code="alioth_6",
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            judge_input={"visible_text": "页面正常"},
            evidence_capture_roles={"primary": "capture-1"},
            service=service,
        )

    request = service.requests[0]
    assert request.task_id == "task-1"
    assert request.agent_run_id == "run-1"
    assert request.rule_code == "alioth_6"
    assert request.evidence_artifact_ids == ("artifact-1",)
    assert request.evidence_capture_roles == {"primary": "capture-1"}
    assert result["judge_strategy"] == "rule_engine"
    assert result["execution_status"] == "pass"


async def test_rule_judge_tool_returns_structured_block_when_route_is_not_ready() -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rule_for_current_run(
            rule_code="alioth_20",
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            judge_input={"visible_text": "弹窗"},
            service=BlockedRuleJudgeService(),
        )

    assert result == {
        "status": "blocked",
        "execution_status": "blocked",
        "rule_code": "alioth_20",
        "page_capture_id": "capture-1",
        "retryable": False,
        "error": {
            "code": "RULE_ENGINE_NOT_READY",
            "message": "规则 alioth_20 的 Handler 尚未注册",
        },
    }


async def test_rule_judge_tool_returns_nonretryable_block_for_invalid_evidence() -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rule_for_current_run(
            rule_code="alioth_6",
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            judge_input={},
            service=InvalidEvidenceRuleJudgeService(),
        )

    assert result == {
        "status": "blocked",
        "execution_status": "blocked",
        "rule_code": "alioth_6",
        "page_capture_id": "capture-1",
        "retryable": False,
        "error": {
            "code": "TRUSTED_EVIDENCE_INVALID",
            "message": "规则 alioth_6 缺少当前页面 visible_text 证据",
        },
    }


async def test_rule_judge_batch_executes_rules_with_shared_evidence() -> None:
    service = FakeRuleJudgeService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                RuleJudgeBatchItem(rule_code="alioth_6", judge_input={}),
                RuleJudgeBatchItem(rule_code="alioth_14", judge_input={"sale_price": 8}),
            ],
            service=service,
        )

    assert result["execution_status"] == "success"
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0
    assert [request.rule_code for request in service.requests] == ["alioth_6", "alioth_14"]
    assert all(request.page_capture_id == "capture-1" for request in service.requests)
    assert all(request.evidence_artifact_ids == ("artifact-1",) for request in service.requests)


async def test_rule_judge_batch_accepts_framework_dict_items() -> None:
    service = FakeRuleJudgeService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                {"rule_code": "alioth_6", "judge_input": {}},
                {"rule_code": "alioth_14", "judge_input": {"sale_price": 8}},
            ],
            service=service,
        )

    assert result["execution_status"] == "success"
    assert [request.rule_code for request in service.requests] == ["alioth_6", "alioth_14"]


async def test_rule_judge_batch_isolates_invalid_item() -> None:
    service = FakeRuleJudgeService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                {"judge_input": {}},
                {"rule_code": "alioth_6", "judge_input": {}},
            ],
            service=service,
        )

    assert result["execution_status"] == "partial_success"
    assert result["items"][0]["error"]["code"] == "RULE_JUDGE_ITEM_INVALID"
    assert result["items"][1]["status"] == "ok"
    assert [request.rule_code for request in service.requests] == ["alioth_6"]


async def test_rule_judge_batch_defers_after_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rule_judge_tools_module,
        "get_settings",
        lambda: SimpleNamespace(quality_optimization=SimpleNamespace(pipeline_mode="off")),
    )
    service = UnavailableRuleJudgeService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                {"rule_code": "alioth_6", "judge_input": {}},
                {"rule_code": "alioth_14", "judge_input": {}},
            ],
            service=service,
        )

    assert result["succeeded_count"] == 0
    assert result["items"][0]["error"]["code"] == "RULE_JUDGE_UNAVAILABLE"
    assert result["items"][1]["error"]["code"] == "RULE_JUDGE_DEFERRED"
    assert result["items"][1]["deferred"] is True
    assert len(service.requests) == 1


async def test_rule_judge_single_fallback_failure_goes_to_manual_review() -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[{"rule_code": "alioth_semantic", "judge_input": {}}],
            service=BrokenSemanticRuleJudgeService(),
        )

    assert result["items"][0]["execution_status"] == "manual_review"
    assert result["items"][0]["judge_strategy"] is None
    assert "retryable" not in result["items"][0]
    assert result["items"][0]["judge_input"]["semantic_fallback"]["rule_code"] == "alioth_semantic"
    assert result["items"][0]["judge_output"]["schema_version"] == "1.0"
    assert result["items"][0]["judge_output"]["conclusion"] == "manual_review"
    assert result["items"][0]["judge_output"]["tool_results"]


async def test_rule_judge_on_mode_uses_batched_candidate_result(monkeypatch) -> None:
    service = FakeBatchRuleJudgeService(candidate_status="fail")
    monkeypatch.setattr(
        rule_judge_tools_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                pipeline_mode="on",
                semantic_batch_size=4,
                semantic_concurrency=2,
            )
        ),
    )

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                {"rule_code": "alioth_6", "judge_input": {}},
                {"rule_code": "alioth_14", "judge_input": {}},
            ],
            service=service,
        )

    assert service.requests == []
    assert len(service.batch_requests) == 1
    assert [item["execution_status"] for item in result["items"]] == [
        "fail",
        "fail",
    ]
    assert result["dispatch"] == {
        "batch_size": 4,
        "concurrency": 2,
        "batch_count": 1,
    }


async def test_rule_judge_on_mode_times_out_batch_to_manual_review(monkeypatch) -> None:
    monkeypatch.setattr(
        rule_judge_tools_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                pipeline_mode="on",
                semantic_batch_size=4,
                semantic_concurrency=2,
                semantic_batch_timeout_seconds=0.01,
            )
        ),
    )

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                {"rule_code": "alioth_6", "judge_input": {}},
                {"rule_code": "alioth_14", "judge_input": {}},
            ],
            service=SlowSemanticRuleJudgeService(),
        )

    assert result["execution_status"] == "success"
    assert [item["execution_status"] for item in result["items"]] == [
        "manual_review",
        "manual_review",
    ]
    assert all(
        item["judge_output"]["error_code"] == "SEMANTIC_BATCH_TIMEOUT" for item in result["items"]
    )
    assert all(
        item["judge_input"]["semantic_timeout"]["timeout_seconds"] == 0.01
        for item in result["items"]
    )
    assert all(item["judge_output"]["schema_version"] == "1.0" for item in result["items"])
    assert all(item["judge_output"]["conclusion"] == "manual_review" for item in result["items"])
    assert all(item["judge_output"]["reasoning_summary"] for item in result["items"])
    assert all(item["judge_output"]["tool_results"] for item in result["items"])


async def test_timed_out_batch_merges_persisted_results_by_rule_code(monkeypatch) -> None:
    monkeypatch.setattr(
        rule_judge_tools_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                pipeline_mode="on",
                semantic_batch_size=4,
                semantic_concurrency=2,
                semantic_batch_timeout_seconds=0.01,
            )
        ),
    )

    recorded_payloads: list[list[dict]] = []

    async def record_with_resolved_ids(*, page_capture_id, executions, service=None):
        del service
        recorded_payloads.append(executions)
        return {
            "items": [
                {
                    "status": "ok",
                    "execution_id": f"execution-{index}",
                    "rule_id": f"resolved-rule-{index}",
                    "rule_code": item["rule_code"],
                    "execution_status": item["execution_status"],
                    "page_capture_id": page_capture_id,
                }
                for index, item in enumerate(executions, start=1)
            ]
        }

    monkeypatch.setattr(
        rule_judge_tools_module,
        "record_rule_executions_for_current_run",
        record_with_resolved_ids,
    )

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_and_record_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                {"rule_code": "alioth_6", "judge_input": {}},
                {"rule_code": "alioth_14", "judge_input": {}},
            ],
            judge_service=SlowSemanticRuleJudgeService(),
        )

    assert result["execution_status"] == "success"
    assert result["succeeded_count"] == 2
    assert all(item["persisted"] is True for item in result["items"])
    assert all("error" not in item for item in result["items"])
    assert all(item["judge_strategy"] is None for item in recorded_payloads[0])


async def test_rule_judge_shadow_mode_keeps_legacy_result_and_records_candidate(
    monkeypatch,
) -> None:
    service = FakeBatchRuleJudgeService(candidate_status="fail")
    shadow_service = FakeShadowService()
    monkeypatch.setattr(
        rule_judge_tools_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                pipeline_mode="shadow",
                semantic_batch_size=4,
                semantic_concurrency=2,
            )
        ),
    )

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[
                {"rule_code": "alioth_6", "judge_input": {}},
                {"rule_code": "alioth_14", "judge_input": {}},
            ],
            service=service,
            shadow_service=shadow_service,
        )

    assert [item["execution_status"] for item in result["items"]] == [
        "pass",
        "pass",
    ]
    assert len(service.requests) == 2
    assert len(service.batch_requests) == 1
    assert result["dispatch"]["batch_size"] == 1
    assert result["shadow_dispatch"]["batch_size"] == 4
    assert len(shadow_service.comparisons) == 2
    assert all(
        item.legacy_conclusion == "pass"
        and item.candidate_conclusion == "fail"
        and item.comparison_mode == "historical_replay"
        for item in shadow_service.comparisons
    )


async def test_rule_judge_atomically_records_trusted_result(monkeypatch) -> None:
    recorded_payloads: list[dict] = []
    ordering: list[str] = []

    class Sink:
        async def persist_judgement(self, result):
            ordering.append("attempt")
            assert result["items"][0]["rule_code"] == "alioth_6"

    async def _record(**payload):
        ordering.append("formal")
        recorded_payloads.append(payload)
        return {
            "execution_id": "execution-6",
            "rule_id": payload["rule_id"],
            "rule_code": payload["rule_code"],
            "execution_status": payload["execution_status"],
            "page_capture_id": payload["page_capture_id"],
        }

    monkeypatch.setattr(
        rule_judge_tools_module,
        "record_rule_execution_for_current_run",
        _record,
    )
    service = FakeRuleJudgeService()
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        async with bind_inspection_judgement_sink(Sink()):
            result = await judge_and_record_rule_for_current_run(
                rule_code="alioth_6",
                page_capture_id="capture-1",
                evidence_artifact_ids=["artifact-1"],
                judge_input={},
                judge_service=service,
            )

    assert result["execution_id"] == "execution-6"
    assert result["persisted"] is True
    assert ordering == ["attempt", "formal"]
    assert recorded_payloads[0]["judge_strategy"] == "rule_engine"
    assert recorded_payloads[0]["evidence_artifact_ids"] == ["artifact-1"]


async def test_rule_judge_batch_atomically_records_only_successes(monkeypatch) -> None:
    recorded_batches: list[list[dict]] = []

    async def _record_batch(*, page_capture_id, executions, service=None):
        del service
        recorded_batches.append(executions)
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

    monkeypatch.setattr(
        rule_judge_tools_module,
        "record_rule_executions_for_current_run",
        _record_batch,
    )
    service = FakeRuleJudgeService()
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await judge_and_record_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=["artifact-1"],
            rules=[{"rule_code": "alioth_6"}, {"rule_code": "alioth_14"}],
            judge_service=service,
        )

    assert result["execution_status"] == "success"
    assert result["succeeded_count"] == 2
    assert all(item["persisted"] is True for item in result["items"])
    assert [item["rule_code"] for item in recorded_batches[0]] == [
        "alioth_6",
        "alioth_14",
    ]


async def test_registered_rule_judge_batch_tool_accepts_framework_arguments(
    monkeypatch,
) -> None:
    service = FakeRuleJudgeService()
    monkeypatch.setattr(
        rule_judge_tools_module,
        "_get_rule_judge_service",
        lambda: service,
    )
    monkeypatch.setattr(
        rule_judge_tools_module,
        "record_rule_executions_for_current_run",
        _record_judged_batch,
    )
    metadata = default_registry.get("rule_judge_batch_tool")
    assert metadata is not None

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await metadata.tool.invoke(
            arguments={
                "page_capture_id": "capture-1",
                "evidence_artifact_ids": ["artifact-1"],
                "rules": [
                    {"rule_code": "alioth_6", "judge_input": {}},
                    {"rule_code": "alioth_14", "judge_input": {"sale_price": 8}},
                ],
            }
        )

    assert result
    result_text = str(getattr(result[0], "text", ""))
    assert '"persisted": true' in result_text
    assert '"judge_input"' not in result_text
    assert '"judge_output"' not in result_text
    assert [request.rule_code for request in service.requests] == ["alioth_6", "alioth_14"]


async def test_registered_rule_judge_batch_tool_accepts_minimal_trusted_arguments(
    monkeypatch,
) -> None:
    service = FakeRuleJudgeService()
    monkeypatch.setattr(
        rule_judge_tools_module,
        "_get_rule_judge_service",
        lambda: service,
    )
    monkeypatch.setattr(
        rule_judge_tools_module,
        "record_rule_executions_for_current_run",
        _record_judged_batch,
    )
    metadata = default_registry.get("rule_judge_batch_tool")
    assert metadata is not None

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await metadata.tool.invoke(
            arguments={
                "page_capture_id": "capture-1",
                "rules": [
                    {"rule_code": "alioth_6"},
                    {"rule_code": "alioth_14"},
                ],
            }
        )

    assert result
    assert '"persisted": true' in str(getattr(result[0], "text", ""))
    assert [request.rule_code for request in service.requests] == ["alioth_6", "alioth_14"]
    assert all(request.evidence_artifact_ids == () for request in service.requests)
    assert all(request.judge_input == {} for request in service.requests)


def test_rule_judge_tool_is_registered_as_safe() -> None:
    metadata = default_registry.get("rule_judge_tool")
    batch_metadata = default_registry.get("rule_judge_batch_tool")

    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SAFE
    assert batch_metadata is not None
    assert batch_metadata.permission_level == PermissionLevel.SAFE
    assert set(metadata.tool.parameters()["required"]) == {
        "rule_code",
        "page_capture_id",
    }
    assert set(batch_metadata.tool.parameters()["required"]) == {
        "page_capture_id",
        "rules",
    }
