"""Web 正式质检的主动交互安全边界。"""

from __future__ import annotations

from dataclasses import dataclass

from services.api.app.domain.inspection import RuleDecision, RuleJudgeOutcome

WEB_ACTIVE_INTERACTION_NOT_ENABLED = "web_active_interaction_not_enabled"


@dataclass(frozen=True, slots=True)
class WebActiveInteractionBoundaryResult:
    """Web 主动交互请求的确定性收口结果。"""

    outcome: RuleJudgeOutcome
    suppressed_request_ids: tuple[str, ...] = ()

    @property
    def suppressed(self) -> bool:
        """返回是否抑制了需要主动交互的补证请求。"""

        return bool(self.suppressed_request_ids)


def apply_web_active_interaction_boundary(
    *,
    outcome: RuleJudgeOutcome,
    target_platform: str | None,
    work_session_dependency: str,
) -> WebActiveInteractionBoundaryResult:
    """Web 正式规则需要继续交互时，直接形成可审计证据不足结论。"""

    if not isinstance(outcome, RuleJudgeOutcome):
        raise TypeError("outcome 必须为 RuleJudgeOutcome")
    platform = str(target_platform or "").strip().casefold()
    if platform != "web" or not outcome.evidence_requests:
        return WebActiveInteractionBoundaryResult(outcome=outcome)

    requests_to_suppress = tuple(
        request
        for request in outcome.evidence_requests
        if work_session_dependency == "active_session"
        or request.session_dependency.value == "active_session"
        or request.required_interaction is not None
        or request.side_effect_policy.value != "read_only"
    )
    if not requests_to_suppress:
        return WebActiveInteractionBoundaryResult(outcome=outcome)

    suppressed_ids = frozenset(request.request_id for request in requests_to_suppress)
    converted = tuple(
        RuleDecision(
            work_item_id=request.work_item_id,
            attempt_id=request.attempt_id,
            rule_id=request.rule_id,
            rule_code=request.rule_code,
            execution_status="evidence_insufficient",
            payload={
                "execution_status": "evidence_insufficient",
                "result_summary": (
                    "Web 正式质检不执行未授权主动交互，规则按证据不足收口。"
                ),
                "problem_desc": request.evidence_goal,
                "error_code": WEB_ACTIVE_INTERACTION_NOT_ENABLED,
                "error_message": "Web 正式质检仅消费已持久化证据，未执行主动交互。",
                "judge_input": {
                    "web_active_interaction": {
                        "source": "persisted_evidence_only",
                        "original_reason_code": request.reason_code,
                        "session_dependency": request.session_dependency.value,
                        "side_effect_policy": request.side_effect_policy.value,
                        "required_interaction": (
                            request.required_interaction.to_dict()
                            if request.required_interaction is not None
                            else None
                        ),
                    }
                },
                "judge_output": {
                    "web_active_interaction": {
                        "request_id": request.request_id,
                        "target_capture_id": request.target_capture_id,
                        "reason_code": WEB_ACTIVE_INTERACTION_NOT_ENABLED,
                        "disposition": "suppressed",
                    }
                },
            },
        )
        for request in requests_to_suppress
    )
    effective = RuleJudgeOutcome(
        work_item_id=outcome.work_item_id,
        attempt_id=outcome.attempt_id,
        task_id=outcome.task_id,
        decisions=(*outcome.decisions, *converted),
        evidence_requests=tuple(
            request
            for request in outcome.evidence_requests
            if request.request_id not in suppressed_ids
        ),
    )
    return WebActiveInteractionBoundaryResult(
        outcome=effective,
        suppressed_request_ids=tuple(
            request.request_id for request in requests_to_suppress
        ),
    )


__all__ = [
    "WEB_ACTIVE_INTERACTION_NOT_ENABLED",
    "WebActiveInteractionBoundaryResult",
    "apply_web_active_interaction_boundary",
]
