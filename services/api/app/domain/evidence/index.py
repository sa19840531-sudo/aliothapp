"""探索级 Evidence Index：封存后只读，不得回写或派生补采。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.protocol import (
    ProtocolContractError,
    optional_text,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_text,
    stable_protocol_hash,
)

EVIDENCE_INDEX_PROTOCOL_VERSION = "evidence-index.v2"


class EvidenceIndexValidationError(ValueError):
    """Evidence Index 身份或封存状态不满足跨模块协议。"""


class ExplorationEvidenceStatus(StrEnum):
    """#gate 探索证据链终态；不是规则执行结论。"""

    EXPLORATION_COMPLETED = "exploration_completed"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"


# 暂不截止：未封存 Index，只出现在 RunResult，禁止写入 qc_evidence_index。
NOT_YET_CUTOFF_STATUS = "not_yet_cutoff"


class EvidenceGapCode(StrEnum):
    """Persistence Gate 盘点缺口类别。"""

    PENDING_MANIFEST = "pending_manifest"
    UNBOUND_ARTIFACT = "unbound_artifact"
    UNPAIRED_BEFORE_AFTER = "unpaired_before_after"
    PENDING_ACTION = "pending_action"
    NO_CAPTURES = "no_captures"
    INVENTORY_FAILED = "inventory_failed"
    NONTERMINAL_WORK = "nonterminal_work"
    IN_FLIGHT_WORK = "in_flight_work"
    MISSING_UI_TREE = "missing_ui_tree"
    MISSING_ACTION_LOG = "missing_action_log"
    WRITE_TIMEOUT = "write_timeout"
    IN_FLIGHT_COMMAND = "in_flight_command"
    PENDING_OBJECT_WRITE = "pending_object_write"
    CORRUPT_ARTIFACT = "corrupt_artifact"
    CROSS_TASK_BINDING = "cross_task_binding"
    MISSING_BEFORE_CAPTURE = "missing_before_capture"
    MISSING_AFTER_CAPTURE = "missing_after_capture"
    MISSING_SCREENSHOT = "missing_screenshot"
    MISSING_TERMINAL_OBSERVATION = "missing_terminal_observation"


# 在途原写入／排空：有限重试期间可继续等待；重试耗尽后必须出闸，禁止永久 not_yet_cutoff。
RETRYABLE_EVIDENCE_GAP_CODES = frozenset(
    {
        EvidenceGapCode.PENDING_MANIFEST,
        EvidenceGapCode.PENDING_ACTION,
        EvidenceGapCode.IN_FLIGHT_WORK,
        EvidenceGapCode.IN_FLIGHT_COMMAND,
        EvidenceGapCode.PENDING_OBJECT_WRITE,
        EvidenceGapCode.UNBOUND_ARTIFACT,
        EvidenceGapCode.UNPAIRED_BEFORE_AFTER,
    }
)
TIMEOUT_EVIDENCE_GAP_CODES = frozenset(
    {
        EvidenceGapCode.PENDING_MANIFEST,
        EvidenceGapCode.PENDING_ACTION,
        EvidenceGapCode.PENDING_OBJECT_WRITE,
        EvidenceGapCode.UNBOUND_ARTIFACT,
        EvidenceGapCode.UNPAIRED_BEFORE_AFTER,
        EvidenceGapCode.IN_FLIGHT_WORK,
        EvidenceGapCode.IN_FLIGHT_COMMAND,
    }
)
INFLIGHT_DRAIN_GAP_CODES = frozenset(
    {
        EvidenceGapCode.IN_FLIGHT_WORK,
        EvidenceGapCode.IN_FLIGHT_COMMAND,
    }
)


