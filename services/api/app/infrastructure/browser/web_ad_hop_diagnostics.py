"""Web 广告逐跳诊断；记录拒绝目标身份，不记录 URL 中的凭据。"""

from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit


def record_ad_hop(events, *, url, resource_type, request_scope, hop_index=0, denial_reason=None):
    if events is None or len(events) >= 256:
        return None
    try:
        parsed = urlsplit(url)
        host = str(parsed.hostname or "").rstrip(".").casefold()
        if parsed.scheme.casefold() not in {"http", "https"} or not host:
            raise ValueError("仅 HTTP(S) 目标允许输出源站与路径")
        if ":" in host:
            host = "[" + host + "]"
        netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
        safe_url = urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", "", ""))
    except ValueError:
        safe_url = ""
    event = {
        "hop_index": hop_index,
        "url": safe_url,
        "url_sha256": sha256(url.encode("utf-8")).hexdigest(),
        "resource_type": resource_type or "unknown",
        "request_scope": request_scope or "subresource",
        "resolved_addresses": [],
        "denial_reason": denial_reason,
    }
    events.append(event)
    return event
