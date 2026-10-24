"""Agent 调试接口：手动触发一次 Main Agent 运行，用于本地/联调阶段验证核心执行链路。

出于安全考虑，本调试接口固定只允许调用 `safe` 权限级别的 Tool（通过
``PermissionEnforcementMiddleware`` 强制），`sensitive` 级别的 Tool（如写入结论、
支付/登录/下载类操作）不应通过该调试入口触发。

本文件同时提供异步提交/查询/取消三个端点（``/agents/runs*``），基于 Redis 队列
（[infrastructure/queue](../../infrastructure/queue/redis_queue.py)）与独立的
[worker.py](../../worker.py) 进程配合：提交端点只入队并立即返回 ``run_id``，
真正的 Agent Loop 执行由 Worker 进程消费队列后异步完成，调用方需轮询查询端点
获取结果。与同步的 ``/agents/debug-run`` 相互独立，互不影响，各自适用不同场景
（前者用于本地快速调试，无需启动 Worker 进程；后者用于验证真实的异步任务链路）。
"""

import logging
from datetime import datetime
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from agent_framework import Agent
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from services.api.app.agents.bootstrap import build_default_main_agent
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.agent_factory import run_main_agent
from services.api.app.agents.runtime.usage_meter import bind_llm_usage_scope
from services.api.app.agents.tools.permissions import PermissionEnforcementMiddleware
from services.api.app.agents.tools.registry import PermissionLevel
from services.api.app.api.v1.auth import (
    AuthUserResponse,
    require_access_scope,
    require_agent_permission,
)
from services.api.app.api.v1.object_access import _get_object_access_repository
from services.api.app.core.config import Settings, get_settings
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.object_access_repository import (
    ObjectAccessAction,
    PostgresObjectAccessRepository,
)
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
    RedisAgentRunQueue,
    build_redis_client,
)
from services.api.app.infrastructure.queue.state_store import (
    AgentRunStateStore,
    build_agent_run_state_store,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class DebugRunRequest(BaseModel):
    """运行请求体：下发给 Main Agent 的任务描述/指令。"""

    message: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="下发给 Main Agent 的任务描述/指令",
    )


class ActionTraceEntryResponse(BaseModel):
    """单次工具调用尝试的留痕记录（对外展示形态）。"""

    step: int
    tool_name: str
    arguments: str
    status: str
    duration_seconds: float | None = None
    error_message: str | None = None
    retry_count: int = 0


class DebugRunResponse(BaseModel):
    """调试运行响应体。"""

    termination_reason: str
    final_text: str | None
    step_count: int
    action_trace: list[ActionTraceEntryResponse]
    error_message: str | None = None
    error_category: str | None = None
    usage: dict[str, object] | None = None
    working_memory: dict


class SubmitRunResponse(BaseModel):
    """提交异步 Agent Run 后的响应体：仅返回 run_id 与初始状态，实际结果需轮询查询接口。"""

    run_id: str
    status: str


class RunStatusResponse(BaseModel):
    """异步 Agent Run 的状态/结果查询响应体。"""

    run_id: str
    status: str
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    termination_reason: str | None
    final_text: str | None
    step_count: int | None
    error_message: str | None
    error_category: str | None
    usage: dict[str, object] | None
    action_trace: list[ActionTraceEntryResponse]
    working_memory: dict


class CancelRunResponse(BaseModel):
    """请求取消一次异步 Agent Run 的响应体。"""

    run_id: str
    cancel_requested: bool


@lru_cache
def _get_agent_run_queue() -> RedisAgentRunQueue:
    """构建 Redis 队列客户端单例；进程内复用同一个连接池。"""
    settings = get_settings()
    client = build_redis_client(settings.queue)
    return RedisAgentRunQueue(client, ttl_seconds=settings.queue.run_record_ttl_seconds)


@lru_cache
def _get_agent_run_state_store() -> AgentRunStateStore:
    """构建长期状态 Store 单例；按配置选择 no-op 或 PostgreSQL 实现。"""
    return build_agent_run_state_store()


