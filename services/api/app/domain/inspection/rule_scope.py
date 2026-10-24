"""聚合规则在一次 TaskExecution 中的冻结适用范围计划。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.api.app.domain.protocol import stable_protocol_hash

RULE_SCOPE_PLAN_PROTOCOL_VERSION = "rule-scope-plan.v1"
_SUPPORTED_AUTHORITY_TYPES = frozenset({"web_coverage_snapshot", "app_exploration_result"})


class RuleScopeValidationError(ValueError):
    """规则范围计划缺少权威来源或包含互相冲突的事实。"""


class RuleScopeStatus(StrEnum):
    """规则在当前执行中的适用性结论。"""

    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNPROVEN = "unproven"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleScopeAuthority:
    """来自同一 Run/Epoch 的不可变页面适用性事实。"""

    task_id: str
    source_agent_run_id: str
    finalization_epoch: int
    authority_type: str
    authority_id: str
    authority_hash: str
    scope_verified: bool
    applicable_page_types: tuple[str, ...]
    not_applicable_page_types: tuple[str, ...]
    unresolved_page_types: tuple[str, ...]
    negative_evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "source_agent_run_id",
            "authority_type",
            "authority_id",
            "authority_hash",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.authority_type not in _SUPPORTED_AUTHORITY_TYPES:
            raise RuleScopeValidationError("authority_type 不受支持")
        if isinstance(self.finalization_epoch, bool) or self.finalization_epoch < 0:
            raise RuleScopeValidationError("finalization_epoch 必须为非负整数")
        for field_name in (
            "applicable_page_types",
            "not_applicable_page_types",
            "unresolved_page_types",
            "negative_evidence_refs",
        ):
            normalized = _normalize_texts(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, normalized)
        page_sets = (
            set(self.applicable_page_types),
            set(self.not_applicable_page_types),
            set(self.unresolved_page_types),
        )
        if any(
            left & right for index, left in enumerate(page_sets) for right in page_sets[index + 1 :]
        ):
            raise RuleScopeValidationError("页面适用性分区不能交叉")
        if self.not_applicable_page_types and not self.scope_verified:
            raise RuleScopeValidationError("不适用结论必须来自已验证的范围快照")
        if self.not_applicable_page_types and not self.negative_evidence_refs:
            raise RuleScopeValidationError("不适用结论必须包含负向依据引用")

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "source_agent_run_id": self.source_agent_run_id,
            "finalization_epoch": self.finalization_epoch,
            "authority_type": self.authority_type,
            "authority_id": self.authority_id,
            "authority_hash": self.authority_hash,
            "scope_verified": self.scope_verified,
            "applicable_page_types": list(self.applicable_page_types),
            "not_applicable_page_types": list(self.not_applicable_page_types),
            "unresolved_page_types": list(self.unresolved_page_types),
            "negative_evidence_refs": list(self.negative_evidence_refs),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleScopePlan:
    """一条规则基于权威适用性事实生成的不可变计划。"""

    task_id: str
    source_agent_run_id: str
    finalization_epoch: int
    rule_id: str
    declared_page_types: tuple[str, ...]
    matched_applicable_page_types: tuple[str, ...]
    matched_not_applicable_page_types: tuple[str, ...]
    unresolved_page_types: tuple[str, ...]
    status: RuleScopeStatus
    authority_type: str
    authority_id: str
    authority_hash: str
    negative_evidence_refs: tuple[str, ...]
    plan_hash: str
    protocol_version: str = RULE_SCOPE_PLAN_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            **self.hash_material(),
            "plan_hash": self.plan_hash,
        }

    def hash_material(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "source_agent_run_id": self.source_agent_run_id,
            "finalization_epoch": self.finalization_epoch,
            "rule_id": self.rule_id,
            "declared_page_types": list(self.declared_page_types),
            "matched_applicable_page_types": list(self.matched_applicable_page_types),
            "matched_not_applicable_page_types": list(self.matched_not_applicable_page_types),
            "unresolved_page_types": list(self.unresolved_page_types),
            "status": self.status.value,
            "authority_type": self.authority_type,
            "authority_id": self.authority_id,
            "authority_hash": self.authority_hash,
            "negative_evidence_refs": list(self.negative_evidence_refs),
        }


def build_rule_scope_plan(
    *,
    authority: RuleScopeAuthority,
    rule_id: str,
    applicable_page_types: tuple[str, ...],
) -> RuleScopePlan:
    """将规则页型声明投影到同一执行的冻结适用性分区。"""

    normalized_rule_id = _require_text(rule_id, "rule_id")
    declared = _normalize_texts(applicable_page_types, "applicable_page_types")
    applicable = set(authority.applicable_page_types)
    not_applicable = set(authority.not_applicable_page_types)
    unresolved_authority = set(authority.unresolved_page_types)
    if declared:
        matched_applicable = tuple(item for item in declared if item in applicable)
        matched_not_applicable = tuple(item for item in declared if item in not_applicable)
        unresolved = tuple(
            item
            for item in declared
            if item in unresolved_authority
            or item not in applicable | not_applicable | unresolved_authority
        )
        if matched_applicable:
            status = RuleScopeStatus.APPLICABLE
        elif authority.scope_verified and len(matched_not_applicable) == len(declared):
            status = RuleScopeStatus.NOT_APPLICABLE
        else:
            status = RuleScopeStatus.UNPROVEN
    else:
        matched_applicable = authority.applicable_page_types
        matched_not_applicable = ()
        unresolved = authority.unresolved_page_types
        status = RuleScopeStatus.APPLICABLE if matched_applicable else RuleScopeStatus.UNPROVEN
    material = {
        "protocol_version": RULE_SCOPE_PLAN_PROTOCOL_VERSION,
        "task_id": authority.task_id,
        "source_agent_run_id": authority.source_agent_run_id,
        "finalization_epoch": authority.finalization_epoch,
        "rule_id": normalized_rule_id,
        "declared_page_types": list(declared),
        "matched_applicable_page_types": list(matched_applicable),
        "matched_not_applicable_page_types": list(matched_not_applicable),
        "unresolved_page_types": list(unresolved),
        "status": status.value,
        "authority_type": authority.authority_type,
        "authority_id": authority.authority_id,
        "authority_hash": authority.authority_hash,
        "negative_evidence_refs": list(
            authority.negative_evidence_refs if status is RuleScopeStatus.NOT_APPLICABLE else ()
        ),
    }
    return RuleScopePlan(
        task_id=authority.task_id,
        source_agent_run_id=authority.source_agent_run_id,
        finalization_epoch=authority.finalization_epoch,
        rule_id=normalized_rule_id,
        declared_page_types=declared,
        matched_applicable_page_types=matched_applicable,
        matched_not_applicable_page_types=matched_not_applicable,
        unresolved_page_types=unresolved,
        status=status,
        authority_type=authority.authority_type,
        authority_id=authority.authority_id,
        authority_hash=authority.authority_hash,
        negative_evidence_refs=tuple(material["negative_evidence_refs"]),
        plan_hash="sha256:" + stable_protocol_hash(material),
    )


def _normalize_texts(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str | bytes):
        raise RuleScopeValidationError(f"{field_name} 必须为字符串集合")
    normalized = tuple(sorted({_require_text(item, field_name).casefold() for item in values}))
    return normalized


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RuleScopeValidationError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


__all__ = [
    "RULE_SCOPE_PLAN_PROTOCOL_VERSION",
    "RuleScopeAuthority",
    "RuleScopePlan",
    "RuleScopeStatus",
    "RuleScopeValidationError",
    "build_rule_scope_plan",
]
