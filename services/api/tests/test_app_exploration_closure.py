"""App 探索最终观察、证据封存与 Session 关闭顺序测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import services.api.app.application.app_exploration_closure as closure_module
from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalization,
)
from services.api.app.application.app_exploration_closure import (
    AppExplorationClosureError,
    AppExplorationClosureService,
    CloseAppExplorationCommand,
)
from services.api.app.application.app_exploration_evidence import (
    AppExplorationEvidenceAssembler,
    PersistedAppEvidenceIndex,
)
from services.api.app.application.app_exploration_results import (
    PersistedAppExplorationResult,
)
from services.api.app.application.app_metrics import (
    APP_EXPLORATION_CLOSURE_FAILURE_TOTAL,
    APP_EXPLORATION_EVIDENCE_GAP_TOTAL,
    APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL,
    app_metric_value,
)
from services.api.app.application.app_screenshot_understanding import (
    AppScreenshotUnderstandingError,
)
from services.api.app.application.evidence_manifests import CaptureEvidenceManifest
from services.api.app.application.surface_sessions import SessionCloseReadiness
from services.api.app.domain.evidence import (
    AppEvidenceIndexStatus,
    AppEvidenceSemanticRole,
    AppExplorationResultOutcome,
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration import AppVisualAnalysis, ExplorationBudget
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSnapshot,
    SurfaceSessionStatus,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _manifest(
    *,
    status: EvidenceQualityStatus,
    capture_id: str = "capture-final",
    artifact_id: str = "artifact-final",
    artifact_role: str = "observation",
):
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=1 if status is EvidenceQualityStatus.PENDING else 2,
        artifacts=(
            ArtifactReference(
                artifact_id=artifact_id,
                artifact_type="screenshot",
                role=artifact_role,
                storage_path=f"task/capture/{capture_id}.png",
                checksum="sha256:" + "a" * 64,
                size_bytes=100,
                content_type="image/png",
            ),
        ),
        quality_status=status,
        quality_reason=(
            "app_capture_committed"
            if status is EvidenceQualityStatus.PENDING
            else "artifact_integrity_verified"
        ),
        created_at=NOW,
    )


def _snapshot(status: SurfaceSessionStatus, row_version: int) -> SurfaceSessionSnapshot:
    return SurfaceSessionSnapshot(
        session_id="session-1",
        task_id="task-1",
        surface_kind=SurfaceKind.ANDROID,
        session_owner="app-workflow",
        status=status,
        command_sequence=5,
        finalization_epoch=0,
        row_version=row_version,
        account_environment_ref=None,
        platform_build_ref="build-1",
        device_ref="cloud:pixel",
        current_surface_ref="app-surface:session-1",
        current_capture_id="capture-current",
        started_at=NOW,
        updated_at=NOW + timedelta(seconds=row_version),
    )


class RecordingCaptureCoordinator:
    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error

    async def capture_observation(self, **kwargs):
        self.events.append("final_observation")
        assert kwargs["semantic_role"] == "final_observation"
        assert kwargs["intent"].deadline == NOW + timedelta(seconds=60)
        assert kwargs["intent"].directive.artifact_types == (
            "screenshot",
            "ui_tree",
            "ui_snapshot",
            "visible_text",
        )
        if self.error is not None:
            raise self.error
        return CaptureRef(
            task_id="task-1",
            session_id="session-1",
            capture_id="capture-final",
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="observation",
            artifact_ids=("artifact-final",),
            parent_capture_id="capture-current",
        )


class MemoryManifestRepository:
    def __init__(self) -> None:
        self.current = _manifest(status=EvidenceQualityStatus.PENDING)

    async def list_latest_for_session(self, **kwargs):
        return (
            CaptureEvidenceManifest(
                capture_id=self.current.capture_id,
                manifest=self.current,
                agent_run_id="run-1",
                capture_role="observation",
                captured_at=NOW + timedelta(seconds=1),
                metadata={"semantic_role": "final_observation"},
            ),
        )


class StaticManifestRepository:
    """用于收口分支的最新 Manifest 投影。"""

    def __init__(
        self,
        manifest,
        *,
        include_final_capture: bool = True,
    ) -> None:
        self.manifest = manifest
        self.include_final_capture = include_final_capture

    async def list_latest_for_session(self, **kwargs):
        if not self.include_final_capture:
            return ()
        return (
            CaptureEvidenceManifest(
                capture_id="capture-final",
                manifest=self.manifest,
                agent_run_id="run-1",
                capture_role="observation",
                captured_at=NOW + timedelta(seconds=1),
                metadata={"semantic_role": "final_observation"},
            ),
        )


class StaticManifestSetRepository:
    """返回一次动作前后与最终观察的 PASS Capture 集合。"""

    def __init__(self, captures: tuple[CaptureEvidenceManifest, ...]) -> None:
        self.captures = captures

    async def list_latest_for_session(self, **kwargs):
        return self.captures


class NoopManifestFinalizer:
    async def finalize(self, manifest):
        return AppEvidenceManifestFinalization(manifest=manifest)


class RaisingManifestFinalizer:
    async def finalize(self, manifest):
        raise RuntimeError("manifest finalization unavailable")


class RecordingManifestFinalizer:
    def __init__(self, events: list[str], repository: MemoryManifestRepository) -> None:
        self.events = events
        self.repository = repository

    async def finalize(self, manifest):
        self.events.append("finalize_manifests")
        finalized = _manifest(status=EvidenceQualityStatus.PASS)
        self.repository.current = finalized
        return AppEvidenceManifestFinalization(manifest=finalized)


class RecordingUnderstanding:
    def __init__(
        self,
        events: list[str],
        *,
        error: Exception | None = None,
        errors_by_capture: dict[str, Exception] | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.errors_by_capture = errors_by_capture or {}
        self.calls = 0

    async def resolve_capture_image_signature(self, capture: CaptureRef) -> str:
        return _visual_analysis(capture).image_signature

    async def understand(self, capture: CaptureRef) -> AppVisualAnalysis:
        self.events.append(
            "understand_final_observation"
            if capture.capture_id == "capture-final"
            else f"understand:{capture.capture_id}"
        )
        self.calls += 1
        error = self.errors_by_capture.get(capture.capture_id, self.error)
        if error is not None:
            raise error
        return _visual_analysis(capture)


class MemoryVisualObservations:
    def __init__(
        self,
        events: list[str],
        *,
        existing: AppVisualAnalysis | tuple[AppVisualAnalysis, ...] | None = None,
    ) -> None:
        self.events = events
        existing_items = (
            () if existing is None else existing if isinstance(existing, tuple) else (existing,)
        )
        self.saved = {item.capture_id: item for item in existing_items}
        self.save_calls = 0

    async def get_by_capture_id(self, *, capture_id: str):
        self.events.append(
            "load_final_visual" if capture_id == "capture-final" else f"load_visual:{capture_id}"
        )
        analysis = self.saved.get(capture_id)
        if analysis is None:
            return None
        return SimpleNamespace(analysis=analysis)

    async def save(self, analysis: AppVisualAnalysis):
        self.events.append(
            "save_final_visual"
            if analysis.capture_id == "capture-final"
            else f"save_visual:{analysis.capture_id}"
        )
        self.save_calls += 1
        self.saved[analysis.capture_id] = analysis
        return SimpleNamespace(analysis=analysis)


class MemoryCaptureReader:
    def __init__(self, *captures: CaptureRef) -> None:
        self.captures = {capture.capture_id: capture for capture in captures}

    async def get_capture(self, *, capture_id: str):
        capture = self.captures.get(capture_id)
        if capture is None:
            return None
        return SimpleNamespace(capture=capture, agent_run_id="run-1")


class RecordingIndexRepository:
    def __init__(
        self,
        events: list[str],
        *,
        fail_create: bool = False,
        fail_seal: bool = False,
    ) -> None:
        self.events = events
        self.fail_create = fail_create
        self.fail_seal = fail_seal
        self.index = None
        self.seal_attempt = None
        self._closure_lock = asyncio.Lock()

    @asynccontextmanager
    async def closure_lock(self, *, run_id: str):
        assert run_id == "run-1"
        async with self._closure_lock:
            yield

    async def get_by_run_id(self, *, run_id: str):
        return self.index

    async def create(self, *, index):
        self.events.append("create_index")
        if self.fail_create:
            raise RuntimeError("create failed")
        self.index = index
        return PersistedAppEvidenceIndex(index=index, created=True)

    async def seal(self, *, index):
        self.events.append("seal_index")
        self.seal_attempt = index
        if self.fail_seal:
            raise RuntimeError("seal failed")
        self.index = index
        return PersistedAppEvidenceIndex(index=index, created=False)


class RecordingResultRepository:
    def __init__(self, events: list[str], *, fail_create: bool = False) -> None:
        self.events = events
        self.fail_create = fail_create
        self.result = None

    async def create(self, *, result):
        self.events.append("persist_result")
        if self.fail_create:
            raise RuntimeError("result create failed")
        self.result = result
        return PersistedAppExplorationResult(result=result, created=True)

    async def get_by_run_id(self, *, run_id: str):
        return self.result


class RecordingResultIntentRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.intent = None

    async def record(self, *, intent):
        self.events.append("persist_result_intent")
        self.intent = intent
        return SimpleNamespace(intent=intent, created=True)


class RecordingSessions:
    def __init__(
        self,
        events: list[str],
        *,
        ready: bool = True,
        readiness_error: Exception | None = None,
        begin_draining_error: Exception | None = None,
        seal_for_close_error: Exception | None = None,
        mark_closed_error: Exception | None = None,
        mark_closed_committed_error: Exception | None = None,
        mark_lost_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.ready = ready
        self.readiness_error = readiness_error
        self.begin_draining_error = begin_draining_error
        self.seal_for_close_error = seal_for_close_error
        self.mark_closed_error = mark_closed_error
        self.mark_closed_committed_error = mark_closed_committed_error
        self.mark_lost_error = mark_lost_error
        self.snapshot = _snapshot(SurfaceSessionStatus.ACTIVE, 1)

    async def get(self, *, session_id: str):
        return self.snapshot

    async def begin_draining(self, **kwargs):
        self.events.append("begin_draining")
        if self.begin_draining_error is not None:
            raise self.begin_draining_error
        self.snapshot = _snapshot(SurfaceSessionStatus.DRAINING, 2)
        return self.snapshot

    async def close_readiness(self, **kwargs):
        self.events.append("close_readiness")
        if self.readiness_error is not None:
            raise self.readiness_error
        blocked = 0 if self.ready else 1
        return SessionCloseReadiness(
            session_id="session-1",
            ready=self.ready,
            pending_actions=blocked,
            pending_capture_commits=0,
            active_evidence_requests=0,
            running_restorations=0,
            nonterminal_active_work_items=0,
            pinned_surfaces=0,
            pending_manifests=0,
            building_evidence_indexes=0,
        )

    async def seal_for_close(self, **kwargs):
        self.events.append("seal_for_close")
        if self.seal_for_close_error is not None:
            raise self.seal_for_close_error
        self.snapshot = _snapshot(SurfaceSessionStatus.CLOSING, 3)
        return self.snapshot

    async def mark_closed(self, **kwargs):
        self.events.append("mark_closed")
        if self.mark_closed_error is not None:
            raise self.mark_closed_error
        self.snapshot = _snapshot(SurfaceSessionStatus.CLOSED, 4)
        if self.mark_closed_committed_error is not None:
            raise self.mark_closed_committed_error
        return self.snapshot

    async def mark_lost(self, **kwargs):
        self.events.append("mark_lost")
        if self.mark_lost_error is not None:
            raise self.mark_lost_error
        self.snapshot = _snapshot(SurfaceSessionStatus.LOST, 4)
        return self.snapshot


class RecordingDriver:
    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error

    async def close_session(self, *, session: SurfaceSessionRef, reason: str) -> None:
        self.events.append("driver.close_session")
        if self.error is not None:
            raise self.error


class RecordingActiveSessionInspection:
    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.calls: list[dict] = []

    async def drain_before_close(self, **kwargs):
        self.events.append("drain_active_session_inspection")
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(published_work_item_count=1, drain_round_count=1)


class RecordingSupplementalEvidenceDrain:
    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.calls: list[dict] = []

    async def drain(self, **kwargs):
        self.events.append("drain_supplemental_evidence")
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(consumed_action_count=1)


def _supplemental_budget(*, remaining_actions: int = 2) -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=5,
        remaining_actions=remaining_actions,
        max_depth=4,
        remaining_duration_ms=50_000,
    )


def _command() -> CloseAppExplorationCommand:
    return CloseAppExplorationCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="session-1",
        build_id="build-1",
        surface_kind=SurfaceKind.ANDROID,
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        coverage_summary={"unique_state_count": 3},
        started_at=NOW,
        deadline=NOW + timedelta(seconds=60),
    )


def _final_capture() -> CaptureRef:
    return CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-final",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="observation",
        artifact_ids=("artifact-final",),
        parent_capture_id="capture-current",
    )


def _action_capture(*, before: bool) -> CaptureRef:
    return CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-before" if before else "capture-after",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="before_action" if before else "after_action",
        artifact_ids=("artifact-before" if before else "artifact-after",),
        parent_capture_id="capture-current" if before else "capture-before",
        action_execution_id="action-1",
    )


def _capture_manifest_item(
    capture: CaptureRef,
    *,
    sequence_no: int,
) -> CaptureEvidenceManifest:
    semantic_role = (
        "action_before"
        if capture.role == "before_action"
        else "action_after"
        if capture.role == "after_action"
        else "final_observation"
    )
    return CaptureEvidenceManifest(
        capture_id=capture.capture_id,
        manifest=_manifest(
            status=EvidenceQualityStatus.PASS,
            capture_id=capture.capture_id,
            artifact_id=capture.artifact_ids[0],
            artifact_role=capture.role,
        ),
        agent_run_id="run-1",
        capture_role=capture.role,
        captured_at=NOW + timedelta(seconds=sequence_no),
        metadata={"semantic_role": semantic_role},
        action_execution_id=capture.action_execution_id,
    )


def _visual_analysis(capture: CaptureRef | None = None) -> AppVisualAnalysis:
    capture = capture or _final_capture()
    return AppVisualAnalysis.build(
        capture_id=capture.capture_id,
        screenshot_artifact_id=capture.artifact_ids[0],
        image_signature="sha256:" + "a" * 64,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="最终首页",
        key_elements=("内容区",),
        candidates=(),
        provider_id="fake-vision",
        model_id="deterministic-v1",
        page_state="stable",
    )


def _service(
    *,
    events: list[str],
    ready: bool = True,
    readiness_error: Exception | None = None,
    capture_error: Exception | None = None,
    fail_create: bool = False,
    fail_seal: bool = False,
    fail_result_create: bool = False,
    driver_error: Exception | None = None,
    begin_draining_error: Exception | None = None,
    seal_for_close_error: Exception | None = None,
    mark_closed_error: Exception | None = None,
    mark_closed_committed_error: Exception | None = None,
    mark_lost_error: Exception | None = None,
    understanding_error: Exception | None = None,
    existing_visual: bool = False,
    result_intents=None,
    active_session_inspection=None,
    supplemental_evidence_drain=None,
):
    manifests = MemoryManifestRepository()
    understanding = RecordingUnderstanding(events, error=understanding_error)
    visual_observations = MemoryVisualObservations(
        events,
        existing=_visual_analysis() if existing_visual else None,
    )
    sessions = RecordingSessions(
        events,
        ready=ready,
        readiness_error=readiness_error,
        begin_draining_error=begin_draining_error,
        seal_for_close_error=seal_for_close_error,
        mark_closed_error=mark_closed_error,
        mark_closed_committed_error=mark_closed_committed_error,
        mark_lost_error=mark_lost_error,
    )
    return (
        AppExplorationClosureService(
            capture_coordinator=RecordingCaptureCoordinator(events, error=capture_error),
            captures=MemoryCaptureReader(_final_capture()),
            manifests=manifests,
            manifest_finalizer=RecordingManifestFinalizer(events, manifests),
            evidence_assembler=AppExplorationEvidenceAssembler(),
            evidence_indexes=RecordingIndexRepository(
                events,
                fail_create=fail_create,
                fail_seal=fail_seal,
            ),
            exploration_results=RecordingResultRepository(
                events,
                fail_create=fail_result_create,
            ),
            result_intents=result_intents,
            understanding=understanding,
            visual_observations=visual_observations,
            sessions=sessions,
            driver=RecordingDriver(events, error=driver_error),
            active_session_inspection=active_session_inspection,
            supplemental_evidence_drain=supplemental_evidence_drain,
            now=lambda: NOW + timedelta(seconds=10),
        ),
        sessions,
    )


def _service_with_manifest(
    *,
    events: list[str],
    manifest=None,
    include_final_capture: bool = True,
    finalizer_error: bool = False,
    readiness_error: Exception | None = None,
    capture_error: Exception | None = None,
    fail_create: bool = False,
    fail_seal: bool = False,
    fail_result_create: bool = False,
    driver_error: Exception | None = None,
    begin_draining_error: Exception | None = None,
    seal_for_close_error: Exception | None = None,
    mark_lost_error: Exception | None = None,
    understanding_error: Exception | None = None,
    existing_visual: bool = False,
):
    manifests = StaticManifestRepository(
        manifest,
        include_final_capture=include_final_capture,
    )
    sessions = RecordingSessions(
        events,
        readiness_error=readiness_error,
        begin_draining_error=begin_draining_error,
        seal_for_close_error=seal_for_close_error,
        mark_lost_error=mark_lost_error,
    )
    indexes = RecordingIndexRepository(
        events,
        fail_create=fail_create,
        fail_seal=fail_seal,
    )
    results = RecordingResultRepository(events, fail_create=fail_result_create)
    understanding = RecordingUnderstanding(events, error=understanding_error)
    visual_observations = MemoryVisualObservations(
        events,
        existing=_visual_analysis() if existing_visual else None,
    )
    service = AppExplorationClosureService(
        capture_coordinator=RecordingCaptureCoordinator(events, error=capture_error),
        captures=MemoryCaptureReader(_final_capture()),
        manifests=manifests,
        manifest_finalizer=(
            RaisingManifestFinalizer() if finalizer_error else NoopManifestFinalizer()
        ),
        evidence_assembler=AppExplorationEvidenceAssembler(),
        evidence_indexes=indexes,
        exploration_results=results,
        understanding=understanding,
        visual_observations=visual_observations,
        sessions=sessions,
        driver=RecordingDriver(events, error=driver_error),
        now=lambda: NOW + timedelta(seconds=10),
    )
    return service, sessions, indexes, results


def _service_with_capture_set(
    *,
    events: list[str],
    captures: tuple[CaptureRef, ...],
    understanding: RecordingUnderstanding | None = None,
    cancellation=None,
):
    observations = MemoryVisualObservations(events)
    visual_understanding = understanding or RecordingUnderstanding(events)
    service = AppExplorationClosureService(
        capture_coordinator=RecordingCaptureCoordinator(events),
        captures=MemoryCaptureReader(*captures),
        manifests=StaticManifestSetRepository(
            tuple(
                _capture_manifest_item(capture, sequence_no=sequence_no)
                for sequence_no, capture in enumerate(captures, start=1)
            )
        ),
        manifest_finalizer=NoopManifestFinalizer(),
        evidence_assembler=AppExplorationEvidenceAssembler(),
        evidence_indexes=RecordingIndexRepository(events),
        exploration_results=RecordingResultRepository(events),
        understanding=visual_understanding,
        visual_observations=observations,
        sessions=RecordingSessions(events),
        driver=RecordingDriver(events),
        cancellation=cancellation,
        now=lambda: NOW + timedelta(seconds=10),
    )
    return service, observations, visual_understanding


async def test_closure_runs_strict_final_observation_to_session_close_order() -> None:
    events: list[str] = []
    active_inspection = RecordingActiveSessionInspection(events)
    service, _ = _service(
        events=events,
        active_session_inspection=active_inspection,
    )

    result = await service.close(_command())

    assert events == [
        "final_observation",
        "finalize_manifests",
        "load_final_visual",
        "understand_final_observation",
        "save_final_visual",
        "drain_active_session_inspection",
        "create_index",
        "seal_index",
        "persist_result",
        "begin_draining",
        "close_readiness",
        "seal_for_close",
        "driver.close_session",
        "mark_closed",
    ]
    assert result.session.status is SurfaceSessionStatus.CLOSED
    assert result.index.status is AppEvidenceIndexStatus.SEALED
    assert result.index.outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED
    assert result.index.evidence_gaps == ()
    assert result.exploration_result.outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED
    assert result.exploration_result.evidence_gaps == result.index.evidence_gaps
    assert result.exploration_result.evidence_index_hash == result.index.content_sha256
    assert result.exploration_result.result_sha256 == (
        result.exploration_result.recompute_result_sha256()
    )
    assert active_inspection.calls == [
        {
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "deadline": NOW + timedelta(seconds=60),
        }
    ]


async def test_active_session_inspection_failure_prevents_seal_and_session_close() -> None:
    events: list[str] = []
    active_inspection = RecordingActiveSessionInspection(
        events,
        error=RuntimeError("active inspection timeout"),
    )
    service, sessions = _service(
        events=events,
        active_session_inspection=active_inspection,
    )

    with pytest.raises(RuntimeError, match="active inspection timeout"):
        await service.close(_command())

    assert "create_index" not in events
    # LOST 前会尽力 Driver close，避免同机残留 APK
    assert events[-2:] == ["driver.close_session", "mark_lost"]
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST


async def test_supplemental_drain_replaces_single_pass_drain_and_reloads_evidence() -> None:
    events: list[str] = []
    active_inspection = RecordingActiveSessionInspection(events)
    supplemental_drain = RecordingSupplementalEvidenceDrain(events)
    service, _ = _service(
        events=events,
        active_session_inspection=active_inspection,
        supplemental_evidence_drain=supplemental_drain,
    )
    command = replace(
        _command(),
        supplemental_evidence_budget=_supplemental_budget(),
    )

    await service.close(command)

    assert active_inspection.calls == []
    assert supplemental_drain.calls == [
        {
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "finalization_epoch": 0,
            "budget": command.supplemental_evidence_budget,
            "deadline": NOW + timedelta(seconds=60),
        }
    ]
    assert events.count("load_final_visual") == 2
    assert events.index("drain_supplemental_evidence") < events.index("create_index")


async def test_supplemental_drain_failure_prevents_seal_and_session_close() -> None:
    events: list[str] = []
    supplemental_drain = RecordingSupplementalEvidenceDrain(
        events,
        error=RuntimeError("supplemental drain failed"),
    )
    service, sessions = _service(
        events=events,
        supplemental_evidence_drain=supplemental_drain,
    )

    with pytest.raises(RuntimeError, match="supplemental drain failed"):
        await service.close(
            replace(
                _command(),
                supplemental_evidence_budget=_supplemental_budget(),
            )
        )

    assert "create_index" not in events
    assert events[-2:] == ["driver.close_session", "mark_lost"]
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST


async def test_closure_persists_result_intent_before_evidence_index_seal() -> None:
    events: list[str] = []
    intents = RecordingResultIntentRepository(events)
    service, _ = _service(events=events, result_intents=intents)

    result = await service.close(_command())

    assert intents.intent is not None
    assert intents.intent.result_context_sha256 == result.index.result_context_sha256
    assert events.index("persist_result_intent") < events.index("create_index")
    assert events.index("persist_result_intent") < events.index("seal_index")


async def test_closed_session_releases_bound_device_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def release(*, session_id: str) -> bool:
        calls.append(("release", session_id))
        return True

    async def quarantine(*, session_id: str, reason: str) -> bool:
        calls.append(("quarantine", f"{session_id}:{reason}"))
        return True

    monkeypatch.setattr(closure_module, "release_device_lease_for_session", release)
    monkeypatch.setattr(
        closure_module,
        "quarantine_device_lease_for_session",
        quarantine,
    )
    service, _ = _service(events=[])
    closed = replace(
        _snapshot(SurfaceSessionStatus.CLOSED, 5),
        device_lease_id="11111111-1111-1111-1111-111111111111",
        device_lease_fencing_token=8,
    )

    assert await service._settle_device_lease(session=closed, reason="completed") is None
    assert calls == [("release", "session-1")]


async def test_existing_final_visual_fact_is_reused_before_seal() -> None:
    events: list[str] = []
    service, _ = _service(events=events, existing_visual=True)

    result = await service.close(_command())

    assert result.index.outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED
    assert "load_final_visual" in events
    assert "understand_final_observation" not in events
    assert "save_final_visual" not in events
    assert events.index("load_final_visual") < events.index("create_index")


async def test_closure_backfills_every_pass_capture_visual_before_seal() -> None:
    events: list[str] = []
    before = _action_capture(before=True)
    after = _action_capture(before=False)
    final = _final_capture()
    service, observations, understanding = _service_with_capture_set(
        events=events,
        captures=(before, after, final),
    )

    result = await service.close(_command())

    assert result.index.outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED
    assert tuple(entry.capture_id for entry in result.index.entries) == (
        "capture-before",
        "capture-after",
        "capture-final",
    )
    assert set(observations.saved) == {
        "capture-before",
        "capture-after",
        "capture-final",
    }
    assert understanding.calls == 3
    assert events.index("save_visual:capture-before") < events.index("create_index")
    assert events.index("save_visual:capture-after") < events.index("create_index")
    assert events.index("save_final_visual") < events.index("create_index")


async def test_cancellation_during_visual_backfill_still_seals_and_closes_session() -> None:
    class AlwaysCanceled:
        async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
            return True

    events: list[str] = []
    before = _action_capture(before=True)
    after = _action_capture(before=False)
    service, _observations, understanding = _service_with_capture_set(
        events=events,
        captures=(before, after),
        cancellation=AlwaysCanceled(),
    )

    result = await service.close(
        replace(
            _command(),
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="workflow_canceled",
            coverage_summary={"cancel_requested": True},
            capture_final_observation=False,
        )
    )

    assert result.session.status is SurfaceSessionStatus.CLOSED
    assert result.index.status is AppEvidenceIndexStatus.SEALED
    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.index.entries == ()
    assert understanding.calls == 0
    assert sum(
        gap.reason_code == "workflow_canceled_before_visual_understanding"
        for gap in result.index.evidence_gaps
    ) == 2
    assert "driver.close_session" in events


async def test_closure_excludes_pass_capture_when_visual_backfill_fails() -> None:
    events: list[str] = []
    before = _action_capture(before=True)
    after = _action_capture(before=False)
    final = _final_capture()
    understanding = RecordingUnderstanding(
        events,
        errors_by_capture={
            "capture-before": AppScreenshotUnderstandingError(
                "截图缺失",
                error_code="SCREENSHOT_ARTIFACT_MISSING",
            )
        },
    )
    service, observations, _ = _service_with_capture_set(
        events=events,
        captures=(before, after, final),
        understanding=understanding,
    )

    visual_before = app_metric_value(APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL)
    gap_before = app_metric_value(APP_EXPLORATION_EVIDENCE_GAP_TOTAL)

    result = await service.close(_command())

    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert tuple(entry.capture_id for entry in result.index.entries) == (
        "capture-after",
        "capture-final",
    )
    assert any(
        gap.reason_code == "capture_visual_understanding_failed"
        and gap.semantic_role.value == "action_before"
        and gap.capture_id == "capture-before"
        and gap.metadata["error_code"] == "SCREENSHOT_ARTIFACT_MISSING"
        for gap in result.index.evidence_gaps
    )
    assert "capture-after" in observations.saved
    assert "capture-final" in observations.saved
    assert events.index("understand:capture-before") < events.index("understand:capture-after")
    assert app_metric_value(APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL) == visual_before + 1
    assert app_metric_value(APP_EXPLORATION_EVIDENCE_GAP_TOTAL) == gap_before + 1


async def test_final_visual_failure_seals_insufficient_gap_and_closes_session() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=_manifest(status=EvidenceQualityStatus.PASS),
        understanding_error=AppScreenshotUnderstandingError(
            "截图校验失败",
            error_code="SCREENSHOT_CHECKSUM_MISMATCH",
        ),
    )

    result = await service.close(_command())

    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert any(
        gap.reason_code == "final_observation_visual_understanding_failed"
        and gap.capture_id == "capture-final"
        for gap in result.index.evidence_gaps
    )
    assert result.exploration_result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert indexes.index == result.index
    assert results.result == result.exploration_result
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED
    assert events.index("understand_final_observation") < events.index("create_index")


@pytest.mark.parametrize(
    "error_code",
    ("VISUAL_UNDERSTANDING_FAILED", "INVALID_VISUAL_OUTPUT"),
)
async def test_visual_provider_technical_failure_marks_session_lost(
    error_code: str,
) -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=_manifest(status=EvidenceQualityStatus.PASS),
        understanding_error=AppScreenshotUnderstandingError(
            "视觉 Provider 技术失败",
            error_code=error_code,
            retryable=error_code == "VISUAL_UNDERSTANDING_FAILED",
        ),
    )

    with pytest.raises(AppScreenshotUnderstandingError, match="视觉 Provider 技术失败"):
        await service.close(_command())

    assert indexes.index is None
    assert results.result is None
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events[-2:] == ["driver.close_session", "mark_lost"]


@pytest.mark.parametrize(
    "manifest",
    (
        _manifest(status=EvidenceQualityStatus.PENDING),
        _manifest(status=EvidenceQualityStatus.FAIL),
        None,
    ),
    ids=("pending_manifest", "failed_manifest", "missing_manifest"),
)
async def test_nonpass_final_manifest_seals_insufficient_result_and_closes_session(
    manifest,
) -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=manifest,
    )

    result = await service.close(_command())

    assert result.index.status is AppEvidenceIndexStatus.SEALED
    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.index.terminal_reason_code == "evidence_incomplete"
    assert result.index.evidence_gaps
    assert result.index.content_sha256 == result.index.recompute_content_sha256()
    assert result.exploration_result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.exploration_result.evidence_gaps == result.index.evidence_gaps
    assert result.exploration_result.evidence_index_hash == result.index.content_sha256
    assert result.exploration_result.result_sha256 == (
        result.exploration_result.recompute_result_sha256()
    )
    assert results.result == result.exploration_result
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED


async def test_final_observation_capture_failure_seals_insufficient_gap_and_closes_session() -> (
    None
):
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        include_final_capture=False,
        capture_error=RuntimeError("capture unavailable"),
    )

    result = await service.close(_command())

    assert result.final_observation is None
    assert result.index.status is AppEvidenceIndexStatus.SEALED
    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.index.terminal_reason_code == "evidence_incomplete"
    assert any(gap.semantic_role.value == "final_observation" for gap in result.index.evidence_gaps)
    assert result.exploration_result.evidence_gaps == result.index.evidence_gaps
    assert results.result == result.exploration_result
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED


async def test_expired_deadline_skips_final_device_io_and_seals_gap() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        include_final_capture=False,
    )

    result = await service.close(
        replace(_command(), deadline=NOW + timedelta(seconds=1))
    )

    assert "final_observation" not in events
    assert result.final_observation is None
    gap = next(
        item
        for item in result.index.evidence_gaps
        if item.reason_code == "final_observation_deadline_exhausted"
    )
    assert gap.metadata["deadline"] == (NOW + timedelta(seconds=1)).isoformat()
    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert results.result == result.exploration_result
    assert indexes.index == result.index
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED


async def test_final_capture_failure_preserves_existing_run_gap() -> None:
    events: list[str] = []
    service, _, _, _ = _service_with_manifest(
        events=events,
        manifest=None,
        include_final_capture=False,
        capture_error=RuntimeError("capture unavailable"),
    )

    result = await service.close(
        replace(
            _command(),
            run_evidence_gap_reason_code="APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
        )
    )

    reason_codes = {item.reason_code for item in result.index.evidence_gaps}
    assert reason_codes == {
        "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
        "final_observation_capture_failed",
    }


async def test_cancellation_skips_final_observation_and_seals_existing_evidence() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        include_final_capture=False,
    )

    result = await service.close(
        replace(
            _command(),
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="workflow_canceled",
            coverage_summary={"cancel_requested": True},
            capture_final_observation=False,
        )
    )

    assert "final_observation" not in events
    assert result.final_observation is None
    assert result.index.status is AppEvidenceIndexStatus.SEALED
    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert any(
        gap.reason_code == "workflow_canceled_before_final_observation"
        for gap in result.index.evidence_gaps
    )
    assert result.exploration_result.evidence_gaps == result.index.evidence_gaps
    assert indexes.index == result.index
    assert results.result == result.exploration_result
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED


async def test_unavailable_terminal_session_seals_existing_evidence_without_new_capture() -> None:
    """APP_READY 会话已 LOST 时只封存已有事实，绝不能再次触达 Driver。"""

    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        include_final_capture=False,
    )
    sessions.snapshot = _snapshot(SurfaceSessionStatus.LOST, 4)

    result = await service.close(
        replace(
            _command(),
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="APP_SESSION_REATTACH_REQUIRED",
            coverage_summary={"session_recovery_unavailable": True},
            capture_final_observation=False,
            run_evidence_gap_reason_code="APP_SESSION_REATTACH_REQUIRED",
        )
    )

    assert "final_observation" not in events
    assert "driver.close_session" not in events
    assert result.index.status is AppEvidenceIndexStatus.SEALED
    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.exploration_result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.session.status is SurfaceSessionStatus.LOST
    assert result.close_error == "surface_session_lost"
    assert any(
        gap.reason_code == "APP_SESSION_REATTACH_REQUIRED"
        and gap.semantic_role
        in {
            AppEvidenceSemanticRole.ACTION_AFTER,
            AppEvidenceSemanticRole.FINAL_OBSERVATION,
        }
        and gap.metadata["scope"] == "run"
        for gap in result.index.evidence_gaps
    )
    assert indexes.index == result.index
    assert results.result == result.exploration_result


async def test_action_recovery_reason_becomes_auditable_run_gap() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        include_final_capture=False,
    )

    result = await service.close(
        replace(
            _command(),
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
            run_evidence_gap_reason_code="APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
        )
    )

    gap = next(
        gap
        for gap in result.index.evidence_gaps
        if gap.reason_code == "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH"
    )
    assert gap.capture_id is None
    assert gap.semantic_role is AppEvidenceSemanticRole.ACTION_AFTER
    assert gap.metadata["scope"] == "run"
    assert result.exploration_result.evidence_gaps == result.index.evidence_gaps
    assert indexes.index == result.index
    assert results.result == result.exploration_result
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED


async def test_manifest_finalization_technical_failure_marks_session_lost() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=_manifest(status=EvidenceQualityStatus.PENDING),
        finalizer_error=True,
    )

    with pytest.raises(RuntimeError, match="manifest finalization unavailable"):
        await service.close(_command())

    assert indexes.index is None
    assert results.result is None
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_insufficient_evidence_replay_is_idempotent_and_does_not_recapture() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
    )

    metric_before = app_metric_value(APP_EXPLORATION_EVIDENCE_GAP_TOTAL)
    first = await service.close(_command())
    metric_after_first_close = app_metric_value(APP_EXPLORATION_EVIDENCE_GAP_TOTAL)
    events.clear()
    replay = await service.close(_command())

    assert replay.index == first.index
    assert replay.exploration_result == first.exploration_result
    assert replay.session.status is SurfaceSessionStatus.CLOSED
    assert events == []
    assert indexes.index == first.index
    assert results.result == first.exploration_result
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED
    assert metric_after_first_close > metric_before
    assert app_metric_value(APP_EXPLORATION_EVIDENCE_GAP_TOTAL) == metric_after_first_close


async def test_closure_replay_ignores_page_graph_shadow_drift() -> None:
    """仅影子栏漂移不得阻断 Closure 幂等重放（#16）。"""

    from dataclasses import replace

    events: list[str] = []
    service, _sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
    )
    first = await service.close(_command())
    events.clear()
    shadow_command = replace(
        _command(),
        coverage_summary={
            "unique_state_count": 3,
            "app_page_graph_snapshot": {"protocol": "shadow", "nodes": []},
            "app_coverage_page_graph_shadow": {
                "authority_source": "coverage_summary",
                "coverage_authority_changed": False,
            },
        },
    )
    replay = await service.close(shadow_command)

    assert replay.index == first.index
    assert replay.exploration_result == first.exploration_result
    assert indexes.index == first.index
    assert results.result == first.exploration_result
    assert events == []


