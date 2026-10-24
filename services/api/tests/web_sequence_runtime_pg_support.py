"""真实 readiness/Coordinator/PG/默认通知组合，仅浏览器测量、模型和对象存储受控替代。"""

import base64
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.capture_coordinator import CaptureCoordinator, CapturePhase
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.application.web_exploration_workflow import web_capture_profile
from services.api.app.application.web_page_readiness import (
    WebObstructionAssessment,
    WebPageReadinessCoordinator,
)
from services.api.app.application.web_runtime_evidence import (
    WebPageEvidenceReaderAdapter,
    WebRuntimeEvidenceSource,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.evidence.presentation import (
    BasePageVisibility,
    BlockingScope,
    PresentationAssessment,
    TopmostLayerKind,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    SurfaceKind,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.page_capture_repository import PostgresPageCaptureRepository
from services.api.app.infrastructure.db.session_command_gate import PostgresSessionCommandGate
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.tests.test_capture_coordinator import (
    RecordingEvidenceExitGate,
    RecordingInteractionExecutor,
)
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics


async def prepare_sequence_with_default_notification(
    connection,
    pool,
    ids,
    monkeypatch,
    *,
    interaction,
    native_video_count=None,
    notification_hook=None,
):
    from services.api.app.application import inspection_composition as composition
    from services.api.app.core.config import get_settings
    from services.api.app.infrastructure.db import (
        web_capability_revision_repository as capability_module,
    )
    from services.api.app.infrastructure.db import (
        web_evidence_sequence_repository as sequence_module,
    )

    store, snapshots = {}, {}

    class Storage:
        async def read(self, storage_path):
            return store[storage_path]

    for name in (
        "PostgresWebCaptureEvidenceRepository",
        "PostgresH5EvidenceInventoryRepository",
        "PostgresH5InspectionPublicationRepository",
        "PostgresWebEvidenceManifestRepository",
    ):
        original = getattr(composition, name)
        monkeypatch.setattr(
            composition, name, lambda _cls=original, **kwargs: _cls(pool=pool, **kwargs)
        )
    original_sequence = sequence_module.PostgresWebEvidenceSequenceRepository
    monkeypatch.setattr(
        sequence_module,
        "PostgresWebEvidenceSequenceRepository",
        lambda **kwargs: original_sequence(pool=pool, **kwargs),
    )
    original_capability = capability_module.PostgresWebCapabilityRevisionRepository
    monkeypatch.setattr(
        capability_module,
        "PostgresWebCapabilityRevisionRepository",
        lambda **kwargs: original_capability(pool=pool, **kwargs),
    )
    monkeypatch.setattr(composition, "build_evidence_artifact_store", lambda settings: Storage())
    notification = composition.build_default_web_page_inspection_notification(
        settings=get_settings()
    )
    await connection.execute(
        "UPDATE qc_task SET task_config=task_config||$2::jsonb WHERE id=$1::uuid",
        ids["task"],
        json.dumps(
            {
                "execution_mode": "full_inspection",
                "web_inspection_notification_mode": "enforced",
                "h5_inspection_notification_mode": "enforced",
                "_task_target": {"platform": "web"},
            }
        ),
    )
    await connection.execute(
        "UPDATE qc_agent_run SET input_context=input_context||$2::jsonb WHERE id=$1::uuid",
        ids["run"],
        json.dumps(
            {"task_config": {"workflow_version": "v2", "_task_target": {"platform": "web"}}}
        ),
    )
    await connection.execute(
        "INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,capture_status) "
        "VALUES($1::uuid,$2::uuid,$3::uuid,1,'t9-source','https://example.test/list','captured') ON CONFLICT(id) DO NOTHING",
        ids["capture"],
        ids["task"],
        ids["run"],
    )
    state = {"popup": interaction}
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY9sAAAAASUVORK5CYII="
    )

    class Writer:
        async def capture(self, request):
            capture_id = str(uuid4())
            role = "after_action" if request.phase is CapturePhase.AFTER_ACTION else "before_action"
            elements = [{"tag": "main", "selector": "#body", "visible": True, "text": "原业务正文"}]
            if state["popup"]:
                elements.append(
                    {
                        "tag": "button",
                        "role": "button",
                        "selector": "#close",
                        "text": "关闭广告",
                        "class_tokens": ["modal-close"],
                        "ancestor_class_tokens": ["modal"],
                        "web_obstruction_layer": "#modal",
                        "web_obstruction_layer_kind": "modal",
                        "web_obstruction_layer_structural": True,
                        "web_obstruction_close_semantics": True,
                        "within_web_navigation": False,
                        "interactive": True,
                        "visible": True,
                        "effective_z_index": 900,
                        "hit_test": {"center_hits_target": True},
                        "rect": {"x": 100, "y": 30, "width": 50, "height": 50},
                    }
                )
            snapshot = {"web_capture_stage": "web_page_ready_baseline", "elements": elements}
            if native_video_count is not None:
                snapshot.update(
                    document={"time_origin_ms": 1000},
                    viewport={"scroll_x": 0, "scroll_y": 0},
                    web_native_structure={
                        "schema_version": "web-native-structure.v1",
                        "scope": "current_document_light_dom",
                        "predicate": "native_video_element_present",
                        "document_time_origin_ms": 1000,
                        "complete": True,
                        "native_video_count": native_video_count,
                        "unobservable_reasons": [],
                        "status": "present" if native_video_count else "local_absent",
                    },
                )
            snapshots[capture_id] = snapshot
            refs, artifacts = [], []
            for kind, mime, content in (
                ("screenshot", "image/png", png),
                ("dom", "text/html", b"<main>original body</main>"),
                ("visible_text", "text/plain", b"original body"),
                ("ui_snapshot", "application/json", json.dumps(snapshot).encode()),
            ):
                artifact_id = str(uuid4())
                path = f"{ids['task']}/{capture_id}/{kind}"
                store[path] = content
                checksum = "sha256:" + sha256(content).hexdigest()
                refs.append(
                    ArtifactReference(
                        artifact_id=artifact_id,
                        artifact_type=kind,
                        role=role,
                        storage_path=path,
                        content_type=mime,
                        checksum=checksum,
                        size_bytes=len(content),
                    )
                )
                artifacts.append(
                    PersistedEvidenceArtifact(
                        artifact_id, kind, path, mime, checksum, len(content), {}
                    )
                )
            metrics = _stable_capture_metrics()
            if not interaction:
                metrics["visible_loader"] = True
                metrics["settle_complete"] = False
                metrics["quorum_met"] = False
            manifest = build_evidence_manifest_reference(
                task_id=ids["task"],
                session_id=ids["session"],
                capture_id=capture_id,
                manifest_version=1,
                artifacts=tuple(refs),
                quality_status=EvidenceQualityStatus.PENDING,
                quality_reason="pending_verification",
                created_at=datetime.now(UTC),
            )
            presentation = PresentationAssessment(
                topmost_layer_kind=TopmostLayerKind.PAGE,
                base_page_visibility=BasePageVisibility.VISIBLE,
                blocking_scope=BlockingScope.NONE,
            ).to_dict()
            await PostgresPageCaptureRepository(pool=pool).save_capture(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                page_capture_id=capture_id,
                url="https://example.test/list",
                mode="web",
                reachable=True,
                title="本地业务页",
                status_code=200,
                action_log=[],
                artifacts=artifacts,
                metrics={"web_readiness": metrics},
                capture_role=role,
                surface_session_id=ids["session"],
                session_command_sequence=request.session_command_sequence,
                finalization_epoch=0,
                parent_page_capture_id=request.parent_capture_id,
                action_execution_id=request.action_execution_id,
                manifest=manifest,
                presentation=presentation,
            )
            await connection.execute(
                "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
                capture_id,
                ids["session"],
            )
            return CaptureRef(
                task_id=ids["task"],
                session_id=ids["session"],
                capture_id=capture_id,
                surface_kind=SurfaceKind.WEB,
                capture_kind=CaptureKind.PAGE,
                role=request.directive.role,
                artifact_ids=tuple(ref.artifact_id for ref in refs),
                parent_capture_id=request.parent_capture_id,
                action_execution_id=request.action_execution_id,
            )

    class Driver(RecordingInteractionExecutor):
        async def execute(self, request):
            if request.intent.action_type is InteractionOperation.CLICK:
                state["popup"] = False
            return await super().execute(request)

    class SnapshotReader:
        async def read_ui_snapshot(self, *, task_id, page_capture_id):
            return snapshots[page_capture_id]

    class Assessor:
        async def assess_obstruction(self, **kwargs):
            candidates = kwargs["candidates"]
            return WebObstructionAssessment(
                obstruction_present=bool(candidates),
                dismiss_candidate_id=candidates[0]["candidate_id"] if candidates else None,
                confidence=0.99,
                reason="受控结构判定",
                human_verification_present=False,
                age_gate_present=False,
            )

    coordinator = CaptureCoordinator(
        capture_writer=Writer(),
        interaction_executor=Driver([]),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        session_command_gate=PostgresSessionCommandGate(
            repository=PostgresSurfaceSessionRepository(pool=pool),
            lease_owner="t9-readiness",
            session_owner="web-v2-workflow",
        ),
        action_execution_repository=PostgresActionExecutionRepository(pool=pool),
    )
    source = WebRuntimeEvidenceSource(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=PostgresWebCaptureEvidenceRepository(pool=pool),
        snapshot_reader=SnapshotReader(),
    )
    entry_id = "t9-entry-" + str(uuid4())
    entry_source = await connection.fetchval(
        "SELECT current_capture_id::text FROM qc_surface_session WHERE session_id=$1",
        ids["session"],
    )
    intent = ActionIntent(
        task_id=ids["task"],
        session_id=ids["session"],
        intent_id=entry_id,
        idempotency_key=entry_id,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=entry_source,
        action_type=InteractionOperation.OBSERVE,
        semantic_target="读取已有页面",
        purpose="本地回归",
        capture_profile=web_capture_profile("web-page-readiness.v1"),
        deadline=source.deadline,
        parameters={"web_capture_stage": "web_page_probe"},
    )
    probe = (
        await coordinator.coordinate(
            CaptureCoordinationCommand(agent_run_id=ids["run"], intent=intent)
        )
    ).after_capture.capture_id
    published = []

    async def notify(**kwargs):
        method = (
            notification.notify_readiness_interaction
            if interaction
            else notification.notify_readiness_failure
        )

        async def publish_sequence():
            return await method(
                task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"], **kwargs
            )

        published.append(
            await notification_hook(publish_sequence, kwargs, source, notification)
            if notification_hook
            else await publish_sequence()
        )

    async def no_wait(_):
        return None

    result = await WebPageReadinessCoordinator(
        assessor=Assessor(),
        coordinator=coordinator,
        evidence_reader=WebPageEvidenceReaderAdapter(source=source),
        sleeper=no_wait,
        failure_sequence_notifier=None if interaction else notify,
        interaction_sequence_notifier=notify if interaction else None,
    ).prepare(source_capture_id=probe, page_role="menu")
    return result, published, store, source, notification
