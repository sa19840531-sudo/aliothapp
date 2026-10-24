"""管理后台成本报表 API。"""

import csv
import io
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel

from services.api.app.application.cost_reports import (
    build_cost_report,
    build_usage_event_report,
)
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.db.cost_report_repository import (
    PostgresCostReportRepository,
)
from services.api.app.infrastructure.db.department_cost_repository import (
    PostgresDepartmentCostRepository,
)
from services.api.app.infrastructure.db.department_repository import PostgresDepartmentRepository
from services.api.app.infrastructure.db.llm_usage_event_repository import (
    PostgresLLMUsageEventRepository,
)

router = APIRouter(prefix="/cost-reports")


class CostBucketResponse(BaseModel):
    """成本报表聚合桶。"""

    key: str
    run_count: int
    ai_calls: int
    usage_missing_count: int
    total_tokens: int
    total_cost: float


class CostRunItemResponse(BaseModel):
    """单次 Agent Run 成本明细。"""

    run_id: str
    task_id: str | None
    task_name: str | None
    task_status: str | None
    agent_type: str
    agent_name: str
    status: str
    provider: str
    model: str
    currency: str
    ai_calls: int
    usage_missing_count: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    pricing_source: str
    pricing_configured: bool
    applied_pricing_tiers: list[str]
    pricing_breakdown: list[dict[str, object]]
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class CostTaskItemResponse(BaseModel):
    """按任务聚合后的成本明细。"""

    task_id: str | None
    task_name: str | None
    status: str
    currency: str
    run_count: int
    latest_run_id: str | None
    latest_run_status: str
    latest_run_created_at: datetime
    provider: str
    model: str
    ai_calls: int
    usage_missing_count: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    pricing_source: str
    applied_pricing_tiers: list[str]
    pricing_breakdown: list[dict[str, object]]


class CostUsageEventItemResponse(BaseModel):
    """单次 LLM 调用流水成本明细。"""

    event_id: str
    scope_type: str
    task_id: str | None
    task_name: str | None
    agent_run_id: str | None
    agent_type: str | None
    agent_name: str | None
    run_status: str | None
    source: str
    operation: str | None
    provider: str
    model: str
    api_style: str
    usage_available: bool
    ai_calls: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    currency: str
    pricing_source: str
    pricing_unit: str
    pricing_configured: bool
    pricing_tier: str | None
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float
    cache_hit_price_per_million_tokens: float
    calculation_formula: str
    created_at: datetime


class CostReportResponse(BaseModel):
    """LLM 成本报表响应。"""

    currency: str
    days: int
    generated_at: datetime
    run_count: int
    completed_run_count: int
    failed_run_count: int
    ai_calls: int
    usage_missing_count: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    average_run_cost: float
    average_call_cost: float
    by_day: list[CostBucketResponse]
    by_model: list[CostBucketResponse]
    by_status: list[CostBucketResponse]
    task_items: list[CostTaskItemResponse]
    recent_runs: list[CostRunItemResponse]


class CostUsageEventReportResponse(BaseModel):
    """全量 LLM 调用流水成本报表响应。"""

    currency: str
    days: int
    generated_at: datetime
    event_count: int
    task_event_count: int
    debug_event_count: int
    unbound_event_count: int
    ai_calls: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    usage_missing_count: int
    by_scope: list[CostBucketResponse]
    by_source: list[CostBucketResponse]
    by_model: list[CostBucketResponse]
    recent_events: list[CostUsageEventItemResponse]


@router.get("/llm-runs", response_model=CostReportResponse)
async def get_llm_run_cost_report(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
) -> CostReportResponse:
    """查询最近 N 天 Agent Run 的 LLM 成本报表。"""
    settings = get_settings()
    repository = PostgresCostReportRepository()
    rows = await repository.list_recent_runs(days=days)
    report = build_cost_report(
        rows,
        llm_settings=settings.llm,
        days=days,
        recent_run_limit=limit,
        generated_at=datetime.now(UTC),
    )
    return CostReportResponse.model_validate(asdict(report))


@router.get("/llm-events", response_model=CostUsageEventReportResponse)
async def get_llm_usage_event_report(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
) -> CostUsageEventReportResponse:
    """查询最近 N 天所有 LLM 调用流水的成本报表。"""
    settings = get_settings()
    repository = PostgresLLMUsageEventRepository()
    rows = await repository.list_recent_events(days=days)
    report = build_usage_event_report(
        rows,
        llm_settings=settings.llm,
        days=days,
        recent_event_limit=limit,
        generated_at=datetime.now(UTC),
    )
    return CostUsageEventReportResponse.model_validate(asdict(report))


# ---------------------------------------------------------------------------
# 部门 Token 消耗与费用（2.1 原型 token-usage.html）
# ---------------------------------------------------------------------------


class DepartmentCostRow(BaseModel):
    tenant_id: str
    tenant_name: str
    department_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    total_tokens: int = 0
    ai_calls: int = 0
    task_count: int = 0
    total_cost: float = 0.0
    main_provider: str | None = None
    main_model: str | None = None
    monthly_budget: float | None = None
    budget_used_percent: float | None = None
    budget_level: Literal["none", "normal", "warning", "critical", "over"] = "none"


