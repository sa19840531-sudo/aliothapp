"""App PNG 对象存储契约：二进制原样保存，数据库仅引用对象及其校验事实。"""

from __future__ import annotations

import hashlib

from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
)

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


async def test_local_store_preserves_png_bytes_and_returns_object_reference(tmp_path) -> None:
    store = LocalEvidenceArtifactStore(root=tmp_path)

    artifact = await store.store(
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
        artifact_id="33333333-3333-3333-3333-333333333333",
        artifact_type="screenshot",
        content=PNG_1X1,
    )

    assert artifact.storage_path.endswith(".png")
    assert artifact.content_type == "image/png"
    assert artifact.checksum == f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}"
    assert artifact.size_bytes == len(PNG_1X1)
    assert artifact.metadata == {"encoding": "binary"}
    assert await store.read(artifact.storage_path) == PNG_1X1

