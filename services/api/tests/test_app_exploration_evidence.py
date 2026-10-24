"""App Manifest 完整校验与 Evidence Index 组装测试。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
)
from services.api.app.application.app_exploration_evidence import (
    AppExplorationEvidenceAssembler,
)
from services.api.app.application.app_manifest_reconciliation import (
    AppManifestBacklogReconciler,
)
from services.api.app.application.evidence_manifests import (
    CaptureEvidenceManifest,
    PersistedEvidenceManifest,
)
from services.api.app.domain.evidence import (
    AppEvidenceSemanticRole,
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
CONTENT = b"trusted-app-screenshot"
CHECKSUM = f"sha256:{hashlib.sha256(CONTENT).hexdigest()}"


class MemoryManifestRepository:
    def __init__(self) -> None:
        self.saved = []

    async def add(self, *, manifest):
        self.saved.append(manifest)
        return PersistedEvidenceManifest(manifest=manifest, created=True)

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ):
        candidates = [
            item
            for item in self.saved
            if item.task_id == task_id
            and item.capture_id == capture_id
            and (not require_quality_pass or item.quality_status is EvidenceQualityStatus.PASS)
        ]
        return max(candidates, key=lambda item: item.manifest_version, default=None)


class MemoryArtifactStore:
    def __init__(self, content: bytes = CONTENT) -> None:
        self.content = content

    async def read(self, storage_path: str) -> bytes:
        assert storage_path == "task/capture/screenshot.png"
        return self.content


def _pending_manifest():
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id="artifact-1",
                artifact_type="screenshot",
                role="bootstrap",
                storage_path="task/capture/screenshot.png",
                checksum=CHECKSUM,
                size_bytes=len(CONTENT),
                content_type="image/png",
            ),
        ),
        quality_status=EvidenceQualityStatus.PENDING,
        quality_reason="app_capture_committed",
        created_at=NOW,
    )


async def test_manifest_finalizer_appends_pass_after_reading_object() -> None:
    repository = MemoryManifestRepository()
    result = await AppEvidenceManifestFinalizer(
        repository=repository,
        artifact_store=MemoryArtifactStore(),
        now=lambda: NOW + timedelta(seconds=1),
    ).finalize(_pending_manifest())

    assert result.manifest.manifest_version == 2
    assert result.manifest.quality_status is EvidenceQualityStatus.PASS
    assert result.gaps == ()
    assert repository.saved == [result.manifest]


async def test_manifest_finalizer_replay_returns_existing_terminal_version() -> None:
    repository = MemoryManifestRepository()
    finalizer = AppEvidenceManifestFinalizer(
        repository=repository,
        artifact_store=MemoryArtifactStore(),
        now=lambda: NOW + timedelta(seconds=len(repository.saved) + 1),
    )

    first = await finalizer.finalize(_pending_manifest())
    replay = await finalizer.finalize(_pending_manifest())

    assert replay.manifest == first.manifest
    assert len(repository.saved) == 1


async def test_manifest_finalizer_adopts_concurrent_terminal_version() -> None:
    class RacingRepository(MemoryManifestRepository):
        async def add(self, *, manifest):
            self.saved.append(replace(manifest, created_at=NOW + timedelta(seconds=2)))
            raise RuntimeError("concurrent insert won")

    repository = RacingRepository()
    result = await AppEvidenceManifestFinalizer(
        repository=repository,
        artifact_store=MemoryArtifactStore(),
        now=lambda: NOW + timedelta(seconds=1),
    ).finalize(_pending_manifest())

    assert result.manifest == repository.saved[0]
    assert result.manifest.quality_status is EvidenceQualityStatus.PASS


async def test_manifest_backlog_reconciler_finalizes_stale_pending() -> None:
    pending = _pending_manifest()
    repository = MemoryManifestRepository()

    class Reader:
        async def list_stale_pending(self, *, created_before, limit=100):
            assert created_before == NOW + timedelta(minutes=3)
            assert limit == 10
            return (pending,)

    reconciler = AppManifestBacklogReconciler(
        manifests=Reader(),
        finalizer=AppEvidenceManifestFinalizer(
            repository=repository,
            artifact_store=MemoryArtifactStore(),
            now=lambda: NOW + timedelta(minutes=5, seconds=1),
        ),
        stale_after_seconds=120,
        now=lambda: NOW + timedelta(minutes=5),
    )

    summary = await reconciler.reconcile_once(limit=10)

    assert summary.finalized == 1
    assert summary.failed == 0
    assert repository.saved[0].quality_status is EvidenceQualityStatus.PASS


async def test_manifest_finalizer_appends_fail_and_gap_on_checksum_mismatch() -> None:
    repository = MemoryManifestRepository()
    result = await AppEvidenceManifestFinalizer(
        repository=repository,
        artifact_store=MemoryArtifactStore(b"x" * len(CONTENT)),
        now=lambda: NOW + timedelta(seconds=1),
    ).finalize(_pending_manifest())

    assert result.manifest.quality_status is EvidenceQualityStatus.FAIL
    assert result.gaps[0].reason_code == "artifact_checksum_mismatch"


def test_assembler_only_indexes_pass_manifests_and_maps_semantic_roles() -> None:
    passed = build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        manifest_version=2,
        artifacts=_pending_manifest().artifacts,
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified",
        created_at=NOW + timedelta(seconds=1),
    )
    pending = _pending_manifest()

    assembly = AppExplorationEvidenceAssembler().assemble(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        captures=(
            CaptureEvidenceManifest(
                capture_id=pending.capture_id,
                manifest=pending,
                agent_run_id="run-1",
                capture_role="observation",
                captured_at=NOW + timedelta(seconds=1),
                metadata={"semantic_role": "final_observation"},
            ),
            CaptureEvidenceManifest(
                capture_id=passed.capture_id,
                manifest=passed,
                agent_run_id="run-1",
                capture_role="bootstrap",
                captured_at=NOW,
                metadata={},
            ),
        ),
        created_at=NOW + timedelta(seconds=2),
        result_context_sha256="sha256:" + "1" * 64,
    )

    assert len(assembly.index.entries) == 1
    assert assembly.index.entries[0].semantic_role is AppEvidenceSemanticRole.BOOTSTRAP
    assert assembly.gaps[0].semantic_role is AppEvidenceSemanticRole.FINAL_OBSERVATION
    assert assembly.gaps[0].reason_code == "manifest_pending"


def test_assembler_exposes_capture_without_manifest_as_gap() -> None:
    assembly = AppExplorationEvidenceAssembler().assemble(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        captures=(
            CaptureEvidenceManifest(
                capture_id="capture-missing",
                manifest=None,
                agent_run_id="run-1",
                capture_role="observation",
                captured_at=NOW,
                metadata={"semantic_role": "final_observation"},
            ),
        ),
        created_at=NOW,
        result_context_sha256="sha256:" + "1" * 64,
    )

    assert assembly.index.entries == ()
    assert assembly.gaps[0].reason_code == "manifest_missing"
    assert assembly.gaps[0].capture_id == "capture-missing"
