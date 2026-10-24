"""工作台配置中心·页面配置接口（只读）。

为配置中心「页面配置」页提供真实口径：
- 页面类型清单：来自页面 Catalog（识别特征 = ocr/dom/layout 信号）
- 每页绑定规则：规则集内启用规则按 applicable_page_types 精确包含匹配，
  与引擎 candidate_rules 的 SQL 语义一致（不含 global 等伪类型兜底）
- 统计：页面类型数、识别特征数、规则绑定数（含跨页复用）、规则覆盖页面数
"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.db.workstation_inspection_repository import (
    PostgresWorkstationInspectionRepository,
)

router = APIRouter(prefix="/page-config")


class BoundRule(BaseModel):
    rule_code: str
    name: str
    severity: str


class PageConfigItem(BaseModel):
    code: str
    name: str
    status: str
    description: str
    feature_count: int
    bound_rule_count: int
    bound_rule_codes: list[str]
    # 页面识别配置详情（2.1 原型「页面识别配置」字段，全部来自页面识别目录）
    category: str = ""
    coverage_required: bool = False
    rule_matchable: bool = True
    purpose: str = ""
    aliases: list[str] = []
    ocr_keywords: list[str] = []
    dom_hints: list[str] = []
    layout_hints: list[str] = []
    entry_hints: list[str] = []
    exit_hints: list[str] = []
    next_page_types: list[str] = []
    risk_actions: list[str] = []
    route_prefixes: list[str] = []
    navigation_aliases: list[str] = []
    default_detail_page_type: str | None = None
    source: str = ""
    remark: str = ""
    bound_rules: list[BoundRule] = []


class PageConfigStats(BaseModel):
    page_type_total: int
    supported_total: int
    feature_total: int
    enabled_rule_total: int
    rule_binding_total: int
    covered_page_total: int


class PageConfigResponse(BaseModel):
    catalog_version: str
    catalog_sha256: str
    # 为空 = 按全部已发布规则集统计
    ruleset_code: str | None
    ruleset_version: int | None
    stats: PageConfigStats
    pages: list[PageConfigItem]


@lru_cache
def _get_inspection_repository() -> PostgresWorkstationInspectionRepository:
    return PostgresWorkstationInspectionRepository()


@router.get("", response_model=PageConfigResponse, summary="查询页面配置总览")
async def get_page_config(
    repository: Annotated[
        PostgresWorkstationInspectionRepository, Depends(_get_inspection_repository)
    ],
    ruleset_code: str | None = None,
    version: int | None = None,
) -> PageConfigResponse:
    rules = await repository.list_rules_in_scope(ruleset_code=ruleset_code, version=version)
    registry = get_default_page_type_registry()
    catalog = registry.catalog

    pages: list[PageConfigItem] = []
    for page_type in catalog.page_types:
        signals = page_type.signals
        feature_count = (
            len(signals.ocr_keywords) + len(signals.dom_hints) + len(signals.layout_hints)
        )
        bound_rule_codes = [
            rule["rule_code"]
            for rule in rules
            if page_type.code in rule["applicable_page_types"]
        ]
        pages.append(
            PageConfigItem(
                code=page_type.code,
                name=page_type.name,
                status=page_type.status,
                description="、".join(signals.layout_hints),
                feature_count=feature_count,
                bound_rule_count=len(bound_rule_codes),
                bound_rule_codes=bound_rule_codes,
                category=page_type.category,
                coverage_required=page_type.coverage_required,
                rule_matchable=page_type.rule_matchable,
                purpose=page_type.purpose,
                aliases=list(page_type.aliases),
                ocr_keywords=list(signals.ocr_keywords),
                dom_hints=list(signals.dom_hints),
                layout_hints=list(signals.layout_hints),
                entry_hints=[
                    f"{hint.from_page_type} → {hint.action_intent}"
                    + (f"（{'、'.join(hint.target_keywords)}）" if hint.target_keywords else "")
                    for hint in page_type.entry_hints
                ],
                exit_hints=list(page_type.exit_hints),
                next_page_types=list(
                    dict.fromkeys(item.page_type for item in page_type.next_page_candidates)
                ),
                risk_actions=list(page_type.risk_actions),
                route_prefixes=list(page_type.route_prefixes),
                navigation_aliases=list(
                    dict.fromkeys(
                        term for alias in page_type.navigation_aliases for term in alias.terms
                    )
                ),
                default_detail_page_type=page_type.default_detail_page_type,
                source=page_type.source,
                remark=page_type.remark,
                bound_rules=[
                    BoundRule(
                        rule_code=rule["rule_code"],
                        name=str(rule.get("name") or rule["rule_code"]),
                        severity=str(rule.get("severity") or "medium"),
                    )
                    for rule in rules
                    if page_type.code in rule["applicable_page_types"]
                ],
            )
        )

    return PageConfigResponse(
        catalog_version=catalog.version,
        catalog_sha256=registry.content_sha256,
        ruleset_code=ruleset_code,
        ruleset_version=(version or 1) if ruleset_code else None,
        stats=PageConfigStats(
            page_type_total=len(pages),
            supported_total=sum(1 for page in pages if page.status == "supported"),
            feature_total=sum(page.feature_count for page in pages),
            enabled_rule_total=len(rules),
            rule_binding_total=sum(page.bound_rule_count for page in pages),
            covered_page_total=sum(1 for page in pages if page.bound_rule_count > 0),
        ),
        pages=pages,
    )
