"""探索侧与判定侧共用取证端口的契约测试。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from services.api.app.application.evidence_access import (
    EVIDENCE_SOURCE_ARTIFACT_TABLE,
    EVIDENCE_SOURCE_MANIFEST,
    ManifestEvidenceAccess,
    describe_from_artifact_rows,
    describe_from_manifest,
    require_adjudicated,
)
from services.api.app.application.manifest_backed_packet import ManifestUnavailableError
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)

NOW = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "surface-session-1"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"
PAYLOAD = b"evidence-bytes"


def _reference(artifact_id: str = "artifact-1") -> ArtifactReference:
    return ArtifactReference(
        artifact_id=artifact_id,
        artifact_type="screenshot",
        role="primary",
        storage_path=f"{TASK_ID}/{CAPTURE_ID}/{artifact_id}.png",
        checksum=f"sha256:{hashlib.sha256(PAYLOAD).hexdigest()}",
        size_bytes=len(PAYLOAD),
        content_type="image/png",
    )


def _manifest(quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS):
    return build_evidence_manifest_reference(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        manifest_version=2,
        artifacts=(_reference(),),
        quality_status=quality_status,
        quality_reason=(
            None if quality_status is EvidenceQualityStatus.PASS else "证据不足"
        ),
        created_at=NOW,
    )


class FakeManifests:
    def __init__(self, manifest=None) -> None:
        self.manifest = manifest
        self.calls: list[dict[str, object]] = []

    async def get(self, *, manifest_id: str):
        return self.manifest

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ):
        self.calls.append({"require_quality_pass": require_quality_pass})
        if self.manifest is None or self.manifest.capture_id != capture_id:
            return None
        if (
            require_quality_pass
            and self.manifest.quality_status is not EvidenceQualityStatus.PASS
        ):
            return None
        return self.manifest


class FakeReader:
    def __init__(self) -> None:
        self.reads: list[str] = []

    async def read(self, storage_path: str) -> bytes:
        self.reads.append(storage_path)
        return PAYLOAD


def _access(
    manifest=None,
    *,
    require_quality_pass: bool = True,
) -> tuple[ManifestEvidenceAccess, FakeManifests, FakeReader]:
    manifests = FakeManifests(manifest if manifest is not None else _manifest())
    reader = FakeReader()
    return (
        ManifestEvidenceAccess(
            manifests=manifests,
            artifact_reader=reader,
            require_quality_pass=require_quality_pass,
        ),
        manifests,
        reader,
    )


# ---------- describe：只看身份，不读原文 ----------


async def test_describe_reads_manifest_without_touching_object_storage() -> None:
    access, _, reader = _access()

    descriptor = await access.describe_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert descriptor is not None
    assert descriptor.evidence_source == EVIDENCE_SOURCE_MANIFEST
    assert descriptor.manifest_version == 2
    assert descriptor.quality_status == "pass"
    assert descriptor.adjudicated_pass is True
    assert [item.artifact_id for item in descriptor.artifacts] == ["artifact-1"]
    # 轻量通道：不拉原文
    assert reader.reads == []


async def test_describe_does_not_filter_by_quality() -> None:
    """探索侧要能看到未过裁决的证据状态，不能被过滤成"没有"。"""
    access, manifests, _ = _access(_manifest(EvidenceQualityStatus.FAIL))

    descriptor = await access.describe_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert descriptor is not None
    assert descriptor.quality_status == "fail"
    assert descriptor.adjudicated_pass is False
    assert manifests.calls[0]["require_quality_pass"] is False


async def test_describe_returns_none_without_manifest() -> None:
    access = ManifestEvidenceAccess(
        manifests=FakeManifests(None),
        artifact_reader=FakeReader(),
    )

    assert await access.describe_capture(task_id=TASK_ID, capture_id=CAPTURE_ID) is None


# ---------- load：读原文并校验 ----------


async def test_load_reads_and_verifies_payloads() -> None:
    access, _, reader = _access()

    packet = await access.load_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert packet.manifest_version == 2
    assert packet.artifacts[0].payload == PAYLOAD
    assert reader.reads == [_reference().storage_path]


async def test_load_for_capture_alias_matches_loader_signature() -> None:
    """别名保证本端口可直接顶替加载器注入判定侧。"""
    access, _, _ = _access()

    packet = await access.load_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert packet.capture_id == CAPTURE_ID


async def test_load_rejects_unadjudicated_manifest() -> None:
    access, _, _ = _access(_manifest(EvidenceQualityStatus.FAIL))

    with pytest.raises(ManifestUnavailableError):
        await access.load_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)


# ---------- 描述符语义 ----------


def test_descriptor_reports_missing_types() -> None:
    descriptor = describe_from_manifest(_manifest())

    assert descriptor.missing_types(("screenshot",)) == ()
    assert descriptor.missing_types(("screenshot", "dom")) == ("dom",)
    assert descriptor.artifact_types == {"screenshot"}


def test_artifact_table_fallback_is_never_treated_as_adjudicated() -> None:
    """回退证据不冒充已裁决证据。"""
    descriptor = describe_from_artifact_rows(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        rows=[
            {
                "artifact_id": "artifact-1",
                "artifact_type": "screenshot",
                "storage_path": "p",
                "checksum": "sha256:x",
                "size_bytes": 10,
                "content_type": "image/png",
            }
        ],
    )

    assert descriptor.evidence_source == EVIDENCE_SOURCE_ARTIFACT_TABLE
    assert descriptor.manifest_backed is False
    assert descriptor.adjudicated_pass is False
    assert descriptor.manifest_id is None


def test_audit_payload_carries_identity_only() -> None:
    payload = describe_from_manifest(_manifest()).to_audit_payload()

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "payload" not in serialized
    assert payload["artifact_count"] == 1
    assert payload["quality_status"] == "pass"


# ---------- 判定侧前置 ----------


def test_require_adjudicated_accepts_passed_manifest() -> None:
    descriptor = describe_from_manifest(_manifest())

    assert require_adjudicated(descriptor) is descriptor


def test_require_adjudicated_rejects_missing_descriptor() -> None:
    with pytest.raises(ManifestUnavailableError, match="没有可用于判定的证据清单"):
        require_adjudicated(None)


def test_require_adjudicated_rejects_artifact_table_source() -> None:
    descriptor = describe_from_artifact_rows(
        task_id=TASK_ID, capture_id=CAPTURE_ID, rows=[]
    )

    with pytest.raises(ManifestUnavailableError, match="未受清单裁决约束"):
        require_adjudicated(descriptor)


def test_require_adjudicated_rejects_unadjudicated_manifest() -> None:
    descriptor = describe_from_manifest(_manifest(EvidenceQualityStatus.PENDING))

    with pytest.raises(ManifestUnavailableError, match="未通过齐全性裁决"):
        require_adjudicated(descriptor)


def test_tool_and_judge_share_the_same_source_vocabulary() -> None:
    """两侧证据来源标注必须同源，否则同一采集在两边说法不一致。"""
    import inspect

    from services.api.app.agents.tools import action_evidence_tools

    source = inspect.getsource(action_evidence_tools._capture_from_row)

    assert "EVIDENCE_SOURCE_MANIFEST" in source
    assert "EVIDENCE_SOURCE_ARTIFACT_TABLE" in source
