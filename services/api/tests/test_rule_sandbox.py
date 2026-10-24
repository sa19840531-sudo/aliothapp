"""规则测试沙箱：人工证据走真实 RuleJudgeService，只替换证据来源与语义模型。"""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.rule_sandbox import (
    OUTCOME_NOT_EXECUTABLE,
    RuleSandboxEvidenceError,
    RuleSandboxRuleNotFoundError,
    RuleSandboxService,
)
from services.api.app.application.rule_sandbox_evidence import (
    RuleSandboxEvidence,
    SandboxCheckMode,
    SandboxPlatform,
    SandboxScreenshot,
)
from services.api.app.application.semantic_rule_judge import (
    SemanticJudgeFinding,
    SemanticJudgeModelOutput,
)
from services.api.app.infrastructure.rule_sandbox_evidence_reader import (
    SandboxRuleJudgeEvidenceReader,
)

HYBRID_RULE_ID = "00000000-0000-0000-0000-000000000012"
ENGINE_RULE_ID = "00000000-0000-0000-0000-000000000006"
AGGREGATE_RULE_ID = "00000000-0000-0000-0000-000000000099"
APP_RULE_ID = "00000000-0000-0000-0000-000000000077"
CHANGE_RULE_ID = "00000000-0000-0000-0000-000000000020"
PRICE_RULE_ID = "00000000-0000-0000-0000-000000000013"
VISUAL_RULE_ID = "00000000-0000-0000-0000-000000000031"


def _png(width: int = 12, height: int = 20, color: tuple[int, int, int] = (200, 30, 30),
         *, content=True) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), color)
    if content:
        # 正例包含真实像素内容；纯色空白图另作为拒绝反例，不伪装成业务截图。
        draw = ImageDraw.Draw(image)
        for x in range(0, width, 4):
            for y in range(0, height, 4):
                if (x + y) % 8 == 0:
                    draw.rectangle((x, y, x + 1, y + 1), fill=(20, 210, 230))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _screenshot(
    name: str = "shot.png",
    width: int = 12,
    height: int = 20,
    color: tuple[int, int, int] = (200, 30, 30),
) -> SandboxScreenshot:
    return SandboxScreenshot(
        filename=name, payload=_png(width, height, color), width=width, height=height
    )


async def test_plain_blank_image_is_not_business_evidence_or_sent_to_judge():
    judge = FakeSemanticJudge(_fail_output())
    blank = SandboxScreenshot(
        filename="blank.png", payload=_png(content=False), width=12, height=20,
    )
    result = await _service(judge, _visual_row()).run(
        rule_id=VISUAL_RULE_ID,
        evidence=_h5_evidence(page_type="video_detail", visible_text="", screenshots=(blank,)),
    )
    assert result.outcome == "evidence_insufficient"
    assert not judge.calls


def _row(rule_id: str, rule_code: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": rule_id,
        "ruleset_id": "ruleset-1",
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_name": "首期规则集",
        "ruleset_version": 1,
        "ruleset_status": "enabled",
        "rule_code": rule_code,
        "name": f"规则 {rule_code}",
        "category": "text",
        "executor_type": "hybrid",
        "severity": "medium",
        "status": "enabled",
        "applicable_page_types": ["content_detail"],
        "url_pattern": None,
        "steps": [],
        "check_point": "提示文字不应使用叹号结尾",
        "input_contract": {},
        "judge_config": {},
        "manual_review_policy": {"min_confidence": 0.82},
    }
    row.update(overrides)
    return row


def _hybrid_row(**overrides: Any) -> dict[str, Any]:
    return _row(
        HYBRID_RULE_ID,
        "alioth_12",
        input_contract={
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": ["screenshot", "ui_snapshot", "dom", "visible_text"],
            "quality_gate": {"min_screenshot_count": 1, "allow_empty_visible_text": False},
        },
        judge_config={
            "llm_checks": ["提示文字不应使用叹号结尾"],
            "tool_refs": ["semantic_rule_judge_tool"],
            "capability_config": {
                "deterministic_tools": ["punctuation_assertion_handler"],
                "semantic_judge": "semantic_rule_judge_tool",
            },
        },
        **overrides,
    )


def _engine_row(**overrides: Any) -> dict[str, Any]:
    return _row(
        ENGINE_RULE_ID,
        "alioth_6",
        name="技术错误直出检查",
        category="visual",
        executor_type="rule_engine",
        applicable_page_types=["home", "video_list"],
        input_contract={
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": ["visible_text"],
            "quality_gate": {"min_screenshot_count": 0},
            "required_capture_roles": ["primary"],
        },
        judge_config={
            "tool_refs": ["deterministic_assertion_tool"],
            "engine_config": {
                "handler": "technical_error_text",
                "input_field": "visible_text",
                "pattern": "(?i)(Internal Server Error|HTTP\\s*500)",
            },
        },
        **overrides,
    )


