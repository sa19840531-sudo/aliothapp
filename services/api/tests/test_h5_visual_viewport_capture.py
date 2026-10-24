"""H5 视觉导航专用视口帧的采集合同测试。"""

from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Any

import pytest

from services.api.app.infrastructure.browser import page_capture as page_capture_module
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum)
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", checksum)
    )


def _valid_grayscale_png(*, width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    scanline = b"\x00" + bytes(width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanline * height))
        + _png_chunk(b"IEND", b"")
    )


_DPR_2_VIEWPORT_PNG = _valid_grayscale_png(width=780, height=1688)


class _SparseFlutterPage:
    """只替代浏览器边界，Snapshot 构建和视觉门禁继续执行真实代码。"""

    url = "https://example.test/flutter"

    def __init__(
        self,
        *,
        profile: str = "sparse_flutter",
        dedicated_png: bytes = _DPR_2_VIEWPORT_PNG,
    ) -> None:
        self.profile = profile
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.screenshot_payloads = [b"existing-full-page", dedicated_png]

    async def evaluate(self, script: str) -> dict[str, Any]:
        self.calls.append(("evaluate", None))
        flutter_root_present = self.profile not in {"dom_spa", "suspected_flutter"}
        semantic_node_count = 12 if self.profile == "sufficient_flutter" else 1
        elements = (
            [
                {
                    "selector": f"flt-semantics#{index}",
                    "tag": "flt-semantics",
                    "role": "button",
                    "text": "首页视频排行帖子我的设置签到活动支付",
                    "interactive": True,
                    "visible": True,
                    "hit_test": {"center_hits_target": True},
                    "rect": {"x": index * 20, "y": 780, "width": 20, "height": 64},
                }
                for index in range(semantic_node_count)
            ]
            if self.profile != "dom_spa"
            else []
        )
        if self.profile in {"dom_spa", "sparse_hybrid"}:
            elements.append(
                {
                    "selector": "#dom-navigation",
                    "tag": "button",
                    "role": "button",
                    "text": "导航",
                    "interactive": True,
                    "visible": True,
                    "rect": {"x": 0, "y": 0, "width": 80, "height": 40},
                }
            )
        return {
            "schema_version": "ui-snapshot.v2",
            "url": self.url,
            "title": "Flutter 稀疏语义页",
            "viewport": {
                "width": 390,
                "height": 844,
                "device_pixel_ratio": 1,
                "scroll_x": 0,
                "scroll_y": 0,
                "visual_viewport_width": 390,
                "visual_viewport_height": 844,
                "visual_viewport_offset_left": 0,
                "visual_viewport_offset_top": 0,
                "visual_viewport_page_left": 0,
                "visual_viewport_page_top": 0,
                "visual_viewport_scale": 1,
                "flutter_host_transform": [1, 0, 0, 1, 0, 0],
            },
            "document": {
                "time_origin_ms": 1000.0,
                "flutter_root_present": flutter_root_present,
                "history_length": 1,
                "history_api_observed": self.profile == "dom_spa",
                "spa_root_present": self.profile == "dom_spa",
            },
            "elements": elements,
        }

    async def screenshot(self, **options: Any) -> bytes:
        self.calls.append(("screenshot", dict(options)))
        return self.screenshot_payloads.pop(0)


async def _collect_with_visual_mode(
    _monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    profile: str = "sparse_flutter",
    dedicated_png: bytes = _DPR_2_VIEWPORT_PNG,
) -> tuple[list[Any], dict[str, Any], _SparseFlutterPage]:
    page = _SparseFlutterPage(profile=profile, dedicated_png=dedicated_png)
    artifacts = await page_capture_module._collect_artifacts(
        page,
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            capture_dom=False,
            capture_visible_text=False,
        ),
        logical_surface_enabled=mode != "off",
        h5_visual_coordinate_mode=mode,
    )
    ui_snapshot = next(
        item.content for item in artifacts if item.artifact_type == "ui_snapshot"
    )
    return artifacts, ui_snapshot, page


async def test_visual_mode_off_does_not_capture_extra_viewport_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, ui_snapshot, page = await _collect_with_visual_mode(monkeypatch, "off")

    assert [item.artifact_type for item in artifacts].count("screenshot") == 1
    assert "visual_navigation_capture" not in ui_snapshot
    assert page.calls == [
        ("screenshot", {"full_page": True}),
        ("evaluate", None),
    ]


@pytest.mark.parametrize(
    ("mode", "profile"),
    [
        ("shadow", "sparse_flutter"),
        ("shadow", "sparse_hybrid"),
        ("enforced", "sparse_flutter"),
        ("enforced", "sparse_hybrid"),
    ],
)
async def test_visual_mode_captures_dedicated_viewport_png(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    profile: str,
) -> None:
    artifacts, ui_snapshot, page = await _collect_with_visual_mode(
        monkeypatch,
        mode,
        profile=profile,
    )

    visual = ui_snapshot["visual_navigation_capture"]
    assert [item.artifact_type for item in artifacts].count("screenshot") == 2
    assert visual["protocol_version"] == "visual_navigation_capture.v1"
    assert visual["screenshot_index"] == 2
    assert visual["image_width"] == 780
    assert visual["image_height"] == 1688
    assert ui_snapshot["viewport"]["visual_viewport_width"] == 390
    assert ui_snapshot["viewport"]["visual_viewport_height"] == 844
    assert visual["coordinate_space"] == "visual_viewport_css_pixels"
    assert visual["purpose"] == "visual_navigation_viewport"
    assert visual["screenshot_sha256"] == "sha256:" + hashlib.sha256(
        _DPR_2_VIEWPORT_PNG
    ).hexdigest()
    assert page.calls == [
        ("evaluate", None),
        ("screenshot", {"full_page": True}),
        ("screenshot", {"full_page": False, "type": "png"}),
    ]


async def test_visual_mode_rejects_non_png_dedicated_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="PNG"):
        await _collect_with_visual_mode(
            monkeypatch,
            "shadow",
            dedicated_png=b"not-a-png",
        )


@pytest.mark.parametrize(
    "profile",
    ["dom_spa", "sufficient_flutter", "suspected_flutter"],
)
async def test_visual_mode_skips_frame_without_sparse_confirmed_flutter_gate(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    artifacts, ui_snapshot, page = await _collect_with_visual_mode(
        monkeypatch,
        "shadow",
        profile=profile,
    )

    assert [item.artifact_type for item in artifacts].count("screenshot") == 1
    assert "visual_navigation_capture" not in ui_snapshot
    assert page.calls == [
        ("evaluate", None),
        ("screenshot", {"full_page": True}),
    ]
