"""绿色新合同不得靠旧编码改变既有规则；此处只使用受控证据夹具。"""

from copy import deepcopy
from dataclasses import asdict, fields, replace
from io import BytesIO

import pytest
from PIL import Image

from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeService,
    rule_execution_route_payload,
)
from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
from services.api.app.application.task_rule_inventory import build_task_rule_inventory
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)
from services.api.tests.test_rule_judge import (
    CountingSemanticJudge,
    FakeObservationPacketService,
    FakeRuleJudgeEvidenceReader,
    FakeRuleJudgeRepository,
    _request,
)
from services.api.tests.test_task_repository import FakePool
from services.api.tests.test_task_rule_inventory import _rule


class ControlledAppPacketService(FakeObservationPacketService):
    async def build(self, request):
        packet = await super().build(request)
        packet.packet_payload["page"]["surface_kind"] = "android"
        return packet


def _controlled_png(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize(("changed", "expected"), [(True, "pass"), (False, "fail")])
async def test_old_app_alioth_7_uses_its_frozen_screenshot_contract(changed, expected):
    """旧规则只声明截图变化时仍按原合同判；不能因 App 身份全局跳过。"""
    definition = RuleJudgeDefinition(
        rule_id="legacy-rule-7",
        rule_code="alioth_7",
        executor_type="rule_engine",
        input_contract={"required_capture_roles": ["before", "primary"]},
        judge_config={
            "engine_config": {
                "handler": "screenshot_change_assertion",
                "before_role": "before",
                "after_role": "primary",
            }
        },
    )
    before = _controlled_png("red")
    evidence_reader = FakeRuleJudgeEvidenceReader(
        "受控截图变化夹具",
        role_artifacts={
            "before": {"screenshot": before},
            "primary": {"screenshot": _controlled_png("blue") if changed else before},
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=evidence_reader,
        observation_packet_service=ControlledAppPacketService(),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_7",
            judge_input={},
            evidence_capture_roles={"before": "capture-before", "primary": "capture-1"},
        )
    )

    assert result.execution_status == expected
    assert result.judge_output["changed"] is changed
    assert "app_external_link_not_implemented" not in str(result.judge_output)
    assert len(evidence_reader.calls) == 1


@pytest.mark.parametrize("legacy_version", [1, 2, 3])
@pytest.mark.parametrize("code", ["alioth_green_s73", "alioth_green_41", "alioth_41"])
async def test_old_green_or_overlapping_code_does_not_activate_new_contract(code, legacy_version):
    """旧 Skill 和阈值原样进入既有语义链，不能按名称升级到第 4 版。"""

    class CapturingPacketService(FakeObservationPacketService):
        request = None

        async def build(self, request):
            self.request = request
            return await super().build(request)

    definition = RuleJudgeDefinition(
        rule_id=f"legacy-{code}-v{legacy_version}",
        rule_code=code,
        executor_type="llm",
        category="visual",
        check_point="旧任务已经冻结的检查义务",
        steps=[{"type": "observe", "description": "只读取既有材料"}],
        input_contract={"required_artifact_types": ["screenshot"]},
        judge_config={
            "schema_version": "1.0",
            "tool_refs": ["semantic_rule_judge_tool"],
            "skill_name": "legacy-frozen-skill",
            "skill_body": f"旧第 {legacy_version} 版已冻结正文",
        },
        manual_review_policy={"min_confidence": 0.95},
    )
    frozen_before = deepcopy(asdict(definition))
    frozen_before["execution_route"] = rule_execution_route_payload(
        executor_type=definition.executor_type,
        judge_config=definition.judge_config,
        input_contract=definition.input_contract,
    )
    packet_service = CapturingPacketService()
    semantic = CountingSemanticJudge()
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=packet_service,
        semantic_judge=semantic,
    )

    result = await service.judge(
        replace(
            _request(rule_code=code, judge_input={}),
            expected_contract_hash=build_inspection_rule_contract_hash(frozen_before),
        )
    )

    assert semantic.call_count == 1
    assert result.execution_status == "manual_review"
    assert result.judge_output["proposed_conclusion"] == "pass"
    assert result.judge_output["confidence_threshold"] == 0.95
    assert packet_service.request is not None
    assert packet_service.request.judge_config == frozen_before["judge_config"]
    assert packet_service.request.input_contract == frozen_before["input_contract"]
    assert packet_service.request.check_point == frozen_before["check_point"]
    assert asdict(definition) == {k: v for k, v in frozen_before.items() if k != "execution_route"}
    assert "green_rule_contract" not in result.judge_output


