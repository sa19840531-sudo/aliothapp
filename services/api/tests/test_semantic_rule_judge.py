import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agent_framework.exceptions import ChatClientException

from services.api.app.application.observation_packet import ObservationPacket
from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
from services.api.app.core.config import LLMSettings
from services.api.app.infrastructure.llm_gateway import semantic_rule_judge as judge_module
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
    LLMSemanticRuleJudge,
)


class FakeStructuredClient:
    def __init__(self, value: dict | list[dict]) -> None:
        self.values = value if isinstance(value, list) else [value]
        self.calls: list[dict] = []

    async def get_response(self, messages, options):
        self.calls.append({"messages": messages, "options": options})
        index = min(len(self.calls) - 1, len(self.values) - 1)
        return SimpleNamespace(value=self.values[index])


class PrevalidatingStructuredClient:
    def __init__(self, value: dict) -> None:
        self.value = value

    async def get_response(self, messages, options):
        del messages, options
        return SimpleNamespace(
            value=SemanticJudgeModelOutput.model_validate(self.value)
        )


class WrappedPrevalidatingStructuredClient(PrevalidatingStructuredClient):
    async def get_response(self, messages, options):
        try:
            return await super().get_response(messages, options)
        except ValueError as exc:
            raise RuntimeError("structured output parsing failed") from exc


class LazyPrevalidatingStructuredClient:
    def __init__(self, value: dict) -> None:
        self.value = value

    async def get_response(self, messages, options):
        del messages, options
        value = self.value

        class LazyResponse:
            @property
            def value(self) -> SemanticJudgeModelOutput:
                return SemanticJudgeModelOutput.model_validate(value)

        return LazyResponse()


class FailingProviderClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def get_response(self, messages, options):
        self.call_count += 1
        raise ChatClientException("provider unavailable")


class ProviderHttp503Error(Exception):
    status_code = 503


class FailingHttpProviderClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def get_response(self, messages, options):
        self.call_count += 1
        raise ChatClientException(
            "provider unavailable",
            inner_exception=ProviderHttp503Error("upstream unavailable"),
        )


class SlowProviderClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def get_response(self, messages, options):
        self.call_count += 1
        await asyncio.Event().wait()


class FlakyProviderClient:
    """指定的第几次调用一直不返回（超时），其余正常给出通过结论。"""

    def __init__(self, *, slow_calls: set[int]) -> None:
        self.slow_calls = slow_calls
        self.call_count = 0

    async def get_response(self, messages, options):
        del messages, options
        self.call_count += 1
        if self.call_count in self.slow_calls:
            await asyncio.Event().wait()
        return SimpleNamespace(
            value={
                "schema_version": "semantic-rule-judge.v1",
                "conclusion": "pass",
                "confidence": 0.96,
                "reasoning_summary": "页面文案满足规则。",
            }
        )


class ControlledStructuredClient:
    def __init__(self) -> None:
        self.call_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_response(self, messages, options):
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(
            value={
                "schema_version": "semantic-rule-judge.v1",
                "conclusion": "pass",
                "confidence": 0.96,
                "reasoning_summary": "证据满足当前规则。",
            }
        )


def _packet(
    *,
    task_id: str = "task-1",
    rule_code: str = "alioth_test",
) -> ObservationPacket:
    return ObservationPacket(
        observation_packet_id="packet-1",
        task_id=task_id,
        page_capture_id="capture-1",
        rule_id="rule-1",
        rule_code=rule_code,
        evidence_artifact_ids=("shot-1", "text-1"),
        packet_payload={
            "schema_version": "observation-packet.v1",
            "rule": {"rule_code": rule_code, "llm_checks": ["图片不应拉伸"]},
            "page": {"url": "https://example.com"},
            "facts": {"visible_text": "首页"},
            "quality_gate": {"status": "pass"},
        },
        quality_gate_status="pass",
        quality_gate_reason=None,
        image_payloads=(("shot-1", b"\x89PNG\r\n\x1a\npayload", "image/png"),),
    )


def _adult_scope_expansion_failure() -> dict:
    return {
        "schema_version": "semantic-rule-judge.v1",
        "conclusion": "fail",
        "confidence": 0.95,
        "reasoning_summary": (
            "Although the rule focuses on political, terrorist, red-line persons, "
            "or personal attacks, there is no evidence of political content. The page "
            "contains adult sexual services and violates broader input_risk prohibitions."
        ),
        "findings": [
            {
                "summary": "Adult services violate prohibited/illegal content policy",
                "evidence_refs": ["shot-1"],
                "element_refs": ["main"],
            }
        ],
        "evidence_refs": ["shot-1"],
    }


