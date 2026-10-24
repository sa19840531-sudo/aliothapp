"""封存后的 App Evidence Index 只读发布正式质检测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationWriteResult,
)
from services.api.app.application.app_sealed_inspection_publisher import (
    AppSealedEvidenceInspectionPublisher,
    AppSealedInspectionPublishError,
)
from services.api.app.domain.evidence import (
    AppEvidenceGap,
    AppEvidenceIndexEntry,
    AppEvidenceSemanticRole,
    AppExplorationResultOutcome,
    ArtifactReference,
    EvidenceQualityStatus,
    app_exploration_result_context_sha256,
    build_app_evidence_index,
    build_evidence_manifest_reference,
    seal_app_evidence_index,
)
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.domain.interaction import (
    SurfaceKind,
    SurfaceSessionSnapshot,
    SurfaceSessionStatus,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _manifest(capture_id: str, *, status: EvidenceQualityStatus = EvidenceQualityStatus.PASS):
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=2,
        artifacts=(
            ArtifactReference(
                artifact_id=f"artifact-{capture_id}",
                artifact_type="screenshot",
                role="observation",
                storage_path=f"task-1/{capture_id}.png",
                checksum="sha256:" + "a" * 64,
                size_bytes=128,
                content_type="image/png",
            ),
        ),
        quality_status=status,
        quality_reason=(
            "artifact_integrity_verified"
            if status is EvidenceQualityStatus.PASS
            else "checksum_failed"
        ),
        created_at=NOW,
    )


def _building_index():
    manifests = (_manifest("capture-2"), _manifest("capture-1"))
    entries = tuple(
        AppEvidenceIndexEntry(
            sequence_no=position,
            capture_id=manifest.capture_id,
            evidence_manifest_id=manifest.manifest_id,
            evidence_manifest_version=manifest.manifest_version,
            semantic_role=(
                AppEvidenceSemanticRole.FINAL_OBSERVATION
                if manifest.capture_id == "capture-2"
                else AppEvidenceSemanticRole.BOOTSTRAP
            ),
            page_identity=f"page:{manifest.capture_id}",
            captured_at=NOW + timedelta(seconds=2 if manifest.capture_id == "capture-2" else 1),
            metadata={"page_state": "stable"},
        )
        for position, manifest in enumerate(manifests, start=1)
    )
    return build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=entries,
        created_at=NOW,
        result_context_sha256=app_exploration_result_context_sha256(
            coverage_summary={"stable_page_count": 2},
            started_at=NOW,
        ),
    )


def _sealed_index(
    *, outcome: AppExplorationResultOutcome = AppExplorationResultOutcome.EXPLORATION_COMPLETED
):
    return seal_app_evidence_index(
        _building_index(),
        outcome=outcome,
        terminal_reason_code=outcome.value,
        sealed_at=NOW + timedelta(minutes=1),
    )


def _sealed_index_without_final(
    *,
    outcome: AppExplorationResultOutcome,
    empty: bool = False,
):
    entries = (
        ()
        if empty
        else tuple(
            entry
            for entry in _building_index().entries
            if entry.semantic_role is not AppEvidenceSemanticRole.FINAL_OBSERVATION
        )
    )
    building = build_app_evidence_index(
        task_id="task-1",
        run_id="run-1",
        build_id="build-1",
        surface_session_id="session-1",
        entries=entries,
        created_at=NOW,
        result_context_sha256=app_exploration_result_context_sha256(
            coverage_summary={"stable_page_count": 2},
            started_at=NOW,
        ),
    )
    gaps = (
        AppEvidenceGap(
            semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
            reason_code="final_observation_missing",
            description="最终观察未形成可信条目",
            metadata={"scope": "run"},
        ),
    )
    sealed = seal_app_evidence_index(
        building,
        outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
        terminal_reason_code="evidence_incomplete",
        evidence_gaps=gaps,
        sealed_at=NOW + timedelta(minutes=1),
    )
    if outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED:
        # 模拟迁移前已落库但违反新 Domain 合同的历史 sealed 记录，继续验证 Publisher 防线。
        object.__setattr__(sealed, "outcome", outcome)
        object.__setattr__(sealed, "terminal_reason_code", "exploration_completed")
        object.__setattr__(sealed, "evidence_gaps", ())
        object.__setattr__(sealed, "content_sha256", sealed.recompute_content_sha256())
    return sealed


def _session(status: SurfaceSessionStatus) -> SurfaceSessionSnapshot:
    return SurfaceSessionSnapshot(
        session_id="session-1",
        task_id="task-1",
        surface_kind=SurfaceKind.ANDROID,
        session_owner="app-workflow",
        status=status,
        command_sequence=3,
        finalization_epoch=1,
        row_version=4,
        account_environment_ref=None,
        platform_build_ref="build-1",
        device_ref="cloud:pixel",
        current_surface_ref="app-surface:session-1",
        current_capture_id="capture-2",
        started_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
    )


def _visual(capture_id: str):
    return SimpleNamespace(
        analysis=AppVisualAnalysis.build(
            capture_id=capture_id,
            screenshot_artifact_id=f"artifact-{capture_id}",
            image_signature="sha256:" + "b" * 64,
            width=1080,
            height=1920,
            orientation="portrait",
            visual_page_type="home",
            visual_summary="首页",
            key_elements=("内容区",),
            candidates=(),
            provider_id="fake-vision",
            model_id="deterministic-v1",
        )
    )


class MemoryIndexes:
    def __init__(self, index) -> None:
        self.index = index

    async def get_by_run_id(self, *, run_id: str):
        assert run_id == "run-1"
        return self.index


class MemoryManifests:
    def __init__(self, manifests) -> None:
        self.values = {item.manifest_id: item for item in manifests}
        self.calls: list[str] = []

    async def get(self, *, manifest_id: str):
        self.calls.append(manifest_id)
        return self.values.get(manifest_id)


class MemorySessions:
    def __init__(self, status: SurfaceSessionStatus) -> None:
        self.snapshot = _session(status)

    async def get(self, *, session_id: str):
        assert session_id == "session-1"
        return self.snapshot


class MemoryVisuals:
    def __init__(self, capture_ids: tuple[str, ...]) -> None:
        self.values = {capture_id: _visual(capture_id) for capture_id in capture_ids}
        self.calls: list[str] = []

    async def get_by_capture_id(self, *, capture_id: str):
        self.calls.append(capture_id)
        return self.values.get(capture_id)


class RecordingPublicationWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_for_index(self, **kwargs):
        self.calls.append(kwargs)
        return AppInspectionPublicationWriteResult(
            publication_id="publication-1",
            created=len(self.calls) == 1,
            entry_count=len(kwargs["entries"]),
        )


def _publisher(
    index,
    *,
    session_status: SurfaceSessionStatus = SurfaceSessionStatus.CLOSED,
    visual_capture_ids: tuple[str, ...] | None = None,
):
    manifests = tuple(_manifest(entry.capture_id) for entry in index.entries)
    manifest_repository = MemoryManifests(manifests)
    visuals = MemoryVisuals(
        visual_capture_ids
        if visual_capture_ids is not None
        else tuple(entry.capture_id for entry in index.entries)
    )
    publication_writer = RecordingPublicationWriter()
    return (
        AppSealedEvidenceInspectionPublisher(
            evidence_indexes=MemoryIndexes(index),
            manifests=manifest_repository,
            sessions=MemorySessions(session_status),
            visual_observations=visuals,
            publication_writer=publication_writer,
        ),
        manifest_repository,
        visuals,
        publication_writer,
    )


async def test_building_index_never_publishes_inspection() -> None:
    publisher, _, visuals, publication_writer = _publisher(_building_index())

    with pytest.raises(AppSealedInspectionPublishError, match="尚未封存"):
        await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert visuals.calls == []
    assert publication_writer.calls == []


@pytest.mark.parametrize("status", [SurfaceSessionStatus.ACTIVE, SurfaceSessionStatus.DRAINING])
async def test_nonterminal_session_never_publishes_inspection(status) -> None:
    publisher, _, _, publication_writer = _publisher(
        _sealed_index(),
        session_status=status,
    )

    with pytest.raises(AppSealedInspectionPublishError, match="尚未终止"):
        await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert publication_writer.calls == []


async def test_lost_session_keeps_sealed_evidence_available_for_inspection() -> None:
    publisher, _, _, publication_writer = _publisher(
        _sealed_index(),
        session_status=SurfaceSessionStatus.LOST,
    )

    result = await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert result.publication_entry_count == 2
    assert len(publication_writer.calls) == 1


async def test_sealed_index_publishes_only_index_captures_in_sequence_order() -> None:
    index = _sealed_index()
    publisher, manifests, visuals, publication_writer = _publisher(index)

    result = await publisher.publish(task_id="task-1", agent_run_id="run-1")

    expected_capture_ids = [entry.capture_id for entry in index.entries]
    entries = publication_writer.calls[0]["entries"]
    assert [entry.capture_id for entry in entries] == expected_capture_ids
    assert publication_writer.calls[0]["source_agent_run_id"] == "run-1"
    assert visuals.calls == expected_capture_ids
    assert manifests.calls == [entry.evidence_manifest_id for entry in index.entries]
    assert result.publication_entry_count == 2
    assert result.publication_created is True
    assert result.published_work_item_count == 0


async def test_repeated_publish_reuses_the_same_downstream_identities() -> None:
    index = _sealed_index()
    publisher, _, _, publication_writer = _publisher(index)

    first = await publisher.publish(task_id="task-1", agent_run_id="run-1")
    replay = await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert len(publication_writer.calls) == 2
    assert first.publication_created is True
    assert replay.publication_created is False


async def test_evidence_insufficient_uses_the_same_snapshot_only_policy() -> None:
    publisher, _, _, publication_writer = _publisher(
        _sealed_index(outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT)
    )

    result = await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.evidence_index_outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert len(publication_writer.calls) == 1


async def test_missing_visual_fact_downgrades_effective_outcome_to_insufficient() -> None:
    index = _sealed_index()
    publisher, _, _, publication_writer = _publisher(
        index,
        visual_capture_ids=("capture-1",),
    )

    result = await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert result.evidence_index_outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED
    assert result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.publication_entry_count == 2
    assert result.review_entry_count == 1
    assert any(item.status == "visual_fact_missing" for item in result.capture_results)
    entries = publication_writer.calls[0]["entries"]
    assert [entry.page_type for entry in entries] == ["home", None]


@pytest.mark.parametrize("empty", (False, True), ids=("bootstrap_only", "empty"))
async def test_insufficient_index_without_final_entry_remains_publishable_for_review(
    empty: bool,
) -> None:
    publisher, _, _, publication_writer = _publisher(
        _sealed_index_without_final(
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            empty=empty,
        )
    )

    result = await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.evidence_index_outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.indexed_capture_count == (0 if empty else 1)
    assert len(publication_writer.calls) == 1
    assert len(publication_writer.calls[0]["entries"]) == (0 if empty else 1)


async def test_completed_index_without_final_entry_still_fails_closed() -> None:
    publisher, _, _, publication_writer = _publisher(
        _sealed_index_without_final(
            outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        )
    )

    with pytest.raises(AppSealedInspectionPublishError) as captured:
        await publisher.publish(task_id="task-1", agent_run_id="run-1")

    assert captured.value.code == "APP_EVIDENCE_INDEX_FINAL_OBSERVATION_MISSING"
    assert publication_writer.calls == []


async def test_hash_mismatch_and_nonpass_manifest_fail_before_any_publish() -> None:
    index = _sealed_index()
    object.__setattr__(index, "content_sha256", "sha256:" + "f" * 64)
    publisher, _, _, publication_writer = _publisher(index)

    with pytest.raises(AppSealedInspectionPublishError, match="哈希"):
        await publisher.publish(task_id="task-1", agent_run_id="run-1")
    assert publication_writer.calls == []

    index = _sealed_index()
    publisher, manifests, _, publication_writer = _publisher(index)
    rejected = _manifest(index.entries[-1].capture_id, status=EvidenceQualityStatus.FAIL)
    manifests.values[index.entries[-1].evidence_manifest_id] = rejected
    with pytest.raises(AppSealedInspectionPublishError, match="Manifest"):
        await publisher.publish(task_id="task-1", agent_run_id="run-1")
    assert publication_writer.calls == []
