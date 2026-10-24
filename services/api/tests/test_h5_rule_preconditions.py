"""H5 缺少业务前置事实不得按技术故障重试，其他端行为保持不变。"""

from dataclasses import replace

import pytest

from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeService,
    RuleJudgeValidationError,
    rule_execution_route_payload,
)
from services.api.tests.test_rule_judge import (
    FakeObservationPacketService,
    FakeRuleJudgeEvidenceReader,
    FakeRuleJudgeRepository,
    _request,
)


class _PlatformPacketService(FakeObservationPacketService):
    def __init__(self, platform, surface):
        self.platform = platform
        self.surface = surface

    async def build(self, request):
        packet = await super().build(request)
        return replace(
            packet,
            packet_payload={
                **packet.packet_payload,
                "identity": {"target_platform": self.platform},
                "page": {**packet.packet_payload["page"], "surface_kind": self.surface},
            },
        )


def _service(platform, surface, source_text):
    definition = RuleJudgeDefinition(
        rule_id="rule-lifecycle",
        rule_code="lifecycle",
        executor_type="rule_engine",
        input_contract={"required_capture_roles": ["source", "primary"]},
        judge_config={
            "engine_config": {
                "handler": "content_lifecycle_state",
                "source_inactive_pattern": "已下架",
                "consumer_entity_pattern": "目标内容",
                "consumer_marked_pattern": "已下架",
            }
        },
    )
    return RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "目标内容",
            role_artifacts={
                "source": {"visible_text": source_text},
                "primary": {"visible_text": "目标内容"},
            },
        ),
        observation_packet_service=_PlatformPacketService(platform, surface),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "surface"),
    [
        ("h5", "web"),
        ("web", "web"),
        ("app", "android"),
        ("h5", "android"),
    ],
)
async def test_missing_lifecycle_precondition_is_structured_only_for_h5(platform, surface):
    service = _service(platform, surface, "目标内容正常展示")
    request = replace(
        _request(rule_code="lifecycle", judge_input={}),
        evidence_capture_roles={"primary": "capture-1", "source": "capture-0"},
    )
    if platform == "h5" and surface == "web":
        result = await service.judge(request)
        assert result.execution_status == "evidence_insufficient"
        assert result.confidence == 0
        assert "失效状态" in result.problem_desc
        assert result.judge_output["reasoning_summary"] == result.problem_desc
        assert result.evidence_artifact_ids
        assert result.observation_packet_id == "packet-1"
    else:
        with pytest.raises(RuleJudgeValidationError, match="失效状态"):
            await service.judge(request)


@pytest.mark.asyncio
async def test_h5_lifecycle_with_actual_precondition_keeps_failure_verdict():
    service = _service("h5", "web", "目标内容已下架")
    request = replace(
        _request(rule_code="lifecycle", judge_input={}),
        evidence_capture_roles={"primary": "capture-1", "source": "capture-0"},
    )
    result = await service.judge(request)
    assert result.execution_status == "fail"


@pytest.mark.asyncio
async def test_h5_malformed_evidence_is_not_hidden_as_missing_precondition():
    service = _service("h5", "web", "目标内容正常展示" + "x" * 100_001)
    request = replace(
        _request(rule_code="lifecycle", judge_input={}),
        evidence_capture_roles={"primary": "capture-1", "source": "capture-0"},
    )
    with pytest.raises(RuleJudgeValidationError, match="最多允许"):
        await service.judge(request)


def _price_service(platform, surface, primary, other):
    definition = RuleJudgeDefinition(
        rule_id="rule-price",
        rule_code="price",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "price_relation",
                "price_pattern": r"(?P<product>\S+月卡)\s*¥(?P<price>\S+)",
            }
        },
    )
    artifacts = {"primary": {"visible_text": primary}}
    if other is not None:
        artifacts["location_1"] = {"visible_text": other}
    return RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(primary, role_artifacts=artifacts),
        observation_packet_service=_PlatformPacketService(platform, surface),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["h5", "web", "app"])
