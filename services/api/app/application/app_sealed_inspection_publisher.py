"""只读消费 sealed App Evidence Index 并发布正式质检工作。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.application.app_exploration_evidence import (
    AppEvidenceIndexRepository,
)
from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationEntryInput,
    AppInspectionPublicationWriteResult,
)
from services.api.app.application.evidence_manifests import (
    EvidenceManifestRepository,
)
from services.api.app.application.surface_sessions import SurfaceSessionRepository
from services.api.app.domain.evidence import (
    AppEvidenceIndex,
    AppEvidenceIndexEntry,
    AppEvidenceIndexStatus,
    AppEvidenceSemanticRole,
    AppExplorationResultOutcome,
    EvidenceManifestReference,
    EvidenceQualityStatus,
)
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.app.infrastructure.db.app_inspection_publication_repository import (
    PostgresAppInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)


class AppSealedInspectionPublishError(RuntimeError):
    """探索结果未满足正式质检的只读交接门禁。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class _SavedVisualObservation(Protocol):
    analysis: AppVisualAnalysis


class _VisualObservationReader(Protocol):
    async def get_by_capture_id(
        self,
        *,
        capture_id: str,
    ) -> _SavedVisualObservation | None: ...


class _AppInspectionPublicationWriter(Protocol):
    async def create_for_index(
        self,
        *,
        task_id: str,
        source_agent_run_id: str,
        evidence_index_id: str,
        evidence_index_hash: str,
        entries: tuple[AppInspectionPublicationEntryInput, ...],
        expected_finalization_epoch: int | None = None,
    ) -> AppInspectionPublicationWriteResult: ...


class AppSealedInspectionPublisherPort(Protocol):
    """Worker 只依赖 sealed Index 发布入口，不感知其仓储组合。"""

    async def publish(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        expected_finalization_epoch: int | None = None,
    ) -> AppSealedInspectionPublishResult: ...


@dataclass(frozen=True, kw_only=True)
class AppSealedInspectionCaptureResult:
    """一个 Index Entry 的只读质检发布结果。"""

    sequence_no: int
    capture_id: str
    status: str
    published_count: int


@dataclass(frozen=True, kw_only=True)
class AppSealedInspectionPublishResult:
    """一次 sealed Index 发布的稳定摘要。"""

    evidence_index_id: str
    evidence_index_hash: str
    evidence_index_outcome: AppExplorationResultOutcome
    outcome: AppExplorationResultOutcome
    publication_id: str
    publication_created: bool
    publication_entry_count: int
    review_entry_count: int
    indexed_capture_count: int
    published_capture_count: int
    skipped_capture_count: int
    published_work_item_count: int
    created_work_item_count: int
    capture_results: tuple[AppSealedInspectionCaptureResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_index_id": self.evidence_index_id,
            "evidence_index_hash": self.evidence_index_hash,
            "evidence_index_outcome": self.evidence_index_outcome.value,
            "outcome": self.outcome.value,
            "publication_id": self.publication_id,
            "publication_created": self.publication_created,
            "publication_entry_count": self.publication_entry_count,
            "review_entry_count": self.review_entry_count,
            "indexed_capture_count": self.indexed_capture_count,
            "published_capture_count": self.published_capture_count,
            "skipped_capture_count": self.skipped_capture_count,
            "published_work_item_count": self.published_work_item_count,
            "created_work_item_count": self.created_work_item_count,
            "capture_results": [
                {
                    "sequence_no": item.sequence_no,
                    "capture_id": item.capture_id,
                    "status": item.status,
                    "published_count": item.published_count,
                }
                for item in self.capture_results
            ],
        }


