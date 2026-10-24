"""App 设备池满有界等待（排期第 57 行）。

池满是调度态，不是探索失败：claim 失败后指数退避再试，超时才 DEVICE_WAIT_TIMEOUT。
等待期间禁止 powerOn／install；Worker concurrency 超过槽位只告警，靠等待消化而不是互抢。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from services.api.app.application.device_pool import (
    DeviceLease,
    DevicePoolError,
    claim_device_lease,
    count_cloud_phone_slots,
)

logger = logging.getLogger(__name__)

DEVICE_WAIT_RETRYABLE_CODES = frozenset(
    {
        "DEVICE_POOL_EXHAUSTED",
        "DEVICE_BUSY",
        "DEVICE_LEASE_CLAIM_RACE",
    }
)
_DEFAULT_WAIT_MAX_SECONDS = 600.0
_MAX_BACKOFF_SECONDS = 30.0
_RACE_BACKOFF_CAP_SECONDS = 0.2


@dataclass(frozen=True, kw_only=True)
class DeviceWaitAttempt:
    """一次池满等待的可观测记录。"""

    error_code: str
    delay_seconds: float
    elapsed_seconds: float
    task_id: str
    session_id: str


def resolve_device_wait_max_seconds() -> float:
    """读取等待上限；0 表示关闭等待、立即返回原错误。"""
    raw = (os.getenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS") or str(_DEFAULT_WAIT_MAX_SECONDS)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_WAIT_MAX_SECONDS


def device_wait_backoff_seconds(attempt_index: int, *, error_code: str) -> float:
    """指数退避秒数；fencing 竞态用更短间隔。"""
    attempt = max(0, int(attempt_index))
    if error_code == "DEVICE_LEASE_CLAIM_RACE":
        return min(_RACE_BACKOFF_CAP_SECONDS, 0.05 * (2**attempt))
    return min(_MAX_BACKOFF_SECONDS, 1.0 * (2**attempt))


def describe_worker_device_concurrency(concurrency: int) -> str | None:
    """concurrency 超过 cloud_phone 槽位时返回告警文案，不改并发数。"""
    slots = count_cloud_phone_slots()
    if slots <= 0 or concurrency <= slots:
        return None
    return (
        f"Worker concurrency={concurrency} 超过 cloud_phone 槽位={slots}，"
        "池满将有界等待，禁止互抢开机"
    )


def _log_waiting_for_device(attempt: DeviceWaitAttempt) -> None:
    logger.info(
        "waiting_for_device task_id=%s session_id=%s error_code=%s delay=%.3f elapsed=%.3f",
        attempt.task_id,
        attempt.session_id,
        attempt.error_code,
        attempt.delay_seconds,
        attempt.elapsed_seconds,
    )


async def claim_device_lease_with_wait(
    *,
    task_id: str,
    session_id: str,
    preferred_device_ref: str | None,
    lease_seconds: int,
    lease_owner: str | None = None,
    is_canceled: Callable[[], Awaitable[bool]] | None = None,
    on_waiting: Callable[[DeviceWaitAttempt], None] | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DeviceLease:
    """claim 可重试池满错误时有界等待；超时 DEVICE_WAIT_TIMEOUT 且不可重试。"""
    max_wait = resolve_device_wait_max_seconds()
    started = monotonic()
    attempt_index = 0
    last_error: DevicePoolError | None = None
    notify = on_waiting or _log_waiting_for_device

    while True:
        if is_canceled is not None and await is_canceled():
            raise DevicePoolError(
                "等待设备期间任务已取消",
                error_code="WORKFLOW_CANCELED",
                retryable=False,
            )
        try:
            return await claim_device_lease(
                task_id=task_id,
                session_id=session_id,
                preferred_device_ref=preferred_device_ref,
                lease_seconds=lease_seconds,
                lease_owner=lease_owner,
            )
        except DevicePoolError as exc:
            if exc.error_code not in DEVICE_WAIT_RETRYABLE_CODES or not exc.retryable:
                raise
            last_error = exc
            elapsed = monotonic() - started
            if max_wait <= 0:
                raise
            remaining = max_wait - elapsed
            if remaining <= 0:
                break
            delay = min(
                device_wait_backoff_seconds(attempt_index, error_code=exc.error_code),
                remaining,
            )
            notify(
                DeviceWaitAttempt(
                    error_code=exc.error_code,
                    delay_seconds=delay,
                    elapsed_seconds=elapsed,
                    task_id=task_id,
                    session_id=session_id,
                )
            )
            await sleep(delay)
            attempt_index += 1

    cause = last_error.error_code if last_error is not None else "DEVICE_POOL_EXHAUSTED"
    raise DevicePoolError(
        f"等待设备超时（{cause}），未领取租约，未开机安装",
        error_code="DEVICE_WAIT_TIMEOUT",
        retryable=False,
    ) from last_error
