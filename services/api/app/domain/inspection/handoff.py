"""Explorer/Evidence 到 Rule Judge 的无运行时句柄中转协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

INSPECTION_HANDOFF_PROTOCOL_VERSION = "inspection-handoff.v2"
PAGE_CAPTURE_EVIDENCE_VERSION = "page-capture.v1"

_SUPPORTED_STRATEGIES = frozenset({"rule_engine", "llm", "hybrid"})
_SUPPORTED_SESSION_DEPENDENCIES = frozenset({"snapshot", "active_session"})


class InspectionHandoffValidationError(ValueError):
    """中转对象缺少稳定身份或携带了不一致的证据引用。"""


@dataclass(frozen=True, kw_only=True)
class EvidenceBundleReference:
    """Rule Judge 可读取的不可变证据引用，不包含证据正文或存储客户端。"""

    page_capture_id: str
    producer_agent_run_id: str
    evidence_version: str = PAGE_CAPTURE_EVIDENCE_VERSION
    manifest_id: str | None = None
    manifest_version: str | None = None
    observation_packet_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    evidence_capture_roles: Mapping[str, str] | None = None
    evidence_manifest_refs: Mapping[str, Mapping[str, str]] | None = None
    retained_baseline_artifact_ids: Mapping[str, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        _require_text(self.page_capture_id, "page_capture_id")
        _require_text(self.producer_agent_run_id, "producer_agent_run_id")
        _require_text(self.evidence_version, "evidence_version")
        if (self.manifest_id is None) != (self.manifest_version is None):
            raise InspectionHandoffValidationError(
                "manifest_id 与 manifest_version 必须同时提供"
            )
        if self.manifest_id is not None:
            _require_text(self.manifest_id, "manifest_id")
            _require_text(self.manifest_version, "manifest_version")
        _validate_unique_texts(self.observation_packet_ids, "observation_packet_ids")
        _validate_unique_texts(self.artifact_ids, "artifact_ids")
        manifest_refs = dict(self.evidence_manifest_refs or {})
        if self.evidence_manifest_refs is not None and not manifest_refs:
            raise InspectionHandoffValidationError("evidence_manifest_refs 不能为空")
        for role, reference in manifest_refs.items():
            _require_text(role, "evidence_manifest_refs.role")
            if not isinstance(reference, Mapping):
                raise InspectionHandoffValidationError(
                    "evidence_manifest_refs 必须包含对象"
                )
            manifest_id = reference.get("manifest_id")
            manifest_version = reference.get("manifest_version")
            _require_text(manifest_id, "evidence_manifest_refs.manifest_id")
            _require_text(manifest_version, "evidence_manifest_refs.manifest_version")
            manifest_refs[role] = {
                "manifest_id": manifest_id,
                "manifest_version": manifest_version,
            }
        retained_baseline_artifact_ids = dict(
            self.retained_baseline_artifact_ids or {}
        )
        if self.retained_baseline_artifact_ids is not None and not retained_baseline_artifact_ids:
            raise InspectionHandoffValidationError(
                "retained_baseline_artifact_ids 不能为空"
            )
        for role, artifact_ids in retained_baseline_artifact_ids.items():
            _require_text(role, "retained_baseline_artifact_ids.role")
            if not isinstance(artifact_ids, (tuple, list)):
                raise InspectionHandoffValidationError(
                    "retained_baseline_artifact_ids 必须包含 ID 列表"
                )
            normalized_artifact_ids = tuple(artifact_ids)
            _validate_unique_texts(
                normalized_artifact_ids,
                "retained_baseline_artifact_ids.artifact_ids",
            )
            retained_baseline_artifact_ids[role] = normalized_artifact_ids
        if retained_baseline_artifact_ids and self.evidence_capture_roles is None:
            raise InspectionHandoffValidationError(
                "retained_baseline_artifact_ids 必须同时声明证据角色"
            )
        if self.evidence_capture_roles is not None:
            roles = dict(self.evidence_capture_roles)
            if not roles:
                raise InspectionHandoffValidationError("evidence_capture_roles 不能为空")
            if roles.get("primary") != self.page_capture_id:
                raise InspectionHandoffValidationError(
                    "evidence_capture_roles.primary 必须指向 page_capture_id"
                )
            for role, capture_id in roles.items():
                _require_text(role, "evidence_capture_roles.role")
                _require_text(capture_id, "evidence_capture_roles.capture_id")
            if len(set(roles.values())) != len(roles):
                raise InspectionHandoffValidationError(
                    "evidence_capture_roles 不得指向同一页面采集"
                )
            object.__setattr__(self, "evidence_capture_roles", roles)
            if any(role not in roles for role in manifest_refs):
                raise InspectionHandoffValidationError(
                    "evidence_manifest_refs 必须绑定已声明的证据角色"
                )
            if any(role not in roles for role in retained_baseline_artifact_ids):
                raise InspectionHandoffValidationError(
                    "retained_baseline_artifact_ids 必须绑定已声明的证据角色"
                )
        if manifest_refs:
            primary_ref = manifest_refs.get("primary")
            if self.manifest_id is not None:
                if primary_ref is not None and (
                    primary_ref["manifest_id"] != self.manifest_id
                    or primary_ref["manifest_version"] != self.manifest_version
                ):
                    raise InspectionHandoffValidationError(
                        "primary Manifest 与兼容字段不一致"
                    )
            elif primary_ref is not None:
                object.__setattr__(self, "manifest_id", primary_ref["manifest_id"])
                object.__setattr__(self, "manifest_version", primary_ref["manifest_version"])
            object.__setattr__(self, "evidence_manifest_refs", manifest_refs)
        if retained_baseline_artifact_ids:
            object.__setattr__(
                self,
                "retained_baseline_artifact_ids",
                retained_baseline_artifact_ids,
            )

    def to_dict(self) -> dict[str, Any]:
        """返回可持久化的纯数据结构。"""
        payload = asdict(self)
        payload["observation_packet_ids"] = list(self.observation_packet_ids)
        payload["artifact_ids"] = list(self.artifact_ids)
        if self.evidence_capture_roles is None:
            payload.pop("evidence_capture_roles", None)
        else:
            payload["evidence_capture_roles"] = dict(self.evidence_capture_roles)
        if self.evidence_manifest_refs is None:
            payload.pop("evidence_manifest_refs", None)
        else:
            payload["evidence_manifest_refs"] = {
                role: dict(reference)
                for role, reference in self.evidence_manifest_refs.items()
            }
        if self.retained_baseline_artifact_ids is None:
            payload.pop("retained_baseline_artifact_ids", None)
        else:
            payload["retained_baseline_artifact_ids"] = {
                role: list(artifact_ids)
                for role, artifact_ids in self.retained_baseline_artifact_ids.items()
            }
        return payload


@dataclass(frozen=True, kw_only=True)
class InspectionRuleTarget:
    """工作项中一条被锁定的规则目标。"""

    rule_id: str
    rule_code: str
    ordinal: int
    contract_hash: str
    evidence_input_set_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rule_id")
        _require_text(self.rule_code, "rule_code")
        _require_text(self.contract_hash, "contract_hash")
        if self.ordinal < 0:
            raise InspectionHandoffValidationError("ordinal 不能小于 0")


@dataclass(frozen=True, kw_only=True)
class InspectionHandoff:
    """Inspection Work Item 向 Judge 暴露的最小只读输入。"""

    work_item_id: str
    attempt_id: str
    task_id: str
    agent_run_id: str
    input_hash: str
    batch_key: str
    strategy: str
    session_dependency: str
    evidence: EvidenceBundleReference
    rules: tuple[InspectionRuleTarget, ...]
    rule_evidence: Mapping[str, EvidenceBundleReference] = field(default_factory=dict)
    category: str | None = None
    lease_token: str | None = field(default=None, repr=False, compare=False)
    original_page_input_sets: Mapping[str, str] = field(
        default_factory=dict, repr=False, compare=False,
    )
    protocol_version: str = INSPECTION_HANDOFF_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "work_item_id",
            "attempt_id",
            "task_id",
            "agent_run_id",
            "input_hash",
            "batch_key",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.protocol_version != INSPECTION_HANDOFF_PROTOCOL_VERSION:
            raise InspectionHandoffValidationError(
                f"protocol_version 必须为 {INSPECTION_HANDOFF_PROTOCOL_VERSION}"
            )
        if self.strategy not in _SUPPORTED_STRATEGIES:
            raise InspectionHandoffValidationError("strategy 不受支持")
        if self.session_dependency not in _SUPPORTED_SESSION_DEPENDENCIES:
            raise InspectionHandoffValidationError("session_dependency 不受支持")
        if self.category is not None:
            _require_text(self.category, "category")
        if not self.rules:
            raise InspectionHandoffValidationError("rules 不能为空")

        ordinals = tuple(rule.ordinal for rule in self.rules)
        if ordinals != tuple(sorted(ordinals)) or len(set(ordinals)) != len(ordinals):
            raise InspectionHandoffValidationError("rules 必须按唯一 ordinal 升序排列")
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        rule_codes = tuple(rule.rule_code for rule in self.rules)
        if set(self.original_page_input_sets) - set(rule_codes):
            raise InspectionHandoffValidationError("页面原输入包含工作项之外的规则")
        for input_set_id in self.original_page_input_sets.values():
            _require_text(input_set_id, "original_page_input_sets.input_set_id")
        if len(set(rule_ids)) != len(rule_ids) or len(set(rule_codes)) != len(rule_codes):
            raise InspectionHandoffValidationError("rules 不能包含重复规则")
        bindings = dict(self.rule_evidence)
        unknown_rule_codes = sorted(set(bindings) - set(rule_codes))
        if unknown_rule_codes:
            raise InspectionHandoffValidationError(
                "rule_evidence 包含工作项之外的规则：" + ", ".join(unknown_rule_codes)
            )
        if any(
            not isinstance(reference, EvidenceBundleReference)
            for reference in bindings.values()
        ):
            raise InspectionHandoffValidationError(
                "rule_evidence 必须引用 EvidenceBundleReference"
            )
        object.__setattr__(self, "rule_evidence", bindings)

    @property
    def idempotency_key(self) -> str:
        """沿用工作项输入哈希，避免中转层产生第二套幂等身份。"""
        return self.input_hash

    def evidence_for_rule(self, rule_code: str) -> EvidenceBundleReference:
        """返回规则精确绑定的证据；未补采规则沿用工作项基线证据。"""
        supplemental = self.rule_evidence.get(rule_code)
        if supplemental is None:
            return self.evidence
        return _merge_evidence_references(self.evidence, supplemental)

    def to_dict(self) -> dict[str, Any]:
        """返回不含租约、Driver 或基础设施句柄的协议载荷。"""
        return {
            "protocol_version": self.protocol_version,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "input_hash": self.input_hash,
            "idempotency_key": self.idempotency_key,
            "batch_key": self.batch_key,
            "strategy": self.strategy,
            "category": self.category,
            "session_dependency": self.session_dependency,
            "evidence": self.evidence.to_dict(),
            "rule_evidence": {
                rule_code: reference.to_dict()
                for rule_code, reference in sorted(self.rule_evidence.items())
            },
            "rules": [
                {key: value for key, value in asdict(rule).items()
                 if key != "evidence_input_set_id" or value is not None}
                for rule in self.rules
            ],
        }


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InspectionHandoffValidationError(f"{field_name} 不能为空")
    text = value.strip()
    if text != value:
        raise InspectionHandoffValidationError(f"{field_name} 不能包含首尾空白")
    return text


def _validate_unique_texts(values: tuple[str, ...], field_name: str) -> None:
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise InspectionHandoffValidationError(f"{field_name} 不能包含重复值")


def _merge_evidence_references(
    baseline: EvidenceBundleReference,
    supplemental: EvidenceBundleReference,
) -> EvidenceBundleReference:
    """补采替换 primary，同时保留同一规则需要的 before 等原角色。"""
    baseline_roles = dict(baseline.evidence_capture_roles or {"primary": baseline.page_capture_id})
    supplemental_roles = dict(
        supplemental.evidence_capture_roles or {"primary": supplemental.page_capture_id}
    )
    merged_roles = {**baseline_roles, **supplemental_roles}
    explicit_manifest_refs = {
        **dict(baseline.evidence_manifest_refs or {}),
        **dict(supplemental.evidence_manifest_refs or {}),
    }
    merged_manifest_refs = explicit_manifest_refs
    if len(merged_roles) > 1:
        merged_manifest_refs = {
            **_manifest_refs_for_reference(baseline),
            **_manifest_refs_for_reference(supplemental),
        }
    primary_replaced = baseline_roles.get("primary") != supplemental_roles.get("primary")
    if primary_replaced:
        # 基线 ID 没有按角色拆分；仅保留给未被补采替换的基线角色，
        # 读取器再与该角色清单求交，旧 primary ID 不会被重新读取。
        merged_artifact_ids = supplemental.artifact_ids
        merged_packet_ids = supplemental.observation_packet_ids
        retained_baseline_artifact_ids = {
            role: baseline.artifact_ids
            for role in baseline_roles
            if role != "primary"
            and role not in supplemental_roles
            and baseline.artifact_ids
        }
    else:
        merged_artifact_ids = _merge_unique_texts(
            baseline.artifact_ids,
            supplemental.artifact_ids,
        )
        merged_packet_ids = _merge_unique_texts(
            baseline.observation_packet_ids,
            supplemental.observation_packet_ids,
        )
        retained_baseline_artifact_ids = {}
    return EvidenceBundleReference(
        page_capture_id=supplemental.page_capture_id,
        producer_agent_run_id=supplemental.producer_agent_run_id,
        evidence_version=supplemental.evidence_version,
        manifest_id=supplemental.manifest_id,
        manifest_version=supplemental.manifest_version,
        observation_packet_ids=merged_packet_ids,
        artifact_ids=merged_artifact_ids,
        evidence_capture_roles=merged_roles,
        evidence_manifest_refs=merged_manifest_refs or None,
        retained_baseline_artifact_ids=retained_baseline_artifact_ids or None,
    )


def _manifest_refs_for_reference(
    reference: EvidenceBundleReference,
) -> dict[str, dict[str, str]]:
    """把旧的 primary 清单字段投影为角色映射，供补采合并使用。"""
    refs = {
        role: dict(manifest)
        for role, manifest in (reference.evidence_manifest_refs or {}).items()
    }
    if reference.manifest_id is not None:
        refs.setdefault(
            "primary",
            {
                "manifest_id": reference.manifest_id,
                "manifest_version": reference.manifest_version or "",
            },
        )
    return refs


def _merge_unique_texts(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """合并基线与补采引用并保持首次出现顺序。"""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if value not in seen:
                merged.append(value)
                seen.add(value)
    return tuple(merged)