async def test_nonpass_evidence_keeps_sealed_index_when_driver_close_fails() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        driver_error=RuntimeError("device lost"),
    )

    result = await service.close(_command())

    assert result.index.status is AppEvidenceIndexStatus.SEALED
    assert result.index.evidence_gaps
    assert result.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
    assert result.index.terminal_reason_code == "evidence_incomplete"
    assert result.exploration_result.evidence_gaps == result.index.evidence_gaps
    assert results.result == result.exploration_result
    assert result.session.status is SurfaceSessionStatus.LOST
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert result.close_error == "device lost"


async def test_index_seal_failure_never_starts_session_close() -> None:
    events: list[str] = []
    service, _ = _service(events=events, fail_seal=True)

    with pytest.raises(RuntimeError, match="seal failed"):
        await service.close(_command())

    # 正规收口未进入 draining；LOST 清理路径仍会尽力 close Driver
    assert "begin_draining" not in events
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_index_seal_failure_retains_gap_payload_and_marks_session_lost() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        fail_seal=True,
    )

    with pytest.raises(RuntimeError, match="seal failed"):
        await service.close(_command())

    assert indexes.index is not None
    assert indexes.index.status is AppEvidenceIndexStatus.BUILDING
    assert indexes.seal_attempt is not None
    assert indexes.seal_attempt.status is AppEvidenceIndexStatus.SEALED
    assert indexes.seal_attempt.evidence_gaps
    assert results.result is None
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_index_create_failure_marks_session_lost_without_claiming_result() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        fail_create=True,
    )

    metric_before = app_metric_value(APP_EXPLORATION_CLOSURE_FAILURE_TOTAL)
    with pytest.raises(RuntimeError, match="create failed"):
        await service.close(_command())

    assert indexes.index is None
    assert results.result is None
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert "begin_draining" not in events
    assert events[-2:] == ["driver.close_session", "mark_lost"]
    assert app_metric_value(APP_EXPLORATION_CLOSURE_FAILURE_TOTAL) == metric_before + 1


