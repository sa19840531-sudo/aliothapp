"""远程 ADB 长会话健康分与预防性端点刷新（换通道）。

换通道 = MoreLogin ensure_ready + reopen + 重建 AdbDeviceOps；
不回切 exec-out（cloud_phone 已固定 remote_pull）。
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps, open_adb_device_ops
from services.api.app.infrastructure.device.backend import BACKEND_CLOUD_PHONE

_LOGGER = logging.getLogger(__name__)

APP_ADB_SESSION_HEALTH_VERSION = "app-adb-session-health.v1"

# Capture 刷新不得占用云手机 boot 的长预算。
_CAPTURE_REFRESH_READY_TIMEOUT_SECONDS = 25.0

# 动作／UI dump：公网 ADB 瞬断后有界重连再试（与截图帧内重试同量级）
_ACTION_ADB_RECONNECT_RETRIES = 2
_ACTION_ADB_RECONNECT_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0)
_ADB_TRANSIENT_TRANSPORT_MARKERS: tuple[str, ...] = (
    "device offline",
    "device not found",
    "no devices/emulators",
    "no devices",
    "connection reset",
    "connection refused",
    "error: closed",
    "broken pipe",
    "timed out",
    "time out",
    "timeout expired",
    "protocol fault",
    "unauthorized",
    "adb.exe: failed",
    "cannot connect",
    "unable to connect",
    "remote object '",
    "device still authorizing",
)

_AGE_REFRESH_SECONDS = 480.0
_SLOW_PULL_ABSOLUTE_MS = 12_000
_SLOW_PULL_BASELINE_FACTOR = 2.0
_LOW_SCORE_THRESHOLD = 50
_REFRESH_COOLDOWN_SECONDS = 60.0
_MAX_PROACTIVE_REFRESHES = 12
_BASELINE_SAMPLE_SIZE = 4
_RECENT_PULL_WINDOW = 8
_SCORE_SLOW_PENALTY = 15
_SCORE_VERY_SLOW_PENALTY = 30
_SCORE_HEALTHY_REWARD = 5


def _percentile_50(values: list[int]) -> int:
    if not values:
        raise ValueError("values 不能为空")
    ordered = sorted(values)
    return int(statistics.median(ordered))


@dataclass
class AdbSessionHealthState:
    """公网 ADB 长会话健康状态（仅 cloud_phone 路径使用）。"""

    score: int = 100
    baseline_pull_ms: int | None = None
    recent_pull_ms: list[int] = field(default_factory=list)
    _baseline_samples: list[int] = field(default_factory=list, repr=False)
    session_started_at: float = field(default_factory=time.monotonic)
    last_refresh_at: float = field(default_factory=time.monotonic)
    refresh_count: int = 0
    pending_refresh_reason: str | None = None
    last_decision_reason: str | None = None
    last_refreshed: bool = False
    last_recorded_pull_ms: int | None = None

    def slow_pull_threshold_ms(self) -> int:
        baseline = self.baseline_pull_ms
        if baseline is None or baseline <= 0:
            return _SLOW_PULL_ABSOLUTE_MS
        return max(_SLOW_PULL_ABSOLUTE_MS, int(baseline * _SLOW_PULL_BASELINE_FACTOR))

    def record_pull(self, pull_ms: int | None, *, now: float | None = None) -> None:
        """根据本次 adb_pull_ms 更新基线、分数与下一帧刷新标记。"""

        del now  # 预留时钟注入；当前分数更新不依赖墙钟
        self.last_recorded_pull_ms = None
        if pull_ms is None or pull_ms < 0:
            return
        pull = int(pull_ms)
        self.last_recorded_pull_ms = pull
        self.recent_pull_ms.append(pull)
        if len(self.recent_pull_ms) > _RECENT_PULL_WINDOW:
            self.recent_pull_ms = self.recent_pull_ms[-_RECENT_PULL_WINDOW:]

        if self.baseline_pull_ms is None:
            self._baseline_samples.append(pull)
            if len(self._baseline_samples) >= _BASELINE_SAMPLE_SIZE:
                self.baseline_pull_ms = _percentile_50(self._baseline_samples)

        threshold = self.slow_pull_threshold_ms()
        if pull >= threshold * 2 and self.baseline_pull_ms is not None:
            self.score = max(0, self.score - _SCORE_VERY_SLOW_PENALTY)
            self.pending_refresh_reason = "slow_pull"
        elif pull >= threshold:
            self.score = max(0, self.score - _SCORE_SLOW_PENALTY)
            self.pending_refresh_reason = "slow_pull"
        else:
            self.score = min(100, self.score + _SCORE_HEALTHY_REWARD)

        if self.score < _LOW_SCORE_THRESHOLD and self.pending_refresh_reason is None:
            self.pending_refresh_reason = "low_score"

    def should_proactive_refresh(self, *, now: float | None = None) -> tuple[bool, str | None]:
        """捕获前判定是否应预防性换通道。"""

        clock = time.monotonic() if now is None else float(now)
        if self.refresh_count >= _MAX_PROACTIVE_REFRESHES:
            self.last_decision_reason = "max_refreshes"
            return False, None

        since_refresh = clock - self.last_refresh_at
        if since_refresh < _REFRESH_COOLDOWN_SECONDS:
            self.last_decision_reason = "cooldown"
            return False, None

        if self.pending_refresh_reason:
            self.last_decision_reason = self.pending_refresh_reason
            return True, self.pending_refresh_reason

        if since_refresh >= _AGE_REFRESH_SECONDS:
            self.last_decision_reason = "age"
            return True, "age"

        if self.score < _LOW_SCORE_THRESHOLD:
            self.last_decision_reason = "low_score"
            return True, "low_score"

        self.last_decision_reason = None
        return False, None

    def mark_refreshed(self, *, reason: str, now: float | None = None) -> None:
        """预防性或失败后刷新成功时记账，并重置 pull 基线。"""

        clock = time.monotonic() if now is None else float(now)
        self.last_refresh_at = clock
        self.refresh_count += 1
        self.pending_refresh_reason = None
        self.last_refreshed = True
        self.last_decision_reason = reason
        self.baseline_pull_ms = None
        self._baseline_samples = []
        self.recent_pull_ms = []
        self.score = min(100, max(self.score, 70) + 10)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": APP_ADB_SESSION_HEALTH_VERSION,
            "score": int(self.score),
            "baseline_pull_ms": self.baseline_pull_ms,
            "recent_pull_ms": list(self.recent_pull_ms),
            "refresh_count": int(self.refresh_count),
            "pending_refresh_reason": self.pending_refresh_reason,
            "last_decision_reason": self.last_decision_reason,
            "refreshed": bool(self.last_refreshed),
            "last_recorded_pull_ms": self.last_recorded_pull_ms,
            "slow_pull_threshold_ms": self.slow_pull_threshold_ms(),
            "session_age_ms": int(max(0.0, (time.monotonic() - self.session_started_at) * 1000)),
            "since_refresh_ms": int(max(0.0, (time.monotonic() - self.last_refresh_at) * 1000)),
        }


def is_transient_adb_transport_error(exc: BaseException) -> bool:
    """识别公网 ADB／设备通道瞬断，便于动作路径有界重连。"""

    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError, OSError)):
        return True
    # subprocess.TimeoutExpired 不总是 TimeoutError 子类
    if type(exc).__name__ in {"TimeoutExpired", "CalledProcessError", "AdbError"}:
        return True
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(marker in text for marker in _ADB_TRANSIENT_TRANSPORT_MARKERS)


def run_with_cloud_phone_adb_reconnect[T](
    *,
    run: Callable[[], T],
    reconnect: Callable[[], None],
    max_retries: int = _ACTION_ADB_RECONNECT_RETRIES,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> T:
    """执行 ADB 调用；遇通道瞬断则重连后有界重试。

    用于 tap／swipe／back／UI dump 等非截图路径；截图帧内重试仍走
    ``recover_cloud_phone_adb_binding`` + screencap 专用循环。
    """

    if max_retries < 0:
        raise ValueError("max_retries 必须为非负整数")
    sleeper = time.sleep if sleep is None else sleep
    logger = log or (lambda _msg: None)
    last_exc: BaseException | None = None
    attempts = max_retries + 1
    for attempt in range(attempts):
        try:
            return run()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries or not is_transient_adb_transport_error(exc):
                raise
            logger(
                "cloud_phone adb reconnect before retry "
                f"attempt={attempt + 1}/{max_retries} error={type(exc).__name__}"
            )
            _LOGGER.info(
                "cloud_phone adb reconnect before retry attempt=%s/%s error=%s",
                attempt + 1,
                max_retries,
                type(exc).__name__,
            )
            try:
                reconnect()
            except Exception:
                raise last_exc from None
            backoff_index = min(attempt, len(_ACTION_ADB_RECONNECT_BACKOFF_SECONDS) - 1)
            sleeper(_ACTION_ADB_RECONNECT_BACKOFF_SECONDS[backoff_index])
    assert last_exc is not None
    raise last_exc


def refresh_cloud_phone_adb_binding(
    *,
    controller: Any,
    log: Callable[[str], None] | None = None,
    pull_timeout_seconds: float | None = None,
    ready_timeout_seconds: float = _CAPTURE_REFRESH_READY_TIMEOUT_SECONDS,
) -> tuple[str, AdbDeviceOps, list[str]]:
    """刷新 MoreLogin ADB 绑定：短超时 ensure_ready + 强制重连并重建 ops。"""

    logger = log or (lambda _msg: None)
    ensure_ready = getattr(controller, "ensure_ready", None)
    if not callable(ensure_ready):
        raise RuntimeError("云手机控制器不支持刷新 ADB 绑定")
    try:
        serial, logs = ensure_ready(timeout_seconds=ready_timeout_seconds)
    except TypeError:
        # 兼容测试／旧控制器不含 timeout_seconds 的签名
        serial, logs = ensure_ready()
    refreshed_serial = str(serial or "").strip()
    if not refreshed_serial:
        raise RuntimeError("MoreLogin 刷新后未返回 ADB endpoint")
    refresh_logs = list(logs or [])
    for line in refresh_logs:
        logger(f"adb_session_health capture_refresh {line}")
        _LOGGER.info("adb_session_health capture_refresh %s", line)
    reopen = getattr(controller, "_reopen_adb_session", None)
    if callable(reopen):
        try:
            refreshed_serial = str(
                reopen(refreshed_serial, refresh_logs) or refreshed_serial
            ).strip() or refreshed_serial
        except Exception as exc:  # noqa: BLE001 - 重连失败仍尝试重建 ops
            _LOGGER.info(
                "adb_session_health capture_refresh reopen skipped: %s",
                type(exc).__name__,
            )
    ops = open_adb_device_ops(
        BACKEND_CLOUD_PHONE,
        log=logger,
        cloud_phone_controller=controller,
        remote_pull_timeout_seconds=pull_timeout_seconds,
    )
    return refreshed_serial, ops, refresh_logs


def recover_cloud_phone_adb_binding(
    *,
    controller: Any,
    log: Callable[[str], None] | None = None,
    pull_timeout_seconds: float | None = None,
    ready_timeout_seconds: float = _CAPTURE_REFRESH_READY_TIMEOUT_SECONDS,
) -> tuple[Any, str, AdbDeviceOps, list[str]]:
    """刷新 ADB；旧控制器失败时用同一环境的新控制器有界重试一次。"""

    logger = log or (lambda _msg: None)
    try:
        serial, ops, logs = refresh_cloud_phone_adb_binding(
            controller=controller,
            log=logger,
            pull_timeout_seconds=pull_timeout_seconds,
            ready_timeout_seconds=ready_timeout_seconds,
        )
        return controller, serial, ops, logs
    except Exception as first_exc:
        fresh_factory = getattr(controller, "fresh_for_adb_refresh", None)
        if not callable(fresh_factory):
            raise
        logger(
            "adb_session_health existing controller refresh failed; "
            f"retry with fresh controller error={type(first_exc).__name__}"
        )
        replacement = fresh_factory()
        if replacement is None or replacement is controller:
            raise RuntimeError("云手机控制器重建未返回新实例") from first_exc
        try:
            serial, ops, logs = refresh_cloud_phone_adb_binding(
                controller=replacement,
                log=logger,
                pull_timeout_seconds=pull_timeout_seconds,
                ready_timeout_seconds=ready_timeout_seconds,
            )
        except Exception as replacement_exc:
            raise replacement_exc from first_exc
        recovery_log = (
            "adb_session_health fresh controller recovery succeeded "
            f"first_error={type(first_exc).__name__}"
        )
        logger(recovery_log)
        _LOGGER.info("%s", recovery_log)
        return replacement, serial, ops, [recovery_log, *logs]


def merge_health_into_pipeline(
    pipeline: Mapping[str, Any] | dict[str, Any],
    health: AdbSessionHealthState | None,
) -> dict[str, Any]:
    """把会话健康审计并入 capture_pipeline_latency。"""

    payload = dict(pipeline)
    if health is not None:
        payload["adb_session_health"] = health.to_dict()
    return payload