@dataclass(frozen=True, kw_only=True)
class EvidenceGapItem:
    """单条证据缺口，供探索结果只读引用。"""

    code: EvidenceGapCode
    subject_id: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, EvidenceGapCode):
            raise EvidenceIndexValidationError("code 不受支持")
        try:
            require_text(self.subject_id, "subject_id")
            require_text(self.detail, "detail")
        except ProtocolContractError as exc:
            raise EvidenceIndexValidationError(str(exc)) from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "subject_id": self.subject_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceGapItem:
        values = _read(
            payload,
            known={"code", "subject_id", "detail"},
            required={"code", "subject_id", "detail"},
            field_name="EvidenceGapItem",
        )
        return cls(
            code=EvidenceGapCode(str(values["code"])),
            subject_id=values["subject_id"],
            detail=values["detail"],
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceCaptureLineage:
    """封存时固定的 Capture 谱系引用。"""

    capture_id: str
    role: str
    parent_capture_id: str | None = None
    action_execution_id: str | None = None

    def __post_init__(self) -> None:
        try:
            require_text(self.capture_id, "capture_id")
            require_text(self.role, "role")
            optional_text(self.parent_capture_id, "parent_capture_id")
            optional_text(self.action_execution_id, "action_execution_id")
        except ProtocolContractError as exc:
            raise EvidenceIndexValidationError(str(exc)) from exc

    def to_dict(self) -> dict[str, str | None]:
        return {
            "capture_id": self.capture_id,
            "role": self.role,
            "parent_capture_id": self.parent_capture_id,
            "action_execution_id": self.action_execution_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceCaptureLineage:
        values = _read(
            payload,
            known={
                "capture_id",
                "role",
                "parent_capture_id",
                "action_execution_id",
            },
            required={"capture_id", "role"},
            field_name="EvidenceCaptureLineage",
        )
        return cls(
            capture_id=str(values["capture_id"]),
            role=str(values["role"]),
            parent_capture_id=values.get("parent_capture_id"),
            action_execution_id=values.get("action_execution_id"),
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactIndexItem:
    """封存时固定的 Artifact 引用与 checksum。"""

    artifact_id: str
    artifact_type: str
    checksum: str
    capture_id: str

    def __post_init__(self) -> None:
        try:
            require_text(self.artifact_id, "artifact_id")
            require_text(self.artifact_type, "artifact_type")
            require_text(self.checksum, "checksum")
            require_text(self.capture_id, "capture_id")
        except ProtocolContractError as exc:
            raise EvidenceIndexValidationError(str(exc)) from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "checksum": self.checksum,
            "capture_id": self.capture_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceArtifactIndexItem:
        values = _read(
            payload,
            known={"artifact_id", "artifact_type", "checksum", "capture_id"},
            required={"artifact_id", "artifact_type", "checksum", "capture_id"},
            field_name="EvidenceArtifactIndexItem",
        )
        return cls(
            artifact_id=str(values["artifact_id"]),
            artifact_type=str(values["artifact_type"]),
            checksum=str(values["checksum"]),
            capture_id=str(values["capture_id"]),
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceActionLogItem:
    """封存时固定的动作日志引用。"""

    action_execution_id: str
    artifact_id: str
    checksum: str
    capture_id: str

    def __post_init__(self) -> None:
        try:
            require_text(self.action_execution_id, "action_execution_id")
            require_text(self.artifact_id, "artifact_id")
            require_text(self.checksum, "checksum")
            require_text(self.capture_id, "capture_id")
        except ProtocolContractError as exc:
            raise EvidenceIndexValidationError(str(exc)) from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "action_execution_id": self.action_execution_id,
            "artifact_id": self.artifact_id,
            "checksum": self.checksum,
            "capture_id": self.capture_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceActionLogItem:
        values = _read(
            payload,
            known={"action_execution_id", "artifact_id", "checksum", "capture_id"},
            required={"action_execution_id", "artifact_id", "checksum", "capture_id"},
            field_name="EvidenceActionLogItem",
        )
        return cls(
            action_execution_id=str(values["action_execution_id"]),
            artifact_id=str(values["artifact_id"]),
            checksum=str(values["checksum"]),
            capture_id=str(values["capture_id"]),
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceIndexReference:
    """一次 App 探索会话的只读证据索引。"""

    index_id: str
    task_id: str
    session_id: str
    status: ExplorationEvidenceStatus
    manifest_ids: tuple[str, ...]
    capture_ids: tuple[str, ...]
    evidence_gap: tuple[EvidenceGapItem, ...]
    index_hash: str
    sealed_at: datetime
    protocol_version: str = EVIDENCE_INDEX_PROTOCOL_VERSION
    page_timeline: tuple[str, ...] = ()
    capture_lineage: tuple[EvidenceCaptureLineage, ...] = ()
    artifacts: tuple[EvidenceArtifactIndexItem, ...] = ()
    action_logs: tuple[EvidenceActionLogItem, ...] = ()

    def __post_init__(self) -> None:
        try:
            for field_name in ("index_id", "task_id", "session_id", "index_hash"):
                require_text(getattr(self, field_name), field_name)
            require_aware_datetime(self.sealed_at, "sealed_at")
        except ProtocolContractError as exc:
            raise EvidenceIndexValidationError(str(exc)) from exc
        if not isinstance(self.status, ExplorationEvidenceStatus):
            raise EvidenceIndexValidationError("status 不受支持")
        if self.protocol_version != EVIDENCE_INDEX_PROTOCOL_VERSION:
            raise EvidenceIndexValidationError("EvidenceIndex protocol_version 不受支持")
        manifest_ids = _unique_text_tuple(self.manifest_ids, "manifest_ids")
        capture_ids = _unique_text_tuple(self.capture_ids, "capture_ids")
        gaps = tuple(self.evidence_gap)
        timeline = _unique_allow_repeat_text_tuple(self.page_timeline, "page_timeline")
        lineage = tuple(self.capture_lineage)
        artifacts = tuple(self.artifacts)
        action_logs = tuple(self.action_logs)
        if any(not isinstance(item, EvidenceGapItem) for item in gaps):
            raise EvidenceIndexValidationError("evidence_gap 必须由 EvidenceGapItem 组成")
        if any(not isinstance(item, EvidenceCaptureLineage) for item in lineage):
            raise EvidenceIndexValidationError("capture_lineage 必须由 EvidenceCaptureLineage 组成")
        if any(not isinstance(item, EvidenceArtifactIndexItem) for item in artifacts):
            raise EvidenceIndexValidationError("artifacts 必须由 EvidenceArtifactIndexItem 组成")
        if any(not isinstance(item, EvidenceActionLogItem) for item in action_logs):
            raise EvidenceIndexValidationError("action_logs 必须由 EvidenceActionLogItem 组成")
        if (
            self.status is ExplorationEvidenceStatus.EXPLORATION_COMPLETED
            and gaps
        ):
            raise EvidenceIndexValidationError("exploration_completed 不得携带 evidence_gap")
        if (
            self.status is ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT
            and not gaps
        ):
            raise EvidenceIndexValidationError("evidence_insufficient 必须说明缺口")
        expected_hash = _index_hash(
            task_id=self.task_id,
            session_id=self.session_id,
            status=self.status,
            manifest_ids=manifest_ids,
            capture_ids=capture_ids,
            evidence_gap=gaps,
            page_timeline=timeline,
            capture_lineage=lineage,
            artifacts=artifacts,
            action_logs=action_logs,
        )
        if self.index_hash != expected_hash:
            raise EvidenceIndexValidationError("index_hash 与证据索引不一致")
        expected_id = _index_id(
            task_id=self.task_id,
            session_id=self.session_id,
            index_hash=self.index_hash,
        )
        if self.index_id != expected_id:
            raise EvidenceIndexValidationError("index_id 与任务、会话和索引哈希不一致")
        object.__setattr__(self, "manifest_ids", manifest_ids)
        object.__setattr__(self, "capture_ids", capture_ids)
        object.__setattr__(self, "evidence_gap", gaps)
        object.__setattr__(self, "page_timeline", timeline)
        object.__setattr__(self, "capture_lineage", lineage)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "action_logs", action_logs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "index_id": self.index_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "manifest_ids": list(self.manifest_ids),
            "capture_ids": list(self.capture_ids),
            "evidence_gap": [item.to_dict() for item in self.evidence_gap],
            "page_timeline": list(self.page_timeline),
            "capture_lineage": [item.to_dict() for item in self.capture_lineage],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "action_logs": [item.to_dict() for item in self.action_logs],
            "index_hash": self.index_hash,
            "sealed_at": self.sealed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceIndexReference:
        values = _read(
            payload,
            known={
                "protocol_version",
                "index_id",
                "task_id",
                "session_id",
                "status",
                "manifest_ids",
                "capture_ids",
                "evidence_gap",
                "page_timeline",
                "capture_lineage",
                "artifacts",
                "action_logs",
                "index_hash",
                "sealed_at",
            },
            required={
                "protocol_version",
                "index_id",
                "task_id",
                "session_id",
                "status",
                "manifest_ids",
                "capture_ids",
                "evidence_gap",
                "index_hash",
                "sealed_at",
            },
            field_name="EvidenceIndexReference",
        )
        sealed_at = values["sealed_at"]
        if isinstance(sealed_at, str):
            try:
                sealed_at = parse_aware_datetime(sealed_at, "sealed_at")
            except ProtocolContractError as exc:
                raise EvidenceIndexValidationError(str(exc)) from exc
        return cls(
            index_id=values["index_id"],
            task_id=values["task_id"],
            session_id=values["session_id"],
            status=ExplorationEvidenceStatus(str(values["status"])),
            manifest_ids=tuple(str(item) for item in values["manifest_ids"]),
            capture_ids=tuple(str(item) for item in values["capture_ids"]),
            evidence_gap=tuple(
                EvidenceGapItem.from_dict(item) for item in values["evidence_gap"]
            ),
            page_timeline=tuple(str(item) for item in values.get("page_timeline") or ()),
            capture_lineage=tuple(
                EvidenceCaptureLineage.from_dict(item)
                for item in values.get("capture_lineage") or ()
            ),
            artifacts=tuple(
                EvidenceArtifactIndexItem.from_dict(item)
                for item in values.get("artifacts") or ()
            ),
            action_logs=tuple(
                EvidenceActionLogItem.from_dict(item)
                for item in values.get("action_logs") or ()
            ),
            index_hash=values["index_hash"],
            sealed_at=sealed_at,
            protocol_version=values["protocol_version"],
        )


def build_evidence_index_reference(
    *,
    task_id: str,
    session_id: str,
    status: ExplorationEvidenceStatus,
    sealed_at: datetime,
    manifest_ids: Sequence[str] = (),
    capture_ids: Sequence[str] = (),
    evidence_gap: Sequence[EvidenceGapItem] = (),
    page_timeline: Sequence[str] = (),
    capture_lineage: Sequence[EvidenceCaptureLineage] = (),
    artifacts: Sequence[EvidenceArtifactIndexItem] = (),
    action_logs: Sequence[EvidenceActionLogItem] = (),
) -> EvidenceIndexReference:
    """从盘点结果生成可校验且幂等的 Evidence Index。"""
    manifests = _unique_text_tuple(manifest_ids, "manifest_ids")
    captures = _unique_text_tuple(capture_ids, "capture_ids")
    gaps = tuple(evidence_gap)
    timeline = _unique_allow_repeat_text_tuple(page_timeline, "page_timeline")
    lineage = tuple(capture_lineage)
    artifact_items = tuple(artifacts)
    logs = tuple(action_logs)
    index_hash = _index_hash(
        task_id=task_id,
        session_id=session_id,
        status=status,
        manifest_ids=manifests,
        capture_ids=captures,
        evidence_gap=gaps,
        page_timeline=timeline,
        capture_lineage=lineage,
        artifacts=artifact_items,
        action_logs=logs,
    )
    return EvidenceIndexReference(
        index_id=_index_id(
            task_id=task_id,
            session_id=session_id,
            index_hash=index_hash,
        ),
        task_id=task_id,
        session_id=session_id,
        status=status,
        manifest_ids=manifests,
        capture_ids=captures,
        evidence_gap=gaps,
        page_timeline=timeline,
        capture_lineage=lineage,
        artifacts=artifact_items,
        action_logs=logs,
        index_hash=index_hash,
        sealed_at=sealed_at,
    )


def _index_hash(
    *,
    task_id: str,
    session_id: str,
    status: ExplorationEvidenceStatus,
    manifest_ids: tuple[str, ...],
    capture_ids: tuple[str, ...],
    evidence_gap: tuple[EvidenceGapItem, ...],
    page_timeline: tuple[str, ...] = (),
    capture_lineage: tuple[EvidenceCaptureLineage, ...] = (),
    artifacts: tuple[EvidenceArtifactIndexItem, ...] = (),
    action_logs: tuple[EvidenceActionLogItem, ...] = (),
) -> str:
    return stable_protocol_hash(
        {
            "task_id": task_id,
            "session_id": session_id,
            "status": status.value,
            "manifest_ids": list(manifest_ids),
            "capture_ids": list(capture_ids),
            "evidence_gap": [item.to_dict() for item in evidence_gap],
            "page_timeline": list(page_timeline),
            "capture_lineage": [item.to_dict() for item in capture_lineage],
            "artifacts": [item.to_dict() for item in artifacts],
            "action_logs": [item.to_dict() for item in action_logs],
        }
    )


def _index_id(*, task_id: str, session_id: str, index_hash: str) -> str:
    return "evidence-index:" + stable_protocol_hash(
        {
            "task_id": task_id,
            "session_id": session_id,
            "index_hash": index_hash,
        }
    )


def _unique_text_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    items = tuple(str(item) for item in values)
    if any(not item.strip() for item in items):
        raise EvidenceIndexValidationError(f"{field_name} 不能包含空值")
    if len(set(items)) != len(items):
        raise EvidenceIndexValidationError(f"{field_name} 不能包含重复项")
    return items


def _unique_allow_repeat_text_tuple(
    values: Sequence[str], field_name: str
) -> tuple[str, ...]:
    items = tuple(str(item) for item in values)
    if any(not item.strip() for item in items):
        raise EvidenceIndexValidationError(f"{field_name} 不能包含空值")
    return items


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
        raise EvidenceIndexValidationError(str(exc)) from exc