class AppSealedEvidenceInspectionPublisher:
    """验证封存事实后，原子创建整份 Index 的持久化 Publication。"""

    def __init__(
        self,
        *,
        evidence_indexes: AppEvidenceIndexRepository,
        manifests: EvidenceManifestRepository,
        sessions: SurfaceSessionRepository,
        visual_observations: _VisualObservationReader,
        publication_writer: _AppInspectionPublicationWriter,
    ) -> None:
        self._evidence_indexes = evidence_indexes
        self._manifests = manifests
        self._sessions = sessions
        self._visual_observations = visual_observations
        self._publication_writer = publication_writer

    async def recover_if_unpublished(
        self, *, task_id: str, agent_run_id: str, finalization_epoch: int,
    ) -> bool:
        """只对当前轮次缺失的正式交接加载全份封存材料。"""

        list_missing = getattr(self._publication_writer, "list_unpublished_sealed_indexes", None)
        if not callable(list_missing):
            return False
        candidates = await list_missing(
            limit=1, task_id=task_id, source_agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        if not candidates:
            return False
        await self.publish(
            task_id=task_id, agent_run_id=agent_run_id,
            expected_finalization_epoch=finalization_epoch,
        )
        return True

    async def publish(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        expected_finalization_epoch: int | None = None,
    ) -> AppSealedInspectionPublishResult:
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        index = await self._evidence_indexes.get_by_run_id(run_id=run_id)
        sealed = _require_sealed_index(index, task_id=task, agent_run_id=run_id)
        await self._require_terminal_session(sealed)

        manifests = await self._load_and_validate_manifests(sealed)
        visual_facts = await self._load_and_validate_visual_facts(
            entries=sealed.entries,
            manifests=manifests,
        )

        publication_entries = tuple(
            AppInspectionPublicationEntryInput(
                sequence_no=entry.sequence_no,
                capture_id=entry.capture_id,
                evidence_manifest_id=entry.evidence_manifest_id,
                evidence_manifest_version=entry.evidence_manifest_version,
                semantic_role=entry.semantic_role.value,
                page_type=(visual.visual_page_type if visual is not None else None),
                page_state=(visual.page_state if visual is not None else "unknown"),
            )
            for entry, visual in zip(sealed.entries, visual_facts, strict=True)
        )
        publication = await self._publication_writer.create_for_index(
            task_id=task,
            source_agent_run_id=run_id,
            evidence_index_id=sealed.index_id,
            evidence_index_hash=sealed.content_sha256 or "",
            entries=publication_entries,
            **(
                {"expected_finalization_epoch": expected_finalization_epoch}
                if expected_finalization_epoch is not None
                else {}
            ),
        )
        capture_results = tuple(
            AppSealedInspectionCaptureResult(
                sequence_no=entry.sequence_no,
                capture_id=entry.capture_id,
                status=(
                    "visual_fact_missing" if visual is None else "publication_pending"
                ),
                published_count=0,
            )
            for entry, visual in zip(sealed.entries, visual_facts, strict=True)
        )

        effective_outcome = (
            AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
            if any(visual is None for visual in visual_facts)
            else sealed.outcome
        )
        return AppSealedInspectionPublishResult(
            evidence_index_id=sealed.index_id,
            evidence_index_hash=sealed.content_sha256 or "",
            evidence_index_outcome=sealed.outcome,
            outcome=effective_outcome,
            publication_id=publication.publication_id,
            publication_created=publication.created,
            publication_entry_count=publication.entry_count,
            review_entry_count=sum(visual is None for visual in visual_facts),
            indexed_capture_count=len(sealed.entries),
            published_capture_count=0,
            skipped_capture_count=0,
            published_work_item_count=0,
            created_work_item_count=0,
            capture_results=capture_results,
        )

    async def _require_terminal_session(self, index: AppEvidenceIndex) -> None:
        session = await self._sessions.get(session_id=index.surface_session_id)
        if (
            session is None
            or session.task_id != index.task_id
            or session.platform_build_ref != index.build_id
        ):
            raise AppSealedInspectionPublishError(
                "APP_SEALED_SESSION_MISMATCH",
                "Evidence Index 对应的 Surface Session 不存在或归属不一致",
            )
        if not session.status.is_terminal:
            raise AppSealedInspectionPublishError(
                "APP_SEALED_SESSION_NOT_TERMINAL",
                "Surface Session 尚未终止，禁止发布正式质检",
            )

    async def _load_and_validate_manifests(
        self,
        index: AppEvidenceIndex,
    ) -> tuple[EvidenceManifestReference, ...]:
        loaded: list[EvidenceManifestReference] = []
        for entry in index.entries:
            manifest = await self._manifests.get(
                manifest_id=entry.evidence_manifest_id
            )
            if not _manifest_matches_entry(manifest, index=index, entry=entry):
                raise AppSealedInspectionPublishError(
                    "APP_SEALED_MANIFEST_INVALID",
                    f"Capture {entry.capture_id} 的 PASS Manifest 与 Index 不一致",
                )
            loaded.append(manifest)
        return tuple(loaded)

    async def _load_and_validate_visual_facts(
        self,
        *,
        entries: tuple[AppEvidenceIndexEntry, ...],
        manifests: tuple[EvidenceManifestReference, ...],
    ) -> tuple[AppVisualAnalysis | None, ...]:
        loaded: list[AppVisualAnalysis | None] = []
        for entry, manifest in zip(entries, manifests, strict=True):
            saved = await self._visual_observations.get_by_capture_id(
                capture_id=entry.capture_id
            )
            if saved is None:
                loaded.append(None)
                continue
            analysis = saved.analysis
            if not isinstance(analysis, AppVisualAnalysis):
                raise AppSealedInspectionPublishError(
                    "APP_SEALED_VISUAL_FACT_INVALID",
                    f"Capture {entry.capture_id} 的视觉事实类型无效",
                )
            artifact_ids = {
                item.artifact_id
                for item in manifest.artifacts
                if item.artifact_type == "screenshot"
            }
            if (
                analysis.capture_id != entry.capture_id
                or analysis.screenshot_artifact_id not in artifact_ids
            ):
                raise AppSealedInspectionPublishError(
                    "APP_SEALED_VISUAL_FACT_INVALID",
                    f"Capture {entry.capture_id} 的视觉事实不属于已封存 Manifest",
                )
            loaded.append(analysis)
        return tuple(loaded)


def build_default_app_sealed_inspection_publisher() -> AppSealedEvidenceInspectionPublisher:
    """构建 Worker 进程复用的 sealed Evidence Index 发布器。"""
    return AppSealedEvidenceInspectionPublisher(
        evidence_indexes=PostgresAppEvidenceIndexRepository(),
        manifests=PostgresEvidenceManifestRepository(),
        sessions=PostgresSurfaceSessionRepository(),
        visual_observations=PostgresAppVisualObservationRepository(),
        publication_writer=PostgresAppInspectionPublicationRepository(),
    )


def _require_sealed_index(
    index: AppEvidenceIndex | None,
    *,
    task_id: str,
    agent_run_id: str,
) -> AppEvidenceIndex:
    if index is None:
        raise AppSealedInspectionPublishError(
            "APP_EVIDENCE_INDEX_MISSING",
            "App Exploration Result 缺少 Evidence Index",
        )
    if index.task_id != task_id or index.run_id != agent_run_id:
        raise AppSealedInspectionPublishError(
            "APP_EVIDENCE_INDEX_MISMATCH",
            "Evidence Index 与 Task 或 Agent Run 不一致",
        )
    if index.status is not AppEvidenceIndexStatus.SEALED:
        raise AppSealedInspectionPublishError(
            "APP_EVIDENCE_INDEX_NOT_SEALED",
            "Evidence Index 尚未封存，禁止发布正式质检",
        )
    if index.outcome is None:
        raise AppSealedInspectionPublishError(
            "APP_EXPLORATION_OUTCOME_MISSING",
            "sealed Evidence Index 缺少探索业务结果",
        )
    if index.outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED and (
        not index.entries
        or not any(
            entry.semantic_role is AppEvidenceSemanticRole.FINAL_OBSERVATION
            for entry in index.entries
        )
    ):
        raise AppSealedInspectionPublishError(
            "APP_EVIDENCE_INDEX_FINAL_OBSERVATION_MISSING",
            "sealed Evidence Index 缺少最终观察条目",
        )
    if index.content_sha256 != index.recompute_content_sha256():
        raise AppSealedInspectionPublishError(
            "APP_EVIDENCE_INDEX_HASH_MISMATCH",
            "Evidence Index 哈希校验失败",
        )
    return index


def _manifest_matches_entry(
    manifest: EvidenceManifestReference | None,
    *,
    index: AppEvidenceIndex,
    entry: AppEvidenceIndexEntry,
) -> bool:
    return bool(
        isinstance(manifest, EvidenceManifestReference)
        and manifest.manifest_id == entry.evidence_manifest_id
        and manifest.manifest_version == entry.evidence_manifest_version
        and manifest.task_id == index.task_id
        and manifest.session_id == index.surface_session_id
        and manifest.capture_id == entry.capture_id
        and manifest.quality_status is EvidenceQualityStatus.PASS
    )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
