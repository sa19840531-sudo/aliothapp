from datetime import UTC, datetime

import pytest

from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools import rule_tools as rule_tools_module
from services.api.app.agents.tools.registry import PermissionLevel, default_registry
from services.api.app.agents.tools.rule_tools import (
    RuleExecutionBatchItem,
    get_candidate_rules_for_current_run,
    record_rule_execution_for_current_run,
    record_rule_executions_for_current_run,
)
from services.api.app.application.rule_execution import (
    RecordedRuleExecution,
    RuleExecutionValidationError,
    derive_quality_outcome,
)
from services.api.app.application.task_query import CandidateRule


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_batch_propagates_web_contract_termination_without_generalizing_errors(fallback):
    from services.api.app.application.web_sequence_errors import WebRuleContractIncompatibleError

    class ContractConflictService:
        async def record_rule_execution(self, command):
            if fallback and command.execution_status != "error":
                raise RuleExecutionValidationError("非法判定字段进入原错误降级")
            raise WebRuleContractIncompatibleError("WEB_RULE_CONTRACT_INCOMPATIBLE", command=command)

    with bind_agent_run_context(AgentRunContext(task_id="task", agent_run_id="consumer", work_item_id="work", attempt_id="attempt", lease_token="lease")):
        with pytest.raises(WebRuleContractIncompatibleError):
            await record_rule_executions_for_current_run(
                page_capture_id="capture",
                executions=[{"rule_id":"r1","execution_status":"pass"},{"rule_id":"r2","execution_status":"pass"}],
                service=ContractConflictService(),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_batch_keeps_ordinary_runtime_errors_local_to_item(fallback):
    class OrdinaryFailureService(BatchRuleExecutionService):
        async def record_rule_execution(self, command):
            if command.rule_code == "alioth_bad":
                if fallback and command.execution_status != "error":
                    raise RuleExecutionValidationError("普通输入验证错误")
                raise RuntimeError("普通业务写入失败")
            return await super().record_rule_execution(command)

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_executions_for_current_run(
            page_capture_id="capture-1",
            executions=[{"rule_code":"alioth_bad","execution_status":"pass"},{"rule_code":"alioth_6","execution_status":"pass"}],
            service=OrdinaryFailureService(),
        )
    assert result["execution_status"] == "partial_success"
    assert result["items"][0]["error"]["code"] == ("RULE_EXECUTION_ERROR_FALLBACK_FAILED" if fallback else "RULE_EXECUTION_WRITE_FAILED")
    assert result["items"][1]["status"] == "ok"


class FakeCandidateRuleService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get_page_capture_url(self, *, task_id: str, page_capture_id: str) -> str:
        assert task_id == "task-1"
        assert page_capture_id == "capture-1"
        return "app://com.example.target/.MainActivity"

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
    ) -> list[CandidateRule]:
        self.calls.append(
            {
                "task_id": task_id,
                "page_type": page_type,
                "page_url": page_url,
            }
        )
        return [
            CandidateRule(
                rule_id="rule-1",
                rule_code="alioth_1",
                name="弹窗关闭能力检查",
                category="ad",
                severity="high",
                executor_type="hybrid",
                page_type="home",
                applicable_page_types=["home"],
                check_point="检查弹窗是否可以关闭。",
                judge_config={
                    "tool_refs": [
                        "ad_modal_detector_tool",
                        "visual_rule_judge_tool",
                    ],
                    "capability_config": {
                        "deterministic_tools": ["ad_modal_detector_tool"],
                        "required_artifact_types": ["screenshot", "ui_snapshot"],
                    },
                },
                sort_order=10,
                match_reason="primary_page_type",
                description="检查广告弹窗关闭能力。",
                steps=[{"action": "observe"}],
                input_contract={"required_artifacts": ["dom_snapshot"]},
                manual_review_policy={"min_confidence": 0.82},
            )
        ]


class FakeRuleExecutionService:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def record_rule_execution(self, command) -> RecordedRuleExecution:
        self.commands.append(command)
        return RecordedRuleExecution(
            execution_id="execution-1",
            rule_id="rule-1",
            rule_code=command.rule_code or "alioth_1",
            execution_status=command.execution_status,
            page_capture_id=command.page_capture_id,
            quality_outcome=derive_quality_outcome(command.execution_status),
        )


