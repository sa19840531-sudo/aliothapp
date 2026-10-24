"""播放器媒体状态读取及补判诊断落库的可信身份合同。"""

import hashlib
import json

import pytest

from services.api.app.application.interaction_runtime import SemanticTargetResolutionError
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)

TASK = "00000000-0000-0000-0000-000000000001"
CAPTURE = "00000000-0000-0000-0000-000000000002"
RUN = "00000000-0000-0000-0000-000000000003"
ARTIFACT = "00000000-0000-0000-0000-000000000004"
MEDIA = {
    "schema_version": "media-state.v1",
    "url": "https://example.test/video",
    "items": [{"tag": "video", "selector": "#player", "ready_state": 4}],
}


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row


class Pool:
    def __init__(self, row):
        self.connection = Connection(row)

    def acquire(self):
        return self.connection


class Store:
    def __init__(self, payload):
        self.payload = payload

    async def read(self, path):
        assert path == f"{TASK}/{CAPTURE}/{ARTIFACT}.json"
        return self.payload


@pytest.mark.asyncio
async def test_media_state_reader_checks_capture_ownership_and_checksum():
    payload = json.dumps(MEDIA).encode()
    pool = Pool(
        {
            "storage_path": f"{TASK}/{CAPTURE}/{ARTIFACT}.json",
            "checksum": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "content_type": "application/json",
        }
    )
    reader = PostgresTrustedUiSnapshotReader(pool=pool, artifact_store=Store(payload))
    result = await reader.read_media_state(task_id=TASK, page_capture_id=CAPTURE)
    assert result["items"][0]["selector"] == "#player"
    sql, args = pool.connection.calls[0]
    assert "evidence.artifact_type = 'media_state'" in sql
    assert args == (TASK, CAPTURE)


@pytest.mark.asyncio
async def test_media_state_reader_rejects_tampered_payload():
    payload = json.dumps(MEDIA).encode()
    reader = PostgresTrustedUiSnapshotReader(
        pool=Pool(
            {
                "storage_path": f"{TASK}/{CAPTURE}/{ARTIFACT}.json",
                "checksum": "sha256:" + "0" * 64,
                "size_bytes": len(payload),
                "content_type": "application/json",
            }
        ),
        artifact_store=Store(payload),
    )
    with pytest.raises(SemanticTargetResolutionError, match="摘要"):
        await reader.read_media_state(task_id=TASK, page_capture_id=CAPTURE)


@pytest.mark.asyncio
async def test_player_visual_writer_keeps_page_type_and_scopes_capture():
    pool = Pool({"capture_id": CAPTURE})
    repo = PostgresWebCaptureEvidenceRepository(pool=pool)
    await repo.record_player_visual_recheck(
        task_id=TASK,
        agent_run_id=RUN,
        session_id="web:" + RUN,
        capture_id=CAPTURE,
        payload={
            "schema_version": "web-player-visual-recheck.v1",
            "status": "uncertain",
            "source_capture_id": CAPTURE,
            "evidence_capture_id": CAPTURE,
        },
    )
    sql, args = pool.connection.calls[0]
    assert "web_player_visual_recheck" in sql
    assert "page_type" not in sql
    assert args[:4] == (TASK, RUN, "web:" + RUN, CAPTURE)
