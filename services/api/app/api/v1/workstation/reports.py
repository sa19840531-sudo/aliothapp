"""工作台报告页面接口。"""

import io
import zipfile
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.api.v1.object_access import (
    require_task_read_access,
    require_task_review_access,
)
from services.api.app.api.v1.workstation.read_errors import audited_read_error
from services.api.app.application.report_generation import (
    ReportGenerationService,
    ReportGenerationTargetNotFoundError,
)
from services.api.app.application.report_insight import (
    ReportConfirmationError,
    ReportExportBlockedError,
    ReportInsightNotFoundError,
    ReportInsightService,
)
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.application.task_query import TaskQueryService
from services.api.app.application.workstation_reports import (
    REPORT_LIST_STATUSES,
    WorkstationReportService,
)
from services.api.app.application.workstation_tasks import WorkstationTaskService
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    PostgresTaskRuleInventoryRepository,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)

router = APIRouter(prefix="/reports")


class WorkstationReportListResponse(BaseModel):
    """报告中心列表响应。"""

    items: list[dict]
    total: int
    page: int
    page_size: int


class ReportConfirmationRequest(BaseModel):
    """人工确认请求：通过 / 未通过 / 忽略（忽略=该规则无需质检）。"""

    verdict: Literal["pass", "fail", "ignored"]
    note: str | None = Field(default=None, max_length=2000)


@lru_cache
def _get_workstation_report_service() -> WorkstationReportService:
    task_repository = PostgresTaskRepository()
    return WorkstationReportService(
        task_service=WorkstationTaskService(repository=PostgresWorkstationQueryRepository()),
        report_generation_service=ReportGenerationService(
            query_service=TaskQueryService(repository=task_repository)
        ),
        final_report_snapshot_repository=task_repository,
        page_exploration_reader=TaskInsightService(
            repository=_get_task_insight_repository()
        ),
        report_insight_reader=_get_report_insight_service(),
    )


@lru_cache
def _get_task_insight_repository() -> PostgresTaskInsightRepository:
    return PostgresTaskInsightRepository()


@lru_cache
def _get_report_insight_service() -> ReportInsightService:
    return ReportInsightService(
        repository=_get_task_insight_repository(),
        final_report_snapshot_repository=PostgresTaskRepository(),
        rule_inventory_repository=PostgresTaskRuleInventoryRepository(),
    )


@router.get("", response_model=WorkstationReportListResponse, summary="查询工作台报告列表")
async def list_workstation_reports(
    service: Annotated[WorkstationReportService, Depends(_get_workstation_report_service)],
    insight_repository: Annotated[
        PostgresTaskInsightRepository, Depends(_get_task_insight_repository)
    ],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    page: int = 1,
    page_size: int = 20,
    task_name: str | None = None,
    product_name: str | None = None,
    department_name: str | None = None,
    target_platform: Literal["h5", "web", "app"] | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    report_status: Literal["ready", "generating", "failed"] | None = None,
) -> WorkstationReportListResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    try:
        payload = await service.list_reports(
            page=safe_page,
            page_size=safe_page_size,
            access_scope=access_scope,
            task_name=(task_name or "").strip() or None,
            product_name=(product_name or "").strip() or None,
            department_name=(department_name or "").strip() or None,
            target_platform=target_platform,
            created_from=created_from,
            created_to=created_to,
            report_status=report_status or REPORT_LIST_STATUSES,
        )
        # 补充报告维度元信息：待人工确认数与真实报告生成时间（run 完成时间）。
        meta = await insight_repository.report_list_meta(
            [item["task_id"] for item in payload["items"]],
            access_scope=access_scope,
        )
    except Exception as exc:
        raise audited_read_error(
            operation="list_reports",
            cause=exc,
            report=True,
        ) from exc
    for item in payload["items"]:
        item_meta = meta.get(item["task_id"], {})
        item["pending_confirm_count"] = item_meta.get("pending_confirm_count", 0)
        if item_meta:
            passed_count = int(item_meta.get("passed_count") or 0)
            failed_count = int(item_meta.get("failed_count") or 0)
            conclusive_count = passed_count + failed_count
            item["issue_count"] = failed_count
            item["pass_rate"] = (
                round(passed_count / conclusive_count * 100, 1) if conclusive_count else None
            )
            if item.get("attention_required") is None:
                item["attention_required"] = failed_count
        generated_at = item_meta.get("generated_at")
        if generated_at is not None:
            item["generated_at"] = generated_at.isoformat()
    return WorkstationReportListResponse(**payload)