async def test_terminal_replay_does_not_count_same_closure_failure_twice() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        fail_create=True,
    )

    metric_before = app_metric_value(APP_EXPLORATION_CLOSURE_FAILURE_TOTAL)
    with pytest.raises(RuntimeError, match="create failed"):
        await service.close(_command())
    with pytest.raises(AppExplorationClosureError, match="Session 仍处于 ACTIVE"):
        await service.close(_command())

    assert indexes.index is None
    assert results.result is None
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events.count("mark_lost") == 1
    assert app_metric_value(APP_EXPLORATION_CLOSURE_FAILURE_TOTAL) == metric_before + 1


async def test_mark_lost_failure_does_not_mask_original_closure_error() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        fail_create=True,
        mark_lost_error=RuntimeError("mark lost unavailable"),
    )

    metric_before = app_metric_value(APP_EXPLORATION_CLOSURE_FAILURE_TOTAL)
    with pytest.raises(RuntimeError, match="create failed"):
        await service.close(_command())

    assert indexes.index is None
    assert results.result is None
    assert sessions.snapshot.status is SurfaceSessionStatus.ACTIVE
    # LOST 落库失败前仍会尽力 close；事件以 mark_lost 尝试作结
    assert "driver.close_session" in events
    assert events[-1] == "mark_lost"
    assert app_metric_value(APP_EXPLORATION_CLOSURE_FAILURE_TOTAL) == metric_before + 1


