"""H5 对象存储重读与完整性校验。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from services.api.app.application.h5_evidence_inventory import (
    EvidenceArtifactReadPort,
    H5EvidenceArtifactRef,
    H5EvidenceInventoryStatus,
)


@dataclass(frozen=True, slots=True)
class H5EvidenceObjectVerification:
    status: H5EvidenceInventoryStatus
    reason_code: str | None
    summary: dict[str, object]


class H5EvidenceObjectVerifier:
    """以存储实际字节而非数据库元数据判定制品完整性。"""

    def __init__(self, store: EvidenceArtifactReadPort) -> None:
        self._store = store

    async def verify(
        self,
        *,
        artifact: H5EvidenceArtifactRef,
        expected_task_id: str,
        expected_agent_run_id: str,
        expected_capture_id: str,
    ) -> H5EvidenceObjectVerification:
        if (
            not artifact.artifact_id
            or not artifact.task_id
            or not artifact.agent_run_id
            or not artifact.capture_id
            or not artifact.storage_path
            or not artifact.artifact_type
            or not artifact.checksum
            or artifact.size_bytes < 0
        ):
            return _insufficient("artifact_metadata_missing")
        if artifact.task_id != expected_task_id or artifact.agent_run_id != expected_agent_run_id:
            return _failed("artifact_ownership_mismatch")
        if artifact.capture_id != expected_capture_id:
            return _failed("artifact_capture_identity_mismatch")
        try:
            payload = await self._store.read(artifact.storage_path)
        except (FileNotFoundError, KeyError):
            return _insufficient("artifact_object_missing")
        if len(payload) != artifact.size_bytes:
            return _failed("artifact_size_mismatch")
        checksum = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if checksum != artifact.checksum:
            return _failed("artifact_checksum_mismatch")
        return H5EvidenceObjectVerification(
            H5EvidenceInventoryStatus.PASS,
            None,
            {"size_bytes": len(payload), "checksum": checksum},
        )


def _failed(code: str) -> H5EvidenceObjectVerification:
    return H5EvidenceObjectVerification(H5EvidenceInventoryStatus.FAILED, code, {})


def _insufficient(code: str) -> H5EvidenceObjectVerification:
    return H5EvidenceObjectVerification(H5EvidenceInventoryStatus.INSUFFICIENT, code, {})
