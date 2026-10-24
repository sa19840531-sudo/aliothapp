"""App 质检结束后清理 ADB 进程／连接。

设计：
- 默认开启（``ALIOTH_ADB_CLEANUP_AFTER_QC=1``）。
- 始终尝试 ``adb disconnect`` 本台 endpoint，避免残留半开连接。
- 仅在无其他活跃设备租约时强杀本机／容器内 ``adb`` 进程并 ``kill-server``；
  多台云手机共享 daemon 时不得互杀。
- 失败只记日志，不回滚租约结算。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Sequence

from services.api.app.infrastructure.device.cloud_phone_vendor import looks_like_adb_endpoint

_LOGGER = logging.getLogger(__name__)
_CLEANUP_ENV = "ALIOTH_ADB_CLEANUP_AFTER_QC"


def adb_cleanup_after_qc_enabled() -> bool:
    """质检结束后是否清理 ADB（默认开）。"""

    raw = (os.getenv(_CLEANUP_ENV) or "1").strip().casefold()
    return raw not in {"0", "false", "off", "no", "-"}


def resolve_adb_cleanup_endpoint(*, preferred: str | None = None) -> str:
    """解析本台应 disconnect 的 ADB endpoint。"""

    candidates = (
        preferred,
        os.getenv("ALIOTH_ADB_SERIAL"),
        os.getenv("ALIOTH_CLOUD_PHONE_ADB"),
        os.getenv("ALIOTH_REDROID_ADB"),
    )
    for raw in candidates:
        text = str(raw or "").strip()
        if looks_like_adb_endpoint(text):
            return text
    return ""


def resolve_sdk_adb_path() -> str | None:
    """定位 SDK platform-tools/adb；找不到则返回 None。"""

    for key in ("ALIOTH_HOST_ADB", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        raw = (os.getenv(key) or "").strip()
        if not raw:
            continue
        if key == "ALIOTH_HOST_ADB":
            if os.path.isfile(raw):
                return raw
            continue
        for name in ("adb.exe", "adb"):
            candidate = os.path.join(raw, "platform-tools", name)
            if os.path.isfile(candidate):
                return candidate
    for name in ("adb.exe", "adb"):
        # PATH 兜底
        from shutil import which

        found = which(name)
        if found:
            return found
    return None


def cleanup_adb_processes_after_app_qc(
    *,
    endpoint: str | None = None,
    kill_processes: bool = True,
    reason: str = "app_qc_done",
) -> list[str]:
    """质检收口后清理 ADB：disconnect →（可选）杀进程 → kill-server。

    ``kill_processes=False`` 时只断本台连接，供并行租约场景使用。
    """

    logs: list[str] = [f"adb_cleanup reason={reason} kill_processes={kill_processes}"]
    target = resolve_adb_cleanup_endpoint(preferred=endpoint)
    adb = resolve_sdk_adb_path()
    if target and adb:
        logs.extend(_disconnect_endpoint(adb=adb, endpoint=target))
    elif target and not adb:
        logs.append(f"adb_cleanup disconnect skipped (no adb binary) endpoint={target}")
    else:
        logs.append("adb_cleanup disconnect skipped (no endpoint)")

    if not kill_processes:
        logs.append("adb_cleanup process kill deferred (other active leases)")
        return logs

    logs.extend(_kill_adb_os_processes())
    if adb:
        logs.extend(_kill_adb_server(adb=adb))
    else:
        logs.append("adb_cleanup kill-server skipped (no adb binary)")
    return logs


def _disconnect_endpoint(*, adb: str, endpoint: str) -> list[str]:
    try:
        completed = subprocess.run(
            [adb, "disconnect", endpoint],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        body = "\n".join(
            part for part in (completed.stdout or "", completed.stderr or "") if part
        ).strip()
        return [f"adb disconnect {endpoint} rc={completed.returncode} {body[:160]}"]
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断收口
        return [f"adb disconnect {endpoint} failed: {type(exc).__name__}"]


def _kill_adb_server(*, adb: str) -> list[str]:
    try:
        completed = subprocess.run(
            [adb, "kill-server"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return [f"adb kill-server rc={completed.returncode}"]
    except Exception as exc:  # noqa: BLE001
        return [f"adb kill-server failed: {type(exc).__name__}"]


def _kill_adb_os_processes() -> list[str]:
    """尽力杀掉本机／容器内 adb 进程；无权的提权僵尸只记 skip。"""

    if sys.platform.startswith("win"):
        return _kill_adb_windows()
    return _kill_adb_posix()


def _kill_adb_windows() -> list[str]:
    logs: list[str] = []
    try:
        listed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='adb.exe'\" | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return [f"adb_cleanup list failed: {type(exc).__name__}"]

    pids = [
        line.strip()
        for line in (listed.stdout or "").splitlines()
        if line.strip().isdigit()
    ]
    logs.append(f"adb_cleanup found_pids={len(pids)}")
    killed = 0
    skipped = 0
    for pid in pids:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/PID", pid],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                killed += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    logs.append(f"adb_cleanup killed={killed} skipped={skipped}")
    return logs


def _kill_adb_posix() -> list[str]:
    logs: list[str] = []
    # 先 pkill；没有则忽略。
    for cmd in (
        ["pkill", "-x", "adb"],
        ["killall", "adb"],
    ):
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            logs.append(f"adb_cleanup {' '.join(cmd)} rc={completed.returncode}")
            if completed.returncode in {0, 1}:
                # 0=killed, 1=no process
                break
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logs.append(f"adb_cleanup {' '.join(cmd)} failed: {type(exc).__name__}")
    if not logs:
        logs.append("adb_cleanup posix kill skipped (no pkill/killall)")
    return logs


def summarize_cleanup_logs(logs: Sequence[str]) -> str:
    return "; ".join(str(item) for item in logs if item)