async def test_readiness_failure_keeps_persisted_gaps_and_marks_session_lost() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        readiness_error=RuntimeError("readiness unavailable"),
    )

    result = await service.close(_command())

    assert indexes.index is not None
    assert indexes.index.status is AppEvidenceIndexStatus.SEALED
    assert indexes.index.evidence_gaps
    assert results.result is not None
    assert results.result.evidence_gaps == indexes.index.evidence_gaps
    assert result.close_error == "readiness unavailable"
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_nonzero_readiness_never_calls_driver_close() -> None:
    events: list[str] = []
    service, sessions = _service(events=events, ready=False)

    result = await service.close(_command())

    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert result.close_error == "Surface Session 尚未排空，禁止关闭 Driver"
    assert events[-2:] == ["driver.close_session", "mark_lost"]


@pytest.mark.parametrize(
    ("service_error", "message", "expected_event"),
    (
        ("begin_draining_error", "begin draining failed", "begin_draining"),
        ("seal_for_close_error", "seal for close failed", "seal_for_close"),
    ),
)
async def test_session_transition_failure_marks_session_lost(
    service_error: str,
    message: str,
    expected_event: str,
) -> None:
    events: list[str] = []
    service, sessions = _service(
        events=events,
        **{service_error: RuntimeError(message)},
    )

    result = await service.close(_command())

    assert expected_event in events
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert result.close_error == message
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_result_create_failure_keeps_sealed_index_and_marks_session_lost() -> None:
    events: list[str] = []
    service, sessions, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
        fail_result_create=True,
    )

    with pytest.raises(RuntimeError, match="result create failed"):
        await service.close(_command())

    assert indexes.index is not None
    assert indexes.index.status is AppEvidenceIndexStatus.SEALED
    assert results.result is None
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_mark_closed_failure_keeps_sealed_result_and_marks_session_lost() -> None:
    events: list[str] = []
    service, sessions = _service(
        events=events,
        mark_closed_error=RuntimeError("mark closed failed"),
    )

    result = await service.close(_command())

    assert "driver.close_session" in events
    assert events[-1] == "mark_lost"
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert result.close_error == "mark closed failed"


