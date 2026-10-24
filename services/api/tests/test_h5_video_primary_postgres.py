"""同批旧样本不能抢占最终主帧；实际 PG 查询与真实存储摘要闭包。"""

import base64
import json
import os
from io import BytesIO
from uuid import UUID, uuid4

import asyncpg
import pytest
from PIL import Image

from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_task_finalization_persistence import FakePool


@pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需要显式隔离数据库")
@pytest.mark.parametrize("primary", [True, False])
async def test_ui_reader_uses_final_primary_not_old_sample_with_same_timestamp(tmp_path, primary):
    database_url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL"),
    )
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,capture_status text);
            CREATE TEMP TABLE qc_evidence_artifact(id uuid,task_id uuid,page_capture_id uuid,
                artifact_type text,storage_path text,checksum text,size_bytes int,
                content_type text,source text,metadata jsonb,created_at timestamptz);
        """)
        task, capture = str(uuid4()), str(uuid4())
        await connection.execute("INSERT INTO qc_capture VALUES($1,$2,'captured')", capture, task)
        store = LocalEvidenceArtifactStore(root=tmp_path)
        images = []
        for index, color in enumerate(((10, 20, 30), (100, 150, 200))):
            stream = BytesIO()
            Image.new("RGB", (4, 4), color).save(stream, format="PNG")
            artifact = await store.store(
                task_id=task, page_capture_id=capture, artifact_id=str(UUID(int=index + 1)),
                artifact_type="screenshot", content={
                    "encoding": "base64", "data": base64.b64encode(stream.getvalue()).decode(),
                    "purpose": ("h5_video_primary" if primary and index
                                else "video_publication_sample"),
                    "full_page": False,
                },
            )
            images.append(artifact)
        artifact = await store.store(
            task_id=task, page_capture_id=capture, artifact_id=str(uuid4()),
            artifact_type="ui_snapshot", content={"url": "https://video.example.test/1"},
        )
        for item in (*images, artifact):
            await connection.execute("""
                INSERT INTO qc_evidence_artifact VALUES($1,$2,$3,$4,$5,$6,$7,$8,'baseline',
                    $9::jsonb,'2026-10-16T00:00:00Z')
            """, item.artifact_id, task, capture, item.artifact_type, item.storage_path,
                item.checksum, item.size_bytes, item.content_type, json.dumps(item.metadata))
        result = await PostgresTrustedUiSnapshotReader(
            pool=FakePool(connection), artifact_store=store,
        ).read_ui_snapshot(task_id=task, page_capture_id=capture)
        assert images[0].checksum != images[1].checksum
        assert result["_capture_visual_checksum"] == images[int(primary)].checksum
        if primary:
            assert images[1].metadata["full_page"] is False
    finally:
        await connection.close()


@pytest.mark.parametrize("full_page", [False, True, "false", None])
def test_video_primary_preserves_only_actual_boolean_full_page(full_page):
    from services.api.app.infrastructure.storage.local_evidence_store import serialize_artifact

    _, _, _, metadata = serialize_artifact("screenshot", {
        "purpose": "h5_video_primary", "encoding": "base64", "data": "",
        "full_page": full_page,
    })
    if type(full_page) is bool:
        assert metadata["full_page"] is full_page
    else:
        assert "full_page" not in metadata
