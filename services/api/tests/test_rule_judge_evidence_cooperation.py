"""判定侧配合探索证据补全（2026-09-21 拍板落地）。

- 证据清单显式字段：登录态（session_auth）不满足规则要求时记不适用；
- 平台不做的采样方式（多浏览器、跨端）记不适用；
- 「遇到才产出」的采集角色（40 的下架来源页）没出现时记不适用；
- 24 启动广告假跳过：旅程里没有可判的启动广告时记不适用，有才交模型；
- 29 防刷：App 采不到网络记录时只看限频提示，H5 / Web 仍硬性要 har。
"""

from __future__ import annotations

import pytest

from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeService,
    RuleJudgeValidationError,
)
from services.api.tests.test_rule_judge import (
    CountingSemanticJudge,
    FakeObservationPacketService,
    FakeRuleJudgeEvidenceReader,
    FakeRuleJudgeRepository,
    _request,
)


class PagePacketService(FakeObservationPacketService):
    def __init__(self, *, page: dict | None = None, journey: list[dict] | None = None) -> None:
        self.page = page or {}
        self.journey = journey

    async def build(self, request):
        packet = await super().build(request)
        packet.packet_payload["page"].update(self.page)
        if self.journey is not None:
            packet.packet_payload["facts"]["journey"] = self.journey
        return packet


class ManualTestSemanticJudge(CountingSemanticJudge):
    _manual_test = True


def _hybrid(
    *,
    rule_code: str,
    input_contract: dict,
    batch_group: str = "generic",
    category: str = "ad",
) -> RuleJudgeDefinition:
    return RuleJudgeDefinition(
        rule_id=f"rule-{rule_code}",
        rule_code=rule_code,
        executor_type="hybrid",
        judge_config={
            "tool_refs": ["page_capture_tool", "semantic_rule_judge_tool"],
            "deterministic_rules": ["先按结构化证据做基础校验"],
            "llm_checks": ["按截图判"],
            "capability_config": {
                "batch_group": batch_group,
                "deterministic_tools": [],
                "semantic_judge": "semantic_rule_judge_tool",
                "required_artifact_types": ["screenshot"],
                "requires_journey": True,
            },
        },
        rule_name="测试规则",
        category=category,
        check_point="测试检查点",
        input_contract=input_contract,
    )


async def _judge(
    definition: RuleJudgeDefinition,
    *,
    packet_service: FakeObservationPacketService | None = None,
    semantic_judge: CountingSemanticJudge | None = None,
    evidence_capture_roles: dict[str, str] | None = None,
    batch: bool = False,
):
    semantic_judge = semantic_judge or CountingSemanticJudge()
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=packet_service or FakeObservationPacketService(),
        semantic_judge=semantic_judge,
    )
    request = _request(
        rule_code=definition.rule_code,
        judge_input={},
        evidence_capture_roles=evidence_capture_roles,
    )
    if batch:
        return (await service.judge_many([request]))[0], semantic_judge
    return await service.judge(request), semantic_judge


# ---- 平台不做的采样方式 -------------------------------------------------------------


async def test_unsupported_sampling_is_not_applicable_without_calling_model() -> None:
    for sampling, keyword in (("multi_browser", "多浏览器"), ("cross_platform", "跨端")):
        definition = _hybrid(
            rule_code="alioth_spec_18",
            input_contract={"required_artifacts": ["screenshot"], "required_sampling": sampling},
        )
        for batch in (False, True):
            result, semantic_judge = await _judge(definition, batch=batch)
            assert result.execution_status == "skipped"
            assert keyword in result.problem_desc
            assert result.judge_output["applicability"] == "not_applicable"
            assert result.judge_output["tool_results"][0]["required_sampling"] == sampling
            assert semantic_judge.call_count == 0


async def test_app_cross_platform_sampling_closes_as_evidence_gap() -> None:
    definition = _hybrid(
        rule_code="alioth_spec_new_25",
        batch_group="multi-client-pending",
        input_contract={
            "required_artifacts": ["screenshot"],
            "required_sampling": "cross_platform",
        },
    )
    result, semantic_judge = await _judge(
        definition,
        packet_service=PagePacketService(page={"surface_kind": "android"}),
    )

    assert result.execution_status == "evidence_insufficient"
    assert result.problem_desc == "单任务只能采一个端，尚不支持同版本的跨端配对采样。"
    assert result.judge_output["reason_code"] == "APP_CROSS_PLATFORM_SAMPLING_UNSUPPORTED"
    assert semantic_judge.call_count == 0


async def test_sampling_the_platform_supports_is_judged_as_usual() -> None:
    definition = _hybrid(
        rule_code="alioth_spec_17",
        input_contract={
            "required_artifacts": ["screenshot"],
            "required_sampling": "multi_viewport",
        },
    )
    result, semantic_judge = await _judge(definition)
    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1


# ---- 遇到才产出的采集角色 -----------------------------------------------------------