def _message_text(message) -> str:
    return "\n".join(
        content.text
        for content in message.contents
        if getattr(content, "text", None) is not None
    )


@pytest.mark.parametrize(
    ("platform", "surface_kind", "preserve_completed"),
    [("h5", "web", True), ("web", "web", False), ("app", "android", False),
     ("h5", "android", False)],
)
@pytest.mark.parametrize("completed_conclusion", ["pass", "fail", "evidence_insufficient"])
async def test_h5_batch_timeout_preserves_validated_single_results(
    platform: str, surface_kind: str, preserve_completed: bool,
    completed_conclusion: str,
) -> None:
    class PartialFallbackClient:
        def __init__(self) -> None:
            self.call_count = 0

        async def get_response(self, messages, options):
            del messages, options
            self.call_count += 1
            if self.call_count == 1:
                return SimpleNamespace(value={"invalid_batch": True})
            if self.call_count == 2:
                return SimpleNamespace(value={
                    "schema_version": "semantic-rule-judge.v1",
                    "conclusion": completed_conclusion,
                    "confidence": 0.96,
                    "reasoning_summary": "当前规则证据满足要求。",
                    "evidence_refs": ["shot-1"],
                    "missing_evidence": (
                        ["temporal_trace"]
                        if completed_conclusion == "evidence_insufficient" else []
                    ),
                })
            await asyncio.Event().wait()

    packets = [
        replace(packet, packet_payload={
            **packet.packet_payload,
            "identity": {"target_platform": platform},
            "page": {**packet.packet_payload["page"], "surface_kind": surface_kind},
        })
        for packet in (_packet(rule_code="rule_first"), _packet(rule_code="rule_slow"))
    ]
    client = PartialFallbackClient()
    judge = LLMSemanticRuleJudge(client=client, request_timeout_seconds=0.1)
    outputs = await judge.judge_batch(items=[(packet, []) for packet in packets])

    assert client.call_count == 3
    assert outputs["rule_first"].conclusion == (
        completed_conclusion if preserve_completed else "manual_review"
    )
    assert outputs["rule_slow"].conclusion == "manual_review"
    assert outputs["rule_slow"].conflicts == [
        "semantic_batch_budget_exhausted" if preserve_completed else "semantic_provider_timeout"
    ]
    if preserve_completed:
        assert outputs["rule_first"].evidence_refs == ["shot-1"]
        assert not outputs["rule_first"].conflicts
        if completed_conclusion == "evidence_insufficient":
            assert outputs["rule_first"].missing_evidence == ["temporal_trace"]


def _h5_packet(rule_code: str) -> ObservationPacket:
    packet = _packet(rule_code=rule_code)
    return replace(packet, packet_payload={
        **packet.packet_payload,
        "identity": {"target_platform": "h5"},
        "page": {**packet.packet_payload["page"], "surface_kind": "web"},
    })


@pytest.mark.parametrize("oversized", [False, True])
async def test_h5_fallback_concurrently_fits_original_batch_deadline(
    monkeypatch, oversized: bool,
) -> None:
    class SlowValidSingles:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.batch_calls = 0

        async def get_response(self, messages, options):
            del messages
            if options["response_format"] is judge_module.SemanticJudgeBatchOutput:
                self.batch_calls += 1
                return SimpleNamespace(value={"invalid_batch": True})
            self.active += 1
            self.max_active = max(self.active, self.max_active)
            try:
                await asyncio.sleep(0.1)
                return SimpleNamespace(value={
                    "schema_version": "semantic-rule-judge.v1",
                    "conclusion": "pass", "confidence": 0.96,
                    "reasoning_summary": "当前规则证据满足要求。",
                    "evidence_refs": ["shot-1"],
                })
            finally:
                self.active -= 1

    if oversized:
        monkeypatch.setattr(judge_module, "estimate_batch_input_tokens", lambda _: 10**9)
    client = SlowValidSingles()
    judge = LLMSemanticRuleJudge(client=client, request_timeout_seconds=0.25)
    outputs = await judge.judge_batch(items=[
        (_h5_packet(f"rule_{index}"), []) for index in range(4)
    ])
    assert len(outputs) == 4
    assert all(output.conclusion == "pass" for output in outputs.values())
    assert client.max_active == 4
    assert client.active == 0
    assert client.batch_calls == (0 if oversized else 1)
    assert not judge._provider_circuits
    assert not judge._consecutive_timeouts