async def test_mark_closed_response_loss_accepts_authoritative_closed_state() -> None:
    events: list[str] = []
    service, sessions = _service(
        events=events,
        mark_closed_committed_error=RuntimeError("mark closed response lost"),
    )

    result = await service.close(_command())

    assert events[-1] == "mark_closed"
    assert "mark_lost" not in events
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED
    assert result.session.status is SurfaceSessionStatus.CLOSED
    assert result.close_error is None


async def test_close_cancellation_marks_session_lost_before_reraising() -> None:
    events: list[str] = []
    service, sessions = _service(events=events)
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def blocking_create(*, index):
        del index
        events.append("create_index")
        started.set()
        await blocked.wait()

    service._evidence_indexes.create = blocking_create  # type: ignore[method-assign]
    close_task = asyncio.create_task(service.close(_command()))
    await started.wait()
    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_wrong_surface_command_does_not_mark_unrelated_session_lost() -> None:
    events: list[str] = []
    service, sessions = _service(events=events)

    with pytest.raises(AppExplorationClosureError, match="Session 不存在或归属不一致"):
        await service.close(replace(_command(), surface_kind=SurfaceKind.IOS))

    assert sessions.snapshot.status is SurfaceSessionStatus.ACTIVE
    assert "mark_lost" not in events