class PriceVersion(BaseModel):
    input_price: float
    output_price: float
    cache_hit_price: float
    pricing_source: str
    pricing_configured: bool
    currency: str = "CNY"
    effective_from: datetime
    last_seen_at: datetime
    event_count: int = 0


class DepartmentCostTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    total_tokens: int = 0
    ai_calls: int = 0
    task_count: int = 0
    total_cost: float = 0.0
    unpriced_calls: int = 0
    currency: str = "CNY"


class DepartmentCostReportResponse(BaseModel):
    month: str
    month_label: str
    start: datetime
    end: datetime
    generated_at: datetime
    provider: str | None = None
    providers: list[str]
    months: list[str]
    totals: DepartmentCostTotals
    previous_totals: DepartmentCostTotals
    token_change_percent: float | None = None
    cost_change_percent: float | None = None
    alert_department_count: int = 0
    over_90_department_count: int = 0
    rows: list[DepartmentCostRow]
    current_price: PriceVersion | None = None
    price_history: list[PriceVersion]


class DepartmentCostTaskRow(BaseModel):
    task_id: str
    task_name: str
    department_name: str
    provider: str | None = None
    model: str | None = None
    ai_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    first_call_at: datetime | None = None
    last_call_at: datetime | None = None


class DepartmentCostDetailResponse(BaseModel):
    month: str
    tenant_id: str | None = None
    department_name: str | None = None
    items: list[DepartmentCostTaskRow]


@lru_cache
def _get_department_cost_repository() -> PostgresDepartmentCostRepository:
    return PostgresDepartmentCostRepository()


@lru_cache
def _get_department_repository() -> PostgresDepartmentRepository:
    return PostgresDepartmentRepository()


def _month_window(month: str | None, *, now: datetime | None = None) -> tuple[str, datetime, datetime, datetime, datetime]:
    """返回 (月份键, 本月起, 本月止, 上月起, 上月止)；month 形如 2026-09，缺省当月。"""
    current = now or datetime.now(UTC)
    if month:
        try:
            year, month_number = (int(part) for part in month.split("-", 1))
            start = datetime(year, month_number, 1, tzinfo=UTC)
        except ValueError as exc:
            raise ValueError("月份格式应为 YYYY-MM") from exc
    else:
        start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    prev_start = (start - timedelta(days=1)).replace(day=1)
    return start.strftime("%Y-%m"), start, end, prev_start, start


def _recent_month_keys(count: int = 12, *, now: datetime | None = None) -> list[str]:
    current = (now or datetime.now(UTC)).replace(day=1)
    keys: list[str] = []
    for _ in range(count):
        keys.append(current.strftime("%Y-%m"))
        current = (current - timedelta(days=1)).replace(day=1)
    return keys