def _offline_content_rule() -> RuleJudgeDefinition:
    return _hybrid(
        rule_code="alioth_40",
        batch_group="content-lifecycle",
        category="text",
        input_contract={
            "required_artifacts": ["screenshot"],
            "required_capture_roles": ["primary"],
            "scenario_trigger_roles": ["source"],
        },
    )


async def test_missing_trigger_role_means_scenario_never_happened() -> None:
    for batch in (False, True):
        result, semantic_judge = await _judge(
            _offline_content_rule(),
            evidence_capture_roles={"primary": "capture-1"},
            batch=batch,
        )
        assert result.execution_status == "skipped"
        assert result.problem_desc == "探索过程中没有遇到这条规则要核对的场景，这条规则不适用"
        assert result.judge_output["tool_results"][0]["missing_trigger_roles"] == ["source"]
        assert semantic_judge.call_count == 0


async def test_bound_trigger_role_goes_to_model() -> None:
    result, semantic_judge = await _judge(
        _offline_content_rule(),
        evidence_capture_roles={"primary": "capture-1", "source": "capture-0"},
    )
    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1


async def test_manual_screenshot_test_is_not_blocked_by_trigger_role() -> None:
    # 规则测试页没有采集角色：来源页、列表页按截图先后交模型看
    result, semantic_judge = await _judge(
        _offline_content_rule(),
        semantic_judge=ManualTestSemanticJudge(),
    )
    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1


# ---- 登录态字段 --------------------------------------------------------------------


def _login_rule() -> RuleJudgeDefinition:
    return _hybrid(
        rule_code="alioth_31",
        category="interaction",
        input_contract={
            "required_artifacts": ["screenshot"],
            # 未登录的任务不会形成这些角色；登录态预检要排在采集角色检查之前
            "required_capture_roles": ["before", "primary", "refreshed"],
            "auth_requirement": "authenticated",
        },
    )


async def test_declared_guest_session_closes_login_rule_as_not_applicable() -> None:
    for batch in (False, True):
        result, semantic_judge = await _judge(
            _login_rule(),
            packet_service=PagePacketService(page={"session_auth": "guest"}),
            batch=batch,
        )
        assert result.execution_status == "skipped"
        assert result.problem_desc == "这条规则要登录后才能判，本次采集时是未登录状态"
        # 和任务级登录闸门同一个原因码，报告口径不分叉
        assert result.judge_output["reason"] == "auth_requirement_not_met"
        assert result.judge_output["tool_results"][0]["session_auth"] == "guest"
        assert semantic_judge.call_count == 0


async def test_guest_only_rule_is_not_applicable_when_logged_in() -> None:
    definition = _hybrid(
        rule_code="alioth_guest",
        input_contract={"required_artifacts": ["screenshot"], "auth_requirement": "guest"},
    )
    result, _ = await _judge(
        definition, packet_service=PagePacketService(page={"session_auth": "authenticated"})
    )
    assert result.execution_status == "skipped"
    assert result.problem_desc == "这条规则只在未登录状态下判，本次采集时已登录"


async def test_undeclared_or_unknown_session_leaves_existing_behaviour() -> None:
    for page in ({}, {"session_auth": "unknown"}, {"session_auth": "authenticated"}):
        result, _ = await _judge(_login_rule(), packet_service=PagePacketService(page=page))
        # 登录态不拦：照旧走到采集角色检查
        assert result.execution_status == "evidence_insufficient"
        assert "before" in result.problem_desc


# ---- 24 启动广告假跳过 --------------------------------------------------------------


def _fake_skip_rule() -> RuleJudgeDefinition:
    return _hybrid(
        rule_code="alioth_24",
        batch_group="startup-ad-skip",
        input_contract={
            "required_artifacts": ["screenshot"],
            "recapture_policy": {"kind": "fresh_observation", "fallback": "manual_only"},
        },
    )


async def test_fake_skip_is_not_applicable_without_a_startup_ad() -> None:
    cases = (("home", [{"page_type": "home"}], "未出现启动广告页"),)
    for page_type, journey, keyword in cases:
        for batch in (False, True):
            result, semantic_judge = await _judge(
                _fake_skip_rule(),
                packet_service=PagePacketService(page={"page_type": page_type}, journey=journey),
                batch=batch,
            )
            assert result.execution_status == "skipped"
            assert keyword in result.problem_desc
            assert semantic_judge.call_count == 0


@pytest.mark.parametrize("batch", [False, True])
async def test_fake_skip_at_journey_end_keeps_missing_after_evidence(batch: bool) -> None:
    result, semantic_judge = await _judge(
        _fake_skip_rule(),
        packet_service=PagePacketService(
            page={"page_type": "startup_ad"}, journey=[{"page_type": "startup_ad"}],
        ),
        batch=batch,
    )
    assert result.execution_status == "evidence_insufficient"
    assert "旅程末端" in result.problem_desc
    assert semantic_judge.call_count == 0
    assert result.judge_output.get("applicability") != "not_applicable"
    gap = result.judge_output["evidence_gap"]
    assert gap["missing"]["capture_roles"] == ["after"]
    assert gap["requested_request"] is not None