def _change_row(**overrides: Any) -> dict[str, Any]:
    """与 alioth_20 同形：比「操作前 / 操作后」两张截图有没有变化。"""
    row = _row(
        CHANGE_RULE_ID,
        "alioth_20",
        name="弹窗广告关闭行为检查",
        category="ad",
        executor_type="rule_engine",
        applicable_page_types=["home"],
        input_contract={
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": ["screenshot"],
            "quality_gate": {"min_screenshot_count": 1, "allow_empty_visible_text": True},
            "required_capture_roles": ["before", "primary"],
        },
        judge_config={
            "tool_refs": ["deterministic_assertion_tool"],
            "engine_config": {
                "handler": "screenshot_change_assertion",
                "before_role": "before",
                "after_role": "primary",
            },
        },
    )
    row.update(overrides)
    return row


def _price_row() -> dict[str, Any]:
    """与 alioth_13 同形：同一商品在两个位置的价格要一致。"""
    return _row(
        PRICE_RULE_ID,
        "alioth_13",
        name="商品价格一致性检查",
        category="payment",
        executor_type="rule_engine",
        applicable_page_types=["payment"],
        input_contract={
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": ["visible_text"],
            "quality_gate": {"min_screenshot_count": 0, "allow_empty_visible_text": False},
            "required_capture_roles": ["primary", "location_1"],
        },
        judge_config={
            "tool_refs": ["deterministic_assertion_tool"],
            "engine_config": {
                "handler": "price_relation",
                "tolerance": 0.01,
                "price_pattern": (
                    "(?im)(?P<product>[^\\n]{1,80}?)\\s*(?:¥|￥|RMB\\s*)"
                    "(?P<price>\\d{1,9}(?:\\.\\d{1,2})?)"
                ),
                "min_shared_products": 1,
                "location_role_pattern": "^(primary|location_\\d+)$",
                "min_locations_per_product": 2,
            },
        },
    )


class FakeRuleSource:
    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows = {row["id"]: row for row in rows}

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        return self.rows.get(rule_id)

    async def list_rules_for_sandbox(self) -> list[dict[str, Any]]:
        return list(self.rows.values())


class FakeSemanticJudge:
    def __init__(self, output: SemanticJudgeModelOutput | Exception) -> None:
        self.output = output
        self.calls: list[tuple[Any, list[dict[str, Any]]]] = []

    async def judge(self, *, packet: Any, tool_results: list[dict[str, Any]]):
        self.calls.append((packet, tool_results))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output

    async def judge_batch(self, *, items):  # pragma: no cover - 单条测试不走批次
        raise AssertionError("规则测试只判一条规则，不应走批次")


def _service(
    judge: FakeSemanticJudge,
    *rows: dict[str, Any],
    screenshot_text_reader: Any = None,
    qr_validator: Any = None,
) -> RuleSandboxService:
    return RuleSandboxService(
        rules=FakeRuleSource(*rows),
        evidence_reader_factory=SandboxRuleJudgeEvidenceReader,
        semantic_judge_factory=lambda: judge,
        screenshot_text_reader=screenshot_text_reader,
        qr_validator=qr_validator,
    )


def _fail_output() -> SemanticJudgeModelOutput:
    return SemanticJudgeModelOutput(
        conclusion="fail",
        confidence=0.9,
        reasoning_summary="提示文案以叹号结尾",
        findings=[
            SemanticJudgeFinding(
                summary="提示文案「操作成功！」以叹号结尾",
                evidence_refs=["screenshot-1"],
            )
        ],
        evidence_refs=["screenshot-1"],
    )


def _h5_evidence(**overrides: Any) -> RuleSandboxEvidence:
    values: dict[str, Any] = {
        "platform": SandboxPlatform.H5,
        "page_type": "content_detail",
        "page_url": "https://example.test/detail/1",
        "visible_text": "操作成功！",
        "screenshots": (_screenshot(),),
    }
    values.update(overrides)
    return RuleSandboxEvidence(**values)


async def test_relaxed_hybrid_rule_judges_given_evidence_and_lists_formal_gaps() -> None:
    judge = FakeSemanticJudge(_fail_output())
    service = _service(judge, _hybrid_row())

    result = await service.run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence())

    assert len(judge.calls) == 1
    packet, _tool_results = judge.calls[0]
    assert packet.quality_gate_status == "pass"
    assert packet.packet_payload["facts"]["visible_text"] == "操作成功！"
    assert packet.packet_payload["page"]["page_type"] == "content_detail"
    assert packet.packet_payload["rule"]["input_contract"] == _hybrid_row()["input_contract"]
    assert packet.packet_payload["quality_gate"]["sandbox_provided_materials_only"] is True
    # 截图按 PNG 原样交给模型，引用名就是测试页的证据名
    assert packet.image_payloads[0][0] == "screenshot-1"
    assert packet.image_payloads[0][1].startswith(b"\x89PNG")
    assert result.outcome == "fail"
    assert result.check_mode == "relaxed"
    assert result.confidence == pytest.approx(0.9)
    assert result.findings[0].evidence == ("截图 1（shot.png）",)
    assert "截图 1（shot.png）" in result.used_evidence
    assert any(
        "正式任务还要求：界面快照（页面元素结构）、页面源码（HTML）" in notice
        for notice in result.notices
    )


