"""雲手機供應商：MoreLogin（Open API）／ONIRENT（CC智控 WS 或固定 ADB／公网直连／跳板隧道）。

ONIRENT／CC智控桌面端：
- WebSocket API：``ws://127.0.0.1:33332/``，``{"type":"list"}`` 回傳 ADB 列表
- 也可手動寫死 ``ALIOTH_DEVICE_POOL_ONIRENT_ADB=host:port,...``
- **推荐**：``ALIOTH_ONIRENT_ADB_SOURCE=tunnel`` 代码直连跳板公网埠（``172.104.172.119:3xxxx``）
- Alioth **不**部署在跳板上；反向隧道由 ``start-linode-adb-tunnel.ps1`` 维持

環境變數：
- ``ALIOTH_CLOUD_PHONE_VENDOR=onirent``
- ``ALIOTH_ONIRENT_ADB_SOURCE``：``auto``（預設）／``direct``／``env``／``ws``／``tunnel``
  - ``tunnel``／``jump``／``linode``：跳板公网 IP:3xxxx（见 ``onirent_adb_tunnel``）
  - ``direct``／``public``／``wan``：直连 ONIRENT 公网（仅出口可达时）
  - ``env``／``static``／``manual``：只用 ``ALIOTH_DEVICE_POOL_ONIRENT_ADB``
  - ``ws``：只用 CC智控 list API
  - ``auto``：env 有值用 env，否則打 WS
- ``ALIOTH_ONIRENT_WS_URL``：預設 ``ws://127.0.0.1:33332/``
  （Docker 建議 ``ws://host.docker.internal:33332/``）
"""

from __future__ import annotations

import os
import re

__all__ = [
    "looks_like_adb_endpoint",
    "resolve_cloud_phone_vendor",
    "cloud_phone_vendor_is_onirent",
    "list_pool_onirent_adb_endpoints",
    "resolve_onirent_adb_source",
]

_ADB_ENDPOINT_RE = re.compile(
    r"^(?P<host>[A-Za-z0-9._\-\[\]]+):(?P<port>\d{2,5})$"
)
_ADB_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")


def looks_like_adb_endpoint(value: str | None) -> bool:
    """判斷是否為可用的 ADB 目標（``host:port``／``emulator-5554``／序號）。"""
    raw = str(value or "").strip()
    if not raw or "://" in raw or " " in raw:
        return False
    match = _ADB_ENDPOINT_RE.match(raw)
    if match:
        port = int(match.group("port"))
        return 1 <= port <= 65535
    if raw.startswith("emulator-") and raw.split("-", 1)[-1].isdigit():
        return True
    # 排除純數字（MoreLogin envId）
    if raw.isdigit():
        return False
    return bool(_ADB_SERIAL_RE.fullmatch(raw)) and len(raw) >= 4


def resolve_onirent_adb_source() -> str:
    raw = (os.getenv("ALIOTH_ONIRENT_ADB_SOURCE") or "auto").strip().lower()
    if raw in ("direct", "public", "wan"):
        return "direct"
    if raw in ("env", "static", "manual"):
        return "env"
    if raw in ("ws", "api", "cc", "zhikong", "list"):
        return "ws"
    if raw in ("tunnel", "jump", "linode", "shared_tunnel"):
        return "tunnel"
    # SOURCE=auto 时可用显式开关切入隧道；TUNNEL=0 时不进隧道
    flag = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return "tunnel"
    if flag in ("0", "false", "no", "off"):
        # 显式关闭隧道：有池则 env，否则 direct 默认公网
        if _env_onirent_adb_endpoints():
            return "env"
        return "direct"
    return "auto"


def resolve_cloud_phone_vendor() -> str:
    """回傳 ``onirent``／``morelogin``。"""
    raw = (os.getenv("ALIOTH_CLOUD_PHONE_VENDOR") or "").strip().lower()
    if raw in ("onirent", "oni", "onlirent", "cc", "zhikong"):
        return "onirent"
    if raw in ("morelogin", "ml"):
        return "morelogin"
    if _env_onirent_adb_endpoints() or resolve_onirent_adb_source() == "ws":
        return "onirent"
    return "morelogin"


def cloud_phone_vendor_is_onirent() -> bool:
    return resolve_cloud_phone_vendor() == "onirent"


def _env_onirent_adb_endpoints() -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in (os.getenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB") or "").split(","):
        endpoint = raw.strip()
        if not endpoint or endpoint in seen:
            continue
        if not looks_like_adb_endpoint(endpoint):
            raise ValueError(
                f"ALIOTH_DEVICE_POOL_ONIRENT_ADB 含非法端點：{endpoint!r}（需 host:port 或 ADB serial）"
            )
        seen.add(endpoint)
        items.append(endpoint)
    return items


def _ws_onirent_adb_endpoints() -> list[str]:
    from services.api.app.infrastructure.device.onirent_ws import (
        CcZhikongWsError,
        fetch_cc_device_list,
    )

    try:
        endpoints = fetch_cc_device_list()
    except CcZhikongWsError:
        raise
    except OSError as exc:
        raise CcZhikongWsError(f"無法連線 CC智控 WS：{exc}") from exc

    cleaned: list[str] = []
    seen: set[str] = set()
    for endpoint in endpoints:
        ep = str(endpoint or "").strip()
        if not ep or ep in seen:
            continue
        if not looks_like_adb_endpoint(ep):
            raise ValueError(f"CC智控 list 回傳非法 ADB 端點：{ep!r}")
        seen.add(ep)
        cleaned.append(ep)
    return cleaned


def _validate_endpoints(items: list[str], *, label: str) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for endpoint in items:
        ep = str(endpoint or "").strip()
        if not ep or ep in seen:
            continue
        if not looks_like_adb_endpoint(ep):
            raise ValueError(f"{label} 含非法 ADB 端點：{ep!r}")
        seen.add(ep)
        cleaned.append(ep)
    return cleaned


def list_pool_onirent_adb_endpoints(*, allow_ws: bool = True) -> list[str]:
    """讀取 ONIRENT 設備池 ADB 端點（direct／env／WS／跳板隧道）。"""
    source = resolve_onirent_adb_source()
    env_items = _env_onirent_adb_endpoints()
    if source == "tunnel":
        from services.api.app.infrastructure.device.onirent_adb_tunnel import (
            list_onirent_adb_tunnel_endpoints,
        )

        return _validate_endpoints(
            list_onirent_adb_tunnel_endpoints(),
            label="ONIRENT 跳板隧道",
        )
    if source == "direct":
        from services.api.app.infrastructure.device.onirent_adb_tunnel import (
            list_onirent_public_adb_endpoints,
        )

        return _validate_endpoints(
            list_onirent_public_adb_endpoints(),
            label="ONIRENT 公网直连",
        )
    if source == "env":
        return env_items
    if source == "ws":
        if not allow_ws:
            return env_items
        return _ws_onirent_adb_endpoints()
    # auto
    if env_items:
        return env_items
    if not allow_ws:
        return []
    return _ws_onirent_adb_endpoints()
