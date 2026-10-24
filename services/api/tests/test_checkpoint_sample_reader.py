"""采样附件与实际 PNG 的归属、数量和摘要反例。"""

import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image

from services.api.app.application.interaction_runtime import SemanticTargetResolutionError
from services.api.app.infrastructure.browser.h5_player_frames import video_region_digest
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_task_finalization_persistence import FakePool


@pytest.mark.parametrize("fault", [None, "one_png", "checksum", "foreign_path", "region"])
async def test_sample_reader_requires_two_real_owned_png_references(fault):
    task, capture = str(uuid4()), str(uuid4())
    package = {
        "schema_version": "checkpoint-observation.v1",
        "samples": [sample(), sample(instant=1600)],
    }
    content = json.dumps(package).encode()
    identity = str(uuid4())
    row = dict(
        id=identity,
        artifact_type="checkpoint_observation",
        storage_path=f"{task}/{capture}/{identity}.json",
        checksum="sha256:" + hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="application/json",
    )
    pngs = [
        dict(id=str(uuid4()), artifact_type="screenshot", checksum="sha256:" + "a" * 64)
        for _ in range(2)
    ]
    if fault == "one_png":
        pngs.pop()
    elif fault == "checksum":
        row["checksum"] = "sha256:" + "b" * 64
    elif fault == "foreign_path":
        row["storage_path"] = f"{str(uuid4())}/{capture}/{identity}.json"
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[row, *pngs]))
    reader = PostgresTrustedUiSnapshotReader(
        pool=FakePool(connection),
        artifact_store=SimpleNamespace(read=AsyncMock(return_value=content)),
    )
    # PNG 的真实摘要与读取边界另有原有读取器测试，此处聚焦两个不同引用的闭包。
    reader.read_visual_evidence_image = AsyncMock()
    stream = BytesIO()
    Image.new("RGB", (360, 640), (30, 80, 150)).save(stream, format="PNG")
    png = stream.getvalue()
    reader.read_visual_evidence_image.side_effect = lambda **kwargs: png
    with_digest = hashlib.sha256(png).hexdigest()
    for item in package["samples"]:
        item["screenshot_sha256"] = with_digest
        item["video_region_sha256"] = video_region_digest(png, item["snapshot"])
    if fault == "region":
        package["samples"][-1]["video_region_sha256"] = "f" * 64
    for item in pngs:
        item["checksum"] = "sha256:" + with_digest
    content = json.dumps(package).encode()
    row["size_bytes"] = len(content)
    if fault != "checksum":
        row["checksum"] = "sha256:" + hashlib.sha256(content).hexdigest()
    reader._artifact_store.read.return_value = content
    if fault is None:
        result = await reader.read_checkpoint_observation(task_id=task, page_capture_id=capture)
        assert len(result["samples"]) == 2
        assert reader.read_visual_evidence_image.await_count == 2
    else:
        with pytest.raises((ValueError, SemanticTargetResolutionError)):
            await reader.read_checkpoint_observation(task_id=task, page_capture_id=capture)
