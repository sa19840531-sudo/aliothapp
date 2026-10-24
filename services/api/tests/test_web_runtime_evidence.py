"""Web Runtime 持久证据到阶段领域对象的投影回归。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.web_detail_candidate_extraction import (
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_readiness_contracts import (
    WebEvidenceRole,
    WebReadinessStatus,
)
from services.api.app.application.web_runtime_evidence import WebRuntimeEvidenceSource
from services.api.app.infrastructure.browser.page_capture import _web_readiness_metrics
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    WebCaptureArtifactIndex,
)
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics


class IndexReader:
    active_identity = True

    async def matches_active_capture(self, **kwargs):
        return self.active_identity

    capture_purpose = "web_page_ready_baseline"
    pipeline_metrics: dict[str, object] = {}
    url = "https://example.com/"
    requested_url: str | None = None
    redirect_chain: tuple[str, ...] = ()
    finalization_epoch = 0

    async def read(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> WebCaptureArtifactIndex:
        assert task_id == "task-1"
        return WebCaptureArtifactIndex(
            capture_id=capture_id,
            url=self.url,
            artifact_ids={
                "screenshot": "artifact-screenshot",
                "dom": "artifact-dom",
                "visible_text": "artifact-text",
            },
            captured_at=datetime.now(UTC),
            capture_purpose=self.capture_purpose,
            pipeline_metrics=self.pipeline_metrics,
            requested_url=self.requested_url,
            redirect_chain=self.redirect_chain,
            finalization_epoch=self.finalization_epoch,
        )


class SnapshotReader:
    async def read_ui_snapshot(self, *, task_id: str, page_capture_id: str):
        assert task_id == "task-1"
        assert page_capture_id == "capture-1"
        return {
            "schema_version": "ui-snapshot.v2",
            "web_capture_stage": "web_page_ready_baseline",
            "elements": [
                {
                    "selector": "#dialog .close",
                    "tag": "button",
                    "role": "button",
                    "text": "关闭广告",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": ["dialog"],
                    "web_obstruction_layer": "#dialog",
                    "web_obstruction_layer_kind": "dialog",
                    "web_obstruction_layer_structural": True,
                    "web_obstruction_close_semantics": True,
                    "web_obstruction_close_control_unique": True,
                    "web_obstruction_close_hit_safe": True,
                    "within_web_navigation": False,
                    "rect": {"x": 900, "y": 760, "width": 40, "height": 40},
                    "interactive": True,
                    "visible": True,
                    "effective_z_index": 100,
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }


@pytest.mark.asyncio
async def test_frontier_metadata_reader_never_reads_ui_snapshot() -> None:
    from services.api.app.application.web_runtime_evidence import (
        WebFrontierCaptureMetadataReaderAdapter,
    )

    class MetadataIndex(IndexReader):
        current = True

        async def matches_active_capture(self, **kwargs):
            return self.current if kwargs["current_only"] else True

    class ForbiddenSnapshotReader:
        async def read_ui_snapshot(self, **_kwargs):
            raise AssertionError("metadata-only 门禁不得读取 UI Snapshot")

    index = MetadataIndex()
    index.url = "https://example.com/menu"
    index.requested_url = "https://example.com/menu"
    index.finalization_epoch = 5
    index.pipeline_metrics = {"web_readiness": _stable_capture_metrics(page_settle_complete=True)}
    reader = WebFrontierCaptureMetadataReaderAdapter(
        source=WebRuntimeEvidenceSource(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            index_reader=index,
            snapshot_reader=ForbiddenSnapshotReader(),
        )
    )

    metadata = await reader.read(capture_id="capture-1", depth=2)

    assert metadata.current is True
    assert metadata.ready is True
    assert metadata.depth == 2
    assert metadata.final_url == "https://example.com/menu"


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [True, False])
async def test_home_redirect_requires_capture_scoped_logical_target_proof(trusted):
    class TargetRepository:
        async def find(self, **kwargs):
            assert kwargs == {
                "task_id": "task-1",
                "agent_run_id": "run-1",
                "session_id": "web:run-1",
                "source_capture_id": "capture-1",
                "url": "https://example.com/",
            }
            if not trusted:
                return None
            from services.api.app.domain.page.web_url_identity import WebUrlIdentityPolicy

            return {
                "logical_page_id": WebUrlIdentityPolicy().logical_id(
                    "https://example.com/index.html"
                )
            }

    index = IndexReader()
    index.url = "https://example.com/index.html"
    index.pipeline_metrics = {"web_readiness": _stable_capture_metrics(page_settle_complete=True)}
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=index,
        snapshot_reader=SnapshotReader(),
        logical_target_repository=TargetRepository(),
    )
    capture = await source.read_ready(capture_id="capture-1", page_role="home")
    assert capture.structure_facts.is_canonical_root_url is trusted
    assert capture.page_role == ("home" if trusted else "menu")


@pytest.mark.asyncio
async def test_ready_capture_projects_redirect_identity_for_frontier_aliases() -> None:
    index = IndexReader()
    index.url = "https://example.com/new"
    index.requested_url = "https://example.com/old"
    index.redirect_chain = (
        "https://example.com/old",
        "https://example.com/new",
    )
    index.finalization_epoch = 9
    index.pipeline_metrics = {"web_readiness": _stable_capture_metrics(page_settle_complete=True)}
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=index,
        snapshot_reader=SnapshotReader(),
    )

    capture = await source.read_ready(capture_id="capture-1", page_role="menu")

    assert capture.requested_url == "https://example.com/old"
    assert capture.redirect_chain[-1] == capture.url
    assert capture.finalization_epoch == 9
    assert len(capture.capture_state_fingerprint) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "identity", "readiness"])
async def test_current_handoff_uses_authority_and_original_ready_fences(failure):
    from services.api.app.application.web_runtime_evidence import WebReadyCaptureReaderAdapter

    class CurrentIndex(IndexReader):
        async def read_current_capture_id(self, **kwargs):
            assert kwargs == dict(task_id="task-1", agent_run_id="run-1", session_id="web:run-1")
            return "capture-1"

        async def matches_active_capture(self, **kwargs):
            assert kwargs["current_only"] is True
            assert kwargs["capture_id"] == "capture-1"
            return failure != "identity"

    index = CurrentIndex()
    index.pipeline_metrics = {
        "web_readiness": _stable_capture_metrics(page_settle_complete=failure != "readiness")
    }
    reader = WebReadyCaptureReaderAdapter(
        source=WebRuntimeEvidenceSource(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            index_reader=index,
            snapshot_reader=SnapshotReader(),
        )
    )
    if failure:
        with pytest.raises(RuntimeError, match="IDENTITY|NOT_FORMAL"):
            await reader.read_current(page_role="menu")
    else:
        assert (await reader.read_current(page_role="menu")).capture_id == "capture-1"


@pytest.mark.asyncio
async def test_old_capture_is_rejected_before_context_can_relabel_it():
    reader = IndexReader()
    reader.active_identity = False
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=reader,
        snapshot_reader=SnapshotReader(),
    )
    with pytest.raises(RuntimeError, match="IDENTITY"):
        await source.read_page(capture_id="capture-1")


@pytest.mark.asyncio
async def test_page_evidence_receives_trusted_obstruction_state_fingerprint() -> None:
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=IndexReader(),
        snapshot_reader=SnapshotReader(),
    )

    evidence = await source.read_page(capture_id="capture-1")

    assert evidence.obstruction_state_fingerprint is not None
    assert len(evidence.obstruction_state_fingerprint) == 64
    assert len(evidence.candidates) == 1
    assert evidence.benign_ordinary_overlays_only is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("overlay_width", "expected"), ((239.999, True), (240, False)))
async def test_page_evidence_marks_only_sub_30_percent_ordinary_overlays_benign(
    overlay_width,
    expected,
) -> None:
    class SmallOverlaySnapshotReader:
        async def read_ui_snapshot(self, *, task_id: str, page_capture_id: str):
            assert task_id == "task-1"
            assert page_capture_id == "capture-1"
            return {
                "viewport": {"width": 1000, "height": 800},
                "elements": [
                    {
                        "selector": "main",
                        "tag": "main",
                        "visible": True,
                        "rect": {"x": 100, "y": 100, "width": 800, "height": 600},
                    },
                    {
                        "selector": "#floating",
                        "tag": "div",
                        "visible": True,
                        "rect": {
                            "x": 100,
                            "y": 100,
                            "width": overlay_width,
                            "height": 600,
                        },
                        "web_obstruction_layer": "#floating",
                        "web_obstruction_layer_kind": "overlay",
                        "web_obstruction_layer_structural": True,
                    },
                    {
                        "selector": "#floating button",
                        "tag": "button",
                        "role": "button",
                        "text": "关闭",
                        "visible": True,
                        "interactive": True,
                        "effective_z_index": 100,
                        "rect": {"x": 270, "y": 110, "width": 20, "height": 20},
                        "web_obstruction_layer": "#floating",
                        "web_obstruction_layer_kind": "overlay",
                        "web_obstruction_layer_structural": True,
                        "web_obstruction_close_semantics": True,
                        "within_web_navigation": False,
                        "hit_test": {"center_hits_target": True},
                    },
                ],
            }

    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=IndexReader(),
        snapshot_reader=SmallOverlaySnapshotReader(),
    )

    evidence = await source.read_page(capture_id="capture-1")

    assert evidence.benign_ordinary_overlays_only is expected
    assert len(evidence.candidates) == 1


def test_ordinary_overlay_without_trusted_subject_geometry_fails_closed() -> None:
    from services.api.app.application.web_obstruction_geometry import (
        web_has_only_benign_ordinary_overlays,
    )

    assert not web_has_only_benign_ordinary_overlays(
        {
            "viewport": {"width": 1000, "height": 800},
            "elements": [
                {
                    "selector": "#floating",
                    "tag": "div",
                    "visible": True,
                    "rect": {"x": 0, "y": 0, "width": 200, "height": 200},
                    "web_obstruction_layer": "#floating",
                    "web_obstruction_layer_kind": "overlay",
                    "web_obstruction_layer_structural": True,
                }
            ],
        }
    )


def test_ordinary_overlay_uses_layer_rect_and_visible_subject_region() -> None:
    from services.api.app.application.web_obstruction_geometry import (
        web_ordinary_overlay_subject_overlap,
    )

    close_control = {
        "selector": "#overlay .close",
        "tag": "button",
        "visible": True,
        "rect": {"x": 939, "y": 870, "width": 42, "height": 42},
        "web_obstruction_layer": "#overlay",
        "web_obstruction_layer_rect": {
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
        },
        "web_obstruction_layer_kind": "geometric_overlay",
        "web_obstruction_layer_structural": True,
        "web_obstruction_close_semantics": True,
    }
    snapshot = {
        "viewport": {"width": 1920, "height": 1080},
        "elements": [
            {
                "selector": "main",
                "tag": "main",
                "visible": True,
                "rect": {"x": 430, "y": 150, "width": 1060, "height": 13118},
            },
            close_control,
        ],
    }

    assert web_ordinary_overlay_subject_overlap(snapshot, close_control) == 1.0


@pytest.mark.asyncio
async def test_ready_reader_projects_primary_supplement_stage_from_persisted_metadata() -> None:
    reader = IndexReader()
    reader.capture_purpose = "web_primary_content_evidence"
    reader.pipeline_metrics = {
        "web_readiness": {
            **_stable_capture_metrics(),
            "role": "classification",
            "identity_before": "https://example.com/",
            "identity_after": "https://example.com/",
            "artifacts_same_epoch": True,
            "capture_failed": False,
            "timed_out": False,
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
            "minimum_dom_quiet_ms": 600,
            "network_capability": "observable",
            "network_required": True,
            "critical_network_pending": 0,
        }
    }
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=reader,
        snapshot_reader=SnapshotReader(),
    )

    capture = await source.read_ready(capture_id="capture-1", page_role="menu")

    assert capture.capture_purpose == "web_primary_content_evidence"
    assert capture.web_capture_stage == "web_primary_content_evidence"


def test_primary_content_stage_emits_classification_readiness_metrics() -> None:
    """删除主体补图阶段映射时，本测试应阻止无就绪事实的补图进入分类。"""

    metrics = _web_readiness_metrics(
        web_capture_stage="web_primary_content_evidence",
        identity_before="https://example.com/news",
        identity_after="https://example.com/news",
        page_settle_complete=True,
        page_inspection_ready=True,
        readiness={
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
        },
        network_observable=True,
        critical_network_pending=0,
    )

    assert metrics is not None
    assert metrics["role"] == "classification"


@pytest.mark.asyncio
async def test_ready_reader_rejects_capture_without_web_readiness() -> None:
    """删除缺省拒绝分支时，本测试应捕获无就绪事实被误当正式证据。"""

    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=IndexReader(),
        snapshot_reader=SnapshotReader(),
    )

    with pytest.raises(RuntimeError, match="WEB_CAPTURE_READINESS_MISSING"):
        await source.read_ready(capture_id="capture-1", page_role="menu")


@pytest.mark.asyncio
async def test_page_evidence_projects_persisted_web_readiness_assessment() -> None:
    reader = IndexReader()
    reader.pipeline_metrics = {
        "web_readiness": {
            **_stable_capture_metrics(),
            "role": "classification",
            "identity_before": "https://example.com/",
            "identity_after": "https://example.com/",
            "artifacts_same_epoch": True,
            "capture_failed": False,
            "timed_out": False,
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
            "minimum_dom_quiet_ms": 600,
            "network_capability": "observable",
            "network_required": True,
            "critical_network_pending": 0,
        }
    }
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=reader,
        snapshot_reader=SnapshotReader(),
    )

    evidence = await source.read_page(capture_id="capture-1")

    assert evidence.readiness is not None
    assert evidence.readiness.role is WebEvidenceRole.CLASSIFICATION
    assert evidence.readiness.status is WebReadinessStatus.STABLE
    assert evidence.readiness.can_serve_evidence is True


@pytest.mark.asyncio
async def test_ready_reader_reassesses_same_frame_for_inspection_role() -> None:
    """分类帧必须基于同一不可变信号另行取得 inspection 就绪结论。"""

    reader = IndexReader()
    reader.pipeline_metrics = {
        "web_readiness": {
            **_stable_capture_metrics(),
            "role": "classification",
            "identity_before": "https://example.com/",
            "identity_after": "https://example.com/",
            "artifacts_same_epoch": True,
            "capture_failed": False,
            "timed_out": False,
            "core_present": True,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
            "minimum_dom_quiet_ms": 600,
            "network_capability": "observable",
            "network_required": True,
            "critical_network_pending": 0,
        }
    }
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=reader,
        snapshot_reader=SnapshotReader(),
    )

    capture = await source.read_ready(capture_id="capture-1", page_role="menu")

    assert capture.inspection_readiness is not None
    assert capture.inspection_readiness.role is WebEvidenceRole.INSPECTION
    assert capture.inspection_readiness.status is WebReadinessStatus.STABLE
    assert capture.inspection_readiness.can_serve_evidence is True


@pytest.mark.asyncio
async def test_ready_reader_rejects_popup_observation_as_formal_evidence() -> None:
    reader = IndexReader()
    reader.pipeline_metrics = {
        "web_readiness": {
            "role": "classification",
            "identity_before": "https://example.com/",
            "identity_after": "https://example.com/",
            "artifacts_same_epoch": True,
            "capture_failed": False,
            "timed_out": False,
            "core_present": False,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
            "minimum_dom_quiet_ms": 800,
            "network_capability": "observable",
            "network_required": False,
            "critical_network_pending": 0,
            "blocking_layer": "popup",
        }
    }
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=reader,
        snapshot_reader=SnapshotReader(),
    )

    with pytest.raises(RuntimeError, match="WEB_CAPTURE_NOT_FORMAL_EVIDENCE"):
        await source.read_ready(capture_id="capture-1", page_role="menu")


@pytest.mark.asyncio
async def test_detail_target_observation_rebinds_from_new_capture_snapshot() -> None:
    original_snapshot = {
        "capture_id": "capture-original",
        "structure_signature": "signature-before-scroll",
        "elements": [
            {
                "selector": "#card-1 a",
                "tag": "a",
                "role": "link",
                "text": "视频标题一",
                "href": "/video/1",
                "rect": {"x": 100, "y": 1380, "width": 300, "height": 180},
                "interactive": True,
                "visible": True,
                "hit_test": {"center_hits_target": False},
                "within_repeated_structure": True,
                "ancestor_class_tokens": ["video-card", "video-grid"],
            }
        ],
    }
    rebound_snapshot = {
        "capture_id": "capture-1",
        "structure_signature": "signature-after-scroll",
        "document_epoch": "epoch-2",
        "viewport": {"width": 1920, "height": 1080, "scroll_y": 800},
        "document": {"scroll_height": 2400},
        "elements": [
            {
                "selector": "main article:nth-of-type(1) a",
                "tag": "a",
                "role": "link",
                "text": "视频标题一",
                "href": "/video/1",
                "rect": {"x": 100, "y": 240, "width": 300, "height": 180},
                "interactive": True,
                "visible": True,
                "disabled": False,
                "hit_test": {"center_hits_target": True},
                "within_repeated_structure": True,
                "ancestor_class_tokens": ["video-card", "video-grid"],
            }
        ],
    }
    candidate = detail_candidates_from_ready_snapshot(
        original_snapshot,
        list_page_type="video_list",
        origin="https://example.com",
        current_url="https://example.com/list",
    )[0]

    class DetailSnapshotReader:
        async def read_ui_snapshot(self, *, task_id, page_capture_id):
            assert task_id == "task-1"
            assert page_capture_id == "capture-1"
            return rebound_snapshot

    reader = IndexReader()
    reader.url = "https://example.com/list"
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=reader,
        snapshot_reader=DetailSnapshotReader(),
    )

    observation = await source.read_detail_target_observation(
        capture_id="capture-1",
        candidate=candidate,
        list_page_type="video_list",
        origin="https://example.com",
        current_url="https://example.com/list",
        previous_scroll_offset=0,
    )

    assert observation.source_epoch == "epoch-2"
    assert observation.matching_target_count == 1
    assert observation.target_visible is True
    assert observation.target_enabled is True
    assert observation.target_hit_test_passed is True
    assert observation.scroll_offset == 800
    assert observation.previous_scroll_offset == 0