@pytest.mark.parametrize("kind,label", [
    ("har", "网络请求记录"),
    ("network_trace", "网络请求记录"),
    ("action_trace", "操作记录"),
    ("action_receipt", "action_receipt"),
    ("temporal_trace", "连续观察记录"),
    ("media_state", "播放器状态"),
])
async def test_relaxed_sandbox_cannot_replace_explicit_nonvisual_facts_with_uploads(kind, label):
    contract = {"required_artifacts": ["screenshot", kind],
                "required_capture_roles": ["before", "primary"]}
    row = _row(HYBRID_RULE_ID, "causal-rule", executor_type="llm", input_contract=contract,
               judge_config={"llm_checks": ["核对合同事实"]})
    judge = FakeSemanticJudge(SemanticJudgeModelOutput(
        conclusion="pass", confidence=1.0, reasoning_summary="不能被调用的替身通过结果",
    ))
    result = await _service(judge, row).run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence(
        screenshots=(_screenshot(), _screenshot("second.png")),
        before_screenshot=_screenshot("before.png"),
    ))
    assert result.outcome == "evidence_insufficient"
    assert result.missing_evidence == (label,)
    assert judge.calls == []
    assert row["input_contract"] == contract


async def test_relaxed_measurement_rule_keeps_original_obligations_visible_to_semantic_judge():
    contract = {"required_artifacts": ["screenshot", "ui_snapshot"],
                "required_capture_roles": ["primary", "before"],
                "measurement": {"unit": "px", "minimum": 12, "allow_estimation": False}}
    row = _row(HYBRID_RULE_ID, "measurement-rule", executor_type="llm", input_contract=contract,
               judge_config={"llm_checks": ["必须使用同对象像素测量"]})
    judge = FakeSemanticJudge(SemanticJudgeModelOutput(
        conclusion="evidence_insufficient", confidence=0.0,
        reasoning_summary="截图不能替代精确测量", missing_evidence=["缺少同对象像素测量"],
    ))
    result = await _service(judge, row).run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence())
    assert result.outcome == "evidence_insufficient"
    packet, _ = judge.calls[0]
    assert packet.packet_payload["rule"]["input_contract"] == contract
    assert packet.packet_payload["quality_gate"]["sandbox_provided_materials_only"] is True
    assert packet.packet_payload["rule"]["input_contract"]["required_capture_roles"] == [
        "primary", "before",
    ]


@pytest.mark.parametrize("kind", ["ui_tree", "ocr_text"])
async def test_relaxed_visual_review_keeps_auxiliary_structure_optional_and_original_contract(kind):
    contract = {"required_artifacts": ["screenshot", kind]}
    row = _row(HYBRID_RULE_ID, "visual-rule", executor_type="llm", input_contract=contract,
               judge_config={"llm_checks": ["只按画面判断文字是否完整"]})
    judge = FakeSemanticJudge(SemanticJudgeModelOutput(
        conclusion="pass", confidence=0.95, reasoning_summary="纯视觉对象清楚可判",
        evidence_refs=["screenshot-1"],
    ))
    result = await _service(judge, row).run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence())
    assert result.outcome == "pass"
    assert judge.calls[0][0].packet_payload["rule"]["input_contract"] == contract


async def test_relaxed_sandbox_preserves_projection_gap_before_real_semantic_adapter_cache():
    from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
        LLMSemanticRuleJudge,
    )
    from services.api.tests.test_semantic_rule_judge import FakeStructuredClient

    row = _hybrid_row()
    row["input_contract"]["measurement_obligation"] = "必须测量，不得目测" * 100
    client = FakeStructuredClient({
        "conclusion": "pass", "confidence": 1.0, "reasoning_summary": "不能被调用的通过结果",
    })
    judge = LLMSemanticRuleJudge(client=client, manual_test=True)
    result = await _service(judge, row).run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence())
    assert result.outcome == "manual_review"
    assert client.calls == []
    assert result.missing_evidence == ()
    assert result.judge_output["model_output"]["conflicts"] == ["rule_input_contract_truncated"]


async def test_strict_mode_blocks_on_formal_evidence_requirements_without_model_call() -> None:
    judge = FakeSemanticJudge(_fail_output())
    service = _service(judge, _hybrid_row())

    result = await service.run(
        rule_id=HYBRID_RULE_ID,
        evidence=_h5_evidence(),
        check_mode=SandboxCheckMode.STRICT,
    )

    assert judge.calls == []
    assert result.outcome == "evidence_insufficient"
    assert result.check_mode == "strict"
    assert "界面快照（页面元素结构）" in result.missing_evidence
    assert "页面源码（HTML）" in result.missing_evidence
    assert result.problem_desc is not None
    assert result.problem_desc.startswith("缺少这条规则要求的证据：")