@pytest.mark.parametrize("platform", ["app", "h5"])
@pytest.mark.parametrize("legacy_version", [1, 2, 3])
async def test_legacy_green_candidates_and_judge_read_frozen_values_after_live_edit(
    platform, legacy_version
):
    """原任务选规则、判定和复核均读取提交快照，后续升级不回写。"""
    source_rule = _rule(
        1,
        rule_code="alioth_green_s73",
        executor_type="llm",
        check_point="旧节点反馈义务",
        judge_config={
            "tool_refs": ["semantic_rule_judge_tool"],
            "skill_body": f"冻结 Skill 第 {legacy_version} 版",
        },
        manual_review_policy={"min_confidence": 0.95},
    )
    frozen_inventory = build_task_rule_inventory(
        ruleset_id="ruleset-legacy-green",
        ruleset_version=legacy_version,
        ruleset_snapshot_hash="old-snapshot",
        target_platform=platform,
        channel="workbench",
        task_config={},
        rules=[source_rule],
    )
    config = {
        "workflow_version": "v2",
        "rule_inventory": frozen_inventory,
        "ruleset_rule_total": 1,
    }
    frozen_expected = deepcopy(config)
    source_rule["check_point"] = "后来编辑的新义务"
    source_rule["judge_config"] = {"green_rule_contract": {"schema_version": "new"}}
    source_rule["manual_review_policy"] = {"min_confidence": 0.1}
    pool = FakePool(None)

    async def frozen_task_only(query, *args):
        assert "SELECT task.target_platform, task.channel, task.task_config" in query
        assert args == ("task-1",)
        return {
            "target_platform": platform,
            "channel": "workbench",
            "ruleset_id": "ruleset-legacy-green",
            "task_config": config,
        }

    pool.connection.fetchrow = frozen_task_only
    repository = PostgresTaskRepository(pool=pool)

    candidates = await repository.list_candidate_rules(task_id="task-1", page_type="home")
    definition = await repository.get_rule_judge_definition(
        task_id="task-1", rule_code="alioth_green_s73"
    )
    writer_definition = await repository.get_rule_execution_definition(
        task_id="task-1", rule_id="rule-1", rule_code="alioth_green_s73"
    )

    assert len(candidates) == 1
    assert candidates[0].check_point == "旧节点反馈义务"
    assert candidates[0].judge_config["skill_body"] == f"冻结 Skill 第 {legacy_version} 版"
    assert definition is not None
    assert definition.judge_config == candidates[0].judge_config
    assert writer_definition is not None
    assert writer_definition.manual_review_policy == {"min_confidence": 0.95}
    assert config == frozen_expected
    assert pool.connection.calls == []


@pytest.mark.parametrize("legacy_version", [1, 2, 3])
@pytest.mark.parametrize("batch", [False, True])
async def test_explicit_v1_contract_keeps_legacy_model_and_review_policy(legacy_version, batch):
    """显式旧合同继续读旧 Skill，不能按新 v4 的必需材料中断原判定。"""
    definitions = {
        code: RuleJudgeDefinition(
            rule_id=f"legacy-{code}",
            rule_code=code,
            executor_type="llm",
            category="visual",
            check_point="旧页面反馈义务",
            manual_review_policy={"min_confidence": 0.95},
            judge_config={
                "tool_refs": ["semantic_rule_judge_tool"],
                "green_rule_contract": {
                    "schema_version": "green-rule-contract.v1",
                    "ruleset_version": legacy_version,
                },
                "skill_body": f"旧第 {legacy_version} 版的冻结正文",
            },
        )
        for code in ("alioth_green_s72", "alioth_green_s73")
    }
    original = deepcopy({code: asdict(value) for code, value in definitions.items()})

    class Repository:
        async def get_rule_judge_definition(self, *, task_id, rule_code):
            assert task_id == "task-1"
            return definitions[rule_code]

    class Model(CountingSemanticJudge):
        batches = 0

        async def judge_batch(self, *, items):
            self.batches += 1
            return {
                packet.rule_code: await self.judge(packet=packet, tool_results=tools)
                for packet, tools in items
            }

    semantic = Model()
    service = RuleJudgeService(
        repository=Repository(),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=semantic,
    )
    requests = [_request(rule_code=code, judge_input={}) for code in definitions]
    results = await service.judge_many(requests) if batch else [await service.judge(requests[0])]

    assert semantic.call_count == len(results)
    assert semantic.batches == (1 if batch else 0)
    for result in results:
        assert result.execution_status == "manual_review"
        assert result.judge_output["proposed_conclusion"] == "pass"
        assert result.judge_output["confidence_threshold"] == 0.95
        assert "green_skill_program_sha256" not in result.judge_output
    assert {code: asdict(value) for code, value in definitions.items()} == original


