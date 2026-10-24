"""E19：ManifestBackedPacketLoader 的只读、清单来源与完整性校验测试。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from services.api.app.application.manifest_backed_packet import (
    ManifestBackedPacketLoader,
    ManifestIntegrityError,
    ManifestUnavailableError,
    verify_artifact_payload,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "surface-session-1"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"


def _reference(
    *,
    artifact_id: str,
    artifact_type: str,
    payload: bytes,
    role: str = "primary",
    content_type: str | None = "application/octet-stream",
    storage_path: str | None = None,
) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        role=role,
        storage_path=storage_path or f"{TASK_ID}/{CAPTURE_ID}/{artifact_id}.data",
        checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size_bytes=len(payload),
        content_type=content_type,
    )


def _manifest(
    references: tuple[ArtifactReference, ...],
    *,
    quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS,
    manifest_version: int = 2,
    task_id: str = TASK_ID,
    capture_id: str = CAPTURE_ID,
):
    return build_evidence_manifest_reference(
        task_id=task_id,
        session_id=SESSION_ID,
        capture_id=capture_id,
        manifest_version=manifest_version,
        artifacts=references,
        quality_status=quality_status,
        quality_reason=(
            None if quality_status is EvidenceQualityStatus.PASS else "证据不足"
        ),
        created_at=NOW,
    )


class FakeManifestSource:
    def __init__(self, manifest=None) -> None:
        self.manifest = manifest
        self.latest_calls: list[dict[str, object]] = []

    async def get(self, *, manifest_id: str):
        if self.manifest is not None and self.manifest.manifest_id == manifest_id:
            return self.manifest
        return None

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ):
        self.latest_calls.append(
            {
                "task_id": task_id,
                "capture_id": capture_id,
                "require_quality_pass": require_quality_pass,
            }
        )
        if self.manifest is None:
            return None
        if (
            require_quality_pass
            and self.manifest.quality_status is not EvidenceQualityStatus.PASS
        ):
            return None
        return self.manifest


class FakeArtifactReader:
    """只暴露 read 的存储读端口；任何写入尝试都会 AttributeError。"""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    async def read(self, storage_path: str) -> bytes:
        self.reads.append(storage_path)
        if storage_path not in self.objects:
            raise FileNotFoundError(storage_path)
        return self.objects[storage_path]


def _loader(
    references: tuple[ArtifactReference, ...],
    *,
    objects: dict[str, bytes] | None = None,
    quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS,
    require_quality_pass: bool = True,
    manifest=None,
) -> tuple[ManifestBackedPacketLoader, FakeManifestSource, FakeArtifactReader]:
    resolved = manifest if manifest is not None else _manifest(
        references, quality_status=quality_status
    )
    source = FakeManifestSource(resolved)
    reader = FakeArtifactReader(objects if objects is not None else {})
    loader = ManifestBackedPacketLoader(
        manifests=source,
        artifact_reader=reader,
        require_quality_pass=require_quality_pass,
    )
    return loader, source, reader


async def test_load_for_capture_sources_every_artifact_from_manifest() -> None:
    screenshot = b"\x89PNG-bytes"
    visible_text = "页面文案".encode()
    references = (
        _reference(artifact_id="artifact-1", artifact_type="screenshot", payload=screenshot),
        _reference(
            artifact_id="artifact-2",
            artifact_type="visible_text",
            payload=visible_text,
            content_type="text/plain; charset=utf-8",
        ),
    )
    objects = {
        references[0].storage_path: screenshot,
        references[1].storage_path: visible_text,
    }
    loader, source, reader = _loader(references, objects=objects)

    packet = await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert packet.task_id == TASK_ID
    assert packet.session_id == SESSION_ID
    assert packet.capture_id == CAPTURE_ID
    assert packet.manifest_version == 2
    assert packet.quality_status is EvidenceQualityStatus.PASS
    assert packet.artifact_ids == ("artifact-1", "artifact-2")
    assert packet.artifact_types == {"screenshot", "visible_text"}
    # 读取路径完全来自清单声明，未查询附件表
    assert reader.reads == [item.storage_path for item in references]
    # E02 条款 2.1：取最新清单时不加 pass 过滤，否则「缺席」与「未过裁决」无法分型
    assert source.latest_calls == [
        {"task_id": TASK_ID, "capture_id": CAPTURE_ID, "require_quality_pass": False}
    ]
    assert packet.require_unique("visible_text").decode_text() == "页面文案"


async def test_packet_exposes_manifest_identity_for_downstream_audit() -> None:
    payload = b"trace"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="action_trace", payload=payload
    )
    manifest = _manifest((reference,))
    loader, _, _ = _loader(
        (reference,),
        objects={reference.storage_path: payload},
        manifest=manifest,
    )

    packet = await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert packet.manifest_id == manifest.manifest_id
    assert packet.manifest_hash == manifest.manifest_hash
    assert packet.manifest_version == manifest.manifest_version


async def test_missing_manifest_raises_unavailable() -> None:
    loader = ManifestBackedPacketLoader(
        manifests=FakeManifestSource(None),
        artifact_reader=FakeArtifactReader({}),
    )

    with pytest.raises(ManifestUnavailableError, match="没有可用于判定的证据清单"):
        await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)


async def test_non_pass_manifest_is_rejected_by_default() -> None:
    payload = b"png"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="screenshot", payload=payload
    )
    loader, _, _ = _loader(
        (reference,),
        objects={reference.storage_path: payload},
        quality_status=EvidenceQualityStatus.FAIL,
    )

    with pytest.raises(ManifestUnavailableError):
        await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)


async def test_pending_manifest_readable_only_when_pass_not_required() -> None:
    payload = b"png"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="screenshot", payload=payload
    )
    loader, source, _ = _loader(
        (reference,),
        objects={reference.storage_path: payload},
        quality_status=EvidenceQualityStatus.PENDING,
        require_quality_pass=False,
    )

    packet = await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert packet.quality_status is EvidenceQualityStatus.PENDING
    assert source.latest_calls[0]["require_quality_pass"] is False


async def test_tampered_payload_raises_integrity_error() -> None:
    payload = b"original-bytes"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="dom", payload=payload
    )
    tampered = b"tampered-bytes"
    assert len(tampered) == len(payload)
    loader, _, _ = _loader((reference,), objects={reference.storage_path: tampered})

    with pytest.raises(ManifestIntegrityError, match="校验和不一致"):
        await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)


async def test_size_mismatch_raises_integrity_error() -> None:
    payload = b"original"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="dom", payload=payload
    )
    loader, _, _ = _loader(
        (reference,), objects={reference.storage_path: b"original-and-more"}
    )

    with pytest.raises(ManifestIntegrityError, match="大小与清单声明不一致"):
        await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)


async def test_unreadable_artifact_raises_integrity_error() -> None:
    payload = b"png"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="screenshot", payload=payload
    )
    loader, _, _ = _loader((reference,), objects={})

    with pytest.raises(ManifestIntegrityError, match="无法读取"):
        await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)


async def test_foreign_storage_path_is_refused_before_read() -> None:
    payload = b"png"
    reference = _reference(
        artifact_id="artifact-1",
        artifact_type="screenshot",
        payload=payload,
        storage_path="55555555-5555-5555-5555-555555555555/other-capture/artifact-1.data",
    )
    loader, _, reader = _loader(
        (reference,), objects={reference.storage_path: payload}
    )

    with pytest.raises(ManifestIntegrityError, match="存储路径归属不匹配"):
        await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)
    assert reader.reads == []


async def test_manifest_of_other_task_is_refused() -> None:
    payload = b"png"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="screenshot", payload=payload
    )
    foreign = _manifest((reference,), task_id="99999999-9999-9999-9999-999999999999")
    loader = ManifestBackedPacketLoader(
        manifests=FakeManifestSource(foreign),
        artifact_reader=FakeArtifactReader({reference.storage_path: payload}),
    )

    with pytest.raises(ManifestUnavailableError, match="不属于当前任务"):
        await loader.load_for_manifest(
            task_id=TASK_ID, manifest_id=foreign.manifest_id
        )


async def test_load_for_manifest_reads_declared_version() -> None:
    payload = b"png"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="screenshot", payload=payload
    )
    manifest = _manifest((reference,), manifest_version=5)
    loader, _, _ = _loader(
        (reference,), objects={reference.storage_path: payload}, manifest=manifest
    )

    packet = await loader.load_for_manifest(
        task_id=TASK_ID, manifest_id=manifest.manifest_id
    )

    assert packet.manifest_version == 5
    assert packet.manifest_id == manifest.manifest_id


async def test_unknown_manifest_id_raises_unavailable() -> None:
    loader = ManifestBackedPacketLoader(
        manifests=FakeManifestSource(None),
        artifact_reader=FakeArtifactReader({}),
    )

    with pytest.raises(ManifestUnavailableError, match="不存在"):
        await loader.load_for_manifest(task_id=TASK_ID, manifest_id="evidence-manifest:x")


async def test_loader_holds_no_write_capability() -> None:
    """E19 验收：调用方不修改或补写原始证据——加载器结构上没有写端口。"""
    payload = b"png"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="screenshot", payload=payload
    )
    loader, source, reader = _loader(
        (reference,), objects={reference.storage_path: payload}
    )

    await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    # 依赖端口只有读方法；写入类方法一律不存在
    for forbidden in ("store", "delete", "write", "put"):
        assert not hasattr(reader, forbidden)
    for forbidden in ("add", "save", "update"):
        assert not hasattr(source, forbidden)


async def test_require_unique_rejects_duplicate_type() -> None:
    first = b"png-1"
    second = b"png-2"
    references = (
        _reference(artifact_id="artifact-1", artifact_type="screenshot", payload=first),
        _reference(artifact_id="artifact-2", artifact_type="screenshot", payload=second),
    )
    loader, _, _ = _loader(
        references,
        objects={
            references[0].storage_path: first,
            references[1].storage_path: second,
        },
    )

    packet = await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert len(packet.artifacts_of_type("screenshot")) == 2
    with pytest.raises(ManifestIntegrityError, match="唯一的 screenshot"):
        packet.require_unique("screenshot")


async def test_artifacts_of_role_projects_capture_roles() -> None:
    before = b"before-png"
    after = b"after-png"
    references = (
        _reference(
            artifact_id="artifact-1",
            artifact_type="screenshot",
            payload=before,
            role="before_action",
        ),
        _reference(
            artifact_id="artifact-2",
            artifact_type="screenshot",
            payload=after,
            role="after_action",
        ),
    )
    loader, _, _ = _loader(
        references,
        objects={
            references[0].storage_path: before,
            references[1].storage_path: after,
        },
    )

    packet = await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert packet.artifacts_of_role("before_action")[0].artifact_id == "artifact-1"
    assert packet.artifacts_of_role("after_action")[0].artifact_id == "artifact-2"
    assert packet.artifacts_of_role("bootstrap") == ()


async def test_decode_json_reads_structured_evidence() -> None:
    trace = json.dumps([{"action": "click"}], ensure_ascii=False).encode()
    reference = _reference(
        artifact_id="artifact-1",
        artifact_type="action_trace",
        payload=trace,
        content_type="application/json",
    )
    loader, _, _ = _loader((reference,), objects={reference.storage_path: trace})

    packet = await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert packet.require_unique("action_trace").decode_json() == [{"action": "click"}]


async def test_decode_json_rejects_invalid_payload() -> None:
    broken = b"{not-json"
    reference = _reference(
        artifact_id="artifact-1",
        artifact_type="action_trace",
        payload=broken,
        content_type="application/json",
    )
    loader, _, _ = _loader((reference,), objects={reference.storage_path: broken})

    packet = await loader.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    with pytest.raises(ManifestIntegrityError, match="不是有效 JSON"):
        packet.require_unique("action_trace").decode_json()


def test_verify_artifact_payload_helper_matches_loader_rules() -> None:
    payload = b"content"
    reference = _reference(
        artifact_id="artifact-1", artifact_type="dom", payload=payload
    )

    assert verify_artifact_payload(payload, reference=reference) is True
    assert verify_artifact_payload(b"content!", reference=reference) is False
