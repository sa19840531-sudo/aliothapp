"""Web v2 路由身份：业务地址与观察状态分离，保留不透明编码。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import unquote, urlsplit, urlunsplit

from services.api.app.domain.page.web_origin import canonical_web_origin

WEB_URL_IDENTITY_VERSION = "web-url-identity.v2"
_LOCAL_KEYS = frozenset(
    {"tab", "filter", "sort", "order", "offset", "cursor", "limit", "expanded", "page_size"}
)
_TRACKING_KEYS = frozenset({"fbclid", "from", "gclid", "ref", "referrer", "source", "spm"})


@dataclass(frozen=True, kw_only=True)
class WebUrlIdentityPolicy:
    """仅由可信 Run 快照装配；旧版本明确失败关闭。"""

    version: str = WEB_URL_IDENTITY_VERSION

    def __post_init__(self) -> None:
        if self.version != WEB_URL_IDENTITY_VERSION:
            raise ValueError("WEB_POLICY_VERSION_UNSUPPORTED: identity_policy_version")

    def canonical(self, url: str) -> str:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("Web 逻辑页面 URL 不能为空")
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.casefold()
        if (
            scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Web 逻辑页面 URL 必须是无凭据绝对 HTTP(S) URL")
        netloc = urlsplit(canonical_web_origin(url.strip())).netloc
        fragment = parsed.fragment
        if fragment.startswith(("/", "!/")):
            prefix = "!" if fragment.startswith("!") else ""
            route = urlsplit(fragment.removeprefix("!"))
            if route.netloc or route.fragment:
                raise ValueError("WEB_URL_HASH_NEEDS_REVIEW")
            fragment = prefix + urlunsplit(("", "", route.path, _business_query(route.query), ""))
        elif fragment:
            # 与图相同的普通锚点留作观察状态；结构不明的 Hash 禁止合并。
            if "=" in fragment or "?" in fragment or "/" in fragment or "%" in fragment:
                raise ValueError("WEB_URL_HASH_NEEDS_REVIEW")
            fragment = ""
        return urlunsplit(
            (scheme, netloc, parsed.path or "/", _business_query(parsed.query), fragment)
        )

    def logical_id(self, url: str) -> str:
        canonical = self.canonical(url)
        return f"web-logical:{sha256(canonical.encode('utf-8')).hexdigest()}"


def _business_query(query: str) -> str:
    """只解码识别已知状态键，不解码重编码不透明业务值或重排重复键。"""
    retained: list[str] = []
    for item in query.split("&"):
        key, _, value = item.partition("=")
        try:
            normalized = unquote(key, errors="strict").casefold()
            decoded_value = unquote(value, errors="strict")
        except UnicodeDecodeError:
            retained.append(item)
            continue
        if normalized in _LOCAL_KEYS or (normalized == "page" and decoded_value.isdecimal()):
            continue
        if normalized.startswith("utm_") or normalized in _TRACKING_KEYS:
            continue
        retained.append(item)
    return "&".join(retained)


DEFAULT_WEB_URL_IDENTITY_POLICY = WebUrlIdentityPolicy()