class MultiCandidateRuleService:
    async def get_page_capture_url(self, *, task_id: str, page_capture_id: str) -> str:
        del task_id, page_capture_id
        return "app://com.example.target/.MainActivity"

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
    ) -> list[CandidateRule]:
        del task_id, page_type, page_url
        return [
            CandidateRule(
                rule_id="rule-6",
                rule_code="alioth_6",
                name="技术错误直出检查",
                description="验证页面不直接展示技术错误信息。",
                category="visual",
                severity="high",
                executor_type="rule_engine",
                page_type=None,
                applicable_page_types=["home"],
                check_point="不应出现技术错误直出",
                input_contract={"required_artifact_types": ["visible_text"]},
                judge_config={
                    "engine_config": {
                        "handler": "technical_error_text",
                        "pattern": "HTTP 500",
                    }
                },
                manual_review_policy={"min_confidence": 0.8},
                sort_order=10,
                match_reason="applicable_page_types",
            ),
            CandidateRule(
                rule_id="rule-3",
                rule_code="alioth_3",
                name="图片展示质量检查",
                description="验证页面图片展示质量。",
                category="visual",
                severity="high",
                executor_type="hybrid",
                page_type="home",
                applicable_page_types=["home"],
                check_point="图片不应拉伸",
                input_contract={"required_artifact_types": ["screenshot"]},
                judge_config={
                    "tool_refs": [
                        "image_quality_analyzer_tool",
                        "visual_rule_judge_tool",
                    ],
                    "capability_config": {
                        "deterministic_tools": ["image_quality_analyzer_tool"],
                    },
                },
                manual_review_policy={"min_confidence": 0.8},
                sort_order=20,
                match_reason="primary_page_type",
            ),
            CandidateRule(
                rule_id="rule-7",
                rule_code="alioth_7",
                name="敏感信息直出检查",
                description="验证页面不直接展示敏感信息。",
                category="text",
                severity="high",
                executor_type="rule_engine",
                page_type=None,
                applicable_page_types=["home"],
                check_point="不应出现敏感信息直出",
                input_contract={"required_artifact_types": ["visible_text"]},
                judge_config={
                    "engine_config": {
                        "handler": "sensitive_content_text",
                        "pattern": "password",
                    }
                },
                manual_review_policy={"min_confidence": 0.8},
                sort_order=25,
                match_reason="applicable_page_types",
            ),
            CandidateRule(
                rule_id="rule-16",
                rule_code="alioth_16",
                name="广告素材比例检查",
                description="验证广告素材比例。",
                category="ad",
                severity="medium",
                executor_type="hybrid",
                page_type="home",
                applicable_page_types=["home"],
                check_point="广告素材比例合理",
                input_contract={"required_artifact_types": ["screenshot"]},
                judge_config={
                    "tool_refs": [
                        "image_geometry_tool",
                        "visual_rule_judge_tool",
                    ],
                    "capability_config": {
                        "deterministic_tools": ["image_geometry_tool"],
                    },
                },
                manual_review_policy={"min_confidence": 0.8},
                sort_order=30,
                match_reason="primary_page_type",
            ),
        ]


class PopupStateRequiredRuleService:
    async def get_page_capture_url(self, *, task_id: str, page_capture_id: str) -> str:
        del task_id, page_capture_id
        return "app://com.example.target/.MainActivity"

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
    ) -> list[CandidateRule]:
        del task_id, page_type, page_url
        return [
            CandidateRule(
                rule_id="rule-21",
                rule_code="alioth_21",
                name="首页弹窗蒙层点击行为检查",
                description="仅在首页弹窗蒙层出现时检查点击行为。",
                category="ad",
                severity="high",
                executor_type="rule_engine",
                page_type="home",
                applicable_page_types=["home"],
                check_point="检查弹窗蒙层点击行为",
                input_contract={"required_artifact_types": ["screenshot"]},
                judge_config={
                    "engine_config": {
                        "handler": "screenshot_change_assertion",
                        "required_page_states": ["popup", "modal", "overlay", "dialog"],
                    }
                },
                manual_review_policy={"min_confidence": 0.8},
                sort_order=210,
                match_reason="primary_page_type",
            )
        ]


