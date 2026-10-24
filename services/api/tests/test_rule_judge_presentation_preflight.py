"""判定侧展示层预检：底层被遮挡或诊断材料不进入规则判定。"""

from __future__ import annotations

from services.api.app.application.observation_packet import (
    ObservationPacketValidationError,
)
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeService,
)
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_PROTOCOL_VERSION,
    PresentationAssessment,
)
from services.api.tests.test_rule_judge import (
    CountingSemanticJudge,
    FakeObservationPacketService,
    FakeRuleJudgeEvidenceReader,
    FakeRuleJudgeRepository,
    _request,
)


def _presentation(**overrides: object) -> dict:
    payload = {
        "protocol_version": PRESENTATION_PROTOCOL_VERSION,
        "topmost_layer_kind": "overlay",
        "base_page_visibility": "fully_occluded",
        "blocking_scope": "page",
        "mutation_mode": "natural",
        "overlay_signature": "recharge-offer",
        "hit_test_status": "available",
        "evidence_sources": ["h5_ui_snapshot"],
        "diagnostic_operations": [],
        "uncertainty": [],
    }
    payload.update(overrides)
    return payload


class PresentationObservationPacketService(FakeObservationPacketService):
    def __init__(self, presentation: dict | None) -> None:
        self.presentation = presentation

    async def build(self, request):
        packet = await super().build(request)
        if self.presentation is not None:
            packet.packet_payload["facts"]["presentation"] = self.presentation
        return packet


def _hybrid_definition(*, category: str, judge_config_extra: dict | None = None):
    return RuleJudgeDefinition(
        rule_id=f"rule-{category}",
        rule_code=f"alioth_{category}",
        executor_type="hybrid",
        judge_config={
            "tool_refs": ["visible_text_extractor_tool", "semantic_rule_judge_tool"],
            "capability_config": {
                "deterministic_tools": ["visible_text_extractor_tool"],
                "semantic_judge": "semantic_rule_judge_tool",
                "required_artifact_types": ["ui_snapshot", "visible_text"],
            },
            **(judge_config_extra or {}),
        },
        rule_name="页面文案检查" if category != "ad" else "弹窗广告关闭行为检查",
        category=category,
        check_point="页面文案应当规范" if category != "ad" else "弹窗广告点击后需关闭",
        input_contract={"required_artifacts": ["ui_snapshot", "visible_text"]},
    )


async def _judge(definition, presentation, semantic_judge):
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=PresentationObservationPacketService(presentation),
        semantic_judge=semantic_judge,
    )
    return await service.judge(_request(rule_code=definition.rule_code, judge_input={}))


async def test_base_page_rule_is_insufficient_when_base_page_is_occluded() -> None:
    semantic_judge = CountingSemanticJudge()

    result = await _judge(_hybrid_definition(category="text"), _presentation(), semantic_judge)

    assert result.execution_status == "evidence_insufficient"
    assert semantic_judge.call_count == 0
    tool_result = result.judge_output["tool_results"][0]
    assert tool_result["tool_name"] == "presentation_preflight"
    assert tool_result["status"] == "rejected"
    assert tool_result["reason_code"] == "BASE_PAGE_OCCLUDED"
    assert tool_result["layer_target"] == "base_page"
    assert "recharge-offer" in result.problem_desc
    assert result.judge_output["evidence_trusted"] is False
    assert result.judge_output["missing_evidence"] == ["关闭遮挡后、底层页面真实可见的当前页面证据"]
    assert result.judge_output["presentation_rejection"]["reason_code"] == "BASE_PAGE_OCCLUDED"


async def test_ad_rule_still_judges_the_overlay_frame() -> None:
    semantic_judge = CountingSemanticJudge()

    result = await _judge(_hybrid_definition(category="ad"), _presentation(), semantic_judge)

    assert result.execution_status != "evidence_insufficient"
    assert semantic_judge.call_count == 1


async def test_rule_declared_layer_target_overrides_category_default() -> None:
    semantic_judge = CountingSemanticJudge()
    definition = _hybrid_definition(
        category="text", judge_config_extra={"evidence_layer_target": "any"}
    )

    result = await _judge(definition, _presentation(), semantic_judge)

    assert result.execution_status != "evidence_insufficient"
    assert semantic_judge.call_count == 1


async def test_diagnostic_material_is_rejected_even_for_ad_rules() -> None:
    semantic_judge = CountingSemanticJudge()
    diagnostic = _presentation(
        topmost_layer_kind="page",
        base_page_visibility="visible",
        blocking_scope="none",
        overlay_signature=None,
        mutation_mode="instrumented_diagnostic",
        diagnostic_operations=[
            {"kind": "style_hide_obstruction", "source": "verified_high_z_obstruction_hide"}
        ],
    )

    result = await _judge(_hybrid_definition(category="ad"), diagnostic, semantic_judge)

    assert result.execution_status == "evidence_insufficient"
    assert semantic_judge.call_count == 0
    assert result.judge_output["tool_results"][0]["reason_code"] == "DIAGNOSTIC_EVIDENCE_EXCLUDED"
    assert "用样式隐藏弹窗" in result.problem_desc


