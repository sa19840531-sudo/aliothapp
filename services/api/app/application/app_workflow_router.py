"""从持久化任务目标选择 H5、App 或 Web Workflow 的纯路由。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from services.api.app.domain.task import TaskTarget, TaskTargetPlatform


class WorkflowRoute(StrEnum):
    """当前阶段允许进入的业务 Workflow。"""

    H5 = "h5"
    H5_SHADOW = "h5_shadow"
    H5_V2 = "h5_v2"
    WEB_V2 = "web_v2"
    APP = "app"


class WorkflowRoutingError(ValueError):
    """任务目标、锁定版本或 Feature Flag 不允许进入任何 Workflow。"""


class AppWorkflowRouter:
    """只消费数据库锁定事实，不从 URL、文件名或 Demo 开关猜测平台。"""

    def __init__(
        self,
        *,
        app_qc_enabled: bool,
        h5_v2_mode: Literal["off", "shadow", "on"] = "off",
        web_v2_mode: Literal["off", "exploration_only", "on"] = "off",
    ) -> None:
        if not isinstance(app_qc_enabled, bool):
            raise TypeError("app_qc_enabled 必须为布尔值")
        if h5_v2_mode not in {"off", "shadow", "on"}:
            raise ValueError("h5_v2_mode 不受支持")
        if web_v2_mode not in {"off", "exploration_only", "on"}:
            raise ValueError("web_v2_mode 不受支持")
        self._app_qc_enabled = app_qc_enabled
        self._h5_v2_mode = h5_v2_mode
        self._web_v2_mode = web_v2_mode

    def route(
        self,
        *,
        target: TaskTarget,
        workflow_version: str,
    ) -> WorkflowRoute:
        if not isinstance(target, TaskTarget):
            raise TypeError("target 必须为持久化 TaskTarget")
        if (
            not isinstance(workflow_version, str)
            or not workflow_version.strip()
            or workflow_version != workflow_version.strip()
        ):
            raise WorkflowRoutingError("workflow_version 必须由任务配置锁定")
        if target.platform is TaskTargetPlatform.H5:
            if workflow_version == "v1":
                return WorkflowRoute.H5
            if workflow_version != "v2":
                raise WorkflowRoutingError("H5 任务的 workflow_version 只支持 v1 或 v2")
            return {
                "off": WorkflowRoute.H5,
                "shadow": WorkflowRoute.H5_SHADOW,
                "on": WorkflowRoute.H5_V2,
            }[self._h5_v2_mode]
        if target.platform is TaskTargetPlatform.WEB:
            if workflow_version != "v2":
                raise WorkflowRoutingError("Web 任务仅支持 workflow_version=v2")
            if self._web_v2_mode == "off":
                raise WorkflowRoutingError("Web V2 Workflow 未开启")
            return WorkflowRoute.WEB_V2
        if target.platform is not TaskTargetPlatform.APP:
            raise WorkflowRoutingError("target_platform 不受支持")
        if not self._app_qc_enabled:
            raise WorkflowRoutingError("App 质检未启用")
        if workflow_version != "v2":
            raise WorkflowRoutingError("App 任务必须锁定 workflow_version=v2")
        return WorkflowRoute.APP
