"""工作台任务详情过程可视化接口：页面时间线 / 规则结果 / 统计 / 日志 / 截图证据。

为新版「任务详情」页提供聚合读模型；数据由 Worker 旁路观察者与既有执行链落库，
本层只读。聚合结构随原型迭代，返回体沿用报告详情的 dict 风格（见
``workstation/reports.py`` 先例），不再为每个区块建僵硬的响应模型。
"""

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Response

from services.api.app.api.v1.object_access import require_task_read_access
from services.api.app.api.v1.workstation.read_errors import audited_read_error
from services.api.app.application.page_graph import PageGraphService
from services.api.app.application.page_graph_contracts import PageGraphUnavailableError
from services.api.app.application.task_insight import (
    TaskInsightNotFoundError,
    TaskInsightService,
)
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    PostgresTaskRuleInventoryRepository,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)

router = APIRouter(prefix="/tasks")

RuleDisplayStatus = Literal[
    "passed",
    "failed",
    "not_detected",
    "not_applicable",
    "coverage_gap",
    "not_executed",
    "detect_error",
    "evidence_insufficient",
    "manual_review",
    "running",
    "pending",
]
LogLevel = Literal["INFO", "WARN", "ERROR", "EXEC"]
InspectionWorkItemControlMode = Literal["shadow", "enforced"]


@lru_cache
def _get_task_insight_service() -> TaskInsightService:
    return TaskInsightService(
        repository=PostgresTaskInsightRepository(),
        page_graph_service=PageGraphService(repository=PostgresPageGraphRepository()),
        web_evidence_repository=PostgresWebCaptureEvidenceRepository(),
        rule_inventory_repository=PostgresTaskRuleInventoryRepository(),
    )


@router.get("/{task_id}/app-stage", summary="查询 App 质检阶段投影")
async def get_app_stage(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.app_stage(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get("/{task_id}/evidence-index", summary="查询已封存的探索 Evidence Index")
async def get_evidence_index(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.evidence_index(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get("/{task_id}/web-evidence", summary="查询当前 Web V2 脱敏探索证据包")
async def get_web_evidence(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.web_evidence(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get("/{task_id}/page-timeline", summary="查询任务页面识别时间线")
async def get_page_timeline(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.page_timeline(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except Exception as exc:
        raise audited_read_error(
            operation="page_timeline",
            cause=exc,
            task_id=task_id,
        ) from exc


@router.get("/{task_id}/page-graph", summary="查询任务页面状态与实际转场图")
async def get_page_graph(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.page_graph(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except PageGraphUnavailableError as exc:
        raise AliothError(message=str(exc), status_code=503) from exc


@router.get("/{task_id}/page-graph/recovery-route", summary="查询页面图只读恢复候选路线")
async def get_page_graph_recovery_route(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
    source_state_id: str | None = None,
    target_identity: str | None = None,
    limit: int = 20,
) -> dict:
    try:
        return await service.page_recovery_route(
            task_id,
            source_state_id=source_state_id,
            target_identity=target_identity,
            limit=min(max(limit, 1), 100),
            access_scope=access_scope,
        )
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except PageGraphUnavailableError as exc:
        raise AliothError(message=str(exc), status_code=503) from exc


@router.get("/{task_id}/rule-results", summary="查询任务规则执行结果（候选∪执行，统一展示态）")
async def get_rule_results(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
    display_status: RuleDisplayStatus | None = None,
    page_type: str | None = None,
) -> dict:
    try:
        return await service.rule_results(
            task_id,
            display_status=display_status,
            page_type=page_type,
            access_scope=access_scope,
        )
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get(
    "/{task_id}/evidence-gaps",
    summary="查询任务规则证据缺口（补采请求台账，只记录不执行）",
)
async def get_evidence_gaps(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.evidence_gaps(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get(
    "/{task_id}/closure-outcome",
    summary="查询任务收口结论（页面探索/证据充分/规则结果/最终完成检查分别给出，只读）",
)
async def get_closure_outcome(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.closure_outcome(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get(
    "/{task_id}/page-type-judgements",
    summary="查询任务页面类型判断与改判记录（各来源判断、裁定与正式识别差异，只读）",
)
async def get_page_type_judgements(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.page_type_judgements(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get(
    "/{task_id}/rule-reuse",
    summary="查询任务规则结论复用记录（来源、依据与失效条件，只读）",
)
async def get_rule_reuse(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.rule_reuse(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get("/{task_id}/stats", summary="查询任务命中规则统计与最近异常")
async def get_stats(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
    recent_limit: int = 10,
) -> dict:
    try:
        return await service.stats(
            task_id,
            recent_limit=min(max(recent_limit, 1), 50),
            access_scope=access_scope,
        )
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get("/{task_id}/inspection-work-items", summary="查询任务持久化质检工作项")
async def get_inspection_work_items(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
    control_mode: InspectionWorkItemControlMode | None = None,
) -> dict:
    try:
        return await service.inspection_work_items(
            task_id,
            control_mode=control_mode,
            access_scope=access_scope,
        )
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc


@router.get("/{task_id}/logs", summary="查询任务执行日志")
async def get_logs(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
    level: LogLevel | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    try:
        return await service.logs(
            task_id,
            level=level,
            page=max(page, 1),
            page_size=min(max(page_size, 1), 500),
            access_scope=access_scope,
        )
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except Exception as exc:
        raise audited_read_error(
            operation="task_logs",
            cause=exc,
            task_id=task_id,
        ) from exc


@router.get("/{task_id}/logs/export", summary="导出任务执行日志（纯文本）")
async def export_logs(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> Response:
    try:
        text = await service.logs_export_text(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    filename = quote(f"task_{task_id}_logs.txt")
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/{task_id}/screenshots", summary="查询任务截图证据（按页面分组去重）")
async def get_screenshots(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> dict:
    try:
        return await service.screenshots(task_id, access_scope=access_scope)
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except Exception as exc:
        raise audited_read_error(
            operation="task_screenshots",
            cause=exc,
            task_id=task_id,
        ) from exc


@router.get("/{task_id}/screenshots/archive", summary="打包下载任务全部截图")
async def download_screenshots_archive(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> Response:
    try:
        filename, content = await service.screenshots_archive(
            task_id,
            access_scope=access_scope,
        )
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.get("/{task_id}/platform-progress/export", summary="导出平台质检能力进展 Excel")
async def export_platform_progress(
    task_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> Response:
    try:
        filename, content = await service.platform_progress_export(
            task_id,
            access_scope=access_scope,
        )
    except TaskInsightNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )
