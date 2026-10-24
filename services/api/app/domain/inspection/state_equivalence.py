"""恢复前后四类状态不变量的指纹化等价证明。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.inspection.evidence_request import (
    EvidenceRequiredEquivalence,
    EvidenceSourceGrade,
)
from services.api.app.domain.inspection.restoration_plan import (
    RestorationValidationError,
    grade_meets_minimum,
    grade_supports,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_mapping,
    require_sequence,
    require_text,
    stable_protocol_hash,
    thaw_json_mapping,
)

STATE_INVARIANT_SNAPSHOT_PROTOCOL_VERSION = "state-invariant-snapshot.v1"
STATE_EQUIVALENCE_PROTOCOL_VERSION = "state-equivalence.v1"


class StateInvariantDimension(StrEnum):
    ENVIRONMENT = "environment"
    BUSINESS = "business"
    SEMANTIC = "semantic"
    STRUCTURE = "structure"


@dataclass(frozen=True, kw_only=True)
class StateInvariantSnapshot:
    """Capture 对应的四类状态不变量，不包含运行时句柄。"""

    task_id: str
    capture_id: str
    session_id: str
    surface_kind: SurfaceKind
    environment: Mapping[str, Any]
    business: Mapping[str, Any]
    semantic: Mapping[str, Any]
    structure: Mapping[str, Any]
    captured_at: datetime
    protocol_version: str = STATE_INVARIANT_SNAPSHOT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "capture_id", "session_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_aware_datetime(self.captured_at, "captured_at")
        for dimension in StateInvariantDimension:
            field_name = dimension.value
            try:
                frozen = freeze_json_mapping(getattr(self, field_name), field_name)
            except ProtocolContractError as exc:
                raise RestorationValidationError(str(exc)) from exc
            object.__setattr__(self, field_name, frozen)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise RestorationValidationError("surface_kind 不受支持")
        if self.protocol_version != STATE_INVARIANT_SNAPSHOT_PROTOCOL_VERSION:
            raise RestorationValidationError("StateInvariantSnapshot 版本不受支持")

    def values_for(self, dimension: StateInvariantDimension) -> Mapping[str, Any]:
        return getattr(self, dimension.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "capture_id": self.capture_id,
            "session_id": self.session_id,
            "surface_kind": self.surface_kind.value,
            "environment": thaw_json_mapping(self.environment),
            "business": thaw_json_mapping(self.business),
            "semantic": thaw_json_mapping(self.semantic),
            "structure": thaw_json_mapping(self.structure),
            "captured_at": self.captured_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StateInvariantSnapshot:
        known = {
            "protocol_version",
            "task_id",
            "capture_id",
            "session_id",
            "surface_kind",
            "environment",
            "business",
            "semantic",
            "structure",
            "captured_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="StateInvariantSnapshot",
            )
            return cls(
                task_id=values["task_id"],
                capture_id=values["capture_id"],
                session_id=values["session_id"],
                surface_kind=SurfaceKind(values["surface_kind"]),
                environment=require_mapping(values["environment"], "environment"),
                business=require_mapping(values["business"], "business"),
                semantic=require_mapping(values["semantic"], "semantic"),
                structure=require_mapping(values["structure"], "structure"),
                captured_at=parse_aware_datetime(values["captured_at"], "captured_at"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise RestorationValidationError(str(exc)) from exc


@dataclass(frozen=True, kw_only=True)
class InvariantComparison:
    """单个不变量的指纹比对，不持久化敏感原值。"""

    dimension: StateInvariantDimension
    key: str
    expected_fingerprint: str
    original_fingerprint: str | None
    restored_fingerprint: str | None
    matched: bool

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, StateInvariantDimension):
            raise RestorationValidationError("dimension 不受支持")
        for field_name in ("key", "expected_fingerprint"):
            _require_text(getattr(self, field_name), field_name)
        for field_name in ("original_fingerprint", "restored_fingerprint"):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if not isinstance(self.matched, bool):
            raise RestorationValidationError("matched 必须为布尔值")
        if self.matched != (
            self.expected_fingerprint == self.original_fingerprint == self.restored_fingerprint
        ):
            raise RestorationValidationError("matched 与不变量指纹不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "key": self.key,
            "expected_fingerprint": self.expected_fingerprint,
            "original_fingerprint": self.original_fingerprint,
            "restored_fingerprint": self.restored_fingerprint,
            "matched": self.matched,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InvariantComparison:
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(
                    {
                        "dimension",
                        "key",
                        "expected_fingerprint",
                        "original_fingerprint",
                        "restored_fingerprint",
                        "matched",
                    }
                ),
                required_fields=frozenset({"dimension", "key", "expected_fingerprint", "matched"}),
                field_name="InvariantComparison",
            )
            return cls(
                dimension=StateInvariantDimension(values["dimension"]),
                key=values["key"],
                expected_fingerprint=values["expected_fingerprint"],
                original_fingerprint=values.get("original_fingerprint"),
                restored_fingerprint=values.get("restored_fingerprint"),
                matched=values["matched"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise RestorationValidationError(str(exc)) from exc


@dataclass(frozen=True, kw_only=True)
class StateEquivalenceAssessment:
    """恢复后的 Capture 是否可作为原规则补充来源的证明。"""

    assessment_id: str
    restoration_attempt_id: str
    request_id: str
    task_id: str
    original_capture_id: str
    restored_capture_id: str
    required_equivalence: EvidenceRequiredEquivalence
    source_grade: EvidenceSourceGrade
    minimum_source_grade: EvidenceSourceGrade
    comparisons: tuple[InvariantComparison, ...]
    passed: bool
    evaluator_version: str
    evaluated_at: datetime
    protocol_version: str = STATE_EQUIVALENCE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "assessment_id",
            "restoration_attempt_id",
            "request_id",
            "task_id",
            "original_capture_id",
            "restored_capture_id",
            "evaluator_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware_datetime(self.evaluated_at, "evaluated_at")
        if not isinstance(self.required_equivalence, EvidenceRequiredEquivalence):
            raise RestorationValidationError("required_equivalence 不受支持")
        if not isinstance(self.source_grade, EvidenceSourceGrade):
            raise RestorationValidationError("source_grade 不受支持")
        if not isinstance(self.minimum_source_grade, EvidenceSourceGrade):
            raise RestorationValidationError("minimum_source_grade 不受支持")
        comparisons = tuple(self.comparisons)
        if not comparisons or any(
            not isinstance(item, InvariantComparison) for item in comparisons
        ):
            raise RestorationValidationError("comparisons 必须是非空不变量比对")
        keys = tuple((item.dimension, item.key) for item in comparisons)
        if len(keys) != len(set(keys)):
            raise RestorationValidationError("comparisons 不能包含重复不变量")
        expected_pass = (
            all(item.matched for item in comparisons)
            and grade_supports(
                self.source_grade,
                self.required_equivalence,
            )
            and grade_meets_minimum(self.source_grade, self.minimum_source_grade)
        )
        if self.passed != expected_pass:
            raise RestorationValidationError("passed 与比对结果或来源等级不一致")
        if self.source_grade is EvidenceSourceGrade.D and self.passed:
            raise RestorationValidationError("D 级来源禁止自动通过")
        if self.protocol_version != STATE_EQUIVALENCE_PROTOCOL_VERSION:
            raise RestorationValidationError("StateEquivalenceAssessment 版本不受支持")
        expected_id = build_state_equivalence_assessment_id(
            restoration_attempt_id=self.restoration_attempt_id,
            request_id=self.request_id,
            task_id=self.task_id,
            original_capture_id=self.original_capture_id,
            restored_capture_id=self.restored_capture_id,
            required_equivalence=self.required_equivalence,
            source_grade=self.source_grade,
            minimum_source_grade=self.minimum_source_grade,
            comparisons=comparisons,
            evaluator_version=self.evaluator_version,
        )
        if self.assessment_id != expected_id:
            raise RestorationValidationError("assessment_id 与等价证明内容不一致")
        object.__setattr__(self, "comparisons", comparisons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "assessment_id": self.assessment_id,
            "restoration_attempt_id": self.restoration_attempt_id,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "original_capture_id": self.original_capture_id,
            "restored_capture_id": self.restored_capture_id,
            "required_equivalence": self.required_equivalence.value,
            "source_grade": self.source_grade.value,
            "minimum_source_grade": self.minimum_source_grade.value,
            "comparisons": [item.to_dict() for item in self.comparisons],
            "passed": self.passed,
            "evaluator_version": self.evaluator_version,
            "evaluated_at": self.evaluated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StateEquivalenceAssessment:
        known = {
            "protocol_version",
            "assessment_id",
            "restoration_attempt_id",
            "request_id",
            "task_id",
            "original_capture_id",
            "restored_capture_id",
            "required_equivalence",
            "source_grade",
            "minimum_source_grade",
            "comparisons",
            "passed",
            "evaluator_version",
            "evaluated_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="StateEquivalenceAssessment",
            )
            comparisons = require_sequence(values["comparisons"], "comparisons")
            return cls(
                assessment_id=values["assessment_id"],
                restoration_attempt_id=values["restoration_attempt_id"],
                request_id=values["request_id"],
                task_id=values["task_id"],
                original_capture_id=values["original_capture_id"],
                restored_capture_id=values["restored_capture_id"],
                required_equivalence=EvidenceRequiredEquivalence(values["required_equivalence"]),
                source_grade=EvidenceSourceGrade(values["source_grade"]),
                minimum_source_grade=EvidenceSourceGrade(values["minimum_source_grade"]),
                comparisons=tuple(
                    InvariantComparison.from_dict(require_mapping(item, "comparisons[]"))
                    for item in comparisons
                ),
                passed=values["passed"],
                evaluator_version=values["evaluator_version"],
                evaluated_at=parse_aware_datetime(
                    values["evaluated_at"],
                    "evaluated_at",
                ),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise RestorationValidationError(str(exc)) from exc


def build_state_equivalence_assessment_id(
    *,
    restoration_attempt_id: str,
    request_id: str,
    task_id: str,
    original_capture_id: str,
    restored_capture_id: str,
    required_equivalence: EvidenceRequiredEquivalence,
    source_grade: EvidenceSourceGrade,
    minimum_source_grade: EvidenceSourceGrade,
    comparisons: Sequence[InvariantComparison],
    evaluator_version: str,
) -> str:
    if not isinstance(required_equivalence, EvidenceRequiredEquivalence):
        raise RestorationValidationError("required_equivalence 不受支持")
    for field_name, value in (
        ("source_grade", source_grade),
        ("minimum_source_grade", minimum_source_grade),
    ):
        if not isinstance(value, EvidenceSourceGrade):
            raise RestorationValidationError(f"{field_name} 不受支持")
    return "state-equivalence:" + stable_protocol_hash(
        {
            "restoration_attempt_id": _require_text(
                restoration_attempt_id,
                "restoration_attempt_id",
            ),
            "request_id": _require_text(request_id, "request_id"),
            "task_id": _require_text(task_id, "task_id"),
            "original_capture_id": _require_text(
                original_capture_id,
                "original_capture_id",
            ),
            "restored_capture_id": _require_text(
                restored_capture_id,
                "restored_capture_id",
            ),
            "required_equivalence": required_equivalence.value,
            "source_grade": source_grade.value,
            "minimum_source_grade": minimum_source_grade.value,
            "comparisons": [item.to_dict() for item in comparisons],
            "evaluator_version": _require_text(evaluator_version, "evaluator_version"),
        }
    )


def fingerprint_invariant(value: Any, *, scope: str) -> str:
    """用请求级作用域把敏感不变量值转换为不可跨任务关联的指纹。"""
    return stable_protocol_hash(
        {
            "scope": _require_text(scope, "scope"),
            "value": value,
        }
    )


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise RestorationValidationError(str(exc)) from exc


def _require_aware_datetime(value: Any, field_name: str) -> None:
    try:
        require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise RestorationValidationError(str(exc)) from exc
