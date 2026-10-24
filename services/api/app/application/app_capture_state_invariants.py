"""从已校验 App Capture 构造完整状态不变量。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from services.api.app.application.app_screenshot_exploration import (
    AppScreenshotUnderstandingPort,
    AppVisualObservationRepositoryPort,
    ensure_app_capture_visual_observation,
)
from services.api.app.application.app_state_invariants import (
    build_app_state_invariant_snapshot,
)
from services.api.app.application.app_surface_evidence import (
    project_ui_snapshot_from_hierarchy,
)
from services.api.app.domain.inspection import StateInvariantSnapshot
from services.api.app.domain.interaction import CaptureRef


class AppInvariantCaptureReader(Protocol):
    """完整不变量读取所需的 App Capture 端口。"""

    async def get_verified_capture(self, *, capture_id: str) -> Any | None: ...

    async def get_verified_ui_tree(self, *, capture: CaptureRef) -> Any | None: ...


class AppCaptureStateInvariantReader:
    """复核截图、视觉事实和 UI Tree 后生成实际状态快照。"""

    def __init__(
        self,
        *,
        captures: AppInvariantCaptureReader,
        understanding: AppScreenshotUnderstandingPort,
        observations: AppVisualObservationRepositoryPort,
    ) -> None:
        self._captures = captures
        self._understanding = understanding
        self._observations = observations

    async def read(
        self,
        *,
        capture: CaptureRef,
        required_role: str | None = None,
    ) -> StateInvariantSnapshot:
        if not isinstance(capture, CaptureRef):
            raise TypeError("capture 必须为 CaptureRef")
        if required_role is not None and capture.role != required_role:
            raise ValueError(f"状态快照只接受 {required_role} Capture")
        persisted = await self._captures.get_verified_capture(
            capture_id=capture.capture_id
        )
        if persisted is None or getattr(persisted, "capture", None) != capture:
            raise ValueError("App Screenshot Capture 不存在或校验失败")
        saved = await ensure_app_capture_visual_observation(
            capture=capture,
            understanding=self._understanding,
            observations=self._observations,
            mismatch_message="App 状态不变量与 Capture 视觉事实身份不一致",
        )
        observed_at = getattr(saved, "observed_at", None)
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("App 视觉事实缺少带时区的观察时间")
        ui_tree = await self._captures.get_verified_ui_tree(capture=capture)
        if ui_tree is None:
            raise ValueError("App Capture 缺少可验证 UI Tree")
        analysis = saved.analysis
        ui_snapshot = project_ui_snapshot_from_hierarchy(
            ui_tree.xml_text,
            package_name=persisted.package_name,
            activity=persisted.activity,
        )
        return build_app_state_invariant_snapshot(
            task_id=capture.task_id,
            capture_id=capture.capture_id,
            session_id=capture.session_id,
            surface_kind=capture.surface_kind,
            package_name=persisted.package_name or analysis.package_name,
            activity=persisted.activity or analysis.activity,
            page_type=analysis.visual_page_type,
            page_state=analysis.page_state,
            image_signature=persisted.image_signature,
            session_options={
                "overlay_signature": analysis.overlay_signature,
                "screen_id": ui_snapshot.get("screen_id"),
                "structure_signature": ui_snapshot.get("structure_signature"),
                "has_ui_tree": True,
            },
            captured_at=observed_at,
        )