@pytest.mark.parametrize("batch", [False, True])
async def test_fake_skip_forms_real_judgement_after_the_home_arrives(batch: bool) -> None:
    packet_service = PagePacketService(
        page={"page_type": "startup_ad"}, journey=[{"page_type": "startup_ad"}],
    )
    first, first_model = await _judge(
        _fake_skip_rule(), packet_service=packet_service, batch=batch,
    )
    assert first.execution_status == "evidence_insufficient"
    assert first_model.call_count == 0
    packet_service.page = {"page_type": "home"}
    packet_service.journey = [{"page_type": "startup_ad"}, {"page_type": "home"}]
    result, semantic_judge = await _judge(
        _fake_skip_rule(), packet_service=packet_service, batch=batch,
    )
    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1
    assert "evidence_gap" not in result.judge_output


async def test_fake_skip_uses_exact_before_when_home_journey_is_compacted() -> None:
    class PairedPacketService(PagePacketService):
        async def build(self, request):
            packet = await super().build(request)
            packet.packet_payload["facts"]["capture_roles"] = {
                "before": {
                    "page_capture_id": "startup-before",
                    "page_type": "startup_ad",
                    "quality_status": "pass",
                },
            }
            return packet

    result, semantic_judge = await _judge(
        _fake_skip_rule(),
        packet_service=PairedPacketService(
            page={"page_type": "home"}, journey=[{"page_type": "home"}],
        ),
    )
    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1


async def test_fake_skip_goes_to_model_once_the_ad_was_left() -> None:
    result, semantic_judge = await _judge(
        _fake_skip_rule(),
        packet_service=PagePacketService(
            page={"page_type": "home"},
            journey=[{"page_type": "startup_ad"}, {"page_type": "home"}],
        ),
    )
    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1


async def test_fake_skip_without_journey_is_left_to_the_model() -> None:
    # 规则测试页没有旅程记录：开屏帧和点跳过后那帧按截图先后交模型
    result, semantic_judge = await _judge(
        _fake_skip_rule(), packet_service=PagePacketService(page={"page_type": "home"})
    )
    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1


# ---- 29 防刷：App 没有网络记录 -------------------------------------------------------


def _rate_limit_rule() -> RuleJudgeDefinition:
    return RuleJudgeDefinition(
        rule_id="rule-alioth_29",
        rule_code="alioth_29",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "rate_limit_assertion",
                "action_pattern": r"button\.submit",
                "probe_content_pattern": "ALIOTH_RATE_PROBE",
                "rate_limit_text_pattern": "操作过于频繁",
                "min_attempts": 4,
                "max_visible_repeats": 3,
                "rate_limit_statuses": [429],
            }
        },
        rule_name="用户输入防刷与刷屏检查",
        category="input_risk",
        check_point="连续发布应触发限频",
        input_contract={
            "required_capture_roles": ["attempt_1", "attempt_2", "attempt_3", "primary"],
        },
    )


def _attempts_without_har(final_text: str) -> dict[str, dict[str, object]]:
    trace = [{"action": "click", "selector": "button.submit"}]
    roles = {
        f"attempt_{index}": {"visible_text": "ALIOTH_RATE_PROBE", "action_trace": trace}
        for index in range(1, 4)
    }
    roles["primary"] = {"visible_text": final_text, "action_trace": trace}
    return roles


async def _judge_rate_limit(surface_kind: str, final_text: str):
    reader = FakeRuleJudgeEvidenceReader(
        "unused", role_artifacts=_attempts_without_har(final_text)
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_rate_limit_rule()),
        evidence_reader=reader,
        observation_packet_service=PagePacketService(page={"surface_kind": surface_kind}),
    )
    roles = {role: f"capture-{role}" for role in _attempts_without_har("x")}
    roles["primary"] = "capture-1"
    result = await service.judge(
        _request(rule_code="alioth_29", judge_input={}, evidence_capture_roles=roles)
    )
    return result, reader


async def test_app_rate_limit_reads_limit_prompt_when_no_network_record() -> None:
    result, reader = await _judge_rate_limit("android", "ALIOTH_RATE_PROBE 操作过于频繁")
    assert reader.calls[0]["required_artifact_types"] == ("visible_text", "action_trace")
    assert result.execution_status == "pass"
    assert result.judge_output["explicit_limit_signal"] is True

    unprotected, _ = await _judge_rate_limit("android", "ALIOTH_RATE_PROBE " * 4)
    assert unprotected.execution_status == "fail"


async def test_browser_rate_limit_still_requires_network_record() -> None:
    # 真实读取器缺 har 会先报证据不足；这里的假读取器不查类型，由处理器兜住同一条底线
    with pytest.raises(RuleJudgeValidationError, match="har 必须为 network_events 对象"):
        await _judge_rate_limit("web", "ALIOTH_RATE_PROBE 操作过于频繁")
