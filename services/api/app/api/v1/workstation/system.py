"""工作台系统状态接口：Worker 存活、队列深度与排队积压的可观测出口。

背景：Worker 启动即崩（如规则引擎配置校验失败）时，任务会无限停留在"排队中"，
前端此前毫无信号。Worker 主循环存活期间会周期写 Redis 心跳
（``worker._worker_heartbeat_loop``），本接口聚合心跳 + 队列深度 + 排队任务概览，
供任务详情页/任务中心渲染"Worker 未运行/队列积压"横幅。

Redis 不可用时不抛 500：按"Worker 离线、队列未知"降级返回（``redis_available``
标记为 False），保证故障场景下前端仍能拿到可渲染的状态。
"""

import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from services.api.app.api.v1.auth import AuthUserResponse, require_auth_user
from services.api.app.application.delivery_operations import (
    DeliveryIncidentNotFoundError,
    DeliveryOperationAction,
    DeliveryOperationConflictError,
    DeliveryOperationService,
    DeliveryPlatform,
)
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.db.delivery_operation_repository import (
    PostgresDeliveryOperationRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.queue.redis_queue import (
    RedisAgentRunQueue,
    build_redis_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system")


class WorkerStatusResponse(BaseModel):
    """Worker 进程存活状态（基于 Redis 心跳）。"""

    alive: bool
    active_instances: int
    last_heartbeat_at: datetime | None


class QueueStatusResponse(BaseModel):
    """Agent Run 队列状态。"""

    depth: int | None
    redis_available: bool


class QueuedTasksResponse(BaseModel):
    """业务任务表中排队任务的概览。"""

    count: int
    oldest_created_at: datetime | None
    oldest_waiting_seconds: int | None


class SystemStatusResponse(BaseModel):
    """工作台系统状态汇总。"""

    worker: WorkerStatusResponse
    queue: QueueStatusResponse
    queued_tasks: QueuedTasksResponse
    generated_at: datetime


class DeliveryOperationAuditResponse(BaseModel):
    """投递处置的不可变审计行。"""

    model_config = ConfigDict(from_attributes=True)

    operation_id: str
    action: DeliveryOperationAction
    operator_user_id: str
    reason: str
    result: str
    before_state: dict
    after_state: dict
    created_at: datetime


class DeliveryIncidentResponse(BaseModel):
    """H5/App Publication 的统一投递事故投影。"""

    model_config = ConfigDict(from_attributes=True)

    platform: DeliveryPlatform
    publication_id: str
    task_id: str
    status: str
    row_version: int
    finalization_epoch: int
    ruleset_id: str
    ruleset_version: int
    ruleset_snapshot_hash: str | None
    source_identity: dict
    outbox_pending_count: int
    outbox_dead_letter_count: int
    inbox_pending_count: int
    inbox_dead_letter_count: int
    linked_work_item_count: int
    linked_work_active_count: int
    linked_work_failed_count: int
    failure_code: str | None
    failure_message: str | None
    allowed_actions: list[DeliveryOperationAction]
    operations: list[DeliveryOperationAuditResponse]
    updated_at: datetime | None


class DeliveryOperationCommandRequest(BaseModel):
    """携带并发围栏和人工理由的处置请求。"""

    action: DeliveryOperationAction
    reason: str = Field(min_length=1, max_length=500)
    expected_row_version: int = Field(gt=0)


class DeliveryOperationResultResponse(BaseModel):
    """已落不可变审计后的处置结果。"""

    operation_id: str
    action: DeliveryOperationAction
    platform: DeliveryPlatform
    publication_id: str
    task_id: str
    result: str
    affected_counts: dict[str, int]
    incident: DeliveryIncidentResponse


@lru_cache
def _get_agent_run_queue() -> RedisAgentRunQueue:
    settings = get_settings()
    return RedisAgentRunQueue(
        build_redis_client(settings.queue),
        ttl_seconds=settings.queue.run_record_ttl_seconds,
    )


@lru_cache
def _get_task_insight_repository() -> PostgresTaskInsightRepository:
    return PostgresTaskInsightRepository()


@lru_cache
def _get_delivery_operation_service() -> DeliveryOperationService:
    return DeliveryOperationService(PostgresDeliveryOperationRepository())


async def require_system_operations(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    """系统心跳和全局队列属于运维面，只允许明确的系统运维角色读取。"""

    if "systemOperations" not in current_user.permissions:
        raise AliothError(message="无权限读取系统运行状态", status_code=403)
    return current_user


@router.get("/status", response_model=SystemStatusResponse, summary="查询系统运行状态")
async def get_system_status(
    _operator: Annotated[AuthUserResponse, Depends(require_system_operations)],
    queue: Annotated[RedisAgentRunQueue, Depends(_get_agent_run_queue)],
    repository: Annotated[
        PostgresTaskInsightRepository, Depends(_get_task_insight_repository)
    ],
) -> SystemStatusResponse:
    now = datetime.now(UTC)
    redis_available = True
    heartbeats: list[dict] = []
    depth: int | None = None
    try:
        heartbeats = await queue.read_worker_heartbeats()
        depth = await queue.queue_depth()
    except Exception:  # noqa: BLE001 - Redis 故障降级为"离线"而不是 500
        redis_available = False
        logger.warning("系统状态接口读取 Redis 失败。", exc_info=True)

    last_heartbeat_at = max(
        (item["heartbeat_at"] for item in heartbeats), default=None
    )
    overview = await repository.queued_task_overview()
    oldest_created_at = overview.get("oldest_created_at")
    return SystemStatusResponse(
        worker=WorkerStatusResponse(
            alive=bool(heartbeats),
            active_instances=len(heartbeats),
            last_heartbeat_at=last_heartbeat_at,
        ),
        queue=QueueStatusResponse(depth=depth, redis_available=redis_available),
        queued_tasks=QueuedTasksResponse(
            count=int(overview.get("queued_count") or 0),
            oldest_created_at=oldest_created_at,
            oldest_waiting_seconds=(
                max(0, int((now - oldest_created_at).total_seconds()))
                if oldest_created_at is not None
                else None
            ),
        ),
        generated_at=now,
    )


@router.get(
    "/delivery-incidents",
    response_model=list[DeliveryIncidentResponse],
    summary="查询任务 H5/App 规则投递事故",
)
async def list_delivery_incidents(
    task_id: UUID,
    _operator: Annotated[AuthUserResponse, Depends(require_system_operations)],
    service: Annotated[
        DeliveryOperationService, Depends(_get_delivery_operation_service)
    ],
    limit: int = 100,
) -> list[DeliveryIncidentResponse]:
    """仅返回主证据 Publication 及关联投递，不包含补采事件。"""

    try:
        incidents = await service.list_incidents(task_id=str(task_id), limit=limit)
    except ValueError as exc:
        raise AliothError(message=str(exc), status_code=400) from exc
    return [DeliveryIncidentResponse.model_validate(item) for item in incidents]


@router.post(
    "/delivery-incidents/{platform}/{publication_id}/commands",
    response_model=DeliveryOperationResultResponse,
    summary="受控处置 H5/App 规则投递事故",
)
async def execute_delivery_incident_command(
    platform: DeliveryPlatform,
    publication_id: UUID,
    payload: DeliveryOperationCommandRequest,
    operator: Annotated[AuthUserResponse, Depends(require_system_operations)],
    service: Annotated[
        DeliveryOperationService, Depends(_get_delivery_operation_service)
    ],
) -> DeliveryOperationResultResponse:
    """先验证 epoch、规则快照和主证据身份，再对账、重放或转人工。"""

    try:
        result = await service.execute(
            platform=platform,
            publication_id=str(publication_id),
            action=payload.action,
            expected_row_version=payload.expected_row_version,
            operator_user_id=operator.id,
            reason=payload.reason,
        )
    except DeliveryIncidentNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except DeliveryOperationConflictError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    except ValueError as exc:
        raise AliothError(message=str(exc), status_code=400) from exc
    return DeliveryOperationResultResponse(
        operation_id=result.operation_id,
        action=result.action,
        platform=result.platform,
        publication_id=result.publication_id,
        task_id=result.task_id,
        result=result.result,
        affected_counts=result.affected_counts,
        incident=DeliveryIncidentResponse.model_validate(result.incident),
    )
