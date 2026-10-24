"""权威恢复链的受控案例：等价标签不能代替实际来源与动作。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)


def seal(row):
    artifact = ArtifactReference(
        artifact_id="artifact-" + row["id"],
        artifact_type="screenshot",
        role="after_action",
        checksum="sha256:" + "a" * 64,
        storage_path="controlled.png",
        size_bytes=12,
        content_type="image/png",
    )
    manifest = build_evidence_manifest_reference(
        task_id=row["task_id"],
        session_id=row["session_id"],
        capture_id=row["id"],
        manifest_version=2,
        quality_status=EvidenceQualityStatus.PASS,
        created_at=row["captured_at"],
        artifacts=(artifact,),
    )
    row.update(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        manifest_version=2,
        manifest_payload=manifest.to_dict(),
        capture_role="after_action",
        artifacts=[
            dict(
                id=artifact.artifact_id,
                checksum=artifact.checksum,
                artifact_type=artifact.artifact_type,
                storage_path=artifact.storage_path,
            )
        ],
    )


@pytest.mark.parametrize("fault", [None, "epoch", "run", "no_parent", "cycle", "manifest"])
async def test_same_session_lineage_requires_actual_closed_capture_chain(fault):
    from services.api.app.infrastructure.db.checkpoint_recovery_binding import (
        load_checkpoint_lineage,
    )

    instant = datetime.now(UTC)
    row = {
        "id": "child",
        "task_id": "task",
        "agent_run_id": "run",
        "session_id": "session",
        "epoch": 0,
        "status": "captured",
        "captured_at": instant,
        "parent_capture_id": "origin",
        "manifest_id": "latest",
        "manifest_version": 2,
        "manifest_hash": "a" * 64,
        "quality_status": "pass",
    }
    origin = {
        **row,
        "id": "origin",
        "parent_capture_id": None,
        "captured_at": instant - timedelta(seconds=1),
    }
    seal(row)
    seal(origin)
    if fault == "epoch":
        row["epoch"] = 1
    elif fault == "run":
        row["agent_run_id"] = "other"
    elif fault == "no_parent":
        row["parent_capture_id"] = None
    elif fault == "cycle":
        row["parent_capture_id"] = "child"
    elif fault == "manifest":
        row["quality_status"] = "fail"

    async def fetchrow(sql, *args):
        return {"child": row, "origin": origin}.get(args[0])

    connection = SimpleNamespace(fetchrow=fetchrow, fetch=AsyncMock(return_value=[]))
    result = await load_checkpoint_lineage(
        connection,
        task_id="task",
        agent_run_id="run",
        epoch=0,
        source_capture_id="child",
        origin_capture_id="origin",
        recovery_policy="same_session",
        before=instant + timedelta(seconds=1),
        deadline=instant + timedelta(hours=1),
    )
    assert (result is not None) is (fault is None)


async def test_cross_session_without_authoritative_restoration_is_not_equivalent():
    from services.api.app.infrastructure.db.checkpoint_recovery_binding import (
        load_checkpoint_lineage,
    )

    instant = datetime.now(UTC)
    rows = {
        "child": dict(
            id="child",
            task_id="task",
            agent_run_id="run",
            session_id="new",
            epoch=0,
            status="captured",
            captured_at=instant,
            parent_capture_id="origin",
            manifest_id="m",
            manifest_version=1,
            manifest_hash="a" * 64,
            quality_status="pass",
        ),
    }
    rows["origin"] = {**rows["child"], "id": "origin", "session_id": "old"}
    for row in rows.values():
        seal(row)
    connection = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=lambda sql, *args: rows.get(args[0])),
        fetch=AsyncMock(return_value=[]),
    )
    assert (
        await load_checkpoint_lineage(
            connection,
            task_id="task",
            agent_run_id="run",
            epoch=0,
            source_capture_id="child",
            origin_capture_id="origin",
            recovery_policy="new_instance",
            before=instant + timedelta(seconds=1),
            deadline=instant + timedelta(hours=1),
        )
        is None
    )