class BatchRuleExecutionService:
    def __init__(self) -> None:
        self.commands: list[object] = []

    async def record_rule_execution(self, command) -> RecordedRuleExecution:
        self.commands.append(command)
        suffix = command.rule_code.rsplit("_", 1)[-1]
        return RecordedRuleExecution(
            execution_id=f"execution-{suffix}",
            rule_id=f"rule-{suffix}",
            rule_code=command.rule_code,
            execution_status=command.execution_status,
            page_capture_id=command.page_capture_id,
        )


async def test_batch_normalizes_blocked_status_to_evidence_insufficient() -> None:
    """探索链路透传的阻断态 blocked 应降级为证据不足并保留原始态。

    2026-08-25 线上实锤：弹窗阻断状态直写导致写入守卫拒收，整条判定
    变成「执行异常：非法规则执行状态：blocked」。"""
    service = BatchRuleExecutionService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_executions_for_current_run(
            page_capture_id="capture-1",
            executions=[
                {
                    "rule_code": "alioth_21",
                    "execution_status": "blocked",
                    "result_summary": "弹窗关闭被策略阻断",
                }
            ],
            service=service,
        )

    assert result["succeeded_count"] == 1
    command = service.commands[0]
    assert command.execution_status == "evidence_insufficient"
    assert command.judge_output["original_execution_status"] == "blocked"


class PartialBatchRuleExecutionService(BatchRuleExecutionService):
    async def record_rule_execution(self, command) -> RecordedRuleExecution:
        if command.rule_code == "alioth_16" and command.execution_status != "error":
            raise RuleExecutionValidationError("缺少 hybrid tool_results")
        return await super().record_rule_execution(command)


class PayloadRejectingRuleExecutionService(BatchRuleExecutionService):
    async def record_rule_execution(self, command) -> RecordedRuleExecution:
        if command.execution_status != "error":
            raise RuleExecutionValidationError("llm/hybrid 判定必须提供非空 judge_input")
        return await super().record_rule_execution(command)


class UnavailableBatchRuleExecutionService(BatchRuleExecutionService):
    async def record_rule_execution(self, command) -> RecordedRuleExecution:
        self.commands.append(command)
        raise OSError("database unavailable")


async def test_candidate_rules_tool_uses_task_id_from_run_context() -> None:
    service = FakeCandidateRuleService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await get_candidate_rules_for_current_run(
            page_type="home",
            page_capture_id="capture-1",
            service=service,
        )

    assert service.calls == [
        {
            "task_id": "task-1",
            "page_type": "home",
            "page_url": "app://com.example.target/.MainActivity",
        }
    ]
    assert result["task_id"] == "task-1"
    assert result["page_capture_id"] == "capture-1"
    assert result["page_type"] == "home"
    assert result["rules"][0]["rule_code"] == "alioth_1"
    assert result["rules"][0]["description"] == "检查广告弹窗关闭能力。"
    assert result["rules"][0]["steps"] == [{"action": "observe"}]
    assert result["rules"][0]["input_contract"] == {"required_artifacts": ["dom_snapshot"]}
    assert result["rules"][0]["judge_config"]["tool_refs"] == [
        "ad_modal_detector_tool",
        "visual_rule_judge_tool",
    ]
    assert result["rules"][0]["manual_review_policy"] == {"min_confidence": 0.82}
    assert result["rules"][0]["execution_route"] == {
        "strategy": "hybrid",
        "ready": True,
        "rule_engine_ready": False,
        "tool_name": "rule_judge_tool",
        "handler": "semantic_rule_judge",
        "handler_tool_name": "visual_rule_judge_tool",
        "judge_input_source": "trusted_observation_packet",
        "required_input_fields": [],
        "required_artifact_types": ["screenshot", "ui_snapshot"],
        "session_dependency": "snapshot",
        "blocked_reason": None,
    }


async def test_candidate_rules_tool_rejects_lifecycle_page_type() -> None:
    service = FakeCandidateRuleService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await get_candidate_rules_for_current_run(
            page_type="line_loading",
            page_capture_id="capture-1",
            service=service,
        )

    assert result["rules"] == []
    assert result["error"]["code"] == "PAGE_TYPE_NOT_RULE_MATCHABLE"
    assert service.calls == []


