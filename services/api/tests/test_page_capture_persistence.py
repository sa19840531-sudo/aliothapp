"""页面采集产物持久化服务与本地证据存储测试。"""

import asyncio
import base64
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from urllib3.exceptions import ProtocolError

from services.api.app.application.evidence_manifest_writer import (
    build_pending_page_capture_manifest,
)
from services.api.app.application.page_capture_persistence import (
    PageCapturePersistenceService,
    PersistedEvidenceArtifact,
)
from services.api.app.core.config import EvidenceStorageSettings
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.exploration.page_graph import normalize_page_identity
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureResult,
)
from services.api.app.infrastructure.db.page_capture_repository import (
    PostgresPageCaptureRepository,
)
from services.api.app.infrastructure.storage import minio_evidence_store
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store
from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
)
from services.api.app.infrastructure.storage.minio_evidence_store import (
    MinioEvidenceArtifactStore,
)


class FakeEvidenceArtifactStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def store(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: Any,
    ) -> PersistedEvidenceArtifact:
        self.calls.append(
            {
                "task_id": task_id,
                "page_capture_id": page_capture_id,
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "content": content,
            }
        )
        return PersistedEvidenceArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            storage_path=f"{task_id}/{page_capture_id}/{artifact_id}.data",
            content_type="application/octet-stream",
            checksum="sha256:" + "a" * 64,
            size_bytes=10,
            metadata={},
        )

    async def delete(self, storage_path: str) -> None:
        return None


class FakePageCaptureRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def save_capture(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return 3


class FakeMinioClient:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []
        self.remove_calls: list[tuple[str, str]] = []
        self.objects: dict[tuple[str, str], bytes] = {}
        self.object_metadata: dict[tuple[str, str], dict[str, str]] = {}
        self.get_object_errors: list[BaseException] = []

    def put_object(self, **kwargs: Any) -> None:
        content = kwargs["data"].read()
        self.put_calls.append({**kwargs, "content": content})
        self.objects[(kwargs["bucket_name"], kwargs["object_name"])] = content
        self.object_metadata[(kwargs["bucket_name"], kwargs["object_name"])] = dict(
            kwargs.get("metadata") or {}
        )

    def _put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: bytes,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> None:
        del query_params
        if headers and headers.get("If-None-Match") == "*" and (
            bucket_name,
            object_name,
        ) in self.objects:
            from minio.error import S3Error

            raise S3Error(
                None,
                "PreconditionFailed",
                "object already exists",
                object_name,
                "request",
                "host",
            )
        self.put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            data=BytesIO(data),
            length=len(data),
            content_type=(headers or {}).get("Content-Type", "application/octet-stream"),
            metadata={
                key.removeprefix("X-Amz-Meta-"): value
                for key, value in (headers or {}).items()
                if key.lower().startswith("x-amz-meta-")
            },
        )

    def stat_object(self, bucket_name: str, object_name: str) -> SimpleNamespace:
        key = (bucket_name, object_name)
        if key not in self.objects:
            raise FileNotFoundError(object_name)
        return SimpleNamespace(
            size=len(self.objects[key]),
            metadata=self.object_metadata.get(key, {}),
        )

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        self.remove_calls.append((bucket_name, object_name))

    def get_object(self, bucket_name: str, object_name: str) -> "FakeMinioResponse":
        if self.get_object_errors:
            raise self.get_object_errors.pop(0)
        return FakeMinioResponse(self.objects[(bucket_name, object_name)])


class FakeMinioResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self) -> bytes:
        return self.content

    def close(self) -> None:
        return None

    def release_conn(self) -> None:
        return None


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.reject_capture_insert = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((" ".join(query.split()), args))
        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        normalized_query = " ".join(query.split())
        self.execute_calls.append((normalized_query, args))
        if "MAX(page_seq)" in normalized_query:
            return 2
        if "INSERT INTO qc_page_capture" in normalized_query and self.reject_capture_insert:
            return None
        return args[0]

    async def fetchrow(self, query: str, *args: Any) -> Any:
        normalized_query = " ".join(query.split())
        self.execute_calls.append((normalized_query, args))
        if "INSERT INTO qc_evidence_manifest" in normalized_query:
            return {"manifest_id": args[0], "created": True}
        return None


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _capture_result() -> PageCaptureResult:
    return PageCaptureResult(
        execution_status="success",
        mode="goto",
        reachable=True,
        final_url="https://example.com/home",
        title="首页",
        status_code=200,
        artifacts=[
            PageCaptureArtifact("dom", None, "<html></html>"),
            PageCaptureArtifact("visible_text", None, "页面文案"),
            PageCaptureArtifact(
                "screenshot",
                None,
                {"encoding": "base64", "data": base64.b64encode(b"png").decode()},
            ),
            PageCaptureArtifact("action_trace", None, [{"action": "click"}]),
        ],
        console_logs=[],
        network_requests=[],
        action_trace=[{"action": "click"}],
        post_action_snapshot=None,
    )


