"""规则结论复用声明与复用记录（04 专项 §5.5、§17.5，D 期「规则范围灰度」）。

规则合同里的 ``reuse_policy`` 声明这条规则的结论能不能在**同一次任务里**沿用到别的页面：

- ``per_page``：默认。每个页面、每次弹窗出现、每个广告位各自执行，不复用；
- ``same_object``：同一对象（同一条内容、商品、链接、二维码）再出现时沿用第一次的结论；
- ``same_material``：同一广告素材版本只判一次它自己的静态属性，位置、遮挡、关闭、跳转不复用；
- ``per_task``：整个任务一次，首个合格结果沿用到其余页面；
- ``sealed_set``：跨页汇总规则，等封存集合齐了判一次（由聚合规则机制执行，本模块只识别声明）。

缺失或非法一律按 ``per_page``，不替规则放宽（设计：规则没有声明复用条件时保持原执行频率）。
每次复用必须记下来源、为什么能复用、什么条件下失效；复用只在同一次任务里，不跨任务。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from services.api.app.domain.inspection.handoff import InspectionHandoff, InspectionRuleTarget
from services.api.app.domain.inspection.judgement import RuleDecision

RULE_REUSE_CONTRACT_KEY = "reuse_policy"
RULE_REUSE_RECORD_PROTOCOL_VERSION = "rule-reuse.v1"


class RuleReuseScope(StrEnum):
    """规则结论的复用范围。"""

    PER_PAGE = "per_page"
    SAME_OBJECT = "same_object"
    SAME_MATERIAL = "same_material"
    PER_TASK = "per_task"
    SEALED_SET = "sealed_set"


REUSE_SCOPE_LABELS: Mapping[RuleReuseScope, str] = {
    RuleReuseScope.PER_PAGE: "每页各自执行",
    RuleReuseScope.SAME_OBJECT: "同一对象复用",
    RuleReuseScope.SAME_MATERIAL: "同一素材复用",
    RuleReuseScope.PER_TASK: "整个任务一次",
    RuleReuseScope.SEALED_SET: "跨页汇总一次",
}

# 真正会在判定前查复用的范围；sealed_set 由聚合规则机制自己执行，per_page 不复用。
LOOKUP_REUSE_SCOPES: frozenset[RuleReuseScope] = frozenset(
    {RuleReuseScope.PER_TASK, RuleReuseScope.SAME_OBJECT, RuleReuseScope.SAME_MATERIAL}
)

# 只有机器已经形成的正式结论才能被沿用；转人工、证据不足、跳过、异常都不是可沿用的结论。
REUSABLE_SOURCE_STATUSES: frozenset[str] = frozenset({"pass", "fail", "warning"})

INVALIDATION_LABELS: Mapping[str, str] = {
    "account_or_login_changed": "账号或登录态变化",
    "environment_changed": "换端、换分辨率或换浏览器",
    "product_declaration_changed": "产品配置变化",
    "rule_version_changed": "规则版本变化",
    "object_changed": "对象换了",
    "object_content_changed": "对象本身内容变化",
    "material_changed": "素材版本变化",
    "member_set_changed": "汇总集合成员变动",
    "task_boundary": "只在同一次任务里有效",
}

_INVALIDATION_BY_SCOPE: Mapping[RuleReuseScope, tuple[str, ...]] = {
    RuleReuseScope.PER_PAGE: (),
    RuleReuseScope.SAME_OBJECT: (
        "object_changed",
        "object_content_changed",
        "rule_version_changed",
        "task_boundary",
    ),
    RuleReuseScope.SAME_MATERIAL: ("material_changed", "rule_version_changed", "task_boundary"),
    RuleReuseScope.PER_TASK: (
        "account_or_login_changed",
        "environment_changed",
        "product_declaration_changed",
        "rule_version_changed",
        "task_boundary",
    ),
    RuleReuseScope.SEALED_SET: ("member_set_changed", "rule_version_changed", "task_boundary"),
}


def _scope_from_text(value: Any) -> RuleReuseScope | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    try:
        return RuleReuseScope(text)
    except ValueError:
        return None


def parse_reuse_declaration(input_contract: Mapping[str, Any] | None) -> RuleReuseScope:
    """读取规则合同里的复用声明；缺失或非法一律按 per_page，不替规则放宽。

    合同写法：``"reuse_policy": "per_task"`` 或 ``{"scope": "per_task"}``。
    """

    contract = input_contract if isinstance(input_contract, Mapping) else {}
    declared = contract.get(RULE_REUSE_CONTRACT_KEY)
    if isinstance(declared, Mapping):
        declared = declared.get("scope")
    return _scope_from_text(declared) or RuleReuseScope.PER_PAGE


def reuse_scope_label(scope: RuleReuseScope | str) -> str:
    """复用范围的大白话名字，工作台直接显示。"""

    resolved = _scope_from_text(scope) or RuleReuseScope.PER_PAGE
    return REUSE_SCOPE_LABELS[resolved]


def invalidation_conditions(scope: RuleReuseScope | str) -> tuple[str, ...]:
    """某个复用范围下会让复用失效的条件代码。"""

    resolved = _scope_from_text(scope) or RuleReuseScope.PER_PAGE
    return _INVALIDATION_BY_SCOPE[resolved]


def invalidation_label(code: str) -> str:
    return INVALIDATION_LABELS.get(code, code)


@dataclass(frozen=True, kw_only=True)
class RuleReuseRecord:
    """一次复用的来源、依据与失效条件；原样写进复用结论的 judge_output.reuse。"""

    scope: RuleReuseScope
    reuse_key: str
    source_execution_id: str
    source_page_capture_id: str
    source_execution_status: str
    reason: str
    source_agent_run_id: str | None = None
    source_page_type: str = ""
    source_page_url: str = ""
    basis: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = RULE_REUSE_RECORD_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.scope, RuleReuseScope) or self.scope not in LOOKUP_REUSE_SCOPES:
            raise ValueError("复用记录的范围必须是会查复用的范围")
        for name in ("reuse_key", "source_execution_id", "source_page_capture_id", "reason"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"复用记录缺少 {name}")
        if self.source_execution_status not in REUSABLE_SOURCE_STATUSES:
            raise ValueError("只有 pass/fail/warning 的正式结论可以被沿用")
        if self.protocol_version != RULE_REUSE_RECORD_PROTOCOL_VERSION:
            raise ValueError("复用记录协议版本不受支持")
        object.__setattr__(self, "basis", dict(self.basis))

    @property
    def invalidate_on(self) -> tuple[str, ...]:
        return invalidation_conditions(self.scope)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "scope": self.scope.value,
            "scope_label": REUSE_SCOPE_LABELS[self.scope],
            "reuse_key": self.reuse_key,
            "source_execution_id": self.source_execution_id,
            "source_page_capture_id": self.source_page_capture_id,
            "source_execution_status": self.source_execution_status,
            "source_agent_run_id": self.source_agent_run_id,
            "source_page_type": self.source_page_type,
            "source_page_url": self.source_page_url,
            "reason": self.reason,
            "basis": dict(self.basis),
            "invalidate_on": list(self.invalidate_on),
        }


def build_reused_decision(
    *,
    handoff: InspectionHandoff,
    rule: InspectionRuleTarget,
    record: RuleReuseRecord,
    source_result_summary: str | None = None,
    source_problem_desc: str | None = None,
    source_confidence: float | None = None,
    source_judge_output: Mapping[str, Any] | None = None,
) -> RuleDecision:
    """把可沿用的来源结论确定性转成当前页面的规则判定，不再调用 Judge。

    结论状态原样沿用；证据不带自己的附件，来源执行记录通过 judge_output.reuse 追溯。
    """

    source_output = source_judge_output if isinstance(source_judge_output, Mapping) else {}
    where = record.source_page_type or "来源页面"
    reasoning = source_output.get("reasoning_summary")
    if not (isinstance(reasoning, str) and reasoning.strip()):
        reasoning = f"沿用{where}上的结论，本页未重新判定"
    # 写回契约（llm/hybrid）要求 schema_version=1.0、conclusion 与状态一致、reasoning_summary
    # 非空，hybrid 还要非空 tool_results。沿用记录没有自己的工具结果，用一条「沿用」标记如实
    # 说明；少了这些，每条沿用记录都会被写回校验整条打成执行异常（2026-09-22 测试服 App
    # 任务 64 条、Web 任务 12 条实锤，全是 per_task 的 ops_1 / ops_2）。
    judge_output: dict[str, Any] = {
        "schema_version": "1.0",
        "conclusion": record.source_execution_status,
        "reasoning_summary": reasoning,
        "tool_results": [
            {
                "tool_name": "rule_result_reuse",
                "status": "reused",
                "source_execution_id": record.source_execution_id,
            }
        ],
        "reuse": record.to_dict(),
    }
    summary = (source_result_summary or "").strip()
    payload: dict[str, Any] = {
        "execution_status": record.source_execution_status,
        "result_summary": f"沿用{where}上的结论" + (f"：{summary}" if summary else ""),
        "problem_desc": (source_problem_desc or "").strip() or None,
        "evidence_artifact_ids": [],
        "judge_input": {
            "schema_version": RULE_REUSE_RECORD_PROTOCOL_VERSION,
            "source_execution_id": record.source_execution_id,
            "reuse_scope": record.scope.value,
            "reuse_key": record.reuse_key,
        },
        "judge_output": judge_output,
    }
    if source_confidence is not None:
        payload["confidence"] = float(source_confidence)
    if payload["problem_desc"] is None:
        del payload["problem_desc"]
    return RuleDecision(
        work_item_id=handoff.work_item_id,
        attempt_id=handoff.attempt_id,
        rule_id=rule.rule_id,
        rule_code=rule.rule_code,
        execution_status=record.source_execution_status,
        payload=payload,
    )


__all__ = [
    "INVALIDATION_LABELS",
    "LOOKUP_REUSE_SCOPES",
    "REUSABLE_SOURCE_STATUSES",
    "REUSE_SCOPE_LABELS",
    "RULE_REUSE_CONTRACT_KEY",
    "RULE_REUSE_RECORD_PROTOCOL_VERSION",
    "RuleReuseRecord",
    "RuleReuseScope",
    "build_reused_decision",
    "invalidation_conditions",
    "invalidation_label",
    "parse_reuse_declaration",
    "reuse_scope_label",
]