async def test_low_confidence_pass_turns_into_manual_review_with_threshold() -> None:
    judge = FakeSemanticJudge(
        SemanticJudgeModelOutput(
            conclusion="pass",
            confidence=0.5,
            reasoning_summary="未发现叹号结尾的提示",
            evidence_refs=["screenshot-1"],
        )
    )
    service = _service(judge, _hybrid_row())

    result = await service.run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence())

    assert result.outcome == "manual_review"
    assert result.confidence_threshold == pytest.approx(0.82)
    assert result.problem_desc is not None
    assert "把握度" in result.problem_desc
    assert "置信度" not in result.problem_desc


async def test_model_line_failure_shows_reason_without_fake_confidence() -> None:
    judge = FakeSemanticJudge(
        SemanticJudgeModelOutput(
            conclusion="manual_review",
            confidence=0.0,
            reasoning_summary="模型 API 暂时不可用，未生成自动判定结论。",
            conflicts=["semantic_provider_temporarily_unavailable"],
            review_reason="模型 API 暂时不可用，未生成自动判定结论。",
        )
    )
    service = _service(judge, _hybrid_row())

    result = await service.run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence())

    assert result.outcome == "manual_review"
    assert result.confidence is None
    assert result.problem_desc is not None
    assert "模型 API 暂时不可用" in result.problem_desc


async def test_rule_engine_rule_runs_deterministic_handler_on_uploaded_text() -> None:
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型"))
    service = _service(judge, _engine_row())

    failed = await service.run(
        rule_id=ENGINE_RULE_ID,
        evidence=_h5_evidence(page_type="home", visible_text="出错了 Internal Server Error"),
    )
    passed = await service.run(
        rule_id=ENGINE_RULE_ID,
        evidence=_h5_evidence(page_type="home", visible_text="欢迎回来"),
    )

    assert judge.calls == []
    assert failed.outcome == "fail"
    assert failed.result_summary == "发现技术错误直出"
    assert passed.outcome == "pass"


async def test_rule_engine_rule_is_never_relaxed_and_reports_missing_type() -> None:
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型"))
    service = _service(judge, _engine_row())

    result = await service.run(
        rule_id=ENGINE_RULE_ID,
        evidence=_h5_evidence(page_type="home", visible_text=""),
    )

    assert result.outcome == "evidence_insufficient"
    assert result.problem_desc is not None
    assert "可见文本" in result.problem_desc
    assert any("规则引擎类规则始终按正式任务的证据要求检查" in item for item in result.notices)


async def test_aggregate_rule_is_reported_as_not_testable_without_judging() -> None:
    judge = FakeSemanticJudge(_fail_output())
    aggregate = _row(
        AGGREGATE_RULE_ID,
        "alioth_ops_8",
        judge_config={
            "llm_checks": ["官方群入口"],
            "capability_config": {
                "evidence_input_scope": "task",
                "evidence_scope_key": "official_group_entry",
            },
        },
    )
    service = _service(judge, aggregate)

    result = await service.run(rule_id=AGGREGATE_RULE_ID, evidence=_h5_evidence())

    assert judge.calls == []
    assert result.outcome == OUTCOME_NOT_EXECUTABLE
    assert result.problem_desc is not None
    assert "暂不支持单页测试" in result.problem_desc


async def test_notices_flag_where_formal_flow_would_not_pick_the_rule() -> None:
    judge = FakeSemanticJudge(_fail_output())
    row = _hybrid_row(
        status="disabled",
        applicable_page_types=["video_detail"],
        url_pattern="https://example.test/video/*",
    )
    row["input_contract"] = {**row["input_contract"], "target_clients": ["APP"]}
    service = _service(judge, row)

    result = await service.run(
        rule_id=HYBRID_RULE_ID,
        evidence=_h5_evidence(page_type="home", page_url="https://other.test/"),
    )

    notices = "\n".join(result.notices)
    assert "「停用」状态" in notices
    assert "只在 App 端跑，你选的是 H5" in notices
    assert "「视频详情页」" in notices and "「首页」" in notices
    assert "https://example.test/video/*" in notices


async def test_unexpected_judge_error_is_returned_as_error_outcome() -> None:
    judge = FakeSemanticJudge(RuntimeError("模型网关断了"))
    service = _service(judge, _hybrid_row())

    result = await service.run(rule_id=HYBRID_RULE_ID, evidence=_h5_evidence())

    assert result.outcome == "error"
    assert result.problem_desc is not None
    assert "模型网关断了" in result.problem_desc


async def test_missing_rule_and_empty_evidence_are_rejected() -> None:
    service = _service(FakeSemanticJudge(_fail_output()), _hybrid_row())

    with pytest.raises(RuleSandboxRuleNotFoundError):
        await service.run(rule_id=ENGINE_RULE_ID, evidence=_h5_evidence())
    with pytest.raises(RuleSandboxEvidenceError):
        await service.run(
            rule_id=HYBRID_RULE_ID,
            evidence=RuleSandboxEvidence(platform=SandboxPlatform.H5),
        )


