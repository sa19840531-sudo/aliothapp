"""App 页面探索目标与覆盖快照合同。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.domain.exploration.app_target_profile import (
    APP_TARGET_APPLICABILITY_DYNAMIC,
    APP_TARGET_APPLICABILITY_EXPLICIT,
    APP_TARGET_APPLICABILITY_PROFILE,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import (
    ProtocolContractError,
    read_known_fields,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V1 = "app-exploration-goal.v1"
APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V2 = "app-exploration-goal.v2"
APP_EXPLORATION_GOAL_PROTOCOL_VERSION = APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V1
APP_EXPLORATION_COVERAGE_PROTOCOL_VERSION = "app-exploration-coverage.v1"
_SUPPORTED_GOAL_PROTOCOL_VERSIONS = frozenset(
    {
        APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V1,
        APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V2,
    }
)
_SUPPORTED_APPLICABILITY_MODES = frozenset(
    {
        APP_TARGET_APPLICABILITY_DYNAMIC,
        APP_TARGET_APPLICABILITY_EXPLICIT,
        APP_TARGET_APPLICABILITY_PROFILE,
    }
)


class AppExplorationGoalValidationError(ValueError):
    """App 探索目标或覆盖快照不满足稳定合同。"""


def build_app_exploration_goal(
    *,
    package_name: str,
    journey_steps: Sequence[str],
    fallback_milestones: Sequence[str],
    minimum_business_pages: int = 2,
    required_page_types: Sequence[str] = (),
    optional_page_types: Sequence[str] = (),
    lifecycle_page_types: Sequence[str] = (),
    required_milestones: Sequence[str] = (),
    applicability_mode: str | None = None,
) -> AppExplorationGoal:
    """从锁定任务输入生成 APP_READY 后不可扩大的默认探索目标。"""
    package = _required_text(package_name, "package_name")
    journeys = _unique_texts(journey_steps, "journey_steps")
    fallbacks = _unique_texts(fallback_milestones, "fallback_milestones")
    explicit_page_types = _unique_texts(required_page_types, "required_page_types")
    optional = _unique_texts(optional_page_types, "optional_page_types")
    lifecycle = _unique_texts(lifecycle_page_types, "lifecycle_page_types")
    registry = get_default_page_type_registry()
    legacy_stable_goal = explicit_page_types == ("stable_business_page",)
    invalid_page_types = sorted(set(explicit_page_types) - set(registry.coverage_required_codes))
    if invalid_page_types and not legacy_stable_goal:
        raise AppExplorationGoalValidationError(
            "required_page_types 不属于业务页面类型目录：" + ", ".join(invalid_page_types)
        )
    invalid_optional_page_types = sorted(
        set(optional) - set(registry.coverage_required_codes)
    )
    if invalid_optional_page_types:
        raise AppExplorationGoalValidationError(
            "optional_page_types 不属于业务页面类型目录："
            + ", ".join(invalid_optional_page_types)
        )
    invalid_lifecycle_page_types = sorted(set(lifecycle) - set(registry.lifecycle_codes))
    if invalid_lifecycle_page_types:
        raise AppExplorationGoalValidationError(
            "lifecycle_page_types 不属于生命周期页面类型目录："
            + ", ".join(invalid_lifecycle_page_types)
        )
    if set(explicit_page_types) & set(optional):
        raise AppExplorationGoalValidationError(
            "required_page_types 与 optional_page_types 不能交叉"
        )
    page_types = explicit_page_types or registry.coverage_required_codes
    explicit_milestones = _unique_texts(required_milestones, "required_milestones")
    milestones = explicit_milestones or (
        tuple(f"journey:{item}" for item in journeys)
        if journeys
        else tuple(f"surface:{item}" for item in fallbacks)
    )
    if not milestones:
        milestones = tuple(f"surface:{item}" for item in page_types)
    resolved_applicability_mode = applicability_mode or (
        APP_TARGET_APPLICABILITY_EXPLICIT
        if explicit_page_types
        else APP_TARGET_APPLICABILITY_DYNAMIC
    )
    return AppExplorationGoal(
        required_page_types=page_types,
        required_milestones=milestones,
        minimum_business_pages=minimum_business_pages,
        allowed_packages=(package,),
        optional_page_types=optional,
        lifecycle_page_types=lifecycle,
        applicability_mode=resolved_applicability_mode,
    )


@dataclass(frozen=True, kw_only=True)
class AppExplorationGoal:
    """APP_READY 后锁定且探索期间不可扩大的覆盖目标。"""

    required_page_types: tuple[str, ...]
    required_milestones: tuple[str, ...]
    minimum_business_pages: int
    allowed_packages: tuple[str, ...]
    protocol_version: str = APP_EXPLORATION_GOAL_PROTOCOL_VERSION
    optional_page_types: tuple[str, ...] = ()
    lifecycle_page_types: tuple[str, ...] = ()
    applicability_mode: str | None = None

    def __post_init__(self) -> None:
        page_types = _unique_texts(self.required_page_types, "required_page_types")
        milestones = _unique_texts(self.required_milestones, "required_milestones")
        packages = _unique_texts(self.allowed_packages, "allowed_packages")
        optional_page_types = _unique_texts(
            self.optional_page_types,
            "optional_page_types",
        )
        lifecycle_page_types = _unique_texts(
            self.lifecycle_page_types,
            "lifecycle_page_types",
        )
        for field_name, values in (
            ("required_page_types", page_types),
            ("required_milestones", milestones),
            ("allowed_packages", packages),
        ):
            if not values:
                raise AppExplorationGoalValidationError(f"{field_name} 不能为空")
        _require_non_negative_int(self.minimum_business_pages, "minimum_business_pages")
        if self.minimum_business_pages == 0:
            raise AppExplorationGoalValidationError("minimum_business_pages 必须大于 0")
        registry = get_default_page_type_registry()
        invalid_required = sorted(set(page_types) - set(registry.coverage_required_codes))
        # v1 曾把生命周期页直接写入 required_page_types；读取历史 Work 时继续
        # 接受该口径。v2 则强制拆到 lifecycle_page_types，避免污染业务覆盖分母。
        legacy_required_codes = {"stable_business_page", *registry.lifecycle_codes}
        if (
            invalid_required
            and self.protocol_version == APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V1
            and not set(invalid_required).issubset(legacy_required_codes)
        ) or (
            invalid_required
            and self.protocol_version != APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V1
        ):
            raise AppExplorationGoalValidationError(
                "required_page_types 不属于业务页面类型目录："
                + ", ".join(invalid_required)
            )
        invalid_optional = sorted(
            set(optional_page_types) - set(registry.coverage_required_codes)
        )
        if invalid_optional:
            raise AppExplorationGoalValidationError(
                "optional_page_types 不属于业务页面类型目录："
                + ", ".join(invalid_optional)
            )
        invalid_lifecycle = sorted(set(lifecycle_page_types) - set(registry.lifecycle_codes))
        if invalid_lifecycle:
            raise AppExplorationGoalValidationError(
                "lifecycle_page_types 不属于生命周期页面类型目录："
                + ", ".join(invalid_lifecycle)
            )
        if set(page_types) & set(optional_page_types):
            raise AppExplorationGoalValidationError(
                "required_page_types 与 optional_page_types 不能交叉"
            )
        has_extended_fields = bool(
            optional_page_types
            or lifecycle_page_types
            or self.applicability_mode is not None
        )
        if self.applicability_mode is None:
            applicability_mode = (
                APP_TARGET_APPLICABILITY_EXPLICIT
                if page_types
                else APP_TARGET_APPLICABILITY_DYNAMIC
            )
        else:
            applicability_mode = self.applicability_mode
        if applicability_mode not in _SUPPORTED_APPLICABILITY_MODES:
            raise AppExplorationGoalValidationError("applicability_mode 不受支持")
        protocol_version = self.protocol_version
        if protocol_version == APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V1 and has_extended_fields:
            protocol_version = APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V2
        if protocol_version not in _SUPPORTED_GOAL_PROTOCOL_VERSIONS:
            raise AppExplorationGoalValidationError("AppExplorationGoal protocol_version 不受支持")
        object.__setattr__(self, "required_page_types", page_types)
        object.__setattr__(self, "required_milestones", milestones)
        object.__setattr__(self, "allowed_packages", packages)
        object.__setattr__(self, "optional_page_types", optional_page_types)
        object.__setattr__(self, "lifecycle_page_types", lifecycle_page_types)
        object.__setattr__(self, "applicability_mode", applicability_mode)
        object.__setattr__(self, "protocol_version", protocol_version)

    @property
    def goal_hash(self) -> str:
        return "sha256:" + stable_protocol_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "required_page_types": list(self.required_page_types),
            "required_milestones": list(self.required_milestones),
            "minimum_business_pages": self.minimum_business_pages,
            "allowed_packages": list(self.allowed_packages),
        }
        if self.protocol_version == APP_EXPLORATION_GOAL_PROTOCOL_VERSION_V2:
            result.update(
                {
                    "optional_page_types": list(self.optional_page_types),
                    "lifecycle_page_types": list(self.lifecycle_page_types),
                    "applicability_mode": self.applicability_mode,
                    "applicability": {
                        "mode": self.applicability_mode,
                        "required_page_types": list(self.required_page_types),
                        "optional_page_types": list(self.optional_page_types),
                        "lifecycle_page_types": list(self.lifecycle_page_types),
                    },
                }
            )
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppExplorationGoal:
        values = _read(
            payload,
            known={
                "protocol_version",
                "required_page_types",
                "required_milestones",
                "minimum_business_pages",
                "allowed_packages",
                "optional_page_types",
                "lifecycle_page_types",
                "applicability_mode",
                "applicability",
            },
            required={
                "protocol_version",
                "required_page_types",
                "required_milestones",
                "minimum_business_pages",
                "allowed_packages",
            },
            field_name="AppExplorationGoal",
        )
        protocol_version = values["protocol_version"]
        optional_page_types = _sequence(
            values.get("optional_page_types", ()),
            "optional_page_types",
        )
        lifecycle_page_types = _sequence(
            values.get("lifecycle_page_types", ()),
            "lifecycle_page_types",
        )
        applicability_mode = values.get("applicability_mode")
        applicability = values.get("applicability")
        if applicability is not None:
            try:
                if not isinstance(applicability, Mapping):
                    raise AppExplorationGoalValidationError("applicability 必须为对象")
                declared_mode = applicability.get("mode")
                if (
                    declared_mode is not None
                    and applicability_mode is not None
                    and declared_mode != applicability_mode
                ):
                    raise AppExplorationGoalValidationError(
                        "applicability.mode 与 applicability_mode 不一致"
                    )
                if applicability_mode is None and declared_mode is not None:
                    applicability_mode = str(declared_mode)
                for field_name, current in (
                    ("required_page_types", values["required_page_types"]),
                    ("optional_page_types", optional_page_types),
                    ("lifecycle_page_types", lifecycle_page_types),
                ):
                    declared_values = applicability.get(field_name)
                    if declared_values is not None and tuple(
                        _sequence(declared_values, f"applicability.{field_name}")
                    ) != tuple(_sequence(current, field_name)):
                        raise AppExplorationGoalValidationError(
                            f"applicability.{field_name} 与顶层字段不一致"
                        )
            except AppExplorationGoalValidationError:
                raise
        return cls(
            required_page_types=_sequence(values["required_page_types"], "required_page_types"),
            required_milestones=_sequence(values["required_milestones"], "required_milestones"),
            minimum_business_pages=values["minimum_business_pages"],
            allowed_packages=_sequence(values["allowed_packages"], "allowed_packages"),
            protocol_version=protocol_version,
            optional_page_types=optional_page_types,
            lifecycle_page_types=lifecycle_page_types,
            applicability_mode=(
                str(applicability_mode) if applicability_mode is not None else None
            ),
        )


@dataclass(frozen=True, kw_only=True)
class AppExplorationCoverageSnapshot:
    """终态判断和审计共用的确定性覆盖事实。"""

    required_page_types: tuple[str, ...]
    covered_page_types: tuple[str, ...]
    missing_page_types: tuple[str, ...]
    blocked_page_types: tuple[str, ...]
    required_milestones: tuple[str, ...]
    covered_milestones: tuple[str, ...]
    missing_milestones: tuple[str, ...]
    blocked_milestones: tuple[str, ...]
    minimum_business_pages: int
    covered_business_page_count: int
    eligible_candidate_count: int
    pending_action_count: int
    pending_evidence_count: int
    frontier_exhausted: bool
    stable_page_reached: bool = False
    baseline_evidence_complete: bool = False
    protocol_version: str = APP_EXPLORATION_COVERAGE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        required_pages = _unique_texts(self.required_page_types, "required_page_types")
        covered_pages = _unique_texts(self.covered_page_types, "covered_page_types")
        missing_pages = _unique_texts(self.missing_page_types, "missing_page_types")
        blocked_pages = _unique_texts(self.blocked_page_types, "blocked_page_types")
        required_milestones = _unique_texts(
            self.required_milestones,
            "required_milestones",
        )
        covered_milestones = _unique_texts(
            self.covered_milestones,
            "covered_milestones",
        )
        missing_milestones = _unique_texts(
            self.missing_milestones,
            "missing_milestones",
        )
        blocked_milestones = _unique_texts(
            self.blocked_milestones,
            "blocked_milestones",
        )
        _validate_partition(
            required=required_pages,
            covered=covered_pages,
            missing=missing_pages,
            blocked=blocked_pages,
            field_name="page_types",
        )
        _validate_partition(
            required=required_milestones,
            covered=covered_milestones,
            missing=missing_milestones,
            blocked=blocked_milestones,
            field_name="milestones",
        )
        for field_name in (
            "minimum_business_pages",
            "covered_business_page_count",
            "eligible_candidate_count",
            "pending_action_count",
            "pending_evidence_count",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name)
        for field_name in (
            "frontier_exhausted",
            "stable_page_reached",
            "baseline_evidence_complete",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise AppExplorationGoalValidationError(f"{field_name} 必须为布尔值")
        if self.protocol_version != APP_EXPLORATION_COVERAGE_PROTOCOL_VERSION:
            raise AppExplorationGoalValidationError(
                "AppExplorationCoverageSnapshot protocol_version 不受支持"
            )
        object.__setattr__(self, "required_page_types", required_pages)
        object.__setattr__(self, "covered_page_types", covered_pages)
        object.__setattr__(self, "missing_page_types", missing_pages)
        object.__setattr__(self, "blocked_page_types", blocked_pages)
        object.__setattr__(self, "required_milestones", required_milestones)
        object.__setattr__(self, "covered_milestones", covered_milestones)
        object.__setattr__(self, "missing_milestones", missing_milestones)
        object.__setattr__(self, "blocked_milestones", blocked_milestones)

    @property
    def coverage_complete(self) -> bool:
        return (
            self.stable_page_reached
            and self.baseline_evidence_complete
            and not self.missing_page_types
            and not self.blocked_page_types
            and not self.missing_milestones
            and not self.blocked_milestones
            and self.covered_business_page_count >= self.minimum_business_pages
            and self.pending_action_count == 0
            and self.pending_evidence_count == 0
        )

    @property
    def content_sha256(self) -> str:
        return "sha256:" + stable_protocol_hash(self.to_dict())

    def recompute_content_sha256(self) -> str:
        return self.content_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "required_page_types": list(self.required_page_types),
            "covered_page_types": list(self.covered_page_types),
            "missing_page_types": list(self.missing_page_types),
            "blocked_page_types": list(self.blocked_page_types),
            "required_milestones": list(self.required_milestones),
            "covered_milestones": list(self.covered_milestones),
            "missing_milestones": list(self.missing_milestones),
            "blocked_milestones": list(self.blocked_milestones),
            "minimum_business_pages": self.minimum_business_pages,
            "covered_business_page_count": self.covered_business_page_count,
            "eligible_candidate_count": self.eligible_candidate_count,
            "pending_action_count": self.pending_action_count,
            "pending_evidence_count": self.pending_evidence_count,
            "frontier_exhausted": self.frontier_exhausted,
            "stable_page_reached": self.stable_page_reached,
            "baseline_evidence_complete": self.baseline_evidence_complete,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppExplorationCoverageSnapshot:
        fields = {
            "protocol_version",
            "required_page_types",
            "covered_page_types",
            "missing_page_types",
            "blocked_page_types",
            "required_milestones",
            "covered_milestones",
            "missing_milestones",
            "blocked_milestones",
            "minimum_business_pages",
            "covered_business_page_count",
            "eligible_candidate_count",
            "pending_action_count",
            "pending_evidence_count",
            "frontier_exhausted",
            "stable_page_reached",
            "baseline_evidence_complete",
        }
        values = _read(
            payload,
            known=fields,
            required=fields,
            field_name="AppExplorationCoverageSnapshot",
        )
        sequence_fields = {
            "required_page_types",
            "covered_page_types",
            "missing_page_types",
            "blocked_page_types",
            "required_milestones",
            "covered_milestones",
            "missing_milestones",
            "blocked_milestones",
        }
        for field_name in sequence_fields:
            values[field_name] = _sequence(values[field_name], field_name)
        return cls(**values)


def _validate_partition(
    *,
    required: tuple[str, ...],
    covered: tuple[str, ...],
    missing: tuple[str, ...],
    blocked: tuple[str, ...],
    field_name: str,
) -> None:
    required_set = set(required)
    groups = (set(covered), set(missing), set(blocked))
    if any(not group.issubset(required_set) for group in groups):
        raise AppExplorationGoalValidationError(f"{field_name} 覆盖分组只能包含 required 项")
    if any(groups[index] & groups[other] for index in range(3) for other in range(index + 1, 3)):
        raise AppExplorationGoalValidationError(f"{field_name} 覆盖分组不能交叉")
    if set().union(*groups) != required_set:
        raise AppExplorationGoalValidationError(f"{field_name} 覆盖分组必须完整划分 required 项")


def _read(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str],
    field_name: str,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(required),
            field_name=field_name,
        )
    except ProtocolContractError as exc:
        raise AppExplorationGoalValidationError(str(exc)) from exc


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    try:
        return require_sequence(value, field_name)
    except ProtocolContractError as exc:
        raise AppExplorationGoalValidationError(str(exc)) from exc


def _unique_texts(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    try:
        normalized = tuple(require_text(value, field_name) for value in values)
    except ProtocolContractError as exc:
        raise AppExplorationGoalValidationError(str(exc)) from exc
    if len(set(normalized)) != len(normalized):
        raise AppExplorationGoalValidationError(f"{field_name} 不能重复")
    return normalized


def _required_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise AppExplorationGoalValidationError(str(exc)) from exc


def _require_non_negative_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppExplorationGoalValidationError(f"{field_name} 必须为非负整数")
