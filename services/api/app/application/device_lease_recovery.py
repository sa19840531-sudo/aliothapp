"""App 设备租约过期回收：recovering 后按 Session 终态释放或隔离。

PostgreSQL 设备池在跨 Worker 场景下，过期租约不得直接变回 available。
本模块把「标记 recovering」「终态 Session 腾槽」「非终态超时隔离」收口成一次对账。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from services.api.app.application.device_pool import (
    DeviceLeasePool,
    _await_if_needed,
    get_runtime_device_pool,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class DeviceLeaseRecoverySummary:
    """一次设备租约对账的计数；全零表示库存健康。"""

    expired_recovered: int = 0
    terminal_reclaimed: int = 0
    stale_quarantined: int = 0
    finished_released: int = 0

    @property
    def changed(self) -> bool:
        return (
            self.expired_recovered > 0
            or self.terminal_reclaimed > 0
            or self.stale_quarantined > 0
            or self.finished_released > 0
        )


async def recover_device_leases_once(
    *,
    pool: DeviceLeasePool | None = None,
    grace_seconds: int = 60,
    limit: int = 100,
) -> DeviceLeaseRecoverySummary:
    """执行一轮过期回收；缺少对应方法的进程内池视为零变更。"""
    if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, int) or grace_seconds < 1:
        raise ValueError("grace_seconds 必须为正整数")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit 必须为正整数")
    runtime = pool if pool is not None else await get_runtime_device_pool()
    expired = await _call_optional(runtime, "recover_expired", limit=limit)
    reclaimed = await _call_optional(runtime, "reclaim_recovering", limit=limit)
    quarantined = await _call_optional(
        runtime,
        "quarantine_stale_recovering",
        grace_seconds=grace_seconds,
        limit=limit,
    )
    finished = await _call_optional(runtime, "release_finished_task_devices", limit=limit)
    return DeviceLeaseRecoverySummary(
        expired_recovered=expired,
        terminal_reclaimed=reclaimed,
        stale_quarantined=quarantined,
        finished_released=finished,
    )


async def run_device_lease_recovery_loop(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
    grace_seconds: int = 60,
) -> None:
    """周期扫描过期设备租约；短时数据库失败只延后，不拖垮 Worker。"""
    if not isinstance(interval_seconds, (int, float)) or interval_seconds <= 0:
        raise ValueError("interval_seconds 必须为正数")
    while not stop_event.is_set():
        try:
            summary = await recover_device_leases_once(grace_seconds=grace_seconds)
            if summary.changed:
                logger.info(
                    "App 设备租约过期回收完成：expired=%d reclaimed=%d quarantined=%d finished=%d",
                    summary.expired_recovered,
                    summary.terminal_reclaimed,
                    summary.stale_quarantined,
                    summary.finished_released,
                )
        except Exception:  # noqa: BLE001 - 回收扫描失败不能拖垮已有 Run
            logger.warning("App 设备租约过期回收失败", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval_seconds))
        except TimeoutError:
            continue


async def _call_optional(pool: DeviceLeasePool, method_name: str, **kwargs: Any) -> int:
    method = getattr(pool, method_name, None)
    if not callable(method):
        return 0
    result = await _await_if_needed(method(**kwargs))
    return int(result or 0)