async def test_app_identity_comes_from_package_name_and_real_screenshot_size() -> None:
    app_rule = _row(
        APP_RULE_ID,
        "alioth_app_1",
        executor_type="llm",
        input_contract={
            "required_artifacts": ["screenshot"],
            "quality_gate": {"min_screenshot_count": 1},
        },
        judge_config={"llm_checks": ["页面不应白屏"], "tool_refs": ["semantic_rule_judge_tool"]},
    )
    judge = FakeSemanticJudge(
        SemanticJudgeModelOutput(conclusion="pass", confidence=0.95, reasoning_summary="页面正常",
                                 evidence_refs=["screenshot-1"])
    )
    service = _service(judge, app_rule)
    app_evidence = RuleSandboxEvidence(
        platform=SandboxPlatform.ANDROID,
        app_package_name="com.demo.app",
        screenshots=(_screenshot(width=30, height=60),),
    )

    passed = await service.run(
        rule_id=APP_RULE_ID, evidence=app_evidence, check_mode=SandboxCheckMode.STRICT
    )
    missing_package = await service.run(
        rule_id=APP_RULE_ID,
        evidence=RuleSandboxEvidence(
            platform=SandboxPlatform.ANDROID,
            screenshots=(_screenshot(width=30, height=60),),
        ),
        check_mode=SandboxCheckMode.STRICT,
    )

    relaxed_without_package = await service.run(
        rule_id=APP_RULE_ID,
        evidence=RuleSandboxEvidence(
            platform=SandboxPlatform.ANDROID,
            screenshots=(_screenshot(width=30, height=60),),
        ),
    )

    assert passed.outcome == "pass"
    identity = judge.calls[0][0].packet_payload["facts"]["app_identity"]
    assert identity["package_name"] == "com.demo.app"
    assert identity["safe_area"]["width"] == 30
    assert missing_package.outcome == "evidence_insufficient"
    assert "App 包名" in missing_package.missing_evidence
    # 放宽口径照样判，但要说清正式任务还要包名
    assert relaxed_without_package.outcome == "pass"
    assert any("App 包名" in notice for notice in relaxed_without_package.notices)


async def test_options_describe_evidence_needs_and_untestable_rules() -> None:
    aggregate = _row(
        AGGREGATE_RULE_ID,
        "alioth_ops_8",
        judge_config={
            "llm_checks": ["官方群入口"],
            "capability_config": {
                "evidence_input_scope": "task",
                "evidence_scope_key": "official_group_entry",
            },
        },
    )
    broken = _row(
        APP_RULE_ID,
        "alioth_broken",
        executor_type="rule_engine",
        input_contract={"required_capture_roles": ["primary", "before"]},
        judge_config={"engine_config": {"handler": "no_such_handler"}},
    )
    service = _service(FakeSemanticJudge(_fail_output()), _hybrid_row(), aggregate, broken)

    options = {item.rule_code: item for item in await service.list_options()}

    hybrid = options["alioth_12"]
    assert hybrid.testable is True
    assert hybrid.relaxable is True
    assert hybrid.min_screenshot_count == 1
    needs = {need.artifact_type: need for need in hybrid.evidence_needs}
    assert needs["ui_snapshot"].label == "界面快照（页面元素结构）"
    assert needs["ui_snapshot"].form_supported is True
    assert options["alioth_ops_8"].testable is False
    assert "暂不支持单页测试" in (options["alioth_ops_8"].untestable_reason or "")
    assert options["alioth_broken"].config_problem is not None
    assert options["alioth_broken"].relaxable is False
    assert any("操作前页面" in note for note in options["alioth_broken"].notes)
    assert hybrid.judge_input_fields == ()


async def test_before_screenshot_lets_before_after_rule_compare() -> None:
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型"))
    service = _service(judge, _change_row())
    after = _screenshot("after.png", color=(10, 10, 10))

    changed = await service.run(
        rule_id=CHANGE_RULE_ID,
        evidence=_h5_evidence(
            page_type="home",
            visible_text="",
            screenshots=(after,),
            before_screenshot=_screenshot("before.png", color=(250, 250, 250)),
        ),
    )
    unchanged = await service.run(
        rule_id=CHANGE_RULE_ID,
        evidence=_h5_evidence(
            page_type="home",
            visible_text="",
            screenshots=(after,),
            before_screenshot=_screenshot("before.png", color=(10, 10, 10)),
        ),
    )
    without_before = await service.run(
        rule_id=CHANGE_RULE_ID,
        evidence=_h5_evidence(page_type="home", visible_text="", screenshots=(after,)),
    )

    assert judge.calls == []
    assert changed.outcome == "pass"
    assert set(changed.used_evidence) == {"截图 1（after.png）", "操作前的截图（before.png）"}
    assert unchanged.outcome == "fail"
    assert without_before.outcome == "evidence_insufficient"
    assert without_before.problem_desc is not None
    assert "操作前页面" in without_before.problem_desc


