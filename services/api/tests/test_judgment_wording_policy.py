"""判定文案措辞硬约束：业务定性词在提示词失守时仍不得进入落库文案。"""

from __future__ import annotations

from services.api.app.application.judgment_wording_policy import (
    contains_business_characterization,
    enforce_plain_display_text,
    has_cjk_text,
    neutralize_business_characterizations,
    sanitize_semantic_judge_output,
)
from services.api.app.application.rule_execution import (
    RecordRuleExecutionCommand,
    RuleExecutionService,
)
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeService,
)
from services.api.app.application.semantic_rule_judge import (
    SemanticJudgeFinding,
    SemanticJudgeModelOutput,
)
from services.api.tests.test_rule_execution import RecordingRuleExecutionRepository
from services.api.tests.test_rule_judge import (
    FakeObservationPacketService,
    FakeRuleJudgeRepository,
    _request,
)

# ---------------------------------------------------------------------------
# 词级替换
# ---------------------------------------------------------------------------


def test_neutralize_replaces_every_forbidden_characterization() -> None:
    text = "该平台是灰产，涉及黑产、违法犯罪、非法引流、涉黄涉赌涉毒、诈骗传销洗钱。"
    cleaned = neutralize_business_characterizations(text)

    assert cleaned is not None
    for forbidden in (
        "灰产",
        "黑产",
        "违法",
        "非法",
        "犯罪",
        "涉黄",
        "涉赌",
        "涉毒",
        "诈骗",
        "传销",
        "洗钱",
    ):
        assert forbidden not in cleaned
    assert "违规" in cleaned


def test_neutralize_handles_compound_terms_without_leftover_fragments() -> None:
    cleaned = neutralize_business_characterizations("疑似黑灰产业链，存在黑灰产团伙")

    assert cleaned == "疑似违规内容，存在违规内容团伙"


def test_neutralize_keeps_clean_text_and_none_untouched() -> None:
    clean = "弹窗关闭按钮命中区域小于规则要求的 44px。"
    assert neutralize_business_characterizations(clean) == clean
    assert neutralize_business_characterizations(None) is None
    assert neutralize_business_characterizations("") == ""
    assert contains_business_characterization("页面包含违法内容") is True
    assert contains_business_characterization(clean) is False


# ---------------------------------------------------------------------------
# 语义 Judge 输出净化
# ---------------------------------------------------------------------------


def _dirty_model_output() -> SemanticJudgeModelOutput:
    return SemanticJudgeModelOutput(
        conclusion="fail",
        confidence=0.95,
        reasoning_summary="页面包含非法色情推广，平台涉嫌灰产运营。",
        findings=[
            SemanticJudgeFinding(
                summary="检测到黑产引流链接与诈骗话术",
                evidence_refs=["artifact-1"],
            )
        ],
        evidence_refs=["artifact-1"],
        missing_evidence=["缺少违法页面的完整截图"],
        conflicts=["工具认为合规但文本疑似洗钱"],
        review_reason="需人工确认是否属于黑灰产",
    )


def test_sanitize_semantic_output_cleans_copy_but_preserves_verdict() -> None:
    sanitized = sanitize_semantic_judge_output(_dirty_model_output())

    assert sanitized.conclusion == "fail"
    assert sanitized.confidence == 0.95
    assert sanitized.evidence_refs == ["artifact-1"]
    assert sanitized.findings[0].evidence_refs == ["artifact-1"]
    serialized = sanitized.model_dump_json()
    for forbidden in ("灰产", "黑产", "违法", "非法", "诈骗", "洗钱"):
        assert forbidden not in serialized
    assert "违规" in sanitized.reasoning_summary


def test_sanitize_semantic_output_returns_same_object_when_clean() -> None:
    clean = SemanticJudgeModelOutput(
        conclusion="pass",
        confidence=0.9,
        reasoning_summary="页面文案满足规则。",
        evidence_refs=["artifact-1"],
    )

    assert sanitize_semantic_judge_output(clean) is clean


# ---------------------------------------------------------------------------
# V2 语义判定链路：模型违反措辞禁令时落库文案仍干净
# ---------------------------------------------------------------------------