async def test_wrong_build_command_does_not_mark_unrelated_session_lost() -> None:
    events: list[str] = []
    service, sessions = _service(events=events)

    with pytest.raises(AppExplorationClosureError, match="Session 不存在或归属不一致"):
        await service.close(replace(_command(), build_id="build-other"))

    assert sessions.snapshot.status is SurfaceSessionStatus.ACTIVE
    assert "mark_lost" not in events


@pytest.mark.parametrize(
    ("changes", "error_pattern"),
    (
        ({"surface_kind": SurfaceKind.IOS}, "Session 不存在或归属不一致"),
        ({"build_id": "build-other"}, "sealed Evidence Index 与关闭请求不一致"),
    ),
)
async def test_sealed_replay_rejects_wrong_session_identity_without_closing(
    changes: dict[str, object],
    error_pattern: str,
) -> None:
    events: list[str] = []
    service, sessions = _service(events=events)
    await service.close(_command())
    events.clear()
    sessions.snapshot = _snapshot(SurfaceSessionStatus.ACTIVE, 5)

    with pytest.raises(AppExplorationClosureError, match=error_pattern):
        await service.close(replace(_command(), **changes))

    assert sessions.snapshot.status is SurfaceSessionStatus.ACTIVE
    assert "driver.close_session" not in events
    assert "mark_lost" not in events


