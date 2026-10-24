"""App 探索结果与不可变 Evidence Index 领域合同。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.evidence.contracts import (
    EvidenceContractValidationError,
)
from services.api.app.domain.protocol import (
    MAX_PROTOCOL_PAYLOAD_BYTES,
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

APP_EVIDENCE_GAP_PROTOCOL_VERSION = "app-evidence-gap.v1"
APP_EVIDENCE_INDEX_ENTRY_PROTOCOL_VERSION = "app-evidence-index-entry.v1"
APP_EVIDENCE_INDEX_PROTOCOL_VERSION = "app-evidence-index.v1"
APP_EXPLORATION_RESULT_PROTOCOL_VERSION = "app-exploration-result.v2"
_MAX_APP_EVIDENCE_INDEX_PAYLOAD_BYTES = 4_000_000
_SUPPORTED_APP_EXPLORATION_RESULT_PROTOCOL_VERSIONS = frozenset(
    {"app-exploration-result.v1", APP_EXPLORATION_RESULT_PROTOCOL_VERSION}
)


class AppEvidenceIndexStatus(StrEnum):
    """Evidence Index 的构建生命周期。"""

    BUILDING = "building"
    SEALED = "sealed"


class AppEvidenceSemanticRole(StrEnum):
    """App 证据在探索业务流程中的稳定语义角色。"""

    BOOTSTRAP = "bootstrap"
    SURFACE_BEFORE = "surface_before"
    SURFACE_AFTER = "surface_after"
    SURFACE_REOBSERVE = "surface_reobserve"
    ACTION_BEFORE = "action_before"
    ACTION_AFTER = "action_after"
    FINAL_OBSERVATION = "final_observation"


class AppExplorationResultOutcome(StrEnum):
    """可以持久化并交接给正式质检的 App 探索业务结果。"""

    EXPLORATION_COMPLETED = "exploration_completed"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"


@dataclass(frozen=True, kw_only=True)
class AppEvidenceGap:
    """无法形成可信证据条目时的显式缺口。"""

    semantic_role: AppEvidenceSemanticRole
    reason_code: str
    description: str
    capture_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = APP_EVIDENCE_GAP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            require_text(self.reason_code, "reason_code")
            require_text(self.description, "description")
            if self.capture_id is not None:
                require_text(self.capture_id, "capture_id")
            metadata = freeze_json_mapping(self.metadata, "metadata")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        if not isinstance(self.semantic_role, AppEvidenceSemanticRole):
            raise EvidenceContractValidationError("semantic_role 不受支持")
        if self.protocol_version != APP_EVIDENCE_GAP_PROTOCOL_VERSION:
            raise EvidenceContractValidationError("AppEvidenceGap protocol_version 不受支持")
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "semantic_role": self.semantic_role.value,
            "reason_code": self.reason_code,
            "description": self.description,
            "capture_id": self.capture_id,
            "metadata": thaw_json_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppEvidenceGap:
        values = _read(
            payload,
            known={
                "protocol_version",
                "semantic_role",
                "reason_code",
                "description",
                "capture_id",
                "metadata",
            },
            required={
                "protocol_version",
                "semantic_role",
                "reason_code",
                "description",
            },
            field_name="AppEvidenceGap",
        )
        try:
            role = AppEvidenceSemanticRole(values["semantic_role"])
        except ValueError as exc:
            raise EvidenceContractValidationError("semantic_role 不受支持") from exc
        return cls(
            semantic_role=role,
            reason_code=values["reason_code"],
            description=values["description"],
            capture_id=values.get("capture_id"),
            metadata=_mapping(values.get("metadata", {}), "metadata"),
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class AppEvidenceIndexEntry:
    """Evidence Index 中一条按采集时间稳定排序的 Manifest 引用。"""

    sequence_no: int
    capture_id: str
    evidence_manifest_id: str
    evidence_manifest_version: int
    semantic_role: AppEvidenceSemanticRole
    captured_at: datetime
    surface_phase: str | None = None
    exploration_work_id: str | None = None
    action_execution_id: str | None = None
    candidate_id: str | None = None
    page_identity: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = APP_EVIDENCE_INDEX_ENTRY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            require_text(self.capture_id, "capture_id")
            require_text(self.evidence_manifest_id, "evidence_manifest_id")
            require_aware_datetime(self.captured_at, "captured_at")
            for field_name in (
                "surface_phase",
                "exploration_work_id",
                "action_execution_id",
                "candidate_id",
                "page_identity",
            ):
                value = getattr(self, field_name)
                if value is not None:
                    require_text(value, field_name)
            metadata = freeze_json_mapping(self.metadata, "metadata")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        for field_name in ("sequence_no", "evidence_manifest_version"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise EvidenceContractValidationError(f"{field_name} 必须为正整数")
        if not isinstance(self.semantic_role, AppEvidenceSemanticRole):
            raise EvidenceContractValidationError("semantic_role 不受支持")
        if self.protocol_version != APP_EVIDENCE_INDEX_ENTRY_PROTOCOL_VERSION:
            raise EvidenceContractValidationError("AppEvidenceIndexEntry protocol_version 不受支持")
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "sequence_no": self.sequence_no,
            "capture_id": self.capture_id,
            "evidence_manifest_id": self.evidence_manifest_id,
            "evidence_manifest_version": self.evidence_manifest_version,
            "semantic_role": self.semantic_role.value,
            "surface_phase": self.surface_phase,
            "exploration_work_id": self.exploration_work_id,
            "action_execution_id": self.action_execution_id,
            "candidate_id": self.candidate_id,
            "page_identity": self.page_identity,
            "captured_at": self.captured_at.isoformat(),
            "metadata": thaw_json_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppEvidenceIndexEntry:
        values = _read(
            payload,
            known={
                "protocol_version",
                "sequence_no",
                "capture_id",
                "evidence_manifest_id",
                "evidence_manifest_version",
                "semantic_role",
                "surface_phase",
                "exploration_work_id",
                "action_execution_id",
                "candidate_id",
                "page_identity",
                "captured_at",
                "metadata",
            },
            required={
                "protocol_version",
                "sequence_no",
                "capture_id",
                "evidence_manifest_id",
                "evidence_manifest_version",
                "semantic_role",
                "captured_at",
            },
            field_name="AppEvidenceIndexEntry",
        )
        try:
            role = AppEvidenceSemanticRole(values["semantic_role"])
            captured_at = parse_aware_datetime(values["captured_at"], "captured_at")
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        return cls(
            sequence_no=values["sequence_no"],
            capture_id=values["capture_id"],
            evidence_manifest_id=values["evidence_manifest_id"],
            evidence_manifest_version=values["evidence_manifest_version"],
            semantic_role=role,
            surface_phase=values.get("surface_phase"),
            exploration_work_id=values.get("exploration_work_id"),
            action_execution_id=values.get("action_execution_id"),
            candidate_id=values.get("candidate_id"),
            page_identity=values.get("page_identity"),
            captured_at=captured_at,
            metadata=_mapping(values.get("metadata", {}), "metadata"),
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class AppEvidenceIndex:
    """每个 App Exploration Run 唯一且封存后不可变的证据索引。"""

    index_id: str
    task_id: str
    run_id: str
    build_id: str
    surface_session_id: str
    status: AppEvidenceIndexStatus
    entries: tuple[AppEvidenceIndexEntry, ...]
    created_at: datetime
    result_context_sha256: str | None = None
    outcome: AppExplorationResultOutcome | None = None
    terminal_reason_code: str | None = None
    evidence_gaps: tuple[AppEvidenceGap, ...] = ()
    content_sha256: str | None = None
    sealed_at: datetime | None = None
    protocol_version: str = APP_EVIDENCE_INDEX_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "index_id",
                "task_id",
                "run_id",
                "build_id",
                "surface_session_id",
            ):
                require_text(getattr(self, field_name), field_name)
            require_aware_datetime(self.created_at, "created_at")
            if self.terminal_reason_code is not None:
                require_text(self.terminal_reason_code, "terminal_reason_code")
            if self.content_sha256 is not None:
                require_text(self.content_sha256, "content_sha256")
            if self.sealed_at is not None:
                require_aware_datetime(self.sealed_at, "sealed_at")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        if not isinstance(self.status, AppEvidenceIndexStatus):
            raise EvidenceContractValidationError("status 不受支持")
        if self.result_context_sha256 is not None and not _is_sha256(self.result_context_sha256):
            raise EvidenceContractValidationError("result_context_sha256 必须为 SHA-256")
        entries = tuple(self.entries)
        if any(not isinstance(item, AppEvidenceIndexEntry) for item in entries):
            raise EvidenceContractValidationError("entries 类型不正确")
        expected_sequence = tuple(range(1, len(entries) + 1))
        if tuple(item.sequence_no for item in entries) != expected_sequence:
            raise EvidenceContractValidationError("sequence_no 必须从 1 连续递增")
        if len({item.capture_id for item in entries}) != len(entries):
            raise EvidenceContractValidationError("同一 Capture 不能重复进入 Evidence Index")
        gaps = tuple(self.evidence_gaps)
        if any(not isinstance(item, AppEvidenceGap) for item in gaps):
            raise EvidenceContractValidationError("evidence_gaps 类型不正确")
        expected_id = _index_id(task_id=self.task_id, run_id=self.run_id)
        if self.index_id != expected_id:
            raise EvidenceContractValidationError("index_id 与 task_id、run_id 不一致")
        if self.status is AppEvidenceIndexStatus.BUILDING:
            if (
                any(
                    value is not None
                    for value in (
                        self.outcome,
                        self.terminal_reason_code,
                        self.content_sha256,
                        self.sealed_at,
                    )
                )
                or gaps
            ):
                raise EvidenceContractValidationError("building Index 不能携带封存结果")
        else:
            if (
                not isinstance(self.outcome, AppExplorationResultOutcome)
                or self.terminal_reason_code is None
                or self.content_sha256 is None
                or self.sealed_at is None
            ):
                raise EvidenceContractValidationError("sealed Index 缺少完整封存字段")
            if self.content_sha256 != self.recompute_content_sha256():
                raise EvidenceContractValidationError("Evidence Index 哈希与内容不一致")
        if self.protocol_version != APP_EVIDENCE_INDEX_PROTOCOL_VERSION:
            raise EvidenceContractValidationError("AppEvidenceIndex protocol_version 不受支持")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "evidence_gaps", gaps)

    def recompute_content_sha256(self) -> str:
        """按固定条目顺序重算封存摘要。"""
        if self.outcome is None or self.terminal_reason_code is None:
            raise EvidenceContractValidationError("building Index 尚不能计算封存哈希")
        return _content_sha256(
            index=self,
            outcome=self.outcome,
            terminal_reason_code=self.terminal_reason_code,
            evidence_gaps=self.evidence_gaps,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "index_id": self.index_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "build_id": self.build_id,
            "surface_session_id": self.surface_session_id,
            "result_context_sha256": self.result_context_sha256,
            "status": self.status.value,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "terminal_reason_code": self.terminal_reason_code,
            "content_sha256": self.content_sha256,
            "evidence_gaps": [item.to_dict() for item in self.evidence_gaps],
            "entries": [item.to_dict() for item in self.entries],
            "sealed_at": self.sealed_at.isoformat() if self.sealed_at is not None else None,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppEvidenceIndex:
        values = _read(
            payload,
            known={
                "protocol_version",
                "index_id",
                "task_id",
                "run_id",
                "build_id",
                "surface_session_id",
                "result_context_sha256",
                "status",
                "outcome",
                "terminal_reason_code",
                "content_sha256",
                "evidence_gaps",
                "entries",
                "sealed_at",
                "created_at",
            },
            required={
                "protocol_version",
                "index_id",
                "task_id",
                "run_id",
                "build_id",
                "surface_session_id",
                "status",
                "entries",
                "created_at",
            },
            field_name="AppEvidenceIndex",
            # Index 是最多约百条独立受限证据条目的聚合读模型，不能沿用
            # 单条跨模块消息的 256 KB 上限，否则长时 App 探索会在封口时
            # 无法重放已经成功持久化的证据账本。
            max_bytes=_MAX_APP_EVIDENCE_INDEX_PAYLOAD_BYTES,
        )
        try:
            status = AppEvidenceIndexStatus(values["status"])
            outcome = (
                AppExplorationResultOutcome(values["outcome"])
                if values.get("outcome") is not None
                else None
            )
            created_at = parse_aware_datetime(values["created_at"], "created_at")
            sealed_at = (
                parse_aware_datetime(values["sealed_at"], "sealed_at")
                if values.get("sealed_at") is not None
                else None
            )
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        return cls(
            index_id=values["index_id"],
            task_id=values["task_id"],
            run_id=values["run_id"],
            build_id=values["build_id"],
            surface_session_id=values["surface_session_id"],
            result_context_sha256=values.get("result_context_sha256"),
            status=status,
            outcome=outcome,
            terminal_reason_code=values.get("terminal_reason_code"),
            content_sha256=values.get("content_sha256"),
            evidence_gaps=tuple(
                AppEvidenceGap.from_dict(_mapping(item, "evidence_gaps[]"))
                for item in _sequence(values.get("evidence_gaps", ()), "evidence_gaps")
            ),
            entries=tuple(
                AppEvidenceIndexEntry.from_dict(_mapping(item, "entries[]"))
                for item in _sequence(values["entries"], "entries")
            ),
            sealed_at=sealed_at,
            created_at=created_at,
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class AppExplorationResult:
    """App 探索交给下游的封存结果。"""

    task_id: str
    run_id: str
    surface_session_id: str
    outcome: AppExplorationResultOutcome
    terminal_reason_code: str
    evidence_index_id: str
    evidence_index_hash: str
    coverage_summary: Mapping[str, Any]
    evidence_gaps: tuple[AppEvidenceGap, ...]
    started_at: datetime
    completed_at: datetime
    result_sha256: str | None = None
    protocol_version: str = APP_EXPLORATION_RESULT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "task_id",
                "run_id",
                "surface_session_id",
                "terminal_reason_code",
                "evidence_index_id",
                "evidence_index_hash",
            ):
                require_text(getattr(self, field_name), field_name)
            require_aware_datetime(self.started_at, "started_at")
            require_aware_datetime(self.completed_at, "completed_at")
            coverage = freeze_json_mapping(self.coverage_summary, "coverage_summary")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        if not isinstance(self.outcome, AppExplorationResultOutcome):
            raise EvidenceContractValidationError("outcome 不受支持")
        if self.completed_at < self.started_at:
            raise EvidenceContractValidationError("completed_at 不能早于 started_at")
        if not _is_sha256(self.evidence_index_hash):
            raise EvidenceContractValidationError("evidence_index_hash 必须为 SHA-256")
        gaps = tuple(self.evidence_gaps)
        if any(not isinstance(item, AppEvidenceGap) for item in gaps):
            raise EvidenceContractValidationError("evidence_gaps 类型不正确")
        if self.protocol_version not in _SUPPORTED_APP_EXPLORATION_RESULT_PROTOCOL_VERSIONS:
            raise EvidenceContractValidationError("AppExplorationResult protocol_version 不受支持")
        object.__setattr__(self, "coverage_summary", coverage)
        object.__setattr__(self, "evidence_gaps", gaps)
        expected_hash = self.recompute_result_sha256()
        if self.result_sha256 is not None and self.result_sha256 != expected_hash:
            raise EvidenceContractValidationError("result_sha256 与探索结果内容不一致")
        object.__setattr__(self, "result_sha256", expected_hash)

    def recompute_result_sha256(self) -> str:
        """重算探索结果摘要，供持久化与重放校验。"""
        return "sha256:" + stable_protocol_hash(self._hash_payload())

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "surface_session_id": self.surface_session_id,
            "outcome": self.outcome.value,
            "terminal_reason_code": self.terminal_reason_code,
            "evidence_index_id": self.evidence_index_id,
            "evidence_index_hash": self.evidence_index_hash,
            "coverage_summary": thaw_json_mapping(self.coverage_summary),
            "evidence_gaps": [item.to_dict() for item in self.evidence_gaps],
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._hash_payload(),
            "result_sha256": self.result_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppExplorationResult:
        common_fields = {
            "protocol_version",
            "task_id",
            "run_id",
            "surface_session_id",
            "outcome",
            "terminal_reason_code",
            "evidence_index_id",
            "evidence_index_hash",
            "coverage_summary",
            "evidence_gaps",
            "started_at",
            "completed_at",
        }
        values = _read(
            payload,
            known=common_fields | {"result_sha256"},
            required=common_fields,
            field_name="AppExplorationResult",
        )
        protocol_version = values["protocol_version"]
        if (
            protocol_version == APP_EXPLORATION_RESULT_PROTOCOL_VERSION
            and values.get("result_sha256") is None
        ):
            raise EvidenceContractValidationError("AppExplorationResult v2 缺少 result_sha256")
        try:
            outcome = AppExplorationResultOutcome(values["outcome"])
            started_at = parse_aware_datetime(values["started_at"], "started_at")
            completed_at = parse_aware_datetime(values["completed_at"], "completed_at")
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        return cls(
            task_id=values["task_id"],
            run_id=values["run_id"],
            surface_session_id=values["surface_session_id"],
            outcome=outcome,
            terminal_reason_code=values["terminal_reason_code"],
            evidence_index_id=values["evidence_index_id"],
            evidence_index_hash=values["evidence_index_hash"],
            coverage_summary=_mapping(values["coverage_summary"], "coverage_summary"),
            evidence_gaps=tuple(
                AppEvidenceGap.from_dict(_mapping(item, "evidence_gaps[]"))
                for item in _sequence(values["evidence_gaps"], "evidence_gaps")
            ),
            started_at=started_at,
            completed_at=completed_at,
            result_sha256=values.get("result_sha256"),
            protocol_version=protocol_version,
        )


def build_app_evidence_index(
    *,
    task_id: str,
    run_id: str,
    build_id: str,
    surface_session_id: str,
    entries: Sequence[AppEvidenceIndexEntry],
    created_at: datetime,
    result_context_sha256: str | None = None,
) -> AppEvidenceIndex:
    """稳定排序证据并创建 building Index。"""
    ordered = sorted(
        tuple(entries),
        key=lambda item: (
            item.captured_at,
            item.capture_id,
            item.semantic_role.value,
        ),
    )
    sequenced = tuple(
        replace(item, sequence_no=position) for position, item in enumerate(ordered, start=1)
    )
    return AppEvidenceIndex(
        index_id=_index_id(task_id=task_id, run_id=run_id),
        task_id=task_id,
        run_id=run_id,
        build_id=build_id,
        surface_session_id=surface_session_id,
        status=AppEvidenceIndexStatus.BUILDING,
        entries=sequenced,
        created_at=created_at,
        result_context_sha256=result_context_sha256,
    )


def seal_app_evidence_index(
    index: AppEvidenceIndex,
    *,
    outcome: AppExplorationResultOutcome,
    terminal_reason_code: str,
    sealed_at: datetime,
    evidence_gaps: Sequence[AppEvidenceGap] = (),
) -> AppEvidenceIndex:
    """原样重放相同封存请求；拒绝修改已封存内容。"""
    if not isinstance(index, AppEvidenceIndex):
        raise TypeError("index 必须为 AppEvidenceIndex")
    if index.status is AppEvidenceIndexStatus.BUILDING and index.result_context_sha256 is None:
        raise EvidenceContractValidationError("Evidence Index 封存前必须绑定探索结果上下文")
    gaps = tuple(evidence_gaps)
    if outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED and gaps:
        raise EvidenceContractValidationError("exploration_completed 不能携带证据缺口")
    if outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED and not any(
        entry.semantic_role is AppEvidenceSemanticRole.FINAL_OBSERVATION for entry in index.entries
    ):
        raise EvidenceContractValidationError("exploration_completed 必须包含 Final Observation")
    gap_identities: set[tuple[str | None, str, str]] = set()
    for gap in gaps:
        if gap.capture_id is None and gap.metadata.get("scope") != "run":
            raise EvidenceContractValidationError("无 capture_id 的证据缺口必须显式声明 run scope")
        identity = (gap.capture_id, gap.semantic_role.value, gap.reason_code)
        if identity in gap_identities:
            raise EvidenceContractValidationError("Evidence Index 不能携带重复证据缺口")
        gap_identities.add(identity)
    if index.status is AppEvidenceIndexStatus.SEALED:
        if (
            index.outcome is outcome
            and index.terminal_reason_code == terminal_reason_code
            and index.evidence_gaps == gaps
        ):
            return index
        raise EvidenceContractValidationError("Evidence Index 已经封存，禁止修改内容")
    content_sha256 = _content_sha256(
        index=index,
        outcome=outcome,
        terminal_reason_code=terminal_reason_code,
        evidence_gaps=gaps,
    )
    return replace(
        index,
        status=AppEvidenceIndexStatus.SEALED,
        outcome=outcome,
        terminal_reason_code=terminal_reason_code,
        evidence_gaps=gaps,
        content_sha256=content_sha256,
        sealed_at=sealed_at,
    )


def _content_sha256(
    *,
    index: AppEvidenceIndex,
    outcome: AppExplorationResultOutcome,
    terminal_reason_code: str,
    evidence_gaps: Sequence[AppEvidenceGap],
) -> str:
    payload: dict[str, Any] = {
        "protocol_version": index.protocol_version,
        "index_id": index.index_id,
        "task_id": index.task_id,
        "run_id": index.run_id,
        "build_id": index.build_id,
        "surface_session_id": index.surface_session_id,
        "outcome": outcome.value,
        "terminal_reason_code": terminal_reason_code,
        "entries": [item.to_dict() for item in index.entries],
        "evidence_gaps": [item.to_dict() for item in evidence_gaps],
    }
    # 历史 v1 Index 没有结果上下文字段，缺失时必须保留原哈希算法。
    if index.result_context_sha256 is not None:
        payload["result_context_sha256"] = index.result_context_sha256
    digest = stable_protocol_hash(payload)
    return f"sha256:{digest}"


def app_exploration_result_context_sha256(
    *,
    coverage_summary: Mapping[str, Any],
    started_at: datetime,
) -> str:
    """把可恢复 Result 的覆盖快照与开始时间绑定到 Evidence Index。"""

    try:
        coverage = freeze_json_mapping(coverage_summary, "coverage_summary")
        require_aware_datetime(started_at, "started_at")
    except ProtocolContractError as exc:
        raise EvidenceContractValidationError(str(exc)) from exc
    return "sha256:" + stable_protocol_hash(
        {
            "coverage_summary": thaw_json_mapping(coverage),
            "started_at": started_at.isoformat(),
        }
    )


def _index_id(*, task_id: str, run_id: str) -> str:
    return "app-evidence-index:" + stable_protocol_hash({"task_id": task_id, "run_id": run_id})


def _is_sha256(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


def _read(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str],
    field_name: str,
    max_bytes: int = MAX_PROTOCOL_PAYLOAD_BYTES,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(required),
            field_name=field_name,
            max_bytes=max_bytes,
        )
    except ProtocolContractError as exc:
        raise EvidenceContractValidationError(str(exc)) from exc


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    try:
        return require_mapping(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceContractValidationError(str(exc)) from exc


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    try:
        return require_sequence(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceContractValidationError(str(exc)) from exc
