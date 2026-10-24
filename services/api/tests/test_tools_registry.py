"""测试 Tool 注册表：装饰器登记、重复注册报错、按 category 过滤、导出工具列表。"""

import pytest

from services.api.app.agents.tools.registry import PermissionLevel, ToolRegistry, tool


def test_tool_decorator_registers_metadata_and_returns_function_tool() -> None:
    registry = ToolRegistry()

    @tool(name="sample-tool", description="示例", category="测试", registry=registry)
    def sample() -> str:
        """示例函数。"""
        return "ok"

    metadata = registry.get("sample-tool")
    assert metadata is not None
    assert metadata.description == "示例"
    assert metadata.category == "测试"
    assert metadata.permission_level == PermissionLevel.SAFE
    assert metadata.tool.name == "sample-tool"


def test_duplicate_registration_raises_value_error() -> None:
    registry = ToolRegistry()

    @tool(name="dup", description="示例", category="测试", registry=registry)
    def first() -> str:
        return "first"

    with pytest.raises(ValueError, match="dup"):

        @tool(name="dup", description="示例2", category="测试", registry=registry)
        def second() -> str:
            return "second"


def test_list_by_category_filters_correctly() -> None:
    registry = ToolRegistry()

    @tool(name="a", description="d", category="cat1", registry=registry)
    def a() -> str:
        return "a"

    @tool(name="b", description="d", category="cat2", registry=registry)
    def b() -> str:
        return "b"

    assert [m.name for m in registry.list_by_category("cat1")] == ["a"]
    assert [m.name for m in registry.list_by_category("cat2")] == ["b"]


def test_list_tools_returns_all_registered_function_tools() -> None:
    registry = ToolRegistry()

    @tool(name="a", description="d", category="cat", registry=registry)
    def a() -> str:
        return "a"

    @tool(name="b", description="d", category="cat", registry=registry)
    def b() -> str:
        return "b"

    tool_names = {t.name for t in registry.list_tools()}
    assert tool_names == {"a", "b"}


def test_sensitive_permission_level_is_recorded() -> None:
    registry = ToolRegistry()

    @tool(
        name="risky",
        description="高风险示例",
        category="测试",
        permission_level=PermissionLevel.SENSITIVE,
        registry=registry,
    )
    def risky() -> str:
        return "risky"

    metadata = registry.get("risky")
    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SENSITIVE