async def test_before_after_rule_takes_exactly_one_after_screenshot() -> None:
    service = _service(FakeSemanticJudge(_fail_output()), _change_row())

    result = await service.run(
        rule_id=CHANGE_RULE_ID,
        evidence=_h5_evidence(
            page_type="home",
            screenshots=(_screenshot("a.png"), _screenshot("b.png", color=(1, 2, 3))),
            before_screenshot=_screenshot("before.png", color=(250, 250, 250)),
        ),
    )

    assert result.outcome == "evidence_insufficient"
    assert result.problem_desc is not None
    assert "只能放 1 张" in result.problem_desc


def _visual_row(**overrides: Any) -> dict[str, Any]:
    """与 alioth_31 同形：模型按操作前 / 操作后 / 刷新后几张截图看，没有程序判。"""
    return _row(
        VISUAL_RULE_ID,
        "alioth_31",
        name="评论发布与计数同步检查",
        category="interaction",
        executor_type="hybrid",
        applicable_page_types=["video_detail"],
        input_contract={
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": ["screenshot"],
            "quality_gate": {"min_screenshot_count": 1, "allow_empty_visible_text": True},
            "required_capture_roles": ["before", "primary", "refreshed"],
        },
        judge_config={
            "deterministic_rules": ["发评论后评论应上墙且计数 +1"],
            "llm_checks": ["按发评论前后的截图看评论有没有上墙"],
            "tool_refs": ["page_capture_tool", "semantic_rule_judge_tool"],
            "capability_config": {
                "deterministic_tools": [],
                "semantic_judge": "semantic_rule_judge_tool",
                "required_artifact_types": ["screenshot"],
            },
        },
        **overrides,
    )


async def test_before_screenshot_goes_to_model_first_for_visual_rule() -> None:
    """模型看图的规则：操作前那张不单独成组，放到第 1 张和页面截图一起按先后顺序交给模型。"""
    judge = FakeSemanticJudge(_fail_output())
    service = _service(judge, _visual_row())
    before = _screenshot("before.png", color=(250, 250, 250))

    result = await service.run(
        rule_id=VISUAL_RULE_ID,
        evidence=_h5_evidence(
            page_type="video_detail",
            visible_text="",
            screenshots=(
                _screenshot("after.png", color=(10, 10, 10)),
                _screenshot("refreshed.png", color=(20, 20, 20)),
            ),
            before_screenshot=before,
        ),
    )

    assert result.outcome == "fail"
    assert len(judge.calls) == 1
    packet, _ = judge.calls[0]
    assert [image_id for image_id, _, _ in packet.image_payloads] == [
        "screenshot-1",
        "screenshot-2",
        "screenshot-3",
    ]
    assert packet.image_payloads[0][1] == before.payload
    assert result.used_evidence == (
        "截图 1（before.png）",
        "截图 2（after.png）",
        "截图 3（refreshed.png）",
    )
    assert any("已放在第 1 张" in note for note in result.notices)
    assert not any("用不到" in note for note in result.notices)


async def test_second_location_text_lets_price_rule_compare() -> None:
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型"))
    service = _service(judge, _price_row())

    def evidence(second: str) -> RuleSandboxEvidence:
        return _h5_evidence(
            page_type="payment",
            screenshots=(),
            visible_text="月卡 ¥30\n季卡 ¥80",
            second_location_text=second,
        )

    same = await service.run(rule_id=PRICE_RULE_ID, evidence=evidence("开通月卡 ¥30\n月卡 ¥30"))
    different = await service.run(rule_id=PRICE_RULE_ID, evidence=evidence("月卡 ¥25"))
    missing = await service.run(rule_id=PRICE_RULE_ID, evidence=evidence(""))

    assert judge.calls == []
    assert same.outcome == "pass"
    assert "第二个位置的页面文字" in same.used_evidence
    assert different.outcome == "fail"
    assert missing.outcome == "evidence_insufficient"
    assert missing.problem_desc is not None
    assert "价格位置1" in missing.problem_desc


async def test_extra_evidence_the_rule_does_not_use_is_left_out_with_a_notice() -> None:
    judge = FakeSemanticJudge(_fail_output())
    service = _service(judge, _hybrid_row())

    result = await service.run(
        rule_id=HYBRID_RULE_ID,
        evidence=_h5_evidence(
            before_screenshot=_screenshot("before.png"),
            second_location_text="月卡 ¥30",
        ),
    )

    packet, _tool_results = judge.calls[0]
    assert [item[0] for item in packet.image_payloads] == ["screenshot-1"]
    notices = "\n".join(result.notices)
    assert "用不到「操作前的截图」" in notices
    assert "用不到「第二个位置的页面文字」" in notices