class _DirtySemanticJudge:
    async def judge(self, *, packet, tool_results):
        del packet, tool_results
        return _dirty_model_output()


async def test_semantic_judge_path_persists_neutralized_copy() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-10",
        rule_code="alioth_10",
        executor_type="llm",
        judge_config={
            "tool_refs": ["semantic_rule_judge_tool"],
            "capability_config": {"required_artifact_types": ["visible_text"]},
        },
        rule_name="提示文案检查",
        category="text",
        check_point="提示应易于理解",
        manual_review_policy={"min_confidence": 0.8},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=_DirtySemanticJudge(),
    )

    result = await service.judge(_request(rule_code="alioth_10", judge_input={}))

    assert result.execution_status == "fail"
    for text in (
        result.result_summary or "",
        result.problem_desc or "",
        str(result.judge_output),
    ):
        for forbidden in ("灰产", "黑产", "违法", "非法", "诈骗", "洗钱"):
            assert forbidden not in text
    assert "违规" in (result.result_summary or "")


# ---------------------------------------------------------------------------
# Result Writer 收口：任何写入路径的可见文案统一中性化
# ---------------------------------------------------------------------------


async def test_rule_execution_writer_neutralizes_visible_copy() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_1",
            execution_status="skipped",
            result_summary="产品疑似灰产，存在违法推广。",
            problem_desc="页面包含黑产引流与非法内容。",
            review_reason="疑似诈骗，需人工确认。",
        )
    )

    assert len(repository.commands) == 1
    persisted = repository.commands[0]
    assert persisted.result_summary == "产品疑似违规内容，存在违规推广。"
    assert persisted.problem_desc == "页面包含违规内容引流与违规内容。"
    assert persisted.review_reason == "疑似风险内容，需人工确认。"


# ---------------------------------------------------------------------------
# 大白话兜底：英文整段与内部工具名不得进入运营可见文案
# ---------------------------------------------------------------------------


def test_enforce_plain_display_text_scrubs_and_collapses_tool_names() -> None:
    text = (
        "广告源素材与落地页未形成可判定的成对证据："
        "landing_page_probe_tool、safe_click_observer_tool 与 "
        "visual_semantic_consistency_tool 均未返回配对结果。"
    )
    cleaned = enforce_plain_display_text(text, fallback="详情见证据")

    assert cleaned is not None
    assert "_tool" not in cleaned
    assert "检测工具、检测工具" not in cleaned
    assert "检测工具" in cleaned
    assert cleaned.startswith("广告源素材与落地页未形成可判定的成对证据：")


def test_enforce_plain_display_text_falls_back_when_no_chinese() -> None:
    english = "Deterministic geometry check on five ad-context media elements passed"
    assert enforce_plain_display_text(english, fallback="检查通过") == "检查通过"


def test_enforce_plain_display_text_keeps_clean_chinese_and_empty() -> None:
    clean = "跳过按钮命中区域 44px，符合规则要求（https://example.com/page）。"
    assert enforce_plain_display_text(clean, fallback="兜底") == clean
    assert enforce_plain_display_text(None, fallback="兜底") is None
    assert enforce_plain_display_text("", fallback="兜底") == ""
    assert has_cjk_text(clean) is True
    assert has_cjk_text("plain english") is False
    assert has_cjk_text(None) is False


async def test_rule_execution_writer_enforces_plain_chinese_copy() -> None:
    repository = RecordingRuleExecutionRepository()
    service = RuleExecutionService(repository=repository)

    await service.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            rule_code="alioth_1",
            execution_status="skipped",
            result_summary="Deterministic geometry check reported zero violations",
            problem_desc=(
                "证据由 landing_page_probe_tool、safe_click_observer_tool 采集，未形成配对。"
            ),
        )
    )

    assert len(repository.commands) == 1
    persisted = repository.commands[0]
    assert persisted.result_summary == "规则未执行"
    assert "landing_page_probe_tool" not in (persisted.problem_desc or "")
    assert "检测工具、检测工具" not in (persisted.problem_desc or "")
    assert "检测工具" in (persisted.problem_desc or "")
