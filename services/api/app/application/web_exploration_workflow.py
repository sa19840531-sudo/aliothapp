"""Web V2 独立会话启动与受信任首跳编排。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationResult,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.surface_sessions import SurfaceSessionRepository
from services.api.app.application.web_browser_profile import WebBrowserProfileRegistry
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceDriver,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.domain.protocol import require_aware_datetime

WEB_SURFACE_SESSION_OWNER = "web-v2-workflow"


class WebCaptureStage(StrEnum):
    """Web 页面从原始落地到可探索基线的采集阶段。"""

    ENTRY_PROBE = "web_entry_probe"
    PAGE_PROBE = "web_page_probe"
    PAGE_READY_BASELINE = "web_page_ready_baseline"


def web_capture_profile(profile_id: str) -> CaptureProfile:
    """返回 Web V2 状态转换统一使用的双采证据配置。"""

    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id 不能为空")
    return CaptureProfile(
        profile_id=profile_id,
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
                "har",
                "media_state",
            ),
        ),
    )


@dataclass(frozen=True, kw_only=True)
class RunWebWorkflowCommand:
    """Web V2 首跳所需的锁定任务与 Browser Profile。"""

    task_id: str
    agent_run_id: str
    session_id: str
    session_owner: str
    target_url: str
    browser_profile_id: str
    deadline: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "session_id",
            "session_owner",
            "target_url",
            "browser_profile_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if not self.target_url.startswith(("http://", "https://")):
            raise ValueError("target_url 必须为 HTTP(S) URL")
        require_aware_datetime(self.deadline, "deadline")


class WebSurfaceBootstrapper:
    """创建桌面 Web Session，并在双采边界内执行锁定入口导航。"""

    def __init__(
        self,
        *,
        driver: SurfaceDriver,
        surface_sessions: SurfaceSessionRepository,
        capture_coordinator: CaptureCoordinator,
        profile_registry: WebBrowserProfileRegistry | None = None,
    ) -> None:
        self._driver = driver
        self._surface_sessions = surface_sessions
        self._capture_coordinator = capture_coordinator
        self._profile_registry = profile_registry or WebBrowserProfileRegistry()

    @staticmethod
    def capture_profile() -> CaptureProfile:
        return web_capture_profile("web-v2-state-transition.v1")

    async def open_session(self, command: RunWebWorkflowCommand) -> None:
        """启动并注册 active Web Session，但不发首页动作。"""

        if not isinstance(command, RunWebWorkflowCommand):
            raise TypeError("command 必须为 RunWebWorkflowCommand")
        profile = self._profile_registry.resolve(command.browser_profile_id)
        browser_parameters = profile.to_action_parameters()
        spec = SurfaceSessionSpec(
            task_id=command.task_id,
            session_id=command.session_id,
            surface_kind=SurfaceKind.WEB,
            options={
                "target_url": command.target_url,
                **browser_parameters,
            },
        )
        session = await self._driver.start_session(spec)
        try:
            await self._surface_sessions.register_active(
                spec=spec,
                session_owner=command.session_owner,
                primary_surface_ref=session.primary_surface_ref,
            )
        except BaseException:
            await self._driver.close_session(
                session=session,
                reason="surface_session_registration_failed",
            )
            raise

    async def navigate_home(
        self,
        command: RunWebWorkflowCommand,
    ) -> CaptureCoordinationResult:
        """仅在 Session 已注册且调用方完成账本 bootstrap 后发首页动作。"""

        if not isinstance(command, RunWebWorkflowCommand):
            raise TypeError("command 必须为 RunWebWorkflowCommand")
        profile = self._profile_registry.resolve(command.browser_profile_id)
        browser_parameters = profile.to_action_parameters()
        intent_id = f"web-bootstrap:{command.agent_run_id}"
        intent = ActionIntent(
            task_id=command.task_id,
            session_id=command.session_id,
            intent_id=intent_id,
            idempotency_key=intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=f"web-entry:{command.agent_run_id}",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="Web 任务锁定入口",
            purpose="打开任务提交时锁定的桌面 Web 目标 URL",
            capture_profile=self.capture_profile(),
            deadline=command.deadline,
            parameters={
                "destination_url": command.target_url,
                "initial_surface_action": True,
                "web_capture_stage": WebCaptureStage.ENTRY_PROBE.value,
                "clear_obstructions": False,
                **browser_parameters,
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        return await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=command.agent_run_id,
                intent=intent,
            )
        )

    async def start(self, command: RunWebWorkflowCommand) -> CaptureCoordinationResult:
        """兼容入口；新 Runtime 使用两个公开阶段插入账本 bootstrap。"""

        await self.open_session(command)
        return await self.navigate_home(command)


__all__ = [
    "RunWebWorkflowCommand",
    "WEB_SURFACE_SESSION_OWNER",
    "WebCaptureStage",
    "WebSurfaceBootstrapper",
    "web_capture_profile",
]
