"""普通 RuleJudge 单条/批次必须消费独立序列语义，不能只信 Manifest PASS。"""

from dataclasses import replace

import pytest

from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.application.observation_packet import ObservationPacketService
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeRequest,
    RuleJudgeService,
    rule_execution_route_payload,
)
from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
from services.api.tests.test_rule_judge import FakeRuleJudgeRepository
from services.api.tests.test_web_sequence_observation import (
    SequenceRepository,
    _ready_source,
    _sequence_request,
)


def _definition_and_request(auth_requirement=None):
    packet_request = _sequence_request()
    if auth_requirement is not None:
        packet_request.input_contract["auth_requirement"] = auth_requirement
    definition = RuleJudgeDefinition(
        rule_id=packet_request.rule_id,
        rule_code=packet_request.rule_code,
        executor_type="llm",
        judge_config={"tool_refs": ["semantic_rule_judge_tool"]},
        input_contract=packet_request.input_contract,
        category="interaction",
        check_point="持续加载是否构成产品异常",
    )
    rule = {
        name: getattr(definition, name)
        for name in (
            "rule_id",
            "rule_code",
            "category",
            "executor_type",
            "severity",
            "check_point",
            "input_contract",
            "judge_config",
            "manual_review_policy",
            "steps",
        )
    }
    rule["execution_route"] = rule_execution_route_payload(
        executor_type="llm",
        judge_config=definition.judge_config,
        input_contract=definition.input_contract,
    )
    request = RuleJudgeRequest(
        task_id="task-1",
        rule_code=definition.rule_code,
        page_capture_id="capture-1",
        evidence_artifact_ids=(),
        judge_input={},
        source_agent_run_id="run-1",
        evidence_input_set_id=packet_request.evidence_input_set_id,
        expected_contract_hash=build_inspection_rule_contract_hash(rule),
    )
    return definition, request


class BusinessJudge:
    def __init__(self, *, evidence_ref=None):
        self.packets = []
        self.evidence_ref = evidence_ref

    async def judge(self, *, packet, tool_results):
        self.packets.append(packet)
        return SemanticJudgeModelOutput(
            conclusion="pass",
            confidence=0.95,
            reasoning_summary="业务模型判定通过，语义合法并不预设失败",
            evidence_refs=[self.evidence_ref or packet.evidence_artifact_ids[0]],
        )

    async def judge_many(self, items):
        return [await self.judge(packet=packet, tool_results=results) for packet, results in items]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("auth_requirement", ["authenticated", "guest"])
async def test_sequence_consumer_keeps_unknown_account_out_of_model(batch, auth_requirement):
    definition, request = _definition_and_request(auth_requirement)
    source = _ready_source(True)
    source.web_sequence["rule_contract_hash"] = request.expected_contract_hash
    business = BusinessJudge()
    service = RuleJudgeService(repository=FakeRuleJudgeRepository(definition), observation_packet_service=ObservationPacketService(repository=SequenceRepository(source)), semantic_judge=business)
    result = (await service.judge_many([request]))[0] if batch else await service.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert not business.packets


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("legal", [False, True])
async def test_actual_rule_judge_sequence_gate_precedes_business_model(batch, legal):
    definition, request = _definition_and_request()
    source = _ready_source(True)
    source.web_sequence["rule_contract_hash"] = request.expected_contract_hash
    if not legal:
        source.web_sequence["sequence"]["target_platform"] = "h5"
    business = BusinessJudge()
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=ObservationPacketService(repository=SequenceRepository(source)),
        semantic_judge=business,
    )
    result = (await service.judge_many([request]))[0] if batch else await service.judge(request)
    assert result.execution_status == ("pass" if legal else "evidence_insufficient")
    assert len(business.packets) == int(legal)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roles",
    [
        ["failure_sequence"],
        ["interaction_sequence"],
        ["diagnostic"],
    ],
)
@pytest.mark.parametrize("batch", [False, True])
async def test_sequence_or_diagnostic_only_rule_rejects_unbound_stable_input(roles, batch):
    from services.api.app.application.rule_judge import RuleJudgeValidationError
    from services.api.tests.test_rule_judge import FakeObservationPacketService

    definition, request = _definition_and_request()
    contract = dict(definition.input_contract)
    contract["web_evidence"] = dict(
        contract["web_evidence"], accepted_roles=roles, accept_unclassified=False
    )
    definition = replace(definition, input_contract=contract)
    request = replace(request, evidence_input_set_id=None, expected_contract_hash=None)
    business = BusinessJudge()
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=business,
    )
    with pytest.raises(RuleJudgeValidationError, match="stable_page"):
        if batch:
            await service.judge_many([request])
        else:
            await service.judge(request)
    assert business.packets == []


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("batch", [False, True])
async def test_stable_compatible_contract_and_legacy_rule_keep_normal_judge(declared, batch):
    from services.api.tests.test_rule_judge import FakeObservationPacketService

    definition, request = _definition_and_request()
    contract = dict(definition.input_contract)
    if declared:
        contract["web_evidence"] = dict(
            contract["web_evidence"], accepted_roles=["stable_page", "failure_sequence"]
        )
    else:
        contract.pop("web_evidence")
    definition = replace(definition, input_contract=contract)
    request = replace(request, evidence_input_set_id=None, expected_contract_hash=None)
    business = BusinessJudge()
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=business,
    )
    result = (await service.judge_many([request]))[0] if batch else await service.judge(request)
    assert result.execution_status == "pass"
    assert business.packets[0].evidence_artifact_ids == ("artifact-1",)
    assert business.packets[0].image_payloads == ()
    assert result.judge_output["model_output"]["evidence_refs"] == ["artifact-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("batch", [False, True])
async def test_stable_compatible_and_legacy_rule_cannot_borrow_sequence_reference(declared, batch):
    from services.api.tests.test_rule_judge import FakeObservationPacketService

    definition, request = _definition_and_request()
    contract = dict(definition.input_contract)
    if declared:
        contract["web_evidence"] = dict(
            contract["web_evidence"], accepted_roles=["stable_page", "failure_sequence"]
        )
    else:
        contract.pop("web_evidence")
    definition = replace(definition, input_contract=contract)
    request = replace(request, evidence_input_set_id=None, expected_contract_hash=None)
    # shot-1 属于上面的序列观察包，不能借给只有 artifact-1 的稳定页面包。
    business = BusinessJudge(evidence_ref="shot-1")
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=business,
    )
    result = (await service.judge_many([request]))[0] if batch else await service.judge(request)
    assert result.execution_status == "manual_review"
    assert result.judge_output["proposed_conclusion"] == "pass"
    assert result.judge_output["invalid_evidence_refs"] == ["shot-1"]
    assert result.evidence_artifact_ids == ("artifact-1",)
    assert "不属于当前观察包" in result.problem_desc
