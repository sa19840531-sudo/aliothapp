"""Main Agent 的默认构建入口：供 API 调试端点与后台 Worker 进程共用，避免重复拼装逻辑。

两个入口——``services/api/app/api/v1/agents.py`` 的 HTTP 调试端点、
``services/api/app/worker.py`` 的后台 Worker——都需要构建"同一套默认配置"的
Main Agent（同一份 instructions、同一个 Tool 工具集、同一个 Skill Context
Provider）。本模块把这部分公共逻辑收敛到一处，避免两处各写一份、后续改配置漏改
其中一处。系统指令（身份、职责门禁、安全护栏等）的具体文案维护在
[instructions.py](instructions.py)，与本模块的构建/组装逻辑分离，保持单文件
职责单一，此处按原名重新导出以兼容既有引用。
"""

from functools import lru_cache

from agent_framework import Agent

from services.api.app.agents.capability_manifest import (
    CapabilityManifestEntry,
    build_capability_manifest,
)
from services.api.app.agents.instructions import MAIN_AGENT_INSTRUCTIONS
from services.api.app.agents.runtime.agent_factory import build_main_agent
from services.api.app.agents.skills.loader import build_skills_context_provider
from services.api.app.agents.tools import (
    app_device_tools,  # noqa: F401  # Android 设备试点 + Product Profile Tool
    assertion_tools,  # noqa: F401  # 触发确定性断言 Tool 登记
    exploration_tools,  # noqa: F401  # 触发页面探索收口 Tool 登记
    page_capture_tools,  # noqa: F401  # 触发页面采集 Tool 注册
    page_detection_tools,  # noqa: F401  # 触发多模态页面识别 Tool 注册
    qr_link_validation_tool,  # noqa: F401  # 触发二维码链接校验 Tool 登记
    report_tools,  # noqa: F401  # 触发报告生成 Tool 登记
    rule_judge_tools,  # noqa: F401  # 触发统一规则判定 Tool 登记
    rule_tools,  # noqa: F401  # 触发真实规则业务 Tool 登记
    sensitive_word_tools,  # noqa: F401  # 触发敏感词检测 Tool 注册
)
from services.api.app.agents.tools.registry import default_registry
from services.api.app.application.workflow_skill_routing import skill_sub_dirs_for_route
from services.api.app.core.config import AgentRuntimeSettings, get_settings
from services.api.app.infrastructure.llm_gateway.client_factory import build_chat_client

# F03：直接 ADB 试点 Tool 不得进入默认 Main（无 Task/租约/Coordinator）。
# 只读 Profile 摘要（SAFE）可保留；截图／tap／cloud_back 一律排除。
_MAIN_EXCLUDED_DIRECT_ADB_TOOLS = frozenset(
    {
        "app_screenshot_tool",
        "app_tap_tool",
        "app_cloud_back_tool",
        "app_cloud_back_twice_tool",
        "app_tap_from_profile_tool",
    }
)

__all__ = [
    "MAIN_AGENT_INSTRUCTIONS",
    "build_default_capability_manifest",
    "build_default_main_agent",
    "main_agent_tool_names",
]


def main_agent_tool_names() -> frozenset[str]:
    """当前默认 Main 实际挂载的 Tool 名（供边界回归）。"""

    return frozenset(
        item.name
        for item in default_registry.list_tool_metadata()
        if item.name not in _MAIN_EXCLUDED_DIRECT_ADB_TOOLS
    )


@lru_cache
def build_default_capability_manifest() -> tuple[CapabilityManifestEntry, ...]:
    """基于实际挂载的 Tool 与 Skill 构建默认能力快照。"""
    return build_capability_manifest(registry=default_registry)


def build_default_main_agent(
    *, runtime_settings: AgentRuntimeSettings | None = None
) -> Agent:
    """按运行配置构建 Main Agent；相同配置复用实例，不同任务配置隔离 client。"""
    effective_settings = runtime_settings or get_settings().agent_runtime
    return _build_cached_main_agent(effective_settings.model_dump_json())


@lru_cache(maxsize=32)
def _build_cached_main_agent(runtime_settings_json: str) -> Agent:
    return _build_main_agent(AgentRuntimeSettings.model_validate_json(runtime_settings_json))


def _build_main_agent(runtime_settings: AgentRuntimeSettings) -> Agent:
    build_default_capability_manifest()
    settings = get_settings()
    client = build_chat_client(
        settings.llm,
        usage_source="main_agent",
        usage_operation="agent_response",
    )
    return build_main_agent(
        client=client,
        instructions=MAIN_AGENT_INSTRUCTIONS,
        runtime_settings=runtime_settings,
        tools=default_registry.list_tools(exclude_names=_MAIN_EXCLUDED_DIRECT_ADB_TOOLS),
        context_providers=[
            build_skills_context_provider(
                sub_dirs=skill_sub_dirs_for_route("h5"),
            )
        ],
    )
