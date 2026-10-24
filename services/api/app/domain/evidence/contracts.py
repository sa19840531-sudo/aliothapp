"""Capture 提交到 Inspection 可消费之间的不可变证据合同。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

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

ARTIFACT_REFERENCE_PROTOCOL_VERSION = "artifact-reference.v1"
EVIDENCE_MANIFEST_PROTOCOL_VERSION = "evidence-manifest.v1"
CAPTURE_COMMITTED_PROTOCOL_VERSION = "capture-committed.v1"
EVIDENCE_BUNDLE_READY_PROTOCOL_VERSION = "evidence-bundle-ready.v1"


class EvidenceContractValidationError(ValueError):
    """证据身份、内容校验和或质量状态不满足跨模块协议。"""


class EvidenceQualityStatus(StrEnum):
    """Evidence Pipeline 对证据包的质量门禁终态。"""

    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, kw_only=True)
class ArtifactReference:
    """对象存储中一份不可变附件的可校验引用。"""

    artifact_id: str
    artifact_type: str
    role: str
    storage_path: str
    checksum: str
    size_bytes: int
    content_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = ARTIFACT_REFERENCE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "artifact_id",
                "artifact_type",
                "role",
                "storage_path",
                "checksum",
            ):
                require_text(getattr(self, field_name), field_name)
            if self.content_type is not None:
                require_text(self.content_type, "content_type")
            metadata = freeze_json_mapping(self.metadata, "metadata")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise EvidenceContractValidationError("size_bytes 必须为整数")
        if self.size_bytes < 0:
            raise EvidenceContractValidationError("size_bytes 不能小于 0")
        if self.protocol_version != ARTIFACT_REFERENCE_PROTOCOL_VERSION:
            raise EvidenceContractValidationError("ArtifactReference protocol_version 不受支持")
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "role": self.role,
            "storage_path": self.storage_path,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "metadata": thaw_json_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ArtifactReference:
        values = _read(
            payload,
            known={
                "protocol_version",
                "artifact_id",
                "artifact_type",
                "role",
                "storage_path",
                "checksum",
                "size_bytes",
                "content_type",
                "metadata",
            },
            required={
                "protocol_version",
                "artifact_id",
                "artifact_type",
                "role",
                "storage_path",
                "checksum",
                "size_bytes",
            },
            field_name="ArtifactReference",
        )
        return cls(
            artifact_id=values["artifact_id"],
            artifact_type=values["artifact_type"],
            role=values["role"],
            storage_path=values["storage_path"],
            checksum=values["checksum"],
            size_bytes=values["size_bytes"],
            content_type=values.get("content_type"),
            metadata=_mapping(values.get("metadata", {}), "metadata"),
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceManifestReference:
    """一次 Capture 对应的版本化附件清单与质量门禁摘要。"""

    manifest_id: str
    manifest_version: int
    task_id: str
    session_id: str
    capture_id: str
    artifacts: tuple[ArtifactReference, ...]
    manifest_hash: str
    quality_status: EvidenceQualityStatus
    created_at: datetime
    quality_reason: str | None = None
    observation_packet_ids: tuple[str, ...] = ()
    protocol_version: str = EVIDENCE_MANIFEST_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "manifest_id",
                "task_id",
                "session_id",
                "capture_id",
                "manifest_hash",
            ):
                require_text(getattr(self, field_name), field_name)
            if self.quality_reason is not None:
                require_text(self.quality_reason, "quality_reason")
            require_aware_datetime(self.created_at, "created_at")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        if (
            isinstance(self.manifest_version, bool)
            or not isinstance(self.manifest_version, int)
            or self.manifest_version <= 0
        ):
            raise EvidenceContractValidationError("manifest_version 必须为正整数")
        artifacts = tuple(self.artifacts)
        if not artifacts or any(not isinstance(item, ArtifactReference) for item in artifacts):
            raise EvidenceContractValidationError("artifacts 必须由至少一个附件引用组成")
        artifact_ids = tuple(item.artifact_id for item in artifacts)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise EvidenceContractValidationError("artifacts 不能包含重复 artifact_id")
        packet_ids = _unique_text_tuple(
            self.observation_packet_ids,
            "observation_packet_ids",
        )
        if not isinstance(self.quality_status, EvidenceQualityStatus):
            raise EvidenceContractValidationError("quality_status 不受支持")
        if self.quality_status is EvidenceQualityStatus.FAIL and self.quality_reason is None:
            raise EvidenceContractValidationError("质量门禁失败必须提供 quality_reason")
        if self.protocol_version != EVIDENCE_MANIFEST_PROTOCOL_VERSION:
            raise EvidenceContractValidationError(
                "EvidenceManifestReference protocol_version 不受支持"
            )
        expected_hash = _manifest_hash(
            task_id=self.task_id,
            session_id=self.session_id,
            capture_id=self.capture_id,
            manifest_version=self.manifest_version,
            artifacts=artifacts,
            observation_packet_ids=packet_ids,
        )
        if self.manifest_hash != expected_hash:
            raise EvidenceContractValidationError("manifest_hash 与证据清单不一致")
        expected_id = _manifest_id(
            capture_id=self.capture_id,
            manifest_version=self.manifest_version,
            manifest_hash=self.manifest_hash,
        )
        if self.manifest_id != expected_id:
            raise EvidenceContractValidationError("manifest_id 与 Capture、版本和清单哈希不一致")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "observation_packet_ids", packet_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "capture_id": self.capture_id,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "manifest_hash": self.manifest_hash,
            "quality_status": self.quality_status.value,
            "quality_reason": self.quality_reason,
            "observation_packet_ids": list(self.observation_packet_ids),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceManifestReference:
        values = _read(
            payload,
            known={
                "protocol_version",
                "manifest_id",
                "manifest_version",
                "task_id",
                "session_id",
                "capture_id",
                "artifacts",
                "manifest_hash",
                "quality_status",
                "quality_reason",
                "observation_packet_ids",
                "created_at",
            },
            required={
                "protocol_version",
                "manifest_id",
                "manifest_version",
                "task_id",
                "session_id",
                "capture_id",
                "artifacts",
                "manifest_hash",
                "quality_status",
                "created_at",
            },
            field_name="EvidenceManifestReference",
        )
        try:
            quality_status = EvidenceQualityStatus(values["quality_status"])
            created_at = parse_aware_datetime(values["created_at"], "created_at")
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        return cls(
            manifest_id=values["manifest_id"],
            manifest_version=values["manifest_version"],
            task_id=values["task_id"],
            session_id=values["session_id"],
            capture_id=values["capture_id"],
            artifacts=tuple(
                ArtifactReference.from_dict(_mapping(item, "artifacts[]"))
                for item in _sequence(values["artifacts"], "artifacts")
            ),
            manifest_hash=values["manifest_hash"],
            quality_status=quality_status,
            quality_reason=values.get("quality_reason"),
            observation_packet_ids=_sequence(
                values.get("observation_packet_ids", ()),
                "observation_packet_ids",
            ),
            created_at=created_at,
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class CaptureCommitted:
    """Capture 与最小证据清单已原子提交的持久化事件。"""

    event_id: str
    task_id: str
    session_id: str
    capture_id: str
    manifest_id: str
    manifest_version: int
    manifest_hash: str
    occurred_at: datetime
    protocol_version: str = CAPTURE_COMMITTED_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_event_identity(
            event_id=self.event_id,
            event_type="capture-committed",
            task_id=self.task_id,
            capture_id=self.capture_id,
            manifest_id=self.manifest_id,
            manifest_version=self.manifest_version,
            manifest_hash=self.manifest_hash,
        )
        try:
            require_text(self.session_id, "session_id")
            require_aware_datetime(self.occurred_at, "occurred_at")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        if self.protocol_version != CAPTURE_COMMITTED_PROTOCOL_VERSION:
            raise EvidenceContractValidationError("CaptureCommitted protocol_version 不受支持")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "capture_id": self.capture_id,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "occurred_at": self.occurred_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CaptureCommitted:
        values = _read(
            payload,
            known={
                "protocol_version",
                "event_id",
                "task_id",
                "session_id",
                "capture_id",
                "manifest_id",
                "manifest_version",
                "manifest_hash",
                "occurred_at",
            },
            required={
                "protocol_version",
                "event_id",
                "task_id",
                "session_id",
                "capture_id",
                "manifest_id",
                "manifest_version",
                "manifest_hash",
                "occurred_at",
            },
            field_name="CaptureCommitted",
        )
        try:
            occurred_at = parse_aware_datetime(values["occurred_at"], "occurred_at")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        return cls(
            event_id=values["event_id"],
            task_id=values["task_id"],
            session_id=values["session_id"],
            capture_id=values["capture_id"],
            manifest_id=values["manifest_id"],
            manifest_version=values["manifest_version"],
            manifest_hash=values["manifest_hash"],
            occurred_at=occurred_at,
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceBundleReady:
    """证据清单通过质量门禁、可以生成 Inspection Work Item 的事件。"""

    event_id: str
    task_id: str
    capture_id: str
    manifest_id: str
    manifest_version: int
    manifest_hash: str
    artifact_ids: tuple[str, ...]
    observation_packet_ids: tuple[str, ...]
    occurred_at: datetime
    protocol_version: str = EVIDENCE_BUNDLE_READY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_event_identity(
            event_id=self.event_id,
            event_type="evidence-bundle-ready",
            task_id=self.task_id,
            capture_id=self.capture_id,
            manifest_id=self.manifest_id,
            manifest_version=self.manifest_version,
            manifest_hash=self.manifest_hash,
        )
        artifact_ids = _unique_text_tuple(self.artifact_ids, "artifact_ids")
        if not artifact_ids:
            raise EvidenceContractValidationError("artifact_ids 不能为空")
        packet_ids = _unique_text_tuple(
            self.observation_packet_ids,
            "observation_packet_ids",
        )
        try:
            require_aware_datetime(self.occurred_at, "occurred_at")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        if self.protocol_version != EVIDENCE_BUNDLE_READY_PROTOCOL_VERSION:
            raise EvidenceContractValidationError("EvidenceBundleReady protocol_version 不受支持")
        object.__setattr__(self, "artifact_ids", artifact_ids)
        object.__setattr__(self, "observation_packet_ids", packet_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "capture_id": self.capture_id,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "artifact_ids": list(self.artifact_ids),
            "observation_packet_ids": list(self.observation_packet_ids),
            "occurred_at": self.occurred_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceBundleReady:
        values = _read(
            payload,
            known={
                "protocol_version",
                "event_id",
                "task_id",
                "capture_id",
                "manifest_id",
                "manifest_version",
                "manifest_hash",
                "artifact_ids",
                "observation_packet_ids",
                "occurred_at",
            },
            required={
                "protocol_version",
                "event_id",
                "task_id",
                "capture_id",
                "manifest_id",
                "manifest_version",
                "manifest_hash",
                "artifact_ids",
                "observation_packet_ids",
                "occurred_at",
            },
            field_name="EvidenceBundleReady",
        )
        try:
            occurred_at = parse_aware_datetime(values["occurred_at"], "occurred_at")
        except ProtocolContractError as exc:
            raise EvidenceContractValidationError(str(exc)) from exc
        return cls(
            event_id=values["event_id"],
            task_id=values["task_id"],
            capture_id=values["capture_id"],
            manifest_id=values["manifest_id"],
            manifest_version=values["manifest_version"],
            manifest_hash=values["manifest_hash"],
            artifact_ids=_sequence(values["artifact_ids"], "artifact_ids"),
            observation_packet_ids=_sequence(
                values["observation_packet_ids"],
                "observation_packet_ids",
            ),
            occurred_at=occurred_at,
            protocol_version=values["protocol_version"],
        )


def build_evidence_manifest_reference(
    *,
    task_id: str,
    session_id: str,
    capture_id: str,
    manifest_version: int,
    artifacts: Sequence[ArtifactReference],
    quality_status: EvidenceQualityStatus,
    created_at: datetime,
    quality_reason: str | None = None,
    observation_packet_ids: Sequence[str] = (),
) -> EvidenceManifestReference:
    """从附件身份生成可校验且可版本化的 Evidence Manifest。"""
    artifact_tuple = tuple(artifacts)
    packet_tuple = tuple(observation_packet_ids)
    manifest_hash = _manifest_hash(
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        manifest_version=manifest_version,
        artifacts=artifact_tuple,
        observation_packet_ids=packet_tuple,
    )
    return EvidenceManifestReference(
        manifest_id=_manifest_id(
            capture_id=capture_id,
            manifest_version=manifest_version,
            manifest_hash=manifest_hash,
        ),
        manifest_version=manifest_version,
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        artifacts=artifact_tuple,
        manifest_hash=manifest_hash,
        quality_status=quality_status,
        quality_reason=quality_reason,
        observation_packet_ids=packet_tuple,
        created_at=created_at,
    )


def build_capture_committed(
    manifest: EvidenceManifestReference,
    *,
    occurred_at: datetime,
) -> CaptureCommitted:
    """从已提交清单生成幂等 CaptureCommitted 事件。"""
    event_id = _event_id(
        event_type="capture-committed",
        task_id=manifest.task_id,
        capture_id=manifest.capture_id,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version,
        manifest_hash=manifest.manifest_hash,
    )
    return CaptureCommitted(
        event_id=event_id,
        task_id=manifest.task_id,
        session_id=manifest.session_id,
        capture_id=manifest.capture_id,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version,
        manifest_hash=manifest.manifest_hash,
        occurred_at=occurred_at,
    )


def build_evidence_bundle_ready(
    manifest: EvidenceManifestReference,
    *,
    occurred_at: datetime,
) -> EvidenceBundleReady:
    """只允许质量门禁通过的 Manifest 进入 Inspection。"""
    if manifest.quality_status is not EvidenceQualityStatus.PASS:
        raise EvidenceContractValidationError("只有 quality_status=pass 才能发布证据包就绪事件")
    event_id = _event_id(
        event_type="evidence-bundle-ready",
        task_id=manifest.task_id,
        capture_id=manifest.capture_id,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version,
        manifest_hash=manifest.manifest_hash,
    )
    return EvidenceBundleReady(
        event_id=event_id,
        task_id=manifest.task_id,
        capture_id=manifest.capture_id,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version,
        manifest_hash=manifest.manifest_hash,
        artifact_ids=tuple(item.artifact_id for item in manifest.artifacts),
        observation_packet_ids=manifest.observation_packet_ids,
        occurred_at=occurred_at,
    )


def _manifest_hash(
    *,
    task_id: str,
    session_id: str,
    capture_id: str,
    manifest_version: int,
    artifacts: Sequence[ArtifactReference],
    observation_packet_ids: Sequence[str],
) -> str:
    return stable_protocol_hash(
        {
            "task_id": task_id,
            "session_id": session_id,
            "capture_id": capture_id,
            "manifest_version": manifest_version,
            "artifacts": [item.to_dict() for item in artifacts],
            "observation_packet_ids": list(observation_packet_ids),
        }
    )


def _manifest_id(*, capture_id: str, manifest_version: int, manifest_hash: str) -> str:
    return "evidence-manifest:" + stable_protocol_hash(
        {
            "capture_id": capture_id,
            "manifest_version": manifest_version,
            "manifest_hash": manifest_hash,
        }
    )


def _event_id(
    *,
    event_type: str,
    task_id: str,
    capture_id: str,
    manifest_id: str,
    manifest_version: int,
    manifest_hash: str,
) -> str:
    return "evidence-event:" + stable_protocol_hash(
        {
            "event_type": event_type,
            "task_id": task_id,
            "capture_id": capture_id,
            "manifest_id": manifest_id,
            "manifest_version": manifest_version,
            "manifest_hash": manifest_hash,
        }
    )


def _validate_event_identity(
    *,
    event_id: str,
    event_type: str,
    task_id: str,
    capture_id: str,
    manifest_id: str,
    manifest_version: int,
    manifest_hash: str,
) -> None:
    try:
        for field_name, value in (
            ("event_id", event_id),
            ("task_id", task_id),
            ("capture_id", capture_id),
            ("manifest_id", manifest_id),
            ("manifest_hash", manifest_hash),
        ):
            require_text(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceContractValidationError(str(exc)) from exc
    if (
        isinstance(manifest_version, bool)
        or not isinstance(manifest_version, int)
        or manifest_version <= 0
    ):
        raise EvidenceContractValidationError("manifest_version 必须为正整数")
    expected = _event_id(
        event_type=event_type,
        task_id=task_id,
        capture_id=capture_id,
        manifest_id=manifest_id,
        manifest_version=manifest_version,
        manifest_hash=manifest_hash,
    )
    if event_id != expected:
        raise EvidenceContractValidationError("event_id 与事件身份不一致")


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


def _unique_text_tuple(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise EvidenceContractValidationError(f"{field_name} 必须为数组")
    try:
        normalized = tuple(require_text(value, field_name) for value in values)
    except ProtocolContractError as exc:
        raise EvidenceContractValidationError(str(exc)) from exc
    if len(set(normalized)) != len(normalized):
        raise EvidenceContractValidationError(f"{field_name} 不能包含重复值")
    return normalized
