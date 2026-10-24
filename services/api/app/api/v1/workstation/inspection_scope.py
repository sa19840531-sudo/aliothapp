"""工作台执行范围预览接口（只读）。

为创建任务页的「执行范围预览」提供真实口径：
- 页面访问路径：本规则集覆盖的页面类型，按 Catalog 顺序（入口页优先）
- 质检项清单：本规则集内启用的规则
- 任务规模统计：访问页面数、质检规则数、预计耗时
"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.db.workstation_inspection_repository import (
    PostgresWorkstationInspectionRepository,
)

router = APIRouter(prefix="/inspection-scope")


class PageVisitStep(BaseModel):
    code: str
    name: str


class InspectionRuleItem(BaseModel):
    rule_code: str
    name: str
    category: str
    severity: str


class InspectionScopeStats(BaseModel):
    page_count: int
    rule_count: int
    estimated_minutes_low: int
    estimated_minutes_high: int


class InspectionScopeResponse(BaseModel):
    # 为空 = 按全部已发布规则集预览
    ruleset_code: str | None
    version: int | None
    page_visit_path: list[PageVisitStep]
    rules: list[InspectionRuleItem]
    stats: InspectionScopeStats


@lru_cache
def _get_inspection_repository() -> PostgresWorkstationInspectionRepository:
    return PostgresWorkstationInspectionRepository()


@router.get("", response_model=InspectionScopeResponse, summary="查询执行范围预览")
async def get_inspection_scope(
    repository: Annotated[
        PostgresWorkstationInspectionRepository, Depends(_get_inspection_repository)
    ],
    ruleset_code: str | None = None,
    version: int | None = None,
) -> InspectionScopeResponse:
    rules = await repository.list_rules_in_scope(ruleset_code=ruleset_code, version=version)

    referenced_codes = {
        page_type for rule in rules for page_type in rule["applicable_page_types"]
    }
    catalog = get_default_page_type_registry().catalog
    # 按 Catalog 顺序（入口页在前）保留被规则覆盖的受支持页面
    page_visit_path = [
        PageVisitStep(code=page_type.code, name=page_type.name)
        for page_type in catalog.page_types
        if page_type.status == "supported" and page_type.code in referenced_codes
    ]

    page_count = len(page_visit_path)
    rule_count = len(rules)
    # 预计耗时：完整巡检以页面为主导成本，粗估 ~1-2 分钟/页并设保底
    estimated_low = max(10, page_count)
    estimated_high = max(20, page_count * 2)

    return InspectionScopeResponse(
        ruleset_code=ruleset_code,
        version=(version or 1) if ruleset_code else None,
        page_visit_path=page_visit_path,
        rules=[
            InspectionRuleItem(
                rule_code=rule["rule_code"],
                name=rule["name"],
                category=rule["category"],
                severity=rule["severity"],
            )
            for rule in rules
        ],
        stats=InspectionScopeStats(
            page_count=page_count,
            rule_count=rule_count,
            estimated_minutes_low=estimated_low,
            estimated_minutes_high=estimated_high,
        ),
    )
