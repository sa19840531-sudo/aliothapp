"""V2 Rule Judge 的 MAF 接线与无写回应用适配测试。"""

from __future__ import annotations

import json
from dataclasses import replace

from agent_framework import Agent

from services.api.app.agents.rule_judge.maf_agent import MafRuleJudgeAgent
from services.api.app.application.inspection_work_items import (
    build_inspection_rule_contract_hash,
)
from services.api.app.application.observation_packet import ObservationPacket
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeResult,
    RuleJudgeService,
    rule_execution_route_payload,
)
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    InspectionHandoff,
    InspectionRuleTarget,
)
from services.api.app.infrastructure.llm_gateway import semantic_rule_judge as semantic_module
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
    LLMSemanticRuleJudge,
)
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_text_response,
)


class FakeRuleJudgeService:
    def __init__(self, results: list[RuleJudgeResult]) -> None:
        self.results = results
        self.requests = []

    async def judge(self, request):
        self.requests.append(request)
        return self.results[0]

    async def judge_many(self, requests):
        self.requests.extend(requests)
        return self.results


def _handoff() -> InspectionHandoff:
    return InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="consumer-run-1",
        input_hash="input-1",
        batch_key="llm:visual:snapshot",
        strategy="llm",
        category="visual",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
            manifest_id="manifest-1",
            manifest_version="evidence-manifest.v1",
            observation_packet_ids=("packet-1",),
            artifact_ids=("shot-1",),
        ),
        rules=(
            InspectionRuleTarget(
                rule_id="rule-1",
                rule_code="alioth_visual_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )


def _result(*, status: str = "pass") -> RuleJudgeResult:
    return RuleJudgeResult(
        rule_id="rule-1",
        rule_code="alioth_visual_1",
        executor_type="llm",
        judge_strategy="llm",
        execution_status=status,
        confidence=0.96 if status == "pass" else 0.0,
        result_summary="证据满足规则。" if status == "pass" else "证据不足。",
        problem_desc=None if status == "pass" else "缺少动作后网络证据。",
        page_capture_id="capture-1",
        evidence_artifact_ids=("shot-1",),
        judge_input={"schema_version": "rule-judge-input.v1"},
        judge_output=(
            {"conclusion": "pass"}
            if status == "pass"
            else {
                "conclusion": "evidence_insufficient",
                "reasoning_summary": "缺少动作后网络证据。",
                "missing_evidence": ["action trace", "network response"],
            }
        ),
        observation_packet_id="packet-1",
    )


async def test_maf_rule_judge_uses_locked_contract_and_never_writes_results() -> None:
    service = FakeRuleJudgeService([_result()])
    agent = MafRuleJudgeAgent(service=service)  # type: ignore[arg-type]

    outcome = await agent.judge(_handoff())

    assert outcome.decisions[0].execution_status == "pass"
    assert outcome.evidence_requests == ()
    assert service.requests[0].expected_contract_hash == "contract-1"
    assert service.requests[0].evidence_artifact_ids == ("shot-1",)
    assert service.requests[0].agent_run_id == "consumer-run-1"
    assert service.requests[0].source_agent_run_id == "producer-run-1"
    assert service.requests[0].judge_input["manifest_id"] == "manifest-1"
    assert service.requests[0].evidence_source_page_capture_id == "capture-1"
    assert service.requests[0].evidence_manifest_id == "manifest-1"


async def test_maf_rule_judge_does_not_fake_supplemental_source_without_manifest() -> None:
    """普通基线 Capture 不能被无条件标记成缺少 Manifest 的补采证据。"""
    service = FakeRuleJudgeService([_result()])
    handoff = replace(
        _handoff(),
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
            evidence_capture_roles={"primary": "capture-1"},
        ),
    )
    agent = MafRuleJudgeAgent(service=service)  # type: ignore[arg-type]

    await agent.judge(handoff)

    request = service.requests[0]
    assert request.page_capture_id == "capture-1"
    assert request.evidence_source_page_capture_id is None
    assert request.evidence_manifest_id is None


async def test_maf_rule_judge_without_manifest_reaches_actual_service_repository() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-1",
        rule_code="alioth_visual_1",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "technical_error_text",
                "pattern": "HTTP\\s*500",
            }
        },
    )
    contract_hash = build_inspection_rule_contract_hash(
        {
            "rule_id": definition.rule_id,
            "rule_code": definition.rule_code,
            "category": definition.category,
            "executor_type": definition.executor_type,
            "severity": definition.severity,
            "check_point": definition.check_point,
            "input_contract": definition.input_contract,
            "judge_config": definition.judge_config,
            "manual_review_policy": definition.manual_review_policy,
            "execution_route": rule_execution_route_payload(
                executor_type=definition.executor_type,
                judge_config=definition.judge_config,
                input_contract=definition.input_contract,
            ),
            "steps": definition.steps,
        }
    )

    class RecordingRepository:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def get_rule_judge_definition(self, *, task_id: str, rule_code: str):
            self.calls.append((task_id, rule_code))
            return definition

    class TextArtifact:
        capture_role = "primary"

        def decode_text(self) -> str:
            return "页面加载正常"

    class EvidenceBundle:
        observation_packet_id = "packet-1"

        def require_unique(self, artifact_type: str):
            assert artifact_type == "visible_text"
            return TextArtifact()

    class RecordingEvidenceReader:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def prepare_observation_packet(self, **kwargs):
            self.calls.append(kwargs)
            return EvidenceBundle()

    repository = RecordingRepository()
    evidence_reader = RecordingEvidenceReader()
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=evidence_reader,
    )
    handoff = replace(
        _handoff(),
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
            artifact_ids=("shot-1",),
        ),
        rules=(replace(_handoff().rules[0], contract_hash=contract_hash),),
    )

    outcome = await MafRuleJudgeAgent(service=service).judge(handoff)

    assert outcome.decisions[0].execution_status == "pass"
    assert outcome.decisions[0].payload["judge_output"]["assertion"]["passed"] is True
    assert repository.calls == [("task-1", "alioth_visual_1")]
    assert "evidence_source_page_capture_id" not in evidence_reader.calls[0]
    assert "evidence_manifest_id" not in evidence_reader.calls[0]


