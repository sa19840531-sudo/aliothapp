"""App 截圖 Artifact：PNG 二進位落庫 + URL 引用；Base64 僅備援。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest

from services.api.app.application.app_screenshot_artifacts import (
    AppScreenshotArtifactService,
    build_app_screenshot_artifact_url,
)
from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps
from services.api.app.infrastructure.device.android_capture_runtime import (
    build_mobile_demo_android_runtime,
    start_mobile_demo_android_runtime,
)
from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
    serialize_artifact,
)


def test_serialize_screenshot_accepts_raw_png_bytes() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    data, ext, ctype, meta = serialize_artifact("screenshot", png)
    assert data.startswith(b"\x89PNG")
    assert ext == "png"
    assert ctype == "image/png"
    assert meta["encoding"] == "binary"


def test_serialize_screenshot_still_accepts_base64_dict() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x01" * 16
    content = {"encoding": "base64", "data": base64.b64encode(png).decode("ascii")}
    data, ext, ctype, _meta = serialize_artifact("screenshot", content)
    assert data == png
    assert ext == "png"
    assert ctype == "image/png"


@pytest.mark.parametrize("purpose", ("ad_landing", "ad_landing_consistency"))
def test_serialize_ad_screenshot_keeps_recovery_purpose(purpose: str) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x03" * 16
    content = {
        "encoding": "base64",
        "data": base64.b64encode(png).decode("ascii"),
        "purpose": purpose,
    }

    _data, _ext, _ctype, metadata = serialize_artifact("screenshot", content)

    assert metadata == {"encoding": "binary", "purpose": purpose}


@pytest.mark.parametrize(
    "purpose",
    ("web_scrolled_viewport", "web_full_page_segment", "visual_navigation_viewport"),
)
def test_serialize_web_screenshot_keeps_internal_purpose(purpose: str) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x04" * 16
    content = {
        "encoding": "base64",
        "data": base64.b64encode(png).decode("ascii"),
        "purpose": purpose,
    }

    _data, _ext, _ctype, metadata = serialize_artifact("screenshot", content)

    assert metadata == {"encoding": "binary", "purpose": purpose}


@pytest.mark.parametrize("artifact_type", ("dom", "ui_snapshot", "har"))
def test_serialize_ad_json_artifact_keeps_recovery_purpose(artifact_type: str) -> None:
    content = {"purpose": "ad_landing", "entries": []}

    _data, _ext, _ctype, metadata = serialize_artifact(artifact_type, content)

    assert metadata == {"encoding": "utf-8", "purpose": "ad_landing"}


def test_segment_manifest_keeps_ordered_artifact_binding_in_storage_metadata() -> None:
    """丢弃清单绑定元数据会让 running phase 无法可靠恢复分段顺序。"""

    content = {
        "protocol_version": "web-full-page-segment-manifest.v1",
        "document_height": 2160,
        "viewport_height": 1080,
        "segment_count": 2,
        "segments": [
            {"index": 0, "top_css_px": 0, "bottom_css_px": 1080},
            {"index": 1, "top_css_px": 1080, "bottom_css_px": 2160},
        ],
        "associated_page_capture_id": "capture-1",
        "associated_screenshot_artifact_ids": ["segment-0", "segment-1"],
    }

    data, extension, content_type, metadata = serialize_artifact("page_coverage", content)

    assert json.loads(data) == content
    assert extension == "json"
    assert content_type == "application/json"
    assert metadata == {
        "encoding": "utf-8",
        "protocol_version": "web-full-page-segment-manifest.v1",
        "segment_count": 2,
        "associated_page_capture_id": "capture-1",
        "associated_screenshot_artifact_ids": ["segment-0", "segment-1"],
    }


def test_scrolled_viewport_manifest_keeps_single_screenshot_binding_metadata() -> None:
    content = {
        "protocol_version": "web-scrolled-viewport-manifest.v1",
        "capture_mode": "scroll_offset_viewport",
        "associated_page_capture_id": "capture-1",
        "associated_screenshot_artifact_ids": ["scrolled-0"],
    }

    data, extension, content_type, metadata = serialize_artifact("page_coverage", content)

    assert json.loads(data) == content
    assert extension == "json"
    assert content_type == "application/json"
    assert metadata == {
        "encoding": "utf-8",
        "protocol_version": "web-scrolled-viewport-manifest.v1",
        "segment_count": None,
        "capture_mode": "scroll_offset_viewport",
        "associated_page_capture_id": "capture-1",
        "associated_screenshot_artifact_ids": ["scrolled-0"],
    }


def test_serialize_json_artifact_restores_frozen_nested_values() -> None:
    """动作轨迹中的冻结对象必须落为 JSON 对象，不能降级成 Python repr。"""

    content = (
        MappingProxyType(
            {
                "action": "tap",
                "target": MappingProxyType({"element_id": "home-tab"}),
            }
        ),
    )

    data, extension, content_type, metadata = serialize_artifact("action_trace", content)

    assert json.loads(data) == [{"action": "tap", "target": {"element_id": "home-tab"}}]
    assert extension == "json"
    assert content_type == "application/json"
    assert metadata == {"encoding": "utf-8"}


@pytest.mark.asyncio
async def test_app_screenshot_artifact_persist_and_read(tmp_path: Path) -> None:
    store = LocalEvidenceArtifactStore(root=tmp_path / "store")
    svc = AppScreenshotArtifactService(store=store, index_root=tmp_path / "index")
    png = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
    ref = await svc.persist_png(
        task_id="task-a",
        page_capture_id="cap-1",
        png=png,
    )
    UUID(ref.artifact_id)  # 必須是正式 UUID
    assert ref.url == build_app_screenshot_artifact_url(ref.artifact_id)
    assert ref.checksum.startswith("sha256:")
    got_ref, content = await svc.read_png(ref.artifact_id)
    assert got_ref.artifact_id == ref.artifact_id
    assert content == png


class _FakeOps(AdbDeviceOps):
    def __init__(self) -> None:
        super().__init__(
            backend="fake",
            width=1080,
            height=1920,
            _log=lambda _m: None,
            _screencap_fn=self._screencap,
            _shell_fn=lambda _a, _t: None,
        )

    def _screencap(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 120)


@pytest.mark.asyncio
async def test_android_observe_registers_artifact_ref(tmp_path: Path) -> None:
    store = LocalEvidenceArtifactStore(root=tmp_path / "store")
    svc = AppScreenshotArtifactService(store=store, index_root=tmp_path / "index")
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path / "shots",
        ops=_FakeOps(),
        artifact_service=svc,
    )
    await start_mobile_demo_android_runtime(runtime)
    cap_id = await runtime.observe_to_file(tmp_path / "obs.png")
    assert runtime.last_artifact_ref is not None
    assert runtime.last_artifact_ref.page_capture_id == cap_id
    UUID(runtime.last_artifact_ref.artifact_id)
    assert runtime.last_artifact_ref.url.endswith(
        f"/artifacts/{runtime.last_artifact_ref.artifact_id}/file"
    )
    _ref, content = await svc.read_png(runtime.last_artifact_ref.artifact_id)
    assert content.startswith(b"\x89PNG")