async def test_h5_fallback_budget_exhaustion_does_not_circuit_or_cache_failure() -> None:
    class BudgetConsumingClient:
        def __init__(self) -> None:
            self.active = 0
            self.slow = True

        async def get_response(self, messages, options):
            del messages
            if options["response_format"] is judge_module.SemanticJudgeBatchOutput:
                await asyncio.sleep(0.06)
                return SimpleNamespace(value={"invalid_batch": True})
            if self.slow:
                self.active += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    self.active -= 1
            return SimpleNamespace(value={
                "schema_version": "semantic-rule-judge.v1",
                "conclusion": "pass", "confidence": 0.96,
                "reasoning_summary": "当前规则证据满足要求。",
                "evidence_refs": ["shot-1"],
            })

    client = BudgetConsumingClient()
    judge = LLMSemanticRuleJudge(client=client, request_timeout_seconds=0.1)
    packets = [_h5_packet("rule_first"), _h5_packet("rule_second")]
    for _ in range(2):
        outputs = await judge.judge_batch(items=[(packet, []) for packet in packets])
        assert all(output.conflicts == ["semantic_batch_budget_exhausted"]
                   for output in outputs.values())
        assert client.active == 0
        assert not judge._inflight_results
        assert not judge._provider_circuits
        assert not judge._consecutive_timeouts
        assert not judge._result_cache
    client.slow = False
    recovered = await judge.judge(packet=packets[0], tool_results=[])
    assert recovered.conclusion == "pass"


async def test_h5_fallback_cancellation_joins_all_child_requests() -> None:
    client = SlowProviderClient()
    judge = LLMSemanticRuleJudge(client=client, request_timeout_seconds=5)
    # 超出合并输入预算时直接进入拆单，不发批次请求。
    from unittest.mock import patch

    with patch.object(judge_module, "estimate_batch_input_tokens", return_value=10**9):
        task = asyncio.create_task(judge.judge_batch(items=[
            (_h5_packet(f"rule_{index}"), []) for index in range(4)
        ]))
        while client.call_count < 4:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not judge._inflight_results
    assert not judge._provider_circuits


async def test_h5_real_timeout_circuit_is_scoped_to_capture_not_next_page() -> None:
    client = FlakyProviderClient(slow_calls={1, 2})
    judge = LLMSemanticRuleJudge(client=client, request_timeout_seconds=0.01)
    first_page = _h5_packet("rule_first")
    for _ in range(3):
        output = await judge.judge(packet=first_page, tool_results=[])
        assert output.conflicts == ["semantic_provider_timeout"]
    assert client.call_count == 2
    second_page = replace(_h5_packet("rule_next"), page_capture_id="capture-next")
    output = await judge.judge(packet=second_page, tool_results=[])
    assert output.conclusion == "pass"
    assert client.call_count == 3
    # 拆单入口和批次入口使用同一实际采集围栏。
    blocked = await judge.judge_batch(items=[(first_page, [])])
    assert blocked["rule_first"].conflicts == ["semantic_provider_timeout"]
    assert client.call_count == 3


async def test_h5_transport_error_still_circuits_whole_task() -> None:
    client = FailingHttpProviderClient()
    judge = LLMSemanticRuleJudge(client=client)
    first = await judge.judge(packet=_h5_packet("rule_first"), tool_results=[])
    next_page = replace(_h5_packet("rule_next"), page_capture_id="capture-next")
    second = await judge.judge(packet=next_page, tool_results=[])
    assert first.conclusion == second.conclusion == "manual_review"
    assert client.call_count == 1
    assert second.conflicts == first.conflicts


async def test_semantic_rule_judge_uses_strict_structured_output() -> None:
    client = FakeStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "fail",
            "confidence": 0.93,
            "reasoning_summary": "图片发生明显拉伸。",
            "findings": [
                {
                    "summary": "人物比例失真",
                    "evidence_refs": ["shot-1"],
                    "element_refs": ["img:nth-of-type(1)"],
                }
            ],
            "evidence_refs": ["shot-1"],
            "missing_evidence": [],
            "conflicts": [],
            "review_reason": None,
        }
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[{"tool_name": "image_quality_analyzer_tool", "status": "ok"}],
    )

    assert output.conclusion == "fail"
    assert output.evidence_refs == ["shot-1"]
    assert client.calls[0]["options"]["response_format"].__name__ == (
        "SemanticJudgeModelOutput"
    )