@pytest.mark.parametrize("old_first", [False, True])
@pytest.mark.parametrize("new_contract_change", [None, "skill", "unknown_schema", "non_mapping"])
async def test_mixed_green_batch_keeps_old_result_and_isolates_new_contract_failure(
    old_first, new_contract_change
):
    """同批新规则缺材料或合同损坏时，旧规则结论与其单独执行保持一致。"""
    old_rule = RuleJudgeDefinition(
        rule_id="old-qr-rule",
        rule_code="alioth_41",
        executor_type="llm",
        category="interaction",
        check_point="旧二维码内容检查",
        judge_config={"tool_refs": ["semantic_rule_judge_tool"]},
    )
    payload = {"rule_id": "new-navigation-rule", **build_green_rule_payload("32")}
    if new_contract_change == "skill":
        payload["judge_config"]["green_skill_program"]["combined_body"] += "损坏正文"
    elif new_contract_change == "unknown_schema":
        payload["judge_config"]["green_rule_contract"]["schema_version"] = (
            "green-rule-contract.v999"
        )
    elif new_contract_change == "non_mapping":
        payload["judge_config"]["green_rule_contract"] = "invalid-declaration"
    new_rule = RuleJudgeDefinition(
        **{
            field.name: payload[field.name]
            for field in fields(RuleJudgeDefinition)
            if field.name in payload
        }
    )
    definitions = {rule.rule_code: rule for rule in (old_rule, new_rule)}

    class Repository:
        async def get_rule_judge_definition(self, *, task_id, rule_code):
            assert task_id == "task-1"
            return definitions[rule_code]

    class Model:
        calls = []

        async def judge(self, *, packet, tool_results):
            assert packet.rule_code == "alioth_41"
            self.calls.append(packet.rule_code)
            return SemanticJudgeModelOutput(
                conclusion="fail",
                confidence=1.0,
                reasoning_summary="受控旧规则反例",
                evidence_refs=["artifact-1"],
            )

    class Reader:
        reads = []

        async def prepare_observation_packet(self, **options):
            self.reads.append(options["rule_id"])
            return TrustedEvidenceBundle(
                "packet-1",
                "capture-1",
                {"primary": "capture-1"},
                (
                    TrustedEvidenceArtifact(
                        "artifact-1",
                        "screenshot",
                        "capture-1",
                        "primary",
                        "image/png",
                        _controlled_png("red"),
                    ),
                ),
            )

    model, reader = Model(), Reader()
    service = RuleJudgeService(
        repository=Repository(),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=model,
        evidence_reader=reader,
    )
    old_request = _request(rule_code=old_rule.rule_code, judge_input={})
    alone = await service.judge(old_request)
    new_request = _request(rule_code=new_rule.rule_code, judge_input={"green_pass": True})
    requests = [old_request, new_request] if old_first else [new_request, old_request]

    results = await service.judge_many(requests)

    assert [result.rule_code for result in results] == [request.rule_code for request in requests]
    by_code = {result.rule_code: result for result in results}
    assert by_code[old_rule.rule_code] == alone
    assert alone.execution_status == "fail"
    assert by_code[new_rule.rule_code].execution_status == "evidence_insufficient"
    assert model.calls == ["alioth_41", "alioth_41"]
    assert reader.reads == ([] if new_contract_change else ["new-navigation-rule"])
