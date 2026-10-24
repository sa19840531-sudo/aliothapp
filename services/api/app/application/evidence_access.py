"""探索侧与判定侧共用的取证端口。

架构口径：探索（含证据留存）与规则质检是两个独立 Agent，
中间靠**同一个取证端口**读取已留存证据——两侧对「一次采集有哪些证据、
证据可不可信」必须给出同一个答案，否则同一份采集在探索眼里齐备、
在判定眼里不足，问题无法归因。

端口给出两级能力，按调用方需要付出的代价分开：

- `describe_capture`：只读清单，给出证据身份与裁决状态，**不读对象存储**。
  探索侧只需要知道"有哪些证据、能不能用"，不必把原文拉下来。
- `load_capture`：读对象存储并逐份核对 checksum，给出可判定的事实包。
  判定侧必须拿原文，也必须为原文的可信性买单。

两者的证据来源都是 Evidence Manifest（E19–E22）：
附件表不再是取证入口，只在清单缺席时作为显式回退，并标注 `evidence_source`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.application.manifest_backed_packet import (
    ManifestBackedPacketLoader,
    ManifestFactPacket,
    ManifestSource,
    ManifestUnavailableError,
)

# 证据来源标注：调用方据此判断拿到的是否为受裁决约束的证据
EVIDENCE_SOURCE_MANIFEST = "evidence_manifest"
EVIDENCE_SOURCE_ARTIFACT_TABLE = "artifact_table"


@dataclass(frozen=True, kw_only=True)
class CaptureEvidenceRef:
    """一份证据的可校验引用（不含原文）。"""

    artifact_id: str
    artifact_type: str
    role: str
    storage_path: str
    checksum: str
    size_bytes: int
    content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "role": self.role,
            "storage_path": self.storage_path,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }


@dataclass(frozen=True, kw_only=True)
class CaptureEvidenceDescriptor:
    """一次采集的证据清单投影：有哪些证据、来自哪、过没过裁决。"""

    task_id: str
    capture_id: str
    evidence_source: str
    artifacts: tuple[CaptureEvidenceRef, ...] = ()
    manifest_id: str | None = None
    manifest_version: int | None = None
    quality_status: str | None = None

    @property
    def manifest_backed(self) -> bool:
        return self.evidence_source == EVIDENCE_SOURCE_MANIFEST

    @property
    def adjudicated_pass(self) -> bool:
        """是否已通过齐全性裁决——判定侧只应消费这种证据。"""
        return self.manifest_backed and self.quality_status == "pass"

    @property
    def artifact_types(self) -> frozenset[str]:
        return frozenset(item.artifact_type for item in self.artifacts)

    def missing_types(self, required: Sequence[str]) -> tuple[str, ...]:
        return tuple(sorted(set(required) - self.artifact_types))

    def to_audit_payload(self) -> dict[str, Any]:
        """审计投影：只含身份与裁决状态，不含证据原文。"""
        return {
            "task_id": self.task_id,
            "capture_id": self.capture_id,
            "evidence_source": self.evidence_source,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "quality_status": self.quality_status,
            "artifact_count": len(self.artifacts),
        }


class EvidenceAccess(Protocol):
    """两侧共用的取证端口。"""

    async def describe_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> CaptureEvidenceDescriptor | None:
        """只读清单给出证据身份；无清单返回 None。"""

    async def load_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> ManifestFactPacket:
        """读原文并逐份核对 checksum，给出可判定事实包。"""


class ManifestEvidenceAccess:
    """以 Evidence Manifest 为唯一来源的取证端口实现。

    与 `ManifestBackedPacketLoader` 的关系：本类持有它并复用其
    完整性校验，同时补上「只看身份不读原文」的轻量通道。
    保留 `load_for_capture` 别名，使本类可直接顶替加载器注入判定侧。
    """

    def __init__(
        self,
        *,
        manifests: ManifestSource,
        artifact_reader: Any,
        require_quality_pass: bool = True,
    ) -> None:
        self._manifests = manifests
        self._loader = ManifestBackedPacketLoader(
            manifests=manifests,
            artifact_reader=artifact_reader,
            require_quality_pass=require_quality_pass,
        )

    async def describe_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> CaptureEvidenceDescriptor | None:
        manifest = await self._manifests.get_latest_for_capture(
            task_id=task_id,
            capture_id=capture_id,
            require_quality_pass=False,
        )
        if manifest is None:
            return None
        return describe_from_manifest(manifest)

    async def load_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> ManifestFactPacket:
        return await self._loader.load_for_capture(
            task_id=task_id,
            capture_id=capture_id,
        )

    async def load_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> ManifestFactPacket:
        """判定侧注入用的别名，签名与 ManifestBackedPacketLoader 一致。"""
        return await self.load_capture(task_id=task_id, capture_id=capture_id)


def describe_from_manifest(manifest: Any) -> CaptureEvidenceDescriptor:
    """把清单投影成取证描述符。"""
    return CaptureEvidenceDescriptor(
        task_id=manifest.task_id,
        capture_id=manifest.capture_id,
        evidence_source=EVIDENCE_SOURCE_MANIFEST,
        artifacts=tuple(
            CaptureEvidenceRef(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type,
                role=item.role,
                storage_path=item.storage_path,
                checksum=item.checksum,
                size_bytes=item.size_bytes,
                content_type=item.content_type,
            )
            for item in manifest.artifacts
        ),
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version,
        quality_status=manifest.quality_status.value,
    )


def describe_from_artifact_rows(
    *,
    task_id: str,
    capture_id: str,
    rows: Sequence[Any],
) -> CaptureEvidenceDescriptor:
    """清单缺席时的显式回退投影；来源标注为附件表，不冒充已裁决证据。"""
    artifacts: list[CaptureEvidenceRef] = []
    for row in rows:
        artifacts.append(
            CaptureEvidenceRef(
                artifact_id=str(_field(row, "artifact_id")),
                artifact_type=str(_field(row, "artifact_type")),
                role=str(_field(row, "role") or "primary"),
                storage_path=str(_field(row, "storage_path") or ""),
                checksum=str(_field(row, "checksum") or ""),
                size_bytes=int(_field(row, "size_bytes") or 0),
                content_type=_optional_text(_field(row, "content_type")),
            )
        )
    return CaptureEvidenceDescriptor(
        task_id=task_id,
        capture_id=capture_id,
        evidence_source=EVIDENCE_SOURCE_ARTIFACT_TABLE,
        artifacts=tuple(artifacts),
    )


def require_adjudicated(descriptor: CaptureEvidenceDescriptor | None) -> CaptureEvidenceDescriptor:
    """判定侧前置：只接受已通过裁决的清单化证据。"""
    if descriptor is None:
        raise ManifestUnavailableError("目标 Capture 没有可用于判定的证据清单")
    if not descriptor.manifest_backed:
        raise ManifestUnavailableError(
            f"证据来源为 {descriptor.evidence_source}，未受清单裁决约束，拒绝判定"
        )
    if not descriptor.adjudicated_pass:
        raise ManifestUnavailableError(
            f"证据清单未通过齐全性裁决：{descriptor.quality_status}"
        )
    return descriptor


def _field(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