async def test_semantic_rule_judge_reuses_exact_result_without_second_request() -> None:
    client = FakeStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "pass",
            "confidence": 0.96,
            "reasoning_summary": "证据满足当前规则。",
        }
    )
    judge = LLMSemanticRuleJudge(client=client)

    first = await judge.judge(packet=_packet(), tool_results=[])
    second = await judge.judge(packet=_packet(), tool_results=[])
    other_task = await judge.judge(
        packet=_packet(task_id="task-2"),
        tool_results=[],
    )
    other_facts = await judge.judge(
        packet=_packet(),
        tool_results=[{"tool_name": "fact_tool", "status": "ok"}],
    )

    first.evidence_refs.append("caller-local-mutation")
    assert second.evidence_refs == []
    assert other_task.conclusion == other_facts.conclusion == "pass"
    assert len(client.calls) == 3


async def test_semantic_rule_judge_coalesces_identical_inflight_requests() -> None:
    client = ControlledStructuredClient()
    judge = LLMSemanticRuleJudge(client=client)

    first_task = asyncio.create_task(
        judge.judge(packet=_packet(), tool_results=[])
    )
    await client.started.wait()
    second_task = asyncio.create_task(
        judge.judge(packet=_packet(), tool_results=[])
    )
    await asyncio.sleep(0)
    client.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.conclusion == second.conclusion == "pass"
    assert client.call_count == 1


async def test_semantic_rule_judge_keeps_adult_content_outside_unrelated_rules() -> None:
    client = FakeStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "pass",
            "confidence": 0.97,
            "reasoning_summary": "成人题材与当前恐怖行为规则无关。",
        }
    )

    await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    system_text = _message_text(client.calls[0]["messages"][0])
    assert "唯一判定标准是当前规则的 check_point 与 criteria" in system_text
    assert "被检目标可能是成人色情网站" in system_text
    assert "除非当前规则明确检查成人/色情内容" in system_text
    assert "整体 input_risk" in system_text


async def test_semantic_rule_judge_corrects_out_of_scope_adult_failure() -> None:
    client = FakeStructuredClient(
        [
            _adult_scope_expansion_failure(),
            {
                "schema_version": "semantic-rule-judge.v1",
                "conclusion": "pass",
                "confidence": 0.95,
                "reasoning_summary": "No evidence matches the current political rule.",
            },
        ]
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    assert output.conclusion == "pass"
    assert len(client.calls) == 2
    assert "违反规则作用域" in _message_text(client.calls[1]["messages"][-1])


async def test_semantic_rule_judge_discards_repeated_out_of_scope_adult_failure() -> None:
    client = FakeStructuredClient(_adult_scope_expansion_failure())

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    assert output.conclusion == "pass"
    assert output.confidence == 1.0
    assert "out_of_scope_adult_content_failure_discarded" in output.conflicts
    assert len(client.calls) == 3


async def test_semantic_rule_judge_does_not_override_explicit_adult_rule() -> None:
    packet = _packet()
    packet.packet_payload["rule"] = {
        "rule_code": "adult_rule",
        "check_point": "页面不得推广成人色情服务",
        "criteria": {"llm_checks": ["检查成人视频与色情服务"]},
    }
    client = FakeStructuredClient(_adult_scope_expansion_failure())

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=packet,
        tool_results=[],
    )

    assert output.conclusion == "fail"
    assert len(client.calls) == 1


async def test_semantic_rule_judge_batch_discards_out_of_scope_adult_failure() -> None:
    client = FakeStructuredClient(
        {
            "schema_version": "semantic-rule-judge-batch.v1",
            "items": [
                {
                    "rule_code": "alioth_test",
                    "result": _adult_scope_expansion_failure(),
                }
            ],
        }
    )

    outputs = await LLMSemanticRuleJudge(client=client).judge_batch(
        items=[(_packet(), [])]
    )

    assert outputs["alioth_test"].conclusion == "pass"
    assert "out_of_scope_adult_content_failure_discarded" in outputs[
        "alioth_test"
    ].conflicts
    assert len(client.calls) == 1


async def test_semantic_rule_judge_quarantines_repeated_foreign_evidence_reference() -> None:
    client = FakeStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "warning",
            "confidence": 0.8,
            "reasoning_summary": "存在风险。",
            "findings": [],
            "evidence_refs": ["foreign-artifact"],
            "missing_evidence": [],
            "conflicts": [],
            "review_reason": None,
        }
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    assert output.conclusion == "manual_review"
    assert output.confidence == 0.5
    assert output.evidence_refs == []
    assert output.review_reason is not None
    assert "foreign-artifact" in output.review_reason
    assert len(client.calls) == 3


