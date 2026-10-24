"""把被补采开关拒绝的 Evidence Request 转换为正式规则结论。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDecision,
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import (
    EvidenceRequest,
    RuleDecision,
    RuleJudgeOutcome,
)


@dataclass(frozen=True, slots=True)
class SupplementalEvidenceOutcomeApplication:
    """补采策略应用结果，保留被抑制请求身份供审计使用。"""

    outcome: RuleJudgeOutcome
    suppressed_request_ids: tuple[str, ...] = ()
    gate_decision: SupplementalEvidenceDecision | None = None

    @property
    def suppressed(self) -> bool:
        """返回是否有 Evidence Request 被策略收口。"""
        return bool(self.suppressed_request_ids)


def apply_supplemental_evidence_policy(
    *,
    outcome: RuleJudgeOutcome,
    policy: SupplementalEvidencePolicy,
) -> SupplementalEvidenceOutcomeApplication:
    """关闭或未就绪时，把每个补采请求确定性转成证据不足。"""
    if not isinstance(outcome, RuleJudgeOutcome):
        raise TypeError("outcome 必须为 RuleJudgeOutcome")
    if not isinstance(policy, SupplementalEvidencePolicy):
        raise TypeError("policy 必须为 SupplementalEvidencePolicy")
    if not outcome.evidence_requests:
        return SupplementalEvidenceOutcomeApplication(outcome=outcome)

    gate = policy.decide(SupplementalEvidenceOperation.PRODUCE_REQUEST)
    if gate.allowed:
        return SupplementalEvidenceOutcomeApplication(outcome=outcome, gate_decision=gate)

    converted = tuple(
        _suppressed_decision(outcome=outcome, request=request, gate=gate)
        for request in outcome.evidence_requests
    )
    effective = RuleJudgeOutcome(
        work_item_id=outcome.work_item_id,
        attempt_id=outcome.attempt_id,
        task_id=outcome.task_id,
        decisions=(*outcome.decisions, *converted),
        evidence_requests=(),
    )
    return SupplementalEvidenceOutcomeApplication(
        outcome=effective,
        suppressed_request_ids=tuple(
            request.request_id for request in outcome.evidence_requests
        ),
        gate_decision=gate,
    )


_SUPPRESSED_ERROR_SUFFIX = "本版本不能自动补采，先按证据不足记录。"


def _suppressed_error_message(reason: str) -> str:
    """工作台把 error_message 当这条规则的「原因」直接展示：先说缺什么，再说为什么没补。

    门禁那句「补采能力当前已关闭…（配置键 …）」是给运维看的审计语，
    reason_code 已经落在 judge_output.supplemental_evidence 里；原样写进
    error_message 只会让验收的人以为是漏开开关（2026-09-22 沈钧铭验收 App 任务实锤）。
    """
    text = str(reason or "").strip().rstrip("。；;")
    return f"{text}；{_SUPPRESSED_ERROR_SUFFIX}" if text else _SUPPRESSED_ERROR_SUFFIX


def _suppressed_decision(
    *,
    outcome: RuleJudgeOutcome,
    request: EvidenceRequest,
    gate: SupplementalEvidenceDecision,
) -> RuleDecision:
    """把被门禁抑制的补采请求确定性转成证据不足判定。

    优先沿用 Judge 暂缓的原判定：judge_output.evidence_gap（缺哪页、哪个状态、
    关闭前后、缺什么、规则声明、请求预览）原样落库，缺口台账和工作台才能看到
    「只记录不执行」的真实基线；在其上只附加抑制事实。没有暂缓判定的旧调用方
    仍走原来的最小载荷。
    """
    supplemental = {
        "request_id": request.request_id,
        "target_capture_id": request.target_capture_id,
        "reason_code": gate.reason_code,
        "policy_version": gate.policy_version,
        "disposition": "suppressed",
    }
    deferred = outcome.deferred_decision_for(request.rule_id)
    if deferred is None:
        return RuleDecision(
            work_item_id=request.work_item_id,
            attempt_id=request.attempt_id,
            rule_id=request.rule_id,
            rule_code=request.rule_code,
            execution_status="evidence_insufficient",
            payload={
                "execution_status": "evidence_insufficient",
                "result_summary": "当前版本未启用完整补采链路，规则按证据不足收口。",
                "problem_desc": request.evidence_goal,
                "error_code": gate.reason_code,
                "error_message": _suppressed_error_message(request.evidence_goal),
                "judge_output": {"supplemental_evidence": supplemental},
            },
        )
    payload = dict(deferred.payload)
    judge_output = payload.get("judge_output")
    merged_output = dict(judge_output) if isinstance(judge_output, Mapping) else {}
    merged_output["supplemental_evidence"] = supplemental
    payload["judge_output"] = merged_output
    payload["execution_status"] = "evidence_insufficient"
    payload["error_code"] = gate.reason_code
    payload["error_message"] = _suppressed_error_message(
        str(payload.get("problem_desc") or request.evidence_goal)
    )
    return RuleDecision(
        work_item_id=deferred.work_item_id,
        attempt_id=deferred.attempt_id,
        rule_id=deferred.rule_id,
        rule_code=deferred.rule_code,
        execution_status="evidence_insufficient",
        observation_packet_id=deferred.observation_packet_id,
        payload=payload,
    )