@pytest.mark.parametrize("session_id,shadow,degraded,expected", [
    ("h5:run-1", False, False, True),
    ("web:run-1", False, False, False),
    (None, False, False, False),
    ("h5:run-1", True, False, False),
    ("h5:run-1", False, True, False),
])
async def test_h5_capture_retains_own_snapshot_identity_without_inventing_page_type(
    session_id, shadow, degraded, expected,
) -> None:
    surface = {
        "protocol_version": "logical_surface.v1", "shadow": shadow,
        "render_profile": {"kind": "dom_spa", "status": "confirmed"},
        "evidence_quality": "high", "degraded_reasons": [],
        "identity_fingerprint": "sha256:" + "a" * 64,
        "state_fingerprint": "sha256:" + "b" * 64,
    }
    result = _capture_result()
    result.artifacts.append(PageCaptureArtifact("ui_snapshot", None, {
        "schema_version": "ui-snapshot.v2", "logical_surface": surface,
        "capture_degraded": degraded,
    }))
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(
        repository=repository, artifact_store=FakeEvidenceArtifactStore(),
    )
    await service.persist(
        task_id="task-1", agent_run_id="run-1", result=result,
        capture_role="probe_result_replay", surface_session_id=session_id,
        session_command_sequence=2 if session_id else None,
        finalization_epoch=0 if session_id else None,
    )
    flags = repository.calls[0]["declared_evidence_flags"]
    assert (flags.get("logical_surface") == surface) is expected
    assert "page_type" not in flags
    identity = normalize_page_identity(
        metadata=flags, surface_kind="web", capture_id="capture-new-frame",
    )
    assert (identity.key == "logical:" + surface["identity_fingerprint"]) is expected
    assert (identity.status == "confirmed") is expected


async def test_persistence_service_stores_supported_artifacts_and_page_capture() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_capture_result(),
    )

    assert persisted.page_seq == 3
    assert len(persisted.artifacts) == 4
    assert [call["artifact_type"] for call in store.calls] == [
        "dom",
        "visible_text",
        "screenshot",
        "action_trace",
    ]
    saved = repository.calls[0]
    assert saved["task_id"] == "task-1"
    assert saved["agent_run_id"] == "run-1"
    assert saved["action_log"] == [{"action": "click"}]
    assert saved["artifacts"] == persisted.artifacts
    assert saved["inherit_parent_semantics"] is False
    assert "capture_purpose" not in saved


async def test_persistence_service_passes_explicit_web_capture_purpose_only_when_requested(
) -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_capture_result(),
        capture_role="supplemental",
        capture_purpose="web_full_page_supplemental",
    )

    assert repository.calls[0]["capture_purpose"] == "web_full_page_supplemental"


async def test_persistence_service_stores_media_state_as_first_class_evidence() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)
    result = _capture_result()
    result = replace(
        result,
        artifacts=[
            *result.artifacts,
            PageCaptureArtifact(
                "media_state",
                None,
                {
                    "schema_version": "media-state.v1",
                    "items": [{"current_time": 30, "duration": 120}],
                },
            ),
        ],
    )

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=result,
    )

    assert "media_state" in [item.artifact_type for item in persisted.artifacts]
    assert store.calls[-1]["artifact_type"] == "media_state"


async def test_persistence_service_retries_entry_capture_without_stale_surface_binding() -> None:
    class StaleSurfaceBindingRepository(FakePageCaptureRepository):
        async def save_capture(self, **kwargs: Any) -> int:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError(
                    "Page Capture 的父证据、Surface Session、命令序列、"
                    "Finalization Epoch 或 Action Execution 已变化"
                )
            return 4

    store = FakeEvidenceArtifactStore()
    repository = StaleSurfaceBindingRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_capture_result(),
        surface_session_id="surface-session-1",
        action_execution_id="action-execution-1",
        session_command_sequence=1,
        finalization_epoch=0,
    )

    assert persisted.page_seq == 4
    assert len(repository.calls) == 2
    assert repository.calls[0]["surface_session_id"] == "surface-session-1"
    assert repository.calls[0]["action_execution_id"] == "action-execution-1"
    assert repository.calls[1]["surface_session_id"] is None
    assert repository.calls[1]["action_execution_id"] is None
    assert repository.calls[1]["session_command_sequence"] is None
    assert repository.calls[1]["finalization_epoch"] is None


async def test_ad_persistence_freezes_requested_and_landing_urls_separately() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_capture_result(),
        parent_page_capture_id="capture-source",
        capture_role="supplemental",
        capture_purpose="web_ad_page",
        direct_page_type="ad_page",
        capture_url="https://ads.example/landing",
        requested_url="https://ads.example/campaign",
        inherit_parent_semantics=False,
    )

    assert repository.calls[0]["url"] == "https://ads.example/landing"
    assert repository.calls[0]["requested_url"] == "https://ads.example/campaign"


