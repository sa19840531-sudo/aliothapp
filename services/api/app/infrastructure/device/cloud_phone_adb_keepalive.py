"""API 进程内保持跳板云手机留在 adb 列表。

只对仍在转发的跳板端口做 ``adb connect``。已经是 ``device`` 的不重复连接。
不 ``adb disconnect``，不 ``adb kill-server``，也不连接公网 ``2xxxx``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# qa-d / qa-e 租约已到期，环境变量里还留着这两埠，不要再接回列表。
_EXPIRED_JUMP_PORTS = frozenset({30033, 30613})


def cloud_phone_adb_keepalive_enabled() -> bool:
    flag = (os.getenv("ALIOTH_CLOUD_PHONE_ADB_KEEPALIVE") or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        onirent_adb_tunnel_enabled,
    )

    return onirent_adb_tunnel_enabled()


def cloud_phone_adb_keepalive_seconds() -> float:
    raw = (os.getenv("ALIOTH_CLOUD_PHONE_ADB_KEEPALIVE_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else 20.0
    except ValueError:
        value = 20.0
    return min(300.0, max(5.0, value))


def jump_adb_endpoints_to_keep() -> list[str]:
    """当前应留在 adb 列表里的跳板端点。"""

    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        list_onirent_adb_tunnel_endpoints,
        resolve_onirent_adb_tunnel_jump_host,
    )

    jump_host = resolve_onirent_adb_tunnel_jump_host()
    kept: list[str] = []
    seen: set[str] = set()
    for endpoint in list_onirent_adb_tunnel_endpoints():
        host, separator, port_s = endpoint.rpartition(":")
        if separator != ":" or host != jump_host or not port_s.isdigit():
            continue
        if int(port_s) in _EXPIRED_JUMP_PORTS:
            continue
        if endpoint in seen:
            continue
        seen.add(endpoint)
        kept.append(endpoint)
    return kept


def endpoints_missing_from_adb_list(devices_text: str, wanted: list[str]) -> list[str]:
    """只有不在列表里，或状态不是 device 的端点才需要 connect。"""

    online: set[str] = set()
    for line in devices_text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "List":
            continue
        if parts[1] == "device":
            online.add(parts[0])
    return [endpoint for endpoint in wanted if endpoint not in online]


def ensure_jump_adb_endpoints() -> list[str]:
    """把掉出列表的跳板云手机接回去。返回本次实际 connect 的端点。"""

    wanted = jump_adb_endpoints_to_keep()
    if not wanted:
        return []
    from services.api.app.infrastructure.device.google_emulator import resolve_adb_executable

    try:
        adb = str(resolve_adb_executable())
    except Exception as exc:  # noqa: BLE001 - 没有 adb 时不能拖垮 API
        logger.warning("跳板 ADB 保活找不到 adb：%s", exc)
        return []
    subprocess.run(
        [adb, "start-server"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    listed = subprocess.run(
        [adb, "devices"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    devices_text = "\n".join(part for part in (listed.stdout, listed.stderr) if part)
    missing = endpoints_missing_from_adb_list(devices_text, wanted)
    connected: list[str] = []
    for endpoint in missing:
        result = subprocess.run(
            [adb, "connect", endpoint],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        detail = " ".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        logger.info("跳板 ADB 接回 %s：%s", endpoint, detail[:200] or result.returncode)
        connected.append(endpoint)
    return connected


async def jump_adb_keepalive_loop() -> None:
    """周期检查 adb 列表，把掉线的跳板端口接回去。"""

    while True:
        try:
            await asyncio.to_thread(ensure_jump_adb_endpoints)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 保活失败不能打断 API
            logger.exception("跳板 ADB 保活失败")
        try:
            await asyncio.sleep(cloud_phone_adb_keepalive_seconds())
        except asyncio.CancelledError:
            raise
