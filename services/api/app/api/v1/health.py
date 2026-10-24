"""服务存活与真实依赖就绪检查。

``/health`` 只回答 API 进程是否还能响应，保持发布脚本和负载均衡器的向后
兼容；``/readyz``（以及兼容别名 ``/ready``）回答当前实例是否具备接收业务流量
的条件。就绪检查只读探测 PostgreSQL、Redis、证据存储和 Worker 心跳，不返回
连接地址、实例名、异常文本或任何凭证。
"""

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.api.app.core.config import Settings, get_settings
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.queue.redis_queue import (
    RedisAgentRunQueue,
    build_redis_client,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

router = APIRouter()

MIN_EVIDENCE_STORAGE_FREE_BYTES = 1024 * 1024 * 1024


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str


class ReadinessDependency(BaseModel):
    """单个就绪依赖的脱敏状态。"""

    status: Literal["ok", "failed", "skipped"]
    code: str | None = None
    required: bool = True


class ReadinessResponse(BaseModel):
    """真实依赖就绪检查结果。"""

    status: Literal["ready", "not_ready"]
    service: str
    environment: str
    checked_at: datetime
    dependencies: dict[str, ReadinessDependency]


def _required_worker_heartbeat() -> bool:
    """读取部署级 Worker 心跳门禁，默认要求至少一个活跃 Worker。"""

    value = os.getenv("ALIOTH_READINESS_REQUIRE_WORKER_HEARTBEAT", "true")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _worker_heartbeat_max_age_seconds() -> int:
    """解析心跳最大年龄，非法值安全回退到 Redis 心跳 TTL。"""

    raw_value = os.getenv("ALIOTH_READINESS_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 30
    return value if value > 0 else 30


async def _check_postgres() -> ReadinessDependency:
    """只读执行 ``SELECT 1``，确认连接池和数据库连接可用。"""

    try:
        pool = await get_postgres_pool()
        await pool.fetchval("SELECT 1")
    except Exception:  # noqa: BLE001 - 就绪接口必须收敛为结构化失败
        return ReadinessDependency(status="failed", code="postgres_unavailable")
    return ReadinessDependency(status="ok")


async def _check_redis_and_worker(
    settings: Settings | None = None,
) -> tuple[ReadinessDependency, ReadinessDependency]:
    """检查 Redis、Worker 心跳及部署配置要求的全部 Worker 角色。"""

    worker_required = _required_worker_heartbeat()
    resolved_settings = settings or get_settings()
    client = build_redis_client(resolved_settings.queue)
    queue = RedisAgentRunQueue(
        client,
        ttl_seconds=resolved_settings.queue.run_record_ttl_seconds,
    )
    try:
        await client.ping()
        heartbeats = await queue.read_worker_heartbeats()
    except Exception:  # noqa: BLE001 - 不向外暴露 Redis URL 或驱动异常
        redis_status = ReadinessDependency(status="failed", code="redis_unavailable")
        worker_status = ReadinessDependency(
            status="failed" if worker_required else "skipped",
            code="redis_unavailable" if worker_required else "worker_heartbeat_not_required",
            required=worker_required,
        )
        return redis_status, worker_status
    finally:
        # 就绪探针按请求创建轻量 Redis 客户端，避免跨事件循环复用连接池。
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不改变已经完成的只读探测结果
            pass

    redis_status = ReadinessDependency(status="ok")
    if not worker_required:
        return redis_status, ReadinessDependency(
            status="skipped",
            code="worker_heartbeat_not_required",
            required=False,
        )

    now = datetime.now(UTC)
    max_age = _worker_heartbeat_max_age_seconds()
    fresh_workers = [
        item
        for item in heartbeats
        if _is_fresh_heartbeat(
            item.get("heartbeat_at"),
            now=now,
            max_age_seconds=max_age,
        )
    ]
    if not fresh_workers:
        return redis_status, ReadinessDependency(
            status="failed",
            code="worker_heartbeat_missing",
            required=True,
        )
    active_roles = {
        str(role)
        for item in fresh_workers
        for role in (item.get("roles") if isinstance(item.get("roles"), list) else [])
    }
    required_roles = {str(role) for role in resolved_settings.worker.roles}
    has_required_roles = required_roles.issubset(active_roles)
    return redis_status, ReadinessDependency(
        status="ok" if has_required_roles else "failed",
        code=None if has_required_roles else "worker_role_missing",
        required=True,
    )


def _is_fresh_heartbeat(
    heartbeat_at: object,
    *,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    if not isinstance(heartbeat_at, datetime):
        return False
    normalized = heartbeat_at
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=UTC)
    else:
        normalized = normalized.astimezone(UTC)
    age_seconds = (now - normalized).total_seconds()
    return 0 <= age_seconds <= max_age_seconds


async def _check_evidence_storage(settings: Settings) -> ReadinessDependency:
    """检查证据存储是否具备当前进程所需的真实读写能力。"""

    storage_settings = settings.evidence_storage
    if storage_settings.backend == "local":
        root = Path(storage_settings.local_root).expanduser().resolve()
        if not root.is_dir():
            return ReadinessDependency(status="failed", code="evidence_root_missing")
        if not os.access(root, os.R_OK | os.X_OK):
            return ReadinessDependency(status="failed", code="evidence_root_unreadable")
        if not os.access(root, os.W_OK):
            return ReadinessDependency(status="failed", code="evidence_root_not_writable")
        failure_code = await asyncio.to_thread(_probe_local_evidence_storage, root)
        if failure_code is not None:
            return ReadinessDependency(status="failed", code=failure_code)
        return ReadinessDependency(status="ok")

    try:
        store = build_evidence_artifact_store(storage_settings)
        readiness_probe = getattr(store, "check_ready", None)
        if not callable(readiness_probe):
            return ReadinessDependency(status="failed", code="evidence_store_invalid")
        exists = await readiness_probe()
    except Exception:  # noqa: BLE001 - 统一脱敏为对象存储不可用
        return ReadinessDependency(status="failed", code="evidence_store_unavailable")
    if not exists:
        return ReadinessDependency(status="failed", code="evidence_bucket_missing")
    return ReadinessDependency(status="ok")


def _probe_local_evidence_storage(root: Path) -> str | None:
    """以真实刷盘验证本地证据目录，避免权限检查掩盖满盘或卷故障。"""

    try:
        if shutil.disk_usage(root).free < MIN_EVIDENCE_STORAGE_FREE_BYTES:
            return "evidence_root_insufficient_space"
    except OSError:
        return "evidence_root_io_error"

    probe_path = root / f".alioth-readiness-{uuid4().hex}.tmp"
    try:
        with probe_path.open("xb") as stream:
            stream.write(b"alioth-evidence-readiness\n")
            stream.flush()
            os.fsync(stream.fileno())
        probe_path.unlink()
    except OSError:
        try:
            probe_path.unlink()
        except OSError:
            pass
        return "evidence_root_io_error"
    return None


async def _collect_readiness(settings: Settings) -> ReadinessResponse:
    """并行收集所有依赖状态，任何必需项失败都拒绝流量。"""

    postgres_status, (redis_status, worker_status), storage_status = await asyncio.gather(
        _check_postgres(),
        _check_redis_and_worker(settings),
        _check_evidence_storage(settings),
    )
    dependencies = {
        "postgres": postgres_status,
        "redis": redis_status,
        "evidence_storage": storage_status,
        "worker": worker_status,
    }
    ready = all(
        dependency.status in {"ok", "skipped"}
        for dependency in dependencies.values()
        if dependency.required
    )
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        service=settings.app_name,
        environment=settings.environment,
        checked_at=datetime.now(UTC),
        dependencies=dependencies,
    )


async def _get_readiness_report(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadinessResponse:
    return await _collect_readiness(settings)


@router.get("/health", response_model=HealthResponse, summary="服务存活检查")
async def read_health(
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    # 使用 async，避免事件循环被其它同步请求占满线程池时连探活也排队假死。
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
    )


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="服务真实依赖就绪检查",
)
@router.get(
    "/ready",
    response_model=ReadinessResponse,
    include_in_schema=False,
)
async def read_readiness(
    report: Annotated[ReadinessResponse, Depends(_get_readiness_report)],
) -> ReadinessResponse | JSONResponse:
    # 503 是流量门禁信号；响应体保留每个依赖的脱敏失败码，便于发布脚本定位。
    if report.status == "not_ready":
        return JSONResponse(status_code=503, content=report.model_dump(mode="json"))
    return report
