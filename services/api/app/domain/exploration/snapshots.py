"""Explorer 可消费的类型化 Surface Observation、Frontier 与边界快照。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    validate_semantic_target,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    optional_text,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_mapping,
    require_sequence,
    require_text,
    stable_protocol_hash,
    thaw_json_mapping,
)

SURFACE_OBSERVATION_PROTOCOL_VERSION = "surface-observation.v1"
CANDIDATE_ACTION_PROTOCOL_VERSION = "candidate-action.v1"
FRONTIER_SNAPSHOT_PROTOCOL_VERSION = "frontier-snapshot.v1"
EXPLORATION_BOUNDARIES_PROTOCOL_VERSION = "exploration-boundaries.v1"


class ExplorationSnapshotValidationError(ValueError):
    """Explorer 快照缺少稳定身份或包含不受支持的语义。"""


class SideEffectPolicy(StrEnum):
    """探索动作允许产生的最大业务副作用。"""

    READ_ONLY = "read_only"
    ISOLATED_TEST_DATA = "isolated_test_data"
    APPROVAL_REQUIRED = "approval_required"
    PROHIBITED = "prohibited"


@dataclass(frozen=True, kw_only=True)
class SurfaceObservation:
    """Interaction Runtime 返回给 Explorer 的有界当前状态。"""

    capture_id: str
    surface_kind: SurfaceKind
    location: str
    state_version: str
    observed_at: datetime
    page_type: str | None = None
    page_state: str | None = None
    overlay_signature: str | None = None
    key_elements: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = SURFACE_OBSERVATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in ("capture_id", "location", "state_version"):
                require_text(getattr(self, field_name), field_name)
            optional_text(self.page_type, "page_type")
            optional_text(self.page_state, "page_state")
            optional_text(self.overlay_signature, "overlay_signature")
            require_aware_datetime(self.observed_at, "observed_at")
        except ProtocolContractError as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        if not isinstance(self.surface_kind, SurfaceKind):
            raise ExplorationSnapshotValidationError("surface_kind 不受支持")
        if self.protocol_version != SURFACE_OBSERVATION_PROTOCOL_VERSION:
            raise ExplorationSnapshotValidationError("SurfaceObservation protocol_version 不受支持")
        key_elements = _unique_text_tuple(self.key_elements, "key_elements")
        try:
            metadata = freeze_json_mapping(self.metadata, "metadata")
        except ProtocolContractError as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        object.__setattr__(self, "key_elements", key_elements)
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "capture_id": self.capture_id,
            "surface_kind": self.surface_kind.value,
            "location": self.location,
            "state_version": self.state_version,
            "observed_at": self.observed_at.isoformat(),
            "page_type": self.page_type,
            "page_state": self.page_state,
            "overlay_signature": self.overlay_signature,
            "key_elements": list(self.key_elements),
            "metadata": thaw_json_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SurfaceObservation:
        values = _read(
            payload,
            known={
                "protocol_version",
                "capture_id",
                "surface_kind",
                "location",
                "state_version",
                "observed_at",
                "page_type",
                "page_state",
                "overlay_signature",
                "key_elements",
                "metadata",
            },
            required={
                "protocol_version",
                "capture_id",
                "surface_kind",
                "location",
                "state_version",
                "observed_at",
            },
            field_name="SurfaceObservation",
        )
        try:
            surface_kind = SurfaceKind(values["surface_kind"])
            observed_at = parse_aware_datetime(values["observed_at"], "observed_at")
        except (ProtocolContractError, ValueError) as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        return cls(
            capture_id=values["capture_id"],
            surface_kind=surface_kind,
            location=values["location"],
            state_version=values["state_version"],
            observed_at=observed_at,
            page_type=values.get("page_type"),
            page_state=values.get("page_state"),
            overlay_signature=values.get("overlay_signature"),
            key_elements=_sequence(values.get("key_elements", ()), "key_elements"),
            metadata=_mapping(values.get("metadata", {}), "metadata"),
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class CandidateAction:
    """Frontier 中一条尚未验证的语义动作候选。"""

    candidate_id: str
    semantic_target: str
    action_type: InteractionOperation
    risk_level: InteractionRiskLevel
    exploration_value: int
    navigation_depth: int
    expected_surface_kind: SurfaceKind | None = None
    prior_failure_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = CANDIDATE_ACTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            require_text(self.candidate_id, "candidate_id")
            validate_semantic_target(self.semantic_target, "semantic_target")
        except (InteractionValidationError, ProtocolContractError) as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        if not isinstance(self.action_type, InteractionOperation):
            raise ExplorationSnapshotValidationError("action_type 不受支持")
        if not isinstance(self.risk_level, InteractionRiskLevel):
            raise ExplorationSnapshotValidationError("risk_level 不受支持")
        if self.expected_surface_kind is not None and not isinstance(
            self.expected_surface_kind, SurfaceKind
        ):
            raise ExplorationSnapshotValidationError("expected_surface_kind 不受支持")
        for field_name in ("exploration_value", "navigation_depth", "prior_failure_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExplorationSnapshotValidationError(f"{field_name} 必须为非负整数")
        if self.exploration_value > 100:
            raise ExplorationSnapshotValidationError("exploration_value 不能超过 100")
        if self.protocol_version != CANDIDATE_ACTION_PROTOCOL_VERSION:
            raise ExplorationSnapshotValidationError("CandidateAction protocol_version 不受支持")
        try:
            metadata = freeze_json_mapping(self.metadata, "metadata")
        except ProtocolContractError as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "candidate_id": self.candidate_id,
            "semantic_target": self.semantic_target,
            "action_type": self.action_type.value,
            "risk_level": self.risk_level.value,
            "exploration_value": self.exploration_value,
            "navigation_depth": self.navigation_depth,
            "expected_surface_kind": (
                self.expected_surface_kind.value if self.expected_surface_kind is not None else None
            ),
            "prior_failure_count": self.prior_failure_count,
            "metadata": thaw_json_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CandidateAction:
        values = _read(
            payload,
            known={
                "protocol_version",
                "candidate_id",
                "semantic_target",
                "action_type",
                "risk_level",
                "exploration_value",
                "navigation_depth",
                "expected_surface_kind",
                "prior_failure_count",
                "metadata",
            },
            required={
                "protocol_version",
                "candidate_id",
                "semantic_target",
                "action_type",
                "risk_level",
                "exploration_value",
                "navigation_depth",
            },
            field_name="CandidateAction",
        )
        try:
            action_type = InteractionOperation(values["action_type"])
            risk_level = InteractionRiskLevel(values["risk_level"])
            expected = values.get("expected_surface_kind")
            expected_kind = SurfaceKind(expected) if expected is not None else None
        except ValueError as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        return cls(
            candidate_id=values["candidate_id"],
            semantic_target=values["semantic_target"],
            action_type=action_type,
            risk_level=risk_level,
            exploration_value=values["exploration_value"],
            navigation_depth=values["navigation_depth"],
            expected_surface_kind=expected_kind,
            prior_failure_count=values.get("prior_failure_count", 0),
            metadata=_mapping(values.get("metadata", {}), "metadata"),
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class FrontierSnapshot:
    """一个状态版本下有序、可重放的探索候选集合。"""

    snapshot_id: str
    observation: SurfaceObservation
    candidates: tuple[CandidateAction, ...]
    visited_state_ids: tuple[str, ...] = ()
    blocked_candidate_ids: tuple[str, ...] = ()
    visited_candidate_ids: tuple[str, ...] = ()
    visited_action_fingerprints: tuple[str, ...] = ()
    repeated_state_count: int = 0
    protocol_version: str = FRONTIER_SNAPSHOT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            require_text(self.snapshot_id, "snapshot_id")
        except ProtocolContractError as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        if not isinstance(self.observation, SurfaceObservation):
            raise ExplorationSnapshotValidationError("observation 必须为 SurfaceObservation")
        candidates = tuple(self.candidates)
        if any(not isinstance(item, CandidateAction) for item in candidates):
            raise ExplorationSnapshotValidationError("candidates 必须由 CandidateAction 组成")
        candidate_ids = tuple(item.candidate_id for item in candidates)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ExplorationSnapshotValidationError("candidates 不能包含重复 candidate_id")
        visited_state_ids = _unique_text_tuple(self.visited_state_ids, "visited_state_ids")
        blocked_candidate_ids = _unique_text_tuple(
            self.blocked_candidate_ids,
            "blocked_candidate_ids",
        )
        visited_candidate_ids = _unique_text_tuple(
            self.visited_candidate_ids,
            "visited_candidate_ids",
        )
        visited_action_fingerprints = _unique_text_tuple(
            self.visited_action_fingerprints,
            "visited_action_fingerprints",
        )
        if (
            isinstance(self.repeated_state_count, bool)
            or not isinstance(self.repeated_state_count, int)
            or self.repeated_state_count < 0
        ):
            raise ExplorationSnapshotValidationError(
                "repeated_state_count 必须为非负整数"
            )
        unknown_blocked = sorted(set(blocked_candidate_ids).difference(candidate_ids))
        if unknown_blocked:
            raise ExplorationSnapshotValidationError(
                "blocked_candidate_ids 包含不属于当前 Frontier 的候选"
            )
        if self.protocol_version != FRONTIER_SNAPSHOT_PROTOCOL_VERSION:
            raise ExplorationSnapshotValidationError("FrontierSnapshot protocol_version 不受支持")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "visited_state_ids", visited_state_ids)
        object.__setattr__(self, "blocked_candidate_ids", blocked_candidate_ids)
        object.__setattr__(self, "visited_candidate_ids", visited_candidate_ids)
        object.__setattr__(
            self,
            "visited_action_fingerprints",
            visited_action_fingerprints,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "snapshot_id": self.snapshot_id,
            "observation": self.observation.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "visited_state_ids": list(self.visited_state_ids),
            "blocked_candidate_ids": list(self.blocked_candidate_ids),
            "visited_candidate_ids": list(self.visited_candidate_ids),
            "visited_action_fingerprints": list(self.visited_action_fingerprints),
            "repeated_state_count": self.repeated_state_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FrontierSnapshot:
        values = _read(
            payload,
            known={
                "protocol_version",
                "snapshot_id",
                "observation",
                "candidates",
                "visited_state_ids",
                "blocked_candidate_ids",
                "visited_candidate_ids",
                "visited_action_fingerprints",
                "repeated_state_count",
            },
            required={"protocol_version", "snapshot_id", "observation", "candidates"},
            field_name="FrontierSnapshot",
        )
        return cls(
            snapshot_id=values["snapshot_id"],
            observation=SurfaceObservation.from_dict(
                _mapping(values["observation"], "observation")
            ),
            candidates=tuple(
                CandidateAction.from_dict(_mapping(item, "candidates[]"))
                for item in _sequence(values["candidates"], "candidates")
            ),
            visited_state_ids=_sequence(
                values.get("visited_state_ids", ()),
                "visited_state_ids",
            ),
            blocked_candidate_ids=_sequence(
                values.get("blocked_candidate_ids", ()),
                "blocked_candidate_ids",
            ),
            visited_candidate_ids=_sequence(
                values.get("visited_candidate_ids", ()),
                "visited_candidate_ids",
            ),
            visited_action_fingerprints=_sequence(
                values.get("visited_action_fingerprints", ()),
                "visited_action_fingerprints",
            ),
            repeated_state_count=values.get("repeated_state_count", 0),
            protocol_version=values["protocol_version"],
        )


def build_action_fingerprint(
    *,
    state_version: str,
    candidate: CandidateAction,
) -> str:
    """按页面状态、动作和语义目标生成跨 Capture 稳定的访问指纹。"""
    try:
        require_text(state_version, "state_version")
    except ProtocolContractError as exc:
        raise ExplorationSnapshotValidationError(str(exc)) from exc
    if not isinstance(candidate, CandidateAction):
        raise TypeError("candidate 必须为 CandidateAction")
    return "action-fingerprint:" + stable_protocol_hash(
        {
            "state_version": state_version,
            "action_type": candidate.action_type.value,
            "semantic_target": candidate.semantic_target,
            "expected_state_hint": candidate.metadata.get("expected_state_hint"),
        }
    )


@dataclass(frozen=True, kw_only=True)
class ExplorationBoundaries:
    """Orchestrator 锁定且 Explorer 不可扩大的探索许可边界。"""

    allowed_hosts: tuple[str, ...]
    allowed_operations: tuple[InteractionOperation, ...]
    max_navigation_depth: int
    side_effect_policy: SideEffectPolicy
    allowed_route_prefixes: tuple[str, ...] = ()
    protocol_version: str = EXPLORATION_BOUNDARIES_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        allowed_hosts = _unique_text_tuple(self.allowed_hosts, "allowed_hosts")
        if not allowed_hosts:
            raise ExplorationSnapshotValidationError("allowed_hosts 不能为空")
        operations = tuple(self.allowed_operations)
        if not operations or any(not isinstance(item, InteractionOperation) for item in operations):
            raise ExplorationSnapshotValidationError(
                "allowed_operations 必须由至少一个 InteractionOperation 组成"
            )
        if len(set(operations)) != len(operations):
            raise ExplorationSnapshotValidationError("allowed_operations 不能重复")
        if (
            isinstance(self.max_navigation_depth, bool)
            or not isinstance(self.max_navigation_depth, int)
            or self.max_navigation_depth < 0
        ):
            raise ExplorationSnapshotValidationError("max_navigation_depth 必须为非负整数")
        if not isinstance(self.side_effect_policy, SideEffectPolicy):
            raise ExplorationSnapshotValidationError("side_effect_policy 不受支持")
        if self.protocol_version != EXPLORATION_BOUNDARIES_PROTOCOL_VERSION:
            raise ExplorationSnapshotValidationError(
                "ExplorationBoundaries protocol_version 不受支持"
            )
        object.__setattr__(self, "allowed_hosts", allowed_hosts)
        object.__setattr__(self, "allowed_operations", operations)
        object.__setattr__(
            self,
            "allowed_route_prefixes",
            _unique_text_tuple(self.allowed_route_prefixes, "allowed_route_prefixes"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_operations": [item.value for item in self.allowed_operations],
            "max_navigation_depth": self.max_navigation_depth,
            "side_effect_policy": self.side_effect_policy.value,
            "allowed_route_prefixes": list(self.allowed_route_prefixes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExplorationBoundaries:
        values = _read(
            payload,
            known={
                "protocol_version",
                "allowed_hosts",
                "allowed_operations",
                "max_navigation_depth",
                "side_effect_policy",
                "allowed_route_prefixes",
            },
            required={
                "protocol_version",
                "allowed_hosts",
                "allowed_operations",
                "max_navigation_depth",
                "side_effect_policy",
            },
            field_name="ExplorationBoundaries",
        )
        try:
            operations = tuple(
                InteractionOperation(item)
                for item in _sequence(values["allowed_operations"], "allowed_operations")
            )
            side_effect_policy = SideEffectPolicy(values["side_effect_policy"])
        except ValueError as exc:
            raise ExplorationSnapshotValidationError(str(exc)) from exc
        return cls(
            allowed_hosts=_sequence(values["allowed_hosts"], "allowed_hosts"),
            allowed_operations=operations,
            max_navigation_depth=values["max_navigation_depth"],
            side_effect_policy=side_effect_policy,
            allowed_route_prefixes=_sequence(
                values.get("allowed_route_prefixes", ()),
                "allowed_route_prefixes",
            ),
            protocol_version=values["protocol_version"],
        )


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
        raise ExplorationSnapshotValidationError(str(exc)) from exc


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    try:
        return require_mapping(value, field_name)
    except ProtocolContractError as exc:
        raise ExplorationSnapshotValidationError(str(exc)) from exc


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    try:
        return require_sequence(value, field_name)
    except ProtocolContractError as exc:
        raise ExplorationSnapshotValidationError(str(exc)) from exc


def _unique_text_tuple(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    try:
        normalized = tuple(require_text(value, field_name) for value in values)
    except ProtocolContractError as exc:
        raise ExplorationSnapshotValidationError(str(exc)) from exc
    if len(set(normalized)) != len(normalized):
        raise ExplorationSnapshotValidationError(f"{field_name} 不能包含重复值")
    return normalized