async def test_candidate_rules_tool_returns_grouped_execution_plan() -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await get_candidate_rules_for_current_run(
            page_type="home",
            page_capture_id="capture-1",
            service=MultiCandidateRuleService(),
        )

    plan = result["execution_plan"]
    assert plan["candidate_rule_count"] == 4
    assert plan["batch_required"] is True
    assert plan["write_tool_name"] == "record_rule_executions_batch_tool"
    assert plan["batches"] == [
        {
            "batch_id": "rule_engine:all:snapshot",
            "strategy": "rule_engine",
            "category": None,
            "skill_name": "rule-matching",
            "session_dependency": "snapshot",
            "rule_codes": ["alioth_6", "alioth_7"],
            "judge_tool_name": "rule_judge_batch_tool",
            "write_tool_name": "record_rule_executions_batch_tool",
        },
        {
            "batch_id": "hybrid:visual:snapshot",
            "strategy": "hybrid",
            "category": "visual",
            "skill_name": "visual-rule",
            "session_dependency": "snapshot",
            "rule_codes": ["alioth_3"],
            "judge_tool_name": "rule_judge_batch_tool",
            "write_tool_name": "record_rule_executions_batch_tool",
        },
        {
            "batch_id": "hybrid:ad:snapshot",
            "strategy": "hybrid",
            "category": "ad",
            "skill_name": "ad-rule",
            "session_dependency": "snapshot",
            "rule_codes": ["alioth_16"],
            "judge_tool_name": "rule_judge_batch_tool",
            "write_tool_name": "record_rule_executions_batch_tool",
        },
    ]


async def test_popup_state_only_returns_popup_relevant_rules() -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await get_candidate_rules_for_current_run(
            page_type="home",
            page_capture_id="capture-1",
            page_state="popup",
            service=MultiCandidateRuleService(),
        )

    assert [item["rule_code"] for item in result["rules"]] == ["alioth_16"]
    assert result["execution_plan"]["candidate_rule_count"] == 1


@pytest.mark.parametrize(
    ("page_state", "expected_rule_codes"),
    [("stable", []), ("popup", ["alioth_21"])],
)
async def test_candidate_rules_respect_required_page_states(
    page_state: str,
    expected_rule_codes: list[str],
) -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await get_candidate_rules_for_current_run(
            page_type="home",
            page_capture_id="capture-1",
            page_state=page_state,
            service=PopupStateRequiredRuleService(),
        )

    assert [item["rule_code"] for item in result["rules"]] == expected_rule_codes
    assert result["execution_plan"]["candidate_rule_count"] == len(expected_rule_codes)


def test_popup_state_excludes_non_popup_journey_ad_rule() -> None:
    rule = {
        "rule_code": "alioth_17",
        "name": "广告点击跳转一致性检查",
        "description": "检查广告入口和落地页的一致性。",
        "check_point": "点击广告后核对落地页",
        "category": "ad",
        "judge_config": {
            "capability_config": {
                "requires_journey": True,
                "deterministic_tools": ["landing_page_probe_tool"],
            }
        },
    }

    assert rule_tools_module._is_popup_relevant_rule(rule) is False


def test_popup_state_excludes_generic_input_risk_rule() -> None:
    rule = {
        "rule_code": "alioth_28",
        "name": "政治恐怖与人身攻击内容检查",
        "description": "检查业务页面中的用户输入内容。",
        "check_point": "用户输入内容不应涉及政治、恐怖或人身攻击。",
        "category": "input_risk",
        "judge_config": {},
    }

    assert rule_tools_module._is_popup_relevant_rule(rule) is False


def test_popup_state_keeps_explicit_popup_input_risk_rule() -> None:
    rule = {
        "rule_code": "popup-risk",
        "name": "弹窗风险文案检查",
        "description": "检查弹窗广告自身的风险文案。",
        "check_point": "弹窗不得展示受限内容。",
        "category": "input_risk",
        "judge_config": {},
    }

    assert rule_tools_module._is_popup_relevant_rule(rule) is True


async def test_candidate_rules_tool_rejects_non_catalog_page_type() -> None:
    service = FakeCandidateRuleService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await get_candidate_rules_for_current_run(
            page_type="ad_popup",
            page_capture_id="capture-1",
            service=service,
        )

    assert service.calls == []
    assert result["rules"] == []
    assert result["error"]["code"] == "INVALID_PAGE_TYPE"
    assert "home" in result["error"]["allowed_page_types"]