async def test_options_offer_extra_inputs_only_when_they_make_the_rule_testable() -> None:
    needs_trace = _change_row(
        id="00000000-0000-0000-0000-000000000026",
        rule_code="alioth_26",
        input_contract={
            "required_artifacts": ["visible_text", "action_trace", "ui_snapshot"],
            "required_capture_roles": ["before", "primary"],
        },
    )
    needs_refresh = _change_row(
        id="00000000-0000-0000-0000-000000000008",
        rule_code="alioth_8",
        input_contract={
            "required_artifacts": ["screenshot"],
            "required_capture_roles": ["before", "primary", "refreshed"],
        },
    )
    service = _service(
        FakeSemanticJudge(_fail_output()),
        _change_row(),
        _price_row(),
        needs_trace,
        needs_refresh,
        _hybrid_row(),
    )

    options = {item.rule_code: item for item in await service.list_options()}

    change_inputs = options["alioth_20"].extra_inputs
    assert [(item.role, item.kind, item.label) for item in change_inputs] == [
        ("before", "screenshot", "操作前的截图")
    ]
    assert [(item.role, item.kind, item.label) for item in options["alioth_13"].extra_inputs] == [
        ("location_1", "text", "第二个位置的页面文字")
    ]
    # 还缺测试页给不了的证据（操作记录、刷新后页面），放出输入框也测不了
    assert options["alioth_26"].extra_inputs == ()
    assert options["alioth_8"].extra_inputs == ()
    assert options["alioth_12"].extra_inputs == ()


async def test_options_expose_structured_judge_input_fields() -> None:
    """正式任务里由采集程序结构化填好的字段（如优惠价、原价），测试页要放出补充输入框。"""
    price = _row(
        APP_RULE_ID,
        "alioth_14",
        executor_type="rule_engine",
        judge_config={
            "tool_refs": ["deterministic_assertion_tool", "price_extractor_tool"],
            "engine_config": {
                "handler": "discount_price_compare",
                "operator": "lt",
                "discount_price_field": "discount_price",
                "original_price_field": "original_price",
                "pass_when_discount_absent": True,
            },
        },
    )
    service = _service(FakeSemanticJudge(_fail_output()), price)

    options = {item.rule_code: item for item in await service.list_options()}

    assert options["alioth_14"].judge_input_fields == ("discount_price", "original_price")


class _FakeTextReader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, screenshots) -> str:
        self.calls.append(tuple(item.filename for item in screenshots))
        return self.text


async def test_rule_engine_rule_reads_text_from_screenshots_when_only_images_are_given() -> None:
    """只传了截图：先让模型把字抄出来，规则引擎类规则就能跑，并说明文字是读出来的。"""
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型判定"))
    reader = _FakeTextReader("页面提示：Internal Server Error 请稍后再试")
    service = _service(judge, _engine_row(), screenshot_text_reader=reader)

    result = await service.run(
        rule_id=ENGINE_RULE_ID,
        evidence=_h5_evidence(
            page_type="home", visible_text="", screenshots=(_screenshot("home.png"),)
        ),
    )

    assert reader.calls == [("home.png",)]
    assert result.outcome == "fail"
    assert result.result_summary == "发现技术错误直出"
    assert any("从你传的截图里读出来" in item for item in result.notices)
    # 判定用的文字原样带回，记录和页面都能看到模型到底读出了什么
    assert result.screenshot_text == "页面提示：Internal Server Error 请稍后再试"


async def test_screenshot_text_is_not_read_when_text_is_given_or_rule_can_see_images() -> None:
    judge = FakeSemanticJudge(_fail_output())
    reader = _FakeTextReader("不该被用到")
    service = _service(judge, _engine_row(), _hybrid_row(), screenshot_text_reader=reader)

    with_text = await service.run(
        rule_id=ENGINE_RULE_ID, evidence=_h5_evidence(page_type="home", visible_text="欢迎回来")
    )
    hybrid = await service.run(
        rule_id=HYBRID_RULE_ID,
        evidence=_h5_evidence(visible_text="", screenshots=(_screenshot(),)),
    )

    assert reader.calls == []
    assert with_text.outcome == "pass"
    assert hybrid.outcome == "fail"  # 混合规则自己看图判，不多花一次抄写
    assert with_text.screenshot_text is None and hybrid.screenshot_text is None


async def test_rule_engine_rule_stays_insufficient_when_nothing_can_be_read_from_screenshots() -> (
    None
):
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型判定"))
    reader = _FakeTextReader("   ")
    service = _service(judge, _engine_row(), screenshot_text_reader=reader)

    result = await service.run(
        rule_id=ENGINE_RULE_ID,
        evidence=_h5_evidence(page_type="home", visible_text="", screenshots=(_screenshot(),)),
    )

    assert reader.calls == [("shot.png",)]
    assert result.outcome == "evidence_insufficient"
    assert any("没能从截图里读出文字" in item for item in result.notices)


AD_PRESENCE_RULE_ID = "00000000-0000-0000-0000-000000000025"
QR_RULE_ID = "00000000-0000-0000-0000-000000000041"


