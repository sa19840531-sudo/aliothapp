"""Web 浏览器主机身份与严格 origin 边界的共享规则。"""

from ipaddress import IPv6Address
from urllib.parse import urlsplit, urlunsplit

import idna


def canonical_web_host(host: str) -> str:
    """将浏览器等价的 IDN、大小写和 IPv6 写法归一化。"""
    if ":" in host:
        return IPv6Address(host).compressed.casefold()
    try:
        return idna.encode(host, uts46=True, transitional=False).decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("Web 主机名不是合法 IDN") from exc


def web_origin_key(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("Web origin 必须为无凭据的 HTTP(S) URL")
    port = parts.port
    return (
        scheme,
        canonical_web_host(parts.hostname),
        port if port is not None else (443 if scheme == "https" else 80),
    )


def canonical_web_origin(url: str) -> str:
    scheme, host, port = web_origin_key(url)
    netloc = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def web_origin_matches(
    target_url: str, source_url: str, *, allow_http_upgrade: bool = False
) -> bool:
    """默认严格同源；显式允许时仅接受同主机默认端口的单向升级。"""
    target = web_origin_key(target_url)
    source = web_origin_key(source_url)
    return target == source or (
        allow_http_upgrade
        and source[0] == "http"
        and source[2] == 80
        and target[0] == "https"
        and target[2] == 443
        and target[1] == source[1]
    )
