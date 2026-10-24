"""H5 预期动作证据库存的行为契约测试。"""

import hashlib
from dataclasses import replace

import pytest

from services.api.app.application.h5_evidence_inventory import (
    H5EvidenceArtifactRef,
    project_h5_evidence_preflight,
)
from services.api.app.infrastructure.storage.h5_evidence_object_verifier import (
    H5EvidenceObjectVerifier,
)


class _ArtifactStore:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, storage_path: str) -> bytes:
        assert storage_path == "task-1/capture-1/artifact-1.png"
        return self.payload


@pytest.mark.asyncio
async def test_object_verifier_reports_checksum_corruption_as_failed() -> None:
    """若读取的对象被篡改，发布前必须稳定归类为完整性失败。"""

    artifact = H5EvidenceArtifactRef(
        artifact_id="artifact-1",
        task_id="task-1",
        agent_run_id="run-1",
        capture_id="capture-1",
        storage_path="task-1/capture-1/artifact-1.png",
        artifact_type="screenshot",
        checksum=f"sha256:{hashlib.sha256(b'expected').hexdigest()}",
        size_bytes=len(b"expected"),
    )

    result = await H5EvidenceObjectVerifier(_ArtifactStore(b"corrupt!")).verify(
        artifact=artifact,
        expected_task_id="task-1",
        expected_agent_run_id="run-1",
        expected_capture_id="capture-1",
    )

    assert result.status.value == "failed"
    assert result.reason_code == "artifact_checksum_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_change", "store_payload", "expected_status", "reason"),
    [
        (
            {"capture_id": "other-capture"},
            b"expected",
            "failed",
            "artifact_capture_identity_mismatch",
        ),
        (
            {"agent_run_id": "other-run"},
            b"expected",
            "failed",
            "artifact_ownership_mismatch",
        ),
        ({"size_bytes": 1}, b"expected", "failed", "artifact_size_mismatch"),
        ({"checksum": ""}, b"expected", "insufficient", "artifact_metadata_missing"),
    ],
)
async def test_object_verifier_keeps_specific_identity_size_and_metadata_reasons(
    artifact_change: dict[str, object],
    store_payload: bytes,
    expected_status: str,
    reason: str,
) -> None:
    """身份、大小和元数据缺失不得被折叠为通用的证据不足。"""
    original = H5EvidenceArtifactRef(
        artifact_id="artifact-1",
        task_id="task-1",
        agent_run_id="run-1",
        capture_id="capture-1",
        storage_path="task-1/capture-1/artifact-1.png",
        artifact_type="screenshot",
        checksum=f"sha256:{hashlib.sha256(b'expected').hexdigest()}",
        size_bytes=len(b"expected"),
    )
    artifact = replace(original, **artifact_change)

    result = await H5EvidenceObjectVerifier(_ArtifactStore(store_payload)).verify(
        artifact=artifact,
        expected_task_id="task-1",
        expected_agent_run_id="run-1",
        expected_capture_id="capture-1",
    )

    assert result.status.value == expected_status
    assert result.reason_code == reason


@pytest.mark.asyncio
async def test_object_verifier_classifies_missing_object_as_insufficient() -> None:
    class _MissingStore:
        async def read(self, storage_path: str) -> bytes:
            del storage_path
            raise FileNotFoundError("missing")

    artifact = H5EvidenceArtifactRef(
        artifact_id="artifact-1",
        task_id="task-1",
        agent_run_id="run-1",
        capture_id="capture-1",
        storage_path="task-1/capture-1/artifact-1.png",
        artifact_type="dom",
        checksum="sha256:" + "0" * 64,
        size_bytes=1,
    )

    result = await H5EvidenceObjectVerifier(_MissingStore()).verify(
        artifact=artifact,
        expected_task_id="task-1",
        expected_agent_run_id="run-1",
        expected_capture_id="capture-1",
    )

    assert (result.status.value, result.reason_code) == (
        "insufficient",
        "artifact_object_missing",
    )


def test_evidence_preflight_blocks_nonpass_inventory_from_rule_publication() -> None:
    projection = project_h5_evidence_preflight(
        inventory_items=[
            {
                "inventory_item_id": "inventory-pass",
                "status": "pass",
                "after_capture_id": "capture-pass",
                "published_inspection_count": 1,
            },
            {
                "inventory_item_id": "inventory-missing",
                "status": "insufficient",
                "before_capture_id": "capture-before",
                "after_capture_id": "capture-missing",
                "reason_codes": ["artifact_object_missing"],
                "published_inspection_count": 0,
            },
        ],
        rule_executions=[
            {
                "execution_id": "execution-pass",
                "execution_status": "pass",
                "page_capture_id": "capture-pass",
            },
            {
                "execution_id": "execution-gap",
                "execution_status": "evidence_insufficient",
                "page_capture_id": "capture-missing",
            },
        ],
        closure={
            "gate_passed": False,
            "gate_block_reasons": ["evidence_insufficient"],
        },
    )

    assert projection == {
        "schema_version": "h5-evidence-preflight.v1",
        "status": "evidence_insufficient",
        "ready": False,
        "inventory_item_count": 2,
        "blocked_item_count": 1,
        "insufficient_count": 1,
        "failed_count": 0,
        "reason_codes": ["artifact_object_missing"],
        "blocked_inventory_item_ids": ["inventory-missing"],
        "blocked_capture_ids": ["capture-before", "capture-missing"],
        "publication_allowed": False,
        "invalid_evidence_publication_blocked": True,
        "improperly_published_inventory_item_ids": [],
        "improper_rule_result_ids": [],
        "gate_passed": False,
        "gate_block_reasons": ["evidence_insufficient"],
    }


def test_evidence_preflight_exposes_invalid_publication_and_rule_result() -> None:
    projection = project_h5_evidence_preflight(
        inventory_items=[
            {
                "inventory_item_id": "inventory-corrupt",
                "status": "failed",
                "after_capture_id": "capture-corrupt",
                "reason_codes": ["artifact_checksum_mismatch"],
                "published_inspection_count": 1,
            }
        ],
        rule_executions=[
            {
                "execution_id": "execution-corrupt",
                "execution_status": "fail",
                "page_capture_id": "capture-corrupt",
            }
        ],
        closure={"gate_passed": True},
    )

    assert projection["status"] == "evidence_integrity_failed"
    assert projection["publication_allowed"] is False
    assert projection["invalid_evidence_publication_blocked"] is False
    assert projection["improperly_published_inventory_item_ids"] == [
        "inventory-corrupt"
    ]
    assert projection["improper_rule_result_ids"] == ["execution-corrupt"]


def test_evidence_preflight_flags_aggregate_result_via_input_set_members() -> None:
    """聚合 execution 无 page_capture_id 时，按 InputSet 成员反查坏材料。"""
    projection = project_h5_evidence_preflight(
        inventory_items=[
            {
                "inventory_item_id": "inventory-corrupt",
                "status": "failed",
                "after_capture_id": "capture-corrupt",
                "reason_codes": ["artifact_checksum_mismatch"],
                "published_inspection_count": 0,
            }
        ],
        rule_executions=[
            {
                "execution_id": "execution-aggregate",
                "execution_status": "pass",
                "page_capture_id": None,
                "evidence_input_set_id": "input-set-1",
                "input_set_member_capture_ids": ["capture-ok", "capture-corrupt"],
            }
        ],
        closure={"gate_passed": True},
    )

    assert projection["invalid_evidence_publication_blocked"] is False
    assert projection["improper_rule_result_ids"] == ["execution-aggregate"]