@pytest.mark.parametrize("other", [None, "白银月卡 ¥9.90"])
async def test_price_comparison_missing_facts_are_structured_only_for_h5(platform, other):
    service = _price_service(
        platform,
        "android" if platform == "app" else "web",
        "黄金月卡 ¥19.90",
        other,
    )
    request = replace(
        _request(rule_code="price", judge_input={}),
        evidence_capture_roles={"primary": "capture-1", "location_1": "capture-2"},
    )
    if platform == "h5":
        result = await service.judge(request)
        assert result.execution_status == "evidence_insufficient"
        assert result.evidence_artifact_ids
    else:
        with pytest.raises(RuleJudgeValidationError):
            await service.judge(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other", "verdict"),
    [
        ("黄金月卡 ¥19.90", "pass"),
        ("黄金月卡 ¥29.90", "fail"),
    ],
)
async def test_actual_price_comparison_verdict_is_preserved(other, verdict):
    service = _price_service("h5", "web", "黄金月卡 ¥19.90", other)
    result = await service.judge(
        replace(
            _request(rule_code="price", judge_input={}),
            evidence_capture_roles={"primary": "capture-1", "location_1": "capture-2"},
        )
    )
    assert result.execution_status == verdict


@pytest.mark.asyncio
async def test_h5_malformed_price_is_not_hidden_as_missing_precondition():
    service = _price_service("h5", "web", "黄金月卡 ¥nan", "黄金月卡 ¥nan")
    with pytest.raises(RuleJudgeValidationError, match="有限数值"):
        await service.judge(
            replace(
                _request(rule_code="price", judge_input={}),
                evidence_capture_roles={"primary": "capture-1", "location_1": "capture-2"},
            )
        )


@pytest.mark.asyncio
async def test_h5_missing_precondition_finishes_consumer_without_technical_retry(monkeypatch):
    from services.api.app.agents.rule_judge.maf_agent import MafRuleJudgeAgent
    from services.api.app.agents.runtime import inspection_work_item_consumer as module
    from services.api.app.application.inspection_work_items import (
        InspectionWorkItemRule,
        build_inspection_rule_contract_hash,
    )
    from services.api.tests.test_inspection_work_item_consumer import FakeService

    judge_service = _service("h5", "web", "目标内容正常展示")
    definition = judge_service._repository.definition
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
            "steps": definition.steps,
            "execution_route": rule_execution_route_payload(
                executor_type=definition.executor_type,
                judge_config=definition.judge_config,
                input_contract=definition.input_contract,
            ),
        }
    )

    class Service(FakeService):
        async def claim(self, command):
            lease = await super().claim(command)
            if lease is None:
                return None
            return replace(
                lease,
                rules=(
                    InspectionWorkItemRule(
                        rule_id=definition.rule_id,
                        rule_code=definition.rule_code,
                        ordinal=0,
                        contract_hash=contract_hash,
                    ),
                ),
            )

        async def sync_formal_results(self, **kwargs):
            synced = await super().sync_formal_results(**kwargs)
            return replace(
                synced,
                links=tuple(
                    replace(
                        link,
                        rule_id=definition.rule_id,
                        rule_code=definition.rule_code,
                        execution_status="evidence_insufficient",
                    )
                    for link in synced.links
                ),
            )

    class Writer:
        async def write(self, *, handoff, outcome):
            assert outcome.decisions[0].execution_status == "evidence_insufficient"
            assert outcome.evidence_requests == ()
            return {"execution_status": "evidence_insufficient"}

    async def roles(lease):
        return {"primary": lease.page_capture_id, "source": "capture-0"}

    monkeypatch.setattr(module, "resolve_inspection_evidence_capture_roles", roles)
    service = Service([], expected_session_dependency="active_session", target_platform="h5")
    consumer = module.InspectionWorkItemConsumer(
        service=service,
        lease_owner="worker-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=MafRuleJudgeAgent(service=judge_service),
        result_writer=Writer(),
    )
    result = await consumer.drain_available(
        task_id="task-1",
        consumer_agent_run_id="run-1",
        session_dependency="active_session",
        max_items=1,
    )
    assert result.completed_count == 1
    assert result.deferred_count == 0
    assert service.claim_count == 1
    # 工作完成不等于规则通过：完整的缺证据结论同样可以正常结束消费。
    assert service.transitions[-1].target_status.value == "succeeded"
    persisted = service.attempt_transitions[0].result_payload
    assert persisted["decisions"][0]["execution_status"] == "evidence_insufficient"
    assert [command.target_status.value for command in service.attempt_transitions] == [
        "judged",
        "written",
    ]
