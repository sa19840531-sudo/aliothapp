"""广告独立出站 URL 策略；DNS 与连接固定由基础设施层执行。"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


class AdOutboundDenied(ValueError):
    """只携带稳定类别，禁止把目标或解析值写入异常。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def public_ad_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_reserved,
                address.is_unspecified,
                address.is_multicast,
            )
        )
        and not getattr(address, "ipv4_mapped", None)
    )


def validate_ad_url(value: object, *, previous_url: str | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) <= 32 or char == "\\" for char in value)
    ):
        raise AdOutboundDenied("WEB_AD_OUTBOUND_INVALID_URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "%" in host
            or port == 0
        ):
            raise ValueError
        if previous_url and urlsplit(previous_url).scheme == "https" and parsed.scheme != "https":
            raise AdOutboundDenied("WEB_AD_OUTBOUND_HTTPS_DOWNGRADE")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host.rstrip(".").casefold() in {"localhost", "localhost.localdomain"}:
                raise AdOutboundDenied("WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS") from None
        else:
            if not public_ad_address(host):
                raise AdOutboundDenied("WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS")
    except AdOutboundDenied:
        raise
    except ValueError:
        raise AdOutboundDenied("WEB_AD_OUTBOUND_INVALID_URL") from None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def safe_ad_url(value: object) -> str | None:
    try:
        return validate_ad_url(value)
    except AdOutboundDenied:
        return None
