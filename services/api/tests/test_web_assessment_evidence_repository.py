"""Web 模型证据元数据仓储只返回同一已完成 Capture 的受信附件。"""

import pytest

from services.api.app.infrastructure.db.web_assessment_evidence_repository import (
    PostgresWebAssessmentArtifactMetadataReader,
)


class AcquireContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return AcquireContext(self.connection)


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return self.rows


@pytest.mark.asyncio
async def test_reads_requested_artifacts_in_requested_order() -> None:
    connection = Connection(
        [
            {
                "id": "dom-id",
                "task_id": "task-1",
                "page_capture_id": "capture-1",
                "artifact_type": "dom",
                "storage_path": "task/capture/dom.html",
                "content_type": "text/html",
                "checksum": "sha256:" + "b" * 64,
                "size_bytes": 20,
            },
            {
                "id": "shot-id",
                "task_id": "task-1",
                "page_capture_id": "capture-1",
                "artifact_type": "screenshot",
                "storage_path": "task/capture/shot.png",
                "content_type": "image/png",
                "checksum": "sha256:" + "a" * 64,
                "size_bytes": 100,
            },
        ]
    )

    result = await PostgresWebAssessmentArtifactMetadataReader(
        pool=Pool(connection)
    ).read_many(artifact_ids=("shot-id", "dom-id"))

    assert tuple(item.artifact_id for item in result) == ("shot-id", "dom-id")
    assert connection.args == (["shot-id", "dom-id"],)
    assert "capture.capture_status = 'captured'" in connection.query
    assert "artifact.source IN ('baseline', 'rule_supplement')" in connection.query


@pytest.mark.asyncio
async def test_guide_model_can_validate_ui_snapshot_and_action_trace_binding() -> None:
    connection = Connection([])
    with pytest.raises(RuntimeError, match="METADATA_MISSING"):
        await PostgresWebAssessmentArtifactMetadataReader(
            pool=Pool(connection)
        ).read_many(artifact_ids=("ui-id", "trace-id"))
    assert "'ui_snapshot'" in connection.query
    assert "'action_trace'" in connection.query


@pytest.mark.asyncio
async def test_missing_requested_artifact_fails_closed() -> None:
    connection = Connection([])
    with pytest.raises(RuntimeError, match="METADATA_MISSING"):
        await PostgresWebAssessmentArtifactMetadataReader(
            pool=Pool(connection)
        ).read_many(artifact_ids=("shot-id", "dom-id"))


@pytest.mark.asyncio
async def test_reads_capture_lineage_metadata_for_supplemental_validation() -> None:
    """仓储遗漏父节点或用途时，加载器将无法拒绝伪造的长图。"""

    connection = Connection(
        [
            {
                "id": "full-page-id",
                "task_id": "task-1",
                "page_capture_id": "capture-full-page",
                "parent_capture_id": "capture-primary",
                "capture_role": "supplemental",
                "capture_purpose": "web_full_page_supplemental",
                "supplemental_phase_protocol_version": "web-full-page-supplemental.v1",
                "artifact_type": "screenshot",
                "storage_path": "task/capture/full-page.png",
                "content_type": "image/png",
                "checksum": "sha256:" + "a" * 64,
                "size_bytes": 100,
            }
        ]
    )

    result = await PostgresWebAssessmentArtifactMetadataReader(
        pool=Pool(connection)
    ).read_many(artifact_ids=("full-page-id",))

    assert result[0].parent_capture_id == "capture-primary"
    assert result[0].capture_role == "supplemental"
    assert result[0].capture_purpose == "web_full_page_supplemental"
    assert result[0].supplemental_phase_protocol_version == (
        "web-full-page-supplemental.v1"
    )
    normalized_query = " ".join(connection.query.split())
    assert "phase.source_capture_id=capture.parent_page_capture_id" in normalized_query
    assert "phase.phase_key='web_full_page_supplemental'" in normalized_query
    assert "phase.phase_protocol_version='web-full-page-supplemental.v1'" in normalized_query
    assert "phase.evidence_artifact_ids ? artifact.id::text" in normalized_query