@router.post(
    "/agents/debug-run",
    response_model=DebugRunResponse,
    summary="手动触发一次 Main Agent 运行（调试用，同步等待结果）",
)
async def debug_run_agent(
    payload: DebugRunRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    current_user: Annotated[AuthUserResponse, Depends(require_agent_permission)],
) -> DebugRunResponse:
    agent: Agent = build_default_main_agent()
    sink = InMemoryActionTraceSink()
    permission_middleware = PermissionEnforcementMiddleware(
        allowed_permission_levels=(PermissionLevel.SAFE,)
    )

    with bind_llm_usage_scope(scope_type="debug", source="api_debug_run"):
        result = await run_main_agent(
            agent,
            payload.message,
            runtime_settings=settings.agent_runtime,
            sink=sink,
            extra_middleware=[permission_middleware],
        )
    logger.info(
        "Agent 同步调试运行完成，actor_user_id=%s termination_reason=%s。",
        current_user.id,
        result.termination_reason.value,
    )

    return DebugRunResponse(
        termination_reason=result.termination_reason.value,
        final_text=result.final_text,
        step_count=result.working_memory.step_count,
        error_message=result.error_message,
        error_category=result.error_category.value if result.error_category else None,
        usage=(
            {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cache_hit_tokens": result.usage.cache_hit_tokens,
                "billable_input_tokens": result.usage.billable_input_tokens,
                "total_tokens": result.usage.total_tokens,
                "ai_calls": result.usage.ai_calls,
                "input_cost": result.usage.input_cost,
                "output_cost": result.usage.output_cost,
                "cache_hit_cost": result.usage.cache_hit_cost,
                "total_cost": result.usage.total_cost,
                "cost_currency": result.usage.cost_currency,
                "pricing_source": result.usage.pricing_source,
                "pricing_configured": result.usage.pricing_configured,
                "applied_pricing_tiers": list(result.usage.applied_pricing_tiers),
            }
            if result.usage is not None
            else None
        ),
        working_memory=result.working_memory.audit_snapshot(action_trace_count=len(sink.entries)),
        action_trace=[
            ActionTraceEntryResponse(
                step=entry.step,
                tool_name=entry.tool_name,
                arguments=entry.arguments,
                status=entry.status,
                duration_seconds=entry.duration_seconds,
                error_message=entry.error_message,
                retry_count=entry.retry_count,
            )
            for entry in sink.entries
        ],
    )