async def test_persistence_service_keeps_parent_capture_when_retrying_without_binding() -> None:
    class StaleSurfaceBindingRepository(FakePageCaptureRepository):
        async def save_capture(self, **kwargs: Any) -> int:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError(
                    "Page Capture 的父证据、Surface Session、命令序列、"
                    "Finalization Epoch 或 Action Execution 已变化"
                )
            return 5

    store = FakeEvidenceArtifactStore()
    repository = StaleSurfaceBindingRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=replace(_capture_result(), mode="observe"),
        parent_page_capture_id="parent-capture-1",
        surface_session_id="surface-session-1",
        action_execution_id="action-execution-1",
        session_command_sequence=2,
        finalization_epoch=0,
    )

    assert persisted.page_seq == 5
    assert len(repository.calls) == 2
    assert repository.calls[0]["parent_page_capture_id"] == "parent-capture-1"
    assert repository.calls[1]["parent_page_capture_id"] == "parent-capture-1"
    assert repository.calls[1]["surface_session_id"] is None
    assert repository.calls[1]["action_execution_id"] is None
    assert repository.calls[1]["session_command_sequence"] is None
    assert repository.calls[1]["finalization_epoch"] is None


async def test_persistence_service_commits_pending_manifest_with_capture() -> None:
    """E08/E09：提交路径携带 v1 pending 清单，与附件同一次 save_capture 提交。"""
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_capture_result(),
        surface_session_id="surface-session-1",
        session_command_sequence=1,
        finalization_epoch=0,
    )

    saved = repository.calls[0]
    manifest = saved["manifest"]
    assert manifest is not None
    assert manifest.manifest_version == 1
    assert manifest.quality_status is EvidenceQualityStatus.PENDING
    assert manifest.task_id == "task-1"
    assert manifest.session_id == "surface-session-1"
    assert manifest.capture_id == persisted.page_capture_id
    assert [item.artifact_id for item in manifest.artifacts] == [
        artifact.artifact_id for artifact in persisted.artifacts
    ]
    assert [item.checksum for item in manifest.artifacts] == [
        artifact.checksum for artifact in persisted.artifacts
    ]
    assert persisted.manifest == manifest


async def test_persistence_service_manifest_uses_run_sentinel_without_session() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_capture_result(),
    )

    assert persisted.manifest is not None
    assert persisted.manifest.session_id == "agent-run:run-1"


async def test_persistence_service_rebuilds_manifest_when_binding_is_stripped() -> None:
    class StaleSurfaceBindingRepository(FakePageCaptureRepository):
        async def save_capture(self, **kwargs: Any) -> int:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError(
                    "Page Capture 的父证据、Surface Session、命令序列、"
                    "Finalization Epoch 或 Action Execution 已变化"
                )
            return 4

    store = FakeEvidenceArtifactStore()
    repository = StaleSurfaceBindingRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=_capture_result(),
        surface_session_id="surface-session-1",
        action_execution_id="action-execution-1",
        session_command_sequence=1,
        finalization_epoch=0,
    )

    assert repository.calls[0]["manifest"].session_id == "surface-session-1"
    retried_manifest = repository.calls[1]["manifest"]
    assert retried_manifest.session_id == "agent-run:run-1"
    assert persisted.manifest == retried_manifest


async def test_persistence_service_skips_manifest_when_no_supported_artifacts() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)
    result = replace(
        _capture_result(),
        artifacts=[PageCaptureArtifact("bogus_type", None, "ignored")],
    )

    persisted = await service.persist(task_id="task-1", agent_run_id="run-1", result=result)

    assert persisted.artifacts == []
    assert persisted.manifest is None
    assert repository.calls[0]["manifest"] is None