async def test_sealed_replay_wrong_surface_does_not_accept_unrelated_lost_session() -> None:
    events: list[str] = []
    service, sessions = _service(events=events)
    await service.close(_command())
    events.clear()
    sessions.snapshot = _snapshot(SurfaceSessionStatus.LOST, 5)

    with pytest.raises(AppExplorationClosureError, match="归属不一致"):
        await service.close(replace(_command(), surface_kind=SurfaceKind.IOS))

    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert events == []


async def test_driver_close_failure_marks_lost_without_reverting_sealed_index() -> None:
    events: list[str] = []
    service, _ = _service(events=events, driver_error=RuntimeError("device lost"))

    result = await service.close(_command())

    assert result.index.status.value == "sealed"
    assert result.session.status is SurfaceSessionStatus.LOST
    assert result.close_error == "device lost"
    assert events[-2:] == ["driver.close_session", "mark_lost"]


async def test_driver_and_lost_persistence_failures_preserve_driver_error() -> None:
    events: list[str] = []
    service, sessions = _service(
        events=events,
        driver_error=RuntimeError("device lost"),
        mark_lost_error=RuntimeError("mark lost unavailable"),
    )

    with pytest.raises(RuntimeError, match="device lost"):
        await service.close(_command())

    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSING
    assert events.count("mark_lost") == 2


