"""V2 Explorer Agent 的默认生产组装入口。"""

from __future__ import annotations

from functools import lru_cache

from services.api.app.agents.explorer.h5_instructions import (
    build_h5_explorer_system_instructions,
)
from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.agents.skills.loader import (
    build_app_workflow_context_provider,
    build_h5_explorer_context_provider,
    build_h5_shortflow_context_provider,
    h5_shortflow_skill_hash,
)
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.llm_gateway.client_factory import build_chat_client


@lru_cache
def build_default_explorer_agent() -> MafExplorerAgent:
    """构建 App V2 Explorer：通用 Workflow + 只读 Product 能力事实 Skill。"""
    workflow_skill_provider = build_app_workflow_context_provider()
    return MafExplorerAgent(
        client=build_chat_client(
            get_settings().llm,
            usage_source="app_explorer_agent",
            usage_operation="exploration_turn",
        ),
        context_providers=[workflow_skill_provider],
        # F04 闭环：组合根同时挂载 app-product-capability（禁止坐标授予）。
        required_workflow_skills=("app-workflow", "app-product-capability"),
        workflow_skill_loader=workflow_skill_provider,
    )


@lru_cache
def build_default_h5_explorer_agent() -> MafExplorerAgent:
    """构建只加载 H5 动态探索课纲的 MAF Explorer。"""
    workflow_skill_provider = build_h5_explorer_context_provider()
    return MafExplorerAgent(
        client=build_chat_client(
            get_settings().llm,
            usage_source="h5_explorer_agent",
            usage_operation="exploration_turn",
        ),
        context_providers=[workflow_skill_provider],
        required_workflow_skills=("h5-explorer-workflow",),
        system_instructions_builder=build_h5_explorer_system_instructions,
        workflow_skill_loader=workflow_skill_provider,
    )


@lru_cache
def build_h5_shortflow_agent(name: str, content_hash: str) -> MafExplorerAgent:
    """短流程复用现有 Explorer 执行合同，不新增 Driver 或模型调用端口。"""
    if h5_shortflow_skill_hash(name) != content_hash:
        raise ValueError("H5 短流程 Skill 内容漂移")
    provider = build_h5_shortflow_context_provider(name)
    return MafExplorerAgent(
        client=build_chat_client(get_settings().llm, usage_source="h5_shortflow_agent",
                                usage_operation=name),
        context_providers=[provider],
        required_workflow_skills=("h5-explorer-workflow", name),
        system_instructions_builder=build_h5_explorer_system_instructions,
        workflow_skill_loader=provider,
    )