@router.get(
    "/{task_id}/insight",
    summary="查询报告中心读模型（摘要/人工确认/全量明细）",
    dependencies=[Depends(require_task_read_access)],
)
async def get_report_insight(
    task_id: str,
    service: Annotated[ReportInsightService, Depends(_get_report_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
) -> dict:
    try:
        return await service.report_insight(task_id, access_scope=access_scope)
    except ReportInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except Exception as exc:
        raise audited_read_error(
            operation="report_insight",
            cause=exc,
            task_id=task_id,
            report=True,
        ) from exc


@router.post(
    "/{task_id}/confirmations/{execution_id}",
    summary="人工确认规则终审结论",
    dependencies=[Depends(require_task_review_access)],
)
async def confirm_report_rule(
    task_id: str,
    execution_id: str,
    payload: ReportConfirmationRequest,
    service: Annotated[ReportInsightService, Depends(_get_report_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
) -> dict:
    try:
        return await service.confirm_rule(
            task_id,
            execution_id,
            verdict=payload.verdict,
            note=payload.note,
            access_scope=access_scope,
        )
    except ReportInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except ReportConfirmationError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc


@router.get(
    "/export-batch",
    summary="批量导出自包含 HTML 质检报告（打包为 zip）",
)
async def export_reports_batch(
    service: Annotated[ReportInsightService, Depends(_get_report_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    task_ids: Annotated[list[str], Query(min_length=1, max_length=50)],
) -> Response:
    buffer = io.BytesIO()
    exported = 0
    skipped: list[str] = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for task_id in dict.fromkeys(task_ids):
            try:
                filename, html_text = await service.export_html(task_id, access_scope=access_scope)
            except (ReportInsightNotFoundError, ReportExportBlockedError) as exc:
                skipped.append(f"{task_id}: {exc}")
                continue
            archive.writestr(filename, html_text)
            exported += 1
        if skipped:
            archive.writestr("未导出说明.txt", "\n".join(skipped))
    if exported == 0:
        raise AliothError(message="所选报告都不能导出（未生成或待确认未清零）", status_code=409)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(f"质检报告_{exported}份.zip")
        },
    )


@router.get(
    "/{task_id}/export",
    summary="导出自包含 HTML 质检报告（待确认清零后可用）",
    dependencies=[Depends(require_task_read_access)],
)
async def export_report_html(
    task_id: str,
    service: Annotated[ReportInsightService, Depends(_get_report_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
) -> Response:
    try:
        filename, html_text = await service.export_html(task_id, access_scope=access_scope)
    except ReportInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except ReportExportBlockedError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except Exception as exc:
        raise audited_read_error(
            operation="export_report",
            cause=exc,
            task_id=task_id,
            report=True,
        ) from exc
    return Response(
        content=html_text,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.get(
    "/{task_id}",
    response_model=dict,
    summary="查询工作台任务报告详情",
    dependencies=[Depends(require_task_read_access)],
)
async def get_workstation_report(
    task_id: str,
    service: Annotated[WorkstationReportService, Depends(_get_workstation_report_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    agent_run_id: str | None = None,
) -> dict:
    try:
        return await service.get_report(
            task_id,
            agent_run_id=agent_run_id,
            access_scope=access_scope,
        )
    except ReportGenerationTargetNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except Exception as exc:
        raise audited_read_error(
            operation="get_report",
            cause=exc,
            task_id=task_id,
            report=True,
        ) from exc
