"""真实静态视觉证据从观察包进入单条及批量语义判定的回归。"""

from dataclasses import replace

import pytest

from services.api.app.application.observation_packet import (
    ManifestQualityProjection,
    ObservationPacketService,
)
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeRequest,
    RuleJudgeService,
)
from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
from services.api.tests.test_observation_packet import (
    FakeObservationPacketRepository,
    _static_visual_app_source,
    _static_visual_request,
)


class StaticVisualRuleRepository:
    async def get_rule_judge_definition(self, *, task_id, rule_code):
        request = _static_visual_request(rule_code)
        return RuleJudgeDefinition(
            rule_id=f"rule-{rule_code}", rule_code=rule_code,
            rule_name="静态视觉检查", category="visual", executor_type="hybrid",
            input_contract=request.input_contract,
            judge_config={
                **request.judge_config,
                "tool_refs": ["visual_rule_judge_tool"],
                "llm_checks": ["界面文字应具有清晰层次"],
                "capability_config": {
                    **request.judge_config["capability_config"],
                    "deterministic_tools": ["text_style_extractor_tool"],
                },
            },
        )


class RecordingVisualJudge:
    def __init__(self):
        self.packets = []
        self.batch_calls = 0

    async def judge(self, *, packet, tool_results):
        self.packets.append(packet)
        assert packet.image_payloads[0][0] == "visual-shot"
        return SemanticJudgeModelOutput(
            conclusion="fail", confidence=0.95,
            reasoning_summary="标题与正文缺少可分辨的层次",
            evidence_refs=["visual-shot"],
        )

    async def judge_batch(self, *, items):
        self.batch_calls += 1
        return {
            packet.rule_code: await self.judge(packet=packet, tool_results=tools)
            for packet, tools in items
        }


def _judge_request(rule_code="alioth_spec_24"):
    return RuleJudgeRequest(
        task_id="task-1", page_capture_id="capture-1", rule_code=rule_code,
        evidence_artifact_ids=(), judge_input={},
        agent_run_id="consumer-run", source_agent_run_id="producer-run-1",
    )


async def test_png_only_static_visual_input_reaches_single_judge_without_auto_passing():
    judge = RecordingVisualJudge()
    service = RuleJudgeService(
        repository=StaticVisualRuleRepository(), semantic_judge=judge,
        observation_packet_service=ObservationPacketService(
            repository=FakeObservationPacketRepository(_static_visual_app_source()),
        ),
    )

    result = await service.judge(_judge_request())

    assert result.execution_status == "fail"
    assert result.judge_output["model_output"]["evidence_refs"] == ["visual-shot"]
    assert len(judge.packets) == 1
    assert judge.packets[0].packet_payload["facts"]["ui_snapshot"]["elements"] == []


async def test_png_only_static_visual_input_reaches_batch_judge():
    judge = RecordingVisualJudge()
    service = RuleJudgeService(
        repository=StaticVisualRuleRepository(), semantic_judge=judge,
        observation_packet_service=ObservationPacketService(
            repository=FakeObservationPacketRepository(_static_visual_app_source()),
        ),
    )

    results = await service.judge_many([
        _judge_request("alioth_spec_24"), _judge_request("alioth_spec_27"),
    ])

    assert [result.execution_status for result in results] == ["fail", "fail"]
    assert judge.batch_calls == 1
    assert {packet.rule_code for packet in judge.packets} == {"alioth_spec_24", "alioth_spec_27"}


@pytest.mark.parametrize("authoritative", [False, True])
@pytest.mark.parametrize("status", ["fail", "pending"])
async def test_manifest_rejection_never_reaches_static_visual_judge(authoritative, status):
    judge = RecordingVisualJudge()
    source = replace(_static_visual_app_source(), manifest_quality=ManifestQualityProjection(
        manifest_id="manifest-1", manifest_version=2, quality_status=status,
        quality_reason="清单证据尚未通过完整性校验",
    ))
    service = RuleJudgeService(
        repository=StaticVisualRuleRepository(), semantic_judge=judge,
        manifest_quality_authoritative=authoritative,
        observation_packet_service=ObservationPacketService(
            repository=FakeObservationPacketRepository(source),
        ),
    )

    result = await service.judge(_judge_request())

    assert result.execution_status == ("evidence_insufficient" if status == "fail" else "error")
    assert judge.packets == []
    assert result.judge_input["manifest_read"]["quality_status"] == status
    assert result.judge_input["manifest_read"]["manifest_id"] == "manifest-1"
    assert result.problem_desc == "清单证据尚未通过完整性校验"


async def test_png_only_combined_image_quality_rule_still_requires_structural_evidence():
    judge = RecordingVisualJudge()
    service = RuleJudgeService(
        repository=StaticVisualRuleRepository(), semantic_judge=judge,
        observation_packet_service=ObservationPacketService(
            repository=FakeObservationPacketRepository(_static_visual_app_source()),
        ),
    )

    result = await service.judge(_judge_request("alioth_3"))

    assert result.execution_status == "evidence_insufficient"
    assert judge.packets == []
