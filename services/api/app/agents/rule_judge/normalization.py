"""Rule Judge 原始结果到 V2 Outcome 的纯数据规范化。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.api.app.domain.inspection import (
    EvidenceRequest,
    InspectionHandoff,
    RuleDecision,
    RuleJudgeOutcome,
    RuleJudgeOutcomeValidationError,
)
from services.api.app.domain.inspection.evidence_gap import materialize_evidence_request

_EVIDENCE_GAP_CODES = frozenset(
    {
        "TRUSTED_EVIDENCE_INVALID",
        "EVIDENCE_INSUFFICIENT",
        "OBSERVATION_PACKET_INCOMPLETE",
    }
)


def normalize_rule_judge_outcome(
    *,
    handoff: InspectionHandoff,
    raw: Mapping[str, Any],
) -> RuleJudgeOutcome:
    """把单条或批量输出规范化为完整、可路由的结果。"""
    raw_items = raw.get("items")
    if isinstance(raw_items, list):
        candidates = [item for item in raw_items if isinstance(item, Mapping)]
    else:
        candidates = [raw]
    by_rule_code: dict[str, Mapping[str, Any]] = {}
    for item in candidates:
        rule_code = str(item.get("rule_code") or "").strip()
        if not rule_code:
            if len(handoff.rules) > 1:
                raise RuleJudgeOutcomeValidationError("批量 Judge 子项缺少 rule_code")
            continue
        if rule_code in by_rule_code:
            raise RuleJudgeOutcomeValidationError("批量 Judge 输出包含重复 rule_code")
        by_rule_code[rule_code] = item
    expected_rule_codes = {item.rule_code for item in handoff.rules}
    unexpected = sorted(set(by_rule_code).difference(expected_rule_codes))
    if unexpected:
        raise RuleJudgeOutcomeValidationError(
            f"Judge 输出包含工作项以外的规则：{', '.join(unexpected)}"
        )

    decisions: list[RuleDecision] = []
    evidence_requests: list[EvidenceRequest] = []
    deferred_decisions: list[RuleDecision] = []
    for target in handoff.rules:
        item = by_rule_code.get(target.rule_code)
        if item is None and len(handoff.rules) == 1 and len(candidates) == 1:
            item = candidates[0]
        if item is None:
            decisions.append(
                _terminal_decision(
                    handoff=handoff,
                    rule_id=target.rule_id,
                    rule_code=target.rule_code,
                    execution_status="error",
                    reason_code="RULE_JUDGE_OUTPUT_MISSING",
                    reason="Rule Judge 未返回当前锁定规则的输出。",
                )
            )
            continue

        error = item.get("error")
        error_mapping = error if isinstance(error, Mapping) else {}
        reason_code = _text(error_mapping.get("code"))
        execution_status = _text(item.get("execution_status"))
        if execution_status == "evidence_insufficient":
            judge_output = item.get("judge_output")
            output_mapping = judge_output if isinstance(judge_output, Mapping) else {}
            evidence_request, effective_item = _materialize_gap_request(
                handoff=handoff,
                rule_id=target.rule_id,
                rule_code=target.rule_code,
                item=item,
            )
            insufficient_decision = _evidence_insufficient_decision(
                handoff=handoff,
                rule_id=target.rule_id,
                rule_code=target.rule_code,
                reason_code="EVIDENCE_INSUFFICIENT",
                reason=(
                    _text(item.get("problem_desc"))
                    or _text(output_mapping.get("reasoning_summary"))
                    or "现有 Observation Packet 不足以形成可信判定。"
                ),
                payload=effective_item,
            )
            if evidence_request is not None:
                # 请求发出时判定先暂缓；请求被门禁抑制时，写回层用这份带缺口的判定收口。
                evidence_requests.append(evidence_request)
                deferred_decisions.append(insufficient_decision)
                continue
            decisions.append(insufficient_decision)
            continue
        if reason_code in _EVIDENCE_GAP_CODES:
            decisions.append(
                _evidence_insufficient_decision(
                    handoff=handoff,
                    rule_id=target.rule_id,
                    rule_code=target.rule_code,
                    reason_code=reason_code,
                    reason=(
                        _text(error_mapping.get("message"))
                        or "现有 Observation Packet 不足以形成可信判定。"
                    ),
                    payload=item,
                )
            )
            continue
        if execution_status is None:
            decisions.append(
                _terminal_decision(
                    handoff=handoff,
                    rule_id=target.rule_id,
                    rule_code=target.rule_code,
                    execution_status="error",
                    reason_code=reason_code or "RULE_JUDGE_NO_DECISION",
                    reason=(
                        _text(error_mapping.get("message"))
                        or "Rule Judge 未形成结构化 Rule Decision。"
                    ),
                    payload=item,
                )
            )
            continue
        decisions.append(
            RuleDecision.from_payload(
                work_item_id=handoff.work_item_id,
                attempt_id=handoff.attempt_id,
                rule_id=target.rule_id,
                rule_code=target.rule_code,
                payload=item,
            )
        )
    return RuleJudgeOutcome(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        task_id=handoff.task_id,
        decisions=tuple(decisions),
        evidence_requests=tuple(evidence_requests),
        deferred_decisions=tuple(deferred_decisions),
    )


def _materialize_gap_request(
    *,
    handoff: InspectionHandoff,
    rule_id: str,
    rule_code: str,
    item: Mapping[str, Any],
) -> tuple[EvidenceRequest | None, Mapping[str, Any]]:
    judge_output = item.get("judge_output")
    if not isinstance(judge_output, Mapping):
        return None, item
    gap = judge_output.get("evidence_gap")
    if not isinstance(gap, Mapping):
        return None, item
    capture_id = handoff.evidence.page_capture_id
    source = judge_output.get("evidence_gap_source")
    evidence = handoff.evidence_for_rule(rule_code)
    if (
        isinstance(source, Mapping)
        and source.get("target_platform") == "h5"
        and source.get("surface_kind") == "web"
        and source.get("page_capture_id") == evidence.page_capture_id
        and evidence.manifest_id is not None
        and source.get("manifest_id") == evidence.manifest_id
    ):
        capture_id = evidence.page_capture_id
    try:
        request = materialize_evidence_request(
            gap=gap,
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            rule_id=rule_id,
            rule_code=rule_code,
            page_capture_id=capture_id,
            agent_run_id=handoff.agent_run_id,
        )
    except ValueError as exc:
        effective_item = dict(item)
        effective_output = dict(judge_output)
        effective_output["evidence_request_rejection"] = {
            "reason_code": "EVIDENCE_REQUEST_CONTRACT_INVALID",
            "message": str(exc),
        }
        effective_item["judge_output"] = effective_output
        return None, effective_item
    if request is not None and (
        isinstance(source, Mapping) and source.get("target_platform") == "h5"
        and source.get("surface_kind") == "web"
        and source.get("page_capture_id") == evidence.page_capture_id
        and evidence.manifest_id is not None
        and source.get("manifest_id") == evidence.manifest_id
    ):
        from services.api.app.application.h5_recapture_coalescing import (
            complete_observation_profile,
        )

        completed = complete_observation_profile(request)
        if completed is not request:
            # 原缺口保持不变，实际派发的新合同另记审计，不能只改变请求哈希。
            effective_item = dict(item)
            effective_item["judge_output"] = {
                **judge_output,
                "evidence_request_profile_completion": {
                    "original_request_id": request.request_id,
                    "original_requirement_hash": request.requirement_hash,
                    "original_artifact_types": list(request.required_artifact_types),
                    "request_id": completed.request_id,
                    "requirement_hash": completed.requirement_hash,
                    "required_artifact_types": list(completed.required_artifact_types),
                },
            }
            return completed, effective_item
    return request, item


def _evidence_insufficient_decision(
    *,
    handoff: InspectionHandoff,
    rule_id: str,
    rule_code: str,
    reason_code: str,
    reason: str,
    payload: Mapping[str, Any] | None = None,
) -> RuleDecision:
    return _terminal_decision(
        handoff=handoff,
        rule_id=rule_id,
        rule_code=rule_code,
        execution_status="evidence_insufficient",
        reason_code=reason_code,
        reason=reason,
        payload=payload,
    )


def _terminal_decision(
    *,
    handoff: InspectionHandoff,
    rule_id: str,
    rule_code: str,
    execution_status: str,
    reason_code: str,
    reason: str,
    payload: Mapping[str, Any] | None = None,
) -> RuleDecision:
    decision_payload = dict(payload or {})
    decision_payload.update(
        {
            "execution_status": execution_status,
            "error_code": reason_code,
            "error_message": reason,
        }
    )
    decision_payload.setdefault(
        "result_summary",
        "证据不足" if execution_status == "evidence_insufficient" else "规则判定异常",
    )
    decision_payload.setdefault("problem_desc", reason)
    decision_payload.setdefault(
        "evidence_artifact_ids",
        list(handoff.evidence.artifact_ids),
    )
    if handoff.evidence.observation_packet_ids:
        decision_payload.setdefault(
            "observation_packet_id",
            handoff.evidence.observation_packet_ids[0],
        )
    return RuleDecision.from_payload(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        rule_id=rule_id,
        rule_code=rule_code,
        payload=decision_payload,
    )


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
