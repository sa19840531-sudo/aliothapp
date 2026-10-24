"""截图专用 App Capture 校验、幂等读取与对象补偿测试。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import pytest

from services.api.app.application.app_capture_persistence import (
    AppCapturePersistenceService,
    AppCaptureRecoveryError,
    PersistedAppCapture,
)
from services.api.app.application.app_metrics import (
    APP_CAPTURE_ORPHAN_TOTAL,
    app_metric_value,
)
from services.api.app.application.capture_profiles import (
    APP_CAPTURE_ARTIFACT_CONTRACT,
    APP_MAIN_CAPTURE_ARTIFACT_TYPES,
)
from services.api.app.application.evidence_manifests import PersistedEvidenceManifest
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    RawArtifact,
    RawArtifactSet,
    SurfaceKind,
)
from services.api.app.infrastructure.storage.local_evidence_store import serialize_artifact

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


class FakeArtifactStore:
    def __init__(self) -> None:
        self.store_calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.objects: dict[str, bytes] = {}

    async def store(self, **kwargs: Any) -> PersistedEvidenceArtifact:
        self.store_calls.append(kwargs)
        data, extension, content_type, metadata = serialize_artifact(
            kwargs["artifact_type"],
            kwargs["content"],
        )
        storage_path = (
            f"{kwargs['task_id']}/{kwargs['page_capture_id']}/{kwargs['artifact_id']}.{extension}"
        )
        self.objects[storage_path] = data
        checksum = hashlib.sha256(data).hexdigest()
        return PersistedEvidenceArtifact(
            artifact_id=kwargs["artifact_id"],
            artifact_type=kwargs["artifact_type"],
            storage_path=storage_path,
            content_type=content_type,
            checksum=f"sha256:{checksum}",
            size_bytes=len(data),
            metadata=metadata,
        )

    async def delete(self, storage_path: str) -> None:
        self.deleted.append(storage_path)

    async def read(self, storage_path: str) -> bytes:
        return self.objects[storage_path]


class InvalidActionArtifactStore(FakeArtifactStore):
    """模拟对象已写入但存储端返回的 Artifact 元数据不符合契约。"""

    async def store(self, **kwargs: Any) -> PersistedEvidenceArtifact:
        stored = await super().store(**kwargs)
        return replace(stored, artifact_id="unexpected-artifact-id")


class InvalidIdentityArtifactStore(FakeArtifactStore):
    """模拟非规范大写 SHA-256 身份越过存储端。"""

    async def store(self, **kwargs: Any) -> PersistedEvidenceArtifact:
        stored = await super().store(**kwargs)
        if kwargs["artifact_type"] == "action_trace":
            return replace(stored, checksum="sha256:" + "A" * 64)
        return stored


class BlankPathArtifactStore(FakeArtifactStore):
    """模拟存储端返回空白对象路径。"""

    async def store(self, **kwargs: Any) -> PersistedEvidenceArtifact:
        return replace(await super().store(**kwargs), storage_path=" ")


class DeleteFailingArtifactStore(FakeArtifactStore):
    async def delete(self, storage_path: str) -> None:
        await super().delete(storage_path)
        raise RuntimeError("object store delete failed")


class ExistingArtifactStore(FakeArtifactStore):
    async def store(self, **kwargs: Any) -> PersistedEvidenceArtifact:
        return replace(await super().store(**kwargs), created=False)


class UnreadableArtifactStore(FakeArtifactStore):
    async def read(self, storage_path: str) -> bytes:
        del storage_path
        raise OSError("object temporarily unavailable")


class MemoryManifestRepository:
    def __init__(self) -> None:
        self.manifests = []

    async def add(self, *, manifest):
        self.manifests.append(manifest)
        return PersistedEvidenceManifest(manifest=manifest, created=True)

    async def get_latest_for_capture(self, *, task_id: str, capture_id: str, **kwargs):
        del kwargs
        matches = [
            item
            for item in self.manifests
            if item.task_id == task_id and item.capture_id == capture_id
        ]
        return matches[-1] if matches else None


class FakeAppCaptureRepository:
    def __init__(self) -> None:
        self.existing: PersistedAppCapture | None = None
        self.action_captures: dict[tuple[str, str], PersistedAppCapture] = {}
        self.artifacts: dict[tuple[str, str], PersistedEvidenceArtifact] = {}
        self.save_calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def get_bootstrap_capture(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> PersistedAppCapture | None:
        del task_id, session_id
        return self.existing

    async def save_bootstrap_capture(self, **kwargs: Any) -> PersistedAppCapture:
        self.save_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        capture = CaptureRef(
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            capture_id=kwargs["capture_id"],
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="bootstrap",
            artifact_ids=(kwargs["screenshot"].artifact_id,),
        )
        return PersistedAppCapture(
            capture=capture,
            agent_run_id=kwargs["agent_run_id"],
            screenshot=kwargs["screenshot"],
            width=kwargs["width"],
            height=kwargs["height"],
            orientation=kwargs["orientation"],
            image_signature=kwargs["image_signature"],
            package_name=kwargs["package_name"],
            activity=kwargs["activity"],
            bootstrap_row_version=kwargs["expected_bootstrap_row_version"] + 1,
        )

    async def get_capture(self, *, capture_id: str) -> PersistedAppCapture | None:
        if self.existing is None or self.existing.capture.capture_id != capture_id:
            return None
        return self.existing

    async def get_capture_artifact(
        self,
        *,
        capture_id: str,
        artifact_type: str,
    ) -> PersistedEvidenceArtifact | None:
        return self.artifacts.get((capture_id, artifact_type))

    async def get_action_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> PersistedAppCapture | None:
        return self.action_captures.get((action_execution_id, role))

    async def save_action_capture(self, **kwargs: Any) -> PersistedAppCapture:
        self.save_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        capture = CaptureRef(
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            capture_id=kwargs["capture_id"],
            surface_kind=kwargs["surface_kind"],
            capture_kind=CaptureKind.APP,
            role=kwargs["role"],
            artifact_ids=tuple(item.artifact_id for item in kwargs["artifacts"]),
            parent_capture_id=kwargs["parent_capture_id"],
            action_execution_id=kwargs["action_execution_id"],
        )
        return PersistedAppCapture(
            capture=capture,
            agent_run_id=kwargs["agent_run_id"],
            screenshot=kwargs["screenshot"],
            width=kwargs["width"],
            height=kwargs["height"],
            orientation=kwargs["orientation"],
            image_signature=kwargs["image_signature"],
            package_name=kwargs["package_name"],
            activity=kwargs["activity"],
        )

    async def save_observation_capture(self, **kwargs: Any) -> PersistedAppCapture:
        self.save_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        capture = CaptureRef(
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            capture_id=kwargs["capture_id"],
            surface_kind=kwargs["surface_kind"],
            capture_kind=CaptureKind.APP,
            role="observation",
            artifact_ids=tuple(item.artifact_id for item in kwargs["artifacts"]),
            parent_capture_id=kwargs["parent_capture_id"],
            action_execution_id=None,
        )
        return PersistedAppCapture(
            capture=capture,
            agent_run_id=kwargs["agent_run_id"],
            screenshot=kwargs["screenshot"],
            width=kwargs["width"],
            height=kwargs["height"],
            orientation=kwargs["orientation"],
            image_signature=kwargs["image_signature"],
            package_name=kwargs["package_name"],
            activity=kwargs["activity"],
        )


class AtomicFakeAppCaptureRepository(FakeAppCaptureRepository):
    """模拟 Capture 与首版 Manifest 已在同一数据库事务提交。"""

    persists_manifest_atomically = True

    def __init__(self, manifests: MemoryManifestRepository) -> None:
        super().__init__()
        self._manifests = manifests

    async def save_bootstrap_capture(self, **kwargs: Any) -> PersistedAppCapture:
        self._manifests.manifests.append(kwargs["manifest"])
        persisted = await super().save_bootstrap_capture(**kwargs)
        self.existing = persisted
        return persisted


class HistoricalActionCaptureRepository(FakeAppCaptureRepository):
    async def save_action_capture(self, **kwargs: Any) -> PersistedAppCapture:
        persisted = await super().save_action_capture(**kwargs)
        historical_screenshot = replace(
            persisted.screenshot,
            artifact_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            storage_path="historical/screenshot.png",
        )
        historical_artifact_ids = tuple(
            (
                historical_screenshot.artifact_id
                if item.artifact_type == "screenshot"
                else "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            )
            for item in kwargs["artifacts"]
        )
        return replace(
            persisted,
            capture=replace(
                persisted.capture,
                capture_id="99999999-9999-9999-9999-999999999999",
                artifact_ids=historical_artifact_ids,
            ),
            screenshot=historical_screenshot,
        )


def _artifact_set(*, content: bytes = PNG_1X1) -> RawArtifactSet:
    signature = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return RawArtifactSet(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
        surface_ref="app-surface-1",
        artifacts=(RawArtifact(artifact_type="screenshot", content=content),),
        metadata={
            "package_name": "com.example.target",
            "activity": ".MainActivity",
            "width": 1,
            "height": 1,
            "orientation": "portrait",
            "image_signature": signature,
        },
    )


def _action_artifact_set() -> RawArtifactSet:
    source = _artifact_set()
    return RawArtifactSet(
        task_id=source.task_id,
        session_id=source.session_id,
        surface_ref=source.surface_ref,
        artifacts=(
            *source.artifacts,
            RawArtifact(
                artifact_type="action_trace",
                content={"entries": [{"action": "click"}]},
            ),
        ),
        metadata=source.metadata,
    )


def _service(
    *,
    repository: FakeAppCaptureRepository | None = None,
    store: FakeArtifactStore | None = None,
    manifest_repository: MemoryManifestRepository | None = None,
    max_screenshot_bytes: int = 1024,
) -> tuple[AppCapturePersistenceService, FakeAppCaptureRepository, FakeArtifactStore]:
    repository = repository or FakeAppCaptureRepository()
    store = store or FakeArtifactStore()
    return (
        AppCapturePersistenceService(
            repository=repository,
            artifact_store=store,
            manifest_repository=manifest_repository,
            max_screenshot_bytes=max_screenshot_bytes,
        ),
        repository,
        store,
    )


def _historical_action_capture(
    *,
    action_execution_id: str = "action-1",
    role: str = "before_action",
    capture_id: str = "99999999-9999-9999-9999-999999999999",
    storage_path: str = "historical/screenshot.png",
) -> PersistedAppCapture:
    checksum = f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}"
    screenshot = PersistedEvidenceArtifact(
        artifact_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        artifact_type="screenshot",
        storage_path=storage_path,
        content_type="image/png",
        checksum=checksum,
        size_bytes=len(PNG_1X1),
        metadata={"encoding": "binary"},
    )
    return PersistedAppCapture(
        capture=CaptureRef(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
            capture_id=capture_id,
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role=role,
            artifact_ids=(screenshot.artifact_id,),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            action_execution_id=action_execution_id,
        ),
        agent_run_id="22222222-2222-2222-2222-222222222222",
        screenshot=screenshot,
        width=1,
        height=1,
        orientation="portrait",
        image_signature=checksum,
        package_name="com.example.target",
        activity=".MainActivity",
    )


async def test_verified_action_capture_reads_by_business_identity_and_validates_object() -> None:
    repository = FakeAppCaptureRepository()
    store = FakeArtifactStore()
    persisted = _historical_action_capture()
    repository.action_captures[("action-1", "before_action")] = persisted
    store.objects[persisted.screenshot.storage_path] = PNG_1X1
    service, _, _ = _service(repository=repository, store=store)

    loaded = await service.get_verified_action_capture(
        action_execution_id="action-1",
        role="before_action",
    )

    assert loaded is persisted
    assert loaded.capture.capture_id == "99999999-9999-9999-9999-999999999999"


async def test_verified_ui_tree_checks_capture_membership_and_object_identity() -> None:
    repository = FakeAppCaptureRepository()
    store = FakeArtifactStore()
    persisted = _historical_action_capture()
    xml = (
        '<hierarchy><node package="com.example.target" content-desc="跳过" '
        'clickable="true" enabled="true" bounds="[860,101][1037,202]"/></hierarchy>'
    )
    raw = xml.encode("utf-8")
    artifact = PersistedEvidenceArtifact(
        artifact_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        artifact_type="ui_tree",
        storage_path="historical/ui_tree.xml",
        content_type="application/xml; charset=utf-8",
        checksum=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        size_bytes=len(raw),
        metadata={"encoding": "utf-8"},
    )
    persisted = replace(
        persisted,
        capture=replace(
            persisted.capture,
            artifact_ids=(*persisted.capture.artifact_ids, artifact.artifact_id),
        ),
    )
    repository.existing = persisted
    repository.artifacts[(persisted.capture_id, "ui_tree")] = artifact
    store.objects[artifact.storage_path] = raw
    service, _, _ = _service(repository=repository, store=store)

    verified = await service.get_verified_ui_tree(capture=persisted.capture)

    assert verified is not None
    assert verified.artifact == artifact
    assert verified.xml_text == xml


async def test_verified_ui_tree_rejects_tampered_object() -> None:
    repository = FakeAppCaptureRepository()
    store = FakeArtifactStore()
    persisted = _historical_action_capture()
    artifact = PersistedEvidenceArtifact(
        artifact_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        artifact_type="ui_tree",
        storage_path="historical/ui_tree.xml",
        content_type="application/xml; charset=utf-8",
        checksum="sha256:" + "0" * 64,
        size_bytes=21,
        metadata={"encoding": "utf-8"},
    )
    persisted = replace(
        persisted,
        capture=replace(
            persisted.capture,
            artifact_ids=(*persisted.capture.artifact_ids, artifact.artifact_id),
        ),
    )
    repository.existing = persisted
    repository.artifacts[(persisted.capture_id, "ui_tree")] = artifact
    store.objects[artifact.storage_path] = b"<hierarchy></hierarchy>"
    service, _, _ = _service(repository=repository, store=store)

    with pytest.raises(AppCaptureRecoveryError) as caught:
        await service.get_verified_ui_tree(capture=persisted.capture)

    assert caught.value.error_code == "APP_UI_TREE_CHECKSUM_MISMATCH"


@pytest.mark.parametrize(
    ("objects", "error_code"),
    [
        ({}, "APP_ACTION_RECOVERY_CAPTURE_UNREADABLE"),
        (
            {"historical/screenshot.png": b"x" * len(PNG_1X1)},
            "APP_ACTION_RECOVERY_CAPTURE_CHECKSUM_MISMATCH",
        ),
    ],
)
async def test_verified_action_capture_fails_closed_for_unreadable_or_tampered_object(
    objects: dict[str, bytes],
    error_code: str,
) -> None:
    repository = FakeAppCaptureRepository()
    store = FakeArtifactStore()
    persisted = _historical_action_capture()
    repository.action_captures[("action-1", "before_action")] = persisted
    store.objects.update(objects)
    service, _, _ = _service(repository=repository, store=store)

    with pytest.raises(AppCaptureRecoveryError) as caught:
        await service.get_verified_action_capture(
            action_execution_id="action-1",
            role="before_action",
        )

    assert caught.value.error_code == error_code


async def test_verified_action_capture_fails_closed_for_non_png_object() -> None:
    bad_content = b"not-a-png"
    bad_checksum = f"sha256:{hashlib.sha256(bad_content).hexdigest()}"
    persisted = _historical_action_capture()
    persisted = replace(
        persisted,
        screenshot=replace(
            persisted.screenshot,
            checksum=bad_checksum,
            size_bytes=len(bad_content),
        ),
        image_signature=bad_checksum,
    )
    repository = FakeAppCaptureRepository()
    repository.action_captures[("action-1", "before_action")] = persisted
    store = FakeArtifactStore()
    store.objects[persisted.screenshot.storage_path] = bad_content
    service, _, _ = _service(repository=repository, store=store)

    with pytest.raises(AppCaptureRecoveryError) as caught:
        await service.get_verified_action_capture(
            action_execution_id="action-1",
            role="before_action",
        )

    assert caught.value.error_code == "APP_ACTION_RECOVERY_CAPTURE_INVALID"


async def test_verified_action_capture_rejects_object_above_read_limit() -> None:
    persisted = _historical_action_capture()
    repository = FakeAppCaptureRepository()
    repository.action_captures[("action-1", "before_action")] = persisted
    store = FakeArtifactStore()
    store.objects[persisted.screenshot.storage_path] = PNG_1X1
    service, _, _ = _service(
        repository=repository,
        store=store,
        max_screenshot_bytes=len(PNG_1X1) - 1,
    )

    with pytest.raises(AppCaptureRecoveryError) as caught:
        await service.get_verified_action_capture(
            action_execution_id="action-1",
            role="before_action",
        )

    assert caught.value.error_code == "APP_ACTION_RECOVERY_CAPTURE_INVALID"


async def test_persist_bootstrap_stores_only_png_and_derives_trusted_metadata() -> None:
    service, repository, store = _service()

    persisted = await service.persist_bootstrap(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        artifacts=_artifact_set(),
        expected_bootstrap_row_version=7,
        workflow_lease_owner="app-worker-1",
        workflow_lease_token="33333333-3333-3333-3333-333333333333",
    )

    assert persisted.capture.capture_kind is CaptureKind.APP
    assert persisted.capture.surface_kind is SurfaceKind.ANDROID
    assert persisted.capture.role == "bootstrap"
    assert persisted.width == 1
    assert persisted.height == 1
    assert persisted.bootstrap_row_version == 8
    assert len(store.store_calls) == 1
    assert store.store_calls[0]["artifact_type"] == "screenshot"
    assert store.store_calls[0]["content"] == PNG_1X1
    saved = repository.save_calls[0]
    assert saved["image_signature"] == f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}"
    assert saved["device_snapshot"] == {}
    assert saved["screenshot"].metadata == {
        "encoding": "binary",
        "width": 1,
        "height": 1,
        "orientation": "portrait",
        "image_signature": f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}",
        "session_auth": "unknown",
    }
    assert "url" not in saved
    assert "dom" not in saved
    assert "visible_text" not in saved


@pytest.mark.parametrize(
    ("artifacts", "message"),
    [
        (
            RawArtifactSet(
                task_id="11111111-1111-1111-1111-111111111111",
                session_id="app-session-1",
                surface_ref="app-surface-1",
                artifacts=(
                    RawArtifact(artifact_type="screenshot", content=PNG_1X1),
                    RawArtifact(artifact_type="dom", content="<html />"),
                ),
            ),
            "且仅允许一个 screenshot",
        ),
        (_artifact_set(content=b"not-png"), "PNG signature"),
    ],
)
async def test_persist_bootstrap_rejects_non_screenshot_or_invalid_png(
    artifacts: RawArtifactSet,
    message: str,
) -> None:
    service, _, store = _service()

    with pytest.raises(ValueError, match=message):
        await service.persist_bootstrap(
            task_id=artifacts.task_id,
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id=artifacts.session_id,
            artifacts=artifacts,
            expected_bootstrap_row_version=0,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )
    assert store.store_calls == []


async def test_persist_bootstrap_rejects_oversized_or_mismatched_metadata() -> None:
    service, _, _ = _service(max_screenshot_bytes=len(PNG_1X1) - 1)
    with pytest.raises(ValueError, match="超过上限"):
        await service.persist_bootstrap(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            artifacts=_artifact_set(),
            expected_bootstrap_row_version=0,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )

    mismatched = _artifact_set()
    mismatched = RawArtifactSet(
        task_id=mismatched.task_id,
        session_id=mismatched.session_id,
        surface_ref=mismatched.surface_ref,
        artifacts=mismatched.artifacts,
        metadata={**dict(mismatched.metadata), "width": 99},
    )
    service, _, _ = _service()
    with pytest.raises(ValueError, match="PNG 实际尺寸"):
        await service.persist_bootstrap(
            task_id=mismatched.task_id,
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id=mismatched.session_id,
            artifacts=mismatched,
            expected_bootstrap_row_version=0,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )


async def test_database_failure_deletes_uploaded_screenshot() -> None:
    repository = FakeAppCaptureRepository()
    repository.error = RuntimeError("database failed")
    service, _, store = _service(repository=repository)

    with pytest.raises(RuntimeError, match="database failed"):
        await service.persist_bootstrap(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            artifacts=_artifact_set(),
            expected_bootstrap_row_version=7,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )

    stored = store.store_calls[0]
    expected_path = f"{stored['task_id']}/{stored['page_capture_id']}/{stored['artifact_id']}.png"
    assert store.deleted == [expected_path]


async def test_compensation_delete_failure_increments_orphan_metric() -> None:
    repository = FakeAppCaptureRepository()
    repository.error = RuntimeError("database failed")
    store = DeleteFailingArtifactStore()
    service, _, _ = _service(repository=repository, store=store)
    metric_before = app_metric_value(APP_CAPTURE_ORPHAN_TOTAL)

    with pytest.raises(RuntimeError, match="database failed"):
        await service.persist_bootstrap(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            artifacts=_artifact_set(),
            expected_bootstrap_row_version=7,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )

    assert app_metric_value(APP_CAPTURE_ORPHAN_TOTAL) == metric_before + 1


async def test_database_failure_never_deletes_a_preexisting_object() -> None:
    repository = FakeAppCaptureRepository()
    repository.error = RuntimeError("database failed")
    store = ExistingArtifactStore()
    service, _, _ = _service(repository=repository, store=store)

    with pytest.raises(RuntimeError, match="database failed"):
        await service.persist_bootstrap(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            artifacts=_artifact_set(),
            expected_bootstrap_row_version=7,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )

    assert store.deleted == []


async def test_persist_action_capture_binds_parent_action_and_command_epoch() -> None:
    service, repository, store = _service()

    persisted = await service.persist_action_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        artifacts=replace(_action_artifact_set(), metadata={"session_auth": "authenticated"}),
        role="before_action",
        required_artifact_types=("screenshot", "action_trace"),
        parent_capture_id="88888888-8888-8888-8888-888888888888",
        action_execution_id="action-1",
        session_command_sequence=3,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert persisted.capture.role == "before_action"
    assert persisted.capture.parent_capture_id == "88888888-8888-8888-8888-888888888888"
    assert persisted.capture.action_execution_id == "action-1"
    assert {item["artifact_type"] for item in store.store_calls} == {
        "screenshot",
        "action_trace",
    }
    saved = repository.save_calls[0]
    assert saved["session_command_sequence"] == 3
    assert saved["finalization_epoch"] == 2
    assert saved["lease_owner"] == "worker-1:app-command"
    assert saved["lease_token"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert len(saved["artifacts"]) == 2
    assert saved["screenshot"].metadata["session_auth"] == "authenticated"


async def test_default_main_profile_artifacts_pass_writer_contract_to_persistence() -> None:
    """默认 Profile 的全部正式制品必须进入同一 Capture/Manifest 持久化批次。"""

    source = _action_artifact_set()
    contract = APP_CAPTURE_ARTIFACT_CONTRACT.select(
        role="after_action",
        requested=APP_MAIN_CAPTURE_ARTIFACT_TYPES,
        produced=APP_MAIN_CAPTURE_ARTIFACT_TYPES,
    )
    artifacts = RawArtifactSet(
        task_id=source.task_id,
        session_id=source.session_id,
        surface_ref=source.surface_ref,
        artifacts=(
            *source.artifacts,
            RawArtifact(
                artifact_type="ui_tree",
                content=b'<hierarchy><node text="home" bounds="[0,0][1,1]"/></hierarchy>',
            ),
            RawArtifact(
                artifact_type="view_hierarchy",
                content={"status": "captured", "nodes": []},
            ),
            RawArtifact(artifact_type="ui_snapshot", content={"screen_id": "screen:1"}),
            RawArtifact(artifact_type="visible_text", content=b"home"),
            RawArtifact(
                artifact_type="media_state",
                content={"schema_version": "app-media-state.v1", "items": []},
            ),
            RawArtifact(artifact_type="device_log", content={"status": "captured"}),
            RawArtifact(artifact_type="network_trace", content={"status": "captured"}),
            RawArtifact(
                artifact_type="har",
                content={"format": "network_events", "entries": []},
            ),
            RawArtifact(
                artifact_type="temporal_trace",
                content={
                    "schema_version": "1.0",
                    "clock": "monotonic",
                    "requested_interval_ms": 1000,
                    "samples": [
                        {"index": 0, "offset_ms": 0.0, "visible_text": "a"},
                        {"index": 1, "offset_ms": 1000.0, "visible_text": "b"},
                        {"index": 2, "offset_ms": 2000.0, "visible_text": "c"},
                    ],
                    "countdown_events": [],
                },
            ),
            RawArtifact(artifact_type="fault_artifact", content={"fault_kind": "none"}),
        ),
        metadata={
            **dict(source.metadata),
            "capture_artifact_contract": contract.to_metadata(),
        },
    )
    manifests = MemoryManifestRepository()
    service, repository, store = _service(manifest_repository=manifests)

    persisted = await service.persist_action_capture(
        task_id=source.task_id,
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id=source.session_id,
        surface_kind=SurfaceKind.ANDROID,
        artifacts=artifacts,
        role="after_action",
        required_artifact_types=APP_MAIN_CAPTURE_ARTIFACT_TYPES,
        parent_capture_id="88888888-8888-8888-8888-888888888888",
        action_execution_id="action-main-profile-1",
        session_command_sequence=3,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert persisted.capture.artifact_ids
    assert {item["artifact_type"] for item in store.store_calls} == set(
        APP_MAIN_CAPTURE_ARTIFACT_TYPES
    )
    assert {item.artifact_type for item in repository.save_calls[0]["artifacts"]} == set(
        APP_MAIN_CAPTURE_ARTIFACT_TYPES
    )
    assert (
        persisted.screenshot.metadata["capture_artifact_contract"][
            "missing_soft_optional_artifact_types"
        ]
        == ()
    )
    screenshot_ref = next(
        item for item in manifests.manifests[0].artifacts if item.artifact_type == "screenshot"
    )
    assert (
        screenshot_ref.metadata["capture_artifact_contract"]["persisted_artifact_types"]
        == APP_MAIN_CAPTURE_ARTIFACT_TYPES
    )
    assert (
        manifests.manifests[0].from_dict(manifests.manifests[0].to_dict()) == manifests.manifests[0]
    )


async def test_capture_artifact_contract_metadata_cannot_hide_missing_optional_state() -> None:
    """Writer 的状态摘要必须能由 requested/produced 重新计算，禁止伪造完整。"""

    source = _action_artifact_set()
    contract = APP_CAPTURE_ARTIFACT_CONTRACT.select(
        role="after_action",
        requested=APP_MAIN_CAPTURE_ARTIFACT_TYPES,
        produced=("screenshot", "action_trace"),
    ).to_metadata()
    contract["missing_soft_optional_artifact_types"] = []
    artifacts = replace(
        source,
        metadata={
            **dict(source.metadata),
            "capture_artifact_contract": contract,
        },
    )
    service, _repository, store = _service()

    with pytest.raises(ValueError, match="missing_soft_optional_artifact_types"):
        await service.persist_action_capture(
            task_id=source.task_id,
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id=source.session_id,
            surface_kind=SurfaceKind.ANDROID,
            artifacts=artifacts,
            role="after_action",
            required_artifact_types=("screenshot", "action_trace"),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            action_execution_id="action-contract-drift-1",
            session_command_sequence=3,
            finalization_epoch=2,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert store.store_calls == []


async def test_persist_observation_capture_has_stable_identity_without_action() -> None:
    service, repository, _store = _service()

    first = await service.persist_observation_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        artifacts=replace(_artifact_set(), metadata={"session_auth": "logged_out"}),
        required_artifact_types=("screenshot",),
        parent_capture_id="88888888-8888-8888-8888-888888888888",
        observation_id="observation-1",
        surface_phase_run_id="phase-1",
        surface_phase_decision_id="decision-1",
        session_command_sequence=4,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    replay = await service.persist_observation_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        artifacts=_artifact_set(),
        required_artifact_types=("screenshot",),
        parent_capture_id="88888888-8888-8888-8888-888888888888",
        observation_id="observation-1",
        surface_phase_run_id="phase-1",
        surface_phase_decision_id="decision-1",
        session_command_sequence=4,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert first.capture.capture_id == replay.capture.capture_id
    assert first.capture.role == "observation"
    assert first.capture.parent_capture_id == "88888888-8888-8888-8888-888888888888"
    assert first.capture.action_execution_id is None
    saved = repository.save_calls[0]
    assert saved["screenshot"].metadata["session_auth"] == "guest"
    assert saved["observation_id"] == "observation-1"
    assert saved["surface_phase_run_id"] == "phase-1"
    assert saved["surface_phase_decision_id"] == "decision-1"


async def test_bootstrap_action_and_observation_write_pending_manifests() -> None:
    manifests = MemoryManifestRepository()
    service, _, _ = _service(manifest_repository=manifests)

    bootstrap = await service.persist_bootstrap(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        artifacts=_artifact_set(),
        expected_bootstrap_row_version=0,
        workflow_lease_owner="app-worker-1",
        workflow_lease_token="33333333-3333-3333-3333-333333333333",
    )
    action = await service.persist_action_capture(
        task_id=bootstrap.capture.task_id,
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id=bootstrap.capture.session_id,
        surface_kind=SurfaceKind.ANDROID,
        artifacts=_action_artifact_set(),
        role="before_action",
        required_artifact_types=("screenshot", "action_trace"),
        parent_capture_id=bootstrap.capture.capture_id,
        action_execution_id="action-1",
        session_command_sequence=1,
        finalization_epoch=0,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    observation = await service.persist_observation_capture(
        task_id=bootstrap.capture.task_id,
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id=bootstrap.capture.session_id,
        surface_kind=SurfaceKind.ANDROID,
        artifacts=_artifact_set(),
        required_artifact_types=("screenshot",),
        parent_capture_id=action.capture.capture_id,
        observation_id="observation-manifest-1",
        surface_phase_run_id="phase-1",
        surface_phase_decision_id="decision-1",
        session_command_sequence=2,
        finalization_epoch=0,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert [item.capture_id for item in manifests.manifests] == [
        bootstrap.capture.capture_id,
        action.capture.capture_id,
        observation.capture.capture_id,
    ]
    assert all(item.quality_status.value == "pending" for item in manifests.manifests)


async def test_atomic_capture_manifest_is_verified_after_transaction_commit() -> None:
    manifests = MemoryManifestRepository()
    repository = AtomicFakeAppCaptureRepository(manifests)
    service, _, _ = _service(
        repository=repository,
        manifest_repository=manifests,
    )

    persisted = await service.persist_bootstrap(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        artifacts=_artifact_set(),
        expected_bootstrap_row_version=0,
        workflow_lease_owner="app-worker-1",
        workflow_lease_token="33333333-3333-3333-3333-333333333333",
    )

    assert [item.quality_status.value for item in manifests.manifests] == [
        "pending",
        "pass",
    ]
    assert all(item.capture_id == persisted.capture.capture_id for item in manifests.manifests)


async def test_atomic_bootstrap_replay_reuses_terminal_manifest() -> None:
    manifests = MemoryManifestRepository()
    repository = AtomicFakeAppCaptureRepository(manifests)
    service, _, _ = _service(
        repository=repository,
        manifest_repository=manifests,
    )
    kwargs = {
        "task_id": "11111111-1111-1111-1111-111111111111",
        "agent_run_id": "22222222-2222-2222-2222-222222222222",
        "session_id": "app-session-1",
        "artifacts": _artifact_set(),
        "expected_bootstrap_row_version": 0,
        "workflow_lease_owner": "app-worker-1",
        "workflow_lease_token": "33333333-3333-3333-3333-333333333333",
    }

    first = await service.persist_bootstrap(**kwargs)
    replay = await service.persist_bootstrap(**kwargs)

    assert replay == first
    assert [item.quality_status.value for item in manifests.manifests] == [
        "pending",
        "pass",
    ]


async def test_manifest_verification_failure_keeps_committed_capture_and_object() -> None:
    manifests = MemoryManifestRepository()
    repository = AtomicFakeAppCaptureRepository(manifests)
    store = UnreadableArtifactStore()
    service, _, _ = _service(
        repository=repository,
        store=store,
        manifest_repository=manifests,
    )

    persisted = await service.persist_bootstrap(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        artifacts=_artifact_set(),
        expected_bootstrap_row_version=0,
        workflow_lease_owner="app-worker-1",
        workflow_lease_token="33333333-3333-3333-3333-333333333333",
    )

    assert repository.save_calls
    assert store.deleted == []
    assert [item.quality_status.value for item in manifests.manifests] == [
        "pending",
        "fail",
    ]
    assert manifests.manifests[-1].capture_id == persisted.capture.capture_id


async def test_action_retry_deletes_objects_not_referenced_by_historical_capture() -> None:
    repository = HistoricalActionCaptureRepository()
    service, _, store = _service(repository=repository)

    persisted = await service.persist_action_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        artifacts=_action_artifact_set(),
        role="after_action",
        required_artifact_types=("screenshot", "action_trace"),
        parent_capture_id="88888888-8888-8888-8888-888888888888",
        action_execution_id="action-1",
        session_command_sequence=3,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    assert persisted.capture.capture_id == "99999999-9999-9999-9999-999999999999"
    assert persisted.screenshot.artifact_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert len(store.deleted) == 2
    assert all(path.startswith("11111111-1111-1111-1111-111111111111/") for path in store.deleted)


async def test_action_database_failure_deletes_all_uploaded_artifacts() -> None:
    repository = FakeAppCaptureRepository()
    repository.error = RuntimeError("database failed")
    service, _, store = _service(repository=repository)

    with pytest.raises(RuntimeError, match="database failed"):
        await service.persist_action_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            surface_kind=SurfaceKind.ANDROID,
            artifacts=_action_artifact_set(),
            role="after_action",
            required_artifact_types=("screenshot", "action_trace"),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            action_execution_id="action-1",
            session_command_sequence=3,
            finalization_epoch=2,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert len(store.deleted) == 2


async def test_observation_database_failure_deletes_all_uploaded_artifacts() -> None:
    repository = FakeAppCaptureRepository()
    repository.error = RuntimeError("database failed")
    service, _, store = _service(repository=repository)

    with pytest.raises(RuntimeError, match="database failed"):
        await service.persist_observation_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            surface_kind=SurfaceKind.ANDROID,
            artifacts=_artifact_set(),
            required_artifact_types=("screenshot",),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            observation_id="observation-db-failure-1",
            surface_phase_run_id="phase-1",
            surface_phase_decision_id="decision-1",
            session_command_sequence=4,
            finalization_epoch=2,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert len(store.deleted) == 1


@pytest.mark.parametrize("extra_type", ["visible_text", "unknown_artifact"])
async def test_action_persistence_rejects_extra_raw_artifacts(extra_type: str) -> None:
    source = _action_artifact_set()
    artifacts = replace(
        source,
        artifacts=(*source.artifacts, RawArtifact(artifact_type=extra_type, content={})),
    )
    service, _repository, store = _service()

    with pytest.raises(ValueError, match="Capture Profile 不一致"):
        await service.persist_action_capture(
            task_id=source.task_id,
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id=source.session_id,
            surface_kind=SurfaceKind.ANDROID,
            artifacts=artifacts,
            role="after_action",
            required_artifact_types=("screenshot", "action_trace"),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            action_execution_id=f"action-extra-{extra_type}",
            session_command_sequence=3,
            finalization_epoch=2,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert store.store_calls == []


async def test_observation_persistence_rejects_known_extra_raw_artifact() -> None:
    service, _repository, store = _service()

    with pytest.raises(ValueError, match="Capture Directive 不一致"):
        await service.persist_observation_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            surface_kind=SurfaceKind.ANDROID,
            artifacts=_action_artifact_set(),
            required_artifact_types=("screenshot",),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            observation_id="observation-extra-1",
            surface_phase_run_id="phase-1",
            surface_phase_decision_id="decision-1",
            session_command_sequence=4,
            finalization_epoch=2,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert store.store_calls == []


async def test_action_artifact_validation_failure_deletes_current_upload() -> None:
    store = InvalidActionArtifactStore()
    service, _, _ = _service(store=store)

    with pytest.raises(ValueError, match="Artifact 元数据不一致"):
        await service.persist_action_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            surface_kind=SurfaceKind.ANDROID,
            artifacts=_action_artifact_set(),
            role="before_action",
            required_artifact_types=("screenshot", "action_trace"),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            action_execution_id="action-1",
            session_command_sequence=3,
            finalization_epoch=2,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert len(store.store_calls) == 1
    stored = store.store_calls[0]
    assert store.deleted == [
        f"{stored['task_id']}/{stored['page_capture_id']}/{stored['artifact_id']}.png"
    ]


async def test_action_capture_rejects_noncanonical_identity_before_formal_write() -> None:
    store = InvalidIdentityArtifactStore()
    service, repository, _ = _service(store=store)

    with pytest.raises(ValueError, match="checksum"):
        await service.persist_action_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            surface_kind=SurfaceKind.ANDROID,
            artifacts=_action_artifact_set(),
            role="before_action",
            required_artifact_types=("screenshot", "action_trace"),
            parent_capture_id="88888888-8888-8888-8888-888888888888",
            action_execution_id="action-identity-1",
            session_command_sequence=3,
            finalization_epoch=2,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    assert repository.save_calls == []
    assert len(store.deleted) == 2


async def test_persist_bootstrap_rejects_blank_storage_path_before_database_write() -> None:
    repository = FakeAppCaptureRepository()
    service, _, store = _service(repository=repository, store=BlankPathArtifactStore())

    with pytest.raises(ValueError, match="存储路径"):
        await service.persist_bootstrap(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            artifacts=_artifact_set(),
            expected_bootstrap_row_version=0,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )

    assert repository.save_calls == []
    assert store.deleted == [" "]


async def test_existing_bootstrap_capture_skips_object_rewrite() -> None:
    repository = FakeAppCaptureRepository()
    manifests = MemoryManifestRepository()
    screenshot = PersistedEvidenceArtifact(
        artifact_id="artifact-existing",
        artifact_type="screenshot",
        storage_path="existing/screenshot.png",
        content_type="image/png",
        checksum=f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}",
        size_bytes=10,
        metadata={},
    )
    repository.existing = PersistedAppCapture(
        capture=CaptureRef(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
            capture_id="44444444-4444-4444-4444-444444444444",
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="bootstrap",
            artifact_ids=(screenshot.artifact_id,),
        ),
        agent_run_id="22222222-2222-2222-2222-222222222222",
        screenshot=screenshot,
        width=1,
        height=1,
        orientation="portrait",
        image_signature=f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}",
        package_name="com.example.target",
        activity=".MainActivity",
        bootstrap_row_version=8,
    )
    service, _, store = _service(
        repository=repository,
        manifest_repository=manifests,
    )

    persisted = await service.persist_bootstrap(
        task_id=repository.existing.capture.task_id,
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id=repository.existing.capture.session_id,
        artifacts=_artifact_set(),
        expected_bootstrap_row_version=7,
        workflow_lease_owner="app-worker-1",
        workflow_lease_token="33333333-3333-3333-3333-333333333333",
    )

    assert persisted is repository.existing
    assert store.store_calls == []
    assert repository.save_calls == []
    assert [item.capture_id for item in manifests.manifests] == [
        repository.existing.capture.capture_id
    ]


async def test_existing_bootstrap_rejects_a_different_png() -> None:
    repository = FakeAppCaptureRepository()
    screenshot = PersistedEvidenceArtifact(
        artifact_id="artifact-existing",
        artifact_type="screenshot",
        storage_path="existing/screenshot.png",
        content_type="image/png",
        checksum="sha256:different",
        size_bytes=10,
        metadata={},
    )
    repository.existing = PersistedAppCapture(
        capture=CaptureRef(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
            capture_id="44444444-4444-4444-4444-444444444444",
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="bootstrap",
            artifact_ids=(screenshot.artifact_id,),
        ),
        agent_run_id="22222222-2222-2222-2222-222222222222",
        screenshot=screenshot,
        width=1,
        height=1,
        orientation="portrait",
        image_signature="sha256:different",
        package_name="com.example.target",
        activity=".MainActivity",
        bootstrap_row_version=8,
    )
    service, _, store = _service(repository=repository)

    with pytest.raises(ValueError, match="不同的 Screenshot"):
        await service.persist_bootstrap(
            task_id=repository.existing.capture.task_id,
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id=repository.existing.capture.session_id,
            artifacts=_artifact_set(),
            expected_bootstrap_row_version=7,
            workflow_lease_owner="app-worker-1",
            workflow_lease_token="33333333-3333-3333-3333-333333333333",
        )

    assert store.store_calls == []


def test_screenshot_bytes_serialize_as_png_not_json() -> None:
    data, extension, content_type, metadata = serialize_artifact("screenshot", PNG_1X1)

    assert data == PNG_1X1
    assert extension == "png"
    assert content_type == "image/png"
    assert metadata == {"encoding": "binary"}
