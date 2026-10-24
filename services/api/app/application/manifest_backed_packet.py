"""E19：按 Evidence Manifest 读取判定事实包的只读加载组件。

冻结依据：
- E02 条款 1.1：齐全性由清单侧裁决一次，判定侧只读结论、不得重算——
  本加载器只消费 quality_status，不做任何齐全性判断
- E19 验收：判定侧通过清单读取证据，调用方不修改或补写原始证据

只读由构造方式保证：本组件只持有「取清单」与「读对象」两个读端口，
不持有仓储写入、对象存储写入或补采能力，结构上无法修改或补写原始证据。

与既有 `PostgresRuleJudgeEvidenceReader` 的区别是证据来源：
后者直查 `qc_evidence_artifact`（E22 待移除的附件直查），
本加载器的附件清单、校验和与存储路径**全部来自清单**，
数据库附件表不参与判定读取。

分工边界：本模块只产出事实包，不生成 ManifestReadDecision、
不写审计载荷、不接生产判定链路——那是 E20；
无清单与校验失败的拒判与告警接线是 E21。
本模块只负责把失败表达为可分流的异常类型。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceManifestReference,
    EvidenceQualityStatus,
)

_SHA256_PREFIX = "sha256:"


class ManifestBackedReadError(RuntimeError):
    """按清单读取事实包失败的基类（E21 据此分流拒判与告警）。"""


class ManifestUnavailableError(ManifestBackedReadError):
    """目标 Capture 没有可用清单，或清单未通过齐全性裁决。

    E02 条款 2.1：pending、控制面未就绪与齐全性不通过必须分别表达，
    不得一律报「证据不足」——`quality_status` 为 None 表示清单缺席
    （控制面未就绪），否则携带清单裁决结论供上层分型。
    """

    def __init__(
        self,
        message: str,
        *,
        quality_status: EvidenceQualityStatus | None = None,
    ) -> None:
        super().__init__(message)
        self.quality_status = quality_status


class ManifestIntegrityError(ManifestBackedReadError):
    """清单声明的证据缺失、不可读或与实际内容不一致（须告警）。"""


class ManifestSource(Protocol):
    """清单读取端口（PostgresEvidenceManifestRepository 结构性满足）。"""

    async def get(self, *, manifest_id: str) -> EvidenceManifestReference | None: ...

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ) -> EvidenceManifestReference | None: ...


class EvidenceArtifactReader(Protocol):
    """对象存储只读端口；本组件不引用 store/delete。"""

    async def read(self, storage_path: str) -> bytes: ...


@dataclass(frozen=True, kw_only=True)
class ManifestFactArtifact:
    """清单声明并已按清单校验和核对过的一份证据原文。"""

    artifact_id: str
    artifact_type: str
    role: str
    checksum: str
    size_bytes: int
    payload: bytes
    content_type: str | None = None

    def decode_text(self) -> str:
        """按 UTF-8 严格读取文本证据。"""
        try:
            return self.payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ManifestIntegrityError(
                f"{self.artifact_type} 证据不是有效 UTF-8"
            ) from exc

    def decode_json(self) -> Any:
        """读取 JSON 证据并拒绝非法载荷。"""
        try:
            return json.loads(self.decode_text())
        except json.JSONDecodeError as exc:
            raise ManifestIntegrityError(
                f"{self.artifact_type} 证据不是有效 JSON"
            ) from exc


@dataclass(frozen=True, kw_only=True)
class ManifestFactPacket:
    """一次 Capture 的清单化事实包（判定侧只读投影）。

    身份字段直接取自清单，供 E20 生成 ManifestReadDecision 与审计载荷。
    """

    task_id: str
    session_id: str
    capture_id: str
    manifest_id: str
    manifest_version: int
    manifest_hash: str
    quality_status: EvidenceQualityStatus
    artifacts: tuple[ManifestFactArtifact, ...]

    @property
    def artifact_types(self) -> frozenset[str]:
        return frozenset(item.artifact_type for item in self.artifacts)

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        return tuple(item.artifact_id for item in self.artifacts)

    def artifacts_of_type(self, artifact_type: str) -> tuple[ManifestFactArtifact, ...]:
        """按类型返回证据，保持清单内顺序。"""
        return tuple(item for item in self.artifacts if item.artifact_type == artifact_type)

    def artifacts_of_role(self, role: str) -> tuple[ManifestFactArtifact, ...]:
        """按采集角色返回证据，保持清单内顺序。"""
        return tuple(item for item in self.artifacts if item.role == role)

    def require_unique(self, artifact_type: str) -> ManifestFactArtifact:
        """要求事实包内某类型恰好一份。"""
        matches = self.artifacts_of_type(artifact_type)
        if len(matches) != 1:
            raise ManifestIntegrityError(
                f"事实包必须包含唯一的 {artifact_type} 证据，实际 {len(matches)} 份"
            )
        return matches[0]


class ManifestBackedPacketLoader:
    """以清单为唯一证据来源，加载判定可消费的只读事实包。"""

    def __init__(
        self,
        *,
        manifests: ManifestSource,
        artifact_reader: EvidenceArtifactReader,
        require_quality_pass: bool = True,
    ) -> None:
        self._manifests = manifests
        self._artifact_reader = artifact_reader
        self._require_quality_pass = require_quality_pass

    async def load_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> ManifestFactPacket:
        """按 Capture 读取最新清单并加载事实包。

        E02 条款 2.1：读取时**不加 pass 过滤**——否则「清单缺席」与
        「清单未通过裁决」会退化成同一个 None，无法分别表达。
        取最新版本后由 `_load` 按 quality_status 分型。
        """
        manifest = await self._manifests.get_latest_for_capture(
            task_id=task_id,
            capture_id=capture_id,
            require_quality_pass=False,
        )
        if manifest is None:
            raise ManifestUnavailableError(
                f"Capture {capture_id} 没有可用于判定的证据清单",
                quality_status=None,
            )
        return await self._load(manifest, expected_task_id=task_id)

    async def load_for_manifest(
        self,
        *,
        task_id: str,
        manifest_id: str,
    ) -> ManifestFactPacket:
        """按清单身份加载事实包（判定复现同一版本证据时使用）。"""
        manifest = await self._manifests.get(manifest_id=manifest_id)
        if manifest is None:
            raise ManifestUnavailableError(
                f"证据清单 {manifest_id} 不存在",
                quality_status=None,
            )
        return await self._load(manifest, expected_task_id=task_id)

    async def _load(
        self,
        manifest: EvidenceManifestReference,
        *,
        expected_task_id: str,
    ) -> ManifestFactPacket:
        if manifest.task_id != expected_task_id:
            # 归属不符属控制面故障，与「清单未过裁决」分开表达
            raise ManifestUnavailableError(
                "证据清单不属于当前任务，拒绝读取",
                quality_status=None,
            )
        if (
            self._require_quality_pass
            and manifest.quality_status is not EvidenceQualityStatus.PASS
        ):
            raise ManifestUnavailableError(
                f"证据清单 {manifest.manifest_id} 未通过齐全性裁决："
                f"{manifest.quality_status.value}",
                quality_status=manifest.quality_status,
            )
        artifacts: list[ManifestFactArtifact] = []
        for reference in manifest.artifacts:
            artifacts.append(await self._read_artifact(reference, manifest=manifest))
        return ManifestFactPacket(
            task_id=manifest.task_id,
            session_id=manifest.session_id,
            capture_id=manifest.capture_id,
            manifest_id=manifest.manifest_id,
            manifest_version=manifest.manifest_version,
            manifest_hash=manifest.manifest_hash,
            quality_status=manifest.quality_status,
            artifacts=tuple(artifacts),
        )

    async def _read_artifact(
        self,
        reference: ArtifactReference,
        *,
        manifest: EvidenceManifestReference,
    ) -> ManifestFactArtifact:
        _require_path_ownership(
            reference.storage_path,
            task_id=manifest.task_id,
            capture_id=manifest.capture_id,
            artifact_id=reference.artifact_id,
        )
        try:
            payload = await self._artifact_reader.read(reference.storage_path)
        except Exception as exc:  # noqa: BLE001 - 读取失败一律按证据不可信处理
            raise ManifestIntegrityError(
                f"Artifact {reference.artifact_id} 无法读取：{exc}"
            ) from exc
        _require_matching_content(payload, reference=reference)
        return ManifestFactArtifact(
            artifact_id=reference.artifact_id,
            artifact_type=reference.artifact_type,
            role=reference.role,
            checksum=reference.checksum,
            size_bytes=reference.size_bytes,
            payload=payload,
            content_type=reference.content_type,
        )


def verify_artifact_payload(payload: bytes, *, reference: ArtifactReference) -> bool:
    """按清单声明核对一份证据原文；供 E20 生成读取裁决时复用。"""
    try:
        _require_matching_content(payload, reference=reference)
    except ManifestIntegrityError:
        return False
    return True


def _require_matching_content(payload: bytes, *, reference: ArtifactReference) -> None:
    if len(payload) != reference.size_bytes:
        raise ManifestIntegrityError(
            f"Artifact {reference.artifact_id} 大小与清单声明不一致："
            f"清单 {reference.size_bytes}，实际 {len(payload)}"
        )
    algorithm, separator, expected = reference.checksum.partition(":")
    if separator != ":" or algorithm.lower() != "sha256" or not expected:
        raise ManifestIntegrityError(
            f"Artifact {reference.artifact_id} 的清单校验和格式不受支持：{reference.checksum}"
        )
    actual = hashlib.sha256(payload).hexdigest()
    if actual.lower() != expected.lower():
        raise ManifestIntegrityError(
            f"Artifact {reference.artifact_id} 内容与清单校验和不一致，证据可能已被改动"
        )


def _require_path_ownership(
    storage_path: str,
    *,
    task_id: str,
    capture_id: str,
    artifact_id: str,
) -> None:
    """存储路径必须落在本任务与本 Capture 的目录下（越权读取防线）。"""
    parts: Sequence[str] = PurePosixPath(storage_path).parts
    if tuple(parts[:2]) != (task_id, capture_id):
        raise ManifestIntegrityError(
            f"Artifact {artifact_id} 的存储路径归属不匹配：{storage_path}"
        )