async def test_persistence_compensation_keeps_reused_artifact() -> None:
    class ReusedArtifactStore(FakeEvidenceArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []

        async def store(self, **kwargs: Any) -> PersistedEvidenceArtifact:
            self.calls.append(kwargs)
            return PersistedEvidenceArtifact(
                artifact_id=kwargs["artifact_id"],
                artifact_type=kwargs["artifact_type"],
                storage_path="reused/object",
                content_type="application/octet-stream",
                checksum="sha256:" + "b" * 64,
                size_bytes=10,
                metadata={},
                created=False,
            )

        async def delete(self, storage_path: str) -> None:
            self.deleted.append(storage_path)

    class FailingRepository(FakePageCaptureRepository):
        async def save_capture(self, **kwargs: Any) -> int:
            self.calls.append(kwargs)
            raise RuntimeError("database unavailable")

    store = ReusedArtifactStore()
    service = PageCapturePersistenceService(
        repository=FailingRepository(),
        artifact_store=store,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.persist(task_id="task-1", agent_run_id="run-1", result=_capture_result())

    assert store.deleted == []


async def test_persistence_rejects_invalid_checksum_before_formal_write() -> None:
    class InvalidIdentityStore(FakeEvidenceArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []

        async def store(self, **kwargs: Any) -> PersistedEvidenceArtifact:
            stored = await super().store(**kwargs)
            return replace(stored, checksum="sha256:" + "A" * 64)

        async def delete(self, storage_path: str) -> None:
            self.deleted.append(storage_path)

    store = InvalidIdentityStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)

    with pytest.raises(ValueError, match="checksum"):
        await service.persist(
            task_id="task-1",
            agent_run_id="run-1",
            result=_capture_result(),
        )

    assert repository.calls == []
    assert len(store.deleted) == 4


async def test_persistence_service_inherits_semantics_for_scroll_supplement() -> None:
    store = FakeEvidenceArtifactStore()
    repository = FakePageCaptureRepository()
    service = PageCapturePersistenceService(repository=repository, artifact_store=store)
    result = replace(
        _capture_result(),
        mode="single_trigger",
        action_trace=[{"action": "scroll"}],
    )

    await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=result,
        parent_page_capture_id="capture-1",
    )

    assert repository.calls[0]["inherit_parent_semantics"] is True


async def test_page_coverage_is_bound_to_capture_and_screenshot_references() -> None:
    store = FakeEvidenceArtifactStore()
    service = PageCapturePersistenceService(
        repository=FakePageCaptureRepository(),
        artifact_store=store,
    )
    result = _capture_result()
    result.artifacts.append(
        PageCaptureArtifact(
            "page_coverage",
            None,
            {"schema_version": "page-coverage.v1", "segments": []},
        )
    )

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=result,
    )

    screenshot_id = next(
        item.artifact_id for item in persisted.artifacts if item.artifact_type == "screenshot"
    )
    coverage_call = next(item for item in store.calls if item["artifact_type"] == "page_coverage")
    assert coverage_call["content"]["associated_page_capture_id"] == (persisted.page_capture_id)
    assert coverage_call["content"]["associated_screenshot_artifact_ids"] == [screenshot_id]


async def test_h5_scroll_coverage_binds_only_its_process_frames() -> None:
    """滚动覆盖索引不能误绑到合并前保留的最后一次观察截图。"""
    store = FakeEvidenceArtifactStore()
    service = PageCapturePersistenceService(
        repository=FakePageCaptureRepository(), artifact_store=store,
    )
    result = _capture_result()
    result.artifacts.extend([
        *[
            PageCaptureArtifact("screenshot", None, {
                "encoding": "base64", "data": "cG5n",
                "capture_role": "h5_vertical_scroll_collection", "frame_index": index,
            })
            for index in range(2)
        ],
        PageCaptureArtifact("page_coverage", None, {
            "schema_version": "page-coverage.v1",
            "capture_role": "h5_vertical_scroll_collection",
            "coverage_complete": False,
        }),
    ])
    await service.persist(task_id="task-1", agent_run_id="run-1", result=result)
    process_ids = [
        item["artifact_id"] for item in store.calls
        if item["artifact_type"] == "screenshot"
        and item["content"].get("capture_role") == "h5_vertical_scroll_collection"
    ]
    coverage = next(item["content"] for item in store.calls
                    if item["artifact_type"] == "page_coverage")
    assert len(process_ids) == 2
    assert coverage["associated_screenshot_artifact_ids"] == process_ids
    assert coverage["coverage_complete"] is False


async def test_scrolled_page_coverage_excludes_visual_navigation_companion() -> None:
    store = FakeEvidenceArtifactStore()
    service = PageCapturePersistenceService(
        repository=FakePageCaptureRepository(),
        artifact_store=store,
    )
    result = replace(
        _capture_result(),
        artifacts=[
            PageCaptureArtifact(
                "screenshot",
                None,
                {
                    "purpose": "web_scrolled_viewport",
                    "encoding": "base64",
                    "data": "cG5n",
                },
            ),
            PageCaptureArtifact(
                "page_coverage",
                None,
                {
                    "protocol_version": "web-scrolled-viewport-manifest.v1",
                    "capture_mode": "scroll_offset_viewport",
                },
            ),
            PageCaptureArtifact(
                "screenshot",
                None,
                {
                    "purpose": "visual_navigation_viewport",
                    "encoding": "base64",
                    "data": "cG5n",
                },
            ),
        ],
    )

    persisted = await service.persist(
        task_id="task-1",
        agent_run_id="run-1",
        result=result,
    )

    screenshot_ids = [
        item.artifact_id for item in persisted.artifacts if item.artifact_type == "screenshot"
    ]
    coverage_call = next(item for item in store.calls if item["artifact_type"] == "page_coverage")
    assert coverage_call["content"]["associated_screenshot_artifact_ids"] == [
        screenshot_ids[0]
    ]


async def test_local_store_decodes_screenshot_and_returns_relative_path(tmp_path: Path) -> None:
    store = LocalEvidenceArtifactStore(root=tmp_path)
    content = {"encoding": "base64", "data": base64.b64encode(b"png-bytes").decode()}

    persisted = await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-1",
        artifact_type="screenshot",
        content=content,
    )

    saved_file = tmp_path / persisted.storage_path
    assert saved_file.read_bytes() == b"png-bytes"
    assert persisted.storage_path == "task-1/capture-1/artifact-1.png"
    assert persisted.content_type == "image/png"
    assert persisted.size_bytes == 9
    assert persisted.checksum.startswith("sha256:")
    assert await store.read(persisted.storage_path) == b"png-bytes"


