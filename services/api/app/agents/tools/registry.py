"""Tool 注册表：进程内、装饰器驱动的 Tool（function calling 工具）元数据登记与查询。

命名说明：本模块登记的是 Agent Framework function calling 意义上的 **Tool**
（``agent_framework.tool`` 包装出的 ``FunctionTool``，Agent 调用后产生副作用或
返回结构化数据），与遵循 Agent Skills 规范（``agentskills.io``，SKILL.md、渐进式
披露）的 **Skill** 是两个不同的概念——本仓库对应目录是
[../skills/](../skills/README.md)，两者的区别见 [../README.md](../README.md)。

V1 范围内 Tool 注册表只在内存中生效（进程重启即重置），不落库，也不做跨进程共享；
后续如需要多进程/多副本部署下的统一 Tool 目录，再引入 domain/integration 的持久化
模型（明确不在本轮范围内，见
[../../domain/integration/README.md](../../domain/integration/README.md)）。
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_framework import FunctionTool
from agent_framework import tool as _af_tool


class PermissionLevel(StrEnum):
    """Tool 的风险分级，供 PermissionEnforcementMiddleware 与后续审批策略参考。"""

    SAFE = "safe"
    """无副作用或副作用可忽略（只读查询、纯计算），默认允许任意运行上下文调用。"""

    SENSITIVE = "sensitive"
    """有实质性副作用（写入结论、支付/登录/下载等高风险操作），必须在运行上下文的
    允许清单中显式声明才能调用，否则会被 ``PermissionEnforcementMiddleware`` 软阻断
    （拦截该次调用但不终止整个 Agent 循环，详见 permissions.py 模块文档）。"""


@dataclass(frozen=True)
class ToolMetadata:
    """一个已注册 Tool 的元数据快照。"""

    name: str
    description: str
    category: str
    permission_level: PermissionLevel
    tool: FunctionTool


class ToolRegistry:
    """进程内 Tool 注册表：登记 Tool 元数据，并可导出 Agent 可用的工具列表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolMetadata] = {}

    def register(self, metadata: ToolMetadata) -> None:
        """登记一个 Tool；同名重复注册视为配置错误，直接抛出异常。"""
        if metadata.name in self._tools:
            raise ValueError(f"Tool 重复注册：{metadata.name}")
        self._tools[metadata.name] = metadata

    def get(self, name: str) -> ToolMetadata | None:
        """按名称查询 Tool 元数据；找不到返回 None。"""
        return self._tools.get(name)

    def list_all(self) -> list[ToolMetadata]:
        """返回全部已注册 Tool 的元数据列表。"""
        return list(self._tools.values())

    def list_by_category(self, category: str) -> list[ToolMetadata]:
        """按 category 过滤已注册 Tool，对应 Skill 与规则分类的取数场景。"""
        return [item for item in self._tools.values() if item.category == category]

    def list_tools(
        self,
        *,
        exclude_names: frozenset[str] | set[str] | None = None,
        exclude_categories: frozenset[str] | set[str] | None = None,
        exclude_permission_levels: frozenset[PermissionLevel]
        | set[PermissionLevel]
        | None = None,
    ) -> list[FunctionTool]:
        """返回已注册 Tool；可按名称／分类／权限排除（Main 默认工具白名单用）。"""

        blocked_names = exclude_names or set()
        blocked_categories = exclude_categories or set()
        blocked_levels = exclude_permission_levels or set()
        return [
            item.tool
            for item in self._tools.values()
            if item.name not in blocked_names
            and item.category not in blocked_categories
            and item.permission_level not in blocked_levels
        ]

    def list_tool_metadata(self) -> list[ToolMetadata]:
        """返回全部 Tool 元数据，供边界测试核对挂载集合。"""

        return list(self._tools.values())


default_registry = ToolRegistry()
"""进程级默认注册表单例；绝大多数场景直接使用该实例即可，无需自行创建 ToolRegistry。"""


def tool(
    *,
    name: str,
    description: str,
    category: str,
    permission_level: PermissionLevel = PermissionLevel.SAFE,
    registry: ToolRegistry | None = None,
) -> Callable[[Callable[..., Any]], FunctionTool]:
    """将一个普通函数注册为 Tool：转换为 Agent Framework 的 ``FunctionTool``，
    同时在 ``ToolRegistry`` 里登记 ``permission_level`` 等元数据。

    用法::

        @tool(name="get_current_time", description="获取当前时间", category="占位示例")
        def get_current_time() -> str:
            \"\"\"获取当前时间。\"\"\"
            ...
    """

    def decorator(func: Callable[..., Any]) -> FunctionTool:
        wrapped = _af_tool(func, name=name, description=description)
        target_registry = registry if registry is not None else default_registry
        target_registry.register(
            ToolMetadata(
                name=name,
                description=description,
                category=category,
                permission_level=permission_level,
                tool=wrapped,
            )
        )
        return wrapped

    return decorator
