"""检查点只冻结业务事实和证据义务，不规定站点按钮与点击路线。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.api.app.domain.journey.validation import (
    JourneyValidationError,
    as_tuple,
    freeze_mapping,
    reject_platform_locators,
    require_text,
    thaw_mapping,
    validate_unique_texts,
)

_ARTIFACT_TYPES = frozenset({"screenshot", "dom", "media_state", "network", "action_receipt"})


@dataclass(frozen=True, kw_only=True)
class JourneyEvidenceRequirement:
    """定义独立观测义务，实际去重仍须由证据绑定环节核验。"""

    role: str
    artifact_types: tuple[str, ...]
    minimum_count: int = 1
    capture_phase: str = "after_action"

    def __post_init__(self) -> None:
        require_text(self.role, "evidence.role")
        if self.capture_phase not in ("before_action", "after_action"):
            raise JourneyValidationError("capture_phase 不受支持")
        kinds = as_tuple(self.artifact_types, "artifact_types")
        validate_unique_texts(kinds, "artifact_types")
        if not kinds or not set(kinds) <= _ARTIFACT_TYPES:
            raise JourneyValidationError("artifact_types 为空或不受支持")
        if (
            isinstance(self.minimum_count, bool)
            or not isinstance(self.minimum_count, int)
            or self.minimum_count <= 0
        ):
            raise JourneyValidationError("minimum_count 必须为正整数")
        object.__setattr__(self, "artifact_types", kinds)

    def to_dict(self) -> dict[str, Any]:
        payload = {"role": self.role, "artifact_types": list(self.artifact_types)}
        if self.minimum_count != 1:
            payload["minimum_count"] = self.minimum_count
        if self.capture_phase != "after_action":
            payload["capture_phase"] = self.capture_phase
        return payload


@dataclass(frozen=True, kw_only=True)
class JourneyCheckpoint:
    """恢复策略不等于执行许可；审批和只读契约仍由运行时独立核验。"""

    checkpoint_id: str
    entry_condition: Mapping[str, Any]
    expected: Mapping[str, Any]
    evidence_requirements: tuple[JourneyEvidenceRequirement, ...]
    depends_on: tuple[str, ...]
    exit_condition: Mapping[str, Any]
    recovery_policy: str
    side_effect_policy: str

    def __post_init__(self) -> None:
        require_text(self.checkpoint_id, "checkpoint.id")
        for name in ("entry_condition", "expected", "exit_condition"):
            frozen = freeze_mapping(getattr(self, name), name)
            reject_platform_locators(frozen, name)
            if name != "entry_condition" and not frozen:
                raise JourneyValidationError(f"{name} 不能为空")
            object.__setattr__(self, name, frozen)
        dependencies = as_tuple(self.depends_on, "depends_on")
        validate_unique_texts(dependencies, "depends_on")
        obligations = as_tuple(self.evidence_requirements, "evidence_requirements")
        if not obligations or any(
            not isinstance(item, JourneyEvidenceRequirement) for item in obligations
        ):
            raise JourneyValidationError("evidence_requirements 必须包含证据义务")
        validate_unique_texts(tuple(item.role for item in obligations), "evidence.role")
        if self.recovery_policy not in ("same_session", "new_instance", "blocked"):
            raise JourneyValidationError("recovery_policy 不受支持")
        if self.side_effect_policy not in ("read_only", "approval_required", "prohibited"):
            raise JourneyValidationError("side_effect_policy 不受支持")
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "evidence_requirements", obligations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.checkpoint_id,
            "entry_condition": thaw_mapping(self.entry_condition),
            "expected": thaw_mapping(self.expected),
            "evidence_requirements": [item.to_dict() for item in self.evidence_requirements],
            "depends_on": list(self.depends_on),
            "exit_condition": thaw_mapping(self.exit_condition),
            "recovery_policy": self.recovery_policy,
            "side_effect_policy": self.side_effect_policy,
        }


def validate_checkpoints(values: Any) -> tuple[JourneyCheckpoint, ...]:
    """依赖必须指向先声明的检查点，拒绝未知引用、自依赖和环。"""
    checkpoints = as_tuple(values, "checkpoints")
    if not checkpoints or any(not isinstance(item, JourneyCheckpoint) for item in checkpoints):
        raise JourneyValidationError("checkpoints 必须包含检查点合同")
    seen: set[str] = set()
    for checkpoint in checkpoints:
        if checkpoint.checkpoint_id in seen:
            raise JourneyValidationError("checkpoints 不能包含重复 id")
        if not set(checkpoint.depends_on) <= seen:
            raise JourneyValidationError("检查点依赖必须引用先声明的检查点")
        seen.add(checkpoint.checkpoint_id)
    return checkpoints