async def test_record_rule_execution_tool_uses_task_and_run_id_from_context() -> None:
    service = FakeRuleExecutionService()
    finished_at = datetime(2026, 7, 8, 10, 30, tzinfo=UTC)

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_execution_for_current_run(
            rule_code="alioth_1",
            execution_status="fail",
            page_capture_id="capture-1",
            judge_strategy="hybrid",
            confidence=0.88,
            result_summary="未通过",
            problem_desc="弹窗关闭按钮不可见",
            evidence_artifact_ids=["artifact-1"],
            judge_input={"page_type": "ad_popup"},
            judge_output={"status": "fail"},
            finished_at=finished_at,
            service=service,
        )

    command = service.commands[0]
    assert command.task_id == "task-1"
    assert command.agent_run_id == "run-1"
    assert command.rule_code == "alioth_1"
    assert command.execution_status == "fail"
    assert command.page_capture_id == "capture-1"
    assert command.evidence_artifact_ids == ["artifact-1"]
    assert command.problem_desc == "弹窗关闭按钮不可见"
    assert command.finished_at == finished_at
    assert {key: value for key, value in result.items() if key != "metrics"} == {
        "execution_id": "execution-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "execution_status": "fail",
        "quality_outcome": "failed",
        "page_capture_id": "capture-1",
    }
    assert result["metrics"]["rule_write_back_ms"] >= 0


async def test_single_rule_invalid_payload_is_persisted_as_error() -> None:
    service = PayloadRejectingRuleExecutionService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_execution_for_current_run(
            rule_code="alioth_2",
            execution_status="manual_review",
            page_capture_id="capture-1",
            service=service,
        )

    assert result["execution_status"] == "error"
    assert len(service.commands) == 1
    command = service.commands[0]
    assert command.error_code == "RULE_EXECUTION_PAYLOAD_INVALID"
    assert command.review_status == "needs_recheck"
    assert command.problem_desc == "llm/hybrid 判定必须提供非空 judge_input"


async def test_record_rule_executions_batch_uses_shared_page_context() -> None:
    service = BatchRuleExecutionService()
    executions = [
        RuleExecutionBatchItem(
            rule_code="alioth_6",
            execution_status="pass",
            evidence_artifact_ids=["artifact-1"],
        ),
        RuleExecutionBatchItem(
            rule_code="alioth_16",
            execution_status="skipped",
            result_summary="当前页面没有广告素材",
        ),
    ]

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_executions_for_current_run(
            page_capture_id="capture-1",
            executions=executions,
            service=service,
        )

    assert result["execution_status"] == "success"
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0
    assert [command.rule_code for command in service.commands] == ["alioth_6", "alioth_16"]
    assert {command.page_capture_id for command in service.commands} == {"capture-1"}
    assert [item["execution_id"] for item in result["items"]] == [
        "execution-6",
        "execution-16",
    ]


async def test_record_rule_executions_batch_accepts_framework_dict_items() -> None:
    service = BatchRuleExecutionService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_executions_for_current_run(
            page_capture_id="capture-1",
            executions=[
                {
                    "rule_code": "alioth_6",
                    "execution_status": "pass",
                    "evidence_artifact_ids": ["artifact-1"],
                },
                {
                    "rule_code": "alioth_16",
                    "execution_status": "skipped",
                    "result_summary": "当前页面没有广告素材",
                },
            ],
            service=service,
        )

    assert result["execution_status"] == "success"
    assert [command.rule_code for command in service.commands] == ["alioth_6", "alioth_16"]
    assert {command.page_capture_id for command in service.commands} == {"capture-1"}


async def test_registered_record_batch_tool_accepts_framework_arguments(
    monkeypatch,
) -> None:
    service = BatchRuleExecutionService()
    monkeypatch.setattr(
        rule_tools_module,
        "_get_rule_execution_service",
        lambda: service,
    )
    metadata = default_registry.get("record_rule_executions_batch_tool")
    assert metadata is not None

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await metadata.tool.invoke(
            arguments={
                "page_capture_id": "capture-1",
                "executions": [
                    {
                        "rule_code": "alioth_6",
                        "execution_status": "pass",
                        "evidence_artifact_ids": ["artifact-1"],
                    },
                    {
                        "rule_code": "alioth_16",
                        "execution_status": "skipped",
                        "result_summary": "当前页面没有广告素材",
                    },
                ],
            }
        )

    assert result
    assert [command.rule_code for command in service.commands] == ["alioth_6", "alioth_16"]