@router.post(
    "/agents/runs",
    response_model=SubmitRunResponse,
    status_code=202,
    summary="提交一次异步 Main Agent 运行，由后台 Worker 进程消费执行",
)
async def submit_agent_run(
    payload: DebugRunRequest,
    current_user: Annotated[AuthUserResponse, Depends(require_agent_permission)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
) -> SubmitRunResponse:
    queue = _get_agent_run_queue()
    record = await queue.enqueue(
        payload.message,
        submitted_by_user_id=access_scope.actor_user_id,
        tenant_id=access_scope.tenant_id,
    )
    await _get_agent_run_state_store().record_submitted(record)
    logger.info(
        "Agent 异步运行已提交，actor_user_id=%s run_id=%s。",
        current_user.id,
        record.run_id,
    )
    return SubmitRunResponse(run_id=record.run_id, status=record.status.value)


@router.get(
    "/agents/runs/{run_id}",
    response_model=RunStatusResponse,
    summary="查询一次异步 Main Agent 运行的状态/结果",
)
async def get_agent_run(
    run_id: str,
    current_user: Annotated[AuthUserResponse, Depends(require_agent_permission)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    object_access_repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> RunStatusResponse:
    queue = _get_agent_run_queue()
    record = await queue.get(run_id)
    durable_record: AgentRunRecord | None = None
    if record is None:
        if not _is_uuid(run_id):
            raise AliothError(message=f"未找到 run_id={run_id} 对应的运行记录", status_code=404)
        durable_record = await _get_agent_run_state_store().get_record_snapshot(run_id=run_id)
        if durable_record is None:
            raise AliothError(message=f"未找到 run_id={run_id} 对应的运行记录", status_code=404)
        record = durable_record
    elif (
        record.task_id is not None
        and not record.action_trace
        and record.status is not AgentRunQueueStatus.QUEUED
    ):
        durable_record = await _get_agent_run_state_store().get_record_snapshot(run_id=run_id)
        if durable_record is not None and durable_record.action_trace:
            record.action_trace = durable_record.action_trace
            record.working_memory = durable_record.working_memory or record.working_memory
    await _require_run_access(
        record,
        scope=access_scope,
        repository=object_access_repository,
        action=ObjectAccessAction.READ,
    )
    logger.info(
        "Agent 异步运行已查询，actor_user_id=%s run_id=%s。",
        current_user.id,
        run_id,
    )

    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status.value,
        submitted_at=record.submitted_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        termination_reason=record.termination_reason,
        final_text=record.final_text,
        step_count=record.step_count,
        error_message=record.error_message,
        error_category=record.error_category,
        usage=record.usage,
        action_trace=[ActionTraceEntryResponse(**entry) for entry in record.action_trace],
        working_memory=record.working_memory,
    )


@router.post(
    "/agents/runs/{run_id}/cancel",
    response_model=CancelRunResponse,
    summary="请求取消一次尚未结束的异步 Main Agent 运行",
)
async def cancel_agent_run(
    run_id: str,
    current_user: Annotated[AuthUserResponse, Depends(require_agent_permission)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    object_access_repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> CancelRunResponse:
    queue = _get_agent_run_queue()
    record = await queue.get(run_id)
    if record is None:
        raise AliothError(message=f"未找到 run_id={run_id} 对应的运行记录", status_code=404)
    await _require_run_access(
        record,
        scope=access_scope,
        repository=object_access_repository,
        action=ObjectAccessAction.WRITE,
    )
    accepted = await queue.request_cancel(run_id)
    if not accepted:
        raise AliothError(message=f"run_id={run_id} 不存在或已处于终态，无法取消", status_code=404)
    if record.task_id is not None:
        await _get_agent_run_state_store().record_cancel_requested(run_id)
    logger.info(
        "Agent 异步运行取消已接受，actor_user_id=%s run_id=%s。",
        current_user.id,
        run_id,
    )
    return CancelRunResponse(run_id=run_id, cancel_requested=True)


async def _require_run_access(
    record: AgentRunRecord,
    *,
    scope: AccessScope,
    repository: PostgresObjectAccessRepository,
    action: ObjectAccessAction,
) -> None:
    """校验异步 Run 归属；拒绝、未知和失效对象统一返回 404。"""

    if record.task_id is not None:
        allowed = await repository.can_access_task(
            task_id=record.task_id,
            scope=scope,
            action=action,
        )
    else:
        allowed = _direct_run_access_allowed(record, scope=scope, action=action)
    if not allowed:
        raise AliothError(message="未找到请求的资源", status_code=404)


def _direct_run_access_allowed(
    record: AgentRunRecord,
    *,
    scope: AccessScope,
    action: ObjectAccessAction,
) -> bool:
    """直接调试 Run 只按提交者或跨主体 any 权限裁决。

    旧记录可能没有 submitted_by_user_id 或 tenant_id；任一归属字段缺失时不能猜测
    owner/tenant，仅允许显式跨主体权限访问。
    """

    owner_id = str(getattr(record, "submitted_by_user_id", "") or "").strip()
    tenant_id = str(getattr(record, "tenant_id", "") or "").strip()
    if not owner_id or not tenant_id:
        return scope.can_read_any if action is ObjectAccessAction.READ else scope.can_write_any
    if owner_id == scope.actor_user_id and tenant_id == scope.tenant_id:
        return True
    return scope.can_read_any if action is ObjectAccessAction.READ else scope.can_write_any


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
