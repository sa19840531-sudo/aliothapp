"""业务中间件公共能力：任务快照读取、参数解析与公共默认值。"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from services.api.app.agents.runtime.run_context import get_agent_run_context
from services.api.app.agents.tools.registry import PermissionLevel, ToolRegistry, default_registry
from services.api.app.application.task_query import TaskQueryService
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskRuntimeSnapshot:
    """业务中间件所需的任务最小快照。"""

    task_id: str
    status: str
    ruleset_version: int
    ruleset_id: str | None = None
    ruleset_snapshot_hash: str | None = None
    priority: int = 100
    target_platform: str | None = None
    inspection_scope: tuple[str, ...] = ()


class TaskRuntimeStateProvider(Protocol):
    """按 task_id 读取任务运行态快照。"""

    async def get_task_snapshot(self, task_id: str) -> TaskRuntimeSnapshot | None:
        """返回任务快照；不存在时返回 None。"""


@lru_cache
def _get_task_query_service() -> TaskQueryService:
    return TaskQueryService(repository=PostgresTaskRepository())


class DefaultTaskRuntimeStateProvider:
    """默认任务快照提供器：读取任务详情并投影为中间件所需字段。"""

    async def get_task_snapshot(self, task_id: str) -> TaskRuntimeSnapshot | None:
        detail = await _get_task_query_service().get_task_detail(task_id)
        if detail is None:
            return None
        return TaskRuntimeSnapshot(
            task_id=detail.task_id,
            status=detail.status,
            ruleset_version=detail.ruleset_version,
            ruleset_id=detail.ruleset_id,
            ruleset_snapshot_hash=detail.ruleset_snapshot_hash,
            priority=detail.priority,
            target_platform=detail.target_platform,
            inspection_scope=getattr(detail, "inspection_scope", ()),
        )


def extract_arguments(arguments: Any) -> dict[str, Any]:
    """尽力把工具参数归一化为 dict，失败时返回空字典。"""
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if hasattr(arguments, "model_dump"):
        try:
            dumped = arguments.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:  # noqa: BLE001 - 参数解析失败时回退为空字典，避免影响主链路
            return {}
    return {}


def current_task_id() -> str | None:
    """读取当前 Run 绑定的 task_id；无上下文时返回 None。"""
    context = get_agent_run_context()
    return context.task_id if context is not None else None


def default_mutating_tools(registry: ToolRegistry | None = None) -> set[str]:
    """从 Tool 注册表推导默认的变更类工具集合。"""
    target_registry = registry if registry is not None else default_registry
    return {
        item.name
        for item in target_registry.list_all()
        if item.permission_level == PermissionLevel.SENSITIVE
    }
