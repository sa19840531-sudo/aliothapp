"""Workflow 路由到 Skill Provider 子目录的确定性映射。"""

from __future__ import annotations

from services.api.app.application.app_workflow_router import WorkflowRoute

_GREEN_RULE_SKILL_SUB_DIRS = (
    "green-content-safety",
    "green-process-evidence",
)
_H5_SKILL_SUB_DIRS = (
    "qc-workflow",
    "phase",
    "rule",
    "navigation-boundary-skill",
    *_GREEN_RULE_SKILL_SUB_DIRS,
)
# F12：App 只挂候选决策一致的只读技能；不整挂 phase（旧探索/识别/报告）
# 与 legacy app-script-rhythm（直接节奏/脚本指导）。
_APP_SKILL_SUB_DIRS = (
    "app-workflow",
    "app-product-capability",  # F04 只读能力事实；禁止坐标授予
    *_GREEN_RULE_SKILL_SUB_DIRS,
)


def skill_sub_dirs_for_route(route: WorkflowRoute | str) -> tuple[str, ...]:
    """返回指定 Workflow 允许挂载的 Skill 目录，拒绝未知路由。"""
    if route is WorkflowRoute.H5 or route == WorkflowRoute.H5.value:
        return _H5_SKILL_SUB_DIRS
    if route is WorkflowRoute.APP or route == WorkflowRoute.APP.value:
        return _APP_SKILL_SUB_DIRS
    raise ValueError(f"不支持的 WorkflowRoute: {route!r}")