async def test_semantic_rule_judge_corrects_foreign_reference_with_allowed_ids() -> None:
    client = FakeStructuredClient(
        [
            {
                "schema_version": "semantic-rule-judge.v1",
                "conclusion": "warning",
                "confidence": 0.8,
                "reasoning_summary": "首次输出引用了错误证据。",
                "findings": [],
                "evidence_refs": ["foreign-artifact"],
                "missing_evidence": [],
                "conflicts": [],
                "review_reason": None,
            },
            {
                "schema_version": "semantic-rule-judge.v1",
                "conclusion": "warning",
                "confidence": 0.8,
                "reasoning_summary": "已改用证据包中的真实证据。",
                "findings": [],
                "evidence_refs": ["shot-1"],
                "missing_evidence": [],
                "conflicts": [],
                "review_reason": None,
            },
        ]
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    assert output.conclusion == "warning"
    assert output.evidence_refs == ["shot-1"]
    assert len(client.calls) == 2
    correction_messages = client.calls[1]["messages"]
    assert "foreign-artifact" in correction_messages[-1].contents[0].text
    assert "shot-1" in correction_messages[-1].contents[0].text


def test_semantic_rule_judge_rejects_oversized_output() -> None:
    with pytest.raises(ValueError, match="超过审计写回上限"):
        SemanticJudgeModelOutput.model_validate(
            {
                "conclusion": "manual_review",
                "confidence": 0.5,
                "reasoning_summary": "需要人工确认",
                "findings": [
                    {
                        "summary": "异常" * 250,
                        "evidence_refs": [],
                        "element_refs": [f"element-{index}-" + "x" * 200 for index in range(20)],
                    }
                    for _ in range(20)
                ],
                "evidence_refs": [],
                "missing_evidence": [],
                "conflicts": [],
                "review_reason": "输出过大",
            }
        )


@pytest.mark.parametrize("conclusion", ["evidence_insufficient", "fail", "warning"])
def test_semantic_rule_judge_quarantines_incomplete_risk_output(
    conclusion: str,
) -> None:
    output = judge_module._parse_model_output(
        {
            "conclusion": conclusion,
            "confidence": 0.9,
            "reasoning_summary": "模型返回了结论，但没有提供契约要求的证据细节。",
            "findings": [],
            "evidence_refs": [],
            "missing_evidence": [],
            "conflicts": [],
            "review_reason": None,
        }
    )

    assert output.conclusion == "manual_review"
    assert output.confidence == 0.5
    assert "semantic_output_evidence_contract_incomplete" in output.conflicts
    assert output.review_reason is not None


async def test_semantic_rule_judge_recovers_raw_input_from_transport_validation() -> None:
    client = PrevalidatingStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "evidence_insufficient",
            "confidence": 0.9,
            "reasoning_summary": "模型没有提供缺失证据明细。",
            "findings": [],
            "evidence_refs": [],
            "missing_evidence": [],
            "conflicts": [],
            "review_reason": None,
        }
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    assert output.conclusion == "manual_review"
    assert "semantic_output_evidence_contract_incomplete" in output.conflicts


async def test_semantic_rule_judge_recovers_wrapped_transport_validation() -> None:
    client = WrappedPrevalidatingStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "evidence_insufficient",
            "confidence": 0.9,
            "reasoning_summary": "模型没有提供缺失证据明细。",
            "findings": [],
            "evidence_refs": [],
            "missing_evidence": [],
            "conflicts": [],
            "review_reason": None,
        }
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    assert output.conclusion == "manual_review"
    assert "semantic_output_evidence_contract_incomplete" in output.conflicts


async def test_semantic_rule_judge_recovers_lazy_response_validation() -> None:
    client = LazyPrevalidatingStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "evidence_insufficient",
            "confidence": 0.9,
            "reasoning_summary": "模型没有提供缺失证据明细。",
            "findings": [],
            "evidence_refs": [],
            "missing_evidence": [],
            "conflicts": [],
            "review_reason": None,
        }
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=_packet(),
        tool_results=[],
    )

    assert output.conclusion == "manual_review"
    assert "semantic_output_evidence_contract_incomplete" in output.conflicts


async def test_semantic_rule_judge_keeps_unrelated_transport_schema_error_strict() -> None:
    client = PrevalidatingStructuredClient(
        {
            "schema_version": "semantic-rule-judge.v1",
            "conclusion": "unknown",
            "confidence": 0.9,
            "reasoning_summary": "非法结论。",
        }
    )

    with pytest.raises(ValueError):
        await LLMSemanticRuleJudge(client=client).judge(
            packet=_packet(),
            tool_results=[],
        )


async def test_semantic_rule_judge_does_not_send_over_budget_request(monkeypatch) -> None:
    client = FakeStructuredClient({})
    packet = _packet()
    packet.packet_payload["facts"] = {"unbounded": "x" * 100_000}
    monkeypatch.setattr(
        judge_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(prompt_input_token_budget=1_000)
        ),
    )

    output = await LLMSemanticRuleJudge(client=client).judge(
        packet=packet,
        tool_results=[],
    )

    assert output.conclusion == "evidence_insufficient"
    assert "prompt_input_token_budget" in output.missing_evidence
    assert client.calls == []


