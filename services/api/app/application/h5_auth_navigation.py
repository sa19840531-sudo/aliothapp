"""H5 主动登录的页面读取与普通受审计导航适配。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.h5_auth_bootstrap import (
    H5AuthBootstrapCommand,
    H5AuthNavigationResult,
    H5AuthPageSnapshot,
)
from services.api.app.application.h5_auth_page_analyzer import H5AuthPageAnalyzer
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.application.interaction_runtime import TrustedUiSnapshotReader
from services.api.app.core.h5_auth_config import normalize_h5_auth_hostname
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


class DetectedH5AuthPageReader:
    """组合页面判定、受信 Snapshot 与认证分析器。"""

    def __init__(
        self,
        *,
        detector: Any,
        snapshot_reader: TrustedUiSnapshotReader,
        analyzer: H5AuthPageAnalyzer,
    ) -> None:
        self._detector = detector
        self._snapshot_reader = snapshot_reader
        self._analyzer = analyzer

    async def read(
        self,
        *,
        command: H5AuthBootstrapCommand,
        capture: CaptureRef,
    ) -> H5AuthPageSnapshot:
        detection: Mapping[str, Any] = await self._detector.detect(
            capture=capture,
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
        )
        snapshot = await self._snapshot_reader.read_ui_snapshot(
            task_id=command.task_id,
            page_capture_id=capture.capture_id,
        )
        page_url = str(detection.get("page_url") or "")
        raw_hostname = urlsplit(page_url).hostname
        hostname = normalize_h5_auth_hostname(raw_hostname) if raw_hostname else None
        return H5AuthPageSnapshot(
            capture=capture,
            hostname=hostname,
            observation=self._analyzer.analyze(
                detection_result=detection,
                ui_snapshot=snapshot,
            ),
        )


class CoordinatedH5AuthNavigation:
    """普通认证页面动作经 CaptureCoordinator 留存前后证据。"""

    def __init__(self, *, coordinator: CaptureCoordinator) -> None:
        self._coordinator = coordinator

    async def click(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        semantic_target: str,
        step_key: str,
        transition_parameters: Mapping[str, str | int] | None = None,
    ) -> H5AuthNavigationResult:
        return await self._coordinate(
            command=command,
            source_capture=source_capture,
            semantic_target=semantic_target,
            step_key=step_key,
            operation=InteractionOperation.CLICK,
            parameters={
                "navigation_depth": 1,
                **dict(transition_parameters or {}),
            },
            purpose="H5 主动登录状态机进入下一认证页面",
        )

    async def observe(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        step_key: str,
        transition_parameters: Mapping[str, str | int] | None = None,
    ) -> H5AuthNavigationResult:
        return await self._coordinate(
            command=command,
            source_capture=source_capture,
            semantic_target=None,
            step_key=step_key,
            operation=InteractionOperation.OBSERVE,
            parameters=dict(transition_parameters or {}),
            purpose="密码提交且字段清空后重新观察登录结果",
        )

    async def navigate_root(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        step_key: str,
    ) -> H5AuthNavigationResult:
        return await self._coordinate(
            command=command,
            source_capture=source_capture,
            semantic_target="H5 任务原始根页面",
            step_key=step_key,
            operation=InteractionOperation.NAVIGATE,
            parameters={"destination_url": command.target_url},
            purpose="认证阶段结束后返回任务原始 H5 URL",
        )

    async def _coordinate(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        semantic_target: str | None,
        step_key: str,
        operation: InteractionOperation,
        parameters: Mapping[str, Any],
        purpose: str,
    ) -> H5AuthNavigationResult:
        intent_id = f"h5-auth:{command.agent_run_id}:{step_key}"
        coordinated = await self._coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=command.agent_run_id,
                intent=ActionIntent(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    intent_id=intent_id,
                    idempotency_key=intent_id,
                    surface_kind=SurfaceKind.WEB,
                    source_capture_id=source_capture.capture_id,
                    action_type=operation,
                    semantic_target=semantic_target,
                    purpose=purpose,
                    capture_profile=_auth_capture_profile(),
                    deadline=command.deadline,
                    parameters={
                        **dict(parameters),
                        **h5_viewport_parameters(),
                    },
                    risk_level=InteractionRiskLevel.LOW,
                ),
            )
        )
        return H5AuthNavigationResult(
            capture=coordinated.after_capture,
            action_execution_id=coordinated.interaction.action_execution_id,
        )


def _auth_capture_profile() -> CaptureProfile:
    return CaptureProfile(
        profile_id="h5-auth-bootstrap-transition.v1",
        before=CaptureDirective(
            role="before_action",
            artifact_types=("screenshot", "dom", "visible_text", "ui_snapshot"),
        ),
        after=CaptureDirective(
            role="after_action",
            artifact_types=(
                "screenshot",
                "dom",
                "visible_text",
                "ui_snapshot",
                "action_trace",
            ),
        ),
    )


__all__ = ["CoordinatedH5AuthNavigation", "DetectedH5AuthPageReader"]
