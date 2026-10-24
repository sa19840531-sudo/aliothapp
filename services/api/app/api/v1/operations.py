"""运营总览与使用分析接口（2.1 原型「运营工作台 index.html」与「使用分析 analytics.html」）。

两个接口返回同一份聚合结构，只是权限不同：运营总览对所有管理后台账号开放（分公司管理员
只看本公司），使用分析只对运营中心开放。数字只做聚合，不展开截图和日志。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.api.v1.auth import AuthUserResponse, require_access_scope, require_auth_user
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.operations_repository import PostgresOperationsRepository

router = APIRouter()

RangeKey = Literal["7d", "30d", "90d", "quarter", "year"]
PlatformKey = Literal["h5", "web", "app", "ios"]


class OperationsSummary(BaseModel):
    task_count: int = 0
    report_count: int = 0
    report_success_rate: float | None = None
    failure_rate: float | None = None
    done_count: int = 0
    review_count: int = 0
    failed_count: int = 0
    canceled_count: int = 0
    active_count: int = 0
    active_tenant_count: int = 0
    active_account_count: int = 0
    avg_duration_seconds: float | None = None


class TrendPoint(BaseModel):
    date: str
    task_count: int = 0
    report_count: int = 0
    failed_count: int = 0


class PlatformShare(BaseModel):
    platform: str
    task_count: int


class TenantUsageRow(BaseModel):
    tenant_id: str
    tenant_name: str
    status: str
    product_count: int = 0
    active_account_count: int = 0
    task_count: int = 0
    report_count: int = 0
    report_success_rate: float | None = None
    failure_rate: float | None = None
    last_used_at: datetime | None = None
    usage_status: Literal["active", "idle"] = "idle"


class OperationsOverviewResponse(BaseModel):
    range: str
    start: datetime
    end: datetime
    tenant_id: str | None = None
    platform: str | None = None
    generated_at: datetime
    summary: OperationsSummary
    previous: OperationsSummary
    trend: list[TrendPoint]
    platform_share: list[PlatformShare]
    tenants: list[TenantUsageRow]


@lru_cache
def _get_operations_repository() -> PostgresOperationsRepository:
    return PostgresOperationsRepository()


def _window(range_key: str, *, now: datetime | None = None) -> tuple[datetime, datetime, datetime, datetime]:
    """返回 (本期起, 本期止, 上期起, 上期止)；本季度 / 本年度的上期取等长的前一段。"""
    current = now or datetime.now(UTC)
    end = current
    if range_key == "quarter":
        quarter_month = ((current.month - 1) // 3) * 3 + 1
        start = current.replace(month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif range_key == "year":
        start = current.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        days = {"7d": 7, "30d": 30, "90d": 90}.get(range_key, 30)
        start = (current - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    length = end - start
    return start, end, start - length, start


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 1)


def _summary(row: dict[str, object]) -> OperationsSummary:
    task_count = int(row.get("task_count") or 0)
    report_count = int(row.get("report_count") or 0)
    failed_count = int(row.get("failed_count") or 0)
    finished = report_count + failed_count
    avg_seconds = row.get("avg_duration_seconds")
    return OperationsSummary(
        task_count=task_count,
        report_count=report_count,
        report_success_rate=_rate(report_count, finished),
        failure_rate=_rate(failed_count, task_count),
        done_count=int(row.get("done_count") or 0),
        review_count=int(row.get("review_count") or 0),
        failed_count=failed_count,
        canceled_count=int(row.get("canceled_count") or 0),
        active_count=int(row.get("active_count") or 0),
        active_tenant_count=int(row.get("active_tenant_count") or 0),
        active_account_count=int(row.get("active_account_count") or 0),
        avg_duration_seconds=(round(float(avg_seconds), 1) if avg_seconds is not None else None),
    )


def _fill_trend(rows: list[dict[str, object]], *, start: datetime, end: datetime) -> list[TrendPoint]:
    by_day = {str(row.get("day")): row for row in rows}
    points: list[TrendPoint] = []
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    # 最多铺 370 个点，避免异常时间窗把响应撑大。
    guard = 0
    while cursor <= end and guard < 370:
        key = cursor.strftime("%Y-%m-%d")
        row = by_day.get(key, {})
        points.append(
            TrendPoint(
                date=key,
                task_count=int(row.get("task_count") or 0),
                report_count=int(row.get("report_count") or 0),
                failed_count=int(row.get("failed_count") or 0),
            )
        )
        cursor += timedelta(days=1)
        guard += 1
    return points


def _tenant_row(row: dict[str, object], *, start: datetime) -> TenantUsageRow:
    task_count = int(row.get("task_count") or 0)
    report_count = int(row.get("report_count") or 0)
    failed_count = int(row.get("failed_count") or 0)
    last_task_at = row.get("last_task_at")
    last_login_at = row.get("last_login_at")
    candidates = [value for value in (last_task_at, last_login_at) if isinstance(value, datetime)]
    last_used_at = max(candidates) if candidates else None
    active = task_count > 0 or (last_used_at is not None and last_used_at >= start)
    return TenantUsageRow(
        tenant_id=str(row.get("tenant_id") or ""),
        tenant_name=str(row.get("tenant_name") or ""),
        status=str(row.get("status") or "enabled"),
        product_count=int(row.get("product_count") or 0),
        active_account_count=int(row.get("active_account_count") or 0),
        task_count=task_count,
        report_count=report_count,
        report_success_rate=_rate(report_count, report_count + failed_count),
        failure_rate=_rate(failed_count, task_count),
        last_used_at=last_used_at,
        usage_status="active" if active else "idle",
    )


async def build_operations_overview(
    repository: PostgresOperationsRepository,
    *,
    range_key: str,
    tenant_id: str | None,
    platform: str | None,
    now: datetime | None = None,
) -> OperationsOverviewResponse:
    start, end, prev_start, prev_end = _window(range_key, now=now)
    current = await repository.summarize_window(start=start, end=end, tenant_id=tenant_id, platform=platform)
    previous = await repository.summarize_window(
        start=prev_start, end=prev_end, tenant_id=tenant_id, platform=platform
    )
    trend_rows = await repository.daily_trend(start=start, end=end, tenant_id=tenant_id, platform=platform)
    share_rows = await repository.platform_share(start=start, end=end, tenant_id=tenant_id, platform=platform)
    tenant_rows = await repository.tenant_rows(start=start, end=end, tenant_id=tenant_id, platform=platform)
    return OperationsOverviewResponse(
        range=range_key,
        start=start,
        end=end,
        tenant_id=tenant_id,
        platform=platform,
        generated_at=now or datetime.now(UTC),
        summary=_summary(current),
        previous=_summary(previous),
        trend=_fill_trend(trend_rows, start=start, end=end),
        platform_share=[
            PlatformShare(platform=str(row.get("platform") or ""), task_count=int(row.get("task_count") or 0))
            for row in share_rows
        ],
        tenants=[_tenant_row(row, start=start) for row in tenant_rows],
    )


def _scoped_tenant(scope: AccessScope, requested: str | None) -> str | None:
    if scope.can_read_any:
        return (requested or "").strip() or None
    return scope.tenant_id


@router.get("/operations/overview", response_model=OperationsOverviewResponse, summary="运营总览")
async def get_operations_overview(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresOperationsRepository, Depends(_get_operations_repository)],
    range: RangeKey = "30d",
    tenant_id: str | None = None,
    platform: PlatformKey | None = None,
) -> OperationsOverviewResponse:
    if "dashboard" not in current_user.permissions:
        raise AliothError(message="无权限访问该资源", status_code=403)
    return await build_operations_overview(
        repository,
        range_key=range,
        tenant_id=_scoped_tenant(scope, tenant_id),
        platform=platform,
    )


@router.get("/operations/analytics", response_model=OperationsOverviewResponse, summary="使用分析")
async def get_operations_analytics(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresOperationsRepository, Depends(_get_operations_repository)],
    range: RangeKey = "30d",
    tenant_id: str | None = None,
    platform: PlatformKey | None = None,
) -> OperationsOverviewResponse:
    # 使用分析只给运营中心看：分公司和部门账号连地址直接访问都不放行。
    if "analytics" not in current_user.permissions or not scope.can_read_any:
        raise AliothError(message="使用分析仅对运营中心开放", status_code=403)
    return await build_operations_overview(
        repository,
        range_key=range,
        tenant_id=(tenant_id or "").strip() or None,
        platform=platform,
    )