async def test_h5_prompt_budget_rejection_does_not_request_more_physical_evidence(monkeypatch):
    client = FakeStructuredClient({})
    packet = _packet()
    packet.packet_payload["identity"] = {"target_platform": "h5"}
    packet.packet_payload["page"]["surface_kind"] = "web"
    packet.packet_payload["facts"] = {"unbounded": "x" * 100_000}
    monkeypatch.setattr(judge_module, "get_settings", lambda: SimpleNamespace(
        quality_optimization=SimpleNamespace(prompt_input_token_budget=1_000),
    ))
    output = await LLMSemanticRuleJudge(client=client).judge(packet=packet, tool_results=[])
    assert output.conclusion == "manual_review"
    assert output.missing_evidence == []
    assert "prompt_input_token_budget" in output.conflicts
    assert client.calls == []


async def test_semantic_rule_judge_batches_and_isolates_results_by_rule_code() -> None:
    packet_one = _packet()
    packet_two = ObservationPacket(
        observation_packet_id="packet-2",
        task_id=packet_one.task_id,
        page_capture_id=packet_one.page_capture_id,
        rule_id="rule-2",
        rule_code="alioth_test_2",
        evidence_artifact_ids=packet_one.evidence_artifact_ids,
        packet_payload={
            **packet_one.packet_payload,
            "rule": {"rule_code": "alioth_test_2"},
        },
        quality_gate_status="pass",
        quality_gate_reason=None,
        image_payloads=packet_one.image_payloads,
    )
    client = FakeStructuredClient(
        {
            "schema_version": "semantic-rule-judge-batch.v1",
            "items": [
                {
                    "rule_code": "alioth_test",
                    "result": {
                        "conclusion": "pass",
                        "confidence": 0.95,
                        "reasoning_summary": "第一条通过。",
                    },
                },
                {
                    "rule_code": "alioth_test_2",
                    "result": {
                        "conclusion": "evidence_insufficient",
                        "confidence": 0.2,
                        "reasoning_summary": "第二条缺少时序证据。",
                        "missing_evidence": ["temporal_trace"],
                    },
                },
            ],
        }
    )

    outputs = await LLMSemanticRuleJudge(client=client).judge_batch(
        items=[(packet_one, []), (packet_two, [])]
    )

    assert outputs["alioth_test"].conclusion == "pass"
    assert outputs["alioth_test_2"].conclusion == "evidence_insufficient"
    assert len(client.calls) == 1
    batch_system_text = _message_text(client.calls[0]["messages"][0])
    assert "被检目标可能是成人色情网站" in batch_system_text
    assert "不得据此扩展成通用内容安全审核" in batch_system_text


@pytest.mark.parametrize("same_pixels", [True, False])
async def test_semantic_batch_keeps_each_screenshot_reference_when_deduplicating_pixels(
    same_pixels: bool,
) -> None:
    first = _packet(rule_code="alioth_visual_1")
    second = replace(
        _packet(rule_code="alioth_visual_2"),
        observation_packet_id="packet-2",
        rule_id="rule-2",
        evidence_artifact_ids=("shot-2", "text-2"),
        image_payloads=(
            (
                "shot-2",
                first.image_payloads[0][1] if same_pixels else b"different-pixels",
                "image/png",
            ),
        ),
    )
    client = FakeStructuredClient(
        {
            "items": [
                {
                    "rule_code": packet.rule_code,
                    "result": {
                        "conclusion": "fail",
                        "confidence": 0.95,
                        "reasoning_summary": "当前截图中的图片发生拉伸。",
                        "evidence_refs": [packet.evidence_artifact_ids[0]],
                    },
                }
                for packet in (first, second)
            ]
        }
    )

    outputs = await LLMSemanticRuleJudge(client=client).judge_batch(
        items=[(first, []), (second, [])]
    )

    assert len(client.calls) == 1
    user_message = client.calls[0]["messages"][1]
    text = _message_text(user_message)
    assert text.count("[同页截图证据 shot-1]") == 1
    assert text.count("[同页截图证据 shot-2]") == 1
    images = [content for content in user_message.contents if content.type == "data"]
    assert len(images) == (1 if same_pixels else 2)
    assert outputs[first.rule_code].evidence_refs == ["shot-1"]
    assert outputs[second.rule_code].evidence_refs == ["shot-2"]
    assert all(output.conclusion == "fail" for output in outputs.values())


