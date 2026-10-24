"""Rule Judge Agent 输出与 Result Writer 分离测试。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.agents.rule_judge import current as current_module
from services.api.app.agents.rule_judge.current import (
    CurrentRuleJudgeAgentAdapter,
    normalize_rule_judge_outcome,
)
from services.api.app.agents.runtime import inspection_result_writer as writer_module
from services.api.app.agents.runtime.inspection_result_writer import (
    CurrentRuleDecisionWriter,
)
from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionResult,
)
from services.api.app.application.supplemental_evidence_outcome import (
    apply_supplemental_evidence_policy,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    InspectionHandoff,
    InspectionRuleTarget,
    RuleJudgeOutcome,
    RuleJudgeOutcomeValidationError,
    build_evidence_request,
)
from services.api.app.domain.inspection.evidence_gap import build_evidence_gap


def _plain(value):
    """把 RuleDecision 冻结后的只读映射/元组还原成普通 dict/list，便于和原始缺口比对。"""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _handoff() -> InspectionHandoff:
    return InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        agent_run_id="consumer-run-1",
        input_hash="input-1",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1",
            producer_agent_run_id="producer-run-1",
        ),
        rules=(
            InspectionRuleTarget(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )


def test_normalizer_locks_decision_to_work_item_attempt_and_rule() -> None:
    outcome = normalize_rule_judge_outcome(
        handoff=_handoff(),
        raw={
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "execution_status": "pass",
            "observation_packet_id": "packet-1",
            "confidence": 0.98,
        },
    )

    assert outcome.protocol_version == "rule-judge-outcome.v1"
    assert outcome.work_item_id == "work-1"
    assert outcome.attempt_id == "attempt-1"
    assert outcome.decisions[0].to_dict()["protocol_version"] == "rule-decision.v1"
    assert outcome.decisions[0].to_writer_payload() == {
        "execution_status": "pass",
        "confidence": 0.98,
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "observation_packet_id": "packet-1",
    }
    assert outcome.evidence_requests == ()


def test_evidence_gap_becomes_formal_evidence_insufficient_decision() -> None:
    outcome = normalize_rule_judge_outcome(
        handoff=_handoff(),
        raw={
            "rule_code": "alioth_1",
            "execution_status": "blocked",
            "error": {
                "code": "TRUSTED_EVIDENCE_INVALID",
                "message": "缺少动作后的截图",
            },
        },
    )

    assert len(outcome.decisions) == 1
    decision = outcome.decisions[0]
    assert decision.execution_status == "evidence_insufficient"
    assert decision.to_writer_payload()["error_code"] == "TRUSTED_EVIDENCE_INVALID"
    assert decision.to_writer_payload()["error_message"] == "缺少动作后的截图"
    assert outcome.evidence_requests == ()


def test_declared_recapture_gap_becomes_handoff_bound_evidence_request() -> None:
    handoff = _handoff()
    gap = build_evidence_gap(
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": "equivalent_state"},
        agent_run_id=handoff.agent_run_id,
        page_capture_id=handoff.evidence.page_capture_id,
        page={
            "page_capture_id": handoff.evidence.page_capture_id,
            "page_type": "home",
            "page_state": "stable",
            "surface_kind": "h5",
        },
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "judge_output": {"evidence_gap": gap.to_dict()},
        },
    )

    assert outcome.decisions == ()
    assert len(outcome.evidence_requests) == 1
    request = outcome.evidence_requests[0]
    assert request.work_item_id == handoff.work_item_id
    assert request.attempt_id == handoff.attempt_id
    assert request.task_id == handoff.task_id
    assert request.rule_id == "rule-1"
    assert request.target_capture_id == handoff.evidence.page_capture_id
    assert request.replay_policy is EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED


def test_tampered_recapture_gap_fails_closed_as_evidence_insufficient() -> None:
    handoff = _handoff()
    gap = build_evidence_gap(
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": "fresh_observation"},
        agent_run_id=handoff.agent_run_id,
        page_capture_id=handoff.evidence.page_capture_id,
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    ).to_dict()
    gap["task_id"] = "another-task"

    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "judge_output": {"evidence_gap": gap},
        },
    )

    assert outcome.evidence_requests == ()
    assert outcome.decisions[0].execution_status == "evidence_insufficient"


def test_missing_judge_output_becomes_error_without_evidence_request() -> None:
    outcome = normalize_rule_judge_outcome(
        handoff=_handoff(),
        raw={"items": []},
    )

    decision = outcome.decisions[0]
    assert decision.execution_status == "error"
    assert decision.to_writer_payload()["error_code"] == "RULE_JUDGE_OUTPUT_MISSING"
    assert outcome.evidence_requests == ()


def test_unstructured_judge_failure_stays_error_without_evidence_request() -> None:
    outcome = normalize_rule_judge_outcome(
        handoff=_handoff(),
        raw={
            "rule_code": "alioth_1",
            "error": {"code": "MODEL_TIMEOUT", "message": "模型调用超时"},
        },
    )

    decision = outcome.decisions[0]
    assert decision.execution_status == "error"
    assert decision.to_writer_payload()["error_code"] == "MODEL_TIMEOUT"
    assert outcome.evidence_requests == ()


async def test_result_writer_persists_evidence_insufficient_without_probe(
    monkeypatch,
) -> None:
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "execution_id": "execution-1"}

    monkeypatch.setattr(writer_module, "record_rule_execution_for_current_run", _record)
    handoff = _handoff()
    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "problem_desc": "现有证据不足。",
        },
    )

    result = await CurrentRuleDecisionWriter().write(handoff=handoff, outcome=outcome)

    assert calls[0]["execution_status"] == "evidence_insufficient"
    assert result["planned_evidence_request_ids"] == []
    assert result["evidence_requests"] == []


async def test_result_writer_converts_evidence_request_to_insufficient_when_disabled(
    monkeypatch,
) -> None:
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "execution_id": "execution-1"}

    monkeypatch.setattr(writer_module, "record_rule_execution_for_current_run", _record)
    handoff = _handoff()
    request = build_evidence_request(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id=handoff.evidence.page_capture_id,
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="缺少动作后的截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    outcome = RuleJudgeOutcome(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        task_id=handoff.task_id,
        evidence_requests=(request,),
    )

    result = await CurrentRuleDecisionWriter().write(handoff=handoff, outcome=outcome)

    assert calls[0]["execution_status"] == "evidence_insufficient"
    assert calls[0]["error_code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    assert calls[0]["error_message"] == "缺少动作后的截图；本版本不能自动补采，先按证据不足记录。"
    assert result["execution_id"] == "execution-1"
    assert result["evidence_requests"] == []
    assert result["planned_evidence_request_ids"] == []
    assert result["suppressed_evidence_request_ids"] == [request.request_id]


async def test_result_writer_can_admit_request_without_publishing_probe() -> None:
    handoff = _handoff()
    request = build_evidence_request(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id=handoff.evidence.page_capture_id,
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="缺少动作后的截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    outcome = RuleJudgeOutcome(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        task_id=handoff.task_id,
        evidence_requests=(request,),
    )
    admission = SimpleNamespace(
        admit=AsyncMock(
            return_value=EvidenceRequestAdmissionResult(
                admitted_request_ids=(request.request_id,)
            )
        )
    )

    result = await CurrentRuleDecisionWriter(
        request_admission=admission,
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    ).write(handoff=handoff, outcome=outcome)

    admission.admit.assert_awaited_once_with(requests=(request,))
    assert result["execution_status"] == "evidence_requested"
    assert result["admitted_evidence_request_ids"] == [request.request_id]
    assert result["planned_evidence_request_ids"] == []


async def test_result_writer_can_route_only_after_request_is_admitted(monkeypatch) -> None:
    handoff = _handoff()
    request = build_evidence_request(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id=handoff.evidence.page_capture_id,
        reason_code="OBSERVATION_PACKET_INCOMPLETE",
        evidence_goal="缺少动作后的截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/home"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    outcome = RuleJudgeOutcome(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        task_id=handoff.task_id,
        evidence_requests=(request,),
    )
    calls: list[str] = []

    async def _admit(*, requests):
        calls.append("admit")
        assert requests == (request,)
        return EvidenceRequestAdmissionResult(
            admitted_request_ids=(request.request_id,)
        )

    async def _route(**kwargs):
        calls.append("route")
        assert kwargs["handoff"] is handoff
        assert kwargs["outcome"] is outcome
        return SimpleNamespace(planned_request_ids=(request.request_id,))

    monkeypatch.setattr(writer_module, "route_outcome_evidence_requests", _route)
    admission = SimpleNamespace(admit=_admit)

    result = await CurrentRuleDecisionWriter(
        orchestrator=object(),
        request_admission=admission,
        route_after_admission=True,
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    ).write(handoff=handoff, outcome=outcome)

    assert calls == ["admit", "route"]
    assert result["admitted_evidence_request_ids"] == [request.request_id]
    assert result["planned_evidence_request_ids"] == [request.request_id]


async def test_materialized_gap_request_is_safely_closed_when_runtime_is_not_ready(
    monkeypatch,
) -> None:
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "execution_id": "execution-1"}

    monkeypatch.setattr(writer_module, "record_rule_execution_for_current_run", _record)
    handoff = _handoff()
    gap = build_evidence_gap(
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": "fresh_observation"},
        agent_run_id=handoff.agent_run_id,
        page_capture_id=handoff.evidence.page_capture_id,
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "judge_output": {"evidence_gap": gap.to_dict()},
        },
    )

    result = await CurrentRuleDecisionWriter().write(handoff=handoff, outcome=outcome)

    assert calls[0]["execution_status"] == "evidence_insufficient"
    assert calls[0]["error_code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    assert result["suppressed_evidence_request_ids"] == [outcome.evidence_requests[0].request_id]


async def test_current_adapter_passes_locked_rule_contract_hash(monkeypatch) -> None:
    calls = []

    async def _judge(**kwargs):
        calls.append(kwargs)
        return {
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "execution_status": "pass",
        }

    monkeypatch.setattr(current_module, "judge_rule_for_current_run", _judge)

    outcome = await CurrentRuleJudgeAgentAdapter().judge(_handoff())

    assert outcome.decisions[0].execution_status == "pass"
    assert calls[0]["expected_contract_hash"] == "contract-1"


async def test_result_writer_only_persists_existing_decision(monkeypatch) -> None:
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "execution_id": "execution-1"}

    monkeypatch.setattr(writer_module, "record_rule_execution_for_current_run", _record)
    handoff = _handoff()
    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "pass",
            "observation_packet_id": "packet-1",
        },
    )

    result = await CurrentRuleDecisionWriter().write(
        handoff=handoff,
        outcome=outcome,
    )

    assert result["execution_id"] == "execution-1"
    assert calls == [
        {
            "page_capture_id": "capture-1",
            "execution_status": "pass",
            "rule_id": "rule-1",
            "rule_code": "alioth_1",
            "observation_packet_id": "packet-1",
        }
    ]


def test_materialized_gap_request_keeps_deferred_insufficient_decision() -> None:
    """请求物化时原判定只是暂缓，不是丢掉：暂缓判定仍带着 evidence_gap。"""
    handoff = _handoff()
    gap = build_evidence_gap(
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": "equivalent_state"},
        agent_run_id=handoff.agent_run_id,
        page_capture_id=handoff.evidence.page_capture_id,
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "problem_desc": "首页截图缺失，无法核对页面元素。",
            "judge_output": {"evidence_gap": gap.to_dict()},
        },
    )

    assert outcome.decisions == ()
    assert len(outcome.evidence_requests) == 1
    assert len(outcome.deferred_decisions) == 1
    deferred = outcome.deferred_decision_for("rule-1")
    assert deferred is not None
    assert deferred.execution_status == "evidence_insufficient"
    assert _plain(deferred.payload["judge_output"]["evidence_gap"]) == gap.to_dict()
    assert deferred.payload["problem_desc"] == "首页截图缺失，无法核对页面元素。"
    assert outcome.deferred_decision_for("rule-9") is None


def test_outcome_payload_preserves_deferred_decision_for_active_request_projection() -> None:
    handoff = _handoff()
    gap = build_evidence_gap(
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": "equivalent_state"},
        agent_run_id=handoff.agent_run_id,
        page_capture_id=handoff.evidence.page_capture_id,
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "judge_output": {"evidence_gap": gap.to_dict()},
        },
    )

    payload = outcome.to_dict()

    assert payload["deferred_decisions"][0]["rule_code"] == "alioth_1"
    assert payload["deferred_decisions"][0]["judge_output"]["evidence_gap"] == gap.to_dict()


def test_deferred_decision_must_belong_to_a_requested_rule() -> None:
    handoff = _handoff()
    decision = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={"rule_code": "alioth_1", "execution_status": "pass"},
    ).decisions[0]

    with pytest.raises(RuleJudgeOutcomeValidationError):
        RuleJudgeOutcome(
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            decisions=(decision,),
            deferred_decisions=(decision,),
        )


def test_allowed_gate_keeps_request_and_drops_nothing() -> None:
    """门禁放行时仍然只发请求，不写任何判定。"""
    handoff = _handoff()
    gap = build_evidence_gap(
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": "equivalent_state"},
        agent_run_id=handoff.agent_run_id,
        page_capture_id=handoff.evidence.page_capture_id,
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "judge_output": {"evidence_gap": gap.to_dict()},
        },
    )

    application = apply_supplemental_evidence_policy(
        outcome=outcome,
        policy=build_lab_supplemental_evidence_policy(),
    )

    assert application.suppressed is False
    assert application.outcome is outcome
    assert application.outcome.decisions == ()
    assert len(application.outcome.evidence_requests) == 1


async def test_suppressed_gap_request_writes_back_original_evidence_gap(monkeypatch) -> None:
    """链路关闭时，非人工策略的缺口也要原样落库，台账不能只剩人工规则。"""
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "execution_id": "execution-1"}

    monkeypatch.setattr(writer_module, "record_rule_execution_for_current_run", _record)
    handoff = _handoff()
    gap = build_evidence_gap(
        task_id=handoff.task_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": "equivalent_state"},
        agent_run_id=handoff.agent_run_id,
        page_capture_id=handoff.evidence.page_capture_id,
        page={
            "page_capture_id": handoff.evidence.page_capture_id,
            "page_type": "home",
            "page_state": "stable",
            "surface_kind": "h5",
        },
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    outcome = normalize_rule_judge_outcome(
        handoff=handoff,
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "problem_desc": "首页截图缺失，无法核对页面元素。",
            "judge_output": {"evidence_gap": gap.to_dict()},
        },
    )

    result = await CurrentRuleDecisionWriter().write(handoff=handoff, outcome=outcome)

    request = outcome.evidence_requests[0]
    assert result["suppressed_evidence_request_ids"] == [request.request_id]
    recorded = calls[0]
    assert recorded["execution_status"] == "evidence_insufficient"
    assert recorded["error_code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    assert recorded["problem_desc"] == "首页截图缺失，无法核对页面元素。"
    assert recorded["error_message"] == (
        "首页截图缺失，无法核对页面元素；本版本不能自动补采，先按证据不足记录。"
    )
    assert _plain(recorded["judge_output"]["evidence_gap"]) == gap.to_dict()
    assert recorded["judge_output"]["supplemental_evidence"] == {
        "request_id": request.request_id,
        "target_capture_id": request.target_capture_id,
        "reason_code": "SUPPLEMENTAL_EVIDENCE_DISABLED",
        "policy_version": recorded["judge_output"]["supplemental_evidence"]["policy_version"],
        "disposition": "suppressed",
    }