async def test_record_rule_executions_batch_returns_partial_failures_by_item() -> None:
    service = PartialBatchRuleExecutionService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_executions_for_current_run(
            page_capture_id="capture-1",
            executions=[
                RuleExecutionBatchItem(
                    rule_code="alioth_6",
                    execution_status="pass",
                    evidence_artifact_ids=["artifact-1"],
                ),
                RuleExecutionBatchItem(
                    rule_code="alioth_16",
                    execution_status="fail",
                    evidence_artifact_ids=["artifact-1"],
                ),
            ],
            service=service,
        )

    assert result["execution_status"] == "success"
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0
    assert result["items"][0]["status"] == "ok"
    assert result["items"][1]["status"] == "ok"
    assert result["items"][1]["degraded"] is True
    assert result["items"][1]["fallback_code"] == "RULE_EXECUTION_PAYLOAD_INVALID"
    assert result["items"][1]["execution_status"] == "error"
    assert service.commands[1].error_message == "缺少 hybrid tool_results"


async def test_record_rule_executions_batch_isolates_invalid_item() -> None:
    service = BatchRuleExecutionService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_executions_for_current_run(
            page_capture_id="capture-1",
            executions=[
                {"rule_code": "alioth_bad"},
                {"rule_code": "alioth_6", "execution_status": "pass"},
            ],
            service=service,
        )

    assert result["execution_status"] == "partial_success"
    assert result["items"][0]["error"]["code"] == "RULE_EXECUTION_ITEM_INVALID"
    assert result["items"][0]["retryable"] is False
    assert result["items"][1]["status"] == "ok"
    assert [command.rule_code for command in service.commands] == ["alioth_6"]


async def test_record_rule_executions_batch_defers_after_infrastructure_failure() -> None:
    service = UnavailableBatchRuleExecutionService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await record_rule_executions_for_current_run(
            page_capture_id="capture-1",
            executions=[
                {"rule_code": "alioth_6", "execution_status": "pass"},
                {"rule_code": "alioth_16", "execution_status": "fail"},
            ],
            service=service,
        )

    assert result["succeeded_count"] == 0
    assert result["failed_count"] == 2
    assert result["items"][0]["error"]["code"] == "RULE_EXECUTION_WRITE_UNAVAILABLE"
    assert result["items"][1]["error"]["code"] == "RULE_EXECUTION_WRITE_DEFERRED"
    assert result["items"][1]["deferred"] is True
    assert len(service.commands) == 1


async def test_rule_tools_require_agent_run_context() -> None:
    service = FakeCandidateRuleService()

    with pytest.raises(RuntimeError, match="Agent Run 上下文"):
        await get_candidate_rules_for_current_run(
            page_type="home",
            page_capture_id="capture-1",
            service=service,
        )


def test_rule_tools_are_registered_with_expected_permissions() -> None:
    candidate = default_registry.get("candidate_rules_tool")
    recorder = default_registry.get("record_rule_execution_tool")
    batch_recorder = default_registry.get("record_rule_executions_batch_tool")

    assert candidate is not None
    assert candidate.permission_level == PermissionLevel.SAFE
    assert recorder is not None
    assert recorder.permission_level == PermissionLevel.SENSITIVE
    assert batch_recorder is not None
    assert batch_recorder.permission_level == PermissionLevel.SENSITIVE
    assert set(candidate.tool.parameters()["required"]) == {"page_type", "page_capture_id"}
    assert set(batch_recorder.tool.parameters()["required"]) == {
        "page_capture_id",
        "executions",
    }


def test_record_rule_execution_tool_exposes_review_status_enum() -> None:
    recorder = default_registry.get("record_rule_execution_tool")

    assert recorder is not None
    parameters = recorder.tool.parameters()
    assert parameters["$defs"]["ReviewStatus"]["enum"] == [
        "pending",
        "approved",
        "rejected",
        "needs_recheck",
    ]
    assert "page_capture_id" in parameters["properties"]
    assert "page_capture_id" in parameters["required"]
    assert "min_confidence" in parameters["properties"]["confidence"]["description"]
    assert "完整 DOM" in parameters["properties"]["judge_input"]["description"]
    assert "schema_version=1.0" in parameters["properties"]["judge_output"]["description"]
    assert "tool_results" in parameters["properties"]["judge_output"]["description"]
