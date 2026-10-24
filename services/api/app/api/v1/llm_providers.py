"""AI 服务与供应商管理接口（2.1 原型「AI 服务管理 provider-management.html」）。

页面只登记供应商名称、地址、模型、主备优先级、权重和熔断条件，不存密钥。当前真正生效的
供应商由环境变量（ALIOTH_LLM_*）决定，每次读取时同步进登记表并标记为「环境配置」；
运行指标来自模型用量事件（近 24 小时调用数、失败数），连接测试实时探测供应商地址。

「设为主供应商」和「自动故障转移」只改登记与审计记录：执行侧（worker）目前仍按环境变量
调用模型，接入路由读取之前不会真的切流量，接口返回里会把这一点写明。
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import AuthUserResponse, require_auth_user
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.db.llm_provider_repository import PostgresLlmProviderRepository

router = APIRouter()

SETTING_ROUTING = "llm_routing"
SETTING_ALERTS = "llm_alert_policy"
DEFAULT_BREAKER_RULE = "连续 3 次失败或错误率 > 5%"
ROUTING_NOTE = "切换只改登记与审计记录；执行侧仍按环境变量配置调用模型，接入路由读取后才会真正切流量。"

ProviderStatus = Literal["active", "standby", "restricted", "disabled"]
HealthKey = Literal["healthy", "warning", "down", "unknown"]


class ProviderPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    priority: int = Field(default=1, ge=1, le=99)
    weight: int = Field(default=0, ge=0, le=100)
    breaker_rule: str | None = Field(default=None, max_length=255)
    status: ProviderStatus = "standby"
    quota_remaining_percent: float | None = Field(default=None, ge=0, le=100)


class ProviderMetrics(BaseModel):
    call_count: int = 0
    failed_count: int = 0
    error_rate: float | None = None
    total_tokens: int = 0
    last_call_at: datetime | None = None


class ProviderResponse(BaseModel):
    id: str
    code: str
    name: str
    base_url: str | None = None
    model: str | None = None
    priority: int = 1
    weight: int = 0
    breaker_rule: str | None = None
    status: str
    is_primary: bool = False
    api_key_configured: bool = False
    managed_by_env: bool = False
    quota_remaining_percent: float | None = None
    health: HealthKey = "unknown"
    health_label: str = "未观测"
    last_check_status: str | None = None
    last_check_latency_ms: int | None = None
    last_check_message: str | None = None
    last_checked_at: datetime | None = None
    metrics: ProviderMetrics = Field(default_factory=ProviderMetrics)
    created_at: datetime
    updated_at: datetime


class ProviderEventResponse(BaseModel):
    id: str
    provider_id: str | None = None
    provider_name: str
    event_type: str
    description: str
    action: str | None = None
    result: str | None = None
    operator_name: str | None = None
    occurred_at: datetime


class RoutingSettings(BaseModel):
    auto_failover: bool = False


class AlertPolicy(BaseModel):
    service_down: bool = True
    quota_low: bool = True
    error_rate: bool = True
    latency: bool = True


class ProviderSettingsPayload(BaseModel):
    routing: RoutingSettings
    alert_policy: AlertPolicy


class ProviderSettingsResponse(BaseModel):
    routing: RoutingSettings
    alert_policy: AlertPolicy
    effective_provider_code: str | None = None
    note: str = ROUTING_NOTE


class ProvidersOverviewResponse(BaseModel):
    monitor_status: Literal["normal", "warning", "down"]
    monitor_label: str
    generated_at: datetime
    providers: list[ProviderResponse]
    settings: ProviderSettingsResponse
    warnings: list[str]
    events: list[ProviderEventResponse]


class ProviderEventListResponse(BaseModel):
    items: list[ProviderEventResponse]


class ConnectionTestResponse(BaseModel):
    provider: ProviderResponse
    status: str
    latency_ms: int | None = None
    message: str


@lru_cache
def _get_provider_repository() -> PostgresLlmProviderRepository:
    return PostgresLlmProviderRepository()


async def _require_provider_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    if "systemOperations" not in current_user.permissions:
        raise AliothError(message="无权限访问该资源", status_code=403)
    return current_user


def _env_provider_payload() -> dict[str, object] | None:
    """环境变量里当前生效的供应商；没配置时返回 None。"""
    provider = (os.getenv("ALIOTH_LLM_PROVIDER") or "").strip()
    base_url = (os.getenv("ALIOTH_LLM_BASE_URL") or "").strip() or None
    model = (os.getenv("ALIOTH_LLM_MODEL") or "").strip() or None
    if not provider and not base_url and not model:
        return None
    code = f"env:{provider or 'default'}"
    host = urlparse(base_url).hostname if base_url else None
    name = f"{provider or '默认'} · {host}" if host else (provider or "默认供应商")
    return {
        "code": code,
        "name": name,
        "base_url": base_url,
        "model": model,
        "breaker_rule": DEFAULT_BREAKER_RULE,
        "api_key_configured": bool((os.getenv("ALIOTH_LLM_API_KEY") or "").strip()),
    }


def _usage_key(provider: dict[str, object]) -> str:
    code = str(provider.get("code") or "")
    return code[4:] if code.startswith("env:") else code


def _metrics(provider: dict[str, object], usage: dict[str, dict[str, object]]) -> ProviderMetrics:
    row = usage.get(_usage_key(provider))
    if not row:
        return ProviderMetrics()
    call_count = int(row.get("call_count") or 0)
    failed_count = int(row.get("failed_count") or 0)
    return ProviderMetrics(
        call_count=call_count,
        failed_count=failed_count,
        error_rate=(round(failed_count / call_count * 100, 1) if call_count else None),
        total_tokens=int(row.get("total_tokens") or 0),
        last_call_at=row.get("last_call_at"),  # type: ignore[arg-type]
    )


def _health(provider: dict[str, object], metrics: ProviderMetrics) -> tuple[HealthKey, str]:
    if provider.get("status") == "disabled":
        return "unknown", "已停用"
    if provider.get("last_check_status") == "error":
        return "down", "连接失败"
    quota = provider.get("quota_remaining_percent")
    if quota is not None and float(quota) < 20:
        return "warning", "额度预警"
    if metrics.error_rate is not None and metrics.error_rate > 5:
        return "warning", "错误率偏高"
    if metrics.call_count == 0 and not provider.get("last_check_status"):
        return "unknown", "未观测"
    return "healthy", "健康"


def _to_response(provider: dict[str, object], usage: dict[str, dict[str, object]]) -> ProviderResponse:
    metrics = _metrics(provider, usage)
    health, label = _health(provider, metrics)
    return ProviderResponse(
        id=str(provider["id"]),
        code=str(provider["code"]),
        name=str(provider["name"]),
        base_url=(str(provider["base_url"]) if provider.get("base_url") else None),
        model=(str(provider["model"]) if provider.get("model") else None),
        priority=int(provider.get("priority") or 1),
        weight=int(provider.get("weight") or 0),
        breaker_rule=(str(provider["breaker_rule"]) if provider.get("breaker_rule") else None),
        status=str(provider.get("status") or "standby"),
        is_primary=bool(provider.get("is_primary")),
        api_key_configured=bool(provider.get("api_key_configured")),
        managed_by_env=bool(provider.get("managed_by_env")),
        quota_remaining_percent=(
            float(provider["quota_remaining_percent"])  # type: ignore[arg-type]
            if provider.get("quota_remaining_percent") is not None
            else None
        ),
        health=health,
        health_label=label,
        last_check_status=(str(provider["last_check_status"]) if provider.get("last_check_status") else None),
        last_check_latency_ms=(
            int(provider["last_check_latency_ms"])  # type: ignore[arg-type]
            if provider.get("last_check_latency_ms") is not None
            else None
        ),
        last_check_message=(str(provider["last_check_message"]) if provider.get("last_check_message") else None),
        last_checked_at=provider.get("last_checked_at"),  # type: ignore[arg-type]
        metrics=metrics,
        created_at=provider["created_at"],  # type: ignore[arg-type]
        updated_at=provider["updated_at"],  # type: ignore[arg-type]
    )


async def _settings(repository: PostgresLlmProviderRepository, effective_code: str | None) -> ProviderSettingsResponse:
    routing = await repository.get_setting(SETTING_ROUTING) or {}
    alerts = await repository.get_setting(SETTING_ALERTS) or {}
    return ProviderSettingsResponse(
        routing=RoutingSettings.model_validate(routing),
        alert_policy=AlertPolicy.model_validate(alerts),
        effective_provider_code=effective_code,
    )


async def _sync_env_provider(repository: PostgresLlmProviderRepository) -> str | None:
    payload = _env_provider_payload()
    if payload is None:
        return None
    row = await repository.sync_env_provider(payload)
    return str(row.get("code") or payload["code"])


@router.get("/llm-providers", response_model=ProvidersOverviewResponse, summary="AI 服务与供应商总览")
async def get_providers_overview(
    _: Annotated[AuthUserResponse, Depends(_require_provider_permission)],
    repository: Annotated[PostgresLlmProviderRepository, Depends(_get_provider_repository)],
) -> ProvidersOverviewResponse:
    effective_code = await _sync_env_provider(repository)
    rows = await repository.list_providers()
    usage = await repository.usage_metrics(hours=24)
    providers = [_to_response(row, usage) for row in rows]
    warnings: list[str] = []
    monitor: Literal["normal", "warning", "down"] = "normal"
    for provider in providers:
        if provider.health == "down":
            warnings.append(f"{provider.name} 连接失败：{provider.last_check_message or '最近一次连接测试未通过'}")
            if provider.is_primary:
                monitor = "down"
            elif monitor == "normal":
                monitor = "warning"
        elif provider.health == "warning":
            detail = (
                f"剩余额度 {provider.quota_remaining_percent:.0f}%"
                if provider.health_label == "额度预警" and provider.quota_remaining_percent is not None
                else f"近 24 小时错误率 {provider.metrics.error_rate}%"
            )
            warnings.append(f"{provider.name} {provider.health_label}：{detail}")
            if monitor == "normal":
                monitor = "warning"
    labels = {"normal": "监控正常", "warning": "有预警", "down": "主供应商异常"}
    events = await repository.list_events(limit=10)
    return ProvidersOverviewResponse(
        monitor_status=monitor,
        monitor_label=labels[monitor],
        generated_at=datetime.now(UTC),
        providers=providers,
        settings=await _settings(repository, effective_code),
        warnings=warnings,
        events=[ProviderEventResponse.model_validate(item) for item in events],
    )


@router.get("/llm-providers/events", response_model=ProviderEventListResponse, summary="供应商服务事件")
async def list_provider_events(
    _: Annotated[AuthUserResponse, Depends(_require_provider_permission)],
    repository: Annotated[PostgresLlmProviderRepository, Depends(_get_provider_repository)],
    limit: int = 50,
) -> ProviderEventListResponse:
    rows = await repository.list_events(limit=min(max(limit, 1), 200))
    return ProviderEventListResponse(items=[ProviderEventResponse.model_validate(item) for item in rows])


@router.put("/llm-providers/settings", response_model=ProviderSettingsResponse, summary="更新路由与预警设置")
async def update_provider_settings(
    payload: ProviderSettingsPayload,
    current_user: Annotated[AuthUserResponse, Depends(_require_provider_permission)],
    repository: Annotated[PostgresLlmProviderRepository, Depends(_get_provider_repository)],
) -> ProviderSettingsResponse:
    operator = current_user.full_name or current_user.username
    await repository.set_setting(SETTING_ROUTING, payload.routing.model_dump(), updated_by=operator)
    await repository.set_setting(SETTING_ALERTS, payload.alert_policy.model_dump(), updated_by=operator)
    await repository.record_event(
        {
            "provider_id": None,
            "provider_name": "平台",
            "event_type": "settings",
            "description": "更新路由与预警设置",
            "action": (
                f"自动故障转移 {'开' if payload.routing.auto_failover else '关'}；"
                f"预警项 {sum(payload.alert_policy.model_dump().values())} 个已启用"
            ),
            "result": "已保存",
            "operator_name": operator,
        }
    )
    effective = _env_provider_payload()
    return await _settings(repository, str(effective["code"]) if effective else None)


@router.post("/llm-providers", response_model=ProviderResponse, status_code=201, summary="新增供应商")
async def create_provider(
    payload: ProviderPayload,
    current_user: Annotated[AuthUserResponse, Depends(_require_provider_permission)],
    repository: Annotated[PostgresLlmProviderRepository, Depends(_get_provider_repository)],
) -> ProviderResponse:
    name = payload.name.strip()
    if not name:
        raise AliothError(message="供应商名称不能为空")
    row = await repository.create_provider(
        {
            "code": f"manual:{secrets.token_hex(4)}",
            "name": name,
            "base_url": (payload.base_url or "").strip() or None,
            "model": (payload.model or "").strip() or None,
            "priority": payload.priority,
            "weight": payload.weight,
            "breaker_rule": (payload.breaker_rule or "").strip() or DEFAULT_BREAKER_RULE,
            "status": payload.status,
            "is_primary": False,
            "api_key_configured": False,
            "managed_by_env": False,
            "quota_remaining_percent": payload.quota_remaining_percent,
        }
    )
    await repository.record_event(
        {
            "provider_id": str(row["id"]),
            "provider_name": name,
            "event_type": "created",
            "description": "新增供应商登记",
            "action": f"备用优先级 {payload.priority}，路由权重 {payload.weight}%",
            "result": "待接入",
            "operator_name": current_user.full_name or current_user.username,
        }
    )
    return _to_response(row, {})


@router.put("/llm-providers/{provider_id}", response_model=ProviderResponse, summary="更新供应商")
async def update_provider(
    provider_id: str,
    payload: ProviderPayload,
    _: Annotated[AuthUserResponse, Depends(_require_provider_permission)],
    repository: Annotated[PostgresLlmProviderRepository, Depends(_get_provider_repository)],
) -> ProviderResponse:
    existing = await repository.find_by_id(provider_id)
    if existing is None:
        raise AliothError(message=f"未找到 provider_id={provider_id} 对应的供应商", status_code=404)
    base_url = (payload.base_url or "").strip() or None
    model = (payload.model or "").strip() or None
    if existing.get("managed_by_env"):
        # 环境变量维护的供应商：地址和模型以环境变量为准，页面只能改优先级、权重、熔断和状态。
        base_url = existing.get("base_url")  # type: ignore[assignment]
        model = existing.get("model")  # type: ignore[assignment]
    row = await repository.update_provider(
        provider_id,
        {
            "name": payload.name.strip() or str(existing["name"]),
            "base_url": base_url,
            "model": model,
            "priority": payload.priority,
            "weight": payload.weight,
            "breaker_rule": (payload.breaker_rule or "").strip() or DEFAULT_BREAKER_RULE,
            "status": payload.status,
            "quota_remaining_percent": payload.quota_remaining_percent,
        },
    )
    if row is None:
        raise AliothError(message=f"未找到 provider_id={provider_id} 对应的供应商", status_code=404)
    usage = await repository.usage_metrics(hours=24)
    return _to_response(row, usage)


@router.post("/llm-providers/{provider_id}/primary", response_model=ProviderResponse, summary="设为主供应商")
async def set_primary_provider(
    provider_id: str,
    current_user: Annotated[AuthUserResponse, Depends(_require_provider_permission)],
    repository: Annotated[PostgresLlmProviderRepository, Depends(_get_provider_repository)],
) -> ProviderResponse:
    target = await repository.find_by_id(provider_id)
    if target is None:
        raise AliothError(message=f"未找到 provider_id={provider_id} 对应的供应商", status_code=404)
    if target.get("status") == "disabled":
        raise AliothError(message="已停用的供应商不能设为主供应商", status_code=409)
    previous = next((row for row in await repository.list_providers() if row.get("is_primary")), None)
    row = await repository.set_primary(provider_id)
    if row is None:
        raise AliothError(message=f"未找到 provider_id={provider_id} 对应的供应商", status_code=404)
    await repository.record_event(
        {
            "provider_id": str(row["id"]),
            "provider_name": str(row["name"]),
            "event_type": "switch",
            "description": "人工切换主供应商",
            "action": f"由 {previous['name'] if previous else '无'} 切换为 {row['name']}",
            "result": "已记录，执行侧接入路由后生效",
            "operator_name": current_user.full_name or current_user.username,
        }
    )
    usage = await repository.usage_metrics(hours=24)
    return _to_response(row, usage)


async def perform_connection_test(provider: dict[str, object]) -> tuple[str, int | None, str]:
    """探测供应商地址：环境配置的供应商带密钥访问 /models，其它只做可达性探测。"""
    base_url = str(provider.get("base_url") or "").strip()
    if not base_url:
        return "error", None, "未填写服务地址，无法测试"
    url = base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    api_key = (os.getenv("ALIOTH_LLM_API_KEY") or "").strip()
    if provider.get("managed_by_env") and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return "error", latency, f"连接失败：{type(exc).__name__}"
    latency = int((time.perf_counter() - started) * 1000)
    if response.status_code < 300:
        return "ok", latency, f"连接正常（HTTP {response.status_code}）"
    if response.status_code in (401, 403):
        return "auth_required", latency, f"地址可达，但未通过鉴权（HTTP {response.status_code}）"
    return "error", latency, f"服务返回 HTTP {response.status_code}"


@router.post(
    "/llm-providers/{provider_id}/connection-test",
    response_model=ConnectionTestResponse,
    summary="连接测试",
)
async def test_provider_connection(
    provider_id: str,
    current_user: Annotated[AuthUserResponse, Depends(_require_provider_permission)],
    repository: Annotated[PostgresLlmProviderRepository, Depends(_get_provider_repository)],
) -> ConnectionTestResponse:
    provider = await repository.find_by_id(provider_id)
    if provider is None:
        raise AliothError(message=f"未找到 provider_id={provider_id} 对应的供应商", status_code=404)
    status, latency_ms, message = await perform_connection_test(provider)
    row = await repository.record_check(provider_id, status=status, latency_ms=latency_ms, message=message)
    await repository.record_event(
        {
            "provider_id": provider_id,
            "provider_name": str(provider["name"]),
            "event_type": "connection_test",
            "description": "连接测试",
            "action": f"探测 {provider.get('base_url') or '（无地址）'}",
            "result": message,
            "operator_name": current_user.full_name or current_user.username,
        }
    )
    usage = await repository.usage_metrics(hours=24)
    return ConnectionTestResponse(
        provider=_to_response(row or provider, usage),
        status=status,
        latency_ms=latency_ms,
        message=message,
    )