async def test_packets_without_presentation_or_with_visible_page_are_untouched() -> None:
    for presentation in (
        None,
        _presentation(
            topmost_layer_kind="page",
            base_page_visibility="visible",
            blocking_scope="none",
            overlay_signature=None,
        ),
    ):
        semantic_judge = CountingSemanticJudge()
        result = await _judge(_hybrid_definition(category="text"), presentation, semantic_judge)
        assert result.execution_status != "evidence_insufficient"
        assert semantic_judge.call_count == 1


async def test_batch_judgement_applies_the_same_preflight() -> None:
    semantic_judge = CountingSemanticJudge()
    definition = _hybrid_definition(category="text")
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=PresentationObservationPacketService(_presentation()),
        semantic_judge=semantic_judge,
    )

    results = await service.judge_many([_request(rule_code=definition.rule_code, judge_input={})])

    assert [item.execution_status for item in results] == ["evidence_insufficient"]
    assert results[0].judge_output["tool_results"][0]["tool_name"] == "presentation_preflight"
    assert semantic_judge.call_count == 0


async def test_deterministic_handler_path_shares_the_preflight() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_6",
        rule_code="alioth_6",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "technical_error_text",
                "pattern": "JSON Parse Error|HTTP\\s*500|NullPointerException",
            }
        },
        rule_name="技术错误直出检查",
        category="visual",
        check_point="不应出现明显技术错误页/错误码直出",
    )

    # 底页被弹窗遮住：被挡住的 DOM 里的错误码不能当作用户可见的直出
    gated = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader("HTTP 500"),
        observation_packet_service=PresentationObservationPacketService(_presentation()),
    )
    blocked = await gated.judge(_request(rule_code="alioth_6", judge_input={}))
    assert blocked.execution_status == "evidence_insufficient"
    assert blocked.judge_output["tool_results"][0]["tool_name"] == "presentation_preflight"

    # 没有配置判定包服务的旧接线保持原行为
    legacy = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader("HTTP 500"),
    )
    judged = await legacy.judge(_request(rule_code="alioth_6", judge_input={}))
    assert judged.execution_status == "fail"


class UnavailableObservationPacketService(FakeObservationPacketService):
    """收口补判场景：采集不属于当前判定 Run，观察包建不出来。"""

    async def build(self, request):
        raise ObservationPacketValidationError("页面采集不属于当前证据生产 Run")


class PresentationEvidenceReader(FakeRuleJudgeEvidenceReader):
    """可信证据包带回持久化的展示层评估（读取器 09-08 起随包返回）。"""

    def __init__(self, visible_text: str, presentation: dict | None) -> None:
        super().__init__(visible_text)
        self.presentation = presentation

    async def prepare_observation_packet(self, **kwargs):
        bundle = await super().prepare_observation_packet(**kwargs)
        bundle.capture_presentations = {
            kwargs["page_capture_id"]: PresentationAssessment.from_dict(self.presentation)
        }
        return bundle


def _technical_error_definition() -> RuleJudgeDefinition:
    return RuleJudgeDefinition(
        rule_id="rule-alioth_6",
        rule_code="alioth_6",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "technical_error_text",
                "pattern": "JSON Parse Error|HTTP\\s*500|NullPointerException",
            }
        },
        rule_name="技术错误直出检查",
        category="visual",
        check_point="不应出现明显技术错误页/错误码直出",
    )


async def test_deterministic_path_reads_bundle_presentation_when_packet_unavailable() -> None:
    """观察包建不出来时不能绕过门禁：收口补判曾用样式隐藏弹窗后的页面把规则判成 pass。"""
    definition = _technical_error_definition()
    diagnostic = _presentation(
        topmost_layer_kind="page",
        base_page_visibility="visible",
        blocking_scope="none",
        mutation_mode="instrumented_diagnostic",
        overlay_signature=None,
        diagnostic_operations=[
            {"kind": "style_hide_obstruction", "source": "verified_high_z_obstruction_hide"}
        ],
    )

    gated = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=PresentationEvidenceReader("HTTP 500", diagnostic),
        observation_packet_service=UnavailableObservationPacketService(),
    )
    blocked = await gated.judge(_request(rule_code="alioth_6", judge_input={}))

    assert blocked.execution_status == "evidence_insufficient"
    assert blocked.judge_output["presentation_rejection"]["reason_code"] == (
        "DIAGNOSTIC_EVIDENCE_EXCLUDED"
    )
    assert blocked.judge_output["tool_results"][0]["tool_name"] == "presentation_preflight"
    assert blocked.observation_packet_id == "packet-1"
    assert "evidence_gap" in blocked.judge_output

    # 自然状态、底页可见的采集照常判定，兜底门禁不会误拒。
    natural = _presentation(
        topmost_layer_kind="page",
        base_page_visibility="visible",
        blocking_scope="none",
        overlay_signature=None,
    )
    passthrough = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=PresentationEvidenceReader("HTTP 500", natural),
        observation_packet_service=UnavailableObservationPacketService(),
    )
    judged = await passthrough.judge(_request(rule_code="alioth_6", judge_input={}))
    assert judged.execution_status == "fail"

    # 读取器没带展示层评估（旧接线）时保持原行为。
    legacy = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader("HTTP 500"),
        observation_packet_service=UnavailableObservationPacketService(),
    )
    untouched = await legacy.judge(_request(rule_code="alioth_6", judge_input={}))
    assert untouched.execution_status == "fail"
