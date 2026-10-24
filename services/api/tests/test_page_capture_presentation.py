"""H5 写入口的展示层评估：持久化服务推导、仓储落库与同会话诊断继承。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from services.api.app.application.page_capture_persistence import (
    PageCapturePersistenceService,
    PersistedEvidenceArtifact,
)
from services.api.app.domain.evidence import build_evidence_manifest_reference
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_METADATA_KEY,
    PRESENTATION_PROTOCOL_VERSION,
    DiagnosticOperation,
)
from services.api.app.infrastructure.browser.page_capture import PageCaptureArtifact
from services.api.app.infrastructure.db.page_capture_repository import (
    PostgresPageCaptureRepository,
)
from services.api.tests.test_page_capture_persistence import (
    FakeConnection,
    FakeEvidenceArtifactStore,
    FakePageCaptureRepository,
    FakePool,
    _capture_result,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "33333333-3333-3333-3333-333333333333"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"


def _snapshot(*elements: dict[str, Any]) -> dict[str, Any]:
    return {"viewport": {"width": 390, "height": 844}, "elements": list(elements)}


def _content() -> dict[str, Any]:
    return {
        "selector": "div.content",
        "class_tokens": ["content"],
        "rect": {"x": 0, "y": 0, "width": 390, "height": 300},
        "style": {"display": "block"},
        "hit_test": {"center_hits_target": True},
    }


def _fullscreen_dialog() -> dict[str, Any]:
    return {
        "selector": "div.van-popup",
        "role": "dialog",
        "class_tokens": ["van-popup", "van-overlay"],
        "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
        "style": {"display": "block"},
        "hit_test": {"center_hits_target": True},
    }


def _result_with_snapshot(snapshot: Any, action_trace: list[dict[str, Any]] | None = None):
    result = _capture_result()
    return replace(
        result,
        artifacts=[*result.artifacts, PageCaptureArtifact("ui_snapshot", None, snapshot)],
        action_trace=action_trace if action_trace is not None else result.action_trace,
    )


async def test_persistence_service_derives_presentation_from_snapshot_and_trace() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_result_with_snapshot(
            _snapshot(_content(), _fullscreen_dialog()),
            action_trace=[
                {"action": "click", "fallback": "verified_high_z_obstruction_hide"},
            ],
        ),
    )

    presentation = repository.calls[0]["presentation"]
    assert presentation["protocol_version"] == PRESENTATION_PROTOCOL_VERSION
    assert presentation["topmost_layer_kind"] == "overlay"
    assert presentation["base_page_visibility"] == "fully_occluded"
    assert presentation["blocking_scope"] == "page"
    assert presentation["mutation_mode"] == "instrumented_diagnostic"
    assert presentation["diagnostic_operations"] == [
        {"kind": "style_hide_obstruction", "source": "verified_high_z_obstruction_hide"}
    ]
    assert "h5_ui_snapshot" in presentation["evidence_sources"]


async def test_persistence_service_uses_terminal_frame_of_snapshot_sequence() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)
    sequence = {
        "schema_version": "ui-snapshot-sequence.v1",
        "sequence": True,
        "frames": [
            {"index": 0, **_snapshot(_content(), _fullscreen_dialog())},
            {"index": 1, **_snapshot(_content())},
        ],
    }

    await service.persist(
        task_id="task-1", agent_run_id="run-1", result=_result_with_snapshot(sequence)
    )

    presentation = repository.calls[0]["presentation"]
    assert presentation["topmost_layer_kind"] == "page"
    assert presentation["base_page_visibility"] == "visible"
    assert presentation["mutation_mode"] == "natural"


async def test_persistence_service_without_snapshot_records_unknown_layer() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(task_id="task-1", agent_run_id="run-1", result=_capture_result())

    presentation = repository.calls[0]["presentation"]
    assert presentation["topmost_layer_kind"] == "unknown"
    assert presentation["hit_test_status"] == "unavailable"
    assert "ui_snapshot_unavailable" in presentation["uncertainty"]


def _artifact() -> PersistedEvidenceArtifact:
    return PersistedEvidenceArtifact(
        artifact_id="22222222-2222-2222-2222-222222222222",
        artifact_type="dom",
        storage_path="task/capture/dom.html",
        content_type="text/html; charset=utf-8",
        checksum="sha256:dom",
        size_bytes=13,
        metadata={"encoding": "utf-8"},
    )


def _natural_presentation() -> dict[str, Any]:
    return {
        "protocol_version": PRESENTATION_PROTOCOL_VERSION,
        "topmost_layer_kind": "page",
        "base_page_visibility": "visible",
        "blocking_scope": "none",
        "mutation_mode": "natural",
        "hit_test_status": "available",
        "evidence_sources": ["h5_ui_snapshot"],
        "diagnostic_operations": [],
        "uncertainty": [],
    }


def _inserted_metadata(connection: FakeConnection) -> dict[str, Any]:
    capture_call = next(
        call for call in connection.execute_calls if "INSERT INTO qc_page_capture" in call[0]
    )
    for arg in capture_call[1]:
        if isinstance(arg, str) and '"schema_version": "v2"' in arg:
            return json.loads(arg)
    raise AssertionError("没有找到采集行 metadata 参数")


async def test_repository_writes_presentation_into_capture_metadata_without_session() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    await repository.save_capture(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        page_capture_id=CAPTURE_ID,
        url="https://example.com",
        mode="goto",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[_artifact()],
        presentation=_natural_presentation(),
    )

    metadata = _inserted_metadata(pool.connection)
    assert metadata[PRESENTATION_METADATA_KEY] == _natural_presentation()
    lineage_queries = [
        query
        for query, _args in pool.connection.execute_calls
        if "instrumented_diagnostic" in query
    ]
    assert lineage_queries == []


async def test_repository_omits_presentation_key_when_not_provided() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    await repository.save_capture(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        page_capture_id=CAPTURE_ID,
        url="https://example.com",
        mode="goto",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[_artifact()],
    )

    assert PRESENTATION_METADATA_KEY not in _inserted_metadata(pool.connection)


class _LineageConnection(FakeConnection):
    """同会话里更早的采集带诊断标记时，返回它的展示层评估。"""

    def __init__(self, lineage: dict[str, Any] | str | None) -> None:
        super().__init__()
        self.lineage = lineage

    async def fetchrow(self, query: str, *args: Any) -> Any:
        normalized = " ".join(query.split())
        if "instrumented_diagnostic" in normalized:
            self.execute_calls.append((normalized, args))
            if self.lineage is None:
                return None
            return {
                "capture_id": "99999999-9999-9999-9999-999999999999",
                "presentation": self.lineage,
            }
        return await super().fetchrow(query, *args)


def _diagnostic_presentation() -> dict[str, Any]:
    return {
        **_natural_presentation(),
        "mutation_mode": "instrumented_diagnostic",
        "diagnostic_operations": [
            {
                "kind": "js_click_dispatch",
                "source": "overlay_direct_dispatch_after_pointer_interception",
            }
        ],
    }


async def test_repository_save_capture_writes_given_presentation_without_lineage_query() -> None:
    pool = FakePool()
    pool.connection = _LineageConnection(json.dumps(_diagnostic_presentation()))
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    await repository.save_capture(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        page_capture_id=CAPTURE_ID,
        url="https://example.com",
        mode="goto",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[_artifact()],
        surface_session_id="h5:session-1",
        session_command_sequence=7,
        finalization_epoch=0,
        presentation=_natural_presentation(),
    )

    # 继承在持久化服务里、建清单之前完成；仓储只写入它拿到的评估
    assert _inserted_metadata(pool.connection)[PRESENTATION_METADATA_KEY] == _natural_presentation()
    assert not any("instrumented_diagnostic" in query for query, _ in pool.connection.execute_calls)


async def test_repository_loads_session_diagnostic_lineage_as_inherited_operations() -> None:
    pool = FakePool()
    pool.connection = _LineageConnection(json.dumps(_diagnostic_presentation()))
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    inherited = await repository.load_session_diagnostic_lineage(
        task_id=TASK_ID, surface_session_id="h5:session-1"
    )

    assert [item.to_dict() for item in inherited] == [
        {
            "kind": "js_click_dispatch",
            "source": "overlay_direct_dispatch_after_pointer_interception",
            "origin_capture_id": "99999999-9999-9999-9999-999999999999",
        }
    ]
    lineage_call = next(
        call for call in pool.connection.execute_calls if "instrumented_diagnostic" in call[0]
    )
    assert lineage_call[1] == (TASK_ID, "h5:session-1", None)
    assert "ORDER BY page_seq DESC" in lineage_call[0]
    # 同一文档边界：本次没有文档起始时间时（None）SQL 按保守口径照常继承。
    assert "document_epoch_ms" in lineage_call[0]
    assert "$3::double precision IS NULL" in lineage_call[0]


async def test_repository_lineage_loader_scopes_inheritance_to_the_same_document() -> None:
    """整页重新加载后文档起始时间变化，继承查询只认同一文档内更早的诊断采集。"""
    pool = FakePool()
    pool.connection = _LineageConnection(json.dumps(_diagnostic_presentation()))
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    await repository.load_session_diagnostic_lineage(
        task_id=TASK_ID,
        surface_session_id="h5:session-1",
        document_epoch_ms=1788845247999.7,
    )

    lineage_call = next(
        call for call in pool.connection.execute_calls if "instrumented_diagnostic" in call[0]
    )
    assert lineage_call[1] == (TASK_ID, "h5:session-1", 1788845247999.7)
    assert (
        "abs( (metadata->'presentation'->>'document_epoch_ms')::double precision"
        " - $3::double precision ) < 1"
    ) in lineage_call[0]
    assert "(metadata->'presentation'->>'document_epoch_ms') IS NULL" in lineage_call[0]


async def test_repository_lineage_loader_returns_nothing_without_diagnostic_history() -> None:
    pool = FakePool()
    pool.connection = _LineageConnection(None)
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    assert await repository.load_session_diagnostic_lineage(
        task_id=TASK_ID, surface_session_id="h5:session-1"
    ) == ()


class _LineageRepository(FakePageCaptureRepository):
    def __init__(self, inherited: tuple[DiagnosticOperation, ...]) -> None:
        super().__init__()
        self.inherited = inherited
        self.lineage_calls: list[dict[str, str]] = []

    async def load_session_diagnostic_lineage(
        self,
        *,
        task_id: str,
        surface_session_id: str,
        document_epoch_ms: float | None = None,
    ):
        self.lineage_calls.append(
            {
                "task_id": task_id,
                "surface_session_id": surface_session_id,
                "document_epoch_ms": document_epoch_ms,
            }
        )
        return self.inherited


class _H5EvidenceFactsRepository(_LineageRepository):
    async def load_h5_session_auth(
        self,
        *,
        task_id: str,
        surface_session_id: str,
    ) -> str:
        assert task_id == "task-1"
        assert surface_session_id == "h5:session-1"
        return "authenticated"


async def test_persistence_service_inherits_lineage_before_building_manifest() -> None:
    store = FakeEvidenceArtifactStore()
    inherited = (
        DiagnosticOperation(
            kind="style_hide_obstruction",
            source="verified_high_z_obstruction_hide",
            origin_capture_id="99999999-9999-9999-9999-999999999999",
        ),
    )
    repository = _LineageRepository(inherited)
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_result_with_snapshot(_snapshot(_content())),
        surface_session_id="h5:session-1",
        session_command_sequence=3,
        finalization_epoch=0,
    )

    assert repository.lineage_calls == [
        {"task_id": "task-1", "surface_session_id": "h5:session-1", "document_epoch_ms": None}
    ]
    saved = repository.calls[0]
    presentation = saved["presentation"]
    assert presentation["mutation_mode"] == "instrumented_diagnostic"
    assert presentation["diagnostic_operations"] == [item.to_dict() for item in inherited]
    assert presentation["topmost_layer_kind"] == "page"
    # 清单里的每份附件都带同一份展示层事实，且与采集行一致
    manifest = saved["manifest"]
    assert manifest is not None
    assert all(
        artifact.to_dict()["metadata"][PRESENTATION_METADATA_KEY] == presentation
        for artifact in manifest.artifacts
    )


async def test_h5_persistence_stamps_declared_evidence_facts_into_capture_and_manifest() -> None:
    store = FakeEvidenceArtifactStore()
    repository = _H5EvidenceFactsRepository(())
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_result_with_snapshot(
            _snapshot(_content(), _fullscreen_dialog()),
            action_trace=[
                {"action": "click", "fallback": "verified_high_z_obstruction_hide"},
            ],
        ),
        surface_session_id="h5:session-1",
        session_command_sequence=3,
        finalization_epoch=0,
    )

    saved = repository.calls[0]
    assert saved["declared_evidence_flags"] == {
        "session_auth": "authenticated",
        "is_obstructed": True,
        "diagnostic": ["style_hide_obstruction"],
    }
    manifest = saved["manifest"]
    assert all(
        {
            key: artifact.to_dict()["metadata"][key]
            for key in ("session_auth", "is_obstructed", "diagnostic")
        }
        == saved["declared_evidence_flags"]
        for artifact in manifest.artifacts
    )


async def test_non_h5_persistence_does_not_stamp_h5_declared_evidence_facts() -> None:
    store = FakeEvidenceArtifactStore()
    repository = _H5EvidenceFactsRepository(())
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_result_with_snapshot(_snapshot(_content())),
        surface_session_id="web-v2:session-1",
        session_command_sequence=3,
        finalization_epoch=0,
    )

    saved = repository.calls[0]
    assert saved["declared_evidence_flags"] == {}


async def test_persistence_service_stamps_manifest_artifacts_with_presentation() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_result_with_snapshot(_snapshot(_content(), _fullscreen_dialog())),
    )

    saved = repository.calls[0]
    manifest = saved["manifest"]
    stamped = manifest.artifacts[0].to_dict()["metadata"][PRESENTATION_METADATA_KEY]
    assert stamped == saved["presentation"]
    assert stamped["base_page_visibility"] == "fully_occluded"
    # 清单哈希把展示层事实纳入身份，换一份评估就是另一份清单
    rebuilt = build_evidence_manifest_reference(
        task_id=manifest.task_id,
        session_id=manifest.session_id,
        capture_id=manifest.capture_id,
        manifest_version=manifest.manifest_version,
        artifacts=manifest.artifacts,
        quality_status=manifest.quality_status,
        quality_reason=manifest.quality_reason,
        created_at=manifest.created_at,
    )
    assert rebuilt.manifest_hash == manifest.manifest_hash
    without_stamp = build_evidence_manifest_reference(
        task_id=manifest.task_id,
        session_id=manifest.session_id,
        capture_id=manifest.capture_id,
        manifest_version=manifest.manifest_version,
        artifacts=tuple(replace(item, metadata={}) for item in manifest.artifacts),
        quality_status=manifest.quality_status,
        quality_reason=manifest.quality_reason,
        created_at=manifest.created_at,
    )
    assert without_stamp.manifest_hash != manifest.manifest_hash


async def test_persistence_service_passes_document_epoch_to_lineage_loader() -> None:
    """快照里的 document.time_origin_ms 既写进评估，也作为继承查询的同一文档边界。"""
    store = FakeEvidenceArtifactStore()
    repository = _LineageRepository(())
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)
    snapshot = {**_snapshot(_content()), "document": {"time_origin_ms": 1788845247999.7}}

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_result_with_snapshot(snapshot),
        surface_session_id="h5:session-1",
        session_command_sequence=3,
        finalization_epoch=0,
    )

    assert repository.lineage_calls == [
        {
            "task_id": "task-1",
            "surface_session_id": "h5:session-1",
            "document_epoch_ms": 1788845247999.7,
        }
    ]
    assert repository.calls[0]["presentation"]["document_epoch_ms"] == 1788845247999.7