async def test_local_store_serializes_action_trace_as_json(tmp_path: Path) -> None:
    store = LocalEvidenceArtifactStore(root=tmp_path)

    persisted = await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-trace",
        artifact_type="action_trace",
        content=[{"action": "click", "result": "success"}],
    )

    assert persisted.storage_path == "task-1/capture-1/artifact-trace.json"
    assert persisted.content_type == "application/json"
    assert await store.read(persisted.storage_path) == b'[{"action": "click", "result": "success"}]'


async def test_local_store_never_overwrites_an_existing_artifact_identity(
    tmp_path: Path,
) -> None:
    store = LocalEvidenceArtifactStore(root=tmp_path)
    first = await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-immutable",
        artifact_type="action_trace",
        content={"version": 1},
    )

    retry = await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-immutable",
        artifact_type="action_trace",
        content={"version": 1},
    )

    assert retry.created is False
    assert await store.read(first.storage_path) == b'{"version": 1}'
    with pytest.raises(ValueError, match="不可覆盖"):
        await store.store(
            task_id="task-1",
            page_capture_id="capture-1",
            artifact_id="artifact-immutable",
            artifact_type="action_trace",
            content={"version": 2},
        )


async def test_local_store_concurrent_create_is_single_winner(tmp_path: Path) -> None:
    store = LocalEvidenceArtifactStore(root=tmp_path)

    results = await asyncio.gather(
        store.store(
            task_id="task-1",
            page_capture_id="capture-1",
            artifact_id="artifact-race",
            artifact_type="action_trace",
            content={"version": 1},
        ),
        store.store(
            task_id="task-1",
            page_capture_id="capture-1",
            artifact_id="artifact-race",
            artifact_type="action_trace",
            content={"version": 2},
        ),
        return_exceptions=True,
    )

    assert sum(
        not isinstance(item, BaseException) and item.created for item in results
    ) == 1
    assert sum(isinstance(item, ValueError) for item in results) == 1


async def test_minio_store_reuses_identical_object_and_rejects_drift() -> None:
    client = FakeMinioClient()
    store = MinioEvidenceArtifactStore(
        endpoint="minio:9000",
        access_key="access",
        secret_key="secret",
        bucket="alioth",
        client=client,
    )

    await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-immutable",
        artifact_type="action_trace",
        content={"version": 1},
    )
    retry = await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-immutable",
        artifact_type="action_trace",
        content={"version": 1},
    )

    assert retry.created is False
    assert len(client.put_calls) == 1
    with pytest.raises(ValueError, match="不可覆盖"):
        await store.store(
            task_id="task-1",
            page_capture_id="capture-1",
            artifact_id="artifact-immutable",
            artifact_type="action_trace",
            content={"version": 2},
        )


async def test_minio_store_uses_conditional_create_header() -> None:
    client = FakeMinioClient()
    store = MinioEvidenceArtifactStore(
        endpoint="minio:9000",
        access_key="access",
        secret_key="secret",
        bucket="alioth",
        client=client,
    )

    await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-conditional",
        artifact_type="action_trace",
        content={"version": 1},
    )

    assert client.objects[("alioth", "task-1/capture-1/artifact-conditional.json")]


async def test_minio_store_uploads_and_deletes_screenshot() -> None:
    client = FakeMinioClient()
    store = MinioEvidenceArtifactStore(
        endpoint="minio:9000",
        access_key="access",
        secret_key="secret",
        bucket="alioth",
        client=client,
    )
    content = {"encoding": "base64", "data": base64.b64encode(b"png-bytes").decode()}

    persisted = await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-1",
        artifact_type="screenshot",
        content=content,
    )
    await store.delete(persisted.storage_path)

    call = client.put_calls[0]
    assert call["bucket_name"] == "alioth"
    assert call["object_name"] == "task-1/capture-1/artifact-1.png"
    assert call["content"] == b"png-bytes"
    assert call["content_type"] == "image/png"
    assert persisted.storage_path == call["object_name"]
    assert persisted.metadata["storage_backend"] == "minio"
    assert await store.read(persisted.storage_path) == b"png-bytes"
    assert client.remove_calls == [("alioth", persisted.storage_path)]


async def test_minio_store_retries_transient_read_error() -> None:
    client = FakeMinioClient()
    store = MinioEvidenceArtifactStore(
        endpoint="minio:9000",
        access_key="access",
        secret_key="secret",
        bucket="alioth",
        client=client,
    )
    persisted = await store.store(
        task_id="task-1",
        page_capture_id="capture-1",
        artifact_id="artifact-retry",
        artifact_type="dom",
        content="<html>ok</html>",
    )

    client.get_object_errors.append(ProtocolError("connection reset"))

    assert await store.read(persisted.storage_path) == b"<html>ok</html>"


