"""页面探索收口 Tool：显式关闭本次 Run 中不再执行的探索项。"""

from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.domain.page.registry import (
    PageTypeRegistry,
    get_default_page_type_registry,
)

_PAGE_TYPE_REGISTRY = get_default_page_type_registry()


def missing_coverage_page_types(
    submitted_page_types: Iterable[str],
    *,
    required_page_types: Iterable[str] | None = None,
    registry: PageTypeRegistry | None = None,
) -> list[str]:
    """按 Catalog 顺序返回指定范围中尚未提交评估的页面类型。"""
    resolved = registry or _PAGE_TYPE_REGISTRY
    submitted = set(submitted_page_types)
    required = set(
        required_page_types
        if required_page_types is not None
        else resolved.coverage_required_codes
    )
    return [
        code
        for code in resolved.coverage_required_codes
        if code in required and code not in submitted
    ]


class PageTypeAssessmentInput(BaseModel):
    """产品业务页面类型的结构化适用性评估。"""

    page_type: str = Field(min_length=1)
    status: Literal[
        "covered",
        "requires_auth",
        "blocked_by_safety",
        "not_found",
        "evidence_insufficient",
        "not_applicable",
    ]
    reason_code: str = Field(min_length=1)
    page_capture_ids: list[str] = Field(default_factory=list)
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    exploration_action_ids: list[str] = Field(default_factory=list)
    page_states: list[str] = Field(default_factory=list)


@tool(
    name="exploration_closure_tool",
    description=(
        "将受风险、抽样或运行预算限制而不再执行的探索动作标记为终态，并可关闭尚未开始"
        "规则匹配的页面；不得用于掩盖已开始但未完成的规则执行。"
    ),
    category="页面探索",
    permission_level=PermissionLevel.SAFE,
)
async def exploration_closure_tool(
    reason: Annotated[str, Field(description="关闭原因。", min_length=1)],
    action_status: Annotated[
        Literal["blocked", "skipped", "failed"],
        Field(description="待关闭探索动作的终态。"),
    ] = "skipped",
    exploration_action_ids: Annotated[
        list[str] | None,
        Field(description="需要关闭的探索动作 ID。"),
    ] = None,
    close_all_pending_actions: Annotated[
        bool,
        Field(description="是否关闭当前 Frontier 中全部待探索动作。"),
    ] = False,
    page_capture_ids: Annotated[
        list[str] | None,
        Field(description="需要标记为 abandoned 的未检查页面采集 ID。"),
    ] = None,
    abandon_all_pending_pages: Annotated[
        bool,
        Field(description="是否关闭全部尚未开始规则匹配的待处理页面。"),
    ] = False,
    page_type_assessments: Annotated[
        list[PageTypeAssessmentInput] | None,
        Field(
            max_length=len(_PAGE_TYPE_REGISTRY.coverage_required_codes),
            description="本产品锁定或动态发现业务页面类型的适用性评估；引用必须来自当前 Run。",
        ),
    ] = None,
    assessment_scope_page_types: Annotated[
        list[str] | None,
        Field(
            max_length=len(_PAGE_TYPE_REGISTRY.coverage_required_codes),
            description="本次评估的产品业务页面范围；不得自动填充 Catalog 全量页型。",
        ),
    ] = None,
    assessment_scope_complete: Annotated[
        bool,
        Field(description="是否已评估本产品锁定或动态发现的全部业务页面类型。"),
    ] = False,
) -> dict:
    """校验探索收口请求，实际状态变更由单次 Run 的工作记忆中间件完成。"""
    action_ids = list(dict.fromkeys(exploration_action_ids or []))
    capture_ids = list(dict.fromkeys(page_capture_ids or []))
    assessment_payloads = [
        PageTypeAssessmentInput.model_validate(item).model_dump(mode="json")
        for item in page_type_assessments or []
    ]
    submitted_page_types = {item["page_type"] for item in assessment_payloads}
    scope_page_types = list(dict.fromkeys(assessment_scope_page_types or submitted_page_types))
    invalid_scope_page_types = sorted(
        set(scope_page_types) - set(_PAGE_TYPE_REGISTRY.coverage_required_codes)
    )
    if invalid_scope_page_types:
        return {
            "execution_status": "blocked",
            "error_code": "PAGE_TYPE_ASSESSMENT_SCOPE_INVALID",
            "retryable": True,
            "message": "页面类型评估范围包含非业务页面类型。",
            "invalid_page_types": invalid_scope_page_types,
        }
    missing_page_types = missing_coverage_page_types(
        submitted_page_types,
        required_page_types=scope_page_types,
    )
    if assessment_scope_complete and missing_page_types:
        return {
            "execution_status": "blocked",
            "error_code": "PAGE_TYPE_ASSESSMENT_SCOPE_INCOMPLETE",
            "retryable": True,
            "message": "产品业务页面类型评估范围不完整。",
            "missing_page_types": missing_page_types,
        }
    if not (
        action_ids
        or close_all_pending_actions
        or capture_ids
        or abandon_all_pending_pages
        or assessment_payloads
    ):
        return {
            "execution_status": "blocked",
            "error_code": "EXPLORATION_CLOSURE_TARGET_REQUIRED",
            "retryable": True,
            "message": "必须指定待关闭的探索动作或页面。",
        }
    if (action_ids and close_all_pending_actions) or (capture_ids and abandon_all_pending_pages):
        return {
            "execution_status": "blocked",
            "error_code": "EXPLORATION_CLOSURE_TARGET_CONFLICT",
            "retryable": True,
            "message": "显式 ID 与对应的全部关闭开关不能同时使用。",
        }
    return {
        "execution_status": "accepted",
        "action_status": action_status,
        "reason": reason,
        "exploration_action_ids": action_ids,
        "close_all_pending_actions": close_all_pending_actions,
        "page_capture_ids": capture_ids,
        "abandon_all_pending_pages": abandon_all_pending_pages,
        "page_type_assessments": assessment_payloads,
        "assessment_scope_page_types": scope_page_types,
        "assessment_scope_complete": assessment_scope_complete,
    }
