"""App Capture 完整状态不变量读取测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.api.app.application.app_capture_state_invariants import (
    AppCaptureStateInvariantReader,
)
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind

NOW = datetime(2026, 9, 8, 16, tzinfo=UTC)


def _capture(*, role: str = "observation") -> CaptureRef:
    return CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role=role,
        artifact_ids=("screenshot-1", "ui-tree-1"),
    )


class Captures:
    def __init__(self, capture: CaptureRef) -> None:
        self.capture = capture

    async def get_verified_capture(self, *, capture_id: str):
        assert capture_id == self.capture.capture_id
        return SimpleNamespace(
            capture=self.capture,
            package_name="com.example.app",
            activity="DetailActivity",
            image_signature="sha256:image",
        )

    async def get_verified_ui_tree(self, *, capture: CaptureRef):
        assert capture == self.capture
        return SimpleNamespace(xml_text="<hierarchy />")


async def test_reader_builds_snapshot_from_verified_capture_facts() -> None:
    capture = _capture()
    saved = SimpleNamespace(
        observed_at=NOW,
        analysis=SimpleNamespace(
            package_name="com.fallback.app",
            activity="FallbackActivity",
            visual_page_type="content_detail",
            page_state="stable",
            overlay_signature="none",
        ),
    )
    reader = AppCaptureStateInvariantReader(
        captures=Captures(capture),
        understanding=SimpleNamespace(),
        observations=SimpleNamespace(),
    )

    with (
        patch(
            "services.api.app.application.app_capture_state_invariants."
            "ensure_app_capture_visual_observation",
            new=AsyncMock(return_value=saved),
        ),
        patch(
            "services.api.app.application.app_capture_state_invariants."
            "project_ui_snapshot_from_hierarchy",
            return_value={
                "screen_id": "screen-detail",
                "structure_signature": "structure-1",
            },
        ),
    ):
        snapshot = await reader.read(capture=capture)

    assert snapshot.capture_id == capture.capture_id
    assert snapshot.captured_at == NOW
    assert snapshot.business["package_name"] == "com.example.app"
    assert snapshot.business["page_type"] == "content_detail"
    assert snapshot.structure["screen_id"] == "screen-detail"
    assert snapshot.structure["state_version"] == "sha256:image"


async def test_reader_rejects_wrong_required_role_before_storage_read() -> None:
    capture = _capture(role="observation")
    captures = Captures(capture)
    captures.get_verified_capture = AsyncMock()  # type: ignore[method-assign]
    reader = AppCaptureStateInvariantReader(
        captures=captures,
        understanding=SimpleNamespace(),
        observations=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="after_action"):
        await reader.read(capture=capture, required_role="after_action")

    captures.get_verified_capture.assert_not_awaited()
