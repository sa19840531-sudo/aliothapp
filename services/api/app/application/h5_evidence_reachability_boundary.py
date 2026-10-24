"""H5 规则补采的可达性边界。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.domain.inspection import (
    OVERLAY_CLOSE_SEMANTIC_TARGET,
    EvidenceRequest,
    RuleDecision,
    RuleJudgeOutcome,
)
from services.api.app.domain.interaction import InteractionOperation

H5_EVIDENCE_TARGET_UNREACHABLE = "h5_evidence_target_unreachable"
_ENTITLEMENT_LOCKED = "entitlement_locked"
_BASE_PAGE = "base_page"


@dataclass(frozen=True, slots=True)
class H5EvidenceReachabilityBoundaryResult:
    """H5 不可达补采请求的确定性收口结果。"""

    outcome: RuleJudgeOutcome
    suppressed_request_ids: tuple[str, ...] = ()

    @property
    def suppressed(self) -> bool:
        return bool(self.suppressed_request_ids)


def apply_h5_evidence_reachability_boundary(
    *,
    outcome: RuleJudgeOutcome,
    target_platform: str | None,
) -> H5EvidenceReachabilityBoundaryResult:
    """锁定页没有安全关闭语义时，不创建必然失败的 H5 Probe。"""

    if not isinstance(outcome, RuleJudgeOutcome):
        raise TypeError("outcome 必须为 RuleJudgeOutcome")
    if str(target_platform or "").strip().casefold() != "h5":
        return H5EvidenceReachabilityBoundaryResult(outcome=outcome)

    suppressed = tuple(
        request
        for request in outcome.evidence_requests
        if _is_unreachable_locked_base_page_request(request)
    )
    if not suppressed:
        return H5EvidenceReachabilityBoundaryResult(outcome=outcome)

    suppressed_ids = frozenset(request.request_id for request in suppressed)
    suppressed_rule_ids = frozenset(request.rule_id for request in suppressed)
    effective = RuleJudgeOutcome(
        work_item_id=outcome.work_item_id,
        attempt_id=outcome.attempt_id,
        task_id=outcome.task_id,
        decisions=(
            *outcome.decisions,
            *(
                _unreachable_decision(outcome=outcome, request=request)
                for request in suppressed
            ),
        ),
        evidence_requests=tuple(
            request
            for request in outcome.evidence_requests
            if request.request_id not in suppressed_ids
        ),
        deferred_decisions=tuple(
            decision
            for decision in outcome.deferred_decisions
            if decision.rule_id not in suppressed_rule_ids
        ),
    )
    return H5EvidenceReachabilityBoundaryResult(
        outcome=effective,
        suppressed_request_ids=tuple(request.request_id for request in suppressed),
    )


def _is_unreachable_locked_base_page_request(request: EvidenceRequest) -> bool:
    interaction = request.required_interaction
    invariants = request.target_invariants
    semantic = invariants.get("semantic")
    structure = invariants.get("structure")
    return bool(
        interaction is not None
        and interaction.operation is InteractionOperation.CLICK
        and interaction.semantic_target == OVERLAY_CLOSE_SEMANTIC_TARGET
        and isinstance(semantic, Mapping)
        and semantic.get("page_state") == _ENTITLEMENT_LOCKED
        and isinstance(structure, Mapping)
        and structure.get("layer_target") == _BASE_PAGE
    )


def _unreachable_decision(
    *,
    outcome: RuleJudgeOutcome,
    request: EvidenceRequest,
) -> RuleDecision:
    reachability = {
        "request_id": request.request_id,
        "target_capture_id": request.target_capture_id,
        "reason_code": H5_EVIDENCE_TARGET_UNREACHABLE,
        "disposition": "suppressed",
        "page_state": _ENTITLEMENT_LOCKED,
    }
    deferred = outcome.deferred_decision_for(request.rule_id)
    if deferred is None:
        payload: dict[str, object] = {
            "execution_status": "evidence_insufficient",
            "result_summary": "当前 H5 状态没有可信的安全关闭路径，底层页面证据不可达。",
            "problem_desc": request.evidence_goal,
            "error_code": H5_EVIDENCE_TARGET_UNREACHABLE,
            "error_message": "锁定态不允许把付费或权限门槛当作普通弹窗关闭。",
            "judge_output": {"h5_evidence_reachability": reachability},
        }
        observation_packet_id = None
    else:
        payload = dict(deferred.payload)
        judge_output = payload.get("judge_output")
        merged_output = dict(judge_output) if isinstance(judge_output, Mapping) else {}
        merged_output["h5_evidence_reachability"] = reachability
        payload["judge_output"] = merged_output
        payload["execution_status"] = "evidence_insufficient"
        payload["error_code"] = H5_EVIDENCE_TARGET_UNREACHABLE
        payload["error_message"] = "锁定态不允许把付费或权限门槛当作普通弹窗关闭。"
        observation_packet_id = deferred.observation_packet_id
    return RuleDecision(
        work_item_id=request.work_item_id,
        attempt_id=request.attempt_id,
        rule_id=request.rule_id,
        rule_code=request.rule_code,
        execution_status="evidence_insufficient",
        observation_packet_id=observation_packet_id,
        payload=payload,
    )


__all__ = [
    "H5_EVIDENCE_TARGET_UNREACHABLE",
    "H5EvidenceReachabilityBoundaryResult",
    "apply_h5_evidence_reachability_boundary",
]
