"""绿色时序原件事实消费；未具业务绑定的呈现观察不能构成业务判定。"""

from __future__ import annotations

from services.api.app.application.green_rule_catalog import GREEN_RULES
from services.api.app.application.green_rule_evidence import _MISSING_FACTS, GreenSubitem
from services.api.app.application.green_temporal_facts import (
    PresentationKind,
    read_temporal_observations,
)

__all__ = ["judge_temporal_subitem", "judge_temporal_subitems"]


_RELEVANT = {
    "empty_state_meaning": {PresentationKind.EMPTY_COPY},
    "load_failure_meaning": {PresentationKind.EMPTY_COPY, PresentationKind.FAILURE_COPY,
                             PresentationKind.GUIDANCE_COPY, PresentationKind.RETRY_TARGET},
    "failure_reason": {PresentationKind.REASON_COPY, PresentationKind.REQUEST_FAILURE},
    "retry_entry": {PresentationKind.RETRY_TARGET, PresentationKind.REQUEST_FAILURE},
    "loading_feedback": {PresentationKind.LOADING_COPY},
    "business_success_feedback": {PresentationKind.TRANSIENT_MESSAGE},
    "empty_state_guidance": {PresentationKind.EMPTY_COPY},
}
_LABELS = {
    PresentationKind.EMPTY_COPY: "可见空态文案",
    PresentationKind.FAILURE_COPY: "可见失败文案",
    PresentationKind.REASON_COPY: "可见网络或超时原因文案",
    PresentationKind.GUIDANCE_COPY: "可见刷新或重试引导文案",
    PresentationKind.RETRY_TARGET: "可见且可命中的重试或刷新控件",
    PresentationKind.LOADING_COPY: "可见加载中文案",
    PresentationKind.MEDIA_SNAPSHOT: "媒体状态快照",
    PresentationKind.REQUEST_FAILURE: "真实请求失败事件",
    PresentationKind.TRANSIENT_MESSAGE: "动作轨迹中的瞬时提示记录",
}


def judge_temporal_subitem(rule_key, key, evidence, *, required_facts=None):
    """正式入口只读既有原件；当前生产器尚不能给业务条件与完整过程赋值。"""
    return judge_temporal_subitems(
        rule_key, evidence, required_facts=required_facts, subchecks=(key,),
    )[0]


def judge_temporal_subitems(rule_key, evidence, *, required_facts=None, subchecks=None):
    """单次规则只读一次原件，在该读取快照上按冻结顺序形成全部分项观察。"""
    definition = next((item for item in GREEN_RULES if item.key == rule_key), None)
    if rule_key not in {"s72", "s49", "s73", "62"} or definition is None:
        raise ValueError("不支持的绿色时序分项")
    keys = definition.subchecks if subchecks is None else tuple(subchecks)
    if (not keys or any(key not in definition.subchecks for key in keys)
            or len(set(keys)) != len(keys)):
        raise ValueError("不支持的绿色时序分项")
    requirements = _MISSING_FACTS if required_facts is None else required_facts
    observed = read_temporal_observations(evidence)
    return tuple(_observed_subitem(rule_key, key, observed, requirements) for key in keys)


def _observed_subitem(rule_key, key, observed, requirements):
    """复用本次读取结果，不重读原件或从缺少业务身份的文案推定业务状态。"""
    relevant = _RELEVANT.get(key, set())
    if rule_key == "62":
        relevant = {PresentationKind.MEDIA_SNAPSHOT}
    facts = tuple(item for item in observed.facts if item.kind in relevant)
    labels = tuple(dict.fromkeys(_LABELS[item.kind] for item in facts))
    reason = (
        "已从核验原件观察到" + "、".join(labels) + "；"
        if labels else "现有原件未形成该分项可用的呈现观察；"
    )
    reason += "尚未证明同模块业务条件、唯一因果或完整过程，不形成业务通过或失败结论"
    return GreenSubitem(
        key, "evidence_insufficient", reason,
        tuple(dict.fromkeys((*observed.issues, *requirements[key]))),
        tuple(dict.fromkeys(item.capture_id for item in facts)),
    )
