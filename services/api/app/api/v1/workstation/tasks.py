"""工作台任务页面接口。"""

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.api.v1.object_access import (
    require_task_read_access,
    require_task_write_access,
)
from services.api.app.api.v1.tasks import _get_task_submission_service
from services.api.app.application.evidence_lifecycle import EvidenceLifecycleService
from services.api.app.application.execution_ruleset import TaskRulesetSelectionError
from services.api.app.application.task_submission import (
    TaskRulesetNotFoundError,
    TaskSubmissionIdempotencyConflictError,
    TaskSubmissionIdempotencyKeyError,
)
from services.api.app.application.workstation_task_deletion import (
    TaskDeletionForbiddenError,
    WorkstationTaskDeletionService,
)
from services.api.app.application.workstation_tasks import (
    InvalidTaskOperationError,
    ProductSnapshot,
    TaskNotFoundError,
    WebStageDurations,
    WorkstationTaskCreateCommand,
    WorkstationTaskListFilter,
    WorkstationTaskService,
)
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.domain.evidence.lifecycle import EvidenceLifecyclePolicy
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import (
    H5InspectionNotificationMode,
    TaskExecutionMode,
    TaskTargetPlatform,
)
from services.api.app.infrastructure.db.evidence_lifecycle_repository import (
    PostgresEvidenceLifecycleRepository,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.db.workstation_task_cancellation_repository import (
    PostgresWorkstationTaskCancellationRepository,
)
from services.api.app.infrastructure.db.workstation_task_deletion_repository import (
    PostgresWorkstationTaskDeletionRepository,
)
from services.api.app.infrastructure.queue.redis_queue import (
    RedisAgentRunQueue,
    build_redis_client,
)

router = APIRouter(prefix="/tasks")

TaskStatus = Literal["queued", "running", "canceling", "review", "done", "failed", "canceled"]
ReportStatus = Literal["not_started", "generating", "ready", "failed"]
TaskChannel = Literal["workbench", "telegram", "api"]
TaskOperation = Literal[
    "start",
    "cancel",
    "rerun",
    "exploration",
    "copy",
    "retry-finalization",
]


class ProductSnapshotResponse(BaseModel):
    """产品快照响应。"""

    department_name: str
    product_code: str
    product_name: str


class ProductSnapshotRequest(BaseModel):
    """产品快照请求。"""

    department_name: str = Field(default="-", max_length=128)
    product_code: str = Field(default="-", max_length=128)
    product_name: str = Field(default="-", max_length=128)


class WorkstationTaskListItemResponse(BaseModel):
    """任务中心列表项响应。"""

    task_id: str
    task_name: str
    product: ProductSnapshotResponse
    target_url: str | None
    target_platform: TaskTargetPlatform
    execution_mode: TaskExecutionMode
    journey_steps: list[str]
    status: str
    progress: int
    report_status: str
    report_availability: Literal["none", "preview", "final"]
    failure_reason: str | None
    available_actions: list[str]
    created_at: datetime
    # 2.0 原型任务列表：所属公司 / 发起人 / 执行时长（执行时长按开始与结束时间现算）
    tenant_name: str | None = None
    submitter_name: str | None = None
    execution_started_at: datetime | None = None
    execution_finished_at: datetime | None = None
    duration_ms: int | None = None


class WorkstationTaskListResponse(BaseModel):
    """任务中心分页响应。"""

    items: list[WorkstationTaskListItemResponse]
    total: int
    page: int
    page_size: int


class WebStageDurationsResponse(BaseModel):
    """Web V2 正式链路阶段耗时，单位为毫秒。"""

    exploration_ms: int | None = Field(default=None, ge=0)
    inspection_ms: int | None = Field(default=None, ge=0)
    report_generation_ms: int | None = Field(default=None, ge=0)
    total_ms: int | None = Field(default=None, ge=0)


class WorkstationTaskDetailResponse(WorkstationTaskListItemResponse):
    """工作台任务详情响应。"""

    started_at: datetime | None
    finished_at: datetime | None
    ruleset_code: str
    ruleset_version: int
    channel: str
    priority: int
    run_id: str | None
    workflow_version: str
    scan_mode: str
    remark: str | None
    required_page_types: list[str]
    optional_page_types: list[str] = Field(default_factory=list)
    not_applicable_page_types: list[str] = Field(default_factory=list)
    applicability_mode: str | None = None
    applicability_catalog_version: str | None = None
    applicability_catalog_hash: str | None = None
    browser_profile_id: str | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    execution_status: str | None = None
    finalization_state: str | None = None
    completion_gate_passed: bool = False
    report_available: bool = False
    coverage_status: str | None = None
    coverage_observed_page_types: list[str] = Field(default_factory=list)
    coverage_missing_page_types: list[str] = Field(default_factory=list)
    coverage_unrecognized_page_types: list[str] = Field(default_factory=list)
    coverage_normalization_protocol_version: str | None = None
    stage_durations: WebStageDurationsResponse | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    run_status: str | None = None
    execution_phase: Literal[
        "queued",
        "pending",
        "running",
        "configuration_error",
        "navigation_failed",
        "evidence_failed",
        "classification_review",
        "done",
        "canceled",
    ] | None = None
    queue_position: int | None = Field(default=None, ge=1)
    tasks_ahead: int | None = Field(default=None, ge=0)
    available_worker_count: int | None = Field(default=None, ge=0)
    start_condition: str | None = None


class WorkstationTaskCreateRequest(BaseModel):
    """工作台创建任务请求。"""

    task_name: str = Field(..., min_length=1, max_length=256)
    target_url: str = Field(..., min_length=1, max_length=4096)
    # 不填 = 跑全部开启的规则集；ruleset_codes 选多个；ruleset_code 只锁单个规则集（老调用方）
    ruleset_code: str | None = Field(default=None, min_length=1, max_length=128)
    ruleset_version: int | None = Field(default=None, ge=1)
    ruleset_codes: list[str] = Field(default_factory=list, max_length=20)
    channel: TaskChannel = "workbench"
    priority: int = Field(default=100, ge=0)
    product: ProductSnapshotRequest = Field(default_factory=ProductSnapshotRequest)
    target_platform: TaskTargetPlatform = TaskTargetPlatform.H5
    execution_mode: TaskExecutionMode = TaskExecutionMode.FULL_INSPECTION
    required_page_types: list[str] = Field(default_factory=list)
    scan_mode: Literal["full", "quick", "custom"] = "full"
    journey_steps: list[str] = Field(default_factory=list)
    remark: str | None = None


class WorkstationTaskOperationResponse(BaseModel):
    """任务操作响应。"""

    task_id: str
    status: str
    run_id: str | None
    message: str


@lru_cache
def _get_workstation_task_service() -> WorkstationTaskService:
    settings = get_settings()
    redis_client = build_redis_client(settings.queue)
    queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.agent_run_queue_key,
    )
    web_queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.web_agent_run_queue_key,
    )
    return WorkstationTaskService(
        repository=PostgresWorkstationQueryRepository(),
        submission_service=_get_task_submission_service(),
        queue_status_reader=queue,
        web_queue_status_reader=web_queue,
        terminal_run_task_canceler=PostgresWorkstationTaskCancellationRepository(),
        h5_inspection_notification_mode=H5InspectionNotificationMode(
            settings.workflow.h5_inspection_notification_mode
        ),
    )


