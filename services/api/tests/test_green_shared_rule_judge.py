"""显式绿色合同复用正式 Judge；原件、分项与批量不能被模型自报绕过。"""

from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeRequest,
    RuleJudgeService,
    RuleJudgeValidationError,
    resolve_rule_execution_route,
)
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)
from services.api.tests.test_rule_judge import (
    FakeObservationPacketService,
    FakeRuleJudgeRepository,
    SemanticJudgeMustNotRun,
)


def definition(key):
    payload = {**build_green_rule_payload(key), "rule_id": "green-rule"}
    return RuleJudgeDefinition(
        **{
            item.name: payload[item.name]
            for item in fields(RuleJudgeDefinition)
            if item.name in payload
        }
    )


def service(rule):
    artifact = TrustedEvidenceArtifact(
        "artifact-1",
        "screenshot",
        "capture-1",
        "primary",
        "image/png",
        b"controlled-unit-placeholder",
    )
    reader = SimpleNamespace(
        prepare_observation_packet=AsyncMock(
            return_value=TrustedEvidenceBundle(
                "packet-1",
                "capture-1",
                {"primary": "capture-1"},
                (artifact,),
            )
        )
    )
    judge = RuleJudgeService(
        repository=FakeRuleJudgeRepository(rule),
        evidence_reader=reader,
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=SemanticJudgeMustNotRun(),
    )
    request = RuleJudgeRequest(
        task_id="task-1",
        rule_code=rule.rule_code,
        page_capture_id="capture-1",
        evidence_artifact_ids=("artifact-1",),
        judge_input={"green_pass": True, "complete": True},
    )
    return judge, request, reader


@pytest.mark.parametrize(
    "key,status",
    [
        ("102", "evidence_insufficient"),
        ("32", "evidence_insufficient"),
        ("41", "evidence_insufficient"),
    ],
)
async def test_page_contract_reads_originals_and_preserves_unproven_subitems(key, status):
    rule = definition(key)
    judge, request, reader = service(rule)
    result = await judge.judge(request)
    assert result.execution_status == status
    assert result.observation_packet_id == "packet-1"
    assert result.evidence_artifact_ids == ("artifact-1",)
    assert result.judge_output["missing_evidence"]
    assert [item["name"] for item in result.judge_output["subresults"]] == (
        rule.judge_config["green_rule_contract"]["subchecks"]
    )
    assert all(item["status"] == status for item in result.judge_output["subresults"])
    assert all(
        item["evaluated"] is False and item["confidence"] is None
        for item in result.judge_output["subresults"]
    )
    assert "green_pass" not in result.judge_input
    options = reader.prepare_observation_packet.call_args.kwargs
    assert options["frozen_observation_packet"].observation_packet_id == "packet-1"
    assert options["required_artifact_types"] == ("screenshot",)


async def test_batch_green_page_cannot_enter_old_visual_fallback():
    judge, request, reader = service(definition("32"))
    results = await judge.judge_many([request])
    assert [result.execution_status for result in results] == ["evidence_insufficient"]
    assert reader.prepare_observation_packet.await_count == 1


async def test_process_contract_cannot_be_judged_with_one_page():
    judge, request, reader = service(definition("s72"))
    with pytest.raises(RuleJudgeValidationError, match="正式过程输入"):
        await judge.judge(request)
    reader.prepare_observation_packet.assert_not_awaited()


async def test_tampered_frozen_skill_is_rejected_before_original_read():
    rule = definition("32")
    rule.judge_config["green_skill_program"]["combined_body"] += "伪造新版程序"
    judge, request, reader = service(rule)
    with pytest.raises(RuleJudgeValidationError, match="SHA256"):
        await judge.judge(request)
    reader.prepare_observation_packet.assert_not_awaited()


@pytest.mark.parametrize("key", ["102", "32", "41", "s72", "s35", "s49", "s73", "62"])
def test_registered_green_route_is_internal_fixed_dispatch(key):
    rule = definition(key)
    route = resolve_rule_execution_route(
        executor_type=rule.executor_type, judge_config=rule.judge_config
    )
    assert route.ready and route.handler == "green_rule_evidence"
    assert route.tool_name == "rule_judge_tool"
    assert route.handler_tool_name == "green_rule_evidence_tool"