def _change_percent(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _budget_level(percent: float | None) -> str:
    if percent is None:
        return "none"
    if percent >= 100:
        return "over"
    if percent >= 90:
        return "critical"
    if percent >= 80:
        return "warning"
    return "normal"


def _totals(row: dict[str, object]) -> DepartmentCostTotals:
    return DepartmentCostTotals(
        input_tokens=int(row.get("input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        cache_hit_tokens=int(row.get("cache_hit_tokens") or 0),
        total_tokens=int(row.get("total_tokens") or 0),
        ai_calls=int(row.get("ai_calls") or 0),
        task_count=int(row.get("task_count") or 0),
        total_cost=round(float(row.get("total_cost") or 0), 2),
        unpriced_calls=int(row.get("unpriced_calls") or 0),
        currency=str(row.get("currency") or "CNY"),
    )


async def build_department_cost_report(
    repository: PostgresDepartmentCostRepository,
    department_repository: PostgresDepartmentRepository,
    *,
    month: str | None,
    provider: str | None,
    tenant_id: str | None,
    now: datetime | None = None,
) -> DepartmentCostReportResponse:
    try:
        month_key, start, end, prev_start, prev_end = _month_window(month, now=now)
    except ValueError as exc:
        raise AliothError(message=str(exc)) from exc
    totals_row = await repository.month_totals(start=start, end=end, provider=provider, tenant_id=tenant_id)
    previous_row = await repository.month_totals(
        start=prev_start, end=prev_end, provider=provider, tenant_id=tenant_id
    )
    department_rows = await repository.department_rows(
        start=start, end=end, provider=provider, tenant_id=tenant_id
    )
    budgets = {
        (str(item["tenant_id"]), str(item["name"])): float(item["monthly_token_budget"])
        for item in await department_repository.list_budgets()
        if item.get("monthly_token_budget") is not None
    }
    rows: list[DepartmentCostRow] = []
    alert_count = 0
    over_90_count = 0
    for item in department_rows:
        cost = round(float(item.get("total_cost") or 0), 2)
        budget = budgets.get((str(item.get("tenant_id")), str(item.get("department_name"))))
        percent = round(cost / budget * 100, 1) if budget else None
        level = _budget_level(percent)
        if level in {"warning", "critical", "over"}:
            alert_count += 1
        if level in {"critical", "over"}:
            over_90_count += 1
        rows.append(
            DepartmentCostRow(
                tenant_id=str(item.get("tenant_id") or ""),
                tenant_name=str(item.get("tenant_name") or ""),
                department_name=str(item.get("department_name") or "未填写部门"),
                input_tokens=int(item.get("input_tokens") or 0),
                output_tokens=int(item.get("output_tokens") or 0),
                cache_hit_tokens=int(item.get("cache_hit_tokens") or 0),
                total_tokens=int(item.get("total_tokens") or 0),
                ai_calls=int(item.get("ai_calls") or 0),
                task_count=int(item.get("task_count") or 0),
                total_cost=cost,
                main_provider=(str(item["main_provider"]) if item.get("main_provider") else None),
                main_model=(str(item["main_model"]) if item.get("main_model") else None),
                monthly_budget=budget,
                budget_used_percent=percent,
                budget_level=level,  # type: ignore[arg-type]
            )
        )
    prices = [PriceVersion.model_validate(item) for item in await repository.price_versions()]
    totals = _totals(totals_row)
    previous = _totals(previous_row)
    return DepartmentCostReportResponse(
        month=month_key,
        month_label=f"{start.year} 年 {start.month} 月",
        start=start,
        end=end,
        generated_at=now or datetime.now(UTC),
        provider=provider,
        providers=await repository.providers(),
        months=_recent_month_keys(now=now),
        totals=totals,
        previous_totals=previous,
        token_change_percent=_change_percent(totals.total_tokens, previous.total_tokens),
        cost_change_percent=_change_percent(totals.total_cost, previous.total_cost),
        alert_department_count=alert_count,
        over_90_department_count=over_90_count,
        rows=rows,
        current_price=prices[0] if prices else None,
        price_history=prices,
    )


@router.get("/departments", response_model=DepartmentCostReportResponse, summary="部门 Token 消耗与费用")
async def get_department_cost_report(
    repository: Annotated[PostgresDepartmentCostRepository, Depends(_get_department_cost_repository)],
    department_repository: Annotated[PostgresDepartmentRepository, Depends(_get_department_repository)],
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    provider: str | None = Query(default=None, max_length=64),
    tenant_id: str | None = Query(default=None),
) -> DepartmentCostReportResponse:
    return await build_department_cost_report(
        repository,
        department_repository,
        month=month,
        provider=(provider or "").strip() or None,
        tenant_id=(tenant_id or "").strip() or None,
    )


@router.get("/departments/export", summary="导出部门费用明细（CSV）")
async def export_department_cost_report(
    repository: Annotated[PostgresDepartmentCostRepository, Depends(_get_department_cost_repository)],
    department_repository: Annotated[PostgresDepartmentRepository, Depends(_get_department_repository)],
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    provider: str | None = Query(default=None, max_length=64),
    tenant_id: str | None = Query(default=None),
) -> Response:
    report = await build_department_cost_report(
        repository,
        department_repository,
        month=month,
        provider=(provider or "").strip() or None,
        tenant_id=(tenant_id or "").strip() or None,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["月份", "公司", "部门", "输入 Token", "输出 Token", "缓存命中 Token", "调用次数", "任务数", "金额", "主要供应商", "主要模型", "月度预算", "预算使用率"])
    for row in report.rows:
        writer.writerow(
            [
                report.month,
                row.tenant_name,
                row.department_name,
                row.input_tokens,
                row.output_tokens,
                row.cache_hit_tokens,
                row.ai_calls,
                row.task_count,
                f"{row.total_cost:.2f}",
                row.main_provider or "",
                row.main_model or "",
                ("" if row.monthly_budget is None else f"{row.monthly_budget:.2f}"),
                ("" if row.budget_used_percent is None else f"{row.budget_used_percent:.1f}%"),
            ]
        )
    filename = f"department-token-cost-{report.month}.csv"
    return Response(
        content=("\ufeff" + buffer.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/departments/detail", response_model=DepartmentCostDetailResponse, summary="部门费用明细（按任务）")
async def get_department_cost_detail(
    repository: Annotated[PostgresDepartmentCostRepository, Depends(_get_department_cost_repository)],
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    provider: str | None = Query(default=None, max_length=64),
    tenant_id: str | None = Query(default=None),
    department_name: str | None = Query(default=None, max_length=128),
) -> DepartmentCostDetailResponse:
    try:
        month_key, start, end, _, _ = _month_window(month)
    except ValueError as exc:
        raise AliothError(message=str(exc)) from exc
    rows = await repository.task_rows(
        start=start,
        end=end,
        provider=(provider or "").strip() or None,
        tenant_id=(tenant_id or "").strip() or None,
        department_name=(department_name or "").strip() or None,
    )
    return DepartmentCostDetailResponse(
        month=month_key,
        tenant_id=(tenant_id or "").strip() or None,
        department_name=(department_name or "").strip() or None,
        items=[DepartmentCostTaskRow.model_validate(row) for row in rows],
    )
