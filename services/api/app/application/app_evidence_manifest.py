"""App Capture 提交后写入版本化 Evidence Manifest。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.domain.evidence import (
    AppEvidenceGap,
    AppEvidenceSemanticRole,
    ArtifactReference,
    EvidenceManifestReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    FinalizationRecoveryFenceError,
)

if TYPE_CHECKING:
    from services.api.app.application.app_capture_persistence import PersistedAppCapture
    from services.api.app.application.page_capture_persistence import (
        PersistedEvidenceArtifact,
    )

logger = logging.getLogger(__name__)


class _PersistedArtifactLike(Protocol):
    artifact_id: str
    artifact_type: str
    storage_path: str
    checksum: str
    size_bytes: int
    content_type: str


class _EvidenceArtifactReader(Protocol):
    async def read(self, storage_path: str) -> bytes: ...


@dataclass(frozen=True, kw_only=True)
class AppEvidenceManifestFinalization:
    """Manifest 追加校验版本以及对应的证据缺口。"""

    manifest: EvidenceManifestReference
    gaps: tuple[AppEvidenceGap, ...] = ()


class AppEvidenceManifestFinalizer:
    """读取真实对象并追加 PASS/FAIL Manifest 版本。"""

    def __init__(
        self,
        *,
        repository: EvidenceManifestRepository,
        artifact_store: _EvidenceArtifactReader,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._now = now or (lambda: datetime.now(UTC))

    async def finalize(
        self,
        manifest: EvidenceManifestReference,
        *,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> AppEvidenceManifestFinalization:
        if not isinstance(manifest, EvidenceManifestReference):
            raise TypeError("manifest 必须为 EvidenceManifestReference")
        if manifest.quality_status is not EvidenceQualityStatus.PENDING:
            return AppEvidenceManifestFinalization(manifest=manifest)

        latest = await self._repository.get_latest_for_capture(
            task_id=manifest.task_id,
            capture_id=manifest.capture_id,
        )
        if latest is not None and latest.manifest_version > manifest.manifest_version:
            if (
                latest.artifacts != manifest.artifacts
                or latest.observation_packet_ids != manifest.observation_packet_ids
            ):
                raise ValueError("历史终态 Manifest 与待校验 Manifest 的证据引用不一致")
            if latest.quality_status is EvidenceQualityStatus.PASS:
                return AppEvidenceManifestFinalization(manifest=latest)
            if latest.quality_status is EvidenceQualityStatus.FAIL:
                return AppEvidenceManifestFinalization(
                    manifest=latest,
                    gaps=(
                        AppEvidenceGap(
                            semantic_role=_semantic_role_from_capture_role(
                                latest.artifacts[0].role
                            ),
                            reason_code=latest.quality_reason or "manifest_failed",
                            description="Capture Manifest 已有失败终态",
                            capture_id=latest.capture_id,
                        ),
                    ),
                )
            manifest = latest

        gaps: list[AppEvidenceGap] = []
        for artifact in manifest.artifacts:
            reason_code: str | None = None
            try:
                content = await self._artifact_store.read(artifact.storage_path)
            except Exception:  # noqa: BLE001 - 读取失败必须落为 FAIL Manifest
                reason_code = "artifact_unreadable"
            else:
                if len(content) != artifact.size_bytes:
                    reason_code = "artifact_size_mismatch"
                elif f"sha256:{hashlib.sha256(content).hexdigest()}" != artifact.checksum:
                    reason_code = "artifact_checksum_mismatch"
            if reason_code is not None:
                gaps.append(
                    AppEvidenceGap(
                        semantic_role=_semantic_role_from_capture_role(artifact.role),
                        reason_code=reason_code,
                        description=f"Artifact {artifact.artifact_id} 完整性校验失败",
                        capture_id=manifest.capture_id,
                        metadata={"artifact_id": artifact.artifact_id},
                    )
                )

        status = EvidenceQualityStatus.FAIL if gaps else EvidenceQualityStatus.PASS
        finalized = build_evidence_manifest_reference(
            task_id=manifest.task_id,
            session_id=manifest.session_id,
            capture_id=manifest.capture_id,
            manifest_version=manifest.manifest_version + 1,
            artifacts=manifest.artifacts,
            observation_packet_ids=manifest.observation_packet_ids,
            quality_status=status,
            quality_reason=(gaps[0].reason_code if gaps else "artifact_integrity_verified"),
            created_at=self._now(),
        )
        try:
            await self._repository.add(
                manifest=finalized,
                **(
                    {
                        "recovery_lease_token": recovery_lease_token,
                        "recovery_generation": recovery_generation,
                        "recovery_lease_expires_at": recovery_lease_expires_at,
                    }
                    if recovery_lease_token is not None
                    else {}
                ),
            )
        except Exception as exc:
            if isinstance(exc, FinalizationRecoveryFenceError):
                raise
            # 多 Worker 可能同时校验同一 PENDING 版本；若赢家已提交相同证据引用的
            # 终态，直接采用 PostgreSQL 最新事实，禁止用不同 created_at 制造冲突。
            concurrent = await self._repository.get_latest_for_capture(
                task_id=manifest.task_id,
                capture_id=manifest.capture_id,
            )
            if (
                concurrent is None
                or concurrent.manifest_version < finalized.manifest_version
                or concurrent.quality_status is EvidenceQualityStatus.PENDING
                or concurrent.artifacts != manifest.artifacts
                or concurrent.observation_packet_ids != manifest.observation_packet_ids
            ):
                raise
            finalized = concurrent
            gaps = (
                [
                    AppEvidenceGap(
                        semantic_role=_semantic_role_from_capture_role(
                            concurrent.artifacts[0].role
                        ),
                        reason_code=concurrent.quality_reason or "manifest_failed",
                        description="Capture Manifest 已由并发恢复机固化为失败终态",
                        capture_id=concurrent.capture_id,
                    )
                ]
                if concurrent.quality_status is EvidenceQualityStatus.FAIL
                else []
            )
        return AppEvidenceManifestFinalization(
            manifest=finalized,
            gaps=tuple(gaps),
        )


def _artifact_references(
    *,
    artifacts: tuple[_PersistedArtifactLike, ...],
    capture_role: str,
) -> tuple[ArtifactReference, ...]:
    refs: list[ArtifactReference] = []
    for item in artifacts:
        refs.append(
            ArtifactReference(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type,
                role=capture_role,
                storage_path=item.storage_path,
                checksum=item.checksum,
                size_bytes=item.size_bytes,
                content_type=item.content_type,
                metadata=item.metadata,
            )
        )
    return tuple(refs)


def _app_capture_quality(
    *,
    persisted: PersistedAppCapture,
    artifacts: tuple[_PersistedArtifactLike, ...],
) -> tuple[EvidenceQualityStatus, str]:
    artifact_by_id = {item.artifact_id: item for item in artifacts}
    missing_ids = tuple(
        artifact_id
        for artifact_id in persisted.capture.artifact_ids
        if artifact_id not in artifact_by_id
    )
    if missing_ids:
        return (
            EvidenceQualityStatus.FAIL,
            "manifest_artifact_missing:" + ",".join(missing_ids),
        )
    screenshot = artifact_by_id.get(persisted.screenshot.artifact_id)
    if screenshot is None or screenshot.artifact_type != "screenshot":
        return EvidenceQualityStatus.FAIL, "screenshot_artifact_missing"
    return EvidenceQualityStatus.PASS, "app_capture_evidence_complete"


async def write_app_capture_manifest(
    *,
    persisted: PersistedAppCapture,
    artifacts: tuple[PersistedEvidenceArtifact, ...] | tuple[_PersistedArtifactLike, ...],
    repository: EvidenceManifestRepository | None = None,
) -> str | None:
    """为已落库 App Capture 写入已裁决 Manifest；失败必须交给调用方收口。"""

    store = repository or PostgresEvidenceManifestRepository()
    quality_status, quality_reason = _app_capture_quality(
        persisted=persisted,
        artifacts=tuple(artifacts),
    )
    manifest = build_evidence_manifest_reference(
        task_id=persisted.capture.task_id,
        session_id=persisted.capture.session_id,
        capture_id=persisted.capture.capture_id,
        manifest_version=1,
        artifacts=_artifact_references(
            artifacts=artifacts,
            capture_role=persisted.capture.role,
        ),
        quality_status=quality_status,
        created_at=datetime.now(UTC),
        quality_reason=quality_reason,
    )
    await store.add(manifest=manifest)
    return manifest.manifest_id


def build_pending_app_capture_manifest(
    *,
    task_id: str,
    session_id: str,
    capture_id: str,
    capture_role: str,
    artifacts: tuple[_PersistedArtifactLike, ...],
) -> EvidenceManifestReference:
    """在数据库事务前构造 PENDING Manifest，供 Capture 与 Manifest 原子提交。"""

    return build_evidence_manifest_reference(
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        manifest_version=1,
        artifacts=_artifact_references(
            artifacts=artifacts,
            capture_role=capture_role,
        ),
        quality_status=EvidenceQualityStatus.PENDING,
        created_at=datetime.now(UTC),
        quality_reason="app_capture_committed",
    )


async def write_pending_app_capture_manifest(
    *,
    persisted: PersistedAppCapture,
    artifacts: tuple[PersistedEvidenceArtifact, ...] | tuple[_PersistedArtifactLike, ...],
    repository: EvidenceManifestRepository | None = None,
) -> str | None:
    """为历史 App Capture 补写 PENDING Manifest；失败必须显式上抛。"""

    store = repository or PostgresEvidenceManifestRepository()
    manifest = build_pending_app_capture_manifest(
        task_id=persisted.capture.task_id,
        session_id=persisted.capture.session_id,
        capture_id=persisted.capture.capture_id,
        capture_role=persisted.capture.role,
        artifacts=tuple(artifacts),
    )
    await store.add(manifest=manifest)
    return manifest.manifest_id


async def promote_app_capture_manifest_pass(
    *,
    persisted: PersistedAppCapture,
    artifacts: tuple[PersistedEvidenceArtifact, ...] | tuple[_PersistedArtifactLike, ...],
    repository: EvidenceManifestRepository | None = None,
    previous_manifest_version: int = 1,
) -> str | None:
    """将仍 pending 的 App Capture Manifest 晋级为 pass。"""
    try:
        store = repository or PostgresEvidenceManifestRepository()
        latest = await store.get_latest_for_capture(
            task_id=persisted.capture.task_id,
            capture_id=persisted.capture.capture_id,
            require_quality_pass=False,
        )
        if latest is not None and latest.quality_status is EvidenceQualityStatus.PASS:
            return latest.manifest_id
        next_version = max(previous_manifest_version, latest.manifest_version if latest else 0) + 1
        manifest = build_evidence_manifest_reference(
            task_id=persisted.capture.task_id,
            session_id=persisted.capture.session_id,
            capture_id=persisted.capture.capture_id,
            manifest_version=next_version,
            artifacts=_artifact_references(
                artifacts=artifacts,
                capture_role=persisted.capture.role,
            ),
            quality_status=EvidenceQualityStatus.PASS,
            created_at=datetime.now(UTC),
            quality_reason="app_baseline_artifacts_ready",
        )
        await store.add(manifest=manifest)
        return manifest.manifest_id
    except Exception:  # noqa: BLE001 - 晋级失败保留原状态
        logger.warning(
            "task_id=%s capture_id=%s App Evidence Manifest 晋级失败",
            persisted.capture.task_id,
            persisted.capture.capture_id,
            exc_info=True,
        )
        return None


async def fail_app_capture_manifest(
    *,
    persisted: PersistedAppCapture,
    artifacts: tuple[PersistedEvidenceArtifact, ...] | tuple[_PersistedArtifactLike, ...],
    repository: EvidenceManifestRepository | None = None,
    previous_manifest_version: int = 1,
    quality_reason: str = "write_timeout",
) -> str | None:
    """将仍 pending 的 App Capture Manifest 固化为 fail。"""
    try:
        store = repository or PostgresEvidenceManifestRepository()
        latest = await store.get_latest_for_capture(
            task_id=persisted.capture.task_id,
            capture_id=persisted.capture.capture_id,
            require_quality_pass=False,
        )
        if latest is not None and latest.quality_status is not EvidenceQualityStatus.PENDING:
            return latest.manifest_id
        if not artifacts:
            return None
        next_version = max(previous_manifest_version, latest.manifest_version if latest else 0) + 1
        manifest = build_evidence_manifest_reference(
            task_id=persisted.capture.task_id,
            session_id=persisted.capture.session_id,
            capture_id=persisted.capture.capture_id,
            manifest_version=next_version,
            artifacts=_artifact_references(
                artifacts=artifacts,
                capture_role=getattr(persisted.capture, "role", "bootstrap") or "bootstrap",
            ),
            quality_status=EvidenceQualityStatus.FAIL,
            created_at=datetime.now(UTC),
            quality_reason=quality_reason,
        )
        await store.add(manifest=manifest)
        return manifest.manifest_id
    except Exception:  # noqa: BLE001 - 固化失败仍由 Index 记录不足
        logger.warning(
            "task_id=%s capture_id=%s App Evidence Manifest 固化 fail 失败",
            persisted.capture.task_id,
            persisted.capture.capture_id,
            exc_info=True,
        )
        return None


def _semantic_role_from_capture_role(value: str) -> AppEvidenceSemanticRole:
    mapping = {
        "bootstrap": AppEvidenceSemanticRole.BOOTSTRAP,
        "before_action": AppEvidenceSemanticRole.ACTION_BEFORE,
        "after_action": AppEvidenceSemanticRole.ACTION_AFTER,
        "observation": AppEvidenceSemanticRole.SURFACE_REOBSERVE,
        "final_observation": AppEvidenceSemanticRole.FINAL_OBSERVATION,
        "surface_before": AppEvidenceSemanticRole.SURFACE_BEFORE,
        "surface_after": AppEvidenceSemanticRole.SURFACE_AFTER,
        "surface_reobserve": AppEvidenceSemanticRole.SURFACE_REOBSERVE,
    }
    return mapping.get(value, AppEvidenceSemanticRole.SURFACE_REOBSERVE)