async def test_maf_rule_judge_keeps_result_target_but_reads_exact_supplement() -> None:
    service = FakeRuleJudgeService([_result()])
    supplemental = EvidenceBundleReference(
        page_capture_id="capture-2",
        producer_agent_run_id="producer-run-2",
        manifest_id="manifest-2",
        manifest_version="2",
        artifact_ids=("shot-2",),
        evidence_capture_roles={"primary": "capture-2"},
    )
    handoff = replace(
        _handoff(),
        rule_evidence={"alioth_visual_1": supplemental},
    )
    agent = MafRuleJudgeAgent(service=service)  # type: ignore[arg-type]

    await agent.judge(handoff)

    request = service.requests[0]
    assert request.page_capture_id == "capture-1"
    assert request.evidence_source_page_capture_id == "capture-2"
    assert request.evidence_manifest_id == "manifest-2"
    assert request.source_agent_run_id == "producer-run-2"
    assert request.evidence_artifact_ids == ("shot-2",)
    assert request.judge_input["manifest_id"] == "manifest-2"


async def test_maf_rule_judge_baseline_without_manifest_skips_supplement_pair() -> None:
    """普通 handoff 只有 Capture、无 Manifest 时不得半套补采字段。"""
    from services.api.app.application.rule_judge import (
        RuleJudgeValidationError,
        _validate_request,
    )

    service = FakeRuleJudgeService([_result()])
    handoff = replace(
        _handoff(),
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
            artifact_ids=("shot-1",),
        ),
    )
    agent = MafRuleJudgeAgent(service=service)  # type: ignore[arg-type]

    await agent.judge(handoff)

    request = service.requests[0]
    assert request.page_capture_id == "capture-1"
    assert request.evidence_source_page_capture_id is None
    assert request.evidence_manifest_id is None
    assert request.judge_input["manifest_id"] is None
    _validate_request(request, require_evidence_artifact_ids=False)

    broken = replace(
        request,
        evidence_source_page_capture_id="capture-1",
        evidence_manifest_id=None,
    )
    try:
        _validate_request(broken, require_evidence_artifact_ids=False)
        raise AssertionError("半套补采字段应被拒绝")
    except RuleJudgeValidationError as exc:
        assert "必须同时提供" in str(exc)


async def test_maf_rule_judge_preserves_evidence_insufficient_without_probe_request() -> None:
    service = FakeRuleJudgeService([_result(status="evidence_insufficient")])
    agent = MafRuleJudgeAgent(service=service)  # type: ignore[arg-type]

    outcome = await agent.judge(_handoff())

    assert len(outcome.decisions) == 1
    assert outcome.decisions[0].execution_status == "evidence_insufficient"
    assert outcome.decisions[0].payload["problem_desc"] == "缺少动作后网络证据。"
    assert outcome.evidence_requests == ()


def _packet() -> ObservationPacket:
    return ObservationPacket(
        observation_packet_id="packet-1",
        task_id="task-1",
        page_capture_id="capture-1",
        rule_id="rule-1",
        rule_code="alioth_visual_1",
        evidence_artifact_ids=("shot-1",),
        packet_payload={
            "schema_version": "observation-packet.v1",
            "rule": {"rule_code": "alioth_visual_1"},
            "page": {"url": "https://example.com"},
            "facts": {"visible_text": "首页"},
            "quality_gate": {"status": "pass"},
        },
        quality_gate_status="pass",
        quality_gate_reason=None,
    )


async def test_production_semantic_judge_runs_through_real_maf_agent(monkeypatch) -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "semantic-rule-judge.v1",
                        "conclusion": "pass",
                        "confidence": 0.97,
                        "reasoning_summary": "证据满足当前锁定规则。",
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    monkeypatch.setattr(
        semantic_module,
        "_get_semantic_judge_client",
        lambda: client,
    )
    semantic_module._get_semantic_judge_agent.cache_clear()
    try:
        framework_agent = semantic_module._get_semantic_judge_agent(batch=False)
        output = await LLMSemanticRuleJudge(agent=framework_agent).judge(
            packet=_packet(),
            tool_results=[],
        )
    finally:
        semantic_module._get_semantic_judge_agent.cache_clear()

    assert isinstance(framework_agent, Agent)
    assert output.conclusion == "pass"
    assert client.received_options[0]["response_format"].__name__ == ("SemanticJudgeModelOutput")
    instructions = str(client.received_options[0].get("instructions") or "")
    assert "Rule Judge Agent" in instructions
    assert "禁止访问或控制浏览器" in instructions