def _ad_presence_row(**overrides: Any) -> dict[str, Any]:
    """与 alioth_25 同形：页面文字只是可选附件，程序从结构或文字里找广告特征。"""
    return _row(
        AD_PRESENCE_RULE_ID,
        "alioth_25",
        name="播放前广告展示检查",
        category="ad",
        executor_type="rule_engine",
        applicable_page_types=["video_detail"],
        input_contract={
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": [],
            "optional_artifacts": ["screenshot", "ui_snapshot", "dom", "visible_text"],
            "quality_gate": {"min_screenshot_count": 0, "allow_empty_visible_text": True},
            "required_capture_roles": ["primary"],
        },
        judge_config={
            "tool_refs": ["ad_region_detector_tool"],
            "engine_config": {"handler": "visible_ad_presence_handler"},
        },
        **overrides,
    )


def _qr_row(**overrides: Any) -> dict[str, Any]:
    """与 alioth_41 同形：只从带二维码语义的元素或文字里取网址再校验。"""
    return _row(
        QR_RULE_ID,
        "alioth_41",
        name="二维码与网址有效性检查",
        category="interaction",
        executor_type="rule_engine",
        applicable_page_types=["profile"],
        input_contract={
            "target_clients": ["WEB", "APP", "H5"],
            "required_artifacts": [],
            "optional_artifacts": ["screenshot", "ui_snapshot", "dom", "visible_text"],
            "quality_gate": {"min_screenshot_count": 0, "allow_empty_visible_text": True},
            "required_capture_roles": ["primary"],
        },
        judge_config={
            "tool_refs": ["qr_link_validation_tool"],
            "engine_config": {"handler": "qr_url_validation", "input_field": "url"},
        },
        **overrides,
    )


def _fake_qr_validator(decoded_url: str | None) -> Any:
    """替代真实网址校验：传网址按可达算，传截图当解出 decoded_url（None 表示没认出二维码）。"""
    calls: list[dict[str, Any]] = []

    async def validator(
        *,
        url: str | None = None,
        image_base64: str | None = None,
        timeout_seconds: float = 8.0,
        max_redirects: int = 5,
    ) -> dict[str, Any]:
        calls.append({"url": url, "from_image": image_base64 is not None})
        target = url or decoded_url
        if target is None:
            return {"is_valid": False, "issues": [], "url_checks": [], "decoded_texts": []}
        return {
            "is_valid": True,
            "issues": [],
            "decoded_texts": [target],
            "url_checks": [{"url": target, "is_valid": True, "status_code": 200}],
        }

    validator.calls = calls  # type: ignore[attr-defined]
    return validator


async def test_screenshot_text_is_read_when_visible_text_is_only_optional() -> None:
    """页面文字只是可选附件的程序判规则（alioth_25 那类）：只传截图也先读字，否则永远判不足。"""
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型判定"))
    reader = _FakeTextReader("同城约炮 开元棋牌 PG电子 大放水")
    service = _service(judge, _ad_presence_row(), screenshot_text_reader=reader)

    result = await service.run(
        rule_id=AD_PRESENCE_RULE_ID,
        evidence=_h5_evidence(
            page_type="video_detail", visible_text="", screenshots=(_screenshot("play.png"),)
        ),
    )

    assert reader.calls == [("play.png",)]
    assert result.outcome == "pass"
    assert result.screenshot_text == "同城约炮 开元棋牌 PG电子 大放水"


async def test_screenshot_text_is_not_read_when_rule_never_uses_visible_text() -> None:
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型判定"))
    reader = _FakeTextReader("不该被用到")
    row = _ad_presence_row()
    row["input_contract"]["optional_artifacts"] = ["screenshot", "ui_snapshot"]
    service = _service(judge, row, screenshot_text_reader=reader)

    result = await service.run(
        rule_id=AD_PRESENCE_RULE_ID,
        evidence=_h5_evidence(
            page_type="video_detail", visible_text="", screenshots=(_screenshot(),)
        ),
    )

    assert reader.calls == []
    assert result.outcome == "evidence_insufficient"


async def test_qr_rule_judges_from_screenshot_alone_with_the_judges_own_decoder() -> None:
    """只传截图的二维码规则：不再被「缺界面快照」挡住，判定程序自己解码截图再校验网址。"""
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型判定"))
    validator = _fake_qr_validator("https://example.com/login?token=abc")
    service = _service(judge, _qr_row(), qr_validator=validator)

    result = await service.run(
        rule_id=QR_RULE_ID,
        evidence=_h5_evidence(
            page_type="profile", visible_text="", screenshots=(_screenshot("qr.png"),)
        ),
    )

    assert result.outcome == "pass"
    assert any(call["from_image"] for call in validator.calls)
    assert not any("界面快照" in item for item in result.notices)


async def test_qr_rule_without_a_readable_qr_code_is_skipped_not_guessed() -> None:
    judge = FakeSemanticJudge(AssertionError("规则引擎类规则不应调用模型判定"))
    service = _service(judge, _qr_row(), qr_validator=_fake_qr_validator(None))

    result = await service.run(
        rule_id=QR_RULE_ID,
        evidence=_h5_evidence(page_type="profile", visible_text="", screenshots=(_screenshot(),)),
    )

    assert result.outcome == "skipped"
    assert "二维码" in f"{result.result_summary} {getattr(result, 'problem_desc', '')}"