async def test_semantic_rule_judge_circuits_provider_failure_without_split_retry() -> None:
    client = FailingProviderClient()
    judge = LLMSemanticRuleJudge(client=client)

    outputs = await judge.judge_batch(items=[(_packet(), [])])
    second_output = await judge.judge(packet=_packet(), tool_results=[])

    assert outputs["alioth_test"].conclusion == "manual_review"
    assert second_output.conclusion == "manual_review"
    assert outputs["alioth_test"].missing_evidence == []
    assert "semantic_provider_chatclientexception" in outputs[
        "alioth_test"
    ].conflicts
    assert client.call_count == 1


async def test_semantic_rule_judge_persists_provider_http_status_in_circuit_result() -> None:
    client = FailingHttpProviderClient()
    judge = LLMSemanticRuleJudge(client=client)

    first = await judge.judge(packet=_packet(), tool_results=[])
    second = await judge.judge(packet=_packet(), tool_results=[])

    assert first.conflicts == ["semantic_provider_http_503"]
    assert second.conflicts == ["semantic_provider_http_503"]
    assert "模型 API 返回 HTTP 503（服务暂时不可用）" in first.reasoning_summary
    assert "短时熔断" in second.review_reason
    assert client.call_count == 1


async def test_semantic_rule_judge_timeout_is_bounded_and_scoped_by_task() -> None:
    client = SlowProviderClient()
    judge = LLMSemanticRuleJudge(
        client=client,
        request_timeout_seconds=0.01,
        circuit_seconds=60,
    )

    first = await judge.judge(packet=_packet(), tool_results=[])
    second = await judge.judge(packet=_packet(), tool_results=[])
    circuited = await judge.judge(packet=_packet(), tool_results=[])
    other_task = await judge.judge(
        packet=_packet(task_id="task-2"),
        tool_results=[],
    )

    assert first.conflicts == ["semantic_provider_timeout"]
    # 单次超时只影响当次请求：不熔断，下一条规则照常去问模型
    assert "短时熔断" not in first.review_reason
    assert "后续规则照常判定" in first.review_reason
    # 同一任务连续第二次超时才熔断，之后的请求不再等 60 秒
    assert second.conflicts == ["semantic_provider_timeout"]
    assert "短时熔断" in second.review_reason
    assert circuited.conflicts == ["semantic_provider_timeout"]
    assert other_task.conflicts == ["semantic_provider_timeout"]
    assert "短时熔断" not in other_task.review_reason
    assert client.call_count == 3


async def test_semantic_rule_judge_success_resets_the_timeout_count() -> None:
    """超时—成功—超时 不算连续超时，不该熔断。"""
    client = FlakyProviderClient(slow_calls={1, 3})
    judge = LLMSemanticRuleJudge(client=client, request_timeout_seconds=0.05, circuit_seconds=60)

    outcomes = [
        await judge.judge(packet=_packet(rule_code=f"alioth_test_{index}"), tool_results=[])
        for index in range(1, 5)
    ]

    assert [item.conclusion for item in outcomes] == [
        "manual_review",
        "pass",
        "manual_review",
        "pass",
    ]
    assert all("短时熔断" not in (item.review_reason or "") for item in outcomes)
    assert client.call_count == 4


@pytest.mark.parametrize("h5", [False, True])
async def test_semantic_rule_judge_batch_timeout_circuits_whole_batch(h5: bool) -> None:
    client = SlowProviderClient()
    judge = LLMSemanticRuleJudge(
        client=client,
        request_timeout_seconds=0.01,
        circuit_seconds=60,
    )

    factory = _h5_packet if h5 else lambda rule: _packet(rule_code=rule)
    items = [(factory("alioth_test"), []), (factory("alioth_test_2"), [])]
    outputs = await judge.judge_batch(items=items)

    assert set(outputs) == {"alioth_test", "alioth_test_2"}
    assert {
        output.conflicts[0] for output in outputs.values()
    } == {"semantic_provider_timeout"}
    assert client.call_count == 1
    await judge.judge_batch(items=items)
    assert client.call_count == 2
    circuited = await judge.judge_batch(items=items)
    assert client.call_count == 2
    assert all(output.conflicts == ["semantic_provider_timeout"]
               for output in circuited.values())


