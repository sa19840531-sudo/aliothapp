"""生产 Worker 的 App Workflow 路由与 fail-closed 处理边界。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from services.api.app.agents.runtime.models import RunResult
from services.api.app.application.app_v2_runtime import AppV2Runtime
from services.api.app.application.app_workflow_router import (
    AppWorkflowRouter,
    WorkflowRoute,
    WorkflowRoutingError,
)
from services.api.app.application.web_workflow_dispatch import (
    WebWorkflowHandler,
    WebWorkflowNotConfiguredError,
)
from services.api.app.core.config import Settings
from services.api.app.domain.task import (
    AdultSelfAttestation,
    AppTaskConfig,
    TaskTarget,
    TaskTargetPlatform,
    WebTaskConfig,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


class AppWorkflowNotConfiguredError(RuntimeError):
    """App Run 没有显式注入 App Workflow 处理器。"""


class H5WorkflowNotConfiguredError(RuntimeError):
    """H5 V2 Run 没有显式注入 H5 Workflow 处理器。"""


class AppWorkflowHandler(Protocol):
    """App Worker 对 Runtime 的唯一编排入口。"""

    async def run(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult: ...


class AppRuntimeProvider(Protocol):
    """生产组合根提供已完成依赖注入的 AppV2Runtime。"""

    async def build(self, *, record: AgentRunRecord, settings: Settings) -> AppV2Runtime: ...


class AppV2RuntimeExecutor(Protocol):
    """把已组合 Runtime 编排为一次 App Run 的执行端口。"""

    async def run(
        self,
        runtime: AppV2Runtime,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult: ...


class H5WorkflowHandler(Protocol):
    """H5 V2 Worker 对 Runtime 的唯一编排入口。"""

    async def run(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult: ...


class H5RuntimeProvider(Protocol):
    """生产组合根提供已完成依赖注入的 H5 V2 Runtime。"""

    async def build(self, *, record: AgentRunRecord, settings: Settings) -> object: ...


class H5V2RuntimeExecutor(Protocol):
    """把已组合 Runtime 编排为一次 H5 V2 Run 的执行端口。"""

    async def run(
        self,
        runtime: object,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult: ...


class RuntimeBackedAppWorkflowHandler:
    """通过显式 Provider/Executor 接入 AppV2Runtime，不接触通用 Main Agent。"""

    def __init__(
        self,
        *,
        provider: AppRuntimeProvider,
        executor: AppV2RuntimeExecutor,
    ) -> None:
        self._provider = provider
        self._executor = executor

    async def run(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        runtime = await self._provider.build(record=record, settings=settings)
        return await self._executor.run(
            runtime,
            record=record,
            settings=settings,
            progress_recorder=progress_recorder,
        )


class RuntimeBackedH5WorkflowHandler:
    """通过显式 Provider/Executor 接入 H5 V2，不接触通用 Main Agent。"""

    def __init__(
        self,
        *,
        provider: H5RuntimeProvider,
        executor: H5V2RuntimeExecutor,
    ) -> None:
        self._provider = provider
        self._executor = executor

    async def run(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        runtime = await self._provider.build(record=record, settings=settings)
        return await self._executor.run(
            runtime,
            record=record,
            settings=settings,
            progress_recorder=progress_recorder,
        )


class AppWorkflowDispatcher:
    """先路由任务，再把 App Run 交给显式 Handler，绝不回退 H5 Agent。"""

    def __init__(
        self,
        *,
        router: AppWorkflowRouter,
        handler: AppWorkflowHandler | None,
        h5_handler: H5WorkflowHandler | None = None,
        web_handler: WebWorkflowHandler | None = None,
    ) -> None:
        self._router = router
        self._handler = handler
        self._h5_handler = h5_handler
        self._web_handler = web_handler

    def route(self, record: AgentRunRecord, settings: Settings) -> WorkflowRoute:
        """从已锁定任务目标计算 Workflow，不读取 URL 或文件名猜测。"""
        del settings
        task_config = record.task_config or {}
        target_payload = task_config.get("_task_target")
        if target_payload is None:
            if task_config.get("legacy_h5_compatible") is not True:
                raise WorkflowRoutingError(
                    "Run 缺少锁定的 _task_target，且未声明 legacy_h5_compatible"
                )
            # 只有提交侧显式标记的历史 H5 Run 才允许进入旧入口。
            target = TaskTarget(
                platform=TaskTargetPlatform.H5,
                target_url="http://legacy-task.invalid",
            )
        else:
            target = task_target_from_dict(target_payload)
        workflow_version = str(task_config.get("workflow_version") or "v1")
        return self._router.route(target=target, workflow_version=workflow_version)

    def require_handler(self) -> AppWorkflowHandler:
        """返回 App Handler；未配置时显式失败，禁止隐式降级。"""
        if self._handler is None:
            raise AppWorkflowNotConfiguredError(
                "App Workflow handler 未配置，禁止回退到通用 H5 Main Agent"
            )
        return self._handler

    def require_h5_handler(self) -> H5WorkflowHandler:
        """返回 H5 V2 Handler；缺失时显式失败，禁止回退 V1。"""
        if self._h5_handler is None:
            raise H5WorkflowNotConfiguredError(
                "H5 V2 Workflow handler 未配置，禁止回退通用 Main Agent"
            )
        return self._h5_handler

    def require_web_handler(self) -> WebWorkflowHandler:
        """返回 Web V2 Handler；缺失时显式失败，禁止回退其他平台。"""
        if self._web_handler is None:
            raise WebWorkflowNotConfiguredError(
                "WEB_RUNTIME_NOT_CONFIGURED: Web V2 Workflow handler 未配置"
            )
        return self._web_handler

    async def dispatch_web_v2(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        """执行专用 Web V2 Handler，并拒绝任何跨平台调用。"""
        route = self.route(record, settings)
        if route is not WorkflowRoute.WEB_V2:
            raise ValueError("dispatch_web_v2 只接受 WEB_V2 Workflow Run")
        return await self.require_web_handler().run(
            record=record,
            settings=settings,
            progress_recorder=progress_recorder,
        )

    async def dispatch_h5_v2(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        """执行专用 H5 V2 Handler，并验证本次路由仍锁定为 H5_V2。"""
        route = self.route(record, settings)
        if route is not WorkflowRoute.H5_V2:
            raise ValueError("dispatch_h5_v2 只接受 H5_V2 Workflow Run")
        return await self.require_h5_handler().run(
            record=record,
            settings=settings,
            progress_recorder=progress_recorder,
        )

    async def dispatch(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        """执行 App Handler；调用方应在此之前完成 Run 状态初始化。"""
        route = self.route(record, settings)
        if route is not WorkflowRoute.APP:
            raise ValueError("AppWorkflowDispatcher 只接受 App Workflow Run")
        return await self.require_handler().run(
            record=record,
            settings=settings,
            progress_recorder=progress_recorder,
        )


def task_target_from_dict(payload: object) -> TaskTarget:
    """把提交时锁定的目标快照还原为领域对象。"""
    if not isinstance(payload, Mapping):
        raise ValueError("_task_target 必须是对象")
    try:
        platform = TaskTargetPlatform(str(payload["platform"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("_task_target.platform 不受支持") from exc
    if platform is TaskTargetPlatform.WEB:
        web_payload = payload.get("web")
        if not isinstance(web_payload, Mapping):
            raise ValueError("Web _task_target.web 必须是对象")
        attestation_payload = web_payload.get("adult_self_attestation")
        attestation = (
            None
            if attestation_payload is None
            else AdultSelfAttestation.from_dict(attestation_payload)
        )
        return TaskTarget(
            platform=TaskTargetPlatform.WEB,
            target_url=payload.get("target_url"),
            web=WebTaskConfig(
                browser_profile_id=str(web_payload.get("browser_profile_id") or ""),
                adult_self_attestation=attestation,
            ),
        )
    if platform is not TaskTargetPlatform.APP:
        return TaskTarget(
            platform=platform,
            target_url=payload.get("target_url"),
        )

    app_payload = payload.get("app")
    if not isinstance(app_payload, Mapping):
        raise ValueError("App _task_target.app 必须是对象")
    device_payload = app_payload.get("device")
    if not isinstance(device_payload, Mapping):
        raise ValueError("App _task_target.app.device 必须是对象")
    return TaskTarget(
        platform=TaskTargetPlatform.APP,
        app=AppTaskConfig(
            platform_build_ref=str(app_payload["platform_build_ref"]),
            package_name=(
                str(app_payload["package_name"])
                if app_payload.get("package_name") is not None
                else None
            ),
            device_backend=str(device_payload.get("backend", "cloud_phone")),
            device_profile_id=str(device_payload.get("profile_id", "default")),
        ),
    )
