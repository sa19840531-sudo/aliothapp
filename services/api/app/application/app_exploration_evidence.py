"""从 App Capture 的最新 Manifest 组装 Evidence Index。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from services.api.app.application.evidence_manifests import CaptureEvidenceManifest
from services.api.app.domain.evidence import (
    AppEvidenceGap,
    AppEvidenceIndex,
    AppEvidenceIndexEntry,
    AppEvidenceSemanticRole,
    EvidenceQualityStatus,
    build_app_evidence_index,
)


@dataclass(frozen=True, kw_only=True)
class AppExplorationEvidenceAssembly:
    """building Index 与尚未满足的证据缺口。"""

    index: AppEvidenceIndex
    gaps: tuple[AppEvidenceGap, ...]


@dataclass(frozen=True, kw_only=True)
class PersistedAppEvidenceIndex:
    """Evidence Index 幂等持久化结果。"""

    index: AppEvidenceIndex
    created: bool


class AppEvidenceIndexRepository(Protocol):
    """Evidence Index 构建、封存与只读查询端口。"""

    def closure_lock(
        self,
        *,
        run_id: str,
    ) -> AbstractAsyncContextManager[None]: ...

    async def create(self, *, index: AppEvidenceIndex) -> PersistedAppEvidenceIndex: ...

    async def seal(self, *, index: AppEvidenceIndex) -> PersistedAppEvidenceIndex: ...

    async def get_by_run_id(self, *, run_id: str) -> AppEvidenceIndex | None: ...


class AppExplorationEvidenceAssembler:
    """只把最新 PASS Manifest 投影为有序 Evidence Index 条目。"""

    def assemble(
        self,
        *,
        task_id: str,
        run_id: str,
        build_id: str,
        surface_session_id: str,
        captures: tuple[CaptureEvidenceManifest, ...],
        created_at: datetime,
        result_context_sha256: str,
    ) -> AppExplorationEvidenceAssembly:
        entries: list[AppEvidenceIndexEntry] = []
        gaps: list[AppEvidenceGap] = []
        for capture in captures:
            manifest = capture.manifest
            role = _semantic_role(capture)
            if manifest is None:
                gaps.append(
                    AppEvidenceGap(
                        semantic_role=role,
                        reason_code="manifest_missing",
                        description="Capture 缺少 Evidence Manifest",
                        capture_id=capture.capture_id,
                    )
                )
                continue
            if (
                manifest.task_id != task_id
                or manifest.session_id != surface_session_id
                or capture.agent_run_id != run_id
            ):
                raise ValueError("Capture Manifest 与目标任务、Run 或 Session 不一致")
            if manifest.quality_status is not EvidenceQualityStatus.PASS:
                gaps.append(
                    AppEvidenceGap(
                        semantic_role=role,
                        reason_code=f"manifest_{manifest.quality_status.value}",
                        description="Capture 最新 Manifest 尚未通过质量门禁",
                        capture_id=capture.capture_id,
                        metadata={"manifest_id": manifest.manifest_id},
                    )
                )
                continue
            metadata = dict(capture.metadata)
            entries.append(
                AppEvidenceIndexEntry(
                    sequence_no=len(entries) + 1,
                    capture_id=manifest.capture_id,
                    evidence_manifest_id=manifest.manifest_id,
                    evidence_manifest_version=manifest.manifest_version,
                    semantic_role=role,
                    surface_phase=_optional_text(
                        metadata.get("surface_phase_key") or metadata.get("surface_phase_run_id")
                    ),
                    exploration_work_id=_optional_text(
                        metadata.get("exploration_work_id") or metadata.get("parent_work_id")
                    ),
                    action_execution_id=capture.action_execution_id,
                    candidate_id=_optional_text(metadata.get("candidate_id")),
                    page_identity=_optional_text(
                        metadata.get("page_identity") or metadata.get("image_signature")
                    ),
                    captured_at=capture.captured_at,
                    metadata=metadata,
                )
            )
        index = build_app_evidence_index(
            task_id=task_id,
            run_id=run_id,
            build_id=build_id,
            surface_session_id=surface_session_id,
            entries=entries,
            created_at=created_at,
            result_context_sha256=result_context_sha256,
        )
        return AppExplorationEvidenceAssembly(index=index, gaps=tuple(gaps))


def _semantic_role(capture: CaptureEvidenceManifest) -> AppEvidenceSemanticRole:
    explicit = capture.metadata.get("semantic_role")
    if isinstance(explicit, str):
        try:
            return AppEvidenceSemanticRole(explicit)
        except ValueError:
            pass
    has_surface_phase = bool(capture.metadata.get("surface_phase_run_id"))
    if capture.capture_role == "bootstrap":
        return AppEvidenceSemanticRole.BOOTSTRAP
    if capture.capture_role == "before_action":
        return (
            AppEvidenceSemanticRole.SURFACE_BEFORE
            if has_surface_phase
            else AppEvidenceSemanticRole.ACTION_BEFORE
        )
    if capture.capture_role == "after_action":
        return (
            AppEvidenceSemanticRole.SURFACE_AFTER
            if has_surface_phase
            else AppEvidenceSemanticRole.ACTION_AFTER
        )
    if capture.capture_role == "observation":
        return AppEvidenceSemanticRole.SURFACE_REOBSERVE
    raise ValueError(f"App Capture role 不受支持：{capture.capture_role}")


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
