"""规则质检缺证据时交还 Orchestrator 的完整补采协议。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.interaction.validation import (
    ACTION_PARAMETER_FORBIDDEN_KEYS,
    InteractionValidationError,
    validate_semantic_target,
)
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

EVIDENCE_REQUEST_PROTOCOL_VERSION = "evidence-request.v1"


class EvidenceRequestValidationError(ValueError):
    """Evidence Request 缺少稳定身份、恢复约束或纯数据边界。"""


class EvidenceSessionDependency(StrEnum):
    """补采是否必须依赖仍存活的原会话。"""

    SNAPSHOT = "snapshot"
    ACTIVE_SESSION = "active_session"


class EvidenceReplayPolicy(StrEnum):
    """允许 Orchestrator 在哪一级环境恢复。"""

    SAME_SURFACE_ONLY = "same_surface_only"
    SAME_CONTEXT_ALLOWED = "same_context_allowed"
    NEW_SESSION_ALLOWED = "new_session_allowed"
    FORBIDDEN = "forbidden"


class EvidenceRequiredEquivalence(StrEnum):
    """规则最低接受的状态来源。"""

    ORIGINAL_ATOMIC = "original_atomic"
    SAME_SURFACE_CONTINUATION = "same_surface_continuation"
    LOGICAL_EQUIVALENT = "logical_equivalent"


class EvidenceSideEffectPolicy(StrEnum):
    """恢复与 Probe 允许产生的最大业务副作用。"""

    READ_ONLY = "read_only"
    ISOLATED_TEST_DATA = "isolated_test_data"
    APPROVAL_REQUIRED = "approval_required"
    PROHIBITED = "prohibited"


class EvidenceSourceGrade(StrEnum):
    """补采证据来源等级，按 HTML 架构从原现场到仅人工参考排序。"""

    A0 = "A0"
    A1 = "A1"
    B = "B"
    C = "C"
    D = "D"


@dataclass(frozen=True, kw_only=True)
class EvidenceInteractionRequirement:
    """需要 Explorer 规划的语义交互目标，不携带平台定位器或 Driver。"""

    operation: InteractionOperation
    semantic_target: str
    purpose: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, InteractionOperation):
            raise EvidenceRequestValidationError("required_interaction.operation 不受支持")
        try:
            validate_semantic_target(
                self.semantic_target,
                "required_interaction.semantic_target",
            )
            require_text(self.purpose, "required_interaction.purpose")
            parameters = freeze_json_mapping(self.parameters, "required_interaction.parameters")
        except (InteractionValidationError, ProtocolContractError) as exc:
            raise EvidenceRequestValidationError(str(exc)) from exc
        forbidden = _find_forbidden_keys(parameters, ACTION_PARAMETER_FORBIDDEN_KEYS)
        if forbidden:
            raise EvidenceRequestValidationError(
                "required_interaction.parameters 包含禁止字段：" + ", ".join(forbidden)
            )
        object.__setattr__(self, "parameters", parameters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "semantic_target": self.semantic_target,
            "purpose": self.purpose,
            "parameters": thaw_json_mapping(self.parameters),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceInteractionRequirement:
        values = _read(
            payload,
            known={"operation", "semantic_target", "purpose", "parameters"},
            required={"operation", "semantic_target", "purpose"},
            field_name="EvidenceInteractionRequirement",
        )
        try:
            operation = InteractionOperation(values["operation"])
            parameters = require_mapping(values.get("parameters", {}), "parameters")
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceRequestValidationError(str(exc)) from exc
        return cls(
            operation=operation,
            semantic_target=values["semantic_target"],
            purpose=values["purpose"],
            parameters=parameters,
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceRequest:
    """描述缺什么证据以及允许如何恢复，不直接下发底层动作。"""

    request_id: str
    requirement_hash: str
    work_item_id: str
    attempt_id: str
    task_id: str
    rule_id: str
    rule_code: str
    target_capture_id: str
    reason_code: str
    evidence_goal: str
    required_artifact_types: tuple[str, ...]
    session_dependency: EvidenceSessionDependency
    replay_policy: EvidenceReplayPolicy
    required_equivalence: EvidenceRequiredEquivalence
    side_effect_policy: EvidenceSideEffectPolicy
    target_invariants: Mapping[str, Any]
    minimum_source_grade: EvidenceSourceGrade
    expires_at: datetime
    required_interaction: EvidenceInteractionRequirement | None = None
    retryable: bool = False
    protocol_version: str = EVIDENCE_REQUEST_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "request_id",
                "requirement_hash",
                "work_item_id",
                "attempt_id",
                "task_id",
                "rule_id",
                "rule_code",
                "target_capture_id",
                "reason_code",
                "evidence_goal",
            ):
                require_text(getattr(self, field_name), field_name)
            require_aware_datetime(self.expires_at, "expires_at")
            invariants = freeze_json_mapping(self.target_invariants, "target_invariants")
        except ProtocolContractError as exc:
            raise EvidenceRequestValidationError(str(exc)) from exc
        artifacts = _unique_text_tuple(
            self.required_artifact_types,
            "required_artifact_types",
        )
        if not artifacts:
            raise EvidenceRequestValidationError("required_artifact_types 不能为空")
        if not invariants:
            raise EvidenceRequestValidationError("target_invariants 不能为空")
        for field_name, expected_type in (
            ("session_dependency", EvidenceSessionDependency),
            ("replay_policy", EvidenceReplayPolicy),
            ("required_equivalence", EvidenceRequiredEquivalence),
            ("side_effect_policy", EvidenceSideEffectPolicy),
            ("minimum_source_grade", EvidenceSourceGrade),
        ):
            if not isinstance(getattr(self, field_name), expected_type):
                raise EvidenceRequestValidationError(f"{field_name} 不受支持")
        if self.required_interaction is not None and not isinstance(
            self.required_interaction,
            EvidenceInteractionRequirement,
        ):
            raise EvidenceRequestValidationError(
                "required_interaction 必须为 EvidenceInteractionRequirement"
            )
        if not isinstance(self.retryable, bool):
            raise EvidenceRequestValidationError("retryable 必须为布尔值")
        if self.protocol_version != EVIDENCE_REQUEST_PROTOCOL_VERSION:
            raise EvidenceRequestValidationError("EvidenceRequest protocol_version 不受支持")
        expected_requirement_hash = _requirement_hash(
            evidence_goal=self.evidence_goal,
            required_artifact_types=artifacts,
            required_interaction=self.required_interaction,
            session_dependency=self.session_dependency,
            replay_policy=self.replay_policy,
            required_equivalence=self.required_equivalence,
            side_effect_policy=self.side_effect_policy,
            target_invariants=invariants,
            minimum_source_grade=self.minimum_source_grade,
        )
        if self.requirement_hash != expected_requirement_hash:
            raise EvidenceRequestValidationError("requirement_hash 与证据要求不一致")
        expected_request_id = _request_id(
            target_capture_id=self.target_capture_id,
            rule_id=self.rule_id,
            rule_code=self.rule_code,
            requirement_hash=self.requirement_hash,
        )
        if self.request_id != expected_request_id:
            raise EvidenceRequestValidationError("request_id 与 Capture、规则和要求哈希不一致")
        object.__setattr__(self, "required_artifact_types", artifacts)
        object.__setattr__(self, "target_invariants", invariants)

    @property
    def reason(self) -> str:
        """兼容现有消费者的原因文本名称。"""
        return self.evidence_goal

    @property
    def requested_evidence(self) -> tuple[str, ...]:
        """兼容迁移期旧字段；新协议以 required_artifact_types 为准。"""
        return self.required_artifact_types

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "requirement_hash": self.requirement_hash,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "rule_id": self.rule_id,
            "rule_code": self.rule_code,
            "target_capture_id": self.target_capture_id,
            "reason_code": self.reason_code,
            "evidence_goal": self.evidence_goal,
            "required_artifact_types": list(self.required_artifact_types),
            "required_interaction": (
                self.required_interaction.to_dict()
                if self.required_interaction is not None
                else None
            ),
            "session_dependency": self.session_dependency.value,
            "replay_policy": self.replay_policy.value,
            "required_equivalence": self.required_equivalence.value,
            "side_effect_policy": self.side_effect_policy.value,
            "target_invariants": thaw_json_mapping(self.target_invariants),
            "minimum_source_grade": self.minimum_source_grade.value,
            "expires_at": self.expires_at.isoformat(),
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceRequest:
        known = {
            "protocol_version",
            "request_id",
            "requirement_hash",
            "work_item_id",
            "attempt_id",
            "task_id",
            "rule_id",
            "rule_code",
            "target_capture_id",
            "reason_code",
            "evidence_goal",
            "required_artifact_types",
            "required_interaction",
            "session_dependency",
            "replay_policy",
            "required_equivalence",
            "side_effect_policy",
            "target_invariants",
            "minimum_source_grade",
            "expires_at",
            "retryable",
        }
        values = _read(
            payload,
            known=known,
            required=known.difference({"required_interaction", "retryable"}),
            field_name="EvidenceRequest",
        )
        try:
            interaction_payload = values.get("required_interaction")
            interaction = (
                EvidenceInteractionRequirement.from_dict(
                    require_mapping(interaction_payload, "required_interaction")
                )
                if interaction_payload is not None
                else None
            )
            return cls(
                request_id=values["request_id"],
                requirement_hash=values["requirement_hash"],
                work_item_id=values["work_item_id"],
                attempt_id=values["attempt_id"],
                task_id=values["task_id"],
                rule_id=values["rule_id"],
                rule_code=values["rule_code"],
                target_capture_id=values["target_capture_id"],
                reason_code=values["reason_code"],
                evidence_goal=values["evidence_goal"],
                required_artifact_types=_sequence(
                    values["required_artifact_types"],
                    "required_artifact_types",
                ),
                required_interaction=interaction,
                session_dependency=EvidenceSessionDependency(values["session_dependency"]),
                replay_policy=EvidenceReplayPolicy(values["replay_policy"]),
                required_equivalence=EvidenceRequiredEquivalence(
                    values["required_equivalence"]
                ),
                side_effect_policy=EvidenceSideEffectPolicy(values["side_effect_policy"]),
                target_invariants=require_mapping(
                    values["target_invariants"],
                    "target_invariants",
                ),
                minimum_source_grade=EvidenceSourceGrade(values["minimum_source_grade"]),
                expires_at=parse_aware_datetime(values["expires_at"], "expires_at"),
                retryable=values.get("retryable", False),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceRequestValidationError(str(exc)) from exc


def build_evidence_request(
    *,
    work_item_id: str,
    attempt_id: str,
    task_id: str,
    rule_id: str,
    rule_code: str,
    target_capture_id: str,
    reason_code: str,
    evidence_goal: str | None = None,
    required_artifact_types: Sequence[str] | None = None,
    required_interaction: EvidenceInteractionRequirement | None = None,
    session_dependency: EvidenceSessionDependency,
    replay_policy: EvidenceReplayPolicy,
    required_equivalence: EvidenceRequiredEquivalence,
    side_effect_policy: EvidenceSideEffectPolicy,
    target_invariants: Mapping[str, Any],
    minimum_source_grade: EvidenceSourceGrade,
    expires_at: datetime,
    retryable: bool = False,
    reason: str | None = None,
    requested_evidence: Sequence[str] | None = None,
) -> EvidenceRequest:
    """按 target Capture + 规则 + requirement hash 生成稳定补采身份。"""
    goal = evidence_goal if evidence_goal is not None else reason
    artifacts_source = (
        required_artifact_types
        if required_artifact_types is not None
        else requested_evidence
    )
    try:
        normalized_goal = require_text(goal, "evidence_goal")
    except ProtocolContractError as exc:
        raise EvidenceRequestValidationError(str(exc)) from exc
    if artifacts_source is None:
        raise EvidenceRequestValidationError("required_artifact_types 不能为空")
    artifacts = _unique_text_tuple(artifacts_source, "required_artifact_types")
    try:
        frozen_invariants = freeze_json_mapping(target_invariants, "target_invariants")
    except ProtocolContractError as exc:
        raise EvidenceRequestValidationError(str(exc)) from exc
    requirement_hash = _requirement_hash(
        evidence_goal=normalized_goal,
        required_artifact_types=artifacts,
        required_interaction=required_interaction,
        session_dependency=session_dependency,
        replay_policy=replay_policy,
        required_equivalence=required_equivalence,
        side_effect_policy=side_effect_policy,
        target_invariants=frozen_invariants,
        minimum_source_grade=minimum_source_grade,
    )
    request_id = _request_id(
        target_capture_id=target_capture_id,
        rule_id=rule_id,
        rule_code=rule_code,
        requirement_hash=requirement_hash,
    )
    return EvidenceRequest(
        request_id=request_id,
        requirement_hash=requirement_hash,
        work_item_id=work_item_id,
        attempt_id=attempt_id,
        task_id=task_id,
        rule_id=rule_id,
        rule_code=rule_code,
        target_capture_id=target_capture_id,
        reason_code=reason_code,
        evidence_goal=normalized_goal,
        required_artifact_types=artifacts,
        required_interaction=required_interaction,
        session_dependency=session_dependency,
        replay_policy=replay_policy,
        required_equivalence=required_equivalence,
        side_effect_policy=side_effect_policy,
        target_invariants=frozen_invariants,
        minimum_source_grade=minimum_source_grade,
        expires_at=expires_at,
        retryable=retryable,
    )


def _requirement_hash(
    *,
    evidence_goal: str,
    required_artifact_types: Sequence[str],
    required_interaction: EvidenceInteractionRequirement | None,
    session_dependency: EvidenceSessionDependency,
    replay_policy: EvidenceReplayPolicy,
    required_equivalence: EvidenceRequiredEquivalence,
    side_effect_policy: EvidenceSideEffectPolicy,
    target_invariants: Mapping[str, Any],
    minimum_source_grade: EvidenceSourceGrade,
) -> str:
    return stable_protocol_hash(
        {
            "evidence_goal": evidence_goal,
            "required_artifact_types": list(required_artifact_types),
            "required_interaction": (
                required_interaction.to_dict()
                if required_interaction is not None
                else None
            ),
            "session_dependency": session_dependency.value,
            "replay_policy": replay_policy.value,
            "required_equivalence": required_equivalence.value,
            "side_effect_policy": side_effect_policy.value,
            "target_invariants": thaw_json_mapping(target_invariants),
            "minimum_source_grade": minimum_source_grade.value,
        }
    )


def _request_id(
    *,
    target_capture_id: str,
    rule_id: str,
    rule_code: str,
    requirement_hash: str,
) -> str:
    return "evidence-request:" + stable_protocol_hash(
        {
            "target_capture_id": target_capture_id,
            "rule_id": rule_id,
            "rule_code": rule_code,
            "requirement_hash": requirement_hash,
        }
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
        raise EvidenceRequestValidationError(str(exc)) from exc


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    try:
        return require_sequence(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceRequestValidationError(str(exc)) from exc


def _unique_text_tuple(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise EvidenceRequestValidationError(f"{field_name} 必须为数组")
    try:
        normalized = tuple(require_text(value, field_name) for value in values)
    except ProtocolContractError as exc:
        raise EvidenceRequestValidationError(str(exc)) from exc
    if len(set(normalized)) != len(normalized):
        raise EvidenceRequestValidationError(f"{field_name} 不能包含重复值")
    return normalized


def _find_forbidden_keys(
    value: Any,
    forbidden_keys: frozenset[str],
) -> tuple[str, ...]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.casefold() in forbidden_keys:
                found.add(key)
            found.update(_find_forbidden_keys(item, forbidden_keys))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_find_forbidden_keys(item, forbidden_keys))
    return tuple(sorted(found))
