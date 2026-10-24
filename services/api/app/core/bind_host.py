"""API 绑定地址解析：供星语 Official／可选 Demo 判断 loopback。"""

from __future__ import annotations

import os


def resolve_api_bind_host() -> str:
    """解析 API 绑定位址。"""
    for key in (
        "ALIOTH_API_BIND_HOST",
        "ALIOTH_BIND_HOST",
        "ALIOTH_API_HOST",
        "HOST",
    ):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return "127.0.0.1"


def is_loopback_bind_host(host: str | None = None) -> bool:
    h = (host or resolve_api_bind_host()).strip().lower()
    return h in ("127.0.0.1", "localhost", "::1")