def test_evidence_store_factory_builds_local_and_minio_backends() -> None:
    local = build_evidence_artifact_store(EvidenceStorageSettings())
    minio = build_evidence_artifact_store(
        EvidenceStorageSettings(
            backend="minio",
            minio_endpoint="minio:9000",
            minio_access_key="access",
            minio_secret_key="secret",
        )
    )

    assert isinstance(local, LocalEvidenceArtifactStore)
    assert isinstance(minio, MinioEvidenceArtifactStore)


def test_evidence_store_factory_rejects_incomplete_minio_settings() -> None:
    settings = EvidenceStorageSettings(backend="minio")

    with pytest.raises(ValueError, match="ALIOTH_MINIO_ENDPOINT"):
        build_evidence_artifact_store(settings)


async def test_postgres_repository_writes_page_capture_and_artifact_rows() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(
        pool=pool, page_graph_repository=None
    )
    artifact = PersistedEvidenceArtifact(
        artifact_id="22222222-2222-2222-2222-222222222222",
        artifact_type="dom",
        storage_path="task/capture/dom.html",
        content_type="text/html; charset=utf-8",
        checksum="sha256:dom",
        size_bytes=13,
        metadata={"encoding": "utf-8"},
    )

    page_seq = await repository.save_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        url="https://example.com",
        mode="goto",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[artifact],
    )

    assert page_seq == 2
    queries = [query for query, _args in pool.connection.execute_calls]
    assert any("pg_advisory_xact_lock" in query for query in queries)
    assert any("INSERT INTO qc_page_capture" in query for query in queries)
    assert any("INSERT INTO qc_evidence_artifact" in query for query in queries)
    capture_call = next(
        call for call in pool.connection.execute_calls if "INSERT INTO qc_page_capture" in call[0]
    )
    assert "agent_run_id" in capture_call[0]
    assert "parent.page_type" in capture_call[0]
    assert "parent.detection_status" in capture_call[0]
    assert "task.finalization_state IN" in capture_call[0]
    lock_call = next(
        call for call in pool.connection.execute_calls if "pg_advisory_xact_lock" in call[0]
    )
    assert lock_call[1] == ("task-finalization:11111111-1111-1111-1111-111111111111",)
    assert capture_call[1][2] == "33333333-3333-3333-3333-333333333333"
    assert "capture_purpose" not in json.loads(capture_call[1][13])


async def test_postgres_repository_persists_explicit_web_capture_purpose_in_metadata() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    await repository.save_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        url="https://example.com",
        mode="observe",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[],
        capture_role="supplemental",
        capture_purpose="web_full_page_supplemental",
    )

    capture_call = next(
        call for call in pool.connection.execute_calls if "INSERT INTO qc_page_capture" in call[0]
    )
    assert json.loads(capture_call[1][13])["capture_purpose"] == (
        "web_full_page_supplemental"
    )


async def test_postgres_repository_persists_ad_requested_url_and_direct_type() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    await repository.save_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        url="https://ads.example/landing",
        mode="single_trigger",
        reachable=True,
        title="广告",
        status_code=200,
        action_log=[],
        artifacts=[],
        parent_page_capture_id="55555555-5555-5555-5555-555555555555",
        capture_role="supplemental",
        capture_purpose="web_ad_page",
        direct_page_type="ad_page",
        requested_url="https://ads.example/campaign",
        inherit_parent_semantics=False,
    )

    capture_call = next(
        call for call in pool.connection.execute_calls if "INSERT INTO qc_page_capture" in call[0]
    )
    metadata = json.loads(capture_call[1][13])
    assert metadata["requested_url"] == "https://ads.example/campaign"
    assert metadata["direct_page_type"] == "ad_page"
    assert capture_call[1][21] == "ad_page"


async def test_postgres_repository_marks_h5_probe_captures_as_evidence_only() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)

    await repository.save_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        url="https://example.com",
        mode="observe",
        reachable=True,
        title="补采结果",
        status_code=200,
        action_log=[],
        artifacts=[],
        capture_role="probe_result_replay",
        surface_session_id="h5:run-1",
        action_execution_id="action-1",
        session_command_sequence=2,
        finalization_epoch=1,
    )

    capture_call = next(
        call for call in pool.connection.execute_calls if "INSERT INTO qc_page_capture" in call[0]
    )
    query = capture_call[0]
    assert "task.target_platform = 'h5'" in query
    assert "'probe_before', 'probe_result_replay'" in query
    assert "THEN 'evidence_only'" in query
    assert "ELSE 'active'" in query


async def test_postgres_repository_uses_image_for_page_screenshot_summary() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)
    image = PersistedEvidenceArtifact(
        artifact_id="22222222-2222-2222-2222-222222222222",
        artifact_type="screenshot",
        storage_path="task/capture/viewport.png",
        content_type="image/png",
        checksum="sha256:image",
        size_bytes=13,
        metadata={},
    )
    sequence = PersistedEvidenceArtifact(
        artifact_id="55555555-5555-5555-5555-555555555555",
        artifact_type="screenshot",
        storage_path="task/capture/sequence.json",
        content_type="application/json",
        checksum="sha256:sequence",
        size_bytes=21,
        metadata={},
    )

    await repository.save_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        url="https://example.com",
        mode="observe",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[image, sequence],
    )

    capture_call = next(
        call for call in pool.connection.execute_calls if "INSERT INTO qc_page_capture" in call[0]
    )
    assert capture_call[1][8] == image.storage_path