@router.get("", response_model=WorkstationTaskListResponse, summary="查询工作台任务中心列表")
async def list_workstation_tasks(
    service: Annotated[WorkstationTaskService, Depends(_get_workstation_task_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    task_name: str | None = None,
    product_name: str | None = None,
    product_code: str | None = None,
    target_url: str | None = None,
    status: TaskStatus | None = None,
    report_status: ReportStatus | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    department_name: str | None = None,
    target_platform: TaskTargetPlatform | None = None,
    submitter_name: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> WorkstationTaskListResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    result = await service.list_tasks(
        WorkstationTaskListFilter(
            task_name=task_name,
            product_name=product_name,
            product_code=product_code,
            target_url=target_url,
            status=status,
            report_status=report_status,
            created_from=created_from,
            created_to=created_to,
            department_name=(department_name or "").strip() or None,
            submitter_name=(submitter_name or "").strip() or None,
            target_platform=target_platform.value if target_platform is not None else None,
            page=safe_page,
            page_size=safe_page_size,
        ),
        access_scope=access_scope,
    )
    return WorkstationTaskListResponse(
        items=[_list_item_response(item) for item in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@lru_cache
def _get_workstation_task_deletion_service() -> WorkstationTaskDeletionService:
    return WorkstationTaskDeletionService(PostgresWorkstationTaskDeletionRepository())


@router.delete(
    "/{task_id}",
    response_model=WorkstationTaskOperationResponse,
    summary="删除管理端已结束任务",
)
async def delete_workstation_task(
    task_id: str,
    service: Annotated[
        WorkstationTaskDeletionService, Depends(_get_workstation_task_deletion_service)
    ],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
) -> WorkstationTaskOperationResponse:
    try:
        result = await service.delete_task(task_id, access_scope=access_scope)
    except TaskDeletionForbiddenError as exc:
        raise AliothError(message=str(exc), status_code=403) from exc
    except TaskNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except InvalidTaskOperationError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    return WorkstationTaskOperationResponse(
        task_id=result.task_id,
        status=result.status,
        run_id=result.run_id,
        message=result.message,
    )


@router.post(
    "",
    response_model=WorkstationTaskOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="从工作台创建质检任务",
)
async def create_workstation_task(
    payload: WorkstationTaskCreateRequest,
    service: Annotated[WorkstationTaskService, Depends(_get_workstation_task_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkstationTaskOperationResponse:
    try:
        result = await service.create_task(
            _create_command(payload),
            access_scope=access_scope,
            idempotency_key=idempotency_key,
        )
    except TaskRulesetSelectionError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except TaskRulesetNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except TaskSubmissionIdempotencyConflictError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except TaskSubmissionIdempotencyKeyError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    except InvalidTaskOperationError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except ValueError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    return WorkstationTaskOperationResponse(
        task_id=result.task_id,
        status=result.status,
        run_id=result.run_id,
        message=result.message,
    )


@router.post(
    "/{task_id}/{operation}",
    response_model=WorkstationTaskOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="执行工作台任务操作",
)
async def perform_workstation_task_operation(
    task_id: str,
    operation: TaskOperation,
    service: Annotated[WorkstationTaskService, Depends(_get_workstation_task_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_write_access)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkstationTaskOperationResponse:
    try:
        result = await service.perform_operation(
            task_id=task_id,
            operation=operation,
            access_scope=access_scope,
            idempotency_key=idempotency_key,
        )
    except TaskNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except InvalidTaskOperationError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except TaskSubmissionIdempotencyConflictError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except TaskSubmissionIdempotencyKeyError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    return WorkstationTaskOperationResponse(
        task_id=result.task_id,
        status=result.status,
        run_id=result.run_id,
        message=result.message,
    )


@router.get(
    "/{task_id}",
    response_model=WorkstationTaskDetailResponse,
    summary="查询工作台任务详情",
)
async def get_workstation_task(
    task_id: str,
    service: Annotated[WorkstationTaskService, Depends(_get_workstation_task_service)],
    access_scope: Annotated[AccessScope, Depends(require_task_read_access)],
) -> WorkstationTaskDetailResponse:
    detail = await service.get_task_detail(task_id, access_scope=access_scope)
    if detail is None:
        raise AliothError(message=f"未找到任务：{task_id}", status_code=404)
    return WorkstationTaskDetailResponse(
        **_list_item_response(detail).model_dump(),
        started_at=detail.started_at,
        finished_at=detail.finished_at,
        ruleset_code=detail.ruleset_code,
        ruleset_version=detail.ruleset_version,
        channel=detail.channel,
        priority=detail.priority,
        run_id=detail.run_id,
        workflow_version=detail.workflow_version,
        scan_mode=detail.scan_mode,
        remark=detail.remark,
        required_page_types=list(detail.required_page_types),
        optional_page_types=list(detail.optional_page_types),
        not_applicable_page_types=list(detail.not_applicable_page_types),
        applicability_mode=detail.applicability_mode,
        applicability_catalog_version=detail.applicability_catalog_version,
        applicability_catalog_hash=detail.applicability_catalog_hash,
        browser_profile_id=detail.browser_profile_id,
        viewport_width=detail.viewport_width,
        viewport_height=detail.viewport_height,
        execution_status=detail.execution_status,
        finalization_state=detail.finalization_state,
        completion_gate_passed=detail.completion_gate_passed,
        report_available=detail.report_available,
        coverage_status=detail.coverage_status,
        coverage_observed_page_types=list(detail.coverage_observed_page_types),
        coverage_missing_page_types=list(detail.coverage_missing_page_types),
        coverage_unrecognized_page_types=list(detail.coverage_unrecognized_page_types),
        coverage_normalization_protocol_version=(
            detail.coverage_normalization_protocol_version
        ),
        stage_durations=(
            WebStageDurationsResponse(**detail.stage_durations.to_dict())
            if isinstance(detail.stage_durations, WebStageDurations)
            else None
        ),
        run_status=detail.run_status,
        execution_phase=detail.execution_phase,
        queue_position=detail.queue_position,
        tasks_ahead=detail.tasks_ahead,
        available_worker_count=detail.available_worker_count,
        start_condition=detail.start_condition,
    )


def _create_command(payload: WorkstationTaskCreateRequest) -> WorkstationTaskCreateCommand:
    return WorkstationTaskCreateCommand(
        task_name=payload.task_name,
        target_url=payload.target_url,
        ruleset_code=payload.ruleset_code,
        ruleset_version=payload.ruleset_version,
        ruleset_codes=tuple(code.strip() for code in payload.ruleset_codes if code.strip()),
        channel=payload.channel,
        priority=payload.priority,
        product=ProductSnapshot(
            department_name=payload.product.department_name,
            product_code=payload.product.product_code,
            product_name=payload.product.product_name,
        ),
        target_platform=payload.target_platform,
        execution_mode=payload.execution_mode,
        required_page_types=tuple(payload.required_page_types),
        scan_mode=payload.scan_mode,
        journey_steps=payload.journey_steps,
        remark=payload.remark,
    )


def _list_item_response(item) -> WorkstationTaskListItemResponse:
    return WorkstationTaskListItemResponse(
        task_id=item.task_id,
        task_name=item.task_name,
        product=ProductSnapshotResponse(
            department_name=item.product.department_name,
            product_code=item.product.product_code,
            product_name=item.product.product_name,
        ),
        target_url=item.target_url,
        target_platform=item.target_platform,
        execution_mode=item.execution_mode,
        journey_steps=item.journey_steps,
        status=item.status,
        progress=item.progress,
        report_status=item.report_status,
        report_availability=item.report_availability,
        failure_reason=item.failure_reason,
        available_actions=item.available_actions,
        created_at=item.created_at,
        tenant_name=getattr(item, "tenant_name", None),
        submitter_name=getattr(item, "submitter_name", None),
        execution_started_at=getattr(item, "execution_started_at", None),
        execution_finished_at=getattr(item, "execution_finished_at", None),
        duration_ms=_duration_ms(
            getattr(item, "execution_started_at", None),
            getattr(item, "execution_finished_at", None),
        ),
    )


def _duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if started_at is None or finished_at is None:
        return None
    return max(int((finished_at - started_at).total_seconds() * 1000), 0)


class EvidenceLegalHoldRequest(BaseModel):
    """任务级证据合法留存申请。"""

    reason: str = Field(min_length=1, max_length=512)


class EvidenceLegalHoldReleaseRequest(BaseModel):
    """释放任务级证据合法留存。"""

    release_reason: str = Field(min_length=1, max_length=512)


@lru_cache
def _get_evidence_lifecycle_service() -> EvidenceLifecycleService:
    settings = get_settings().evidence_lifecycle
    return EvidenceLifecycleService(
        repository=PostgresEvidenceLifecycleRepository(),
        policy=EvidenceLifecyclePolicy(
            policy_version=settings.policy_version,
            retention_days=settings.retention_days,
            gc_enabled=settings.gc_enabled,
            batch_limit=settings.batch_limit,
        ),
    )


@router.post(
    "/{task_id}/evidence/legal-hold",
    summary="对任务证据施加合法留存（阻止 GC）",
)
async def apply_task_evidence_legal_hold(
    task_id: str,
    payload: EvidenceLegalHoldRequest,
    access_scope: Annotated[AccessScope, Depends(require_task_write_access)],
    service: Annotated[
        EvidenceLifecycleService,
        Depends(_get_evidence_lifecycle_service),
    ],
) -> dict[str, str]:
    hold_id = await service.apply_legal_hold(
        task_id=task_id,
        reason=payload.reason,
        held_by=access_scope.actor_user_id,
    )
    return {"task_id": task_id, "hold_id": hold_id}


@router.post(
    "/{task_id}/evidence/legal-hold/release",
    summary="释放任务证据合法留存",
)
async def release_task_evidence_legal_hold(
    task_id: str,
    payload: EvidenceLegalHoldReleaseRequest,
    access_scope: Annotated[AccessScope, Depends(require_task_write_access)],
    service: Annotated[
        EvidenceLifecycleService,
        Depends(_get_evidence_lifecycle_service),
    ],
) -> dict[str, object]:
    released = await service.release_legal_hold(
        task_id=task_id,
        release_reason=payload.release_reason,
        released_by=access_scope.actor_user_id,
    )
    return {"task_id": task_id, "released_count": released}
