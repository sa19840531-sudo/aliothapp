"""LLM 成本核算架构守卫测试。"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

_OPENAI_SDK_CLIENT_NAMES = {
    "AsyncAzureOpenAI",
    "AsyncOpenAI",
    "AzureOpenAI",
    "OpenAI",
}
_AGENT_FRAMEWORK_OPENAI_CLIENT_NAMES = {
    "OpenAIChatClient",
    "OpenAIChatCompletionClient",
}

_SDK_CLIENT_IMPORT_ALLOWLIST = {
    "infrastructure/app_vision/llm.py",
    "infrastructure/llm_gateway/client_factory.py",
}
_AGENT_CLIENT_ALLOWLIST = {
    "infrastructure/llm_gateway/client_factory.py",
}
_RAW_ASYNC_CLIENT_IMPORT_ALLOWLIST = {
    "application/app_script_mapping.py",
    "infrastructure/app_vision/llm.py",
}
_DIRECT_CHAT_COMPLETION_ALLOWLIST = {
    "application/app_script_mapping.py",
    "infrastructure/app_vision/llm.py",
}


def test_openai_sdk_clients_only_exist_in_metered_factories() -> None:
    violations: list[str] = []
    for path, tree in _iter_app_trees():
        imported = _imported_names_from(tree, module="openai")
        client_names = sorted(imported.intersection(_OPENAI_SDK_CLIENT_NAMES))
        if client_names and _relative_path(path) not in _SDK_CLIENT_IMPORT_ALLOWLIST:
            violations.append(f"{_relative_path(path)} imports {client_names}")

    assert not violations, (
        "OpenAI SDK Client 只能在统一计量工厂或已显式记录 usage 的视觉 Provider 中构建："
        + "; ".join(violations)
    )


def test_agent_framework_openai_clients_only_built_by_client_factory() -> None:
    violations: list[str] = []
    for path, tree in _iter_app_trees():
        imported = _imported_names_from(tree, module="agent_framework.openai")
        client_names = sorted(imported.intersection(_AGENT_FRAMEWORK_OPENAI_CLIENT_NAMES))
        if client_names and _relative_path(path) not in _AGENT_CLIENT_ALLOWLIST:
            violations.append(f"{_relative_path(path)} imports {client_names}")

    assert not violations, (
        "业务代码必须通过 build_chat_client 构建 MAF LLM Client，禁止直接使用 "
        "OpenAIChatClient/OpenAIChatCompletionClient："
        + "; ".join(violations)
    )


def test_build_chat_client_call_sites_declare_usage_source() -> None:
    violations: list[str] = []
    for path, tree in _iter_app_trees():
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            if _call_name(call.func) != "build_chat_client":
                continue
            if not any(keyword.arg == "usage_source" for keyword in call.keywords):
                violations.append(f"{_relative_path(path)}:{call.lineno}")

    assert not violations, (
        "所有 build_chat_client 调用点必须声明 usage_source，确保成本报表可追溯来源："
        + "; ".join(violations)
    )


def test_private_raw_openai_client_is_only_used_by_metered_direct_calls() -> None:
    violations: list[str] = []
    for path, tree in _iter_app_trees():
        imported = _imported_names_from(
            tree,
            module="services.api.app.infrastructure.llm_gateway.client_factory",
        )
        if (
            "_build_raw_async_client" in imported
            and _relative_path(path) not in _RAW_ASYNC_CLIENT_IMPORT_ALLOWLIST
        ):
            violations.append(_relative_path(path))

    assert not violations, (
        "_build_raw_async_client 是私有底层 SDK 工厂；新增调用必须先进入统一 "
        "build_chat_client，确需直调时必须纳入 allowlist 并显式记录 usage："
        + "; ".join(violations)
    )


def test_direct_chat_completion_calls_record_usage() -> None:
    violations: list[str] = []
    for path, tree in _iter_app_trees():
        direct_calls = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _call_name(call.func).endswith(".chat.completions.create")
        ]
        if not direct_calls:
            continue
        relative_path = _relative_path(path)
        if relative_path not in _DIRECT_CHAT_COMPLETION_ALLOWLIST:
            violations.append(f"{relative_path} has direct chat.completions.create")
            continue
        source = path.read_text(encoding="utf-8")
        metered_factory_calls = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _call_name(call.func).endswith("_build_raw_async_client")
            and any(keyword.arg == "usage_source" for keyword in call.keywords)
        ]
        if metered_factory_calls:
            continue
        if "record_current_openai_compatible_response_usage(" not in source:
            violations.append(f"{relative_path} does not record OpenAI-compatible usage")
            continue
        if "record_current_llm_attempt_failure(" not in source:
            violations.append(f"{relative_path} does not audit failed provider attempts")
            continue
        source_calls = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _call_name(call.func).endswith("record_current_openai_compatible_response_usage")
        ]
        if any(
            not any(keyword.arg == "source" for keyword in call.keywords)
            for call in source_calls
        ):
            violations.append(f"{relative_path} records usage without source")

    assert not violations, (
        "直接 OpenAI-compatible chat completion 调用必须显式调用 "
        "record_current_openai_compatible_response_usage，并审计失败供应商请求，"
        "否则 Run 成本会漏记："
        + "; ".join(violations)
    )


def _iter_app_trees() -> list[tuple[Path, ast.AST]]:
    trees: list[tuple[Path, ast.AST]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        trees.append((path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return trees


def _relative_path(path: Path) -> str:
    return path.relative_to(APP_ROOT).as_posix()


def _imported_names_from(tree: ast.AST, *, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""
