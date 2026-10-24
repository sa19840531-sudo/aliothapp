"""判定侧证据缺口：每个「证据不足」出口都要说清缺哪个页面、状态、区域、关闭前后。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.api.app.agents.rule_judge.normalization import normalize_rule_judge_outcome
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeService,
    RuleJudgeValidationError,
)
from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
from services.api.app.domain.inspection import EvidenceBundleReference
from services.api.app.domain.inspection.evidence_gap import (
    EVIDENCE_GAP_PROTOCOL_VERSION,
    assert_gap_has_no_operation_details,
)
from services.api.tests.test_maf_rule_judge_agent import _handoff
from services.api.tests.test_rule_judge import (
    CountingSemanticJudge,
    FakeObservationPacketService,
    FakeRuleJudgeEvidenceReader,
    FakeRuleJudgeRepository,
    MissingRequiredEvidenceReader,
    PageStateObservationPacketService,
    _request,
)
from services.api.tests.test_rule_judge_presentation_preflight import (
    PresentationObservationPacketService,
    _hybrid_definition,
    _presentation,
)


class InsufficientSemanticJudge:
    """模型自己判证据不足，并按提示词要求说明缺的事实。"""

    async def judge(self, *, packet, tool_results):
        del packet, tool_results
        return SemanticJudgeModelOutput(
            conclusion="evidence_insufficient",
            confidence=0.0,
            reasoning_summary="缺少播放器加载完成后的画面。",
            missing_evidence=["播放器区域加载完成后的截图", "视频播放 3 秒后的页面状态"],
            review_reason="材料不足",
        )


def _service(definition, packet_service, semantic_judge):
    return RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=packet_service,
        semantic_judge=semantic_judge,
    )


@pytest.mark.parametrize("failure", ["empty_text", "missing_artifact"])
@pytest.mark.parametrize("platform,surface", [("h5", "web"), ("web", "web"), ("app", "android")])
@pytest.mark.parametrize("state", ["popup", "stable"])
async def test_engine_evidence_failure_retains_only_h5_page_context(failure, platform, surface, state):
    definition = RuleJudgeDefinition(
        rule_id="rule-1", rule_code="alioth_visual_1", executor_type="rule_engine",
        judge_config={"engine_config": {"handler": "technical_error_text", "pattern": "HTTP 500"}},
        input_contract={"recapture_policy": "equivalent"},
    )

    class Packets(PageStateObservationPacketService):
        async def build(self, request):
            packet = await super().build(request)
            packet.packet_payload["identity"] = {"target_platform": platform}
            packet.packet_payload["page"].update(surface_kind=surface, page_type="video_list")
            return packet

    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=Packets(state),
        evidence_reader=(FakeRuleJudgeEvidenceReader("") if failure == "empty_text"
                         else MissingRequiredEvidenceReader()),
    )
    result = await service.judge(_request(rule_code=definition.rule_code, judge_input={}))
    assert result.execution_status == "evidence_insufficient"
    gap = result.judge_output["evidence_gap"]
    target = gap["target"]
    assert target["page_capture_id"] == "capture-1"
    is_h5 = platform == "h5" and surface == "web"
    assert target["page_url"] == ("https://example.com" if is_h5 else "")
    assert target["page_type"] == ("video_list" if is_h5 else "")
    assert target["observed_page_state"] == (state if is_h5 else "")
    if is_h5:
        assert result.observation_packet_id == "packet-1"
        assert gap["observation_packet_id"] == "packet-1"
        handoff = replace(_handoff(), rule_evidence={definition.rule_code: EvidenceBundleReference(
            page_capture_id="capture-1", producer_agent_run_id="producer-run-1",
            artifact_ids=("artifact-1",),
        )})
        outcome = normalize_rule_judge_outcome(handoff=handoff, raw={"items": [{
            "rule_code": definition.rule_code, "execution_status": result.execution_status,
            "page_capture_id": result.page_capture_id, "judge_output": result.judge_output,
        }]})
        assert len(outcome.evidence_requests) == 1
        invariants = outcome.evidence_requests[0].target_invariants
        assert invariants["business"] == {"page_type": "video_list", "url_pattern": "https://example.com"}
        assert invariants["semantic"]["page_state"] == state
        assert invariants["structure"]["layer_target"] == "base_page"


@pytest.mark.parametrize("variation", ["supplement", "no_manifest", "foreign_task", "foreign_rule", "foreign_capture", "foreign_page"])
async def test_engine_missing_evidence_context_is_bound_to_h5_request(variation):
    definition = RuleJudgeDefinition(
        rule_id="rule-1", rule_code="alioth_visual_1", executor_type="rule_engine",
        judge_config={"engine_config": {"handler": "technical_error_text", "pattern": "HTTP 500"}},
        input_contract={"recapture_policy": "equivalent"},
    )

    class Packets(PageStateObservationPacketService):
        async def build(self, request):
            packet = await super().build(request)
            packet.packet_payload["identity"] = {"target_platform": "h5"}
            packet.packet_payload["page"].update(
                surface_kind="web", page_type="video_list",
                page_capture_id="foreign" if variation == "foreign_page" else "capture-2",
            )
            changed_field = {
                "foreign_task": "task_id", "foreign_rule": "rule_id",
                "foreign_capture": "page_capture_id",
            }.get(variation)
            return replace(packet, **{changed_field: "foreign"}) if changed_field else packet

    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=Packets("stable"),
        evidence_reader=MissingRequiredEvidenceReader(),
    )
    request = replace(
        _request(rule_code=definition.rule_code, judge_input={"page_type": "untrusted"}),
        evidence_source_page_capture_id="capture-2",
        evidence_manifest_id=None if variation == "no_manifest" else "manifest-2",
    )
    if variation == "no_manifest":
        with pytest.raises(RuleJudgeValidationError, match="必须同时提供"):
            await service.judge(request)
        return
    result = await service.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert result.confidence == 0.0
    assert result.page_capture_id == "capture-1"
    gap = result.judge_output["evidence_gap"]
    assert gap["target"]["page_capture_id"] == ("capture-2" if variation == "supplement" else "capture-1")
    assert gap["target"]["page_type"] == ("video_list" if variation == "supplement" else "")
    if variation == "supplement":
        assert gap["target"]["observed_page_state"] == "stable"
        assert result.judge_output["evidence_gap_source"]["manifest_id"] == "manifest-2"
        handoff = replace(_handoff(), rule_evidence={definition.rule_code: EvidenceBundleReference(
            page_capture_id="capture-2", producer_agent_run_id="producer-run-1",
            manifest_id="manifest-2", manifest_version="2", artifact_ids=("artifact-1",),
        )})
        outcome = normalize_rule_judge_outcome(handoff=handoff, raw={"items": [{
            "rule_code": definition.rule_code, "execution_status": result.execution_status,
            "page_capture_id": result.page_capture_id, "judge_output": result.judge_output,
        }]})
        assert len(outcome.evidence_requests) == 1
        assert outcome.evidence_requests[0].target_capture_id == "capture-2"
        assert outcome.evidence_requests[0].target_invariants["semantic"]["page_state"] == "stable"
    else:
        assert "evidence_gap_source" not in result.judge_output


async def test_occluded_page_gap_names_page_region_and_after_close_phase() -> None:
    definition = _hybrid_definition(category="text")
    service = _service(
        definition, PresentationObservationPacketService(_presentation()), CountingSemanticJudge()
    )

    result = await service.judge(_request(rule_code=definition.rule_code, judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    gap = result.judge_output["evidence_gap"]
    assert gap["protocol_version"] == EVIDENCE_GAP_PROTOCOL_VERSION
    assert gap["rule_code"] == definition.rule_code
    assert gap["task_id"] == "task-1"
    assert gap["target"]["page_capture_id"] == "capture-1"
    assert gap["target"]["page_url"] == "https://example.com"
    assert gap["target"]["region"]["layer_target"] == "base_page"
    assert gap["target"]["region"]["label"].startswith("页面可见文案：")
    assert gap["overlay_phase"] == "after_close"
    assert gap["missing"]["presentation"]["code"] == "BASE_PAGE_OCCLUDED"
    assert gap["missing"]["judge_statements"] == ["关闭遮挡后、底层页面真实可见的当前页面证据"]
    assert gap["recapture_policy"] == "manual"
    assert gap["disposition"] == "manual_review_required"
    assert gap["requested_request"] is None
    assert "弹窗关闭后" in gap["evidence_goal"]
    assert_gap_has_no_operation_details(gap)


async def test_missing_capture_roles_gap_lists_roles_and_request_when_rule_declares_policy() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-8",
        rule_code="alioth_8",
        executor_type="hybrid",
        judge_config={
            "tool_refs": ["visible_text_extractor_tool", "semantic_rule_judge_tool"],
            "capability_config": {
                "deterministic_tools": ["visible_text_extractor_tool"],
                "semantic_judge": "semantic_rule_judge_tool",
                "required_artifact_types": ["ui_snapshot", "visible_text"],
            },
        },
        rule_name="操作状态即时反馈检测",
        category="interaction",
        check_point="关注、收藏、点赞即时反馈",
        input_contract={
            "required_capture_roles": ["before", "primary", "refreshed"],
            "recapture_policy": "equivalent",
        },
    )
    semantic_judge = CountingSemanticJudge()
    service = _service(definition, FakeObservationPacketService(), semantic_judge)

    result = await service.judge(
        _request(
            rule_code="alioth_8",
            judge_input={},
            evidence_capture_roles={"primary": "capture-1"},
        )
    )

    assert result.execution_status == "evidence_insufficient"
    assert semantic_judge.call_count == 0
    gap = result.judge_output["evidence_gap"]
    assert gap["missing"]["capture_roles"] == ["before", "refreshed"]
    assert gap["target"]["region"]["label"].startswith("交互控件与反馈区域：")
    assert gap["overlay_phase"] == "not_applicable"
    assert gap["recapture_policy"] == "equivalent"
    assert gap["disposition"] == "recorded_only"
    request_preview = gap["requested_request"]
    assert request_preview["request_id"].startswith("evidence-request:")
    assert request_preview["replay_policy"] == "same_context_allowed"
    assert request_preview["required_equivalence"] == "logical_equivalent"
    assert request_preview["side_effect_policy"] == "read_only"
    assert "缺采集角色 before、refreshed" in gap["evidence_goal"]


async def test_model_declared_insufficiency_keeps_its_statements_in_the_gap() -> None:
    definition = _hybrid_definition(category="player")
    service = _service(
        definition,
        PageStateObservationPacketService("stable"),
        InsufficientSemanticJudge(),
    )

    result = await service.judge(_request(rule_code=definition.rule_code, judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    gap = result.judge_output["evidence_gap"]
    assert gap["target"]["observed_page_state"] == "stable"
    assert gap["target"]["region"]["label"].startswith("播放器区域：")
    assert gap["missing"]["judge_statements"] == [
        "播放器区域加载完成后的截图",
        "视频播放 3 秒后的页面状态",
    ]
    assert "播放器区域加载完成后的截图" in gap["evidence_goal"]


async def test_pass_and_skip_results_carry_no_gap() -> None:
    definition = _hybrid_definition(category="text")
    passed = await _service(
        definition, FakeObservationPacketService(), CountingSemanticJudge()
    ).judge(_request(rule_code=definition.rule_code, judge_input={}))

    assert passed.execution_status == "pass"
    assert "evidence_gap" not in passed.judge_output


async def test_gap_is_stable_for_the_same_capture_rule_and_shortfall() -> None:
    definition = _hybrid_definition(category="text")
    packet_service = PresentationObservationPacketService(_presentation())
    first = await _service(definition, packet_service, CountingSemanticJudge()).judge(
        _request(rule_code=definition.rule_code, judge_input={})
    )
    second = await _service(definition, packet_service, CountingSemanticJudge()).judge(
        _request(rule_code=definition.rule_code, judge_input={})
    )

    assert first.judge_output["evidence_gap"]["gap_id"] == second.judge_output["evidence_gap"]["gap_id"]


@pytest.mark.parametrize("platform,surface", [("h5", "web"), ("web", "web"), ("app", "android"), ("h5", "android")])
async def test_h5_followup_gap_targets_exact_supplement_without_moving_result(platform, surface):
    definition = replace(
        _hybrid_definition(category="player"),
        rule_id="rule-1",
        rule_code="alioth_visual_1",
        input_contract={"recapture_policy": "equivalent"},
    )

    class Packets(PageStateObservationPacketService):
        async def build(self, request):
            packet = await super().build(request)
            packet.packet_payload["identity"] = {"target_platform": platform}
            packet.packet_payload["page"].update(
                surface_kind=surface, page_capture_id="capture-2", page_type="content_detail",
            )
            return packet

    result = await _service(definition, Packets("loaded"), InsufficientSemanticJudge()).judge(
        replace(
            _request(rule_code=definition.rule_code, judge_input={}),
            agent_run_id="consumer-run-1",
            evidence_source_page_capture_id="capture-2",
            evidence_manifest_id="manifest-2",
        )
    )
    is_h5 = platform == "h5" and surface == "web"
    assert result.page_capture_id == "capture-1"
    assert result.judge_output["evidence_gap"]["target"]["page_capture_id"] == (
        "capture-2" if is_h5 else "capture-1"
    )
    handoff = replace(
        _handoff(),
        rule_evidence={definition.rule_code: EvidenceBundleReference(
            page_capture_id="capture-2", producer_agent_run_id="producer-run-1",
            manifest_id="manifest-2", manifest_version="2", artifact_ids=("artifact-1",),
        )},
    )
    outcome = normalize_rule_judge_outcome(handoff=handoff, raw={"items": [{
        "rule_code": definition.rule_code, "execution_status": result.execution_status,
        "page_capture_id": result.page_capture_id, "judge_output": result.judge_output,
    }]})
    assert len(outcome.evidence_requests) == 1
    assert outcome.evidence_requests[0].target_capture_id == ("capture-2" if is_h5 else "capture-1")
    assert outcome.evidence_requests[0].target_invariants["semantic"]["page_state"] == "loaded"
    if is_h5:
        bad_output = {**result.judge_output, "evidence_gap_source": {
            **result.judge_output["evidence_gap_source"], "manifest_id": "foreign-manifest",
        }}
        rejected = normalize_rule_judge_outcome(handoff=handoff, raw={"items": [{
            "rule_code": definition.rule_code, "execution_status": "evidence_insufficient",
            "judge_output": bad_output,
        }]})
        assert rejected.evidence_requests == ()
        assert rejected.decisions[0].payload["judge_output"]["evidence_request_rejection"]["reason_code"] == "EVIDENCE_REQUEST_CONTRACT_INVALID"


@pytest.mark.parametrize("platform", ["h5", "web"])
async def test_only_manifest_bound_h5_missing_text_gap_completes_capture_profile(platform):
    definition = RuleJudgeDefinition(
        rule_id="rule-1", rule_code="alioth_visual_1", executor_type="rule_engine",
        judge_config={"engine_config": {"handler": "technical_error_text", "pattern": "HTTP 500"}},
        input_contract={"recapture_policy": "equivalent"},
    )

    class Packets(PageStateObservationPacketService):
        async def build(self, request):
            packet = await super().build(request)
            packet.packet_payload["identity"] = {"target_platform": platform}
            packet.packet_payload["page"].update(surface_kind="web", page_type="video_list")
            packet.packet_payload["quality_gate"]["missing_sources"] = ["visible_text(non_empty)"]
            return packet

    class Reader(FakeRuleJudgeEvidenceReader):
        async def prepare_observation_packet(self, **kwargs):
            kwargs.pop("evidence_source_page_capture_id")
            kwargs.pop("evidence_manifest_id")
            return await super().prepare_observation_packet(**kwargs)

    result = await RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=Packets("stable"),
        evidence_reader=Reader(""),
    ).judge(replace(
        _request(rule_code=definition.rule_code, judge_input={}),
        evidence_source_page_capture_id="capture-1", evidence_manifest_id="manifest-1",
    ))
    handoff = replace(_handoff(), rule_evidence={definition.rule_code: EvidenceBundleReference(
        page_capture_id="capture-1", producer_agent_run_id="producer-run-1",
        manifest_id="manifest-1", manifest_version="1", artifact_ids=("artifact-1",),
    )})
    outcome = normalize_rule_judge_outcome(handoff=handoff, raw={"items": [{
        "rule_code": definition.rule_code, "execution_status": result.execution_status,
        "page_capture_id": result.page_capture_id, "judge_output": result.judge_output,
    }]})
    assert len(outcome.evidence_requests) == 1
    assert ("visible_text" in outcome.evidence_requests[0].required_artifact_types) is (platform == "h5")
    completion = outcome.deferred_decisions[0].payload["judge_output"].get(
        "evidence_request_profile_completion"
    )
    if platform == "h5":
        assert completion["request_id"] == outcome.evidence_requests[0].request_id
        assert completion["requirement_hash"] == outcome.evidence_requests[0].requirement_hash
        assert completion["original_request_id"] != completion["request_id"]
        assert "visible_text" in completion["required_artifact_types"]
    else:
        assert completion is None
    assert "evidence_request_profile_completion" not in result.judge_output