def test_semantic_judge_client_overrides_generic_timeout_and_retries(monkeypatch) -> None:
    captured: dict[str, LLMSettings] = {}
    settings = SimpleNamespace(
        llm=LLMSettings(
            api_key="test-key",
            timeout_seconds=180,
            max_retries=2,
        ),
        quality_optimization=SimpleNamespace(
            semantic_request_timeout_seconds=60,
            semantic_max_retries=0,
        ),
    )
    monkeypatch.setattr(judge_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        judge_module,
        "build_chat_client",
        lambda client_settings, **_kwargs: captured.setdefault("settings", client_settings),
    )
    judge_module._get_semantic_judge_client.cache_clear()

    try:
        resolved = judge_module._get_semantic_judge_client()
    finally:
        judge_module._get_semantic_judge_client.cache_clear()

    assert resolved is captured["settings"]
    assert captured["settings"].timeout_seconds == 60
    assert captured["settings"].max_retries == 0


def test_manual_test_mode_reaches_system_text_and_tool_block() -> None:
    tools = [
        {
            "tool_name": "layout_geometry_assertion_handler",
            "status": "insufficient",
            "reason": "缺少浏览器 viewport 几何事实",
        }
    ]
    formal = judge_module._build_messages(packet=_packet(), tool_results=tools)
    manual = judge_module._build_messages(packet=_packet(), tool_results=tools, manual_test=True)
    manual_batch = judge_module._build_batch_messages(items=[(_packet(), tools)], manual_test=True)

    assert "人工证据测试模式" not in _message_text(formal[0])
    assert judge_module.MANUAL_TEST_TOOL_RESULTS_NOTE not in _message_text(formal[1])
    assert "人工证据测试模式" in _message_text(manual[0])
    assert "人工证据测试模式" in _message_text(manual_batch[0])
    user_text = _message_text(manual[1])
    # 提醒紧跟在 Tool 结果标题后、结果内容前，模型看到 insufficient 之前先看到它
    assert (
        user_text.index("【确定性 Tool 结果】")
        < user_text.index(judge_module.MANUAL_TEST_TOOL_RESULTS_NOTE)
        < user_text.index("layout_geometry_assertion_handler")
    )


@pytest.mark.parametrize("manual_test", [False, True])
async def test_truncated_locked_input_contract_never_reaches_model(manual_test: bool) -> None:
    packet = _packet()
    packet.packet_payload["rule"]["input_contract"] = {
        "audit_payload_truncated": True, "keys": ["required_artifacts"],
    }
    client = FakeStructuredClient({
        "conclusion": "pass", "confidence": 1.0, "reasoning_summary": "截图看起来正常。",
    })
    judge = LLMSemanticRuleJudge(client=client, manual_test=manual_test)

    output = await judge.judge(packet=packet, tool_results=[])
    assert output.conclusion == "manual_review"
    assert output.conflicts == ["rule_input_contract_truncated"]
    assert output.missing_evidence == []
    assert client.calls == []
    batch = await judge.judge_batch(items=[(packet, [])])
    assert batch[packet.rule_code].conclusion == "manual_review"
    assert client.calls == []


async def test_batch_contract_projection_gap_does_not_block_other_rule() -> None:
    blocked = _packet(rule_code="blocked")
    blocked.packet_payload["rule"]["input_contract"] = {"budget_truncated": True}
    good = _packet(rule_code="good")
    client = FakeStructuredClient({
        "schema_version": "semantic-rule-judge-batch.v1",
        "items": [{"rule_code": "good", "result": {
            "conclusion": "pass", "confidence": 0.95, "reasoning_summary": "视觉材料充分。",
        }}],
    })
    outputs = await LLMSemanticRuleJudge(client=client).judge_batch(
        items=[(blocked, []), (good, [])],
    )
    assert outputs["blocked"].conclusion == "manual_review"
    assert outputs["good"].conclusion == "pass"
    assert len(client.calls) == 1


async def test_manual_test_judge_instance_sends_manual_mode_only_when_enabled() -> None:
    output = {
        "schema_version": "semantic-rule-judge.v1",
        "conclusion": "pass",
        "confidence": 0.9,
        "reasoning_summary": "截图里能看清，按看到的判通过。",
    }
    manual_client = FakeStructuredClient(output)
    await LLMSemanticRuleJudge(client=manual_client, manual_test=True).judge(
        packet=_packet(), tool_results=[]
    )
    sent = manual_client.calls[0]["messages"]
    assert "人工证据测试模式" in _message_text(sent[0])
    assert judge_module.MANUAL_TEST_TOOL_RESULTS_NOTE in _message_text(sent[1])

    formal_client = FakeStructuredClient(output)
    await LLMSemanticRuleJudge(client=formal_client).judge(packet=_packet(), tool_results=[])
    formal_sent = formal_client.calls[0]["messages"]
    assert "人工证据测试模式" not in _message_text(formal_sent[0])
    assert judge_module.MANUAL_TEST_TOOL_RESULTS_NOTE not in _message_text(formal_sent[1])
