"""工作台首页聚合接口。"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.application.workstation_dashboard import WorkstationDashboardService
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.workstation_dashboard_repository import (
    PostgresWorkstationDashboardRepository,
)

router = APIRouter(prefix="/dashboard")


class WorkstationDashboardOverviewResponse(BaseModel):
    """工作台首页聚合响应。"""

    summary_cards: list[dict]
    today_metrics: dict
    recent_tasks: list[dict]
    status_distribution: list[dict]
    risk_distribution: list[dict]
    execution_status_distribution: list[dict]
    latest_rule_executions: list[dict]
    token_usage: dict | None = None


@lru_cache
def _get_workstation_dashboard_service() -> WorkstationDashboardService:
    return WorkstationDashboardService(repository=PostgresWorkstationDashboardRepository())


@router.get(
    "/overview",
    response_model=WorkstationDashboardOverviewResponse,
    summary="查询工作台首页聚合信息",
)
async def get_workstation_dashboard_overview(
    service: Annotated[WorkstationDashboardService, Depends(_get_workstation_dashboard_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
) -> WorkstationDashboardOverviewResponse:
    return WorkstationDashboardOverviewResponse(
        **await service.get_overview(access_scope=access_scope)
    )