async def test_postgres_repository_persists_web_evidence_admission_without_migration() -> None:
    from services.api.app.domain.exploration.web_run_policy_snapshot import (
        WebRunPolicySnapshot,
    )
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    pool = FakePool()
    repository = PostgresPageCaptureRepository(
        pool=pool,
        page_graph_repository=None,
        web_run_policy=WebRunPolicySnapshot.create(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="33333333-3333-3333-3333-333333333333",
            payload=policy_payload(),
        ),
    )

    await repository.save_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        url="https://example.com/",
        mode="observe",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[],
        metrics={
            "web_readiness": {
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
                "settle_complete": True,
                "quorum_met": True,
                "capture_consistent": True,
                "profile_version": "web-structure.v1",
                "network_scope": {
                    "resource_types": ["document", "script"],
                    "url_prefixes": [],
                    "same_origin_only": True,
                },
                "settle_diagnostics": {
                    "settle_complete": True,
                    "quorum_met": True,
                    "stable_sample_count": 3,
                    "sample_window_started_ms": 10000,
                    "sample_window_ended_ms": 10600,
                    "document_time_origin_ms": 1000,
                    "structure_digest": "structure-1",
                },
                "capture_consistency": {
                    "status": "consistent_with_capability_gaps",
                    "changed_signals": [],
                    "unavailable_signals": ["account_identity"],
                    "before": {
                        "document_time_origin_ms": 1000,
                        "page_url_sha256": "url-1",
                        "context_page_urls_sha256": ["url-1"],
                        "active_page_index": 0,
                        "visibility_state": "visible",
                        "top_layer_sha256": None,
                        "document_observation_available": True,
                    },
                    "after": {
                        "document_time_origin_ms": 1000,
                        "page_url_sha256": "url-1",
                        "context_page_urls_sha256": ["url-1"],
                        "active_page_index": 0,
                        "visibility_state": "visible",
                        "top_layer_sha256": None,
                        "document_observation_available": True,
                    },
                },
                "capture_started_ms": 10610,
                "capture_ended_ms": 10900,
                "structure_before": "structure-1",
                "structure_after": "structure-1",
            }
        },
        capture_role="after_action",
    )

    capture_call = next(
        call for call in pool.connection.execute_calls if "INSERT INTO qc_page_capture" in call[0]
    )
    metadata = json.loads(capture_call[1][13])
    assert metadata["web_evidence_admission"] == {
        "kind": "business_page",
        "evidence_role": "classification",
        "readiness_status": "stable",
        "can_project_business_node": True,
        "can_enter_formal_bundle": True,
        "reason_codes": [],
    }


async def test_postgres_repository_writes_manifest_inside_capture_transaction() -> None:
    """E09：pending 清单与采集行、附件行、Outbox 事件在同一事务内提交。"""
    pool = FakePool()
    repository = PostgresPageCaptureRepository(
        pool=pool, page_graph_repository=None
    )
    artifact = PersistedEvidenceArtifact(
        artifact_id="22222222-2222-2222-2222-222222222222",
        artifact_type="dom",
        storage_path="task/capture/dom.html",
        content_type="text/html; charset=utf-8",
        checksum="sha256:dom",
        size_bytes=13,
        metadata={"encoding": "utf-8"},
    )
    manifest = build_pending_page_capture_manifest(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="surface-session-1",
        capture_id="44444444-4444-4444-4444-444444444444",
        capture_role="primary",
        artifacts=[artifact],
    )

    await repository.save_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        url="https://example.com",
        mode="goto",
        reachable=True,
        title="首页",
        status_code=200,
        action_log=[],
        artifacts=[artifact],
        manifest=manifest,
    )

    queries = [query for query, _args in pool.connection.execute_calls]
    assert any("INSERT INTO qc_page_capture" in query for query in queries)
    assert any("INSERT INTO qc_evidence_artifact" in query for query in queries)
    manifest_call = next(
        call
        for call in pool.connection.execute_calls
        if "INSERT INTO qc_evidence_manifest" in call[0]
    )
    # 同事务清单沿用采集许可；review 仍须由前面的 H5 恢复围栏证明。
    assert "task.status = ANY($13::text[])" in manifest_call[0]
    assert manifest_call[1][0] == manifest.manifest_id
    assert manifest_call[1][12] == ["queued", "running", "review"]
    capture_query = next(query for query in queries if "INSERT INTO qc_page_capture" in query)
    assert "task.status = 'review' AND EXISTS" in capture_query
    assert "lower(task.target_platform) = 'h5'" in capture_query
    assert "h5_attempt.strategy = 'new_session_replay'" in capture_query
    assert any("INSERT INTO qc_outbox_event" in query for query in queries)


