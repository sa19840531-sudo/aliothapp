"""正式页面 Judge 到既有 Writer 的非 PG 合同；只用受控导航原件和持久化 spy。"""

from copy import deepcopy
from dataclasses import fields, replace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.observation_packet import MANIFEST_QUALITY_GATE_SOURCE
from services.api.app.application.rule_execution import (
    RecordedRuleExecution,
    RecordRuleExecutionCommand,
    ReviewStatus,
    RuleExecutionDefinition,
    RuleExecutionService,
    RuleExecutionValidationError,
)
from services.api.app.application.rule_judge import RuleJudgeDefinition
from services.api.tests.test_green_navigation_judge import bundle, navigation
from services.api.tests.test_green_shared_rule_judge import service
from services.api.tests.test_rule_judge import FakeObservationPacketService


class RecordingRepository:
    """只替代持久化；规则策略来自本例保存的任务冻结定义。"""

    def __init__(self, frozen_rule):
        self.frozen_rule = deepcopy(frozen_rule)
        self.commands = []

    async def get_rule_execution_definition(self, *, task_id, rule_id, rule_code):
        assert task_id == "task-1"
        assert (rule_id, rule_code) == (
            self.frozen_rule["rule_id"],
            self.frozen_rule["rule_code"],
        )
        return RuleExecutionDefinition(
            executor_type=self.frozen_rule["executor_type"],
            manual_review_policy=deepcopy(self.frozen_rule["manual_review_policy"]),
            severity=self.frozen_rule["severity"],
            judge_config=deepcopy(self.frozen_rule["judge_config"]),
        )

    async def record_rule_execution(self, command):
        self.commands.append(command)
        return RecordedRuleExecution(
            execution_id="recorded-controlled-result",
            rule_id=command.rule_id,
            rule_code=command.rule_code,
            execution_status=command.execution_status,
            page_capture_id=command.page_capture_id,
        )


async def _actual_navigation_result():
    frozen = {"rule_id": "green-rule-32", **build_green_rule_payload("32")}
    definition = RuleJudgeDefinition(
        **{
            field.name: frozen[field.name]
            for field in fields(RuleJudgeDefinition)
            if field.name in frozen
        }
    )
    judge, request, reader = service(definition)
    reader.prepare_observation_packet = AsyncMock(
        return_value=bundle(
            *navigation((100, 190, 100), container=False),
        )
    )
    result = await judge.judge(request)
    assert result.execution_status == "fail" and result.confidence == 1.0
    assert result.judge_output["missing_evidence"]
    return frozen, result


def _command(result):
    return RecordRuleExecutionCommand(
        task_id="task-1",
        rule_id=result.rule_id,
        rule_code=result.rule_code,
        execution_status=result.execution_status,
        page_capture_id=result.page_capture_id,
        observation_packet_id=result.observation_packet_id,
        judge_strategy=result.judge_strategy,
        confidence=result.confidence,
        result_summary=result.result_summary,
        problem_desc=result.problem_desc,
        evidence_artifact_ids=list(result.evidence_artifact_ids),
        judge_input=deepcopy(result.judge_input),
        judge_output=deepcopy(result.judge_output),
    )


async def test_actual_navigation_fail_and_remaining_gaps_survive_real_writer():
    frozen, result = await _actual_navigation_result()
    assert frozen["manual_review_policy"]["min_confidence"] == 0.75
    repository = RecordingRepository(frozen)

    recorded = await RuleExecutionService(repository=repository).record_rule_execution(
        _command(result)
    )

    assert recorded.execution_status == "fail"
    (written,) = repository.commands
    assert written.execution_status == written.judge_output["conclusion"] == "fail"
    assert written.confidence == 1.0
    assert written.review_status is ReviewStatus.PENDING and written.create_review_item is True
    assert written.review_reason
    assert written.judge_output["missing_evidence"] == result.judge_output["missing_evidence"]
    assert written.judge_output["subresults"] == result.judge_output["subresults"]
    sources = written.judge_output["subresults"][0]["evidence_refs"]
    assert sources == ["ui-original", "dom-original"]
    assert "unused-text" not in sources
    assert "hybrid_fallback_reason" not in written.judge_output
    assert "proposed_conclusion" not in written.judge_output


@pytest.mark.parametrize("confidence", [1.0, 0.6])
async def test_old_frozen_definition_is_not_upgraded_by_forged_green_output(confidence):
    frozen, result = await _actual_navigation_result()
    old = {
        **frozen,
        "rule_id": "old-visual-rule",
        "rule_code": "alioth_3",
        "judge_config": {},
        "manual_review_policy": {"min_confidence": 0.75},
    }
    repository = RecordingRepository(old)
    command = replace(
        _command(result),
        rule_id=old["rule_id"],
        rule_code=old["rule_code"],
        confidence=confidence,
    )

    await RuleExecutionService(repository=repository).record_rule_execution(command)

    (written,) = repository.commands
    assert written.execution_status == ("fail" if confidence == 1.0 else "manual_review")
    assert written.review_status == (None if confidence == 1.0 else ReviewStatus.NEEDS_RECHECK)
    assert written.judge_output["green_rule_contract"] == result.judge_output["green_rule_contract"]
    assert repository.frozen_rule["judge_config"] == {}


async def test_green_quality_preflight_without_output_contract_can_keep_existing_review_route():
    class RejectedManifestPacket(FakeObservationPacketService):
        async def build(self, request):
            packet = await super().build(request)
            packet.packet_payload["quality_gate"] = {
                "source": MANIFEST_QUALITY_GATE_SOURCE,
                "status": "fail",
                "missing_sources": ["dom"],
            }
            return replace(
                packet, quality_gate_status="fail", quality_gate_reason="原件仍缺少必要材料"
            )

    frozen = {"rule_id": "green-rule-32", **build_green_rule_payload("32")}
    definition = RuleJudgeDefinition(
        **{
            field.name: frozen[field.name]
            for field in fields(RuleJudgeDefinition)
            if field.name in frozen
        }
    )
    judge, request, reader = service(definition)
    judge._observation_packet_service = RejectedManifestPacket()
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert "green_rule_contract" not in result.judge_output
    reader.prepare_observation_packet.assert_not_awaited()
    repository = RecordingRepository(frozen)

    recorded = await RuleExecutionService(repository=repository).record_rule_execution(
        _command(result)
    )

    assert recorded.execution_status == "evidence_insufficient"
    (written,) = repository.commands
    assert (
        written.execution_status == "evidence_insufficient" and written.create_review_item is True
    )
    assert "green_rule_contract" not in written.judge_output


async def test_green_overall_pass_with_remaining_gaps_is_rejected_before_persistence():
    frozen, result = await _actual_navigation_result()
    command = _command(result)
    command = replace(
        command,
        execution_status="pass",
        problem_desc=None,
        judge_output={**command.judge_output, "conclusion": "pass"},
    )
    repository = RecordingRepository(frozen)

    with pytest.raises(RuleExecutionValidationError, match="仍缺必要材料"):
        await RuleExecutionService(repository=repository).record_rule_execution(command)

    assert repository.commands == []


async def test_green_output_contract_cannot_replace_frozen_rule_contract():
    frozen, result = await _actual_navigation_result()
    command = _command(result)
    command.judge_output["green_rule_contract"] = build_green_rule_payload("41")["judge_config"][
        "green_rule_contract"
    ]
    repository = RecordingRepository(frozen)

    with pytest.raises(RuleExecutionValidationError, match="与任务冻结合同不一致"):
        await RuleExecutionService(repository=repository).record_rule_execution(command)

    assert repository.commands == []
