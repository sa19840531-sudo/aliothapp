"""Web 登录秘密释放前独立验证目标主机的 TLS 证书。"""

from __future__ import annotations

import asyncio
import ssl
from urllib.parse import urlsplit


async def verify_web_login_tls(origin: str, *, proxy_server: str | None = None) -> bool:
    """主采集容忍证书错误；登录必须由系统信任链重新验签。"""
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        if proxy_server:
            import httpx

            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(
                        proxy=proxy_server, verify=True, timeout=12, follow_redirects=False
                    ) as client:
                        await client.get(origin)
                    return True
                except httpx.TimeoutException:
                    if attempt:
                        return False
        context = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                parsed.hostname,
                parsed.port or 443,
                ssl=context,
                server_hostname=parsed.hostname,
            ),
            timeout=6,
        )
        del reader
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:  # noqa: BLE001 - 网络库的异常不应暴露目标响应内容
        return False
