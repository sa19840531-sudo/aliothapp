"""S35只读响应判定；只扩大现有正式展示协议可证明的局部操作。"""

from __future__ import annotations

from services.api.app.application.green_display_response import read_extended_display_response
from services.api.app.application.green_evidence_contracts import GreenEvidenceSet
from services.api.app.application.green_local_response import _local_response
from services.api.app.application.green_rule_evidence import GreenSubitem


def judge_green_correct_response(evidence: GreenEvidenceSet) -> GreenSubitem:
    """消费全部冻结动作；未知动作和缺项不能被已有局部成功掩盖。"""
    if not isinstance(evidence, GreenEvidenceSet) or not evidence.verified_read:
        raise ValueError("交互判读仅接受权威只读加载器的原件")
    if evidence.target_platform != "h5":
        return GreenSubitem(
            "correct_response",
            "evidence_insufficient",
            "现有正式展示协议仅接入H5，其他端仍缺对应原生事实适配",
            ("trusted_h5_task_identity",),
        )
    captures = {capture.capture_id: capture for capture in evidence.captures}
    missing, observed, references, action_ids = list(evidence.issues), [], [], []
    if len(captures) != len(evidence.captures):
        missing.append("unique_formal_capture_identity")
    if len({action.action_id for action in evidence.actions}) != len(evidence.actions):
        missing.append("independent_formal_action_identity")
    for action in evidence.actions:
        try:
            before = captures[action.source_capture_id]
            after = captures[action.result_capture_id]
            outcome = _local_response(action, before, after, evidence.task_id)
            if outcome is None:
                outcome = read_extended_display_response(action, before, after, evidence.task_id)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            outcome = None
        if outcome is None:
            missing.append("native_local_response_causality:" + action.action_id)
            continue
        observed.append(outcome)
        references.extend((action.source_capture_id, action.result_capture_id))
        action_ids.append(action.action_id)
    if not evidence.actions:
        missing.append("formal_local_display_action")
    failures = [reason for status, reason in observed if status == "fail"]
    if failures:
        status, reason = "fail", "；".join(dict.fromkeys(failures))
    elif observed and not missing:
        status, reason = "pass", "本次冻结材料内的正式只读展示动作均出现同对象预期响应"
    else:
        status = "evidence_insufficient"
        reason = (
            "已观察到部分只读展示响应，仍有动作或材料不能建立完整因果"
            if observed
            else "尚缺可复核的正式只读展示动作、同对象前后原件或原生效果"
        )
    return GreenSubitem(
        "correct_response",
        status,
        reason,
        tuple(dict.fromkeys(missing)),
        tuple(dict.fromkeys(references)),
        tuple(dict.fromkeys(action_ids)),
    )