async def test_closure_replay_reuses_sealed_index_and_persisted_result() -> None:
    events: list[str] = []
    service, _ = _service(events=events)
    first = await service.close(_command())
    events.clear()

    replay = await service.close(_command())

    assert replay.index == first.index
    assert replay.exploration_result == first.exploration_result
    assert replay.session.status is SurfaceSessionStatus.CLOSED
    assert events == []


async def test_sealed_replay_redrains_active_session_rules_before_driver_close() -> None:
    events: list[str] = []
    active_inspection = RecordingActiveSessionInspection(events)
    service, sessions = _service(
        events=events,
        active_session_inspection=active_inspection,
    )
    first = await service.close(_command())
    events.clear()
    calls_before_replay = len(active_inspection.calls)
    sessions.snapshot = _snapshot(SurfaceSessionStatus.ACTIVE, 5)

    replay = await service.close(_command())

    assert replay.index == first.index
    assert replay.exploration_result == first.exploration_result
    assert replay.session.status is SurfaceSessionStatus.CLOSED
    assert len(active_inspection.calls) == calls_before_replay + 1
    assert events == [
        "drain_active_session_inspection",
        "begin_draining",
        "close_readiness",
        "seal_for_close",
        "driver.close_session",
        "mark_closed",
    ]


async def test_sealed_index_recovers_missing_result_only_from_same_context() -> None:
    events: list[str] = []
    service, _, indexes, results = _service_with_manifest(
        events=events,
        manifest=None,
    )
    first = await service.close(_command())
    assert indexes.index is not None
    results.result = None
    events.clear()

    recovered = await service.close(_command())

    assert recovered.index == first.index
    assert (
        recovered.exploration_result.coverage_summary == first.exploration_result.coverage_summary
    )
    assert events == ["persist_result"]


async def test_sealed_index_without_result_rejects_changed_recovery_context() -> None:
    events: list[str] = []
    service, _, _, results = _service_with_manifest(events=events, manifest=None)
    await service.close(_command())
    results.result = None
    events.clear()

    with pytest.raises(AppExplorationClosureError, match="探索结果上下文"):
        await service.close(
            replace(
                _command(),
                coverage_summary={"unique_state_count": 999},
            )
        )

    assert events == []


async def test_concurrent_close_is_serialized_and_replays_one_sealed_result() -> None:
    events: list[str] = []
    service, sessions = _service(events=events)

    first, second = await asyncio.gather(
        service.close(_command()),
        service.close(_command()),
    )

    assert first.index == second.index
    assert first.exploration_result == second.exploration_result
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED
    assert events.count("final_observation") == 1
    assert events.count("create_index") == 1
    assert events.count("seal_index") == 1
    assert events.count("driver.close_session") == 1