async def test_postgres_repository_rejects_manifest_with_foreign_identity() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(
        pool=pool, page_graph_repository=None
    )
    artifact = PersistedEvidenceArtifact(
        artifact_id="22222222-2222-2222-2222-222222222222",
        artifact_type="dom",
        storage_path="task/capture/dom.html",
        content_type="text/html; charset=utf-8",
        checksum="sha256:dom",
        size_bytes=13,
        metadata={},
    )
    manifest = build_pending_page_capture_manifest(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="surface-session-1",
        capture_id="55555555-5555-5555-5555-555555555555",
        capture_role="primary",
        artifacts=[artifact],
    )

    with pytest.raises(RuntimeError, match="身份不一致"):
        await repository.save_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="33333333-3333-3333-3333-333333333333",
            page_capture_id="44444444-4444-4444-4444-444444444444",
            url="https://example.com",
            mode="goto",
            reachable=True,
            title="首页",
            status_code=200,
            action_log=[],
            artifacts=[artifact],
            manifest=manifest,
        )
    assert pool.connection.execute_calls == []


async def test_postgres_repository_rejects_capture_when_fact_bindings_do_not_match() -> None:
    pool = FakePool()
    pool.connection.reject_capture_insert = True
    repository = PostgresPageCaptureRepository(
        pool=pool, page_graph_repository=None
    )

    with pytest.raises(RuntimeError, match="Finalization Epoch"):
        await repository.save_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="33333333-3333-3333-3333-333333333333",
            page_capture_id="44444444-4444-4444-4444-444444444444",
            url="https://example.com",
            mode="observe",
            reachable=True,
            title="首页",
            status_code=200,
            action_log=[],
            artifacts=[],
            surface_session_id="surface-session-1",
            session_command_sequence=1,
            finalization_epoch=0,
        )


async def test_postgres_repository_persists_page_detection_with_run_ownership() -> None:
    pool = FakePool()
    repository = PostgresPageCaptureRepository(
        pool=pool, page_graph_repository=None
    )
    navigation_knowledge = {
        "visible_label_count": 2,
        "match_count": 1,
        "match_type_counts": {"static": 0, "exact": 0, "vector": 1},
        "vector_lookup_attempt_count": 2,
        "repository_degraded": False,
        "degraded_events": [],
    }


    updated = await repository.update_detection_result(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="33333333-3333-3333-3333-333333333333",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        page_type="home",
        page_state="stable",
        detection_status="recognized",
        confidence=0.94,
        catalog_version="v1",
        catalog_sha256="sha256:test-catalog",
        navigation_knowledge=navigation_knowledge,
    )

    assert updated is True
    query, args = next(
        call for call in pool.connection.execute_calls if "UPDATE qc_page_capture" in call[0]
    )
    assert "UPDATE qc_page_capture" in query
    assert "page_type = $4" in query
    assert "detection_status = $6::text" in query
    assert "detection_confidence = $7::double precision" in query
    assert "metadata" in query
    assert "'detection_status', $6::text" in query
    assert "'confidence', $7::double precision" in query
    assert "'catalog_version', $8::text" in query
    assert "'catalog_sha256', $9::text" in query
    assert "'navigation_knowledge', $13::jsonb" in query
    assert json.loads(args[12]) == navigation_knowledge
    assert "task_id = $2::uuid" in query
    assert "agent_run_id = $3::uuid" in query
    assert "task.finalization_state IN" in query
    assert args[:4] == (
        "44444444-4444-4444-4444-444444444444",
        "11111111-1111-1111-1111-111111111111",
        "33333333-3333-3333-3333-333333333333",
        "home",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchObject"])
async def test_minio_public_read_normalizes_missing_s3_object(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """对象不存在必须归一为 FileNotFoundError，供库存判定为业务不足。"""
    from minio.error import S3Error

    def missing(*args, **kwargs):
        del args, kwargs
        raise S3Error(None, code, "missing", "object", "request", "host")

    monkeypatch.setattr(minio_evidence_store, "read_minio_object_with_retry", missing)
    store = MinioEvidenceArtifactStore(
        endpoint="minio.test", access_key="key", secret_key="secret", bucket="evidence",
        client=FakeMinioClient(),
    )
    with pytest.raises(FileNotFoundError):
        await store.read("task/capture/artifact.png")


@pytest.mark.asyncio
async def test_minio_public_read_preserves_access_denied_s3_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权限、超时等基础设施问题不能被误分为对象缺失。"""
    from minio.error import S3Error

    def denied(*args, **kwargs):
        del args, kwargs
        raise S3Error(None, "AccessDenied", "denied", "object", "request", "host")

    monkeypatch.setattr(minio_evidence_store, "read_minio_object_with_retry", denied)
    store = MinioEvidenceArtifactStore(
        endpoint="minio.test",
        access_key="key",
        secret_key="secret",
        bucket="evidence",
        client=FakeMinioClient(),
    )
    with pytest.raises(S3Error) as raised:
        await store.read("task/capture/artifact.png")
    assert raised.value.code == "AccessDenied"
