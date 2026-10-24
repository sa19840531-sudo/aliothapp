"""ONIRENT 跳板隧道契约：Alioth 代码直连跳板公网埠（不做隧道本身）。

拓扑（Alioth 不部署在跳板上）：
1. 负责人 Windows 跑 ``start-linode-adb-tunnel.ps1``（SSH ``-R 0.0.0.0:3xxxx``）
2. 跳板 ``172.104.172.119:3xxxx`` 对外可连（需 sshd ``GatewayPorts clientspecified``）
3. Alioth（任意能访问跳板的机器）``adb connect 172.104.172.119:3xxxx``

本地埠约定：``10000 + 公网埠``。槽位代号与默认映射（顺序即设备池顺序；**五台全走跳板**）：
- ``qa-a``：``51.89.164.208:20615`` → ``{JUMP}:30615``
- ``qa-b``：``51.195.137.27:20660`` → ``{JUMP}:30660``
- ``qa-c``：``198.244.190.180:20907`` → ``{JUMP}:30907``
- ``qa-d``：``51.195.137.27:20033`` → ``{JUMP}:30033``
- ``qa-e``：``51.89.164.208:20613`` → ``{JUMP}:30613``

环境变量：
- ``ALIOTH_ONIRENT_ADB_SOURCE=tunnel``（或 ``jump``／``linode``）
- ``ALIOTH_ONIRENT_ADB_TUNNEL=1``
- ``ALIOTH_ONIRENT_ADB_TUNNEL_JUMP_HOST``（默认 ``172.104.172.119``）
- ``ALIOTH_ONIRENT_ADB_TUNNEL_MAP``：``公网=跳板可达端点,...``（须含 qa-a…qa-e 五台）
- ``ALIOTH_ONIRENT_ADB_TUNNEL_BIND``：SSH ``-R`` 远端绑定（默认 ``0.0.0.0``）
- ``ALIOTH_DEVICE_POOL_SLOTS``／``ALIOTH_DEVICE_POOL_ONIRENT_ADB``：须等长，按 qa-a…qa-e
  全部填跳板 ``{JUMP}:3xxxx``（禁止本机再 ``adb connect`` 公网 ``2xxxx``）

SSH 运维密钥见 ``infra/ssh/``；业务侧不要把 Alioth 装在跳板当隧道消费者。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DEFAULT_DEVICE_SLOT_CODES",
    "DEFAULT_TUNNEL_JUMP_HOST",
    "DEFAULT_TUNNEL_SSH_USER",
    "DEFAULT_TUNNEL_SSH_IDENTITY_NAME",
    "DEFAULT_TUNNEL_SSH_PUBLIC_KEY",
    "DEFAULT_TUNNEL_SSH_FINGERPRINT",
    "DEFAULT_TUNNEL_BIND_HOST",
    "DEFAULT_TUNNEL_LOCAL_ENDPOINT",
    "DEFAULT_TUNNEL_PUBLIC_ENDPOINT",
    "DEFAULT_TUNNEL_PORT_MAP",
    "DEFAULT_TUNNEL_MAP",
    "DEFAULT_PUBLIC_ADB_ENDPOINTS",
    "DEFAULT_POOL_ADB_ENDPOINTS",
    "onirent_adb_tunnel_enabled",
    "resolve_onirent_adb_tunnel_jump_host",
    "resolve_onirent_adb_tunnel_bind_host",
    "resolve_onirent_adb_tunnel_ssh_user",
    "resolve_onirent_adb_tunnel_ssh_identity",
    "resolve_onirent_adb_tunnel_local_endpoint",
    "resolve_onirent_adb_tunnel_public_endpoint",
    "list_onirent_adb_tunnel_endpoint_pairs",
    "list_onirent_adb_tunnel_endpoints",
    "list_onirent_public_adb_endpoints",
    "list_onirent_adb_tunnel_ssh_forwards",
    "list_default_device_pool_slots",
    "map_onirent_public_adb_to_tunnel",
]

DEFAULT_TUNNEL_PUBLIC_ENDPOINT = "51.89.164.208:20615"
DEFAULT_TUNNEL_JUMP_HOST = "172.104.172.119"
DEFAULT_TUNNEL_BIND_HOST = "0.0.0.0"
DEFAULT_TUNNEL_SSH_USER = "root"
DEFAULT_TUNNEL_SSH_IDENTITY_NAME = "alioth-linode-tunnel"
DEFAULT_TUNNEL_SSH_PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINE5BK7+rUD5ncFjKLuemf6OaM/2FCs722W/9Hehs+7L "
    "alioth-linode-tunnel"
)
DEFAULT_TUNNEL_SSH_FINGERPRINT = (
    "SHA256:r95llCcyqRxYWJ9ApiBvaUTnarE+rHnsllsO3Du/WVI"
)

# 槽位代号（与 DEFAULT_POOL_ADB_ENDPOINTS 下标一一对应）
DEFAULT_DEVICE_SLOT_CODES: tuple[str, ...] = (
    "qa-a",
    "qa-b",
    "qa-c",
    "qa-d",
    "qa-e",
)

# 公网 → 跳板监听埠（五台全隧道；本地埠 = 10000 + 公网埠）
DEFAULT_TUNNEL_PORT_MAP: tuple[tuple[str, int], ...] = (
    ("51.89.164.208:20615", 30615),  # qa-a
    ("51.195.137.27:20660", 30660),  # qa-b
    ("198.244.190.180:20907", 30907),  # qa-c
    ("51.195.137.27:20033", 30033),  # qa-d
    ("51.89.164.208:20613", 30613),  # qa-e
)

DEFAULT_PUBLIC_ADB_ENDPOINTS: tuple[str, ...] = (
    "51.89.164.208:20615",
    "51.195.137.27:20660",
    "198.244.190.180:20907",
    "51.195.137.27:20033",
    "51.89.164.208:20613",
)


def resolve_onirent_adb_tunnel_jump_host() -> str:
    raw = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_JUMP_HOST") or "").strip()
    return raw or DEFAULT_TUNNEL_JUMP_HOST


def resolve_onirent_adb_tunnel_bind_host() -> str:
    """SSH -R 在跳板上的绑定地址（对外可连时用 0.0.0.0）。"""

    raw = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_BIND") or "").strip()
    return raw or DEFAULT_TUNNEL_BIND_HOST


def _client_endpoint_for_port(port: int) -> str:
    return f"{resolve_onirent_adb_tunnel_jump_host()}:{int(port)}"


def _default_tunnel_map_pairs() -> list[tuple[str, str]]:
    return [
        (public, _client_endpoint_for_port(port))
        for public, port in DEFAULT_TUNNEL_PORT_MAP
    ]


def _default_pool_adb_endpoints() -> list[str]:
    """默认设备池：五台全部走跳板（与槽位同序）。"""

    return [client for _, client in _default_tunnel_map_pairs()]


# 动态默认：客户端连跳板公网 IP（非 127.0.0.1）
DEFAULT_TUNNEL_MAP: tuple[tuple[str, str], ...] = tuple(_default_tunnel_map_pairs())
DEFAULT_TUNNEL_LOCAL_ENDPOINT = _client_endpoint_for_port(30615)
DEFAULT_POOL_ADB_ENDPOINTS: tuple[str, ...] = tuple(_default_pool_adb_endpoints())


def list_default_device_pool_slots() -> list[str]:
    """默认设备池槽位 ``cloud_phone:qa-a`` … ``qa-e``（与隧道端点等长）。"""

    return [f"cloud_phone:{code}" for code in DEFAULT_DEVICE_SLOT_CODES]


def list_onirent_public_adb_endpoints() -> list[str]:
    """公网 ONIRENT 端点（非跳板）。显式池优先。"""

    configured: list[str] = []
    seen: set[str] = set()
    for raw in (os.getenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB") or "").split(","):
        endpoint = raw.strip()
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        configured.append(endpoint)
    if configured:
        return configured
    return list(DEFAULT_PUBLIC_ADB_ENDPOINTS)


def onirent_adb_tunnel_enabled() -> bool:
    """是否启用跳板隧道 ADB 源（含 SOURCE=tunnel 或显式开关）。"""
    flag = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    raw = (os.getenv("ALIOTH_ONIRENT_ADB_SOURCE") or "").strip().lower()
    return raw in ("tunnel", "jump", "linode", "shared_tunnel")


def resolve_onirent_adb_tunnel_ssh_user() -> str:
    raw = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_SSH_USER") or "").strip()
    return raw or DEFAULT_TUNNEL_SSH_USER


def _default_ssh_identity_candidates() -> list[Path]:
    home = Path.home()
    repo_infra_ssh = (
        Path(__file__).resolve().parents[5] / "infra" / "ssh" / DEFAULT_TUNNEL_SSH_IDENTITY_NAME
    )
    return [
        repo_infra_ssh,
        home / ".ssh" / "id_ed25519_alioth_linode",
        home / ".ssh" / DEFAULT_TUNNEL_SSH_IDENTITY_NAME,
    ]


def resolve_onirent_adb_tunnel_ssh_identity() -> str:
    raw = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_SSH_IDENTITY") or "").strip()
    if raw:
        return raw
    for candidate in _default_ssh_identity_candidates():
        if candidate.is_file():
            return str(candidate)
    return str(_default_ssh_identity_candidates()[0])


def _normalize_tunnel_client_endpoint(endpoint: str) -> str:
    """把 127.0.0.1／0.0.0.0 写成跳板公网客户端地址。"""

    raw = str(endpoint or "").strip()
    if ":" not in raw:
        return raw
    host, port_s = raw.rsplit(":", 1)
    if not port_s.isdigit():
        return raw
    if host in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "*"):
        return _client_endpoint_for_port(int(port_s))
    return raw


def _parse_tunnel_map_env(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen_public: set[str] = set()
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        public, local = item.split("=", 1)
        public = public.strip()
        local = _normalize_tunnel_client_endpoint(local.strip())
        if not public or not local or ":" not in public or ":" not in local:
            continue
        if public in seen_public:
            continue
        seen_public.add(public)
        pairs.append((public, local))
    return pairs


def list_onirent_adb_tunnel_endpoint_pairs() -> list[tuple[str, str]]:
    """返回 ``(ONIRENT公网, 跳板可达端点)``。"""

    mapped = _parse_tunnel_map_env(os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_MAP") or "")
    if mapped:
        pairs = mapped
    else:
        pairs = _default_tunnel_map_pairs()

    local_override = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_LOCAL") or "").strip()
    public_override = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_PUBLIC") or "").strip()
    if local_override and public_override:
        local_override = _normalize_tunnel_client_endpoint(local_override)
        pairs = [(public_ep, local_ep) for public_ep, local_ep in pairs if public_ep != public_override]
        pairs.insert(0, (public_override, local_override))
    elif local_override and not mapped:
        first_public = pairs[0][0] if pairs else DEFAULT_TUNNEL_PUBLIC_ENDPOINT
        pairs = [
            (first_public, _normalize_tunnel_client_endpoint(local_override)),
            *pairs[1:],
        ]
    return pairs


def resolve_onirent_adb_tunnel_local_endpoint() -> str:
    pairs = list_onirent_adb_tunnel_endpoint_pairs()
    if pairs:
        return pairs[0][1]
    raw = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_LOCAL") or "").strip()
    if raw:
        return _normalize_tunnel_client_endpoint(raw)
    return _client_endpoint_for_port(30615)


def resolve_onirent_adb_tunnel_public_endpoint() -> str:
    pairs = list_onirent_adb_tunnel_endpoint_pairs()
    if pairs:
        return pairs[0][0]
    raw = (os.getenv("ALIOTH_ONIRENT_ADB_TUNNEL_PUBLIC") or "").strip()
    return raw or DEFAULT_TUNNEL_PUBLIC_ENDPOINT


def list_onirent_adb_tunnel_endpoints() -> list[str]:
    """隧道模式下设备池：跳板公网 IP:埠（Alioth 直连这些地址）。"""

    configured: list[str] = []
    seen: set[str] = set()
    for raw in (os.getenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB") or "").split(","):
        endpoint = _normalize_tunnel_client_endpoint(raw.strip())
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        configured.append(endpoint)
    if configured:
        return configured
    # 默认五台全跳板（与 DEFAULT_TUNNEL_PORT_MAP／槽位等长）
    return _default_pool_adb_endpoints()


def list_onirent_adb_tunnel_ssh_forwards() -> list[str]:
    """供隧道脚本使用的 ``-R`` 规格：``bind:port:onirent_host:onirent_port``。"""

    bind = resolve_onirent_adb_tunnel_bind_host()
    forwards: list[str] = []
    for public, client in list_onirent_adb_tunnel_endpoint_pairs():
        if ":" not in public or ":" not in client:
            continue
        pub_host, pub_port = public.rsplit(":", 1)
        _, listen_port = client.rsplit(":", 1)
        if not pub_port.isdigit() or not listen_port.isdigit():
            continue
        forwards.append(f"{bind}:{listen_port}:{pub_host}:{pub_port}")
    return forwards


def map_onirent_public_adb_to_tunnel(endpoint: str) -> str:
    """若端点等于已登记公网 ADB，改写为跳板可达端点；否则原样返回。"""

    raw = str(endpoint or "").strip()
    if not raw:
        return raw
    for public, local in list_onirent_adb_tunnel_endpoint_pairs():
        if raw == public:
            return local
    return raw
