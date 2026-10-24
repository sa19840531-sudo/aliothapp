"""跨页面、旅程与环境矩阵的不可变规则证据输入集合。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.evidence.contracts import (
    EvidenceManifestReference,
    EvidenceQualityStatus,
)
from services.api.app.domain.evidence.vocabulary import normalize_manifest_capture_role
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_mapping,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

EVIDENCE_INPUT_SET_PROTOCOL_VERSION = "evidence-input-set.v1"
_SCOPE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


class EvidenceInputSetValidationError(ValueError):
    """证据集合的范围、封存快照或成员闭包不可信。"""


class EvidenceInputScope(StrEnum):
    """规则输入的聚合边界，不再借用任意页面作为任务级锚点。"""

    PAGE = "page"
    JOURNEY = "journey"
    TASK = "task"
    ENVIRONMENT_MATRIX = "environment_matrix"
    WEB_SEQUENCE = "web_sequence"


@dataclass(frozen=True, kw_only=True)
class EvidenceInputCandidate:
    """封存前由权威选择快照提供的一项 Capture 候选。"""

    manifest: EvidenceManifestReference
    capture_role: str
    page_sequence: int
    journey_execution_id: str | None = None
    environment_profile_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, EvidenceManifestReference):
            raise EvidenceInputSetValidationError("manifest 必须是证据清单引用")
        _require_nonnegative_int(self.page_sequence, "page_sequence")
        _optional_text(self.journey_execution_id, "journey_execution_id")
        _optional_text(self.environment_profile_id, "environment_profile_id")
        try:
            normalized_role = normalize_manifest_capture_role(self.capture_role)
        except ValueError as exc:
            raise EvidenceInputSetValidationError(str(exc)) from exc
        object.__setattr__(self, "capture_role", normalized_role)


@dataclass(frozen=True, kw_only=True)
class EvidenceInputSetMember:
    """一项已通过质量门禁、可按 Manifest 重放校验的输入。"""

    ordinal: int
    capture_id: str
    capture_role: str
    page_sequence: int
    manifest_id: str
    manifest_version: int
    manifest_hash: str
    artifact_ids: tuple[str, ...]
    observation_packet_ids: tuple[str, ...] = ()
    journey_execution_id: str | None = None
    environment_profile_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.ordinal, "ordinal")
        _require_nonnegative_int(self.page_sequence, "page_sequence")
        _require_positive_int(self.manifest_version, "manifest_version")
        for field_name in (
            "capture_id",
            "manifest_id",
            "manifest_hash",
        ):
            _require_text(getattr(self, field_name), field_name)
        try:
            normalized_role = normalize_manifest_capture_role(self.capture_role)
        except ValueError as exc:
            raise EvidenceInputSetValidationError(str(exc)) from exc
        artifact_ids = _unique_text_tuple(self.artifact_ids, "artifact_ids", allow_empty=False)
        packet_ids = _unique_text_tuple(
            self.observation_packet_ids,
            "observation_packet_ids",
            allow_empty=True,
        )
        _optional_text(self.journey_execution_id, "journey_execution_id")
        _optional_text(self.environment_profile_id, "environment_profile_id")
        object.__setattr__(self, "capture_role", normalized_role)
        object.__setattr__(self, "artifact_ids", artifact_ids)
        object.__setattr__(self, "observation_packet_ids", packet_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "capture_id": self.capture_id,
            "capture_role": self.capture_role,
            "page_sequence": self.page_sequence,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "artifact_ids": list(self.artifact_ids),
            "observation_packet_ids": list(self.observation_packet_ids),
            "journey_execution_id": self.journey_execution_id,
            "environment_profile_id": self.environment_profile_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceInputSetMember:
        known = {
            "ordinal",
            "capture_id",
            "capture_role",
            "page_sequence",
            "manifest_id",
            "manifest_version",
            "manifest_hash",
            "artifact_ids",
            "observation_packet_ids",
            "journey_execution_id",
            "environment_profile_id",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="EvidenceInputSetMember",
            )
            return cls(
                ordinal=values["ordinal"],
                capture_id=values["capture_id"],
                capture_role=values["capture_role"],
                page_sequence=values["page_sequence"],
                manifest_id=values["manifest_id"],
                manifest_version=values["manifest_version"],
                manifest_hash=values["manifest_hash"],
                artifact_ids=_text_sequence(values["artifact_ids"], "artifact_ids"),
                observation_packet_ids=_text_sequence(
                    values["observation_packet_ids"],
                    "observation_packet_ids",
                ),
                journey_execution_id=values["journey_execution_id"],
                environment_profile_id=values["environment_profile_id"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceInputSetValidationError(str(exc)) from exc


@dataclass(frozen=True, kw_only=True)
class EvidenceInputSet:
    """任务末从权威选择快照封存的一次规则输入闭包。"""

    input_set_id: str
    input_set_hash: str
    task_id: str
    source_agent_run_id: str
    rule_id: str
    rule_code: str
    rule_contract_hash: str
    ruleset_snapshot_hash: str
    scope: EvidenceInputScope
    scope_key: str
    finalization_epoch: int
    eligibility_snapshot_id: str
    eligibility_snapshot_hash: str
    selection_policy_hash: str
    eligible_capture_ids: tuple[str, ...]
    members: tuple[EvidenceInputSetMember, ...]
    sealed_at: datetime
    protocol_version: str = EVIDENCE_INPUT_SET_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "input_set_id",
            "input_set_hash",
            "task_id",
            "source_agent_run_id",
            "rule_id",
            "rule_code",
            "rule_contract_hash",
            "ruleset_snapshot_hash",
            "eligibility_snapshot_id",
            "eligibility_snapshot_hash",
            "selection_policy_hash",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.scope, EvidenceInputScope):
            raise EvidenceInputSetValidationError("scope 不受支持")
        if not _SCOPE_KEY_PATTERN.fullmatch(str(self.scope_key or "")):
            raise EvidenceInputSetValidationError("scope_key 不符合稳定命名规则")
        _require_nonnegative_int(self.finalization_epoch, "finalization_epoch")
        _require_aware_datetime(self.sealed_at, "sealed_at")
        eligible_capture_ids = _unique_text_tuple(
            self.eligible_capture_ids,
            "eligible_capture_ids",
            allow_empty=False,
        )
        members = tuple(self.members)
        if not members or any(not isinstance(item, EvidenceInputSetMember) for item in members):
            raise EvidenceInputSetValidationError("members 必须由至少一个输入成员组成")
        if tuple(item.ordinal for item in members) != tuple(range(1, len(members) + 1)):
            raise EvidenceInputSetValidationError("members.ordinal 必须从 1 连续递增")
        member_capture_ids = tuple(item.capture_id for item in members)
        if member_capture_ids != eligible_capture_ids:
            raise EvidenceInputSetValidationError("members 必须完整覆盖权威 eligible_capture_ids")
        _validate_scope_members(scope=self.scope, members=members)
        if self.protocol_version != EVIDENCE_INPUT_SET_PROTOCOL_VERSION:
            raise EvidenceInputSetValidationError("EvidenceInputSet 版本不受支持")
        expected_hash = _input_set_hash(
            task_id=self.task_id,
            source_agent_run_id=self.source_agent_run_id,
            rule_id=self.rule_id,
            rule_code=self.rule_code,
            rule_contract_hash=self.rule_contract_hash,
            ruleset_snapshot_hash=self.ruleset_snapshot_hash,
            scope=self.scope,
            scope_key=self.scope_key,
            finalization_epoch=self.finalization_epoch,
            eligibility_snapshot_id=self.eligibility_snapshot_id,
            eligibility_snapshot_hash=self.eligibility_snapshot_hash,
            selection_policy_hash=self.selection_policy_hash,
            eligible_capture_ids=eligible_capture_ids,
            members=members,
        )
        if self.input_set_hash != expected_hash:
            raise EvidenceInputSetValidationError("input_set_hash 与封存输入不一致")
        if self.input_set_id != f"evidence-input-set:{expected_hash}":
            raise EvidenceInputSetValidationError("input_set_id 与封存输入不一致")
        object.__setattr__(self, "eligible_capture_ids", eligible_capture_ids)
        object.__setattr__(self, "members", members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "input_set_id": self.input_set_id,
            "input_set_hash": self.input_set_hash,
            "task_id": self.task_id,
            "source_agent_run_id": self.source_agent_run_id,
            "rule_id": self.rule_id,
            "rule_code": self.rule_code,
            "rule_contract_hash": self.rule_contract_hash,
            "ruleset_snapshot_hash": self.ruleset_snapshot_hash,
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "finalization_epoch": self.finalization_epoch,
            "eligibility_snapshot_id": self.eligibility_snapshot_id,
            "eligibility_snapshot_hash": self.eligibility_snapshot_hash,
            "selection_policy_hash": self.selection_policy_hash,
            "eligible_capture_ids": list(self.eligible_capture_ids),
            "members": [item.to_dict() for item in self.members],
            "sealed_at": self.sealed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceInputSet:
        known = {
            "protocol_version",
            "input_set_id",
            "input_set_hash",
            "task_id",
            "source_agent_run_id",
            "rule_id",
            "rule_code",
            "rule_contract_hash",
            "ruleset_snapshot_hash",
            "scope",
            "scope_key",
            "finalization_epoch",
            "eligibility_snapshot_id",
            "eligibility_snapshot_hash",
            "selection_policy_hash",
            "eligible_capture_ids",
            "members",
            "sealed_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="EvidenceInputSet",
            )
            members = tuple(
                EvidenceInputSetMember.from_dict(require_mapping(item, "members[]"))
                for item in require_sequence(values["members"], "members")
            )
            return cls(
                input_set_id=values["input_set_id"],
                input_set_hash=values["input_set_hash"],
                task_id=values["task_id"],
                source_agent_run_id=values["source_agent_run_id"],
                rule_id=values["rule_id"],
                rule_code=values["rule_code"],
                rule_contract_hash=values["rule_contract_hash"],
                ruleset_snapshot_hash=values["ruleset_snapshot_hash"],
                scope=EvidenceInputScope(values["scope"]),
                scope_key=values["scope_key"],
                finalization_epoch=values["finalization_epoch"],
                eligibility_snapshot_id=values["eligibility_snapshot_id"],
                eligibility_snapshot_hash=values["eligibility_snapshot_hash"],
                selection_policy_hash=values["selection_policy_hash"],
                eligible_capture_ids=_text_sequence(
                    values["eligible_capture_ids"],
                    "eligible_capture_ids",
                ),
                members=members,
                sealed_at=parse_aware_datetime(values["sealed_at"], "sealed_at"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceInputSetValidationError(str(exc)) from exc


def seal_evidence_input_set(
    *,
    task_id: str,
    source_agent_run_id: str,
    rule_id: str,
    rule_code: str,
    rule_contract_hash: str,
    ruleset_snapshot_hash: str,
    scope: EvidenceInputScope,
    scope_key: str,
    finalization_epoch: int,
    eligibility_snapshot_id: str,
    eligibility_snapshot_hash: str,
    selection_policy_hash: str,
    eligible_capture_ids: Sequence[str],
    candidates: Sequence[EvidenceInputCandidate],
    sealed_at: datetime,
) -> EvidenceInputSet:
    """只在 PASS Manifest 完整覆盖权威候选快照时封存。"""
    normalized_task_id = _require_text(task_id, "task_id")
    eligible = _unique_text_tuple(
        eligible_capture_ids,
        "eligible_capture_ids",
        allow_empty=False,
    )
    candidate_items = tuple(candidates)
    if any(not isinstance(item, EvidenceInputCandidate) for item in candidate_items):
        raise EvidenceInputSetValidationError("candidates 类型不受支持")
    candidate_by_capture = {item.manifest.capture_id: item for item in candidate_items}
    if len(candidate_by_capture) != len(candidate_items):
        raise EvidenceInputSetValidationError("candidates 不能包含重复 Capture")
    if set(candidate_by_capture) != set(eligible):
        raise EvidenceInputSetValidationError("candidates 必须完整覆盖权威 eligible_capture_ids")
    members: list[EvidenceInputSetMember] = []
    for ordinal, capture_id in enumerate(eligible, start=1):
        candidate = candidate_by_capture[capture_id]
        manifest = candidate.manifest
        if manifest.task_id != normalized_task_id:
            raise EvidenceInputSetValidationError("Manifest 与 EvidenceInputSet 不属于同一任务")
        if manifest.quality_status is not EvidenceQualityStatus.PASS:
            raise EvidenceInputSetValidationError("EvidenceInputSet 只允许引用 PASS Manifest")
        artifact_roles = {item.role for item in manifest.artifacts}
        if artifact_roles != {candidate.capture_role}:
            raise EvidenceInputSetValidationError("Manifest 附件角色与 Capture 角色不一致")
        members.append(
            EvidenceInputSetMember(
                ordinal=ordinal,
                capture_id=manifest.capture_id,
                capture_role=candidate.capture_role,
                page_sequence=candidate.page_sequence,
                manifest_id=manifest.manifest_id,
                manifest_version=manifest.manifest_version,
                manifest_hash=manifest.manifest_hash,
                artifact_ids=tuple(item.artifact_id for item in manifest.artifacts),
                observation_packet_ids=manifest.observation_packet_ids,
                journey_execution_id=candidate.journey_execution_id,
                environment_profile_id=candidate.environment_profile_id,
            )
        )
    member_tuple = tuple(members)
    digest = _input_set_hash(
        task_id=normalized_task_id,
        source_agent_run_id=source_agent_run_id,
        rule_id=rule_id,
        rule_code=rule_code,
        rule_contract_hash=rule_contract_hash,
        ruleset_snapshot_hash=ruleset_snapshot_hash,
        scope=scope,
        scope_key=scope_key,
        finalization_epoch=finalization_epoch,
        eligibility_snapshot_id=eligibility_snapshot_id,
        eligibility_snapshot_hash=eligibility_snapshot_hash,
        selection_policy_hash=selection_policy_hash,
        eligible_capture_ids=eligible,
        members=member_tuple,
    )
    return EvidenceInputSet(
        input_set_id=f"evidence-input-set:{digest}",
        input_set_hash=digest,
        task_id=normalized_task_id,
        source_agent_run_id=source_agent_run_id,
        rule_id=rule_id,
        rule_code=rule_code,
        rule_contract_hash=rule_contract_hash,
        ruleset_snapshot_hash=ruleset_snapshot_hash,
        scope=scope,
        scope_key=scope_key,
        finalization_epoch=finalization_epoch,
        eligibility_snapshot_id=eligibility_snapshot_id,
        eligibility_snapshot_hash=eligibility_snapshot_hash,
        selection_policy_hash=selection_policy_hash,
        eligible_capture_ids=eligible,
        members=member_tuple,
        sealed_at=sealed_at,
    )


def _input_set_hash(
    *,
    task_id: str,
    source_agent_run_id: str,
    rule_id: str,
    rule_code: str,
    rule_contract_hash: str,
    ruleset_snapshot_hash: str,
    scope: EvidenceInputScope,
    scope_key: str,
    finalization_epoch: int,
    eligibility_snapshot_id: str,
    eligibility_snapshot_hash: str,
    selection_policy_hash: str,
    eligible_capture_ids: Sequence[str],
    members: Sequence[EvidenceInputSetMember],
) -> str:
    return stable_protocol_hash(
        {
            "task_id": _require_text(task_id, "task_id"),
            "source_agent_run_id": _require_text(
                source_agent_run_id,
                "source_agent_run_id",
            ),
            "rule_id": _require_text(rule_id, "rule_id"),
            "rule_code": _require_text(rule_code, "rule_code"),
            "rule_contract_hash": _require_text(
                rule_contract_hash,
                "rule_contract_hash",
            ),
            "ruleset_snapshot_hash": _require_text(
                ruleset_snapshot_hash,
                "ruleset_snapshot_hash",
            ),
            "scope": scope.value if isinstance(scope, EvidenceInputScope) else str(scope),
            "scope_key": _require_text(scope_key, "scope_key"),
            "finalization_epoch": finalization_epoch,
            "eligibility_snapshot_id": _require_text(
                eligibility_snapshot_id,
                "eligibility_snapshot_id",
            ),
            "eligibility_snapshot_hash": _require_text(
                eligibility_snapshot_hash,
                "eligibility_snapshot_hash",
            ),
            "selection_policy_hash": _require_text(
                selection_policy_hash,
                "selection_policy_hash",
            ),
            "eligible_capture_ids": list(eligible_capture_ids),
            "members": [item.to_dict() for item in members],
        }
    )


def _validate_scope_members(
    *,
    scope: EvidenceInputScope,
    members: tuple[EvidenceInputSetMember, ...],
) -> None:
    if scope is EvidenceInputScope.PAGE and len(members) != 1:
        raise EvidenceInputSetValidationError("page scope 必须且只能绑定一个 Capture")
    if scope is EvidenceInputScope.WEB_SEQUENCE:
        roles = tuple(item.capture_role for item in members)
        if len(roles) < 4 or len(roles) % 2 or any(
            role != ("before_action" if index % 2 == 0 else "after_action")
            for index, role in enumerate(roles)
        ):
            raise EvidenceInputSetValidationError("Web 序列必须封存至少两组真实 before/after 帧")
    if scope is EvidenceInputScope.JOURNEY:
        if len(members) < 2:
            raise EvidenceInputSetValidationError("journey scope 至少需要两个 Capture")
        journey_ids = {item.journey_execution_id for item in members}
        if None in journey_ids or len(journey_ids) != 1:
            raise EvidenceInputSetValidationError("journey scope 必须绑定同一 Journey Execution")
    if scope is EvidenceInputScope.ENVIRONMENT_MATRIX:
        environment_ids = tuple(item.environment_profile_id for item in members)
        if any(item is None for item in environment_ids):
            raise EvidenceInputSetValidationError("环境矩阵成员必须绑定 environment_profile_id")
        if len(set(environment_ids)) != len(environment_ids):
            raise EvidenceInputSetValidationError("环境矩阵不能重复 environment_profile_id")
        if len(environment_ids) < 2:
            raise EvidenceInputSetValidationError("环境矩阵至少需要两个环境成员")
        if len({item.page_sequence for item in members}) != 1:
            raise EvidenceInputSetValidationError("一个环境矩阵只能对应同一逻辑页面")


def _text_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    items = require_sequence(value, field_name)
    return tuple(require_text(item, f"{field_name}[]") for item in items)


def _unique_text_tuple(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    try:
        sequence = require_sequence(values, field_name)
        items = tuple(require_text(item, f"{field_name}[]") for item in sequence)
    except (ProtocolContractError, TypeError) as exc:
        raise EvidenceInputSetValidationError(str(exc)) from exc
    if not allow_empty and not items:
        raise EvidenceInputSetValidationError(f"{field_name} 不能为空")
    if len(set(items)) != len(items):
        raise EvidenceInputSetValidationError(f"{field_name} 不能包含重复值")
    return items


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceInputSetValidationError(str(exc)) from exc


def _optional_text(value: Any, field_name: str) -> None:
    if value is not None:
        _require_text(value, field_name)


def _require_nonnegative_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceInputSetValidationError(f"{field_name} 必须为非负整数")


def _require_positive_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceInputSetValidationError(f"{field_name} 必须为正整数")


def _require_aware_datetime(value: Any, field_name: str) -> None:
    try:
        require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceInputSetValidationError(str(exc)) from exc
