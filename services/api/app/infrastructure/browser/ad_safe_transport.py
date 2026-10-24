"""广告专用固定地址传输：不使用环境代理、不让浏览器重新解析目标。"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from services.api.app.application.web_ad_outbound_policy import (
    AdOutboundDenied,
    public_ad_address,
    validate_ad_url,
)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_REQUESTS = 64
MAX_REDIRECTS = 5
MAX_CLIENT_REDIRECT_SCAN_BYTES = 64 * 1024


_CLIENT_REDIRECT_PATTERNS = (
    re.compile(
        r"(?:window\.|document\.)?location(?:\.href)?\s*=\s*(['\"])(?P<url>.*?)\1",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:window\.|document\.)?location\.(?:replace|assign)\s*\(\s*(['\"])(?P<url>.*?)\1\s*\)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<meta\b(?=[^>]*\bhttp-equiv\s*=\s*(['\"]?)refresh\1)(?=[^>]*\bcontent\s*=\s*(['\"])(?P<content>.*?)\2)[^>]*>",
        re.IGNORECASE | re.DOTALL,
    ),
)


@dataclass(frozen=True)
class AdResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


def _client_document_redirect(response: AdResponse, *, resource_kind: str | None) -> str | None:
    """仅解析文档中的静态跳转字面量，不执行广告脚本。"""

    if resource_kind != "document" or not response.body:
        return None
    content_type = response.headers.get("content-type", "").casefold()
    prefix = response.body[:MAX_CLIENT_REDIRECT_SCAN_BYTES].lstrip().lower()
    if content_type and not any(
        marker in content_type for marker in ("text/html", "application/xhtml+xml")
    ):
        return None
    if not content_type and not prefix.startswith((b"<html", b"<!doctype", b"<script", b"<meta")):
        return None
    text = response.body[:MAX_CLIENT_REDIRECT_SCAN_BYTES].decode("utf-8", errors="replace")
    for index, pattern in enumerate(_CLIENT_REDIRECT_PATTERNS):
        match = pattern.search(text)
        if match is None:
            continue
        target = match.groupdict().get("url")
        if index == 2:
            content = match.groupdict().get("content") or ""
            target_match = re.search(
                r"(?:^|;)\s*url\s*=\s*(['\"]?)(?P<url>.*?)\1\s*$",
                content,
                flags=re.IGNORECASE | re.DOTALL,
            )
            target = target_match.group("url") if target_match is not None else None
        target = str(target or "").strip()
        if target:
            return target
    return None


async def _resolve_addresses(host: str, port: int) -> list[str]:
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({row[4][0] for row in rows})


def _connect_pinned(address: tuple[str, int], timeout: float) -> socket.socket:
    # address 只能来自已验证的 IP 集合；此处不能再使用原始域名。
    return socket.create_connection(address, timeout=timeout)


def _ordered_pinned_addresses(addresses: frozenset[str]) -> tuple[str, ...]:
    """优先容器普遍可达的 IPv4；失败时仍只在同一已验签固定集合内切换。"""

    return tuple(
        sorted(
            addresses,
            key=lambda value: (
                ipaddress.ip_address(value).version,
                ipaddress.ip_address(value).packed,
            ),
        )
    )


def _fetch_pinned(
    url: str,
    address: str,
    deadline: float,
    remaining_bytes: int,
    consume_bytes: Callable[[int], None],
) -> AdResponse:
    if remaining_bytes <= 0:
        raise AdOutboundDenied("WEB_AD_OUTBOUND_TOTAL_BYTES_LIMIT")
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = http.client.HTTPConnection(parsed.hostname, port)
    sock = _connect_pinned((address, port), max(0.01, min(3, deadline - time.monotonic())))

    def expire_socket():
        # HTTP 响应头慢速滴流不能把单次 socket 超时反复续期。
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    expiry = threading.Timer(max(0.01, deadline - time.monotonic()), expire_socket)
    expiry.daemon = True
    expiry.start()
    try:
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        connection.sock = sock
        connection.request(
            "GET",
            parsed.path + ("?" + parsed.query if parsed.query else ""),
            headers={
                "Host": parsed.netloc,
                "Connection": "close",
                "Accept-Encoding": "identity",
                "User-Agent": "Alioth-Ad-Evidence/1",
            },
        )
        response = connection.getresponse()
        header_pairs = response.getheaders()
        if sum(len(key) + len(value) for key, value in header_pairs) > 65536:
            raise AdOutboundDenied("WEB_AD_OUTBOUND_HEADER_LIMIT")
        headers = {key.lower(): value for key, value in header_pairs}
        if sum(key.lower() == "location" for key, _ in header_pairs) > 1:
            raise AdOutboundDenied("WEB_AD_OUTBOUND_AMBIGUOUS_REDIRECT")
        body = bytearray()
        # 定长响应读完会关闭底层文件；先判EOF，再操作socket或申请下一块额度。
        while not response.isclosed():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdOutboundDenied("WEB_AD_OUTBOUND_TIME_LIMIT")
            byte_allowance = min(MAX_RESPONSE_BYTES, remaining_bytes) - len(body)
            if byte_allowance <= 0:
                reason = (
                    "WEB_AD_OUTBOUND_TOTAL_BYTES_LIMIT"
                    if remaining_bytes <= MAX_RESPONSE_BYTES
                    else "WEB_AD_OUTBOUND_RESPONSE_LIMIT"
                )
                raise AdOutboundDenied(reason)
            sock.settimeout(min(3, remaining))
            chunk = response.read1(min(65536, byte_allowance))
            if not chunk:
                break
            # 已读取字节立即消费；响应失败或超额终止不能返还累计预算。
            consume_bytes(len(chunk))
            body.extend(chunk)
        return AdResponse(url, response.status, headers, bytes(body))
    finally:
        expiry.cancel()
        connection.close()
        sock.close()


class AdSafeTransport:
    """每次观察持有独立 DNS 固定集与额度；任一异常不回退直连。"""

    def __init__(self, *, timeout_seconds: float, follow_client_redirects: bool = True,
                 diagnostic_events: list | None = None) -> None:
        self.deadline = time.monotonic() + min(30, timeout_seconds)
        self.follow_client_redirects = follow_client_redirects
        self._pins: dict[str, frozenset[str]] = {}
        self._lock = asyncio.Lock()
        self._requests = 0
        self._bytes = 0
        self.denied_reasons: set[str] = set()
        self.har_entries: list[dict[str, object]] = []
        self.diagnostic_events = diagnostic_events
        self._diagnostic_event = None

    async def fetch(
        self,
        url: str,
        *,
        previous_url: str | None = None,
        resource_kind: str | None = None,
        request_scope: str | None = None,
    ) -> AdResponse:
        # 单并发确保整个观察窗口的字节/请求/DNS 固定预算不会竞态超领。
        async with self._lock:
            self._diagnostic_event = None
            try:
                return await self._fetch(
                    url,
                    previous_url=previous_url,
                    resource_kind=resource_kind,
                    request_scope=request_scope,
                )
            except AdOutboundDenied as exc:
                self.denied_reasons.add(exc.reason)
                if self._diagnostic_event is not None:
                    self._diagnostic_event['denial_reason'] = exc.reason
                raise

    async def _fetch(
        self,
        url: str,
        *,
        previous_url: str | None,
        resource_kind: str | None,
        request_scope: str | None,
    ) -> AdResponse:
        for redirect_index in range(MAX_REDIRECTS + 1):
            from .web_ad_hop_diagnostics import record_ad_hop

            event = record_ad_hop(
                self.diagnostic_events, url=url, hop_index=redirect_index,
                resource_type=resource_kind, request_scope=request_scope,
            )
            self._diagnostic_event = event
            remaining_bytes = MAX_TOTAL_BYTES - self._bytes
            if remaining_bytes <= 0:
                raise AdOutboundDenied("WEB_AD_OUTBOUND_TOTAL_BYTES_LIMIT")
            url = validate_ad_url(url, previous_url=previous_url)
            parsed = urlsplit(url)
            host = str(parsed.hostname).rstrip(".").casefold()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self._requests >= MAX_REQUESTS:
                raise AdOutboundDenied("WEB_AD_OUTBOUND_TIME_OR_REQUEST_LIMIT")
            try:
                answers = frozenset(
                    await asyncio.wait_for(_resolve_addresses(host, port), min(3, remaining))
                )
            except (OSError, TimeoutError, ValueError):
                raise AdOutboundDenied("WEB_AD_OUTBOUND_DNS_FAILED") from None
            if event is not None:
                event['resolved_addresses'] = sorted(answers)[:32]
            if not answers or not all(public_ad_address(item) for item in answers):
                raise AdOutboundDenied("WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS")
            if host in self._pins and answers != self._pins[host]:
                raise AdOutboundDenied("WEB_AD_OUTBOUND_DNS_REBINDING")
            self._pins[host] = answers
            started_at = time.monotonic()
            result = None
            for address in _ordered_pinned_addresses(answers):
                if self._requests >= MAX_REQUESTS or time.monotonic() >= self.deadline:
                    break
                attempt_bytes = MAX_TOTAL_BYTES - self._bytes
                if attempt_bytes <= 0:
                    raise AdOutboundDenied("WEB_AD_OUTBOUND_TOTAL_BYTES_LIMIT")
                self._requests += 1
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            _fetch_pinned,
                            url,
                            address,
                            self.deadline,
                            attempt_bytes,
                            self._consume_bytes,
                        ),
                        max(0.01, self.deadline - time.monotonic()),
                    )
                    break
                except AdOutboundDenied:
                    raise
                except (OSError, TimeoutError, http.client.HTTPException):
                    continue
            if result is None:
                raise AdOutboundDenied("WEB_AD_OUTBOUND_NETWORK_FAILED") from None
            if event is not None:
                event['status'] = result.status
            server_redirect = result.status in {301, 302, 303, 307, 308}
            client_redirect = (
                None
                if server_redirect or not self.follow_client_redirects
                else _client_document_redirect(result, resource_kind=resource_kind)
            )
            har_entry = {
                    "index": len(self.har_entries),
                    "method": "GET",
                    "resource_type": (resource_kind or "unknown")[:32],
                    "url": _har_url_without_secrets(result.url),
                    "status": result.status,
                    "body_size": len(result.body),
                    "redirect": server_redirect or client_redirect is not None,
                    "duration_ms": min(
                        30_000,
                        max(0, int((time.monotonic() - started_at) * 1_000)),
                    ),
                }
            if client_redirect is not None:
                har_entry["redirect_kind"] = "client_document"
            self.har_entries.append(har_entry)
            if not server_redirect and client_redirect is None:
                return result
            location = result.headers.get("location") if server_redirect else client_redirect
            if redirect_index == MAX_REDIRECTS or not location:
                raise AdOutboundDenied("WEB_AD_OUTBOUND_REDIRECT_LIMIT")
            previous_url, url = url, urljoin(url, location)
        raise AdOutboundDenied("WEB_AD_OUTBOUND_REDIRECT_LIMIT")

    def _consume_bytes(self, count: int) -> None:
        self._bytes += count


def _har_url_without_secrets(value: str) -> str:
    """HAR 只保留定位所需的源站和路径，查询、片段及用户信息一律删除。"""

    parsed = urlsplit(value)
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if not host:
        return ""
    port = parsed.port
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", "", ""))
