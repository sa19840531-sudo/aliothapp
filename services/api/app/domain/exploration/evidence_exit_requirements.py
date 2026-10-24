"""Evidence Exit Gate 消费的不可变 active_session 需求快照。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

EVIDENCE_EXIT_REQUIREMENT_PROTOCOL_VERSION = "evidence-exit-requirement.v1"
EVIDENCE_EXIT_REQUIREMENT_SET_PROTOCOL_VERSION = "evidence-exit-requirement-set.v1"


class EvidenceExitRequirementStatus(StrEnum):
    """一个活跃会话证据需求的收敛状态。"""

    PENDING = "pending"
    SATISFIED = "satisfied"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    MANUAL_REVIEW = "manual_review"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self is not EvidenceExitRequirementStatus.PENDING


@dataclass(frozen=True, kw_only=True)
class ActiveSessionEvidenceRequirement:
    """规则匹配器冻结的一条 active_session Capture Requirement。"""

    requirement_id: str
    rule_id: str
    rule_code: str
    required_artifact_types: tuple[str, ...]
    required_capture_roles: tuple[str, ...]
    # E02 条款 1.6：规则级 quality_gate 两键随需求集下发（需求声明，不是裁决结果）；
    # 不参与 requirement_id 身份，跨修订漂移由仓储校验拦截。
    min_screenshot_count: int = 0
    allow_empty_visible_text: bool = True
    status: EvidenceExitRequirementStatus
    evidence_request_id: str | None = None
    resolution_manifest_id: str | None = None
    resolution_reason: str | None = None
    protocol_version: str = EVIDENCE_EXIT_REQUIREMENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("requirement_id", "rule_id", "rule_code"):
            _require_text(getattr(self, field_name), field_name)
        artifacts = _unique_text_tuple(
            self.required_artifact_types,
            "required_artifact_types",
        )
        roles = _unique_text_tuple(self.required_capture_roles, "required_capture_roles")
        if not artifacts and not roles:
            raise ValueError("active_session 需求必须声明 Artifact 或 Capture Role")
        if isinstance(self.min_screenshot_count, bool) or not isinstance(
            self.min_screenshot_count, int
        ):
            raise ValueError("min_screenshot_count 必须为非负整数")
        if self.min_screenshot_count < 0:
            raise ValueError("min_screenshot_count 必须为非负整数")
        if not isinstance(self.allow_empty_visible_text, bool):
            raise ValueError("allow_empty_visible_text 必须为布尔值")
        if not isinstance(self.status, EvidenceExitRequirementStatus):
            raise ValueError("status 不受支持")
        for field_name in (
            "evidence_request_id",
            "resolution_manifest_id",
            "resolution_reason",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if self.status is EvidenceExitRequirementStatus.PENDING:
            if self.resolution_manifest_id is not None or self.resolution_reason is not None:
                raise ValueError("pending 需求不能提前携带终态结果")
        elif self.status is EvidenceExitRequirementStatus.SATISFIED:
            if self.resolution_manifest_id is None or self.resolution_reason is not None:
                raise ValueError("satisfied 需求必须且只能关联质量通过的 Manifest")
        elif self.resolution_reason is None:
            raise ValueError("非满足终态必须记录 resolution_reason")
        if self.protocol_version != EVIDENCE_EXIT_REQUIREMENT_PROTOCOL_VERSION:
            raise ValueError("Evidence Exit Requirement 版本不受支持")
        object.__setattr__(self, "required_artifact_types", artifacts)
        object.__setattr__(self, "required_capture_roles", roles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "requirement_id": self.requirement_id,
            "rule_id": self.rule_id,
            "rule_code": self.rule_code,
            "required_artifact_types": list(self.required_artifact_types),
            "required_capture_roles": list(self.required_capture_roles),
            "min_screenshot_count": self.min_screenshot_count,
            "allow_empty_visible_text": self.allow_empty_visible_text,
            "status": self.status.value,
            "evidence_request_id": self.evidence_request_id,
            "resolution_manifest_id": self.resolution_manifest_id,
            "resolution_reason": self.resolution_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ActiveSessionEvidenceRequirement:
        known = {
            "protocol_version",
            "requirement_id",
            "rule_id",
            "rule_code",
            "required_artifact_types",
            "required_capture_roles",
            "min_screenshot_count",
            "allow_empty_visible_text",
            "status",
            "evidence_request_id",
            "resolution_manifest_id",
            "resolution_reason",
        }
        values = _read(
            payload,
            known=known,
            required=known,
            field_name="ActiveSessionEvidenceRequirement",
        )
        try:
            return cls(
                requirement_id=values["requirement_id"],
                rule_id=values["rule_id"],
                rule_code=values["rule_code"],
                required_artifact_types=_sequence(
                    values["required_artifact_types"],
                    "required_artifact_types",
                ),
                required_capture_roles=_sequence(
                    values["required_capture_roles"],
                    "required_capture_roles",
                ),
                min_screenshot_count=values["min_screenshot_count"],
                allow_empty_visible_text=values["allow_empty_visible_text"],
                status=EvidenceExitRequirementStatus(values["status"]),
                evidence_request_id=values["evidence_request_id"],
                resolution_manifest_id=values["resolution_manifest_id"],
                resolution_reason=values["resolution_reason"],
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


@dataclass(frozen=True, kw_only=True)
class EvidenceExitRequirementSet:
    """一次规则匹配/证据收敛后发布的追加式需求快照。"""

    requirement_set_id: str
    revision: int
    task_id: str
    session_id: str
    capture_id: str
    matcher_snapshot_hash: str
    requirements: tuple[ActiveSessionEvidenceRequirement, ...]
    created_at: datetime
    protocol_version: str = EVIDENCE_EXIT_REQUIREMENT_SET_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "requirement_set_id",
            "task_id",
            "session_id",
            "capture_id",
            "matcher_snapshot_hash",
        ):
            _require_text(getattr(self, field_name), field_name)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ValueError("revision 必须为正整数")
        if self.revision <= 0:
            raise ValueError("revision 必须为正整数")
        requirements = tuple(self.requirements)
        if any(not isinstance(item, ActiveSessionEvidenceRequirement) for item in requirements):
            raise ValueError("requirements 类型不正确")
        requirement_ids = tuple(item.requirement_id for item in requirements)
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirements 不能包含重复 requirement_id")
        for item in requirements:
            expected_id = build_evidence_exit_requirement_id(
                task_id=self.task_id,
                capture_id=self.capture_id,
                rule_id=item.rule_id,
                required_artifact_types=item.required_artifact_types,
                required_capture_roles=item.required_capture_roles,
            )
            if item.requirement_id != expected_id:
                raise ValueError("requirement_id 与任务、Capture、规则和需求内容不一致")
        _require_aware_datetime(self.created_at, "created_at")
        if self.protocol_version != EVIDENCE_EXIT_REQUIREMENT_SET_PROTOCOL_VERSION:
            raise ValueError("Evidence Exit Requirement Set 版本不受支持")
        expected_set_id = _requirement_set_id(
            revision=self.revision,
            task_id=self.task_id,
            session_id=self.session_id,
            capture_id=self.capture_id,
            matcher_snapshot_hash=self.matcher_snapshot_hash,
            requirements=requirements,
        )
        if self.requirement_set_id != expected_set_id:
            raise ValueError("requirement_set_id 与需求快照内容不一致")
        object.__setattr__(self, "requirements", requirements)

    @property
    def nonterminal_count(self) -> int:
        return sum(not item.status.is_terminal for item in self.requirements)

    @property
    def all_terminal(self) -> bool:
        return self.nonterminal_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "requirement_set_id": self.requirement_set_id,
            "revision": self.revision,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "capture_id": self.capture_id,
            "matcher_snapshot_hash": self.matcher_snapshot_hash,
            "requirements": [item.to_dict() for item in self.requirements],
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceExitRequirementSet:
        if payload.get("protocol_version") == "web-payload-requirement-set.v1":
            from services.api.app.domain.exploration.web_payload_requirements import (
                WebPayloadRequirementSet,
            )
            return WebPayloadRequirementSet.from_dict(payload)
        known = {
            "protocol_version",
            "requirement_set_id",
            "revision",
            "task_id",
            "session_id",
            "capture_id",
            "matcher_snapshot_hash",
            "requirements",
            "created_at",
        }
        values = _read(
            payload,
            known=known,
            required=known,
            field_name="EvidenceExitRequirementSet",
        )
        try:
            return cls(
                requirement_set_id=values["requirement_set_id"],
                revision=values["revision"],
                task_id=values["task_id"],
                session_id=values["session_id"],
                capture_id=values["capture_id"],
                matcher_snapshot_hash=values["matcher_snapshot_hash"],
                requirements=tuple(
                    ActiveSessionEvidenceRequirement.from_dict(_mapping(item, "requirements[]"))
                    for item in _sequence(values["requirements"], "requirements")
                ),
                created_at=parse_aware_datetime(values["created_at"], "created_at"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


def build_evidence_exit_requirement_id(
    *,
    task_id: str,
    capture_id: str,
    rule_id: str,
    required_artifact_types: Sequence[str],
    required_capture_roles: Sequence[str],
) -> str:
    """生成跨需求快照版本稳定的成员身份。"""
    artifacts = _unique_text_tuple(required_artifact_types, "required_artifact_types")
    roles = _unique_text_tuple(required_capture_roles, "required_capture_roles")
    return "exit-requirement:" + stable_protocol_hash(
        {
            "task_id": _require_text(task_id, "task_id"),
            "capture_id": _require_text(capture_id, "capture_id"),
            "rule_id": _require_text(rule_id, "rule_id"),
            "required_artifact_types": sorted(artifacts),
            "required_capture_roles": sorted(roles),
        }
    )


def build_active_session_evidence_requirement(
    *,
    task_id: str,
    capture_id: str,
    rule_id: str,
    rule_code: str,
    required_artifact_types: Sequence[str],
    required_capture_roles: Sequence[str],
    status: EvidenceExitRequirementStatus,
    min_screenshot_count: int = 0,
    allow_empty_visible_text: bool = True,
    evidence_request_id: str | None = None,
    resolution_manifest_id: str | None = None,
    resolution_reason: str | None = None,
) -> ActiveSessionEvidenceRequirement:
    """构造一条已绑定稳定身份的活跃会话需求。"""
    artifacts = _unique_text_tuple(required_artifact_types, "required_artifact_types")
    roles = _unique_text_tuple(required_capture_roles, "required_capture_roles")
    return ActiveSessionEvidenceRequirement(
        requirement_id=build_evidence_exit_requirement_id(
            task_id=task_id,
            capture_id=capture_id,
            rule_id=rule_id,
            required_artifact_types=artifacts,
            required_capture_roles=roles,
        ),
        rule_id=rule_id,
        rule_code=rule_code,
        required_artifact_types=artifacts,
        required_capture_roles=roles,
        min_screenshot_count=min_screenshot_count,
        allow_empty_visible_text=allow_empty_visible_text,
        status=status,
        evidence_request_id=evidence_request_id,
        resolution_manifest_id=resolution_manifest_id,
        resolution_reason=resolution_reason,
    )


def build_evidence_exit_requirement_set(
    *,
    revision: int,
    task_id: str,
    session_id: str,
    capture_id: str,
    matcher_snapshot_hash: str,
    requirements: Sequence[ActiveSessionEvidenceRequirement],
    created_at: datetime,
) -> EvidenceExitRequirementSet:
    """构造追加式需求快照；空 requirements 明确表示当前页没有命中规则产生的证据需求。"""
    members = tuple(requirements)
    return EvidenceExitRequirementSet(
        requirement_set_id=_requirement_set_id(
            revision=revision,
            task_id=task_id,
            session_id=session_id,
            capture_id=capture_id,
            matcher_snapshot_hash=matcher_snapshot_hash,
            requirements=members,
        ),
        revision=revision,
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        matcher_snapshot_hash=matcher_snapshot_hash,
        requirements=members,
        created_at=created_at,
    )


def _requirement_set_id(
    *,
    revision: int,
    task_id: str,
    session_id: str,
    capture_id: str,
    matcher_snapshot_hash: str,
    requirements: Sequence[ActiveSessionEvidenceRequirement],
) -> str:
    return "exit-requirement-set:" + stable_protocol_hash(
        {
            "revision": revision,
            "task_id": task_id,
            "session_id": session_id,
            "capture_id": capture_id,
            "matcher_snapshot_hash": matcher_snapshot_hash,
            "requirements": [item.to_dict() for item in requirements],
        }
    )


def _unique_text_tuple(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} 必须为数组")
    normalized = tuple(_require_text(item, field_name) for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} 不能重复")
    return tuple(sorted(normalized))


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
        raise ValueError(str(exc)) from exc


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须为对象")
    return value


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    try:
        return require_sequence(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc


def _require_aware_datetime(value: Any, field_name: str) -> datetime:
    try:
        return require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc
