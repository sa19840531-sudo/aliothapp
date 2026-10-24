"""工作台「已发布规则目录」接口（只读）。

部门规则中心的三个页签、创建任务 / 快速质检的规则集与页面范围选择器共用这一份真实口径：
- 规则集：启用中的规则集（只有开启的规则集会进入任务选择范围）
- 规则：启用规则集里的启用规则
- 页面类型：页面识别目录，附每页关联的已发布规则
- 页面范围选项：各平台提交任务时后端认可的页面类型（H5 / App 为业务页型，Web 为四类）
"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.page.web_page_catalog import WEB_PAGE_TYPE_CODES
from services.api.app.infrastructure.db.workstation_inspection_repository import (
    PostgresWorkstationInspectionRepository,
)

router = APIRouter(prefix="/rule-catalog")


class CatalogRuleset(BaseModel):
    ruleset_id: str
    ruleset_code: str
    version: int
    name: str
    description: str
    status: str
    enabled_rule_count: int
    rule_codes: list[str]


class CatalogRule(BaseModel):
    rule_id: str
    ruleset_id: str | None = None
    rule_code: str
    name: str
    category: str
    severity: str
    executor_type: str
    check_point: str
    applicable_page_types: list[str]
    source_type: str | None = None
    ruleset_code: str
    ruleset_version: int


class CatalogPageType(BaseModel):
    code: str
    name: str
    category: str
    rule_matchable: bool
    coverage_required: bool
    description: str
    related_rule_codes: list[str]


class PageScopeOptions(BaseModel):
    h5: list[str]
    web: list[str]
    app: list[str]


class RuleCatalogResponse(BaseModel):
    rulesets: list[CatalogRuleset]
    rules: list[CatalogRule]
    page_types: list[CatalogPageType]
    page_scope_options: PageScopeOptions


@lru_cache
def _get_inspection_repository() -> PostgresWorkstationInspectionRepository:
    return PostgresWorkstationInspectionRepository()


@router.get(
    "",
    response_model=RuleCatalogResponse,
    summary="查询已发布的规则集、规则、页面类型与页面范围",
)
async def get_rule_catalog(
    repository: Annotated[
        PostgresWorkstationInspectionRepository, Depends(_get_inspection_repository)
    ],
) -> RuleCatalogResponse:
    ruleset_rows = await repository.list_published_rulesets()
    rule_rows = await repository.list_published_rules()
    registry = get_default_page_type_registry()

    rules = [
        CatalogRule(
            rule_id=str(row["rule_id"]),
            ruleset_id=str(row["ruleset_id"]) if row.get("ruleset_id") is not None else None,
            rule_code=str(row["rule_code"]),
            name=str(row["name"]),
            category=str(row["category"] or ""),
            severity=str(row["severity"] or "medium"),
            executor_type=str(row["executor_type"] or ""),
            check_point=str(row["check_point"] or ""),
            applicable_page_types=list(row["applicable_page_types"]),
            source_type=str(row["source_type"]) if row.get("source_type") is not None else None,
            ruleset_code=str(row["ruleset_code"]),
            ruleset_version=int(row["ruleset_version"]),
        )
        for row in rule_rows
    ]
    rulesets = []
    for row in ruleset_rows:
        codes = [
            rule.rule_code
            for rule in rules
            if rule.ruleset_code == row["ruleset_code"]
            and rule.ruleset_version == row["version"]
        ]
        rulesets.append(
            CatalogRuleset(
                ruleset_id=str(row["ruleset_id"]),
                ruleset_code=str(row["ruleset_code"]),
                version=int(row["version"]),
                name=str(row["name"]),
                description=str(row["description"] or ""),
                status=str(row["status"]),
                enabled_rule_count=len(codes),
                rule_codes=codes,
            )
        )
    page_types = [
        CatalogPageType(
            code=page_type.code,
            name=page_type.name,
            category=page_type.category,
            rule_matchable=page_type.rule_matchable,
            coverage_required=page_type.coverage_required,
            description=page_type.purpose or "、".join(page_type.signals.layout_hints),
            related_rule_codes=list(
                dict.fromkeys(
                    rule.rule_code
                    for rule in rules
                    if page_type.code in rule.applicable_page_types
                )
            ),
        )
        for page_type in registry.catalog.page_types
    ]
    business_codes = list(registry.coverage_required_codes)
    return RuleCatalogResponse(
        rulesets=rulesets,
        rules=rules,
        page_types=page_types,
        page_scope_options=PageScopeOptions(
            h5=business_codes,
            web=list(WEB_PAGE_TYPE_CODES),
            app=business_codes,
        ),
    )
